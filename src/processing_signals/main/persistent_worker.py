from __future__ import annotations

import contextlib
import gc
import os
import traceback
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .main_pipeline import run_main_pipeline


def run_family_job(job: Mapping[str, Any]) -> int:
    """Run one family inside an already-warm worker process.

    The worker returns only a status code.  Contracts remain filesystem-owned
    and are published atomically by the pipeline, so large JSON trees never
    cross the multiprocessing connection.
    """
    try:
        mode = str(job.get("mode") or "auto")
        requested_mode = None if mode == "auto" else mode
        output = open(os.devnull, "w", encoding="utf-8")
        try:
            with contextlib.redirect_stdout(output):
                run_main_pipeline(
                    repo_root=Path(str(job["repo_root"])),
                    screens_contracts_root=Path(str(job["contracts_root"])),
                    enabled_families=(str(job["family"]),),
                    requested_mode=requested_mode,
                    reference_timestamp=job.get("reference_timestamp"),
                    source_mode=str(job.get("source") or "emulator"),
                    data_mode=job.get("data_mode"),
                )
        finally:
            output.close()
        return 0
    except Exception:
        traceback.print_exc()
        return 1
    finally:
        # Long-lived workers must not retain cyclic references between cycles;
        # allocator arenas are bounded separately by periodic worker recycling.
        gc.collect()
