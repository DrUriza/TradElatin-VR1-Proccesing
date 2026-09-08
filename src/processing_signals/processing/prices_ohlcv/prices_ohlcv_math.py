"""Family bridge to shared mathematical algorithms.

This module is the only place in this family allowed to import from ``processing.math``.
"""

from __future__ import annotations

from processing_signals.processing.math.indicators.market_structure.fibonacci_levels import fibonacci_levels
from processing_signals.processing.math.indicators.momentum.cci import cci
from processing_signals.processing.math.indicators.momentum.rsi import rsi
from processing_signals.processing.math.indicators.momentum.stochastic import stochastic
from processing_signals.processing.math.indicators.momentum.tsi import tsi
from processing_signals.processing.math.indicators.momentum.williams_r import williams_r
from processing_signals.processing.math.indicators.trend.adx import adx
from processing_signals.processing.math.indicators.trend.macd import macd
from processing_signals.processing.math.indicators.trend.moving_averages import ema, sma, wma
from processing_signals.processing.math.indicators.volatility.atr import atr
from processing_signals.processing.math.indicators.volatility.bollinger_bands import bollinger_bands
from processing_signals.processing.math.indicators.volume.mfi import mfi
from processing_signals.processing.math.technical_cross_signals import detect_cross_pairs
from processing_signals.processing.math.native_analysis import interpolated_cross, rolling_wasserstein, support_resistance_levels, finite
from processing_signals.processing.math.patterns import detect_candlestick_patterns
from processing_signals.processing.math.statistics.descriptive_statistics import calculate_kurtosis, calculate_mean, calculate_skewness, calculate_standard_deviation, calculate_z_score
from processing_signals.processing.math.statistics.risk_metrics import calculate_historical_cvar, calculate_historical_var
from processing_signals.processing.math.statistics.return_performance import calculate_calmar_ratio, calculate_equity_curve, calculate_max_consecutive_losses, calculate_max_consecutive_wins, calculate_max_drawdown, calculate_omega_ratio, calculate_profit_factor, calculate_recovery_factor, calculate_sharpe_ratio, calculate_simple_returns, calculate_sortino_ratio, calculate_win_rate
