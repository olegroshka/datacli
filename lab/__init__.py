"""Raw Data Lab -- a grounded EDA copilot for the pre-signal stage.

See DESIGN.md for the full design. Phase 0 provides the substrate: model access
(LiteLLM), budget + response cache, the ``Finding`` type, ``[lab]`` config, and
the ``lab config`` command. LLM access is optional and lazily imported, so the
core shell runs without the ``lab`` extra installed.
"""

from __future__ import annotations

__all__ = ["types", "config", "cache", "models"]
