"""TradELATIN VR1 Processing V4.2 standalone entrypoint."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from processing_signals.main.main_pipeline import FAMILY_ORDER, run_main_pipeline


def _families(value: str) -> tuple[str, ...]:
    families = tuple(item.strip() for item in value.split(",") if item.strip())
    if not families:
        raise argparse.ArgumentTypeError("specify at least one family")
    unknown = [family for family in families if family not in FAMILY_ORDER]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown families: {', '.join(unknown)}; valid values: {', '.join(FAMILY_ORDER)}"
        )
    return families


def _default_contract_dir() -> Path:
    override = os.environ.get("TRADELATIN_CONTRACT_DIR", "").strip()
    return Path(override) if override else REPO_ROOT / "data" / "contracts"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TradELATIN V4.2 Processing runtime")
    parser.add_argument("--once", action="store_true", help="run selected families once and exit")
    parser.add_argument(
        "--families",
        type=_families,
        default=FAMILY_ORDER,
        metavar="FAMILY[,FAMILY...]",
        help="families to process (default for --once: all eight)",
    )
    parser.add_argument("--poll-seconds", type=float, default=0.25)
    parser.add_argument("--mode", choices=("auto", "bootstrap", "incremental"), default="auto")
    parser.add_argument("--source", choices=("emulator", "live"), default="emulator")
    parser.add_argument("--data-mode", choices=("synthetic", "live"), default=None)
    parser.add_argument("--reference-timestamp", type=int, default=None, metavar="UNIX_SECONDS")
    parser.add_argument(
        "--contracts-root",
        "--screens-root",
        dest="contracts_root",
        type=Path,
        default=None,
        metavar="PATH",
        help="output contract directory; defaults to TRADELATIN_CONTRACT_DIR or ./data/contracts",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    requested_mode = None if args.mode == "auto" else args.mode
    contracts_root = args.contracts_root or _default_contract_dir()
    contracts_root.mkdir(parents=True, exist_ok=True)
    print(f"Contracts output: {contracts_root}", flush=True)

    if args.once:
        result = run_main_pipeline(
            repo_root=REPO_ROOT,
            screens_contracts_root=contracts_root,
            enabled_families=args.families,
            requested_mode=requested_mode,
            reference_timestamp=args.reference_timestamp,
            source_mode=args.source,
            data_mode=args.data_mode,
        )
        print("TradELATIN pipeline completed", flush=True)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        return 0

    from processing_signals.main.continuous_runtime import AUTOMATIC_INTERVALS, ContinuousRuntime
    from processing_signals.main.persistent_worker import run_family_job

    # Large Trades / executed operations do not need the expensive structural
    # Liquidity pipeline.  Poll and patch that narrow surface independently.
    from processing_signals.input.acquisition import build_family_fetcher
    from processing_signals.input.prices_ohlcv.prices_fast_lane import (
        run_prices_fast_lane_cycle,
    )
    from processing_signals.input.liquidity_microstructure.liquidity_fast_lane import (
        run_liquidity_fast_lane_cycle,
    )

    fast_lane_seconds = max(
        1.0, float(os.environ.get("TRADELATIN_LIQUIDITY_TABLE_SECONDS", "5"))
    )
    runtime_root = Path(
        os.environ.get("TRADELATIN_RUNTIME_DIR", "").strip()
        or REPO_ROOT / "data" / "runtime"
    )
    liquidity_contract = contracts_root / "liquidity_microstructure_VR1_FINAL.json"
    liquidity_state = runtime_root / "liquidity_fast_lane_state.json"
    liquidity_fetcher = build_family_fetcher(
        repo_root=REPO_ROOT,
        family="liquidity_microstructure",
        source_mode=args.source,
    )
    prices_fast_lane_seconds = max(
        1.0, float(os.environ.get("TRADELATIN_PRICES_TABLE_SECONDS", "5"))
    )
    prices_contract = contracts_root / "prices_VR1_FINAL.json"
    prices_state = runtime_root / "prices_fast_lane_state.json"
    prices_fetcher = build_family_fetcher(
        repo_root=REPO_ROOT,
        family="prices_ohlcv",
        source_mode=args.source,
    )

    def prices_fast_lane_worker() -> None:
        # Phase the narrow writer away from the three cold structural launches.
        time.sleep(1.0)
        while True:
            started = time.monotonic()
            try:
                result = run_prices_fast_lane_cycle(
                    fetcher=prices_fetcher,
                    screen_contract_path=prices_contract,
                    state_path=prices_state,
                )
                if result.get("patched"):
                    candle = result.get("candle") or {}
                    print(
                        f"[Prices 1m] UPDATED close={candle.get('close')}",
                        flush=True,
                    )
            except Exception as exc:
                print(f"[Prices 1m] ERROR {type(exc).__name__}: {exc}", flush=True)
            time.sleep(max(0.1, prices_fast_lane_seconds - (time.monotonic() - started)))

    threading.Thread(
        target=prices_fast_lane_worker,
        name="prices-1m-fast-lane",
        daemon=True,
    ).start()
    print(f"[Prices 1m/5m] fast lane={prices_fast_lane_seconds:g}s", flush=True)

    def liquidity_fast_lane_worker() -> None:
        # A short phase prevents Emulator/cache warm-up from becoming a startup
        # stampede while leaving the already-published bundled contract usable.
        time.sleep(3.0)
        while True:
            started = time.monotonic()
            try:
                result = run_liquidity_fast_lane_cycle(
                    fetcher=liquidity_fetcher,
                    screen_contract_path=liquidity_contract,
                    state_path=liquidity_state,
                    source_mode=args.source,
                )
                if result.get("patched"):
                    print(
                        f"[Liquidity Table] UPDATED new_events={result.get('new_events', 0)}",
                        flush=True,
                    )
            except Exception as exc:
                print(
                    f"[Liquidity Table] ERROR {type(exc).__name__}: {exc}",
                    flush=True,
                )
            time.sleep(max(0.1, fast_lane_seconds - (time.monotonic() - started)))

    threading.Thread(
        target=liquidity_fast_lane_worker,
        name="liquidity-table-fast-lane",
        daemon=True,
    ).start()
    print(f"[Liquidity KPI/Table] fast lane={fast_lane_seconds:g}s", flush=True)

    intervals = {
        family: float(os.environ.get(f"TRADELATIN_{family.upper()}_SECONDS", seconds))
        for family, seconds in AUTOMATIC_INTERVALS.items()
    }

    def command_builder(family: str) -> list[str]:
        command = [
            sys.executable,
            str(REPO_ROOT / "main.py"),
            "--once",
            "--families",
            family,
            "--mode",
            args.mode,
            "--source",
            args.source,
            "--contracts-root",
            str(contracts_root),
        ]
        if args.data_mode:
            command.extend(["--data-mode", args.data_mode])
        if args.reference_timestamp is not None:
            command.extend(["--reference-timestamp", str(args.reference_timestamp)])
        return command

    def persistent_job_builder(family: str) -> dict[str, object]:
        return {
            "repo_root": str(REPO_ROOT),
            "contracts_root": str(contracts_root),
            "family": family,
            "mode": args.mode,
            "source": args.source,
            "data_mode": args.data_mode,
            "reference_timestamp": args.reference_timestamp,
        }

    persistent_workers = os.environ.get(
        "TRADELATIN_PERSISTENT_WORKERS", "1"
    ).strip().lower() not in {"0", "false", "no", "off"}
    try:
        persistent_worker_max_jobs = max(
            1,
            int(os.environ.get("TRADELATIN_WORKER_MAX_JOBS", "120")),
        )
    except ValueError:
        persistent_worker_max_jobs = 120

    runtime = ContinuousRuntime(
        command_builder=command_builder,
        repo_root=REPO_ROOT,
        intervals=intervals,
        poll_seconds=args.poll_seconds,
        persistent_job_builder=persistent_job_builder if persistent_workers else None,
        persistent_job_runner=run_family_job if persistent_workers else None,
        persistent_worker_max_jobs=persistent_worker_max_jobs,
    )
    runtime.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
