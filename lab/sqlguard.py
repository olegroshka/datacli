"""Read-only SQL guard -- the safety boundary for agent-generated queries.

A query is accepted only if it is a *single* statement whose leading keyword is
``SELECT`` or ``WITH`` and which contains no data-/schema-/setting-modifying
keyword anywhere (which also rejects ``WITH ... DELETE`` style CTEs and stacked
``SELECT ...; DROP ...`` statements). Accepted queries get a ``LIMIT`` appended if
they lack one; the executor caps the fetch regardless.

This is enforced in code, never left to the prompt.
"""

from __future__ import annotations

import re

# Keywords that must never appear in a read-only query (whole-word match).
FORBIDDEN = (
    "insert",
    "update",
    "delete",
    "create",
    "drop",
    "alter",
    "truncate",
    "replace",
    "merge",
    "attach",
    "detach",
    "copy",
    "export",
    "import",
    "install",
    "load",
    "pragma",
    "set",
    "reset",
    "call",
    "vacuum",
    "checkpoint",
    "begin",
    "commit",
    "rollback",
)


def _strip_comments(query: str) -> str:
    query = re.sub(r"/\*.*?\*/", " ", query, flags=re.DOTALL)  # block comments
    query = re.sub(r"--[^\n]*", " ", query)  # line comments
    return query.strip()


def _statements(query: str) -> list[str]:
    return [s.strip() for s in query.split(";") if s.strip()]


def validate(query: str, *, max_rows: int = 200) -> tuple[bool, str, str]:
    """Validate a query is read-only.

    Returns ``(ok, error, normalized)``. When ``ok`` is True, ``normalized`` is the
    query to execute (with a ``LIMIT`` appended if none was present); otherwise
    ``error`` explains the rejection.
    """
    stripped = _strip_comments(query)
    if not stripped:
        return False, "empty query", ""

    statements = _statements(stripped)
    if len(statements) != 1:
        return False, "only a single statement is allowed", ""
    stmt = statements[0]

    lead = re.match(r"[a-zA-Z_]+", stmt)
    keyword = lead.group(0).lower() if lead else ""
    if keyword not in ("select", "with"):
        shown = keyword or stmt[:12]
        return (
            False,
            f"only read-only SELECT/WITH queries are allowed (got '{shown}')",
            "",
        )

    lowered = stmt.lower()
    for bad in FORBIDDEN:
        if re.search(rf"\b{bad}\b", lowered):
            return False, f"query contains a forbidden keyword: {bad}", ""

    normalized = stmt
    if not re.search(r"\blimit\b", lowered):
        normalized = f"{stmt}\nLIMIT {max_rows}"
    return True, "", normalized
