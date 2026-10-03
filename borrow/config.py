"""Where borrow snapshots live.

    DATACLI_BORROW_ROOT env var  >  datacli.toml [borrow].data_root  >  default

where the default is a ``borrow`` sibling of the resolved eodhd data root. The
snapshots are irreplaceable (the broker publishes no history), so the root is
a sync unit and is backed up like the finra and macro roots.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_ROOT = "DATACLI_BORROW_ROOT"
SECTION = "borrow"


def resolve_root() -> tuple[Path, str]:
    """The borrow root and where it came from: ``env``, ``config`` or ``default``."""
    explicit = os.environ.get(ENV_ROOT)
    if explicit:
        return Path(explicit).expanduser(), "env"
    import config as eodhd_config  # type: ignore[import-not-found]

    override = eodhd_config.section(SECTION).get("data_root")
    if override:
        return Path(str(override)).expanduser(), "config"
    eodhd_root, _ = eodhd_config.eodhd_data_root()
    return Path(eodhd_root).parent / SECTION, "default"


def borrow_root() -> Path:
    return resolve_root()[0]
