"""Classification v0.1 for Liquidity Microstructure Processing."""

# ruff: noqa: E701, E702

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
import json
import math
from typing import Any


STATUSES = {"available", "partial", "unavailable", "invalid"}


def _find_invalid_json_path(value: Any, path: str) -> tuple[str, str] | None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                return "non_string_key", path
            found = _find_invalid_json_path(item, f"{path}.{key}")
            if found is not None:
                return found
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, item in enumerate(value):
            found = _find_invalid_json_path(item, f"{path}[{index}]")
            if found is not None:
                return found
    elif isinstance(value, float):
        if not math.isfinite(value) or (value == 0 and math.copysign(1, value) < 0):
            return "invalid_float", path
    elif value is not None and not isinstance(value, (str, int, bool)):
        return "non_json_value", path
    return None


def _validate_json(value: Any, path: str = "root") -> None:
    """Validate JSON values without materializing a path for every valid node."""
    stack = [value]
    while stack:
        current = stack.pop()
        # Runtime contracts are concrete JSON dict/list trees.  Using the
        # collections.abc/typing protocols here invoked millions of expensive
        # __instancecheck__ calls on the 50+ MB Liquidity processing payload.
        if type(current) is dict:
            if any(type(key) is not str for key in current):
                kind, found = _find_invalid_json_path(value, path) or ("non_string_key", path)
                raise ValueError(f"{kind}:{found}")
            stack.extend(current.values())
        elif type(current) in (list, tuple):
            stack.extend(current)
        elif type(current) is float:
            if not math.isfinite(current) or (current == 0 and math.copysign(1, current) < 0):
                kind, found = _find_invalid_json_path(value, path) or ("invalid_float", path)
                raise ValueError(f"{kind}:{found}")
        elif current is not None and type(current) not in (str, int, bool):
            kind, found = _find_invalid_json_path(value, path) or ("non_json_value", path)
            raise ValueError(f"{kind}:{found}")


def _timestamp(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"invalid_timestamp:{path}")
    return value


def _status(node: Mapping[str, Any], path: str) -> str:
    status = node.get("status")
    if status not in STATUSES:
        raise ValueError(f"invalid_status:{path}")
    reason = node.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise ValueError(f"invalid_reason:{path}")
    return status


def validate_liquidity_microstructure_processing(processing_contract: Mapping[str, Any]) -> None:
    if not isinstance(processing_contract, Mapping) or processing_contract.get("family") != "liquidity_microstructure":
        raise ValueError("invalid_processing_family")
    if processing_contract.get("stage") != "processing" or processing_contract.get("mode") not in {"bootstrap", "incremental", "recovery"}:
        raise ValueError("invalid_processing_stage_or_mode")
    _timestamp(processing_contract.get("reference_timestamp"), "reference_timestamp")
    _timestamp(processing_contract.get("execution_timestamp"), "execution_timestamp")
    for key in ("configuration", "context", "source_selection", "markets", "whale_activity", "market_history", "comparison", "features", "quality"):
        if not isinstance(processing_contract.get(key), Mapping):
            raise ValueError(f"invalid_processing_mapping:{key}")
    markets = processing_contract["markets"]
    # Processing deliberately emits an invalid-but-JSON-safe shell when its
    # provider Input is invalid. Classification must preserve that state and
    # let the Screen builder emit a complete fallback contract instead of
    # raising before the invalid branch can run.
    if processing_contract["quality"].get("status") == "invalid":
        _validate_json(processing_contract)
        return
    if set(markets) != set(MARKETS):
        raise ValueError("invalid_processing_markets")
    for market in MARKETS:
        for feature in ("orderbook", "order_depth", "large_trades"):
            _status(markets[market][feature], f"markets.{market}.{feature}")
        for feature in ("orderbook", "order_depth"):
            nodes = markets[market][feature].get("timeframes")
            if not isinstance(nodes, Mapping) or set(nodes) != set(TIMEFRAMES):
                raise ValueError(f"invalid_processing_timeframes:{market}.{feature}")
            for timeframe, node in nodes.items():
                _status(node, f"markets.{market}.{feature}.{timeframe}")
        windows = markets[market]["large_trades"].get("windows")
        if not isinstance(windows, Mapping) or not set(LARGE_TRADE_WINDOWS).issubset(windows):
            raise ValueError(f"invalid_trade_windows:{market}")
    _status(processing_contract["whale_activity"], "whale_activity")
    _status(processing_contract["market_history"], "market_history")
    if processing_contract["quality"].get("status") not in {"ok", "partial", "invalid"}:
        raise ValueError("invalid_processing_quality")
    _validate_json(processing_contract)


