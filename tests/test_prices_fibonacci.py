from __future__ import annotations

import math

import pandas as pd

from processing_signals.classification.prices_ohlcv.prices_ohlcv_contract_builder import _ohlcv_overlays
from processing_signals.processing.math.indicators.market_structure.fibonacci_levels import fibonacci_levels
from processing_signals.processing.prices_ohlcv.prices_ohlcv_processor import calculate_prices_indicator_package


RATIOS = ("0.0", "0.236", "0.382", "0.5", "0.618", "0.786", "1.0")


def _assert_close(actual: float, expected: float) -> None:
    assert math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-9)


def _records(highs: list[float], lows: list[float]) -> list[dict[str, float | int]]:
    rows: list[dict[str, float | int]] = []
    for index, (high, low) in enumerate(zip(highs, lows)):
        close = (high + low) / 2.0
        rows.append({
            "timestamp": 1_700_000_000 + index * 60,
            "open": close,
            "high": high,
            "low": low,
            "close": close,
            "volume_usd": 1_000_000.0 + index,
        })
    return rows


def test_bullish_swing_is_anchored_low_to_high() -> None:
    highs = pd.Series([102.0, 103.0, 104.0, 110.0], dtype="float64")
    lows = pd.Series([100.0, 101.0, 102.0, 108.0], dtype="float64")

    current = fibonacci_levels(highs, lows, window=4).iloc[-1]

    _assert_close(current["fib_swing_low"], 100.0)
    _assert_close(current["fib_swing_high"], 110.0)
    _assert_close(current["fib_000_100"], 100.0)
    _assert_close(current["fib_236_100"], 102.36)
    _assert_close(current["fib_382_100"], 103.82)
    _assert_close(current["fib_500_100"], 105.0)
    _assert_close(current["fib_618_100"], 106.18)
    _assert_close(current["fib_786_100"], 107.86)
    _assert_close(current["fib_1000_100"], 110.0)


def test_bearish_swing_is_anchored_high_to_low() -> None:
    highs = pd.Series([110.0, 108.0, 106.0, 104.0], dtype="float64")
    lows = pd.Series([108.0, 105.0, 102.0, 100.0], dtype="float64")

    current = fibonacci_levels(highs, lows, window=4).iloc[-1]

    _assert_close(current["fib_swing_high"], 110.0)
    _assert_close(current["fib_swing_low"], 100.0)
    _assert_close(current["fib_000_100"], 110.0)
    _assert_close(current["fib_236_100"], 107.64)
    _assert_close(current["fib_382_100"], 106.18)
    _assert_close(current["fib_500_100"], 105.0)
    _assert_close(current["fib_618_100"], 103.82)
    _assert_close(current["fib_786_100"], 102.14)
    _assert_close(current["fib_1000_100"], 100.0)


def test_same_candle_extremes_do_not_invent_direction() -> None:
    highs = pd.Series([101.0, 102.0, 120.0], dtype="float64")
    lows = pd.Series([99.0, 98.0, 80.0], dtype="float64")

    current = fibonacci_levels(highs, lows, window=3).iloc[-1]

    assert current.isna().all()


def test_repeated_extreme_uses_most_recent_occurrence_for_direction() -> None:
    # High 110 repeats after the low 90. Using the latest high occurrence makes
    # the chronological swing bullish (90 -> 110), not bearish from stale high.
    highs = pd.Series([110.0, 100.0, 105.0, 110.0], dtype="float64")
    lows = pd.Series([108.0, 90.0, 95.0, 104.0], dtype="float64")

    current = fibonacci_levels(highs, lows, window=4).iloc[-1]

    _assert_close(current["fib_000_100"], 90.0)
    _assert_close(current["fib_1000_100"], 110.0)


def test_processor_and_contract_publish_directional_anchor_metadata() -> None:
    # 100 records are required by the production Fibonacci lookback. The low
    # happens first and the high last, establishing an unambiguous bullish swing.
    highs = [101.0 + 0.05 * i for i in range(100)]
    lows = [99.0 + 0.05 * i for i in range(100)]
    lows[10] = 90.0
    highs[90] = 120.0

    package = calculate_prices_indicator_package(
        records=_records(highs, lows),
        market_type="spot",
        timeframe="1m",
    )
    fibonacci = package["fibonacci_levels"]
    current = fibonacci["current"]

    assert current["direction"] == "bullish"
    _assert_close(current["origin"], current["swing_low"])
    _assert_close(current["endpoint"], current["swing_high"])
    assert list(current["levels"]) == list(RATIOS)
    assert list(current["levels"].values()) == sorted(current["levels"].values())
    assert fibonacci["parameters"]["anchor_convention"] == "0%=swing_origin;100%=swing_endpoint"

    overlays = _ohlcv_overlays(package, limit=100)
    contract_fibonacci = overlays["fibonacci_levels"]
    assert contract_fibonacci["current"]["direction"] == "bullish"
    assert contract_fibonacci["current"]["origin"] == current["origin"]
    assert contract_fibonacci["current"]["endpoint"] == current["endpoint"]
    assert contract_fibonacci["current"]["levels"] == current["levels"]
    assert contract_fibonacci["parameters"]["anchor_convention"] == "0%=swing_origin;100%=swing_endpoint"
