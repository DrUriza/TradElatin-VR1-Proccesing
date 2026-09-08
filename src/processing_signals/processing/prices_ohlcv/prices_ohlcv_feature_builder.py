from __future__ import annotations

from copy   import deepcopy
import math
from typing import Any, Mapping


TIMEFRAME_ORDER = ("1m", "5m", "15m", "4h")
MARKET_ORDER    = ("spot", "futures")


def build_market_series_features(market: Mapping[str, Any]) -> dict[str, Any]:
    """Copy already-computed numeric market series without recalculation."""
    timeframes = market.get("timeframes", {})
    return {
        "timeframes": {
            timeframe: {
                "records": deepcopy(timeframes.get(timeframe, {}).get("records", [])),
                "unavailable_records": deepcopy(
                    timeframes.get(timeframe, {}).get("unavailable_records", [])
                ),
            }
            for timeframe in TIMEFRAME_ORDER
        }
    }


def build_main_ohlcv_features(markets: Mapping[str, Any]) -> dict[str, Any]:
    return {
        market: build_market_series_features(markets.get(market, {}))
        for market in MARKET_ORDER
    }


def build_market_selector_features(markets: Mapping[str, Any]) -> dict[str, Any]:
    # The current Prices HMI defaults to the real Spot market. Futures remains
    # in Processing for basis and confirmation.
    available = ["spot"] if any(
        markets.get("spot", {}).get("timeframes", {}).get(timeframe, {}).get("records")
        for timeframe in TIMEFRAME_ORDER
    ) else []
    return {
        "default_market": "spot",
        "selected_market": "spot",
        "available_markets": available,
        "timeframes": list(TIMEFRAME_ORDER),
        "default_timeframe": "15m",
        "selected_timeframe": "15m",
        "canonical_source_market": "spot",
    }


def build_spot_futures_comparison_features(
    comparison: Mapping[str, Any],
) -> dict[str, Any]:
    source = comparison.get("by_timeframe", comparison)
    return {
        "by_timeframe": {
            timeframe: {
                "series": deepcopy(source.get(timeframe, {}).get("series", [])),
                "current": deepcopy(source.get(timeframe, {}).get("current", {})),
            }
            for timeframe in TIMEFRAME_ORDER
        }
    }


def build_indicator_placeholders() -> dict[str, dict[str, Any]]:
    return {market: {} for market in MARKET_ORDER}


def build_timeframe_indicator_features(indicators: Mapping[str, Any]) -> dict[str, Any]:
    """Package previously calculated indicators without recalculating them."""
    return deepcopy(dict(indicators))


def build_market_indicator_features(indicators: Mapping[str, Any]) -> dict[str, Any]:
    return {
        timeframe: build_timeframe_indicator_features(indicators.get(timeframe, {}))
        for timeframe in TIMEFRAME_ORDER
    }


def build_indicator_features(indicators: Mapping[str, Any]) -> dict[str, Any]:
    return {
        market: build_market_indicator_features(indicators.get(market, {}))
        for market in MARKET_ORDER
    }


def build_technical_cross_features(crosses: Mapping[str, Any]) -> dict[str, Any]:
    return {
        market: {
            timeframe: deepcopy(crosses.get(market, {}).get(timeframe, []))
            for timeframe in TIMEFRAME_ORDER
        }
        for market in MARKET_ORDER
    }


def build_candlestick_pattern_features(patterns: Mapping[str, Any]) -> dict[str, Any]:
    return {market: {timeframe: deepcopy(patterns.get(market, {}).get(timeframe, [])) for timeframe in TIMEFRAME_ORDER} for market in MARKET_ORDER}


def build_statistical_features(results: Mapping[str, Any]) -> dict[str, Any]:
    return {market: {timeframe: deepcopy(results.get(market, {}).get(timeframe, {}).get("descriptive", {})) for timeframe in TIMEFRAME_ORDER} for market in MARKET_ORDER}


def build_risk_features(results: Mapping[str, Any]) -> dict[str, Any]:
    return {market: {timeframe: deepcopy(results.get(market, {}).get(timeframe, {}).get("risk", {})) for timeframe in TIMEFRAME_ORDER} for market in MARKET_ORDER}


def build_performance_features(results: Mapping[str, Any]) -> dict[str, Any]:
    return {market: {timeframe: deepcopy(results.get(market, {}).get(timeframe, {}).get("performance", {})) for timeframe in TIMEFRAME_ORDER} for market in MARKET_ORDER}


