"""Pure Classification v0.1 for Open Interest and Funding Processing output."""
from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from typing import Any, Callable

FAMILY = "open_interest_and_funding"
VERSION = "0.1"
TIMEFRAMES = ("5m", "15m", "4h")
MODES = {"bootstrap", "incremental", "recovery"}
STATUSES = {"available", "partial", "unavailable", "invalid"}
REQUIRED_TYPES = ("open_interest_change_state", "funding_state", "oi_funding_quadrant")
OPTIONAL_TYPES = ("oi_trend_strength", "directional_index_relation", "macd_relation",
                  "stochastic_range_state", "bollinger_position", "cci_state", "oi_roc_state")
ROOT_SECTIONS = ("context", "series", "indicators", "events", "snapshots", "confirmations", "availability", "quality")
EVENT_TYPES = {"moving_average_cross", "channel_cross", "macd_signal_cross", "stochastic_cross", "directional_indicator_cross",
               "adx_threshold_cross", "oi_roc_zero_cross", "funding_zero_cross"}
EVENT_STATES = {
    ("macd_signal_cross", "macd_x_signal", 1): "macd_crossed_above_signal",
    ("macd_signal_cross", "macd_x_signal", -1): "macd_crossed_below_signal",
    ("stochastic_cross", "k_x_d", 1): "k_crossed_above_d",
    ("stochastic_cross", "k_x_d", -1): "k_crossed_below_d",
    ("directional_indicator_cross", "di_plus_x_di_minus", 1): "di_plus_crossed_above_di_minus",
    ("directional_indicator_cross", "di_plus_x_di_minus", -1): "di_plus_crossed_below_di_minus",
    ("channel_cross", "regression_middle_x_bollinger_middle", 1): "regression_middle_crossed_above_bollinger_middle",
    ("channel_cross", "regression_middle_x_bollinger_middle", -1): "regression_middle_crossed_below_bollinger_middle",
    ("adx_threshold_cross", "adx_x_25", 1): "adx_crossed_above_25",
    ("adx_threshold_cross", "adx_x_25", -1): "adx_crossed_below_25",
    ("oi_roc_zero_cross", "oi_roc_12_x_0", 1): "oi_roc_crossed_above_zero",
    ("oi_roc_zero_cross", "oi_roc_12_x_0", -1): "oi_roc_crossed_below_zero",
    ("funding_zero_cross", "funding_close_x_0", 1): "funding_crossed_above_zero",
    ("funding_zero_cross", "funding_close_x_0", -1): "funding_crossed_below_zero",
}
_MA_EVENT_PAIRS = (
    ("ema_9", "ema_21"), ("ema_9", "ema_50"), ("ema_21", "ema_50"),
    ("sma_20", "sma_50"), ("sma_20", "sma_100"), ("sma_20", "sma_200"),
    ("sma_50", "sma_100"), ("sma_50", "sma_200"), ("sma_100", "sma_200"),
    ("wma_20", "wma_50"),
)
for _fast, _slow in _MA_EVENT_PAIRS:
    _pair_name = f"{_fast}_x_{_slow}"
    EVENT_STATES[("moving_average_cross", _pair_name, 1)] = f"{_fast}_crossed_above_{_slow}"
    EVENT_STATES[("moving_average_cross", _pair_name, -1)] = f"{_fast}_crossed_below_{_slow}"

EVENT_TYPES_FOR_ATOM = {
    "funding_state": {"funding_zero_cross"}, "oi_funding_quadrant": {"funding_zero_cross"},
    "oi_trend_strength": {"adx_threshold_cross"}, "directional_index_relation": {"directional_indicator_cross"},
    "macd_relation": {"macd_signal_cross"}, "stochastic_range_state": {"stochastic_cross"},
    "oi_roc_state": {"oi_roc_zero_cross"},
}
OWN_REASONS = {"classification_source_unavailable", "classification_source_partial",
               "classification_current_not_available", "classification_unit_mismatch",
               "classification_required_value_missing", "classification_input_invalid",
               "classification_state_not_determinable", "classification_timestamp_mismatch",
               "classification_event_invalid", "classification_event_id_duplicate",
               "classification_series_units_mismatch"}


def _json_copy(value: Any, path: str = "root") -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"classification_input_invalid:{path}")
        return 0.0 if value == 0.0 else value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError(f"classification_input_invalid:{path}")
        return {key: _json_copy(item, f"{path}.{key}") for key, item in value.items()}
    if isinstance(value, list):
        return [_json_copy(item, f"{path}[{index}]") for index, item in enumerate(value)]
    raise ValueError(f"classification_input_invalid:{path}")


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return 0.0 if value == 0.0 else value


def _timestamp(value: Any) -> int | None:
    return value if type(value) is int else None


