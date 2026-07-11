"""Small shared helpers for the macro providers."""

from __future__ import annotations

import pandas as pd


def merge_on(
    existing: pd.DataFrame | None, new: pd.DataFrame, keys: list[str]
) -> pd.DataFrame:
    """Upsert ``new`` into ``existing`` on ``keys`` (new wins on conflict).

    Rows present only in ``existing`` are retained -- so a re-fetch where some
    series failed or returned nothing never destroys their previously-fetched data.
    """
    if existing is None or existing.empty:
        return new
    if new is None or new.empty:
        return existing
    combined = pd.concat([existing, new], ignore_index=True)
    return combined.drop_duplicates(subset=keys, keep="last").reset_index(drop=True)
