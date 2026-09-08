"""CoinGlass-only raw extraction for the CVD volume/order-flow Input family.

VR1 deliberately uses only the four contracted CVD primitives: spot/futures
aggregated CVD and spot/perpetual footprint.  No dormant confirmation endpoints
are kept in this family.
"""
from __future__ import annotations

import copy
import math
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

CVD_VOLUME_ORDERFLOW_FAMILY = "cvd_volume_orderflow"
COINGLASS_PROVIDER           = "coinglass"
BASE_TIMEFRAMES              = ("5m", "15m")
FINAL_TIMEFRAMES             = ("5m", "15m", "4h")
# 5m and 15m are provider-native CVD bases. 4h is built from 15m (x16).
# No 1-minute CVD source exists in VR1.
BASE_REQUIRED_FACTORS        = {"5m": 1, "15m": 16}
EMULATOR_PAGE_RECORDS        = 500
VALID_MODES                  = {"bootstrap", "incremental", "recovery"}
FINAL_DISPLAY_RECORDS        = 220
FINAL_WARMUP_RECORDS         = 32
COINGLASS_CVD_MAX_LIMIT      = 4500
COINGLASS_FOOTPRINT_MAX_LIMIT = 1000
DEFAULT_INCREMENTAL_LIMITS   = {"5m": 12, "15m": 12}
DEFAULT_FOOTPRINT_HISTORY_SECONDS = 172800
TIMEFRAME_SECONDS            = {"5m": 300, "15m": 900}

COINGLASS_ENDPOINT_PATHS = {
    "spot_aggregated_cvd": "/api/spot/aggregated-cvd/history", "futures_aggregated_cvd": "/api/futures/aggregated-cvd/history",
    "spot_footprint": "/api/spot/volume/footprint-history", "futures_footprint": "/api/futures/volume/footprint-history",
}
CvdVolumeOrderflowFetcher = Callable[..., Mapping[str, Any] | Sequence[Any]]


