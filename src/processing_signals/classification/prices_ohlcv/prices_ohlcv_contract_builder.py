from __future__ import annotations

from copy     import deepcopy
from datetime import UTC, datetime
import json
import math
from typing import Any, Mapping

from .prices_ohlcv_classifier import RSI_OVERBOUGHT, RSI_OVERSOLD, STOCHASTIC_OVERBOUGHT, STOCHASTIC_OVERSOLD


TIMEFRAME_ORDER        = ("1m", "5m", "15m", "4h")
MARKET_ORDER           = ("spot",)
DEFAULT_MARKET         = "spot"
DEFAULT_TIMEFRAME      = "15m"
DEFAULT_DISPLAY_WINDOW = 120
TIMEFRAME_SECONDS      = {"1m": 60, "5m": 300, "15m": 900, "4h": 14_400}
TIMEFRAME_SOURCES      = {"1m": ("1m", 1), "5m": ("1m", 5), "15m": ("15m", 1), "4h": ("15m", 16)}

INDICATOR_ROWS = (
    ("rsi", "RSI (14)"), ("macd", "MACD"), ("macd_signal", "MACD Signal"), ("macd_histogram", "MACD Hist"),
    ("stochastic", "Stochastic (14,3,3)"), ("adx", "ADX (14)"), ("cci", "CCI (20)"), ("mfi", "MFI (14)"),
    ("williams_r", "Williams %R (14)"), ("atr", "ATR (14)"), ("tsi", "TSI (25,13)"),
)
STATISTICAL_ROWS = (
    ("mean", "Mean"), ("standard_deviation", "Std Dev"), ("skewness", "Skewness"), ("kurtosis", "Kurtosis"),
    ("z_score", "Z-Score"), ("var_95", "VaR (95%)"), ("cvar_95", "CVaR (95%)"),
    ("max_consecutive_wins", "Max Consec Wins"), ("max_consecutive_losses", "Max Consec Losses"),
    ("omega_ratio", "Omega Ratio"), ("sharpe_ratio", "Sharpe Ratio"), ("sortino_ratio", "Sortino Ratio"),
    ("calmar_ratio", "Calmar Ratio"), ("max_drawdown", "Max Drawdown"), ("profit_factor", "Profit Factor"),
    ("recovery_factor", "Recovery Factor"), ("win_rate", "Win Rate"),
)

INDICATOR_DISPLAY_FIELD = {
    "rsi": "state", "macd": "signal", "macd_signal": "signal", "macd_histogram": "signal", "stochastic": "state",
    "adx": "state", "cci": "signal", "mfi": "state", "williams_r": "state", "atr": "state", "tsi": "state"}


def resolve_prices_selection(processing_output: Mapping[str, Any]) -> dict[str, Any]:
    selector             = processing_output.get("features", {}).get("market_selector", {})
    available_markets    = list(selector.get("available_markets") or MARKET_ORDER)
    available_timeframes = list(selector.get("timeframes") or TIMEFRAME_ORDER)
    selected_market      = selector.get("selected_market") or selector.get("default_market") or DEFAULT_MARKET
    selected_timeframe   = selector.get("selected_timeframe") or selector.get("default_timeframe") or DEFAULT_TIMEFRAME
    if selected_market not in available_markets:
        selected_market = available_markets[0] if available_markets else DEFAULT_MARKET
    if selected_timeframe not in available_timeframes:
        selected_timeframe = available_timeframes[0] if available_timeframes else DEFAULT_TIMEFRAME
    return {"selected_market": selected_market, "selected_timeframe": selected_timeframe,
            "available_markets": available_markets, "available_timeframes": available_timeframes}


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _normalize_display_zero(value: Any, *, decimals: int) -> float | None:
    numeric = _finite(value)
    if numeric is None:
        return None
    rounded = round(numeric, decimals)
    return 0.0 if rounded == 0 else rounded


def _display(value: Any, *, percent: bool = False) -> str:
    numeric = _normalize_display_zero((_finite(value) or 0.0) * 100.0 if percent and _finite(value) is not None else value, decimals=2)
    if numeric is None:
        return "N/A"
    return f"{numeric:.2f}%" if percent else f"{numeric:.2f}"


def _display_signal(signal: str | None) -> str:
    return str(signal or "neutral").replace("_", " ").title()


def build_market_selector(processing_output: Mapping[str, Any], selection: Mapping[str, Any] | None = None) -> dict[str, Any]:
    selection = dict(selection or resolve_prices_selection(processing_output))
    return {"selector_id": "prices_market", "selected": selection["selected_market"], "options": list(selection["available_markets"])}


def build_timeframe_selector(processing_output: Mapping[str, Any], selection: Mapping[str, Any] | None = None) -> dict[str, Any]:
    selection = dict(selection or resolve_prices_selection(processing_output))
    return {"selector_id": "prices_timeframe", "selected": selection["selected_timeframe"], "options": list(selection["available_timeframes"])}


def _candle_metadata(*, timeframe: str, records: list[Mapping[str, Any]], reference_timestamp: int | None,
                     display_window: int = DEFAULT_DISPLAY_WINDOW) -> dict[str, Any]:
    last     = records[-1] if records else {}
    interval = TIMEFRAME_SECONDS[timeframe]
    canonical_source, canonical_expected = TIMEFRAME_SOURCES[timeframe]
    records_used = int(last.get("source_records", canonical_expected if last else 0))
    expected     = int(last.get("expected_source_records", canonical_expected))
    source       = str(last.get("source_timeframe", canonical_source))
    is_closed    = bool(last.get("is_closed", reference_timestamp is not None and int(last.get("timestamp", reference_timestamp)) + interval <= reference_timestamp))
    is_partial   = bool(last.get("is_partial", not is_closed))
    returned     = records[-display_window:]
    return {"reference_timestamp": reference_timestamp, "data_as_of": _timestamp_iso(last.get("timestamp")) if last else None,
            "timeframe": timeframe, "bar_interval_seconds": interval, "source_timeframe": source, "resampled": source != timeframe,
            "is_closed": is_closed, "is_partial": is_partial, "records_expected": expected, "records_used": records_used,
            "coverage_complete": bool(records and records_used >= expected and not is_partial),
            "records_available": len(records), "records_returned": len(returned), "display_window": display_window,
            "history_truncated": len(records) > len(returned), "first_available_timestamp": records[0].get("timestamp") if records else None,
            "last_available_timestamp": last.get("timestamp") if last else None, "first_returned_timestamp": returned[0].get("timestamp") if returned else None,
            "last_returned_timestamp": returned[-1].get("timestamp") if returned else None}


def _tail_series(series: Mapping[str, Any], limit: int) -> dict[str, Any]:
    return {name: (list(values[-limit:]) if limit else []) if isinstance(values, list) else deepcopy(values) for name, values in series.items()}


def _trim_indicator_package(package: Mapping[str, Any], limit: int) -> dict[str, Any]:
    output = deepcopy(dict(package))
    if isinstance(output.get("timestamps"), list):
        output["timestamps"] = output["timestamps"][-limit:] if limit else []
    if isinstance(output.get("series"), Mapping):
        output["series"] = _tail_series(output["series"], limit)
    return output


def _ohlcv_overlays(indicators: Mapping[str, Any], limit: int) -> dict[str, Any]:
    moving    = indicators.get("moving_averages", {})
    bands     = indicators.get("bollinger_bands", {})
    fibonacci = indicators.get("fibonacci_levels", {})
    regression = indicators.get("regression_channel", {})
    support_resistance = indicators.get("support_resistance", {})
    sr_current = support_resistance.get("current", {}) if isinstance(support_resistance, Mapping) else {}
    overlays  = {
        "moving_averages": {"alignment": "ohlcv_records_by_index", "series": _tail_series(moving.get("series", {}), limit), "parameters": deepcopy(moving.get("parameters", {})),
                            "status": moving.get("quality", {}).get("status", "unavailable")},
        "bollinger_bands": {"alignment": "ohlcv_records_by_index", "series": _tail_series(bands.get("series", {}), limit), "parameters": deepcopy(bands.get("parameters", {})),
                            "status": bands.get("quality", {}).get("status", "unavailable")},
        "fibonacci_levels": {
            "render_mode": "horizontal_levels",
            "current": deepcopy(fibonacci.get("current", {})),
            "parameters": {**deepcopy(fibonacci.get("parameters", {})),
                           "ratios": [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0],
                           "mode": "processing_calculated"},
            "unit": "USDT",
            "status": "available" if fibonacci.get("quality", {}).get("status") == "ok" else "unavailable",
            "data_mode": "processing",
            "is_proxy": False,
            "reason": None if fibonacci.get("quality", {}).get("status") == "ok" else fibonacci.get("quality", {}).get("status", "fibonacci_unavailable"),
        },
        "regression_channel": {
            "alignment": "ohlcv_records_by_index",
            "series": _tail_series(regression.get("series", {}), limit),
            "parameters": deepcopy(regression.get("parameters", {})),
            "status": regression.get("quality", {}).get("status", "unavailable"),
            "reason": None if regression.get("quality", {}).get("status") == "ok" else regression.get("quality", {}).get("status", "unavailable"),
            "quality": deepcopy(regression.get("quality", {})),
            "provenance": {"owner": "Prices Processing", "calculation": "rolling_ordinary_least_squares", "recalculate_in_hmi": False},
        },
    }
    # Processor publishes support/resistance under ``current.support`` and
    # ``current.resistance``. Screen A consumes two levels per side.
    # contract, so normalize the Processing result here without HMI math.
    supports = list(sr_current.get("support", sr_current.get("support_levels", [])) or [])
    resistances = list(sr_current.get("resistance", sr_current.get("resistance_levels", [])) or [])
    support_levels = {f"S{i+1}": value for i, value in enumerate(supports[:1])}
    resistance_levels = {f"R{i+1}": value for i, value in enumerate(resistances[:1])}
    sr_parameters = deepcopy(support_resistance.get("parameters", {})) if isinstance(support_resistance, Mapping) else {}
    sr_parameters = {"level_count": 1, "mode": "processing_calculated", **sr_parameters}
    overlays["support"] = {
        "render_mode": "horizontal_levels",
        "current": {"levels": support_levels},
        "parameters": sr_parameters,
        "unit": "USDT",
        "status": "available" if len(support_levels) == 1 else "unavailable",
        "data_mode": "processing",
        "is_proxy": False,
        "reason": None if len(support_levels) == 1 else "support_levels_unavailable",
    }
    overlays["resistance"] = {
        "render_mode": "horizontal_levels",
        "current": {"levels": resistance_levels},
        "parameters": deepcopy(sr_parameters),
        "unit": "USDT",
        "status": "available" if len(resistance_levels) == 1 else "unavailable",
        "data_mode": "processing",
        "is_proxy": False,
        "reason": None if len(resistance_levels) == 1 else "resistance_levels_unavailable",
    }
    for overlay_id in ("pivot_points", "vwap"):
        overlays[overlay_id] = {"status": "unavailable", "reason": "not_available_in_prices_processing"}
    return overlays