def build_statistical_performance_features(results: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "default_market": "spot", "default_metrics_timeframe": "15m",
        "markets": {market: {timeframe: deepcopy(results.get(market, {}).get(timeframe, {})) for timeframe in TIMEFRAME_ORDER} for market in MARKET_ORDER},
        "statistics": build_statistical_features(results), "risk": build_risk_features(results),
        "performance": build_performance_features(results),
    }


def build_bias_component_features(components: Mapping[str, Any]) -> dict[str, Any]:
    return deepcopy(dict(components))



def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def _percent_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous in (None, 0.0):
        return None
    return (current / previous - 1.0) * 100.0


def _nearest_change(records: list[Mapping[str, Any]], seconds: int) -> float | None:
    valid = [row for row in records if isinstance(row, Mapping) and type(row.get("timestamp")) is int and _finite(row.get("close")) is not None]
    if not valid:
        return None
    current = valid[-1]
    target = int(current["timestamp"]) - seconds
    candidates = [row for row in valid if int(row["timestamp"]) <= target]
    previous = candidates[-1] if candidates else None
    return _percent_change(_finite(current.get("close")), _finite(previous.get("close")) if previous else None)


def _timeframe_dynamics(records: list[Mapping[str, Any]]) -> dict[str, Any]:
    valid = [row for row in records if isinstance(row, Mapping)]
    if not valid:
        return {"status": "unavailable", "reason": "records_unavailable", "current": {}, "returns_histogram": {"status": "unavailable", "reason": "returns_unavailable", "counts": [], "bin_edges": []}}
    row = valid[-1]
    open_ = _finite(row.get("open")); high = _finite(row.get("high")); low = _finite(row.get("low")); close = _finite(row.get("close")); volume = _finite(row.get("volume_usd"))
    span = None if high is None or low is None else high - low
    body = None if open_ is None or close is None else abs(close - open_)
    upper_wick = None if high is None or open_ is None or close is None else max(0.0, high - max(open_, close))
    lower_wick = None if low is None or open_ is None or close is None else max(0.0, min(open_, close) - low)

    prior_volumes = [_finite(item.get("volume_usd")) for item in valid[-21:-1]]
    prior_volumes = [value for value in prior_volumes if value is not None]
    mean_volume = sum(prior_volumes) / len(prior_volumes) if len(prior_volumes) >= 5 else None
    variance = (sum((value - mean_volume) ** 2 for value in prior_volumes) / len(prior_volumes)) if mean_volume is not None else None
    std_volume = math.sqrt(variance) if variance is not None and variance > 0 else None
    relative_volume = volume / mean_volume if volume is not None and mean_volume not in (None, 0.0) else None
    volume_zscore = (volume - mean_volume) / std_volume if volume is not None and mean_volume is not None and std_volume not in (None, 0.0) else None

    returns: list[float] = []
    closes = [_finite(item.get("close")) for item in valid[-201:]]
    for previous, current in zip(closes, closes[1:]):
        change = _percent_change(current, previous)
        if change is not None:
            returns.append(change)
    if len(returns) >= 2:
        low_r, high_r = min(returns), max(returns)
        if math.isclose(low_r, high_r):
            edges = [low_r - 0.5, high_r + 0.5]
            counts = [len(returns)]
        else:
            bins = min(20, max(5, int(math.sqrt(len(returns)))))
            width = (high_r - low_r) / bins
            edges = [low_r + index * width for index in range(bins + 1)]
            counts = [0] * bins
            for value in returns:
                index = min(bins - 1, int((value - low_r) / width))
                counts[index] += 1
        histogram = {"status": "available", "reason": None, "unit": "percent", "sample_size": len(returns), "bin_edges": edges, "counts": counts}
    else:
        histogram = {"status": "unavailable", "reason": "insufficient_returns", "unit": "percent", "sample_size": len(returns), "bin_edges": [], "counts": []}

    return {
        "status": "available", "reason": None,
        "current": {
            "timestamp": row.get("timestamp"),
            "bar_return_percent": _percent_change(close, _finite(valid[-2].get("close")) if len(valid) >= 2 else None),
            "range_percent": (span / close * 100.0) if span is not None and close not in (None, 0.0) else None,
            "body_percent_of_range": (body / span * 100.0) if body is not None and span not in (None, 0.0) else None,
            "upper_wick_percent_of_range": (upper_wick / span * 100.0) if upper_wick is not None and span not in (None, 0.0) else None,
            "lower_wick_percent_of_range": (lower_wick / span * 100.0) if lower_wick is not None and span not in (None, 0.0) else None,
            "relative_volume_20": relative_volume,
            "volume_zscore_20": volume_zscore,
            "volume_baseline_mean_20": mean_volume,
            "volume_baseline_records": len(prior_volumes),
        },
        "returns_histogram": histogram,
    }


