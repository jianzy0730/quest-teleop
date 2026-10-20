"""Base classes and utilities for hand retargeting optimizers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

import nlopt
import numpy as np

from ..robot import RobotWrapper


# Package root for URDF path resolution
_THIS_FILE = Path(__file__).resolve()
_PACKAGE_ROOT = _THIS_FILE.parent.parent

# Unit conversion: internal computations use cm
M_TO_CM = 100.0


def huber_loss_np(x: np.ndarray, delta: float = 2.0) -> np.ndarray:
    """Huber loss function (smooth L1 loss)."""
    abs_x = np.abs(x)
    return np.where(
        abs_x <= delta,
        0.5 * x ** 2,
        delta * (abs_x - 0.5 * delta)
    )


def huber_loss_grad_np(x: np.ndarray, delta: float = 2.0) -> np.ndarray:
    """Gradient of Huber loss w.r.t. x (numpy version)."""
    abs_x = np.abs(x)
    return np.where(abs_x <= delta, x, delta * np.sign(x))


class BaseOptimizer(ABC):
    """Base class for hand retargeting optimizers.

    All parameters are read from configuration dict (loaded from YAML).
    """

    # MediaPipe keypoint indices
    MP_ORIGIN_IDX = 0  # Wrist
    MP_TIP_INDICES = [4, 8, 12, 16, 20]  # Fingertips
    MP_PIP_INDICES = [2, 6, 10, 14, 18]  # PIP joints (thumb uses MCP=2)
    MP_DIP_INDICES = [3, 7, 11, 15, 19]  # DIP joints

    def __init__(self, config: dict):
        """Initialize optimizer from configuration dict.

        Args:
            config: Configuration dict (typically loaded from YAML)
        """
        self.config = config

        # Extract optimizer config
        opt_config = config.get('optimizer', {})
        self.hand_side = opt_config.get('hand_side', 'right').lower()
        if self.hand_side not in ['right', 'left']:
            raise ValueError(f"hand_side must be 'right' or 'left', got {self.hand_side}")

        # Extract retarget config
        retarget_config = config.get('retarget', {})
        self.huber_delta = retarget_config.get('huber_delta', 2.0)
        self.norm_delta = retarget_config.get('norm_delta', 0.04)

        # Load URDF. Honor optimizer.urdf_path (e.g. a Wuji Hand 2 model) up front so the
        # link-name resolution and FK index building below run against the actual
        # hand; otherwise use the bundled default URDF for this side. Loading the
        # override here (rather than swapping it in after indices are built) is what
        # lets a differently-named URDF + optimizer.link_naming resolve correctly.
        urdf_override = opt_config.get('urdf_path')
        if urdf_override:
            urdf_path = Path(urdf_override)
            if not urdf_path.is_absolute():
                yaml_dir = config.get('__yaml_dir')
                if yaml_dir is None:
                    raise RuntimeError(
                        "optimizer.urdf_path is relative but config['__yaml_dir'] not set; "
                        "use Retargeter.from_yaml or set __yaml_dir explicitly"
                    )
                urdf_path = (Path(yaml_dir) / urdf_path).resolve()
            if not urdf_path.exists():
                raise FileNotFoundError(f"optimizer.urdf_path not found: {urdf_path}")
            urdf_path = str(urdf_path)
        else:
            urdf_path = str((_PACKAGE_ROOT / f"wuji-description/hand/body/urdf/{self.hand_side}.urdf").resolve())
        self.robot = RobotWrapper(urdf_path, hand_side=self.hand_side)
        self.num_joints = self.robot.model.nq

        # Setup NLopt optimizer
        self.opt = nlopt.opt(nlopt.LD_SLSQP, self.num_joints)
        self.opt.set_maxeval(50)      # Reduced from 1000 for faster convergence with warm-start
        self.opt.set_ftol_abs(1e-4)   # Relaxed from 1e-6 for faster convergence
        self.opt.set_lower_bounds(self.robot.joint_limits[:, 0].tolist())
        self.opt.set_upper_bounds(self.robot.joint_limits[:, 1].tolist())

        # Link names (resolved from config; default reproduces the default Wuji Hand)
        self._resolve_link_names(config)

        # Build link indices
        self._build_link_indices()

        # Store last solution for warm start
        self.last_qpos = None

    # Default link-name scheme: the default Wuji Hand convention. get_link_index
    # additionally tries the "{hand_side}_" prefix, so these unprefixed names
    # resolve to right_palm_link / right_finger1_link3 / ... on the bundled hands.
    _DEFAULT_LINK_NAMING = {
        "prefix": "",
        "palm": "palm_link",
        "fingers": ["finger1", "finger2", "finger3", "finger4", "finger5"],
        "tip": "{finger}_tip_link",
        "pip": "{finger}_link3",
        "dip": "{finger}_link4",
    }

    def _resolve_link_names(self, config: dict):
        """Resolve the optimizer's required link names from config.

        Hand URDFs name their links differently — Wuji Hand uses
        ``right_finger1_link3`` while Wuji Hand 2 uses ``r_index_finger_middle`` — so
        ``optimizer.link_naming`` maps the optimizer's logical roles (palm and the
        per-finger tip / PIP / DIP) onto whatever the URDF actually calls
        them, keeping the retargeting code URDF-agnostic. Omitting the block
        uses the default Wuji Hand naming.

        ``fingers`` is ordered thumb..pinky. ``prefix`` is
        prepended to every name (e.g. ``r_``); per-finger templates use
        ``{finger}``.
        """
        naming = dict(self._DEFAULT_LINK_NAMING)
        naming.update((config.get('optimizer') or {}).get('link_naming') or {})
        fingers = naming["fingers"]
        if len(fingers) != 5:
            raise ValueError(
                f"optimizer.link_naming.fingers must list exactly 5 fingers "
                f"(thumb..pinky), got {fingers!r}"
            )
        prefix = naming["prefix"]

        def per_finger(template):
            return [prefix + template.format(finger=f) for f in fingers]

        self.origin_link_name = prefix + naming["palm"]
        self.task_link_names = per_finger(naming["tip"])
        self.link3_names = per_finger(naming["pip"])
        self.link4_names = per_finger(naming["dip"])

    def _build_link_indices(self):
        """Build link indices for FK computation."""
        # Collect all needed link names
        all_link_names = (
            [self.origin_link_name] +
            self.task_link_names +
            self.link3_names +
            self.link4_names
        )
        self.computed_link_names = list(dict.fromkeys(all_link_names))
        self.computed_link_indices = [
            self.robot.get_link_index(name) for name in self.computed_link_names
        ]

        # Build index mappings
        self.origin_indices = [
            self.computed_link_names.index(self.origin_link_name)
            for _ in range(5)
        ]
        self.task_indices = [
            self.computed_link_names.index(name) for name in self.task_link_names
        ]
        self.link3_indices = [
            self.computed_link_names.index(name) for name in self.link3_names
        ]
        self.link4_indices = [
            self.computed_link_names.index(name) for name in self.link4_names
        ]

    @classmethod
    def from_config(cls, config: dict) -> "BaseOptimizer":
        """Create optimizer from configuration dict.

        Args:
            config: Configuration dict

        Returns:
            Optimizer instance
        """
        from .adaptive_analytical import AdaptiveOptimizerAnalytical

        registry = {
            'AdaptiveOptimizerAnalytical': AdaptiveOptimizerAnalytical,
        }
        opt_type = config.get('optimizer', {}).get('type', 'AdaptiveOptimizerAnalytical')

        if opt_type not in registry:
            raise ValueError(
                f"Unknown optimizer type: {opt_type}. "
                f"Supported: {sorted(registry)}"
            )
        return registry[opt_type](config)

    @abstractmethod
    def solve(
        self,
        mediapipe_keypoints: np.ndarray,
        last_qpos: Optional[np.ndarray] = None,
    ) -> np.ndarray:
        """Solve for joint angles.

        Args:
            mediapipe_keypoints: (21, 3) MediaPipe keypoints in wrist frame
            last_qpos: Initial guess for optimization (warm start)

        Returns:
            qpos: (num_joints,) joint angles
        """
        pass

    # =========================================================================
    # Common helper methods (shared by subclasses)
    # =========================================================================

    def _get_init_qpos(self, last_qpos: Optional[np.ndarray]) -> np.ndarray:
        """Get initial qpos for optimization (clipped to joint limits).

        Args:
            last_qpos: Optional last qpos from caller

        Returns:
            Initial qpos for optimization
        """
        if last_qpos is not None:
            init_qpos = np.asarray(last_qpos, dtype=np.float64)
        elif self.last_qpos is not None:
            init_qpos = self.last_qpos
        else:
            init_qpos = self.robot.joint_limits.mean(axis=1)

        return np.clip(
            init_qpos,
            self.robot.joint_limits[:, 0],
            self.robot.joint_limits[:, 1]
        )

    def _get_reg_qpos(self, last_qpos: Optional[np.ndarray]) -> Optional[np.ndarray]:
        """Get regularization qpos for norm_delta term.

        Args:
            last_qpos: Optional last qpos from caller

        Returns:
            Regularization qpos or None
        """
        if last_qpos is not None:
            return np.asarray(last_qpos, dtype=np.float64)
        elif self.last_qpos is not None:
            return self.last_qpos
        return None

    def _run_optimization(self, objective_fn, init_qpos: np.ndarray) -> np.ndarray:
        """Run NLopt optimization and update last_qpos.

        Args:
            objective_fn: NLopt objective function
            init_qpos: Initial qpos

        Returns:
            Optimized qpos
        """
        self.opt.set_min_objective(objective_fn)
        try:
            qpos = self.opt.optimize(init_qpos.tolist())
            qpos = np.array(qpos, dtype=np.float32)
        except RuntimeError as e:
            print(f"[{self.__class__.__name__}] Optimization failed: {e}")
            qpos = np.array(init_qpos, dtype=np.float32)
        self.last_qpos = qpos.astype(np.float64)
        return qpos

    def _compute_tip_vectors(self, keypoints: np.ndarray, scaling: float = 1.0) -> np.ndarray:
        """Compute wrist->tip vectors.

        Args:
            keypoints: (21, 3) MediaPipe keypoints in meters
            scaling: Global scaling factor

        Returns:
            vectors: (5, 3) tip vectors in cm
        """
        wrist = keypoints[self.MP_ORIGIN_IDX]
        vectors = np.array([
            keypoints[idx] - wrist for idx in self.MP_TIP_INDICES
        ]) * scaling * M_TO_CM
        return vectors.astype(np.float64)

    def _compute_tip_dirs(self, keypoints: np.ndarray) -> np.ndarray:
        """Compute DIP->tip direction vectors (normalized).

        Args:
            keypoints: (21, 3) MediaPipe keypoints

        Returns:
            tip_dirs: (5, 3) normalized direction vectors
        """
        tip_dirs = []
        for dip_idx, tip_idx in zip(self.MP_DIP_INDICES, self.MP_TIP_INDICES):
            dir_vec = keypoints[tip_idx] - keypoints[dip_idx]
            norm = np.linalg.norm(dir_vec)
            tip_dirs.append(dir_vec / (norm + 1e-8))
        return np.array(tip_dirs, dtype=np.float64)

    def _compute_full_hand_vectors(self, keypoints: np.ndarray, scaling: np.ndarray) -> np.ndarray:
        """Compute full hand vectors (wrist->PIP, wrist->DIP, wrist->TIP).

        Args:
            keypoints: (21, 3) MediaPipe keypoints in meters
            scaling: (5, 3) scaling factors for each finger and segment

        Returns:
            vectors: (15, 3) vectors in cm [PIP*5, DIP*5, TIP*5]
        """
        wrist = keypoints[self.MP_ORIGIN_IDX]

        # wrist -> PIP (5 vectors)
        pip_vectors = np.array([
            keypoints[idx] - wrist for idx in self.MP_PIP_INDICES
        ]) * scaling[:, 0:1]

        # wrist -> DIP (5 vectors)
        dip_vectors = np.array([
            keypoints[idx] - wrist for idx in self.MP_DIP_INDICES
        ]) * scaling[:, 1:2]

        # wrist -> TIP (5 vectors)
        tip_vectors = np.array([
            keypoints[idx] - wrist for idx in self.MP_TIP_INDICES
        ]) * scaling[:, 2:3]

        # Concatenate and convert to cm
        vectors = np.vstack([pip_vectors, dip_vectors, tip_vectors]) * M_TO_CM
        return vectors.astype(np.float64)
