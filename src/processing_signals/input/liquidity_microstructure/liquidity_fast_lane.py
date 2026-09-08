"""Five-second table lane for Liquidity Microstructure.

This module intentionally updates only the execution tape derived from the two
CoinGlass footprint primitives.  Structural liquidity state (order book, whale
orders and the full analytical contract) remains owned by the ~5 s canonical
Liquidity worker.

Fast-lane invariants
--------------------
* poll cadence is service-owned (default ~1 s);
* Emulator requests still use the frozen 500-record transport contract;
* cumulative footprint snapshots are converted to *new positive deltas*;
* at most 10 new events are admitted per cycle across Spot + Perpetual;
* events are deduplicated by event_id;
* each market keeps a FIFO of 20 visible events;
* a watermark/snapshot is persisted so restart/repeated polls do not re-inject
  the same 500 historical records;
* the HMI receives only precomputed rows/charts -- it performs no market math.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time
from typing import Any

from processing_signals.main.atomic_replace import replace_with_retry

from .liquidity_microstructure_data_raw_preprocessing import (
    _normalize_footprint,
    _normalize_heatmap,
    _normalize_large_order,
)
from processing_signals.processing.liquidity_microstructure.liquidity_microstructure_math import process_order_book_levels

FAMILY = "liquidity_microstructure"
MARKETS = ("spot", "perpetual")
MAX_NEW_EVENTS_PER_CYCLE = 10
VISIBLE_FIFO_LIMIT = 20
OPERATIONS_WINDOW_SECONDS = 30 * 60
OPERATIONS_BUCKET_SECONDS = 60
STATE_SCHEMA = "trad_elatin.liquidity.fast-lane-state.v1"
FAST_LANE_CADENCE_SECONDS = 5.0

_ENDPOINTS = {
    "spot": ("spot_footprint", "/api/spot/volume/footprint-history"),
    "perpetual": ("futures_footprint", "/api/futures/volume/footprint-history"),
}

_SNAPSHOT_ENDPOINTS = {
    "spot": {
        "orderbook": ("spot_orderbook_heatmap", "/api/spot/orderbook/history", "BTCUSDT"),
        "whales": ("spot_large_limit_orders", "/api/spot/orderbook/large-limit-order", "BTCUSDT"),
    },
    "perpetual": {
        "orderbook": ("perpetual_orderbook_heatmap", "/api/futures/orderbook/history", "BTCUSDT"),
        "whales": ("perpetual_large_limit_orders", "/api/futures/orderbook/large-limit-order", "BTCUSDT"),
    },
}


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _envelope_records(payload: Any) -> list[Any]:
    if not isinstance(payload, Mapping):
        return []
    data: Any = payload.get("data")
    # Emulator/provider envelopes can nest one provider-level data node.
    if isinstance(data, Mapping) and "data" in data:
        data = data.get("data")
    return list(data) if isinstance(data, list) else []


def _request(market: str) -> dict[str, Any]:
    endpoint_id, path = _ENDPOINTS[market]
    return {
        "provider": "coinglass",
        "transport": "rest",
        "endpoint_id": endpoint_id,
        "path": path,
        "channel": None,
        "params": {
            "exchange": "Binance",
            "symbol": "BTCUSDT",
            "interval": "1m",
            # Acquisition forces 500 in Emulator mode.  Keeping 500 here makes
            # the intent explicit and is also valid for the current live plan.
            "limit": 500,
        },
        "dimensions": {
            "asset": "BTC",
            "exchange": "Binance",
            "market_type": market,
            "symbol": "BTCUSDT",
            "timeframe": "1m",
            "range_percent": None,
        },
    }


def _snapshot_request(market: str, kind: str) -> dict[str, Any]:
    endpoint_id, path, symbol = _SNAPSHOT_ENDPOINTS[market][kind]
    params: dict[str, Any] = {"exchange": "Binance", "symbol": symbol}
    if kind == "orderbook":
        params.update({"interval": "1m", "limit": 500})
    return {
        "provider": "coinglass", "transport": "rest", "endpoint_id": endpoint_id,
        "path": path, "channel": None, "params": params,
        "dimensions": {"asset": "BTC", "exchange": "Binance", "market_type": market,
                       "symbol": symbol, "timeframe": "1m", "range_percent": None},
    }


def _current_market_snapshot(fetcher: Callable[..., Any], market: str, observed_at: int) -> dict[str, Any]:
    order_request = _snapshot_request(market, "orderbook")
    order_records = _envelope_records(fetcher(**deepcopy(order_request)))
    orderbook: dict[str, Any] = {}
    if order_records:
        normalized, warning, _unit = _normalize_heatmap(order_records[-1], order_request)
        if warning is None and "bid_levels" in normalized and "ask_levels" in normalized:
            orderbook = process_order_book_levels(normalized["bid_levels"], normalized["ask_levels"])
            orderbook["timestamp"] = normalized["timestamp"]

    whale_request = _snapshot_request(market, "whales")
    whale_records = _envelope_records(fetcher(**deepcopy(whale_request)))
    whales: list[dict[str, Any]] = []
    mid = _finite(orderbook.get("mid_price"))
    for record in whale_records:
        if not isinstance(record, Mapping):
            continue
        try:
            row, _unit = _normalize_large_order(record, whale_request)
        except (TypeError, ValueError):
            continue
        price = _finite(row.get("price"))
        row["age_seconds"] = max(0, observed_at - int(row.get("first_seen_timestamp") or observed_at))
        row["distance_percent"] = None if price is None or mid in {None, 0.0} else (price - mid) / mid * 100.0
        whales.append(row)
    whales.sort(key=lambda row: (int(row.get("first_seen_timestamp") or 0), str(row.get("event_id") or "")))
    return {"orderbook": orderbook, "whales": whales[-20:]}


def _normalized_latest_snapshot(payload: Any, market: str) -> list[dict[str, Any]]:
    records = _envelope_records(payload)
    if not records:
        return []
    request = _request(market)
    normalized = _normalize_footprint(records[-1], request)
    rows = [deepcopy(row) for row, _unit in normalized]
    rows.sort(key=lambda row: (int(row.get("timestamp") or 0), str(row.get("event_id") or "")))
    return rows


def _snapshot_map(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        event_id = str(row.get("event_id") or "")
        if event_id:
            result[event_id] = deepcopy(dict(row))
    return result


def _event_identity(*, market: str, source_id: str, observed_at: int, quantity: float, notional: float) -> str:
    payload = f"fast|{market}|{source_id}|{observed_at}|{quantity:.12f}|{notional:.6f}"
    return hashlib.sha256(payload.encode()).hexdigest()


def delta_events(
    previous_snapshot: Mapping[str, Mapping[str, Any]],
    current_rows: Sequence[Mapping[str, Any]],
    *,
    market: str,
    observed_at: int,
) -> list[dict[str, Any]]:
    """Convert cumulative footprint changes into non-duplicated execution deltas.

    A brand-new source bucket is admitted as fresh volume.  Within the same
    bucket only positive increments are emitted, which prevents the 500-record
    history from being re-injected on every poll.
    """
    if market not in MARKETS:
        raise ValueError("invalid_fast_lane_market")
    candidates: list[dict[str, Any]] = []
    previous_latest_ts = max((int(row.get("timestamp") or 0) for row in previous_snapshot.values()), default=0)

    for raw in current_rows:
        row = dict(raw)
        source_id = str(row.get("event_id") or "")
        if not source_id:
            continue
        source_timestamp = int(row.get("timestamp") or 0)
        current_qty = max(0.0, float(_finite(row.get("quantity_base")) or 0.0))
        current_notional = max(0.0, float(_finite(row.get("volume_usd")) or 0.0))
        prior = previous_snapshot.get(source_id)

        if prior is None:
            # Only a genuinely newer bucket is an execution event. Missing ids
            # inside the same bucket can be a provider revision and are seeded
            # silently to avoid false replay.
            if source_timestamp <= previous_latest_ts and previous_latest_ts > 0:
                continue
            delta_qty = current_qty
            delta_notional = current_notional
        else:
            delta_qty = max(0.0, current_qty - float(_finite(prior.get("quantity_base")) or 0.0))
            delta_notional = max(0.0, current_notional - float(_finite(prior.get("volume_usd")) or 0.0))

        if delta_qty <= 0.0 and delta_notional <= 0.0:
            continue
        price = _finite(row.get("price"))
        if price is None or price <= 0:
            continue
        if delta_qty <= 0.0 and delta_notional > 0.0:
            delta_qty = delta_notional / price
        if delta_notional <= 0.0 and delta_qty > 0.0:
            delta_notional = delta_qty * price

        candidates.append({
            "event_id": _event_identity(
                market=market,
                source_id=source_id,
                observed_at=observed_at,
                quantity=delta_qty,
                notional=delta_notional,
            ),
            "timestamp": observed_at,
            "source_timestamp": source_timestamp,
            "side": str(row.get("side") or "").lower(),
            "price": price,
            "quantity_base": delta_qty,
            "notional_quote": delta_notional,
            "distance_percent": None,
            "exchange": str(row.get("exchange") or "Binance"),
            "source": str(row.get("provider_channel") or _ENDPOINTS[market][0]),
            "market_type": market,
            "trade_type": "executed_footprint_delta",
        })

    candidates.sort(key=lambda row: (-float(row.get("notional_quote") or 0.0), str(row.get("event_id") or "")))
    return candidates


def _fair_cap(candidates: Mapping[str, Sequence[dict[str, Any]]], limit: int = MAX_NEW_EVENTS_PER_CYCLE) -> dict[str, list[dict[str, Any]]]:
    """Cap globally while giving both market streams a fair share."""
    selected = {market: [] for market in MARKETS}
    queues = {market: list(candidates.get(market, [])) for market in MARKETS}
    while sum(len(rows) for rows in selected.values()) < limit and any(queues.values()):
        for market in MARKETS:
            if not queues[market]:
                continue
            selected[market].append(queues[market].pop(0))
            if sum(len(rows) for rows in selected.values()) >= limit:
                break
    return selected


def append_fifo(existing: Sequence[Mapping[str, Any]], incoming: Sequence[Mapping[str, Any]], *, limit: int = VISIBLE_FIFO_LIMIT) -> list[dict[str, Any]]:
    """Append unique events and evict the oldest rows first."""
    rows = [deepcopy(dict(row)) for row in existing if isinstance(row, Mapping)]
    seen = {str(row.get("event_id") or "") for row in rows if row.get("event_id")}
    for event in incoming:
        event_id = str(event.get("event_id") or "")
        if not event_id or event_id in seen:
            continue
        rows.append(deepcopy(dict(event)))
        seen.add(event_id)
    return rows[-max(1, int(limit)):]


def _mid_price(contract: Mapping[str, Any], market: str) -> float | None:
    view = contract.get("market_views", {}).get(market, {}) if isinstance(contract.get("market_views"), Mapping) else {}
    items = view.get("kpis", {}).get("items", []) if isinstance(view, Mapping) else []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, Mapping) and item.get("metric_id") == "mid_price":
            return _finite(item.get("value"))
    return None


def _with_distance(rows: Sequence[Mapping[str, Any]], mid_price: float | None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        copy = deepcopy(dict(row))
        price = _finite(copy.get("price"))
        copy["distance_percent"] = ((price - mid_price) / mid_price * 100.0) if price is not None and mid_price not in {None, 0.0} else None
        result.append(copy)
    return result


def _profile(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    cumulative = defaultdict(float)
    output: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: abs(float(item.get("distance_percent") or 0.0))):
        side = str(row.get("side") or "")
        cumulative[side] += float(row.get("quantity_base") or 0.0)
        item = deepcopy(dict(row))
        item["cumulative_quantity_base"] = cumulative[side]
        output.append(item)
    return output


def _operations(rows: Sequence[Mapping[str, Any]], *, window_seconds: int = OPERATIONS_WINDOW_SECONDS, bucket_seconds: int = OPERATIONS_BUCKET_SECONDS) -> tuple[list[int], list[float | None], list[float | None], list[float | None]]:
    timestamps = [int(row.get("timestamp") or 0) for row in rows if int(row.get("timestamp") or 0) > 0]
    if not timestamps:
        return [], [], [], []

    end_ts = max(timestamps)
    start_ts = max(min(timestamps), end_ts - max(int(window_seconds), int(bucket_seconds)) + int(bucket_seconds))
    aligned_start = (start_ts // int(bucket_seconds)) * int(bucket_seconds)
    aligned_end = (end_ts // int(bucket_seconds)) * int(bucket_seconds)

    buckets: dict[int, dict[str, float]] = {
        ts: {"buy": 0.0, "sell": 0.0, "count": 0.0}
        for ts in range(aligned_start, aligned_end + int(bucket_seconds), int(bucket_seconds))
    }
    for row in rows:
        timestamp = int(row.get("timestamp") or 0)
        if timestamp < aligned_start or timestamp > end_ts:
            continue
        bucket_ts = (timestamp // int(bucket_seconds)) * int(bucket_seconds)
        bucket = buckets.setdefault(bucket_ts, {"buy": 0.0, "sell": 0.0, "count": 0.0})
        side = str(row.get("side") or "")
        if side in {"buy", "sell"}:
            bucket[side] += float(row.get("notional_quote") or 0.0)
            bucket["count"] += 1.0

    out_timestamps = sorted(buckets)
    buy = [buckets[ts]["buy"] for ts in out_timestamps]
    sell = [buckets[ts]["sell"] for ts in out_timestamps]
    net = [None if buckets[ts]["count"] <= 0 else (buckets[ts]["buy"] - buckets[ts]["sell"]) for ts in out_timestamps]
    return out_timestamps, buy, sell, net


def _patch_market_view(view: dict[str, Any], tape: Sequence[Mapping[str, Any]], *, contract: Mapping[str, Any], market: str, observed_at: int) -> None:
    mid = _mid_price(contract, market)
    rows_chronological = _with_distance(tape, mid)
    rows_display = list(reversed(rows_chronological))

    tables = view.setdefault("tables", {})
    table = tables.setdefault("large_trades", {})
    table.update({
        "status": "available" if rows_display else "unavailable",
        "reason": None if rows_display else "fast_lane_no_events_yet",
        "columns": ["timestamp", "side", "price", "quantity_base", "notional_quote", "distance_percent"],
        "display_columns": [
            {"field": "timestamp", "label": "Time", "format": "time_hms"},
            {"field": "side", "label": "Side"},
            {"field": "price", "label": "Price (USDT)", "format": "price"},
            {"field": "quantity_base", "label": "Size (BTC)", "format": "base_quantity"},
            {"field": "notional_quote", "label": "Notional (USD)", "format": "compact_currency"},
            {"field": "distance_percent", "label": "Distance", "format": "signed_percent"},
        ],
        "rows": rows_display,
        "summary": {
            "buy": sum(float(row.get("notional_quote") or 0.0) for row in rows_chronological if row.get("side") == "buy"),
            "sell": sum(float(row.get("notional_quote") or 0.0) for row in rows_chronological if row.get("side") == "sell"),
            "net_flow_usd": sum((1.0 if row.get("side") == "buy" else -1.0) * float(row.get("notional_quote") or 0.0) for row in rows_chronological),
            "window": "fast_fifo_20",
            "status": "available" if rows_display else "unavailable",
            "reason": None if rows_display else "fast_lane_no_events_yet",
        },
        "metadata": {
            **(table.get("metadata") if isinstance(table.get("metadata"), Mapping) else {}),
            "events_available": len(rows_chronological),
            "events_returned": len(rows_display),
            "events_truncated": False,
            "display_limit": VISIBLE_FIFO_LIMIT,
            "fifo_limit": VISIBLE_FIFO_LIMIT,
            "max_new_events_per_cycle": MAX_NEW_EVENTS_PER_CYCLE,
            "fast_lane": True,
            "fast_lane_data_as_of": observed_at,
            "required_row_fields": ["event_id", "timestamp", "side", "price", "quantity_base", "notional_quote", "distance_percent"],
            "scroll_if_more_rows": False,
            "records_available": len(rows_chronological),
        },
        "data_as_of": observed_at,
    })

    charts = view.setdefault("charts", {})
    profile_chart = charts.setdefault("executed_liquidity_profile", {})
    profile_rows = _profile(rows_chronological)
    profile_chart.update({
        "status": "available" if profile_rows else "unavailable",
        "reason": None if profile_rows else "fast_lane_no_events_yet",
        "records": profile_rows,
        "data_as_of": observed_at,
    })

    operations = charts.setdefault("executed_operations", {})
    timestamps, buy, sell, net = _operations(rows_chronological)
    operations.update({
        "status": "available" if timestamps else "unavailable",
        "reason": None if timestamps else "fast_lane_no_events_yet",
        "timestamps": timestamps,
        "buy_executed": buy,
        "sell_executed": sell,
        "net_pressure": net,
        "series": ["buy_executed", "sell_executed", "net_pressure"],
        "data_as_of": observed_at if timestamps else None,
        "metadata": {
            **(operations.get("metadata") if isinstance(operations.get("metadata"), Mapping) else {}),
            "fast_lane": True,
            "fifo_source": "tables.large_trades.rows",
            "max_new_events_per_cycle": MAX_NEW_EVENTS_PER_CYCLE,
        },
    })


def _patch_snapshot_tables(view: dict[str, Any], snapshot: Mapping[str, Any], *, observed_at: int) -> None:
    tables = view.setdefault("tables", {})
    charts = view.setdefault("charts", {})
    orderbook = snapshot.get("orderbook")
    if isinstance(orderbook, Mapping) and orderbook.get("status") == "available":
        bids = [{"side": "bid", **dict(row)} for row in orderbook.get("bid_levels", [])]
        asks = [{"side": "ask", **dict(row)} for row in orderbook.get("ask_levels", [])]
        table = tables.setdefault("orderbook_snapshot", {})
        table.update({
            "status": "available", "reason": None, "bids": bids, "asks": asks,
            "summary": deepcopy(orderbook.get("bands", {}).get("full_visible_book", {})),
            "data_as_of": observed_at,
            "metadata": {**(table.get("metadata") if isinstance(table.get("metadata"), Mapping) else {}),
                         "fast_lane": True, "fast_lane_data_as_of": observed_at,
                         "rows_available": {"bid": len(bids), "ask": len(asks)},
                         "rows_returned": {"bid": len(bids), "ask": len(asks)}},
        })
        depth = charts.setdefault("order_depth", {})
        depth.update({"status": "available", "reason": None, "records": [*bids, *asks],
                      "data_as_of": observed_at,
                      "metadata": {**(depth.get("metadata") if isinstance(depth.get("metadata"), Mapping) else {}),
                                   "fast_lane": True}})

    whales = snapshot.get("whales")
    if isinstance(whales, list):
        rows = [deepcopy(row) for row in reversed(whales[-20:]) if isinstance(row, Mapping)]
        table = tables.setdefault("whale_orders", {})
        table.update({
            "status": "available" if rows else "unavailable",
            "reason": None if rows else "fast_lane_no_whale_orders",
            "rows": rows, "data_as_of": observed_at,
            "metadata": {**(table.get("metadata") if isinstance(table.get("metadata"), Mapping) else {}),
                         "fast_lane": True, "fast_lane_data_as_of": observed_at,
                         "events_available": len(rows), "events_returned": len(rows)},
        })
        profile = charts.setdefault("whale_liquidity_profile", {})
        profile.update({"status": "available" if rows else "unavailable",
                        "reason": None if rows else "fast_lane_no_whale_orders",
                        "records": _profile(list(reversed(rows))), "data_as_of": observed_at,
                        "metadata": {**(profile.get("metadata") if isinstance(profile.get("metadata"), Mapping) else {}),
                                     "fast_lane": True}})


def patch_screen_contract(
    contract: Mapping[str, Any], tapes: Mapping[str, Sequence[Mapping[str, Any]]], *,
    observed_at: int, snapshots: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    out = deepcopy(dict(contract))
    views = out.get("market_views")
    if not isinstance(views, dict):
        return out
    for market in MARKETS:
        view = views.get(market)
        if isinstance(view, dict):
            _patch_market_view(view, tapes.get(market, []), contract=out, market=market, observed_at=observed_at)
            if snapshots is not None and isinstance(snapshots.get(market), Mapping):
                _patch_snapshot_tables(view, snapshots[market], observed_at=observed_at)

    selected = str(out.get("context", {}).get("selected_market") or out.get("selectors", {}).get("market", {}).get("selected") or "perpetual")
    selected_view = views.get(selected)
    if isinstance(selected_view, Mapping):
        for section in ("tables", "charts"):
            if section in selected_view:
                out[section] = deepcopy(selected_view[section])

    context = out.setdefault("context", {})
    if isinstance(context, dict):
        context["data_as_of"] = max(int(context.get("data_as_of") or 0), observed_at)
        context["fast_lane_data_as_of"] = observed_at
    quality = out.setdefault("quality", {})
    if isinstance(quality, dict):
        extensions = quality.setdefault("extensions", {})
        if isinstance(extensions, dict):
            extensions["liquidity_tables_fast_lane_v1"] = {
                "status": "available",
                "cadence_seconds": FAST_LANE_CADENCE_SECONDS,
                "max_new_events_per_cycle": MAX_NEW_EVENTS_PER_CYCLE,
                "fifo_visible_events": VISIBLE_FIFO_LIMIT,
                "watermark_persisted": True,
                "duplicate_events_allowed": False,
                "transport_records_per_request": 500,
            "operations_window_seconds": OPERATIONS_WINDOW_SECONDS,
            "operations_bucket_seconds": OPERATIONS_BUCKET_SECONDS,
                "structural_liquidity_cadence_seconds": None,
                "data_as_of": observed_at,
                "surfaces": ["orderbook_snapshot", "whale_orders", "large_trades"],
            }
    return out


def _seed_tape_from_contract(contract: Mapping[str, Any], market: str) -> list[dict[str, Any]]:
    view = contract.get("market_views", {}).get(market, {}) if isinstance(contract.get("market_views"), Mapping) else {}
    rows = view.get("tables", {}).get("large_trades", {}).get("rows", []) if isinstance(view, Mapping) else []
    if not isinstance(rows, list):
        return []
    # Screen rows are newest-first. Convert to FIFO chronological order.
    clean = [deepcopy(row) for row in rows if isinstance(row, Mapping)]
    clean.reverse()
    return append_fifo([], clean, limit=VISIBLE_FIFO_LIMIT)


def _empty_state(*, source_mode: str) -> dict[str, Any]:
    return {
        "schema": STATE_SCHEMA,
        "source_mode": source_mode,
        "updated_at": None,
        "markets": {
            market: {"watermark": None, "snapshot": {}, "event_tape": []}
            for market in MARKETS
        },
    }


def _state(state_path: Path, *, source_mode: str, screen_contract: Mapping[str, Any]) -> dict[str, Any]:
    state = _read_json(state_path)
    if state.get("schema") != STATE_SCHEMA or state.get("source_mode") != source_mode:
        state = _empty_state(source_mode=source_mode)
    markets = state.setdefault("markets", {})
    for market in MARKETS:
        node = markets.setdefault(market, {"watermark": None, "snapshot": {}, "event_tape": []})
        if not isinstance(node.get("snapshot"), dict):
            node["snapshot"] = {}
        if not isinstance(node.get("event_tape"), list) or not node["event_tape"]:
            node["event_tape"] = _seed_tape_from_contract(screen_contract, market)
    return state


def run_liquidity_fast_lane_cycle(
    *,
    fetcher: Callable[..., Any],
    screen_contract_path: str | Path,
    state_path: str | Path,
    source_mode: str,
    observed_at: int | None = None,
) -> dict[str, Any]:
    """Execute one fast poll and atomically patch the existing Screen contract."""
    now = int(observed_at or time.time())
    screen_path = Path(screen_contract_path)
    runtime_state_path = Path(state_path)
    contract = _read_json(screen_path)
    if not contract:
        return {"status": "waiting_for_structural_contract", "new_events": 0, "patched": False}

    state = _state(runtime_state_path, source_mode=source_mode, screen_contract=contract)
    candidates: dict[str, list[dict[str, Any]]] = {market: [] for market in MARKETS}
    current_snapshots: dict[str, dict[str, dict[str, Any]]] = {}
    market_snapshots: dict[str, dict[str, Any]] = {}

    for market in MARKETS:
        request = _request(market)
        payload = fetcher(**deepcopy(request))
        current_rows = _normalized_latest_snapshot(payload, market)
        current_snapshot = _snapshot_map(current_rows)
        current_snapshots[market] = current_snapshot
        previous_snapshot = state["markets"][market].get("snapshot", {})
        # First observation is a watermark seed only. Never replay the current
        # 500-record history into the visible tape on startup/restart.
        if previous_snapshot:
            candidates[market] = delta_events(previous_snapshot, current_rows, market=market, observed_at=now)
        market_snapshots[market] = _current_market_snapshot(fetcher, market, now)

    admitted = _fair_cap(candidates, MAX_NEW_EVENTS_PER_CYCLE)
    admitted_count = 0
    for market in MARKETS:
        node = state["markets"][market]
        node["snapshot"] = current_snapshots[market]
        source_ts = max((int(row.get("timestamp") or 0) for row in current_snapshots[market].values()), default=0)
        node["watermark"] = {
            "source_timestamp": source_ts or None,
            "observed_at": now,
            "snapshot_fingerprint": hashlib.sha256(
                json.dumps(current_snapshots[market], sort_keys=True, separators=(",", ":"), default=str).encode()
            ).hexdigest()[:24],
        }
        node["event_tape"] = append_fifo(node.get("event_tape", []), admitted[market], limit=VISIBLE_FIFO_LIMIT)
        admitted_count += len(admitted[market])

    state["updated_at"] = now
    state["market_snapshots"] = market_snapshots
    _atomic_json(runtime_state_path, state)

    patched = patch_screen_contract(
        contract,
        {market: state["markets"][market]["event_tape"] for market in MARKETS},
        observed_at=now,
        snapshots=market_snapshots,
    )
    _atomic_json(screen_path, patched)
    return {"status": "updated", "new_events": admitted_count, "patched": True, "state": state}


__all__ = [
    "MAX_NEW_EVENTS_PER_CYCLE",
    "OPERATIONS_BUCKET_SECONDS",
    "OPERATIONS_WINDOW_SECONDS",
    "VISIBLE_FIFO_LIMIT",
    "append_fifo",
    "delta_events",
    "patch_screen_contract",
    "run_liquidity_fast_lane_cycle",
]