def _classify_orderbook(node: dict[str, Any], thresholds: Mapping[str, float]) -> None:
    source_status, current = node["status"], node.get("current")
    if not isinstance(current, Mapping):
        node["classification"] = {"spread_condition": classify_spread(None, source_status=source_status, thresholds=thresholds),
                                  "execution_liquidity_state": classify_execution(
                                      classify_spread(None, source_status=source_status, thresholds=thresholds),
                                      classify_impact(None, source_status=source_status, fully_filled=False, thresholds=thresholds), source_timestamp=None)}
        return
    timestamp = current["timestamp"]
    spread = classify_spread(current.get("spread_bps"), source_status=source_status, source_timestamp=timestamp, thresholds=thresholds)
    impact_node = current.get("market_impact", {})
    impacts = {}
    for side in ("buy", "sell"):
        side_node = impact_node.get(side, {})
        impacts[side] = classify_impact(side_node.get("impact_bps"), source_status=side_node.get("status", impact_node.get("status", source_status)),
                                        source_timestamp=timestamp, fully_filled=side_node.get("fully_filled", False), thresholds=thresholds)
    worst = classify_impact(impact_node.get("worst_side_impact_bps"), source_status=impact_node.get("status", source_status),
                            source_timestamp=timestamp, fully_filled=impact_node.get("worst_side_impact_bps") is not None, thresholds=thresholds)
    full = current.get("bands", {}).get("full_visible_book", {})
    balances = {basis: classify_imbalance(full.get(basis, {}).get("imbalance_percent"), source_status=full.get(basis, {}).get("status", source_status),
                                          source_timestamp=timestamp, basis=basis, thresholds=thresholds)
                for basis in ("quote_notional", "base_quantity")}
    node["classification"] = {"spread_condition": spread, "market_impact": {**impacts, "worst_side": worst},
                              "orderbook_balance": {"primary_basis": "quote_notional", **balances},
                              "execution_liquidity_state": classify_execution(spread, worst, source_timestamp=timestamp)}


def _classify_depth(node: dict[str, Any], thresholds: Mapping[str, float]) -> None:
    for collection in ("direct_ranges", "derived_bands"):
        for row in node[collection]:
            timestamp, source_status = row["timestamp"], row.get("status", node["status"])
            row["classification"] = {basis: classify_imbalance(row.get(basis, {}).get("imbalance_percent"),
                                                                source_status=row.get(basis, {}).get("status", source_status),
                                                                source_timestamp=timestamp, basis=basis, thresholds=thresholds)
                                         for basis in ("quote_notional", "base_quantity")}
    reference = next((row for row in reversed(node["direct_ranges"]) if row["range_percent"] == 10), None)
    node["classification"] = {"primary_depth_basis": "quote_notional", "primary_depth_reference": "range_10",
                              "reference_range_percent": 10,
                              "reference_balance": reference.get("classification", {}).get("quote_notional") if reference else None}


def _classify_trades(node: dict[str, Any], thresholds: Mapping[str, float]) -> None:
    coverage = node.get("coverage", {})
    node["classification"] = {window: classify_trade_window(node["windows"][window], source_status=node["status"],
                                                             source_timestamp=node["windows"][window].get("last_event_timestamp"),
                                                             coverage_complete=bool(coverage.get("coverage_complete")), thresholds=thresholds)
                              for window in LARGE_TRADE_WINDOWS}


def _classify_whale(node: dict[str, Any], thresholds: Mapping[str, float]) -> None:
    for timeframe, timeframe_node in node["timeframes"].items():
        statistics = timeframe_node.get("statistics", {})
        timeframe_node["classification"] = classify_whale(statistics.get("rolling_z_score_20"), source_status=statistics.get("status", timeframe_node["status"]),
                                                            source_timestamp=(timeframe_node.get("current") or {}).get("timestamp"),
                                                            reason=statistics.get("reason"), thresholds=thresholds)


def _classify_history(node: dict[str, Any], thresholds: Mapping[str, float]) -> None:
    for window, change in node["changes"].items():
        change["classification"] = classify_market_change(change.get("change_percent"), source_status=change["status"],
                                                           source_timestamp=change.get("source_timestamp"), thresholds=thresholds)


