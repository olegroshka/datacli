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

import shlex
import sys
from pathlib import Path
from typing import Any

import cmd2
from rich.console import Console
from rich.table import Table

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE / "eodhd"))
import cli as eodhd_cli  # type: ignore[import-not-found]  # noqa: E402
from eodhd_datasets import LANES  # type: ignore[import-not-found]  # noqa: E402

console = Console()

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


SOURCES: dict[str, SourcePlugin] = {"eodhd": EodhdPlugin()}


# --------------------------------------------------------------------------- #
# the shell
# --------------------------------------------------------------------------- #
class DataCli(cmd2.Cmd):
    """Interactive data-operations shell."""

    def __init__(self) -> None:
        super().__init__(allow_cli_args=False)
        self.current: str | None = None
        self.intro = (
            "datacli -- data operations shell. Type 'sources' to list, "
            "'source <name>' to enter one, 'help' for commands, 'quit' to exit."
        )
        self._apply_prompt()
        for noisy in (
            "edit",
            "macro",
            "run_pyscript",
            "run_script",
            "shell",
            "shortcuts",
            "ipy",
        ):
            if noisy not in self.hidden_commands:
                self.hidden_commands.append(noisy)

    # ----- slash-optional: strip a leading '/' before cmd2 parses ----------- #
    def onecmd_plus_hooks(self, line, *args, **kwargs):  # type: ignore[override]
        if isinstance(line, str):
            stripped = line.lstrip()
            if stripped.startswith("/"):
                line = stripped[1:]
        return super().onecmd_plus_hooks(line, *args, **kwargs)

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
        table = Table(title="data sources", title_style="bold")
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

    # ----- source-scoped commands ------------------------------------------- #
    def do_status(self, statement: object) -> None:
        """Show the source's data status."""
        self._dispatch("status", statement)

    def do_fetch(self, statement: object) -> None:
        """Fetch / refresh data (source-specific args, e.g. --fast --run)."""
        self._dispatch("fetch", statement)

    def do_qc(self, statement: object) -> None:
        """Run raw-data quality checks."""
        self._dispatch("qc", statement)

    def do_lanes(self, statement: object) -> None:
        """List the source's lanes / datasets."""
        self._dispatch("lanes", statement)

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


def main() -> int:
    return DataCli().cmdloop() or 0


if __name__ == "__main__":
    sys.exit(main())
