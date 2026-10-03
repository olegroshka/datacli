"""Holdings-leg inputs from 13F (DD-002 WP10, SPEC's G1 members).

One row per ``(cusip, period, aggregate)`` over the amendment-resolved 13F
panel of ``positioning.long_ladder`` (same lateness cutoff, same cohort,
same ``published_at``), with the cross-manager distribution of the position
on that quarter end:

- ``long_fund_weight``: SPEC's ``long_fund_gmv_mmr`` without margin (R5): the
  sum over the aggregate's managers of the position's weight in that
  manager's 13F book, ``value_usd / book_value``, where the book is the
  manager's effective ``SH`` rows without puts or calls. Weights are taken
  from the reported values, which share one unit inside a filing, so the
  thousands-versus-dollars question (S19) cannot touch them.
- ``long_conc``: the Herfindahl index of the position's shares across the
  aggregate's holders (SPEC's ``z_long_conc`` before the z-score); 1 for a
  security with one holder, ``1 / n`` for ``n`` equal holders.
- ``best_ideas``: the number of the aggregate's managers for whom the
  security is among the ten largest weights of their book, L1-normalised per
  period and aggregate so the column sums to 1 over the period's securities.
  ``best_ideas_count`` keeps the raw count.
- ``n_holders``: managers of the aggregate holding the security.
- ``level_raw`` and ``value_usd``: the aggregate's shares and value.

Recorded deviations: V11 (Herfindahl, SPEC leaves the functional open), V12
(top ten by weight for ``best_ideas``, SPEC leaves the cut open), the margin
term dropped and the book restricted to reported long shares.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd
import pyarrow as pa

from positioning.long_ladder import AGGREGATES, LATE_DAYS, MEMBER, panel_ctes, source_latest  # noqa: F401

NAME = "holdings_inputs"
TOP_N = 10

SCHEMA = pa.schema(
    [
        ("cusip", pa.string()),
        ("period", pa.date32()),
        ("published_at", pa.date32()),
        ("aggregate", pa.string()),
        ("n_holders", pa.int32()),
        ("level_raw", pa.float64()),
        ("value_usd", pa.float64()),
        ("long_fund_weight", pa.float64()),
        ("long_conc", pa.float64()),
        ("best_ideas_count", pa.int32()),
        ("best_ideas", pa.float64()),
    ]
)
COLUMNS = list(SCHEMA.names)
KEY = ("cusip", "period", "aggregate")


def inputs_sql(late_days: int | None = LATE_DAYS, *, top_n: int = TOP_N) -> str:
    """SQL producing the holdings inputs, one row per ``(cusip, period, aggregate)``."""
    per_aggregate = " UNION ALL ".join(
        f"""SELECT '{agg}' AS aggregate, cusip, period,
       count(*) AS n_holders, sum(shares) AS level_raw, sum(value_usd) AS value_usd,
       sum(weight) AS long_fund_weight,
       sum(shares * shares) / (sum(shares) * sum(shares)) AS long_conc,
       count(*) FILTER (WHERE weight_rank <= {int(top_n)}) AS best_ideas_count
FROM _weighted WHERE {MEMBER[agg]} GROUP BY cusip, period"""
        for agg in AGGREGATES
    )
    return f"""
WITH {panel_ctes(late_days)},
_books AS (
  SELECT cik, period, sum(value_usd) AS book_value FROM _hold GROUP BY cik, period),
_weighted AS (
  SELECT j.cusip, j.period, j.cik, j.cohort, j.shares, j.value_usd,
         CASE WHEN b.book_value > 0 THEN j.value_usd / b.book_value END AS weight,
         row_number() OVER (PARTITION BY j.cik, j.period ORDER BY j.value_usd DESC, j.cusip) AS weight_rank
  FROM _joined j JOIN _books b USING (cik, period)),
_inputs AS ({per_aggregate})
SELECT i.aggregate, i.cusip, i.period, s.published_at, i.n_holders, i.level_raw, i.value_usd,
       i.long_fund_weight, i.long_conc, i.best_ideas_count,
       CASE WHEN sum(i.best_ideas_count) OVER (PARTITION BY i.aggregate, i.period) > 0
            THEN i.best_ideas_count / sum(i.best_ideas_count) OVER (PARTITION BY i.aggregate, i.period)
            ELSE 0.0 END AS best_ideas
FROM _inputs i JOIN _sched s USING (period)
"""


def compute(con: Any, *, late_days: int | None = LATE_DAYS, top_n: int = TOP_N) -> pd.DataFrame:
    """The holdings inputs as a frame in column order, sorted by key."""
    frame = con.execute(inputs_sql(late_days, top_n=top_n)).df()
    if frame.empty:
        return pd.DataFrame(columns=COLUMNS)
    frame["period"] = pd.to_datetime(frame["period"]).dt.date
    frame["published_at"] = pd.to_datetime(frame["published_at"]).dt.date
    frame = frame[COLUMNS].sort_values(list(KEY), kind="stable").reset_index(drop=True)
    frame.attrs["stats"] = {"securities": int(frame["cusip"].nunique()), "periods": int(frame["period"].nunique())}
    return frame


def checks(frame: pd.DataFrame) -> list[tuple[str, str, str]]:
    """``(severity, check, detail)`` over a frame of inputs: the DD-002 acceptance as invariants."""
    findings: list[tuple[str, str, str]] = []
    if frame.empty:
        return findings
    sums = frame.groupby(["aggregate", "period"])["best_ideas"].sum()
    off = sums[(sums - 1).abs() > 1e-9]
    off = off[frame.groupby(["aggregate", "period"])["best_ideas_count"].sum().loc[off.index] > 0]
    if len(off):
        findings.append(("error", "best_ideas_not_normalised", f"{len(off)} (aggregate, period) groups do not sum to 1"))
    single = frame[frame["n_holders"] == 1]
    if len(single) and ((single["long_conc"] - 1).abs() > 1e-9).any():
        findings.append(("error", "single_holder_conc", "a security held by one manager has long_conc != 1"))
    bounds = frame[(frame["long_conc"] < 0) | (frame["long_conc"] > 1 + 1e-9)]
    if len(bounds):
        findings.append(("error", "conc_out_of_bounds", f"{len(bounds)} rows"))
    heavy = frame[frame["long_fund_weight"] > frame["n_holders"] + 1e-9]
    if len(heavy):
        findings.append(("error", "weight_exceeds_holders", f"{len(heavy)} rows: a weight above 1 per holder"))
    return findings


def latest(con: Any) -> date | None:
    return source_latest(con)
