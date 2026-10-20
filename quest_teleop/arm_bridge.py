"""Run on Thor: receive end-effector targets and forward them to vendor ROS IK."""
import argparse
import math
from pathlib import Path
import time
import uuid

from .ipc import FramePublisher


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8002)
    parser.add_argument('--stop-file', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    from .devices.thor import Thor
    robot = Thor()
    targets = {}
    received = 0.
    publisher = None
    ready = {}
    epochs = {}

    def exchange(request):
        nonlocal targets, received
        incoming = request['targets']
        validated = {}
        for side, values in incoming.items():
            if side not in ('left', 'right') or len(values) != 7 or not all(math.isfinite(v) for v in values):
                raise ValueError('invalid end-effector target')
            norm = math.sqrt(sum(v * v for v in values[3:]))
            if not .9 <= norm <= 1.1:
                raise ValueError('invalid target quaternion')
            validated[side] = list(values[:3]) + [v / norm for v in values[3:]]
        now = time.time_ns()
        age = (now - request.get('feedback_ns', 0)) / 1e9
        targets = {side: values for side, values in validated.items()
                   if 0 <= age <= .2 and ready[side]
                   and request.get('epochs', {}).get(side) == epochs[side]}
        received = time.monotonic()
        return {'recv_unix_ns': now, 'prepared': robot.prepared(), 'epochs': dict(epochs),
                'actual': {side: robot.measured_pose(side) for side in ('left', 'right')}}

    try:
        publisher = FramePublisher(args.host, args.port, on_request=exchange)
        print(f'Thor target bridge listening on {args.host}:{args.port}. Ctrl+C stops.', flush=True)
        while not (args.stop_file and args.stop_file.exists()):
            started = time.monotonic()
            robot.spin()
            prepared = robot.prepared()
            for side in ('left', 'right'):
                available = prepared and robot.measured_pose(side) is not None
                if ready.get(side) != available:
                    epochs[side] = uuid.uuid4().hex
                ready[side] = available
            publisher.poll()
            if not publisher.clients or started - received > .2 or not robot.prepared():
                targets.clear()
            for side in list(targets):
                if robot.measured_pose(side) is None:
                    del targets[side]
            robot.publish(targets)
            time.sleep(max(0., .02 - (time.monotonic() - started)))
    except KeyboardInterrupt:
        print('STOP: Ctrl+C', flush=True)
    finally:
        if publisher is not None:
            publisher.close()
        robot.close()


if __name__ == '__main__':
    main()
