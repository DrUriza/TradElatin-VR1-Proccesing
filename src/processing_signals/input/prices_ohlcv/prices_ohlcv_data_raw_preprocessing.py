from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing          import Any

from .prices_ohlcv_data_raw_extract import (
    BOOTSTRAP_TIMEFRAMES,
    PricesFetcher,
    PricesOhlcvRawExtractor,
)


OHLC_FIELDS = ("open", "high", "low", "close")


def determine_prices_input_mode(
    *,
    existing_contract: Mapping[str, Any] | None = None,
    recovery_requests: Sequence[Mapping[str, Any]] | None = None,
    requested_mode: str | None = None,
) -> str:
    if requested_mode is not None:
        if requested_mode not in {"bootstrap", "incremental", "recovery"}:
            raise ValueError(f"Unsupported Prices input mode: {requested_mode}")
        if requested_mode == "recovery" and not recovery_requests:
            raise ValueError("recovery mode requires recovery_requests")
        return requested_mode
    if recovery_requests:
        return "recovery"
    markets = (existing_contract or {}).get("markets", {})
    # Spot is the only required Prices primitive in the final 33-endpoint
    # runtime. Futures is a structural unavailable placeholder and must never
    # force a perpetual bootstrap loop.
    timeframes = markets.get("spot", {}).get("timeframes", {}) if isinstance(markets, Mapping) else {}
    if not all(timeframes.get(timeframe, {}).get("records") for timeframe in BOOTSTRAP_TIMEFRAMES):
        return "bootstrap"
    return "incremental"


def unwrap_coinglass_ohlcv(response: Mapping[str, Any] | Sequence[Any] | None) -> list[Any]:
    if response is None:
        return []
    if isinstance(response, Sequence) and not isinstance(response, (str, bytes, bytearray)):
        return list(response)
    if not isinstance(response, Mapping):
        raise ValueError("CoinGlass OHLC response must be a mapping or sequence")
    code = response.get("code")
    if code not in (None, 0, "0", 200, "200"):
        raise ValueError(f"CoinGlass OHLC request failed: {response.get('msg') or code}")
    data: Any = response.get("data", [])
    while isinstance(data, Mapping):
        for key in ("list", "rows", "items", "data"):
            if key in data:
                data = data[key]
                break
        else:
            return []
    return list(data) if isinstance(data, Sequence) and not isinstance(data, (str, bytes, bytearray)) else []


def normalize_ohlcv_record(record: Mapping[str, Any] | Sequence[Any]) -> dict[str, float | int]:
    if isinstance(record, Mapping):
        timestamp = record.get("timestamp", record.get("time", record.get("t")))
        values    = {field: record.get(field) for field in OHLC_FIELDS}
        volume    = record.get("volume_usd", record.get("volume", record.get("vol", 0.0)))
    elif isinstance(record, Sequence) and not isinstance(record, (str, bytes, bytearray)):
        if len(record) < 5:
            raise ValueError("OHLC sequence requires timestamp, open, high, low and close")
        timestamp = record[0]
        values    = dict(zip(OHLC_FIELDS, record[1:5], strict=True))
        volume    = record[5] if len(record) > 5 else 0.0
    else:
        raise ValueError("OHLC record must be a mapping or sequence")

    if timestamp is None or any(values[field] is None for field in OHLC_FIELDS):
        raise ValueError("OHLC record is missing timestamp or price fields")
    normalized_timestamp = int(float(timestamp))
    if normalized_timestamp > 100_000_000_000:
        normalized_timestamp //= 1000

    normalized = {
        "timestamp": normalized_timestamp,
        **{field: float(values[field]) for field in OHLC_FIELDS},
        "volume_usd": float(volume or 0.0),
    }
    if normalized["high"] < max(normalized["open"], normalized["close"], normalized["low"]):
        raise ValueError("OHLC high is below another price field")
    if normalized["low"] > min(normalized["open"], normalized["close"], normalized["high"]):
        raise ValueError("OHLC low is above another price field")
    return normalized



