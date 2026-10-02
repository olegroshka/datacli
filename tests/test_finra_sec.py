"""finra.sec: the strict fails-to-deliver file parser and the SEC file client."""

from __future__ import annotations

import io
import sys
import zipfile
from datetime import date
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from finra import sec  # noqa: E402
from finra.errors import DailyFileFormatError, SecError  # noqa: E402

HALF = date(2026, 9, 1)
ROWS = [
    "20260901|B6S7WD106|NYXH|2508|NYXOAH S A SHS (BMU)|1.56",
    "20260903|037833100|AAPL|1332|APPLE INC;COM NPV|324.96",
    "20260903|084670702|BRKB|10|BERKSHIRE HATHAWAY INC;CL B|497.95",
    "20260915|12345X100|ZZZ|7|SOME CO;COM|.",
]


def _zip(
    rows=ROWS,
    *,
    header=sec.HEADER,
    count=None,
    quantity=None,
    name="cnsfails202609a.txt",
    members=1,
    encoding="utf-8",
) -> bytes:
    n = len(rows) if count is None else count
    q = sum(int(r.split("|")[3]) for r in rows) if quantity is None else quantity
    text = (
        "\n".join(
            [
                header,
                *rows,
                f"Trailer record count {n}",
                f"Trailer total quantity of shares {q}",
            ]
        )
        + "\n"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, text.encode(encoding))
        for i in range(members - 1):
            z.writestr(f"extra{i}.txt", b"")
    return buf.getvalue()


def test_names_urls_and_half_bounds() -> None:
    assert sec.half_name(date(2026, 9, 1)) == "cnsfails202609a"
    assert sec.half_name(date(2026, 9, 16)) == "cnsfails202609b"
    assert (
        sec.file_url(date(2018, 8, 16))
        == "https://www.sec.gov/files/data/fails-deliver-data/cnsfails201808b.zip"
    )
    assert sec.half_bounds(date(2026, 2, 16)) == (date(2026, 2, 16), date(2026, 2, 28))
    assert sec.half_bounds(date(2026, 12, 16)) == (
        date(2026, 12, 16),
        date(2026, 12, 31),
    )
    with pytest.raises(ValueError):
        sec.half_name(date(2026, 9, 2))


def test_parses_the_published_layout_and_checks_both_trailers() -> None:
    parsed = sec.parse_fails_file(_zip(), half_start=HALF)
    assert parsed.declared_count == 4 and parsed.declared_quantity == 3857
    assert parsed.rows[1] == sec.FailRow(
        date(2026, 9, 3), "037833100", "AAPL", 1332, "APPLE INC;COM NPV", 324.96
    )
    assert parsed.rows[3].price is None  # '.' means no price
    assert len(parsed.sha256) == 64
    # old files carry cp1252 bytes
    parsed = sec.parse_fails_file(
        _zip(["20260901|X|SYM|1|CAF\xc9 CO|1.0"], encoding="cp1252"), half_start=HALF
    )
    assert parsed.rows[0].description == "CAF\xc9 CO"


@pytest.mark.parametrize(
    ("raw", "expect"),
    [
        (b"not a zip", "not a zip file"),
        (_zip(members=2), "zip holds 2 members"),
        (
            _zip(header="SETTLEMENT DATE|CUSIP|SYMBOL|QUANTITY|DESCRIPTION|PRICE"),
            "line 1: header",
        ),
        (_zip(count=3), "trailer says 3 rows"),
        (_zip(quantity=1), "trailer says 1 shares"),
        (_zip(["20261001|X|SYM|1|D|1.0"]), "outside 2026-09-01..2026-09-30"),
        (_zip(["2026090|X|SYM|1|D|1.0"]), "outside"),
        (_zip(["20260901|X|SYM|1|D"]), "5 fields, expected 6"),
        (_zip(["20260901||SYM|1|D|1.0"]), "empty CUSIP or symbol"),
        (
            _zip(["20260901|X|SYM|1|D|1.0", "20260901|X|OTHER|2|D|1.0"]),
            r"duplicate \(settlement date, CUSIP\)",
        ),
        (_zip(["20260901|X|SYM|1.5|D|1.0"], quantity=1), "non-integer quantity"),
        (_zip(["20260901|X|SYM|1|D|abc"]), "non-numeric price"),
    ],
)
def test_every_strict_rule_names_its_line(raw: bytes, expect: str) -> None:
    with pytest.raises(DailyFileFormatError, match=expect):
        sec.parse_fails_file(raw, half_start=HALF)


