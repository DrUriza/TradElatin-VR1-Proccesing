"""Provider-adapter boundary for market-specific acquisition.

No concrete IBKR implementation exists here.  The protocol keeps future provider
code outside the current BTC acquisition module and makes read-only events the
only accepted adapter output.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Protocol, runtime_checkable

from .models import CanonicalMarketEvent, MarketContext


@dataclass(frozen=True)
class AdapterRequest:
    logical_surface: str
    context: MarketContext
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        surface = str(self.logical_surface).strip()
        if not surface:
            raise ValueError("logical_surface_must_not_be_empty")
        object.__setattr__(self, "logical_surface", surface)
        if not isinstance(self.parameters, Mapping):
            raise TypeError("parameters_must_be_mapping")
        object.__setattr__(
            self, "parameters", MappingProxyType(dict(self.parameters))
        )


@runtime_checkable
class ProviderAdapter(Protocol):
    """Read-only source adapter; account, order and execution APIs are excluded."""

    @property
    def provider_id(self) -> str:
        ...

    def capabilities(self, context: MarketContext) -> Mapping[str, bool]:
        ...

    def acquire(self, request: AdapterRequest) -> Iterable[CanonicalMarketEvent]:
        ...

