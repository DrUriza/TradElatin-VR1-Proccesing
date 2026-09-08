from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing          import Any, Callable


SECONDS_PER_DAY             = 86_400
HASHES_PER_EXAHASH          = 1_000_000_000_000_000_000
DIFFICULTY_PER_TRILLION     = 1_000_000_000_000
SOPR_SMA_PERIOD_DAYS        = 7
RESERVE_TREND_WINDOWS_DAYS  = (7, 30)
DEFAULT_RESERVE_TREND_DAYS  = 30
DAILY_WINDOW_TOLERANCE_DAYS = 2
STATUS_PRIORITY             = {"available": 0, "partial": 1, "unavailable": 2, "invalid": 3}


def _finite(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("value_must_be_finite_number")
    number = float(value)
    return 0.0 if number == 0.0 else number


def _metadata(source_count: int, records: Sequence[Mapping[str, Any]], unavailable: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {"records_source": source_count, "records_calculated": len(records), "records_unavailable": len(unavailable),
            "first_valid_timestamp": records[0]["timestamp"] if records else None, "last_valid_timestamp": records[-1]["timestamp"] if records else None,
            "calculation_history": "full_available_history", "history_truncated": False}


def _stable_unique(messages: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(messages))


def _source_timestamp_errors(source: Mapping[str, Any], metric_id: str) -> list[str]:
    errors: list[str] = []
    previous: int | None = None
    seen: set[int] = set()
    records = source.get("records", [])
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes, bytearray)):
        return [f"source_records_must_be_sequence:{metric_id}"]
    for record in records:
        if not isinstance(record, Mapping):
            return [f"source_record_must_be_mapping:{metric_id}"]
        timestamp = record.get("timestamp")
        if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
            return [f"source_timestamp_invalid:{metric_id}"]
        if timestamp in seen:
            errors.append(f"source_duplicate_timestamp:{metric_id}:{timestamp}")
            break
        if previous is not None and timestamp < previous:
            errors.append(f"source_timestamps_not_strictly_ascending:{metric_id}")
            break
        seen.add(timestamp)
        previous = timestamp
    return errors


def build_current_snapshot(series_payload: Mapping[str, Any]) -> dict[str, Any]:
    records = series_payload.get("records", [])
    if isinstance(records, Sequence) and not isinstance(records, (str, bytes, bytearray)):
        for record in reversed(records):
            if isinstance(record, Mapping) and isinstance(record.get("timestamp"), int):
                try:
                    value = _finite(record.get("value"))
                except ValueError:
                    continue
                return {"status": "available", "timestamp": int(record["timestamp"]), "value": value, "unit": record.get("unit", series_payload.get("unit"))}
    return {"status": "unavailable", "value": None, "reason": "no_valid_calculated_records"}


def _series_payload(*, metric_id: str, unit: str, source_count: int, records: list[dict[str, Any]], unavailable: list[dict[str, Any]],
                    warnings: list[str] | None = None, errors: list[str] | None = None, source_status: str = "available",
                    force_invalid: bool = False, partial_reasons: bool = False) -> dict[str, Any]:
    warnings = list(warnings or [])
    errors   = list(errors or [])
    if force_invalid:
        status = "invalid"
    elif not records:
        status = "unavailable"
    elif source_status == "partial" or partial_reasons:
        status = "partial"
    else:
        status = "available"
    payload = {"metric_id": metric_id, "status": status, "unit": unit, "records": records, "unavailable_records": unavailable,
               "warnings": warnings, "errors": errors, "metadata": _metadata(source_count, records, unavailable)}
    payload["current"] = build_current_snapshot(payload)
    return payload


def _blocked_source_series(*, metric_id: str, unit: str, source_metric_id: str, source_status: str) -> dict[str, Any]:
    invalid = source_status == "invalid"
    reason  = f"source_series_{source_status}:{source_metric_id}"
    payload = _series_payload(metric_id=metric_id, unit=unit, source_count=0, records=[], unavailable=[],
                              warnings=[] if invalid else [reason], errors=[reason] if invalid else [], force_invalid=invalid)
    payload["status"]  = source_status
    payload["current"] = {"status": "unavailable", "value": None, "reason": f"source_series_{source_status}"}
    return payload


