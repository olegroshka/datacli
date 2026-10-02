"""``finra`` CLI: help and usage errors never touch the network; status is offline."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from finra import api, auth  # noqa: E402
from finra import cli as finra_cli  # noqa: E402
from finra.errors import EntitlementError  # noqa: E402


def _boom(*args: object, **kwargs: object) -> None:
    raise AssertionError("a network entry point was touched")


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No sessions, no credentials, a temporary data root."""
    monkeypatch.setattr(finra_cli, "make_session", _boom)
    monkeypatch.setattr(finra_cli, "credentials", lambda: None)
    monkeypatch.setenv("DATACLI_FINRA_ROOT", str(tmp_path / "finra"))
    monkeypatch.setenv("NO_COLOR", "1")
    # `fetch --run` takes the scheduler's direct mutation lock; the registry
    # would resolve this machine's real roots, so tell it the locks are held
    monkeypatch.setenv("DATACLI_LOCKS_HELD", "1")
    monkeypatch.setenv(
        "COLUMNS", "200"
    )  # captured output is not a TTY; stop Rich wrapping paths


# --------------------------------------------------------------------------- #
# help / usage
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("command", sorted(finra_cli.COMMANDS))
@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_per_command(
    command: str, flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert finra_cli.main([command, flag]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"usage: {finra_cli.COMMANDS[command].usage}")
    assert "-h, --help" in out


def test_top_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert finra_cli.main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "bare `finra` == `finra status`" in out
    assert "FINRA_CLIENT_ID" in out and "FINRA_API_KEY" in out
    for cmd in finra_cli.COMMANDS.values():
        assert cmd.synopsis in out
    assert finra_cli.main(["help", "probe"]) == 0
    assert capsys.readouterr().out.startswith("usage: finra probe <what> [<dataset>]")


@pytest.mark.parametrize(
    ("argv", "expect"),
    [
        (["status", "--lvie"], "did you mean --live"),
        (["status", "--json=1"], "does not take a value"),
        (["probe", "metadata", "--group"], "needs a value"),
        (["list", "--all"], "unknown flag --all"),
    ],
)
def test_bad_flags_exit_2(
    argv: list[str], expect: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert finra_cli.main(argv) == 2
    out = capsys.readouterr().out
    assert expect in out and "usage: finra" in out


def test_unknown_command_hints(capsys: pytest.CaptureFixture[str]) -> None:
    assert finra_cli.main(["stats"]) == 2
    assert "did you mean status" in capsys.readouterr().err


def test_probe_usage_errors(capsys: pytest.CaptureFixture[str]) -> None:
    assert finra_cli.main(["probe"]) == 2
    assert "auth, metadata, partitions" in capsys.readouterr().out
    assert finra_cli.main(["probe", "metdata", "short_volume"]) == 2
    assert "did you mean metadata" in capsys.readouterr().out
    assert finra_cli.main(["probe", "metadata"]) == 2
    assert "needs a dataset name or both --group and --name" in capsys.readouterr().out
    assert finra_cli.main(["probe", "metadata", "shortvol"]) == 2
    assert "did you mean short_volume" in capsys.readouterr().out
    assert (
        finra_cli.main(
            ["probe", "metadata", "short_volume", "--group", "x", "--name", "y"]
        )
        == 2
    )
    assert "not both" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# list / status (offline)
# --------------------------------------------------------------------------- #
def test_list_shows_the_registry(capsys: pytest.CaptureFixture[str]) -> None:
    assert finra_cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert (
        "short_volume" in out and "otcMarket/regShoDaily" in out and "cdn:CNMS" in out
    )


def test_status_offline_without_data_or_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert finra_cli.main([]) == 0  # bare `finra` == status
    out = capsys.readouterr().out
    assert str(tmp_path / "finra") in out and "(env)" in out
    assert "credentials: not set" in out
    assert "no FINRA data yet" in out


def test_status_json_shape_and_credential_masking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        finra_cli,
        "credentials",
        lambda: auth.Credentials("client-wxyz", "secret-value"),
    )
    (tmp_path / "finra" / "short_volume" / "daily").mkdir(parents=True)
    (tmp_path / "finra" / "short_volume" / "daily" / "2026-09-30.parquet").write_bytes(
        b""
    )
    assert finra_cli.main(["status", "--json"]) == 0
    out = capsys.readouterr().out
    status = json.loads(out)
    assert status["root_source"] == "env"
    assert status["credentials"] == {
        "configured": True,
        "client_id": "****wxyz",
        "error": "",
    }
    assert status["datasets"][0] == {
        "dataset": "short_volume",
        "dir": str(tmp_path / "finra" / "short_volume"),
        "present": True,
        "days": 1,
        "first": "2026-09-30",
        "last": "2026-09-30",
        "state": {},
        "last_fetch": "",
    }
    assert "secret-value" not in out


def test_status_reports_half_configured_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        finra_cli,
        "credentials",
        lambda: auth.Credentials.from_env(
            {auth.CLIENT_ID_VAR: "only-id"}, registry=lambda n: ""
        ),
    )
    assert finra_cli.main(["status"]) == 0
    assert "FINRA_CLIENT_ID is set but FINRA_API_KEY is not" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# live paths through a fake client
