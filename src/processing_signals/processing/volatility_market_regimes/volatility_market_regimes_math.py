"""Family bridge to shared mathematical algorithms.

This module is the only place in this family allowed to import from ``processing.math``.
"""

from __future__ import annotations

from processing_signals.processing.math.series import rolling_mean_std, rolling_percentile_ranks, rolling_z_scores
from processing_signals.processing.math.technical_cross_signals import detect_cross_pairs

from processing_signals.processing.math.native_analysis import (
    rolling_mean, rolling_zscore, rolling_empirical_percentile, rolling_normalized_wasserstein
)


def native_rolling_mean(values, window: int):
    return [0.0 if value is None else float(value) for value in rolling_mean(values, window, min_periods=1)]


def native_rolling_zscore(values, window: int = 30):
    return [0.0 if value is None else float(value) for value in rolling_zscore(values, window, min_periods=1)]


def native_rolling_percentile(values, window: int = 30):
    return [0.0 if value is None else float(value) for value in rolling_empirical_percentile(values, window, min_periods=1, scale=100.0)]


def native_normalized_wasserstein(values, window: int = 7):
    """Normalized 1-D Wasserstein distance between consecutive windows.

    Volatility exposes 7D/30D display ranges and bootstrap currently provides
    roughly 43 daily observations.  Using adjacent 7-day distributions yields
    30 genuinely usable Wasserstein observations without fabricating warm-up
    zeros.  ``None`` is preserved during warm-up so the HMI never mistakes
    missing history for a zero regime shift.
    """
    return [None if value is None else float(value)
            for value in rolling_normalized_wasserstein(values, window, window)]
