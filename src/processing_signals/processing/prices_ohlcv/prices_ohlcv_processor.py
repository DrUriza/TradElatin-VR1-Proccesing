from __future__ import annotations

from copy import deepcopy
import math
import time
from typing import Any, Mapping, Sequence

import pandas as pd

from .prices_ohlcv_math import fibonacci_levels
from .prices_ohlcv_math import cci
from .prices_ohlcv_math import rsi
from .prices_ohlcv_math import stochastic
from .prices_ohlcv_math import tsi
from .prices_ohlcv_math import williams_r
from .prices_ohlcv_math import adx
from .prices_ohlcv_math import macd
from .prices_ohlcv_math import ema, sma, wma
from .prices_ohlcv_math import atr
from .prices_ohlcv_math import bollinger_bands
from .prices_ohlcv_math import mfi
from .prices_ohlcv_math import detect_cross_pairs
from .prices_ohlcv_math import (
    interpolated_cross,
    rolling_wasserstein,
    support_resistance_levels,
    finite as native_finite,
)
from .prices_ohlcv_math import detect_candlestick_patterns
from .prices_ohlcv_math import (
    calculate_kurtosis,
    calculate_mean,
    calculate_skewness,
    calculate_standard_deviation,
    calculate_z_score,
)
from .prices_ohlcv_math import (
    calculate_historical_cvar,
    calculate_historical_var,
)
from .prices_ohlcv_math import (
    calculate_calmar_ratio,
    calculate_equity_curve,
    calculate_max_consecutive_losses,
    calculate_max_consecutive_wins,
    calculate_max_drawdown,
    calculate_omega_ratio,
    calculate_profit_factor,
    calculate_recovery_factor,
    calculate_sharpe_ratio,
    calculate_simple_returns,
    calculate_sortino_ratio,
    calculate_win_rate,
)

from .prices_ohlcv_feature_builder import PricesOhlcvFeatureBuilder


TIMEFRAME_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "4h": 14400,
}

TIMEFRAME_ORDER = tuple(TIMEFRAME_SECONDS)
CALCULATION_MARKETS = ("spot", "futures")

RESAMPLING_RULES = {
    "5m": {"source_timeframe": "1m", "expected_source_records": 5},
    "4h": {"source_timeframe": "15m", "expected_source_records": 16},
}

OHLC_FIELDS = ("open", "high", "low", "close")

PRICE_INDICATOR_CONFIG = {
    "ema_periods": (9, 21),
    "sma_periods": (20, 50),
    "wma_periods": (20, 50),
    "bollinger": {"period": 20, "standard_deviations": 2.0},
    "rsi_period": 14,
    "macd": {"fast_period": 12, "slow_period": 26, "signal_period": 9},
    "stochastic": {"k_period": 14, "k_smoothing": 3, "d_period": 3},
    "adx_period": 14,
    "cci_period": 20,
    "mfi_period": 14,
    "williams_r_period": 14,
    "atr_period": 14,
    "fibonacci_lookback": 100,
    "support_resistance_lookback": 120,
    "wasserstein": {"recent_window": 20, "reference_window": 100},
    "tsi": {"slow_period": 25, "fast_period": 13},
    "dynamic_oscillator_thresholds": {"lookback": 120, "lower_fraction": 0.20, "upper_fraction": 0.80},
}

INDICATOR_MODULES = {
    "moving_averages": "processing.math.indicators.trend.moving_averages",
    "bollinger_bands": "processing.math.indicators.volatility.bollinger_bands",
    "fibonacci_levels": "processing.math.indicators.market_structure.fibonacci_levels",
    "rsi": "processing.math.indicators.momentum.rsi",
    "macd": "processing.math.indicators.trend.macd",
    "stochastic": "processing.math.indicators.momentum.stochastic",
    "adx": "processing.math.indicators.trend.adx",
    "cci": "processing.math.indicators.momentum.cci",
    "mfi": "processing.math.indicators.volume.mfi",
    "williams_r": "processing.math.indicators.momentum.williams_r",
    "atr": "processing.math.indicators.volatility.atr",
    "tsi": "processing.math.indicators.momentum.tsi",
}

PRICE_STATISTICS_CONFIG = {
    "return_type": "simple", "z_score_lookback": 100,
    "standard_deviation_ddof": 1, "kurtosis_mode": "pearson",
    "confidence_level": 0.95, "risk_free_rate": 0.0, "target_return": 0.0,
}

PRICE_PERIODS_PER_YEAR = {
    "1m": 525600, "5m": 105120, "15m": 35040,
    "4h": 2190,
}


def _finite_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def extract_ohlcv_arrays(records: list[dict[str, Any]]) -> dict[str, Any]:
    ordered    = _validated_records(records)
    timestamps = [int(row["timestamp"]) for row in ordered]
    arrays: dict[str, Any] = {
        "records": ordered,
        "timestamps": timestamps,
        **{field: pd.Series([row[field] for row in ordered], dtype="float64") for field in OHLC_FIELDS},
    }
    has_base_volume = bool(ordered) and all(
        row.get("base_volume") is not None for row in ordered
    )
    if has_base_volume:
        arrays["mfi_volume"] = pd.Series(
            [float(row["base_volume"]) for row in ordered], dtype="float64"
        )
        arrays["volume_metadata"] = {
            "volume_mode": "base_volume",
            "source_field": "base_volume",
        }
    else:
        source_field = "combined_volume_usd" if any(
            "combined_volume_usd" in row for row in ordered
        ) else "volume_usd"
        typical      = (arrays["high"] + arrays["low"] + arrays["close"]) / 3.0
        quote_volume = pd.Series(
            [float(row.get(source_field, 0.0) or 0.0) for row in ordered], dtype="float64"
        )
        safe_typical = typical.where(typical > 0)
        arrays["mfi_volume"] = (quote_volume / safe_typical).replace(
            [float("inf"), float("-inf")], pd.NA
        )
        arrays["volume_metadata"] = {
            "volume_mode": "estimated_base_from_quote_volume",
            "source_field": source_field,
            "estimation": "volume_usd_divided_by_typical_price",
        }
    return arrays


def align_indicator_series(values: Any, length: int) -> list[float | None]:
    if isinstance(values, pd.Series):
        raw = values.tolist()
    elif isinstance(values, Sequence) and not isinstance(values, (str, bytes, bytearray)):
        raw = list(values)
    else:
        raw = []
    if len(raw) < length:
        raw = [None] * (length - len(raw)) + raw
    elif len(raw) > length:
        raw = raw[-length:]
    return [_finite_or_none(value) for value in raw]


