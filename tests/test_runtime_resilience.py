import os
import time
from pathlib import Path

from processing_signals.main import atomic_replace
from processing_signals.main import continuous_runtime
from processing_signals.processing.cvd_volume_orderflow.cvd_volume_orderflow_processor import (
    _aligned_difference,
    _price_records_with_spot_fallback,
)


def test_cvd_cross_market_difference_aligns_unequal_series_by_timestamp():
    assert _aligned_difference(
        [100, 200, 300], [1.0, 2.0, 3.0],
        [200, 300, 400, 500], [0.5, 1.5, 9.0, 10.0],
    ) == [None, 1.5, 1.5]


def test_cvd_futures_price_divergence_uses_canonical_spot_when_futures_is_empty():
    spot = [
        {"timestamp": 100, "close": 50_000.0},
        {"timestamp": 200, "close": 50_100.0},
    ]
    history = {
        "spot": {"5m": spot},
        "futures": {"5m": []},
    }

    assert _price_records_with_spot_fallback(history, "futures", "5m") is spot


def test_cvd_futures_price_divergence_prefers_native_futures_when_available():
    spot = [{"timestamp": 100, "close": 50_000.0}]
    futures = [{"timestamp": 100, "close": 50_010.0}]
    history = {
        "spot": {"5m": spot},
        "futures": {"5m": futures},
    }

    assert _price_records_with_spot_fallback(history, "futures", "5m") is futures


def test_atomic_replace_retries_transient_permission_error(monkeypatch, tmp_path: Path):
    source = tmp_path / "source.tmp"
    destination = tmp_path / "destination.json"
    source.write_text("new", encoding="utf-8")
    destination.write_text("old", encoding="utf-8")
    real_replace = atomic_replace.os.replace
    calls = 0

    def flaky_replace(src, dst):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise PermissionError("transient lock")
        return real_replace(src, dst)

    monkeypatch.setattr(atomic_replace.os, "replace", flaky_replace)
    monkeypatch.setattr(atomic_replace.time, "sleep", lambda _: None)
    atomic_replace.replace_with_retry(source, destination)
    assert calls == 3
    assert destination.read_text(encoding="utf-8") == "new"


class _CompletedProcess:
    def poll(self) -> int:
        return 0


def _record_worker_pid(job) -> int:
    path = Path(job["pid_path"])
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{os.getpid()}\n")
    return 0


def _wait_for_release(job) -> int:
    pid_path = Path(job["pid_path"])
    pid_path.write_text(str(os.getpid()), encoding="utf-8")
    release_path = Path(job["release_path"])
    deadline = time.monotonic() + 10.0
    while not release_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    return 0 if release_path.exists() else 1


def _runtime(tmp_path: Path, interval: float) -> continuous_runtime.ContinuousRuntime:
    return continuous_runtime.ContinuousRuntime(
        command_builder=lambda _family: ["unused"],
        repo_root=tmp_path,
        intervals={"prices_ohlcv": interval},
        runtime_root=tmp_path / "runtime",
        request_root=tmp_path / "requests",
    )


def test_scheduler_targets_write_to_write_period(monkeypatch, tmp_path: Path):
    runtime = _runtime(tmp_path, 5.0)
    runtime._duration_estimate["prices_ohlcv"] = 2.0
    runtime._last_published_monotonic["prices_ohlcv"] = 97.0
    runtime._processes["prices_ohlcv"] = _CompletedProcess()
    runtime._started_monotonic["prices_ohlcv"] = 100.0
    monkeypatch.setattr(continuous_runtime.time, "monotonic", lambda: 102.0)

    runtime._collect_completed()

    # A two-second run waits three seconds so the next completed publication
    # targets the five-second contract-write cadence.
    assert runtime._next_due["prices_ohlcv"] == 105.0


def test_scheduler_coalesces_overrun_without_backlog(monkeypatch, tmp_path: Path):
    runtime = _runtime(tmp_path, 5.0)
    runtime._duration_estimate["prices_ohlcv"] = 5.0
    runtime._last_published_monotonic["prices_ohlcv"] = 97.0
    runtime._processes["prices_ohlcv"] = _CompletedProcess()
    runtime._started_monotonic["prices_ohlcv"] = 90.0
    monkeypatch.setattr(continuous_runtime.time, "monotonic", lambda: 102.0)

    runtime._collect_completed()

    # A twelve-second run missed multiple five-second slots. Only one next run
    # becomes due immediately; no historical jobs are queued.
    assert runtime._next_due["prices_ohlcv"] == 102.0


