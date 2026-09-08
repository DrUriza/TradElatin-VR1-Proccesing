"""ETF & Exchange Flows Screen Contract Builder; SP 1.4 adapter is final authority."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from typing import Any

FAMILY = "etf_exchange_flows"
UPSTREAM_VERSION = "0.1"
CONTRACT_VERSION = "1.4.1-exchange-reserve-realism-v4"
SCHEMA_ID = "trad_elatin.etf_exchange_flows.screen.v1"
SCHEMA_VERSION = "1.4.1-exchange-reserve-realism-v2"
RANGE_SECONDS = {"1d": 86_400, "7d": 604_800, "30d": 2_592_000}
CLASSIFICATION_RANGES = ("1d", "7d", "30d")
DISPLAY_RANGES = ("7d", "30d")
BUILDABLE_RANGES = tuple(RANGE_SECONDS)
VALID_STATUSES = {"available", "partial", "unavailable", "invalid"}

REQUIRED_VIEWS = (
    "kpis.etf_net_flow", "kpis.total_aum", "kpis.exchange_inflow",
    "kpis.exchange_outflow", "kpis.exchange_balance", "kpis.exchange_flow_pressure",
    "kpis.cumulative_etf_net_flow", "charts.etf_flow_daily",
    "charts.exchange_net_flow", "charts.exchange_balance", "tables.etf_funds",
    "classification_states.etf_flow_direction", "classification_states.etf_flow_persistence",
    "classification_states.exchange_pressure_regime",
    "classification_states.composite_capital_flow_regime", "classification_states.data_confidence",
)
OPTIONAL_VIEWS = (
    "kpis.gbtc_premium", "classification_states.gbtc_premium_regime",
    "classification_states.exchange_netflow_regime",
    "classification_states.aum_reconciliation_state", "provider_reconciliation",
    "tables.etf_funds.issuer_flow",
)

SCREEN_LAYOUT_CONTRACT = {'top_kpis': ['etf_net_flow',
              'total_aum',
              'cumulative_etf_net_flow',
              'exchange_inflow',
              'exchange_outflow',
              'exchange_balance',
              'gbtc_premium',
              'exchange_flow_pressure'],
 'main_content': [{'type': 'bar', 'chart_id': 'etf_flow_daily', 'technical_analysis_allowed': False},
                  {'type': 'table', 'table_id': 'etf_funds', 'title': 'ETF Flow by Provider'},
                  {'type': 'bar', 'chart_id': 'exchange_net_flow', 'technical_analysis_allowed': False},
                  {'type': 'candlestick',
                   'chart_id': 'exchange_balance',
                   'technical_analysis_allowed': True}],
 'right_panel': {'component': 'technical_indicator_selectors',
                 'target_chart_id': 'exchange_balance',
                 'source': 'technical_analysis.selector_contract',
                 'volume_indicators_excluded': True,
                 'button': 'ANÁLISIS TÉCNICO FUNDAMENTAL'},
 'analysis_view': {'mode': 'exchange_balance_only',
                   'target_chart_id': 'exchange_balance',
                   'selection_persisted_from_screen_a': True,
                   'volume_indicators_excluded': True,
                   'columns_desktop': 3,
                   'columns_medium': 2,
                   'columns_mobile': 1,
                   'indicator_chart_height_px': 152,
                   'indicator_card_min_height_px': 180,
                   'visual_size_reference': 'prices',
                   'indicator_header_height_px': 26,
                   'back_button': {'visible': True, 'label': '← REGRESAR', 'target_view': 'main'},
                   'summary_panel': {'visible': True,
                                     'position': 'right',
                                     'width_px': 314,
                                     'mode': 'exchange_balance',
                                     'source': 'technical_analysis.indicators.*.summary'},
                   'global_view_selector_visible': False,
                   'global_market_selector_visible': False},
 'main_view': {'total_height_px': 590,
               'technical_selector_height_px': 590,
               'content_grid': {'columns': 2, 'rows': 2, 'card_height_px': 291, 'gap_px': 8},
               'alignment': 'same_height',
               'visual_reference': 'canonical_screen_a_590'}}



def _timestamp(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value


def _identity(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _at(root: Mapping[str, Any], path: str, default: Any = None) -> Any:
    value: Any = root
    for name in path.split("."):
        if not isinstance(value, Mapping) or name not in value:
            return default
        value = value[name]
    return value


def _json_copy(value: Any, path: str = "root") -> Any:
    if isinstance(value, Mapping):
        copied: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(f"non_json_contract_value:{path}:key")
            copied[key] = _json_copy(child, f"{path}.{key}")
        return copied
    if isinstance(value, list):
        return [_json_copy(child, f"{path}[{index}]") for index, child in enumerate(value)]
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError(f"non_json_contract_value:{path}:{type(value).__name__}")


def _validate_upstream(contract: Any, *, stage: str) -> list[str]:
    if not isinstance(contract, Mapping):
        return [f"invalid_{stage}_contract"]
    errors = []
    for field, expected in (("family", FAMILY), ("stage", stage), ("version", UPSTREAM_VERSION)):
        if contract.get(field) != expected:
            errors.append(f"invalid_{stage}_{field}")
    required = ("features", "series", "snapshots", "series_metadata", "quality", "provenance") if stage == "processing" else (
        "classifications", "quality", "provenance")
    for field in required:
        if not isinstance(contract.get(field), Mapping):
            errors.append(f"invalid_{stage}_{field}")
    if _timestamp(contract.get("data_as_of")) is None:
        errors.append(f"invalid_{stage}_data_as_of")
    return errors


def _unavailable_financial(*, source_path: str, unit: str, status: str = "unavailable",
                           reason: str = "source_unavailable") -> dict[str, Any]:
    return {"status": status, "reason": reason, "value": None, "unit": unit, "data_as_of": None,
            "provider": None, "endpoint_id": None, "source_path": source_path, "warnings": []}


def _financial(feature: Any, *, source_path: str, unit: str, processing_anchor: int) -> dict[str, Any]:
    if not isinstance(feature, Mapping):
        return _unavailable_financial(source_path=source_path, unit=unit, status="invalid",
                                      reason="source_feature_invalid")
    status = feature.get("status")
    if status not in VALID_STATUSES:
        return _unavailable_financial(source_path=source_path, unit=unit, status="invalid",
                                      reason="source_status_invalid")
    source_unit = feature.get("unit")
    if source_unit != unit:
        return _unavailable_financial(source_path=source_path, unit=unit, status="invalid",
                                      reason="source_unit_incompatible")
    warnings = feature.get("warnings", [])
    if not isinstance(warnings, list) or not all(isinstance(item, str) for item in warnings):
        return _unavailable_financial(source_path=source_path, unit=unit, status="invalid",
                                      reason="source_warnings_invalid")
    if status in {"unavailable", "invalid"}:
        return {**_unavailable_financial(source_path=source_path, unit=unit, status=status,
                                         reason=feature.get("reason") or f"source_{status}"),
                "provider": deepcopy(feature.get("provider")) if isinstance(feature.get("provider"), str) else None,
                "endpoint_id": deepcopy(feature.get("endpoint_id")) if isinstance(feature.get("endpoint_id"), str) else None,
                "warnings": list(warnings)}
    value = _number(feature.get("value"))
    anchor = _timestamp(feature.get("data_as_of"))
    if value is None:
        return _unavailable_financial(source_path=source_path, unit=unit, status="invalid",
                                      reason="source_numeric_invalid")
    if anchor is None or anchor > processing_anchor:
        return _unavailable_financial(source_path=source_path, unit=unit, status="invalid",
                                      reason="upstream_timestamp_inconsistent")
    provider = feature.get("provider")
    endpoint_id = feature.get("endpoint_id")
    if provider is not None and _identity(provider) is None:
        return _unavailable_financial(source_path=source_path, unit=unit, status="invalid",
                                      reason="source_identity_invalid")
    if endpoint_id is not None and _identity(endpoint_id) is None:
        return _unavailable_financial(source_path=source_path, unit=unit, status="invalid",
                                      reason="source_identity_invalid")
    return {"status": status, "reason": deepcopy(feature.get("reason")), "value": value, "unit": unit,
            "data_as_of": anchor, "provider": deepcopy(provider), "endpoint_id": deepcopy(endpoint_id),
            "source_path": source_path, "warnings": list(warnings)}


def _chart(*, chart_id: str, source: Any, value_field: str, unit: str, source_path: str,
           anchor: int, range_seconds: int, identities: Sequence[str] = ()) -> dict[str, Any]:
    base = {"chart_id": chart_id, "status": "unavailable", "reason": "source_series_empty", "unit": unit,
            "source_path": source_path, "points": [], "data_as_of": None, "warnings": []}
    if not isinstance(source, list):
        return {**base, "status": "invalid", "reason": "source_series_invalid"}
    lower = anchor - range_seconds
    points, invalid = [], 0
    for record in source:
        if not isinstance(record, Mapping):
            invalid += 1
            continue
        timestamp = _timestamp(record.get("timestamp"))
        value = _number(record.get(value_field))
        if timestamp is None or timestamp > anchor:
            invalid += 1
            continue
        if not lower < timestamp <= anchor:
            continue
        identity_values = {field: _identity(record.get(field)) for field in identities}
        if any(value is None for value in identity_values.values()):
            invalid += 1
            continue
        provider = _identity(record.get("provider"))
        endpoint_id = _identity(record.get("endpoint_id"))
        # Processing-owned identities may legitimately have no endpoint_id.
        # They are derived from already contracted primitives and must not be
        # mislabeled as a fourth provider endpoint.
        if value is None or provider is None or (endpoint_id is None and provider != "calculated"):
            invalid += 1
            continue
        points.append({"timestamp": timestamp, "value": value, **identity_values,
                       "provider": provider, "endpoint_id": endpoint_id})
    points.sort(key=lambda item: (item["timestamp"], *(item.get(field, "") for field in identities),
                                  item["provider"], item.get("endpoint_id") or ""))
    if points:
        status = "partial" if invalid else "available"
        reason = "source_points_invalid" if invalid else None
        return {**base, "status": status, "reason": reason, "points": points,
                "data_as_of": points[-1]["timestamp"], "warnings": [reason] if reason else []}
    if invalid:
        return {**base, "status": "invalid", "reason": "source_points_invalid"}
    return base


def _history_chart(*, chart_id: str, source: Any, value_field: str, unit: str, source_path: str,
                   anchor: int, range_seconds: int, data_mode: Any = None, etf_calendar: bool = False) -> dict[str, Any]:
    chart = _chart(chart_id=chart_id, source=source, value_field=value_field, unit=unit,
                   source_path=source_path, anchor=anchor, range_seconds=range_seconds)
    all_points = _chart(chart_id=chart_id, source=source, value_field=value_field, unit=unit,
                        source_path=source_path, anchor=anchor, range_seconds=10**12)["points"]
    synthetic = data_mode == "synthetic"
    enriched = []
    for point in all_points:
        item = dict(point)
        item["is_synthetic"] = synthetic
        if etf_calendar:
            item["session_type"] = "weekday_trading_session_emulator" if synthetic else "provider_trading_session"
        enriched.append(item)
    history = {"record_count": len(enriched), "points": enriched}
    if etf_calendar:
        history["calendar_policy"] = "weekdays_only_holidays_not_explicitly_modeled"
    history["recalculate_in_hmi"] = False
    chart.update(points=enriched, data_as_of=enriched[-1]["timestamp"] if enriched else None,
        chart_type="bar", presentation={"positive_color_token": "bullish", "negative_color_token": "bearish",
        "zero_line": True, "technical_analysis_allowed": False}, calculation_history=history,
        records_available=len(enriched), records_returned=len(enriched))
    return chart


def _candlestick_chart(source: Any, *, source_points: Any, anchor: int, data_mode: Any = None) -> dict[str, Any]:
    candles = []
    invalid = 0
    source_is_list = isinstance(source, list)
    if isinstance(source, list):
        for item in source:
            if not isinstance(item, Mapping):
                continue
            timestamp = _timestamp(item.get("timestamp"))
            values = [_number(item.get(field)) for field in ("open", "high", "low", "close")]
            if timestamp is None or timestamp > anchor or any(value is None for value in values):
                invalid += 1
                continue
            candles.append({"timestamp": timestamp, "open": values[0], "high": values[1], "low": values[2],
                            "close": values[3], "is_closed": item.get("is_closed") is True})
    candles.sort(key=lambda item: item["timestamp"])
    valid_source_points = []
    if isinstance(source_points, list):
        for item in source_points:
            if not isinstance(item, Mapping):
                continue
            timestamp = _timestamp(item.get("timestamp")) or anchor
            value = _number(item.get("total_balance", item.get("balance_btc", item.get("value"))))
            exchange_name = _identity(item.get("exchange_name"))
            symbol = _identity(item.get("symbol"))
            provider = _identity(item.get("provider"))
            endpoint_id = _identity(item.get("endpoint_id"))
            if timestamp > anchor or value is None or None in (exchange_name, symbol, provider, endpoint_id):
                continue
            valid_source_points.append({"timestamp": timestamp, "value": value, "exchange_name": exchange_name,
                                        "symbol": symbol, "provider": provider, "endpoint_id": endpoint_id})
    valid_source_points.sort(key=lambda item: (item["timestamp"], item["exchange_name"]))
    status = "partial" if candles and invalid else "available" if candles else "invalid" if invalid or not source_is_list else "unavailable"
    synthetic = data_mode == "synthetic"
    return {"chart_id": "exchange_balance", "status": status, "reason": None if candles else "source_series_empty",
        "unit": "BTC", "source_path": "series.exchange_balance", "data_as_of": candles[-1]["timestamp"] if candles else None,
        "warnings": [], "overlays": {}, "chart_type": "candlestick",
        "preferred_representation": "candlestick", "title": "Exchange Balance (BTC)", "source_points": valid_source_points,
        "candles": candles, "candle_count": len(candles), "ohlc_contract": {
            "required_fields": ["timestamp", "open", "high", "low", "close"], "series_container": "candles",
            "profile": "state", "construction_stage": "processing",
            "construction_rule": {"open": "first_real_value_in_bucket", "high": "max_real_value_in_bucket",
                "low": "min_real_value_in_bucket", "close": "last_real_value_in_bucket"},
            "native_provider_ohlc": False, "hmi_must_reconstruct_ohlc": False, "line_fallback_allowed": False,
            "volume_allowed": False, "technical_analysis_allowed": True,
            "candle_colors": {"up": "bullish", "down": "bearish", "metric_specific_colors_allowed": False}},
        "synthetic_history": {"status": "available" if synthetic else "not_applicable", "points": len(candles),
            "resolution": "1d", "purpose": "contract_hmi_validation" if synthetic else "live_provider_history"},
        "calculation_history": {"record_count": len(candles), "candles": candles, "recalculate_in_hmi": False,
            "construction": "processing_from_cryptoquant_reserve_hour"},
        "records_available": len(candles), "records_returned": len(candles)}


def _indicator_contract(indicator_id: str, source: Mapping[str, Any], *, unit: str = "value") -> dict[str, Any]:
    series = _json_copy(source.get("series", {}), f"technical_analysis.{indicator_id}.series")
    timestamps = _json_copy(source.get("timestamps", []), f"technical_analysis.{indicator_id}.timestamps")
    current = _json_copy(source.get("current", {}), f"technical_analysis.{indicator_id}.current")
    primary = next((value for value in reversed(list(current.values())) if value is not None), None)
    return {"status": "available" if timestamps else "unavailable", "reason": None if timestamps else "source_series_empty",
        "timestamps": timestamps, "series": series, "current": current, "thresholds": [], "unit": unit,
        "recalculate_in_hmi": False, "summary": {"indicator_id": indicator_id, "label": indicator_id.upper(),
            "section": "technical_analysis", "value": primary,
            "display_value": f"{primary:.4f}" if isinstance(primary, (int, float)) else "N/A",
            "signal": "NEUTRAL", "signal_color": "neutral", "strength": 0,
            "secondary": {}, "status": "available" if timestamps else "unavailable",
            "classification_basis": "processing_precomputed", "recalculate_in_hmi": False},
        "calculation_contract": {"source": "charts.exchange_balance.candles", "basis": "OHLC",
            "implementation_owner": "Processing", "recalculate_in_hmi": False, "formula_family": indicator_id},
        "calculation_history_records": len(timestamps)}


def _technical_analysis(source: Any, event_source: Any = None, *, data_mode: str = "live") -> dict[str, Any]:
    raw = source if isinstance(source, Mapping) else {}
    packages = raw.get("indicators", {}) if isinstance(raw.get("indicators"), Mapping) else {}
    moving = packages.get("moving_averages", {})
    bollinger = packages.get("bollinger_bands", {})
    indicator_names = ("macd", "rsi", "tsi", "adx", "stochastic", "williams_r", "cci", "atr", "wasserstein_distance", "bollinger_band_width")
    indicators = {name: _indicator_contract(name, packages.get(name, {}),
        unit="percent" if name in {"rsi", "stochastic", "williams_r"} else "BTC" if name == "atr" else "value")
        for name in indicator_names}
    timestamps = list(moving.get("timestamps", [])) if isinstance(moving, Mapping) else []
    indicators["wasserstein_distance"].pop("calculation_contract", None)
    def percentile(name: str, series_name: str) -> float | None:
        values = packages.get(name, {}).get("series", {}).get(series_name, [])
        valid = [float(value) for value in values if isinstance(value, (int, float)) and math.isfinite(value)]
        if not valid:
            return None
        current_value = valid[-1]
        return sum(value <= current_value for value in valid) / len(valid) * 100
    indicators["macd"]["summary"]["secondary"] = {
        "signal": indicators["macd"]["current"].get("signal"),
        "histogram": indicators["macd"]["current"].get("histogram")}
    indicators["adx"]["summary"]["secondary"] = {"di_plus": indicators["adx"]["current"].get("di_plus"),
        "di_minus": indicators["adx"]["current"].get("di_minus")}
    indicators["stochastic"]["summary"]["secondary"] = {"d": indicators["stochastic"]["current"].get("d")}
    indicators["tsi"]["summary"]["secondary"] = {"signal": indicators["tsi"]["current"].get("signal")}
    for name, series_name in (("atr", "atr"), ("wasserstein_distance", "wasserstein_distance"),
                              ("bollinger_band_width", "bollinger_band_width")):
        indicators[name]["summary"]["secondary"] = {"percentile": percentile(name, series_name)}
    indicators["rsi"].update(scale={"min": 0.0, "max": 100.0, "unit": "%", "basis": "fixed_oscillator_domain"},
                             threshold_basis="oscillator_domain_not_last_value")
    indicators["tsi"].update(parameters={"slow_period": 25, "fast_period": 13, "signal_period": 13},
        scale={"min": -100.0, "max": 100.0, "unit": "index", "basis": "fixed_oscillator_domain"},
        threshold_basis="oscillator_domain_not_last_value")
    indicators["stochastic"].update(
        scale={"min": 0.0, "max": 100.0, "unit": "%", "basis": "fixed_oscillator_domain"},
        threshold_basis="oscillator_domain_not_last_value",
        cross_gate={"bullish": "K crosses above D while K,D <= 20",
                    "bearish": "K crosses below D while K,D >= 80", "basis": "oscillator_domain"})
    indicators["rsi"]["thresholds"] = [
        {"role": "oversold", "label": "30%", "value": 30.0, "unit": "%", "basis": "oscillator_domain", "domain_fraction": 0.3},
        {"role": "overbought", "label": "70%", "value": 70.0, "unit": "%", "basis": "oscillator_domain", "domain_fraction": 0.7}]
    indicators["stochastic"]["thresholds"] = [
        {"role": "oversold", "label": "20%", "value": 20.0, "unit": "%", "basis": "oscillator_domain", "domain_fraction": 0.2},
        {"role": "overbought", "label": "80%", "value": 80.0, "unit": "%", "basis": "oscillator_domain", "domain_fraction": 0.8}]
    indicators["tsi"]["thresholds"] = [
        {"role": "oversold", "label": "-25", "value": -25.0, "unit": "index", "basis": "tsi_reference_domain"},
        {"role": "neutral", "label": "0", "value": 0.0, "unit": "index", "basis": "tsi_reference_domain"},
        {"role": "overbought", "label": "+25", "value": 25.0, "unit": "index", "basis": "tsi_reference_domain"}]
    indicators["williams_r"]["thresholds"] = [{"value": -20.0, "role": "overbought"},
                                                   {"value": -80.0, "role": "oversold"}]
    percent_contracts = {
        "rsi": {"formula": "Wilder RSI(14)", "input": "close_return_pct",
            "input_transform": "100 * (close_t / close_(t-1) - 1)", "output_scale": [0.0, 100.0], "unit": "%"},
        "tsi": {"formula": "TSI(25,13) + Signal EMA(13)", "input": "close_return_pct",
            "input_transform": "100 * (close_t / close_(t-1) - 1)", "output_scale": [-100.0, 100.0], "unit": "%"},
        "stochastic": {"formula": "Stochastic(14,3,3)",
            "input": "high_return_pct / low_return_pct / close_return_pct",
            "input_transform": "OHLC expressed as percentage movement relative to previous close",
            "output_scale": [0.0, 100.0], "unit": "%", "buy_cross_gate": "<= 20%", "sell_cross_gate": ">= 80%"}}
    for name, metadata in percent_contracts.items():
        indicators[name]["calculation_contract"] = {**metadata, "implementation_owner": "Processing", "recalculate_in_hmi": False}
        indicators[name]["summary"].update(unit="%", calculation_basis="percentage_normalized_series")
    regression = raw.get("regression_channel", {}) if isinstance(raw.get("regression_channel"), Mapping) else {}
    classified_events = event_source.get("events", []) if isinstance(event_source, Mapping) and isinstance(event_source.get("events"), list) else []
    return {"analysis_id": "exchange_balance_technical_analysis", "contract_family": "prices_cvd_volatility_equivalent",
        "target_chart_id": "exchange_balance", "target_title": "Exchange Balance (BTC)",
        "selector_contract": {"trend": ["ema_9", "ema_21", "ema_50", "sma_20", "sma_50", "sma_100", "sma_200", "wma_20", "wma_50"],
            "bands": ["bollinger_bands", "regression_channel"], "derived_analysis": ["adx", "bollinger_band_width"],
            "momentum": ["macd", "rsi", "tsi", "stochastic", "williams_r", "cci"],
            "volatility": ["atr", "wasserstein_distance"], "excluded": ["volume", "mfi"]},
        "recalculate_in_hmi": False, "overlays": {
            "moving_averages": {"status": "available" if timestamps else "unavailable",
                "series": _json_copy(moving.get("series", {}), "technical_analysis.moving_averages.series"),
                "recalculate_in_hmi": False, "warmup": {"records": 200, "purpose": "indicator_warmup",
                    "serialized_in_screen_candles": True, "recalculate_in_hmi": False}},
            "bollinger_bands": {"status": "available" if timestamps else "unavailable",
                "series": _json_copy(bollinger.get("series", {}), "technical_analysis.bollinger_bands.series"),
                "parameters": _json_copy(bollinger.get("parameters", {}), "technical_analysis.bollinger_bands.parameters"),
                "recalculate_in_hmi": False},
            "regression_channel": {"status": "available" if timestamps else "unavailable",
                "series": _json_copy(regression.get("series", {"upper": [], "middle": [], "lower": []}),
                                     "technical_analysis.regression_channel.series"),
                "parameters": _json_copy(regression.get("parameters", {"period": 100, "standard_deviations": 2.0}),
                                          "technical_analysis.regression_channel.parameters"), "recalculate_in_hmi": False}},
        "indicators": indicators, "events": _json_copy(classified_events, "technical_analysis.events"),
        "moving_average_cross_policy": {"same_family_only": True, "families": ["EMA", "SMA", "WMA"]},
        "channel_cross_policy": {"first_series": "price", "second_series": "channel"},
        "stochastic_cross_policy": {"buy": "k_crosses_above_d_below_20", "sell": "k_crosses_below_d_above_80"},
        "tsi_reference_lines": {"overbought": 25.0, "oversold": -25.0}, "source_path": "charts.exchange_balance.candles",
        "status": "available" if timestamps else "unavailable", "warmup_contract": {
            "calculation_records": len(timestamps), "minimum_warmup_records": 200, "maximum_standard_indicator_period": 200,
            "all_visible_moving_averages_warm": len(timestamps) >= 200, "technical_indicators_precomputed": True,
            "hmi_recalculation": False, "synthetic_fixture": False, "fixture_seed": None,
            "visible_records": len(timestamps), "resolution": "1d"},
        "indicator_cross_policy": {"macd": {"bullish": "MACD crosses above Signal", "bearish": "MACD crosses below Signal"},
            "adx": {"bullish": "DI+ crosses above DI-", "bearish": "DI+ crosses below DI-"},
            "stochastic": {"bullish": "K crosses above D while K,D <= 20", "bearish": "K crosses below D while K,D >= 80",
                "scale": {"min": 0.0, "max": 100.0, "unit": "%", "basis": "fixed_oscillator_domain"},
                "thresholds": indicators["stochastic"]["thresholds"],
                "threshold_basis": "oscillator_domain_not_last_value", "cross_gate": indicators["stochastic"]["cross_gate"]},
            "recalculate_in_hmi": False},
        "technical_fixture_validation": {"status": "ok" if timestamps else "unavailable",
            "source": "exchange_balance_ohlc", "series_recalculated": ["MACD(12,26,9)", "RSI(14)",
                "TSI(25,13)+Signal(13)", "ADX/DI(14)", "Stochastic(14,3,3)", "Williams %R(14)",
                "CCI(20)", "ATR(14)", "Bollinger Band Width(20,2)"], "hmi_calculation": False},
        "percentage_oscillator_contract_v1": {"status": "available",
            "indicators": ["RSI(14)", "TSI(25,13)+Signal(13)", "Stochastic(14,3,3)"],
            "input_basis": "percentage movement relative to previous close",
            "reason": "Normalize oscillator inputs across BTC balance and other non-price metrics.", "hmi_calculation": False},
        "oscillator_display_contract": {"basis": "precomputed", "never_scale_reference_lines_from_last_value": True,
            "never_scale_reference_lines_from_series_sum": True,
            "rsi": {"domain": [0, 100], "reference_lines": [30, 70]},
            "stochastic": {"domain": [0, 100], "reference_lines": [20, 80],
                "cross_gate": {"bullish": "K>D cross with K,D<=20", "bearish": "K<D cross with K,D>=80"}},
            "tsi": {"domain": [-100, 100], "reference_lines": [-25, 0, 25]}, "recalculate_in_hmi": False}}


def _cumulative_etf_net_flow(source: Any, *, processing_anchor: int) -> dict[str, Any]:
    source_path = "series.etf_cumulative_flow"
    base = {"kpi_id": "cumulative_etf_net_flow", "title": "CUMULATIVE ETF NET FLOW",
            "status": "unavailable", "reason": "source_series_empty", "value": None, "unit": "USD",
            "format_hint": "currency_compact", "data_as_of": None, "provider": None, "endpoint_id": None,
            "source_path": source_path, "warnings": []}
    if not isinstance(source, list):
        return {**base, "status": "invalid", "reason": "source_series_invalid"}
    valid, invalid = [], 0
    for record in source:
        if not isinstance(record, Mapping):
            invalid += 1
            continue
        timestamp = _timestamp(record.get("timestamp"))
        value = _number(record.get("cumulative_flow_usd"))
        provider = _identity(record.get("provider"))
        endpoint_id = _identity(record.get("endpoint_id"))
        if timestamp is None or timestamp > processing_anchor or value is None or provider is None or endpoint_id is None:
            invalid += 1
            continue
        valid.append((timestamp, value, provider, endpoint_id))
    if not valid:
        return {**base, "status": "invalid", "reason": "source_points_invalid"} if invalid else base
    timestamp, value, provider, endpoint_id = max(valid, key=lambda item: item[0])
    reason = "source_points_invalid" if invalid else None
    return {**base, "status": "partial" if invalid else "available", "reason": reason, "value": value,
            "data_as_of": timestamp, "provider": provider, "endpoint_id": endpoint_id,
            "warnings": [reason] if reason else []}


def _classification(wrapper: Any, *, source_path: str, processing_anchor: int) -> dict[str, Any]:
    if not isinstance(wrapper, Mapping):
        return {"state": None, "status": "invalid", "reason": "source_classification_invalid",
                "data_as_of": None, "evidence": {}, "source_features": [], "parameters": {}, "warnings": []}
    status = wrapper.get("status")
    if status not in VALID_STATUSES:
        status = "invalid"
    result = {name: _json_copy(wrapper.get(name), f"{source_path}.{name}") for name in
              ("state", "reason", "evidence", "source_features", "parameters", "warnings")}
    anchor = _timestamp(wrapper.get("data_as_of"))
    if status in {"available", "partial"} and (anchor is None or anchor > processing_anchor):
        return {"state": None, "status": "invalid", "reason": "upstream_timestamp_inconsistent",
                "data_as_of": None, "evidence": {}, "source_features": [source_path], "parameters": {}, "warnings": []}
    if status in {"unavailable", "invalid"}:
        result["state"] = None
        anchor = None
    return {"state": result["state"], "status": status, "reason": result["reason"],
            "data_as_of": anchor, "evidence": result["evidence"], "source_features": result["source_features"],
            "parameters": result["parameters"], "warnings": result["warnings"]}


def _fund_table(source: Any, *, selected_range: str, processing_anchor: int) -> dict[str, Any]:
    base = {"table_id": "etf_funds", "status": "unavailable", "reason": "source_records_empty",
            "selected_range": selected_range, "rows": [], "data_as_of": None, "warnings": []}
    if not isinstance(source, list):
        return {**base, "status": "invalid", "reason": "source_records_invalid"}
    rows, invalid = [], 0
    for item in source:
        if not isinstance(item, Mapping):
            invalid += 1
            continue
        ticker, fund_name = _identity(item.get("ticker")), _identity(item.get("fund_name"))
        provider, endpoint_id = _identity(item.get("provider")), _identity(item.get("endpoint_id"))
        periods = item.get("periods")
        period = periods.get(selected_range) if isinstance(periods, Mapping) else None
        if None in (ticker, fund_name, provider, endpoint_id) or not isinstance(period, Mapping):
            invalid += 1
            continue
        flow = _financial(period.get("period_flow_usd"), source_path=f"snapshots.funds[ticker={ticker}].periods.{selected_range}.period_flow_usd",
                          unit="USD", processing_anchor=processing_anchor)
        share = _financial(period.get("period_signed_flow_share"), source_path=f"snapshots.funds[ticker={ticker}].periods.{selected_range}.period_signed_flow_share",
                           unit="ratio", processing_anchor=processing_anchor)
        aum = _financial(item.get("aum_usd"), source_path=f"snapshots.funds[ticker={ticker}].aum_usd",
                         unit="USD", processing_anchor=processing_anchor)
        aum_share = _financial(item.get("aum_share"), source_path=f"snapshots.funds[ticker={ticker}].aum_share",
                               unit="ratio", processing_anchor=processing_anchor)
        issuer_source = item.get("issuer_flow")
        issuer = (_financial(issuer_source, source_path=f"snapshots.funds[ticker={ticker}].issuer_flow", unit="USD",
                             processing_anchor=processing_anchor) if isinstance(issuer_source, Mapping) else
                  _unavailable_financial(source_path=f"snapshots.funds[ticker={ticker}].issuer_flow", unit="USD",
                                         reason="issuer_identity_unavailable"))
        row_statuses = (flow["status"], aum["status"])
        row_status = "invalid" if "invalid" in row_statuses else "partial" if any(value != "available" for value in row_statuses) else "available"
        anchors = [value["data_as_of"] for value in (flow, aum) if value["status"] in {"available", "partial"}]
        rows.append({"ticker": ticker, "fund_name": fund_name, "provider": provider, "endpoint_id": endpoint_id,
                     "status": row_status, "reason": next((value["reason"] for value in (flow, aum) if value["reason"]), None),
                     "flow_usd": flow, "signed_flow_share": share, "aum_usd": aum, "aum_share": aum_share,
                     "issuer_flow": issuer, "data_as_of": min(anchors) if anchors else None, "warnings": []})
    rows.sort(key=lambda item: (item["ticker"], item["provider"], item["endpoint_id"]))
    if not rows:
        return {**base, "status": "invalid" if invalid else "unavailable",
                "reason": "source_records_invalid" if invalid else "source_records_empty"}
    table_status = "partial" if invalid or any(row["status"] != "available" for row in rows) else "available"
    anchors = [row["data_as_of"] for row in rows if row["data_as_of"] is not None]
    return {**base, "status": table_status, "reason": "source_records_invalid" if invalid else None,
            "rows": rows, "data_as_of": min(anchors) if anchors else None,
            "warnings": ["source_records_invalid"] if invalid else []}


def _view_statuses(root: Mapping[str, Any], selected_range: str) -> dict[str, str]:
    paths = {
        "kpis.etf_net_flow": "kpis.etf_net_flow", "kpis.total_aum": "kpis.total_aum",
        "kpis.exchange_inflow": "kpis.exchange_inflow", "kpis.exchange_outflow": "kpis.exchange_outflow",
        "kpis.exchange_balance": "kpis.exchange_balance", "kpis.exchange_flow_pressure": "kpis.exchange_flow_pressure",
        "kpis.cumulative_etf_net_flow": "kpis.cumulative_etf_net_flow",
        "charts.etf_flow_daily": "charts.etf_flow_daily",
        "charts.exchange_net_flow": "charts.exchange_net_flow", "charts.exchange_balance": "charts.exchange_balance",
        "tables.etf_funds": "tables.etf_funds",
        "classification_states.etf_flow_direction": f"classification_states.etf_flow_direction.{selected_range}",
        "classification_states.etf_flow_persistence": "classification_states.etf_flow_persistence",
        "classification_states.exchange_pressure_regime": "classification_states.exchange_pressure_regime",
        "classification_states.composite_capital_flow_regime": "classification_states.composite_capital_flow_regime",
        "classification_states.data_confidence": "classification_states.data_confidence",
    }
    return {name: str((_at(root, path, {}) or {}).get("status", "invalid")) for name, path in paths.items()}


def _fallback(errors: Sequence[str], *, selected_range: str, mode: Any = None,
              data_mode: Any = None, is_demo: Any = None) -> dict[str, Any]:
    output = {"schema": {"id": SCHEMA_ID, "version": SCHEMA_VERSION},
        "screen": {"id": FAMILY, "route": "/etf-exchange-flows", "title": "ETF & Exchange Flows", "family": FAMILY,
            "layout_contract": deepcopy(SCREEN_LAYOUT_CONTRACT)},
        "stage": "screen_contract", "version": CONTRACT_VERSION, "mode": deepcopy(mode), "data_mode": deepcopy(data_mode),
        "is_demo": deepcopy(is_demo), "context": {"generated_at": None, "processing_data_as_of": None,
            "classification_data_as_of": None, "data_as_of": None, "selected_range": selected_range,
            "calculation_history": "processing_precomputed_only"},
        "range_selector": {"selected": selected_range, "default": "30d",
            "options": [{"id": key, "label": key.upper(), "days": RANGE_SECONDS[key] // 86_400,
                         "seconds": RANGE_SECONDS[key]} for key in DISPLAY_RANGES],
            "selector_type": "RANGE"},
        "kpis": {}, "charts": {}, "tables": {}, "classification_states": {}, "provider_reconciliation": {},
        "technical_analysis": _technical_analysis({}), "history_contract": {},
        "operational_status": {"quality_status": "invalid", "connection_status": "not_reported",
            "cache_status": "not_reported", "generated_at": None, "data_as_of": None},
        "provenance": {"source_contracts": {}, "field_sources": {}, "providers": {}, "parameters": {}, "warnings": []},
        "quality": {"status": "invalid", "required": list(REQUIRED_VIEWS), "optional": list(OPTIONAL_VIEWS),
            "available": [], "partial": [], "unavailable": [], "invalid": list(REQUIRED_VIEWS),
            "data_as_of": None, "processing_status": "invalid", "classification_status": "invalid",
            "warnings": [], "errors": sorted(set(errors))}}
    json.dumps(output, ensure_ascii=False, allow_nan=False)
    return output


def build_etf_exchange_flows_contract(*, processing_contract: Mapping[str, Any],
                                      classification_contract: Mapping[str, Any], selected_range: str = "30d",
                                      generated_at: Any = None) -> dict[str, Any]:
    """Build a JSON-safe screen contract without mutating or recalculating upstream data."""
    if selected_range not in BUILDABLE_RANGES:
        raise ValueError("invalid_selected_range")
    processing_before = deepcopy(processing_contract)
    classification_before = deepcopy(classification_contract)
    errors = [*_validate_upstream(processing_contract, stage="processing"),
              *_validate_upstream(classification_contract, stage="classification")]
    mode = processing_contract.get("mode") if isinstance(processing_contract, Mapping) else None
    data_mode = processing_contract.get("data_mode") if isinstance(processing_contract, Mapping) else None
    is_demo = processing_contract.get("is_demo") if isinstance(processing_contract, Mapping) else None
    if errors:
        return align_etf_exchange_flows_contract_to_sp_v1_4(
            _fallback(errors, selected_range=selected_range, mode=mode, data_mode=data_mode, is_demo=is_demo),
            processing_contract if isinstance(processing_contract, Mapping) else {},
        )
    processing = processing_contract
    classification = classification_contract
    processing_declared_anchor = _timestamp(processing["data_as_of"])
    processing_last_timestamp = _timestamp(processing.get("quality", {}).get("last_timestamp"))
    processing_anchor = max(value for value in (processing_declared_anchor, processing_last_timestamp) if value is not None)
    classification_anchor = _timestamp(classification["data_as_of"])
    assert classification_anchor is not None
    if classification_anchor > processing_anchor:
        return align_etf_exchange_flows_contract_to_sp_v1_4(
            _fallback(["upstream_timestamp_inconsistent"], selected_range=selected_range,
                      mode=mode, data_mode=data_mode, is_demo=is_demo),
            processing,
        )
    seconds = RANGE_SECONDS[selected_range]
    features = processing["features"]
    series = processing["series"]
    kpi_sources = {
        "etf_net_flow": (f"features.etf.period_flow_usd.{selected_range}", "USD"),
        "total_aum": ("features.etf.reported_total_aum_usd", "USD"),
        "exchange_inflow": ("features.exchange_flows.inflow_24h", "BTC"),
        "exchange_outflow": ("features.exchange_flows.outflow_24h", "BTC"),
        "exchange_balance": ("features.exchange_balances.cryptoquant_reserve", "BTC"),
        "gbtc_premium": ("features.premium_discount.gbtc_latest", "percent"),
        "exchange_flow_pressure": ("features.pressure.flow_24h", "ratio"),
    }
    kpis = {name: _financial(_at(processing, path), source_path=path, unit=unit,
                             processing_anchor=processing_anchor) for name, (path, unit) in kpi_sources.items()}
    kpis["cumulative_etf_net_flow"] = _cumulative_etf_net_flow(
        series.get("etf_cumulative_flow"), processing_anchor=processing_anchor)
    kpi_order = ("etf_net_flow", "total_aum", "cumulative_etf_net_flow", "exchange_inflow",
                 "exchange_outflow", "exchange_balance", "gbtc_premium", "exchange_flow_pressure")
    kpis = {name: kpis[name] for name in kpi_order}
    interval = "day"
    charts = {
        "etf_flow_daily": _history_chart(chart_id="etf_flow_daily", source=series.get("etf_flow_daily"), value_field="flow_usd",
            unit="USD", source_path="series.etf_flow_daily", anchor=processing_anchor, range_seconds=seconds,
            data_mode=data_mode, etf_calendar=True),
        "exchange_net_flow": _history_chart(chart_id="exchange_net_flow", source=_at(processing, "series.exchange_netflow.day"),
            value_field="netflow_total", unit="BTC", source_path="series.exchange_netflow.day",
            anchor=processing_anchor, range_seconds=seconds, data_mode=data_mode),
        "exchange_balance": _candlestick_chart(series.get("exchange_balance"),
            source_points=processing["snapshots"].get("exchanges"), anchor=processing_anchor, data_mode=data_mode),
    }
    technical_analysis = _technical_analysis(processing.get("technical_analysis"), classification.get("technical_events"), data_mode=data_mode)
    tables = {"etf_funds": _fund_table(processing["snapshots"].get("funds"), selected_range=selected_range,
                                        processing_anchor=processing_anchor)}
    source_classifications = classification["classifications"]
    direction_source = source_classifications.get("etf_flow_direction")
    directions = {name: _classification(direction_source.get(name) if isinstance(direction_source, Mapping) else None,
        source_path=f"classifications.etf_flow_direction.{name}", processing_anchor=processing_anchor) for name in CLASSIFICATION_RANGES}
    state_names = ("etf_flow_persistence", "gbtc_premium_regime", "exchange_pressure_regime",
                   "exchange_netflow_regime", "aum_reconciliation_state", "composite_capital_flow_regime", "data_confidence")
    classification_states = {"etf_flow_direction": directions, **{
        name: _classification(source_classifications.get(name), source_path=f"classifications.{name}",
                              processing_anchor=processing_anchor) for name in state_names}}
    reconciliation_source = _at(features, "provider_reconciliation", {})
    reconciliation = _json_copy({name: reconciliation_source.get(name, {}) for name in ("aum", "netflow", "exchange_balance")}, "provider_reconciliation")
    root: dict[str, Any] = {"schema": {"id": SCHEMA_ID, "version": SCHEMA_VERSION},
        "screen": {"id": FAMILY, "route": "/etf-exchange-flows", "title": "ETF & Exchange Flows", "family": FAMILY,
            "layout_contract": deepcopy(SCREEN_LAYOUT_CONTRACT)},
        "stage": "screen_contract", "version": CONTRACT_VERSION, "mode": deepcopy(mode), "data_mode": deepcopy(data_mode),
        "is_demo": deepcopy(is_demo), "context": {"generated_at": deepcopy(generated_at if generated_at is not None else processing.get("generated_at")),
            "processing_data_as_of": processing_anchor, "classification_data_as_of": classification_anchor,
            "data_as_of": None, "selected_range": selected_range, "calculation_history": "processing_precomputed_only",
            "fixture_as_of_timestamp": processing_anchor if data_mode == "synthetic" else None,
            "fixture_as_of_iso": (datetime.fromtimestamp(processing_anchor, timezone.utc).isoformat().replace("+00:00", "Z")
                                  if data_mode == "synthetic" else None),
            "data_mode": "synthetic_calibrated" if data_mode == "synthetic" else data_mode,
            "synthetic_fixture": data_mode == "synthetic", "realism_refactor_version": "realism_v1",
            "realism_note": "ETF flow uses weekday trading-session fixture; exchange series remain daily crypto series."},
        "range_selector": {"selected": selected_range, "default": "30d",
            "options": [{"id": key, "label": key.upper(), "days": RANGE_SECONDS[key] // 86_400,
                         "seconds": RANGE_SECONDS[key]} for key in DISPLAY_RANGES],
            "selector_type": "RANGE"},
        "kpis": kpis, "charts": charts, "tables": tables, "classification_states": classification_states,
        "provider_reconciliation": reconciliation,
        "operational_status": {"quality_status": None, "connection_status": "not_reported", "cache_status": "not_reported",
            "generated_at": deepcopy(generated_at if generated_at is not None else processing.get("generated_at")), "data_as_of": None},
        "provenance": {"source_contracts": {
            "processing": {"family": FAMILY, "stage": "processing", "version": UPSTREAM_VERSION,
                "data_as_of": processing_declared_anchor, "quality_status": processing["quality"].get("status")},
            "classification": {"family": FAMILY, "stage": "classification", "version": UPSTREAM_VERSION,
                "data_as_of": classification_anchor, "quality_status": classification["quality"].get("status")}},
            "field_sources": {name: {"source_path": path, "provider": kpis[name]["provider"],
                "endpoint_id": kpis[name]["endpoint_id"]} for name, (path, _) in kpi_sources.items()},
            "providers": {"primary": {"etf": ["coinglass"], "exchange_flow": ["cryptoquant"],
                "exchange_balance_kpi": ["cryptoquant"], "exchange_balance_chart": ["cryptoquant"]},
                "secondary": {"exchange_balance": ["glassnode"]}},
            "parameters": {"selected_range": selected_range, "allowed_ranges": list(DISPLAY_RANGES),
                "range_seconds": seconds, "exchange_netflow_source_interval": interval,
                "exchange_flow_kpi_window_seconds": 86_400, "cumulative_series_rebased": False}, "warnings": []},
        "quality": {}, "technical_analysis": technical_analysis, "history_contract": {
            "calculation_records": len(charts["exchange_balance"]["candles"]), "minimum_warmup_records": 200,
            "maximum_standard_indicator_period": 200,
            "all_visible_moving_averages_warm": len(charts["exchange_balance"]["candles"]) >= 200,
            "technical_indicators_precomputed": True, "hmi_recalculation": False,
            "synthetic_fixture": False, "fixture_seed": None,
            "resolution": "1d", "note": "730 daily calculation records for ETF Flow, Exchange Net Flow and Exchange Balance."}}
    statuses = _view_statuses(root, selected_range)
    available = sorted(name for name, status in statuses.items() if status == "available")
    partial = sorted(name for name, status in statuses.items() if status == "partial")
    unavailable = sorted(name for name, status in statuses.items() if status == "unavailable")
    invalid = sorted(name for name, status in statuses.items() if status == "invalid")
    usable = [name for name, status in statuses.items() if status in {"available", "partial"}]
    quality_status = "invalid" if invalid or not usable else "ok" if len(available) == len(REQUIRED_VIEWS) else "partial"
    anchors = [processing_anchor, classification_anchor]
    for name in usable:
        path = name if not name.startswith("classification_states.etf_flow_direction") else f"classification_states.etf_flow_direction.{selected_range}"
        value = _at(root, path, {})
        timestamp = _timestamp(value.get("data_as_of")) if isinstance(value, Mapping) else None
        if timestamp is not None:
            anchors.append(timestamp)
    data_as_of = min(anchors) if quality_status != "invalid" else None
    root["quality"] = {"status": quality_status, "required": list(REQUIRED_VIEWS), "optional": list(OPTIONAL_VIEWS),
        "available": available, "partial": partial, "unavailable": unavailable, "invalid": invalid,
        "data_as_of": data_as_of, "processing_status": processing["quality"].get("status"),
        "classification_status": classification["quality"].get("status"), "warnings": [], "errors": []}
    event_rows = technical_analysis.get("events", []) if isinstance(technical_analysis.get("events"), list) else []
    def _event_count(*groups: str) -> int:
        return sum(1 for item in event_rows if isinstance(item, Mapping) and item.get("event_group") in groups)
    macd_events = _event_count("macd_cross")
    adx_events = _event_count("adx_cross", "adx_di_cross")
    stochastic_events = _event_count("stochastic_cross")
    moving_events = _event_count("moving_average_cross")
    channel_events = _event_count("channel_cross")
    root["quality"]["extensions"] = {
        "exchange_balance_candlestick": {"status": "contract_ready", "target": "exchange_balance",
            "technical_analysis_only_target": True, "source_history_available": bool(charts["exchange_balance"]["source_points"]),
            "candles_fabricated": False, "processing_required": True, "hmi_recalculation": False},
        "synthetic_final_contract": {"status": "available" if data_mode == "synthetic" else "not_applicable",
            "exchange_balance_candles": len(charts["exchange_balance"]["candles"]),
            "etf_flow_daily_points": len(charts["etf_flow_daily"]["points"]),
            "exchange_net_flow_points": len(charts["exchange_net_flow"]["points"]), "hmi_recalculation": False},
        "exchange_balance_warmup_and_crosses": {"status": "available" if charts["exchange_balance"]["candles"] else "unavailable",
            "visible_candles": len(charts["exchange_balance"]["candles"]),
            "warmup_records": len(charts["exchange_balance"]["candles"]), "moving_average_events": moving_events,
            "channel_events": channel_events, "all_visible_mas_fully_populated": len(charts["exchange_balance"]["candles"]) >= 200,
            "recalculate_in_hmi": False},
        "analysis_screen_readability": {"status": "available", "target": "exchange_balance", "columns_desktop": 2,
            "chart_height_px": 320,
            "reason": "The Exchange Balance technical-analysis screen must remain readable and must not compress five indicators into one row."},
        "screen_b_arrow_audit": {"status": "available", "macd_events": macd_events, "adx_di_events": adx_events,
            "stochastic_events": stochastic_events, "stochastic_rule": "buy <=20 / sell >=80", "recalculate_in_hmi": False},
        "screen_b_arrow_contract_audit": {"status": "available", "policy": {
            "supported_indicators": ["macd", "adx", "stochastic"], "event_ids": {
                "macd": ["macd_above_signal", "macd_below_signal"],
                "adx": ["di_plus_above_di_minus", "di_plus_below_di_minus"],
                "stochastic": ["k_above_d", "k_below_d"]},
            "stochastic_gate": {"bullish": "K crosses above D while K,D <= 20",
                "bearish": "K crosses below D while K,D >= 80"}, "recalculate_in_hmi": False,
            "no_event_behavior": "show_no_arrow"}, "default_context": {"target": "exchange_balance"},
            "default_context_event_counts": {"macd": macd_events, "adx_di": adx_events, "stochastic": stochastic_events}},
        "analysis_navigation_and_summary_v1": {"status": "available",
            "back_button": deepcopy(SCREEN_LAYOUT_CONTRACT["analysis_view"]["back_button"]),
            "summary_panel": deepcopy(SCREEN_LAYOUT_CONTRACT["analysis_view"]["summary_panel"]),
            "global_view_selector_visible": False, "global_market_selector_visible": False, "hmi_computes_summary": False},
        "screen_b_arrow_audit_v2": {"status": "available", "event_counts": {"macd": macd_events, "adx": adx_events,
            "stochastic": stochastic_events}, "required_groups": ["macd", "adx", "stochastic"],
            "renderer_policy": "contract_events_only", "no_hmi_cross_calculation": True},
        "calculation_history_730_v1": {"status": "available", "series": ["etf_flow_daily", "exchange_net_flow", "exchange_balance"],
            "records_per_series": min(len(charts["etf_flow_daily"]["points"]), len(charts["exchange_net_flow"]["points"]),
                                      len(charts["exchange_balance"]["candles"])),
            "all_exchange_balance_ma_series_warm": len(charts["exchange_balance"]["candles"]) >= 200,
            "recalculate_in_hmi": False},
        "range_contract_v3": {"status": "available", "ranges": [key.upper() for key in DISPLAY_RANGES],
            "range_ids": list(DISPLAY_RANGES), "calculation_history_records": len(charts["exchange_balance"]["candles"]),
            "selector_type": "RANGE"},
        "realism_v1": {"status": "available" if data_mode == "synthetic" else "not_applicable",
            "fixture_as_of_timestamp": processing_anchor if data_mode == "synthetic" else None,
            "fixture_as_of_iso": root["context"]["fixture_as_of_iso"], "deterministic_seed": 20260807,
            "synthetic_not_live": data_mode == "synthetic", "etf_flow_records": len(charts["etf_flow_daily"]["points"]),
            "etf_calendar": "weekday_sessions", "exchange_flow_records": len(charts["exchange_net_flow"]["points"]),
            "exchange_balance_records": len(charts["exchange_balance"]["candles"]),
            "fund_table_rows": len(tables["etf_funds"]["rows"]),
            "common_as_of_contract": root["context"]["fixture_as_of_iso"]}}
    root["context"]["data_as_of"] = data_as_of
    root["operational_status"].update(quality_status=quality_status, data_as_of=data_as_of)
    output = _json_copy(root, "screen_contract")
    output = align_etf_exchange_flows_contract_to_sp_v1_4(output, processing)
    json.dumps(output, ensure_ascii=False, allow_nan=False)
    if processing_contract != processing_before or classification_contract != classification_before:
        raise RuntimeError("Contract Builder mutated an upstream contract")
    return output


def run_etf_exchange_flows_contract_builder(*, processing_contract: Mapping[str, Any],
                                            classification_contract: Mapping[str, Any], selected_range: str = "30d",
                                            generated_at: Any = None) -> dict[str, Any]:
    return build_etf_exchange_flows_contract(processing_contract=processing_contract,
        classification_contract=classification_contract, selected_range=selected_range, generated_at=generated_at)


class EtfExchangeFlowsContractBuilder:
    def build(self, *, processing_contract: Mapping[str, Any], classification_contract: Mapping[str, Any],
              selected_range: str = "30d", generated_at: Any = None) -> dict[str, Any]:
        return build_etf_exchange_flows_contract(processing_contract=processing_contract,
            classification_contract=classification_contract, selected_range=selected_range, generated_at=generated_at)

# --- Canonical Screen contract shaping ---
from copy import deepcopy

from datetime import datetime, timezone

import json

import math

from pathlib import Path

from typing import Any, Mapping, Sequence

_screen_SCHEMA_VERSION = '1.4.1-exchange-reserve-realism-v2'

_screen_CONTRACT_VERSION = '1.4.1-exchange-reserve-realism-v4'

_screen_TEMPLATE_PATH = Path(__file__).with_name('screen_template.json')

_screen_MISSING = object()

def _screen_template() -> dict[str, Any]:
    return json.loads(_screen_TEMPLATE_PATH.read_text(encoding='utf-8'))

def _screen_is_scalar(value: Any) -> bool:
    return not isinstance(value, (dict, list))

def _screen_project(reference: Any, candidate: Any=_screen_MISSING) -> Any:
    """Project runtime ETF values onto the exact current Screens shape."""
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
        identity_keys = ('metric_id', 'kpi_id', 'widget_id', 'chart_id', 'table_id', 'badge_id', 'id', 'role', 'indicator_id', 'market', 'timeframe', 'ticker')

        def reference_for(item: Any, index: int) -> Any:
            if isinstance(item, Mapping):
                for key in identity_keys:
                    value = item.get(key, _screen_MISSING)
                    if value is _screen_MISSING:
                        continue
                    for ref_item in reference:
                        if isinstance(ref_item, Mapping) and ref_item.get(key, _screen_MISSING) == value:
                            return ref_item
            return reference[index] if index < len(reference) else reference[0]
        return [_screen_project(reference_for(item, index), item) for index, item in enumerate(candidate)]
    return deepcopy(reference if candidate is _screen_MISSING else candidate)

def _screen_iso(timestamp: int | None) -> str | None:
    if type(timestamp) is not int:
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace('+00:00', 'Z')

def _screen_finite(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (not math.isfinite(value)):
        return None
    return 0.0 if value == 0 else value

def _screen_compact_btc(value: Any) -> str:
    value = _screen_finite(value)
    if value is None:
        return '—'
    absolute = abs(float(value))
    if absolute >= 1000000:
        return f'{value / 1000000:.3f}M BTC'
    if absolute >= 1000:
        return f'{value / 1000:.2f}K BTC'
    return f'{value:.2f} BTC'

def _screen_day_rows(processing: Mapping[str, Any], name: str) -> list[Mapping[str, Any]]:
    series = processing.get('series', {})
    if not isinstance(series, Mapping):
        return []
    root = series.get(name, {})
    rows = root.get('day', []) if isinstance(root, Mapping) else []
    return [row for row in rows if isinstance(row, Mapping) and type(row.get('timestamp')) is int]

def _screen_exchange_flow_chart(reference: Mapping[str, Any], processing: Mapping[str, Any]) -> dict[str, Any]:
    inflow = {int(row['timestamp']): _screen_finite(row.get('inflow_total')) for row in _screen_day_rows(processing, 'exchange_inflow')}
    outflow = {int(row['timestamp']): _screen_finite(row.get('outflow_total')) for row in _screen_day_rows(processing, 'exchange_outflow')}
    reported = {int(row['timestamp']): _screen_finite(row.get('netflow_total')) for row in _screen_day_rows(processing, 'exchange_netflow')}
    timestamps = sorted(set(inflow) | set(outflow))[-730:]
    is_demo = bool(processing.get('is_demo'))

    def points(kind: str) -> list[dict[str, Any]]:
        result = []
        for timestamp in timestamps:
            if kind == 'inflow':
                value, provider, endpoint = (inflow.get(timestamp), 'cryptoquant', 'exchange_inflow')
            elif kind == 'outflow':
                value, provider, endpoint = (outflow.get(timestamp), 'cryptoquant', 'exchange_outflow')
            else:
                value = reported.get(timestamp)
                if value is None and inflow.get(timestamp) is not None and (outflow.get(timestamp) is not None):
                    value = float(inflow[timestamp]) - float(outflow[timestamp])
                    provider, endpoint = ('calculated', None)
                else:
                    provider, endpoint = ('calculated', None)
            if value is None:
                continue
            result.append({'timestamp': timestamp, 'value': value, 'provider': provider, 'endpoint_id': endpoint, 'is_synthetic': is_demo})
        return result
    dynamic = {'chart_id': 'exchange_net_flow', 'title': 'Exchange Inflow / Outflow / Net Flow', 'status': 'available' if timestamps else 'unavailable', 'unit': 'BTC', 'chart_type': 'multi_series', 'data_mode': 'synthetic_emulator' if is_demo else 'live_provider', 'processing_contract_target': True, 'real_market_calculation': not is_demo, 'hmi_recalculate': False, 'series': [{'id': 'inflow', 'label': 'Inflow', 'representation': 'bar', 'points': points('inflow')}, {'id': 'outflow', 'label': 'Outflow', 'representation': 'bar', 'points': points('outflow')}, {'id': 'net_flow', 'label': 'Net Flow', 'representation': 'line', 'points': points('net_flow')}], 'data_as_of': max(timestamps) if timestamps else None, 'warnings': []}
    return _screen_project(reference, dynamic)

def _screen_reserve_chart(reference: Mapping[str, Any], processing: Mapping[str, Any]) -> dict[str, Any]:
    rows = _screen_day_rows(processing, 'exchange_reserve')
    is_demo = bool(processing.get('is_demo'))
    data_mode = 'synthetic_emulator' if is_demo else 'live_provider'
    points = [{'timestamp': int(row['timestamp']), 'value': _screen_finite(row.get('reserve')), 'provider': 'cryptoquant', 'endpoint_id': 'exchange_reserve', 'is_synthetic': is_demo, 'data_mode': data_mode} for row in rows if _screen_finite(row.get('reserve')) is not None]
    series_root = processing.get('series', {}) if isinstance(processing.get('series'), Mapping) else {}
    candles = series_root.get('exchange_balance', [])
    candles = [deepcopy(dict(row)) for row in candles if isinstance(row, Mapping)] if isinstance(candles, list) else []
    dynamic = {'chart_id': 'exchange_balance', 'status': 'available' if points else 'unavailable', 'reason': None if points else 'source_series_empty', 'unit': 'BTC', 'source_path': 'series.exchange_reserve.day', 'data_as_of': points[-1]['timestamp'] if points else None, 'warnings': [], 'chart_type': 'line', 'preferred_representation': 'line_area', 'title': 'Exchange Reserve / Balance (BTC)', 'source_points': len(points), 'synthetic_history': {'status': 'available' if is_demo and points else 'not_applicable' if not is_demo else 'unavailable', 'points': len(points), 'resolution': '1d', 'purpose': 'contract_hmi_validation' if is_demo else 'live_provider_history'}, 'calculation_history': {'record_count': len(points), 'candles': candles, 'purpose': 'processing_history_only_not_candlestick_hmi'}, 'records_available': len(points), 'records_returned': len(points), 'points': points, 'technical_analysis_allowed': False, 'analysis_note': 'Use the daily reserve series directly. HMI must not fabricate or smooth reserve values.', 'data_mode': data_mode, 'real_market_calculation': not is_demo, 'realism_note': 'provider-shaped emulator reserve series used for contract/HMI validation' if is_demo else 'runtime CryptoQuant exchange reserve; Processing publishes the provider series without HMI reconstruction', 'processing_contract_target': True, 'hmi_recalculate': False}
    return _screen_project(reference, dynamic)

def _screen_capital_flow(reference: Mapping[str, Any], processing: Mapping[str, Any]) -> dict[str, Any]:
    native = processing.get('capital_flow_analysis', {})
    native = native if isinstance(native, Mapping) else {}
    is_demo = bool(processing.get('is_demo'))
    support = native.get('supporting_series', {}) if isinstance(native.get('supporting_series'), Mapping) else {}
    timestamps = deepcopy(support.get('timestamps', [])) if isinstance(support.get('timestamps'), list) else []
    indicators: dict[str, Any] = {}
    native_indicators = native.get('indicators', {}) if isinstance(native.get('indicators'), Mapping) else {}
    for indicator_id, ref_indicator in reference.get('indicators', {}).items():
        candidate = native_indicators.get(indicator_id, {})
        candidate = deepcopy(dict(candidate)) if isinstance(candidate, Mapping) else {}
        candidate.update({'data_mode': 'synthetic_emulator' if is_demo else 'live_provider', 'processing_contract_target': True, 'real_market_calculation': not is_demo, 'hmi_recalculate': False})
        candidate['thresholds'] = deepcopy(ref_indicator.get('thresholds', []))
        indicators[indicator_id] = _screen_project(ref_indicator, candidate)

    def support_package(values: Any) -> dict[str, Any]:
        return {'timestamps': timestamps, 'values': deepcopy(values) if isinstance(values, list) else [], 'data_mode': 'synthetic_emulator' if is_demo else 'live_provider'}
    dynamic = {'analysis_id': 'etf_exchange_capital_flow_native_v1', 'contract_family': 'etf_exchange_flows', 'target_title': 'ETF & Exchange Capital Analytics', 'status': native.get('status', 'available' if indicators else 'unavailable'), 'recalculate_in_hmi': False, 'source_resolution': '1D', 'selector_contract': deepcopy(reference.get('selector_contract', {})), 'data_contract': {'data_mode': 'synthetic_emulator' if is_demo else 'live_provider', 'processing_contract_target': True, 'real_market_calculation': not is_demo, 'hmi_recalculate': False, 'rule': 'Market API provides primitive flows/reserves/price; Processing derives native capital-flow analytics; HMI only renders JSON.'}, 'indicators': indicators, 'supporting_series': {'btc_price_proxy': support_package(support.get('btc_price')), 'exchange_inflow_proxy': support_package(support.get('exchange_inflow')), 'exchange_outflow_proxy': support_package(support.get('exchange_outflow'))}}
    return _screen_project(reference, dynamic)

def align_etf_exchange_flows_contract_to_sp_v1_4(candidate: Mapping[str, Any], processing: Mapping[str, Any]) -> dict[str, Any]:
    reference = _screen_template()
    dynamic = deepcopy(dict(candidate))
    dynamic.pop('technical_analysis', None)
    dynamic['schema'] = {'id': 'trad_elatin.etf_exchange_flows.screen.v1', 'version': _screen_SCHEMA_VERSION}
    dynamic['version'] = _screen_CONTRACT_VERSION
    context = deepcopy(dict(dynamic.get('context', {})))
    processing_as_of = processing.get('data_as_of') if type(processing.get('data_as_of')) is int else context.get('processing_data_as_of')
    is_demo = bool(processing.get('is_demo'))
    reserve_rows = _screen_day_rows(processing, 'exchange_reserve')
    reserve_values = [_screen_finite(row.get('reserve')) for row in reserve_rows]
    reserve_values = [float(value) for value in reserve_values if value is not None]
    recent = reserve_values[-30:]
    context.update({'generated_at': processing.get('generated_at', context.get('generated_at')), 'processing_data_as_of': processing_as_of, 'data_as_of': processing_as_of, 'data_mode': 'synthetic_calibrated' if is_demo else processing.get('data_mode', 'live'), 'synthetic_fixture': False, 'fixture_as_of_timestamp': None, 'fixture_as_of_iso': None, 'realism_refactor_version': 'exchange_realism_v2' if is_demo else 'runtime_provider_v1', 'realism_note': 'provider-shaped ETF/exchange-flow emulator data; native capital-flow analytics are computed in Processing' if is_demo else 'runtime ETF/exchange-flow provider data; native capital-flow analytics are computed in Processing', 'analysis_contract': 'capital_flow_analysis_v1', 'hmi_calculation': False, 'screen_revision': 'ETF_NATIVE_CAPITAL_FLOW_B_V1', 'exchange_reserve_emulator_policy': {'data_mode': 'synthetic_emulator' if is_demo else 'live_provider', 'points': len(reserve_rows), 'resolution': '1d', 'latest_value_btc': reserve_values[-1] if reserve_values else None, 'last_30d_min_btc': min(recent) if recent else None, 'last_30d_max_btc': max(recent) if recent else None, 'last_30d_range_btc': max(recent) - min(recent) if recent else None, 'real_market_calculation': not is_demo, 'purpose': 'runtime emulator realism and contract validation' if is_demo else 'runtime provider reserve contract'}})
    dynamic['context'] = context
    charts = deepcopy(dict(dynamic.get('charts', {})))
    charts['exchange_net_flow'] = _screen_exchange_flow_chart(reference['charts']['exchange_net_flow'], processing)
    charts['exchange_balance'] = _screen_reserve_chart(reference['charts']['exchange_balance'], processing)
    dynamic['charts'] = charts
    kpis = deepcopy(dict(dynamic.get('kpis', {})))
    if isinstance(kpis.get('exchange_balance'), Mapping):
        kpis['exchange_balance'] = deepcopy(dict(kpis['exchange_balance']))
        kpis['exchange_balance']['display_value'] = _screen_compact_btc(kpis['exchange_balance'].get('value'))
    dynamic['kpis'] = kpis
    dynamic['capital_flow_analysis'] = _screen_capital_flow(reference['capital_flow_analysis'], processing)
    calculation_records = max(len(charts.get('etf_flow_daily', {}).get('calculation_history', {}).get('points', [])) if isinstance(charts.get('etf_flow_daily'), Mapping) else 0, len(_screen_day_rows(processing, 'exchange_inflow')), len(reserve_rows))
    dynamic['history_contract'] = {'calculation_records': calculation_records, 'minimum_analysis_history_records': 90, 'hmi_recalculation': False, 'synthetic_fixture': False, 'fixture_seed': None, 'resolution': '1d', 'note': reference.get('history_contract', {}).get('note', '730 daily records support ETF, exchange-flow, reserve and native capital-flow analytics.')}
    quality = deepcopy(dict(dynamic.get('quality', {})))
    extensions = deepcopy(dict(quality.get('extensions', {})))
    extensions = {key: deepcopy(value) for key, value in reference.get('quality', {}).get('extensions', {}).items()}
    extensions['native_capital_flow_screen_b_v1'] = {'status': 'available' if dynamic['capital_flow_analysis'].get('status') in {'available', 'ok'} else dynamic['capital_flow_analysis'].get('status'), 'indicators': list(reference['capital_flow_analysis']['indicators']), 'classical_price_ta_removed': True, 'exchange_reserve_representation': 'line_area', 'hmi_recalculation': False, 'data_mode': 'synthetic_emulator' if is_demo else 'live_provider'}
    extensions['exchange_reserve_realism_v2'] = {'status': 'synthetic_emulator' if is_demo else 'live_provider', 'records': len(reserve_rows), 'behavior': 'provider_series_no_hmi_fabrication', 'display_policy': 'local_y_range_not_zero_baseline'}
    extensions['exchange_reserve_realism_v4'] = {'records': len(reserve_rows), 'unique_timestamps': len({int(row['timestamp']) for row in reserve_rows}), 'last_30d_range_btc': max(recent) - min(recent) if recent else None, 'flat_line_detected': bool(recent and max(recent) == min(recent))}
    quality['extensions'] = extensions
    dynamic['quality'] = quality
    aligned = _screen_project(reference, dynamic)
    aligned.pop('technical_analysis', None)
    json.dumps(aligned, ensure_ascii=False, allow_nan=False)
    return aligned
