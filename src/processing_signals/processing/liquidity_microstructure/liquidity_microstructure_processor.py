"""Liquidity Microstructure Processing v0.1."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import json
import math
import time
from typing import Any

from .liquidity_microstructure_math import depth_metrics, derive_cumulative_band, process_order_book_levels
from .liquidity_microstructure_math import (
    absolute_change,
    clean_zero,
    observation_at_or_before,
    rolling_mean,
    rolling_std,
    rolling_z_score,
    safe_percent_change,
)
from .liquidity_microstructure_math import aggregate_trade_window, enrich_trade_event
from .liquidity_microstructure_math import (
    rolling_zscore as native_rolling_zscore,
    rolling_wasserstein,
    difference as native_difference,
    latest as native_latest,
)
from .liquidity_microstructure_feature_builder import build_liquidity_microstructure_features

PROCESSING_VERSION                  = "0.1"
MARKETS                             = ("spot", "perpetual")
TIMEFRAMES                          = ("1m", "5m", "15m", "4h")
REQUIRED_TIMEFRAMES                 = ("1m", "4h")
DEPTH_RANGES_PERCENT                = (10,)
SUPPORTED_DEPTH_RANGES_PERCENT      = (1, 5, 10)
REFERENCE_DEPTH_RANGE_PERCENT       = 10
MARKET_IMPACT_QUANTITY_BASE         = 1.0
LARGE_TRADE_WINDOWS_SECONDS         = {"1m": 60, "5m": 300, "15m": 900, "4h": 14400, "24h": 86400}
WHALE_ROLLING_LOOKBACK              = 20
MARKET_HISTORY_WINDOWS_DAYS         = (1, 7, 30)
DATASET_STATUSES                    = {"available", "partial", "unavailable", "invalid"}
INPUT_QUALITY_STATUSES              = {"ok", "partial", "invalid"}


def _number(value: Any, field: str, *, positive: bool = False, nonnegative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"invalid_numeric:{field}")
    if (positive and value <= 0) or (nonnegative and value < 0):
        raise ValueError(f"invalid_numeric_range:{field}")
    return float(value)


def _timestamp(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("invalid_timestamp")
    return value


def _validate_dataset(dataset: Any, *, events: bool = False, kind: str) -> None:
    if not isinstance(dataset, Mapping) or dataset.get("status") not in DATASET_STATUSES or "reason" not in dataset:
        raise ValueError(f"invalid_dataset:{kind}")
    rows = dataset.get("events" if events else "records")
    if not isinstance(rows, list):
        raise ValueError(f"invalid_dataset_rows:{kind}")
    identities: dict[Any, str] = {}
    previous = 0
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError(f"invalid_record:{kind}")
        timestamp = _timestamp(row.get("timestamp"))
        if timestamp < previous:
            raise ValueError(f"records_not_chronological:{kind}")
        previous = timestamp
        identity = row.get("event_id") if events else (timestamp, row.get("timeframe"), row.get("range_percent"))
        fingerprint = json.dumps(row, sort_keys=True, allow_nan=False)
        if identity in identities and identities[identity] != fingerprint:
            raise ValueError(f"incompatible_duplicate:{kind}")
        identities[identity] = fingerprint
        if events:
            if not isinstance(row.get("event_id"), str) or not row["event_id"] or row.get("side") not in {"buy", "sell"}:
                raise ValueError("invalid_trade_event")
            _number(row.get("price"), "price", positive=True)
            if "volume_usd" in row:
                _number(row.get("volume_usd"), "volume_usd", nonnegative=True)
            else:
                _number(row.get("quantity_base"), "quantity_base", nonnegative=True)
                _number(row.get("notional_quote"), "notional_quote", nonnegative=True)
        elif kind == "orderbook":
            if row.get("market_type") not in MARKETS or row.get("timeframe") not in TIMEFRAMES:
                raise ValueError("invalid_orderbook_dimensions")
            for side in ("bid_levels", "ask_levels"):
                for level in row.get(side, []):
                    _number(level.get("price"), "price", positive=True)
                    _number(level.get("quantity"), "quantity", nonnegative=True)
        elif kind == "order_depth":
            if row.get("market_type") not in MARKETS or row.get("timeframe") not in TIMEFRAMES or row.get("range_percent") not in SUPPORTED_DEPTH_RANGES_PERCENT:
                raise ValueError("invalid_depth_dimensions")
            for field in ("bids_usd", "asks_usd", "bids_quantity", "asks_quantity"):
                _number(row.get(field), field, nonnegative=True)
        elif kind == "whale":
            if row.get("timeframe") not in TIMEFRAMES:
                raise ValueError("invalid_whale_timeframe")
            _number(row.get("whale_index_value"), "whale_index_value")
        elif kind == "market":
            _number(row.get("price"), "price", positive=True)
            _number(row.get("market_cap"), "market_cap", nonnegative=True)
            _number(row.get("circulating_supply"), "circulating_supply", nonnegative=True)


def validate_liquidity_microstructure_input(input_contract: Mapping[str, Any]) -> None:
    if not isinstance(input_contract, Mapping) or input_contract.get("family") != "liquidity_microstructure":
        raise ValueError("invalid_input_family")
    if input_contract.get("stage") != "input" or input_contract.get("mode") not in {"bootstrap", "incremental", "recovery"}:
        raise ValueError("invalid_input_stage_or_mode")
    _timestamp(input_contract.get("reference_timestamp"))
    _timestamp(input_contract.get("execution_timestamp"))
    if not isinstance(input_contract.get("context"), Mapping):
        raise ValueError("invalid_input_context")
    provider = input_contract.get("providers", {}).get("coinglass") if isinstance(input_contract.get("providers"), Mapping) else None
    quality = input_contract.get("quality")
    if not isinstance(provider, Mapping) or not isinstance(quality, Mapping) or quality.get("status") not in INPUT_QUALITY_STATUSES:
        raise ValueError("invalid_input_provider_or_quality")
    for market in MARKETS:
        _validate_dataset(provider["orderbook"][market], kind="orderbook")
        _validate_dataset(provider["order_depth"][market], kind="order_depth")
        _validate_dataset(provider["large_trades"][market], events=True, kind="trades")
        if "whale_orders" in provider:
            _validate_dataset(provider["whale_orders"][market], events=True, kind="trades")
    _validate_dataset(provider["whale_activity"], kind="whale")
    _validate_dataset(provider["market_history"], kind="market")
    json.dumps(input_contract, ensure_ascii=False, allow_nan=False)


def _provenance(path: str, dataset: Mapping[str, Any], timestamp: int | None, method: str, units: str, parameters: Mapping[str, Any]) -> dict[str, Any]:
    return {"source_dataset": path, "source_status": dataset["status"], "source_timestamp": timestamp,
            "parameters": dict(parameters), "calculation_method": method, "units": units}


def _orderbooks(dataset: Mapping[str, Any], market: str, impact_quantity: float) -> dict[str, Any]:
    path = f"providers.coinglass.orderbook.{market}"
    timeframes = {}
    for timeframe in TIMEFRAMES:
        limit = 730 if timeframe == "4h" else 240 if timeframe == "1m" else 0
        source = ([record for record in dataset["records"] if record.get("timeframe") == timeframe][-limit:]
                  if limit else [])
        history = []
        for record in source:
            base = {key: record.get(key) for key in ("timestamp", "market_type", "exchange", "symbol", "timeframe")}
            if "bid_levels" not in record or "ask_levels" not in record:
                calculated = {"status": "unavailable", "reason": "orderbook_side_mapping_unverified"}
            else:
                calculated = process_order_book_levels(record["bid_levels"], record["ask_levels"], impact_quantity=impact_quantity)
            history.append({**base, **calculated,
                            "provenance": _provenance(path, dataset, record["timestamp"], "verified_orderbook_levels", "base_and_quote", {"impact_quantity_base": impact_quantity})})
        valid = [record for record in history if record["status"] == "available"]
        status = "available" if valid else ("invalid" if any(record["status"] == "invalid" for record in history) else "unavailable")
        reason = None if valid else (history[-1]["reason"] if history else dataset.get("reason") or "no_usable_orderbook_snapshots")
        timeframes[timeframe] = {"status": status, "reason": reason, "history": history,
                                 "current": max(valid, key=lambda record: record["timestamp"]) if valid else None,
                                 "metadata": {"records_available": len(history), "first_timestamp": min((r["timestamp"] for r in history), default=None),
                                              "last_timestamp": max((r["timestamp"] for r in history), default=None),
                                              "current_timestamp": max((r["timestamp"] for r in valid), default=None),
                                              "source_status": dataset["status"], "history_truncated": False}}
    required_nodes = [timeframes[timeframe] for timeframe in REQUIRED_TIMEFRAMES]
    aggregate = "invalid" if any(value["status"] == "invalid" for value in required_nodes) else (
        "available" if all(value["status"] == "available" for value in required_nodes) else "partial")
    return {"status": aggregate, "reason": None if aggregate == "available" else "required_1m_or_4h_unavailable", "timeframes": timeframes}


def _direct_depth(record: Mapping[str, Any], dataset: Mapping[str, Any], path: str) -> dict[str, Any]:
    base = depth_metrics(float(record["bids_quantity"]), float(record["asks_quantity"]))
    quote = depth_metrics(float(record["bids_usd"]), float(record["asks_usd"]))
    return {**deepcopy(dict(record)), "status": "available" if base["status"] == quote["status"] == "available" else "unavailable",
            "reason": None if base["status"] == quote["status"] == "available" else "zero_total_depth",
            "source_type": "provider_aggregated_depth", "base_quantity": base, "quote_notional": quote,
            "source_fields": ["bids_usd", "asks_usd", "bids_quantity", "asks_quantity"],
            "provenance": _provenance(path, dataset, record["timestamp"], "provider_cumulative_depth", "base_and_quote", {"range_percent": record["range_percent"]})}


def _depth(dataset: Mapping[str, Any], market: str) -> dict[str, Any]:
    path = f"providers.coinglass.order_depth.{market}"
    timeframes = {}
    for timeframe in TIMEFRAMES:
        source = [record for record in dataset["records"] if record["timeframe"] == timeframe]
        if timeframe == "4h":
            source = source[-730:]
        elif timeframe == "1m":
            source = source[-240:]
        else:
            source = []
        direct = [_direct_depth(record, dataset, path) for record in source]
        by_key = {(record["timestamp"], record["range_percent"]): record for record in source}
        derived = []
        def derived_with_metrics(lower: Mapping[str, Any], upper: Mapping[str, Any], name: str) -> dict[str, Any]:
            band = derive_cumulative_band(lower, upper, name=name)
            if band["status"] == "available":
                band["base_quantity"] = depth_metrics(band["bids_quantity"], band["asks_quantity"])
                band["quote_notional"] = depth_metrics(band["bids_usd"], band["asks_usd"])
            return band
        for timestamp in sorted({record["timestamp"] for record in source}):
            if (timestamp, 1) in by_key and (timestamp, 5) in by_key:
                derived.append({"timestamp": timestamp, **derived_with_metrics(by_key[(timestamp, 1)], by_key[(timestamp, 5)], "one_to_five")})
            if (timestamp, 5) in by_key and (timestamp, 10) in by_key:
                derived.append({"timestamp": timestamp, **derived_with_metrics(by_key[(timestamp, 5)], by_key[(timestamp, 10)], "five_to_ten")})
        status = "invalid" if any(item["status"] == "invalid" for item in derived) else ("available" if direct else "unavailable")
        timeframes[timeframe] = {"status": status, "reason": "non_monotonic_cumulative_depth" if status == "invalid" else (None if direct else dataset.get("reason")),
                                 "direct_ranges": direct, "derived_bands": derived}
    required_nodes = [timeframes[timeframe] for timeframe in REQUIRED_TIMEFRAMES]
    aggregate = "invalid" if any(value["status"] == "invalid" for value in required_nodes) else (
        "available" if all(value["status"] == "available" for value in required_nodes) else "partial")
    return {"status": aggregate, "reason": None if aggregate == "available" else "required_1m_or_4h_unavailable", "timeframes": timeframes}


def _rebuild_derived_depth_bands(timeframe_node: dict[str, Any]) -> None:
    direct = timeframe_node["direct_ranges"]
    by_key = {(record["timestamp"], record["range_percent"]): record for record in direct}
    derived = []
    for timestamp in sorted({record["timestamp"] for record in direct}):
        for lower_range, upper_range, name in ((1, 5, "one_to_five"), (5, 10, "five_to_ten")):
            if (timestamp, lower_range) not in by_key or (timestamp, upper_range) not in by_key:
                continue
            band = derive_cumulative_band(by_key[(timestamp, lower_range)], by_key[(timestamp, upper_range)], name=name)
            if band["status"] == "available":
                band["base_quantity"] = depth_metrics(band["bids_quantity"], band["asks_quantity"])
                band["quote_notional"] = depth_metrics(band["bids_usd"], band["asks_usd"])
            derived.append({"timestamp": timestamp, **band})
    timeframe_node["derived_bands"] = derived
    timeframe_node["status"] = "invalid" if any(item["status"] == "invalid" for item in derived) else (
        "partial" if any(item.get("preserved_from_previous") for item in direct) else ("available" if direct else "unavailable"))
    timeframe_node["reason"] = ("non_monotonic_cumulative_depth" if timeframe_node["status"] == "invalid" else
                                "update_failed_previous_processing_preserved" if timeframe_node["status"] == "partial" else
                                None if direct else "no_usable_depth_records")


def _mark_preserved(node: Mapping[str, Any], *, path: str, previous_execution: int | None) -> dict[str, Any]:
    preserved = deepcopy(dict(node))
    source_timestamp = (preserved.get("current_timestamp") or preserved.get("timestamp") or
                        preserved.get("metadata", {}).get("current_timestamp"))
    preserved.update({"status": "partial", "reason": "update_failed_previous_processing_preserved", "preserved_from_previous": True,
                      "preserved_source_timestamp": source_timestamp, "preserved_execution_timestamp": previous_execution,
                      "preserved_feature_path": path})
    return preserved


def _preserve_granular(*, provider: Mapping[str, Any], markets: dict[str, Any], whale: dict[str, Any], history: dict[str, Any],
                       previous: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    previous_execution = previous.get("execution_timestamp")
    for market in MARKETS:
        orderbook_source = provider["orderbook"][market]
        if orderbook_source["status"] in {"partial", "unavailable"}:
            present_timeframes = {record["timeframe"] for record in orderbook_source["records"] if "bid_levels" in record and "ask_levels" in record}
            for timeframe in TIMEFRAMES:
                current_node = markets[market]["orderbook"]["timeframes"][timeframe]
                previous_node = previous["markets"][market]["orderbook"]["timeframes"][timeframe]
                if timeframe not in present_timeframes and current_node["status"] != "invalid" and previous_node["status"] != "invalid":
                    path = f"markets.{market}.orderbook.timeframes.{timeframe}"
                    markets[market]["orderbook"]["timeframes"][timeframe] = _mark_preserved(previous_node, path=path,
                                                                                            previous_execution=previous_execution)
        depth_source = provider["order_depth"][market]
        if depth_source["status"] in {"partial", "unavailable"}:
            for timeframe in TIMEFRAMES:
                current_node = markets[market]["order_depth"]["timeframes"][timeframe]
                previous_node = previous["markets"][market]["order_depth"]["timeframes"][timeframe]
                present_ranges = {record["range_percent"] for record in depth_source["records"] if record["timeframe"] == timeframe}
                for range_percent in DEPTH_RANGES_PERCENT:
                    if range_percent in present_ranges:
                        continue
                    previous_rows = [row for row in previous_node["direct_ranges"] if row["range_percent"] == range_percent and row["status"] != "invalid"]
                    for row in previous_rows:
                        path = f"markets.{market}.order_depth.timeframes.{timeframe}.direct_ranges.{range_percent}"
                        current_node["direct_ranges"].append(_mark_preserved(row, path=path, previous_execution=previous_execution))
                current_node["direct_ranges"].sort(key=lambda row: (row["timestamp"], row["range_percent"]))
                _rebuild_derived_depth_bands(current_node)
        trades_source = provider["large_trades"][market]
        if trades_source["status"] in {"partial", "unavailable"} and not trades_source["events"]:
            previous_node = previous["markets"][market]["large_trades"]
            if previous_node["status"] != "invalid":
                path = f"markets.{market}.large_trades"
                markets[market]["large_trades"] = _mark_preserved(previous_node, path=path, previous_execution=previous_execution)
    whale_source = provider["whale_activity"]
    if whale_source["status"] in {"partial", "unavailable"}:
        present_timeframes = {record["timeframe"] for record in whale_source["records"]}
        for timeframe in TIMEFRAMES:
            previous_node = previous["whale_activity"]["timeframes"][timeframe]
            if timeframe not in present_timeframes and previous_node["status"] != "invalid":
                path = f"whale_activity.timeframes.{timeframe}"
                whale["timeframes"][timeframe] = _mark_preserved(previous_node, path=path, previous_execution=previous_execution)
    market_source = provider["market_history"]
    if market_source["status"] in {"partial", "unavailable"} and not market_source["records"] and previous["market_history"]["status"] != "invalid":
        history = _mark_preserved(previous["market_history"], path="market_history", previous_execution=previous_execution)
    return whale, history


def _refresh_aggregate_statuses(markets: dict[str, Any], whale: dict[str, Any]) -> None:
    for market in MARKETS:
        for feature in ("orderbook", "order_depth"):
            statuses = [node["status"] for node in markets[market][feature]["timeframes"].values()]
            status = "invalid" if "invalid" in statuses else ("available" if all(value == "available" for value in statuses) else "partial")
            markets[market][feature]["status"] = status
            markets[market][feature]["reason"] = None if status == "available" else "one_or_more_timeframes_unavailable"
    whale_statuses = [node["status"] for node in whale["timeframes"].values()]
    whale["status"] = "invalid" if "invalid" in whale_statuses else ("available" if all(value == "available" for value in whale_statuses) else "partial")
    whale["reason"] = None if whale["status"] == "available" else "one_or_more_timeframes_unavailable"


def _trades(dataset: Mapping[str, Any], market: str, reference: int) -> dict[str, Any]:
    observed = [enrich_trade_event(event) for event in dataset["events"]]
    for event in observed:
        event["age_seconds"] = max(0, reference - int(event["timestamp"]))
    large = [event for event in observed if event["meets_configured_threshold"]]
    windows = {name: aggregate_trade_window(large, window_end=reference, window_seconds=seconds)
               for name, seconds in LARGE_TRADE_WINDOWS_SECONDS.items()}
    status = "partial" if dataset["status"] == "partial" else ("available" if dataset["status"] == "available" else dataset["status"])
    return {"status": status, "reason": dataset.get("reason"), "observed_events": observed, "large_trade_events": large, "windows": windows,
            "coverage": {"observed_first_timestamp": min((event["timestamp"] for event in observed), default=None),
                         "observed_last_timestamp": max((event["timestamp"] for event in observed), default=None),
                         "observed_span_seconds": (max(event["timestamp"] for event in observed) - min(event["timestamp"] for event in observed)) if observed else 0,
                         "coverage_complete": False, "reason": "source_collection_window_not_exposed"},
            "provenance": _provenance(f"providers.coinglass.large_trades.{market}", dataset, max((e["timestamp"] for e in observed), default=None),
                                      "threshold_flag_filter_and_window_aggregation", "usd_and_base", LARGE_TRADE_WINDOWS_SECONDS)}



def _whale_orders(dataset: Mapping[str, Any], market: str, reference: int) -> dict[str, Any]:
    events = []
    for raw in dataset.get("events", []):
        row = deepcopy(dict(raw))
        row["age_seconds"] = max(0, reference - int(row["timestamp"]))
        events.append(row)
    events.sort(key=lambda row: (row["timestamp"], row["notional_quote"]), reverse=True)
    status = dataset.get("status", "unavailable")
    return {
        "status": status, "reason": dataset.get("reason"), "events": events,
        "current_active": [row for row in events if row.get("order_state") == "active"],
        "provenance": _provenance(f"providers.coinglass.whale_orders.{market}", dataset,
                                  max((row["timestamp"] for row in events), default=None),
                                  "provider_large_limit_orders", "usd_and_base", {}),
    }

def _whale(dataset: Mapping[str, Any], lookback: int) -> dict[str, Any]:
    timeframes = {}
    for timeframe in TIMEFRAMES:
        # Whale Activity is derived from normalized Large Limit Orders.
        # The deep 4h history is retained for Screen B; no separate whale-index endpoint exists.
        limit = 730 if timeframe == "4h" else 0
        records = ([deepcopy(record) for record in dataset["records"] if record["timeframe"] == timeframe][-limit:]
                   if limit else [])
        values = [float(record["whale_index_value"]) for record in records]
        current, previous = (records[-1] if records else None), (records[-2] if len(records) > 1 else None)
        mean, std, zscore = rolling_mean(values, lookback), rolling_std(values, lookback), rolling_z_score(values, lookback)
        reason = "insufficient_data" if len(values) < lookback else ("zero_rolling_standard_deviation" if std == 0 else None)
        timeframes[timeframe] = {"status": "available" if records else "unavailable", "reason": None if records else dataset.get("reason"),
                                 "records": records, "current": current, "previous": previous,
                                 "statistics": {"status": "available" if zscore is not None else "unavailable", "reason": reason,
                                                "absolute_change": absolute_change(values[-1], values[-2]) if len(values) > 1 else None,
                                                "percent_change": safe_percent_change(values[-1], values[-2]) if len(values) > 1 else None,
                                                "rolling_mean_20": mean, "rolling_std_20": std, "rolling_z_score_20": zscore}}
    status = timeframes["4h"]["status"]
    return {"status": status, "reason": None if status == "available" else "whale_activity_history_unavailable", "timeframes": timeframes}


def _historical_market_join(
    prices_input: Mapping[str, Any],
    provider: Mapping[str, Any],
    cvd_processing_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Join shared price/CVD context with liquidity-native 4h provider data.

    Price is reused from the canonical Prices Spot 4h history; it is never
    re-fetched by Liquidity. Historical aggressive buy/sell notional is reused
    from CVD Processing 4h when available, so all deep Screen-B primitives share
    one exact timestamp grid. The Footprint endpoint remains a recent Screen-A
    detail feed rather than a second deep historical source.
    """
    price_source = prices_input.get("markets", {}).get("spot", {}).get("timeframes", {}).get("4h", {})
    prices = {row["timestamp"]: row for row in price_source.get("records", []) if isinstance(row, Mapping) and type(row.get("timestamp")) is int}
    if not prices:
        return {
            "status": "unavailable",
            "reason": "normalized_prices_context_unavailable",
            "records": [],
            "market_records": {},
            "source_data_as_of": None,
            "provenance": {"providers": ["prices_shared", "coinglass"]},
        }

    books = {
        market: {
            row["timestamp"]: row
            for row in provider["orderbook"][market].get("records", [])
            if row.get("timeframe") == "4h" and row.get("bid_levels") and row.get("ask_levels")
        }
        for market in MARKETS
    }
    depths = {
        market: {
            row["timestamp"]: row
            for row in provider["order_depth"][market].get("records", [])
            if row.get("timeframe") == "4h" and row.get("range_percent") == REFERENCE_DEPTH_RANGE_PERCENT
        }
        for market in MARKETS
    }
    whales = {
        row["timestamp"]: row
        for row in provider["whale_activity"].get("records", [])
        if row.get("timeframe") == "4h"
    }

    cvd_rows: dict[str, dict[int, Mapping[str, Any]]] = {market: {} for market in MARKETS}
    if isinstance(cvd_processing_context, Mapping):
        for market, cvd_market in (("spot", "spot"), ("perpetual", "futures")):
            records = (
                cvd_processing_context.get("markets", {})
                .get(cvd_market, {})
                .get("timeframes", {})
                .get("4h", {})
                .get("records", [])
            )
            cvd_rows[market] = {
                row["timestamp"]: row
                for row in records
                if isinstance(row, Mapping) and type(row.get("timestamp")) is int
            }

    # Footprint remains an honest fallback if CVD is unavailable in an isolated
    # family run.  In the integrated runtime CVD is canonical for deep executed
    # buy/sell history.
    trade_bins: dict[str, dict[int, dict[str, float | int]]] = {market: {} for market in MARKETS}
    for market in MARKETS:
        for event in provider["large_trades"][market].get("events", []):
            bucket = int(event["timestamp"]) // 14400 * 14400
            totals = trade_bins[market].setdefault(bucket, {"buy": 0.0, "sell": 0.0, "count": 0})
            totals[str(event["side"])] = float(totals[str(event["side"])]) + float(event.get("volume_usd", 0.0))
            totals["count"] = int(totals["count"]) + 1

    market_records: dict[str, list[dict[str, Any]]] = {market: [] for market in MARKETS}
    for market in MARKETS:
        required_sets = [set(prices), set(whales), set(books[market]), set(depths[market])]
        if cvd_rows[market]:
            required_sets.append(set(cvd_rows[market]))
        timestamps = sorted(set.intersection(*required_sets)) if all(required_sets) else []
        for timestamp in timestamps:
            processed = process_order_book_levels(
                books[market][timestamp]["bid_levels"],
                books[market][timestamp]["ask_levels"],
                impact_quantity=MARKET_IMPACT_QUANTITY_BASE,
            )
            if processed.get("status") != "available":
                continue
            depth = depths[market][timestamp]
            bid_depth = float(depth["bids_usd"])
            ask_depth = float(depth["asks_usd"])
            total_depth = bid_depth + ask_depth
            bid_levels = processed.get("bid_levels", [])
            ask_levels = processed.get("ask_levels", [])
            bid_total = sum(float(x.get("notional_quote", 0.0)) for x in bid_levels)
            ask_total = sum(float(x.get("notional_quote", 0.0)) for x in ask_levels)
            bid_wall = (max((float(x.get("notional_quote", 0.0)) for x in bid_levels), default=0.0) / bid_total) if bid_total else None
            ask_wall = (max((float(x.get("notional_quote", 0.0)) for x in ask_levels), default=0.0) / ask_total) if ask_total else None

            cvd = cvd_rows[market].get(timestamp)
            if cvd is not None:
                buy = float(cvd.get("taker_buy_volume_usd") or 0.0)
                sell = float(cvd.get("taker_sell_volume_usd") or 0.0)
                trade_count = None
                executed_source = "cvd_processing_4h_aligned_to_4h_liquidity"
            else:
                trades = trade_bins[market].get(timestamp, {"buy": 0.0, "sell": 0.0, "count": 0})
                buy = float(trades["buy"])
                sell = float(trades["sell"])
                trade_count = int(trades["count"])
                executed_source = "liquidity_footprint_fallback"

            price_row = prices[timestamp]
            price_value = price_row.get("close", price_row.get("value", price_row.get("price")))
            market_records[market].append({
                "timestamp": timestamp,
                "asset": "BTC",
                "price": float(price_value),
                "market_cap": None,
                "circulating_supply": None,
                "best_bid": processed["best_bid"],
                "best_ask": processed["best_ask"],
                "mid_price": processed["mid_price"],
                "spread": processed["spread_quote"],
                "spread_bps": processed["spread_bps"],
                "bid_depth": bid_depth,
                "ask_depth": ask_depth,
                "depth_range_percent": REFERENCE_DEPTH_RANGE_PERCENT,
                "depth_imbalance": None if total_depth == 0 else (bid_depth - ask_depth) / total_depth,
                "bid_ask_depth_ratio": None if ask_depth == 0 else bid_depth / ask_depth,
                "market_impact_1btc_bps": processed.get("market_impact", {}).get("worst_side_impact_bps"),
                "bid_wall_score": bid_wall,
                "ask_wall_score": ask_wall,
                "whale_index_value": float(whales[timestamp]["whale_index_value"]),
                "large_trade_buy_notional": buy,
                "large_trade_sell_notional": sell,
                "large_trade_delta": clean_zero(buy - sell),
                "large_trade_count": trade_count,
                "executed_liquidity_source": executed_source,
                "status": "available",
                "market": market,
            })

    as_of_candidates: list[int] = []
    for values in (prices, whales, *books.values(), *depths.values()):
        if values:
            as_of_candidates.append(max(values))
    for values in cvd_rows.values():
        if values:
            as_of_candidates.append(max(values))
    effective_as_of = min(as_of_candidates) if as_of_candidates else None
    for market in MARKETS:
        market_records[market] = [
            row for row in market_records[market]
            if effective_as_of is not None and row["timestamp"] <= effective_as_of
        ][-730:]
    records = deepcopy(market_records["spot"])
    return {
        "status": "available" if records else "unavailable",
        "reason": None if records else "no_common_hourly_buckets",
        "records": records,
        "market_records": market_records,
        "source_data_as_of": effective_as_of,
        "provenance": {
            "providers": ["prices_shared", "coinglass", "cvd_processing" if any(cvd_rows.values()) else "liquidity_footprint_fallback"],
            "timezone": "UTC",
            "bucket_seconds": 3600,
            "bucket_semantics": "closed_hourly",
            "staleness_tolerance_seconds": 3600,
            "forward_fill": False,
            "reference_depth_range_percent": REFERENCE_DEPTH_RANGE_PERCENT,
            "canonical_market": "spot",
            "market_views": ["spot", "perpetual"],
            "effective_as_of_rule": "minimum_source_data_as_of",
            "price_owner": "prices_ohlcv",
            "executed_flow_owner": "cvd_volume_orderflow" if any(cvd_rows.values()) else "liquidity_microstructure",
        },
    }

