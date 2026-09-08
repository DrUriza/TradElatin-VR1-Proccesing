from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from copy import deepcopy
from numbers import Integral, Real
from typing import Any


FAMILY = "volatility_market_regimes"
PROCESSING_VERSION = "0.2.0"
CLASSIFICATION_VERSION = "0.2.0"
LOW_VOL_PERCENTILE_THRESHOLD = 1.0 / 3.0
HIGH_VOL_PERCENTILE_THRESHOLD = 2.0 / 3.0
CONFIDENCE_HIGH_THRESHOLD = 0.75
CONFIDENCE_MEDIUM_THRESHOLD = 0.40
DAY_SECONDS = 86400

REGIME_STATES = {"low_vol", "normal", "high_vol"}
AVAILABILITY_STATES = {"available", "partial", "unavailable", "invalid"}
_MODES = {"bootstrap", "incremental", "recovery"}
_BASIS_FIELDS = (
    "realized_volatility_percent",
    "realized_rolling_mean_30d",
    "realized_rolling_std_30d",
    "realized_z_score_30d",
    "realized_percentile_rank_30d",
    "dvol",
    "spread_7d",
)


def _clean(value: float | None) -> float | None:
    if value is None:
        return None
    result = float(value)
    return 0.0 if result == 0 else result


def _finite(value: Any, path: str, nullable: bool = True) -> float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{path}:finite_number_required")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{path}:finite_number_required")
    return _clean(result)


