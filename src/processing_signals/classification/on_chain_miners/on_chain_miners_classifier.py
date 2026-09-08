from __future__ import annotations

import copy
import json
import math
from collections.abc import Mapping, Sequence
from typing          import Any


MPI_LOW_PRESSURE_MAX                   = 0.0
MPI_HIGH_PRESSURE_MIN                  = 2.0
SOPR_BREAKEVEN_EPSILON                = 0.001
RESERVE_TREND_EPSILON_PERCENT_PER_DAY = 0.001
DEFAULT_RESERVE_TREND_WINDOW          = "30d"

ON_CHAIN_MINERS_FAMILY = "on_chain_miners"
VALID_MODES            = {"bootstrap", "incremental", "recovery"}
VALID_BASIS_STATUSES   = {"available", "partial", "unavailable", "invalid"}
VALID_QUALITY_STATUSES = {"ok", "partial", "invalid"}
CLASSIFICATION_IDS     = ("miner_pressure", "reserve_trend", "net_position", "sopr_regime")


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
            return None, [f"non_finite_source_value:{path}"]
        return (0.0 if value == 0.0 else value), []
    if isinstance(value, Mapping):
        copied: dict[str, Any] = {}
        errors: list[str] = []
        for key, child in value.items():
            if not isinstance(key, str):
                errors.append(f"non_string_source_key:{path}")
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
    return None, [f"non_json_source_value:{path}:{type(value).__name__}"]


SOURCE_TIMESTAMP_PATHS = {
    "miner_pressure": (("timestamp",), ("previous", "timestamp")),
    "reserve_trend":  (("theoretical_start_timestamp",), ("theoretical_end_timestamp",), ("first_timestamp",), ("last_timestamp",)),
    "net_position":   (("timestamp",),),
    "sopr_regime":    (("timestamp",), ("raw_sopr_current", "timestamp")),
}
REQUIRED_SOURCE_TIMESTAMP_PATHS = {
    "miner_pressure": {("timestamp",)},
    "reserve_trend":  {("last_timestamp",)},
    "net_position":   {("timestamp",)},
    "sopr_regime":    {("timestamp",)},
}


def _nested_value(mapping: dict[str, Any], parts: tuple[str, ...]) -> tuple[dict[str, Any] | None, str, Any]:
    parent: Any = mapping
    for part in parts[:-1]:
        if not isinstance(parent, dict) or part not in parent:
            return None, parts[-1], None
        parent = parent[part]
    if not isinstance(parent, dict):
        return None, parts[-1], None
    return parent, parts[-1], parent.get(parts[-1])


def _safe_messages(value: Any, *, path: str) -> tuple[list[str], list[str]]:
    if value is None:
        return [], []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return [], [f"invalid_source_message:{path}"]
    messages: list[str] = []
    errors: list[str] = []
    for index, message in enumerate(value):
        if not isinstance(message, str):
            errors.append(f"invalid_source_message:{path}[{index}]")
        elif message not in messages:
            messages.append(message)
    return messages, errors


def _invalidate_source_result(result: dict[str, Any], errors: Sequence[str]) -> dict[str, Any]:
    result.update({"status": "invalid", "state": None, "signal": None, "display_label": "INVALID",
                   "display_color_token": "invalid", "reason": "invalid_source_payload"})
    result["errors"] = _stable_unique([*result.get("errors", []), *errors])
    return result


def _finalize_source(result: dict[str, Any], raw_source: Mapping[str, Any]) -> dict[str, Any]:
    classification_id = str(result["classification_id"])
    source, errors = copy_json_safe_value(raw_source, path=f"{classification_id}.source")
    for parts in SOURCE_TIMESTAMP_PATHS[classification_id]:
        parent, key, value = _nested_value(source, parts)
        semantic_status = result.get("status") == "available" or (result.get("status") == "partial" and result.get("state") is not None)
        required = semantic_status and parts in REQUIRED_SOURCE_TIMESTAMP_PATHS[classification_id]
        if parent is not None and ((required and value is None) or (value is not None and not _timestamp(value))):
            parent[key] = None
            errors.append(f"invalid_source_timestamp:{classification_id}.source.{'.'.join(parts)}")
    for field in ("warnings", "errors"):
        if field in source:
            messages, message_errors = _safe_messages(source[field], path=f"{classification_id}.source.{field}")
            source[field] = messages
            errors.extend(message_errors)
    result["source"] = source
    return _invalidate_source_result(result, errors) if errors else result


