from __future__ import annotations

import json
import os
import tempfile
import gc
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from processing_signals.classification.classification_pipeline import run_classification_pipeline
from processing_signals.input.input_pipeline import run_input_pipeline
from processing_signals.input.liquidity_microstructure.liquidity_fast_lane import patch_screen_contract
from processing_signals.input.prices_ohlcv.prices_fast_lane import patch_prices_contract
from processing_signals.processing.processing_pipeline import run_processing_pipeline

from .screen_contract_export import export_screen_contracts
from .atomic_replace import replace_with_retry

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


def _iso(timestamp: int) -> str:
    return datetime.fromtimestamp(int(timestamp), tz=UTC).isoformat().replace("+00:00", "Z")


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ) + "\n"
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _load_existing(stage_root: Path, family: str) -> dict[str, Any] | None:
    path = stage_root / f"{family}.json"
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else None




_INCREMENTAL_RECORD_EXCLUDED_PATH_TOKENS = {"events", "event_stream", "whale_orders"}


def _record_timestamp(row: Any) -> int | None:
    if not isinstance(row, Mapping):
        return None
    value = row.get("timestamp")
    return int(value) if type(value) is int else None


def _preserve_incremental_record_list(
    candidate_rows: Sequence[Any],
    existing_rows: Sequence[Any],
) -> tuple[list[Any], list[Any]]:
    """Keep closed history immutable and admit only the live tail.

    Provider/Emulator requests may still return a fixed 500-row transport
    window.  On incremental runs the persisted chart history is authoritative:
    rows older than the last persisted timestamp are never rewritten.  The
    last open bucket may be replaced and any genuinely newer buckets are
    appended (for example after a short polling gap).
    """
    # Both inputs are cycle-local JSON trees.  Keep only new list containers and
    # reuse their row objects: this function never mutates a row, and every
    # downstream stage treats Input contracts as read-only.  Deep-copying every
    # closed row used to duplicate tens of megabytes on each Liquidity cycle.
    old = list(existing_rows)
    new = list(candidate_rows)
    old_timestamps = [ts for row in old if (ts := _record_timestamp(row)) is not None]
    new_timestamps = [ts for row in new if (ts := _record_timestamp(row)) is not None]
    if not old_timestamps or not new_timestamps:
        return new, new
    watermark = max(old_timestamps)
    preserved = [row for row in old if (_record_timestamp(row) is None or _record_timestamp(row) < watermark)]
    tail = [row for row in new if (ts := _record_timestamp(row)) is not None and ts >= watermark]
    if not tail:
        return old, []
    merged = preserved + tail
    # Most market series are timestamp ordered.  Keep stable ordering among
    # multiple rows at the same timestamp (ETF tickers / price bins).
    merged.sort(key=lambda row: (_record_timestamp(row) is None, _record_timestamp(row) or 0))
    return merged, tail


def _preserve_incremental_history(candidate: Any, existing: Any, path: tuple[str, ...] = ()) -> Any:
    """Recursively enforce append/replace-last semantics on timestamped series.

    Snapshot/event collections are intentionally excluded: a current order
    snapshot may legitimately remove rows, and event streams use their own
    event-id/watermark deduplication.
    """
    if not isinstance(candidate, Mapping):
        return candidate
    source = existing if isinstance(existing, Mapping) else {}
    # Shallow copy only the mapping currently being rewritten.  Nested mappings
    # are copied on their own recursive visit; scalar values and immutable
    # cycle-local lists can be shared until serialization.
    out: dict[str, Any] = dict(candidate)

    has_timestamped_records = (
        isinstance(candidate.get("records"), list)
        and isinstance(source.get("records"), list)
        and any(_record_timestamp(row) is not None for row in candidate.get("records", []))
        and not any(token in _INCREMENTAL_RECORD_EXCLUDED_PATH_TOKENS for token in path)
    )
    live_tail: list[Any] | None = None
    if has_timestamped_records:
        records, live_tail = _preserve_incremental_record_list(candidate["records"], source["records"])
        out["records"] = records

    for key, value in candidate.items():
        if key == "records" and has_timestamped_records:
            continue
        if key == "incoming_records" and has_timestamped_records and isinstance(value, list):
            if not value:
                out[key] = []
            else:
                old_timestamps = [
                    ts for row in source.get("records", [])
                    if (ts := _record_timestamp(row)) is not None
                ]
                watermark = max(old_timestamps) if old_timestamps else None
                out[key] = [
                    row for row in value
                    if watermark is None
                    or ((ts := _record_timestamp(row)) is not None and ts >= watermark)
                ]
            continue
        previous = source.get(key) if isinstance(source, Mapping) else None
        if isinstance(value, Mapping):
            out[key] = _preserve_incremental_history(value, previous, path + (str(key),))
        else:
            out[key] = value

    if has_timestamped_records:
        records = out.get("records", [])
        timestamps = [ts for row in records if (ts := _record_timestamp(row)) is not None]
        for key, value in (
            ("records_available", len(records)),
            ("incoming_valid_count", len(live_tail or [])),
            ("first_timestamp", min(timestamps) if timestamps else None),
            ("last_timestamp", max(timestamps) if timestamps else None),
            ("source_data_as_of", max(timestamps) if timestamps else None),
        ):
            if key in out:
                out[key] = value
    return out


