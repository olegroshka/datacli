# FINRA source: design for the access layer and the daily short volume dataset

Status: design approved by the owner on 2026-10-01 (decisions in section
13). Phases 0 and 1 implemented the same day (section 11 tracks progress).

## 1. Purpose and scope

Add `finra` as a third operational source next to `eodhd` and `macro`, built as
two things:

1. A **FINRA access layer**: one authenticated client for the FINRA Query API
   (`api.finra.org`) and one reader for FINRA's public daily files on
   `cdn.finra.org`. It is dataset-agnostic and is what every later FINRA
   dataset (consolidated short interest, threshold list, TRACE) is built on.
2. The **first dataset on top of it**: Reg SHO daily short sale volume for NMS
   stocks, stored point-in-time, exposed as a DuckDB view and schedulable by
   the daily job.

The scoring use case that motivates it: the news materiality judgement
forecasts the *size* of the next session's move (README, "What we measured").
Short volume ratio is a positioning variable to test as an interaction with
materiality. That evaluation is out of scope here; this document only gets
the data in cleanly.

Out of scope for v1: OTC (non-NMS) symbols, per-facility breakdown, bi-monthly
short interest, TRACE. Each is a registry entry or a new provider module on the
same layer, not a redesign (section 12 checks this).

## 2. Verified facts (probed live on 2026-10-01, no credentials)

Query API, dataset `otcMarket/regShoDaily`:

- Metadata endpoint `GET /metadata/group/otcMarket/name/regShoDaily` is public.
  Fields: `tradeReportDate` (Date, `yyyy-MM-dd`),
  `securitiesInformationProcessorSymbolIdentifier` (String),
  `shortParQuantity`, `shortExemptParQuantity`, `totalParQuantity` (Number),
  `marketCode` (String), `reportingFacilityCode` (String; N = NYSE TRF,
  Q = Nasdaq TRF Carteret, B = Nasdaq TRF Chicago, D = ADF). Partition field is
  `tradeReportDate`.
- `GET /partitions/group/otcMarket/name/regShoDaily` lists published trade
  dates. Unauthenticated it returned 251 partitions, 2025-10-01 to
  2026-09-30: a **rolling one-year window**. A query for 2025-01-02 returned
  `204 No Content` with `record-total: 0`.
- `POST /data/group/otcMarket/name/regShoDaily` with
  `compareFilters=[{EQUAL, tradeReportDate, 2026-09-30}]` returned
  `record-total: 28574` rows for the day, one row per symbol per facility,
  `record-max-limit: 5000`. Quantities can be **fractional**
  (`totalParQuantity: 1465.9293`).
- Response headers: `record-total`, `record-offset`, `record-limit`,
  `record-max-limit`, `finra-api-request-id`. An empty result is `204` with an
  empty body, not `[]`. Without `Accept: application/json` the API returns CSV.
- Documented limits: sync 5,000 rows per request, offset up to 500,000,
  1,200 requests/min per IP; async mode up to 100,000 rows with a polling
  status URL. Token endpoint:
  `POST https://ews.fip.finra.org/fip/rest/ews/oauth2/access_token?grant_type=client_credentials`
  with HTTP Basic `client_id:client_secret`; the response carries
  `access_token` and `expires_in` seconds.
- **Authenticated probe (same day, credentials from the Windows user
  environment):** FIP token flow works, `expires_in` was 43,169 seconds
  (about 12 hours), `token_type: Bearer`. The authenticated `regShoDaily`
  partitions list is the **same 251-day window**, and 2025-01-02 is still
  204. The one-year window is a property of the dataset, not of
  entitlement, so the CDN backfill is required regardless of credentials.
- Entitled datasets visible with these credentials, for later phases:
  - `otcMarket/consolidatedShortInterest`: 210 settlement-date partitions
    from 2017-12-29 to 2026-09-15, 3.97M rows; fields include `symbolCode`,
    `settlementDate`, `currentShortPositionQuantity`,
    `previousShortPositionQuantity`, `averageDailyVolumeQuantity`,
    `daysToCoverQuantity`, `revisionFlag`, `stockSplitFlag`.
  - `otcMarket/thresholdList`: daily partitions from 2016-01-04.
  - `otcMarket/weeklySummary`: weekly ATS and OTC volume from 2021-12-06,
    57M rows.

CDN daily files, `https://cdn.finra.org/equity/regsho/daily/<FAMILY>shvol<YYYYMMDD>.txt`:

- Families: `CNMS` (consolidated NMS: TRFs + ADF, exchange-listed), `FNSQ`
  (Nasdaq TRF Carteret), `FNYX` (NYSE TRF), `FNQC` (Nasdaq TRF Chicago),
  `FNRA` (ADF), `FORF` (ORF, OTC).
- Layout, verified on `CNMSshvol20250102.txt`: header
  `Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market`, pipe
  delimited, dates `YYYYMMDD`, `Market` is a comma list of facility codes
  (`B,Q,N`), and a **trailer line holding the record count** (`10347` for
  10,347 data rows). No row had short greater than total.
- **Volumes are whole shares until early 2026 and fractional shares since.**
  Found by the strict parser on the first live fetch (2026-10-01): every row
  of `CNMSshvol20260930.txt` reads like `A|388551.214956|45|1009929.273351`.
  Monthly probes show integers through 2026-02-02 and fractions from
  2026-03-02 on, so FINRA started publishing fractional-share volumes in
  February 2026, matching the API's fractional quantities. The canonical
  volume columns are therefore float64, stored exactly as published.