def _messages(value: Any, path: str) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return [f"{path}_must_be_sequence_of_strings"]
    return [f"{path}[{index}]_must_be_nonempty_string" for index, message in enumerate(value) if not isinstance(message, str) or not message]


def _non_string_key_errors(value: Any, path: str = "processing") -> list[str]:
    errors: list[str] = []
    if isinstance(value, Mapping):
        for index, (key, child) in enumerate(value.items()):
            if not isinstance(key, str):
                errors.append(f"non_string_source_key:{path}[key_index={index}]")
                continue
            errors.extend(_non_string_key_errors(child, f"{path}.{key}"))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            errors.extend(_non_string_key_errors(child, f"{path}[{index}]"))
    return errors


def _basis_status(basis: Mapping[str, Any], current: Any = None) -> str:
    explicit = basis.get("status")
    if explicit in VALID_BASIS_STATUSES:
        return str(explicit)
    if isinstance(current, Mapping) and current.get("status") == "available":
        return "available"
    if isinstance(current, Mapping) and current.get("status") in {"unavailable", "invalid"}:
        return str(current["status"])
    return "invalid"


def _empty(classification_id: str, status: str, reason: str, source: Mapping[str, Any], thresholds: Mapping[str, Any] | None = None) -> dict[str, Any]:
    token = "invalid" if status == "invalid" else "unavailable"
    result = {"classification_id": classification_id, "status": status, "state": None, "signal": None,
              "display_label": token.upper(), "display_color_token": token, "source": {},
              "thresholds": copy.deepcopy(dict(thresholds or {})), "reason": reason, "warnings": [], "errors": []}
    return _finalize_source(result, source)


def _source_current(feature_id: str, current: Any) -> dict[str, Any]:
    if not isinstance(current, Mapping):
        return {"feature_id": feature_id, "timestamp": None, "value": None, "unit": None}
    return {"feature_id": feature_id, "timestamp": current.get("timestamp"), "value": current.get("value"), "unit": current.get("unit")}


def classify_miner_pressure(basis: Mapping[str, Any]) -> dict[str, Any]:
    current    = basis.get("current") if isinstance(basis, Mapping) else None
    status     = _basis_status(basis, current)
    source     = _source_current("miner_pressure_basis", current)
    source.update({"components": copy.deepcopy(basis.get("components", current.get("components") if isinstance(current, Mapping) else {})),
                   "previous": basis.get("previous"), "change_1d": basis.get("change_1d")})
    thresholds = {"low_pressure_max": MPI_LOW_PRESSURE_MAX, "high_pressure_min": MPI_HIGH_PRESSURE_MIN}
    if status in {"invalid", "unavailable"}:
        return _empty("miner_pressure", status, f"miner_pressure_basis_{status}", source, thresholds)
    value = source["value"]
    if not _finite(value):
        return _empty("miner_pressure", "invalid", "miner_outflow_z_value_not_finite", source, thresholds)
    if value < MPI_LOW_PRESSURE_MAX:
        state, signal, label, token, reason = "low_selling_pressure", "bullish", "LOW", "positive", "reserve_outflow_z_below_zero"
    elif value <= MPI_HIGH_PRESSURE_MIN:
        state, signal, label, token, reason = "moderate_selling_pressure", "neutral", "MODERATE", "warning", "reserve_outflow_z_between_zero_and_two"
    else:
        state, signal, label, token, reason = "high_selling_pressure", "bearish", "HIGH", "negative", "reserve_outflow_z_above_two"
    result = {"classification_id": "miner_pressure", "status": status, "state": state, "signal": signal, "display_label": label,
              "display_color_token": token, "source": source, "thresholds": thresholds, "reason": reason, "warnings": [], "errors": []}
    if status == "partial":
        result["warnings"].append("miner_pressure_basis_partial")
    return _finalize_source(result, source)


