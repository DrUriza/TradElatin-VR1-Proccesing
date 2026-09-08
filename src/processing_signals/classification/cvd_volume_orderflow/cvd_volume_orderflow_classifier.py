"""Semantic classification for frozen CVD volume/order-flow Processing v0.1."""
from __future__ import annotations

import copy
import math
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

CVD_VOLUME_ORDERFLOW_FAMILY = "cvd_volume_orderflow"
CLASSIFICATION_STAGE        = "classification"
CLASSIFICATION_VERSION      = "0.1.0"
PROCESSING_VERSION          = "0.1.0"
MARKETS                     = ("spot", "futures")
TIMEFRAMES                  = ("5m", "15m", "4h")
SUMMARY_WINDOWS             = ("4h", "24h")
VALID_AVAILABILITY          = {"available", "partial", "unavailable", "invalid"}
THRESHOLDS = {
    "order_flow_imbalance": {"strong_selling_max": -0.25, "neutral_min": -0.05, "neutral_max": 0.05, "strong_buying_min": 0.25},
    "buy_sell_ratio": {"strong_selling_max": 0.80, "balanced_min": 0.95, "balanced_max": 1.05, "strong_buying_min": 1.25},
    "flow_efficiency": {"moderate_min": 0.33, "high_min": 0.67, "domain_min": 0.0, "domain_max": 1.0},
    "price_vs_vwap": {"far_below_max": -0.005, "far_above_min": 0.005},
}
DIRECTION = {"buying": "positive", "strong_buying": "positive", "positive": "positive", "rising": "positive", "above": "positive",
    "far_above": "positive", "selling": "negative", "strong_selling": "negative", "negative": "negative", "falling": "negative",
    "below": "negative", "far_below": "negative", "neutral": "neutral", "balanced": "neutral", "flat": "neutral", "at_vwap": "neutral",
    "complete": "neutral", "broken": "neutral", "partial": "neutral", "low": "neutral", "moderate": "neutral", "high": "neutral"}


def _sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"invalid_{name}")
    return float(value)


def _clock(clock: Callable[[], Any] | None) -> int:
    value = time.time() if clock is None else clock()
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("invalid_clock")
    return int(value)


def _availability(status: str, reason: str | None) -> dict[str, Any]:
    return {"status": status, "reason": None if status == "available" else (reason or "source_value_unavailable")}


def _unavailable_atom(path: str, unit: str, status: str, reason: str | None, value: Any = None, threshold_id: str | None = None) -> dict[str, Any]:
    availability_status = "invalid" if status == "invalid" else ("partial" if status == "partial" and value is not None else "unavailable")
    return {"state": "unavailable", "direction": "unavailable", "value": value, "unit": unit, "source_path": path,
        "source_status": status, "threshold_id": threshold_id, "availability": _availability(availability_status, reason)}


def _atom(state: str, value: Any, unit: str, path: str, source_status: str, threshold_id: str,
          reason: str | None = None, *, neutral_direction: bool = False) -> dict[str, Any]:
    status = "partial" if source_status == "partial" else "available"
    return {"state": state, "direction": "neutral" if neutral_direction else DIRECTION[state], "value": value, "unit": unit,
        "source_path": path, "source_status": source_status, "threshold_id": threshold_id,
        "availability": _availability(status, reason or ("source_partial" if status == "partial" else None))}


def _group(state: str) -> str | None:
    if state in {"buying", "strong_buying"}:
        return "positive"
    if state in {"selling", "strong_selling"}:
        return "negative"
    if state == "neutral":
        return "neutral"
    return None


def _aggregate_status(statuses: Sequence[str]) -> str:
    if "invalid" in statuses:
        return "invalid"
    if "unavailable" in statuses:
        return "unavailable"
    if "partial" in statuses:
        return "partial"
    return "available"


def _effective_metric(metric: Any, parent_status: str, parent_reason: str | None) -> Any:
    if not isinstance(metric, Mapping) or parent_status != "partial" or metric.get("status") != "available":
        return metric
    return {**metric, "status": "partial", "reason": parent_reason or "source_partial"}