def _native_liquidity_analysis(records: Sequence[Mapping[str, Any]], *, whale_activity_history_available: bool = False) -> dict[str, Any]:
    rows=list(records); timestamps=[int(r["timestamp"]) for r in rows]
    depth_imb=[r.get("depth_imbalance") for r in rows]; depth_z=native_rolling_zscore(depth_imb,30,10); ratios=[r.get("bid_ask_depth_ratio") for r in rows]
    spreads=[r.get("spread_bps") for r in rows]; impacts=[r.get("market_impact_1btc_bps") for r in rows]
    spread_z=native_rolling_zscore(spreads,30,10); impact_z=native_rolling_zscore(impacts,30,10)
    stress=[None if a is None and b is None else float((a or 0.0)+(b or 0.0))/2.0 for a,b in zip(spread_z,impact_z,strict=True)]
    bid_wall=[r.get("bid_wall_score") for r in rows]; ask_wall=[r.get("ask_wall_score") for r in rows]
    wall_conc=[None if a is None and b is None else max(float(a or 0.0),float(b or 0.0)) for a,b in zip(bid_wall,ask_wall,strict=True)]
    bid_depth=[r.get("bid_depth") for r in rows]; ask_depth=[r.get("ask_depth") for r in rows]
    bid_depth_z=native_rolling_zscore(bid_depth,30,10); ask_depth_z=native_rolling_zscore(ask_depth,30,10)
    upside_vac=[None if v is None else max(0.0,-float(v)) for v in ask_depth_z]; downside_vac=[None if v is None else max(0.0,-float(v)) for v in bid_depth_z]
    whale=[r.get("whale_index_value") for r in rows]; whale_z=native_rolling_zscore(whale,30,10)
    whale_persistence=[]
    for i,v in enumerate(whale_z):
        if v is None: whale_persistence.append(None); continue
        prev=[x for x in whale_z[max(0,i-5):i+1] if x is not None]
        whale_persistence.append(None if not prev else sum(1.0 if x*float(v)>0 else 0.0 for x in prev)/len(prev))
    cancellation=[None]*len(rows); cancellation_z=[None]*len(rows)
    price=[r.get("price") for r in rows]; price_return=([None]+[None if price[i-1] in (None,0) or price[i] is None else float(price[i])/float(price[i-1])-1 for i in range(1,len(price))]) if price else []
    buy=[r.get("large_trade_buy_notional") for r in rows]; sell=[r.get("large_trade_sell_notional") for r in rows]
    buy_int=[]; sell_int=[]
    for b,s,ad,bd in zip(buy,sell,ask_depth,bid_depth,strict=True):
        buy_int.append(None if b is None or ad in (None,0) else float(b)/float(ad)); sell_int.append(None if s is None or bd in (None,0) else float(s)/float(bd))
    buy_abs=[]; sell_abs=[]
    for bi,si,ret in zip(buy_int,sell_int,price_return,strict=True):
        buy_abs.append(None if bi is None or ret is None else float(bi)*max(0.0,-float(ret))*100.0)
        sell_abs.append(None if si is None or ret is None else float(si)*max(0.0,float(ret))*100.0)
    absorption=[None if a is None and b is None else float((a or 0.0)-(b or 0.0)) for a,b in zip(buy_abs,sell_abs,strict=True)]
    hmi=[]
    for di,st,ab,wc in zip(depth_z,stress,absorption,wall_conc,strict=True):
        vals=[v for v in (di,None if st is None else -float(st),ab,wc) if v is not None]
        hmi.append(None if not vals else float(sum(vals)/len(vals)))
    wasserstein=rolling_wasserstein(hmi,20,100)
    return {"status":"available" if rows else "unavailable","records":rows,"timestamps":timestamps,"indicators":{
        "depth_imbalance_pressure":{"depth_imbalance":depth_imb,"depth_imbalance_zscore":depth_z,"bid_ask_depth_ratio":ratios},
        "spread_market_impact_stress":{"spread_bps":spreads,"market_impact_1btc_bps":impacts,"liquidity_stress_score":stress},
        "liquidity_wall_concentration_vacuum":{"bid_wall_score":bid_wall,"ask_wall_score":ask_wall,"upside_vacuum_score":upside_vac,"downside_vacuum_score":downside_vac,"wall_concentration_score":wall_conc},
        "whale_persistence_cancellation":{"whale_persistence_score":whale_persistence,"cancellation_activity":cancellation,"cancellation_activity_zscore":cancellation_z,"status":"available" if whale_activity_history_available else "partial","reason":None if whale_activity_history_available else "historical_whale_activity_unavailable"},
        "executed_liquidity_absorption":{"buy_absorption_score":buy_abs,"sell_absorption_score":sell_abs,"absorption_index":absorption},
        "liquidity_regime_hmi":{"liquidity_hmi_score":hmi,"wasserstein_distance":wasserstein},
    },"current":{"liquidity_regime":"balanced" if native_latest(hmi) is None or abs(float(native_latest(hmi)))<0.5 else ("robust" if float(native_latest(hmi))>0 else "stressed"),
                         "depth_imbalance":native_latest(depth_imb),"liquidity_stress_score":native_latest(stress),"absorption_index":native_latest(absorption),"wasserstein_distance":native_latest(wasserstein)},
            "recalculate_in_hmi":False}


