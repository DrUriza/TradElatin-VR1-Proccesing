"""Cost-aware CoinGlass extraction plan for Liquidity Microstructure Input.

The family owns only liquidity-native provider calls.  Historical price context
is reused from Prices and historical aggressive buy/sell flow is reused from
CVD Processing when available.

Acquisition policy:
- bootstrap: native 1m for current-screen density + native 4h for a deep
  analytical seed (up to 1000 points), range-10 depth only;
- incremental: native 1m orderbook + footprint + large-limit-order snapshots
  are refreshed on every Liquidity worker cycle (~5 s);
- Input consumes only the newest historical snapshot from each fixed-500
  Emulator response, so fast refresh does not reprocess the full history.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
import hashlib
import json
import time
from typing import Any

LIQUIDITY_MICROSTRUCTURE_FAMILY = "liquidity_microstructure"
PROVIDER = "coinglass"
TIMEFRAMES = ("1m", "5m", "15m", "4h")
BOOTSTRAP_TIMEFRAMES = ("1m", "4h")
INCREMENTAL_TIMEFRAMES = ("1m",)
TIMEFRAME_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "4h": 14400}
# The final HMI consumes the 10% reference depth.  1% / 5% were legacy
# exploratory ranges and multiplied provider calls without feeding the final
# contract.
DEPTH_RANGES_PERCENT = (10,)
VALID_MODES = {"bootstrap", "incremental", "recovery"}
WHALE_ORDERS_TTL_SECONDS = 300

ENDPOINT_MANIFEST = {
    "spot_orderbook_heatmap": {"transport": "rest", "path": "/api/spot/orderbook/history"},
    "perpetual_orderbook_heatmap": {"transport": "rest", "path": "/api/futures/orderbook/history"},
    "spot_footprint": {"transport": "rest", "path": "/api/spot/volume/footprint-history"},
    "futures_footprint": {"transport": "rest", "path": "/api/futures/volume/footprint-history"},
    "spot_large_limit_orders": {"transport": "rest", "path": "/api/spot/orderbook/large-limit-order"},
    "perpetual_large_limit_orders": {"transport": "rest", "path": "/api/futures/orderbook/large-limit-order"},
}

RawFetcher = Callable[..., Any]


def _request_id(request: Mapping[str, Any]) -> str:
    identity = {key: request.get(key) for key in ("provider", "transport", "endpoint_id", "path", "channel", "params", "dimensions")}
    payload = json.dumps(identity, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def _request(endpoint_id: str, *, params: Mapping[str, Any], dimensions: Mapping[str, Any]) -> dict[str, Any]:
    manifest = ENDPOINT_MANIFEST[endpoint_id]
    request = {
        "provider": PROVIDER,
        "transport": manifest["transport"],
        "endpoint_id": endpoint_id,
        "path": manifest["path"],
        "channel": None,
        "params": dict(params),
        "dimensions": dict(dimensions),
    }
    request["request_id"] = _request_id(request)
    return request


def _dataset(existing_contract: Mapping[str, Any] | None, *parts: str) -> Mapping[str, Any] | None:
    if not isinstance(existing_contract, Mapping):
        return None
    node: Any = existing_contract
    for part in parts:
        if not isinstance(node, Mapping):
            return None
        node = node.get(part)
    return node if isinstance(node, Mapping) else None


def _source_as_of(existing_contract: Mapping[str, Any] | None, *parts: str) -> int | None:
    node = _dataset(existing_contract, *parts)
    value = node.get("source_data_as_of") if node is not None else None
    return value if type(value) is int else None


def _needs_bucket(existing_contract: Mapping[str, Any] | None, parts: tuple[str, ...], reference: int, bucket_seconds: int) -> bool:
    """Return True only when the persisted source is older than one bucket.

    Provider history is normally closed-bucket data, so at 12:01 a persisted
    12:00 observation is still current.  This also makes repeated warm runs at
    the same wall-clock minute a true no-op before transport.
    """
    last = _source_as_of(existing_contract, *parts)
    if last is None:
        return True
    return last < reference - bucket_seconds


def build_liquidity_microstructure_fetch_plan(
    *,
    mode: str = "bootstrap",
    reference_timestamp: int | None = None,
    asset: str = "BTC",
    exchange: str = "Binance",
    spot_symbol: str = "BTCUSDT",
    perpetual_symbol: str = "BTCUSDT",
    timeframes: Sequence[str] | None = None,
    depth_ranges_percent: Sequence[int] = DEPTH_RANGES_PERCENT,
    history_limit: int = 240,
    hourly_history_limit: int | None = 1000,
    footprint_limit: int = 240,
    overlap_seconds: int = 300,
    existing_contract: Mapping[str, Any] | None = None,
    whale_orders_ttl_seconds: int = WHALE_ORDERS_TTL_SECONDS,
    recovery_requests: Sequence[str | Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if mode not in VALID_MODES:
        raise ValueError("invalid_liquidity_microstructure_mode")
    reference = int(reference_timestamp or time.time())
    if history_limit <= 0 or footprint_limit <= 0 or (hourly_history_limit is not None and hourly_history_limit <= 0):
        raise ValueError("invalid_history_limit")
    if whale_orders_ttl_seconds <= 0:
        raise ValueError("invalid_refresh_ttl")

    if timeframes is None:
        selected_timeframes = BOOTSTRAP_TIMEFRAMES if mode in {"bootstrap", "recovery"} else INCREMENTAL_TIMEFRAMES
    else:
        selected_timeframes = tuple(timeframes)
    if not selected_timeframes or any(tf not in TIMEFRAMES for tf in selected_timeframes):
        raise ValueError("invalid_timeframe")
    selected_ranges = tuple(int(value) for value in depth_ranges_percent)
    if not selected_ranges or any(value != 10 for value in selected_ranges):
        # Only range-10 is part of the final VR1 liquidity contract.
        raise ValueError("unsupported_depth_range")

    def limit_for(timeframe: str) -> int:
        return int(hourly_history_limit if timeframe == "4h" and hourly_history_limit is not None else history_limit)

    def start_for(timeframe: str, limit: int | None = None) -> int:
        if mode == "incremental":
            return reference - overlap_seconds
        seconds = TIMEFRAME_SECONDS[timeframe]
        count = int(limit if limit is not None else limit_for(timeframe))
        return reference - max(seconds * count, seconds)

    common = {"asset": asset, "exchange": exchange}
    plan: list[dict[str, Any]] = []

    # Orderbook is the only depth primitive in the final 33-endpoint policy.
    # Bid/ask depth at the contractual 10% window is derived deterministically
    # from each normalized orderbook snapshot in Input.
    for market_type, symbol, heatmap_id in (
        ("spot", spot_symbol, "spot_orderbook_heatmap"),
        ("perpetual", perpetual_symbol, "perpetual_orderbook_heatmap"),
    ):
        for timeframe in selected_timeframes:
            # Liquidity is a fast snapshot family. Even within the same 1m
            # provider bucket, the current book can materially change every
            # simulation/market update (~5 s), so incremental runs must poll.
            orderbook_needed = True
            limit = limit_for(timeframe)
            start = start_for(timeframe)
            dimensions = {**common, "market_type": market_type, "symbol": symbol, "timeframe": timeframe, "range_percent": None}
            params = {
                "exchange": exchange,
                "symbol": symbol,
                "interval": timeframe,
                "limit": limit,
                "start_time": start * 1000,
                "end_time": reference * 1000,
            }
            if orderbook_needed:
                plan.append(_request(heatmap_id, params=params, dimensions=dimensions))


    # Recent executed price-bin detail is required for Screen A.  Historical
    # Screen-B absorption reuses CVD Processing; therefore Footprint no longer
    # needs a 730-bar seed.
    footprint_count = min(int(footprint_limit), 1000)
    for market_type, symbol, endpoint_id in (
        ("spot", spot_symbol, "spot_footprint"),
        ("perpetual", perpetual_symbol, "futures_footprint"),
    ):
        # Footprint current-bucket execution changes inside the minute; always
        # poll on the dedicated Liquidity cadence.
        dimensions = {**common, "market_type": market_type, "symbol": symbol, "timeframe": "1m", "range_percent": None}
        footprint_start = start_for("1m", footprint_count)
        plan.append(_request(endpoint_id, params={
            "exchange": exchange,
            "symbol": symbol,
            "interval": "1m",
            "limit": footprint_count,
            "start_time": footprint_start * 1000,
            "end_time": reference * 1000,
        }, dimensions=dimensions))

    # Active whale-order snapshots feed Screen A and persistence/cancellation
    # analysis, so they refresh on every dedicated Liquidity cycle.
    for market_type, symbol, endpoint_id in (
        ("spot", spot_symbol, "spot_large_limit_orders"),
        ("perpetual", perpetual_symbol, "perpetual_large_limit_orders"),
    ):
        # Large resting orders are snapshot state, not a slow historical
        # indicator. Refresh them every Liquidity worker cycle.
        dimensions = {**common, "market_type": market_type, "symbol": symbol, "timeframe": "1m", "range_percent": None}
        plan.append(_request(endpoint_id, params={"exchange": exchange, "symbol": symbol}, dimensions=dimensions))

    # Whale activity/persistence is derived from the retained large-limit-order
    # history.  No separate Whale Index request is required.

    if mode != "recovery":
        return plan
    requested = list(recovery_requests or [])
    if not requested:
        raise ValueError("recovery_requests_required")
    selected: list[dict[str, Any]] = []
    for request in plan:
        for target in requested:
            if isinstance(target, str) and target in {request["request_id"], request["endpoint_id"]}:
                selected.append(request)
                break
            if isinstance(target, Mapping) and all(request.get("dimensions", {}).get(key) == value for key, value in target.items()):
                selected.append(request)
                break
    return selected


def execute_liquidity_microstructure_raw_request(request: Mapping[str, Any], fetcher: RawFetcher) -> dict[str, Any]:
    result = {**deepcopy(dict(request)), "status": "ok", "response": None, "error": None, "warnings": []}
    try:
        result["response"] = fetcher(**deepcopy(dict(request)))
    except Exception as exc:
        result["status"] = "error"
        result["error"] = {"type": type(exc).__name__, "message": str(exc)}
    return result


def extract_liquidity_microstructure_raw(*, fetcher: RawFetcher, mode: str = "bootstrap", **plan_arguments: Any) -> dict[str, Any]:
    plan = build_liquidity_microstructure_fetch_plan(mode=mode, **plan_arguments)
    return {
        "family": LIQUIDITY_MICROSTRUCTURE_FAMILY,
        "stage": "raw_extract",
        "mode": mode,
        "requests": [execute_liquidity_microstructure_raw_request(request, fetcher) for request in plan],
    }


class LiquidityMicrostructureRawExtractor:
    def __init__(self, fetcher: RawFetcher) -> None:
        self.fetcher = fetcher

    def build_plan(self, **kwargs: Any) -> list[dict[str, Any]]:
        return build_liquidity_microstructure_fetch_plan(**kwargs)

    def extract(self, **kwargs: Any) -> dict[str, Any]:
        return extract_liquidity_microstructure_raw(fetcher=self.fetcher, **kwargs)