def _classify_comparisons(node: dict[str, Any], thresholds: Mapping[str, float]) -> None:
    for row in node.get("order_depth", []):
        row["classification"] = {
            "depth_quote": classify_comparison(row.get("perpetual_to_spot_total_depth_ratio_quote"), kind="depth_quote", source_timestamp=row["timestamp"], thresholds=thresholds),
            "depth_base": classify_comparison(row.get("perpetual_to_spot_total_depth_ratio_base"), kind="depth_base", source_timestamp=row["timestamp"], thresholds=thresholds)}
    for row in node.get("orderbook", []):
        row["classification"] = {
            "spread": classify_comparison(row.get("spread_difference_bps"), kind="spread", source_timestamp=row["timestamp"], thresholds=thresholds),
            "buy_impact": classify_comparison(row.get("buy_impact_difference_bps"), kind="buy_impact", source_timestamp=row["timestamp"], thresholds=thresholds),
            "sell_impact": classify_comparison(row.get("sell_impact_difference_bps"), kind="sell_impact", source_timestamp=row["timestamp"], thresholds=thresholds)}


def _invalid_output(source: Mapping[str, Any], thresholds: Mapping[str, float], execution: int) -> dict[str, Any]:
    return {"family": "liquidity_microstructure", "stage": "classification", "mode": source["mode"],
            "reference_timestamp": source["reference_timestamp"], "source_execution_timestamp": source["execution_timestamp"],
            "execution_timestamp": execution, "classification_version": CLASSIFICATION_VERSION,
            "classification_rule_version": CLASSIFICATION_RULE_VERSION, "context": deepcopy(source["context"]),
            "configuration": {"thresholds": dict(thresholds), "calibration_status": "provisional_coinglass_only"},
            "source_selection": deepcopy(source["source_selection"]), "markets": {}, "whale_activity": {}, "market_history": {},
            "comparison": {"spot_perpetual": {}}, "summary": {"observed_liquidity": {}},
            "quality": {"status": "invalid", "reason": "processing_quality_invalid", "required_groups": [], "optional_groups": [],
                        "available_groups": [], "partial_groups": [], "unavailable_groups": [], "invalid_groups": [], "warnings": [], "errors": []}}


def classify_liquidity_microstructure(processing_contract: Mapping[str, Any], *, config: Mapping[str, Any] | None = None,
                                      now_timestamp: int | None = None,
                                      _copy_input_branches: bool = True) -> dict[str, Any]:
    validate_liquidity_microstructure_processing(processing_contract)
    # Processing is an external read-only input.  Copy only the branches that
    # Classification enriches instead of cloning the entire 50+ MB contract and
    # then cloning those same branches a second time below.
    thresholds, source = validate_thresholds(config), processing_contract
    execution = source["execution_timestamp"] if now_timestamp is None else _timestamp(now_timestamp, "now_timestamp")
    if source["quality"]["status"] == "invalid":
        return _invalid_output(source, thresholds, execution)
    markets = deepcopy(source["markets"]) if _copy_input_branches else source["markets"]
    for market in MARKETS:
        for timeframe in TIMEFRAMES:
            _classify_orderbook(markets[market]["orderbook"]["timeframes"][timeframe], thresholds)
            _classify_depth(markets[market]["order_depth"]["timeframes"][timeframe], thresholds)
        _classify_trades(markets[market]["large_trades"], thresholds)
        summaries = {}
        for timeframe in TIMEFRAMES:
            orderbook = markets[market]["orderbook"]["timeframes"][timeframe]["classification"]
            trade = markets[market]["large_trades"]["classification"][timeframe]
            alignment = classify_pressure_alignment(orderbook.get("orderbook_balance", {}).get("quote_notional", {}).get("state", "unavailable"),
                                                    trade["state"], source_timestamp=trade.get("source_timestamp"))
            summaries[timeframe] = {"market_type": market, "timeframe": timeframe,
                                    "execution_liquidity_state": orderbook["execution_liquidity_state"]["state"],
                                    "balance_state": orderbook.get("orderbook_balance", {}).get("quote_notional", {}).get("state", "unavailable"),
                                    "reference_depth_balance_state": (markets[market]["order_depth"]["timeframes"][timeframe]["classification"].get("reference_balance") or {}).get("state", "unavailable"),
                                    "large_trade_pressure_state": trade["state"], "pressure_alignment_state": alignment["state"],
                                    "pressure_alignment": alignment, "provisional": any(item.get("provisional") for item in (trade, alignment))}
        markets[market]["summary"] = summaries
    if _copy_input_branches:
        whale, history, comparison = deepcopy(source["whale_activity"]), deepcopy(source["market_history"]), deepcopy(source["comparison"])
    else:
        whale, history, comparison = source["whale_activity"], source["market_history"], source["comparison"]
    _classify_whale(whale, thresholds); _classify_history(history, thresholds); _classify_comparisons(comparison["spot_perpetual"], thresholds)
    cross = {timeframe: classify_cross_market_execution(markets["spot"]["summary"][timeframe]["execution_liquidity_state"],
                                                        markets["perpetual"]["summary"][timeframe]["execution_liquidity_state"],
                                                        source_timestamp=source["reference_timestamp"]) for timeframe in TIMEFRAMES}
    required = {f"markets.{market}.{feature}": markets[market][feature]["status"] for market in MARKETS for feature in ("orderbook", "order_depth", "large_trades")}
    required.update({"whale_activity": whale["status"]})
    optional = {"market_history": history["status"]}
    invalid = [key for key, value in required.items() if value == "invalid"]
    partial = [key for key, value in required.items() if value == "partial"]
    unavailable = [key for key, value in required.items() if value == "unavailable"]
    available = [key for key, value in required.items() if value == "available"]
    quality = "invalid" if invalid else ("partial" if partial or unavailable else "ok")
    output = {"family": "liquidity_microstructure", "stage": "classification", "mode": source["mode"],
              "reference_timestamp": source["reference_timestamp"], "source_execution_timestamp": source["execution_timestamp"],
              "execution_timestamp": execution, "classification_version": CLASSIFICATION_VERSION,
              "classification_rule_version": CLASSIFICATION_RULE_VERSION, "context": deepcopy(source["context"]),
              "configuration": {"thresholds": thresholds, "calibration_status": "provisional_coinglass_only"},
              "source_selection": deepcopy(source["source_selection"]), "markets": markets, "whale_activity": whale,
              "market_history": history, "comparison": comparison,
              "summary": {"observed_liquidity": {"markets": {market: markets[market]["summary"] for market in MARKETS},
                                                   "cross_market": cross, "calibration_status": "provisional_coinglass_only",
                                                   "provider_scope": "coinglass", "limitations": ["coinglass_only", "no_glassnode", "no_cryptoquant",
                                                       "large_trades_may_have_incomplete_coverage", "whale_activity_is_derived_from_large_limit_orders",
                                                       "range_10_is_not_full_book", "observed_conditions_not_absolute_global_liquidity"]}},
              "quality": {"status": quality, "reason": None if quality == "ok" else "one_or_more_required_groups_not_available",
                          "required_groups": list(required), "optional_groups": ["market_history", "execution_liquidity", "comparison.spot_perpetual",
                              "pressure_alignment", "cross_market_execution_liquidity", "whale_rolling_classification"],
                          "available_groups": available, "partial_groups": partial, "unavailable_groups": unavailable,
                          "invalid_groups": invalid, "warnings": [], "errors": []}}
    return output


