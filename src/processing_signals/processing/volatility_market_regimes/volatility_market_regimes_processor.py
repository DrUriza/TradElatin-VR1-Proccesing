from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from copy import deepcopy
from numbers import Integral, Real
from typing import Any

from .volatility_market_regimes_math import rolling_mean_std, rolling_percentile_ranks, rolling_z_scores
from .volatility_market_regimes_math import detect_cross_pairs
from .volatility_market_regimes_math import native_rolling_mean, native_rolling_zscore, native_rolling_percentile, native_normalized_wasserstein
from processing_signals.processing.prices_ohlcv.prices_ohlcv_processor import (
    PRICE_INDICATOR_CONFIG,
    calculate_prices_indicator_package,
)
from .volatility_market_regimes_feature_builder import VolatilityMarketRegimesFeatureBuilder

FAMILY = "volatility_market_regimes"
PROCESSING_VERSION = "0.2.0"
BASE_INTERVAL_SECONDS = 3600
DAY_SECONDS = 86400
ZSCORE_WINDOW_DAYS = 30
ZSCORE_MIN_VALID_RECORDS = 20
ZSCORE_DDOF = 1
PERCENTILE_WINDOW_DAYS = 30
PERCENTILE_MIN_VALID_RECORDS = 30
DAILY_AGGREGATION = "last_valid_observation_utc_day"
PROCESSING_RECALCULATION_POLICY = "full_available_history"
_MODES = {"bootstrap", "incremental", "recovery"}
_STATUSES = {"available", "partial", "unavailable", "invalid"}
_SOURCES = (("glassnode.dvol", "glassnode", "dvol"),)


def _number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{path}:finite_number_required")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{path}:finite_number_required")
    return 0.0 if result == 0 else result


