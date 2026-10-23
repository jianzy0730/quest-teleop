"""Quest landmarks to absolute Wuji Hand 2 angles for either side."""
import json
from pathlib import Path
import time

import numpy as np
import yaml

ASSETS = Path(__file__).resolve().parent / 'assets'


class OneEuroFilter:
    """Speed-adaptive joint filtering in radians, using source sample times.

    Casiez et al., CHI 2012: https://gery.casiez.net/1euro/
    Fast motion increases the cutoff; no lookahead or velocity cap is used.
    """

    def __init__(self, min_cutoff=3., beta=8., derivative_cutoff=3.):
        self.min_cutoff, self.beta = min_cutoff, beta
        self.derivative_cutoff = derivative_cutoff
        self.timestamp = self.filtered = None
        self.derivative = 0.

    @staticmethod
    def alpha(cutoff, dt):
        r = 2 * np.pi * cutoff * dt
        return r / (1 + r)

    def __call__(self, value, timestamp):
        dt = None if self.timestamp is None else timestamp - self.timestamp
        if dt is None or not 0 < dt <= .25:
            # Do not blend across a tracking gap or a Quest clock restart.
            self.filtered = value.copy()
            self.derivative = np.zeros_like(value)
        else:
            velocity = (value - self.filtered) / dt
            a = self.alpha(self.derivative_cutoff, dt)
            self.derivative += a * (velocity - self.derivative)
            cutoff = self.min_cutoff + self.beta * np.abs(self.derivative)
            self.filtered += self.alpha(cutoff, dt) * (value - self.filtered)
        self.timestamp = timestamp
        return self.filtered.copy()


class WujiHandRetargeter:
    def __init__(self, side='right'):
        if side not in ('left', 'right'):
            raise ValueError('side must be left or right')
        from .vendor.wuji_retargeting import Retargeter
        from .vendor.wuji_retargeting.mediapipe import estimate_frame_from_hand_points, OPERATOR2MANO_RIGHT, OPERATOR2MANO_LEFT
        from scipy.spatial.transform import Rotation
        config = yaml.safe_load((ASSETS / 'retarget.yaml').read_text())
        config['__yaml_dir'] = str(ASSETS)
        prefix = side[0] + '_'
        config['optimizer']['urdf_path'] = side + '.urdf'
        config['optimizer']['link_naming']['prefix'] = prefix
        self.retargeter = Retargeter.from_config(config, side)
        robot = self.retargeter.optimizer.robot
        fingers = ['thumb', 'index_finger', 'middle_finger', 'ring_finger', 'pinky']
        expected = []
        for f in fingers:
            suffixes = ['cmc_flex', 'cmc_abd', 'mcp', 'ip'] if f == 'thumb' else ['mcp_flex', 'mcp_abd', 'pip', 'dip']
            expected.extend(f'{prefix}{f}_{s}' for s in suffixes)
        self.order = []
        for name in expected:
            joint_id = robot.model.getJointId(name)
            if joint_id >= robot.model.njoints or robot.model.joints[joint_id].nq != 1:
                raise RuntimeError('unexpected Hand 2 model joint: ' + name)
            self.order.append(int(robot.model.joints[joint_id].idx_q))
        if sorted(self.order) != list(range(20)):
            raise RuntimeError('Hand 2 model joint mapping is not bijective')
        self.frame_names = [prefix + 'wrist'] + [f'{prefix}{f}_{link}' for f in fingers for link in ['proximal_abd', 'middle', 'distal', 'tip']]
        model_points = self.fk(np.zeros(20))
        # Derive canonical-to-model rotation from the model's own palm frame.
        # It is device independent and also removes the model palm's slight tilt.
        rotation = estimate_frame_from_hand_points(model_points) @ (OPERATOR2MANO_RIGHT if side == 'right' else OPERATOR2MANO_LEFT)
        angles = Rotation.from_matrix(rotation).as_euler('xyz', degrees=True)
        self.retargeter.rotation_xyz = dict(zip('xyz', angles.tolist()))
        driver_config = json.loads((ASSETS / 'hand.json').read_text(encoding='utf-8'))
        self.limits = np.asarray(driver_config['gestures']['joint_limits_rad'])
        if not np.allclose(robot.joint_limits[self.order], self.limits, atol=1e-6):
            raise RuntimeError('retarget model and driver joint limits differ')
        self.mapping = dict(zip(expected, self.order))
        self.pose_filter = OneEuroFilter()

    def fk(self, q_sdk):
        robot = self.retargeter.optimizer.robot
        q = np.zeros(20)
        q[self.order] = q_sdk
        robot.compute_forward_kinematics(q)
        return np.array([robot.get_link_pose(robot.get_link_index(n))[:3, 3] for n in self.frame_names])

    @staticmethod
    def validate(points):
        p = np.asarray(points, dtype=float)
        if p.shape != (21, 3) or not np.all(np.isfinite(p)):
            raise ValueError('21 finite xyz keypoints required')
        palm_a, palm_b = p[5] - p[0], p[9] - p[0]
        if not .03 < np.linalg.norm(palm_a) < .18 or not .03 < np.linalg.norm(palm_b) < .18:
            raise ValueError('invalid palm size / units')
        if np.linalg.norm(np.cross(palm_a, palm_b)) < .00015:
            raise ValueError('degenerate palm geometry')
        for base in (1, 5, 9, 13, 17):
            lengths = np.linalg.norm(np.diff(p[base:base + 4], axis=0), axis=1)
            if np.any(lengths < .006) or np.any(lengths > .085):
                raise ValueError('invalid finger geometry')
        return p

    def solve(self, points, timestamp=None):
        # Unity is left handed. Reflect once before constructing the anatomical
        # right-handed coordinate frame. Points are wrist-local metres already.
        p = self.validate(points) * [1, 1, -1]
        q = self.retargeter.retarget(p)[self.order]
        if q.shape != (20,) or not np.all(np.isfinite(q)):
            self.retargeter.reset()
            raise ValueError('invalid retarget output')
        q = np.clip(q, self.limits[:, 0], self.limits[:, 1])
        return self.pose_filter(q, time.monotonic() if timestamp is None else timestamp)
