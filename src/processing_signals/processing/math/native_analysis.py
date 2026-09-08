from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
from typing import Any

import numpy as np
import pandas as pd


def finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def aligned(values: Sequence[Any], length: int) -> list[float | None]:
    raw = list(values)
    if len(raw) < length:
        raw = [None] * (length - len(raw)) + raw
    elif len(raw) > length:
        raw = raw[-length:]
    return [finite(value) for value in raw]


def series(values: Sequence[Any]) -> pd.Series:
    return pd.Series([np.nan if finite(v) is None else float(v) for v in values], dtype="float64")


def rolling_zscore(values: Sequence[Any], window: int = 30, min_periods: int | None = None) -> list[float | None]:
    s = series(values)
    minimum = int(min_periods or window)
    mean = s.rolling(window, min_periods=minimum).mean()
    std = s.rolling(window, min_periods=minimum).std(ddof=0).replace(0.0, np.nan)
    out = (s - mean) / std
    return [finite(v) for v in out.tolist()]


def rolling_percentile(values: Sequence[Any], window: int = 90, min_periods: int | None = None) -> list[float | None]:
    s = series(values)
    minimum = int(min_periods or max(5, min(window, 20)))

    def rank(x: np.ndarray) -> float:
        current = x[-1]
        valid = x[np.isfinite(x)]
        if not np.isfinite(current) or len(valid) == 0:
            return np.nan
        return float(np.sum(valid <= current) / len(valid))

    out = s.rolling(window, min_periods=minimum).apply(rank, raw=True)
    return [finite(v) for v in out.tolist()]


def difference(values: Sequence[Any], periods: int = 1) -> list[float | None]:
    out = series(values).diff(periods)
    return [finite(v) for v in out.tolist()]


def pct_change(values: Sequence[Any], periods: int = 1, scale: float = 100.0) -> list[float | None]:
    out = series(values).pct_change(periods=periods, fill_method=None) * float(scale)
    return [finite(v) for v in out.tolist()]


def rolling_mean(values: Sequence[Any], window: int, min_periods: int | None = None) -> list[float | None]:
    out = series(values).rolling(window, min_periods=min_periods or window).mean()
    return [finite(v) for v in out.tolist()]


def rolling_std(values: Sequence[Any], window: int, min_periods: int | None = None) -> list[float | None]:
    out = series(values).rolling(window, min_periods=min_periods or window).std(ddof=0)
    return [finite(v) for v in out.tolist()]


def normalize_01(values: Sequence[Any], window: int = 90) -> list[float | None]:
    s = series(values)
    lo = s.rolling(window, min_periods=max(5, min(window, 20))).min()
    hi = s.rolling(window, min_periods=max(5, min(window, 20))).max()
    den = (hi - lo).replace(0.0, np.nan)
    out = (s - lo) / den
    return [finite(v) for v in out.tolist()]


