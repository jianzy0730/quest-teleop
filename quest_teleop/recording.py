"""Quest frame recordings and replay through the existing follower IPC."""
import argparse
from copy import deepcopy
import json
import math
from pathlib import Path
import time
import uuid

from .ipc import FramePublisher, write_status


FORMAT = 'quest-teleop/frames-v1'


def read_header(path):
    with Path(path).open(encoding='utf-8') as source:
        header = json.loads(source.readline())
    if not isinstance(header, dict) or header.get('format') != FORMAT:
        raise ValueError('Unsupported recording format')
    config = header.get('config')
    if not isinstance(config, dict) or config.get('only') not in ('hand', 'arm', 'both') or config.get('side') not in ('left', 'right', 'both'):
        raise ValueError('Invalid recording devices')
    if type(config.get('scale')) not in (int, float) or not math.isfinite(config['scale']) or config['scale'] <= 0:
        raise ValueError('Invalid recording scale')
    return header


def frames(path):
    previous = -1.
    with Path(path).open(encoding='utf-8') as source:
        next(source)
        for line in source:
            row = json.loads(line)
            if 'summary' in row:
                break
            t, frame = row['t'], row['frame']
            if not math.isfinite(t) or t < 0 or t < previous:
                raise ValueError('Recording timestamps must advance')
            if type(frame['recv_unix_ns']) is not int or not isinstance(frame['packets'], list):
                raise ValueError('Invalid recorded frame')
            previous = t
            yield t, frame


