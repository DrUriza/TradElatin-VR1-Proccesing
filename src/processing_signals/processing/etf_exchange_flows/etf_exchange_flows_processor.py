"""Processing contract assembly for ETF and exchange flows."""
from __future__ import annotations
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from typing import Any

from .etf_exchange_flows_feature_builder import (
    FAMILY,
    MIN_COVERAGE_RATIO,
    RANGE_SECONDS,
    PRESSURE_WINDOW,
    build_etf_exchange_flows_features,
)

STAGE   = "processing"
VERSION = "0.1"

REQUIRED_FEATURES = ("etf_net_flow_usd_latest", "reported_total_aum_usd", "exchange_inflow_24h",
                     "exchange_outflow_24h", "exchange_netflow_24h_calculated", "cryptoquant_reserve_latest")
OPTIONAL_FEATURES = ("etf_net_flow_btc_latest", "etf_period_flow_usd", "etf_period_flow_btc", "etf_cumulative_flow_usd",
    "etf_cumulative_flow_btc", "fund_period_flow_usd", "fund_period_signed_flow_share", "fund_aum_usd", "fund_aum_share",
    "calculated_fund_aum_usd", "aum_difference_usd", "aum_difference_percent", "exchange_netflow_24h_calculated",
    "netflow_difference", "exchange_flow_pressure_24h")

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


def _iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def validate_etf_exchange_flows_input(input_contract: Any) -> None:
    if not isinstance(input_contract, Mapping):
        raise ValueError("invalid_processing_input")
    if input_contract.get("family") != FAMILY or input_contract.get("stage") != "input":
        raise ValueError("invalid_processing_input")
    if not isinstance(input_contract.get("datasets"), Mapping):
        raise ValueError("invalid_processing_input")
    if input_contract.get("data_mode") not in {"live", "synthetic"} or not isinstance(input_contract.get("is_demo"), bool):
        raise ValueError("invalid_processing_input")
    if not isinstance(input_contract.get("quality"), Mapping) or not isinstance(input_contract.get("provenance"), Mapping):
        raise ValueError("invalid_processing_input")
    if _timestamp(input_contract.get("requested_at")) is None:
        raise ValueError("invalid_processing_input")
    if input_contract.get("data_as_of") is not None and _timestamp(input_contract.get("data_as_of")) is None:
        raise ValueError("invalid_processing_input")