- History: `CNMS` starts 2018-08-01 (2018-07-31 is absent); `FNSQ` starts
  2009-08-03. Published "no later than 6:00 pm ET on the trade date".
- A missing day (holiday, future, before the first date) returns **HTTP 403**,
  not 404, served by CloudFront with an S3 body
  `<Error><Code>AccessDenied</Code>...`. 2025-07-04 (holiday) returned exactly
  that. Any 403 with a different body is therefore not "absent".
- The oldest consolidated file, `CNMSshvol20180801.txt`, has the identical
  header, row layout and trailer (7,676 rows), so one strict parser covers the
  whole history.
- FINRA states that in rare cases it republishes a corrected file and marks it
  "Updated" on the catalogue page. Whether the CDN URL content changes in place
  is unverified.
- Symbols are SIP spellings: class shares `BRK/B`, preferreds `ABRpD`
  (lower-case `p` + series), warrants `AACT/WS`, units `AACT/U`. EODHD spells
  the same class share `BRK-B` and carries no preferreds in `us_common`.

## 3. Design principles (how it fits datacli)

- **Follow the `macro/` shape**: a real package with `config.py`,
  `registry.py`, provider modules, `views.py`, `cli.py`; a `SourcePlugin` in
  `datacli.py`; views layered best-effort onto the explorer and lab
  connections.
- **Reuse, do not fork**: `eodhd/_http.py` retry, `eodhd/_atomic.py` atomic
  writes, `eodhd/_render.py` palette, `macro.util.merge_on`. One shared change
  is needed (section 5.1).
- **Dry-run by default**, `--run` mutates, `direct_mutation_lock` around it.
- **Three planes stay separate**: what FINRA has published (API partitions or
  CDN probe), what the store holds (parquet plus state sidecar), and what each
  run did (typed report, scheduler journal). Status reports all three and never
  infers one from another.
- **Store raw, map at the edge**: the SIP symbol and the integer volumes are
  stored exactly as published. Mapping to EODHD tickers and derived ratios live
  in the DuckDB view.
- **Typed results**: providers return frozen dataclasses, the CLI renders them,
  the exit code is derived from them.
- **No secrets anywhere but the environment**: `FINRA_CLIENT_ID` and
  `FINRA_API_KEY` (the client secret) are read once, held in a dataclass with a
  masked `repr`, sent only in HTTP headers, never in URLs, logs, state files or
  config.

## 4. Architecture

Four layers, each independently unit-testable with a fake session and a
temporary root.

```
finra/
  __init__.py
  config.py          finra_root(), dataset paths, [finra] section loader
  auth.py            Credentials.from_env(), TokenProvider (FIP client-credentials, cached)
  api.py             QueryApiClient: metadata / partitions / query (paged) / error mapping
  cdn.py             DailyFileClient: fetch_day(family, date) -> DailyFile | None, strict parser
  registry.py        DatasetSpec entries: short_volume (v1), later short_interest, threshold_list
  short_volume.py    provider: plan_days, refresh(run=...), normalisation from either transport
  store.py           DayPartitionedStore: day parquet files + fetch-state CSV, atomic
  calendar.py        publication clock: now_et(), weekdays(), is_publishable(date, now)
  views.py           register(con): finra_short_volume view + schema_snippet()
  cli.py             list / status / fetch / qc / probe
```

Layer boundaries:

| Layer | Knows about | Returns | Must not know |
|---|---|---|---|
| transport (`auth`, `api`, `cdn`) | HTTP, auth, paging, FINRA error codes | plain Python (`list[dict]`, `DailyFile`) | pandas, disk, what a dataset means |
| dataset (`registry`, `short_volume`, `calendar`) | schema, normalisation, which transport for which date | `pandas.DataFrame` in the canonical schema, `RefreshReport` | Rich, CLI flags |
| store (`store`) | paths, parquet, state sidecar, atomic writes | frames, `DayState` rows | HTTP |
| surface (`cli`, `views`, plugin, scheduler entry) | rendering, flags, exit codes, DuckDB | exit `int` | the FINRA wire format |

## 5. The access layer

### 5.1 Shared retry helper gains POST

`eodhd/_http.get_with_retry` is GET-only. Add
`request_with_retry(session, method, url, *, params=None, json=None,
headers=None, timeout, attempts, backoff, max_backoff, log, label, sleep)`
with identical semantics, and make `get_with_retry` a one-line wrapper. The
existing tests in `tests/test_eodhd_http.py` must pass unchanged; new tests
cover POST and headers.

One more shared change surfaced during phase 1: the `macro` CLI carried its
own 120-line command table and flag parser, and the `finra` CLI would have
been a second copy. It now lives once in `eodhd/_cmdtable.py` (`Command`,
`Flag`, `parse`, `command_help`, `parse_or_exit`, `bad_choice`) and
`macro/cli.py` imports it; its 37 CLI tests pass unchanged.

### 5.2 `finra/auth.py`

```python
@dataclass(frozen=True)
class Credentials:
    client_id: str
    client_secret: str              # FINRA_API_KEY
    def __repr__(self) -> str: ...  # 'Credentials(client_id=****abcd)'
    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Credentials | None":
        """Both FINRA_CLIENT_ID and FINRA_API_KEY set -> Credentials; neither -> None;
        exactly one -> CredentialsError naming the missing variable."""
```

