"""Windows-tolerant atomic file replacement."""
from __future__ import annotations

import os
from pathlib import Path
import time


def replace_with_retry(source: str | Path, destination: str | Path, *, attempts: int = 10) -> None:
    """Replace destination atomically, retrying transient Windows share locks."""
    if attempts < 1:
        raise ValueError("attempts must be positive")
    for attempt in range(attempts):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(min(0.01 * (2 ** attempt), 0.5))


__all__ = ["replace_with_retry"]
