"""Shared logic of the register-driven price lanes (``uk_domestic``, ``fr_domestic``).

A register lane prices a market's domestic common stocks, listed and
delisted, so that the public short register (``registers``, DD-003) can be
priced: the universe is every common stock on the provider's active and
delisted symbol lists for the exchange that is quoted in the market's
currencies, plus any common stock of the exchange whose ISIN the register
names (``in_register``), ordered register names first.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import _atomic
import pandas as pd
import requests
from _datadir import EODHD_RAW_ROOT
from fetch_eodhd_us_fundamentals import _api_get, _get_api_key

COLUMNS = ["Code", "Name", "Country", "Exchange", "Currency", "Type", "Isin"]
KEEP_TYPES = frozenset({"common stock"})


@dataclass(frozen=True)
class LaneSpec:
    name: str  # the datacli lane
    market: str  # the register's market code
    exchange: str  # the provider's exchange code
    currencies: frozenset[str]
    tickers_file: str
    default_from: str = "2012-01-01"
    #: Further exchanges whose lists contribute the register's names only (Frankfurt for the
    #: German register: XETRA lists 270 of its 1,042 ISINs, Frankfurt 574).
    extra_exchanges: tuple[str, ...] = ()

    @property
    def raw_dir(self) -> Path:
        return EODHD_RAW_ROOT / self.name

    @property
    def tickers_path(self) -> Path:
        return self.raw_dir / self.tickers_file

    @property
    def unmatched_path(self) -> Path:
        return self.raw_dir / "register_unmatched.csv"


#: The register lanes served by the generic scripts (``--lane``); the UK and France keep their own.
LANE_SPECS: dict[str, LaneSpec] = {
    "nl_domestic": LaneSpec(name="nl_domestic", market="nl", exchange="AS", currencies=frozenset({"EUR"}), tickers_file="tickers_NL.parquet"),
    "se_domestic": LaneSpec(name="se_domestic", market="se", exchange="ST", currencies=frozenset({"SEK"}), tickers_file="tickers_SE.parquet", default_from="2010-01-01"),
    "no_domestic": LaneSpec(name="no_domestic", market="no", exchange="OL", currencies=frozenset({"NOK"}), tickers_file="tickers_NO.parquet"),
    "ie_domestic": LaneSpec(name="ie_domestic", market="ie", exchange="IR", currencies=frozenset({"EUR"}), tickers_file="tickers_IE.parquet"),
    "de_domestic": LaneSpec(name="de_domestic", market="de", exchange="XETRA", currencies=frozenset({"EUR"}), tickers_file="tickers_DE.parquet", extra_exchanges=("F",)),
}


def take_lane(argv: list[str]) -> str:
    """Remove ``--lane NAME`` from ``argv`` and return the name (a ``LANE_SPECS`` key)."""
    if "--lane" not in argv:
        raise SystemExit("--lane NAME is required (one of " + ", ".join(LANE_SPECS) + ")")
    i = argv.index("--lane")
    try:
        name = argv[i + 1]
    except IndexError:
        raise SystemExit("--lane needs a name") from None
    del argv[i : i + 2]
    if name not in LANE_SPECS:
        raise SystemExit(f"unknown lane {name!r}; expected one of " + ", ".join(LANE_SPECS))
    return name


def select_universe(
    active: Iterable[Mapping[str, Any]],
    delisted: Iterable[Mapping[str, Any]],
    register_isins: set[str],
    *,
    currencies: frozenset[str],
) -> tuple[pd.DataFrame, set[str]]:
    """The lane's rows and the register ISINs no list knows.

    A code in both lists is taken from the active one. Rows of an extra
    exchange (``extra`` is the row's ``Exchange`` not equal to the lane's)
    are kept only for the register's names, and an ISIN already taken from
    an earlier list is not taken again.
    """
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    seen_isins: set[str] = set()
    primary_exchange: str | None = None
    for source, is_delisted in ((active, False), (delisted, True)):
        for raw in source:
            code = str(raw.get("Code", "")).strip()
            exchange = str(raw.get("Exchange", "")).strip()
            if primary_exchange is None:
                primary_exchange = exchange
            if not code or (code, exchange) in seen:
                continue
            if str(raw.get("Type", "")).strip().lower() not in KEEP_TYPES:
                continue
            currency = str(raw.get("Currency", "")).strip().upper()
            isin = str(raw.get("Isin", "") or "").strip().upper()
            in_register = bool(isin) and isin in register_isins
            extra = exchange != primary_exchange
            if (currency not in currencies and not in_register) or (extra and not in_register):
                continue
            if isin and isin in seen_isins:
                continue
            seen.add((code, exchange))
            if isin:
                seen_isins.add(isin)
            row = {column: raw.get(column, "") for column in COLUMNS}
            row.update(delisted=is_delisted, in_register=in_register)
            rows.append(row)
    frame = pd.DataFrame(rows, columns=COLUMNS + ["delisted", "in_register"])
    frame = frame.sort_values(["in_register", "delisted", "Code"], ascending=[False, True, True]).reset_index(drop=True)
    known = set(frame["Isin"].astype(str).str.upper())
    return frame, {i for i in register_isins if i not in known}


def register_isins(con: Any, market: str) -> set[str]:
    """The register's ISINs for ``market``, empty when the register is not stored."""
    try:
        rows = con.execute(f"SELECT DISTINCT isin FROM short_register WHERE market = '{market}' AND isin IS NOT NULL").fetchall()
    except Exception:  # noqa: BLE001 (the view is optional)
        return set()
    return {str(r[0]).strip().upper() for r in rows}


