"""Validation and normalization for the CVD volume/order-flow Input family."""
from __future__ import annotations

import copy
import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from .cvd_volume_orderflow_data_raw_extract import (
    BASE_TIMEFRAMES, CVD_VOLUME_ORDERFLOW_FAMILY, FINAL_DISPLAY_RECORDS, FINAL_TIMEFRAMES, FINAL_WARMUP_RECORDS,
    TIMEFRAME_SECONDS, CvdVolumeOrderflowFetcher, CvdVolumeOrderflowRawExtractor, required_base_records,
)

READINESS_SOURCE = {"5m": "5m", "15m": "15m", "4h": "15m"}
READINESS_FACTORS = {"5m": 1, "15m": 1, "4h": 16}

def _sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def normalize_finite_float(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("invalid_numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_numeric") from exc
    if not math.isfinite(result):
        raise ValueError("invalid_numeric")
    return 0.0 if result == 0.0 else result


def normalize_non_negative_float(value: Any) -> float:
    result = normalize_finite_float(value)
    if result < 0:
        raise ValueError("negative_volume")
    return result


def normalize_timestamp(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("invalid_timestamp")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_timestamp") from exc
    if not math.isfinite(numeric) or numeric < 0 or not numeric.is_integer():
        raise ValueError("invalid_timestamp")
    result = int(numeric)
    if result >= 10_000_000_000:
        result //= 1000
    return result



def unwrap_coinglass_response(response: Any) -> list[Any]:
    if not isinstance(response, Mapping) or str(response.get("code")) != "0" or not isinstance(response.get("data"), list):
        raise ValueError("invalid_coinglass_envelope")
    return copy.deepcopy(response["data"])




def normalize_coinglass_cvd_record(row: Any) -> dict[str, Any]:
    if not isinstance(row, Mapping) or not {"time", "agg_taker_buy_vol", "agg_taker_sell_vol", "cum_vol_delta"}.issubset(row):
        raise ValueError("invalid_coinglass_cvd_record")
    return {"timestamp": normalize_timestamp(row["time"]), "taker_buy_volume_usd": normalize_non_negative_float(row["agg_taker_buy_vol"]),
        "taker_sell_volume_usd": normalize_non_negative_float(row["agg_taker_sell_vol"]),
        "provider_cvd_usd": normalize_finite_float(row["cum_vol_delta"])}


def normalize_footprint_snapshot(row: Any) -> dict[str, Any]:
    if not _sequence(row) or len(row) != 2 or not _sequence(row[1]):
        raise ValueError("invalid_footprint_snapshot")
    levels, invalid = [], []
    fields = ("price_start", "price_end", "taker_buy_volume_base", "taker_sell_volume_base", "taker_buy_volume_quote",
        "taker_sell_volume_quote", "taker_buy_volume_usdt", "taker_sell_volume_usdt", "taker_buy_trade_count", "taker_sell_trade_count")
    for index, level in enumerate(row[1]):
        if not _sequence(level) or len(level) != 10:
            invalid.append({"index": index, "reason": "footprint_level_must_have_ten_positions"})
            continue
        try:
            values = [normalize_non_negative_float(item) for item in level]
            if not values[8].is_integer() or not values[9].is_integer():
                raise ValueError("invalid_trade_count")
            levels.append({name: (int(value) if offset >= 8 else value) for offset, (name, value) in enumerate(zip(fields, values))})
        except ValueError as exc:
            invalid.append({"index": index, "reason": str(exc)})
    return {"timestamp": normalize_timestamp(row[0]), "levels": levels, "invalid_levels": invalid}




def upsert_records_by_timestamp(existing: Sequence[Mapping[str, Any]], incoming: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    old = copy.deepcopy(list(existing))
    new = copy.deepcopy(list(incoming))
    records = {row["timestamp"]: row for row in old if isinstance(row, Mapping) and type(row.get("timestamp")) is int}
    existing_timestamps = set(records)
    incoming_seen, duplicates, replaced = set(), 0, []
    for row in new:
        if not isinstance(row, Mapping) or type(row.get("timestamp")) is not int:
            raise ValueError("invalid_upsert_record")
        stamp = row["timestamp"]
        duplicates += stamp in incoming_seen
        incoming_seen.add(stamp)
        if stamp in existing_timestamps and stamp not in replaced:
            replaced.append(stamp)
        records[stamp] = copy.deepcopy(dict(row))
    output = [records[key] for key in sorted(records)]
    return output, {"records_before": len(old), "records_incoming": len(new), "records_after": len(output),
        "duplicates_incoming": duplicates, "timestamps_replaced": sorted(replaced)}


def detect_internal_gaps(records: Sequence[Mapping[str, Any]], expected_interval_seconds: int) -> list[dict[str, int]]:
    gaps = []
    for previous, following in zip(records, records[1:]):
        difference = following["timestamp"] - previous["timestamp"]
        if difference > expected_interval_seconds:
            missing_records = (difference - 1) // expected_interval_seconds
            first_missing = previous["timestamp"] + expected_interval_seconds
            last_missing = previous["timestamp"] + missing_records * expected_interval_seconds
            gaps.append({"previous_timestamp": previous["timestamp"], "next_timestamp": following["timestamp"],
                "expected_interval_seconds": expected_interval_seconds, "missing_records": missing_records,
                "first_missing_timestamp": first_missing, "last_missing_timestamp": last_missing,
                "start_timestamp": first_missing, "end_timestamp": last_missing})
    return gaps


def _normalize_rows(rows: Sequence[Any], normalizer: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    valid, invalid = [], []
    for index, row in enumerate(rows):
        try:
            valid.append(normalizer(row))
        except (TypeError, ValueError, KeyError) as exc:
            invalid.append({"index": index, "reason": str(exc)})
    return valid, invalid


def merge_paginated_records(requests: Sequence[Mapping[str, Any]], *, dataset: str = "aggregated_cvd",
                            records_required: int | None = None) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    incoming, invalid, raw_count, succeeded, failed = [], [], 0, 0, 0
    signatures, repeated = set(), False
    stop_reasons = []
    for page in sorted(copy.deepcopy(list(requests)), key=lambda item: item.get("page_index", 0)):
        stop_reasons.append(page.get("pagination_stop_reason"))
        if page.get("status") != "ok":
            failed += 1
            invalid.append({"page_index": page.get("page_index"), "reason": page.get("error") or "request_failed"})
            continue
        try:
            rows = unwrap_coinglass_response(page.get("response"))
        except ValueError as exc:
            failed += 1
            invalid.append({"page_index": page.get("page_index"), "reason": str(exc)})
            continue
        succeeded += 1
        raw_count += len(rows)
        signature = tuple(sorted(normalize_timestamp(row.get("time")) for row in rows if isinstance(row, Mapping) and "time" in row)) if dataset == "aggregated_cvd" else repr(rows)
        if signature in signatures and signature:
            repeated = True
        signatures.add(signature)
        valid, bad = _normalize_rows(rows, normalize_coinglass_cvd_record if dataset == "aggregated_cvd" else normalize_footprint_snapshot)
        incoming.extend(valid)
        invalid.extend({"page_index": page.get("page_index"), **item} for item in bad)
    unique = {row["timestamp"]: row for row in incoming}
    records = [unique[key] for key in sorted(unique)]
    stop = next((item for item in reversed(stop_reasons) if item), None) or ("repeated_page_signature" if repeated else "single_page")
    coverage_complete = records_required is None or len(records) >= records_required
    metadata = {"pages_requested": len(requests), "pages_succeeded": succeeded, "pages_failed": failed, "records_raw": raw_count,
        "records_unique": len(records), "duplicates_removed": len(incoming) - len(records),
        "pagination_complete": coverage_complete and failed == 0 and not repeated and stop not in {"max_pages_reached", "pagination_cursor_not_advancing", "page_error"},
        "pagination_stop_reason": "repeated_page_signature" if repeated else stop}
    return records, metadata, invalid


def _existing_input(existing_input: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if existing_input is None:
        return None
    if not isinstance(existing_input, Mapping):
        raise ValueError("existing_input is incompatible")
    candidate = existing_input.get("input", existing_input)
    if not isinstance(candidate, Mapping) or candidate.get("family") != CVD_VOLUME_ORDERFLOW_FAMILY or candidate.get("stage") != "input":
        raise ValueError("existing_input is incompatible")
    return candidate


def determine_cvd_volume_orderflow_input_mode(*, requested_mode: str | None = None,
                                                recovery_requests: Sequence[Mapping[str, Any]] | None = None,
                                                existing_input: Mapping[str, Any] | None = None) -> str:
    if requested_mode is not None:
        if requested_mode not in {"bootstrap", "incremental", "recovery"}:
            raise ValueError("unsupported mode")
        return requested_mode
    if recovery_requests:
        return "recovery"
    existing = _existing_input(existing_input)
    if existing is None:
        return "bootstrap"
    return "incremental" if all(existing.get("markets", {}).get(market, {}).get("cvd", {}).get("timeframes", {}).get(frame, {}).get("records")
        for market in ("spot", "futures") for frame in BASE_TIMEFRAMES) else "bootstrap"


def _status(*, structural: bool, records: Sequence[Any], failed: bool, invalid: Sequence[Any], gaps: Sequence[Any],
            insufficient: bool = False, disabled: bool = False) -> tuple[str, str | None]:
    if disabled:
        return "unavailable", "endpoint_disabled"
    if structural and not records:
        return "invalid", "invalid_structure"
    if not records:
        return "unavailable", "request_failed" if failed else "empty_data"
    if structural or failed or invalid or gaps:
        return "partial", "partial_response"
    if insufficient:
        return "partial", "insufficient_history_for_final_timeframes"
    return "available", None


def _primary_payload(pages: Sequence[Mapping[str, Any]], existing: Mapping[str, Any] | None, timeframe: str,
                     required: int) -> dict[str, Any]:
    incoming, pagination, invalid = merge_paginated_records(pages, records_required=required)
    records, upsert = upsert_records_by_timestamp(existing.get("records", []) if isinstance(existing, Mapping) else [], incoming)
    gaps = detect_internal_gaps(records, TIMEFRAME_SECONDS[timeframe])
    failed = pagination["pages_failed"] > 0
    structural = any(item.get("reason") == "invalid_coinglass_envelope" for item in invalid) or bool(invalid and not incoming and not failed)
    status, reason = _status(structural=structural, records=records, failed=failed, invalid=invalid, gaps=gaps, insufficient=len(records) < required)
    earliest_required = records[-1]["timestamp"] - (required - 1) * TIMEFRAME_SECONDS[timeframe] if records else None
    return {"status": status, "reason": reason, "records": records, "incoming_records": incoming, "invalid_records": invalid,
        "records_required": required, "records_available": len(records), "missing_records": max(0, required - len(records)),
        "earliest_required_timestamp": earliest_required, "earliest_available_timestamp": records[0]["timestamp"] if records else None,
        "expected_interval_seconds": TIMEFRAME_SECONDS[timeframe], "gaps": gaps, "pagination": pagination, "upsert": upsert,
        "provenance": {"provider": "coinglass", "dataset": "aggregated_cvd", "timeframe": timeframe}}


def _optional_payload(pages: Sequence[Mapping[str, Any]], existing: Mapping[str, Any] | None, provider: str,
                      dataset: str, enabled: bool) -> dict[str, Any]:
    """Normalize the contracted CoinGlass footprint enrichment only."""
    if provider != "coinglass" or dataset != "footprint":
        raise ValueError("unsupported_optional_cvd_source")
    if not enabled:
        return {"status": "unavailable", "reason": "endpoint_disabled", "records": [], "invalid_records": []}
    incoming, invalid, failed, structural = [], [], False, False
    for page in pages:
        if page.get("status") != "ok":
            failed = True
            continue
        try:
            rows = unwrap_coinglass_response(page.get("response"))
        except ValueError as exc:
            structural = True
            invalid.append({"reason": str(exc)})
            continue
        good, bad = _normalize_rows(rows, normalize_footprint_snapshot)
        incoming.extend(good)
        invalid.extend(bad)
    records, upsert = upsert_records_by_timestamp(existing.get("records", []) if isinstance(existing, Mapping) else [], incoming)
    nested_invalid = any(row.get("invalid_levels") for row in records)
    status, reason = _status(structural=structural, records=records, failed=failed, invalid=invalid or ([{}] if nested_invalid else []), gaps=[])
    return {"status": status, "reason": reason, "records": records, "incoming_records": incoming, "invalid_records": invalid,
        "upsert": upsert, "provenance": {"provider": "coinglass", "dataset": "footprint"}}


def evaluate_readiness(markets: Mapping[str, Any], target_display_records: int, warmup_records: int) -> dict[str, Any]:
    result = {}
    for target in FINAL_TIMEFRAMES:
        source = READINESS_SOURCE[target]
        required = (target_display_records + warmup_records) * READINESS_FACTORS[target]
        counts = [len(markets[market]["cvd"]["timeframes"][source]["records"]) for market in ("spot", "futures")]
        available = min(counts)
        result[target] = {"status": "available" if available >= required else "partial", "source_timeframe": source,
            "records_required": required, "records_available": available}
        if available < required:
            result[target]["reason"] = "insufficient_history_for_final_timeframes"
    return {"target_timeframes": result}


def evaluate_quality(markets: Mapping[str, Any]) -> dict[str, Any]:
    primary = {f"{market}.{frame}": markets[market]["cvd"]["timeframes"][frame]["status"] for market in ("spot", "futures") for frame in BASE_TIMEFRAMES}
    optional = {f"{market}.footprint": markets[market]["footprint"]["status"] for market in ("spot", "futures")}
    actionable_optional = [
        markets[market]["footprint"]["status"]
        for market in ("spot", "futures")
        if markets[market]["footprint"].get("reason") != "endpoint_disabled"
    ]
    if "invalid" in primary.values():
        status = "invalid"
    elif any(item != "available" for item in primary.values()) or any(item != "available" for item in actionable_optional):
        status = "partial"
    else:
        status = "ok"
    warnings = [key for key, value in {**primary, **optional}.items() if value in {"partial", "unavailable"}]
    errors = [key for key, value in {**primary, **optional}.items() if value == "invalid"]
    recovery = any(payload["gaps"] for market in ("spot", "futures") for payload in markets[market]["cvd"]["timeframes"].values())
    return {"status": status, "primary_sources": primary, "optional_sources": optional, "recovery_required": recovery,
        "warnings": warnings, "errors": errors}


class CvdVolumeOrderflowInputPreprocessor:
    def __init__(self, raw_extractor: CvdVolumeOrderflowRawExtractor, existing_input: Mapping[str, Any] | None = None) -> None:
        self.raw_extractor, self.existing_input = raw_extractor, copy.deepcopy(existing_input)

    def determine_mode(self, *, requested_mode: str | None = None,
                       recovery_requests: Sequence[Mapping[str, Any]] | None = None) -> str:
        return determine_cvd_volume_orderflow_input_mode(requested_mode=requested_mode, recovery_requests=recovery_requests,
            existing_input=self.existing_input)

    def preprocess_request(self, requests: Sequence[Mapping[str, Any]], **kwargs: Any) -> dict[str, Any]:
        return _optional_payload(requests, **kwargs)

    def preprocess_coinglass_cvd(self, requests: Sequence[Mapping[str, Any]], *, existing: Mapping[str, Any] | None,
                                 timeframe: str, records_required: int) -> dict[str, Any]:
        return _primary_payload(requests, existing, timeframe, records_required)

    def preprocess_footprint(self, requests: Sequence[Mapping[str, Any]], *, existing: Mapping[str, Any] | None, enabled: bool) -> dict[str, Any]:
        return _optional_payload(requests, existing, "coinglass", "footprint", enabled)

    def merge_paginated_records(self, requests: Sequence[Mapping[str, Any]], *, dataset: str = "aggregated_cvd",
                                records_required: int | None = None) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
        return merge_paginated_records(requests, dataset=dataset, records_required=records_required)

    def evaluate_readiness(self, markets: Mapping[str, Any], target_display_records: int, warmup_records: int) -> dict[str, Any]:
        return evaluate_readiness(markets, target_display_records, warmup_records)

    def evaluate_quality(self, markets: Mapping[str, Any]) -> dict[str, Any]:
        return evaluate_quality(markets)

    def run(self, *, reference_timestamp: int, requested_mode: str | None = None,
            recovery_requests: Sequence[Mapping[str, Any]] | None = None, include_footprint: bool = True,
            target_display_records: int = FINAL_DISPLAY_RECORDS, warmup_records: int = FINAL_WARMUP_RECORDS, **kwargs: Any) -> dict[str, Any]:
        mode = self.determine_mode(requested_mode=requested_mode, recovery_requests=recovery_requests)
        raw = self.raw_extractor.run(mode=mode, reference_timestamp=reference_timestamp, existing_input=self.existing_input,
            recovery_requests=recovery_requests, include_footprint=include_footprint,
            target_display_records=target_display_records, warmup_records=warmup_records, **kwargs)
        return self.preprocess_raw(raw, include_footprint=include_footprint,
            target_display_records=target_display_records, warmup_records=warmup_records)

    def preprocess_raw(self, raw: Mapping[str, Any], *, include_footprint: bool, target_display_records: int, warmup_records: int) -> dict[str, Any]:
        if not isinstance(raw, Mapping) or raw.get("family") != CVD_VOLUME_ORDERFLOW_FAMILY or raw.get("stage") != "raw_extract":
            raise ValueError("raw contract is incompatible")
        old = _existing_input(self.existing_input)
        requests = raw.get("requests", [])
        by_logical: dict[str, list[Mapping[str, Any]]] = {}
        for request in requests:
            by_logical.setdefault(request["logical_request_id"], []).append(request)
        markets = {}
        for market in ("spot", "futures"):
            frames = {}
            for timeframe in BASE_TIMEFRAMES:
                identifier = f"coinglass:{market}:aggregated_cvd:{timeframe}"
                previous = old.get("markets", {}).get(market, {}).get("cvd", {}).get("timeframes", {}).get(timeframe) if old else None
                required = required_base_records(timeframe, target_display_records, warmup_records)
                pages = by_logical.get(identifier, [])
                frames[timeframe] = self.preprocess_coinglass_cvd(pages, existing=previous,
                    timeframe=timeframe, records_required=required)
            footprint_pages = [item for key, value in by_logical.items() if key.startswith(f"coinglass:{market}:footprint:") for item in value]
            previous_footprint = old.get("markets", {}).get(market, {}).get("footprint") if old else None
            footprint = self.preprocess_footprint(footprint_pages, existing=previous_footprint, enabled=include_footprint)
            markets[market] = {"cvd": {"provider": "coinglass", "endpoint_id": f"{market}_aggregated_cvd", "timeframes": frames},
                "footprint": footprint}
        context = raw["context"]
        output_context = {"base_asset": context["base_asset"], "pair_symbol": context["pair_symbol"],
            "requested_exchanges": copy.deepcopy(context["requested_exchanges"]), "effective_exchanges": copy.deepcopy(context["requested_exchanges"]),
            "base_timeframes": list(BASE_TIMEFRAMES), "target_timeframes": list(FINAL_TIMEFRAMES), "target_display_records": target_display_records,
            "data_mode": context["data_mode"], "is_demo": context["is_demo"], "reference_timestamp": context["reference_timestamp"],
            "requested_at": context["requested_at"], "execution_timestamp": context["execution_timestamp"]}
        return {"family": CVD_VOLUME_ORDERFLOW_FAMILY, "stage": "input", "mode": raw["mode"], "context": output_context,
            "markets": markets, "readiness": self.evaluate_readiness(markets, target_display_records, warmup_records),
            "quality": self.evaluate_quality(markets)}


def run_cvd_volume_orderflow_input(*, fetcher: CvdVolumeOrderflowFetcher, base_asset: str = "BTC", pair_symbol: str = "BTCUSDT",
                                   exchanges: Sequence[str] = ("Binance", "OKX", "Bybit"), existing_input: Mapping[str, Any] | None = None,
                                   requested_mode: str | None = None, recovery_requests: Sequence[Mapping[str, Any]] | None = None,
                                   include_footprint: bool = True, footprint_exchanges: Sequence[str] = ("Binance", "OKX", "Bybit"),
                                   target_display_records: int = FINAL_DISPLAY_RECORDS, warmup_records: int = FINAL_WARMUP_RECORDS,
                                   incremental_limits: Mapping[str, int] | None = None, footprint_history_seconds: int = 172800,
                                   max_pages: int | None = None, data_mode: str = "synthetic", is_demo: bool = True,
                                   reference_timestamp: int | None = None, clock: Any = None, refresh_secondary: bool = False) -> dict[str, Any]:
    if reference_timestamp is None:
        raise ValueError("reference_timestamp is required")
    extractor = CvdVolumeOrderflowRawExtractor(fetcher, clock=clock)
    return CvdVolumeOrderflowInputPreprocessor(extractor, existing_input).run(reference_timestamp=reference_timestamp,
        requested_mode=requested_mode, recovery_requests=recovery_requests, include_footprint=include_footprint,
        target_display_records=target_display_records, warmup_records=warmup_records, base_asset=base_asset, pair_symbol=pair_symbol,
        exchanges=exchanges, footprint_exchanges=footprint_exchanges, incremental_limits=incremental_limits,
        footprint_history_seconds=footprint_history_seconds, max_pages=max_pages, data_mode=data_mode, is_demo=is_demo,
        refresh_secondary=refresh_secondary)