def _liquidity_prices_context(prices_input: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(prices_input, Mapping):
        return None
    # Liquidity Screen-B native analysis is a 4h historical join. Passing only
    # 15m context made the timestamp intersection empty even though both sources
    # had deep history. Preserve only the canonical Spot 4h slice here.
    result: dict[str, Any] = {"markets": {"spot": {"timeframes": {}}}}
    four_hour = (prices_input.get("markets", {}).get("spot", {}).get("timeframes", {}).get("4h", {})
                 if isinstance(prices_input.get("markets"), Mapping) else {})
    if isinstance(four_hour, Mapping):
        result["markets"]["spot"]["timeframes"]["4h"] = {"records": deepcopy(four_hour.get("records", []))}
    return result


def _liquidity_cvd_context(cvd_processing: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(cvd_processing, Mapping):
        return None
    markets: dict[str, Any] = {}
    for market in ("spot", "futures"):
        block = cvd_processing.get("markets", {}).get(market, {}).get("timeframes", {}).get("4h", {})
        if isinstance(block, Mapping):
            markets[market] = {"timeframes": {"4h": {"records": deepcopy(block.get("records", []))}}}
    return {"markets": markets}


def _build_input_arguments(
    *,
    reference_timestamp: int | None,
    requested_mode: str | None,
    existing_inputs: Mapping[str, Mapping[str, Any]],
    data_mode: str,
    is_demo: bool,
) -> dict[str, dict[str, Any]]:
    """Build family parameters for Input; RAW file access is owned by Input."""
    now = int(reference_timestamp or datetime.now(tz=UTC).timestamp())
    refs = {family: now for family in FAMILY_ORDER}
    pairs = {"Binance": "BTCUSDT", "OKX": "BTCUSDT", "Bybit": "BTCUSDT"}
    previous = existing_inputs if requested_mode != "bootstrap" else {}
    return {
        "prices_ohlcv": {
            "requested_mode": requested_mode,
            "existing_contract": previous.get("prices_ohlcv"),
            "bootstrap_limit": 500,
            "data_mode": data_mode,
            "is_demo": is_demo,
            "reference_timestamp": refs["prices_ohlcv"],
            "execution_timestamp": refs["prices_ohlcv"] + 5,
        },
        "cvd_volume_orderflow": {
            "requested_mode": requested_mode,
            "existing_input": previous.get("cvd_volume_orderflow"),
            "reference_timestamp": refs["cvd_volume_orderflow"],
            "clock": lambda ref=refs["cvd_volume_orderflow"]: ref + 5,
            "data_mode": data_mode,
            "is_demo": is_demo,
        },
        "open_interest_and_funding": {
            "requested_mode": requested_mode,
            "existing_state": previous.get("open_interest_and_funding"),
            "reference_timestamp": refs["open_interest_and_funding"],
            "execution_timestamp": refs["open_interest_and_funding"] + 5,
            "data_mode": data_mode,
            "is_demo": is_demo,
        },
        "etf_exchange_flows": {
            "requested_mode": requested_mode,
            "existing_contract": previous.get("etf_exchange_flows"),
            "include_secondary": False,
            "data_mode": data_mode,
            "is_demo": is_demo,
            "exchange_scope": "all_exchange",
            "symbol": "BTC",
            "now": refs["etf_exchange_flows"],
        },
        "on_chain_miners": {
            "requested_mode": requested_mode,
            "existing_contract": previous.get("on_chain_miners"),
            "reference_timestamp": refs["on_chain_miners"],
            "execution_timestamp": refs["on_chain_miners"] + 5,
            "data_mode": data_mode,
            "is_demo": is_demo,
        },
        "volatility_market_regimes": {
            "requested_mode": requested_mode,
            "existing_contract": previous.get("volatility_market_regimes"),
            "reference_timestamp": refs["volatility_market_regimes"],
            "clock": lambda ref=refs["volatility_market_regimes"]: ref + 5,
        },
        "long_short_liquidations": {
            "requested_mode": requested_mode,
            "existing_contract": previous.get("long_short_liquidations"),
            "reference_timestamp": refs["long_short_liquidations"],
            "clock": lambda ref=refs["long_short_liquidations"]: ref + 5,
            "exchange_pairs": pairs,
            "history_hours": 730,
            "include_confirmations": False,
        },
        "liquidity_microstructure": {
            "requested_mode": requested_mode,
            "existing_contract": previous.get("liquidity_microstructure"),
            "reference_timestamp": refs["liquidity_microstructure"],
            "execution_timestamp": refs["liquidity_microstructure"] + 5,
            "data_mode": data_mode,
            "is_demo": is_demo,
            "history_limit": 240,
            "hourly_history_limit": 1000,
            "footprint_limit": 240,
        },
    }


def _latest_spot_close(prices_processing: Mapping[str, Any]) -> dict[str, Any] | None:
    records = (
        prices_processing.get("markets", {})
        .get("spot", {})
        .get("timeframes", {})
        .get("1m", {})
        .get("records", [])
    )
    if not isinstance(records, list):
        return None
    usable = [
        item for item in records
        if isinstance(item, Mapping)
        and isinstance(item.get("timestamp"), int)
        and not isinstance(item.get("timestamp"), bool)
        and isinstance(item.get("close"), (int, float))
        and not isinstance(item.get("close"), bool)
        and float(item["close"]) > 0
    ]
    if not usable:
        return None
    record = max(usable, key=lambda item: int(item["timestamp"]))
    return {"value": float(record["close"]), "timestamp": int(record["timestamp"])}


def _liquidations_reference_price_context(
    prices_processing: Mapping[str, Any], *, target_timestamp: int, is_demo: bool
) -> dict[str, Any] | None:
    latest = _latest_spot_close(prices_processing)
    # A missing/stale Prices observation must degrade liquidation-map features
    # to unavailable, not abort generation of the entire Screen contract.
    if latest is None:
        return None
    return {
        "source_family": "prices_ohlcv",
        "source_market": "spot",
        "source_timeframe": "1m",
        "price_field": "close",
        "is_closed_bar": True,
        "value": latest["value"],
        "timestamp": int(target_timestamp),
        "synthetic_fixture_alignment": bool(is_demo),
        "source_fixture_timestamp": latest["timestamp"] if is_demo else None,
        "timestamp_alignment": (
            "synthetic_fixture_rebased_to_target_reference"
            if is_demo
            else "reference_timestamp_aligned"
        ),
    }


def _builder_arguments(
    processing: Mapping[str, Mapping[str, Any]], *, data_mode: str, is_demo: bool
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if "prices_ohlcv" in processing:
        result["prices_ohlcv"] = {"cvd_processing_context": processing.get("cvd_volume_orderflow")}
    if "cvd_volume_orderflow" in processing:
        result["cvd_volume_orderflow"] = {"selected_market": "spot", "selected_timeframe": "15m"}
    if "open_interest_and_funding" in processing:
        result["open_interest_and_funding"] = {"selected_timeframe": "15m"}
    if "etf_exchange_flows" in processing:
        etf = processing["etf_exchange_flows"]
        # Classification time is execution metadata, not market-data freshness.
        # ETF may legitimately be partial with data_as_of=None during startup;
        # generated_at remains valid and must not be derived from data_as_of.
        result["etf_exchange_flows"] = {
            "selected_range": "30d",
            "generated_at": etf.get("generated_at"),
        }
    if "volatility_market_regimes" in processing:
        volatility = processing["volatility_market_regimes"]
        result["volatility_market_regimes"] = {
            "runtime_context": {
                "data_mode": data_mode,
                "is_demo": is_demo,
                "generated_at": _iso(int(volatility["context"]["input_execution_timestamp"])),
                "updated_at": _iso(int(volatility["context"]["reference_timestamp"])),
            },
            "selected_range": "30d",
        }
    if "long_short_liquidations" in processing:
        liquidations = processing["long_short_liquidations"]
        result["long_short_liquidations"] = {
            "context": {
                "symbol": "BTCUSDT",
                "base_asset": "BTC",
                "quote_asset": "USDT",
                "market": "futures",
                "price_precision": 2,
            },
            "runtime_context": {
                "generated_at": int(liquidations["reference_timestamp"]) + 10,
                "updated_at": int(liquidations["reference_timestamp"]),
                "data_mode": data_mode,
                "is_demo": is_demo,
                "cache_status": "disabled",
            },
        }
    if "liquidity_microstructure" in processing:
        liquidity = processing["liquidity_microstructure"]
        result["liquidity_microstructure"] = {
            "runtime_context": {
                "data_mode": data_mode,
                "is_demo": is_demo,
                "generated_at": _iso(int(liquidity["execution_timestamp"])),
                "updated_at": _iso(int(liquidity["reference_timestamp"])),
                "connection_status": "not_reported",
                "cache_status": "not_reported",
                "latency_ms": None,
                "refresh_interval_seconds": float(os.environ.get("TRADELATIN_LIQUIDITY_SECONDS", "5")),
                "cache_ttl_seconds": None,
            }
        }
    return result


def run_main_pipeline(
    *,
    repo_root: str | Path,
    screens_contracts_root: str | Path,
    enabled_families: Sequence[str] = FAMILY_ORDER,
    requested_mode: str | None = None,
    reference_timestamp: int | None = None,
    source_mode: str = "emulator",
    data_mode: str | None = None,
    fetcher_factory: Any | None = None,
) -> dict[str, Any]:
    """Single Processing orchestration path: Input -> Processing -> Classification -> Screen export."""
    root = Path(repo_root)
    if source_mode not in {"emulator", "live"}:
        raise ValueError(f"unsupported_source_mode:{source_mode}")
    if data_mode is None:
        data_mode = "synthetic" if source_mode == "emulator" else "live"
    if data_mode not in {"synthetic", "live"}:
        raise ValueError(f"unsupported_data_mode:{data_mode}")
    is_demo = data_mode == "synthetic"
    contracts_root = root / "data" / "contracts"
    families = tuple(enabled_families)
    unknown = [family for family in families if family not in FAMILY_ORDER]
    if unknown:
        raise ValueError(f"unsupported_families:{unknown}")

    input_dependencies = {
        "cvd_volume_orderflow": {"prices_ohlcv"},
        "open_interest_and_funding": {"prices_ohlcv"},
        "etf_exchange_flows": {"prices_ohlcv"},
        "volatility_market_regimes": {"prices_ohlcv"},
        "long_short_liquidations": {"prices_ohlcv"},
        "liquidity_microstructure": {"prices_ohlcv"},
    }
    processing_dependencies = {
        "prices_ohlcv": {"cvd_volume_orderflow"},
        "long_short_liquidations": {"prices_ohlcv"},
        "liquidity_microstructure": {"cvd_volume_orderflow"},
    }

    # Bootstrap ignores only the family's own previous stage state. Persisted
    # cross-family context is still valid and required for selective starts
    # (for example a first Liquidity run reuses the latest Prices/CVD state).
    needed_inputs = set() if requested_mode == "bootstrap" else set(families)
    needed_processing = set() if requested_mode == "bootstrap" else set(families)
    for family in families:
        needed_inputs.update(input_dependencies.get(family, set()))
        needed_processing.update(processing_dependencies.get(family, set()))
    existing_inputs = {
        family: value for family in needed_inputs
        if (value := _load_existing(contracts_root / "input", family)) is not None
    }
    existing_processing = {
        family: value for family in needed_processing
        if (value := _load_existing(contracts_root / "processing", family)) is not None
    }

    input_args = _build_input_arguments(
        reference_timestamp=reference_timestamp,
        requested_mode=requested_mode,
        existing_inputs=existing_inputs,
        data_mode=data_mode,
        is_demo=is_demo,
    )

    inputs: dict[str, Any] = {}
    for family in families:
        print(f"[{family}][INPUT] RUN", flush=True)
        inputs[family] = run_input_pipeline(
            repo_root=root,
            source_mode=source_mode,
            enabled_families=(family,),
            family_arguments={family: input_args[family]},
            fetcher_factory=fetcher_factory,
        )[family]
        if (
            inputs[family].get("mode") == "incremental"
            and isinstance(existing_inputs.get(family), Mapping)
        ):
            inputs[family] = _preserve_incremental_history(
                inputs[family], existing_inputs[family], (family,)
            )
        _atomic_write_json(contracts_root / "input" / f"{family}.json", inputs[family])
        print(f"[{family}][INPUT] OK", flush=True)

    # Cross-family dependencies belong here, not inside family pipelines.
    processing: dict[str, Any] = {}
    now_timestamp = int(reference_timestamp or datetime.now(tz=UTC).timestamp()) + 5

    if "cvd_volume_orderflow" in families:
        prices_input = inputs.get("prices_ohlcv") or existing_inputs.get("prices_ohlcv")
        price_history: dict[str, dict[str, Any]] = {}
        if isinstance(prices_input, Mapping):
            for market in ("spot", "futures"):
                tf_map = prices_input.get("markets", {}).get(market, {}).get("timeframes", {})
                if isinstance(tf_map, Mapping):
                    price_history[market] = {
                        tf: block.get("records", [])
                        for tf, block in tf_map.items()
                        if isinstance(block, Mapping)
                    }
        print("[cvd_volume_orderflow][PROCESSING] RUN", flush=True)
        processing["cvd_volume_orderflow"] = run_processing_pipeline(
            input_contracts=inputs,
            enabled_families=("cvd_volume_orderflow",),
            existing_processing=existing_processing,
            now_timestamp=now_timestamp,
            family_arguments={"cvd_volume_orderflow": {
                "clock": lambda ref=now_timestamp: ref,
                "price_history_by_market_timeframe": price_history,
            }},
        )["cvd_volume_orderflow"]
        _atomic_write_json(contracts_root / "processing" / "cvd_volume_orderflow.json", processing["cvd_volume_orderflow"])
        print("[cvd_volume_orderflow][PROCESSING] OK", flush=True)

    if "prices_ohlcv" in families:
        print("[prices_ohlcv][PROCESSING] RUN", flush=True)
        spot_timeframes = inputs.get("prices_ohlcv", {}).get("markets", {}).get("spot", {}).get("timeframes", {})
        dirty_timeframes = [
            str(timeframe)
            for timeframe, payload in spot_timeframes.items()
            if isinstance(payload, Mapping) and bool(payload.get("incoming_records"))
        ]
        processing["prices_ohlcv"] = run_processing_pipeline(
            input_contracts=inputs,
            enabled_families=("prices_ohlcv",),
            existing_processing=existing_processing,
            now_timestamp=now_timestamp,
            family_arguments={"prices_ohlcv": {
                "dirty_timeframes": dirty_timeframes,
                "cvd_processing_context": processing.get("cvd_volume_orderflow") or existing_processing.get("cvd_volume_orderflow"),
            }},
        )["prices_ohlcv"]
        _atomic_write_json(contracts_root / "processing" / "prices_ohlcv.json", processing["prices_ohlcv"])
        print("[prices_ohlcv][PROCESSING] OK", flush=True)

    prices_input = inputs.get("prices_ohlcv") or existing_inputs.get("prices_ohlcv")
    for family in families:
        if family in {"prices_ohlcv", "cvd_volume_orderflow"}:
            continue
        family_args: dict[str, Any] = {}
        if family == "open_interest_and_funding" and isinstance(prices_input, Mapping):
            spot_tfs = prices_input.get("markets", {}).get("spot", {}).get("timeframes", {})
            family_args["price_history_by_timeframe"] = {
                tf: block.get("records", []) for tf, block in spot_tfs.items() if isinstance(block, Mapping)
            }
        elif family in {"etf_exchange_flows", "volatility_market_regimes"} and isinstance(prices_input, Mapping):
            # ETF divergence needs enough daily history for rolling z-scores.
            # 500 x 15m is only ~5 days; 500 x 4h supplies ~83 days while
            # remaining inside the frozen public timeframe set.
            source_tf = "4h"
            intraday = prices_input.get("markets", {}).get("spot", {}).get("timeframes", {}).get(source_tf, {})
            rows = intraday.get("records", []) if isinstance(intraday, Mapping) else []
            by_day: dict[int, list[Mapping[str, Any]]] = {}
            for row in rows:
                if isinstance(row, Mapping) and type(row.get("timestamp")) is int:
                    day = int(row["timestamp"]) - int(row["timestamp"]) % 86_400
                    by_day.setdefault(day, []).append(row)
            family_args["price_history_daily"] = [
                {
                    "timestamp": day,
                    "open": ordered[0].get("open"),
                    "high": max(float(item.get("high")) for item in ordered),
                    "low": min(float(item.get("low")) for item in ordered),
                    "close": ordered[-1].get("close"),
                    "volume": sum(float(item.get("volume", 0.0) or 0.0) for item in ordered),
                }
                for day, bucket in sorted(by_day.items())
                if (ordered := sorted(bucket, key=lambda item: int(item["timestamp"])))
            ]
        elif family == "long_short_liquidations":
            prices_context = processing.get("prices_ohlcv") or existing_processing.get("prices_ohlcv")
            if not isinstance(prices_context, Mapping):
                raise ValueError("long_short_liquidations_requires_prices_processing")
            target = int(inputs[family]["reference_timestamp"])
            alignment_prices = []
            if isinstance(prices_input, Mapping):
                block = prices_input.get("markets", {}).get("spot", {}).get("timeframes", {}).get("15m", {})
                alignment_prices = block.get("records", []) if isinstance(block, Mapping) else []
            family_args = {
                "reference_price_context": _liquidations_reference_price_context(prices_context, target_timestamp=target, is_demo=is_demo),
                "price_history": alignment_prices,
            }
        elif family == "liquidity_microstructure":
            if not isinstance(prices_input, Mapping):
                raise ValueError("liquidity_microstructure_requires_prices_input")
            cvd_context = processing.get("cvd_volume_orderflow") or existing_processing.get("cvd_volume_orderflow")
            family_args = {
                "prices_input_context": _liquidity_prices_context(prices_input),
                "cvd_processing_context": _liquidity_cvd_context(cvd_context),
            }

        print(f"[{family}][PROCESSING] RUN", flush=True)
        processing[family] = run_processing_pipeline(
            input_contracts=inputs,
            enabled_families=(family,),
            existing_processing=existing_processing,
            now_timestamp=now_timestamp,
            family_arguments={family: family_args},
        )[family]
        _atomic_write_json(contracts_root / "processing" / f"{family}.json", processing[family])
        print(f"[{family}][PROCESSING] OK", flush=True)

    # Classification no longer needs Input payloads.  Release them before the
    # heaviest builders run so an all-family bootstrap cannot retain hundreds
    # of MB of duplicate stage contracts.
    inputs.clear()
    existing_inputs.clear()
    existing_processing.clear()
    gc.collect()

    classifications: dict[str, Any] = {}
    for family in families:
        family_processing = processing[family]
        classifier_args: dict[str, Any] = {}
        if family == "cvd_volume_orderflow":
            ref = int(family_processing["context"]["reference_timestamp"])
            classifier_args["clock"] = lambda ref=ref: ref

        # Builder context is assembled only for the family being classified.
        # Prices may reference CVD, but no builder receives the complete
        # Processing mapping.
        builder_context: dict[str, Mapping[str, Any]] = {family: family_processing}
        if family == "prices_ohlcv" and "cvd_volume_orderflow" in processing:
            builder_context["cvd_volume_orderflow"] = processing["cvd_volume_orderflow"]
        output_arg = _builder_arguments(builder_context, data_mode=data_mode, is_demo=is_demo).get(family, {})

        print(f"[{family}][CLASSIFICATION] RUN", flush=True)
        classifications[family] = run_classification_pipeline(
            processing_contracts={family: family_processing},
            enabled_families=(family,),
            classifier_arguments={family: classifier_args},
            output_arguments={family: output_arg},
        )[family]
        _atomic_write_json(contracts_root / "classification" / f"{family}.json", classifications[family])
        print(f"[{family}][CLASSIFICATION] OK", flush=True)

        # Processing is already persisted and no later classification depends
        # on the full family payload. Free it immediately.
        processing.pop(family, None)
        del family_processing, builder_context, output_arg
        gc.collect()

    screen_contracts = classifications

    exported = export_screen_contracts(
        screen_contracts,
        screens_contracts_root=screens_contracts_root,
    )

    # Preserve the current 1m candle if the full Prices calculation began
    # before a newer five-second fast-lane observation was published.
    if "prices_ohlcv" in families:
        runtime_root = Path(
            os.environ.get("TRADELATIN_RUNTIME_DIR", "").strip()
            or root / "data" / "runtime"
        )
        prices_state_path = runtime_root / "prices_fast_lane_state.json"
        try:
            prices_state = json.loads(prices_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            prices_state = None
        if isinstance(prices_state, Mapping):
            candles = prices_state.get("candles")
            if not isinstance(candles, Mapping) and isinstance(prices_state.get("candle"), Mapping):
                candles = {"1m": prices_state["candle"]}
            observed_at = prices_state.get("updated_at")
            if isinstance(candles, Mapping) and type(observed_at) is int:
                destination = exported["prices_ohlcv"]
                try:
                    latest_contract = json.loads(destination.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    latest_contract = None
                if isinstance(latest_contract, Mapping):
                    patched_contract = latest_contract
                    for timeframe, candle in candles.items():
                        if timeframe in {"1m", "5m"} and isinstance(candle, Mapping):
                            patched_contract = patch_prices_contract(
                                patched_contract, candle,
                                observed_at=observed_at, timeframe=str(timeframe),
                            )
                    _atomic_write_json(
                        destination,
                        patched_contract,
                    )

    # A structural Liquidity export must retain the newer five-second execution
    # tape. Reapply the persisted fast-lane state immediately after replacement.
    if "liquidity_microstructure" in families:
        runtime_root = Path(
            os.environ.get("TRADELATIN_RUNTIME_DIR", "").strip()
            or root / "data" / "runtime"
        )
        fast_state_path = runtime_root / "liquidity_fast_lane_state.json"
        try:
            fast_state = json.loads(fast_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            fast_state = None
        if isinstance(fast_state, Mapping):
            markets = fast_state.get("markets")
            if isinstance(markets, Mapping):
                tapes = {
                    market: node.get("event_tape", [])
                    for market, node in markets.items()
                    if isinstance(node, Mapping)
                }
                destination = exported["liquidity_microstructure"]
                try:
                    latest_contract = json.loads(destination.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    latest_contract = None
                observed_at = fast_state.get("updated_at")
                market_snapshots = fast_state.get("market_snapshots")
                if isinstance(latest_contract, Mapping) and type(observed_at) is int:
                    _atomic_write_json(
                        destination,
                        patch_screen_contract(
                            latest_contract, tapes, observed_at=observed_at,
                            snapshots=market_snapshots if isinstance(market_snapshots, Mapping) else None,
                        ),
                    )
    for family in families:
        print(f"[{family}][EXPORT] OK -> {exported[family]}", flush=True)

    return {
        "families": families,
        "artifacts": {
            "input": {family: str(contracts_root / "input" / f"{family}.json") for family in families},
            "processing": {family: str(contracts_root / "processing" / f"{family}.json") for family in families},
            "classification": {family: str(contracts_root / "classification" / f"{family}.json") for family in families},
        },
        "exported": {family: str(path) for family, path in exported.items()},
    }
