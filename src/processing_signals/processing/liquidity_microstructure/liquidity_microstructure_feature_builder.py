"""Presentation-neutral packaging of already calculated Liquidity features."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any


def _profile(records: list[dict[str, Any]], mid_price: float | None) -> list[dict[str, Any]]:
    output, cumulative = [], {"buy": 0.0, "sell": 0.0}
    for row in sorted(records, key=lambda item: abs(item.get("distance_percent") or 0.0)):
        side = row["side"]
        cumulative[side] += float(row["quantity_base"])
        output.append({**deepcopy(row), "distance_percent": row.get("distance_percent") if mid_price else None,
            "cumulative_quantity_base": cumulative[side]})
    return output


def _unified_profiles(markets: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for market, payload in markets.items():
        by_timeframe = {}
        events = payload["large_trades"].get("large_trade_events", [])
        whale_orders = payload.get("whale_orders", {}).get("events", [])
        for timeframe, orderbook in payload["orderbook"]["timeframes"].items():
            current = orderbook.get("current") or {}
            mid = current.get("mid_price")
            resting = []
            for row in whale_orders:
                copied = deepcopy(row)
                copied["distance_percent"] = ((copied["price"] - mid) / mid * 100) if mid else None
                resting.append(copied)
            executed = [{"event_id": row["event_id"], "timestamp": row["timestamp"], "side": row["side"],
                "price": row["price"], "quantity_base": row["quantity_base"], "notional_quote": row["volume_usd"],
                "distance_percent": ((row["price"] - mid) / mid * 100) if mid else None,
                "exchange": row.get("exchange"), "source": row.get("provider_channel"),
                "trade_type": "executed_footprint_bin", "age_seconds": row.get("age_seconds")}
                for row in events]
            by_timeframe[timeframe] = {"whale_liquidity_profile": _profile(resting, mid),
                "whale_orders": sorted(resting, key=lambda row: row["notional_quote"], reverse=True),
                "executed_liquidity_profile": _profile(executed, mid),
                "calculation_history": {"orderbook_snapshot_records": 1 if current else 0,
                    "whale_order_records": len(whale_orders), "executed_trade_records": len(events),
                    "resolution": timeframe, "source": "coinglass", "fabricated_records": 0}}
        result[market] = by_timeframe
    return result


def build_liquidity_microstructure_features(*, markets: Mapping[str, Any], whale_activity: Mapping[str, Any],
                                             market_history: Mapping[str, Any], comparison: Mapping[str, Any]) -> dict[str, Any]:
    """Build only the derived feature payload that has a real consumer.

    ``markets``, ``whale_activity``, ``market_history`` and ``comparison`` already
    exist at Processing top level.  The legacy implementation deep-copied all of
    them again under ``features`` even though Classification/Screen only consume
    ``features.profiles``.  Keeping one owner removes a multi-megabyte duplicate
    tree and a large deepcopy/JSON-serialization cost.
    """
    del whale_activity, market_history, comparison
    return {"profiles": _unified_profiles(markets)}


class LiquidityMicrostructureFeatureBuilder:
    def build(self, **kwargs: Any) -> dict[str, Any]:
        return build_liquidity_microstructure_features(**kwargs)
