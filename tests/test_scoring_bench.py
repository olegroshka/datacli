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
    # ':7b' is a model tag, not a schema; a bare tag gains its 'ollama/' prefix
    # so a local-only run does not refuse it as a paid model.
    assert [(c.model, c.schema_spec) for c in cfgs] == [
        ("ollama/qwen2.5-coder:7b", "event"),
        ("ollama/qwen2.5:7b-instruct", "event@2"),
        ("ollama/phi4:14b", "event"),
        ("ollama/llama3.1:8b-instruct-q4_K_M", "event"),
    ]
    # ids are unaffected by the prefix, so runs stay comparable across it
    assert cfgs[0].id == "qwen2.5-coder_7b__event"
    assert cfgs[1].id == "qwen2.5_7b-instruct__eventv2"


def test_normalize_model_leaves_tiers_and_prefixed_ids_alone() -> None:
    from llm import tiers

    assert tiers.normalize_model("qwen2.5:14b-instruct-q4_K_M") == (
        "ollama/qwen2.5:14b-instruct-q4_K_M"
    )
    assert tiers.normalize_model("local") == "local"  # tier key
    assert tiers.normalize_model("gpt-4o-mini") == "gpt-4o-mini"  # no tag
    assert tiers.normalize_model("openai/gpt-4o-mini") == "openai/gpt-4o-mini"
    assert tiers.normalize_model("anthropic/claude-sonnet-5") == (
        "anthropic/claude-sonnet-5"
    )


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


def test_config_metrics_scores_r0_and_r1_edges_independently() -> None:
    """A config can be right about the session the news lands in and wrong about
    the next one. r0 is contaminated by articles that report the move itself, so
    the two edges are reported separately and must not be conflated."""
    n = 40
    sents = [0.6 if i % 2 == 0 else -0.6 for i in range(n)]
    r0s = [0.02 if i % 2 == 0 else -0.02 for i in range(n)]  # matches sentiment
    r1s = [-0.02 if i % 2 == 0 else 0.02 for i in range(n)]  # exactly inverted
    m = bm.config_metrics(
        _frame("c1", sents, [2] * n, ["earnings"] * n), _reactions(n, r0s, r1s), 40.0
    )
    assert m["neutral_share"] == 0.0 and m["sent_coverage"] == 1.0
    assert m["r0_pos_base_rate"] == 0.5 and m["sent_hit_rate"] == 1.0
    assert m["r0_base_committed"] == 0.5 and m["sent_edge_pp"] == 50.0
    assert m["r1_base_committed"] == 0.5 and m["sent_hit_rate_r1"] == 0.0
    assert m["sent_edge_r1_pp"] == -50.0


def test_config_metrics_treats_neutral_as_abstention_not_a_wrong_call() -> None:
    """Half the calls are neutral; they must be excluded from the hit rate rather
    than counted as losses, and coverage must show how much was actually called."""
    n = 60
    sents = [0.0 if i % 2 == 0 else 0.6 for i in range(n)]
    r0s = [-0.02 if i % 2 == 0 else 0.02 for i in range(n)]
    m = bm.config_metrics(
        _frame("c1", sents, [2] * n, ["earnings"] * n),
        _reactions(n, r0s, [0.0] * n),
        60.0,
    )
    assert m["neutral_share"] == 0.5 and m["sent_coverage"] == 0.5
    # every committed call is correct, despite the neutral half moving down
    assert m["sent_hit_rate"] == 1.0


def test_market_adjusted_horizons_are_scored_when_present_and_skipped_when_not() -> None:
    """The ``_ex`` columns are optional: older saved reaction frames lack them and
    must still produce the raw-horizon metrics rather than raising."""
    n = 100
    sents = [0.6 if i % 2 == 0 else -0.6 for i in range(n)]
    r0s = [0.02 if i % 2 == 0 else -0.02 for i in range(n)]
    frame = _frame("c1", sents, [2] * n, ["earnings"] * n)

    plain = bm.config_metrics(frame, _reactions(n, r0s, r0s), 100.0)
    assert plain["sent_edge_pp"] == 50.0
    assert "sent_edge_r1ex_pp" not in plain  # nothing to compute, nothing emitted

    # market-adjusted columns invert the call, so the raw and adjusted edges must
    # disagree -- proving the _ex horizon is scored on its own column
    rx = _reactions(n, r0s, r0s)
    rx["r0_ex"] = [-v for v in r0s]
    rx["r1_ex"] = [-v for v in r0s]
    adj = bm.config_metrics(frame, rx, 100.0)
    assert adj["sent_edge_pp"] == 50.0 and adj["sent_edge_r0ex_pp"] == -50.0
    assert adj["sent_edge_r1ex_pp"] == -50.0
    assert adj["sent_edge_r0ex_n"] == n