def _market_history(dataset: Mapping[str, Any], reference: int) -> dict[str, Any]:
    del reference
    records = [deepcopy(record) for record in dataset["records"]]
    enriched = []
    for index, record in enumerate(records):
        previous = records[index - 1] if index else None
        enriched.append({**record, "price_return_decimal": None if previous is None else clean_zero(record["price"] / previous["price"] - 1)})
    current = enriched[-1] if enriched else None
    changes = {}
    for days in MARKET_HISTORY_WINDOWS_DAYS:
        historical = observation_at_or_before(enriched, current["timestamp"] - days * 86400) if current else None
        changes[f"{days}d"] = {"status": "available" if historical else "unavailable",
                                "reason": None if historical else "insufficient_market_history",
                                "source_timestamp": historical["timestamp"] if historical else None,
                                "change_percent": clean_zero(100 * (current["price"] / historical["price"] - 1)) if historical else None}
    provenance = _provenance("providers.coinglass.market_history", dataset, current["timestamp"] if current else None,
                             "historical_observation_at_or_before", "percent", {"windows_days": MARKET_HISTORY_WINDOWS_DAYS})
    if isinstance(dataset.get("provenance"), Mapping):
        provenance["historical_join"] = deepcopy(dict(dataset["provenance"]))
    return {"status": "available" if current else "unavailable", "reason": None if current else dataset.get("reason"),
            "records": enriched, "current": current, "changes": changes,
            "provenance": provenance}


