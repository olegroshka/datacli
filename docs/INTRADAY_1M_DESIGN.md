# Intraday 1m bars: design for the `intraday` lane

Status: design approved by the owner on 2026-11-13 ("go with your
recommendations"; decisions in section 3). Cut 1 is in progress (section 12
tracks it).

## 1. Purpose and scope

Add 1-minute OHLCV bars for a liquid subset of instruments: US stocks, US
context ETFs, indices, FX and ISA-eligible LSE UCITS ETFs. The bars are for
building and testing intraday models whose signals operate at minutes, not
microseconds. A heavy model's plausible edge is breadth (many names, many
weak signals) and cross-asset context, not speed, so the universe favours
breadth plus context over depth of history.

The lane is history for research and backtests. Live bars for trading come
from IB through `blive`, whose bar tape keeps 90 days. Longer history is
datacli's and btest's job.

Out of scope for v1:
- tick data (`/api/ticks`);
- the live WebSocket recorder;
- continental European intraday (EODHD has none);
- coarser stored grains: 5m, 15m and 1h are derived from 1m, never stored.

## 2. Verified facts (probed live on 2026-11-12 and 2026-11-13)

All probes ran with the owner's key and spent about 180 units in total.

**Endpoint and limits**
- `GET /api/intraday/{TICKER}.{EXCH}?interval=1m&from=<unix>&to=<unix>`
  costs 5 units per request, whatever the span.
- The maximum span is 120 days for 1m (beyond that the API returns HTTP 422
  "Max period length is 120 days"), 600 days for 5m and 7200 days for 1h.
- Rows are `timestamp` (unix seconds, bar open, UTC), `gmtoffset` (0),
  `datetime` (UTC string) and `open`, `high`, `low`, `close`, `volume`.

**Prices are unadjusted.** AAPL 1m bars for 2005-03-01 show about $44.

**US**
- 1m history goes back to at least 2004.
- Bars cover 04:00 to 20:00 ET (extended hours): about 960 bars a day for
  liquid names and 212 for a thin one (ABCB). A minute with no trades has no
  bar; there is no zero-volume filler.
- Delisted names have history (TWTR in 2021 worked).

**Indices**
- GSPC.INDX and NDX.INDX return about 1,390 bars a day, around the clock. What
  the overnight bars are is not yet understood.
- VIX.INDX has recent data only (2018 is empty).
- FTSE.INDX and VIX3M.INDX return nothing.

**FX:** 1m bars run 24 hours a day and go back to at least 2010 (GBPUSD.FOREX).

**LSE**
- 1m history starts between 2020-03 and 2021-03; 5m from 2020-11.
- CSPX.LSE and SWDA.LSE work; QQL3.LSE is "Ticker Not Found".

**Continental Europe:** XETRA, F, PA, AS, ST and OL return nothing.

**Volume does not reconcile with daily volume.** The AAPL bar at 21:00 UTC
(16:00 ET, the first post-close minute) holds 16.2M shares on 2026-11-10 and
12.7M on 2026-11-11. Regular-hours bars plus that bar sum to 37.2M against a
daily 29.8M, and 37.8M against 34.5M. Section 11 covers what follows from this.

**From the first real fetches (2026-11-13, AAPL then six T1 names)**
- A full 1m window (118 days of a liquid name) is about 75k bars and takes
  about 8 s, almost all of it server and transfer time.
- Stored bars take about 15 bytes each: AAPL 2016-2026 is 2.15M bars in 37 MB.
- Six workers in parallel gave about 2.75 s per window.
- **Vendor holes exist.** TSLA has no 1m bars from 2023-08 to 2024-04 (two
  empty windows) even though it traded throughout.
- **Publication lag.** At 00:45 UTC on 2026-11-13, the 12 Nov session was not
  published yet: the latest bar was 11 Nov. The lag is more than 3.75 h after
  the regular close.

**Liquidity, from datacli's own daily data** (median close times volume over
the 252 days to 2026-11-12, price at least $5, us_common and us_extended
combined): the 100th name trades about $960M a day, the 150th about $690M, the
200th about $520M and the 300th about $370M. With later forward splits divided
out of the daily volume (section 4), the 2026Q4 ranking gives $928M, $662M,
$518M and $359M.

