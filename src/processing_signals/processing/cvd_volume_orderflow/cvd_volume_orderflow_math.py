"""Family bridge to shared mathematical algorithms.

This module is the only place in this family allowed to import from ``processing.math``.
"""

from __future__ import annotations

from processing_signals.processing.math.technical_cross_signals import detect_cross_pairs
from processing_signals.processing.math.native_analysis import difference, rolling_zscore, rolling_wasserstein, interpolated_cross, latest, finite
from processing_signals.processing.math.indicators.trend.moving_averages import ema, sma, wma
