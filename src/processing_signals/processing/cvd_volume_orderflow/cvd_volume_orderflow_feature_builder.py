"""Pure numeric feature construction for CVD volume/order-flow Processing."""
from __future__ import annotations

import copy
import math
from collections import deque
from collections.abc import Mapping, Sequence
from itertools import pairwise
from typing import Any

CVD_VOLUME_ORDERFLOW_FAMILY = "cvd_volume_orderflow"
PROCESSING_STAGE             = "processing"
PROCESSING_VERSION           = "0.1.0"
MARKETS                      = ("spot", "futures")
BASE_TIMEFRAMES              = ("5m", "15m")
TARGET_TIMEFRAMES            = ("5m", "15m", "4h")
TIMEFRAME_SECONDS            = {"5m": 300, "15m": 900, "4h": 14400}
SOURCE_TIMEFRAME             = {"5m": "5m", "15m": "15m", "4h": "15m"}
SOURCE_FACTOR                = {"5m": 1, "15m": 1, "4h": 16}
DELTA_MA_PERIOD              = 21
FLOW_EFFICIENCY_PERIOD       = 21
FIXED_WINDOWS_SECONDS        = {"4h": 14400, "24h": 86400}


def _sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def finite_float(value: Any, name: str, *, non_negative: bool = False, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"invalid_{name}")
    result = float(value)
    if not math.isfinite(result) or (non_negative and result < 0) or (positive and result <= 0):
        raise ValueError(f"invalid_{name}")
    return 0.0 if result == 0.0 else result


def _metric(value: float | None, reason: str | None = None) -> dict[str, Any]:
    return {"value": value, "status": "available" if reason is None else "unavailable", "reason": reason}


def volume_features(buy: float, sell: float) -> dict[str, Any]:
    total, delta = buy + sell, buy - sell
    if total == 0:
        ratio = buy_share = sell_share = imbalance = _metric(None, "zero_total_volume")
    else:
        ratio = _metric(buy / sell) if sell > 0 else _metric(None, "sell_volume_zero")
        buy_share, sell_share, imbalance = _metric(buy / total), _metric(sell / total), _metric(delta / total)
    return {"taker_buy_volume_usd": buy, "taker_sell_volume_usd": sell, "total_volume_usd": total,
        "volume_delta_usd": delta, "buy_sell_ratio": ratio, "buy_share": buy_share, "sell_share": sell_share,
        "order_flow_imbalance": imbalance}


def validate_base_records(records: Any) -> list[dict[str, Any]]:
    if not _sequence(records):
        raise ValueError("records_must_be_sequence")
    output, previous, seen = [], None, set()
    for row in records:
        if not isinstance(row, Mapping) or type(row.get("timestamp")) is not int or row["timestamp"] < 0:
            raise ValueError("invalid_base_record")
        timestamp = row["timestamp"]
        if timestamp in seen or (previous is not None and timestamp <= previous):
            raise ValueError("records_not_strictly_ordered_or_duplicate")
        seen.add(timestamp)
        previous = timestamp
        buy = finite_float(row.get("taker_buy_volume_usd"), "buy_volume", non_negative=True)
        sell = finite_float(row.get("taker_sell_volume_usd"), "sell_volume", non_negative=True)
        provider = row.get("provider_cvd_usd")
        if provider is not None:
            provider = finite_float(provider, "provider_cvd")
        output.append({"timestamp": timestamp, **volume_features(buy, sell), "provider_cvd_reference_usd": provider})
    return output


