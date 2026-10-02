# FINRA cut 2: positioning datasets, execution plan

Status: plan written 2026-10-02 after the owner's decision to add, in this
order, long volume for symmetry, consolidated short interest, and whatever
else belongs in a first positioning cut. Builds on `FINRA_SOURCE_DESIGN.md`
(the source, `short_volume`, `weekly_flow`, the phase 5 evaluation). Every
iteration below is small, ends with tests, a doc update and a commit, and
records what it found in section 5 before the next one starts.

## 1. Goal

Give the scoring work a complete, point-in-time picture of short-side
positioning per NMS symbol: how much is sold short each day (have), how much
is held short at each settlement date and how long it takes to cover (this
cut), and how much fails to deliver (this cut, from the SEC). Then re-ask
the phase 5 question with the measure the hypothesis actually named: days to
cover.

## 2. Verified facts (probed 2026-10-02 with credentials)

**Consolidated short interest** (`otcMarket/consolidatedShortInterest`):

- Partitioned by `settlementDate`; 210 partitions from 2017-12-29 to
  2026-09-15, twice a month (mid-month and month-end settlement dates).
  About 22,600 rows per partition, 5 s per partition, 4.7M rows in all.
- Fields: `symbolCode`, `issueName`, `marketClassCode` (OTC 9,559; NNM
  3,862; NYSE 2,907; ARCA 2,710; SC 1,654; BZX 1,597; AMEX 306 on
  2026-09-15), `issuerServicesGroupExchangeCode`,
  `currentShortPositionQuantity`, `previousShortPositionQuantity`,
  `changePreviousNumber`, `changePercent`, `averageDailyVolumeQuantity`,
  `daysToCoverQuantity` (1.00 to 999.99, never null), `revisionFlag` (`R` on
  17 rows), `stockSplitFlag` (`S` on 54), `settlementDate`,
  `accountingYearMonthNumber`. No null or duplicate `symbolCode` in a
  partition.
- **Symbols have no separators**: `BRKB`, `ABRPRD` (preferred as `PR` +
  series). The daily short volume store, which carries the SIP spelling
  (`BRK/B`, `ABRpD`), is the lookup that maps them.
- **Publication rule, from FINRA's schedule page**: positions are due by
  18:00 ET on the second business day after settlement and published five
  business days after that, i.e. **settlement + 7 business days**, holidays
  skipped. Checked against four schedule rows: 2025-11-14 published
  2025-11-25, 2025-12-15 published 2025-12-24, 2025-12-31 published
  2026-01-12 (New Year skipped), 2026-01-15 published 2026-01-27 (MLK Day
  skipped). The API carries no publication date, so the rule is the
  point-in-time fact and needs a market-holiday calendar.

**Threshold list** (`otcMarket/thresholdList`): daily from 2016-01-04, about
20 rows a day, and **OTC-only** on every date probed (2021-02-01 at the
meme-stock peak, 2021-06-15, 2024-03-15, 2026-06-15, 2026-10-01). Exchange
threshold lists are published by the listing exchanges, not FINRA. Dropped
from this cut.

**SEC fails to deliver** (sec.gov, public, no credentials): zip files
`/files/data/fails-deliver-data/cnsfails<YYYYMM>a.zip` (first half of the
month) and `...b.zip` (second half), each a pipe-delimited text file with no
header: settlement date, CUSIP, symbol, fails quantity, description, price.
History from February 2004. The first half appears at the end of the month,
the second half about the 15th of the next month, so the point-in-time lag
is two to four weeks. The SEC's fair-access policy expects a descriptive
`User-Agent`; to confirm on first fetch.

**Market holidays**: the daily short volume store already records every
NYSE holiday since 2018 as an `absent` weekday (79 of them, plus the
2018-12-05 day of mourning). The rule-based calendar built in iteration 1 is
checked against that list.

## 3. Decisions taken for this cut

1. `long_volume` and `long_ratio` are view columns on `finra_short_volume`;
   no dataset. "Long interest" has no counterpart anywhere; the symmetric
   measure is short interest over float, a view once short interest and the
   EODHD share statistics meet.
2. Short interest is stored **as published, all market classes**: the whole
   dataset is one partition per settlement date and 4.7M rows, so filtering
   OTC at ingest would save nothing and break "store raw". The views expose
   the exchange-listed classes.
3. Point-in-time columns are added at ingest from the rule
   (`published_at = settlement + 7 business days`), never inferred later.
4. Threshold list out; SEC fails to deliver in, as the fourth transport.
5. The symbol mapping for short interest is data-driven: a separator-free
   symbol is matched against the SIP spellings in the daily short volume
   store (`BRKB` = `BRK/B` -> `BRK-B`), with the `PR` preferred convention
   mapped to NULL like the SIP `p` convention.

## 4. Iterations

| # | scope | acceptance | out of scope |
|---|---|---|---|
| 1 | `finra/calendar.py`: NYSE holiday rules and business-day arithmetic; `long_volume` / `long_ratio` on the short volume view | holiday set equals the store's absent weekdays 2018-2026 except the mourning day; `settlement + 7 business days` reproduces the four schedule rows; view test | any new dataset |
| 2 | `short_interest` dataset: registry, provider (API, one partition per settlement date, `published_at` by rule, restatement by digest and `revisionFlag`), store under the existing partition store, CLI and scheduler admission, qc | 2 live partitions pass qc; dry run plans 210; tests offline | views, backfill |
| 3 | views `finra_short_interest` (with `eodhd_code` via the SIP lookup, `published_at`) and `finra_short_interest_float` (short interest over EODHD shares outstanding, when the share statistics exist); docs | BRKB maps to BRK-B; the float ratio joins for AAPL; lab snippet; REFERENCE | backfill |
| 4 | backfill 210 partitions (about 20 min), qc, commit | qc clean; partitions = settlement dates since 2017-12-29 | |
| 5 | evaluation rerun: days to cover and short interest over float as positioning, point-in-time by `published_at` (ASOF), same tests as phase 5 | numbers recorded in section 5 and in the design doc | new tests of a different kind |
| 6 | SEC fails to deliver: file transport, dataset `fails_to_deliver` (half-month files, settlement-date rows, `published_at` by rule), views, backfill | 2 live files pass qc; backfill since 2018 to match the store | 2004-2017 history unless cheap |
| 7 | nightly job generation with the new steps; REFERENCE and design doc closed out | job draft shows every step; docs current | |

Each iteration: tests first for the pure parts, then the live check with
credentials, read-only probes only until the fetch step, full suite before
the commit, one commit per iteration.

## 5. Log

**Iteration 1, 2026-10-02: calendar and long volume.** `finra/calendar.py`
gained the NYSE closure rules (fixed days with the exchange's observance
rule, the Monday holidays, Good Friday from the Easter algorithm,
Thanksgiving, Juneteenth from 2022, and the two days of mourning as named
special closures) plus `is_trading_day`, `next_trading_day` and
`business_days_after`. The rule set reproduces all 79 absent weekdays the
daily short volume store recorded from 2018-08-01 to 2026-09-30, exactly,
and `settlement + 7 business days` reproduces the four schedule rows
including the New Year and MLK Day skips. `finra_short_volume` gained
`long_volume` and `long_ratio`. Suite 639 passed.
