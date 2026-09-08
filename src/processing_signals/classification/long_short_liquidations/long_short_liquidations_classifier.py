"""Semantic Classification v0.1 for long/short liquidation Processing output."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import json
import math
from typing import Any

STATUSES = {"available", "partial", "unavailable", "invalid"}
STATUS_FACTOR = {"available": 1., "partial": .7, "unavailable": 0., "invalid": 0.}
CANONICAL_REASONS = {
    "classified_within_threshold_band", "classified_at_lower_boundary", "classified_at_upper_boundary",
    "partial_source_feature", "unavailable_source_feature", "invalid_source_feature", "missing_numeric_value",
    "zero_denominator", "missing_reference_price", "stale_reference_price", "future_reference_price",
    "invalid_reference_price_context", "insufficient_coverage", "insufficient_aligned_points", "unit_mismatch",
    "interval_mismatch", "zero_variance", "conflicting_confirmation_metrics",
    "conflicting_concentration_evidence", "missing_required_classification_input",
    "conflicting_realized_and_estimated_sides", "no_clusters_detected", "classification_not_applicable",
    "missing_processing_feature",
}
REQUIRED = ["realized_side_regime_4h", "realized_side_regime_24h",
            "exchange_concentration_regime", "event_activity_regime_15m"]
OPTIONAL = ["pressure_regime", "realized_side_regime_4h", "realized_side_regime_12h", "estimated_side_regime",
            "aggregate_map_concentration_regime", "estimated_long_concentration_regime",
            "estimated_short_concentration_regime", "cluster_regime", "provider_confirmation_regime",
            "max_pain_proximity_regime", "event_activity_regime_4h", "composite_regime"]


def _invalid(path: str) -> ValueError:
    return ValueError(f"invalid_processing_contract:{path}")


def _validate_json(value: Any, path: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise _invalid(path)
            _validate_json(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_json(item, f"{path}[{index}]")
    elif isinstance(value, float) and not math.isfinite(value):
        raise _invalid(path)
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise _invalid(path)


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _invalid(path)
    if any(not isinstance(key, str) for key in value):
        raise _invalid(path)
    return value


def _at(root: Mapping[str, Any], path: str, *, required: bool = True) -> Any:
    value: Any = root
    walked = []
    for part in path.split("."):
        walked.append(part)
        if not isinstance(value, Mapping) or part not in value:
            if required:
                raise _invalid(".".join(walked))
            return None
        value = value[part]
    return value


def _number(value: Any, path: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise _invalid(path)
    return float(value)


def _status(feature: Mapping[str, Any], path: str) -> str:
    value = feature.get("status")
    if value not in STATUSES:
        raise _invalid(f"{path}.status")
    return value


def _reason(source_status: str, source_reason: Any) -> tuple[str, dict[str, Any]]:
    evidence = {}
    if isinstance(source_reason, str) and source_reason in CANONICAL_REASONS:
        return source_reason, evidence
    if source_reason is not None:
        evidence["source_reason"] = deepcopy(source_reason)
    return ("invalid_source_feature" if source_status == "invalid" else "unavailable_source_feature"), evidence


def _boundary_reason(value: float, boundaries: tuple[float, ...], upper: float | None = None) -> str:
    if value in boundaries:
        return "classified_at_lower_boundary"
    if upper is not None and value == upper:
        return "classified_at_upper_boundary"
    return "classified_within_threshold_band"


def _confidence(status: str, margin: float, coverage: float | None = None) -> float:
    data = 1. if coverage is None else coverage
    if not 0 <= data <= 1:
        raise _invalid("coverage_ratio")
    return round(min(1., max(0., STATUS_FACTOR[status] * data * (.6 + .4 * margin))), 6)


def _atom(path: str, feature: Mapping[str, Any], *, method: str, thresholds_id: str,
          thresholds: Mapping[str, Any], classify: Any, evidence: Mapping[str, Any] | None = None,
          coverage: float | None = None) -> dict[str, Any]:
    feature = _mapping(feature, path)
    source_status = _status(feature, path)
    source_reason = feature.get("reason")
    value = _number(feature.get("value"), f"{path}.value")
    provenance = {"source_path": path, "source_status": source_status, "source_reason": deepcopy(source_reason),
                  "source_provenance": deepcopy(feature.get("provenance", {})), "classification_method": method,
                  "classification_version": "0.1", "thresholds_id": thresholds_id,
                  "confidence_method": "status_x_coverage_x_threshold_margin_v1"}
    base = {"source_path": path, "value": deepcopy(feature.get("value")), "classification": None, "strength": None,
            "status": source_status, "reason": None, "confidence": 0., "thresholds": deepcopy(dict(thresholds)),
            "evidence": deepcopy(dict(evidence or {})), "provenance": provenance}
    if source_status in {"unavailable", "invalid"}:
        base["reason"], extra = _reason(source_status, source_reason)
        base["evidence"].update(extra)
        return base
    if value is None:
        base.update(status="unavailable", reason="missing_numeric_value")
        return base
    classification, strength, margin, reason = classify(value)
    if classification is None:
        base.update(status="invalid", reason="invalid_source_feature")
        return base
    base.update(classification=classification, strength=strength,
                reason="partial_source_feature" if source_status == "partial" else reason,
                confidence=_confidence(source_status, margin, coverage))
    return base


def classify_pressure_regime(feature: Mapping[str, Any], *, source_path: str = "pressure.score") -> dict[str, Any]:
    def classify(value: float) -> tuple[Any, Any, float, str]:
        if not 0 <= value <= 100:
            return None, None, 0., "invalid_source_feature"
        bands = ((0., 25., "low_pressure"), (25., 50., "moderate_pressure"),
                 (50., 75., "high_pressure"), (75., 100., "extreme_pressure"))
        for lower, upper, label in bands:
            if lower <= value < upper or value == upper == 100:
                margin = (value-lower)/(100-lower) if upper == 100 else (upper-value)/(upper-0) if lower == 0 else 2*min(value-lower, upper-value)/(upper-lower)
                return label, None, margin, _boundary_reason(value, (0., 25., 50., 75.), 100.)
        raise AssertionError
    return _atom(source_path, feature, method="pressure_threshold_bands_v1", thresholds_id="pressure_v1",
                 thresholds={"low": [0, 25], "moderate": [25, 50], "high": [50, 75], "extreme": [75, 100]}, classify=classify)


def _imbalance(feature: Mapping[str, Any], path: str, estimated: bool) -> dict[str, Any]:
    def classify(value: float) -> tuple[Any, Any, float, str]:
        magnitude = abs(value)
        if magnitude > 1:
            return None, None, 0., "invalid_source_feature"
        reason = _boundary_reason(magnitude, (0., .1, .3, .6), 1.)
        if magnitude < .1:
            return ("estimated_exposure_balanced" if estimated else "realized_balanced"), "balanced", 1-magnitude/.1, reason
        if magnitude < .3:
            strength, margin = "slight", 2*min(magnitude-.1, .3-magnitude)/.2
        elif magnitude < .6:
            strength, margin = "clear", 2*min(magnitude-.3, .6-magnitude)/.3
        else:
            strength, margin = "strong", (magnitude-.6)/.4
        if estimated:
            label = "estimated_long_exposure_dominant" if value > 0 else "estimated_short_exposure_dominant"
        else:
            label = "realized_long_liquidations_dominant" if value > 0 else "realized_short_liquidations_dominant"
        return label, strength, margin, reason
    evidence = {"side_assignment_method": "spatial_convention_v1", "provider_side_label_supplied": False} if estimated else {}
    return _atom(path, feature, method="symmetric_imbalance_bands_v1", thresholds_id="imbalance_v1",
                 thresholds={"balanced": .1, "slight": [.1, .3], "clear": [.3, .6], "strong": [.6, 1.]},
                 classify=classify, evidence=evidence)


def classify_realized_side_regime(feature: Mapping[str, Any], *, source_path: str = "realized.windows.4h.imbalance") -> dict[str, Any]:
    return _imbalance(feature, source_path, False)


def classify_estimated_side_regime(feature: Mapping[str, Any], *, source_path: str = "maps.aggregated.estimated_side_imbalance") -> dict[str, Any]:
    return _imbalance(feature, source_path, True)


def classify_event_activity_regime(feature: Mapping[str, Any], *, source_path: str = "pressure.components.event_intensity") -> dict[str, Any]:
    def classify(value: float) -> tuple[Any, Any, float, str]:
        if not 0 <= value <= 1:
            return None, None, 0., "invalid_source_feature"
        bands = ((0., .25, "subdued_event_activity"), (.25, .5, "normal_event_activity"),
                 (.5, .75, "elevated_event_activity"), (.75, .9, "high_event_activity"), (.9, 1., "extreme_event_activity"))
        for lower, upper, label in bands:
            if lower <= value < upper or value == upper == 1:
                margin = (upper-value)/upper if lower == 0 else (value-lower)/(1-lower) if upper == 1 else 2*min(value-lower, upper-value)/(upper-lower)
                return label, None, margin, _boundary_reason(value, (0., .25, .5, .75, .9), 1.)
        raise AssertionError
    metadata = deepcopy(feature.get("metadata", {})) if isinstance(feature, Mapping) else {}
    coverage = metadata.get("coverage_ratio") if isinstance(metadata, Mapping) else None
    coverage = _number(coverage, f"{source_path}.metadata.coverage_ratio") if coverage is not None else None
    return _atom(source_path, feature, method="normalized_event_percentile_bands_v1", thresholds_id="event_intensity_v1",
                 thresholds={"subdued": [0, .25], "normal": [.25, .5], "elevated": [.5, .75], "high": [.75, .9], "extreme": [.9, 1]},
                 classify=classify, evidence={"baseline_metadata": metadata}, coverage=coverage)


def classify_concentration_regime(feature: Mapping[str, Any], *, source_path: str) -> dict[str, Any]:
    feature = _mapping(feature, source_path)
    wrapped = {"value": feature.get("top3_share"), "status": feature.get("status"), "reason": feature.get("reason"),
               "provenance": feature.get("provenance", {})}
    top1 = _number(feature.get("top1_share"), f"{source_path}.top1_share")
    hhi = _number(feature.get("hhi"), f"{source_path}.hhi")
    effective = feature.get("effective_exchange_count", feature.get("effective_bucket_count"))
    if effective is not None:
        _number(effective, f"{source_path}.effective_count")
    if hhi is not None and not 0 <= hhi <= 1:
        raise _invalid(f"{source_path}.hhi")
    hhi_band = None if hhi is None else "low_hhi" if hhi < .15 else "moderate_hhi" if hhi < .25 else "high_hhi"
    evidence = {"top1_share": deepcopy(top1), "top3_share": deepcopy(feature.get("top3_share")), "hhi": deepcopy(hhi),
                "hhi_band": hhi_band, "effective_count": deepcopy(effective)}
    def classify(value: float) -> tuple[Any, Any, float, str]:
        if not 0 <= value <= 1:
            return None, None, 0., "invalid_source_feature"
        bands = ((0., .5, "dispersed", "low_hhi"), (.5, .7, "moderately_concentrated", "moderate_hhi"),
                 (.7, .85, "concentrated", "high_hhi"), (.85, 1., "highly_concentrated", "high_hhi"))
        for lower, upper, label, expected in bands:
            if lower <= value < upper or value == upper == 1:
                margin = (upper-value)/upper if lower == 0 else (value-lower)/(1-lower) if upper == 1 else 2*min(value-lower, upper-value)/(upper-lower)
                reason = _boundary_reason(value, (0., .5, .7, .85), 1.)
                if hhi_band is not None and hhi_band != expected:
                    reason = "conflicting_concentration_evidence"
                return label, None, margin, reason
        raise AssertionError
    return _atom(source_path, wrapped, method="top3_concentration_bands_v1", thresholds_id="concentration_v1",
                 thresholds={"top3": [.5, .7, .85], "hhi": [.15, .25]}, classify=classify, evidence=evidence)


def classify_cluster_regime(clusters: Mapping[str, Any], *, source_path: str = "maps.aggregated.clusters") -> dict[str, Any]:
    clusters = _mapping(clusters, source_path)
    sides, statuses, reasons = {}, [], []
    for side in ("estimated_long", "estimated_short"):
        raw = clusters.get(side)
        if isinstance(raw, list):
            statuses.append("available")
            sides[side] = raw
        elif isinstance(raw, Mapping):
            statuses.append(_status(raw, f"{source_path}.{side}"))
            reasons.append(raw.get("reason"))
            items = raw.get("items")
            if not isinstance(items, list):
                raise _invalid(f"{source_path}.{side}.items")
            sides[side] = items
        else:
            raise _invalid(f"{source_path}.{side}")
    status = "invalid" if "invalid" in statuses else "unavailable" if "unavailable" in statuses else "partial" if "partial" in statuses else "available"
    base = {"source_path": source_path, "value": deepcopy(sides), "classification": None, "strength": None, "status": status,
            "reason": None, "confidence": 0., "thresholds": {"strong_share": .25, "strong_distance_bps": 100,
            "moderate_share": .1, "moderate_distance_bps": 250}, "evidence": {}, "provenance": {"source_path": source_path,
            "source_status": status, "source_reason": deepcopy(reasons), "source_provenance": {},
            "classification_method": "cluster_presence_strength_v1", "classification_version": "0.1",
            "thresholds_id": "clusters_v1", "confidence_method": "status_x_coverage_x_threshold_margin_v1"}}
    if status in {"invalid", "unavailable"}:
        base["reason"] = "invalid_source_feature" if status == "invalid" else "unavailable_source_feature"
        return base
    strengths, margins = {}, []
    for side, items in sides.items():
        if not items:
            continue
        main = _mapping(items[0], f"{source_path}.{side}[0]")
        share = _number(main.get("share_of_side"), f"{source_path}.{side}[0].share_of_side")
        distance = _number(main.get("nearest_distance_bps"), f"{source_path}.{side}[0].nearest_distance_bps")
        if share is None or distance is None or not 0 <= share <= 1:
            base.update(status="invalid", reason="invalid_source_feature")
            return base
        distance = abs(distance)
        if share >= .25 and distance <= 100:
            strength, margin = "strong", min((share-.25)/.75, (100-distance)/100)
        elif share >= .1 and distance <= 250:
            strength, margin = "moderate", min((share-.1)/.9, (250-distance)/250)
        else:
            strength, margin = "weak", 0.
        strengths[side] = strength
        margins.append(margin)
    has_long, has_short = bool(sides["estimated_long"]), bool(sides["estimated_short"])
    label = "bilateral_clusters" if has_long and has_short else "long_side_clustered" if has_long else "short_side_clustered" if has_short else "no_spatial_clusters"
    rank = {"weak": 0, "moderate": 1, "strong": 2}
    strength = max(strengths.values(), key=rank.get) if strengths else None
    base.update(classification=label, strength=strength, reason="partial_source_feature" if status == "partial" else
                "no_clusters_detected" if not strengths else "classified_within_threshold_band",
                confidence=_confidence(status, min(margins) if margins else 1.), evidence={"side_strengths": strengths,
                "main_clusters": {side: deepcopy(items[0]) for side, items in sides.items() if items}})
    return base


def _confirmation_required_status(metrics: Mapping[str, Mapping[str, Any]], source_path: str) -> tuple[str, Any, dict[str, Any]]:
    names = ("pearson_correlation", "median_absolute_percentage_error", "aligned_point_count", "coverage_ratio")
    statuses, source_reasons, evidence = {}, {}, {}
    for name in names:
        item = _mapping(metrics.get(name), f"{source_path}.{name}")
        status = _status(item, f"{source_path}.{name}")
        reason = item.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise _invalid(f"{source_path}.{name}.reason")
        statuses[name] = status
        evidence[name] = {"status": status, "reason": deepcopy(reason)}
        if reason is not None:
            source_reasons[name] = deepcopy(reason)
    aggregate = ("invalid" if "invalid" in statuses.values() else
                 "unavailable" if "unavailable" in statuses.values() else
                 "partial" if "partial" in statuses.values() else "available")
    degraded = [name for name in names if statuses[name] == aggregate]
    reason = next((metrics[name].get("reason") for name in degraded if metrics[name].get("reason") in CANONICAL_REASONS), None)
    if aggregate in {"invalid", "unavailable"} and reason is None:
        reason = "invalid_source_feature" if aggregate == "invalid" else "unavailable_source_feature"
    return aggregate, reason, {"required_metric_statuses": evidence, "source_reasons": source_reasons}


def classify_provider_confirmation_regime(feature: Mapping[str, Any], *, source_path: str) -> dict[str, Any]:
    feature = _mapping(feature, source_path)
    status, reason, status_evidence = _confirmation_required_status(feature, source_path)
    def metric(name: str) -> float | None:
        item = _mapping(feature.get(name), f"{source_path}.{name}")
        if item["status"] in {"invalid", "unavailable"}:
            return None
        value = _number(item.get("value"), f"{source_path}.{name}.value")
        if value is None:
            raise _invalid(f"{source_path}.{name}.value")
        return value
    corr, mape, aligned, coverage = (metric(name) for name in
        ("pearson_correlation", "median_absolute_percentage_error", "aligned_point_count", "coverage_ratio"))
    if status in {"available", "partial"}:
        assert corr is not None and mape is not None and aligned is not None and coverage is not None
        if not -1 <= corr <= 1:
            raise _invalid(f"{source_path}.pearson_correlation.value")
        if mape < 0:
            raise _invalid(f"{source_path}.median_absolute_percentage_error.value")
        raw_aligned = feature["aligned_point_count"]["value"]
        if isinstance(raw_aligned, bool) or not isinstance(raw_aligned, int) or raw_aligned < 0:
            raise _invalid(f"{source_path}.aligned_point_count.value")
        if not 0 <= coverage <= 1:
            raise _invalid(f"{source_path}.coverage_ratio.value")
    wrapped = {"value": corr if status in {"available", "partial"} else None, "status": status, "reason": reason,
               "provenance": feature.get("provenance", {})}
    def classify(_: float) -> tuple[Any, Any, float, str]:
        assert corr is not None and mape is not None
        if corr >= .7 and mape <= .25:
            return "provider_aligned", None, min((corr-.7)/.3, (.25-mape)/.25), "classified_within_threshold_band"
        if corr < .3 or mape > .5:
            margins = []
            if corr < .3:
                margins.append((.3-corr)/1.3)
            if mape > .5:
                margins.append((mape-.5)/(mape+.5))
            return "provider_divergent", None, min(margins), "classified_within_threshold_band"
        return "provider_mixed", None, min(abs(corr-.3)/.4 if corr <= .7 else 1., abs(mape-.25)/.25 if mape <= .5 else 1.), "classified_within_threshold_band"
    return _atom(source_path, wrapped, method="provider_confirmation_corr_mape_v1", thresholds_id="confirmation_v1",
                 thresholds={"aligned_correlation": .7, "aligned_mape": .25, "divergent_correlation": .3, "divergent_mape": .5},
                 classify=classify, evidence={"pearson_correlation": corr, "median_absolute_percentage_error": mape,
                 "aligned_point_count": aligned, "coverage_ratio": coverage, **status_evidence},
                 coverage=coverage if status in {"available", "partial"} else None)


def classify_max_pain_proximity_regime(feature: Mapping[str, Any], *, source_path: str = "maps.max_pain.provider_price_difference_bps") -> dict[str, Any]:
    def classify(value: float) -> tuple[Any, Any, float, str]:
        distance = abs(value)
        if distance <= 50:
            return "max_pain_very_near", None, (50-distance)/50, _boundary_reason(distance, (0,), 50)
        if distance <= 150:
            return "max_pain_near", None, 2*min(distance-50, 150-distance)/100, _boundary_reason(distance, (50,), 150)
        if distance <= 500:
            return "max_pain_moderate_distance", None, 2*min(distance-150, 500-distance)/350, _boundary_reason(distance, (150,), 500)
        return "max_pain_far", None, (distance-500)/distance, "classified_within_threshold_band"
    return _atom(source_path, feature, method="max_pain_distance_bands_v1", thresholds_id="max_pain_v1",
                 thresholds={"very_near": 50, "near": 150, "moderate": 500}, classify=classify)


def classify_processing_quality(quality: Mapping[str, Any]) -> dict[str, Any]:
    quality = _mapping(quality, "quality")
    status = _status(quality, "quality")
    return {"status": status, "warnings": deepcopy(quality.get("warnings", [])), "errors": deepcopy(quality.get("errors", [])),
            "confidence": STATUS_FACTOR[status]}


def _not_applicable(path: str | None, reason: str, *, evidence: Mapping[str, Any] | None = None, value: Any = None) -> dict[str, Any]:
    return {"source_path": path, "value": deepcopy(value), "classification": None, "strength": None,
            "status": "unavailable", "reason": reason, "confidence": 0., "thresholds": {},
            "evidence": deepcopy(dict(evidence or {})), "provenance": {}}


def _classification_quality(processing_status: str, atoms: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    required = {name: atoms[name] for name in REQUIRED}
    optional = {name: atoms[name] for name in OPTIONAL}
    missing = [name for name, atom in required.items() if atom is None]
    invalid = [name for name, atom in atoms.items() if atom and atom["status"] == "invalid"]
    partial = [name for name, atom in atoms.items() if atom and atom["status"] == "partial"]
    unavailable = [name for name, atom in atoms.items() if atom and atom["status"] == "unavailable"]
    if processing_status == "invalid" or missing or any(required[name]["status"] == "invalid" for name in required):
        status = "invalid"
    elif processing_status == "unavailable":
        status = "unavailable"
    elif any(atom["status"] == "unavailable" for atom in required.values()):
        status = "partial" if any(atom["status"] in {"available", "partial"} for atom in required.values()) else "unavailable"
    elif any(atom["status"] == "partial" for atom in required.values()):
        status = "partial"
    else:
        status = "available"
    return {"status": status, "required_classifications": list(required), "optional_classifications": list(optional),
            "missing_classifications": missing, "invalid_classifications": invalid, "partial_classifications": partial,
            "unavailable_classifications": unavailable, "warnings": [],
            "errors": ["missing_required_classification_input"] if missing else []}


def _blocked_result(timestamp: int, configuration: Mapping[str, Any], quality: Mapping[str, Any]) -> dict[str, Any]:
    status = quality["status"]
    reason = "invalid_source_feature" if status == "invalid" else "unavailable_source_feature"
    def atom(path=None):
        return _not_applicable(path, reason)
    classifications = {"pressure": atom("pressure.score"),
        "realized_side": {window: atom(f"realized.windows.{window}.imbalance.value") for window in ("4h", "12h", "24h")},
        "estimated_side": atom("maps.aggregated.estimated_side_imbalance.value"),
        "events": {"15m": atom("pressure.components.event_intensity.value"),
                   "4h": _not_applicable("events.aggregate.4h.event_usd_total", "missing_processing_feature")},
        "concentration": {"exchanges": atom("exchange_distribution.concentration.top3_share"),
            "aggregate_map": atom("maps.aggregated.concentration.complete_map.top3_share"),
            "estimated_long": atom("maps.aggregated.concentration.estimated_long.top3_share"),
            "estimated_short": atom("maps.aggregated.concentration.estimated_short.top3_share")},
        "clusters": atom("maps.aggregated.clusters"), "confirmations": {},
        "max_pain": atom("maps.max_pain.provider_price_difference_bps"),
        "composite_regime": _not_applicable(None, "classification_not_applicable",
                                             evidence={"implementation_version": None, "approved": False})}
    atoms = {name: atom() for name in REQUIRED + OPTIONAL}
    return {"family": "long_short_liquidations", "stage": "classification", "reference_timestamp": timestamp,
            "configuration": deepcopy(dict(configuration)), "source_processing_status": {"status": status,
            "warnings": deepcopy(quality.get("warnings", [])), "errors": deepcopy(quality.get("errors", []))},
            "classifications": classifications, "quality": _classification_quality(status, atoms)}


def classify_long_short_liquidations(processing_contract: Mapping[str, Any], *,
                                     config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    contract = _mapping(processing_contract, "root")
    if contract.get("family") != "long_short_liquidations":
        raise _invalid("family")
    if contract.get("stage") != "processing":
        raise _invalid("stage")
    timestamp = contract.get("reference_timestamp")
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp <= 0:
        raise _invalid("reference_timestamp")
    _validate_json(contract, "root")
    _validate_json(config, "config")
    configuration = deepcopy(dict(_mapping(config, "config"))) if config is not None else {}
    quality = _mapping(_at(contract, "quality"), "quality")
    processing_quality = classify_processing_quality(quality)
    if processing_quality["status"] in {"invalid", "unavailable"}:
        result = _blocked_result(timestamp, configuration, quality)
        json.dumps(result, ensure_ascii=False, allow_nan=False)
        return result
    pressure = _mapping(_at(contract, "pressure"), "pressure")
    pressure_feature = {"value": pressure.get("score"), "status": pressure.get("status"), "reason": pressure.get("reason"), "provenance": pressure.get("provenance", {})}
    realized = {}
    for window in ("4h", "12h", "24h"):
        feature = _mapping(_at(contract, f"realized.windows.{window}.imbalance"), f"realized.windows.{window}.imbalance")
        atom = classify_realized_side_regime(feature, source_path=f"realized.windows.{window}.imbalance.value")
        coverage = _number(_at(contract, f"realized.windows.{window}.coverage_ratio"), f"realized.windows.{window}.coverage_ratio")
        if atom["status"] in {"available", "partial"}:
            atom["confidence"] = _confidence(atom["status"], (atom["confidence"]/STATUS_FACTOR[atom["status"]]-.6)/.4, coverage)
        atom["evidence"]["coverage_ratio"] = coverage
        realized[window] = atom
    estimated_feature = _mapping(_at(contract, "maps.aggregated.estimated_side_imbalance"), "maps.aggregated.estimated_side_imbalance")
    estimated = classify_estimated_side_regime(estimated_feature)
    event_feature = _mapping(_at(contract, "pressure.components.event_intensity"), "pressure.components.event_intensity")
    event15 = classify_event_activity_regime(event_feature)
    event4_source = _at(contract, "events.aggregate.4h", required=False)
    event4_value = event4_source.get("event_usd_total") if isinstance(event4_source, Mapping) else None
    event4 = _not_applicable("events.aggregate.4h.event_usd_total", "missing_processing_feature", value=event4_value)
    exchange_conc = classify_concentration_regime(_mapping(_at(contract, "exchange_distribution.concentration"), "exchange_distribution.concentration"), source_path="exchange_distribution.concentration")
    map_conc = _mapping(_at(contract, "maps.aggregated.concentration"), "maps.aggregated.concentration")
    def optional_map_concentration(name: str) -> dict[str, Any]:
        path = f"maps.aggregated.concentration.{name}"
        feature = map_conc.get(name)
        if not isinstance(feature, Mapping):
            return _not_applicable(path, "missing_processing_feature")
        return classify_concentration_regime(feature, source_path=path)

    concentrations = {"exchanges": exchange_conc,
        "aggregate_map": optional_map_concentration("complete_map"),
        "estimated_long": optional_map_concentration("estimated_long"),
        "estimated_short": optional_map_concentration("estimated_short")}
    clusters = classify_cluster_regime(_mapping(_at(contract, "maps.aggregated.clusters"), "maps.aggregated.clusters"))
    confirmations = {provider: classify_provider_confirmation_regime(_mapping(feature, f"realized.confirmations.{provider}"), source_path=f"realized.confirmations.{provider}")
                     for provider, feature in _mapping(_at(contract, "realized.confirmations"), "realized.confirmations").items()}
    max_source = _mapping(_at(contract, "maps.max_pain"), "maps.max_pain")
    max_feature = {"value": max_source.get("provider_price_difference_bps"), "status": max_source.get("status"),
                   "reason": max_source.get("reason"), "provenance": max_source.get("provenance", {})}
    max_pain = classify_max_pain_proximity_regime(max_feature)
    max_pain["evidence"].update({key: deepcopy(max_source.get(key)) for key in ("long_distance_bps", "short_distance_bps", "provider_price", "long_max_pain_price", "short_max_pain_price")})
    composite = _not_applicable(None, "classification_not_applicable", evidence={"implementation_version": None, "approved": False})
    classifications = {"pressure": classify_pressure_regime(pressure_feature), "realized_side": realized,
                       "estimated_side": estimated, "events": {"15m": event15, "4h": event4},
                       "concentration": concentrations, "clusters": clusters, "confirmations": confirmations,
                       "max_pain": max_pain, "composite_regime": composite}
    atoms = {"pressure_regime": classifications["pressure"], **{f"realized_side_regime_{key}": value for key, value in realized.items()},
             "estimated_side_regime": estimated, "event_activity_regime_15m": event15, "event_activity_regime_4h": event4,
             "exchange_concentration_regime": concentrations["exchanges"], "aggregate_map_concentration_regime": concentrations["aggregate_map"],
             "estimated_long_concentration_regime": concentrations["estimated_long"], "estimated_short_concentration_regime": concentrations["estimated_short"],
             "cluster_regime": clusters, "provider_confirmation_regime": _not_applicable(None, "classification_not_applicable") if not confirmations else
             {"status": "invalid" if any(v["status"] == "invalid" for v in confirmations.values()) else
                        "unavailable" if any(v["status"] == "unavailable" for v in confirmations.values()) else
                        "partial" if any(v["status"] == "partial" for v in confirmations.values()) else "available"},
             "max_pain_proximity_regime": max_pain, "composite_regime": composite}
    result = {"family": "long_short_liquidations", "stage": "classification", "reference_timestamp": timestamp,
              "configuration": configuration, "source_processing_status": {"status": processing_quality["status"],
              "warnings": processing_quality["warnings"], "errors": processing_quality["errors"]},
              "classifications": classifications, "quality": _classification_quality(processing_quality["status"], atoms)}
    json.dumps(result, ensure_ascii=False, allow_nan=False)
    return result


class LongShortLiquidationsClassifier:
    """Object façade over the pure functional classifier."""

    def __init__(self, *, config: Mapping[str, Any] | None = None):
        _validate_json(config, "config")
        self._config = deepcopy(dict(_mapping(config, "config"))) if config is not None else None

    def classify(self, processing_contract: Mapping[str, Any]) -> dict[str, Any]:
        return classify_long_short_liquidations(processing_contract, config=self._config)