def test_missing_trailers_are_an_error() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "f.txt",
            (sec.HEADER + "\n" + ROWS[0] + "\nTrailer record count 1\n").encode(),
        )
    with pytest.raises(DailyFileFormatError, match="trailers"):
        sec.parse_fails_file(buf.getvalue(), half_start=HALF)


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
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append((url, dict(headers or {})))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_client_sends_the_declared_user_agent_and_parses() -> None:
    session = _Session(_Response(200, _zip()))
    client = sec.SecFileClient(
        session, " Name contact@example.org ", sleep=lambda _w: None
    )
    parsed = client.fetch_half(HALF)
    assert parsed is not None and len(parsed.rows) == 4
    url, headers = session.calls[0]
    assert url.endswith("/cnsfails202609a.zip")
    assert headers["User-Agent"] == "Name contact@example.org"


def test_client_falls_back_to_the_alternate_path_on_404() -> None:
    session = _Session(_Response(404), _Response(200, _zip()))
    client = sec.SecFileClient(session, "n c@x.org", sleep=lambda _w: None)
    assert client.fetch_half(HALF) is not None
    assert [u.split("/files/")[1] for u, _ in session.calls] == [
        "data/fails-deliver-data/cnsfails202609a.zip",
        "data/other/fails-deliver-data/cnsfails202609a.zip",
    ]


def test_the_b_file_may_carry_the_fifteenth() -> None:
    raw = _zip(
        ["20260715|X|SYM|1|D|1.0", "20260731|Y|SYM2|2|D|1.0"],
        name="cnsfails202607b.txt",
    )
    parsed = sec.parse_fails_file(raw, half_start=date(2026, 7, 16))
    assert [r.settlement_date for r in parsed.rows] == [
        date(2026, 7, 15),
        date(2026, 7, 31),
    ]
    assert sec.month_bounds(date(2026, 7, 16)) == (date(2026, 7, 1), date(2026, 7, 31))


def test_client_404_is_absent_403_is_a_policy_error() -> None:
    assert (
        sec.SecFileClient(
            _Session(_Response(404), _Response(404)), "n c@x.org", sleep=lambda _w: None
        ).fetch_half(HALF)
        is None
    )
    with pytest.raises(SecError, match="SEC_USER_AGENT"):
        sec.SecFileClient(
            _Session(_Response(403, b"<html>")), "n c@x.org", sleep=lambda _w: None
        ).fetch_half(HALF)
    with pytest.raises(SecError) as info:
        sec.SecFileClient(
            _Session(*[_Response(503)] * 4), "n c@x.org", sleep=lambda _w: None
        ).fetch_half(HALF)
    assert info.value.status == 503
    with pytest.raises(ValueError, match="declared user agent"):
        sec.SecFileClient(_Session(), "   ")


def test_user_agent_lookup_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sec,
        "read_user_env",
        lambda name: " from env " if name == sec.USER_AGENT_VAR else "",
    )
    assert sec.user_agent() == "from env"
    monkeypatch.setattr(sec, "read_user_env", lambda name: "")
    import config as eodhd_config  # type: ignore[import-not-found]

    monkeypatch.setattr(
        eodhd_config,
        "section",
        lambda name: {"sec_user_agent": "from config"} if name == "finra" else {},
    )
    assert sec.user_agent() == "from config"
    monkeypatch.setattr(eodhd_config, "section", lambda name: {})
    assert sec.user_agent() is None