Lookup order per variable: the process environment, then the Windows user
environment (`HKCU\Environment`), the same two-step rule the EODHD key uses.
The registry fallback matters because a shell or IDE started before the
variable was set never sees it in its own environment; the scheduled task
and any fresh process do. The eodhd lookup is specific to one variable name;
phase 1 generalises it into a small `read_user_env(name)` helper in
`finra/auth.py` and leaves the eodhd code as is.

```python

class TokenProvider:
    """Caches one bearer token; refreshes when fewer than `skew` seconds remain."""
    def __init__(self, session, credentials, *, clock=time.monotonic, skew=60.0, timeout=30.0): ...
    def token(self) -> str: ...        # POST FIP with Basic auth; raises AuthError on 4xx
    def invalidate(self) -> None: ...  # called by the client after a 401
```

The FIP response body is never logged. `AuthError` carries the HTTP status and
`finra-api-request-id` only.

### 5.3 `finra/api.py`

```python
class QueryApiClient:
    def __init__(self, session, token_provider: TokenProvider | None, *,
                 base_url=BASE_URL, timeout=60.0, page_size=5000, sleep=None, log=None): ...
    def metadata(self, group: str, name: str) -> DatasetMetadata
    def partitions(self, group: str, name: str) -> list[str]       # sorted ISO dates
    def query(self, group: str, name: str, *, filters: Filters, fields=(), limit=None) -> list[dict]
```

- `token_provider=None` means anonymous: public datasets work, and a 401 or
  403 raises `EntitlementError("set FINRA_CLIENT_ID and FINRA_API_KEY")`.
- With a provider, every request carries `Authorization: Bearer`. On 401 the
  client invalidates the token and retries that request once.
- `query` pages with `limit` and `offset` until `offset >= record-total`,
  honouring `record-max-limit` if lower than `page_size`. A 204 is an empty
  page. It refuses to page past the documented 500,000 offset and raises
  `TooManyRows` telling the caller to narrow the filter.
- `Filters` is a small frozen dataclass (`compare`, `date_range`, `domain`)
  that serialises to FINRA's JSON. Field names are passed through verbatim.
- Headers: `Accept: application/json`, `Content-Type: application/json`.
- Errors map to a small hierarchy under `FinraError`: `AuthError`,
  `EntitlementError`, `NotFound`, `RateLimited`, `ServerError`, `TooManyRows`.
  Every error carries the request id when present.
- Rate limiting: an optional fixed `sleep` between pages (default 0.1s).
- Async mode is **not** implemented in v1. `query` stays synchronous; the
  per-day partition keeps results under 30k rows, well inside the limits.

### 5.4 `finra/cdn.py`

```python
@dataclass(frozen=True)
class DailyFile:
    family: str; trade_date: date; rows: tuple[DailyRow, ...]; sha256: str; declared_count: int

class DailyFileClient:
    def __init__(self, session, *, base_url=CDN_BASE, timeout=60.0, sleep=None, log=None): ...
    def fetch_day(self, family: str, trade_date: date) -> DailyFile | None
```

- "Absent" is defined narrowly: HTTP 403 whose body is the S3 `AccessDenied`
  XML, or HTTP 404. Only then does `fetch_day` return `None`. A 403 with any
  other body (a WAF block, a changed CDN) raises `CdnError`, so a block can
  never be recorded as a string of holidays. The caller decides what absence
  means (section 7).
- The parser is **strict**: the header must equal the expected six columns in
  order, every data row must have six fields, volumes must parse as
  non-negative integers, `Date` must equal the requested date, the trailer
  count must equal the number of data rows, and short must not exceed total.
  Any violation raises `DailyFileFormatError` with line number and reason. A
  changed layout therefore fails loudly instead of being misread.
- `sha256` of the raw bytes is recorded in state to detect restatements.

## 6. The dataset

### 6.1 Registry (`finra/registry.py`)

```python
@dataclass(frozen=True)
class DatasetSpec:
    name: str                 # "short_volume"
    api_group: str            # "otcMarket"
    api_name: str             # "regShoDaily"
    cdn_family: str | None    # "CNMS"
    first_date: date          # 2018-08-01
    key_cols: tuple[str, ...] # ("date", "symbol")
    columns: Mapping[str, str]  # name -> "key" | "date" | "value" | "meta", like eodhd/schema.py

DATASETS = {"short_volume": DatasetSpec(...)}
```

### 6.2 Canonical schema for `short_volume`

| column | type | from CDN | from API |
|---|---|---|---|
| `date` | date | `Date` | `tradeReportDate` |
| `symbol` | string, raw SIP spelling | `Symbol` | `securitiesInformationProcessorSymbolIdentifier` |
| `short_volume` | float64, shares as published | `ShortVolume` | sum of `shortParQuantity` over NMS facilities |
| `short_exempt_volume` | float64 | `ShortExemptVolume` | same, `shortExemptParQuantity` |
| `total_volume` | float64 | `TotalVolume` | same, `totalParQuantity` |
| `facilities` | string, sorted codes joined by comma | `Market` normalised | distinct `marketCode` |
| `source` | string | `"cdn"` | `"api"` |

One row per `(date, symbol)`. The pinned pyarrow schema is written with every
file, as `news/articles` does. The API path sums per-facility rows to the
consolidated row. A day is only ever stored from **one** source, recorded in
`source`, and a mixed day is a `qc` finding, not a silent merge. The content
digest used for restatement detection ignores `source`, so the same rows from
either transport compare equal.

