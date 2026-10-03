"""positioning.export: the point-in-time carry-forward of a published signal."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from positioning import export  # noqa: E402


def test_carry_forward_is_visible_only_after_publication_and_until_the_next() -> None:
    dates = pd.bdate_range("2024-01-01", "2024-02-29")
    obs = pd.DataFrame(
        {
            "symbol": ["A", "A", "B", "B"],
            "published_at": pd.to_datetime(["2024-01-10", "2024-01-25", "2024-01-13", "2024-01-25"]),  # the 13th is a Saturday
            "value": [1.0, 2.0, 5.0, 6.0],
        }
    )
    wide = export.carry_forward_wide(obs, dates=dates)
    assert list(wide.columns) == ["A", "B"] and len(wide) == len(dates)
    assert np.isnan(wide.loc["2024-01-10", "A"])  # the publication day itself sees nothing
    assert wide.loc["2024-01-11", "A"] == 1.0 and wide.loc["2024-01-24", "A"] == 1.0
    assert wide.loc["2024-01-26", "A"] == 2.0 and wide.loc["2024-02-29", "A"] == 2.0
    assert np.isnan(wide.loc["2024-01-12", "B"]) and wide.loc["2024-01-15", "B"] == 5.0  # Saturday -> Monday
    assert wide.loc["2024-01-26", "B"] == 6.0
    # a prefix of the dates gives the prefix of the frame: nothing leaks backward
    short = export.carry_forward_wide(obs, dates=dates[:20])
    pd.testing.assert_frame_equal(short, wide.iloc[:20])
    # a publication after the last date is dropped, duplicates keep the last
    late = pd.concat([obs, pd.DataFrame({"symbol": ["A", "A"], "published_at": pd.to_datetime(["2024-03-05", "2024-01-10"]), "value": [9.0, 1.5]})])
    wide2 = export.carry_forward_wide(late, dates=dates)
    assert wide2.loc["2024-01-11", "A"] == 1.5 and wide2.loc["2024-02-29", "A"] == 2.0
