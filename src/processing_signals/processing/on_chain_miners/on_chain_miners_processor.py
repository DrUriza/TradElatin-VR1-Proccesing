from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from typing          import Any

from .on_chain_miners_math import (
    rolling_zscore,
    rolling_mean,
    pct_change as native_pct_change,
    difference,
    rolling_wasserstein,
    latest,
    score_to_probability,
)

from .on_chain_miners_feature_builder import build_on_chain_miners_features


ON_CHAIN_MINERS_FAMILY = "on_chain_miners"
VALID_MODES            = {"bootstrap", "incremental", "recovery"}
EXPECTED_UNITS = {
    "miner_reserve": "BTC", "sopr": "ratio", "hashrate": "H/s",
    "difficulty": "provider_native_difficulty", "miner_net_position_change": "BTC/day",
    "mpi": "z_score", "miner_outflow_total": "BTC/day", "miner_revenue_total_usd": "USD/day",
}
PROCESSING_SERIES = ("miner_reserve_btc", "sopr", "sopr_7d", "hashrate_eh_s", "difficulty_t",
                     "miner_net_position_change", "mpi", "miner_outflow_total_btc", "miner_revenue_total_usd")
DATA_AS_OF_SERIES = ("miner_reserve_btc", "sopr_7d", "hashrate_eh_s", "difficulty_t",
                     "miner_net_position_change", "mpi", "miner_outflow_total_btc", "miner_revenue_total_usd")
VALID_SERIES_STATUSES = {"available", "partial", "unavailable", "invalid"}
VALID_QUALITY_STATUSES = {"ok", "partial", "invalid"}


def _stable_unique(messages: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(messages))


def _json_safe_copy(value: Any) -> tuple[Any, bool]:
    if value is None or isinstance(value, (str, bool, int)):
        return value, False
    if isinstance(value, float):
        if not math.isfinite(value):
            return None, True
        return (0.0 if value == 0.0 else value), False
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        invalid = False
        for key, item in value.items():
            if not isinstance(key, str):
                invalid = True
                continue
            copied, item_invalid = _json_safe_copy(item)
            output[key] = copied
            invalid = invalid or item_invalid
        return output, invalid
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        output_list = []
        invalid = False
        for item in value:
            copied, item_invalid = _json_safe_copy(item)
            output_list.append(copied)
            invalid = invalid or item_invalid
        return output_list, invalid
    return None, True


def _validate_messages(value: Any, path: str) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return [f"{path}_must_be_sequence_of_strings"]
    return [f"{path}[{index}]_must_be_nonempty_string" for index, message in enumerate(value) if not isinstance(message, str) or not message]


def _non_string_key_errors(value: Any, path: str = "input") -> list[str]:
    errors: list[str] = []
    if isinstance(value, Mapping):
        for index, (key, item) in enumerate(value.items()):
            if not isinstance(key, str):
                errors.append(f"non_string_input_key:{path}[key_index={index}]")
                continue
            errors.extend(_non_string_key_errors(item, f"{path}.{key}"))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            errors.extend(_non_string_key_errors(item, f"{path}[{index}]"))
    return errors


def _validate_record(metric_id: str, record: Any, index: int, previous_timestamp: int | None) -> tuple[int | None, list[str]]:
    errors: list[str] = []
    if not isinstance(record, Mapping):
        return previous_timestamp, [f"{metric_id}.records[{index}]:record_must_be_mapping"]
    timestamp = record.get("timestamp")
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
        errors.append(f"{metric_id}.records[{index}]:timestamp_must_be_non_negative_unix_seconds")
        return previous_timestamp, errors
    if previous_timestamp is not None:
        if timestamp == previous_timestamp:
            errors.append(f"{metric_id}.records[{index}]:duplicate_timestamp:{timestamp}")
        elif timestamp < previous_timestamp:
            errors.append(f"{metric_id}.records[{index}]:timestamps_not_ascending")
    value = record.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        errors.append(f"{metric_id}.records[{index}]:value_must_be_finite_number")
    if metric_id == "sopr":
        for field in ("sopr", "a_sopr", "sth_sopr", "lth_sopr"):
            auxiliary = record.get(field)
            if auxiliary is not None and (isinstance(auxiliary, bool) or not isinstance(auxiliary, (int, float)) or not math.isfinite(auxiliary)):
                errors.append(f"sopr.records[{index}].{field}:must_be_finite_number_or_null")
    return timestamp, errors


