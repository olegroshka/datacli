"""sec.form13f / views / cli: listing, planning, conversion and views, all offline."""

from __future__ import annotations

import io
import sys
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sec import cli, form13f, views  # noqa: E402
from sec.config import ENV_ROOT  # noqa: E402

duckdb = pytest.importorskip("duckdb")

PAGE = """
<a href="/files/form_13f_readme.pdf">readme</a>
<a href="/files/datastandardsinnovation/data/form-13f-data-sets/01jun2026-31aug2026_form13f.zip">new</a>
<a href="/files/structureddata/data/form-13f-data-sets/2022q4_form13f.zip">old</a>
<a href="/files/structureddata/data/form-13f-data-sets/2013q2_form13f.zip">oldest</a>
<a href="/files/structureddata/data/form-13f-data-sets/2013q2_form13f.zip">dup</a>
"""


def _zip(filings: list[tuple[str, str, str, str]], holdings: list[tuple[str, str, str, str, str]], *, drop: str = "") -> bytes:
    tables = {
        "SUBMISSION.tsv": "ACCESSION_NUMBER\tFILING_DATE\tSUBMISSIONTYPE\tCIK\tPERIODOFREPORT\n"
        + "".join(f"{a}\t{f}\t13F-HR\t{c}\t{p}\n" for a, f, c, p in filings),
        "COVERPAGE.tsv": "ACCESSION_NUMBER\tISAMENDMENT\tAMENDMENTNO\tAMENDMENTTYPE\tFILINGMANAGER_NAME\tREPORTTYPE\tFORM13FFILENUMBER\tCRDNUMBER\tSECFILENUMBER\n"
        + "".join(f"{a}\tN\t\t\tMANAGER \"{c}\" LP\t13F HOLDINGS REPORT\t028-1\t10{c}\t801-1\n" for a, _, c, _ in filings),
        "INFOTABLE.tsv": "ACCESSION_NUMBER\tINFOTABLE_SK\tNAMEOFISSUER\tTITLEOFCLASS\tCUSIP\tFIGI\tVALUE\tSSHPRNAMT\tSSHPRNAMTTYPE\tPUTCALL\tINVESTMENTDISCRETION\n"
        + "".join(f"{a}\t{i}\tISSUER {cusip}\tCOM\t{cusip}\t\t{value}\t{shares}\tSH\t{putcall}\tSOLE\n" for i, (a, cusip, value, shares, putcall) in enumerate(holdings)),
        "SUMMARYPAGE.tsv": "ACCESSION_NUMBER\tTABLEENTRYTOTAL\n",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in tables.items():
            if name != drop:
                archive.writestr(name, text)
    return buffer.getvalue()


OLD_ZIP = _zip(
    [("0001-22-1", "14-NOV-2022", "111", "30-SEP-2022")],
    [("0001-22-1", "037833100", "1500", "10", ""), ("0001-22-1", "594918104", "700", "3", "Put")],
)
NEW_ZIP = _zip(
    [("0001-26-9", "31-JUL-2026", "111", "30-JUN-2026"), ("0002-26-3", "14-AUG-2026", "222", "30-JUN-2026")],
    [("0001-26-9", "037833100", "2500000", "10000", ""), ("0002-26-3", "037833100", "100000", "400", "")],
)


class _Response:
    def __init__(self, status: int = 200, *, text: str = "", content: bytes = b"", headers: dict | None = None) -> None:
        self.status_code = status
        self.text = text
        self._content = content
        self.headers = headers or {}

    def iter_content(self, chunk_size: int = 0):
        yield self._content


class _Session:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.gets: list[str] = []
        self.heads: list[str] = []
        self.agents: set[str] = set()

    def _file(self, url: str) -> bytes | None:
        return self.files.get(url.rsplit("/", 1)[-1].removesuffix(".zip"))

    def get(self, url, headers=None, timeout=None, stream=False):
        self.gets.append(url)
        self.agents.add((headers or {}).get("User-Agent", ""))
        if url == form13f.PAGE_URL:
            return _Response(text=PAGE)
        body = self._file(url)
        return _Response(content=body) if body is not None else _Response(404)

    def head(self, url, headers=None, timeout=None, allow_redirects=True):
        self.heads.append(url)
        body = self._file(url)
        if body is None:
            return _Response(404)
        return _Response(headers={"Content-Length": str(len(body))})


FILES = {
    "2013q2_form13f": OLD_ZIP,
    "2022q4_form13f": OLD_ZIP,
    "01jun2026-31aug2026_form13f": NEW_ZIP,
}


def test_archive_period_and_listing() -> None:
    assert form13f.archive_period("2013q2_form13f") == (date(2013, 4, 1), date(2013, 6, 30))
    assert form13f.archive_period("2023q4_form13f") == (date(2023, 10, 1), date(2023, 12, 31))
    assert form13f.archive_period("01DEC2025-28FEB2026_form13f") == (date(2025, 12, 1), date(2026, 2, 28))
    with pytest.raises(form13f.Form13FError):
        form13f.archive_period("form13f_readme")
    listed = form13f.parse_listing(PAGE)
    assert [a.name for a in listed] == ["2013q2_form13f", "2022q4_form13f", "01jun2026-31aug2026_form13f"]
    assert listed[-1].url.startswith("https://www.sec.gov/files/datastandardsinnovation/")


def test_plan_newest_first_limit_and_republished() -> None:
    listed = form13f.parse_listing(PAGE)
    assert [a.name for a in form13f.plan(listed, {})][0] == "01jun2026-31aug2026_form13f"
    assert len(form13f.plan(listed, {}, limit=2)) == 2
    state = {a.name: form13f.ArchiveState(a.name, "ok", bytes=100) for a in listed}
    assert form13f.plan(listed, state) == []
    grown = form13f.plan(listed, state, remote_bytes={"01jun2026-31aug2026_form13f": 150})
    assert [a.name for a in grown] == ["01jun2026-31aug2026_form13f"]
    state["2022q4_form13f"] = form13f.ArchiveState("2022q4_form13f", "error")
    assert [a.name for a in form13f.plan(listed, state)] == ["2022q4_form13f"]
    assert len(form13f.plan(listed, state, full=True)) == 3


def test_refresh_dry_run_then_run_then_idempotent(tmp_path: Path) -> None:
    session = _Session(FILES)
    dry = form13f.refresh(session, "Test test@example.com", tmp_path, run=False)
    assert len(dry.planned) == 3 and not dry.stored and not form13f.Store(tmp_path).archives_on_disk()
    assert session.gets == [form13f.PAGE_URL]

    report = form13f.refresh(session, "Test test@example.com", tmp_path, run=True)
    assert report.ok and len(report.stored) == 3 and report.holdings == 6
    assert session.agents == {"Test test@example.com"}
    store = form13f.Store(tmp_path)
    assert len(store.archives_on_disk()) == 3
    state = store.load_state()
    assert state["01jun2026-31aug2026_form13f"].filings == 2
    assert state["2022q4_form13f"].bytes == len(OLD_ZIP) and state["2022q4_form13f"].sha256
    assert not list(store.dir.glob("*.part")) and not list(store.dir.rglob("*.tmp"))

    again = form13f.refresh(session, "Test test@example.com", tmp_path, run=True)
    assert again.planned == () and len(session.heads) >= 2  # the newest two are re-checked


def test_republished_archive_is_refetched_and_noted(tmp_path: Path) -> None:
    session = _Session(dict(FILES))
    form13f.refresh(session, "T t@example.com", tmp_path, run=True)
    bigger = _zip(
        [("0001-26-9", "31-JUL-2026", "111", "30-JUN-2026"), ("0002-26-3", "14-AUG-2026", "222", "30-JUN-2026"), ("0003-26-1", "28-AUG-2026", "333", "30-JUN-2026")],
        [("0001-26-9", "037833100", "2500000", "10000", ""), ("0003-26-1", "037833100", "5", "1", "")],
    )
    session.files["01jun2026-31aug2026_form13f"] = bigger
    report = form13f.refresh(session, "T t@example.com", tmp_path, run=True)
    assert report.stored == ("01jun2026-31aug2026_form13f",)
    state = form13f.Store(tmp_path).load_state()["01jun2026-31aug2026_form13f"]
    assert state.filings == 3 and state.detail.startswith("republished")


def test_a_broken_archive_is_an_error_row_and_leaves_no_tables(tmp_path: Path) -> None:
    files = dict(FILES)
    files["2022q4_form13f"] = _zip([], [], drop="INFOTABLE.tsv")
    files["2013q2_form13f"] = b"not a zip"
    report = form13f.refresh(_Session(files), "T t@example.com", tmp_path, run=True)
    assert not report.ok and {name for name, _ in report.failed} == {"2022q4_form13f", "2013q2_form13f"}
    store = form13f.Store(tmp_path)
    assert store.archives_on_disk() == ["01jun2026-31aug2026_form13f"]
    assert store.load_state()["2022q4_form13f"].status == "error"
    assert any(check == "failed_archives" for _, check, _ in cli.qc(tmp_path))
    # the next run retries the failed ones
    retry = form13f.refresh(_Session(FILES), "T t@example.com", tmp_path, run=True)
    assert set(retry.stored) == {"2022q4_form13f", "2013q2_form13f"} and retry.ok


def test_views_types_units_and_point_in_time_columns(tmp_path: Path) -> None:
    con = duckdb.connect()
    assert views.register(con, root=tmp_path)[views.HOLDINGS_VIEW] is False
    form13f.refresh(_Session({k: v for k, v in FILES.items() if k != "2013q2_form13f"} | {"2013q2_form13f": _zip([], [])}), "T t@example.com", tmp_path, run=True)
    assert views.register(con, root=tmp_path)[views.HOLDINGS_VIEW] is True
    rows = con.execute(
        f"SELECT cik, filing_date, period, cusip, value_usd, shares, put_call, crd_number, manager_name, is_amendment "
        f"FROM {views.HOLDINGS_VIEW} ORDER BY filing_date, cusip, value_usd"
    ).fetchall()
    assert len(rows) == 4
    # 2022 filing: values are thousands; 2026 filing: dollars
    assert rows[0][:7] == ("111", date(2022, 11, 14), date(2022, 9, 30), "037833100", 1_500_000.0, 10.0, None)
    assert rows[1][4] == 700_000.0 and rows[1][6] == "Put"
    assert rows[2][:6] == ("111", date(2026, 7, 31), date(2026, 6, 30), "037833100", 2_500_000.0, 10000.0)
    assert rows[2][7] == 10111 and rows[2][8] == 'MANAGER "111" LP' and rows[2][9] is False
    assert con.execute(f"SELECT count(*) FROM {views.SUBMISSION_VIEW}").fetchone()[0] == 3
    assert views.HOLDINGS_VIEW in views.schema_snippet()
    assert cli.qc(tmp_path) == []


def test_cli_fetch_status_qc(tmp_path: Path, monkeypatch, capsys) -> None:
    import finra.sec

    monkeypatch.setenv(ENV_ROOT, str(tmp_path))
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("DATACLI_LOCKS_HELD", "1")
    monkeypatch.setattr(cli, "_session", lambda: _Session(FILES))
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    monkeypatch.setattr(finra.sec, "user_agent", lambda: None)
    assert cli.main(["fetch"]) == 1 and "SEC_USER_AGENT" in capsys.readouterr().out
    monkeypatch.setattr(finra.sec, "user_agent", lambda: "T t@example.com")
    assert cli.main(["fetch"]) == 0 and "dry run" in capsys.readouterr().out
    assert not form13f.Store(tmp_path).archives_on_disk()
    assert cli.main(["fetch", "--limit", "1", "--run"]) == 0
    assert form13f.Store(tmp_path).archives_on_disk() == ["01jun2026-31aug2026_form13f"]
    assert cli.main(["fetch", "--run"]) == 0
    capsys.readouterr()
    assert cli.main(["status", "--json"]) == 0
    entry = __import__("json").loads(capsys.readouterr().out)["datasets"][0]
    assert entry["archives"] == 3 and entry["holdings"] == 6 and entry["first"] == "2013-04-01"
    assert cli.main(["qc"]) == 0
    assert cli.main(["fetch", "--limit", "x"]) == 2
    assert cli.main(["fetch", "extra"]) == 2
    assert cli.main(["fetsh"]) == 2


def test_units_classifier_catches_a_filer_still_reporting_in_thousands(tmp_path: Path) -> None:
    filings = [(f"000{i}-23-1", "15-MAY-2023", str(100 + i), "31-MAR-2023") for i in range(6)]
    filings.append(("0009-22-1", "14-NOV-2022", "109", "30-SEP-2022"))  # pre-switch, thousands
    holdings = [(f"000{i}-23-1", "037833100", str(100 * (10 + i)), str(10 + i), "") for i in range(5)]
    holdings.append(("0005-23-1", "037833100", "2", "20", ""))  # 0.1 per share: thousands
    holdings.append(("0009-22-1", "037833100", "2", "20", ""))
    files = {"01mar2023-31may2023_form13f": _zip(filings, holdings)}
    page = '<a href="/files/x/01mar2023-31may2023_form13f.zip">a</a>'
    session = _Session(files)
    session.get = lambda url, headers=None, timeout=None, stream=False, _g=session.get: (
        _Response(text=page) if url == form13f.PAGE_URL else _g(url, headers, timeout, stream)
    )
    form13f.refresh(session, "T t@example.com", tmp_path, run=True)
    store = form13f.Store(tmp_path)
    assert store.table_path(form13f.UNITS_TABLE, "01mar2023-31may2023_form13f").exists()
    con = duckdb.connect()
    views.register(con, root=tmp_path)
    rows = dict(con.execute(f"SELECT accession_number, (evidence, value_factor) FROM {views.UNITS_VIEW}").fetchall())
    assert rows["0005-23-1"] == ("below_crowd", 1000.0)
    assert rows["0000-23-1"] == ("with_crowd", 1.0)
    assert rows["0009-22-1"] == ("assumed", 1000.0)  # alone in its period: the date rule
    per_share = dict(con.execute(
        f"SELECT accession_number, value_usd / shares FROM {views.HOLDINGS_VIEW}"
    ).fetchall())
    assert per_share["0005-23-1"] == pytest.approx(100.0) and per_share["0000-23-1"] == pytest.approx(100.0)
    assert cli.qc(tmp_path) == []


# --------------------------------------------------------------------------- #
# Form ADV adviser snapshots and the 13F manager cohort
# --------------------------------------------------------------------------- #
from sec import adv  # noqa: E402

ADV_PAGE = """
<a href="/files/investment/data/other/x/ia100226-exempt.zip">exempt</a>
<a href="/files/investment/data/other/x/ia100226.zip">new</a>
<a href="/files/investment/data/other/x/ia09012026.zip">eight digits</a>
<a href="/files/data/y/ia050707.zip">old</a>
"""


def _adv_zip(rows: list[tuple[str, str, str, str]]) -> bytes:
    header = '"SEC Region","Organization CRD#","SEC#","Primary Business Name","Legal Name","5F(2)(c)","7B","Count of Private Funds - 7B(1)","Any Hedge Funds","Total number of Hedge funds","Total Gross Assets of Private Funds"\n'
    body = "".join(f'"NY","{crd}","801-{crd}","{name}","{name} LLC","{raum}","Y","1","{hedge}","{"1" if hedge == "Y" else ""}","500"\n' for crd, name, raum, hedge in rows)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("ia_report.csv", (header + body).encode("latin-1"))
    return buffer.getvalue()


class _AdvSession(_Session):
    def get(self, url, headers=None, timeout=None, stream=False):
        if url == adv.PAGE_URL:
            self.gets.append(url)
            return _Response(text=ADV_PAGE)
        if url == form13f.PAGE_URL:
            return _Response(text=PAGE)
        return super().get(url, headers, timeout, stream)


def test_adv_listing_dates_and_plan() -> None:
    assert adv.snapshot_date("ia100226") == date(2026, 10, 2)
    assert adv.snapshot_date("ia09012026") == date(2026, 9, 1)
    assert adv.snapshot_date("ia050707") == date(2007, 5, 7)
    listed = adv.parse_listing(ADV_PAGE)
    assert [s.name for s in listed] == ["ia050707", "ia09012026", "ia100226"]  # exempt files skipped
    assert [s.name for s in adv.plan(listed, {})][0] == "ia100226"
    state = {"ia100226": adv.SnapshotState("ia100226", "ok")}
    assert [s.name for s in adv.plan(listed, state)] == ["ia09012026", "ia050707"]
    assert len(adv.plan(listed, state, full=True, limit=2)) == 2


def test_adv_refresh_views_and_cohort(tmp_path: Path) -> None:
    files = dict(FILES)
    files["2013q2_form13f"] = _zip([], [])  # the fixture's 2013 and 2022 zips are otherwise identical
    files["ia050707"] = _adv_zip([("10111", "MANAGER 111", "1000", "N")])
    files["ia09012026"] = _adv_zip([("10111", "MANAGER 111", "5000", "Y"), ("10222", "MANAGER 222", "900", "N")])
    files["ia100226"] = _adv_zip([("10111", "MANAGER 111", "6000", "Y"), ("10222", "MANAGER 222", "900", "N")])
    session = _AdvSession(files)
    report = adv.refresh(session, "T t@example.com", tmp_path, run=True)
    assert report.ok and len(report.stored) == 3
    store = adv.Store(tmp_path)
    assert store.load_state()["ia09012026"].hedge_fund_advisers == 1
    assert cli.qc_adv(tmp_path) == []
    form13f.refresh(session, "T t@example.com", tmp_path, run=True)
    con = duckdb.connect()
    registered = views.register(con, root=tmp_path)
    assert registered[views.ADV_VIEW] and registered[views.COHORT_VIEW]
    advisers = con.execute(
        f"SELECT snapshot_date, crd_number, any_hedge_funds, n_hedge_funds, raum_total FROM {views.ADV_VIEW} "
        "WHERE crd_number = 10111 ORDER BY 1"
    ).fetchall()
    assert advisers == [
        (date(2007, 5, 7), 10111, False, None, 1000.0),
        (date(2026, 9, 1), 10111, True, 1, 5000.0),
        (date(2026, 10, 2), 10111, True, 1, 6000.0),
    ]
    cohort = dict(
        con.execute(
            f"SELECT accession_number, (adv_snapshot_date, any_hedge_funds) FROM {views.COHORT_VIEW}"
        ).fetchall()
    )
    # filed 2022-11-14: the 2007 snapshot is the latest before it (no hedge funds then);
    # filed 2026-07-31: still the 2007 one (September 2026 is after the filing);
    # filed 2026-08-14 by CRD 10222: nothing before -> NULL
    assert cohort["0001-22-1"] == (date(2007, 5, 7), False)
    assert cohort["0001-26-9"] == (date(2007, 5, 7), False)
    assert cohort["0002-26-3"] == (None, None)
    assert cli.qc(tmp_path) == []


def test_cli_fetch_adv_dataset(tmp_path: Path, monkeypatch, capsys) -> None:
    import finra.sec

    files = dict(FILES)
    files["ia100226"] = _adv_zip([("10111", "M", "1", "Y")])
    files["ia09012026"] = files["ia100226"]
    files["ia050707"] = files["ia100226"]
    monkeypatch.setenv(ENV_ROOT, str(tmp_path))
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("DATACLI_LOCKS_HELD", "1")
    monkeypatch.setattr(cli, "_session", lambda: _AdvSession(files))
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    monkeypatch.setattr(finra.sec, "user_agent", lambda: "T t@example.com")
    assert cli.main(["fetch", "--dataset", "adv", "--limit", "2", "--run"]) == 0
    assert adv.Store(tmp_path).snapshots_on_disk() == ["ia09012026", "ia100226"]
    assert cli.main(["fetch", "--dataset", "nope"]) == 2
    capsys.readouterr()
    assert cli.main(["status", "--json"]) == 0
    entries = {e["dataset"]: e for e in __import__("json").loads(capsys.readouterr().out)["datasets"]}
    assert entries["adv"]["archives"] == 2 and entries["adv"]["holdings"] == 2


def test_adv_reads_the_three_layouts(tmp_path: Path) -> None:
    import openpyxl

    store = adv.Store(tmp_path)
    pipe = b'SEC Region Name|Organization CRD #|SEC #|Primary Business Name|\r\nNY|   146|801-1|ADVANTAGE|\r\n'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("2199190.txt", pipe)
    assert adv.convert_snapshot(buffer.getvalue(), "ia060506", store) == (1, 0)
    frame = pd.read_parquet(store.path("ia060506"))
    assert list(frame.columns)[:3] == ["snapshot_date", "SEC Region Name", "Organization CRD#"]
    assert frame.loc[0, "Organization CRD#"].strip() == "146" and frame.loc[0, "snapshot_date"] == "2006-06-05"

    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["SEC Region", "Organization CRD#", "Primary Business Name", "Any Hedge Funds"])
    sheet.append(["NY", 10111, "MANAGER", "Y"])
    sheet.append(["NY", 10222, "OTHER", "N"])
    xlsx = io.BytesIO()
    book.save(xlsx)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("SEC Registered Investment Adviser 2019-7-1.xlsx", xlsx.getvalue())
    assert adv.convert_snapshot(buffer.getvalue(), "ia070119", store) == (2, 1)
    assert pd.read_parquet(store.path("ia070119"))["Organization CRD#"].tolist() == ["10111", "10222"]


