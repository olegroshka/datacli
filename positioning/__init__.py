"""Derived point-in-time positioning datasets (docs/samrt-money-flow-dataset-initiative).

``basis`` puts share counts and prices on a split-neutral basis, ``ladder`` is
the FIFO lot kernel (DD-001), ``short_ladder`` runs it over FINRA short
interest. Signals built on these outputs live outside datacli (ADR-001 D1).
"""

import finra  # noqa: F401  (puts eodhd/ on sys.path for the shared helpers)