**EODHD daily volume is split-adjusted; daily close is not** (found
2026-11-13). AAPL 2020-08-28: close 499.23, volume 187.6M, against 46.9M shares
traded before the 4:1 split (the raw 1m bars sum to 38.0M in regular hours).
NVDA 2024-06-05: close 1224.4, volume 528.4M (10x).

## 3. Decisions (owner, 2026-11-13)

| # | Decision | Choice |
|---|---|---|
| D1 | History start | 2016-01-01 (US 1m reaches 2004; deepen later only if a model needs it) |
| D2 | Breadth tier T2 | Included (ranks 101 to 300, training only) |
| D3 | Google Drive | Not recommended in the discussion. Default: the daily job keeps the existing whole-root push, but `bars_1m` is excluded until the Drive quota is checked. The exclude option lands in cut 2, before the job is enabled. |
| D4 | Scheduling | A separate job `eodhd-intraday-1m-daily` at 06:00 London, Tuesday to Saturday (section 9) |
| D5 | Lane shape | One lane `intraday` with one dataset kind `bars_1m`, opt-in like `fundamentals` (not part of the default refresh or `--fast`) |

## 4. Universe

The universe is defined in `eodhd/intraday_universe.toml`, versioned and
reviewed. It is built by `eodhd/build_intraday_universe.py`, which is local and
spends no API units.

| Tier | Rule | Count | Role |
|---|---|---|---|
| `T1` | US common stocks (us_common and us_extended), price at least $5, at least 200 trading days in the trailing 365 calendar days, ranked by median daily dollar volume. A name joins at rank 100 or better and stays until it falls below rank 130. | 100 | trade |
| `T2` | Same ranking, next 200 names. A name joins at rank 300 or better and stays until it falls below rank 390. | 200 | training breadth |
| `context` | Curated US ETFs in groups: broad, sectors, themes, rates and credit, commodities, international, volatility and crypto | 38 | signal only (no UK KID) |
| `index` | GSPC, NDX, VIX | 3 | signal only |
| `fx` | GBPUSD, EURUSD, USDJPY, USDCNH | 4 | signal only |
| `uk` | ISA-eligible LSE UCITS ETFs | 8 | trade |

**Dollar volume without lookahead.** Because daily volume is restated for
later splits (section 2), `close * volume` would rank future splitters first:
the first build put NVDA at rank 1 in 2018 with $89B a day. The ranker divides
each day's volume by the forward splits (ratio above 1) with a later ex-date,
from the lane's own splits file, and only where the history shows it was
re-adjusted for that split (a `close / adjusted_close` step at the ex-date;
the bulk `--fast` top-up does not re-adjust history). Reverse splits are left
alone: for serial reverse-splitters in us_extended the series is already
fully adjusted (SNCA closes at 104 in 2017), so undoing them inflates volume
up to 12,500x (SPLP). On 2026-11-13 all 98 member splits of ratio 1.5 or more
were confirmed. The fix moved about 1,200 of 13,200 stock member-quarters and
added 48 pairs (AON, HLT, HSY, WM, XEL, DLR, ...).

**Point in time.** Stock tiers are re-ranked at every quarter start
(1 Jan, 1 Apr, 1 Jul, 1 Oct) from the trailing year that ends the day before.
Hysteresis compares against the previous quarter's tier.
`universe_membership.parquet` keeps one row per quarter and member, so a
backtest can ask who was a member on any date.

**Survivorship.** The ranking only sees names datacli prices daily. us_extended
adds delisted names, but only from FINRA short interest, which starts in 2018,
and its daily prices start in 2017. Rankings before 2018 can therefore miss
names that later delisted. The manifest must state this.

**Listing floor.** `fetch_from` is also clipped at the pair's first daily price
in any lane (`listed_from` in the membership file; FX has none). A recent
listing therefore costs no empty history windows.

