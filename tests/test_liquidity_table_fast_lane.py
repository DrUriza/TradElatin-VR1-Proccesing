from processing_signals.input.liquidity_microstructure.liquidity_fast_lane import patch_screen_contract


def test_fast_lane_patches_orderbook_whales_and_large_trades():
    contract = {
        "context": {"selected_market": "perpetual", "data_as_of": 1},
        "market_views": {market: {"tables": {}, "charts": {}} for market in ("spot", "perpetual")},
        "quality": {},
    }
    tape = [{"event_id": "trade", "timestamp": 10, "side": "buy", "price": 100.0,
             "quantity_base": 1.0, "notional_quote": 100.0}]
    orderbook = {
        "status": "available", "mid_price": 100.0,
        "bid_levels": [{"price": 99.0, "quantity_base": 2.0, "notional_quote": 198.0,
                        "distance_percent": 1.0, "cumulative_quantity_base": 2.0}],
        "ask_levels": [{"price": 101.0, "quantity_base": 3.0, "notional_quote": 303.0,
                        "distance_percent": 1.0, "cumulative_quantity_base": 3.0}],
        "bands": {"full_visible_book": {"base_quantity": {"bid": 2.0, "ask": 3.0}}},
    }
    whale = {"event_id": "whale", "timestamp": 10, "first_seen_timestamp": 10,
             "side": "sell", "price": 101.0, "quantity_base": 4.0,
             "notional_quote": 404.0, "distance_percent": 1.0}
    result = patch_screen_contract(
        contract, {"spot": tape, "perpetual": tape}, observed_at=12,
        snapshots={market: {"orderbook": orderbook, "whales": [whale]} for market in ("spot", "perpetual")},
    )
    tables = result["market_views"]["perpetual"]["tables"]
    assert tables["orderbook_snapshot"]["bids"][0]["price"] == 99.0
    assert tables["whale_orders"]["rows"][0]["event_id"] == "whale"
    assert tables["large_trades"]["rows"][0]["event_id"] == "trade"
    assert result["quality"]["extensions"]["liquidity_tables_fast_lane_v1"]["cadence_seconds"] == 5.0
