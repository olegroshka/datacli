"""Where the derived positioning datasets live.

Resolution precedence, mirroring the finra source:

    DATACLI_POSITIONING_ROOT env var  >  datacli.toml [positioning].data_root  >  default

where the default is a ``positioning`` sibling of the resolved eodhd data root.
Everything under it is derived from other roots and can be rebuilt.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_ROOT = "DATACLI_POSITIONING_ROOT"
SECTION = "positioning"


def resolve_root() -> tuple[Path, str]:
    """The positioning root and where it came from: ``env``, ``config`` or ``default``."""
    explicit = os.environ.get(ENV_ROOT)
    if explicit:
        return Path(explicit).expanduser(), "env"
    import config as eodhd_config  # type: ignore[import-not-found]

    override = eodhd_config.section(SECTION).get("data_root")
    if override:
        return Path(str(override)).expanduser(), "config"
    eodhd_root, _ = eodhd_config.eodhd_data_root()
    return Path(eodhd_root).parent / SECTION, "default"


def positioning_root() -> Path:
    return resolve_root()[0]
