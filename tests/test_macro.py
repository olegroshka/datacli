from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from macro import config as macro_config  # noqa: E402
from macro import fred  # noqa: E402
from macro import views  # noqa: E402
from macro import registry as reg  # noqa: E402


# --------------------------------------------------------------------------- #
# registry / config
# --------------------------------------------------------------------------- #
def test_registry_has_market_series() -> None:
    assert {"DGS10", "T10Y2Y", "VIXCLS", "BAMLH0A0HYM2"} <= set(reg.SERIES)
    assert reg.SERIES["DGS10"].category == "rates"
    assert "rates" in reg.categories() and "vol" in reg.categories()


def test_config_paths() -> None:
    assert macro_config.macro_root().name == "macro"
    assert macro_config.observations_path().name == "observations.parquet"


# --------------------------------------------------------------------------- #
# fred fetcher (mocked HTTP)
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


class _Session:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.calls: list[dict] = []

    def get(
        self, url: str, params: dict | None = None, timeout: int | None = None
    ) -> _Resp:
        self.calls.append(params or {})
        return _Resp(self._payload)


_PAYLOAD = {
    "observations": [
        {"date": "2020-01-01", "value": "1.5"},
        {"date": "2020-01-02", "value": "."},  # missing -> skipped
        {"date": "2020-01-03", "value": "1.6"},
    ]
}


def test_fetch_series_parses_and_skips_missing() -> None:
    out = fred.fetch_series(_Session(_PAYLOAD), "DGS10", "key")
    assert out == [("2020-01-01", 1.5), ("2020-01-03", 1.6)]


def test_refresh_dry_run() -> None:
    result = fred.refresh(["DGS10"], run=False, root=Path("."))
    assert result["run"] is False and result["planned"] == ["DGS10"]


def test_refresh_writes_parquet(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    result = fred.refresh(
        ["DGS10", "DGS2"], run=True, root=tmp_path, session=_Session(_PAYLOAD), key="k"
    )
    assert result["rows"] == 4 and result["series"] == 2
    frame = fred.load(tmp_path)
    assert frame is not None
    assert set(frame["series_id"]) == {"DGS10", "DGS2"}
    assert list(frame.columns) == ["series_id", "date", "value"]


# --------------------------------------------------------------------------- #
# views over a fixture (real DuckDB)
# --------------------------------------------------------------------------- #
def test_macro_views_register_and_query(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    fred.refresh(
        ["DGS10", "VIXCLS"],
        run=True,
        root=tmp_path,
        session=_Session(_PAYLOAD),
        key="k",
    )
    con = duckdb.connect()
    assert views.register(con, root=tmp_path) is True
    # the joined `macro` view enriches observations with registry metadata
    cat = con.execute(
        "SELECT category FROM macro WHERE series_id = 'DGS10' LIMIT 1"
    ).fetchone()
    assert cat == ("rates",)
    cols = [d[0] for d in con.execute("SELECT * FROM macro LIMIT 0").description]
    assert cols == ["series_id", "name", "category", "date", "value"]


def test_register_noop_without_data(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    assert views.register(con, root=tmp_path) is False  # no parquet yet


def test_schema_snippet_lists_views() -> None:
    snippet = views.schema_snippet()
    assert "macro(series_id" in snippet and "categories:" in snippet


# --------------------------------------------------------------------------- #
# lab integration: schema text mentions macro only when present
# --------------------------------------------------------------------------- #
def test_lab_schema_text_includes_macro_when_present(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    from lab import data as lab_data

    con = duckdb.connect()
    # no macro view yet -> no macro in schema text
    assert "Macro views" not in lab_data.schema_text(con)
    fred.refresh(
        ["DGS10"], run=True, root=tmp_path, session=_Session(_PAYLOAD), key="k"
    )
    views.register(con, root=tmp_path)
    assert "Macro views" in lab_data.schema_text(con)
