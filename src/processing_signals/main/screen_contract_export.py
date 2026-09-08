from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .atomic_replace import replace_with_retry

FAMILY_FILENAMES: dict[str, str] = {
    "prices_ohlcv": "prices_VR1_FINAL.json",
    "cvd_volume_orderflow": "cvd_volume_orderflow_VR1_FINAL.json",
    "open_interest_and_funding": "open_interest_and_funding_VR1_FINAL.json",
    "etf_exchange_flows": "etf_exchange_flows_VR1_FINAL.json",
    "on_chain_miners": "on_chain_miners_VR1_FINAL.json",
    "volatility_market_regimes": "volatility_market_regimes_VR1_FINAL.json",
    "long_short_liquidations": "long_short_liquidations_VR1_FINAL.json",
    "liquidity_microstructure": "liquidity_microstructure_VR1_FINAL.json",
}


def _validate_json_tree(value: Any) -> None:
    stack = [value]
    seen_containers: set[int] = set()
    while stack:
        current = stack.pop()
        if type(current) is dict:
            identity = id(current)
            if identity in seen_containers:
                continue
            seen_containers.add(identity)
            if any(type(key) is not str for key in current):
                raise ValueError("screen_contract_keys_must_be_strings")
            stack.extend(current.values())
        elif type(current) is list:
            identity = id(current)
            if identity in seen_containers:
                continue
            seen_containers.add(identity)
            stack.extend(current)
        elif type(current) is float:
            if not math.isfinite(current):
                raise ValueError("screen_contract_not_strict_json")
        elif current is not None and type(current) not in (str, int, bool):
            raise ValueError(f"screen_contract_unsupported_type:{type(current).__name__}")


def validate_screen_contract(family: str, contract: Mapping[str, Any]) -> None:
    """Minimal boundary validation before publishing to Screens.

    Classification owns contract semantics.  Export only verifies the four
    integration invariants required at the repository boundary.
    """
    if family not in FAMILY_FILENAMES:
        raise ValueError(f"unsupported_family:{family}")
    if not isinstance(contract, Mapping):
        raise ValueError(f"{family}:screen_contract_must_be_object")

    screen = contract.get("screen")
    if isinstance(screen, Mapping):
        contract_family = screen.get("family") or screen.get("id")
    else:
        contract_family = contract.get("family") or contract.get("screen_id") or screen
    if contract_family != family:
        raise ValueError(f"{family}:screen_contract_family_mismatch:{contract_family}")

    _validate_json_tree(contract)


_LEGACY_DEMO_BLOCK_KEYS = {"demo_fixture", "visual_fixture", "visual_fixture_population", "dense_demo_liquidity_fixture", "technical_fixture_validation"}

def _strip_legacy_demo_payloads(value: Any) -> Any:
    """Remove obsolete Screen demo payloads without hiding Emulator provenance."""
    if type(value) is dict:
        changed = False
        output: dict[str, Any] = {}
        for key, item in value.items():
            if key in _LEGACY_DEMO_BLOCK_KEYS:
                changed = True
                continue
            cleaned = _strip_legacy_demo_payloads(item)
            output[key] = cleaned
            changed = changed or cleaned is not item
        return output if changed else value
    if type(value) is list:
        output = [_strip_legacy_demo_payloads(item) for item in value]
        return output if any(cleaned is not item for cleaned, item in zip(output, value)) else value
    if isinstance(value, str):
        return ("emulator" if value == "demo_fixture" else value.replace("available_demo_fixture", "available").replace("weekday_trading_session_fixture", "weekday_trading_session_emulator"))
    return value


def _atomic_write_json(destination: Path, payload: Mapping[str, Any]) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ) + "\n"
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(temporary_name, destination)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise
    return destination


def export_screen_contract(
    family: str,
    contract: Mapping[str, Any],
    *,
    screens_contracts_root: str | Path,
) -> Path:
    validate_screen_contract(family, contract)
    clean_contract = _strip_legacy_demo_payloads(contract)
    if clean_contract is not contract:
        validate_screen_contract(family, clean_contract)
    destination = Path(screens_contracts_root) / FAMILY_FILENAMES[family]
    return _atomic_write_json(destination, clean_contract)


def export_screen_contracts(
    contracts: Mapping[str, Mapping[str, Any]],
    *,
    screens_contracts_root: str | Path,
) -> dict[str, Path]:
    return {
        family: export_screen_contract(
            family,
            contract,
            screens_contracts_root=screens_contracts_root,
        )
        for family, contract in contracts.items()
    }
