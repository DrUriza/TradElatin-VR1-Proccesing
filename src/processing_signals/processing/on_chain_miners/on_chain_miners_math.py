"""Family bridge to shared mathematical algorithms.

This module is the only place in this family allowed to import from ``processing.math``.
"""

from __future__ import annotations

from processing_signals.processing.math.native_analysis import rolling_zscore, rolling_mean, pct_change, difference, rolling_wasserstein, latest, score_to_probability
