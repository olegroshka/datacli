"""Register read-only macro views on a DuckDB connection.

Layers two independent surfaces onto an existing connection (e.g. the eodhd
explorer's):

- ``macro`` -- FRED market/US time series (series_id, name, category, date, value)
- ``macro_country`` -- EODHD cross-country annual indicators (country, indicator, ...)

Both are best-effort: a surface appears only once its parquet has been fetched.
Idempotent (``CREATE OR REPLACE``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from macro import registry as reg
from macro.config import EODHD_PARQUET, FRED_PARQUET, macro_root


def _values(rows: list[tuple[str, ...]]) -> str:
    return ", ".join(
        "(" + ", ".join("'" + c.replace("'", "''") + "'" for c in row) + ")"
        for row in rows
    )


def _register_fred(con: Any, root: Path, series: dict) -> bool:
    path = root / FRED_PARQUET
    if not path.exists() or not series:
        return False
    con.execute(
        "CREATE OR REPLACE VIEW macro_obs AS "
        f"SELECT series_id, CAST(date AS DATE) AS date, value "
        f"FROM read_parquet('{path.as_posix()}')"
    )
    meta = _values([(sid, s.name, s.category) for sid, s in series.items()])
    con.execute(
        "CREATE OR REPLACE VIEW macro_series AS "
        f"SELECT * FROM (VALUES {meta}) v(series_id, name, category)"
    )
    con.execute(
        "CREATE OR REPLACE VIEW macro AS "
        "SELECT o.series_id, s.name, s.category, o.date, o.value "
        "FROM macro_obs o LEFT JOIN macro_series s USING (series_id)"
    )
    return True


def _register_eodhd(con: Any, root: Path, indicators: dict, countries: dict) -> bool:
    path = root / EODHD_PARQUET
    if not path.exists() or not indicators or not countries:
        return False
    con.execute(
        "CREATE OR REPLACE VIEW macro_country_obs AS "
        f"SELECT country, indicator, CAST(date AS DATE) AS date, value "
        f"FROM read_parquet('{path.as_posix()}')"
    )
    ind_meta = _values([(i.indicator, i.name, i.category) for i in indicators.values()])
    con.execute(
        "CREATE OR REPLACE VIEW macro_indicator AS "
        f"SELECT * FROM (VALUES {ind_meta}) v(indicator, indicator_name, category)"
    )
    geo_meta = _values([(code, name) for code, name in countries.items()])
    con.execute(
        "CREATE OR REPLACE VIEW macro_geo AS "
        f"SELECT * FROM (VALUES {geo_meta}) v(country, country_name)"
    )
    con.execute(
        "CREATE OR REPLACE VIEW macro_country AS "
        "SELECT o.country, g.country_name, o.indicator, i.indicator_name, "
        "i.category, o.date, o.value "
        "FROM macro_country_obs o "
        "LEFT JOIN macro_indicator i USING (indicator) "
        "LEFT JOIN macro_geo g USING (country)"
    )
    return True


def register(con: Any, *, root: Path | None = None) -> dict[str, bool]:
    """Register whichever macro surfaces have data. Returns ``{view: registered}``."""
    base = Path(root) if root is not None else macro_root()
    return {
        "macro": _register_fred(con, base, reg.FRED_SERIES),
        "macro_country": _register_eodhd(
            con, base, reg.EODHD_INDICATORS, reg.EODHD_COUNTRIES
        ),
    }


def schema_snippet(*, fred: bool = True, country: bool = True) -> str:
    parts = ["Macro views (join to equities/each other by date):"]
    if fred:
        parts += [
            "- macro(series_id, name, category, date, value)  [FRED market series]",
            f"  categories: {', '.join(reg.categories())}",
            "  examples: DGS10, T10Y2Y, VIXCLS, BAMLH0A0HYM2, DTWEXBGS",
        ]
    if country:
        inds = ", ".join(reg.EODHD_INDICATORS)
        countries = ", ".join(reg.EODHD_COUNTRIES)
        parts += [
            "- macro_country(country, country_name, indicator, indicator_name, "
            "category, date, value)  [EODHD annual, per country]",
            f"  indicators: {inds}",
            f"  countries: {countries}",
        ]
    return "\n".join(parts)