class CvdVolumeOrderflowClassifier:
    def __init__(self, *, clock: Callable[[], Any] | None = None) -> None:
        self.clock = clock

    def validate_processing_contract(self, contract: Any) -> None:
        if not isinstance(contract, Mapping):
            raise ValueError("processing_contract_must_be_mapping")
        if contract.get("family") != CVD_VOLUME_ORDERFLOW_FAMILY:
            raise ValueError("incompatible_family")
        if contract.get("stage") != "processing":
            raise ValueError("incompatible_stage")
        if contract.get("version") != PROCESSING_VERSION:
            raise ValueError("incompatible_processing_version")
        if contract.get("mode") not in {"bootstrap", "incremental", "recovery"}:
            raise ValueError("invalid_mode")
        context, parameters, markets = contract.get("context"), contract.get("parameters"), contract.get("markets")
        if not isinstance(context, Mapping) or not isinstance(parameters, Mapping) or not isinstance(markets, Mapping) or set(markets) != set(MARKETS):
            raise ValueError("invalid_processing_structure")
        cross_market = contract.get("cross_market")
        if not isinstance(cross_market, Mapping):
            raise ValueError("invalid_cross_market")
        if not isinstance(cross_market.get("window_summaries"), Mapping) or set(cross_market["window_summaries"]) != set(SUMMARY_WINDOWS):
            raise ValueError("invalid_cross_market_summaries")
        ratios = cross_market.get("volume_ratios")
        if not isinstance(ratios, Mapping) or not isinstance(ratios.get("futures_vs_spot"), Mapping) or not isinstance(ratios["futures_vs_spot"].get("4h"), Mapping):
            raise ValueError("invalid_cross_market_ratio")
        if context.get("data_mode") == "synthetic" and context.get("is_demo") is not True:
            raise ValueError("synthetic_requires_demo")
        for market in MARKETS:
            payload = markets[market]
            if not isinstance(payload, Mapping) or not isinstance(payload.get("timeframes"), Mapping) or set(payload["timeframes"]) != set(TIMEFRAMES):
                raise ValueError("invalid_market_timeframes")
            summaries = payload.get("window_summaries")
            if not isinstance(summaries, Mapping) or set(summaries) != set(SUMMARY_WINDOWS):
                raise ValueError("invalid_window_summaries")
            for timeframe in TIMEFRAMES:
                source = payload["timeframes"][timeframe]
                if not isinstance(source, Mapping) or source.get("status") not in VALID_AVAILABILITY:
                    raise ValueError("invalid_timeframe")
                current = source.get("current")
                if current is not None and not isinstance(current, Mapping):
                    raise ValueError("invalid_current")
                if source["status"] == "unavailable" and current is not None:
                    raise ValueError("unavailable_timeframe_has_current")
                if current is not None and (type(current.get("timestamp")) is not int or current["timestamp"] < 0):
                    raise ValueError("invalid_current_timestamp")
            for window in SUMMARY_WINDOWS:
                if not isinstance(summaries[window], Mapping) or summaries[window].get("status") not in VALID_AVAILABILITY:
                    raise ValueError("invalid_summary")
                for field in ("first_timestamp", "last_timestamp"):
                    value = summaries[window].get(field)
                    if value is not None and (type(value) is not int or value < 0):
                        raise ValueError("invalid_summary_timestamp")
        for window in SUMMARY_WINDOWS:
            source = cross_market["window_summaries"][window]
            if not isinstance(source, Mapping) or source.get("status") not in VALID_AVAILABILITY:
                raise ValueError("invalid_cross_market_summary")

    def classify_delta(self, value: Any, status: str, reason: str | None, path: str) -> dict[str, Any]:
        if value is None or status in {"unavailable", "invalid"}:
            return _unavailable_atom(path, "USD", status, reason, value, "volume_delta_sign_v1")
        numeric = _finite(value, "volume_delta")
        state = "negative" if numeric < 0 else ("positive" if numeric > 0 else "neutral")
        return _atom(state, value, "USD", path, status, "volume_delta_sign_v1", reason)

    def classify_order_flow_imbalance(self, metric: Any, path: str) -> dict[str, Any]:
        return self._classify_nested(metric, path, "decimal", "order_flow_imbalance_v1", self._imbalance_state)

    @staticmethod
    def _imbalance_state(value: float) -> str:
        if value <= -0.25:
            return "strong_selling"
        if value < -0.05:
            return "selling"
        if value <= 0.05:
            return "neutral"
        if value < 0.25:
            return "buying"
        return "strong_buying"

    def classify_buy_sell_ratio(self, metric: Any, path: str) -> dict[str, Any]:
        return self._classify_nested(metric, path, "ratio", "buy_sell_ratio_v1", self._ratio_state)

    @staticmethod
    def _ratio_state(value: float) -> str:
        if value <= 0.80:
            return "strong_selling"
        if value < 0.95:
            return "selling"
        if value <= 1.05:
            return "balanced"
        if value < 1.25:
            return "buying"
        return "strong_buying"

    def classify_cvd_direction(self, ohlc: Any, status: str, reason: str | None, path: str) -> dict[str, Any]:
        if ohlc is None or status in {"unavailable", "invalid"}:
            return _unavailable_atom(path, "USD", status, reason, None, "cvd_open_close_direction_v1")
        if not isinstance(ohlc, Mapping):
            raise ValueError("invalid_cvd_ohlc")
        open_value, close_value = _finite(ohlc.get("open"), "cvd_open"), _finite(ohlc.get("close"), "cvd_close")
        _finite(ohlc.get("high"), "cvd_high")
        _finite(ohlc.get("low"), "cvd_low")
        state = "falling" if close_value < open_value else ("rising" if close_value > open_value else "flat")
        return _atom(state, {"open": ohlc["open"], "close": ohlc["close"]}, "USD", path, status, "cvd_open_close_direction_v1", reason)

    def classify_flow_efficiency(self, metric: Any, path: str) -> dict[str, Any]:
        return self._classify_nested(metric, path, "decimal", "flow_efficiency_v1", self._efficiency_state, neutral_direction=True, domain=(0, 1))

    @staticmethod
    def _efficiency_state(value: float) -> str:
        return "low" if value < 0.33 else ("moderate" if value < 0.67 else "high")

    def classify_price_vs_vwap(self, metric: Any, path: str) -> dict[str, Any]:
        return self._classify_nested(metric, path, "decimal", "price_vs_vwap_v1", self._price_state)

    @staticmethod
    def _price_state(value: float) -> str:
        if value <= -0.005:
            return "far_below"
        if value < 0:
            return "below"
        if value == 0:
            return "at_vwap"
        if value < 0.005:
            return "above"
        return "far_above"

    def _classify_nested(self, metric: Any, path: str, unit: str, threshold_id: str, state_fn: Any,
                         *, neutral_direction: bool = False, domain: tuple[float, float] | None = None) -> dict[str, Any]:
        if not isinstance(metric, Mapping):
            raise ValueError("invalid_metric")
        status, reason, value = metric.get("status"), metric.get("reason"), metric.get("value")
        if status not in VALID_AVAILABILITY:
            raise ValueError("invalid_metric_status")
        if value is None or status in {"unavailable", "invalid"}:
            return _unavailable_atom(path, unit, status, reason, value, threshold_id)
        numeric = _finite(value, threshold_id)
        if domain is not None and not domain[0] <= numeric <= domain[1]:
            raise ValueError(f"{threshold_id}_outside_domain")
        return _atom(state_fn(numeric), value, unit, path, status, threshold_id, reason, neutral_direction=neutral_direction)

    def classify_continuity(self, value: Any, status: str, reason: str | None, path: str) -> dict[str, Any]:
        if value not in {"complete", "broken"}:
            return _unavailable_atom(path, "state", status if status in VALID_AVAILABILITY else "unavailable", reason, value, "continuity_mapping_v1")
        return _atom(value, value, "state", path, status, "continuity_mapping_v1", reason, neutral_direction=True)

    def classify_coverage(self, coverage: Any, partial: Any, status: str, reason: str | None, path: str) -> dict[str, Any]:
        if status == "invalid":
            return _unavailable_atom(path, "state", status, reason, None, "coverage_mapping_v1")
        if status == "unavailable":
            return _unavailable_atom(path, "state", status, reason, None, "coverage_mapping_v1")
        if type(coverage) is not bool or type(partial) is not bool:
            raise ValueError("invalid_coverage_flags")
        state = "complete" if coverage and not partial and status == "available" else "partial"
        return _atom(state, coverage, "boolean", path, status, "coverage_mapping_v1", reason, neutral_direction=True)

    def classify_timeframe(self, source: Mapping[str, Any], path: str) -> dict[str, Any]:
        status, reason, current = source["status"], source.get("reason"), source.get("current")
        if current is None:
            atoms = {name: _unavailable_atom(f"{path}.current", "state", status, reason) for name in
                ("delta_state", "order_flow_state", "buy_sell_pressure_state", "cvd_direction_state", "flow_efficiency_state", "continuity_state", "coverage_state")}
            return {"timestamp": None, "atoms": atoms, "availability": _availability("invalid" if status == "invalid" else "unavailable", reason)}
        atoms = {"delta_state": self.classify_delta(current.get("volume_delta_usd"), status, reason, f"{path}.current.volume_delta_usd"),
            "order_flow_state": self.classify_order_flow_imbalance(_effective_metric(current.get("order_flow_imbalance"), status, reason), f"{path}.current.order_flow_imbalance.value"),
            "buy_sell_pressure_state": self.classify_buy_sell_ratio(_effective_metric(current.get("buy_sell_ratio"), status, reason), f"{path}.current.buy_sell_ratio.value"),
            "cvd_direction_state": self.classify_cvd_direction(current.get("cvd_ohlc_usd"), status, reason, f"{path}.current.cvd_ohlc_usd"),
            "flow_efficiency_state": self.classify_flow_efficiency(_effective_metric(current.get("flow_efficiency"), status, reason), f"{path}.current.flow_efficiency.value"),
            "continuity_state": self.classify_continuity(current.get("continuity_status"), status, reason, f"{path}.current.continuity_status"),
            "coverage_state": self.classify_coverage(current.get("coverage_complete"), current.get("is_partial"), status, reason, f"{path}.current.coverage_complete")}
        atom_statuses = [atom["availability"]["status"] for atom in atoms.values()]
        availability_status = "invalid" if "invalid" in atom_statuses or status == "invalid" else (
            "partial" if status == "partial" or any(item != "available" for item in atom_statuses) or current.get("continuity_status") == "broken" else "available")
        return {"timestamp": current.get("timestamp"), "atoms": atoms,
            "availability": _availability(availability_status, reason or ("timeframe_atoms_incomplete" if availability_status != "available" else None))}

    def classify_window_summary(self, source: Mapping[str, Any], path: str) -> dict[str, Any]:
        status, reason = source["status"], source.get("reason")
        atoms = {"delta_state": self.classify_delta(source.get("volume_delta_usd"), status, reason, f"{path}.volume_delta_usd"),
            "order_flow_state": self.classify_order_flow_imbalance(_effective_metric(source.get("order_flow_imbalance"), status, reason), f"{path}.order_flow_imbalance.value"),
            "buy_sell_pressure_state": self.classify_buy_sell_ratio(_effective_metric(source.get("buy_sell_ratio"), status, reason), f"{path}.buy_sell_ratio.value"),
            "flow_efficiency_state": self.classify_flow_efficiency(_effective_metric(source.get("flow_efficiency"), status, reason), f"{path}.flow_efficiency.value"),
            "coverage_state": self.classify_coverage(source.get("coverage_complete"), not source.get("coverage_complete", False), status, reason, f"{path}.coverage_complete")}
        atom_statuses = [atom["availability"]["status"] for atom in atoms.values()]
        availability_status = "invalid" if "invalid" in atom_statuses or status == "invalid" else (
            "partial" if status == "partial" or any(item != "available" for item in atom_statuses) else ("unavailable" if status == "unavailable" else "available"))
        return {"timestamp": source.get("last_timestamp"), "atoms": atoms,
            "availability": _availability(availability_status, reason or ("summary_atoms_incomplete" if availability_status != "available" else None))}

    def classify_futures_vs_spot_ratio(self, source: Mapping[str, Any], path: str) -> dict[str, Any]:
        status = source.get("status", "unavailable")
        reason = source.get("reason")
        value = source.get("value")
        if value is None or status in {"unavailable", "invalid"}:
            return _unavailable_atom(path, "ratio", status, reason, value, "futures_vs_spot_volume_ratio_v1")
        numeric = _finite(value, "futures_vs_spot_volume_ratio")
        if numeric > 1.05:
            state = "futures_dominant"
        elif numeric < 0.95:
            state = "spot_dominant"
        else:
            state = "balanced"
        return {
            "state": state,
            "direction": "neutral",
            "value": numeric,
            "unit": "ratio",
            "source_path": path,
            "source_status": status,
            "threshold_id": "futures_vs_spot_volume_ratio_v1",
            "availability": _availability("partial" if status == "partial" else "available", reason),
        }

    def build_market_agreement(self, classified: Mapping[str, Any]) -> dict[str, Any]:
        atoms = {market: classified[market]["window_summaries"]["4h"]["atoms"]["order_flow_state"] for market in MARKETS}
        states = {market: atoms[market]["state"] for market in MARKETS}
        spot_group, futures_group = _group(states["spot"]), _group(states["futures"])
        if None in {spot_group, futures_group}:
            state = "unavailable"
        elif spot_group == futures_group == "positive":
            state = "confirmed_buying"
        elif spot_group == futures_group == "negative":
            state = "confirmed_selling"
        elif spot_group == futures_group == "neutral":
            state = "balanced"
        elif {spot_group, futures_group} == {"positive", "negative"}:
            state = "divergent"
        else:
            state = "mixed"
        source_status = _aggregate_status([atoms["spot"]["availability"]["status"], atoms["futures"]["availability"]["status"]])
        if state == "unavailable":
            availability_status, availability_reason = "unavailable", "source_state_unavailable"
        elif source_status != "available":
            availability_status, availability_reason = source_status, "source_partial"
        else:
            # mixed/divergent is a legitimate market relationship, not a data-quality defect.
            availability_status, availability_reason = "available", None
        return {"state": state, "spot_state": states["spot"], "futures_state": states["futures"],
            "availability": _availability(availability_status, availability_reason)}

    def build_temporal_alignment(self, classified: Mapping[str, Any]) -> dict[str, Any]:
        output = {}
        for market in MARKETS:
            one_atom = classified[market]["window_summaries"]["4h"]["atoms"]["order_flow_state"]
            day_atom = classified[market]["window_summaries"]["24h"]["atoms"]["order_flow_state"]
            one, day = one_atom["state"], day_atom["state"]
            one_group, day_group = _group(one), _group(day)
            if None in {one_group, day_group}:
                state = "unavailable"
            elif one_group == day_group == "positive":
                state = "persistent_buying"
            elif one_group == day_group == "negative":
                state = "persistent_selling"
            elif one_group == day_group == "neutral":
                state = "balanced"
            elif {one_group, day_group} == {"positive", "negative"}:
                state = "reversal"
            else:
                state = "mixed"
            source_status = _aggregate_status([one_atom["availability"]["status"], day_atom["availability"]["status"]])
            availability_status = "unavailable" if state == "unavailable" else ("partial" if source_status == "partial" else "available")
            output[market] = {"state": state, "one_hour_state": one, "twenty_four_hour_state": day,
                "availability": _availability(availability_status, "source_state_unavailable" if state == "unavailable" else (
                    "source_partial" if availability_status == "partial" else None))}
        return output

    def build_snapshots(self, contract: Mapping[str, Any]) -> dict[str, Any]:
        markets = {}
        for market in MARKETS:
            source_market = contract["markets"][market]
            timeframes = {}
            for timeframe in TIMEFRAMES:
                source = source_market["timeframes"][timeframe]
                current = source.get("current")
                timeframes[timeframe] = None if current is None else {"timestamp": current.get("timestamp"), "volume_delta_usd": current.get("volume_delta_usd"),
                    "buy_sell_ratio": copy.deepcopy(current.get("buy_sell_ratio")), "order_flow_imbalance": copy.deepcopy(current.get("order_flow_imbalance")),
                    "flow_efficiency": copy.deepcopy(current.get("flow_efficiency")), "cvd_ohlc_usd": copy.deepcopy(current.get("cvd_ohlc_usd")),
                    "continuity_status": current.get("continuity_status"), "coverage_complete": current.get("coverage_complete"),
                    "is_partial": current.get("is_partial"), "status": source["status"]}
            summaries = {window: {key: copy.deepcopy(source_market["window_summaries"][window].get(key)) for key in
                ("first_timestamp", "last_timestamp", "volume_delta_usd", "buy_sell_ratio", "order_flow_imbalance", "flow_efficiency", "coverage_complete", "status")}
                for window in SUMMARY_WINDOWS}
            price = source_market.get("price_vs_vwap", {})
            markets[market] = {"timeframes": timeframes, "window_summaries": summaries,
                "price_vs_vwap": {"timestamp": price.get("price_timestamp"), "value": price.get("value"), "status": price.get("status"), "reason": price.get("reason")}}
        return {"markets": markets, "cross_market": copy.deepcopy(contract.get("cross_market", {}))}

    def build_interpreted_events(self, contract: Mapping[str, Any], classified: Mapping[str, Any], agreement: Mapping[str, Any]) -> list[dict[str, Any]]:
        events = []
        for market in MARKETS:
            for timeframe in TIMEFRAMES:
                path = f"markets.{market}.timeframes.{timeframe}.records"
                records = contract["markets"][market]["timeframes"][timeframe].get("records", [])
                if _sequence(records) and len(records) >= 2:
                    previous, current = records[-2], records[-1]
                    previous_atom = self.classify_order_flow_imbalance(previous.get("order_flow_imbalance"), f"{path}[-2].order_flow_imbalance.value")
                    current_atom = self.classify_order_flow_imbalance(current.get("order_flow_imbalance"), f"{path}[-1].order_flow_imbalance.value")
                    if _group(previous_atom["state"]) != _group(current_atom["state"]) and None not in {_group(previous_atom["state"]), _group(current_atom["state"])}:
                        events.append({"event_id": f"cvd:{market}:{timeframe}:order_flow_transition:{current['timestamp']}",
                            "event_type": "order_flow_transition", "timestamp": current["timestamp"], "market": market, "timeframe": timeframe,
                            "previous_state": previous_atom["state"], "current_state": current_atom["state"], "severity": "medium",
                            "source_paths": [previous_atom["source_path"], current_atom["source_path"]], "availability": _availability("available", None)})
                current = contract["markets"][market]["timeframes"][timeframe].get("current")
                previous = records[-2] if _sequence(records) and len(records) >= 2 else None
                if isinstance(current, Mapping) and current.get("continuity_status") == "broken" and (not isinstance(previous, Mapping) or previous.get("continuity_status") != "broken"):
                    events.append({"event_id": f"cvd:{market}:{timeframe}:continuity_break:{current['timestamp']}", "event_type": "continuity_break",
                        "timestamp": current["timestamp"], "market": market, "timeframe": timeframe, "severity": "high",
                        "source_paths": [f"markets.{market}.timeframes.{timeframe}.current.continuity_status"], "availability": _availability("available", None)})
        unique = {event["event_id"]: event for event in events}
        return sorted(unique.values(), key=lambda event: (event["timestamp"], event["event_type"], event["market"], event["timeframe"], event["event_id"]))

    def build_technical_events(self, contract: Mapping[str, Any]) -> dict[str, Any]:
        by_id: dict[str, Any] = {}
        ids: list[str] = []
        technical = contract.get("technical_analysis", {}).get("markets", {})
        for market in MARKETS:
            for timeframe in TIMEFRAMES:
                timeframe_payload = technical.get(market, {}).get("timeframes", {}).get(timeframe, {})
                indicators = timeframe_payload.get("indicators", {}) if isinstance(timeframe_payload, Mapping) else {}
                for candidate in timeframe_payload.get("cross_candidates", []):
                    timestamp = candidate.get("timestamp")
                    first, second, sign = candidate.get("first_series"), candidate.get("second_series"), candidate.get("direction")
                    if type(timestamp) is not int or not isinstance(first, str) or not isinstance(second, str) or sign not in {-1, 1}:
                        raise ValueError("invalid_technical_cross_candidate")
                    # Screen-B stochastic arrows are intentionally gated to
                    # oversold/overbought zones. The HMI must never invent this
                    # gate from plotted values.
                    if {first, second} == {"k", "d"}:
                        stochastic = indicators.get("stochastic", {}) if isinstance(indicators, Mapping) else {}
                        k_series = stochastic.get("k", []) if isinstance(stochastic, Mapping) else []
                        d_series = stochastic.get("d", []) if isinstance(stochastic, Mapping) else []
                        ts_series = timeframe_payload.get("timestamps", []) if isinstance(timeframe_payload, Mapping) else []
                        if isinstance(ts_series, Sequence) and timestamp in ts_series:
                            idx = list(ts_series).index(timestamp)
                            if idx < len(k_series) and idx < len(d_series):
                                k_value, d_value = k_series[idx], d_series[idx]
                                if isinstance(k_value, (int, float)) and isinstance(d_value, (int, float)):
                                    if sign == 1 and not (k_value <= 20 and d_value <= 20):
                                        continue
                                    if sign == -1 and not (k_value >= 80 and d_value >= 80):
                                        continue
                    direction = "bullish" if sign == 1 else "bearish"
                    semantic_id = f"{first}_{'above' if sign == 1 else 'below'}_{second}"
                    if {first, second} <= {"macd", "signal"}:
                        event_group = "macd_cross"
                    elif {first, second} <= {"plus_di", "minus_di"}:
                        event_group = "adx_cross"
                    elif {first, second} <= {"k", "d"}:
                        event_group = "stochastic_cross"
                    elif "regression_channel.middle" in {first, second} or "bollinger_bands.middle" in {first, second}:
                        event_group = "channel_cross"
                    else:
                        event_group = "moving_average_cross"
                    event_uid = f"{market}:{timeframe}:{timestamp}:technical_cross:{semantic_id}"
                    label = semantic_id.replace("_", " ").upper()
                    by_id[event_uid] = {
                        "event_uid": event_uid,
                        "event_id": semantic_id,
                        "event_type": "technical_cross",
                        "event_group": event_group,
                        "timestamp": timestamp,
                        "label": label,
                        "signal": direction,
                        "marker": "arrow_up" if sign == 1 else "arrow_down",
                        "source": {"market": market, "timeframe": timeframe},
                        "display": {"screen_a": True, "screen_b": True},
                    }
                    ids.append(event_uid)
        return {"by_id": by_id, "technical_cross_ids": ids}

    def evaluate_availability(self, classified: Mapping[str, Any], agreement: Mapping[str, Any]) -> dict[str, Any]:
        markets, core_statuses, enrichment_statuses = {}, [], []
        for market in MARKETS:
            core = [classified[market]["timeframes"][timeframe]["availability"]["status"] for timeframe in TIMEFRAMES]
            core.extend(classified[market]["window_summaries"][window]["availability"]["status"] for window in SUMMARY_WINDOWS)
            enrichment = classified[market]["price_vs_vwap"]["availability"]["status"]
            market_core, market_enrichment = _aggregate_status(core), enrichment
            market_status = market_core if market_enrichment != "invalid" else "invalid"
            markets[market] = {"status": market_status, "core_status": market_core,
                "enrichment_status": market_enrichment, "reason": None if market_core == "available" else "market_core_classification_incomplete"}
            core_statuses.append(market_core)
            enrichment_statuses.append(market_enrichment)
        confirmation_status = agreement["availability"]["status"]
        core_statuses.append(confirmation_status)
        core, enrichment = _aggregate_status(core_statuses), _aggregate_status(enrichment_statuses)
        status = "invalid" if core == "invalid" or enrichment == "invalid" else core
        no_safe_base = all(classified[market]["timeframes"][timeframe]["availability"]["status"] in {"unavailable", "invalid"}
            for market in ("spot", "futures") for timeframe in TIMEFRAMES)
        return {"status": status, "core_status": core, "enrichment_status": enrichment, "markets": markets,
            "confirmations": {"market_agreement_4h": agreement["availability"]}, "no_safe_base": no_safe_base,
            "reason": None if status == "available" else "classification_incomplete"}

    def evaluate_quality(self, availability: Mapping[str, Any], processing_quality: Mapping[str, Any]) -> dict[str, Any]:
        processing_status = processing_quality.get("status")
        core = "invalid" if availability["no_safe_base"] or processing_status == "invalid" or availability["core_status"] == "invalid" else (
            "ok" if processing_status == "ok" and availability["core_status"] == "available" else "partial")
        enrichment = "invalid" if availability["enrichment_status"] == "invalid" else (
            "ok" if availability["enrichment_status"] == "available" else "partial")
        # price-vs-VWAP is optional enrichment. Missing enrichment must not
        # degrade a fully valid order-flow classification.
        status = "invalid" if core == "invalid" or enrichment == "invalid" else ("ok" if core == "ok" else "partial")
        warnings = [] if status == "ok" else sorted({f"classification_availability:{availability['status']}", f"processing_quality:{processing_status}"})
        errors = [] if status != "invalid" else sorted({item for item in warnings if "invalid" in item})
        return {"status": status, "core_status": core, "enrichment_status": enrichment, "processing_quality_status": processing_status,
            "warnings": warnings, "errors": errors}

    def run(self, contract: Mapping[str, Any]) -> dict[str, Any]:
        self.validate_processing_contract(contract)
        classified = {}
        for market in MARKETS:
            source = contract["markets"][market]
            timeframes = {timeframe: self.classify_timeframe(source["timeframes"][timeframe], f"markets.{market}.timeframes.{timeframe}") for timeframe in TIMEFRAMES}
            summaries = {window: self.classify_window_summary(source["window_summaries"][window], f"markets.{market}.window_summaries.{window}") for window in SUMMARY_WINDOWS}
            price = self.classify_price_vs_vwap(source.get("price_vs_vwap"), f"markets.{market}.price_vs_vwap.value")
            classified[market] = {"timeframes": timeframes, "window_summaries": summaries, "price_vs_vwap": price}
        agreement = self.build_market_agreement(classified)
        temporal = self.build_temporal_alignment(classified)
        cross_source = contract["cross_market"]
        cross_summaries = {
            window: self.classify_window_summary(
                cross_source["window_summaries"][window],
                f"cross_market.window_summaries.{window}",
            )
            for window in SUMMARY_WINDOWS
        }
        ratio_source = cross_source["volume_ratios"]["futures_vs_spot"]["4h"]
        cross_market = {
            "window_summaries": cross_summaries,
            "volume_ratios": {"futures_vs_spot": {"4h": self.classify_futures_vs_spot_ratio(
                ratio_source, "cross_market.volume_ratios.futures_vs_spot.4h.value")}},
        }
        for market in MARKETS:
            statuses = [classified[market]["timeframes"][timeframe]["availability"]["status"] for timeframe in TIMEFRAMES]
            statuses.extend(classified[market]["window_summaries"][window]["availability"]["status"] for window in SUMMARY_WINDOWS)
            classified[market]["availability"] = _availability(_aggregate_status(statuses), "market_core_incomplete" if any(item != "available" for item in statuses) else None)
        snapshots = self.build_snapshots(contract)
        events = self.build_interpreted_events(contract, classified, agreement)
        technical_events = self.build_technical_events(contract)
        availability = self.evaluate_availability(classified, agreement)
        quality = self.evaluate_quality(availability, contract.get("quality", {}))
        context = contract["context"]
        classification_timestamp = _clock(self.clock)
        return {"family": CVD_VOLUME_ORDERFLOW_FAMILY, "stage": CLASSIFICATION_STAGE, "version": CLASSIFICATION_VERSION, "mode": contract["mode"],
            "context": {"base_asset": context.get("base_asset"), "pair_symbol": context.get("pair_symbol"), "markets": list(MARKETS),
                "timeframes": list(TIMEFRAMES), "data_mode": context.get("data_mode"), "is_demo": context.get("is_demo"),
                "reference_timestamp": context.get("reference_timestamp"), "processing_timestamp": context.get("processing_timestamp"),
                "classification_timestamp": classification_timestamp},
            "parameters": {"thresholds": copy.deepcopy(THRESHOLDS), "source_processing_version": PROCESSING_VERSION,
                "classification_policy": "interpret_processing_values_without_recalculation"},
            "classifications": {"markets": classified, "cross_market": cross_market}, "snapshots": snapshots,
            "confirmations": {"market_agreement_4h": agreement, "temporal_alignment": temporal},
            "interpreted_events": events, "technical_events": technical_events,
            "availability": availability, "quality": quality}


def classify_cvd_volume_orderflow(processing_contract: Mapping[str, Any], *, clock: Callable[[], Any] | None = None) -> dict[str, Any]:
    return CvdVolumeOrderflowClassifier(clock=clock).run(processing_contract)