def validate_on_chain_miners_input(input_contract: Any) -> list[str]:
    if not isinstance(input_contract, Mapping):
        return ["input_contract_must_be_mapping"]
    errors: list[str] = _non_string_key_errors(input_contract)
    if errors:
        return errors
    if input_contract.get("family") != ON_CHAIN_MINERS_FAMILY:
        errors.append("family_must_be_on_chain_miners")
    if input_contract.get("stage") != "input":
        errors.append("stage_must_be_input")
    if input_contract.get("mode") not in VALID_MODES:
        errors.append("mode_must_be_bootstrap_incremental_or_recovery")
    series = input_contract.get("series")
    if not isinstance(series, Mapping):
        return [*errors, "series_must_be_mapping"]
    for metric_id, expected_unit in EXPECTED_UNITS.items():
        payload = series.get(metric_id)
        if not isinstance(payload, Mapping):
            errors.append(f"missing_core_series:{metric_id}")
            continue
        if payload.get("status") not in VALID_SERIES_STATUSES:
            errors.append(f"{metric_id}:invalid_or_missing_series_status:{payload.get('status')}")
        errors.extend(_validate_messages(payload.get("warnings"), f"{metric_id}.warnings"))
        errors.extend(_validate_messages(payload.get("errors"), f"{metric_id}.errors"))
        if payload.get("unit") != expected_unit:
            errors.append(f"{metric_id}:incompatible_unit:{payload.get('unit')}!=:{expected_unit}")
        records = payload.get("records")
        if not isinstance(records, Sequence) or isinstance(records, (str, bytes, bytearray)):
            errors.append(f"{metric_id}.records_must_be_sequence")
            continue
        previous_timestamp = None
        for index, record in enumerate(records):
            previous_timestamp, record_errors = _validate_record(metric_id, record, index, previous_timestamp)
            errors.extend(record_errors)
    quality = input_contract.get("quality")
    if not isinstance(quality, Mapping):
        errors.append("quality_must_be_mapping")
    else:
        if quality.get("status") not in VALID_QUALITY_STATUSES:
            errors.append(f"invalid_or_missing_input_quality_status:{quality.get('status')}")
        errors.extend(_validate_messages(quality.get("warnings"), "quality.warnings"))
        errors.extend(_validate_messages(quality.get("errors"), "quality.errors"))
    return errors


def _invalid_series(metric_id: str, reason: str) -> dict[str, Any]:
    return {"metric_id": metric_id, "status": "invalid", "unit": None, "records": [], "unavailable_records": [], "warnings": [], "errors": [reason],
            "current": {"status": "unavailable", "value": None, "reason": "invalid_input_contract"},
            "metadata": {"records_source": 0, "records_calculated": 0, "records_unavailable": 0, "first_valid_timestamp": None,
                         "last_valid_timestamp": None, "calculation_history": "full_available_history", "history_truncated": False}}


def _invalid_features(reason: str) -> dict[str, Any]:
    unavailable = {"status": "unavailable", "value": None, "reason": "invalid_input_contract"}
    return {
        "reserve_trend": {"feature_id": "reserve_trend", "status": "invalid", "default_window_days": 30, "windows": {}, "warnings": [], "errors": [reason]},
        "miner_pressure_basis": {"source_metric_id": "mpi", "status": "invalid", "current": unavailable, "previous": None, "change_1d": None, "unit": "z_score"},
        "sopr_regime_basis": {"source_metric_id": "sopr_7d", "status": "invalid", "current": unavailable, "raw_sopr_current": unavailable},
        "net_position_basis": {"source_metric_id": "miner_net_position_change", "status": "invalid", "current": unavailable},
    }


