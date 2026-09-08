from __future__ import annotations

import copy
import math
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

ON_CHAIN_MINERS_FAMILY = "on_chain_miners"
GLASSNODE_PROVIDER = "glassnode"
CRYPTOQUANT_PROVIDER = "cryptoquant"
VALID_MODES = {"bootstrap", "incremental", "recovery"}
SECONDS_PER_DAY = 86_400
BOOTSTRAP_HISTORY_DAYS = 730
BOOTSTRAP_LIMIT = 740
INCREMENTAL_OVERLAP_DAYS = 7
INCREMENTAL_LIMIT = 14

# Final VR1 On-Chain acquisition surface: 7 external primitives.
# miner_net_position_change and Puell are derived downstream and therefore
# deliberately have no provider endpoint here.
ENDPOINTS: dict[str, dict[str, Any]] = {
    "miner_reserve": {
        "provider": GLASSNODE_PROVIDER,
        "endpoint_id": "balance_miners_sum",
        "path": "/v1/metrics/distribution/balance_miners_sum",
        "source_field": "v",
        "interval": "24h",
    },
    "sopr": {
        "provider": GLASSNODE_PROVIDER,
        "endpoint_id": "sopr",
        "path": "/v1/metrics/indicators/sopr",
        "source_field": "v",
        "interval": "24h",
    },
    "hashrate": {
        "provider": GLASSNODE_PROVIDER,
        "endpoint_id": "hash_rate_mean",
        "path": "/v1/metrics/mining/hash_rate_mean",
        "source_field": "v",
        "interval": "24h",
    },
    "difficulty": {
        "provider": GLASSNODE_PROVIDER,
        "endpoint_id": "difficulty_latest",
        "path": "/v1/metrics/mining/difficulty_latest",
        "source_field": "v",
        "interval": "24h",
    },
    "mpi": {
        "provider": CRYPTOQUANT_PROVIDER,
        "endpoint_id": "mpi",
        "path": "/btc/flow-indicator/mpi",
        "source_field": "mpi",
        "interval": "24h",
    },
    "miner_outflow_total": {
        "provider": GLASSNODE_PROVIDER,
        "endpoint_id": "transfers_volume_from_miners_sum",
        "path": "/v1/metrics/transactions/transfers_volume_from_miners_sum",
        "source_field": "v",
        "interval": "24h",
    },
    "miner_revenue_total_usd": {
        "provider": GLASSNODE_PROVIDER,
        "endpoint_id": "revenue_sum",
        "path": "/v1/metrics/mining/revenue_sum",
        "source_field": "v",
        "interval": "24h",
    },
}
FETCH_METRIC_IDS = tuple(ENDPOINTS)
DERIVED_METRIC_IDS = ("miner_net_position_change",)
CORE_METRIC_IDS = (
    "miner_reserve", "sopr", "hashrate", "difficulty", "miner_net_position_change",
    "mpi", "miner_outflow_total", "miner_revenue_total_usd",
)
RECOVERY_WARMUP_DAYS = {
    "miner_reserve": 31,
    "sopr": 7,
    "hashrate": 2,
    "difficulty": 2,
    "mpi": 2,
    "miner_outflow_total": 2,
    "miner_revenue_total_usd": 365,
    "miner_net_position_change": 2,
}

OnChainMinersFetcher = Callable[..., Mapping[str, Any] | Sequence[Any]]


