"""Canonical Volatility screen builder.

Volatility owns volatility only. Long/short positioning is owned by the
Long/Short & Liquidations family and is intentionally absent here.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
import json
import math
from typing import Any


FAMILY = "volatility_market_regimes"
PROCESSING_VERSION = "0.2.0"
CLASSIFICATION_VERSION = "0.2.0"
SCREEN_SCHEMA_VERSION = "2.0.0-native-volatility-screen-b-demo"
DISPLAY_RANGE_OPTIONS = ("7d", "30d")
DEFAULT_DISPLAY_RANGE = "30d"
_VALID_MODES = {"bootstrap", "incremental", "recovery"}


def _iso(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}:timezone_iso8601_required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{path}:timezone_iso8601_required") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{path}:timezone_iso8601_required")
    return value


def _strict_json(value: Any, path: str = "root") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path}:finite_number_required")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path}:string_keys_required")
            _strict_json(item, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _strict_json(item, f"{path}[{index}]")
        return
    raise ValueError(f"{path}:json_value_required")


def validate_runtime_context(runtime_context: Any) -> None:
    if not isinstance(runtime_context, Mapping):
        raise ValueError("runtime_context:mapping_required")
    data_mode, is_demo = runtime_context.get("data_mode"), runtime_context.get("is_demo")
    if data_mode not in {"synthetic", "live"} or type(is_demo) is not bool:
        raise ValueError("runtime_context:data_mode_or_is_demo_invalid")
    if (data_mode == "synthetic") != is_demo:
        raise ValueError("runtime_context:data_mode_is_demo_mismatch")
    _iso(runtime_context.get("generated_at"), "runtime_context.generated_at")
    _iso(runtime_context.get("updated_at"), "runtime_context.updated_at")


def validate_volatility_market_regimes_builder_inputs(
    processing: Any,
    classification: Any,
    runtime_context: Any,
    selected_range: str = DEFAULT_DISPLAY_RANGE,
) -> None:
    if selected_range not in DISPLAY_RANGE_OPTIONS:
        raise ValueError("selected_range:invalid")
    validate_runtime_context(runtime_context)
    for name, contract, stage, version in (
        ("processing", processing, "processing", PROCESSING_VERSION),
        ("classification", classification, "classification", CLASSIFICATION_VERSION),
    ):
        if not isinstance(contract, Mapping):
            raise ValueError(f"{name}:mapping_required")
        if contract.get("family") != FAMILY or contract.get("stage") != stage or contract.get("version") != version:
            raise ValueError(f"{name}:identity_invalid")
        if contract.get("mode") not in _VALID_MODES or not isinstance(contract.get("context"), Mapping):
            raise ValueError(f"{name}:mode_or_context_invalid")
    if processing.get("mode") != classification.get("mode"):
        raise ValueError("builder_contract_mismatch:mode")
    features = processing.get("features")
    classes = classification.get("classifications")
    if not isinstance(features, Mapping):
        raise ValueError("processing.features:mapping_required")
    if not isinstance(classes, Mapping):
        raise ValueError("classification.classifications:mapping_required")
    for key in ("realized_volatility", "dvol", "volatility_spread", "daily_regime_basis", "volatility_native_analytics"):
        if key not in features:
            raise ValueError(f"processing.features.{key}:required")
    for key in ("daily_regimes", "volatility_context"):
        if key not in classes:
            raise ValueError(f"classification.classifications.{key}:required")
    _strict_json(processing, "processing")
    _strict_json(classification, "classification")
    _strict_json(runtime_context, "runtime_context")


def _invalid_screen(error: str) -> dict[str, Any]:
    return {
        "family": FAMILY,
        "screen": FAMILY,
        "schema_version": SCREEN_SCHEMA_VERSION,
        "context": {},
        "badges": [],
        "selectors": {},
        "kpis": {"items": []},
        "charts": {},
        "volatility_analysis": {"indicator_order": [], "indicators": {}},
        "quality": {"status": "invalid", "errors": [error]},
        "provenance": {},
    }


class VolatilityMarketRegimesContractBuilder:
    def build(
        self,
        processing_contract: Mapping[str, Any],
        classification_contract: Mapping[str, Any],
        *,
        runtime_context: Mapping[str, Any],
        selected_range: str = DEFAULT_DISPLAY_RANGE,
    ) -> dict[str, Any]:
        before = deepcopy((processing_contract, classification_contract, runtime_context))
        try:
            validate_volatility_market_regimes_builder_inputs(
                processing_contract, classification_contract, runtime_context, selected_range
            )
            # SP 2.0 is the sole screen projection. No legacy positioning/TA layer
            # is allowed to mutate the canonical native-volatility contract.
            screen = align_volatility_market_regimes_to_sp_v2_0(
                {}, processing_contract, classification_contract,
                runtime_context=runtime_context, selected_range=selected_range,
            )
            _strict_json(screen, "screen_contract")
            json.dumps(screen, ensure_ascii=False, allow_nan=False, sort_keys=False)
            if (processing_contract, classification_contract, runtime_context) != before:
                raise RuntimeError("Contract Builder mutated upstream state")
            return screen
        except RuntimeError:
            raise
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            return _invalid_screen(str(exc))


def build_volatility_market_regimes_screen(
    processing_contract: Any,
    classification_contract: Any,
    *,
    runtime_context: Any,
    selected_range: str = DEFAULT_DISPLAY_RANGE,
) -> dict[str, Any]:
    return VolatilityMarketRegimesContractBuilder().build(
        processing_contract,
        classification_contract,
        runtime_context=runtime_context,
        selected_range=selected_range,
    )

# --- Canonical Screen contract shaping ---
from collections.abc import Mapping

from copy import deepcopy

import json, math

from pathlib import Path

from typing import Any

SP_SCHEMA_VERSION = '2.0.0-native-volatility-screen-b-demo'

_screen_TEMPLATE = Path(__file__).with_name('screen_template.json')

_screen_DAY = 86400

def _screen_daily_last(records: list[Mapping[str, Any]], value_key: str) -> list[tuple[int, float]]:
    by_day: dict[int, float] = {}
    for row in records:
        ts = row.get('timestamp')
        value = row.get(value_key)
        if type(ts) is int and isinstance(value, (int, float)) and (not isinstance(value, bool)) and math.isfinite(float(value)):
            by_day[ts - ts % _screen_DAY] = float(value)
    return sorted(by_day.items())[-730:]

def _screen_rolling(values: list[float], window: int) -> list[float]:
    out = []
    for i in range(len(values)):
        w = values[max(0, i + 1 - window):i + 1]
        out.append(sum(w) / len(w))
    return out

def _screen_z(values: list[float], window: int=30) -> list[float]:
    out = []
    for i, v in enumerate(values):
        w = values[max(0, i + 1 - window):i + 1]
        m = sum(w) / len(w)
        var = sum(((x - m) ** 2 for x in w)) / len(w)
        sd = math.sqrt(var)
        out.append(0.0 if sd == 0 else (v - m) / sd)
    return out

def _screen_pct(values: list[float], window: int=30) -> list[float]:
    out = []
    for i, v in enumerate(values):
        w = values[max(0, i + 1 - window):i + 1]
        out.append(100.0 * sum((x <= v for x in w)) / len(w))
    return out

def _screen_wasserstein(values: list[float], window: int=30) -> list[float]:
    out = []
    for i in range(len(values)):
        if i + 1 < window * 2:
            out.append(0.0)
            continue
        a = sorted(values[i + 1 - window * 2:i + 1 - window])
        b = sorted(values[i + 1 - window:i + 1])
        scale = max(abs(sum(a) / len(a)), 1e-09)
        out.append(sum((abs(x - y) for x, y in zip(a, b))) / len(a) / scale)
    return out

def _screen_summary(label: str, series: list[float | None]) -> dict[str, Any]:
    value = next((float(item) for item in reversed(series)
                  if isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(float(item))), None)
    if value is None:
        return {'label': label, 'display_value': 'UNAVAILABLE', 'signal': 'UNAVAILABLE', 'strength': 0.0, 'value': None}
    strength = min(1.0, abs(value))
    signal = 'HIGH' if value > 1 else 'LOW' if value < -1 else 'NORMAL'
    return {'label': label, 'display_value': f'{value:.3f}', 'signal': signal, 'strength': strength, 'value': value}

def align_volatility_market_regimes_to_sp_v2_0(screen: Mapping[str, Any], processing: Mapping[str, Any], classification: Mapping[str, Any], *, runtime_context: Mapping[str, Any], selected_range: str) -> dict[str, Any]:
    target = json.loads(_screen_TEMPLATE.read_text(encoding='utf-8'))
    features = processing.get('features', {})
    native = features.get('volatility_native_analytics', {}) if isinstance(features.get('volatility_native_analytics'), Mapping) else {}
    ts = list(native.get('timestamps', []))[-730:]
    charts = native.get('charts', {}) if isinstance(native.get('charts'), Mapping) else {}
    indicators_native = native.get('indicators', {}) if isinstance(native.get('indicators'), Mapping) else {}

    def series(block: str, key: str) -> list[float]:
        payload = charts.get(block, {}) if isinstance(charts.get(block), Mapping) else {}
        return list(payload.get(key, []))[-len(ts):] if ts else []
    rv7 = series('realized_volatility', 'rv_7d')
    rv30 = series('realized_volatility', 'rv_30d')
    dvol = series('implied_volatility', 'dvol')
    iv1m = series('implied_volatility', 'iv_1m')
    iv1w = series('term_structure', 'iv_1w')
    iv3m = series('term_structure', 'iv_3m')
    iv6m = series('term_structure', 'iv_6m')
    vrp = series('implied_vs_realized', 'vrp')
    rvz = list(indicators_native.get('volatility_zscore_percentile', {}).get('rv_zscore', []))[-len(ts):] if ts else []
    rvp = list(indicators_native.get('volatility_zscore_percentile', {}).get('rv_percentile', []))[-len(ts):] if ts else []
    vrpz = list(indicators_native.get('volatility_risk_premium', {}).get('vrp_zscore', []))[-len(ts):] if ts else []
    term_slope = list(indicators_native.get('term_structure_slope', {}).get('term_slope', []))[-len(ts):] if ts else []
    term_curvature = list(indicators_native.get('term_structure_slope', {}).get('term_curvature', []))[-len(ts):] if ts else []
    skew = list(indicators_native.get('volatility_skew_tail_risk', {}).get('skew_25d', []))[-len(ts):] if ts else []
    upside = list(indicators_native.get('volatility_skew_tail_risk', {}).get('upside_iv', []))[-len(ts):] if ts else []
    downside = list(indicators_native.get('volatility_skew_tail_risk', {}).get('downside_iv', []))[-len(ts):] if ts else []
    vol_of_vol = list(indicators_native.get('vol_of_vol_acceleration', {}).get('vol_of_vol', []))[-len(ts):] if ts else []
    accel = list(indicators_native.get('vol_of_vol_acceleration', {}).get('rv_acceleration', []))[-len(ts):] if ts else []
    wz = list(indicators_native.get('regime_shift_wasserstein', {}).get('wasserstein_distance', []))[-len(ts):] if ts else []
    transition = list(indicators_native.get('regime_shift_wasserstein', {}).get('transition_probability', []))[-len(ts):] if ts else []
    target['context']['selected_display_range'] = selected_range
    target['context']['data_mode'] = runtime_context.get('data_mode')
    target['context']['is_demo'] = runtime_context.get('is_demo')
    target['context']['generated_at'] = runtime_context.get('generated_at')
    target['context']['data_as_of'] = processing.get('context', {}).get('reference_timestamp')
    target['context']['raw_provider_targets'] = {'realized_volatility': 'derived_from_prices', 'dvol': 'glassnode.dvol_ohlc', 'implied_volatility': 'derived_from_dvol', 'term_structure': 'derived_from_dvol', 'skew': 'derived_from_dvol'}
    target['charts']['realized_volatility']['records'] = [{'timestamp': t, 'rv_7d': a, 'rv_30d': b} for t, a, b in zip(ts, rv7, rv30)]
    target['charts']['implied_volatility']['records'] = [{'timestamp': t, 'dvol': a, 'iv_1m': b} for t, a, b in zip(ts, dvol, iv1m)]
    target['charts']['implied_vs_realized']['records'] = [{'timestamp': t, 'iv_1m': a, 'rv_30d': b, 'vrp': c} for t, a, b, c in zip(ts, iv1m, rv30, vrp)]
    target['charts']['term_structure']['records'] = [{'timestamp': t, 'iv_1w': a, 'iv_1m': b, 'iv_3m': c, 'iv_6m': d} for t, a, b, c, d in zip(ts, iv1w, iv1m, iv3m, iv6m)]
    for chart in target['charts'].values():
        if isinstance(chart, dict):
            chart['status'] = 'available' if chart.get('records') else 'unavailable'
            chart['reason'] = None if chart.get('records') else 'processing_history_unavailable'
    inds = target['volatility_analysis']['indicators']
    payloads = {'volatility_zscore_percentile': ({'rv_zscore': rvz, 'rv_percentile': rvp}, 'Volatility Z-Score / Percentile'), 'volatility_risk_premium': ({'vrp': vrp, 'vrp_zscore': vrpz}, 'Volatility Risk Premium'), 'term_structure_slope': ({'term_slope': term_slope, 'term_curvature': term_curvature}, 'IV Term Structure / Slope'), 'volatility_skew_tail_risk': ({'skew_25d': skew, 'upside_iv': upside, 'downside_iv': downside}, 'Volatility Skew / Tail Risk'), 'vol_of_vol_acceleration': ({'vol_of_vol': vol_of_vol, 'rv_acceleration': accel}, 'Vol-of-Vol / Acceleration'), 'regime_shift_wasserstein': ({'wasserstein_distance': wz, 'transition_probability': transition}, 'Regime Shift / Wasserstein')}
    for key, (series, label) in payloads.items():
        inds[key]['timestamps'] = ts
        inds[key]['series'] = series
        first = next(iter(series.values())) if series else []
        inds[key]['summary'] = _screen_summary(label, first)
        has_data = any(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))
                       for values in series.values() for value in values)
        inds[key]['status'] = 'available' if ts and has_data else 'unavailable'
        inds[key]['data_mode'] = runtime_context.get('data_mode')
        inds[key]['real_market_calculation'] = True
        inds[key]['hmi_recalculate'] = False
    current_rv = rv7[-1] if rv7 else None
    current_d = dvol[-1] if dvol else None
    current_vrp = vrp[-1] if vrp else None
    current_skew = skew[-1] if skew else None
    pct = rvp[-1] if rvp else 50.0
    regime = 'high' if pct >= 80 else 'low' if pct <= 20 else 'normal'
    conf = min(1.0, abs(pct - 50) / 50) if ts else 0.0
    vals = [regime, current_rv, current_d, current_vrp, current_skew, conf]
    for item, val in zip(target['kpis']['items'], vals):
        item['value'] = val
        item['status'] = 'available' if val is not None else 'unavailable'
        if item['metric_id'] == 'current_regime':
            item['display_value'] = str(val).upper() if val is not None else '—'
        elif item['metric_id'] == 'regime_confidence':
            item['display_value'] = f'{val * 100:.0f}%' if val is not None else '—'
        elif item['unit'] == 'percent':
            item['display_value'] = f'{val:.1f}%' if val is not None else '—'
        elif item['unit'] == 'volatility_points':
            item['display_value'] = f'{val:+.1f}' if val is not None else '—'
    target['quality']['data_mode'] = runtime_context.get('data_mode')
    target['quality']['records'] = len(ts)
    target['quality']['real_market_calculation'] = True
    target['quality']['hmi_recalculate'] = False
    charts_ok = all(isinstance(chart, Mapping) and chart.get('status') == 'available' for chart in target['charts'].values())
    indicators_ok = all(isinstance(indicator, Mapping) and indicator.get('status') == 'available' for indicator in inds.values())
    final_ok = bool(ts) and charts_ok and indicators_ok
    target['quality']['status'] = 'ok' if final_ok else 'partial'
    target['quality']['contract_complete'] = True
    target['quality']['data_complete'] = final_ok
    return target