def _timestamp(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ValueError(f"{path}:integer_timestamp_required")
    return int(value)


def validate_volatility_market_regimes_processing_contract(contract: Any) -> None:
    if not isinstance(contract, Mapping):
        raise ValueError("processing_contract:mapping_required")
    if contract.get("family") != FAMILY:
        raise ValueError(f"family:{FAMILY}_required")
    if contract.get("stage") != "processing":
        raise ValueError("stage:processing_required")
    if contract.get("version") != PROCESSING_VERSION:
        raise ValueError(f"version:{PROCESSING_VERSION}_required")
    if contract.get("mode") not in _MODES:
        raise ValueError("mode:invalid")
    if not isinstance(contract.get("context"), Mapping):
        raise ValueError("context:mapping_required")
    features = contract.get("features")
    if not isinstance(features, Mapping):
        raise ValueError("features:mapping_required")
    quality = contract.get("quality")
    if not isinstance(quality, Mapping) or quality.get("status") not in {"ok", "partial", "invalid"}:
        raise ValueError("quality:invalid")
    for name in ("realized_volatility", "dvol", "volatility_spread", "daily_regime_basis"):
        feature = features.get(name)
        if not isinstance(feature, Mapping) or feature.get("status") not in AVAILABILITY_STATES:
            raise ValueError(f"features.{name}:invalid")
    for feature_name in ("realized_volatility", "dvol", "volatility_spread", "daily_regime_basis"):
        records = features[feature_name].get("records")
        if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
            raise ValueError(f"features.{feature_name}.records:sequence_required")
        seen: set[int] = set()
        for index, record in enumerate(records):
            if not isinstance(record, Mapping):
                raise ValueError(f"features.{feature_name}.records[{index}]:mapping_required")
            timestamp = _timestamp(record.get("timestamp"), f"features.{feature_name}.records[{index}].timestamp")
            if timestamp in seen:
                raise ValueError(f"features.{feature_name}.records:duplicate_timestamp")
            seen.add(timestamp)


def classify_percentile_regime(rank: Any) -> str | None:
    if rank is None:
        return None
    value = _finite(rank, "percentile_rank", nullable=False)
    if value < 0 or value > 1:
        raise ValueError("invalid_percentile_rank")
    if value < LOW_VOL_PERCENTILE_THRESHOLD:
        return "low_vol"
    if value > HIGH_VOL_PERCENTILE_THRESHOLD:
        return "high_vol"
    return "normal"


def calculate_regime_confidence(realized_rank: Any, realized_state: str) -> dict[str, Any]:
    """Confidence derives only from the realized-volatility percentile geometry."""
    rank = _finite(realized_rank, "realized_percentile_rank_30d", nullable=False)
    if rank < 0 or rank > 1:
        raise ValueError("invalid_percentile_rank")
    if realized_state == "low_vol":
        strength = (LOW_VOL_PERCENTILE_THRESHOLD - rank) / LOW_VOL_PERCENTILE_THRESHOLD
    elif realized_state == "normal":
        half_width = HIGH_VOL_PERCENTILE_THRESHOLD - 0.5
        strength = 1.0 - abs(rank - 0.5) / half_width
    elif realized_state == "high_vol":
        strength = (rank - HIGH_VOL_PERCENTILE_THRESHOLD) / (1.0 - HIGH_VOL_PERCENTILE_THRESHOLD)
    else:
        raise ValueError("realized_state:invalid")
    score = _clean(min(max(strength, 0.0), 1.0))
    state = "high" if score >= CONFIDENCE_HIGH_THRESHOLD else "medium" if score >= CONFIDENCE_MEDIUM_THRESHOLD else "low"
    return {
        "confidence_score": score,
        "confidence_state": state,
        "confidence_basis": "realized_percentile_boundary_distance",
        "realized_strength": score,
    }


def classify_daily_regime_record(record: Mapping[str, Any]) -> dict[str, Any]:
    timestamp = _timestamp(record.get("timestamp"), "daily_record.timestamp")
    basis = {field: deepcopy(record.get(field)) for field in _BASIS_FIELDS}
    output = {
        "timestamp": timestamp,
        "data_as_of": record.get("data_as_of"),
        "regime": None,
        "confidence_score": None,
        "confidence_state": None,
        "confidence_basis": None,
        "realized_strength": None,
        "persistence_days": None,
        "basis": basis,
        "status": "unavailable",
        "reason": "realized_percentile_unavailable",
        "warnings": [],
    }
    if record.get("status") == "invalid":
        output.update(status="invalid", reason="invalid_processing_basis")
        return output
    try:
        realized_state = classify_percentile_regime(record.get("realized_percentile_rank_30d"))
    except ValueError:
        output.update(status="invalid", reason="invalid_percentile_rank")
        return output
    if realized_state is None:
        output["reason"] = "classification_warmup_incomplete"
        return output
    confidence = calculate_regime_confidence(record.get("realized_percentile_rank_30d"), realized_state)
    output.update(regime=realized_state, status="available", reason=None, **confidence)
    return output


def calculate_regime_persistence(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    previous_regime = None
    previous_time = None
    persistence = 0
    for record in records:
        valid = record.get("regime") in REGIME_STATES and record.get("status") == "available"
        if valid:
            consecutive = previous_regime == record["regime"] and previous_time is not None and record["timestamp"] - previous_time == DAY_SECONDS
            persistence = persistence + 1 if consecutive else 1
            record["persistence_days"] = persistence
            previous_regime = record["regime"]
            previous_time = record["timestamp"]
        else:
            persistence, previous_regime, previous_time = 0, None, None
            record["persistence_days"] = None
    return records


def classify_daily_regime_history(feature: Mapping[str, Any]) -> dict[str, Any]:
    records = [classify_daily_regime_record(record) for record in sorted(feature.get("records", []), key=lambda item: item["timestamp"])]
    calculate_regime_persistence(records)
    current = next((deepcopy(record) for record in reversed(records) if record["regime"] is not None and record["status"] == "available"), None)
    if any(record["status"] == "invalid" for record in records):
        status, reason = "invalid", "invalid_daily_regime_record"
    elif not current:
        status, reason = "unavailable", "current_regime_unavailable"
    elif feature.get("status") in {"available", "partial"}:
        # Expected warm-up rows before a 30-day percentile are not a current-data degradation.
        status, reason = "available", None
    else:
        status, reason = feature.get("status", "unavailable"), feature.get("reason") or "daily_regime_basis_unavailable"
    return {
        "status": status,
        "reason": reason,
        "records": records,
        "current": current,
        "current_persistence_days": current["persistence_days"] if current else None,
        "records_available": sum(record["status"] == "available" for record in records),
        "records_warmup": sum(record.get("reason") == "classification_warmup_incomplete" for record in records),
        "source_data_as_of": feature.get("source_data_as_of"),
    }


def _distribution(records: Sequence[Mapping[str, Any]], window: str) -> dict[str, Any]:
    valid = [record for record in records if record.get("regime") in REGIME_STATES and record.get("status") == "available"]
    if window == "30d" and valid:
        end = valid[-1]["timestamp"]
        start = end - 29 * DAY_SECONDS
        valid = [record for record in valid if start <= record["timestamp"] <= end]
    if not valid:
        return {
            "status": "unavailable", "reason": "no_classified_regime_days", "basis": "empirical_classified_day_share", "window": window,
            "window_start_timestamp": None, "window_end_timestamp": None, "classified_days": 0,
            "counts": {state: 0 for state in ("low_vol", "normal", "high_vol")},
            "shares": {state: None for state in ("low_vol", "normal", "high_vol")},
        }
    counts = {state: sum(record["regime"] == state for record in valid) for state in ("low_vol", "normal", "high_vol")}
    total = len(valid)
    return {
        "status": "available", "reason": None, "basis": "empirical_classified_day_share", "window": window,
        "window_start_timestamp": valid[0]["timestamp"], "window_end_timestamp": valid[-1]["timestamp"], "classified_days": total,
        "counts": counts, "shares": {state: _clean(counts[state] / total) for state in counts},
    }


def calculate_regime_distribution(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {"full_history": _distribution(records, "full_history"), "trailing_30d": _distribution(records, "30d")}


def calculate_regime_statistics(records: Sequence[Mapping[str, Any]], current: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    valid = [record for record in records if record.get("regime") in REGIME_STATES and record.get("status") == "available"]
    episodes: list[tuple[str, int]] = []
    previous_timestamp: int | None = None
    for record in valid:
        if episodes and episodes[-1][0] == record["regime"] and previous_timestamp is not None and record["timestamp"] - previous_timestamp == DAY_SECONDS:
            episodes[-1] = (episodes[-1][0], episodes[-1][1] + 1)
        else:
            episodes.append((record["regime"], 1))
        previous_timestamp = record["timestamp"]
    total = len(valid)
    current_state = current.get("regime") if current else None
    output = []
    for state in ("low_vol", "normal", "high_vol"):
        lengths = [length for regime, length in episodes if regime == state]
        days = sum(record["regime"] == state for record in valid)
        active = current_state == state
        output.append({
            "regime": state,
            "classified_days": days,
            "empirical_share": _clean(days / total) if total else None,
            "episode_count": len(lengths),
            "average_episode_days": _clean(sum(lengths) / len(lengths)) if lengths else None,
            "maximum_episode_days": max(lengths) if lengths else None,
            "current_episode_days": current.get("persistence_days", 0) if active and current else 0,
            "is_current": active,
        })
    return output


def build_regime_transition_events(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_id: dict[str, Any] = {}
    ids: list[str] = []
    previous: Mapping[str, Any] | None = None
    rank = {"low_vol": 0, "normal": 1, "high_vol": 2}
    for record in records:
        valid = record.get("regime") in REGIME_STATES and record.get("status") == "available"
        if valid and previous and record["timestamp"] - previous["timestamp"] == DAY_SECONDS and record["regime"] != previous["regime"]:
            event_id = f"volatility_market_regimes:{record['timestamp']}:regime_transition:{previous['regime']}:{record['regime']}"
            by_id[event_id] = {
                "event_id": event_id,
                "event_type": "regime_transition",
                "timestamp": record["timestamp"],
                "from_regime": previous["regime"],
                "to_regime": record["regime"],
                "transition_direction": "volatility_expansion" if rank[record["regime"]] > rank[previous["regime"]] else "volatility_contraction",
                "confidence_score": record.get("confidence_score"),
                "data_as_of": record.get("data_as_of"),
            }
            ids.append(event_id)
        previous = record if valid else None
    return {"by_id": by_id, "regime_transition_ids": ids}


def build_technical_events(feature: Mapping[str, Any]) -> dict[str, Any]:
    by_id: dict[str, Any] = {}
    ids: list[str] = []
    for candidate in feature.get("cross_candidates", []):
        timestamp = _timestamp(candidate.get("timestamp"), "technical_event.timestamp")
        first, second = candidate.get("first_series"), candidate.get("second_series")
        direction_value = candidate.get("direction")
        if not isinstance(first, str) or not isinstance(second, str) or direction_value not in {-1, 1}:
            raise ValueError("technical_event:invalid_candidate")
        direction = "bullish" if direction_value == 1 else "bearish"
        event_id = f"volatility_market_regimes:{timestamp}:technical_cross:{first}:{direction}:{second}"
        group = "momentum" if {first, second} & {"macd", "signal", "k", "d"} else "trend"
        event = {"event_id": event_id, "event_type": "technical_cross", "event_group": group,
            "timestamp": timestamp, "indicator_id": first, "comparison_indicator_id": second,
            "direction": direction, "state": f"{first}_{'above' if direction_value == 1 else 'below'}_{second}",
            "indicator_context": {"first_series": first, "second_series": second,
                "previous_difference": candidate.get("previous_difference"), "current_difference": candidate.get("current_difference")},
            "source_path": "features.technical_analysis.cross_candidates"}
        by_id[event_id] = event
        ids.append(event_id)
    return {"by_id": by_id, "technical_cross_ids": ids}


def _source_availability(features: Mapping[str, Any]) -> dict[str, Any]:
    output = {}
    for name in ("realized_volatility", "dvol", "volatility_spread", "daily_regime_basis"):
        feature = features[name]
        output[f"processing.{name}"] = {
            "status": feature.get("status"),
            "reason": feature.get("reason"),
            "records_available": feature.get("records_available"),
            "source_data_as_of": feature.get("source_data_as_of"),
        }
    return output


def classify_volatility_context(features: Mapping[str, Any]) -> dict[str, Any]:
    dvol = features.get("dvol", {}) if isinstance(features.get("dvol"), Mapping) else {}
    spread = features.get("volatility_spread", {}) if isinstance(features.get("volatility_spread"), Mapping) else {}
    current_dvol = dvol.get("current") if isinstance(dvol.get("current"), Mapping) else {}
    current_spread = spread.get("current") if isinstance(spread.get("current"), Mapping) else {}
    dvol_value = _finite(current_dvol.get("close"), "dvol.close") if current_dvol else None
    spread_value = _finite(current_spread.get("spread_7d"), "spread_7d") if current_spread else None
    records = [row for row in dvol.get("records", []) if isinstance(row, Mapping) and row.get("close") is not None]
    recent = [float(row["close"]) for row in records[-30 * 24:]]
    median = None
    if recent:
        ordered = sorted(recent)
        mid = len(ordered) // 2
        median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    if dvol_value is None:
        dvol_state = "unavailable"
    elif median is None:
        dvol_state = "available"
    elif dvol_value > median:
        dvol_state = "above_recent_median"
    elif dvol_value < median:
        dvol_state = "below_recent_median"
    else:
        dvol_state = "at_recent_median"
    spread_state = None if spread_value is None else ("realized_above_implied" if spread_value > 0 else "implied_above_realized" if spread_value < 0 else "balanced")
    status = "available" if dvol_value is not None and spread_value is not None else "partial" if dvol_value is not None or spread_value is not None else "unavailable"
    return {
        "status": status, "reason": None if status == "available" else "dvol_or_spread_unavailable",
        "current": {"dvol": dvol_value, "dvol_recent_median": median, "dvol_state": dvol_state,
                    "spread_7d": spread_value, "spread_state": spread_state,
                    "timestamp": current_dvol.get("timestamp") or current_spread.get("timestamp")},
    }


def evaluate_volatility_market_regimes_classification_quality(
    classifications: Mapping[str, Any], distribution: Mapping[str, Any], events: Mapping[str, Any],
    processing_status: str, errors: Sequence[str] = (), warnings: Sequence[str] = (),
) -> dict[str, Any]:
    required = ["daily_regimes", "volatility_context"]
    groups = {state: [name for name in required if classifications[name].get("status") == state] for state in AVAILABILITY_STATES}
    current = classifications["daily_regimes"].get("current")
    refs_ok = all(event_id in events["by_id"] for event_id in events["regime_transition_ids"])
    all_warnings = sorted(set(warnings))
    if errors or groups["invalid"] or processing_status == "invalid" or not refs_ok:
        status = "invalid"
    elif processing_status != "ok" or groups["partial"] or groups["unavailable"] or not current:
        status = "partial"
    else:
        status = "ok"
    return {
        "status": status,
        "required_classifications": required,
        "available_classifications": groups["available"],
        "partial_classifications": groups["partial"],
        "unavailable_classifications": groups["unavailable"],
        "invalid_classifications": groups["invalid"],
        "current_regime_available": current is not None,
        "confidence_available": bool(current and current.get("confidence_score") is not None),
        "persistence_available": bool(current and current.get("persistence_days") is not None),
        "distribution_available": distribution["full_history"]["status"] == "available",
        "events_complete": refs_ok,
        "warnings": all_warnings,
        "errors": list(errors),
    }


def _context(processing: Mapping[str, Any]) -> dict[str, Any]:
    source = processing.get("context") if isinstance(processing.get("context"), Mapping) else {}
    return {
        "reference_timestamp": source.get("reference_timestamp"),
        "input_execution_timestamp": source.get("input_execution_timestamp"),
        "asset": source.get("asset"),
        "symbol": source.get("symbol"),
        "exchange": source.get("exchange"),
        "base_interval": source.get("base_interval"),
        "parameters": {
            "low_vol_percentile_threshold": LOW_VOL_PERCENTILE_THRESHOLD,
            "high_vol_percentile_threshold": HIGH_VOL_PERCENTILE_THRESHOLD,
            "confidence_high_threshold": CONFIDENCE_HIGH_THRESHOLD,
            "confidence_medium_threshold": CONFIDENCE_MEDIUM_THRESHOLD,
        },
        "classification_policy": {
            "primary_regime_basis": "realized_percentile_rank_30d",
            "confidence_basis": "realized_percentile_boundary_distance",
            "dvol_role": "implied_volatility_context",
            "spread_basis": "realized_minus_implied",
            "distribution_basis": "empirical_classified_day_share",
            "persistence_basis": "consecutive_classified_utc_days",
        },
        "history_policy": {"calculation": "full_processing_history", "presentation": "not_applied_in_classification"},
    }


def _invalid_contract(processing: Any, error: str) -> dict[str, Any]:
    safe = processing if isinstance(processing, Mapping) else {}
    invalid = {"status": "invalid", "reason": "processing_contract_invalid", "records": [], "current": None}
    classifications = {"daily_regimes": deepcopy(invalid), "volatility_context": deepcopy(invalid)}
    distribution = calculate_regime_distribution([])
    events = {"by_id": {}, "regime_transition_ids": [], "technical_cross_ids": []}
    quality = evaluate_volatility_market_regimes_classification_quality(classifications, distribution, events, "invalid", [error])
    return {
        "family": FAMILY,
        "stage": "classification",
        "version": CLASSIFICATION_VERSION,
        "mode": safe.get("mode") if safe.get("mode") in _MODES else "bootstrap",
        "context": _context(safe),
        "source_availability": {},
        "classifications": classifications,
        "summaries": {"regime_distribution": distribution, "regime_statistics": calculate_regime_statistics([])},
        "interpreted_events": events,
        "quality": quality,
    }


class VolatilityMarketRegimesClassifier:
    def classify(self, processing: Any) -> dict[str, Any]:
        try:
            validate_volatility_market_regimes_processing_contract(processing)
            features = processing["features"]
            daily = classify_daily_regime_history(features["daily_regime_basis"])
            volatility_context = classify_volatility_context(features)
            classifications = {"daily_regimes": daily, "volatility_context": volatility_context}
            distribution = calculate_regime_distribution(daily["records"])
            statistics = calculate_regime_statistics(daily["records"], daily["current"])
            events = build_regime_transition_events(daily["records"])
            technical_events = build_technical_events(features.get("technical_analysis", {"cross_candidates": []}))
            events["by_id"].update(technical_events["by_id"])
            events["technical_cross_ids"] = technical_events["technical_cross_ids"]
            warnings = [warning for record in daily["records"] for warning in record.get("warnings", [])]
            quality = evaluate_volatility_market_regimes_classification_quality(
                classifications, distribution, events, processing["quality"]["status"], warnings=warnings,
            )
            result = {
                "family": FAMILY,
                "stage": "classification",
                "version": CLASSIFICATION_VERSION,
                "mode": processing["mode"],
                "context": _context(processing),
                "source_availability": _source_availability(features),
                "classifications": classifications,
                "summaries": {"regime_distribution": distribution, "regime_statistics": statistics},
                "interpreted_events": events,
                "quality": quality,
            }
            json.dumps(result, ensure_ascii=False, allow_nan=False)
            return result
        except (KeyError, TypeError, ValueError) as exc:
            return _invalid_contract(processing, str(exc))


def classify_volatility_market_regimes(processing: Any) -> dict[str, Any]:
    return VolatilityMarketRegimesClassifier().classify(processing)
