"""MediaPipe format conversion utilities for hand pose data."""

import numpy as np


# Coordinate transformation matrices for MANO hand model
OPERATOR2MANO_RIGHT = np.array([
    [0, 0, -1],
    [-1, 0, 0],
    [0, 1, 0],
])

OPERATOR2MANO_LEFT = np.array([
    [0, 0, -1],
    [1, 0, 0],
    [0, -1, 0],
])


def estimate_frame_from_hand_points(keypoint_3d_array: np.ndarray) -> np.ndarray:
    """
    Compute the 3D coordinate frame (orientation only) from detected 3d key points.
    
    Args:
        keypoint_3d_array: keypoint3 detected from hand detector. Shape: (21, 3)
        
    Returns:
        frame: the coordinate frame of wrist in MANO convention
    """
    assert keypoint_3d_array.shape == (21, 3)
    points = keypoint_3d_array[[0, 5, 9], :]

    x_vector = points[0] - points[2]

    points = points - np.mean(points, axis=0, keepdims=True)
    u, s, v = np.linalg.svd(points)

    normal = v[2, :]

    x = x_vector - np.sum(x_vector * normal) * normal
    x = x / np.linalg.norm(x)
    z = np.cross(x, normal)

    if np.sum(z * (points[1] - points[2])) < 0:
        normal *= -1
        z *= -1
    frame = np.stack([x, normal, z], axis=1)
    return frame


def apply_mediapipe_transformations(keypoint_3d_array: np.ndarray, hand_type: str = "right") -> np.ndarray:
    """
    Apply the same coordinate transformations as MediaPipe data processing.
    
    Args:
        keypoint_3d_array: numpy array of shape (21, 3) - hand landmarks
        hand_type: "right" or "left" - determines coordinate system
        
    Returns:
        transformed_joint_pos: numpy array of shape (21, 3) - transformed landmarks
    """
    hand_type = hand_type.lower()
    
    keypoint_3d_array = keypoint_3d_array - keypoint_3d_array[0:1, :]
    
    mediapipe_wrist_rot = estimate_frame_from_hand_points(keypoint_3d_array)
    
    operator2mano = OPERATOR2MANO_RIGHT if hand_type == "right" else OPERATOR2MANO_LEFT
    joint_pos = keypoint_3d_array @ mediapipe_wrist_rot @ operator2mano
    
    return joint_pos


__all__ = [
    'OPERATOR2MANO_RIGHT',
    'OPERATOR2MANO_LEFT',
    'estimate_frame_from_hand_points',
    'apply_mediapipe_transformations',
]