### 6.3 Transport policy

The CDN consolidated file is the **default transport** for `short_volume`: it
is public, covers the full history, carries integer figures that match
FINRA's own publication, and is one row per symbol. The API is used for
`short_volume` only when `--transport api` is given (cross-checking, or if the
CDN ever disappears). The API is the only transport for the entitled datasets
that come later, which is why the access layer is built first and exercised by
`finra probe` and `finra status` from day one.

### 6.4 Symbol mapping to EODHD

Stored raw. The view adds:

- `security_kind`: `common` (no marker), `preferred` (`p` followed by a
  series letter), `warrant` (`/WS`), `unit` (`/U`), `right` (`/R`), `other`.
- `eodhd_code`: for `common`, `symbol` with `/` replaced by `-`
  (`BRK/B` becomes `BRK-B`); NULL otherwise.

This rule is checked by a unit test on the real spellings above and by a
`qc` check that reports the share of `us_common` tickers with no FINRA row
on the latest day.

## 7. Store, state and planning

### 7.1 Layout

```
<finra root>/                        default: <eodhd root>.parent / "finra"
  short_volume/
    daily/YYYY-MM-DD.parquet         one file per trade date, zstd, pinned schema
    short_volume_fetch_state.csv     one row per attempted trade date
  STATUS.json, STATUS.md             written by `finra status --write`
```

One file per day mirrors `news/articles` and makes presence, idempotency and
restatement trivial: re-fetching a day replaces one file atomically. About
2,050 files for the full history, well within what the explorer and the sync
engine already handle for news. DuckDB prunes files by the parquet `date`
column statistics, so a `WHERE date BETWEEN` query on the view touches only
the footers of the other files.

### 7.2 State sidecar columns

`date, status, source, rows, short_sum, total_sum, sha256, fetched_at, detail`

`status` is one of exactly three values:

- `ok`: stored. A re-fetch inside the overlap window that yields a different
  `sha256` keeps `ok`, replaces the file, and records the previous sums and
  the restatement date in `detail`; the run report lists the day as restated.
- `absent`: a publishable weekday with no file, which is a market holiday, a
  late FINRA publication, or a FINRA gap. Re-probed while the day is inside
  the overlap window, final afterwards unless `--retry-absent`.
- `error`: transport or format failure; retried by every following run.

A day that is not yet publishable is never requested and gets no row, so
there is no "pending" state to keep consistent.

**Absent streak guard.** US markets have not closed on more than two
consecutive weekdays since 2012. If a run observes a third consecutive
`absent` weekday, it records those days as `error` with detail
`absent streak: suspect block or outage`, stops, and exits 1. This is what
keeps a CDN outage or an IP block from being filed as holidays.

### 7.3 Publication clock (`finra/calendar.py`)

`now_et()` uses `zoneinfo.ZoneInfo("America/New_York")` (the `tzdata` package is
present through pandas). A date is `publishable` once it is a weekday and
`now_et()` is past 18:00 on that date. The planner never requests a date that
is not yet publishable, so a run at 3 pm ET does not burn a request on today
and does not mislabel it.

**Job timing (decided 2026-10-01):** the finra fetch runs as its own
scheduled job at **18:30 ET**, which is 23:30 London on the Windows box.
London is five hours ahead of New York except for the two short windows each
year when the US has switched DST and the UK has not, when it is four hours
ahead; 23:30 London is therefore always 18:30 or 19:30 ET, never earlier
than the 18:00 deadline. The existing eodhd job at 05:00 London (00:00 or
01:00 ET) is left untouched.

**Job as defined (2026-10-01):** `finra-short-volume-daily`, daily 23:30
system-local, logged-off (S4U) with wake-to-run like the eodhd job, timeout
3600 s, steps `finra fetch --limit-days 10 --run` then `finra qc`. Until the
backfill has run, each night stores the newest day plus nine days of history
behind it. The desired state is committed (generation 1); Windows refuses to
register an S4U task from a non-elevated terminal, so the task is installed
with one elevated command:

```powershell
.venv\Scripts\python.exe datacli.py schedule --json reconcile finra-short-volume-daily
.venv\Scripts\python.exe datacli.py schedule --json doctor finra-short-volume-daily
```

### 7.4 Planner

`plan_days(from_date, to_date, state, overlap_days=3, retry_absent=False,
full_refresh=False, now=...) -> list[date]`, newest first like the news
planner: a date is included when it is a weekday, publishable, and either has
no `ok` row, or is `error`, or lies inside the trailing overlap window, the
last `overlap_days` weekdays ending at the range end inclusive, where `ok`
days are re-checked for restatements and `absent` days for late publication
(`0` means no re-check), or is `absent` with `retry_absent`.
`--limit-days N` caps the list from the newest end, so a routine run stays
bounded while a backfill fills in behind it.

Defaults: `--from` is the dataset's `first_date`, `--to` is the latest
publishable date, no day limit. A fresh root therefore plans the whole
history and the dry run says so; the scheduled step passes `--limit-days 10`.

The API partitions list is **not** required by the planner (the planner must
work offline and for pre-window history). `status` fetches it when reachable
to report "published through" against "stored through" as separate facts.

### 7.5 Refresh report

```python
@dataclass(frozen=True)
class RefreshReport:
    dataset: str; run: bool; planned: tuple[date, ...]
    stored: tuple[date, ...]; pending: tuple[date, ...]; absent: tuple[date, ...]
    restated: tuple[date, ...]; failed: tuple[tuple[date, str], ...]
    rows: int; root: Path
    @property
    def ok(self) -> bool: return not self.failed
```

