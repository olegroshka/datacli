"""Atomic file writes for the data lanes.

Every parquet / CSV output is written to ``<name>.tmp`` next to its target and
then renamed over it (``os.replace`` is atomic on the same filesystem). Readers
-- DuckDB views, ``status``, the sync engine -- therefore never see a partially
written file during a refresh, and a crash mid-write leaves the previous
version intact instead of a truncated dataset.

On Windows the rename itself can fail transiently with ``PermissionError``
(a sharing violation) while another process -- an indexer, a virus scanner, a
reader that has the old file open -- holds the target for a moment. The FINRA
backfill lost a day's state row to exactly that after 898 clean days, so the
rename is retried a few times with short pauses before the error propagates.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable

import pandas as pd

REPLACE_ATTEMPTS = 6
REPLACE_PAUSE = 0.25  # seconds, doubled each attempt: 0.25 .. 8s in total


def _tmp(path: Path) -> Path:
    return path.with_name(path.name + ".tmp")


def replace_with_retry(
    tmp: Path,
    target: Path,
    *,
    attempts: int = REPLACE_ATTEMPTS,
    pause: float = REPLACE_PAUSE,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """``os.replace(tmp, target)``, retrying a transient ``PermissionError``."""
    for attempt in range(1, attempts + 1):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            if attempt == attempts:
                raise
            sleep(pause * (2 ** (attempt - 1)))


def to_parquet(df: pd.DataFrame, path: Path | str, **kwargs: Any) -> None:
    """``df.to_parquet(path, **kwargs)`` via a temp file + atomic rename."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp(target)
    try:
        df.to_parquet(tmp, **kwargs)
        replace_with_retry(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def to_csv(df: pd.DataFrame, path: Path | str, **kwargs: Any) -> None:
    """``df.to_csv(path, **kwargs)`` via a temp file + atomic rename."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp(target)
    try:
        df.to_csv(tmp, **kwargs)
        replace_with_retry(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def write_table(table: Any, path: Path | str, **kwargs: Any) -> None:
    """``pyarrow.parquet.write_table(table, path, **kwargs)`` atomically."""
    import pyarrow.parquet as pq

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp(target)
    try:
        pq.write_table(table, tmp, **kwargs)
        replace_with_retry(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
