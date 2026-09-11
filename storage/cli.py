"""``sync`` — push the data root to personal cloud storage (backup, one-way).

Same culture as the eodhd CLI: everything is a dry-run plan until ``--run``.
``status`` and the plan never touch the network; auth (a browser OAuth pop on
first use) only happens for ``push --run``, ``reconcile`` and ``login``.

Usage:
    uv run python storage/cli.py <command> [options]

Commands:
    status     What would sync: local scan vs the push manifest (offline).
    push       Upload new/changed files (dry-run unless --run).
    reconcile  Rebuild the manifest from what the backend holds; find duplicates.
    login      Sign in to the configured backend / show the account.

Config lives in the git-ignored ``datacli.toml`` (see ``storage/backends.py``
for the [sync] keys) and is editable via ``config set sync-*``.

Push semantics (after the 2026-09-11 incident):

* An unreadable manifest fails the push loudly and is kept aside as
  ``<manifest>.corrupt-<stamp>``. It is never silently treated as empty.
* An empty/absent manifest against a backend that already holds files is
  rebuilt from the backend listing before anything is uploaded, so a lost
  manifest cannot produce a duplicated tree.
* Per-file uploads retry transient errors with exponential backoff, and the
  push continues past a file that still fails (``--fail-fast`` restores the
  old stop-at-first-failure behaviour). The exit code still reports failures.
* Uploads go metadata first, then newest-modified first.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Callable

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
DEFAULT_ATTEMPTS = 5  # per-file upload attempts (1 + 4 retries)
DEFAULT_BACKOFF = 2.0  # seconds; doubles per retry, capped at MAX_BACKOFF
MAX_BACKOFF = 60.0
EXIT_MANIFEST_UNREADABLE = 2


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


def _local(root: Path, *, with_caches: bool) -> dict[str, engine.FileStat]:
    return engine.scan_local(root, **_scan_kwargs(with_caches))


def _plan(root: Path, manifest: dict, *, with_caches: bool) -> list[engine.PlanItem]:
    local = _local(root, with_caches=with_caches)
    return engine.build_plan(
        local, manifest, hasher=lambda rel: engine.md5_file(root / rel)
    )


def _interactive() -> bool:
    return os.environ.get("DATACLI_NONINTERACTIVE") != "1"


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

    actionable = [
        p for p in plan if p.action in (engine.ACTION_UPLOAD, engine.ACTION_TOUCH)
    ]
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


def _manifest_line(manifest_path: Path, manifest: dict) -> str:
    count = len(manifest.get("files", {}))
    if count == 0:
        state = "empty"
    elif count == 1:
        state = "1 entry"
    else:
        state = f"{count} entries"
    return f"manifest:  {manifest_path} ({state})"


EMPTY_MANIFEST_NOTE = (
    "manifest is empty: a real push first rebuilds it from the backend so "
    "existing remote files are updated, not duplicated."
)


# --------------------------------------------------------------------------- #
# manifest handling shared by the commands
# --------------------------------------------------------------------------- #
def _load_manifest_or_quarantine(manifest_path: Path, echo) -> dict | None:
    """Load the manifest; on corruption move it aside, explain, return None."""
    try:
        return engine.load_manifest(manifest_path)
    except engine.ManifestError as exc:
        moved = engine.quarantine_manifest(exc.path)
        echo(
            f"sync: {exc}\n"
            f"  kept aside as: {moved}\n"
            "  The next push rebuilds the manifest from the backend before "
            "uploading; to do it now, run: sync reconcile --run"
        )
        return None


def _rebuild_from_remote(
    root: Path,
    backend: StorageBackend,
    *,
    with_caches: bool,
) -> tuple[dict, engine.RebuildReport] | None:
    """Rebuild a manifest from the backend listing; None if unsupported."""
    remote = backend.list_remote()
    if remote is None:
        return None
    local = _local(root, with_caches=with_caches)
    return engine.rebuild_manifest(
        local, remote, hasher=lambda rel: engine.md5_file(root / rel)
    )


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_status(argv: list[str]) -> int:
    """Offline view: what a push would do right now."""
    parser = argparse.ArgumentParser(
        prog=f"{PROG} status", description=cmd_status.__doc__
    )
    parser.add_argument(
        "--with-caches",
        action="store_true",
        help="Include the payload cache dirs (many small files).",
    )
    args = parser.parse_args(argv)

    root, settings, manifest_path = _context()
    backend = make_backend(settings)
    console = _render.make_console()
    from rich.text import Text

    console.print(Text(f"data root: {root}", style="dim"))
    try:
        manifest = engine.load_manifest(manifest_path)
    except engine.ManifestError as exc:
        console.print(Text(f"sync: {exc}", style="red"))
        console.print(
            Text("  fix with: sync reconcile --run   (rebuilds it from the backend)")
        )
        return EXIT_MANIFEST_UNREADABLE
    console.print(Text(_manifest_line(manifest_path, manifest), style="dim"))
    plan = _plan(root, manifest, with_caches=args.with_caches)
    _print_plan(console, plan, backend, run=False)
    has_uploads = any(p.action == engine.ACTION_UPLOAD for p in plan)
    if has_uploads and not manifest.get("files"):
        console.print(Text(EMPTY_MANIFEST_NOTE, style="yellow"))
    if has_uploads:
        console.print(Text("push with:  sync push --run", style="dim"))
    return 0


def _upload_with_retry(
    backend: StorageBackend,
    local: Path,
    relpath: str,
    prev_id: str | None,
    *,
    attempts: int,
    backoff: float,
    max_backoff: float,
    sleep: Callable[[float], None],
    echo,
) -> str:
    """``backend.upload`` with bounded exponential backoff on transient errors."""
    for attempt in range(1, attempts + 1):
        try:
            return backend.upload(local, relpath, remote_id=prev_id)
        except Exception as exc:  # noqa: BLE001 - classified by the backend
            if attempt >= attempts or not backend.retryable(exc):
                raise
            wait = min(max_backoff, backoff * (2 ** (attempt - 1)))
            echo(
                f"    retry {attempt}/{attempts - 1} in {wait:.0f}s: "
                f"{type(exc).__name__}: {exc}"
            )
            sleep(wait)
    raise AssertionError("unreachable")  # pragma: no cover


def execute_push(
    root: Path,
    backend: StorageBackend,
    manifest_path: Path,
    *,
    run: bool,
    with_caches: bool = False,
    keep_going: bool = True,
    attempts: int = DEFAULT_ATTEMPTS,
    backoff: float = DEFAULT_BACKOFF,
    max_backoff: float = MAX_BACKOFF,
    sleep: Callable[[float], None] = time.sleep,
    echo=print,
) -> int:
    """Plan and (optionally) execute a push. Testable core behind ``cmd_push``.

    The manifest is re-saved after every uploaded file, so an interrupted push
    resumes where it left off on the next run.
    """
    manifest = _load_manifest_or_quarantine(manifest_path, echo)
    if manifest is None:
        return EXIT_MANIFEST_UNREADABLE

    console = _render.make_console()
    signed_in = False
    if run and not manifest.get("files"):
        # Lost or first manifest: recover from the backend, never by re-creating.
        echo(f"signed in: {backend.ensure_auth(interactive=_interactive())}")
        signed_in = True
        rebuilt = _rebuild_from_remote(root, backend, with_caches=with_caches)
        if rebuilt is not None:
            manifest, report = rebuilt
            if report.remote_paths:
                engine.save_manifest(manifest_path, manifest)
                echo(
                    "manifest was empty but the backend already holds files; "
                    f"rebuilt it from {backend.describe()}: {report.summary()}"
                )
                for relpath, kept, losers in report.duplicates[:PLAN_ROWS_SHOWN]:
                    echo(
                        f"    duplicate: {relpath}  kept={kept}  "
                        f"other={','.join(losers)}"
                    )
                if report.duplicates:
                    echo(
                        "    duplicates are never trashed automatically: "
                        "review with `sync reconcile` and use --trash-duplicates"
                    )
            else:
                echo("manifest is empty and the backend holds no files: first push")

    plan = _plan(root, manifest, with_caches=with_caches)
    _print_plan(console, plan, backend, run=run)

    todo = [p for p in plan if p.action in (engine.ACTION_UPLOAD, engine.ACTION_TOUCH)]
    if not todo:
        echo("Everything in sync.")
        return 0
    if not run:
        from rich.text import Text

        if not manifest.get("files"):
            console.print(Text(EMPTY_MANIFEST_NOTE, style="yellow"))
        console.print(Text("dry-run only — re-run with --run to upload.", style="dim"))
        return 0

    if not signed_in:
        echo(f"signed in: {backend.ensure_auth(interactive=_interactive())}")

    uploads = [p for p in todo if p.action == engine.ACTION_UPLOAD]
    failures: list[tuple[engine.PlanItem, Exception]] = []
    completed_uploads = 0
    done_bytes = 0
    for i, item in enumerate(uploads, 1):
        local = root / item.relpath
        stat = engine.FileStat(
            item.relpath, local.stat().st_size, local.stat().st_mtime_ns
        )
        md5 = item.md5 or engine.md5_file(local)
        prev_id = manifest.get("files", {}).get(item.relpath, {}).get("remote_id")
        echo(
            f"[{i}/{len(uploads)}] {item.relpath} ({engine.human_size(item.size)}) ..."
        )
        try:
            remote_id = _upload_with_retry(
                backend,
                local,
                item.relpath,
                prev_id,
                attempts=attempts,
                backoff=backoff,
                max_backoff=max_backoff,
                sleep=sleep,
                echo=echo,
            )
        except (
            Exception
        ) as exc:  # noqa: BLE001 - report per-file, don't crash the batch
            failures.append((item, exc))
            echo(f"    -> FAILED: {exc}")
            if not keep_going:
                echo("Stopping (--fail-fast).")
                break
            continue
        engine.record_push(manifest, stat, md5=md5, remote_id=remote_id)
        engine.save_manifest(manifest_path, manifest)
        done_bytes += item.size
        completed_uploads += 1

    # metadata-only refreshes (content identical, mtime moved) — no upload needed
    for item in (p for p in todo if p.action == engine.ACTION_TOUCH):
        local = root / item.relpath
        stat = engine.FileStat(
            item.relpath, local.stat().st_size, local.stat().st_mtime_ns
        )
        prev = manifest.get("files", {}).get(item.relpath, {})
        engine.record_push(
            manifest,
            stat,
            md5=item.md5 or prev.get("md5", ""),
            remote_id=prev.get("remote_id", ""),
        )
    engine.save_manifest(manifest_path, manifest)

    echo(
        f"\nPush finished: {completed_uploads} uploaded "
        f"({engine.human_size(done_bytes)}), {len(failures)} failed, "
        f"{len(uploads) - completed_uploads - len(failures)} not run."
    )
    for item, exc in failures:
        echo(f"  FAILED: {item.relpath}: {exc}")
    return 1 if failures else 0


def cmd_push(argv: list[str]) -> int:
    """Upload new/changed files to the configured backend (dry-run unless --run)."""
    parser = argparse.ArgumentParser(prog=f"{PROG} push", description=cmd_push.__doc__)
    parser.add_argument(
        "--run", action="store_true", help="Actually upload (default: dry-run)."
    )
    parser.add_argument(
        "--with-caches",
        action="store_true",
        help="Include the payload cache dirs (many small files; slow on Drive).",
    )
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="Continue past per-file upload failures (the default; kept for scripts).",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop at the first file that still fails after retries.",
    )
    args = parser.parse_args(argv)
    if args.keep_going and args.fail_fast:
        parser.error("--keep-going and --fail-fast are mutually exclusive")

    root, settings, manifest_path = _context()
    backend = make_backend(settings)
    return execute_push(
        root,
        backend,
        manifest_path,
        run=args.run,
        with_caches=args.with_caches,
        keep_going=not args.fail_fast,
    )


def execute_reconcile(
    root: Path,
    backend: StorageBackend,
    manifest_path: Path,
    *,
    run: bool,
    trash_duplicates: bool = False,
    with_caches: bool = False,
    report_path: Path | None = None,
    echo=print,
) -> int:
    """List the backend, match it to local files, optionally rebuild the manifest.

    Read-only unless ``run`` (rewrites the manifest after backing it up) and
    ``trash_duplicates`` (soft-deletes every non-chosen copy, after listing).
    """
    echo(f"signed in: {backend.ensure_auth(interactive=_interactive())}")
    rebuilt = _rebuild_from_remote(root, backend, with_caches=with_caches)
    if rebuilt is None:
        echo(f"sync: {backend.describe()} cannot list its files; reconcile unsupported")
        return 2
    manifest, report = rebuilt
    echo(f"reconcile {backend.describe()}: {report.summary()}")
    for relpath, kept, losers in report.duplicates:
        echo(
            f"  duplicate: {relpath}\n      keep  {kept}\n      other "
            + ", ".join(losers)
        )

    if report_path is not None:
        payload = {
            "backend": backend.describe(),
            "summary": {
                "matched": report.matched,
                "differs": report.differs,
                "remote_only": report.remote_only,
                "local_only": report.local_only,
                "duplicates": len(report.duplicates),
            },
            "duplicates": [
                {"relpath": relpath, "keep": kept, "other": list(losers)}
                for relpath, kept, losers in report.duplicates
            ],
            "files": manifest["files"],
        }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
        echo(f"report written: {report_path}")

    if not run:
        tail = " and trash the duplicates." if trash_duplicates else "."
        echo(f"dry-run only — re-run with --run to rewrite the manifest{tail}")
        return 0

    backup = engine.backup_manifest(manifest_path)
    engine.save_manifest(manifest_path, manifest)
    kept = f"; previous copy kept as {backup}" if backup else ""
    echo(f"manifest rebuilt: {manifest_path} ({len(manifest['files'])} entries){kept}")
    if trash_duplicates:
        trashed = 0
        for relpath, _kept, losers in report.duplicates:
            for loser in losers:
                backend.trash(loser)
                trashed += 1
                echo(f"  trashed {loser}  ({relpath})")
        echo(f"{trashed} duplicate copy/copies moved to trash")
    return 0


def cmd_reconcile(argv: list[str]) -> int:
    """Rebuild the manifest from the backend's own listing; report duplicates."""
    parser = argparse.ArgumentParser(
        prog=f"{PROG} reconcile", description=cmd_reconcile.__doc__
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Rewrite the manifest (a .bak copy is kept). Default: report only.",
    )
    parser.add_argument(
        "--trash-duplicates",
        action="store_true",
        help="With --run: move every non-chosen duplicate copy to the backend's trash.",
    )
    parser.add_argument(
        "--with-caches",
        action="store_true",
        help="Match the payload cache dirs too (only if they were ever pushed).",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Write the full reconcile result (per-path ids, duplicates) as JSON.",
    )
    args = parser.parse_args(argv)
    if args.trash_duplicates and not args.run:
        parser.error("--trash-duplicates needs --run")

    root, settings, manifest_path = _context()
    backend = make_backend(settings)
    return execute_reconcile(
        root,
        backend,
        manifest_path,
        run=args.run,
        trash_duplicates=args.trash_duplicates,
        with_caches=args.with_caches,
        report_path=args.report,
    )


