from __future__ import annotations

BUILD_SUCCESS_REWARD_WEIGHT = 0.05
OVERALL_SCORE_REWARD_WEIGHT = 0.95


def clamp_unit_interval(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