Each day is written and its state row flushed before the next day is fetched,
so an interrupted backfill resumes where it stopped.

## 8. CLI surface (`finra/cli.py`)

Same command-table pattern as `macro/cli.py` (`Command`, `Flag`, `_parse`,
did-you-mean, exit 2 on usage errors).

| command | effect |
|---|---|
| `finra list` | datasets in the registry, transport, first date, entitlement needed |
| `finra status [--live] [--write] [--json]` | offline by default: per dataset, stored through, days stored, absent and error counts, credential presence (masked). `--live` adds "published through" from the API partitions with a short timeout and reports "unreachable" rather than failing |
| `finra fetch [--dataset short_volume] [--from D] [--to D] [--limit-days N] [--overlap-days N] [--retry-absent] [--transport cdn or api] [--full] [--run]` | dry run prints the plan and the first and last dates; `--run` fetches under `direct_mutation_lock("finra", "fetch", ...)` |
| `finra qc [--dataset short_volume]` | duplicates, short greater than total, gaps against weekdays not marked absent, mixed sources, EODHD mapping coverage |
| `finra probe metadata or partitions <dataset>` | live read-only calls against the API; the way to exercise credentials |

Exit codes: 0 success or clean dry run, 1 any failed day or a qc finding of
severity error, 2 usage.

## 9. Integration points

1. `datacli.py`: `FinraPlugin(SourcePlugin)` with `COMMANDS = ("list",
   "status", "fetch", "qc", "probe")` and `refresh` accepted as an alias of
   `fetch` (the eodhd plugin maps the other way), added to `SOURCES`; a
   `do_finra` shortcut like `do_macro`; `do_probe` and `do_qc` already
   dispatch per source.
   `finra/config.py` reads `[finra]` through `eodhd/config.section()` rather
   than adding a third copy of the TOML loader (macro carries the second).
2. `finra/views.py: register(con)` called best-effort from
   `eodhd/explore_eodhd.connect()` and `lab/data.py: connect()` next to
   `macro_views.register`; `schema_text()` appends `schema_snippet()` so the
   lab and the MCP `describe_schema` see `finra_short_volume`.
3. `scheduler/commands.py`: `Capability("finra", "fetch", "OPTIONAL",
   mutation=True, network=True, requires_run=True)` and `("finra", "status",
   "OPTIONAL", False, False)`; option allowlist in `_validate_arguments`;
   `finra_data_root` binding and exclusive `ResourceClaim` in `_resolve`;
   `preflight` emits an informational finding when credentials are absent
   (the default transport needs none); `execute` runs
   `[interpreter, "-m", "finra.cli", verb, *argv]` with `DATACLI_FINRA_ROOT`
   passed like `DATACLI_MACRO_ROOT`. `scheduler/cli.py` completions and help;
   `tests/test_scheduler.py` capability assertion updated.
4. `datacli.example.toml`: a `[finra]` section with `data_root` commented out
   and a note that credentials are environment-only.
5. `docs/REFERENCE.md`: a `finra` section and the view columns.
6. Sync (decided, built in phase 4): `sync status | push | reconcile` now
   run over **units**: the eodhd root always, plus the macro and finra roots
   when they exist, each with its own manifest under its own `.sync/` and
   its own remote folder next to `remote_root` (`datacli/finra`; for the
   `local` backend a sibling of `local_dest`). The scheduler's no-op sentinel
   line is printed once, only when every unit was current.
   **Deliberately not changed:** the `sync push` command's scheduler
   bindings and resource claims. The runner re-resolves a job's bindings at
   run time and refuses to run when they differ from the stored definition,
   so adding finra and macro root claims to `sync push` would have stopped
   the live eodhd job (generation 4) until it was re-enabled. The finra root
   is therefore not locked by a scheduled `sync push`; the day files are
   written atomically, so an overlapping `finra fetch` can only make the
   push see a day's old or new file, never a partial one. Revisit when the
   eodhd job is next re-created.

## 10. Testing plan

All offline; fakes follow the hand-written `_Session` and `_Response` style.

- `tests/test_eodhd_http.py`: existing cases unchanged; POST passes `json`
  and `headers`; retries identical for POST.
- `tests/test_finra_auth.py`: `from_env` three cases; token cached; refresh
  after expiry minus skew; `invalidate`; 401 on FIP raises `AuthError`;
  `repr` masks the secret; the secret never appears in any log record.
- `tests/test_finra_api.py`: anonymous metadata and partitions; paging across
  three pages using `record-total`; `record-max-limit` lower than page size;
  204 empty; 401 then refresh then success; 401 twice raises; 403 anonymous
  raises `EntitlementError`; offset cap raises `TooManyRows`; a CSV body
  without a JSON content type is rejected.
- `tests/test_finra_cdn.py`: fixture file with header, 5 rows, trailer;
  parses; each strict check fails on a crafted variant (bad header, wrong
  date, trailer mismatch, short greater than total, non-integer); 403 returns
  `None`; sha256 stable.
- `tests/test_finra_short_volume.py`: normalisation from both transports gives
  identical frames on a matching sample; API fractional rounding; a day stays
  single-source.
- `tests/test_finra_calendar.py`: publishable before and after 18:00 ET,
  weekends, DST edges.
- `tests/test_finra_planner.py`: newest first, overlap, `limit_days`, error
  retry, absent not retried unless asked, pending excluded.
