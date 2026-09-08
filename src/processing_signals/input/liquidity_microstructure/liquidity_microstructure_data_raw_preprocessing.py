"""Validation, normalization and state merge for Liquidity Microstructure Input v0.1."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
import hashlib
import json
import math
from statistics import median
import time
from typing import Any

from .liquidity_microstructure_data_raw_extract import (
    LIQUIDITY_MICROSTRUCTURE_FAMILY, VALID_MODES, RawFetcher, extract_liquidity_microstructure_raw,
)

DATASET_STATES = {"available", "partial", "unavailable", "invalid"}
REQUIRED_DATASETS = (
    "coinglass.orderbook.spot", "coinglass.orderbook.perpetual", "coinglass.order_depth.spot",
    "coinglass.order_depth.perpetual", "coinglass.whale_activity",
)
OPTIONAL_DATASETS = (
    "coinglass.large_trades.spot", "coinglass.large_trades.perpetual",
    "coinglass.whale_orders.spot", "coinglass.whale_orders.perpetual", "coinglass.market_history",
)


def _finite(value: Any, field: str, *, positive: bool = False, nonnegative: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field}_boolean_not_allowed")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}_must_be_numeric") from exc
    if not math.isfinite(number) or (positive and number <= 0) or (nonnegative and number < 0):
        raise ValueError(f"{field}_out_of_range")
    return number


def _timestamp(value: Any) -> tuple[int, str]:
    number = _finite(value, "timestamp", positive=True)
    unit = "milliseconds" if number >= 100_000_000_000 else "seconds"
    return int(number / 1000 if unit == "milliseconds" else number), unit


def _envelope(response: Any, *, websocket: bool = False) -> list[Any]:
    if websocket:
        if response is None:
            return []
        if isinstance(response, list):
            return response
        if isinstance(response, Mapping):
            data = response.get("data", response.get("events", []))
            return data if isinstance(data, list) else [data]
        raise ValueError("websocket_response_invalid")
    if not isinstance(response, Mapping) or response.get("code") not in (0, "0") or "data" not in response:
        raise ValueError("coinglass_envelope_invalid")
    data = response["data"]
    if isinstance(data, Mapping) and isinstance(data.get("data"), list):
        data = data["data"]
    if not isinstance(data, list):
        raise ValueError("coinglass_data_must_be_list")
    return data


def _levels(value: Any, *, descending: bool) -> list[dict[str, float]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("orderbook_levels_invalid")
    result = []
    for row in value:
        if isinstance(row, Mapping):
            price, quantity = row.get("price"), row.get("quantity", row.get("volume"))
        elif isinstance(row, Sequence) and not isinstance(row, (str, bytes)) and len(row) >= 2:
            price, quantity = row[0], row[1]
        else:
            raise ValueError("orderbook_level_invalid")
        result.append({"price": _finite(price, "price", positive=True), "quantity": _finite(quantity, "quantity", nonnegative=True)})
    return sorted(result, key=lambda row: row["price"], reverse=descending)


def _record_time(record: Mapping[str, Any]) -> tuple[int, str]:
    return _timestamp(record.get("timestamp", record.get("time", record.get("t"))))


def _normalize_heatmap(record: Any, request: Mapping[str, Any]) -> tuple[dict[str, Any], str | None, str]:
    dimensions = request["dimensions"]
    base = {"market_type": dimensions["market_type"],
            "exchange": dimensions["exchange"], "symbol": dimensions["symbol"],
            "timeframe": dimensions["timeframe"]}

    # Current CoinGlass V4 Orderbook Heatmap provider shape:
    # [timestamp_seconds, side_0_levels, side_1_levels]
    if isinstance(record, Sequence) and not isinstance(record, (str, bytes, Mapping)) and len(record) >= 3:
        timestamp, unit = _timestamp(record[0])
        side_0 = _levels(record[1], descending=True)
        side_1 = _levels(record[2], descending=False)
        if not side_0 or not side_1:
            raise ValueError("orderbook_heatmap_empty_side")

        # The published sample does not label the two side arrays. Infer the
        # semantic mapping only when the price sets are non-overlapping.
        max_0 = max(row["price"] for row in side_0)
        min_0 = min(row["price"] for row in side_0)
        max_1 = max(row["price"] for row in side_1)
        min_1 = min(row["price"] for row in side_1)

        if max_0 < min_1:
            bids = sorted(side_0, key=lambda row: row["price"], reverse=True)
            asks = sorted(side_1, key=lambda row: row["price"])
            warning = None
        elif max_1 < min_0:
            bids = sorted(side_1, key=lambda row: row["price"], reverse=True)
            asks = sorted(side_0, key=lambda row: row["price"])
            warning = None
        else:
            return {**base, "timestamp": timestamp,
                    "provider_side_0": side_0, "provider_side_1": side_1}, "orderbook_side_mapping_unverified", unit

        return {**base, "timestamp": timestamp,
                "bid_levels": bids, "ask_levels": asks}, warning, unit

    # Compatibility with earlier fixture/live adapters that may already label sides.
    if not isinstance(record, Mapping):
        raise ValueError("orderbook_snapshot_shape_invalid")
    timestamp, unit = _record_time(record)
    if "bids" in record and "asks" in record:
        return {**base, "timestamp": timestamp,
                "bid_levels": _levels(record["bids"], descending=True),
                "ask_levels": _levels(record["asks"], descending=False)}, None, unit
    sides = record.get("data", record.get("levels"))
    if isinstance(sides, Sequence) and len(sides) >= 2:
        return {**base, "timestamp": timestamp,
                "provider_side_0": _levels(sides[0], descending=False),
                "provider_side_1": _levels(sides[1], descending=False)}, "orderbook_side_mapping_unverified", unit
    raise ValueError("orderbook_snapshot_shape_invalid")


def _normalize_depth(record: Mapping[str, Any], request: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    timestamp, unit = _record_time(record)
    dimensions = request["dimensions"]
    aliases = {"bids_usd": ("bids_usd", "bid_usd", "bidUsd"), "bids_quantity": ("bids_quantity", "bid_quantity", "bidQuantity"),
               "asks_usd": ("asks_usd", "ask_usd", "askUsd"), "asks_quantity": ("asks_quantity", "ask_quantity", "askQuantity")}
    output = {"timestamp": timestamp, "market_type": dimensions["market_type"], "exchange": dimensions["exchange"],
              "symbol": dimensions["symbol"], "timeframe": dimensions["timeframe"], "range_percent": dimensions["range_percent"]}
    for target, names in aliases.items():
        output[target] = _finite(next((record[name] for name in names if name in record), None), target, nonnegative=True)
    return output, unit


def _normalize_footprint(record: Any, request: Mapping[str, Any]) -> list[tuple[dict[str, Any], str]]:
    if not isinstance(record, Sequence) or isinstance(record, (str, bytes, Mapping)) or len(record) < 2:
        raise ValueError("footprint_record_invalid")
    timestamp, unit = _timestamp(record[0])
    bins = record[1]
    if not isinstance(bins, Sequence) or isinstance(bins, (str, bytes)):
        raise ValueError("footprint_bins_invalid")
    dimensions = request["dimensions"]
    output: list[tuple[dict[str, Any], str]] = []
    for index, row in enumerate(bins):
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)) or len(row) < 8:
            raise ValueError("footprint_bin_invalid")
        price_start = _finite(row[0], "price_start", positive=True)
        price_end = _finite(row[1], "price_end", positive=True)
        price = (price_start + price_end) / 2.0
        buy_qty = _finite(row[2], "buy_quantity", nonnegative=True)
        sell_qty = _finite(row[3], "sell_quantity", nonnegative=True)
        buy_usd = _finite(row[6], "buy_usd", nonnegative=True)
        sell_usd = _finite(row[7], "sell_usd", nonnegative=True)
        for side, quantity, notional in (("buy", buy_qty, buy_usd), ("sell", sell_qty, sell_usd)):
            if quantity <= 0 and notional <= 0:
                continue
            identity = f"footprint|{dimensions['market_type']}|{timestamp}|{index}|{side}"
            output.append(({
                "event_id": hashlib.sha256(identity.encode()).hexdigest(),
                "timestamp": timestamp, "market_type": dimensions["market_type"],
                "exchange": dimensions["exchange"], "symbol": dimensions["symbol"],
                "base_asset": dimensions["asset"], "side": side, "price": price,
                "volume_usd": notional, "quantity_base": quantity,
                "provider_channel": request["endpoint_id"],
                "configured_min_volume_usd": 0.0, "meets_configured_threshold": True,
                "price_start": price_start, "price_end": price_end,
                "aggregation_semantics": "footprint_price_bin",
            }, unit))
    return output


def _normalize_large_order(record: Mapping[str, Any], request: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    dimensions = request["dimensions"]
    timestamp, unit = _timestamp(record.get("current_time", record.get("start_time")))
    start_timestamp, _ = _timestamp(record.get("start_time", record.get("current_time")))
    side_raw = record.get("order_side")
    if side_raw not in (1, 2):
        raise ValueError("large_order_side_invalid")
    state_raw = record.get("order_state")
    price = _finite(record.get("price"), "price", positive=True)
    quantity = _finite(record.get("current_quantity", record.get("start_quantity")), "current_quantity", nonnegative=True)
    notional = _finite(record.get("current_usd_value", record.get("start_usd_value")), "current_usd_value", nonnegative=True)
    return {
        "event_id": str(record.get("id") or hashlib.sha256(f"large_order|{timestamp}|{price}|{quantity}".encode()).hexdigest()),
        "timestamp": timestamp, "market_type": dimensions["market_type"],
        "exchange": str(record.get("exchange_name", dimensions["exchange"])),
        "symbol": str(record.get("symbol", dimensions["symbol"])),
        "base_asset": str(record.get("base_asset", dimensions["asset"])),
        "side": "buy" if side_raw == 1 else "sell", "price": price,
        "quantity_base": quantity, "notional_quote": notional,
        "first_seen_timestamp": start_timestamp, "last_seen_timestamp": timestamp,
        "order_state": "active" if state_raw == 1 else "completed" if state_raw == 2 else "unknown",
        "executed_quantity_base": _finite(record.get("executed_volume", 0), "executed_volume", nonnegative=True),
        "executed_usd": _finite(record.get("executed_usd_value", 0), "executed_usd_value", nonnegative=True),
        "trade_count": int(record.get("trade_count", 0) or 0),
        "provider_endpoint": request["endpoint_id"],
    }, unit


def _normalize_market(record: Mapping[str, Any], request: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    timestamp, unit = _record_time(record)
    return {"timestamp": timestamp, "asset": request["dimensions"]["asset"], "price": _finite(record.get("price"), "price", positive=True),
            "circulating_supply": _finite(record.get("circulating_supply"), "circulating_supply", nonnegative=True),
            "market_cap": _finite(record.get("market_cap"), "market_cap", nonnegative=True)}, unit


def _dataset_key(request: Mapping[str, Any]) -> str:
    endpoint, market = request["endpoint_id"], request["dimensions"].get("market_type")
    if "orderbook_heatmap" in endpoint:
        return f"coinglass.orderbook.{market}"
    if "order_depth" in endpoint:
        return f"coinglass.order_depth.{market}"
    if "footprint" in endpoint:
        return f"coinglass.large_trades.{market}"
    if "large_limit_orders" in endpoint:
        return f"coinglass.whale_orders.{market}"
    raise ValueError(f"unsupported_liquidity_endpoint:{endpoint}")


def validate_liquidity_microstructure_raw_bundle(bundle: Mapping[str, Any]) -> None:
    if not isinstance(bundle, Mapping) or bundle.get("family") != LIQUIDITY_MICROSTRUCTURE_FAMILY or not isinstance(bundle.get("requests"), list):
        raise ValueError("invalid_liquidity_microstructure_raw_bundle")
    for request in bundle["requests"]:
        if not isinstance(request, Mapping) or request.get("status") not in {"ok", "error"}:
            raise ValueError("invalid_liquidity_microstructure_raw_request")


def determine_liquidity_microstructure_input_mode(*, requested_mode: str | None = None,
                                                   existing_contract: Mapping[str, Any] | None = None,
                                                   recovery_requests: Sequence[Any] | None = None) -> str:
    mode = requested_mode or ("incremental" if existing_contract else "bootstrap")
    if mode not in VALID_MODES or (mode == "recovery" and not recovery_requests):
        raise ValueError("invalid_liquidity_microstructure_input_mode")
    return mode


def _empty_dataset() -> dict[str, Any]:
    return {"status": "unavailable", "reason": "no_data", "records": [], "incoming_records": 0,
            "source_data_as_of": None, "provenance": {"provider": "coinglass", "timestamp_units": []}, "warnings": [], "errors": []}


def _get_existing(contract: Mapping[str, Any] | None, key: str) -> Mapping[str, Any] | None:
    if not contract:
        return None
    node: Any = contract.get("providers", {}).get("coinglass", {})
    for part in key.split(".")[1:]:
        node = node.get(part, {}) if isinstance(node, Mapping) else {}
    return node if isinstance(node, Mapping) and "status" in node else None


def _merge(existing: Mapping[str, Any] | None, incoming: list[dict[str, Any]], *, events: bool = False) -> list[dict[str, Any]]:
    old = list((existing or {}).get("events" if events else "records", []))
    def identity(row: Mapping[str, Any]) -> Any:
        if events:
            return row["event_id"]
        return (row["timestamp"], row.get("market_type"), row.get("timeframe"), row.get("range_percent"))
    merged = {identity(row): deepcopy(dict(row)) for row in old}
    merged.update({identity(row): row for row in incoming})
    return sorted(merged.values(), key=lambda row: (row["timestamp"], str(identity(row))))


def _derive_depth_dataset(orderbook: Mapping[str, Any], *, market_type: str, range_percent: int = 10) -> dict[str, Any]:
    records = []
    for row in orderbook.get("records", []):
        if not isinstance(row, Mapping):
            continue
        bids = row.get("bid_levels") or []
        asks = row.get("ask_levels") or []
        if not bids or not asks:
            continue
        try:
            best_bid = max(float(level["price"]) for level in bids)
            best_ask = min(float(level["price"]) for level in asks)
        except (KeyError, TypeError, ValueError):
            continue
        mid = (best_bid + best_ask) / 2.0
        lower = mid * (1.0 - range_percent / 100.0)
        upper = mid * (1.0 + range_percent / 100.0)
        selected_bids = [level for level in bids if float(level.get("price", 0.0)) >= lower]
        selected_asks = [level for level in asks if float(level.get("price", 0.0)) <= upper]
        records.append({
            "timestamp": int(row["timestamp"]), "market_type": market_type,
            "exchange": row.get("exchange"), "symbol": row.get("symbol"),
            "timeframe": row.get("timeframe"), "range_percent": range_percent,
            "bids_usd": sum(float(level["price"]) * float(level["quantity"]) for level in selected_bids),
            "bids_quantity": sum(float(level["quantity"]) for level in selected_bids),
            "asks_usd": sum(float(level["price"]) * float(level["quantity"]) for level in selected_asks),
            "asks_quantity": sum(float(level["quantity"]) for level in selected_asks),
            "mid_price": mid, "calculation": "derived_from_orderbook_levels_10pct",
        })
    status = "available" if records else "unavailable"
    return {
        "status": status, "reason": None if records else "orderbook_depth_derivation_unavailable",
        "records": records, "incoming_records": len(records),
        "source_data_as_of": max((row["timestamp"] for row in records), default=None),
        "provenance": {"provider": "calculated", "source": f"coinglass.orderbook.{market_type}",
                       "calculation": "sum levels inside +/-10% of snapshot mid", "timestamp_units": ["seconds"] if records else []},
        "warnings": [], "errors": [],
    }


def _large_level_notional(levels: Sequence[Mapping[str, Any]]) -> float:
    notionals = [
        float(level.get("price", 0.0)) * float(level.get("quantity", 0.0))
        for level in levels
        if isinstance(level, Mapping) and float(level.get("price", 0.0) or 0.0) > 0 and float(level.get("quantity", 0.0) or 0.0) > 0
    ]
    if not notionals:
        return 0.0
    threshold = median(notionals) * 1.75
    selected = [value for value in notionals if value >= threshold]
    if not selected:
        selected = sorted(notionals, reverse=True)[:2]
    return sum(selected)


def _derive_whale_activity_dataset(
    spot_orders: Mapping[str, Any],
    perpetual_orders: Mapping[str, Any],
    spot_orderbook: Mapping[str, Any],
    perpetual_orderbook: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a 4h whale-liquidity activity history from frozen primitives.

    The CoinGlass Large Limit Order endpoint is a current snapshot, so it can
    provide the latest true whale-order state but not a deep bootstrap history.
    Historical bootstrap therefore derives a large-level imbalance proxy from
    the existing 4h order-book primitive. Once repeated structural cycles have
    accumulated actual whale snapshots, the current bucket is replaced by the
    large-limit-order calculation. No extra endpoint is introduced.
    """
    buckets: dict[int, dict[str, Any]] = {}

    # Deep historical proxy from the 4h order-book primitive.
    for dataset in (spot_orderbook, perpetual_orderbook):
        for row in dataset.get("records", []):
            if not isinstance(row, Mapping) or row.get("timeframe") != "4h" or type(row.get("timestamp")) is not int:
                continue
            timestamp = int(row["timestamp"])
            bids = row.get("bid_levels") or []
            asks = row.get("ask_levels") or []
            buy = _large_level_notional(bids)
            sell = _large_level_notional(asks)
            state = buckets.setdefault(timestamp, {"buy": 0.0, "sell": 0.0, "sources": set()})
            state["buy"] += buy
            state["sell"] += sell
            state["sources"].add("orderbook_large_level_proxy")

    # Latest real large-limit-order state overrides/adds to the current 4h bucket.
    actual: dict[int, dict[str, float]] = {}
    for dataset in (spot_orders, perpetual_orders):
        for row in dataset.get("events", []):
            if not isinstance(row, Mapping) or type(row.get("timestamp")) is not int:
                continue
            bucket = int(row["timestamp"]) // 14400 * 14400
            state = actual.setdefault(bucket, {"buy": 0.0, "sell": 0.0})
            side = str(row.get("side", "")).lower()
            notional = float(row.get("notional_quote", row.get("volume_usd", 0.0)) or 0.0)
            if side in {"buy", "bid", "long"}:
                state["buy"] += notional
            elif side in {"sell", "ask", "short"}:
                state["sell"] += notional
    for timestamp, values in actual.items():
        buckets[timestamp] = {"buy": values["buy"], "sell": values["sell"], "sources": {"large_limit_orders"}}

    records = []
    for timestamp in sorted(buckets):
        state = buckets[timestamp]
        buy, sell = float(state["buy"]), float(state["sell"] )
        total = buy + sell
        if total <= 0:
            continue
        sources = sorted(state.get("sources") or [])
        records.append({
            "timestamp": timestamp, "market_type": "aggregate", "exchange": "Binance",
            "symbol": "BTCUSDT", "timeframe": "4h",
            "whale_index_value": (buy - sell) / total,
            "buy_notional_quote": buy, "sell_notional_quote": sell,
            "calculation": "large_limit_order_notional_imbalance" if sources == ["large_limit_orders"] else "orderbook_large_level_imbalance_proxy",
            "source_kind": sources[0] if len(sources) == 1 else "+".join(sources),
        })
    records = records[-500:]
    status = "available" if records else "unavailable"
    return {
        "status": status, "reason": None if records else "whale_activity_history_unavailable",
        "records": records, "incoming_records": len(records),
        "source_data_as_of": max((row["timestamp"] for row in records), default=None),
        "provenance": {
            "provider": "calculated",
            "source": "coinglass.whale_orders+coinglass.orderbook",
            "calculation": "actual large-limit-order imbalance for current bucket; 4h large-level orderbook proxy for bootstrap history",
            "timestamp_units": ["seconds"] if records else [],
            "uses_new_endpoint": False,
        },
        "warnings": [], "errors": [],
    }


