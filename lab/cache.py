"""A tiny on-disk cache for model responses.

Keyed by a hash of ``(model, messages, temperature)`` so identical grounded calls
(temperature 0) are free and deterministic on re-run. One JSON file per key; the
cache is safe to delete at any time.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def make_key(model: str, messages: list[dict[str, Any]], temperature: float) -> str:
    """Stable content hash for a model call."""
    payload = json.dumps(
        {"model": model, "messages": messages, "temperature": temperature},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ResponseCache:
    """Filesystem-backed cache of model responses (``<cache_dir>/<key>.json``)."""

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = Path(cache_dir)

    def _path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def put(self, key: str, value: dict[str, Any]) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._path(key).write_text(
                json.dumps(value, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass  # cache is best-effort; never break a run over it

    def count(self) -> int:
        try:
            return sum(1 for _ in self.cache_dir.glob("*.json"))
        except OSError:
            return 0