def resample_records(records: Sequence[Mapping[str, Any]], source_timeframe: str, target_timeframe: str) -> list[dict[str, Any]]:
    if SOURCE_TIMEFRAME.get(target_timeframe) != source_timeframe:
        raise ValueError("invalid_source_target_timeframe")
    interval, expected = TIMEFRAME_SECONDS[target_timeframe], SOURCE_FACTOR[target_timeframe]
    buckets: dict[int, list[Mapping[str, Any]]] = {}
    for row in records:
        bucket = row["timestamp"] - row["timestamp"] % interval
        buckets.setdefault(bucket, []).append(row)
    output = []
    for timestamp in sorted(buckets):
        source = sorted(buckets[timestamp], key=lambda row: row["timestamp"])
        buy = sum(row["taker_buy_volume_usd"] for row in source)
        sell = sum(row["taker_sell_volume_usd"] for row in source)
        expected_timestamps = [timestamp + index * TIMEFRAME_SECONDS[source_timeframe] for index in range(expected)]
        coverage = len(source) == expected and [row["timestamp"] for row in source] == expected_timestamps
        provider = source[0].get("provider_cvd_reference_usd") if expected == 1 else None
        output.append({"timestamp": timestamp, **volume_features(buy, sell), "source_timeframe": source_timeframe,
            "source_records_expected": expected, "source_records_used": len(source), "coverage_complete": coverage,
            "is_partial": not coverage, "first_source_timestamp": source[0]["timestamp"], "last_source_timestamp": source[-1]["timestamp"],
            "provider_cvd_reference_usd": provider, "_source_deltas": [row["volume_delta_usd"] for row in source]})
    # A finite bootstrap commonly starts/ends inside a larger UTC bucket.  Those
    # edge buckets are not gaps in the observed source series and must not poison
    # the cumulative path forever.  Interior incomplete buckets are retained and
    # still break continuity.
    if any(not row["is_partial"] for row in output):
        while output and output[0]["is_partial"]:
            output.pop(0)
        # Keep the right-edge partial bucket: it is the live CVD bucket and
        # must be replaced on each 15 s refresh.  Only the incomplete left
        # bootstrap edge is discarded; interior partial buckets still break
        # continuity.
    return output


def _continuity_breaks(records: Sequence[Mapping[str, Any]], interval: int) -> list[dict[str, int]]:
    breaks = []
    for previous, following in zip(records, records[1:]):
        difference = following["timestamp"] - previous["timestamp"]
        if difference > interval:
            breaks.append({"after_timestamp": previous["timestamp"], "before_timestamp": following["timestamp"],
                "missing_records": (difference - 1) // interval})
    return breaks


def build_cvd_bars(records: Sequence[Mapping[str, Any]], target_timeframe: str,
                   declared_gaps: Sequence[Mapping[str, Any]] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, int]], dict[str, Any]]:
    interval = TIMEFRAME_SECONDS[target_timeframe]
    breaks = _continuity_breaks(records, interval)
    for gap in declared_gaps or ():
        if not isinstance(gap, Mapping) or type(gap.get("previous_timestamp")) is not int or type(gap.get("next_timestamp")) is not int:
            raise ValueError("invalid_declared_gap")
        after = gap["previous_timestamp"] - gap["previous_timestamp"] % interval
        before = gap["next_timestamp"] - gap["next_timestamp"] % interval
        missing = gap.get("missing_records")
        if type(missing) is not int or missing <= 0:
            raise ValueError("invalid_declared_gap")
        breaks.append({"after_timestamp": after, "before_timestamp": before, "missing_records": missing})
    for row in records:
        if row["is_partial"]:
            breaks.append({"after_timestamp": row["last_source_timestamp"], "before_timestamp": row["timestamp"] + interval,
                "missing_records": max(1, row["source_records_expected"] - row["source_records_used"])})
    breaks = [{"after_timestamp": after, "before_timestamp": before, "missing_records": missing} for after, before, missing in
        sorted({(item["after_timestamp"], item["before_timestamp"], item["missing_records"]) for item in breaks})]
    break_before = {item["before_timestamp"] for item in breaks}
    output, running, broken = [], 0.0, False
    for row in records:
        if row["timestamp"] in break_before or row["is_partial"]:
            broken = True
        open_value, path = running, [running]
        for delta in row["_source_deltas"]:
            running += delta
            path.append(running)
        partial = bool(row["is_partial"] or broken)
        item = {key: copy.deepcopy(value) for key, value in row.items() if key != "_source_deltas"}
        item.update(cvd_ohlc_usd={"open": open_value, "high": max(path), "low": min(path), "close": running},
            continuity_status="broken" if broken else "complete", status="partial" if partial else "available",
            reason="cvd_continuity_broken_by_missing_intervals" if broken else ("incomplete_source_bucket" if row["is_partial"] else None),
            delta_ma_21_usd=None, flow_efficiency=_metric(None, "rolling_warmup_incomplete"))
        output.append(item)
    anchor = {"first_available_timestamp": output[0]["timestamp"] if output else None,
        "anchor_timestamp": output[0]["timestamp"] if output else None, "anchor_value_usd": 0.0,
        "anchor_method": "zero_before_first_available_record", "history_relative": True,
        "construction": "derived_from_interval_volume_delta_path", "native_ohlc": False}
    return output, breaks, anchor


