# EODHD Intraday (1m bars) Data Manifest

**Status:** FACTUAL-INVENTORY / BACKFILL COMPLETE, ALIASES AND MASKS APPLIED, VOLUME FIXED, ON DRIVE (2026-11-13)
**Created:** 2026-11-13
**Design:** `docs/INTRADAY_1M_DESIGN.md` (decisions, universe rules, fetcher)
**Lane / kind:** `intraday` / `bars_1m` (opt-in; not in the default refresh or `--fast`)

**Live counts:** the numbers below were observed on the date shown and are not
auto-updated. For current counts run `uv run python eodhd/cli.py status intraday`
or `uv run python eodhd/cli.py qc intraday --all`.

## 1. Scope

1-minute OHLCV bars from EODHD `/api/intraday` (`interval=1m`) for a liquid
subuniverse chosen for short-horizon models:

| Group | Members now | Rule / list | Role |
|---|---|---|---|
| T1 | 120 | top US common stocks by median daily dollar volume (join rank <= 100, stay <= 130; counts float), price >= $5 | trade |
| T2 | 235 | below T1 (join <= 300, stay <= 390) | training breadth |
| context | 38 | curated US ETFs (broad, sectors, themes, rates/credit, commodities, international, vol/crypto) | signal only (no UK KID) |
| index | 3 | GSPC, NDX, VIX | signal only |
| fx | 4 | GBPUSD, EURUSD, USDJPY, USDCNH | signal only |
| uk | 8 | ISA-eligible LSE UCITS ETFs (CSPX, SWDA, VUSA, EQQQ, IUIT, CNX1, SGLN, IGLT) | trade |

Stock tiers are re-ranked at each quarter start from the trailing year, so the
lane also holds former members (kept for history, not refreshed): 693 pairs over
44 quarters (2016Q1 .. 2026Q4), 408 current members (549 and 353 before the
buffer-rule fix, caveat 18). A company filed under
several symbols ranks once (caveat 17). Before that fix the fetch state held 599 pairs plus 13
alias rows (caveat 5): the extra pairs were members under earlier rankings
(caveats 10 and 17) and keep their bars as retired pairs.

## 2. Local paths (under `<data-root>/intraday/`)

- `intraday_universe.toml` (repo, `eodhd/`): the rules and curated lists
- `tickers_INTRADAY.parquet`: current members (`tier`, `group`, `role`, `isa_tradable`, `adv_rank`, `fetch_from`)
- `universe_membership.parquet`: one row per quarter and member (point in time), `listed_from`
- `bars_1m/<TICKER>.<EXCH>_<YYYY>.parquet`: bars, one file per pair-year
- `bars_1m_fetch_state.csv`: per pair `coverage_from` / `coverage_through`, status;
  alias rows carry the requested old symbol in `source`
- `bars_1m_fetch_audit.csv`: one row per request (`requested_at`, units, rows,
  outcome, `source` for alias requests)
- `bars_1m_volume_days.parquet`: per US pair-day, the volume fix's regime,
  vendor and daily volume, split factor, excess, `cross_est` (closing cross),
  `n_reports`, `residual`, `status` (caveat 2)
- `bars_1m_volume_fix.parquet`: per corrected bar, `delta` (raw shares) and
  `synthetic` / `price` for close bars the vendor lacks

Bar columns: `ticker`, `exchange`, `ts` (bar open, UTC), `date`
(exchange-local trading date), `session` (`pre` / `rth` / `post`, `24h` for FX),
`open`, `high`, `low`, `close`, `volume`.

DuckDB views (after `reindex`): `bars_1m`, `bars_1m_state`, `bars_1m_adj`
(split-adjusted from `splits`, `split_adj` is the factor; for US bars `volume`
is consolidated, `volume_vendor` the delivered number, `synthetic` a close bar
the vendor lacks), `intraday_universe`, `intraday_membership`.

## 3. Observed state (2026-11-13, after the alias and mask run)

From `status intraday` and `qc intraday --all` after the last run:

| Measure | Value |
|---|---|
| bars | 487,970,529 in 4,432 pair-year files, 8.5 GB (about 18 bytes a bar); the state's `n_rows` sum to the same number |
| state rows | 612: 599 pairs and 13 alias rows; 587 `ok`, 25 `empty` (an `empty` own row can sit beside an alias row with bars, e.g. BKR / BHGE), 0 partial |
| stock member-quarters without bars | 213 of 13,200 (1.6%), counted strictly (bars must overlap the quarter): caveat 5 |
| span | 2015-12 (warmup) .. 2026-11-12 |
| duplicates | 0 |
| OHLC failures | 1 bar (0.1% tolerance) |
| close check (>2% off daily close) | 7,002 pair-days; worst GSPC.INDX 2025-04-25 (ratio 0.009). Groups: spin-off-adjusted daily closes (HON, SONY, AIV, CHTR, CB, FDX, and RTX before 2020-04 via its UTX alias), bad index prints, VIX's 16:15 close |
| holes (5+ missing trading days) | 9 pairs; worst LUMN.US 683 days (2018-01 .. 2020-09, when CTL was the member: not missing data); TSLA, NVDA vendor holes |
| thin sessions | 15 pairs below 80% of the expected regular-hours bars (UHAL, IGLT and the other LSE ETFs: thin markets, not errors) |
| T1 gaps over 5 min | 15 pairs, most MSTR (thin before 2020) |
| short days excluded | US 22 (the 21 early closes of the calendar, caveat 9, plus one), INDX 186 (degraded index quotes from 2024), LSE none (too thin to judge) |
| vendor volume ratio (rth + closing bar) / daily | US p10 0.79 / p50 0.93 / p90 1.12; LSE p50 2.4; INDX meaningless |
| volume fix | 992,222 of 1,009,996 US pair-days (98.2%) consolidated; vendor volume kept on 13,060 `unreconciled`, 4,603 `no_excess`, 111 `no_daily` |