def _current_timestamp(wrapper: Mapping[str, Any], current: Mapping[str, Any], *, allow_implicit: bool = False) -> tuple[int | None, bool]:
    wrapper_value = wrapper.get("current_timestamp")
    current_value = current.get("timestamp")
    wrapper_timestamp = _timestamp(wrapper_value)
    current_timestamp = _timestamp(current_value)
    if allow_implicit and "current_timestamp" not in wrapper:
        wrapper_timestamp = current_timestamp
    if wrapper_timestamp is None:
        return None, False
    if "timestamp" in current and (current_timestamp is None or current_timestamp != wrapper_timestamp):
        return None, False
    return wrapper_timestamp, True


def _source_timeframe_valid(wrapper: Mapping[str, Any], timeframe: str) -> bool:
    source = wrapper.get("source")
    return isinstance(source, Mapping) and type(source.get("timeframe")) is str and source.get("timeframe") == timeframe


def _reason(source: Mapping[str, Any], fallback: str) -> str:
    value = source.get("reason")
    return value if isinstance(value, str) and value else fallback


def _atom(*, classification_type: str, timeframe: str, status: str, state: str | None, reason: str | None,
          timestamp: int | None, source_path: str, values: Mapping[str, Any], units: Mapping[str, str],
          event_ids: Sequence[str] = ()) -> dict[str, Any]:
    if status not in STATUSES:
        status, state, reason = "invalid", None, "classification_input_invalid"
    if status in {"unavailable", "invalid"}:
        state = None
    if status == "available":
        reason = None
    elif not isinstance(reason, str) or not reason:
        reason = "classification_input_invalid" if status == "invalid" else "classification_source_partial"
    normalized_values = {key: _number(value) for key, value in values.items()}
    normalized_units = dict(units)
    if set(normalized_values) != set(normalized_units):
        status, state, reason = "invalid", None, "classification_series_units_mismatch"
    usable_timestamp = _timestamp(timestamp)
    classification_id = (f"{FAMILY}:{timeframe}:{usable_timestamp}:{classification_type}"
                         if usable_timestamp is not None else None)
    return {"classification_id": classification_id, "type": classification_type, "status": status, "state": state,
            "reason": reason, "evidence": {"timeframe": timeframe, "timestamp": usable_timestamp,
                "source_path": source_path, "values": normalized_values, "units": normalized_units,
                "event_ids": sorted(set(event_ids))}}


def _wrapper_atom(*, wrapper: Any, classification_type: str, timeframe: str, source_path: str,
                  fields: Mapping[str, str], state_builder: Callable[[Mapping[str, int | float]], str],
                  events_at_timestamp: Mapping[int, list[Mapping[str, Any]]], include_fields: Sequence[str] | None = None) -> dict[str, Any]:
    empty = {"classification_type": classification_type, "timeframe": timeframe, "timestamp": None,
             "source_path": source_path, "values": {}, "units": {}, "event_ids": ()}
    if not isinstance(wrapper, Mapping):
        return _atom(status="invalid", state=None, reason="classification_input_invalid", **empty)
    if not _source_timeframe_valid(wrapper, timeframe):
        return _atom(status="invalid", state=None, reason="classification_input_invalid", **empty)
    status = wrapper.get("status")
    if status not in STATUSES:
        return _atom(status="invalid", state=None, reason="classification_input_invalid", **empty)
    series, units = wrapper.get("series"), wrapper.get("units")
    if not isinstance(series, Mapping) or not isinstance(units, Mapping) or set(series) != set(units):
        return _atom(status="invalid", state=None, reason="classification_series_units_mismatch", **empty)
    if status == "invalid":
        return _atom(status="invalid", state=None, reason=_reason(wrapper, "classification_input_invalid"), **empty)
    if status == "unavailable":
        return _atom(status="unavailable", state=None, reason=_reason(wrapper, "classification_source_unavailable"), **empty)
    current = wrapper.get("current")
    if current is None:
        return _atom(status="unavailable", state=None,
                     reason=_reason(wrapper, "classification_current_not_available"), **empty)
    if not isinstance(current, Mapping):
        return _atom(status="invalid", state=None, reason="classification_input_invalid", **empty)
    current_timestamp, timestamp_valid = _current_timestamp(wrapper, current)
    if not timestamp_valid:
        return _atom(status="invalid", state=None, reason="classification_timestamp_mismatch", **empty)
    values: dict[str, int | float] = {}
    expected_units: dict[str, str] = {}
    for field, expected_unit in fields.items():
        if units.get(field) != expected_unit:
            return _atom(status="invalid", state=None, reason="classification_unit_mismatch", **empty)
        numeric = _number(current.get(field))
        if numeric is None:
            return _atom(status="invalid", state=None, reason="classification_required_value_missing", **empty)
        values[field] = numeric
        expected_units[field] = expected_unit
    evidence_fields = tuple(include_fields or fields)
    evidence_values = {field: values[field] for field in evidence_fields}
    evidence_units = {field: expected_units[field] for field in evidence_fields}
    relevant_types = EVENT_TYPES_FOR_ATOM.get(classification_type, set())
    event_ids = [event["event_id"] for event in events_at_timestamp.get(current_timestamp, [])
                 if event["event_type"] in relevant_types]
    output_status = "partial" if status == "partial" else "available"
    output_reason = _reason(wrapper, "classification_source_partial") if output_status == "partial" else None
    return _atom(classification_type=classification_type, timeframe=timeframe, status=output_status,
                 state=state_builder(values), reason=output_reason, timestamp=current_timestamp,
                 source_path=source_path, values=evidence_values, units=evidence_units, event_ids=event_ids)