class Recorder:
    """Called under the web controller lock; writes only during explicit capture."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.file = None
        self.last = None

    def start(self, config):
        if self.file:
            raise ValueError('Recording is already active')
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / (time.strftime('%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:6] + '.jsonl')
        self.file = self.path.with_suffix('.partial').open('x', encoding='utf-8')
        self.started = time.monotonic()
        self.count = 0
        self.origin = self.last_ns = None
        self.header = {'format': FORMAT, 'created': time.strftime('%Y-%m-%d %H:%M:%S'),
                       'config': {k: config[k] for k in ('only', 'side', 'scale')}}
        self.file.write(json.dumps(self.header) + '\n')

    def add(self, frame):
        if not self.file:
            return
        stamp = frame.get('source_recv_unix_ns', frame['recv_unix_ns'])
        if stamp == self.last_ns:
            return
        # A retained cache reply is not a new sample. Preserve each side's age.
        if not -.1 <= (time.time_ns() - frame['recv_unix_ns']) / 1e9 <= .2:
            return
        frame = deepcopy(frame)
        frame['recv_unix_ns'] = frame.pop('source_recv_unix_ns', stamp)
        for packet in frame['packets']:
            if 'source_recv_unix_ns' in packet:
                packet['recv_unix_ns'] = packet.pop('source_recv_unix_ns')
        if self.origin is None:
            self.origin = stamp
        # Wall clock changes cannot produce a backwards replay timeline.
        if self.last_ns is not None and stamp <= self.last_ns:
            raise ValueError('Receiver clock moved backwards; start a new recording')
        self.file.write(json.dumps({'t': (stamp - self.origin) / 1e9, 'frame': frame},
                                   allow_nan=False, separators=(',', ':')) + '\n')
        self.last_ns = stamp
        self.count += 1

    def stop(self):
        if not self.file:
            return self.last
        self.last = {**self.header, 'id': self.path.name, 'frames': self.count,
                     'duration_s': (self.last_ns - self.origin) / 1e9 if self.count else 0,
                     'elapsed_s': round(time.monotonic() - self.started, 3)}
        self.file.write(json.dumps({'summary': self.last}) + '\n')
        self.file.close()
        self.file = None
        if self.count:
            self.path.with_suffix('.partial').replace(self.path)
        else:
            self.path.with_suffix('.partial').unlink()
        return self.last

    def status(self):
        return {'active': self.file is not None,
                'elapsed_s': time.monotonic() - self.started if self.file else 0,
                'frames': self.count if self.file else 0, 'last': self.last}


class ReplayClock:
    def __init__(self, first, speed):
        self.origin = first['recv_unix_ns']
        self.speed = speed
        self.sources = {}
        for packet in first['packets']:
            debug = packet.get('debug')
            if debug:
                self.sources[packet['key'].split('/')[0]] = debug['source_ts_ns']

    def frame(self, original, base_ns, seq_offset=0):
        frame = deepcopy(original)
        frame.pop('source_recv_unix_ns', None)
        frame['recv_unix_ns'] = base_ns + int((original['recv_unix_ns'] - self.origin) / self.speed)
        for packet in frame['packets']:
            packet.pop('source_recv_unix_ns', None)
            packet['recv_unix_ns'] = base_ns + int((packet.get('recv_unix_ns', original['recv_unix_ns']) - self.origin) / self.speed)
            debug = packet.get('debug')
            if debug:
                side = packet['key'].split('/')[0]
                origin = self.sources.setdefault(side, debug['source_ts_ns'])
                debug['source_ts_ns'] = base_ns + int((debug['source_ts_ns'] - origin) / self.speed)
                debug['source_frame_seq'] += seq_offset
        return frame


def ready(output, only, side):
    names = ([] if only == 'hand' else ['arms'])
    if only != 'arm':
        names += ['hand_' + s for s in (('left', 'right') if side == 'both' else (side,))]
    for name in names:
        try:
            state = json.loads((output / (name + '.status.json')).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return False
        if time.time_ns() - state['updated_ns'] > 2_000_000_000:
            return False
        if name == 'arms':
            if not state['prepared'] or any(not v['calibrated'] or v['paused'] for v in state['sides'].values()):
                return False
        elif state['phase'] != 'running':
            return False
    return True


def replay(path, output, host, port, speed=1., only='both', side='right'):
    read_header(path)
    source = frames(path)
    _, first = next(source)
    clock = ReplayClock(first, speed)
    stop = output / 'STOP'
    publisher = FramePublisher(host, port)
    count = offset = 0
    last_report = 0.

    def report(phase, position=0.):
        write_status(output / 'replay.status.json', {'phase': phase, 'position_s': position,
                                                     'frames': count, 'speed': speed})
    try:
        report('preparing')
        # Hold the initial input until SDK initialization and arm calibration
        # finish; none of the recorded movement is consumed during this wait.
        while not stop.exists() and not ready(output, only, side):
            publisher.publish(clock.frame(first, time.time_ns(), offset))
            offset += 1
            end = time.monotonic() + .02
            while time.monotonic() < end and not stop.exists():
                publisher.poll()
                time.sleep(.002)
            if time.monotonic() - last_report >= .5:
                report('preparing')
                last_report = time.monotonic()
        if stop.exists():
            return
        base_ns, begun = time.time_ns(), time.monotonic()
        import itertools
        for t, frame in itertools.chain([(0., first)], source):
            deadline = begun + t / speed
            while time.monotonic() < deadline and not stop.exists():
                publisher.poll()
                if time.monotonic() - last_report >= .5:
                    report('playing', (time.monotonic() - begun) * speed)
                    last_report = time.monotonic()
                time.sleep(min(.002, max(0., deadline - time.monotonic())))
            if stop.exists():
                return
            publisher.publish(clock.frame(frame, base_ns, offset + 1))
            count += 1
        # Give readers time to consume the last frame, then withdraw the cache.
        end = time.monotonic() + .1
        while time.monotonic() < end and not stop.exists():
            publisher.poll()
            time.sleep(.002)
        publisher.latest = None
        report('complete', (time.monotonic() - begun) * speed)
        stop.touch()  # Withdraw followers before closing their frame connection.
    finally:
        publisher.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', type=Path)
    parser.add_argument('--speed', type=float, default=1.)
    parser.add_argument('--frame-host', default='127.0.0.1')
    parser.add_argument('--frame-port', type=int, default=8001)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--only', choices=['both', 'arm', 'hand'], default='both')
    parser.add_argument('--side', choices=['left', 'right', 'both'], default='right')
    args = parser.parse_args()
    if not math.isfinite(args.speed) or not .1 <= args.speed <= 4:
        parser.error('speed must be 0.1..4')
    replay(args.path, args.output, args.frame_host, args.frame_port, args.speed, args.only, args.side)


if __name__ == '__main__':
    main()
