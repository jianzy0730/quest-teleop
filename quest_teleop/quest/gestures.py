"""Victory-sign detection from the existing wrist-local hand landmarks."""
import time

import numpy as np

from .frames import LiveGate


def is_victory(points):
    p = np.asarray(points, dtype=float)
    if p.shape != (21, 3) or not np.isfinite(p).all():
        return False
    palm = np.linalg.norm(p[9] - p[0])
    if not .03 < palm < .18:
        return False
    extended, folded = [], []
    for base in (5, 9, 13, 17):
        bones = np.diff(p[base:base + 4], axis=0)
        lengths = np.linalg.norm(bones, axis=1)
        if np.any(lengths < .006) or np.any(lengths > .085):
            return False
        directions = bones / lengths[:, None]
        straight = np.sum(directions[:-1] * directions[1:], axis=1)
        reach = np.linalg.norm(p[base + 3] - p[base]) / lengths.sum()
        extended.append(bool(np.all(straight > .8) and reach > .9))
        folded.append(bool(reach < .75 and np.any(straight < .5)))
    return (extended[0] and extended[1] and folded[2] and folded[3]
            and np.linalg.norm(p[8] - p[12]) > .3 * palm)


class VictoryGesture:
    """Trigger once per held sign; require release before another trigger."""

    def __init__(self):
        self.gate = LiveGate()
        self.since = self.last = None
        self.latched = False

    def block(self):
        self.since = None
        self.latched = True

    def update(self, packet):
        now = time.monotonic()
        if self.last is not None and now - self.last > .2:
            self.since = None
        try:
            if not self.gate.accept(packet, packet):
                return False
            if self.gate.restarted:
                self.since = None
                self.gate.restarted = False
            victory = is_victory(packet['data']['points'])
        except (KeyError, TypeError, ValueError, TimeoutError):
            self.since = None
            return False
        self.last = now
        if not victory:
            self.since = None
            self.latched = False
        elif not self.latched:
            if self.since is None:
                self.since = now
            elif now - self.since >= .7:
                self.latched = True
                return True
        return False
