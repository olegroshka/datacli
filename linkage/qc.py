"""Quality checks over the stored linkage datasets (read-only, local)."""

from __future__ import annotations

import pandas as pd

from linkage.datasets import AUCTION_ABNORMAL, AUCTION_COSPIKE, ETF_BETAS
from linkage.store import PartitionStore

Finding = tuple[str, str, str]  # (severity, check, detail)


def findings(store: PartitionStore) -> list[Finding]:
    """Checks every dataset shares, then the dataset's own."""
    keys = store.keys_on_disk()
    if not keys:
        return [
            (
                "warn",
                "empty",
                f"{store.name}: nothing built yet -- run: linkage build --run",
            )
        ]
    out: list[Finding] = []
    entries = store.entries()
    missing = sorted(set(keys) - set(entries))
    if missing:
        out.append(
            (
                "error",
                "manifest",
                f"{store.name}: {len(missing)} file(s) without a manifest entry, e.g. {missing[0]}",
            )
        )
    ghosts = sorted(set(entries) - set(keys))
    if ghosts:
        out.append(
            (
                "error",
                "manifest",
                f"{store.name}: {len(ghosts)} manifest entr(ies) without a file, e.g. {ghosts[0]}",
            )
        )
    latest = store.read(keys[-1])
    checks = {ETF_BETAS: _betas, AUCTION_ABNORMAL: _abnormal, AUCTION_COSPIKE: _cospike}
    out.extend(checks[store.name](latest, keys[-1]))
    return out


def _betas(frame: pd.DataFrame, key: str) -> list[Finding]:
    out: list[Finding] = []
    if frame.duplicated(["ticker", "etf", "window"]).any():
        out.append(
            ("error", "keys", f"etf_betas {key}: duplicate (ticker, etf, window) rows")
        )
    if (
        (frame["corr"].abs() > 1.0 + 1e-6) | (frame["corr_ex_mkt"].abs() > 1.0 + 1e-6)
    ).any():
        out.append(
            ("error", "range", f"etf_betas {key}: a correlation outside [-1, 1]")
        )
    market = frame[frame["beta_ex_mkt"].isna()]["etf"].unique()
    if len(market) != 1:
        out.append(
            (
                "error",
                "market",
                f"etf_betas {key}: expected one market ETF without net betas, got {list(market)}",
            )
        )
    else:
        mkt = frame[frame["etf"] == market[0]]
        median = float(mkt["beta"].median())
        if not 0.4 <= median <= 1.5:
            out.append(
                (
                    "warn",
                    "market",
                    f"etf_betas {key}: median market beta {median:.2f} is unusual",
                )
            )
    return out


def _abnormal(frame: pd.DataFrame, key: str) -> list[Finding]:
    out: list[Finding] = []
    if frame.duplicated(["date", "ticker", "side"]).any():
        out.append(
            (
                "error",
                "keys",
                f"auction_abnormal {key}: duplicate (date, ticker, side) rows",
            )
        )
    members = frame[frame["is_member"] & frame["resid"].notna()]
    if len(members):
        centre = (
            members.groupby(["date", "side", "asset_type"])["resid"]
            .median()
            .abs()
            .max()
        )
        if centre > 1e-3:
            out.append(
                (
                    "error",
                    "centre",
                    f"auction_abnormal {key}: members' daily median resid is off zero ({centre:.4f})",
                )
            )
    return out


def _cospike(frame: pd.DataFrame, key: str) -> list[Finding]:
    out: list[Finding] = []
    if (frame["ticker"] == frame["neighbor"]).any():
        out.append(
            ("error", "self", f"auction_cospike {key}: a name is its own neighbour")
        )
    if frame.duplicated(["ticker", "neighbor"]).any():
        out.append(
            (
                "error",
                "keys",
                f"auction_cospike {key}: duplicate (ticker, neighbor) rows",
            )
        )
    if (frame["corr"].abs() > 1.0 + 1e-6).any():
        out.append(
            ("error", "range", f"auction_cospike {key}: a correlation outside [-1, 1]")
        )
    return out