def rolling_wasserstein(values: Sequence[Any], recent_window: int = 20, reference_window: int = 100) -> list[float | None]:
    """Small dependency-free 1D Wasserstein proxy on equal quantile grids.

    For each point, compare a recent sample with the immediately preceding
    reference sample. Samples are independently sorted and linearly sampled on
    a shared quantile grid. This preserves the intended distribution-shift
    semantics without requiring scipy.
    """
    raw = [finite(v) for v in values]
    output: list[float | None] = [None] * len(raw)
    required = recent_window + reference_window
    for end in range(required - 1, len(raw)):
        ref = [v for v in raw[end - required + 1:end - recent_window + 1] if v is not None]
        recent = [v for v in raw[end - recent_window + 1:end + 1] if v is not None]
        if len(ref) < max(5, reference_window // 2) or len(recent) < max(5, recent_window // 2):
            continue
        n = max(len(ref), len(recent), 20)
        q = np.linspace(0.0, 1.0, n)
        ref_q = np.quantile(np.asarray(ref, dtype=float), q)
        recent_q = np.quantile(np.asarray(recent, dtype=float), q)
        output[end] = float(np.mean(np.abs(ref_q - recent_q)))
    return output


def rolling_empirical_percentile(
    values: Sequence[Any], window: int = 90, *, min_periods: int = 1, scale: float = 100.0
) -> list[float | None]:
    """Rolling empirical CDF rank of the current observation."""
    if window <= 0 or min_periods <= 0:
        raise ValueError("window and min_periods must be positive")
    raw = [finite(v) for v in values]
    output: list[float | None] = []
    for index, current in enumerate(raw):
        sample = [v for v in raw[max(0, index + 1 - window): index + 1] if v is not None]
        if current is None or len(sample) < min_periods:
            output.append(None)
            continue
        output.append(float(scale) * sum(v <= current for v in sample) / len(sample))
    return output


def rolling_normalized_wasserstein(
    values: Sequence[Any], recent_window: int = 30, reference_window: int = 30, *, floor: float = 1e-9
) -> list[float | None]:
    """Dimensionless rolling 1-D Wasserstein distance normalized by reference mean magnitude."""
    distances = rolling_wasserstein(values, recent_window=recent_window, reference_window=reference_window)
    raw = [finite(v) for v in values]
    required = recent_window + reference_window
    output: list[float | None] = [None] * len(raw)
    for end in range(required - 1, len(raw)):
        distance = distances[end]
        reference = [v for v in raw[end - required + 1:end - recent_window + 1] if v is not None]
        if distance is None or not reference:
            continue
        scale_value = max(abs(sum(reference) / len(reference)), float(floor))
        output[end] = float(distance) / scale_value
    return output


def latest(values: Sequence[Any]) -> float | None:
    for value in reversed(values):
        number = finite(value)
        if number is not None:
            return number
    return None


def align_by_timestamp(
    left_timestamps: Sequence[int], right_records: Sequence[Mapping[str, Any]], field: str,
    *, timestamp_field: str = "timestamp",
) -> list[float | None]:
    lookup: dict[int, float] = {}
    for row in right_records:
        try:
            ts = int(row[timestamp_field])
        except (KeyError, TypeError, ValueError):
            continue
        value = finite(row.get(field))
        if value is not None:
            lookup[ts] = value
    return [lookup.get(int(ts)) for ts in left_timestamps]


def interpolated_cross(
    *, previous_timestamp: int, timestamp: int,
    previous_first: Any, previous_second: Any,
    first: Any, second: Any,
) -> dict[str, float | int | None]:
    p1, p2, c1, c2 = map(finite, (previous_first, previous_second, first, second))
    if None in (p1, p2, c1, c2):
        return {"interpolation_fraction": None, "event_timestamp_exact": None, "event_value_exact": None}
    previous_difference = p1 - p2
    current_difference = c1 - c2
    denominator = current_difference - previous_difference
    if denominator == 0:
        fraction = 1.0
    else:
        fraction = -previous_difference / denominator
    fraction = max(0.0, min(1.0, float(fraction)))
    event_timestamp = float(previous_timestamp) + fraction * (float(timestamp) - float(previous_timestamp))
    event_value = p1 + fraction * (c1 - p1)
    return {
        "interpolation_fraction": fraction,
        "event_timestamp_exact": event_timestamp,
        "event_value_exact": event_value,
        "previous_first_value": p1,
        "previous_second_value": p2,
        "previous_difference": previous_difference,
        "current_difference": current_difference,
    }


def support_resistance_levels(
    highs: Sequence[Any], lows: Sequence[Any], closes: Sequence[Any], *, lookback: int = 120, levels: int = 1,
) -> dict[str, Any]:
    """Return the strongest observed support and resistance around spot.

    Local swing extrema are clustered with a tolerance derived from observed
    candle range, then ranked by touches, recency and proximity. V4.1 keeps one
    candidate per side. If a pivot cluster is absent, an actually observed
    completed wick may be used; projected/synthetic offsets are never created.
    """
    h = [finite(v) for v in highs][-lookback:]
    l = [finite(v) for v in lows][-lookback:]
    c = [finite(v) for v in closes][-lookback:]
    valid_close = next((v for v in reversed(c) if v is not None), None)
    if valid_close is None:
        return {"support": [], "resistance": [], "method": "observed_swing_cluster_single", "fallback_used": False, "fallback_type": None, "candidates": {}}

    n = min(len(h), len(l), len(c))
    radius = 2
    candidates: list[tuple[float, int, str]] = []
    for i in range(radius, n - radius):
        low_value = l[i]
        if low_value is not None:
            local_lows = [v for v in l[i - radius:i + radius + 1] if v is not None]
            if local_lows and low_value == min(local_lows):
                candidates.append((float(low_value), i, "support"))
        high_value = h[i]
        if high_value is not None:
            local_highs = [v for v in h[i - radius:i + radius + 1] if v is not None]
            if local_highs and high_value == max(local_highs):
                candidates.append((float(high_value), i, "resistance"))

    observed_ranges = [float(hi - lo) for hi, lo in zip(h, l) if hi is not None and lo is not None and hi >= lo]
    median_range = float(np.median(observed_ranges)) if observed_ranges else 0.0
    tolerance = max(median_range * 0.75, abs(float(valid_close)) * 0.0015, 1e-12)

    clusters: list[dict[str, Any]] = []
    for value, index, kind in candidates:
        compatible = [cluster for cluster in clusters if cluster["kind"] == kind and abs(float(cluster["value"]) - value) <= tolerance]
        if not compatible:
            clusters.append({"value": value, "kind": kind, "touches": 1, "last_index": index, "first_index": index})
            continue
        cluster = min(compatible, key=lambda item: abs(float(item["value"]) - value))
        count = int(cluster["touches"])
        cluster["value"] = (float(cluster["value"]) * count + value) / (count + 1)
        cluster["touches"] = count + 1
        cluster["last_index"] = max(int(cluster["last_index"]), index)

    def score(cluster: Mapping[str, Any]) -> float:
        value = float(cluster["value"])
        touches = int(cluster["touches"])
        last_index = int(cluster["last_index"])
        recency = (last_index + 1) / max(n, 1)
        distance_fraction = abs(float(valid_close) - value) / max(abs(float(valid_close)), 1e-12)
        proximity = math.exp(-distance_fraction / 0.02)
        return 2.0 * math.log1p(touches) + 1.20 * recency + 0.80 * proximity

    supports = [cluster for cluster in clusters if cluster["kind"] == "support" and float(cluster["value"]) < valid_close]
    resistances = [cluster for cluster in clusters if cluster["kind"] == "resistance" and float(cluster["value"]) > valid_close]
    supports.sort(key=lambda cluster: (-score(cluster), valid_close - float(cluster["value"])))
    resistances.sort(key=lambda cluster: (-score(cluster), float(cluster["value"]) - valid_close))

    requested = max(1, int(levels))
    chosen_supports = supports[:requested]
    chosen_resistances = resistances[:requested]
    observed_fallback_used = False
    completed_highs = [float(v) for v in h[:-radius] if v is not None and float(v) > valid_close]
    completed_lows = [float(v) for v in l[:-radius] if v is not None and float(v) < valid_close]
    if not chosen_supports and completed_lows:
        value = max(completed_lows)
        chosen_supports = [{"value": value, "touches": 1, "last_index": max(i for i, v in enumerate(l[:-radius]) if v is not None and float(v) == value)}]
        observed_fallback_used = True
    if not chosen_resistances and completed_highs:
        value = min(completed_highs)
        chosen_resistances = [{"value": value, "touches": 1, "last_index": max(i for i, v in enumerate(h[:-radius]) if v is not None and float(v) == value)}]
        observed_fallback_used = True

    return {
        "support": [float(cluster["value"]) for cluster in chosen_supports],
        "resistance": [float(cluster["value"]) for cluster in chosen_resistances],
        "method": "observed_swing_cluster_single" if requested == 1 else "observed_swing_clusters",
        "fallback_used": observed_fallback_used,
        "fallback_type": "observed_completed_wick" if observed_fallback_used else None,
        "cluster_tolerance": tolerance,
        "candidates": {
            "support": [{"value": float(cluster["value"]), "touches": int(cluster["touches"]), "last_index": int(cluster["last_index"]), "score": score(cluster)} for cluster in chosen_supports],
            "resistance": [{"value": float(cluster["value"]), "touches": int(cluster["touches"]), "last_index": int(cluster["last_index"]), "score": score(cluster)} for cluster in chosen_resistances],
        },
    }

def score_to_probability(score: Any, scale: float = 1.0) -> float | None:
    value = finite(score)
    if value is None:
        return None
    x = max(-20.0, min(20.0, value / max(scale, 1e-12)))
    return float(100.0 / (1.0 + math.exp(-x)))