def last_valid_value(values: Sequence[Any]) -> float | None:
    for value in reversed(values):
        valid = _finite_or_none(value)
        if valid is not None:
            return valid
    return None


def _observed_range_thresholds(
    values: Sequence[Any],
    *,
    lookback: int = 120,
    lower_fraction: float = 0.20,
    upper_fraction: float = 0.80,
) -> dict[str, Any]:
    """Precompute adaptive oscillator bands from the observed value range.

    HMI consumes these numbers verbatim; it never derives market metrics.
    Lower/upper are range fractions, not fixed oscillator values or quantiles.
    """
    finite_values = [value for value in (_finite_or_none(item) for item in list(values)[-int(lookback):]) if value is not None]
    if len(finite_values) < 5:
        return {
            "status": "unavailable", "reason": "insufficient_observed_range",
            "lookback": int(lookback), "valid_points": len(finite_values),
            "lower_fraction": float(lower_fraction), "upper_fraction": float(upper_fraction),
            "observed_min": None, "observed_max": None, "lower": None, "midpoint": None, "upper": None,
        }
    observed_min = min(finite_values)
    observed_max = max(finite_values)
    span = observed_max - observed_min
    if span <= 1e-12:
        return {
            "status": "unavailable", "reason": "degenerate_observed_range",
            "lookback": int(lookback), "valid_points": len(finite_values),
            "lower_fraction": float(lower_fraction), "upper_fraction": float(upper_fraction),
            "observed_min": observed_min, "observed_max": observed_max,
            "lower": None, "midpoint": None, "upper": None,
        }
    return {
        "status": "available", "reason": None,
        "lookback": int(lookback), "valid_points": len(finite_values),
        "lower_fraction": float(lower_fraction), "upper_fraction": float(upper_fraction),
        "observed_min": observed_min, "observed_max": observed_max,
        "lower": observed_min + float(lower_fraction) * span,
        "midpoint": observed_min + 0.50 * span,
        "upper": observed_min + float(upper_fraction) * span,
        "basis": "observed_min_plus_fraction_of_observed_range",
    }


def _attach_dynamic_oscillator_thresholds(package: dict[str, Any], config: Mapping[str, Any]) -> None:
    series = package.get("series", {})
    values = next((items for items in series.values() if isinstance(items, list)), [])
    thresholds = _observed_range_thresholds(
        values,
        lookback=int(config.get("lookback", 120)),
        lower_fraction=float(config.get("lower_fraction", 0.20)),
        upper_fraction=float(config.get("upper_fraction", 0.80)),
    )
    package["dynamic_thresholds"] = thresholds
    if thresholds.get("status") == "available":
        package["reference_lines"] = [
            {"role": "oversold", "value": thresholds["lower"], "label": "20% RANGE", "basis": thresholds["basis"]},
            {"role": "overbought", "value": thresholds["upper"], "label": "80% RANGE", "basis": thresholds["basis"]},
        ]
    else:
        package["reference_lines"] = []


def evaluate_indicator_quality(
    series: Mapping[str, Sequence[Any]],
    *,
    required_records: int,
    available_records: int,
    warnings: Sequence[str] = (),
) -> dict[str, Any]:
    values       = [value for items in series.values() for value in items]
    valid_points = sum(_finite_or_none(value) is not None for value in values)
    null_points  = len(values) - valid_points
    status       = "insufficient_data" if available_records < required_records or valid_points == 0 else "ok"
    return {
        "status": status,
        "valid_points": valid_points,
        "null_points": null_points,
        "required_records": required_records,
        "available_records": available_records,
        "warnings": list(warnings),
    }


def _source_metadata(market_type: str, timeframe: str) -> dict[str, Any]:
    return {"market_type": market_type, "timeframe": timeframe, "is_synthetic_source": False}