# --------------------------------------------------------------------------- #
class _FakeClient:
    def __init__(self, parts=None, meta=None, error: Exception | None = None) -> None:
        self._parts, self._meta, self._error = parts or [], meta, error
        self.calls: list[tuple[str, str, str]] = []

    def partitions(self, group: str, name: str) -> list[str]:
        self.calls.append(("partitions", group, name))
        if self._error:
            raise self._error
        return list(self._parts)

    def metadata(self, group: str, name: str):
        self.calls.append(("metadata", group, name))
        if self._error:
            raise self._error
        return self._meta


def test_status_live_reports_published_separately(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeClient(parts=["2025-10-01", "2026-09-30"])
    monkeypatch.setattr(finra_cli, "make_client", lambda **kw: fake)
    assert finra_cli.main(["status", "--live", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["datasets"][0]["published"] == {
        "reachable": True,
        "partitions": 2,
        "first": "2025-10-01",
        "last": "2026-09-30",
    }
    assert fake.calls == [
        ("partitions", "otcMarket", "regShoDaily"),
        ("partitions", "otcMarket", "weeklySummary"),
        ("partitions", "otcMarket", "consolidatedShortInterest"),
    ]


def test_status_live_unreachable_is_a_fact_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeClient(error=ConnectionError("dns"))
    monkeypatch.setattr(finra_cli, "make_client", lambda **kw: fake)
    assert finra_cli.main(["status", "--live"]) == 0
    assert "unreachable: ConnectionError: dns" in capsys.readouterr().out


def test_probe_metadata_renders_fields(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    meta = api.DatasetMetadata(
        "OTCMARKET",
        "REGSHODAILY",
        "Reg SHO Daily File",
        ("tradeReportDate",),
        (api.Field("tradeReportDate", "Date", "Trade Date", "yyyy-MM-dd"),),
    )
    fake = _FakeClient(meta=meta)
    monkeypatch.setattr(finra_cli, "make_client", lambda **kw: fake)
    assert finra_cli.main(["probe", "metadata", "short_volume"]) == 0
    out = capsys.readouterr().out
    assert (
        "tradeReportDate" in out
        and "yyyy-MM-dd" in out
        and "partitioned by: tradeReportDate" in out
    )
    assert fake.calls == [("metadata", "otcMarket", "regShoDaily")]
    # --group/--name address any dataset, registry or not
    assert (
        finra_cli.main(
            ["probe", "metadata", "--group", "otcMarket", "--name", "thresholdList"]
        )
        == 0
    )
    assert fake.calls[-1] == ("metadata", "otcMarket", "thresholdList")


def test_probe_partitions_and_api_errors_exit_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeClient(parts=[f"2026-09-{d:02d}" for d in range(1, 31)])
    monkeypatch.setattr(finra_cli, "make_client", lambda **kw: fake)
    assert finra_cli.main(["probe", "partitions", "short_volume"]) == 0
    out = capsys.readouterr().out
    assert "30 partitions" in out and "2026-09-01 .. 2026-09-30" in out

    fake = _FakeClient(error=EntitlementError("needs credentials", status=403))
    monkeypatch.setattr(finra_cli, "make_client", lambda **kw: fake)
    assert finra_cli.main(["probe", "partitions", "short_volume"]) == 1
    assert "EntitlementError: needs credentials (HTTP 403)" in capsys.readouterr().out


def test_probe_auth_without_credentials(capsys: pytest.CaptureFixture[str]) -> None:
    assert finra_cli.main(["probe", "auth"]) == 1
    assert "credentials not set" in capsys.readouterr().out


def test_probe_auth_reports_lifetime_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _Resp:
        status_code = 200
        headers: dict = {}

        @staticmethod
        def json():
            return {"access_token": "tok-should-not-print", "expires_in": 43169}

    class _Session:
        def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(finra_cli, "make_session", lambda: _Session())
    monkeypatch.setattr(
        finra_cli, "credentials", lambda: auth.Credentials("client-1234", "s")
    )
    assert finra_cli.main(["probe", "auth"]) == 0
    out = capsys.readouterr().out
    assert "token ok" in out and "****1234" in out and "12.0 h" in out
    assert "tok-should-not-print" not in out


# --------------------------------------------------------------------------- #
# fetch / qc (phase 2): dry run offline, --run through a fake transport
# --------------------------------------------------------------------------- #
from datetime import date as _date  # noqa: E402

import pandas as _pd  # noqa: E402

from finra import short_volume as _sv  # noqa: E402


def _frame(day: _date) -> _pd.DataFrame:
    return _pd.DataFrame(
        {
            "date": [day, day],
            "symbol": ["A", "BRK/B"],
            "short_volume": [10, 5],
            "short_exempt_volume": [1, 0],
            "total_volume": [20, 9],
            "facilities": ["B,N,Q", "Q"],
            "source": ["cdn", "cdn"],
        }
    )


class _FakeTransport:
    name = "cdn"

    def __init__(self, days: set[_date]) -> None:
        self.days = days

    def fetch_day(self, trade_date: _date):
        return _frame(trade_date) if trade_date in self.days else None


@pytest.mark.parametrize(
    ("argv", "expect"),
    [
        (["fetch", "--from", "2026-13-01"], "--from must be YYYY-MM-DD"),
        (["fetch", "--limit-days", "x"], "--limit-days must be an integer >= 0"),
        (
            ["fetch", "--from", "2026-09-30", "--to", "2026-09-01"],
            "--to is before --from",
        ),
        (["fetch", "--transport", "ftp"], "unknown transport for short_volume 'ftp'"),
        (["fetch", "--dataset", "threshold_list"], "unknown dataset 'threshold_list'"),
        (["fetch", "extra"], "takes flags only"),
        (["qc", "extra"], "takes flags only"),
    ],
)
def test_fetch_and_qc_argument_errors(
    argv: list[str], expect: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert finra_cli.main(argv) == 2
    assert expect in capsys.readouterr().out


def test_fetch_dry_run_never_builds_a_transport(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(finra_cli, "make_transport", _boom)
    assert finra_cli.main(["fetch", "--from", "2026-09-28", "--to", "2026-09-30"]) == 0
    out = capsys.readouterr().out
    assert (
        "plan ->" in out
        and "3 partition(s) to fetch" in out
        and "2026-09-30 .. 2026-09-28" in out
    )
    assert "re-run with --run" in out


def test_fetch_run_stores_days_then_qc_and_status_see_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeTransport({_date(2026, 9, 28), _date(2026, 9, 30)})
    monkeypatch.setattr(finra_cli, "make_transport", lambda spec, name: fake)
    monkeypatch.setattr(finra_cli, "eodhd_common_codes", lambda: ["A", "BRK-B"])
    assert (
        finra_cli.main(["fetch", "--from", "2026-09-28", "--to", "2026-09-30", "--run"])
        == 0
    )
    out = capsys.readouterr().out
    assert "stored 2" in out and "absent 1" in out and "4 rows" in out
    daily = tmp_path / "finra" / "short_volume" / "daily"
    assert sorted(p.name for p in daily.glob("*.parquet")) == [
        "2026-09-28.parquet",
        "2026-09-30.parquet",
    ]

    assert finra_cli.main(["qc"]) == 0
    out = capsys.readouterr().out
    assert "2 partition(s) on disk" in out and "2026-09-28 .. 2026-09-30" in out
    assert "eodhd_coverage" in out and "0 of 2" in out

    assert finra_cli.main(["status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    entry = status["datasets"][0]
    assert (
        entry["days"] == 2
        and entry["first"] == "2026-09-28"
        and entry["last"] == "2026-09-30"
    )
    assert entry["state"] == {"absent": 1, "ok": 2} and entry["last_fetch"]

    # a second run is a no-op outside the overlap and exits 0
    assert (
        finra_cli.main(
            [
                "fetch",
                "--from",
                "2026-09-28",
                "--to",
                "2026-09-30",
                "--overlap-days",
                "0",
            ]
        )
        == 0
    )
    assert "nothing to fetch" in capsys.readouterr().out


def test_fetch_run_exit_1_on_failures(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _Broken:
        name = "cdn"

        def fetch_day(self, trade_date):
            raise EntitlementError("nope", status=403)

    monkeypatch.setattr(finra_cli, "make_transport", lambda spec, name: _Broken())
    assert (
        finra_cli.main(["fetch", "--from", "2026-09-30", "--to", "2026-09-30", "--run"])
        == 1
    )
    out = capsys.readouterr().out
    assert "failed 1" in out and "EntitlementError: nope (HTTP 403)" in out


def test_qc_exit_1_on_error_findings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(finra_cli, "eodhd_common_codes", lambda: None)
    store = _sv.store(tmp_path / "finra")
    bad = _frame(_date(2026, 9, 30))
    bad.loc[0, "short_volume"] = 999
    store.write_day(_date(2026, 9, 30), bad)
    assert finra_cli.main(["qc"]) == 1
    out = capsys.readouterr().out
    assert "short_gt_total" in out and "state" in out
