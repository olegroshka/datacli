"""The ``finra`` source: FINRA Query API + public daily files, as a datacli peer.

Layers (see ``docs/FINRA_SOURCE_DESIGN.md``):

- transport: :mod:`finra.auth` (credentials, FIP bearer tokens),
  :mod:`finra.api` (Query API client), :mod:`finra.cdn` (daily files);
- dataset: :mod:`finra.registry` and the per-dataset providers;
- store: :mod:`finra.store`;
- surface: :mod:`finra.cli`, :mod:`finra.views`, the shell plugin.

Credentials come only from ``FINRA_CLIENT_ID`` and ``FINRA_API_KEY`` in the
environment (or the Windows user environment); they are never stored.

Importing the package puts ``eodhd/`` on ``sys.path`` so the shared helpers
(``_http``, ``_atomic``, ``_render``, ``_cmdtable``, ``config``) import flat,
the way every other source does.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_EODHD = REPO_ROOT / "eodhd"
if str(_EODHD) not in sys.path:
    sys.path.insert(0, str(_EODHD))
