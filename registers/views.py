"""Register the read-only register views on a DuckDB connection.

- ``short_register`` -- every published position change of every stored
  market, the canonical columns of :mod:`registers.common`. A reader on day
  ``d`` sees a row when ``published_from <= d``; a row is the holder's
  position on ``position_date``, in percent of the share capital, and stays
  the holder's position until the holder's next row for the ISIN.
- ``short_register_state`` -- one row per market from the state sidecars.

Best-effort: the views appear only once a history exists. Idempotent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from registers import common
from registers.config import registers_root

VIEW = "short_register"
STATE_VIEW = "short_register_state"


def register(con: Any, *, root: Path | None = None) -> dict[str, bool]:
    base = Path(root) if root is not None else registers_root()
    stores = [common.store(base, m) for m in common.MARKETS]
    present = [s for s in stores if s.exists()]
    if not present:
        return {VIEW: False}
    files = ", ".join(f"'{s.path.as_posix()}'" for s in present)
    con.execute(f"CREATE OR REPLACE VIEW {VIEW} AS SELECT * FROM read_parquet([{files}], union_by_name=true)")
    rows = []
    for s in present:
        state = s.load_state()
        rows.append((s.market, state.get("file_date"), int(state.get("rows", 0)), state.get("fetched_at"), state.get("source"), json.dumps(state.get("detail", ""))))
    values = ", ".join(
        "(" + ", ".join("NULL" if v is None else (str(v) if isinstance(v, int) else "'" + str(v).replace("'", "''") + "'") for v in r) + ")"
        for r in rows
    )
    con.execute(
        f"CREATE OR REPLACE VIEW {STATE_VIEW} AS SELECT * FROM (VALUES {values}) "
        "AS t(market, file_date, rows, fetched_at, source, detail)"
    )
    return {VIEW: True}


def schema_snippet() -> str:
    return "\n".join(
        [
            "Register views (public net short position registers, per holder):",
            f"- {VIEW}(market, holder, holder_lei, issuer, isin, net_short_pct, position_date, published_from, published_to, file_date)",
            "  [market uk = FCA (history 2012-10 to 2026-07-09, frozen), fr = AMF, nl = AFM, se = FI (from 2010),",
            "  no = Finanstilsynet (from 2024-10), ie = CBI, de = Bundesanzeiger (live); net_short_pct in percent of",
            "  the share capital, 0.0 = fell below the 0.5 percent publication threshold; visible from published_from;",
            "  a holder's row stands until the holder's next row for the ISIN; join isin to issuer_map for a ticker]",
            f"- {STATE_VIEW}(market, file_date, rows, fetched_at, source, detail)",
        ]
    )
