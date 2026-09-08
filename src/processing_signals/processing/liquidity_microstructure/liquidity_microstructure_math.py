"""Family bridge to shared mathematical algorithms.

This module is the only place in this family allowed to import from ``processing.math``.
"""

from __future__ import annotations

from processing_signals.processing.math.microstructure.order_book import depth_metrics, derive_cumulative_band, process_order_book_levels
from processing_signals.processing.math.microstructure.series_metrics import absolute_change, clean_zero, observation_at_or_before, rolling_mean, rolling_std, rolling_z_score, safe_percent_change
from processing_signals.processing.math.microstructure.trade_flow import aggregate_trade_window, enrich_trade_event
from processing_signals.processing.math.native_analysis import rolling_zscore, rolling_wasserstein, difference, latest
