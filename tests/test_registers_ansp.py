"""registers.fca_ansp: the FCA aggregated-net-short-position vintage archive (dedup, log, wayback backfill, status, qc)."""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from registers import fca_ansp  # noqa: E402

CURRENT_V1 = (
    "﻿Name of Company,International Securities Identification Number (ISIN),Aggregated net short position (%),Position date (of latest position date notified)\r\n"
    "BAE Systems Plc,GB0002634946,0.2,22/09/2026\r\n"
    "SSE PLC,GB0007908733,1.2,22/10/2026\r\n"
).encode("utf-8")
CURRENT_V2 = CURRENT_V1 + b"ENERGEAN PLC,GB00BG12Y042,4.18,26/10/2026\r\n"
HISTORIC = (
    "﻿Name of Company,International Securities Identification Number (ISIN),Aggregated net short position (%),Position date,Date the aggregated net short position became historical\r\n"
    "3I GROUP PLC,GB00B1YW4409,1.07,16/10/2026,19/10/2026\r\n"
).encode("utf-8")
RSL = "﻿Share ISIN,Company name,Date added,Class of share (Main or Other Class of Shares)\r\nGB00BFZ45C84,1SPATIAL PLC,13/07/2026,Main\r\n".encode("utf-8")


def _xlsx(rows: int) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame({"a": range(rows)}).to_excel(writer, sheet_name="Sheet1", index=False)
    return buffer.getvalue()


XLSX = _xlsx(3)
KEYS = ("current_csv", "historic_csv", "rsl_csv", "ansp_xlsx")


def _responses(current: bytes, *, last_modified: str = "Fri, 30 Oct 2026 11:39:37 GMT") -> dict[str, fca_ansp.Response]:
    headers = {"last-modified": last_modified, "date": "Sun, 01 Nov 2026 17:54:28 GMT", "content-type": "text/csv"}
    data = {"current_csv": current, "historic_csv": HISTORIC, "rsl_csv": RSL, "ansp_xlsx": XLSX}
    return {key: fca_ansp.Response(data[key], headers, fca_ansp.FILES[key][0]) for key in KEYS}


def _fetcher(responses: dict[str, fca_ansp.Response]):
    by_url = {r.url: r for r in responses.values()}
    return lambda url: by_url[url]


def test_refresh_archives_each_distinct_version_once_and_logs_every_retrieval(tmp_path: Path) -> None:
    clock = lambda: dt.datetime(2026, 11, 1, 13, 0, tzinfo=dt.timezone.utc)  # noqa: E731
    planned = fca_ansp.refresh(tmp_path, run=False, keys=KEYS, fetch_file=_fetcher(_responses(CURRENT_V1)), now=clock)
    assert {r.outcome for r in planned.results} == {"planned"} and not fca_ansp.VintageStore(tmp_path).exists()

    stored = fca_ansp.refresh(tmp_path, run=True, keys=KEYS, fetch_file=_fetcher(_responses(CURRENT_V1)), now=clock)
    assert stored.ok and stored.stored == 4 and stored.retrieved_at == "2026-11-01T13:00:00Z"
    current = next(r for r in stored.results if r.key == "current_csv")
    assert current.last_modified == "2026-10-30T11:39:37Z" and "2 rows" in current.detail and "2 ISINs" in current.detail
    store = fca_ansp.VintageStore(tmp_path)
    files = sorted(p.name for p in (store.files / "current_csv").iterdir())
    sha = hashlib.sha256(CURRENT_V1).hexdigest()
    assert files == [f"20261030T113937Z__{sha[:12]}.csv"]
    state = store.load_state()
    assert state["last_retrieval"] == "2026-11-01T13:00:00Z" and state["last_retrieval_ok"] is True
    assert state["versions"]["current_csv"][0]["summary"]["max_dates"] == {"Position date (of latest position date notified)": "2026-10-22"}
    assert state["versions"]["ansp_xlsx"][0]["summary"] == {"sheets": {"Sheet1": 3}}

    again = fca_ansp.refresh(tmp_path, run=True, keys=KEYS, fetch_file=_fetcher(_responses(CURRENT_V1)), now=clock)
    assert {r.outcome for r in again.results} == {"unchanged"} and again.stored == 0
    assert len(list((store.files / "current_csv").iterdir())) == 1
    log = store.observations()
    assert len(log) == 8 and sum(1 for o in log if o["changed"]) == 4 and all(o["headers"]["last-modified"] for o in log)

    later = lambda: dt.datetime(2026, 11, 2, 13, 0, tzinfo=dt.timezone.utc)  # noqa: E731
    changed = fca_ansp.refresh(tmp_path, run=True, keys=KEYS, fetch_file=_fetcher(_responses(CURRENT_V2, last_modified="Mon, 02 Nov 2026 11:40:00 GMT")), now=later)
    outcomes = {r.key: r.outcome for r in changed.results}
    assert outcomes["current_csv"] == "stored" and outcomes["historic_csv"] == "unchanged"  # same bytes, new Last-Modified: content decides, the header only names the file
    assert len(state_versions := store.load_state()["versions"]["current_csv"]) == 2 and state_versions[1]["summary"]["rows"] == 3


