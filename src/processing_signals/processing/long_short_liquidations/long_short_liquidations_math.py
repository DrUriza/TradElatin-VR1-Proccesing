"""Family bridge to shared mathematical algorithms.

This module is the only place in this family allowed to import from ``processing.math``.
"""

from __future__ import annotations

from processing_signals.processing.math.native_analysis import rolling_zscore, rolling_percentile, difference, rolling_wasserstein, pct_change, latest
