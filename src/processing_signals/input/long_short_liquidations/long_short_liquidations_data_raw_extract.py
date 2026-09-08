"""Raw extraction contract for the long/short liquidations input family."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import math
import os
import time
from typing import Any

LONG_SHORT_LIQUIDATIONS_FAMILY = "long_short_liquidations"
COINGLASS_PROVIDER = "coinglass"
VALID_MODES = {"bootstrap", "incremental", "recovery"}
DEFAULT_ASSET = "BTC"
DEFAULT_INTERVAL = "15m"
POSITIONING_TIMEFRAMES = ("1m", "5m", "15m", "4h")
DEFAULT_HISTORY_HOURS = 730
DEFAULT_INCREMENTAL_OVERLAP_H = 6
DEFAULT_EVENT_LOOKBACK_H = 24
DEFAULT_EVENT_OVERLAP_MINUTES = 15
DEFAULT_MIN_EVENT_USD = 10_000
PUBLIC_MAP_RANGES = ("1d", "7d", "30d")
DEFAULT_MAP_RANGE = "1d"
DEFAULT_EXCHANGE_RANGE = "24h"  # compatibility-only; no external exchange-list request
DEFAULT_MAX_PAIN_RANGE = "24h"  # compatibility-only; max-pain endpoint removed from final 33
DEFAULT_EXCHANGES = ("Binance", "OKX", "Bybit")
PAIR_MAP_EXCHANGES = (*DEFAULT_EXCHANGES, "Hyperliquid")
COINGLASS_GLOBAL_ACCOUNT_ENDPOINT_ID = "global_account_long_short_ratio"

RawFetcher = Callable[..., Any]
Clock = Callable[[], int | float]


def _fetch_worker_count(fetcher: RawFetcher | None = None) -> int:
    """Choose bounded request concurrency without changing data semantics.

    The real provider is network-bound, so a small worker pool cuts manual
    RELOAD latency substantially.  The Emulator is CPU-bound while it builds
    deterministic stochastic histories; parallel HTTP calls only contend for
    the same simulation engine and are slower there, so its safe default is 1.

    ``TRADELATIN_LIQUIDATIONS_FETCH_WORKERS`` remains an explicit override for
    either source and is clamped to 1..16.
    """
    override = os.environ.get("TRADELATIN_LIQUIDATIONS_FETCH_WORKERS", "").strip()
    if override:
        try:
            return max(1, min(16, int(override)))
        except (TypeError, ValueError):
            pass

    owner = getattr(fetcher, "__self__", None)
    if str(getattr(owner, "source_mode", "")).lower() == "emulator":
        return 1
    return 6

# Liquidations uses exactly these seven CoinGlass primitives from the frozen 33.
ENDPOINT_MANIFEST: dict[tuple[str, str], str] = {
    (COINGLASS_PROVIDER, "aggregated_liquidation_history"): "/api/futures/liquidation/aggregated-history",
    (COINGLASS_PROVIDER, "liquidation_order_events"): "/api/futures/liquidation/order",
    (COINGLASS_PROVIDER, "aggregated_liquidation_map"): "/api/futures/liquidation/aggregated-map",
    (COINGLASS_PROVIDER, "pair_liquidation_map"): "/api/futures/liquidation/map",
    (COINGLASS_PROVIDER, COINGLASS_GLOBAL_ACCOUNT_ENDPOINT_ID): "/api/futures/global-long-short-account-ratio/history",
}

ENDPOINT_REQUEST_SCHEMAS: dict[tuple[str, str], dict[str, Any]] = {
    (COINGLASS_PROVIDER, "aggregated_liquidation_history"): {
        "params": ("exchange_list", "symbol", "interval", "limit", "start_time", "end_time"),
        "dimensions": ("asset", "symbol"),
    },
    (COINGLASS_PROVIDER, "liquidation_order_events"): {
        "params": ("exchange", "symbol", "min_liquidation_amount", "start_time", "end_time"),
        "dimensions": ("exchange", "asset", "symbol"),
    },
    (COINGLASS_PROVIDER, "aggregated_liquidation_map"): {
        "params": ("symbol", "range"), "dimensions": ("asset", "symbol"),
    },
    (COINGLASS_PROVIDER, "pair_liquidation_map"): {
        "params": ("exchange", "symbol", "range"),
        "dimensions": ("exchange", "asset", "symbol"),
    },
    (COINGLASS_PROVIDER, COINGLASS_GLOBAL_ACCOUNT_ENDPOINT_ID): {
        "params": ("exchange", "symbol", "interval", "limit", "start_time", "end_time"),
        "dimensions": ("exchange", "asset", "symbol"),
    },
}

_COINGLASS_INTERVALS = {"1m", "5m", "15m", "4h"}
_MAP_RANGES = {"1d", "7d", "30d", "180d", "365d"}


def _require_string(mapping: Mapping[str, Any], field: str, kind: str) -> str:
    if field not in mapping:
        raise ValueError(f"missing_required_{kind}:{field}")
    value = mapping[field]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid_request_{kind}:{field}")
    return value


def _require_positive_int(mapping: Mapping[str, Any], field: str, kind: str = "param") -> int:
    if field not in mapping:
        raise ValueError(f"missing_required_{kind}:{field}")
    value = mapping[field]
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"invalid_request_{kind}:{field}")
    return value


def _validate_string_keys(mapping: Mapping[Any, Any], kind: str) -> None:
    if any(not isinstance(key, str) for key in mapping):
        raise ValueError(f"invalid_request_{kind}:non_string_key")


def validate_request_contract(request: Mapping[str, Any], *, require_dimensions: bool = True,
                              allow_skipped: bool = False) -> None:
    """Validate one endpoint request before execution or normalization."""
    if not isinstance(request, Mapping):
        raise ValueError("request_must_be_mapping")
    _validate_string_keys(request, "field")
    provider = _require_string(request, "provider", "param")
    endpoint_id = _require_string(request, "endpoint_id", "param")
    schema = ENDPOINT_REQUEST_SCHEMAS.get((provider, endpoint_id))
    if schema is None:
        raise ValueError(f"unsupported_request_endpoint:{provider}:{endpoint_id}")
    if "path" in request and request["path"] != ENDPOINT_MANIFEST[(provider, endpoint_id)]:
        raise ValueError("request_path_mismatch")
    params = request.get("params")
    dimensions = request.get("dimensions", {})
    if not isinstance(params, Mapping):
        raise ValueError("request_params_must_be_mapping")
    if not isinstance(dimensions, Mapping):
        raise ValueError("request_dimensions_must_be_mapping")
    _validate_string_keys(params, "param")
    _validate_string_keys(dimensions, "dimension")
    skipped = allow_skipped and (bool(request.get("skip_reason")) or request.get("status") == "skipped")
    if not skipped:
        for field in schema["params"]:
            if field not in params:
                raise ValueError(f"missing_required_param:{field}")
    if skipped:
        for field in ("exchange", "asset"):
            if field in schema["dimensions"]:
                _require_string(dimensions, field, "dimension")
        return
    if require_dimensions:
        for field in schema["dimensions"]:
            _require_string(dimensions, field, "dimension")

    string_params = set(schema["params"]) - {
        "limit", "start_time", "end_time", "s", "u", "from", "to", "min_liquidation_amount",
    }
    for field in string_params:
        _require_string(params, field, "param")
    for field in ("limit", "start_time", "end_time", "s", "u"):
        if field in schema["params"]:
            _require_positive_int(params, field)
    if "start_time" in schema["params"] and params["start_time"] > params["end_time"]:
        raise ValueError("invalid_request_time_range")
    if "s" in schema["params"] and params["s"] > params["u"]:
        raise ValueError("invalid_request_time_range")
    if endpoint_id in {"aggregated_liquidation_history", COINGLASS_GLOBAL_ACCOUNT_ENDPOINT_ID} and params["interval"] not in _COINGLASS_INTERVALS:
        raise ValueError("invalid_request_param:interval")
    if endpoint_id in {"aggregated_liquidation_map", "pair_liquidation_map"} and params["range"] not in _MAP_RANGES:
        raise ValueError("invalid_request_param:range")
    if endpoint_id == "liquidation_order_events":
        try:
            amount = float(params["min_liquidation_amount"])
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid_request_param:min_liquidation_amount") from exc
        if not math.isfinite(amount) or amount <= 0 or isinstance(params["min_liquidation_amount"], bool):
            raise ValueError("invalid_request_param:min_liquidation_amount")

    for field in ("exchange", "symbol"):
        if field in params and field in schema["dimensions"] and dimensions.get(field) != params[field]:
            raise ValueError(f"request_dimension_mismatch:{field}")
    for dimension, param in schema.get("dimension_param_matches", {}).items():
        if dimensions.get(dimension) != params[param]:
            raise ValueError(f"request_dimension_mismatch:{dimension}")
    if endpoint_id == "liquidation_order_events" and dimensions.get("asset") != params["symbol"]:
        raise ValueError("request_dimension_mismatch:asset")


def build_canonical_dimensions(*, provider: str, endpoint_id: str, params: Mapping[str, Any],
                               asset: str) -> dict[str, Any]:
    schema = ENDPOINT_REQUEST_SCHEMAS[(provider, endpoint_id)]
    dimensions: dict[str, Any] = {}
    for field in schema["dimensions"]:
        if field == "exchange":
            dimensions[field] = params.get("exchange")
        elif field == "symbol":
            dimensions[field] = params.get("symbol", params.get("a", asset))
        else:
            dimensions[field] = params.get("a", asset)
    return dimensions


def _request(provider: str, endpoint_id: str, params: Mapping[str, Any], suffix: str = "",
             dimensions: Mapping[str, Any] | None = None) -> dict[str, Any]:
    path = ENDPOINT_MANIFEST[(provider, endpoint_id)]
    identity = suffix or ":".join(str(value) for value in params.values())
    return {
        "request_id": f"{provider}:{endpoint_id}:{identity}",
        "provider": provider,
        "endpoint_id": endpoint_id,
        "path": path,
        "params": deepcopy(dict(params)),
        "dimensions": deepcopy(dict(dimensions or {})),
    }


def _window(reference_timestamp: int, hours: int) -> tuple[int, int]:
    return reference_timestamp - hours * 3600, reference_timestamp


def build_long_short_liquidations_fetch_plan(
    *,
    mode: str,
    reference_timestamp: int,
    asset: str = DEFAULT_ASSET,
    exchanges: Sequence[str] = DEFAULT_EXCHANGES,
    map_exchanges: Sequence[str] = PAIR_MAP_EXCHANGES,
    exchange_pairs: Mapping[str, str] | None = None,
    cryptoquant_exchanges: Sequence[str] | None = None,
    history_hours: int = DEFAULT_HISTORY_HOURS,
    incremental_overlap_hours: int = DEFAULT_INCREMENTAL_OVERLAP_H,
    event_lookback_hours: int = DEFAULT_EVENT_LOOKBACK_H,
    event_overlap_minutes: int = DEFAULT_EVENT_OVERLAP_MINUTES,
    event_cursors: Mapping[str, int] | None = None,
    min_event_usd: int | float = DEFAULT_MIN_EVENT_USD,
    map_range: str = DEFAULT_MAP_RANGE,
    exchange_range: str = DEFAULT_EXCHANGE_RANGE,
    max_pain_range: str = DEFAULT_MAX_PAIN_RANGE,
    recovery_requests: Sequence[Mapping[str, Any]] | None = None,
    refresh_discovery: bool = False,
    include_confirmations: bool = True,
    reuse_hourly: bool = False,
) -> list[dict[str, Any]]:
    """Build the deterministic endpoint plan without performing I/O."""
    if mode not in VALID_MODES:
        raise ValueError(f"unsupported_mode:{mode}")
    if mode == "recovery":
        if not recovery_requests:
            raise ValueError("recovery_requests_required")
        plan = []
        for item in recovery_requests or ():
            if not isinstance(item, Mapping):
                raise ValueError("recovery_request_must_be_mapping")
            provider = item.get("provider")
            endpoint_id = item.get("endpoint_id")
            if (provider, endpoint_id) not in ENDPOINT_MANIFEST:
                raise ValueError(f"unsupported_recovery_endpoint:{provider}:{endpoint_id}")
            params = item.get("params")
            if not isinstance(params, Mapping):
                raise ValueError("recovery_params_must_be_mapping")
            dimensions = item.get("dimensions", {})
            if not isinstance(dimensions, Mapping):
                raise ValueError("recovery_dimensions_must_be_mapping")
            canonical = build_canonical_dimensions(
                provider=provider, endpoint_id=endpoint_id, params=params, asset=asset,
            )
            candidate = _request(provider, endpoint_id, params,
                                 str(item.get("request_id", "recovery")), canonical)
            validate_request_contract(candidate)
            for field, value in dimensions.items():
                if field in canonical and canonical[field] != value:
                    raise ValueError(f"request_dimension_mismatch:{field}")
            candidate["dimensions"].update(deepcopy(dict(dimensions)))
            validate_request_contract(candidate)
            plan.append(candidate)
        return plan

    pairs = dict(exchange_pairs or {})
    del cryptoquant_exchanges, exchange_range, max_pain_range, refresh_discovery, include_confirmations
    history_window = history_hours if mode == "bootstrap" else incremental_overlap_hours
    start, end = _window(reference_timestamp, history_window)
    limit = max(1, history_window)
    plan: list[dict[str, Any]] = []
    # Final 33-endpoint policy: exchange discovery is configuration-owned, not
    # a runtime provider primitive.  `refresh_discovery` is kept for API
    # compatibility but does not issue an external request.
    if not reuse_hourly:
        plan.append(_request(COINGLASS_PROVIDER, "aggregated_liquidation_history", {
            "exchange_list": ",".join(exchanges), "symbol": asset, "interval": DEFAULT_INTERVAL,
            "limit": limit, "start_time": start * 1000, "end_time": end * 1000,
        }, f"{asset}:{DEFAULT_INTERVAL}:{start}:{end}",
            {"exchange": None, "asset": asset, "symbol": asset}))
        # Long/Short Positioning exposes one canonical market-wide ratio.
        # The frozen registry still retains the historical 33 endpoint identities,
        # but Processing consumes only Global Account L/S for the public view.
        positioning_limit = 500
        for exchange in exchanges:
            symbol = pairs.get(exchange, "BTCUSDT")
            endpoint_id = COINGLASS_GLOBAL_ACCOUNT_ENDPOINT_ID
            for positioning_timeframe in POSITIONING_TIMEFRAMES:
                plan.append(_request(COINGLASS_PROVIDER, endpoint_id, {
                    "exchange": exchange, "symbol": symbol, "interval": positioning_timeframe,
                    "limit": positioning_limit, "start_time": start * 1000, "end_time": end * 1000,
                }, f"{exchange}:{symbol}:{positioning_timeframe}:{start}:{end}",
                    {"exchange": exchange, "asset": asset, "symbol": symbol}))
        # Exchange-distribution snapshot was a drilldown-only duplicate and is
        # intentionally not requested in the 33-endpoint runtime.

    # High-frequency realized liquidation events: one stream/window per exchange.
    for exchange in exchanges:
        cursor = (event_cursors or {}).get(exchange)
        if cursor is not None:
            event_start = cursor - event_overlap_minutes * 60
        elif mode == "incremental":
            event_start = end - event_overlap_minutes * 60
        else:
            event_start = end - event_lookback_hours * 3600
        plan.append(_request(COINGLASS_PROVIDER, "liquidation_order_events", {
            "exchange": exchange, "symbol": asset,
            "min_liquidation_amount": f"{min_event_usd:g}",
            "start_time": event_start * 1000, "end_time": end * 1000,
        }, f"{exchange}:{event_start}:{end}",
            {"exchange": exchange, "asset": asset, "symbol": asset}))

    # Liquidation maps are snapshots and MUST refresh on every manual RELOAD,
    # even when hourly history/positioning is safely reused.  These are requests
    # against the SAME frozen logical endpoints; endpoint inventory remains 33.
    public_ranges = PUBLIC_MAP_RANGES
    for public_range in public_ranges:
        plan.append(_request(COINGLASS_PROVIDER, "aggregated_liquidation_map", {
            "symbol": asset, "range": public_range,
        }, f"{asset}:{public_range}", {"exchange": None, "asset": asset, "symbol": asset}))
        for exchange in map_exchanges:
            symbol = pairs.get(exchange, "BTCUSDT")
            plan.append(_request(COINGLASS_PROVIDER, "pair_liquidation_map", {
                "exchange": exchange, "symbol": symbol, "range": public_range,
            }, f"{exchange}:{symbol}:{public_range}",
                {"exchange": exchange, "asset": asset, "symbol": symbol}))
    return plan


def execute_raw_request(*, fetcher: RawFetcher, request: Mapping[str, Any]) -> dict[str, Any]:
    """Execute one request and isolate all endpoint failures."""
    base = {key: deepcopy(request[key]) for key in
            ("request_id", "provider", "endpoint_id", "path", "params", "dimensions")}
    if request.get("skip_reason"):
        return {**base, "status": "skipped", "response": None, "error": None, "warnings": [request["skip_reason"]]}
    try:
        response = fetcher(
            provider=request["provider"], endpoint_id=request["endpoint_id"],
            path=request["path"], params=deepcopy(request["params"]),
        )
        return {**base, "status": "ok", "response": deepcopy(response), "error": None, "warnings": []}
    except Exception as exc:  # endpoint isolation is part of the Raw contract
        return {**base, "status": "error", "response": None, "error": f"{type(exc).__name__}:{exc}", "warnings": []}


def _event_rows(response: Any) -> list[Any] | None:
    if not isinstance(response, Mapping):
        return None
    data = response.get("data")
    return data if isinstance(data, list) else None


def _execute_event_window(
    *, fetcher: RawFetcher, request: Mapping[str, Any], minimum_event_window_seconds: int,
) -> list[dict[str, Any]]:
    """Execute the liquidation-event primitive exactly once.

    The final 33-endpoint Emulator contract returns a fixed 500-record snapshot.
    Recursively bisecting the requested window when that snapshot is full would
    re-request the same logical endpoint exponentially and can stall bootstrap.
    Processing therefore consumes the provider response as one primitive; any
    provider-side paging policy belongs to the real Market API adapter, not to
    this family extractor.
    """
    del minimum_event_window_seconds
    validate_request_contract(request)
    result = execute_raw_request(fetcher=fetcher, request=request)
    rows = _event_rows(result.get("response")) if result["status"] == "ok" else None
    if rows is not None and len(rows) >= 500:
        result["warnings"].append("event_snapshot_fixed_limit_no_recursive_pagination")
    return [result]


def extract_long_short_liquidations_raw(
    *, fetcher: RawFetcher, mode: str, reference_timestamp: int,
    execution_timestamp: int | None = None, minimum_event_window_seconds: int = 60, **plan_options: Any,
) -> dict[str, Any]:
    """Build and execute a Raw bundle while preserving every response.

    The fetch plan remains deterministic, but independent provider requests are
    executed concurrently. ``executor.map`` preserves plan order in the final
    Raw contract, so downstream normalization and audit trails remain stable.
    """
    executed_at = int(time.time()) if execution_timestamp is None else execution_timestamp
    plan = build_long_short_liquidations_fetch_plan(
        mode=mode, reference_timestamp=reference_timestamp, **plan_options,
    )
    for request in plan:
        validate_request_contract(request, allow_skipped=True)

    def execute_one(request: Mapping[str, Any]) -> list[dict[str, Any]]:
        if request["endpoint_id"] == "liquidation_order_events" and not request.get("skip_reason"):
            return _execute_event_window(
                fetcher=fetcher, request=request, minimum_event_window_seconds=minimum_event_window_seconds,
            )
        return [execute_raw_request(fetcher=fetcher, request=request)]

    workers = min(_fetch_worker_count(fetcher), max(1, len(plan)))
    if workers <= 1 or len(plan) <= 1:
        batches = [execute_one(request) for request in plan]
    else:
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="tradelatin-liquidations",
        ) as executor:
            batches = list(executor.map(execute_one, plan))

    results = [result for batch in batches for result in batch]
    return {
        "family": LONG_SHORT_LIQUIDATIONS_FAMILY, "stage": "extracted_raw", "mode": mode,
        "reference_timestamp": reference_timestamp, "execution_timestamp": executed_at,
        "requests": results,
    }


class LongShortLiquidationsRawExtractor:
    """Configured facade around plan construction and isolated execution."""

    def __init__(
        self, *, fetcher: RawFetcher, asset: str = DEFAULT_ASSET,
        exchanges: Sequence[str] = DEFAULT_EXCHANGES, exchange_pairs: Mapping[str, str] | None = None,
        cryptoquant_exchanges: Sequence[str] | None = None, reference_timestamp: int | None = None,
        clock: Clock | None = None, history_hours: int = DEFAULT_HISTORY_HOURS,
        incremental_overlap_hours: int = DEFAULT_INCREMENTAL_OVERLAP_H,
        event_lookback_hours: int = DEFAULT_EVENT_LOOKBACK_H,
        event_overlap_minutes: int = DEFAULT_EVENT_OVERLAP_MINUTES,
        min_event_usd: int | float = DEFAULT_MIN_EVENT_USD, map_range: str = DEFAULT_MAP_RANGE,
        exchange_range: str = DEFAULT_EXCHANGE_RANGE, max_pain_range: str = DEFAULT_MAX_PAIN_RANGE,
        minimum_event_window_seconds: int = 60,
    ) -> None:
        self.fetcher = fetcher
        self.asset = asset
        self.exchanges = tuple(exchanges)
        self.exchange_pairs = deepcopy(dict(exchange_pairs or {}))
        del cryptoquant_exchanges, exchange_range, max_pain_range
        self.clock = clock or time.time
        self.reference_timestamp = reference_timestamp
        self.history_hours = history_hours
        self.incremental_overlap_hours = incremental_overlap_hours
        self.event_lookback_hours = event_lookback_hours
        self.event_overlap_minutes = event_overlap_minutes
        self.min_event_usd = min_event_usd
        self.map_range = map_range
        self.minimum_event_window_seconds = minimum_event_window_seconds

    def _options(self) -> dict[str, Any]:
        return {
            "asset": self.asset, "exchanges": self.exchanges, "exchange_pairs": self.exchange_pairs,
            "history_hours": self.history_hours,
            "incremental_overlap_hours": self.incremental_overlap_hours,
            "event_lookback_hours": self.event_lookback_hours,
            "event_overlap_minutes": self.event_overlap_minutes, "min_event_usd": self.min_event_usd,
            "map_range": self.map_range,
        }

    def build_fetch_plan(self, *, mode: str, recovery_requests: Sequence[Mapping[str, Any]] | None = None,
                         event_cursors: Mapping[str, int] | None = None,
                         refresh_discovery: bool = False, include_confirmations: bool = True, reuse_hourly: bool = False) -> list[dict[str, Any]]:
        reference = int(self.clock()) if self.reference_timestamp is None else self.reference_timestamp
        return build_long_short_liquidations_fetch_plan(
            mode=mode, reference_timestamp=reference, recovery_requests=recovery_requests,
            event_cursors=event_cursors, refresh_discovery=refresh_discovery,
            include_confirmations=include_confirmations, reuse_hourly=reuse_hourly, **self._options(),
        )

    def run(self, *, mode: str, recovery_requests: Sequence[Mapping[str, Any]] | None = None,
            event_cursors: Mapping[str, int] | None = None,
            refresh_discovery: bool = False, include_confirmations: bool = True, reuse_hourly: bool = False) -> dict[str, Any]:
        execution = int(self.clock())
        reference = execution if self.reference_timestamp is None else self.reference_timestamp
        return extract_long_short_liquidations_raw(
            fetcher=self.fetcher, mode=mode, reference_timestamp=reference,
            execution_timestamp=execution, minimum_event_window_seconds=self.minimum_event_window_seconds,
            recovery_requests=recovery_requests, event_cursors=event_cursors,
            refresh_discovery=refresh_discovery, include_confirmations=include_confirmations, reuse_hourly=reuse_hourly, **self._options(),
        )
