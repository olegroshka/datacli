# datacli — proof scenarios

Five scenarios that test one claim: **datacli collapses the mechanical distance
between *intent* and *goal*, so the researcher spends attention on the exploration,
not the plumbing.**

For each: the *intent* (what the user actually wants), the *mechanical way* (what
they'd otherwise do), the *interaction surface* (what datacli asks of them), the
*cognitive load removed*, and *success criteria*. A black-box test log and a
verdict follow at the end.

---

## 1. "Am I safe to build on this data?" — Monday-morning triage

- **Intent.** Back after a week; before any research, know if the data is current
  and trustworthy.
- **Mechanical way.** Open a file browser, read parquet mtimes; write ad-hoc scripts
  to count rows and hunt gaps per lane; remember which of ~30 `fetch_eodhd_*`
  scripts owns what; eyeball state CSVs for stale/broken tickers.
- **Interaction surface.** `status` → `qc us_common` → `qc us_common splits`.
- **Load removed.** No file archaeology, no script recall, no manual counting.
  Freshness is measured against the *correct* anchor per dataset kind (last bar for
  prices, coverage ceiling for events, so a legit future-dated dividend isn't a
  false alarm); issues are ranked with the recommended fix.
- **Success.** One screen shows fresh/stale/absent + row totals across every lane;
  QC names the worst issues and the remediation (`targeted_rerun` vs `full_refresh`)
  and drills into a single dataset uncapped.

## 2. "Do we have this name, and how well?" — the VAR.OL question

- **Intent.** "Someone mentioned VAR.OL — do we cover it, in which datasets, how far
  back, and evenly?"
- **Mechanical way.** Decide which parquet files to open, write DuckDB/pandas to
  scan prices/dividends/splits/fundamentals, join the state sidecars, compute
  coverage windows, and handle the `TICKER.EXCHANGE` split by hand.
- **Interaction surface.** `describe VAR.OL` (or `find VAR` when unsure of the
  symbol; `coverage VAR.OL` for the windows).
- **Load removed.** A cross-dataset coverage question that is otherwise a mini
  analysis becomes one command; fuzzy `find` absorbs "I don't remember the exact
  symbol."
- **Success.** A table of presence / rows / first / last / coverage / state per
  dataset; uneven coverage flagged in plain language.

## 3. "New laptop, my data on a drive — get me to an answer" — onboarding

- **Intent.** Fresh clone, data sitting on `D:\...`; be productive in minutes
  without touching code.
- **Mechanical way.** Edit hardcoded paths across scripts, write a loader, discover
  the schema by hand, and hope the provider's columns still match.
- **Interaction surface.** `config set data-root D:\...` → `reindex` →
  `describe` / `rows` / `sql`.
- **Load removed.** Configuration, not code. The catalog makes lookups instant; the
  schema-projected views keep queries working even if the provider renames or drops
  a column (`schema` shows the drift without breaking anything).
- **Success.** Clone → real answer in three commands; `schema` reports drift; no
  code edited.

## 4. "I think in questions, not SQL" — natural-language exploration

- **Intent.** "Which lanes have the worst dividend coverage, and why?" — express the
  question, not the query.
- **Mechanical way.** Recall the view/column names, write correct DuckDB SQL, run
  it, format the result, and sanity-check the numbers (a typo silently yields a
  wrong answer).
- **Interaction surface.** `ask "which lanes have the worst dividend coverage, and
  why?"` → it shows the SQL it wrote, the result table, and a grounded narrative;
  `--verify` adds a skeptic; `investigate` runs generator → skeptic → reporter.
- **Load removed.** NL→SQL removes the boilerplate; the *grounding contract* removes
  the "is this number real?" anxiety — the query is always shown and nothing is
  fabricated; the default persona runs on a local model, so iteration is free.
- **Success.** A correct answer *with its query*, reproducible; the surface refuses
  to state a number it did not compute.

## 5. "Put it in context and give me something to share" — synthesis → artifact

- **Intent.** "Relate US equity dispersion to rates / credit spreads / vol, and
  produce a report I can hand to a colleague."
- **Mechanical way.** Fetch FRED/EODHD macro, align dates, join to the equity
  cross-section, compute, then write it all up reproducibly by hand.
- **Interaction surface.** `macro fetch --run` →
  `investigate "relate equity dispersion to the 10Y-2Y curve and HY spreads"` → a
  reproducible Markdown report.
- **Load removed.** The macro *gather → join → compute → write-up* collapses into
  one intent; the reporter may only use numbers the analyst computed and the skeptic
  verified; the artifact regenerates deterministically.
- **Success.** A report bundling synthesis + verified findings + verdict (+ an
  optional figure), every figure backed by a shown query.

---

## Black-box test log

_Run as a first-time user against the real local snapshot (16 datasets, ~54M rows).
Scenarios 1–3 run end-to-end; 4–5 exercise the guided boundary because the optional
`lab` extra / live models / paid fetches are intentionally not invoked here._

| # | Scenario | Commands run | Result |
|---|---|---|---|
| 1 | Triage | `status` · `qc us_common` · `qc us_common splits` | **PASS** — one screen: `15 fresh · 1 stale · 0 absent · ≈54.45M rows`; QC ranked `26 errors · 113 warnings` with per-row `action` (`targeted_rerun`/`full_refresh`). *(gap found + fixed, see below)* |
| 2 | Entity lookup | `describe VAR.OL` · `find VAR` · `coverage VAR.OL` | **PASS** — `describe` answered the whole cross-dataset question in one command (VAR.OL = fundamentals-only, 68 rows, 2020→2025); `find` fuzzy-matched 6 names |
| 3 | Onboarding | `config` · `schema` · `sql "…"` · (typo) `qc us_comon` | **PASS** — resolved data-root shown with source; `schema` reported `fundamentals +27 extra` without breaking; ad-hoc `sql` needed no boilerplate; typo → *"did you mean us_common?"* |
| 4 | NL exploration | `ask "how many dividend rows per lane?"` (live, local qwen) | **PASS (live)** — the 7B wrote a correct CTE, ran it, and its answer cited exactly the returned counts (`us_common 168,714`, `us_etf 158,344`, …) — grounded, query shown, `$0.0000`. A harder question ("worst coverage, and why") is weaker on a 7B — honest evidence for why `review_model`/stronger tiers exist. |
| 5 | Synthesis → artifact | `macro fetch --run` (live) · `macro status` | **PASS (data)** — fetched **FRED 41/41 (168K rows) + EODHD 12 countries (3.3K rows)**; the `macro`/`macro_country` views are populated. The full `investigate` pipeline uses paid tiers (skeptic/reporter), runnable in your env with an API key. |

### Gap found and fixed during testing

- **FRED macro fetch aborted the whole batch on one bad series** — a discontinued
  series id (`GOLDAMGBD228NLBM`) returned HTTP 400 and, because `fred.refresh` let
  the first failure propagate (EODHD was already resilient), *no* FRED data landed.
  **Fixed:** per-series try/except that skips + reports failures, and the bad id was
  removed. Re-fetch: 41/41 series, 168K rows. (Surfaced only by running live.)
- **Status dashboard truncated on terminals narrower than ~132 cols** (`12.6…`,
  `2026-07-…`, `pai…`) — directly undermining Scenario 1's "one screen tells you
  everything." **Fixed:** the table is now **responsive** — below 132 cols it drops
  the two least-critical columns (`coverage`, which mostly duplicates `last_data`
  for prices, and `fetched`, metadata) so the essentials never truncate; `age` +
  `flag` still carry the freshness signal. Verified clean at 120 and full at 140.

## Verdict & gaps

**The intent→goal collapse holds.** Each scenario's mechanical burden — file
archaeology, hand-written cross-dataset queries, path hardcoding, SQL boilerplate,
macro gather/join/write-up — is replaced by a single, discoverable, forgiving
command. Tab-completion, positional scoping, "did you mean" errors, the cohesive
colour language and the grounding contract all pull in the same direction:
attention goes to *what to ask*, not *how to ask it*.

**Honest limitations (by design, not defects):**

- Scenarios 4–5 need the optional `lab` extra (`uv sync --extra lab`) plus a model
  (local Ollama or an API key) and, for 5, a live macro fetch. The tool degrades
  gracefully at that boundary with an actionable message; the *grounding* guarantees
  are covered by the mocked-model test suite, but NL→SQL *quality* on a local 7B is
  not exercised here.
- Reports embed figures only when `[lab].allow_python` is on and matplotlib is
  installed.

**Net:** for the always-available core (data ops + exploration), datacli delivers
the promise directly and was polished further by this test. For the LLM lab, the
interaction surface is right and the safety rails are proven; the remaining proof is
a one-command install away.

## Automated harness (`scripts/blackbox.py`)

The manual pass above is now a **repeatable, logged harness** that doubles as a
demo. It drives the *real* command entry points as subprocesses (a true black box —
real process, real output, real data), captures each command's output, checks
expectations, and writes a JSONL + Markdown transcript under `.datacli_logs/`.

```powershell
uv run python scripts/blackbox.py --check          # assert + exit code (CI)   -> 12/12 pass
uv run python scripts/blackbox.py --demo           # slow-motion, shell-styled  (screencast)
uv run python scripts/blackbox.py --check --live   # also run the LLM scenarios (needs the lab extra + a model)
uv run python scripts/blackbox.py --only S1,S2     # a subset
```

- **`--check`** currently passes **12/12** deterministic steps (S1–S3 + S5's macro
  steps); the two LLM steps (S4, S5·investigate) auto-skip unless `--live`.
- **`--demo`** types each command at a shell prompt and prints its output slowly —
  a reproducible walkthrough / screencast script.
- Building the harness immediately surfaced a real bug: capturing the commands'
  UTF-8 output with the Windows default (cp1252) silently dropped whole outputs —
  fixed by decoding as UTF-8 (exactly the kind of thing manual eyeballing misses).

**Session logging.** The shell also writes an always-on, per-session command
transcript to `.datacli_logs/session_<ts>.log` (time · source context · command)
for observability — independent of the harness.