def test_scheduler_never_overwrites_an_uncollected_completed_process(tmp_path: Path):
    runtime = _runtime(tmp_path, 5.0)
    completed = _CompletedProcess()
    runtime._processes["prices_ohlcv"] = completed

    assert runtime._start_family("prices_ohlcv") is False
    assert runtime._processes["prices_ohlcv"] is completed
    assert runtime._cycle_count["prices_ohlcv"] == 0


def test_persistent_worker_reuses_imported_process_and_recycles(tmp_path: Path):
    pid_path = tmp_path / "worker-pids.txt"
    pid_path.write_text("", encoding="utf-8")
    runtime = continuous_runtime.ContinuousRuntime(
        command_builder=lambda _family: ["unused"],
        repo_root=tmp_path,
        intervals={"prices_ohlcv": 5.0},
        runtime_root=tmp_path / "runtime",
        request_root=tmp_path / "requests",
        persistent_job_builder=lambda family: {
            "family": family,
            "pid_path": str(pid_path),
        },
        persistent_job_runner=_record_worker_pid,
        persistent_worker_max_jobs=2,
    )

    def complete_cycle() -> None:
        assert runtime._start_family("prices_ohlcv") is True
        deadline = time.monotonic() + 10.0
        while "prices_ohlcv" in runtime._processes and time.monotonic() < deadline:
            runtime._collect_completed()
            time.sleep(0.01)
        assert "prices_ohlcv" not in runtime._processes

    try:
        complete_cycle()
        first_process = runtime._persistent_workers["prices_ohlcv"].process
        complete_cycle()
        assert "prices_ohlcv" not in runtime._persistent_workers
        complete_cycle()
        second_process = runtime._persistent_workers["prices_ohlcv"].process
        assert second_process is not first_process
    finally:
        runtime.close()

    pids = pid_path.read_text(encoding="utf-8").splitlines()
    assert len(pids) == 3
    assert pids[0] == pids[1]


def test_persistent_worker_is_recreated_after_it_dies_during_a_job(tmp_path: Path):
    pid_path = tmp_path / "worker-pid.txt"
    release_path = tmp_path / "release"
    runtime = continuous_runtime.ContinuousRuntime(
        command_builder=lambda _family: ["unused"],
        repo_root=tmp_path,
        intervals={"prices_ohlcv": 5.0},
        runtime_root=tmp_path / "runtime",
        request_root=tmp_path / "requests",
        persistent_job_builder=lambda family: {
            "family": family,
            "pid_path": str(pid_path),
            "release_path": str(release_path),
        },
        persistent_job_runner=_wait_for_release,
    )

    try:
        assert runtime._start_family("prices_ohlcv") is True
        deadline = time.monotonic() + 10.0
        while not pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        first_worker = runtime._persistent_workers["prices_ohlcv"].process
        first_pid = first_worker.pid
        first_worker.terminate()
        first_worker.join(timeout=5.0)

        deadline = time.monotonic() + 10.0
        while "prices_ohlcv" in runtime._processes and time.monotonic() < deadline:
            runtime._collect_completed()
            time.sleep(0.01)
        assert "prices_ohlcv" not in runtime._processes
        assert "prices_ohlcv" not in runtime._persistent_workers

        release_path.touch()
        assert runtime._start_family("prices_ohlcv") is True
        second_worker = runtime._persistent_workers["prices_ohlcv"].process
        assert second_worker.pid != first_pid
        deadline = time.monotonic() + 10.0
        while "prices_ohlcv" in runtime._processes and time.monotonic() < deadline:
            runtime._collect_completed()
            time.sleep(0.01)
        assert "prices_ohlcv" not in runtime._processes
    finally:
        runtime.close()


def test_manual_reload_of_automatic_family_completes_on_persistent_worker(tmp_path: Path):
    pid_path = tmp_path / "worker-pids.txt"
    pid_path.write_text("", encoding="utf-8")
    request_id = "reload-prices"
    runtime = continuous_runtime.ContinuousRuntime(
        command_builder=lambda _family: ["unused"],
        repo_root=tmp_path,
        intervals={"prices_ohlcv": 5.0},
        runtime_root=tmp_path / "runtime",
        request_root=tmp_path / "requests",
        persistent_job_builder=lambda family: {
            "family": family,
            "pid_path": str(pid_path),
        },
        persistent_job_runner=_record_worker_pid,
    )
    runtime._request_remaining[request_id] = {"prices_ohlcv"}

    try:
        assert runtime._start_family(
            "prices_ohlcv", manual_request_ids={request_id}
        ) is True
        deadline = time.monotonic() + 10.0
        while "prices_ohlcv" in runtime._processes and time.monotonic() < deadline:
            runtime._collect_completed()
            time.sleep(0.01)
        assert runtime._status["requests"][request_id]["status"] == "completed"
        assert "prices_ohlcv" in runtime._persistent_workers
    finally:
        runtime.close()