def _event_uid(event: Mapping[str, Any]) -> str:
    source = event.get("source", {})
    return f"{source.get('market')}:{source.get('timeframe')}:{event.get('timestamp')}:{event.get('event_type')}:{event.get('event_id')}"


def _processing_records(processing_output: Mapping[str, Any], market: str, timeframe: str) -> list[Mapping[str, Any]]:
    records = processing_output.get("markets", {}).get(market, {}).get("timeframes", {}).get(timeframe, {}).get("records", [])
    return records or processing_output.get("features", {}).get("main_ohlcv", {}).get(market, {}).get("timeframes", {}).get(timeframe, {}).get("records", [])


def _visible_timestamp_sets(processing_output: Mapping[str, Any], display_window: int = DEFAULT_DISPLAY_WINDOW) -> dict[str, dict[str, set[int]]]:
    return {market: {timeframe: {int(record["timestamp"]) for record in _processing_records(processing_output, market, timeframe)[-display_window:]}
                     for timeframe in TIMEFRAME_ORDER} for market in MARKET_ORDER}


def _event_registry(classification_output: Mapping[str, Any], processing_output: Mapping[str, Any]) -> dict[str, Any]:
    events   = classification_output.get("events", {})
    by_id    = {}
    type_ids = {"technical_cross_ids": [], "candlestick_pattern_ids": []}
    visible  = _visible_timestamp_sets(processing_output)
    for group, id_group in (("technical_crosses", "technical_cross_ids"), ("candlestick_patterns", "candlestick_pattern_ids")):
        for market in MARKET_ORDER:
            for timeframe in TIMEFRAME_ORDER:
                for event in events.get(group, {}).get(market, {}).get(timeframe, []):
                    if int(event.get("timestamp", -1)) not in visible[market][timeframe]:
                        continue
                    uid = _event_uid(event)
                    by_id[uid] = {"event_uid": uid, **deepcopy(event)}
                    if uid not in type_ids[id_group]:
                        type_ids[id_group].append(uid)
    return {"by_id": by_id, **type_ids}


def _chart_annotations(classification_output: Mapping[str, Any], processing_output: Mapping[str, Any]) -> dict[str, Any]:
    registry = _event_registry(classification_output, processing_output)
    output   = {}
    for market in MARKET_ORDER:
        output[market] = {}
        for timeframe in TIMEFRAME_ORDER:
            event_ids = [uid for uid, event in registry["by_id"].items()
                         if event.get("source", {}).get("market") == market and event.get("source", {}).get("timeframe") == timeframe]
            timestamps = sorted({registry["by_id"][uid].get("timestamp") for uid in event_ids})
            output[market][timeframe] = {"by_timestamp": {str(timestamp): [uid for uid in event_ids if registry["by_id"][uid].get("timestamp") == timestamp]
                                                                  for timestamp in timestamps}}
    return output



def _volume_side_row(record: Mapping[str, Any]) -> dict[str, Any]:
    direct_buy = _finite(record.get("buy_volume_usd"))
    direct_sell = _finite(record.get("sell_volume_usd"))
    if direct_buy is not None and direct_sell is not None and direct_buy >= 0 and direct_sell >= 0:
        total = direct_buy + direct_sell
        return {
            "timestamp": record.get("timestamp"),
            "buy_volume_usd": direct_buy,
            "sell_volume_usd": direct_sell,
            "buy_share": direct_buy / total if total > 0 else None,
            "sell_share": direct_sell / total if total > 0 else None,
            "is_proxy": False,
        }
    high = _finite(record.get("high")); low = _finite(record.get("low")); close = _finite(record.get("close")); volume = _finite(record.get("volume_usd"))
    if high is None or low is None or close is None or volume is None:
        return {"timestamp": record.get("timestamp"), "buy_volume_usd": None, "sell_volume_usd": None, "buy_share": None, "sell_share": None, "is_proxy": True}
    span = high - low
    buy_share = 0.5 if span <= 0 else max(0.0, min(1.0, (close - low) / span)); sell_share = 1.0 - buy_share
    return {"timestamp": record.get("timestamp"), "buy_volume_usd": volume * buy_share, "sell_volume_usd": volume * sell_share,
            "buy_share": buy_share, "sell_share": sell_share, "is_proxy": True}


def _volume_side_summary(rows: list[Mapping[str, Any]], *, expected: int | None = None) -> dict[str, Any]:
    valid = [row for row in rows if _finite(row.get("buy_volume_usd")) is not None and _finite(row.get("sell_volume_usd")) is not None]
    buy = sum(float(row["buy_volume_usd"]) for row in valid)
    sell = sum(float(row["sell_volume_usd"]) for row in valid)
    total = buy + sell
    out = {"records_used": len(valid), "buy_volume_usd": buy if valid else None, "sell_volume_usd": sell if valid else None,
           "total_volume_usd": total if valid else None, "buy_share": (buy / total) if total else None, "sell_share": (sell / total) if total else None}
    if expected is not None:
        out = {"records_expected": expected, **out}
    return out


def _volume_by_side_contract(records: list[Mapping[str, Any]]) -> dict[str, Any]:
    visible = [_volume_side_row(row) for row in records]
    summary = _volume_side_summary(visible)
    current = deepcopy(visible[-1]) if visible else {"timestamp": None, "buy_volume_usd": None, "sell_volume_usd": None, "buy_share": None, "sell_share": None}
    return {
        "alignment": "ohlcv_records_by_index", "status": "available" if visible else "unavailable", "reason": None if visible else "records_unavailable",
        "unit": "USD", "source_volume_field": "buy_volume_usd/sell_volume_usd",
        "method": "cvd_taker_buy_sell_when_aligned_else_candle_position_proxy",
        "is_proxy": any(bool(row.get("is_proxy")) for row in visible),
        "series": {"buy_volume_usd": [row.get("buy_volume_usd") for row in visible], "sell_volume_usd": [row.get("sell_volume_usd") for row in visible],
                   "buy_volume_share": [row.get("buy_share") for row in visible], "sell_volume_share": [row.get("sell_share") for row in visible]},
        "current": current, "summary": summary,
        "presentation": {"component": "mirrored_volume_bars", "zero_line": True, "buy": "positive", "sell": "negative", "same_bar_width": True,
                         "symmetric_axis": True, "annotations": True, "summary_box": True},
        "timestamps": [row.get("timestamp") for row in visible],
    }

def build_main_ohlcv_chart(processing_output: Mapping[str, Any], classification_output: Mapping[str, Any] | None = None,
                           selection: Mapping[str, Any] | None = None) -> dict[str, Any]:
    selection           = dict(selection or resolve_prices_selection(processing_output))
    markets             = deepcopy(processing_output.get("features", {}).get("main_ohlcv", {}))
    indicators          = processing_output.get("features", {}).get("indicators", {})
    reference_timestamp = processing_output.get("context", {}).get("reference_timestamp")
    for market in MARKET_ORDER:
        for timeframe in TIMEFRAME_ORDER:
            timeframe_data = markets.setdefault(market, {}).setdefault("timeframes", {}).setdefault(timeframe, {"records": [], "unavailable_records": []})
            full_records   = timeframe_data.get("records", [])
            timeframe_data["metadata"] = _candle_metadata(timeframe=timeframe, records=full_records, reference_timestamp=reference_timestamp)
            timeframe_data["records"]  = deepcopy(full_records[-DEFAULT_DISPLAY_WINDOW:])
            timeframe_data["overlays"] = _ohlcv_overlays(indicators.get(market, {}).get(timeframe, {}), len(timeframe_data["records"]))
            timeframe_data["volume_by_side"] = _volume_by_side_contract(timeframe_data["records"])
            timeframe_data["calculation_history"] = {
                "calculation_records": len(full_records), "minimum_warmup_records": 200, "maximum_standard_indicator_period": 200,
                "all_visible_moving_averages_warm": len(full_records) >= 200, "technical_indicators_precomputed": True, "hmi_recalculation": False,
                "synthetic_fixture": bool(processing_output.get("context", {}).get("is_demo", False)), "fixture_seed": 20260807,
                "visible_records": len(timeframe_data["records"]), "resolution": timeframe,
            }
    return {"chart_id": "prices_main_ohlcv", "selected_market": selection["selected_market"], "available_markets": list(selection["available_markets"]),
            "selected_timeframe": selection["selected_timeframe"], "available_timeframes": list(selection["available_timeframes"]), "markets": deepcopy(markets),
            "optional_overlays": {"spot_close": True, "regression_channel": True, "moving_average_cross_markers": True,
                                  "regression_bollinger_cross_markers": True, "buy_sell_volume_split": True},
            "annotations": _chart_annotations(classification_output or {}, processing_output)}


