from __future__ import annotations

import json
import math
from typing import Any, Mapping, Sequence

RSI_OVERSOLD                = 30.0
RSI_OVERBOUGHT              = 70.0
RSI_NEUTRAL_LOW             = 45.0
RSI_NEUTRAL_HIGH            = 55.0
STOCHASTIC_OVERSOLD         = 20.0
STOCHASTIC_OVERBOUGHT       = 80.0
CCI_BEARISH_LEVEL           = -100.0
CCI_BULLISH_LEVEL           = 100.0
MFI_OVERSOLD                = 20.0
MFI_OVERBOUGHT              = 80.0
WILLIAMS_OVERSOLD           = -80.0
WILLIAMS_OVERBOUGHT         = -20.0
ADX_WEAK_MAX                = 20.0
ADX_TREND_MIN               = 25.0
ADX_STRONG_MIN              = 40.0
TSI_NEUTRAL_TOLERANCE       = 1.0
BIAS_NEUTRAL_TOLERANCE      = 0.15
BIAS_STRONG_THRESHOLD       = 0.60
BASIS_NEUTRAL_PERCENT       = 0.05
LEADERSHIP_SCORE_DIFFERENCE = 0.20
ATR_LOW_PERCENT             = 0.50
ATR_MODERATE_PERCENT        = 1.50
ATR_HIGH_PERCENT            = 3.00

TIMEFRAME_ORDER = ("1m", "5m", "15m", "4h")
MARKET_ORDER    = ("spot", "futures")

PRICE_RETURN_VOLATILITY_THRESHOLDS = {
    "1m": {"low": 0.0010, "high": 0.0030}, "5m": {"low": 0.0020, "high": 0.0060},
    "15m": {"low": 0.0030, "high": 0.0100},
    "4h": {"low": 0.0120, "high": 0.0400}}

BIAS_COMPONENT_WEIGHTS = {
    "ema_9_minus_ema_21": 1.00, "ema_21_minus_ema_50": 1.20, "sma_20_minus_sma_50": 1.00,
    "macd_minus_signal": 1.10, "macd_histogram": 1.10, "rsi_centered": 0.80,
    "stochastic_k_minus_d": 0.60, "di_plus_minus_di_minus": 1.00, "cci": 0.60,
    "mfi_centered": 0.60, "williams_r_centered": 0.50, "tsi": 0.80,
    "close_minus_bollinger_middle": 0.70,
}
SHORT_TIMEFRAME_WEIGHTS = {"1m": 0.25, "5m": 0.75}
MID_TIMEFRAME_WEIGHTS   = {"15m": 1.00}
LONG_TIMEFRAME_WEIGHTS  = {"4h": 1.00}
OVERALL_GROUP_WEIGHTS   = {"short": 0.30, "mid": 0.40, "long": 0.30}

REQUIRED_INDICATOR_SIGNALS = (
    "rsi", "macd", "macd_signal", "macd_histogram", "stochastic", "adx", "cci", "mfi", "williams_r", "atr", "tsi")
REQUIRED_STATISTICAL_SIGNALS = (
    "mean", "standard_deviation", "skewness", "kurtosis", "z_score", "var_95", "cvar_95", "max_consecutive_wins",
    "max_consecutive_losses", "omega_ratio", "sharpe_ratio", "sortino_ratio", "calmar_ratio", "max_drawdown",
    "profit_factor", "recovery_factor", "win_rate")

PATTERN_LABELS = {
    "doji": "Doji", "hammer": "Hammer", "inverted_hammer": "Inverted Hammer", "shooting_star": "Shooting Star",
    "bullish_engulfing": "Bullish Engulfing", "bearish_engulfing": "Bearish Engulfing", "morning_star": "Morning Star",
    "evening_star": "Evening Star", "three_white_soldiers": "Three White Soldiers", "three_black_crows": "Three Black Crows",
}
CROSS_LABELS = {
    "ema_9_above_ema_21": "EMA 9 crossed above EMA 21", "ema_9_below_ema_21": "EMA 9 crossed below EMA 21",
    "ema_21_above_ema_50": "EMA 21 crossed above EMA 50", "ema_21_below_ema_50": "EMA 21 crossed below EMA 50",
    "sma_20_above_sma_50": "SMA 20 crossed above SMA 50", "sma_20_below_sma_50": "SMA 20 crossed below SMA 50",
    "macd_above_signal": "MACD crossed above Signal", "macd_below_signal": "MACD crossed below Signal",
    "k_above_d": "Stochastic K crossed above D", "k_below_d": "Stochastic K crossed below D",
    "di_plus_above_di_minus": "DI+ crossed above DI-", "di_plus_below_di_minus": "DI+ crossed below DI-",
}


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _classification(value: Any, signal: str, state: str, reason: str, confidence: float = 0.50,
                    color_token: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"value": _finite(value), "signal": signal, "state": state, "color_token": color_token or signal,
            "confidence": min(1.0, max(0.0, float(confidence))), "reason": reason, **extra}


def _missing(reason: str = "Numeric value is unavailable") -> dict[str, Any]:
    return _classification(None, "neutral", "unavailable", reason, 0.0, "neutral")


