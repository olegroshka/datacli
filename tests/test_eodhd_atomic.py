"""eodhd/_atomic: temp-file + rename, with a bounded retry on Windows sharing violations."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_EODHD = _REPO_ROOT / "eodhd"
if str(_EODHD) not in sys.path:
    sys.path.insert(0, str(_EODHD))

import _atomic  # type: ignore  # noqa: E402


def test_to_csv_replaces_atomically_and_cleans_tmp(tmp_path: Path) -> None:
    target = tmp_path / "state.csv"
    _atomic.to_csv(pd.DataFrame({"a": [1]}), target, index=False)
    _atomic.to_csv(pd.DataFrame({"a": [2]}), target, index=False)
    assert target.read_text(encoding="utf-8").splitlines() == ["a", "2"]
    assert not list(tmp_path.glob("*.tmp"))


def test_replace_retries_transient_permission_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp, target = tmp_path / "x.tmp", tmp_path / "x"
    tmp.write_text("new", encoding="utf-8")
    target.write_text("old", encoding="utf-8")
    calls = {"n": 0}
    real_replace = _atomic.os.replace

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(5, "Access is denied")
        real_replace(src, dst)

    monkeypatch.setattr(_atomic.os, "replace", flaky)
    waits: list[float] = []
    _atomic.replace_with_retry(tmp, target, sleep=waits.append)
    assert target.read_text(encoding="utf-8") == "new"
    assert calls["n"] == 3 and waits == [0.25, 0.5]


def test_replace_gives_up_after_the_last_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp, target = tmp_path / "x.tmp", tmp_path / "x"
    tmp.write_text("new", encoding="utf-8")

    def always(src, dst):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(_atomic.os, "replace", always)
    waits: list[float] = []
    with pytest.raises(PermissionError):
        _atomic.replace_with_retry(tmp, target, attempts=3, sleep=waits.append)
    assert waits == [0.25, 0.5]  # no pause after the final failure
