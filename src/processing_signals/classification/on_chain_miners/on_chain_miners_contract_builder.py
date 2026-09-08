from __future__ import annotations


import copy
import json
import math
from collections.abc import Mapping, Sequence
from typing          import Any, Callable


ON_CHAIN_MINERS_SCREEN_ID        = "on_chain_miners"
ON_CHAIN_MINERS_ROUTE            = "/on-chain-miners"
ON_CHAIN_MINERS_TITLE            = "ON-CHAIN & MINERS METRICS"
ON_CHAIN_MINERS_CONTRACT_SCHEMA  = "trad_elatin.on_chain_miners.screen.v1"
ON_CHAIN_MINERS_CONTRACT_VERSION = "1.0.0"

RANGE_OPTIONS = ("7D", "30D")
DEFAULT_RANGE = "30D"
RANGE_DAYS    = {"7D": 7, "30D": 30}
SECONDS_PER_DAY = 86_400

VALID_MODES            = {"bootstrap", "incremental", "recovery"}
VALID_STATUSES         = {"available", "partial", "unavailable", "invalid"}
VALID_QUALITY_STATUSES = {"ok", "partial", "invalid"}
VALID_COLOR_TOKENS     = {"positive", "negative", "warning", "neutral", "unavailable", "invalid"}
STATUS_PRIORITY        = {"available": 0, "partial": 1, "unavailable": 2, "invalid": 3}
CHART_IDS              = ("miner_reserve", "sopr_7d", "hashrate", "difficulty", "miner_net_position_change")
WIDGET_IDS             = ("miner_pressure", "reserve_trend", "net_position", "sopr_regime")
DRILLDOWN_IDS: tuple[str, ...] = ()
OPTIONAL_UNAVAILABLE: tuple[str, ...] = ()

SERIES_CONFIG = {
    "miner_reserve":             ("miner_reserve_btc", "Miner Reserve (BTC)", "Total miner-held BTC", "area", "BTC", "Glassnode"),
    "sopr_7d":                   ("sopr_7d", "SOPR (7D)", "Spent Output Profit Ratio", "line", "ratio", "CryptoQuant"),
    "hashrate":                  ("hashrate_eh_s", "Hashrate (EH/s)", "Network hash rate", "area", "EH/s", "Glassnode"),
    "difficulty":                ("difficulty_t", "Difficulty (T)", "Network difficulty", "line", "T", "CryptoQuant"),
    "miner_net_position_change": ("miner_net_position_change", "Miner Net Position Change (BTC)", "Daily miner reserve delta", "bar", "BTC/day", "Derived"),
}
WIDGET_TITLES = {"miner_pressure": "MINER PRESSURE", "reserve_trend": "RESERVE TREND", "net_position": "NET POSITION", "sopr_regime": "SOPR REGIME"}


