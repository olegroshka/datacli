"""The ``linkage`` source: point-in-time links between names, derived locally.

Three datasets, each built only from data dated on or before its own date:

- ``etf_betas``: each US stock's trailing betas to the 38 context ETFs of the
  intraday lane, plain and net of the market, at every month end (from the
  eodhd daily prices, splits and dividends);
- ``auction_abnormal``: each name's closing and opening auction value against
  its own trailing median, and net of that day's cross-section (from the
  Alpaca auctions);
- ``auction_cospike``: at every month end, each member's nearest neighbours by
  how its unusual auction value moves with theirs over the trailing year.

They stand in for ETF holdings and index membership, which datacli does not
have point in time: they describe behaviour, not holdings.

Layers (see ``docs/AUCTIONS_AND_LINKAGE_DESIGN.md``):

- inputs: :mod:`linkage.sources` (interfaces and the eodhd / Alpaca adapters);
- pure computation: :mod:`linkage.calendar`, :mod:`linkage.moments`,
  :mod:`linkage.returns`, :mod:`linkage.betas`, :mod:`linkage.abnormal`,
  :mod:`linkage.cospike`;
- store: :mod:`linkage.store`;
- orchestration: :mod:`linkage.datasets`;
- surface: :mod:`linkage.cli`, :mod:`linkage.views`, :mod:`linkage.qc`.
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
