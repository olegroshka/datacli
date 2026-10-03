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
    """eodhd views + macro + finra views (both best-effort; skipped if not fetched)."""
    import explore_eodhd  # type: ignore[import-not-found]

    con = explore_eodhd.connect()
    try:
        from macro import views as macro_views

        macro_views.register(con)
    except Exception:
        pass  # macro is optional; never break the eodhd surface over it
    try:
        from finra import views as finra_views

        finra_views.register(con)
    except Exception:
        pass  # finra is optional too
    try:
        from sec import views as sec_views

        sec_views.register(con)
    except Exception:
        pass  # sec is optional too
    try:
        from positioning import views as positioning_views

        positioning_views.register(con)
    except Exception:
        pass  # derived views are optional
    return con


def schema_text(con: Any) -> str:
    """The eodhd schema, plus the macro / finra snippets for whichever views exist."""
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
    has_sv = _has_view(con, "finra_short_volume")
    has_flow = _has_view(con, "finra_weekly_flow")
    has_si = _has_view(con, "finra_short_interest")
    has_ftd = _has_view(con, "finra_fails_to_deliver")
    if has_sv or has_flow or has_si or has_ftd:
        try:
            from finra import views as finra_views

            text += "\n\n" + finra_views.schema_snippet(
                short_volume=has_sv,
                weekly_flow=has_flow,
                short_interest=has_si,
                fails_to_deliver=has_ftd,
            )
        except Exception:
            pass
    if _has_view(con, "sec_13f_holdings"):
        try:
            from sec import views as sec_views

            text += "\n\n" + sec_views.schema_snippet()
        except Exception:
            pass
    has_ladder = _has_view(con, "positioning_short_ladder")
    has_long = _has_view(con, "positioning_long_ladder")
    has_factors = _has_view(con, "positioning_factors")
    has_map = _has_view(con, "positioning_cusip_map")
    if has_ladder or has_long or has_factors or has_map:
        try:
            from positioning import views as positioning_views

            text += "\n\n" + positioning_views.schema_snippet(
                ladder=has_ladder, factor_view=has_factors, cusip_map=has_map, long_ladder=has_long
            )
        except Exception:
            pass
    return text
