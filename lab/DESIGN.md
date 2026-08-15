# Raw Data Lab — Design

Status: **Phase 3 complete; MCP server and macro adapter shipped** · Owner: datacli · Last updated: 2026-08-15

> Sections marked *(as built)* were reconciled with the code on 2026-08-15; where
> the original plan and the implementation diverged, the plan is kept as history
> and the divergence is called out inline.

The Raw Data Lab turns datacli from a data-ops shell into a **grounded EDA
copilot for the pre-signal stage** — the exploratory, data-quality, and
hypothesis work that happens *before* anything reaches the btest strategy DSL.
LLM-backed agents (configured through files, with personas and skills) help with
the tedious, systematic parts; the researcher stays in command, and **every
number is computed by a query, never by the model.**

---

## 1. Goals

1. Lower the friction of asking the data questions — natural language → a
   **verified** DuckDB result, with the query always shown.
2. Systematize recurring EDA as reusable, declarative **skills**.
3. A **configurable agent roster** (personas + skills) defined in files, no code
   changes — mirroring datacli's registry-driven ethos.
4. **Reproducible artifacts** — reports whose numbers regenerate deterministically
   from the embedded queries.
5. **Provider-agnostic & cost-aware** — Claude / OpenAI / free-local (Ollama),
   chosen per persona, with budgets and caching.
6. **Native feel** — lives in the shell, renders through the existing `_render`
   palette, and wraps existing verbs (`qc`, `coverage`, `describe`).

## 2. Non-goals

- **Not signal/factor construction.** No feature engineering destined for a
  backtest; the lab *describes* data and *proposes* hypotheses to test in btest —
  it never concludes a signal works.
- **Not an oracle.** An accelerant + systematizer with a human reviewer.
- **No external side effects.** Read-only over the data; no trading, no writes to
  the raw snapshots.

## 3. Locked decisions

| Fork | Choice | Why |
|---|---|---|
| Stack | **Layered & light**: LiteLLM (models) + our DuckDB explorer as tools + a thin loop / Pydantic AI | Grounding control, tiny deps, predictable |
| Execution | **Read-only DuckDB only** (`SELECT`/`WITH`) | Safe + fully grounded; sandboxed Python deferred to Phase 3 |
| Models | **Tiered per persona** (local/cheap for mechanical, Claude for reasoning) | Cost-aware; quality where it matters |
| MVP | **Grounded `ask` analyst** + personas/skills registry + 3 skills | Smallest slice that proves the grounding spine |

## 4. The grounding contract (non-negotiable core)

Every answer flows **plan → generate SQL → validate → execute → narrate**, and the
atomic unit is a `Finding`. Enforced in code, not prompts:

- **No number without a query.** The narrative may only reference values present
  in an executed result.
- **SQL guard.** Reject anything that is not a single read-only `SELECT`/`WITH`
  statement — no DDL/DML, `ATTACH`, `COPY`, `INSTALL`, `PRAGMA` writes, or multiple
  statements. Enforce a `LIMIT` (appended when absent; rows are capped again on
  fetch). Run only against the existing projected views. *(As built:
  `lab/sqlguard.py` + `Tools.run_sql`. A SQL wall-clock timeout was planned but
  is **not implemented** — DuckDB queries run to completion; only the Phase 3b
  Python executor has a timeout.)*
