"""The lab's data surface: the eodhd views plus macro, and the schema the agent sees.

``connect()`` builds the eodhd explorer connection and layers the macro views on
top (a no-op until macro data is fetched). ``schema_text()`` describes exactly the
views that are present, so the agent only ever sees what it can actually query.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_EODHD = Path(__file__).resolve().parents[1] / "eodhd"
if str(_EODHD) not in sys.path:
    sys.path.insert(0, str(_EODHD))


def _has_view(con: Any, name: str) -> bool:
    return bool(
        con.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = ?", [name]
        ).fetchone()
    )


def connect() -> Any:
    """eodhd views + macro views (macro is best-effort; skipped if not fetched)."""
    import explore_eodhd  # type: ignore[import-not-found]

    con = explore_eodhd.connect()
    try:
        from macro import views as macro_views

        macro_views.register(con)
    except Exception:
        pass  # macro is optional; never break the eodhd surface over it
    return con


def schema_text(con: Any) -> str:
    """The eodhd schema, plus the macro snippet for whichever macro views exist."""
    from lab.tools import schema_context

    text = schema_context()
    has_fred = _has_view(con, "macro")
    has_country = _has_view(con, "macro_country")
    has_market = _has_view(con, "macro_market")
    if has_fred or has_country or has_market:
        try:
            from macro import views as macro_views

            text += "\n\n" + macro_views.schema_snippet(
                fred=has_fred, country=has_country, market=has_market
            )
        except Exception:
            pass
    return text
