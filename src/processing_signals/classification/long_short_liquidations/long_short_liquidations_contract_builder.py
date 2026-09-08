"""Screen contract builder for long/short liquidations v0.1."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import json
import math
from typing import Any


VALID_STATUS = {"available", "partial", "unavailable", "invalid"}
INTERVALS = {
    "1m": (None, "1m", None), "5m": (None, "5m", None), "15m": (None, "15m", "classifications.events.15m"),
    "4h": ("4h", "4h", "classifications.events.4h"),
}
DEFAULT_SELECTION = {"interval": "15m", "exchange": "aggregate", "map": "aggregate"}
MAP_OPTIONS = {"aggregate"}
REQUIRED_VIEWS = ["current_price", "total_liquidations_24h", "long_liquidations_24h",
                  "short_liquidations_24h", "pressure_score", "realized_side_24h",
                  "aggregate_liquidation_map", "event_activity_15m", "exchange_concentration"]
OPTIONAL_VIEWS = ["selected_realized_side", "selected_realized_imbalance", "estimated_side",
                  "estimated_imbalance", "map_concentrations", "clusters", "event_4h_classification"]
STATUS_RANK = {"available": 0, "partial": 1, "unavailable": 2, "invalid": 3}
CLASS_TOKENS = {
    "low_pressure": "pressure_low", "moderate_pressure": "pressure_moderate",
    "high_pressure": "pressure_high", "extreme_pressure": "pressure_extreme",
    "realized_long_liquidations_dominant": "realized_long_dominant",
    "realized_short_liquidations_dominant": "realized_short_dominant", "realized_balanced": "realized_balanced",
    "estimated_long_exposure_dominant": "estimated_long_dominant",
    "estimated_short_exposure_dominant": "estimated_short_dominant", "estimated_exposure_balanced": "estimated_balanced",
    "subdued_event_activity": "event_subdued", "normal_event_activity": "event_normal",
    "elevated_event_activity": "event_elevated", "high_event_activity": "event_high",
    "extreme_event_activity": "event_extreme", "dispersed": "concentration_dispersed",
    "moderately_concentrated": "concentration_moderate", "concentrated": "concentration_high",
    "highly_concentrated": "concentration_extreme", "provider_aligned": "confirmation_aligned",
    "provider_mixed": "confirmation_mixed", "provider_divergent": "confirmation_divergent",
}
CLASS_LABELS = {
    "realized_long_liquidations_dominant": "Realized Long Liquidations Dominant",
    "realized_short_liquidations_dominant": "Realized Short Liquidations Dominant",
    "realized_balanced": "Realized Balanced", "estimated_long_exposure_dominant": "Estimated Long Exposure Dominant",
    "estimated_short_exposure_dominant": "Estimated Short Exposure Dominant",
    "estimated_exposure_balanced": "Estimated Exposure Balanced",
}


def _error(path: str) -> ValueError:
    return ValueError(f"invalid_contract_input:{path}")


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise _error(path)
    return value


def _json_safe(value: Any, path: str) -> None:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise _error(path)
        for key, item in value.items():
            _json_safe(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _json_safe(item, f"{path}[{index}]")
    elif isinstance(value, float) and not math.isfinite(value):
        raise _error(path)
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise _error(path)


def _at(root: Mapping[str, Any], path: str, default: Any = ...):
    value: Any = root
    walked = []
    for part in path.split("."):
        walked.append(part)
        if not isinstance(value, Mapping) or part not in value:
            if default is not ...:
                return default
            raise _error(".".join(walked))
        value = value[part]
    return value


def _status(value: Any, path: str) -> str:
    if value not in VALID_STATUS:
        raise _error(path)
    return value


def _number(value: Any, path: str, *, integer: bool = False, positive: bool = False) -> float | int:
    expected = int if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, expected) or not math.isfinite(value):
        raise _error(path)
    if positive and value <= 0:
        raise _error(path)
    return value


def _combined_status(*statuses: str) -> str:
    return max(statuses, key=STATUS_RANK.get)


def _usable(status: str) -> bool:
    return status in {"available", "partial"}


def _sanitize_optional_payload(status: str, payload: Any, empty_value: Any) -> Any:
    return deepcopy(payload) if _usable(status) else deepcopy(empty_value)


def _sanitize_components(status: str, components: Any) -> dict[str, Any] | list[Any]:
    if not _usable(status):
        return []
    clean = {}
    for name, component in _mapping(components, "pressure.components").items():
        component = _mapping(component, f"pressure.components.{name}")
        component_status = _status(component.get("status"), f"pressure.components.{name}.status")
        clean[name] = _sanitize_optional_payload(component_status, component, {
            "value": None, "status": component_status, "reason": deepcopy(component.get("reason"))})
    return clean


def _sanitize_cluster_source(status: str, source: Mapping[str, Any]) -> dict[str, Any]:
    if not _usable(status):
        return {"estimated_long": [], "estimated_short": []}
    clean = {}
    for side in ("estimated_long", "estimated_short"):
        branch = source.get(side, [])
        if isinstance(branch, Mapping) and branch.get("status") in VALID_STATUS:
            branch_status = _status(branch.get("status"), f"clusters.{side}.status")
            branch = branch.get("items", branch.get("value", []))
            clean[side] = _sanitize_optional_payload(branch_status, branch, [])
        else:
            clean[side] = deepcopy(branch)
    return clean


def _validate_timestamp_anchor(value: Any, *, path: str, generated_at: int) -> tuple[int | None, str | None]:
    if type(value) is not int:
        return None, "invalid_type"
    if value <= 0:
        return None, "non_positive"
    if value > generated_at:
        return None, "future"
    return value, None


def _with_view_id(view_model: Any, *, view_id: str, label: str | None = None) -> dict[str, Any]:
    source = deepcopy(dict(view_model)) if isinstance(view_model, Mapping) else {}
    source["id"] = view_id
    if label is not None:
        source.setdefault("label", label)
    source.setdefault("status", "unavailable")
    source.setdefault("reason", "view_payload_not_available" if not isinstance(view_model, Mapping) else None)
    return source


def _token(status: str, classification: str | None = None) -> str:
    return CLASS_TOKENS.get(classification, f"status_{status}")


def _format_price(value: float, precision: int) -> str:
    return f"{value:,.{precision}f}"


def _format_usd(value: float) -> str:
    magnitude = abs(value)
    divisor, suffix = ((1e9, "B") if magnitude >= 1e9 else (1e6, "M") if magnitude >= 1e6 else
                       (1e3, "K") if magnitude >= 1e3 else (1., ""))
    return f"${value/divisor:,.2f}{suffix}"


def _format_ratio(value: float) -> str:
    return f"{value*100:+.1f}%"


def _format_score(value: float) -> str:
    return f"{value:.1f}"


def _format_bps(value: float) -> str:
    return f"{value:+.1f} bps"


def _format_level(value: float) -> str:
    return f"{value:.4f}" if abs(value) < 1 else f"{value:.2f}"


def _classification_model(atom: Mapping[str, Any]) -> dict[str, Any]:
    atom = _mapping(atom, "classification_atom")
    status = _status(atom.get("status"), "classification_atom.status")
    classification = atom.get("classification") if _usable(status) else None
    return {"classification": deepcopy(classification), "label": CLASS_LABELS.get(classification,
            str(classification).replace("_", " ").title() if classification else "—"),
            "strength": deepcopy(atom.get("strength")) if _usable(status) else None,
            "confidence": deepcopy(atom.get("confidence", 0.)) if _usable(status) else 0., "status": status,
            "reason": deepcopy(atom.get("reason")), "color_token": _token(status, classification),
            "evidence": deepcopy(atom.get("evidence", {})), "provenance": deepcopy(atom.get("provenance", {}))}


def _scalar(identifier: str, label: str, value: Any, *, unit: str | None, status: str, reason: Any,
            formatter: Any, timestamp: Any = None, classification: Mapping[str, Any] | None = None,
            provenance: Any = None, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    status = _status(status, f"{identifier}.status")
    class_model = _classification_model(classification) if classification is not None else None
    usable = _usable(status)
    if usable and value is not None:
        _number(value, f"{identifier}.value")
    raw = deepcopy(value) if usable else None
    display = formatter(value) if usable and value is not None else "—"
    output = {"id": identifier, "label": label, "value": raw, "display_value": display, "unit": unit,
              "status": status, "reason": deepcopy(reason),
              "classification": class_model["classification"] if class_model and usable else None,
              "confidence": class_model["confidence"] if class_model and usable else 0.,
              "color_token": class_model["color_token"] if class_model and usable else _token(status),
              "timestamp": deepcopy(timestamp), "provenance": deepcopy(provenance or {})}
    output.update(deepcopy(dict(extra or {})))
    return output


def _badge(identifier: str, label: str, active: bool, reason: Any, source_path: str) -> dict[str, Any]:
    return {"id": identifier, "label": label, "status": "active" if active else "inactive",
            "reason": deepcopy(reason), "source_path": source_path}


def _validate_contract(contract: Any, stage: str, name: str) -> Mapping[str, Any]:
    contract = _mapping(contract, name)
    if contract.get("family") != "long_short_liquidations":
        raise _error(f"{name}.family")
    if contract.get("stage") != stage:
        raise _error(f"{name}.stage")
    _number(contract.get("reference_timestamp"), f"{name}.reference_timestamp", integer=True, positive=True)
    quality = _mapping(contract.get("quality"), f"{name}.quality")
    _status(quality.get("status"), f"{name}.quality.status")
    _json_safe(contract, name)
    return contract


def _validate_context(context: Any) -> dict[str, Any]:
    context = _mapping(context, "context")
    for name in ("symbol", "base_asset", "quote_asset"):
        if not isinstance(context.get(name), str) or not context[name]:
            raise _error(f"context.{name}")
    if context.get("market") != "futures":
        raise _error("context.market")
    precision = _number(context.get("price_precision"), "context.price_precision", integer=True)
    if not 0 <= precision <= 12:
        raise _error("context.price_precision")
    _json_safe(context, "context")
    return deepcopy(dict(context))


def _validate_runtime(runtime: Any) -> dict[str, Any]:
    runtime = _mapping(runtime, "runtime_context")
    generated = _number(runtime.get("generated_at"), "runtime_context.generated_at", integer=True, positive=True)
    updated = _number(runtime.get("updated_at"), "runtime_context.updated_at", integer=True, positive=True)
    if updated > generated:
        raise _error("runtime_context.updated_at")
    mode, demo, cache = runtime.get("data_mode"), runtime.get("is_demo"), runtime.get("cache_status")
    if mode not in {"synthetic", "live"}:
        raise _error("runtime_context.data_mode")
    if not isinstance(demo, bool) or demo != (mode == "synthetic"):
        raise _error("runtime_context.is_demo")
    if cache not in {"disabled", "fresh", "stale", "unknown"}:
        raise _error("runtime_context.cache_status")
    _json_safe(runtime, "runtime_context")
    return deepcopy(dict(runtime))


def _validate_selection(selection: Any) -> dict[str, str]:
    selected = {**DEFAULT_SELECTION, **dict(_mapping(selection, "selection"))} if selection is not None else dict(DEFAULT_SELECTION)
    if selected.get("interval") not in INTERVALS:
        raise ValueError("invalid_selection:interval")
    if not isinstance(selected.get("exchange"), str) or not selected["exchange"]:
        raise ValueError("invalid_selection:exchange")
    if selected.get("map") not in MAP_OPTIONS:
        raise ValueError("invalid_selection:map")
    if set(selected) != set(DEFAULT_SELECTION):
        raise ValueError("invalid_selection:keys")
    return selected


def _view_status(source: Mapping[str, Any], path: str) -> tuple[str, Any]:
    return _status(source.get("status"), f"{path}.status"), source.get("reason")


def _kpis(processing: Mapping[str, Any], classification: Mapping[str, Any], context: Mapping[str, Any],
          generated_at: int) -> list[dict[str, Any]]:
    reference = _mapping(_at(processing, "maps.reference_price"), "maps.reference_price")
    ref_status, ref_reason = _view_status(reference, "maps.reference_price")
    current = _scalar("current_price", "Current Price", reference.get("value"), unit=context["quote_asset"],
        status=ref_status, reason=ref_reason, formatter=lambda value: _format_price(value, context["price_precision"]),
        timestamp=_validate_timestamp_anchor(reference.get("timestamp"), path="maps.reference_price.timestamp",
                                             generated_at=generated_at)[0], provenance=reference.get("provenance", {}))
    window = _mapping(_at(processing, "realized.windows.24h"), "realized.windows.24h")
    win_status, win_reason = _view_status(window, "realized.windows.24h")
    common = {"status": win_status, "reason": win_reason, "formatter": _format_usd,
              "timestamp": _validate_timestamp_anchor(window.get("window_end"), path="realized.windows.24h.window_end",
                                                       generated_at=generated_at)[0],
              "provenance": _at(processing, "realized.provenance", {})}
    totals = [_scalar("total_liquidations_24h", "Total Liquidations 24H", window.get("total_usd"), unit="USD", **common,
                     extra={"coverage_ratio": deepcopy(window.get("coverage_ratio")), "window_start": window.get("window_start"), "window_end": window.get("window_end")}),
              _scalar("long_liquidations_24h", "Long Liquidations 24H", window.get("long_total_usd"), unit="USD", **common),
              _scalar("short_liquidations_24h", "Short Liquidations 24H", window.get("short_total_usd"), unit="USD", **common)]
    imbalance = _mapping(window.get("imbalance"), "realized.windows.24h.imbalance")
    atom24 = _mapping(_at(classification, "classifications.realized_side.24h"), "classifications.realized_side.24h")
    imb_status = _combined_status(_status(imbalance.get("status"), "imbalance.status"), _status(atom24.get("status"), "atom24.status"))
    realized_imbalance = _scalar("realized_imbalance_24h", "Realized Imbalance 24H", imbalance.get("value"), unit="ratio",
        status=imb_status, reason=atom24.get("reason") or imbalance.get("reason"), formatter=_format_ratio,
        timestamp=window.get("window_end"), classification=atom24, provenance=atom24.get("provenance"))
    realized_side = {"id": "realized_side_24h", "label": "Realized Dominant Side 24H",
                     **_classification_model(atom24), "timestamp": common["timestamp"]}
    pressure = _mapping(_at(processing, "pressure"), "pressure")
    pressure_atom = _mapping(_at(classification, "classifications.pressure"), "classifications.pressure")
    pressure_status = _combined_status(_status(pressure.get("status"), "pressure.status"), _status(pressure_atom.get("status"), "pressure_atom.status"))
    pressure_kpi = _scalar("pressure_score", "Pressure Score", pressure.get("score"), unit="score", status=pressure_status,
        reason=pressure_atom.get("reason") or pressure.get("reason"), formatter=_format_score,
        timestamp=_validate_timestamp_anchor(processing["reference_timestamp"], path="processing.reference_timestamp",
                                             generated_at=generated_at)[0],
        classification=pressure_atom, provenance=pressure.get("provenance"),
        extra={"components": _sanitize_components(pressure_status, pressure.get("components", {}))})
    return [current, *totals, realized_imbalance, realized_side, pressure_kpi]


def _selectors(selected: Mapping[str, str], processing: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    options = []
    for identifier, (realized, event, class_path) in INTERVALS.items():
        options.append({"id": identifier, "enabled": True, "realized_window": realized, "event_window": event,
                        "map_mode": "snapshot", "classification_paths": [class_path] if class_path else []})
    binance_key = config.get("binance_exchange_key", "Binance")
    by_exchange = _at(processing, "maps.by_exchange", {})
    binance_enabled = isinstance(by_exchange, Mapping) and binance_key in by_exchange
    return {"interval": {"selected": selected["interval"], "options": options},
            "exchange": {"selected": selected["exchange"], "options": ["aggregate", *list(_at(processing, "maps.by_exchange", {}).keys())]},
            "map": {"selected": selected["map"], "options": [
                {"id": "aggregate", "enabled": True},
                {"id": "hyperliquid", "enabled": False, "reason": "source_not_contractually_available"},
                {"id": "binance", "enabled": binance_enabled,
                 "reason": None if binance_enabled else "exchange_map_not_available"}]}}


def _events(processing: Mapping[str, Any], classification: Mapping[str, Any], selected: Mapping[str, str]) -> dict[str, Any]:
    event_window = INTERVALS[selected["interval"]][1]
    source = _mapping(_at(processing, f"events.aggregate.{event_window}"), f"events.aggregate.{event_window}")
    status, reason = _view_status(source, f"events.aggregate.{event_window}")
    atom = _at(classification, "classifications.events.15m") if selected["interval"] == "15m" else None
    return {"id": "selected_event_statistics", "label": f"Observed Liquidation Events {selected['interval'].upper()}",
            "window": event_window, "status": status, "reason": deepcopy(reason),
            "window_start": deepcopy(source.get("window_start")) if _usable(status) else None,
            "window_end": deepcopy(source.get("window_end")) if _usable(status) else None,
            "event_count": deepcopy(source.get("event_count")) if _usable(status) else None,
            "event_usd_total": deepcopy(source.get("event_usd_total")) if _usable(status) else None,
            "event_usd_mean": deepcopy(source.get("event_usd_mean")) if _usable(status) else None,
            "event_usd_median": deepcopy(source.get("event_usd_median")) if _usable(status) else None,
            "event_usd_max": deepcopy(source.get("event_usd_max")) if _usable(status) else None,
            "max_event": deepcopy(source.get("max_event")) if _usable(status) else None,
            "is_lower_bound": bool(source.get("is_lower_bound")) if _usable(status) else False,
            "classification": _classification_model(atom) if atom is not None else None,
            "provenance": deepcopy(_at(processing, "events.provenance", {}))}


def _exchange_table(processing: Mapping[str, Any], classification: Mapping[str, Any]) -> dict[str, Any]:
    source = _mapping(_at(processing, "exchange_distribution"), "exchange_distribution")
    status, reason = _view_status(source, "exchange_distribution")
    rows = []
    if _usable(status):
        for item in source.get("exchanges", []):
            item = _mapping(item, "exchange_distribution.exchanges[]")
            rows.append({"exchange": deepcopy(item.get("exchange")), "exchange_key": deepcopy(item.get("exchange_key")),
                "long_usd": deepcopy(item.get("long_liquidation_usd")), "short_usd": deepcopy(item.get("short_liquidation_usd")),
                "computed_total_usd": deepcopy(item.get("computed_total_usd")), "provider_total_usd": deepcopy(item.get("provider_total_usd")),
                "provider_difference_usd": deepcopy(item.get("provider_total_difference_usd")),
                "provider_difference_ratio": deepcopy(item.get("provider_total_difference_ratio")),
                "exchange_share": deepcopy(item.get("exchange_share")), "status": status})
    return {"id": "exchange_distribution", "status": status, "reason": deepcopy(reason), "rows": rows,
            "concentration": _classification_model(_at(classification, "classifications.concentration.exchanges")),
            "provenance": deepcopy(source.get("provenance", {}))}


def _cluster_payload(processing: Mapping[str, Any], classification: Mapping[str, Any]) -> dict[str, Any]:
    source = _mapping(_at(processing, "maps.aggregated.clusters"), "maps.aggregated.clusters")
    atom = _classification_model(_at(classification, "classifications.clusters"))
    map_source = _mapping(_at(processing, "maps.aggregated"), "maps.aggregated")
    status = _combined_status(_status(map_source.get("status"), "maps.aggregated.status"), atom["status"])
    if not _usable(status):
        atom.update(classification=None, strength=None, confidence=0., status=status,
                    reason=map_source.get("reason") or atom["reason"], color_token=_token(status), evidence={})
    clean_source = _sanitize_cluster_source(status, source)
    return {"source": clean_source, "classification": atom}


def _aggregate_map(processing: Mapping[str, Any], classification: Mapping[str, Any], context: Mapping[str, Any], *,
                   generated_at: int, range_id: str | None = None) -> dict[str, Any]:
    selected_range = str(range_id or "1d").lower()
    maps_root = _mapping(_at(processing, "maps"), "maps")
    agg_ranges = maps_root.get("aggregated_by_range", {}) if isinstance(maps_root.get("aggregated_by_range"), Mapping) else {}
    aligned_ranges = maps_root.get("aligned_exchanges_by_range", {}) if isinstance(maps_root.get("aligned_exchanges_by_range"), Mapping) else {}
    source_candidate = agg_ranges.get(selected_range, maps_root.get("aggregated"))
    aligned_candidate = aligned_ranges.get(selected_range, maps_root.get("aligned_exchanges"))
    source = _mapping(source_candidate, f"maps.aggregated_by_range.{selected_range}")
    aligned = _mapping(aligned_candidate, f"maps.aligned_exchanges_by_range.{selected_range}")
    reference = _mapping(_at(processing, "maps.reference_price"), "maps.reference_price")
    status, reason = _view_status(source, "maps.aggregated")
    ref_status, ref_reason = _view_status(reference, "maps.reference_price")
    ref_timestamp = _validate_timestamp_anchor(reference.get("timestamp"), path="maps.reference_price.timestamp",
                                               generated_at=generated_at)[0]
    reference_view = {"value": deepcopy(reference.get("value")) if _usable(ref_status) else None,
        "display_value": str(reference.get("value")) if _usable(ref_status) and reference.get("value") is not None else "â€”",
        "unit": context["quote_asset"], "classification": None, "confidence": 0., "status": ref_status, "reason": deepcopy(ref_reason),
        "color_token": _token(ref_status), "timestamp": ref_timestamp,
        "provenance": {"source_family": reference.get("source_family"), "source_market": reference.get("source_market"),
            "source_timeframe": reference.get("source_timeframe"), "price_field": reference.get("price_field")}}
    buckets = deepcopy(source.get("buckets", {"status": "unavailable", "reason": "missing_buckets", "items": []}))
    series = []
    items = aligned.get("buckets", {}).get("items", {}) if _usable(_status(aligned.get("status"), "maps.aligned_exchanges.status")) else {}
    for exchange, points in items.items():
        series.append({"exchange": exchange, "status": aligned["status"], "reason": deepcopy(aligned.get("reason")),
                       "unit": "provider_level", "points": deepcopy(points)})
    bucket_items = buckets.get("items", []) if isinstance(buckets, Mapping) else []
    central = [deepcopy(item) for item in bucket_items if isinstance(item, Mapping) and item.get("region") == "central"]
    exchange_points = {item["exchange"]: {point["bucket_index"]: point for point in item["points"]} for item in series}
    raw_long_curve = source.get("curves", {}).get("estimated_long", [])
    raw_short_curve = source.get("curves", {}).get("estimated_short", [])
    long_curve = {item["price"]: item["cumulative_level"] for item in raw_long_curve
                  if isinstance(item, Mapping) and "price" in item and "cumulative_level" in item}
    short_curve = {item["price"]: item["cumulative_level"] for item in raw_short_curve
                   if isinstance(item, Mapping) and "price" in item and "cumulative_level" in item}
    visual_buckets = [{"bucket_index": item["bucket_index"], "price_low": item["lower_price"],
        "price_center": item["center_price"], "price_high": item["upper_price"],
        "bars": {exchange.lower(): exchange_points.get(exchange, {}).get(item["bucket_index"], {}).get("level_total", 0)
            for exchange in exchange_points}, "cumulative_long": long_curve.get(item["center_price"], 0),
        "cumulative_short": short_curve.get(item["center_price"], 0)} for item in bucket_items]
    axes = {"x": {"field": "price_center", "unit": context["quote_asset"], "type": "linear"},
        "bar_axis": {"unit": "provider_level", "side": "left"},
        "cumulative_axis": {"unit": "normalized_level", "side": "right", "range": [0, 1]}}
    return {"id": "aggregate_liquidation_map", "chart_id": "aggregate_liquidation_map", "title": f"{context['base_asset']} Exchange Liquidation Map",
        "map_semantics": "estimated", "map_time_semantics": "snapshot", "provider": "coinglass",
        "current_price": reference_view["value"], "reference_price": reference_view, "axes": axes,
        "bar_series": [{"series_id": exchange.lower(), "label": exchange} for exchange in exchange_points],
        "buckets": visual_buckets if _usable(status) else [], "series_by_exchange": series,
        "provider_levels": deepcopy(source.get("provider_levels", [])) if _usable(status) else [],
        "estimated_long_curve": deepcopy(_curve_points(source.get("curves"), "estimated_long")) if _usable(status) else [],
        "estimated_short_curve": deepcopy(_curve_points(source.get("curves"), "estimated_short")) if _usable(status) else [],
        "estimated_side": _classification_model(_at(classification, "classifications.estimated_side")),
        "central_region": {"items": central if _usable(ref_status) else []}, "clusters": _cluster_payload(processing, classification),
        "concentration": {"source": deepcopy(source.get("concentration", {})),
            "aggregate": _classification_model(_at(classification, "classifications.concentration.aggregate_map")),
            "estimated_long": _classification_model(_at(classification, "classifications.concentration.estimated_long")),
            "estimated_short": _classification_model(_at(classification, "classifications.concentration.estimated_short"))},
        "status": status, "reason": deepcopy(reason), "selected_range": selected_range,
        "ranges": ["1d", "7d", "30d"],
        "provenance": deepcopy(source.get("provenance", {})), "unit": "provider_level",
        "visual_contract": {"renderer": "stacked_bars_plus_dual_cumulative_curves", "reference_line": {"field": "current_price", "label": "CURRENT PRICE"},
            "barmode": "stack", "x_axis": "linear_price", "hmi_calculation": False, "bucket_count": len(visual_buckets)}}


def _curve_points(curves: Mapping[str, Any] | None, side: str) -> list[dict[str, Any]]:
    """Normalize current/legacy cumulative-curve shapes to a point list."""
    if not isinstance(curves, Mapping):
        return []
    raw = curves.get(side, [])
    if isinstance(raw, Mapping):
        raw = raw.get("points", [])
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, Mapping)]


def _providers(processing: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    selection = deepcopy(dict(_mapping(_at(processing, "source_selection"), "source_selection")))
    grouped: dict[str, dict[str, Any]] = {}
    for role, item in selection.items():
        item = _mapping(item, f"source_selection.{role}")
        provider = item.get("provider")
        if not isinstance(provider, str):
            raise _error(f"source_selection.{role}.provider")
        entry = grouped.setdefault(provider, {"provider": provider, "roles": [], "status": "unavailable", "selected": False})
        entry["roles"].append(role)
        entry["selected"] = entry["selected"] or item.get("selected") is True
        status = _status(item.get("status"), f"source_selection.{role}.status")
        if STATUS_RANK[status] < STATUS_RANK[entry["status"]] or len(entry["roles"]) == 1:
            entry["status"] = status
    reference = _mapping(_at(processing, "maps.reference_price"), "maps.reference_price")
    if reference.get("source_family") == "prices_ohlcv":
        grouped["prices_ohlcv"] = {"provider": "prices_ohlcv", "roles": ["reference_price"],
            "status": _status(reference.get("status"), "maps.reference_price.status"), "selected": True}
    return list(grouped.values()), selection


def _quality(processing: Mapping[str, Any], classification: Mapping[str, Any], views: Mapping[str, Any],
             anchors: Mapping[str, tuple[Any, str]], generated_at: int) -> tuple[dict[str, Any], Any, dict[str, Any]]:
    missing = [name for name in REQUIRED_VIEWS if name not in views]
    statuses = {name: views[name]["status"] for name in views if isinstance(views[name], Mapping) and views[name].get("status") in VALID_STATUS}
    invalid = [name for name, status in statuses.items() if status == "invalid"]
    partial = [name for name, status in statuses.items() if status == "partial"]
    unavailable = [name for name, status in statuses.items() if status == "unavailable"]
    required_statuses = [statuses.get(name, "invalid") for name in REQUIRED_VIEWS]
    p_quality = _at(processing, "quality.status")
    c_quality = _at(classification, "quality.status")
    if p_quality == "invalid" or c_quality == "invalid" or missing or any(status == "invalid" for status in required_statuses):
        status = "invalid"
    elif p_quality == "unavailable" or c_quality == "unavailable":
        status = "unavailable"
    elif any(value == "unavailable" for value in required_statuses):
        status = "partial" if any(_usable(value) for value in required_statuses) else "unavailable"
    elif p_quality == "partial" or c_quality == "partial" or any(value == "partial" for value in required_statuses):
        status = "partial"
    else:
        status = "available"
    sanitized, own_warnings, seen = {}, [], set()
    for name in REQUIRED_VIEWS:
        if not _usable(statuses.get(name, "invalid")):
            sanitized[name] = None
            continue
        value, path = anchors[name]
        valid, cause = _validate_timestamp_anchor(value, path=path, generated_at=generated_at)
        sanitized[name] = valid
        issue = (path, cause)
        if cause is not None and issue not in seen:
            if value is None:
                own_warnings.append(f"missing_required_timestamp:{name}")
            elif cause == "future":
                own_warnings.append(f"future_required_timestamp:{name}")
            else:
                own_warnings.append(f"invalid_required_timestamp:{name}:{cause}")
            seen.add(issue)
    missing_timestamps = [name for name in REQUIRED_VIEWS if _usable(statuses.get(name, "invalid")) and sanitized[name] is None]
    if missing_timestamps and status == "available":
        status = "partial"
    valid_anchors = [sanitized[name] for name in REQUIRED_VIEWS if _usable(statuses.get(name, "invalid"))]
    data_as_of = min(valid_anchors) if valid_anchors and not missing_timestamps else None
    return {"status": status, "required_view_models": list(REQUIRED_VIEWS), "optional_view_models": list(OPTIONAL_VIEWS),
        "missing_view_models": missing, "partial_view_models": partial, "unavailable_view_models": unavailable,
        "invalid_view_models": invalid, "warnings": own_warnings,
        "errors": ["missing_required_view_model"] if missing else []}, data_as_of, sanitized


def build_long_short_liquidations_contract(processing_contract: Mapping[str, Any], classification_contract: Mapping[str, Any], *,
                                            context: Mapping[str, Any], runtime_context: Mapping[str, Any],
                                            selection: Mapping[str, Any] | None = None,
                                            config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    processing = _validate_contract(processing_contract, "processing", "processing")
    classification = _validate_contract(classification_contract, "classification", "classification")
    if processing["reference_timestamp"] != classification["reference_timestamp"]:
        raise _error("reference_timestamp")
    screen_context, runtime, selected = _validate_context(context), _validate_runtime(runtime_context), _validate_selection(selection)
    config = deepcopy(dict(_mapping(config, "config"))) if config is not None else {}
    _json_safe(config, "config")
    kpis = _kpis(processing, classification, screen_context, runtime["generated_at"])
    by_id = {item["id"]: item for item in kpis}
    events = _events(processing, classification, selected)
    selected_event_timestamp = events.get("window_end")
    events["window_end"] = _validate_timestamp_anchor(events.get("window_end"),
        path=f"events.aggregate.{events['window']}.window_end", generated_at=runtime["generated_at"])[0]
    event15_source = _mapping(_at(processing, "events.aggregate.15m"), "events.aggregate.15m")
    event15_atom = _mapping(_at(classification, "classifications.events.15m"), "classifications.events.15m")
    event15_status = _combined_status(_status(event15_source.get("status"), "events.aggregate.15m.status"),
                                      _status(event15_atom.get("status"), "classifications.events.15m.status"))
    event15_view = {"id": "event_activity_15m", "status": event15_status, "reason": event15_atom.get("reason") or event15_source.get("reason"),
                    "classification": _classification_model(event15_atom)}
    exchange = _exchange_table(processing, classification)
    aggregate = _aggregate_map(processing, classification, screen_context, generated_at=runtime["generated_at"])
    interval_window = INTERVALS[selected["interval"]][0]
    if interval_window is None:
        selected_side = {"id": "selected_realized_side", "label": "Selected Realized Side", **_classification_model(
            {"classification": None, "strength": None, "confidence": 0., "status": "unavailable",
             "reason": "realized_window_not_available_for_selection", "evidence": {}, "provenance": {}})}
        selected_imbalance = _scalar("selected_realized_imbalance", "Selected Realized Imbalance", None, unit="ratio",
            status="unavailable", reason="realized_window_not_available_for_selection", formatter=_format_ratio)
    else:
        window = _at(processing, f"realized.windows.{interval_window}")
        atom = _at(classification, f"classifications.realized_side.{interval_window}")
        selected_side = {"id": "selected_realized_side", "label": "Selected Realized Side", **_classification_model(atom),
                         "timestamp": _validate_timestamp_anchor(window.get("window_end"),
                            path=f"realized.windows.{interval_window}.window_end", generated_at=runtime["generated_at"])[0]}
        imb = window["imbalance"]
        selected_imbalance = _scalar("selected_realized_imbalance", "Selected Realized Imbalance", imb.get("value"), unit="ratio",
            status=_combined_status(imb["status"], atom["status"]), reason=atom.get("reason") or imb.get("reason"), formatter=_format_ratio,
            timestamp=_validate_timestamp_anchor(window.get("window_end"), path=f"realized.windows.{interval_window}.window_end",
                                                 generated_at=runtime["generated_at"])[0],
            classification=atom, provenance=atom.get("provenance"))
    estimated_atom = _at(classification, "classifications.estimated_side")
    estimated_source = _at(processing, "maps.aggregated.estimated_side_imbalance")
    estimated_side = {"id": "estimated_side", "label": "Estimated Exposure Side", **_classification_model(estimated_atom)}
    estimated_imbalance = _scalar("estimated_imbalance", "Estimated Exposure Imbalance", estimated_source.get("value"), unit="ratio",
        status=_combined_status(estimated_source["status"], estimated_atom["status"]), reason=estimated_atom.get("reason") or estimated_source.get("reason"),
        formatter=_format_ratio, classification=estimated_atom, provenance=estimated_atom.get("provenance"))
    cluster_payload = _cluster_payload(processing, classification)
    clusters_atom = cluster_payload["classification"]
    clusters_source = cluster_payload["source"]
    clusters = {"id": "clusters", "status": clusters_atom["status"], "reason": clusters_atom["reason"],
        "nearest_estimated_long_cluster": deepcopy((clusters_source.get("estimated_long") or [None])[0]),
        "nearest_estimated_short_cluster": deepcopy((clusters_source.get("estimated_short") or [None])[0]),
        "cluster_regime": clusters_atom, "side_strengths": deepcopy(clusters_atom["evidence"].get("side_strengths", {}))}
    concentration = _classification_model(_at(classification, "classifications.concentration.exchanges"))
    map_concentration = _classification_model(_at(classification, "classifications.concentration.aggregate_map"))
    views = {**by_id, "aggregate_liquidation_map": aggregate, "event_activity_15m": event15_view,
             "exchange_concentration": concentration, "selected_realized_side": selected_side,
             "selected_realized_imbalance": selected_imbalance, "estimated_side": estimated_side,
             "estimated_imbalance": estimated_imbalance, "map_concentrations": map_concentration, "clusters": clusters,
             "event_4h_classification": {"status": "unavailable"}}
    distribution_provenance = _at(processing, "exchange_distribution.provenance", {})
    realized_anchor = (_at(processing, "realized.windows.24h.window_end", None), "realized.windows.24h.window_end")
    distribution_anchor = distribution_provenance.get("source_data_as_of")
    distribution_path = "exchange_distribution.provenance.source_data_as_of"
    if distribution_anchor is None:
        distribution_anchor = distribution_provenance.get("snapshot_observed_at")
        distribution_path = "exchange_distribution.provenance.snapshot_observed_at"
    anchors = {"current_price": (_at(processing, "maps.reference_price.timestamp", None), "maps.reference_price.timestamp"),
        "total_liquidations_24h": realized_anchor, "long_liquidations_24h": realized_anchor,
        "short_liquidations_24h": realized_anchor,
        "pressure_score": (processing["reference_timestamp"], "processing.reference_timestamp"),
        "realized_side_24h": realized_anchor,
        "aggregate_liquidation_map": (_at(processing, "maps.aggregated.provenance.source_snapshot_timestamp", None),
                                      "maps.aggregated.provenance.source_snapshot_timestamp"),
        "event_activity_15m": (event15_source.get("window_end"), "events.aggregate.15m.window_end"),
        "exchange_concentration": (distribution_anchor, distribution_path)}
    quality, data_as_of, clean_anchors = _quality(processing, classification, views, anchors, runtime["generated_at"])
    optional_warnings = []
    if _usable(events["status"]):
        _, optional_cause = _validate_timestamp_anchor(selected_event_timestamp,
            path=f"events.aggregate.{events['window']}.window_end", generated_at=runtime["generated_at"])
        if optional_cause is not None and events["window"] != "15m":
            prefix = "missing_optional_timestamp" if selected_event_timestamp is None else "invalid_optional_timestamp"
            optional_warnings.append(f"{prefix}:selected_window_largest_event" +
                                     (f":{optional_cause}" if prefix.startswith("invalid") else ""))
    warnings = list(dict.fromkeys([*deepcopy(_at(processing, "quality.warnings", [])),
        *deepcopy(_at(classification, "quality.warnings", [])), *quality["warnings"], *optional_warnings]))
    errors = list(dict.fromkeys([*deepcopy(_at(processing, "quality.errors", [])),
        *deepcopy(_at(classification, "quality.errors", [])), *quality["errors"]]))
    event_provenance = _at(processing, "events.provenance", {})
    badges = [_badge("synthetic", "Synthetic", runtime["data_mode"] == "synthetic", None, "context.data_mode"),
        _badge("estimated", "Estimated", True, None, "maps.aggregated"),
        _badge("interpolated", "Interpolated", False, None, "maps.aggregated.provenance.interpolation_enabled"),
        _badge("partial", "Partial", quality["status"] == "partial", None, "quality.status"),
        _badge("stale_reference", "Stale Reference", _at(processing, "maps.reference_price.reason", None) == "stale_reference_price",
               _at(processing, "maps.reference_price.reason", None), "maps.reference_price.reason"),
        _badge("truncated_events", "Truncated Events", event_provenance.get("truncation_detected") is True, None,
               "events.provenance.truncation_detected"),
        _badge("lower_bound", "Lower Bound", events["is_lower_bound"], None, f"events.aggregate.{events['window']}.is_lower_bound")]
    providers, source_selection = _providers(processing)
    side_items = [by_id["pressure_score"], selected_side, selected_imbalance, by_id["realized_side_24h"],
        by_id["realized_imbalance_24h"], estimated_side, estimated_imbalance,
        _with_view_id(concentration, view_id="exchange_concentration"),
        _with_view_id(map_concentration, view_id="aggregate_map_concentration"),
        _with_view_id(_classification_model(event15_atom), view_id="event_activity_15m"),
        _with_view_id({"status": events["status"], "reason": events["reason"],
                       "value": deepcopy(events.get("max_event")) if _usable(events["status"]) else None},
                      view_id="selected_window_largest_event"),
        _with_view_id({"status": clusters["status"], "reason": clusters["reason"],
                       "value": clusters["nearest_estimated_long_cluster"]}, view_id="nearest_estimated_long_cluster"),
        _with_view_id({"status": clusters["status"], "reason": clusters["reason"],
                       "value": clusters["nearest_estimated_short_cluster"]}, view_id="nearest_estimated_short_cluster"),
        _with_view_id({"status": quality["status"], "reason": None, "warnings": quality["warnings"],
                       "errors": quality["errors"]}, view_id="screen_quality_summary")]
    result = {"contract_version": "1.3.0-native-liquidations-b", "screen_id": "long_short_liquidations", "family": "long_short_liquidations",
        "stage": "screen_contract_final", "reference_timestamp": processing["reference_timestamp"],
        "context": {**screen_context, "exchange_scope": selected["exchange"], "selected_interval": selected["interval"],
                    "available_intervals": list(INTERVALS)},
        "timestamps": {"generated_at": runtime["generated_at"], "updated_at": runtime["updated_at"], "data_as_of": data_as_of,
            "reference_price_as_of": clean_anchors["current_price"],
            "map_snapshot_as_of": clean_anchors["aggregate_liquidation_map"],
            "events_coverage_as_of": events.get("window_end"),
            "realized_data_as_of": clean_anchors["total_liquidations_24h"],
            "exchange_distribution_as_of": clean_anchors["exchange_concentration"]},
        "mode": runtime["data_mode"],
        "header": {"title": "LONG / SHORT LIQUIDATIONS", "symbol": screen_context["symbol"], "market": screen_context["market"],
            "exchange_scope": selected["exchange"], "selected_interval": selected["interval"], "data_as_of": data_as_of,
            "updated_at": runtime["updated_at"], "badges": deepcopy(badges), "status": quality["status"]},
        "kpis": kpis, "selectors": _selectors(selected, processing, config),
        "charts": {"aggregate_map": aggregate},
        "side_panel": {"id": "liquidation_target_summary", "title": "LIQUIDATION TARGET SUMMARY", "items": side_items},
        "tables": {"exchange_distribution": exchange}, "badges": badges,
        "providers": providers, "source_selection": source_selection,
        "calculation_history": {"source_interval": _at(processing, "realized.provenance.source_interval", "15m"),
            "records_available": len(_at(processing, "realized.series", [])), "fabricated_records": 0,
            "warmup_records": 0, "owner": "Processing"},
        "history_contract": {"source_resolution": "15m", "map_time_semantics": "snapshot",
            "hmi_calculation": False, "fabricated_records": 0},
        "quality": quality, "warnings": warnings, "errors": errors}
    result = align_long_short_liquidations_to_sp_v1_3(result, processing, classification, runtime)
    json.dumps(result, ensure_ascii=False, allow_nan=False)
    return result



# --- Canonical Screen contract shaping ---
from copy import deepcopy

from datetime import datetime, timezone

import json

import math

from pathlib import Path

from typing import Any, Mapping

_screen_VERSION = '1.5.0-liquidations-maps3'

_screen_TEMPLATE_PATH = Path(__file__).with_name('screen_template.json')

_screen_MISSING = object()

def _screen_template() -> dict[str, Any]:
    return json.loads(_screen_TEMPLATE_PATH.read_text(encoding='utf-8'))

def _screen_finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (not math.isfinite(value)):
        return None
    return 0.0 if value == 0 else float(value)

def _screen_iso(timestamp: Any) -> str | None:
    if type(timestamp) is not int or timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace('+00:00', 'Z')

def _screen_shape(reference: Any, candidate: Any=_screen_MISSING) -> Any:
    """Project runtime values onto the exact frozen SP key/nesting shape."""
    if isinstance(reference, dict):
        source = candidate if isinstance(candidate, Mapping) else {}
        return {key: _screen_shape(value, source.get(key, _screen_MISSING)) for key, value in reference.items()}
    if isinstance(reference, list):
        if candidate is _screen_MISSING:
            return deepcopy(reference)
        if not isinstance(candidate, list):
            return deepcopy(reference)
        if not reference:
            return deepcopy(candidate)
        if all((not isinstance(item, (dict, list)) for item in reference)):
            return deepcopy(candidate)
        identity_keys = ('id', 'provider', 'series_id', 'exchange', 'badge_id')

        def ref_for(item: Any, index: int) -> Any:
            if isinstance(item, Mapping):
                for key in identity_keys:
                    value = item.get(key, _screen_MISSING)
                    if value is _screen_MISSING:
                        continue
                    for ref_item in reference:
                        if isinstance(ref_item, Mapping) and ref_item.get(key, _screen_MISSING) == value:
                            return ref_item
            return reference[index] if index < len(reference) else reference[0]
        return [_screen_shape(ref_for(item, index), item) for index, item in enumerate(candidate)]
    return deepcopy(reference if candidate is _screen_MISSING else candidate)

def _screen_kpis(reference: list[Any], candidate: list[Any]) -> list[dict[str, Any]]:
    by_id = {str(item.get('id')): deepcopy(item) for item in candidate if isinstance(item, Mapping)}
    output: list[dict[str, Any]] = []
    for ref in reference:
        identifier = str(ref.get('id'))
        item = deepcopy(by_id.get(identifier, {}))
        item.setdefault('id', identifier)
        item.setdefault('label', ref.get('label'))
        if identifier in {'total_liquidations_24h', 'long_liquidations_24h', 'short_liquidations_24h'}:
            value = _screen_finite(item.get('value'))
            if value is not None:
                item['value'] = value / 1000000.0
                item['display_value'] = f"${item['value']:.1f}M"
                item['unit'] = 'M USD'
        elif identifier == 'current_price':
            value = _screen_finite(item.get('value'))
            if value is not None:
                item['display_value'] = f'${value:,.0f}'
                item['unit'] = 'USDT'
        elif identifier == 'realized_imbalance_24h':
            value = _screen_finite(item.get('value'))
            if value is not None:
                item['display_value'] = f'{value * 100:+.1f}%'
                item['unit'] = 'ratio'
        elif identifier == 'realized_side_24h':
            classification = item.get('classification')
            side = {'realized_long_liquidations_dominant': 'LONGS', 'realized_short_liquidations_dominant': 'SHORTS', 'realized_balanced': 'BALANCED'}.get(classification)
            item.update({'value': side, 'display_value': side or '—', 'unit': 'side'})
        output.append(_screen_shape(ref, item))
    return output

def _screen_filter_aggregate_chart(reference: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    item = deepcopy(dict(candidate))
    item['id'] = reference.get('id')
    item['chart_id'] = reference.get('chart_id')
    item['title'] = reference.get('title')
    allowed = {'binance', 'okx', 'bybit'}
    item['bar_series'] = [row for row in item.get('bar_series', []) if str(row.get('series_id', '')).lower() in allowed]
    buckets = []
    for row in item.get('buckets', []):
        if not isinstance(row, Mapping):
            continue
        copied = deepcopy(dict(row))
        copied['bars'] = {k: v for k, v in copied.get('bars', {}).items() if str(k).lower() in allowed}
        buckets.append(copied)
    item['buckets'] = buckets
    item['provider'] = 'coinglass'
    item['unit'] = 'provider_level'
    item.setdefault('axes', {}).setdefault('bar_axis', {})['unit'] = 'provider_level'
    provenance = item.get('provenance') if isinstance(item.get('provenance'), Mapping) else {}
    item['provenance'] = {'provider': 'coinglass', 'endpoint_id': provenance.get('endpoint_id', 'aggregated_liquidation_map'), 'source_dataset': provenance.get('source_dataset', 'coinglass.aggregated_map'), 'source_snapshot_timestamp': provenance.get('source_snapshot_timestamp'), 'side_assignment_method': provenance.get('side_assignment_method', 'spatial_convention_v1')}
    item.setdefault('visual_contract', {})['bucket_count'] = len(buckets)
    return _screen_shape(reference, item)

def _screen_exchange_chart(reference: Mapping[str, Any], candidate: Mapping[str, Any], *, exchange: str) -> dict[str, Any]:
    item = deepcopy(dict(candidate))
    item['unit'] = 'provider_level'
    item.setdefault('axes', {}).setdefault('bar_axis', {})['unit'] = 'provider_level'
    item.setdefault('visual_contract', {})['bucket_count'] = len(item.get('buckets', []))
    source_provenance = item.get('provenance') if isinstance(item.get('provenance'), Mapping) else {}
    snapshot_ts = source_provenance.get('source_snapshot_timestamp')
    reference_price = item.get('reference_price') if isinstance(item.get('reference_price'), Mapping) else {}
    reference_ts = reference_price.get('timestamp')
    aligned = type(snapshot_ts) is int and type(reference_ts) is int and (abs(snapshot_ts - reference_ts) <= 120)
    if reference_price:
        reference_price = deepcopy(dict(reference_price))
        reference_price['provenance'] = {**(deepcopy(reference_price.get('provenance')) if isinstance(reference_price.get('provenance'), Mapping) else {}), 'map_provider': 'coinglass', 'map_source_dataset': f'coinglass.pair_maps.{exchange}', 'map_data_as_of': snapshot_ts, 'reference_price_as_of': reference_ts, 'temporally_aligned': aligned, 'coverage_bucket_count': len(item.get('buckets', []))}
        item['reference_price'] = reference_price
    item['proxy'] = False
    item['provenance'] = {'provider': 'coinglass', 'endpoint_id': source_provenance.get('endpoint_id', 'pair_liquidation_map'), 'source_dataset': f'coinglass.pair_maps.{exchange}', 'source_snapshot_timestamp': snapshot_ts, 'side_assignment_method': source_provenance.get('side_assignment_method', 'spatial_convention_v1'), 'proxy': False}
    return _screen_shape(reference, item)

def _screen_pair_map_chart(reference: Mapping[str, Any], processing: Mapping[str, Any], *, exchange: str, range_id: str | None = None) -> dict[str, Any]:
    selected_range = str(range_id or '1d').lower()
    maps = processing.get('maps', {}) if isinstance(processing.get('maps'), Mapping) else {}
    all_ranges = maps.get('by_exchange_by_range', {}) if isinstance(maps.get('by_exchange_by_range'), Mapping) else {}
    by_exchange = all_ranges.get(selected_range, maps.get('by_exchange', {}))
    by_exchange = by_exchange if isinstance(by_exchange, Mapping) else {}
    source = by_exchange.get(exchange, {}) if isinstance(by_exchange.get(exchange), Mapping) else {}
    reference_payload = maps.get('reference_price', {}) if isinstance(maps.get('reference_price'), Mapping) else {}
    ref_value = _screen_finite(reference_payload.get('value'))
    bucket_block = source.get('buckets', {}) if isinstance(source.get('buckets'), Mapping) else {}
    bucket_items = bucket_block.get('items', []) if isinstance(bucket_block.get('items'), list) else []
    curves = source.get('curves', {}) if isinstance(source.get('curves'), Mapping) else {}
    def curve_map(name: str) -> dict[float, float]:
        raw = curves.get(name, [])
        if isinstance(raw, Mapping):
            raw = raw.get('points', [])
        out = {}
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, Mapping) and isinstance(item.get('price'), (int, float)) and isinstance(item.get('cumulative_level'), (int, float)):
                    out[float(item['price'])] = float(item['cumulative_level'])
        return out
    long_curve, short_curve = curve_map('estimated_long'), curve_map('estimated_short')
    visual_buckets = []
    for item in bucket_items:
        if not isinstance(item, Mapping):
            continue
        center = item.get('center_price')
        if not isinstance(center, (int, float)):
            continue
        if exchange == 'Hyperliquid':
            bars = {
                'long': item.get('level_total', 0) if ref_value is not None and float(center) < float(ref_value) else 0,
                'short': item.get('level_total', 0) if ref_value is not None and float(center) > float(ref_value) else 0,
            }
        elif exchange == 'Binance':
            breakdown = item.get('leverage_breakdown', {}) if isinstance(item.get('leverage_breakdown'), Mapping) else {}
            bars = {f'{lev}x': float(breakdown.get(str(lev), breakdown.get(f'{float(lev):.1f}', 0)) or 0) for lev in (10, 25, 50, 100)}
        else:
            bars = {exchange.lower(): item.get('level_total', 0)}
        visual_buckets.append({
            'bucket_index': item.get('bucket_index'), 'price_low': item.get('lower_price'),
            'price_center': center, 'price_high': item.get('upper_price'),
            'bars': bars,
            'cumulative_long': long_curve.get(float(center), 0),
            'cumulative_short': short_curve.get(float(center), 0),
        })
    status = source.get('status', 'unavailable')
    ref_status = reference_payload.get('status', 'unavailable')
    ref_view = {
        'value': ref_value, 'display_value': f'{ref_value:,.2f}' if ref_value is not None else '—',
        'unit': 'USDT', 'classification': None, 'confidence': 0.0, 'status': ref_status,
        'reason': reference_payload.get('reason'), 'color_token': 'status_available' if ref_value is not None else 'status_unavailable',
        'timestamp': reference_payload.get('timestamp'), 'provenance': deepcopy(reference_payload.get('provenance', {})) if isinstance(reference_payload.get('provenance'), Mapping) else {},
    }
    candidate = {
        'id': reference.get('id'), 'chart_id': reference.get('chart_id'), 'title': reference.get('title'),
        'status': status if visual_buckets else 'unavailable', 'reason': source.get('reason') if not visual_buckets else None,
        'map_semantics': 'estimated', 'map_time_semantics': 'snapshot', 'provider': 'coinglass',
        'selected_range': selected_range, 'ranges': ['1d','7d','30d'],
        'current_price': ref_value, 'reference_price': ref_view,
        'axes': {'x': {'field':'price_center','unit':'USDT','type':'linear'}, 'bar_axis': {'unit':'provider_level','side':'left'}, 'cumulative_axis': {'unit':'provider_level','side':'right'}},
        'bar_series': ([{'series_id':'long','label':'Long'}, {'series_id':'short','label':'Short'}]
                       if exchange == 'Hyperliquid' else
                       [{'series_id':f'{lev}x','label':f'{lev}x Leverage'} for lev in (10,25,50,100)]
                       if exchange == 'Binance' else
                       [{'series_id': exchange.lower(), 'label': exchange}]),
        'buckets': visual_buckets, 'unit': 'provider_level',
        'provenance': deepcopy(source.get('provenance', {})) if isinstance(source.get('provenance'), Mapping) else {},
        'visual_contract': {'renderer':'stacked_bars_plus_dual_cumulative_curves','reference_line':{'field':'current_price','label':'CURRENT PRICE'},'barmode':'stack','x_axis':'linear_price','hmi_calculation':False,'bucket_count':len(visual_buckets)},
    }
    return _screen_exchange_chart(reference, candidate, exchange=exchange)

def _screen_attach_range_blocks(variants: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    ranges = ["1d", "7d", "30d"]
    base = deepcopy(dict(variants.get("1d", {})))
    base["selected_range"] = "1d"
    base["ranges"] = ranges
    keys = ("status", "reason", "current_price", "bar_series", "buckets", "reference_price", "provenance", "unit", "visual_contract")
    base["range_blocks"] = {
        range_id: {key: deepcopy(variants.get(range_id, {}).get(key)) for key in keys}
        for range_id in ranges
    }
    return base


def _screen_exchange_distribution_table(reference: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    item = deepcopy(dict(candidate))
    rows = [deepcopy(dict(row)) for row in item.get('rows', []) if isinstance(row, Mapping) and str(row.get('exchange_key', '')).lower() not in {'all', 'all_exchange', 'aggregate'}]
    item['rows'] = rows
    return _screen_shape(reference, item)

def _screen_side_panel(reference: Mapping[str, Any], candidate: Mapping[str, Any], processing: Mapping[str, Any]) -> dict[str, Any]:
    kpis = {str(item.get('id')): item for item in candidate.get('kpis', []) if isinstance(item, Mapping)}
    pressure = kpis.get('pressure_score', {})
    imbalance = kpis.get('realized_imbalance_24h', {})
    side = kpis.get('realized_side_24h', {})
    classification = pressure.get('classification')
    pressure_state = {'low_pressure': ('low', 'Baja'), 'moderate_pressure': ('moderate', 'Moderada'), 'high_pressure': ('high', 'Alta'), 'extreme_pressure': ('extreme', 'Extrema')}.get(classification, (None, '—'))
    realized_side = {'realized_long_liquidations_dominant': 'LONGS', 'realized_short_liquidations_dominant': 'SHORTS', 'realized_balanced': 'BALANCED'}.get(side.get('classification'))
    rows = candidate.get('tables', {}).get('exchange_distribution', {}).get('rows', [])
    exchange_rows = [row for row in rows if isinstance(row, Mapping) and str(row.get('exchange_key', '')).lower() != 'all']
    total = sum((float(row.get('computed_total_usd', 0) or 0) for row in exchange_rows))
    top_share = max((float(row.get('computed_total_usd', 0) or 0) / total for row in exchange_rows), default=None) if total > 0 else None
    event24 = processing.get('events', {}).get('aggregate', {}).get('24h', {})
    event4h = processing.get('events', {}).get('aggregate', {}).get('4h', {})
    max_event = event24.get('max_event') if isinstance(event24.get('max_event'), Mapping) else None
    max_event_usd = _screen_finite(max_event.get('usd_value')) if max_event else None
    spike_usd = _screen_finite(event4h.get('event_usd_total'))
    agg = processing.get('maps', {}).get('aggregated', {})
    clusters = agg.get('clusters', {}) if isinstance(agg, Mapping) else {}
    long_clusters = clusters.get('estimated_long', []) if isinstance(clusters, Mapping) else []
    short_clusters = clusters.get('estimated_short', []) if isinstance(clusters, Mapping) else []

    def cluster_price(rows: Any) -> float | None:
        if not isinstance(rows, list) or not rows:
            return None
        row = rows[0]
        if not isinstance(row, Mapping):
            return None
        for key in ('weighted_centroid_price', 'peak_bucket_price', 'center_price', 'price', 'cluster_center_price'):
            value = _screen_finite(row.get(key))
            if value is not None:
                return value
        return None
    long_price = cluster_price(long_clusters)
    short_price = cluster_price(short_clusters)
    score = _screen_finite(pressure.get('value'))
    side_imbalance = _screen_finite(imbalance.get('value'))
    dynamic = {'pressure_score': {'id': 'pressure_score', 'label': 'Pressure Score', 'value': score, 'display_value': f'{score:.2f}' if score is not None else '—', 'unit': 'score', 'status': pressure.get('status', 'unavailable')}, 'pressure_label': {'id': 'pressure_label', 'label': 'Pressure Label', 'value': pressure_state[0], 'display_value': pressure_state[1], 'unit': 'state', 'status': pressure.get('status', 'unavailable')}, 'dominant_side': {'id': 'dominant_side', 'label': 'Dominant Side', 'value': realized_side, 'display_value': realized_side or '—', 'unit': 'side', 'status': side.get('status', 'unavailable')}, 'side_imbalance': {'id': 'side_imbalance', 'label': 'Side Imbalance', 'value': side_imbalance, 'display_value': f'{side_imbalance:+.4f}' if side_imbalance is not None else '—', 'unit': 'ratio', 'status': imbalance.get('status', 'unavailable')}, 'top_exchange_concentration': {'id': 'top_exchange_concentration', 'label': 'Top Exchange Conc.', 'value': top_share, 'display_value': f'{top_share:.4f}' if top_share is not None else '—', 'unit': 'ratio', 'status': 'available' if top_share is not None else 'unavailable'}, 'max_event_spike': {'id': 'max_event_spike', 'label': 'Max Event Spike', 'value': spike_usd / 1000000.0 if spike_usd is not None else None, 'display_value': f'${spike_usd / 1000000.0:.2f}M' if spike_usd is not None else '—', 'unit': 'M USD', 'status': event4h.get('status', 'unavailable')}, 'max_single_liquidation': {'id': 'max_single_liquidation', 'label': 'Max Single Liq.', 'value': max_event_usd / 1000000.0 if max_event_usd is not None else None, 'display_value': f"{max_event.get('exchange')} ${max_event_usd / 1000000.0:.2f}M" if max_event_usd is not None else '—', 'unit': 'M USD', 'status': event24.get('status', 'unavailable')}, 'nearest_long_cluster': {'id': 'nearest_long_cluster', 'label': 'Nearest Long Cluster', 'value': long_price, 'display_value': f'${long_price:,.0f}' if long_price is not None else '—', 'unit': 'USDT', 'status': 'available' if long_price is not None else 'unavailable'}, 'nearest_short_cluster': {'id': 'nearest_short_cluster', 'label': 'Nearest Short Cluster', 'value': short_price, 'display_value': f'${short_price:,.0f}' if short_price is not None else '—', 'unit': 'USDT', 'status': 'available' if short_price is not None else 'unavailable'}}
    items = [_screen_shape(ref, dynamic.get(str(ref.get('id')), {})) for ref in reference.get('items', [])]
    return {'id': reference.get('id'), 'title': reference.get('title'), 'items': items}

def _screen_selectors(reference: Mapping[str, Any], charts: Mapping[str, Any]) -> dict[str, Any]:
    del reference, charts
    return {
        'exchange': {'selected': 'Binance', 'options': ['Binance', 'OKX', 'Bybit']},
        'timeframe': {'selected': '15m', 'options': ['1m', '5m', '15m', '4h']},
        'range': {'selected': '1d', 'options': ['1d', '7d', '30d']},
    }

def _screen_history(reference: Mapping[str, Any], processing: Mapping[str, Any], runtime_context: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    series = [row for row in processing.get('realized', {}).get('series', []) if isinstance(row, Mapping)]
    totals = [float(row.get('total_liquidation_usd', 0) or 0) for row in series]
    max_total = max(totals) if totals else 0.0
    current_price = _screen_finite(processing.get('maps', {}).get('reference_price', {}).get('value'))
    agg = processing.get('maps', {}).get('aggregated', {})
    clusters = agg.get('clusters', {}) if isinstance(agg, Mapping) else {}

    def nearest_distance(side: str) -> float | None:
        if current_price is None or not isinstance(clusters, Mapping):
            return None
        rows = clusters.get(side, [])
        if not isinstance(rows, list) or not rows:
            return None
        values = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            price = _screen_finite(row.get('center_price', row.get('price')))
            if price is not None:
                values.append(abs(price / current_price - 1.0) * 100.0)
        return min(values) if values else None
    long_distance = nearest_distance('estimated_long')
    short_distance = nearest_distance('estimated_short')
    records = []
    for row in series:
        long_v = _screen_finite(row.get('long_liquidation_usd')) or 0.0
        short_v = _screen_finite(row.get('short_liquidation_usd')) or 0.0
        total = long_v + short_v
        imbalance = 0.0 if total == 0 else (long_v - short_v) / total
        records.append({'timestamp': row.get('timestamp'), 'price': current_price, 'long_liquidation_intensity': 0.0 if max_total == 0 else long_v / max_total, 'short_liquidation_intensity': 0.0 if max_total == 0 else short_v / max_total, 'liquidation_imbalance': imbalance, 'nearest_long_cluster_distance_percent': long_distance, 'nearest_short_cluster_distance_percent': short_distance, 'estimated_long_liquidation_usd': long_v, 'estimated_short_liquidation_usd': short_v, 'is_synthetic': bool(runtime_context.get('is_demo'))})
    calculation = {'record_count': len(records), 'resolution': '15m', 'records': records, 'purpose': deepcopy(reference.get('calculation_history', {}).get('purpose', [])), 'map_bucket_semantics': 'Current maps are spatial price buckets; temporal selector intentionally absent.'}
    history = deepcopy(dict(reference.get('history_contract', {})))
    history.update({'calculation_records': len(records), 'minimum_warmup_records': 200, 'maximum_standard_indicator_period': 200, 'all_visible_moving_averages_warm': len(records) >= 200, 'technical_indicators_precomputed': True, 'hmi_recalculation': False, 'synthetic_fixture': False, 'fixture_seed': None, 'resolution': '15m', 'note': f'{len(records)} temporal liquidation-context snapshots; map buckets remain spatial price buckets.'})
    return (_screen_shape(reference.get('history_contract', {}), history), _screen_shape(reference.get('calculation_history', {}), calculation))

def _screen_quality(reference: Mapping[str, Any], candidate: Mapping[str, Any], charts: Mapping[str, Any], runtime_context: Mapping[str, Any], history_count: int) -> dict[str, Any]:
    out = deepcopy(dict(reference))
    status = candidate.get('quality', {}).get('status', 'unavailable')
    bucket_counts = {key: len(value.get('buckets', [])) for key, value in charts.items() if isinstance(value, Mapping)}
    missing = [name for name, value in charts.items() if value.get('status') not in {'available', 'partial'}]
    out.update({'status': status, 'synthetic_fixture': False, 'required_view_models': ['current_price', 'hyperliquid_map', 'exchange_maps', 'binance_map', 'long_short_positioning'], 'missing_view_models': missing, 'warnings': deepcopy(candidate.get('quality', {}).get('warnings', [])), 'errors': deepcopy(candidate.get('quality', {}).get('errors', [])), 'visual_fixture': {'status': status, 'synthetic': bool(runtime_context.get('is_demo')), 'bucket_count': max(bucket_counts.values(), default=0), 'maps': list(charts), 'hmi_calculation': False}})
    ext = deepcopy(out.get('extensions', {}))
    if 'temporal_history_730_v1' in ext:
        ext['temporal_history_730_v1'].update({'status': 'available' if history_count else 'unavailable', 'temporal_records': history_count, 'map_bucket_count_per_map': bucket_counts, 'buckets_are_spatial_not_temporal': True})
    if 'realism_v1' in ext:
        ext['realism_v1'].update({'status': 'available' if status in {'available', 'partial'} else status, 'fixture_as_of_timestamp': None, 'fixture_as_of_iso': None, 'deterministic_seed': None, 'synthetic_not_live': bool(runtime_context.get('is_demo')), 'bucket_count_per_map': max(bucket_counts.values(), default=0), 'temporal_history_records': history_count, 'temporal_selector': 'NONE', 'common_as_of_contract': _screen_iso(candidate.get('reference_timestamp'))})
    out['extensions'] = ext
    shaped = _screen_shape(reference, out)
    badge_state = {str(row.get('id')): bool(row.get('active')) for row in candidate.get('badges', []) if isinstance(row, Mapping)}
    shaped.setdefault('extensions', {})['event_badge_contract_v1'] = {'truncated_events': {'active': badge_state.get('truncated_events', False), 'semantic': 'true_when_event_stream_is_truncated', 'processing_must_populate': True}, 'lower_bound': {'active': badge_state.get('lower_bound', False), 'semantic': 'true_when_reported_liquidation_aggregate_is_only_a_lower_bound', 'processing_must_populate': True}}
    return shaped

def _screen_positioning_chart(reference: Mapping[str, Any], processing: Mapping[str, Any], runtime_context: Mapping[str, Any]) -> dict[str, Any]:
    native = processing.get('liquidation_analysis', {}) if isinstance(processing.get('liquidation_analysis'), Mapping) else {}
    history_cube = native.get('positioning_history_by_exchange_timeframe', {}) if isinstance(native.get('positioning_history_by_exchange_timeframe'), Mapping) else {}

    def _point_from_ratio_row(row: Mapping[str, Any]) -> dict[str, Any] | None:
        if type(row.get('timestamp')) is not int:
            return None
        value = _screen_finite(row.get('long_short_ratio'))
        long_share = None if value is None or value <= 0 else value / (1.0 + value)
        short_share = None if long_share is None else 1.0 - long_share
        return {
            'timestamp': int(row['timestamp']),
            'data_mode': 'synthetic_emulator' if runtime_context.get('is_demo') else 'live_provider',
            'long_short_ratio': value,
            'long_share': long_share,
            'short_share': short_share,
            'long_percent': None if long_share is None else long_share * 100.0,
            'short_percent': None if short_share is None else short_share * 100.0,
        }

    series_by_exchange: dict[str, Any] = {}
    any_points = False
    for exchange in ('Binance', 'OKX', 'Bybit'):
        tf_blocks: dict[str, Any] = {}
        source_exchange = history_cube.get(exchange, {}) if isinstance(history_cube.get(exchange), Mapping) else {}
        for timeframe in ('1m', '5m', '15m', '4h'):
            source_tf = source_exchange.get(timeframe, {}) if isinstance(source_exchange.get(timeframe), Mapping) else {}
            points = []
            for row in source_tf.get('points', [])[-500:]:
                if isinstance(row, Mapping):
                    point = _point_from_ratio_row(row)
                    if point is not None:
                        points.append(point)
            any_points = any_points or bool(points)
            tf_blocks[timeframe] = {'timeframe': timeframe, 'status': 'available' if points else 'unavailable', 'points': points, 'current': deepcopy(points[-1]) if points else None}
        series_by_exchange[exchange] = {'exchange': exchange, 'selected_timeframe': '15m', 'series_by_timeframe': tf_blocks}

    default_points = deepcopy(series_by_exchange['Binance']['series_by_timeframe']['15m']['points'])
    candidate = {'id':'long_short_positioning','chart_id':'long_short_positioning','title':'LONG / SHORT POSITIONING','status':'available' if any_points else 'unavailable','unit':'ratio','exchange':'Binance','timeframe':'15m','selected_timeframe':'15m','timeframes':['1m','5m','15m','4h'],'primary_series':'long_short_ratio','reference_value':1.0,'data_mode':'synthetic_emulator' if runtime_context.get('is_demo') else 'live_provider','processing_contract_target':True,'real_market_calculation':not bool(runtime_context.get('is_demo')),'points':default_points,'series_by_exchange':series_by_exchange,'series_semantics':deepcopy(reference.get('series_semantics', {}))}
    return _screen_shape(reference, candidate)

def _screen_realized_liquidations_chart(reference: Mapping[str, Any], processing: Mapping[str, Any]) -> dict[str, Any]:
    rows = processing.get('realized', {}).get('series', []) if isinstance(processing.get('realized'), Mapping) else []
    points = []
    for row in rows[-500:]:
        if not isinstance(row, Mapping) or type(row.get('timestamp')) is not int:
            continue
        points.append({'timestamp':int(row['timestamp']),'long_liquidation_usd':_screen_finite(row.get('long_liquidation_usd')),'short_liquidation_usd':_screen_finite(row.get('short_liquidation_usd')),'total_liquidation_usd':_screen_finite(row.get('total_liquidation_usd'))})
    candidate={'id':'realized_liquidations','chart_id':'realized_liquidations','title':'REALIZED LIQUIDATIONS','status':'available' if points else 'unavailable','reason':None if points else 'source_unavailable','unit':'USD','timeframe':'15m','points':points,'series_semantics':deepcopy(reference.get('series_semantics', {})),'provenance':deepcopy(processing.get('realized', {}).get('provenance', {})) if isinstance(processing.get('realized'), Mapping) else {}}
    return _screen_shape(reference, candidate)

def _screen_liquidation_pressure_chart(reference: Mapping[str, Any], processing: Mapping[str, Any]) -> dict[str, Any]:
    native = processing.get('liquidation_analysis', {}) if isinstance(processing.get('liquidation_analysis'), Mapping) else {}
    timestamps = list(native.get('timestamps', []))[-500:]
    indicators = native.get('indicators', {}) if isinstance(native.get('indicators'), Mapping) else {}
    crowd = indicators.get('crowding_liquidation_pressure', {}) if isinstance(indicators.get('crowding_liquidation_pressure'), Mapping) else {}
    pressure_values = list(crowd.get('liquidation_pressure_score', []))[-len(timestamps):] if timestamps else []
    combined_values = list(crowd.get('crowding_liquidation_score', []))[-len(timestamps):] if timestamps else []
    points=[]
    for idx,ts in enumerate(timestamps):
        points.append({'timestamp':int(ts),'liquidation_pressure_score':_screen_finite(pressure_values[idx]) if idx < len(pressure_values) else None,'crowding_liquidation_score':_screen_finite(combined_values[idx]) if idx < len(combined_values) else None})
    current = _screen_finite(processing.get('pressure', {}).get('score')) if isinstance(processing.get('pressure'), Mapping) else None
    candidate={'id':'liquidation_pressure','chart_id':'liquidation_pressure','title':'LIQUIDATION PRESSURE','status':'available' if points or current is not None else 'unavailable','reason':None if points or current is not None else 'source_unavailable','unit':'score','points':points,'current_pressure_score':current,'series_semantics':deepcopy(reference.get('series_semantics', {})),'provenance':deepcopy(processing.get('pressure', {}).get('provenance', {})) if isinstance(processing.get('pressure'), Mapping) else {}}
    return _screen_shape(reference, candidate)

def _screen_regime_label(value: Any) -> str | None:
    score = _screen_finite(value)
    if score is None:
        return None
    if score >= 0.65:
        return 'SHORT_SQUEEZE'
    if score <= -0.65:
        return 'LONG_SQUEEZE_FLUSH'
    if abs(score) <= 0.2:
        return 'CALM'
    if abs(score) >= 0.45:
        return 'BUILDING_PRESSURE'
    return 'NORMAL'

def _screen_native_analysis(reference: Mapping[str, Any], processing: Mapping[str, Any], runtime_context: Mapping[str, Any]) -> dict[str, Any]:
    native = processing.get('liquidation_analysis', {}) if isinstance(processing.get('liquidation_analysis'), Mapping) else {}
    timestamps = list(native.get('timestamps', []))[-730:]
    indicators = native.get('indicators', {}) if isinstance(native.get('indicators'), Mapping) else {}
    built = deepcopy(dict(reference))
    built.update({'status': native.get('status', 'unavailable'), 'data_mode': 'synthetic' if runtime_context.get('is_demo') else 'live', 'processing_contract_target': True, 'real_market_calculation': not bool(runtime_context.get('is_demo')), 'hmi_recalculate': False})
    for iid in reference.get('indicator_order', []):
        ref_ind = reference.get('indicators', {}).get(iid, {})
        src = indicators.get(iid, {}) if isinstance(indicators.get(iid), Mapping) else {}
        point_ref = (ref_ind.get('points') or [{}])[0]
        fields = [str(spec.get('field')) for spec in ref_ind.get('series', []) if isinstance(spec, Mapping) and spec.get('field')]
        if not fields:
            fields = [key for key in point_ref if key not in {'timestamp', 'regime'}]
        points = []
        for index, ts in enumerate(timestamps):
            row = {'timestamp': ts}
            for field in fields:
                values = src.get(field, []) if isinstance(src.get(field), list) else []
                row[field] = values[-len(timestamps) + index] if len(values) >= len(timestamps) else values[index] if index < len(values) else None
            if iid == 'liquidation_regime_hmi' or 'regime' in point_ref:
                score = row.get('liquidation_regime_score')
                row['regime'] = _screen_regime_label(score)
            points.append(row)
        candidate = deepcopy(dict(ref_ind))
        candidate['status'] = 'available' if points else 'unavailable'
        candidate['points'] = points
        built['indicators'][iid] = _screen_shape(ref_ind, candidate)
    return _screen_shape(reference, built)

def align_long_short_liquidations_to_sp_v1_3(candidate: Mapping[str, Any], processing: Mapping[str, Any], classification: Mapping[str, Any], runtime_context: Mapping[str, Any]) -> dict[str, Any]:
    ref = _screen_template()
    is_demo = bool(runtime_context.get('is_demo'))
    context = deepcopy(dict(ref['context']))
    context.update({'symbol': candidate.get('context', {}).get('symbol', 'BTCUSDT'), 'base_asset': candidate.get('context', {}).get('base_asset', 'BTC'), 'quote_asset': candidate.get('context', {}).get('quote_asset', 'USDT'), 'market': candidate.get('context', {}).get('market', 'futures'), 'price_precision': candidate.get('context', {}).get('price_precision', 2), 'exchange_scope': 'aggregate', 'selected_interval': '15m', 'available_intervals': ['1m', '5m', '15m', '4h'], 'fixture_as_of_timestamp': None, 'fixture_as_of_iso': None, 'data_mode': runtime_context.get('data_mode'), 'synthetic_fixture': False, 'realism_refactor_version': 'runtime_emulator_v1' if is_demo else 'runtime_provider_v1', 'realism_note': 'CoinGlass final-33 liquidation primitives only; HMI performs no liquidation calculations.'})
    public_ranges = ('1d', '7d', '30d')
    hyper_variants = {r: _screen_pair_map_chart(ref['charts']['hyperliquid_map'], processing, exchange='Hyperliquid', range_id=r) for r in public_ranges}
    binance_variants = {r: _screen_pair_map_chart(ref['charts']['binance_map'], processing, exchange='Binance', range_id=r) for r in public_ranges}
    generated_at = runtime_context.get('generated_at')
    if type(generated_at) is not int:
        generated_at = int(candidate.get('reference_timestamp') or processing.get('reference_timestamp') or 1)
    screen_context = candidate.get('context', {}) if isinstance(candidate.get('context'), Mapping) else {}
    aggregate_variants = {r: _screen_filter_aggregate_chart(
        ref['charts']['exchange_maps'],
        _aggregate_map(processing, classification, screen_context, generated_at=generated_at, range_id=r),
    ) for r in public_ranges}
    charts = {
        'hyperliquid_map': _screen_attach_range_blocks(hyper_variants),
        'exchange_maps': _screen_attach_range_blocks(aggregate_variants),
        'binance_map': _screen_attach_range_blocks(binance_variants),
        'long_short_positioning': _screen_positioning_chart(ref['charts']['long_short_positioning'], processing, runtime_context),
    }
    history_contract, calculation_history = _screen_history(ref, processing, runtime_context)
    history_count = calculation_history.get('record_count', 0)
    source_selection = deepcopy(candidate.get('source_selection', {}))
    for name in ('exchange_maps', 'events'):
        if name in source_selection and isinstance(source_selection[name], Mapping):
            status = source_selection[name].get('status')
            source_selection[name]['selected'] = status in {'available', 'partial'}
    if 'exchange_maps' in source_selection:
        source_selection['exchange_maps']['role'] = 'screen_a_pair_maps'
    providers = deepcopy(candidate.get('providers', []))
    allowed_badge_ids = {item.get('id') for item in ref.get('badges', []) if isinstance(item, Mapping)}
    public_badges = [deepcopy(item) for item in candidate.get('badges', [])
                     if isinstance(item, Mapping) and item.get('id') in allowed_badge_ids]
    built = {'contract_version': _screen_VERSION, 'screen_id': 'long_short_liquidations', 'family': 'long_short_liquidations', 'stage': 'screen_contract_final', 'reference_timestamp': candidate.get('reference_timestamp'), 'context': context, 'timestamps': deepcopy(candidate.get('timestamps', {})), 'mode': runtime_context.get('data_mode'), 'header': deepcopy(candidate.get('header', {})), 'kpis': _screen_kpis(ref['kpis'], [*candidate.get('kpis', []), {'id': 'long_short_position_ratio', 'label': 'Long/Short Ratio', 'value': processing.get('liquidation_analysis', {}).get('current', {}).get('long_short_ratio'), 'status': 'available', 'unit': 'ratio'}, {'id': 'crowding_score', 'label': 'Crowding Score', 'value': next((v for v in reversed(processing.get('liquidation_analysis', {}).get('indicators', {}).get('crowding_liquidation_pressure', {}).get('crowding_score', [])) if v is not None), None), 'status': 'available', 'unit': 'score'}]), 'selectors': _screen_selectors(ref['selectors'], charts), 'charts': charts, 'side_panel': _screen_side_panel(ref['side_panel'], candidate, processing), 'tables': {'exchange_distribution': _screen_exchange_distribution_table(ref['tables']['exchange_distribution'], candidate.get('tables', {}).get('exchange_distribution', {}))}, 'badges': public_badges, 'providers': providers, 'source_selection': _screen_shape(ref['source_selection'], source_selection), 'quality': {}, 'warnings': deepcopy(candidate.get('warnings', [])), 'errors': deepcopy(candidate.get('errors', [])), 'history_contract': history_contract, 'calculation_history': calculation_history, 'liquidation_analysis': _screen_native_analysis(ref['liquidation_analysis'], processing, runtime_context), 'screen_layout': deepcopy(ref['screen_layout'])}
    built['header']['badges'] = deepcopy(built['badges'])
    built['header']['status'] = candidate.get('quality', {}).get('status', 'unavailable')
    built['quality'] = _screen_quality(ref['quality'], candidate, charts, runtime_context, history_count)
    shaped = _screen_shape(ref, built)
    native_current = processing.get('liquidation_analysis', {}).get('current', {})
    side_by_id = {item.get('id'): item for item in shaped.get('side_panel', {}).get('items', []) if isinstance(item, Mapping)}
    ls_ratio = native_current.get('long_short_ratio')
    if 'long_short_ratio' in side_by_id:
        side_by_id['long_short_ratio'].update({'value': ls_ratio, 'display_value': f'{float(ls_ratio):.3f}' if ls_ratio is not None else '—', 'unit': 'ratio', 'status': 'available' if ls_ratio is not None else 'unavailable'})
    regime = _screen_regime_label(native_current.get('liquidation_regime_score'))
    if 'liquidation_regime' in side_by_id:
        side_by_id['liquidation_regime'].update({'value': regime, 'display_value': regime or '—', 'unit': 'state', 'status': 'available' if regime else 'unavailable'})
    shaped['quality'].setdefault('extensions', {})['event_badge_contract_v1'] = deepcopy(built['quality']['extensions']['event_badge_contract_v1'])
    return shaped