def _comparison(markets: Mapping[str, Any]) -> dict[str, Any]:
    depth_rows = []
    orderbook_rows = []
    def difference(left: float | None, right: float | None) -> float | None:
        return None if left is None or right is None else clean_zero(left - right)
    for timeframe in TIMEFRAMES:
        spot = {(row["timestamp"], row["range_percent"]): row for row in markets["spot"]["order_depth"]["timeframes"][timeframe]["direct_ranges"]}
        perpetual = {(row["timestamp"], row["range_percent"]): row for row in markets["perpetual"]["order_depth"]["timeframes"][timeframe]["direct_ranges"]}
        for timestamp, range_percent in sorted(set(spot) & set(perpetual)):
            s, p = spot[(timestamp, range_percent)], perpetual[(timestamp, range_percent)]
            depth_rows.append({"timestamp": timestamp, "timeframe": timeframe, "range_percent": range_percent,
                               "perpetual_to_spot_total_depth_ratio_quote": None if s["quote_notional"]["total"] == 0 else p["quote_notional"]["total"] / s["quote_notional"]["total"],
                               "perpetual_to_spot_total_depth_ratio_base": None if s["base_quantity"]["total"] == 0 else p["base_quantity"]["total"] / s["base_quantity"]["total"],
                               "imbalance_difference_quote_percent": difference(p["quote_notional"]["imbalance_percent"], s["quote_notional"]["imbalance_percent"]),
                               "imbalance_difference_base_percent": difference(p["base_quantity"]["imbalance_percent"], s["base_quantity"]["imbalance_percent"]),
                               "net_depth_difference_quote": difference(p["quote_notional"]["net"], s["quote_notional"]["net"])})
        spot_books = {row["timestamp"]: row for row in markets["spot"]["orderbook"]["timeframes"][timeframe]["history"] if row["status"] == "available"}
        perpetual_books = {row["timestamp"]: row for row in markets["perpetual"]["orderbook"]["timeframes"][timeframe]["history"] if row["status"] == "available"}
        for timestamp in sorted(set(spot_books) & set(perpetual_books)):
            spot_book, perpetual_book = spot_books[timestamp], perpetual_books[timestamp]
            spot_impact, perpetual_impact = spot_book["market_impact"], perpetual_book["market_impact"]
            orderbook_rows.append({"timestamp": timestamp, "timeframe": timeframe,
                                   "spread_difference_bps": difference(perpetual_book["spread_bps"], spot_book["spread_bps"]),
                                   "buy_impact_difference_bps": difference(perpetual_impact["buy"]["impact_bps"], spot_impact["buy"]["impact_bps"]),
                                   "sell_impact_difference_bps": difference(perpetual_impact["sell"]["impact_bps"], spot_impact["sell"]["impact_bps"])})
    available = bool(depth_rows or orderbook_rows)
    return {"spot_perpetual": {"status": "available" if available else "unavailable", "reason": None if available else "no_exact_timestamp_matches",
                               "order_depth": depth_rows, "orderbook": orderbook_rows}}


