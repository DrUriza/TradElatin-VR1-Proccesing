"""Orchestration and contracts for CVD volume/order-flow Processing v0.1."""
from __future__ import annotations

import copy
import math
import time

import pandas as pd
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from .cvd_volume_orderflow_math import detect_cross_pairs
from .cvd_volume_orderflow_math import (
    difference,
    rolling_zscore,
    rolling_wasserstein,
    interpolated_cross,
    latest,
    finite,
)
from .cvd_volume_orderflow_math import ema, sma, wma


from .cvd_volume_orderflow_feature_builder import (
    BASE_TIMEFRAMES, CVD_VOLUME_ORDERFLOW_FAMILY, DELTA_MA_PERIOD, FLOW_EFFICIENCY_PERIOD, MARKETS, PROCESSING_STAGE,
    PROCESSING_VERSION, SOURCE_FACTOR, SOURCE_TIMEFRAME, TARGET_TIMEFRAMES, TIMEFRAME_SECONDS, CvdVolumeOrderflowFeatureBuilder,
    volume_features,
)


def _sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _clock_timestamp(clock: Callable[[], Any] | None) -> int:
    value = time.time() if clock is None else clock()
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("invalid_clock")
    return int(value)


def _iso_utc(value: int) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def _aligned_difference(
    own_timestamps: Sequence[int], own_values: Sequence[float | None],
    peer_timestamps: Sequence[int], peer_values: Sequence[float | None],
) -> list[float | None]:
    """Subtract peer values only where both market timestamps overlap."""
    peer_by_timestamp = dict(zip(peer_timestamps, peer_values, strict=True))
    return [
        None if own is None or peer_by_timestamp.get(timestamp) is None
        else float(own) - float(peer_by_timestamp[timestamp])
        for timestamp, own in zip(own_timestamps, own_values, strict=True)
    ]


def _price_records_with_spot_fallback(
    price_history: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    market: str,
    timeframe: str,
) -> Sequence[Mapping[str, Any]]:
    """Return a usable market price series, falling back to canonical Spot.

    The frozen Prices family exposes Spot OHLC as its canonical BTC price and
    keeps a Futures branch in the input contract for schema compatibility.  In
    emulator mode that Futures branch is intentionally empty.  CVD Futures is
    still a real, independent flow series, so its Price/CVD divergence must use
    the canonical Spot price rather than turning the whole indicator into
    ``null`` values.

    A future provider can supply native Futures OHLC: when it contains at least
    one timestamped close it remains preferred and no fallback occurs.
    """

    def records_for(candidate_market: str) -> Sequence[Mapping[str, Any]]:
        market_history = price_history.get(candidate_market, {})
        if not isinstance(market_history, Mapping):
            return ()
        records = market_history.get(timeframe, ())
        if not _sequence(records):
            return ()
        return records

    preferred = records_for(market)
    if any(
        isinstance(row, Mapping)
        and row.get("timestamp") is not None
        and isinstance(row.get("close"), (int, float))
        and not isinstance(row.get("close"), bool)
        and math.isfinite(float(row["close"]))
        for row in preferred
    ):
        return preferred
    return records_for("spot")


