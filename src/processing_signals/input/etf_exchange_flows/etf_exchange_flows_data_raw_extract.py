"""Raw extraction for the canonical ETF and exchange flows Input family."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

FAMILY = "etf_exchange_flows"
VALID_MODES = {"bootstrap", "incremental", "recovery"}
PROVIDERS = ("coinglass", "cryptoquant")
SUPPORTED_WINDOWS = {"hour", "day"}
RECOVERY_ALLOWED_FIELDS = {
    "coinglass": {"provider", "endpoint_id", "start_time", "end_time", "limit", "ticker", "symbol"},
    "cryptoquant": {"provider", "endpoint_id", "window", "start_time", "end_time", "limit", "exchange_scope"},
}
BOOTSTRAP_LIMITS = {"cryptoquant_hour": 48, "cryptoquant_day": 730}
INCREMENTAL_LIMITS = {"cryptoquant_hour": 48, "cryptoquant_day": 8}
ETF_HISTORICAL_BOOTSTRAP_LIMITS = {
    ("exchange_inflow", "day"): 730,
    ("exchange_outflow", "day"): 730,
    ("exchange_reserve", "day"): 730,
}
# Runtime request groups. CoinGlass ETF flow is hourly-cached; the catalog is
# fetched at bootstrap. Exchange net flow is derived in Processing as inflow - outflow
# from the three contracted CryptoQuant primitives; there is no netflow endpoint.
HOURLY_COINGLASS_ENDPOINTS = ("bitcoin_etf_flows",)
SLOW_COINGLASS_ENDPOINTS: tuple[str, ...] = ()
BOOTSTRAP_STATIC_COINGLASS_ENDPOINTS = ("bitcoin_etf_list",)
PRIMARY_CRYPTOQUANT_ENDPOINTS = ("exchange_inflow", "exchange_outflow", "exchange_reserve")
ProviderFetcher = Callable[..., Mapping[str, Any] | Sequence[Any]]

ENDPOINT_SPECS = {
    "coinglass": {
        "bitcoin_etf_flows": {"path": "/api/etf/bitcoin/flow-history", "widgets": ["ETF flows"]},
        "bitcoin_etf_list": {"path": "/api/etf/bitcoin/list", "widgets": ["ETF fund catalog and AUM"]},
    },
    "cryptoquant": {
        "exchange_inflow": {"path": "/btc/exchange-flows/inflow", "widgets": ["Exchange inflow"]},
        "exchange_outflow": {"path": "/btc/exchange-flows/outflow", "widgets": ["Exchange outflow"]},
        "exchange_reserve": {"path": "/btc/exchange-flows/reserve", "widgets": ["Exchange reserve"]},
    },
}
PRIMARY_ENDPOINT_IDS = tuple((*ENDPOINT_SPECS["coinglass"], *ENDPOINT_SPECS["cryptoquant"]))
SECONDARY_ENDPOINT_IDS: tuple[str, ...] = ()


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"invalid_{name}")
    return value


def _validate_recovery_requests(recovery_requests: Sequence[Mapping[str, Any]] | None, *,
                                exchange_scope: str | None, symbol: str) -> list[Mapping[str, Any]]:
    if (not isinstance(recovery_requests, Sequence) or isinstance(recovery_requests, (str, bytes))
            or not recovery_requests):
        raise ValueError("recovery_requests_required")
    validated = []
    for item in recovery_requests:
        if not isinstance(item, Mapping):
            raise ValueError("invalid_recovery_request")
        provider, endpoint = item.get("provider"), item.get("endpoint_id")
        if provider not in ENDPOINT_SPECS or endpoint not in ENDPOINT_SPECS[provider]:
            raise ValueError("invalid_recovery_endpoint")
        unknown = set(item) - RECOVERY_ALLOWED_FIELDS[provider]
        if unknown:
            raise ValueError("unsupported_recovery_field")
        for field in ("start_time", "end_time"):
            if field in item:
                _positive_int(item[field], field)
        if "limit" in item:
            _positive_int(item["limit"], "limit")
        if "start_time" in item and "end_time" in item and item["start_time"] > item["end_time"]:
            raise ValueError("invalid_time_range")
        if provider == "coinglass":
            unsupported = {field for field in ("start_time", "end_time", "limit") if field in item}
            if unsupported:
                raise ValueError("unsupported_coinglass_recovery_field")
            build_coinglass_params(endpoint, symbol=item.get("symbol", symbol), ticker=item.get("ticker", "GBTC"))
        elif provider == "cryptoquant":
            build_cryptoquant_params(exchange_scope=item.get("exchange_scope", exchange_scope), window=item.get("window"),
                limit=item.get("limit", 100), start_time=item.get("start_time"), end_time=item.get("end_time"))
        else:
            raise ValueError("provider_not_in_final33")
        validated.append(item)
    return validated


def _utc_iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def build_coinglass_params(endpoint_id: str, *, symbol: str = "BTC", ticker: str = "GBTC") -> dict[str, Any]:
    del symbol, ticker
    if endpoint_id not in ENDPOINT_SPECS["coinglass"]:
        raise ValueError("unknown_coinglass_endpoint")
    return {}


def build_cryptoquant_params(*, exchange_scope: str, window: str, limit: int,
                             start_time: int | None = None, end_time: int | None = None) -> dict[str, Any]:
    if not isinstance(exchange_scope, str) or not exchange_scope:
        raise ValueError("invalid_exchange_scope")
    if window not in SUPPORTED_WINDOWS:
        raise ValueError("invalid_cryptoquant_window")
    params = {"exchange": exchange_scope, "window": window, "limit": _positive_int(limit, "limit"), "format": "json"}
    if start_time is not None:
        params["from"] = datetime.fromtimestamp(_positive_int(start_time, "start_time"), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if end_time is not None:
        params["to"] = datetime.fromtimestamp(_positive_int(end_time, "end_time"), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if start_time is not None and end_time is not None and start_time > end_time:
        raise ValueError("invalid_time_range")
    return params



def _request(provider: str, endpoint_id: str, params: Mapping[str, Any], variant: str | None = None) -> dict[str, Any]:
    return {"provider": provider, "endpoint_id": endpoint_id, "path": ENDPOINT_SPECS[provider][endpoint_id]["path"],
            "params": deepcopy(dict(params)), "variant": variant}


def build_etf_exchange_flows_fetch_plan(*, mode: str, exchange_scope: str | None, symbol: str = "BTC",
                                        include_secondary: bool = False, refresh_hourly: bool = True,
                                        refresh_slow: bool = True,
                                        recovery_requests: Sequence[Mapping[str, Any]] | None = None,
                                        bootstrap_limits: Mapping[str, int] | None = None,
                                        incremental_limits: Mapping[str, int] | None = None) -> list[dict[str, Any]]:
    if mode not in VALID_MODES:
        raise ValueError("invalid_mode")
    if mode == "recovery":
        recovery_requests = _validate_recovery_requests(recovery_requests, exchange_scope=exchange_scope, symbol=symbol)
        plan = []
        for item in recovery_requests:
            provider, endpoint = item.get("provider"), item.get("endpoint_id")
            start, end, limit = item.get("start_time"), item.get("end_time"), item.get("limit", 100)
            if provider == "cryptoquant":
                params = build_cryptoquant_params(exchange_scope=item.get("exchange_scope", exchange_scope),
                    window=item.get("window"), limit=limit, start_time=start, end_time=end)
                variant = item.get("window")
            else:
                params = build_coinglass_params(endpoint, symbol=item.get("symbol", symbol), ticker=item.get("ticker", "GBTC"))
                variant = None
            plan.append(_request(provider, endpoint, params, variant))
        return plan
    if not exchange_scope:
        raise ValueError("exchange_scope_required")
    limits = {**(BOOTSTRAP_LIMITS if mode == "bootstrap" else INCREMENTAL_LIMITS),
              **dict((bootstrap_limits if mode == "bootstrap" else incremental_limits) or {})}
    plan: list[dict[str, Any]] = []

    if mode == "bootstrap" or refresh_hourly:
        for endpoint in HOURLY_COINGLASS_ENDPOINTS:
            plan.append(_request("coinglass", endpoint, build_coinglass_params(endpoint, symbol=symbol)))

    if mode == "bootstrap" or refresh_slow:
        for endpoint in SLOW_COINGLASS_ENDPOINTS:
            plan.append(_request("coinglass", endpoint, build_coinglass_params(endpoint, symbol=symbol)))

    if mode == "bootstrap":
        for endpoint in BOOTSTRAP_STATIC_COINGLASS_ENDPOINTS:
            plan.append(_request("coinglass", endpoint, build_coinglass_params(endpoint, symbol=symbol)))
        # Deep history is paid only once.  Hourly windows remain short and are
        # used for the exact rolling-24h KPIs; day history feeds Screen A/B.
        for endpoint in PRIMARY_CRYPTOQUANT_ENDPOINTS:
            for window in ("day", "hour"):
                limit = limits[f"cryptoquant_{window}"]
                limit = max(limit, ETF_HISTORICAL_BOOTSTRAP_LIMITS.get((endpoint, window), limit))
                plan.append(_request("cryptoquant", endpoint, build_cryptoquant_params(
                    exchange_scope=exchange_scope, window=window, limit=_positive_int(limit, "limit")), window))
    elif refresh_hourly:
        # One short hourly request per primitive.  Input rolls these observations
        # into the current UTC day locally; no duplicate day request is needed.
        for endpoint in PRIMARY_CRYPTOQUANT_ENDPOINTS:
            limit = limits["cryptoquant_hour"]
            plan.append(_request("cryptoquant", endpoint, build_cryptoquant_params(
                exchange_scope=exchange_scope, window="hour", limit=_positive_int(limit, "limit")), "hour"))

    if include_secondary:
        raise ValueError("secondary_endpoints_removed_final33")
    return plan


def sanitize_provider_error(error: Any) -> str:
    text = str(error)
    for token in ("Authorization", "CG-API-KEY", "api_key", "apikey", "token"):
        if token.lower() in text.lower():
            return "provider_error_redacted"
    return text[:500]


def extract_endpoint_raw(*, fetcher: ProviderFetcher, request: Mapping[str, Any], fetched_at: str) -> dict[str, Any]:
    try:
        response = fetcher(provider=request["provider"], endpoint_id=request["endpoint_id"], path=request["path"],
                           params=deepcopy(request["params"]))
        if not isinstance(response, (Mapping, Sequence)) or isinstance(response, (str, bytes)):
            raise TypeError("unsupported_provider_body")
        body, status, error = deepcopy(response), "ok", None
    except Exception as exc:
        body, status, error = None, "error", sanitize_provider_error(exc)
    return {"status": status, "path": request["path"], "params": deepcopy(request["params"]),
            "response": body, "error": error, "fetched_at": fetched_at}


def extract_etf_exchange_flows_raw(*, fetcher: ProviderFetcher, mode: str, exchange_scope: str | None,
                                   symbol: str = "BTC", include_secondary: bool = False,
                                   refresh_hourly: bool = True, refresh_slow: bool = True,
                                   recovery_requests: Sequence[Mapping[str, Any]] | None = None,
                                   data_mode: str = "live", is_demo: bool = False, now: int,
                                   bootstrap_limits: Mapping[str, int] | None = None,
                                   incremental_limits: Mapping[str, int] | None = None) -> dict[str, Any]:
    if data_mode not in {"live", "synthetic"} or (data_mode == "synthetic" and is_demo is not True):
        raise ValueError("invalid_data_mode")
    timestamp = _positive_int(now, "now")
    plan = build_etf_exchange_flows_fetch_plan(mode=mode, exchange_scope=exchange_scope, symbol=symbol,
        include_secondary=include_secondary, refresh_hourly=refresh_hourly, refresh_slow=refresh_slow,
        recovery_requests=recovery_requests, bootstrap_limits=bootstrap_limits, incremental_limits=incremental_limits)
    requested_at, raw = _utc_iso(timestamp), {provider: {} for provider in PROVIDERS}
    for request in plan:
        entry = extract_endpoint_raw(fetcher=fetcher, request=request, fetched_at=requested_at)
        target = raw[request["provider"]].setdefault(request["endpoint_id"], {})
        if request["variant"] is None:
            raw[request["provider"]][request["endpoint_id"]] = entry
        else:
            target[str(request["variant"])] = entry
    return {
        "schema": {"id": "trad_elatin.etf_exchange_flows.extracted_raw.v1", "version": "1.0.0"},
        "family": FAMILY,
        "stage": "extracted_raw",
        "mode": mode,
        "data_mode": data_mode,
        "is_demo": is_demo,
        "context": {
            "asset": symbol,
            "exchange_scope": exchange_scope,
            "include_secondary": include_secondary,
            "refresh_hourly": refresh_hourly,
            "refresh_slow": refresh_slow,
            "refresh_timestamp": timestamp,
        },
        "requested_at": requested_at,
        "raw": raw,
    }


class EtfExchangeFlowsRawExtractor:
    def __init__(self, *, fetcher: ProviderFetcher, exchange_scope: str | None, symbol: str = "BTC",
                 include_secondary: bool = False, data_mode: str = "live", is_demo: bool = False) -> None:
        self.options = {"fetcher": fetcher, "exchange_scope": exchange_scope, "symbol": symbol,
                        "include_secondary": include_secondary, "data_mode": data_mode, "is_demo": is_demo}

    def run(self, *, mode: str, now: int, recovery_requests=None, bootstrap_limits=None, incremental_limits=None,
                refresh_hourly: bool = True, refresh_slow: bool = True):
        return extract_etf_exchange_flows_raw(mode=mode, now=now, recovery_requests=recovery_requests,
            bootstrap_limits=bootstrap_limits, incremental_limits=incremental_limits,
            refresh_hourly=refresh_hourly, refresh_slow=refresh_slow, **self.options)
