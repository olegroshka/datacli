"""Register the read-only Alpaca views on a DuckDB connection.

- ``alpaca_auction_prints`` -- every auction print per store ticker and day:
  ``side`` open | close, ``condition`` O / 6 (the crosses), Q / M (official
  reports, which repeat the listing exchange's cross: never sum them), price,
  size (shares as traded), ``ts`` (UTC) and the venue.
- ``alpaca_auctions`` -- one row per ``(ticker, date)``: the opening and the
  closing cross of the listing exchange (the largest cross print), and how
  many venues printed one. A day's auctions are known after its close.
- ``alpaca_auctions_state`` -- the fetch-state sidecar, one row per fetch unit
  (a ticker's main symbol, or a reviewed alias for a predecessor security).
- ``alpaca_auctions_coverage`` -- per ticker: the store's sessions in its
  extent, how many have a closing cross, the sessions before the first one,
  and ``status`` ok | thin. Read this before trusting a ticker's history.

Best-effort and idempotent: a view appears only once its files exist.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alpaca.config import alpaca_root
from alpaca.store import AuctionStore

PRINTS_VIEW = "alpaca_auction_prints"
DAILY_VIEW = "alpaca_auctions"
STATE_VIEW = "alpaca_auctions_state"
COVERAGE_VIEW = "alpaca_auctions_coverage"


def register(con: Any, *, root: Path | None = None) -> dict[str, bool]:
    store = AuctionStore(Path(root) if root is not None else alpaca_root())
    made = {
        PRINTS_VIEW: False,
        DAILY_VIEW: False,
        STATE_VIEW: False,
        COVERAGE_VIEW: False,
    }
    if store.tickers_on_disk():
        glob = (store.prints_dir / "*.parquet").as_posix()
        con.execute(
            f"CREATE OR REPLACE VIEW {PRINTS_VIEW} AS SELECT * FROM read_parquet('{glob}')"
        )
        made[PRINTS_VIEW] = True
    if store.daily_path.exists():
        con.execute(
            f"CREATE OR REPLACE VIEW {DAILY_VIEW} AS SELECT * FROM read_parquet('{store.daily_path.as_posix()}')"
        )
        made[DAILY_VIEW] = True
    if store.state_path.exists():
        con.execute(
            f"CREATE OR REPLACE VIEW {STATE_VIEW} AS SELECT * FROM "
            f"read_csv_auto('{store.state_path.as_posix()}', all_varchar=true)"
        )
        made[STATE_VIEW] = True
    if store.coverage_path.exists():
        con.execute(
            f"CREATE OR REPLACE VIEW {COVERAGE_VIEW} AS SELECT * FROM "
            f"read_csv_auto('{store.coverage_path.as_posix()}')"
        )
        made[COVERAGE_VIEW] = True
    return made


def schema_snippet() -> str:
    return "\n".join(
        [
            "Alpaca views (official auction prints, consolidated tape, from 2016):",
            f"- {DAILY_VIEW}(ticker, date, open_price, open_size, open_exchange, open_ts, open_crosses,",
            "  close_price, close_size, close_exchange, close_ts, close_crosses, n_reports)",
            "  [the listing exchange's opening and closing cross per ticker and day; sizes are shares as",
            "  traded (not split-adjusted); ticker is the eodhd spelling; known after that day's close]",
            f"- {PRINTS_VIEW}(ticker, symbol, date, side, condition, price, size, ts, exchange)",
            "  [every print: condition O/6 = cross, Q/M = official report repeating the cross -- never sum them]",
            f"- {STATE_VIEW}(ticker, symbol, asset_type, asof, span_from, span_to, coverage_from,",
            "  coverage_through, first_data, last_data, n_days, n_prints, status, fetched_at, detail, unit, role)",
            "  [one row per fetch unit: a ticker's main symbol, or a reviewed alias (unit TICKER<SYMBOL@ASOF)]",
            f"- {COVERAGE_VIEW}(ticker, extent_from, extent_to, expected_days, close_days, coverage,",
            "  first_close, late_sessions, status)",
            "  [status thin = the auctions cover less of the ticker's extent than the store prices; check first]",
        ]
    )
