"""Where FINRA data lives.

Resolution precedence, mirroring the macro source:

    DATACLI_FINRA_ROOT env var  >  datacli.toml [finra].data_root  >  default

where the default is a ``finra`` sibling of the resolved eodhd data root, so the
FINRA store travels with the rest of the raw data. The TOML section is read
through the shared ``eodhd/config.py`` loader rather than a third copy of it.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_ROOT = "DATACLI_FINRA_ROOT"
SECTION = "finra"


def resolve_root() -> tuple[Path, str]:
    """The FINRA data root and where it came from: ``env``, ``config`` or ``default``."""
    explicit = os.environ.get(ENV_ROOT)
    if explicit:
        return Path(explicit).expanduser(), "env"
    import config as eodhd_config  # type: ignore[import-not-found]

    override = eodhd_config.section(SECTION).get("data_root")
    if override:
        return Path(str(override)).expanduser(), "config"
    eodhd_root, _ = eodhd_config.eodhd_data_root()
    return Path(eodhd_root).parent / "finra", "default"


def finra_root() -> Path:
    return resolve_root()[0]


def dataset_dir(name: str, *, root: Path | None = None) -> Path:
    """``<root>/<dataset>`` -- each dataset owns one directory under the root."""
    base = Path(root) if root is not None else finra_root()
    return base / name
