"""Isolated registry for proposed Equities observables.

Nothing in the current Input, Processing, Classification or runtime imports this
module.  The registry is deliberately separate from acquisition.ALLOWED_ENDPOINTS.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping

from .models import CapabilityState, DerivationType

FROZEN_CRYPTO_ENDPOINT_COUNT = 33
PROPOSED_EQUITIES_STATUS = "PROPOSED EQUITIES OBSERVABLE — NOT IMPLEMENTED"


@dataclass(frozen=True)
class ObservableDefinition:
    observable_id: str
    family: str
    derivation_type: DerivationType
    capability: CapabilityState
    intended_source: str
    description: str
    semantic_restriction: str

    def __post_init__(self) -> None:
        if not self.observable_id.startswith("eq_c"):
            raise ValueError(f"invalid_equities_observable_id:{self.observable_id}")
        if self.family not in {"C1", "C2", "C3", "C6", "C8"}:
            raise ValueError(f"unsupported_equities_family:{self.family}")
        for name in ("intended_source", "description", "semantic_restriction"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name}_must_not_be_empty")


def _definition(
    observable_id: str,
    family: str,
    kind: DerivationType,
    capability: CapabilityState,
    source: str,
    description: str,
    restriction: str,
) -> ObservableDefinition:
    return ObservableDefinition(
        observable_id, family, kind, capability, source, description, restriction
    )


_DIRECT = CapabilityState.COMPLETE
_PARTIAL = CapabilityState.PARTIAL
_RAW = DerivationType.RAW
_DERIVED = DerivationType.DERIVED
_INFERRED = DerivationType.INFERRED

_DEFINITIONS = (
    # C1 — Prices
    _definition("eq_c1_trade_price", "C1", _RAW, _DIRECT, "IBKR tick-by-tick/Level I", "Execution or last price.", "Preserve source granularity."),
    _definition("eq_c1_best_quote", "C1", _RAW, _DIRECT, "IBKR Level I", "Reported best bid and ask.", "Preserve routing, data type and staleness."),
    _definition("eq_c1_ohlcv_bar", "C1", _RAW, _DIRECT, "IBKR historical/realtime bars", "OHLCV for an explicit interval.", "Declare session and adjustment policy."),
    _definition("eq_c1_previous_close", "C1", _RAW, _DIRECT, "IBKR historical/Level I", "Provider previous close.", "Declare session and adjustment basis."),
    _definition("eq_c1_wap", "C1", _RAW, _DIRECT, "IBKR realtime bars", "Provider weighted average price for a bar.", "Do not relabel as session VWAP."),
    _definition("eq_c1_reference_quote", "C1", _RAW, _PARTIAL, "IBKR ES/NQ/VIX", "Separate reference-instrument quote.", "Context is not causality."),
    _definition("eq_c1_midpoint", "C1", _DERIVED, _DIRECT, "Valid best quote", "Bid/ask midpoint.", "Suppress invalid or crossed quotes."),
    _definition("eq_c1_return", "C1", _DERIVED, _DIRECT, "Versioned price series", "Return over an explicit window.", "Declare basis, window and missing-data policy."),
    # C2 — Order flow
    _definition("eq_c2_trade_size", "C2", _RAW, _PARTIAL, "IBKR tick-by-tick", "Reported executed size.", "Preserve feed coverage and units."),
    _definition("eq_c2_trade_count", "C2", _RAW, _PARTIAL, "IBKR realtime bars", "Provider trade count per bar.", "Does not reconstruct trade sequence."),
    _definition("eq_c2_volume_rate", "C2", _DERIVED, _PARTIAL, "Trades or bars", "Executed volume per unit time.", "Declare input granularity and gaps."),
    _definition("eq_c2_trade_rate", "C2", _DERIVED, _PARTIAL, "Trade events/counts", "Trades per unit time.", "Do not mix bar and tick counts silently."),
    _definition("eq_c2_estimated_aggressor_volume", "C2", _INFERRED, _PARTIAL, "Time & Sales plus quotes", "Estimated BUY/SELL/UNKNOWN volume.", "Quote-test estimate, not IBKR fact."),
    _definition("eq_c2_estimated_cvd", "C2", _INFERRED, _PARTIAL, "Estimated aggressor volume", "Estimated cumulative volume delta.", "Publish coverage and unknown volume."),
    _definition("eq_c2_classification_coverage", "C2", _DERIVED, _PARTIAL, "Aggressor classifications", "Fraction classified BUY or SELL.", "Quality fact, not model confidence."),
    _definition("eq_c2_large_trade_activity", "C2", _DERIVED, _PARTIAL, "Trade-size distribution", "Trades above a versioned threshold.", "No participant identity or intent."),
    # C3 — partial only
    _definition("eq_c3_shortable_shares", "C3", _RAW, _PARTIAL, "IBKR generic tick 236", "Indicative shortable shares.", "Not OI, borrow fee, locate or positioning."),
    # C6 — Volatility
    _definition("eq_c6_realized_volatility", "C6", _DERIVED, _PARTIAL, "Trade/OHLC returns", "Realized variability by estimator/window.", "No structural regime label."),
    _definition("eq_c6_intraday_range", "C6", _DERIVED, _PARTIAL, "Intraday OHLC", "High-low range.", "Declare interval, session and gaps."),
    _definition("eq_c6_gap", "C6", _DERIVED, _PARTIAL, "Previous close and session open", "Session price gap.", "Adjustment policy required."),
    _definition("eq_c6_vix_reference", "C6", _RAW, _PARTIAL, "IBKR VIX market data", "Observed VIX reference.", "Separate instrument, not asset realized volatility."),
    # C8 — Liquidity microstructure
    _definition("eq_c8_top_of_book_size", "C8", _RAW, _DIRECT, "IBKR Level I", "Displayed best bid/ask size.", "Venue and units required."),
    _definition("eq_c8_depth_level", "C8", _RAW, _PARTIAL, "IBKR Market Depth", "Visible price/size/side level.", "Handle operations, resets and sequence integrity."),
    _definition("eq_c8_spread", "C8", _DERIVED, _DIRECT, "Valid best quote", "Absolute and bps spread.", "Suppress stale/crossed/mismatched quotes."),
    _definition("eq_c8_top_imbalance", "C8", _DERIVED, _DIRECT, "Best sizes", "Top-of-book imbalance.", "Version formula and missing-size behavior."),
    _definition("eq_c8_microprice", "C8", _DERIVED, _DIRECT, "Best prices/sizes", "Size-weighted top price.", "Descriptive, not forecast or fair value."),
    _definition("eq_c8_visible_bid_depth", "C8", _DERIVED, _PARTIAL, "Reconstructed bid book", "Displayed bid depth by scope.", "Publish levels, completeness and venue."),
    _definition("eq_c8_visible_ask_depth", "C8", _DERIVED, _PARTIAL, "Reconstructed ask book", "Displayed ask depth by scope.", "Publish levels, completeness and venue."),
    _definition("eq_c8_depth_imbalance", "C8", _DERIVED, _PARTIAL, "Visible bid/ask depth", "Relative depth imbalance.", "Matched scopes and valid snapshot required."),
    _definition("eq_c8_depth_concentration", "C8", _DERIVED, _PARTIAL, "Depth levels", "Concentration across levels.", "Publish formula and book scope."),
    _definition("eq_c8_displayed_liquidity_change", "C8", _DERIVED, _PARTIAL, "Depth event history", "Added/removed displayed size.", "Removal is not necessarily cancellation/execution."),
    _definition("eq_c8_large_visible_order", "C8", _DERIVED, _PARTIAL, "Depth-size distribution", "Visible size above threshold.", "No whale/institution/intent claim."),
    _definition("eq_c8_visible_liquidity_persistence", "C8", _DERIVED, _PARTIAL, "Depth history", "Duration/recurrence of visible liquidity.", "Does not prove one participant."),
    _definition("eq_c8_time_sales", "C8", _RAW, _PARTIAL, "IBKR tick-by-tick", "Normalized execution sequence.", "Preserve coverage and event/receipt times."),
    _definition("eq_c8_large_liquidity_activity", "C8", _INFERRED, _PARTIAL, "Trades plus depth changes", "Concurrence of large executed/displayed activity.", "No identity, intent, absorption or causality."),
    _definition("eq_c8_observable_sweep_behavior", "C8", _INFERRED, _PARTIAL, "Trades, quotes and depth", "Rapid executions across visible levels.", "No LONG/SHORT or structural state."),
    _definition("eq_c8_depth_tape_alignment", "C8", _INFERRED, _PARTIAL, "Depth plus classified tape", "Quantitative alignment or conflict.", "Not confirmation or causality."),
)

EQUITIES_OBSERVABLES: Mapping[str, ObservableDefinition] = MappingProxyType(
    {definition.observable_id: definition for definition in _DEFINITIONS}
)
if len(EQUITIES_OBSERVABLES) != len(_DEFINITIONS):
    raise RuntimeError("duplicate_equities_observable_id")


def get_equities_observable(observable_id: str) -> ObservableDefinition:
    try:
        return EQUITIES_OBSERVABLES[observable_id]
    except KeyError as exc:
        raise KeyError(f"unknown_equities_observable:{observable_id}") from exc


def list_equities_observables(
    *, family: str | None = None
) -> tuple[ObservableDefinition, ...]:
    values: Iterable[ObservableDefinition] = EQUITIES_OBSERVABLES.values()
    if family is not None:
        normalized = str(family).strip().upper()
        values = (item for item in values if item.family == normalized)
    return tuple(values)