def test_refresh_keeps_going_after_a_transport_error_and_records_it(tmp_path: Path) -> None:
    clock = lambda: dt.datetime(2026, 11, 1, 13, 0, tzinfo=dt.timezone.utc)  # noqa: E731
    responses = _responses(CURRENT_V1)

    def fetch_file(url: str) -> fca_ansp.Response:
        if url == fca_ansp.FILES["historic_csv"][0]:
            raise OSError("HTTP Error 503: Service Unavailable")
        return responses[next(k for k, (u, _) in fca_ansp.FILES.items() if u == url)]

    report = fca_ansp.refresh(tmp_path, run=True, keys=KEYS, fetch_file=fetch_file, now=clock)
    assert not report.ok and report.stored == 3
    failed = next(r for r in report.results if r.outcome == "failed")
    assert failed.key == "historic_csv" and "503" in failed.detail
    state = fca_ansp.VintageStore(tmp_path).load_state()
    assert state["last_retrieval_ok"] is False
    errors = [o for o in fca_ansp.VintageStore(tmp_path).observations() if "error" in o]
    assert len(errors) == 1 and errors[0]["key"] == "historic_csv"
    assert any(f[1] == "uk_ansp_last_retrieval_failed" for f in fca_ansp.qc(tmp_path, today=dt.date(2026, 11, 1)))


def test_summaries_and_http_dates() -> None:
    summary = fca_ansp.summarize("current_csv", CURRENT_V2)
    assert summary["rows"] == 3 and summary["n_isins"] == 3 and summary["sum_pct"] == 5.58
    assert fca_ansp.summarize("rsl_csv", RSL)["max_dates"] == {"Date added": "2026-07-13"}
    assert "parse_error" in fca_ansp.summarize("ansp_xlsx", b"not a workbook")
    latin = "Share ISIN,Company name,Date added,Class of share (Main or Other Class of Shares)\r\nSE0000000001,M\u00dcNCHENER PLC,01/09/2026,Main\r\n".encode("cp1252")
    assert fca_ansp.summarize("rsl_csv", latin)["encoding"] == "cp1252" and fca_ansp.summarize("rsl_csv", RSL)["encoding"] == "utf-8-sig"
    assert fca_ansp.parse_http_date("Fri, 17 Jul 2026 10:54:19 GMT") == dt.datetime(2026, 7, 17, 10, 54, 19, tzinfo=dt.timezone.utc)
    assert fca_ansp.parse_http_date(None) is None and fca_ansp.parse_http_date("yesterday") is None