def _source_selection(provider: Mapping[str, Any]) -> dict[str, Any]:
    result = {}
    for market in MARKETS:
        for name, role in (("orderbook", "canonical"), ("order_depth", "canonical"),
                           ("large_trades", "executed_footprint"), ("whale_orders", "large_limit_orders")):
            dataset = provider.get(name, {}).get(market, _empty_events_dataset())
            result[f"{name}_{market}"] = {"provider": "coinglass", "dataset_path": f"providers.coinglass.{name}.{market}",
                                           "source_status": dataset["status"], "selected": True, "role": role, "fallback_applied": False}
    dataset = provider["whale_activity"]
    result["whale_activity"] = {"provider": "coinglass", "dataset_path": "providers.coinglass.whale_activity",
                                "source_status": dataset["status"], "selected": True,
                                "role": "derived_large_limit_order_activity", "fallback_applied": False}
    # Market history is no longer a Liquidity provider endpoint.  It is joined
    # from the already-normalized Prices context inside Processing.
    result["market_history"] = {"provider": "shared_prices_context", "dataset_path": "processing.market_history",
                                "source_status": "available", "selected": True,
                                "role": "shared_price_context", "fallback_applied": False}
    return result


def _invalid_output(input_contract: Mapping[str, Any], execution_timestamp: int, configuration: Mapping[str, Any]) -> dict[str, Any]:
    return {"family": "liquidity_microstructure", "stage": "processing", "mode": input_contract.get("mode"),
            "reference_timestamp": input_contract.get("reference_timestamp"), "execution_timestamp": execution_timestamp,
            "configuration": dict(configuration), "context": deepcopy(dict(input_contract.get("context", {}))),
            "source_selection": {}, "markets": {}, "whale_activity": {}, "market_history": {},
            "comparison": {"spot_perpetual": {}}, "features": {},
            "quality": {"status": "invalid", "reason": "input_quality_invalid", "required_features": [], "optional_features": [],
                        "missing_features": [], "partial_features": [], "unavailable_features": [], "invalid_features": [], "warnings": [], "errors": []}}