def _funding_atom(frame: Any, timeframe: str, events_at_timestamp: Mapping[int, list[Mapping[str, Any]]]) -> dict[str, Any]:
    path = f"series.funding_rate_ohlc.timeframes.{timeframe}.current.close"
    empty = {"classification_type": "funding_state", "timeframe": timeframe, "timestamp": None,
             "source_path": path, "values": {}, "units": {}, "event_ids": ()}
    if not isinstance(frame, Mapping) or frame.get("status") not in STATUSES:
        return _atom(status="invalid", state=None, reason="classification_input_invalid", **empty)
    status = frame["status"]
    if frame.get("timeframe") != timeframe or not _source_timeframe_valid(frame, timeframe):
        return _atom(status="invalid", state=None, reason="classification_input_invalid", **empty)
    if frame.get("unit") != "percent_points" or frame.get("representation") != "percentage_points":
        return _atom(status="invalid", state=None, reason="classification_unit_mismatch", **empty)
    if status == "invalid":
        return _atom(status="invalid", state=None, reason=_reason(frame, "classification_input_invalid"), **empty)
    if status == "unavailable":
        return _atom(status="unavailable", state=None, reason=_reason(frame, "classification_source_unavailable"), **empty)
    current = frame.get("current")
    if current is None:
        return _atom(status="unavailable", state=None, reason=_reason(frame, "classification_current_not_available"), **empty)
    if not isinstance(current, Mapping):
        return _atom(status="invalid", state=None, reason="classification_input_invalid", **empty)
    timestamp, timestamp_valid = _current_timestamp(frame, current, allow_implicit=True)
    value = _number(current.get("close"))
    if not timestamp_valid:
        return _atom(status="invalid", state=None, reason="classification_timestamp_mismatch", **empty)
    if value is None:
        return _atom(status="invalid", state=None, reason="classification_required_value_missing", **empty)
    state = "positive" if value > 0 else "negative" if value < 0 else "neutral"
    event_ids = [event["event_id"] for event in events_at_timestamp.get(timestamp, [])
                 if event["event_type"] == "funding_zero_cross"]
    result_status = "partial" if status == "partial" else "available"
    reason = _reason(frame, "classification_source_partial") if result_status == "partial" else None
    return _atom(classification_type="funding_state", timeframe=timeframe, status=result_status, state=state,
                 reason=reason, timestamp=timestamp, source_path=path, values={"funding_close": value},
                 units={"funding_close": "percent_points"}, event_ids=event_ids)


def _quadrant(oi: Mapping[str, Any], funding: Mapping[str, Any], timeframe: str) -> dict[str, Any]:
    path = "classifications.current.open_interest_change_state+funding_state"
    oi_status, funding_status = oi["status"], funding["status"]
    timestamps = (oi["evidence"]["timestamp"], funding["evidence"]["timestamp"])
    values = {"open_interest_change_state": oi.get("state"), "funding_state": funding.get("state")}
    numeric_values: dict[str, Any] = {}
    event_ids = [*oi["evidence"]["event_ids"], *funding["evidence"]["event_ids"]]
    if "invalid" in (oi_status, funding_status):
        return _atom(classification_type="oi_funding_quadrant", timeframe=timeframe, status="invalid", state=None,
                     reason="classification_input_invalid", timestamp=None, source_path=path, values=numeric_values, units={}, event_ids=event_ids)
    if all(item is not None for item in timestamps) and timestamps[0] != timestamps[1]:
        return _atom(classification_type="oi_funding_quadrant", timeframe=timeframe, status="unavailable", state=None,
                     reason="classification_timestamp_mismatch", timestamp=None, source_path=path, values=numeric_values, units={}, event_ids=event_ids)
    if "unavailable" in (oi_status, funding_status):
        source = oi if oi_status == "unavailable" else funding
        return _atom(classification_type="oi_funding_quadrant", timeframe=timeframe, status="unavailable", state=None,
                     reason=source["reason"] or "classification_source_unavailable", timestamp=None,
                     source_path=path, values=numeric_values, units={}, event_ids=event_ids)
    oi_state, funding_state = values["open_interest_change_state"], values["funding_state"]
    if oi_state not in {"expanding", "contracting", "unchanged"} or funding_state not in {"positive", "negative", "neutral"}:
        return _atom(classification_type="oi_funding_quadrant", timeframe=timeframe, status="invalid", state=None,
                     reason="classification_state_not_determinable", timestamp=None, source_path=path, values={}, units={}, event_ids=event_ids)
    suffix = {"expanding": "expansion", "contracting": "contraction", "unchanged": "unchanged"}[oi_state]
    result_status = "partial" if "partial" in (oi_status, funding_status) else "available"
    reason = "classification_source_partial" if result_status == "partial" else None
    return _atom(classification_type="oi_funding_quadrant", timeframe=timeframe, status=result_status,
                 state=f"{funding_state}_funding_{suffix}", reason=reason, timestamp=timestamps[0],
                 source_path=path, values={}, units={}, event_ids=event_ids)