run_liquidity_microstructure_classification = classify_liquidity_microstructure


class LiquidityMicrostructureClassifier:
    def __init__(self, processing_contract: Mapping[str, Any], *, config: Mapping[str, Any] | None = None,
                 now_timestamp: int | None = None) -> None:
        self.arguments = {"processing_contract": processing_contract, "config": config, "now_timestamp": now_timestamp}

    def run(self) -> dict[str, Any]:
        return classify_liquidity_microstructure(**self.arguments)

# --- Liquidity classification rules (merged) ---
from collections.abc import Mapping

import math

from typing import Any

CLASSIFICATION_VERSION = '0.1'

CLASSIFICATION_RULE_VERSION = 'liquidity_microstructure.rules.v0.1'

MARKETS = ('spot', 'perpetual')

TIMEFRAMES = ('1m', '5m', '15m', '4h')

LARGE_TRADE_WINDOWS = ('1m', '5m', '15m', '4h', '24h')

DEFAULT_THRESHOLDS = {'imbalance_balanced_max_abs_percent': 3.0, 'imbalance_dominant_min_abs_percent': 10.0, 'spread_tight_max_bps': 2.0, 'spread_normal_max_bps': 5.0, 'spread_wide_max_bps': 10.0, 'impact_low_max_bps': 3.0, 'impact_moderate_max_bps': 10.0, 'impact_high_max_bps': 25.0, 'large_trade_dominant_share_percent': 60.0, 'whale_elevated_z_abs': 1.0, 'whale_extreme_z_abs': 2.0, 'market_return_flat_max_abs_percent': 0.25, 'depth_ratio_spot_stronger_max': 0.8, 'depth_ratio_perpetual_stronger_min': 1.25, 'spread_comparable_max_abs_diff_bps': 1.0, 'impact_comparable_max_abs_diff_bps': 2.0}

