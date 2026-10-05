"""DD-006 C2: parse the AFM holdings export, map its issuers to ISINs and priced series, classify the holders, report coverage.

    .venv\\Scripts\\python.exe scripts\\afm_issuer_map.py [--holdings FILE] [--capital FILE] [--out REPORT.md]

Reads the two AFM exports (defaults: the 2026-10-05 probe downloads under the
positioning root), the ``short_register`` view (the AFM short register's
issuer names and ISINs), the price lanes' ticker lists and the ``prices``
view (which series are on disk), and ``sec_adv_advisers`` (the hedge-fund
advisers). Writes ``<positioning root>/registers/nl_holdings/`` :
``lines.parquet``, ``notifications.parquet``, ``issuer_map.csv``,
``holders.csv``. Nothing is fetched.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
for p in (_REPO, _REPO / "eodhd"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pandas as pd  # noqa: E402

from positioning import afm_holdings as ah  # noqa: E402
from positioning.config import positioning_root  # noqa: E402
from registers import holdings  # noqa: E402

SUBDIR = Path("registers") / "nl_holdings"
PROBE = Path("registers") / "_probe_2026-10-05"


def _markdown(table: pd.DataFrame) -> str:
    cols = list(table.columns)
    rows = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    rows += ["| " + " | ".join("" if pd.isna(v) else str(v) for v in r) + " |" for r in table.itertuples(index=False)]
    return "\n".join(rows)


def lane_tickers(raw_root: Path) -> pd.DataFrame:
    frames = []
    for lane_dir in sorted(p for p in raw_root.iterdir() if p.is_dir()):
        for file in lane_dir.glob("tickers_*.parquet"):
            frame = pd.read_parquet(file)
            if "Code" not in frame.columns or "Name" not in frame.columns:
                continue
            frames.append(pd.DataFrame({"lane": lane_dir.name, "ticker": frame["Code"].astype(str), "name": frame["Name"].astype(str),
                                        "isin": frame["Isin"] if "Isin" in frame.columns else None}))
    return pd.concat(frames, ignore_index=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    root = positioning_root()
    parser.add_argument("--holdings", type=Path, default=root / PROBE / "afm_holdings.csv")
    parser.add_argument("--capital", type=Path, default=root / PROBE / "afm_capital.csv")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    t0 = time.time()

    import _datadir  # type: ignore[import-not-found]
    import explore_eodhd

    parsed = holdings.parse_holdings(args.holdings)
    notes = holdings.notifications(parsed.lines)
    notes["published_from"] = ah.visible_from(notes["obligation_date"])  # DD-006 C3's rule until measured
    capital = holdings.parse_capital(args.capital)
    kvk = holdings.issuer_kvk(capital)
    print(f"holdings: {len(parsed.lines):,} lines ({parsed.duplicates:,} duplicates, {parsed.dropped} stray rows dropped, "
          f"{parsed.inconsistent_totals} notifications with disagreeing totals), {len(notes):,} notifications, "
          f"{notes['issuer'].nunique()} issuers, {notes['holder'].nunique():,} holders, {notes['obligation_date'].min()} to {notes['obligation_date'].max()}; "
          f"capital register {len(capital):,} rows, {capital['issuer'].nunique():,} issuers ({time.time() - t0:.0f}s)")

    con = explore_eodhd.connect()
    short = con.execute(f"SELECT DISTINCT issuer, isin FROM short_register WHERE market = '{ah.MARKET}'").df()
    tickers = lane_tickers(_datadir.EODHD_RAW_ROOT)
    spans = con.execute("SELECT lane, ticker, CAST(min(date) AS DATE) AS first, CAST(max(date) AS DATE) AS last FROM prices GROUP BY lane, ticker").df()
    priced = {(r.lane, r.ticker): (str(r.first), str(r.last)) for r in spans.itertuples()}
    adv = con.execute("SELECT DISTINCT name FROM sec_adv_advisers WHERE coalesce(n_hedge_funds, 0) > 0 "
                      "UNION SELECT DISTINCT legal_name FROM sec_adv_advisers WHERE coalesce(n_hedge_funds, 0) > 0").df()["name"].dropna().tolist()

    issuer_map = ah.build_issuer_map(notes["issuer"].unique(), short_register=short, tickers=tickers, priced=priced, kvk=kvk)
    holders = ah.classify_holders(notes["holder"].unique(), adv_hedge_fund_names=adv, issuers=notes["issuer"].unique())
    cov = ah.coverage(notes, issuer_map, holders)

    out_dir = root / SUBDIR
    out_dir.mkdir(parents=True, exist_ok=True)
    parsed.lines.to_parquet(out_dir / "lines.parquet", index=False)
    notes.to_parquet(out_dir / "notifications.parquet", index=False)
    issuer_map.to_csv(out_dir / "issuer_map.csv", index=False)
    holders.to_csv(out_dir / "holders.csv", index=False)

    pd.set_option("display.width", 250)
    pd.set_option("display.max_rows", 300)
    pd.set_option("display.max_colwidth", 60)
    sections: list[tuple[str, str]] = []
    print("\nissuer map by source:", issuer_map["source"].value_counts().to_dict(), "| priced:", int(issuer_map["priced"].sum()), "of", len(issuer_map))
    print("holders by kind:", holders["kind"].value_counts().to_dict(), "| ADV matches:", holders["adv_match"].value_counts().to_dict())
    print("\ncoverage (share of notifications):")
    print(cov.to_string(index=False))
    sections.append(("Coverage", _markdown(cov)))

    per_issuer = notes.merge(holders, on="holder", how="left").groupby("issuer").agg(notifications=("holder", "size"), hedge_fund=("kind", lambda s: int((s == "hedge_fund").sum())),
                                                                                       first=("obligation_date", "min"), last=("obligation_date", "max")).reset_index()
    table = issuer_map.merge(per_issuer, on="issuer", how="left").sort_values(["hedge_fund", "notifications"], ascending=False)
    table = table[["issuer", "issuer_kvk", "isin", "lane", "ticker", "priced", "priced_from", "priced_to", "source", "notifications", "hedge_fund", "first", "last", "note"]]
    unpriced = table[~table["priced"].astype(bool)]
    print(f"\nunpriced issuers: {len(unpriced)} ({int(unpriced['notifications'].sum()):,} notifications, {int(unpriced['hedge_fund'].sum())} hedge-fund)")
    print(unpriced[["issuer", "isin", "lane", "ticker", "source", "notifications", "hedge_fund", "last", "note"]].to_string(index=False))
    sections.append(("Issuer map", _markdown(table)))
    hf = holders[holders["kind"] == "hedge_fund"].merge(notes.groupby("holder").size().rename("notifications").reset_index(), on="holder").sort_values("notifications", ascending=False)
    print(f"\nhedge-fund holders: {len(hf)}, {int(hf['notifications'].sum())} notifications")
    print(hf.head(40).to_string(index=False))
    sections.append(("Hedge-fund holders", _markdown(hf)))
    print(f"\nwrote {out_dir} ({time.time() - t0:.0f}s)")
    if args.out:
        head = (f"# DD-006 C2 run {pd.Timestamp.now():%Y-%m-%d %H:%M}\n\n{len(notes):,} notifications, {notes['issuer'].nunique()} issuers, "
                f"{notes['holder'].nunique():,} holders; holdings file {args.holdings}\n")
        args.out.write_text(head + "\n".join(f"\n## {title}\n\n{body}\n" for title, body in sections), encoding="utf-8")
        print("report:", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
