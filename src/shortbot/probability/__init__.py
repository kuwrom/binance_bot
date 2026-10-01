"""Probability layer: no-edge baseline odds (closed form + fat-tail Monte Carlo), triple-barrier labels,
calibration and expectancy. See docs/PROBABILITY.md."""

from .engine import ProbabilityEngine, sigma_hourly, vol_scaled_bracket

__all__ = ["ProbabilityEngine", "sigma_hourly", "vol_scaled_bracket"]
