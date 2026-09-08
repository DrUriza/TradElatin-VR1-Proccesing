"""Screen-contract assembler for Liquidity Microstructure v0.1."""

# ruff: noqa: E702, E731

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime
import json
import math
from typing import Any


FAMILY = SCREEN_ID = "liquidity_microstructure"
SCREEN_SCHEMA = "trad_elatin.liquidity_microstructure.screen.v1"
SCREEN_VERSION = "1.5.0"
SCREEN_ROUTE = "/liquidity"
SCREEN_TITLE = "LIQUIDITY MICROSTRUCTURE"
SCREEN_SUBTITLE = "Order-book depth, whale orders, large trades & liquidity context"
STAGE = "screen_contract"
MARKETS = ("spot", "perpetual")
TIMEFRAMES = ("1m", "5m", "15m", "4h")
DEFAULT_MARKET = "perpetual"
DEFAULT_TIMEFRAME = "1m"
DISPLAY_DEPTH_BASIS = "base_quantity"
REFERENCE_DEPTH_RANGE_PERCENT = 10
DISPLAY_POINT_LIMIT = 220
ORDERBOOK_TABLE_LIMIT = 50
LARGE_TRADE_TABLE_LIMIT = 20
KPI_IDS = ("bid_depth", "ask_depth", "spread", "liquidity_imbalance", "mid_price", "impact_1_btc")
CHART_IDS = ("order_depth", "whale_liquidity_profile", "executed_liquidity_profile", "executed_operations", "whale_activity", "market_history")
TABLE_IDS = ("orderbook_snapshot", "whale_orders", "large_trades")
WIDGET_IDS = ("observed_liquidity", "large_trade_pressure", "whale_activity_state", "market_context", "spot_perpetual_comparison", "source_status")
DRILLDOWN_IDS = ("orderbook_details", "market_impact_details", "large_trades_details", "whale_activity_details", "market_history_details", "cross_market_details")
LIMITATIONS = ("coinglass_only", "no_glassnode", "no_cryptoquant", "range_10_is_not_full_book",
               "observed_conditions_not_global_absolute_liquidity", "provider_is_coinglass_exchange_is_binance",
               "whale_orders_from_large_limit_order_endpoint",
               "executed_liquidity_is_footprint_price_bin_aggregation_not_individual_trade_tape")


