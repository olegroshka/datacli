"""borrow.ib / views / cli: the broker's shortable list, all offline on a fixture file."""

from __future__ import annotations

import datetime as dt
import hashlib
import sys
from pathlib import Path

import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from borrow import cli, ib, views  # noqa: E402
from borrow.config import ENV_ROOT  # noqa: E402

duckdb = pytest.importorskip("duckdb")

ROWS = [
    "A|USD|AGILENT TECHNOLOGIES INC|1715006|US00846U1016|3.6300|0.2500|7500000|BBG000C2V3D6|",
    "BRK B|USD|BERKSHIRE HATHAWAY INC-CL B|11|XXXXXXX10200|3.6300|0.2500|100000|BBG000DWG505|",
    "2777456Q|USD|HYDROMAID INTERNATIONAL INC|244127238|XXXXXXXF1093|NA|NA|2000||",
    "3WM|EUR|WESTERN MAGNESIUM CORP|603515870|XXXXXXXT1025|-14.2764|16.7184|300|BBG000KYNJF9|",
    "GME|USD|GAMESTOP CORP-CLASS A|9|US36467W1099|-45.2000|48.8300|25000|BBG000BB5BF6|",
    "AAPL|USD|APPLE INC|265598|US0378331005|3.6300|0.2500|>10000000|BBG000B9XRY4|",
]


def _file(stamp: str = "2026.10.03|10:42:07", rows: list[str] | None = None, *, declared: int | None = None) -> str:
    body = ROWS if rows is None else rows
    n = len(body) if declared is None else declared
    return "\n".join([f"#BOF|{stamp}", ib.HEADER, *body, f"#EOF|{n}", ""])


class _Ftp:
    """A fake FTP session serving one file and its companion."""

    def __init__(self, text: str, md5: str | None = None) -> None:
        self.data = text.encode("latin-1")
        self.md5 = (md5 if md5 is not None else hashlib.md5(self.data).hexdigest()) + "\n"
        self.quit_called = False

    def retrbinary(self, command: str, callback) -> None:
        name = command.split(" ", 1)[1]
        callback(self.md5.encode("ascii") if name.endswith(".md5") else self.data)

    def quit(self) -> None:
        self.quit_called = True


def test_parse_is_strict_and_typed() -> None:
    snapshot = ib.parse(_file())
    assert snapshot.stamp == dt.datetime(2026, 10, 3, 10, 42, 7) and snapshot.date == dt.date(2026, 10, 3)
    frame = snapshot.rows
    assert list(frame.columns) == list(ib.SCHEMA.names) and len(frame) == 6
    capped = frame.iloc[5]
    assert capped["available"] == 10_000_000 and bool(capped["available_capped"]) and not bool(frame.iloc[0]["available_capped"])
    a = frame.iloc[0]
    assert a["symbol"] == "A" and a["fee_rate"] == 0.25 and a["rebate_rate"] == 3.63
    assert a["available"] == 7_500_000 and a["isin"] == "US00846U1016"
    assert pd.isna(frame.iloc[1]["isin"])  # masked
    na = frame.iloc[2]
    assert pd.isna(na["fee_rate"]) and pd.isna(na["figi"]) and na["available"] == 2000
    assert frame.iloc[3].currency == "EUR" and frame.iloc[3].fee_rate == pytest.approx(16.7184)
    for bad, message in (
        (_file(declared=4), "#EOF declares 4 rows"),
        (_file(rows=["A|USD|X|1|US1|1|1|100"]), "no trailing pipe"),
        (_file(rows=["A|USD|X|1|US1|1|1|"]), "expected 9 fields"),
        (_file(rows=["A|USD|X|1|US1|abc|1|100|BBG|"]), "REBATERATE is not a number"),
        (_file(rows=["A|USD|X|1|US1|1|1|1.5|BBG|"]), "AVAILABLE is not an integer"),
        ("#BOF|2026.10.03|10:42:07\n" + ib.HEADER + "\n", "fewer than three lines"),
        ("BOF\n" + ib.HEADER + "\nA|USD|X|1|US1|1|1|100|BBG|\n#EOF|1\n", "not a #BOF stamp"),
        ("#BOF|2026.10.03|10:42:07\n#SYM|X|\nA|USD|X|1|US1|1|1|100|BBG|\n#EOF|1\n", "unexpected header"),
        (_file().replace("#EOF|", "#END|"), "not an #EOF trailer"),
    ):
        with pytest.raises(ib.BorrowError, match=message):
            ib.parse(bad)


