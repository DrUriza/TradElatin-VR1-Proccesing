from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .prices_ohlcv.prices_ohlcv_classifier import run_prices_ohlcv_classification
from .prices_ohlcv.prices_ohlcv_contract_builder import build_prices_screen_contract
from .cvd_volume_orderflow.cvd_volume_orderflow_classifier import classify_cvd_volume_orderflow
from .cvd_volume_orderflow.cvd_volume_orderflow_contract_builder import build_cvd_volume_orderflow_contract
from .open_interest_and_funding.open_interest_and_funding_classifier import classify_open_interest_and_funding
from .open_interest_and_funding.open_interest_and_funding_contract_builder import build_open_interest_and_funding_contract
from .etf_exchange_flows.etf_exchange_flows_classifier import run_etf_exchange_flows_classification
from .etf_exchange_flows.etf_exchange_flows_contract_builder import build_etf_exchange_flows_contract
from .on_chain_miners.on_chain_miners_classifier import classify_on_chain_miners
from .on_chain_miners.on_chain_miners_contract_builder import build_on_chain_miners_screen_contract
from .volatility_market_regimes.volatility_market_regimes_classifier import classify_volatility_market_regimes
from .volatility_market_regimes.volatility_market_regimes_contract_builder import build_volatility_market_regimes_screen
from .long_short_liquidations.long_short_liquidations_classifier import classify_long_short_liquidations
from .long_short_liquidations.long_short_liquidations_contract_builder import build_long_short_liquidations_contract
from .liquidity_microstructure.liquidity_microstructure_classifier import classify_liquidity_microstructure
from .liquidity_microstructure.liquidity_microstructure_contract_builder import build_liquidity_microstructure_screen_contract

FAMILY_ORDER = (
    "prices_ohlcv",
    "cvd_volume_orderflow",
    "open_interest_and_funding",
    "etf_exchange_flows",
    "on_chain_miners",
    "volatility_market_regimes",
    "long_short_liquidations",
    "liquidity_microstructure",
)


def _classify(family: str, processing: Mapping[str, Any], arguments: Mapping[str, Any]) -> dict[str, Any]:
    args = dict(arguments)
    if family == "prices_ohlcv":
        if args:
            raise ValueError("prices_ohlcv Classification does not accept classifier arguments")
        return run_prices_ohlcv_classification(processing)
    if family == "cvd_volume_orderflow":
        return classify_cvd_volume_orderflow(processing, **args)
    if family == "open_interest_and_funding":
        if args:
            raise ValueError("open_interest_and_funding Classification does not accept classifier arguments")
        return classify_open_interest_and_funding(processing)
    if family == "etf_exchange_flows":
        return run_etf_exchange_flows_classification(processing_contract=processing, **args)
    if family == "on_chain_miners":
        if args:
            raise ValueError("on_chain_miners Classification does not accept classifier arguments")
        return classify_on_chain_miners(processing)
    if family == "volatility_market_regimes":
        if args:
            raise ValueError("volatility_market_regimes Classification does not accept classifier arguments")
        return classify_volatility_market_regimes(processing)
    if family == "long_short_liquidations":
        return classify_long_short_liquidations(processing, **args)
    if family == "liquidity_microstructure":
        # This processing tree is transient and is consumed immediately by the
        # Liquidity builder. Enrich it in place to avoid cloning 50+ MB before
        # the final immutable JSON export.
        return classify_liquidity_microstructure(
            processing, _copy_input_branches=False, **args
        )
    raise ValueError(f"unsupported_family:{family}")


def _build_output(
    family: str,
    processing: Mapping[str, Any],
    classification: Mapping[str, Any],
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    args = dict(arguments)
    if family == "prices_ohlcv":
        return build_prices_screen_contract(processing, classification, **args)
    if family == "cvd_volume_orderflow":
        return build_cvd_volume_orderflow_contract({"processing": processing, "classification": classification}, **args)
    if family == "open_interest_and_funding":
        return build_open_interest_and_funding_contract({"processing": processing, "classification": classification}, **args)
    if family == "etf_exchange_flows":
        return build_etf_exchange_flows_contract(
            processing_contract=processing,
            classification_contract=classification,
            **args,
        )
    if family == "on_chain_miners":
        if args:
            raise ValueError("on_chain_miners Output does not accept output arguments")
        return build_on_chain_miners_screen_contract(processing, classification)
    if family == "volatility_market_regimes":
        return build_volatility_market_regimes_screen(processing, classification, **args)
    if family == "long_short_liquidations":
        return build_long_short_liquidations_contract(processing, classification, **args)
    if family == "liquidity_microstructure":
        selected = str(args.pop("selected_market", "perpetual"))
        if selected not in {"spot", "perpetual"}:
            selected = "perpetual"
        # The Screen contract represents the selected market. Building both
        # complete views and discarding one doubled Classification cost and
        # memory without changing the emitted JSON.
        return build_liquidity_microstructure_screen_contract(
            {"processing": processing, "classification": classification},
            selected_market=selected,
            **args,
        )
    raise ValueError(f"unsupported_family:{family}")


def run_classification_pipeline(
    *,
    processing_contracts: Mapping[str, Mapping[str, Any]],
    enabled_families: Sequence[str] = ("prices_ohlcv",),
    classifier_arguments: Mapping[str, Mapping[str, Any]] | None = None,
    output_arguments: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Classify and immediately build the final Screen-ready JSON per family.

    The classifier result is intentionally transient.  The only JSON emitted by
    the Classification stage is the canonical Screen contract consumed by Main's
    exporter.
    """
    classifier_args = classifier_arguments or {}
    output_args = output_arguments or {}
    outputs: dict[str, Any] = {}
    for family in enabled_families:
        if family not in FAMILY_ORDER:
            raise ValueError(f"unsupported_family:{family}")
        if family not in processing_contracts:
            raise ValueError(f"processing_contract_missing:{family}")
        processing = processing_contracts[family]
        classification = _classify(family, processing, classifier_args.get(family, {}))
        outputs[family] = _build_output(family, processing, classification, output_args.get(family, {}))
    return outputs
