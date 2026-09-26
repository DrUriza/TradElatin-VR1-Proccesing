"""Canonical multi-market types.

The types in this module are structural building blocks.  They do not alter the
current C1-C8 runtime contracts and do not activate an Equities provider.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


class MarketClass(str, Enum):
    CRYPTO = "CRYPTO"
    EQUITIES = "EQUITIES"


class DerivationType(str, Enum):
    RAW = "RAW"
    DERIVED = "DERIVED"
    INFERRED = "INFERRED"


class CapabilityState(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNSUPPORTED = "UNSUPPORTED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class SourceStatus(str, Enum):
    LIVE = "LIVE"
    FROZEN = "FROZEN"
    DELAYED = "DELAYED"
    DELAYED_FROZEN = "DELAYED_FROZEN"
    REPLAY = "REPLAY"
    UNAVAILABLE = "UNAVAILABLE"


class SourceMode(str, Enum):
    LIVE = "LIVE"
    SYNTHETIC = "SYNTHETIC"
    REPLAY = "REPLAY"


class ObservationQuality(str, Enum):
    VALID = "VALID"
    PARTIAL = "PARTIAL"
    STALE = "STALE"
    INVALID = "INVALID"
    UNKNOWN = "UNKNOWN"


def _required_text(value: str, field_name: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{field_name}_must_not_be_empty")
    return normalized


def _immutable_mapping(value: Mapping[str, Any], field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name}_must_be_mapping")
    return MappingProxyType(dict(value))


@dataclass(frozen=True)
class MarketContext:
    market_class: MarketClass
    asset_id: str
    symbol: str
    asset_class: str
    venue: str
    provider: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "asset_id", _required_text(self.asset_id, "asset_id"))
        object.__setattr__(self, "symbol", _required_text(self.symbol, "symbol"))
        object.__setattr__(self, "asset_class", _required_text(self.asset_class, "asset_class"))
        object.__setattr__(self, "venue", _required_text(self.venue, "venue"))
        object.__setattr__(self, "provider", _required_text(self.provider, "provider").lower())


@dataclass(frozen=True)
class CanonicalMarketEvent:
    """Normalized provider fact before family-specific calculation."""

    event_type: str
    context: MarketContext
    event_timestamp: int
    received_timestamp: int
    source_status: SourceStatus
    payload: Mapping[str, Any]
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_type", _required_text(self.event_type, "event_type"))
        for name in ("event_timestamp", "received_timestamp"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name}_must_be_non_negative_integer")
        object.__setattr__(self, "payload", _immutable_mapping(self.payload, "payload"))
        object.__setattr__(
            self, "provenance", _immutable_mapping(self.provenance, "provenance")
        )


@dataclass(frozen=True)
class ObservableEnvelope:
    """Conceptual v2 envelope for a calculated or normalized observable."""

    contract_version: str
    observable_id: str
    family: str
    context: MarketContext
    event_timestamp: int
    received_timestamp: int
    calculated_timestamp: int
    value: Any
    units: str
    source_mode: SourceMode
    source_status: SourceStatus
    quality: ObservationQuality
    capability: CapabilityState
    derivation_type: DerivationType
    method_version: str | None = None
    data_quality: Mapping[str, Any] = field(default_factory=dict)
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "contract_version", _required_text(self.contract_version, "contract_version")
        )
        object.__setattr__(
            self, "observable_id", _required_text(self.observable_id, "observable_id")
        )
        family = _required_text(self.family, "family").upper()
        if family not in {f"C{number}" for number in range(1, 10)}:
            raise ValueError(f"unsupported_family:{family}")
        object.__setattr__(self, "family", family)
        object.__setattr__(self, "units", _required_text(self.units, "units"))
        for name in ("event_timestamp", "received_timestamp", "calculated_timestamp"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name}_must_be_non_negative_integer")
        if self.derivation_type is not DerivationType.RAW and not self.method_version:
            raise ValueError("method_version_required_for_non_raw_observable")
        object.__setattr__(
            self, "data_quality", _immutable_mapping(self.data_quality, "data_quality")
        )
        object.__setattr__(
            self, "provenance", _immutable_mapping(self.provenance, "provenance")
        )
