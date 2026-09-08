from processing_signals.input.prices_ohlcv.prices_fast_lane import patch_prices_contract


def _contract():
    return {
        "charts": {"ohlcv": {"markets": {"spot": {"timeframes": {"1m": {
            "records": [{"timestamp": 60, "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.0, "volume_usd": 1.0}],
            "metadata": {},
        }}}}}},
        "kpis": {"items": [{"metric_id": "last_price", "value": 10.0, "status": "available"}]},
        "context": {},
        "quality": {},
    }


def test_fast_lane_replaces_live_candle_and_current_price():
    candle = {"timestamp": 60, "open": 10.0, "high": 12.0, "low": 9.0, "close": 11.5, "volume_usd": 2.0}
    result = patch_prices_contract(_contract(), candle, observed_at=65)
    records = result["charts"]["ohlcv"]["markets"]["spot"]["timeframes"]["1m"]["records"]
    assert records == [candle]
    assert result["kpis"]["items"][0]["value"] == 11.5
    assert result["context"]["prices_fast_lane_data_as_of"] == 65


def test_fast_lane_appends_new_bucket_without_recalculating_indicators():
    candle = {"timestamp": 120, "open": 10.0, "high": 12.0, "low": 9.0, "close": 11.0, "volume_usd": 2.0}
    result = patch_prices_contract(_contract(), candle, observed_at=125)
    records = result["charts"]["ohlcv"]["markets"]["spot"]["timeframes"]["1m"]["records"]
    assert [row["timestamp"] for row in records] == [60, 120]
    extension = result["quality"]["extensions"]["prices_fast_lane_v1"]
    assert extension["cadence_seconds"] == 5.0
    assert extension["timeframes"] == ["1m", "5m"]
    assert extension["technical_indicators_recalculated"] is False


def test_fast_lane_can_update_active_5m_candle_without_changing_kpi():
    contract = _contract()
    contract["charts"]["ohlcv"]["markets"]["spot"]["timeframes"]["5m"] = {
        "records": [{"timestamp": 300, "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.0, "volume_usd": 1.0}],
        "metadata": {},
    }
    candle = {"timestamp": 300, "open": 10.0, "high": 13.0, "low": 9.0, "close": 12.0, "volume_usd": 4.0}
    result = patch_prices_contract(contract, candle, observed_at=325, timeframe="5m")
    records = result["charts"]["ohlcv"]["markets"]["spot"]["timeframes"]["5m"]["records"]
    assert records == [candle]
    assert result["kpis"]["items"][0]["value"] == 10.0