def _classic_pivots(daily_records: list[Mapping[str, Any]]) -> dict[str, Any]:
    closed = [row for row in daily_records if isinstance(row, Mapping) and row.get("is_closed", True)]
    if len(closed) < 2:
        return {"status": "unavailable", "reason": "previous_daily_candle_unavailable"}
    previous = closed[-2]
    high = _finite(previous.get("high")); low = _finite(previous.get("low")); close = _finite(previous.get("close"))
    if None in {high, low, close}:
        return {"status": "unavailable", "reason": "previous_daily_candle_invalid"}
    pivot = (high + low + close) / 3.0
    return {
        "status": "available", "reason": None, "method": "classic_previous_closed_daily_candle",
        "source_timestamp": previous.get("timestamp"), "pivot": pivot,
        "r1": 2.0 * pivot - low, "s1": 2.0 * pivot - high,
        "r2": pivot + (high - low), "s2": pivot - (high - low),
        "r3": high + 2.0 * (pivot - low), "s3": low - 2.0 * (high - pivot),
    }




def _aggregate_daily(records: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[int, list[Mapping[str, Any]]] = {}
    for row in records:
        if not isinstance(row, Mapping) or type(row.get("timestamp")) is not int:
            continue
        day = int(row["timestamp"]) - int(row["timestamp"]) % 86_400
        buckets.setdefault(day, []).append(row)
    output: list[dict[str, Any]] = []
    for day, bucket in sorted(buckets.items()):
        ordered = sorted(bucket, key=lambda item: int(item["timestamp"]))
        highs = [_finite(item.get("high")) for item in ordered]
        lows = [_finite(item.get("low")) for item in ordered]
        if not ordered or any(value is None for value in highs + lows):
            continue
        output.append({"timestamp": day, "open": _finite(ordered[0].get("open")),
                       "high": max(highs), "low": min(lows), "close": _finite(ordered[-1].get("close")),
                       "is_closed": all(bool(item.get("is_closed", True)) for item in ordered)})
    return output

def build_market_dynamics_features(markets: Mapping[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for market in MARKET_ORDER:
        timeframes = markets.get(market, {}).get("timeframes", {})
        one_minute = list(timeframes.get("1m", {}).get("records", []))
        output[market] = {
            "change_windows_percent": {
                "15m": _nearest_change(one_minute, 900),
                "4h": _nearest_change(one_minute, 14_400),
                "24h": _nearest_change(one_minute, 86_400),
            },
            "pivot_points": _classic_pivots(_aggregate_daily(list(timeframes.get("15m", {}).get("records", [])))),
            "timeframes": {
                timeframe: _timeframe_dynamics(list(timeframes.get(timeframe, {}).get("records", [])))
                for timeframe in TIMEFRAME_ORDER
            },
        }
    return output

def build_prices_features(
    *,
    markets: Mapping[str, Any],
    comparison: Mapping[str, Any],
    indicators: Mapping[str, Any] | None = None,
    technical_crosses: Mapping[str, Any] | None = None,
    candlestick_patterns: Mapping[str, Any] | None = None,
    statistical_performance: Mapping[str, Any] | None = None,
    bias_components: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Organize numeric Processing results for the future classifier."""
    return {
        "market_selector": build_market_selector_features(markets),
        "main_ohlcv": build_main_ohlcv_features(markets),
        "spot_futures_comparison": build_spot_futures_comparison_features(comparison),
        "market_dynamics": build_market_dynamics_features(markets),
        "indicators": (
            build_indicator_features(indicators)
            if indicators is not None
            else build_indicator_placeholders()
        ),
        "technical_crosses": build_technical_cross_features(technical_crosses or {}),
        "candlestick_patterns": build_candlestick_pattern_features(candlestick_patterns or {}),
        "statistical_performance": build_statistical_performance_features(statistical_performance or {}),
        "bias_components": build_bias_component_features(bias_components or {}),
    }


class PricesOhlcvFeatureBuilder:
    """OO facade that only packages existing numeric results."""

    def build(
        self,
        *,
        markets: Mapping[str, Any],
        comparison: Mapping[str, Any],
        indicators: Mapping[str, Any] | None = None,
        technical_crosses: Mapping[str, Any] | None = None,
        candlestick_patterns: Mapping[str, Any] | None = None,
        statistical_performance: Mapping[str, Any] | None = None,
        bias_components: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return build_prices_features(
            markets=markets, comparison=comparison, indicators=indicators,
            technical_crosses=technical_crosses,
            candlestick_patterns=candlestick_patterns,
            statistical_performance=statistical_performance,
            bias_components=bias_components,
        )