def validate_thresholds(config: Mapping[str, Any] | None) -> dict[str, float]:
    if config is not None and (not isinstance(config, Mapping)):
        raise ValueError('classification_config_must_be_mapping')
    thresholds = {**DEFAULT_THRESHOLDS, **dict((config or {}).get('thresholds', config or {}))}
    if set(thresholds) != set(DEFAULT_THRESHOLDS):
        raise ValueError('unknown_classification_threshold')
    if any((isinstance(value, bool) or not isinstance(value, (int, float)) or (not math.isfinite(value)) for value in thresholds.values())):
        raise ValueError('classification_threshold_must_be_finite_number')
    t = {key: float(value) for key, value in thresholds.items()}
    if not 0 <= t['imbalance_balanced_max_abs_percent'] < t['imbalance_dominant_min_abs_percent'] <= 100:
        raise ValueError('invalid_imbalance_thresholds')
    if not 0 <= t['spread_tight_max_bps'] <= t['spread_normal_max_bps'] <= t['spread_wide_max_bps']:
        raise ValueError('invalid_spread_thresholds')
    if not 0 <= t['impact_low_max_bps'] <= t['impact_moderate_max_bps'] <= t['impact_high_max_bps']:
        raise ValueError('invalid_impact_thresholds')
    if not (50 < t['large_trade_dominant_share_percent'] <= 100 and 0 < t['whale_elevated_z_abs'] < t['whale_extreme_z_abs']):
        raise ValueError('invalid_flow_or_whale_thresholds')
    if not 0 < t['depth_ratio_spot_stronger_max'] < 1 < t['depth_ratio_perpetual_stronger_min']:
        raise ValueError('invalid_depth_ratio_thresholds')
    return t

def atom(*, status: str, state: str, signal: str, signal_color: str, display: str, display_color: str, source_status: str='available', source_timestamp: int | None=None, source_value: Any=None, source_values: Mapping[str, Any] | None=None, unit: str, parameters: Mapping[str, Any], reasons: list[str] | None=None) -> dict[str, Any]:
    result = {'status': status, 'state': state, 'signal': signal, 'signal_color_token': signal_color, 'display_signal': display, 'display_color_token': display_color, 'reason_codes': list(dict.fromkeys(reasons or [])), 'provisional': status == 'partial', 'source_status': source_status, 'source_timestamp': source_timestamp, 'unit': unit, 'parameters': dict(parameters)}
    result['source_values' if source_values is not None else 'source_value'] = dict(source_values) if source_values is not None else source_value
    return result

def unavailable_atom(source_status: str, *, source_timestamp: int | None, unit: str, parameters: Mapping[str, Any], reason: str | None=None) -> dict[str, Any]:
    status = source_status if source_status in {'partial', 'unavailable', 'invalid'} else 'unavailable'
    invalid = status == 'invalid'
    return atom(status=status, state='invalid' if invalid else 'indeterminate' if status == 'partial' else 'unavailable', signal='unavailable', signal_color='critical' if invalid else 'unavailable', display='Unavailable', display_color='critical' if invalid else 'muted', source_status=source_status, source_timestamp=source_timestamp, unit=unit, parameters=parameters, reasons=[reason or f'{status}_source'])

def classify_imbalance(value: float | None, *, source_status: str='available', source_timestamp: int | None=None, basis: str='quote_notional', thresholds: Mapping[str, float]=DEFAULT_THRESHOLDS) -> dict[str, Any]:
    params = {'basis': basis, 'balanced_max_abs_percent': thresholds['imbalance_balanced_max_abs_percent'], 'dominant_min_abs_percent': thresholds['imbalance_dominant_min_abs_percent']}
    if value is None or source_status in {'unavailable', 'invalid'}:
        return unavailable_atom(source_status, source_timestamp=source_timestamp, unit='percent', parameters=params)
    dominant, balanced = (thresholds['imbalance_dominant_min_abs_percent'], thresholds['imbalance_balanced_max_abs_percent'])
    if value >= dominant:
        rule = ('bid_dominant', 'bid_support', 'positive', 'Bid Dominant', 'success')
    elif value >= balanced:
        rule = ('bid_leaning', 'bid_support', 'positive', 'Bid Leaning', 'success')
    elif value > -balanced:
        rule = ('balanced', 'balanced_book', 'neutral', 'Balanced', 'neutral')
    elif value > -dominant:
        rule = ('ask_leaning', 'ask_pressure', 'negative', 'Ask Leaning', 'danger')
    else:
        rule = ('ask_dominant', 'ask_pressure', 'negative', 'Ask Dominant', 'danger')
    return atom(status=source_status, state=rule[0], signal=rule[1], signal_color=rule[2], display=rule[3], display_color=rule[4], source_status=source_status, source_timestamp=source_timestamp, source_value=value, unit='percent', parameters=params, reasons=['partial_source'] if source_status == 'partial' else [])

