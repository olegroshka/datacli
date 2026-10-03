"""The AMF's net short positions, per holder (France), from data.gouv.fr.

Dataset ``62738e33f1be79935d3e5553`` ("Historique des positions courtes
nettes sur actions rendues publiques depuis le 1er Novembre 2012"): one CSV
resource regenerated daily under a timestamped name
(``export_od_vad_YYYYMMDDHHMMSS_....csv``, about 5 MB, 40,903 rows on
2026-10-02), semicolon-separated, UTF-8 with a byte-order mark, columns::

    Detenteur de la position courte nette; Legal Entity Identifier detenteur;
    Emetteur / issuer; Ratio; code ISIN; Date de debut position;
    Date de debut de publication position; Date de fin de publication position

``Ratio`` is the percentage of the share capital; the three dates are the
position date, the first day of publication and the last (empty while the
row is current). The resource URL changes every day, so the fetch asks the
dataset API for the newest CSV resource first.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import re
import urllib.request
from typing import Any

import pandas as pd

from registers.common import Parsed, RegisterError, canonical

MARKET = "fr"
DATASET_ID = "62738e33f1be79935d3e5553"
API_URL = f"https://www.data.gouv.fr/api/1/datasets/{DATASET_ID}/"
COLUMNS: tuple[str, ...] = (
    "Detenteur de la position courte nette",
    "Legal Entity Identifier detenteur",
    "Emetteur / issuer",
    "Ratio",
    "code ISIN",
    "Date de debut position",
    "Date de debut de publication position",
    "Date de fin de publication position",
)
_STAMP = re.compile(r"export_od_vad_(\d{4})(\d{2})(\d{2})\d{6}")
USER_AGENT = "datacli registers (research; see repository)"


def latest_resource(payload: dict[str, Any]) -> tuple[str, dt.date]:
    """The newest CSV resource's URL and file date from the dataset API payload."""
    resources = [r for r in payload.get("resources", []) if str(r.get("format", "")).lower() == "csv" and r.get("url")]
    if not resources:
        raise RegisterError("the dataset lists no CSV resource")
    newest = max(resources, key=lambda r: str(r.get("last_modified") or ""))
    url = str(newest["url"])
    match = _STAMP.search(url)
    if match:
        year, month, day = (int(g) for g in match.groups())
        return url, dt.date(year, month, day)
    modified = str(newest.get("last_modified") or "")[:10]
    try:
        return url, dt.date.fromisoformat(modified)
    except ValueError:
        raise RegisterError(f"cannot date the resource {url}") from None


def _get(url: str, *, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (fixed https URLs)
        return response.read()


def fetch(api_url: str = API_URL, *, timeout: float = 120.0) -> tuple[bytes, str]:
    payload = json.loads(_get(api_url, timeout=timeout).decode("utf-8"))
    url, file_date = latest_resource(payload)
    data = _get(url, timeout=timeout)
    # the file date travels in the URL; parse() reads it back from the source label
    return data, url


def file_date_of(source: str) -> dt.date | None:
    match = _STAMP.search(source)
    if not match:
        return None
    year, month, day = (int(g) for g in match.groups())
    return dt.date(year, month, day)


def parse(data: bytes, *, file_date: dt.date | None = None) -> Parsed:
    """Parse the CSV strictly: the eight French headers, semicolons, UTF-8 with BOM."""
    try:
        frame = pd.read_csv(io.BytesIO(data), sep=";", encoding="utf-8-sig", dtype=str, keep_default_na=False)
    except Exception as exc:  # noqa: BLE001
        raise RegisterError(f"not a readable CSV: {exc}") from None
    if tuple(frame.columns) != COLUMNS:
        raise RegisterError(f"unexpected columns: {list(frame.columns)}")
    frame = frame.rename(
        columns={
            COLUMNS[0]: "holder",
            COLUMNS[1]: "holder_lei",
            COLUMNS[2]: "issuer",
            COLUMNS[3]: "net_short_pct",
            COLUMNS[4]: "isin",
            COLUMNS[5]: "position_date",
            COLUMNS[6]: "published_from",
            COLUMNS[7]: "published_to",
        }
    )
    for column in ("published_to", "holder_lei"):
        frame[column] = frame[column].replace({"": None})
    if file_date is None:
        latest = pd.to_datetime(frame["published_from"], errors="coerce").max()
        if pd.isna(latest):
            raise RegisterError("no publication dates in the file")
        file_date = latest.date()
    return Parsed(file_date, canonical(frame, market=MARKET, file_date=file_date))
