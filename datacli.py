"""datacli -- an interactive data-operations shell.

A cmd2 REPL that unifies the operational tooling across data sources. You enter a
source context and run source-scoped commands:

    data> /source eodhd
    eodhd> /status
    eodhd> /fetch --fast --run
    eodhd> /qc

The leading ``/`` is optional (``status`` and ``/status`` both work). Global
commands (``source``, ``sources``, ``help``, ``quit``) work anywhere; source
commands (``status``, ``fetch``, ``qc``, ``lanes``, ``probe``, ``config``,
``describe``, ``find``, ``rows``, ``coverage``, ``sql``, ``schema``, ``reindex``)
require an active source. Each source is a plugin in ``SOURCES``; the ``eodhd``
plugin reuses ``eodhd/cli.py``.

Usage:
    uv run python datacli.py
"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path
from typing import Any

import cmd2
from cmd2 import plugin

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE / "eodhd"))
import _render  # type: ignore[import-not-found]  # noqa: E402
import cli as eodhd_cli  # type: ignore[import-not-found]  # noqa: E402
from eodhd_datasets import LANES  # type: ignore[import-not-found]  # noqa: E402

console = _render.make_console()

# Data-source adapters that can currently only *load* (no ops tooling yet) --
# shown in `sources` for context so the roadmap is visible.
LOAD_ONLY: dict[str, str] = {
    "fred": "FRED economic series (adapter; plugin deferred)",
    "yahoo": "Yahoo Finance",
    "csv": "Local CSV files",
    "parquet": "Local parquet files",
}


# --------------------------------------------------------------------------- #
# source plugins
# --------------------------------------------------------------------------- #
class SourcePlugin:
    """Operational commands for one data source."""

    name: str = ""
    summary: str = ""

    def command_names(self) -> list[str]:
        raise NotImplementedError

    def detail(self) -> str:
        return ""

    def run(self, command: str, argv: list[str]) -> int:
        raise NotImplementedError


class EodhdPlugin(SourcePlugin):
    """eodhd source -- delegates to the eodhd CLI (scripts/eodhd/cli.py)."""

    name = "eodhd"
    summary = "US/UK-EU equities, ETFs, indices, fundamentals"

    # query verbs run in-process against a warm DuckDB connection (snappy,
    # skips per-command process startup); everything else delegates to the CLI.
    EXPLORE_QUERY = frozenset({"describe", "find", "rows", "coverage", "sql"})

    def __init__(self) -> None:
        self._con: Any = None  # warm DuckDB connection, lazily built

    # shell command -> eodhd CLI subcommand
    COMMAND_MAP = {
        "status": "status",
        "fetch": "refresh",
        "qc": "qc",
        "lanes": "lanes",
        "probe": "probe",
        "describe": "describe",
        "find": "find",
        "rows": "rows",
        "coverage": "coverage",
        "sql": "sql",
        "config": "config",
        "schema": "schema",
        "reindex": "reindex",
    }

    def command_names(self) -> list[str]:
        return list(self.COMMAND_MAP.keys())

    def detail(self) -> str:
        return f"{len(LANES)} lanes"

    def build_argv(self, command: str, argv: list[str]) -> list[str]:
        """Map a shell command + args to the eodhd CLI argv."""
        return [self.COMMAND_MAP[command], *argv]

    def run(self, command: str, argv: list[str]) -> int:
        if command in self.EXPLORE_QUERY:
            return self._explore(command, argv)
        # anything else may change data/config -> drop the warm connection so the
        # next query rebuilds against the current state.
        self._con = None
        try:
            return int(eodhd_cli.main(self.build_argv(command, argv)) or 0)
        except SystemExit as exc:  # argparse errors must not kill the shell
            return int(exc.code or 0)

    def _explore(self, command: str, argv: list[str]) -> int:
        import explore_eodhd as ex  # type: ignore[import-not-found]

        if self._con is None:
            self._con = ex.connect()
        try:
            return int(ex.main([command, *argv], con=self._con) or 0)
        except SystemExit as exc:
            return int(exc.code or 0)


class MacroPlugin(SourcePlugin):
    """macro source -- FRED + EODHD macro series (a peer of eodhd, not part of it)."""

    name = "macro"
    summary = "FRED + EODHD macro series (rates, country indicators, index/FX)"
    COMMANDS = ("status", "list", "fetch")

    def command_names(self) -> list[str]:
        return list(self.COMMANDS)

    def detail(self) -> str:
        from macro import registry as reg

        return f"{len(reg.FRED_SERIES)} FRED + EODHD"

    def run(self, command: str, argv: list[str]) -> int:
        import macro.cli as macro_cli

        try:
            return int(macro_cli.main([command, *argv]) or 0)
        except SystemExit as exc:
            return int(exc.code or 0)


SOURCES: dict[str, SourcePlugin] = {"eodhd": EodhdPlugin(), "macro": MacroPlugin()}


# --------------------------------------------------------------------------- #
# the shell
# --------------------------------------------------------------------------- #
class DataCli(cmd2.Cmd):
    """Interactive data-operations shell."""

    # We replace cmd2's built-in `macro` (command-recording) with our data command.
    # Neutralise its argparse subcommands so cmd2's subcommand registration doesn't
    # try to attach `macro create/delete/list` to our non-argparse do_macro.
    _macro_create = None  # type: ignore[assignment]
    _macro_delete = None  # type: ignore[assignment]
    _macro_list = None  # type: ignore[assignment]

    def __init__(self) -> None:
        # Register '/' as a shortcut that expands to nothing, so the leading slash
        # is optional (`/qc` == `qc`) for BOTH execution and tab-completion --
        # cmd2 resolves `/qc us_<tab>` to the qc completer natively.
        super().__init__(
            allow_cli_args=False,
            shortcuts={**cmd2.DEFAULT_SHORTCUTS, "/": ""},
        )
        self.current: str | None = None
        self.intro = (
            "datacli -- data operations shell. Type 'sources' to list, "
            "'source <name>' to enter one, 'help' for commands, "
            "'quit' (or 'exit') to leave."
        )
        self._apply_prompt()
        for noisy in (
            "edit",
            "run_pyscript",
            "run_script",
            "shell",
            "shortcuts",
            "ipy",
        ):
            if noisy not in self.hidden_commands:
                self.hidden_commands.append(noisy)

        from datetime import datetime

        self._session_stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.register_postcmd_hook(self._log_command)

    def _log_command(self, data: plugin.PostcommandData) -> plugin.PostcommandData:
        """cmd2 post-command hook: append each command to a per-session log.

        A lightweight, always-on transcript for observability -- one tab-separated
        line per command (time, source context, raw input). Never breaks the shell.
        """
        try:
            from datetime import datetime

            raw = (getattr(data.statement, "raw", None) or str(data.statement)).strip()
            if raw:
                logdir = _HERE / ".datacli_logs"
                logdir.mkdir(exist_ok=True)
                stamp = datetime.now().isoformat(timespec="seconds")
                with (logdir / f"session_{self._session_stamp}.log").open(
                    "a", encoding="utf-8"
                ) as fh:
                    fh.write(f"{stamp}\t{self.current or '-'}\t{raw}\n")
        except Exception:
            pass
        return data

    def _apply_prompt(self) -> None:
        self.prompt = f"{self.current}> " if self.current else "data> "

    @staticmethod
    def _argv(statement: object) -> list[str]:
        arg_list = getattr(statement, "arg_list", None)
        if arg_list is not None:
            return list(arg_list)
        return shlex.split(str(statement))

    def _dispatch(self, command: str, statement: object) -> None:
        if self.current is None:
            self.perror("no source selected -- use: source <name>")
            return
        plugin = SOURCES[self.current]
        if command not in plugin.command_names():
            self.perror(f"'{self.current}' has no '{command}' command")
            return
        plugin.run(command, self._argv(statement))

    # ----- global commands -------------------------------------------------- #
    def do_sources(self, _statement: object) -> None:
        """List data sources."""
        table = _render.boxed_table(title="data sources")
        table.add_column("source", style="cyan")
        table.add_column("summary")
        table.add_column("commands")
        for name, plugin in SOURCES.items():
            table.add_row(name, plugin.summary, " ".join(plugin.command_names()))
        for name, desc in LOAD_ONLY.items():
            table.add_row(name, desc, "[dim]load-only (no ops tooling yet)[/dim]")
        console.print(table)

    def do_source(self, statement: object) -> None:
        """Enter a source context:  source <name>"""
        argv = self._argv(statement)
        if not argv:
            self.poutput(f"current source: {self.current or '(none)'}")
            return
        name = argv[0].lower()
        if name not in SOURCES:
            hint = " (load-only, no ops tooling yet)" if name in LOAD_ONLY else ""
            self.perror(
                f"no operational source '{name}'{hint}. Available: {', '.join(SOURCES)}"
            )
            return
        self.current = name
        self._apply_prompt()
        self.poutput(f"-> {name}: {SOURCES[name].summary}")

    def do_back(self, _statement: object) -> None:
        """Leave the current source context."""
        self.current = None
        self._apply_prompt()

    def do_clear(self, _statement: object) -> None:
        """Clear the terminal screen."""
        # Rich's console.clear() only rewinds the visible viewport; the OS clear
        # (cls/clear) actually resets the buffer, which is what users expect.
        os.system("cls" if os.name == "nt" else "clear")

    def do_exit(self, statement: Any) -> bool:
        """Exit the shell (alias for quit)."""
        return bool(self.do_quit(statement))

    def do_lab(self, statement: object) -> None:
        """Raw Data Lab management:  lab config | agents | skills | run <skill>"""
        import lab.cli as lab_cli  # lazy: keeps shell startup free of lab deps

        lab_cli.main(self._argv(statement))

    def do_ask(self, statement: object) -> None:
        """Ask the default lab persona a grounded question:  ask "worst coverage?" """
        import lab.cli as lab_cli

        lab_cli.main(["ask", *self._argv(statement)])

    def do_agent(self, statement: object) -> None:
        """Ask a named persona:  agent auditor "check us_common splits" """
        import lab.cli as lab_cli

        lab_cli.main(["agent", *self._argv(statement)])

    def do_investigate(self, statement: object) -> None:
        """Multi-agent investigation (generator -> skeptic -> reporter):
        investigate "post-dividend volume patterns in us_common" """
        import lab.cli as lab_cli

        lab_cli.main(["investigate", *self._argv(statement)])

    def do_macro(self, statement: object) -> None:
        """Shortcut to the macro source from anywhere:  macro list | status | fetch [--run]

        `macro` is a first-class source (see `sources`); this is just a shortcut so
        you don't have to `source macro` first. Equivalent to `source macro` + status.
        """
        import macro.cli as macro_cli

        macro_cli.main(self._argv(statement))

    # ----- source-scoped commands ------------------------------------------- #
    def do_status(self, statement: object) -> None:
        """Show the CURRENT source's data status (each source has its own datasets)."""
        self._dispatch("status", statement)
        # A source's status only covers that source's datasets. Point at the peers
        # so `status` never leaves the analyst wondering where the other data went.
        others = [n for n in SOURCES if n != self.current]
        if self.current is not None and others:
            tips = "   ".join(f"`source {n}` -> status" for n in others)
            console.print(f"[dim]other sources: {tips}[/dim]")

    def do_fetch(self, statement: object) -> None:
        """Fetch / refresh data (source-specific args, e.g. --fast --run)."""
        self._dispatch("fetch", statement)

    def do_qc(self, statement: object) -> None:
        """Run raw-data quality checks."""
        self._dispatch("qc", statement)

    def do_lanes(self, statement: object) -> None:
        """List the source's lanes / datasets."""
        self._dispatch("lanes", statement)

    def do_list(self, statement: object) -> None:
        """List the current source's catalog (macro: FRED series / indicators / symbols)."""
        self._dispatch("list", statement)

    def do_probe(self, statement: object) -> None:
        """Ad-hoc availability probe (source-specific args)."""
        self._dispatch("probe", statement)

    def do_config(self, statement: object) -> None:
        """Show/edit config:  config set data-root <path>"""
        self._dispatch("config", statement)

    def do_schema(self, statement: object) -> None:
        """Show the declared schema version and drift vs the on-disk data."""
        self._dispatch("schema", statement)

    def do_reindex(self, statement: object) -> None:
        """(Re)build the fast query catalog after new data."""
        self._dispatch("reindex", statement)

    # ----- exploration verbs ------------------------------------------------ #
    def do_describe(self, statement: object) -> None:
        """Everything about one ticker across datasets:  describe VAR.OL"""
        self._dispatch("describe", statement)

    def do_find(self, statement: object) -> None:
        """Locate a ticker (lane / exchange / datasets):  find VAR"""
        self._dispatch("find", statement)

    def do_rows(self, statement: object) -> None:
        """Actual rows for a ticker in a dataset:  rows VAR.OL dividends"""
        self._dispatch("rows", statement)

    def do_coverage(self, statement: object) -> None:
        """Do the datasets cover a ticker equally?  coverage VAR.OL"""
        self._dispatch("coverage", statement)

    def do_sql(self, statement: object) -> None:
        """Raw DuckDB query over the datasets:  sql "select ..." """
        self._dispatch("sql", statement)

    # ----- tab-completion (known lanes / datasets / sub-commands) ------------ #
    _LANES = (*LANES, "all")
    _QC_DATASETS = ("prices", "dividends", "splits")
    _ROW_DATASETS = ("prices", "dividends", "splits", "fundamentals")
    _CONFIG_ACTIONS = ("show", "get", "set")
    _FETCH_FLAGS = (
        "--fast",
        "--run",
        "--days",
        "--datasets",
        "--with-fundamentals",
        "--no-universe",
        "--keep-going",
        "--full-refresh",
        "--to",
        "--limit",
        "--tickers",
    )

    def _arg_pos(self, line: str, begidx: int, endidx: int) -> int:
        """1-based position of the argument being completed (the command is 0)."""
        tokens, _ = self.tokens_for_completion(line, begidx, endidx)
        return max(len(tokens) - 1, 0)

    def _by_position(
        self,
        text: str,
        line: str,
        begidx: int,
        endidx: int,
        by_pos: dict[int, tuple[str, ...]],
    ) -> Any:
        """Complete against the choice list for the current positional argument."""
        choices = by_pos.get(self._arg_pos(line, begidx, endidx), ())
        return self.basic_complete(text, line, begidx, endidx, choices)

    def complete_qc(self, text: str, line: str, begidx: int, endidx: int) -> Any:
        return self._by_position(
            text, line, begidx, endidx, {1: self._LANES, 2: self._QC_DATASETS}
        )

    def complete_status(self, text: str, line: str, begidx: int, endidx: int) -> Any:
        return self._by_position(text, line, begidx, endidx, {1: self._LANES})

    def complete_rows(self, text: str, line: str, begidx: int, endidx: int) -> Any:
        return self._by_position(text, line, begidx, endidx, {2: self._ROW_DATASETS})

    def complete_config(self, text: str, line: str, begidx: int, endidx: int) -> Any:
        return self._by_position(
            text, line, begidx, endidx, {1: self._CONFIG_ACTIONS, 2: ("data-root",)}
        )

    def complete_fetch(self, text: str, line: str, begidx: int, endidx: int) -> Any:
        return self.basic_complete(
            text, line, begidx, endidx, [*self._LANES, *self._FETCH_FLAGS]
        )

    def complete_source(self, text: str, line: str, begidx: int, endidx: int) -> Any:
        return self.basic_complete(text, line, begidx, endidx, [*SOURCES, *LOAD_ONLY])

    def complete_agent(self, text: str, line: str, begidx: int, endidx: int) -> Any:
        from lab import registry

        return self._by_position(
            text, line, begidx, endidx, {1: tuple(registry.load_personas())}
        )

    def complete_macro(self, text: str, line: str, begidx: int, endidx: int) -> Any:
        return self._by_position(
            text, line, begidx, endidx, {1: ("list", "status", "fetch")}
        )

    def complete_lab(self, text: str, line: str, begidx: int, endidx: int) -> Any:
        pos = self._arg_pos(line, begidx, endidx)
        if pos == 1:
            return self.basic_complete(
                text, line, begidx, endidx, ("config", "agents", "skills", "run")
            )
        tokens, _ = self.tokens_for_completion(line, begidx, endidx)
        if pos == 2 and len(tokens) > 1 and tokens[1] == "run":
            from lab import registry

            return self.basic_complete(
                text, line, begidx, endidx, tuple(registry.load_skills())
            )
        return self.basic_complete(text, line, begidx, endidx, ())


def main() -> int:
    return DataCli().cmdloop() or 0


if __name__ == "__main__":
    sys.exit(main())
