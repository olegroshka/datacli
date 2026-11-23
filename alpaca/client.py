"""A small client for Alpaca's market data API (historical auctions and daily bars).

``GET {data_url}/auctions?symbols=A,B&start=..&end=..&feed=sip[&asof=..]``
answers ``{"auctions": {SYMBOL: [day, ...]}, "next_page_token": ...}`` where a
day is ``{"d": "YYYY-MM-DD", "o": [print, ...] | null, "c": [print, ...] | null}``
and a print is ``{"c": condition, "p": price, "s": size, "t": RFC-3339, "x": exchange}``.
``limit`` counts day records across all symbols of the request.

``GET {data_url}/bars?symbols=..&timeframe=1Day&adjustment=raw|split|all&...``
(:class:`BarsClient`) answers ``{"bars": {SYMBOL: [bar, ...]}, ...}`` with a bar
``{"t": RFC-3339, "o", "h", "l", "c", "v", "n", "vw"}``; ``limit`` counts bars.

The client knows HTTP, paging and the rate budget; it knows nothing about
what a print means (that is :mod:`alpaca.auctions`). Transient failures are
retried by the shared :func:`_http.request_with_retry`; what is left is mapped
onto :mod:`alpaca.errors`, which never carry a header or a key.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any, Callable, Mapping, Protocol, Sequence

from alpaca.auth import Credentials
from alpaca.errors import ApiError, AuthError, RateLimited, ServerError

log = logging.getLogger(__name__)

DayRecords = dict[str, list[dict[str, Any]]]


class HttpSession(Protocol):
    """The slice of ``requests.Session`` the client uses (a fake in tests)."""

    def get(self, url: str, **kwargs: Any) -> Any: ...


class RateLimiter:
    """At most ``per_minute`` requests a minute, spaced evenly.

    Even spacing keeps this process's draw on the shared key flat instead of
    bursting at the start of every minute.
    """

    def __init__(
        self,
        per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if per_minute < 1:
            raise ValueError("per_minute must be >= 1")
        self.interval = 60.0 / per_minute
        self._clock = clock
        self._sleep = sleep
        self._next = 0.0

    def wait(self) -> None:
        now = self._clock()
        if now < self._next:
            self._sleep(self._next - now)
            now = self._next
        self._next = now + self.interval


class AuctionsClient:
    """Paged reads of the historical auctions endpoint."""

    #: the endpoint under ``data_url``, the payload key and the name in messages
    endpoint = "auctions"

    def __init__(
        self,
        credentials: Credentials,
        *,
        data_url: str,
        session: HttpSession | None = None,
        feed: str = "sip",
        page_limit: int = 10_000,
        max_pages: int = 2_000,
        timeout: float = 30.0,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if session is None:
            import requests

            session = requests.Session()  # type: ignore[assignment]
        self._session: HttpSession = session  # type: ignore[assignment]
        self._headers = credentials.headers()
        self._url = data_url.rstrip("/") + "/" + self.endpoint
        self.feed = feed
        self.page_limit = page_limit
        self.max_pages = max_pages
        self.timeout = timeout
        self._limiter = limiter
        self._sleep = sleep
        self.requests_sent = 0

    def auctions(
        self,
        symbols: Sequence[str],
        start: dt.date,
        end: dt.date,
        *,
        asof: dt.date | None = None,
    ) -> tuple[DayRecords, int]:
        """Every day record for ``symbols`` in ``[start, end]``, all pages merged.

        Returns ``(records by Alpaca symbol, pages read)``. ``asof`` names the
        date at which each symbol identifies its company (Alpaca maps a renamed
        symbol by that date; by default today). Days outside ``[start, end]``
        can come back (a late official-close report carries the next day's
        timestamp); filtering them is the caller's job.
        """
        return self._paged(symbols, start, end, asof=asof, extra={})

    def _paged(
        self,
        symbols: Sequence[str],
        start: dt.date,
        end: dt.date,
        *,
        asof: dt.date | None,
        extra: Mapping[str, Any],
    ) -> tuple[DayRecords, int]:
        """Every record of ``symbols`` in ``[start, end]`` from :attr:`endpoint`, all pages merged."""
        if not symbols:
            return {}, 0
        if start > end:
            raise ValueError(f"start {start} is after end {end}")
        name = self.endpoint
        params: dict[str, Any] = {
            "symbols": ",".join(symbols),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "feed": self.feed,
            "limit": self.page_limit,
            **extra,
        }
        if asof is not None:
            params["asof"] = asof.isoformat()
        merged: DayRecords = {}
        pages = 0
        token: str | None = None
        while True:
            if pages >= self.max_pages:
                raise ApiError(
                    f"{name} paging did not end after {self.max_pages} pages"
                )
            payload = self._get({**params, **({"page_token": token} if token else {})})
            pages += 1
            body = payload.get(name) or {}
            if not isinstance(body, Mapping):
                raise ApiError(f"{name} payload is not an object keyed by symbol")
            for symbol, days in body.items():
                if days:
                    if not isinstance(days, list):
                        raise ApiError(f"{name} for {symbol} are not a list")
                    merged.setdefault(str(symbol), []).extend(days)
            token = payload.get("next_page_token") or None
            if token is None:
                return merged, pages

    # ----- transport -------------------------------------------------------- #
    def _get(self, params: Mapping[str, Any]) -> dict[str, Any]:
        import _http  # type: ignore[import-not-found]
        import requests

        if self._limiter is not None:
            self._limiter.wait()
        try:
            response = _http.request_with_retry(
                self._session,
                "GET",
                self._url,
                params=params,
                headers=self._headers,
                timeout=self.timeout,
                log=log,
                label=f"alpaca {self.endpoint}",
                sleep=self._sleep,
            )
        except requests.RequestException as exc:
            raise ApiError(
                f"{self.endpoint} request failed: {type(exc).__name__}"
            ) from None
        self.requests_sent += 1
        status = int(response.status_code)
        if status == 200:
            try:
                payload = response.json()
            except ValueError:
                raise ApiError(
                    f"{self.endpoint} response is not JSON", status=status
                ) from None
            if not isinstance(payload, dict):
                raise ApiError(
                    f"{self.endpoint} response is not a JSON object", status=status
                )
            return payload
        message = _message(response)
        if status in (401, 403):
            raise AuthError(f"Alpaca rejected the key pair: {message}", status=status)
        if status == 429:
            raise RateLimited(f"rate limited: {message}", status=status)
        if status >= 500:
            raise ServerError(f"server error: {message}", status=status)
        raise ApiError(f"{self.endpoint} request refused: {message}", status=status)


class BarsClient(AuctionsClient):
    """Paged reads of the historical bars endpoint (daily bars by default).

    ``adjustment`` is Alpaca's: ``raw`` (as traded), ``split`` or ``all``
    (splits and dividends). Volume is restated for splits under ``split`` and
    ``all``. Bars carry an RFC-3339 ``t`` at the session's midnight in New York.
    """

    endpoint = "bars"
    ADJUSTMENTS = ("raw", "split", "dividend", "all")

    def bars(
        self,
        symbols: Sequence[str],
        start: dt.date,
        end: dt.date,
        *,
        adjustment: str = "raw",
        timeframe: str = "1Day",
        asof: dt.date | None = None,
    ) -> tuple[DayRecords, int]:
        """Every bar of ``symbols`` in ``[start, end]``, all pages merged."""
        if adjustment not in self.ADJUSTMENTS:
            raise ValueError(f"unknown adjustment: {adjustment}")
        return self._paged(
            symbols,
            start,
            end,
            asof=asof,
            extra={"timeframe": timeframe, "adjustment": adjustment},
        )


def _message(response: Any) -> str:
    """Alpaca's ``message`` field, trimmed; never the request or its headers."""
    try:
        payload = response.json()
    except ValueError:
        return "no message"
    text = str(payload.get("message", "")) if isinstance(payload, dict) else ""
    return text[:200] or "no message"
