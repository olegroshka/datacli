"""finra.cdn: the strict daily-file parser and the absent-vs-error rule."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from finra import cdn  # noqa: E402
from finra.errors import CdnError, DailyFileFormatError  # noqa: E402

DAY = date(2025, 1, 2)
HEADER = "Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market"
ROWS = [
    "20250102|A|102002|124|308407|B,Q,N",
    "20250102|BRK/B|504210|1665|1213815|Q,B,N",
    "20250102|ABRpD|10|0|20|Q",
    "20250102|ZZZ|735|0|793|Q,N",
]
ABSENT_BODY = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b"<Error><Code>AccessDenied</Code><Message>Access Denied</Message></Error>"
)


def _file(
    rows=ROWS, *, trailer: str | None = None, header: str = HEADER, crlf: bool = True
) -> bytes:
    lines = [header, *rows, str(len(rows)) if trailer is None else trailer]
    return ("\r\n" if crlf else "\n").join(lines).encode() + (
        b"\r\n" if crlf else b"\n"
    )


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
def test_parses_the_published_layout_with_crlf() -> None:
    parsed = cdn.parse_daily_file(_file(), family="CNMS", trade_date=DAY)
    assert parsed.family == "CNMS" and parsed.trade_date == DAY
    assert parsed.declared_count == 4 and len(parsed.rows) == 4
    assert parsed.rows[0] == cdn.DailyRow("A", 102002, 124, 308407, "B,N,Q")
    assert parsed.rows[1].symbol == "BRK/B" and parsed.rows[1].facilities == "B,N,Q"
    assert parsed.rows[2].symbol == "ABRpD"  # raw SIP spelling, never upper-cased
    assert len(parsed.sha256) == 64
    assert (
        cdn.parse_daily_file(_file(crlf=False), family="CNMS", trade_date=DAY).sha256
        != parsed.sha256
    )


@pytest.mark.parametrize(
    ("raw", "expect"),
    [
        (_file(header="Date|Symbol|ShortVolume|TotalVolume|Market"), "line 1: header"),
        (_file(trailer="3"), "trailer says 3 rows, file holds 4"),
        (_file(trailer="20250102|X|1|0|1|Q"), "is not a row count"),
        (_file(["20250102|A|1|0|1"]), "5 fields, expected 6"),
        (_file(["20250103|A|1|0|1|Q"]), "date '20250103' != 20250102"),
        (_file(["20250102||1|0|1|Q"]), "empty symbol"),
        (_file(["20250102|A|1|0|1|Q", "20250102|A|1|0|1|Q"]), "duplicate symbol 'A'"),
        (_file(["20250102|A|abc|0|1|Q"]), "non-numeric volume"),
        (_file(["20250102|A|1e3|0|1|Q"]), "non-numeric volume"),
        (_file(["20250102|A|nan|0|1|Q"]), "non-numeric volume"),
        (_file(["20250102|A|-1|0|1|Q"]), "non-numeric volume"),
        (_file(["20250102|A|5|0|4|Q"]), "short 5 > total 4"),
        (_file(["20250102|A|5|6|9|Q"]), "exempt 6 > short 5"),
        (_file(["20250102|A|1|0|1|"]), "empty market"),
        (b"", "no header and trailer"),
    ],
)
def test_every_strict_rule_names_its_line(raw: bytes, expect: str) -> None:
    with pytest.raises(DailyFileFormatError, match=expect):
        cdn.parse_daily_file(raw, family="CNMS", trade_date=DAY)


def test_fractional_shares_are_kept_as_published() -> None:
    raw = _file(["20260930|A|388551.214956|45|1009929.273351|B,Q,N"])
    parsed = cdn.parse_daily_file(raw, family="CNMS", trade_date=date(2026, 9, 30))
    assert parsed.rows[0].short_volume == 388551.214956
    assert parsed.rows[0].total_volume == 1009929.273351


def test_names_and_urls() -> None:
    assert cdn.file_name("CNMS", DAY) == "CNMSshvol20250102.txt"
    assert (
        cdn.file_url("FORF", DAY)
        == "https://cdn.finra.org/equity/regsho/daily/FORFshvol20250102.txt"
    )
    assert cdn.normalise_facilities(" Q, B ,N,Q") == "B,N,Q"


def test_absent_is_only_404_or_s3_access_denied() -> None:
    assert cdn.is_absent(404, b"")
    assert cdn.is_absent(403, ABSENT_BODY)
    assert not cdn.is_absent(403, b"<html>blocked by WAF</html>")
    assert not cdn.is_absent(500, ABSENT_BODY)


# --------------------------------------------------------------------------- #
# client
# --------------------------------------------------------------------------- #
class _Response:
    def __init__(self, status: int, content: bytes = b"") -> None:
        self.status_code = status
        self.content = content
        self.headers: dict = {}


class _Session:
    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.urls: list[str] = []

    def get(self, url, params=None, timeout=None):
        self.urls.append(url)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _client(session) -> cdn.DailyFileClient:
    return cdn.DailyFileClient(session, sleep=lambda _w: None)


def test_fetch_day_parses_a_200() -> None:
    session = _Session(_Response(200, _file()))
    daily = _client(session).fetch_day("CNMS", DAY)
    assert daily is not None and len(daily.rows) == 4
    assert session.urls == [cdn.file_url("CNMS", DAY)]


def test_fetch_day_absent_returns_none() -> None:
    assert _client(_Session(_Response(403, ABSENT_BODY))).fetch_day("CNMS", DAY) is None
    assert _client(_Session(_Response(404))).fetch_day("CNMS", DAY) is None


def test_fetch_day_other_403_and_5xx_raise_cdn_error() -> None:
    with pytest.raises(CdnError) as info:
        _client(_Session(_Response(403, b"<html>blocked</html>"))).fetch_day(
            "CNMS", DAY
        )
    assert info.value.status == 403
    with pytest.raises(CdnError) as info:
        _client(_Session(*[_Response(503)] * 4)).fetch_day(
            "CNMS", DAY
        )  # retried, then raised
    assert info.value.status == 503


def test_fetch_day_retries_transient_then_succeeds() -> None:
    session = _Session(_Response(502), _Response(200, _file()))
    assert _client(session).fetch_day("CNMS", DAY) is not None
    assert len(session.urls) == 2


def test_unknown_family_is_rejected_before_any_request() -> None:
    session = _Session()
    with pytest.raises(ValueError, match="unknown file family"):
        _client(session).fetch_day("NOPE", DAY)
    assert session.urls == []