**Fetch span per pair.** Bars are fetched from the first quarter a pair was a
member, minus `warmup_days` (90), clipped at the history start. A current
member is fetched up to today; a former member up to the end of its last member
quarter. Former members drop out of `tickers_INTRADAY.parquet`, so `status`
reports them as retired (kept for history, not refreshed), the same way it
treats delisted names in other lanes.

**ISA tradability.** The `isa_tradable` column is:
- `yes` for the curated UK ETFs;
- `no` for US ETFs (no KID) and for names on the curated ADR list (IBKR says
  an ISA cannot hold ADRs);
- `unverified` for every other US common stock.

The ADR list is hand-kept because datacli's metadata has no ADR flag.

**Bad identities.** `[exclude]` in the toml lists symbols whose daily series
in the stock lanes is not one US security (TNT and MRIN, found 2026-11-13:
one-year price swings of 1,672x and 12,478x, closes in the thousands). They
are dropped before ranking, so they take no rank and displace no real name.

**Deliberately excluded**
- UK single stocks: 0.5% stamp duty on every buy kills intraday turnover.
- Continental Europe: no intraday history.
- Leveraged ETPs: they do not fill on the PMA-bound retail account (blive
  ADR-052).
- Small and mid caps: thin, so stale prices fake predictability.

## 5. Storage and schema

```
<data_root>/intraday/
  tickers_INTRADAY.parquet        # current members: ticker, exchange, tier, role, asset_type,
                                  #   group, adv_rank, adv_musd, isa_tradable, reason, fetch_from
  universe_membership.parquet     # point in time: quarter_start, ticker, exchange, tier, adv_rank, adv_musd
  bars_1m/<TICKER>.<EXCH>_<YYYY>.parquet   # flat ticker-year partitions, zstd
  bars_1m_fetch_state.csv         # one row per (ticker, exchange)
  bars_1m_fetch_audit.csv         # one row per request
```

**Bar rows**, sorted by `ts`:

| Column | Meaning |
|---|---|
| `ticker`, `exchange` | Pair keys |
| `ts` | Bar open, UTC timestamp (milliseconds) |
| `date` | Trading date in the exchange's timezone, as a `YYYY-MM-DD` string like `prices_daily.date`, so US post-market bars past midnight UTC join to the right day |
| `session` | `pre`, `rth`, `post` or `24h` |
| `open`, `high`, `low`, `close` | Unadjusted, as the vendor delivers them |
| `volume` | As the vendor delivers it |

**Session rules**

| Exchange | Timezone | `pre` | `rth` | `post` |
|---|---|---|---|---|
| US and INDX | America/New_York | before 09:30 | 09:30 to 16:00 | from 16:00 |
| LSE | Europe/London | before 08:00 | 08:00 to 16:30 | from 16:30 |

FOREX is UTC and always `24h`. The US 16:00 bar is therefore `post`, and it is
the bar that carries the closing-cross volume.

The year in a partition name is the trading date's year, so a session never
straddles two files. The flat layout keeps `DatasetSpec.output_glob`
(`bars_1m/*.parquet`) working unchanged for the explorer and `status`. The
expected scale is about 5k files of 1 to 6 MB each.

**State columns:** `ticker`, `exchange`, `fetch_from`, `fetch_to`,
`coverage_from`, `coverage_through`, `first_data_date`, `latest_data_date`,
`n_rows`, `fetched_at`, `status`, `last_error`.

The status values:
- `ok`: covered from `fetch_from` to the newest target date;
- `partial`: history still to fill;
- `empty`: queried and never returned data;
- `not_found`: HTTP 404;
- `error`: the last request failed.

**Schema:** a `bars_1m` entry in `eodhd/schema.py`, with `SCHEMA_VERSION` 5.

## 6. Fetcher (`eodhd/fetch_eodhd_intraday.py`)

**Windows.** Requests use windows of at most 118 days (under the 120-day cap),
at 5 units each. Per pair the covered span is the single interval from
`coverage_from` to `coverage_through`, and every request extends it at one end.

Each run, in order:
1. **Top-up:** every pair with coverage and a span still open, from
   `coverage_through` minus 3 days (late corrections) to the target date.
