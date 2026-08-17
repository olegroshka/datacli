"""Repo-local configuration for datacli (``datacli.toml`` at the repo root).

The eodhd data root is resolved with this precedence:

    EODHD_DATA_ROOT env var  >  datacli.toml [eodhd].data_root  >  default

where the default is the ``btest`` sibling repo's ``data/raw/eodhd`` (the tool's
origin). Point datacli at your own data with:

    eodhd> config set data-root  D:\\my\\data\\raw\\eodhd

or by editing ``datacli.toml`` directly (see ``datacli.example.toml``).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# eodhd/config.py -> parents[1] == the datacli repo root.  A scheduler runner
# supplies an explicit, validated config identity through the environment.
REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = (
    Path(os.environ.get("DATACLI_CONFIG_PATH", str(REPO_ROOT / "datacli.toml")))
    .expanduser()
    .resolve()
)


def _load() -> dict[str, dict[str, Any]]:
    if not CONFIG_PATH.exists():
        return {}
    import tomllib

    try:
        return tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get(section: str, key: str, default: Any = None) -> Any:
    return _load().get(section, {}).get(key, default)


def section(name: str) -> dict[str, Any]:
    """A whole config section as a plain dict (empty when missing)."""
    return dict(_load().get(name, {}))


def _write(data: dict[str, dict[str, Any]]) -> None:
    # Minimal TOML writer for our flat string-valued sections (no tomli_w dep).
    lines: list[str] = []
    for section, kv in data.items():
        lines.append(f"[{section}]")
        for key, value in kv.items():
            escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{key} = "{escaped}"')
        lines.append("")
    from scheduler.commands import config_write_lock

    with config_write_lock(CONFIG_PATH):
        CONFIG_PATH.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")


def set_value(section: str, key: str, value: str) -> Path:
    """Persist ``[section] key = value`` into datacli.toml (preserving other keys)."""
    data = _load()
    data.setdefault(section, {})[key] = value
    _write(data)
    return CONFIG_PATH


def eodhd_data_root() -> tuple[Path, str]:
    """Resolve the eodhd raw-data root and where it came from.

    Returns ``(path, source)`` with ``source`` in ``{"env", "config", "default"}``.
    """
    env = os.environ.get("EODHD_DATA_ROOT")
    if env:
        return Path(env), "env"
    cfg = get("eodhd", "data_root")
    if cfg:
        return Path(cfg), "config"
    return REPO_ROOT.parent / "btest" / "data" / "raw" / "eodhd", "default"
