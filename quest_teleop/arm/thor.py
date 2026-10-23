"""Run on Thor: receive end-effector targets and forward them to vendor ROS IK."""
import argparse
from functools import partial
import json
import math
import os
from pathlib import Path
import time
import uuid

from ..config import load_config
from ..ipc import FramePublisher


CONTROL_TARGET = {'robot_mode': [1, 1], 'input': 1, 'ik_source': 2}


class ModePreparation:
    """One asynchronous preparation per follower session, confirmed by ROS feedback."""

    def __init__(self, node, request_type):
        self.node, self.request_type = node, request_type
        self.session = None
        self.status = {'phase': 'idle'}
        self.pending = None
        self.future = None
        self.deadline = 0.

    def request(self, session):
        if session == self.session:
            return
        self.cancel()
        self.session = session
        self.status = {'phase': 'waiting for control feedback'}

    def cancel(self):
        if self.future is not None:
            self.future.cancel()
        self.future = self.pending = None

    def fail(self, detail):
        self.cancel()
        self.status = {'phase': 'failed', 'detail': detail + '; restart teleoperation to retry'}
        print(json.dumps({'mode_preparation': self.status}), flush=True)

    def tick(self, control, active):
        if not self.session or self.status['phase'] in ('ready', 'failed'):
            return
        if not active:
            if self.pending is not None:
                self.fail('controller disconnected during mode preparation')
            return
        now = time.monotonic()
        if self.pending is not None:
            key, value, started = self.pending
            if self.future is not None:
                if not self.future.done():
                    if now >= self.deadline:
                        self.fail('control service timed out: ' + key)
                    return
                try:
                    reply = self.future.result()
                    if reply is None or not reply.success or reply.error_code != 0:
                        self.fail('control service rejected ' + key + ': ' + getattr(reply, 'message', 'no response'))
                        return
                except Exception as error:
                    self.fail('control service failed: ' + str(error))
                    return
                self.future = None
                self.deadline = now + 3.
                self.status = {'phase': 'confirming', 'setting': key, 'value': value}
            sample = self.node.control_samples.get(key)
            expected = [value, value] if key == 'robot_mode' else value
            if not (sample and sample['seen'] >= started and control.get(key) == expected):
                if now >= self.deadline:
                    self.fail('mode feedback timed out: ' + key)
                return
            self.pending = None
        if any(control.get(key) is None for key in CONTROL_TARGET):
            self.status = {'phase': 'waiting for control feedback'}
            return
        if control == CONTROL_TARGET:
            self.status = {'phase': 'ready'}
            print(json.dumps({'mode_preparation': self.status}), flush=True)
            return
        # Disconnect the input before changing the IK source or robot mode.
        if control['input'] != 0 and (control['ik_source'] != 2 or control['robot_mode'] != [1, 1]):
            key, value = 'input', 0
        elif control['ik_source'] != 2:
            key, value = 'ik_source', 2
        elif control['robot_mode'] != [1, 1]:
            key, value = 'robot_mode', 1
        else:
            key, value = 'input', 1
        client = self.node.control_clients[key]
        if not client.service_is_ready():
            self.status = {'phase': 'waiting for control service', 'setting': key}
            return
        request = self.request_type()
        request.data = value
        try:
            self.future = client.call_async(request)
        except Exception as error:
            self.fail('cannot call control service: ' + str(error))
            return
        self.pending = key, value, now
        self.deadline = now + 8.
        self.status = {'phase': 'switching', 'setting': key, 'value': value}
        print(json.dumps({'mode_preparation': self.status}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8002)
    parser.add_argument('--stop-file', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    import rclpy
    from rclpy.node import Node
    from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy, HistoryPolicy
    from geometry_msgs.msg import PoseStamped, Pose
    from std_msgs.msg import Int16MultiArray, Int32
    from marvin_msgs.msg import IKRequest
    from marvin_msgs.srv import Int as IntService

    class Thor(Node):
        """Read feedback and publish both arms together from the follower loop."""

        def __init__(self):
            os.environ.setdefault('ROS_DOMAIN_ID', str(load_config()['ros_domain_id']))
            rclpy.init()
            super().__init__('quest_teleop_thor')
            self.eef = {}
            self.control_samples = {}
            self.control_topics = {'robot_mode': '/tj/info/robot_state',
                                   'input': '/tj/control/input_mode',
                                   'ik_source': '/tj/info/ik_request_mux/active_source'}
            latched = QoSProfile(reliability=ReliabilityPolicy.RELIABLE,
                                 durability=DurabilityPolicy.TRANSIENT_LOCAL,
                                 history=HistoryPolicy.KEEP_LAST, depth=1)
            for side in ('left', 'right'):
                self.create_subscription(PoseStamped, f'/tj/info/eef_{side}',
                                         partial(self.on_pose, side), 10)
            self.create_subscription(Int16MultiArray, self.control_topics['robot_mode'],
                                     lambda m, info: self.control_sample('robot_mode', list(m.data), info), 10)
            self.create_subscription(Int32, self.control_topics['input'],
                                     lambda m, info: self.control_sample('input', int(m.data), info), latched)
            self.create_subscription(Int32, self.control_topics['ik_source'],
                                     lambda m, info: self.control_sample('ik_source', int(m.data), info), latched)
            self.pub_ik = self.create_publisher(IKRequest, '/tj/control/ik_request/vla', 10)
            self.control_clients = {
                key: self.create_client(IntService, '/tj/control/' + service)
                for key, service in (('robot_mode', 'set_mode'), ('input', 'set_input'),
                                     ('ik_source', 'set_ik_input'))}

        def on_pose(self, side, message):
            stamp = message.header.stamp.sec + message.header.stamp.nanosec / 1e9
            p, q = message.pose.position, message.pose.orientation
            self.eef[side] = ([p.x, p.y, p.z, q.x, q.y, q.z, q.w], stamp or time.time())

        def spin(self):
            for _ in range(24):
                rclpy.spin_once(self, timeout_sec=0.)

        def measured_pose(self, side):
            values, stamp = self.eef.get(side, (None, 0.))
            return values if 0 <= time.time() - stamp < .5 else None

        def control_sample(self, key, value, info):
            gid = info.get('publisher_gid') if isinstance(info, dict) else getattr(info, 'publisher_gid', None)
            if gid is None:
                publishers = self.get_publishers_info_by_topic(self.control_topics[key])
                gid = publishers[0].endpoint_gid if len(publishers) == 1 else None
            self.control_samples[key] = {'value': value, 'seen': time.monotonic(),
                                         'gid': bytes(gid) if gid is not None else None}

        def control_state(self):
            values = {}
            for key, topic in self.control_topics.items():
                sample = self.control_samples.get(key)
                publishers = {bytes(item.endpoint_gid) for item in self.get_publishers_info_by_topic(topic)}
                live = sample is not None and len(publishers) == 1 and sample['gid'] in publishers
                if key == 'robot_mode':
                    live = live and time.monotonic() - sample['seen'] <= 1.
                values[key] = sample['value'] if live else None
            return values

        @staticmethod
        def make_pose(values):
            result = Pose()
            result.position.x, result.position.y, result.position.z = map(float, values[:3])
            result.orientation.x, result.orientation.y, result.orientation.z, result.orientation.w = map(float, values[3:])
            return result

        def publish(self, targets):
            if not targets:
                return
            request = IKRequest()
            request.header.stamp = self.get_clock().now().to_msg()
            request.header.frame_id = 'base_link'
            request.left_hand_pose = self.make_pose(targets['left']) if 'left' in targets else Pose()
            request.right_hand_pose = self.make_pose(targets['right']) if 'right' in targets else Pose()
            request.available = ['left' in targets, 'right' in targets] + [False] * 9
            self.pub_ik.publish(request)

        def close(self):
            self.destroy_node()
            rclpy.try_shutdown()

    robot = Thor()
    preparation = ModePreparation(robot, IntService.Request)
    targets = {}
    received = 0.
    publisher = None
    ready = {}
    epochs = {}
    prepared = False
    control = {}

    def exchange(request):
        nonlocal targets, received
        incoming = request['targets']
        incoming_epochs = request.get('epochs', {})
        feedback_ns = request.get('feedback_ns', 0)
        session = request.get('prepare_session')
        if (not isinstance(incoming, dict) or not isinstance(incoming_epochs, dict)
                or type(feedback_ns) is not int or feedback_ns < 0):
            raise ValueError('targets/epochs must be objects; feedback_ns must be a nonnegative integer')
        if session is not None and (not isinstance(session, str) or not 1 <= len(session) <= 64):
            raise ValueError('prepare_session must be a nonempty string of at most 64 characters')
        validated = {}
        for side, values in incoming.items():
            if (side not in ('left', 'right') or not isinstance(values, list) or len(values) != 7
                    or not all(type(v) in (int, float) and math.isfinite(v) for v in values)):
                raise ValueError('invalid end-effector target')
            norm = math.sqrt(sum(v * v for v in values[3:]))
            if not .9 <= norm <= 1.1:
                raise ValueError('invalid target quaternion')
            validated[side] = list(values[:3]) + [v / norm for v in values[3:]]
        if session is not None:
            preparation.request(session)
        now = time.time_ns()
        age = (now - feedback_ns) / 1e9
        targets = {side: values for side, values in validated.items()
                   if 0 <= age <= .2 and ready[side]
                   and incoming_epochs.get(side) == epochs[side]}
        rejected = {}
        for side in validated.keys() - targets.keys():
            rejected[side] = ('control mode not ready' if not prepared else
                              'end-effector feedback unavailable' if not ready[side] else
                              'calibration epoch changed' if incoming_epochs.get(side) != epochs[side] else
                              f'feedback round trip expired ({age:.3f}s)')
        received = time.monotonic()
        return {'recv_unix_ns': now, 'prepared': prepared, 'epochs': dict(epochs),
                'mode_preparation': dict(preparation.status),
                'control': control, 'accepted': list(targets), 'rejected': rejected,
                'actual': {side: robot.measured_pose(side) for side in ('left', 'right')}}

    try:
        publisher = FramePublisher(args.host, args.port, on_request=exchange)
        print(f'Thor target bridge listening on {args.host}:{args.port}. Ctrl+C stops.', flush=True)
        while not (args.stop_file and args.stop_file.exists()):
            started = time.monotonic()
            robot.spin()
            control = robot.control_state()
            preparation.tick(control, bool(publisher.clients) and time.monotonic() - received <= .2)
            prepared = control == CONTROL_TARGET and preparation.status['phase'] in ('idle', 'ready')
            for side in ('left', 'right'):
                available = prepared and robot.measured_pose(side) is not None
                if ready.get(side) != available:
                    epochs[side] = uuid.uuid4().hex
                ready[side] = available
            publisher.poll()
            if not publisher.clients or started - received > .2 or not prepared:
                targets.clear()
            for side in list(targets):
                if robot.measured_pose(side) is None:
                    del targets[side]
            robot.publish(targets)
            time.sleep(max(0., .02 - (time.monotonic() - started)))
    except KeyboardInterrupt:
        print('STOP: Ctrl+C', flush=True)
    finally:
        preparation.cancel()
        if publisher is not None:
            publisher.close()
        robot.close()


if __name__ == '__main__':
    main()
