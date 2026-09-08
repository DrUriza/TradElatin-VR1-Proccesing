"""Pure screen-contract assembly for Open Interest and Funding v0.1."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
import json
import math
from typing import Any



FAMILY = "open_interest_and_funding"
VERSION = "0.1"
SCREEN_SCHEMA = "trad_elatin.open_interest_and_funding.screen.v1"
SCREEN_VERSION = "1.12.0-oi-native-screen-b-demo"
TIMEFRAMES = ("5m", "15m", "4h")
HMI_TIMEFRAMES = ("5m", "15m", "4h")
TIMEFRAME_SECONDS = {"5m": 300, "15m": 900, "4h": 14_400}
STATUSES = ("available", "partial", "unavailable", "invalid")
CONTEXT_FIELDS = (
    "asset", "exchange_scope", "primary_provider", "confirmation_providers", "data_mode", "is_demo",
    "reference_timestamp", "execution_timestamp", "generated_at",
)
OPTIONAL_CONTEXT_FIELDS = ("requested_at", "include_snapshots", "include_confirmations", "refresh_secondary")
CHART_IDS = (
    "open_interest_candlestick", "open_interest_ohlc", "macd", "rsi", "tsi", "adx",
    "stochastic", "williams_r", "cci", "atr", "wasserstein_distance",
    "bollinger_band_width", "oi_roc", "funding_rate",
)
REQUIRED_IDS = (
    "timeframe_selector", "kpis.open_interest_usd", "kpis.oi_change_24h", "kpis.funding_rate",
    "charts.open_interest_line", "charts.funding_rate_line", "charts.open_interest_candlestick",
    "charts.funding_candlestick", "tables.oi_technical_indicators",
)
OPTIONAL_IDS = (
    "charts.open_interest_interval_delta", "charts.oi_funding_overlay", "charts.bollinger_bands",
    "charts.macd", "charts.adx_di", "charts.stochastic", "charts.atr", "charts.cci", "charts.oi_roc",
    "widgets.oi_funding_state", "widgets.provider_availability", "drilldowns.open_interest_by_exchange",
    "drilldowns.funding_rate_by_exchange", "drilldowns.options_open_interest", "events.recent_events",
)
PLACEHOLDER_IDS = (
    "funding_8h", "mfi", "oi_vs_price", "contract_type_split",
    "provider_comparisons",
)
SOURCE_PREFIXES = (
    "series.open_interest_ohlc", "series.funding_rate_ohlc", "indicators.open_interest",
    "classifications.by_timeframe", "interpreted_events", "snapshots", "confirmations",
    "availability.unavailable",
)


def _error(path: str) -> ValueError:
    return ValueError(f"contract_builder_input_invalid:{path}")


def _json_copy(value: Any, path: str = "root") -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise _error(path)
        return 0.0 if value == 0 else value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise _error(path)
            result[key] = _json_copy(item, f"{path}.{key}")
        return result
    if isinstance(value, list):
        return [_json_copy(item, f"{path}[{index}]") for index, item in enumerate(value)]
    raise _error(path)


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _error(path)
    return value


def _timestamp(value: Any, path: str) -> int:
    if type(value) is not int or value < 0:
        raise _error(path)
    return value


def _number(value: Any, path: str, *, optional: bool = True) -> int | float | None:
    if value is None and optional:
        return None
    if type(value) not in (int, float) or not math.isfinite(value):
        raise _error(path)
    return 0.0 if value == 0 else value


def _status(value: Any, path: str) -> str:
    if value not in STATUSES:
        raise _error(path)
    return str(value)


def _reason(status: str, value: Any, path: str) -> str | None:
    if status == "available":
        return None
    if not isinstance(value, str) or not value:
        raise _error(path)
    return value


def _combined(statuses: Sequence[str]) -> str:
    return max(statuses, key={"available": 0, "partial": 1, "unavailable": 2, "invalid": 3}.__getitem__)


def _at(root: Mapping[str, Any], path: str) -> Any:
    value: Any = root
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            raise _error(path)
        value = value[part]
    return value


def _classification(classification: Mapping[str, Any], timeframe: str, name: str) -> Mapping[str, Any]:
    return _mapping(_at(classification, f"classifications.by_timeframe.{timeframe}.current.{name}"),
                    f"classification.{name}")


def _state(classification: Mapping[str, Any], timeframe: str, name: str) -> str | None:
    atom = _classification(classification, timeframe, name)
    _status(atom.get("status"), f"classification.{name}.status")
    state = atom.get("state")
    if state is not None and not isinstance(state, str):
        raise _error(f"classification.{name}.state")
    return state


def _validate_bundle(bundle: Any, selected_timeframe: Any) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    if not isinstance(selected_timeframe, str) or selected_timeframe not in HMI_TIMEFRAMES:
        raise ValueError("contract_builder_selected_timeframe_invalid")
    root = _mapping(bundle, "bundle")
    if set(root) != {"processing", "classification"} or len(root) != 2:
        raise _error("bundle.keys")
    processing = _mapping(root["processing"], "bundle.processing")
    classification = _mapping(root["classification"], "bundle.classification")
    for name, contract, stage in (("processing", processing, "processing"),
                                  ("classification", classification, "classification")):
        for field, expected in (("family", FAMILY), ("stage", stage), ("version", VERSION)):
            if contract.get(field) != expected:
                raise ValueError(f"contract_builder_bundle_mismatch:{name}.{field}")
    if processing.get("mode") != classification.get("mode"):
        raise ValueError("contract_builder_bundle_mismatch:mode")
    p_context = _mapping(processing.get("context"), "processing.context")
    c_context = _mapping(classification.get("context"), "classification.context")
    allowed_context_fields = set(CONTEXT_FIELDS) | set(OPTIONAL_CONTEXT_FIELDS)
    if (p_context != c_context or not set(CONTEXT_FIELDS).issubset(p_context)
            or not set(p_context).issubset(allowed_context_fields)):
        raise ValueError("contract_builder_bundle_mismatch:context")
    if "requested_at" in p_context:
        requested_at = p_context["requested_at"]
        if type(requested_at) is not str or not requested_at.strip():
            raise ValueError("contract_builder_bundle_mismatch:context")
    for field in ("include_snapshots", "include_confirmations", "refresh_secondary"):
        if field in p_context and type(p_context[field]) is not bool:
            raise ValueError("contract_builder_bundle_mismatch:context")
    reference = _timestamp(p_context.get("reference_timestamp"), "context.reference_timestamp")
    del reference
    if p_context.get("data_mode") not in {"live", "synthetic"} or type(p_context.get("is_demo")) is not bool:
        raise _error("context.data_mode")
    if (p_context["data_mode"] == "synthetic") is not p_context["is_demo"]:
        raise _error("context.is_demo")
    p_quality = _mapping(processing.get("quality"), "processing.quality")
    c_quality = _mapping(classification.get("quality"), "classification.quality")
    if c_quality.get("processing_quality") != p_quality:
        raise ValueError("contract_builder_bundle_mismatch:processing_quality")
    if classification.get("snapshots") != processing.get("snapshots"):
        raise ValueError("contract_builder_bundle_mismatch:snapshots")
    _json_copy(root, "bundle")
    return processing, classification


def _series_item(identifier: str, wrapper: Any, field_names: Sequence[str], unit: str,
                 representation: str, source_path: str) -> dict[str, Any]:
    source = _mapping(wrapper, source_path)
    status = _status(source.get("status"), f"{source_path}.status")
    reason = _reason(status, source.get("reason"), f"{source_path}.reason")
    if status == "invalid":
        return {"id": identifier, "status": status, "reason": reason, "unit": unit,
                "representation": representation, "points": [], "source_path": source_path}
    timestamps = source.get("timestamps")
    series = source.get("series")
    if not isinstance(timestamps, list) or not isinstance(series, Mapping):
        raise _error(source_path)
    arrays = []
    for field in field_names:
        values = series.get(field)
        if not isinstance(values, list) or len(values) != len(timestamps):
            raise _error(f"{source_path}.series.{field}")
        arrays.append(values)
    points = []
    for index, timestamp in enumerate(timestamps):
        point = {"timestamp": _timestamp(timestamp, f"{source_path}.timestamps[{index}]")}
        if len(field_names) == 1:
            point["value"] = _number(arrays[0][index], f"{source_path}.{field_names[0]}[{index}]")
        else:
            for field, values in zip(field_names, arrays, strict=True):
                point[field] = _number(values[index], f"{source_path}.{field}[{index}]")
        points.append(point)
    current = source.get("current")
    if status != "invalid" and points and not isinstance(current, Mapping):
        status, reason = "partial", "current_unavailable_history_available"
    return {"id": identifier, "status": status, "reason": reason, "unit": unit,
            "representation": representation, "points": points, "source_path": source_path}


def _ohlc_item(identifier: str, frame: Any, unit: str, source_path: str) -> dict[str, Any]:
    source = _mapping(frame, source_path)
    status = _status(source.get("status"), f"{source_path}.status")
    reason = _reason(status, source.get("reason"), f"{source_path}.reason")
    if status == "invalid":
        return {"id": identifier, "status": status, "reason": reason, "unit": unit,
                "representation": "ohlc", "points": [], "source_path": source_path}
    records = source.get("records")
    if not isinstance(records, list):
        raise _error(f"{source_path}.records")
    points = []
    for index, record in enumerate(records):
        row = _mapping(record, f"{source_path}.records[{index}]")
        points.append({"timestamp": _timestamp(row.get("timestamp"), f"{source_path}.records[{index}].timestamp"),
            **{name: _number(row.get(name), f"{source_path}.records[{index}].{name}")
               for name in ("open", "high", "low", "close")}})
    if points and not isinstance(source.get("current"), Mapping):
        status, reason = "partial", "current_unavailable_history_available"
    elif not points and not isinstance(source.get("current"), Mapping) and status == "available":
        raise _error(f"{source_path}.history_and_current")
    return {"id": identifier, "status": status, "reason": reason, "unit": unit,
            "representation": "ohlc", "points": points, "source_path": source_path}


def _axis(left: str | None, right: str | None = None) -> dict[str, Any]:
    return {"x": {"field": "timestamp", "unit": "unix_seconds"},
            "y": {"left": {"unit": left}, "right": {"unit": right}}}


def _chart(identifier: str, chart_type: str, series: list[dict[str, Any]], timeframe: str,
           classification: Any, source_paths: list[str], *, overlays: list[dict[str, Any]] | None = None,
           right_unit: str | None = None) -> dict[str, Any]:
    overlays = overlays or []
    statuses = [item["status"] for item in [*series, *overlays]]
    status = _combined(statuses) if statuses else "unavailable"
    reason = next((item["reason"] for item in [*series, *overlays] if item["status"] == status), None)
    if status in {"unavailable", "invalid"}:
        series, overlays, classification = [], [], None
    left = series[0]["unit"] if series else None
    return {"id": identifier, "status": status, "reason": reason,
        "label_key": f"screens.{FAMILY}.charts.{identifier}", "timeframe": timeframe,
        "chart_type": chart_type, "series": series, "axes": _axis(left, right_unit), "overlays": overlays,
        "classification": _json_copy(classification), "source_paths": source_paths}


def _placeholder_chart(identifier: str, reason: str, source_path: str, timeframe: str) -> dict[str, Any]:
    return {"id": identifier, "status": "unavailable", "reason": reason,
        "label_key": f"screens.{FAMILY}.charts.{identifier}", "timeframe": timeframe,
        "chart_type": "placeholder", "series": [], "axes": _axis(None), "overlays": [],
        "classification": None, "source_paths": [source_path]}


def _kpi(identifier: str, wrapper: Mapping[str, Any], value: Any, unit: str | None, timestamp: Any,
         timeframe: str, classification: Any, source_paths: list[str], *, secondary: Any = None,
         secondary_unit: str | None = None) -> dict[str, Any]:
    status = _status(wrapper.get("status"), f"kpi.{identifier}.status")
    reason = _reason(status, wrapper.get("reason"), f"kpi.{identifier}.reason")
    if status in {"unavailable", "invalid"}:
        value = secondary = classification = timestamp = None
    return {"id": identifier, "status": status, "reason": reason,
        "label_key": f"screens.{FAMILY}.kpis.{identifier}", "value": _number(value, f"kpi.{identifier}.value"),
        "secondary_value": _number(secondary, f"kpi.{identifier}.secondary"), "unit": unit,
        "secondary_unit": secondary_unit, "timestamp": timestamp, "timeframe": timeframe,
        "classification": _json_copy(classification), "source_paths": source_paths}


def _unavailable_kpi(identifier: str, reason: str, timeframe: str, source_path: str) -> dict[str, Any]:
    return {"id": identifier, "status": "unavailable", "reason": reason,
        "label_key": f"screens.{FAMILY}.kpis.{identifier}", "value": None, "secondary_value": None,
        "unit": None, "secondary_unit": None, "timestamp": None, "timeframe": timeframe,
        "classification": None, "source_paths": [source_path]}


def _build_kpis(processing: Mapping[str, Any], classification: Mapping[str, Any], timeframe: str) -> list[dict[str, Any]]:
    oi_path = f"series.open_interest_ohlc.timeframes.{timeframe}"
    funding_path = f"series.funding_rate_ohlc.timeframes.{timeframe}"
    oi = _mapping(_at(processing, oi_path), oi_path)
    funding = _mapping(_at(processing, funding_path), funding_path)
    change_path = f"{oi_path}.derived.oi_change_24h"
    change = _mapping(_at(processing, change_path), change_path)
    oi_current = oi.get("current") if isinstance(oi.get("current"), Mapping) else {}
    funding_current = funding.get("current") if isinstance(funding.get("current"), Mapping) else {}
    change_current = change.get("current") if isinstance(change.get("current"), Mapping) else {}
    oi_atom = _classification(classification, timeframe, "open_interest_change_state")
    funding_atom = _classification(classification, timeframe, "funding_state")
    return [
        _kpi("open_interest_usd", oi, oi_current.get("close"), "USD", oi_current.get("timestamp"), timeframe,
             oi_atom.get("state"), [f"{oi_path}.current.close"]),
        _kpi("oi_change_24h", change, change_current.get("change_absolute_usd"), "USD",
             change.get("current_timestamp"), timeframe, oi_atom.get("state"),
             [f"{change_path}.current.change_absolute_usd", f"{change_path}.current.change_percent"],
             secondary=change_current.get("change_percent"), secondary_unit="percent"),
        _kpi("funding_rate", funding, funding_current.get("close"), "percent_points",
             funding_current.get("timestamp"), timeframe, funding_atom.get("state"),
             [f"{funding_path}.current.close"]),
    ]


def _build_charts(processing: Mapping[str, Any], classification: Mapping[str, Any], timeframe: str) -> dict[str, Any]:
    oi_path = f"series.open_interest_ohlc.timeframes.{timeframe}"
    funding_path = f"series.funding_rate_ohlc.timeframes.{timeframe}"
    indicator_base = f"indicators.open_interest.timeframes.{timeframe}"
    oi = _at(processing, oi_path)
    funding = _at(processing, funding_path)
    derived_delta = _at(processing, f"{oi_path}.derived.oi_delta")
    def atom(name: str) -> str | None: return _state(classification, timeframe, name)
    oi_line = _ohlc_item("open_interest_ohlc_source", oi, "USD", oi_path)
    oi_line = {**oi_line, "id": "open_interest_close", "representation": "line",
               "points": [{"timestamp": p["timestamp"], "value": p["close"]} for p in oi_line["points"]]}
    funding_line = _ohlc_item("funding_ohlc_source", funding, "percent_points", funding_path)
    funding_line = {**funding_line, "id": "funding_close", "representation": "line",
                    "points": [{"timestamp": p["timestamp"], "value": p["close"]} for p in funding_line["points"]]}
    delta = _series_item("delta_absolute_usd", derived_delta, ("delta_absolute_usd",), "USD", "bar",
                         f"{oi_path}.derived.oi_delta")
    oi_candle = _ohlc_item("open_interest_ohlc", oi, "USD", oi_path)
    funding_candle = _ohlc_item("funding_ohlc", funding, "percent_points", funding_path)
    moving = _at(processing, f"{indicator_base}.moving_averages")
    smas = [_series_item(name, moving, (name,), "USD", "line", f"{indicator_base}.moving_averages")
            for name in ("sma_20", "sma_50")]
    bollinger = _at(processing, f"{indicator_base}.bollinger_bands")
    bands = [_series_item(name, bollinger, (name,), "USD", "line", f"{indicator_base}.bollinger_bands")
             for name in ("middle", "upper", "lower")]
    macd_source = _at(processing, f"{indicator_base}.macd")
    macd = [_series_item(name, macd_source, (name,), "USD", "bar" if name == "histogram" else "line",
                         f"{indicator_base}.macd") for name in ("macd", "signal", "histogram")]
    adx_source = _at(processing, f"{indicator_base}.adx")
    adx = _series_item("adx_di", adx_source, ("adx", "di_plus", "di_minus"), "index_0_100", "multi_value",
                       f"{indicator_base}.adx")
    stochastic_source = _at(processing, f"{indicator_base}.stochastic")
    stochastic = _series_item("stochastic", stochastic_source, ("k", "d"), "index_0_100", "multi_value",
                              f"{indicator_base}.stochastic")
    charts = {
        "open_interest_candlestick": _chart("open_interest_candlestick", "candlestick", [oi_candle], timeframe,
            atom("open_interest_change_state"), [oi_path, f"{indicator_base}.moving_averages"], overlays=smas),
        "open_interest_ohlc": _chart("open_interest_ohlc", "candlestick", [oi_candle], timeframe,
            atom("open_interest_change_state"), [oi_path]),
        "macd": _chart("macd", "oscillator", macd, timeframe, atom("macd_relation"), [f"{indicator_base}.macd"]),
        "adx": _chart("adx", "oscillator", [adx], timeframe,
            {"oi_trend_strength": atom("oi_trend_strength"), "directional_index_relation": atom("directional_index_relation")}, [f"{indicator_base}.adx"]),
        "stochastic": _chart("stochastic", "oscillator", [stochastic], timeframe, atom("stochastic_range_state"), [f"{indicator_base}.stochastic"]),
        "funding_rate": _chart("funding_rate", "line", [funding_line], timeframe, atom("funding_state"), [funding_path]),
    }
    for identifier, source_name, unit, classification_name, chart_type in (
        ("rsi", "rsi", "index_0_100", None, "oscillator"),
        ("tsi", "tsi", "index_-100_100", None, "oscillator"),
        ("williams_r", "williams_r", "index_-100_0", None, "oscillator"),
        ("atr", "atr", "USD", None, "line"), ("cci", "cci", "index", "cci_state", "oscillator"),
        ("wasserstein_distance", "wasserstein_distance", "ratio", None, "line"),
        ("bollinger_band_width", "bollinger_band_width", "ratio", None, "line"),
        ("oi_roc", "oi_roc", "percent", "oi_roc_state", "oscillator"),
    ):
        wrapper = _at(processing, f"{indicator_base}.{source_name}")
        field = {"oi_roc": "roc", "wasserstein_distance": "distance", "bollinger_band_width": "bandwidth"}.get(identifier, identifier)
        item = _series_item(field, wrapper, (field,), unit, "line", f"{indicator_base}.{source_name}")
        charts[identifier] = _chart(identifier, chart_type, [item], timeframe,
                                    atom(classification_name) if classification_name else None,
                                    [f"{indicator_base}.{source_name}"])
    return {name: charts[name] for name in CHART_IDS}


def _table(processing: Mapping[str, Any], classification: Mapping[str, Any], timeframe: str) -> dict[str, Any]:
    base = f"indicators.open_interest.timeframes.{timeframe}"
    specs = (
        ("macd", "macd", "macd", "USD", "macd_relation"),
        ("rsi", "rsi", "rsi", "index_0_100", None), ("tsi", "tsi", "tsi", "index", None),
        ("adx", "adx", "adx", "index_0_100", "oi_trend_strength"),
        ("di_plus", "adx", "di_plus", "index_0_100", "directional_index_relation"),
        ("di_minus", "adx", "di_minus", "index_0_100", "directional_index_relation"),
        ("stochastic_k", "stochastic", "k", "index_0_100", "stochastic_range_state"),
        ("stochastic_d", "stochastic", "d", "index_0_100", "stochastic_range_state"),
        ("williams_r", "williams_r", "williams_r", "index_-100_0", None),
        ("atr", "atr", "atr", "USD", None), ("cci", "cci", "cci", "index", "cci_state"),
        ("wasserstein_distance", "wasserstein_distance", "distance", "return_decimal", None),
        ("bollinger_band_width", "bollinger_band_width", "bandwidth", "decimal", None),
        ("oi_roc", "oi_roc", "roc", "percent", "oi_roc_state"),
    )
    rows = []
    for identifier, package, field, unit, classification_name in specs:
        path = f"{base}.{package}"
        wrapper = _mapping(_at(processing, path), path)
        status = _status(wrapper.get("status"), f"{path}.status")
        reason = _reason(status, wrapper.get("reason"), f"{path}.reason")
        current = wrapper.get("current") if isinstance(wrapper.get("current"), Mapping) else {}
        value = current.get(field) if status not in {"unavailable", "invalid"} else None
        secondary = ({"signal": current.get("signal"), "histogram": current.get("histogram")} if identifier == "macd" else {})
        rows.append({"id": identifier, "status": status, "reason": reason, "value": _number(value, f"table.{identifier}"),
            "secondary_values": _json_copy(secondary), "unit": unit,
            "timestamp": wrapper.get("current_timestamp") if status not in {"unavailable", "invalid"} else None,
            "timeframe": timeframe, "classification_state": _state(classification, timeframe, classification_name) if classification_name and status not in {"unavailable", "invalid"} else None,
            "source_path": f"{path}.current.{field}"})
    status = _combined([row["status"] for row in rows])
    reason = next((row["reason"] for row in rows if row["status"] == status), None)
    if status == "invalid":
        rows = []
    return {"id": "indicators_metrics", "table_id": "open_interest_indicators_metrics", "status": status, "reason": reason,
        "label_key": f"screens.{FAMILY}.tables.indicators_metrics", "timeframe": timeframe,
        "columns": ["indicator", "value", "secondary_values", "unit", "classification_state", "status", "reason", "timestamp"],
        "rows": rows, "source_paths": [base, "availability.unavailable.mfi"]}


def _widgets(classification: Mapping[str, Any], timeframe: str) -> dict[str, Any]:
    names = ("open_interest_change_state", "funding_state", "oi_funding_quadrant")
    atoms = {name: _classification(classification, timeframe, name) for name in names}
    status = _combined([_status(atom.get("status"), f"widget.{name}.status") for name, atom in atoms.items()])
    reason = next((_reason(status, atom.get("reason"), f"widget.{name}.reason") for name, atom in atoms.items()
                   if atom.get("status") == status), None)
    usable = status not in {"invalid", "unavailable"}
    oi_widget = {"id": "oi_funding_state", "status": status, "reason": reason,
        "label_key": f"screens.{FAMILY}.widgets.oi_funding_state", "timeframe": timeframe,
        "timestamp": atoms["oi_funding_quadrant"].get("evidence", {}).get("timestamp") if usable else None,
        "open_interest_change_state": atoms["open_interest_change_state"].get("state") if usable else None,
        "funding_state": atoms["funding_state"].get("state") if usable else None,
        "quadrant_state": atoms["oi_funding_quadrant"].get("state") if usable else None,
        "source_paths": [f"classifications.by_timeframe.{timeframe}.current.{name}" for name in names]}
    quality = _mapping(classification.get("quality"), "classification.quality")
    processing_quality = _mapping(quality.get("processing_quality"), "classification.quality.processing_quality")
    source_statuses = _mapping(processing_quality.get("source_statuses"), "classification.quality.processing_quality.source_statuses")
    confirmations = _mapping(classification.get("confirmations"), "classification.confirmations")
    leverage = _mapping(_mapping(confirmations.get("estimated_leverage_ratio"), "confirmations.estimated_leverage_ratio").get("glassnode"),
                        "confirmations.estimated_leverage_ratio.glassnode")

    def primary_row(metric: str, endpoint_id: str, unit: str) -> dict[str, Any]:
        statuses = [_status(source_statuses.get(f"{metric}.{tf}"), f"quality.source_statuses.{metric}.{tf}") for tf in HMI_TIMEFRAMES]
        row_status = _combined(statuses)
        return {"metric": metric, "provider": "coinglass", "status": row_status,
            "reason": None if row_status == "available" else "primary_series_not_fully_available",
            "provider_state": "primary", "endpoint_id": endpoint_id, "unit": unit,
            "window_or_interval": list(HMI_TIMEFRAMES), "source_path": f"quality.processing_quality.source_statuses.{metric}.*"}

    leverage_status = _status(leverage.get("status"), "confirmations.estimated_leverage_ratio.glassnode.status")
    rows = [
        primary_row("open_interest_ohlc", "aggregated_open_interest_ohlc", "USD"),
        primary_row("funding_rate_ohlc", "oi_weighted_funding_rate_ohlc", "percentage_points"),
        {"metric": "estimated_leverage_ratio", "provider": "glassnode", "status": leverage_status,
         "reason": leverage.get("reason"), "provider_state": leverage.get("provider_state", "confirmation"),
         "endpoint_id": leverage.get("endpoint_id", "futures_estimated_leverage_ratio"), "unit": leverage.get("unit", "ratio"),
         "window_or_interval": leverage.get("provider_interval", "1h"),
         "source_path": "confirmations.estimated_leverage_ratio.glassnode"},
    ]
    provider_status = "invalid" if any(row["status"] == "invalid" for row in rows) else \
        "partial" if any(row["status"] in {"partial", "unavailable"} for row in rows) else "available"
    provider_reason = next((row["reason"] for row in rows if row["status"] != "available"), None)
    provider_widget = {"id": "provider_availability", "status": provider_status, "reason": provider_reason,
        "label_key": f"screens.{FAMILY}.widgets.provider_availability", "rows": [] if provider_status == "invalid" else rows,
        "comparisons": {"status": "unavailable", "reason": "provider_comparisons_not_contracted_final33", "provider_state": "not_contracted"},
        "source_paths": ["quality.processing_quality.source_statuses", "confirmations.estimated_leverage_ratio.glassnode"]}
    return {"oi_funding_state": oi_widget, "provider_availability": provider_widget}


def _drilldown(identifier: str, snapshot: Any, columns: list[str], metadata_fields: list[str]) -> dict[str, Any]:
    source_path = f"snapshots.{identifier}"
    source = _mapping(snapshot, source_path)
    status = _status(source.get("status"), f"{source_path}.status")
    reason = _reason(status, source.get("reason"), f"{source_path}.reason")
    records = source.get("records")
    if not isinstance(records, list):
        raise _error(f"{source_path}.records")
    clean_records = [{field: _json_copy(_mapping(row, source_path).get(field), f"{source_path}.{field}") for field in columns}
                     for row in records]
    aggregate = source.get("aggregate_record")
    clean_aggregate = ({field: _json_copy(_mapping(aggregate, source_path).get(field), f"{source_path}.aggregate.{field}") for field in columns}
                       if isinstance(aggregate, Mapping) else None)
    metadata = {field: _json_copy(source.get(field), f"{source_path}.{field}") for field in metadata_fields}
    if identifier == "funding_rate_by_exchange":
        for field in ("stablecoin_margin_records", "token_margin_records"):
            nested = metadata[field]
            if not isinstance(nested, list):
                raise _error(f"{source_path}.{field}")
            metadata[field] = [
                {column: _json_copy(_mapping(row, f"{source_path}.{field}[{index}]").get(column),
                                    f"{source_path}.{field}[{index}].{column}") for column in columns}
                for index, row in enumerate(nested)
            ]
    if status in {"unavailable", "invalid"}:
        clean_records, clean_aggregate = [], None
    return {"id": identifier, "status": status, "reason": reason,
        "label_key": f"screens.{FAMILY}.drilldowns.{identifier}", "columns": columns, "records": clean_records,
        "aggregate_record": clean_aggregate, "metadata": metadata, "source_path": source_path}


def _drilldowns(classification: Mapping[str, Any]) -> dict[str, Any]:
    snapshots = _mapping(classification.get("snapshots"), "classification.snapshots")
    return {
        "open_interest_by_exchange": _drilldown("open_interest_by_exchange", snapshots.get("open_interest_by_exchange"),
            ["exchange", "open_interest_usd", "open_interest_change_percent_24h"],
            ["invalid_records", "exchange_count", "current_total_usd", "reported_changes"]),
        "funding_rate_by_exchange": _drilldown("funding_rate_by_exchange", snapshots.get("funding_rate_by_exchange"),
            ["exchange", "margin_type", "funding_rate_percent", "next_funding_timestamp"],
            ["invalid_records", "stablecoin_margin_records", "token_margin_records", "exchange_count", "next_funding_timestamps"]),
        "options_open_interest": _drilldown("options_open_interest", snapshots.get("options_open_interest"),
            ["exchange", "open_interest_usd", "open_interest_contracts"],
            ["invalid_records", "current_options_open_interest_usd", "current_options_contracts"]),
    }


def _events(classification: Mapping[str, Any], timeframe: str) -> dict[str, Any]:
    raw_source = classification.get("interpreted_events")
    if not isinstance(raw_source, Mapping):
        return _invalid_events(timeframe, "contract_builder_input_invalid:interpreted_events")
    source = raw_source
    by_id = source.get("by_id")
    if not isinstance(by_id, Mapping):
        upstream_reason = source.get("reason")
        reason = upstream_reason if isinstance(upstream_reason, str) and upstream_reason else \
            "contract_builder_input_invalid:interpreted_events"
        return _invalid_events(timeframe, reason)
    candidates = []
    for identifier, payload in by_id.items():
        event = _mapping(payload, f"interpreted_events.{identifier}")
        if event.get("timeframe") != timeframe:
            continue
        evidence = _mapping(event.get("evidence"), f"interpreted_events.{identifier}.evidence")
        source_event = _mapping(evidence.get("source_event"), f"interpreted_events.{identifier}.source_event")
        item_status = _status(event.get("status"), f"interpreted_events.{identifier}.status")
        candidates.append({"interpreted_event_id": event.get("interpretation_id"), "event_id": event.get("event_id"),
            "status": item_status, "reason": event.get("reason"), "state": event.get("state"),
            "event_type": event.get("event_type"), "timestamp": _timestamp(event.get("timestamp"), f"event.{identifier}.timestamp"),
            "timeframe": timeframe, "values": _json_copy(source_event.get("values", {})),
            "parameters": _json_copy(source_event.get("parameters", {})),
            "source_path": f"interpreted_events.by_id.{identifier}"})
    candidates.sort(key=lambda item: (-item["timestamp"], item["event_type"], item["event_id"]))
    total = sum(item["status"] != "invalid" for item in candidates)
    items = candidates[:50]
    markers = [{name: item[name] for name in ("event_id", "timestamp", "state", "event_type")}
               for item in items if item["status"] != "invalid"]
    status = "partial" if any(item["status"] == "invalid" for item in items) else "available"
    return {"id": "recent_events", "status": status,
        "reason": "classification_event_invalid" if status == "partial" else None, "timeframe": timeframe,
        "order": "timestamp_desc_event_type_asc_event_id_asc", "limit": 50, "total_available": total,
        "items": items, "event_markers": markers, "source_path": "interpreted_events"}


def _invalid_events(timeframe: str, reason: str) -> dict[str, Any]:
    return {"id": "recent_events", "status": "invalid", "reason": reason, "timeframe": timeframe,
        "order": "timestamp_desc_event_type_asc_event_id_asc", "limit": 50, "total_available": 0,
        "items": [], "event_markers": [], "source_path": "interpreted_events"}


def _entry(status: str, reason: Any, paths: list[str]) -> dict[str, Any]:
    if status == "available":
        reason = None
    elif not isinstance(reason, str) or not reason:
        raise _error("availability.reason")
    return {"status": status, "reason": reason, "source_paths": paths}


def _aggregate_status_reason(items: Sequence[Mapping[str, Any]]) -> tuple[str, str | None]:
    status = _combined([str(item["status"]) for item in items])
    if status == "available":
        return status, None
    reason = next((item.get("reason") for item in items
                   if item.get("status") == status and isinstance(item.get("reason"), str) and item.get("reason")), None)
    if reason is None:
        raise _error("availability.passthrough.reason")
    return status, reason


def _availability(kpis: list[dict[str, Any]], charts: Mapping[str, Any], table: Mapping[str, Any],
                  widgets: Mapping[str, Any], drilldowns: Mapping[str, Any], events: Mapping[str, Any]) -> dict[str, Any]:
    kpi_map = {item["id"]: item for item in kpis}
    objects = {"timeframe_selector": {"status": "available", "reason": None, "source_paths": []},
        **{f"kpis.{key}": value for key, value in kpi_map.items()}, **{f"charts.{key}": value for key, value in charts.items()},
        "tables.oi_technical_indicators": table, **{f"widgets.{key}": value for key, value in widgets.items()},
        **{f"drilldowns.{key}": value for key, value in drilldowns.items()}, "events.recent_events": events}
    def category(names: Sequence[str]) -> dict[str, Any]:
        return {name: _entry(objects[name]["status"], objects[name]["reason"],
                             list(objects[name].get("source_paths", []))) for name in names}
    snapshot_status, snapshot_reason = _aggregate_status_reason(list(drilldowns.values()))
    placeholders = {
        "funding_8h": kpi_map["funding_8h"],
        "mfi": charts["mfi"], "oi_vs_price": charts["oi_vs_price"],
        "contract_type_split": charts["contract_type_split"],
        "provider_comparisons": {"status": "unavailable", "reason": "provider_scope_not_proven_comparable",
                                 "source_paths": ["availability.unavailable.provider_comparisons"]},
    }
    return {"required": category(REQUIRED_IDS), "optional": category(OPTIONAL_IDS),
        "passthrough": {"snapshots": _entry(snapshot_status, snapshot_reason, ["snapshots"]),
                        "confirmations": _entry(widgets["provider_availability"]["status"], widgets["provider_availability"]["reason"], ["confirmations"]),
                        "events": _entry(events["status"], events["reason"], ["interpreted_events"])},
        "placeholders": {name: _entry(item["status"], item["reason"], list(item["source_paths"])) for name, item in placeholders.items()}}


def _quality(processing: Mapping[str, Any], classification: Mapping[str, Any], availability: Mapping[str, Any]) -> dict[str, Any]:
    required = {name: item["status"] for name, item in availability["required"].items()}
    optional = {name: item["status"] for name, item in availability["optional"].items()}
    placeholders = {name: item["status"] for name, item in availability["placeholders"].items()}
    warnings = [f"required_{status}:{name}" for name, status in required.items() if status in {"partial", "unavailable"}]
    warnings += [f"optional_{status}:{name}" for name, status in optional.items() if status != "available"]
    errors = [f"required_invalid:{name}" for name, status in required.items() if status == "invalid"]
    classification_status = classification["quality"].get("status")
    if classification_status == "invalid":
        errors.append("source_classification_invalid")
    status = "invalid" if errors else "partial" if warnings else "ok"
    builder_status = "invalid" if errors else "partial" if warnings else "ok"
    return {"status": status, "contract_complete": True,
        "data_complete": all(value == "available" for value in required.values()),
        "source_quality": {"processing": _json_copy(processing["quality"]), "classification": _json_copy(classification["quality"])},
        "builder_quality": {"status": builder_status, "warnings": sorted(set(warnings)), "errors": sorted(set(errors))},
        "required_statuses": required, "optional_statuses": optional, "placeholder_statuses": placeholders,
        "warnings": sorted(set(warnings)), "errors": sorted(set(errors))}


def _validate_source_paths(value: Any, path: str = "root") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key == "source_path":
                paths = [item]
            elif key == "source_paths":
                paths = item
            else:
                _validate_source_paths(item, f"{path}.{key}")
                continue
            if not isinstance(paths, list) or any(not isinstance(p, str) or not p.startswith(SOURCE_PREFIXES) for p in paths):
                if paths != []:
                    raise _error(f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_source_paths(item, f"{path}[{index}]")


def _title(identifier: str) -> str:
    return identifier.replace("_", " ").upper()


def _visual_kpis(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result = {}
    for item in items:
        identifier = str(item["id"])
        result[identifier] = {
            "kpi_id": identifier, "title": _title(identifier), "status": item["status"],
            "reason": item["reason"], "value": item["value"], "secondary_value": item["secondary_value"],
            "unit": item["unit"], "secondary_unit": item["secondary_unit"], "timestamp": item["timestamp"],
            "timeframe": item["timeframe"], "classification": deepcopy(item["classification"]),
            "source_paths": deepcopy(item["source_paths"]),
        }
    return result


def _chart_current(chart: Mapping[str, Any]) -> dict[str, Any]:
    values, timestamps = {}, []
    for series in chart["series"]:
        points = series.get("points", [])
        if points:
            point = points[-1]
            timestamp = point.get("timestamp")
            if type(timestamp) is int:
                timestamps.append(timestamp)
            values[series["id"]] = {key: deepcopy(value) for key, value in point.items() if key != "timestamp"}
    timestamp = max(timestamps) if timestamps else None
    return {"status": chart["status"], "timestamp": timestamp, "values": values,
            "reason": chart["reason"]}


def _visual_charts(processing: Mapping[str, Any], classification: Mapping[str, Any],
                   selected_timeframe: str) -> dict[str, Any]:
    by_timeframe = {timeframe: _build_charts(processing, classification, timeframe) for timeframe in HMI_TIMEFRAMES}
    result = {}
    for identifier in CHART_IDS:
        selected = by_timeframe[selected_timeframe][identifier]
        ranges = {}
        for timeframe in HMI_TIMEFRAMES:
            chart = by_timeframe[timeframe][identifier]
            ranges[timeframe] = {
                "timeframe": timeframe, "seconds": TIMEFRAME_SECONDS[timeframe], "status": chart["status"],
                "reason": chart["reason"], "series": deepcopy(chart["series"]),
                "overlays": deepcopy(chart["overlays"]), "classification": deepcopy(chart["classification"]),
            }
        result[identifier] = {
            "chart_id": identifier, "title": _title(identifier),
            "subtitle": f"{_title(identifier)} BY TIMEFRAME", "chart_type": selected["chart_type"],
            "unit": selected["axes"]["y"]["left"]["unit"], "provider": "Coinglass",
            "status": selected["status"], "reason": selected["reason"], "selected_timeframe": selected_timeframe,
            "current": _chart_current(selected), "series_by_timeframe": ranges,
            "axes": deepcopy(selected["axes"]), "source_paths": deepcopy(selected["source_paths"]),
            "selected_market": "all_exchanges", "available_markets": ["all_exchanges"],
            "available_timeframes": list(HMI_TIMEFRAMES),
            "markets": {"all_exchanges": {timeframe: deepcopy(ranges[timeframe]) for timeframe in HMI_TIMEFRAMES}},
        }
    return result


def build_open_interest_and_funding_contract(bundle: Mapping[str, Any], *, selected_timeframe: str = "15m") -> dict[str, Any]:
    """Build the complete visual screen contract from the canonical vertical bundle."""
    before = deepcopy(bundle)
    processing, classification = _validate_bundle(bundle, selected_timeframe)
    context = _json_copy({field: processing["context"][field] for field in CONTEXT_FIELDS}, "context")
    kpis = _build_kpis(processing, classification, selected_timeframe)
    selected_charts = _build_charts(processing, classification, selected_timeframe)
    table = _table(processing, classification, selected_timeframe)
    widgets = _widgets(classification, selected_timeframe)
    drilldowns = _drilldowns(classification)
    events = _events(classification, selected_timeframe)
    visual_charts = _visual_charts(processing, classification, selected_timeframe)
    visible_events = {item["event_id"]: {
        "event_uid": item["event_id"], "timestamp": item["timestamp"], "event_id": item["event_id"],
        "event_type": item["event_type"], "event_group": "technical_cross", "signal": item["state"],
        "state": item["state"], "source": {"market": "all_exchanges", "timeframe": item["timeframe"]},
        "indicator_context": deepcopy(item["values"]), "calculation": deepcopy(item["parameters"]),
    } for item in events["items"] if item["status"] != "invalid"}
    counts = [len(processing["series"]["open_interest_ohlc"]["timeframes"][tf]["records"])
              for tf in HMI_TIMEFRAMES]
    calculation_records = min(counts, default=0)
    output_context = {**context, "data_as_of": context["reference_timestamp"],
                      "presentation_default_timeframe": selected_timeframe}
    visual_kpis = _visual_kpis(kpis)
    state_widget = widgets["oi_funding_state"]
    provider_widget = widgets["provider_availability"]
    kpi_items = [{"metric_id": item["kpi_id"], **{k: deepcopy(v) for k, v in item.items() if k != "kpi_id"}}
                 for item in visual_kpis.values()]
    kpi_items.insert(2, {"metric_id": "oi_funding_state", "value": None, "unit": "state",
        "status": state_widget["status"], "display_value": " / ".join(filter(None, (
            state_widget["open_interest_change_state"], state_widget["funding_state"]))),
        "secondary_display_value": state_widget["quadrant_state"]})
    available_providers = sum(row["status"] == "available" for row in provider_widget["rows"])
    kpi_items.insert(3, {"metric_id": "provider_availability", "value": None, "unit": "state",
        "status": provider_widget["status"], "display_value": f"{available_providers}/{len(provider_widget['rows'])}",
        "secondary_display_value": provider_widget["status"]})
    output = {
        "family": FAMILY, "screen": FAMILY, "schema_version": SCREEN_VERSION, "context": {
            **output_context, "default_market": "all_exchanges", "available_markets": ["all_exchanges"],
            "default_timeframe": "15m", "available_timeframes": list(HMI_TIMEFRAMES),
            "units": {"open_interest": "USD", "funding_rate": "percent_points"},
            "history_policy": {"calculation": "upstream_full_history", "presentation": "selected_timeframe", "default_display_window": calculation_records},
        },
        "badges": [],
        "selectors": {
            "market": {"selector_id": "open_interest_market", "selected": "all_exchanges", "options": ["all_exchanges"], "status": "fixed", "visible": False},
            "timeframe": {"selector_id": "open_interest_timeframe", "selected": selected_timeframe, "options": list(HMI_TIMEFRAMES)},
        },
        "kpis": {"selected_market": "all_exchanges", "selected_timeframe": selected_timeframe, "items": kpi_items},
        "widgets": {
            "oi_funding_state": {"widget_id": "oi_funding_state", "status": state_widget["status"], "payload": state_widget},
            "provider_availability": {"widget_id": "provider_availability", "status": provider_widget["status"], "rows": provider_widget["rows"]},
            "funding_rate": {"widget_id": "funding_rate", "status": visual_kpis["funding_rate"]["status"],
                "value": visual_kpis["funding_rate"]["value"], "unit": visual_kpis["funding_rate"]["unit"],
                "classification": visual_kpis["funding_rate"]["classification"], "timestamp": visual_kpis["funding_rate"]["timestamp"]},
        }, "charts": visual_charts,
        "tables": {"indicators_metrics": {**table, "indicator_package": {
            "selected_market": "all_exchanges", "selected_timeframe": selected_timeframe,
            "markets": {"all_exchanges": {tf: _table(processing, classification, tf)["rows"] for tf in HMI_TIMEFRAMES}},
        }}},
        "events": {"by_id": visible_events, "technical_cross_ids": list(visible_events),
                   "indexes": {"by_timeframe": {tf: [event_id for event_id, event in visible_events.items()
                                                        if event["source"]["timeframe"] == tf] for tf in HMI_TIMEFRAMES}}},
        "technical_analysis": {"analysis_id": "open_interest_technical_analysis", "calculation_stage": "processing",
            "classification_stage": "classification", "markets": {"all_exchanges": {"timeframes": {
                tf: deepcopy(processing["indicators"]["open_interest"]["timeframes"][tf]) for tf in HMI_TIMEFRAMES}}}},
        "history_contract": {"calculation_records": calculation_records, "minimum_warmup_records": 200,
            "maximum_standard_indicator_period": 200, "all_visible_moving_averages_warm": calculation_records >= 200,
            "technical_indicators_precomputed": True, "hmi_recalculation": False, "synthetic_fixture": False},
        "quality": {"status": classification["quality"]["status"], "contract_complete": True,
                    "data_complete": classification["quality"]["data_complete"], "warnings": [], "errors": []},
    }
    output = align_open_interest_and_funding_to_sp_v1_12(
        output, processing, classification, selected_timeframe=selected_timeframe,
    )
    output = _json_copy(output, "screen_contract")
    json.dumps(output, ensure_ascii=False, allow_nan=False, sort_keys=False)
    if bundle != before:
        raise RuntimeError("Contract Builder mutated its input bundle")
    return output


class OpenInterestAndFundingContractBuilder:
    """Object facade for the pure screen-contract builder."""

    def build(self, bundle: Mapping[str, Any], *, selected_timeframe: str = "15m") -> dict[str, Any]:
        return build_open_interest_and_funding_contract(bundle, selected_timeframe=selected_timeframe)

# --- Canonical Screen contract shaping ---
from copy import deepcopy

from datetime import datetime, timezone

import json

import math

from pathlib import Path

from typing import Any, Mapping

SP_SCHEMA_VERSION = '1.12.0-oi-native-screen-b-demo'

_screen_HMI_TIMEFRAMES = ('5m', '15m', '4h')

_screen_DISPLAY_WINDOW = 120

_screen_TEMPLATE_PATH = Path(__file__).with_name('screen_template.json')

_screen_MISSING = object()

_screen_NATIVE_CHARTS = ('oi_dynamics', 'oi_zscore_percentile', 'price_oi_regime', 'price_oi_divergence', 'funding_oi_crowding', 'wasserstein_distance')

_screen_PRIMARY_SERIES = {'oi_dynamics': 'oi_roc', 'oi_zscore_percentile': 'oi_zscore', 'price_oi_regime': 'regime_score', 'price_oi_divergence': 'divergence_score', 'funding_oi_crowding': 'crowding_score', 'wasserstein_distance': 'wasserstein_distance'}

def _screen_template() -> dict[str, Any]:
    return json.loads(_screen_TEMPLATE_PATH.read_text(encoding='utf-8'))

def _screen_finite(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (not math.isfinite(value)):
        return None
    return 0.0 if value == 0 else value

def _screen_is_scalar(value: Any) -> bool:
    return not isinstance(value, (dict, list))

def _screen_project(reference: Any, candidate: Any=_screen_MISSING) -> Any:
    """Project runtime values onto the exact current Screens contract shape."""
    if isinstance(reference, dict):
        source = candidate if isinstance(candidate, Mapping) else {}
        return {key: _screen_project(value, source.get(key, _screen_MISSING)) for key, value in reference.items()}
    if isinstance(reference, list):
        if candidate is _screen_MISSING:
            return deepcopy(reference)
        if not isinstance(candidate, list):
            return deepcopy(reference)
        if not reference or all((_screen_is_scalar(item) for item in reference)):
            return deepcopy(candidate)
        if not candidate:
            return []
        identity_keys = ('metric_id', 'kpi_id', 'widget_id', 'chart_id', 'table_id', 'badge_id', 'id', 'role', 'event_group', 'indicator_id', 'market', 'timeframe')

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
        return [_screen_project(ref_for(item, index), item) for index, item in enumerate(candidate)]
    return deepcopy(reference if candidate is _screen_MISSING else candidate)

def _screen_iso(timestamp: int | None) -> str | None:
    if type(timestamp) is not int:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace('+00:00', 'Z')

def _screen_compact(value: int | float | None) -> str:
    if value is None:
        return '—'
    absolute = abs(float(value))
    for divisor, suffix in ((1000000000000.0, 'T'), (1000000000.0, 'B'), (1000000.0, 'M'), (1000.0, 'K')):
        if absolute >= divisor:
            return f'{value / divisor:.2f}{suffix}'
    return f'{value:.2f}'

def _screen_context(reference: Mapping[str, Any], processing: Mapping[str, Any]) -> dict[str, Any]:
    out = deepcopy(dict(reference))
    pctx = processing['context']
    out.update({'default_market': 'all_exchanges', 'available_markets': ['all_exchanges'], 'default_timeframe': '15m', 'available_timeframes': list(_screen_HMI_TIMEFRAMES), 'asset': pctx.get('asset', 'BTC'), 'exchange_scope': pctx.get('exchange_scope', 'all_exchanges'), 'primary_provider': pctx.get('primary_provider', 'coinglass'), 'confirmation_providers': deepcopy(pctx.get('confirmation_providers', ['glassnode'])), 'data_mode': pctx.get('data_mode'), 'is_demo': pctx.get('is_demo'), 'reference_timestamp': pctx.get('reference_timestamp'), 'generated_at': pctx.get('generated_at'), 'data_as_of': pctx.get('reference_timestamp'), 'analysis_profile': 'ohlc_without_volume', 'volume_enabled': False, 'screen_revision': 'oi_native_screen_b_v1'})
    is_demo = bool(pctx.get('is_demo'))
    out['fixture_as_of_timestamp'] = None
    out['fixture_as_of_iso'] = None
    out['synthetic_fixture'] = False
    out['realism_refactor_version'] = 'runtime_emulator_v1' if is_demo else 'runtime_provider_v1'
    out['realism_note'] = 'deterministic provider-shaped emulator data; CoinGlass OI/Funding plus Glassnode ELR' if is_demo else 'runtime provider data; CoinGlass OI/Funding plus Glassnode ELR'
    return out

def _screen_elr_payload(processing: Mapping[str, Any]) -> Mapping[str, Any]:
    return processing.get('confirmations', {}).get('estimated_leverage_ratio', {}).get('glassnode', {})

def _screen_latest_confirmation(payload: Mapping[str, Any]) -> tuple[int | None, float | None]:
    records = payload.get('records') if isinstance(payload, Mapping) else None
    if not isinstance(records, list) or not records:
        records = payload.get('incoming_records') if isinstance(payload, Mapping) else None
    if not isinstance(records, list) or not records:
        return (None, None)
    record = records[-1] if isinstance(records[-1], Mapping) else {}
    return (record.get('timestamp') if type(record.get('timestamp')) is int else None, _screen_finite(record.get('value')))

def _screen_kpis(reference: Mapping[str, Any], candidate: Mapping[str, Any], processing: Mapping[str, Any], selected_timeframe: str) -> dict[str, Any]:
    candidate_items = {str(item.get('metric_id')): item for item in candidate.get('kpis', {}).get('items', []) if isinstance(item, Mapping)}
    dynamic_items: list[dict[str, Any]] = []
    for ref_item in reference.get('items', []):
        if not isinstance(ref_item, Mapping):
            continue
        metric_id = str(ref_item.get('metric_id'))
        if metric_id == 'estimated_leverage_ratio':
            payload = _screen_elr_payload(processing)
            ts, value = _screen_latest_confirmation(payload)
            status = str(payload.get('status', 'unavailable')) if isinstance(payload, Mapping) else 'unavailable'
            item = deepcopy(dict(ref_item))
            item.update({'value': value, 'display_value': f'{value:.3f}' if value is not None else '—', 'status': status, 'reason': payload.get('reason') if status not in {'available', 'ok'} else None})
            provenance = item.get('provenance')
            if isinstance(provenance, dict):
                provenance['integration_state'] = 'processing_confirmation_available' if value is not None else 'processing_confirmation_unavailable'
            if isinstance(item.get('quality'), dict):
                item['quality']['data_mode'] = processing['context'].get('data_mode')
                item['quality']['contract_ready'] = value is not None
            dynamic_items.append(item)
            continue
        item = _screen_project(ref_item, candidate_items.get(metric_id, {}))
        if metric_id == 'open_interest_usd':
            item['display_value'] = _screen_compact(_screen_finite(item.get('value')))
        elif metric_id == 'funding_rate':
            value = _screen_finite(item.get('value'))
            item['display_value'] = f'{value:.4f}%' if value is not None else '—'
        dynamic_items.append(item)
    result = deepcopy(dict(reference))
    result['selected_market'] = 'all_exchanges'
    result['selected_timeframe'] = selected_timeframe
    result['items'] = dynamic_items
    return result

def _screen_records(frame: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [{'timestamp': row.get('timestamp'), 'open': row.get('open'), 'high': row.get('high'), 'low': row.get('low'), 'close': row.get('close'), 'market_type': 'all_exchanges'} for row in frame.get('records', []) if isinstance(row, Mapping)]

def _screen_slice_series(series: Mapping[str, Any], count: int) -> dict[str, list[Any]]:
    return {str(key): deepcopy(value[-count:] if isinstance(value, list) else []) for key, value in series.items()}

def _screen_candle_tf(processing: Mapping[str, Any], timeframe: str, reference: Mapping[str, Any]) -> dict[str, Any]:
    frame = processing['series']['open_interest_ohlc']['timeframes'][timeframe]
    full_records = _screen_records(frame)
    visible = full_records[-_screen_DISPLAY_WINDOW:]
    indicators = processing['indicators']['open_interest']['timeframes'][timeframe]
    count = len(visible)
    overlays = {'moving_averages': {'status': indicators['moving_averages'].get('status'), 'series': _screen_slice_series(indicators['moving_averages'].get('series', {}), count)}, 'bollinger_bands': {'status': indicators['bollinger_bands'].get('status'), 'series': _screen_slice_series(indicators['bollinger_bands'].get('series', {}), count)}, 'regression_channel': {'status': indicators['regression_channel'].get('status'), 'series': _screen_slice_series(indicators['regression_channel'].get('series', {}), count)}}
    dynamic = {'status': frame.get('status'), 'records': visible, 'overlays': overlays, 'display_window': count, 'calculation_history': {'record_count': len(full_records), 'records': full_records, 'history_contract': {'calculation': 'full_available_history', 'presentation': 'tail_window', 'display_window': count, 'recalculate_in_hmi': False}}}
    return _screen_project(reference, dynamic)

def _screen_native_status(panel: Mapping[str, Any], chart_id: str) -> str:
    values = panel.get('series', {}).get(_screen_PRIMARY_SERIES[chart_id], []) if isinstance(panel.get('series'), Mapping) else []
    return 'available' if any((_screen_finite(value) is not None for value in values)) else 'partial'

def _screen_native_tf(processing: Mapping[str, Any], chart_id: str, timeframe: str, reference: Mapping[str, Any]) -> dict[str, Any]:
    panel = processing.get('native_analysis', {}).get(timeframe, {}).get(chart_id, {})
    timestamps = panel.get('timestamps', []) if isinstance(panel, Mapping) else []
    count = min(_screen_DISPLAY_WINDOW, len(timestamps))
    series = _screen_slice_series(panel.get('series', {}) if isinstance(panel, Mapping) else {}, count)
    current = deepcopy(panel.get('current', {}) if isinstance(panel, Mapping) else {})
    dynamic = {'status': _screen_native_status(panel, chart_id) if isinstance(panel, Mapping) else 'unavailable', 'timestamps': deepcopy(timestamps[-count:]), 'series': series, 'current': current, 'reference_lines': deepcopy(reference.get('reference_lines', [])), 'history': {'closed_candles_available': len(timestamps), 'display_records': count, 'uses_closed_candles_only': True, 'all_indicator_periods_warm': len(timestamps) >= 90}, 'provenance': {'data_mode': processing['context'].get('data_mode'), 'processing_contract_target': True, 'real_market_calculation': not bool(processing['context'].get('is_demo')), 'hmi_recalculate': False}}
    return _screen_project(reference, dynamic)

def _screen_charts(reference: Mapping[str, Any], processing: Mapping[str, Any], selected_timeframe: str) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for chart_id, ref_chart in reference.items():
        chart = deepcopy(ref_chart)
        if 'selected_market' in chart:
            chart['selected_market'] = 'all_exchanges'
        if 'selected_timeframe' in chart:
            chart['selected_timeframe'] = selected_timeframe
        if chart_id in {'open_interest_candlestick', 'open_interest_ohlc'}:
            ref_frames = ref_chart['markets']['all_exchanges']['timeframes']
            prototype = next(iter(ref_frames.values()))
            tf_map = {tf: _screen_candle_tf(processing, tf, ref_frames.get(tf, prototype)) for tf in _screen_HMI_TIMEFRAMES}
            chart['markets'] = {'all_exchanges': {'timeframes': tf_map}}
            chart['status'] = tf_map[selected_timeframe]['status']
        elif chart_id in _screen_NATIVE_CHARTS:
            ref_frames = ref_chart['markets']['all_exchanges']
            prototype = next(iter(ref_frames.values()))
            tf_map = {tf: _screen_native_tf(processing, chart_id, tf, ref_frames.get(tf, prototype)) for tf in _screen_HMI_TIMEFRAMES}
            chart['markets'] = {'all_exchanges': tf_map}
            chart['status'] = tf_map[selected_timeframe]['status']
        output[chart_id] = chart
    return output

def _screen_event_candidates_from_visible_charts(charts: Mapping[str, Any]) -> dict[str, Any]:
    """Build Screen-A MA crosses only from the exact OHLC/MA window published to HMI.

    This prevents stale crosses from a deeper calculation history from leaking into
    the visible chart and makes the exact marker reproducible from contractual MAs.
    """
    by_id: dict[str, Any] = {}
    chart = charts.get('open_interest_candlestick', {}) if isinstance(charts, Mapping) else {}
    frames = chart.get('markets', {}).get('all_exchanges', {}).get('timeframes', {}) if isinstance(chart, Mapping) else {}
    for timeframe in _screen_HMI_TIMEFRAMES:
        frame = frames.get(timeframe, {}) if isinstance(frames, Mapping) else {}
        records = frame.get('records', []) if isinstance(frame, Mapping) else []
        overlays = frame.get('overlays', {}) if isinstance(frame, Mapping) else {}
        ma = overlays.get('moving_averages', {}).get('series', {}) if isinstance(overlays, Mapping) else {}
        if not isinstance(records, list) or len(records) < 2 or (not isinstance(ma, Mapping)):
            continue
        timestamps = [row.get('timestamp') for row in records if isinstance(row, Mapping)]
        if len(timestamps) != len(records) or any((type(ts) is not int for ts in timestamps)):
            continue
        first_visible, last_visible = (timestamps[0], timestamps[-1])
        for first, second in (('ema_9', 'ema_21'), ('sma_20', 'sma_50'), ('wma_20', 'wma_50')):
            a, b = (ma.get(first, []), ma.get(second, []))
            if not isinstance(a, list) or not isinstance(b, list) or len(a) != len(records) or (len(b) != len(records)):
                continue
            for index in range(1, len(records)):
                p1, p2, c1, c2 = map(_screen_finite, (a[index - 1], b[index - 1], a[index], b[index]))
                if None in (p1, p2, c1, c2):
                    continue
                d0, d1 = (float(p1 - p2), float(c1 - c2))
                crossed = d0 < 0 <= d1 or d0 > 0 >= d1
                if not crossed or d1 == d0:
                    continue
                alpha = -d0 / (d1 - d0)
                if not 0.0 <= alpha <= 1.0:
                    continue
                exact_timestamp = float(timestamps[index - 1]) + alpha * (timestamps[index] - timestamps[index - 1])
                if not first_visible <= exact_timestamp <= last_visible:
                    continue
                exact_value = float(p1) + alpha * (float(c1) - float(p1))
                direction = 1 if d1 >= 0 else -1
                relation = 'above' if direction == 1 else 'below'
                event_id = f'{first}_{relation}_{second}'
                uid = f'all_exchanges:{timeframe}:{timestamps[index]}:technical_cross:{event_id}'
                by_id[uid] = {'event_uid': uid, 'timestamp': timestamps[index], 'event_id': event_id, 'event_type': 'technical_cross', 'event_group': 'moving_average_cross', 'signal': 'bullish' if direction == 1 else 'bearish', 'label': event_id.replace('_', ' ').upper(), 'marker': 'arrow_up' if direction == 1 else 'arrow_down', 'source': {'market': 'all_exchanges', 'timeframe': timeframe}, 'display': {'screen_a': True, 'screen_b': False, 'anchor_price': exact_value, 'marker_anchor': 'exact_interpolated_cross'}, 'event_timestamp_exact': exact_timestamp, 'event_value_exact': exact_value, 'event_price': exact_value}
    return dict(sorted(by_id.items(), key=lambda item: (item[1].get('event_timestamp_exact') or 0, item[0])))

def _screen_events(reference: Mapping[str, Any], charts: Mapping[str, Any]) -> dict[str, Any]:
    """Publish exact Screen-A MA crosses generated by Processing.

    The template intentionally carries an empty events.by_id map. Requiring a
    template prototype therefore discarded every runtime cross and left the
    HMI without arrows. HMI only renders these precomputed events.
    """
    out = deepcopy(dict(reference))
    by_id = _screen_event_candidates_from_visible_charts(charts)
    out['by_id'] = by_id
    out['technical_cross_ids'] = list(by_id)
    return out

def _screen_signal(chart_id: str, current: Mapping[str, Any]) -> tuple[str, str, float]:
    primary = _screen_finite(current.get(chart_id))
    if chart_id == 'price_oi_regime':
        state = str(current.get('regime_state') or 'normal')
        if state in {'bullish_expansion', 'short_covering'}:
            return ('bullish', state, 0.75)
        if state in {'bearish_expansion', 'deleveraging', 'long_liquidation'}:
            return ('bearish', state, 0.75)
        return ('neutral', state, 0.55)
    if chart_id == 'funding_oi_crowding':
        if primary is not None and primary >= 2:
            return ('bearish', 'crowded_long', 0.8)
        if primary is not None and primary <= -2:
            return ('bullish', 'crowded_short', 0.8)
        return ('neutral', 'balanced', 0.55)
    if chart_id == 'wasserstein_distance':
        return ('neutral', 'regime_shift_elevated' if primary is not None and primary >= 0.1 else 'stable', 0.6)
    if primary is None:
        return ('neutral', 'unavailable', 0.0)
    return ('bullish', 'positive', 0.65) if primary > 0 else ('bearish', 'negative', 0.65) if primary < 0 else ('neutral', 'neutral', 0.5)

def _screen_table(reference: Mapping[str, Any], processing: Mapping[str, Any], selected_timeframe: str) -> dict[str, Any]:
    out = deepcopy(dict(reference))
    ref_markets = reference.get('indicator_package', {}).get('markets', {}).get('all_exchanges', {})
    markets: dict[str, list[dict[str, Any]]] = {}
    for tf in _screen_HMI_TIMEFRAMES:
        ref_rows = {str(row.get('metric_id')): row for row in ref_markets.get(tf, []) if isinstance(row, Mapping)}
        rows: list[dict[str, Any]] = []
        for chart_id in _screen_NATIVE_CHARTS:
            panel = processing.get('native_analysis', {}).get(tf, {}).get(chart_id, {})
            current = panel.get('current', {}) if isinstance(panel, Mapping) else {}
            primary = _screen_finite(current.get(chart_id))
            signal, state, confidence = _screen_signal(chart_id, current)
            dynamic = {'metric_id': chart_id, 'value': primary, 'timestamp': panel.get('timestamps', [])[-1] if panel.get('timestamps') else None, 'status': _screen_native_status(panel, chart_id) if isinstance(panel, Mapping) else 'unavailable', 'signal': signal, 'state': state, 'display_signal': signal.upper(), 'confidence': confidence, 'secondary_values': {key: value for key, value in current.items() if key != chart_id}, 'provenance': {'data_mode': processing['context'].get('data_mode'), 'processing_contract_target': True, 'real_market_calculation': not bool(processing['context'].get('is_demo')), 'hmi_recalculate': False}}
            rows.append(_screen_project(ref_rows.get(chart_id, {}), dynamic))
        markets[tf] = rows
    out['indicator_package'] = {'selected_market': 'all_exchanges', 'selected_timeframe': selected_timeframe, 'markets': {'all_exchanges': markets}}
    return out

def _screen_quality(reference: Mapping[str, Any], classification: Mapping[str, Any], processing: Mapping[str, Any], events: Mapping[str, Any]) -> dict[str, Any]:
    out = deepcopy(dict(reference))
    q = classification.get('quality', {})
    status = q.get('status', 'partial')
    out.update({'status': 'ok' if status == 'available' else status, 'contract_complete': True, 'data_complete': bool(q.get('data_complete', status == 'available')), 'volume_enabled': False, 'warnings': deepcopy(q.get('warnings', [])), 'errors': deepcopy(q.get('errors', []))})
    ext = out.get('extensions', {})
    if isinstance(ext.get('screen_b_arrow_audit_v2'), dict):
        ext['screen_b_arrow_audit_v2']['event_counts'] = {'macd': 0, 'adx': 0, 'stochastic': 0}
    if isinstance(ext.get('realistic_oi_500_v2'), dict):
        full = len(processing['series']['open_interest_ohlc']['timeframes']['15m'].get('records', []))
        ext['realistic_oi_500_v2']['history_records_per_timeframe'] = full
        ext['realistic_oi_500_v2']['visible_records'] = min(_screen_DISPLAY_WINDOW, full)
        ext['realistic_oi_500_v2']['screen_a_cross_events'] = len(events.get('by_id', {}))
        ext['realistic_oi_500_v2']['screen_b_indicator_cross_events'] = 0
    if isinstance(ext.get('realism_v1'), dict):
        ref = processing['context'].get('reference_timestamp')
        ext['realism_v1']['fixture_as_of_timestamp'] = None
        ext['realism_v1']['fixture_as_of_iso'] = None
        ext['realism_v1']['synthetic_not_live'] = bool(processing['context'].get('is_demo'))
    out['extensions'] = ext
    return out

def align_open_interest_and_funding_to_sp_v1_12(candidate: Mapping[str, Any], processing: Mapping[str, Any], classification: Mapping[str, Any], *, selected_timeframe: str) -> dict[str, Any]:
    reference = _screen_template()
    charts = _screen_charts(reference['charts'], processing, selected_timeframe)
    events = _screen_events(reference['events'], charts)
    dynamic: dict[str, Any] = {'family': 'open_interest_and_funding', 'screen': 'open_interest_and_funding', 'schema_version': SP_SCHEMA_VERSION, 'context': _screen_context(reference['context'], processing), 'badges': deepcopy(reference['badges'] if processing['context'].get('is_demo') else []), 'kpis': _screen_kpis(reference['kpis'], candidate, processing, selected_timeframe), 'widgets': deepcopy(candidate.get('widgets', {})), 'selectors': deepcopy(reference['selectors']), 'charts': charts, 'tables': {'indicators_metrics': _screen_table(reference['tables']['indicators_metrics'], processing, selected_timeframe)}, 'events': events, 'screen_layout': deepcopy(reference['screen_layout']), 'history_contract': deepcopy(reference['history_contract']), 'technical_analysis': deepcopy(reference['technical_analysis'])}
    dynamic['selectors']['timeframe']['selected'] = selected_timeframe
    dynamic['selectors']['timeframe']['options'] = list(_screen_HMI_TIMEFRAMES)
    history_count = min((len(processing['series']['open_interest_ohlc']['timeframes'][tf].get('records', [])) for tf in _screen_HMI_TIMEFRAMES))
    dynamic['history_contract'].update({'calculation_records': history_count, 'all_visible_moving_averages_warm': history_count >= 50, 'technical_indicators_precomputed': True, 'hmi_recalculation': False, 'synthetic_fixture': False, 'fixture_seed': None, 'note': 'runtime full-history calculation; Screen B uses six native OI analyses and HMI renders precomputed values only'})
    dynamic['quality'] = _screen_quality(reference['quality'], classification, processing, events)
    aligned = _screen_project(reference, dynamic)
    aligned['events']['by_id'] = events['by_id']
    aligned['events']['technical_cross_ids'] = events['technical_cross_ids']
    aligned['schema_version'] = SP_SCHEMA_VERSION
    aligned['kpis']['selected_timeframe'] = selected_timeframe
    for item in aligned.get('kpis', {}).get('items', []):
        if isinstance(item, dict) and item.get('metric_id') == 'estimated_leverage_ratio':
            provenance = item.get('provenance')
            if isinstance(provenance, dict):
                provenance['integration_state'] = 'processing_confirmation_available' if item.get('value') is not None else 'processing_confirmation_unavailable'
    for chart_id in ('open_interest_candlestick', 'open_interest_ohlc'):
        contract = aligned.get('charts', {}).get(chart_id, {}).get('ohlc_contract')
        if isinstance(contract, dict):
            contract['native_provider_ohlc'] = True
            contract['construction_stage'] = 'input_provider_native_or_local_resample'
    json.dumps(aligned, ensure_ascii=False, allow_nan=False)
    return aligned
