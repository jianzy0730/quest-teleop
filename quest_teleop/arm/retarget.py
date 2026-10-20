"""Relative Quest wrist pose mapping into the Thor base frame."""
import math

import numpy as np


def quaternion(q):
    q = np.asarray(q, dtype=float)
    if q.shape != (4,) or not np.isfinite(q).all():
        raise ValueError('four finite quaternion values required (xyzw)')
    n = np.linalg.norm(q)
    if not .9 <= n <= 1.1:
        raise ValueError('invalid quaternion norm')
    return q / n


def multiply(a, b):
    av, bv = a[:3], b[:3]
    return np.r_[a[3] * bv + b[3] * av + np.cross(av, bv), a[3] * b[3] - av @ bv]


def rotation(q):
    x, y, z, w = quaternion(q)
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])


def pose(values):
    p = np.asarray(values, dtype=float)
    if p.shape != (7,) or not np.isfinite(p).all():
        raise ValueError('seven finite pose values required')
    return np.r_[p[:3], quaternion(p[3:])]


def packet_pose(packet):
    return pose([packet['data'][k] for k in ('x', 'y', 'z', 'qx', 'qy', 'qz', 'qw')])


class WristMapper:
    def rebase(self, wrist, robot):
        """Keep the original control axes when recovering from a data gap."""
        self.wrist = pose(wrist)
        self.robot = pose(robot)
        self.previous_q = self.robot[3:].copy()

    def __init__(self, wrist, robot, head, scale=1.):
        self.rebase(wrist, robot)
        head = pose(head)
        if not math.isfinite(scale) or scale <= 0:
            raise ValueError('scale must be finite and positive')
        self.scale = scale
        # Y-up Unity world: project the initial head forward onto the floor.
        # Unity right/up/forward -> base forward/left/up has determinant -1.
        forward = rotation(head[3:]) @ [0., 0., 1.]
        forward[1] = 0.
        if np.linalg.norm(forward) < .2:
            raise ValueError('look horizontally when calibrating')
        forward /= np.linalg.norm(forward)
        up = np.array([0., 1., 0.])
        right = np.cross(up, forward)
        self.axes = np.array([forward, -right, up])

    def map(self, wrist):
        wrist = pose(wrist)
        position = self.robot[:3] + self.scale * self.axes @ (wrist[:3] - self.wrist[:3])
        # Express a world-frame rotation increment in a reflected basis:
        # R_delta_base = M R_delta_unity M^T. Quaternion vector parts are axial
        # vectors, so transform them with det(M)*M, not M alone.
        inverse = self.wrist[3:] * [-1., -1., -1., 1.]
        delta = multiply(wrist[3:], inverse)
        delta_base = np.r_[-self.axes @ delta[:3], delta[3]]
        q = quaternion(multiply(delta_base, self.robot[3:]))
        if q @ self.previous_q < 0:
            q = -q
        self.previous_q = q.copy()
        return np.r_[position, q]