def _descriptive_or_missing(value: Any, *, state: str, reason: str, signal: str = "neutral",
                            color_token: str = "neutral", confidence: float = 0.50) -> dict[str, Any]:
    numeric = _finite(value)
    if numeric is None:
        return _missing(reason)
    return _classification(numeric, signal, state, reason, confidence, color_token)


def _sanitize_json_value(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(key): _sanitize_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_json_value(item) for item in value]
    return value


def _adaptive_oscillator_state(value: Any, thresholds: Mapping[str, Any] | None, *, name: str) -> dict[str, Any]:
    numeric = _finite(value)
    if numeric is None:
        return _missing()
    source = thresholds if isinstance(thresholds, Mapping) else {}
    lower = _finite(source.get("lower"))
    midpoint = _finite(source.get("midpoint"))
    upper = _finite(source.get("upper"))
    observed_min = _finite(source.get("observed_min"))
    observed_max = _finite(source.get("observed_max"))
    if None in (lower, midpoint, upper, observed_min, observed_max) or not (observed_min <= lower < midpoint < upper <= observed_max):
        return _classification(numeric, "neutral", "unavailable", f"{name} adaptive range is unavailable", 0.0, "neutral", dynamic_thresholds=dict(source))
    span = max(observed_max - observed_min, 1e-12)
    if numeric <= lower:
        state, signal, color = "oversold", "neutral", "warning"
    elif numeric >= upper:
        state, signal, color = "overbought", "neutral", "warning"
    elif numeric >= midpoint:
        state, signal, color = "normal_bullish", "bullish", "bullish"
    else:
        state, signal, color = "normal_bearish", "bearish", "bearish"
    confidence = min(1.0, max(0.0, abs(numeric - midpoint) / (0.5 * span)))
    return _classification(
        numeric, signal, state,
        f"{name} uses 20%/80% of its observed range with the observed midpoint separating normal bearish/bullish states",
        confidence, color, dynamic_thresholds=dict(source),
    )


