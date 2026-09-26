"""Market-agnostic primitives for future TradELATIN VR1 extensions.

This package is intentionally not wired into the current BTC runtime.
"""

from .adapters import AdapterRequest, ProviderAdapter
from .models import (
    CapabilityState,
    CanonicalMarketEvent,
    DerivationType,
    MarketClass,
    MarketContext,
    ObservationQuality,
    ObservableEnvelope,
    SourceMode,
    SourceStatus,
)
from .contracts import to_vr1_observation_v1
from .registry import (
    EQUITIES_OBSERVABLES,
    FROZEN_CRYPTO_ENDPOINT_COUNT,
    get_equities_observable,
    list_equities_observables,
)

__all__ = [
    "AdapterRequest",
    "CapabilityState",
    "CanonicalMarketEvent",
    "DerivationType",
    "EQUITIES_OBSERVABLES",
    "FROZEN_CRYPTO_ENDPOINT_COUNT",
    "MarketClass",
    "MarketContext",
    "ObservationQuality",
    "ObservableEnvelope",
    "ProviderAdapter",
    "SourceMode",
    "SourceStatus",
    "get_equities_observable",
    "list_equities_observables",
    "to_vr1_observation_v1",
]