def classify_spread(value: float | None, *, source_status: str='available', source_timestamp: int | None=None, thresholds: Mapping[str, float]=DEFAULT_THRESHOLDS) -> dict[str, Any]:
    params = {key: thresholds[key] for key in ('spread_tight_max_bps', 'spread_normal_max_bps', 'spread_wide_max_bps')}
    if value is None or source_status in {'unavailable', 'invalid'}:
        return unavailable_atom(source_status, source_timestamp=source_timestamp, unit='bps', parameters=params)
    if value <= params['spread_tight_max_bps']:
        rule = ('tight', 'efficient_transaction_cost', 'positive', 'Tight', 'success')
    elif value <= params['spread_normal_max_bps']:
        rule = ('normal', 'normal_transaction_cost', 'neutral', 'Normal', 'neutral')
    elif value <= params['spread_wide_max_bps']:
        rule = ('wide', 'elevated_transaction_cost', 'warning', 'Wide', 'warning')
    else:
        rule = ('stressed', 'stressed_transaction_cost', 'critical', 'Stressed', 'critical')
    return atom(status=source_status, state=rule[0], signal=rule[1], signal_color=rule[2], display=rule[3], display_color=rule[4], source_status=source_status, source_timestamp=source_timestamp, source_value=value, unit='bps', parameters=params)

def classify_impact(value: float | None, *, source_status: str='available', source_timestamp: int | None=None, fully_filled: bool=True, thresholds: Mapping[str, float]=DEFAULT_THRESHOLDS) -> dict[str, Any]:
    params = {key: thresholds[key] for key in ('impact_low_max_bps', 'impact_moderate_max_bps', 'impact_high_max_bps')}
    if not fully_filled or value is None:
        return unavailable_atom('partial' if source_status != 'invalid' else 'invalid', source_timestamp=source_timestamp, unit='bps', parameters=params, reason='incomplete_market_impact_fill')
    if source_status in {'unavailable', 'invalid'}:
        return unavailable_atom(source_status, source_timestamp=source_timestamp, unit='bps', parameters=params)
    if value <= params['impact_low_max_bps']:
        rule = ('low', 'low_execution_cost', 'positive', 'Low', 'success')
    elif value <= params['impact_moderate_max_bps']:
        rule = ('moderate', 'moderate_execution_cost', 'neutral', 'Moderate', 'neutral')
    elif value <= params['impact_high_max_bps']:
        rule = ('high', 'high_execution_cost', 'warning', 'High', 'warning')
    else:
        rule = ('severe', 'severe_execution_cost', 'critical', 'Severe', 'critical')
    return atom(status=source_status, state=rule[0], signal=rule[1], signal_color=rule[2], display=rule[3], display_color=rule[4], source_status=source_status, source_timestamp=source_timestamp, source_value=value, unit='bps', parameters=params)

def classify_trade_window(window: Mapping[str, Any], *, source_status: str, source_timestamp: int | None, coverage_complete: bool, thresholds: Mapping[str, float]=DEFAULT_THRESHOLDS) -> dict[str, Any]:
    count, buy, sell = (window.get('event_count'), window.get('buy_share_percent'), window.get('sell_share_percent'))
    params = {'dominant_share_percent': thresholds['large_trade_dominant_share_percent']}
    if source_status == 'invalid':
        return unavailable_atom('invalid', source_timestamp=source_timestamp, unit='percent', parameters=params)
    if count == 0:
        return atom(status=source_status, state='no_observations', signal='unavailable', signal_color='unavailable', display='No Observations', display_color='muted', source_status=source_status, source_timestamp=source_timestamp, source_values={'event_count': count, 'buy_share_percent': buy, 'sell_share_percent': sell}, unit='percent', parameters=params, reasons=['stream_warmup_in_progress'] if source_status == 'partial' else [])
    if buy is None or sell is None:
        return unavailable_atom(source_status, source_timestamp=source_timestamp, unit='percent', parameters=params)
    if buy >= params['dominant_share_percent']:
        rule = ('buy_dominant', 'buy_pressure', 'positive', 'Buy Dominant', 'success')
    elif sell >= params['dominant_share_percent']:
        rule = ('sell_dominant', 'sell_pressure', 'negative', 'Sell Dominant', 'danger')
    else:
        rule = ('balanced', 'balanced_trade_flow', 'neutral', 'Balanced', 'neutral')
    status = 'partial' if source_status == 'partial' or not coverage_complete else 'available'
    return atom(status=status, state=rule[0], signal=rule[1], signal_color=rule[2], display=rule[3], display_color=rule[4], source_status=source_status, source_timestamp=source_timestamp, source_values={'event_count': count, 'buy_share_percent': buy, 'sell_share_percent': sell}, unit='percent', parameters=params, reasons=['incomplete_collection_window'] if not coverage_complete else [])