def test_adv_quirks_macosx_member_duplicate_header_and_placeholder(tmp_path: Path) -> None:
    store = adv.Store(tmp_path)
    csv = b'"Organization CRD#","5H","5H","7B"\n"1","a","b","Y"\n'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("ia100226.csv", csv)
        z.writestr("__MACOSX/._ia100226.csv", b"junk")
    assert adv.convert_snapshot(buffer.getvalue(), "ia100226", store) == (1, 0)
    assert list(pd.read_parquet(store.path("ia100226")).columns) == ["snapshot_date", "Organization CRD#", "5H", "5H.1", "7B"]
    pipe = b"Organization CRD #|SEC #|Name|\r\n1|801|A|\r\n2|802|B|extra|\r\n3|803|\r\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("x.txt", pipe)
    assert adv.convert_snapshot(buffer.getvalue(), "ia112907", store) == (3, 0)
    assert pd.read_parquet(store.path("ia112907"))["Name"].tolist() == ["A", "B", ""]
    notice = b'"The report for January 1, 2019 is unavailable due to the federal government shutdown"\n'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("ia010119.csv", notice)
    with pytest.raises(adv.AdvPlaceholder):
        adv.convert_snapshot(buffer.getvalue(), "ia010119", store)
    state = {"a": adv.SnapshotState("a", "absent"), "b": adv.SnapshotState("b", "error")}
    listed = [adv.Snapshot("a", "u", date(2019, 1, 1)), adv.Snapshot("b", "u", date(2019, 2, 1))]
    assert [s.name for s in adv.plan(listed, state)] == ["b"]  # absent is final, error is retried
