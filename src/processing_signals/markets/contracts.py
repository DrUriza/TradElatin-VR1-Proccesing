"""Compatibility projections for versioned downstream VR contracts."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .models import ObservableEnvelope


def _iso8601(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat().replace("+00:00", "Z")


def to_vr1_observation_v1(envelope: ObservableEnvelope) -> dict[str, Any]:
    """Project a market-aware envelope into VR2's frozen VR1 input shape.

    Provider-specific and derivation metadata remains namespaced under metadata,
    while the top-level fields match ``vr1-observation-v1`` exactly.  This
    function does not alter the existing BTC Screen contracts or runtime path.
    """
    return {
        "schema_version": "vr1-observation-v1",
        "market": envelope.context.market_class.value,
        "asset": envelope.context.asset_id,
        "timestamp": _iso8601(envelope.event_timestamp),
        "family": envelope.family,
        "observable_id": envelope.observable_id,
        "value": envelope.value,
        "units": envelope.units,
        "source": envelope.context.provider,
        "source_mode": envelope.source_mode.value,
        "quality": envelope.quality.value,
        "metadata": {
            "contract_version": envelope.contract_version,
            "symbol": envelope.context.symbol,
            "asset_class": envelope.context.asset_class,
            "venue": envelope.context.venue,
            "source_status": envelope.source_status.value,
            "capability": envelope.capability.value,
            "derivation_type": envelope.derivation_type.value,
            "method_version": envelope.method_version,
            "received_timestamp": _iso8601(envelope.received_timestamp),
            "calculated_timestamp": _iso8601(envelope.calculated_timestamp),
            "data_quality": dict(envelope.data_quality),
            "provenance": dict(envelope.provenance),
        },
    }