def test_wayback_listing_and_backfill_dedupe_against_live_versions(tmp_path: Path) -> None:
    cdx = "20260718212825 200 AZD6GCYKPBOWL2XBSLAPJJZMM6WASMX4\n20260720000000 302 XXXX\n20260728063538 200 XJRRJGV545RVAL4IJE4UBUVGHQ5S26GF\n"
    assert fca_ansp.parse_cdx(cdx) == [("20260718212825", "AZD6GCYKPBOWL2XBSLAPJJZMM6WASMX4"), ("20260728063538", "XJRRJGV545RVAL4IJE4UBUVGHQ5S26GF")]
    seen: list[str] = []

    def get(url: str) -> str:
        seen.append(url)
        return cdx

    listing = fca_ansp.wayback_captures(fca_ansp.FILES["current_csv"][0], get=get)
    assert len(listing) == 2 and "url=www.fca.org.uk%2Fpublication%2Fdocuments%2Faggregated-current-net-short-positions.csv" in seen[0] and "collapse=digest" in seen[0]

    raw = {"x-archive-orig-last-modified": "Fri, 17 Jul 2026 10:54:19 GMT", "x-archive-orig-date": "Sat, 18 Jul 2026 21:28:25 GMT", "content-type": "text/csv"}
    assert fca_ansp.wayback_headers(raw) == {"last-modified": "Fri, 17 Jul 2026 10:54:19 GMT", "date": "Sat, 18 Jul 2026 21:28:25 GMT", "content-type": "text/csv"}

    clock = lambda: dt.datetime(2026, 11, 1, 14, 0, tzinfo=dt.timezone.utc)  # noqa: E731
    url = fca_ansp.FILES["current_csv"][0]
    old = b"\xef\xbb\xbfName of Company,International Securities Identification Number (ISIN),Aggregated net short position (%),Position date (of latest position date notified)\r\nSSE PLC,GB0007908733,0.9,14/07/2026\r\n"
    capture_bytes = {"20260718212825": old, "20260728063538": CURRENT_V1}  # the second capture equals today's live file

    def fetch_capture(u: str, timestamp: str) -> fca_ansp.Response:
        assert u == url
        headers = {"last-modified": "Fri, 17 Jul 2026 10:54:19 GMT" if timestamp.startswith("20260718") else "Fri, 30 Oct 2026 11:39:37 GMT"}
        return fca_ansp.Response(capture_bytes[timestamp], headers, u, source="wayback", capture_at=f"2026-07-{timestamp[6:8]}T00:00:00Z", wayback_timestamp=timestamp)

    fca_ansp.refresh(tmp_path, run=True, keys=("current_csv",), fetch_file=_fetcher(_responses(CURRENT_V1)), now=clock)
    pauses: list[float] = []
    report = fca_ansp.backfill_wayback(
        tmp_path, run=True, keys=("current_csv",), captures=lambda u: fca_ansp.parse_cdx(cdx), fetch_capture=fetch_capture, now=clock, sleep=pauses.append
    )
    outcomes = [(r.outcome, r.source) for r in report.results]
    assert outcomes == [("stored", "wayback"), ("unchanged", "wayback")] and pauses == [1.0]
    store = fca_ansp.VintageStore(tmp_path)
    names = sorted(p.name for p in (store.files / "current_csv").iterdir())
    assert names[0].startswith("20260717T105419Z__") and len(names) == 2
    state = store.load_state()
    assert state["last_wayback_backfill"] == "2026-11-01T14:00:00Z"
    assert [v["source"] for v in state["versions"]["current_csv"]] == ["live", "wayback"]
    assert state["latest"]["current_csv"]["source"] == "live" and state["latest"]["current_csv"]["last_modified"] == "2026-10-30T11:39:37Z"  # captures never move `latest`
    assert state["versions"]["current_csv"][1]["capture_at"] == "2026-07-18T00:00:00Z"
    wayback_logs = [o for o in store.observations() if o["source"] == "wayback"]
    assert len(wayback_logs) == 2 and wayback_logs[0]["wayback_timestamp"] == "20260718212825"

    view = fca_ansp.status(tmp_path)
    current = next(f for f in view["files"] if f["key"] == "current_csv")
    assert current["versions"] == 2 and current["live_versions"] == 1 and current["wayback_versions"] == 1
    assert current["earliest_last_modified"] == "2026-07-17T10:54:19Z" and current["latest_last_modified"] == "2026-10-30T11:39:37Z"


def test_status_and_qc_when_absent_fresh_and_stale(tmp_path: Path) -> None:
    assert fca_ansp.status(tmp_path)["present"] is False and fca_ansp.qc(tmp_path) == []
    clock = lambda: dt.datetime(2026, 11, 1, 13, 0, tzinfo=dt.timezone.utc)  # noqa: E731
    fca_ansp.refresh(tmp_path, run=True, keys=KEYS, fetch_file=_fetcher(_responses(CURRENT_V1)), now=clock)
    assert fca_ansp.qc(tmp_path, today=dt.date(2026, 11, 2)) == []
    stale = fca_ansp.qc(tmp_path, today=dt.date(2026, 11, 9))
    assert [f[1] for f in stale] == ["uk_ansp_stale_retrieval", "uk_ansp_stale_publication"]
    view = fca_ansp.status(tmp_path)
    assert view["present"] and view["observations"] == 4 and view["last_retrieval"] == "2026-11-01T13:00:00Z"
    assert json.loads((fca_ansp.VintageStore(tmp_path).state_path).read_text(encoding="utf-8"))["latest"]["rsl_csv"]["sha256"] == hashlib.sha256(RSL).hexdigest()
