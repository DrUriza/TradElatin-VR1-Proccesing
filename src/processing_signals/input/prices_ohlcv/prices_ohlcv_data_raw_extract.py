from __future__      import annotations
from collections.abc import Callable, Mapping, Sequence
from typing          import Any

PRICES_FAMILY        = "prices_ohlcv"
COINGLASS_PROVIDER   = "coinglass"
GLASSNODE_PROVIDER    = "glassnode"
SPOT_ENDPOINT_ID     = "spot_ohlcv"
GLASSNODE_MARKET_CAP_ENDPOINT_ID = "marketcap_usd"
GLASSNODE_MARKET_CAP_PATH = "/v1/metrics/market/marketcap_usd"
SPOT_ENDPOINT_PATH   = "/api/spot/price/history"
BOOTSTRAP_TIMEFRAMES = ("1m", "5m", "15m", "4h")
INCREMENTAL_LIMITS   = {"1m": 15, "15m": 8}
VALID_MODES          = {"bootstrap", "incremental", "recovery"}

PricesFetcher = Callable[..., Any]


def build_prices_fetch_plan(*, mode: str, requests: Sequence[Mapping[str, Any]] | None = None, bootstrap: int = 500,
                            incremental: Mapping[str, int] | None = None, recovery_requests: Sequence[Mapping[str, Any]] | None = None,
                            bootstrap_limit: int | None = None, incremental_limits: Mapping[str, int] | None = None,
                            refresh_secondary: bool = False) -> list[dict[str, Any]]:
    """Build the exact CoinGlass requests required for one Prices run."""
    requests    = recovery_requests if recovery_requests is not None else requests
    bootstrap   = bootstrap_limit if bootstrap_limit is not None else bootstrap
    incremental = incremental_limits if incremental_limits is not None else incremental
    if mode not in VALID_MODES:
        raise ValueError(f"Unsupported Prices input mode: {mode}")
    if bootstrap <= 0:
        raise ValueError("bootstrap must be positive")
    if mode == "bootstrap":
        # VR1 final endpoint policy: Spot is the canonical Prices primitive.
        # Futures OHLC is derivable/contextual and is no longer part of the
        # contractual Emulator surface.  Keep the extractor for compatibility,
        # but do not request it in the default bootstrap/runtime.
        requests = [{"market": "spot", "timeframe": timeframe, "limit": bootstrap}
                    for timeframe in BOOTSTRAP_TIMEFRAMES]
    elif mode == "incremental":
        limits = dict(INCREMENTAL_LIMITS)
        limits.update(incremental or {})
        if not 3 <= int(limits["1m"]) <= 15:
            raise ValueError("incremental 1m limit must be between 3 and 15")
        if not 4 <= int(limits["15m"]) <= 8:
            raise ValueError("incremental 15m limit must be between 4 and 8")
        markets = ("spot",)
        requests = [{"market": market, "timeframe": timeframe, "limit": int(limit)}
                    for market in markets
                    for timeframe, limit in limits.items()]
    else:
        recovery_source = requests
        requests        = []
        for item in recovery_source or ():
            market    = str(item.get("market", ""))
            timeframe = str(item.get("timeframe", ""))
            limit     = int(item.get("limit", bootstrap))
            if market != "spot":
                raise ValueError(f"Invalid recovery market under final 33-endpoint policy: {market}")
            if timeframe not in BOOTSTRAP_TIMEFRAMES:
                raise ValueError(f"Invalid recovery timeframe: {timeframe}")
            if limit <= 0:
                raise ValueError("recovery limit must be positive")
            request = {"market": market, "timeframe": timeframe, "limit": limit}
            for key in ("start_time", "end_time"):
                if item.get(key) is not None:
                    request[key] = int(item[key])
            requests.append(request)
        if not requests:
            raise ValueError("recovery mode requires at least one recovery request")
    return requests

def build_coinglass_ohlc_params(*, symbol: str, exchange: str, timeframe: str, limit: int, start_time: int | None = None, end_time: int | None = None) -> dict[str, Any]:
    """Build provider parameters without performing I/O."""
    params: dict[str, Any] = {"symbol": symbol.upper(), "exchange": exchange, "interval": timeframe, "limit": int(limit)}
    if start_time is not None:
        params["start_time"] = int(start_time)
    if end_time is not None:
        params["end_time"] = int(end_time)
    return params


def build_glassnode_market_params(*, asset: str = "BTC", interval: str = "1h", start_time: int | None = None, end_time: int | None = None) -> dict[str, Any]:
    """Build Glassnode parameters for the unique Market Cap primitive."""
    params: dict[str, Any] = {"a": asset.upper(), "i": interval}
    if start_time is not None:
        params["s"] = int(start_time)
    if end_time is not None:
        params["u"] = int(end_time)
    return params


