from __future__ import annotations

from copy import deepcopy

from processing_signals.main.main_pipeline import _preserve_incremental_history


def test_incremental_history_reuses_closed_rows_without_mutating_sources() -> None:
    existing = {
        "series": {
            "records": [
                {"timestamp": 100, "close": 1.0, "nested": {"value": "closed"}},
                {"timestamp": 200, "close": 2.0, "nested": {"value": "live-old"}},
            ],
            "records_available": 2,
            "incoming_valid_count": 2,
            "last_timestamp": 200,
        }
    }
    candidate = {
        "series": {
            "records": [
                {"timestamp": 100, "close": 999.0, "nested": {"value": "provider-rewrite"}},
                {"timestamp": 200, "close": 2.5, "nested": {"value": "live-new"}},
                {"timestamp": 300, "close": 3.0, "nested": {"value": "appended"}},
            ],
            "incoming_records": [{"timestamp": 200}, {"timestamp": 300}],
            "records_available": 3,
            "incoming_valid_count": 3,
            "last_timestamp": 300,
        }
    }
    existing_before = deepcopy(existing)
    candidate_before = deepcopy(candidate)

    merged = _preserve_incremental_history(candidate, existing, ("prices_ohlcv",))

    assert [row["close"] for row in merged["series"]["records"]] == [1.0, 2.5, 3.0]
    assert merged["series"]["records"][0] is existing["series"]["records"][0]
    assert merged["series"]["records"][1] is candidate["series"]["records"][1]
    assert merged["series"]["records"][2] is candidate["series"]["records"][2]
    assert existing == existing_before
    assert candidate == candidate_before
    assert merged["series"]["records_available"] == 3
    assert merged["series"]["incoming_valid_count"] == 2
    assert merged["series"]["last_timestamp"] == 300


def test_event_records_remain_candidate_owned_and_unmerged() -> None:
    existing = {"whale_orders": {"records": [{"timestamp": 100, "price": 1.0}]}}
    candidate = {"whale_orders": {"records": [{"timestamp": 100, "price": 2.0}]}}

    result = _preserve_incremental_history(
        candidate,
        existing,
        ("liquidity_microstructure", "whale_orders"),
    )

    assert result["whale_orders"]["records"] is candidate["whale_orders"]["records"]
    assert result["whale_orders"]["records"][0]["price"] == 2.0
