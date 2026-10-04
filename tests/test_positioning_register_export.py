from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_REPO = Path(__file__).resolve().parents[1]
for p in (_REPO, _REPO / "eodhd"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from borrow import ib  # noqa: E402
from positioning import register_export as rx  # noqa: E402


def _panel(n_names: int = 60, days: int = 6) -> pd.DataFrame:
    rng = np.random.default_rng(3)
    dates = pd.bdate_range("2026-09-01", periods=days)
    rows = []
    for i in range(n_names):
        for j, day in enumerate(dates):
            holders = 0 if (i == 0 and j >= 2) else 1  # the first name loses its holder on the third day
            rows.append({
                "market": "de", "isin": f"DE{i:010d}", "date": day, "n_holders": holders,
                "profit_pct": np.nan if holders == 0 else rng.normal(), "reversal_21d": rng.normal(),
                "momentum_12_1": rng.normal(), "ticker": f"T{i}.XETRA", "code": f"T{i}",
            })
    return pd.DataFrame(rows)


def test_signal_ranks_are_demeaned_shown_next_day_and_dropped_after_the_carry() -> None:
    panel = _panel()
    obs = rx.signal_observations(panel, min_names=50)
    assert set(obs.columns) == {"ticker", "date", "profit", "profit_raw"}
    per_day = obs.groupby("date")["profit"]
    assert (per_day.max() <= 0.5).all() and (per_day.min() >= -0.5).all() and abs(per_day.mean()).max() < 1e-9
    assert obs[obs["ticker"] == "T0.XETRA"]["date"].nunique() == 2  # marked only while it had a holder
    dates = pd.bdate_range("2026-09-01", periods=8)
    wide = rx.daily_wide(obs, "profit", dates=dates, carry_days=3)
    assert wide.index.equals(pd.DatetimeIndex(dates)) and wide.iloc[0].isna().all()  # shown from the next day
    assert wide.loc[dates[1], "T1.XETRA"] == obs[(obs["ticker"] == "T1.XETRA") & (obs["date"] == dates[0])]["profit"].item()
    t0 = wide["T0.XETRA"]
    assert t0[dates[2]] == t0[dates[2]] and t0[dates[5]] == t0[dates[5]]  # carried three days after its last mark (dates[1])
    assert np.isnan(t0[dates[6]])  # then dropped
    assert wide["T1.XETRA"].notna().sum() == 7  # six marked days shown from the next day, carried once past the panel's end


def test_borrow_rates_take_the_home_file_first_then_the_lowest_fee(tmp_path: Path) -> None:
    day = dt.date(2026, 10, 4)

    def rows(pairs: list[tuple[str, str, float]]) -> pd.DataFrame:
        return pd.DataFrame(
            [{"snapshot_date": day, "symbol": s, "currency": "EUR", "name": s, "con_id": 1, "isin": i,
              "rebate_rate": None, "fee_rate": fee, "available": 100, "available_capped": False, "figi": None}
             for s, i, fee in pairs], columns=list(ib.SCHEMA.names))

    ib.store(tmp_path, "germany").write_day(day, rows([("SAP", "DE0007164600", 0.5), ("AIR", "NL0000235190", 2.0)]))
    ib.store(tmp_path, "france").write_day(day, rows([("AIR", "NL0000235190", 1.0), ("SAP", "DE0007164600", 0.3)]))
    panels = pd.DataFrame({
        "market": ["de", "fr", "fr"], "isin": ["DE0007164600", "NL0000235190", "FR0000000001"],
        "ticker": ["SAP.XETRA", "AIR.PA", "X.PA"],
    })
    rates = rx.borrow_rates(tmp_path, panels)
    got = rates.set_index("ticker")["fee_rate"].to_dict()
    assert got == pytest.approx({"SAP.XETRA": 0.005, "AIR.PA": 0.01})  # SAP from germany.txt (home), AIR from france.txt (home)
    assert "X.PA" not in got and str(rates["snapshot_date"].iloc[0]) == "2026-10-04"
    assert rx.borrow_rates(tmp_path, panels.iloc[:0]).empty
