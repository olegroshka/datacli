"""Scoring backends: the pluggable "text -> values" step.

Every backend implements :class:`scoring.backends.base.Backend`: it declares an
``id`` (goes into the sidecar path and provenance), the ``kind`` of output it
produces (``"record"`` for schema fields, ``"vector"`` for embeddings), and
``score(items, schema)``. Registry: :func:`get_backend`.
"""

from __future__ import annotations

from typing import Any

from scoring.backends.base import Backend, Estimate, Item, Result

BACKENDS = ("vendor", "llm", "embed")


def get_backend(name: str, **kwargs: Any) -> Backend:
    """Instantiate a backend by name (lazy imports keep startup light)."""
    if name == "vendor":
        from scoring.backends.vendor import VendorBackend

        return VendorBackend()
    if name == "llm":
        from scoring.backends.llm import LLMBackend

        return LLMBackend(**kwargs)
    if name == "embed":
        from scoring.backends.embed import EmbedBackend

        return EmbedBackend(**kwargs)
    raise ValueError(f"unknown backend {name!r}. Known: {', '.join(BACKENDS)}")


__all__ = ["Backend", "Estimate", "Item", "Result", "BACKENDS", "get_backend"]
