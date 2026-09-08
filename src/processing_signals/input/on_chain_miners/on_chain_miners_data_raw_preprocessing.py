from __future__ import annotations

import copy
import math
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from .on_chain_miners_data_raw_extract import (
    CORE_METRIC_IDS,
    CRYPTOQUANT_PROVIDER,
    ENDPOINTS,
    FETCH_METRIC_IDS,
    GLASSNODE_PROVIDER,
    ON_CHAIN_MINERS_FAMILY,
    SECONDS_PER_DAY,
    VALID_MODES,
    OnChainMinersFetcher,
    OnChainMinersRawExtractor,
    resolve_existing_input_state,
)

UNITS = {
    "miner_reserve": "BTC",
    "sopr": "ratio",
    "hashrate": "H/s",
    "difficulty": "provider_native_difficulty",
    "miner_net_position_change": "BTC/day",
    "mpi": "z_score",
    "miner_outflow_total": "BTC/day",
    "miner_revenue_total_usd": "USD/day",
}
VALID_STATUSES = {"available", "partial", "unavailable", "invalid"}


def _finite(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("value_must_be_finite_number_or_null")
    number = float(value)
    return 0.0 if number == 0.0 else number


def normalize_unix_timestamp(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or int(value) != value or value < 0:
        raise ValueError("timestamp_must_be_non_negative_integer")
    value = int(value)
    return value // 1000 if value > 100_000_000_000 else value


def _cryptoquant_timestamp(record: Mapping[str, Any]) -> int:
    raw = record.get("datetime", record.get("date"))
    if not isinstance(raw, str):
        raise ValueError("cryptoquant_timestamp_missing")
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _unwrap(metric_id: str, response: Any) -> tuple[str | None, list[Any]]:
    provider = ENDPOINTS[metric_id]["provider"]
    if provider == GLASSNODE_PROVIDER:
        if not isinstance(response, Sequence) or isinstance(response, (str, bytes, bytearray)):
            raise ValueError("glassnode_response_must_be_sequence")
        return None, list(response)
    if provider == CRYPTOQUANT_PROVIDER:
        if not isinstance(response, Mapping):
            raise ValueError("cryptoquant_response_must_be_mapping")
        status = response.get("status")
        if not isinstance(status, Mapping) or status.get("code") not in (200, "200"):
            raise ValueError("cryptoquant_request_failed")
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise ValueError("cryptoquant_result_missing")
        data = result.get("data")
        if not isinstance(data, Sequence) or isinstance(data, (str, bytes, bytearray)):
            raise ValueError("cryptoquant_data_must_be_sequence")
        window = result.get("window")
        return str(window) if window is not None else None, list(data)
    raise ValueError("unsupported_provider")


def _record(metric_id: str, raw: Mapping[str, Any], source_window: str | None) -> dict[str, Any]:
    endpoint = ENDPOINTS[metric_id]
    if endpoint["provider"] == GLASSNODE_PROVIDER:
        timestamp = normalize_unix_timestamp(raw.get("t"))
        value = _finite(raw.get(endpoint["source_field"]))
    else:
        timestamp = _cryptoquant_timestamp(raw)
        value = _finite(raw.get(endpoint["source_field"]))
    if value is None:
        raise ValueError("provider_value_missing")
    output = {
        "timestamp": timestamp,
        "value": value,
        "unit": UNITS[metric_id],
        "provider": endpoint["provider"],
        "endpoint_id": endpoint["endpoint_id"],
        "source_field": endpoint["source_field"],
    }
    if source_window:
        output["source_window"] = source_window
    if metric_id == "sopr":
        output["sopr"] = value
    return output


def _merge_records(existing: Sequence[Mapping[str, Any]], incoming: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_timestamp: dict[int, dict[str, Any]] = {}
    for row in (*existing, *incoming):
        if not isinstance(row, Mapping):
            continue
        timestamp = row.get("timestamp")
        if isinstance(timestamp, bool) or not isinstance(timestamp, int):
            continue
        by_timestamp[timestamp] = copy.deepcopy(dict(row))
    return [by_timestamp[t] for t in sorted(by_timestamp)]


def _current(records: Sequence[Mapping[str, Any]], unit: str) -> dict[str, Any]:
    if not records:
        return {"status": "unavailable", "timestamp": None, "value": None, "unit": unit}
    row = records[-1]
    return {"status": "available", "timestamp": row.get("timestamp"), "value": row.get("value"), "unit": unit}


def _series_payload(metric_id: str, records: Sequence[Mapping[str, Any]], *, status: str,
                    warnings: Sequence[str] = (), errors: Sequence[str] = ()) -> dict[str, Any]:
    records = [copy.deepcopy(dict(row)) for row in records]
    return {
        "metric_id": metric_id,
        "status": status,
        "unit": UNITS[metric_id],
        "records": records,
        "current": _current(records, UNITS[metric_id]),
        "gaps": [],
        "warnings": list(dict.fromkeys(str(x) for x in warnings)),
        "errors": list(dict.fromkeys(str(x) for x in errors)),
        "metadata": {
            "records": len(records),
            "first_available_timestamp": records[0]["timestamp"] if records else None,
            "last_available_timestamp": records[-1]["timestamp"] if records else None,
        },
    }


def preprocess_on_chain_metric(*, metric_id: str, raw_payload: Mapping[str, Any] | None,
                               existing_series: Mapping[str, Any] | None = None,
                               mode: str = "bootstrap") -> dict[str, Any]:
    existing_series = existing_series if isinstance(existing_series, Mapping) else {}
    existing_records = existing_series.get("records", []) if mode in {"incremental", "recovery"} else []
    if not isinstance(existing_records, Sequence) or isinstance(existing_records, (str, bytes, bytearray)):
        existing_records = []
    if raw_payload is None:
        if existing_records:
            return _series_payload(metric_id, existing_records, status=str(existing_series.get("status", "available")),
                                   warnings=existing_series.get("warnings", []), errors=[])
        return _series_payload(metric_id, [], status="unavailable", warnings=["provider_bucket_not_requested"])
    if raw_payload.get("status") != "ok":
        error = raw_payload.get("error")
        message = error.get("message") if isinstance(error, Mapping) else "provider_request_failed"
        if existing_records:
            return _series_payload(metric_id, existing_records, status="partial", warnings=[f"provider_refresh_failed:{message}"])
        return _series_payload(metric_id, [], status="unavailable", warnings=[f"provider_request_failed:{message}"])
    try:
        window, rows = _unwrap(metric_id, raw_payload.get("response"))
        normalized = [_record(metric_id, row, window) for row in rows if isinstance(row, Mapping)]
        records = _merge_records(existing_records, normalized)
        return _series_payload(metric_id, records, status="available" if records else "unavailable",
                               warnings=[] if records else ["provider_returned_no_records"])
    except Exception as exc:
        if existing_records:
            return _series_payload(metric_id, existing_records, status="partial", warnings=[f"normalization_failed:{type(exc).__name__}"])
        return _series_payload(metric_id, [], status="invalid", errors=[f"normalization_failed:{type(exc).__name__}:{exc}"])


def derive_miner_net_position_change_from_reserve(reserve_series: Mapping[str, Any]) -> dict[str, Any]:
    source = reserve_series.get("records", [])
    by_day: dict[int, Mapping[str, Any]] = {}
    for row in source if isinstance(source, Sequence) else []:
        if not isinstance(row, Mapping) or not isinstance(row.get("timestamp"), int):
            continue
        day = int(row["timestamp"]) - int(row["timestamp"]) % SECONDS_PER_DAY
        previous = by_day.get(day)
        if previous is None or int(row["timestamp"]) >= int(previous["timestamp"]):
            by_day[day] = row
    daily = [by_day[d] for d in sorted(by_day)]
    records: list[dict[str, Any]] = []
    for previous, current in zip(daily, daily[1:]):
        day = int(current["timestamp"]) - int(current["timestamp"]) % SECONDS_PER_DAY
        prev_day = int(previous["timestamp"]) - int(previous["timestamp"]) % SECONDS_PER_DAY
        if day - prev_day != SECONDS_PER_DAY:
            continue
        value = float(current["value"]) - float(previous["value"])
        records.append({
            "timestamp": day,
            "value": 0.0 if value == 0.0 else value,
            "unit": "BTC/day",
            "provider": "derived",
            "endpoint_id": None,
            "source_metric_id": "miner_reserve",
            "calculation": "daily_first_difference",
        })
    source_status = str(reserve_series.get("status", "invalid"))
    status = "invalid" if source_status == "invalid" else "partial" if source_status == "partial" else "available" if records else "unavailable"
    warnings = [] if records else ["insufficient_reserve_history_for_daily_difference"]
    return _series_payload("miner_net_position_change", records, status=status, warnings=warnings,
                           errors=reserve_series.get("errors", []) if source_status == "invalid" else [])


def determine_on_chain_miners_input_mode(*, existing_contract: Mapping[str, Any] | None = None,
                                         recovery_requests: Sequence[Mapping[str, Any]] | None = None,
                                         requested_mode: str | None = None) -> str:
    if requested_mode is not None:
        if requested_mode not in VALID_MODES:
            raise ValueError(f"Unsupported on_chain_miners input mode: {requested_mode}")
        if requested_mode == "recovery" and not recovery_requests:
            raise ValueError("recovery mode requires recovery_requests")
        return requested_mode
    if recovery_requests:
        return "recovery"
    existing = resolve_existing_input_state(existing_contract)
    series = existing.get("series", {}) if isinstance(existing, Mapping) else {}
    return "incremental" if all(isinstance(series.get(metric), Mapping) and series[metric].get("records") for metric in CORE_METRIC_IDS) else "bootstrap"


def evaluate_on_chain_miners_quality(series: Mapping[str, Any]) -> dict[str, Any]:
    availability = {metric: str(series.get(metric, {}).get("status", "invalid")) for metric in CORE_METRIC_IDS}
    warnings = [f"{metric}:{message}" for metric in CORE_METRIC_IDS for message in series.get(metric, {}).get("warnings", [])]
    errors = [f"{metric}:{message}" for metric in CORE_METRIC_IDS for message in series.get(metric, {}).get("errors", [])]
    missing = [metric for metric, status in availability.items() if status in {"unavailable", "invalid"}]
    if errors or "invalid" in availability.values():
        status = "invalid"
    elif all(value == "available" for value in availability.values()):
        status = "ok"
    else:
        status = "partial"
    last_values = [series[m].get("metadata", {}).get("last_available_timestamp") for m in CORE_METRIC_IDS]
    data_as_of = min((value for value in last_values if isinstance(value, int)), default=None)
    return {
        "status": status,
        "availability": availability,
        "missing_fields": missing,
        "warnings": list(dict.fromkeys(warnings)),
        "errors": list(dict.fromkeys(errors)),
        "recovery_required": status != "ok",
        "data_as_of": data_as_of,
    }


class OnChainMinersInputPreprocessor:
    def __init__(self, *, raw_extractor: OnChainMinersRawExtractor,
                 existing_contract: Mapping[str, Any] | None = None) -> None:
        self.raw_extractor = raw_extractor
        self.existing_contract = resolve_existing_input_state(existing_contract)

    def run(self, *, requested_mode: str | None = None,
            recovery_requests: Sequence[Mapping[str, Any]] | None = None,
            reference_timestamp: int,
            execution_timestamp: int | None = None) -> dict[str, Any]:
        execution_timestamp = normalize_unix_timestamp(int(time.time()) if execution_timestamp is None else execution_timestamp)
        mode = determine_on_chain_miners_input_mode(existing_contract=self.existing_contract,
                                                     recovery_requests=recovery_requests,
                                                     requested_mode=requested_mode)
        extracted = self.raw_extractor.run(mode=mode, reference_timestamp=reference_timestamp,
                                           existing_contract=self.existing_contract,
                                           recovery_requests=recovery_requests,
                                           execution_timestamp=execution_timestamp)
        existing_series = self.existing_contract.get("series", {}) if mode in {"incremental", "recovery"} else {}
        existing_series = existing_series if isinstance(existing_series, Mapping) else {}
        series: dict[str, Any] = {}
        for metric_id in FETCH_METRIC_IDS:
            series[metric_id] = preprocess_on_chain_metric(metric_id=metric_id,
                                                           raw_payload=extracted["raw"].get(metric_id),
                                                           existing_series=existing_series.get(metric_id, {}),
                                                           mode=mode)
        series["miner_net_position_change"] = derive_miner_net_position_change_from_reserve(series["miner_reserve"])
        generated_at = datetime.fromtimestamp(execution_timestamp, timezone.utc).isoformat().replace("+00:00", "Z")
        return {
            "family": ON_CHAIN_MINERS_FAMILY,
            "stage": "input",
            "mode": mode,
            "context": {
                "asset": self.raw_extractor.asset,
                "data_mode": self.raw_extractor.data_mode,
                "is_demo": self.raw_extractor.is_demo,
                "reference_timestamp": reference_timestamp,
                "execution_timestamp": execution_timestamp,
                "generated_at": generated_at,
            },
            "series": series,
            "quality": evaluate_on_chain_miners_quality(series),
        }


def run_on_chain_miners_input(*, fetcher: OnChainMinersFetcher, reference_timestamp: int,
                              asset: str = "BTC", existing_contract: Mapping[str, Any] | None = None,
                              requested_mode: str | None = None,
                              recovery_requests: Sequence[Mapping[str, Any]] | None = None,
                              data_mode: str = "live", is_demo: bool = False,
                              execution_timestamp: int | None = None) -> dict[str, Any]:
    extractor = OnChainMinersRawExtractor(fetcher=fetcher, asset=asset, data_mode=data_mode, is_demo=is_demo)
    preprocessor = OnChainMinersInputPreprocessor(raw_extractor=extractor, existing_contract=existing_contract)
    return preprocessor.run(requested_mode=requested_mode, recovery_requests=recovery_requests,
                            reference_timestamp=reference_timestamp, execution_timestamp=execution_timestamp)
