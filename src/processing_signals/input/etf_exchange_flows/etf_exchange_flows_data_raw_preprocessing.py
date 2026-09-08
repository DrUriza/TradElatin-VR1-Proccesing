"""Validation, normalization and persistence for ETF and exchange flows Input."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from typing import Any

from .etf_exchange_flows_data_raw_extract import FAMILY, ENDPOINT_SPECS, EtfExchangeFlowsRawExtractor

DATASET_KEYS = ("etf_flows_daily", "etf_fund_flows_daily", "etf_funds_snapshot")
CQ_FIELDS = {"exchange_inflow": ("inflow_total", "inflow_top10", "inflow_mean"),
             "exchange_outflow": ("outflow_total", "outflow_top10", "outflow_mean"),
             "exchange_reserve": ("reserve",)}

ETF_HOURLY_REFRESH_SECONDS = 3_600
ETF_SLOW_REFRESH_SECONDS = 86_400


def _timestamp(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("invalid_timestamp")
    if isinstance(value, (int, float)) and math.isfinite(value):
        if isinstance(value, float) and not value.is_integer():
            raise ValueError("invalid_timestamp")
        integer = int(value)
        result = integer // 1000 if integer > 10_000_000_000 else integer
        if result > 0:
            return result
    if isinstance(value, str) and value:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    raise ValueError("invalid_timestamp")


def _number(value: Any, *, nullable: bool = True) -> float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool):
        raise ValueError("invalid_number")
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError as exc:
            raise ValueError("invalid_number") from exc
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("invalid_number")
    result = float(value)
    return 0.0 if result == 0 else result


def _empty_datasets() -> dict[str, Any]:
    datasets = {key: [] for key in DATASET_KEYS}
    for endpoint in CQ_FIELDS:
        datasets[endpoint] = {"hour": [], "day": []}
    datasets["secondary_sources"] = {}
    return datasets


def _invalid(store: dict[str, Any], provider: str, endpoint: str, index: int, reason: str, record: Any) -> None:
    clean = _json_clean(record)
    store.setdefault(provider, {}).setdefault(endpoint, []).append({"index": index, "reason": reason, "record": clean})


def _json_clean(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_clean(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _coinglass_data(entry: Mapping[str, Any]) -> list[Any]:
    body = entry.get("response")
    if not isinstance(body, Mapping) or body.get("code") not in {0, "0"} or not isinstance(body.get("data"), list):
        raise ValueError("invalid_coinglass_envelope")
    return body["data"]


def _normalize_coinglass(endpoint: str, entry: Mapping[str, Any], datasets: dict[str, Any], invalid: dict[str, Any]) -> int:
    rows = _coinglass_data(entry)
    valid = 0
    for index, row in enumerate(rows):
        try:
            if not isinstance(row, Mapping):
                raise ValueError("invalid_record")
            if endpoint == "bitcoin_etf_flows":
                timestamp = _timestamp(row.get("timestamp"))
                pending_parent = {"timestamp": timestamp, "flow_usd": _number(row.get("flow_usd")),
                    "price_usd": _number(row.get("price_usd")), "provider": "coinglass", "endpoint_id": endpoint}
                flows = row.get("etf_flows")
                if not isinstance(flows, list):
                    raise ValueError("invalid_etf_flows")
                pending_children = []
                for fund in flows:
                    if not isinstance(fund, Mapping):
                        raise ValueError("invalid_etf_flow_child")
                    ticker = fund.get("etf_ticker")
                    if not isinstance(ticker, str) or not ticker.strip():
                        raise ValueError("etf_ticker_required")
                    pending_children.append({"timestamp": timestamp, "ticker": ticker,
                        "flow_usd": _number(fund.get("flow_usd")), "provider": "coinglass", "endpoint_id": endpoint})
                datasets["etf_flows_daily"].append(pending_parent)
                datasets["etf_fund_flows_daily"].extend(pending_children)
            elif endpoint == "bitcoin_etf_list":
                item = deepcopy(dict(row))
                for key in ("shares_outstanding", "aum_usd", "management_fee_percent"):
                    item[key] = _number(item.get(key))
                details = item.get("asset_details")
                if isinstance(details, Mapping):
                    details = deepcopy(dict(details))
                    details["premium_discount_percent"] = _number(details.get("premium_discount_percent"))
                    item["asset_details"] = details
                raw_snapshot = item.get("last_quote_time", item.get("last_trade_time"))
                try:
                    snapshot_timestamp = _timestamp(raw_snapshot)
                except (TypeError, ValueError):
                    snapshot_timestamp = _timestamp(entry.get("requested_at", entry.get("timestamp", 1))) if entry.get("requested_at") or entry.get("timestamp") else None
                item.update(ticker=str(item["ticker"]), snapshot_timestamp=snapshot_timestamp, provider="coinglass", endpoint_id=endpoint)
                datasets["etf_funds_snapshot"].append(item)
            else:
                raise ValueError("endpoint_not_in_final33")
            valid += 1
        except (KeyError, TypeError, ValueError) as exc:
            _invalid(invalid, "coinglass", endpoint, index, str(exc), row)
    return valid


def _normalize_cryptoquant(endpoint: str, window: str, entry: Mapping[str, Any], datasets: dict[str, Any], invalid: dict[str, Any]) -> int:
    body = entry.get("response")
    if not isinstance(body, Mapping) or not isinstance(body.get("status"), Mapping) or body["status"].get("code") != 200:
        raise ValueError("invalid_cryptoquant_envelope")
    result = body.get("result")
    if not isinstance(result, Mapping) or result.get("window") != window or not isinstance(result.get("data"), list):
        raise ValueError("inconsistent_cryptoquant_window")
    valid = 0
    for index, row in enumerate(result["data"]):
        try:
            item = {"timestamp": _timestamp(row.get("date", row.get("datetime", row.get("timestamp")))), "window": window,
                    "exchange_scope": entry["params"]["exchange"], "provider": "cryptoquant", "endpoint_id": endpoint}
            for field in CQ_FIELDS[endpoint]:
                item[field] = _number(row.get(field))
            datasets[endpoint][window].append(item)
            valid += 1
        except (KeyError, TypeError, ValueError) as exc:
            _invalid(invalid, "cryptoquant", f"{endpoint}.{window}", index, str(exc), row)
    return valid


def _normalize_glassnode(endpoint: str, interval: str, entry: Mapping[str, Any], datasets: dict[str, Any],
                         invalid: dict[str, Any], warnings: list[str]) -> int:
    body = entry.get("response")
    if not isinstance(body, list):
        raise ValueError("invalid_glassnode_envelope")
    target = datasets["secondary_sources"].setdefault("glassnode", {}).setdefault(endpoint, {}).setdefault(interval, [])
    valid = 0
    for index, row in enumerate(body):
        try:
            value_raw = deepcopy(row["v"])
            scalar = not isinstance(value_raw, (Mapping, list))
            value = _number(value_raw) if scalar else None
            if not scalar:
                warnings.append(f"structured_glassnode_value:{endpoint}:{interval}")
            target.append({"timestamp": _timestamp(row["t"]), "value": value, "value_raw": value_raw,
                "asset": entry["params"]["a"], "interval": interval, "exchange_scope": None,
                "provider": "glassnode", "endpoint_id": endpoint})
            valid += 1
        except (KeyError, TypeError, ValueError) as exc:
            _invalid(invalid, "glassnode", f"{endpoint}.{interval}", index, str(exc), row)
    return valid


def _rollup_cryptoquant_hour_to_day(datasets: dict[str, Any], endpoint: str) -> None:
    """Roll persisted hourly primitives into UTC-day records locally.

    Inflow/outflow totals are additive; reserve is a state variable and uses the
    final hourly observation in the UTC day.  This avoids a duplicate provider
    day request on every incremental refresh while retaining the 1D Screen B
    history contract.
    """
    hourly = datasets.get(endpoint, {}).get("hour", [])
    if not isinstance(hourly, list) or not hourly:
        return
    grouped: dict[tuple[int, str], list[Mapping[str, Any]]] = {}
    for row in hourly:
        if not isinstance(row, Mapping) or type(row.get("timestamp")) is not int:
            continue
        day = int(row["timestamp"]) - int(row["timestamp"]) % 86_400
        scope = str(row.get("exchange_scope") or "")
        grouped.setdefault((day, scope), []).append(row)
    derived: list[dict[str, Any]] = []
    for (day, scope), rows in sorted(grouped.items()):
        rows = sorted(rows, key=lambda item: int(item["timestamp"]))
        item: dict[str, Any] = {
            "timestamp": day, "window": "day", "exchange_scope": scope,
            "provider": "cryptoquant", "endpoint_id": endpoint,
        }
        if endpoint == "exchange_reserve":
            value = rows[-1].get("reserve")
            if value is None:
                continue
            item["reserve"] = value
        else:
            any_numeric = False
            for field in CQ_FIELDS[endpoint]:
                values = [row.get(field) for row in rows if isinstance(row.get(field), (int, float)) and not isinstance(row.get(field), bool)]
                item[field] = float(sum(values)) if values else None
                any_numeric = any_numeric or bool(values)
            if not any_numeric and all(row.get(field) is None for row in rows for field in CQ_FIELDS[endpoint]):
                # Preserve the canonical day schema for an explicitly observed
                # null provider record instead of silently dropping its fields.
                pass
        derived.append(item)
    keys = ("endpoint_id", "window", "exchange_scope", "timestamp")
    current = datasets.get(endpoint, {}).get("day", [])
    # Keep the canonical rolling 730-day calculation window.  The hourly
    # observation for the current UTC day replaces/extends the newest day, so
    # the oldest day is dropped when the bootstrap seed already contains 730.
    datasets[endpoint]["day"] = _upsert(current, derived, keys)[-730:]


def _refresh_due(existing_contract: Mapping[str, Any] | None, *, now: int, key: str, interval: int) -> bool:
    if not isinstance(existing_contract, Mapping):
        return True
    state = existing_contract.get("context", {}).get("api_refresh_state", {})
    last = state.get(key) if isinstance(state, Mapping) else None
    return type(last) is not int or now - last >= interval


NATURAL_KEYS = {"etf_flows_daily": ("timestamp",), "etf_fund_flows_daily": ("timestamp", "ticker"),
    "etf_funds_snapshot": ("ticker",)}
CG_DATASETS = {"bitcoin_etf_flows": ("etf_flows_daily", "etf_fund_flows_daily"),
    "bitcoin_etf_list": ("etf_funds_snapshot",)}


def _upsert(existing: Sequence[Mapping[str, Any]], incoming: Sequence[Mapping[str, Any]], keys: Sequence[str]) -> list[dict[str, Any]]:
    # Old/partial states may contain availability placeholders with timestamp=None.
    # They are valid metadata, but must never participate in numeric timestamp sorting.
    merged = {tuple(item.get(key) for key in keys): deepcopy(dict(item)) for item in existing if isinstance(item, Mapping)}
    merged.update({tuple(item.get(key) for key in keys): deepcopy(dict(item)) for item in incoming if isinstance(item, Mapping)})
    def sort_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
        timestamp = item.get("timestamp")
        safe_timestamp = timestamp if type(timestamp) is int else -1
        return (safe_timestamp, *(str(item.get(key)) for key in keys))
    return sorted(merged.values(), key=sort_key)


def determine_etf_exchange_flows_input_mode(*, existing_contract=None, recovery_requests=None, requested_mode=None) -> str:
    if requested_mode is not None:
        if requested_mode not in {"bootstrap", "incremental", "recovery"}:
            raise ValueError("invalid_mode")
        if requested_mode == "recovery" and not recovery_requests:
            raise ValueError("recovery_requests_required")
        return requested_mode
    if recovery_requests:
        return "recovery"
    datasets = existing_contract.get("datasets", {}) if isinstance(existing_contract, Mapping) else {}
    required = all(datasets.get(key) for key in (
        "etf_flows_daily", "etf_funds_snapshot"
    ))
    required = required and all(
        datasets.get(endpoint, {}).get(window)
        for endpoint in ("exchange_inflow", "exchange_outflow", "exchange_reserve")
        for window in ("hour", "day")
    )
    return "incremental" if required else "bootstrap"


class EtfExchangeFlowsInputPreprocessor:
    def __init__(self, *, existing_contract: Mapping[str, Any] | None = None) -> None:
        self.existing = deepcopy(dict(existing_contract or {}))

    def run(self, raw_contract: Mapping[str, Any], *, generated_at: str) -> dict[str, Any]:
        if raw_contract.get("family") != FAMILY or raw_contract.get("stage") != "extracted_raw":
            raise ValueError("invalid_raw_contract")
        datasets, invalid, warnings, endpoint_quality = _empty_datasets(), {}, [], {}
        for provider, endpoints in raw_contract.get("raw", {}).items():
            for endpoint, value in endpoints.items():
                variants = value if endpoint in ENDPOINT_SPECS.get(provider, {}) and isinstance(value, Mapping) and "status" not in value else {None: value}
                for variant, entry in variants.items():
                    quality_id = f"{provider}.{endpoint}" + (f".{variant}" if variant else "")
                    received = valid = 0
                    try:
                        if entry.get("status") != "ok":
                            raise RuntimeError(entry.get("error") or "provider_unavailable")
                        if provider == "coinglass":
                            received = len(_coinglass_data(entry))
                            valid = _normalize_coinglass(endpoint, entry, datasets, invalid)
                        elif provider == "cryptoquant":
                            body = entry.get("response", {}).get("result", {}).get("data", [])
                            received = len(body) if isinstance(body, list) else 0
                            valid = _normalize_cryptoquant(endpoint, str(variant), entry, datasets, invalid)
                        else:
                            received = len(entry.get("response", [])) if isinstance(entry.get("response"), list) else 0
                            valid = _normalize_glassnode(endpoint, str(variant), entry, datasets, invalid, warnings)
                        rejected = invalid.get(provider, {}).get(endpoint, [])
                        if provider in {"cryptoquant", "glassnode"}:
                            rejected = invalid.get(provider, {}).get(f"{endpoint}.{variant}", [])
                        if received == 0:
                            status, reason = "unavailable", "empty_data"
                        elif valid == received:
                            status, reason = "available", None
                        elif valid:
                            status, reason = "partial", "records_rejected"
                        else:
                            status = "invalid"
                            reason = rejected[0]["reason"] if rejected else "records_rejected"
                    except (KeyError, TypeError, ValueError) as exc:
                        status, reason = "invalid", str(exc)
                    except RuntimeError as exc:
                        status, reason = "unavailable", str(exc)
                    endpoint_quality[quality_id] = {"status": status, "records_received": received, "records_valid": valid,
                        "records_rejected": max(received-valid, 0), "warnings": [], "errors": [], "reason": reason}
        old = self.existing.get("datasets", {})
        for key, natural in NATURAL_KEYS.items():
            datasets[key] = _upsert(old.get(key, []), datasets[key], natural)
        for endpoint in CQ_FIELDS:
            for window in ("hour", "day"):
                keys = ("endpoint_id", "window", "exchange_scope", "timestamp")
                datasets[endpoint][window] = _upsert(old.get(endpoint, {}).get(window, []), datasets[endpoint][window], keys)
        for endpoint in ("exchange_inflow", "exchange_outflow", "exchange_reserve"):
            _rollup_cryptoquant_hour_to_day(datasets, endpoint)
        old_secondary = old.get("secondary_sources", {}).get("glassnode", {})
        for endpoint, intervals in old_secondary.items():
            for interval, records in intervals.items():
                incoming = datasets["secondary_sources"].setdefault("glassnode", {}).setdefault(endpoint, {}).get(interval, [])
                datasets["secondary_sources"]["glassnode"][endpoint][interval] = _upsert(records, incoming,
                    ("endpoint_id", "interval", "asset", "exchange_scope", "timestamp"))
        for name, quality in endpoint_quality.items():
            provider, endpoint, *variant = name.split(".")
            if provider == "coinglass":
                records = [item for key in CG_DATASETS[endpoint] for item in datasets[key]]
            elif provider == "cryptoquant":
                records = datasets[endpoint][variant[0]]
            else:
                records = datasets["secondary_sources"].get("glassnode", {}).get(endpoint, {}).get(variant[0], [])
            record_timestamps = sorted({item.get("timestamp") for item in records if isinstance(item.get("timestamp"), int)})
            quality.update(records_available=len(records), first_timestamp=record_timestamps[0] if record_timestamps else None,
                           last_timestamp=record_timestamps[-1] if record_timestamps else None)
            if provider in {"cryptoquant", "glassnode"} and variant:
                step = 3600 if variant[0] in {"hour", "1h"} else 86400
                gaps = [(left, right) for left, right in zip(record_timestamps, record_timestamps[1:]) if right-left > step]
                if gaps:
                    warning = f"timestamp_gaps:{name}:{len(gaps)}"
                    quality["warnings"].append(warning)
                    warnings.append(warning)
        timestamps = []
        for key in NATURAL_KEYS:
            timestamps.extend(item["timestamp"] for item in datasets[key] if isinstance(item.get("timestamp"), int))
        for endpoint in CQ_FIELDS:
            for window in ("hour", "day"):
                timestamps.extend(
                    item["timestamp"] for item in datasets[endpoint][window]
                    if isinstance(item, Mapping) and type(item.get("timestamp")) is int
                )
        primary = [item["status"] for name, item in endpoint_quality.items() if not name.startswith("glassnode.")]
        usable = sum(status in {"available", "partial"} for status in primary)
        global_status = "ok" if primary and all(status == "available" for status in primary) else "partial" if usable else "invalid"
        provenance = {"providers": {}, "endpoint_requests": list(endpoint_quality), "requested_at": raw_contract["requested_at"],
                      "generated_at": generated_at, "data_as_of": max(timestamps) if timestamps else None}
        for provider in ENDPOINT_SPECS:
            names = [name for name in endpoint_quality if name.startswith(provider + ".")]
            provenance["providers"][provider] = {"requested_endpoints": names,
                "successful_endpoints": [name for name in names if endpoint_quality[name]["status"] in {"available", "partial"}],
                "failed_endpoints": [name for name in names if endpoint_quality[name]["status"] in {"unavailable", "invalid"}]}
        context = deepcopy(raw_contract.get("context", {}))
        previous_state = self.existing.get("context", {}).get("api_refresh_state", {})
        refresh_state = deepcopy(dict(previous_state)) if isinstance(previous_state, Mapping) else {}
        refresh_timestamp = context.get("refresh_timestamp")
        if type(refresh_timestamp) is int:
            if raw_contract.get("mode") == "bootstrap" or context.get("refresh_hourly") is True:
                refresh_state["hourly"] = refresh_timestamp
            if raw_contract.get("mode") == "bootstrap" or context.get("refresh_slow") is True:
                refresh_state["slow"] = refresh_timestamp
            if context.get("include_secondary") is True:
                refresh_state["secondary"] = refresh_timestamp
        context["api_refresh_state"] = refresh_state

        output = {
            "schema": {"id": "trad_elatin.etf_exchange_flows.input.v1", "version": "1.0.0"},
            "family": FAMILY,
            "stage": "input",
            "mode": raw_contract["mode"],
            "data_mode": raw_contract["data_mode"],
            "is_demo": raw_contract["is_demo"],
            "context": context,
            "requested_at": raw_contract["requested_at"],
            "generated_at": generated_at,
            "data_as_of": provenance["data_as_of"],
            "datasets": datasets,
            "invalid_records": invalid,
            "provenance": provenance,
            "quality": {
                "status": global_status,
                "endpoints": endpoint_quality,
                "warnings": list(dict.fromkeys(warnings)),
                "errors": [],
            },
        }
        json.dumps(output, allow_nan=False)
        return output


def run_etf_exchange_flows_input(*, fetcher, existing_contract=None, requested_mode=None, recovery_requests=None,
                                 include_secondary=False, data_mode="live", is_demo=False, exchange_scope=None,
                                 symbol="BTC", now=None, bootstrap_limits=None, incremental_limits=None,
                                 hourly_refresh_seconds: int = ETF_HOURLY_REFRESH_SECONDS,
                                 slow_refresh_seconds: int = ETF_SLOW_REFRESH_SECONDS):
    timestamp = now() if callable(now) else now
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp <= 0:
        raise ValueError("now must provide a positive integer timestamp")
    if type(hourly_refresh_seconds) is not int or hourly_refresh_seconds <= 0:
        raise ValueError("invalid_hourly_refresh_seconds")
    if type(slow_refresh_seconds) is not int or slow_refresh_seconds <= 0:
        raise ValueError("invalid_slow_refresh_seconds")
    mode = determine_etf_exchange_flows_input_mode(existing_contract=existing_contract,
        recovery_requests=recovery_requests, requested_mode=requested_mode)
    refresh_hourly = mode != "incremental" or _refresh_due(
        existing_contract, now=timestamp, key="hourly", interval=hourly_refresh_seconds)
    refresh_slow = mode != "incremental" or _refresh_due(
        existing_contract, now=timestamp, key="slow", interval=slow_refresh_seconds)

    # ETF data changes much more slowly than the one-minute market loop.  A
    # warm cycle inside both TTLs is a true acquisition NOOP: reuse the persisted
    # Input contract and spend zero provider calls.
    if mode == "incremental" and not refresh_hourly and not refresh_slow and not include_secondary:
        reused = deepcopy(dict(existing_contract or {}))
        if reused:
            reused["mode"] = "incremental"
            return reused

    extractor = EtfExchangeFlowsRawExtractor(fetcher=fetcher, exchange_scope=exchange_scope, symbol=symbol,
        include_secondary=include_secondary, data_mode=data_mode, is_demo=is_demo)
    raw = extractor.run(mode=mode, now=timestamp, recovery_requests=recovery_requests,
                        bootstrap_limits=bootstrap_limits, incremental_limits=incremental_limits,
                        refresh_hourly=refresh_hourly, refresh_slow=refresh_slow)
    return EtfExchangeFlowsInputPreprocessor(existing_contract=existing_contract).run(raw,
        generated_at=datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z"))

