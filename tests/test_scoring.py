from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(_REPO_ROOT), str(_REPO_ROOT / "eodhd")):
    if p not in sys.path:
        sys.path.insert(0, p)

from llm.models import LLM  # noqa: E402
from scoring import runner  # noqa: E402
from scoring import schema as sch  # noqa: E402
from scoring import select as sel  # noqa: E402
from scoring import store  # noqa: E402
from scoring.backends.base import Item, sanitize_model_id  # noqa: E402
from scoring.backends.llm import LLMBackend, LocalOnlyError, parse_json  # noqa: E402
from scoring.backends.vendor import VendorBackend  # noqa: E402
from scoring.config import ScoringConfig  # noqa: E402

EVENT = sch.load_schema("event")


# --------------------------------------------------------------------------- #
# schema
# --------------------------------------------------------------------------- #
def test_event_schema_loads_and_identifies() -> None:
    assert EVENT.key == "event@1" and EVENT.per_symbol and EVENT.max_symbols == 3
    assert (
        "event_type" in EVENT.field_names()
        and "direction" in EVENT.symbol_field_names()
    )
    assert len(EVENT.prompt_hash()) == 16
    spec = EVENT.field_spec()
    assert '"sentiment": float in [-1.0, 1.0]' in spec and '"role": one of' in spec
    shape = EVENT.json_shape(["AAPL.US"])
    assert shape["properties"]["symbols"]["required"] == ["AAPL.US"]
    assert sch.load_schema("event@1").key == "event@1"
    assert sch.load_schema("event_v1").key == "event@1"
    with pytest.raises(sch.SchemaError):
        sch.load_schema("nope")


def test_schema_file_validation(tmp_path: Path) -> None:
    (tmp_path / "bad").mkdir()
    bad = tmp_path / "bad" / "x_v1.toml"
    bad.write_text(
        '[schema]\nname="x"\nversion=1\n[[fields]]\nname="a"\ntype="enum"\n',
        encoding="utf-8",
    )
    with pytest.raises(sch.SchemaError):
        sch.load_schema_file(bad)
    ok = tmp_path / "y_v2.toml"
    ok.write_text(
        '[schema]\nname="y"\nversion=2\n[prompt]\nsystem="s"\ninstructions="i"\n'
        '[[fields]]\nname="score"\ntype="float"\nmin=0\nmax=1\n',
        encoding="utf-8",
    )
    s = sch.load_schema_file(ok)
    assert s.key == "y@2" and s.scope == "article" and not s.per_symbol
    assert sch.load_schema("y", schemas_dir=tmp_path).version == 2


def test_validate_coerces_clamps_and_reports() -> None:
    payload = {
        "article": {
            "event_type": "M&A",  # tolerant enum match -> m_and_a
            "summary": "x" * 500,  # truncated
            "sentiment": "1.7",  # clamped
            "confidence": 0.5,
            "materiality": 7,  # clamped to 3
            "novelty": "yes",
            "horizon": "weeks",
        },
        "symbols": {
            "aapl.us": {"role": "subject", "direction": "sideways", "relevance": 0.9}
        },
    }
    article, symbols, problems = EVENT.validate(payload, ["AAPL.US", "MSFT.US"])
    assert article["event_type"] == "m_and_a" and len(article["summary"]) == 240
    assert article["sentiment"] == 1.0 and article["materiality"] == 3
    assert article["novelty"] is True
    assert (
        symbols["AAPL.US"]["role"] == "subject"
        and symbols["AAPL.US"]["direction"] is None
    )
    assert symbols["MSFT.US"] == {"role": None, "direction": None, "relevance": None}
    assert any("direction: not in enum" in p for p in problems)
    assert any("symbols.MSFT.US: missing" in p for p in problems)
    # flat payload (no "article" wrapper) is tolerated
    flat, _, _ = EVENT.validate({"event_type": "earnings", "sentiment": 0.1}, [])
    assert flat["event_type"] == "earnings" and flat["summary"] is None


def test_parse_json_variants() -> None:
    assert parse_json('{"a": 1}') == {"a": 1}
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Sure! Here it is: {"a": {"b": 2}} thanks') == {"a": {"b": 2}}
    assert parse_json("no json here") is None
    assert parse_json("") is None


