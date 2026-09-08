from __future__ import annotations

import os
import threading
import time

from processing_signals.input.long_short_liquidations.long_short_liquidations_data_raw_extract import (
    _fetch_worker_count,
    build_long_short_liquidations_fetch_plan,
    extract_long_short_liquidations_raw,
)


class _Owner:
    def __init__(self, source_mode: str) -> None:
        self.source_mode = source_mode

    def fetch(self, **_request):
        return {"data": []}


def test_emulator_defaults_to_sequential_fetching(monkeypatch) -> None:
    monkeypatch.delenv("TRADELATIN_LIQUIDATIONS_FETCH_WORKERS", raising=False)
    assert _fetch_worker_count(_Owner("emulator").fetch) == 1
    assert _fetch_worker_count(_Owner("live").fetch) == 6


def test_explicit_worker_override_is_respected_and_bounded(monkeypatch) -> None:
    owner = _Owner("emulator")
    monkeypatch.setenv("TRADELATIN_LIQUIDATIONS_FETCH_WORKERS", "4")
    assert _fetch_worker_count(owner.fetch) == 4
    monkeypatch.setenv("TRADELATIN_LIQUIDATIONS_FETCH_WORKERS", "999")
    assert _fetch_worker_count(owner.fetch) == 16
    monkeypatch.setenv("TRADELATIN_LIQUIDATIONS_FETCH_WORKERS", "0")
    assert _fetch_worker_count(owner.fetch) == 1


def test_live_parallel_execution_preserves_fetch_plan_order(monkeypatch) -> None:
    monkeypatch.setenv("TRADELATIN_LIQUIDATIONS_FETCH_WORKERS", "4")
    lock = threading.Lock()
    active = 0
    max_active = 0

    def fetcher(**request):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        # Different completion times make an unordered implementation visible.
        time.sleep(0.002 if request["endpoint_id"] == "pair_liquidation_map" else 0.004)
        with lock:
            active -= 1
        return {"data": [], "endpoint": request["endpoint_id"]}

    reference = 1_780_000_000
    expected = [
        item["request_id"]
        for item in build_long_short_liquidations_fetch_plan(
            mode="bootstrap", reference_timestamp=reference
        )
    ]
    raw = extract_long_short_liquidations_raw(
        fetcher=fetcher,
        mode="bootstrap",
        reference_timestamp=reference,
        execution_timestamp=reference,
    )

    assert [item["request_id"] for item in raw["requests"]] == expected
    assert max_active >= 2
