"""positioning.factors: the factor view on a small fake price surface."""

from __future__ import annotations

import math
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from positioning import factors  # noqa: E402

duckdb = pytest.importorskip("duckdb")


def _weekdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = _weekdays(date(2017, 1, 2), 400)
TICKERS = [f"T{i}" for i in range(6)]


def _close(ticker: str, i: int) -> float:
    k = int(ticker[1:])
    # a common drift plus a ticker-specific wiggle, so excess returns are not zero
    return 100.0 * (1.0005**i) * (1 + 0.01 * k * math.sin(i / (3.0 + k)))


def _prices(con) -> pd.DataFrame:
    frame = pd.DataFrame(
        [
            {"ticker": t, "date": d.isoformat(), "adjusted_close": _close(t, i), "close": _close(t, i), "lane": "us_common"}
            for t in TICKERS
            for i, d in enumerate(DAYS)
        ]
    )
    con.register("_px", frame)
    con.execute("CREATE OR REPLACE VIEW prices AS SELECT * FROM _px")
    return frame


def test_no_prices_no_view() -> None:
    con = duckdb.connect()
    assert factors.register(con) is False


def test_reversal_momentum_and_risk_use_only_trailing_closes() -> None:
    con = duckdb.connect()
    _prices(con)
    assert factors.register(con) is True
    i = 350
    row = con.execute(
        f"SELECT ret_1d, reversal_21d, momentum_12_1, specific_risk_63d, sector, short_interest_ratio "
        f"FROM {factors.VIEW} WHERE ticker = 'T3' AND date = DATE '{DAYS[i]}'"
    ).fetchone()
    ret, rev, mom, risk, sector, si = row
    assert ret == pytest.approx(_close("T3", i) / _close("T3", i - 1) - 1)
    assert rev == pytest.approx(_close("T3", i) / _close("T3", i - 21) - 1)
    assert mom == pytest.approx(_close("T3", i - 21) / _close("T3", i - 252) - 1)
    assert sector is None and si is None

    # specific risk: annualised stdev of the last 63 returns in excess of the market median
    def ret_of(t: str, j: int) -> float:
        return _close(t, j) / _close(t, j - 1) - 1

    excess = [
        ret_of("T3", j) - float(pd.Series([ret_of(t, j) for t in TICKERS]).median())
        for j in range(i - 62, i + 1)
    ]
    assert risk == pytest.approx(pd.Series(excess).std() * math.sqrt(252), rel=1e-9)

    # nothing before the first output date, and momentum needs a year of history
    first, n_null = con.execute(
        f"SELECT min(date), count(*) FILTER (WHERE momentum_12_1 IS NULL) FROM {factors.VIEW}"
    ).fetchone()
    assert first >= date(2018, 1, 1) and n_null == 0


def test_later_prices_do_not_change_earlier_rows() -> None:
    con = duckdb.connect()
    full = _prices(con)
    factors.register(con)
    cut = DAYS[330]
    q = (
        f"SELECT ticker, date, reversal_21d, momentum_12_1, specific_risk_63d FROM {factors.VIEW} "
        f"WHERE date <= DATE '{cut}' ORDER BY 1, 2"
    )
    before = con.execute(q).df()
    con.unregister("_px")
    con.register("_px", full[full["date"] <= cut.isoformat()])
    after = con.execute(q).df()
    pd.testing.assert_frame_equal(before, after)


def test_sector_excess_and_point_in_time_short_interest(tmp_path: Path) -> None:
    con = duckdb.connect()
    _prices(con)
    lane = tmp_path / "us_common"
    lane.mkdir()
    # five names in one sector (enough for a sector mean), one alone (falls back to market)
    pd.DataFrame(
        {"ticker": TICKERS, "sector": ["Tech"] * 5 + ["Lonely"]}
    ).to_parquet(lane / "firm_metadata.parquet")
    published = DAYS[340]
    con.register(
        "_si",
        pd.DataFrame(
            {
                "eodhd_code": ["T1", "T1"],
                "published_at": [DAYS[300], published],
                "short_over_outstanding": [0.05, 0.09],
            }
        ),
    )
    con.execute("CREATE VIEW finra_short_interest_float AS SELECT * FROM _si")
    assert factors.register(con, eodhd_root=tmp_path) is True
    rows = dict(
        con.execute(
            f"SELECT date, short_interest_ratio FROM {factors.VIEW} WHERE ticker = 'T1' "
            f"AND date IN (DATE '{DAYS[339]}', DATE '{published}', DATE '{DAYS[341]}')"
        ).fetchall()
    )
    # a report published on the day itself is not yet known on that day
    assert rows[DAYS[339]] == 0.05 and rows[published] == 0.05 and rows[DAYS[341]] == 0.09
    sectors = dict(
        con.execute(
            f"SELECT ticker, any_value(sector) FROM {factors.VIEW} GROUP BY 1"
        ).fetchall()
    )
    assert sectors["T0"] == "Tech" and sectors["T5"] == "Lonely"
    assert factors.VIEW in factors.schema_snippet()


def test_cusip_map_groups_pairs_with_their_dates() -> None:
    from positioning import master

    con = duckdb.connect()
    assert master.register(con) is False
    con.register(
        "_ftd",
        pd.DataFrame(
            {
                "settlement_date": [date(2019, 1, 2), date(2019, 1, 3), date(2021, 5, 4), date(2021, 5, 4), date(2022, 1, 5)],
                "cusip": ["111", "111", "111", "222", ""],
                "symbol": ["OLD", "OLD", "NEW", "OLD", "X"],
                "eodhd_code": ["OLD", "OLD", "NEW", "OLD", "X"],
                "description": ["OLDCO", "OLDCO", "NEWCO", "REUSER", "NONE"],
            }
        ),
    )
    con.execute("CREATE VIEW finra_fails_to_deliver AS SELECT * FROM _ftd")
    assert master.register(con) is True
    rows = con.execute(
        f"SELECT cusip, eodhd_code, first_seen, last_seen, n_days FROM {master.VIEW} ORDER BY 1, 2"
    ).fetchall()
    assert rows == [
        ("111", "NEW", date(2021, 5, 4), date(2021, 5, 4), 1),
        ("111", "OLD", date(2019, 1, 2), date(2019, 1, 3), 2),
        ("222", "OLD", date(2021, 5, 4), date(2021, 5, 4), 1),
    ]
    assert master.VIEW in master.schema_snippet()
