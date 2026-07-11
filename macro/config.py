"""Where macro data lives.

By default macro parquet sits next to the eodhd snapshots (a ``macro`` sibling of
the resolved eodhd data root), so it travels with the rest of the data. Override
with ``[macro].data_root`` in ``datacli.toml``.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "datacli.toml"

FRED_PARQUET = "fred_observations.parquet"
EODHD_PARQUET = "eodhd_indicators.parquet"
EODHD_MARKET_PARQUET = "eodhd_market.parquet"
OBSERVATIONS = FRED_PARQUET  # back-compat (Phase 2.5)

_EODHD = REPO_ROOT / "eodhd"
if str(_EODHD) not in sys.path:
    sys.path.insert(0, str(_EODHD))


def _read_section() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        data = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    section = data.get("macro", {})
    return section if isinstance(section, dict) else {}


def _eodhd_root() -> Path:
    from _datadir import EODHD_RAW_ROOT  # type: ignore[import-not-found]

    return Path(EODHD_RAW_ROOT)


def macro_root() -> Path:
    override = _read_section().get("data_root")
    return Path(override) if override else (_eodhd_root().parent / "macro")


def fred_path() -> Path:
    return macro_root() / FRED_PARQUET


def eodhd_path() -> Path:
    return macro_root() / EODHD_PARQUET


def eodhd_market_path() -> Path:
    return macro_root() / EODHD_MARKET_PARQUET


def observations_path() -> Path:  # back-compat
    return fred_path()
