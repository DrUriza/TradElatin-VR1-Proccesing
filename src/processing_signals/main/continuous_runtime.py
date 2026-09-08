from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import tempfile
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any
from uuid import uuid4

from .main_pipeline import FAMILY_ORDER
from .atomic_replace import replace_with_retry

# V4.2 runtime policy: ONLY these families are automatic.
AUTOMATIC_INTERVALS: dict[str, float] = {
    "prices_ohlcv": 5.0,
    # The five-second table/KPI lane publishes into the same atomic contract.
    # Structural Screen-B graph rebuilds are allowed a ten-second cadence.
    "liquidity_microstructure": 10.0,
    "cvd_volume_orderflow": 15.0,
}
MANUAL_FAMILIES = frozenset(set(FAMILY_ORDER) - set(AUTOMATIC_INTERVALS))

# Measured warm-cycle durations are used to launch work early enough that the
# *completed contract writes* (not merely process starts) land on cadence.
DEFAULT_DURATION_ESTIMATES: dict[str, float] = {
    "prices_ohlcv": 2.5,
    "liquidity_microstructure": 8.0,
    "cvd_volume_orderflow": 4.5,
}
INITIAL_PHASE_SECONDS: dict[str, float] = {
    "prices_ohlcv": 0.0,
    "cvd_volume_orderflow": 3.0,
    "liquidity_microstructure": 6.0,
}

FAMILY_CONSOLE_NAMES = {
    "prices_ohlcv": "Prices",
    "liquidity_microstructure": "Liquidity",
    "cvd_volume_orderflow": "CVD",
    "open_interest_and_funding": "Open Interest",
    "etf_exchange_flows": "ETF",
    "on_chain_miners": "On-Chain",
    "volatility_market_regimes": "Volatility",
    "long_short_liquidations": "Liquidations",
}


PersistentJobRunner = Callable[[Mapping[str, Any]], int]


def _persistent_worker_main(
    connection: Connection,
    runner: PersistentJobRunner,
    max_jobs: int,
) -> None:
    """Receive small job descriptors while keeping heavy imports warm."""
    completed = 0
    try:
        while completed < max_jobs:
            try:
                message = connection.recv()
            except EOFError:
                break
            if message is None:
                break
            job_id = str(message.get("job_id") or "")
            try:
                return_code = int(runner(message["payload"]))
            except Exception:
                # A family exception must not kill the resident worker or hide
                # the failed publication from the scheduler.
                traceback.print_exc()
                return_code = 1
            completed += 1
            retiring = completed >= max_jobs
            try:
                connection.send(
                    {
                        "job_id": job_id,
                        "return_code": return_code,
                        "retiring": retiring,
                    }
                )
            except (BrokenPipeError, EOFError, OSError):
                break
            if retiring:
                break
    finally:
        connection.close()


@dataclass
class _PersistentWorkerSlot:
    process: Any
    connection: Connection


class _PersistentJobHandle:
    """Popen-compatible completion surface for one resident-worker job."""

    def __init__(
        self,
        *,
        job_id: str,
        process: Any,
        connection: Connection,
    ) -> None:
        self.job_id = job_id
        self.process = process
        self.connection = connection
        self.retiring = False
        self.broken = False
        self._return_code: int | None = None

    def poll(self) -> int | None:
        if self._return_code is not None:
            return self._return_code
        available = self.connection.poll()
        if not available and not self.process.is_alive():
            # Allow the multiprocessing pipe feeder a brief final flush after
            # an intentional max-jobs worker retirement.
            available = self.connection.poll(0.05)
        if available:
            try:
                message = self.connection.recv()
            except (EOFError, OSError):
                self.broken = True
                self._return_code = int(self.process.exitcode or 1)
                return self._return_code
            if str(message.get("job_id") or "") != self.job_id:
                self.broken = True
                self._return_code = 1
                return self._return_code
            self.retiring = bool(message.get("retiring"))
            self._return_code = int(message.get("return_code", 1))
            return self._return_code
        if not self.process.is_alive():
            self.broken = True
            self._return_code = int(self.process.exitcode or 1)
            return self._return_code
        return None


