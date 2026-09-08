from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .acquisition import build_family_fetcher
from .prices_ohlcv.prices_ohlcv_data_raw_preprocessing import run_prices_ohlcv_input
from .cvd_volume_orderflow.cvd_volume_orderflow_data_raw_preprocessing import run_cvd_volume_orderflow_input
from .open_interest_and_funding.open_interest_and_funding_data_raw_preprocessing import run_open_interest_and_funding_input
from .etf_exchange_flows.etf_exchange_flows_data_raw_preprocessing import run_etf_exchange_flows_input
from .on_chain_miners.on_chain_miners_data_raw_preprocessing import run_on_chain_miners_input
from .volatility_market_regimes.volatility_market_regimes_data_raw_preprocessing import run_volatility_market_regimes_input
from .long_short_liquidations.long_short_liquidations_data_raw_preprocessing import run_long_short_liquidations_input
from .liquidity_microstructure.liquidity_microstructure_data_raw_preprocessing import run_liquidity_microstructure_input

INPUT_FAMILY_HANDLERS = {
    "prices_ohlcv": run_prices_ohlcv_input,
    "cvd_volume_orderflow": run_cvd_volume_orderflow_input,
    "open_interest_and_funding": run_open_interest_and_funding_input,
    "etf_exchange_flows": run_etf_exchange_flows_input,
    "on_chain_miners": run_on_chain_miners_input,
    "volatility_market_regimes": run_volatility_market_regimes_input,
    "long_short_liquidations": run_long_short_liquidations_input,
    "liquidity_microstructure": run_liquidity_microstructure_input,
}


def run_input_pipeline(
    *,
    repo_root: str | Path,
    source_mode: str,
    enabled_families: Sequence[str] = ("prices_ohlcv",),
    family_arguments: Mapping[str, Mapping[str, Any]] | None = None,
    fetcher_factory: Callable[[str], Callable[..., Any]] | None = None,
) -> dict[str, Any]:
    """Acquire RAW data and execute only the requested Input families.

    Acquisition is owned by Input. No provider/API/emulator logic exists in
    Main, Processing or Classification.
    """
    arguments = family_arguments or {}
    root = Path(repo_root)
    outputs: dict[str, Any] = {}
    for family in enabled_families:
        handler = INPUT_FAMILY_HANDLERS.get(family)
        if handler is None:
            raise ValueError(f"no_input_handler:{family}")
        family_args = dict(arguments.get(family, {}))
        if "fetcher" not in family_args:
            family_args["fetcher"] = (
                fetcher_factory(family)
                if fetcher_factory is not None
                else build_family_fetcher(repo_root=root, family=family, source_mode=source_mode)
            )
        outputs[family] = handler(**family_args)
    return outputs
