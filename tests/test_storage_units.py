"""``sync`` over several roots: eodhd always, macro / finra when present, each its own unit."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_REPO_ROOT), str(_REPO_ROOT / "eodhd")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from storage import cli as sync_cli  # noqa: E402


def _roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, finra: bool
) -> dict[str, Path]:
    eodhd = tmp_path / "raw" / "eodhd"
    (eodhd / "us_common").mkdir(parents=True, exist_ok=True)
    (eodhd / "us_common" / "prices_daily.parquet").write_bytes(b"eodhd-bytes")
    dest = tmp_path / "backup" / "eodhd"
    settings = {
        "backend": "local",
        "local_dest": str(dest),
        "remote_root": "datacli/eodhd",
    }
    monkeypatch.setattr(sync_cli.cfg, "eodhd_data_root", lambda: (eodhd, "test"))
    monkeypatch.setattr(
        sync_cli.cfg, "section", lambda name: dict(settings) if name == "sync" else {}
    )
    monkeypatch.setenv(
        "DATACLI_MACRO_ROOT", str(tmp_path / "raw" / "macro")
    )  # absent: skipped
    roots = {"eodhd": eodhd, "dest": dest}
    if finra:
        finra_root = tmp_path / "raw" / "finra"
        (finra_root / "short_volume" / "daily").mkdir(parents=True, exist_ok=True)
        (finra_root / "short_volume" / "daily" / "2026-09-30.parquet").write_bytes(
            b"finra-bytes"
        )
        (finra_root / "short_volume" / "short_volume_fetch_state.csv").write_text(
            "date,status\n", encoding="utf-8"
        )
        monkeypatch.setenv("DATACLI_FINRA_ROOT", str(finra_root))
        roots["finra"] = finra_root
    else:
        monkeypatch.setenv("DATACLI_FINRA_ROOT", str(tmp_path / "raw" / "finra"))
    monkeypatch.setenv("DATACLI_NONINTERACTIVE", "1")
    return roots


def test_sibling_settings_point_next_to_the_eodhd_targets() -> None:
    settings = {
        "backend": "gdrive",
        "remote_root": "datacli/eodhd",
        "gdrive_token": "t",
    }
    out = sync_cli._sibling_settings(settings, "finra")
    assert out["remote_root"] == "datacli/finra" and out["gdrive_token"] == "t"
    assert settings["remote_root"] == "datacli/eodhd"  # the original is untouched
    assert (
        sync_cli._sibling_settings({"backend": "gdrive"}, "macro")["remote_root"]
        == "datacli/macro"
    )
    local = sync_cli._sibling_settings(
        {"backend": "local", "local_dest": "D:/backup/eodhd"}, "finra"
    )
    assert Path(local["local_dest"]) == Path("D:/backup/finra")


def test_units_skip_missing_siblings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _roots(tmp_path, monkeypatch, finra=False)
    assert [u.name for u in sync_cli._units()] == ["eodhd"]
    _roots(tmp_path, monkeypatch, finra=True)
    units = sync_cli._units()
    assert [u.name for u in units] == ["eodhd", "finra"]
    finra = units[1]
    assert finra.manifest_path == finra.root / ".sync" / "local.json"
    assert Path(finra.settings["local_dest"]) == tmp_path / "backup" / "finra"


def test_status_and_push_cover_every_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    roots = _roots(tmp_path, monkeypatch, finra=True)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "200")

    assert sync_cli.cmd_status([]) == 0
    out = capsys.readouterr().out
    assert "eodhd data root:" in out and "finra data root:" in out
    assert "push with:  sync push --run" in out

    assert sync_cli.cmd_push(["--run"]) == 0
    out = capsys.readouterr().out
    assert "== eodhd:" in out and "== finra:" in out
    assert (
        roots["dest"] / "us_common" / "prices_daily.parquet"
    ).read_bytes() == b"eodhd-bytes"
    finra_dest = tmp_path / "backup" / "finra"
    assert (
        finra_dest / "short_volume" / "daily" / "2026-09-30.parquet"
    ).read_bytes() == b"finra-bytes"
    assert (finra_dest / "short_volume" / "short_volume_fetch_state.csv").exists()
    assert (roots["finra"] / ".sync" / "local.json").exists()
    assert sync_cli.NOOP_SENTINEL not in out  # something was pushed

    # second push: nothing to do anywhere -> the sentinel appears exactly once, at the end
    assert sync_cli.cmd_push(["--run"]) == 0
    out = capsys.readouterr().out
    assert "eodhd: nothing to push" in out and "finra: nothing to push" in out
    assert out.count(sync_cli.NOOP_SENTINEL) == 1 and out.rstrip().endswith(
        sync_cli.NOOP_SENTINEL
    )

    # a change in one unit only: no sentinel, the other unit still reports nothing
    (roots["finra"] / "short_volume" / "daily" / "2026-10-01.parquet").write_bytes(
        b"more"
    )
    assert sync_cli.cmd_push(["--run"]) == 0
    out = capsys.readouterr().out
    assert "eodhd: nothing to push" in out and sync_cli.NOOP_SENTINEL not in out
    assert (finra_dest / "short_volume" / "daily" / "2026-10-01.parquet").exists()
