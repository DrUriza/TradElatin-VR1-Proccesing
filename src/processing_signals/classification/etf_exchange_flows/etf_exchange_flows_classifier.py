"""Pure Classification v0.1 for frozen ETF exchange-flow Processing output."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from typing import Any

FAMILY = "etf_exchange_flows"
VERSION = "0.1"
RANGES = ("1d", "7d", "30d")
STATUSES = {"available", "partial", "unavailable", "invalid"}

ETF_DEADBAND_USD = 0.0
GBTC_PREMIUM_THRESHOLD_PERCENT = 0.5
GBTC_DISCOUNT_THRESHOLD_PERCENT = -0.5
PRESSURE_NEUTRAL_THRESHOLD = 0.10
PRESSURE_STRONG_THRESHOLD = 0.25
NETFLOW_DEADBAND_BTC = 0.0
AUM_ALIGNED_MAX_PERCENT = 2.0
AUM_WATCH_MAX_PERCENT = 5.0

DEFAULT_PARAMETERS = {
    "etf_deadband_usd": ETF_DEADBAND_USD,
    "gbtc_premium_threshold_percent": GBTC_PREMIUM_THRESHOLD_PERCENT,
    "gbtc_discount_threshold_percent": GBTC_DISCOUNT_THRESHOLD_PERCENT,
    "pressure_neutral_threshold": PRESSURE_NEUTRAL_THRESHOLD,
    "pressure_strong_threshold": PRESSURE_STRONG_THRESHOLD,
    "netflow_deadband_btc": NETFLOW_DEADBAND_BTC,
    "aum_aligned_max_percent": AUM_ALIGNED_MAX_PERCENT,
    "aum_watch_max_percent": AUM_WATCH_MAX_PERCENT,
}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _timestamp(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        if isinstance(value, float) and not value.is_integer():
            return None
        return int(value) if value > 0 else None
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            result = int(parsed.timestamp())
            return result if result > 0 else None
        except ValueError:
            return None
    return None


def _strict_data_as_of(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _validate_source_data_as_of(*, feature_data_as_of: Any,
                                processing_data_as_of: Any) -> tuple[bool, int | None, int | None]:
    """Validate a feature anchor against Processing without clipping either value."""
    processing_timestamp = _strict_data_as_of(processing_data_as_of)
    feature_timestamp = _strict_data_as_of(feature_data_as_of)
    valid = (feature_timestamp is not None and processing_timestamp is not None and
             feature_timestamp <= processing_timestamp)
    return valid, feature_timestamp, processing_timestamp


def _inconsistent_timestamp_evidence(value: Any, processing_timestamp: int | None) -> dict[str, Any]:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return {"source_data_as_of_type": type(value).__name__,
                "processing_data_as_of": processing_timestamp}
    return {"source_data_as_of": deepcopy(value), "processing_data_as_of": processing_timestamp}


def _json_value(value: Any, path: str = "root") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"invalid_classification_input:{path}")
            _json_value(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _json_value(item, f"{path}[{index}]")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"invalid_classification_input:{path}")
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise ValueError(f"invalid_classification_input:{path}")


def _parameters(overrides: Mapping[str, Any] | None) -> dict[str, float]:
    if overrides is not None and not isinstance(overrides, Mapping):
        raise ValueError("invalid_classification_input:parameters")
    supplied = dict(overrides or {})
    unknown = set(supplied) - set(DEFAULT_PARAMETERS)
    if unknown:
        raise ValueError(f"invalid_classification_input:parameters.{sorted(unknown)[0]}")
    result = deepcopy(DEFAULT_PARAMETERS)
    for key, value in supplied.items():
        numeric = _number(value)
        if numeric is None:
            raise ValueError(f"invalid_classification_input:parameters.{key}")
        result[key] = numeric
    if not 0 <= result["pressure_neutral_threshold"] < result["pressure_strong_threshold"] <= 1:
        raise ValueError("invalid_classification_input:parameters.pressure_thresholds")
    if not result["gbtc_discount_threshold_percent"] <= 0 <= result["gbtc_premium_threshold_percent"]:
        raise ValueError("invalid_classification_input:parameters.gbtc_thresholds")
    if not 0 <= result["aum_aligned_max_percent"] <= result["aum_watch_max_percent"]:
        raise ValueError("invalid_classification_input:parameters.aum_thresholds")
    if result["etf_deadband_usd"] < 0 or result["netflow_deadband_btc"] < 0:
        raise ValueError("invalid_classification_input:parameters.deadbands")
    return result


def _wrapper(*, state: str | None, status: str, reason: str | None, data_as_of: int | None,
             evidence: Mapping[str, Any], source_features: list[str], parameters: Mapping[str, Any],
             warnings: list[str] | None = None) -> dict[str, Any]:
    if status in {"unavailable", "invalid"}:
        state = None
    return {"state": state, "status": status, "reason": reason, "data_as_of": data_as_of,
            "evidence": deepcopy(dict(evidence)), "source_features": list(source_features),
            "parameters": deepcopy(dict(parameters)), "warnings": sorted(set(warnings or []))}


def _source(feature: Any, path: str, unit: str, generated_timestamp: int) -> tuple[str, float | None, int | None, str | None, list[str]]:
    if not isinstance(feature, Mapping):
        return "unavailable", None, None, "processing_feature_not_available", []
    status = feature.get("status")
    if status not in STATUSES:
        return "invalid", None, None, "processing_invalid", []
    warnings = [item for item in feature.get("warnings", []) if isinstance(item, str)] if isinstance(feature.get("warnings", []), list) else []
    if status in {"unavailable", "invalid"}:
        reason = feature.get("reason") if isinstance(feature.get("reason"), str) else (
            "processing_unavailable" if status == "unavailable" else "processing_invalid")
        return status, None, None, reason, warnings
    value = _number(feature.get("value"))
    if value is None:
        return "invalid", None, None, "missing_required_value", warnings
    if feature.get("unit") != unit:
        return "invalid", None, None, "invalid_unit", warnings
    data_as_of = _strict_data_as_of(feature.get("data_as_of"))
    if data_as_of is None:
        return "invalid", None, None, "processing_invalid", warnings
    if data_as_of > generated_timestamp:
        return "invalid", None, None, "future_timestamp", sorted(set([*warnings, "future_timestamp"]))
    return status, value, data_as_of, "partial_source_feature" if status == "partial" else None, warnings


def _atom(feature: Any, *, path: str, unit: str, generated_timestamp: int,
          parameters: Mapping[str, Any], state_builder: Any, validator: Any = None,
          extra_evidence: Mapping[str, Any] | None = None, extra_warnings: list[str] | None = None,
          processing_data_as_of: Any = None) -> dict[str, Any]:
    if isinstance(feature, Mapping) and feature.get("status") in {"available", "partial"}:
        # Processing.data_as_of is the conservative shared anchor across required
        # features, not an upper bound for each independently fresher feature.
        source_value = feature.get("data_as_of")
        if _strict_data_as_of(source_value) is None:
            return _wrapper(state=None, status="invalid", reason="processing_timestamp_inconsistent", data_as_of=None,
                            evidence=_inconsistent_timestamp_evidence(source_value, _strict_data_as_of(processing_data_as_of)),
                            source_features=[path], parameters=parameters,
                            warnings=["processing_timestamp_inconsistent"])
    status, value, data_as_of, reason, warnings = _source(feature, path, unit, generated_timestamp)
    evidence = {"value": value, "unit": unit, "source_status": feature.get("status") if isinstance(feature, Mapping) else None}
    evidence.update(deepcopy(dict(extra_evidence or {})))
    if status in {"unavailable", "invalid"}:
        return _wrapper(state=None, status=status, reason=reason, data_as_of=None, evidence=evidence,
                        source_features=[path], parameters=parameters, warnings=[*warnings, *(extra_warnings or [])])
    if validator is not None and not validator(value):
        return _wrapper(state=None, status="invalid", reason="processing_invalid", data_as_of=None,
                        evidence=evidence, source_features=[path], parameters=parameters, warnings=warnings)
    return _wrapper(state=state_builder(value), status=status, reason=reason, data_as_of=data_as_of,
                    evidence=evidence, source_features=[path], parameters=parameters,
                    warnings=[*warnings, *(extra_warnings or [])])


def classify_etf_flow_direction(feature: Any, *, range_id: str, generated_timestamp: int,
                                parameters: Mapping[str, float], processing_data_as_of: Any = None) -> dict[str, Any]:
    path = f"features.etf.period_flow_usd.{range_id}"
    deadband = parameters["etf_deadband_usd"]
    def state(value: float) -> str:
        return "inflow" if value > deadband else "outflow" if value < -deadband else "neutral"
    return _atom(feature, path=path, unit="USD", generated_timestamp=generated_timestamp,
                 parameters={"deadband_usd": deadband, "method": "sign_only"}, state_builder=state,
                 processing_data_as_of=processing_data_as_of)


def classify_etf_flow_persistence(directions: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    required = [directions.get(name, {}) for name in ("1d", "7d", "30d")]
    source_features = [f"features.etf.period_flow_usd.{name}" for name in RANGES]
    invalid = any(item.get("status") == "invalid" for item in required)
    usable = all(item.get("status") in {"available", "partial"} and item.get("state") is not None for item in required)
    if invalid:
        temporal = any(item.get("reason") == "processing_timestamp_inconsistent" for item in required)
        reason = "processing_timestamp_inconsistent" if temporal else "processing_invalid"
        return _wrapper(state=None, status="invalid", reason=reason, data_as_of=None,
                        evidence={}, source_features=source_features, parameters={},
                        warnings=[reason] if temporal else [])
    if not usable:
        return _wrapper(state=None, status="unavailable", reason="insufficient_classification_evidence", data_as_of=None,
                        evidence={}, source_features=source_features, parameters={}, warnings=[])
    one, seven, thirty = (item["state"] for item in required)
    if (one, seven, thirty) == ("inflow", "inflow", "inflow"):
        state = "persistent_inflow"
    elif thirty == "inflow" and one == "outflow":
        state = "inflow_reversal"
    elif thirty == "inflow":
        state = "inflow_weakening"
    elif (one, seven, thirty) == ("outflow", "outflow", "outflow"):
        state = "persistent_outflow"
    elif thirty == "outflow" and one == "inflow":
        state = "outflow_reversal"
    elif thirty == "outflow":
        state = "outflow_weakening"
    elif (one, seven, thirty) == ("neutral", "neutral", "neutral"):
        state = "neutral"
    else:
        state = "mixed"
    partial = any(item["status"] == "partial" for item in required)
    timestamps = [item["data_as_of"] for item in required]
    return _wrapper(state=state, status="partial" if partial else "available",
                    reason="partial_source_feature" if partial else None, data_as_of=min(timestamps),
                    evidence={name: directions[name]["state"] for name in RANGES}, source_features=source_features,
                    parameters={"decision_windows": ["1d", "7d", "30d"], "context_window": "30d"}, warnings=[])


def classify_gbtc_premium_regime(feature: Any, *, generated_timestamp: int,
                                 parameters: Mapping[str, float], processing_data_as_of: Any = None) -> dict[str, Any]:
    low, high = parameters["gbtc_discount_threshold_percent"], parameters["gbtc_premium_threshold_percent"]
    def state(value: float) -> str:
        return "premium" if value >= high else "discount" if value <= low else "near_par"
    return _atom(feature, path="features.premium_discount.gbtc_latest", unit="percent",
                 generated_timestamp=generated_timestamp, parameters={"premium_threshold_percent": high,
                 "discount_threshold_percent": low}, state_builder=state,
                 processing_data_as_of=processing_data_as_of)


def classify_exchange_pressure_regime(feature: Any, *, generated_timestamp: int,
                                      parameters: Mapping[str, float], processing_data_as_of: Any = None) -> dict[str, Any]:
    neutral, strong = parameters["pressure_neutral_threshold"], parameters["pressure_strong_threshold"]
    def state(value: float) -> str:
        if value >= strong:
            return "strong_exchange_inflow"
        if value > neutral:
            return "exchange_inflow"
        if value >= -neutral:
            return "balanced"
        if value > -strong:
            return "exchange_outflow"
        return "strong_exchange_outflow"
    return _atom(feature, path="features.pressure.flow_24h", unit="ratio", generated_timestamp=generated_timestamp,
                 parameters={"neutral_threshold": neutral, "strong_threshold": strong}, state_builder=state,
                 validator=lambda value: -1 <= value <= 1, processing_data_as_of=processing_data_as_of)


def classify_exchange_netflow_regime(feature: Any, reconciliation: Any, *, generated_timestamp: int,
                                     parameters: Mapping[str, float], processing_data_as_of: Any = None) -> dict[str, Any]:
    deadband = parameters["netflow_deadband_btc"]
    warnings = []
    if isinstance(reconciliation, Mapping):
        reasons = [item.get("reason") for item in reconciliation.values() if isinstance(item, Mapping)]
        if reconciliation.get("reason") == "anchors_not_aligned" or "anchors_not_aligned" in reasons:
            warnings.append("anchors_not_aligned")
    def state(value: float) -> str:
        return "positive_netflow" if value > deadband else "negative_netflow" if value < -deadband else "balanced_netflow"
    calculated = reconciliation.get("calculated", {}) if isinstance(reconciliation, Mapping) else {}
    evidence = {"calculated_value": calculated.get("value") if isinstance(calculated, Mapping) else None,
                "calculated_status": calculated.get("status") if isinstance(calculated, Mapping) else None}
    return _atom(feature, path="features.exchange_flows.netflow_24h_reported", unit="BTC",
                 generated_timestamp=generated_timestamp, parameters={"deadband_btc": deadband}, state_builder=state,
                 extra_evidence=evidence, extra_warnings=warnings,
                 processing_data_as_of=processing_data_as_of)


def classify_aum_reconciliation_state(feature: Any, *, generated_timestamp: int,
                                      parameters: Mapping[str, float], processing_data_as_of: Any = None) -> dict[str, Any]:
    aligned, watch = parameters["aum_aligned_max_percent"], parameters["aum_watch_max_percent"]
    def state(value: float) -> str:
        return "aligned" if abs(value) <= aligned else "watch" if abs(value) <= watch else "divergent"
    return _atom(feature, path="features.provider_reconciliation.aum.difference_percent", unit="percent",
                 generated_timestamp=generated_timestamp, parameters={"aligned_max_percent": aligned,
                 "watch_max_percent": watch}, state_builder=state,
                 processing_data_as_of=processing_data_as_of)


def classify_composite_capital_flow_regime(direction: Mapping[str, Any], pressure: Mapping[str, Any],
                                            persistence: Mapping[str, Any], netflow: Mapping[str, Any]) -> dict[str, Any]:
    pillars = (direction, pressure)
    sources = ["classifications.etf_flow_direction.1d", "classifications.exchange_pressure_regime",
               "classifications.etf_flow_persistence", "classifications.exchange_netflow_regime"]
    if any(item.get("status") == "invalid" for item in pillars):
        temporal = any(item.get("reason") == "processing_timestamp_inconsistent" for item in pillars)
        reason = "processing_timestamp_inconsistent" if temporal else "processing_invalid"
        return _wrapper(state=None, status="invalid", reason=reason, data_as_of=None,
                        evidence={}, source_features=sources, parameters={},
                        warnings=[reason] if temporal else [])
    if any(item.get("status") not in {"available", "partial"} or item.get("state") is None for item in pillars):
        return _wrapper(state=None, status="unavailable", reason="insufficient_classification_evidence", data_as_of=None,
                        evidence={}, source_features=sources, parameters={}, warnings=[])
    etf, exchange = direction["state"], pressure["state"]
    persistence_usable = persistence.get("status") in {"available", "partial"} and persistence.get("state") is not None
    persistence_state = persistence.get("state")
    if not persistence_usable:
        state, status, reason = "mixed", "partial", "insufficient_classification_evidence"
    elif etf == "inflow" and exchange in {"exchange_outflow", "strong_exchange_outflow"} and persistence_state in {"persistent_inflow", "inflow_weakening"}:
        state, status, reason = "accumulation", "available", None
    elif etf == "outflow" and exchange in {"exchange_inflow", "strong_exchange_inflow"} and persistence_state in {"persistent_outflow", "outflow_weakening"}:
        state, status, reason = "distribution", "available", None
    elif etf == "neutral" and exchange == "balanced":
        state, status, reason = "neutral", "available", None
    else:
        state, status, reason = "mixed", "available", None
    if status == "available" and any(item.get("status") == "partial" for item in (*pillars, persistence)):
        status, reason = "partial", "partial_source_feature"
    warnings = []
    net_state = netflow.get("state")
    pressure_sign = 1 if "inflow" in exchange else -1 if "outflow" in exchange else 0
    net_sign = 1 if net_state == "positive_netflow" else -1 if net_state == "negative_netflow" else 0
    if pressure_sign and net_sign and pressure_sign != net_sign:
        warnings.append("netflow_confirmation_mismatch")
    timestamps = [item.get("data_as_of") for item in (*pillars, persistence) if item.get("status") in {"available", "partial"}]
    evidence = {"etf_flow_direction": etf, "exchange_pressure_regime": exchange,
                "etf_flow_persistence": persistence_state, "exchange_netflow_regime": net_state}
    return _wrapper(state=state, status=status, reason=reason, data_as_of=min(timestamps), evidence=evidence,
                    source_features=sources, parameters={}, warnings=warnings)


def classify_data_confidence(processing_quality: Mapping[str, Any], direction: Mapping[str, Any],
                             pressure: Mapping[str, Any], anomalies: Mapping[str, Any]) -> dict[str, Any]:
    sources = ["quality", "features.etf.period_flow_usd.1d", "features.pressure.flow_24h", "provenance.anomalies"]
    if processing_quality.get("status") == "invalid":
        return _wrapper(state=None, status="invalid", reason="processing_invalid", data_as_of=None,
                        evidence={}, source_features=sources, parameters={}, warnings=[])
    pillars = (direction, pressure)
    if any(item.get("status") == "invalid" for item in pillars):
        temporal = any(item.get("reason") == "processing_timestamp_inconsistent" for item in pillars)
        reason = "processing_timestamp_inconsistent" if temporal else "processing_invalid"
        return _wrapper(state=None, status="invalid", reason=reason, data_as_of=None,
                        evidence={}, source_features=sources, parameters={},
                        warnings=[reason] if temporal else [])
    if any(item.get("status") not in {"available", "partial"} for item in pillars):
        return _wrapper(state=None, status="unavailable", reason="insufficient_classification_evidence", data_as_of=None,
                        evidence={}, source_features=sources, parameters={}, warnings=[])
    degradations = sum(item.get("status") == "partial" for item in pillars)
    material = []
    for item in pillars:
        material.extend(item.get("warnings", []))
    if processing_quality.get("status") == "partial":
        material.append("processing_quality_partial")
    if isinstance(anomalies, Mapping):
        if anomalies.get("future_records_excluded", 0):
            material.append("future_timestamp")
        if anomalies.get("negative_observations_rejected", 0):
            material.append("negative_flow_observation")
    degradation_count = degradations + len(set(material))
    state = "high" if processing_quality.get("status") == "ok" and degradation_count == 0 else "medium" if degradation_count == 1 else "low"
    status = "available" if state == "high" else "partial"
    reason = None if state == "high" else "partial_source_feature" if state == "medium" else "insufficient_coverage"
    timestamps = [item["data_as_of"] for item in pillars]
    evidence = {"processing_quality_status": processing_quality.get("status"), "degradation_count": degradation_count,
                "future_records_excluded": anomalies.get("future_records_excluded", 0) if isinstance(anomalies, Mapping) else 0,
                "negative_observations_rejected": anomalies.get("negative_observations_rejected", 0) if isinstance(anomalies, Mapping) else 0}
    return _wrapper(state=state, status=status, reason=reason, data_as_of=min(timestamps), evidence=evidence,
                    source_features=sources, parameters={}, warnings=material)


def _at(root: Mapping[str, Any], path: str) -> Any:
    value: Any = root
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def _validate_contract(contract: Any) -> Mapping[str, Any]:
    if not isinstance(contract, Mapping):
        raise ValueError("invalid_classification_input:root")
    _json_value(contract)
    for field, expected in (("family", FAMILY), ("stage", "processing"), ("version", VERSION)):
        if contract.get(field) != expected:
            raise ValueError(f"invalid_classification_input:{field}")
    for field in ("features", "quality", "provenance"):
        if not isinstance(contract.get(field), Mapping):
            raise ValueError(f"invalid_classification_input:{field}")
    if contract["quality"].get("status") not in {"ok", "partial", "invalid"}:
        raise ValueError("invalid_classification_input:quality.status")
    return contract


def _blocked(contract: Mapping[str, Any], generated_at: Any, parameters: Mapping[str, float]) -> dict[str, Any]:
    atom = _wrapper(state=None, status="invalid", reason="processing_invalid", data_as_of=None,
                    evidence={}, source_features=[], parameters={}, warnings=[])
    directions = {name: deepcopy(atom) for name in RANGES}
    classifications = {"etf_flow_direction": directions, "etf_flow_persistence": deepcopy(atom),
        "gbtc_premium_regime": deepcopy(atom), "exchange_pressure_regime": deepcopy(atom),
        "exchange_netflow_regime": deepcopy(atom), "aum_reconciliation_state": deepcopy(atom),
        "composite_capital_flow_regime": deepcopy(atom), "data_confidence": deepcopy(atom)}
    return {"family": FAMILY, "stage": "classification", "version": VERSION, "mode": contract.get("mode"),
        "data_mode": contract.get("data_mode"), "is_demo": contract.get("is_demo"), "generated_at": deepcopy(generated_at),
        "data_as_of": None, "classifications": classifications, "technical_events": {"events": [], "indexes": {}},
        "provenance": {"source_family": FAMILY,
        "source_stage": "processing", "source_version": VERSION, "source_processing_data_as_of": contract.get("data_as_of"),
        "parameters": deepcopy(dict(parameters)), "warnings": []}, "quality": {"status": "invalid", "required": [],
        "optional": [], "available": [], "partial": [], "unavailable": [], "invalid": ["processing"],
        "data_as_of": None, "warnings": [], "errors": ["processing_invalid"]}}


def classify_etf_exchange_flows(*, processing_contract: Mapping[str, Any], generated_at: Any = None,
                                parameters: Mapping[str, Any] | None = None) -> dict[str, Any]:
    contract = _validate_contract(processing_contract)
    configured = _parameters(parameters)
    effective_generated_at = generated_at if generated_at is not None else contract.get("generated_at")
    generated_timestamp = _timestamp(effective_generated_at)
    if generated_timestamp is None:
        raise ValueError("invalid_classification_input:generated_at")
    quality = contract["quality"]
    # A provider can legitimately yield no usable ETF anchor during startup.
    # Processing already represents that condition through quality.status;
    # Classification must emit a blocked contract instead of aborting startup.
    if quality.get("status") == "invalid":
        result = _blocked(contract, effective_generated_at, configured)
        json.dumps(result, allow_nan=False)
        return result
    processing_data_as_of = _strict_data_as_of(contract.get("data_as_of"))
    if processing_data_as_of is None:
        result = _blocked(contract, effective_generated_at, configured)
        json.dumps(result, allow_nan=False)
        return result
    directions = {name: classify_etf_flow_direction(_at(contract, f"features.etf.period_flow_usd.{name}"),
        range_id=name, generated_timestamp=generated_timestamp, parameters=configured,
        processing_data_as_of=processing_data_as_of) for name in RANGES}
    persistence = classify_etf_flow_persistence(directions)
    premium = classify_gbtc_premium_regime(_at(contract, "features.premium_discount.gbtc_latest"),
        generated_timestamp=generated_timestamp, parameters=configured, processing_data_as_of=processing_data_as_of)
    pressure = classify_exchange_pressure_regime(_at(contract, "features.pressure.flow_24h"),
        generated_timestamp=generated_timestamp, parameters=configured, processing_data_as_of=processing_data_as_of)
    reconciliation = _at(contract, "features.provider_reconciliation.netflow")
    netflow = classify_exchange_netflow_regime(_at(contract, "features.exchange_flows.netflow_24h_calculated"), reconciliation,
        generated_timestamp=generated_timestamp, parameters=configured, processing_data_as_of=processing_data_as_of)
    aum = classify_aum_reconciliation_state(_at(contract, "features.provider_reconciliation.aum.difference_percent"),
        generated_timestamp=generated_timestamp, parameters=configured, processing_data_as_of=processing_data_as_of)
    composite = classify_composite_capital_flow_regime(directions["1d"], pressure, persistence, netflow)
    anomalies = _at(contract, "provenance.anomalies") or {}
    confidence = classify_data_confidence(quality, directions["1d"], pressure, anomalies)
    technical = contract.get("technical_analysis", {}) if isinstance(contract.get("technical_analysis"), Mapping) else {}
    candidates = technical.get("cross_candidates", []) if isinstance(technical.get("cross_candidates"), list) else []
    events = []
    indexes: dict[str, list[str]] = {"moving_average_cross": [], "macd": [], "adx": [], "stochastic": []}
    for item in candidates:
        if not isinstance(item, Mapping):
            continue
        timestamp = _timestamp(item.get("timestamp"))
        cross_id = item.get("cross_id")
        first, second = item.get("first_series"), item.get("second_series")
        direction = item.get("direction")
        if timestamp is None or not isinstance(cross_id, str) or direction not in {-1, 1}:
            continue
        indicator_id = None
        if (first, second) == ("k", "d"):
            group, index_name, indicator_id = "stochastic_cross", "stochastic", "stochastic"
        elif (first, second) == ("macd", "signal"):
            group, index_name, indicator_id = "macd_cross", "macd", "macd"
        elif (first, second) == ("di_plus", "di_minus"):
            group, index_name, indicator_id = "adx_cross", "adx", "adx"
        else:
            group, index_name = "moving_average_cross", "moving_average_cross"
        signal = "bullish" if direction == 1 else "bearish"
        event_uid = f"all_exchange:1d:{timestamp}:technical_cross:{cross_id}"
        event = {"event_uid": event_uid, "timestamp": timestamp, "event_id": cross_id,
            "event_type": "technical_cross", "event_group": group, "signal": signal,
            "label": cross_id.replace("_", " ").upper(), "marker": "arrow_up" if direction == 1 else "arrow_down",
            "source": {"market": "all_exchange", "timeframe": "1d"},
            "display": {"screen_a": indicator_id is None, "screen_b": True}}
        if indicator_id is not None:
            event["indicator_id"] = indicator_id
        events.append(event)
        indexes[index_name].append(event_uid)
    classifications = {"etf_flow_direction": directions, "etf_flow_persistence": persistence,
        "gbtc_premium_regime": premium, "exchange_pressure_regime": pressure,
        "exchange_netflow_regime": netflow, "aum_reconciliation_state": aum,
        "composite_capital_flow_regime": composite, "data_confidence": confidence}
    required = {"etf_flow_direction.1d": directions["1d"], "exchange_pressure_regime": pressure,
                "composite_capital_flow_regime": composite, "data_confidence": confidence}
    optional = {**{f"etf_flow_direction.{name}": directions[name] for name in ("7d", "30d")},
                "etf_flow_persistence": persistence, "gbtc_premium_regime": premium,
                "exchange_netflow_regime": netflow, "aum_reconciliation_state": aum}
    usable = [name for name, item in required.items() if item["status"] in {"available", "partial"}]
    if not usable:
        quality_status = "invalid"
    elif all(item["status"] == "available" for item in required.values()):
        quality_status = "ok"
    else:
        quality_status = "partial"
    all_items = {**required, **optional}
    availability = {status: sorted(name for name, item in all_items.items() if item["status"] == status)
                    for status in STATUSES}
    timestamps = [required[name]["data_as_of"] for name in usable if required[name]["data_as_of"] is not None]
    data_as_of = min(timestamps) if timestamps else None
    if data_as_of is not None and processing_data_as_of is not None:
        data_as_of = min(data_as_of, processing_data_as_of)
    result = {"family": FAMILY, "stage": "classification", "version": VERSION, "mode": contract.get("mode"),
        "data_mode": contract.get("data_mode"), "is_demo": contract.get("is_demo"),
        "generated_at": deepcopy(effective_generated_at), "data_as_of": data_as_of, "classifications": classifications,
        "technical_events": {"events": events, "indexes": indexes},
        "provenance": {"source_family": FAMILY, "source_stage": "processing", "source_version": VERSION,
            "source_processing_data_as_of": contract.get("data_as_of"), "parameters": deepcopy(configured), "warnings": []},
        "quality": {"status": quality_status, "required": list(required), "optional": list(optional),
            "available": availability["available"], "partial": availability["partial"],
            "unavailable": availability["unavailable"], "invalid": availability["invalid"],
            "data_as_of": data_as_of, "warnings": [], "errors": []}}
    json.dumps(result, ensure_ascii=False, allow_nan=False)
    return result


def run_etf_exchange_flows_classification(*, processing_contract: Mapping[str, Any], generated_at: Any = None,
                                          parameters: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return classify_etf_exchange_flows(processing_contract=processing_contract, generated_at=generated_at,
                                       parameters=parameters)


class EtfExchangeFlowsClassifier:
    """Object facade over the pure functional classifier."""

    def __init__(self, *, parameters: Mapping[str, Any] | None = None) -> None:
        self._parameters = _parameters(parameters)

    def classify(self, *, processing_contract: Mapping[str, Any], generated_at: Any = None,
                 parameters: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if parameters is None:
            effective = self._parameters
        elif not isinstance(parameters, Mapping):
            raise ValueError("invalid_classification_input:parameters")
        else:
            effective = _parameters({**self._parameters, **dict(parameters)})
        return classify_etf_exchange_flows(processing_contract=processing_contract, generated_at=generated_at,
                                           parameters=effective)
