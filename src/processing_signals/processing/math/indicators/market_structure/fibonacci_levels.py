from __future__ import annotations

import numpy as np
import pandas as pd


FIBONACCI_RATIOS = (0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0)


def fibonacci_levels(high: pd.Series, low: pd.Series, window: int = 100) -> pd.DataFrame:
    """Build rolling Fibonacci levels from the chronological swing direction.

    The rolling-window maximum high and minimum low define the swing pair. The
    most recent occurrence of each extreme is used when an extreme repeats.

    Anchor convention is deliberately directional and stable:

    * bullish swing: low -> high, therefore 0% = low and 100% = high;
    * bearish swing: high -> low, therefore 0% = high and 100% = low.

    Intermediate ratios are linear points from the swing origin (0%) to the
    swing endpoint (100%). If both extrema occur on the same candle, direction
    is ambiguous and that row is left unavailable instead of inventing a swing.
    """
    if type(window) is not int or window <= 1:
        raise ValueError("window must be an integer greater than one")

    highs = pd.to_numeric(high, errors="coerce")
    lows = pd.to_numeric(low, errors="coerce")
    if len(highs) != len(lows):
        raise ValueError("high and low must have equal length")

    swing_high = pd.Series(np.nan, index=highs.index, dtype=float)
    swing_low = pd.Series(np.nan, index=lows.index, dtype=float)
    origin = pd.Series(np.nan, index=highs.index, dtype=float)
    endpoint = pd.Series(np.nan, index=highs.index, dtype=float)

    for position in range(window - 1, len(highs)):
        start = position - window + 1
        window_high = highs.iloc[start:position + 1]
        window_low = lows.iloc[start:position + 1]
        if window_high.isna().any() or window_low.isna().any():
            continue

        high_values = window_high.to_numpy(dtype=float)
        low_values = window_low.to_numpy(dtype=float)
        high_value = float(np.max(high_values))
        low_value = float(np.min(low_values))
        if not np.isfinite(high_value) or not np.isfinite(low_value) or high_value <= low_value:
            continue

        # Use the most recent occurrence when an extreme repeats. This prevents
        # a stale duplicate from changing the inferred chronological direction.
        high_position = int(np.flatnonzero(high_values == high_value)[-1])
        low_position = int(np.flatnonzero(low_values == low_value)[-1])

        # A single outside candle may contain both extrema. Chronology inside a
        # candle is unknown, so do not fabricate bullish/bearish direction.
        if high_position == low_position:
            continue

        swing_high.iloc[position] = high_value
        swing_low.iloc[position] = low_value

        if low_position < high_position:  # bullish: low -> high
            origin.iloc[position] = low_value
            endpoint.iloc[position] = high_value
        else:  # bearish: high -> low
            origin.iloc[position] = high_value
            endpoint.iloc[position] = low_value

    distance = endpoint - origin
    return pd.DataFrame({
        "fib_swing_high": swing_high,
        "fib_swing_low": swing_low,
        "fib_000_100": origin,
        "fib_236_100": origin + distance * 0.236,
        "fib_382_100": origin + distance * 0.382,
        "fib_500_100": origin + distance * 0.500,
        "fib_618_100": origin + distance * 0.618,
        "fib_786_100": origin + distance * 0.786,
        "fib_1000_100": endpoint,
    })
