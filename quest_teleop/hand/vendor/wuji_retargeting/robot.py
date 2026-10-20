from typing import List

import numpy as np
import numpy.typing as npt
import pinocchio as pin


class RobotWrapper:
    """Pinocchio robot wrapper for forward kinematics."""

    def __init__(self, urdf_path: str, hand_side: str = None):
        # Create robot model and data
        self.model: pin.Model = pin.buildModelFromUrdf(urdf_path)
        self.data: pin.Data = self.model.createData()

        if self.model.nv != self.model.nq:
            raise NotImplementedError("Cannot handle robot with special joint.")

        # Store hand side for frame name resolution
        self.hand_side = hand_side.lower() if hand_side else None

    @property
    def joint_limits(self):
        """Return joint limits as (lower, upper) pairs."""
        lower = self.model.lowerPositionLimit
        upper = self.model.upperPositionLimit
        return np.stack([lower, upper], axis=1)

    def get_link_index(self, name: str) -> int:
        """Get frame index by name, trying both unprefixed and prefixed variants.

        Args:
            name: Frame name without prefix (e.g., "palm_link", "finger1_tip_link")
        """
        for candidate in [name, f"{self.hand_side}_{name}"]:
            idx = self.model.getFrameId(candidate, pin.BODY)
            if idx < self.model.nframes:
                return idx
        raise RuntimeError(
            f"Frame '{name}' not found. "
            f"Available: {[self.model.frames[i].name for i in range(self.model.nframes)]}"
        )

    def get_actuated_qpos_index(self, link_name: str) -> int:
        """Return the qpos index of the joint that actuates ``link_name``.

        A link's parent joint is the joint directly above it in the kinematic
        chain (the joint whose URDF ``<child>`` is this link). For the wuji
        finger chain ``link1->link2->link3->link4``, ``finger{i}_link3``'s parent
        joint is the PIP joint and ``finger{i}_link4``'s is the DIP joint.

        Resolving the qpos slot this way (instead of hardcoding indices) keeps the
        mapping correct when a custom URDF declares joints in a different order or
        with a non-uniform DOF layout. Raises if the link is unknown or its parent
        joint is not a single-DOF joint.
        """
        frame_id = self.get_link_index(link_name)
        joint_id = self.model.frames[frame_id].parentJoint
        joint = self.model.joints[joint_id]
        if joint.nq != 1:
            raise RuntimeError(
                f"Parent joint of '{link_name}' ('{self.model.names[joint_id]}') has "
                f"nq={joint.nq}, expected a single-DOF (revolute/prismatic) joint."
            )
        return int(joint.idx_q)

    def compute_forward_kinematics(self, qpos: npt.NDArray):
        """Compute forward kinematics for all links."""
        pin.forwardKinematics(self.model, self.data, qpos)

    def get_link_pose(self, link_id: int) -> npt.NDArray:
        """Get link pose as 4x4 homogeneous matrix."""
        pose: pin.SE3 = pin.updateFramePlacement(self.model, self.data, link_id)
        return pose.homogeneous

    def compute_all_jacobians_batch(self, qpos: npt.NDArray, link_indices: List[int]) -> npt.NDArray:
        """Batch compute position Jacobians for multiple links.

        Uses one joint-Jacobian computation for all requested links.

        Args:
            qpos: Joint positions
            link_indices: List of frame indices

        Returns:
            jacobians: (num_links, 3, nq) position Jacobians in world frame
        """
        qpos = np.asarray(qpos, dtype=np.float64)

        # Compute all joint Jacobians at once (updates data.J internally)
        pin.computeJointJacobians(self.model, self.data, qpos)
        # Update all frame placements
        pin.updateFramePlacements(self.model, self.data)

        jacobians = []
        for idx in link_indices:
            # getFrameJacobian reuses computed joint Jacobians (faster than computeFrameJacobian)
            J_local = pin.getFrameJacobian(self.model, self.data, idx, pin.LOCAL)
            # Get rotation to transform to world frame
            R = self.data.oMf[idx].rotation
            # Only take position part (3, nq) and transform to world frame
            J_world_pos = R @ J_local[:3, :]
            jacobians.append(J_world_pos)

        return np.stack(jacobians, axis=0)