2. **First window** for pairs with no state, newest first.
3. **Backfill windows** across all pairs, newest window end first. A capped run
   therefore keeps every pair current before it fills any history, the same as
   the news crawler.

**Budget.** `--max-units N` stops before a request that would exceed N. The
registry passes `--max-units 25000` for `refresh`. A direct run is uncapped
unless the flag is given.

**Start of history.** Listings are handled by the listing floor (section 4).
Eight consecutive empty windows (about 2.6 years) older than a pair's first bar
end its backfill. This is only a backstop for vendor-side history starts (LSE
about 2021).

The first version ended a backfill after two empty windows. TSLA's vendor hole
showed that was too aggressive, and TSLA was refetched.

**Concurrency and time budget**
- `--workers N` (default 6) fetches different pairs at once, one window per
  pair at a time.
- The plan runs in rounds: stage 1 (top-ups and first windows) completes
  before stage 2 (history), and round k holds each pair's k-th newest history
  window. Every pair is current before any history is filled, and each pair's
  span still grows contiguously.
- `--max-minutes N` stops starting requests after N minutes. The registry
  passes 150 for `refresh`, under the job's 3-hour timeout: a full 25k-unit
  budget (5,000 windows) can take longer than that.

**Errors**
- HTTP 404 marks the pair `not_found`, and it is skipped until `--full-refresh`
  or an explicit `--tickers`.
- Any other failure, after the shared bounded retry, marks it `error` and skips
  the pair for the rest of the run.

**Flags:** `--tickers`, `--to YYYY-MM-DD`, `--limit N`, `--full-refresh`,
`--max-units N`, `--max-minutes N`, `--workers N` and `--dry-run`. `--dry-run` prints windows and units per
phase and spends nothing.

**Writes.** Each response is merged into its year file(s): concatenate, keep
the last copy of a duplicate `ts`, sort, and write atomically through
`_atomic`. State is written after every pair, so an interrupted run resumes.

## 7. CLI surface

Every existing verb works through the registry. There are no new mutating
verbs.

```
eodhd refresh intraday [--datasets bars_1m] [--tickers ...] [--to ...] [--limit N] [--full-refresh] [--no-universe] [--run]
eodhd status intraday | lanes | schema | qc intraday [bars_1m]
eodhd describe AAPL.US | find NVDA | coverage NVDA.US | rows NVDA.US bars_1m
eodhd sql "... FROM bars_1m ..."   # views: bars_1m, bars_1m_state (cut 1); bars_1m_adj, intraday_universe, intraday_membership (cut 2)
```

- **`refresh`:** `bars_1m` is in `KNOWN_KINDS`, not `DEFAULT_KINDS`. It takes
  the same passthrough flags as prices. `--fast` leaves it out and prints a
  note when the lane is named explicitly. A dry-run of a bars_1m step also runs
  the fetcher's own `--dry-run`, so the plan shows units.
- **Catalog:** `reindex` takes bars_1m catalog rows from the state file
  (`n_rows`, `first_data_date`, `latest_data_date`) instead of scanning about
  600M rows.
- **Two new read-only verbs in cut 2:**
  - `bars TICKER --on DATE [--freq 1m|5m|15m|1h] [--session rth|all] [--adjusted]`
    shows one session, resampling from 1m.
  - `universe intraday [--as-of DATE] [--tier ...] [--changes]` shows
    membership.
  
  Both are exploratory, which the scheduler forbids.
- **REPL and MCP:** `datacli.py` (`source eodhd`) and the MCP `sql` tool see
  the new views with no change.

## 8. Quality checks (`qc intraday`, `eodhd/report_intraday_quality.py`)

`qc intraday` is delegated to its own report, like `qc news`. The checks:
- duplicate `ts` per pair;
- OHLC sanity (low at most open and close, which are at most high; prices
  above zero);
- regular-hours bar count per pair and day against the expected 390 for US
  and 510 for LSE, with a low-share threshold;
- intraday gaps longer than 5 minutes inside regular hours for T1 names;
- the last regular-hours close against the unadjusted daily close, where a
  ratio far from 1 means a missed split or a bad bar;
