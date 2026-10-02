"""Materiality x short-sale positioning: the phase 5 evaluation of the FINRA source.

Usage (read-only; needs the eodhd store, the event@5 scores and the FINRA
short volume store on disk):

    .venv\\Scripts\\python.exe scripts/finra_materiality_eval.py [--schema event@5]
        [--pos sr5_known] [--horizons f1_ex,f5_ex] [--json out.json]

Builds the scored (date, symbol) panel exactly as ``score panel-eval`` does,
attaches forward returns, joins the point-in-time positioning features from
``finra.panel`` on the trading day, and runs the tests in
``scoring.positioning_eval``. Every statistic is a t over trading days.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

_REPO = Path(__file__).resolve().parents[1]
for _p in (str(_REPO), str(_REPO / "eodhd")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _render  # type: ignore[import-not-found]  # noqa: E402

from finra import panel as fpanel  # noqa: E402
from lab import data as lab_data  # noqa: E402
from scoring import panel_eval as pev  # noqa: E402
from scoring import positioning_eval as pos  # noqa: E402
from scoring.cli import score_view_name  # noqa: E402

POS_FIELDS = (
    "sr5_known",
    "sr20_known",
    "sr_abn_known",
    "sr1_known",
    "sr5_t",
    "dtc_known",
    "si_out_known",
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--schema", default="event@5")
    ap.add_argument("--backend", default=None)
    ap.add_argument("--pos", default="sr5_known", help="primary positioning field")
    ap.add_argument("--horizons", default="f1_ex,f5_ex")
    ap.add_argument("--min-history", type=int, default=20)
    ap.add_argument(
        "--json", type=Path, default=None, help="write every table/stat here"
    )
    args = ap.parse_args(argv)
    horizons = [h.strip() for h in args.horizons.split(",") if h.strip()]
    console = _render.make_console()
    out: dict[str, Any] = {
        "schema": args.schema,
        "primary_pos": args.pos,
        "horizons": horizons,
    }

    con = lab_data.connect()
    view = score_view_name(args.schema)
    panel = pev.signal_panel(con, view, backend=args.backend)
    if panel.empty:
        console.print(f"[red]empty panel from {view}[/red]")
        return 1
    joined, report = pev.attach_returns(con, panel)
    console.print(
        f"[dim]panel {len(panel):,} rows from {view}; joined {report.get('joined_rows', 0):,} "
        f"over {report.get('n_days', 0)} trading days[/dim]"
    )
    start = joined["trade_date"].min() - pd.Timedelta(days=120)
    end = joined["trade_date"].max()
    features = fpanel.short_ratio_features(con, start=start, end=end)
    data, jrep = fpanel.attach_positioning(
        joined, features, min_history=args.min_history
    )
    tables = {
        r[0]
        for r in con.execute(
            "SELECT table_name FROM information_schema.tables"
        ).fetchall()
    }
    if "finra_short_interest_float" in tables:
        si_feats = fpanel.short_interest_features(con, data)
        data = data.merge(si_feats, on=["eodhd_code", "trade_date"], how="left")
        known = int(data["dtc_known"].notna().sum())
        console.print(
            f"[dim]short interest known on {known:,} of {len(data):,} rows "
            f"(median report age {data['si_age_known'].median():.0f} days)[/dim]"
        )
    out["join"] = jrep
    console.print(
        f"[dim]positioning joined {jrep['rows']:,} rows ({jrep['match_share']:.0%} of the panel; "
        f"{jrep['dropped_thin_history']} dropped for thin history) over {jrep['n_days']} days[/dim]"
    )
    if data.empty:
        console.print("[red]no rows after the positioning join[/red]")
        return 1
    console.print(
        _render.df_table(
            data[[c for c in POS_FIELDS if c in data.columns]]
            .describe()
            .T.reset_index()
            .round(4),
            title="positioning features on the matched panel",
        )
    )

    n_tests = 0
    for horizon in horizons:
        if horizon not in data.columns:
            continue
        section: dict[str, Any] = {}
        # baseline on the identical rows
        base = pev.magnitude(data, horizon=horizon, field="mat_max")[1]
        section["materiality_baseline"] = base
        console.print(
            _render.df_table(
                pd.DataFrame(
                    [
                        {
                            k: base.get(k)
                            for k in ("corr_n_days", "corr_mean", "corr_t", "corr_p")
                        }
                    ]
                ),
                title=f"{horizon}: materiality alone on the matched rows (baseline)",
            )
        )
        alone_rows = []
        for field in POS_FIELDS:
            if field in data.columns:
                st = pos.positioning_alone(data, field=field, horizon=horizon)
                alone_rows.append(
                    {
                        "field": field,
                        **{
                            k: st.get(k)
                            for k in ("corr_n_days", "corr_mean", "corr_t", "corr_p")
                        },
                    }
                )
                n_tests += 1
        section["positioning_alone"] = alone_rows
        console.print(
            _render.df_table(
                pd.DataFrame(alone_rows),
                title=f"{horizon}: positioning alone vs |move|",
            )
        )

        table, st = pos.interaction_by_tercile(
            data, pos_field=args.pos, horizon=horizon
        )
        n_tests += 1
        section["interaction_terciles"] = {
            "table": table.to_dict("records"),
            "stats": st,
        }
        console.print(
            _render.df_table(
                table, title=f"{horizon}: materiality corr inside {args.pos} terciles"
            )
        )
        console.print(
            f"  high - low: mean {st.get('diff_mean')}  t {st.get('diff_t')}  p {st.get('diff_p')}  over {st.get('diff_n_days')} days"
        )

        table, st = pos.conditional_magnitude(data, pos_field=args.pos, horizon=horizon)
        n_tests += 1
        section["conditional_magnitude"] = {
            "table": table.to_dict("records"),
            "stats": st,
        }
        console.print(
            _render.df_table(
                table,
                title=f"{horizon}: mean |move| bps by materiality x {args.pos} tercile",
            )
        )
        console.print(
            f"  material (>=2) names, high - low tercile: {st.get('spread_mean_bps')} bps  "
            f"t {st.get('spread_t')}  p {st.get('spread_p')}  over {st.get('spread_n_days')} days"
        )

        table, st = pos.fama_macbeth_ranks(data, pos_field=args.pos, horizon=horizon)
        n_tests += 1
        section["fama_macbeth"] = {"table": table.to_dict("records"), "stats": st}
        console.print(
            _render.df_table(
                table,
                title=f"{horizon}: per-day rank regression (Fama-MacBeth), {st.get('n_days')} days",
            )
        )
        out[horizon] = section

    note = pos.bonferroni_note(n_tests)
    out["multiple_testing"] = note
    console.print(f"[dim]{note}[/dim]")
    if args.json:
        args.json.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
        console.print(f"[dim]-> {args.json}[/dim]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
