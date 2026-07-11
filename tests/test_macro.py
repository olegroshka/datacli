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
    assert macro_config.fred_path().name == "fred_observations.parquet"
    assert macro_config.eodhd_path().name == "eodhd_indicators.parquet"


def test_registry_has_both_providers() -> None:
    assert len(reg.FRED_SERIES) >= 30  # expanded set
    assert "gdp_growth_annual" in reg.EODHD_INDICATORS
    assert {"USA", "GBR", "EMU"} <= set(reg.EODHD_COUNTRIES)
    # every (country, indicator) pair is enumerated for fetching
    assert len(reg.eodhd_pairs()) == len(reg.EODHD_COUNTRIES) * len(
        reg.EODHD_INDICATORS
    )


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


class _BadResp:
    def raise_for_status(self) -> None:
        raise RuntimeError("400 Bad Request. The series does not exist.")

    def json(self) -> dict:
        return {}


class _FlakySession:
    """Raises 400 for a set of series ids, returns data for the rest."""

    def __init__(self, payload: dict, bad: set[str]) -> None:
        self._payload = payload
        self._bad = bad

    def get(self, url: str, params: dict | None = None, timeout: int | None = None):
        if params and params.get("series_id") in self._bad:
            return _BadResp()
        return _Resp(self._payload)


def test_fred_refresh_skips_failing_series(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    result = fred.refresh(
        ["DGS10", "BADSERIES", "DGS2"],
        run=True,
        root=tmp_path,
        session=_FlakySession(_PAYLOAD, {"BADSERIES"}),
        key="k",
    )
    assert result["failed"] == ["BADSERIES"]  # skipped, not aborted
    assert result["series_with_data"] == 2  # the two good ones still landed
    frame = fred.load(tmp_path)
    assert frame is not None and set(frame["series_id"]) == {"DGS10", "DGS2"}


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
    assert views.register(con, root=tmp_path)["macro"] is True
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
    assert views.register(con, root=tmp_path) == {
        "macro": False,
        "macro_country": False,
    }


def test_schema_snippet_lists_views() -> None:
    snippet = views.schema_snippet()
    assert "macro(series_id" in snippet and "macro_country(country" in snippet
    # can describe just one surface
    fred_only = views.schema_snippet(country=False)
    assert "macro_country" not in fred_only


# --------------------------------------------------------------------------- #
# EODHD provider (mocked HTTP) + macro_country view
# --------------------------------------------------------------------------- #
_EODHD_PAYLOAD = [
    {"Date": "2020-01-01", "Value": "2.5"},
    {"Date": "2021-01-01", "Value": "."},  # missing -> skipped
    {"Date": "2022-01-01", "Value": "3.1"},
]


def test_eodhd_fetch_indicator_parses() -> None:
    from macro import eodhd

    out = eodhd.fetch_indicator(
        _Session(_EODHD_PAYLOAD), "USA", "gdp_growth_annual", "k"
    )
    assert out == [("2020-01-01", 2.5), ("2022-01-01", 3.1)]


def test_eodhd_refresh_and_country_view(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    from macro import eodhd

    result = eodhd.refresh(
        [("USA", "gdp_growth_annual"), ("GBR", "gdp_growth_annual")],
        run=True,
        root=tmp_path,
        session=_Session(_EODHD_PAYLOAD),
        key="k",
    )
    assert result["series_with_data"] == 2 and result["rows"] == 4
    con = duckdb.connect()
    assert views.register(con, root=tmp_path)["macro_country"] is True
    cols = [
        d[0] for d in con.execute("SELECT * FROM macro_country LIMIT 0").description
    ]
    assert cols == [
        "country",
        "country_name",
        "indicator",
        "indicator_name",
        "category",
        "date",
        "value",
    ]
    name = con.execute(
        "SELECT country_name FROM macro_country WHERE country = 'USA' LIMIT 1"
    ).fetchone()
    assert name == ("United States",)


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