def _stable_unique(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _finite(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _timestamp(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def copy_json_safe_value(value: Any, *, path: str) -> tuple[Any, list[str]]:
    if value is None or isinstance(value, (str, bool, int)):
        return value, []
    if isinstance(value, float):
        if not math.isfinite(value):
            return None, [f"non_finite_contract_value:{path}"]
        return (0.0 if value == 0.0 else value), []
    if isinstance(value, Mapping):
        copied: dict[str, Any] = {}
        errors: list[str] = []
        for key, child in value.items():
            if not isinstance(key, str):
                errors.append(f"non_string_contract_key:{path}")
                continue
            copied_child, child_errors = copy_json_safe_value(child, path=f"{path}.{key}")
            copied[key] = copied_child
            errors.extend(child_errors)
        return copied, errors
    if isinstance(value, (list, tuple)):
        copied_list: list[Any] = []
        errors: list[str] = []
        for index, child in enumerate(value):
            copied_child, child_errors = copy_json_safe_value(child, path=f"{path}[{index}]")
            copied_list.append(copied_child)
            errors.extend(child_errors)
        return copied_list, errors
    return None, [f"non_json_contract_value:{path}:{type(value).__name__}"]


def _messages(value: Any, path: str) -> tuple[list[str], list[str]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return [], [f"invalid_upstream_messages:{path}"]
    messages: list[str] = []
    errors: list[str] = []
    for index, message in enumerate(value):
        if not isinstance(message, str):
            errors.append(f"invalid_upstream_message:{path}[{index}]")
        elif message not in messages:
            messages.append(message)
    return messages, errors


def _trim_decimal(value: float, decimals: int) -> str:
    text = f"{value:,.{decimals}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def format_miner_reserve(value: Any) -> str:
    if not _finite(value):
        return "--"
    return f"{value / 1_000_000:.2f}M" if abs(value) >= 1_000_000 else _trim_decimal(value, 2)


def format_sopr(value: Any) -> str:
    return f"{value:.3f}" if _finite(value) else "--"


def format_one_decimal(value: Any) -> str:
    return f"{value:.1f}" if _finite(value) else "--"


def format_net_position(value: Any) -> str:
    if not _finite(value):
        return "--"
    if value == 0:
        return "0"
    formatted = _trim_decimal(abs(value), 2)
    return f"+{formatted}" if value > 0 else f"-{formatted}"


def format_currency(value: Any) -> str:
    if not _finite(value):
        return "--"
    if abs(value) >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if abs(value) >= 1_000:
        return f"${value:,.0f}"
    return f"${value:,.2f}"


def format_percent(value: Any) -> str:
    return f"{value * 100:.2f}%" if _finite(value) else "--"


def format_price(value: Any) -> str:
    return f"${value:,.2f}" if _finite(value) else "--"


DISPLAY_FORMATTERS: dict[str, Callable[[Any], str]] = {
    "miner_reserve": format_miner_reserve, "sopr_7d": format_sopr, "hashrate": format_one_decimal,
    "difficulty": format_one_decimal, "miner_net_position_change": format_net_position,
}


def _validate_upstreams(processing: Any, classification: Any) -> list[str]:
    errors: list[str] = []
    for name, contract, stage in (("processing", processing, "processing"), ("classification", classification, "classification")):
        if not isinstance(contract, Mapping):
            errors.append(f"{name}_contract_must_be_mapping")
            continue
        if contract.get("family") != "on_chain_miners":
            errors.append(f"{name}_family_must_be_on_chain_miners")
        if contract.get("stage") != stage:
            errors.append(f"{name}_stage_must_be_{stage}")
        if contract.get("mode") not in VALID_MODES:
            errors.append(f"{name}_mode_invalid")
        for field in (("context", "series", "features", "quality") if name == "processing" else ("context", "classifications", "quality")):
            if not isinstance(contract.get(field), Mapping):
                errors.append(f"{name}_{field}_must_be_mapping")
    if errors:
        return errors
    for chart_id, (series_id, _, _, _, expected_unit, _) in SERIES_CONFIG.items():
        payload = processing["series"].get(series_id)
        if not isinstance(payload, Mapping):
            errors.append(f"missing_processing_series:{series_id}")
            continue
        for field in ("status", "unit", "records", "current", "warnings", "errors", "metadata"):
            if field not in payload:
                errors.append(f"missing_processing_series_field:{series_id}:{field}")
        if payload.get("unit") != expected_unit:
            errors.append(f"incompatible_processing_unit:{series_id}")
        if payload.get("status") not in VALID_STATUSES:
            errors.append(f"invalid_processing_series_status:{series_id}")
    for classification_id in WIDGET_IDS:
        payload = classification["classifications"].get(classification_id)
        if not isinstance(payload, Mapping):
            errors.append(f"missing_classification:{classification_id}")
            continue
        for field in ("classification_id", "status", "state", "signal", "display_label", "display_color_token", "source", "thresholds", "reason", "warnings", "errors"):
            if field not in payload:
                errors.append(f"missing_classification_field:{classification_id}:{field}")
        if payload.get("classification_id") != classification_id:
            errors.append(f"classification_id_mismatch:{classification_id}")
        if payload.get("status") not in VALID_STATUSES:
            errors.append(f"invalid_classification_status:{classification_id}")
        if payload.get("display_color_token") not in VALID_COLOR_TOKENS:
            errors.append(f"invalid_classification_color_token:{classification_id}")
    for name, contract in (("processing", processing), ("classification", classification)):
        quality = contract["quality"]
        if quality.get("status") not in VALID_QUALITY_STATUSES:
            errors.append(f"invalid_{name}_quality_status")
        for field in ("warnings", "errors"):
            _, message_errors = _messages(quality.get(field), f"{name}.quality.{field}")
            errors.extend(message_errors)
    p_context, c_context = processing["context"], classification["context"]
    for field in ("asset", "data_mode", "is_demo", "reference_timestamp", "execution_timestamp", "generated_at"):
        if p_context.get(field) != c_context.get(field):
            errors.append(f"upstream_context_mismatch:{field}")
    if processing.get("mode") != classification.get("mode"):
        errors.append("upstream_context_mismatch:mode")
    return _stable_unique(errors)


def _data_as_of(processing: Mapping[str, Any], classification: Mapping[str, Any]) -> int | None:
    if processing.get("quality", {}).get("status") == "invalid" or classification.get("quality", {}).get("status") == "invalid":
        return None
    processing_value     = processing.get("quality", {}).get("data_as_of")
    classification_value = classification.get("quality", {}).get("data_as_of")
    return min(processing_value, classification_value) if _timestamp(processing_value) and _timestamp(classification_value) else None


def build_range_selector() -> dict[str, Any]:
    return {"options": [{"id": range_id, "days": RANGE_DAYS[range_id]} for range_id in RANGE_OPTIONS], "default": DEFAULT_RANGE,
            "source_resolution": "1D", "intraday_available": False}


def _empty_range(range_id: str, status: str, reason: str) -> dict[str, Any]:
    return {"range_id": range_id, "days": RANGE_DAYS[range_id], "status": status, "from_timestamp": None, "to_timestamp": None,
            "expected_points": RANGE_DAYS[range_id], "actual_points": 0, "coverage_ratio": 0.0, "points": [],
            "reason": reason, "warnings": [], "errors": []}


def _point(record: Mapping[str, Any], *, bar: bool) -> dict[str, Any]:
    point = {"timestamp": record["timestamp"], "value": record["value"]}
    if bar:
        point["bar_token"] = "positive" if record["value"] > 0 else "negative" if record["value"] < 0 else "neutral"
        point["unit"] = "BTC/day"
    return point


def build_series_ranges(series: Mapping[str, Any], *, data_as_of: int | None, bar: bool = False) -> tuple[dict[str, Any], list[str]]:
    if data_as_of is None:
        return {range_id: _empty_range(range_id, "unavailable", "screen_data_as_of_unavailable") for range_id in RANGE_OPTIONS}, []
    if series.get("status") == "invalid":
        return {range_id: _empty_range(range_id, "invalid", "source_series_invalid") for range_id in RANGE_OPTIONS}, []
    records = series.get("records")
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes, bytearray)):
        return {range_id: _empty_range(range_id, "invalid", "source_records_invalid") for range_id in RANGE_OPTIONS}, ["source_records_invalid"]
    valid_records: list[Mapping[str, Any]] = []
    errors: list[str] = []
    for index, record in enumerate(records):
        if not isinstance(record, Mapping) or not _timestamp(record.get("timestamp")) or not _finite(record.get("value")):
            errors.append(f"invalid_chart_point:{index}")
        else:
            valid_records.append(record)
    if errors:
        return {range_id: _empty_range(range_id, "invalid", "source_points_invalid") for range_id in RANGE_OPTIONS}, errors
    output: dict[str, Any] = {}
    for range_id in RANGE_OPTIONS:
        days = RANGE_DAYS[range_id]
        start = data_as_of - (days - 1) * SECONDS_PER_DAY
        points = [_point(record, bar=bar) for record in valid_records if start <= record["timestamp"] <= data_as_of]
        actual = len(points)
        status = "available" if actual == days else "partial" if actual else "unavailable"
        output[range_id] = {"range_id": range_id, "days": days, "status": status, "from_timestamp": start, "to_timestamp": data_as_of,
                            "expected_points": days, "actual_points": actual, "coverage_ratio": min(actual / days, 1.0), "points": points,
                            "reason": None if status == "available" else "range_history_partial" if status == "partial" else "range_history_unavailable",
                            "warnings": [], "errors": []}
    return output, []


def _current(series: Mapping[str, Any], chart_id: str) -> tuple[dict[str, Any], list[str]]:
    current = series.get("current") if isinstance(series.get("current"), Mapping) else {}
    status  = str(current.get("status", series.get("status", "invalid")))
    if status not in VALID_STATUSES:
        status = "invalid"
    value = current.get("value")
    timestamp = current.get("timestamp")
    errors: list[str] = []
    if status in {"available", "partial"} and (not _finite(value) or not _timestamp(timestamp)):
        errors.append("current_value_or_timestamp_invalid")
        status, value, timestamp = "invalid", None, None
    elif status in {"unavailable", "invalid"}:
        value, timestamp = None, None
    payload = {"status": status, "timestamp": timestamp, "value": 0.0 if isinstance(value, float) and value == 0 else value,
               "unit": series.get("unit"), "display_value": DISPLAY_FORMATTERS[chart_id](value)}
    return payload, errors


def build_chart(chart_id: str, processing: Mapping[str, Any], *, data_as_of: int | None) -> tuple[dict[str, Any], list[str]]:
    series_id, title, subtitle, chart_type, unit, provider = SERIES_CONFIG[chart_id]
    series = processing["series"][series_id]
    ranges, range_errors = build_series_ranges(series, data_as_of=data_as_of, bar=chart_type == "bar")
    current, current_errors = _current(series, chart_id)
    warnings, warning_errors = _messages(series.get("warnings"), f"processing.series.{series_id}.warnings")
    errors, error_errors = _messages(series.get("errors"), f"processing.series.{series_id}.errors")
    if range_errors or current_errors or warning_errors or error_errors or series.get("status") == "invalid" or current["status"] == "invalid":
        status = "invalid"
    elif series.get("status") == "unavailable" or current["status"] == "unavailable":
        status = "unavailable"
    elif series.get("status") == "partial" or current["status"] == "partial":
        status = "partial"
    else:
        status = "available"
    chart = {"chart_id": chart_id, "title": title, "subtitle": subtitle, "chart_type": chart_type, "unit": unit, "provider": provider,
             "status": status, "current": current, "series_by_range": ranges, "warnings": warnings, "errors": errors}
    available_records = len(series.get("records", [])) if isinstance(series.get("records"), Sequence) else 0
    chart["calculation_history"] = {"records_available": available_records,
        "first_timestamp": series.get("records", [{}])[0].get("timestamp") if available_records else None,
        "last_timestamp": series.get("records", [{}])[-1].get("timestamp") if available_records else None,
        "source_resolution": "1d", "fabricated_records": 0}
    chart["history_contract"] = {"requested_days": 30, "available_days": available_records,
        "status": "available" if available_records >= 30 else "blocked_upstream",
        "reason": None if available_records >= 30 else "provider_history_beyond_available_daily_records_unproven"}
    if chart_id != "miner_net_position_change":
        chart["preferred_representation"] = chart_type
        chart["ohlc_contract"] = {"status": "blocked_upstream", "reason": "daily_scalar_source_has_no_intra_bucket_observations",
            "source_resolution": "1d_scalar", "required_capability": "multiple_temporal_observations_per_daily_bucket",
            "fabricated_ohlc": False}
    if chart_id == "sopr_7d":
        chart["reference_lines"] = [{"value": 1.0, "label": "Breakeven", "token": "neutral"}]
    if chart_id == "miner_net_position_change":
        chart.update({"source_provider": "Glassnode", "calculation_source": "miner_reserve_btc"})
    return chart, [*range_errors, *current_errors, *warning_errors, *error_errors]


def build_widget(widget_id: str, classification: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    item = classification["classifications"][widget_id]
    source, source_errors = copy_json_safe_value(item.get("source"), path=f"widgets.{widget_id}.source")
    thresholds, threshold_errors = copy_json_safe_value(item.get("thresholds"), path=f"widgets.{widget_id}.thresholds")
    warnings, warning_errors = _messages(item.get("warnings"), f"classification.{widget_id}.warnings")
    errors, error_errors = _messages(item.get("errors"), f"classification.{widget_id}.errors")
    copy_errors = [*source_errors, *threshold_errors, *warning_errors, *error_errors]
    status = "invalid" if copy_errors else str(item.get("status"))
    valid  = status in {"available", "partial"} and item.get("state") is not None
    label  = item.get("display_label")
    raw_value = source.get("value") if isinstance(source, Mapping) else None
    display = format_net_position(raw_value) if widget_id == "net_position" and valid else str(label) if valid else "--"
    widget = {"widget_id": widget_id, "title": WIDGET_TITLES[widget_id], "status": status, "state": item.get("state") if valid else None,
              "signal": item.get("signal") if valid else None, "classification_label": label, "display_value": display,
              "display_color_token": item.get("display_color_token") if valid else "invalid" if status == "invalid" else "unavailable",
              "source": source, "thresholds": thresholds, "reason": item.get("reason"), "warnings": warnings, "errors": errors}
    if widget_id == "net_position":
        widget.update({"raw_value": raw_value if valid else None, "unit": source.get("unit") if isinstance(source, Mapping) else "BTC/day"})
    return widget, copy_errors


def _record_ranges(records: Any, *, data_as_of: int | None, item_key: str, point_builder: Callable[[Mapping[str, Any]], dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    if data_as_of is None:
        ranges = {range_id: {**_empty_range(range_id, "unavailable", "screen_data_as_of_unavailable"), item_key: []} for range_id in RANGE_OPTIONS}
        for payload in ranges.values():
            payload.pop("points", None)
        return ranges, []
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes, bytearray)):
        return {}, ["source_records_invalid"]
    valid: list[Mapping[str, Any]] = []
    for index, record in enumerate(records):
        if not isinstance(record, Mapping) or not _timestamp(record.get("timestamp")):
            return {}, [f"source_record_invalid:{index}"]
        valid.append(record)
    output: dict[str, Any] = {}
    for range_id in RANGE_OPTIONS:
        days = RANGE_DAYS[range_id]
        start = data_as_of - (days - 1) * SECONDS_PER_DAY
        points = [point_builder(record) for record in valid if start <= record["timestamp"] <= data_as_of]
        actual = len(points)
        status = "available" if actual == days else "partial" if actual else "unavailable"
        output[range_id] = {"range_id": range_id, "days": days, "status": status, "from_timestamp": start, "to_timestamp": data_as_of,
                            "expected_points": days, "actual_points": actual, "coverage_ratio": min(actual / days, 1.0), item_key: points,
                            "reason": None if status == "available" else "range_history_partial" if status == "partial" else "range_history_unavailable",
                            "warnings": [], "errors": []}
    return output, []


def build_drilldowns(
    processing: Mapping[str, Any], classification: Mapping[str, Any], *, data_as_of: int | None
) -> tuple[dict[str, Any], list[str]]:
    """VR1 has no On-Chain drilldown surface."""
    del processing, classification, data_as_of
    return {}, []

def _fallback(mode: Any, errors: Sequence[str]) -> dict[str, Any]:
    ranges = {range_id: _empty_range(range_id, "invalid", "invalid_upstream_contract") for range_id in RANGE_OPTIONS}
    charts = {}
    for chart_id, (_, title, subtitle, chart_type, unit, provider) in SERIES_CONFIG.items():
        charts[chart_id] = {"chart_id": chart_id, "title": title, "subtitle": subtitle, "chart_type": chart_type, "unit": unit, "provider": provider,
                            "status": "invalid", "current": {"status": "unavailable", "timestamp": None, "value": None, "unit": unit, "display_value": "--"},
                            "series_by_range": copy.deepcopy(ranges), "warnings": [], "errors": []}
    widgets = {widget_id: {"widget_id": widget_id, "title": WIDGET_TITLES[widget_id], "status": "invalid", "state": None, "signal": None,
                           "classification_label": "INVALID", "display_value": "--", "display_color_token": "invalid", "source": {}, "thresholds": {},
                           "reason": "invalid_upstream_contract", "warnings": [], "errors": []} for widget_id in WIDGET_IDS}
    drilldowns: dict[str, Any] = {}
    context = {"asset": None, "data_mode": None, "is_demo": None, "reference_timestamp": None, "execution_timestamp": None, "generated_at": None,
               "processing_data_as_of": None, "classification_data_as_of": None, "data_as_of": None,
               "calculation_history": "full_available_history", "presentation_default_range": DEFAULT_RANGE}
    quality = {"status": "invalid", "availability": {"charts": {chart_id: "invalid" for chart_id in CHART_IDS},
                                                       "widgets": {widget_id: "invalid" for widget_id in WIDGET_IDS},
                                                       }, "data_as_of": None,
               "processing_status": "invalid", "classification_status": "invalid", "missing_fields": [*CHART_IDS, *WIDGET_IDS],
               "warnings": [], "errors": _stable_unique(list(errors)), "optional_unavailable": []}
    return {"schema": {"id": ON_CHAIN_MINERS_CONTRACT_SCHEMA, "version": ON_CHAIN_MINERS_CONTRACT_VERSION},
            "screen": {"id": ON_CHAIN_MINERS_SCREEN_ID, "route": ON_CHAIN_MINERS_ROUTE, "title": ON_CHAIN_MINERS_TITLE, "family": "on_chain_miners"},
            "stage": "screen_contract", "mode": mode if mode in VALID_MODES else None, "context": context, "range_selector": build_range_selector(),
            "operational_status": {"data_mode": None, "is_demo": None, "quality_status": "invalid", "connection_status": "not_reported",
                                   "cache_status": "not_reported", "generated_at": None, "data_as_of": None},
            "charts": charts, "widgets": widgets, "quality": quality}


def evaluate_screen_quality(*, processing: Mapping[str, Any], classification: Mapping[str, Any], charts: Mapping[str, Any],
                            widgets: Mapping[str, Any], drilldowns: Mapping[str, Any], data_as_of: int | None, build_errors: Sequence[str]) -> dict[str, Any]:
    chart_availability  = {chart_id: str(charts[chart_id]["status"]) for chart_id in CHART_IDS}
    widget_availability = {widget_id: str(widgets[widget_id]["status"]) for widget_id in WIDGET_IDS}
    drilldown_availability = {drilldown_id: str(drilldowns[drilldown_id]["status"]) for drilldown_id in DRILLDOWN_IDS}
    p_quality, c_quality = processing["quality"], classification["quality"]
    p_warnings, p_warning_errors = _messages(p_quality.get("warnings"), "processing.quality.warnings")
    p_errors, p_error_errors     = _messages(p_quality.get("errors"), "processing.quality.errors")
    c_warnings, c_warning_errors = _messages(c_quality.get("warnings"), "classification.quality.warnings")
    c_errors, c_error_errors     = _messages(c_quality.get("errors"), "classification.quality.errors")
    warnings = [*(f"processing_warning:{message}" for message in p_warnings), *(f"classification_warning:{message}" for message in c_warnings)]
    errors = [*(f"processing_error:{message}" for message in p_errors), *(f"classification_error:{message}" for message in c_errors),
              *build_errors, *p_warning_errors, *p_error_errors, *c_warning_errors, *c_error_errors]
    for chart_id, chart in charts.items():
        warnings.extend(f"chart_warning:{chart_id}:{message}" for message in chart["warnings"])
        errors.extend(f"chart_error:{chart_id}:{message}" for message in chart["errors"])
    for widget_id, widget in widgets.items():
        warnings.extend(f"widget_warning:{widget_id}:{message}" for message in widget["warnings"])
        errors.extend(f"widget_error:{widget_id}:{message}" for message in widget["errors"])
    for drilldown_id, drilldown in drilldowns.items():
        warnings.extend(f"drilldown_warning:{drilldown_id}:{message}" for message in drilldown["warnings"])
        errors.extend(f"drilldown_error:{drilldown_id}:{message}" for message in drilldown["errors"])
    all_availability = (*chart_availability.values(), *widget_availability.values(), *drilldown_availability.values())
    missing = [name for name, status in {**chart_availability, **widget_availability}.items() if status in {"unavailable", "invalid"}]
    optional_unavailable = [name for name, status in drilldown_availability.items() if status == "unavailable"]
    semantic = any(status in {"available", "partial"} for status in all_availability)
    if p_quality.get("status") == "invalid" or c_quality.get("status") == "invalid" or errors or "invalid" in all_availability:
        status = "invalid"
    elif p_quality.get("status") == "ok" and c_quality.get("status") == "ok" and all(value == "available" for value in all_availability) and data_as_of is not None:
        status = "ok"
    else:
        status = "partial" if semantic else "invalid"
    if status == "partial" and not warnings and not errors and not missing:
        warnings.append("screen_quality_partial")
    return {"status": status, "availability": {"charts": chart_availability, "widgets": widget_availability},
            "data_as_of": data_as_of if status != "invalid" else None, "processing_status": p_quality.get("status"),
            "classification_status": c_quality.get("status"), "missing_fields": _stable_unique(missing), "warnings": _stable_unique(warnings),
            "errors": _stable_unique(errors), "optional_unavailable": _stable_unique(optional_unavailable)}


class OnChainMinersContractBuilder:
    def __init__(self, processing_contract: Mapping[str, Any], classification_contract: Mapping[str, Any]) -> None:
        self.processing_contract     = processing_contract
        self.classification_contract = classification_contract

    def build(self) -> dict[str, Any]:
        errors = _validate_upstreams(self.processing_contract, self.classification_contract)
        mode   = self.processing_contract.get("mode") if isinstance(self.processing_contract, Mapping) else None
        if errors:
            return _fallback(mode, errors)
        processing, classification = self.processing_contract, self.classification_contract
        context = processing["context"]
        data_as_of = _data_as_of(processing, classification)
        output_context = {"asset": context.get("asset"), "data_mode": context.get("data_mode"), "is_demo": context.get("is_demo"),
                          "reference_timestamp": context.get("reference_timestamp"), "execution_timestamp": context.get("execution_timestamp"),
                          "generated_at": context.get("generated_at"), "processing_data_as_of": processing["quality"].get("data_as_of"),
                          "classification_data_as_of": classification["quality"].get("data_as_of"), "data_as_of": data_as_of,
                          "calculation_history": "full_available_history", "presentation_default_range": DEFAULT_RANGE}
        charts: dict[str, Any] = {}
        widgets: dict[str, Any] = {}
        build_errors: list[str] = []
        for chart_id in CHART_IDS:
            charts[chart_id], chart_errors = build_chart(chart_id, processing, data_as_of=data_as_of)
            build_errors.extend(f"chart_build_error:{chart_id}:{error}" for error in chart_errors)
        for widget_id in WIDGET_IDS:
            widgets[widget_id], widget_errors = build_widget(widget_id, classification)
            build_errors.extend(f"widget_build_error:{widget_id}:{error}" for error in widget_errors)
        drilldowns, drilldown_errors = build_drilldowns(processing, classification, data_as_of=data_as_of)
        build_errors.extend(drilldown_errors)
        quality = evaluate_screen_quality(processing=processing, classification=classification, charts=charts, widgets=widgets, drilldowns=drilldowns,
                                          data_as_of=data_as_of, build_errors=build_errors)
        output_context["data_as_of"] = quality["data_as_of"]
        output = {"schema": {"id": ON_CHAIN_MINERS_CONTRACT_SCHEMA, "version": ON_CHAIN_MINERS_CONTRACT_VERSION},
                  "screen": {"id": ON_CHAIN_MINERS_SCREEN_ID, "route": ON_CHAIN_MINERS_ROUTE, "title": ON_CHAIN_MINERS_TITLE, "family": "on_chain_miners"},
                  "stage": "screen_contract", "mode": mode, "context": output_context, "range_selector": build_range_selector(),
                  "operational_status": {"data_mode": context.get("data_mode"), "is_demo": context.get("is_demo"), "quality_status": quality["status"],
                                         "connection_status": "not_reported", "cache_status": "not_reported", "generated_at": context.get("generated_at"),
                                         "data_as_of": quality["data_as_of"]},
                  "charts": charts, "widgets": widgets,
                  "technical_analysis": {"status": "blocked_upstream", "recalculate_in_hmi": False,
                      "targets": {chart_id: {"status": "blocked_upstream", "reason": "daily_scalar_source_has_no_legitimate_ohlc",
                          "required_capability": "multiple_temporal_observations_per_daily_bucket", "indicator_ids": [],
                          "technical_event_ids": []} for chart_id in ("miner_reserve", "sopr_7d", "hashrate", "difficulty")},
                      "event_indexes": {"by_id": {}, "technical_event_ids": []}},
                  "history_contract": {"requested_days": 30, "available_days": min(
                      len(processing["series"][series_id].get("records", [])) for series_id, *_ in SERIES_CONFIG.values()),
                      "status": "blocked_upstream", "fabricated_records": 0},
                  "quality": quality}
        output = align_on_chain_miners_to_sp_v2_0(output, processing, classification)
        copied, copy_errors = copy_json_safe_value(output, path="screen_contract")
        if copy_errors:
            output = _fallback(mode, copy_errors)
        else:
            output = copied
        try:
            json.dumps(output, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            output = _fallback(mode, [f"screen_contract_serialization_failed:{type(exc).__name__}"])
            json.dumps(output, ensure_ascii=False, allow_nan=False)
        return output


def build_on_chain_miners_screen_contract(processing_contract: Mapping[str, Any], classification_contract: Mapping[str, Any]) -> dict[str, Any]:
    processing_before, _ = copy_json_safe_value(processing_contract, path="processing")
    classification_before, _ = copy_json_safe_value(classification_contract, path="classification")
    output = OnChainMinersContractBuilder(processing_contract, classification_contract).build()
    processing_after, _ = copy_json_safe_value(processing_contract, path="processing")
    classification_after, _ = copy_json_safe_value(classification_contract, path="classification")
    if processing_before != processing_after or classification_before != classification_after:
        raise RuntimeError("Contract Builder mutated an upstream contract")
    return output

# --- Canonical Screen contract shaping ---
from copy import deepcopy

from datetime import datetime, timezone

import json

import math

from pathlib import Path

from typing import Any, Mapping

_screen_TEMPLATE_PATH = Path(__file__).with_name('screen_template.json')

_screen_VERSION = '2.0.0'

_screen_RANGES = {'7D': 7, '30D': 30}

_screen_PRIMARY = {'miner_reserve': 'miner_reserve_btc', 'sopr_7d': 'sopr_7d', 'hashrate': 'hashrate_eh_s', 'difficulty': 'difficulty_t'}

_screen_MISSING = object()

_screen_PROVIDERS = {'miner_reserve': 'glassnode', 'sopr_7d': 'glassnode', 'hashrate': 'glassnode', 'difficulty': 'glassnode'}

def _screen_template() -> dict[str, Any]:
    return json.loads(_screen_TEMPLATE_PATH.read_text(encoding='utf-8'))

def _screen_finite(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (not math.isfinite(value)):
        return None
    return 0.0 if value == 0 else value

def _screen_iso(timestamp: Any) -> str | None:
    if type(timestamp) is not int or timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace('+00:00', 'Z')

def _screen_display(value: Any, chart_id: str) -> str:
    value = _screen_finite(value)
    if value is None:
        return '—'
    if chart_id == 'miner_reserve':
        return f'{value / 1000000:.3f}M'
    if chart_id == 'sopr_7d':
        return f'{value:.3f}'
    if chart_id == 'hashrate':
        return f'{value:.1f} EH/s'
    if chart_id == 'difficulty':
        return f'{value:.2f} T'
    if chart_id == 'miner_net_position_change':
        return f'{value:+,.0f}'
    return str(value)

def _screen_context(reference: Mapping[str, Any], processing: Mapping[str, Any], classification: Mapping[str, Any]) -> dict[str, Any]:
    pctx = processing.get('context', {})
    out = deepcopy(dict(reference))
    data_as_of = min((x for x in (processing.get('quality', {}).get('data_as_of'), classification.get('quality', {}).get('data_as_of')) if type(x) is int)) if any((type(x) is int for x in (processing.get('quality', {}).get('data_as_of'), classification.get('quality', {}).get('data_as_of')))) else None
    calc_days = min((len(processing.get('series', {}).get(series_id, {}).get('daily_candles', [])) for series_id in _screen_PRIMARY.values()))
    is_demo = bool(pctx.get('is_demo'))
    out.update({'asset': pctx.get('asset', 'BTC'), 'data_mode': pctx.get('data_mode'), 'is_demo': is_demo, 'reference_timestamp': pctx.get('reference_timestamp'), 'execution_timestamp': pctx.get('execution_timestamp'), 'generated_at': pctx.get('generated_at'), 'processing_data_as_of': processing.get('quality', {}).get('data_as_of'), 'classification_data_as_of': classification.get('quality', {}).get('data_as_of'), 'data_as_of': data_as_of, 'calculation_history': 'full_available_history', 'presentation_default_range': '30D', 'calculation_history_days': calc_days, 'fixture_seed': None, 'fixture_as_of_timestamp': None, 'fixture_as_of_iso': None, 'synthetic_fixture': False, 'realism_refactor_version': 'runtime_emulator_v1' if is_demo else 'runtime_provider_v1', 'realism_note': 'Glassnode-primary miner/on-chain runtime acquired at 24h resolution; Processing publishes daily presentation series.'})
    return out

def _screen_range_selector(reference: Mapping[str, Any]) -> dict[str, Any]:
    out = deepcopy(dict(reference))
    out['options'] = [{'id': key, 'label': key, 'days': days} for key, days in _screen_RANGES.items()]
    out.update({'default': '30D'})
    out.pop('source_resolution', None)
    out.pop('provider_intraday_resolution_used_for_ohlc', None)
    out.pop('intraday_available', None)
    return out

def _screen_candle(row: Mapping[str, Any]) -> dict[str, Any]:
    return {'timestamp': row.get('timestamp'), 'open': _screen_finite(row.get('open')), 'high': _screen_finite(row.get('high')), 'low': _screen_finite(row.get('low')), 'close': _screen_finite(row.get('close')), 'is_closed': bool(row.get('is_closed', True))}

def _screen_primary_range(
    candles: list[dict[str, Any]], *, range_id: str, unit: str, representation: str
) -> dict[str, Any]:
    """Project Processing daily OHLC history to the HMI presentation points contract.

    Processing may retain daily candles for calculations, but Screen A is a scalar
    time-series view.  The HMI must receive ``points`` directly and never derive
    presentation values from ``calculation_history.candles``.
    """
    days = _screen_RANGES[range_id]
    rows = candles[-days:]
    points = [
        {"timestamp": row.get("timestamp"), "value": row.get("close"), "unit": unit}
        for row in rows
        if _screen_finite(row.get("close")) is not None
    ]
    actual = len(points)
    status = 'available' if actual == days else 'partial' if actual else 'unavailable'
    return {
        'range_id': range_id,
        'days': days,
        'status': status,
        'from_timestamp': points[0]['timestamp'] if points else None,
        'to_timestamp': points[-1]['timestamp'] if points else None,
        'expected_points': days,
        'actual_points': actual,
        'coverage_ratio': min(actual / days, 1.0),
        'reason': None if status == 'available' else 'range_history_partial' if actual else 'range_history_unavailable',
        'warnings': [],
        'errors': [],
        'representation': representation,
        'points': points,
        'point_count': actual,
    }

def _screen_primary_chart(ref: Mapping[str, Any], chart_id: str, processing: Mapping[str, Any], is_demo: bool) -> dict[str, Any]:
    series = processing['series'][_screen_PRIMARY[chart_id]]
    candles = [_screen_candle(row) for row in series.get('daily_candles', []) if isinstance(row, Mapping)]
    last = candles[-1] if candles else None
    out = deepcopy(dict(ref))
    out['provider'] = _screen_PROVIDERS[chart_id]
    out['status'] = series.get('status')
    out['current'] = {'status': series.get('status'), 'timestamp': last.get('timestamp') if last else None, 'value': last.get('close') if last else None, 'unit': series.get('unit'), 'display_value': _screen_display(last.get('close') if last else None, chart_id)}
    representation = str(ref.get('preferred_representation') or ref.get('chart_type') or 'line')
    out['series_by_range'] = {
        rid: _screen_primary_range(
            candles, range_id=rid, unit=str(series.get('unit') or ''), representation=representation
        )
        for rid in _screen_RANGES
    }
    out['warnings'] = deepcopy(series.get('warnings', []))
    out['errors'] = deepcopy(series.get('errors', []))
    out['preferred_representation'] = representation
    out['reason'] = None if candles else 'provider_history_unavailable'
    ohlc = deepcopy(ref.get('ohlc_contract', {}))
    ohlc.update({'construction_stage': 'processing', 'native_provider_ohlc': False, 'hmi_must_reconstruct_ohlc': False, 'line_fallback_allowed': False, 'volume_allowed': False})
    ohlc['construction_rule'] = {'open': 'first_real_value_in_bucket', 'high': 'max_real_value_in_bucket', 'low': 'min_real_value_in_bucket', 'close': 'last_real_value_in_bucket'}
    out['ohlc_contract'] = ohlc
    out['history_contract'] = {'history_days': len(candles), 'history_points': len(candles), 'from_timestamp': candles[0]['timestamp'] if candles else None, 'to_timestamp': candles[-1]['timestamp'] if candles else None, 'calculation_resolution': '1D', 'provider_source_resolution': '24h', 'daily_ohlc_construction': 'processing_from_provider_daily_samples', 'minimum_warmup_required_days': 200, 'sma_200_fully_formed_in_all_visible_ranges': len(candles) >= 559, 'synthetic_fixture': False}
    out['calculation_history'] = {'resolution': '1d', 'point_count': len(candles), 'candles': deepcopy(candles)}
    return out

def _screen_point_range(records: list[dict[str, Any]], range_id: str) -> dict[str, Any]:
    days = _screen_RANGES[range_id]
    rows = records[-days:]
    actual = len(rows)
    status = 'available' if actual == days else 'partial' if actual else 'unavailable'
    return {'range_id': range_id, 'days': days, 'status': status, 'from_timestamp': rows[0]['timestamp'] if rows else None, 'to_timestamp': rows[-1]['timestamp'] if rows else None, 'expected_points': days, 'actual_points': actual, 'coverage_ratio': min(actual / days, 1.0), 'points': deepcopy(rows), 'reason': None if status == 'available' else 'range_history_partial' if actual else 'range_history_unavailable', 'warnings': [], 'errors': []}

def _screen_net_position_chart(ref: Mapping[str, Any], processing: Mapping[str, Any]) -> dict[str, Any]:
    series = processing['series']['miner_net_position_change']
    records = [{'timestamp': row.get('timestamp'), 'value': _screen_finite(row.get('value')), 'unit': 'BTC/day'} for row in series.get('records', []) if isinstance(row, Mapping) and _screen_finite(row.get('value')) is not None]
    last = records[-1] if records else None
    out = deepcopy(dict(ref))
    out.update({'provider': 'glassnode', 'source_provider': 'glassnode', 'calculation_source': 'processing.series.miner_net_position_change.records', 'status': series.get('status'), 'reason': None if records else 'provider_history_unavailable'})
    out['subtitle'] = 'Direct Glassnode Miner Net Position Change'
    out['current'] = {'status': series.get('status'), 'timestamp': last.get('timestamp') if last else None, 'value': last.get('value') if last else None, 'unit': 'BTC/day', 'display_value': _screen_display(last.get('value') if last else None, 'miner_net_position_change')}
    out['series_by_range'] = {rid: _screen_point_range(records, rid) for rid in _screen_RANGES}
    out['warnings'] = deepcopy(series.get('warnings', []))
    out['errors'] = deepcopy(series.get('errors', []))
    out['calculation_history'] = {'resolution': '1d', 'point_count': len(records), 'points': deepcopy(records)}
    return out

def _screen_charts(reference: Mapping[str, Any], processing: Mapping[str, Any]) -> dict[str, Any]:
    is_demo = bool(processing.get('context', {}).get('is_demo'))
    out = {cid: _screen_primary_chart(reference[cid], cid, processing, is_demo) for cid in _screen_PRIMARY}
    out['miner_net_position_change'] = _screen_net_position_chart(reference['miner_net_position_change'], processing)
    return out

def _screen_widgets(reference: Mapping[str, Any], candidate: Mapping[str, Any], classification: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for wid, ref in reference.items():
        item = deepcopy(candidate.get(wid, {})) if isinstance(candidate.get(wid), Mapping) else {}
        if wid == 'net_position' and isinstance(item.get('source'), Mapping):
            item['source']['feature_id'] = 'miner_net_position_change'
        out[wid] = _screen_shape(ref, item)
    return out

def _screen_slice(values: Any, count: int) -> list[Any]:
    return deepcopy(values[-count:]) if isinstance(values, list) else []

def _screen_quality(reference: Mapping[str, Any], processing: Mapping[str, Any], classification: Mapping[str, Any], charts: Mapping[str, Any], drilldowns: Mapping[str, Any]) -> dict[str, Any]:
    out = deepcopy(dict(reference))
    pq = processing.get('quality', {})
    cq = classification.get('quality', {})
    statuses = [pq.get('status'), cq.get('status'), *[x.get('status') for x in charts.values() if isinstance(x, Mapping)]]
    status = 'invalid' if 'invalid' in statuses else 'partial' if 'partial' in statuses else 'unavailable' if all((x == 'unavailable' for x in statuses if x)) else 'available'
    warnings = deepcopy(pq.get('warnings', []))
    if pq.get('data_as_of') is None:
        warnings.append('processing_data_as_of_unavailable')
    if cq.get('data_as_of') is None:
        warnings.append('classification_data_as_of_unavailable')
    out.update({'status': status, 'data_as_of': pq.get('data_as_of'), 'processing_status': pq.get('status'), 'classification_status': cq.get('status'), 'missing_fields': deepcopy(pq.get('missing_fields', [])), 'warnings': list(dict.fromkeys(warnings)), 'errors': deepcopy(pq.get('errors', []))})
    ext = deepcopy(out.get('extensions', {}))
    for key in list(ext):
        low = key.lower()
        if 'net_position_derived' in low:
            ext[key] = False
    return out | {'extensions': ext}

def _screen_history(reference: Mapping[str, Any], processing: Mapping[str, Any]) -> dict[str, Any]:
    out = deepcopy(dict(reference))
    counts = [len(processing.get('series', {}).get(series_id, {}).get('daily_candles', [])) for series_id in _screen_PRIMARY.values()]
    calc = min(counts) if counts else 0
    demo = bool(processing.get('context', {}).get('is_demo'))
    out.update({'calculation_records': calc, 'minimum_warmup_records': 200, 'maximum_standard_indicator_period': 200, 'all_visible_moving_averages_warm': calc >= 559, 'technical_indicators_precomputed': True, 'hmi_recalculation': False, 'synthetic_fixture': False, 'fixture_seed': None, 'resolution': '1d', 'note': f'{calc} daily calculation records available across all primary on-chain metrics.'})
    return out

def _screen_shape(reference: Any, candidate: Any=_screen_MISSING) -> Any:
    """Project onto exact SP keys while preserving explicit runtime nulls."""
    if isinstance(reference, dict):
        source = candidate if isinstance(candidate, Mapping) else {}
        return {key: _screen_shape(value, source.get(key, _screen_MISSING)) for key, value in reference.items()}
    if isinstance(reference, list):
        if candidate is _screen_MISSING:
            return deepcopy(reference)
        if not isinstance(candidate, list):
            return deepcopy(candidate)
        if not reference:
            return deepcopy(candidate)
        if all((not isinstance(x, (dict, list)) for x in reference)):
            return deepcopy(candidate)
        identity_keys = ('metric_id', 'widget_id', 'chart_id', 'indicator_id', 'event_group', 'event_id', 'id', 'state', 'range_id')

        def ref_for(item: Any, index: int) -> Any:
            if isinstance(item, Mapping):
                if item.get('event_group') is not None:
                    has_gate = isinstance(item.get('calculation'), Mapping) and 'gate' in item.get('calculation', {})
                    for ref_item in reference:
                        if not isinstance(ref_item, Mapping) or ref_item.get('event_group') != item.get('event_group'):
                            continue
                        ref_gate = isinstance(ref_item.get('calculation'), Mapping) and 'gate' in ref_item.get('calculation', {})
                        if ref_gate == has_gate and bool(ref_item.get('indicator_id')) == bool(item.get('indicator_id')):
                            return ref_item
                for key in identity_keys:
                    value = item.get(key)
                    if value is None:
                        continue
                    for ref_item in reference:
                        if isinstance(ref_item, Mapping) and ref_item.get(key) == value:
                            return ref_item
            return reference[index] if index < len(reference) else reference[0]
        return [_screen_shape(ref_for(item, index), item) for index, item in enumerate(candidate)]
    return deepcopy(reference if candidate is _screen_MISSING else candidate)

def _screen_analysis_summary(indicator_id: str, package: Mapping[str, Any]) -> dict[str, Any]:
    series = package if isinstance(package, Mapping) else {}

    def last(name: str) -> float | None:
        values = series.get(name, [])
        if not isinstance(values, list):
            return None
        for value in reversed(values):
            value = _screen_finite(value)
            if value is not None:
                return float(value)
        return None
    if indicator_id == 'miner_reserve_change_zscore':
        value = last('reserve_change_zscore')
        signal = 'DISTRIBUTION' if value is not None and value <= -1.0 else 'ACCUMULATION' if value is not None and value >= 1.0 else 'NEUTRAL'
        display = '—' if value is None else f'{value:+.2f}σ'
        return {'label': 'MINER TREASURY', 'display_value': display, 'signal': signal, 'strength': 2 if value is not None and abs(value) >= 2 else 1}
    if indicator_id == 'miner_selling_pressure':
        value = last('selling_pressure_score')
        signal = 'HIGH PRESSURE' if value is not None and value >= 1.0 else 'LOW PRESSURE' if value is not None and value <= -1.0 else 'NORMAL'
        return {'label': 'SELLING PRESSURE', 'display_value': '—' if value is None else f'{value:+.2f}', 'signal': signal, 'strength': 2 if value is not None and abs(value) >= 2 else 1}
    if indicator_id == 'puell_revenue_stress':
        value = last('puell_multiple')
        signal = 'STRESSED' if value is not None and value < 0.7 else 'ELEVATED PROFITABILITY' if value is not None and value > 1.5 else 'NORMAL'
        return {'label': 'MINER ECONOMICS', 'display_value': '—' if value is None else f'{value:.2f}x', 'signal': signal, 'strength': 2 if value is not None and (value < 0.5 or value > 2.0) else 1}
    if indicator_id == 'hashrate_momentum_hash_ribbon':
        value = last('hash_momentum_pct')
        signal = 'HASH RECOVERY' if value is not None and value >= 0 else 'HASH CONTRACTION'
        return {'label': 'NETWORK HEALTH', 'display_value': '—' if value is None else f'{value:+.2f}%', 'signal': signal, 'strength': 1}
    if indicator_id == 'hashrate_difficulty_stress':
        value = last('network_stress_score')
        signal = 'STRESS' if value is not None and value >= 1 else 'RELIEF' if value is not None and value <= -1 else 'BALANCED'
        return {'label': 'NETWORK STRESS', 'display_value': '—' if value is None else f'{value:+.2f}', 'signal': signal, 'strength': 2 if value is not None and abs(value) >= 2 else 1}
    value = last('regime_score')
    cap = last('capitulation_probability_pct')
    wass = last('wasserstein_distance')
    signal = 'CAPITULATION' if value is not None and value >= 1 else 'RECOVERY' if value is not None and value <= -0.5 else 'TRANSITION'
    secondary = '—'
    if cap is not None or wass is not None:
        secondary = f'Capitulation {(0 if cap is None else cap):.0f}% · W {(0 if wass is None else wass):.2f}'
    return {'label': 'MINER REGIME', 'display_value': '—' if value is None else f'{value:+.2f}', 'signal': signal, 'strength': 2 if value is not None and abs(value) >= 2 else 1, 'secondary': secondary}

def _screen_miner_analysis(reference: Mapping[str, Any], processing: Mapping[str, Any]) -> dict[str, Any]:
    native = processing.get('miner_analysis', {})
    native = native if isinstance(native, Mapping) else {}
    native_indicators = native.get('indicators', {}) if isinstance(native.get('indicators'), Mapping) else {}
    timestamps = native.get('timestamps', [])
    timestamps = list(timestamps) if isinstance(timestamps, list) else []
    is_demo = bool(processing.get('context', {}).get('is_demo'))
    built_indicators: dict[str, Any] = {}
    for indicator_id, ref_indicator in reference.get('indicators', {}).items():
        package = native_indicators.get(indicator_id, {})
        package = package if isinstance(package, Mapping) else {}
        ranges: dict[str, Any] = {}
        for range_id, ref_range in ref_indicator.get('series_by_range', {}).items():
            count = _screen_RANGES.get(range_id, len(timestamps))
            selected_ts = timestamps[-count:]
            series = {}
            for series_id in ref_range.get('series', {}):
                values = package.get(series_id, [])
                series[series_id] = _screen_slice(values, len(selected_ts))
            dynamic_range = {'timestamps': selected_ts, 'series': series, 'point_count': len(selected_ts), 'status': 'available' if selected_ts else 'unavailable'}
            ranges[range_id] = _screen_shape(ref_range, dynamic_range)
        dynamic = {'indicator_id': indicator_id, 'status': native.get('status', 'unavailable'), 'data_mode': 'synthetic_emulator' if is_demo else 'live_provider', 'processing_contract_target': True, 'real_market_calculation': not is_demo, 'hmi_recalculate': False, 'series_by_range': ranges, 'summary': _screen_analysis_summary(indicator_id, package)}
        built_indicators[indicator_id] = _screen_shape(ref_indicator, dynamic)
    dynamic_root = {'analysis_id': 'on_chain_miners_native_v1', 'status': native.get('status', 'unavailable'), 'data_mode': 'synthetic_emulator' if is_demo else 'live_provider', 'processing_contract_target': True, 'hmi_computes_market_indicators': False, 'indicators': built_indicators}
    return _screen_shape(reference, dynamic_root)

def align_on_chain_miners_to_sp_v2_0(candidate: Mapping[str, Any], processing: Mapping[str, Any], classification: Mapping[str, Any]) -> dict[str, Any]:
    ref = _screen_template()
    context = _screen_context(ref['context'], processing, classification)
    charts = _screen_charts(ref['charts'], processing)
    drilldowns: dict[str, Any] = {}
    built = {'schema': {'id': ref['schema']['id'], 'version': _screen_VERSION}, 'screen': deepcopy(ref['screen']), 'stage': 'screen_contract', 'mode': processing.get('mode'), 'context': context, 'range_selector': _screen_range_selector(ref['range_selector']), 'operational_status': {**deepcopy(ref['operational_status']), 'data_mode': context.get('data_mode'), 'is_demo': context.get('is_demo'), 'quality_status': processing.get('quality', {}).get('status'), 'connection_status': 'emulator' if context.get('is_demo') else 'market_api', 'generated_at': context.get('generated_at'), 'data_as_of': context.get('data_as_of')}, 'charts': charts, 'widgets': _screen_widgets(ref['widgets'], candidate.get('widgets', {}), classification), 'quality': {}, 'history_contract': _screen_history(ref['history_contract'], processing), 'miner_analysis': _screen_miner_analysis(ref['miner_analysis'], processing)}
    built['quality'] = _screen_quality(ref['quality'], processing, classification, charts, drilldowns)
    built['operational_status']['quality_status'] = built['quality']['status']
    return _screen_shape(ref, built)