- `tests/test_finra_store.py`: write a day atomically, replace a day, read a
  range, state upsert, `tmp_path` roots.
- `tests/test_finra_cli.py`: dry run touches nothing, `--run` writes, exit
  codes, `status --json` shape, did-you-mean.
- `tests/test_finra_views.py`: view columns, `security_kind` and `eodhd_code`
  on `BRK/B`, `ABRpD`, `AACT/WS`, `AACT/U`.
- `tests/test_scheduler.py`: capability set and `finra fetch` validation.

Live verification is user-run only: `finra probe metadata short_volume`, then
`finra fetch --from 2026-09-25 --run`, then `finra qc`.

## 11. Build order

| phase | deliverable | done when |
|---|---|---|
| 0 | `request_with_retry` in `eodhd/_http.py` | **done 2026-10-01**: old tests pass, new POST tests pass |
| 1 | `finra/auth.py`, `finra/api.py`, `finra/config.py`, `finra/registry.py`, `finra/cli.py` (`list`, `status [--live]`, `probe auth/metadata/partitions`), `FinraPlugin` + `finra` shortcut in the shell, `[finra]` in the example config | **done 2026-10-01**: probes verified anonymously and authenticated (token valid about 12 h; `consolidatedShortInterest` partitions listed with credentials) |
| 2 | `finra/cdn.py`, `calendar.py`, `store.py`, `short_volume.py`, `fetch` + `status` + `qc` | **done 2026-10-01**: a live 5-day fetch into a temporary root passed `qc`; 100+ offline tests over parser, clock, store, planner, refresh guards and CLI |
| 3 | `views.py`, explorer and lab registration, example config, REFERENCE docs | **done 2026-10-01**: `finra_short_volume` and `finra_short_volume_state` register on the explorer, lab and MCP connections; the SQL symbol mapping is tested against the Python one; a join to `prices` on `eodhd_code` verified live |
| 4 | scheduler capability, daily job step, sync root decision applied | **done 2026-10-01**: `finra fetch / status / qc` are allowlisted with an option allowlist, a `finra_data_root` binding (exclusive for fetch) and a credentials finding only for `--transport api`; `sync` covers the eodhd, macro and finra roots as separate units; the 23:30 London job is defined (section 7.3) |
| 5 | full backfill 2018-08-01 to date, then the materiality by short-ratio evaluation | **done 2026-10-01**: backfill run from the public files (one transient Windows rename error, fixed with a bounded retry in `_atomic`); evaluation in section 14: no positioning effect found |

Full test suite before each handoff.

## 12. Self-review: what-if scenarios and the decisions they forced

1. **FINRA adds a column to the daily file.** The strict header check fails
   the day with `error`; nothing is misparsed; the fix is one registry change.
2. **CDN returns 5xx for an hour.** Bounded retries, then `error`; the next
   run retries automatically.
3. **The job runs at 5 pm ET.** Today is not publishable, so it is not
   requested and not mislabelled; yesterday is fetched. No false `absent`.
4. **The job runs at 2 am local the next day.** Yesterday's file is up (6 pm
   ET deadline) and is fetched normally.
5. **Backfill is killed at day 1,200.** Per-day flush; a rerun plans the rest.
6. **Token expires mid-pagination.** One 401, invalidate, refresh, retry that
   page. A second 401 raises.
7. **Authenticated API shows more than one year.** Irrelevant to the default
   transport; the API transport works on whatever window it sees.
8. **API and CDN disagree for a day.** Days are single-source; `qc` can
   cross-check later; v1 does not merge sources.
9. **A preferred or warrant has no EODHD counterpart.** Stored anyway, the
   view maps to NULL; `qc` reports mapping coverage over `us_common` only.
10. **A restated file.** The overlap re-fetch sees a new sha256, stores the
    new file, marks `restated`, keeps the old sums in `detail`. Restatements
    older than the overlap window are missed unless `--full` is run; accepted.
11. **Disk and file count.** About 2,050 day files, roughly 100 KB each; news
    already works at this shape.
12. **Credentials set but wrong.** FIP returns 4xx, `AuthError`, exit 1.
    The run does not silently downgrade to anonymous, because a
    misconfiguration must be visible.
13. **Only one of the two env vars set.** `CredentialsError` naming the
    missing one, before any network call.
14. **Secret in a traceback.** The secret lives only in the `Credentials`
    object with a masked `repr` and in the Basic header built at call time;
    `requests` exceptions carry URLs, and FINRA URLs never hold the secret.
    The scheduler `Redactor` additionally masks any env value whose name
    contains KEY.
15. **Second dataset, consolidated short interest.** A new `DatasetSpec` with
    `cdn_family=None`, a provider that uses `QueryApiClient.query` with a
    `date_range` filter on the settlement date, the same store with a
    different key. No change to the access layer.
16. **Someone runs `finra fetch --run` while the scheduler runs it.**
    `direct_mutation_lock("finra", ...)` and the exclusive resource claim on
    the finra root serialise them.
17. **FINRA publishes late, at 8 pm ET, and the job ran at 6:30 pm.** The
    day is recorded `absent`, stays inside the overlap window, is re-probed
    by the next runs and becomes `ok`. Without the overlap re-probe this day
    would have been lost for good; this is why `absent` is not final inside
    the window.
