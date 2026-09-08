"""Pure visual contract builder for CVD volume/order-flow v0.1."""
from __future__ import annotations

import copy
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any


FAMILY = "cvd_volume_orderflow"
PROCESSING_VERSION = "0.1.0"
CLASSIFICATION_VERSION = "0.1.0"
SCREEN_SCHEMA = "trad_elatin.cvd_volume_orderflow.screen.v1"
SCREEN_VERSION = "1.5.0"
SCREEN_ID = "cvd_volume_orderflow"
SCREEN_ROUTE = "/cvd-orderflow"
SCREEN_TITLE = "CVD & ORDER FLOW"
SCREEN_SUBTITLE = "Cumulative volume delta, trades & market microstructure"
MARKETS = ("spot", "futures")
TIMEFRAMES = ("5m", "15m", "4h")
TIMEFRAME_SECONDS = {"5m": 300, "15m": 900, "4h": 14400}
DEFAULT_MARKET = "spot"
DEFAULT_TIMEFRAME = "15m"
DISPLAY_POINT_LIMIT = 220
CALCULATION_HISTORY_LIMIT = 730
VALID_SOURCE_STATUS = {"available", "partial", "unavailable", "invalid"}
VALID_QUALITY_STATUS = {"ok", "partial", "invalid"}
_PRIORITY = {"available": 0, "partial": 1, "unavailable": 2, "invalid": 3}


def _sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _find_non_finite_path(value: Any, path: str) -> str | None:
    if isinstance(value, float) and not math.isfinite(value):
        return path
    if isinstance(value, Mapping):
        for key, child in value.items():
            found = _find_non_finite_path(child, f"{path}.{key}")
            if found is not None:
                return found
    elif _sequence(value):
        for index, child in enumerate(value):
            found = _find_non_finite_path(child, f"{path}[{index}]")
            if found is not None:
                return found
    return None


def _finite_tree(value: Any, path: str = "bundle") -> None:
    """Validate finiteness without materializing a path string for every node."""
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, float) and not math.isfinite(current):
            found = _find_non_finite_path(value, path)
            raise ValueError(f"non_finite_value:{found or path}")
        if isinstance(current, Mapping):
            stack.extend(current.values())
        elif _sequence(current):
            stack.extend(current)


def _number(value: Any, path: str, *, nullable: bool = True) -> Any:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"invalid_numeric_value:{path}")
    if value == 0:
        return 0.0 if isinstance(value, float) else 0
    return value


def _timestamp(value: Any, path: str, *, nullable: bool = True) -> Any:
    if value is None and nullable:
        return None
    if type(value) is not int or value < 0:
        raise ValueError(f"invalid_timestamp:{path}")
    return value


def _status(value: Any, path: str) -> str:
    if value not in VALID_SOURCE_STATUS:
        raise ValueError(f"invalid_source_status:{path}")
    return value


def _reason(status: str, reason: Any, fallback: str = "source_value_unavailable") -> str | None:
    return None if status == "available" else (reason if isinstance(reason, str) and reason else fallback)


def _combine(statuses: Sequence[str]) -> str:
    if not statuses:
        return "unavailable"
    if all(value == "unavailable" for value in statuses):
        return "unavailable"
    if "invalid" in statuses:
        return "invalid"
    if any(value != "available" for value in statuses):
        return "partial"
    return "available"


def _availability(status: str, reason: str | None, paths: Sequence[str]) -> dict[str, Any]:
    return {"status": status, "reason": _reason(status, reason), "source_paths": list(paths)}


def _metric(source: Any, path: str) -> tuple[Any, str, str | None]:
    if not isinstance(source, Mapping):
        raise ValueError(f"invalid_metric:{path}")
    status = _status(source.get("status"), f"{path}.status")
    return _number(source.get("value"), f"{path}.value"), status, _reason(status, source.get("reason"))


def _classification(atom: Any) -> dict[str, Any] | None:
    if not isinstance(atom, Mapping):
        return None
    return {"state": copy.deepcopy(atom.get("state")), "direction": copy.deepcopy(atom.get("direction"))}


def _copy_classification(value: Any) -> Any:
    """Copy a Classification fragment and qualify its contract-relative paths."""
    if isinstance(value, Mapping):
        output = {}
        for key, child in value.items():
            if key in {"source_path", "source_paths"}:
                paths = [child] if key == "source_path" else child
                if not _sequence(paths):
                    raise ValueError("invalid_source_paths")
                qualified = []
                for path in paths:
                    if not isinstance(path, str):
                        raise ValueError("invalid_source_path")
                    if path.startswith(("processing.", "classification.")):
                        qualified.append(path)
                    elif path.startswith("markets."):
                        qualified.append(f"processing.{path}")
                    elif path.startswith("confirmations."):
                        qualified.append(f"classification.{path}")
                    else:
                        raise ValueError("invalid_source_path")
                output[key] = qualified[0] if key == "source_path" else qualified
            else:
                output[key] = _copy_classification(child)
        return output
    if _sequence(value):
        return [_copy_classification(child) for child in value]
    return copy.deepcopy(value)


