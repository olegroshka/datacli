"""Where linkage data lives and the parameters of each dataset.

Root resolution, mirroring the other sources::

    DATACLI_LINKAGE_ROOT env var  >  datacli.toml [linkage].data_root  >  default

where the default is a ``linkage`` sibling of the resolved eodhd data root.

Parameters live in ``[linkage.etf_betas]``, ``[linkage.auction_abnormal]`` and
``[linkage.auction_cospike]``; every field has the documented default and an
unknown key is an error. Each dataset's parameters have a digest that is
stored with what was built, so a change of parameters rebuilds everything.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Mapping, TypeVar

ENV_ROOT = "DATACLI_LINKAGE_ROOT"
SECTION = "linkage"


def resolve_root() -> tuple[Path, str]:
    """The linkage root and where it came from: ``env``, ``config`` or ``default``."""
    explicit = os.environ.get(ENV_ROOT)
    if explicit:
        return Path(explicit).expanduser(), "env"
    import config as eodhd_config  # type: ignore[import-not-found]

    override = eodhd_config.section(SECTION).get("data_root")
    if override:
        return Path(str(override)).expanduser(), "config"
    eodhd_root, _ = eodhd_config.eodhd_data_root()
    return Path(eodhd_root).parent / SECTION, "default"


def linkage_root() -> Path:
    return resolve_root()[0]


def eodhd_root() -> Path:
    import config as eodhd_config  # type: ignore[import-not-found]

    return Path(eodhd_config.eodhd_data_root()[0])


def alpaca_root() -> Path:
    from alpaca.config import alpaca_root as root

    return root()


@dataclass(frozen=True)
class BetaParams:
    """``etf_betas``: trailing OLS betas of daily total returns."""

    start: dt.date = dt.date(2016, 1, 1)  # first month end built
    windows: tuple[int, ...] = (63, 252)  # trading days
    min_obs_share: float = 0.8  # a pair needs this share of the window's days
    market: str = "SPY"  # the market leg netted out of ``*_ex_mkt``
    min_price: float = 5.0  # median close over the window
    min_dollar_volume: float = 1_000_000.0  # median close * volume over the window
    event_window: int = 1  # sessions masked on each side of a split ex-date
    max_abs_log_return: float = 0.5  # larger daily moves are masked (unrecorded events)
    max_dividend_yield: float = 0.25  # a larger dividend / previous close masks the day

    def __post_init__(self) -> None:
        if not self.windows or any(w < 20 for w in self.windows):
            raise ValueError(
                "[linkage.etf_betas].windows must be trading-day counts of at least 20"
            )
        if not 0 < self.min_obs_share <= 1:
            raise ValueError("[linkage.etf_betas].min_obs_share must be in (0, 1]")
        if (
            self.event_window < 0
            or self.max_abs_log_return <= 0
            or self.max_dividend_yield <= 0
        ):
            raise ValueError(
                "[linkage.etf_betas] event_window >= 0, max_abs_log_return > 0, max_dividend_yield > 0"
            )

    def min_obs(self, window: int) -> int:
        return max(2, int(round(window * self.min_obs_share)))


@dataclass(frozen=True)
class AbnormalParams:
    """``auction_abnormal``: auction value against its own trailing median."""

    baseline_window: int = 63  # previous auction days in the median
    baseline_min_obs: int = 40
    sides: tuple[str, ...] = ("close", "open")

    def __post_init__(self) -> None:
        if not 2 <= self.baseline_min_obs <= self.baseline_window:
            raise ValueError(
                "[linkage.auction_abnormal] needs 2 <= baseline_min_obs <= baseline_window"
            )
        if not self.sides or set(self.sides) - {"close", "open"}:
            raise ValueError(
                "[linkage.auction_abnormal].sides is a subset of close, open"
            )


@dataclass(frozen=True)
class CospikeParams:
    """``auction_cospike``: neighbours by co-movement of unusual auction value."""

    start: dt.date = dt.date(2017, 1, 1)  # first month end built
    side: str = "close"
    window: int = 252  # auction days in the trailing window
    min_overlap: int = 150  # days both names have a value
    top_k: int = 20  # neighbours kept per name
    clip: float = 3.0  # residuals are clipped to +-clip (log units) before correlating
    spike_threshold: float = (
        0.6931471805599453  # ln 2: twice the usual value, net of the day
    )
    include_etfs: bool = True  # context ETFs are nodes too

    def __post_init__(self) -> None:
        if self.side not in ("close", "open"):
            raise ValueError("[linkage.auction_cospike].side is close or open")
        if not 2 <= self.min_overlap <= self.window or self.top_k < 1 or self.clip <= 0:
            raise ValueError(
                "[linkage.auction_cospike] needs 2 <= min_overlap <= window, top_k >= 1, clip > 0"
            )


@dataclass(frozen=True)
class Params:
    etf_betas: BetaParams = BetaParams()
    auction_abnormal: AbnormalParams = AbnormalParams()
    auction_cospike: CospikeParams = CospikeParams()


T = TypeVar("T")


def _build(cls: type[T], section: Mapping[str, Any], label: str) -> T:
    known = {f.name: f for f in fields(cls)}  # type: ignore[arg-type]
    unknown = sorted(set(section) - set(known))
    if unknown:
        raise ValueError(f"unknown [{label}] setting(s): {', '.join(unknown)}")
    values: dict[str, Any] = {}
    for name, value in section.items():
        default = known[name].default
        if isinstance(default, dt.date):
            value = (
                value
                if isinstance(value, dt.date)
                else dt.date.fromisoformat(str(value))
            )
        elif isinstance(default, tuple):
            value = tuple(value)
        elif isinstance(default, bool):
            value = bool(value)
        elif isinstance(default, int):
            value = int(value)
        elif isinstance(default, float):
            value = float(value)
        values[name] = value
    return cls(**values)


def params_from(section: Mapping[str, Any]) -> Params:
    """Build :class:`Params` from a ``[linkage]`` TOML section."""
    names = {f.name for f in fields(Params)}
    scalars = {k for k, v in section.items() if not isinstance(v, Mapping)} - {
        "data_root"
    }
    if scalars:
        raise ValueError(f"unknown [linkage] setting(s): {', '.join(sorted(scalars))}")
    tables = {k: v for k, v in section.items() if isinstance(v, Mapping)}
    unknown = sorted(set(tables) - names)
    if unknown:
        raise ValueError(f"unknown [linkage] table(s): {', '.join(unknown)}")
    return Params(
        etf_betas=_build(BetaParams, tables.get("etf_betas", {}), "linkage.etf_betas"),
        auction_abnormal=_build(
            AbnormalParams,
            tables.get("auction_abnormal", {}),
            "linkage.auction_abnormal",
        ),
        auction_cospike=_build(
            CospikeParams, tables.get("auction_cospike", {}), "linkage.auction_cospike"
        ),
    )


def load_params() -> Params:
    import config as eodhd_config  # type: ignore[import-not-found]

    return params_from(eodhd_config.section(SECTION))


def digest(params: Any) -> str:
    """A short stable digest of a parameter dataclass."""
    payload = json.dumps(dataclasses.asdict(params), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
