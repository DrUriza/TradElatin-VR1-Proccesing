"""Five-second publication lane for active Prices 1m and 5m candles."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any

from processing_signals.main.atomic_replace import replace_with_retry

FAMILY = "prices_ohlcv"
CADENCE_SECONDS = 5.0
STATE_SCHEMA = "trad_elatin.prices.fast-lane-state.v1"
FAST_TIMEFRAMES = ("1m", "5m")


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _latest_candle(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    rows = payload.get("data")
    if isinstance(rows, Mapping):
        rows = rows.get("data")
    if not isinstance(rows, list) or not rows:
        return None
    row = rows[-1]
    if not isinstance(row, Mapping):
        return None
    try:
        timestamp = int(row.get("time", row.get("timestamp")))
        if timestamp > 10_000_000_000:
            timestamp //= 1000
        candle = {
            "timestamp": timestamp,
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume_usd": float(row.get("volume_usd", row.get("volume", 0.0))),
        }
    except (KeyError, TypeError, ValueError):
        return None
    return candle if timestamp > 0 else None


def patch_prices_contract(
    contract: Mapping[str, Any], candle: Mapping[str, Any], *, observed_at: int,
    timeframe: str = "1m",
) -> dict[str, Any]:
    """Replace/append one active fast-timeframe candle."""
    if timeframe not in FAST_TIMEFRAMES:
        raise ValueError(f"unsupported_prices_fast_timeframe:{timeframe}")
    out = deepcopy(dict(contract))
    try:
        block = out["charts"]["ohlcv"]["markets"]["spot"]["timeframes"][timeframe]
    except (KeyError, TypeError):
        return out
    records = block.get("records")
    if not isinstance(records, list):
        return out

    fresh = dict(candle)
    fresh_timestamp = int(fresh["timestamp"])
    if records and isinstance(records[-1], Mapping) and int(records[-1].get("timestamp") or 0) == fresh_timestamp:
        records[-1] = fresh
    elif not records or int(records[-1].get("timestamp") or 0) < fresh_timestamp:
        records.append(fresh)
        del records[:-500]
    else:
        return out

    metadata = block.setdefault("metadata", {})
    if isinstance(metadata, dict):
        metadata["fast_lane"] = True
        metadata["fast_lane_data_as_of"] = observed_at

    items = out.get("kpis", {}).get("items", []) if isinstance(out.get("kpis"), Mapping) else []
    if timeframe == "1m" and isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and item.get("metric_id") == "last_price":
                item.update({"value": float(fresh["close"]), "status": "available", "reason": None})
                break

    context = out.setdefault("context", {})
    if isinstance(context, dict):
        context["prices_fast_lane_data_as_of"] = observed_at
    quality = out.setdefault("quality", {})
    if isinstance(quality, dict):
        extensions = quality.setdefault("extensions", {})
        if isinstance(extensions, dict):
            extensions["prices_fast_lane_v1"] = {
                "status": "available",
                "cadence_seconds": CADENCE_SECONDS,
                "timeframes": list(FAST_TIMEFRAMES),
                "surface": [
                    "charts.ohlcv.markets.spot.timeframes.1m.records",
                    "charts.ohlcv.markets.spot.timeframes.5m.records",
                    "kpis.items.last_price",
                ],
                "technical_indicators_recalculated": False,
                "data_as_of": observed_at,
            }
    return out


def run_prices_fast_lane_cycle(
    *, fetcher: Callable[..., Any], screen_contract_path: str | Path,
    state_path: str | Path, observed_at: int | None = None,
) -> dict[str, Any]:
    screen_path = Path(screen_contract_path)
    contract = _read_json(screen_path)
    if not contract:
        return {"status": "waiting_for_structural_contract", "patched": False}
    now = int(observed_at or time.time())
    candles: dict[str, dict[str, Any]] = {}
    patched = contract
    for timeframe in FAST_TIMEFRAMES:
        payload = fetcher(
            provider="coinglass", endpoint_id="spot_ohlcv", path="/api/spot/price/history",
            params={"exchange": "Binance", "symbol": "BTCUSDT", "interval": timeframe, "limit": 500},
        )
        candle = _latest_candle(payload)
        if candle is None:
            continue
        candles[timeframe] = candle
        patched = patch_prices_contract(patched, candle, observed_at=now, timeframe=timeframe)
    if not candles:
        return {"status": "no_candle", "patched": False}
    _atomic_json(screen_path, patched)
    state = {"schema": STATE_SCHEMA, "updated_at": now, "candles": candles}
    _atomic_json(Path(state_path), state)
    return {"status": "updated", "patched": True, "candles": candles, "candle": candles.get("1m")}


__all__ = ["CADENCE_SECONDS", "FAST_TIMEFRAMES", "patch_prices_contract", "run_prices_fast_lane_cycle"]
