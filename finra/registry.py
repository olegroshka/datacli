"""The FINRA datasets datacli knows about: one :class:`DatasetSpec` per dataset.

Add a dataset by dropping an entry here and a provider module next to
``short_volume.py``. ``api_group`` / ``api_name`` address it on the Query
API; ``cdn_family`` names the public daily-file family when one exists
(``None`` for entitled, API-only datasets); ``first_date`` is the earliest
publication the fetcher may plan for; ``entitled`` says whether the API needs
credentials for it; ``parts`` lists the secondary partition values fetched
(the weekly flow data is partitioned by week *and* tier).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    summary: str
    api_group: str
    api_name: str
    partition_field: str
    first_date: date
    key_cols: tuple[str, ...]
    cdn_family: str | None = None
    entitled: bool = False
    part_field: str = ""  # secondary API partition field, if any
    parts: tuple[str, ...] = ()  # the values of it that are fetched
    subdir: str = "daily"  # where the partition files live under the dataset dir
    cadence: str = "daily"  # daily | weekly | semimonthly
    sources: tuple[str, ...] = ()  # transports; empty = cdn+api or api, from cdn_family

    @property
    def transport(self) -> str:
        """Where routine fetches read from: the public file family, the API, or the SEC."""
        if self.sources and self.sources[0] not in ("cdn", "api"):
            return self.sources[0]
        return f"cdn:{self.cdn_family}" if self.cdn_family else "api"

    @property
    def api_addressable(self) -> bool:
        return bool(self.api_group and self.api_name)


DATASETS: dict[str, DatasetSpec] = {
    s.name: s
    for s in (
        DatasetSpec(
            name="short_volume",
            summary="Reg SHO daily short sale volume per symbol, consolidated NMS",
            api_group="otcMarket",
            api_name="regShoDaily",
            partition_field="tradeReportDate",
            first_date=date(2018, 8, 1),
            key_cols=("date", "symbol"),
            cdn_family="CNMS",
            entitled=False,
        ),
        DatasetSpec(
            name="weekly_flow",
            summary="Weekly ATS / OTC trade flow per symbol and venue, NMS tiers 1 and 2",
            api_group="otcMarket",
            api_name="weeklySummary",
            partition_field="weekStartDate",
            first_date=date(2021, 12, 6),
            key_cols=(
                "week_start",
                "tier",
                "summary_type",
                "symbol",
                "mpid",
                "firm_crd",
                "product_type",
                "issue_name",
            ),
            cdn_family=None,
            entitled=True,
            part_field="tierIdentifier",
            parts=(
                "T1",
                "T2",
            ),  # OTCE (OTC equities) excluded like OTC for short volume
            subdir="weekly",
            cadence="weekly",
        ),
        DatasetSpec(
            name="short_interest",
            summary="Consolidated short interest per symbol at each settlement date, all market classes",
            api_group="otcMarket",
            api_name="consolidatedShortInterest",
            partition_field="settlementDate",
            first_date=date(2017, 12, 29),
            key_cols=("settlement_date", "symbol"),
            cdn_family=None,
            entitled=True,
            subdir="settlement",
            cadence="semimonthly",
        ),
        DatasetSpec(
            name="fails_to_deliver",
            summary="SEC CNS fails to deliver per settlement date and CUSIP, from the half-month files",
            api_group="",
            api_name="",
            partition_field="",
            first_date=date(
                2018, 8, 1
            ),  # the SEC has files from 2004-02; this matches the short volume store
            key_cols=("settlement_date", "cusip"),
            cdn_family=None,
            entitled=False,
            subdir="halves",
            cadence="semimonthly",
            sources=("sec",),
        ),
    )
}


def spec(name: str) -> DatasetSpec:
    """The spec for ``name``; ``KeyError`` lists the known names."""
    try:
        return DATASETS[name]
    except KeyError:
        raise KeyError(
            f"unknown FINRA dataset {name!r}; known: {', '.join(DATASETS)}"
        ) from None