# --------------------------------------------------------------------------- #
# backends
# --------------------------------------------------------------------------- #
def _item(
    aid: str = "a1", symbols=("AAPL.US", "MSFT.US"), targets=("AAPL.US",), pol=0.6
) -> Item:
    return Item(
        aid,
        "2026-08-13",
        "Apple beats",
        "Apple reported strong results. " * 20,
        symbols,
        targets,
        vendor_polarity=pol,
    )


def _good_json(symbols):
    return json.dumps(
        {
            "article": {
                "event_type": "earnings",
                "summary": "Apple beat.",
                "sentiment": 0.6,
                "confidence": 0.9,
                "materiality": 2,
                "novelty": True,
                "horizon": "quarters",
            },
            "symbols": {
                s: {"role": "subject", "direction": "up", "relevance": 1.0}
                for s in symbols
            },
        }
    )


def _fake_llm(tmp_path: Path, replies: list[str]) -> tuple[LLM, list[dict]]:
    calls: list[dict] = []

    def fake(**kw):
        calls.append(kw)
        text = replies[min(len(calls) - 1, len(replies) - 1)]
        return {
            "choices": [{"message": {"content": text}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        }

    return (
        LLM(cache_dir=tmp_path / "cache", completion_fn=fake, cost_fn=lambda r: 0.0),
        calls,
    )


def test_llm_backend_scores_and_repairs(tmp_path: Path) -> None:
    llm, calls = _fake_llm(tmp_path, ["not json at all", _good_json(["AAPL.US"])])
    be = LLMBackend(ScoringConfig(), model="local", llm=llm)
    assert be.id == "ollama__qwen2.5-coder_7b" and be.kind == "record"
    [res] = be.score([_item()], EVENT)
    assert res.status == "ok" and res.article["event_type"] == "earnings"
    assert res.symbols["AAPL.US"]["direction"] == "up"
    assert len(calls) == 2  # one repair turn
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert (
        "Target symbols for the per-symbol fields: AAPL.US"
        in calls[0]["messages"][-1]["content"]
    )
    assert res.prompt_tokens == 200 and res.prompt_hash == EVENT.prompt_hash()


def test_llm_backend_invalid_and_error(tmp_path: Path) -> None:
    llm, _ = _fake_llm(tmp_path, ['{"article": {"event_type": "earnings"}}'])
    be = LLMBackend(ScoringConfig(), model="local", llm=llm, max_repairs=0)
    [res] = be.score([_item()], EVENT)
    assert res.status == "invalid" and any(
        "sentiment: missing" in p for p in res.problems
    )

    def boom(**kw):
        raise ConnectionError("ollama down")

    be2 = LLMBackend(
        ScoringConfig(),
        model="local",
        llm=LLM(cache_dir=tmp_path / "c2", completion_fn=boom, cost_fn=lambda r: 0.0),
    )
    [res2] = be2.score([_item()], EVENT)
    assert res2.status == "error" and "ConnectionError" in res2.error


def test_llm_backend_local_only_guard(tmp_path: Path) -> None:
    with pytest.raises(LocalOnlyError):
        LLMBackend(ScoringConfig(budget_usd=0.0), model="anthropic/claude-x")
    be = LLMBackend(
        ScoringConfig(budget_usd=5.0),
        model="anthropic/claude-x",
        llm=_fake_llm(tmp_path, ["{}"])[0],
    )
    assert be.model == "anthropic/claude-x"
    est = be.estimate([_item()], EVENT)
    assert est.n_items == 1 and est.seconds > 0


def test_vendor_backend_baseline() -> None:
    be = VendorBackend()
    [ok, skipped] = be.score([_item(pol=-0.4), _item("a2", pol=None)], EVENT)
    assert ok.status == "ok" and ok.article == {"sentiment": -0.4, "confidence": 0.4}
    assert ok.symbols == {"AAPL.US": {}}
    assert skipped.status == "skipped"
    assert sanitize_model_id("ollama/qwen2.5-coder:7b") == "ollama__qwen2.5-coder_7b"


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #
def test_store_round_trip_and_upsert(tmp_path: Path) -> None:
    llm, _ = _fake_llm(tmp_path, [_good_json(["AAPL.US"])])
    be = LLMBackend(ScoringConfig(), model="local", llm=llm)
    results = be.score([_item()], EVENT)
    frame = store.results_to_frame(results, EVENT, be.id, store.now_iso())
    assert len(frame) == 2 and set(frame["symbol"].dropna()) == {"AAPL.US"}
    d = store.sidecar_dir(tmp_path, EVENT, be.id, be.kind)
    assert d == tmp_path / "news" / "scores" / "event@1" / be.id
    path = store.partition_path(d, "2026-08-13")
    arrow = store.score_arrow_schema(EVENT)
    assert store.upsert_partition(frame, path, arrow, ["article_id", "symbol"]) == 2
    assert (
        store.upsert_partition(frame, path, arrow, ["article_id", "symbol"]) == 2
    )  # idempotent
    back = pd.read_parquet(path)
    assert back["event_type"].tolist() == ["earnings", "earnings"]
    assert store.scored_ids(path) == {"a1"}
    state = store.write_state(
        d, None, [{"date": "2026-08-13", "status": "ok", "n_ok": 1}]
    )
    assert store.state_lookup(state)["2026-08-13"]["status"] == "ok"
    disc = store.discover(tmp_path)
    assert disc and disc[0]["schema"] == "event@1" and disc[0]["days"] == 1


# --------------------------------------------------------------------------- #
# select + runner on an in-memory corpus
# --------------------------------------------------------------------------- #
def _corpus(con) -> None:
    con.execute("""
        CREATE TABLE news AS SELECT * FROM (VALUES
          ('a1', DATE '2026-08-13', TIMESTAMP '2026-08-13 10:00:00', 'T1', repeat('body ', 60), ['AAPL.US','MSFT.US'], 'x.com', 0.5, 0.1, 0.0),
          ('a1', DATE '2026-08-13', TIMESTAMP '2026-08-13 12:00:00', 'T1 v2', repeat('body ', 60), ['AAPL.US'], 'x.com', 0.5, 0.1, 0.0),
          ('a2', DATE '2026-08-13', TIMESTAMP '2026-08-13 11:00:00', 'T2', repeat('body ', 60), ['ZZZ.US'], 'y.com', 0.1, 0.0, 0.0),
          ('a3', DATE '2026-08-13', TIMESTAMP '2026-08-13 09:00:00', 'T3', 'short', ['AAPL.US'], 'y.com', 0.1, 0.0, 0.0),
          ('a4', DATE '2026-08-13', TIMESTAMP '2026-08-13 08:00:00', 'T4', repeat('body ', 60), ['AAPL.US','MSFT.US','GOOG.US','AMZN.US'], 'y.com', -0.3, 0.0, 0.2),
          ('a5', DATE '2026-08-12', TIMESTAMP '2026-08-12 08:00:00', 'T5', repeat('body ', 60), ['MSFT.US'], 'y.com', 0.9, 0.3, 0.0)
        ) t(article_id, date, published_at, title, content, symbols, source, polarity, pos, neg)
        """)
    con.execute(
        "CREATE TABLE prices_state AS SELECT * FROM (VALUES ('AAPL','US'), ('MSFT','US'), ('GOOG','US'), ('AMZN','US')) t(ticker, exchange)"
    )


def test_select_day_rules() -> None:
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    _corpus(con)
    sel.install_universe(con, sel.universe_symbols(con))
    assert sel.days_with_articles(con, "2026-08-01", "2026-08-31") == [
        "2026-08-13",
        "2026-08-12",
    ]
    items = sel.select_day(con, "2026-08-13", use_universe=True, max_symbols=3)
    by_id = {it.article_id: it for it in items}
    assert set(by_id) == {"a1", "a4"}  # a2 not in universe, a3 too short
    assert by_id["a1"].title == "T1 v2" and by_id["a1"].target_symbols == (
        "AAPL.US",
    )  # latest version wins
    assert (
        by_id["a4"].target_symbols == ()
    )  # 4 universe symbols > max_symbols -> article-level only
    everything = sel.select_day(con, "2026-08-13", use_universe=False, max_symbols=3)
    assert {it.article_id for it in everything} == {"a1", "a2", "a4"}
    sampled = sel.select_day(
        con, "2026-08-13", use_universe=False, max_symbols=3, sample=1
    )
    assert len(sampled) == 1


def test_runner_plan_and_run_resume(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    _corpus(con)
    llm, calls = _fake_llm(tmp_path, [_good_json(["AAPL.US"])])
    be = LLMBackend(ScoringConfig(), model="local", llm=llm)
    target = runner.Target(since="2026-08-12", until="2026-08-13", max_symbols=3)
    p = runner.plan(con, data_root=tmp_path, schema=EVENT, backend=be, target=target)
    assert [d.day for d in p.days] == ["2026-08-13", "2026-08-12"]
    assert p.n_pending == 3 and p.n_candidates == 3
    totals = runner.run(
        con, data_root=tmp_path, schema=EVENT, backend=be, target=target, chunk=2
    )
    assert totals["n_ok"] == 3 and totals["days"] == 2 and not totals["stopped"]
    d = store.sidecar_dir(tmp_path, EVENT, be.id, be.kind)
    assert store.scored_ids(store.partition_path(d, "2026-08-13")) == {"a1", "a4"}
    state = store.state_lookup(store.read_state(d))
    assert state["2026-08-13"]["status"] == "ok" and state["2026-08-12"]["n_ok"] == "1"
    # second run: nothing pending, no model calls
    n_calls = len(calls)
    p2 = runner.plan(con, data_root=tmp_path, schema=EVENT, backend=be, target=target)
    assert p2.n_pending == 0
    runner.run(con, data_root=tmp_path, schema=EVENT, backend=be, target=target)
    assert len(calls) == n_calls
    # --limit caps, --force re-scores
    p3 = runner.plan(
        con,
        data_root=tmp_path,
        schema=EVENT,
        backend=be,
        target=runner.Target("2026-08-12", "2026-08-13", force=True, limit=2),
    )
    assert p3.n_pending == 2


def test_default_window() -> None:
    from datetime import date

    assert runner.default_window(90, date(2026, 8, 15)) == ("2026-05-18", "2026-08-15")
    assert runner.default_window(1, date(2026, 8, 15)) == ("2026-08-15", "2026-08-15")


def test_cli_parser_and_local_only_default() -> None:
    from scoring import cli

    p = cli.build_parser()
    a = p.parse_args(["plan", "--days", "3", "--limit", "5"])
    assert a.command == "plan" and a.backend == "llm" and a.days == 3
    t = cli._target(a, ScoringConfig())
    assert t.limit == 5 and t.use_universe
    a2 = p.parse_args(
        ["run", "--since", "2026-08-01", "--until", "2026-08-02", "--no-universe"]
    )
    t2 = cli._target(a2, ScoringConfig())
    assert (t2.since, t2.until, t2.use_universe) == ("2026-08-01", "2026-08-02", False)
    # a paid model without a budget is refused before anything runs
    a3 = p.parse_args(["plan", "--model", "openai/gpt-4o-mini"])
    with pytest.raises(LocalOnlyError):
        cli._backend(a3, ScoringConfig())


def test_register_score_views_and_status_dataset(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb")
    import eodhd_datasets as reg  # type: ignore
    import explore_eodhd as ex  # type: ignore
    import status_eodhd as st  # type: ignore

    llm, _ = _fake_llm(tmp_path, [_good_json(["AAPL.US"])])
    be = LLMBackend(ScoringConfig(), model="local", llm=llm)
    frame = store.results_to_frame(
        be.score([_item()], EVENT), EVENT, be.id, store.now_iso()
    )
    d = store.sidecar_dir(tmp_path, EVENT, be.id, be.kind)
    store.upsert_partition(
        frame,
        store.partition_path(d, "2026-08-13"),
        store.score_arrow_schema(EVENT),
        ["article_id", "symbol"],
    )
    con = duckdb.connect()
    created = ex.register_score_views(con, tmp_path)
    assert set(created) == {"news_scores_event_v1", "news_scores_event"}
    rows = con.execute(
        "SELECT backend, count(*) FROM news_scores_event GROUP BY 1"
    ).fetchall()
    assert rows == [(be.id, 2)]
    assert ex.register_score_views(duckdb.connect(), tmp_path / "nothing") == []
    # the derived registry dataset picks the nested sidecars up in status
    scores_ds = next(
        ds for ds in reg.LANES["news"].datasets if ds.kind == "news_scores"
    )
    lane = reg.LaneConfig(
        name="news",
        region="Global",
        asset_class="news",
        datasets=(scores_ds,),
        root=tmp_path / "news",
    )
    rec = st.collect_dataset(
        lane, scores_ds, as_of_ts=pd.Timestamp("2026-08-15"), stale_days=7, deep=True
    )
    assert rec["rows"] == 2 and rec["last_data"] == "2026-08-13" and rec["pairs"] == 1
