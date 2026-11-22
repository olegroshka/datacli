"""The ``alpaca`` source: Alpaca's market data API as a datacli peer.

The first dataset is ``auctions``: the official opening and closing auction
prints of US stocks and ETFs on the consolidated tape (SIP), from 2016.

Layers (see ``docs/AUCTIONS_AND_LINKAGE_DESIGN.md``):

- transport: :mod:`alpaca.auth` (the key pair), :mod:`alpaca.client`
  (paged, rate-limited GETs against the data API);
- domain: :mod:`alpaca.auctions` (pure parsing of the vendor records and the
  choice of the cross print), :mod:`alpaca.universe` (which store symbols to
  fetch, over which span, under which Alpaca symbol), :mod:`alpaca.plan`
  (fetch tasks and request batches), :mod:`alpaca.quiet` (the window around
  the US close when the shared key is left alone);
- store: :mod:`alpaca.store`;
- orchestration: :mod:`alpaca.refresh`;
- surface: :mod:`alpaca.cli`, :mod:`alpaca.views`, the shell plugin.

The key pair comes only from ``APCA_API_KEY_ID`` and ``APCA_API_SECRET_KEY``
in the environment (or the Windows user environment); it is never stored,
printed or logged.

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
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