- regular-hours plus closing-bar volume against the daily volume, reported as
  a distribution rather than a failure (section 11);
- state status counts and pairs whose history is still to fill.

## 9. Scheduling

The scheduler hard-codes its lane and dataset lists, so `intraday` and
`bars_1m` are added to `EODHD_LANES` and `EODHD_KINDS` in
`scheduler/commands.py`. A parity test then fails whenever those lists and the
registry drift apart. The scheduled command uses only flags the scheduler
already admits, so no INV-002 capability class changes.

The job, built in cut 2:

| Field | Value |
|---|---|
| id | `eodhd-intraday-1m-daily` |
| steps | `eodhd refresh intraday --datasets bars_1m --run`, then `eodhd reindex`, then `sync push --run` |
| trigger | daily at 06:00 London, Tuesday to Saturday |
| logon | logged-off (S4U), enabled by the owner from an elevated terminal |
| policy | stop on first failure, skip on overlap, timeout 3 hours |

The 05:00 job finishes by about 05:20, judging by its last 8 runs.

The draft was created on 2026-11-13 (`schedule drafts` shows it) with
`--weekly 06:00 --days tuesday..saturday --wake --logged-off --timeout 10800`.
Because it runs logged off (S4U), the owner registers it from an elevated
terminal with `schedule enable eodhd-intraday-1m-daily`, then optionally runs
`schedule test eodhd-intraday-1m-daily`. Until it is enabled, nothing is
registered with Windows.

## 10. Volume and cost

These estimates use about 25 bytes per bar after compression and about 800
bars a day for liquid US names.

| Part | Estimate |
|---|---|
| US stocks, 300 names over 10 years | about 600M bars, about 15 to 20 GB (former members add partial spans) |
| Context ETFs and indices (41) | about 2 GB |
| FX (4) | about 0.4 GB |
| LSE ETFs (8) | about 0.1 GB |
| Backfill | 536 pairs, 12,494 windows, 62,470 units (`--dry-run` on 2026-11-13, before the listing floor; 61,090 after it). At about 2.75 s a window with 6 workers that is roughly 9 hours of fetching, so 3 to 4 capped runs. |
| Routine top-up | about 350 pairs at 5 units each, about 1.8k units a night |

**Actual, first backfill (2026-11-13 01:01 to 04:36 UTC, 10 workers):** 11,536
requests, 57,680 units, 0 failures (two connection resets retried), 424M new
bars, about 76 requests a minute. The lane then held 436M bars in 3,963
pair-year files, 7.7 GB (about 18 bytes a bar). It stopped at its 215-minute
limit with 465 windows of 2016 history left; the next capped run finished them.
After the ranking fix (section 4) two more runs fetched the new members:
about 65,000 units in all, 477M bars, 4,312 files, 8.4 GB, 589 pairs in state.

## 11. Known data issues and open checks

1. **1m volume is not daily volume** (section 2). Volume-based features (VWAP,
   participation) wait until the closing-bar content is understood, for
   example from the tick API's sale-condition codes.
2. **Bars are unadjusted, and the daily splits data was 40 days stale on
   2026-11-12.** The cut 2 `bars_1m_adj` view needs a fresh splits pull for the
   lane's names. The QC close check catches unseen splits.
3. **Overnight GSPC and NDX bars** (checked 2026-11-13): about 570 pre-market
   and 425 post-market bars a day, every close different, prices with 5
   decimals and non-zero volume. They behave like a vendor-computed 24-hour
   quote (futures-implied or similar), not the official index, which is only
   disseminated in regular hours. Use their `rth` bars only, or SPY / QQQ. VIX
   extended-hours bars line up with Cboe's extended session and look genuine.
   Index 1m history starts 2020-03 (VIX 2020-03-20, GSPC / NDX 2020-03-24).
4. **EODHD 1m against IB live bars:** a week-long parity comparison in blive
   before live use.
5. **Survivorship before 2018** (section 4).
6. **Same-day round trips in the ISA cash account** need checking with IBKR.
7. **Publication lag:** more than 3.75 h after the regular close (section 2).
   Check whether the previous session is complete by the 06:00 job; the 3-day
   top-up overlap self-heals a late day either way.
