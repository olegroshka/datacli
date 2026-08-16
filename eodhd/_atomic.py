"""Atomic file writes for the data lanes.

Every parquet / CSV output is written to ``<name>.tmp`` next to its target and
then renamed over it (``os.replace`` is atomic on the same filesystem). Readers
-- DuckDB views, ``status``, the sync engine -- therefore never see a partially
written file during a refresh, and a crash mid-write leaves the previous
version intact instead of a truncated dataset.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pandas as pd


def _tmp(path: Path) -> Path:
    return path.with_name(path.name + ".tmp")


def to_parquet(df: pd.DataFrame, path: Path | str, **kwargs: Any) -> None:
    """``df.to_parquet(path, **kwargs)`` via a temp file + atomic rename."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp(target)
    df.to_parquet(tmp, **kwargs)
    os.replace(tmp, target)


def to_csv(df: pd.DataFrame, path: Path | str, **kwargs: Any) -> None:
    """``df.to_csv(path, **kwargs)`` via a temp file + atomic rename."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp(target)
    df.to_csv(tmp, **kwargs)
    os.replace(tmp, target)


def write_table(table: Any, path: Path | str, **kwargs: Any) -> None:
    """``pyarrow.parquet.write_table(table, path, **kwargs)`` atomically."""
    import pyarrow.parquet as pq

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp(target)
    pq.write_table(table, tmp, **kwargs)
    os.replace(tmp, target)
