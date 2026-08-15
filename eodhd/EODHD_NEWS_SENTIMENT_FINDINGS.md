# EODHD News & Sentiment Feeds — Findings and Lane Design

**Status:** FINDINGS-COMPLETE / SUBSTRATE-BUILT / BACKFILL-COMPLETE (2021-01-01..2026-08-15)  
**Created:** 2026-08-15  
**Purpose:** what the current EODHD subscription exposes for news / sentiment, measured
live, and the design of the `news` lane that lands it in the data root for downstream
ML processing.  
**Provenance:** live probes on 2026-08-15 against `https://eodhd.com/api` with the
configured key (`/user` reported `dailyRateLimit=100000`, `subscriptionMode=paid`).

## 1. Scope

Before this note the repo consumed only prices, dividends, splits, fundamentals, the
bulk EOD endpoint, the earnings calendar and macro indicators. There was no news or
sentiment ingestion at all — the lab personas even state "you have NO news data".

Three vendor endpoints were probed: `/news`, `/sentiments`, `/tweets-sentiments`.

## 2. What the subscription exposes (verified)

### 2.1 `/news` — full-text article feed with per-article sentiment

- **Fields per article:** `date` (ISO-8601, UTC, second precision), `title`, `content`
  (full text — median `~4,000` chars, max `~45,000`, `0/2,837` empty in a whole-day
  sample), `link`, `symbols` (multi-ticker, multi-exchange list; median `10`, max
  `50`), `tags` (topic labels), `sentiment {polarity, neg, neu, pos}`.
- **Query modes:** `s=TICKER.EXCH` (one symbol), `t=TAG` (one tag), or **no filter**
  (global feed). All accept `from`/`to` (day-inclusive), `limit` (`≤ 1000`) and
  `offset` (tested to `20,000` without hitting a ceiling).
- **Cost:** flat **5 API units per call regardless of `limit`** — always pull `1000`.
- **Volume:** global feed ≈ `2,700`–`3,900` articles/day since 2021 (`~700`/day in
  2019–2020, near zero before). AAPL ≈ `35`–`55`/day; mid-caps a few/day; small caps a
  few/month.
- **History:** effectively **2021 → today**. Yearly counts for AAPL: 2016 `1`, 2017
  `3`, 2018 `7`, 2019 `41`, 2020 `303`, 2021+ dense (`> 1000`/yr).
- **Sources:** `~86%` finance.yahoo.com, then nasdaq.com, seekingalpha.com, cnbc.com,
  globenewswire.com — aggregator/press-release wire, not primary newswires.
- **Ordering:** newest first; `offset` pages backwards in time.

### 2.2 `/sentiments` — daily per-symbol rollup

- Returns `{symbol: [{date, count, normalized}]}`; multi-symbol per call
  (`s=A.US,B.US,BTC-USD.CC`), covers equities, indices, forex, crypto.
- **It is a rollup of `/news`:** for `AAPL.US` on 2026-08-13 `count=34` and `/news`
  returned exactly `34` articles. History nominally back to 2012 but the pre-2016 rows
  are `count=1` artefacts.
