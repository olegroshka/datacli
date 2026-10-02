"""A client for the FINRA Query API (``https://api.finra.org``).

Three verbs cover the whole surface datacli needs:

- :meth:`QueryApiClient.metadata` -- a dataset's fields and partition fields;
- :meth:`QueryApiClient.partitions` -- the published partition values (for a
  date-partitioned dataset, the dates FINRA has published);
- :meth:`QueryApiClient.query` -- rows matching a :class:`Filters`, paged
  transparently with ``limit`` / ``offset`` and the ``record-total`` header.

Wire facts the client encodes (probed 2026-10-01, see the design doc): JSON
only with ``Accept: application/json`` (CSV otherwise); an empty result is
``204`` with an empty body; at most 5,000 rows per page and 500,000 as an
offset; ``record-total`` / ``record-max-limit`` response headers; a
``finra-api-request-id`` header on every response. Public datasets work
without a token; entitled ones need a :class:`~finra.auth.TokenProvider`.

The client returns plain Python (``list[dict]``) and knows nothing about
pandas, disk or what a dataset means; that is the dataset layer's job.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Mapping, Sequence

from finra.auth import TokenProvider
from finra.errors import (
    ApiError,
    AuthError,
    EntitlementError,
    NotFound,
    RateLimited,
    ServerError,
    TooManyRows,
)

BASE_URL = "https://api.finra.org"
MAX_PAGE_SIZE = 5000
MAX_OFFSET = 500_000
DEFAULT_TIMEOUT = 60.0
DEFAULT_PACE_SECONDS = 0.1  # between pages; the documented ceiling is 1,200/min
REQUEST_ID_HEADER = "finra-api-request-id"


# --------------------------------------------------------------------------- #
# filters
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CompareFilter:
    """``fieldName <op> fieldValue``; ``op`` is EQUAL, GREATER, LESSER, GTE, LTE, NOT_EQUAL, BEGINS_WITH."""

    field: str
    value: str
    op: str = "EQUAL"

    def payload(self) -> dict[str, str]:
        return {
            "compareType": self.op,
            "fieldName": self.field,
            "fieldValue": self.value,
        }


@dataclass(frozen=True)
class DateRangeFilter:
    """``start <= fieldName <= end`` on a date field (ISO ``YYYY-MM-DD``)."""

    field: str
    start: str
    end: str

    def payload(self) -> dict[str, str]:
        return {"fieldName": self.field, "startDate": self.start, "endDate": self.end}


@dataclass(frozen=True)
class DomainFilter:
    """``fieldName IN values``."""

    field: str
    values: tuple[str, ...]

    def payload(self) -> dict[str, Any]:
        return {"fieldName": self.field, "values": list(self.values)}


@dataclass(frozen=True)
class Filters:
    """The three FINRA filter families, serialised only when non-empty."""

    compare: tuple[CompareFilter, ...] = ()
    date_range: tuple[DateRangeFilter, ...] = ()
    domain: tuple[DomainFilter, ...] = ()

    @classmethod
    def equal(cls, field_name: str, value: str) -> "Filters":
        return cls(compare=(CompareFilter(field_name, value),))

    @classmethod
    def between(cls, field_name: str, start: str, end: str) -> "Filters":
        return cls(date_range=(DateRangeFilter(field_name, start, end),))

    def payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.compare:
            out["compareFilters"] = [f.payload() for f in self.compare]
        if self.date_range:
            out["dateRangeFilters"] = [f.payload() for f in self.date_range]
        if self.domain:
            out["domainFilters"] = [f.payload() for f in self.domain]
        return out


# --------------------------------------------------------------------------- #
# metadata
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Field:
    name: str
    type: str
    description: str = ""
    format: str = ""


@dataclass(frozen=True)
class DatasetMetadata:
    group: str
    name: str
    description: str
    partition_fields: tuple[str, ...]
    fields: tuple[Field, ...]

    @classmethod
    def from_json(cls, body: Mapping[str, Any]) -> "DatasetMetadata":
        try:
            fields = tuple(
                Field(
                    name=str(f["name"]),
                    type=str(f.get("type", "")),
                    description=str(f.get("description", "") or ""),
                    format=str(f.get("format", "") or ""),
                )
                for f in body.get("fields", [])
            )
            return cls(
                group=str(body.get("datasetGroup", "")),
                name=str(body.get("datasetName", "")),
                description=str(body.get("description", "") or ""),
                partition_fields=tuple(str(p) for p in body.get("partitionFields", [])),
                fields=fields,
            )
        except (KeyError, TypeError, AttributeError) as exc:
            raise ApiError(f"malformed metadata payload: {exc}") from None


@dataclass(frozen=True)
class Page:
    """One page of a query, with the headers that drive paging."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    total: int | None = None
    max_limit: int | None = None
    request_id: str | None = None


