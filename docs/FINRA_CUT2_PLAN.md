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
is two to four weeks. **Confirmed on 2026-10-02:** a request with a plain
descriptive `User-Agent` is refused with the SEC's "Request Rate Threshold
Exceeded" page. The SEC's automated-access policy requires the user agent to
declare a company name and a contact email address. Iteration 6 therefore
needs an owner-provided contact string, held in the config under `[finra]
sec_user_agent` (not in code, not in this doc), before any file is fetched.

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

**Iteration 2, 2026-10-02: the `short_interest` dataset.**
`finra/short_interest.py` on the existing partition store, one file per
settlement date under `short_interest/settlement/`. The settlement calendar
rule (the 15th and the month's last day, each moved back to the preceding
trading day) yields exactly the 210 dates FINRA's partitions endpoint
lists, so the planner works offline. `published_at` is set at ingest as
settlement plus seven business days. Rows are stored for every market
class, with `revised` and `split_adjusted` as booleans; a restatement is a
changed digest and the state notes how many rows FINRA flagged `R`. CLI,
scheduler and completion admit the dataset; the API transport is now built
generically from the provider. Two live settlement dates (2026-08-31,
2026-09-15; 45,164 rows) passed `qc`. Noted for a later iteration: three
providers now carry near-identical refresh loops; a shared engine is the
cleanup once the fourth dataset shows what varies.

**Iteration 3, 2026-10-02: short interest views.** `finra_short_interest`
adds `listed`, a `security_kind` taken from the issue name (the symbols
carry no separators) and `eodhd_code` resolved through the SIP spellings in
the short volume store, so `BRKB` maps to `BRK-B`. On the live 2026-09-15
partition every one of the 12,748 listed common names maps; 9,559 OTC rows
and 288 warrants, units, preferreds and rights map to NULL by design.
`finra_short_interest_float` divides the position by the EODHD quarterly
shares outstanding taken as known 45 days after the quarter end
(point-in-time) and, separately labelled, by today's float snapshot (not
point-in-time): GME 6.6 percent of outstanding on 2026-09-15, AAPL 0.87.
Lab snippet, MCP surface and REFERENCE updated. Suite 647 passed.

**Iteration 4, 2026-10-02: short interest backfill.** All 210 settlement
dates from 2017-12-29 to 2026-09-15 in one unattended run of about an hour,
no absences, no errors, `qc` clean. The store holds 3,971,752 rows, the
exact `record-total` FINRA reports for the whole dataset; 54.8 percent of
rows are exchange-listed classes.

**Iteration 5, 2026-10-02: the evaluation with short interest.**
`finra/panel.py` gained `short_interest_features`: for each panel row the
latest report with `published_at` strictly before the trading day (FINRA
disseminates after the close, so a report published on T is not known at
T's close), via an ASOF join on `finra_short_interest_float`; a test pins
the strict inequality. On the 60-day `event@5` panel the report was known
on 7,655 of 7,663 rows with a median age of 10 days.

Result, next-session horizon, per-day rank correlation with the size of the
market-adjusted move, t over days:

| variable | corr | t |
|---|---|---|
| materiality (baseline, same rows) | 0.095 | 8.3 |
| short interest over shares outstanding | 0.153 | 10.9 |
| days to cover | -0.045 | -3.0 |
| trailing 5-day short volume ratio (phase 5) | -0.024 | -1.7 |

Short interest over outstanding is the strongest single ordering of move
size in the panel, and it holds **jointly** with materiality: in the per-day
rank regression both coefficients keep their size (materiality 0.096, t
7.1; short interest 0.152, t 10.6). It is **additive, not an amplifier**: the
interaction term is flat (t -1.05 at one day, -0.86 at five), materiality's
correlation is the same inside every short-interest tercile (0.096, 0.073,
0.075; high minus low t -0.68), and the tercile gradient of move size is the
same at every materiality level (mat 0: 142 to 210 bps; mat 1: 165 to 279;
mat 2: 202 to 320). Days to cover carries nothing the two do not. Twenty-six
tests in the family; the stand-alone and joint short-interest statistics
clear Bonferroni by a wide margin; no interaction does.

Reading: heavily shorted names move more after news, and move more without
it; that is the long-known volatility of crowded shorts, now measured
point-in-time on this panel. The practical product is a two-factor
magnitude model, materiality plus short interest over outstanding, which
beats either alone. The hypothesis as stated in phase 5, that positioning
*changes* the effect of material news, is not supported by any of the three
positioning measures now on disk.

**Iteration 6, 2026-10-02: SEC fails to deliver.** The owner provided the
contact string; it lives in the Windows user environment as
`SEC_USER_AGENT` (read like the API keys, with `[finra] sec_user_agent` as
a config fallback) because the live eodhd job fingerprints `datacli.toml`
and an edit there would stop it. `finra/sec.py` is the fourth transport: a
strict parser for the half-month zips (header, six fields, dates inside the
half, unique `(settlement date, CUSIP)`, integer quantities, `.` prices as
null, and **both trailers checked**, the record count and the share total),
UTF-8 with a cp1252 fallback for old files, 404 as absent and the SEC's 403
policy page as a named error. `finra/fails_to_deliver.py` stores one
partition per half (`halves/2026-09-01.parquet`, `2026-09-16.parquet`) with
`published_at` as the SEC's posting rule plus a five-day margin, chosen so a
row is never treated as known before the SEC could have posted it. The
view resolves separator-free symbols through the short volume store. Two
live halves (121,504 rows, 21 settlement days) passed `qc`; the backfill
from 2018-08 (194 halves) ran the same day.

The backfill taught four SEC conventions, each caught by the strict parser
or the 404 handling and fixed with a test: the split between a month's two
files is the SEC's own (the July 2026 "b" file carries the 15th), so a file
is checked against its month and `qc` checks the two files of a month for
shared rows; some months live under `/files/data/other/` (May 2026) or
`/files/node/add/data_distribution/` (February to April 2020) and one file
carries a `_0` suffix (October 2019), so the client reads the SEC's listing
page once per run and tries the listed URL before the rule-based paths;
and "CNS INELIGIBLE SECURITY" rows carry a CUSIP and no symbol and are kept
as published; and a description may itself contain a pipe (April 2021), so
extra fields are folded into the description while the five fixed-position
fields still validate. The store is complete: 194 halves from 2018-08-01
to 2026-08-16, about 10.1M rows, `qc` clean; the finra root is 1.3 GB
across the four datasets. Suite 677 passed.

**Iteration 7, 2026-10-02: the nightly job and docs.** The job draft
`finra-short-volume-daily-edit-935f997f` carries five steps: short volume
fetch and qc, weekly flow, short interest and fails-to-deliver fetches,
each capped with `--limit-days`. Enabling it (an elevated terminal, since
the task runs logged-off) registers generation 2. REFERENCE and the design
doc are current.

## 6. Where this leaves the positioning question

Four point-in-time positioning measures are now on disk per NMS symbol:
daily short volume (and long volume), twice-monthly short interest with
days to cover and the float ratio, weekly dark-pool and OTC flow by venue,
and daily fails to deliver. Against the 60-day scored panel, none of them
changes the effect of material news; short interest over outstanding is a
strong additive magnitude factor, and the rest add nothing beyond it. The
next useful experiment is not another positioning measure but a longer
scored window, which the nightly scoring job accumulates by itself.