def _valid_timestamp(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or int(value) != value or value < 0:
        raise ValueError(f"{name} must be a non-negative Unix timestamp")
    return int(value)


def _utc_day(timestamp: Any) -> int:
    value = _valid_timestamp(timestamp, "timestamp")
    return value - value % SECONDS_PER_DAY


def _iso_utc(timestamp: int) -> str:
    return datetime.fromtimestamp(_valid_timestamp(timestamp, "execution_timestamp"), timezone.utc).isoformat().replace("+00:00", "Z")


def resolve_existing_input_state(existing_state: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if existing_state is None or existing_state == {}:
        return {}
    if not isinstance(existing_state, Mapping):
        raise ValueError("existing_state must be a mapping, None, or an empty mapping")
    if "input" in existing_state:
        value = existing_state.get("input")
        if not isinstance(value, Mapping):
            raise ValueError("existing_state.input must be a mapping")
        return resolve_existing_input_state(value)
    if existing_state.get("family") == ON_CHAIN_MINERS_FAMILY and existing_state.get("stage") == "input" and isinstance(existing_state.get("series"), Mapping):
        return existing_state
    raise ValueError("existing_state must be an on_chain_miners Input output")


def build_cryptoquant_daily_params(*, from_timestamp: int, to_timestamp: int, limit: int) -> dict[str, Any]:
    if limit <= 0:
        raise ValueError("limit must be positive")
    start = datetime.fromtimestamp(_valid_timestamp(from_timestamp, "from_timestamp"), timezone.utc).strftime("%Y%m%d")
    end = datetime.fromtimestamp(_valid_timestamp(to_timestamp, "to_timestamp"), timezone.utc).strftime("%Y%m%d")
    return {"window": "day", "from": start, "to": end, "limit": int(limit), "format": "json"}


def build_glassnode_params(*, asset: str, from_timestamp: int, to_timestamp: int, interval: str, currency: str | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {
        "a": str(asset).upper(),
        "i": interval,
        "s": _valid_timestamp(from_timestamp, "from_timestamp"),
        "u": _valid_timestamp(to_timestamp, "to_timestamp"),
    }
    if currency:
        params["c"] = currency
    return params


def _existing_records(existing_contract: Mapping[str, Any], metric_id: str) -> Sequence[Any]:
    series = existing_contract.get("series", {})
    payload = series.get(metric_id, {}) if isinstance(series, Mapping) else {}
    records = payload.get("records", []) if isinstance(payload, Mapping) else []
    return records if isinstance(records, Sequence) and not isinstance(records, (str, bytes, bytearray)) else []


def _last_existing_timestamp(existing_contract: Mapping[str, Any], metric_id: str) -> int | None:
    values = [row.get("timestamp") for row in _existing_records(existing_contract, metric_id) if isinstance(row, Mapping)]
    valid = [int(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool) and int(v) == v]
    return max(valid) if valid else None


def _source_bucket(timestamp: int, interval: str) -> int:
    # All public On-Chain/Miners primitives are acquired at daily resolution.
    del interval
    return timestamp - timestamp % SECONDS_PER_DAY


def _metric_due(metric_id: str, reference_timestamp: int, existing_contract: Mapping[str, Any]) -> bool:
    last = _last_existing_timestamp(existing_contract, metric_id)
    return last is None or last < _source_bucket(reference_timestamp, str(ENDPOINTS[metric_id]["interval"]))


def _build_request(metric_id: str, start: int, end: int, limit: int, asset: str = "BTC") -> dict[str, Any]:
    endpoint = ENDPOINTS[metric_id]
    if endpoint["provider"] == CRYPTOQUANT_PROVIDER:
        params = build_cryptoquant_daily_params(from_timestamp=start, to_timestamp=end, limit=limit)
    else:
        currency = "NATIVE" if metric_id == "miner_reserve" else "USD" if metric_id == "miner_revenue_total_usd" else None
        params = build_glassnode_params(asset=asset, from_timestamp=start, to_timestamp=end,
                                        interval=str(endpoint["interval"]), currency=currency)
    return {
        "metric_id": metric_id,
        "provider": endpoint["provider"],
        "endpoint_id": endpoint["endpoint_id"],
        "path": endpoint["path"],
        "from_timestamp": start,
        "to_timestamp": end,
        "limit": limit,
        "params": params,
        "required": True,
    }


def build_on_chain_miners_fetch_plan(*, mode: str, reference_timestamp: int,
                                     existing_contract: Mapping[str, Any] | None = None,
                                     recovery_requests: Sequence[Mapping[str, Any]] | None = None) -> list[dict[str, Any]]:
    if mode not in VALID_MODES:
        raise ValueError(f"Unsupported on_chain_miners input mode: {mode}")
    reference_timestamp = _valid_timestamp(reference_timestamp, "reference_timestamp")
    reference_day = _utc_day(reference_timestamp)
    existing = resolve_existing_input_state(existing_contract)

    if mode == "recovery":
        if not recovery_requests:
            raise ValueError("recovery mode requires at least one recovery request")
        requests: list[dict[str, Any]] = []
        for item in recovery_requests:
            if not isinstance(item, Mapping):
                raise ValueError("recovery request must be a mapping")
            requested_metric = str(item.get("metric_id", ""))
            # net-position recovery is fulfilled by the reserve primitive because
            # the series is a deterministic first difference of reserve.
            metric_id = "miner_reserve" if requested_metric == "miner_net_position_change" else requested_metric
            if metric_id not in ENDPOINTS:
                raise ValueError(f"Invalid recovery metric_id: {requested_metric}")
            start = _utc_day(item.get("start_timestamp"))
            end = _utc_day(item.get("end_timestamp"))
            if start > end:
                raise ValueError("recovery range must not be inverted")
            warmup = RECOVERY_WARMUP_DAYS.get(requested_metric, RECOVERY_WARMUP_DAYS[metric_id])
            fetch_start = max(0, start - warmup * SECONDS_PER_DAY)
            limit = max(1, (end - fetch_start) // SECONDS_PER_DAY + 1)
            request_end = end
            requests.append(_build_request(metric_id, fetch_start, request_end, limit))
        # Dedupe in case reserve and derived net-position were both requested.
        return list({(r["provider"], r["endpoint_id"], str(r["params"])): r for r in requests}.values())

    requests = []
    for metric_id in FETCH_METRIC_IDS:
        if mode == "incremental" and not _metric_due(metric_id, reference_timestamp, existing):
            continue
        last = _last_existing_timestamp(existing, metric_id) if mode == "incremental" else None
        if mode == "bootstrap" or last is None:
            start = max(0, reference_day - BOOTSTRAP_HISTORY_DAYS * SECONDS_PER_DAY)
            limit = BOOTSTRAP_LIMIT
        else:
            start = max(0, _utc_day(last) - INCREMENTAL_OVERLAP_DAYS * SECONDS_PER_DAY)
            limit = INCREMENTAL_LIMIT
        endpoint = ENDPOINTS[metric_id]
        request_end = reference_day
        requests.append(_build_request(metric_id, start, request_end, limit))
    return requests


def _safe_error_message(exc: Exception) -> str:
    text = str(exc)
    return "provider request failed; sensitive details redacted" if any(token in text.lower() for token in ("api_key", "apikey", "token", "authorization", "bearer")) else text


def _execute_metric_request(*, fetcher: OnChainMinersFetcher, request: Mapping[str, Any]) -> dict[str, Any]:
    base = {key: copy.deepcopy(request[key]) for key in ("metric_id", "provider", "endpoint_id", "path", "required", "params", "from_timestamp", "to_timestamp")}
    try:
        response = fetcher(provider=request["provider"], endpoint_id=request["endpoint_id"], path=request["path"], params=copy.deepcopy(request["params"]))
        return {**base, "status": "ok", "response": copy.deepcopy(response), "error": None}
    except Exception as exc:
        return {**base, "status": "error", "response": None,
                "error": {"type": type(exc).__name__, "message": _safe_error_message(exc)}}


class OnChainMinersRawExtractor:
    def __init__(self, *, fetcher: OnChainMinersFetcher, asset: str = "BTC", data_mode: str = "live", is_demo: bool = False) -> None:
        if str(asset).upper() != "BTC":
            raise ValueError("on_chain_miners currently supports asset BTC only")
        if data_mode not in {"live", "synthetic"}:
            raise ValueError("data_mode must be live or synthetic")
        if data_mode == "synthetic" and not is_demo:
            raise ValueError("data_mode=synthetic requires is_demo=True")
        self.fetcher = fetcher
        self.asset = "BTC"
        self.data_mode = data_mode
        self.is_demo = bool(is_demo)

    def build_fetch_plan(self, **kwargs: Any) -> list[dict[str, Any]]:
        return build_on_chain_miners_fetch_plan(**kwargs)

    def run(self, *, mode: str, reference_timestamp: int,
            existing_contract: Mapping[str, Any] | None = None,
            recovery_requests: Sequence[Mapping[str, Any]] | None = None,
            execution_timestamp: int | None = None) -> dict[str, Any]:
        reference_timestamp = _valid_timestamp(reference_timestamp, "reference_timestamp")
        execution_timestamp = _valid_timestamp(int(time.time()) if execution_timestamp is None else execution_timestamp, "execution_timestamp")
        plan = self.build_fetch_plan(mode=mode, reference_timestamp=reference_timestamp,
                                     existing_contract=existing_contract, recovery_requests=recovery_requests)
        raw = {request["metric_id"]: _execute_metric_request(fetcher=self.fetcher, request=request) for request in plan}
        return {
            "schema": {"id": "trad_elatin.on_chain_miners.extracted_raw.v1", "version": "1.0.0"},
            "family": ON_CHAIN_MINERS_FAMILY,
            "stage": "extracted_raw",
            "mode": mode,
            "context": {
                "asset": self.asset,
                "data_mode": self.data_mode,
                "is_demo": self.is_demo,
                "reference_timestamp": reference_timestamp,
                "execution_timestamp": execution_timestamp,
                "requested_at": _iso_utc(execution_timestamp),
            },
            "raw": raw,
        }


def extract_on_chain_miners_raw(*, fetcher: OnChainMinersFetcher, mode: str, reference_timestamp: int,
                                asset: str = "BTC", existing_contract: Mapping[str, Any] | None = None,
                                recovery_requests: Sequence[Mapping[str, Any]] | None = None,
                                data_mode: str = "live", is_demo: bool = False,
                                execution_timestamp: int | None = None) -> dict[str, Any]:
    extractor = OnChainMinersRawExtractor(fetcher=fetcher, asset=asset, data_mode=data_mode, is_demo=is_demo)
    return extractor.run(mode=mode, reference_timestamp=reference_timestamp, existing_contract=existing_contract,
                         recovery_requests=recovery_requests, execution_timestamp=execution_timestamp)