def _number(value: Any, *, positive: bool = False, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or (positive and value <= 0):
        raise ValueError("invalid_numeric_argument")


def _iso(value: Any) -> None:
    if not isinstance(value, str):
        raise ValueError("invalid_runtime_timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("runtime_timestamp_timezone_required")


def _validate_runtime(runtime: Mapping[str, Any]) -> None:
    if not isinstance(runtime, Mapping):
        raise ValueError("runtime_context_must_be_mapping")
    required = {"data_mode", "is_demo", "generated_at", "updated_at", "connection_status", "cache_status", "latency_ms",
                "refresh_interval_seconds", "cache_ttl_seconds"}
    if not required.issubset(runtime):
        raise ValueError("runtime_context_missing_fields")
    if runtime["data_mode"] not in {"live", "synthetic"} or not isinstance(runtime["is_demo"], bool):
        raise ValueError("invalid_runtime_mode")
    if (runtime["data_mode"] == "synthetic") != runtime["is_demo"]:
        raise ValueError("runtime_mode_demo_mismatch")
    if runtime["connection_status"] not in {"connected", "degraded", "disconnected", "not_reported"}:
        raise ValueError("invalid_connection_status")
    if runtime["cache_status"] not in {"hit", "miss", "stale", "not_reported"}:
        raise ValueError("invalid_cache_status")
    _iso(runtime["generated_at"]); _iso(runtime["updated_at"])
    _number(runtime["latency_ms"], nullable=True)
    if runtime["latency_ms"] is not None and runtime["latency_ms"] < 0:
        raise ValueError("invalid_latency")
    _number(runtime["refresh_interval_seconds"], positive=True, nullable=True)
    _number(runtime["cache_ttl_seconds"], positive=True, nullable=True)


def _validate_json(value: Any) -> None:
    stack = [value]
    seen_containers: set[int] = set()
    while stack:
        current = stack.pop()
        if type(current) is dict:
            identity = id(current)
            if identity in seen_containers:
                continue
            seen_containers.add(identity)
            if any(type(key) is not str for key in current):
                raise ValueError("non_string_json_key")
            stack.extend(current.values())
        elif type(current) in (list, tuple):
            identity = id(current)
            if identity in seen_containers:
                continue
            seen_containers.add(identity)
            stack.extend(current)
        elif type(current) is float and (
            not math.isfinite(current)
            or (current == 0 and math.copysign(1, current) < 0)
        ):
            raise ValueError("invalid_json_number")
        elif current is not None and type(current) not in (str, int, bool, float):
            raise ValueError("invalid_json_value")


def validate_liquidity_microstructure_builder_inputs(bundle: Mapping[str, Any], *, runtime_context: Mapping[str, Any],
                                                      selected_market: str = DEFAULT_MARKET, selected_timeframe: str = DEFAULT_TIMEFRAME,
                                                      display_point_limit: int = DISPLAY_POINT_LIMIT, orderbook_table_limit: int = ORDERBOOK_TABLE_LIMIT,
                                                      large_trade_table_limit: int = LARGE_TRADE_TABLE_LIMIT) -> None:
    if not isinstance(bundle, Mapping) or not isinstance(bundle.get("processing"), Mapping) or not isinstance(bundle.get("classification"), Mapping):
        raise ValueError("invalid_builder_bundle")
    if selected_market not in MARKETS or selected_timeframe not in TIMEFRAMES:
        raise ValueError("invalid_selector")
    for limit in (display_point_limit, orderbook_table_limit, large_trade_table_limit):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("invalid_display_limit")
    _validate_runtime(runtime_context)
    p, c = bundle["processing"], bundle["classification"]
    if p.get("family") != FAMILY or p.get("stage") != "processing" or p.get("configuration", {}).get("version") != "0.1":
        raise ValueError("invalid_processing_contract")
    if c.get("family") != FAMILY or c.get("stage") != "classification" or c.get("classification_version") != "0.1" or c.get("classification_rule_version") != "liquidity_microstructure.rules.v0.1":
        raise ValueError("invalid_classification_contract")
    pairs = ((p.get("mode"), c.get("mode")), (p.get("reference_timestamp"), c.get("reference_timestamp")),
             (p.get("execution_timestamp"), c.get("source_execution_timestamp")), (p.get("context"), c.get("context")))
    if any(left != right for left, right in pairs):
        raise ValueError("upstream_contract_mismatch")
    for source in (p, c):
        if set(source.get("markets", {})) != set(MARKETS):
            raise ValueError("upstream_contract_mismatch")
    for key in ("data_mode", "is_demo"):
        if key in p.get("context", {}) and p["context"][key] != runtime_context[key]:
            raise ValueError("upstream_contract_mismatch")
    _validate_json(bundle); _validate_json(runtime_context)


def _component(component_id: str, title: str, status: str, source_paths: list[str], **extra: Any) -> dict[str, Any]:
    result = {"status": status, "reason": None if status == "available" else "source_not_available", "source_paths": source_paths,
              "warnings": [], "errors": []}
    result.update(extra)
    result[next(key for key in ("chart_id", "table_id", "widget_id", "drilldown_id") if key in extra)] = component_id
    result["title"] = title
    return result


def _fmt(value: Any, kind: str) -> str:
    if value is None:
        return "--"
    return {"btc": f"{value:,.2f} BTC", "bps": f"{value:,.1f} bps", "percent": f"{value:+.1f}%", "usd": f"${value:,.2f}"}[kind]


def _kpi(metric_id: str, label: str, value: Any, unit: str, kind: str, status: str, source_paths: list[str], timestamp: Any,
         color: str = "neutral", metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
    usable = status in {"available", "partial"} and value is not None
    return {"metric_id": metric_id, "label": label, "value": value if usable else None, "display_value": _fmt(value, kind) if usable else "--", "unit": unit,
            "status": status, "reason": None if usable else "source_not_available", "color_token": color, "source_paths": source_paths,
            "source_timestamp": timestamp, "metadata": deepcopy(dict(metadata or {}))}


def _depth_chart(chart_id: str, title: str, current: Mapping[str, Any], limit: int, band: tuple[float, float] | None, path: str) -> dict[str, Any]:
    def selected(side: str) -> tuple[list[dict[str, Any]], int]:
        values = list(current.get(f"{side}_levels", []))
        if band is not None:
            low, high = band
            values = [row for row in values if (row.get("distance_percent") is not None and low < row["distance_percent"] <= high)]
        # Order-book rows are flat JSON records.  Copy only each row that is
        # decorated below instead of recursively cloning the entire slice.
        return [dict(row) for row in values[:limit]], len(values)
    bids, bid_count = selected("bid"); asks, ask_count = selected("ask")
    records = [{"side": "bid", **row} for row in bids] + [{"side": "ask", **row} for row in asks]
    status = current.get("status", "unavailable")
    return _component(chart_id, title, status, [path], chart_id=chart_id, chart_type="mirrored_cumulative_depth",
                      selector_behavior="selected_market_and_timeframe", data_as_of=current.get("timestamp"), series=["Bids", "Asks"], records=records,
                      metadata={"scope": "single_exchange_visible_orderbook", "provider": "coinglass", "exchange": "Binance", "not_full_market_book": True,
                                "x_axis": {"rendering": "mirrored_by_side", "field": "distance_percent"}, "basis": "cumulative_quantity_base", "unit": "BTC",
                                "bid_levels_available": bid_count, "bid_levels_returned": len(bids), "ask_levels_available": ask_count,
                                "ask_levels_returned": len(asks), "display_point_limit": limit, "depth_truncated": bid_count > limit or ask_count > limit,
                                "cumulative_origin": "mid_price", "band_filter": None if band is None else {"min_exclusive": band[0], "max_inclusive": band[1]}})


def _table(table_id: str, title: str, chart: Mapping[str, Any], summary: Mapping[str, Any], limit: int, path: str) -> dict[str, Any]:
    bids = [dict(row) for row in chart["records"] if row["side"] == "bid"][:limit]
    asks = [dict(row) for row in chart["records"] if row["side"] == "ask"][:limit]
    available = {"bid": sum(row["side"] == "bid" for row in chart["records"]), "ask": sum(row["side"] == "ask" for row in chart["records"])}
    return _component(table_id, title, chart["status"], [path], table_id=table_id,
                      columns=["price", "quantity_base", "cumulative_quantity_base", "notional_quote", "distance_percent"], bids=bids, asks=asks,
                      summary=deepcopy(summary), metadata={"rows_available": available, "rows_returned": {"bid": len(bids), "ask": len(asks)},
                                                          "rows_truncated": available["bid"] > limit or available["ask"] > limit, "display_limit": limit})


def _fallback(mode: str | None, runtime: Mapping[str, Any] | None, error: str) -> dict[str, Any]:
    invalid = lambda key, title: {key: title, "title": title, "status": "invalid", "reason": "invalid_upstream_contract", "source_paths": [], "warnings": [], "errors": [error]}
    return {"schema": {"id": SCREEN_SCHEMA, "version": SCREEN_VERSION}, "screen": {"id": SCREEN_ID, "family": FAMILY, "route": SCREEN_ROUTE, "title": SCREEN_TITLE, "subtitle": SCREEN_SUBTITLE},
            "stage": STAGE, "mode": mode if mode in {"bootstrap", "incremental", "recovery"} else "bootstrap", "context": {}, "badges": [],
            "selectors": {"market": {}, "timeframe": {}}, "operational_status": {"status": "invalid", "reason": "invalid_upstream_contract"},
            "kpis": {"items": [_kpi(key, key.replace("_", " ").title(), None, "", "bps", "invalid", [], None) for key in KPI_IDS]},
            "charts": {key: invalid("chart_id", key) | {"chart_id": key, "series": [], "records": []} for key in CHART_IDS},
            "tables": {key: invalid("table_id", key) | {"table_id": key, "columns": [], "bids": [], "asks": [], "rows": [], "summary": {}} for key in TABLE_IDS},
            "widgets": {key: invalid("widget_id", key) | {"widget_id": key, "current": None, "items": []} for key in WIDGET_IDS},
            "drilldowns": {key: invalid("drilldown_id", key) | {"drilldown_id": key, "enabled": False, "current": None, "details": {}} for key in DRILLDOWN_IDS},
            "availability": {"required": {}, "optional": {}, "summary": {"required_available": 0, "required_total": 22, "optional_available": 0, "optional_total": 7}},
            "quality": {"status": "invalid", "contract_complete": True, "data_complete": False, "processing_status": "invalid", "classification_status": "invalid",
                        "availability": {}, "missing_required_components": list(KPI_IDS + CHART_IDS + TABLE_IDS + WIDGET_IDS), "partial_components": [],
                        "unavailable_components": [], "invalid_components": list(KPI_IDS + CHART_IDS + TABLE_IDS + WIDGET_IDS), "warnings": [], "errors": [error], "data_as_of": None}}


def build_liquidity_microstructure_screen_contract(bundle: Mapping[str, Any], *, runtime_context: Mapping[str, Any], selected_market: str = DEFAULT_MARKET,
                                                    selected_timeframe: str = DEFAULT_TIMEFRAME, display_point_limit: int = DISPLAY_POINT_LIMIT,
                                                    orderbook_table_limit: int = ORDERBOOK_TABLE_LIMIT,
                                                    large_trade_table_limit: int = LARGE_TRADE_TABLE_LIMIT) -> dict[str, Any]:
    processing = bundle.get("processing") if isinstance(bundle, Mapping) else None
    classification = bundle.get("classification") if isinstance(bundle, Mapping) else None
    # Invalid upstream data is still a valid operational outcome. Emit the
    # canonical fallback Screen contract so startup can publish all 8 families
    # even when Liquidity RAW is temporarily unavailable.
    if (isinstance(processing, Mapping) and isinstance(classification, Mapping)
            and (processing.get("quality", {}).get("status") == "invalid"
                 or classification.get("quality", {}).get("status") == "invalid")):
        return _fallback(processing.get("mode"), runtime_context, "invalid_upstream_contract")
    validate_liquidity_microstructure_builder_inputs(bundle, runtime_context=runtime_context, selected_market=selected_market,
                                                      selected_timeframe=selected_timeframe, display_point_limit=display_point_limit,
                                                      orderbook_table_limit=orderbook_table_limit, large_trade_table_limit=large_trade_table_limit)
    # The builder treats both upstream contracts as read-only and explicitly
    # copies every projected mutable branch below.  Avoid cloning the complete
    # Processing and Classification trees merely to read from them.
    p, c, runtime = bundle["processing"], bundle["classification"], dict(runtime_context)
    market_path = f"markets.{selected_market}"; ob_path = f"{market_path}.orderbook.timeframes.{selected_timeframe}"
    depth_path = f"{market_path}.order_depth.timeframes.{selected_timeframe}"
    ob, cob = p["markets"][selected_market]["orderbook"]["timeframes"][selected_timeframe], c["markets"][selected_market]["orderbook"]["timeframes"][selected_timeframe]
    current = ob.get("current") or {}; depth = p["markets"][selected_market]["order_depth"]["timeframes"][selected_timeframe]
    reference = next((row for row in reversed(depth.get("direct_ranges", [])) if row.get("range_percent") == REFERENCE_DEPTH_RANGE_PERCENT and row.get("status") in {"available", "partial"}), None)
    provider_base = (reference or {}).get(DISPLAY_DEPTH_BASIS, {})
    visible_base = current.get("bands", {}).get("full_visible_book", {}).get("base_quantity", {}) if isinstance(current, Mapping) else {}
    timestamp = current.get("timestamp")
    bid_max_distance = max((float(row.get("distance_percent")) for row in current.get("bid_levels", []) if row.get("distance_percent") is not None), default=None)
    ask_max_distance = max((float(row.get("distance_percent")) for row in current.get("ask_levels", []) if row.get("distance_percent") is not None), default=None)
    depth_window_metadata = {
        "depth_window": "full_visible_orderbook",
        "max_bid_distance_percent": bid_max_distance,
        "max_ask_distance_percent": ask_max_distance,
        "provider_reference_depth_range_percent": REFERENCE_DEPTH_RANGE_PERCENT,
        "provider_reference_bid_depth": provider_base.get("bid"),
        "provider_reference_ask_depth": provider_base.get("ask"),
    }
    depth_class = c["markets"][selected_market]["order_depth"]["timeframes"][selected_timeframe].get("classification", {}).get("reference_balance") or {}
    ob_class = cob.get("classification", {}); impact = current.get("market_impact", {}); worst = impact.get("worst_side_impact_bps")
    filled = bool(impact.get("buy", {}).get("fully_filled")) and bool(impact.get("sell", {}).get("fully_filled"))
    kpis = [_kpi("bid_depth", "Bid Depth", visible_base.get("bid"), "BTC", "btc", visible_base.get("status", "unavailable"), [f"{ob_path}.current.bands.full_visible_book.base_quantity.bid"], timestamp, depth_class.get("display_color_token", "neutral"), depth_window_metadata),
            _kpi("ask_depth", "Ask Depth", visible_base.get("ask"), "BTC", "btc", visible_base.get("status", "unavailable"), [f"{ob_path}.current.bands.full_visible_book.base_quantity.ask"], timestamp, depth_class.get("display_color_token", "neutral"), depth_window_metadata),
            _kpi("spread", "Spread", current.get("spread_bps"), "bps", "bps", current.get("status", "unavailable"), [f"{ob_path}.current.spread_bps"], current.get("timestamp"), ob_class.get("spread_condition", {}).get("display_color_token", "neutral"), current),
            _kpi("liquidity_imbalance", "Liquidity Imbalance", visible_base.get("imbalance_percent"), "%", "percent", visible_base.get("status", "unavailable"), [f"{ob_path}.current.bands.full_visible_book.base_quantity.imbalance_percent"], timestamp, depth_class.get("display_color_token", "neutral"), depth_window_metadata | {"basis": DISPLAY_DEPTH_BASIS}),
            _kpi("mid_price", "Mid Price", current.get("mid_price"), "USD", "usd", current.get("status", "unavailable"), [f"{ob_path}.current.mid_price"], current.get("timestamp"), metadata={"best_bid": current.get("best_bid"), "best_ask": current.get("best_ask")}),
            _kpi("impact_1_btc", "Impact 1 BTC", worst if filled else None, "bps", "bps", impact.get("status", "unavailable") if filled else "partial", [f"{ob_path}.current.market_impact.worst_side_impact_bps"], current.get("timestamp"), ob_class.get("market_impact", {}).get("worst_side", {}).get("display_color_token", "neutral"), impact)]
    order_depth = _depth_chart("order_depth", "ORDER DEPTH", current, display_point_limit, None, ob_path)
    for row in order_depth["records"]:
        distance = row.get("distance_percent")
        row["band"] = "0_to_1" if distance is not None and distance <= 1 else "1_to_5" if distance is not None and distance <= 5 else "over_5"
    order_depth["metadata"].update(
        bands=["0_to_1", "1_to_5", "over_5"],
        provenance={"provider": "coinglass", "source_path": ob_path},
        depth_window="full_visible_orderbook",
        max_bid_distance_percent=bid_max_distance,
        max_ask_distance_percent=ask_max_distance,
        provider_reference_depth_range_percent=REFERENCE_DEPTH_RANGE_PERCENT,
    )
    charts = {"order_depth": order_depth}
    bands = current.get("bands", {})
    tables = {"orderbook_snapshot": _table("orderbook_snapshot", "ORDER BOOK SNAPSHOT", charts["order_depth"], bands.get("full_visible_book", {}), orderbook_table_limit, ob_path)}
    for side in ("bids", "asks"):
        for row in tables["orderbook_snapshot"][side]:
            distance = row.get("distance_percent")
            row["band"] = "0_to_1" if distance is not None and distance <= 1 else "1_to_5" if distance is not None and distance <= 5 else "over_5"
    tables["orderbook_snapshot"]["columns"].extend(["side", "band"])
    tables["orderbook_snapshot"]["metadata"]["provenance"] = {"provider": "coinglass", "source_path": ob_path}
    trades = p["markets"][selected_market]["large_trades"]; c_trades = c["markets"][selected_market]["large_trades"]
    events = sorted((dict(row) for row in trades.get("large_trade_events", []) if row.get("meets_configured_threshold") is True), key=lambda row: row["timestamp"], reverse=True)
    tables["large_trades"] = _component("large_trades", "LARGE TRADES — RECENT EVENTS", trades["status"], [f"{market_path}.large_trades.large_trade_events"], table_id="large_trades", columns=list(events[0]) if events else [], rows=events[:large_trade_table_limit], bids=[], asks=[], summary={}, metadata={"events_available": len(events), "events_returned": min(len(events), large_trade_table_limit), "events_truncated": len(events) > large_trade_table_limit, **deepcopy(trades.get("coverage", {}))})
    profiles = p["features"]["profiles"][selected_market][selected_timeframe]
    def profile_chart(identifier: str, title: str, records: list[dict[str, Any]], series: list[str], source_path: str) -> dict[str, Any]:
        return _component(identifier, title, "available" if records else "unavailable", [source_path], chart_id=identifier,
            chart_type="mirrored_cumulative_profile", selector_behavior="selected_market_and_timeframe",
            data_as_of=max((row["timestamp"] for row in records), default=None), series=series,
            records=[dict(row) for row in records[:display_point_limit]], metadata={"unit": "BTC", "profile_dynamic": True,
                "records_available": len(records), "records_returned": min(len(records), display_point_limit),
                "provenance": {"source_path": source_path}, "calculation_history": deepcopy(profiles["calculation_history"])})
    charts["whale_liquidity_profile"] = profile_chart("whale_liquidity_profile", "WHALE LIQUIDITY PROFILE", profiles["whale_liquidity_profile"], ["Buy Concentration", "Sell Concentration"], f"features.profiles.{selected_market}.{selected_timeframe}.whale_liquidity_profile")
    charts["executed_liquidity_profile"] = profile_chart("executed_liquidity_profile", "EXECUTED LIQUIDITY PROFILE", profiles["executed_liquidity_profile"], ["Buy Executed", "Sell Executed"], f"features.profiles.{selected_market}.{selected_timeframe}.executed_liquidity_profile")
    whale_orders = [dict(row) for row in profiles["whale_orders"][:large_trade_table_limit]]
    tables["whale_orders"] = _component("whale_orders", "WHALE ORDERS", "available" if whale_orders else "unavailable", [f"features.profiles.{selected_market}.{selected_timeframe}.whale_orders"], table_id="whale_orders", columns=list(whale_orders[0]) if whale_orders else [], display_columns=["timestamp", "side", "price", "quantity_base", "notional_quote", "exchange", "order_state"], rows=whale_orders, summary={}, metadata={"records_available": len(profiles["whale_orders"]), "records_returned": len(whale_orders), "cardinality": "dynamic", "provenance": {"source": "real_orderbook_snapshot"}})
    whale, c_whale = p["whale_activity"]["timeframes"][selected_timeframe], c["whale_activity"]["timeframes"][selected_timeframe]
    whale_records = list(whale.get("records", [])[-display_point_limit:])
    charts["whale_activity"] = _component("whale_activity", "WHALE ACTIVITY", whale["status"], [f"whale_activity.timeframes.{selected_timeframe}"], chart_id="whale_activity", chart_type="line", selector_behavior="fixed_perpetual_market_selected_timeframe", data_as_of=(whale.get("current") or {}).get("timestamp"), series=["whale_index_value"], records=whale_records, metadata={"scope": "perpetual", "statistics": deepcopy(whale.get("statistics", {})), "indicator": "derived_large_limit_order_activity"})
    history = p["market_history"]; history_records = list(history.get("records", [])[-display_point_limit:])
    charts["market_history"] = _component("market_history", "MARKET HISTORY", history["status"], ["market_history.records"], chart_id="market_history", chart_type="multi_series_line", selector_behavior="fixed_asset_daily_context", data_as_of=(history.get("current") or {}).get("timestamp"), series=["price", "market_cap", "circulating_supply"], records=history_records, metadata={"scope": "asset_level_daily", "changes": deepcopy(history.get("changes", {})), "optional": True, "cardinality": "dynamic"})
    charts["market_history"]["calculation_history"] = {"records_available": len(history.get("records", [])), "records_returned": len(history_records), "resolution": history.get("provenance", {}).get("interval"), "fabricated_records": 0}
    trade_atom = c_trades.get("classification", {}).get(selected_timeframe, {}); whale_atom = c_whale.get("classification", {})
    widgets = {"observed_liquidity": _component("observed_liquidity", "OBSERVED LIQUIDITY CONDITIONS", c["quality"]["status"] if c["quality"]["status"] != "ok" else "available", ["summary.observed_liquidity"], widget_id="observed_liquidity", current=deepcopy(c["markets"][selected_market]["summary"][selected_timeframe]), items=[], data_as_of=current.get("timestamp"), scope="observed_not_global_absolute_liquidity"),
               "large_trade_pressure": _component("large_trade_pressure", "LARGE TRADE PRESSURE", trade_atom.get("status", "unavailable"), [f"markets.{selected_market}.large_trades.classification.{selected_timeframe}"], widget_id="large_trade_pressure", current=deepcopy(trade_atom), items=[], data_as_of=trade_atom.get("source_timestamp")),
               "whale_activity_state": _component("whale_activity_state", "WHALE ACTIVITY STATE", whale_atom.get("status", "unavailable"), [f"whale_activity.timeframes.{selected_timeframe}.classification"], widget_id="whale_activity_state", current=deepcopy(whale_atom) | {"rolling_z_score_20": whale.get("statistics", {}).get("rolling_z_score_20"), "scope": "perpetual"}, items=[], data_as_of=(whale.get("current") or {}).get("timestamp")),
               "market_context": _component("market_context", "MARKET CONTEXT", history["status"], ["market_history", "classification.market_history"], widget_id="market_context", current=deepcopy(history.get("current")), items=[deepcopy(row) for row in c["market_history"].get("changes", {}).values()], data_as_of=(history.get("current") or {}).get("timestamp")),
               "spot_perpetual_comparison": _component("spot_perpetual_comparison", "SPOT / PERPETUAL COMPARISON", c["comparison"]["spot_perpetual"].get("status", "unavailable"), ["comparison.spot_perpetual"], widget_id="spot_perpetual_comparison", current=deepcopy(c["comparison"]["spot_perpetual"]), items=[], data_as_of=current.get("timestamp")),
               "source_status": _component("source_status", "SOURCE STATUS", "available", ["processing.quality", "classification.quality"], widget_id="source_status", current=None, data_as_of=max(filter(lambda value: isinstance(value, int), [current.get("timestamp"), timestamp, (history.get("current") or {}).get("timestamp")]), default=None), items=[{"provider_id": "coinglass", "label": "CoinGlass", "exchange": "Binance", "status": runtime["connection_status"]}, {"provider_id": "internal_processing", "label": "Internal Processing", "status": p["quality"]["status"]}, {"provider_id": "internal_classification", "label": "Internal Classification", "status": c["quality"]["status"]}])}
    data_as_of = widgets["source_status"]["data_as_of"]
    # These upstream trees are read-only for the rest of the build.  Reuse
    # them until final contract shaping/serialization instead of materializing
    # a second 50+ MB object graph solely for drilldown projection.
    drilldown_values = {
        "orderbook_details": current,
        "market_impact_details": impact,
        "large_trades_details": trades,
        "whale_activity_details": whale,
        "market_history_details": history,
        "cross_market_details": c["comparison"]["spot_perpetual"],
    }
    drilldown_sources = {
        "orderbook_details": f"processing.features.current_orderbooks.{selected_market}.{selected_timeframe}",
        "market_impact_details": f"processing.features.order_depth.{selected_market}.{selected_timeframe}",
        "large_trades_details": f"processing.features.large_trades.{selected_market}",
        "whale_activity_details": f"processing.features.whale_activity.timeframes.{selected_timeframe}",
        "market_history_details": "processing.features.market_history",
        "cross_market_details": "classification.comparison.spot_perpetual",
    }
    drilldowns = {
        key: _component(
            key, key.replace("_", " ").upper(), value.get("status", "available"),
            [drilldown_sources[key]], drilldown_id=key, enabled=True,
            current=value.get("current"), details=value,
        )
        for key, value in drilldown_values.items()
    }
    required_components = {
        **{f"kpis.{row['metric_id']}": row for row in kpis},
        **{f"charts.{key}": value for key, value in charts.items() if key != "market_history"},
        **{f"tables.{key}": value for key, value in tables.items()},
        **{f"widgets.{key}": value for key, value in widgets.items() if key not in {"spot_perpetual_comparison", "market_context"}},
    }
    optional_components = {
        "charts.market_history": charts["market_history"],
        "widgets.market_context": widgets["market_context"],
        "widgets.spot_perpetual_comparison": widgets["spot_perpetual_comparison"],
        **{f"drilldowns.{key}": value for key, value in drilldowns.items()},
    }
    entry = lambda value: {"status": value["status"], "reason": value.get("reason"), "source_paths": deepcopy(value.get("source_paths", []))}
    availability = {"required": {key: entry(value) for key, value in required_components.items()}, "optional": {key: entry(value) for key, value in optional_components.items()}}
    availability["summary"] = {"required_available": sum(value["status"] == "available" for value in required_components.values()), "required_total": len(required_components),
                               "optional_available": sum(value["status"] == "available" for value in optional_components.values()), "optional_total": len(optional_components)}
    statuses = {key: value["status"] for key, value in required_components.items()}; invalid = sorted(key for key, value in statuses.items() if value == "invalid")
    partial = sorted(key for key, value in statuses.items() if value == "partial"); unavailable = sorted(key for key, value in statuses.items() if value == "unavailable")
    quality_status = "invalid" if invalid or p["quality"]["status"] == "invalid" or c["quality"]["status"] == "invalid" else "partial" if partial or unavailable else "ok"
    badges = ([{"id": "SYNTHETIC", "color_token": "info"}] if runtime["is_demo"] else []) + ([{"id": "DEGRADED", "color_token": "warning"}] if quality_status == "partial" or runtime["connection_status"] == "degraded" else []) + ([{"id": "STALE", "color_token": "warning"}] if runtime["cache_status"] == "stale" else [])
    output = {"schema": {"id": SCREEN_SCHEMA, "version": SCREEN_VERSION}, "screen": {"id": SCREEN_ID, "family": FAMILY, "route": SCREEN_ROUTE, "title": SCREEN_TITLE, "subtitle": SCREEN_SUBTITLE}, "stage": STAGE, "mode": p["mode"],
              "context": {"asset": p["context"].get("asset"), "base_asset": "BTC", "quote_asset": "USDT", "spot_symbol": p["markets"]["spot"]["orderbook"]["timeframes"][selected_timeframe].get("current", {}).get("symbol"), "perpetual_symbol": p["markets"]["perpetual"]["orderbook"]["timeframes"][selected_timeframe].get("current", {}).get("symbol"), "selected_symbol": current.get("symbol"), "selected_market": selected_market, "selected_timeframe": selected_timeframe, "provider": {"id": "coinglass", "label": "CoinGlass"}, "exchange": "Binance", "reference_timestamp": p["reference_timestamp"], "processing_execution_timestamp": p["execution_timestamp"], "classification_execution_timestamp": c["execution_timestamp"], "data_as_of": data_as_of, **runtime, "display_depth_basis": DISPLAY_DEPTH_BASIS, "reference_depth_range_percent": 10, "market_impact_quantity_base": p["configuration"]["market_impact_quantity_base"], "calibration_status": c["configuration"]["calibration_status"], "calculation_history": "upstream_preserved", "presentation_policy": "select_filter_truncate_without_recalculation", "units": {"depth": "BTC", "spread": "bps", "impact": "bps"}, "limitations": list(LIMITATIONS)},
              "badges": badges, "selectors": {"market": {"selector_id": "liquidity_market", "selected": selected_market, "options": list(MARKETS), "behavior": "contract_rebuild_required"}, "timeframe": {"selector_id": "liquidity_timeframe", "selected": selected_timeframe, "options": list(TIMEFRAMES), "behavior": "contract_rebuild_required"}},
              "operational_status": {"status": "invalid" if quality_status == "invalid" else "partial" if quality_status == "partial" or runtime["connection_status"] in {"degraded", "disconnected"} else "available", "provider": "coinglass", "exchange": "Binance", **runtime, "data_as_of": data_as_of, "quality_status": quality_status, "reason": None},
              "kpis": {"items": kpis}, "charts": charts, "tables": tables, "widgets": widgets, "drilldowns": drilldowns,
              "history_contract": {"calculation_history": deepcopy(profiles["calculation_history"]), "cardinality": "dynamic",
                  "market_history": {"status": history["status"], "optional": True,
                      "records_available": len(history.get("records", [])), "fabricated_records": 0}, "hmi_recalculation": False},
              "availability": availability,
              "quality": {"status": quality_status, "contract_complete": True, "data_complete": not partial and not unavailable, "processing_status": p["quality"]["status"], "classification_status": c["quality"]["status"], "availability": deepcopy(availability["summary"]), "missing_required_components": unavailable, "partial_components": partial, "unavailable_components": unavailable, "invalid_components": invalid, "warnings": [], "errors": [], "data_as_of": data_as_of}}
    output = align_liquidity_microstructure_to_sp_v1_4(
        output, p, c, runtime, _copy_candidate=False
    )
    _validate_json(output)
    return output


class LiquidityMicrostructureContractBuilder:
    def __init__(self, *, selected_market: str = DEFAULT_MARKET, selected_timeframe: str = DEFAULT_TIMEFRAME,
                 display_point_limit: int = DISPLAY_POINT_LIMIT, orderbook_table_limit: int = ORDERBOOK_TABLE_LIMIT,
                 large_trade_table_limit: int = LARGE_TRADE_TABLE_LIMIT) -> None:
        if selected_market not in MARKETS or selected_timeframe not in TIMEFRAMES or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in (display_point_limit, orderbook_table_limit, large_trade_table_limit)):
            raise ValueError("invalid_builder_arguments")
        self.arguments = {"selected_market": selected_market, "selected_timeframe": selected_timeframe, "display_point_limit": display_point_limit,
                          "orderbook_table_limit": orderbook_table_limit, "large_trade_table_limit": large_trade_table_limit}

    def run(self, bundle: Mapping[str, Any], *, runtime_context: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return build_liquidity_microstructure_screen_contract(bundle, runtime_context=runtime_context, **self.arguments)
        except ValueError as exc:
            mode = bundle.get("processing", {}).get("mode") if isinstance(bundle, Mapping) else None
            return _fallback(mode, runtime_context if isinstance(runtime_context, Mapping) else None, str(exc))

# --- Canonical Screen contract shaping ---
from copy import deepcopy

from datetime import datetime, timezone

import json

import math

from pathlib import Path

from typing import Any, Mapping

_screen_VERSION = '1.5.0'

_screen_TEMPLATE_PATH = Path(__file__).with_name('screen_template.json')

_screen_MISSING = object()

_screen_SCREEN_A_EVENT_FIFO_LIMIT = 15

def _screen_template() -> dict[str, Any]:
    return json.loads(_screen_TEMPLATE_PATH.read_text(encoding='utf-8'))

def _screen_shape(reference: Any, candidate: Any=_screen_MISSING) -> Any:
    """Project runtime values onto the exact frozen SP key/nesting topology."""
    if isinstance(reference, dict):
        source = candidate if isinstance(candidate, Mapping) else {}
        return {key: _screen_shape(value, source.get(key, _screen_MISSING)) for key, value in reference.items()}
    if isinstance(reference, list):
        if candidate is _screen_MISSING:
            return deepcopy(reference)
        if not isinstance(candidate, list):
            return []
        if not reference:
            return deepcopy(candidate)
        if all((not isinstance(item, (dict, list)) for item in reference)):
            return deepcopy(candidate)
        if not candidate:
            return []
        identity_keys = ('metric_id', 'event_id', 'id', 'provider_id', 'side', 'panel_id')

        def ref_for(item: Any, index: int) -> Any:
            if isinstance(item, Mapping):
                for key in identity_keys:
                    value = item.get(key, _screen_MISSING)
                    if value is _screen_MISSING:
                        continue
                    for ref_item in reference:
                        if isinstance(ref_item, Mapping) and ref_item.get(key, _screen_MISSING) == value:
                            return ref_item
            return reference[index] if index < len(reference) else reference[0]
        return [_screen_shape(ref_for(item, index), item) for index, item in enumerate(candidate)]
    return deepcopy(reference if candidate is _screen_MISSING else candidate)

def _screen_iso(timestamp: Any) -> str | None:
    if type(timestamp) is not int or timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace('+00:00', 'Z')

def _screen_finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (not math.isfinite(value)):
        return None
    return float(value)

def _screen_event_row(row: Mapping[str, Any], reference_timestamp: int | None) -> dict[str, Any]:
    timestamp = row.get('timestamp')
    age = row.get('age_seconds')
    if age is None and type(timestamp) is int and (type(reference_timestamp) is int):
        age = max(0, reference_timestamp - timestamp)
    quantity = _screen_finite(row.get('quantity_base'))
    price = _screen_finite(row.get('price'))
    notional = _screen_finite(row.get('notional_quote', row.get('volume_usd')))
    if quantity is None and price and (notional is not None):
        quantity = notional / price
    return {'event_id': str(row.get('event_id', '')), 'timestamp': timestamp, 'age_seconds': age, 'side': row.get('side'), 'price': price, 'quantity_base': quantity, 'notional_quote': notional, 'distance_percent': row.get('distance_percent')}

def _screen_whale_row(row: Mapping[str, Any], reference_timestamp: int | None, *, mid_price: Any=None) -> dict[str, Any]:
    event = _screen_event_row(row, reference_timestamp)
    if event.get('distance_percent') is None:
        price = _screen_finite(event.get('price'))
        mid = _screen_finite(mid_price)
        if price is not None and mid not in {None, 0.0}:
            event['distance_percent'] = (price - mid) / mid * 100.0
    event.update({'first_seen_timestamp': row.get('first_seen_timestamp'), 'last_seen_timestamp': row.get('last_seen_timestamp', row.get('timestamp')), 'order_state': row.get('order_state', 'unknown')})
    return event

def _screen_fifo_event_window(rows: list[dict[str, Any]], *, limit: int=_screen_SCREEN_A_EVENT_FIFO_LIMIT, whale_orders: bool=False) -> list[dict[str, Any]]:
    """Return the newest FIFO window while preserving first-in/first-out semantics.

    Runtime/Input may retain a deeper event history for analytics.  Screen A intentionally
    exposes only the last ``limit`` unique events.  Whale orders use first_seen_timestamp
    as their queue insertion time so a later provider refresh does not move an old order
    back to the end of the queue.  Executed trades use their execution timestamp.
    """
    if limit <= 0:
        return []
    unique: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            continue
        event_id = str(row.get('event_id') or f'anonymous:{index}')
        unique[event_id] = deepcopy(dict(row))

    def queue_time(row: Mapping[str, Any]) -> int:
        if whale_orders:
            value = row.get('first_seen_timestamp')
            if type(value) is int:
                return value
        value = row.get('timestamp')
        return int(value) if type(value) is int else -1
    ordered = sorted(unique.values(), key=lambda row: (queue_time(row), str(row.get('event_id') or '')))
    return ordered[-limit:]

def _screen_profile(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cumulative = {'buy': 0.0, 'sell': 0.0}
    output: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: abs(float(item.get('distance_percent') or 0.0))):
        side = str(row.get('side'))
        if side not in cumulative:
            continue
        cumulative[side] += float(row.get('quantity_base') or 0.0)
        output.append({**deepcopy(row), 'cumulative_quantity_base': cumulative[side]})
    return output

def _screen_availability(candidate: Mapping[str, Any], template: Mapping[str, Any]) -> dict[str, Any]:
    charts = candidate.get('charts', {})
    tables = candidate.get('tables', {})
    widgets = candidate.get('widgets', {})
    drilldowns = candidate.get('drilldowns', {})
    kpis = {row.get('metric_id'): row for row in candidate.get('kpis', {}).get('items', []) if isinstance(row, Mapping)}

    def component(path: str) -> Mapping[str, Any]:
        head, _, tail = path.partition('.')
        if head == 'kpis':
            return kpis.get(tail, {})
        if head == 'charts':
            return charts.get(tail, {})
        if head == 'tables':
            return tables.get(tail, {})
        if head == 'widgets':
            return widgets.get(tail, {})
        if head == 'drilldowns':
            return drilldowns.get(tail, {})
        return {}
    result = {'required': {}, 'optional': {}}
    for group in ('required', 'optional'):
        for path, ref in template['availability'][group].items():
            node = component(path)
            status = node.get('status', 'unavailable')
            reason = node.get('reason') if status != 'available' else None
            result[group][path] = {'status': status, 'reason': reason, 'source_paths': deepcopy(node.get('source_paths', ref.get('source_paths', [])))}
    result['summary'] = {'required_available': sum((v['status'] == 'available' for v in result['required'].values())), 'required_total': len(result['required']), 'optional_available': sum((v['status'] == 'available' for v in result['optional'].values())), 'optional_total': len(result['optional'])}
    return result

def _screen_latest_non_null(values: list[Any]) -> Any:
    return next((value for value in reversed(values) if value is not None), None)

def _screen_regime_label(value: Any) -> str:
    number = _screen_finite(value)
    if number is None or abs(number) < 0.5:
        return 'DEEP / BALANCED'
    return 'ABSORPTION' if number > 0 else 'LIQUIDITY STRESS'

def _screen_native_analysis_payload(native: Mapping[str, Any], reference: Mapping[str, Any], *, is_demo: bool) -> dict[str, Any]:
    timestamps = list(native.get('timestamps', []) or [])[-730:]
    indicators = native.get('indicators', {}) if isinstance(native.get('indicators'), Mapping) else {}
    full_timestamps = list(native.get('timestamps', []) or [])
    offset = max(0, len(full_timestamps) - len(timestamps))
    records: list[dict[str, Any]] = []
    for local_index, timestamp in enumerate(timestamps):
        source_index = offset + local_index
        row: dict[str, Any] = {'timestamp': timestamp}
        for package in indicators.values():
            if not isinstance(package, Mapping):
                continue
            for key, values in package.items():
                if isinstance(values, list) and source_index < len(values):
                    row[key] = values[source_index]
        row['liquidity_regime'] = _screen_regime_label(row.get('liquidity_hmi_score'))
        row['is_synthetic'] = bool(is_demo)
        records.append(row)
    current_source = native.get('current', {}) if isinstance(native.get('current'), Mapping) else {}

    def series_value(package: str, key: str) -> Any:
        values = indicators.get(package, {}).get(key, []) if isinstance(indicators.get(package), Mapping) else []
        return _screen_latest_non_null(values) if isinstance(values, list) else None
    hmi = current_source.get('liquidity_hmi_score', series_value('liquidity_regime_hmi', 'liquidity_hmi_score'))
    current = {'depth_imbalance': current_source.get('depth_imbalance', series_value('depth_imbalance_pressure', 'depth_imbalance')), 'liquidity_stress_score': current_source.get('liquidity_stress_score', series_value('spread_market_impact_stress', 'liquidity_stress_score')), 'wall_concentration_score': series_value('liquidity_wall_concentration_vacuum', 'wall_concentration_score'), 'whale_persistence_score': series_value('whale_persistence_cancellation', 'whale_persistence_score'), 'absorption_index': current_source.get('absorption_index', series_value('executed_liquidity_absorption', 'absorption_index')), 'liquidity_hmi_score': hmi, 'wasserstein_distance': current_source.get('wasserstein_distance', series_value('liquidity_regime_hmi', 'wasserstein_distance')), 'liquidity_regime': _screen_regime_label(hmi)}
    candidate = {'status': native.get('status', 'available' if records else 'unavailable'), 'data_mode': 'synthetic_emulator' if is_demo else 'live_processing', 'processing_contract_target': True, 'real_market_calculation': not is_demo, 'hmi_recalculate': False, 'history_resolution': '4h', 'record_count': len(records), 'display_tail_records': 240, 'records': records, 'selector_contract': deepcopy(reference.get('selector_contract', {})), 'current': current}
    return _screen_shape(reference, candidate)

def _screen_liquidity_zone_candidate(levels: Any, whale_rows: Any, *, side: str, mid_price: Any=None) -> dict[str, Any] | None:
    if side not in {"bid", "ask"}:
        raise ValueError("liquidity_zone_side_must_be_bid_or_ask")
    wanted_whale_side = "buy" if side == "bid" else "sell"
    mid = _screen_finite(mid_price)
    candidates: list[dict[str, Any]] = []
    for row in list(levels or []):
        if not isinstance(row, Mapping):
            continue
        price = _screen_finite(row.get("price"))
        notional = _screen_finite(row.get("notional_quote"))
        quantity = _screen_finite(row.get("quantity_base"))
        distance = _screen_finite(row.get("distance_percent"))
        if price is None or notional is None or notional <= 0 or distance is None:
            continue
        if mid is not None and ((side == "bid" and price >= mid) or (side == "ask" and price <= mid)):
            continue
        signed_distance = -abs(distance) if side == "bid" else abs(distance)
        proximity_weight = 1.0 / (1.0 + 8.0 * abs(distance))
        candidates.append({
            "side": side, "price": price, "quantity_base": quantity,
            "notional_usd": notional, "distance_percent": signed_distance,
            "source": "order_book", "persistence_seconds": None,
            "selection_score": notional * proximity_weight,
        })
    for row in list(whale_rows or []):
        if not isinstance(row, Mapping) or str(row.get("side") or "").lower() != wanted_whale_side:
            continue
        price = _screen_finite(row.get("price"))
        notional = _screen_finite(row.get("notional_quote"))
        quantity = _screen_finite(row.get("quantity_base"))
        distance = _screen_finite(row.get("distance_percent"))
        if price is None or notional is None or notional <= 0 or distance is None:
            continue
        if mid is not None and ((side == "bid" and price >= mid) or (side == "ask" and price <= mid)):
            continue
        first_seen = row.get("first_seen_timestamp")
        last_seen = row.get("last_seen_timestamp", row.get("timestamp"))
        persistence = max(0, int(last_seen) - int(first_seen)) if type(first_seen) is int and type(last_seen) is int else 0
        persistence_weight = 1.0 + min(float(persistence) / 300.0, 1.0)
        proximity_weight = 1.0 / (1.0 + 8.0 * abs(distance))
        candidates.append({
            "side": side, "price": price, "quantity_base": quantity,
            "notional_usd": notional,
            "distance_percent": -abs(distance) if side == "bid" else abs(distance),
            "source": "whale_order", "persistence_seconds": persistence,
            "selection_score": notional * proximity_weight * persistence_weight,
        })
    return max(candidates, key=lambda item: float(item.get("selection_score") or 0.0)) if candidates else None


def _screen_executed_operations_chart(processing: Mapping[str, Any], market: str, reference: Mapping[str, Any]) -> dict[str, Any]:
    """Expose one actionable nearby-liquidity indicator instead of a sparse tape chart.

    The hidden fast execution tape remains available to Processing/analysis, but
    Screen A answers the user's operational question directly: where is the
    strongest nearby bid and ask liquidity right now?
    """
    current_book = (
        processing.get("markets", {}).get(market, {}).get("orderbook", {})
        .get("timeframes", {}).get("1m", {}).get("current") or {}
    ) if isinstance(processing.get("markets"), Mapping) else {}
    reference_timestamp = processing.get("reference_timestamp")
    whale_rows = [
        _screen_whale_row(row, reference_timestamp, mid_price=current_book.get("mid_price"))
        for row in processing.get("markets", {}).get(market, {}).get("whale_orders", {}).get("events", [])
        if isinstance(row, Mapping)
    ] if isinstance(processing.get("markets"), Mapping) else []
    mid = _screen_finite(current_book.get("mid_price"))
    bid_zone = _screen_liquidity_zone_candidate(current_book.get("bid_levels", []), whale_rows, side="bid", mid_price=mid)
    ask_zone = _screen_liquidity_zone_candidate(current_book.get("ask_levels", []), whale_rows, side="ask", mid_price=mid)
    bid_notional = _screen_finite((bid_zone or {}).get("notional_usd")) or 0.0
    ask_notional = _screen_finite((ask_zone or {}).get("notional_usd")) or 0.0
    total = bid_notional + ask_notional
    bid_share = (bid_notional / total) if total > 0 else None
    if bid_share is None:
        dominant = None
    elif bid_share >= 0.55:
        dominant = "bid"
    elif bid_share <= 0.45:
        dominant = "ask"
    else:
        dominant = "balanced"
    status = "available" if bid_zone is not None and ask_zone is not None and mid is not None else "partial" if (bid_zone or ask_zone) else "unavailable"
    candidate = deepcopy(dict(reference))
    candidate.update({
        "status": status,
        "reason": None if status == "available" else "one_or_more_liquidity_zones_unavailable" if status == "partial" else "liquidity_zones_unavailable",
        "chart_type": "nearest_liquidity_zones_indicator",
        "source_paths": [f"markets.{market}.orderbook.timeframes.1m.current", f"markets.{market}.whale_orders.events"],
        "data_as_of": current_book.get("timestamp"),
        "mid_price": mid,
        "bid_zone": bid_zone,
        "ask_zone": ask_zone,
        "dominant_side": dominant,
        "bid_liquidity_share": bid_share,
        "ask_liquidity_share": (1.0 - bid_share) if bid_share is not None else None,
        # Legacy arrays are intentionally empty: this Screen-A component is no
        # longer a sparse historical operations chart.
        "timestamps": [], "buy_executed": [], "sell_executed": [], "net_pressure": [],
        "series": [],
    })
    metadata = deepcopy(reference.get("metadata", {}))
    metadata.update({
        "hmi_recalculation": False,
        "selection_method": "max_adjusted_notional_by_proximity_and_whale_persistence",
        "indicator_semantics": "strongest_nearby_bid_and_ask_liquidity",
        "uses_order_book": True,
        "uses_whale_orders": True,
    })
    candidate["metadata"] = metadata
    return _screen_shape(reference, candidate)

def _screen_market_history_records(processing: Mapping[str, Any], market: str, *, is_demo: bool) -> list[dict[str, Any]]:
    source = processing.get('market_histories', {}).get(market, {}).get('records', [])
    result: list[dict[str, Any]] = []
    for row in list(source)[-730:]:
        if not isinstance(row, Mapping):
            continue
        buy = float(row.get('large_trade_buy_notional') or 0.0)
        sell = float(row.get('large_trade_sell_notional') or 0.0)
        total = buy + sell
        imbalance = row.get('depth_imbalance')
        result.append({'timestamp': row.get('timestamp'), 'asset': row.get('asset', 'BTC'), 'price': row.get('price'), 'spread_bps': row.get('spread_bps'), 'liquidity_imbalance_percent': None if imbalance is None else float(imbalance) * 100.0, 'whale_activity_index': row.get('whale_index_value'), 'large_trade_pressure': 0.0 if total == 0 else (buy - sell) / total, 'bid_depth_usd': row.get('bid_depth'), 'ask_depth_usd': row.get('ask_depth'), 'market_cap': row.get('market_cap'), 'circulating_supply': row.get('circulating_supply'), 'price_return_decimal': row.get('price_return_decimal'), 'is_synthetic': bool(is_demo)})
    return result

def _screen_current_depth(processing: Mapping[str, Any], market: str, timeframe: str='1m') -> Mapping[str, Any]:
    rows = processing.get('markets', {}).get(market, {}).get('order_depth', {}).get('timeframes', {}).get(timeframe, {}).get('direct_ranges', [])
    return next((row for row in reversed(list(rows or [])) if row.get('range_percent') == 10 and row.get('status') in {'available', 'partial'}), {})

def _screen_format_kpi(metric_id: str, value: Any) -> str:
    if value is None:
        return '--'
    number = float(value)
    if metric_id in {'bid_depth', 'ask_depth'}:
        return f'{number:,.2f} BTC'
    if metric_id in {'spread', 'impact_1_btc'}:
        return f'{number:,.2f} bps'
    if metric_id == 'liquidity_imbalance':
        return f'{number:+.2f}%'
    if metric_id == 'mid_price':
        return f'${number:,.2f}'
    return str(value)

def _screen_finalize_live_tables(payload: dict[str, Any]) -> None:
    """Force live table density and summaries after template shaping.

    `_screen_shape` deliberately preserves the frozen contract topology, but the
    reference template still carries historical display limits and example
    summaries.  Re-apply the runtime values after shaping so HMI metadata and
    rows are guaranteed to describe the same live book.
    """
    tables = payload.get("tables")
    if not isinstance(tables, dict):
        return

    orderbook = tables.get("orderbook_snapshot")
    if isinstance(orderbook, dict):
        bids = list(orderbook.get("bids") or [])
        asks = list(orderbook.get("asks") or [])
        bid_total = sum(float(row.get("quantity_base") or 0.0) for row in bids if isinstance(row, Mapping))
        ask_total = sum(float(row.get("quantity_base") or 0.0) for row in asks if isinstance(row, Mapping))
        bid_notional = sum(float(row.get("notional_quote") or 0.0) for row in bids if isinstance(row, Mapping))
        ask_notional = sum(float(row.get("notional_quote") or 0.0) for row in asks if isinstance(row, Mapping))
        total = bid_total + ask_total
        quote_total = bid_notional + ask_notional
        base = orderbook.setdefault("summary", {}).setdefault("base_quantity", {})
        base.update({
            "status": "available" if bids and asks else "unavailable",
            "reason": None if bids and asks else "source_not_available",
            "bid": bid_total,
            "ask": ask_total,
            "total": total,
            "net": bid_total - ask_total,
            "bid_share_percent": (100.0 * bid_total / total) if total else None,
            "ask_share_percent": (100.0 * ask_total / total) if total else None,
            "imbalance_percent": (100.0 * (bid_total - ask_total) / total) if total else None,
        })
        quote = orderbook["summary"].setdefault("quote_notional", {})
        quote.update({
            "status": "available" if bids and asks else "unavailable",
            "reason": None if bids and asks else "source_not_available",
            "bid": bid_notional,
            "ask": ask_notional,
            "total": quote_total,
            "net": bid_notional - ask_notional,
        })
        orderbook["summary"]["bid"] = {"quantity_base": bid_total, "notional_quote": bid_notional, "level_count": len(bids)}
        orderbook["summary"]["ask"] = {"quantity_base": ask_total, "notional_quote": ask_notional, "level_count": len(asks)}
        meta = orderbook.setdefault("metadata", {})
        meta.update({
            "rows_available": {"bid": len(bids), "ask": len(asks)},
            "rows_returned": {"bid": len(bids), "ask": len(asks)},
            "rows_truncated": False,
            "display_limit": 15,
            "scroll_if_more_rows": True,
            "visual_rows_target": min(15, max(len(bids), len(asks))),
            "bid_level_count": len(bids),
            "ask_level_count": len(asks),
        })

    for table_id in ("whale_orders", "large_trades"):
        table = tables.get(table_id)
        if not isinstance(table, dict):
            continue
        rows = list(table.get("rows") or [])
        meta = table.setdefault("metadata", {})
        meta["display_limit"] = 15
        meta["visual_rows_target"] = min(15, len(rows))
        meta["scroll_if_more_rows"] = len(rows) > 15


def _screen_runtime_market_view(reference: Mapping[str, Any], processing: Mapping[str, Any], classification: Mapping[str, Any], runtime_context: Mapping[str, Any], market: str) -> dict[str, Any]:
    view = deepcopy(dict(reference))
    timeframe = '1m'
    current_book = processing.get('markets', {}).get(market, {}).get('orderbook', {}).get('timeframes', {}).get(timeframe, {}).get('current') or {}
    depth = _screen_current_depth(processing, market, timeframe)
    provider_depth = depth.get('base_quantity', {}) if isinstance(depth, Mapping) else {}
    visible_book = current_book.get('bands', {}).get('full_visible_book', {}) if isinstance(current_book, Mapping) else {}
    visible_base = visible_book.get('base_quantity', {}) if isinstance(visible_book, Mapping) else {}
    impact = current_book.get('market_impact', {}) if isinstance(current_book, Mapping) else {}
    kpi_values = {'bid_depth': visible_base.get('bid'), 'ask_depth': visible_base.get('ask'), 'spread': current_book.get('spread_bps'), 'liquidity_imbalance': visible_base.get('imbalance_percent'), 'mid_price': current_book.get('mid_price'), 'impact_1_btc': impact.get('worst_side_impact_bps')}
    kpis = deepcopy(reference.get('kpis', {}))
    for item in kpis.get('items', []):
        metric_id = item.get('metric_id')
        value = kpi_values.get(metric_id)
        item['value'] = value
        item['display_value'] = _screen_format_kpi(str(metric_id), value)
        item['status'] = 'available' if value is not None else 'unavailable'
        item['reason'] = None if value is not None else 'source_not_available'
        item['source_timestamp'] = current_book.get('timestamp')
        if metric_id in {'bid_depth', 'ask_depth', 'liquidity_imbalance'}:
            bid_max = max((float(row.get('distance_percent')) for row in current_book.get('bid_levels', []) if row.get('distance_percent') is not None), default=None)
            ask_max = max((float(row.get('distance_percent')) for row in current_book.get('ask_levels', []) if row.get('distance_percent') is not None), default=None)
            item.setdefault('metadata', {}).update({'depth_window': 'full_visible_orderbook', 'max_bid_distance_percent': bid_max, 'max_ask_distance_percent': ask_max, 'provider_reference_depth_range_percent': depth.get('range_percent', 10), 'provider_reference_bid_depth': provider_depth.get('bid'), 'provider_reference_ask_depth': provider_depth.get('ask')})
    view['kpis'] = kpis
    profiles = processing.get('features', {}).get('profiles', {}).get(market, {}).get(timeframe, {})
    whale_history = [_screen_whale_row(row, processing.get('reference_timestamp'), mid_price=current_book.get('mid_price')) for row in processing.get('markets', {}).get(market, {}).get('whale_orders', {}).get('events', [])]
    whale_fifo = _screen_fifo_event_window(whale_history, whale_orders=True)
    whale_rows = sorted(whale_fifo, key=lambda row: row.get('first_seen_timestamp') or row.get('timestamp') or 0, reverse=True)
    whale_profile = _screen_profile(whale_rows)
    executed_rows = [_screen_event_row(row, processing.get('reference_timestamp')) for row in profiles.get('executed_liquidity_profile', [])]
    executed_profile = _screen_profile(executed_rows)
    trade_history = [_screen_event_row(row, processing.get('reference_timestamp')) for row in processing.get('markets', {}).get(market, {}).get('large_trades', {}).get('large_trade_events', [])]
    trade_fifo = _screen_fifo_event_window(trade_history)
    tape_rows = sorted(trade_fifo, key=lambda row: row.get('timestamp') or 0, reverse=True)
    charts = deepcopy(reference.get('charts', {}))
    bids = list(current_book.get('bid_levels', []))
    asks = list(current_book.get('ask_levels', []))
    order = charts.get('order_depth', {})
    order['status'] = current_book.get('status', 'available' if bids and asks else 'unavailable')
    order['reason'] = None if bids and asks else current_book.get('reason', 'source_not_available')
    order['data_as_of'] = current_book.get('timestamp')
    order['records'] = [{'side': 'bid', **row} for row in bids] + [{'side': 'ask', **row} for row in asks]
    order.setdefault('metadata', {})['bid_level_count'] = len(bids)
    order.setdefault('metadata', {})['ask_level_count'] = len(asks)
    order.setdefault('metadata', {})['total_level_count'] = len(bids) + len(asks)
    order.setdefault('metadata', {}).update({'depth_window': 'full_visible_orderbook', 'max_bid_distance_percent': max((float(row.get('distance_percent')) for row in bids if row.get('distance_percent') is not None), default=None), 'max_ask_distance_percent': max((float(row.get('distance_percent')) for row in asks if row.get('distance_percent') is not None), default=None), 'provider_reference_depth_range_percent': depth.get('range_percent', 10)})
    charts['order_depth'] = order
    for key, rows in (('whale_liquidity_profile', whale_profile), ('executed_liquidity_profile', executed_profile)):
        chart = charts.get(key, {})
        chart['status'] = 'available' if rows else 'unavailable'
        chart['reason'] = None if rows else 'source_not_available'
        chart['records'] = rows
        chart['data_as_of'] = max((row.get('timestamp') for row in rows if type(row.get('timestamp')) is int), default=None)
        chart.setdefault('metadata', {})['events_available'] = len(rows)
        chart.setdefault('metadata', {})['events_returned'] = len(rows)
        chart.setdefault('metadata', {})['event_count'] = len(rows)
        charts[key] = chart
    whale_source = processing.get('whale_activity', {}).get('timeframes', {}).get('4h', {}).get('records', [])
    whale_records = list(whale_source)[-730:]
    whale_chart = charts.get('whale_activity', {})
    whale_chart['status'] = 'available' if whale_records else 'unavailable'
    whale_chart['reason'] = None if whale_records else 'source_not_available'
    whale_chart['records'] = whale_records
    whale_chart['data_as_of'] = whale_records[-1].get('timestamp') if whale_records else None
    whale_chart['calculation_history'] = {'record_count': len(whale_records), 'records': whale_records}
    charts['whale_activity'] = whale_chart
    market_records = _screen_market_history_records(processing, market, is_demo=bool(runtime_context.get('is_demo')))
    market_chart = charts.get('market_history', {})
    market_chart['status'] = 'available' if market_records else 'unavailable'
    market_chart['reason'] = None if market_records else 'source_not_available'
    market_chart['records'] = market_records
    market_chart['data_as_of'] = market_records[-1].get('timestamp') if market_records else None
    market_chart['calculation_history'] = {'record_count': len(market_records), 'records': market_records}
    market_chart.setdefault('metadata', {})['records_available'] = len(market_records)
    market_chart.setdefault('metadata', {})['records_returned'] = len(market_records)
    charts['market_history'] = market_chart
    charts['executed_operations'] = _screen_executed_operations_chart(processing, market, charts.get('executed_operations', {}))
    view['charts'] = charts
    tables = deepcopy(reference.get('tables', {}))
    order_table = tables.get('orderbook_snapshot', {})
    order_table['status'] = 'available' if bids and asks else 'unavailable'
    order_table['reason'] = None if bids and asks else 'source_not_available'
    order_table['bids'] = bids[:50]
    order_table['asks'] = asks[:50]
    order_table.setdefault('metadata', {})['bid_level_count'] = len(bids)
    order_table.setdefault('metadata', {})['ask_level_count'] = len(asks)
    tables['orderbook_snapshot'] = order_table
    whale_table = tables.get('whale_orders', {})
    whale_table['status'] = 'available' if whale_rows else 'unavailable'
    whale_table['reason'] = None if whale_rows else 'source_not_available'
    whale_table['rows'] = whale_rows
    whale_table['summary'] = {'buy': sum((row.get('side') == 'buy' for row in whale_rows)), 'sell': sum((row.get('side') == 'sell' for row in whale_rows))}
    whale_table.setdefault('metadata', {}).update({'events_available': len(whale_history), 'events_returned': len(whale_rows), 'events_truncated': len(whale_history) > _screen_SCREEN_A_EVENT_FIFO_LIMIT, 'display_limit': _screen_SCREEN_A_EVENT_FIFO_LIMIT, 'scroll_if_more_rows': False, 'records_available': len(whale_history)})
    tables['whale_orders'] = whale_table
    trade_table = tables.get('large_trades', {})
    trade_table['status'] = 'available' if tape_rows else 'unavailable'
    trade_table['reason'] = None if tape_rows else 'source_not_available'
    trade_table['rows'] = tape_rows
    trade_table.setdefault('metadata', {}).update({'events_available': len(trade_history), 'events_returned': len(tape_rows), 'events_truncated': len(trade_history) > _screen_SCREEN_A_EVENT_FIFO_LIMIT, 'display_limit': _screen_SCREEN_A_EVENT_FIFO_LIMIT, 'scroll_if_more_rows': False})
    buy_notional = sum((float(row.get('notional_quote') or 0.0) for row in tape_rows if row.get('side') == 'buy'))
    sell_notional = sum((float(row.get('notional_quote') or 0.0) for row in tape_rows if row.get('side') == 'sell'))
    trade_table['summary'] = {'buy': buy_notional, 'sell': sell_notional, 'net_flow_usd': buy_notional - sell_notional, 'window': '1m', 'status': trade_table['status'], 'reason': trade_table['reason']}
    tables['large_trades'] = trade_table
    view['tables'] = tables
    widgets = deepcopy(reference.get('widgets', {}))
    market_class = classification.get('markets', {}).get(market, {})
    summary = market_class.get('summary', {}).get('1m', {})
    if isinstance(widgets.get('observed_liquidity'), Mapping):
        widgets['observed_liquidity']['current'] = deepcopy(summary)
        widgets['observed_liquidity']['status'] = 'available' if summary else 'unavailable'
        widgets['observed_liquidity']['reason'] = None if summary else 'source_not_available'
    trade_class = market_class.get('large_trades', {}).get('classification', {}).get('1m', {})
    if isinstance(widgets.get('large_trade_pressure'), Mapping):
        widgets['large_trade_pressure']['current'] = deepcopy(trade_class)
        trade_status = trade_class.get('status', 'unavailable')
        trade_values = trade_class.get('source_values', {}) if isinstance(trade_class.get('source_values'), Mapping) else {}
        usable_provisional = (
            trade_status == 'partial'
            and int(trade_values.get('event_count') or 0) > 0
            and trade_class.get('state') not in {None, 'no_observations', 'indeterminate'}
        )
        # The fast lane intentionally keeps a bounded FIFO and can therefore be
        # statistically provisional while still having a valid current signal.
        # Availability describes whether HMI can render the widget; the nested
        # classification keeps its own partial/provisional semantics.
        widgets['large_trade_pressure']['status'] = 'available' if usable_provisional else trade_status
        widgets['large_trade_pressure']['reason'] = None if usable_provisional else trade_class.get('reason')
    whale_class = classification.get('whale_activity', {}).get('timeframes', {}).get('4h', {}).get('classification', {})
    if isinstance(widgets.get('whale_activity_state'), Mapping):
        widgets['whale_activity_state']['current'] = deepcopy(whale_class)
        widgets['whale_activity_state']['status'] = whale_class.get('status', 'unavailable')
        widgets['whale_activity_state']['reason'] = whale_class.get('reason')
    history = processing.get('market_histories', {}).get(market, {})
    if isinstance(widgets.get('market_context'), Mapping):
        widgets['market_context']['current'] = deepcopy(history.get('current'))
        widgets['market_context']['status'] = history.get('status', 'unavailable')
        widgets['market_context']['reason'] = history.get('reason')
    comparison = classification.get('comparison', {}).get('spot_perpetual', {})
    if isinstance(widgets.get('spot_perpetual_comparison'), Mapping):
        widgets['spot_perpetual_comparison']['current'] = deepcopy(comparison)
        widgets['spot_perpetual_comparison']['status'] = comparison.get('status', 'unavailable')
        widgets['spot_perpetual_comparison']['reason'] = comparison.get('reason')
    if isinstance(widgets.get('source_status'), Mapping):
        widgets['source_status']['items'] = [{'provider_id': 'coinglass', 'label': 'CoinGlass', 'exchange': 'Binance', 'status': runtime_context.get('connection_status', 'not_reported')}, {'provider_id': 'internal_processing', 'label': 'Internal Processing', 'status': processing.get('quality', {}).get('status')}, {'provider_id': 'internal_classification', 'label': 'Internal Classification', 'status': classification.get('quality', {}).get('status')}]
        widgets['source_status']['status'] = 'available'
        widgets['source_status']['reason'] = None
        widgets['source_status']['source_paths'] = ['processing.quality', 'classification.quality']
    view['widgets'] = widgets
    native = processing.get('liquidity_analysis', {}).get(market, {})
    view['liquidity_analysis'] = _screen_native_analysis_payload(native, reference.get('liquidity_analysis', {}), is_demo=bool(runtime_context.get('is_demo')))
    live_timestamps = [
        current_book.get('timestamp'),
        processing.get('execution_timestamp'),
        *(row.get('timestamp') for row in whale_rows),
        *(row.get('timestamp') for row in tape_rows),
    ]
    live_data_as_of = max((int(value) for value in live_timestamps if type(value) is int), default=None)
    view['context'] = {
        'selected_market': market,
        'market_label': market.upper(),
        'symbol': current_book.get('symbol', 'BTCUSDT'),
        'data_as_of': live_data_as_of,
    }
    shaped = _screen_shape(reference, view)
    _screen_finalize_live_tables(shaped)
    source_status = shaped.get('widgets', {}).get('source_status')
    if isinstance(source_status, dict):
        source_status['source_paths'] = ['processing.quality', 'classification.quality']
    return shaped

def _screen_fill_status_reasons(value: Any) -> None:
    if isinstance(value, dict):
        if 'status' in value and 'reason' in value and (value.get('status') in {'partial', 'unavailable', 'invalid', 'warming_up', 'error'}) and (not value.get('reason')):
            value['reason'] = {'partial': 'partial_runtime_data', 'unavailable': 'source_not_available', 'invalid': 'invalid_runtime_data', 'warming_up': 'runtime_warmup_in_progress', 'error': 'runtime_source_error'}[value['status']]
        for child in value.values():
            _screen_fill_status_reasons(child)
    elif isinstance(value, list):
        for child in value:
            _screen_fill_status_reasons(child)

def align_liquidity_microstructure_to_sp_v1_4(candidate: Mapping[str, Any], processing: Mapping[str, Any], classification: Mapping[str, Any], runtime_context: Mapping[str, Any], *, _copy_candidate: bool = True) -> dict[str, Any]:
    ref = _screen_template()
    out = deepcopy(dict(candidate)) if _copy_candidate else candidate
    clone = deepcopy if _copy_candidate else (lambda value: value)
    reference_timestamp = processing.get('reference_timestamp')
    selected_market = out.get('context', {}).get('selected_market', 'perpetual')
    selected_timeframe = out.get('context', {}).get('selected_timeframe', '1m')
    out['schema'] = {'id': ref['schema']['id'], 'version': _screen_VERSION}
    out['screen'] = deepcopy(ref['screen'])
    out['stage'] = 'screen_contract'
    context = deepcopy(out.get('context', {}))
    context.update({'fixture_as_of_timestamp': None, 'fixture_as_of_iso': None, 'synthetic_fixture': False, 'realism_refactor_version': 'runtime_v1_2', 'realism_note': 'CoinGlass orderbook + large-limit-order + footprint runtime vertical; no HMI reconstruction.', 'limitations': ['coinglass_only', 'no_glassnode', 'no_cryptoquant', 'range_10_is_not_full_book', 'observed_conditions_not_global_absolute_liquidity', 'provider_is_coinglass_exchange_is_binance', 'whale_orders_from_large_limit_order_endpoint', 'executed_liquidity_is_footprint_price_bin_aggregation_not_individual_trade_tape']})
    out['context'] = context
    out['selectors'] = {'market': deepcopy(out.get('selectors', {}).get('market', ref['selectors']['market']))}
    current_book = processing.get('markets', {}).get(selected_market, {}).get('orderbook', {}).get('timeframes', {}).get(selected_timeframe, {}).get('current') or {}
    bids = list(current_book.get('bid_levels', []))
    asks = list(current_book.get('ask_levels', []))
    order_chart = clone(out.get('charts', {}).get('order_depth', {}))
    order_chart['subtitle'] = ref['charts']['order_depth']['subtitle']
    order_chart['records'] = [{'side': 'bid', **row} for row in bids] + [{'side': 'ask', **row} for row in asks]
    order_chart.setdefault('metadata', {}).update({'semantic_sides': ['bid', 'ask'], 'legend_labels': {'bid': 'Bids', 'ask': 'Asks'}, 'bid_level_count': len(bids), 'ask_level_count': len(asks), 'total_level_count': len(bids) + len(asks), 'depth_window': 'full_visible_orderbook', 'max_bid_distance_percent': max((float(row.get('distance_percent')) for row in bids if row.get('distance_percent') is not None), default=None), 'max_ask_distance_percent': max((float(row.get('distance_percent')) for row in asks if row.get('distance_percent') is not None), default=None), 'provider_reference_depth_range_percent': 10})
    profiles = processing.get('features', {}).get('profiles', {}).get(selected_market, {}).get(selected_timeframe, {})
    whale_history = [_screen_whale_row(row, reference_timestamp, mid_price=current_book.get('mid_price')) for row in processing.get('markets', {}).get(selected_market, {}).get('whale_orders', {}).get('events', [])]
    whale_fifo = _screen_fifo_event_window(whale_history, whale_orders=True)
    whale_rows = sorted(whale_fifo, key=lambda row: row.get('first_seen_timestamp') or row.get('timestamp') or 0, reverse=True)
    whale_profile = _screen_profile(whale_rows)
    executed_rows = [_screen_event_row(row, reference_timestamp) for row in profiles.get('executed_liquidity_profile', [])]
    executed_profile = _screen_profile(executed_rows)
    charts = clone(out.get('charts', {}))
    charts['order_depth'] = order_chart
    for key, rows, semantic, title in (('whale_liquidity_profile', whale_profile, 'large_limit_order_liquidity', 'WHALE LIQUIDITY PROFILE'), ('executed_liquidity_profile', executed_profile, 'executed_footprint_price_bins', 'EXECUTED LIQUIDITY PROFILE')):
        chart = clone(charts.get(key, {}))
        chart['title'] = title
        chart['subtitle'] = ref['charts'][key]['subtitle']
        chart['status'] = 'available' if rows else 'unavailable'
        chart['reason'] = None if rows else 'source_not_available'
        chart['records'] = rows
        chart['data_as_of'] = max((r.get('timestamp') for r in rows if type(r.get('timestamp')) is int), default=None)
        chart['metadata'] = {'scope': 'single_exchange_runtime', 'semantic_sides': ['buy', 'sell'], 'legend_labels': {'buy': 'Buy', 'sell': 'Sell'}, 'x_axis': {'field': 'distance_percent'}, 'basis': 'cumulative_quantity_base', 'unit': 'BTC', 'cumulative_origin': 'mid_price', 'required_record_fields': list(ref['charts'][key]['metadata']['required_record_fields']), 'note': semantic, 'events_available': len(rows), 'events_returned': len(rows), 'event_count': len(rows)}
        if key == 'executed_liquidity_profile':
            chart['metadata'].update({'selected_window': selected_timeframe, 'profile_normalized': True})
        charts[key] = chart
    trade_history = [_screen_event_row(row, reference_timestamp) for row in processing.get('markets', {}).get(selected_market, {}).get('large_trades', {}).get('large_trade_events', [])]
    trade_fifo = _screen_fifo_event_window(trade_history)
    tape_rows = sorted(trade_fifo, key=lambda row: row.get('timestamp') or 0, reverse=True)
    whale_source = processing.get('whale_activity', {}).get('timeframes', {}).get(selected_timeframe, {}).get('records', []) or []
    whale_calc_records = list(whale_source)[-730:]
    whale_chart = clone(charts.get('whale_activity', {}))
    whale_chart['calculation_history'] = {'record_count': len(whale_calc_records), 'records': whale_calc_records}
    whale_chart.setdefault('metadata', {}).update({'events_available': len(whale_source), 'events_returned': len(whale_chart.get('records', []))})
    charts['whale_activity'] = whale_chart
    market_source = processing.get('market_history', {}).get('records', []) or []
    market_calc_records = list(market_source)[-730:]
    market_chart = clone(charts.get('market_history', {}))
    market_chart['calculation_history'] = {'record_count': len(market_calc_records), 'records': market_calc_records}
    market_chart.setdefault('metadata', {}).update({'records_available': len(market_source), 'records_returned': len(market_chart.get('records', [])), 'history_truncated': len(market_source) > len(market_chart.get('records', []))})
    charts['market_history'] = market_chart
    out['charts'] = charts
    tables = clone(out.get('tables', {}))
    ob_table = clone(tables.get('orderbook_snapshot', {}))
    ob_table['bids'] = bids
    ob_table['asks'] = asks
    bid_total = sum(float(r.get('quantity_base') or 0.0) for r in bids)
    ask_total = sum(float(r.get('quantity_base') or 0.0) for r in asks)
    bid_notional = sum(float(r.get('notional_quote') or 0.0) for r in bids)
    ask_notional = sum(float(r.get('notional_quote') or 0.0) for r in asks)
    ob_table['summary'] = {
        'base_quantity': {'bid': bid_total, 'ask': ask_total},
        'notional_quote': {'bid': bid_notional, 'ask': ask_notional},
        'imbalance': (bid_total - ask_total) / max(bid_total + ask_total, 1e-12),
    }
    ob_table['subtitle'] = ref['tables']['orderbook_snapshot']['subtitle']
    ob_table['display_columns'] = deepcopy(ref['tables']['orderbook_snapshot']['display_columns'])
    ob_table.setdefault('metadata', {}).update({'rows_available': len(bids) + len(asks), 'rows_returned': len(bids) + len(asks), 'rows_truncated': False, 'display_limit': 15, 'scroll_if_more_rows': True, 'visual_rows_target': min(15, max(len(bids), len(asks))), 'bid_level_count': len(bids), 'ask_level_count': len(asks)})
    tables['orderbook_snapshot'] = ob_table
    whale_table = clone(tables.get('whale_orders', {}))
    whale_table['rows'] = sorted(whale_rows, key=lambda row: row.get('timestamp') or 0, reverse=True)
    whale_table['status'] = 'available' if whale_rows else 'unavailable'
    whale_table['reason'] = None if whale_rows else 'source_not_available'
    whale_table['display_columns'] = deepcopy(ref['tables']['whale_orders']['display_columns'])
    whale_table['summary'] = {'buy': sum((1 for r in whale_rows if r.get('side') == 'buy')), 'sell': sum((1 for r in whale_rows if r.get('side') == 'sell'))}
    whale_table['metadata'] = {'events_available': len(whale_history), 'events_returned': len(whale_rows), 'events_truncated': len(whale_history) > _screen_SCREEN_A_EVENT_FIFO_LIMIT, 'display_limit': _screen_SCREEN_A_EVENT_FIFO_LIMIT, 'age_semantics': 'reference_timestamp_minus_last_seen', 'side_semantics': 'provider_order_side', 'required_row_fields': deepcopy(ref['tables']['whale_orders']['metadata']['required_row_fields']), 'note': 'CoinGlass Large Orderbook active orders', 'source_note': 'runtime_provider_feed', 'scroll_if_more_rows': False, 'visual_rows_target': min(15, len(whale_rows)), 'records_available': len(whale_history)}
    tables['whale_orders'] = whale_table
    trade_table = clone(tables.get('large_trades', {}))
    trade_table['rows'] = tape_rows
    trade_table['status'] = 'available' if tape_rows else 'unavailable'
    trade_table['reason'] = None if tape_rows else 'source_not_available'
    trade_table['display_columns'] = deepcopy(ref['tables']['large_trades']['display_columns'])
    buy = sum((float(r.get('notional_quote') or 0) for r in tape_rows if r.get('side') == 'buy'))
    sell = sum((float(r.get('notional_quote') or 0) for r in tape_rows if r.get('side') == 'sell'))
    trade_table['summary'] = {'buy': buy, 'sell': sell, 'net_flow_usd': buy - sell, 'window': selected_timeframe, 'status': trade_table['status'], 'reason': trade_table['reason']}
    trade_table['metadata'] = {'events_available': len(trade_history), 'events_returned': len(tape_rows), 'events_truncated': len(trade_history) > _screen_SCREEN_A_EVENT_FIFO_LIMIT, 'observed_first_timestamp': min((r.get('timestamp') for r in tape_rows if type(r.get('timestamp')) is int), default=None), 'observed_last_timestamp': max((r.get('timestamp') for r in tape_rows if type(r.get('timestamp')) is int), default=None), 'observed_span_seconds': 0 if len(tape_rows) < 2 else max((r['timestamp'] for r in tape_rows)) - min((r['timestamp'] for r in tape_rows)), 'coverage_complete': bool(tape_rows), 'reason': None if tape_rows else 'source_not_available', 'side_semantics': 'taker_buy_sell_footprint', 'required_row_fields': deepcopy(ref['tables']['large_trades']['metadata']['required_row_fields']), 'source_note': 'runtime_coinGlass_footprint_bins', 'display_limit': _screen_SCREEN_A_EVENT_FIFO_LIMIT, 'scroll_if_more_rows': False, 'visual_rows_target': min(15, len(tape_rows)), 'records_available': len(tape_rows)}
    tables['large_trades'] = trade_table
    out['tables'] = tables
    out['layout'] = deepcopy(ref['layout'])
    market_history_chart = out.get('charts', {}).get('market_history', {})
    whale_activity_chart = out.get('charts', {}).get('whale_activity', {})
    market_history_records = max(len(market_history_chart.get('records', [])), len(market_history_chart.get('calculation_history', {}).get('records', [])))
    whale_activity_records = max(len(whale_activity_chart.get('records', [])), len(whale_activity_chart.get('calculation_history', {}).get('records', [])))
    temporal_records = max(market_history_records, whale_activity_records)
    out['history_contract'] = {'calculation_records': temporal_records, 'minimum_warmup_records': 200, 'maximum_standard_indicator_period': 200, 'all_visible_moving_averages_warm': temporal_records >= 200, 'technical_indicators_precomputed': True, 'hmi_recalculation': False, 'synthetic_fixture': False, 'fixture_seed': None, 'resolution': selected_timeframe, 'note': 'Temporal context is upstream-owned; orderbook/order-event snapshots remain family-specific.'}
    out['technical_analysis'] = {'enabled': False, 'applies': False, 'screen_b': True, 'reason': 'screen_b_uses_native_liquidity_microstructure_analytics_not_generic_technical_indicators', 'hmi_must_not_render_generic_technical_indicators': True, 'hmi_recalculation': False}
    market_views = {market: _screen_runtime_market_view(ref['market_views'][market], processing, classification, runtime_context, market) for market in ('spot', 'perpetual')}
    out['market_views'] = market_views
    market_data_as_of = max(
        (
            int(view.get('context', {}).get('data_as_of'))
            for view in market_views.values()
            if type(view.get('context', {}).get('data_as_of')) is int
        ),
        default=processing.get('execution_timestamp'),
    )
    out['context']['data_as_of'] = market_data_as_of
    selected_view = market_views.get(selected_market, market_views['perpetual'])
    out['kpis'] = clone(selected_view['kpis'])
    out['charts'] = clone(selected_view['charts'])
    out['tables'] = clone(selected_view['tables'])
    out['widgets'] = clone(selected_view['widgets'])
    out['liquidity_analysis'] = clone(selected_view['liquidity_analysis'])
    out['selectors'] = deepcopy(ref['selectors'])
    out['selectors']['market']['selected'] = selected_market
    out['context']['selected_market'] = selected_market
    out['context']['selected_timeframe'] = '1m'
    out['history_contract'] = {'calculation_records': len(out['liquidity_analysis'].get('records', [])), 'minimum_warmup_records': 200, 'maximum_standard_indicator_period': 200, 'all_visible_moving_averages_warm': len(out['liquidity_analysis'].get('records', [])) >= 200, 'technical_indicators_precomputed': False, 'hmi_recalculation': False, 'synthetic_fixture': False, 'fixture_seed': None, 'resolution': '4h', 'note': '730 temporal liquidity snapshots; snapshot/order-event structures remain family-specific.', 'native_liquidity_screen_b_precomputed': True}
    out['availability'] = _screen_availability(out, ref)
    required_statuses = {k: v['status'] for k, v in out['availability']['required'].items()}
    invalid = sorted((k for k, v in required_statuses.items() if v == 'invalid'))
    partial = sorted((k for k, v in required_statuses.items() if v == 'partial'))
    unavailable = sorted((k for k, v in required_statuses.items() if v == 'unavailable'))
    quality_status = 'invalid' if invalid else 'partial' if partial or unavailable else 'ok'
    out['quality'] = {'status': quality_status, 'contract_complete': True, 'data_complete': not partial and (not unavailable), 'processing_status': processing.get('quality', {}).get('status'), 'classification_status': classification.get('quality', {}).get('status'), 'availability': deepcopy(out['availability']['summary']), 'missing_required_components': unavailable, 'partial_components': partial, 'unavailable_components': unavailable, 'invalid_components': invalid, 'warnings': ['executed_liquidity_is_footprint_price_bin_aggregation'] if tape_rows else ['executed_liquidity_footprint_unavailable'], 'errors': [], 'data_as_of': out.get('context', {}).get('data_as_of'), 'extensions': {'visual_emulator_population': {'status': 'available', 'orderbook_levels_each_side': max(len(bids), len(asks)), 'whale_order_events': len(whale_rows), 'large_trade_events': len(tape_rows), 'synthetic': bool(runtime_context.get('is_demo'))}, 'dense_liquidity_emulator_profile': {'status': 'available', 'orderbook_levels_per_side': max(len(bids), len(asks)), 'whale_order_events': len(whale_rows), 'large_trade_events': len(tape_rows), 'synthetic': bool(runtime_context.get('is_demo')), 'purpose': 'visual_contract_and_hmi_density_validation'}, 'global_control_visibility_v1': {'view_selector_visible': False, 'market_selector_visible': False, 'state_preserved_internally': True}, 'liquidity_table_density_v2': {'status': 'available', 'orderbook_visible_rows_per_side': min(15, max(len(bids), len(asks))), 'whale_orders_visible_rows': min(15, len(whale_rows)), 'large_trades_visible_rows': min(15, len(tape_rows)), 'scroll_if_more_rows': True, 'hmi_truncation_source': 'table.metadata.display_limit'}, 'large_trades_dense_tape_v2': {'events': len(tape_rows), 'visible_rows': min(20, len(tape_rows)), 'scroll_enabled': False, 'synthetic': bool(runtime_context.get('is_demo'))}, 'canonical_590_scope': {'applies': False, 'reason': 'No technical-selector column is present in this family; its accepted family-specific geometry is preserved.'}, 'temporal_history_730_v1': {'status': 'available' if temporal_records else 'partial', 'temporal_snapshots': temporal_records, 'temporal_fields': deepcopy(ref['quality']['extensions']['temporal_history_730_v1']['temporal_fields']), 'snapshot_structures_not_forced_to_730': deepcopy(ref['quality']['extensions']['temporal_history_730_v1']['snapshot_structures_not_forced_to_730'])}, 'selector_cleanup_v2': {'timeframe_selector_removed': True, 'reason': 'snapshot_and_temporal_context_are_contract_driven'}, 'temporal_selector_contract_v3': {'selector_type': 'NONE', 'timeframe_selector_visible': False, 'range_selector_visible': False, 'refresh_mode': 'structural_5s_plus_executed_fast_1s', 'structural_refresh_seconds': 5.0, 'executed_liquidity_refresh_seconds': 1.0, 'large_trades_fifo': 15, 'large_trades_max_new_per_cycle': 10}, 'realism_v1': {'status': 'available', 'fixture_as_of_timestamp': None, 'fixture_as_of_iso': None, 'deterministic_seed': None, 'synthetic_not_live': bool(runtime_context.get('is_demo')), 'orderbook_levels_per_side': max(len(bids), len(asks)), 'whale_orders': len(whale_rows), 'large_trades': len(tape_rows), 'temporal_history_records': temporal_records, 'common_as_of_contract': _screen_iso(reference_timestamp)}}}
    cadence_contract = out['quality']['extensions']['temporal_selector_contract_v3']
    cadence_contract.update({
        'refresh_mode': 'structural_10s_plus_snapshot_tables_5s',
        'structural_refresh_seconds': 10.0,
        'executed_liquidity_refresh_seconds': 5.0,
    })
    # Synchronize the public status after all six native Screen-B blocks and
    # required widgets have been evaluated. Emulator is synthetic runtime data,
    # not a legacy DEMO/fixture mode.
    if isinstance(out.get('operational_status'), dict):
        out['operational_status']['status'] = quality_status
        out['operational_status']['quality_status'] = quality_status
        out['operational_status']['reason'] = None if quality_status == 'ok' else 'partial_runtime_data'
    out['badges'] = ([{'id': 'SYNTHETIC', 'color_token': 'info'}] if runtime_context.get('is_demo') else []) + ([{'id': 'DEGRADED', 'color_token': 'warning'}] if quality_status != 'ok' else [])
    _screen_fill_status_reasons(out)
    shaped = _screen_shape(ref, out)
    _screen_finalize_live_tables(shaped)
    for market_view in shaped.get('market_views', {}).values():
        if isinstance(market_view, dict):
            _screen_finalize_live_tables(market_view)
    shaped['technical_analysis'] = clone(out['technical_analysis'])
    return shaped