def evaluate_on_chain_miners_processing_quality(*, series: Mapping[str, Any], features: Mapping[str, Any],
                                                input_quality: Mapping[str, Any], input_series: Mapping[str, Any] | None = None) -> dict[str, Any]:
    availability = {metric_id: str(series.get(metric_id, {}).get("status", "invalid")) for metric_id in PROCESSING_SERIES}
    availability["reserve_trend"] = str(features.get("reserve_trend", {}).get("status", "invalid"))
    warnings = [f"{metric_id}:{warning}" for metric_id, payload in series.items() for warning in payload.get("warnings", [])]
    errors = [f"{metric_id}:{error}" for metric_id, payload in series.items() for error in payload.get("errors", [])]
    trend = features.get("reserve_trend", {})
    warnings.extend(f"reserve_trend:{warning}" for warning in trend.get("warnings", []))
    errors.extend(f"reserve_trend:{error}" for error in trend.get("errors", []))
    warnings.extend(f"input_warning:{message}" for message in input_quality.get("warnings", []))
    errors.extend(f"input_error:{message}" for message in input_quality.get("errors", []))
    for metric_id, payload in (input_series or {}).items():
        warnings.extend(f"input_series_warning:{metric_id}:{message}" for message in payload.get("warnings", []))
        errors.extend(f"input_series_error:{metric_id}:{message}" for message in payload.get("errors", []))
    missing = [metric_id for metric_id in PROCESSING_SERIES if series.get(metric_id, {}).get("current", {}).get("status") not in {"available", "partial"}]
    if availability["reserve_trend"] in {"unavailable", "invalid"}:
        missing.append("reserve_trend")
    input_status = str(input_quality.get("status", "invalid"))
    if input_status == "invalid":
        errors.append("input_quality_invalid")
    elif input_status == "partial":
        warnings.append("input_quality_partial")
    for metric_id, status_value in availability.items():
        if status_value == "partial": warnings.append(f"required_series_partial:{metric_id}")
        elif status_value == "unavailable": warnings.append(f"required_series_unavailable:{metric_id}")
    if any(status == "invalid" for status in availability.values()) or input_status == "invalid" or errors:
        status = "invalid"
    elif not missing and all(status == "available" for status in availability.values()) and input_status == "ok":
        status = "ok"
    else:
        status = "partial"
    timestamps = [series[mid]["current"].get("timestamp") for mid in DATA_AS_OF_SERIES
                  if series.get(mid, {}).get("current", {}).get("status") == "available"]
    data_as_of = min(timestamps) if status != "invalid" and len(timestamps) == len(DATA_AS_OF_SERIES) else None
    if status != "invalid" and data_as_of is None:
        warnings.append("processing_data_as_of_unavailable")
    return {"status": status, "availability": availability, "data_as_of": data_as_of, "input_status": input_status,
            "missing_fields": _stable_unique(missing), "warnings": _stable_unique(warnings), "errors": _stable_unique(errors)}


def _records_map(metric: Mapping[str, Any]) -> dict[int, float | None]:
    return {int(row["timestamp"]): row.get("value") for row in metric.get("records", []) if isinstance(row, Mapping) and row.get("timestamp") is not None}


