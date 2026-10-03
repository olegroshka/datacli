"""The registers source: public net short position registers, per holder (DD-003 WP17).

Importing the package puts ``eodhd/`` on ``sys.path`` so the shared helpers
(``config``, ``_atomic``, ``_cmdtable``, ``_render``) resolve as they do for
the finra, sec and borrow sources.
"""

from __future__ import annotations

import sys
from pathlib import Path

_EODHD = Path(__file__).resolve().parents[1] / "eodhd"
if str(_EODHD) not in sys.path:
    sys.path.insert(0, str(_EODHD))