def classify_rsi(value: Any, thresholds: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return _adaptive_oscillator_state(value, thresholds, name="RSI")

def classify_macd_histogram(value: Any, tolerance: float) -> dict[str, Any]:
    value = _finite(value)
    if value is None:
        return _missing()
    if value > tolerance:
        signal = "bullish"
    elif value < -tolerance:
        signal = "bearish"
    else:
        signal = "neutral"
    return _classification(value, signal, signal, "MACD histogram is compared with its scale-aware tolerance", min(1, abs(value) / max(tolerance, 1e-12)))


def classify_macd_signal(value: Any) -> dict[str, Any]:
    value = _finite(value)
    if value is None:
        return _missing()
    if value > 0:
        signal = "bullish"
    elif value < 0:
        signal = "bearish"
    else:
        signal = "neutral"
    return _classification(value, signal, signal, "MACD Signal position relative to zero", min(1, abs(value) / max(abs(value), 1)))


def classify_macd(macd: Any, signal: Any, histogram: Any) -> dict[str, Any]:
    macd_value, signal_value = _finite(macd), _finite(signal)
    tolerance = max(abs(macd_value or 0), abs(signal_value or 0), 1.0) * 0.001
    output    = classify_macd_histogram(histogram, tolerance)
    output.update({"macd": macd_value, "signal_value": signal_value, "histogram": _finite(histogram), "tolerance": tolerance})
    return output


def classify_stochastic(k: Any, d: Any) -> dict[str, Any]:
    k_value, d_value = _finite(k), _finite(d)
    if k_value is None or d_value is None:
        return _missing()
    tolerance = max(abs(k_value), abs(d_value), 1) * 0.001
    if k_value - d_value > tolerance:
        signal = "bullish"
    elif d_value - k_value > tolerance:
        signal = "bearish"
    else:
        signal = "neutral"
    if k_value > STOCHASTIC_OVERBOUGHT:
        state = "overbought"
    elif k_value < STOCHASTIC_OVERSOLD:
        state = "oversold"
    else:
        state = "neutral"
    return _classification(k_value, signal, state, "Stochastic K direction is measured against D", min(1, abs(k_value - d_value) / 100), k=k_value, d=d_value)


def classify_adx(adx: Any, di_plus: Any, di_minus: Any) -> dict[str, Any]:
    adx_value, plus, minus = _finite(adx), _finite(di_plus), _finite(di_minus)
    if adx_value is None:
        return _missing()
    if adx_value < ADX_WEAK_MAX:
        strength = "weak"
    elif adx_value < ADX_TREND_MIN:
        strength = "developing"
    elif adx_value < ADX_STRONG_MIN:
        strength = "strong"
    else:
        strength = "very_strong"
    tolerance = max(abs(plus or 0), abs(minus or 0), 1) * 0.001
    if plus is None or minus is None or abs(plus - minus) <= tolerance:
        direction = "neutral"
    elif plus > minus:
        direction = "bullish"
    else:
        direction = "bearish"
    return _classification(adx_value, "neutral", strength, "ADX measures strength while DI values provide direction", min(1, adx_value / ADX_STRONG_MIN), "neutral",
                           direction=direction, di_plus=plus, di_minus=minus)


def classify_cci(value: Any) -> dict[str, Any]:
    value = _finite(value)
    if value is None:
        return _missing()
    if value > CCI_BULLISH_LEVEL:
        signal = "bullish"
    elif value < CCI_BEARISH_LEVEL:
        signal = "bearish"
    else:
        signal = "neutral"
    return _classification(value, signal, signal, "CCI is compared with configured directional levels", min(1, abs(value) / 200))


def classify_mfi(value: Any) -> dict[str, Any]:
    value = _finite(value)
    if value is None:
        return _missing()
    if value > MFI_OVERBOUGHT:
        state = "overbought"
    elif value < MFI_OVERSOLD:
        state = "oversold"
    else:
        state = "neutral"
    return _classification(value, "neutral", state, "MFI state uses configured overbought and oversold thresholds", abs(value - 50) / 50, "warning" if state != "neutral" else "neutral")


def classify_williams_r(value: Any) -> dict[str, Any]:
    value = _finite(value)
    if value is None:
        return _missing()
    if value > WILLIAMS_OVERBOUGHT:
        state = "overbought"
    elif value < WILLIAMS_OVERSOLD:
        state = "oversold"
    else:
        state = "neutral"
    return _classification(value, "neutral", state, "Williams %R state uses configured thresholds", min(1, abs(value + 50) / 50), "warning" if state != "neutral" else "neutral")


def classify_atr(absolute_value: Any, atr_percent_of_close: Any) -> dict[str, Any]:
    absolute, percent = _finite(absolute_value), _finite(atr_percent_of_close)
    if percent is None:
        return _missing("ATR percent of close is unavailable")
    if percent < ATR_LOW_PERCENT:
        state = "low"
    elif percent < ATR_MODERATE_PERCENT:
        state = "moderate"
    elif percent <= ATR_HIGH_PERCENT:
        state = "high"
    else:
        state = "extreme"
    return _classification(absolute, "neutral", state, "ATR percent of close is compared with configured volatility thresholds", min(1, percent / ATR_HIGH_PERCENT), "neutral",
                           atr_absolute=absolute, atr_percent_of_close=percent)


def classify_tsi(
    value: Any,
    parameters: Mapping[str, Any] | None = None,
    thresholds: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    output = _adaptive_oscillator_state(value, thresholds, name="TSI")
    output["parameters"] = dict(parameters or {})
    return output

def classify_indicator_package(indicator_package: Mapping[str, Any], bias_components: Mapping[str, Any] | None = None) -> dict[str, Any]:
    current = lambda name, field: indicator_package.get(name, {}).get("current", {}).get(field)
    macd    = indicator_package.get("macd", {}).get("current", {})
    stoch   = indicator_package.get("stochastic", {}).get("current", {})
    adx     = indicator_package.get("adx", {}).get("current", {})
    bias    = (bias_components or {}).get("values", bias_components or {})
    output  = {
        "rsi": classify_rsi(current("rsi", "rsi"), indicator_package.get("rsi", {}).get("dynamic_thresholds", {})),
        "macd": classify_macd(macd.get("macd"), macd.get("signal"), macd.get("histogram")),
        "macd_signal": classify_macd_signal(macd.get("signal")),
        "macd_histogram": classify_macd_histogram(macd.get("histogram"), max(abs(_finite(macd.get("macd")) or 0), abs(_finite(macd.get("signal")) or 0), 1) * 0.001),
        "stochastic": classify_stochastic(stoch.get("k"), stoch.get("d")),
        "adx": classify_adx(adx.get("adx"), adx.get("di_plus"), adx.get("di_minus")),
        "cci": classify_cci(current("cci", "cci")), "mfi": classify_mfi(current("mfi", "mfi")),
        "williams_r": classify_williams_r(current("williams_r", "williams_r")),
        "atr": classify_atr(current("atr", "atr"), bias.get("atr_percent_of_close")),
        "tsi": classify_tsi(
            current("tsi", "tsi"),
            indicator_package.get("tsi", {}).get("parameters", {}),
            indicator_package.get("tsi", {}).get("dynamic_thresholds", {}),
        ),
    }
    parameter_sources = {"rsi": "rsi", "macd": "macd", "macd_signal": "macd", "macd_histogram": "macd", "stochastic": "stochastic",
                         "adx": "adx", "cci": "cci", "mfi": "mfi", "williams_r": "williams_r", "atr": "atr", "tsi": "tsi"}
    for indicator_id, source_id in parameter_sources.items():
        output[indicator_id]["parameters"] = dict(indicator_package.get(source_id, {}).get("parameters", {}))
    return output


def _threshold_band_confidence(value: float, thresholds: Sequence[tuple[float, str]], selected_index: int) -> float:
    selected_threshold = thresholds[selected_index][0]
    if selected_index == 0:
        neighbor_width = abs(thresholds[0][0] - thresholds[1][0]) if len(thresholds) > 1 else 1.0
        progress       = (value - selected_threshold) / max(neighbor_width, 1e-12)
    elif not math.isfinite(selected_threshold):
        upper_boundary = thresholds[selected_index - 1][0]
        neighbor_width = abs(thresholds[selected_index - 2][0] - upper_boundary) if selected_index > 1 else max(abs(upper_boundary), 1.0)
        progress       = (upper_boundary - value) / max(neighbor_width, 1e-12)
    else:
        upper_boundary = thresholds[selected_index - 1][0]
        band_width     = upper_boundary - selected_threshold
        if band_width <= 0:
            return 0.50
        distance_to_boundary = min(value - selected_threshold, upper_boundary - value)
        progress             = 2.0 * distance_to_boundary / band_width
    if not math.isfinite(progress):
        return 0.50
    return min(1.0, max(0.50, 0.50 + 0.50 * max(0.0, progress)))


def _state_by_thresholds(value: Any, thresholds: Sequence[tuple[float, str]], reason: str) -> dict[str, Any]:
    value = _finite(value)
    if value is None:
        return _missing()
    selected_index = len(thresholds) - 1
    state          = thresholds[-1][1]
    for index, (threshold, candidate) in enumerate(thresholds):
        if value >= threshold:
            selected_index = index
            state          = candidate
            break
    if state in {"strong", "good", "positive"}:
        signal = "positive"
    elif state in {"poor", "negative"}:
        signal = "negative"
    else:
        signal = "neutral"
    confidence = _threshold_band_confidence(value, thresholds, selected_index)
    return _classification(value, signal, state, reason, confidence, state if state in {"strong", "good", "poor"} else signal)


def classify_skewness(value: Any) -> dict[str, Any]:
    value = _finite(value)
    if value is None:
        return _missing()
    if value > 0.50:
        state = "positive"
    elif value < -0.50:
        state = "negative"
    else:
        state = "neutral"
    return _classification(value, state, state, "Skewness is compared with ±0.5", min(1, abs(value)))


def classify_kurtosis(value: Any, mode: str = "pearson") -> dict[str, Any]:
    value = _finite(value)
    if value is None:
        return _missing()
    if value < 2.50:
        state = "platykurtic"
    elif value <= 3.50:
        state = "mesokurtic"
    else:
        state = "leptokurtic"
    return _classification(value, "neutral", state, f"Kurtosis is interpreted in {mode} mode", min(1, abs(value - 3) / 3), "neutral", kurtosis_mode=mode)


def classify_z_score(value: Any) -> dict[str, Any]:
    value = _finite(value)
    if value is None:
        return _missing()
    if value > 2:
        state = "high_positive_deviation"
    elif value < -2:
        state = "high_negative_deviation"
    elif value > 1:
        state = "moderate_positive"
    elif value < -1:
        state = "moderate_negative"
    else:
        state = "neutral"
    if value > 1:
        signal = "positive"
    elif value < -1:
        signal = "negative"
    else:
        signal = "neutral"
    return _classification(value, signal, state, "Z-score measures the latest close deviation", min(1, abs(value) / 2))

def classify_var(value: Any) -> dict[str, Any]:
    value = _finite(value)
    if value is None:
        return _missing()
    magnitude = abs(value)
    if magnitude < 0.02:
        state = "low"
    elif magnitude < 0.05:
        state = "moderate"
    else:
        state = "high"
    return _classification(value, "neutral", state, "Historical VaR severity uses absolute return magnitude", min(1, magnitude / 0.05), state)

def classify_cvar(value: Any) -> dict[str, Any]:
    output           = classify_var(value)
    output["reason"] = "Historical CVaR severity uses absolute tail-return magnitude"
    return output

def classify_omega(value: Any) -> dict[str, Any]:
    return _state_by_thresholds(value, ((1.50, "strong"), (1.10, "good"), (0.90, "neutral"), (-math.inf, "poor")), "Omega ratio uses configured quality thresholds")

def classify_sharpe(value: Any) -> dict[str, Any]:
    return _state_by_thresholds(value, ((2, "strong"), (1, "good"), (0, "neutral"), (-math.inf, "poor")), "Sharpe ratio uses configured quality thresholds")

def classify_sortino(value: Any) -> dict[str, Any]:
    output           = classify_sharpe(value)
    output["reason"] = "Sortino ratio uses configured quality thresholds"
    return output

def classify_calmar(value: Any) -> dict[str, Any]:
    output           = classify_sharpe(value)
    output["reason"] = "Calmar ratio uses configured quality thresholds"
    return output

def classify_max_drawdown(value: Any) -> dict[str, Any]:
    value = _finite(value)
    if value is None:
        return _missing()
    magnitude = abs(value)
    if magnitude < 0.05:
        state = "low"
    elif magnitude < 0.10:
        state = "moderate"
    else:
        state = "high"
    return _classification(value, "negative", state, "Drawdown severity uses absolute return magnitude", min(1, magnitude / 0.10), state)

def classify_profit_factor(value: Any) -> dict[str, Any]:
    return _state_by_thresholds(value, ((2.0, "strong"), (1.20, "good"), (1.0, "neutral"), (-math.inf, "poor")), "Profit Factor uses market-return thresholds")

def classify_recovery_factor(value: Any) -> dict[str, Any]:
    return _state_by_thresholds(value, ((3.0, "strong"), (1.50, "good"), (0.0, "neutral"), (-math.inf, "poor")), "Recovery Factor uses configured thresholds")

def classify_win_rate(value: Any) -> dict[str, Any]:
    return _state_by_thresholds(value, ((0.60, "strong"), (0.55, "good"), (0.45, "neutral"), (-math.inf, "poor")), "Win rate describes positive market-return periods")

def classify_statistical_performance(package: Mapping[str, Any], timeframe: str) -> dict[str, Any]:
    descriptive      = package.get("descriptive", {})
    risk             = package.get("risk", {})
    performance      = package.get("performance", {})
    close_deviation  = _finite(descriptive.get("close_standard_deviation"))
    return_deviation = _finite(descriptive.get("return_standard_deviation"))
    thresholds       = PRICE_RETURN_VOLATILITY_THRESHOLDS[timeframe]
    if return_deviation is None:
        volatility_state = "unavailable"
    elif return_deviation < thresholds["low"]:
        volatility_state = "low"
    elif return_deviation >= thresholds["high"]:
        volatility_state = "high"
    else:
        volatility_state = "moderate"
    mean         = _descriptive_or_missing(descriptive.get("mean_close"), state="descriptive", reason="Mean close is descriptive")
    std_metadata = {"display_basis": "close", "classification_basis": "simple_returns", "timeframe": timeframe,
                    "low_threshold": thresholds["low"], "high_threshold": thresholds["high"]}
    if close_deviation is None and return_deviation is None:
        std = _missing("Close and return standard deviations are unavailable")
        std.update({"return_value": None, "metadata": std_metadata})
    else:
        std = _classification(close_deviation, "neutral", volatility_state,
                              "Volatility state uses return standard deviation while the display value preserves close standard deviation",
                              0.50, volatility_state if volatility_state != "unavailable" else "neutral",
                              return_value=return_deviation, metadata=std_metadata)
        if close_deviation is None or return_deviation is None:
            std["quality"] = "partial"
    wins   = _descriptive_or_missing(performance.get("max_consecutive_wins"), signal="positive", state="positive", color_token="positive",
                                     reason="Consecutive positive market-return periods")
    losses = _descriptive_or_missing(performance.get("max_consecutive_losses"), signal="negative", state="negative", color_token="negative",
                                     reason="Consecutive negative market-return periods")
    return {
        "mean": mean, "standard_deviation": std, "skewness": classify_skewness(descriptive.get("skewness")),
        "kurtosis": classify_kurtosis(descriptive.get("kurtosis"), descriptive.get("metadata", {}).get("kurtosis_mode", "pearson")),
        "z_score": classify_z_score(descriptive.get("z_score")),
        "var_95": {**classify_var(risk.get("var_95_return")), "price_value": _finite(risk.get("var_95_price"))},
        "cvar_95": {**classify_cvar(risk.get("cvar_95_return")), "price_value": _finite(risk.get("cvar_95_price"))},
        "max_consecutive_wins": wins, "max_consecutive_losses": losses,
        "omega_ratio": classify_omega(performance.get("omega_ratio")), "sharpe_ratio": classify_sharpe(performance.get("sharpe_ratio")),
        "sortino_ratio": classify_sortino(performance.get("sortino_ratio")), "calmar_ratio": classify_calmar(performance.get("calmar_ratio")),
        "max_drawdown": classify_max_drawdown(performance.get("max_drawdown")), "profit_factor": classify_profit_factor(performance.get("profit_factor")),
        "recovery_factor": classify_recovery_factor(performance.get("recovery_factor")), "win_rate": classify_win_rate(performance.get("win_rate")),
        "metadata": {"performance_basis": performance.get("performance_basis"), "periods_per_year": performance.get("metadata", {}).get("periods_per_year"),
                     "return_type": performance.get("metadata", {}).get("return_type", "simple")},
    }


def _component_tolerance(name: str, value: float) -> float:
    if name in {"rsi_centered"}:
        return 5.0
    if name in {"cci"}:
        return 100.0
    if name in {"mfi_centered", "williams_r_centered"}:
        return 10.0
    if name == "tsi":
        return TSI_NEUTRAL_TOLERANCE
    return max(abs(value), 1.0) * 0.001

def classify_bias_component(name: str, value: Any) -> dict[str, Any]:
    value = _finite(value)
    if value is None:
        return {"value": None, "vote": 0, "available": False}
    tolerance = _component_tolerance(name, value)
    if value > tolerance:
        vote = 1
    elif value < -tolerance:
        vote = -1
    else:
        vote = 0
    return {"value": value, "vote": vote, "available": True, "tolerance": tolerance, "weight": BIAS_COMPONENT_WEIGHTS.get(name, 0.0)}

def _score_label(score: float | None) -> str:
    if score is None:
        return "neutral"
    if score >= BIAS_STRONG_THRESHOLD:
        return "strong_bullish"
    if score > BIAS_NEUTRAL_TOLERANCE:
        return "bullish"
    if score <= -BIAS_STRONG_THRESHOLD:
        return "strong_bearish"
    if score < -BIAS_NEUTRAL_TOLERANCE:
        return "bearish"
    return "neutral"

def calculate_timeframe_bias(components: Mapping[str, Any], timeframe: str | None = None) -> dict[str, Any]:
    values          = components.get("values", components)
    component_votes = {name: classify_bias_component(name, values.get(name)) for name in BIAS_COMPONENT_WEIGHTS}
    available       = {name: item for name, item in component_votes.items() if item["available"]}
    total_weight    = sum(BIAS_COMPONENT_WEIGHTS[name] for name in available)
    weighted_sum    = sum(item["vote"] * BIAS_COMPONENT_WEIGHTS[name] for name, item in available.items())
    score           = weighted_sum / total_weight if total_weight else None
    adx             = _finite(values.get("adx"))
    if adx is None or adx < 20:
        adx_multiplier = 0.50
    elif adx < 25:
        adx_multiplier = 0.75
    elif adx < 40:
        adx_multiplier = 1.0
    else:
        adx_multiplier = 1.15
    confidence      = min(1.0, (abs(score) if score is not None else 0) * adx_multiplier)
    return {"score": score, "label": _score_label(score), "confidence": confidence, "timeframes_used": [timeframe] if timeframe else [],
            "component_votes": component_votes, "missing_components": [name for name, item in component_votes.items() if not item["available"]],
            "metadata": {"adx_confidence_multiplier": adx_multiplier, "atr_directional_weight": 0.0}}

def calculate_group_bias(timeframe_biases: Mapping[str, Mapping[str, Any]], weights: Mapping[str, float]) -> dict[str, Any]:
    available    = [(timeframe, item, weights[timeframe]) for timeframe, item in timeframe_biases.items() if timeframe in weights and item.get("score") is not None]
    total_weight = sum(weight for _, _, weight in available)
    score        = sum(item["score"] * weight for _, item, weight in available) / total_weight if total_weight else None
    confidence   = sum(item.get("confidence", 0) * weight for _, item, weight in available) / total_weight if total_weight else 0.0
    return {"score": score, "label": _score_label(score), "confidence": confidence, "timeframes_used": [timeframe for timeframe, _, _ in available],
            "component_votes": {timeframe: item.get("component_votes", {}) for timeframe, item, _ in available}, "missing_components": []}

def calculate_overall_bias(groups: Mapping[str, Mapping[str, Any]], micro: Mapping[str, Any] | None = None) -> dict[str, Any]:
    available    = [(name, groups[name], weight) for name, weight in OVERALL_GROUP_WEIGHTS.items() if groups.get(name, {}).get("score") is not None]
    total_weight = sum(weight for _, _, weight in available)
    score        = sum(item["score"] * weight for _, item, weight in available) / total_weight if total_weight else None
    confidence   = sum(item.get("confidence", 0) * weight for _, item, weight in available) / total_weight if total_weight else 0.0
    short_score  = groups.get("short", {}).get("score")
    micro_score  = (micro or {}).get("score")
    if short_score is not None and micro_score is not None and short_score * micro_score != 0:
        confidence *= 1.10 if short_score * micro_score > 0 else 0.90
    return {"score": score, "label": _score_label(score), "confidence": min(1, confidence), "timeframes_used": [name for name, _, _ in available],
            "component_votes": {}, "missing_components": [], "metadata": {"group_weights": dict(OVERALL_GROUP_WEIGHTS), "micro_affects_score": False}}

def calculate_market_biases(bias_components: Mapping[str, Any]) -> dict[str, Any]:
    output = {}
    for market in MARKET_ORDER:
        source     = bias_components.get(market, {})
        timeframes = source.get("timeframes", source)
        tf_biases  = {timeframe: calculate_timeframe_bias(timeframes.get(timeframe, {}), timeframe) for timeframe in TIMEFRAME_ORDER}
        micro      = tf_biases["1m"]
        short      = calculate_group_bias(tf_biases, SHORT_TIMEFRAME_WEIGHTS)
        mid        = calculate_group_bias(tf_biases, MID_TIMEFRAME_WEIGHTS)
        long       = calculate_group_bias(tf_biases, LONG_TIMEFRAME_WEIGHTS)
        output[market] = {"micro": micro, "short": short, "mid": mid, "long": long,
                          "overall": calculate_overall_bias({"short": short, "mid": mid, "long": long}, micro), "timeframes": tf_biases}
    return output

def classify_basis(comparison: Mapping[str, Any]) -> dict[str, Any]:
    percent = _finite(comparison.get("basis_percent"))
    if percent is None:
        state = "unavailable"
    elif percent > BASIS_NEUTRAL_PERCENT:
        state = "premium"
    elif percent < -BASIS_NEUTRAL_PERCENT:
        state = "discount"
    else:
        state = "aligned"
    return {"basis_usd": _finite(comparison.get("basis_usd")), "basis_percent": percent, "state": state,
            "reason": "Basis percent is compared with the configured neutral band"}

def _direction(label: str | None) -> int:
    if label in {"bullish", "strong_bullish"}:
        return 1
    if label in {"bearish", "strong_bearish"}:
        return -1
    return 0

def classify_market_agreement(biases: Mapping[str, Any]) -> dict[str, Any]:
    """Classify direct Spot/Futures agreement without constructing a third market."""
    directions = {market: _direction(biases.get(market, {}).get("overall", {}).get("label")) for market in ("spot", "futures")}
    spot, futures = directions["spot"], directions["futures"]
    if spot == futures and spot != 0:
        state = "confirmed"
    elif spot != 0 and futures != 0 and spot == -futures:
        state = "divergent"
    elif spot == futures == 0:
        state = "neutral"
    else:
        state = "mixed"
    return {"state": state, "directions": directions, "basis": "spot_futures_bias_direction"}

def classify_market_leadership(biases: Mapping[str, Any]) -> dict[str, Any]:
    spot       = _finite(biases.get("spot", {}).get("overall", {}).get("score"))
    futures    = _finite(biases.get("futures", {}).get("overall", {}).get("score"))
    difference = None if spot is None or futures is None else futures - spot
    if difference is None or abs(difference) <= LEADERSHIP_SCORE_DIFFERENCE:
        state = "balanced"
    elif difference > 0:
        state = "derivatives_led"
    else:
        state = "spot_led"
    return {"state": state, "spot_score": spot, "futures_score": futures, "score_difference": difference,
            "metadata": {"method": "bias_score_difference", "threshold": LEADERSHIP_SCORE_DIFFERENCE}}

def classify_prices_market_relationship(comparison: Mapping[str, Any], biases: Mapping[str, Any], timeframe: str = "15m") -> dict[str, Any]:
    current = comparison.get("by_timeframe", comparison).get(timeframe, {}).get("current", {})
    return {"basis": classify_basis(current), "agreement": classify_market_agreement(biases),
            "leadership": classify_market_leadership(biases), "timeframe": timeframe}

def classify_technical_crosses(crosses: Mapping[str, Any]) -> dict[str, Any]:
    output = {market: {timeframe: [] for timeframe in TIMEFRAME_ORDER} for market in MARKET_ORDER}
    for market in MARKET_ORDER:
        for timeframe in TIMEFRAME_ORDER:
            for event in crosses.get(market, {}).get(timeframe, []):
                raw_direction = _finite(event.get("direction"))
                direction     = int(raw_direction) if raw_direction in {-1.0, 1.0} else 0
                if direction == 1:
                    signal = "bullish"
                    marker = "arrow_up"
                elif direction == -1:
                    signal = "bearish"
                    marker = "arrow_down"
                else:
                    signal = "neutral"
                    marker = "dot"
                cross_id  = str(event.get("cross_id", "unknown_cross"))
                item = {"timestamp": event.get("timestamp"), "event_id": cross_id, "event_type": "technical_cross",
                        "signal": signal, "label": CROSS_LABELS.get(cross_id, cross_id.replace("_", " ").title()),
                        "marker": marker, "color_token": signal, "source": {"market": market, "timeframe": timeframe},
                        "calculation": {"first_series": event.get("first_series"), "second_series": event.get("second_series"),
                                        "previous_difference": _finite(event.get("previous_difference")),
                                        "current_difference": _finite(event.get("current_difference")),
                                        "first_value": _finite(event.get("first_value")),
                                        "second_value": _finite(event.get("second_value")),
                                        "previous_first_value": _finite(event.get("previous_first_value")),
                                        "previous_second_value": _finite(event.get("previous_second_value")),
                                        "interpolation_fraction": _finite(event.get("interpolation_fraction")),
                                        "event_value_exact": _finite(event.get("event_value_exact")),
                                        "raw_direction": raw_direction}}
                if event.get("event_timestamp_exact") is not None:
                    item["event_timestamp_exact"] = _finite(event.get("event_timestamp_exact"))
                    item["event_value_exact"] = _finite(event.get("event_value_exact"))
                    item["event_price"] = _finite(event.get("event_price", event.get("event_value_exact")))
                output[market][timeframe].append(item)
    return output

def classify_candlestick_patterns(patterns: Mapping[str, Any]) -> dict[str, Any]:
    output = {market: {timeframe: [] for timeframe in TIMEFRAME_ORDER} for market in MARKET_ORDER}
    for market in MARKET_ORDER:
        for timeframe in TIMEFRAME_ORDER:
            for event in patterns.get(market, {}).get(timeframe, []):
                raw_direction = _finite(event.get("direction"))
                direction     = int(raw_direction) if raw_direction in {-1.0, 1.0} else 0
                if direction == 1:
                    signal = "bullish"
                elif direction == -1:
                    signal = "bearish"
                else:
                    signal = "neutral"
                confidence = min(1, max(0, _finite(event.get("confidence")) or 0))
                if confidence >= 0.80:
                    level = "high"
                elif confidence >= 0.60:
                    level = "medium"
                else:
                    level = "low"
                pattern_id = str(event.get("pattern_id", "unknown_pattern"))
                output[market][timeframe].append({"timestamp": event.get("timestamp"), "event_id": pattern_id, "event_type": "candlestick_pattern",
                                                   "signal": signal, "label": PATTERN_LABELS.get(pattern_id, pattern_id.replace("_", " ").title()),
                                                   "color_token": signal, "confidence": confidence, "confidence_level": level,
                                                   "source": {"market": market, "timeframe": timeframe},
                                                   "calculation": {"components": _sanitize_json_value(event.get("components", {})),
                                                                   "raw_direction": raw_direction}})
    return output

def _classify_all_indicators(features: Mapping[str, Any]) -> dict[str, Any]:
    indicators = features.get("indicators", {})
    biases     = features.get("bias_components", {})
    return {market: {timeframe: classify_indicator_package(indicators.get(market, {}).get(timeframe, {}),
                                                            biases.get(market, {}).get("timeframes", {}).get(timeframe, {}))
                     for timeframe in TIMEFRAME_ORDER} for market in MARKET_ORDER}

def _classify_all_statistics(features: Mapping[str, Any]) -> dict[str, Any]:
    source = features.get("statistical_performance", {}).get("markets", {})
    return {market: {timeframe: classify_statistical_performance(source.get(market, {}).get(timeframe, {}), timeframe)
                     for timeframe in TIMEFRAME_ORDER} for market in MARKET_ORDER}


def evaluate_prices_classification_quality(*, processing_features: Mapping[str, Any], indicator_signals: Mapping[str, Any],
                                           statistical_signals: Mapping[str, Any], technical_bias: Mapping[str, Any],
                                           market_relationship: Mapping[str, Any], events: Mapping[str, Any]) -> dict[str, Any]:
    missing_fields: list[str] = []
    warnings: list[str]       = []
    errors: list[str]         = []
    for market in MARKET_ORDER:
        if market not in indicator_signals or market not in statistical_signals or market not in technical_bias:
            errors.append(f"market.{market}")
            continue
        for timeframe in TIMEFRAME_ORDER:
            indicator_package   = indicator_signals[market].get(timeframe)
            statistical_package = statistical_signals[market].get(timeframe)
            if indicator_package is None:
                errors.append(f"indicator_signals.{market}.{timeframe}")
            else:
                for indicator_id in REQUIRED_INDICATOR_SIGNALS:
                    if indicator_id not in indicator_package:
                        missing_fields.append(f"indicator_signals.{market}.{timeframe}.{indicator_id}")
                tsi_parameters = indicator_package.get("tsi", {}).get("parameters", {})
                slow_period    = tsi_parameters.get("slow_period", tsi_parameters.get("long_period"))
                fast_period    = tsi_parameters.get("fast_period", tsi_parameters.get("short_period"))
                if slow_period != 25 or fast_period != 13:
                    missing_fields.append(f"indicator_signals.{market}.{timeframe}.tsi.parameters")
            if statistical_package is None:
                errors.append(f"statistical_signals.{market}.{timeframe}")
            else:
                for metric_id in REQUIRED_STATISTICAL_SIGNALS:
                    if metric_id not in statistical_package:
                        missing_fields.append(f"statistical_signals.{market}.{timeframe}.{metric_id}")
                if statistical_package.get("metadata", {}).get("performance_basis") != "market_returns":
                    missing_fields.append(f"statistical_signals.{market}.{timeframe}.metadata.performance_basis")
        for bias_id in ("micro", "short", "mid", "long", "overall", "timeframes"):
            if bias_id not in technical_bias[market]:
                missing_fields.append(f"technical_bias.{market}.{bias_id}")
    for field in ("basis", "agreement", "leadership", "timeframe"):
        if field not in market_relationship:
            missing_fields.append(f"market_relationship.{field}")
    for event_type in ("technical_crosses", "candlestick_patterns"):
        event_markets = events.get(event_type, {})
        for market in MARKET_ORDER:
            for timeframe in TIMEFRAME_ORDER:
                for index, event in enumerate(event_markets.get(market, {}).get(timeframe, [])):
                    source = event.get("source", {})
                    if source.get("market") != market or source.get("timeframe") != timeframe:
                        missing_fields.append(f"events.{event_type}.{market}.{timeframe}.{index}.source")
    candidate = {"processing_features": processing_features, "indicator_signals": indicator_signals,
                 "statistical_signals": statistical_signals, "technical_bias": technical_bias,
                 "market_relationship": market_relationship, "events": events}
    try:
        json.dumps(_sanitize_json_value(candidate), allow_nan=False)
    except (TypeError, ValueError) as exc:
        errors.append(f"serialization: {exc}")
    if errors:
        status = "invalid"
    elif missing_fields or warnings:
        status = "partial"
    else:
        status = "ok"
    return {"status": status, "is_complete": status == "ok", "missing_fields": sorted(set(missing_fields)),
            "warnings": warnings, "errors": sorted(set(errors))}

def run_prices_ohlcv_classification(processing_output: Mapping[str, Any]) -> dict[str, Any]:
    if processing_output.get("family") != "prices_ohlcv":
        raise ValueError("Prices classifier requires family=prices_ohlcv")
    if processing_output.get("stage") != "processing":
        raise ValueError("Prices classifier requires stage=processing")
    features     = processing_output.get("features", {})
    indicators   = _classify_all_indicators(features)
    statistics   = _classify_all_statistics(features)
    biases       = calculate_market_biases(features.get("bias_components", {}))
    relationship = classify_prices_market_relationship(features.get("spot_futures_comparison", {}), biases)
    crosses      = classify_technical_crosses(features.get("technical_crosses", {}))
    patterns     = classify_candlestick_patterns(features.get("candlestick_patterns", {}))
    events       = {"technical_crosses": crosses, "candlestick_patterns": patterns}
    quality      = evaluate_prices_classification_quality(processing_features=features, indicator_signals=indicators,
                                                          statistical_signals=statistics, technical_bias=biases,
                                                          market_relationship=relationship, events=events)
    output = {"family": "prices_ohlcv", "stage": "classification", "mode": processing_output.get("mode"),
              "indicator_signals": indicators, "statistical_signals": statistics, "technical_bias": biases,
              "market_relationship": relationship, "events": events, "quality": quality}
    output = _sanitize_json_value(output)
    json.dumps(output, allow_nan=False)
    return output
