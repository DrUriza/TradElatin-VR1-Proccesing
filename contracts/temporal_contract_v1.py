"""TradELATIN Temporal Contract v1.0.0 — Input/Processing reference validator.

Pure Python 3.10+, no third-party dependencies. Keeps the frozen C1–C8
runtime unchanged. Input adapters and Processing builders must explicitly
invoke validate_selection before integrating this into production pipelines.
Source: DrUriza/TradElatinVR_Screen/src/contracts/temporalContract.js
"""
from __future__ import annotations

from types import MappingProxyType
from typing import Any

VERSION = "1.0.0"
BASE_CANDLE_TIMEFRAMES = ("1m", "5m", "15m", "1H", "4H", "1D")
WINDOW_DAYS = MappingProxyType({"7D": 7, "30D": 30, "90D": 90})
_MATRIX = {
    "vr1": {
        "c1": (BASE_CANDLE_TIMEFRAMES, "4H", "CANDLE"),
        "c2": (BASE_CANDLE_TIMEFRAMES, "4H", "CANDLE"),
        "c3": (("5m", "15m", "1H", "4H", "1D"), "1H", "CANDLE"),
        "c4": (("1H", "1D", "7D", "30D"), "1D", "HYBRID"),
        "c5": (("1D", "7D", "30D", "90D"), "1D", "WINDOW"),
        "c6": (("1H", "4H", "1D"), "4H", "CANDLE"),
        "c7": (BASE_CANDLE_TIMEFRAMES, "1H", "CANDLE"),
        "c8": (BASE_CANDLE_TIMEFRAMES, "1m", "CANDLE"),
    },
    "vr2": {"default": (("5m", "15m", "1H", "4H", "1D"), "1H", "CANDLE")},
    "vr3": {"default": (("15m", "1H", "4H", "1D"), "1H", "CANDLE")},
    "vr4": {"default": (BASE_CANDLE_TIMEFRAMES, "4H", "CANDLE")},
    "vr5": {"default": (("1H", "4H", "1D"), "4H", "CANDLE")},
}

def profile(vr: str, family: str = "c1") -> tuple[tuple[str, ...], str, str]:
    """Return the approved options, family default and profile type."""
    if vr not in _MATRIX:
        raise ValueError(f"Unsupported VR: {vr}")
    key = family if vr == "vr1" else "default"
    if key not in _MATRIX[vr]:
        raise ValueError(f"Unsupported family for {vr}: {family}")
    return _MATRIX[vr][key]

def resolve(vr: str, family: str, selected: str | None = None) -> dict[str, Any]:
    """Resolve selection into source candle timeframe and independent window."""
    choices, default, _ = profile(vr, family)
    choice = default if selected is None else selected
    if choice not in choices:
        raise ValueError(f"Unsupported timeframe {choice!r} for {vr}/{family}")
    window = WINDOW_DAYS.get(choice)
    return {
        "contractVersion": VERSION,
        "vr": vr,
        "family": family if vr == "vr1" else "default",
        "selection": choice,
        "kind": "WINDOW" if window else "CANDLE",
        "sourceTimeframe": "1D" if window else choice,
        "windowDays": window,
        "etfObservationCadence": "SESSION" if vr == "vr1" and family == "c4" else None,
        "exchangeFlowCadence": "1H" if vr == "vr1" and family == "c4" else None,
        "options": list(choices),
        "label": (
            "ETF por sesión · Exchange 1H/1D" if vr == "vr1" and family == "c4"
            else "On-chain · ventana agregada" if vr == "vr1" and family == "c5"
            else "RV/IV con ventanas independientes" if vr == "vr1" and family == "c6"
            else "Estructura: ventanas de análisis independientes" if vr == "vr2"
            else "Predicción: horizonte independiente" if vr == "vr3"
            else "Ejecución sujeta a estrategia y autorización" if vr == "vr4"
            else "Agentic: monitoreo por eventos" if vr == "vr5"
            else None
        ),
    }

def validate_selection(packet: dict[str, Any]) -> dict[str, Any]:
    """Strict Input→Processing→Screen boundary validation (reject invalid data)."""
    if not isinstance(packet, dict) or packet.get("contractVersion") != VERSION:
        raise ValueError("Temporal contract version mismatch")
    expected = resolve(packet.get("vr"), packet.get("family"), packet.get("selection"))
    for key in ("sourceTimeframe", "windowDays", "kind", "etfObservationCadence", "exchangeFlowCadence"):
        if packet.get(key) != expected[key]:
            raise ValueError(f"Invalid temporal field: {key}")
    return expected
