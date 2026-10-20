"""Map Quest wrists on the computer and send end-effector targets to Thor."""
import argparse
import json
import math
from pathlib import Path
import time

from ..config import ROOT, load_config
from ..quest.frames import WristGate, packets_by_key
from ..ipc import FrameSubscriber
from .retarget import WristMapper, packet_pose, pose


class ArmFollower:
    """One wrist mapping; pauses and rebases independently of the other arm."""

    def __init__(self, side, scale):
        self.side, self.scale = side, scale
        self.gate = WristGate(side)
        self.mapper = None
        self.paused = True
        self.target = None
        self.last_input = 0.
        self.sent = 0

    def pause(self):
        if not self.paused:
            print(json.dumps({'side': self.side, 'warning': 'arm paused; waiting for input/feedback'}), flush=True)
        self.paused = True
        self.target = None

    def update(self, packets, actual):
        if actual is None:
            self.pause()
            return
        try:
            actual = pose(actual)
            accepted = self.gate.accept(packets, calibration=self.mapper is None)
            wrist = packet_pose(packets[self.side.title() + '/wrist'][1])
            if not accepted:
                if time.monotonic() - self.last_input > .25:
                    self.pause()
                return
            if self.mapper is None or self.gate.restarted:
                self.gate.fresh(packets['Head/pose'][0], time.time_ns())
                head = packet_pose(packets['Head/pose'][1])
                self.mapper = WristMapper(wrist, actual, head, self.scale)
                self.gate.restarted = False
                print(json.dumps({'side': self.side, 'calibrated': True, 'anchor': actual.tolist(),
                                  'wrist': wrist.tolist(), 'axes': self.mapper.axes.tolist()}), flush=True)
                self.target = None
            elif self.paused:
                self.mapper.rebase(wrist, actual)
                self.target = None
                print(json.dumps({'side': self.side, 'event': 'resumed', 'rebased': True}), flush=True)
            else:
                self.target = self.mapper.map(wrist)
                self.sent += 1
            self.paused = False
            self.last_input = time.monotonic()
        except (TimeoutError, ValueError, KeyError, TypeError):
            self.pause()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frame-host', default='127.0.0.1')
    parser.add_argument('--frame-port', type=int, default=8001)
    parser.add_argument('--thor-host', help='Thor target bridge IP')
    parser.add_argument('--thor-port', type=int)
    parser.add_argument('--side', choices=['left', 'right', 'both'])
    parser.add_argument('--scale', type=float)
    parser.add_argument('--preview', action='store_true', help='Calculate poses without connecting to Thor')
    parser.add_argument('--output', type=Path, default=ROOT / 'logs')
    parser.add_argument('--stop-file', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    config = load_config()
    side = args.side or config['side']
    scale = config['scale'] if args.scale is None else args.scale
    if not math.isfinite(scale) or scale <= 0:
        parser.error('scale must be positive and finite')
    subscriber = FrameSubscriber(args.frame_host, args.frame_port)
    bridge = None if args.preview else FrameSubscriber(args.thor_host or config['thor_host'],
                                                       args.thor_port or config['thor_port'])
    sides = ('left', 'right') if side == 'both' else (side,)
    followers = {side: ArmFollower(side, scale) for side in sides}
    targets = {}
    feedback_ns = 0
    epochs = {}
    feedback = None
    connected = False
    failure = None
    last_report = last_wait = -math.inf
    args.output.mkdir(parents=True, exist_ok=True)
    log_path = args.output / ('arms_' + time.strftime('%Y%m%d_%H%M%S') + '.json')
    print(f'Waiting for Quest at {args.frame_host}:{args.frame_port}. Ctrl+C stops.', flush=True)
    try:
        while not (args.stop_file and args.stop_file.exists()):
            started = time.monotonic()
            try:
                if bridge is not None:
                    reply = bridge.next_latest(timeout=.02, request={
                        'targets': {s: t.tolist() for s, t in targets.items()},
                        'feedback_ns': feedback_ns, 'epochs': epochs})
                    if reply is not None:
                        feedback = reply
                        feedback_ns = feedback['source_recv_unix_ns']
                        for side, follower in followers.items():
                            if epochs.get(side) != feedback['epochs'][side]:
                                follower.pause()
                        epochs = feedback['epochs']
                        if not connected:
                            print(json.dumps({'event': 'Thor connected'}), flush=True)
                            connected = True
                    if feedback is not None and not 0 <= (time.time_ns() - feedback['recv_unix_ns']) / 1e9 <= .2:
                        bridge.close()
                        raise TimeoutError('Thor feedback delayed')
                targets = {}
                frame = subscriber.next_latest(timeout=.02)
                packets = packets_by_key(frame) if frame is not None else None
                prepared = bridge is None or feedback is not None and feedback['prepared']
                for side, follower in followers.items():
                    actual = [0, 0, 0, 0, 0, 0, 1] if bridge is None else (feedback['actual'].get(side) if feedback else None)
                    if not prepared or actual is None:
                        follower.pause()
                    elif packets is not None:
                        follower.update(packets, actual)
                    elif started - follower.last_input > .2:
                        follower.pause()
                    if follower.target is not None:
                        targets[side] = follower.target
                if not prepared and started - last_wait >= 5:
                    print(json.dumps({'phase': 'waiting for Thor bridge' if feedback is None else
                                      'waiting for Thor mode=[1,1], input=1, IK source=2'}), flush=True)
                    last_wait = started
                if bridge is None and started - last_report >= 1:
                    print(json.dumps({'preview': {side: target.tolist() for side, target in targets.items()}}), flush=True)
                    last_report = started
            except (OSError, ValueError, KeyError, TypeError) as error:
                targets.clear()
                feedback = None
                connected = False
                for follower in followers.values():
                    follower.pause()
                if started - last_wait >= 5:
                    print(json.dumps({'warning': 'waiting for Quest/Thor', 'detail': str(error)}), flush=True)
                    last_wait = started
            time.sleep(max(0., .02 - (time.monotonic() - started)))
    except KeyboardInterrupt:
        print('STOP: Ctrl+C', flush=True)
    except Exception as error:
        failure = str(error) or type(error).__name__
        print('STOP: ' + failure, flush=True)
    finally:
        subscriber.close()
        if bridge is not None:
            bridge.close()
        summary = {'preview': args.preview, 'sent': {s: f.sent for s, f in followers.items()}, 'failure': failure}
        log_path.write_text(json.dumps(summary, indent=2), encoding='utf-8')
        print(json.dumps(summary), flush=True)
    if failure:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
