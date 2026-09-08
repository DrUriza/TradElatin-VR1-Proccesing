"""Mathematical Processing v0.1 for open interest and funding."""
from __future__ import annotations

import copy
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any, Callable

import numpy as np
import pandas as pd

from .open_interest_and_funding_math import ema, sma, wma
from .open_interest_and_funding_math import detect_numeric_crosses
from .open_interest_and_funding_math import (
    open_interest_wasserstein_frame,
    rolling_zscore,
    rolling_percentile,
    pct_change as native_pct_change,
    difference as native_difference,
    latest,
    interpolated_cross,
)
from processing_signals.processing.open_interest_and_funding.open_interest_and_funding_feature_builder import OpenInterestAndFundingFeatureBuilder

FAMILY            = "open_interest_and_funding"
TIMEFRAMES        = ("5m", "15m", "4h")
TIMEFRAME_SECONDS = {"5m": 300, "15m": 900, "4h": 14_400}
CHANGE_24H_WARMUP = {timeframe: 86_400 // seconds + 1 for timeframe, seconds in TIMEFRAME_SECONDS.items()}
VALID_STATUSES    = {"available", "partial", "unavailable", "invalid"}
CONTEXT_FIELDS    = ("asset", "exchange_scope", "primary_provider", "confirmation_providers", "data_mode", "is_demo",
                     "reference_timestamp", "execution_timestamp", "generated_at")
SOURCE_IDS        = {"open_interest_ohlc": ("aggregated_open_interest_ohlc", "USD"),
                     "funding_rate_ohlc": ("oi_weighted_funding_rate_ohlc", "percent_points")}


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return 0.0 if number == 0.0 else number


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, (float, np.floating)):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("processing output contains a non-finite value")
        return 0.0 if number == 0.0 else number
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("processing output contains a non-string key")
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe(item) for item in value]
    raise ValueError(f"processing output contains unsupported type {type(value).__name__}")


