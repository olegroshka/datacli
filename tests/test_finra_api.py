"""finra.api: filters, metadata, partitions, paging and error mapping, all offline."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from finra import api, auth  # noqa: E402
from finra.errors import (  # noqa: E402
    ApiError,
    AuthError,
    EntitlementError,
    NotFound,
    RateLimited,
    ServerError,
    TooManyRows,
)

JSON = {"Content-Type": "application/json"}


class _Response:
    def __init__(self, status: int, body=None, headers=None) -> None:
        self.status_code = status
        self._body = body
        self.headers = dict(headers or {})
        if body is not None and "Content-Type" not in self.headers:
            self.headers["Content-Type"] = "application/json"

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _Session:
    """Scripted outcomes for any verb; records every call."""

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def _send(self, method, url, params=None, timeout=None, json=None, headers=None):
        self.calls.append(
            {"method": method, "url": url, "json": json, "headers": dict(headers or {})}
        )
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def get(self, url, **kw):
        return self._send("GET", url, **kw)

    def post(self, url, **kw):
        return self._send("POST", url, **kw)


def _client(session, provider=None, **kw) -> api.QueryApiClient:
    kw.setdefault("sleep", lambda _w: None)
    return api.QueryApiClient(session, provider, **kw)


def _page(rows, total, max_limit=None, request_id="r1") -> _Response:
    headers = {"record-total": str(total), "finra-api-request-id": request_id}
    if max_limit is not None:
        headers["record-max-limit"] = str(max_limit)
    return _Response(200, rows, headers)


# --------------------------------------------------------------------------- #
# filters
# --------------------------------------------------------------------------- #
def test_filters_serialise_only_what_is_set() -> None:
    assert api.Filters().payload() == {}
    assert api.Filters.equal("tradeReportDate", "2026-09-30").payload() == {
        "compareFilters": [
            {
                "compareType": "EQUAL",
                "fieldName": "tradeReportDate",
                "fieldValue": "2026-09-30",
            }
        ]
    }
    both = api.Filters(
        date_range=(api.DateRangeFilter("settlementDate", "2026-01-01", "2026-01-31"),),
        domain=(api.DomainFilter("symbolCode", ("A", "AA")),),
    )
    assert both.payload() == {
        "dateRangeFilters": [
            {
                "fieldName": "settlementDate",
                "startDate": "2026-01-01",
                "endDate": "2026-01-31",
            }
        ],
        "domainFilters": [{"fieldName": "symbolCode", "values": ["A", "AA"]}],
    }


# --------------------------------------------------------------------------- #
# metadata + partitions (anonymous)
# --------------------------------------------------------------------------- #
def test_metadata_parses_fields_and_partitions() -> None:
    body = {
        "datasetGroup": "OTCMARKET",
        "datasetName": "REGSHODAILY",
        "description": "Reg SHO Daily File",
        "partitionFields": ["tradeReportDate"],
        "fields": [
            {
                "name": "tradeReportDate",
                "type": "Date",
                "format": "yyyy-MM-dd",
                "description": "Trade Date",
            },
            {"name": "shortParQuantity", "type": "Number", "description": None},
        ],
    }
    session = _Session(_Response(200, body))
    meta = _client(session).metadata("otcMarket", "regShoDaily")
    assert meta.partition_fields == ("tradeReportDate",)
    assert [f.name for f in meta.fields] == ["tradeReportDate", "shortParQuantity"]
    assert meta.fields[0].format == "yyyy-MM-dd" and meta.fields[1].description == ""
    call = session.calls[0]
    assert call["method"] == "GET"
    assert (
        call["url"] == "https://api.finra.org/metadata/group/otcMarket/name/regShoDaily"
    )
    assert call["headers"] == {"Accept": "application/json"}  # anonymous: no bearer


def test_partitions_are_sorted_unique_first_values() -> None:
    body = {
        "partitionFields": ["tradeReportDate"],
        "availablePartitions": [
            {"partitions": ["2026-09-30"]},
            {"partitions": ["2026-09-28"]},
            {"partitions": ["2026-09-30"]},
            {"partitions": []},
        ],
    }
    session = _Session(_Response(200, body))
    assert _client(session).partitions("otcMarket", "regShoDaily") == [
        "2026-09-28",
        "2026-09-30",
    ]


def test_malformed_payloads_raise_api_error() -> None:
    with pytest.raises(ApiError, match="malformed partitions"):
        _client(_Session(_Response(200, {"nope": 1}))).partitions("g", "n")
    with pytest.raises(ApiError, match="not an object"):
        _client(_Session(_Response(200, [1, 2]))).metadata("g", "n")
    with pytest.raises(ApiError, match="expected JSON"):
        _client(_Session(_Response(200, None, {"Content-Type": "text/csv"}))).metadata(
            "g", "n"
        )
    with pytest.raises(ApiError, match="not valid JSON"):
        _client(_Session(_Response(200, ValueError("bad"), JSON))).metadata("g", "n")


# --------------------------------------------------------------------------- #
# paging
# --------------------------------------------------------------------------- #
def test_query_pages_until_record_total() -> None:
    session = _Session(
        _page([{"i": 1}, {"i": 2}], total=5),
        _page([{"i": 3}, {"i": 4}], total=5),
        _page([{"i": 5}], total=5),
    )
    waits: list[float] = []
    client = _client(session, page_size=2, sleep=waits.append, pace=0.5)
    rows = client.query(
        "otcMarket",
        "regShoDaily",
        filters=api.Filters.equal("tradeReportDate", "2026-09-30"),
    )
    assert [r["i"] for r in rows] == [1, 2, 3, 4, 5]
    assert [c["json"]["offset"] for c in session.calls] == [0, 2, 4]
    assert all(c["json"]["limit"] == 2 for c in session.calls)
    assert all("compareFilters" in c["json"] for c in session.calls)
    assert all(
        c["headers"]["Content-Type"] == "application/json" for c in session.calls
    )
    assert waits == [0.5, 0.5]  # paced between pages, not after the last


def test_query_honours_limit_and_fields() -> None:
    session = _Session(_page([{"i": 1}, {"i": 2}, {"i": 3}], total=10))
    rows = _client(session, page_size=5).query("g", "n", fields=("i",), limit=3)
    assert len(rows) == 3
    assert session.calls[0]["json"] == {"fields": ["i"], "limit": 3, "offset": 0}


def test_query_shrinks_to_record_max_limit() -> None:
    session = _Session(
        _page([{"i": 1}, {"i": 2}], total=4, max_limit=2),
        _page([{"i": 3}, {"i": 4}], total=4, max_limit=2),
    )
    rows = _client(session, page_size=5000).query("g", "n")
    assert len(rows) == 4
    assert [c["json"]["limit"] for c in session.calls] == [5000, 2]


def test_empty_result_is_204_with_empty_body() -> None:
    session = _Session(_Response(204, None, {"record-total": "0"}))
    pages = list(_client(session).query_pages("g", "n"))
    assert len(pages) == 1 and pages[0].rows == [] and pages[0].total == 0


def test_short_page_without_total_header_ends_paging() -> None:
    session = _Session(_Response(200, [{"i": 1}]))
    assert len(_client(session, page_size=10).query("g", "n")) == 1
    assert len(session.calls) == 1


def test_offset_ceiling_raises_too_many_rows() -> None:
    many = [{"i": i} for i in range(5000)]
    session = _Session(*[_page(many, total=1_000_000)] * 101)
    with pytest.raises(TooManyRows, match="narrow the filter"):
        _client(session, pace=0).query("g", "n")
    assert (
        len(session.calls) == 100
    )  # stopped at the 500,000 offset, not at FINRA's wall


def test_non_object_rows_are_rejected() -> None:
    with pytest.raises(ApiError, match="non-object rows"):
        _client(_Session(_Response(200, [{"a": 1}, 2]))).query("g", "n")
    with pytest.raises(ApiError, match="not a list"):
        _client(_Session(_Response(200, {"a": 1}))).query("g", "n")


# --------------------------------------------------------------------------- #
# auth behaviour
# --------------------------------------------------------------------------- #
class _Tokens:
    """A stand-in TokenProvider that hands out numbered tokens."""

    def __init__(self) -> None:
        self.n = 0
        self.invalidated = 0

    def token(self) -> str:
        self.n += 1
        return f"tok-{self.n}"

    def invalidate(self) -> None:
        self.invalidated += 1


def test_bearer_header_is_sent_and_401_is_retried_once_with_a_fresh_token() -> None:
    session = _Session(_Response(401, {}, JSON), _Response(200, {"fields": []}))
    tokens = _Tokens()
    client = _client(session, tokens)  # type: ignore[arg-type]
    client.metadata("g", "n")
    assert [c["headers"]["Authorization"] for c in session.calls] == [
        "Bearer tok-1",
        "Bearer tok-2",
    ]
    assert tokens.invalidated == 1


def test_second_401_raises_auth_error() -> None:
    session = _Session(
        _Response(401, {}, JSON), _Response(401, {}, {"finra-api-request-id": "x"})
    )
    with pytest.raises(AuthError) as info:
        _client(session, _Tokens()).metadata("g", "n")  # type: ignore[arg-type]
    assert info.value.status == 401 and info.value.request_id == "x"


def test_anonymous_401_and_403_say_how_to_authenticate() -> None:
    with pytest.raises(EntitlementError, match="set FINRA_CLIENT_ID and FINRA_API_KEY"):
        _client(_Session(_Response(401))).metadata("g", "n")
    with pytest.raises(EntitlementError, match="set FINRA_CLIENT_ID and FINRA_API_KEY"):
        _client(_Session(_Response(403))).metadata("g", "n")


def test_authenticated_403_is_an_entitlement_problem() -> None:
    with pytest.raises(EntitlementError, match="lack entitlement"):
        _client(_Session(_Response(403)), _Tokens()).metadata("g", "n")  # type: ignore[arg-type]


def test_other_statuses_map_to_typed_errors() -> None:
    assert isinstance(_err(404), NotFound)
    assert isinstance(_err(429), RateLimited)
    assert isinstance(_err(500), ServerError)
    assert isinstance(_err(418), ApiError)


def _err(status: int) -> Exception:
    # 429/5xx are retried by the shared helper first (4 attempts), then mapped
    session = _Session(*[_Response(status)] * 4)
    with pytest.raises(Exception) as info:
        _client(session).metadata("g", "n")
    return info.value


def test_real_token_provider_plugs_in() -> None:
    """End to end with the real TokenProvider: FIP once, bearer on the data call."""
    session = _Session(
        _Response(200, {"access_token": "real-tok", "expires_in": 3600}),
        _Response(200, {"fields": []}),
    )
    provider = auth.TokenProvider(
        session, auth.Credentials("id", "s"), sleep=lambda _w: None
    )
    _client(session, provider).metadata("g", "n")
    assert session.calls[0]["url"] == auth.TOKEN_URL
    assert session.calls[1]["headers"]["Authorization"] == "Bearer real-tok"


def test_page_size_is_validated() -> None:
    with pytest.raises(ValueError):
        api.QueryApiClient(_Session(), page_size=0)
    with pytest.raises(ValueError):
        api.QueryApiClient(_Session(), page_size=5001)
    with pytest.raises(ValueError):
        _client(_Session()).query("g", "n", limit=0)