18. **The CDN starts returning 403 to this IP.** The body is not the S3
    `AccessDenied` XML, so each day is `error`, not `absent`; and even if it
    were, the streak guard stops the run at the third consecutive absence.
    `status` shows the error count the next morning.
19. **A file has a different layout.** Checked: the 2018-08-01 file is
    identical in header and trailer to today's. The strict parser proved its
    worth on the very first live fetch: it refused five days of fractional
    volumes with the exact line and rule, instead of storing truncated
    integers. The schema was widened to float64 and the design corrected
    (section 2), which is the intended failure mode.
20. **`status` is run by the scheduler with no network.** The default is
    offline; the capability is declared `network=False`; `--live` is not in
    the scheduler's option allowlist.

Alternatives rejected:

- *API as the default transport.* Rejected: one-year window, 28k per-facility
  rows and six requests per day where one public file gives the consolidated
  published figure.
- *Monthly parquet files.* Fewer files, but merge-on-write and non-trivial
  restatement; day files match the news precedent and are simpler to reason
  about.
- *FINRA data under the eodhd root for free sync.* Rejected: it would make
  `discover_lane_dirs` special-case a foreign directory and muddle the eodhd
  snapshot; multi-root sync is the right fix and helps macro too.
- *A generic `Source` abstraction in `datacli.py`.* Tempting, but a refactor
  of the shell is a separate change; the plugin pattern is followed as is.

## 13. Owner decisions (recorded 2026-10-01)

1. **Sync coverage.** Agreed: extend `sync` to multiple roots (eodhd, macro,
   finra) in phase 4, as a separate small change.
2. **Job timing.** Decided: a separate finra job at 18:30 ET (23:30 London);
   the eodhd job stays at 05:00 London. See 7.3.
3. **OTC (`FORF`).** Agreed: excluded from v1.
4. **`--transport api` for `short_volume` in v1.** Agreed: kept.

No open questions remain for phases 0 to 3. The credentials are present in
the Windows user environment and were verified against FIP and the entitled
datasets (section 2).

## 15. Dataset 2: weekly ATS / OTC flow (`weekly_flow`), decided 2026-10-01

**Owner decision.** After phase 5 the owner asked for "long flow data" and
chose FINRA's weekly ATS/OTC transparency data. It is the second dataset on
the access layer and the first entitled, API-only one.

**Verified facts (probed with credentials, 2026-10-01).**

- Dataset `otcMarket/weeklySummary`, partitioned by `weekStartDate` (the
  week's Monday) **and** `tierIdentifier`. 249 week partitions from
  2021-12-06 to 2026-09-07.
- Tiers seen: `T1` (NMS Tier 1: S&P 500, Russell 1000, selected ETPs), `T2`
  (other NMS), `OTCE` (OTC equities, non-NMS), `NA` (six market-wide rows).
  A full week holds about 72k `T1`, 168k `T2`, 24k `OTCE` rows.
- Row kinds (`summaryTypeCode`): `ATS_W_SMBL_FIRM` and `OTC_W_SMBL_FIRM`
  (per symbol per ATS or OTC firm, the bulk), `ATS_W_SMBL` and `OTC_W_SMBL`
  (per symbol totals, about 9k and 10k rows per tier-week), `ATS_W_FIRM` and
  `OTC_W_FIRM` (per firm totals), `ATS_W_VOL_STATS` (market-wide).
- Fields: `issueSymbolIdentifier`, `issueName`, `MPID`, `firmCRDNumber`,
  `marketParticipantName`, `summaryStartDate`, `totalWeeklyTradeCount`,
  `totalWeeklyShareQuantity` (fractional on 13 percent of rows),
  `totalNotionalSum`, `productTypeCode`, `initialPublishedDate`,
  `lastUpdateDate`, `lastReportedDate`.
- **Publication lag is the point-in-time fact.** `T1` for a week is first
  published on the Monday 3 weeks after the week's Monday (2026-09-07 was
  published 2026-09-28); `T2` and `OTCE` on the Monday 5 weeks after
  (2026-08-24 published 2026-09-28); the `NA` rows a week later still.
  `initialPublishedDate` carries this per row, and `lastUpdateDate` moves
  when a firm restates (3 of 167,600 rows in the probe).
- One `T2` partition pulls in about 47 s over 34 pages; a full week over all
  tiers is roughly 75 s, so the whole history is about 5 hours of
  synchronous paging. Async mode (100k rows per request) would cut the
  request count by two thirds and is the optimisation to add if the backfill
  is rerun often.

**Design.**

- Registry: `weekly_flow` with `tiers=("T1", "T2")`. `OTCE` is excluded, as
  OTC was for short volume; it is one tuple entry to add. The `NA` rows are
  dropped at ingest.
- Unit of work and of storage: one `(week_start, tier)` partition, fetched
  with two `compareFilters` on the partition fields, one parquet file
  `weekly_flow/weekly/YYYY-MM-DD_T1.parquet`. The store generalises the day
  store with an optional `part` component in the file name and the state
  sidecar (empty for short volume, so nothing stored changes).
- Canonical schema, one row per published record: `week_start`, `tier`,
  `summary_type`, `symbol` (raw), `issue_name`, `mpid`, `firm_crd`,
  `participant_name`, `product_type`, `trade_count` (int64),
  `share_quantity` (float64), `notional` (float64), `published_at`,
  `updated_at`, `last_reported_at` (dates), `source`.