def apply_rolling_features(records: Sequence[Mapping[str, Any]], period: int = DELTA_MA_PERIOD,
                           *, copy_records: bool = True) -> list[dict[str, Any]]:
    """Add rolling fields, optionally taking ownership of a freshly built list.

    The public default preserves the original no-mutation contract.  The feature
    pipeline passes its private ``bars`` list with ``copy_records=False`` because
    no other object can observe or reuse those records after this call.
    """
    output = copy.deepcopy(list(records)) if copy_records else list(records)
    if period <= 0:
        # Preserve deterministic non-positive-period behavior; production uses period 21.
        for index, row in enumerate(output):
            window = output[max(0, index - period + 1):index + 1]
            consecutive = len(window) == period and all(item["coverage_complete"] and item["continuity_status"] == "complete" for item in window)
            consecutive = consecutive and all(b["timestamp"] - a["timestamp"] > 0 for a, b in zip(window, window[1:]))
            if not consecutive:
                reason = "partial_or_broken_window" if row["is_partial"] or row["continuity_status"] == "broken" else "rolling_warmup_incomplete"
                row["delta_ma_21_usd"] = None
                row["flow_efficiency"] = _metric(None, reason)
                continue
            deltas = [item["volume_delta_usd"] for item in window]
            row["delta_ma_21_usd"] = sum(deltas) / period
            denominator = sum(abs(value) for value in deltas)
            row["flow_efficiency"] = _metric(abs(sum(deltas)) / denominator) if denominator else _metric(None, "zero_absolute_delta_path")
        return output

    window: deque[Mapping[str, Any]] = deque(maxlen=period)
    for row in output:
        window.append(row)
        consecutive = len(window) == period and all(item["coverage_complete"] and item["continuity_status"] == "complete" for item in window)
        consecutive = consecutive and all(b["timestamp"] - a["timestamp"] > 0 for a, b in pairwise(window))
        if not consecutive:
            reason = "partial_or_broken_window" if row["is_partial"] or row["continuity_status"] == "broken" else "rolling_warmup_incomplete"
            row["delta_ma_21_usd"] = None
            row["flow_efficiency"] = _metric(None, reason)
            continue
        total_delta = sum(item["volume_delta_usd"] for item in window)
        row["delta_ma_21_usd"] = total_delta / period
        denominator = sum(abs(item["volume_delta_usd"]) for item in window)
        row["flow_efficiency"] = _metric(abs(total_delta) / denominator) if denominator else _metric(None, "zero_absolute_delta_path")
    return output


def directional_persistence(deltas: Sequence[float]) -> dict[str, Any]:
    """Directional consistency of aggressive flow in [0, 1].

    1.0 means every non-zero interval points the same way; values near zero
    mean alternating/balanced flow.  This is derived only from contracted CVD
    deltas and requires no additional provider data.
    """
    signs = [1 if value > 0 else -1 if value < 0 else 0 for value in deltas]
    nonzero = [value for value in signs if value]
    if not nonzero:
        return {"value": None, "direction": "neutral", "status": "unavailable", "reason": "no_directional_delta"}
    score = abs(sum(nonzero)) / len(nonzero)
    net = sum(nonzero)
    return {"value": score, "direction": "buy" if net > 0 else "sell" if net < 0 else "neutral", "status": "available", "reason": None}


def build_fixed_window_summary(records_15m: Sequence[Mapping[str, Any]], window_name: str) -> dict[str, Any]:
    expected = FIXED_WINDOWS_SECONDS[window_name] // TIMEFRAME_SECONDS["15m"]
    selected = list(records_15m[-expected:])
    buy, sell = sum(row["taker_buy_volume_usd"] for row in selected), sum(row["taker_sell_volume_usd"] for row in selected)
    features = volume_features(buy, sell)
    deltas = [row["volume_delta_usd"] for row in selected]
    denominator = sum(abs(value) for value in deltas)
    consecutive = len(selected) == expected and all(not row["is_partial"] for row in selected)
    if selected:
        consecutive = consecutive and selected[-1]["timestamp"] - selected[0]["timestamp"] == (expected - 1) * TIMEFRAME_SECONDS["15m"]
    status, reason = ("available", None) if consecutive else (("partial", "incomplete_fixed_window") if selected else ("unavailable", "no_records"))
    return {**features, "flow_efficiency": _metric(abs(sum(deltas)) / denominator) if denominator else _metric(None, "zero_absolute_delta_path"),
        "directional_persistence": directional_persistence(deltas),
        "records_expected": expected, "records_used": len(selected), "coverage_complete": consecutive,
        "first_timestamp": selected[0]["timestamp"] if selected else None, "last_timestamp": selected[-1]["timestamp"] if selected else None,
        "status": status, "reason": reason}