def _empty_events_dataset(reason: str = "provider_dataset_not_available") -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason, "events": [], "records": [],
            "source_data_as_of": None, "provenance": {"provider": "coinglass"}, "warnings": [], "errors": []}

def process_liquidity_microstructure(input_contract: Mapping[str, Any], *, existing_processing: Mapping[str, Any] | None = None,
                                     prices_input_context: Mapping[str, Any] | None = None,
                                     cvd_processing_context: Mapping[str, Any] | None = None,
                                     now_timestamp: int | None = None, config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    input_copy, config_copy = input_contract, dict(config or {})
    validate_liquidity_microstructure_input(input_copy)
    impact_quantity = float(config_copy.get("market_impact_quantity_base", MARKET_IMPACT_QUANTITY_BASE))
    lookback = int(config_copy.get("whale_rolling_lookback", WHALE_ROLLING_LOOKBACK))
    configured_depth_ranges = tuple(config_copy.get("depth_ranges_percent", DEPTH_RANGES_PERCENT))
    if impact_quantity <= 0 or lookback <= 0 or not configured_depth_ranges or any(value not in DEPTH_RANGES_PERCENT for value in configured_depth_ranges):
        raise ValueError("invalid_processing_configuration")
    configuration = {"version": PROCESSING_VERSION, "markets": list(MARKETS), "timeframes": list(TIMEFRAMES),
                     "depth_ranges_percent": list(configured_depth_ranges), "reference_depth_range_percent": REFERENCE_DEPTH_RANGE_PERCENT,
                     "market_impact_quantity_base": impact_quantity, "whale_rolling_lookback": lookback}
    execution = int(now_timestamp or time.time())
    if input_copy["quality"]["status"] == "invalid":
        return _invalid_output(input_copy, execution, configuration)
    provider, reference = input_copy["providers"]["coinglass"], input_copy["reference_timestamp"]
    whale_provider = provider.get("whale_orders", {})
    markets = {market: {"orderbook": _orderbooks(provider["orderbook"][market], market, impact_quantity),
                        "order_depth": _depth(provider["order_depth"][market], market),
                        "large_trades": _trades(provider["large_trades"][market], market, reference),
                        "whale_orders": _whale_orders(whale_provider.get(market, _empty_events_dataset()), market, reference)} for market in MARKETS}
    whale = _whale(provider["whale_activity"], lookback)
    market_dataset = (_historical_market_join(prices_input_context, provider, cvd_processing_context)
                      if isinstance(prices_input_context, Mapping) else provider["market_history"])
    history = _market_history(market_dataset, reference)
    market_histories = {market: _market_history({"status": market_dataset.get("status", "unavailable"),
                                                  "reason": market_dataset.get("reason"),
                                                  "records": market_dataset.get("market_records", {}).get(market, market_dataset.get("records", []) if market == "spot" else []),
                                                  "provenance": market_dataset.get("provenance", {})}, reference)
                        for market in MARKETS}
    previous = existing_processing
    compatible_previous = (isinstance(previous, Mapping) and previous.get("family") == "liquidity_microstructure" and
                           previous.get("configuration") == configuration and previous.get("context") == input_copy["context"])
    if compatible_previous:
        whale, history = _preserve_granular(provider=provider, markets=markets, whale=whale, history=history, previous=previous)
        _refresh_aggregate_statuses(markets, whale)
    whale_history_available = len(whale.get("timeframes", {}).get("4h", {}).get("records", [])) >= 20
    liquidity_analysis = {
        market: _native_liquidity_analysis(
            market_histories[market].get("records", []),
            whale_activity_history_available=whale_history_available,
        )
        for market in MARKETS
    }
    comparison = _comparison(markets)
    required = {f"markets.{market}.{feature}": markets[market][feature]["status"] for market in MARKETS
                for feature in ("orderbook", "order_depth")}
    required.update({"whale_activity": whale["status"]})
    optional = {"market_history": history["status"],
                **{f"markets.{market}.large_trades": markets[market]["large_trades"]["status"] for market in MARKETS},
                **{f"markets.{market}.whale_orders": markets[market]["whale_orders"]["status"] for market in MARKETS}}
    invalid = [name for name, status in required.items() if status == "invalid"]
    partial = [name for name, status in required.items() if status == "partial"]
    unavailable = [name for name, status in required.items() if status == "unavailable"]
    quality_status = "invalid" if invalid else ("partial" if partial or unavailable else "ok")
    output = {"family": "liquidity_microstructure", "stage": "processing", "mode": input_copy["mode"],
              "reference_timestamp": reference, "execution_timestamp": execution, "configuration": configuration,
              "context": deepcopy(dict(input_copy["context"])),
              "source_selection": _source_selection(provider), "markets": markets, "whale_activity": whale,
              "market_history": history, "market_histories": market_histories, "liquidity_analysis": liquidity_analysis, "comparison": comparison,
              "features": build_liquidity_microstructure_features(markets=markets, whale_activity=whale,
                                                                   market_history=history, comparison=comparison),
              "quality": {"status": quality_status, "required_features": list(required),
                          "optional_features": ["market_history", "markets.spot.orderbook.market_impact", "markets.perpetual.orderbook.market_impact",
                                                "comparison.spot_perpetual", "whale_activity.rolling_statistics"],
                          "optional_feature_statuses": optional,
                          "missing_features": [], "partial_features": partial, "unavailable_features": unavailable,
                          "invalid_features": invalid, "warnings": [], "errors": []}}
    return output


class LiquidityMicrostructureProcessor:
    def __init__(self, input_contract: Mapping[str, Any], *, existing_processing: Mapping[str, Any] | None = None,
                 prices_input_context: Mapping[str, Any] | None = None,
                 cvd_processing_context: Mapping[str, Any] | None = None,
                 now_timestamp: int | None = None, config: Mapping[str, Any] | None = None) -> None:
        self.arguments = {"input_contract": input_contract, "existing_processing": existing_processing,
                          "prices_input_context": prices_input_context, "cvd_processing_context": cvd_processing_context,
                          "now_timestamp": now_timestamp, "config": config}

    def run(self) -> dict[str, Any]:
        return process_liquidity_microstructure(**self.arguments)