def test_edge_null_is_measured_on_the_rows_the_config_committed_to() -> None:
    """A config that abstains selectively must not be scored against a base rate
    drawn from the rows it declined.

    Here the config always calls "positive" and only commits on rows that rose,
    abstaining on every row that fell. Against the whole sample the up-rate is
    50%, which would flatter it into a +50pp 'edge'; against the rows it actually
    committed to the up-rate is 100%, so its true edge is zero -- it has no skill,
    only a filter.
    """
    n = 80
    sents = [0.6 if i % 2 == 0 else 0.0 for i in range(n)]  # commits only on evens
    r0s = [0.02 if i % 2 == 0 else -0.02 for i in range(n)]  # evens rose
    m = bm.config_metrics(
        _frame("c1", sents, [2] * n, ["earnings"] * n),
        _reactions(n, r0s, r0s),
        80.0,
    )
    assert m["sent_hit_rate"] == 1.0  # right on every row it called
    assert m["r0_pos_base_rate"] == 0.5  # ...but the sample-wide rate is 50%
    assert m["r0_base_committed"] == 1.0  # and its own rows all rose
    assert m["sent_edge_pp"] == 0.0  # so the edge is zero, not +50pp
    assert m["sent_coverage"] == 0.5


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


def test_edge_stats_report_se_and_p_against_the_trivial_strategy() -> None:
    # a 60% hit rate against a 50% base: real at n=1000, noise at n=40
    strong = bm._edge_stats(0.60, 0.50, 1000)
    weak = bm._edge_stats(0.60, 0.50, 40)
    assert strong["edge_se_pp"] == pytest.approx(1.58, abs=0.02)
    assert strong["edge_z"] > 6 and strong["edge_p"] < 0.001
    assert weak["edge_se_pp"] == pytest.approx(7.91, abs=0.02)
    assert weak["edge_z"] < 1.5 and weak["edge_p"] > 0.20
    # a hit rate equal to the trivial strategy is exactly zero edge
    flat = bm._edge_stats(0.55, 0.55, 500)
    assert flat["edge_z"] == 0.0 and flat["edge_p"] == pytest.approx(1.0)
    assert bm._edge_stats(0.6, 0.5, 0) == {}


def test_config_metrics_carries_the_edge_standard_error() -> None:
    n = 200
    sents = [0.6 if i % 2 == 0 else -0.6 for i in range(n)]
    r0s = [0.02 if i % 2 == 0 else -0.02 for i in range(n)]
    # r1 mixed but orthogonal to sentiment -- each consecutive (+sent, -sent) pair
    # shares an r1 sign, so the hit rate is exactly 0.5. An edge test is possible
    # here yet finds nothing, which is the distinction the two horizons exist to
    # draw: skill on the publication session is not skill on the next one.
    r1s = [0.01 if (i // 2) % 2 == 0 else -0.01 for i in range(n)]
    m = bm.config_metrics(
        _frame("c1", sents, [2] * n, ["earnings"] * n), _reactions(n, r0s, r1s), 200.0
    )
    assert m["n_committed"] == n and m["edge_se_pp"] == pytest.approx(3.54, abs=0.02)
    assert m["sent_hit_rate_r1"] == 0.5
    assert m["sent_edge_r1_z"] == 0.0 and m["sent_edge_r1_p"] == pytest.approx(1.0)

    # a degenerate base rate (every move the same direction) supports no test at
    # all, and must yield no number rather than a divide-by-zero or a fake one
    flat = bm.config_metrics(
        _frame("c2", sents, [2] * n, ["earnings"] * n),
        _reactions(n, r0s, [0.01] * n),
        200.0,
    )
    assert flat["r1_base_committed"] == 1.0
    assert flat["sent_edge_r1_z"] is None and flat["sent_edge_r1_p"] is None


def test_paired_sign_test_needs_disagreement_to_have_power() -> None:
    n = 120
    r0s = [0.02 if i % 2 == 0 else -0.02 for i in range(n)]
    reactions = _reactions(n, r0s, r0s)
    right = [0.6 if i % 2 == 0 else -0.6 for i in range(n)]  # always correct
    wrong = [-0.6 if i % 2 == 0 else 0.6 for i in range(n)]  # always incorrect
    a = _frame("a", right, [2] * n, ["earnings"] * n)

    # total disagreement: a sweeps the discordant set, decisively
    h = bm.paired_sign_test(a, _frame("b", wrong, [2] * n, ["earnings"] * n), reactions)
    assert h["n_both_committed"] == n and h["sign_agree"] == 0.0
    assert h["a_only_right"] == n and h["b_only_right"] == 0
    assert h["n_discordant"] == n and h["mcnemar_p"] < 0.001

    # near-total agreement: the discordant set is too small to conclude anything,
    # which is the realistic case and must not be reported as a win
    almost = list(right)
    almost[0] = -almost[0]
    h2 = bm.paired_sign_test(a, _frame("b", almost, [2] * n, ["earnings"] * n), reactions)
    assert h2["sign_agree"] > 0.99 and h2["n_discordant"] == 1
    assert "mcnemar_p" not in h2  # refuses to test on 1 discordant pair


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
