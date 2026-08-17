"""``score`` -- plan, run and inspect scoring jobs over the news corpus.

Usage:
    uv run python -m scoring.cli plan   [--schema event] [--backend llm|vendor|embed] [--days 90 | --since --until]
    uv run python -m scoring.cli run    ... --run            (same flags; nothing is scored without --run)
    uv run python -m scoring.cli status                      (what exists on disk)
    uv run python -m scoring.cli schemas | backends

Local-only by default: the llm/embed backends refuse a non-Ollama model unless
``--budget-usd N`` (or ``[scoring].budget_usd``) allows paid calls. ``plan`` never
touches a model. Inside the datacli shell the same command is ``score ...``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
_EODHD = _REPO_ROOT / "eodhd"
for p in (str(_REPO_ROOT), str(_EODHD)):
    if p not in sys.path:
        sys.path.insert(0, p)

from scoring import config as cfg_mod  # noqa: E402
import pandas as pd  # noqa: E402
from scoring import bench as bench_mod  # noqa: E402
from scoring import evaluate as ev  # noqa: E402
from scoring import runner, store  # noqa: E402
from scoring.backends import BACKENDS, get_backend  # noqa: E402
from scoring.schema import SchemaError, list_schemas, load_schema  # noqa: E402
from llm import tiers  # noqa: E402

PROG = "uv run python -m scoring.cli"


def _console() -> Any:
    import _render  # type: ignore[import-not-found]

    return _render.make_console()


def _data_root() -> Path:
    from _datadir import EODHD_RAW_ROOT  # type: ignore[import-not-found]

    return EODHD_RAW_ROOT


def _connect() -> Any:
    import explore_eodhd  # type: ignore[import-not-found]

    return explore_eodhd.connect()


def build_parser() -> argparse.ArgumentParser:
    cfg = cfg_mod.load()
    p = argparse.ArgumentParser(
        prog=PROG,
        description="Schema-driven, backend-pluggable scoring of the news corpus "
        "(see eodhd/NEWS_SCORING_DESIGN.md). plan is free; run scores; both are "
        "local-only unless --budget-usd allows paid models.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    def add_job_flags(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "--schema",
            default="event",
            help="Schema name or name@version (default: event, latest)",
        )
        sp.add_argument(
            "--backend", default="llm", choices=BACKENDS, help="Backend (default: llm)"
        )
        sp.add_argument(
            "--model",
            default=None,
            help=f"Tier or model id (default: [scoring].llm_model={cfg.llm_model!r} / embed_model={cfg.embed_model!r})",
        )
        sp.add_argument(
            "--days",
            type=int,
            default=None,
            help=f"Window: last N days (default {cfg.window_days}); ignored with --since",
        )
        sp.add_argument(
            "--since", default=None, help="First publication day (YYYY-MM-DD)"
        )
        sp.add_argument(
            "--until", default=None, help="Last publication day (default: today)"
        )
        sp.add_argument(
            "--no-universe",
            action="store_true",
            help="Do not restrict to articles tagging our price universe",
        )
        sp.add_argument(
            "--max-symbols",
            type=int,
            default=cfg.max_symbols,
            help=f"Per-symbol scoring only up to this many target symbols (default {cfg.max_symbols})",
        )
        sp.add_argument(
            "--sample",
            type=int,
            default=None,
            help="Random sample of N articles per day (seeded)",
        )
        sp.add_argument("--seed", type=int, default=0)
        sp.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Cap the number of items this run (smoke tests)",
        )
        sp.add_argument(
            "--force", action="store_true", help="Re-score items already scored ok"
        )
        sp.add_argument(
            "--budget-usd",
            type=float,
            default=None,
            help=f"Allow paid calls up to this much (default {cfg.budget_usd}: local-only)",
        )
        sp.add_argument(
            "--chunk",
            type=int,
            default=25,
            help="Items per write/progress line (default 25)",
        )

    sp_plan = sub.add_parser(
        "plan", help="Count pending items and estimate time/cost (free)"
    )
    add_job_flags(sp_plan)
    sp_run = sub.add_parser("run", help="Score the pending items (needs --run)")
    add_job_flags(sp_run)
    sp_run.add_argument(
        "--run", action="store_true", help="Actually score (default: show the plan)"
    )
    sub.add_parser("status", help="What score/embedding sidecars exist on disk")
    sp_eval = sub.add_parser(
        "eval",
        help="Health, agreement with the vendor score, distributions, backend vs backend",
    )
    sp_eval.add_argument(
        "--schema", default="event", help="Schema name (default: event)"
    )
    sp_eval.add_argument("--backend", default=None, help="Restrict to one backend id")
    sp_eval.add_argument(
        "--compare",
        nargs=2,
        metavar=("BACKEND_A", "BACKEND_B"),
        default=None,
        help="Agreement between two backend ids on the articles both scored",
    )
    sp_bench = sub.add_parser(
        "bench",
        help="Compare (model x schema) configs on one fixed article sample (validity, calibration, return-signal, head-to-head)",
    )
    sp_bench.add_argument(
        "--configs",
        required=True,
        help="Comma list of MODEL[:SCHEMA] (schema defaults to 'event'). A bare "
        "ollama tag gets its 'ollama/' prefix added, e.g. "
        "'qwen2.5-coder:7b,ollama/qwen2.5:7b-instruct:event@2'",
    )
    sp_bench.add_argument(
        "--n", type=int, default=300, help="Articles in the sample (default 300)"
    )
    sp_bench.add_argument(
        "--days", type=int, default=30, help="Sample from the last N days (default 30)"
    )
    sp_bench.add_argument("--seed", type=int, default=7)
    sp_bench.add_argument("--chunk", type=int, default=25)
    sp_bench.add_argument(
        "--run-id", default=None, help="Name for this bench run (default: timestamp)"
    )
    sp_bench.add_argument(
        "--budget-usd",
        type=float,
        default=None,
        help="Allow paid models up to this much",
    )
    sp_panel = sub.add_parser(
        "panel-eval",
        help="Cross-sectional and magnitude tests over the whole scored panel "
        "(direction by quintile, materiality/intensity vs |return|)",
    )
    sp_panel.add_argument(
        "--schema",
        default="event",
        help="Schema whose score view to read, or 'vendor' for the free "
        "polarity baseline over the entire corpus",
    )
    sp_panel.add_argument("--backend", default=None)
    sp_panel.add_argument(
        "--buckets", type=int, default=5, help="Cross-section buckets (default 5)"
    )
    sp_panel.add_argument(
        "--since", default=None, help="Restrict the panel to dates >= this"
    )
    sub.add_parser("schemas", help="List available schemas")
    sub.add_parser("backends", help="List backends and the resolved default models")
    return p


def _target(args: argparse.Namespace, cfg: cfg_mod.ScoringConfig) -> runner.Target:
    if args.since:
        since = args.since
        until = args.until or runner.default_window(1)[1]
    else:
        since, until = runner.default_window(args.days or cfg.window_days)
        if args.until:
            until = args.until
    return runner.Target(
        since=since,
        until=until,
        use_universe=not args.no_universe,
        max_symbols=args.max_symbols,
        sample_per_day=args.sample,
        seed=args.seed,
        limit=args.limit,
        force=args.force,
    )


def _backend(args: argparse.Namespace, cfg: cfg_mod.ScoringConfig) -> Any:
    kwargs: dict[str, Any] = {}
    if args.backend in ("llm", "embed"):
        kwargs = {"config": cfg, "model": args.model, "budget_usd": args.budget_usd}
    return get_backend(args.backend, **kwargs)


def _print_plan(plan: runner.Plan, *, run: bool) -> None:
    import _render  # type: ignore[import-not-found]
    from rich.text import Text

    console = _console()
    mode = "RUN" if run else "PLAN"
    title = Text(f"score {mode}  ", style="bold red" if run else "bold")
    title.append(f"{plan.schema} · {plan.backend}", style="cyan")
    title.append(f"  ({plan.model})", style="dim")
    table = _render.minimal_table(title=title)  # type: ignore[arg-type]
    for col, just in (
        ("day", "left"),
        ("candidates", "right"),
        ("pending", "right"),
        ("est. time", "right"),
        ("est. cost", "right"),
    ):
        table.add_column(col, justify=just, no_wrap=True)
    shown = plan.days[:15]
    for d in shown:
        table.add_row(
            d.day,
            f"{d.n_candidates:,}",
            f"{d.n_pending:,}",
            _fmt_secs(d.seconds),
            f"${d.cost_usd:,.2f}",
        )
    if len(plan.days) > len(shown):
        table.add_row(f"… {len(plan.days) - len(shown)} more days", "", "", "", "")
    console.print(table)
    t = plan.target
    console.print(
        f"window {t.since}..{t.until} · universe={'on' if t.use_universe else 'off'} · "
        f"max_symbols={t.max_symbols}"
        + (f" · sample/day={t.sample_per_day}" if t.sample_per_day else "")
        + (f" · limit={t.limit}" if t.limit else "")
        + (" · force" if t.force else "")
    )
    console.print(
        f"[bold]{plan.n_pending:,}[/bold] pending of {plan.n_candidates:,} candidates "
        f"over {len(plan.days)} day(s) · est. {_fmt_secs(plan.seconds)} · est. ${plan.cost_usd:,.2f}"
        + (f"  [dim]({plan.note})[/dim]" if plan.note else "")
    )
    if not run:
        console.print("[dim]plan only — add --run to score.[/dim]")


def _fmt_secs(s: float) -> str:
    if s < 90:
        return f"{s:.0f}s"
    if s < 5400:
        return f"{s/60:.0f}m"
    if s < 172800:
        return f"{s/3600:.1f}h"
    return f"{s/86400:.1f}d"


def cmd_status() -> int:
    import _render  # type: ignore[import-not-found]
    from rich.text import Text

    console = _console()
    rows = store.discover(_data_root())
    if not rows:
        console.print(
            f"[dim]no scores yet under {store.scores_root(_data_root())} — "
            f"try: {PROG} plan[/dim]"
        )
        return 0
    table = _render.minimal_table(title="news scores on disk")
    table.add_column("schema", no_wrap=True)
    table.add_column("backend", overflow="fold")
    table.add_column("days", justify="right", no_wrap=True)
    table.add_column("last", no_wrap=True)
    table.add_column("ok", justify="right", no_wrap=True)
    table.add_column("inv", justify="right", no_wrap=True)
    table.add_column("err", justify="right", no_wrap=True)
    table.add_column("s/item", justify="right", no_wrap=True)
    table.add_column("cost", justify="right", no_wrap=True)
    for r in rows:
        n = max(r["n_ok"] + r["n_invalid"] + r["n_error"], 1)
        table.add_row(
            "embeddings" if r["kind"] == "vector" else r["schema"],
            Text(r["backend"], style="cyan"),
            str(r["days"]),
            r["last_day"] or "-",
            f"{r['n_ok']:,}",
            f"{r['n_invalid']:,}",
            f"{r['n_error']:,}",
            f"{r['seconds'] / n:.1f}",
            f"${r['cost_usd']:.2f}",
        )
    console.print(table)
    console.print(
        "[dim]views: news_scores_<schema> (latest) / news_scores_<schema>_vN / "
        "news_embeddings — query with sql[/dim]"
    )
    return 0


def warn_if_shadowed(console: Any, con: Any, view: str) -> None:
    """Warn when a sibling schema version holds far more rows than ``view``.

    ``news_scores_event`` means "the latest version", which is resolved by version
    number rather than by how much data each holds. A handful of stray rows from a
    one-off ``event@3`` test is therefore enough to shadow a 62,000-row v2
    dataset, and every downstream verb then reports on the strays -- which reads
    as "nothing has been scored" rather than "you are pointed at the wrong view".
    """
    import re

    m = re.fullmatch(r"(news_scores_[a-z0-9_]+?)(?:_v(\d+))?", view)
    if not m:
        return
    base = m.group(1)
    try:
        here = con.execute(f"SELECT count(*) FROM {view}").fetchone()[0]
    except Exception:
        return
    rows = con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_name LIKE ?",
        [f"{base}_v%"],
    ).fetchall()
    bigger = []
    for (name,) in rows:
        if name == view:
            continue
        try:
            n = con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
        except Exception:
            continue
        if n > max(here * 10, here + 1000):
            bigger.append((name, n))
    if not bigger:
        return
    best = max(bigger, key=lambda t: t[1])
    console.print(
        f"[yellow]{view} has {here:,} rows, but {best[0]} has {best[1]:,}[/yellow]\n"
        f"[dim]'{view}' resolves to the highest schema version present, not the "
        f"largest. Pass --schema {best[0].replace('news_scores_', '').replace('_v', '@')} "
        f"to use it.[/dim]"
    )


def score_view_name(schema_spec: str) -> str:
    """``event`` -> ``news_scores_event`` (latest); ``event@2`` / ``event_v2`` ->
    ``news_scores_event_v2``."""
    spec = schema_spec.strip()
    if "@" in spec:
        name, _, ver = spec.partition("@")
        return f"news_scores_{name}_v{ver}"
    if "_v" in spec and spec.rsplit("_v", 1)[1].isdigit():
        name, ver = spec.rsplit("_v", 1)
        return f"news_scores_{name}_v{ver}"
    return f"news_scores_{spec}"


def _df_table(console: Any, df: Any, title: str) -> None:
    import _render  # type: ignore[import-not-found]

    if df is None or len(df) == 0:
        console.print(f"[dim]{title}: (nothing)[/dim]")
        return
    console.print(_render.df_table(df, title=title))


def cmd_eval(args: argparse.Namespace) -> int:
    console = _console()
    con = _connect()
    view = score_view_name(args.schema)
    from scoring.select import has_view

    if not has_view(con, view):
        console.print(
            f"[yellow]no view {view}[/yellow] -- nothing scored yet for schema "
            f"{args.schema!r}? try: {PROG} run --run"
        )
        return 0
    warn_if_shadowed(console, con, view)
    df = ev.load_scores(con, view, backend=args.backend)
    syms = ev.load_symbol_scores(con, view, backend=args.backend)
    if df.empty:
        console.print(f"[yellow]no rows in {view}[/yellow]")
        return 0
    _df_table(console, ev.health(df), "health by backend")
    vv = ev.vs_vendor(df)
    if vv.get("n", 0) >= 3:
        console.print(
            f"[bold]vs vendor polarity[/bold]  n={vv['n']:,}  pearson={vv['pearson']}  "
            f"spearman={vv['spearman']}  sign agreement={vv['sign_agreement']}"
        )
        table = vv["sign_table"].copy()
        table.index = [f"ours={i}" for i in table.index]
        table.columns = [f"vendor={c}" for c in table.columns]
        _df_table(
            console, table.reset_index(names="sign"), "sign agreement (rows = ours)"
        )
        for label, d in (("ours:  ", vv["ours"]), ("vendor:", vv["vendor"])):
            console.print(
                f"{label} mean={d['mean']} std={d['std']} "
                f"p10/50/90={d['p10']}/{d['p50']}/{d['p90']} "
                f"pos={d['share_pos']} neg={d['share_neg']}"
            )
    else:
        console.print("[dim]vs vendor: fewer than 3 comparable articles[/dim]")
    dist = ev.distributions(df, syms)
    for key in (
        "event_type",
        "sentiment_by_event_type",
        "horizon",
        "materiality",
        "novelty",
        "symbol_role",
        "symbol_direction",
    ):
        if key in dist:
            _df_table(console, dist[key], key)
    if args.compare:
        a_id, b_id = args.compare
        a = ev.load_scores(con, view, backend=a_id)
        b = ev.load_scores(con, view, backend=b_id)
        sa = ev.load_symbol_scores(con, view, backend=a_id)
        sb = ev.load_symbol_scores(con, view, backend=b_id)
        cmp_ = ev.compare(a, b, sa, sb)
        console.print(
            f"[bold]{a_id} vs {b_id}[/bold]  "
            + "  ".join(f"{k}={v}" for k, v in cmp_.items())
        )
    return 0


def _parse_configs(spec: str) -> list[Any]:
    """``model[:schema]`` items; a model id may itself contain ':' (ollama tags)."""
    out = []
    for raw in spec.split(","):
        raw = raw.strip()
        if not raw:
            continue
        schema = "event"
        # a trailing ':event' / ':event@2' / ':event_v2' is the schema
        if ":" in raw:
            head, _, tail = raw.rpartition(":")
            if tail.split("@")[0].split("_v")[0].isalpha() and head:
                raw, schema = head, tail
        out.append(bench_mod.Config(model=tiers.normalize_model(raw), schema_spec=schema))
    return out


def cmd_bench(args: argparse.Namespace, cfg: cfg_mod.ScoringConfig) -> int:
    from datetime import datetime, timezone

    import _render  # type: ignore[import-not-found]
    from rich.text import Text

    console = _console()
    configs = _parse_configs(args.configs)
    if len(configs) < 1:
        console.print("[red]no configs[/red]")
        return 2
    if args.budget_usd is not None:
        cfg = cfg_mod.ScoringConfig(**{**cfg.__dict__, "budget_usd": args.budget_usd})

    con = _connect()
    since, until = runner.default_window(args.days)
    console.print(
        f"[dim]building sample: {args.n} articles from {since}..{until} (seed {args.seed})[/dim]"
    )
    items = bench_mod.build_sample(
        con, n=args.n, since=since, until=until, seed=args.seed
    )
    if not items:
        console.print(
            "[yellow]no sampleable articles (need universe symbols with prices)[/yellow]"
        )
        return 0
    console.print(
        f"sample: [bold]{len(items)}[/bold] articles, {sum(len(i.target_symbols) for i in items)} (article, symbol) rows"
    )

    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = _data_root() / "news" / "bench" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    bench_mod.sample_frame(items).to_parquet(out_dir / "_sample.parquet", index=False)
    reactions = bench_mod.price_reactions(con, items)
    console.print(
        f"[dim]price reactions available for {len(reactions)} (article, symbol) rows[/dim]"
    )

    results: list[Any] = []
    for c in configs:
        console.print(Text(f"\n=== {c.id} ===", style="bold cyan"))
        try:
            res = bench_mod.run_config(
                items,
                c,
                cfg=cfg,
                chunk=args.chunk,
                on_progress=lambda m: console.print(f"[dim]{m}[/dim]"),
            )
        except Exception as exc:
            console.print(f"[red]{type(exc).__name__}: {exc}[/red]")
            continue
        res.metrics = bench_mod.config_metrics(res.frame, reactions, res.seconds)
        res.frame.to_parquet(out_dir / f"{c.id}.parquet", index=False)
        results.append(res)

    if not results:
        console.print("[red]every config failed[/red]")
        return 1

    card = bench_mod.scorecard(results)
    card.to_csv(out_dir / "scorecard.csv", index=False)
    console.print(Text("\nscorecard", style="bold"))
    console.print(_render.df_table(card, title=f"bench {run_id}"))

    if len(results) > 1:
        base = results[0]
        rows = []
        for other in results[1:]:
            h = bench_mod.head_to_head(base.frame, other.frame, reactions)
            rows.append({"a": base.config.id, "b": other.config.id, **h})
        h2h = pd.DataFrame(rows)
        h2h.to_csv(out_dir / "head_to_head.csv", index=False)
        console.print(_render.df_table(h2h, title="head-to-head vs first config"))

        # Every pair, paired McNemar on both horizons. Read `sign_agree` first:
        # when it is high the configs make the same directional calls and the
        # test cannot separate them, whatever the win counts look like.
        prows = []
        for i, ra in enumerate(results):
            for rb in results[i + 1 :]:
                for horizon in ("r0", "r1"):
                    prows.append(
                        {
                            "a": ra.config.id,
                            "b": rb.config.id,
                            **bench_mod.paired_sign_test(
                                ra.frame, rb.frame, reactions, horizon=horizon
                            ),
                        }
                    )
        paired = pd.DataFrame(prows)
        paired.to_csv(out_dir / "paired_sign_test.csv", index=False)
        console.print(_render.df_table(paired, title="paired sign test (McNemar)"))
    console.print(f"[dim]written to {out_dir}[/dim]")
    return 0


def cmd_panel_eval(args: argparse.Namespace) -> int:
    """Ask whether the scored panel carries signal the way it would be used.

    ``score bench`` judges one article's call against one stock, which is the
    noisiest possible framing. This aggregates to ``(date, symbol)`` first, ranks
    the cross-section, and reports a t over *days* with a Newey-West correction
    for overlapping horizons.
    """
    import _render  # type: ignore[import-not-found]

    from scoring import panel_eval as pev

    console = _console()
    con = _connect()
    from scoring.select import has_view

    if args.schema == "vendor":
        panel = pev.vendor_panel(con, since=args.since)
        label, score_col, mat_field = "vendor polarity", "score", None
    else:
        view = score_view_name(args.schema)
        if not has_view(con, view):
            console.print(f"[yellow]no view {view}[/yellow] -- nothing scored yet")
            return 0
        warn_if_shadowed(console, con, view)
        panel = pev.signal_panel(con, view, backend=args.backend)
        if args.since:
            panel = panel[panel["date"] >= args.since]
        label, score_col, mat_field = view, "score_w", None
    if panel.empty:
        console.print("[yellow]empty panel[/yellow]")
        return 0
    console.print(f"[dim]panel: {len(panel):,} (date, symbol) rows from {label}[/dim]")

    joined, report = pev.attach_returns(con, panel)
    if joined.empty:
        console.print("[yellow]no panel rows joined to prices[/yellow]")
        return 0
    console.print(
        f"[dim]joined {report['joined_rows']:,} rows "
        f"({report['match_share']:.0%} of the panel) over "
        f"{report['n_days']:,} trading days[/dim]"
    )

    horizons = [f"f{h}_ex" for h in pev.HORIZONS]
    keys = ("n_days", "nw_lags", "mean_bps", "t", "p", "monotone")

    def _rows(fn: Any, **kw: Any) -> pd.DataFrame:
        out = []
        for h in horizons:
            _, st = fn(joined, horizon=h, **kw)
            if st:
                out.append({"horizon": h, **{k: st.get(k) for k in keys}})
        return pd.DataFrame(out)

    direction = _rows(pev.cross_section, score_col=score_col, n_buckets=args.buckets)
    if not direction.empty:
        console.print(
            _render.df_table(direction, title=f"direction: {score_col} long-short")
        )
    # Every impact field the schema carries, so v4's `expected_move` and the
    # `materiality` it is meant to beat are shown side by side on identical rows.
    impact_cols = [
        f"{f}_max" for f in pev.IMPACT_FIELDS if f"{f}_max" in joined.columns
    ] or ([mat_field] if mat_field and mat_field in joined.columns else [])
    for field in impact_cols:
        mag = _rows(pev.magnitude, field=field)
        if not mag.empty:
            console.print(
                _render.df_table(mag, title=f"magnitude: {field} vs |return|")
            )
    inten = _rows(pev.intensity)
    if not inten.empty:
        console.print(
            _render.df_table(inten, title="magnitude: article count vs |return| (free)")
        )
    console.print(
        "[dim]t is over days with a Newey-West correction for overlapping "
        "horizons; `monotone` is the Spearman of bucket order against outcome. "
        "Read both -- a significant t with a flat monotone is a single odd bucket, "
        "not an ordering.[/dim]"
    )
    return 0


def cmd_schemas() -> int:
    console = _console()
    for key, s in list_schemas().items():
        console.print(
            f"[cyan]{key}[/cyan]  {s.description}\n"
            f"   scope={s.scope} max_symbols={s.max_symbols} text={s.text} max_chars={s.max_chars}\n"
            f"   fields: {', '.join(s.field_names())}"
            + (
                f"\n   symbol_fields: {', '.join(s.symbol_field_names())}"
                if s.symbol_fields
                else ""
            )
            + f"\n   [dim]{s.source}[/dim]"
        )
    return 0


def cmd_backends(cfg: cfg_mod.ScoringConfig) -> int:
    console = _console()
    console.print(f"vendor   record   eodhd-vader (free baseline)")
    console.print(
        f"llm      record   default model: {cfg.resolve_model(cfg.llm_model)}  (tier {cfg.llm_model!r})"
    )
    console.print(
        f"embed    vector   default model: {cfg.resolve_model(cfg.embed_model)}  (tier {cfg.embed_model!r})"
    )
    console.print(
        f"[dim]budget_usd={cfg.budget_usd} -> {'local-only' if cfg.local_only else 'paid calls allowed'}; cache={cfg.cache_dir}[/dim]"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = cfg_mod.load()
    if args.command == "status":
        return cmd_status()
    if args.command == "eval":
        return cmd_eval(args)
    if args.command == "bench":
        return cmd_bench(args, cfg)
    if args.command == "panel-eval":
        return cmd_panel_eval(args)
    if args.command == "schemas":
        return cmd_schemas()
    if args.command == "backends":
        return cmd_backends(cfg)

    console = _console()
    try:
        schema = load_schema(args.schema)
        backend = _backend(args, cfg)
    except (SchemaError, ValueError, RuntimeError) as exc:
        console.print(f"[red]{exc}[/red]")
        return 2
    target = _target(args, cfg)
    con = _connect()
    plan = runner.plan(
        con, data_root=_data_root(), schema=schema, backend=backend, target=target
    )
    do_run = args.command == "run" and getattr(args, "run", False)
    _print_plan(plan, run=do_run)
    if not do_run:
        if args.command == "run":
            console.print("[yellow]nothing scored: pass --run to execute.[/yellow]")
        return 0
    if plan.n_pending == 0:
        console.print("[green]nothing pending.[/green]")
        return 0
    totals = runner.run(
        con,
        data_root=_data_root(),
        schema=schema,
        backend=backend,
        target=target,
        chunk=args.chunk,
        on_progress=lambda m: console.print(f"[dim]{m}[/dim]"),
    )
    console.print(
        f"[bold]done[/bold]: days={totals['days']} ok={totals['n_ok']} invalid={totals['n_invalid']} "
        f"error={totals['n_error']} skipped={totals['n_skipped']} · {_fmt_secs(totals['seconds'])} · "
        f"${totals['cost_usd']:.2f}"
        + (f" · stopped: {totals['stopped']}" if totals["stopped"] else "")
    )
    return 0 if not totals["stopped"].startswith("budget") else 1


if __name__ == "__main__":
    sys.exit(main())
