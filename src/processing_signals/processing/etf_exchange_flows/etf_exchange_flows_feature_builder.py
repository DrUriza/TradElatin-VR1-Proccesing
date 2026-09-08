"""Pure mathematical feature construction for ETF and exchange flows Processing."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timezone
import math
import pandas as pd
from typing import Any

from processing_signals.processing.prices_ohlcv.prices_ohlcv_processor import (
    PRICE_INDICATOR_CONFIG,
    calculate_prices_indicator_package,
)
from .etf_exchange_flows_math import ema
from .etf_exchange_flows_math import detect_cross_pairs

FAMILY                     = "etf_exchange_flows"
SUPPORTED_RANGES           = ("1d", "7d", "30d")
RANGE_SECONDS              = {"1d": 86_400, "7d": 604_800, "30d": 2_592_000}
HOURLY_STEP_SECONDS        = 3_600
DAILY_STEP_SECONDS         = 86_400
PRESSURE_WINDOW            = 86_400
MIN_COVERAGE_RATIO         = 0.80
GLASSNODE_DEFAULT_CURRENCY = "NATIVE"

REASONS = {"source_unavailable", "source_invalid", "no_observations", "missing_required_value", "insufficient_coverage",
    "timestamp_gap", "future_timestamp", "invalid_unit", "invalid_denominator", "nonfinite_result", "price_missing",
    "price_not_positive", "zero_total_flow", "anchors_not_aligned", "exchange_scope_mismatch", "secondary_unavailable",
    "issuer_identity_unavailable", "provider_unit_unconfirmed", "duplicate_input_record", "invalid_processing_input",
    "negative_flow_observation", "invalid_entity_identity"}


ENTITY_IDENTITY_FIELDS = ("exchange_name", "symbol", "provider", "endpoint_id")


def _entity_identity(record: Mapping[str, Any]) -> tuple[tuple[str, str, str, str] | None, list[str]]:
    """Return a safe balance identity without coercing provider-shaped values."""
    values: list[str] = []
    invalid_fields: list[str] = []
    for field in ENTITY_IDENTITY_FIELDS:
        value = record.get(field)
        if not isinstance(value, str) or not value.strip():
            invalid_fields.append(field)
        else:
            values.append(value.strip())
    if invalid_fields:
        return None, invalid_fields
    return (values[0], values[1], values[2], values[3]), []


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


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        result = float(value)
        return 0.0 if result == 0 else result
    return None


def _coverage(records: Sequence[Mapping[str, Any]], expected: int | None = None, step: int | None = None) -> dict[str, Any]:
    timestamps = sorted({int(item["timestamp"]) for item in records if _timestamp(item.get("timestamp")) is not None})
    gaps       = [right-left for left, right in zip(timestamps, timestamps[1:]) if step and right-left > step]
    valid      = len(records)
    return {"samples_valid": valid, "samples_expected": expected, "ratio": valid/expected if expected else None,
            "first_timestamp": timestamps[0] if timestamps else None, "last_timestamp": timestamps[-1] if timestamps else None,
            "gaps": len(gaps), "max_gap_seconds": max(gaps) if gaps else None}


def _feature(value: float | None, *, status: str, reason: str | None, timestamp: int | None, unit: str,
             provider: str, endpoint_id: str | None, coverage: Mapping[str, Any] | None = None,
             warnings: Sequence[str] = (), data_as_of: int | None = None, **extra: Any) -> dict[str, Any]:
    value = _finite(value)
    if value is None and status in {"available", "partial"}:
        status, reason = "invalid", "nonfinite_result"
    result = {"value": value, "status": status, "reason": reason, "timestamp": timestamp, "data_as_of": data_as_of if data_as_of is not None else timestamp,
              "unit": unit, "provider": provider, "endpoint_id": endpoint_id, "coverage": deepcopy(dict(coverage or _coverage([]))),
              "warnings": sorted(set(warnings))}
    result.update(deepcopy(extra))
    return result


def _missing(*, reason: str = "source_unavailable", unit: str, provider: str, endpoint_id: str | None,
             status: str = "unavailable", warnings: Sequence[str] = ()) -> dict[str, Any]:
    return _feature(None, status=status, reason=reason, timestamp=None, unit=unit, provider=provider,
                    endpoint_id=endpoint_id, warnings=warnings)


def _dedupe(records: Any, keys: Sequence[str], generated_timestamp: int) -> tuple[list[dict[str, Any]], list[str], int]:
    if not isinstance(records, list):
        return [], [], 0
    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    warnings: list[str] = []
    future_records = 0
    for record in records:
        if not isinstance(record, Mapping):
            continue
        timestamp = _timestamp(record.get("timestamp")) if "timestamp" in record else None
        if "timestamp" in record and timestamp is None:
            continue
        if timestamp is not None and timestamp > generated_timestamp:
            future_records += 1
            warnings.append("future_timestamp")
            continue
        key = tuple(record.get(name) for name in keys)
        if key in merged:
            warnings.append("duplicate_input_record")
        merged[key] = deepcopy(dict(record))
    output = list(merged.values())
    output.sort(key=lambda item: (item.get("timestamp", 0), *(str(item.get(key)) for key in keys)))
    return output, sorted(set(warnings)), future_records


def _window(records: Sequence[Mapping[str, Any]], anchor: int, seconds: int) -> list[dict[str, Any]]:
    return [deepcopy(dict(item)) for item in records if anchor-seconds < int(item["timestamp"]) <= anchor]


def _regular_sum(records: Sequence[Mapping[str, Any]], field: str, anchor: int, *, expected: int = 24,
                 future_records: int = 0, reject_negative: bool = False, expected_unit: str = "BTC") -> dict[str, Any]:
    selected = _window(records, anchor, PRESSURE_WINDOW)
    numeric = [(item, _finite(item.get(field))) for item in selected]
    incompatible_units = [item for item, value in numeric
                          if value is not None and item.get("unit", expected_unit) != expected_unit]
    rejected_negative = [item for item, value in numeric if value is not None and value < 0]
    usable = [item for item, value in numeric if value is not None and item not in incompatible_units
              and (not reject_negative or value >= 0)]
    coverage = _coverage(usable, expected, HOURLY_STEP_SECONDS)
    coverage.update(samples_received=len(selected), samples_rejected=len(selected)-len(usable),
                    invalid_observations=len(rejected_negative), future_records=future_records)
    observed_anchor = int(usable[-1]["timestamp"]) if usable else None
    common = {"requested_anchor": anchor, "observed_anchor": observed_anchor,
              "window_start": anchor-PRESSURE_WINDOW, "window_end": anchor}
    if incompatible_units:
        return _feature(None, status="invalid", reason="invalid_unit", timestamp=None, unit=expected_unit,
                        provider="cryptoquant", endpoint_id=str(incompatible_units[-1].get("endpoint_id")),
                        coverage=coverage, warnings=["invalid_unit"], data_as_of=None, **common)
    if reject_negative and rejected_negative:
        return _feature(None, status="invalid", reason="negative_flow_observation", timestamp=None, unit="BTC",
                        provider="cryptoquant", endpoint_id=str(rejected_negative[-1].get("endpoint_id")), coverage=coverage,
                        warnings=["negative_flow_observation"], data_as_of=None, **common)
    if not usable:
        status = "invalid" if future_records else "unavailable"
        reason = "future_timestamp" if future_records else "source_unavailable"
        return _feature(None, status=status, reason=reason, timestamp=None, unit="BTC", provider="cryptoquant",
                        endpoint_id=None, coverage=coverage, warnings=[reason] if future_records else [], **common)
    gap = coverage["max_gap_seconds"] is not None and coverage["max_gap_seconds"] > 2*HOURLY_STEP_SECONDS
    good = (coverage["ratio"] or 0) >= MIN_COVERAGE_RATIO and not gap
    reason = "future_timestamp" if future_records else None if good else "timestamp_gap" if gap else "insufficient_coverage"
    warnings = (["timestamp_gap"] if gap else []) + (["insufficient_coverage"] if not good and not gap else [])
    if future_records:
        warnings.append("future_timestamp")
    status = "partial" if future_records or not good else "available"
    return _feature(sum(_finite(item[field]) for item in usable), status="available" if good else "partial", reason=reason,
                    timestamp=observed_anchor, data_as_of=observed_anchor, unit="BTC", provider="cryptoquant",
                    endpoint_id=str(usable[-1].get("endpoint_id")), coverage=coverage, warnings=warnings,
                    exchange_scope=usable[-1].get("exchange_scope"), **common) | {"status": status}


def _build_etf(datasets: Mapping[str, Any], generated_timestamp: int, snapshot_anchor: int | None) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    records, warnings, future = _dedupe(datasets.get("etf_flows_daily", []), ("timestamp", "provider", "endpoint_id"), generated_timestamp)
    usable = [item for item in records if _finite(item.get("flow_usd")) is not None]
    latest = usable[-1] if usable else None
    if latest:
        latest_usd = _feature(_finite(latest["flow_usd"]), status="partial" if future else "available",
            reason="future_timestamp" if future else None, timestamp=int(latest["timestamp"]), unit="USD",
            provider="coinglass", endpoint_id=str(latest.get("endpoint_id")), coverage=_coverage([latest]), warnings=warnings)
        price = _finite(latest.get("price_usd"))
        if price is None:
            latest_btc = _missing(reason="price_missing", unit="BTC", provider="coinglass", endpoint_id=str(latest.get("endpoint_id")))
        elif price <= 0:
            latest_btc = _missing(reason="price_not_positive", unit="BTC", provider="coinglass", endpoint_id=str(latest.get("endpoint_id")), status="invalid")
        else:
            latest_btc = _feature(_finite(latest["flow_usd"])/price, status="partial" if future else "available",
                reason="future_timestamp" if future else None, timestamp=int(latest["timestamp"]), unit="BTC",
                provider="coinglass", endpoint_id=str(latest.get("endpoint_id")), coverage=_coverage([latest]), warnings=warnings)
    else:
        reason = "future_timestamp" if future else "source_unavailable"
        latest_usd = _missing(reason=reason, unit="USD", provider="coinglass", endpoint_id="bitcoin_etf_flows", status="invalid" if future else "unavailable")
        latest_btc = _missing(reason=reason, unit="BTC", provider="coinglass", endpoint_id="bitcoin_etf_flows", status="invalid" if future else "unavailable")
    period_usd: dict[str, Any] = {}
    period_btc: dict[str, Any] = {}
    anchor = int(latest["timestamp"]) if latest else None
    for range_id in SUPPORTED_RANGES:
        selected = _window(records, anchor, RANGE_SECONDS[range_id]) if anchor else []
        valid_usd = [item for item in selected if _finite(item.get("flow_usd")) is not None]
        gap = any(right["timestamp"]-left["timestamp"] > 4*DAILY_STEP_SECONDS for left, right in zip(valid_usd, valid_usd[1:]))
        status, reason = (("partial", "future_timestamp") if future else
                          ("partial", "timestamp_gap") if gap else ("available", None))
        period_usd[range_id] = (_feature(sum(_finite(item["flow_usd"]) for item in valid_usd), status=status, reason=reason,
            timestamp=anchor, unit="USD", provider="coinglass", endpoint_id="bitcoin_etf_flows", coverage=_coverage(valid_usd),
            warnings=(["timestamp_gap"] if gap else []) + (["future_timestamp"] if future else [])) if valid_usd else
            _missing(reason="future_timestamp" if future else "source_unavailable", unit="USD", provider="coinglass",
                     endpoint_id="bitcoin_etf_flows", status="invalid" if future else "unavailable"))
        convertible = [(item, _finite(item.get("price_usd"))) for item in valid_usd]
        convertible = [(item, price) for item, price in convertible if price is not None and price > 0]
        if convertible:
            incomplete = len(convertible) != len(valid_usd)
            period_btc[range_id] = _feature(sum(_finite(item["flow_usd"])/price for item, price in convertible),
                status="partial" if incomplete or gap or future else "available",
                reason="price_missing" if incomplete else "future_timestamp" if future else reason, timestamp=anchor,
                unit="BTC", provider="coinglass", endpoint_id="bitcoin_etf_flows", coverage=_coverage([item for item, _ in convertible]),
                warnings=(["price_missing"] if incomplete else []) + (["timestamp_gap"] if gap else []) +
                         (["future_timestamp"] if future else []))
        else:
            period_btc[range_id] = _missing(reason="price_missing", unit="BTC", provider="coinglass", endpoint_id="bitcoin_etf_flows")
    cumulative_usd = cumulative_btc = 0.0
    daily: list[dict[str, Any]] = []
    cumulative: list[dict[str, Any]] = []
    for item in records:
        flow, price = _finite(item.get("flow_usd")), _finite(item.get("price_usd"))
        flow_btc = flow/price if flow is not None and price is not None and price > 0 else None
        if flow is not None:
            cumulative_usd += flow
        if flow_btc is not None:
            cumulative_btc += flow_btc
        row_status = "available" if flow is not None and flow_btc is not None else "partial"
        row_warnings = [] if flow_btc is not None else ["price_missing"]
        base = {"timestamp": item["timestamp"], "flow_usd": flow, "price_usd": price, "flow_btc": flow_btc,
                "status": row_status, "warnings": row_warnings, "provider": "coinglass", "endpoint_id": item.get("endpoint_id")}
        daily.append(deepcopy(base))
        cumulative.append({**base, "cumulative_flow_usd": cumulative_usd if flow is not None else None,
                           "cumulative_flow_btc": cumulative_btc if any(row.get("flow_btc") is not None for row in daily) else None})
    # Total AUM is sourced from the contracted ``bitcoin_etf_list`` catalog in
    # _build_funds and injected by build_etf_exchange_flows_features.
    reported = _missing(reason="derived_from_etf_catalog", unit="USD", provider="coinglass", endpoint_id="bitcoin_etf_list")
    return {"net_flow_usd_latest": latest_usd, "net_flow_btc_latest": latest_btc, "period_flow_usd": period_usd,
            "period_flow_btc": period_btc, "reported_total_aum_usd": reported}, {"etf_flow_daily": daily, "etf_cumulative_flow": cumulative}, warnings



def _build_funds(datasets: Mapping[str, Any], generated_timestamp: int, snapshot_anchor: int | None) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    flows, flow_warn, flow_future = _dedupe(datasets.get("etf_fund_flows_daily", []), ("timestamp", "ticker", "provider", "endpoint_id"), generated_timestamp)
    snapshots = datasets.get("etf_funds_snapshot", []) if isinstance(datasets.get("etf_funds_snapshot", []), list) else []
    catalog: dict[str, dict[str, Any]] = {}
    warnings = list(flow_warn)
    for item in snapshots:
        if isinstance(item, Mapping) and isinstance(item.get("ticker"), str) and item["ticker"]:
            if item["ticker"] in catalog:
                warnings.append("duplicate_input_record")
            catalog[item["ticker"]] = deepcopy(dict(item))
    tickers = sorted(set(catalog) | {str(item.get("ticker")) for item in flows if item.get("ticker")})
    aums = {ticker: _finite(catalog.get(ticker, {}).get("aum_usd")) for ticker in tickers}
    valid_aum = {ticker: value for ticker, value in aums.items() if value is not None}
    total_aum = sum(valid_aum.values()) if valid_aum else None
    calculated = (_feature(total_aum, status="partial" if len(valid_aum) < len(tickers) else "available",
        reason="missing_required_value" if len(valid_aum) < len(tickers) else None, timestamp=snapshot_anchor, unit="USD", provider="calculated",
        endpoint_id="bitcoin_etf_list", coverage=_coverage([{"timestamp": snapshot_anchor}] * len(valid_aum)) if snapshot_anchor else _coverage([]),
        warnings=["missing_required_value"] if len(valid_aum) < len(tickers) else []) if valid_aum else
        _missing(unit="USD", provider="calculated", endpoint_id="bitcoin_etf_list"))
    anchors = [int(item["timestamp"]) for item in flows] or ([snapshot_anchor] if snapshot_anchor else [])
    anchor = max(anchors) if anchors else None
    period_totals: dict[str, dict[str, float]] = {}
    for range_id in SUPPORTED_RANGES:
        selected = _window(flows, anchor, RANGE_SECONDS[range_id]) if anchor else []
        totals = {ticker: sum(_finite(item["flow_usd"]) for item in selected if item.get("ticker") == ticker and _finite(item.get("flow_usd")) is not None) for ticker in tickers}
        period_totals[range_id] = totals
    output = []
    for ticker in tickers:
        aum = aums[ticker]
        periods = {}
        for range_id in SUPPORTED_RANGES:
            value = period_totals[range_id][ticker]
            gross = sum(abs(number) for number in period_totals[range_id].values())
            periods[range_id] = {"period_flow_usd": _feature(value, status="partial" if flow_future else "available",
                reason="future_timestamp" if flow_future else None, timestamp=anchor, unit="USD", provider="coinglass",
                endpoint_id="bitcoin_etf_flows", coverage=_coverage([]), warnings=flow_warn),
                "period_signed_flow_share": (_feature(value/gross, status="partial" if flow_future else "available",
                    reason="future_timestamp" if flow_future else None, timestamp=anchor, unit="ratio", provider="calculated",
                    endpoint_id=None, coverage=_coverage([]), share_basis="gross_absolute_flow", signed=True) if gross else
                    _missing(reason="invalid_denominator", unit="ratio", provider="calculated", endpoint_id=None))}
        output.append({"ticker": ticker, "fund_name": catalog.get(ticker, {}).get("fund_name"),
            "aum_usd": (_feature(aum, status="available", reason=None, timestamp=snapshot_anchor, unit="USD", provider="coinglass", endpoint_id="bitcoin_etf_list")
                        if aum is not None else _missing(reason="missing_required_value", unit="USD", provider="coinglass", endpoint_id="bitcoin_etf_list")),
            "aum_share": (_feature(aum/total_aum, status="partial" if len(valid_aum)<len(tickers) else "available", reason="missing_required_value" if len(valid_aum)<len(tickers) else None,
                timestamp=snapshot_anchor, unit="ratio", provider="calculated", endpoint_id=None) if aum is not None and total_aum else
                _missing(reason="invalid_denominator", unit="ratio", provider="calculated", endpoint_id=None)),
            "periods": periods, "issuer_flow": _missing(reason="issuer_identity_unavailable", unit="USD", provider="calculated", endpoint_id=None),
            "provider": "coinglass", "endpoint_id": "bitcoin_etf_list", "warnings": []})
    return output, calculated, sorted(set(warnings))


def _build_premium(datasets: Mapping[str, Any], generated_timestamp: int) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """Read GBTC premium/discount from the contracted ETF catalog snapshot.

    The former premium-history endpoint is not part of the frozen 33. CoinGlass
    already exposes ``asset_details.premium_discount_percent`` on
    ``bitcoin_etf_list``; using that primitive avoids an unnecessary endpoint.
    """
    snapshots = datasets.get("etf_funds_snapshot", [])
    snapshots = snapshots if isinstance(snapshots, list) else []
    gbtc = [row for row in snapshots if isinstance(row, Mapping) and str(row.get("ticker", "")).upper() == "GBTC"]
    if not gbtc:
        return (_missing(reason="source_unavailable", unit="percent", provider="coinglass", endpoint_id="bitcoin_etf_list"), [], [])
    row = gbtc[-1]
    details = row.get("asset_details", {}) if isinstance(row.get("asset_details"), Mapping) else {}
    value = _finite(details.get("premium_discount_percent"))
    if value is None:
        return (_missing(reason="premium_discount_missing", unit="percent", provider="coinglass", endpoint_id="bitcoin_etf_list"), [], [])
    anchor = _timestamp(row.get("snapshot_timestamp")) or generated_timestamp
    point = {
        "timestamp": anchor, "ticker": "GBTC", "premium_discount_percent": value,
        "provider": "coinglass", "endpoint_id": "bitcoin_etf_list",
    }
    return (_feature(value, status="available", reason=None, timestamp=anchor, unit="percent",
                     provider="coinglass", endpoint_id="bitcoin_etf_list", coverage=_coverage([point])),
            [point], [])

def _scope_records(records: list[dict[str, Any]], exchange_scope: str | None) -> tuple[list[dict[str, Any]], str | None]:
    scopes = {item.get("exchange_scope") for item in records}
    if exchange_scope is not None:
        return [item for item in records if item.get("exchange_scope") == exchange_scope], exchange_scope
    return (records, next(iter(scopes))) if len(scopes) == 1 else ([], None)


def _daily_reserve_candles(records: Sequence[Mapping[str, Any]], *, limit: int = 730) -> list[dict[str, Any]]:
    """Build closed UTC OHLC candles from one hourly aggregate reserve series."""
    days: dict[int, list[tuple[int, float]]] = {}
    for item in records:
        timestamp = _timestamp(item.get("timestamp"))
        value = _finite(item.get("reserve"))
        if timestamp is None or value is None:
            continue
        day = timestamp - timestamp % DAILY_STEP_SECONDS
        days.setdefault(day, []).append((timestamp, value))
    candles: list[dict[str, Any]] = []
    for day, observations in sorted(days.items()):
        ordered = sorted(observations)
        timestamps = {timestamp for timestamp, _ in ordered}
        expected = {day + hour * HOURLY_STEP_SECONDS for hour in range(24)}
        if timestamps != expected:
            continue
        values = [value for _, value in ordered]
        candles.append({"timestamp": day, "open": values[0], "high": max(values), "low": min(values),
                        "close": values[-1], "is_closed": True})
    return candles[-limit:]


def _exchange_balance_technical_analysis(candles: list[dict[str, Any]]) -> dict[str, Any]:
    if not candles:
        return {"status": "unavailable", "reason": "no_complete_candles", "indicators": {}}
    config = deepcopy(PRICE_INDICATOR_CONFIG)
    config["sma_periods"] = (20, 50, 100, 200)
    package = calculate_prices_indicator_package(records=candles, market_type="all_exchange", timeframe="1d", config=config)
    percent_candles = []
    previous_close = None
    for candle in candles:
        base = previous_close if previous_close not in {None, 0} else candle["close"]
        percent_candles.append({"timestamp": candle["timestamp"], "is_closed": candle["is_closed"],
            **{field: (candle[field] / base - 1) * 100 for field in ("open", "high", "low", "close")}})
        previous_close = candle["close"]
    percent_package = calculate_prices_indicator_package(records=percent_candles, market_type="all_exchange",
                                                          timeframe="1d", config=config)
    for name in ("rsi", "tsi", "stochastic"):
        package[name] = percent_package[name]
    tsi_values = package["tsi"]["series"]["tsi"]
    tsi_signal = [None if pd.isna(value) else float(value)
                  for value in ema(pd.Series(tsi_values, dtype="float64"), span=13)]
    package["tsi"]["series"]["signal"] = tsi_signal
    package["tsi"]["current"]["signal"] = next((value for value in reversed(tsi_signal) if value is not None), None)
    bollinger = package["bollinger_bands"]["series"]
    widths = []
    for upper, middle, lower in zip(bollinger["upper"], bollinger["middle"], bollinger["lower"]):
        widths.append(None if upper is None or middle in {None, 0} or lower is None else (upper - lower) / middle * 100)
    package["bollinger_band_width"] = {
        "indicator_id": "bollinger_band_width", "parameters": {"period": 20, "standard_deviations": 2.0},
        "timestamps": [item["timestamp"] for item in candles], "series": {"bollinger_band_width": widths},
        "current": {"bollinger_band_width": next((value for value in reversed(widths) if value is not None), None)},
        "warmup_records": 20, "source": {"market_type": "all_exchange", "timeframe": "1d", "is_synthetic_source": False},
        "quality": {"status": "ok", "valid_points": sum(value is not None for value in widths),
                    "null_points": sum(value is None for value in widths), "required_records": 20,
                    "available_records": len(candles), "warnings": []},
        "calculation": {"module": "processing.etf_exchange_flows", "function": "bollinger_band_width",
                        "parameters": {"period": 20, "standard_deviations": 2.0}, "records": len(candles),
                        "last_valid_timestamp": candles[-1]["timestamp"]},
    }
    timestamps = [item["timestamp"] for item in candles]
    closes = [float(item["close"]) for item in candles]
    regression = {"upper": [], "middle": [], "lower": []}
    period = 100
    for index in range(len(closes)):
        if index + 1 < period:
            for values in regression.values():
                values.append(None)
            continue
        window = closes[index + 1 - period:index + 1]
        x_mean = (period - 1) / 2
        y_mean = sum(window) / period
        denominator = sum((x - x_mean) ** 2 for x in range(period))
        slope = sum((x - x_mean) * (value - y_mean) for x, value in enumerate(window)) / denominator
        intercept = y_mean - slope * x_mean
        fitted = [intercept + slope * x for x in range(period)]
        residual_std = math.sqrt(sum((value - fit) ** 2 for value, fit in zip(window, fitted)) / period)
        middle = fitted[-1]
        regression["middle"].append(middle)
        regression["upper"].append(middle + 2 * residual_std)
        regression["lower"].append(middle - 2 * residual_std)
    wasserstein = []
    distribution_window = 30
    for index in range(len(closes)):
        if index + 1 < distribution_window * 2:
            wasserstein.append(None)
            continue
        previous = sorted(closes[index + 1 - 2 * distribution_window:index + 1 - distribution_window])
        current = sorted(closes[index + 1 - distribution_window:index + 1])
        scale = max(abs(sum(previous) / distribution_window), 1e-12)
        wasserstein.append(sum(abs(left - right) for left, right in zip(previous, current)) / distribution_window / scale)
    package["wasserstein_distance"] = {"indicator_id": "wasserstein_distance", "parameters": {"window": distribution_window},
        "timestamps": timestamps, "series": {"wasserstein_distance": wasserstein},
        "current": {"wasserstein_distance": next((value for value in reversed(wasserstein) if value is not None), None)},
        "warmup_records": distribution_window * 2, "source": {"market_type": "all_exchange", "timeframe": "1d",
            "is_synthetic_source": False}, "quality": {"status": "ok", "valid_points": sum(v is not None for v in wasserstein),
            "null_points": sum(v is None for v in wasserstein), "required_records": distribution_window * 2,
            "available_records": len(candles), "warnings": []}, "calculation": {"module": "processing.etf_exchange_flows",
            "function": "rolling_wasserstein_distance", "parameters": {"window": distribution_window},
            "records": len(candles), "last_valid_timestamp": timestamps[-1]}}
    cross_series = dict(package["moving_averages"]["series"])
    for name in ("macd", "adx", "stochastic"):
        cross_series.update(package[name]["series"])
    crosses = detect_cross_pairs(timestamps=timestamps, series=cross_series, pairs=(
        ("ema_9", "ema_21"), ("ema_21", "ema_50"), ("sma_20", "sma_50"),
        ("sma_50", "sma_100"), ("sma_100", "sma_200"), ("wma_20", "wma_50"),
        ("macd", "signal"), ("di_plus", "di_minus"), ("k", "d")))
    return {"status": "available", "reason": None, "source_path": "series.exchange_balance",
            "calculation_history_records": len(candles), "indicators": package,
            "regression_channel": {"timestamps": timestamps, "series": regression,
                "parameters": {"period": period, "standard_deviations": 2.0}}, "cross_candidates": crosses}


def _derive_exchange_netflow_series(
    inflow_rows: Sequence[Mapping[str, Any]],
    outflow_rows: Sequence[Mapping[str, Any]],
    *,
    window: str,
    exchange_scope: str | None,
) -> list[dict[str, Any]]:
    """Derive Net Flow from the two contracted CryptoQuant primitives.

    ``exchange_netflow`` is intentionally not a logical endpoint in the frozen
    33-endpoint inventory.  Processing therefore owns the exact identity
    ``netflow = inflow - outflow`` and publishes it as a calculated series.
    """
    incoming = {
        int(row["timestamp"]): _finite(row.get("inflow_total"))
        for row in inflow_rows
        if isinstance(row, Mapping) and type(row.get("timestamp")) is int
    }
    outgoing = {
        int(row["timestamp"]): _finite(row.get("outflow_total"))
        for row in outflow_rows
        if isinstance(row, Mapping) and type(row.get("timestamp")) is int
    }
    rows: list[dict[str, Any]] = []
    for timestamp in sorted(set(incoming) & set(outgoing)):
        left, right = incoming[timestamp], outgoing[timestamp]
        if left is None or right is None:
            continue
        rows.append({
            "timestamp": timestamp,
            "window": window,
            "exchange_scope": exchange_scope,
            "netflow_total": float(left) - float(right),
            "provider": "calculated",
            "endpoint_id": None,
            "source_endpoints": ["exchange_inflow", "exchange_outflow"],
            "calculation": "inflow_total-outflow_total",
        })
    return rows


def _build_exchange(datasets: Mapping[str, Any], generated_timestamp: int, exchange_scope: str | None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[str]]:
    warnings: list[str] = []
    prepared: dict[str, dict[str, list[dict[str, Any]]]] = {}
    future_counts: dict[str, dict[str, int]] = {}
    series: dict[str, Any] = {"inflow": {}, "outflow": {}, "netflow": {}, "reserve": {}}

    # Only the three contracted CryptoQuant primitives are acquired.
    for endpoint in ("exchange_inflow", "exchange_outflow", "exchange_reserve"):
        prepared[endpoint] = {}
        future_counts[endpoint] = {}
        series_name = endpoint.removeprefix("exchange_")
        source = datasets.get(endpoint, {}) if isinstance(datasets.get(endpoint), Mapping) else {}
        for window in ("hour", "day"):
            records, duplicate, future_count = _dedupe(
                source.get(window, []),
                ("timestamp", "window", "exchange_scope", "provider", "endpoint_id"),
                generated_timestamp,
            )
            records, _ = _scope_records(records, exchange_scope)
            prepared[endpoint][window] = records
            future_counts[endpoint][window] = future_count
            series[series_name][window] = deepcopy(records)
            warnings.extend(duplicate)

    for window in ("hour", "day"):
        series["netflow"][window] = _derive_exchange_netflow_series(
            prepared["exchange_inflow"][window],
            prepared["exchange_outflow"][window],
            window=window,
            exchange_scope=exchange_scope,
        )

    inflow, in_scope = _scope_records(prepared["exchange_inflow"]["hour"], exchange_scope)
    outflow, out_scope = _scope_records(prepared["exchange_outflow"]["hour"], exchange_scope)
    mismatch = bool(inflow or outflow) and in_scope != out_scope
    common_anchor = (
        min(int(inflow[-1]["timestamp"]), int(outflow[-1]["timestamp"]))
        if inflow and outflow and not mismatch
        else None
    )

    if mismatch:
        inflow_feature = _missing(reason="exchange_scope_mismatch", unit="BTC", provider="cryptoquant", endpoint_id="exchange_inflow")
        outflow_feature = _missing(reason="exchange_scope_mismatch", unit="BTC", provider="cryptoquant", endpoint_id="exchange_outflow")
    elif common_anchor is None:
        inflow_feature = _missing(unit="BTC", provider="cryptoquant", endpoint_id="exchange_inflow")
        outflow_feature = _missing(unit="BTC", provider="cryptoquant", endpoint_id="exchange_outflow")
    else:
        inflow_feature = _regular_sum(
            inflow, "inflow_total", common_anchor,
            future_records=future_counts["exchange_inflow"]["hour"], reject_negative=True,
        )
        outflow_feature = _regular_sum(
            outflow, "outflow_total", common_anchor,
            future_records=future_counts["exchange_outflow"]["hour"], reject_negative=True,
        )

    usable_components = all(
        item["status"] in {"available", "partial"} and item["value"] is not None
        for item in (inflow_feature, outflow_feature)
    )
    invalid_component = any(item["status"] == "invalid" for item in (inflow_feature, outflow_feature))
    if usable_components:
        calc_status = "available" if inflow_feature["status"] == outflow_feature["status"] == "available" else "partial"
        calc_reason = None if calc_status == "available" else "insufficient_coverage"
        net_calculated = _feature(
            inflow_feature["value"] - outflow_feature["value"],
            status=calc_status, reason=calc_reason, timestamp=common_anchor,
            data_as_of=min(inflow_feature["data_as_of"], outflow_feature["data_as_of"]),
            unit="BTC", provider="calculated", endpoint_id=None,
            coverage=inflow_feature["coverage"], exchange_scope=in_scope,
            source_endpoints=["exchange_inflow", "exchange_outflow"],
            calculation="inflow_24h-outflow_24h",
        )
        denominator = inflow_feature["value"] + outflow_feature["value"]
        if denominator == 0:
            pressure = _missing(reason="zero_total_flow", unit="ratio", provider="calculated", endpoint_id=None)
        else:
            pressure_value = (inflow_feature["value"] - outflow_feature["value"]) / denominator
            pressure = _feature(
                pressure_value, status=calc_status, reason=calc_reason, timestamp=common_anchor,
                data_as_of=min(inflow_feature["data_as_of"], outflow_feature["data_as_of"]),
                unit="ratio", provider="calculated", endpoint_id=None,
                coverage=inflow_feature["coverage"], exchange_scope=in_scope,
                window_start=common_anchor-PRESSURE_WINDOW, window_end=common_anchor,
                source_endpoints=["exchange_inflow", "exchange_outflow"],
            ) if -1 <= pressure_value <= 1 else _missing(
                reason="nonfinite_result", unit="ratio", provider="calculated", endpoint_id=None, status="invalid"
            )
    else:
        reason = "exchange_scope_mismatch" if mismatch else "source_invalid" if invalid_component else "source_unavailable"
        net_calculated = _missing(reason=reason, unit="BTC", provider="calculated", endpoint_id=None)
        pressure = _missing(reason=reason, unit="ratio", provider="calculated", endpoint_id=None)

    # Kept only as an explicit provenance slot: no fourth CryptoQuant endpoint
    # is required or queried for Net Flow.
    net_reported = _missing(
        reason="derived_from_contracted_primitives", unit="BTC", provider="calculated", endpoint_id=None
    )
    net_reconciliation = {
        "reported": deepcopy(net_reported),
        "calculated": deepcopy(net_calculated),
        "difference": _missing(
            reason="no_independent_netflow_endpoint", unit="BTC", provider="calculated", endpoint_id=None
        ),
        "calculated_anchor": common_anchor,
        "reported_anchor": None,
        "timestamp_distance": None,
        "window_seconds": PRESSURE_WINDOW,
        "scope": in_scope,
        "alignment_required": "not_applicable_derived_identity",
    }

    reserve_records = prepared["exchange_reserve"]["hour"] or prepared["exchange_reserve"]["day"]
    reserve_records, reserve_scope = _scope_records(reserve_records, exchange_scope)
    reserve_valid = [item for item in reserve_records if _finite(item.get("reserve")) is not None]
    reserve_future = future_counts["exchange_reserve"]["hour"] + future_counts["exchange_reserve"]["day"]
    reserve = (
        _feature(
            _finite(reserve_valid[-1]["reserve"]),
            status="partial" if reserve_future else "available",
            reason="future_timestamp" if reserve_future else None,
            timestamp=int(reserve_valid[-1]["timestamp"]), unit="BTC",
            provider="cryptoquant", endpoint_id="exchange_reserve", coverage=_coverage([reserve_valid[-1]]),
            exchange_scope=reserve_scope, warnings=["future_timestamp"] if reserve_future else [],
        )
        if reserve_valid else
        _missing(
            reason="future_timestamp" if reserve_future else "source_unavailable", unit="BTC",
            provider="cryptoquant", endpoint_id="exchange_reserve",
            status="invalid" if reserve_future else "unavailable",
            warnings=["future_timestamp"] if reserve_future else [],
        )
    )
    for feature in (inflow_feature, outflow_feature, net_calculated, pressure, reserve):
        warnings.extend(feature.get("warnings", []))
    reserve_candles = _daily_reserve_candles(prepared["exchange_reserve"]["hour"])
    return (
        {
            "inflow_24h": inflow_feature,
            "outflow_24h": outflow_feature,
            "netflow_24h_reported": net_reported,
            "netflow_24h_calculated": net_calculated,
            "cryptoquant_reserve": reserve,
        },
        {"flow_24h": pressure},
        {"netflow": net_reconciliation, "series": series, "reserve_daily_candles": reserve_candles},
        sorted(set(warnings)),
    )

def _build_balances(datasets: Mapping[str, Any], generated_timestamp: int, exchange_scope: str | None,
                    reserve: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]],
                                                         dict[str, Any], dict[str, Any], list[str]]:
    """Expose the contracted CryptoQuant reserve without legacy confirmations.

    Exchange balance/list/chart and Glassnode balance confirmations were removed
    from the frozen endpoint inventory.  Reserve is the single source of truth
    for the exchange inventory view; the HMI consumes it directly.
    """
    del datasets, generated_timestamp, exchange_scope
    history_metadata = {
        "status": "not_applicable", "reason": "reserve_series_is_primary_history",
        "warnings": [], "records_available": 0, "future_records_excluded": 0,
        "future_records_by_entity": [], "first_timestamp": None, "last_timestamp": None,
    }
    return ({"cryptoquant_reserve": deepcopy(reserve)}, [], [], history_metadata, {}, [])

def build_etf_exchange_flows_features(*, input_contract: Mapping[str, Any], generated_at: Any = None,
                                      exchange_scope: str | None = None) -> dict[str, Any]:
    if not isinstance(input_contract, Mapping) or input_contract.get("family") != FAMILY or input_contract.get("stage") != "input":
        raise ValueError("invalid_processing_input")
    generated_timestamp = _timestamp(generated_at if generated_at is not None else input_contract.get("generated_at"))
    if generated_timestamp is None:
        raise ValueError("invalid_processing_input")
    datasets = input_contract.get("datasets")
    if not isinstance(datasets, Mapping):
        raise ValueError("invalid_processing_input")
    snapshot_anchor = _timestamp(input_contract.get("data_as_of"))
    etf, etf_series, warnings = _build_etf(datasets, generated_timestamp, snapshot_anchor)
    funds, calculated_aum, fund_warn = _build_funds(datasets, generated_timestamp, snapshot_anchor)
    etf["calculated_fund_aum_usd"] = calculated_aum
    if etf.get("reported_total_aum_usd", {}).get("value") is None and calculated_aum.get("value") is not None:
        etf["reported_total_aum_usd"] = deepcopy(calculated_aum)
        etf["reported_total_aum_usd"].update(provider="derived_from_coinglass_etf_list", endpoint_id="bitcoin_etf_list", reason=None)
    premium, premium_series, premium_warn = _build_premium(datasets, generated_timestamp)
    exchange, pressure, exchange_payload, exchange_warn = _build_exchange(datasets, generated_timestamp, exchange_scope)
    balances, exchanges, balance_series, balance_metadata, balance_spread, balance_warn = _build_balances(
        datasets, generated_timestamp, exchange_scope, exchange["cryptoquant_reserve"])
    reported, calculated = etf["reported_total_aum_usd"], etf["calculated_fund_aum_usd"]
    if reported["value"] is not None and calculated["value"] is not None:
        difference = calculated["value"]-reported["value"]
        difference_usd = _feature(difference, status="available", reason=None, timestamp=min(reported["data_as_of"], calculated["data_as_of"]),
                                  unit="USD", provider="calculated", endpoint_id=None)
        difference_percent = (_feature(difference/reported["value"]*100, status="available", reason=None,
            timestamp=difference_usd["timestamp"], unit="percent", provider="calculated", endpoint_id=None) if reported["value"] != 0 else
            _missing(reason="invalid_denominator", unit="percent", provider="calculated", endpoint_id=None, status="invalid"))
    else:
        difference_usd = _missing(unit="USD", provider="calculated", endpoint_id=None)
        difference_percent = _missing(unit="percent", provider="calculated", endpoint_id=None)
    reconciliation = {
        "aum": {"reported": deepcopy(reported), "calculated": deepcopy(calculated),
                "difference_usd": difference_usd, "difference_percent": difference_percent},
        "netflow": exchange_payload["netflow"],
    }
    all_warnings = sorted(set(warnings + fund_warn + premium_warn + exchange_warn + balance_warn))
    balance_candles = exchange_payload["reserve_daily_candles"]
    return {"features": {"etf": etf, "exchange_flows": {key: value for key, value in exchange.items() if key != "cryptoquant_reserve"},
            "exchange_balances": balances, "premium_discount": {"gbtc_latest": premium}, "pressure": pressure,
            "provider_reconciliation": reconciliation},
        "series": {**etf_series, "fund_premium_discount": premium_series,
            "exchange_inflow": exchange_payload["series"]["inflow"], "exchange_outflow": exchange_payload["series"]["outflow"],
            "exchange_netflow": exchange_payload["series"]["netflow"], "exchange_reserve": exchange_payload["series"]["reserve"],
            "exchange_balance": balance_candles,
            "exchange_balance_source_points": balance_series}, "series_metadata": {"exchange_balance": balance_metadata,
                "exchange_balance_candles": {"construction_stage": "processing",
                    "construction_rule": "UTC hourly first/max/min/last", "source_provider": "cryptoquant",
                    "source_endpoint_id": "exchange_reserve", "exchange_scope": exchange_scope,
                    "records_available": len(balance_candles)}},
        "technical_analysis": _exchange_balance_technical_analysis(balance_candles),
        "snapshots": {"funds": funds, "exchanges": exchanges}, "warnings": all_warnings,
        "generated_timestamp": generated_timestamp}


class EtfExchangeFlowsFeatureBuilder:
    def build(self, *, input_contract: Mapping[str, Any], generated_at: Any = None, exchange_scope: str | None = None) -> dict[str, Any]:
        return build_etf_exchange_flows_features(input_contract=input_contract, generated_at=generated_at, exchange_scope=exchange_scope)