def _positive_int(value: Any, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _timestamp(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer timestamp")
    return value


def _clock_timestamp(clock: Callable[[], Any] | None) -> int:
    value = time.time() if clock is None else clock()
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("clock must return a non-negative finite timestamp")
    return int(value)


def _iso_utc(value: int) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def _compact_utc(value: int) -> str:
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y%m%dT%H%M%S")


def required_base_records(timeframe: str, display_records: int = FINAL_DISPLAY_RECORDS, warmup_records: int = FINAL_WARMUP_RECORDS) -> int:
    _positive_int(display_records, "display_records")
    if type(warmup_records) is not int or warmup_records < 0:
        raise ValueError("warmup_records must be a non-negative integer")
    if timeframe not in BASE_TIMEFRAMES:
        raise ValueError("unsupported base timeframe")
    return (display_records + warmup_records) * BASE_REQUIRED_FACTORS[timeframe]


def build_coinglass_aggregated_cvd_params(*, exchanges: Sequence[str], symbol: str, timeframe: str, limit: int,
                                           start_timestamp: int | None = None, end_timestamp: int | None = None) -> dict[str, Any]:
    if timeframe not in BASE_TIMEFRAMES or not isinstance(symbol, str) or not symbol.strip() or not exchanges:
        raise ValueError("invalid CoinGlass CVD parameters")
    limit = _positive_int(limit, "limit")
    if limit > COINGLASS_CVD_MAX_LIMIT or any(not isinstance(item, str) or not item.strip() for item in exchanges):
        raise ValueError("invalid CoinGlass CVD limit or exchanges")
    params: dict[str, Any] = {"exchange_list": ",".join(item.strip() for item in exchanges), "symbol": symbol.strip().upper(),
                              "interval": timeframe, "limit": limit, "unit": "usd"}
    if start_timestamp is not None:
        params["start_time"] = _timestamp(start_timestamp, "start_timestamp") * 1000
    if end_timestamp is not None:
        params["end_time"] = _timestamp(end_timestamp, "end_timestamp") * 1000
    if start_timestamp is not None and end_timestamp is not None and start_timestamp > end_timestamp:
        raise ValueError("start_timestamp must not exceed end_timestamp")
    return params


def build_coinglass_footprint_params(*, exchange: str, symbol: str, timeframe: str = "5m", limit: int = COINGLASS_FOOTPRINT_MAX_LIMIT,
                                      start_timestamp: int | None = None, end_timestamp: int | None = None) -> dict[str, Any]:
    if not isinstance(exchange, str) or not exchange.strip() or not isinstance(symbol, str) or not symbol.strip() or timeframe not in BASE_TIMEFRAMES:
        raise ValueError("invalid CoinGlass footprint parameters")
    limit = _positive_int(limit, "limit")
    if limit > COINGLASS_FOOTPRINT_MAX_LIMIT:
        raise ValueError("footprint limit exceeds provider maximum")
    params: dict[str, Any] = {"exchange": exchange.strip(), "symbol": symbol.strip().upper(), "interval": timeframe, "limit": limit}
    if start_timestamp is not None:
        params["start_time"] = _timestamp(start_timestamp, "start_timestamp") * 1000
    if end_timestamp is not None:
        params["end_time"] = _timestamp(end_timestamp, "end_timestamp") * 1000
    return params



def _existing_input(existing_input: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if existing_input is None:
        return None
    if not isinstance(existing_input, Mapping):
        raise ValueError("existing_input is incompatible")
    candidate = existing_input.get("input", existing_input)
    if not isinstance(candidate, Mapping) or candidate.get("family") != CVD_VOLUME_ORDERFLOW_FAMILY or candidate.get("stage") != "input":
        raise ValueError("existing_input is incompatible")
    return candidate


def _last_timestamp(existing: Mapping[str, Any] | None, market: str, timeframe: str) -> int | None:
    rows = existing.get("markets", {}).get(market, {}).get("cvd", {}).get("timeframes", {}).get(timeframe, {}).get("records", []) if existing else []
    values = [row.get("timestamp") for row in rows if isinstance(row, Mapping) and type(row.get("timestamp")) is int]
    return max(values, default=None)


def _logical(*, provider: str, dataset: str, market: str, endpoint_id: str, path: str, timeframe: str | None,
             limit: int, start: int | None, end: int | None, records_required: int | None = None, **extra: Any) -> dict[str, Any]:
    identifier = f"{provider}:{market}:{dataset}" + (f":{timeframe}" if timeframe else "")
    result = {"logical_request_id": identifier, "provider": provider, "dataset": dataset, "market": market, "endpoint_id": endpoint_id,
              "path": path, "limit": limit, "start_timestamp": start, "end_timestamp": end, "pagination_required": bool(records_required and records_required > limit)}
    if timeframe is not None:
        result["timeframe"] = timeframe
    if records_required is not None:
        result["records_required"] = records_required
    result.update(copy.deepcopy(extra))
    return result


def build_cvd_volume_orderflow_fetch_plan(*, mode: str, reference_timestamp: int, base_asset: str = "BTC", pair_symbol: str = "BTCUSDT",
                                            exchanges: Sequence[str] = ("Binance", "OKX", "Bybit"), existing_input: Mapping[str, Any] | None = None,
                                            recovery_requests: Sequence[Mapping[str, Any]] | None = None, include_footprint: bool = True,
                                            footprint_exchanges: Sequence[str] = ("Binance", "OKX", "Bybit"),
                                            target_display_records: int = FINAL_DISPLAY_RECORDS, warmup_records: int = FINAL_WARMUP_RECORDS,
                                            incremental_limits: Mapping[str, int] | None = None,
                                            footprint_history_seconds: int = DEFAULT_FOOTPRINT_HISTORY_SECONDS,
                                            refresh_secondary: bool = False,
                                            emulator_fixed_records: bool = False) -> list[dict[str, Any]]:
    if mode not in VALID_MODES:
        raise ValueError("unsupported mode")
    reference = _timestamp(reference_timestamp, "reference_timestamp")
    existing = _existing_input(existing_input)
    limits = dict(DEFAULT_INCREMENTAL_LIMITS if incremental_limits is None else incremental_limits)
    if set(limits) != set(BASE_TIMEFRAMES):
        raise ValueError("incremental_limits must define 5m and 15m")
    plan: list[dict[str, Any]] = []
    if mode == "recovery":
        if not isinstance(recovery_requests, Sequence) or isinstance(recovery_requests, (str, bytes)) or not recovery_requests:
            raise ValueError("recovery_requests must be a non-empty sequence")
        for item in copy.deepcopy(recovery_requests):
            if not isinstance(item, Mapping):
                raise ValueError("invalid recovery request")
            market, timeframe = item.get("market"), item.get("timeframe")
            if market not in {"spot", "futures"} or timeframe not in BASE_TIMEFRAMES:
                raise ValueError("unsupported recovery request")
            start, end = _timestamp(item.get("start_timestamp"), "start_timestamp"), _timestamp(item.get("end_timestamp"), "end_timestamp")
            if start > end or end > reference:
                raise ValueError("invalid recovery range")
            required = int(item.get("records_required", (end - start) // TIMEFRAME_SECONDS[timeframe] + 1))
            endpoint = f"{market}_aggregated_cvd"
            plan.append(_logical(provider=COINGLASS_PROVIDER, dataset="aggregated_cvd", market=market, endpoint_id=endpoint,
                path=COINGLASS_ENDPOINT_PATHS[endpoint], timeframe=timeframe, limit=min(COINGLASS_CVD_MAX_LIMIT, required), start=start, end=end,
                records_required=required, exchanges=list(exchanges), symbol=base_asset))
        return plan
    for market in ("spot", "futures"):
        # Both 5m and 15m are provider-native CVD bases. Incremental refreshes
        # request both so their live right-edge buckets update independently;
        # 4h is derived later from the 15m base.
        requested_timeframes = BASE_TIMEFRAMES
        for timeframe in requested_timeframes:
            required = required_base_records(timeframe, target_display_records, warmup_records)
            if emulator_fixed_records:
                # Acquisition always sends limit=500 in Emulator mode. Keep the
                # logical page size identical so pagination advances instead of
                # misclassifying every 500-row response as a short provider page.
                limit = min(EMULATOR_PAGE_RECORDS, required)
            else:
                limit = min(COINGLASS_CVD_MAX_LIMIT, required) if mode == "bootstrap" else _positive_int(limits[timeframe], f"incremental_limits[{timeframe}]")
            end = reference
            prior = _last_timestamp(existing, market, timeframe)
            start = max(0, (end - (required - 1) * TIMEFRAME_SECONDS[timeframe])) if mode == "bootstrap" else max(0, (prior if prior is not None else end) - limit * TIMEFRAME_SECONDS[timeframe])
            endpoint = f"{market}_aggregated_cvd"
            plan.append(_logical(provider=COINGLASS_PROVIDER, dataset="aggregated_cvd", market=market, endpoint_id=endpoint,
                path=COINGLASS_ENDPOINT_PATHS[endpoint], timeframe=timeframe, limit=limit, start=start, end=end,
                records_required=required, exchanges=list(exchanges), symbol=base_asset))
    optional_start = max(0, reference - _positive_int(footprint_history_seconds, "footprint_history_seconds"))
    refresh_optional = mode != "incremental" or bool(refresh_secondary)
    if include_footprint and refresh_optional:
        for market in ("spot", "futures"):
            for exchange in footprint_exchanges:
                endpoint = "spot_footprint" if market == "spot" else "futures_footprint"
                plan.append(_logical(provider=COINGLASS_PROVIDER, dataset="footprint", market=market, endpoint_id=endpoint,
                    path=COINGLASS_ENDPOINT_PATHS[endpoint], timeframe="5m", limit=COINGLASS_FOOTPRINT_MAX_LIMIT,
                    start=optional_start, end=reference, exchange=exchange, symbol=pair_symbol))
    return plan


def _coinglass_rows(response: Any) -> list[Any]:
    if not isinstance(response, Mapping) or str(response.get("code")) != "0" or not isinstance(response.get("data"), list):
        return []
    return response["data"]


def _row_timestamp(row: Any, dataset: str) -> int | None:
    value = row.get("time") if dataset == "aggregated_cvd" and isinstance(row, Mapping) else (row[0] if dataset == "footprint" and isinstance(row, Sequence) and row else None)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    value = int(value)
    return value // 1000 if value >= 10_000_000_000 else value


class CvdVolumeOrderflowRawExtractor:
    def __init__(self, fetcher: CvdVolumeOrderflowFetcher, *, clock: Callable[[], Any] | None = None) -> None:
        if not callable(fetcher):
            raise ValueError("fetcher must be callable")
        self.fetcher, self.clock = fetcher, clock

    def build_fetch_plan(self, **kwargs: Any) -> list[dict[str, Any]]:
        return build_cvd_volume_orderflow_fetch_plan(**kwargs)

    def build_params(self, request: Mapping[str, Any], *, end_timestamp: int | None = None) -> dict[str, Any]:
        end = request.get("end_timestamp") if end_timestamp is None else end_timestamp
        if request["provider"] == COINGLASS_PROVIDER and request["dataset"] == "aggregated_cvd":
            return build_coinglass_aggregated_cvd_params(exchanges=request["exchanges"], symbol=request["symbol"], timeframe=request["timeframe"], limit=request["limit"],
                start_timestamp=request.get("start_timestamp"), end_timestamp=end)
        if request["provider"] == COINGLASS_PROVIDER:
            return build_coinglass_footprint_params(exchange=request["exchange"], symbol=request["symbol"], timeframe=request["timeframe"], limit=request["limit"],
                start_timestamp=request.get("start_timestamp"), end_timestamp=end)

    def execute_request(self, request: Mapping[str, Any], *, page_index: int = 1, end_timestamp: int | None = None,
                        requested_at: str | None = None) -> dict[str, Any]:
        params = self.build_params(request, end_timestamp=end_timestamp)
        request_id = f"{request['logical_request_id']}:page:{page_index:04d}"
        payload = {"request_id": request_id, "logical_request_id": request["logical_request_id"], "page_index": page_index,
            "provider": request["provider"], "dataset": request["dataset"], "market": request["market"], "endpoint_id": request["endpoint_id"],
            "path": request["path"], "params": copy.deepcopy(params), "requested_at": requested_at or _iso_utc(_clock_timestamp(self.clock))}
        try:
            response = self.fetcher(provider=request["provider"], endpoint_id=request["endpoint_id"], path=request["path"], params=copy.deepcopy(params))
            payload.update(status="ok", response=copy.deepcopy(response), error=None)
        except Exception as exc:  # provider failures are isolated by the raw contract
            payload.update(status="error", response=None, error=f"{type(exc).__name__}: {exc}")
        return payload

    def execute_paginated_request(self, request: Mapping[str, Any], *, max_pages: int | None = None,
                                  requested_at: str | None = None) -> tuple[list[dict[str, Any]], str]:
        pages, seen, cursor = [], set(), request.get("end_timestamp")
        required, collected = request.get("records_required", request["limit"]), set()
        page_cap = _positive_int(max_pages, "max_pages") if max_pages is not None else max(1, math.ceil(required / request["limit"]) + 1)
        stop = "records_required_reached"
        for index in range(1, page_cap + 1):
            page = self.execute_request(request, page_index=index, end_timestamp=cursor, requested_at=requested_at)
            pages.append(page)
            if page["status"] == "error":
                stop = "page_error"
                continue
            rows = _coinglass_rows(page["response"])
            if not rows:
                stop = "empty_page"
                break
            timestamps = [stamp for row in rows if (stamp := _row_timestamp(row, request["dataset"])) is not None]
            signature = tuple(timestamps)
            if signature in seen:
                stop = "repeated_page_signature"
                break
            seen.add(signature)
            collected.update(timestamps)
            if len(collected) >= required:
                stop = "records_required_reached"
                break
            if len(rows) < request["limit"]:
                stop = "short_page"
                break
            if not timestamps:
                stop = "pagination_cursor_not_advancing"
                break
            next_cursor = min(timestamps) - 1
            if cursor is not None and next_cursor >= cursor:
                stop = "pagination_cursor_not_advancing"
                break
            if request.get("start_timestamp") is not None and next_cursor < request["start_timestamp"]:
                stop = "start_timestamp_reached"
                break
            cursor = next_cursor
        else:
            stop = "max_pages_reached"
        return pages, stop

    def run(self, *, mode: str, reference_timestamp: int, base_asset: str = "BTC", pair_symbol: str = "BTCUSDT",
            exchanges: Sequence[str] = ("Binance", "OKX", "Bybit"), existing_input: Mapping[str, Any] | None = None,
            recovery_requests: Sequence[Mapping[str, Any]] | None = None, include_footprint: bool = True,
            footprint_exchanges: Sequence[str] = ("Binance", "OKX", "Bybit"), target_display_records: int = FINAL_DISPLAY_RECORDS,
            warmup_records: int = FINAL_WARMUP_RECORDS, incremental_limits: Mapping[str, int] | None = None,
            footprint_history_seconds: int = DEFAULT_FOOTPRINT_HISTORY_SECONDS, max_pages: int | None = None,
            data_mode: str = "synthetic", is_demo: bool = True, refresh_secondary: bool = False) -> dict[str, Any]:
        if data_mode not in {"synthetic", "live"} or type(is_demo) is not bool or (data_mode == "synthetic" and not is_demo):
            raise ValueError("invalid data_mode/is_demo combination")
        execution = _clock_timestamp(self.clock)
        requested_at = _iso_utc(execution)
        plan = self.build_fetch_plan(mode=mode, reference_timestamp=reference_timestamp, base_asset=base_asset, pair_symbol=pair_symbol,
            exchanges=copy.deepcopy(tuple(exchanges)), existing_input=existing_input, recovery_requests=recovery_requests, include_footprint=include_footprint,
            footprint_exchanges=copy.deepcopy(tuple(footprint_exchanges)), target_display_records=target_display_records, warmup_records=warmup_records,
            incremental_limits=incremental_limits, footprint_history_seconds=footprint_history_seconds,
            refresh_secondary=refresh_secondary, emulator_fixed_records=(data_mode == "synthetic"))
        physical, warnings, errors = [], [], []
        for request in plan:
            if request["provider"] == COINGLASS_PROVIDER and request["dataset"] == "aggregated_cvd":
                pages, stop = self.execute_paginated_request(request, max_pages=max_pages, requested_at=requested_at)
                for page in pages:
                    page["pagination_stop_reason"] = stop
                physical.extend(pages)
                if stop not in {"records_required_reached", "start_timestamp_reached", "short_page", "empty_page"}:
                    warnings.append(f"{request['logical_request_id']}:{stop}")
            else:
                physical.append(self.execute_request(request, requested_at=requested_at))
        errors.extend(page["error"] for page in physical if page["status"] == "error")
        quality = "invalid" if not plan else ("partial" if errors or warnings else "ok")
        return {"family": CVD_VOLUME_ORDERFLOW_FAMILY, "stage": "raw_extract", "mode": mode,
            "context": {"base_asset": base_asset, "pair_symbol": pair_symbol, "requested_exchanges": copy.deepcopy(list(exchanges)),
                "data_mode": data_mode, "is_demo": is_demo, "reference_timestamp": _timestamp(reference_timestamp, "reference_timestamp"),
                "requested_at": requested_at, "execution_timestamp": execution},
            "logical_requests": copy.deepcopy(plan), "requests": physical,
            "quality": {"status": quality, "warnings": warnings, "errors": errors}}


def extract_cvd_volume_orderflow_raw(*, fetcher: CvdVolumeOrderflowFetcher, clock: Callable[[], Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    return CvdVolumeOrderflowRawExtractor(fetcher, clock=clock).run(**kwargs)