def unwrap_glassnode_series(response: Any) -> list[Mapping[str, Any]]:
    if response is None:
        return []
    if not isinstance(response, Sequence) or isinstance(response, (str, bytes, bytearray)):
        raise ValueError("Glassnode market response must be a sequence")
    rows: list[Mapping[str, Any]] = []
    for row in response:
        if not isinstance(row, Mapping):
            raise ValueError("Glassnode market row must be a mapping")
        rows.append(row)
    return rows


def normalize_glassnode_market_cap_record(record: Mapping[str, Any]) -> dict[str, Any]:
    timestamp = record.get("t", record.get("timestamp"))
    value = record.get("v", record.get("value"))
    if timestamp is None or value is None:
        raise ValueError("Glassnode Market Cap row is missing timestamp or value")
    ts = int(float(timestamp))
    if ts > 100_000_000_000:
        ts //= 1000
    number = float(value)
    if number < 0:
        raise ValueError("Glassnode Market Cap cannot be negative")
    return {"timestamp": ts, "value": number, "unit": "USD", "provider": "glassnode", "endpoint_id": "marketcap_usd"}


def preprocess_glassnode_prices(raw_glassnode: Mapping[str, Any] | None, existing: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Normalize the sole contracted Glassnode Prices primitive: Market Cap."""
    raw_glassnode = raw_glassnode or {}
    metrics = raw_glassnode.get("metrics", {}) if isinstance(raw_glassnode, Mapping) else {}
    prior = existing or {}
    warnings: list[str] = []
    payload = metrics.get("marketcap_usd", {}) if isinstance(metrics, Mapping) else {}
    incoming: list[dict[str, Any]] = []
    if payload.get("status") == "ok":
        try:
            for index, row in enumerate(unwrap_glassnode_series(payload.get("response"))):
                try:
                    incoming.append(normalize_glassnode_market_cap_record(row))
                except (TypeError, ValueError) as exc:
                    warnings.append(f"glassnode/marketcap_usd/record[{index}]: {exc}")
        except ValueError as exc:
            warnings.append(f"glassnode/marketcap_usd: {exc}")
    elif payload:
        warnings.append(f"glassnode/marketcap_usd: {payload.get('error') or 'request_failed'}")
    previous = prior.get("records", []) if isinstance(prior, Mapping) else []
    by_ts = {int(row["timestamp"]): dict(row) for row in previous if isinstance(row, Mapping) and row.get("timestamp") is not None}
    by_ts.update({int(row["timestamp"]): dict(row) for row in incoming})
    records = [by_ts[key] for key in sorted(by_ts)]
    result = {
        "market_cap": {"provider": "glassnode", "endpoint_id": "marketcap_usd", "interval": raw_glassnode.get("interval", "1h"),
                       "status": "available" if records else "unavailable", "records": records,
                       "current": deepcopy(records[-1]) if records else None},
    }
    if warnings:
        result["market_cap"]["warnings"] = warnings
    return result

def upsert_ohlcv_records(
    existing_records: Sequence[Mapping[str, Any]],
    incoming_records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    by_timestamp = {int(record["timestamp"]): dict(record) for record in existing_records}
    by_timestamp.update({int(record["timestamp"]): dict(record) for record in incoming_records})
    return [by_timestamp[timestamp] for timestamp in sorted(by_timestamp)]


def preprocess_market_response(
    *,
    raw_market: Mapping[str, Any],
    existing_market: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    output_timeframes: dict[str, dict[str, Any]] = {}
    existing_timeframes = (existing_market or {}).get("timeframes", {})
    for timeframe, raw_timeframe in raw_market.get("timeframes", {}).items():
        warnings: list[str] = []
        incoming: list[dict[str, Any]] = []
        if raw_timeframe.get("status") == "ok":
            try:
                for index, record in enumerate(unwrap_coinglass_ohlcv(raw_timeframe.get("response"))):
                    try:
                        incoming.append(normalize_ohlcv_record(record))
                    except (TypeError, ValueError) as exc:
                        warnings.append(f"record[{index}]: {exc}")
            except ValueError as exc:
                warnings.append(str(exc))
        else:
            warnings.append(str(raw_timeframe.get("error") or "request_failed"))

        previous = existing_timeframes.get(timeframe, {}).get("records", [])
        output_timeframes[str(timeframe)] = {
            "incoming_records": incoming,
            "records": upsert_ohlcv_records(previous, incoming),
            "warnings": warnings,
        }

    for timeframe, previous_payload in existing_timeframes.items():
        if str(timeframe) not in output_timeframes:
            cached = deepcopy(dict(previous_payload))
            cached["incoming_records"] = []
            output_timeframes[str(timeframe)] = cached

    return {
        "provider": raw_market.get("provider", "coinglass"),
        "endpoint_id": raw_market.get("endpoint_id"),
        "timeframes": output_timeframes,
    }



def evaluate_prices_input_quality(markets: Mapping[str, Any]) -> dict[str, Any]:
    warnings: list[str] = []
    errors: list[str] = []
    statuses: dict[str, str] = {}
    for market in ("spot", "futures"):
        timeframes = markets.get(market, {}).get("timeframes", {})
        has_records = bool(timeframes) and all(payload.get("records") for payload in timeframes.values())
        has_unavailable = any(payload.get("unavailable_records") for payload in timeframes.values())
        if market == "futures":
            statuses[market] = "unavailable"
            # Structural compatibility only; Futures OHLC is not a required
            # source and therefore must not trigger recovery.
            continue
        statuses[market] = "ok" if has_records and not has_unavailable else "partial"
        for timeframe, payload in timeframes.items():
            for warning in payload.get("warnings", []):
                warnings.append(f"{market}/{timeframe}: {warning}")
            unavailable = payload.get("unavailable_records", [])
            if unavailable:
                warnings.append(f"{market}/{timeframe}: {len(unavailable)} unsynchronized timestamps")
    recovery_required = statuses.get("spot") != "ok"
    return {
        **statuses,
        "recovery_required": recovery_required,
        "warnings": warnings,
        "errors": errors,
    }


class PricesOhlcvInputPreprocessor:
    """Orchestrate normalization, persistence merge and quality for Spot and Futures."""

    def __init__(
        self,
        *,
        raw_extractor: PricesOhlcvRawExtractor,
        existing_contract: Mapping[str, Any] | None = None,
    ) -> None:
        self.raw_extractor     = raw_extractor
        self.existing_contract = dict(existing_contract or {})

    def determine_mode(
        self,
        *,
        requested_mode: str | None = None,
        recovery_requests: Sequence[Mapping[str, Any]] | None = None,
    ) -> str:
        return determine_prices_input_mode(
            existing_contract=self.existing_contract,
            recovery_requests=recovery_requests,
            requested_mode=requested_mode,
        )

    @staticmethod
    def unwrap_response(response: Mapping[str, Any] | Sequence[Any] | None) -> list[Any]:
        return unwrap_coinglass_ohlcv(response)

    @staticmethod
    def normalize_record(record: Mapping[str, Any] | Sequence[Any]) -> dict[str, float | int]:
        return normalize_ohlcv_record(record)

    @staticmethod
    def upsert_records(
        existing_records: Sequence[Mapping[str, Any]],
        incoming_records: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        return upsert_ohlcv_records(existing_records, incoming_records)

    @staticmethod
    def evaluate_quality(markets: Mapping[str, Any]) -> dict[str, Any]:
        return evaluate_prices_input_quality(markets)

    def preprocess_market(
        self,
        *,
        market: str,
        raw_market: Mapping[str, Any],
    ) -> dict[str, Any]:
        existing_markets = self.existing_contract.get("markets", {})
        return preprocess_market_response(
            raw_market=raw_market,
            existing_market=existing_markets.get(market, {}),
        )

    def run(
        self,
        *,
        requested_mode: str | None = None,
        recovery_requests: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        mode = self.determine_mode(
            requested_mode=requested_mode,
            recovery_requests=recovery_requests,
        )
        raw = self.raw_extractor.run(
            mode=mode,
            recovery_requests=recovery_requests,
        )
        spot = self.preprocess_market(market="spot", raw_market=raw["raw"]["spot"])
        # Final 33-endpoint policy: Prices has one canonical external market,
        # CoinGlass Spot.  Keep a structural Futures placeholder because older
        # Processing/Classification contracts expose a Spot/Futures comparison,
        # but never carry stale Futures OHLC forward or fabricate it from Spot.
        futures = {
            "provider": "coinglass",
            "endpoint_id": None,
            "status": "unavailable",
            "reason": "retired_by_33_endpoint_policy",
            "timeframes": {
                timeframe: {
                    "incoming_records": [],
                    "records": [],
                    "warnings": ["retired_by_33_endpoint_policy"],
                    "status": "unavailable",
                    "reason": "retired_by_33_endpoint_policy",
                }
                for timeframe in BOOTSTRAP_TIMEFRAMES
            },
        }
        spot.update({"exchange": self.raw_extractor.exchange, "symbol": self.raw_extractor.symbol})
        futures.update({"exchange": self.raw_extractor.exchange, "symbol": self.raw_extractor.symbol})

        markets = {"spot": spot, "futures": futures}
        previous_features = self.existing_contract.get("provider_features", {})
        provider_features = preprocess_glassnode_prices(
            raw.get("raw", {}).get("glassnode"), existing=previous_features.get("market_cap", {})
        )
        return {
            "family": "prices_ohlcv",
            "stage": "input",
            "mode": mode,
            "context": {
                "price_market": "spot",
                "canonical_contract_market": "spot",
                "canonical_source_market": "spot",
                "available_markets": ["spot"],
                "symbol": self.raw_extractor.symbol,
                "exchange": self.raw_extractor.exchange,
            },
            "markets": markets,
            "provider_features": provider_features,
            "quality": self.evaluate_quality(markets),
        }


def run_prices_ohlcv_input(
    *,
    fetcher: PricesFetcher,
    symbol: str = "BTCUSDT",
    exchange: str = "Binance",
    existing_contract: Mapping[str, Any] | None = None,
    requested_mode: str | None = None,
    recovery_requests: Sequence[Mapping[str, Any]] | None = None,
    bootstrap_limit: int = 500,
    incremental_limits: Mapping[str, int] | None = None,
    include_glassnode: bool = True,
    refresh_secondary: bool = False,
    data_mode: str = "live",
    is_demo: bool = False,
    reference_timestamp: int | None = None,
    execution_timestamp: int | None = None,
) -> dict[str, Any]:
    """Single public family facade backed by the OO implementation."""
    raw_extractor = PricesOhlcvRawExtractor(
        fetcher=fetcher,
        symbol=symbol,
        exchange=exchange,
        bootstrap_limit=bootstrap_limit,
        incremental_limits=incremental_limits,
        include_glassnode=include_glassnode,
        refresh_secondary=refresh_secondary,
    )
    preprocessor = PricesOhlcvInputPreprocessor(
        raw_extractor=raw_extractor,
        existing_contract=existing_contract,
    )
    output = preprocessor.run(
        requested_mode=requested_mode,
        recovery_requests=recovery_requests,
    )
    output.setdefault("context", {}).update({
        "data_mode": data_mode,
        "is_demo": bool(is_demo),
        "reference_timestamp": reference_timestamp,
        "execution_timestamp": execution_timestamp,
    })
    return output