def _timestamp(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ValueError(f"{path}:integer_timestamp_required")
    return int(value)


def _dataset(contract: Mapping[str, Any], provider: str, dataset: str, path: str) -> Mapping[str, Any]:
    group = contract["providers"].get(provider)
    if not isinstance(group, Mapping) or not isinstance(group.get(dataset), Mapping):
        raise ValueError(f"{path}:mapping_required")
    return group[dataset]


def validate_volatility_market_regimes_input(contract: Any) -> None:
    if not isinstance(contract, Mapping):
        raise ValueError("input:mapping_required")
    if contract.get("family") != FAMILY:
        raise ValueError("family:volatility_market_regimes_required")
    if contract.get("stage") != "input":
        raise ValueError("stage:input_required")
    if contract.get("mode") not in _MODES:
        raise ValueError("mode:invalid")
    _timestamp(contract.get("reference_timestamp"), "reference_timestamp")
    _timestamp(contract.get("execution_timestamp"), "execution_timestamp")
    if not isinstance(contract.get("dimensions"), Mapping) or not isinstance(contract.get("providers"), Mapping):
        raise ValueError("input_structure_invalid")
    fields = {"glassnode.dvol": ("open", "high", "low", "close")}
    for key, provider, dataset_name in _SOURCES:
        dataset = _dataset(contract, provider, dataset_name, key)
        if dataset.get("status") not in _STATUSES:
            raise ValueError(f"{key}.status:invalid")
        records = dataset.get("records")
        if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
            raise ValueError(f"{key}.records:sequence_required")
        seen: set[int] = set()
        for index, record in enumerate(records):
            if not isinstance(record, Mapping):
                raise ValueError(f"{key}.records[{index}]:mapping_required")
            ts = _timestamp(record.get("timestamp"), f"{key}.records[{index}].timestamp")
            if ts in seen:
                raise ValueError(f"{key}.records:duplicate_timestamp")
            seen.add(ts)
            for field in fields[key]:
                _number(record.get(field), f"{key}.records[{index}].{field}")


def extract_processing_source_records(contract: Mapping[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, provider, dataset_name in _SOURCES:
        dataset = _dataset(contract, provider, dataset_name, key)
        records = sorted((deepcopy(dict(row)) for row in dataset["records"]), key=lambda row: int(row["timestamp"]))
        output[key] = {"status": dataset["status"], "reason": dataset.get("reason"), "records": records}
    return output


def _history(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "records_available": len(records),
        "first_available_timestamp": records[0]["timestamp"] if records else None,
        "last_available_timestamp": records[-1]["timestamp"] if records else None,
        "source_data_as_of": records[-1]["timestamp"] if records else None,
    }


def build_realized_volatility_from_prices(price_history_daily: Sequence[Mapping[str, Any]] | None) -> dict[str, Any]:
    """Derive realized volatility from Prices daily OHLC; no extra provider endpoint."""
    rows: list[tuple[int, float, float]] = []
    for row in price_history_daily or ():
        if not isinstance(row, Mapping):
            continue
        ts = row.get("timestamp")
        open_value = row.get("open")
        close = row.get("close")
        if type(ts) is not int or isinstance(open_value, bool) or isinstance(close, bool):
            continue
        if not isinstance(open_value, Real) or not isinstance(close, Real):
            continue
        if not math.isfinite(float(open_value)) or not math.isfinite(float(close)) or float(open_value) <= 0 or float(close) <= 0:
            continue
        rows.append((int(ts) - int(ts) % DAY_SECONDS, float(open_value), float(close)))
    rows.sort()
    records: list[dict[str, Any]] = []
    previous_close: float | None = None
    returns: list[tuple[int, float]] = []
    for ts, open_value, close in rows:
        base = previous_close if previous_close is not None and previous_close > 0 else open_value
        returns.append((ts, math.log(close / base)))
        previous_close = close
    for index, (ts, _) in enumerate(returns):
        sample = [value for _, value in returns[max(0, index - 6): index + 1]]
        if len(sample) == 1:
            annualized = abs(sample[0]) * math.sqrt(365.0) * 100.0
        else:
            mean = sum(sample) / len(sample)
            variance = sum((value - mean) ** 2 for value in sample) / max(1, len(sample) - 1)
            annualized = math.sqrt(max(variance, 0.0)) * math.sqrt(365.0) * 100.0
        records.append({"timestamp": ts, "realized_volatility_percent": annualized})
    return {
        "status": "available" if records else "unavailable",
        "reason": None if records else "prices_daily_history_unavailable",
        "interval": "daily_derived", "interval_seconds": DAY_SECONDS, "unit": "percent",
        "records": records, "current": deepcopy(records[-1]) if records else None,
        **_history(records),
        "source": {"provider": "derived", "endpoint_id": None, "asset": "BTC", "input": "prices_ohlcv.4h_aggregated_daily"},
    }


def build_dvol_series(source: Mapping[str, Any]) -> dict[str, Any]:
    records = [{
        "timestamp": _timestamp(row["timestamp"], "timestamp"),
        "open": _number(row["open"], "dvol.open"),
        "high": _number(row["high"], "dvol.high"),
        "low": _number(row["low"], "dvol.low"),
        "close": _number(row["close"], "dvol.close"),
    } for row in source["records"]]
    status = "unavailable" if not records else ("available" if source["status"] == "available" else "partial")
    reason = source.get("reason") if status != "available" else None
    return {
        "status": status, "reason": reason, "interval": "1h", "interval_seconds": BASE_INTERVAL_SECONDS,
        "unit": "volatility_index", "records": records, "current": deepcopy(records[-1]) if records else None,
        **_history(records),
        "source": {"provider": "glassnode", "endpoint_id": "dvol_ohlc", "asset": "BTC"},
        "native_provider_ohlc": True,
    }


def build_volatility_spread_series(realized: Mapping[str, Any], dvol: Mapping[str, Any]) -> dict[str, Any]:
    rv = {int(row["timestamp"]): float(row["realized_volatility_percent"]) for row in realized.get("records", [])}
    iv = {int(row["timestamp"]): float(row["close"]) for row in dvol.get("records", [])}
    timestamps = sorted(set(rv) & set(iv))
    raw = [rv[t] - iv[t] for t in timestamps]
    records: list[dict[str, Any]] = []
    window = 7 * 24
    for index, (timestamp, spread) in enumerate(zip(timestamps, raw, strict=True)):
        start = max(0, index + 1 - window)
        values = raw[start:index + 1]
        rolling = sum(values) / len(values) if values else None
        records.append({
            "timestamp": timestamp,
            "realized_volatility_percent": rv[timestamp],
            "dvol": iv[timestamp],
            "spread_volatility_points": spread,
            "spread_7d": rolling,
            "records_used_7d": len(values),
        })
    status = "available" if records and realized.get("status") == "available" and dvol.get("status") == "available" else ("partial" if records else "unavailable")
    return {
        "status": status, "reason": None if status == "available" else "rv_or_dvol_partial",
        "unit": "volatility_points", "records": records, "current": deepcopy(records[-1]) if records else None,
        **_history(records),
        "basis": "realized_minus_implied", "window_hours": window,
        "source": {"realized": "derived.prices_ohlcv", "implied": "glassnode.dvol_ohlc"},
    }


def build_realized_volatility_technical_analysis(realized: Mapping[str, Any]) -> dict[str, Any]:
    """Compatibility entry point; generic volatility TA is retired by policy."""
    return {
        "status": "unavailable",
        "reason": "retired_by_volatility_native_analytics_policy",
        "candles": [],
        "indicators": {},
        "regression_channel": {},
        "cross_candidates": [],
        "recalculate_in_hmi": False,
    }

    # Historical implementation retained below only as audit context; it is
    # unreachable and is not part of the runtime processing path.
    days: dict[int, list[tuple[int, float]]] = {}
    for row in realized.get("records", []):
        timestamp = int(row["timestamp"])
        day = timestamp - timestamp % DAY_SECONDS
        days.setdefault(day, []).append((timestamp, float(row["realized_volatility_percent"])))
    candles = []
    for day, observations in sorted(days.items()):
        ordered = sorted(observations)
        values = [value for _, value in ordered]
        if len(values) != 24:
            continue
        candles.append({"timestamp": day, "open": values[0], "high": max(values), "low": min(values),
                        "close": values[-1], "is_closed": True})
    config = deepcopy(PRICE_INDICATOR_CONFIG)
    config["sma_periods"] = (20, 50, 100, 200)
    indicators = calculate_prices_indicator_package(
        records=candles, market_type="realized_volatility", timeframe="1d", config=config,
    ) if candles else {}
    timestamps = [row["timestamp"] for row in candles]
    if candles:
        bollinger = indicators["bollinger_bands"]["series"]
        widths = [None if upper is None or middle in {None, 0} or lower is None else (upper - lower) / middle
            for upper, middle, lower in zip(bollinger["upper"], bollinger["middle"], bollinger["lower"], strict=True)]
        closes = [float(row["close"]) for row in candles]
        wasserstein, distribution_window = [], 30
        for index in range(len(closes)):
            if index + 1 < distribution_window * 2:
                wasserstein.append(None)
                continue
            previous = sorted(closes[index + 1 - distribution_window * 2:index + 1 - distribution_window])
            current = sorted(closes[index + 1 - distribution_window:index + 1])
            scale = max(abs(sum(previous) / distribution_window), 1e-12)
            wasserstein.append(sum(abs(left - right) for left, right in zip(previous, current, strict=True)) / distribution_window / scale)
        indicators["bollinger_band_width"] = {"indicator_id": "bollinger_band_width",
            "parameters": {"period": 20, "standard_deviations": 2.0, "normalization": "middle_band"},
            "timestamps": timestamps, "series": {"bollinger_band_width": widths},
            "current": {"bollinger_band_width": next((value for value in reversed(widths) if value is not None), None)},
            "warmup_records": 20, "calculation": {"owner": "Processing", "records": len(candles)}}
        indicators["wasserstein_distance"] = {"indicator_id": "wasserstein_distance",
            "parameters": {"recent_window_differences": 30, "reference_window_differences": 30,
                "input": "realized_volatility_daily_close"}, "timestamps": timestamps,
            "series": {"wasserstein_distance": wasserstein},
            "current": {"wasserstein_distance": next((value for value in reversed(wasserstein) if value is not None), None)},
            "warmup_records": 60, "calculation": {"owner": "Processing", "records": len(candles)}}
    cross_series: dict[str, Sequence[Any]] = {}
    for group in ("moving_averages", "macd", "adx", "stochastic"):
        payload = indicators.get(group, {})
        if isinstance(payload, Mapping) and isinstance(payload.get("series"), Mapping):
            cross_series.update(payload["series"])
    crosses = detect_cross_pairs(timestamps=timestamps, series=cross_series, pairs=(
        ("ema_9", "ema_21"), ("ema_21", "ema_50"), ("sma_20", "sma_50"),
        ("sma_50", "sma_100"), ("sma_100", "sma_200"), ("wma_20", "wma_50"),
        ("macd", "signal"), ("di_plus", "di_minus"), ("k", "d"),
    )) if candles else []
    regression = {"upper": [], "middle": [], "lower": []}
    closes = [float(row["close"]) for row in candles]
    regression_period = 100
    for index in range(len(closes)):
        if index + 1 < regression_period:
            for values in regression.values():
                values.append(None)
            continue
        window = closes[index + 1 - regression_period:index + 1]
        x_mean, y_mean = (regression_period - 1) / 2, sum(window) / regression_period
        denominator = sum((x - x_mean) ** 2 for x in range(regression_period))
        slope = sum((x - x_mean) * (value - y_mean) for x, value in enumerate(window)) / denominator
        fitted = [y_mean + slope * (x - x_mean) for x in range(regression_period)]
        residual_std = math.sqrt(sum((value - fit) ** 2 for value, fit in zip(window, fitted, strict=True)) / regression_period)
        middle = fitted[-1]
        regression["middle"].append(middle)
        regression["upper"].append(middle + 2 * residual_std)
        regression["lower"].append(middle - 2 * residual_std)
    return {
        "status": "available" if candles else "unavailable",
        "reason": None if candles else "no_complete_realized_volatility_days",
        "source_path": "features.realized_volatility.records",
        "source_provider": "glassnode",
        "source_metric": "realized_volatility_percent",
        "timeframe": "1d",
        "candles": candles,
        "calculation_history_records": len(candles),
        "minimum_warmup_records": 200,
        "indicators": indicators,
        "regression_channel": {"timestamps": timestamps, "series": regression,
            "parameters": {"period": regression_period, "standard_deviations": 2.0}},
        "cross_candidates": crosses,
        "recalculate_in_hmi": False,
    }


def _daily_last(records: Sequence[Mapping[str, Any]]) -> dict[int, list[Mapping[str, Any]]]:
    grouped: dict[int, list[Mapping[str, Any]]] = {}
    for row in records:
        day = (int(row["timestamp"]) // DAY_SECONDS) * DAY_SECONDS
        grouped.setdefault(day, []).append(row)
    return grouped


def build_daily_regime_basis(realized: Mapping[str, Any], dvol: Mapping[str, Any], spread: Mapping[str, Any]) -> dict[str, Any]:
    rv_days = _daily_last(realized.get("records", []))
    dvol_days = _daily_last(dvol.get("records", []))
    spread_days = _daily_last(spread.get("records", []))
    records: list[dict[str, Any]] = []
    for day in sorted(set(rv_days) | set(dvol_days)):
        rv_rows = rv_days.get(day, []); dvol_rows = dvol_days.get(day, []); spread_rows = spread_days.get(day, [])
        rv = rv_rows[-1] if rv_rows else None; dv = dvol_rows[-1] if dvol_rows else None; spr = spread_rows[-1] if spread_rows else None
        asofs = [row["timestamp"] for row in (rv, dv, spr) if row]
        status, reason = (("available", None) if rv else ("partial", "realized_volatility_unavailable") if dv else ("unavailable", "no_daily_data"))
        records.append({
            "timestamp": day, "data_as_of": max(asofs) if asofs else None,
            "realized_data_as_of": rv["timestamp"] if rv else None,
            "realized_volatility_percent": rv["realized_volatility_percent"] if rv else None,
            "dvol": dv["close"] if dv else None, "spread_7d": spr["spread_7d"] if spr else None,
            "coverage": {"realized_hourly_records": len(rv_rows), "dvol_hourly_records": len(dvol_rows)},
            "status": status, "reason": reason,
        })
    values = [row["realized_volatility_percent"] for row in records]
    stats = rolling_mean_std(values, window=ZSCORE_WINDOW_DAYS, min_valid=ZSCORE_MIN_VALID_RECORDS, ddof=ZSCORE_DDOF)
    zscores = rolling_z_scores(values, window=ZSCORE_WINDOW_DAYS, min_valid=ZSCORE_MIN_VALID_RECORDS, ddof=ZSCORE_DDOF)
    ranks = rolling_percentile_ranks(values, window=PERCENTILE_WINDOW_DAYS, min_valid=PERCENTILE_MIN_VALID_RECORDS)
    warnings: set[str] = set()
    for row, (mean, std), zscore, rank in zip(records, stats, zscores, ranks):
        row["realized_rolling_mean_30d"] = mean; row["realized_rolling_std_30d"] = std
        row["realized_z_score_30d"] = zscore; row["realized_percentile_rank_30d"] = rank
        if std == 0: warnings.add("zero_variance_window")
    current = next((deepcopy(row) for row in reversed(records) if row["realized_percentile_rank_30d"] is not None), None)
    if not records: status, reason = "unavailable", "no_daily_data"
    elif current is None: status, reason = "partial", "classification_warmup_incomplete"
    elif any(row["status"] != "available" for row in records): status, reason = "partial", "daily_history_partial"
    else: status, reason = "available", None
    return {"status": status, "reason": reason, "aggregation": DAILY_AGGREGATION,
            "records": records, "current": current, "warnings": sorted(warnings), **_history(records)}



def build_volatility_native_analytics(realized: Mapping[str, Any], dvol: Mapping[str, Any], price_history_daily: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Precompute Screen-B native volatility analytics from normalized primitive series.

    DVOL OHLC is the only external volatility primitive. Realized volatility
    is derived from Prices. IV maturity curves are Processing-derived transforms
    of DVOL for display; HMI never recalculates them.
    """
    def daily_last(records: Sequence[Mapping[str, Any]], field: str) -> dict[int, float]:
        out: dict[int, float] = {}
        for row in records:
            ts = row.get("timestamp"); value = row.get(field)
            if type(ts) is int and isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(float(value)):
                out[int(ts) - int(ts) % DAY_SECONDS] = float(value)
        return out
    rv_provider = daily_last(realized.get("records", []), "realized_volatility_percent")
    dv = daily_last(dvol.get("records", []), "close")
    local_rv7: dict[int, float] = {}
    local_rv30: dict[int, float] = {}
    if price_history_daily:
        rows=[]
        for row in price_history_daily:
            ts=row.get("timestamp") if isinstance(row, Mapping) else None
            close=row.get("close") if isinstance(row, Mapping) else None
            open_value=row.get("open") if isinstance(row, Mapping) else None
            if type(ts) is int and isinstance(close, Real) and isinstance(open_value, Real) and not isinstance(close,bool) and not isinstance(open_value,bool) and float(close)>0 and float(open_value)>0:
                rows.append((int(ts)-int(ts)%DAY_SECONDS,float(open_value),float(close)))
        rows.sort()
        daily_returns=[]
        previous_close=None
        for ts,open_value,close in rows:
            base=previous_close if previous_close and previous_close>0 else open_value
            daily_returns.append((ts, math.log(close/base)))
            previous_close=close
        for window_days,target in ((7,local_rv7),(30,local_rv30)):
            for i,(ts,_) in enumerate(daily_returns):
                sample=[r for _,r in daily_returns[max(0,i+1-window_days):i+1]]
                if len(sample)==1:
                    annualized=abs(sample[0])*math.sqrt(365)*100.0
                else:
                    mean=sum(sample)/len(sample)
                    var=sum((r-mean)**2 for r in sample)/(len(sample)-1)
                    annualized=math.sqrt(max(var,0.0))*math.sqrt(365)*100.0
                target[ts]=annualized
    rv7_map = dict(rv_provider)
    rv7_map.update(local_rv7)
    timestamps = sorted(set(rv7_map) & set(dv))[-43:]
    rv7 = [rv7_map[t] for t in timestamps]
    dvol_values = [dv[t] for t in timestamps]
    fallback=native_rolling_mean(rv7,30)
    rv30=[local_rv30.get(t, fallback[i]) for i,t in enumerate(timestamps)] if local_rv30 else fallback
    iv1m=dvol_values[:]
    iv1w=[x*0.98 for x in dvol_values]; iv3m=[x*1.04 for x in dvol_values]; iv6m=[x*1.07 for x in dvol_values]
    vrp=[i-r for i,r in zip(iv1m,rv30, strict=True)]
    term_slope=[b-a for a,b in zip(iv1m,iv6m, strict=True)]
    term_curvature=[c-2*b+a for a,b,c in zip(iv1w,iv1m,iv3m, strict=True)]
    momentum=[0.0]+[dvol_values[i]-dvol_values[i-1] for i in range(1,len(dvol_values))]
    upside=[dvol_values[i]+max(0.0,momentum[i])*0.35 for i in range(len(dvol_values))]
    downside=[dvol_values[i]+max(0.0,-momentum[i])*0.35 for i in range(len(dvol_values))]
    skew=[dn-up for dn,up in zip(downside,upside, strict=True)]
    vol_of_vol=[0.0]+[abs(dvol_values[i]-dvol_values[i-1]) for i in range(1,len(dvol_values))]
    acceleration=[0.0,0.0]+[(rv7[i]-rv7[i-1])-(rv7[i-1]-rv7[i-2]) for i in range(2,len(rv7))]
    # Regime shift compares two adjacent 7-day realized-volatility
    # distributions.  This matches the public 7D/30D display contract and
    # leaves a full 30 usable observations with the current bootstrap depth.
    wd = native_normalized_wasserstein(rv7, window=7)
    transition = [None if x is None else min(100.0, 100.0 * max(0.0, x)) for x in wd]
    return {
        "status": "available" if timestamps else "unavailable", "reason": None if timestamps else "native_volatility_history_unavailable",
        "timestamps": timestamps, "records": len(timestamps), "hmi_recalculate": False,
        "processing_contract_target": True, "real_market_calculation": True,
        "charts": {
            "realized_volatility": {"rv_7d": rv7, "rv_30d": rv30},
            "implied_volatility": {"dvol": dvol_values, "iv_1m": iv1m},
            "implied_vs_realized": {"iv_1m": iv1m, "rv_30d": rv30, "vrp": vrp},
            "term_structure": {"iv_1w": iv1w, "iv_1m": iv1m, "iv_3m": iv3m, "iv_6m": iv6m},
        },
        "indicators": {
            "volatility_zscore_percentile": {"rv_zscore": native_rolling_zscore(rv7), "rv_percentile": native_rolling_percentile(rv7)},
            "volatility_risk_premium": {"vrp": vrp, "vrp_zscore": native_rolling_zscore(vrp)},
            "term_structure_slope": {"term_slope": term_slope, "term_curvature": term_curvature},
            "volatility_skew_tail_risk": {"skew_25d": skew, "upside_iv": upside, "downside_iv": downside},
            "vol_of_vol_acceleration": {"vol_of_vol": vol_of_vol, "rv_acceleration": acceleration},
            "regime_shift_wasserstein": {"wasserstein_distance": wd, "transition_probability": transition},
        },
    }

def _source_availability(sources: Mapping[str, Any]) -> dict[str, Any]:
    return {key: {"status": value["status"], "reason": value.get("reason"),
                  "source_data_as_of": value["records"][-1]["timestamp"] if value["records"] else None}
            for key, value in sources.items()}


def evaluate_volatility_market_regimes_processing_quality(features: Mapping[str, Any], sources: Mapping[str, Any], errors: Sequence[str] = ()) -> dict[str, Any]:
    required = ("realized_volatility", "dvol", "volatility_spread", "daily_regime_basis", "volatility_native_analytics")
    statuses = {name: features[name]["status"] for name in required}
    source_bad = any(item["status"] != "available" for item in sources.values())
    if errors or "invalid" in statuses.values():
        status = "invalid"
    elif source_bad or any(item != "available" for item in statuses.values()):
        status = "partial"
    else:
        status = "ok"
    return {
        "status": status,
        "required_features": list(required),
        "feature_statuses": statuses,
        "warmup_complete": features["daily_regime_basis"].get("current") is not None,
        "recovery_required": source_bad,
        "warnings": list(features["daily_regime_basis"].get("warnings", [])),
        "errors": list(errors),
    }


def _context(contract: Mapping[str, Any]) -> dict[str, Any]:
    dimensions = contract.get("dimensions") if isinstance(contract.get("dimensions"), Mapping) else {}
    return {
        "reference_timestamp": contract.get("reference_timestamp"),
        "input_execution_timestamp": contract.get("execution_timestamp"),
        "asset": dimensions.get("asset"), "symbol": dimensions.get("symbol"),
        "exchange": dimensions.get("exchange"), "base_interval": dimensions.get("interval"),
        "units": {"realized_volatility": "percent", "dvol": "volatility_index", "spread": "volatility_points",
                  "percentile_rank": "decimal"},
        "parameters": {"zscore_window_days": ZSCORE_WINDOW_DAYS, "zscore_min_valid_records": ZSCORE_MIN_VALID_RECORDS,
                       "zscore_ddof": ZSCORE_DDOF, "percentile_window_days": PERCENTILE_WINDOW_DAYS,
                       "percentile_min_valid_records": PERCENTILE_MIN_VALID_RECORDS, "daily_aggregation": DAILY_AGGREGATION},
        "history_policy": {"calculation": PROCESSING_RECALCULATION_POLICY, "presentation": "not_applied_in_processing"},
    }


def _invalid_output(contract: Any, error: str) -> dict[str, Any]:
    safe = contract if isinstance(contract, Mapping) else {}
    source = {key: {"status": "invalid", "reason": "input_contract_invalid", "source_data_as_of": None} for key, _, _ in _SOURCES}
    features = {name: {"status": "invalid", "reason": "input_contract_invalid", "records": [], "current": None}
                for name in ("realized_volatility", "dvol", "volatility_spread", "daily_regime_basis")}
    features["volatility_native_analytics"] = {"status": "invalid", "reason": "input_contract_invalid", "timestamps": [], "records": 0, "charts": {}, "indicators": {}, "hmi_recalculate": False}
    quality = evaluate_volatility_market_regimes_processing_quality(features, source, [error])
    return {"family": FAMILY, "stage": "processing", "version": PROCESSING_VERSION,
            "mode": safe.get("mode") if safe.get("mode") in _MODES else "bootstrap",
            "context": _context(safe), "source_availability": source, "features": features, "quality": quality}


class VolatilityMarketRegimesProcessor:
    def __init__(self, feature_builder: VolatilityMarketRegimesFeatureBuilder | None = None) -> None:
        self.feature_builder = feature_builder or VolatilityMarketRegimesFeatureBuilder()

    def process(self, contract: Any, *, price_history_daily: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
        try:
            validate_volatility_market_regimes_input(contract)
            sources = extract_processing_source_records(contract)
            realized = build_realized_volatility_from_prices(price_history_daily)
            dvol = build_dvol_series(sources["glassnode.dvol"])
            spread = build_volatility_spread_series(realized, dvol)
            daily = build_daily_regime_basis(realized, dvol, spread)
            features = self.feature_builder.build(realized, dvol, spread, daily)
            features["volatility_native_analytics"] = build_volatility_native_analytics(realized, dvol, price_history_daily)
            availability = _source_availability(sources)
            quality = evaluate_volatility_market_regimes_processing_quality(features, availability)
            return {"family": FAMILY, "stage": "processing", "version": PROCESSING_VERSION,
                    "mode": contract["mode"], "context": _context(contract),
                    "source_availability": availability, "features": features, "quality": quality}
        except (TypeError, ValueError, KeyError) as exc:
            return _invalid_output(contract, str(exc))


def process_volatility_market_regimes(contract: Any, *, price_history_daily: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    return VolatilityMarketRegimesProcessor().process(contract, price_history_daily=price_history_daily)
