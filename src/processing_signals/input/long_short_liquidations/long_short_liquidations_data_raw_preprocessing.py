"""Validation and normalization for long/short liquidations Raw bundles."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from typing import Any

from .long_short_liquidations_data_raw_extract import (
    DEFAULT_ASSET,
    DEFAULT_EXCHANGES,
    DEFAULT_EXCHANGE_RANGE,
    DEFAULT_HISTORY_HOURS,
    DEFAULT_INCREMENTAL_OVERLAP_H,
    DEFAULT_EVENT_LOOKBACK_H,
    DEFAULT_EVENT_OVERLAP_MINUTES,
    DEFAULT_MAP_RANGE,
    DEFAULT_MAX_PAIN_RANGE,
    DEFAULT_MIN_EVENT_USD,
    ENDPOINT_MANIFEST,
    COINGLASS_GLOBAL_ACCOUNT_ENDPOINT_ID,
    POSITIONING_TIMEFRAMES,
    PUBLIC_MAP_RANGES,
    LONG_SHORT_LIQUIDATIONS_FAMILY,
    VALID_MODES,
    LongShortLiquidationsRawExtractor,
    RawFetcher,
    validate_request_contract,
)

DATASET_STATES = {"available", "partial", "unavailable", "invalid"}
RAW_STATES = {"ok", "error", "skipped"}


def copy_json_safe_value(value: Any, *, path: str = "value") -> Any:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"json_value_not_finite:{path}")
        return value
    if isinstance(value, (list, tuple)):
        return [copy_json_safe_value(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"json_key_not_string:{path}")
            result[key] = copy_json_safe_value(item, path=f"{path}.{key}")
        return result
    raise ValueError(f"json_value_unsupported:{path}:{type(value).__name__}")


def _timestamp(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("timestamp_must_be_positive_integer")
    return value


def _finite(value: Any, *, positive: bool = False, nonnegative: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean_is_not_numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("value_must_be_numeric") from exc
    if not math.isfinite(result):
        raise ValueError("value_must_be_finite")
    if positive and result <= 0:
        raise ValueError("value_must_be_positive")
    if nonnegative and result < 0:
        raise ValueError("value_must_be_nonnegative")
    return result


def _optional_finite(value: Any, **rules: Any) -> float | None:
    return None if value is None else _finite(value, **rules)


def milliseconds_to_seconds(value: Any) -> int:
    raw = _timestamp(value)
    return raw // 1000


def parse_cryptoquant_utc_date(value: Any) -> int:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("date_must_be_string")
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return _timestamp(int(parsed.astimezone(timezone.utc).timestamp()))


def normalize_exchange_key(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("exchange_must_be_string")
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def unwrap_coinglass_list_response(response: Any) -> list[Any]:
    if not isinstance(response, Mapping):
        raise ValueError("coinglass_response_must_be_mapping")
    if response.get("code") not in (0, "0"):
        raise ValueError(f"coinglass_error:{response.get('msg') or response.get('code')}")
    if "data" not in response or not isinstance(response["data"], list):
        raise ValueError("coinglass_data_must_be_list")
    return response["data"]


def unwrap_coinglass_map_response(response: Any) -> Mapping[str, Any]:
    if not isinstance(response, Mapping):
        raise ValueError("coinglass_response_must_be_mapping")
    if response.get("code") not in (0, "0"):
        raise ValueError(f"coinglass_error:{response.get('msg') or response.get('code')}")
    outer = response.get("data")
    if not isinstance(outer, Mapping) or not isinstance(outer.get("data"), Mapping):
        raise ValueError("coinglass_map_data_must_be_mapping")
    return outer["data"]


def unwrap_glassnode_metric(response: Any) -> list[Mapping[str, Any]]:
    if not isinstance(response, list):
        raise ValueError("glassnode_response_must_be_list")
    for record in response:
        if not isinstance(record, Mapping) or "t" not in record or "v" not in record:
            raise ValueError("glassnode_observation_invalid")
    return response


def normalize_coinglass_aggregated_history_record(record: Mapping[str, Any]) -> dict[str, Any]:
    return {"timestamp": milliseconds_to_seconds(record.get("time")),
            "long_liquidation_usd": _finite(record.get("aggregated_long_liquidation_usd"), nonnegative=True),
            "short_liquidation_usd": _finite(record.get("aggregated_short_liquidation_usd"), nonnegative=True)}


def normalize_coinglass_positioning_record(record: Mapping[str, Any], *, kind: str) -> dict[str, Any]:
    prefixes = {
        "top_position": "top_position",
        "top_account": "top_account",
        "global_account": "global_account",
    }
    prefix = prefixes[kind]
    long_percent = _finite(record.get(f"{prefix}_long_percent"), nonnegative=True)
    short_percent = _finite(record.get(f"{prefix}_short_percent"), nonnegative=True)
    ratio = _finite(record.get(f"{prefix}_long_short_ratio"), positive=True)
    if long_percent > 100 or short_percent > 100 or abs(long_percent + short_percent - 100.0) > 0.5:
        raise ValueError("inconsistent_positioning_percentages")
    expected = long_percent / short_percent if short_percent else None
    if expected is None or abs(expected - ratio) > 0.05:
        raise ValueError("inconsistent_positioning_ratio")
    return {
        "timestamp": milliseconds_to_seconds(record.get("time")),
        "long_percent": long_percent, "short_percent": short_percent, "long_short_ratio": ratio,
    }


def normalize_coinglass_exchange_snapshot_record(record: Mapping[str, Any]) -> dict[str, Any]:
    exchange = record.get("exchange")
    return {"exchange": exchange, "exchange_key": normalize_exchange_key(exchange),
            "liquidation_usd": _finite(record.get("liquidation_usd"), nonnegative=True),
            "long_liquidation_usd": _finite(record.get("long_liquidation_usd"), nonnegative=True),
            "short_liquidation_usd": _finite(record.get("short_liquidation_usd"), nonnegative=True)}


def normalize_coinglass_pair_history_record(record: Mapping[str, Any]) -> dict[str, Any]:
    long_value = record.get("long_liquidation_usd", record.get("longLiquidationUsd"))
    short_value = record.get("short_liquidation_usd", record.get("shortLiquidationUsd"))
    return {"timestamp": milliseconds_to_seconds(record.get("time")),
            "long_liquidation_usd": _finite(long_value, nonnegative=True),
            "short_liquidation_usd": _finite(short_value, nonnegative=True)}


def normalize_coinglass_liquidation_event(record: Mapping[str, Any], *, expected_asset: str | None = None) -> dict[str, Any]:
    exchange = record.get("exchange_name", record.get("exchangeName", record.get("exchange")))
    symbol = record.get("symbol")
    side = record.get("side")
    if not isinstance(side, int) or isinstance(side, bool) or side not in (1, 2):
        raise ValueError("side_must_be_integer_1_or_2")
    if not isinstance(symbol, str) or not symbol:
        raise ValueError("symbol_must_be_string")
    price = _finite(record.get("price"), positive=True)
    usd_value = _finite(record.get("usd_value", record.get("usdValue")), nonnegative=True)
    raw_time = record.get("time")
    timestamp = milliseconds_to_seconds(raw_time)
    base_asset = record.get("base_asset")
    if not isinstance(base_asset, str) or not base_asset:
        raise ValueError("base_asset_must_be_non_empty_string")
    if expected_asset is not None and base_asset != expected_asset:
        raise ValueError("base_asset_mismatch")
    canonical = json.dumps(["coinglass", exchange, symbol, raw_time, price, usd_value, side], separators=(",", ":"), ensure_ascii=False)
    return {"event_id": hashlib.sha256(canonical.encode()).hexdigest(), "timestamp": timestamp,
            "exchange": exchange, "exchange_key": normalize_exchange_key(exchange), "symbol": symbol,
            "base_asset": base_asset,
            "price": price, "usd_value": usd_value,
            "raw_side": side, "order_side": "buy" if side == 1 else "sell"}


def _normalize_map_row(raw_price_key: Any, row: Any, *, pair: bool) -> tuple[dict[str, Any], list[str]]:
    if not isinstance(raw_price_key, str) or not isinstance(row, list) or len(row) < 2:
        raise ValueError("map_row_invalid")
    key_price = _finite(raw_price_key, positive=True)
    row_price = _finite(row[0], positive=True)
    warnings = [] if math.isclose(key_price, row_price) else ["map_price_key_mismatch"]
    leverage = _optional_finite(row[2], nonnegative=True) if pair and len(row) > 2 else None
    return {"price_level": row_price, "provider_liquidation_level": _finite(row[1], nonnegative=True),
            "leverage_ratio": leverage,
            "provider_reserved": copy_json_safe_value(row[3], path="provider_reserved") if len(row) > 3 else None,
            "raw_price_key": raw_price_key}, warnings


def normalize_coinglass_aggregated_map_row(raw_price_key: Any, row: Any) -> dict[str, Any]:
    return _normalize_map_row(raw_price_key, row, pair=False)[0]


def normalize_coinglass_pair_map_row(raw_price_key: Any, row: Any) -> dict[str, Any]:
    return _normalize_map_row(raw_price_key, row, pair=True)[0]


def normalize_coinglass_max_pain_record(record: Mapping[str, Any]) -> dict[str, Any]:
    symbol = record.get("symbol")
    if not isinstance(symbol, str):
        raise ValueError("symbol_must_be_string")
    return {"symbol": symbol, "provider_price": _finite(record.get("price"), positive=True),
            "long_max_pain_liquidation_level": _finite(record.get("long_max_pain_liq_level", record.get("longMaxPainLiquidationLevel")), nonnegative=True),
            "long_max_pain_liquidation_price": _finite(record.get("long_max_pain_liq_price", record.get("longMaxPainLiquidationPrice")), positive=True),
            "short_max_pain_liquidation_level": _finite(record.get("short_max_pain_liq_level", record.get("shortMaxPainLiquidationLevel")), nonnegative=True),
            "short_max_pain_liquidation_price": _finite(record.get("short_max_pain_liq_price", record.get("shortMaxPainLiquidationPrice")), positive=True)}


def normalize_cryptoquant_liquidation_record(record: Mapping[str, Any]) -> dict[str, Any]:
    return {"timestamp": parse_cryptoquant_utc_date(record.get("date")),
            "long_liquidations_asset": _optional_finite(record.get("long_liquidations"), nonnegative=True),
            "short_liquidations_asset": _optional_finite(record.get("short_liquidations"), nonnegative=True),
            "long_liquidations_usd": _optional_finite(record.get("long_liquidations_usd"), nonnegative=True),
            "short_liquidations_usd": _optional_finite(record.get("short_liquidations_usd"), nonnegative=True)}


def normalize_glassnode_metric_record(record: Mapping[str, Any]) -> dict[str, Any]:
    return {"timestamp": _timestamp(record.get("t")), "value": _optional_finite(record.get("v"), nonnegative=True)}


def upsert_records_by_timestamp(existing: Sequence[Mapping[str, Any]], incoming: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    merged = {record["timestamp"]: deepcopy(dict(record)) for record in existing}
    merged.update({record["timestamp"]: deepcopy(dict(record)) for record in incoming})
    return [merged[key] for key in sorted(merged)]


def _merge_events(existing: Sequence[Mapping[str, Any]], incoming: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    merged = {record["event_id"]: deepcopy(dict(record)) for record in existing}
    merged.update({record["event_id"]: deepcopy(dict(record)) for record in incoming})
    return sorted(merged.values(), key=lambda item: (item["timestamp"], item["event_id"]))


def _gaps(records: Sequence[Mapping[str, Any]], seconds: int = 3600) -> list[dict[str, int]]:
    gaps = []
    for previous, current in zip(records, records[1:]):
        difference = current["timestamp"] - previous["timestamp"]
        if difference > seconds:
            gaps.append({"start_timestamp": previous["timestamp"] + seconds,
                         "end_timestamp": current["timestamp"] - seconds,
                         "missing_intervals": difference // seconds - 1})
    return gaps


def _provenance(requests: Sequence[Mapping[str, Any]], raw: Mapping[str, Any],
                existing: Mapping[str, Any] | None = None) -> dict[str, Any]:
    first = requests[0]
    previous = existing.get("provenance", {}) if existing else {}
    request_ids = list(previous.get("request_ids", [])) + [item["request_id"] for item in requests]
    coverage_intervals = list(previous.get("coverage_intervals", []))
    for request in requests:
        params = request.get("params", {})
        start, end = params.get("start_time"), params.get("end_time")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
            coverage_intervals.append({
                "start": int(start / 1000 if start > 10**11 else start),
                "end": int(end / 1000 if end > 10**11 else end),
                "status": "complete" if request.get("status") == "ok" and
                          "event_endpoint_record_limit_reached" not in request.get("warnings", []) else "incomplete",
            })
    return {"provider": first["provider"], "endpoint_id": first["endpoint_id"],
            "path": first["path"], "params": deepcopy(requests[-1]["params"]),
            "request_ids": list(dict.fromkeys(request_ids)),
            "coverage_intervals": coverage_intervals,
            "reference_timestamp": raw["reference_timestamp"],
            "execution_timestamp": raw["execution_timestamp"]}


def _latest_attempt(requests: Sequence[Mapping[str, Any]], raw: Mapping[str, Any],
                    errors: Sequence[str], *, invalid_record_count: int = 0,
                    warnings: Sequence[str] = ()) -> dict[str, Any]:
    return {"request_ids": [item["request_id"] for item in requests],
            "reference_timestamp": raw["reference_timestamp"],
            "execution_timestamp": raw["execution_timestamp"],
            "invalid_record_count": invalid_record_count,
            "warnings": list(warnings), "errors": list(errors)}


def _old(existing: Mapping[str, Any] | None, *path: str) -> Mapping[str, Any] | None:
    value: Any = existing
    for part in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value if isinstance(value, Mapping) else None


def determine_record_availability(
    *, received_count: int, valid_count: int, invalid_count: int,
    request_failed: bool, envelope_invalid: bool, has_existing_data: bool,
    dataset_kind: str,
) -> tuple[str, str | None]:
    prefix = "snapshot" if dataset_kind == "snapshot" else "history"
    if request_failed and valid_count == 0 and received_count == 0:
        return (("partial", f"latest_{prefix}_request_failed") if has_existing_data else
                ("unavailable", "request_failed"))
    if envelope_invalid and valid_count == 0:
        return (("partial", f"latest_{prefix}_invalid") if has_existing_data else
                ("invalid", "invalid_response_envelope"))
    if received_count == 0:
        return (("partial", f"latest_{prefix}_empty_response") if has_existing_data else
                ("unavailable", "empty_response"))
    if valid_count == 0 and invalid_count > 0:
        return (("partial", f"latest_{prefix}_invalid") if has_existing_data else
                ("invalid", "all_records_invalid"))
    if valid_count > 0 and (invalid_count > 0 or request_failed or envelope_invalid):
        return "partial", "some_records_invalid" if invalid_count else "some_requests_failed"
    return "available", None


def _dataset(requests: Sequence[Mapping[str, Any]], raw: Mapping[str, Any], parser: Callable[[Any], Any],
             normalizer: Callable[[Mapping[str, Any]], dict[str, Any]], *, existing: Mapping[str, Any] | None = None,
             interval: str | None = None, interval_seconds: int = 3600, events: bool = False,
             cryptoquant: bool = False) -> dict[str, Any]:
    if not requests:
        if existing is not None:
            return deepcopy(dict(existing))
        return {"status": "unavailable", "reason": "not_requested_in_mode",
                **({"interval": interval} if interval else {}), "incoming_records": [], "records": [],
                "gaps": [], "source_data_as_of": None, "provenance": {}, "warnings": [], "errors": []}
    if all(request["status"] == "skipped" for request in requests):
        reason = requests[0]["warnings"][0] if requests[0]["warnings"] else "request_skipped"
        return {"status": "unavailable", "reason": reason, **({"interval": interval} if interval else {}),
                "incoming_records": [], "records": [], "gaps": [], "source_data_as_of": None,
                "provenance": _provenance(requests, raw),
                "warnings": list(dict.fromkeys(requests[0]["warnings"])), "errors": []}
    incoming, warnings, errors = [], [], []
    received_count = invalid_count = 0
    request_failed = envelope_invalid = False
    detected_interval = interval
    detected_seconds = interval_seconds
    detected_window = None
    for request in requests:
        warnings.extend(request.get("warnings", []))
        if request.get("status") != "ok":
            request_failed = True
            if request.get("error"):
                errors.append(request["error"])
            continue
        try:
            rows = parser(request.get("response"))
            if cryptoquant:
                response_window, rows = rows
                requested_window = request["params"].get("window")
                if requested_window is not None and requested_window != response_window:
                    raise ValueError("response_window_mismatch")
                detected_window = response_window
                detected_interval, detected_seconds = CRYPTOQUANT_WINDOWS[response_window]
        except ValueError as exc:
            envelope_invalid = True
            errors.append(str(exc))
            continue
        received_count += len(rows)
        for index, record in enumerate(rows):
            try:
                if not isinstance(record, Mapping):
                    raise ValueError("record_must_be_mapping")
                incoming.append(normalizer(record))
            except (KeyError, TypeError, ValueError) as exc:
                invalid_count += 1
                warnings.append(f"invalid_record:{index}:{exc}")
    prior = list(existing.get("records", [])) if existing else []
    records = _merge_events(prior, incoming) if events else upsert_records_by_timestamp(prior, incoming)
    gaps = [] if events else _gaps(records, detected_seconds)
    if gaps:
        warnings.append("source_history_has_gaps")
    has_existing = bool(existing and existing.get("records"))
    status, reason = determine_record_availability(
        received_count=received_count, valid_count=len(incoming), invalid_count=invalid_count,
        request_failed=request_failed, envelope_invalid=envelope_invalid,
        has_existing_data=has_existing, dataset_kind="history",
    )
    if "response_window_mismatch" in errors:
        reason = "response_window_mismatch"
    degrading_warnings = [warning for warning in warnings if warning != "event_snapshot_fixed_limit_no_recursive_pagination"]
    if gaps and status == "available":
        status, reason = "partial", "source_history_has_gaps"
    elif degrading_warnings and status == "available":
        status, reason = "partial", "source_warnings"
    if status == "partial" and not incoming and has_existing:
        result = deepcopy(dict(existing))
        result["status"], result["reason"] = status, reason
        result["warnings"] = list(dict.fromkeys(list(result.get("warnings", [])) + warnings))
        result["errors"] = list(dict.fromkeys(list(result.get("errors", [])) + errors))
        result["latest_attempt"] = _latest_attempt(
            requests, raw, errors, invalid_record_count=invalid_count, warnings=warnings,
        )
        return result
    return {"status": status, "reason": reason,
            **({"window": detected_window} if detected_window else {}),
            **({"interval": detected_interval} if detected_interval else {}),
            **({"interval_seconds": detected_seconds} if cryptoquant else {}), "incoming_records": incoming,
            "records": records, "gaps": gaps, "source_data_as_of": records[-1]["timestamp"] if records else None,
            "provenance": _provenance(requests, raw, existing),
            "warnings": list(dict.fromkeys(warnings)), "errors": list(dict.fromkeys(errors))}


def _snapshot(requests: Sequence[Mapping[str, Any]], raw: Mapping[str, Any], parser: Callable[[Any], Any],
              normalizer: Callable[..., dict[str, Any]], *, existing: Mapping[str, Any] | None = None,
              key: str = "records", range_value: str | None = None, map_rows: bool = False) -> dict[str, Any]:
    if not requests:
        if existing is not None:
            return deepcopy(dict(existing))
        return {"status": "unavailable", "reason": "not_requested_in_mode",
                **({"range": range_value} if range_value else {}), "snapshot_observed_at": None,
                "source_data_as_of": None, key: [], "provenance": {}, "warnings": [], "errors": []}
    if all(request["status"] == "skipped" for request in requests):
        reason = requests[0]["warnings"][0] if requests[0]["warnings"] else "request_skipped"
        return {"status": "unavailable", "reason": reason,
                **({"range": range_value} if range_value else {}), "snapshot_observed_at": None,
                "source_data_as_of": None, key: [], "provenance": _provenance(requests, raw),
                "warnings": list(dict.fromkeys(requests[0]["warnings"])), "errors": []}
    warnings, errors, records = [], [], []
    received_count = invalid_count = 0
    request_failed = envelope_invalid = False
    for request in requests:
        warnings.extend(request.get("warnings", []))
        if request.get("status") != "ok":
            request_failed = True
            if request.get("error"):
                errors.append(request["error"])
            continue
        try:
            payload = parser(request.get("response"))
        except (KeyError, TypeError, ValueError) as exc:
            envelope_invalid = True
            errors.append(str(exc))
            continue
        iterable = payload.items() if map_rows else enumerate(payload)
        for raw_key, record in iterable:
            if map_rows:
                if not isinstance(record, list):
                    received_count += 1
                    invalid_count += 1
                    warnings.append(f"invalid_record:{raw_key}:map_price_rows_must_be_list")
                    continue
                for row in record:
                    received_count += 1
                    try:
                        normalized, row_warnings = _normalize_map_row(
                            raw_key, row, pair=normalizer is normalize_coinglass_pair_map_row,
                        )
                        records.append(normalized)
                        warnings.extend(row_warnings)
                    except (KeyError, TypeError, ValueError) as exc:
                        invalid_count += 1
                        warnings.append(f"invalid_record:{raw_key}:{exc}")
            else:
                received_count += 1
                try:
                    records.append(normalizer(record))
                except (KeyError, TypeError, ValueError) as exc:
                    invalid_count += 1
                    warnings.append(f"invalid_record:{raw_key}:{exc}")
    records.sort(key=lambda item: item.get("price_level", item.get("exchange_key", item.get("symbol", ""))))
    has_existing = bool(existing and existing.get(key))
    status, reason = determine_record_availability(
        received_count=received_count, valid_count=len(records), invalid_count=invalid_count,
        request_failed=request_failed, envelope_invalid=envelope_invalid,
        has_existing_data=has_existing, dataset_kind="snapshot",
    )
    if warnings and status == "available":
        status, reason = "partial", "source_warnings"
    if status == "partial" and not records and has_existing:
        result = deepcopy(dict(existing))
        result["status"], result["reason"] = status, reason
        result["warnings"] = list(dict.fromkeys(list(result.get("warnings", [])) + warnings))
        result["errors"] = list(dict.fromkeys(list(result.get("errors", [])) + errors))
        result["latest_attempt"] = _latest_attempt(
            requests, raw, errors, invalid_record_count=invalid_count, warnings=warnings,
        )
        return result
    return {"status": status, "reason": reason,
            **({"range": range_value} if range_value else {}),
            "snapshot_observed_at": raw.get("execution_timestamp") if records else None,
            "source_data_as_of": None, key: records, "provenance": _provenance(requests, raw),
            "warnings": list(dict.fromkeys(warnings)), "errors": list(dict.fromkeys(errors))}


def determine_long_short_liquidations_input_mode(*, requested_mode: str | None = None,
                                                  recovery_requests: Sequence[Mapping[str, Any]] | None = None,
                                                  existing_contract: Mapping[str, Any] | None = None) -> str:
    if requested_mode is not None:
        if requested_mode not in VALID_MODES:
            raise ValueError(f"unsupported_mode:{requested_mode}")
        return requested_mode
    if recovery_requests:
        return "recovery"
    primary = _old(existing_contract, "providers", "coinglass", "aggregated_history")
    return "incremental" if primary and primary.get("records") else "bootstrap"


def validate_long_short_liquidations_raw_bundle(raw_bundle: Any) -> None:
    if not isinstance(raw_bundle, Mapping):
        raise ValueError("raw_bundle_must_be_mapping")
    if raw_bundle.get("family") != LONG_SHORT_LIQUIDATIONS_FAMILY:
        raise ValueError("invalid_raw_family")
    if raw_bundle.get("stage") != "extracted_raw":
        raise ValueError("invalid_raw_stage")
    if raw_bundle.get("mode") not in VALID_MODES:
        raise ValueError("invalid_raw_mode")
    for field in ("reference_timestamp", "execution_timestamp"):
        value = raw_bundle.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"invalid_raw_{field}")
    requests = raw_bundle.get("requests")
    if not isinstance(requests, list):
        raise ValueError("raw_requests_must_be_list")
    if raw_bundle.get("mode") == "recovery" and not requests:
        raise ValueError("recovery_requests_required")
    for index, request in enumerate(requests):
        path = f"requests[{index}]"
        if not isinstance(request, Mapping):
            raise ValueError(f"raw_request_must_be_mapping:{path}")
        if not isinstance(request.get("request_id"), str) or not request["request_id"]:
            raise ValueError(f"invalid_request_id:{path}")
        provider, endpoint_id = request.get("provider"), request.get("endpoint_id")
        if (provider, endpoint_id) not in ENDPOINT_MANIFEST:
            raise ValueError(f"invalid_request_endpoint:{path}")
        if request.get("path") != ENDPOINT_MANIFEST[(provider, endpoint_id)]:
            raise ValueError(f"request_path_mismatch:{path}")
        if not isinstance(request.get("params"), Mapping):
            raise ValueError(f"request_params_must_be_mapping:{path}")
        if not isinstance(request.get("dimensions"), Mapping):
            raise ValueError(f"request_dimensions_must_be_mapping:{path}")
        copy_json_safe_value(request["dimensions"], path=f"{path}.dimensions")
        try:
            validate_request_contract(request, allow_skipped=True)
        except ValueError as exc:
            raise ValueError(f"{exc}:{path}") from exc
        status = request.get("status")
        if status not in RAW_STATES:
            raise ValueError(f"invalid_request_status:{path}")
        warnings = request.get("warnings")
        if not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings):
            raise ValueError(f"request_warnings_must_be_strings:{path}")
        if status == "ok" and request.get("response") is None:
            raise ValueError(f"ok_request_requires_response:{path}")
        if status == "error" and not isinstance(request.get("error"), str):
            raise ValueError(f"error_request_requires_error:{path}")
        if status == "skipped" and request.get("response") is not None:
            raise ValueError(f"skipped_request_requires_null_response:{path}")


def _request_dataset_path(request: Mapping[str, Any]) -> str:
    endpoint = request["endpoint_id"]
    exchange = request["dimensions"].get("exchange")
    direct = {
        "aggregated_liquidation_history": "coinglass.aggregated_history",
        "aggregated_liquidation_map": "coinglass.aggregated_map",
        COINGLASS_GLOBAL_ACCOUNT_ENDPOINT_ID: "coinglass.long_short_ratio",
    }
    if endpoint in direct:
        return direct[endpoint]
    keyed = {
        "liquidation_order_events": "coinglass.events",
        "pair_liquidation_map": "coinglass.pair_maps",
    }
    if endpoint in keyed and isinstance(exchange, str):
        return f"{keyed[endpoint]}.{exchange}"
    raise ValueError("unresolvable_dataset_target")


def determine_required_datasets(
    *, mode: str, raw_requests: Sequence[Mapping[str, Any]],
    existing_contract: Mapping[str, Any] | None = None,
) -> set[str]:
    del existing_contract
    if mode == "recovery":
        if not raw_requests:
            raise ValueError("recovery_requests_required")
        required = {_request_dataset_path(request) for request in raw_requests}
        if not required:
            raise ValueError("recovery_targets_unresolvable")
        return required
    required = set()
    if mode == "bootstrap":
        required.update({"coinglass.aggregated_history", "coinglass.aggregated_map"})
    required_endpoints = {"aggregated_liquidation_history",
                          "aggregated_liquidation_map", "pair_liquidation_map",
                          "liquidation_order_events",
                          COINGLASS_GLOBAL_ACCOUNT_ENDPOINT_ID}
    for request in raw_requests:
        endpoint = request["endpoint_id"]
        if endpoint in required_endpoints:
            required.add(_request_dataset_path(request))
    return required


class LongShortLiquidationsInputPreprocessor:
    def __init__(self, *, existing_contract: Mapping[str, Any] | None = None) -> None:
        self.existing_contract = deepcopy(existing_contract) if existing_contract is not None else None

    def determine_mode(self, *, requested_mode: str | None = None,
                       recovery_requests: Sequence[Mapping[str, Any]] | None = None) -> str:
        return determine_long_short_liquidations_input_mode(
            requested_mode=requested_mode, recovery_requests=recovery_requests,
            existing_contract=self.existing_contract,
        )

    def preprocess_raw(self, raw_bundle: Mapping[str, Any], *, debug_raw: bool = False) -> dict[str, Any]:
        validate_long_short_liquidations_raw_bundle(raw_bundle)
        raw = deepcopy(raw_bundle)
        requests = raw.get("requests")
        if not isinstance(requests, list):
            raise ValueError("raw_requests_must_be_list")
        grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
        for request in requests:
            if isinstance(request, Mapping):
                grouped.setdefault((request.get("provider"), request.get("endpoint_id")), []).append(request)
        def select(provider: str, endpoint: str) -> list[Mapping[str, Any]]:
            return grouped.get((provider, endpoint), [])
        coinglass_old = _old(self.existing_contract, "providers", "coinglass") or {}
        history = _dataset(select("coinglass", "aggregated_liquidation_history"), raw,
                           unwrap_coinglass_list_response, normalize_coinglass_aggregated_history_record,
                           existing=coinglass_old.get("aggregated_history"), interval="15m", interval_seconds=900)
        def positioning_by_exchange_timeframe(endpoint_id: str, kind: str, legacy_key: str) -> dict[str, Any]:
            previous_all = coinglass_old.get("positioning_by_exchange_timeframe", {}) \
                if isinstance(coinglass_old.get("positioning_by_exchange_timeframe"), Mapping) else {}
            requests_by_exchange_tf: dict[str, dict[str, list[Mapping[str, Any]]]] = {}
            for request in select("coinglass", endpoint_id):
                params = request.get("params", {}) if isinstance(request.get("params"), Mapping) else {}
                dimensions = request.get("dimensions", {}) if isinstance(request.get("dimensions"), Mapping) else {}
                exchange = dimensions.get("exchange") or params.get("exchange")
                interval = params.get("interval")
                if isinstance(exchange, str) and isinstance(interval, str):
                    requests_by_exchange_tf.setdefault(exchange, {}).setdefault(interval, []).append(request)
            result: dict[str, Any] = {}
            for exchange in DEFAULT_EXCHANGES:
                previous_exchange = previous_all.get(exchange, {}).get(legacy_key, {}) \
                    if isinstance(previous_all.get(exchange), Mapping) else {}
                result[exchange] = {}
                for timeframe in POSITIONING_TIMEFRAMES:
                    existing_tf = previous_exchange.get(timeframe) if isinstance(previous_exchange, Mapping) else None
                    if exchange == "Binance" and timeframe == "4h" and existing_tf is None:
                        legacy_tf = coinglass_old.get("positioning_by_timeframe", {}).get(legacy_key, {}) \
                            if isinstance(coinglass_old.get("positioning_by_timeframe"), Mapping) else {}
                        existing_tf = legacy_tf.get(timeframe) if isinstance(legacy_tf, Mapping) else coinglass_old.get(legacy_key)
                    result[exchange][timeframe] = _dataset(
                        requests_by_exchange_tf.get(exchange, {}).get(timeframe, []), raw, unwrap_coinglass_list_response,
                        lambda row, k=kind: normalize_coinglass_positioning_record(row, kind=k),
                        existing=existing_tf, interval=timeframe, interval_seconds={"1m":60,"5m":300,"15m":900,"4h":14400}[timeframe])
            return result

        positioning_exchange_tf = {
            "long_short_ratio": positioning_by_exchange_timeframe(COINGLASS_GLOBAL_ACCOUNT_ENDPOINT_ID, "global_account", "long_short_ratio"),
        }
        positioning_tf = {metric: {tf: positioning_exchange_tf[metric]["Binance"][tf] for tf in POSITIONING_TIMEFRAMES}
                          for metric in positioning_exchange_tf}
        long_short_ratio = positioning_tf["long_short_ratio"]["4h"]
        exchange_snapshot = {"status": "unavailable", "reason": "derived_from_event_stream_in_processing",
                             "records": [], "warnings": [], "errors": [], "provenance": {}}
        pair_history: dict[str, Any] = {}
        events = deepcopy(coinglass_old.get("events") or {})

        def keyed_requests(endpoint: str) -> dict[str, list[Mapping[str, Any]]]:
            result: dict[str, list[Mapping[str, Any]]] = {}
            for request in select("coinglass", endpoint):
                exchange = request["dimensions"].get("exchange")
                if isinstance(exchange, str):
                    result.setdefault(exchange, []).append(request)
            return result

        def range_requests(requests_in: Sequence[Mapping[str, Any]], range_value: str) -> list[Mapping[str, Any]]:
            return [request for request in requests_in
                    if isinstance(request.get("params"), Mapping) and request["params"].get("range") == range_value]

        for exchange, event_requests in keyed_requests("liquidation_order_events").items():
            expected_asset = event_requests[0]["dimensions"].get("asset") if event_requests else None
            events[exchange] = _dataset(event_requests, raw, unwrap_coinglass_list_response,
                                        lambda record: normalize_coinglass_liquidation_event(
                                            record, expected_asset=expected_asset,
                                        ),
                                        existing=(coinglass_old.get("events") or {}).get(exchange), events=True)

        previous_agg_ranges = coinglass_old.get("aggregated_maps_by_range", {}) \
            if isinstance(coinglass_old.get("aggregated_maps_by_range"), Mapping) else {}
        previous_pair_ranges = coinglass_old.get("pair_maps_by_range", {}) \
            if isinstance(coinglass_old.get("pair_maps_by_range"), Mapping) else {}
        if DEFAULT_MAP_RANGE not in previous_agg_ranges and isinstance(coinglass_old.get("aggregated_map"), Mapping):
            previous_agg_ranges = {**previous_agg_ranges, DEFAULT_MAP_RANGE: coinglass_old.get("aggregated_map")}
        if DEFAULT_MAP_RANGE not in previous_pair_ranges and isinstance(coinglass_old.get("pair_maps"), Mapping):
            previous_pair_ranges = {**previous_pair_ranges, DEFAULT_MAP_RANGE: coinglass_old.get("pair_maps")}

        pair_requests = keyed_requests("pair_liquidation_map")
        pair_maps_by_range: dict[str, dict[str, Any]] = {}
        for range_value in PUBLIC_MAP_RANGES:
            pair_maps_by_range[range_value] = {}
            previous_range = previous_pair_ranges.get(range_value, {}) \
                if isinstance(previous_pair_ranges.get(range_value), Mapping) else {}
            for exchange in (*DEFAULT_EXCHANGES, "Hyperliquid"):
                requests_for_exchange = range_requests(pair_requests.get(exchange, []), range_value)
                pair_maps_by_range[range_value][exchange] = _snapshot(
                    requests_for_exchange, raw, unwrap_coinglass_map_response, normalize_coinglass_pair_map_row,
                    existing=previous_range.get(exchange) if isinstance(previous_range, Mapping) else None,
                    key="levels", range_value=range_value, map_rows=True)

        aggregated_requests = select("coinglass", "aggregated_liquidation_map")
        aggregated_maps_by_range: dict[str, Any] = {}
        for range_value in PUBLIC_MAP_RANGES:
            aggregated_maps_by_range[range_value] = _snapshot(
                range_requests(aggregated_requests, range_value), raw,
                unwrap_coinglass_map_response, normalize_coinglass_aggregated_map_row,
                existing=previous_agg_ranges.get(range_value), key="levels",
                range_value=range_value, map_rows=True)

        # Backward-compatible aliases point at the public default (1d).
        aggregated_map = deepcopy(aggregated_maps_by_range[DEFAULT_MAP_RANGE])
        pair_maps = deepcopy(pair_maps_by_range[DEFAULT_MAP_RANGE])
        max_pain = {"status": "unavailable", "reason": "endpoint_removed_final33",
                    "records": [], "warnings": [], "errors": [], "provenance": {}}
        providers = {"coinglass": {"aggregated_history": history, "exchange_snapshot": exchange_snapshot,
                                     "long_short_ratio": long_short_ratio,
                                     "positioning_by_timeframe": positioning_tf,
                                     "positioning_by_exchange_timeframe": positioning_exchange_tf,
                                     "pair_history": pair_history, "events": events,
                                     "aggregated_map": aggregated_map, "pair_maps": pair_maps,
                                     "aggregated_maps_by_range": aggregated_maps_by_range,
                                     "pair_maps_by_range": pair_maps_by_range,
                                     "map_ranges": list(PUBLIC_MAP_RANGES),
                                     "max_pain": max_pain}}
        required = determine_required_datasets(
            mode=raw["mode"], raw_requests=requests, existing_contract=self.existing_contract,
        )
        quality = self._quality(providers, required)
        output = {"schema": {"id": "trad_elatin.long_short_liquidations.input.v1", "version": "1.0.0"},
                  "family": LONG_SHORT_LIQUIDATIONS_FAMILY, "stage": "input", "mode": raw.get("mode"),
                  "reference_timestamp": raw.get("reference_timestamp"),
                  "execution_timestamp": raw.get("execution_timestamp"), "providers": providers,
                  "quality": quality}
        if debug_raw:
            output["debug"] = {"raw": deepcopy(raw_bundle)}
        try:
            json.dumps(output, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("normalized_input_not_json_safe") from exc
        return output

    @staticmethod
    def _request_range(requests: Sequence[Mapping[str, Any]], default: str) -> str:
        if requests and isinstance(requests[0].get("params"), Mapping):
            value = requests[0]["params"].get("range")
            if isinstance(value, str):
                return value
        return default

    @staticmethod
    def _quality(providers: Mapping[str, Any], required: set[str]) -> dict[str, Any]:
        datasets: dict[str, str] = {}
        reasons: dict[str, str | None] = {}
        warnings, errors = [], []
        def visit(value: Any, path: str) -> None:
            if not isinstance(value, Mapping):
                return
            if value.get("status") in DATASET_STATES:
                datasets[path] = value["status"]
                reasons[path] = value.get("reason")
                warnings.extend(value.get("warnings", []))
                errors.extend(value.get("errors", []))
                return
            for key, child in value.items():
                visit(child, f"{path}.{key}" if path else key)
        visit(providers, "")
        missing = sorted(path for path in required
                         if path not in datasets or reasons.get(path) == "not_requested_in_mode")
        invalid = sorted(path for path in required if datasets.get(path) == "invalid")
        partial = sorted(path for path in required if datasets.get(path) == "partial")
        unavailable = sorted(path for path in required
                             if datasets.get(path) == "unavailable" and path not in missing)
        if missing or invalid:
            status, reason = "invalid", "required_datasets_invalid_or_missing"
        elif partial or unavailable:
            status, reason = "partial", "required_datasets_incomplete"
        else:
            status, reason = "ok", None
        optional = sorted(set(datasets) - required)
        provider_quality = {}
        for provider in providers:
            required_values = [datasets.get(path) for path in required if path.startswith(f"{provider}.")]
            optional_values = [datasets[path] for path in optional if path.startswith(f"{provider}.")]
            if "invalid" in required_values:
                provider_quality[provider] = "invalid"
            elif any(value in {"partial", "unavailable", None} for value in required_values) or any(
                value != "available" for value in optional_values
            ):
                provider_quality[provider] = "partial"
            else:
                provider_quality[provider] = "ok"
        return {"status": status, "reason": reason, "providers": provider_quality,
                "datasets": datasets, "required_datasets": sorted(required),
                "optional_datasets": optional, "missing_required_datasets": missing,
                "invalid_required_datasets": invalid, "partial_required_datasets": partial,
                "unavailable_required_datasets": unavailable,
                "recovery_required": status != "ok", "warnings": list(dict.fromkeys(warnings)),
                "errors": list(dict.fromkeys(errors))}

    def run(self, raw_bundle: Mapping[str, Any], *, debug_raw: bool = False) -> dict[str, Any]:
        return self.preprocess_raw(raw_bundle, debug_raw=debug_raw)


def run_long_short_liquidations_input(
    *, fetcher: RawFetcher, existing_contract: Mapping[str, Any] | None = None,
    requested_mode: str | None = None, recovery_requests: Sequence[Mapping[str, Any]] | None = None,
    reference_timestamp: int | None = None, clock: Callable[[], int | float] | None = None,
    asset: str = DEFAULT_ASSET, exchanges: Sequence[str] = DEFAULT_EXCHANGES,
    exchange_pairs: Mapping[str, str] | None = None, cryptoquant_exchanges: Sequence[str] | None = None,
    history_hours: int = DEFAULT_HISTORY_HOURS,
    incremental_overlap_hours: int = DEFAULT_INCREMENTAL_OVERLAP_H,
    event_lookback_hours: int = DEFAULT_EVENT_LOOKBACK_H,
    event_overlap_minutes: int = DEFAULT_EVENT_OVERLAP_MINUTES,
    min_event_usd: int | float = DEFAULT_MIN_EVENT_USD, map_range: str = DEFAULT_MAP_RANGE,
    exchange_range: str = DEFAULT_EXCHANGE_RANGE, max_pain_range: str = DEFAULT_MAX_PAIN_RANGE,
    minimum_event_window_seconds: int = 60, event_cursors: Mapping[str, int] | None = None,
    refresh_discovery: bool = False, include_confirmations: bool = True, debug_raw: bool = False,
) -> dict[str, Any]:
    preprocessor = LongShortLiquidationsInputPreprocessor(existing_contract=existing_contract)
    mode = preprocessor.determine_mode(requested_mode=requested_mode, recovery_requests=recovery_requests)
    extractor = LongShortLiquidationsRawExtractor(
        fetcher=fetcher, asset=asset, exchanges=exchanges, exchange_pairs=exchange_pairs,
        cryptoquant_exchanges=cryptoquant_exchanges, reference_timestamp=reference_timestamp, clock=clock,
        history_hours=history_hours, incremental_overlap_hours=incremental_overlap_hours,
        event_lookback_hours=event_lookback_hours, event_overlap_minutes=event_overlap_minutes,
        min_event_usd=min_event_usd, map_range=map_range, exchange_range=exchange_range,
        max_pain_range=max_pain_range, minimum_event_window_seconds=minimum_event_window_seconds,
    )
    if event_cursors is None and existing_contract is not None:
        event_cursors = {}
        existing_events = _old(existing_contract, "providers", "coinglass", "events") or {}
        for exchange, dataset in existing_events.items():
            records = dataset.get("records") if isinstance(dataset, Mapping) else None
            if isinstance(exchange, str) and isinstance(records, list) and records:
                timestamp = records[-1].get("timestamp") if isinstance(records[-1], Mapping) else None
                if isinstance(timestamp, int) and not isinstance(timestamp, bool):
                    event_cursors[exchange] = timestamp
    reuse_hourly = False
    if mode == "incremental" and isinstance(existing_contract, Mapping):
        previous_ref = existing_contract.get("reference_timestamp")
        reference_now = int(clock()) if reference_timestamp is None and clock is not None else reference_timestamp
        if reference_now is None:
            reference_now = int(__import__("time").time())
        if type(previous_ref) is int and previous_ref // 3600 == int(reference_now) // 3600:
            reuse_hourly = True
    raw = extractor.run(mode=mode, recovery_requests=recovery_requests, event_cursors=event_cursors,
                        refresh_discovery=refresh_discovery, include_confirmations=include_confirmations,
                        reuse_hourly=reuse_hourly)
    return preprocessor.preprocess_raw(raw, debug_raw=debug_raw)