def _validated_paths(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key == "source_path":
                paths = [child]
            elif key == "source_paths":
                if not _sequence(child):
                    raise ValueError("invalid_source_paths")
                paths = child
            else:
                paths = []
            for path in paths:
                if not isinstance(path, str) or not path.startswith(("processing.", "classification.")):
                    raise ValueError("invalid_source_path")
            _validated_paths(child)
    elif _sequence(value):
        for child in value:
            _validated_paths(child)


class CvdVolumeOrderflowContractBuilder:
    """Validate two frozen contracts and project them into a screen contract."""

    def __init__(self, *, selected_market: str = DEFAULT_MARKET, selected_timeframe: str = DEFAULT_TIMEFRAME,
                 display_point_limit: int = DISPLAY_POINT_LIMIT) -> None:
        if selected_market not in MARKETS:
            raise ValueError("invalid_selected_market")
        if selected_timeframe not in TIMEFRAMES:
            raise ValueError("invalid_selected_timeframe")
        if type(display_point_limit) is not int or not 0 < display_point_limit <= DISPLAY_POINT_LIMIT:
            raise ValueError("invalid_display_point_limit")
        self.selected_market = selected_market
        self.selected_timeframe = selected_timeframe
        self.display_point_limit = display_point_limit

    def validate_bundle(self, bundle: Any) -> None:
        if not isinstance(bundle, Mapping):
            raise ValueError("bundle_must_be_mapping")
        if set(bundle) != {"processing", "classification"}:
            raise ValueError("bundle_root_keys_mismatch")
        self.validate_processing_contract(bundle["processing"])
        self.validate_classification_contract(bundle["classification"])
        self.validate_bundle_consistency(bundle["processing"], bundle["classification"])
        _finite_tree(bundle)

    def validate_processing_contract(self, contract: Any) -> None:
        if not isinstance(contract, Mapping):
            raise ValueError("processing_contract_must_be_mapping")
        if contract.get("family") != FAMILY or contract.get("stage") != "processing" or contract.get("version") != PROCESSING_VERSION:
            raise ValueError("incompatible_processing_contract")
        if contract.get("mode") not in {"bootstrap", "incremental", "recovery"}:
            raise ValueError("invalid_processing_mode")
        context, parameters, markets, quality = (contract.get(key) for key in ("context", "parameters", "markets", "quality"))
        if not all(isinstance(value, Mapping) for value in (context, parameters, markets, quality)) or set(markets) != set(MARKETS):
            raise ValueError("invalid_processing_structure")
        cross_market = contract.get("cross_market")
        if not isinstance(cross_market, Mapping) or not isinstance(cross_market.get("window_summaries"), Mapping):
            raise ValueError("invalid_cross_market")
        if context.get("data_mode") == "synthetic" and context.get("is_demo") is not True:
            raise ValueError("synthetic_requires_demo")
        if context.get("data_mode") == "live" and context.get("is_demo") is not False:
            raise ValueError("live_cannot_be_demo")
        for key in ("reference_timestamp", "processing_timestamp"):
            _timestamp(context.get(key), f"processing.context.{key}", nullable=False)
        if quality.get("status") not in VALID_QUALITY_STATUS:
            raise ValueError("invalid_processing_quality")
        for market in MARKETS:
            payload = markets[market]
            if not isinstance(payload, Mapping) or not isinstance(payload.get("timeframes"), Mapping) or set(payload["timeframes"]) != set(TIMEFRAMES):
                raise ValueError("invalid_processing_timeframes")
            if not isinstance(payload.get("window_summaries"), Mapping) or set(payload["window_summaries"]) != {"4h", "24h"}:
                raise ValueError("invalid_processing_summaries")
            for timeframe in TIMEFRAMES:
                source = payload["timeframes"][timeframe]
                if not isinstance(source, Mapping) or not _sequence(source.get("records")):
                    raise ValueError("invalid_processing_timeframe")
                _status(source.get("status"), f"processing.{market}.{timeframe}")
                previous = None
                for row in source["records"]:
                    if not isinstance(row, Mapping):
                        raise ValueError("invalid_processing_record")
                    timestamp = _timestamp(row.get("timestamp"), "processing.record.timestamp", nullable=False)
                    if previous is not None and timestamp <= previous:
                        raise ValueError("records_not_strictly_ascending")
                    previous = timestamp
                if source.get("current") is not None and not isinstance(source["current"], Mapping):
                    raise ValueError("invalid_processing_current")
            for window in ("4h", "24h"):
                if not isinstance(payload["window_summaries"][window], Mapping):
                    raise ValueError("invalid_processing_summary")
                _status(payload["window_summaries"][window].get("status"), "processing.summary.status")

    def validate_classification_contract(self, contract: Any) -> None:
        if not isinstance(contract, Mapping):
            raise ValueError("classification_contract_must_be_mapping")
        if contract.get("family") != FAMILY or contract.get("stage") != "classification" or contract.get("version") != CLASSIFICATION_VERSION:
            raise ValueError("incompatible_classification_contract")
        if contract.get("mode") not in {"bootstrap", "incremental", "recovery"}:
            raise ValueError("invalid_classification_mode")
        context, parameters, classified, quality = (contract.get(key) for key in ("context", "parameters", "classifications", "quality"))
        if not all(isinstance(value, Mapping) for value in (context, parameters, classified, quality)):
            raise ValueError("invalid_classification_structure")
        markets = classified.get("markets")
        if not isinstance(markets, Mapping) or set(markets) != set(MARKETS):
            raise ValueError("invalid_classification_markets")
        if not isinstance(classified.get("cross_market"), Mapping):
            raise ValueError("invalid_cross_market_classification")
        if quality.get("status") not in VALID_QUALITY_STATUS:
            raise ValueError("invalid_classification_quality")
        _timestamp(context.get("classification_timestamp"), "classification.context.classification_timestamp", nullable=False)
        for market in MARKETS:
            payload = markets[market]
            if not isinstance(payload, Mapping) or not isinstance(payload.get("timeframes"), Mapping) or set(payload["timeframes"]) != set(TIMEFRAMES):
                raise ValueError("invalid_classification_timeframes")
            if not isinstance(payload.get("window_summaries"), Mapping) or set(payload["window_summaries"]) != {"4h", "24h"}:
                raise ValueError("invalid_classification_summaries")

    def validate_bundle_consistency(self, processing: Mapping[str, Any], classification: Mapping[str, Any]) -> None:
        if processing["mode"] != classification["mode"]:
            raise ValueError("bundle_mode_mismatch")
        p_context, c_context = processing["context"], classification["context"]
        for key in ("base_asset", "pair_symbol", "data_mode", "is_demo", "reference_timestamp", "processing_timestamp"):
            if p_context.get(key) != c_context.get(key):
                raise ValueError(f"bundle_context_mismatch:{key}")
        if set(c_context.get("markets", ())) != set(MARKETS) or tuple(c_context.get("timeframes", ())) != TIMEFRAMES:
            raise ValueError("classification_context_scope_mismatch")

    def build_context(self, processing: Mapping[str, Any], classification: Mapping[str, Any]) -> dict[str, Any]:
        p, c = processing["context"], classification["context"]
        return {"base_asset": p.get("base_asset"), "pair_symbol": p.get("pair_symbol"), "markets": list(MARKETS),
            "timeframes": list(TIMEFRAMES), "data_mode": p.get("data_mode"), "is_demo": p.get("is_demo"),
            "reference_timestamp": p.get("reference_timestamp"), "processing_timestamp": p.get("processing_timestamp"),
            "classification_timestamp": c.get("classification_timestamp"), "data_as_of": p.get("reference_timestamp"),
            "presentation_default_market": self.selected_market, "presentation_default_timeframe": self.selected_timeframe,
            "display_point_limit": self.display_point_limit}

    def build_selectors(self, processing: Mapping[str, Any]) -> dict[str, Any]:
        return {"market": {"id": "market_selector", "selected": self.selected_market,
                "options": [{"id": item, "label": item.title()} for item in MARKETS],
                "status": "context_only", "visible": False},
            "timeframe": {"id": "timeframe_selector", "selected": self.selected_timeframe,
                "options": [{"id": item, "seconds": TIMEFRAME_SECONDS[item],
                    "status": processing["markets"][self.selected_market]["timeframes"][item]["status"]} for item in TIMEFRAMES]}}

    def _kpi(self, identifier: str, title: str, value: Any, unit: str, status: str, reason: Any, timestamp: Any,
             classification: Any, paths: Sequence[str], *, secondary: Mapping[str, Any] | None = None,
             window: str | None = "4h", format_hint: str = "number", market: str = "cross_market") -> dict[str, Any]:
        return {"kpi_id": identifier, "title": title, "status": status, "reason": _reason(status, reason), "value": value,
            "unit": unit, "secondary_values": copy.deepcopy(dict(secondary or {})), "timestamp": timestamp,
            "market": market, "window": window, "classification": _classification(classification),
            "format_hint": format_hint, "source_paths": list(paths)}

    def build_kpis(self, processing: Mapping[str, Any], classification: Mapping[str, Any]) -> dict[str, Any]:
        summary = processing["cross_market"]["window_summaries"]["4h"]
        atoms = classification["classifications"]["cross_market"]["window_summaries"]["4h"]["atoms"]
        base = "processing.cross_market.window_summaries.4h"
        cbase = "classification.classifications.cross_market.window_summaries.4h.atoms"
        ratio, ratio_status, ratio_reason = _metric(summary["buy_sell_ratio"], f"{base}.buy_sell_ratio")
        imbalance, imbalance_status, imbalance_reason = _metric(summary["order_flow_imbalance"], f"{base}.order_flow_imbalance")
        efficiency, efficiency_status, efficiency_reason = _metric(summary["flow_efficiency"], f"{base}.flow_efficiency")
        buy_share, _, _ = _metric(summary["buy_share"], f"{base}.buy_share")
        sell_share, _, _ = _metric(summary["sell_share"], f"{base}.sell_share")
        footprint = processing["cross_market"].get("footprint_summaries", {}).get("4h", {})
        footprint_status = _status(footprint.get("status", "unavailable"), "processing.cross_market.footprint.status")
        source_status = _status(summary.get("status"), f"{base}.status")
        timestamp = _timestamp(summary.get("last_timestamp"), f"{base}.last_timestamp")
        volume_ratio_source = processing["cross_market"]["volume_ratios"]["futures_vs_spot"]["4h"]
        volume_ratio_status = _status(volume_ratio_source.get("status", "unavailable"), "processing.cross_market.volume_ratio.status")
        volume_ratio = _number(volume_ratio_source.get("value"), "processing.cross_market.volume_ratio.value")
        volume_ratio_atom = classification["classifications"]["cross_market"]["volume_ratios"]["futures_vs_spot"]["4h"]
        spot_4h = processing["markets"]["spot"]["window_summaries"]["4h"]
        futures_4h = processing["markets"]["futures"]["window_summaries"]["4h"]
        persistence = summary.get("directional_persistence", {})
        persistence_status = _status(persistence.get("status", "unavailable"), "processing.cross_market.window_summaries.4h.directional_persistence.status")
        technical = processing.get("technical_analysis", {}).get("markets", {}).get(self.selected_market, {}).get("timeframes", {}).get(self.selected_timeframe, {}).get("indicators", {})
        dynamics = technical.get("cvd_slope_acceleration", {})
        delta_z = technical.get("delta_zscore", {})
        price_div = technical.get("price_cvd_divergence", {})
        slope_current = dynamics.get("current", {}) if isinstance(dynamics, Mapping) else {}
        delta_z_current = delta_z.get("current", {}) if isinstance(delta_z, Mapping) else {}
        price_div_current = price_div.get("current", {}) if isinstance(price_div, Mapping) else {}
        return {
            "delta_4h": self._kpi("delta_4h", "Delta 4H", _number(summary.get("volume_delta_usd"), f"{base}.volume_delta_usd"), "USD", source_status, summary.get("reason"), timestamp, atoms.get("delta_state"), [f"{base}.volume_delta_usd", f"{cbase}.delta_state"], format_hint="currency"),
            "buy_sell_ratio_4h": self._kpi("buy_sell_ratio_4h", "Buy/Sell", ratio, "ratio", ratio_status, ratio_reason, timestamp, atoms.get("buy_sell_pressure_state"), [f"{base}.buy_sell_ratio.value", f"{base}.buy_share.value", f"{base}.sell_share.value", f"{cbase}.buy_sell_pressure_state"], secondary={"buy_share": {"value": buy_share, "unit": "decimal"}, "sell_share": {"value": sell_share, "unit": "decimal"}}),
            "futures_vs_spot_volume_ratio_4h": {**self._kpi("futures_vs_spot_volume_ratio_4h", "Futures vs Spot Volume Ratio", volume_ratio, "ratio", volume_ratio_status, volume_ratio_source.get("reason"), volume_ratio_source.get("timestamp"), volume_ratio_atom, ["processing.cross_market.volume_ratios.futures_vs_spot.4h"], secondary={"futures_volume_usd": {"value": volume_ratio_source.get("futures_volume_usd"), "unit": "USD"}, "spot_volume_usd": {"value": volume_ratio_source.get("spot_volume_usd"), "unit": "USD"}}), "provider_group": "API Market", "recalculate_in_hmi": False},
            "flow_efficiency_4h": self._kpi("flow_efficiency_4h", "Flow Efficiency", efficiency, "decimal", efficiency_status, efficiency_reason, timestamp, atoms.get("flow_efficiency_state"), [f"{base}.flow_efficiency.value", f"{cbase}.flow_efficiency_state"]),
            "vwap_4h": self._kpi("vwap_4h", "VWAP 4H", _number(footprint.get("vwap_usd"), "processing.cross_market.footprint.vwap"), "USD", footprint_status, footprint.get("reason"), timestamp, None, ["processing.cross_market.footprint_summaries.4h.vwap_usd"], format_hint="currency"),
            "order_flow_imbalance_4h": self._kpi("order_flow_imbalance_4h", "Order Flow Imbalance", imbalance, "decimal", imbalance_status, imbalance_reason, timestamp, atoms.get("order_flow_state"), [f"{base}.order_flow_imbalance.value", f"{cbase}.order_flow_state"]),
            "spot_delta_4h": self._kpi("spot_delta_4h", "Spot Delta 4H", _number(spot_4h.get("volume_delta_usd"), "processing.markets.spot.window_summaries.4h.volume_delta_usd"), "USD", _status(spot_4h.get("status"), "processing.markets.spot.window_summaries.4h.status"), spot_4h.get("reason"), spot_4h.get("last_timestamp"), None, ["processing.markets.spot.window_summaries.4h.volume_delta_usd"], format_hint="currency", market="spot"),
            "futures_delta_4h": self._kpi("futures_delta_4h", "Futures Delta 4H", _number(futures_4h.get("volume_delta_usd"), "processing.markets.futures.window_summaries.4h.volume_delta_usd"), "USD", _status(futures_4h.get("status"), "processing.markets.futures.window_summaries.4h.status"), futures_4h.get("reason"), futures_4h.get("last_timestamp"), None, ["processing.markets.futures.window_summaries.4h.volume_delta_usd"], format_hint="currency", market="futures"),
            "flow_persistence_4h": self._kpi("flow_persistence_4h", "Flow Persistence 4H", _number(persistence.get("value"), "processing.cross_market.window_summaries.4h.directional_persistence.value"), "decimal", persistence_status, persistence.get("reason"), timestamp, None, ["processing.cross_market.window_summaries.4h.directional_persistence"]),
            "delta_zscore": self._kpi("delta_zscore", f"Delta Z-Score {self.selected_timeframe.upper()}", _number(delta_z_current.get("zscore"), "processing.technical_analysis.delta_zscore.current.zscore"), "decimal", _status(delta_z.get("status", "unavailable"), "processing.technical_analysis.delta_zscore.status"), None, timestamp, None, [f"processing.technical_analysis.markets.{self.selected_market}.timeframes.{self.selected_timeframe}.indicators.delta_zscore"], market=self.selected_market, window=self.selected_timeframe),
            "cvd_slope": self._kpi("cvd_slope", f"CVD Slope {self.selected_timeframe.upper()}", _number(slope_current.get("slope"), "processing.technical_analysis.cvd_slope.current.slope"), "score", _status(dynamics.get("status", "unavailable"), "processing.technical_analysis.cvd_slope.status"), None, timestamp, None, [f"processing.technical_analysis.markets.{self.selected_market}.timeframes.{self.selected_timeframe}.indicators.cvd_slope_acceleration"], secondary={"acceleration": {"value": slope_current.get("acceleration"), "unit": "score"}}, market=self.selected_market, window=self.selected_timeframe),
            "price_cvd_divergence": self._kpi("price_cvd_divergence", f"Price/CVD Divergence {self.selected_timeframe.upper()}", _number(price_div_current.get("divergence"), "processing.technical_analysis.price_cvd_divergence.current.divergence"), "score", _status(price_div.get("status", "unavailable"), "processing.technical_analysis.price_cvd_divergence.status"), None, timestamp, None, [f"processing.technical_analysis.markets.{self.selected_market}.timeframes.{self.selected_timeframe}.indicators.price_cvd_divergence"], market=self.selected_market, window=self.selected_timeframe),
        }

    def _visual_status(self, source: Mapping[str, Any], count: int) -> tuple[str, str | None]:
        status = _status(source.get("status"), "processing.timeframe.status")
        reason = source.get("reason")
        if count == 0:
            status = "invalid" if status == "invalid" else "unavailable"
            reason = reason or "no_visual_records"
        elif count < self.display_point_limit and status != "invalid":
            status = "partial"
            reason = "insufficient_visual_history" if not reason else f"{reason};insufficient_visual_history"
        return status, _reason(status, reason)

    def build_cvd_chart(self, processing: Mapping[str, Any], market: str) -> dict[str, Any]:
        series = {}
        for timeframe in TIMEFRAMES:
            source = processing["markets"][market]["timeframes"][timeframe]
            records = source["records"]
            full_points = [{"timestamp": row["timestamp"], "open": _number(row.get("cvd_ohlc_usd", {}).get("open"), "cvd.open"),
                "high": _number(row.get("cvd_ohlc_usd", {}).get("high"), "cvd.high"), "low": _number(row.get("cvd_ohlc_usd", {}).get("low"), "cvd.low"),
                "close": _number(row.get("cvd_ohlc_usd", {}).get("close"), "cvd.close"), "is_partial": row.get("is_partial"),
                "continuity_status": row.get("continuity_status")} for row in records[-CALCULATION_HISTORY_LIMIT:]]
            points = full_points[-self.display_point_limit:]
            status, reason = self._visual_status(source, len(points))
            series[timeframe] = {"timeframe": timeframe, "seconds": TIMEFRAME_SECONDS[timeframe], "status": status, "reason": reason,
                "records_available": len(records), "records_returned": len(points), "history_truncated": len(records) > self.display_point_limit,
                "source_path": f"processing.markets.{market}.timeframes.{timeframe}.records",
                "representation": "candlestick", "candle_fields": ["timestamp", "open", "high", "low", "close"],
                "candle_count": len(points), "candles": points,
                "calculation_history": {"record_count": len(full_points), "candles": full_points,
                    "recalculate_in_hmi": False, "fabricated_records": 0}}
        selected = series[self.selected_timeframe]["candles"]
        chart_status = _combine([payload["status"] for payload in series.values()])
        return {"chart_id": f"cvd_{market}", "title": f"CVD {market.title()}", "subtitle": "Cumulative Volume Delta",
            "chart_type": "candlestick", "preferred_representation": "candlestick", "ohlc_available": True,
            "native_ohlc": False, "construction": "derived_from_interval_volume_delta_path", "unit": "USD",
            "status": chart_status, "reason": _reason(chart_status, "chart_series_incomplete"),
            "selected_timeframe": self.selected_timeframe, "current": copy.deepcopy(selected[-1]) if selected else None,
            "series_by_timeframe": series, "source_paths": [f"processing.markets.{market}.timeframes"]}

    def build_delta_chart(self, processing: Mapping[str, Any], market: str) -> dict[str, Any]:
        series = {}
        for timeframe in TIMEFRAMES:
            source = processing["markets"][market]["timeframes"][timeframe]
            records = source["records"]
            full_points = []
            for row in records[-CALCULATION_HISTORY_LIMIT:]:
                buy = float(_number(row.get("taker_buy_volume_usd"), "flow.buy", nullable=False))
                sell = float(_number(row.get("taker_sell_volume_usd"), "flow.sell", nullable=False))
                total = buy + sell
                buy_percent = (100.0 * buy / total) if total > 0 else None
                sell_percent = (100.0 * sell / total) if total > 0 else None
                net_flow_percent = (buy_percent - sell_percent) if buy_percent is not None else None
                full_points.append({
                    "timestamp": row["timestamp"],
                    "delta_buy_sell_usd": _number(row.get("cvd_ohlc_usd", {}).get("close"), "delta.close") - _number(row.get("cvd_ohlc_usd", {}).get("open"), "delta.open"),
                    "delta_ma_21": _number(row.get("delta_ma_21_usd"), "delta.ma"),
                    "direction": "buy" if row.get("volume_delta_usd", 0) > 0 else "sell" if row.get("volume_delta_usd", 0) < 0 else "neutral",
                    "taker_buy_volume_usd": buy,
                    "taker_sell_volume_usd": sell,
                    "buy_percent": buy_percent,
                    "sell_percent": sell_percent,
                    "net_flow_percent": net_flow_percent,
                })
            points = full_points[-self.display_point_limit:]
            status, reason = self._visual_status(source, len(points))
            series[timeframe] = {"timeframe": timeframe, "seconds": TIMEFRAME_SECONDS[timeframe], "status": status, "reason": reason,
                "records_available": len(records), "records_returned": len(points), "history_truncated": len(records) > self.display_point_limit,
                "bars": points, "calculation": "cvd_close - cvd_open",
                "source_paths": [f"processing.markets.{market}.timeframes.{timeframe}.records"],
                "calculation_history": {"record_count": len(full_points),
                    "timestamps": [row["timestamp"] for row in full_points],
                    "delta_buy_sell_usd": [row["delta_buy_sell_usd"] for row in full_points],
                    "delta_ma_21": [row["delta_ma_21"] for row in full_points],
                    "buy_percent": [row["buy_percent"] for row in full_points],
                    "sell_percent": [row["sell_percent"] for row in full_points],
                    "net_flow_percent": [row["net_flow_percent"] for row in full_points],
                    "recalculate_in_hmi": False, "construction": "taker_buy_sell_volume_normalized_to_100_percent"}}
        selected = series[self.selected_timeframe]["bars"]
        period = processing["parameters"].get("delta_ma_period", processing["parameters"].get("delta_ma", {}).get("period", 21))
        chart_status = _combine([payload["status"] for payload in series.values()])
        display_name = "Spot Flow" if market == "spot" else "Futures Flow"
        return {"chart_id": f"delta_buy_sell_{market}", "title": display_name, "subtitle": "Aggressive buy/sell flow normalized to 100%", "chart_type": "buy_sell_flow_percent",
            "presentation": {"bar_field": "net_flow_percent", "buy_field": "buy_percent", "sell_field": "sell_percent", "moving_average_period": period},
            "unit": "USD", "market": market, "status": chart_status,
            "reason": _reason(chart_status, "chart_series_incomplete"), "selected_timeframe": self.selected_timeframe,
            "current": copy.deepcopy(selected[-1]) if selected else None, "series_by_timeframe": series,
            "source_paths": [f"processing.markets.{market}.timeframes", "processing.parameters.delta_ma_period"]}

    def build_charts(self, processing: Mapping[str, Any]) -> dict[str, Any]:
        return {"cvd_spot": self.build_cvd_chart(processing, "spot"), "cvd_futures": self.build_cvd_chart(processing, "futures"),
            "delta_buy_sell_spot": self.build_delta_chart(processing, "spot"),
            "delta_buy_sell_futures": self.build_delta_chart(processing, "futures")}

    def _side_widget(self, processing: Mapping[str, Any], window: str) -> dict[str, Any]:
        market = self.selected_market
        source = processing["markets"][market]["window_summaries"][window]
        base = f"processing.markets.{market}.window_summaries.{window}"
        status = _status(source.get("status"), f"{base}.status")
        buy_share, _, _ = _metric(source["buy_share"], f"{base}.buy_share")
        sell_share, _, _ = _metric(source["sell_share"], f"{base}.sell_share")
        return {"widget_id": f"volume_by_side_{window}", "title": f"Volume by Trade Side {window.upper()}", "widget_type": "donut",
            "status": status, "reason": _reason(status, source.get("reason")), "market": market, "window": window,
            "taker_buy_volume_usd": _number(source.get("taker_buy_volume_usd"), f"{base}.buy"),
            "taker_sell_volume_usd": _number(source.get("taker_sell_volume_usd"), f"{base}.sell"),
            "buy_share": buy_share, "sell_share": sell_share, "source_paths": [base]}

    def build_widgets(self, processing: Mapping[str, Any], classification: Mapping[str, Any]) -> dict[str, Any]:
        market = self.selected_market
        summary = processing["markets"][market]["window_summaries"]["4h"]
        metric, status, reason = _metric(summary["order_flow_imbalance"], "processing.order_flow_imbalance")
        atom = classification["classifications"]["markets"][market]["window_summaries"]["4h"]["atoms"]["order_flow_state"]
        agreement = classification["confirmations"]["market_agreement_4h"]
        temporal = classification["confirmations"]["temporal_alignment"][market]
        return {"volume_by_side_4h": self._side_widget(processing, "4h"), "volume_by_side_24h": self._side_widget(processing, "24h"),
            "order_flow_imbalance_4h": {"widget_id": "order_flow_imbalance_4h", "title": "Order Flow Imbalance", "widget_type": "gauge",
                "minimum": -1, "maximum": 1, "value": metric, "state": atom.get("state"), "direction": atom.get("direction"),
                "status": status, "reason": reason, "source_paths": [f"processing.markets.{market}.window_summaries.4h.order_flow_imbalance.value", f"classification.classifications.markets.{market}.window_summaries.4h.atoms.order_flow_state"]},
            "market_agreement_4h": {"widget_id": "market_agreement_4h", "title": "Market Agreement 4H", "widget_type": "state",
                **_copy_classification(agreement), "source_path": "classification.confirmations.market_agreement_4h"},
            "temporal_alignment": {"widget_id": "temporal_alignment", "title": "Temporal Alignment", "widget_type": "state",
                **_copy_classification(temporal), "source_path": f"classification.confirmations.temporal_alignment.{market}"}}

    def build_tables(self, processing: Mapping[str, Any], classification: Mapping[str, Any]) -> dict[str, Any]:
        classified = classification["classifications"]["markets"]
        overview = []
        for market in MARKETS:
            for timeframe in TIMEFRAMES:
                source = processing["markets"][market]["timeframes"][timeframe]
                current = source.get("current")
                atoms = classified[market]["timeframes"][timeframe].get("atoms", {})
                row = {"market": market, "timeframe": timeframe, "timestamp": None, "volume_delta_usd": None,
                    "buy_sell_ratio": None, "order_flow_imbalance": None, "flow_efficiency": None, "cvd_close_usd": None,
                    "delta_state": None, "buy_sell_pressure_state": None, "order_flow_state": None, "cvd_direction_state": None,
                    "flow_efficiency_state": None, "continuity_state": None, "coverage_state": None,
                    "status": source["status"], "reason": _reason(source["status"], source.get("reason")),
                    "source_paths": [f"processing.markets.{market}.timeframes.{timeframe}.current", f"classification.classifications.markets.{market}.timeframes.{timeframe}.atoms"]}
                if isinstance(current, Mapping):
                    row.update({"timestamp": current.get("timestamp"), "volume_delta_usd": copy.deepcopy(current.get("volume_delta_usd")),
                        "buy_sell_ratio": copy.deepcopy(current.get("buy_sell_ratio", {}).get("value")), "order_flow_imbalance": copy.deepcopy(current.get("order_flow_imbalance", {}).get("value")),
                        "flow_efficiency": copy.deepcopy(current.get("flow_efficiency", {}).get("value")), "cvd_close_usd": copy.deepcopy(current.get("cvd_ohlc_usd", {}).get("close"))})
                    for name in ("delta_state", "buy_sell_pressure_state", "order_flow_state", "cvd_direction_state", "flow_efficiency_state", "continuity_state", "coverage_state"):
                        row[name] = copy.deepcopy(atoms.get(name, {}).get("state"))
                else:
                    row["status"] = "invalid" if source["status"] == "invalid" else "unavailable"
                    row["reason"] = source.get("reason") or "current_record_unavailable"
                overview.append(row)
        comparisons = []
        for market in MARKETS:
            for window in ("4h", "24h"):
                source = processing["markets"][market]["window_summaries"][window]
                comparisons.append({"market": market, "window": window, "first_timestamp": copy.deepcopy(source.get("first_timestamp")),
                    "last_timestamp": copy.deepcopy(source.get("last_timestamp")), "volume_delta_usd": copy.deepcopy(source.get("volume_delta_usd")),
                    "buy_sell_ratio": copy.deepcopy(source.get("buy_sell_ratio", {}).get("value")), "buy_share": copy.deepcopy(source.get("buy_share", {}).get("value")),
                    "sell_share": copy.deepcopy(source.get("sell_share", {}).get("value")), "order_flow_imbalance": copy.deepcopy(source.get("order_flow_imbalance", {}).get("value")),
                    "flow_efficiency": copy.deepcopy(source.get("flow_efficiency", {}).get("value")), "coverage_complete": copy.deepcopy(source.get("coverage_complete")),
                    "status": source["status"], "reason": _reason(source["status"], source.get("reason")),
                    "atoms": _copy_classification(classified[market]["window_summaries"][window].get("atoms", {})),
                    "source_paths": [f"processing.markets.{market}.window_summaries.{window}", f"classification.classifications.markets.{market}.window_summaries.{window}.atoms"]})
        overview_status = _combine([row["status"] for row in overview])
        comparison_status = _combine([row["status"] for row in comparisons])
        return {"market_timeframe_overview": {"table_id": "market_timeframe_overview", "title": "Market Timeframe Overview", "status": overview_status, "reason": _reason(overview_status, "market_timeframe_rows_incomplete"), "rows": overview, "source_paths": ["processing.markets", "classification.classifications.markets"]},
            "window_summary_comparison": {"table_id": "window_summary_comparison", "title": "Window Summary Comparison", "status": comparison_status, "reason": _reason(comparison_status, "window_summary_rows_incomplete"), "rows": comparisons, "source_paths": ["processing.markets", "classification.classifications.markets"]}}

    def build_drilldowns(self, processing: Mapping[str, Any], classification: Mapping[str, Any]) -> dict[str, Any]:
        market, timeframe = self.selected_market, self.selected_timeframe
        current = processing["markets"][market]["timeframes"][timeframe].get("current")
        atoms = classification["classifications"]["markets"][market]["timeframes"][timeframe].get("atoms", {})
        footprint_rows = []
        for item in MARKETS:
            source = processing["markets"][item].get("footprint_summaries", {}).get("4h", {})
            footprint_rows.append({"market": item, **{key: copy.deepcopy(source.get(key)) for key in ("vwap_usd", "base_volume", "quote_volume", "records_used", "levels_used", "calculation_basis", "aggregation_scope", "status", "reason")},
                "source_path": f"processing.markets.{item}.footprint_summaries.4h"})
        return {"current_market_detail": {"drilldown_id": "current_market_detail", "market": market, "timeframe": timeframe,
                "current": copy.deepcopy(current), "atoms": _copy_classification(atoms), "source_paths": [f"processing.markets.{market}.timeframes.{timeframe}.current", f"classification.classifications.markets.{market}.timeframes.{timeframe}.atoms"]},
            "market_agreement_detail": {"drilldown_id": "market_agreement_detail", "value": _copy_classification(classification["confirmations"]["market_agreement_4h"]), "source_path": "classification.confirmations.market_agreement_4h"},
            "temporal_alignment_detail": {"drilldown_id": "temporal_alignment_detail", "value": _copy_classification(classification["confirmations"]["temporal_alignment"]), "source_path": "classification.confirmations.temporal_alignment"},
            "footprint_vwap_scope": {"drilldown_id": "footprint_vwap_scope", "rows": footprint_rows, "source_paths": [f"processing.markets.{item}.footprint_summaries.4h" for item in MARKETS]},
            "classification_snapshots": {"drilldown_id": "classification_snapshots", "value": _copy_classification(classification.get("snapshots", {})), "source_path": "classification.snapshots"}}

    def build_events(self, classification: Mapping[str, Any]) -> dict[str, Any]:
        items = _copy_classification(classification.get("interpreted_events", []))
        if not _sequence(items):
            raise ValueError("invalid_interpreted_events")
        statuses = []
        reasons = []
        for item in items:
            if not isinstance(item, Mapping) or not isinstance(item.get("availability"), Mapping):
                raise ValueError("invalid_interpreted_event")
            item_status = _status(item["availability"].get("status"), "classification.interpreted_events.availability")
            statuses.append(item_status)
            if item_status != "available":
                reasons.append(_reason(item_status, item["availability"].get("reason")))
        status = _combine(statuses) if statuses else "available"
        reason = None if status == "available" else ";".join(sorted(set(reasons)))
        return {"id": "recent_events", "status": status, "reason": reason, "items": items,
            "source_path": "classification.interpreted_events"}

    def _inventory(self, selectors: Mapping[str, Any], kpis: Mapping[str, Any], charts: Mapping[str, Any], widgets: Mapping[str, Any],
                   tables: Mapping[str, Any], drilldowns: Mapping[str, Any], events: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        required_objects = {"selectors.market": selectors["market"], "selectors.timeframe": selectors["timeframe"],
            **{f"kpis.{key}": kpis[key] for key in ("delta_4h", "buy_sell_ratio_4h", "futures_vs_spot_volume_ratio_4h", "flow_efficiency_4h", "order_flow_imbalance_4h")},
            **{f"charts.{key}": value for key, value in charts.items()},
            **{f"widgets.{key}": widgets[key] for key in ("volume_by_side_4h", "volume_by_side_24h", "order_flow_imbalance_4h")},
            **{f"tables.{key}": value for key, value in tables.items()}}
        optional_objects = {
            **{f"kpis.{key}": kpis[key] for key in ("vwap_4h",)},
            **{f"widgets.{key}": widgets[key] for key in ("market_agreement_4h", "temporal_alignment")},
            **{f"drilldowns.{key}": value for key, value in drilldowns.items()}, "events.recent_events": events}
        def entry(obj: Mapping[str, Any], name: str) -> dict[str, Any]:
            status = obj.get("status", obj.get("availability", {}).get("status", "available"))
            if status not in VALID_SOURCE_STATUS:
                status = "available"
            paths = obj.get("source_paths", [obj["source_path"]] if "source_path" in obj else [])
            return _availability(status, obj.get("reason"), paths or ["processing.markets"])
        return ({key: entry(value, key) for key, value in required_objects.items()},
                {key: entry(value, key) for key, value in optional_objects.items()})

    def evaluate_availability(self, processing: Mapping[str, Any], classification: Mapping[str, Any], required: Mapping[str, Any], optional: Mapping[str, Any]) -> dict[str, Any]:
        markets = {}
        for market in MARKETS:
            statuses = [processing["markets"][market]["timeframes"][timeframe]["status"] for timeframe in TIMEFRAMES]
            status = _combine(statuses)
            markets[market] = _availability(status, None if status == "available" else "market_data_incomplete", [f"processing.markets.{market}"])
        timeframes = {}
        for timeframe in TIMEFRAMES:
            statuses = [processing["markets"][market]["timeframes"][timeframe]["status"] for market in MARKETS]
            status = _combine(statuses)
            timeframes[timeframe] = _availability(status, None if status == "available" else "timeframe_data_incomplete", [f"processing.markets.{market}.timeframes.{timeframe}" for market in MARKETS])
        return {"required": copy.deepcopy(required), "optional": copy.deepcopy(optional), "markets": markets, "timeframes": timeframes,
            "source_contracts": {"processing": _availability("available" if processing["quality"]["status"] == "ok" else processing["quality"]["status"], None if processing["quality"]["status"] == "ok" else "source_quality_incomplete", ["processing.quality"]),
                "classification": _availability("available" if classification["quality"]["status"] == "ok" else classification["quality"]["status"], None if classification["quality"]["status"] == "ok" else "source_quality_incomplete", ["classification.quality"])}}

    def evaluate_quality(self, processing: Mapping[str, Any], classification: Mapping[str, Any], required: Mapping[str, Any], optional: Mapping[str, Any], charts: Mapping[str, Any]) -> dict[str, Any]:
        required_statuses = {key: value["status"] for key, value in required.items()}
        optional_statuses = {key: value["status"] for key, value in optional.items()}
        series = {}
        for chart_id, chart in charts.items():
            for timeframe, payload in chart["series_by_timeframe"].items():
                series[f"{chart_id}.{timeframe}"] = {key: copy.deepcopy(payload[key]) for key in ("records_available", "records_returned", "history_truncated", "status", "reason")}
                series[f"{chart_id}.{timeframe}"]["target_records"] = self.display_point_limit
        source_statuses = [processing["quality"]["status"], classification["quality"]["status"]]
        invalid = "invalid" in source_statuses or "invalid" in required_statuses.values()
        incomplete = any(value != "available" for value in required_statuses.values()) or any(item["records_returned"] < self.display_point_limit for item in series.values()) or "partial" in source_statuses
        status = "invalid" if invalid else ("partial" if incomplete else "ok")
        warnings = sorted({f"required:{key}:{value}" for key, value in required_statuses.items() if value != "available"} |
            {f"source_quality:{name}:{value}" for name, value in zip(("processing", "classification"), source_statuses) if value != "ok"} |
            {f"visual_density:{key}:{value['records_returned']}" for key, value in series.items() if value["records_returned"] < self.display_point_limit})
        errors = sorted(item for item in warnings if ":invalid" in item)
        return {"status": status, "contract_complete": True, "data_complete": all(value == "available" for value in required_statuses.values()),
            "source_quality": {"processing": processing["quality"]["status"], "classification": classification["quality"]["status"]},
            "builder_quality": {"status": status, "warnings": warnings, "errors": errors}, "required_statuses": required_statuses,
            "optional_statuses": optional_statuses, "visual_density": {"display_point_limit": self.display_point_limit, "series": series},
            "strict_json": True, "warnings": warnings, "errors": errors}

    def build_badges(self, context: Mapping[str, Any], quality: Mapping[str, Any]) -> list[dict[str, Any]]:
        badges = []
        if context["data_mode"] == "synthetic" and context["is_demo"] is True:
            badges.append({"id": "synthetic", "text": "SYNTHETIC", "status": "active"})
        badges.append({"id": "data_quality", "text": quality["status"].upper(), "status": quality["status"]})
        return badges

    def build_operational_status(self, context: Mapping[str, Any], quality: Mapping[str, Any]) -> dict[str, Any]:
        statuses = quality["required_statuses"]
        return {"state": {"ok": "operational", "partial": "degraded", "invalid": "blocked"}[quality["status"]],
            "quality_status": quality["status"], "data_mode": context["data_mode"], "is_demo": context["is_demo"],
            "data_as_of": context["data_as_of"], "selected_market": self.selected_market, "selected_timeframe": self.selected_timeframe,
            "required_components_available": sum(value == "available" for value in statuses.values()), "required_components_total": len(statuses),
            "warnings": copy.deepcopy(quality["warnings"]), "errors": copy.deepcopy(quality["errors"])}

    @staticmethod
    def _indicator_unit(indicator_id: str) -> str:
        return {
            "rsi": "percent", "stochastic": "percent", "williams_r": "percent",
            "tsi": "index", "cci": "index", "adx": "index", "macd": "value",
            "atr": "value", "wasserstein_distance": "distance",
            "bollinger_band_width": "ratio",
        }.get(indicator_id, "value")

    @staticmethod
    def _summary(indicator_id: str, package: Mapping[str, Any]) -> dict[str, Any]:
        series = package.get("series", {}) if isinstance(package, Mapping) else {}
        current = package.get("current", {}) if isinstance(package, Mapping) else {}
        primary_by_indicator = {
            "macd": "macd", "rsi": "rsi", "tsi": "tsi", "stochastic": "k",
            "williams_r": "williams_r", "cci": "cci", "adx": "adx", "atr": "atr",
            "wasserstein_distance": "wasserstein_distance", "bollinger_band_width": "bollinger_band_width",
        }
        primary = primary_by_indicator[indicator_id]
        value = current.get(primary)
        if value is None and isinstance(series, Mapping):
            values = series.get(primary, [])
            if isinstance(values, Sequence):
                value = next((item for item in reversed(values) if isinstance(item, (int, float)) and not isinstance(item, bool)), None)
        secondary = {key: val for key, val in current.items() if key != primary and isinstance(val, (int, float)) and not isinstance(val, bool)} if isinstance(current, Mapping) else {}
        display = "—" if value is None else (f"{value:.4f}" if abs(float(value)) < 1000 else f"{value:.2f}")
        return {
            "indicator_id": indicator_id,
            "label": indicator_id.replace("_", " ").upper(),
            "section": "volatility" if indicator_id in {"atr", "bollinger_band_width"} else ("distribution" if indicator_id == "wasserstein_distance" else "momentum"),
            "status": "available" if value is not None else "unavailable",
            "value": value,
            "display_value": display,
            "secondary": secondary,
            "signal": "NEUTRAL" if value is not None else "UNAVAILABLE",
            "signal_color": "#ffab00" if value is not None else "#8998a5",
            "strength": 1 if value is not None else 0,
            "classification_basis": "processing_precomputed_summary",
            "recalculate_in_hmi": False,
        }

    def build_technical_analysis(self, processing: Mapping[str, Any], classification: Mapping[str, Any]) -> dict[str, Any]:
        """Publish the native CVD analysis already computed by Processing.

        Screen A exposes only the six approved moving averages. Screen B owns
        six native order-flow analyses.  HMI never recalculates either package.
        """
        source_ta = processing.get("technical_analysis", {})
        context = processing.get("context", {})
        is_demo = bool(context.get("is_demo", False))
        data_mode = "synthetic_emulator" if is_demo else "runtime_processing"
        classification_basis = "processing_precomputed_native"
        selector_contract = {
            "trend": ["ema_9", "ema_21", "sma_20", "sma_50", "wma_20", "wma_50"],
            "derived_analysis": ["cvd_slope_acceleration", "delta_zscore", "buy_sell_imbalance"],
            "momentum": ["price_cvd_divergence", "spot_futures_divergence"],
            "volatility": ["wasserstein_distance"],
        }
        indicator_order = (
            "cvd_slope_acceleration", "delta_zscore", "buy_sell_imbalance",
            "price_cvd_divergence", "spot_futures_divergence", "wasserstein_distance",
        )
        parameter_defaults = {
            "cvd_slope_acceleration": {"slope": "first_difference_then_rolling_zscore", "zscore_window": 30,
                                       "acceleration": "first_difference_of_slope_zscore"},
            "delta_zscore": {"source": "delta_buy_sell_usd", "window": 30},
            "buy_sell_imbalance": {"source": "normalized_order_flow_imbalance", "range": [-1, 1]},
            "price_cvd_divergence": {"price_source": "prices.processing.ohlcv.close", "cvd_source": "cvd.close",
                                     "return_zscore_window": 30, "definition": "z(price_return)-z(cvd_return)"},
            "spot_futures_divergence": {"definition": "z(spot_cvd_return)-z(futures_cvd_return)", "return_zscore_window": 30},
            "wasserstein_distance": {"recent_window_differences": 20, "reference_window_differences": 100,
                                     "input": "close first differences"},
        }
        threshold_defaults = {
            "cvd_slope_acceleration": [{"value": 0.0, "role": "neutral"}],
            "delta_zscore": [{"value": 2.0, "role": "upper_extreme"}, {"value": 0.0, "role": "neutral"},
                             {"value": -2.0, "role": "lower_extreme"}],
            "buy_sell_imbalance": [{"value": 0.0, "role": "neutral"}],
            "price_cvd_divergence": [{"value": 1.0, "role": "positive_divergence"}, {"value": 0.0, "role": "neutral"},
                                     {"value": -1.0, "role": "negative_divergence"}],
            "spot_futures_divergence": [{"value": 1.0, "role": "positive_divergence"}, {"value": 0.0, "role": "neutral"},
                                        {"value": -1.0, "role": "negative_divergence"}],
            "wasserstein_distance": [],
        }
        unit_defaults = {
            "cvd_slope_acceleration": "zscore", "delta_zscore": "zscore",
            "buy_sell_imbalance": "normalized_ratio", "price_cvd_divergence": "standardized_divergence",
            "spot_futures_divergence": "standardized_divergence", "wasserstein_distance": "distance",
        }
        label_defaults = {
            "cvd_slope_acceleration": ("CVD Slope / Acceleration", "flow"),
            "delta_zscore": ("Delta Z-Score", "flow"),
            "buy_sell_imbalance": ("Buy/Sell Imbalance", "flow"),
            "price_cvd_divergence": ("Price ↔ CVD Divergence", "divergence"),
            "spot_futures_divergence": ("Spot ↔ Futures CVD Divergence", "divergence"),
            "wasserstein_distance": ("Wasserstein Distance", "regime"),
        }

        def summary(indicator_id: str, package: Mapping[str, Any]) -> dict[str, Any]:
            current = package.get("current", {}) if isinstance(package.get("current"), Mapping) else {}
            value = next((v for v in current.values() if isinstance(v, (int, float)) and not isinstance(v, bool)), None)
            source_summary = package.get("summary", {}) if isinstance(package.get("summary"), Mapping) else {}
            label, section = label_defaults[indicator_id]
            status = package.get("status", "unavailable")
            display = source_summary.get("display_value")
            if display is None and value is not None:
                display = f"{float(value):+.3f}" if indicator_id != "wasserstein_distance" else f"{float(value):.3f}"
            signal = source_summary.get("signal", "unavailable" if value is None else "neutral")
            signal_color = source_summary.get("signal_color", signal)
            strength = source_summary.get("strength", 0.0 if value is None else min(5.0, abs(float(value))))
            return {
                "indicator_id": indicator_id, "label": label, "section": section, "value": value,
                "display_value": display, "signal": signal, "signal_color": signal_color, "strength": strength,
                "status": status, "classification_basis": classification_basis, "recalculate_in_hmi": False,
            }

        markets: dict[str, Any] = {}
        for market in MARKETS:
            target_timeframes: dict[str, Any] = {}
            market_source = source_ta.get("markets", {}).get(market, {}) if isinstance(source_ta, Mapping) else {}
            for timeframe in TIMEFRAMES:
                source = market_source.get("timeframes", {}).get(timeframe, {}) if isinstance(market_source, Mapping) else {}
                all_timestamps = list(source.get("timestamps", []))[-CALCULATION_HISTORY_LIMIT:]
                tail_timestamps = all_timestamps[-self.display_point_limit:]
                history_records = min(int(source.get("calculation_history_records", len(all_timestamps)) or 0), CALCULATION_HISTORY_LIMIT)
                source_overlays = source.get("overlays", {}) if isinstance(source, Mapping) else {}
                moving = source_overlays.get("moving_averages", {}) if isinstance(source_overlays, Mapping) else {}
                moving_series = moving.get("series", {}) if isinstance(moving, Mapping) else {}
                approved_mas = ("ema_9", "ema_21", "sma_20", "sma_50", "wma_20", "wma_50")
                overlay = {
                    "series": {name: list(moving_series.get(name, []))[-self.display_point_limit:] for name in approved_mas},
                    "recalculate_in_hmi": False,
                    "status": moving.get("status", source.get("status", "unavailable")),
                    "series_ids": list(approved_mas),
                }
                source_indicators = source.get("indicators", {}) if isinstance(source, Mapping) else {}
                indicators: dict[str, Any] = {}
                for indicator_id in indicator_order:
                    package = source_indicators.get(indicator_id, {}) if isinstance(source_indicators, Mapping) else {}
                    raw_series = package.get("series", {}) if isinstance(package, Mapping) else {}
                    current = copy.deepcopy(package.get("current", {})) if isinstance(package, Mapping) else {}
                    payload = {
                        "status": package.get("status", source.get("status", "unavailable")),
                        "unit": unit_defaults[indicator_id],
                        "parameters": copy.deepcopy(parameter_defaults[indicator_id]),
                        "timestamps": tail_timestamps,
                        "series": {key: list(values)[-self.display_point_limit:] for key, values in raw_series.items()} if isinstance(raw_series, Mapping) else {},
                        "current": current,
                        "thresholds": copy.deepcopy(threshold_defaults[indicator_id]),
                        "recalculate_in_hmi": False,
                        "summary": summary(indicator_id, package if isinstance(package, Mapping) else {}),
                        "calculation_history_records": history_records,
                    }
                    if indicator_id != "wasserstein_distance":
                        payload.update({"data_mode": data_mode, "is_proxy": False})
                    indicators[indicator_id] = payload
                display_start = tail_timestamps[0] if tail_timestamps else None
                events = [
                    copy.deepcopy(event) for event in source.get("events", [])
                    if isinstance(event, Mapping)
                    and (display_start is None or int(event.get("timestamp", 0)) >= int(display_start))
                ] if isinstance(source, Mapping) else []
                events.sort(key=lambda event: (event.get("timestamp", 0), event.get("event_uid", "")))
                target_timeframes[timeframe] = {
                    "status": source.get("status", "unavailable"), "source_records": history_records,
                    "timestamps": tail_timestamps, "overlays": {"moving_averages": overlay},
                    "indicators": indicators, "events": events,
                    "calculation_history_records": history_records, "screen_b_data_mode": data_mode,
                    "screen_b_processing_contract": "native_cvd_orderflow_vr1",
                }
            markets[market] = {"source_chart_id": f"cvd_{market}", "title": f"CVD {market.title()}", "timeframes": target_timeframes}
        return {
            "analysis_id": "cvd_native_orderflow_analysis", "contract_version": "2.0.0",
            "source": "CVD OHLC + executed buy/sell flow + synchronized BTC price", "recalculate_in_hmi": False,
            "markets": markets, "selector_contract": selector_contract,
            "screen_b_native_orderflow_contract": {
                "status": "available", "data_mode": data_mode, "recalculate_in_hmi": False, "future_owner": "Processing",
                "panels": list(indicator_order), "screen_title": "ANÁLISIS CVD · ORDER FLOW",
                "panel_labels": {
                    "cvd_slope_acceleration": "CVD SLOPE / ACCELERATION", "delta_zscore": "DELTA Z-SCORE",
                    "buy_sell_imbalance": "BUY / SELL IMBALANCE", "price_cvd_divergence": "PRICE ↔ CVD DIVERGENCE",
                    "spot_futures_divergence": "SPOT ↔ FUTURES CVD DIVERGENCE", "wasserstein_distance": "WASSERSTEIN DISTANCE",
                },
            },
        }

    def run(self, bundle: Mapping[str, Any]) -> dict[str, Any]:
        self.validate_bundle(bundle)
        processing, classification = bundle["processing"], bundle["classification"]
        context = self.build_context(processing, classification)
        selectors = self.build_selectors(processing)
        kpis = self.build_kpis(processing, classification)
        charts = self.build_charts(processing)
        widgets = self.build_widgets(processing, classification)
        tables = self.build_tables(processing, classification)
        drilldowns = self.build_drilldowns(processing, classification)
        events = self.build_events(classification)
        technical_analysis = self.build_technical_analysis(processing, classification)
        filtered_events = [event for market in technical_analysis.get("markets", {}).values()
            for payload in market.get("timeframes", {}).values() for event in payload.get("events", [])]
        technical_events = {"by_id": {event["event_uid"]: copy.deepcopy(event) for event in filtered_events},
            "technical_cross_ids": [event["event_uid"] for event in filtered_events]}
        events.update(technical_events)
        required, optional = self._inventory(selectors, kpis, charts, widgets, tables, drilldowns, events)
        availability = self.evaluate_availability(processing, classification, required, optional)
        quality = self.evaluate_quality(processing, classification, required, optional, charts)
        output = {"schema": {"id": SCREEN_SCHEMA, "version": SCREEN_VERSION},
            "screen": {"id": SCREEN_ID, "family": FAMILY, "route": SCREEN_ROUTE, "title": SCREEN_TITLE, "subtitle": SCREEN_SUBTITLE},
            "stage": "screen_contract", "mode": processing["mode"], "context": context,
            "badges": self.build_badges(context, quality), "selectors": selectors,
            "operational_status": self.build_operational_status(context, quality), "kpis": kpis, "charts": charts,
            "widgets": widgets, "tables": tables, "drilldowns": drilldowns, "events": events,
            "technical_analysis": technical_analysis,
            "history_contract": {
                "calculation_records": min((payload["calculation_history_records"] for market in technical_analysis.get("markets", {}).values() for payload in market.get("timeframes", {}).values()), default=0),
                "minimum_warmup_records": 200,
                "maximum_standard_indicator_period": 200,
                "all_visible_moving_averages_warm": min((payload["calculation_history_records"] for market in technical_analysis.get("markets", {}).values() for payload in market.get("timeframes", {}).values()), default=0) >= 200,
                "technical_indicators_precomputed": True,
                "hmi_recalculation": False,
                "synthetic_fixture": False,
                "fixture_seed": None,
                "note": (
                    f"Up to {CALCULATION_HISTORY_LIMIT} calculation records are retained per Spot/Futures CVD timeframe; "
                    f"current minimum available history is {min((payload['calculation_history_records'] for market in technical_analysis.get('markets', {}).values() for payload in market.get('timeframes', {}).values()), default=0)} records. "
                    "CVD has no General market and HMI performs no indicator recalculation."
                ),
            },
            "availability": availability, "quality": quality}
        _validated_paths(output)
        aligned = align_cvd_contract_to_sp_v1_5(output)
        json.dumps(aligned, ensure_ascii=False, allow_nan=False)
        return aligned


def build_cvd_volume_orderflow_contract(bundle: Mapping[str, Any], *, selected_market: str = DEFAULT_MARKET,
                                        selected_timeframe: str = DEFAULT_TIMEFRAME,
                                        display_point_limit: int = DISPLAY_POINT_LIMIT) -> dict[str, Any]:
    return CvdVolumeOrderflowContractBuilder(selected_market=selected_market, selected_timeframe=selected_timeframe,
        display_point_limit=display_point_limit).run(bundle)

# --- Canonical Screen contract shaping ---
from copy import deepcopy

import json

from pathlib import Path

from typing import Any, Mapping

_screen_SCHEMA_VERSION = '1.5.0'

_screen_TEMPLATE_PATH = Path(__file__).with_name('screen_template.json')

_screen_MISSING = object()

def _screen_template() -> dict[str, Any]:
    return json.loads(_screen_TEMPLATE_PATH.read_text(encoding='utf-8'))

def _screen_is_scalar(value: Any) -> bool:
    return not isinstance(value, (dict, list))

def _screen_project(reference: Any, candidate: Any=_screen_MISSING) -> Any:
    """Project runtime data onto the exact frozen CVD Screen-SP shape.

    Static presentation policy comes from the SP.  Runtime values, arrays,
    statuses, timestamps and provenance are supplied by the ordinary builder.
    Extra runtime keys are deliberately discarded so the HMI contract remains
    structurally stable.
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
        identity_keys = ('metric_id', 'kpi_id', 'widget_id', 'chart_id', 'table_id', 'badge_id', 'id', 'role', 'family', 'group', 'indicator_id', 'event_type', 'event_group', 'market', 'timeframe', 'window')

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

def align_cvd_contract_to_sp_v1_5(candidate: Mapping[str, Any]) -> dict[str, Any]:
    reference = _screen_template()
    aligned = _screen_project(reference, dict(candidate))
    candidate_events = candidate.get('events', {}) if isinstance(candidate, Mapping) else {}
    if isinstance(aligned.get('events'), dict) and isinstance(candidate_events, Mapping):
        aligned['events']['by_id'] = _screen_project_dynamic_events(reference.get('events', {}), candidate_events)
        aligned['events']['technical_cross_ids'] = list(aligned['events']['by_id'])
    aligned['schema'] = {'id': 'trad_elatin.cvd_volume_orderflow.screen.v1', 'version': _screen_SCHEMA_VERSION}
    context = aligned.get('context', {})
    is_demo = bool(context.get('is_demo', False))
    context['synthetic_fixture'] = False
    context['fixture_as_of_timestamp'] = None
    context['fixture_as_of_iso'] = None
    context['realism_refactor_version'] = 'runtime_provider_v2'
    context['realism_note'] = ('runtime Emulator aggressor-flow state; no Screen fixture history is injected'
                               if is_demo else
                               'runtime provider data; CVD OHLC is constructed in Processing from interval aggressor-flow observations')
    aligned['context'] = context
    json.dumps(aligned, ensure_ascii=False, allow_nan=False)
    return aligned