def apply_source_series_context(payload: Mapping[str, Any], source: Mapping[str, Any], source_metric_id: str) -> dict[str, Any]:
    """Combine mathematical and Input status using invalid > unavailable > partial > available."""
    output        = dict(payload)
    source_status = str(source["status"])
    result_status = str(output["status"])
    output["status"] = max((source_status, result_status), key=lambda status: STATUS_PRIORITY[status])
    source_marker = f"source_series_{source_status}:{source_metric_id}" if source_status != "available" else None
    warnings = list(output.get("warnings", []))
    errors   = list(output.get("errors", []))
    if source_marker:
        (errors if source_status == "invalid" else warnings).append(source_marker)
    warnings.extend(f"input_series_warning:{source_metric_id}:{message}" for message in source.get("warnings", []))
    errors.extend(f"input_series_error:{source_metric_id}:{message}" for message in source.get("errors", []))
    if source.get("gaps"):
        warnings.append(f"input_series_gaps:{source_metric_id}:{len(source['gaps'])}")
    output["warnings"] = _stable_unique(warnings)
    output["errors"]   = _stable_unique(errors)
    return output


def _copy_base_series(source: Mapping[str, Any], *, metric_id: str, unit: str, transform: Callable[[Mapping[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    if source["status"] in {"invalid", "unavailable"}:
        return apply_source_series_context(_blocked_source_series(metric_id=metric_id, unit=unit, source_metric_id=str(source["metric_id"]),
                                                                   source_status=str(source["status"])), source, str(source["metric_id"]))
    source_records = source.get("records", [])
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        for record in source_records:
            records.append(transform(record))
    except (TypeError, ValueError) as exc:
        errors.append(str(exc))
    payload = _series_payload(metric_id=metric_id, unit=unit, source_count=len(source_records), records=records, unavailable=[], errors=errors,
                              force_invalid=bool(errors))
    return apply_source_series_context(payload, source, str(source["metric_id"]))




def _daily_close_source(source: Mapping[str, Any], *, metric_id: str) -> dict[str, Any]:
    """Collapse intraday provider observations to one canonical UTC close per day."""
    if source.get("status") in {"invalid", "unavailable"}:
        return dict(source)
    records = source.get("records", [])
    by_day: dict[int, Mapping[str, Any]] = {}
    for record in records:
        if not isinstance(record, Mapping) or not isinstance(record.get("timestamp"), int):
            continue
        day = int(record["timestamp"]) - int(record["timestamp"]) % SECONDS_PER_DAY
        previous = by_day.get(day)
        if previous is None or int(record["timestamp"]) >= int(previous["timestamp"]):
            by_day[day] = record
    daily = []
    for day in sorted(by_day):
        record = dict(by_day[day])
        record["timestamp"] = day
        record["source_timestamp"] = int(by_day[day]["timestamp"])
        record["source_resolution"] = "24h"
        daily.append(record)
    output = dict(source)
    output["records"] = daily
    output["metadata"] = {**dict(source.get("metadata", {})), "daily_close_normalization": True,
                          "source_records_before_daily_close": len(records), "daily_records": len(daily)}
    return output


def build_daily_ohlc_from_intraday(records: Sequence[Mapping[str, Any]], *, unit: str,
                                   value_transform: Callable[[float], float] | None = None) -> list[dict[str, Any]]:
    """Construct legitimate UTC daily OHLC from multiple real provider observations."""
    buckets: dict[int, list[tuple[int, float]]] = {}
    for record in records:
        try:
            timestamp = int(record["timestamp"])
            value = _finite(record["value"])
            if value_transform is not None:
                value = _finite(value_transform(value))
        except (KeyError, TypeError, ValueError):
            continue
        day = timestamp - timestamp % SECONDS_PER_DAY
        buckets.setdefault(day, []).append((timestamp, value))
    candles: list[dict[str, Any]] = []
    for day in sorted(buckets):
        observations = sorted(buckets[day])
        values = [value for _, value in observations]
        candles.append({"timestamp": day, "open": values[0], "high": max(values), "low": min(values), "close": values[-1],
                        "is_closed": True, "observation_count": len(observations), "unit": unit,
                        "ohlc_origin": "processing_derived", "source_resolution": "24h"})
    return candles


def build_sopr_7d_intraday_candles(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Build 7-day rolling SOPR at provider resolution, then aggregate those values to daily OHLC."""
    ordered = sorted((int(r["timestamp"]), _finite(r.get("sopr", r.get("value")))) for r in records
                     if isinstance(r, Mapping) and isinstance(r.get("timestamp"), int))
    if not ordered:
        return []
    window_seconds = 7 * SECONDS_PER_DAY
    smoothed: list[dict[str, Any]] = []
    left = 0
    running: list[tuple[int, float]] = []
    for timestamp, value in ordered:
        running.append((timestamp, value))
        while left < len(running) and running[left][0] < timestamp - window_seconds + 3600:
            left += 1
        window = running[left:]
        if window and timestamp - window[0][0] >= window_seconds - 3600:
            smoothed.append({"timestamp": timestamp, "value": sum(v for _, v in window) / len(window)})
    return build_daily_ohlc_from_intraday(smoothed, unit="ratio")


def _copy_direct_value_series(source: Mapping[str, Any], *, metric_id: str, unit: str, source_metric_id: str) -> dict[str, Any]:
    if source.get("status") in {"invalid", "unavailable"}:
        return apply_source_series_context(_blocked_source_series(metric_id=metric_id, unit=unit, source_metric_id=source_metric_id,
                                                                   source_status=str(source.get("status"))), source, source_metric_id)
    records = []
    errors = []
    for index, record in enumerate(source.get("records", [])):
        try:
            records.append({"timestamp": int(record["timestamp"]), "value": _finite(record["value"]), "unit": unit,
                            "provider": str(record.get("provider", "glassnode")), "source_metric_id": source_metric_id,
                            "endpoint_id": record.get("endpoint_id")})
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"record[{index}]: {exc}")
    payload = _series_payload(metric_id=metric_id, unit=unit, source_count=len(source.get("records", [])), records=records,
                              unavailable=[], errors=errors, force_invalid=bool(errors), source_status=str(source.get("status", "available")))
    return apply_source_series_context(payload, source, source_metric_id)


def _reserve_record(record: Mapping[str, Any]) -> dict[str, Any]:
    return {"timestamp": int(record["timestamp"]), "value": _finite(record["value"]), "unit": "BTC", "source_metric_id": "miner_reserve",
            "provider": str(record.get("provider", "glassnode"))}


def _sopr_record(record: Mapping[str, Any]) -> dict[str, Any]:
    value = _finite(record.get("sopr", record.get("value")))
    return {"timestamp": int(record["timestamp"]), "value": value, "sopr": value,
            **{field: None if record.get(field) is None else _finite(record[field]) for field in ("a_sopr", "sth_sopr", "lth_sopr")},
            "unit": "ratio", "source_metric_id": "sopr", "provider": str(record.get("provider", "glassnode"))}


def _mpi_record(record: Mapping[str, Any]) -> dict[str, Any]:
    return {"timestamp": int(record["timestamp"]), "value": _finite(record["value"]), "unit": "z_score", "source_metric_id": "mpi",
            "provider": str(record.get("provider", "cryptoquant"))}


def build_sopr_7d_series(sopr_records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, record in enumerate(sopr_records):
        try:
            timestamp = int(record["timestamp"])
            if index < SOPR_SMA_PERIOD_DAYS - 1:
                unavailable.append({"timestamp": timestamp, "status": "unavailable", "reason": "insufficient_history"})
                continue
            window = sopr_records[index - SOPR_SMA_PERIOD_DAYS + 1:index + 1]
            contiguous = all(int(window[position]["timestamp"]) - int(window[position - 1]["timestamp"]) == SECONDS_PER_DAY
                             for position in range(1, len(window)))
            if not contiguous:
                unavailable.append({"timestamp": timestamp, "status": "unavailable", "reason": "non_contiguous_window"})
                continue
            values = [_finite(item.get("sopr", item.get("value"))) for item in window]
            value  = _finite(sum(values) / SOPR_SMA_PERIOD_DAYS)
            records.append({"timestamp": timestamp, "value": 0.0 if value == 0.0 else value, "unit": "ratio", "period_days": SOPR_SMA_PERIOD_DAYS,
                            "observations": SOPR_SMA_PERIOD_DAYS, "window_start_timestamp": int(window[0]["timestamp"]), "window_end_timestamp": timestamp,
                            "source_metric_id": "sopr", "calculation": "simple_moving_average"})
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"record[{index}]: {exc}")
    has_gap = any(item["reason"] == "non_contiguous_window" for item in unavailable)
    return _series_payload(metric_id="sopr_7d", unit="ratio", source_count=len(sopr_records), records=records, unavailable=unavailable,
                           warnings=(["sopr_7d_non_contiguous_history"] if has_gap else []) + (["sopr_7d_insufficient_history"] if not records else []),
                           errors=errors, force_invalid=bool(errors), partial_reasons=has_gap)


def _build_conversion_series(source_records: Sequence[Mapping[str, Any]], *, metric_id: str, unit: str, source_unit: str, conversion: str,
                             scale: int, source_metric_id: str) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, record in enumerate(source_records):
        try:
            source_value = _finite(record["value"])
            if source_value < 0:
                raise ValueError("negative_source_value")
            records.append({"timestamp": int(record["timestamp"]), "value": source_value / scale, "unit": unit, "source_value": source_value,
                            "source_unit": source_unit, "conversion": conversion, "conversion_scale": float(scale), "source_metric_id": source_metric_id})
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"record[{index}]: {exc}")
    return _series_payload(metric_id=metric_id, unit=unit, source_count=len(source_records), records=records, unavailable=[], errors=errors, force_invalid=bool(errors))


def build_hashrate_eh_s_series(hashrate_records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return _build_conversion_series(hashrate_records, metric_id="hashrate_eh_s", unit="EH/s", source_unit="H/s", conversion="H/s_to_EH/s",
                                    scale=HASHES_PER_EXAHASH, source_metric_id="hashrate")


def build_difficulty_trillion_series(difficulty_records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return _build_conversion_series(difficulty_records, metric_id="difficulty_t", unit="T", source_unit="provider_native_difficulty",
                                    conversion="native_difficulty_to_trillion", scale=DIFFICULTY_PER_TRILLION, source_metric_id="difficulty")


def build_miner_net_position_change_series(reserve_records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, record in enumerate(reserve_records):
        try:
            timestamp = int(record["timestamp"])
            current   = _finite(record["value"])
            if index == 0:
                unavailable.append({"timestamp": timestamp, "status": "unavailable", "reason": "insufficient_previous_record"})
                continue
            previous = reserve_records[index - 1]
            if timestamp - int(previous["timestamp"]) != SECONDS_PER_DAY:
                unavailable.append({"timestamp": timestamp, "status": "unavailable", "reason": "previous_day_missing"})
                continue
            previous_value = _finite(previous["value"])
            value = _finite(current - previous_value)
            records.append({"timestamp": timestamp, "value": 0.0 if value == 0.0 else value, "unit": "BTC/day", "current_reserve_btc": current,
                            "previous_reserve_btc": previous_value, "previous_timestamp": int(previous["timestamp"]), "period_seconds": SECONDS_PER_DAY,
                            "source_metric_id": "miner_reserve", "calculation": "daily_first_difference"})
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"record[{index}]: {exc}")
    has_gap = any(item["reason"] == "previous_day_missing" for item in unavailable)
    return _series_payload(metric_id="miner_net_position_change", unit="BTC/day", source_count=len(reserve_records), records=records,
                           unavailable=unavailable, warnings=["non_contiguous_daily_history"] if has_gap else [], errors=errors,
                           force_invalid=bool(errors), partial_reasons=has_gap)


def _linear_regression(records: Sequence[Mapping[str, Any]]) -> dict[str, float | int | None]:
    first_timestamp = int(records[0]["timestamp"])
    x = [(int(record["timestamp"]) - first_timestamp) / SECONDS_PER_DAY for record in records]
    y = [_finite(record["value"]) for record in records]
    mean_x, mean_y = sum(x) / len(x), sum(y) / len(y)
    denominator = sum((value - mean_x) ** 2 for value in x)
    if denominator == 0:
        raise ValueError("regression_x_variance_zero")
    slope     = sum((x_value - mean_x) * (y_value - mean_y) for x_value, y_value in zip(x, y)) / denominator
    intercept = mean_y - slope * mean_x
    total     = sum((value - mean_y) ** 2 for value in y)
    residual  = sum((y_value - (intercept + slope * x_value)) ** 2 for x_value, y_value in zip(x, y))
    r_squared = 1.0 if total == 0 else 1.0 - residual / total
    first_value, last_value = y[0], y[-1]
    result = {"slope_btc_per_day": 0.0 if slope == 0.0 else slope, "normalized_slope_percent_per_day": None if mean_y == 0 else 100 * slope / mean_y,
            "intercept_btc": intercept, "r_squared": r_squared, "mean_reserve_btc": mean_y, "net_change_btc": last_value - first_value,
            "percent_change": None if first_value == 0 else 100 * (last_value - first_value) / abs(first_value), "first_timestamp": first_timestamp,
            "last_timestamp": int(records[-1]["timestamp"]), "first_value_btc": first_value, "last_value_btc": last_value}
    for key, value in result.items():
        if isinstance(value, float):
            result[key] = _finite(value)
    return result


def build_reserve_trend_features(reserve_records: Sequence[Mapping[str, Any]], *, window_days: Sequence[int] = RESERVE_TREND_WINDOWS_DAYS,
                                 default_window_days: int = DEFAULT_RESERVE_TREND_DAYS) -> dict[str, Any]:
    windows: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    errors: list[str] = []
    current_timestamp = int(reserve_records[-1]["timestamp"]) if reserve_records else None
    for days in window_days:
        if not isinstance(days, int) or days <= 0:
            errors.append(f"invalid_window_days:{days}")
            continue
        theoretical_start = current_timestamp - (days - 1) * SECONDS_PER_DAY if current_timestamp is not None else None
        selected = [record for record in reserve_records if theoretical_start is not None and theoretical_start <= int(record["timestamp"]) <= current_timestamp]
        observations = len(selected)
        first = int(selected[0]["timestamp"]) if selected else None
        last  = int(selected[-1]["timestamp"]) if selected else None
        leading_missing  = max(0, (first - theoretical_start) // SECONDS_PER_DAY) if first is not None and theoretical_start is not None else days
        trailing_missing = max(0, (current_timestamp - last) // SECONDS_PER_DAY) if last is not None and current_timestamp is not None else 0
        span_days        = ((last - first) // SECONDS_PER_DAY) + 1 if first is not None and last is not None else 0
        internal_missing = max(0, span_days - observations)
        total_missing    = leading_missing + trailing_missing + internal_missing
        window_warnings: list[str] = []
        window_errors: list[str] = []
        window = {"window_days": days, "observations": observations, "expected_observations": days, "coverage_ratio": min(observations / days, 1.0),
                  "history_complete": False, "theoretical_start_timestamp": theoretical_start, "theoretical_end_timestamp": current_timestamp,
                  "first_timestamp": first, "last_timestamp": last, "leading_missing_days": leading_missing, "trailing_missing_days": trailing_missing,
                  "internal_missing_days": internal_missing, "total_missing_days": total_missing, "span_calendar_days": span_days,
                  "warnings": window_warnings, "errors": window_errors}
        if observations >= 3:
            try:
                window.update(_linear_regression(selected))
                window["history_complete"] = total_missing <= DAILY_WINDOW_TOLERANCE_DAYS
                if window["history_complete"]:
                    window["status"] = "available"
                else:
                    window["status"] = "partial"
                    window_warnings.append("reserve_trend_window_incomplete")
            except (ArithmeticError, KeyError, TypeError, ValueError) as exc:
                reason = "regression_x_variance_zero" if str(exc) == "regression_x_variance_zero" else "non_finite_regression_result"
                window.update({"status": "invalid", "slope_btc_per_day": None, "normalized_slope_percent_per_day": None, "intercept_btc": None,
                               "r_squared": None, "mean_reserve_btc": None, "net_change_btc": None, "percent_change": None})
                window_errors.append(reason)
        else:
            window.update({"status": "unavailable", "slope_btc_per_day": None, "normalized_slope_percent_per_day": None, "intercept_btc": None,
                           "r_squared": None, "mean_reserve_btc": None, "net_change_btc": None, "percent_change": None,
                           "first_timestamp": first, "last_timestamp": last})
            window_warnings.append("insufficient_observations_for_regression")
        windows[f"{days}d"] = window
    default = windows.get(f"{default_window_days}d", {"status": "invalid"})
    status  = str(default["status"])
    warnings.extend(default.get("warnings", []))
    errors.extend(default.get("errors", []))
    return {"feature_id": "reserve_trend", "status": status, "default_window_days": default_window_days, "windows": windows,
            "warnings": _stable_unique(warnings), "errors": _stable_unique(errors)}


def _mpi_basis(mpi_series: Mapping[str, Any]) -> dict[str, Any]:
    records  = mpi_series.get("records", [])
    current  = build_current_snapshot(mpi_series)
    previous = None
    change   = None
    if len(records) >= 2 and int(records[-1]["timestamp"]) - int(records[-2]["timestamp"]) == SECONDS_PER_DAY:
        previous = {"status": "available", "timestamp": int(records[-2]["timestamp"]), "value": _finite(records[-2]["value"]), "unit": "z_score"}
        change   = _finite(_finite(records[-1]["value"]) - _finite(records[-2]["value"]))
    return {"source_metric_id": "mpi", "current": current, "previous": previous, "change_1d": change, "unit": "z_score"}


def build_on_chain_miners_features(input_series: Mapping[str, Any], *, input_data_as_of: int | None = None) -> dict[str, Any]:
    """Build only the final VR1 On-Chain Processing surface.

    External acquisition is limited to the seven frozen primitives.  Net-position
    change and Puell are derived later; no retired endpoint-dependent drilldowns
    are constructed here.
    """
    reserve_source = _daily_close_source(input_series["miner_reserve"], metric_id="miner_reserve")
    sopr_source = _daily_close_source(input_series["sopr"], metric_id="sopr")
    hashrate_source = _daily_close_source(input_series["hashrate"], metric_id="hashrate")
    difficulty_source = _daily_close_source(input_series["difficulty"], metric_id="difficulty")

    reserve = _copy_base_series(reserve_source, metric_id="miner_reserve_btc", unit="BTC", transform=_reserve_record)
    sopr = _copy_base_series(sopr_source, metric_id="sopr", unit="ratio", transform=_sopr_record)
    mpi = _copy_base_series(input_series["mpi"], metric_id="mpi", unit="z_score", transform=_mpi_record)

    def derived_from(source: Mapping[str, Any], source_id: str, metric_id: str, unit: str,
                     builder: Callable[[Sequence[Mapping[str, Any]]], dict[str, Any]]) -> dict[str, Any]:
        if source["status"] in {"invalid", "unavailable"}:
            payload = _blocked_source_series(metric_id=metric_id, unit=unit, source_metric_id=source_id, source_status=str(source["status"]))
        else:
            payload = builder(source["records"])
        return apply_source_series_context(payload, source, source_id)

    sopr_7d = derived_from(sopr_source, "sopr", "sopr_7d", "ratio", build_sopr_7d_series)
    hashrate = derived_from(hashrate_source, "hashrate", "hashrate_eh_s", "EH/s", build_hashrate_eh_s_series)
    difficulty = derived_from(difficulty_source, "difficulty", "difficulty_t", "T", build_difficulty_trillion_series)
    net_position = _copy_direct_value_series(input_series["miner_net_position_change"], metric_id="miner_net_position_change",
                                             unit="BTC/day", source_metric_id="miner_net_position_change")
    outflow = _copy_direct_value_series(input_series["miner_outflow_total"], metric_id="miner_outflow_total_btc",
                                        unit="BTC/day", source_metric_id="miner_outflow_total")
    revenue = _copy_direct_value_series(input_series["miner_revenue_total_usd"], metric_id="miner_revenue_total_usd",
                                        unit="USD/day", source_metric_id="miner_revenue_total_usd")

    if reserve_source["status"] == "invalid":
        reserve_trend = {"feature_id": "reserve_trend", "status": "invalid", "default_window_days": DEFAULT_RESERVE_TREND_DAYS,
                         "windows": {}, "warnings": [], "errors": ["source_series_invalid:miner_reserve"]}
    elif reserve_source["status"] == "unavailable":
        reserve_trend = {"feature_id": "reserve_trend", "status": "unavailable", "default_window_days": DEFAULT_RESERVE_TREND_DAYS,
                         "windows": {}, "warnings": ["source_series_unavailable:miner_reserve"], "errors": []}
    else:
        reserve_trend = build_reserve_trend_features(reserve["records"])
        if reserve_source["status"] == "partial" and reserve_trend["status"] == "available":
            reserve_trend["status"] = "partial"
        reserve_trend["warnings"] = _stable_unique([*reserve_trend["warnings"],
            *(["source_series_partial:miner_reserve"] if reserve_source["status"] == "partial" else []),
            *(f"input_series_warning:miner_reserve:{message}" for message in reserve_source.get("warnings", []))])
        reserve_trend["errors"] = _stable_unique([*reserve_trend["errors"],
            *(f"input_series_error:miner_reserve:{message}" for message in reserve_source.get("errors", []))])

    daily_candles = {
        "miner_reserve": build_daily_ohlc_from_intraday(input_series["miner_reserve"].get("records", []), unit="BTC"),
        "sopr_7d": build_daily_ohlc_from_intraday(sopr_7d.get("records", []), unit="ratio"),
        "hashrate": build_daily_ohlc_from_intraday(input_series["hashrate"].get("records", []), unit="EH/s", value_transform=lambda v: v / HASHES_PER_EXAHASH),
        "difficulty": build_daily_ohlc_from_intraday(input_series["difficulty"].get("records", []), unit="T", value_transform=lambda v: v / DIFFICULTY_PER_TRILLION),
    }
    series = {
        "miner_reserve_btc": reserve, "sopr": sopr, "sopr_7d": sopr_7d, "hashrate_eh_s": hashrate,
        "difficulty_t": difficulty, "miner_net_position_change": net_position, "mpi": mpi,
        "miner_outflow_total_btc": outflow, "miner_revenue_total_usd": revenue,
    }
    for chart_id, series_id in (("miner_reserve", "miner_reserve_btc"), ("sopr_7d", "sopr_7d"),
                                ("hashrate", "hashrate_eh_s"), ("difficulty", "difficulty_t")):
        series[series_id]["daily_candles"] = daily_candles[chart_id]
        series[series_id]["metadata"] = {**dict(series[series_id].get("metadata", {})),
            "daily_ohlc_source": "glassnode_24h", "daily_ohlc_candles": len(daily_candles[chart_id]),
            "ohlc_origin": "processing_derived"}

    # Classification pressure state remains anchored to MPI (z-score); the
    # richer native selling-pressure analysis below also consumes total outflow.
    pressure_basis = (
        {"source_metric_id": "mpi", "status": mpi["status"], "current": mpi["current"],
         "previous": None, "change_1d": None, "unit": "z_score"}
        if mpi["status"] in {"invalid", "unavailable"}
        else _mpi_basis(mpi)
    )
    features = {
        "reserve_trend": reserve_trend,
        "miner_pressure_basis": pressure_basis,
        "mpi_context": _mpi_basis(mpi) if mpi.get("status") not in {"invalid", "unavailable"} else {"status": mpi.get("status"), "current": mpi.get("current")},
        "sopr_regime_basis": {"source_metric_id": "sopr_7d", "status": sopr_7d["status"], "current": sopr_7d["current"], "raw_sopr_current": sopr["current"]},
        "net_position_basis": {"source_metric_id": "miner_net_position_change", "status": net_position["status"], "current": net_position["current"]},
    }
    return {"series": series, "features": features}