def cmd_login(argv: list[str]) -> int:
    """Sign in to the configured backend (browser flow if needed); show the account."""
    parser = argparse.ArgumentParser(
        prog=f"{PROG} login", description=cmd_login.__doc__
    )
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
        f"Usage:\n  {PROG} [status|push|reconcile|login] [options]\n\n"
        "Commands:\n"
        "  status     What would sync (offline; no auth needed)\n"
        "  push       Upload new/changed files (dry-run unless --run)\n"
        "  reconcile  Rebuild the manifest from the backend; list duplicate copies\n"
        "  login      Sign in to the configured backend / show the account\n\n"
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
    handlers = {
        "status": cmd_status,
        "push": cmd_push,
        "reconcile": cmd_reconcile,
        "login": cmd_login,
    }
    if command not in handlers:
        print(f"unknown sync command: {command!r}\n\n{top_help()}", file=sys.stderr)
        return 2
    try:
        mutating_reconcile = command == "reconcile" and "--run" in rest
        if command == "push" or mutating_reconcile:
            from scheduler.commands import direct_mutation_lock

            # reconcile --run rewrites the same manifest a scheduled push owns,
            # so it takes the `sync push --run` resource locks.
            lock_argv = ["--run"] if mutating_reconcile else rest
            with direct_mutation_lock("sync", "push", lock_argv):
                return handlers[command](rest)
        return handlers[command](rest)
    except (SyncConfigError, SyncAuthError) as exc:
        from rich.text import Text

        _render.make_console().print(Text(f"sync: {exc}", style="red"))
        return 2


if __name__ == "__main__":
    sys.exit(main())
