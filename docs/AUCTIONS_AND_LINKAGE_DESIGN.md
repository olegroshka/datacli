# Auctions and linkage: design for the `alpaca` and `linkage` sources

Status: built 2026-11-22 on the owner's instruction ("go with your
recommendations", same day). The recommendations taken, as assumptions the
owner can revisit, are listed in section 3.

## 1. Purpose

datacli has no point-in-time ETF holdings and no index membership history:
the ETF lane holds prices, dividends and splits only, and sector is a
present-day snapshot. A present-day list applied to the past is a leak. The
S7 research session (intraday-research, `prereg/S7_auction_liquidity_graph.md`
item 11.4) therefore asked for views that are point in time by construction:

1. each stock's **trailing betas to the 38 context ETFs** of the intraday lane;
2. the names whose **auction volume spikes on the same days**.

The second needs the official auction prints, which also answer S7 item 11.3
(a second source for auction volume). They come from Alpaca's historical
auctions endpoint, under the market data plan the owner subscribes to.

Out of scope, and a separate request to the owner if index-flow days turn out
to matter: index change history, SEC N-PORT filings, any ETF holdings source.

## 2. Verified facts

**Probed 2026-11-22** (a Sunday; about ten requests, the owner's key):

- `GET https://data.alpaca.markets/v2/stocks/auctions?symbols=..&start=..&end=..&feed=sip`
  returns `{"auctions": {SYMBOL: [{"d", "o": [print] | null, "c": [print] | null}]},
  "next_page_token"}`; a print is `{"c": condition, "p": price, "s": size, "t": RFC-3339 with
  nanoseconds, "x": venue}`. `limit` counts day records across the request's symbols.
- Rate limit header: 10,000 requests a minute.
- AAPL 2016-01-04 has an opening (`O`, venue T) and a closing (`6`) cross:
  history from 2016 is real.
- A late official-close report (`M`) can carry the next day's 00:00 UTC
  timestamp and come back as a record for the day before the requested range
  (BRK.B 2026-11-17 in a request starting 2026-11-18). The parser drops days
  outside the requested range; the top-up re-reads the last three days, so
  the report is captured on the next run.
- Symbols are mapped to a company **as of** a date (`asof`, default today). A
  current symbol returns the company's whole history, old names included
  (MRSH holds MMC's 2024 auctions, GAP holds GPS's, BKR holds BHGE's, META
  holds FB's). A retired symbol returns nothing by default, and its own
  history with `asof` set to a day it was in use (MMC with `asof=2025-12-31`,
  GPS with `asof=2024-01-02`, BHGE with `asof=2019-06-03`).
- Share classes are spelt with a dot (BRK.B).
- Alpaca keys auctions by **security**, not by company. Where the store's
  daily series of a ticker runs through a predecessor security, the
  predecessor's auctions sit under its own symbol (MYL for VTRS, LB for
  BBWI, RDS.A for SHEL, PX for LIN, CBS / VIAC / PARA for PSKY), or under
  the same symbol at an earlier `asof` (VMW before 2023-10-30, TEVA before
  2026-09-14, FTI before 2017-01-17). Found 2026-11-22 after the S7 session
  reported VMW and TEVA nearly empty though `ok`: 48 of 674 tickers had a
  closing cross on fewer than 95% of the store's sessions. Each fix was
  verified by matching the closing cross to the store's daily close (0.2%) in
  a week near each end of the segment.

**From the S7 session** (same day, 2016 to 2023, 591 tickers, 4.3 million
prints in about two and a half minutes):

- `O` and `6` are the crosses. Several venues print one on the same day; the
  largest is the listing exchange's. `Q` and `M` are the official reports; the
  listing exchange's repeats its cross, they appear in bulk from 2021, and
  they must never be summed with the crosses.
- Twelve renamed tickers return nothing under the store's symbol without
  `asof` (MMC, BBWI, PSKY, VTRS, GPS, CHRD, WFRD, WLTW, BHGE, HCN, and SHEL
  and CTRA in part).
- On quarterly expiry days the median closing auction is 24.5% of the day's
  volume, against 8.4% on other days.

The S7 session also measured how far the eodhd lane's closing cross estimate
and the eodhd daily open are from the official crosses. `alpaca qc
--crosscheck` measures the same on our store; the results are in section 9.

## 3. Decisions and recorded assumptions

| # | Decision | Why |
|---|---|---|
| A1 | Alpaca is a datacli source (`alpaca/`), keys from the environment only | the auctions are a vendor dataset like any other; S7 reads datacli read-only |
| A2 | Universe: every US stock and ETF ever in the intraday lane (`universe_membership.parquet`), each over its own daily-price span from 2016 | lines up with the names that have minute bars (S7's universe); about 670 tickers |
| A3 | A retired symbol (no daily price for 10 days) is asked for `asof` its last daily price, and its span stops there | the only way to get its history; it never duplicates its successor's later prints |
| A4 | Store every print, and one chosen row per ticker and day | the choice (largest cross) is a rule that could be revisited; raw prints keep that possible |
| A5 | No request starts in 15:30-16:15 New York on weekdays; at most 600 requests a minute | the key is shared with the paper book, which fetches its bars at 15:45 |
| A6 | Betas for every US stock with daily prices (us_common and us_extended), screened per snapshot by median close >= $5 and median dollar volume >= $1M over the window | the context nodes of S7 section 8; the screen is point in time |
| A7 | Month-end snapshots; windows 63 and 252 sessions; a pair needs 80% of the window | betas move slowly; month ends keep the store small (130 files) |
| A8 | Two kinds of beta: plain, and net of SPY on both legs | plain betas to a sector ETF are mostly market beta; the net one is the nearest stand-in for membership |
| A9 | Returns drop what cannot be trusted rather than repair it: split ex-dates +-1 session, moves above 0.5 in log terms, dividends above 25% of the close, sessions after a gap | the vendor's close is raw for most names and adjusted for some; adjusted_close is not re-adjusted by the bulk top-up; betas do not need every day |
| A10 | Unusual auction value is in dollars (price x size), against the name's own median of the previous 63 auction days, then net of that day's median over members of the same asset type | dollars make a split invisible; the daily netting removes expiry, rebalance and month-end days, which lift every name at once |
| A11 | Co-spike neighbours: correlation of the netted closing-auction residual over the trailing 252 auction days (clipped at +-3), top 20 per member, with joint spike counts (residual above ln 2) and their lift over independence | correlation uses every day and needs no threshold; the spike counts keep the "same days" reading |
| A12 | A partition is rewritten when its content changes, whatever the reason (vendor restatement, a parameter, an upstream change); nothing keeps vintages | the inputs are vendor history, not vintages; each partition is what today's inputs give for its own window, never more |
| A13 | Predecessor securities are reviewed `[[alias]]` entries in the versioned `alpaca/auctions_universe.toml` (symbol, `asof`, date range), and every fetch re-checks an alias against the store's closes (at least 90% of days within 0.2%) before storing it; pre-listing stretches are `[[listed]]` | the same pattern as the intraday lane's `[[alias]]`; a discovery heuristic in the nightly fetch could map the wrong security, a verified list cannot |
| A14 | A fetch status says what was requested; a separate per-ticker coverage (`ok` with a closing cross on at least 80% of the store's sessions and no run of more than 20 sessions without one, anywhere in the extent; else `thin`) says what the data holds. Reviewed stretches with no auctions (OTC between a delisting and a relisting) are `[[gap]]` entries and are left out | `ok` must not hide a ticker that holds 1% of its history (VMW, TEVA); a start-only check missed CTRA's 167-session hole in 2021 |

## 4. The `alpaca` source

```
<alpaca_root>/auctions/
  prints/<TICKER>.parquet        every print of one store ticker (all its units)
  auctions_daily.parquet         one row per (ticker, date): the chosen crosses
  auctions_fetch_state.csv       one row per fetch unit
  auctions_coverage.csv          one row per ticker: does the data cover its extent
  auctions_fetch_audit.csv       one row per request batch
```

**Extents and units.** A ticker's *extent* runs from its first daily price
(not before 2016, nor before a `[[listed]]` date) to its last, or to the
target date if it is current. It is fetched as *units*: each reviewed
`[[alias]]` of `alpaca/auctions_universe.toml` takes its date range (another
symbol, or the same symbol at an earlier `asof`), and the main symbol takes
the one interval left. A main unit's state key is the ticker; an alias's is
`TICKER<SYMBOL@ASOF` (`PSKY<CBS@2019-12-04`). All units of a ticker store
their prints under the ticker; `symbol` in the prints says which security
they came from.

`<alpaca_root>` is `DATACLI_ALPACA_ROOT`, else `[alpaca].data_root`, else an
`alpaca` sibling of the eodhd root.

**Prints** (`alpaca_auction_prints`): `ticker` (eodhd spelling), `symbol`
(Alpaca spelling asked for), `date`, `side` (open | close), `condition`,
`price`, `size` (shares as traded, not split-adjusted), `ts` (UTC, ns),
`exchange`.

**Daily** (`alpaca_auctions`): `open_price`, `open_size`, `open_exchange`,
`open_ts`, `open_crosses` (venues that printed an opening cross), the same
five for the close, and `n_reports`. The chosen cross is the largest print
with the side's cross code; ties go to the earliest. A side with no cross is
null.

**State** (`alpaca_auctions_state`, one row per unit): `symbol`,
`asset_type`, `asof`, `span_from` / `span_to` (the unit's range),
`coverage_from` / `coverage_through` (what has been read), `first_data` /
`last_data`, `n_days`, `n_prints` (inside the unit's range), `status` (ok |
partial | empty | error: about the request only), `fetched_at`, `detail`,
`unit`, `role` (main | alias).

**Coverage** (`alpaca_auctions_coverage`, one row per ticker):
`extent_from` / `extent_to`, `expected_days` (the store's trading days for
the ticker inside its extent), `close_days` (those with a closing cross),
`coverage`, `first_close`, `late_sessions` (store sessions before the first
closing cross), `longest_gap` with `gap_from` / `gap_to` (the longest run of
store sessions without one, anywhere), `reviewed_gap_sessions` (left out by
a `[[gap]]`) and `status`: `ok` when coverage is at least
`[alpaca].min_coverage` (0.8) and `longest_gap` at most `max_gap_sessions`
(20), else `thin`. Rewritten on every run, since the store's daily prices
change too. `alpaca qc` warns on every thin ticker.

**Planning.** Requested coverage is one interval per unit. A unit with
nothing stored, a new Alpaca symbol or a new `asof` gets a `first` task over
its whole range (the range's old prints are replaced); a range past the
coverage gets a `topup` from three days before the coverage end; a range
before it gets a `backfill`. Tasks with the same range and `asof` share a
request of up to 100 symbols; two tasks with the same Alpaca symbol never
share one. After the requests, a touched ticker's prints outside all its
current units (an older segmentation) are pruned. The newest session asked
for is today in New York after 20:30, else the last weekday before.

**Failure policy.** A rejected key stops the run. Any other failed request
marks its units `error` and the run carries on. An alias whose closing
crosses match the store's closes on fewer than `alias_min_match` (90%) of the
compared days, within `alias_tolerance` (0.2%), is refused: nothing is
stored, the unit is `error` with the counts, and the run exits 1. A quiet window stops the
run before the next request; what was fetched is stored. The daily file is
rebuilt from every prints file at the end of a run that stored anything.

**Code.** `auth` (key pair, masked), `client` (paged GETs, even rate
limiting, error mapping), `auctions` (pure parsing and selection), `universe`
(`SymbolUniverse` protocol, the overrides, pure segmentation,
`IntradayUniverse`), `plan` (pure), `coverage` (pure assessment and the alias
check), `quiet`, `store`, `store_closes` (the store's closes, `StoreCloses`),
`refresh` (orchestration against those interfaces), `qc`, `views`, `cli`. Daily spans come from `eodhd/daily_store.py`, a read-only adapter over
the US daily lanes shared with `linkage`.

## 5. The `linkage` source

```
<linkage_root>/<dataset>/<key>.parquet     one file per partition
<linkage_root>/<dataset>/manifest.json     parameters digest; per partition: content digest, rows, built_at
```

### etf_betas (key: month-end session)

One row per `(as_of, ticker, etf, window)`: `etf_group`, `n_obs`, `beta`,
`corr`, `beta_ex_mkt`, `corr_ex_mkt`. Daily total return
`(close_t + dividend_t) / close_{t-1} - 1` between consecutive SPY sessions,
with the masks of A9. Over the window ending at `as_of` (inclusive): `beta`
and `corr` of the stock on the ETF; `*_ex_mkt` the same after each leg is
taken net of its own OLS fit on SPY (NULL for SPY). Pairwise-complete: a pair
uses the sessions where both have a return. Usable from the session after
`as_of`.

### auction_abnormal (key: year)

One row per `(date, ticker, side)`: `asset_type`, `is_member` (a member of
the intraday lane in that quarter), `value_usd`, `log_value`, `baseline`
(median log value of the previous 63 auction days, at least 40), `abnormal`,
`market_abnormal` (that day's median over members of the same asset type),
`resid`. Known after that day's close.

### auction_cospike (key: month-end auction day)

One row per `(as_of, ticker, neighbor)`: `rank`, `corr`, `n_overlap`,
`co_spikes`, `expected_co_spikes`, `lift`, `asset_type`,
`neighbor_asset_type`. Nodes are the members at `as_of` (stocks and context
ETFs). The lists are directed. Usable from the session after `as_of`.

### Build

`linkage build` computes every partition from local data (no network) and
classifies each against the manifest as new, restated or unchanged; `--run`
writes the changed ones and drops partitions the inputs no longer support;
`--full` rewrites all. Order: etf_betas, auction_abnormal, auction_cospike
(built together, cospike takes the fresh panel; alone, it reads the stored
one). A full etf_betas plan takes about 50 s (8,700 screened stocks, 3,000
sessions, 130 month ends).

**Code.** `sources` (the `PriceSource`, `UniverseSource` and `AuctionSource`
protocols and their adapters), pure `calendar`, `moments` (pairwise-complete
second moments as matrix products), `returns`, `betas`, `abnormal`,
`cospike`; `store`; `datasets` (each dataset's `plan()` and the shared
`apply()`); `qc`, `views`, `cli`.

## 6. Point in time

- Every value dated `t` uses data dated on or before `t`; tests delete the
  data after `t` and check the value at `t` is unchanged (betas, abnormal,
  cospike).
- Membership is the intraday lane's, which ranks each quarter on the trailing
  year before it.
- The beta screen (price, dollar volume, observations) is applied per
  snapshot over its own window.
- Not vintages (A12): a vendor correction rewrites the partitions it touches.
- The daily `volume` used by the screen is restated for later splits by the
  vendor, so a future forward splitter looks more liquid and a future reverse
  splitter less. It only moves names across a floor; it never enters a beta.

### Reading under a fence (a holdout)

Every dataset here includes the most recent year, so a project that keeps a
holdout must cut it on the right column. Each value is dated by the last
input it uses:

| Dataset | Cut on | Keep rows with | Files to open |
|---|---|---|---|
| `alpaca_auctions`, `alpaca_auction_prints` | `date` | `date < fence` | prints files hold all years: filter rows |
| `auction_abnormal` | `date` | `date < fence` (the baseline uses only earlier days) | `<year>.parquet`: skip later years, filter the fence year |
| `etf_betas` | `as_of` | `as_of < fence` (the window ends at `as_of`) | `<as_of>.parquet`: never open one dated on or after the fence |
| `auction_cospike` | `as_of` | `as_of < fence` | `<as_of>.parquet`, as above |
| `alpaca_auctions_coverage`, `*_state`, manifests | none | not dated by data; they describe the whole store, holdout included, so do not use them to choose names | |

The loader belongs to the reading project (S7 keeps its fence in its own
`src/ir`); datacli only promises that these columns are the last input date.

## 7. Surfaces

```powershell
uv run python -m alpaca.cli fetch                 # plan: no key needed, nothing sent
uv run python -m alpaca.cli fetch --run           # top up, then backfill
uv run python -m alpaca.cli fetch --tickers AAPL,BRK-B --run
uv run python -m alpaca.cli status [--json]
uv run python -m alpaca.cli qc [--crosscheck]
uv run python -m linkage.cli build [--dataset etf_betas|auction_abnormal|auction_cospike] [--full] [--run]
uv run python -m linkage.cli status [--json]
uv run python -m linkage.cli qc [--dataset NAME]
```

Shell: `source alpaca` / `source linkage`. Views in `sql`, the lab and MCP:
`alpaca_auctions`, `alpaca_auction_prints`, `alpaca_auctions_state`,
`etf_betas`, `auction_abnormal`, `auction_cospike`.

Scheduler: `alpaca fetch --run` and `linkage build --run` are admitted
commands with the same resource locks as the direct CLIs (alpaca: its root
exclusive, the eodhd root shared; linkage: its root exclusive, the eodhd and
alpaca roots shared). The readiness preflight reports a missing key pair as
`alpaca_key_missing`. **Nightly steps** (owner, 2026-11-22): `alpaca fetch
--run` and `linkage build --run` are steps 4 and 5 of the 05:00 London daily
job `eodhd-all-sync-20260820-r3`, after `eodhd refresh --fast`, `reindex` and
`sync push`. At 05:00 London (00:00 New York) the previous session's auctions
are final (after 20:30 New York) and the morning's daily prices are in. They
come after the Drive push because the job stops at the first failed step: an
Alpaca failure must not skip the backup. The draft
`eodhd-all-sync-20260820-r3-edit-c0c1053a` became generation 6 on
2026-11-22 (21:27 London), enabled by the owner from an elevated terminal:
the job runs logged off (S4U), and Windows only registers that elevated. An
enable from a normal terminal would commit the generation, fail to install,
and leave the job in `stale_dispatch` until an elevated `schedule reconcile`.

Not a sync unit yet: the auctions can be fetched again from Alpaca and the
linkage datasets rebuilt, so neither root is pushed to Drive until the owner
adds it.

## 8. Open items

- The `thin` tickers of section 9, if a verified source turns up; the five
  with gaps inside member quarters first.
- Decide whether the alpaca and linkage roots become sync units.
- Opening auction share (cross over the day's volume) and closing auction
  share need daily volume in the auction's own share basis; neither is built.
- Whether betas from intraday returns (5-minute, the 393 names with bars)
  would sharpen the 63-day window.

## 9. Measured on our store

**First backfill** (2026-11-22, 20:18 to 20:23 London, a Sunday): 674
tickers (86 retired, asked `asof` their last daily price), 175 requests,
1,184 pages, 1,587,912 ticker-days and 7,908,570 prints from 2016-01-04 to
2026-11-20; every ticker `ok`, no failed request. A page holds about 1,000
to 1,900 day records whatever `limit` asks for, so the dry-run estimate
assumes 1,000. The auctions take 168 MB.

**Predecessor securities** (same day, 21:43 London): 20 reviewed aliases
for 17 tickers, all passing the close check (22 pages, 18,132 ticker-days,
59,180 prints). A second pass the same evening, on the S7 session's list
of thirteen, found WFRD's old Weatherford (WFT, which the store's series
holds before 2019) and CTRA's 2021 hole under COG, and the gap rule found
CTL's tail after the rename to LUMN: 22 aliases for 19 tickers in all, plus
reviewed OTC stretches for WFRD (2019-05 to 2021-06) and CHRD (2020-10 to
2020-11). Every name on that list (VMW, TEVA, SHEL, BBWI, PSKY, LIN, VTRS,
CTRA, CHRD, APTV, BKR, WFRD, FTI) is now `ok` at 99.8% or more, with no run
of more than four sessions missing. VMW went from 1% of its sessions with a closing cross to
99.9%, TEVA from 2% to 99.9%, PSKY from 11% to 99.9%, VIAC 41% to 99.9%,
WBD 42% to 99.9%; with the nine `[[listed]]` names, 661 tickers are `ok`.
The derived datasets restated accordingly (auction_abnormal all 11 years,
auction_cospike 105 of 118 month ends; etf_betas unchanged).

With the longest-gap rule, 23 tickers stay `thin` (651 `ok`). Only five
have their gap inside a quarter in which they are a member of the intraday
lane, which is what a reader of member names sees:

| Ticker | Longest gap | Where | Member quarters in it | Why |
|---|---|---|---|---|
| VXX | 201 | 2018-01 to 2018-11 | 4 | the store's 2018 series is not the line Alpaca holds (1 of 6 days match) |
| XLRE | 127 | 2016-01 to 2016-07 | 3 | Alpaca has no closing crosses for its first months |
| HCN | 522 | 2018-02 to 2020-10 | 2 | the store's HCN series runs on after the rename to WELL and matches no security |
| VRX | 31 | 2018-07 to 2019-06 | 2 | the store's VRX series after the rename to BHC is a few scattered days |
| UAA | 66 | 2016-01 to 2016-04 | 1 | the store's closes before the 2016 share-class dividend are adjusted for it |

The other 17 (HWM, MARA, SMCI, DWDP, DKNG, OSTK, ASTS, HIMS, FLT, COG,
FTCH, GPS, CIFR, OKLO, RGTI, VRT, IAC, WULF) have their gap outside every
member quarter: spin-off-adjusted store closes (HWM, DWDP), OTC stretches
(SMCI 2018-2020, OSTK, FTCH), SPAC weeks (DKNG, ASTS, HIMS, CIFR, RGTI, VRT,
OKLO), an old symbol's tail after a rename (COG, FLT, GPS) and MARA, whose
earlier entity matches on 4 of 5 days only. `alpaca_auctions_coverage`
lists all of them with `gap_from` / `gap_to`.

**`alpaca qc --crosscheck`** (official crosses against eodhd):

| Comparison | n | Median | p10 | p90 | Within 5% |
|---|---|---|---|---|---|
| `cross_est` / closing cross, from 2023-06-22 | 338,129 | 1.000 | 0.994 | 1.000 | 95% |
| `cross_est` / closing cross, before | 748,166 | 1.087 | 0.948 | 1.875 | 29% |

The vendor's daily open differs from the opening cross by more than half a
cent on 27% of name-days: stocks a median 14.7 bp (p90 64 bp) when they
differ, the context ETFs 2.6 bp (p90 12 bp); the share rises to 39% (stocks)
and 46% (ETFs) in 2026. Both agree with the S7 session's independent
measurement (old-regime median 1.09, p90 1.81, 29% within 5%; new regime 94%;
open 28%). Recorded in `eodhd/EODHD_INTRADAY_DATA_MANIFEST.md`,
`docs/INTRADAY_1M_DESIGN.md` section 11 and the US stock and ETF manifests.

**First linkage build** (about 70 s, 860 MB): etf_betas 130 month ends
(2016-01-29 to 2026-10-30, about 3,300 eligible stocks a month from 8,682
screened in, 29.7M rows, 642 MB); auction_abnormal 11 years (3.17M rows);
auction_cospike 118 month ends (2017-01-31 to 2026-10-30, 897k rows). A
second build found every partition unchanged. Masked stock-days: 7,331
around split ex-dates, 11,834 jumps, 52 dividends, 39,437 after gaps.

Sanity reads of the last snapshot: XOM's nearest auction neighbours are CVX,
COP, EOG, KMI and VLO; NVDA's AAPL, AMD, MSFT, LRCX and AVGO; GLD's GDX and
SLV; SPY's QQQ and IWM; HD's MCD, AXP, PG and BA (fellow Dow members, an
index-flow link). JPM's top net-of-market ETF beta is XLF, XOM's XLE then XOP
and USO, NVDA's XLK then SMH. The members' median closing-auction
`abnormal` is 1.57 (log units, about 4.8 times usual) on quarterly expiry
Fridays, 0.09 on monthly ones and -0.02 on other days.
