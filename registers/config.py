"""Where the register histories live.

    DATACLI_REGISTERS_ROOT env var  >  datacli.toml [registers].data_root  >  default

where the default is a ``registers`` sibling of the resolved eodhd data root.
The regulators republish the whole history each day, so the files are
replaceable in principle; the root is still a sync unit because a regulator
can stop publishing (the FCA's per-holder file froze on 2026-07-11).
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_ROOT = "DATACLI_REGISTERS_ROOT"
SECTION = "registers"


def resolve_root() -> tuple[Path, str]:
    """The registers root and where it came from: ``env``, ``config`` or ``default``."""
    explicit = os.environ.get(ENV_ROOT)
    if explicit:
        return Path(explicit).expanduser(), "env"
    import config as eodhd_config  # type: ignore[import-not-found]

    override = eodhd_config.section(SECTION).get("data_root")
    if override:
        return Path(str(override)).expanduser(), "config"
    eodhd_root, _ = eodhd_config.eodhd_data_root()
    return Path(eodhd_root).parent / SECTION, "default"


def registers_root() -> Path:
    return resolve_root()[0]