- **Determinism.** Temperature 0 on the SQL/analysis path, and a response cache so
  identical calls are free on re-run. *(As built: `lab/cache.py` keys on
  `(model, messages, temperature)` — the full prompt, which already embeds the
  persona role and the schema text, so a schema change or a different persona
  changes the key. The originally planned explicit
  `(persona, question, data_root, SCHEMA_VERSION)` key was not adopted; `data_root`
  and `schema_version` travel in each Finding's provenance instead.)*
- **The query is always shown** alongside the answer.

```python
@dataclass(frozen=True)
class Finding:
    claim: str                 # the model's statement, grounded in `result`
    sql: str                   # the exact query that produced `result`
    columns: list[str]
    rows: list[tuple]          # capped result set
    provenance: dict           # persona, model, data_root, schema_version, cached, usage
```

## 5. Architecture

Three independent layers:

```
① models     LiteLLM  ── one interface: anthropic / openai / ollama (free-local)
② tools      our DuckDB explorer, schema.py, catalog, registry  (read-only)
③ orchestr.  thin grounded loop (Phase 1)  ─►  handoffs (Phase 3)
```

### Module layout *(as built)*

```
lab/
├─ DESIGN.md            this document
├─ __init__.py
├─ types.py             Finding + small value types
├─ models.py            LiteLLM wrapper: model-tier resolution, budget, response cache
├─ cache.py             on-disk response cache keyed by (model, messages, temperature)
├─ config.py            reads [lab] from datacli.toml (models, budget, cache, reports,
│                       default persona, allow_python)
├─ registry.py          loads personas (*.toml) and skills (SKILL.md) — hot-reloadable
├─ sqlguard.py          the read-only SQL guard (single SELECT/WITH, LIMIT appended)
├─ tools.py             Tools.run_sql (guarded DuckDB) + schema_context() for the prompt
├─ data.py              the lab's connection: eodhd views + macro views; schema_text()
├─ agent.py             the grounded loop: plan -> SQL -> validate -> execute -> narrate
├─ verify.py            the skeptic pass: re-derive an answer's numbers -> Verdict
├─ pipeline.py          investigate: generator -> skeptic -> reporter (shared budget)
├─ report.py            reproducible markdown reports (single run and pipeline)
├─ pyexec.py            Phase 3b restricted Python executor (subprocess + timeout)
├─ _pyexec_runner.py    the child-process side of pyexec
├─ cli.py               `lab config|agents|skills|run` and `ask|agent|investigate`
├─ personas/            9 personas: analyst, auditor, quant, skeptic, reporter,
│                       hypothesizer, macro-strategist, microstructure, event-study
└─ skills/
   ├─ coverage-audit/SKILL.md
   ├─ distribution-profile/SKILL.md
   └─ corporate-action-consistency/SKILL.md
```

Two things differ from the original sketch: the tool surface is narrower than
planned — only `run_sql` (and, opt-in, `run_python`) are dispatchable by the model;
`schema` / `catalog_lookup` / `list_lanes` were folded into **static prompt
context** (`tools.schema_context()` + `data.schema_text()`, which also lists the
`catalog` view and the macro views when present) rather than callable tools. And
the roster grew from the two sketched personas to nine.

Rendering reuses the existing `_render` palette so Findings look like the rest of
the tool. (`_render` currently lives in `eodhd/`; `lab` imports it via the same
`sys.path` convention the eodhd scripts use. If cross-package coupling gets
awkward we promote `_render` to a shared top-level module — tracked, not done in
Phase 0.)

## 6. Config shapes (concrete)

### `datacli.toml` — new `[lab]` section (keys via env, never printed)

```toml
[lab]
default_persona = "analyst"
cache_dir = ".lab_cache"          # under the repo; git-ignored

[lab.budget]
per_session_usd = 1.00            # hard ceiling; agent stops when hit
warn_usd = 0.50

[lab.models]                      # tier -> concrete model id (LiteLLM syntax)
local  = "ollama/qwen2.5-coder:7b"   # fits a 12GB GPU; strong at text-to-SQL
cheap  = "openai/gpt-4o-mini"
mid    = "anthropic/claude-sonnet-5"
strong = "anthropic/claude-opus-4-8"
```

### Persona — `lab/personas/analyst.toml`

```toml
name = "analyst"
description = "Grounded EDA analyst for pre-signal data exploration."
model = "local"                   # everyday EDA -> the free local model
# review_model = "strong"         # optional: do the legwork locally, then have a
                                  # stronger model write the FINAL answer from the
                                  # same evidence
temperature = 0.0
tools  = ["run_sql"]              # add "run_python" for the Phase 3b executor (quant)
skills = ["coverage-audit", "distribution-profile", "corporate-action-consistency"]
role = """
You are a meticulous market-data analyst working PRE-signal. You describe and
audit raw data; you do NOT design trading signals. Hard rules:
- Never state a number you did not compute with run_sql. Show the query.
- Prefer small, exact queries over sweeping ones; cap rows.
- Flag anomalies plainly; label any speculation as a HYPOTHESIS to test in btest.
"""
```

`lab/personas/auditor.toml` is the same shape with `model = "local"` and a role
focused on coverage, missingness, and universe/state/output mismatches.

**Model tiering.** Serious personas use serious models: the investigation roles
(`skeptic`, `hypothesizer`, `reporter`, and the `macro-strategist` /
`microstructure` / `event-study` analysts) default to `strong` (Opus 4.8); the
everyday `analyst` and the mechanical `auditor` / `quant` stay on the free `local`
model. **Grounding is temperature-independent** — every number comes from an
executed query, so it is reproducible regardless of how the model samples prose;
that is why a temp-1-only reasoning tier is fine for grounded work. Opus/Sonnet
reject `temperature=0`, so LiteLLM runs with `drop_params=True` (see
`LLM._litellm`) to drop the unsupported value rather than error. A persona can
still set `review_model` to do cheap/local legwork and synthesise the FINAL answer
on a stronger tier, and the pipeline degrades gracefully if any stage's model is
unavailable (rate-limited, unauthenticated, or a local server that's down).

### Skill — `lab/skills/coverage-audit/SKILL.md`

```markdown
---
name: coverage-audit
summary: Per (lane, dataset) coverage windows, gaps, and universe/state/output mismatches.
inputs: [lane]        # optional args the skill accepts
tier: cheap           # suggested model tier for this skill
---

Goal
Assess how completely each dataset covers its universe and how fresh it is.

Starting queries (adapt as needed; all read-only)
- Coverage window per lane/dataset from the state views:
    SELECT lane, 'dividends' AS dataset, max(coverage_through) AS through
    FROM dividends_state GROUP BY lane
- Universe vs. output vs. state pair counts …

Output
A table of (lane, dataset, coverage_through, gap_days, mismatch) plus a short
narrative that names the worst offenders and the recommended action.
```

## 7. Shell surface (Phase 1)

```
ask "<question>"          grounded analyst (default persona)
agent <name> "<task>"     invoke a specific persona
lab run <skill> [args]    run an EDA playbook  (fuller reports in Phase 2)
lab agents                list configured personas          (like `sources`)
lab skills                list EDA playbooks                (like `lanes`)
lab config                providers / per-persona models / budget / cache
```

*(As built: Phase 3a added `investigate <topic> [--generator <persona>]
[--no-report]`; `ask`/`agent` take `[--verify] [--report]`, `lab run` takes
`[--verify] [--no-report]`; every command answers `--help` before touching a
model, and bare `lab` is `lab config`. `lab/cli.py` holds the one command table
the help text is rendered from.)*

- Global shell commands (not a `source lab` context): the analyst is cross-cutting
  and operates on whatever data root is configured.
- **Tab-completion** for persona names (`agent <tab>`) and skill names
  (`lab run <tab>`), consistent with the existing completers.
- Answers stream as a short narrative + the SQL + a `_render` result table.

## 8. Tools exposed to the agent *(as built; all read-only)*

| Tool | Returns | Backed by |
|---|---|---|
| `run_sql(query)` | validated read-only result (capped) | `lab/data.connect()` (eodhd + macro views) + `sqlguard` |
| `run_python(code)` *(opt-in)* | stdout + at most one figure, over the last query's `df` | `lab/pyexec.py` (Phase 3b; `[lab].allow_python` + persona `tools`) |

Originally sketched as callable tools but shipped as **static prompt context**
instead (cheaper for small models, nothing to mis-call):

| Was planned as | Now | Backed by |
|---|---|---|
| `schema()` / `describe_dataset(name)` | the view list with canonical columns in the system prompt | `tools.schema_context()` from `schema.py` |
| `catalog_lookup(...)` | the `catalog` view (present after `reindex`), described in the prompt | `_datacli_index.parquet` |
| `list_lanes()` | every view carries a `lane` column; the prompt says so | `eodhd_datasets` registry |

The agent receives that compact, accurate schema (from `schema.py`, plus the
macro snippet for whichever macro views exist and the `news` notes when the news
lane is present) in its context, which is what makes NL→SQL reliable even for
small/free models. The same `run_sql` / schema / lanes surface is exposed to
external clients by `mcp_server.py` (tools `sql`, `describe_schema`, `list_lanes`).

## 9. Phases & review gates

Each phase ends with a **review gate**: stop, demo, adjust if required, then
proceed. Nothing merges past a gate without a green review.

- **Phase 0 — substrate.** LiteLLM wrapper + budget + cache + `Finding` type +
  `[lab]` config + `lab config` command. `litellm` added as an optional `lab`
  extra, imported lazily so the shell runs without it. Tests use a **mocked
  model** (no live API). → *gate*
- **Phase 1 — grounded analyst (MVP).** `ask`/`agent`, the grounded loop, the
  read-only SQL guard, personas/skills registry, 3 skills, `lab agents/skills`.
  → *gate*
- **Phase 2 — reports & skeptic.** `lab run <skill>` → reproducible
  markdown/notebook; an adversarial skeptic re-derives numeric claims before they
  enter a report. → *gate*
- **Phase 2.5 — macro data adapter (FRED + EODHD).** Widen grounding beyond the
  equity tape: a registry of market-macro series and cross-country indicators,
  fetchers to parquet, and three views the lab layers onto the eodhd connection --
  `macro` (FRED daily/monthly: rates, curve, credit spreads, VIX, FX, money,
  activity, conditions), `macro_country` (EODHD annual: GDP, CPI, unemployment,
  real rates per country) and `macro_market` (EODHD end-of-day index and FX
  levels: S&P 500, Nasdaq, DAX, Nikkei, EUR/USD, ...). The macro-strategist /
  event-study personas may join any of them to the price data. → *gate (done)*
- **Phase 3a — multi-agent pipeline.** `investigate <topic>` runs
  generator → skeptic → reporter (shared session budget, grounded synthesis) into a
  combined reproducible report. → *gate (done)*
- **Phase 3b — richer analysis (restricted executor).** A *restricted* local
  Python executor (subprocess + wall-clock timeout + restricted builtins + import
  whitelist + network block) runs analysis code on the last query's DataFrame for
  distributions/plots SQL can't express; figures embed in the report. Off by
  default (`[lab].allow_python`), gated per persona (`run_python` tool); the `quant`
  persona uses it. Scoped honestly as a trusted-local tool, NOT a hardened security
  sandbox. → *gate (done)*
- **Later (optional) — all three since done.** MCP server exposing the read-only
  tools to external clients (Claude Code / Cursor) → `mcp_server.py` (`sql`,
  `describe_schema`, `list_lanes`; `uv sync --extra mcp`); incremental macro fetch
  → `macro fetch --run` merges into the existing parquet unless `--full`; EODHD
  as a second FRED-style time-series provider → the `macro_market` view.

## 10. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Small/free models write plausible-but-wrong SQL | Accurate schema in-context + read-only guard + **query always shown** so the human catches it; skeptic pass (Phase 2) |
| Hallucinated statistics | Grounding contract: no number without a query; Findings carry provenance |
| Cost creep | Per-persona tiers + response cache + hard session budget in `lab config` |
| EDA silently becoming in-sample-optimized signals | Non-goal stated in every persona role; hypotheses explicitly labelled "test in btest" |
| Arbitrary code execution | MVP is SQL-only; Python only in Phase 3 behind a sandbox |
| Dependency weight / breakage | `litellm` is an optional extra, lazily imported; core shell unaffected |

## 11. Testing

- **Never hit a live API in unit tests** (repo rule). The model is mocked; tests
  assert on prompt construction, budget accounting, cache hit/miss, the SQL guard
  (accept `SELECT`/`WITH`, reject DML/DDL/multi-statement), registry loading, and
  Finding rendering.
- A small set of **golden NL→SQL** cases can be run manually/optionally against a
  real (ideally local/free) model, gated behind a marker — not in the default
  suite.

## 12. Open items (small; defaults chosen, flag to change)

- `_render` sharing: import from `eodhd/` for now; promote to shared module if it
  gets awkward.
- Personas as TOML, skills as folder + `SKILL.md` (Claude-Code-skills style),
  hot-loadable.
- `ask`/`agent` are **global** shell commands; the lab reads whatever data root
  `config` resolves.