- Planner: a `(week, tier)` is **due** once today is on or after its first
  publication Monday (week Monday plus 21 or 35 days), and is re-fetched
  while inside a trailing overlap of 2 weeks so late restatements are
  caught; `absent` (a 204 for a due partition) is re-probed inside the
  overlap as for short volume. Restatement is a changed content digest, as
  before.
- Views: `finra_weekly_flow` (every row with `eodhd_code`) and
  `finra_weekly_flow_symbol`, the per-symbol totals pivoted to
  `ats_shares`, `ats_trades`, `ats_notional`, `otc_shares`, `otc_trades`,
  `otc_notional` with `published_at`, which is the column to ASOF-join on
  for anything point-in-time.
- Scheduler and CLI: `--dataset weekly_flow` on `fetch`, `status` and `qc`
  become per dataset; the nightly job gains a `finra fetch --dataset
  weekly_flow --run` step once the backfill exists (new weeks are due on
  Mondays, so a nightly run is mostly a no-op).

**Built 2026-10-01 (phase 6).** `finra/weekly_flow.py`, the store's optional
`part`, the two views, CLI and scheduler admission, 10 new tests. The first
live runs found the row key wrong, not the data, twice: OTC firm rows are
identified by `firmCRDNumber` and carry no `MPID`, and a symbol that changes
listing tape mid-week (FITB in June 2026) gets one per-symbol row per
`productTypeCode` (CTS and UTP), and a ticker reassigned to another issuer
inside the week (RNA from Avidity to Atrium, 2026-02) gets one row per
`issueName`. FINRA's identity is the issue, so the key is `(summary_type,
symbol, mpid, firm_crd, product_type, issue_name)`; the per-symbol view sums
over it, so totals stay whole, with the known caveat that a reused ticker's
week mixes two issuers. The first full backfill rejected 131 of 496
partitions, the second pass 32; each rerun picks the rejected ones up as
`error` retries, and the final key stores every partition. Two
live partitions (2026-08-31/T1, 2026-09-07/T1, 144,150 rows) passed `qc`
and the per-symbol view reproduced AAPL's weekly ATS and OTC shares with
`published_at` three weeks after the week. The full backfill (494
partitions) was started the same evening. A second generation of the
nightly job with a `finra fetch --dataset weekly_flow --limit-days 20 --run`
step is drafted; enabling it registers the task and needs the elevated
terminal.

## 14. Phase 5: the evaluation (2026-10-01)

**Question.** Does short-sale positioning change how big the move after
material news is? The scoring result to build on: the model's materiality
judgement orders the size of the next session's market-adjusted move
(per-day rank correlation about 0.10, t about 8.5); direction is dead.

**Data.** The `event@5` scored panel (8,073 `(date, symbol)` rows, 64
trading days, 2026-05-21 to 2026-08-15) joined to point-in-time FINRA
features on the trading day: 7,663 rows over 60 days, 95 percent of the
panel. The feature is the trailing short ratio (`short_volume /
total_volume`) over the symbol's last 5 FINRA days **ending the day before**
the panel's trading day (`sr5_known`), plus 1-day, 20-day and abnormal
(5-day minus 60-day) variants. Code: `finra/panel.py`,
`scoring/positioning_eval.py`, `scripts/finra_materiality_eval.py`.

**Timeline (lookahead audit).** Article lands in session T; the decision
point is close(T); `f1` runs close(T) to close(T+1). FINRA publishes day T
at 18:00 ET, after that close, so the honest feature uses FINRA days up to
T-1 (`*_known`). Day-T variants (`*_t`) are reported only as "available for
a next-open entry". All cross-sectional ranks are within the trading day; no
scaler or statistic is fitted on the full sample; the market adjustment is
the exchange median of the same day. The lookahead linter flags five
`.rank()` calls; each is inside a per-day group.

**Result: no effect found.** Sixteen tests in the family; Bonferroni at 5
percent needs p below 0.0031. The materiality baseline reproduces on the
matched rows (0.095, t 8.3). Nothing involving positioning clears the bar:

| test (horizon f1_ex, 60 days) | statistic |
|---|---|
| short ratio alone vs size of move, 5-day | corr -0.024, t -1.7 |
| short ratio alone, 20-day | corr -0.023, t -1.8 |
| abnormal short ratio alone | corr 0.011, t 0.7 |
| materiality's correlation, high minus low short-ratio tercile | 0.027, t 0.8 |
| material names (level 2+), mean size of move, high minus low tercile | -27 bps, t -1.3 |
| Fama-MacBeth interaction term (rank materiality x rank short ratio) | coef 0.054, t 1.0 |

At the 5-day horizon the picture is the same: positioning alone is weakly
negative (t about -2, not significant after correction), the interaction is
flat (t -0.07 and t 0.6).

**Reading.** The sign is informative. Names with a high off-exchange short
ratio move slightly *less*, not more: in this feed the ratio is dominated by
market-maker and liquidity-provider short selling, so a high ratio marks a
liquid, heavily intermediated name, not a crowded short. The one striking
cell, top materiality in the high tercile at 596 bps, rests on 15 rows
against 16 and 23 in the other terciles and does not survive the per-day
spread test. As designed, the next-session effect of material news is not
amplified by this positioning measure; the honest number to carry forward is
the unchanged materiality correlation.

**What would be worth testing next**, in order: the bi-monthly
consolidated short interest (days to cover is the positioning measure the
hypothesis actually names, and the credentials are entitled to it); a
longer scored window, since 60 days bounds the power here; and the short
ratio's own change around the news day as an *outcome* rather than a
conditioner.
