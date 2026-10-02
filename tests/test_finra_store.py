"""finra.store: day files under a pinned schema plus the state sidecar."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from finra import store as st  # noqa: E402

SCHEMA = pa.schema([("date", pa.date32()), ("symbol", pa.string()), ("n", pa.int64())])


def _frame(day: date, symbols=("A", "B")) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": [day] * len(symbols),
            "symbol": list(symbols),
            "n": range(len(symbols)),
        }
    )


def test_day_files_round_trip_and_replace(tmp_path: Path) -> None:
    store = st.DayStore(tmp_path, "demo", SCHEMA)
    day = date(2026, 9, 30)
    assert store.days_on_disk() == [] and store.read_day(day) is None

    path = store.write_day(day, _frame(day))
    assert path == tmp_path / "demo" / "daily" / "2026-09-30.parquet"
    assert store.days_on_disk() == [day]
    back = store.read_day(day)
    assert back is not None and list(back.columns) == ["date", "symbol", "n"]
    assert list(back["symbol"]) == ["A", "B"]

    store.write_day(day, _frame(day, ("Z",)))  # a re-fetch replaces the file
    back = store.read_day(day)
    assert back is not None and list(back["symbol"]) == ["Z"]
    assert not list((tmp_path / "demo" / "daily").glob("*.tmp"))


def test_write_day_validates_columns_and_ignores_foreign_files(tmp_path: Path) -> None:
    store = st.DayStore(tmp_path, "demo", SCHEMA)
    with pytest.raises(ValueError, match="lacks columns"):
        store.write_day(date(2026, 9, 30), pd.DataFrame({"date": [], "symbol": []}))
    store.daily_dir.mkdir(parents=True)
    (store.daily_dir / "notes.parquet").write_bytes(b"")
    assert store.days_on_disk() == []


def test_read_range(tmp_path: Path) -> None:
    store = st.DayStore(tmp_path, "demo", SCHEMA)
    days = [date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30)]
    for day in days:
        store.write_day(day, _frame(day))
    assert len(store.read_range()) == 6
    assert len(store.read_range(start=date(2026, 9, 29))) == 4
    assert len(store.read_range(end=date(2026, 9, 28))) == 2
    assert list(store.read_range(start=date(2027, 1, 1)).columns) == [
        "date",
        "symbol",
        "n",
    ]


def test_state_upsert_and_reload(tmp_path: Path) -> None:
    store = st.DayStore(tmp_path, "demo", SCHEMA)
    assert store.load_state() == {}
    store.upsert_state(
        [
            st.DayState(
                "2026-09-30",
                "ok",
                "cdn",
                rows=10,
                short_sum=5,
                total_sum=9,
                sha256="abc",
                fetched_at="t1",
            ),
            st.DayState("2026-09-29", "absent", "cdn", detail="no file published"),
        ]
    )
    store.upsert_state(
        [
            st.DayState(
                "2026-09-30",
                "ok",
                "cdn",
                rows=11,
                sha256="def",
                fetched_at="t2",
                detail="restated",
            )
        ]
    )
    state = store.load_state()
    assert sorted(state) == ["2026-09-29", "2026-09-30"]
    assert state["2026-09-30"].rows == 11 and state["2026-09-30"].sha256 == "def"
    assert state["2026-09-30"].day == date(2026, 9, 30)
    assert state["2026-09-29"].status == "absent" and state["2026-09-29"].rows == 0
    assert store.state_counts() == {"absent": 1, "ok": 1}
    text = store.state_path.read_text(encoding="utf-8")
    assert text.splitlines()[0] == ",".join(st.STATE_COLUMNS)
    assert text.index("2026-09-29") < text.index("2026-09-30")  # sorted by date


def test_day_state_validation_and_tolerant_parsing() -> None:
    with pytest.raises(ValueError, match="status"):
        st.DayState("2026-09-30", "pending")
    with pytest.raises(ValueError):
        st.DayState("not-a-date", "ok")
    parsed = st.DayState.from_row(
        {"date": "2026-09-30", "status": "ok", "rows": "12.0", "sha256": float("nan")}
    )
    assert parsed.rows == 12 and parsed.sha256 == "" and parsed.source == ""


def test_unreadable_state_is_empty_not_fatal(tmp_path: Path) -> None:
    store = st.DayStore(tmp_path, "demo", SCHEMA)
    store.dir.mkdir(parents=True)
    store.state_path.write_text(
        "date,status\n2026-09-30,bogus\n,ok\n", encoding="utf-8"
    )
    assert store.load_state() == {}