def classify_whale(value: float | None, *, source_status: str, source_timestamp: int | None, reason: str | None=None, thresholds: Mapping[str, float]=DEFAULT_THRESHOLDS) -> dict[str, Any]:
    params = {'elevated_z_abs': thresholds['whale_elevated_z_abs'], 'extreme_z_abs': thresholds['whale_extreme_z_abs']}
    if value is None or source_status in {'unavailable', 'invalid'}:
        return unavailable_atom(source_status, source_timestamp=source_timestamp, unit='z_score', parameters=params, reason=reason)
    elevated, extreme = (params['elevated_z_abs'], params['extreme_z_abs'])
    if value >= extreme:
        rule = ('extreme_positive_deviation', 'unusual_positive_whale_activity', 'positive', 'Extreme Positive Deviation', 'success')
    elif value >= elevated:
        rule = ('elevated_positive_deviation', 'elevated_positive_whale_activity', 'positive', 'Elevated Positive Deviation', 'success')
    elif value > -elevated:
        rule = ('normal_range', 'normal_whale_activity', 'neutral', 'Normal Range', 'neutral')
    elif value > -extreme:
        rule = ('elevated_negative_deviation', 'elevated_negative_whale_activity', 'negative', 'Elevated Negative Deviation', 'danger')
    else:
        rule = ('extreme_negative_deviation', 'unusual_negative_whale_activity', 'negative', 'Extreme Negative Deviation', 'danger')
    return atom(status=source_status, state=rule[0], signal=rule[1], signal_color=rule[2], display=rule[3], display_color=rule[4], source_status=source_status, source_timestamp=source_timestamp, source_value=value, unit='z_score', parameters=params)

def classify_market_change(value: float | None, *, source_status: str, source_timestamp: int | None, thresholds: Mapping[str, float]=DEFAULT_THRESHOLDS) -> dict[str, Any]:
    flat = thresholds['market_return_flat_max_abs_percent']
    if value is None or source_status in {'unavailable', 'invalid'}:
        return unavailable_atom(source_status, source_timestamp=source_timestamp, unit='percent', parameters={'flat_max_abs_percent': flat})
    if value >= flat:
        rule = ('rising', 'positive_price_change', 'positive', 'Rising', 'success')
    elif value <= -flat:
        rule = ('falling', 'negative_price_change', 'negative', 'Falling', 'danger')
    else:
        rule = ('flat', 'flat_price_change', 'neutral', 'Flat', 'neutral')
    return atom(status=source_status, state=rule[0], signal=rule[1], signal_color=rule[2], display=rule[3], display_color=rule[4], source_status=source_status, source_timestamp=source_timestamp, source_value=value, unit='percent', parameters={'flat_max_abs_percent': flat})

def classify_execution(spread: Mapping[str, Any], impact: Mapping[str, Any], *, source_timestamp: int | None) -> dict[str, Any]:
    if 'invalid' in {spread['status'], impact['status']}:
        return unavailable_atom('invalid', source_timestamp=source_timestamp, unit='semantic_state', parameters={'scope': 'observed_transaction_cost_and_market_impact'})
    if spread['signal'] == 'unavailable' or impact['signal'] == 'unavailable':
        return unavailable_atom('partial', source_timestamp=source_timestamp, unit='semantic_state', parameters={'scope': 'observed_transaction_cost_and_market_impact'}, reason='insufficient_execution_liquidity_inputs')
    states = (spread['state'], impact['state'])
    if states == ('tight', 'low'):
        rule = ('robust', 'strong_execution_liquidity', 'positive', 'Robust', 'success')
    elif states[0] in {'tight', 'normal'} and states[1] in {'low', 'moderate'}:
        rule = ('healthy', 'healthy_execution_liquidity', 'positive', 'Healthy', 'success')
    elif states[0] == 'stressed' or states[1] == 'severe':
        rule = ('critical', 'critical_execution_liquidity', 'critical', 'Critical', 'critical')
    elif states[0] == 'wide' or states[1] == 'high':
        rule = ('constrained', 'constrained_execution_liquidity', 'warning', 'Constrained', 'warning')
    else:
        rule = ('mixed', 'mixed_execution_liquidity', 'neutral', 'Mixed', 'neutral')
    status = 'partial' if 'partial' in {spread['status'], impact['status']} else 'available'
    return atom(status=status, state=rule[0], signal=rule[1], signal_color=rule[2], display=rule[3], display_color=rule[4], source_status=status, source_timestamp=source_timestamp, source_values={'spread_state': states[0], 'impact_state': states[1]}, unit='semantic_state', parameters={'scope': 'observed_transaction_cost_and_market_impact'})