def _native_miner_analysis(series_map: Mapping[str, Any]) -> dict[str, Any]:
    reserve_records = series_map.get("miner_reserve_btc", {}).get("records", [])
    timestamps = [int(row["timestamp"]) for row in reserve_records]
    def aligned_metric(name: str) -> list[float | None]:
        lookup = _records_map(series_map.get(name, {}))
        return [lookup.get(ts) for ts in timestamps]

    reserve = aligned_metric("miner_reserve_btc")
    mpi = aligned_metric("mpi")
    outflow = aligned_metric("miner_outflow_total_btc")
    revenue = aligned_metric("miner_revenue_total_usd")
    hashrate = aligned_metric("hashrate_eh_s")
    difficulty = aligned_metric("difficulty_t")
    sopr = aligned_metric("sopr")

    reserve_change = difference(reserve)
    reserve_change_z = rolling_zscore(reserve_change, 30, 10)
    reserve_roc_30 = native_pct_change(reserve, 30, 100.0)

    mpi_z = rolling_zscore(mpi, 30, 10)
    mte_z = rolling_zscore(outflow, 30, 10)
    # Final MSP formula. Weights are explicit Processing configuration defaults.
    weights = {"w1_mpi": 0.40, "w2_mte": 0.35, "w3_reserve_change": 0.25}
    selling_pressure: list[float | None] = []
    for zm, zte, zr in zip(mpi_z, mte_z, reserve_change_z, strict=True):
        if zm is None and zte is None and zr is None:
            selling_pressure.append(None)
        else:
            selling_pressure.append(weights["w1_mpi"] * float(zm or 0.0) + weights["w2_mte"] * float(zte or 0.0) - weights["w3_reserve_change"] * float(zr or 0.0))

    revenue_ma365 = rolling_mean(revenue, 365, 180)
    puell: list[float | None] = []
    for value, avg in zip(revenue, revenue_ma365, strict=True):
        puell.append(None if value is None or avg in (None, 0) else float(value) / float(avg))
    revenue_stress = [-v if v is not None else None for v in rolling_zscore(revenue, 30, 7)]

    hash_ma30 = rolling_mean(hashrate, 30, 15)
    hash_ma60 = rolling_mean(hashrate, 60, 30)
    hash_momentum = native_pct_change(hashrate, 30, 100.0)
    hash_change_z = rolling_zscore(native_pct_change(hashrate, 1, 100.0), 30, 10)
    diff_change_z = rolling_zscore(native_pct_change(difficulty, 1, 100.0), 30, 10)
    network_stress = [None if h is None and d is None else float(-(h or 0.0) + (d or 0.0)) / 2.0 for h,d in zip(hash_change_z,diff_change_z,strict=True)]

    sopr_stress = [None if v is None else max(-3.0, min(3.0, (1.0 - float(v)) * 10.0)) for v in sopr]
    regime_score: list[float | None] = []
    for sp, ns, ss, rz in zip(selling_pressure, network_stress, sopr_stress, reserve_change_z, strict=True):
        vals = [v for v in (sp, ns, ss, None if rz is None else -float(rz)) if v is not None]
        regime_score.append(None if not vals else float(sum(vals) / len(vals)))
    wasserstein = rolling_wasserstein(regime_score, 20, 60)
    capitulation = [score_to_probability(v, 1.0) for v in regime_score]
    recovery = [None if p is None else 100.0 - float(p) for p in capitulation]

    analyses = {
        "miner_reserve_change_zscore": {"reserve_change_btc":reserve_change,"reserve_change_zscore":reserve_change_z,"reserve_roc_30d_pct":reserve_roc_30},
        "miner_selling_pressure": {"mpi":mpi,"miner_to_exchange_zscore":mte_z,"selling_pressure_score":selling_pressure},
        "puell_revenue_stress": {"puell_multiple":puell,"revenue_stress_score":revenue_stress},
        "hashrate_momentum_hash_ribbon": {"hashrate_eh_s":hashrate,"hash_ma_30":hash_ma30,"hash_ma_60":hash_ma60,"hash_momentum_pct":hash_momentum},
        "hashrate_difficulty_stress": {"hashrate_change_zscore":hash_change_z,"difficulty_change_zscore":diff_change_z,"network_stress_score":network_stress},
        "miner_capitulation_recovery_regime": {"regime_score":regime_score,"capitulation_probability_pct":capitulation,"recovery_score_pct":recovery,"wasserstein_distance":wasserstein},
    }
    return {"analysis_id":"native_miner_analysis_vr1", "status":"available", "timestamps":timestamps,
            "weights":{"miner_selling_pressure":weights}, "indicators":analyses, "recalculate_in_hmi":False}