def _indicator_chart(indicator_id: str, processing_output: Mapping[str, Any], selection: Mapping[str, Any],
                     thresholds: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    indicators = processing_output.get("features", {}).get("indicators", {})
    markets    = {market: {timeframe: _trim_indicator_package(indicators.get(market, {}).get(timeframe, {}).get(indicator_id, {}),
                                                                 min(DEFAULT_DISPLAY_WINDOW, len(_processing_records(processing_output, market, timeframe))))
                        for timeframe in TIMEFRAME_ORDER} for market in MARKET_ORDER}
    selected_package = markets.get(selection["selected_market"], {}).get(selection["selected_timeframe"], {})
    selected_dynamic_lines = deepcopy(selected_package.get("reference_lines", [])) if indicator_id in {"rsi", "tsi"} else None
    chart_thresholds = selected_dynamic_lines if selected_dynamic_lines is not None else (thresholds or [])
    scales = {"rsi": (0.0, 100.0, "%"), "stochastic": (0.0, 100.0, "%"),
              "tsi": (-100.0, 100.0, "index")}
    scale = ({"min": scales[indicator_id][0], "max": scales[indicator_id][1], "unit": scales[indicator_id][2],
              "basis": "fixed_oscillator_domain"} if indicator_id in scales else
             {"min": None, "max": None, "unit": "numeric", "basis": "data_driven"})
    threshold_basis = (
        "observed_min_plus_fraction_of_observed_range_precomputed" if indicator_id in {"rsi", "tsi"}
        else "fixed_normalized_oscillator_domain" if thresholds
        else "not_applicable"
    )
    return {"chart_id": indicator_id, "selected_market": selection["selected_market"], "selected_timeframe": selection["selected_timeframe"],
            "available_markets": list(selection["available_markets"]), "available_timeframes": list(selection["available_timeframes"]), "markets": markets,
            "scale": scale, "thresholds": chart_thresholds, "threshold_basis": threshold_basis}


def build_indicator_charts(processing_output: Mapping[str, Any], selection: Mapping[str, Any] | None = None) -> dict[str, Any]:
    selection = dict(selection or resolve_prices_selection(processing_output))
    return {
        "rsi": _indicator_chart("rsi", processing_output, selection),
        "macd": _indicator_chart("macd", processing_output, selection), "stochastic": _indicator_chart("stochastic", processing_output, selection,
            [{"value": STOCHASTIC_OVERBOUGHT, "role": "overbought"}, {"value": STOCHASTIC_OVERSOLD, "role": "oversold"}]),
        "adx": _indicator_chart("adx", processing_output, selection),
        "cci": _indicator_chart("cci", processing_output, selection),
        "mfi": _indicator_chart("mfi", processing_output, selection),
        "williams_r": _indicator_chart("williams_r", processing_output, selection),
        "atr": _indicator_chart("atr", processing_output, selection),
        "tsi": _indicator_chart("tsi", processing_output, selection),
        "wasserstein_distance": _indicator_chart("wasserstein_distance", processing_output, selection),
        "bollinger_band_width": _indicator_chart("bollinger_band_width", processing_output, selection),
    }


def _indicator_row(metric_id: str, label: str, classification: Mapping[str, Any]) -> dict[str, Any]:
    value_fields  = {"macd": "macd", "macd_signal": "value", "macd_histogram": "value"}
    value         = classification.get(value_fields.get(metric_id, "value"))
    display_field = INDICATOR_DISPLAY_FIELD[metric_id]
    display_value = classification.get(display_field, "unavailable")
    parameters    = deepcopy(classification.get("parameters", {}))
    if metric_id == "tsi":
        parameters = {"long_period": parameters.get("long_period", parameters.get("slow_period")),
                      "short_period": parameters.get("short_period", parameters.get("fast_period"))}
    signal = classification.get("signal", "neutral")
    return {"metric_id": metric_id, "label": label, "value": _finite(value), "display_value": _display(value),
            "signal": signal, "signal_color_token": signal if signal in {"bullish", "bearish", "neutral", "positive", "negative"} else classification.get("color_token", "neutral"),
            "display_signal": _display_signal(display_value), "display_color_token": str(display_value or "neutral"),
            "state": classification.get("state", "unavailable"), "color_token": classification.get("color_token", "neutral"),
            "confidence": classification.get("confidence", 0.0), "parameters": parameters}


def build_indicators_metrics_table(classification_output: Mapping[str, Any], selection: Mapping[str, Any] | None = None) -> dict[str, Any]:
    selection = dict(selection or {"selected_market": DEFAULT_MARKET, "selected_timeframe": DEFAULT_TIMEFRAME})
    source    = classification_output.get("indicator_signals", {})
    markets   = {market: {timeframe: [_indicator_row(metric_id, label, source.get(market, {}).get(timeframe, {}).get(metric_id, {}))
                                   for metric_id, label in INDICATOR_ROWS] for timeframe in TIMEFRAME_ORDER} for market in MARKET_ORDER}
    selected_market, selected_timeframe = selection["selected_market"], selection["selected_timeframe"]
    return {"table_id": "prices_indicator_package", "selected_market": selected_market, "selected_timeframe": selected_timeframe,
            "rows": deepcopy(markets[selected_market][selected_timeframe]), "markets": markets}


def build_technical_bias_table(classification_output: Mapping[str, Any], selection: Mapping[str, Any] | None = None) -> dict[str, Any]:
    selection = dict(selection or {"selected_market": DEFAULT_MARKET})
    source    = classification_output.get("technical_bias", {})
    groups    = (("overall", "Overall Bias"), ("short", "Short (5m–15m)"), ("mid", "Mid (15m)"), ("long", "Long (4h)"))
    markets   = {market: [{"metric_id": group, "label": label, "score": _finite(source.get(market, {}).get(group, {}).get("score")),
                         "signal": source.get(market, {}).get(group, {}).get("label", "neutral"),
                         "display_signal": _display_signal(source.get(market, {}).get(group, {}).get("label")),
                         "confidence": _finite(source.get(market, {}).get(group, {}).get("confidence"))}
                        for group, label in groups] for market in MARKET_ORDER}
    selected_market = selection["selected_market"]
    return {"table_id": "prices_technical_bias", "selected_market": selected_market, "rows": deepcopy(markets[selected_market]), "markets": markets}


def _statistical_row(metric_id: str, label: str, classification: Mapping[str, Any]) -> dict[str, Any]:
    value    = classification.get("price_value") if metric_id in {"var_95", "cvar_95"} and classification.get("price_value") is not None else classification.get("value")
    percent  = metric_id in {"win_rate", "max_drawdown"}
    metadata = classification.get("metadata", {})
    is_dual  = metric_id in {"standard_deviation", "var_95", "cvar_95"}
    return {"metric_id": metric_id, "label": label, "value": _finite(value), "return_value": _finite(classification.get("return_value", classification.get("value"))) if is_dual else None,
            "unit": "quote_currency" if is_dual else ("decimal" if percent else "numeric"),
            "classification_unit": "decimal_return" if is_dual else None,
            "display_basis": metadata.get("display_basis", "price" if metric_id in {"var_95", "cvar_95"} else "close" if metric_id == "standard_deviation" else None),
            "classification_basis": metadata.get("classification_basis", "historical_simple_returns" if metric_id in {"var_95", "cvar_95"} else "simple_returns" if metric_id == "standard_deviation" else None),
            "confidence_level": 0.95 if metric_id in {"var_95", "cvar_95"} else None, "display_value": _display(value, percent=percent),
            "signal": classification.get("signal", "neutral"), "state": classification.get("state", "unavailable"),
            "display_signal": _display_signal(classification.get("state")), "display_color_token": classification.get("state", "neutral"),
            "signal_color_token": classification.get("signal", "neutral"), "color_token": classification.get("color_token", "neutral"),
            "metadata": deepcopy(metadata)}


def build_statistical_performance_table(classification_output: Mapping[str, Any], selection: Mapping[str, Any] | None = None) -> dict[str, Any]:
    selection = dict(selection or {"selected_market": DEFAULT_MARKET, "selected_timeframe": DEFAULT_TIMEFRAME})
    source    = classification_output.get("statistical_signals", {})
    markets   = {market: {timeframe: [_statistical_row(metric_id, label, source.get(market, {}).get(timeframe, {}).get(metric_id, {}))
                                   for metric_id, label in STATISTICAL_ROWS] for timeframe in TIMEFRAME_ORDER} for market in MARKET_ORDER}
    selected_market, selected_timeframe = selection["selected_market"], selection["selected_timeframe"]
    metadata = source.get(selected_market, {}).get(selected_timeframe, {}).get("metadata", {})
    return {"table_id": "prices_statistical_performance", "selected_market": selected_market, "selected_timeframe": selected_timeframe,
            "rows": deepcopy(markets[selected_market][selected_timeframe]), "markets": markets, "metadata": deepcopy(metadata)}


def build_spot_futures_comparison_panel(processing_output: Mapping[str, Any], classification_output: Mapping[str, Any],
                                        selection: Mapping[str, Any] | None = None) -> dict[str, Any]:
    selection = dict(selection or resolve_prices_selection(processing_output))
    return {"panel_id": "spot_futures", "selected_market": selection["selected_market"], "selected_timeframe": selection["selected_timeframe"],
            "numeric": deepcopy(processing_output.get("features", {}).get("spot_futures_comparison", {})),
            "classification": deepcopy(classification_output.get("market_relationship", {}))}


def _technical_event_group(event_id: str) -> tuple[str, str | None]:
    if event_id.startswith(("ema_", "sma_", "wma_")):
        return "moving_average_cross", None
    if event_id.startswith("regression_") and "bollinger_" in event_id:
        return "channel_cross", None
    if event_id.startswith("macd_"):
        return "macd_cross", "macd"
    if event_id in {"k_above_d", "k_below_d"}:
        return "stochastic_cross", "stochastic"
    if event_id in {"di_plus_above_di_minus", "di_plus_below_di_minus"}:
        return "adx_cross", "adx"
    return "indicator_cross", None


def _enrich_prices_event(event: Mapping[str, Any]) -> dict[str, Any]:
    enriched = deepcopy(dict(event))
    if enriched.get("event_type") != "technical_cross":
        return enriched
    event_id = str(enriched.get("event_id") or "")
    group, indicator_id = _technical_event_group(event_id)
    enriched["event_group"] = group
    if indicator_id is not None:
        enriched["indicator_id"] = indicator_id
    enriched["label"] = str(enriched.get("label") or event_id.replace("_", " ")).upper()
    calculation = deepcopy(enriched.get("calculation", {}))
    if calculation.get("first_series") == "regression_middle":
        calculation["first_series"] = "regression_channel.middle"
    if calculation.get("second_series") == "bollinger_middle":
        calculation["second_series"] = "bollinger_bands.middle"
    calculation.pop("raw_direction", None)
    enriched["calculation"] = calculation
    enriched["display"] = {
        "screen_a": group in {"moving_average_cross", "channel_cross"},
        "screen_b": True,
    }
    return enriched


def build_prices_events(classification_output: Mapping[str, Any], processing_output: Mapping[str, Any]) -> dict[str, Any]:
    registry = _event_registry(classification_output, processing_output)
    enriched_by_id = {uid: _enrich_prices_event(event) for uid, event in registry["by_id"].items()}

    # The final SP exposes stochastic K/D arrows only inside its 20/80 event
    # zones.  Filter here in Contract Builder so the HMI never recomputes the
    # gate and never receives ineligible stochastic arrow events.
    filtered_by_id: dict[str, Any] = {}
    for uid, event in enriched_by_id.items():
        if event.get("event_group") == "stochastic_cross":
            calc = event.get("calculation", {})
            first = _finite(calc.get("first_value"))
            second = _finite(calc.get("second_value"))
            signal = event.get("signal")
            eligible = (
                first is not None and second is not None and
                ((signal == "bullish" and first <= 20.0 and second <= 20.0) or
                 (signal == "bearish" and first >= 80.0 and second >= 80.0))
            )
            if not eligible:
                continue
        filtered_by_id[uid] = event
    registry["by_id"] = filtered_by_id
    registry["technical_cross_ids"] = [uid for uid in registry["technical_cross_ids"] if uid in filtered_by_id]
    registry["candlestick_pattern_ids"] = [uid for uid in registry["candlestick_pattern_ids"] if uid in filtered_by_id]
    technical = registry["technical_cross_ids"]
    registry["moving_average_cross_ids"] = [
        uid for uid in technical if registry["by_id"].get(uid, {}).get("event_group") == "moving_average_cross"
    ]
    registry["channel_cross_ids"] = [
        uid for uid in technical if registry["by_id"].get(uid, {}).get("event_group") == "channel_cross"
    ]
    registry["technical_cross_policy"] = {
        "moving_average_families": {"ema": ["ema_9", "ema_21"], "sma": ["sma_20", "sma_50"], "wma": ["wma_20", "wma_50"]},
        "supported_moving_average_pairs": [
            {"family": "ema", "first_series": "ema_9", "second_series": "ema_21"},
            {"family": "sma", "first_series": "sma_20", "second_series": "sma_50"},
            {"family": "wma", "first_series": "wma_20", "second_series": "wma_50"},
        ],
        "pair_generation": "same_family_only", "cross_family_policy": "forbidden", "moving_average_pair_count": 3,
        "channel_pairs": [{"first_series": "regression_channel.middle", "second_series": "bollinger_bands.middle",
                           "selection_requirements": ["regression_channel", "bollinger_bands"]}],
        "cross_rule": {"above": "previous_difference <= 0 and current_difference > 0",
                       "below": "previous_difference >= 0 and current_difference < 0"},
        "exact_interpolation": True,
        "event_groups": ["moving_average_cross", "channel_cross"],
    }

    coverage: dict[str, Any] = {"spot": {}}
    stochastic: dict[str, Any] = {}
    for timeframe in TIMEFRAME_ORDER:
        events = [
            registry["by_id"][uid] for uid in technical
            if registry["by_id"].get(uid, {}).get("source", {}).get("market") == "spot"
            and registry["by_id"].get(uid, {}).get("source", {}).get("timeframe") == timeframe
        ]
        ma_events = [event for event in events if event.get("event_group") == "moving_average_cross"]
        channel_events = [event for event in events if event.get("event_group") == "channel_cross"]
        coverage["spot"][timeframe] = {
            "moving_average_cross_events": len(ma_events),
            "channel_cross_events": len(channel_events),
            "ema_cross_events": sum(str(event.get("event_id", "")).startswith("ema_") for event in ma_events),
            "sma_cross_events": sum(str(event.get("event_id", "")).startswith("sma_") for event in ma_events),
            "wma_cross_events": sum(str(event.get("event_id", "")).startswith("wma_") for event in ma_events),
        }
        stoch_events = [event for event in events if event.get("event_group") == "stochastic_cross"]
        buy = 0
        sell = 0
        for event in stoch_events:
            calc = event.get("calculation", {})
            first = _finite(calc.get("first_value"))
            second = _finite(calc.get("second_value"))
            if first is None or second is None:
                continue
            if event.get("signal") == "bullish" and first <= 20.0 and second <= 20.0:
                buy += 1
            if event.get("signal") == "bearish" and first >= 80.0 and second >= 80.0:
                sell += 1
        if buy or sell:
            stochastic[timeframe] = {
                "buy_cross_events_lte_20": buy,
                "sell_cross_events_gte_80": sell,
                "total_filtered_stochastic_events": buy + sell,
            }
    registry["cross_coverage_by_market_timeframe"] = coverage
    registry["screen_a_display_policy"] = {
        "policy_id": "prices_same_family_cross_priority_v2",
        "mode": "highest_eligible_same_family_score_per_cluster",
        "cluster_partition": ["market", "timeframe", "signal"],
        "cluster_window_candles": 2,
        "max_visible_markers_per_cluster": 1,
        "selection_aware": True,
        "priority_field": "importance.score",
        "rank_field": "display.screen_a.rank_in_cluster",
        "supported_event_groups": ["moving_average_cross", "channel_cross"],
        "allowed_moving_average_families": ["ema", "sma", "wma"],
        "mixed_family_crosses_allowed": False,
        "score_components": ["time_horizon", "period_separation", "canonical_pair_bonus", "normalized_cross_intensity", "channel_structural_weight"],
    }
    registry["screen_b_display_policy"] = {
        "policy_id": "prices_indicator_cross_markers_v1",
        "source": "events.technical_cross_ids",
        "recalculate_in_hmi": False,
        "cross_markers": {
            "macd": ["macd_above_signal", "macd_below_signal"],
            "stochastic": ["k_above_d", "k_below_d"],
            "adx": ["di_plus_above_di_minus", "di_plus_below_di_minus"],
        },
        "marker_mapping": {"bullish": "arrow_up", "bearish": "arrow_down"},
        "reference_lines": {
            "tsi": [
                {"value": 25.0, "role": "overbought", "label": "SOBRECOMPRA", "color_token": "bearish"},
                {"value": -25.0, "role": "oversold", "label": "SOBREVENTA", "color_token": "bullish"},
            ],
            "stochastic": [
                {"value": 80.0, "role": "overbought", "label": "VENTA", "color_token": "bearish"},
                {"value": 20.0, "role": "oversold", "label": "COMPRA", "color_token": "bullish"},
            ],
        },
        "stochastic_zone_filter": {"policy_id": "stochastic_cross_zone_20_80_v2", "recalculate_in_hmi": False},
    }
    registry["stochastic_cross_coverage"] = {
        "market": "spot",
        "thresholds": {"buy_max": 20.0, "sell_min": 80.0},
        "by_timeframe": stochastic,
    }
    return registry


def _buy_sell_projection(processing_output: Mapping[str, Any], selection: Mapping[str, Any]) -> dict[str, Any]:
    """Expose Prices buy/sell volume using already-aligned CVD when available.

    Processing enriches canonical Spot candles with CoinGlass CVD taker buy/sell
    values when timestamps align.  Only candles without aligned CVD fall back to
    the local candle-position proxy; no additional provider endpoint is needed.
    """
    by_market_timeframe: dict[str, Any] = {"spot": {}}
    expected_24h = {"1m": 1440, "5m": 288, "15m": 96, "4h": 6}

    for timeframe in TIMEFRAME_ORDER:
        records = _processing_records(processing_output, "spot", timeframe)
        display_records = records[-DEFAULT_DISPLAY_WINDOW:]
        display_rows = [_volume_side_row(row) for row in display_records]
        expected = expected_24h[timeframe]
        window_records = records[-expected:] if expected > 0 else []
        window_rows = [_volume_side_row(row) for row in window_records]
        current = deepcopy(display_rows[-1]) if display_rows else {
            "timestamp": None, "buy_volume_usd": None, "sell_volume_usd": None,
            "buy_share": None, "sell_share": None,
        }
        status = "available" if display_rows else "unavailable"
        by_market_timeframe["spot"][timeframe] = {
            "status": status,
            "current": current,
            "display_window": _volume_side_summary(display_rows),
            "window_24h": _volume_side_summary(window_rows, expected=expected),
        }

    selected_timeframe = str(selection.get("selected_timeframe") or DEFAULT_TIMEFRAME)
    selected = by_market_timeframe["spot"].get(selected_timeframe, {})
    current = deepcopy(selected.get("current"))
    window_24h = deepcopy(selected.get("window_24h"))
    status = selected.get("status", "unavailable")
    selected_records = _processing_records(processing_output, "spot", selected_timeframe)[-DEFAULT_DISPLAY_WINDOW:]
    selected_rows = [_volume_side_row(row) for row in selected_records]
    proxy_count = sum(1 for row in selected_rows if bool(row.get("is_proxy", True)))
    direct_count = len(selected_rows) - proxy_count
    if direct_count and not proxy_count:
        method = "aligned_cvd_taker_buy_sell"
        is_proxy = False
    elif direct_count:
        method = "aligned_cvd_with_candle_position_fallback"
        is_proxy = True
    else:
        method = "candle_position_proxy"
        is_proxy = True
    return {
        "widget_id": "volume_buy_sell_split",
        "status": status,
        "reason": None if status == "available" else "records_unavailable",
        "unit": "USD",
        "selected_market": "spot",
        "selected_timeframe": selected_timeframe,
        "current": current,
        "window_24h": window_24h,
        "by_market_timeframe": by_market_timeframe,
        "data_mode": "synthetic" if bool(processing_output.get("context", {}).get("is_demo", False)) else "runtime",
        "is_proxy": is_proxy,
        "method": method,
        "presentation": {
            "chart_type": "mirrored_histogram", "buy_position": "above_zero",
            "sell_position": "below_zero", "sell_series_sign": "negative_for_display",
            "same_bar_width": True, "symmetric_axis": True, "zero_line": True,
            "annotations": False, "summary_box": False,
            "buy_color_token": "bullish", "sell_color_token": "bearish",
        },
        "source_paths": ["charts.ohlcv.markets.spot.timeframes.*.volume_by_side"],
    }


def _prices_history_contract(processing_output: Mapping[str, Any]) -> dict[str, Any]:
    counts = [len(_processing_records(processing_output, market, timeframe)) for market in MARKET_ORDER for timeframe in TIMEFRAME_ORDER]
    calculation_records = min(counts, default=0)
    return {"calculation_records": calculation_records, "minimum_warmup_records": 200, "maximum_standard_indicator_period": 200,
            "all_visible_moving_averages_warm": calculation_records >= 200, "technical_indicators_precomputed": True, "hmi_recalculation": False,
            "synthetic_fixture": False, "fixture_seed": None,
            "note": f"{calculation_records} calculation records per timeframe; HMI keeps current visible windows."}


def _kpi(metric_id: str, value: Any, *, unit: str, reason: str | None = None) -> dict[str, Any]:
    numeric = _finite(value)
    return {"metric_id": metric_id, "value": numeric, "unit": unit, "status": "available" if numeric is not None else "unavailable",
            **({"reason": reason or "insufficient_data"} if numeric is None else {})}


def _resolve_closed_24h_window(processing_output: Mapping[str, Any], market: str) -> dict[str, Any]:
    records       = processing_output.get("markets", {}).get(market, {}).get("timeframes", {}).get("15m", {}).get("records", [])
    closed        = [record for record in records if record.get("is_closed", True)]
    window        = closed[-96:]
    prior         = closed[-97] if len(closed) >= 97 else None
    current       = window[-1] if window else None
    volume_field  = "volume_usd"
    current_close = _finite(current.get("close")) if current else None
    prior_close   = _finite(prior.get("close")) if prior else None
    volume_24h    = sum(float(record.get(volume_field, 0.0) or 0.0) for record in window) if len(window) == 96 else None
    return {"records": window, "records_used": len(window), "close_24h_ago": prior_close,
            "change_percent": ((current_close / prior_close) - 1.0) * 100.0 if current_close is not None and prior_close not in (None, 0.0) else None,
            "high": max((float(record["high"]) for record in window), default=None) if len(window) == 96 else None,
            "low": min((float(record["low"]) for record in window), default=None) if len(window) == 96 else None,
            "volume": volume_24h, "average_volume": volume_24h / 96.0 if volume_24h is not None else None, "volume_field": volume_field}


def build_prices_kpis(processing_output: Mapping[str, Any], selection: Mapping[str, Any]) -> dict[str, Any]:
    market     = selection["selected_market"]
    timeframe  = selection["selected_timeframe"]
    records    = processing_output.get("markets", {}).get(market, {}).get("timeframes", {}).get(timeframe, {}).get("records", [])
    indicators = processing_output.get("features", {}).get("indicators", {}).get(market, {}).get(timeframe, {})
    dynamics = processing_output.get("features", {}).get("market_dynamics", {}).get(market, {})
    timeframe_dynamics = dynamics.get("timeframes", {}).get(timeframe, {}) if isinstance(dynamics, Mapping) else {}
    current_dynamics = timeframe_dynamics.get("current", {}) if isinstance(timeframe_dynamics, Mapping) else {}
    change_windows = dynamics.get("change_windows_percent", {}) if isinstance(dynamics, Mapping) else {}
    if not records:
        return {"selected_market": market, "selected_timeframe": timeframe,
                "items": [_kpi(metric_id, None, unit=unit) for metric_id, unit in (("last_price", "quote_currency"), ("high_24h", "quote_currency"),
                           ("low_24h", "quote_currency"), ("change_24h", "percent"), ("volume_24h", "quote_currency"),
                           ("market_cap", "quote_currency"), ("volatility_atr_percent", "percent"), ("average_range", "quote_currency"),
                           ("change_15m", "percent"), ("change_4h", "percent"), ("relative_volume_20", "ratio"),
                           ("volume_zscore_20", "decimal"), ("range_percent", "percent"), ("beta", "ratio"))]}
    last          = records[-1]
    last_close    = _finite(last.get("close"))
    window_24h    = _resolve_closed_24h_window(processing_output, market)
    window        = window_24h["records"]
    atr           = _finite(indicators.get("atr", {}).get("current", {}).get("atr"))
    atr_percent   = atr / last_close * 100.0 if atr is not None and last_close not in (None, 0.0) else None
    average_range = sum(float(record["high"]) - float(record["low"]) for record in window) / len(window) if window else None
    market_cap_payload = processing_output.get("provider_features", {}).get("market_cap", {})
    market_cap_current = market_cap_payload.get("current") if isinstance(market_cap_payload, Mapping) else None
    market_cap_value = _finite((market_cap_current or {}).get("value")) if isinstance(market_cap_current, Mapping) else None
    items         = [_kpi("last_price", last_close, unit="quote_currency"), _kpi("high_24h", window_24h["high"], unit="quote_currency"),
             _kpi("low_24h", window_24h["low"], unit="quote_currency"), _kpi("change_24h", window_24h["change_percent"], unit="percent"),
             _kpi("volume_24h", window_24h["volume"], unit="quote_currency"),
             {**_kpi("market_cap", market_cap_value, unit="USD", reason="glassnode_market_cap_unavailable"),
              "label": "Market Cap", "display_value": (f"{market_cap_value/1_000_000_000_000:.2f}T" if market_cap_value is not None and market_cap_value >= 1_000_000_000_000 else None),
              "source": {"provider": "glassnode", "metric": "market_cap", "role": "primary_feature"},
              "quality": {"data_mode": processing_output.get("context", {}).get("data_mode", "live"), "contract_ready": market_cap_value is not None},
              "provenance": {"provider": "glassnode", "metric": "marketcap_usd", "endpoint_id": market_cap_payload.get("endpoint_id", "marketcap_usd"),
                             "integration_state": "runtime_feed" if market_cap_value is not None else "unavailable"}},
             _kpi("volatility_atr_percent", atr_percent, unit="percent"), _kpi("average_range", average_range, unit="quote_currency"),
             _kpi("change_15m", change_windows.get("15m"), unit="percent", reason="insufficient_1m_history"),
             _kpi("change_4h", change_windows.get("4h"), unit="percent", reason="insufficient_1m_history"),
             _kpi("relative_volume_20", current_dynamics.get("relative_volume_20"), unit="ratio", reason="insufficient_volume_baseline"),
             _kpi("volume_zscore_20", current_dynamics.get("volume_zscore_20"), unit="decimal", reason="insufficient_volume_baseline"),
             _kpi("range_percent", current_dynamics.get("range_percent"), unit="percent", reason="current_candle_unavailable"),
             _kpi("beta", None, unit="ratio", reason="benchmark_series_not_available")]
    return {"selected_market": market, "selected_timeframe": timeframe, "window_seconds": 86_400,
            "records_used_24h": window_24h["records_used"], "items": items}


def _timestamp_iso(timestamp: Any) -> str | None:
    numeric = _finite(timestamp)
    return datetime.fromtimestamp(numeric, tz=UTC).isoformat() if numeric is not None else None


def build_prices_operational_context(processing_output: Mapping[str, Any], selection: Mapping[str, Any]) -> dict[str, Any]:
    metadata    = processing_output.get("context", {})
    records     = processing_output.get("markets", {}).get(selection["selected_market"], {}).get("timeframes", {}).get(selection["selected_timeframe"], {}).get("records", [])
    data_as_of  = _timestamp_iso(records[-1].get("timestamp")) if records else None
    symbol      = metadata.get("symbol") or "BTCUSDT"
    quote_asset = metadata.get("quote_asset") or "USDT"
    base_asset  = symbol[:-len(quote_asset)] if isinstance(symbol, str) and symbol.endswith(quote_asset) else symbol
    return {"symbol": symbol, "base_asset": base_asset, "quote_asset": quote_asset, "exchange": metadata.get("exchange"),
            "provider": metadata.get("provider"), "units": {"price": quote_asset, "volume": f"{quote_asset}_notional", "returns": "decimal"},
            "refresh_policy": {"mode": processing_output.get("mode"), "contract": "atomic_file_replace"},
            "history_policy": {"calculation": "full_available_history", "presentation": "tail_window", "default_display_window": DEFAULT_DISPLAY_WINDOW},
            "data_mode": metadata.get("data_mode", "live"), "is_demo": bool(metadata.get("is_demo", False)),
            "reference_timestamp": metadata.get("reference_timestamp"),
            "generated_at": _timestamp_iso(metadata.get("reference_timestamp")) or _timestamp_iso(processing_output.get("updated_at")),
            "selected_data_as_of": data_as_of, "data_as_of": data_as_of, "updated_at": processing_output.get("updated_at")}


def _price_change_windows(records: list[Mapping[str, Any]], *, change_24h: float | None) -> dict[str, Any]:
    if not records:
        return {"15m": None, "4h": None, "24h": change_24h}
    current   = _finite(records[-1].get("close"))
    current_t = int(records[-1]["timestamp"])
    changes   = {}
    for window, seconds in (("15m", 900), ("4h", 14_400)):
        candidates = [record for record in records if int(record["timestamp"]) <= current_t - seconds]
        previous   = _finite(candidates[-1].get("close")) if candidates else None
        changes[window] = ((current / previous) - 1.0) * 100.0 if current is not None and previous not in (None, 0.0) else None
    changes["24h"] = change_24h
    return changes


def _unavailable_widget(widget_id: str, reason: str) -> dict[str, Any]:
    return {"widget_id": widget_id, "status": "unavailable", "reason": reason}


def build_prices_widgets(processing_output: Mapping[str, Any], classification_output: Mapping[str, Any], selection: Mapping[str, Any],
                         cvd_processing_context: Mapping[str, Any] | None = None) -> dict[str, Any]:
    market       = selection["selected_market"]
    timeframe    = selection["selected_timeframe"]
    records      = processing_output.get("markets", {}).get(market, {}).get("timeframes", {}).get(timeframe, {}).get("records", [])
    indicators   = processing_output.get("features", {}).get("indicators", {}).get(market, {}).get(timeframe, {})
    last         = deepcopy(records[-1]) if records else None
    window_24h   = _resolve_closed_24h_window(processing_output, market)
    window       = window_24h["records"]
    volume_key   = window_24h["volume_field"]
    volumes      = [_finite(record.get(volume_key)) for record in window]
    volumes      = [value for value in volumes if value is not None]
    registry     = _event_registry(classification_output, processing_output)
    pattern_rows = [uid for uid in registry["candlestick_pattern_ids"]
                     if registry["by_id"][uid].get("source", {}).get("market") == market and registry["by_id"][uid].get("source", {}).get("timeframe") == timeframe]
    averages   = indicators.get("moving_averages", {})
    statistics = classification_output.get("statistical_signals", {}).get(market, {}).get(timeframe, {})
    dynamics = processing_output.get("features", {}).get("market_dynamics", {}).get(market, {})
    timeframe_dynamics = dynamics.get("timeframes", {}).get(timeframe, {}) if isinstance(dynamics, Mapping) else {}
    current_dynamics = timeframe_dynamics.get("current", {}) if isinstance(timeframe_dynamics, Mapping) else {}
    pivot_points = dynamics.get("pivot_points", {}) if isinstance(dynamics, Mapping) else {}
    returns_histogram = timeframe_dynamics.get("returns_histogram", {}) if isinstance(timeframe_dynamics, Mapping) else {}

    # Screen A consumes precomputed support/resistance for every timeframe.
    # Keep the selected pair at the widget root and the full map available for
    # one-click timeframe changes; the HMI never derives levels.
    sr_by_timeframe: dict[str, Any] = {}
    all_indicators = processing_output.get("features", {}).get("indicators", {}).get(market, {})
    for tf in TIMEFRAME_ORDER:
        sr_package = all_indicators.get(tf, {}).get("support_resistance", {})
        current = sr_package.get("current", {}) if isinstance(sr_package, Mapping) else {}
        supports = list(current.get("support", current.get("support_levels", [])) or [])
        resistances = list(current.get("resistance", current.get("resistance_levels", [])) or [])
        support_map = {f"S{i+1}": value for i, value in enumerate(supports[:1])}
        resistance_map = {f"R{i+1}": value for i, value in enumerate(resistances[:1])}
        sr_by_timeframe[tf] = {
            "support": support_map,
            "resistance": resistance_map,
            "status": "available" if len(support_map) == 1 and len(resistance_map) == 1 else "unavailable",
        }
    selected_sr = sr_by_timeframe.get(timeframe, {})
    sr_widget = {
        "widget_id": "support_resistance_zones",
        "status": selected_sr.get("status", "unavailable"),
        "selected_market": market,
        "selected_timeframe": timeframe,
        "by_market_timeframe": {market: sr_by_timeframe},
        "data_mode": "processing",
        "is_proxy": False,
        "reason": None if selected_sr.get("status") == "available" else "support_resistance_unavailable",
    }
    return {
        "price_change": {"widget_id": "price_change", "status": "available" if records else "unavailable", "unit": "percent",
                         "windows": _price_change_windows(records, change_24h=window_24h["change_percent"])},
        "moving_averages_summary": {"widget_id": "moving_averages_summary", "status": "available" if averages.get("current") else "unavailable",
                                    "values": deepcopy(averages.get("current", {})), "parameters": deepcopy(averages.get("parameters", {}))},
        "candlestick_patterns_analysis": {"widget_id": "candlestick_patterns_analysis", "status": "available", "row_ids": pattern_rows,
                                           "most_recent_id": pattern_rows[-1] if pattern_rows else None},
        "most_recent_candle": {"widget_id": "most_recent_candle", "status": "available" if last else "unavailable", "candle": last},
        "volume_analysis": {"widget_id": "volume_analysis", "status": "available" if volumes else "unavailable", "source_field": volume_key,
                            "current": volumes[-1] if volumes else None, "total_24h": window_24h["volume"],
                            "average_24h": window_24h["average_volume"], "records_used": window_24h["records_used"],
                            "relative_volume_20": current_dynamics.get("relative_volume_20"),
                            "volume_zscore_20": current_dynamics.get("volume_zscore_20"),
                            "baseline_mean_20": current_dynamics.get("volume_baseline_mean_20"),
                            "baseline_records": current_dynamics.get("volume_baseline_records")},
        "drawdown": {"widget_id": "drawdown", "status": "available" if _finite(statistics.get("max_drawdown", {}).get("value")) is not None else "unavailable",
                     "value": _finite(statistics.get("max_drawdown", {}).get("value")), "unit": "decimal", "basis": "market_returns"},
        "range_price_behavior": {"widget_id": "range_price_behavior", "status": "available" if window else "unavailable",
                                 "current_range": (float(last["high"]) - float(last["low"])) if last else None,
                                 "average_range_24h": sum(float(record["high"]) - float(record["low"]) for record in window) / len(window) if window else None,
                                 "high_24h": max((float(record["high"]) for record in window), default=None),
                                 "low_24h": min((float(record["low"]) for record in window), default=None),
                                 "current_range_percent": current_dynamics.get("range_percent"),
                                 "body_percent_of_range": current_dynamics.get("body_percent_of_range"),
                                 "upper_wick_percent_of_range": current_dynamics.get("upper_wick_percent_of_range"),
                                 "lower_wick_percent_of_range": current_dynamics.get("lower_wick_percent_of_range")},
        "volume_profile": _unavailable_widget("volume_profile", "requires_footprint_price_bins_not_new_endpoint"),
        "pivot_points_summary": {"widget_id": "pivot_points_summary", **deepcopy(pivot_points)},
        "support_resistance_zones": sr_widget,
        "distribution_histogram": {"widget_id": "distribution_histogram", **deepcopy(returns_histogram)},
        "correlation": _unavailable_widget("correlation", "benchmark_series_not_available"),
        "price_forecast": _unavailable_widget("price_forecast", "forecast_model_not_configured"),
        "volume_buy_sell_split": _buy_sell_projection(processing_output, selection),
    }


def _contains_nonfinite(value: Any) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, Mapping):
        return any(_contains_nonfinite(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_nonfinite(item) for item in value)
    return False


def validate_prices_screen_coverage(contract: Mapping[str, Any], processing_output: Mapping[str, Any] | None = None,
                                    classification_output: Mapping[str, Any] | None = None) -> dict[str, Any]:
    missing = []
    charts  = contract.get("charts", {})
    tables  = contract.get("tables", {}).get("indicators_metrics", {})
    # Canonical Prices Screen contract contains OHLCV + ten native Screen-B
    # charts (RSI, MACD, Stochastic, ADX, CCI, MFI, Williams %R, ATR, TSI,
    # Wasserstein) = 11 chart models total.  The old value 10 predated the
    # Wasserstein panel and incorrectly marked complete contracts as partial.
    if len(charts) != 11:
        missing.append("charts.count")
    if len(tables.get("indicator_package", {}).get("rows", [])) != 11:
        missing.append("tables.indicator_package.rows")
    if len(tables.get("technical_bias", {}).get("rows", [])) != 4:
        missing.append("tables.technical_bias.rows")
    if len(tables.get("statistical_performance", {}).get("rows", [])) != len(STATISTICAL_ROWS):
        missing.append("tables.statistical_performance.rows")
    if contract.get("context", {}).get("performance_basis") != "market_returns":
        missing.append("context.performance_basis")
    if any(row.get("label") == "TSI (14)" for row in tables.get("indicator_package", {}).get("rows", [])):
        missing.append("tables.indicator_package.tsi_parameters")
    tsi_rows = [row for row in tables.get("indicator_package", {}).get("rows", []) if row.get("metric_id") == "tsi"]
    if not tsi_rows or tsi_rows[0].get("parameters") != {"long_period": 25, "short_period": 13}:
        missing.append("tables.indicator_package.tsi.parameters")
    if _contains_nonfinite(contract):
        missing.append("nonfinite_values")
    selection = {"market": contract.get("context", {}).get("default_market"), "timeframe": contract.get("context", {}).get("default_timeframe")}
    for chart in charts.values():
        if chart.get("selected_market") != selection["market"] or chart.get("selected_timeframe") != selection["timeframe"]:
            missing.append(f"charts.{chart.get('chart_id', 'unknown')}.selection")
    if classification_output is not None:
        indicators = classification_output.get("indicator_signals", {})
        statistics = classification_output.get("statistical_signals", {})
        biases     = classification_output.get("technical_bias", {})
        for market in contract.get("context", {}).get("available_markets", []):
            for timeframe in contract.get("context", {}).get("available_timeframes", []):
                for metric_id, _ in INDICATOR_ROWS:
                    if metric_id not in indicators.get(market, {}).get(timeframe, {}):
                        missing.append(f"classification.indicator_signals.{market}.{timeframe}.{metric_id}")
                for metric_id, _ in STATISTICAL_ROWS:
                    if metric_id not in statistics.get(market, {}).get(timeframe, {}):
                        missing.append(f"classification.statistical_signals.{market}.{timeframe}.{metric_id}")
            for group in ("overall", "short", "mid", "long"):
                if group not in biases.get(market, {}):
                    missing.append(f"classification.technical_bias.{market}.{group}")
    registered_events = contract.get("events", {}).get("by_id", {})
    if any(not event.get("source", {}).get("market") or not event.get("source", {}).get("timeframe") for event in registered_events.values()):
        missing.append("events.by_id.source")
    return {"status": "partial" if missing else "ok", "is_complete": not missing, "missing_fields": sorted(set(missing)), "warnings": [], "errors": []}


def _combine_prices_quality(*qualities: Mapping[str, Any]) -> dict[str, Any]:
    precedence     = {"ok": 0, "partial": 1, "invalid": 2}
    status         = max((str(quality.get("status", "ok")) for quality in qualities), key=lambda item: precedence.get(item, 2), default="ok")
    missing_fields = sorted({str(item) for quality in qualities for item in quality.get("missing_fields", [])})
    warnings       = [str(item) for quality in qualities for item in quality.get("warnings", [])]
    errors         = [str(item) for quality in qualities for item in quality.get("errors", [])]
    if errors:
        status = "invalid"
    elif missing_fields or warnings:
        status = "partial" if status != "invalid" else status
    return {"status": status, "is_complete": status == "ok", "missing_fields": missing_fields,
            "warnings": warnings, "errors": errors,
            "sources": {"processing": deepcopy(dict(qualities[0])), "classification": deepcopy(dict(qualities[1])),
                        "screen_coverage": deepcopy(dict(qualities[2])), "serialization": deepcopy(dict(qualities[3]))}}


def _screen_availability(contract: Mapping[str, Any]) -> dict[str, int]:
    kpis    = contract.get("kpis", {}).get("items", [])
    widgets = list(contract.get("widgets", {}).values())
    charts  = list(contract.get("charts", {}).values())
    tables  = list(contract.get("tables", {}).get("indicators_metrics", {}).values())
    return {"kpis_available": sum(item.get("status") == "available" for item in kpis), "kpis_total": len(kpis),
            "widgets_available": sum(item.get("status") == "available" for item in widgets), "widgets_total": len(widgets),
            "charts_available": len(charts), "charts_total": len(charts), "tables_available": len(tables), "tables_total": len(tables)}


def _prices_quality_extensions(*, processing_output: Mapping[str, Any], classification_output: Mapping[str, Any], contract: Mapping[str, Any]) -> dict[str, Any]:
    events = contract.get("events", {}).get("by_id", {})
    technical = [event for event in events.values() if isinstance(event, Mapping) and event.get("event_type") == "technical_cross"]
    moving = [event for event in technical if event.get("event_group") == "moving_average_cross"]
    channel = [event for event in technical if event.get("event_group") == "channel_cross"]
    macd_events = [event for event in technical if event.get("event_group") == "macd_cross"]
    adx_events = [event for event in technical if event.get("event_group") == "adx_cross"]
    stochastic_events = [event for event in technical if event.get("event_group") == "stochastic_cross"]

    raw_stochastic: list[Mapping[str, Any]] = []
    for timeframe in TIMEFRAME_ORDER:
        raw_stochastic.extend(
            event for event in classification_output.get("events", {}).get("technical_crosses", {}).get("spot", {}).get(timeframe, [])
            if str(event.get("event_id", "")) in {"k_above_d", "k_below_d"}
        )
    retained_buy = sum(event.get("signal") == "bullish" for event in stochastic_events)
    retained_sell = sum(event.get("signal") == "bearish" for event in stochastic_events)

    def count_prefix(prefix: str) -> int:
        return sum(str(event.get("event_id", "")).startswith(prefix) for event in moving)

    one_hour = [event for event in technical if event.get("source", {}).get("timeframe") == "15m"]
    one_hour_counts = {
        "macd": sum(event.get("event_group") == "macd_cross" for event in one_hour),
        "adx_di": sum(event.get("event_group") == "adx_cross" for event in one_hour),
        "stochastic": sum(event.get("event_group") == "stochastic_cross" for event in one_hour),
    }
    history = contract.get("history_contract", {})
    records_per_timeframe = int(history.get("calculation_records") or 0)
    context = contract.get("context", {})
    is_demo = bool(context.get("is_demo", False))
    reference_timestamp = context.get("reference_timestamp")
    fixture_iso = None
    if is_demo and reference_timestamp is not None:
        try:
            fixture_iso = datetime.fromtimestamp(int(reference_timestamp), tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (TypeError, ValueError, OSError):
            fixture_iso = None

    return {
        "regression_channel": {
            "status": "available", "window": 100, "deviation_multiplier": 2.0,
            "channel_cross_event_count": len(channel), "market_scope": "spot",
        },
        "buy_sell_volume_split": {
            "status": contract.get("widgets", {}).get("volume_buy_sell_split", {}).get("status", "unavailable"),
            "method": "synthetic_candle_position_proxy", "is_proxy": True,
            "presentation": "mirrored_buy_above_sell_below", "market_scope": "spot",
        },
        "single_spot_price": {
            "status": "available", "market": "spot",
            "construction": "direct_spot_reference_series", "alternate_market_prices": False,
        },
        "same_family_moving_average_pairs": {
            "status": "available", "family_count": 3, "series_count": 6, "pair_count": 3,
            "event_count": len(moving),
            "events_by_family": {"ema": count_prefix("ema_"), "sma": count_prefix("sma_"), "wma": count_prefix("wma_")},
            "cross_family_policy": "same_family_only", "mixed_family_events_removed": 0, "market_scope": "spot",
        },
        "stochastic_cross_zone_filter": {
            "status": "available", "policy_id": "stochastic_cross_zone_20_80_v2",
            "buy_rule": "k <= 20 and d <= 20 and k crosses above d",
            "sell_rule": "k >= 80 and d >= 80 and k crosses below d",
            "events_before_filter": len(raw_stochastic), "events_after_filter": len(stochastic_events),
            "events_removed": max(0, len(raw_stochastic) - len(stochastic_events)),
            "retained_buy_events": retained_buy, "retained_sell_events": retained_sell, "recalculate_in_hmi": False,
        },
        "screen_b_arrow_contract_audit": {
            "status": "available",
            "policy": {
                "supported_indicators": ["macd", "adx", "stochastic"],
                "event_ids": {
                    "macd": ["macd_above_signal", "macd_below_signal"],
                    "adx": ["di_plus_above_di_minus", "di_plus_below_di_minus"],
                    "stochastic": ["k_above_d", "k_below_d"],
                },
                "stochastic_gate": {"bullish": "K crosses above D while K,D <= 20", "bearish": "K crosses below D while K,D >= 80"},
                "recalculate_in_hmi": False, "no_event_behavior": "show_no_arrow",
            },
            "default_context": {"market": "spot", "timeframe": "15m"},
            "default_context_event_counts": one_hour_counts,
        },
        "analysis_navigation_and_summary_v1": {
            "status": "available",
            "back_button": {"visible": True, "label": "← REGRESAR", "target_view": "main"},
            "summary_panel": {"visible": True, "position": "right", "width_px": 314, "mode": "single_market_summary", "source": "tables.indicators_metrics + precomputed classifications"},
            "global_view_selector_visible": False, "global_market_selector_visible": False, "hmi_computes_summary": False,
        },
        "screen_b_arrow_audit_v2": {
            "status": "available",
            "event_counts": {"macd": len(macd_events), "adx": len(adx_events), "stochastic": len(stochastic_events)},
            "required_groups": ["macd", "adx", "stochastic"], "renderer_policy": "contract_events_only", "no_hmi_cross_calculation": True,
        },
        "calculation_history_730_v1": {
            "status": "available", "records_per_timeframe": records_per_timeframe,
            "all_ma_series_warm_in_visible_window": records_per_timeframe >= 200, "recalculate_in_hmi": False,
        },
        "temporal_selector_contract_v3": {"selector_type": "TIMEFRAME", "options": list(TIMEFRAME_ORDER), "auto_refresh": True},
        "screen_a_price_levels_v1": {
            "status": "available",
            "scope": "prices_screen_a",
            "timeframes": list(TIMEFRAME_ORDER),
            "support_resistance": "processing_calculated",
            "fibonacci": "processing_calculated",
            "real_market_calculation": True,
            "hmi_recalculation": False,
            "note": "Support, resistance and Fibonacci are calculated upstream by Prices Processing.",
        },
        "realism_v1": {
            "status": "available", "fixture_as_of_timestamp": None,
            "fixture_as_of_iso": None,
            "deterministic_seed": None,
            "synthetic_not_live": is_demo,
            "common_as_of_contract": fixture_iso if is_demo else None,
        },
    }


def build_prices_screen_contract(processing_output: Mapping[str, Any], classification_output: Mapping[str, Any], *,
                                 cvd_processing_context: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if processing_output.get("family") != "prices_ohlcv" or classification_output.get("family") != "prices_ohlcv":
        raise ValueError("Prices screen contract requires prices_ohlcv inputs")
    if processing_output.get("stage") != "processing":
        raise ValueError("Prices screen contract requires stage=processing")
    if classification_output.get("stage") != "classification":
        raise ValueError("Prices screen contract requires stage=classification")
    selection = resolve_prices_selection(processing_output)
    charts    = {"ohlcv": build_main_ohlcv_chart(processing_output, classification_output, selection), **build_indicator_charts(processing_output, selection)}
    tables    = {"indicator_package": build_indicators_metrics_table(classification_output, selection),
                 "technical_bias": build_technical_bias_table(classification_output, selection),
                 "statistical_performance": build_statistical_performance_table(classification_output, selection)}
    performance_basis = classification_output.get("statistical_signals", {}).get(selection["selected_market"], {}).get(
        selection["selected_timeframe"], {}).get("metadata", {}).get("performance_basis")
    updated_at          = classification_output.get("updated_at") or processing_output.get("updated_at") or processing_output.get("context", {}).get("updated_at")
    operational_context = build_prices_operational_context(processing_output, selection)
    operational_context["updated_at"] = updated_at
    contract            = {"family": "prices_ohlcv", "screen": "prices", "schema_version": "1.9.0",
                "context": {"default_market": selection["selected_market"], "available_markets": list(selection["available_markets"]),
                            "default_timeframe": selection["selected_timeframe"], "available_timeframes": list(selection["available_timeframes"]),
                            "performance_basis": performance_basis, **operational_context},
                "badges": ([{"badge_id": "synthetic", "text": "SYNTHETIC"}] if operational_context["is_demo"] else []),
                "kpis": build_prices_kpis(processing_output, selection),
                "widgets": build_prices_widgets(processing_output, classification_output, selection, cvd_processing_context),
                "selectors": {"market": build_market_selector(processing_output, selection), "timeframe": build_timeframe_selector(processing_output, selection)},
                "charts": charts, "tables": {"indicators_metrics": tables},
                "events": build_prices_events(classification_output, processing_output),
                "history_contract": _prices_history_contract(processing_output),
                "technical_analysis": {"oscillator_display_contract": {
                    "basis": "processing_precomputed_reference_lines", "never_scale_reference_lines_from_last_value": True,
                    "never_scale_reference_lines_from_series_sum": True,
                    "rsi": {"domain": [0, 100], "reference_rule": "observed_min + {0.20,0.80}*(observed_max-observed_min)"},
                    "stochastic": {"domain": [0, 100], "reference_lines": [20, 80], "cross_gate": {"bullish": "K>D cross with K,D<=20", "bearish": "K<D cross with K,D>=80"}},
                    "tsi": {"domain": [-100, 100], "reference_rule": "observed_min + {0.20,0.80}*(observed_max-observed_min)"}, "recalculate_in_hmi": False}},
                "screen_layout": {
                    "analysis_view": {"back_button": {"visible": True, "label": "← REGRESAR", "target_view": "main"},
                                      "summary_panel": {"visible": True, "position": "right", "width_px": 314, "mode": "single_market_summary",
                                                        "source": "tables.indicators_metrics + precomputed classifications"},
                                      "global_view_selector_visible": False, "global_market_selector_visible": False},
                    "main_view": {"volume_height_share": 0.2, "price_height_share": 0.8, "total_height_px": 590, "left_content_height_px": 590,
                                  "technical_selector_height_px": 590, "alignment": "same_height", "visual_reference": "canonical_screen_a_590"}},
                "quality": {}}
    coverage_quality      = validate_prices_screen_coverage(contract, processing_output, classification_output)
    serialization_quality = {"status": "ok", "is_complete": True, "missing_fields": [], "warnings": [], "errors": []}
    try:
        json.dumps(contract, allow_nan=False)
    except (TypeError, ValueError) as exc:
        serialization_quality.update({"status": "invalid", "is_complete": False, "errors": [str(exc)]})
    contract["quality"] = _combine_prices_quality(processing_output.get("quality", {}), classification_output.get("quality", {}),
                                                  coverage_quality, serialization_quality)
    availability = _screen_availability(contract)
    contract["quality"].update({"contract_complete": coverage_quality["is_complete"] and serialization_quality["is_complete"],
                                "data_complete": coverage_quality["is_complete"] and serialization_quality["is_complete"],
                                "availability": availability, "presentation": {"window_limited": True, "default_display_window": DEFAULT_DISPLAY_WINDOW},
                                "compatibility_alias": {"is_complete": "contract_complete"}})
    contract["quality"]["is_complete"] = contract["quality"]["contract_complete"]
    contract["quality"]["extensions"] = _prices_quality_extensions(
        processing_output=processing_output, classification_output=classification_output, contract=contract
    )
    contract = align_prices_contract_to_sp_v1_9(contract)
    json.dumps(contract, allow_nan=False)
    return contract


def _selected_data_as_of(processing_output: Mapping[str, Any], market: str, timeframe: str) -> str | None:
    records = processing_output.get("markets", {}).get(market, {}).get("timeframes", {}).get(timeframe, {}).get("records", [])
    return datetime.fromtimestamp(int(records[-1]["timestamp"]), tz=UTC).isoformat() if records else None


def _selected_comparison(processing_output: Mapping[str, Any], classification_output: Mapping[str, Any], timeframe: str) -> dict[str, Any]:
    numeric_source = processing_output.get("features", {}).get("spot_futures_comparison", {}).get("by_timeframe", {}).get(timeframe, {})
    numeric        = {"current": deepcopy(numeric_source.get("current", {}))}
    classification = classification_output.get("market_relationship", {})
    if classification.get("timeframe") != timeframe:
        classification = {"status": "unavailable", "reason": "classification_not_available_for_selected_timeframe"}
    return {"timeframe": timeframe, "numeric": deepcopy(numeric), "classification": deepcopy(classification)}


def _selected_view_quality(*, kpis: Mapping[str, Any], widgets: Mapping[str, Any], tables: Mapping[str, Any], serializable: bool) -> dict[str, Any]:
    kpi_items         = list(kpis.get("items", []))
    widget_items      = list(widgets.values())
    contract_complete = serializable and all(name in tables for name in ("indicator_package", "technical_bias", "statistical_performance"))
    data_complete     = all(item.get("status") == "available" for item in kpi_items) and all(item.get("status") == "available" for item in widget_items)
    return {"status": "ok" if contract_complete else "invalid", "contract_complete": contract_complete, "data_complete": data_complete,
            "availability": {"kpis_available": sum(item.get("status") == "available" for item in kpi_items), "kpis_total": len(kpi_items),
                             "widgets_available": sum(item.get("status") == "available" for item in widget_items), "widgets_total": len(widget_items),
                             "tables_available": sum(bool(table.get("rows")) for table in tables.values()), "tables_total": len(tables)},
            "missing_fields": [], "warnings": [], "errors": [] if serializable else ["selected view is not strictly JSON serializable"]}


def build_prices_selected_view(processing_output: Mapping[str, Any], classification_output: Mapping[str, Any], *, market: str, timeframe: str) -> dict[str, Any]:
    """Build a small selector response from already-computed Prices state."""
    if processing_output.get("family") != "prices_ohlcv" or processing_output.get("stage") != "processing":
        raise ValueError("Selected Prices view requires a prices_ohlcv processing contract")
    if classification_output.get("family") != "prices_ohlcv" or classification_output.get("stage") != "classification":
        raise ValueError("Selected Prices view requires a prices_ohlcv classification contract")
    if market not in {"spot", "spot", "futures"}:
        raise ValueError(f"Unsupported Prices market: {market}")
    if timeframe not in TIMEFRAME_ORDER:
        raise ValueError(f"Unsupported Prices timeframe: {timeframe}")
    selection  = {"selected_market": market, "selected_timeframe": timeframe, "available_markets": list(MARKET_ORDER), "available_timeframes": list(TIMEFRAME_ORDER)}
    kpis       = build_prices_kpis(processing_output, selection)
    widgets    = build_prices_widgets(processing_output, classification_output, selection)
    indicators = build_indicators_metrics_table(classification_output, selection)
    bias       = build_technical_bias_table(classification_output, selection)
    statistics = build_statistical_performance_table(classification_output, selection)
    tables     = {"indicator_package": {key: deepcopy(indicators[key]) for key in ("table_id", "selected_market", "selected_timeframe", "rows")},
                  "technical_bias": {key: deepcopy(bias[key]) for key in ("table_id", "selected_market", "rows")},
                  "statistical_performance": {key: deepcopy(statistics[key]) for key in ("table_id", "selected_market", "selected_timeframe", "rows", "metadata")}}
    metadata   = processing_output.get("context", {})
    updated_at = classification_output.get("updated_at") or processing_output.get("updated_at") or metadata.get("updated_at")
    contract   = {"family": "prices_ohlcv", "screen": "prices", "contract_type": "selected_view", "schema_version": "1.2.0",
                  "selection": {"market": market, "timeframe": timeframe}, "kpis": kpis, "widgets": widgets, "tables": tables,
                  "comparison": _selected_comparison(processing_output, classification_output, timeframe),
                  "quality": {}, "data_as_of": _selected_data_as_of(processing_output, market, timeframe), "updated_at": updated_at,
                  "data_mode": metadata.get("data_mode", "live"), "is_demo": bool(metadata.get("is_demo", False))}
    serializable = True
    try:
        json.dumps(contract, allow_nan=False)
    except (TypeError, ValueError):
        serializable = False
    contract["quality"] = _selected_view_quality(kpis=kpis, widgets=widgets, tables=tables, serializable=serializable)
    json.dumps(contract, allow_nan=False)
    return contract

# --- Canonical Screen contract shaping ---
from copy import deepcopy

import json

from pathlib import Path

from typing import Any, Mapping

_screen_SCHEMA_VERSION = '1.9.0'

_screen_TEMPLATE_PATH = Path(__file__).with_name('screen_template.json')

_screen_MISSING = object()

def _screen_template() -> dict[str, Any]:
    return json.loads(_screen_TEMPLATE_PATH.read_text(encoding='utf-8'))

def _screen_is_scalar(value: Any) -> bool:
    return not isinstance(value, (dict, list))

def _screen_project(reference: Any, candidate: Any=_screen_MISSING) -> Any:
    """Project candidate values onto the exact SP key/nesting structure.

    Extra candidate keys are deliberately discarded. Missing keys retain the
    SP contract default, which is appropriate for static presentation policy
    fields. Dynamic Prices fields are supplied by the regular contract builder
    before this projection.
    """
    if isinstance(reference, dict):
        source = candidate if isinstance(candidate, Mapping) else {}
        return {key: _screen_project(value, source.get(key, _screen_MISSING)) for key, value in reference.items()}
    if isinstance(reference, list):
        if candidate is _screen_MISSING:
            return deepcopy(reference)
        if not isinstance(candidate, list):
            return deepcopy(reference)
        if not reference:
            return deepcopy(candidate)
        if all((_screen_is_scalar(item) for item in reference)):
            return deepcopy(candidate)
        if not candidate:
            return []
        identity_keys = ('metric_id', 'kpi_id', 'widget_id', 'chart_id', 'table_id', 'badge_id', 'id', 'role', 'family', 'group', 'indicator_id', 'first_series', 'event_type')

        def reference_for(item: Any, index: int) -> Any:
            if isinstance(item, Mapping):
                for key in identity_keys:
                    value = item.get(key, _screen_MISSING)
                    if value is _screen_MISSING:
                        continue
                    for ref_item in reference:
                        if isinstance(ref_item, Mapping) and ref_item.get(key, _screen_MISSING) == value:
                            return ref_item
            if index < len(reference):
                return reference[index]
            return reference[0]
        return [_screen_project(reference_for(item, index), item) for index, item in enumerate(candidate)]
    if candidate is _screen_MISSING:
        return deepcopy(reference)
    return deepcopy(candidate)

def _screen_project_dynamic_events(reference_events: Mapping[str, Any], candidate_events: Mapping[str, Any]) -> dict[str, Any]:
    reference_by_id = reference_events.get('by_id', {}) if isinstance(reference_events, Mapping) else {}
    candidate_by_id = candidate_events.get('by_id', {}) if isinstance(candidate_events, Mapping) else {}
    prototypes: dict[tuple[str | None, str | None], Mapping[str, Any]] = {}
    for event in reference_by_id.values():
        if isinstance(event, Mapping):
            prototypes.setdefault((event.get('event_type'), event.get('event_group')), event)
            prototypes.setdefault((event.get('event_type'), None), event)
    output: dict[str, Any] = {}
    for uid, event in candidate_by_id.items():
        if not isinstance(event, Mapping):
            continue
        prototype = prototypes.get((event.get('event_type'), event.get('event_group'))) or prototypes.get((event.get('event_type'), None))
        output[str(uid)] = _screen_project(prototype, event) if prototype is not None else deepcopy(dict(event))
    return output

def align_prices_contract_to_sp_v1_9(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Return a Prices contract with the exact final Screen-SP structure."""
    reference = _screen_template()
    aligned = _screen_project(reference, dict(candidate))
    candidate_events = candidate.get('events', {}) if isinstance(candidate, Mapping) else {}
    if isinstance(aligned.get('events'), dict) and isinstance(candidate_events, Mapping):
        aligned['events']['by_id'] = _screen_project_dynamic_events(reference.get('events', {}), candidate_events)
    aligned['family'] = 'prices_ohlcv'
    aligned['screen'] = 'prices'
    aligned['schema_version'] = _screen_SCHEMA_VERSION
    context = aligned.get('context', {})
    context['default_market'] = 'spot'
    context['available_markets'] = ['spot']
    context['price_role'] = 'canonical_spot_reference'
    context['market_scope'] = 'single_spot_market'
    context['price_construction'] = 'direct_spot_reference_series'
    context['synthetic_fixture'] = False
    context['fixture_as_of_timestamp'] = None
    context['fixture_as_of_iso'] = None
    context['realism_refactor_version'] = 'runtime_provider_v2'
    context['realism_note'] = ('runtime Emulator market state; no Screen fixture history is injected'
                               if bool(context.get('is_demo', False)) else
                               'runtime live provider data; spot is canonical CoinGlass Spot')
    aligned['context'] = context
    selectors = aligned.get('selectors', {})
    if isinstance(selectors.get('market'), dict):
        selectors['market'].update({'selected': 'spot', 'options': ['spot'], 'status': 'fixed', 'visible': False})
    aligned['selectors'] = selectors
    candidate_charts = candidate.get('charts', {}) if isinstance(candidate, Mapping) else {}
    aligned_charts = aligned.setdefault('charts', {})
    if isinstance(candidate_charts, Mapping):
        wasserstein_chart = candidate_charts.get('wasserstein_distance')
        if isinstance(wasserstein_chart, Mapping):
            aligned_charts['wasserstein_distance'] = deepcopy(dict(wasserstein_chart))
        bbw_chart = candidate_charts.get('bollinger_band_width')
        if isinstance(bbw_chart, Mapping):
            aligned_charts['bollinger_band_width'] = deepcopy(dict(bbw_chart))
        # Indicator payloads are runtime data. Copy their market/timeframe packages
        # exactly so stale template fixture keys can never survive projection.
        for indicator_id, candidate_chart in candidate_charts.items():
            if indicator_id == 'ohlcv' or not isinstance(candidate_chart, Mapping):
                continue
            aligned_chart = aligned_charts.get(indicator_id)
            if not isinstance(aligned_chart, dict):
                continue
            if isinstance(candidate_chart.get('markets'), Mapping):
                aligned_chart['markets'] = deepcopy(candidate_chart['markets'])
            if 'thresholds' in candidate_chart:
                aligned_chart['thresholds'] = deepcopy(candidate_chart.get('thresholds', []))
            if 'threshold_basis' in candidate_chart:
                aligned_chart['threshold_basis'] = candidate_chart.get('threshold_basis')
    candidate_ohlcv = candidate_charts.get('ohlcv', {}) if isinstance(candidate_charts, Mapping) else {}
    aligned_ohlcv = aligned_charts.get('ohlcv', {}) if isinstance(aligned_charts, Mapping) else {}
    if isinstance(candidate_ohlcv, Mapping) and isinstance(aligned_ohlcv, dict):
        candidate_markets = candidate_ohlcv.get('markets', {})
        aligned_markets = aligned_ohlcv.get('markets', {})
        if isinstance(candidate_markets, Mapping) and isinstance(aligned_markets, dict):
            for market, candidate_market in candidate_markets.items():
                candidate_timeframes = candidate_market.get('timeframes', {}) if isinstance(candidate_market, Mapping) else {}
                aligned_market = aligned_markets.get(market, {})
                aligned_timeframes = aligned_market.get('timeframes', {}) if isinstance(aligned_market, Mapping) else {}
                if not isinstance(candidate_timeframes, Mapping) or not isinstance(aligned_timeframes, dict):
                    continue
                for timeframe, candidate_tf in candidate_timeframes.items():
                    aligned_tf = aligned_timeframes.get(timeframe)
                    if isinstance(candidate_tf, Mapping) and isinstance(aligned_tf, dict) and isinstance(candidate_tf.get('overlays'), Mapping):
                        aligned_tf['overlays'] = deepcopy(candidate_tf['overlays'])
    candidate_widgets = candidate.get('widgets', {}) if isinstance(candidate, Mapping) else {}
    aligned_widgets = aligned.get('widgets', {})
    if isinstance(candidate_widgets, Mapping) and isinstance(aligned_widgets, dict):
        sr_widget = candidate_widgets.get('support_resistance_zones')
        if isinstance(sr_widget, Mapping):
            aligned_widgets['support_resistance_zones'] = deepcopy(dict(sr_widget))

    chart_analysis = aligned.get('charts', {}).get('ohlcv', {}).get('technical_fundamental_analysis')
    if isinstance(chart_analysis, Mapping):
        root_analysis = deepcopy(dict(chart_analysis))
        root_reference = reference.get('technical_analysis', {})
        oscillator_contract = aligned.get('technical_analysis', {}).get('oscillator_display_contract', deepcopy(root_reference.get('oscillator_display_contract')))
        root_analysis.update({'canonical_location': 'technical_analysis', 'compatibility_mirror': False, 'legacy_mirror_paths': ['charts.ohlcv.technical_fundamental_analysis'], 'oscillator_display_contract': oscillator_contract})
        aligned['technical_analysis'] = root_analysis
        chart_analysis.update({'canonical_location': 'technical_analysis', 'compatibility_mirror': True, 'excluded_from_panel_order': ['price_vs_vwap']})
    json.dumps(aligned, allow_nan=False)
    return aligned
