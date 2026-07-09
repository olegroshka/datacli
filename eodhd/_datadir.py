"""Resolve the EODHD raw-data root.

``datacli`` was extracted from ``btest``; the raw snapshots (~GBs) stay in
``btest`` for now. By default this points at the ``btest`` sibling repo's
``data/raw/eodhd`` directory; override with the ``EODHD_DATA_ROOT`` environment
variable (set it to the absolute ``.../data/raw/eodhd`` path you want to use).
"""

from __future__ import annotations

import os
from pathlib import Path


def eodhd_raw_root() -> Path:
    """Directory that holds the per-lane EODHD raw snapshots."""
    env = os.environ.get("EODHD_DATA_ROOT")
    if env:
        return Path(env)
    # this file: <PycharmProjects>/datacli/eodhd/_datadir.py -> parents[2] == PycharmProjects
    return Path(__file__).resolve().parents[2] / "btest" / "data" / "raw" / "eodhd"


EODHD_RAW_ROOT = eodhd_raw_root()