def extract_glassnode_prices_raw(*, fetcher: PricesFetcher, asset: str = "BTC", interval: str = "1h") -> dict[str, Any]:
    """Fetch the unique Glassnode Market Cap primitive used by the Prices KPI."""
    params = build_glassnode_market_params(asset=asset, interval=interval)
    output: dict[str, Any] = {}
    # CoinGlass Spot is the canonical price source.  The duplicate Glassnode
    # Price OHLC confirmation was removed from the paid bootstrap path; Market
    # Cap remains because it is a unique KPI primitive.
    for endpoint_id, path in (
        (GLASSNODE_MARKET_CAP_ENDPOINT_ID, GLASSNODE_MARKET_CAP_PATH),
    ):
        try:
            response = fetcher(provider=GLASSNODE_PROVIDER, endpoint_id=endpoint_id, path=path, params=params)
            output[endpoint_id] = {"status": "ok", "params": dict(params), "response": response}
        except Exception as exc:
            # Glassnode is confirmation/fallback for Price and primary only for Market Cap.
            # A provider failure must not invalidate CoinGlass Spot OHLC ingestion.
            output[endpoint_id] = {"status": "error", "params": dict(params), "response": None, "error": str(exc)}
    return {"provider": GLASSNODE_PROVIDER, "interval": interval, "metrics": output}


def extract_spot_ohlcv_raw(*, fetcher: PricesFetcher, fetch_plan: Sequence[Mapping[str, Any]], symbol: str = "BTCUSDT", exchange: str = "Binance") -> dict[str, Any]:
    timeframes: dict[str, dict[str, Any]] = {}
    for request in fetch_plan:
        if request.get("market") != "spot":
            continue
        timeframe = str(request["timeframe"])
        params = build_coinglass_ohlc_params(
            symbol=symbol, exchange=exchange, timeframe=timeframe, limit=int(request["limit"]),
            start_time=request.get("start_time"), end_time=request.get("end_time"),
        )
        try:
            response = fetcher(provider=COINGLASS_PROVIDER, endpoint_id=SPOT_ENDPOINT_ID, path=SPOT_ENDPOINT_PATH, params=params)
            timeframes[timeframe] = {"status": "ok", "params": params, "response": dict(response)}
        except Exception as exc:
            timeframes[timeframe] = {"status": "error", "params": params, "response": None, "error": str(exc)}
    return {"provider": COINGLASS_PROVIDER, "endpoint_id": SPOT_ENDPOINT_ID, "timeframes": timeframes}

class PricesOhlcvRawExtractor:
    """Stateful adapter for the two contracted Prices primitives: Spot OHLCV and Market Cap."""
    def __init__(self, *, fetcher: PricesFetcher, symbol: str = "BTCUSDT", exchange: str = "Binance", bootstrap: int = 500,
                 incremental: Mapping[str, int] | None = None, bootstrap_limit: int | None = None,
                 incremental_limits: Mapping[str, int] | None = None, include_glassnode: bool = True,
                 refresh_secondary: bool = False) -> None:
        self.fetcher            = fetcher
        self.symbol             = symbol
        self.exchange           = exchange
        self.bootstrap_limit    = bootstrap_limit if bootstrap_limit is not None else bootstrap
        self.incremental_limits = dict(incremental_limits if incremental_limits is not None else (incremental or {}))
        self.include_glassnode   = bool(include_glassnode)
        self.refresh_secondary   = bool(refresh_secondary)

    def build_fetch_plan(self, *, mode: str, requests: Sequence[Mapping[str, Any]] | None = None) -> list[dict[str, Any]]:
        return build_prices_fetch_plan(mode=mode, requests=requests, bootstrap=self.bootstrap_limit,
                                       incremental=self.incremental_limits, refresh_secondary=self.refresh_secondary)

    def build_params(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return build_coinglass_ohlc_params(symbol=self.symbol, exchange=self.exchange, timeframe=str(request["timeframe"]),
                                           limit=int(request["limit"]), start_time=request.get("start_time"), end_time=request.get("end_time"))

    def extract_spot(self, fetch_plan: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        return extract_spot_ohlcv_raw(fetcher=self.fetcher, fetch_plan=fetch_plan, symbol=self.symbol, exchange=self.exchange)


    def run(self, *, mode: str, requests: Sequence[Mapping[str, Any]] | None = None, recovery_requests: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
        fetch_plan = self.build_fetch_plan(mode=mode, requests=recovery_requests if recovery_requests is not None else requests)
        raw: dict[str, Any] = {"spot": self.extract_spot(fetch_plan)}
        # Market Cap is slow-moving. Bootstrap it once; incremental runs reuse
        # persisted values unless a scheduled secondary refresh is requested.
        if self.include_glassnode and (mode == "bootstrap" or self.refresh_secondary):
            asset = self.symbol.upper().removesuffix("USDT").removesuffix("USD") or "BTC"
            raw["glassnode"] = extract_glassnode_prices_raw(fetcher=self.fetcher, asset=asset, interval="1h")
        return {"family": PRICES_FAMILY, "mode": mode, "raw": raw}

def extract_prices_ohlcv_raw(*, fetcher: PricesFetcher, mode: str, symbol: str = "BTCUSDT", exchange: str = "Binance",
                             requests: Sequence[Mapping[str, Any]] | None = None, bootstrap: int = 500,
                             incremental: Mapping[str, int] | None = None, include_glassnode: bool = True,
                             refresh_secondary: bool = False) -> dict[str, Any]:
    """Public compatibility facade for the OO raw extractor."""
    extractor = PricesOhlcvRawExtractor(fetcher=fetcher, symbol=symbol, exchange=exchange, bootstrap=bootstrap, incremental=incremental,
                                        include_glassnode=include_glassnode, refresh_secondary=refresh_secondary)
    return extractor.run(mode=mode, requests=requests)
