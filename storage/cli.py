"""``sync`` — push the data root to personal cloud storage (backup, one-way).

Same culture as the eodhd CLI: everything is a dry-run plan until ``--run``.
``status`` and the plan never touch the network; auth (a browser OAuth pop on
first use) only happens for ``push --run`` and ``login``.

Usage:
    uv run python storage/cli.py <command> [options]

Commands:
    status   What would sync: local scan vs the push manifest (offline).
    push     Upload new/changed files (dry-run unless --run).
    login    Sign in to the configured backend / show the account.

Config lives in the git-ignored ``datacli.toml`` (see ``storage/backends.py``
for the [sync] keys) and is editable via ``config set sync-*``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_REPO_ROOT), str(_REPO_ROOT / "eodhd")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _render  # type: ignore[import-not-found]  # noqa: E402
import config as cfg  # type: ignore[import-not-found]  # noqa: E402

from storage import engine  # noqa: E402
from storage.backends import (  # noqa: E402
    StorageBackend,
    SyncAuthError,
    SyncConfigError,
    make_backend,
)

PROG = "sync"
PLAN_ROWS_SHOWN = 40  # dry-run detail rows before "... and N more"


def _scan_kwargs(with_caches: bool) -> dict:
    if not with_caches:
        return {}
    # keep .sync excluded always; caches opt in via --with-caches
    return {
        "exclude_dirs": frozenset({".sync"}),
        "include": (*engine.DEFAULT_INCLUDE, "*.gz"),
    }


def _context() -> tuple[Path, dict, Path]:
    """Resolve (data_root, [sync] settings, manifest_path) from config."""
    root, _source = cfg.eodhd_data_root()
    if not root.exists():
        raise SyncConfigError(
            f"data root does not exist: {root}\n  set it with: config set data-root <path>"
        )
    settings = cfg.section("sync")
    backend_name = str(settings.get("backend") or "gdrive")
    manifest_path = root / ".sync" / f"{backend_name}.json"
    return root, settings, manifest_path


def _plan(
    root: Path, manifest: dict, *, with_caches: bool
) -> list[engine.PlanItem]:
    local = engine.scan_local(root, **_scan_kwargs(with_caches))
    return engine.build_plan(
        local, manifest, hasher=lambda rel: engine.md5_file(root / rel)
    )


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def _print_plan(
    console, plan: list[engine.PlanItem], backend: StorageBackend, *, run: bool
) -> None:
    from rich.text import Text

    summary = engine.summarize_plan(plan)
    mode = "RUN" if run else "DRY-RUN"
    title = Text("sync plan  ", style="bold")
    title.append(f"[{mode}]", style="bold red" if run else "yellow")
    title.append(f"  -> {backend.describe()}", style="dim")
    table = _render.minimal_table(title=title)
    table.add_column("#", justify="right", style="dim", no_wrap=True)
    table.add_column("action", no_wrap=True)
    table.add_column("file", no_wrap=True)
    table.add_column("size", justify="right", no_wrap=True)
    table.add_column("reason", style="dim", no_wrap=True)

    actionable = [p for p in plan if p.action in (engine.ACTION_UPLOAD, engine.ACTION_TOUCH)]
    for i, item in enumerate(actionable[:PLAN_ROWS_SHOWN], 1):
        style = "green" if item.action == engine.ACTION_UPLOAD else "cyan"
        table.add_row(
            str(i),
            Text(item.action, style=style),
            item.relpath,
            engine.human_size(item.size),
            item.reason,
        )
    console.print(table)
    if len(actionable) > PLAN_ROWS_SHOWN:
        console.print(
            Text(f"  ... and {len(actionable) - PLAN_ROWS_SHOWN} more", style="dim")
        )
    console.print(
        Text(
            f"{summary['upload']} upload ({engine.human_size(summary['upload_bytes'])})"
            f"   {summary['touch']} touch   {summary['skip']} unchanged"
            f"   {summary['orphan']} orphan (kept remotely)",
            style="dim",
        )
    )


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_status(argv: list[str]) -> int:
    """Offline view: what a push would do right now."""
    parser = argparse.ArgumentParser(prog=f"{PROG} status", description=cmd_status.__doc__)
    parser.add_argument("--with-caches", action="store_true", help="Include the payload cache dirs (many small files).")
    args = parser.parse_args(argv)

    root, settings, manifest_path = _context()
    backend = make_backend(settings)
    manifest = engine.load_manifest(manifest_path)
    plan = _plan(root, manifest, with_caches=args.with_caches)

    console = _render.make_console()
    from rich.text import Text

    console.print(Text(f"data root: {root}", style="dim"))
    console.print(Text(f"manifest:  {manifest_path}", style="dim"))
    _print_plan(console, plan, backend, run=False)
    if any(p.action == engine.ACTION_UPLOAD for p in plan):
        console.print(Text("push with:  sync push --run", style="dim"))
    return 0


def execute_push(
    root: Path,
    backend: StorageBackend,
    manifest_path: Path,
    *,
    run: bool,
    with_caches: bool = False,
    keep_going: bool = False,
    echo=print,
) -> int:
    """Plan and (optionally) execute a push. Testable core behind ``cmd_push``.

    The manifest is re-saved after every uploaded file, so an interrupted push
    resumes where it left off on the next run.
    """
    manifest = engine.load_manifest(manifest_path)
    plan = _plan(root, manifest, with_caches=with_caches)
    console = _render.make_console()
    _print_plan(console, plan, backend, run=run)

    todo = [p for p in plan if p.action in (engine.ACTION_UPLOAD, engine.ACTION_TOUCH)]
    if not todo:
        echo("Everything in sync.")
        return 0
    if not run:
        from rich.text import Text

        console.print(
            Text("dry-run only — re-run with --run to upload.", style="dim")
        )
        return 0

    label = backend.ensure_auth(interactive=True)
    echo(f"signed in: {label}")

    uploads = [p for p in todo if p.action == engine.ACTION_UPLOAD]
    failures: list[tuple[engine.PlanItem, Exception]] = []
    done_bytes = 0
    for i, item in enumerate(uploads, 1):
        local = root / item.relpath
        stat = engine.FileStat(item.relpath, local.stat().st_size, local.stat().st_mtime_ns)
        md5 = item.md5 or engine.md5_file(local)
        prev_id = manifest.get("files", {}).get(item.relpath, {}).get("remote_id")
        echo(f"[{i}/{len(uploads)}] {item.relpath} ({engine.human_size(item.size)}) ...")
        try:
            remote_id = backend.upload(local, item.relpath, remote_id=prev_id)
        except Exception as exc:  # noqa: BLE001 - report per-file, don't crash the batch
            failures.append((item, exc))
            echo(f"    -> FAILED: {exc}")
            if not keep_going:
                echo("Stopping (use --keep-going to continue past failures).")
                break
            continue
        engine.record_push(manifest, stat, md5=md5, remote_id=remote_id)
        engine.save_manifest(manifest_path, manifest)
        done_bytes += item.size

    # metadata-only refreshes (content identical, mtime moved) — no upload needed
    for item in (p for p in todo if p.action == engine.ACTION_TOUCH):
        local = root / item.relpath
        stat = engine.FileStat(item.relpath, local.stat().st_size, local.stat().st_mtime_ns)
        prev = manifest.get("files", {}).get(item.relpath, {})
        engine.record_push(
            manifest, stat, md5=item.md5 or prev.get("md5", ""), remote_id=prev.get("remote_id", "")
        )
    engine.save_manifest(manifest_path, manifest)

    ok = len(uploads) - len(failures)
    echo(
        f"\nPush finished: {ok} uploaded ({engine.human_size(done_bytes)}), "
        f"{len(failures)} failed."
    )
    for item, exc in failures:
        echo(f"  FAILED: {item.relpath}: {exc}")
    return 1 if failures else 0


def cmd_push(argv: list[str]) -> int:
    """Upload new/changed files to the configured backend (dry-run unless --run)."""
    parser = argparse.ArgumentParser(prog=f"{PROG} push", description=cmd_push.__doc__)
    parser.add_argument("--run", action="store_true", help="Actually upload (default: dry-run).")
    parser.add_argument("--with-caches", action="store_true", help="Include the payload cache dirs (many small files; slow on Drive).")
    parser.add_argument("--keep-going", action="store_true", help="Continue past per-file upload failures.")
    args = parser.parse_args(argv)

    root, settings, manifest_path = _context()
    backend = make_backend(settings)
    return execute_push(
        root,
        backend,
        manifest_path,
        run=args.run,
        with_caches=args.with_caches,
        keep_going=args.keep_going,
    )


def cmd_login(argv: list[str]) -> int:
    """Sign in to the configured backend (browser flow if needed); show the account."""
    parser = argparse.ArgumentParser(prog=f"{PROG} login", description=cmd_login.__doc__)
    parser.parse_args(argv)
    _root, settings, _manifest = _context()
    backend = make_backend(settings)
    print(f"signed in: {backend.ensure_auth(interactive=True)}")
    return 0


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #
def top_help() -> str:
    return (
        "sync - push the data root to personal cloud storage (one-way backup)\n\n"
        f"Usage:\n  {PROG} [status|push|login] [options]\n\n"
        "Commands:\n"
        "  status  What would sync (offline; no auth needed)\n"
        "  push    Upload new/changed files (dry-run unless --run)\n"
        "  login   Sign in to the configured backend / show the account\n\n"
        "Config (git-ignored datacli.toml, edit via `config set`):\n"
        "  sync-backend         gdrive | local\n"
        "  sync-remote-root     Drive folder path (default: datacli/eodhd)\n"
        "  sync-gdrive-secrets  path to your OAuth client JSON (GDRIVE_SETUP.md)\n"
        "  sync-local-dest      destination dir for the 'local' backend"
    )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("-h", "--help", "help"):
        print(top_help())
        return 0
    command, rest = (args[0], args[1:]) if args else ("status", [])
    handlers = {"status": cmd_status, "push": cmd_push, "login": cmd_login}
    if command not in handlers:
        print(f"unknown sync command: {command!r}\n\n{top_help()}", file=sys.stderr)
        return 2
    try:
        return handlers[command](rest)
    except (SyncConfigError, SyncAuthError) as exc:
        from rich.text import Text

        _render.make_console().print(Text(f"sync: {exc}", style="red"))
        return 2


if __name__ == "__main__":
    sys.exit(main())