def classify_reserve_trend(basis: Mapping[str, Any]) -> dict[str, Any]:
    windows = basis.get("windows") if isinstance(basis, Mapping) else None
    window  = windows.get(DEFAULT_RESERVE_TREND_WINDOW) if isinstance(windows, Mapping) else None
    status  = _basis_status(basis)
    if isinstance(window, Mapping) and window.get("status") in VALID_BASIS_STATUSES:
        window_status = str(window["status"])
        status = "invalid" if "invalid" in {status, window_status} else "unavailable" if "unavailable" in {status, window_status} else "partial" if "partial" in {status, window_status} else "available"
    source = {"feature_id": "reserve_trend", "window": DEFAULT_RESERVE_TREND_WINDOW, **dict(window or {})}
    thresholds = {"epsilon_percent_per_day": RESERVE_TREND_EPSILON_PERCENT_PER_DAY, "window": DEFAULT_RESERVE_TREND_WINDOW}
    if status in {"invalid", "unavailable"}:
        return _empty("reserve_trend", status, f"reserve_trend_basis_{status}", source, thresholds)
    value = source.get("normalized_slope_percent_per_day")
    if value is None:
        return _empty("reserve_trend", "unavailable", "normalized_slope_unavailable", source, thresholds)
    if not _finite(value):
        return _empty("reserve_trend", "invalid", "normalized_slope_not_finite", source, thresholds)
    epsilon = RESERVE_TREND_EPSILON_PERCENT_PER_DAY
    if value > epsilon:
        state, signal, label, token, reason = "increasing", "bullish", "INCREASING", "positive", "normalized_slope_above_positive_threshold"
    elif value < -epsilon:
        state, signal, label, token, reason = "decreasing", "bearish", "DECREASING", "negative", "normalized_slope_below_negative_threshold"
    else:
        state, signal, label, token, reason = "stable", "neutral", "STABLE", "neutral", "normalized_slope_inside_stable_band"
    result = {"classification_id": "reserve_trend", "status": status, "state": state, "signal": signal, "display_label": label,
              "display_color_token": token, "source": source, "thresholds": thresholds, "reason": reason, "warnings": [], "errors": []}
    if status == "partial":
        result["warnings"].append("reserve_trend_basis_partial")
    return _finalize_source(result, source)


def classify_net_position(basis: Mapping[str, Any]) -> dict[str, Any]:
    current = basis.get("current") if isinstance(basis, Mapping) else None
    status  = _basis_status(basis, current)
    source  = _source_current("net_position_basis", current)
    if status in {"invalid", "unavailable"}:
        return _empty("net_position", status, f"net_position_basis_{status}", source)
    value = source["value"]
    if not _finite(value):
        return _empty("net_position", "invalid", "net_position_value_not_finite", source)
    if value > 0:
        state, signal, label, token, reason = "net_accumulation", "bullish", "ACCUMULATION", "positive", "glassnode_miner_net_position_positive"
    elif value < 0:
        state, signal, label, token, reason = "net_distribution", "bearish", "DISTRIBUTION", "negative", "glassnode_miner_net_position_negative"
    else:
        state, signal, label, token, reason = "balanced", "neutral", "BALANCED", "neutral", "glassnode_miner_net_position_zero"
    result = {"classification_id": "net_position", "status": status, "state": state, "signal": signal, "display_label": label,
              "display_color_token": token, "source": source, "thresholds": {}, "reason": reason, "warnings": [], "errors": []}
    if status == "partial":
        result["warnings"].append("net_position_basis_partial")
    return _finalize_source(result, source)