The 25 `empty` rows are a symbol's own requests that returned nothing: renamed
members whose bars now come from an alias (BKR, CHRD, COR, CTRA, GAP, PSKY,
VRX, VTRS), delisted or renamed symbols EODHD has no 1m for (AET, CPRI, DOC,
DWDP, ESRX, ET, HCN, TWX, VIAC, WFRD), retired pairs (BALL, DXC, PPLI, RDS-A,
SGI) and the excluded TNT and MRIN.

## 4. Caveats

1. **Unadjusted prices.** Use `bars_1m_adj` (or `bars --adjusted`) across splits.
   Dividends are not adjusted.
2. **Use `bars_1m_adj.volume`, not `bars_1m.volume`.** The vendor's 1m
   volume mishandles the closing cross (design doc section 6, "Volume"):
   before 2023-06-22 the cross is missing (a day sums to about 0.9 of the
   daily volume, the 16:00 bar is often absent); from then on the bars also
   count every "official close" report, each a repeat of the cross (AAPL
   2026-11-10: a 16.2M 16:00 bar for a 5.0M cross; NYSE names carry reports
   at 16:10, 18:30, 19:00 or 20:00 too). The volume fix makes each US day sum
   to the daily volume and puts the cross in its minute. Checked on tick truth:
   exact from 2023-06-22 (median error 0.000); before, the close minute is a
   median 7% too high (it also absorbs through-the-day prints the old bars
   lack), so `cross_est` is an upper bound there. Against the official
   closing cross on every member day (Alpaca, `alpaca qc --crosscheck`,
   2026-11-22): from 2023-06-22 `cross_est` / cross has median 1.000 and 95%
   of 338k days are within 5%; before, median 1.087, p90 1.875 and only 29%
   of 748k days within 5%. For the closing cross size before 2023-06-22 use
   `alpaca_auctions.close_size`, not `cross_est`. Days with `status` other
   than `ok` keep the vendor volume. Away from the close, minutes match the
   tape to within a few shares; the opening cross is in the 09:30 bar.
3. **Extended hours.** US bars run 04:00-20:00 ET; filter `session = 'rth'`
   for regular hours. Minutes with no trade have no bar.
4. **Vendor holes.** Some pairs miss whole stretches although they traded
   (TSLA 2023-08 .. 2024-04; NVDA 2024-06-10 .. 2024-07-24, right after its
   10:1 split, confirmed missing at the source). Refetching does not fill
   them; `qc intraday` lists them under `holes`.