def _feature_map(payload: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    features = payload["features"]
    return {"etf_net_flow_usd_latest": features["etf"]["net_flow_usd_latest"],
        "reported_total_aum_usd": features["etf"]["reported_total_aum_usd"],
        "gbtc_premium_latest": features["premium_discount"]["gbtc_latest"],
        "exchange_inflow_24h": features["exchange_flows"]["inflow_24h"],
        "exchange_outflow_24h": features["exchange_flows"]["outflow_24h"],
        "exchange_netflow_24h_reported": features["exchange_flows"]["netflow_24h_reported"],
        "cryptoquant_reserve_latest": features["exchange_balances"]["cryptoquant_reserve"],
        "etf_net_flow_btc_latest": features["etf"]["net_flow_btc_latest"],
        "calculated_fund_aum_usd": features["etf"]["calculated_fund_aum_usd"],
        "exchange_netflow_24h_calculated": features["exchange_flows"]["netflow_24h_calculated"],
        "exchange_flow_pressure_24h": features["pressure"]["flow_24h"]}


def is_non_isolatable_required_error(feature: Mapping[str, Any]) -> bool:
    return feature.get("status") == "invalid" and feature.get("reason") in {"nonfinite_result", "future_timestamp", "invalid_processing_input"}


def _future_records_by_dataset(input_contract: Mapping[str, Any], generated_timestamp: int) -> dict[str, int]:
    datasets = input_contract.get("datasets", {})
    if not isinstance(datasets, Mapping):
        return {}
    result: dict[str, int] = {}

    def walk(value: Any, path: tuple[str, ...]) -> None:
        if isinstance(value, list):
            future = [item for item in value if isinstance(item, Mapping) and
                      (_timestamp(item.get("timestamp")) or 0) > generated_timestamp]
            endpoint_values = [item.get("endpoint_id") for item in future]
            valid_endpoint_values = [item.strip() for item in endpoint_values
                                     if isinstance(item, str) and item.strip()]
            endpoints = set(valid_endpoint_values)
            secondary = path[:1] == ("secondary_sources",)
            if secondary and len(endpoints) > 1 and len(valid_endpoint_values) == len(future):
                for endpoint_id in sorted(endpoints):
                    count = sum(item.get("endpoint_id") == endpoint_id for item in future)
                    endpoint_path = (*path[:-1], endpoint_id.strip(), path[-1])
                    result[".".join(endpoint_path)] = int(count)
            elif future:
                result[".".join(path)] = len(future)
            return
        if isinstance(value, Mapping):
            for name, child in value.items():
                walk(child, (*path, str(name)))

    for name, value in datasets.items():
        walk(value, (str(name),))
    return dict(sorted(result.items()))


def _apply_input_quality(payload: dict[str, Any], input_contract: Mapping[str, Any]) -> None:
    endpoints = input_contract.get("quality", {}).get("endpoints", {})
    if not isinstance(endpoints, Mapping):
        return
    dependencies = {
        "coinglass.bitcoin_etf_flows": payload["features"]["etf"]["net_flow_usd_latest"],
        "coinglass.bitcoin_etf_list": payload["features"]["etf"]["reported_total_aum_usd"],
        "cryptoquant.exchange_inflow.hour": payload["features"]["exchange_flows"]["inflow_24h"],
        "cryptoquant.exchange_outflow.hour": payload["features"]["exchange_flows"]["outflow_24h"],
        "cryptoquant.exchange_reserve.hour": payload["features"]["exchange_balances"]["cryptoquant_reserve"],
    }
    for endpoint, feature in dependencies.items():
        source_status = endpoints.get(endpoint, {}).get("status") if isinstance(endpoints.get(endpoint), Mapping) else None
        if source_status not in {"invalid", "unavailable"}:
            continue
        reason = "source_invalid" if source_status == "invalid" else "source_unavailable"
        if feature.get("value") is None:
            feature.update(status="unavailable", reason=reason)
        else:
            feature.update(status="partial", reason=reason, warnings=sorted(set([*feature.get("warnings", []), reason])))


def evaluate_etf_exchange_flows_processing_quality(payload: Mapping[str, Any], input_contract: Mapping[str, Any]) -> dict[str, Any]:
    feature_map = _feature_map(payload)
    statuses = [item.get("status") for item in feature_map.values()]
    required = {name: feature_map[name] for name in REQUIRED_FEATURES}
    usable = [item for item in required.values() if item.get("status") in {"available", "partial"} and isinstance(item.get("value"), (int, float))]
    nonisolatable = any(is_non_isolatable_required_error(item) for item in required.values())
    if nonisolatable or not usable:
        status = "invalid"
    elif all(item.get("status") == "available" for item in required.values()):
        status = "ok"
    else:
        status = "partial"
    anchors = [int(item["data_as_of"]) for item in usable if isinstance(item.get("data_as_of"), int)]
    timestamps = [item.get("timestamp") for item in feature_map.values() if isinstance(item.get("timestamp"), int)]
    input_datasets = input_contract.get("datasets", {})
    coverage_by_dataset = {name: len(value) if isinstance(value, list) else
        sum(len(rows) for rows in value.values() if isinstance(rows, list)) if isinstance(value, Mapping) else 0 for name, value in input_datasets.items()}
    warnings = sorted(set(payload.get("warnings", [])))
    return {"status": status, "features_available": statuses.count("available"), "features_partial": statuses.count("partial"),
        "features_unavailable": statuses.count("unavailable"), "features_invalid": statuses.count("invalid"),
        "required_available": sum(item.get("status") == "available" for item in required.values()),
        "required_partial": sum(item.get("status") == "partial" for item in required.values()),
        "required_unavailable": sum(item.get("status") == "unavailable" for item in required.values()),
        "required_invalid": sum(item.get("status") == "invalid" for item in required.values()), "required_usable": len(usable),
        "required_degraded": sum(item.get("status") != "available" for item in required.values()), "coverage_by_dataset": coverage_by_dataset,
        "first_timestamp": min(timestamps) if timestamps else None, "last_timestamp": max(timestamps) if timestamps else None,
        "data_as_of": min(anchors) if anchors else None, "warnings": warnings, "errors": []}


def _provenance(payload: Mapping[str, Any], input_contract: Mapping[str, Any], exchange_scope: str | None) -> dict[str, Any]:
    netflow_reconciliation = payload["features"]["provider_reconciliation"]["netflow"]
    generated_timestamp = int(payload["generated_timestamp"])
    future_by_dataset = _future_records_by_dataset(input_contract, generated_timestamp)
    flows = payload["features"]["exchange_flows"]
    negative_by_feature = {name: int(flows[name].get("coverage", {}).get("invalid_observations", 0))
                           for name in ("inflow_24h", "outflow_24h")}
    negative_by_feature = {name: count for name, count in negative_by_feature.items() if count}
    return {"input_family": FAMILY, "input_data_as_of": input_contract.get("data_as_of"),
        "datasets_used": sorted(input_contract.get("datasets", {})),
        "providers": deepcopy(input_contract.get("provenance", {}).get("providers", {})),
        "feature_sources": {name: {"provider": item.get("provider"), "endpoint_id": item.get("endpoint_id"),
            "data_as_of": item.get("data_as_of"), "scope": item.get("exchange_scope")} for name, item in _feature_map(payload).items()},
        "formulas": {"etf_net_flow_btc_latest": "flow_usd/price_usd_same_row", "etf_period_flow_btc": "sum(flow_usd_i/price_usd_i)",
            "exchange_netflow_24h_calculated": "inflow_24h-outflow_24h", "exchange_flow_pressure_24h": "(inflow_24h-outflow_24h)/(inflow_24h+outflow_24h)",
            "fund_period_signed_flow_share": "fund_flow/sum(abs(fund_flow))"},
        "parameters": {"ranges": deepcopy(RANGE_SECONDS), "pressure_window_seconds": PRESSURE_WINDOW,
            "minimum_coverage_ratio": MIN_COVERAGE_RATIO, "exchange_scope": exchange_scope},
        "reconciliations": {"netflow": {name: deepcopy(netflow_reconciliation.get(name)) for name in
            ("calculated_anchor", "reported_anchor", "timestamp_distance", "window_seconds", "scope", "alignment_required")}},
        "anomalies": {"warnings": sorted(set(payload.get("warnings", []))),
            "future_records_excluded": sum(future_by_dataset.values()),
            "future_records_by_dataset": future_by_dataset,
            "negative_observations_rejected": sum(negative_by_feature.values()),
            "negative_observations_by_feature": negative_by_feature}}



def _native_capital_flow_analysis(payload: Mapping[str, Any], *, price_history_daily: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    raw_flows = list(payload.get("series", {}).get("etf_flow_daily", []))
    flows = [row for row in raw_flows if isinstance(row, Mapping) and _timestamp(row.get("timestamp")) is not None]
    timestamps = [int(_timestamp(row.get("timestamp"))) for row in flows]
    flow_values = [row.get("flow_usd") for row in flows]
    flow_z = rolling_zscore(flow_values, 30, 10)
    flow_momentum_z = rolling_zscore(difference(flow_values), 20, 8)
    signs = [None if value is None else (1.0 if float(value) > 0 else (-1.0 if float(value) < 0 else 0.0)) for value in flow_values]
    persistence = rolling_mean(signs, 10, 5)

    prices = price_history_daily or []
    price_by_ts: dict[int, Any] = {}
    for row in prices:
        if not isinstance(row, Mapping):
            continue
        timestamp = _timestamp(row.get("timestamp"))
        if timestamp is not None:
            price_by_ts[timestamp] = row.get("close")
    aligned_prices = [price_by_ts.get(ts) for ts in timestamps]
    price_return_z = rolling_zscore(native_pct_change(aligned_prices, 1, 100.0), 30, 10)
    divergence = [None if p is None or f is None else float(p) - float(f) for p, f in zip(price_return_z, flow_z, strict=True)]

    def align_day(name: str, field: str) -> list[float | None]:
        rows = payload.get("series", {}).get(name, {}).get("day", [])
        # Partial/unavailable provider datasets may preserve placeholder rows
        # with timestamp=None.  Those rows are valid availability markers, not
        # time-series observations, so they must never reach int(None).
        lookup: dict[int, Any] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            timestamp = _timestamp(row.get("timestamp"))
            if timestamp is not None:
                lookup[timestamp] = row.get(field)
        return [lookup.get(ts) for ts in timestamps]

    inflow = align_day("exchange_inflow", "inflow_total")
    outflow = align_day("exchange_outflow", "outflow_total")
    reported_netflow = align_day("exchange_netflow", "netflow_total")
    # Netflow is an exact identity, not an independent primitive requirement.
    # Prefer the provider confirmation when present; otherwise derive it locally
    # from the already acquired inflow/outflow series.
    netflow = [
        reported if reported is not None else
        (None if incoming is None or outgoing is None else float(incoming) - float(outgoing))
        for reported, incoming, outgoing in zip(reported_netflow, inflow, outflow, strict=True)
    ]
    reserve = align_day("exchange_reserve", "reserve")
    pressure: list[float | None] = []
    for i, o in zip(inflow, outflow, strict=True):
        if i is None or o is None or float(i) + float(o) == 0:
            pressure.append(None)
        else:
            pressure.append((float(i) - float(o)) / (float(i) + float(o)))
    netflow_z = rolling_zscore(netflow, 30, 10)
    reserve_change = difference(reserve)
    reserve_change_z = rolling_zscore(reserve_change, 30, 10)
    reserve_roc = native_pct_change(reserve, 30, 100.0)
    reserve_roc_z = rolling_zscore(reserve_roc, 30, 10)

    capital_score: list[float | None] = []
    for fz, nz, rz in zip(flow_z, netflow_z, reserve_change_z, strict=True):
        vals = [v for v in (fz, nz, rz) if v is not None]
        capital_score.append(None if not vals else float(( (fz or 0.0) - (nz or 0.0) - (rz or 0.0) ) / 3.0))
    wasserstein = rolling_wasserstein(capital_score, 20, 60)

    def package(indicator_id: str, series_map: Mapping[str, Sequence[Any]], *, section: str, label: str) -> dict[str, Any]:
        current = {name: latest(values) for name, values in series_map.items()}
        primary = next((v for v in current.values() if v is not None), None)
        signal = "neutral" if primary is None or abs(float(primary)) < 0.25 else ("positive" if float(primary) > 0 else "negative")
        return {"status": "available" if primary is not None else "partial", "data_mode": "runtime_processing",
                "processing_contract_target": True, "real_market_calculation": True, "hmi_recalculate": False,
                "unit": "score", "timestamps": timestamps, "series": {k:list(v) for k,v in series_map.items()},
                "thresholds": [{"value":0.0,"role":"neutral"}],
                "summary": {"section": section, "label": label, "display_value": None if primary is None else f"{primary:.3f}",
                            "signal": signal,
                            "signal_color": {"positive": "#20d05c", "negative": "#ff3d55", "neutral": "#ffab00"}.get(signal, "#59636b"),
                            "strength": 0.0 if primary is None else min(1.0,abs(float(primary))/2.0)},
                "provenance": {"owner":"ETF Processing", "indicator_id":indicator_id}}

    indicators = {
        "etf_flow_momentum_persistence": package("etf_flow_momentum_persistence", {"flow_momentum_z":flow_momentum_z,"rolling_flow_z":flow_z,"persistence_score":persistence}, section="institutional", label="ETF FLOW MOMENTUM / PERSISTENCE"),
        "etf_flow_zscore": package("etf_flow_zscore", {"zscore":flow_z}, section="institutional", label="ETF FLOW Z-SCORE"),
        "btc_etf_flow_divergence": package("btc_etf_flow_divergence", {"btc_return_z":price_return_z,"etf_flow_z":flow_z,"divergence_score":divergence}, section="confirmation", label="BTC ↔ ETF FLOW DIVERGENCE"),
        "exchange_flow_pressure": package("exchange_flow_pressure", {"pressure":pressure,"netflow_z":netflow_z}, section="exchange", label="EXCHANGE FLOW PRESSURE"),
        "exchange_reserve_change": package("exchange_reserve_change", {"reserve_change_z":reserve_change_z,"reserve_roc_z":reserve_roc_z}, section="exchange", label="EXCHANGE RESERVE CHANGE"),
        "capital_regime_wasserstein": package("capital_regime_wasserstein", {"capital_regime_score":capital_score,"wasserstein_distance":wasserstein}, section="regime", label="CAPITAL REGIME / WASSERSTEIN"),
    }
    return {"analysis_id":"capital_flow_analysis", "contract_family":"native_capital_flow", "status":"available",
            "recalculate_in_hmi":False, "source_resolution":"1d", "indicators":indicators,
            "supporting_series":{
                "timestamps":timestamps, "etf_flow_usd":flow_values, "btc_price":aligned_prices,
                "exchange_inflow":inflow, "exchange_outflow":outflow,
                "exchange_netflow":netflow, "exchange_reserve":reserve,
            }}


def process_etf_exchange_flows(*, input_contract: Mapping[str, Any], generated_at: Any = None,
                               exchange_scope: str | None = None,
                               price_history_daily: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    validate_etf_exchange_flows_input(input_contract)
    source = deepcopy(dict(input_contract))
    generated_timestamp = _timestamp(generated_at if generated_at is not None else source.get("generated_at"))
    if generated_timestamp is None:
        raise ValueError("invalid_processing_input")
    payload = build_etf_exchange_flows_features(input_contract=source, generated_at=generated_timestamp, exchange_scope=exchange_scope)
    _apply_input_quality(payload, source)
    quality = evaluate_etf_exchange_flows_processing_quality(payload, source)
    output = {"family": FAMILY, "stage": STAGE, "version": VERSION, "mode": source.get("mode"), "data_mode": source.get("data_mode"),
        "is_demo": source.get("is_demo"), "generated_at": _iso(generated_timestamp), "data_as_of": quality["data_as_of"],
        "features": deepcopy(payload["features"]), "series": deepcopy(payload["series"]),
        "technical_analysis": deepcopy(payload.get("technical_analysis", {})),
        "capital_flow_analysis": _native_capital_flow_analysis(payload, price_history_daily=price_history_daily),
        "series_metadata": deepcopy(payload.get("series_metadata", {})), "snapshots": deepcopy(payload["snapshots"]),
        "provenance": _provenance(payload, source, exchange_scope), "quality": quality}
    json.dumps(output, ensure_ascii=False, allow_nan=False, sort_keys=False)
    return output


def run_etf_exchange_flows_processing(*, input_contract: Mapping[str, Any], generated_at: Any = None,
                                      exchange_scope: str | None = None,
                                      price_history_daily: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    return process_etf_exchange_flows(input_contract=input_contract, generated_at=generated_at, exchange_scope=exchange_scope, price_history_daily=price_history_daily)


class EtfExchangeFlowsProcessor:
    def process(self, *, input_contract: Mapping[str, Any], generated_at: Any = None, exchange_scope: str | None = None,
                price_history_daily: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
        return process_etf_exchange_flows(input_contract=input_contract, generated_at=generated_at, exchange_scope=exchange_scope, price_history_daily=price_history_daily)
from .etf_exchange_flows_math import (
    rolling_zscore,
    rolling_wasserstein,
    difference,
    pct_change as native_pct_change,
    rolling_mean,
    latest,
)