def _input_contract(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("input_contract must be an Input contract or vertical bundle")
    candidate = value.get("input", value)
    if not isinstance(candidate, Mapping) or candidate.get("family") != FAMILY or candidate.get("stage") != "input":
        raise ValueError("Processing requires family=open_interest_and_funding and stage=input")
    context, series = candidate.get("context"), candidate.get("series")
    if candidate.get("mode") not in {"bootstrap", "incremental", "recovery"}:
        raise ValueError("Input mode is incompatible")
    if not isinstance(context, Mapping) or any(field not in context for field in CONTEXT_FIELDS):
        raise ValueError("Input context is structurally incomplete")
    if context.get("asset") != "BTC" or context.get("primary_provider") != "coinglass" or type(context.get("reference_timestamp")) is not int:
        raise ValueError("Input context is incompatible")
    if not isinstance(series, Mapping):
        raise ValueError("Input series must be a mapping")
    for metric_id, (endpoint_id, unit) in SOURCE_IDS.items():
        metric = series.get(metric_id)
        if not isinstance(metric, Mapping) or metric.get("provider") != "coinglass" or metric.get("endpoint_id") != endpoint_id or metric.get("unit") != unit:
            raise ValueError(f"Input {metric_id} metadata is incompatible")
        if metric_id == "funding_rate_ohlc" and (metric.get("aggregation") != "open_interest_weighted" or metric.get("representation") != "percentage_points"):
            raise ValueError("Input funding metadata is incompatible")
        timeframes = metric.get("timeframes")
        if not isinstance(timeframes, Mapping) or any(timeframe not in timeframes for timeframe in TIMEFRAMES):
            raise ValueError(f"Input {metric_id} must contain all six timeframes")
    return candidate


def _validate_frame(metric_id: str, timeframe: str, frame: Any, reference_timestamp: int) -> tuple[list[dict[str, Any]], str | None]:
    if not isinstance(frame, Mapping) or frame.get("status") not in VALID_STATUSES or not isinstance(frame.get("records"), list):
        return [], "invalid_timeframe_structure"
    if frame.get("expected_interval_seconds") != TIMEFRAME_SECONDS[timeframe] or frame.get("unit") != SOURCE_IDS[metric_id][1]:
        return [], "invalid_timeframe_metadata"
    records, previous = [], None
    for row in frame["records"]:
        if not isinstance(row, Mapping) or type(row.get("timestamp")) is not int or row["timestamp"] > reference_timestamp:
            return [], "invalid_record_timestamp"
        if previous is not None and row["timestamp"] <= previous:
            return [], "timestamps_not_strictly_increasing"
        values = {field: _finite(row.get(field)) for field in ("open", "high", "low", "close")}
        if any(value is None for value in values.values()):
            return [], "invalid_ohlc_numeric"
        if metric_id == "open_interest_ohlc" and any(value < 0 for value in values.values()):
            return [], "negative_open_interest"
        if values["high"] < max(values.values()) or values["low"] > min(values.values()):
            return [], "inconsistent_ohlc"
        records.append({"timestamp": row["timestamp"], **values})
        previous = row["timestamp"]
    return records, None


def _segments(records: Sequence[Mapping[str, Any]], interval: int) -> tuple[list[tuple[int, int]], list[dict[str, int]]]:
    if not records:
        return [], []
    bounds, gaps, start = [], [], 0
    for index in range(1, len(records)):
        difference = records[index]["timestamp"] - records[index - 1]["timestamp"]
        if difference != interval:
            bounds.append((start, index))
            missing = max(0, math.ceil(difference / interval) - 1)
            gaps.append({"previous_timestamp": records[index - 1]["timestamp"], "next_timestamp": records[index]["timestamp"],
                "expected_interval_seconds": interval, "missing_records": missing,
                "start_timestamp": records[index - 1]["timestamp"] + interval, "end_timestamp": records[index]["timestamp"] - interval})
            start = index
    bounds.append((start, len(records)))
    return bounds, gaps


def _segment_metadata(records: Sequence[Mapping[str, Any]], bounds: Sequence[tuple[int, int]]) -> list[dict[str, Any]]:
    return [{"segment_start_index": start, "segment_end_index": end - 1, "segment_first_timestamp": records[start]["timestamp"],
        "segment_last_timestamp": records[end - 1]["timestamp"], "record_count": end - start} for start, end in bounds]


def _source(metric_id: str, timeframe: str) -> dict[str, Any]:
    endpoint_id, unit = SOURCE_IDS[metric_id]
    return {"metric_id": metric_id, "provider": "coinglass", "endpoint_id": endpoint_id, "timeframe": timeframe, "unit": unit}


def _wrapper(*, timestamps: Sequence[int], series: Mapping[str, Sequence[Any]], units: Mapping[str, str], source: Mapping[str, Any], parameters: Mapping[str, Any],
             warmup: int | None, calculation: str | None, source_status: str, bounds: Sequence[tuple[int, int]], gaps: Sequence[Mapping[str, Any]],
             insufficient_reason: str = "insufficient_history", forced_status: str | None = None, forced_reason: str | None = None) -> dict[str, Any]:
    normalized = {name: [_finite(value) for value in values] for name, values in series.items()}
    normalized_units = copy.deepcopy(dict(units))
    if any(len(values) != len(timestamps) for values in normalized.values()):
        raise ValueError("calculation arrays have incompatible lengths")
    last_segment = range(*bounds[-1]) if bounds else range(0)
    complete_indices = [index for index in last_segment if normalized and all(values[index] is not None for values in normalized.values())]
    current_index = complete_indices[-1] if complete_indices else None
    current = {name: values[current_index] for name, values in normalized.items()} if current_index is not None else None
    if forced_status:
        status, reason = forced_status, forced_reason
    elif source_status == "invalid":
        status, reason = "invalid", "source_invalid"
    elif source_status == "unavailable" or not timestamps:
        status, reason = "unavailable", "source_unavailable"
    elif current is None:
        status, reason = "partial", insufficient_reason
    elif source_status == "partial" or gaps:
        status, reason = "partial", "source_partial_or_gapped"
    else:
        status, reason = "available", None
    if set(normalized) != set(normalized_units) or any(not isinstance(unit, str) for unit in normalized_units.values()):
        status, reason = "invalid", "series_units_mismatch"
    finite_records = sum(all(values[index] is not None for values in normalized.values()) for index in range(len(timestamps))) if normalized else 0
    return {"status": status, "reason": reason, "timestamps": list(timestamps), "series": normalized, "units": normalized_units, "current": current,
        "current_timestamp": timestamps[current_index] if current_index is not None else None, "parameters": copy.deepcopy(dict(parameters)),
        "warmup_records": warmup, "calculation": calculation, "source": copy.deepcopy(dict(source)),
        "quality": {"records_available": len(timestamps), "finite_records": finite_records, "null_records": len(timestamps) - finite_records,
            "segments": len(bounds), "gaps_present": bool(gaps), "warnings": []}}


def _segment_calculation(records: Sequence[Mapping[str, Any]], bounds: Sequence[tuple[int, int]], names: Sequence[str],
                         calculator: Callable[[pd.DataFrame], Mapping[str, Sequence[Any]]], warmup: int) -> dict[str, list[float | None]]:
    output = {name: [None] * len(records) for name in names}
    for start, end in bounds:
        frame = pd.DataFrame(records[start:end])
        calculated = calculator(frame)
        for name in names:
            values = list(calculated[name])
            if len(values) != end - start:
                raise ValueError("Math returned an incompatible series length")
            for local_index, value in enumerate(values):
                output[name][start + local_index] = None if local_index < warmup - 1 else _finite(value)
    return output


def _oi_delta(records: Sequence[Mapping[str, Any]], bounds: Sequence[tuple[int, int]]) -> dict[str, list[float | None]]:
    absolute, percent = [None] * len(records), [None] * len(records)
    for start, end in bounds:
        for index in range(start + 1, end):
            previous, current = records[index - 1]["close"], records[index]["close"]
            absolute[index] = current - previous
            percent[index] = 100 * (current / previous - 1) if previous > 0 else None
    return {"delta_absolute_usd": absolute, "delta_percent": percent}


def _oi_change_24h(records: Sequence[Mapping[str, Any]], bounds: Sequence[tuple[int, int]]) -> dict[str, list[float | None]]:
    absolute, percent = [None] * len(records), [None] * len(records)
    for start, end in bounds:
        positions = {records[index]["timestamp"]: index for index in range(start, end)}
        for index in range(start, end):
            reference_index = positions.get(records[index]["timestamp"] - 86_400)
            if reference_index is None:
                continue
            previous, current = records[reference_index]["close"], records[index]["close"]
            absolute[index] = current - previous
            percent[index] = 100 * (current / previous - 1) if previous > 0 else None
    return {"change_absolute_usd": absolute, "change_percent": percent}


def _oi_roc(records: Sequence[Mapping[str, Any]], bounds: Sequence[tuple[int, int]], period: int = 12) -> dict[str, list[float | None]]:
    values = [None] * len(records)
    for start, end in bounds:
        for index in range(start + period, end):
            previous = records[index - period]["close"]
            values[index] = 100 * (records[index]["close"] / previous - 1) if previous > 0 else None
    return {"roc": values}


def _regression_channel_series(records: Sequence[Mapping[str, Any]], bounds: Sequence[tuple[int, int]], window: int = 100, deviation_multiplier: float = 2.0) -> dict[str, list[float | None]]:
    middle = [None] * len(records)
    upper = [None] * len(records)
    lower = [None] * len(records)
    n = int(window)
    sx = n * (n - 1) / 2.0
    sx2 = (n - 1) * n * (2 * n - 1) / 6.0
    denominator = n * sx2 - sx * sx
    if n <= 1 or denominator == 0:
        return {"middle": middle, "upper": upper, "lower": lower}
    for start, end in bounds:
        closes = [float(records[index]["close"]) for index in range(start, end)]
        if len(closes) < n:
            continue
        for local_end in range(n - 1, len(closes)):
            ys = closes[local_end - n + 1:local_end + 1]
            sy = sum(ys)
            sxy = sum(index * value for index, value in enumerate(ys))
            slope = (n * sxy - sx * sy) / denominator
            intercept = (sy - slope * sx) / n
            fitted = [intercept + slope * index for index in range(n)]
            residual_std = math.sqrt(sum((value - fit) ** 2 for value, fit in zip(ys, fitted, strict=True)) / n)
            center = fitted[-1]
            width = float(deviation_multiplier) * residual_std
            target = start + local_end
            middle[target] = center
            upper[target] = center + width
            lower[target] = center - width
    return {"middle": middle, "upper": upper, "lower": lower}


def _indicator_packages(records: Sequence[Mapping[str, Any]], bounds: Sequence[tuple[int, int]], gaps: Sequence[Mapping[str, Any]],
                        timeframe: str, source_status: str) -> dict[str, Any]:
    timestamps, source = [row["timestamp"] for row in records], _source("open_interest_ohlc", timeframe)
    ma_names = ("ema_9", "ema_21", "sma_20", "sma_50", "wma_20", "wma_50")
    def ma_calc(frame: pd.DataFrame) -> dict[str, pd.Series]:
        close = frame["close"]
        return {
            "ema_9": ema(close, 9), "ema_21": ema(close, 21),
            "sma_20": sma(close, 20), "sma_50": sma(close, 50),
            "wma_20": wma(close, 20), "wma_50": wma(close, 50),
        }
    ma = _segment_calculation(records, bounds, ma_names, ma_calc, 1)
    warmups = {"ema_9": 9, "ema_21": 21, "sma_20": 20, "sma_50": 50, "wma_20": 20, "wma_50": 50}
    for start, end in bounds:
        for name, period in warmups.items():
            for index in range(start, min(end, start + period - 1)):
                ma[name][index] = None
    moving = _wrapper(timestamps=timestamps, series=ma, units={name: "USD" for name in ma_names}, source=source,
        parameters={"ema_periods": [9, 21], "sma_periods": [20, 50], "wma_periods": [20, 50]}, warmup=50,
        calculation="ema_sma_wma_on_open_interest_close", source_status=source_status, bounds=bounds, gaps=gaps)

    roc_package = _wrapper(timestamps=timestamps, series=_oi_roc(records, bounds), units={"roc": "percent"}, source=source, parameters={"period": 12}, warmup=13,
        calculation="100*(close[t]/close[t-12]-1)", source_status=source_status, bounds=bounds, gaps=gaps)

    def bollinger_calc(frame: pd.DataFrame) -> dict[str, pd.Series]:
        close = pd.to_numeric(frame["close"], errors="coerce")
        middle = close.rolling(window=20, min_periods=20).mean()
        sigma = close.rolling(window=20, min_periods=20).std(ddof=0)
        upper = middle + 2.0 * sigma
        lower = middle - 2.0 * sigma
        width = upper - lower
        bandwidth = 100.0 * width / middle.replace(0.0, np.nan)
        percent_b = (close - lower) / width.replace(0.0, np.nan)
        return {"middle": middle, "upper": upper, "lower": lower, "bandwidth": bandwidth, "percent_b": percent_b}

    bollinger_values = _segment_calculation(
        records, bounds, ("middle", "upper", "lower", "bandwidth", "percent_b"), bollinger_calc, 20
    )
    bollinger_package = _wrapper(
        timestamps=timestamps,
        series=bollinger_values,
        units={"middle": "USD", "upper": "USD", "lower": "USD", "bandwidth": "percent", "percent_b": "ratio"},
        source=source,
        parameters={"period": 20, "standard_deviations": 2.0, "std_ddof": 0},
        warmup=20,
        calculation="oi_close_rolling_mean_20_plus_minus_2_population_std",
        source_status=source_status,
        bounds=bounds,
        gaps=gaps,
    )

    regression_values = _regression_channel_series(records, bounds, window=100, deviation_multiplier=2.0)
    regression_package = _wrapper(
        timestamps=timestamps,
        series=regression_values,
        units={"middle": "USD", "upper": "USD", "lower": "USD"},
        source=source,
        parameters={"window": 100, "residual_standard_deviations": 2.0},
        warmup=100,
        calculation="rolling_ols_on_oi_close_with_plus_minus_2_residual_population_std",
        source_status=source_status,
        bounds=bounds,
        gaps=gaps,
    )

    wasserstein_values = _segment_calculation(records, bounds, ("distance",), open_interest_wasserstein_frame, 60)
    wasserstein_package = _wrapper(timestamps=timestamps, series=wasserstein_values, units={"distance": "ratio"}, source=source,
        parameters={"recent_window_returns": 20, "reference_window_returns": 40}, warmup=61,
        calculation="first_wasserstein_distance_between_recent_and_reference_open_interest_returns",
        source_status=source_status, bounds=bounds, gaps=gaps)
    retired_fields = {
        "bollinger_band_width": ("bandwidth",),
        "macd": ("macd", "signal", "histogram"), "rsi": ("rsi",), "tsi": ("tsi", "signal"),
        "adx": ("adx", "di_plus", "di_minus"), "stochastic": ("k", "d"),
        "williams_r": ("williams_r",), "atr": ("atr",), "cci": ("cci",), "mfi": (),
    }
    retired = {
        name: _wrapper(timestamps=[], series={field: [] for field in fields}, units={field: "not_applicable" for field in fields}, source=source, parameters={}, warmup=None,
            calculation=None, source_status=source_status, bounds=[], gaps=[], forced_status="unavailable",
            forced_reason="retired_by_open_interest_indicator_policy")
        for name, fields in retired_fields.items()
    }
    return {
        "moving_averages": moving,
        "bollinger_bands": bollinger_package,
        "regression_channel": regression_package,
        "wasserstein_distance": wasserstein_package,
        "oi_roc": roc_package,
        **retired,
    }


def _renamed_bollinger(frame: pd.DataFrame) -> dict[str, pd.Series]:
    return {"middle": frame["bb_middle_20"], "upper": frame["bb_upper_20"], "lower": frame["bb_lower_20"],
        "bandwidth": frame["bb_bandwidth_20"], "percent_b": frame["bb_percent_b_20"]}


def _renamed_macd(frame: pd.DataFrame) -> dict[str, pd.Series]:
    return {"macd": frame["macd"], "signal": frame["macd_signal"], "histogram": frame["macd_hist"]}


def _renamed_adx(frame: pd.DataFrame) -> dict[str, pd.Series]:
    return {"adx": frame["adx_14"], "di_plus": frame["plus_di_14"], "di_minus": frame["minus_di_14"]}


def _renamed_stochastic(frame: pd.DataFrame) -> dict[str, pd.Series]:
    return {"k": frame["stoch_k_14"], "d": frame["stoch_d_14"]}


def _processed_frame(metric_id: str, timeframe: str, input_frame: Mapping[str, Any], reference_timestamp: int) -> tuple[dict[str, Any], dict[str, Any] | None]:
    records, error = _validate_frame(metric_id, timeframe, input_frame, reference_timestamp)
    source_status = input_frame.get("status") if isinstance(input_frame, Mapping) and input_frame.get("status") in VALID_STATUSES else "invalid"
    if error:
        source_status = "invalid"
    bounds, gaps = _segments(records, TIMEFRAME_SECONDS[timeframe])
    status = "invalid" if error else ("unavailable" if source_status == "unavailable" or not records else ("partial" if source_status == "partial" or gaps else source_status))
    current = copy.deepcopy(records[-1]) if records and status != "invalid" else None
    source_reason = input_frame.get("reason") if isinstance(input_frame, Mapping) else None
    result = {"status": status, "reason": error or source_reason or ("source_partial" if status == "partial" else None), "timeframe": timeframe,
        "expected_interval_seconds": TIMEFRAME_SECONDS[timeframe], "unit": SOURCE_IDS[metric_id][1],
        "representation": "percentage_points" if metric_id == "funding_rate_ohlc" else None, "source": _source(metric_id, timeframe),
        "records": copy.deepcopy(records), "current": current, "coverage": {"records": len(records), "segment_count": len(bounds),
            "segments": _segment_metadata(records, bounds), "first_timestamp": records[0]["timestamp"] if records else None,
            "last_timestamp": records[-1]["timestamp"] if records else None, "gaps": copy.deepcopy(gaps)},
        "quality": {"source_status": source_status, "records_available": len(records), "gaps_present": bool(gaps), "warnings": []}}
    if metric_id == "open_interest_ohlc":
        timestamps, source = [row["timestamp"] for row in records], _source(metric_id, timeframe)
        result["derived"] = {
            "oi_delta": _wrapper(timestamps=timestamps, series=_oi_delta(records, bounds), units={"delta_absolute_usd": "USD", "delta_percent": "percent"}, source=source, parameters={}, warmup=2,
                calculation="close[t]-close[t-1];100*(close[t]/close[t-1]-1)", source_status=source_status, bounds=bounds, gaps=gaps),
            "oi_change_24h": _wrapper(timestamps=timestamps, series=_oi_change_24h(records, bounds), units={"change_absolute_usd": "USD", "change_percent": "percent"}, source=source,
                parameters={"seconds": 86_400, "lag_bars": 86_400 // TIMEFRAME_SECONDS[timeframe]}, warmup=CHANGE_24H_WARMUP[timeframe],
                calculation="exact_timestamp_close_change_over_86400_seconds", source_status=source_status, bounds=bounds, gaps=gaps,
                insufficient_reason="insufficient_history_for_exact_24h_change")}
        return result, _indicator_packages(records, bounds, gaps, timeframe, source_status)
    return result, None


def _event(*, timeframe: str, event_type: str, pair: str, source_metric: str, cross: Mapping[str, Any],
           first_series: str, second_series: str | None, threshold: float | None, values: Mapping[str, Any], parameters: Mapping[str, Any]) -> dict[str, Any]:
    event_id = f"{FAMILY}:{timeframe}:{cross['timestamp']}:{event_type}:{pair}"
    return {"event_id": event_id, "event_type": event_type, "timestamp": cross["timestamp"], "timeframe": timeframe,
        "source_metric": source_metric, "first_series": first_series, "second_series": second_series, "threshold": threshold,
        "direction_numeric": cross["direction"], "previous_difference": _finite(cross["previous_difference"]),
        "current_difference": _finite(cross["current_difference"]), "interpolation_fraction": _finite(cross.get("interpolation_fraction")),
        "event_timestamp_exact": _finite(cross.get("event_timestamp_exact")), "event_value_exact": _finite(cross.get("event_value_exact")),
        "values": _json_safe(dict(values)), "parameters": copy.deepcopy(dict(parameters))}


def _events_for_timeframe(timeframe: str, oi_frame: Mapping[str, Any], funding_frame: Mapping[str, Any], indicators: Mapping[str, Any]) -> list[dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    bounds = [(item["segment_start_index"], item["segment_end_index"] + 1) for item in oi_frame["coverage"]["segments"]]
    timestamps = [row["timestamp"] for row in oi_frame["records"]]
    timestamp_indices = {timestamp: index for index, timestamp in enumerate(timestamps)}
    ma_pairs = [("ema_9", "ema_21"), ("sma_20", "sma_50"), ("wma_20", "wma_50")]
    specifications = [
        *(("moving_average_cross", f"{first}_x_{second}", indicators["moving_averages"], first, second, None) for first, second in ma_pairs),
        ("oi_roc_zero_cross", "oi_roc_12_x_0", indicators["oi_roc"], "roc", None, 0.0),
    ]
    for event_type, pair, package, first, second, threshold in specifications:
        first_values = package["series"].get(first, [])
        second_values = package["series"].get(second, []) if second else [threshold] * len(timestamps)
        for start, end in bounds:
            crosses = detect_numeric_crosses(timestamps=timestamps[start:end], first_values=first_values[start:end], second_values=second_values[start:end],
                first_series=first, second_series=second or str(int(threshold or 0)))
            for cross in crosses:
                index = timestamp_indices[cross["timestamp"]]
                values: dict[str, Any] = {first: first_values[index], second or "threshold": second_values[index]}
                if index > 0:
                    exact = interpolated_cross(
                        previous_timestamp=timestamps[index - 1], timestamp=timestamps[index],
                        previous_first=first_values[index - 1], previous_second=second_values[index - 1],
                        first=first_values[index], second=second_values[index],
                    )
                    cross = {**cross, **exact}
                    values.update({key: value for key, value in exact.items() if key.endswith("value") or key in {"interpolation_fraction", "previous_difference", "current_difference"}})
                item = _event(timeframe=timeframe, event_type=event_type, pair=pair, source_metric="open_interest_ohlc", cross=cross,
                    first_series=first, second_series=second, threshold=threshold, values=values, parameters=package["parameters"])
                if cross.get("event_timestamp_exact") is not None:
                    item["event_timestamp_exact"] = cross.get("event_timestamp_exact")
                    item["event_value_exact"] = cross.get("event_value_exact")
                    item["event_price"] = cross.get("event_value_exact")
                output[item["event_id"]] = item
    funding_timestamps = [row["timestamp"] for row in funding_frame["records"]]
    funding_timestamp_indices = {timestamp: index for index, timestamp in enumerate(funding_timestamps)}
    funding_values = [row["close"] for row in funding_frame["records"]]
    for segment in funding_frame["coverage"]["segments"]:
        start, end = segment["segment_start_index"], segment["segment_end_index"] + 1
        for cross in detect_numeric_crosses(timestamps=funding_timestamps[start:end], first_values=funding_values[start:end],
                                            second_values=[0.0] * (end - start), first_series="funding_close", second_series="zero"):
            index = funding_timestamp_indices[cross["timestamp"]]
            item = _event(timeframe=timeframe, event_type="funding_zero_cross", pair="funding_close_x_0", source_metric="funding_rate_ohlc",
                cross=cross, first_series="funding_close", second_series=None, threshold=0.0, values={"funding_close": funding_values[index], "threshold": 0.0}, parameters={})
            output[item["event_id"]] = item
    return sorted(output.values(), key=lambda item: (item["timestamp"], item["event_type"], item["event_id"]))


def _snapshot_sections(input_snapshots: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    # Final-33 OI does not contract exchange snapshot/options endpoints. Preserve
    # explicit unavailable shells for schema compatibility without fabricating
    # provider observations or referencing retired endpoint IDs.
    snapshots = {
        "open_interest_by_exchange": {"status": "unavailable", "reason": "not_contracted_final33",
            "records": [], "invalid_records": [], "aggregate_record": None, "exchange_count": 0,
            "current_total_usd": None, "reported_changes": {}},
        "funding_rate_by_exchange": {"status": "unavailable", "reason": "not_contracted_final33",
            "records": [], "invalid_records": [], "stablecoin_margin_records": [], "token_margin_records": [],
            "exchange_count": 0, "next_funding_timestamps": []},
        "options_open_interest": {"status": "unavailable", "reason": "not_contracted_final33",
            "records": [], "invalid_records": [], "aggregate_record": None,
            "current_options_open_interest_usd": None, "current_options_contracts": None},
    }
    metrics = {
        "reported_24h_percent": {"status": "unavailable", "reason": "not_contracted_final33",
            "value": None, "unit": "percent", "provider": None, "endpoint_id": None,
            "source_scope": "all_exchanges", "observation_timestamp": None},
        "current_open_interest": {"series_current_close_usd": {}, "snapshot_current_usd": None,
            "comparison": {"status": "unavailable", "reason": "snapshot_not_contracted_final33"}},
    }
    return snapshots, metrics

def _confirmations(input_confirmations: Any) -> dict[str, Any]:
    source = input_confirmations if isinstance(input_confirmations, Mapping) else {}
    leverage = source.get("estimated_leverage_ratio") if isinstance(source.get("estimated_leverage_ratio"), Mapping) else {}
    payload = leverage.get("glassnode")
    base = {"provider": "glassnode", "endpoint_id": "futures_estimated_leverage_ratio", "unit": "ratio", "provider_interval": "1h"}
    if not isinstance(payload, Mapping):
        normalized = {**base, "status": "invalid", "reason": "confirmation_payload_not_mapping", "records": []}
    else:
        normalized = {**copy.deepcopy(dict(payload)), **base}
        records = payload.get("records")
        if not isinstance(records, list):
            normalized.update(status="invalid", reason="confirmation_records_not_list", records=[])
        else:
            valid = [copy.deepcopy(dict(row)) for row in records if isinstance(row, Mapping)]
            invalid = [{"index": index, "reason": "confirmation_record_not_mapping"}
                for index, row in enumerate(records) if not isinstance(row, Mapping)]
            normalized["records"] = valid
            if invalid:
                normalized.update(status="partial" if valid else "invalid",
                    reason="invalid_confirmation_records_isolated" if valid else "confirmation_records_incompatible",
                    invalid_records=invalid)
    return {"estimated_leverage_ratio": {"glassnode": normalized}}


def _aggregate(packages: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    timeframes = {timeframe: package["status"] for timeframe, package in packages.items()}
    statuses = list(timeframes.values())
    status = "invalid" if "invalid" in statuses else ("unavailable" if statuses and all(item == "unavailable" for item in statuses)
        else ("available" if statuses and all(item == "available" for item in statuses) else "partial"))
    return {"status": status, "timeframes": timeframes}


def _availability(series: Mapping[str, Any], indicators: Mapping[str, Any], snapshot_metrics: Mapping[str, Any], confirmations: Mapping[str, Any]) -> dict[str, Any]:
    oi_frames, funding_frames = series["open_interest_ohlc"]["timeframes"], series["funding_rate_ohlc"]["timeframes"]
    indicator_frames = indicators["open_interest"]["timeframes"]
    availability = {"open_interest_primary": _aggregate(oi_frames), "funding_rate_primary": _aggregate(funding_frames),
        "oi_delta": _aggregate({tf: frame["derived"]["oi_delta"] for tf, frame in oi_frames.items()}),
        "oi_change_24h_derived": _aggregate({tf: frame["derived"]["oi_change_24h"] for tf, frame in oi_frames.items()}),
        "oi_change_24h_reported": copy.deepcopy(snapshot_metrics["reported_24h_percent"])}
    for key in ("moving_averages", "bollinger_bands", "regression_channel", "bollinger_band_width", "macd", "rsi", "tsi", "adx",
                "stochastic", "williams_r", "atr", "cci", "wasserstein_distance", "oi_roc", "mfi"):
        availability[key] = _aggregate({tf: frame[key] for tf, frame in indicator_frames.items()})
    availability.update({"contract_type_split": {"status": "unavailable", "reason": "dated_futures_open_interest_not_separated_by_current_sources"},
        "funding_8h_aggregate": {"status": "unavailable", "reason": "cross_exchange_8h_weighting_not_defined"},
        "confirmations": {"estimated_leverage_ratio": {"glassnode": confirmations["estimated_leverage_ratio"]["glassnode"].get("status", "unavailable")}}})
    return availability


def _quality(series: Mapping[str, Any], indicators: Mapping[str, Any], snapshots: Mapping[str, Any], confirmations: Mapping[str, Any], availability: Mapping[str, Any]) -> dict[str, Any]:
    source_statuses = {f"{metric}.{timeframe}": frame["status"] for metric, metric_payload in series.items()
        for timeframe, frame in metric_payload["timeframes"].items()}
    calculation_statuses = {f"{name}.{timeframe}": package[name]["status"] for timeframe, package in indicators["open_interest"]["timeframes"].items()
        for name in ("moving_averages", "bollinger_bands", "regression_channel", "wasserstein_distance", "oi_roc")}
    calculation_statuses.update({f"oi_delta.{timeframe}": frame["derived"]["oi_delta"]["status"] for timeframe, frame in series["open_interest_ohlc"]["timeframes"].items()})
    oi_change_statuses = {f"oi_change_24h.{timeframe}": frame["derived"]["oi_change_24h"]["status"]
        for timeframe, frame in series["open_interest_ohlc"]["timeframes"].items()}
    calculation_statuses.update(oi_change_statuses)
    optional_calculations = set()
    for timeframe, frame in series["open_interest_ohlc"]["timeframes"].items():
        records = frame.get("records", [])
        if len(records) < 2 or records[-1]["timestamp"] - records[0]["timestamp"] < 86_400:
            optional_calculations.add(f"oi_change_24h.{timeframe}")
    gaps_present = any(frame["coverage"]["gaps"] for metric in series.values() for frame in metric["timeframes"].values())
    required_calculation_statuses = [value for key, value in calculation_statuses.items() if key not in optional_calculations]
    required_statuses = list(source_statuses.values()) + required_calculation_statuses
    if any(status == "invalid" for status in source_statuses.values()):
        status = "invalid"
    elif any(item != "available" for item in required_statuses) or gaps_present:
        status = "partial"
    else:
        status = "ok"
    leverage_payload = confirmations.get("estimated_leverage_ratio", {}).get("glassnode", {})
    optional_invalid = ["estimated_leverage_ratio.glassnode"] if not isinstance(leverage_payload, Mapping) or leverage_payload.get("status") == "invalid" else []
    snapshot_warnings = [f"snapshot_{name}_invalid_records" for name in
        ("open_interest_by_exchange", "funding_rate_by_exchange", "options_open_interest")
        if snapshots[name].get("invalid_records")]
    if optional_invalid and status == "ok" or snapshot_warnings and status == "ok":
        status = "partial"
    return {"status": status, "contract_complete": True,
        "data_complete": all(item == "available" for item in required_statuses) and not optional_invalid and not snapshot_warnings,
        "source_statuses": source_statuses, "calculation_statuses": calculation_statuses,
        "optional_calculations": sorted(optional_calculations),
        "records_processed": {metric: {timeframe: frame["coverage"]["records"] for timeframe, frame in payload["timeframes"].items()} for metric, payload in series.items()},
        "gaps_present": gaps_present, "warnings": snapshot_warnings + [f"optional_confirmation_invalid:{item}" for item in optional_invalid], "errors": []}



def _align_price_closes_to_oi(
    oi_timestamps: Sequence[int],
    price_records: Sequence[Mapping[str, Any]],
    timeframe: str,
) -> list[float | None]:
    """Align shared Prices context to OI by closed-candle bucket, not poll timestamp.

    Independent family acquisitions can be a few seconds apart. Exact timestamp
    joins therefore intermittently emptied Price x OI analysis (most visible on
    5m). Both inputs represent the same closed timeframe candle, so alignment is
    performed on the canonical timeframe bucket while retaining the provider
    close value and never interpolating market prices in HMI.
    """
    step = TIMEFRAME_SECONDS[timeframe]
    by_bucket: dict[int, float] = {}
    for row in price_records:
        if not isinstance(row, Mapping) or type(row.get("timestamp")) is not int:
            continue
        close = _finite(row.get("close"))
        if close is None:
            continue
        bucket = int(row["timestamp"]) - int(row["timestamp"]) % step
        by_bucket[bucket] = close
    return [by_bucket.get(int(ts) - int(ts) % step) for ts in oi_timestamps]


def _native_oi_analysis(series: Mapping[str, Any], indicators: Mapping[str, Any], *, price_history_by_timeframe: Mapping[str, Sequence[Mapping[str, Any]]] | None = None) -> dict[str, Any]:
    price_history = price_history_by_timeframe or {}
    output: dict[str, Any] = {}
    for timeframe in TIMEFRAMES:
        oi_frame = series["open_interest_ohlc"]["timeframes"][timeframe]
        funding_frame = series["funding_rate_ohlc"]["timeframes"][timeframe]
        records = oi_frame.get("records", [])
        timestamps = [int(row["timestamp"]) for row in records]
        closes = [row.get("close") for row in records]
        changes = native_pct_change(closes, 1, 100.0)
        oi_roc = indicators["open_interest"]["timeframes"][timeframe].get("oi_roc", {}).get("series", {}).get("roc", [])
        slope = rolling_zscore(changes, 20, 10)
        acceleration = native_difference(slope)
        oi_z = rolling_zscore(closes, 30, 10)
        oi_pct = rolling_percentile(closes, 90, 20)

        price_records = price_history.get(timeframe, [])
        price_closes = _align_price_closes_to_oi(timestamps, price_records, timeframe)
        price_returns = native_pct_change(price_closes, 1, 100.0)
        price_z = rolling_zscore(price_returns, 30, 10)
        oi_change_z = rolling_zscore(changes, 30, 10)
        divergence = [None if a is None or b is None else float(a) - float(b) for a, b in zip(price_z, oi_change_z, strict=True)]
        regime_score: list[float | None] = []
        regime_state: list[str | None] = []
        for pr, oc in zip(price_returns, changes, strict=True):
            if pr is None or oc is None:
                regime_score.append(None); regime_state.append(None); continue
            if pr > 0 and oc > 0:
                regime_score.append(2.0); regime_state.append("bullish_expansion")
            elif pr < 0 and oc > 0:
                regime_score.append(-2.0); regime_state.append("bearish_expansion")
            elif pr > 0 and oc < 0:
                regime_score.append(1.0); regime_state.append("short_covering")
            elif pr < 0 and oc < 0:
                regime_score.append(-1.0); regime_state.append("deleveraging")
            else:
                regime_score.append(0.0); regime_state.append("normal")

        funding_by_ts = {int(row["timestamp"]): row.get("close") for row in funding_frame.get("records", [])}
        funding_values = [funding_by_ts.get(ts) for ts in timestamps]
        funding_z = rolling_zscore(funding_values, 30, 10)
        crowding = [None if a is None or b is None else float(a) * 0.6 + float(b) * 0.4 for a, b in zip(oi_z, funding_z, strict=True)]
        wasserstein = indicators["open_interest"]["timeframes"][timeframe].get("wasserstein_distance", {}).get("series", {}).get("distance", [])

        output[timeframe] = {
            "oi_dynamics": {"timestamps": timestamps, "series": {"oi_roc": list(oi_roc), "oi_slope_pct": slope, "oi_acceleration_pct": acceleration},
                            "current": {"oi_dynamics": latest(oi_roc), "oi_slope_pct": latest(slope), "oi_acceleration_pct": latest(acceleration)}},
            "oi_zscore_percentile": {"timestamps": timestamps, "series": {"oi_zscore": oi_z, "oi_percentile": oi_pct},
                                     "current": {"oi_zscore_percentile": latest(oi_z), "oi_percentile": latest(oi_pct)}},
            "price_oi_regime": {"timestamps": timestamps, "series": {"regime_score": regime_score},
                                "current": {"price_oi_regime": latest(regime_score), "regime_state": next((v for v in reversed(regime_state) if v), None)}},
            "price_oi_divergence": {"timestamps": timestamps, "series": {"price_return_z": price_z, "oi_change_z": oi_change_z, "divergence_score": divergence},
                                    "current": {"price_oi_divergence": latest(divergence), "price_return_z": latest(price_z), "oi_change_z": latest(oi_change_z)}},
            "funding_oi_crowding": {"timestamps": timestamps, "series": {"oi_zscore": oi_z, "funding_zscore": funding_z, "crowding_score": crowding},
                                    "current": {"funding_oi_crowding": latest(crowding), "funding_zscore": latest(funding_z), "oi_zscore": latest(oi_z), "funding_rate": latest(funding_values)}},
            "wasserstein_distance": {"timestamps": timestamps, "series": {"wasserstein_distance": list(wasserstein)},
                                     "current": {"wasserstein_distance": latest(wasserstein)}},
        }
    return output


class OpenInterestAndFundingProcessor:
    """Validate Input and deterministically calculate Processing v0.1."""

    def __init__(self, feature_builder: OpenInterestAndFundingFeatureBuilder | None = None) -> None:
        self.feature_builder = feature_builder or OpenInterestAndFundingFeatureBuilder()

    def process(self, input_contract: Mapping[str, Any], *, price_history_by_timeframe: Mapping[str, Sequence[Mapping[str, Any]]] | None = None) -> dict[str, Any]:
        source = _input_contract(input_contract)
        context, series, indicator_frames = copy.deepcopy(dict(source["context"])), {}, {}
        reference_timestamp = context["reference_timestamp"]
        for metric_id in ("open_interest_ohlc", "funding_rate_ohlc"):
            frames = {}
            for timeframe in TIMEFRAMES:
                frames[timeframe], indicator = _processed_frame(metric_id, timeframe, source["series"][metric_id]["timeframes"][timeframe], reference_timestamp)
                if indicator is not None:
                    indicator_frames[timeframe] = indicator
            endpoint_id, unit = SOURCE_IDS[metric_id]
            series[metric_id] = {"provider": "coinglass", "endpoint_id": endpoint_id, "unit": unit, "timeframes": frames}
            if metric_id == "funding_rate_ohlc":
                series[metric_id].update(aggregation="open_interest_weighted", representation="percentage_points")
        snapshots, snapshot_metrics = _snapshot_sections(source.get("snapshots"))
        for timeframe in TIMEFRAMES:
            snapshot_metrics["current_open_interest"]["series_current_close_usd"][timeframe] = series["open_interest_ohlc"]["timeframes"][timeframe]["current"]["close"] if series["open_interest_ohlc"]["timeframes"][timeframe]["current"] else None
        snapshots["open_interest_by_exchange"]["reported_changes"] = copy.deepcopy(snapshot_metrics["reported_24h_percent"])
        snapshots["current_open_interest"] = snapshot_metrics["current_open_interest"]
        indicators = {"open_interest": {"timeframes": indicator_frames}}
        all_events = {timeframe: _events_for_timeframe(timeframe, series["open_interest_ohlc"]["timeframes"][timeframe],
            series["funding_rate_ohlc"]["timeframes"][timeframe], indicator_frames[timeframe]) for timeframe in TIMEFRAMES}
        by_id = {event["event_id"]: event for timeframe in TIMEFRAMES for event in all_events[timeframe]}
        events = {"by_id": by_id, "timeframes": {timeframe: {"event_ids": [event["event_id"] for event in all_events[timeframe]]} for timeframe in TIMEFRAMES}}
        confirmations = _confirmations(source.get("confirmations"))
        availability = _availability(series, indicators, snapshot_metrics, confirmations)
        quality = _quality(series, indicators, snapshots, confirmations, availability)
        native_analysis = _native_oi_analysis(series, indicators, price_history_by_timeframe=price_history_by_timeframe)
        sections = {"mode": source.get("mode"), "context": context, "series": series, "indicators": indicators, "events": events,
            "snapshots": snapshots, "confirmations": confirmations, "availability": availability, "quality": quality,
            "native_analysis": native_analysis}
        output = _json_safe(self.feature_builder.build(sections))
        json.dumps(output, ensure_ascii=False, allow_nan=False, sort_keys=False)
        return output


def process_open_interest_and_funding(input_contract: Mapping[str, Any], *, price_history_by_timeframe: Mapping[str, Sequence[Mapping[str, Any]]] | None = None) -> dict[str, Any]:
    return OpenInterestAndFundingProcessor().process(input_contract, price_history_by_timeframe=price_history_by_timeframe)