def build_footprint_vwap(footprint: Mapping[str, Any] | None, *, window_seconds: int = 3600) -> dict[str, Any]:
    records = footprint.get("records", []) if isinstance(footprint, Mapping) else []
    if not _sequence(records) or not records:
        return {"vwap_usd": None, "base_volume": 0.0, "quote_volume": 0.0, "records_used": 0, "levels_used": 0,
            "status": "unavailable", "reason": "footprint_data_not_available", "calculation_basis": "available_normalized_footprint_levels",
            "aggregation_scope": "partial_input_scope"}
    latest = max(row.get("timestamp", 0) for row in records if isinstance(row, Mapping))
    selected = [row for row in records if isinstance(row, Mapping) and type(row.get("timestamp")) is int and row["timestamp"] > latest - window_seconds]
    base = quote = 0.0
    levels_used = 0
    for snapshot in selected:
        for level in snapshot.get("levels", []):
            if not isinstance(level, Mapping):
                continue
            try:
                level_base = finite_float(level.get("taker_buy_volume_base"), "footprint_base", non_negative=True) + finite_float(level.get("taker_sell_volume_base"), "footprint_base", non_negative=True)
                level_quote = finite_float(level.get("taker_buy_volume_quote"), "footprint_quote", non_negative=True) + finite_float(level.get("taker_sell_volume_quote"), "footprint_quote", non_negative=True)
            except ValueError:
                continue
            base, quote, levels_used = base + level_base, quote + level_quote, levels_used + 1
    if not levels_used or base == 0:
        return {"vwap_usd": None, "base_volume": base, "quote_volume": quote, "records_used": len(selected), "levels_used": levels_used,
            "status": "unavailable", "reason": "footprint_data_not_available", "calculation_basis": "available_normalized_footprint_levels",
            "aggregation_scope": "partial_input_scope"}
    scope_complete = all(row.get("exchange") and row.get("source_timeframe") and row.get("provenance") for row in selected)
    return {"vwap_usd": quote / base, "base_volume": base, "quote_volume": quote, "records_used": len(selected), "levels_used": levels_used,
        "status": "available" if scope_complete else "partial", "reason": None if scope_complete else "footprint_exchange_or_timeframe_scope_not_preserved",
        "calculation_basis": "available_normalized_footprint_levels", "aggregation_scope": "complete_input_scope" if scope_complete else "partial_input_scope"}


def build_price_vs_vwap(vwap: Mapping[str, Any], price_reference: Mapping[str, Any] | None) -> dict[str, Any]:
    if vwap.get("vwap_usd") is None or vwap.get("vwap_usd", 0) <= 0:
        return {"value": None, "status": "unavailable", "reason": "vwap_not_available", "price_timestamp": None, "price_usd": None}
    if price_reference is None:
        return {"value": None, "status": "unavailable", "reason": "price_reference_not_provided", "price_timestamp": None, "price_usd": None}
    if not isinstance(price_reference, Mapping) or type(price_reference.get("timestamp")) is not int:
        raise ValueError("invalid_price_reference")
    price = finite_float(price_reference.get("price_usd"), "price_reference", positive=True)
    return {"value": (price - vwap["vwap_usd"]) / vwap["vwap_usd"], "status": "available", "reason": None,
        "price_timestamp": price_reference["timestamp"], "price_usd": price}


class CvdVolumeOrderflowFeatureBuilder:
    validate_base_records       = staticmethod(validate_base_records)
    resample_records            = staticmethod(resample_records)
    build_cvd_bars              = staticmethod(build_cvd_bars)
    apply_rolling_features      = staticmethod(apply_rolling_features)
    build_fixed_window_summary  = staticmethod(build_fixed_window_summary)
    directional_persistence     = staticmethod(directional_persistence)
    build_footprint_vwap        = staticmethod(build_footprint_vwap)
    build_price_vs_vwap         = staticmethod(build_price_vs_vwap)

    def build_market_features(self, base_records: Mapping[str, Sequence[Mapping[str, Any]]],
                              declared_gaps: Mapping[str, Sequence[Mapping[str, Any]]] | None = None) -> dict[str, Any]:
        timeframes = {}
        for target in TARGET_TIMEFRAMES:
            source = SOURCE_TIMEFRAME[target]
            resampled = resample_records(base_records[source], source, target)
            bars, breaks, anchor = build_cvd_bars(resampled, target, (declared_gaps or {}).get(source, ()))
            timeframes[target] = {"records": apply_rolling_features(bars, copy_records=False), "continuity_breaks": breaks, **anchor}
        return timeframes


def build_cvd_volume_orderflow_features(base_records: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
    return CvdVolumeOrderflowFeatureBuilder().build_market_features(base_records)