def _console_time() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, allow_nan=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _request_families(payload: Any) -> tuple[str, ...]:
    if not isinstance(payload, Mapping):
        raise ValueError("request must be a JSON object")
    raw: Any = payload.get("families", payload.get("family"))
    if raw is None:
        raw = [name for name in FAMILY_ORDER if payload.get(name) is True]
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError("'family' must be a string or 'families' must be a list")
    families = tuple(dict.fromkeys(str(item).strip() for item in raw if str(item).strip()))
    unknown = [family for family in families if family not in FAMILY_ORDER]
    if unknown:
        raise ValueError(f"unsupported families: {', '.join(unknown)}")
    if not families:
        raise ValueError("request does not contain any family")
    return families


class ContinuousRuntime:
    """V4.2 process-isolated scheduler + HMI request consumer.

    Each family run is a separate Python process. This is intentional on Windows:
    Prices, Liquidity and CVD must be able to execute concurrently without the
    Python GIL or one slow family serialising the other two.
    """

    def __init__(
        self,
        *,
        command_builder: Callable[[str], list[str]],
        repo_root: str | Path,
        intervals: Mapping[str, float] | None = None,
        poll_seconds: float = 0.25,
        request_root: str | Path | None = None,
        runtime_root: str | Path | None = None,
        environment: Mapping[str, str] | None = None,
        persistent_job_builder: Callable[[str], Mapping[str, Any]] | None = None,
        persistent_job_runner: PersistentJobRunner | None = None,
        persistent_worker_max_jobs: int = 120,
    ) -> None:
        self.command_builder = command_builder
        self.repo_root = Path(repo_root)
        self.request_root = Path(
            request_root
            or os.environ.get("TRADELATIN_REFRESH_DIR", "").strip()
            or (self.repo_root / "data" / "refresh_requests")
        )
        self.runtime_root = Path(
            runtime_root
            or os.environ.get("TRADELATIN_RUNTIME_DIR", "").strip()
            or (self.repo_root / "data" / "runtime")
        )
        self.archive_root = self.runtime_root / "refresh_archive"
        self.status_path = self.runtime_root / "refresh_status.json"
        self.intervals = dict(AUTOMATIC_INTERVALS if intervals is None else intervals)
        self.poll_seconds = float(poll_seconds)
        self.environment = dict(os.environ if environment is None else environment)
        self.persistent_job_builder = persistent_job_builder
        self.persistent_job_runner = persistent_job_runner
        self.persistent_worker_max_jobs = int(persistent_worker_max_jobs)
        self._persistent_enabled = (
            persistent_job_builder is not None and persistent_job_runner is not None
        )
        self._mp_context = multiprocessing.get_context("spawn")
        self._persistent_workers: dict[str, _PersistentWorkerSlot] = {}
        self._processes: dict[str, Any] = {}
        self._active_manual: dict[str, set[str]] = {}
        self._pending_manual: dict[str, set[str]] = {}
        self._request_remaining: dict[str, set[str]] = {}
        self._failed_requests: set[str] = set()
        initial_clock = time.monotonic()
        self._next_due: dict[str, float] = {
            family: initial_clock + INITIAL_PHASE_SECONDS.get(family, 0.0)
            for family in self.intervals
        }
        self._cycle_count: dict[str, int] = {family: 0 for family in FAMILY_ORDER}
        self._started_monotonic: dict[str, float] = {}
        self._last_published_monotonic: dict[str, float] = {}
        self._duration_estimate: dict[str, float] = {
            family: min(
                float(seconds),
                DEFAULT_DURATION_ESTIMATES.get(family, float(seconds) * 0.5),
            )
            for family, seconds in self.intervals.items()
        }
        self._last_console_event = time.monotonic()
        self._heartbeat_seconds = 15.0
        self._status: dict[str, Any] = {"updated_at": _utc_now(), "requests": {}}
        if self.poll_seconds <= 0:
            raise ValueError("poll_seconds must be > 0")
        if any(seconds <= 0 for seconds in self.intervals.values()):
            raise ValueError("all family intervals must be > 0")
        if self.persistent_worker_max_jobs <= 0:
            raise ValueError("persistent_worker_max_jobs must be > 0")

    def _log(self, message: str) -> None:
        print(f"[{_console_time()}] {message}", flush=True)
        self._last_console_event = time.monotonic()

    def _heartbeat_if_quiet(self, now: float) -> None:
        if now - self._last_console_event < self._heartbeat_seconds:
            return
        active = []
        for family, process in self._processes.items():
            if process.poll() is None:
                started = self._started_monotonic.get(family, now)
                active.append(
                    f"{FAMILY_CONSOLE_NAMES.get(family, family)}:{now - started:.1f}s"
                )
        state = ", ".join(active) if active else "scheduler idle/waiting"
        self._log(f"[RUNTIME] ALIVE — {state}")

    def _write_status(self) -> None:
        self._status["updated_at"] = _utc_now()
        _atomic_json(self.status_path, self._status)

    def _set_request_status(self, request_id: str, **changes: Any) -> None:
        record = self._status["requests"].setdefault(request_id, {})
        record.update(changes)
        self._write_status()

    def _archive_request(self, path: Path, request_id: str) -> None:
        self.archive_root.mkdir(parents=True, exist_ok=True)
        destination = self.archive_root / f"{request_id}.json"
        counter = 1
        while destination.exists():
            destination = self.archive_root / f"{request_id}_{counter}.json"
            counter += 1
        replace_with_retry(path, destination)

    def _read_requests(self) -> None:
        self.request_root.mkdir(parents=True, exist_ok=True)
        for path in sorted(self.request_root.glob("*.json")):
            request_id = path.stem
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                families = _request_families(payload)
                self._request_remaining[request_id] = set(families)
                for family in families:
                    self._pending_manual.setdefault(family, set()).add(request_id)
                    print(
                        f"[REFRESH] REQUESTED  {family} request_id={request_id}",
                        flush=True,
                    )
                self._archive_request(path, request_id)
                self._set_request_status(
                    request_id,
                    status="accepted",
                    families=list(families),
                    accepted_at=_utc_now(),
                    error=None,
                )
            except Exception as exc:
                self._set_request_status(
                    request_id,
                    status="rejected",
                    rejected_at=_utc_now(),
                    error=f"{type(exc).__name__}: {exc}",
                )
                try:
                    self._archive_request(path, f"{request_id}_rejected")
                except OSError:
                    pass

    def _retire_persistent_worker(self, family: str, *, terminate: bool) -> None:
        slot = self._persistent_workers.pop(family, None)
        if slot is None:
            return
        try:
            if terminate and slot.process.is_alive():
                slot.process.terminate()
            elif slot.process.is_alive():
                try:
                    slot.connection.send(None)
                except (BrokenPipeError, EOFError, OSError):
                    pass
            slot.process.join(timeout=1.0)
            if slot.process.is_alive():
                slot.process.terminate()
                slot.process.join(timeout=1.0)
        finally:
            slot.connection.close()

    def _ensure_persistent_worker(self, family: str) -> _PersistentWorkerSlot:
        slot = self._persistent_workers.get(family)
        if slot is not None and slot.process.is_alive():
            return slot
        if slot is not None:
            self._retire_persistent_worker(family, terminate=False)
        parent_connection, child_connection = self._mp_context.Pipe(duplex=True)
        process = self._mp_context.Process(
            target=_persistent_worker_main,
            args=(
                child_connection,
                self.persistent_job_runner,
                self.persistent_worker_max_jobs,
            ),
            name=f"tradelatin-{family}-worker",
        )
        process.start()
        child_connection.close()
        slot = _PersistentWorkerSlot(process=process, connection=parent_connection)
        self._persistent_workers[family] = slot
        self._log(
            f"[{FAMILY_CONSOLE_NAMES.get(family, family)}] WORKER "
            f"pid={process.pid} recycle={self.persistent_worker_max_jobs} jobs"
        )
        return slot

    def _start_persistent_family(self, family: str) -> _PersistentJobHandle:
        if self.persistent_job_builder is None:
            raise RuntimeError("persistent job builder is not configured")
        slot = self._ensure_persistent_worker(family)
        job_id = f"{family}-{uuid4().hex}"
        payload = dict(self.persistent_job_builder(family))
        payload.setdefault("family", family)
        try:
            slot.connection.send({"job_id": job_id, "payload": payload})
        except (BrokenPipeError, EOFError, OSError):
            self._retire_persistent_worker(family, terminate=True)
            raise
        return _PersistentJobHandle(
            job_id=job_id,
            process=slot.process,
            connection=slot.connection,
        )

    def _start_family(self, family: str, *, manual_request_ids: set[str] | None = None) -> bool:
        active = self._processes.get(family)
        # Completion collection owns removal and publication accounting. Even
        # if a child exits between the collector's poll and this call, never
        # overwrite its handle or silently lose that completed publication.
        if active is not None:
            return False
        command = self.command_builder(family)
        self._cycle_count[family] = self._cycle_count.get(family, 0) + 1
        cycle = self._cycle_count[family]
        name = FAMILY_CONSOLE_NAMES.get(family, family)
        self._started_monotonic[family] = time.monotonic()
        self._log(f"[{name}] RUN  cycle={cycle}")
        if self._persistent_enabled and family in self.intervals:
            try:
                self._processes[family] = self._start_persistent_family(family)
            except (OSError, RuntimeError):
                # A local multiprocessing restriction must not prevent market
                # publication.  Fall back to the proven one-shot process for
                # this cycle and retry a resident worker next time.
                self._processes[family] = subprocess.Popen(
                    command,
                    cwd=self.repo_root,
                    env=self.environment,
                    stdout=subprocess.DEVNULL,
                    stderr=None,
                )
        else:
            self._processes[family] = subprocess.Popen(
                command,
                cwd=self.repo_root,
                env=self.environment,
                stdout=subprocess.DEVNULL,
                # Keep stderr visible so Python tracebacks are never hidden.
                stderr=None,
            )
        if manual_request_ids:
            self._active_manual[family] = set(manual_request_ids)
            for request_id in sorted(manual_request_ids):
                print(
                    f"[REFRESH] PROCESSING {family} request_id={request_id}",
                    flush=True,
                )
                self._set_request_status(request_id, status="running", started_at=_utc_now())
        return True

    def _collect_completed(self) -> None:
        for family, process in tuple(self._processes.items()):
            return_code = process.poll()
            if return_code is None:
                continue
            name = FAMILY_CONSOLE_NAMES.get(family, family)
            cycle = self._cycle_count.get(family, 0)
            started = self._started_monotonic.pop(family, time.monotonic())
            elapsed = max(0.0, time.monotonic() - started)
            published = time.monotonic()
            previous_publication = self._last_published_monotonic.get(family)
            publication_period = (
                None
                if previous_publication is None
                else max(0.0, published - previous_publication)
            )
            self._last_published_monotonic[family] = published

            # Schedule from completed publication using a warm-duration
            # estimate. The first cold cycle is intentionally not allowed to
            # distort the estimate; later cycles update it gradually. If work
            # cannot finish inside its interval, missed cycles are coalesced
            # into one immediate retry instead of becoming a backlog.
            interval = self.intervals.get(family)
            if interval is not None:
                estimate = self._duration_estimate.get(family, interval * 0.5)
                if previous_publication is not None:
                    observed = min(interval, elapsed)
                    estimate = (estimate * 0.75) + (observed * 0.25)
                    self._duration_estimate[family] = estimate
                self._next_due[family] = published + max(0.0, interval - estimate)
            due_in = max(
                0.0,
                self._next_due.get(family, published) - published,
            )
            period_text = (
                "first"
                if publication_period is None
                else f"period={publication_period:.2f}s"
            )
            if return_code == 0:
                self._log(
                    f"[{name}] OK   cycle={cycle} duration={elapsed:.2f}s "
                    f"{period_text} next~{due_in:.1f}s"
                )
                error = None
            else:
                error = f"child_process_exit_code:{return_code}"
                self._log(
                    f"[{name}] ERROR cycle={cycle} duration={elapsed:.2f}s {error}"
                )

            for request_id in self._active_manual.pop(family, set()):
                if request_id in self._failed_requests:
                    continue
                remaining = self._request_remaining.get(request_id, set())
                remaining.discard(family)
                if error is not None:
                    self._failed_requests.add(request_id)
                    self._request_remaining.pop(request_id, None)
                    self._set_request_status(
                        request_id,
                        status="error",
                        failed_family=family,
                        completed_at=_utc_now(),
                        error=error,
                    )
                elif not remaining:
                    self._request_remaining.pop(request_id, None)
                    self._set_request_status(
                        request_id,
                        status="completed",
                        completed_at=_utc_now(),
                        error=None,
                    )
                    print(
                        f"[REFRESH] COMPLETED  {family} request_id={request_id}",
                        flush=True,
                    )
            if isinstance(process, _PersistentJobHandle) and (
                process.retiring or process.broken
            ):
                self._retire_persistent_worker(
                    family,
                    terminate=process.broken,
                )
            del self._processes[family]

    def close(self) -> None:
        """Stop one-shot children and every resident family worker."""
        for process in self._processes.values():
            if isinstance(process, _PersistentJobHandle):
                continue
            if process.poll() is None:
                process.terminate()
        deadline = time.monotonic() + 5.0
        for process in self._processes.values():
            if isinstance(process, _PersistentJobHandle):
                continue
            timeout = max(0.0, deadline - time.monotonic())
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
        for family in tuple(self._persistent_workers):
            self._retire_persistent_worker(
                family,
                terminate=family in self._processes,
            )
        self._processes.clear()

    def run_forever(self) -> None:
        self.request_root.mkdir(parents=True, exist_ok=True)
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self._write_status()
        self._log("[RUNTIME] continuous scheduler started")
        self._log(
            "[RUNTIME] automatic: "
            + ", ".join(
                f"{FAMILY_CONSOLE_NAMES.get(family, family)}={seconds:g}s"
                for family, seconds in self.intervals.items()
            )
        )
        self._log(
            "[RUNTIME] manual only: "
            + ", ".join(FAMILY_CONSOLE_NAMES.get(f, f) for f in sorted(MANUAL_FAMILIES))
        )
        self._log(
            "[RUNTIME] structural workers: "
            + ("persistent/recycled" if self._persistent_enabled else "one-shot")
        )
        try:
            while True:
                now = time.monotonic()
                self._collect_completed()
                self._read_requests()

                for family, interval in self.intervals.items():
                    if now >= self._next_due[family]:
                        self._start_family(family)

                for family, request_ids in tuple(self._pending_manual.items()):
                    if self._start_family(family, manual_request_ids=request_ids):
                        del self._pending_manual[family]

                self._heartbeat_if_quiet(now)
                time.sleep(self.poll_seconds)
        except KeyboardInterrupt:
            print("TradELATIN V4.2 continuous runtime stopping...", flush=True)
        finally:
            self.close()
            print("TradELATIN V4.2 continuous runtime stopped", flush=True)