def test_md5_companion_must_match() -> None:
    data = b"abc"
    ib.verify_md5(data, hashlib.md5(data).hexdigest() + "\n")
    ib.verify_md5(data, hashlib.md5(data).hexdigest().upper() + "  usa.txt\n")
    crlf = _file().replace("\n", "\r\n").encode("latin-1")  # served with CRLF, digested with LF
    ib.verify_md5(crlf, hashlib.md5(crlf.replace(b"\r\n", b"\n")).hexdigest())
    with pytest.raises(ib.BorrowError, match="md5 mismatch"):
        ib.verify_md5(data, "0" * 32)
    with pytest.raises(ib.BorrowError, match="not a digest"):
        ib.verify_md5(data, "garbage")


def test_refresh_is_idempotent_per_day_and_replaces_a_changed_file(tmp_path: Path) -> None:
    clock = lambda: dt.datetime(2026, 10, 3, 22, 30, tzinfo=dt.timezone.utc)  # noqa: E731
    plan = ib.refresh(lambda: _Ftp(_file()), tmp_path, run=False, now=clock)
    assert plan.outcome == "planned" and plan.rows == 6 and not ib.store(tmp_path).days_on_disk()
    first = ib.refresh(lambda: _Ftp(_file()), tmp_path, run=True, now=clock)
    assert first.outcome == "stored" and first.ok
    assert ib.store(tmp_path).days_on_disk() == [dt.date(2026, 10, 3)]
    again = ib.refresh(lambda: _Ftp(_file()), tmp_path, run=True, now=clock)
    assert again.outcome == "unchanged"
    later = ib.refresh(lambda: _Ftp(_file("2026.10.03|16:05:00", ROWS[:4])), tmp_path, run=True, now=clock)
    assert later.outcome == "replaced" and "replaced an earlier snapshot" in later.detail
    state = ib.store(tmp_path).load_state()["2026-10-03"]
    assert state.rows == 4 and "replaced" in state.detail and state.source == "ftp"
    next_day = ib.refresh(lambda: _Ftp(_file("2026.10.06|10:00:00")), tmp_path, run=True, now=clock)
    assert next_day.outcome == "stored" and len(ib.store(tmp_path).days_on_disk()) == 2
    bad = ib.refresh(lambda: _Ftp(_file(), md5="0" * 32), tmp_path, run=True, now=clock)
    assert bad.outcome == "failed" and "md5 mismatch" in bad.detail and not bad.ok
    assert len(ib.store(tmp_path).days_on_disk()) == 2
    broken = ib.refresh(lambda: _Ftp(_file(declared=99)), tmp_path, run=True, now=clock)
    assert broken.outcome == "failed" and "#EOF declares" in broken.detail

    def _down():
        raise OSError("connection refused")

    assert ib.refresh(_down, tmp_path, run=True, now=clock).outcome == "failed"
    status = ib.status(tmp_path)
    assert status["snapshots"] == 2 and status["rows"] == 10 and status["replaced"] == 1
    assert all(check in ("stale", "gaps") for _, check, _ in ib.qc(tmp_path))  # two recent days: at most warnings


def test_views_register_only_with_data_and_spell_eodhd_codes(tmp_path: Path) -> None:
    con = duckdb.connect()
    assert views.register(con, root=tmp_path)[views.VIEW] is False
    ib.refresh(lambda: _Ftp(_file()), tmp_path, run=True)
    assert views.register(con, root=tmp_path)[views.VIEW] is True
    rows = dict(con.execute(f"SELECT symbol, eodhd_code FROM {views.VIEW} ORDER BY symbol").fetchall())
    assert rows == {"2777456Q": None, "3WM": None, "A": "A", "AAPL": "AAPL", "BRK B": "BRK-B", "GME": "GME"}
    fee, rebate = con.execute(f"SELECT fee_rate, rebate_rate FROM {views.VIEW} WHERE symbol = 'GME'").fetchone()
    assert fee == pytest.approx(48.83) and rebate == pytest.approx(-45.2)
    assert con.execute(f"SELECT count(*) FROM {views.STATE_VIEW}").fetchone()[0] == 1
    assert views.VIEW in views.schema_snippet()