def _pair(event: Mapping[str, Any]) -> str | None:
    event_type, first, second, threshold = (event.get("event_type"), event.get("first_series"),
                                             event.get("second_series"), event.get("threshold"))
    if event_type == "moving_average_cross" and (first, second) in _MA_EVENT_PAIRS:
        return f"{first}_x_{second}"
    if event_type == "channel_cross" and first == "regression_middle" and second == "bollinger_middle":
        return "regression_middle_x_bollinger_middle"
    if event_type == "macd_signal_cross" and first == "macd" and second == "signal":
        return "macd_x_signal"
    if event_type == "stochastic_cross" and first == "k" and second == "d":
        return "k_x_d"
    if event_type == "directional_indicator_cross" and first == "di_plus" and second == "di_minus":
        return "di_plus_x_di_minus"
    if event_type == "adx_threshold_cross" and first == "adx" and second is None and threshold == 25.0:
        return "adx_x_25"
    if event_type == "oi_roc_zero_cross" and first == "roc" and second is None and threshold == 0.0:
        return "oi_roc_12_x_0"
    if event_type == "funding_zero_cross" and first == "funding_close" and second is None and threshold == 0.0:
        return "funding_close_x_0"
    return None


def _safe_event_mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    try:
        copied = _json_copy(value)
    except ValueError:
        return None
    return copied


