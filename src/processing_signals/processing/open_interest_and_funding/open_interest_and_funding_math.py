"""Family bridge to shared mathematical algorithms.

This module is the only place in this family allowed to import from ``processing.math``.
"""

from __future__ import annotations

from processing_signals.processing.math.indicators.trend.moving_averages import ema, sma, wma
from processing_signals.processing.math.technical_cross_signals import detect_numeric_crosses
from processing_signals.processing.math.native_analysis import (
    rolling_zscore, rolling_percentile, pct_change, difference, latest, interpolated_cross, rolling_wasserstein
)


def open_interest_wasserstein_frame(frame):
    """Return the OI Wasserstein series using the shared rolling implementation.

    The one-position shift preserves the historical contract alignment: the
    value stamped at row i compares the two completed windows ending at i-1.
    """
    import pandas as pd

    close = pd.to_numeric(frame["close"], errors="coerce").tolist()
    returns = pct_change(close, periods=1, scale=1.0)
    raw = rolling_wasserstein(returns, recent_window=20, reference_window=40)
    shifted = [None] + raw[:-1] if raw else []
    if len(shifted) > 60:
        shifted[59] = None
    return {"distance": pd.Series(shifted, index=frame.index, dtype="float64")}
