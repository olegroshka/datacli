from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(_REPO_ROOT), str(_REPO_ROOT / "eodhd")):
    if p not in sys.path:
        sys.path.insert(0, p)

from scoring import bench as bm  # noqa: E402
from scoring.cli import _parse_configs  # noqa: E402


def test_parse_configs_handles_ollama_tags_and_schema_suffix() -> None:
    cfgs = _parse_configs(
        "qwen2.5-coder:7b, ollama/qwen2.5:7b-instruct:event@2, phi4:14b, llama3.1:8b-instruct-q4_K_M:event"
    )
    assert [(c.model, c.schema_spec) for c in cfgs] == [
        ("qwen2.5-coder:7b", "event"),  # ':7b' is a model tag, not a schema
        ("ollama/qwen2.5:7b-instruct", "event@2"),
        ("phi4:14b", "event"),
        ("llama3.1:8b-instruct-q4_K_M", "event"),
    ]
    assert cfgs[0].id == "qwen2.5-coder_7b__event"
    assert cfgs[1].id == "qwen2.5_7b-instruct__eventv2"


def _frame(config: str, sentiments, materialities, events, statuses=None, symbols=None):
    """Build an article-level + per-symbol frame shaped like store.results_to_frame."""
    n = len(sentiments)
    statuses = statuses or ["ok"] * n
    rows = []
    for i in range(n):
        aid = f"a{i}"
        rows.append(
            {
                "article_id": aid,
                "symbol": None,
                "status": statuses[i],
                "sentiment": sentiments[i],
                "materiality": materialities[i],
                "event_type": events[i],
                "horizon": "weeks",
                "config": config,
            }
        )
        rows.append(
            {
                "article_id": aid,
                "symbol": (symbols or ["AAPL.US"] * n)[i],
                "status": statuses[i],
                "sentiment": sentiments[i],
                "materiality": materialities[i],
                "event_type": events[i],
                "horizon": "weeks",
                "role": "subject",
                "direction": "up" if sentiments[i] > 0 else "down",
                "config": config,
            }
        )
    return pd.DataFrame(rows)


def _reactions(n: int, r0s, r1s, symbols=None):
    return pd.DataFrame(
        {
            "article_id": [f"a{i}" for i in range(n)],
            "symbol": symbols or ["AAPL.US"] * n,
            "r0": r0s,
            "r1": r1s,
        }
    )


def test_config_metrics_validity_and_calibration() -> None:
    f = _frame(
        "c1",
        [0.6] * 8 + [-0.6] * 2,
        [2] * 10,
        ["earnings"] * 9 + ["other"],
        statuses=["ok"] * 9 + ["invalid"],
    )
    m = bm.config_metrics(
        f, pd.DataFrame(columns=["article_id", "symbol", "r0", "r1"]), seconds=20.0
    )
    assert (
        m["n_articles"] == 10 and m["invalid_share"] == 0.1 and m["s_per_item"] == 2.0
    )
    assert m["other_share"] == 0.0  # the 'other' row is the invalid one, excluded
    assert m["sent_distinct"] == 2 and m["sent_modal_share"] == pytest.approx(
        8 / 9, abs=0.01
    )
    assert m["mat_modal_share"] == 1.0 and m["mat_low_share"] == 0.0
    assert m["horizon_na_share"] == 0.0


def test_config_metrics_signal_detects_monotone_ordering() -> None:
    # 40 articles: sentiment ordered with returns, materiality ordered with |r1|
    sents, mats, r0s, r1s = [], [], [], []
    for i in range(40):
        bucket = i % 4
        sents.append([-0.6, -0.3, 0.3, 0.6][bucket])
        mats.append(bucket)
        r0s.append([-0.03, -0.01, 0.01, 0.03][bucket])
        r1s.append([0.001, 0.002, 0.003, 0.004][bucket])
    f = _frame("c1", sents, mats, ["earnings"] * 40)
    m = bm.config_metrics(f, _reactions(40, r0s, r1s), seconds=40.0)
    assert m["n_signal_rows"] == 40
    assert m["sent_r0_monotone"] == 1.0
    assert m["sent_r0_spread_bps"] == 600  # -300 bps -> +300 bps
    assert m["mat_absr1_monotone"] == 1.0
    assert m["mat_absr1_spread_bps"] == 30
    assert m["dir_r0_spread_bps"] == 400


def test_head_to_head_picks_the_config_whose_sign_matches_the_move() -> None:
    # a is right on every disagreement, b is wrong
    n = 30
    sents_a = [0.6 if i % 2 == 0 else -0.6 for i in range(n)]
    sents_b = [-0.6 if i % 2 == 0 else 0.6 for i in range(n)]
    r0s = [0.02 if i % 2 == 0 else -0.02 for i in range(n)]
    a = _frame("a", sents_a, [2] * n, ["earnings"] * n)
    b = _frame("b", sents_b, [2] * n, ["guidance"] * n)
    h = bm.head_to_head(a, b, _reactions(n, r0s, [0.0] * n))
    assert h["n_both"] == n and h["n_disagree"] == n
    assert h["a_wins"] == n and h["b_wins"] == 0 and h["a_win_rate"] == 1.0
    assert h["event_agree"] == 0.0
    assert h["sent_corr"] == pytest.approx(-1.0)


def test_scorecard_one_row_per_config() -> None:
    r1 = bm.BenchResult(
        bm.Config("m1", "event"), pd.DataFrame(), 1.0, {"ok_share": 1.0}
    )
    r2 = bm.BenchResult(
        bm.Config("m2", "event@2"), pd.DataFrame(), 2.0, {"ok_share": 0.9}
    )
    card = bm.scorecard([r1, r2])
    assert list(card["config"]) == ["m1__event", "m2__eventv2"]
    assert list(card["ok_share"]) == [1.0, 0.9]