def _invalid_event(event_id: str, timeframe: str, event: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    timestamp = _timestamp(event.get("timestamp"))
    timestamp_token: int | str = timestamp if timestamp is not None else "invalid"
    interpretation_id = f"{FAMILY}:{timeframe}:{timestamp_token}:interpreted_event:{event_id}"
    evidence = {key: _json_copy(value) for key, value in event.items()
                if key not in {"values", "parameters"} and isinstance(key, str)}
    evidence["values"] = _safe_event_mapping(event.get("values")) or {}
    evidence["parameters"] = _safe_event_mapping(event.get("parameters")) or {}
    return interpretation_id, {"interpretation_id": interpretation_id, "event_id": event_id,
        "event_type": event.get("event_type") if isinstance(event.get("event_type"), str) else None,
        "timestamp": timestamp, "timeframe": timeframe, "status": "invalid", "state": None,
        "reason": "classification_event_invalid", "evidence": {"source_event": evidence}}


def _events(source: Any) -> tuple[dict[str, Any], dict[str, dict[int, list[Mapping[str, Any]]]], list[str]]:
    if not isinstance(source, Mapping) or not isinstance(source.get("by_id"), Mapping) or not isinstance(source.get("timeframes"), Mapping):
        raise ValueError("classification_input_invalid:events")
    by_id, references = source["by_id"], source["timeframes"]
    if tuple(references) != TIMEFRAMES:
        raise ValueError("classification_input_invalid:events.timeframes")
    seen: list[str] = []
    warnings: list[str] = []
    interpreted: dict[str, Any] = {}
    result_refs: dict[str, dict[str, list[str]]] = {}
    indexed: dict[str, dict[int, list[Mapping[str, Any]]]] = {timeframe: {} for timeframe in TIMEFRAMES}
    for timeframe in TIMEFRAMES:
        payload = references.get(timeframe)
        if not isinstance(payload, Mapping) or not isinstance(payload.get("event_ids"), list):
            raise ValueError("classification_input_invalid:events.timeframes")
        ids = payload["event_ids"]
        if len(ids) != len(set(ids)):
            raise ValueError("classification_event_id_duplicate")
        expected_order: list[tuple[int, str, str]] = []
        interpreted_ids: list[str] = []
        for event_id in ids:
            if not isinstance(event_id, str) or event_id not in by_id:
                raise ValueError("classification_event_invalid")
            event = by_id[event_id]
            if not isinstance(event, Mapping) or event.get("event_id") != event_id:
                raise ValueError("classification_event_invalid")
            if event.get("event_type") not in EVENT_TYPES or event.get("timeframe") != timeframe:
                raise ValueError("classification_event_invalid")
            timestamp, direction = _timestamp(event.get("timestamp")), event.get("direction_numeric")
            values, parameters = _safe_event_mapping(event.get("values")), _safe_event_mapping(event.get("parameters"))
            if timestamp is None or type(direction) is not int or direction not in {-1, 1} or values is None or parameters is None:
                interpretation_id, invalid = _invalid_event(event_id, timeframe, event)
                interpreted[interpretation_id] = invalid
                interpreted_ids.append(interpretation_id)
                expected_order.append((timestamp if timestamp is not None else -1, str(event.get("event_type")), event_id))
                seen.append(event_id)
                warnings.append(f"classification_event_invalid:{event_id}")
                continue
            pair = _pair(event)
            state = EVENT_STATES.get((event["event_type"], pair, direction))
            if state is None:
                raise ValueError("classification_event_invalid")
            interpretation_id = f"{FAMILY}:{timeframe}:{timestamp}:interpreted_event:{event_id}"
            interpreted[interpretation_id] = {"interpretation_id": interpretation_id, "event_id": event_id,
                "event_type": event["event_type"], "timestamp": timestamp, "timeframe": timeframe,
                "status": "available", "state": state, "reason": None, "evidence": {"source_event": _json_copy(event)}}
            interpreted_ids.append(interpretation_id)
            indexed[timeframe].setdefault(timestamp, []).append(event)
            expected_order.append((timestamp, event["event_type"], event_id))
            seen.append(event_id)
        if expected_order != sorted(expected_order):
            raise ValueError("classification_event_invalid")
        result_refs[timeframe] = {"event_ids": interpreted_ids}
    if len(seen) != len(set(seen)):
        raise ValueError("classification_event_id_duplicate")
    if set(seen) != set(by_id) or len(seen) != len(by_id):
        raise ValueError("classification_event_invalid")
    ordered = dict(sorted(interpreted.items(), key=lambda item: (
        item[1]["timestamp"] if item[1]["timestamp"] is not None else -1,
        item[1]["event_type"] or "", item[1]["event_id"])))
    return {"by_id": ordered, "timeframes": result_refs}, indexed, sorted(set(warnings))


def _processing_contract(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("classification_input_invalid:root")
    candidate = value.get("processing", value)
    if not isinstance(candidate, Mapping):
        raise ValueError("classification_input_invalid:processing")
    validation_view = dict(candidate)
    source_events = candidate.get("events")
    if isinstance(source_events, Mapping):
        validation_events = dict(source_events)
        source_by_id = source_events.get("by_id")
        if isinstance(source_by_id, Mapping):
            validation_events["by_id"] = {event_id: ({**event, "values": {}, "parameters": {}}
                if isinstance(event, Mapping) else event) for event_id, event in source_by_id.items()}
        validation_view["events"] = validation_events
    _json_copy(validation_view)
    for field, expected in (("family", FAMILY), ("stage", "processing"), ("version", VERSION)):
        if candidate.get(field) != expected:
            raise ValueError(f"classification_input_invalid:{field}")
    if candidate.get("mode") not in MODES:
        raise ValueError("classification_input_invalid:mode")
    for section in ROOT_SECTIONS:
        if not isinstance(candidate.get(section), Mapping):
            raise ValueError(f"classification_input_invalid:{section}")
    series = candidate["series"]
    indicators = candidate["indicators"]
    for metric in ("open_interest_ohlc", "funding_rate_ohlc"):
        if not isinstance(series.get(metric), Mapping) or not isinstance(series[metric].get("timeframes"), Mapping):
            raise ValueError(f"classification_input_invalid:series.{metric}")
        if tuple(series[metric]["timeframes"]) != TIMEFRAMES:
            raise ValueError(f"classification_input_invalid:series.{metric}.timeframes")
    oi_indicators = indicators.get("open_interest")
    if not isinstance(oi_indicators, Mapping) or not isinstance(oi_indicators.get("timeframes"), Mapping):
        raise ValueError("classification_input_invalid:indicators.open_interest")
    if tuple(oi_indicators["timeframes"]) != TIMEFRAMES:
        raise ValueError("classification_input_invalid:indicators.open_interest.timeframes")
    if candidate["quality"].get("status") not in {"ok", "partial", "invalid"}:
        raise ValueError("classification_input_invalid:quality.status")
    return candidate


def _provider_copy(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {"status": "invalid", "reason": "classification_input_invalid", "provider_state": "provider_invalid"}
    copied = _json_copy(payload)
    status = copied.get("status")
    if status not in STATUSES:
        copied.update(status="invalid", reason="classification_input_invalid")
        status = "invalid"
    copied["provider_state"] = f"provider_{status}"
    return copied


def _confirmations(source: Mapping[str, Any]) -> dict[str, Any]:
    leverage = source.get("estimated_leverage_ratio")
    if not isinstance(leverage, Mapping):
        leverage = {}
    return {"estimated_leverage_ratio": {"glassnode": _provider_copy(leverage.get("glassnode"))}}


def _availability(by_timeframe: Mapping[str, Any], snapshots: Mapping[str, Any], confirmations: Mapping[str, Any],
                  processing_availability: Mapping[str, Any], optional_calculations: set[str]) -> dict[str, Any]:
    required: dict[str, dict[str, str]] = {name: {} for name in REQUIRED_TYPES}
    optional: dict[str, dict[str, str]] = {name: {} for name in (*REQUIRED_TYPES, *OPTIONAL_TYPES)}
    for timeframe in TIMEFRAMES:
        dynamic_optional = f"oi_change_24h.{timeframe}" in optional_calculations
        for name in REQUIRED_TYPES:
            status = by_timeframe[timeframe]["current"][name]["status"]
            if dynamic_optional and name in {"open_interest_change_state", "oi_funding_quadrant"}:
                optional[name][timeframe] = status
            else:
                required[name][timeframe] = status
        for name in OPTIONAL_TYPES:
            optional[name][timeframe] = by_timeframe[timeframe]["current"][name]["status"]
    optional = {name: values for name, values in optional.items() if values}
    required = {name: values for name, values in required.items() if values}
    passthrough = {"snapshots": {name: payload.get("status", "invalid") if isinstance(payload, Mapping) else "invalid"
                                  for name, payload in snapshots.items()},
                   "confirmations": {
                       "estimated_leverage_ratio": {"glassnode": confirmations["estimated_leverage_ratio"]["glassnode"]["status"]},
                   }}
    unavailable_names = {
        "funding_8h_aggregate": "funding_8h_aggregate",
        "contract_type_split": "contract_type_split",
        "mfi": "mfi",
    }
    unavailable: dict[str, Any] = {}
    for output_name, source_name in unavailable_names.items():
        payload = processing_availability.get(source_name)
        unavailable[output_name] = (_json_copy(payload) if isinstance(payload, Mapping) else
                                    {"status": "unavailable", "reason": "classification_source_unavailable"})
    unavailable["provider_comparisons"] = {"status": "unavailable", "reason": "oi_provider_comparisons_not_contracted_final33"}
    unavailable["series_snapshot_comparison"] = {"status": "unavailable", "reason": "observation_scope_or_timestamp_not_comparable"}
    return {"required": required, "optional": optional, "passthrough": passthrough, "unavailable": unavailable}


def _leverage_context(confirmations: Mapping[str, Any]) -> dict[str, Any]:
    payload = confirmations.get("estimated_leverage_ratio", {}).get("glassnode", {}) if isinstance(confirmations, Mapping) else {}
    if not isinstance(payload, Mapping) or payload.get("status") not in {"available", "partial"}:
        return {"status": "unavailable", "state": None, "value": None, "recent_median": None,
                "reason": payload.get("reason") if isinstance(payload, Mapping) else "source_unavailable",
                "provider": "glassnode", "endpoint_id": "futures_estimated_leverage_ratio"}
    records = payload.get("records") if isinstance(payload.get("records"), list) else []
    values = [float(row["value"]) for row in records if isinstance(row, Mapping) and isinstance(row.get("value"), (int, float)) and not isinstance(row.get("value"), bool)]
    if not values:
        return {"status": "unavailable", "state": None, "value": None, "recent_median": None, "reason": "empty_data",
                "provider": "glassnode", "endpoint_id": "futures_estimated_leverage_ratio"}
    recent = sorted(values[-30:])
    n = len(recent)
    median = recent[n // 2] if n % 2 else (recent[n // 2 - 1] + recent[n // 2]) / 2.0
    current = values[-1]
    state = "above_recent_median" if current > median else "below_recent_median" if current < median else "at_recent_median"
    return {"status": payload.get("status"), "state": state, "value": current, "recent_median": median, "reason": None,
            "provider": "glassnode", "endpoint_id": "futures_estimated_leverage_ratio", "window_records": n}


def classify_open_interest_and_funding(processing_contract: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and classify a direct Processing contract or a bundle containing ``processing``."""
    contract = _processing_contract(processing_contract)
    optional_calculations = set(contract.get("quality", {}).get("optional_calculations", []))
    interpreted_events, indexed_events, event_warnings = _events(contract["events"])
    oi_frames = contract["series"]["open_interest_ohlc"]["timeframes"]
    funding_frames = contract["series"]["funding_rate_ohlc"]["timeframes"]
    indicator_frames = contract["indicators"]["open_interest"]["timeframes"]
    by_timeframe: dict[str, Any] = {}
    for timeframe in TIMEFRAMES:
        events_at_timestamp = indexed_events[timeframe]
        oi_frame, indicators = oi_frames[timeframe], indicator_frames[timeframe]
        if not isinstance(oi_frame, Mapping) or oi_frame.get("timeframe") != timeframe or not isinstance(indicators, Mapping):
            raise ValueError(f"classification_input_invalid:timeframe.{timeframe}")
        derived = oi_frame.get("derived")
        if not isinstance(derived, Mapping):
            raise ValueError(f"classification_input_invalid:timeframe.{timeframe}.derived")
        oi_change = _wrapper_atom(wrapper=derived.get("oi_change_24h"), classification_type="open_interest_change_state",
            timeframe=timeframe, source_path=f"series.open_interest_ohlc.timeframes.{timeframe}.derived.oi_change_24h.current.change_percent",
            fields={"change_percent": "percent"}, state_builder=lambda v: "expanding" if v["change_percent"] > 0 else "contracting" if v["change_percent"] < 0 else "unchanged",
            events_at_timestamp=events_at_timestamp)
        funding = _funding_atom(funding_frames[timeframe], timeframe, events_at_timestamp)
        current = {"open_interest_change_state": oi_change, "funding_state": funding,
                   "oi_funding_quadrant": _quadrant(oi_change, funding, timeframe)}
        current["oi_trend_strength"] = _wrapper_atom(wrapper=indicators.get("adx"), classification_type="oi_trend_strength",
            timeframe=timeframe, source_path=f"indicators.open_interest.timeframes.{timeframe}.adx.current.adx",
            fields={"adx": "index_0_100"}, state_builder=lambda v: "strong" if v["adx"] > 25 else "weak" if v["adx"] < 25 else "exactly_threshold",
            events_at_timestamp=events_at_timestamp)
        current["directional_index_relation"] = _wrapper_atom(wrapper=indicators.get("adx"), classification_type="directional_index_relation",
            timeframe=timeframe, source_path=f"indicators.open_interest.timeframes.{timeframe}.adx.current",
            fields={"di_plus": "index_0_100", "di_minus": "index_0_100"}, state_builder=lambda v: "di_plus_dominant" if v["di_plus"] > v["di_minus"] else "di_minus_dominant" if v["di_plus"] < v["di_minus"] else "balanced",
            events_at_timestamp=events_at_timestamp)
        current["macd_relation"] = _wrapper_atom(wrapper=indicators.get("macd"), classification_type="macd_relation",
            timeframe=timeframe, source_path=f"indicators.open_interest.timeframes.{timeframe}.macd.current",
            fields={"macd": "USD", "signal": "USD"}, state_builder=lambda v: "above_signal" if v["macd"] > v["signal"] else "below_signal" if v["macd"] < v["signal"] else "equal_signal",
            events_at_timestamp=events_at_timestamp)
        current["stochastic_range_state"] = _wrapper_atom(wrapper=indicators.get("stochastic"), classification_type="stochastic_range_state",
            timeframe=timeframe, source_path=f"indicators.open_interest.timeframes.{timeframe}.stochastic.current",
            fields={"k": "index_0_100", "d": "index_0_100"}, include_fields=("k", "d"),
            state_builder=lambda v: "low_range" if v["k"] <= 20 else "high_range" if v["k"] >= 80 else "mid_range",
            events_at_timestamp=events_at_timestamp)
        current["bollinger_position"] = _wrapper_atom(wrapper=indicators.get("bollinger_bands"), classification_type="bollinger_position",
            timeframe=timeframe, source_path=f"indicators.open_interest.timeframes.{timeframe}.bollinger_bands.current.percent_b",
            fields={"percent_b": "ratio"}, state_builder=lambda v: "below_lower_band" if v["percent_b"] < 0 else "lower_half" if v["percent_b"] < 0.5 else "on_middle" if v["percent_b"] == 0.5 else "upper_half" if v["percent_b"] <= 1 else "above_upper_band",
            events_at_timestamp=events_at_timestamp)
        current["cci_state"] = _wrapper_atom(wrapper=indicators.get("cci"), classification_type="cci_state",
            timeframe=timeframe, source_path=f"indicators.open_interest.timeframes.{timeframe}.cci.current.cci",
            fields={"cci": "index"}, state_builder=lambda v: "high_positive" if v["cci"] >= 100 else "high_negative" if v["cci"] <= -100 else "neutral",
            events_at_timestamp=events_at_timestamp)
        current["oi_roc_state"] = _wrapper_atom(wrapper=indicators.get("oi_roc"), classification_type="oi_roc_state",
            timeframe=timeframe, source_path=f"indicators.open_interest.timeframes.{timeframe}.oi_roc.current.roc",
            fields={"roc": "percent"}, state_builder=lambda v: "positive" if v["roc"] > 0 else "negative" if v["roc"] < 0 else "neutral",
            events_at_timestamp=events_at_timestamp)
        if contract["quality"].get("status") == "invalid":
            processing_reason = contract["quality"].get("reason")
            if not isinstance(processing_reason, str) or not processing_reason:
                processing_errors = contract["quality"].get("errors")
                processing_reason = (next((item for item in processing_errors if isinstance(item, str) and item), None)
                                     if isinstance(processing_errors, list) else None)
            processing_reason = processing_reason or "classification_input_invalid"
            for name in REQUIRED_TYPES:
                source_path = current[name]["evidence"]["source_path"]
                current[name] = _atom(classification_type=name, timeframe=timeframe, status="invalid", state=None,
                                      reason=processing_reason, timestamp=None, source_path=source_path,
                                      values={}, units={}, event_ids=())
        dynamic_optional = f"oi_change_24h.{timeframe}" in optional_calculations
        required_names = ("funding_state",) if dynamic_optional else REQUIRED_TYPES
        required_statuses = [current[name]["status"] for name in required_names]
        timeframe_status = "invalid" if "invalid" in required_statuses else "available" if all(item == "available" for item in required_statuses) else "partial"
        timeframe_reason = None if timeframe_status == "available" else "classification_input_invalid" if timeframe_status == "invalid" else "classification_source_partial"
        timestamps = {current[name]["evidence"]["timestamp"] for name in required_names if current[name]["evidence"]["timestamp"] is not None}
        by_timeframe[timeframe] = {"status": timeframe_status, "reason": timeframe_reason,
                                  "timestamp": next(iter(timestamps)) if len(timestamps) == 1 else None, "current": current}
    snapshots = _json_copy(contract["snapshots"])
    confirmations = _confirmations(contract["confirmations"])
    availability = _availability(by_timeframe, snapshots, confirmations, contract["availability"], optional_calculations)
    required_atoms: dict[str, Any] = {}
    optional_atoms: dict[str, Any] = {}
    for timeframe in TIMEFRAMES:
        dynamic_optional = f"oi_change_24h.{timeframe}" in optional_calculations
        for name in REQUIRED_TYPES:
            target = optional_atoms if dynamic_optional and name in {"open_interest_change_state", "oi_funding_quadrant"} else required_atoms
            target[f"{timeframe}.{name}"] = by_timeframe[timeframe]["current"][name]
        for name in OPTIONAL_TYPES:
            optional_atoms[f"{timeframe}.{name}"] = by_timeframe[timeframe]["current"][name]
    required_statuses = {name: atom["status"] for name, atom in required_atoms.items()}
    optional_statuses = {name: atom["status"] for name, atom in optional_atoms.items()}
    if contract["quality"].get("status") == "invalid" or "invalid" in required_statuses.values():
        quality_status, data_complete = "invalid", False
    elif all(status == "available" for status in required_statuses.values()):
        quality_status, data_complete = "ok", True
    else:
        quality_status, data_complete = "partial", False
    warnings = [*event_warnings,
                *(f"optional_{status}:{name}" for name, status in optional_statuses.items() if status != "available")]
    warnings.extend(f"snapshot_{payload.get('status', 'invalid')}:{name}" for name, payload in snapshots.items()
                    if not isinstance(payload, Mapping) or payload.get("status") == "invalid")
    if confirmations["estimated_leverage_ratio"]["glassnode"]["status"] == "invalid":
        warnings.append("confirmation_invalid:estimated_leverage_ratio.glassnode")
    errors = [f"required_invalid:{name}" for name, status in required_statuses.items() if status == "invalid"]
    classification_quality = {"status": quality_status, "required_statuses": required_statuses,
                              "optional_statuses": optional_statuses,
                              "passthrough_statuses": _json_copy(availability["passthrough"])}
    result = {"family": FAMILY, "stage": "classification", "version": VERSION, "mode": contract["mode"],
        "context": _json_copy(contract["context"]), "classifications": {"by_timeframe": by_timeframe, "leverage_context": _leverage_context(confirmations)},
        "interpreted_events": interpreted_events, "snapshots": snapshots, "confirmations": confirmations,
        "availability": availability, "quality": {"status": quality_status, "contract_complete": True,
            "data_complete": data_complete, "processing_quality": _json_copy(contract["quality"]),
            "classification_quality": classification_quality, "warnings": sorted(set(warnings)), "errors": sorted(set(errors))}}
    result = _json_copy(result)
    json.dumps(result, ensure_ascii=False, allow_nan=False, sort_keys=False)
    return result


class OpenInterestAndFundingClassifier:
    """Object facade for the pure Open Interest and Funding classifier."""

    def classify(self, processing_contract: Mapping[str, Any]) -> dict[str, Any]:
        return classify_open_interest_and_funding(processing_contract)