8. **Unprobed symbols** (resolved by the backfill): USDCNH.FOREX and all
   eight UK ETFs return bars; every LSE pair starts 2021-10-12. No pair was
   `not_found`.
9. **Vendor holes** (TSLA 2023-08 to 2024-04): `qc intraday` reports them as
   `holes`, meaning trading-day stretches inside a pair's span where daily
   prices exist but no 1m bars do.
10. **Renamed and delisted symbols** (found by the backfill): 22 former members
    are `empty`. EODHD files 1m history under the symbol in use at the time
    (BHGE.US has 2018 bars, BKR.US does not), while the daily lanes and so the
    ranking use the current symbol. Renamed names (BKR, CTRA, LUMN, VTRS, GAP,
    BALL, CPRI, DOC, ET, COR, PSKY, DXC) could be recovered with a hand-kept
    alias map in `intraday_universe.toml` (old symbol and date range per
    member); delisted ones (AET, ESRX, TWX, TNT, DWDP, RDS-A, VIAC, SGI, PPLI,
    MRIN) returned nothing. Together they are 204 of about 13,200 stock
    member-quarters (1.6%), 13 of them T1 (PPLI, TNT; counted before the
    ranking fix). Not built yet; the membership file still marks them as
    members, so a backtest sees missing data, not a survivor-only universe.
11. **Wrong security under a symbol** (close check): FI.US 1m bars before
    2021-10 are Frank's International (about 1.5% of Fiserv's daily close),
    MTCH.US before 2020-07 the old Match Group. The bars are the vendor's
    truth for the symbol; a per-pair validity mask from the close check is the
    fix, built on the btest side or as a later `bars_1m_valid` view.
12. **Spin-off-adjusted daily closes** (HON, SONY, AIV, CHTR, CB, FDX): long
    runs 1-11% off. The daily lanes adjust for spin-offs; the 1m bars are raw.
13. **Index quotes degrade from 2024:** bad GSPC prints on 48 days
    (2024-11 .. 2025-04; 2025-04-25 trades at about 49) and weekend dates with
    overnight bars only. A further reason to prefer SPY / QQQ.
14. **Half days:** `rth` is 09:30-16:00 ET every day; on a 13:00 close the
    later bars are after-hours. A market calendar in `bars_from_payload` would
    fix the tag.

## 12. Cuts and progress

**Cut 1** (done 2026-11-13)
- this document;
- `intraday_universe.toml` and `build_intraday_universe.py`;
- `fetch_eodhd_intraday.py` (tested against a fake session);
- registry and schema entries;
- the CLI kinds, passthrough, `--fast` note and dry-run cost;
- the scheduler lists and parity test;
- catalog rows from state;
- status retirement for bars_1m;
- `report_intraday_quality.py` behind `qc intraday`.

No real backfill.

**Cut 2** (done 2026-11-13; job awaits the owner's `schedule enable`)
- the `bars` and `universe` verbs and the `bars_1m_adj`, `intraday_universe`
  and `intraday_membership` views;
- the Drive exclude option;
- a splits refresh for the lane's names;
- the manifest `EODHD_INTRADAY_DATA_MANIFEST.md` and the README lane map;
- the job draft (`schedule test` is the owner's, after enabling);
- the first backfill, with the owner's go;
- found by the backfill: the `holes` QC check, the `[exclude]` list for
  symbols whose daily series mixes securities (TNT, MRIN; section 4), the
  renamed / delisted findings (open check 10), the split-adjusted daily
  volume and the ranking fix (section 4), per-exchange short days and an OHLC
  tolerance in `qc intraday`.

**Next (not started)**
- a symbol-alias map for renamed members (open check 10);
- a per-pair-day validity mask from the close check (open check 11);
- a market calendar for half-day sessions (open check 14);
- the Drive decision (D3), after a quota check;
- the closing-bar volume question (open check 1), using the tick API;
- the blive parity week (open check 4).

## 13. Out of scope

Tick data, the WebSocket recorder, the btest 1m loader (btest side), the blive
parity harness (blive side), and model work.