def classify_comparison(value: float | None, *, kind: str, source_timestamp: int | None, thresholds: Mapping[str, float]=DEFAULT_THRESHOLDS) -> dict[str, Any]:
    if value is None:
        return unavailable_atom('unavailable', source_timestamp=source_timestamp, unit='ratio' if 'depth' in kind else 'bps', parameters={'kind': kind})
    if kind in {'depth_quote', 'depth_base'}:
        low, high = (thresholds['depth_ratio_spot_stronger_max'], thresholds['depth_ratio_perpetual_stronger_min'])
        state = 'spot_deeper' if value <= low else 'perpetual_deeper' if value >= high else 'comparable_depth'
        signal = state
        unit = 'ratio'
    else:
        limit = thresholds['spread_comparable_max_abs_diff_bps'] if kind == 'spread' else thresholds['impact_comparable_max_abs_diff_bps']
        if kind == 'spread':
            state = 'spot_tighter' if value >= limit else 'perpetual_tighter' if value <= -limit else 'comparable_spread'
        elif kind == 'buy_impact':
            state = 'spot_lower_buy_impact' if value >= limit else 'perpetual_lower_buy_impact' if value <= -limit else 'comparable_buy_impact'
        else:
            state = 'spot_lower_sell_impact' if value >= limit else 'perpetual_lower_sell_impact' if value <= -limit else 'comparable_sell_impact'
        signal = state
        unit = 'bps'
    color = 'neutral' if state.startswith('comparable') else 'warning'
    return atom(status='available', state=state, signal=signal, signal_color=color, display=state.replace('_', ' ').title(), display_color=color, source_timestamp=source_timestamp, source_value=value, unit=unit, parameters={'kind': kind})

def classify_pressure_alignment(book_state: str, trade_state: str, *, source_timestamp: int | None) -> dict[str, Any]:
    bid, ask = (book_state in {'bid_leaning', 'bid_dominant'}, book_state in {'ask_leaning', 'ask_dominant'})
    if trade_state == 'no_observations':
        rule = ('indeterminate', 'unavailable', 'unavailable', 'Unavailable', 'muted')
    elif book_state == 'balanced' or trade_state == 'balanced':
        rule = ('mixed', 'mixed_book_trade_pressure', 'neutral', 'Mixed', 'neutral')
    elif bid and trade_state == 'buy_dominant':
        rule = ('aligned_buy_side', 'book_and_trades_buy_alignment', 'positive', 'Aligned Buy Side', 'success')
    elif ask and trade_state == 'sell_dominant':
        rule = ('aligned_sell_side', 'book_and_trades_sell_alignment', 'negative', 'Aligned Sell Side', 'danger')
    elif bid and trade_state == 'sell_dominant' or (ask and trade_state == 'buy_dominant'):
        rule = ('divergent', 'book_trade_divergence', 'warning', 'Divergent', 'warning')
    else:
        rule = ('indeterminate', 'unavailable', 'unavailable', 'Unavailable', 'muted')
    return atom(status='available' if rule[0] != 'indeterminate' else 'partial', state=rule[0], signal=rule[1], signal_color=rule[2], display=rule[3], display_color=rule[4], source_timestamp=source_timestamp, source_values={'orderbook_balance_state': book_state, 'large_trade_state': trade_state}, unit='semantic_state', parameters={})

def classify_cross_market_execution(spot_state: str, perpetual_state: str, *, source_timestamp: int | None) -> dict[str, Any]:
    states = {spot_state, perpetual_state}
    if 'indeterminate' in states or 'unavailable' in states:
        return unavailable_atom('partial', source_timestamp=source_timestamp, unit='semantic_state', parameters={'scope': 'coinglass_spot_perpetual_observed_execution_conditions'})
    if spot_state == perpetual_state == 'robust':
        state = 'robust'
    elif states <= {'robust', 'healthy'}:
        state = 'healthy'
    elif spot_state == perpetual_state == 'critical':
        state = 'critical'
    elif 'critical' in states:
        state = 'stressed'
    elif 'constrained' in states:
        state = 'constrained'
    else:
        state = 'mixed'
    signal = f'cross_market_{state}_execution_liquidity'
    color = 'positive' if state in {'robust', 'healthy'} else 'critical' if state in {'critical', 'stressed'} else 'warning' if state == 'constrained' else 'neutral'
    display_color = 'success' if color == 'positive' else color
    return atom(status='available', state=state, signal=signal, signal_color=color, display=state.title(), display_color=display_color, source_timestamp=source_timestamp, source_values={'spot': spot_state, 'perpetual': perpetual_state}, unit='semantic_state', parameters={'scope': 'coinglass_spot_perpetual_observed_execution_conditions'})
