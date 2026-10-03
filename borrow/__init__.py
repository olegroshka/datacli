"""The borrow source: daily snapshots of a broker's shortable list (DD-002 WP6).

Importing the package puts ``eodhd/`` on ``sys.path`` so the shared helpers
(``config``, ``_atomic``, ``_cmdtable``, ``_render``) resolve as they do for
the finra and sec sources.
"""

from __future__ import annotations

import sys
from pathlib import Path

_EODHD = Path(__file__).resolve().parents[1] / "eodhd"
if str(_EODHD) not in sys.path:
    sys.path.insert(0, str(_EODHD))
