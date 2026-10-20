"""Direct Thor ROS 2 interface; targets go to the vendor IK."""
import os
import time
from functools import partial

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy, HistoryPolicy
from geometry_msgs.msg import PoseStamped, Pose
from std_msgs.msg import Int16MultiArray, Int32
from marvin_msgs.msg import IKRequest

from ..config import load_config


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

    def prepared(self):
        values = {}
        for key, topic in self.control_topics.items():
            sample = self.control_samples.get(key)
            publishers = {bytes(item.endpoint_gid) for item in self.get_publishers_info_by_topic(topic)}
            live = sample is not None and len(publishers) == 1 and sample['gid'] in publishers
            if key == 'robot_mode':
                live = live and time.monotonic() - sample['seen'] <= 1.
            values[key] = sample['value'] if live else None
        return values == {'robot_mode': [1, 1], 'input': 1, 'ik_source': 2}

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