def classify_sopr_regime(basis: Mapping[str, Any]) -> dict[str, Any]:
    current = basis.get("current") if isinstance(basis, Mapping) else None
    status  = _basis_status(basis, current)
    source  = _source_current("sopr_regime_basis", current)
    source["raw_sopr_current"] = basis.get("raw_sopr_current")
    thresholds = {"center": 1.0, "epsilon": SOPR_BREAKEVEN_EPSILON}
    if status in {"invalid", "unavailable"}:
        return _empty("sopr_regime", status, f"sopr_regime_basis_{status}", source, thresholds)
    value = source["value"]
    if not _finite(value):
        return _empty("sopr_regime", "invalid", "sopr_7d_value_not_finite", source, thresholds)
    upper, lower = 1.0 + SOPR_BREAKEVEN_EPSILON, 1.0 - SOPR_BREAKEVEN_EPSILON
    if value > upper:
        state, signal, label, token, reason = "profit", "bullish", "PROFIT", "positive", "sopr_above_breakeven_band"
    elif value < lower:
        state, signal, label, token, reason = "loss", "bearish", "LOSS", "negative", "sopr_below_breakeven_band"
    else:
        state, signal, label, token, reason = "breakeven", "neutral", "BREAKEVEN", "neutral", "sopr_inside_breakeven_band"
    result = {"classification_id": "sopr_regime", "status": status, "state": state, "signal": signal, "display_label": label,
              "display_color_token": token, "source": source, "thresholds": thresholds, "reason": reason, "warnings": [], "errors": []}
    if status == "partial":
        result["warnings"].append("sopr_regime_basis_partial")
    return _finalize_source(result, source)


def _validate_processing(contract: Any) -> list[str]:
    if not isinstance(contract, Mapping):
        return ["processing_contract_must_be_mapping"]
    errors: list[str] = _non_string_key_errors(contract)
    if contract.get("family") != ON_CHAIN_MINERS_FAMILY:
        errors.append("family_must_be_on_chain_miners")
    if contract.get("stage") != "processing":
        errors.append("stage_must_be_processing")
    if contract.get("mode") not in VALID_MODES:
        errors.append("mode_must_be_bootstrap_incremental_or_recovery")
    for field in ("context", "series", "features", "quality"):
        if not isinstance(contract.get(field), Mapping):
            errors.append(f"{field}_must_be_mapping")
    features = contract.get("features", {})
    if isinstance(features, Mapping):
        for feature_id in ("reserve_trend", "miner_pressure_basis", "sopr_regime_basis", "net_position_basis"):
            if not isinstance(features.get(feature_id), Mapping):
                errors.append(f"missing_required_feature:{feature_id}")
    quality = contract.get("quality", {})
    if isinstance(quality, Mapping):
        if quality.get("status") not in VALID_QUALITY_STATUSES:
            errors.append(f"invalid_processing_quality_status:{quality.get('status')}")
        errors.extend(_messages(quality.get("warnings"), "quality.warnings"))
        errors.extend(_messages(quality.get("errors"), "quality.errors"))
    if errors or not isinstance(features, Mapping):
        return errors
    specifications = {
        "miner_pressure_basis": (features["miner_pressure_basis"].get("current"), "z_score"),
        "net_position_basis":   (features["net_position_basis"].get("current"), "BTC/day"),
        "sopr_regime_basis":    (features["sopr_regime_basis"].get("current"), "ratio"),
    }
    for feature_id, (current, unit) in specifications.items():
        basis  = features[feature_id]
        status = _basis_status(basis, current)
        if basis.get("status") is not None and basis.get("status") not in VALID_BASIS_STATUSES:
            errors.append(f"{feature_id}:invalid_status:{basis.get('status')}")
        if status in {"available", "partial"}:
            if not isinstance(current, Mapping) or current.get("unit") != unit:
                errors.append(f"{feature_id}:incompatible_unit")
    trend = features["reserve_trend"]
    if trend.get("status") not in VALID_BASIS_STATUSES:
        errors.append(f"reserve_trend:invalid_status:{trend.get('status')}")
    return errors


