"""Validate normalized live Quest frames for both followers."""
import time


class FrameAssembler:
    """Pair source frames in memory and retain each packet's actual arrival time."""

    def __init__(self, only='both', side='right'):
        sides = ('Left', 'Right') if side == 'both' else (side.title(),)
        kinds = ('landmarks',) if only == 'hand' else ('wrist', 'landmarks')
        self.required = {s: {s + '/' + kind for kind in kinds} for s in sides}
        self.pending = {}
        self.latest = {}
        self.head = None

    def add(self, record):
        touched = {}
        for packet in record['packets']:
            key = packet.get('key')
            packet = {**packet, 'recv_unix_ns': packet.get('recv_unix_ns', record['recv_unix_ns'])}
            if key == 'Head/pose':
                self.head = packet
            elif key and key.split('/')[0] in self.required:
                side = key.split('/')[0]
                if key not in self.required[side]:
                    continue
                debug = packet.get('debug') or {}
                source = debug.get('source_frame_seq'), debug.get('source_ts_ns')
                if not all(type(value) is int and value >= 0 for value in source):
                    continue
                source = (side, *source)
                pair = self.pending.setdefault(source, {})
                if pair is not None:
                    pair[key] = packet
                    touched[source] = pair
        changed = False
        for source, pair in touched.items():
            if self.required[source[0]] <= pair.keys():
                self.latest[source[0]] = list(pair.values())
                changed = True
                self.pending[source] = None  # Suppress duplicates of completed frames.
        while len(self.pending) > 32 * len(self.required):
            del self.pending[next(iter(self.pending))]
        if changed:
            # Each side advances independently. Retained packets keep their
            # original timestamps, so another side cannot make them fresh.
            packets = [p for pair in self.latest.values() for p in pair]
            return {'recv_unix_ns': max(p['recv_unix_ns'] for p in packets),
                    'packets': packets + ([self.head] if self.head else [])}
        return None


def packets_by_key(frame):
    return {packet['key']: (packet if 'recv_unix_ns' in packet else frame, packet)
            for packet in frame['packets'] if packet.get('key')}


class LiveGate:
    """Fresh frames; command sequence survives a confirmed source restart."""

    def __init__(self):
        self.seq = -1
        self.source_seq = self.source_ns = self.offset = None
        self.restart_candidate = None
        self.restarted = False

    @staticmethod
    def fresh(record, now_ns):
        age = (now_ns - record['recv_unix_ns']) / 1e9
        if not -.1 <= age <= .2:
            raise TimeoutError(f'Quest input age {age:.3f}s; live input required')

    def accept(self, record, packet):
        self.fresh(record, time.time_ns())
        debug = packet.get('debug') or {}
        seq, source_ns = debug.get('source_frame_seq'), debug.get('source_ts_ns')
        if type(seq) is not int or type(source_ns) is not int or seq < 0 or source_ns < 0:
            raise ValueError('enable Debug Info in HTS; nonnegative frame sequence/time required')
        if self.source_seq is not None and (seq < self.source_seq or source_ns < self.source_ns):
            candidate = self.restart_candidate
            self.restart_candidate = (seq, source_ns)
            # One reordered packet is not a restart; confirm advancing frames.
            if candidate is None or seq <= candidate[0] or source_ns <= candidate[1]:
                return False
            self.offset = None
            self.restarted = True
        elif seq == self.source_seq or (self.source_ns is not None and source_ns <= self.source_ns):
            return False
        self.restart_candidate = None
        # Backlog is measured on the receiver's clock; network RTT correction
        # changes local freshness but must not shift the source-time baseline.
        offset = (record.get('source_recv_unix_ns', record['recv_unix_ns']) - source_ns) / 1e9
        self.offset = offset if self.offset is None else min(self.offset, offset)
        if offset - self.offset > .15:
            raise TimeoutError('Quest transport backlog >150ms')
        self.source_seq, self.source_ns = seq, source_ns
        self.seq += 1
        return True


class WristGate(LiveGate):
    def __init__(self, side='right'):
        super().__init__()
        self.wrist_key = side.title() + '/wrist'
        self.hand_key = side.title() + '/landmarks'

    def accept(self, packets, calibration=False):
        now = time.time_ns()
        for key in (self.wrist_key, self.hand_key) + (('Head/pose',) if calibration or self.restarted else ()):
            self.fresh(packets[key][0], now)
        record, wrist = packets[self.wrist_key]
        _, hand = packets[self.hand_key]
        wrist_debug, hand_debug = wrist.get('debug') or {}, hand.get('debug') or {}
        if any(wrist_debug.get(key) != hand_debug.get(key)
               for key in ('source_frame_seq', 'source_ts_ns')):
            return False  # Never combine different source frames.
        return super().accept(record, wrist)