def test_country_files_are_stored_as_their_own_datasets(tmp_path: Path) -> None:
    clock = lambda: dt.datetime(2026, 10, 4, 8, 0, tzinfo=dt.timezone.utc)  # noqa: E731
    assert ib.COUNTRIES["usa"] == "usa.txt" and ib.COUNTRIES["germany"] == "germany.txt"
    assert ib.dataset_name("usa") == "ib" and ib.dataset_name("germany") == "ib_germany"
    seen: list[str] = []

    class _Recording(_Ftp):
        def retrbinary(self, command: str, sink) -> None:
            seen.append(command)
            super().retrbinary(command, sink)

    report = ib.refresh(lambda: _Recording(_file()), tmp_path, run=True, now=clock, country="germany")
    assert report.outcome == "stored" and seen == ["RETR germany.txt", "RETR germany.txt.md5"]
    assert ib.store(tmp_path, "germany").days_on_disk() == [dt.date(2026, 10, 3)]
    assert not ib.store(tmp_path).days_on_disk()  # usa untouched
    assert (tmp_path / "ib_germany" / "snapshots" / "2026-10-03.parquet").exists()
    assert ib.status(tmp_path, "germany")["dataset"] == "ib_germany" and ib.status(tmp_path)["present"] is False
    assert ib.store(tmp_path, "germany").load_state()["2026-10-03"].source == "ftp:germany.txt"
    with pytest.raises(ib.BorrowError, match="unknown country"):
        ib.store(tmp_path, "mars")
    assert ib.latest_snapshots(tmp_path) == {"germany": dt.date(2026, 10, 3)}


def test_cli_fetch_status_qc(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv(ENV_ROOT, str(tmp_path))
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("DATACLI_LOCKS_HELD", "1")
    monkeypatch.setattr(cli, "_ftp", lambda: _Ftp(_file()))
    assert cli.main(["fetch"]) == 0
    assert "dry run" in capsys.readouterr().out and not ib.store(tmp_path).days_on_disk()
    assert cli.main(["fetch", "--run"]) == 0
    assert "stored for 2026-10-03" in capsys.readouterr().out
    assert cli.main(["fetch", "--run"]) == 0
    assert "Everything in sync." in capsys.readouterr().out
    assert cli.main(["status", "--json"]) == 0
    payload = __import__("json").loads(capsys.readouterr().out)
    assert payload["datasets"][0]["snapshots"] == 1 and payload["root_source"] == "env"
    assert cli.main(["fetch", "--country", "all", "--run"]) == 0
    out = " ".join(capsys.readouterr().out.split())  # the console wraps long lines
    assert "ib_germany" in out and "ib_swedish" in out and "already stored" in out  # usa unchanged, the rest stored
    assert cli.main(["fetch", "--country", "mars"]) == 2
    assert "unknown country" in capsys.readouterr().out
    assert cli.main(["status", "--json"]) == 0
    payload = __import__("json").loads(capsys.readouterr().out)
    assert [d["dataset"] for d in payload["datasets"]] == [ib.dataset_name(c) for c in ib.COUNTRIES]
    assert cli.main(["qc"]) in (0, 1)  # staleness depends on today's date
    monkeypatch.setattr(cli, "_ftp", lambda: _Ftp(_file(), md5="0" * 32))
    assert cli.main(["fetch", "--run"]) == 1
    assert cli.main(["fetch", "extra"]) == 2
    assert cli.main(["nope"]) == 2
    assert cli.main(["--help"]) == 0


def test_scheduler_admits_the_borrow_family(tmp_path: Path) -> None:
    from scheduler.commands import CommandValidationError, ValidationContext, default_registry

    data = tmp_path / "data"
    data.mkdir()
    config = tmp_path / "datacli.toml"
    config.write_text(f'[eodhd]\ndata_root = "{data.as_posix()}"\n', encoding="utf-8")
    context = ValidationContext.current(_REPO_ROOT, Path(sys.executable), config, environment={})
    registry = default_registry()
    fetch = registry.validate("borrow", "fetch", ["--run"], context)
    names = {b.name: b for b in fetch.bindings}
    claims = {c.resource_id: c.mode for c in fetch.spec.resources}
    assert Path(names["borrow_data_root"].resolved_value).name == "borrow"
    assert claims[names["borrow_data_root"].resource_id] == "exclusive"
    assert fetch.spec.network and fetch.spec.mutation
    assert registry.validate("borrow", "fetch", ["--country", "all", "--run"], context).spec.argv == ("--country", "all", "--run")
    with pytest.raises(CommandValidationError):
        registry.validate("borrow", "fetch", ["--country", "mars", "--run"], context)
    assert registry.validate("borrow", "status", ["--json"], context).spec.resources
    assert registry.validate("borrow", "qc", [], context).spec.resources
    with pytest.raises(CommandValidationError, match="requires its own --run"):
        registry.validate("borrow", "fetch", [], context)
    with pytest.raises(CommandValidationError):
        registry.validate("borrow", "fetch", ["--run", "--full"], context)
    with pytest.raises(CommandValidationError, match="no arguments"):
        registry.validate("borrow", "qc", ["x"], context)
