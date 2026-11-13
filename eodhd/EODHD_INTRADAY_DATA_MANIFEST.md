# EODHD Intraday (1m bars) Data Manifest

**Status:** FACTUAL-INVENTORY / BACKFILL COMPLETE (2026-11-13)
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
| T1 | 100 | top US common stocks by median daily dollar volume (join rank <= 100, stay <= 130), price >= $5 | trade |
| T2 | 200 | next 200 (join <= 300, stay <= 390) | training breadth |
| context | 38 | curated US ETFs (broad, sectors, themes, rates/credit, commodities, international, vol/crypto) | signal only (no UK KID) |
| index | 3 | GSPC, NDX, VIX | signal only |
| fx | 4 | GBPUSD, EURUSD, USDJPY, USDCNH | signal only |
| uk | 8 | ISA-eligible LSE UCITS ETFs (CSPX, SWDA, VUSA, EQQQ, IUIT, CNX1, SGLN, IGLT) | trade |

Stock tiers are re-ranked at each quarter start from the trailing year, so the
lane also holds former members (kept for history, not refreshed): 544 pairs over
44 quarters (2016Q1 .. 2026Q4), 353 current members. The fetch state holds 589
pairs: 45 more were members under the first, lookahead-biased ranking (section
4, caveat 10) and keep their bars as retired pairs.

## 2. Local paths (under `<data-root>/intraday/`)

- `intraday_universe.toml` (repo, `eodhd/`): the rules and curated lists
- `tickers_INTRADAY.parquet`: current members (`tier`, `group`, `role`, `isa_tradable`, `adv_rank`, `fetch_from`)
- `universe_membership.parquet`: one row per quarter and member (point in time), `listed_from`
- `bars_1m/<TICKER>.<EXCH>_<YYYY>.parquet`: bars, one file per pair-year
- `bars_1m_fetch_state.csv`: per pair `coverage_from` / `coverage_through`, status
- `bars_1m_fetch_audit.csv`: one row per request (`requested_at`, units, rows, outcome)

Bar columns: `ticker`, `exchange`, `ts` (bar open, UTC), `date`
(exchange-local trading date), `session` (`pre` / `rth` / `post`, `24h` for FX),
`open`, `high`, `low`, `close`, `volume`.

DuckDB views (after `reindex`): `bars_1m`, `bars_1m_state`, `bars_1m_adj`
(split-adjusted from `splits`; `split_adj` is the factor), `intraday_universe`,
`intraday_membership`.

## 3. Observed state (2026-11-13, after the first backfill)

From `status intraday` and `qc intraday --all` after the last run:

| Measure | Value |
|---|---|
| bars | 477,088,033 in 4,312 pair-year files, 8.4 GB (about 18 bytes a bar) |
| pairs in state | 589: 565 `ok`, 24 `empty`, 0 partial |
| span | 2015-12 (warmup) .. 2026-11-12 |
| duplicates | 0 |
| OHLC failures | 1 bar (0.1% tolerance) |
| close check (>2% off daily close) | 7,636 pair-days; worst GSPC.INDX 2025-04-25 (ratio 0.009). Groups: wrong security (FI, MTCH), spin-off-adjusted daily closes (HON, SONY, AIV, CHTR, CB, FDX), bad index prints, half days, VIX's 16:15 close |
| holes (5+ missing trading days) | 9 pairs, worst FI.US 423 days (2021-10 .. 2023-06: the symbol was unused); TSLA, NVDA vendor holes |
| thin sessions | 15 pairs below 80% of the expected regular-hours bars (UHAL, IGLT and the other LSE ETFs: thin markets, not errors) |
| T1 gaps over 5 min | 15 pairs, most MSTR (thin before 2020) |
| short days excluded | US 22, INDX 186 (degraded index quotes from 2024), LSE none (too thin to judge) |
| volume ratio (rth + closing bar) / daily | US p10 0.79 / p50 0.93 / p90 1.12; LSE p50 2.4; INDX meaningless |