class CvdVolumeOrderflowProcessor:
    def __init__(self, *, feature_builder: CvdVolumeOrderflowFeatureBuilder | None = None,
                 clock: Callable[[], Any] | None = None) -> None:
        self.feature_builder = feature_builder or CvdVolumeOrderflowFeatureBuilder()
        self.clock           = clock

    def validate_input_contract(self, input_contract: Any) -> dict[str, Any]:
        if not isinstance(input_contract, Mapping):
            raise ValueError("input_contract_must_be_mapping")
        if input_contract.get("family") != CVD_VOLUME_ORDERFLOW_FAMILY:
            raise ValueError("incompatible_family")
        if input_contract.get("stage") != "input":
            raise ValueError("incompatible_stage")
        if input_contract.get("mode") not in {"bootstrap", "incremental", "recovery"}:
            raise ValueError("invalid_mode")
        context, markets = input_contract.get("context"), input_contract.get("markets")
        if not isinstance(context, Mapping) or not isinstance(markets, Mapping) or set(("spot", "futures")) - set(markets):
            raise ValueError("invalid_input_structure")
        reference = context.get("reference_timestamp")
        if type(reference) is not int or reference < 0:
            raise ValueError("invalid_reference_timestamp")
        normalized = {}
        for market in ("spot", "futures"):
            payload = markets.get(market)
            if not isinstance(payload, Mapping):
                raise ValueError("invalid_market_structure")
            timeframes = payload.get("cvd", {}).get("timeframes") if isinstance(payload.get("cvd"), Mapping) else None
            if not isinstance(timeframes, Mapping) or set(BASE_TIMEFRAMES) - set(timeframes):
                raise ValueError("missing_base_timeframes")
            normalized[market] = {}
            for timeframe in BASE_TIMEFRAMES:
                timeframe_payload = timeframes[timeframe]
                if not isinstance(timeframe_payload, Mapping) or not _sequence(timeframe_payload.get("records")):
                    raise ValueError("invalid_timeframe_payload")
                normalized[market][timeframe] = self.feature_builder.validate_base_records(timeframe_payload["records"])
                if any(row["timestamp"] > reference for row in normalized[market][timeframe]):
                    raise ValueError("timestamp_after_reference_timestamp")
        return normalized

    def build_context(self, input_contract: Mapping[str, Any], processing_timestamp: int) -> dict[str, Any]:
        context = input_contract["context"]
        required = ("base_asset", "pair_symbol", "data_mode", "is_demo", "reference_timestamp", "requested_at", "execution_timestamp")
        if any(key not in context for key in required):
            raise ValueError("incomplete_input_context")
        return {"base_asset": context["base_asset"], "pair_symbol": context["pair_symbol"], "markets": list(MARKETS),
            "base_timeframes": list(BASE_TIMEFRAMES), "available_timeframes": list(TARGET_TIMEFRAMES), "data_mode": context["data_mode"],
            "is_demo": context["is_demo"], "reference_timestamp": context["reference_timestamp"], "input_requested_at": context["requested_at"],
            "input_execution_timestamp": context["execution_timestamp"], "processing_timestamp": processing_timestamp,
            "processing_requested_at": _iso_utc(processing_timestamp)}

    def build_parameters(self) -> dict[str, Any]:
        return {"source_timeframes": copy.deepcopy(SOURCE_TIMEFRAME), "source_factors": copy.deepcopy(SOURCE_FACTOR),
            "delta_ma_period": DELTA_MA_PERIOD, "flow_efficiency_period": FLOW_EFFICIENCY_PERIOD,
            "delta_ma": {"method": "simple_moving_average", "period": DELTA_MA_PERIOD, "source_field": "volume_delta_usd"},
            "flow_efficiency": {"method": "net_delta_displacement_over_absolute_delta_path", "period": FLOW_EFFICIENCY_PERIOD, "unit": "decimal"},
            "cvd_anchor_method": "zero_before_first_available_record",
            "resampling_alignment": "utc_epoch", "recalculation_policy": "full_history_deterministic_rebuild",
            "cvd_ohlc": {"construction": "derived_from_interval_volume_delta_path", "native_ohlc": False},
            "provider_reference_used_in_calculation": False}

    @staticmethod
    def _percentile(values: Sequence[Any], current: Any) -> float | None:
        valid = [float(value) for value in values if isinstance(value, (int, float)) and math.isfinite(value)]
        if not valid or not isinstance(current, (int, float)) or not math.isfinite(current):
            return None
        return sum(value <= float(current) for value in valid) / len(valid) * 100.0

    @staticmethod
    def _wasserstein_first_differences(closes: Sequence[float], *, recent_window: int = 20, reference_window: int = 100) -> list[float | None]:
        """Rolling dimensionless 1-D Wasserstein distance on CVD first differences.

        The reference window immediately precedes the recent window.  The raw
        empirical transport distance is normalized by the reference population
        standard deviation so CVD level/scale changes do not dominate the state.
        """
        differences = [float(right) - float(left) for left, right in zip(closes, closes[1:])]
        output: list[float | None] = [None] * len(closes)

        def quantile(sorted_values: list[float], q: float) -> float:
            if len(sorted_values) == 1:
                return sorted_values[0]
            position = q * (len(sorted_values) - 1)
            lower = int(math.floor(position))
            upper = min(lower + 1, len(sorted_values) - 1)
            weight = position - lower
            return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight

        required = recent_window + reference_window
        for close_index in range(1, len(closes)):
            diff_end = close_index
            if diff_end < required:
                continue
            recent = sorted(differences[diff_end - recent_window:diff_end])
            reference = sorted(differences[diff_end - required:diff_end - recent_window])
            if not recent or not reference:
                continue
            samples = max(len(recent), len(reference))
            distance = sum(
                abs(quantile(recent, (idx + 0.5) / samples) - quantile(reference, (idx + 0.5) / samples))
                for idx in range(samples)
            ) / samples
            mean = sum(reference) / len(reference)
            variance = sum((value - mean) ** 2 for value in reference) / len(reference)
            scale = max(math.sqrt(variance), 1e-12)
            output[close_index] = distance / scale
        return output

    @staticmethod
    def _augment_cvd_indicators(package: dict[str, Any], candles: Sequence[Mapping[str, Any]], market: str, timeframe: str) -> dict[str, Any]:
        """Compatibility filter for the approved CVD moving-average surface."""
        return {"moving_averages": copy.deepcopy(package.get("moving_averages", {}))}

        # Historical implementation retained below as unreachable audit context.
        package = copy.deepcopy(package)
        package.pop("mfi", None)
        package.pop("fibonacci_levels", None)
        package["regression_channel"] = build_regression_channel_indicator(
            records=[dict(row) for row in candles], market_type=f"cvd_{market}", timeframe=timeframe,
            window=30, deviation_multiplier=2.0,
        )

        tsi_payload = package.get("tsi", {})
        tsi_values = tsi_payload.get("series", {}).get("tsi", []) if isinstance(tsi_payload, Mapping) else []
        if isinstance(tsi_values, Sequence) and not isinstance(tsi_values, (str, bytes, bytearray)):
            signal_series = ema(pd.Series(list(tsi_values), dtype="float64"), span=13).tolist()
            signal = [None if pd.isna(value) else float(value) for value in signal_series]
            tsi_payload.setdefault("series", {})["signal"] = signal
            tsi_payload.setdefault("current", {})["signal"] = next((value for value in reversed(signal) if value is not None), None)

        bollinger = package.get("bollinger_bands", {}).get("series", {})
        upper = list(bollinger.get("upper", []))
        middle = list(bollinger.get("middle", []))
        lower = list(bollinger.get("lower", []))
        closes = [float(row["close"]) for row in candles]
        widths: list[float | None] = []
        period = 20
        for index, (u, m, l) in enumerate(zip(upper, middle, lower)):
            if u is None or m is None or l is None or index + 1 < period:
                widths.append(None)
                continue
            window = closes[index + 1 - period:index + 1]
            mean_abs = sum(abs(value) for value in window) / period
            mean = sum(window) / period
            std = math.sqrt(sum((value - mean) ** 2 for value in window) / period)
            denominator = mean_abs + std
            widths.append(None if denominator <= 0 else (float(u) - float(l)) / denominator)
        timestamps = [int(row["timestamp"]) for row in candles]
        package["bollinger_band_width"] = {
            "indicator_id": "bollinger_band_width",
            "parameters": {"period": 20, "standard_deviations": 2.0, "normalization": "rolling_mean_abs_close_plus_std"},
            "timestamps": timestamps,
            "series": {"bollinger_band_width": widths},
            "current": {"bollinger_band_width": next((value for value in reversed(widths) if value is not None), None)},
            "warmup_records": period,
            "source": {"market_type": f"cvd_{market}", "timeframe": timeframe, "is_synthetic_source": False},
            "quality": {
                "status": "ok" if any(value is not None for value in widths) else "insufficient_data",
                "valid_points": sum(value is not None for value in widths),
                "null_points": sum(value is None for value in widths),
                "required_records": period,
                "available_records": len(candles),
                "warnings": [],
            },
            "calculation": {"module": __name__, "function": "rolling_bollinger_band_width",
                "parameters": {"period": 20, "standard_deviations": 2.0}, "records": len(candles)},
        }

        wasserstein = CvdVolumeOrderflowProcessor._wasserstein_first_differences(closes)
        package["wasserstein_distance"] = {
            "indicator_id": "wasserstein_distance",
            "parameters": {"input": "close first differences", "recent_window_differences": 20, "reference_window_differences": 100},
            "timestamps": timestamps,
            "series": {"wasserstein_distance": wasserstein},
            "current": {"wasserstein_distance": next((value for value in reversed(wasserstein) if value is not None), None)},
            "warmup_records": 121,
            "source": {"market_type": f"cvd_{market}", "timeframe": timeframe, "is_synthetic_source": False},
            "quality": {
                "status": "ok" if any(value is not None for value in wasserstein) else "insufficient_data",
                "valid_points": sum(value is not None for value in wasserstein),
                "null_points": sum(value is None for value in wasserstein),
                "required_records": 121,
                "available_records": len(candles),
                "warnings": [],
            },
            "calculation": {"module": __name__, "function": "rolling_wasserstein_first_differences",
                "parameters": {"recent_window_differences": 20, "reference_window_differences": 100}, "records": len(candles)},
        }
        return package

    @staticmethod
    def _native_indicator(indicator_id: str, timestamps: Sequence[int], series: Mapping[str, Sequence[Any]], *, section: str, label: str, unit: str = "score") -> dict[str, Any]:
        current = {name: latest(values) for name, values in series.items()}
        primary = next((value for value in current.values() if value is not None), None)
        if primary is None:
            signal, strength, status = "unavailable", 0.0, "unavailable"
        else:
            signal = "positive" if primary > 0.25 else ("negative" if primary < -0.25 else "neutral")
            strength, status = min(1.0, abs(float(primary)) / 2.0), "available"
        return {
            "indicator_id": indicator_id, "status": status, "timestamps": list(timestamps),
            "series": {name: list(values) for name, values in series.items()},
            "thresholds": [{"value": 0.0, "role": "neutral"}],
            "current": current,
            "summary": {"section": section, "label": label, "display_value": None if primary is None else f"{primary:.3f}",
                        "signal": signal,
                        "signal_color": {"positive": "#20d05c", "negative": "#ff3d55", "neutral": "#ffab00", "unavailable": "#59636b"}.get(signal, "#22c7e8"),
                        "strength": strength},
            "calculation_owner": "Processing", "recalculate_in_hmi": False, "unit": unit,
        }

    def build_technical_analysis(
        self, markets: Mapping[str, Any], *,
        price_history_by_market_timeframe: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]] | None = None,
    ) -> dict[str, Any]:
        """Build the frozen native CVD Screen A overlays and six Screen-B analyses."""
        price_history = price_history_by_market_timeframe or {}
        prepared: dict[str, dict[str, Any]] = {market: {} for market in MARKETS}

        # Precompute normalized Spot/Futures CVD changes used by divergence.
        cvd_change_z: dict[str, dict[str, list[float | None]]] = {market: {} for market in MARKETS}
        cvd_timestamps: dict[str, dict[str, list[int]]] = {market: {} for market in MARKETS}
        for market in MARKETS:
            for timeframe in TARGET_TIMEFRAMES:
                records = markets[market]["timeframes"][timeframe]["records"][-730:]
                closes = [row.get("cvd_ohlc_usd", {}).get("close") for row in records]
                cvd_change_z[market][timeframe] = rolling_zscore(difference(closes), 30, 10)
                cvd_timestamps[market][timeframe] = [int(row["timestamp"]) for row in records]

        for market in MARKETS:
            for timeframe in TARGET_TIMEFRAMES:
                source = markets[market]["timeframes"][timeframe]
                records = source["records"][-730:]
                candles = [{"timestamp": row["timestamp"], **copy.deepcopy(row["cvd_ohlc_usd"]),
                            "volume_usd": row.get("total_volume_usd", 0.0)} for row in records]
                timestamps = [int(row["timestamp"]) for row in records]
                closes = [row.get("cvd_ohlc_usd", {}).get("close") for row in records]
                deltas = [row.get("volume_delta_usd") for row in records]
                imbalances = [row.get("order_flow_imbalance", {}).get("value") for row in records]

                close_series = pd.Series(closes, dtype="float64")
                ma_series = {
                    "ema_9": ema(close_series, 9).where(lambda values: values.index >= 8).tolist(),
                    "ema_21": ema(close_series, 21).where(lambda values: values.index >= 20).tolist(),
                    "sma_20": sma(close_series, 20).tolist(),
                    "sma_50": sma(close_series, 50).tolist(),
                    "wma_20": wma(close_series, 20).tolist(),
                    "wma_50": wma(close_series, 50).tolist(),
                } if candles else {}
                ma_series = {
                    name: [None if pd.isna(value) else float(value) for value in values]
                    for name, values in ma_series.items()
                }
                cross_pairs = (("ema_9", "ema_21"), ("sma_20", "sma_50"), ("wma_20", "wma_50"))
                crosses = detect_cross_pairs(timestamps=timestamps, series=ma_series, pairs=cross_pairs) if candles else []
                index_by_timestamp = {ts: index for index, ts in enumerate(timestamps)}
                events: list[dict[str, Any]] = []
                for cross in crosses:
                    index = index_by_timestamp.get(int(cross["timestamp"]))
                    first_name, second_name = str(cross.get("first_series")), str(cross.get("second_series"))
                    if index is None:
                        continue
                    first_values, second_values = ma_series.get(first_name, []), ma_series.get(second_name, [])
                    first_value = first_values[index] if index < len(first_values) else None
                    second_value = second_values[index] if index < len(second_values) else None
                    signal = "bullish" if int(cross.get("direction", 0)) > 0 else "bearish"
                    exact = {}
                    if index > 0:
                        exact = interpolated_cross(
                            previous_timestamp=timestamps[index - 1], timestamp=timestamps[index],
                            previous_first=first_values[index - 1], previous_second=second_values[index - 1],
                            first=first_value, second=second_value,
                        )
                    event_id = f"{first_name}_{'above' if signal == 'bullish' else 'below'}_{second_name}"
                    crossing_value = exact.get("event_value_exact")
                    events.append({
                        "event_uid": f"{market}:{timeframe}:{timestamps[index]}:technical_cross:{event_id}",
                        "timestamp": timestamps[index], "event_id": event_id, "event_type": "technical_cross",
                        "event_group": "moving_average_cross", "signal": signal,
                        "label": f"{first_name.replace('_',' ').upper()} {'ABOVE' if signal == 'bullish' else 'BELOW'} {second_name.replace('_',' ').upper()}",
                        "marker": "arrow_up" if signal == "bullish" else "arrow_down",
                        "source": {"market": market, "timeframe": timeframe},
                        "display": {"screen_a": True, "screen_b": True,
                                    "anchor_timestamp": exact.get("event_timestamp_exact", timestamps[index]),
                                    "anchor_value": crossing_value if crossing_value is not None else finite(first_value),
                                    "marker_anchor": "exact_interpolated_cross" if crossing_value is not None else "source_candle"},
                        "calculation": {"first_series": first_name, "second_series": second_name,
                                        "first_value": finite(first_value), "second_value": finite(second_value),
                                        **exact, "crossing_value": crossing_value,
                                        "reference_basis": "precomputed_contract_series_linear_cross_interpolation"},
                        "event_timestamp_exact": exact.get("event_timestamp_exact"),
                        "event_value_exact": crossing_value, "event_price": crossing_value,
                    })

                slope = rolling_zscore(difference(closes), 30, 10)
                acceleration = difference(slope)
                delta_z = rolling_zscore(deltas, 30, 10)
                own_cvd_z = cvd_change_z[market][timeframe]
                other = "futures" if market == "spot" else "spot"
                cross_div = _aligned_difference(
                    timestamps, own_cvd_z,
                    cvd_timestamps[other][timeframe], cvd_change_z[other][timeframe],
                )

                price_records = _price_records_with_spot_fallback(
                    price_history, market, timeframe
                )
                price_by_ts = {int(row["timestamp"]): row.get("close") for row in price_records if isinstance(row, Mapping) and row.get("timestamp") is not None}
                price_closes = [price_by_ts.get(ts) for ts in timestamps]
                price_z = rolling_zscore(difference(price_closes), 30, 10)
                price_cvd_div = [None if pz is None or cz is None else float(pz) - float(cz) for pz, cz in zip(price_z, own_cvd_z, strict=True)]
                wasserstein = rolling_wasserstein(difference(closes), 20, 100)

                native = {
                    "cvd_slope_acceleration": self._native_indicator("cvd_slope_acceleration", timestamps, {"slope": slope, "acceleration": acceleration}, section="CVD DYNAMICS", label="CVD SLOPE / ACCELERATION"),
                    "delta_zscore": self._native_indicator("delta_zscore", timestamps, {"zscore": delta_z}, section="DELTA", label="DELTA Z-SCORE"),
                    "buy_sell_imbalance": self._native_indicator("buy_sell_imbalance", timestamps, {"imbalance": imbalances}, section="ORDER FLOW", label="BUY / SELL IMBALANCE"),
                    "price_cvd_divergence": self._native_indicator("price_cvd_divergence", timestamps, {"divergence": price_cvd_div}, section="DIVERGENCE", label="PRICE ↔ CVD DIVERGENCE"),
                    "spot_futures_divergence": self._native_indicator("spot_futures_divergence", timestamps, {"divergence": cross_div}, section="CROSS MARKET", label="SPOT ↔ FUTURES DIVERGENCE"),
                    "wasserstein_distance": self._native_indicator("wasserstein_distance", timestamps, {"wasserstein_distance": wasserstein}, section="REGIME", label="WASSERSTEIN DISTANCE"),
                }
                prepared[market][timeframe] = {
                    "status": source["status"], "source_records": len(records), "timestamps": timestamps,
                    "overlays": {"moving_averages": {"alignment": "cvd_candles_by_index",
                                                     "series": {name: list(values) for name, values in ma_series.items() if name in {"ema_9","ema_21","sma_20","sma_50","wma_20","wma_50"}},
                                                     "parameters": {"ema_periods": [9,21], "sma_periods": [20,50], "wma_periods": [20,50]},
                                                     "status": "ok" if candles and any(value is not None for values in ma_series.values() for value in values) else "unavailable"}},
                    "indicators": native, "events": events, "calculation_history_records": len(records),
                    "screen_b_data_mode": "runtime_processing", "screen_b_processing_contract": "native_cvd_orderflow_vr1",
                }
        return {
            "analysis_id": "cvd_native_orderflow_analysis", "contract_version": "2.0.0",
            "source": "CVD OHLC + taker buy/sell + Price context",
            "markets": {m: {"source_chart_id": f"cvd_{m}", "title": f"CVD {m.title()}", "timeframes": prepared[m]} for m in MARKETS},
            "recalculate_in_hmi": False, "calculation_owner": "Processing",
            "selector_contract": {"markets": list(MARKETS), "timeframes": list(TARGET_TIMEFRAMES)},
            "screen_b_native_orderflow_contract": {"enabled": True, "indicator_order": [
                "cvd_slope_acceleration", "delta_zscore", "buy_sell_imbalance",
                "price_cvd_divergence", "spot_futures_divergence", "wasserstein_distance"
            ]},
        }

    def build_cross_market(self, markets: Mapping[str, Any]) -> dict[str, Any]:
        """Build KPI-level cross-market flow without collapsing Spot/Futures charts."""
        windows: dict[str, Any] = {}
        for window in ("4h", "24h"):
            spot = markets["spot"]["window_summaries"][window]
            futures = markets["futures"]["window_summaries"][window]
            buy = float(spot.get("taker_buy_volume_usd", 0.0) or 0.0) + float(futures.get("taker_buy_volume_usd", 0.0) or 0.0)
            sell = float(spot.get("taker_sell_volume_usd", 0.0) or 0.0) + float(futures.get("taker_sell_volume_usd", 0.0) or 0.0)
            features = volume_features(buy, sell)

            expected = 4 if window == "4h" else 96
            spot_rows = markets["spot"]["timeframes"]["15m"]["records"][-expected:]
            futures_rows = markets["futures"]["timeframes"]["15m"]["records"][-expected:]
            spot_by_ts = {row["timestamp"]: row for row in spot_rows}
            futures_by_ts = {row["timestamp"]: row for row in futures_rows}
            timestamps = sorted(set(spot_by_ts) & set(futures_by_ts))
            combined_deltas = [spot_by_ts[t]["volume_delta_usd"] + futures_by_ts[t]["volume_delta_usd"] for t in timestamps]
            denominator = sum(abs(value) for value in combined_deltas)
            efficiency = abs(sum(combined_deltas)) / denominator if denominator else None
            complete = (len(timestamps) == expected and len(spot_rows) == expected and len(futures_rows) == expected
                and all(not spot_by_ts[t].get("is_partial") and not futures_by_ts[t].get("is_partial") for t in timestamps))
            status = "available" if complete else ("partial" if timestamps else "unavailable")
            windows[window] = {
                **features,
                "flow_efficiency": {"value": efficiency, "status": "available" if efficiency is not None else "unavailable",
                    "reason": None if efficiency is not None else "zero_absolute_delta_path"},
                "directional_persistence": self.feature_builder.directional_persistence(combined_deltas),
                "records_expected": expected, "records_used": len(timestamps), "coverage_complete": complete,
                "first_timestamp": timestamps[0] if timestamps else None, "last_timestamp": timestamps[-1] if timestamps else None,
                "status": status, "reason": None if status == "available" else "cross_market_window_incomplete",
            }

        spot_4h = markets["spot"]["window_summaries"]["4h"]
        futures_4h = markets["futures"]["window_summaries"]["4h"]
        spot_volume = float(spot_4h.get("total_volume_usd", 0.0) or 0.0)
        futures_volume = float(futures_4h.get("total_volume_usd", 0.0) or 0.0)
        ratio = None if spot_volume <= 0 else futures_volume / spot_volume
        ratio_status = "available" if ratio is not None else "unavailable"

        spot_fp = markets["spot"]["footprint_summaries"]["4h"]
        futures_fp = markets["futures"]["footprint_summaries"]["4h"]
        base = float(spot_fp.get("base_volume", 0.0) or 0.0) + float(futures_fp.get("base_volume", 0.0) or 0.0)
        quote = float(spot_fp.get("quote_volume", 0.0) or 0.0) + float(futures_fp.get("quote_volume", 0.0) or 0.0)
        vwap = None if base <= 0 else quote / base
        fp_status = "available" if vwap is not None and all(p.get("status") == "available" for p in (spot_fp, futures_fp)) else (
            "partial" if vwap is not None else "unavailable")
        footprint = {
            "vwap_usd": vwap, "base_volume": base, "quote_volume": quote,
            "records_used": int(spot_fp.get("records_used", 0) or 0) + int(futures_fp.get("records_used", 0) or 0),
            "levels_used": int(spot_fp.get("levels_used", 0) or 0) + int(futures_fp.get("levels_used", 0) or 0),
            "status": fp_status, "reason": None if fp_status == "available" else "cross_market_footprint_partial",
            "calculation_basis": "combined_spot_futures_normalized_footprint", "aggregation_scope": "cross_market",
        }
        return {
            "window_summaries": windows,
            "volume_ratios": {"futures_vs_spot": {"4h": {
                "value": ratio, "status": ratio_status, "reason": None if ratio_status == "available" else "spot_volume_zero",
                "futures_volume_usd": futures_volume, "spot_volume_usd": spot_volume,
                "timestamp": max(spot_4h.get("last_timestamp") or 0, futures_4h.get("last_timestamp") or 0) or None,
            }}},
            "footprint_summaries": {"4h": footprint},
        }

    def evaluate_availability(self, records: Sequence[Mapping[str, Any]], *, input_status: str = "available",
                              alignment_complete: bool = True) -> tuple[str, str | None]:
        if not records:
            return "unavailable", "no_records"
        if input_status == "invalid":
            return "invalid", "input_dataset_invalid"
        if input_status in {"partial", "unavailable"}:
            return "partial", "input_dataset_partial"
        if not alignment_complete:
            return "partial", "spot_futures_timestamp_misalignment"
        current = records[-1]
        if any(row["is_partial"] for row in records):
            return "partial", "incomplete_source_bucket"
        if current["continuity_status"] == "broken":
            return "partial", "cvd_continuity_broken_by_missing_intervals"
        if current["delta_ma_21_usd"] is None or current["flow_efficiency"]["status"] != "available":
            return "partial", "rolling_warmup_incomplete"
        return "available", None

    def _timeframe_contract(self, target: str, feature: Mapping[str, Any], *, input_status: str,
                            alignment_complete: bool = True) -> dict[str, Any]:
        # build_market_features creates this list solely for the resulting
        # Processing contract.  Transfer that ownership instead of cloning the
        # full history; this method only reads it.  ``current`` remains copied so
        # callers can mutate that contractual snapshot independently of records.
        records = feature["records"]
        status, reason = self.evaluate_availability(records, input_status=input_status, alignment_complete=alignment_complete)
        return {"status": status, "reason": reason, "source_timeframe": SOURCE_TIMEFRAME[target], "target_timeframe": target,
            "interval_seconds": TIMEFRAME_SECONDS[target], "source_factor": SOURCE_FACTOR[target], "records_available": len(records),
            "first_timestamp": records[0]["timestamp"] if records else None, "last_timestamp": records[-1]["timestamp"] if records else None,
            "current_timestamp": records[-1]["timestamp"] if records else None,
            "complete_records": sum(not row["is_partial"] for row in records), "partial_records": sum(row["is_partial"] for row in records),
            "gap_count": len(feature["continuity_breaks"]), "continuity_break_count": len(feature["continuity_breaks"]),
            "continuity_breaks": copy.deepcopy(feature["continuity_breaks"]), "anchor_method": feature["anchor_method"],
            "anchor_timestamp": feature["anchor_timestamp"], "anchor_value_usd": feature["anchor_value_usd"],
            "history_relative": feature["history_relative"], "construction": feature["construction"], "native_ohlc": feature["native_ohlc"],
            "provider_reference_used_in_calculation": False, "records": records,
            "current": copy.deepcopy(records[-1]) if records else None}

    def process_market(self, market: str, base_records: Mapping[str, Sequence[Mapping[str, Any]]],
                       input_market: Mapping[str, Any]) -> dict[str, Any]:
        declared_gaps = {source: input_market["cvd"]["timeframes"][source].get("gaps", []) for source in BASE_TIMEFRAMES}
        features = self.feature_builder.build_market_features(base_records, declared_gaps)
        timeframes = {}
        for target in TARGET_TIMEFRAMES:
            source = SOURCE_TIMEFRAME[target]
            input_status = input_market["cvd"]["timeframes"][source].get("status", "available")
            timeframes[target] = self._timeframe_contract(target, features[target], input_status=input_status)
        summaries = {name: self.feature_builder.build_fixed_window_summary(timeframes["15m"]["records"], name) for name in ("4h", "24h")}
        footprint = self.feature_builder.build_footprint_vwap(input_market.get("footprint"))
        availability = {"timeframes": {target: {"status": payload["status"], "reason": payload["reason"]} for target, payload in timeframes.items()},
            "window_summaries": {name: {"status": payload["status"], "reason": payload["reason"]} for name, payload in summaries.items()},
            "footprint_vwap": {"status": footprint["status"], "reason": footprint["reason"]}}
        return {"timeframes": timeframes, "window_summaries": summaries, "footprint_summaries": {"4h": footprint},
            "price_vs_vwap": {}, "availability": availability}

    def evaluate_quality(self, markets: Mapping[str, Any], input_quality: Mapping[str, Any]) -> dict[str, Any]:
        core = [markets[market]["timeframes"][timeframe]["status"] for market in MARKETS for timeframe in TARGET_TIMEFRAMES]
        enrichments = [markets[market]["footprint_summaries"]["4h"]["status"] for market in MARKETS]
        enrichments.extend(markets[market]["price_vs_vwap"]["status"] for market in MARKETS)
        summaries = [markets[market]["window_summaries"][window]["status"] for market in MARKETS for window in ("4h", "24h")]
        no_safe_base = all(markets[market]["timeframes"][timeframe]["status"] == "unavailable"
            for market in ("spot", "futures") for timeframe in BASE_TIMEFRAMES)
        core_status = "invalid" if no_safe_base or "invalid" in core or input_quality.get("status") == "invalid" else (
            "available" if all(item == "available" for item in core + summaries) else "partial")
        enrichment_status = "available" if all(item == "available" for item in enrichments) else "partial"
        # Footprint/price-reference enrichments are optional. They are reported
        # independently but do not downgrade a complete CVD core contract.
        status = "invalid" if core_status == "invalid" else ("ok" if core_status == "available" else "partial")
        warnings = [f"{market}.{timeframe}:{markets[market]['timeframes'][timeframe]['reason']}" for market in MARKETS for timeframe in TARGET_TIMEFRAMES
            if markets[market]["timeframes"][timeframe]["status"] != "available"]
        warnings.extend(f"input_quality:{input_quality.get('status')}" for _ in [0] if input_quality.get("status") not in {None, "ok"})
        errors = [item for item in warnings if "invalid" in item]
        return {"status": status, "core_status": core_status, "enrichment_status": enrichment_status,
            "warnings": warnings, "errors": errors}

    def run(self, input_contract: Mapping[str, Any], *, price_reference_by_market: Mapping[str, Mapping[str, Any]] | None = None,
            price_history_by_market_timeframe: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]] | None = None) -> dict[str, Any]:
        normalized = self.validate_input_contract(input_contract)
        processing_timestamp = _clock_timestamp(self.clock)
        input_markets = input_contract["markets"]
        spot = self.process_market("spot", normalized["spot"], input_markets["spot"])
        futures = self.process_market("futures", normalized["futures"], input_markets["futures"])
        markets = {"spot": spot, "futures": futures}
        references = price_reference_by_market or {}
        if not isinstance(references, Mapping) or set(references) - set(MARKETS):
            raise ValueError("invalid_price_reference_markets")
        for market in MARKETS:
            markets[market]["price_vs_vwap"] = self.feature_builder.build_price_vs_vwap(
                markets[market]["footprint_summaries"]["4h"], references.get(market))
            markets[market]["availability"]["price_vs_vwap"] = {"status": markets[market]["price_vs_vwap"]["status"],
                "reason": markets[market]["price_vs_vwap"]["reason"]}
        cross_market = self.build_cross_market(markets)
        quality = self.evaluate_quality(markets, input_contract.get("quality", {}))
        return {"family": CVD_VOLUME_ORDERFLOW_FAMILY, "stage": PROCESSING_STAGE, "version": PROCESSING_VERSION,
            "mode": input_contract["mode"], "context": self.build_context(input_contract, processing_timestamp),
            "parameters": self.build_parameters(), "markets": markets, "cross_market": cross_market,
            "technical_analysis": self.build_technical_analysis(markets, price_history_by_market_timeframe=price_history_by_market_timeframe), "quality": quality}


def process_cvd_volume_orderflow(input_contract: Mapping[str, Any], *, price_reference_by_market: Mapping[str, Mapping[str, Any]] | None = None,
                                 price_history_by_market_timeframe: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]] | None = None,
                                 clock: Callable[[], Any] | None = None) -> dict[str, Any]:
    return CvdVolumeOrderflowProcessor(clock=clock).run(
        input_contract, price_reference_by_market=price_reference_by_market,
        price_history_by_market_timeframe=price_history_by_market_timeframe,
    )
