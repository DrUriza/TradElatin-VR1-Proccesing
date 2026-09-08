from __future__ import annotations

from typing import Any, Mapping, Sequence

from .prices_ohlcv.prices_ohlcv_processor import run_prices_ohlcv_processing
from .etf_exchange_flows.etf_exchange_flows_processor import run_etf_exchange_flows_processing
from .liquidity_microstructure.liquidity_microstructure_processor import process_liquidity_microstructure
from .long_short_liquidations.long_short_liquidations_processor import process_long_short_liquidations
from .on_chain_miners.on_chain_miners_processor import process_on_chain_miners
from .open_interest_and_funding.open_interest_and_funding_processor import process_open_interest_and_funding
from .volatility_market_regimes.volatility_market_regimes_processor import process_volatility_market_regimes
from .cvd_volume_orderflow.cvd_volume_orderflow_processor import process_cvd_volume_orderflow


def _simple(handler, input_contract: Mapping[str, Any], *, family_arguments: Mapping[str, Any]) -> dict[str, Any]:
    return handler(input_contract, **dict(family_arguments))


def _run_prices(input_contract: Mapping[str, Any], *, existing_processing, now_timestamp, family_arguments):
    arguments = dict(family_arguments)
    return run_prices_ohlcv_processing(input_contract, existing_processing=existing_processing,
                                       now_timestamp=now_timestamp, **arguments)


def _run_etf(input_contract: Mapping[str, Any], *, existing_processing, now_timestamp, family_arguments):
    del existing_processing
    arguments = dict(family_arguments)
    if "generated_at" not in arguments and now_timestamp is not None:
        arguments["generated_at"] = now_timestamp
    return run_etf_exchange_flows_processing(input_contract=input_contract, **arguments)


def _run_liquidity(input_contract: Mapping[str, Any], *, existing_processing, now_timestamp, family_arguments):
    return process_liquidity_microstructure(input_contract, existing_processing=existing_processing,
                                            now_timestamp=now_timestamp, **dict(family_arguments))


def _run_liquidations(input_contract: Mapping[str, Any], *, existing_processing, now_timestamp, family_arguments):
    del existing_processing, now_timestamp
    return _simple(process_long_short_liquidations, input_contract, family_arguments=family_arguments)


def _run_onchain(input_contract: Mapping[str, Any], *, existing_processing, now_timestamp, family_arguments):
    del existing_processing, now_timestamp
    if family_arguments:
        raise ValueError("on_chain_miners Processing does not accept family arguments")
    return process_on_chain_miners(input_contract)


def _run_oi(input_contract: Mapping[str, Any], *, existing_processing, now_timestamp, family_arguments):
    del existing_processing, now_timestamp
    return process_open_interest_and_funding(input_contract, **dict(family_arguments))


def _run_volatility(input_contract: Mapping[str, Any], *, existing_processing, now_timestamp, family_arguments):
    del existing_processing, now_timestamp
    return process_volatility_market_regimes(input_contract, **dict(family_arguments))


def _run_cvd(input_contract: Mapping[str, Any], *, existing_processing, now_timestamp, family_arguments):
    del existing_processing, now_timestamp
    return process_cvd_volume_orderflow(input_contract, **dict(family_arguments))


PROCESSING_FAMILY_HANDLERS = {
    "prices_ohlcv": _run_prices,
    "cvd_volume_orderflow": _run_cvd,
    "open_interest_and_funding": _run_oi,
    "etf_exchange_flows": _run_etf,
    "on_chain_miners": _run_onchain,
    "volatility_market_regimes": _run_volatility,
    "long_short_liquidations": _run_liquidations,
    "liquidity_microstructure": _run_liquidity,
}


def run_processing_pipeline(*, input_contracts: Mapping[str, Mapping[str, Any]],
                            enabled_families: Sequence[str] = ("prices_ohlcv",),
                            existing_processing: Mapping[str, Mapping[str, Any]] | None = None,
                            now_timestamp: int | None = None,
                            family_arguments: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    existing = existing_processing or {}
    arguments = family_arguments or {}
    outputs: dict[str, Any] = {}
    for family in enabled_families:
        handler = PROCESSING_FAMILY_HANDLERS.get(family)
        if handler is None:
            raise ValueError(f"No Processing handler registered for family: {family}")
        if family not in input_contracts:
            raise ValueError(f"Input contract missing for family: {family}")
        outputs[family] = handler(
            input_contracts[family],
            existing_processing=existing.get(family),
            now_timestamp=now_timestamp,
            family_arguments=dict(arguments.get(family, {})),
        )
    return outputs
