"""Pure mathematical feature builders for liquidation Processing v0.1."""
from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from statistics import mean, median
from typing import Any

REALIZED_WINDOWS_SECONDS = {"4h": 14400, "12h": 43200, "24h": 86400}
EVENT_WINDOWS_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "4h": 14400, "24h": 86400}
MIN_COMPUTABLE_COVERAGE = .75
MAP_BUCKET_WIDTH_BPS = 25
MAP_CENTRAL_TOLERANCE_BPS = 5
MAP_INTERPOLATION_ENABLED = False
CLUSTER_MAX_EMPTY_BUCKETS = 0
CLUSTER_MIN_BUCKETS = 2
CLUSTER_MIN_SHARE = .03
MAP_PROXIMITY_DECAY_BPS = 100
CONFIRMATION_MIN_ALIGNED_POINTS = 24
EVENT_INTENSITY_MIN_COMPLETE_BINS = 32
PRESSURE_MIN_AVAILABLE_WEIGHT = .70
PRESSURE_WEIGHTS = {"realized_intensity": .30, "realized_acceleration": .15,
                    "event_intensity": .20, "map_proximity": .20,
                    "map_concentration": .10, "imbalance_magnitude": .05}


def _number(value: Any, *, nonnegative: bool = False, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("finite_number_required")
    result = float(value)
    if (nonnegative and result < 0) or (positive and result <= 0):
        raise ValueError("number_out_of_range")
    return result


def window_end_for_hourly(reference_timestamp: int, records: Sequence[Mapping[str, Any]], source_interval_seconds: int = 3600) -> int:
    """Return a closed-data semi-open end on the provider's hourly grid.

    A record timestamp equal to the runtime reference cannot be assumed to
    represent a fully closed future hour.  Use the latest provider grid boundary
    that does not exceed ``reference_timestamp``; this prevents downstream screen
    contracts from receiving a required timestamp in the future.
    """
    reference = int(reference_timestamp)
    candidates = sorted(
        int(record["timestamp"])
        for record in records
        if isinstance(record, Mapping)
        and isinstance(record.get("timestamp"), int)
        and not isinstance(record.get("timestamp"), bool)
        and int(record["timestamp"]) <= reference
    )
    if not candidates:
        return reference
    latest = candidates[-1]
    next_boundary = latest + int(source_interval_seconds)
    return next_boundary if next_boundary <= reference else latest


def aggregate_regular_window(records: Sequence[Mapping[str, Any]], *, window_end: int,
                             window_seconds: int, source_interval_seconds: int = 3600) -> dict[str, Any]:
    start = window_end - window_seconds
    by_timestamp: dict[int, Mapping[str, Any]] = {}
    for record in records:
        timestamp = record.get("timestamp")
        if (isinstance(timestamp, int) and not isinstance(timestamp, bool) and start <= timestamp < window_end
                and (timestamp - start) % source_interval_seconds == 0):
            try:
                _number(record.get("long_liquidation_usd"), nonnegative=True)
                _number(record.get("short_liquidation_usd"), nonnegative=True)
            except ValueError:
                continue
            by_timestamp[timestamp] = record
    expected = window_seconds // source_interval_seconds
    expected_timestamps = [start + i * source_interval_seconds for i in range(expected)]
    observed = len(by_timestamp)
    ratio = observed / expected if expected else 0.0
    status = "available" if ratio == 1 else "partial" if ratio >= MIN_COMPUTABLE_COVERAGE else "unavailable"
    coverage = {"window_start": start, "window_end": window_end, "expected_count": expected,
                "observed_count": observed, "coverage_ratio": ratio,
                "missing_timestamps": [t for t in expected_timestamps if t not in by_timestamp],
                "status": status, "reason": None if status == "available" else
                ("incomplete_coverage" if status == "partial" else "insufficient_coverage")}
    coverage["misaligned_timestamps"] = sorted({r.get("timestamp") for r in records if isinstance(r, Mapping)
                                                  and isinstance(r.get("timestamp"), int) and not isinstance(r.get("timestamp"), bool)
                                                  and start <= r["timestamp"] < window_end
                                                  and (r["timestamp"] - start) % source_interval_seconds != 0})
    if coverage["misaligned_timestamps"] and status == "available":
        coverage["status"], coverage["reason"] = "partial", "misaligned_timestamps_excluded"
        status = "partial"
    if status == "unavailable":
        return {**coverage, "long_total_usd": None, "short_total_usd": None,
                "total_usd": None, "event_count": None,
                "imbalance": {"value": None, "status": "unavailable", "reason": "insufficient_coverage"}}
    long_total = sum(_number(r["long_liquidation_usd"], nonnegative=True) for r in by_timestamp.values())
    short_total = sum(_number(r["short_liquidation_usd"], nonnegative=True) for r in by_timestamp.values())
    return {**coverage, "long_total_usd": long_total, "short_total_usd": short_total,
            "total_usd": long_total + short_total, "event_count": observed,
            "imbalance": realized_imbalance(long_total, short_total, status=status)}


def realized_imbalance(long_total: float, short_total: float, *, status: str = "available") -> dict[str, Any]:
    total = _number(long_total, nonnegative=True) + _number(short_total, nonnegative=True)
    if total == 0:
        return {"value": None, "status": "unavailable", "reason": "zero_total_liquidation"}
    return {"value": (long_total - short_total) / total, "status": status, "reason": None}


def variation(current: Mapping[str, Any], previous: Mapping[str, Any]) -> dict[str, Any]:
    statuses = (current.get("status"), previous.get("status"))
    if any(s not in {"available", "partial"} for s in statuses):
        return {"status": "unavailable", "reason": "insufficient_window_coverage"}
    result: dict[str, Any] = {"status": "partial" if "partial" in statuses else "available", "reason": None}
    for name in ("long", "short", "total"):
        field = "total_usd" if name == "total" else f"{name}_total_usd"
        current_value, previous_value = current[field], previous[field]
        result[name] = {"absolute_change": current_value - previous_value,
                        "relative_change": None if previous_value == 0 else (current_value - previous_value) / previous_value,
                        "relative_change_status": "unavailable" if previous_value == 0 else result["status"],
                        "relative_change_reason": "zero_previous_value" if previous_value == 0 else None}
    return result


def concentration(values: Sequence[float], *, count_name: str = "effective_bucket_count") -> dict[str, Any]:
    valid, invalid_count = [], 0
    for value in values:
        try:
            valid.append(_number(value, nonnegative=True))
        except ValueError:
            invalid_count += 1
    if not valid and invalid_count:
        return {"status": "invalid", "reason": "all_levels_invalid", "invalid_count": invalid_count,
                "top1_share": None, "top3_share": None, "top5_share": None, "hhi": None, count_name: None}
    total = sum(valid)
    if total == 0:
        return {"status": "unavailable", "reason": "zero_level_total" if valid else "empty_levels", "invalid_count": invalid_count, "top1_share": None,
                "top3_share": None, "top5_share": None, "hhi": None, count_name: None}
    shares = sorted((v / total for v in valid), reverse=True)
    hhi = sum(v * v for v in shares)
    return {"status": "partial" if invalid_count else "available", "reason": "some_levels_invalid" if invalid_count else None,
            "invalid_count": invalid_count, "top1_share": sum(shares[:1]),
            "top3_share": sum(shares[:3]), "top5_share": sum(shares[:5]),
            "hhi": hhi, count_name: 1 / hhi}


def build_exchange_distribution(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rows = []
    for record in records:
        try:
            long_value = _number(record["long_liquidation_usd"], nonnegative=True)
            short_value = _number(record["short_liquidation_usd"], nonnegative=True)
            provider = _number(record["liquidation_usd"], nonnegative=True)
        except (KeyError, ValueError):
            continue
        computed = long_value + short_value
        rows.append({"exchange": record.get("exchange"), "exchange_key": record.get("exchange_key"),
                     "long_liquidation_usd": long_value, "short_liquidation_usd": short_value,
                     "computed_total_usd": computed, "provider_total_usd": provider,
                     "provider_total_difference_usd": provider - computed,
                     "provider_total_difference_ratio": None if computed == 0 else (provider - computed) / computed})
    # CoinGlass exchange-list responses may include an aggregate "All" row in
    # addition to exchange rows.  Never count that aggregate a second time when
    # computing exchange shares/concentration.
    aggregate_keys = {"all", "all_exchange", "aggregate"}
    detail_rows = [row for row in rows if str(row.get("exchange_key", "")).lower() not in aggregate_keys]
    basis_rows = detail_rows if detail_rows else rows
    total = sum(r["computed_total_usd"] for r in basis_rows)
    for row in rows:
        key = str(row.get("exchange_key", "")).lower()
        row["exchange_share"] = (None if key in aggregate_keys else (None if total == 0 else row["computed_total_usd"] / total))
    metrics = concentration([r["computed_total_usd"] for r in basis_rows], count_name="effective_exchange_count")
    if total == 0:
        metrics["reason"] = "zero_exchange_total"
    return {"status": metrics["status"], "reason": metrics["reason"], "valid_exchange_total_usd": total,
            "exchanges": sorted(rows, key=lambda r: (-r["computed_total_usd"], str(r["exchange_key"]))), "concentration": metrics}


def bucket_map_levels(levels: Sequence[Mapping[str, Any]], reference_price: float) -> list[dict[str, Any]]:
    reference = _number(reference_price, positive=True)
    grouped: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for level in levels:
        try:
            price = _number(level["price_level"], positive=True)
            _number(level["provider_liquidation_level"], nonnegative=True)
        except (KeyError, ValueError):
            continue
        try:
            relative = ((Decimal(str(price)) / Decimal(str(reference))) - Decimal("1")) * Decimal("10000")
            index = int((relative / Decimal(str(MAP_BUCKET_WIDTH_BPS))).to_integral_value(rounding=ROUND_FLOOR))
        except (InvalidOperation, ZeroDivisionError) as exc:
            raise ValueError("bucket_decimal_calculation_failed") from exc
        grouped[index].append(level)
    result = []
    for index, members in grouped.items():
        lower_bps, upper_bps = index * MAP_BUCKET_WIDTH_BPS, (index + 1) * MAP_BUCKET_WIDTH_BPS
        lower_price, upper_price = reference * (1 + lower_bps / 10000), reference * (1 + upper_bps / 10000)
        breakdown: dict[str, float] = defaultdict(float)
        for member in members:
            if member.get("leverage_ratio") is not None:
                breakdown[str(member["leverage_ratio"])] += float(member["provider_liquidation_level"])
        region = "central" if lower_bps <= MAP_CENTRAL_TOLERANCE_BPS and upper_bps >= -MAP_CENTRAL_TOLERANCE_BPS else (
                 "estimated_long" if upper_bps < -MAP_CENTRAL_TOLERANCE_BPS else "estimated_short")
        result.append({"bucket_index": index, "lower_price": lower_price, "upper_price": upper_price,
                       "center_price": (lower_price + upper_price) / 2, "lower_distance_bps": lower_bps,
                       "upper_distance_bps": upper_bps,
                       "level_total": sum(float(m["provider_liquidation_level"]) for m in members),
                       "member_count": len(members), "leverage_breakdown": dict(sorted(breakdown.items())), "region": region})
    return sorted(result, key=lambda b: b["bucket_index"])


def cumulative_curve(buckets: Sequence[Mapping[str, Any]], side: str) -> list[dict[str, Any]]:
    selected = [b for b in buckets if b.get("region") == side]
    selected.sort(key=lambda b: b["center_price"], reverse=side == "estimated_long")
    total, running = sum(float(b["level_total"]) for b in selected), 0.0
    output = []
    for bucket in selected:
        running += float(bucket["level_total"])
        distance = bucket["upper_distance_bps"] if side == "estimated_long" else bucket["lower_distance_bps"]
        output.append({"price": bucket["center_price"], "distance_bps": distance,
                       "bucket_level": bucket["level_total"], "cumulative_level": running,
                       "cumulative_share": None if total == 0 else running / total})
    return output


def build_clusters(buckets: Sequence[Mapping[str, Any]], side: str) -> list[dict[str, Any]]:
    selected = sorted((b for b in buckets if b.get("region") == side), key=lambda b: b["bucket_index"])
    side_total = sum(float(b["level_total"]) for b in selected)
    groups: list[list[Mapping[str, Any]]] = []
    for bucket in selected:
        if not groups or bucket["bucket_index"] != groups[-1][-1]["bucket_index"] + 1:
            groups.append([])
        groups[-1].append(bucket)
    result = []
    for group in groups:
        total = sum(float(b["level_total"]) for b in group)
        if len(group) < CLUSTER_MIN_BUCKETS or side_total == 0 or total / side_total < CLUSTER_MIN_SHARE:
            continue
        peak = max(group, key=lambda b: (b["level_total"], -b["center_price"]))
        distances = [b["upper_distance_bps"] if side == "estimated_long" else b["lower_distance_bps"] for b in group]
        result.append({"start_price": group[0]["lower_price"], "end_price": group[-1]["upper_price"],
                       "weighted_centroid_price": sum(b["center_price"] * b["level_total"] for b in group) / total,
                       "nearest_distance_bps": min(distances, key=abs), "farthest_distance_bps": max(distances, key=abs),
                       "bucket_count": len(group), "member_count": sum(b["member_count"] for b in group),
                       "total_level": total, "share_of_side": total / side_total,
                       "peak_bucket_price": peak["center_price"], "peak_bucket_level": peak["level_total"]})
    return sorted(result, key=lambda c: (abs(c["nearest_distance_bps"]), -c["total_level"], c["start_price"]))


def build_map_features(levels: Sequence[Mapping[str, Any]], reference_price: float | None,
                       *, reference_reason: str = "missing_reference_price") -> dict[str, Any]:
    valid_levels = []
    for level in levels:
        if not isinstance(level, Mapping):
            continue
        try:
            _number(level.get("price_level"), positive=True)
            _number(level.get("provider_liquidation_level"), nonnegative=True)
            valid_levels.append(level)
        except ValueError:
            continue
    valid_levels.sort(key=lambda level: float(level["price_level"]))
    base_concentration = concentration([level.get("provider_liquidation_level") for level in levels if isinstance(level, Mapping)])
    provenance = {"side_assignment_method": "spatial_convention_v1", "provider_side_label_supplied": False}
    if reference_price is None:
        return {"status": "partial" if valid_levels else "unavailable", "reason": "reference_price_unavailable",
                "provider_levels": list(valid_levels),
                "buckets": {"status": "unavailable", "reason": reference_reason, "items": []},
                "concentration": {"complete_map": base_concentration},
                "spatial": {"status": "unavailable", "reason": reference_reason},
                "estimated_long": {"status": "unavailable", "reason": reference_reason, "level": None},
                "estimated_short": {"status": "unavailable", "reason": reference_reason, "level": None},
                "central": {"status": "unavailable", "reason": reference_reason, "level": None},
                "curves": {"estimated_long": {"status": "unavailable", "reason": reference_reason, "points": []},
                           "estimated_short": {"status": "unavailable", "reason": reference_reason, "points": []}},
                "estimated_side_imbalance": {"value": None, "status": "unavailable", "reason": reference_reason},
                "map_proximity": {"value": None, "status": "unavailable", "reason": reference_reason},
                "clusters": {"estimated_long": {"status": "unavailable", "reason": reference_reason, "items": []},
                             "estimated_short": {"status": "unavailable", "reason": reference_reason, "items": []}},
                "provenance": provenance}
    bucket_items = bucket_map_levels(valid_levels, reference_price)
    bucket_status = "partial" if base_concentration["status"] == "partial" else "available"
    buckets = {"status": bucket_status, "reason": base_concentration.get("reason") if bucket_status == "partial" else None,
               "items": bucket_items}
    long = [b for b in bucket_items if b["region"] == "estimated_long"]
    short = [b for b in bucket_items if b["region"] == "estimated_short"]
    long_total, short_total = sum(b["level_total"] for b in long), sum(b["level_total"] for b in short)
    denominator = long_total + short_total
    imbalance = {"value": None, "status": "unavailable", "reason": "zero_estimated_liquidation_level"} if denominator == 0 else {
        "value": (long_total - short_total) / denominator, "status": "available", "reason": None}
    proximity_denominator = sum(b["level_total"] for b in bucket_items)
    proximity = None if proximity_denominator == 0 else sum(
        b["level_total"] * math.exp(-abs((b["lower_distance_bps"] + b["upper_distance_bps"]) / 2) / MAP_PROXIMITY_DECAY_BPS)
        for b in bucket_items) / proximity_denominator
    return {"status": "available" if bucket_items else "unavailable", "reason": None if bucket_items else "no_valid_levels",
            "provider_levels": list(valid_levels), "buckets": buckets, "estimated_long_level": long_total,
            "estimated_short_level": short_total, "estimated_side_imbalance": imbalance,
            "curves": {"estimated_long": cumulative_curve(bucket_items, "estimated_long"),
                       "estimated_short": cumulative_curve(bucket_items, "estimated_short")},
            "concentration": {"complete_map": base_concentration,
                              "estimated_long": concentration([b["level_total"] for b in long]),
                              "estimated_short": concentration([b["level_total"] for b in short])},
            "clusters": {"estimated_long": build_clusters(bucket_items, "estimated_long"),
                         "estimated_short": build_clusters(bucket_items, "estimated_short")},
            "map_proximity": proximity, "provenance": provenance}


def build_event_intensity(events: Sequence[Mapping[str, Any]], *, current_end: int,
                          coverage_checker: Any) -> dict[str, Any]:
    bin_seconds, baseline_seconds = 900, 86400
    current_start = current_end - bin_seconds
    current_complete = bool(coverage_checker(current_start, current_end))
    current = build_event_window(events, window_end=current_end, window_seconds=bin_seconds,
                                 coverage_complete=current_complete)
    baseline = []
    for offset in range(1, baseline_seconds // bin_seconds + 1):
        end = current_end - offset * bin_seconds
        if coverage_checker(end - bin_seconds, end):
            item = build_event_window(events, window_end=end, window_seconds=bin_seconds, coverage_complete=True)
            baseline.append(item["event_usd_total"])
    value = None if current.get("status") != "available" else empirical_percentile(
        current["event_usd_total"], baseline, EVENT_INTENSITY_MIN_COMPLETE_BINS,
    )
    status = "available" if value is not None else "unavailable"
    reason = None if value is not None else ("current_bin_incomplete" if not current_complete else "insufficient_complete_event_bins")
    return {"value": value, "status": status, "reason": reason,
            "current_window": {"start": current_start, "end": current_end,
                               "event_usd_total": current.get("event_usd_total"), "coverage_complete": current_complete},
            "baseline": {"window_seconds": baseline_seconds, "bin_seconds": bin_seconds,
                         "eligible_bin_count": len(baseline), "minimum_required": EVENT_INTENSITY_MIN_COMPLETE_BINS}}


def build_event_window(events: Sequence[Mapping[str, Any]], *, window_end: int, window_seconds: int,
                       coverage_complete: bool) -> dict[str, Any]:
    start = window_end - window_seconds
    deduplicated = {}
    for event in events:
        if not isinstance(event, Mapping):
            raise ValueError("invalid_event_record:mapping_required")
        timestamp = event.get("timestamp")
        if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp <= 0:
            raise ValueError("invalid_event_record:timestamp")
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("invalid_event_record:event_id")
        try:
            _number(event.get("usd_value"), nonnegative=True)
        except ValueError as exc:
            raise ValueError("invalid_event_record:usd_value") from exc
        if start <= timestamp < window_end:
            deduplicated[event_id] = event
    valid = []
    for event in deduplicated.values():
        try:
            _number(event.get("usd_value"), nonnegative=True)
            valid.append(event)
        except ValueError:
            pass
    coverage = {"coverage_start": start if coverage_complete else None,
                "coverage_end": window_end if coverage_complete else None,
                "expected_window_seconds": window_seconds,
                "covered_window_seconds": window_seconds if coverage_complete else 0,
                "coverage_ratio": 1.0 if coverage_complete else 0.0,
                "missing_intervals": [] if coverage_complete else [{"start": start, "end": window_end}],
                "source_complete": bool(coverage_complete),
                "first_event": min((event["timestamp"] for event in valid), default=None),
                "last_event": max((event["timestamp"] for event in valid), default=None)}
    if not valid and not coverage_complete:
        return {"status": "unavailable", "reason": "incomplete_event_coverage", "is_lower_bound": False,
                "event_count": 0, "coverage": coverage}
    values = [float(e["usd_value"]) for e in valid]
    maximum = max(valid, key=lambda e: (e["usd_value"], e["timestamp"], e["event_id"])) if valid else None
    return {"status": "available" if coverage_complete else "partial", "reason": None if coverage_complete else "incomplete_event_coverage",
            "is_lower_bound": not coverage_complete, "window_start": start, "window_end": window_end,
            "event_count": len(valid), "event_usd_total": sum(values), "event_usd_mean": mean(values) if values else None,
            "event_usd_median": median(values) if values else None, "event_usd_max": max(values) if values else None,
            "max_event": dict(maximum) if maximum else None, "coverage": coverage}


def empirical_percentile(value: float, baseline: Sequence[float], minimum: int) -> float | None:
    valid = [float(v) for v in baseline if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)]
    return None if len(valid) < minimum else sum(v <= value for v in valid) / len(valid)


def build_pressure_score(components: Mapping[str, float | None]) -> dict[str, Any]:
    clean = {key: (float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 1 else None)
             for key, value in components.items() if key in PRESSURE_WEIGHTS}
    missing = [key for key in PRESSURE_WEIGHTS if clean.get(key) is None]
    available_weight = sum(weight for key, weight in PRESSURE_WEIGHTS.items() if clean.get(key) is not None)
    mandatory = all(clean.get(key) is not None for key in ("realized_intensity", "map_proximity"))
    if not mandatory or available_weight < PRESSURE_MIN_AVAILABLE_WEIGHT:
        status, score, reason = "unavailable", None, "mandatory_component_unavailable" if not mandatory else "insufficient_available_weight"
    else:
        score = 100 * sum(PRESSURE_WEIGHTS[k] * clean[k] for k in PRESSURE_WEIGHTS if clean.get(k) is not None) / available_weight
        status, reason = ("available", None) if not missing else ("partial", "components_unavailable")
    return {"components": clean, "configured_weights": dict(PRESSURE_WEIGHTS), "available_weight": available_weight,
            "missing_components": missing, "score": score, "status": status, "reason": reason}


def confirmation(primary: Sequence[Mapping[str, Any]], secondary: Sequence[Mapping[str, Any]], *,
                 primary_fields: tuple[str, str] = ("long_liquidation_usd", "short_liquidation_usd"),
                 secondary_fields: tuple[str, str] = ("long_liquidations_usd", "short_liquidations_usd"),
                 units_match: bool = True, intervals_match: bool = True) -> dict[str, Any]:
    def metric(value: Any, status: str = "available", reason: str | None = None) -> dict[str, Any]:
        return {"value": value, "status": status, "reason": reason}
    if not units_match or not intervals_match:
        reason = "unit_mismatch" if not units_match else "interval_mismatch"
        return {"status": "unavailable", "reason": reason,
                **{name: metric(None, "unavailable", reason) for name in ("aligned_point_count", "coverage_ratio", "mean_absolute_error",
                                                                           "median_absolute_error", "median_absolute_percentage_error",
                                                                           "pearson_correlation")}}
    try:
        left = {r["timestamp"]: sum(_number(r[f], nonnegative=True) for f in primary_fields) for r in primary}
        right = {r["timestamp"]: sum(_number(r[f], nonnegative=True) for f in secondary_fields) for r in secondary
                 if all(r.get(f) is not None for f in secondary_fields)}
    except (KeyError, TypeError, ValueError):
        return {"status": "invalid", "reason": "invalid_confirmation_series",
                **{name: metric(None, "unavailable", "invalid_confirmation_series") for name in ("aligned_point_count", "coverage_ratio",
                    "mean_absolute_error", "median_absolute_error", "median_absolute_percentage_error", "pearson_correlation")}}
    timestamps = sorted(set(left) & set(right))
    pairs = [(left[t], right[t]) for t in timestamps]
    if not pairs:
        return {"status": "unavailable", "reason": "no_aligned_points",
                "aligned_point_count": metric(0), **{name: metric(None, "unavailable", "no_aligned_points") for name in
                ("coverage_ratio", "mean_absolute_error", "median_absolute_error", "median_absolute_percentage_error", "pearson_correlation")}}
    errors = [abs(a - b) for a, b in pairs]
    percentages = [abs(a - b) / abs(a) for a, b in pairs if a != 0]
    pearson, pearson_reason = None, None
    if len(pairs) >= CONFIRMATION_MIN_ALIGNED_POINTS:
        xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
        mx, my = mean(xs), mean(ys)
        vx = sum((x-mx)**2 for x in xs)
        vy = sum((y-my)**2 for y in ys)
        if vx and vy:
            pearson = sum((x-mx)*(y-my) for x, y in pairs) / math.sqrt(vx*vy)
        else:
            pearson_reason = "zero_variance"
    else:
        pearson_reason = "insufficient_aligned_points"
    mape_reason = None if percentages else "no_nonzero_reference_points"
    status = "available" if pearson is not None and percentages else "partial"
    return {"status": status, "reason": None if status == "available" else "some_metrics_unavailable",
            "aligned_point_count": metric(len(pairs)), "coverage_ratio": metric(len(pairs) / max(len(left), 1)),
            "mean_absolute_error": metric(mean(errors)), "median_absolute_error": metric(median(errors)),
            "median_absolute_percentage_error": metric(median(percentages) if percentages else None,
                                                       "available" if percentages else "unavailable", mape_reason),
            "pearson_correlation": metric(pearson, "available" if pearson is not None else "unavailable", pearson_reason)}
