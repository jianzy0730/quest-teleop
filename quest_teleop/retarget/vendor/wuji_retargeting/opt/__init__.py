"""Optimizers for hand retargeting.

AdaptiveOptimizerAnalytical - Recommended optimizer using Huber loss + analytical gradients + NLopt SLSQP.
Uses adaptive blending between TipDirVec and FullHandVec based on pinch distance.

All parameters are read from YAML configuration files.
"""

from .base import (
    BaseOptimizer,
    M_TO_CM,
)
from .adaptive_analytical import AdaptiveOptimizerAnalytical


__all__ = [
    "BaseOptimizer",
    "AdaptiveOptimizerAnalytical",
    "M_TO_CM",
]