class LiquidityMicrostructureInputPreprocessor:
    def preprocess(self, raw_bundle: Mapping[str, Any], *, existing_contract: Mapping[str, Any] | None = None,
                   reference_timestamp: int | None = None, execution_timestamp: int | None = None,
                   data_mode: str = "live", is_demo: bool = False, debug_raw: bool = False) -> dict[str, Any]:
        validate_liquidity_microstructure_raw_bundle(raw_bundle)
        mode = str(raw_bundle["mode"])
        grouped: dict[str, list[Mapping[str, Any]]] = {}
        for request in raw_bundle["requests"]:
            grouped.setdefault(_dataset_key(request), []).append(request)
        datasets: dict[str, dict[str, Any]] = {}
        for key in (*REQUIRED_DATASETS, *OPTIONAL_DATASETS):
            existing = _get_existing(existing_contract, key)
            incoming: list[dict[str, Any]] = []
            warnings: list[str] = []
            errors: list[str] = []
            units: set[str] = set()
            requests = grouped.get(key, [])
            if not requests and existing is not None:
                datasets[key] = deepcopy(dict(existing))
                continue
            for request in requests:
                if request["status"] == "error":
                    errors.append(f'{request["request_id"]}:{request["error"]["type"]}')
                    continue
                try:
                    records = _envelope(request["response"], websocket=False)
                    # Emulator integration always returns 500 records. During
                    # fast incremental Liquidity refresh only the newest
                    # historical snapshot is new/current; ingesting all 500 on
                    # every 10 s cycle would be wasteful and can blur the live
                    # tape. Large-limit-order endpoints are current snapshots,
                    # so their full 500 rows are intentionally retained.
                    if ".orderbook." in key:
                        # Incremental structural refresh consumes only the newest
                        # snapshot. Bootstrap keeps the full 500-record 4h history
                        # required by Screen B while bounding the dense 1m book.
                        if mode == "incremental":
                            records = records[-1:]
                        else:
                            timeframe = str(request.get("dimensions", {}).get("timeframe") or "")
                            records = records[-500:] if timeframe == "4h" else records[-120:]
                    elif ".large_trades." in key:
                        # Footprint expands each provider candle into multiple
                        # price-bin events. Deep execution history is owned by CVD,
                        # so Liquidity keeps a compact recent tape only.
                        records = records[-1:] if mode == "incremental" else records[-24:]
                    for record in records:
                        if ".large_trades." in key:
                            for normalized, unit in _normalize_footprint(record, request):
                                units.add(unit); incoming.append(normalized)
                            continue
                        if not isinstance(record, Mapping) and ".orderbook." not in key:
                            raise ValueError("record_must_be_mapping")
                        if ".orderbook." in key:
                            normalized, warning, unit = _normalize_heatmap(record, request)
                            if warning:
                                warnings.append(warning)
                        elif ".order_depth." in key:
                            normalized, unit = _normalize_depth(record, request)
                        elif ".whale_orders." in key:
                            normalized, unit = _normalize_large_order(record, request)
                        else:
                            normalized, unit = _normalize_market(record, request)
                        units.add(unit); incoming.append(normalized)
                except Exception as exc:
                    errors.append(f'{request["request_id"]}:{type(exc).__name__}:{exc}')
            events = ".large_trades." in key or ".whale_orders." in key
            merged = _merge(existing, incoming, events=events)
            if ".orderbook." in key:
                # Bound long-running state per native timeframe. Keep the deep
                # 4h bootstrap history required by native Screen-B analytics,
                # while dense intraday snapshots stay compact.
                compact: list[dict[str, Any]] = []
                retention = {"1m": 120, "5m": 120, "15m": 120, "4h": 500}
                for timeframe, limit in retention.items():
                    compact.extend([row for row in merged if row.get("timeframe") == timeframe][-limit:])
                merged = sorted(compact, key=lambda row: (row["timestamp"], str(row.get("timeframe"))))
            elif ".large_trades." in key and len(merged) > 3_000:
                merged = merged[-3_000:]
            elif ".whale_orders." in key and len(merged) > 2_000:
                # A bounded rolling order-state history is enough to measure
                # persistence/cancellation while preventing unbounded growth.
                merged = merged[-2_000:]
            if incoming:
                status = "partial" if warnings or errors else "available"
                reason = warnings[0] if warnings else ("endpoint_update_partial" if errors else None)
            elif existing and existing.get("status") in {"available", "partial"}:
                status, reason = existing["status"], "update_failed_previous_state_preserved" if errors else existing.get("reason")
            elif events and requests and not errors:
                status, reason = "partial", "stream_warmup_in_progress"
            else:
                status, reason = ("invalid", "all_updates_invalid") if errors else ("unavailable", "no_data")
            dataset = {"status": status, "reason": reason, "incoming_records": len(incoming),
                       "source_data_as_of": max((row["timestamp"] for row in merged), default=(existing or {}).get("source_data_as_of")),
                       "provenance": {"provider": "coinglass", "timestamp_units": sorted(units),
                                      "latest_attempt": int(execution_timestamp or time.time())},
                       "warnings": sorted(set(warnings)), "errors": errors}
            dataset["events" if events else "records"] = merged
            datasets[key] = dataset
        # Derive depth and whale-activity datasets from primitives that
        # are already part of the frozen 33-endpoint inventory.
        datasets["coinglass.order_depth.spot"] = _derive_depth_dataset(datasets["coinglass.orderbook.spot"], market_type="spot")
        datasets["coinglass.order_depth.perpetual"] = _derive_depth_dataset(datasets["coinglass.orderbook.perpetual"], market_type="perpetual")
        datasets["coinglass.whale_activity"] = _derive_whale_activity_dataset(
            datasets["coinglass.whale_orders.spot"], datasets["coinglass.whale_orders.perpetual"],
            datasets["coinglass.orderbook.spot"], datasets["coinglass.orderbook.perpetual"])

        coinglass = {"orderbook": {"spot": datasets["coinglass.orderbook.spot"], "perpetual": datasets["coinglass.orderbook.perpetual"]},
                     "order_depth": {"spot": datasets["coinglass.order_depth.spot"], "perpetual": datasets["coinglass.order_depth.perpetual"]},
                     "large_trades": {"spot": datasets["coinglass.large_trades.spot"], "perpetual": datasets["coinglass.large_trades.perpetual"]},
                     "whale_orders": {"spot": datasets["coinglass.whale_orders.spot"], "perpetual": datasets["coinglass.whale_orders.perpetual"]},
                     "whale_activity": datasets["coinglass.whale_activity"], "market_history": datasets["coinglass.market_history"]}
        missing = [key for key in REQUIRED_DATASETS if datasets[key]["status"] == "unavailable"]
        invalid = [key for key in REQUIRED_DATASETS if datasets[key]["status"] == "invalid"]
        partial = [key for key in REQUIRED_DATASETS if datasets[key]["status"] == "partial"]
        quality_status = "invalid" if invalid else ("partial" if missing or partial else "ok")
        output = {"family": LIQUIDITY_MICROSTRUCTURE_FAMILY, "stage": "input", "mode": mode,
                  "reference_timestamp": int(reference_timestamp or time.time()), "execution_timestamp": int(execution_timestamp or time.time()),
                  "context": {"asset": "BTC", "exchange": "Binance", "spot_symbol": "BTCUSDT", "perpetual_symbol": "BTCUSDT",
                              "data_mode": data_mode, "is_demo": bool(is_demo)}, "providers": {"coinglass": coinglass},
                  "quality": {"status": quality_status, "providers": ["coinglass"], "datasets": list(datasets),
                              "required_datasets": list(REQUIRED_DATASETS), "optional_datasets": list(OPTIONAL_DATASETS),
                              "missing_required_datasets": missing, "invalid_required_datasets": invalid,
                              "partial_required_datasets": partial, "unavailable_required_datasets": missing,
                              "recovery_required": bool(invalid),
                              "warnings": sorted({warning for dataset in datasets.values() for warning in dataset["warnings"]}),
                              "errors": [error for dataset in datasets.values() for error in dataset["errors"]]}}
        if debug_raw:
            output["debug_raw"] = deepcopy(raw_bundle)
        json.dumps(output, ensure_ascii=False, allow_nan=False)
        return output


def run_liquidity_microstructure_input(*, fetcher: RawFetcher, requested_mode: str | None = None,
                                       existing_contract: Mapping[str, Any] | None = None,
                                       recovery_requests: Sequence[Any] | None = None, debug_raw: bool = False,
                                       reference_timestamp: int | None = None, execution_timestamp: int | None = None,
                                       data_mode: str = "live", is_demo: bool = False, **plan_arguments: Any) -> dict[str, Any]:
    mode = determine_liquidity_microstructure_input_mode(requested_mode=requested_mode, existing_contract=existing_contract,
                                                         recovery_requests=recovery_requests)
    raw = extract_liquidity_microstructure_raw(fetcher=fetcher, mode=mode, reference_timestamp=reference_timestamp,
                                                recovery_requests=recovery_requests, existing_contract=existing_contract,
                                                **plan_arguments)
    return LiquidityMicrostructureInputPreprocessor().preprocess(raw, existing_contract=existing_contract,
                                                                 reference_timestamp=reference_timestamp,
                                                                 execution_timestamp=execution_timestamp,
                                                                 data_mode=data_mode, is_demo=is_demo, debug_raw=debug_raw)
