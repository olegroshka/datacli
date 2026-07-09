"""Post-fetch QC for the EODHD us_common refresh + ad-hoc ticker pull.

Verifies three things after running the prices/dividends fetchers:

1. The targeted ad-hoc tickers landed with the expected first/last dates and
   no internal trading-day gaps (checked against a data-derived market
   calendar, so holidays are not false positives).
2. The existing universe tail-refresh actually advanced — i.e. no firm is left
   stale at the pre-refresh coverage date.
3. Structural price/dividend quality: NaNs, non-positive closes, duplicate
   (ticker, exchange, date) keys, adjusted_close presence, fetch-state errors.

Usage:
    uv run python eodhd/qc_eodhd_refresh.py \
        --tickers CRWD DDOG DASH PLTR PYPL SHOP ZS APP ARM BKR CEG GEHC SNDK WDAY
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
from _datadir import EODHD_RAW_ROOT

_ROOT = Path(__file__).resolve().parents[2]
RAW = EODHD_RAW_ROOT / "us_common"
PRICES = RAW / "prices_daily.parquet"
PRICES_STATE = RAW / "prices_fetch_state.csv"
DIVS = RAW / "dividends_history.parquet"
DIVS_STATE = RAW / "dividends_fetch_state.csv"

# Expected first dates from the read-only availability probe (sanity anchor).
EXPECTED_FIRST = {
    "BKR": "1990-01-02",
    "WDAY": "2012-10-12",
    "SHOP": "2015-05-20",
    "PYPL": "2015-07-06",
    "ZS": "2018-03-16",
    "CRWD": "2019-06-12",
    "DDOG": "2019-09-19",
    "PLTR": "2020-09-30",
    "DASH": "2020-12-09",
    "APP": "2021-04-15",
    "CEG": "2022-01-19",
    "GEHC": "2022-12-15",
    "ARM": "2023-09-14",
    "SNDK": "2025-02-13",
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="QC the EODHD refresh")
    p.add_argument(
        "--tickers",
        nargs="+",
        required=True,
        help="Ad-hoc tickers to verify (bare codes)",
    )
    p.add_argument("--exchange", default="US")
    p.add_argument(
        "--stale-from",
        default="2026-05-06",
        help="Pre-refresh coverage date; firms not advancing past this are flagged stale",
    )
    return p.parse_args()


def hr(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def build_market_calendar(
    prices: pd.DataFrame, min_firms: int = 200
) -> pd.DatetimeIndex:
    """Trading days where at least `min_firms` firms have a bar — a robust proxy
    for the exchange calendar (filters out sparse/erroneous one-off dates)."""
    counts = prices.groupby("date")["ticker"].size()
    cal = counts[counts >= min_firms].index
    return pd.DatetimeIndex(sorted(cal))


def main() -> None:
    args = parse_args()
    tickers = [t.upper() for t in args.tickers]

    prices = pd.read_parquet(PRICES)
    prices["date"] = pd.to_datetime(prices["date"])
    pstate = pd.read_csv(PRICES_STATE)

    issues: list[str] = []

    # ---- 0. dataset-level summary -------------------------------------------------
    hr("0. DATASET SUMMARY")
    n_firms = prices[["ticker", "exchange"]].drop_duplicates().shape[0]
    print(
        f"prices_daily.parquet : {len(prices):,} rows, {n_firms} firms, "
        f"{prices['date'].min().date()} -> {prices['date'].max().date()}"
    )

    market_cal = build_market_calendar(prices)
    print(
        f"derived market calendar: {len(market_cal)} trading days, "
        f"{market_cal.min().date()} -> {market_cal.max().date()}"
    )

    # ---- 1. ad-hoc tickers: presence, range, internal gaps ------------------------
    hr("1. AD-HOC TICKERS — coverage & trading-day gaps")
    rows = []
    for t in tickers:
        sub = prices[(prices["ticker"] == t) & (prices["exchange"] == args.exchange)]
        if sub.empty:
            issues.append(f"[MISSING] {t} not present in prices_daily.parquet")
            rows.append({"ticker": t, "status": "MISSING"})
            continue
        sub = sub.sort_values("date")
        first, last = sub["date"].iloc[0], sub["date"].iloc[-1]
        # Expected trading days within this ticker's own [first,last] span.
        span_cal = market_cal[(market_cal >= first) & (market_cal <= last)]
        have = set(sub["date"])
        missing = [d for d in span_cal if d not in have]
        # Extra dates the ticker has that the market calendar lacks (data noise).
        extra = sorted(set(sub["date"]) - set(span_cal))
        exp_first = EXPECTED_FIRST.get(t)
        first_ok = (exp_first is None) or (first.date().isoformat() == exp_first)
        dup = int(sub.duplicated(subset=["ticker", "exchange", "date"]).sum())
        rows.append(
            {
                "ticker": t,
                "first": first.date().isoformat(),
                "exp_first": exp_first or "-",
                "first_ok": first_ok,
                "last": last.date().isoformat(),
                "rows": len(sub),
                "missing_tdays": len(missing),
                "extra_dates": len(extra),
                "dups": dup,
            }
        )
        if not first_ok:
            issues.append(f"[FIRST-DATE] {t}: got {first.date()} expected {exp_first}")
        if missing:
            sample = ", ".join(d.date().isoformat() for d in missing[:5])
            issues.append(
                f"[GAP] {t}: {len(missing)} missing trading day(s) e.g. {sample}"
            )
        if dup:
            issues.append(f"[DUP] {t}: {dup} duplicate (ticker,exchange,date) rows")
    print(pd.DataFrame(rows).to_string(index=False))

    # ---- 2. structural price quality on the 14 ------------------------------------
    hr("2. AD-HOC TICKERS — structural quality")
    sub14 = prices[
        prices["ticker"].isin(tickers) & (prices["exchange"] == args.exchange)
    ]
    close = pd.to_numeric(sub14["close"], errors="coerce")
    adj = pd.to_numeric(sub14["adjusted_close"], errors="coerce")
    vol = pd.to_numeric(sub14["volume"], errors="coerce")
    q = {
        "nan_close": int(close.isna().sum()),
        "nonpos_close": int((close <= 0).sum()),
        "nan_adj_close": int(adj.isna().sum()),
        "neg_volume": int((vol < 0).sum()),
        "rows_total": len(sub14),
    }
    print(q)
    if q["nan_close"]:
        issues.append(f"[QUALITY] {q['nan_close']} NaN closes among ad-hoc tickers")
    if q["nonpos_close"]:
        issues.append(
            f"[QUALITY] {q['nonpos_close']} non-positive closes among ad-hoc tickers"
        )
    if q["nan_adj_close"]:
        issues.append(
            f"[QUALITY] {q['nan_adj_close']} NaN adjusted_close among ad-hoc tickers"
        )

    # ---- 3. universe tail-refresh advanced ----------------------------------------
    hr("3. UNIVERSE TAIL-REFRESH — staleness & fetch-state")
    stale_cut = pd.Timestamp(args.stale_from)
    pstate["latest_data_date"] = pd.to_datetime(
        pstate["latest_data_date"], errors="coerce"
    )
    print("prices fetch_state status counts:")
    print(pstate["status"].value_counts().to_string())
    bad_status = pstate[~pstate["status"].isin(["ok", "up_to_date"])]
    if len(bad_status):
        issues.append(
            f"[FETCH-STATE] {len(bad_status)} prices rows with non-ok status "
            f"({bad_status['status'].value_counts().to_dict()})"
        )
    stale = pstate[pstate["latest_data_date"] <= stale_cut]
    print(f"\nfirms with latest_data_date <= {args.stale_from}: {len(stale)}")
    if len(stale):
        issues.append(
            f"[STALE] {len(stale)} firms did not advance past {args.stale_from}"
        )
        print(
            stale[["ticker", "exchange", "status", "latest_data_date"]]
            .head(20)
            .to_string(index=False)
        )
    print(
        f"\nuniverse latest_data_date range: "
        f"{pstate['latest_data_date'].min()} -> {pstate['latest_data_date'].max()}"
    )

    # ---- 4. dividends -------------------------------------------------------------
    hr("4. DIVIDENDS — ad-hoc tickers present & fetch-state")
    if DIVS.exists():
        divs = pd.read_parquet(DIVS)
        present = sorted(set(divs["ticker"]) & set(tickers))
        absent = sorted(set(tickers) - set(divs["ticker"]))
        print(
            f"of {len(tickers)} ad-hoc tickers, {len(present)} have dividend rows: {present}"
        )
        print(f"no dividend rows (may be legitimately non-paying): {absent}")
        if DIVS_STATE.exists():
            dstate = pd.read_csv(DIVS_STATE)
            d14 = dstate[dstate["ticker"].isin(tickers)]
            print("\ndividend fetch_state for ad-hoc tickers:")
            print(
                d14[
                    ["ticker", "status", "response_rows", "coverage_through"]
                ].to_string(index=False)
            )
            dbad = dstate[~dstate["status"].isin(["ok", "up_to_date", "empty"])]
            if len(dbad):
                issues.append(
                    f"[DIV FETCH-STATE] {len(dbad)} rows non-ok "
                    f"({dbad['status'].value_counts().to_dict()})"
                )
    else:
        issues.append("[DIV] dividends_history.parquet not found")

    # ---- verdict ------------------------------------------------------------------
    hr("VERDICT")
    if not issues:
        print(
            "PASS — no issues found. All ad-hoc tickers present with expected ranges, "
            "no trading-day gaps, universe fully refreshed, no structural defects."
        )
    else:
        print(f"{len(issues)} ISSUE(S) FOUND:")
        for s in issues:
            print("  - " + s)


if __name__ == "__main__":
    main()