5. **Renamed and delisted symbols.** EODHD mostly keys intraday history by the
   symbol in use at the time, while the daily lanes file a renamed company's
   history under its current symbol. 13 `[[alias]]` entries in
   `intraday_universe.toml` fetch a member's pre-rename bars from its old
   symbol and store them under the member, each checked by a one-week probe
   matching the daily close within 0.1%: BKR (BHGE), CHRD (OAS), COR (ABC),
   CTRA (COG), GAP (GPS), PSKY (CBS until 2019-12-04), VTRS (MYL), VRX (BHC
   from 2018-07-16), APTV (DLPH), LUMN (CTL), MRSH (MMC), RTX (UTX, at a
   constant 1.067: RTX's daily series is spin-off adjusted) and TT (IR).
   Alias rows in the state carry `source`. What stays without bars, 213 of
   13,200 stock member-quarters: Barrick as B before 2025-05 (ABX, GOLD
   empty), BBWI (LB), BKNG (PCLN), LIN (PX), SPGI (MHFI), TFC (BBT), TPR
   (COH), WELL (HCN), CPRI (KORS), DOC (HCP / PEAK), ET (ETE), BKR before
   2017-07 (BHI), DD and DWDP before 2019-06, FI before 2023-06 (caveat 6),
   PSKY / VIAC 2019-12 .. 2025-08 (VIAC, PARA), and the delisted AET, ESRX,
   TWX, WFRD (all probed empty). The membership file still lists them, so a
   backtest must treat "member without bars" as missing, not as flat.
6. **Wrong security under a symbol.** Before a symbol passed to its current
   owner, its 1m bars are the previous owner's: FI.US before 2021-10 is Frank's
   International, not Fiserv (bars about 1.5% of Fiserv's daily close; FISV
   holds no 1m for those years), and MTCH.US before 2020-07 is the old Match
   Group (ratio about 0.28 against the daily series, which follows the former
   IAC). `[[mask]]` entries in the toml (FI until 2023-06-06, MTCH until
   2020-06-30) deleted those 596,059 bars on 2026-11-13, and the fetcher never
   requests them again.
7. **Daily series adjusted for spin-offs.** HON, SONY, AIV, CHTR, CB and FDX
   show long runs where the raw 1m close sits 1-11% off the daily `close`: the
   daily lanes adjust history for spin-offs and mergers, the 1m bars do not.
   Not a 1m error; the close check reports them alongside real ones.
8. **Index quote quality from 2024.** GSPC / NDX 1m bars carry bad prints
   (GSPC on 48 days 2024-11 .. 2025-04, e.g. 2025-04-25 from 10:00 ET at about
   49 instead of 5,500) and weekend / holiday dates with overnight bars only.
9. **Half days.** On early-close days `rth` ends at the early close (US and
   INDX 13:00 ET the day after Thanksgiving, and 24 December and 3 July
   Monday to Thursday; LSE 12:30 on 24 and 31 December); later bars are
   `post`. Rule-based (`early_close` in the fetcher); 191,047 stored bars were
   retagged on 2026-11-13.
10. **Daily volume is split-adjusted, daily close is not.** EODHD's daily
   `volume` is restated for later splits (AAPL 2020-08-28: 187.6M against 46.9M
   traded) while `close` stays raw. The universe ranking divides volume by the
   later forward splits (the lookahead otherwise ranked NVDA, AMZN, GOOGL and
   TSLA first in 2018), and `qc intraday` does the same for its volume ratio.
11. **Survivorship before 2018.** The ranking only sees names priced daily;
   us_extended adds delisted names from FINRA short interest (2018 on) with
   daily prices from 2017. Rankings before 2018 can miss names that later
   delisted.
12. **GSPC / NDX overnight bars** are a vendor-computed 24-hour quote (prices
   with 5 decimals that move every minute overnight, with volume), not the
   official index. Use their `rth` bars only, or SPY / QQQ. VIX extended-hours
   bars match Cboe's extended session. Index 1m history starts 2020-03.
13. **LSE 1m starts 2021-10-12** for all eight UK ETFs.
14. **Publication lag.** The previous US session was not published at 00:45 UTC,
   and on 2026-11-17 and 11-18 it was still missing for 150 of 350 pairs at
   06:00 London. The job runs at 12:00 London since 2026-11-19, tops up with a
   3-day overlap, and holds a recent window's coverage only through its newest
   bar, so a late session is asked for again. A 12:00 run also stores part of
   the current LSE day (and US pre-market bars), completed by the next run.
15. **Not consolidated live.** The free WebSocket is Cboe EDGX only; historical
    1m bars are consolidated. Do not mix them as one series without checking.
16. **Drive.** Pushed to Google Drive with the rest of the root since
    2026-11-13 (quota: 5.4 TB free; first push 4,432 files, 8.0 GB). The
    nightly push uploads the changed current-year files.
17. **One company, several symbols.** The stock lanes keep a renamed
    company's series under both symbols (CBS / VIAC / PSKY, BK / BNY, SQ / XYZ,
    MMC / MRSH, ANTM / ELV, FI / FISV, ...). The ranking counts such a group
    once per quarter, labelled by the symbol whose 1m bars cover the quarter
    (XYZ holds SQ's history, ELV ANTM's, SHEL RDS-A's), else the symbol in use
    then; the company keeps its tier across the relabel. A name also needs a
    daily price in the 10 days before the quarter. Together these changed 326
    of 15,532 member-quarters on 2026-11-13. A model that tracks a company
    across quarters must follow the label change (same `company`, different
    `ticker`).
18. **Membership before 2026-11-19** left out 12 to 48 top-300 names a quarter
    (incumbents filled a fixed count first, so a newcomer ranked as high as 86
    could miss the lane). Rebuilt with the buffer rule, where tier counts float
    (2026Q4: 355 stocks); a membership file or backtest from before then is
    stale.

## 5. Cost

5 units per request (one pair, at most 118 days). Backfill on 2026-11-13:
11,536 requests (57,680 units, 01:01-04:36 UTC, 10 workers), then 496, 934 and
17 more after the universe fixes, about 65,000 units in all, 0 failures. The
lane's splits refresh cost 521 units. The alias and duplicate-company fixes
cost 1,900 units (380 requests, 0 failures) plus 215 units of probes. The
volume investigation used about 480 units of tick requests (1 unit per
10,000-tick page; a full AAPL day is about 65 pages).
Routine top-up: about 350 pairs, about 1.8k units a night.