- **Cost: 5 units per symbol per call**, so a `3,000`-ticker universe is `15,000`
  units per pull. Cheaper and more flexible to **derive it locally from the article
  corpus** (and we can then use our own scoring model instead of the vendor's).

### 2.3 `/tweets-sentiments` — dead

HTTP 200 with an empty list for `AAPL.US` in Aug-2025. Ignore.

### 2.4 Backfill result (run 2026-08-15, 12:50–15:53 local)

One uncapped `fetch_eodhd_news.py` run, newest-first, single-threaded:

| | |
|---|---|
| Days crawled | `2,053` (2021-01-01 → 2026-08-15), all `ok`, `0` failures, `0` page-cap hits |
| Pages / API units | `5,511` pages ⇒ `≈ 27,600` units |
| Rows / unique articles | `4,457,038` / `4,457,020` (18 cross-midnight re-publications) |
| On disk | `6.96 GB` in `2,053` daily partitions (`≈ 3.4 MB/day`, `≈ 1.25 GB/yr`) |
| Mean text | `≈ 4,700` chars/article over the whole corpus (recent years run `≈ 7,000`) |
| Empty content | `6,275` (`0.14%`) |
| No symbol tags | `621,458` (`14%`) — untargetable by ticker without NER |
| No topic tags | `1,484,312` (`33%`) |
| Mean vendor polarity | `0.71` |

Articles per day by year — note the **2024 dip**, a vendor-side coverage change:

| year | rows | per day |
|---|---|---|
| 2021 | `883,019` | `2,419` |
| 2022 | `809,921` | `2,219` |
| 2023 | `785,482` | `2,152` |
| 2024 | `453,430` | **`1,239`** |
| 2025 | `958,829` | `2,627` |
| 2026 (to 08-15) | `566,357` | `2,495` |

Sources all-time: finance.yahoo.com `65%`, globenewswire.com `21%` (press releases),
then fxstreet, reuters, nasdaq, seekingalpha, u.today, coindesk. Most-tagged symbols:
`GSPC.INDX`, `NVDA.US`, `AAPL.US`, `USDUSD.FOREX` (a junk tag — filter it), `MSFT.US`,
`TSLA.US`, `DJI.INDX`.

## 3. Cost / size envelope

| Item | Measured / derived |
|---|---|
| Daily quota | `100,000` units (`+397,130` extra) |
| One `/news` page | 5 units, `≤ 1000` articles |
| One global day | `3`–`4` pages ⇒ `15`–`20` units |
| Full backfill 2021-01-01 → today (2,053 days) | estimated `≈ 8,000` calls; **measured `5,511` pages ⇒ `≈ 27,600` units** (§2.4) — under a third of one day's quota |
| Text per article (title + content) | `≈ 7 KB` mean in 2026, `≈ 4.7 KB` over the whole corpus |
| On disk (zstd parquet, pinned schema) | measured **`6.96 GB` for 2021→2026-08-15** (`≈ 3.4 MB/day` avg, `≈ 7 MB/day` recently) |
| Incremental refresh | re-crawl last 2 days ⇒ `< 50` units, `≈ 15 MB` rewritten |
| Backfill wall-clock | `≈ 3 h` single-threaded (`≈ 5 s/day`, download-bound) |

The disk figure is the reason the corpus is partitioned **per day** (§5.1): a
month would be `≈ 100–200 MB` on disk / up to `≈ 600 MB` of text, and rewriting it
on every flush during a backfill would push peak RAM past a couple of GB.

## 4. Gotchas that matter for ML

- **Vendor sentiment is VADER-style** (`neg+neu+pos = 1`, `polarity` = compound) and
  saturated: on `1,000` AAPL articles median polarity `0.98`, mean `0.72`. Treat as a
  baseline feature only; the asset is the full text (own FinBERT / LLM labelling /
  embeddings / event extraction).
- **Symbol tagging is exchange-specific and US-biased.** 30-day counts: `SAP.US 248`
  vs `SAP.XETRA 1`; `HSBC.US 305` vs `HSBA.LSE 6`; `ASML.US 515` vs `ASML.AS 127`;
  `SIE.XETRA 28` vs `SIEGY.US 46`. Any UK/EU use needs an issuer-level mapping (ISIN /
  name → every EODHD listing) rather than filtering on the primary ticker.
- **Tag vocabulary is noisy:** `848` distinct tags in `1,000` articles, near-duplicates
  (`PRICE-TARGET` / `PRICE TARGET`, `EARNINGS` / `EARNINGS REPORT`), `~7%` untagged.
- **Tagging bursts:** AAPL had `1,097` articles on 2025-08-15 (~30% of the whole
  global feed that day). Counts need outlier handling before use as a signal.
- **Volume is not stationary:** 2024 carries roughly half the daily volume of the
  surrounding years (§2.4). Any count-based feature must be normalised against the
  day's global total, not used raw.
- **`14%` of articles have no symbol tags** — reachable only via text (NER / issuer
  matching), and `USDUSD.FOREX` is a junk symbol tag to filter.
- **Duplicates:** the same article appears under every tagged symbol; `link` is the
  practical identity (`997/1000` unique in a per-symbol pull, `2,808/2,837` in a
  global-day pull). Dedup on a hash of `link`.
- **Re-publications:** `~0.1%` of links come back a second time with a *later*
  timestamp (nasdaq.com / Barchart market wraps updated 2–3 h on). Within a day this
  is deduped away (last write wins); across midnight UTC both versions are kept, one
  per daily partition. For a one-row-per-article view take the latest
  `published_at` per `article_id`
  (`QUALIFY row_number() OVER (PARTITION BY article_id ORDER BY published_at DESC) = 1`).
- **Pagination of the current day is not stable** (new articles shift the offsets), so
  the crawler must overlap: re-fetch the trailing days on every run.

## 5. Lane design (`news`)

**Principle:** one global crawl by day, article-level storage, everything symbol-level
derived locally. Per-ticker `/news` and `/sentiments` pulls are avoided — they cost
more, duplicate rows, and lock us into the vendor's tagging.

### 5.1 On-disk layout

```
data/raw/eodhd/news/
  articles/YYYY-MM-DD.parquet  # article-level, one partition per publication day
  news_fetch_state.csv         # one row per crawled UTC day
```

One file per day (`≈ 3k` rows / `≈ 7 MB`) means a flush only *writes* new days
and a re-crawl rewrites a single small file; nothing is ever re-read and merged
at scale. Readers see one dataset through the `articles/*.parquet` glob (DuckDB,
pyarrow), and every partition is written under a pinned Arrow schema so the glob
never hits a type mismatch (an all-empty `tags` day would otherwise be inferred
as `list<null>`). `≈ 2,000` files for the full history is well within what those
readers handle; the sync engine walks the tree as-is.

### 5.2 Article schema (`articles/*.parquet`)

| column | type | notes |
|---|---|---|
| `article_id` | str | `sha1(link)[:16]` — dedup key, unique *within* a partition |
| `date` | date | UTC publication day (from `published_at`); partition + join key |
| `published_at` | timestamp (UTC) | vendor `date` |
| `title` | str | |
| `content` | str | full text |
| `link` | str | |
| `source` | str | hostname of `link` |
| `symbols` | list\<str\> | vendor tags, `TICKER.EXCH` |
| `tags` | list\<str\> | vendor topic tags, upper-case as delivered |
| `polarity`, `neg`, `neu`, `pos` | float | vendor sentiment |
| `fetched_at` | timestamp (UTC) | crawl wall-clock |

Symbol- and tag-level views are `unnest`s in DuckDB; no second table in the substrate.

### 5.3 Fetch state (`news_fetch_state.csv`, keyed by `date`)

`date, status, pages, articles, unique_articles, min_published, max_published,
fetched_at, detail`. `status ∈ {ok, empty, request_error, http_NNN, decode_error}`.
Freshness for the status report = max `date` with `status=ok`.

### 5.4 Crawl semantics (`fetch_eodhd_news.py`)

- Walk days `--to` (default today UTC) **back to** `--from` (default `2021-01-01`),
  newest first, so a run capped with `--limit-days N` refreshes the most recent days
  and only then works into history.
- Skip days already `ok` in state, except the trailing `--overlap-days` (default `2`)
  which are always re-crawled; `--full-refresh` ignores state.
- Per day: `GET /news?from=D&to=D&limit=1000&offset=k` until a short page.
- Dedup on `article_id` (keep last), bucket rows by *their own* publication day
  (normally the crawled day; unparseable timestamps fall back to it), upsert into
  that day's partition, flush every 5 days; write state after every flush so a
  killed run resumes.
- Failures (`request_error`, `http_NNN`, `decode_error`) keep whatever pages were
  collected, record the failing status, and are re-crawled next run.

### 5.5 Operating it

```
uv run python eodhd/fetch_eodhd_news.py                        # ONE-OFF backfill (uncapped, hours)
uv run python eodhd/cli.py refresh --run                       # daily: prices + events + news top-up
uv run python eodhd/cli.py refresh --fast --run                # same, bulk path; news top-up still runs
uv run python eodhd/cli.py refresh news --run                  # news top-up only
uv run python eodhd/cli.py status news
uv run python eodhd/cli.py sql "SELECT s, count(*) FROM news, unnest(symbols) t(s) GROUP BY 1 ORDER BY 2 DESC LIMIT 20"
```

`news` **is** a default `refresh` kind, but a routine refresh is deliberately a
*bounded top-up*: the registry pins `--limit-days 30` (`NEWS_REFRESH_MAX_DAYS`), and
because the crawler runs newest-first that always means "the trailing overlap days
plus up to ~28 not-yet-crawled days", never a surprise 2,000-day backfill. The
ticker-style passthrough flags (`--full-refresh`, `--tickers`, `--to`, `--limit`)
never reach the crawler. The backfill is an explicit, uncapped run of the fetcher
script. The ticker verbs (`describe`, `find`, `rows`, `coverage`, `reindex`) skip
the lane; `sql` and `schema` cover it.

### 5.6 Registry / consumer wiring

- `eodhd_datasets.py`: lane `news` (region `Global`, asset class `news`), dataset kind
  `news` with `key_cols=("date",)`, `partitioned=True`, `output="articles"`.
- `schema.py`: `SCHEMAS["news"]`, `SCHEMA_VERSION` → 2.
- `cli.py`: `news` in `KNOWN_KINDS` and `DEFAULT_KINDS` (capped by the registry's
  `--limit-days`); `--fast` runs the news step after the bulk step; `qc` lane list
  restricted to lanes the QC script knows.
- `explore_eodhd.py` / `status_eodhd.py`: read partition directories via a glob; skip
  ticker-keyed operations for datasets whose `key_cols` lack `ticker`.

## 6. Follow-on phases (not in the substrate)

Tracked in detail in `NEWS_ROADMAP.md`; the scoring design in `NEWS_SCORING_DESIGN.md`.

0. ~~Full backfill~~ — done 2026-08-15 (§2.4); daily `refresh` keeps it current.
1. ~~**Derived daily panel**~~ — done 2026-08-15: `news_symbol_daily.parquet` (`build_news_symbol_daily.py`, kind `news_daily`, view `news_daily`; `share_of_day` is the normalised volume feature). Original scope: `news_symbol_daily.parquet` (`date, ticker, exchange, n,
   polarity_mean, …`) built locally from the corpus — replaces `/sentiments`.
2. **Issuer mapping** so UK/EU tickers pick up their US/ADR/Frankfurt lines.
3. **Own scoring / embeddings** over `content` (model outputs stored as sidecar
   parquet keyed by `article_id`, never mutating the raw corpus).
4. **Lab persona updates** — remove the "no news data" claims once the lane is live.
5. Optional: per-symbol `/news` gap-fill for names whose history predates the global
   feed density (2019–2020).

## 7. Maintenance rule

Re-measure §2/§3 whenever the vendor changes limits or fields; append the date and
delta here rather than rewriting the numbers silently.