The 24 `empty` pairs are renamed or delisted symbols (caveat 5) plus TNT and
MRIN, which are now excluded: AET, BALL, BKR, CHRD, COR, CPRI, CTRA, DOC,
DWDP, DXC, ESRX, ET, GAP, MRIN, PPLI, PSKY, RDS-A, SGI, TNT, TWX, VIAC, VRX,
VTRS, WFRD.

## 4. Caveats

1. **Unadjusted prices.** Use `bars_1m_adj` (or `bars --adjusted`) across splits.
   Dividends are not adjusted.
2. **1m volume is not daily volume.** For AAPL the first post-close bar (16:00
   ET) holds 12-16M shares (the closing auction prints); regular-hours bars
   plus that bar exceed daily volume by 10-25%. Treat volume features with care.
3. **Extended hours.** US bars run 04:00-20:00 ET; filter `session = 'rth'`
   for regular hours. Minutes with no trade have no bar.
4. **Vendor holes.** Some pairs miss whole stretches although they traded
   (TSLA 2023-08 .. 2024-04; NVDA 2024-06-10 .. 2024-07-24, right after its
   10:1 split, confirmed missing at the source). Refetching does not fill
   them; `qc intraday` lists them under `holes`.
5. **Renamed and delisted symbols (22 `empty` pairs).** EODHD keys intraday
   history by the symbol in use at the time, while the daily lanes file a
   renamed company's history under its current symbol. A member ranked under
   its current symbol (BKR, CTRA, LUMN, VTRS, GAP, BALL, CPRI, ...) therefore has
   no 1m bars for its pre-rename quarters; the bars exist under the old symbol
   (probed: BHGE.US has 2018 bars, BKR.US does not). Some delisted members
   (AET, ESRX, TWX, ...) return nothing under any symbol tried (AET.US is
   empty for 2018). The membership file still lists these pairs, so a backtest
   must treat "member without bars" as missing, not as flat. A symbol-alias
   map would recover the renamed ones (design doc, open check 10).
6. **Wrong security under a symbol.** Before a symbol passed to its current
   owner, its 1m bars are the previous owner's: FI.US before 2021-10 is Frank's
   International, not Fiserv (bars about 1.5% of Fiserv's daily close; Fiserv's
   own history is under FISV), and MTCH.US before 2020-07 is the old Match
   Group (ratio about 0.28 against the daily series, which follows the former
   IAC). `qc intraday`'s `close_check` lists such pair-days; mask them before
   training.
7. **Daily series adjusted for spin-offs.** HON, SONY, AIV, CHTR, CB and FDX
   show long runs where the raw 1m close sits 1-11% off the daily `close`: the
   daily lanes adjust history for spin-offs and mergers, the 1m bars do not.
   Not a 1m error; the close check reports them alongside real ones.
8. **Index quote quality from 2024.** GSPC / NDX 1m bars carry bad prints
   (GSPC on 48 days 2024-11 .. 2025-04, e.g. 2025-04-25 from 10:00 ET at about
   49 instead of 5,500) and weekend / holiday dates with overnight bars only.
9. **Half days.** `session = 'rth'` is 09:30-16:00 ET on every day, so on a
   13:00 close the bars after 13:00 are after-hours trades.
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
14. **Publication lag.** The previous US session was not published at 00:45 UTC;
   the 06:00 London job tops up with a 3-day overlap, which self-heals a late day.
15. **Not consolidated live.** The free WebSocket is Cboe EDGX only; historical
    1m bars are consolidated. Do not mix them as one series without checking.
16. **Drive.** `intraday/bars_1m` is not pushed to Google Drive
    (`storage/engine.DEFAULT_EXCLUDE_PATHS`) until the quota is checked.

## 5. Cost

5 units per request (one pair, at most 118 days). Backfill on 2026-11-13:
11,536 requests (57,680 units, 01:01-04:36 UTC, 10 workers), then 496, 934 and
17 more after the universe fixes, about 65,000 units in all, 0 failures. The
lane's splits refresh cost 521 units.
Routine top-up: about 350 pairs, about 1.8k units a night.