# --------------------------------------------------------------------------- #
# client
# --------------------------------------------------------------------------- #
class QueryApiClient:
    """Metadata, partitions and paged queries against one FINRA API host.

    Args:
        session: anything with ``get`` / ``post`` like :class:`requests.Session`.
        token_provider: ``None`` for anonymous access to public datasets.
        page_size: rows per page, capped by FINRA at 5,000 and further by the
            ``record-max-limit`` header when the server says so.
        pace: seconds to wait between pages (politeness, not correctness).
        sleep: injectable clock for tests; also used by the retry backoff.
    """

    def __init__(
        self,
        session: Any,
        token_provider: TokenProvider | None = None,
        *,
        base_url: str = BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        page_size: int = MAX_PAGE_SIZE,
        pace: float = DEFAULT_PACE_SECONDS,
        sleep: Callable[[float], None] | None = None,
        log: logging.Logger | None = None,
    ) -> None:
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be in 1..{MAX_PAGE_SIZE}")
        self._session = session
        self._tokens = token_provider
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._page_size = page_size
        self._pace = pace
        self._sleep = time.sleep if sleep is None else sleep
        self._log = log

    @property
    def authenticated(self) -> bool:
        return self._tokens is not None

    # ----- verbs ----------------------------------------------------------- #
    def metadata(self, group: str, name: str) -> DatasetMetadata:
        response = self._request("GET", f"/metadata/group/{group}/name/{name}")
        body = self._json(response)
        if not isinstance(body, Mapping):
            raise ApiError(
                "metadata payload is not an object", status=response.status_code
            )
        return DatasetMetadata.from_json(body)

    def partitions(self, group: str, name: str) -> list[str]:
        """Sorted first-partition values (ISO dates for date-partitioned datasets)."""
        response = self._request("GET", f"/partitions/group/{group}/name/{name}")
        body = self._json(response)
        try:
            values = {
                str(entry["partitions"][0])
                for entry in body["availablePartitions"]  # type: ignore[index]
                if entry.get("partitions")
            }
        except (KeyError, TypeError, IndexError, AttributeError) as exc:
            raise ApiError(f"malformed partitions payload: {exc}") from None
        return sorted(values)

    def query(
        self,
        group: str,
        name: str,
        *,
        filters: Filters | None = None,
        fields: Sequence[str] = (),
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """All rows matching ``filters`` (at most ``limit``), across pages."""
        rows: list[dict[str, Any]] = []
        for page in self.query_pages(
            group, name, filters=filters, fields=fields, limit=limit
        ):
            rows.extend(page.rows)
        return rows

    def query_pages(
        self,
        group: str,
        name: str,
        *,
        filters: Filters | None = None,
        fields: Sequence[str] = (),
        limit: int | None = None,
    ) -> Iterator[Page]:
        """Yield pages until ``record-total`` is reached, ``limit`` is met, or a short page arrives.

        Raises:
            TooManyRows: when the next page would start past FINRA's offset
                ceiling; narrow the filter (e.g. one partition per query).
        """
        if limit is not None and limit < 1:
            raise ValueError("limit must be >= 1")
        path = f"/data/group/{group}/name/{name}"
        base: dict[str, Any] = dict((filters or Filters()).payload())
        if fields:
            base["fields"] = list(fields)
        offset = 0
        fetched = 0
        size = self._page_size
        while True:
            if limit is not None:
                size = min(size, limit - fetched)
            response = self._request(
                "POST", path, json={**base, "limit": size, "offset": offset}
            )
            page = self._page(response)
            yield page
            fetched += len(page.rows)
            offset += len(page.rows)
            if page.max_limit is not None and 0 < page.max_limit < size:
                size = page.max_limit
            if not page.rows:
                return
            if limit is not None and fetched >= limit:
                return
            if page.total is not None:
                if offset >= page.total:
                    return
            elif len(page.rows) < size:
                return
            if offset >= MAX_OFFSET:
                raise TooManyRows(
                    f"{group}/{name}: {page.total or 'more than ' + str(offset)} rows exceed "
                    f"FINRA's offset ceiling of {MAX_OFFSET:,}; narrow the filter",
                    request_id=page.request_id,
                )
            if self._pace > 0:
                self._sleep(self._pace)

    # ----- plumbing -------------------------------------------------------- #
    def _request(self, method: str, path: str, *, json: Any = None) -> Any:
        from _http import request_with_retry  # type: ignore[import-not-found]

        url = self._base_url + path
        headers: dict[str, str] = {"Accept": "application/json"}
        if json is not None:
            headers["Content-Type"] = "application/json"
        response: Any = None
        for attempt in (1, 2):
            if self._tokens is not None:
                headers["Authorization"] = f"Bearer {self._tokens.token()}"
            response = request_with_retry(
                self._session,
                method,
                url,
                json=json,
                headers=headers,
                timeout=self._timeout,
                log=self._log,
                label=path,
                sleep=self._sleep,
            )
            if (
                response.status_code == 401
                and self._tokens is not None
                and attempt == 1
            ):
                self._tokens.invalidate()  # expired mid-run: one fresh token, one retry
                continue
            break
        self._raise_for_status(response, path)
        return response

    def _raise_for_status(self, response: Any, path: str) -> None:
        status = int(response.status_code)
        if 200 <= status < 300:
            return
        request_id = _header(response, REQUEST_ID_HEADER)
        kwargs: dict[str, Any] = {"status": status, "request_id": request_id}
        if status == 401:
            if self.authenticated:
                raise AuthError(f"{path}: bearer token rejected", **kwargs)
            raise EntitlementError(
                f"{path}: needs credentials; set FINRA_CLIENT_ID and FINRA_API_KEY",
                **kwargs,
            )
        if status == 403:
            if self.authenticated:
                raise EntitlementError(
                    f"{path}: credentials lack entitlement", **kwargs
                )
            raise EntitlementError(
                f"{path}: forbidden anonymously; set FINRA_CLIENT_ID and FINRA_API_KEY",
                **kwargs,
            )
        if status == 404:
            raise NotFound(f"{path}: not found", **kwargs)
        if status == 429:
            raise RateLimited(f"{path}: rate limited", **kwargs)
        if status >= 500:
            raise ServerError(f"{path}: server error", **kwargs)
        raise ApiError(f"{path}: unexpected status", **kwargs)

    @staticmethod
    def _json(response: Any) -> Any:
        """The decoded body, or ``None`` for an empty ``204``."""
        if response.status_code == 204:
            return None
        content_type = str(_header(response, "Content-Type") or "")
        if "json" not in content_type.lower():
            raise ApiError(
                f"expected JSON, got {content_type or 'no content type'}",
                status=response.status_code,
                request_id=_header(response, REQUEST_ID_HEADER),
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ApiError(
                f"body is not valid JSON: {exc}",
                status=response.status_code,
                request_id=_header(response, REQUEST_ID_HEADER),
            ) from None

    def _page(self, response: Any) -> Page:
        body = self._json(response)
        rows: list[dict[str, Any]]
        if body is None:
            rows = []
        elif isinstance(body, list):
            rows = [dict(r) for r in body if isinstance(r, Mapping)]
            if len(rows) != len(body):
                raise ApiError(
                    "query payload holds non-object rows", status=response.status_code
                )
        else:
            raise ApiError("query payload is not a list", status=response.status_code)
        return Page(
            rows=rows,
            total=_int_header(response, "record-total"),
            max_limit=_int_header(response, "record-max-limit"),
            request_id=_header(response, REQUEST_ID_HEADER),
        )


def _header(response: Any, name: str) -> str | None:
    try:
        value = response.headers.get(name)
    except AttributeError:
        return None
    return str(value) if value is not None and value != "" else None


def _int_header(response: Any, name: str) -> int | None:
    value = _header(response, name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None