class OnChainMinersProcessor:
    def __init__(self, input_contract: Mapping[str, Any]) -> None:
        self.input_contract = input_contract

    def run(self) -> dict[str, Any]:
        errors = validate_on_chain_miners_input(self.input_contract)
        context = self.input_contract.get("context", {}) if isinstance(self.input_contract, Mapping) else {}
        mode = self.input_contract.get("mode") if isinstance(self.input_contract, Mapping) else None
        output_context = {"asset": context.get("asset"), "data_mode": context.get("data_mode"), "is_demo": context.get("is_demo"),
                          "reference_timestamp": context.get("reference_timestamp"), "execution_timestamp": context.get("execution_timestamp"),
                          "generated_at": context.get("generated_at"),
                          "input_data_as_of": self.input_contract.get("quality", {}).get("data_as_of") if isinstance(self.input_contract, Mapping) else None,
                          "calculation_history": "full_available_history", "presentation_window": None}
        if errors:
            reason = ";".join(errors)
            series = {metric_id: _invalid_series(metric_id, reason) for metric_id in PROCESSING_SERIES}
            features = _invalid_features(reason)
            quality = {"status": "invalid", "availability": {**{metric_id: "invalid" for metric_id in PROCESSING_SERIES}, "reserve_trend": "invalid"},
                       "data_as_of": None, "input_status": self.input_contract.get("quality", {}).get("status", "invalid"),
                       "missing_fields": list(PROCESSING_SERIES), "warnings": [], "errors": errors}
        else:
            built = build_on_chain_miners_features(self.input_contract["series"],
                                                  input_data_as_of=self.input_contract.get("quality", {}).get("data_as_of"))
            series = built["series"]
            features = built["features"]
            quality = evaluate_on_chain_miners_processing_quality(series=series, features=features,
                                                                  input_quality=self.input_contract.get("quality", {}),
                                                                  input_series=self.input_contract.get("series", {}))
        miner_analysis = _native_miner_analysis(series) if not errors else {"analysis_id": "native_miner_analysis_vr1", "status": "invalid", "timestamps": [], "indicators": {}, "recalculate_in_hmi": False}
        output = {"family": ON_CHAIN_MINERS_FAMILY, "stage": "processing", "mode": mode, "context": output_context,
                  "series": series, "features": features, "miner_analysis": miner_analysis, "quality": quality}
        output, unsafe = _json_safe_copy(output)
        if unsafe:
            output["quality"].update({"status": "invalid", "data_as_of": None})
            output["quality"]["errors"] = _stable_unique([*output["quality"].get("errors", []), "non_json_safe_processing_value"])
        try:
            json.dumps(output, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            output["quality"].update({"status": "invalid", "data_as_of": None})
            output["quality"]["errors"].append(f"non_serializable_processing_output:{exc}")
        return output


def process_on_chain_miners(input_contract: Mapping[str, Any]) -> dict[str, Any]:
    """Build deterministic Processing output from the complete persisted Input history."""
    original_safe, _ = _json_safe_copy(input_contract)
    try:
        output = OnChainMinersProcessor(input_contract).run()
        current_safe, _ = _json_safe_copy(input_contract)
        if current_safe != original_safe:
            raise RuntimeError("processing_input_mutated")
        return output
    except Exception as exc:  # Public contract must remain JSON-safe for adversarial Input.
        context = input_contract.get("context", {}) if isinstance(input_contract, Mapping) else {}
        context = context if isinstance(context, Mapping) else {}
        required_series = PROCESSING_SERIES
        reason = f"processing_contract_build_failed:{type(exc).__name__}"
        safe_context, _ = _json_safe_copy({"asset": context.get("asset"), "data_mode": context.get("data_mode"), "is_demo": context.get("is_demo"),
                                           "reference_timestamp": context.get("reference_timestamp"), "execution_timestamp": context.get("execution_timestamp"),
                                           "generated_at": context.get("generated_at"), "input_data_as_of": None,
                                           "calculation_history": "full_available_history", "presentation_window": None})
        series = {metric_id: _invalid_series(metric_id, reason) for metric_id in required_series}
        features = _invalid_features(reason)
        availability = {**{metric_id: "invalid" for metric_id in required_series}, "reserve_trend": "invalid"}
        output = {"family": ON_CHAIN_MINERS_FAMILY, "stage": "processing",
                  "mode": input_contract.get("mode") if isinstance(input_contract, Mapping) else None, "context": safe_context,
                  "series": series, "features": features,
                  "quality": {"status": "invalid", "availability": availability, "data_as_of": None, "input_status": "invalid",
                              "missing_fields": list(required_series), "warnings": [], "errors": [reason]}}
        safe_output, _ = _json_safe_copy(output)
        return safe_output
