"""A tiny declarative command table shared by the ``macro`` and ``finra`` CLIs.

Each source CLI declares its commands once as :class:`Command` values (usage
line, one-liner, longer help, accepted flags) and everything else -- flag
parsing with did-you-mean hints, per-command help, the top-level help -- is
derived from that table. Help and usage errors never touch the network or the
disk; only a successful parse lets a command proceed.

Flag grammar: boolean flags map to ``True``; value flags accept both
``--name=value`` and ``--name value``; ``-h`` / ``--help`` win before anything
else. Anything that does not start with ``--`` is a positional.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class Flag:
    """A ``--flag`` a command accepts (``metavar`` set => takes a value)."""

    name: str
    help: str
    metavar: str = ""

    @property
    def label(self) -> str:
        return f"{self.name} {self.metavar}".rstrip()


@dataclass(frozen=True)
class Command:
    """One CLI command: name, one-liner, longer help, accepted flags.

    ``prog`` is the CLI's name (``macro``, ``finra``) so ``usage`` can be
    rendered without a module global.
    """

    name: str
    summary: str
    detail: str = ""
    flags: tuple[Flag, ...] = ()
    prog: str = ""
    positionals: str = ""  # e.g. "<what> <dataset>"; shown in the synopsis

    @property
    def synopsis(self) -> str:
        """``fetch [--provider <p>] [--run] [--full]`` -- no program prefix."""
        parts = [self.name]
        if self.positionals:
            parts.append(self.positionals)
        parts += [
            f"[{f.name} {f.metavar}]" if f.metavar else f"[{f.name}]"
            for f in self.flags
        ]
        return " ".join(parts)

    @property
    def usage(self) -> str:
        return f"{self.prog} {self.synopsis}".strip()


class HelpRequested(Exception):
    """``-h`` / ``--help`` seen: print the command's help and stop."""


class UsageError(Exception):
    """A flag we do not know or that is malformed; the message is user-facing."""


def parse(command: Command, argv: list[str]) -> tuple[dict[str, Any], list[str]]:
    """Split ``argv`` into ``(flags, positionals)`` for ``command``.

    Raises:
        HelpRequested: on ``-h`` / ``--help``.
        UsageError: on an unknown flag (with a did-you-mean hint), a value flag
            without a value, or a boolean flag given a value.
    """
    spec = {f.name: f for f in command.flags}
    if any(tok in ("-h", "--help") for tok in argv):
        raise HelpRequested()
    flags: dict[str, Any] = {}
    rest: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        i += 1
        if not tok.startswith("--"):
            rest.append(tok)
            continue
        name, eq, value = tok.partition("=")
        flag = spec.get(name)
        if flag is None:
            near = difflib.get_close_matches(name, list(spec), n=1, cutoff=0.5)
            hint = f" -- did you mean {near[0]}?" if near else ""
            raise UsageError(f"unknown flag {name}{hint}")
        if flag.metavar:
            if not eq:
                if i >= len(argv) or argv[i].startswith("--"):
                    raise UsageError(f"flag {name} needs a value ({flag.metavar})")
                value = argv[i]
                i += 1
            flags[name] = value
        else:
            if eq:
                raise UsageError(f"flag {name} does not take a value")
            flags[name] = True
    return flags, rest


def command_help(command: Command) -> str:
    """The per-command help block (usage, description, flags)."""
    rows = [(f.label, f.help) for f in command.flags]
    rows.append(("-h, --help", "show this help"))
    width = max(len(label) for label, _ in rows)
    lines = [
        f"usage: {command.usage}",
        "",
        command.detail or command.summary,
        "",
        "flags:",
    ]
    lines += [f"  {label:<{width}}  {text}" for label, text in rows]
    return "\n".join(lines)


def parse_or_exit(
    command: Command, argv: list[str], *, console: Any
) -> tuple[dict[str, Any], list[str], int | None]:
    """Parse ``argv``; the third item is an exit code when the caller is done.

    ``--help`` prints the command help and yields ``0``; a bad flag prints a
    friendly error plus the usage line on ``console`` and yields ``2``.
    Otherwise ``None`` and the caller proceeds with ``(flags, positionals)`` --
    only then may any network or disk work start.
    """
    try:
        flags, rest = parse(command, argv)
    except HelpRequested:
        print(command_help(command))
        return {}, [], 0
    except UsageError as exc:
        from rich.text import Text

        console.print(Text(str(exc), style="red"))
        console.print(
            Text(
                f"usage: {command.usage}   ({command.prog} {command.name} --help)",
                style="dim",
            )
        )
        return {}, [], 2
    return flags, rest, None


def bad_choice(
    console: Any, kind: str, value: str, choices: tuple[str, ...] | list[str]
) -> int:
    """Print ``unknown <kind> '<value>'`` with a did-you-mean hint; returns 2."""
    from rich.text import Text

    near = difflib.get_close_matches(value, list(choices), n=1)
    hint = f" -- did you mean {near[0]}?" if near else ""
    console.print(f"[red]unknown {kind} '{value}'[/red]{hint}")
    console.print(Text(f"choices: {'|'.join(choices)}", style="dim"))
    return 2


def commands_block(commands: Mapping[str, Command]) -> list[str]:
    """``  synopsis  summary`` lines for a top-level help, aligned."""
    rows = [(c.synopsis, c.summary) for c in commands.values()]
    width = max(len(u) for u, _ in rows)
    return [f"  {u:<{width}}  {s}" for u, s in rows]


def flags_block(flags: tuple[Flag, ...]) -> list[str]:
    """``  --flag <v>  help`` lines, aligned."""
    width = max(len(f.label) for f in flags)
    return [f"  {f.label:<{width}}  {f.help}" for f in flags]
