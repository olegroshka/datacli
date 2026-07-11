"""Subprocess runner for the restricted Python executor.

NOT imported in-process -- it is executed as ``python _pyexec_runner.py <workdir>``
so a hang or crash cannot take down the shell, and a wall-clock timeout is enforced
by the parent. Inside, it: pins matplotlib to a headless backend, loads the input
DataFrame, blocks network, restricts builtins (no open/os/subprocess and only a
whitelist of imports for the *user* code), execs the code, and writes back stdout /
error / whether a figure was produced.

Honest scope: this stops accidents (stray file writes, network, runaway loops via
the parent timeout) in a trusted, single-user, local context. It is NOT a hardened
security sandbox -- a determined escape via object traversal is not defended
against, which is acceptable here because the code author is an LLM over the user's
own local data, not an attacker.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

# imports the *user code* is allowed to make (by top-level package name)
_ALLOWED_IMPORTS = {
    "math",
    "statistics",
    "datetime",
    "collections",
    "itertools",
    "functools",
    "pandas",
    "numpy",
    "matplotlib",
}

_SAFE_BUILTIN_NAMES = [
    "abs",
    "all",
    "any",
    "bool",
    "bytes",
    "chr",
    "dict",
    "divmod",
    "enumerate",
    "filter",
    "float",
    "format",
    "frozenset",
    "getattr",
    "hasattr",
    "hash",
    "int",
    "isinstance",
    "issubclass",
    "iter",
    "len",
    "list",
    "map",
    "max",
    "min",
    "next",
    "ord",
    "pow",
    "print",
    "range",
    "repr",
    "reversed",
    "round",
    "set",
    "slice",
    "sorted",
    "str",
    "sum",
    "tuple",
    "type",
    "zip",
]


def _safe_builtins() -> dict:
    import builtins as _b

    def _guarded_import(name: str, *args: object, **kwargs: object) -> object:
        root = name.split(".")[0]
        if root in _ALLOWED_IMPORTS:
            return __import__(name, *args, **kwargs)  # type: ignore[arg-type]
        raise ImportError(f"import of '{name}' is not allowed in the lab executor")

    safe: dict = {n: getattr(_b, n) for n in _SAFE_BUILTIN_NAMES if hasattr(_b, n)}
    safe["__import__"] = _guarded_import
    safe["True"], safe["False"], safe["None"] = True, False, None
    return safe


def main() -> int:
    workdir = Path(sys.argv[1])
    result = {"ok": True, "stdout": "", "error": "", "figure": False}

    try:
        code = (workdir / "code.py").read_text(encoding="utf-8")

        import pandas as pd

        input_path = workdir / "input.parquet"
        df = pd.read_parquet(input_path) if input_path.exists() else pd.DataFrame()

        namespace: dict = {"df": df, "pd": pd}
        try:
            import numpy as np

            namespace["np"] = np
        except Exception:
            pass

        plt = None
        try:
            import matplotlib

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt  # type: ignore[no-redef]

            namespace["plt"] = plt
        except Exception:
            pass

        # block network for the user code
        import socket

        def _blocked(*_a: object, **_k: object) -> None:
            raise RuntimeError("network access is disabled in the lab executor")

        socket.socket = _blocked  # type: ignore[assignment]

        namespace["__builtins__"] = _safe_builtins()

        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                exec(compile(code, "<lab-code>", "exec"), namespace)
            if plt is not None and plt.get_fignums():
                plt.gcf().savefig(workdir / "figure.png", dpi=110, bbox_inches="tight")
                result["figure"] = True
        except Exception as exc:  # user-code error -> reported, not fatal
            result["ok"] = False
            result["error"] = f"{type(exc).__name__}: {exc}"
        result["stdout"] = out.getvalue()
    except Exception as exc:  # runner-level failure
        result["ok"] = False
        result["error"] = f"runner error: {type(exc).__name__}: {exc}"

    (workdir / "result.json").write_text(json.dumps(result), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
