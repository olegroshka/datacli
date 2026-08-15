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
from scoring import runner, store  # noqa: E402
from scoring.backends import BACKENDS, get_backend  # noqa: E402
from scoring.schema import SchemaError, list_schemas, load_schema  # noqa: E402

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
