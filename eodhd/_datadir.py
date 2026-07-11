"""Resolve the EODHD raw-data root (delegates to ``config``).

Precedence: ``EODHD_DATA_ROOT`` env > ``datacli.toml`` [eodhd].data_root > the
``btest`` sibling default. Point datacli at your own data with
``config set data-root <path>`` (see ``config.py`` / ``datacli.example.toml``).
"""

from __future__ import annotations

from pathlib import Path

from config import eodhd_data_root  # type: ignore[import-not-found]


def eodhd_raw_root() -> Path:
    """Directory that holds the per-lane EODHD raw snapshots."""
    return eodhd_data_root()[0]


EODHD_RAW_ROOT = eodhd_raw_root()