def evaluate_on_chain_miners_classification_quality(*, classifications: Mapping[str, Mapping[str, Any]], processing_quality: Mapping[str, Any]) -> dict[str, Any]:
    processing_status = str(processing_quality.get("status", "invalid"))
    availability      = {name: str(classifications[name]["status"]) for name in CLASSIFICATION_IDS}
    warnings = [f"processing_warning:{message}" for message in processing_quality.get("warnings", [])]
    errors   = [f"processing_error:{message}" for message in processing_quality.get("errors", [])]
    missing: list[str] = []
    semantic = False
    for name in CLASSIFICATION_IDS:
        result = classifications[name]
        semantic = semantic or result.get("state") is not None
        warnings.extend(result.get("warnings", []))
        errors.extend(f"classification_error:{name}:{error}" for error in result.get("errors", []))
        if result.get("reason") == "invalid_source_payload":
            errors.append(f"classification_source_invalid:{name}")
        if result["status"] == "partial":
            warnings.append(f"classification_basis_partial:{name}")
        elif result["status"] == "unavailable":
            warnings.append(f"classification_basis_unavailable:{name}")
            missing.append(name)
        elif result["status"] == "invalid":
            errors.append(f"classification_basis_invalid:{name}")
            missing.append(name)
    if processing_status == "partial":
        warnings.append("processing_quality_partial")
    if processing_status == "invalid" or "invalid" in availability.values() or errors:
        status = "invalid"
    elif processing_status == "ok" and all(value == "available" for value in availability.values()) and semantic:
        status = "ok"
    else:
        status = "partial"
    if status == "partial" and not warnings and not errors and not missing:
        warnings.append("classification_partial_unspecified")
    return {"status": status, "availability": availability, "data_as_of": None, "processing_status": processing_status,
            "missing_fields": _stable_unique(missing), "warnings": _stable_unique(warnings), "errors": _stable_unique(errors)}


def _invalid_classifications(reason: str) -> dict[str, dict[str, Any]]:
    return {name: _empty(name, "invalid", "incompatible_processing_contract", {"feature_id": None}) | {"errors": [reason]} for name in CLASSIFICATION_IDS}


def _feature_messages(classification_id: str, basis: Mapping[str, Any]) -> tuple[list[str], list[str], list[str]]:
    warnings, warning_errors = _safe_messages(basis.get("warnings", []), path=f"{classification_id}.source.warnings")
    errors, error_errors     = _safe_messages(basis.get("errors", []), path=f"{classification_id}.source.errors")
    prefixed_warnings = [f"feature_warning:{classification_id}:{message}" for message in warnings]
    prefixed_errors   = [f"feature_error:{classification_id}:{message}" for message in errors]
    return prefixed_warnings, prefixed_errors, [*warning_errors, *error_errors]


def _serialization_fallback(mode: Any, serialization_error_type: str) -> dict[str, Any]:
    classifications = {name: _empty(name, "invalid", "classification_output_serialization_failed", {"feature_id": None}) for name in CLASSIFICATION_IDS}
    error = f"classification_output_serialization_failed:{serialization_error_type}"
    return {"family": ON_CHAIN_MINERS_FAMILY, "stage": "classification", "mode": mode if mode in VALID_MODES else None,
            "context": {"asset": None, "data_mode": None, "is_demo": None, "reference_timestamp": None, "execution_timestamp": None,
                        "generated_at": None, "processing_data_as_of": None, "calculation_history": None,
                        "classification_policy": "on_chain_miners_v1"},
            "classifications": classifications,
            "quality": {"status": "invalid", "availability": {name: "invalid" for name in CLASSIFICATION_IDS}, "data_as_of": None,
                        "processing_status": "invalid", "missing_fields": list(CLASSIFICATION_IDS), "warnings": [], "errors": [error]}}