def build_single_series_indicator(
    *,
    indicator_id: str,
    series_name: str,
    values: Any,
    timestamps: Sequence[int],
    parameters: Mapping[str, Any],
    warmup_records: int,
    market_type: str,
    timeframe: str,
    module: str,
    function: str,
    extra_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    aligned = align_indicator_series(values, len(timestamps))
    if len(timestamps) < warmup_records:
        aligned = [None] * len(timestamps)
    current    = last_valid_value(aligned)
    last_index = max((i for i, value in enumerate(aligned) if value is not None), default=None)
    output     = {
        "indicator_id": indicator_id,
        "parameters": dict(parameters),
        "timestamps": list(timestamps),
        "series": {series_name: aligned},
        "current": {series_name: current},
        "warmup_records": warmup_records,
        "source": _source_metadata(market_type, timeframe),
        "quality": evaluate_indicator_quality(
            {series_name: aligned},
            required_records=warmup_records,
            available_records=len(timestamps),
        ),
        "calculation": {
            "module": module,
            "function": function,
            "parameters": dict(parameters),
            "records": len(timestamps),
            "last_valid_timestamp": timestamps[last_index] if last_index is not None else None,
        },
    }
    if extra_metadata:
        output["metadata"] = dict(extra_metadata)
    return output


def build_multi_series_indicator(
    *,
    indicator_id: str,
    raw_series: Mapping[str, Any],
    timestamps: Sequence[int],
    parameters: Mapping[str, Any],
    warmup_records: int,
    market_type: str,
    timeframe: str,
    module: str,
    function: str,
) -> dict[str, Any]:
    series = {name: align_indicator_series(values, len(timestamps)) for name, values in raw_series.items()}
    if len(timestamps) < warmup_records:
        series = {name: [None] * len(timestamps) for name in series}
    current      = {name: last_valid_value(values) for name, values in series.items()}
    last_indexes = [
        i for values in series.values() for i, value in enumerate(values) if value is not None
    ]
    return {
        "indicator_id": indicator_id,
        "parameters": dict(parameters),
        "timestamps": list(timestamps),
        "series": series,
        "current": current,
        "warmup_records": warmup_records,
        "source": _source_metadata(market_type, timeframe),
        "quality": evaluate_indicator_quality(
            series,
            required_records=warmup_records,
            available_records=len(timestamps),
        ),
        "calculation": {
            "module": module,
            "function": function,
            "parameters": dict(parameters),
            "records": len(timestamps),
            "last_valid_timestamp": timestamps[max(last_indexes)] if last_indexes else None,
        },
    }


def calculate_prices_indicator_package(
    *, records: list[dict[str, Any]], market_type: str, timeframe: str,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Calculate the complete, presentation-neutral Prices indicator package."""
    cfg    = deepcopy(config or PRICE_INDICATOR_CONFIG)
    arrays = extract_ohlcv_arrays(records)
    ts, high, low, close = arrays["timestamps"], arrays["high"], arrays["low"], arrays["close"]

    ma_series: dict[str, Any] = {}
    for period in cfg["ema_periods"]:
        ma_series[f"ema_{period}"] = ema(close, span=period)
    for period in cfg["sma_periods"]:
        ma_series[f"sma_{period}"] = sma(close, window=period)
    for period in cfg["wma_periods"]:
        ma_series[f"wma_{period}"] = wma(close, window=period)
    ma_parameters = {key: list(cfg[key]) for key in ("ema_periods", "sma_periods", "wma_periods")}
    package: dict[str, Any] = {
        "moving_averages": build_multi_series_indicator(
            indicator_id="moving_averages", raw_series=ma_series, timestamps=ts,
            parameters=ma_parameters, warmup_records=max((*cfg["ema_periods"], *cfg["sma_periods"], *cfg["wma_periods"])),
            market_type=market_type, timeframe=timeframe,
            module=INDICATOR_MODULES["moving_averages"], function="ema/sma/wma",
        )
    }

    bb_cfg = cfg["bollinger"]
    bb     = bollinger_bands(close, window=bb_cfg["period"], std_mult=bb_cfg["standard_deviations"])
    package["bollinger_bands"] = build_multi_series_indicator(
        indicator_id="bollinger_bands",
        raw_series={"upper": bb[f"bb_upper_{bb_cfg['period']}"] , "middle": bb[f"bb_middle_{bb_cfg['period']}"] , "lower": bb[f"bb_lower_{bb_cfg['period']}"]},
        timestamps=ts, parameters={"period": bb_cfg["period"], "standard_deviations": bb_cfg["standard_deviations"]},
        warmup_records=bb_cfg["period"], market_type=market_type, timeframe=timeframe,
        module=INDICATOR_MODULES["bollinger_bands"], function="bollinger_bands",
    )

    fib_period = cfg["fibonacci_lookback"]
    fib        = fibonacci_levels(high, low, window=fib_period)
    fib_names  = {
        "swing_high": "fib_swing_high", "swing_low": "fib_swing_low", "0.0": "fib_000_100",
        "0.236": "fib_236_100", "0.382": "fib_382_100", "0.5": "fib_500_100",
        "0.618": "fib_618_100", "0.786": "fib_786_100", "1.0": "fib_1000_100",
    }
    fib_result = build_multi_series_indicator(
        indicator_id="fibonacci_levels", raw_series={name: fib[column] for name, column in fib_names.items()},
        timestamps=ts,
        parameters={
            "lookback": fib_period,
            "ratios": [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0],
            "anchor_convention": "0%=swing_origin;100%=swing_endpoint",
            "swing_rule": "rolling_window_extremes_most_recent_occurrence",
        },
        warmup_records=fib_period,
        market_type=market_type, timeframe=timeframe, module=INDICATOR_MODULES["fibonacci_levels"], function="fibonacci_levels",
    )
    fib_levels_current = {
        level: fib_result["current"][level]
        for level in ("0.0", "0.236", "0.382", "0.5", "0.618", "0.786", "1.0")
    }
    fib_origin = fib_levels_current["0.0"]
    fib_endpoint = fib_levels_current["1.0"]
    fib_direction = None
    if fib_origin is not None and fib_endpoint is not None:
        if fib_endpoint > fib_origin:
            fib_direction = "bullish"
        elif fib_endpoint < fib_origin:
            fib_direction = "bearish"
    fib_result["current"] = {
        "swing_high": fib_result["current"]["swing_high"],
        "swing_low": fib_result["current"]["swing_low"],
        "direction": fib_direction,
        "origin": fib_origin,
        "endpoint": fib_endpoint,
        "levels": fib_levels_current,
    }
    package["fibonacci_levels"] = fib_result

    simple_specs = (
        ("rsi", "rsi", rsi(close, window=cfg["rsi_period"]), {"period": cfg["rsi_period"]}, cfg["rsi_period"], "rsi"),
        ("cci", "cci", cci(high, low, close, window=cfg["cci_period"]), {"period": cfg["cci_period"]}, cfg["cci_period"], "cci"),
        ("williams_r", "williams_r", williams_r(high, low, close, window=cfg["williams_r_period"]), {"period": cfg["williams_r_period"]}, cfg["williams_r_period"], "williams_r"),
        ("atr", "atr", atr(high, low, close, window=cfg["atr_period"]), {"period": cfg["atr_period"]}, cfg["atr_period"], "atr"),
        ("tsi", "tsi", tsi(close, slow=cfg["tsi"]["slow_period"], fast=cfg["tsi"]["fast_period"]), dict(cfg["tsi"]), cfg["tsi"]["slow_period"] + cfg["tsi"]["fast_period"], "tsi"),
    )
    for indicator_id, series_name, values, parameters, warmup, function in simple_specs:
        package[indicator_id] = build_single_series_indicator(
            indicator_id=indicator_id, series_name=series_name, values=values, timestamps=ts,
            parameters=parameters, warmup_records=warmup, market_type=market_type, timeframe=timeframe,
            module=INDICATOR_MODULES[indicator_id], function=function,
        )

    adaptive_cfg = cfg["dynamic_oscillator_thresholds"]
    _attach_dynamic_oscillator_thresholds(package["rsi"], adaptive_cfg)
    _attach_dynamic_oscillator_thresholds(package["tsi"], adaptive_cfg)

    macd_cfg   = cfg["macd"]
    macd_frame = macd(close, fast=macd_cfg["fast_period"], slow=macd_cfg["slow_period"], signal=macd_cfg["signal_period"])
    package["macd"] = build_multi_series_indicator(
        indicator_id="macd", raw_series={"macd": macd_frame["macd"], "signal": macd_frame["macd_signal"], "histogram": macd_frame["macd_hist"]},
        timestamps=ts, parameters=macd_cfg, warmup_records=macd_cfg["slow_period"] + macd_cfg["signal_period"] - 1,
        market_type=market_type, timeframe=timeframe, module=INDICATOR_MODULES["macd"], function="macd",
    )
    st_cfg = cfg["stochastic"]
    st     = stochastic(high, low, close, window=st_cfg["k_period"], smooth=st_cfg["d_period"], k_smoothing=st_cfg["k_smoothing"], d_period=st_cfg["d_period"])
    package["stochastic"] = build_multi_series_indicator(
        indicator_id="stochastic", raw_series={"k": st[f"stoch_k_{st_cfg['k_period']}"] , "d": st[f"stoch_d_{st_cfg['k_period']}"]},
        timestamps=ts, parameters=st_cfg, warmup_records=st_cfg["k_period"] + st_cfg["k_smoothing"] + st_cfg["d_period"] - 2,
        market_type=market_type, timeframe=timeframe, module=INDICATOR_MODULES["stochastic"], function="stochastic",
    )
    adx_period = cfg["adx_period"]
    adx_frame  = adx(high, low, close, window=adx_period)
    package["adx"] = build_multi_series_indicator(
        indicator_id="adx", raw_series={"adx": adx_frame[f"adx_{adx_period}"], "di_plus": adx_frame[f"plus_di_{adx_period}"], "di_minus": adx_frame[f"minus_di_{adx_period}"]},
        timestamps=ts, parameters={"period": adx_period}, warmup_records=adx_period * 2,
        market_type=market_type, timeframe=timeframe, module=INDICATOR_MODULES["adx"], function="adx",
    )
    mfi_period = cfg["mfi_period"]
    package["mfi"] = build_single_series_indicator(
        indicator_id="mfi", series_name="mfi", values=mfi(high, low, close, arrays["mfi_volume"], window=mfi_period),
        timestamps=ts, parameters={"period": mfi_period}, warmup_records=mfi_period,
        market_type=market_type, timeframe=timeframe, module=INDICATOR_MODULES["mfi"], function="mfi",
        extra_metadata=arrays["volume_metadata"],
    )
    package["regression_channel"] = build_regression_channel_indicator(
        records=records, market_type=market_type, timeframe=timeframe, window=100, deviation_multiplier=2.0
    )

    bb_upper = package["bollinger_bands"]["series"]["upper"]
    bb_middle = package["bollinger_bands"]["series"]["middle"]
    bb_lower = package["bollinger_bands"]["series"]["lower"]
    bb_width = []
    for upper_value, middle_value, lower_value in zip(bb_upper, bb_middle, bb_lower, strict=True):
        if upper_value is None or middle_value in (None, 0) or lower_value is None:
            bb_width.append(None)
        else:
            bb_width.append((float(upper_value) - float(lower_value)) / abs(float(middle_value)))
    package["bollinger_band_width"] = {
        "indicator_id": "bollinger_band_width", "parameters": {"period": bb_cfg["period"]},
        "timestamps": list(ts), "series": {"bollinger_band_width": bb_width},
        "current": {"bollinger_band_width": last_valid_value(bb_width)},
        "warmup_records": bb_cfg["period"], "source": _source_metadata(market_type, timeframe),
        "quality": evaluate_indicator_quality({"bollinger_band_width": bb_width}, required_records=bb_cfg["period"], available_records=len(ts)),
        "calculation": {"module": "processing.prices_ohlcv.prices_ohlcv_processor", "function": "bollinger_band_width", "records": len(ts)},
    }

    close_returns = close.pct_change(fill_method=None).tolist()
    wcfg = cfg["wasserstein"]
    wasserstein = rolling_wasserstein(close_returns, recent_window=wcfg["recent_window"], reference_window=wcfg["reference_window"])
    package["wasserstein_distance"] = {
        "indicator_id": "wasserstein_distance", "parameters": dict(wcfg), "timestamps": list(ts),
        "series": {"wasserstein_distance": wasserstein}, "current": {"wasserstein_distance": last_valid_value(wasserstein)},
        "warmup_records": wcfg["recent_window"] + wcfg["reference_window"], "source": _source_metadata(market_type, timeframe),
        "quality": evaluate_indicator_quality({"wasserstein_distance": wasserstein}, required_records=wcfg["recent_window"] + wcfg["reference_window"], available_records=len(ts)),
        "calculation": {"module": "processing.math.native_analysis", "function": "rolling_wasserstein", "records": len(ts)},
    }

    sr = support_resistance_levels(high.tolist(), low.tolist(), close.tolist(), lookback=cfg["support_resistance_lookback"], levels=1)
    package["support_resistance"] = {
        "indicator_id": "support_resistance",
        "parameters": {"lookback": cfg["support_resistance_lookback"], "levels": 1,
                       "mode": sr.get("method", "observed_swing_cluster_single"),
                       "fallback_used": bool(sr.get("fallback_used", False)), "fallback_type": sr.get("fallback_type")},
        "current": {"support": sr["support"], "resistance": sr["resistance"], "candidates": sr.get("candidates", {})},
        "source": _source_metadata(market_type, timeframe),
        "quality": {"status": "ok" if sr["support"] or sr["resistance"] else "insufficient_data"},
        "calculation": {"module": "processing.math.native_analysis", "function": "support_resistance_levels"},
    }
    return package



def build_regression_channel_indicator(*, records: list[dict[str, Any]], market_type: str, timeframe: str, window: int = 100, deviation_multiplier: float = 2.0) -> dict[str, Any]:
    ordered = _validated_records(records)
    timestamps = [int(row["timestamp"]) for row in ordered]
    closes = [float(row["close"]) for row in ordered]
    middle: list[float | None] = [None] * len(ordered)
    upper: list[float | None] = [None] * len(ordered)
    lower: list[float | None] = [None] * len(ordered)
    n = int(window)
    sx = n * (n - 1) / 2.0
    sx2 = (n - 1) * n * (2 * n - 1) / 6.0
    denominator = n * sx2 - sx * sx
    if n > 1 and denominator != 0:
        for end in range(n - 1, len(closes)):
            ys = closes[end - n + 1:end + 1]
            sy = sum(ys)
            sxy = sum(index * value for index, value in enumerate(ys))
            slope = (n * sxy - sx * sy) / denominator
            intercept = (sy - slope * sx) / n
            fitted = [intercept + slope * index for index in range(n)]
            residual_std = math.sqrt(sum((value - fit) ** 2 for value, fit in zip(ys, fitted, strict=True)) / n)
            center = fitted[-1]
            width = float(deviation_multiplier) * residual_std
            middle[end] = center
            upper[end] = center + width
            lower[end] = center - width
    current_index = max((idx for idx, value in enumerate(middle) if value is not None), default=None)
    current = {
        "middle": middle[current_index] if current_index is not None else None,
        "upper": upper[current_index] if current_index is not None else None,
        "lower": lower[current_index] if current_index is not None else None,
    }
    return {
        "indicator_id": "regression_channel",
        "parameters": {"window": n, "deviation_multiplier": float(deviation_multiplier), "field": "close", "fit": "rolling_ordinary_least_squares", "width_basis": "population_standard_deviation_of_residuals"},
        "timestamps": timestamps,
        "series": {"middle": middle, "upper": upper, "lower": lower},
        "current": current,
        "warmup_records": n,
        "source": _source_metadata(market_type, timeframe),
        "quality": evaluate_indicator_quality({"middle": middle, "upper": upper, "lower": lower}, required_records=n, available_records=len(timestamps)),
        "calculation": {"module": "processing.prices_ohlcv.prices_ohlcv_processor", "function": "build_regression_channel_indicator", "parameters": {"window": n, "deviation_multiplier": float(deviation_multiplier)}, "records": len(timestamps)},
    }

def calculate_market_indicators(*, market: Mapping[str, Any], market_type: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    timeframes = market.get("timeframes", {})
    return {
        timeframe: calculate_prices_indicator_package(records=deepcopy(timeframes.get(timeframe, {}).get("records", [])),
                                                      market_type=market_type, timeframe=timeframe, config=config)
        for timeframe in TIMEFRAME_ORDER
    }


def calculate_all_prices_indicators(*, markets: Mapping[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    return {market_type: calculate_market_indicators(market=markets.get(market_type, {}), market_type=market_type, config=config) for market_type in CALCULATION_MARKETS}


def calculate_prices_crosses(indicators: Mapping[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for market_type in CALCULATION_MARKETS:
        output[market_type] = {}
        for timeframe in TIMEFRAME_ORDER:
            package    = indicators.get(market_type, {}).get(timeframe, {})
            timestamps = package.get("moving_averages", {}).get("timestamps", [])
            combined: dict[str, Sequence[Any]] = {}
            for indicator_id in ("moving_averages", "macd", "stochastic", "adx", "tsi"):
                combined.update(package.get(indicator_id, {}).get("series", {}))
            regression_middle = package.get("regression_channel", {}).get("series", {}).get("middle")
            bollinger_middle = package.get("bollinger_bands", {}).get("series", {}).get("middle")
            if regression_middle is not None:
                combined["regression_middle"] = regression_middle
            if bollinger_middle is not None:
                combined["bollinger_middle"] = bollinger_middle
            pairs = [
                ("ema_9", "ema_21"),
                ("sma_20", "sma_50"),
                ("wma_20", "wma_50"),
                ("regression_middle", "bollinger_middle"),
                ("macd", "signal"), ("k", "d"), ("di_plus", "di_minus"),
            ]
            if "signal" in package.get("tsi", {}).get("series", {}):
                pairs.append(("tsi", "signal"))
            cross_events = detect_cross_pairs(timestamps=timestamps, series=combined, pairs=pairs)
            index_by_timestamp = {int(timestamp): index for index, timestamp in enumerate(timestamps)}
            for event in cross_events:
                index = index_by_timestamp.get(int(event["timestamp"]))
                first_name = str(event.get("first_series"))
                second_name = str(event.get("second_series"))
                if index is not None:
                    first_series_values = combined.get(first_name, [])
                    second_series_values = combined.get(second_name, [])
                    event["first_value"] = _finite_or_none(first_series_values[index]) if index < len(first_series_values) else None
                    event["second_value"] = _finite_or_none(second_series_values[index]) if index < len(second_series_values) else None
                    if index > 0:
                        exact = interpolated_cross(
                            previous_timestamp=int(timestamps[index - 1]), timestamp=int(timestamps[index]),
                            previous_first=first_series_values[index - 1] if index - 1 < len(first_series_values) else None,
                            previous_second=second_series_values[index - 1] if index - 1 < len(second_series_values) else None,
                            first=event["first_value"], second=event["second_value"],
                        )
                        event.update(exact)
                        event["event_price"] = exact.get("event_value_exact")
            output[market_type][timeframe] = cross_events
    return output


def calculate_prices_patterns(markets: Mapping[str, Any]) -> dict[str, Any]:
    return {
        market: {
            timeframe: detect_candlestick_patterns(
                records=deepcopy(markets.get(market, {}).get("timeframes", {}).get(timeframe, {}).get("records", []))
            )
            for timeframe in TIMEFRAME_ORDER
        }
        for market in CALCULATION_MARKETS
    }


def _safe_number(value: Any) -> float | None:
    return _finite_or_none(value)


def calculate_prices_statistical_package(
    *, records: list[dict[str, Any]], market_type: str, timeframe: str,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    cfg                = {**PRICE_STATISTICS_CONFIG, **dict(config or {})}
    ordered            = _validated_records(records)
    closes             = [row["close"] for row in ordered]
    returns            = calculate_simple_returns(closes)
    clean_return_count = sum(value is not None for value in returns)
    last_close         = closes[-1] if closes else None
    confidence         = float(cfg["confidence_level"])
    var_return         = calculate_historical_var(returns, confidence)
    cvar_return        = calculate_historical_cvar(returns, confidence)
    equity_curve       = calculate_equity_curve(returns)
    max_drawdown       = calculate_max_drawdown(equity_curve)
    periods            = PRICE_PERIODS_PER_YEAR[timeframe]
    performance_values = {
        "max_consecutive_wins": calculate_max_consecutive_wins(returns),
        "max_consecutive_losses": calculate_max_consecutive_losses(returns),
        "omega_ratio": calculate_omega_ratio(returns, cfg["target_return"]),
        "sharpe_ratio": calculate_sharpe_ratio(returns, cfg["risk_free_rate"], periods),
        "sortino_ratio": calculate_sortino_ratio(returns, cfg["target_return"], periods),
        "calmar_ratio": calculate_calmar_ratio(returns, max_drawdown, periods),
        "max_drawdown": max_drawdown,
        "profit_factor": calculate_profit_factor(returns),
        "recovery_factor": calculate_recovery_factor(returns, max_drawdown),
        "win_rate": calculate_win_rate(returns),
    }
    warnings = [f"{key}_unavailable" for key, value in performance_values.items() if value is None]
    status   = "insufficient_data" if clean_return_count < 2 else ("partial" if warnings else "ok")
    risk_key = int(round(confidence * 100))
    return {
        "timestamps": [row["timestamp"] for row in ordered],
        "returns": returns,
        "descriptive": {
            "mean_close": calculate_mean(closes),
            "close_standard_deviation": calculate_standard_deviation(closes, cfg["standard_deviation_ddof"]),
            "return_standard_deviation": calculate_standard_deviation(returns, cfg["standard_deviation_ddof"]),
            "skewness": calculate_skewness(returns),
            "kurtosis": calculate_kurtosis(returns, cfg["kurtosis_mode"]),
            "z_score": calculate_z_score(closes, cfg["z_score_lookback"], cfg["standard_deviation_ddof"]),
            "metadata": {key: cfg[key] for key in ("return_type", "z_score_lookback", "standard_deviation_ddof", "kurtosis_mode")},
        },
        "risk": {
            f"var_{risk_key}_return": var_return, f"cvar_{risk_key}_return": cvar_return,
            f"var_{risk_key}_price": var_return * last_close if var_return is not None and last_close is not None else None,
            f"cvar_{risk_key}_price": cvar_return * last_close if cvar_return is not None and last_close is not None else None,
            "metadata": {"method": "historical", "confidence_level": confidence,
                         "return_type": cfg["return_type"], "price_transformation": "return_metric_multiplied_by_last_close"},
        },
        "performance": {
            **performance_values, "equity_curve": equity_curve,
            "performance_basis": "market_returns",
            "metadata": {"performance_basis": "market_returns", "return_type": cfg["return_type"],
                         "risk_free_rate": cfg["risk_free_rate"], "target_return": cfg["target_return"],
                         "periods_per_year": periods},
        },
        "source": _source_metadata(market_type, timeframe),
        "quality": {"status": status, "available_records": len(ordered), "warnings": warnings, "errors": []},
    }


def calculate_all_prices_statistics(*, markets: Mapping[str, Any], config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {
        market: {
            timeframe: calculate_prices_statistical_package(
                records=deepcopy(markets.get(market, {}).get("timeframes", {}).get(timeframe, {}).get("records", [])),
                market_type=market, timeframe=timeframe, config=config,
            )
            for timeframe in TIMEFRAME_ORDER
        }
        for market in CALCULATION_MARKETS
    }


def _current(indicator_package: Mapping[str, Any], indicator: str, series: str) -> float | None:
    return _safe_number(indicator_package.get(indicator, {}).get("current", {}).get(series))


def _difference(first: float | None, second: float | None) -> float | None:
    return first - second if first is not None and second is not None else None


def build_indicator_bias_components(*, indicator_package: dict[str, Any], close: float | None = None) -> dict[str, Any]:
    ma                 = indicator_package.get("moving_averages", {}).get("current", {})
    macd_current       = indicator_package.get("macd", {}).get("current", {})
    stochastic_current = indicator_package.get("stochastic", {}).get("current", {})
    adx_current        = indicator_package.get("adx", {}).get("current", {})
    bb_middle          = _current(indicator_package, "bollinger_bands", "middle")
    atr_value          = _current(indicator_package, "atr", "atr")
    rsi_value          = _current(indicator_package, "rsi", "rsi")
    mfi_value          = _current(indicator_package, "mfi", "mfi")
    williams           = _current(indicator_package, "williams_r", "williams_r")
    values             = {
        "ema_9_minus_ema_21": _difference(_safe_number(ma.get("ema_9")), _safe_number(ma.get("ema_21"))),
        "ema_21_minus_ema_50": _difference(_safe_number(ma.get("ema_21")), _safe_number(ma.get("ema_50"))),
        "sma_20_minus_sma_50": _difference(_safe_number(ma.get("sma_20")), _safe_number(ma.get("sma_50"))),
        "macd_minus_signal": _difference(_safe_number(macd_current.get("macd")), _safe_number(macd_current.get("signal"))),
        "macd_histogram": _safe_number(macd_current.get("histogram")),
        "rsi_centered": rsi_value - 50.0 if rsi_value is not None else None,
        "stochastic_k_minus_d": _difference(_safe_number(stochastic_current.get("k")), _safe_number(stochastic_current.get("d"))),
        "adx": _safe_number(adx_current.get("adx")),
        "di_plus_minus_di_minus": _difference(_safe_number(adx_current.get("di_plus")), _safe_number(adx_current.get("di_minus"))),
        "cci": _current(indicator_package, "cci", "cci"),
        "mfi_centered": mfi_value - 50.0 if mfi_value is not None else None,
        "williams_r_centered": williams + 50.0 if williams is not None else None,
        "tsi": _current(indicator_package, "tsi", "tsi"),
        "close_minus_bollinger_middle": _difference(_safe_number(close), bb_middle),
        "atr_percent_of_close": atr_value / close * 100.0 if atr_value is not None and close not in (None, 0) else None,
    }
    return {"values": values, "metadata": {"aggregation": "none", "weights_applied": False}}


def calculate_all_prices_bias_components(*, markets: Mapping[str, Any], indicators: Mapping[str, Any]) -> dict[str, Any]:
    grouping = {"short": ["5m", "15m"], "mid": ["4h"], "micro_confirmation": ["1m"]}
    return {
        market: {
            "timeframes": {
                timeframe: build_indicator_bias_components(
                    indicator_package=indicators.get(market, {}).get(timeframe, {}),
                    close=(markets.get(market, {}).get("timeframes", {}).get(timeframe, {}).get("records") or [{}])[-1].get("close"),
                )
                for timeframe in TIMEFRAME_ORDER
            },
            "metadata": {"timeframe_groups": deepcopy(grouping), "overall_calculated": False},
        }
        for market in CALCULATION_MARKETS
    }


def bucket_start(timestamp: int, timeframe: str) -> int:
    if timeframe not in TIMEFRAME_SECONDS:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    seconds = TIMEFRAME_SECONDS[timeframe]
    return int(timestamp) - (int(timestamp) % seconds)


def _validated_records(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_timestamp: dict[int, dict[str, Any]] = {}
    for record in records:
        timestamp  = int(record["timestamp"])
        normalized = dict(record)
        normalized["timestamp"] = timestamp
        for field in OHLC_FIELDS:
            normalized[field] = float(record[field])
        normalized["volume_usd"] = float(record.get("volume_usd", 0.0) or 0.0)
        if normalized["high"] < max(normalized["open"], normalized["close"], normalized["low"]):
            raise ValueError(f"Invalid OHLC high at timestamp {timestamp}")
        if normalized["low"] > min(normalized["open"], normalized["close"], normalized["high"]):
            raise ValueError(f"Invalid OHLC low at timestamp {timestamp}")
        by_timestamp[timestamp] = normalized
    return [by_timestamp[timestamp] for timestamp in sorted(by_timestamp)]


def aggregate_ohlcv_bucket(
    records: Sequence[Mapping[str, Any]],
    *,
    source_timeframe: str,
    target_timeframe: str,
    now_timestamp: int | None = None,
) -> dict[str, Any]:
    if target_timeframe not in RESAMPLING_RULES:
        raise ValueError(f"No resampling rule for {target_timeframe}")
    rule = RESAMPLING_RULES[target_timeframe]
    if rule["source_timeframe"] != source_timeframe:
        raise ValueError(f"{target_timeframe} must be built from {rule['source_timeframe']}")
    ordered = _validated_records(records)
    if not ordered:
        raise ValueError("Cannot aggregate an empty OHLCV bucket")
    start = bucket_start(ordered[0]["timestamp"], target_timeframe)
    end   = start + TIMEFRAME_SECONDS[target_timeframe]
    if any(not start <= row["timestamp"] < end for row in ordered):
        raise ValueError("Source records span multiple target buckets")

    expected       = int(rule["expected_source_records"])
    now            = int(time.time()) if now_timestamp is None else int(now_timestamp)
    complete_count = len(ordered) == expected
    is_closed      = complete_count and now >= end
    return {
        "timestamp": start,
        "open": ordered[0]["open"],
        "high": max(row["high"] for row in ordered),
        "low": min(row["low"] for row in ordered),
        "close": ordered[-1]["close"],
        "volume_usd": sum(row["volume_usd"] for row in ordered),
        "source_records": len(ordered),
        "expected_source_records": expected,
        "is_closed": is_closed,
        "is_partial": not is_closed,
        "source_timeframe": source_timeframe,
        "target_timeframe": target_timeframe,
    }


def find_affected_buckets(
    incoming_records: Sequence[Mapping[str, Any]],
    *,
    target_timeframe: str,
) -> list[int]:
    return sorted({bucket_start(int(record["timestamp"]), target_timeframe) for record in incoming_records})


def recompute_affected_buckets(
    source_records: Sequence[Mapping[str, Any]],
    *,
    source_timeframe: str,
    target_timeframe: str,
    affected_buckets: Sequence[int],
    now_timestamp: int | None = None,
) -> list[dict[str, Any]]:
    validated = _validated_records(source_records)
    duration  = TIMEFRAME_SECONDS[target_timeframe]
    results: list[dict[str, Any]] = []
    for start in sorted(set(int(value) for value in affected_buckets)):
        members = [row for row in validated if start <= row["timestamp"] < start + duration]
        if members:
            results.append(
                aggregate_ohlcv_bucket(
                    members,
                    source_timeframe=source_timeframe,
                    target_timeframe=target_timeframe,
                    now_timestamp=now_timestamp,
                )
            )
    return results


def upsert_derived_records(
    existing_records: Sequence[Mapping[str, Any]],
    derived_records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    by_timestamp = {int(record["timestamp"]): dict(record) for record in existing_records}
    by_timestamp.update({int(record["timestamp"]): dict(record) for record in derived_records})
    return [by_timestamp[timestamp] for timestamp in sorted(by_timestamp)]


def update_market_timeframes(
    market: Mapping[str, Any],
    *,
    mode: str,
    existing_market: Mapping[str, Any] | None = None,
    now_timestamp: int | None = None,
) -> dict[str, Any]:
    input_timeframes    = market.get("timeframes", {})
    existing_timeframes = (existing_market or {}).get("timeframes", {})
    output: dict[str, dict[str, Any]] = {}

    for timeframe in TIMEFRAME_ORDER:
        input_payload    = input_timeframes.get(timeframe, {})
        existing_payload = existing_timeframes.get(timeframe, {})
        input_records    = input_payload.get("records", [])
        base_records     = input_records or existing_payload.get("records", [])
        output[timeframe] = {
            "records": _validated_records(base_records),
            "incoming_records": deepcopy(input_payload.get("incoming_records", [])),
        }

    for target_timeframe, rule in RESAMPLING_RULES.items():
        source_timeframe = str(rule["source_timeframe"])
        source_payload   = output[source_timeframe]
        target_payload   = output[target_timeframe]

        if mode == "bootstrap" and target_payload["records"]:
            continue

        affected = find_affected_buckets(
            source_payload["incoming_records"],
            target_timeframe=target_timeframe,
        )
        if not affected and not target_payload["records"] and source_payload["records"]:
            affected = sorted(
                {bucket_start(row["timestamp"], target_timeframe) for row in source_payload["records"]}
            )
        recomputed = recompute_affected_buckets(
            source_payload["records"],
            source_timeframe=source_timeframe,
            target_timeframe=target_timeframe,
            affected_buckets=affected,
            now_timestamp=now_timestamp,
        )
        target_payload["records"] = upsert_derived_records(target_payload["records"], recomputed)
        target_payload["incoming_records"] = recomputed

    return {
        key: deepcopy(value)
        for key, value in market.items()
        if key != "timeframes"
    } | {"timeframes": output}


def update_prices_timeframes(
    input_contract: Mapping[str, Any],
    *,
    existing_processing: Mapping[str, Any] | None = None,
    now_timestamp: int | None = None,
) -> dict[str, Any]:
    markets          = input_contract.get("markets", {})
    existing_markets = (existing_processing or {}).get("markets", {})
    mode             = str(input_contract.get("mode", "bootstrap"))
    output = {
        market_name: update_market_timeframes(
            markets.get(market_name, {}),
            mode=mode,
            existing_market=existing_markets.get(market_name, {}),
            now_timestamp=now_timestamp,
        )
        for market_name in ("spot", "futures")
    }
    return output


def calculate_spot_futures_comparison(markets: Mapping[str, Any]) -> dict[str, Any]:
    by_timeframe: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for timeframe in TIMEFRAME_ORDER:
        spot = {int(row["timestamp"]): row for row in markets["spot"]["timeframes"][timeframe]["records"]}
        futures = {int(row["timestamp"]): row for row in markets["futures"]["timeframes"][timeframe]["records"]}
        series: list[dict[str, float | int]] = []
        for timestamp in sorted(set(spot) & set(futures)):
            spot_close = float(spot[timestamp]["close"])
            futures_close = float(futures[timestamp]["close"])
            if spot_close == 0:
                warnings.append(f"{timeframe}/{timestamp}: zero spot denominator")
                continue
            series.append({
                "timestamp": timestamp,
                "spot_price": spot_close,
                "futures_price": futures_close,
                "basis_usd": futures_close - spot_close,
                "basis_percent": ((futures_close / spot_close) - 1.0) * 100.0,
            })
        by_timeframe[timeframe] = {
            "series": series,
            "current": deepcopy(series[-1]) if series else {},
        }
    return {"by_timeframe": by_timeframe, "warnings": warnings}


def evaluate_prices_processing_quality(markets: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate only the canonical Spot OHLCV surface.

    Futures OHLC was retired from the frozen 33-endpoint VR1 contract.  Empty
    compatibility placeholders must therefore never degrade Prices quality or
    trigger recovery; they are not an external dependency.
    """
    warnings: list[str] = []
    errors: list[str] = []
    market_name = "spot"
    for timeframe in TIMEFRAME_ORDER:
        payload = markets.get(market_name, {}).get("timeframes", {}).get(timeframe)
        if payload is None:
            errors.append(f"missing {market_name}/{timeframe}")
        elif not payload.get("records"):
            warnings.append(f"empty {market_name}/{timeframe}")
    status = "invalid" if errors else ("partial" if warnings else "ok")
    return {"status": status, "warnings": warnings, "errors": errors}



def apply_cvd_volume_sides(markets: dict[str, Any], cvd_processing_context: Mapping[str, Any] | None) -> None:
    if not isinstance(cvd_processing_context, Mapping):
        return
    spot = cvd_processing_context.get("markets", {}).get("spot", {})
    for timeframe in TIMEFRAME_ORDER:
        cvd_records = spot.get("timeframes", {}).get(timeframe, {}).get("records", [])
        lookup = {int(row["timestamp"]): row for row in cvd_records if isinstance(row, Mapping) and row.get("timestamp") is not None}
        for market_name in ("spot",):
            records = markets.get(market_name, {}).get("timeframes", {}).get(timeframe, {}).get("records", [])
            for row in records:
                source = lookup.get(int(row.get("timestamp", -1)))
                if source is None:
                    continue
                buy = native_finite(source.get("taker_buy_volume_usd"))
                sell = native_finite(source.get("taker_sell_volume_usd"))
                if buy is None or sell is None:
                    continue
                row["buy_volume_usd"] = buy
                row["sell_volume_usd"] = sell
                total = buy + sell
                row["buy_share"] = buy / total if total else None
                row["sell_share"] = sell / total if total else None


class PricesOhlcvProcessor:
    """Family-specific OO orchestrator for numeric Prices processing."""

    def __init__(self, *, feature_builder: PricesOhlcvFeatureBuilder | None = None) -> None:
        self.feature_builder = feature_builder or PricesOhlcvFeatureBuilder()

    def run(
        self,
        input_contract: Mapping[str, Any],
        *,
        existing_processing: Mapping[str, Any] | None = None,
        now_timestamp: int | None = None,
        dirty_timeframes: Sequence[str] | None = None,
        cvd_processing_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if input_contract.get("family") != "prices_ohlcv":
            raise ValueError("Prices processor requires family=prices_ohlcv")
        mode    = str(input_contract.get("mode", "bootstrap"))
        markets = update_prices_timeframes(
            input_contract,
            existing_processing=existing_processing,
            now_timestamp=now_timestamp,
        )
        apply_cvd_volume_sides(markets, cvd_processing_context)
        comparison              = calculate_spot_futures_comparison(markets)
        dirty = set(dirty_timeframes or ())
        for target_timeframe, rule in RESAMPLING_RULES.items():
            if str(rule["source_timeframe"]) in dirty:
                dirty.add(target_timeframe)
        previous_features = (existing_processing or {}).get("features", {})
        windowed = mode == "incremental" and bool(dirty) and bool(previous_features)
        if windowed:
            indicators = deepcopy(previous_features.get("indicators", {}))
            technical_crosses = deepcopy(previous_features.get("technical_crosses", {}))
            candlestick_patterns = deepcopy(previous_features.get("candlestick_patterns", {}))
            statistical_performance = deepcopy(previous_features.get("statistical_performance", {}).get("markets", {}))
            bias_components = deepcopy(previous_features.get("bias_components", {}))
            for market_name in CALCULATION_MARKETS:
                indicators.setdefault(market_name, {})
                technical_crosses.setdefault(market_name, {})
                candlestick_patterns.setdefault(market_name, {})
                statistical_performance.setdefault(market_name, {})
                bias_components.setdefault(market_name, {})
                for timeframe in dirty:
                    records = deepcopy(markets.get(market_name, {}).get("timeframes", {}).get(timeframe, {}).get("records", []))
                    indicators[market_name][timeframe] = calculate_prices_indicator_package(
                        records=records, market_type=market_name, timeframe=timeframe,
                    )
                    candlestick_patterns[market_name][timeframe] = detect_candlestick_patterns(records=records)
                    statistical_performance[market_name][timeframe] = calculate_prices_statistical_package(
                        records=records, market_type=market_name, timeframe=timeframe,
                    )
                    bias_components[market_name][timeframe] = build_indicator_bias_components(
                        indicator_package=indicators[market_name][timeframe],
                        close=(records[-1]["close"] if records else None),
                    )
            recalculated_crosses = calculate_prices_crosses(indicators)
            for market_name in CALCULATION_MARKETS:
                for timeframe in dirty:
                    technical_crosses[market_name][timeframe] = recalculated_crosses[market_name][timeframe]
        else:
            indicators              = calculate_all_prices_indicators(markets=markets)
            technical_crosses       = calculate_prices_crosses(indicators)
            candlestick_patterns    = calculate_prices_patterns(markets)
            statistical_performance = calculate_all_prices_statistics(markets=markets)
            bias_components         = calculate_all_prices_bias_components(markets=markets, indicators=indicators)
        quality                 = evaluate_prices_processing_quality(markets)
        quality["warnings"].extend(comparison["warnings"])
        if quality["status"] == "ok" and comparison["warnings"]:
            quality["status"] = "partial"
        return {
            "family": "prices_ohlcv",
            "stage": "processing",
            "mode": mode,
            "context": deepcopy(input_contract.get("context", {})),
            "markets": markets,
            "provider_features": deepcopy(input_contract.get("provider_features", {})),
            "features": self.feature_builder.build(
                markets=markets, comparison=comparison, indicators=indicators,
                technical_crosses=technical_crosses,
                candlestick_patterns=candlestick_patterns,
                statistical_performance=statistical_performance,
                bias_components=bias_components,
            ),
            "quality": quality,
            "incremental_execution": {
                "mode": "INCREMENTAL_WINDOWED" if windowed else "FULL",
                "dirty_timeframes": sorted(dirty),
                "indicator_records_recomputed": (
                    sum(len(markets[market]["timeframes"][timeframe]["records"])
                        for market in CALCULATION_MARKETS for timeframe in dirty)
                    if windowed else sum(len(markets[market]["timeframes"][timeframe]["records"])
                                         for market in CALCULATION_MARKETS for timeframe in TIMEFRAME_ORDER)
                ),
            },
        }


def run_prices_ohlcv_processing(
    input_contract: Mapping[str, Any],
    *,
    existing_processing: Mapping[str, Any] | None = None,
    now_timestamp: int | None = None,
    dirty_timeframes: Sequence[str] | None = None,
    cvd_processing_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Single public facade for Processing Prices."""
    return PricesOhlcvProcessor().run(
        input_contract,
        existing_processing=existing_processing,
        now_timestamp=now_timestamp,
        dirty_timeframes=dirty_timeframes,
        cvd_processing_context=cvd_processing_context,
    )
