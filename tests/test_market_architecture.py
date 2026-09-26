from __future__ import annotations

import re
from pathlib import Path

import pytest

from processing_signals.input.acquisition import ALLOWED_ENDPOINTS
from processing_signals.markets import (
    AdapterRequest,
    CapabilityState,
    DerivationType,
    EQUITIES_OBSERVABLES,
    FROZEN_CRYPTO_ENDPOINT_COUNT,
    MarketClass,
    MarketContext,
    ObservationQuality,
    ObservableEnvelope,
    SourceMode,
    SourceStatus,
    to_vr1_observation_v1,
)


def _equity_context() -> MarketContext:
    return MarketContext(
        market_class=MarketClass.EQUITIES,
        asset_id="US:NVDA",
        symbol="NVDA",
        asset_class="equity",
        venue="SMART",
        provider="IBKR",
    )


def test_frozen_crypto_inventory_remains_exactly_33() -> None:
    assert FROZEN_CRYPTO_ENDPOINT_COUNT == 33
    assert len(ALLOWED_ENDPOINTS) == FROZEN_CRYPTO_ENDPOINT_COUNT
    assert {provider for provider, _ in ALLOWED_ENDPOINTS} == {
        "coinglass",
        "cryptoquant",
        "glassnode",
    }
    assert all(not endpoint.startswith("eq_") for _, endpoint in ALLOWED_ENDPOINTS)


def test_equities_registry_is_isolated_and_semantically_bounded() -> None:
    assert len(EQUITIES_OBSERVABLES) == 37
    assert {item.family for item in EQUITIES_OBSERVABLES.values()} == {
        "C1",
        "C2",
        "C3",
        "C6",
        "C8",
    }
    assert all(key.startswith("eq_c") for key in EQUITIES_OBSERVABLES)
    assert "eq_c2_estimated_cvd" in EQUITIES_OBSERVABLES
    assert (
        EQUITIES_OBSERVABLES["eq_c2_estimated_cvd"].derivation_type
        is DerivationType.INFERRED
    )
    assert (
        EQUITIES_OBSERVABLES["eq_c3_shortable_shares"].capability
        is CapabilityState.PARTIAL
    )


def test_registry_ids_match_the_documented_catalog() -> None:
    catalog = (
        Path(__file__).resolve().parents[1]
        / "docs"
        / "equities"
        / "EQUITIES_OBSERVABLE_CATALOG.md"
    ).read_text(encoding="utf-8")
    documented = set(re.findall(r"`(eq_c[1-9]_[a-z0-9_]+)`", catalog))
    assert documented == set(EQUITIES_OBSERVABLES)


def test_market_context_normalizes_provider_without_losing_identity() -> None:
    context = _equity_context()
    assert context.market_class is MarketClass.EQUITIES
    assert context.provider == "ibkr"
    assert context.symbol == "NVDA"


def test_non_raw_observable_requires_method_version() -> None:
    with pytest.raises(ValueError, match="method_version_required"):
        ObservableEnvelope(
            contract_version="vr1-observable-v2-draft",
            observable_id="eq_c8_spread",
            family="C8",
            context=_equity_context(),
            event_timestamp=1,
            received_timestamp=2,
            calculated_timestamp=3,
            value=0.01,
            units="USD",
            source_mode=SourceMode.REPLAY,
            source_status=SourceStatus.REPLAY,
            quality=ObservationQuality.VALID,
            capability=CapabilityState.COMPLETE,
            derivation_type=DerivationType.DERIVED,
        )


def test_adapter_request_parameters_are_immutable() -> None:
    request = AdapterRequest(
        logical_surface="level_1_quote",
        context=_equity_context(),
        parameters={"snapshot": False},
    )
    with pytest.raises(TypeError):
        request.parameters["snapshot"] = True


def test_equities_envelope_projects_to_vr2_frozen_observation_contract() -> None:
    envelope = ObservableEnvelope(
        contract_version="vr1-observable-v2-draft",
        observable_id="eq_c8_spread",
        family="C8",
        context=_equity_context(),
        event_timestamp=1_700_000_000,
        received_timestamp=1_700_000_001,
        calculated_timestamp=1_700_000_002,
        value=1.25,
        units="bps",
        source_mode=SourceMode.REPLAY,
        source_status=SourceStatus.REPLAY,
        quality=ObservationQuality.VALID,
        capability=CapabilityState.COMPLETE,
        derivation_type=DerivationType.DERIVED,
        method_version="spread-bps-v1",
        data_quality={"quote_stale": False},
    )

    observation = to_vr1_observation_v1(envelope)

    assert observation["schema_version"] == "vr1-observation-v1"
    assert observation["market"] == "EQUITIES"
    assert observation["asset"] == "US:NVDA"
    assert observation["family"] == "C8"
    assert observation["source"] == "ibkr"
    assert observation["source_mode"] == "REPLAY"
    assert observation["quality"] == "VALID"
    assert observation["metadata"]["derivation_type"] == "DERIVED"
    assert {
        "schema_version",
        "market",
        "asset",
        "timestamp",
        "family",
        "observable_id",
        "value",
        "units",
        "source",
        "source_mode",
        "quality",
        "metadata",
    } <= observation.keys()
    assert "processing_version" not in observation
    assert "request_id" not in observation