class OnChainMinersClassifier:
    def __init__(self, processing_contract: Mapping[str, Any]) -> None:
        self.processing_contract = processing_contract

    def run(self) -> dict[str, Any]:
        contract_errors = _validate_processing(self.processing_contract)
        raw_context = self.processing_contract.get("context", {}) if isinstance(self.processing_contract, Mapping) else {}
        raw_quality = self.processing_contract.get("quality", {}) if isinstance(self.processing_contract, Mapping) else {}
        context = raw_context if isinstance(raw_context, Mapping) else {}
        processing_quality = raw_quality if isinstance(raw_quality, Mapping) else {}
        mode = self.processing_contract.get("mode") if isinstance(self.processing_contract, Mapping) else None
        output_context = {"asset": context.get("asset"), "data_mode": context.get("data_mode"), "is_demo": context.get("is_demo"),
                          "reference_timestamp": context.get("reference_timestamp"), "execution_timestamp": context.get("execution_timestamp"),
                          "generated_at": context.get("generated_at"), "processing_data_as_of": processing_quality.get("data_as_of"),
                          "calculation_history": context.get("calculation_history"), "classification_policy": "on_chain_miners_v1"}
        if contract_errors:
            reason          = ";".join(contract_errors)
            classifications = _invalid_classifications(reason)
        else:
            features = self.processing_contract["features"]
            classifications = {"miner_pressure": classify_miner_pressure(features["miner_pressure_basis"]),
                               "reserve_trend": classify_reserve_trend(features["reserve_trend"]),
                               "net_position": classify_net_position(features["net_position_basis"]),
                               "sopr_regime": classify_sopr_regime(features["sopr_regime_basis"])}
            basis_by_classification = {"miner_pressure": features["miner_pressure_basis"], "reserve_trend": features["reserve_trend"],
                                       "net_position": features["net_position_basis"], "sopr_regime": features["sopr_regime_basis"]}
            for name, basis in basis_by_classification.items():
                feature_warnings, feature_errors, message_errors = _feature_messages(name, basis)
                classifications[name]["warnings"] = _stable_unique([*classifications[name]["warnings"], *feature_warnings])
                classifications[name]["errors"]   = _stable_unique([*classifications[name]["errors"], *feature_errors])
                if message_errors:
                    _invalidate_source_result(classifications[name], message_errors)
            trend_window = features["reserve_trend"].get("windows", {}).get(DEFAULT_RESERVE_TREND_WINDOW, {})
            if isinstance(trend_window, Mapping):
                window_warnings = classifications["reserve_trend"]["source"].get("warnings", [])
                window_errors   = classifications["reserve_trend"]["source"].get("errors", [])
                classifications["reserve_trend"]["warnings"] = _stable_unique([
                    *classifications["reserve_trend"]["warnings"], *(f"feature_window_warning:reserve_trend:30d:{message}" for message in window_warnings)])
                classifications["reserve_trend"]["errors"] = _stable_unique([
                    *classifications["reserve_trend"]["errors"], *(f"feature_window_error:reserve_trend:30d:{message}" for message in window_errors)])
            if processing_quality.get("status") == "invalid":
                classifications = {name: _empty(name, "invalid", "processing_quality_invalid", result["source"], result["thresholds"])
                                   for name, result in classifications.items()}
        quality = evaluate_on_chain_miners_classification_quality(classifications=classifications, processing_quality=processing_quality)
        if contract_errors:
            quality["errors"] = _stable_unique([*quality["errors"], *contract_errors])
            quality["status"] = "invalid"
        if quality["status"] != "invalid":
            timestamps = [classifications["miner_pressure"]["source"].get("timestamp"),
                          classifications["reserve_trend"]["source"].get("last_timestamp"),
                          classifications["net_position"]["source"].get("timestamp"),
                          classifications["sopr_regime"]["source"].get("timestamp")]
            if all(_timestamp(value) for value in timestamps):
                data_as_of = min(timestamps)
                processing_data_as_of = processing_quality.get("data_as_of")
                if _timestamp(processing_data_as_of) and data_as_of > processing_data_as_of:
                    data_as_of = processing_data_as_of
                    quality["warnings"] = _stable_unique([*quality["warnings"], "classification_data_as_of_capped_by_processing"])
                quality["data_as_of"] = data_as_of
            else:
                quality["warnings"] = _stable_unique([*quality["warnings"], "classification_data_as_of_unavailable"])
        output = {"family": ON_CHAIN_MINERS_FAMILY, "stage": "classification", "mode": mode, "context": output_context,
                  "classifications": classifications, "quality": quality}
        try:
            _, tree_errors = copy_json_safe_value(output, path="classification")
            if tree_errors:
                raise TypeError("invalid_json_tree")
            json.dumps(output, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            output = _serialization_fallback(mode, type(exc).__name__)
            json.dumps(output, ensure_ascii=False, allow_nan=False)
        return output


def classify_on_chain_miners(processing_contract: Mapping[str, Any]) -> dict[str, Any]:
    """Classify the four approved On-Chain Screen bases without recomputing Processing mathematics."""
    original, _ = copy_json_safe_value(processing_contract, path="processing")
    output   = OnChainMinersClassifier(processing_contract).run()
    current, _ = copy_json_safe_value(processing_contract, path="processing")
    if current != original:
        raise RuntimeError("Classification mutated the Processing contract")
    return output