def fetch_symbol_list(session: requests.Session, exchange: str, *, delisted: bool) -> list[dict]:
    data = _api_get(session, f"exchange-symbol-list/{exchange}", {"delisted": "1"} if delisted else None)
    if not data or not isinstance(data, list):
        raise RuntimeError(f"empty {'delisted' if delisted else 'active'} {exchange} symbol list from the provider")
    return [row for row in data if row.get("Code")]


def build_universe(spec: LaneSpec, *, plan: bool, log: logging.Logger) -> None:
    import explore_eodhd

    wanted = register_isins(explore_eodhd.connect(), spec.market)
    log.info("Register ISINs (%s): %d", spec.market, len(wanted))
    session = requests.Session()
    session.params = {"api_token": _get_api_key()}
    active: list[dict] = []
    delisted: list[dict] = []
    for exchange in (spec.exchange, *spec.extra_exchanges):
        for target, flag in ((active, False), (delisted, True)):
            for row in fetch_symbol_list(session, exchange, delisted=flag):
                row = dict(row)
                row["Exchange"] = exchange  # the provider's lists carry the venue inconsistently
                target.append(row)
    log.info("Provider lists: %d active, %d delisted (%s)", len(active), len(delisted), ", ".join((spec.exchange, *spec.extra_exchanges)))
    universe, unmatched = select_universe(active, delisted, wanted, currencies=spec.currencies)
    log.info(
        "Universe %d (%d delisted, %d in the register); register ISINs unmatched %d",
        len(universe), int(universe["delisted"].sum()), int(universe["in_register"].sum()), len(unmatched),
    )
    log.info("By currency: %s", universe["Currency"].value_counts().head(6).to_dict())
    log.info("First fill costs about %d price calls (%d for the register's issuers)", len(universe), int(universe["in_register"].sum()))
    if plan:
        log.info("--plan: nothing written")
        return
    spec.raw_dir.mkdir(parents=True, exist_ok=True)
    _atomic.to_parquet(universe, spec.tickers_path, index=False)
    log.info("Wrote %s", spec.tickers_path)
    _atomic.to_csv(pd.DataFrame({"isin": sorted(unmatched)}), spec.unmatched_path, index=False)
    log.info("Wrote %s (%d ISINs)", spec.unmatched_path, len(unmatched))


def universe_main(spec: LaneSpec, log: logging.Logger, doc: str) -> None:
    parser = argparse.ArgumentParser(description=doc.splitlines()[0])
    parser.add_argument("--plan", action="store_true", help="print the counts and write nothing")
    args = parser.parse_args()
    build_universe(spec, plan=args.plan, log=log)


# --------------------------------------------------------------------------- #
# price targets
# --------------------------------------------------------------------------- #
def take_flag(flag: str) -> bool:
    if flag in sys.argv:
        sys.argv.remove(flag)
        return True
    return False


class TargetLoader:
    """``load_target_tickers`` for a register lane, with the lane's own flags taken off ``sys.argv``."""

    def __init__(self, tickers_path: Path, exchange: str) -> None:
        self.tickers_path = tickers_path
        self.exchange = exchange
        self.include_delisted = False
        self.register_only = False

    def take_flags(self) -> None:
        self.include_delisted = take_flag("--include-delisted") or self.include_delisted
        self.register_only = take_flag("--register-only") or self.register_only

    def __call__(self, *, explicit_specs: list[str], limit: int = 0, **_ignored: Any) -> list[tuple[str, str]]:
        if explicit_specs:
            from fetch_eodhd_eu_fundamentals import parse_ticker_spec

            return [parse_ticker_spec(value) for value in explicit_specs][: limit or None]
        self.take_flags()
        universe = pd.read_parquet(self.tickers_path)
        if not self.include_delisted and "delisted" in universe.columns:
            universe = universe[~universe["delisted"].astype(bool)]
        if self.register_only and "in_register" in universe.columns:
            universe = universe[universe["in_register"].astype(bool)]
        exchanges = universe["Exchange"].astype(str).str.strip() if "Exchange" in universe.columns else pd.Series("", index=universe.index)
        tickers = [
            (str(code).strip(), exchange or self.exchange)
            for code, exchange in zip(universe["Code"], exchanges)
            if str(code).strip()
        ]
        return tickers[: limit or None]


def configure_prices(base: Any, spec: LaneSpec, loader: TargetLoader, log_name: str) -> None:
    """Point the shared ETF price fetcher at the lane's universe and outputs."""
    base.PRICES_PATH = spec.raw_dir / "prices_daily.parquet"
    base.PRICES_STATE_PATH = spec.raw_dir / "prices_fetch_state.csv"
    base.ETF_TICKERS_PATH = spec.tickers_path
    base.load_target_tickers = loader
    base.log = logging.getLogger(log_name)


def prices_main(base: Any, spec: LaneSpec, loader: TargetLoader, log_name: str) -> None:
    configure_prices(base, spec, loader, log_name)
    loader.take_flags()  # before the shared parser rejects them
    if not any(arg == "--from" or arg.startswith("--from=") for arg in sys.argv[1:]):
        sys.argv += ["--from", spec.default_from]
    base.main()


def make_main(spec: LaneSpec, loader: TargetLoader, log_name: str) -> Callable[[], None]:
    def main() -> None:
        import fetch_eodhd_us_etf_prices as base

        prices_main(base, spec, loader, log_name)

    return main
