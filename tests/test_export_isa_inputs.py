"""The ISA price export: pence, dollars, dividends and the GBPUSD splice."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "export_isa_inputs.py"
_spec = importlib.util.spec_from_file_location("export_isa_inputs", _PATH)
isa = importlib.util.module_from_spec(_spec)
import sys  # noqa: E402

sys.modules["export_isa_inputs"] = isa
_spec.loader.exec_module(isa)


def _dates(n: int, start: str = "2024-01-01") -> pd.DatetimeIndex:
    return pd.bdate_range(start, periods=n)


def _bars(close, adjusted=None, index=None) -> pd.DataFrame:
    close = list(close)
    index = index if index is not None else _dates(len(close))
    adjusted = list(adjusted) if adjusted is not None else close
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "adjusted_close": adjusted, "volume": [1000.0] * len(close)},
        index=index,
    )


def test_pence_become_pounds():
    out = isa.to_pounds_total_return(_bars([1000.0, 1010.0]), "GBX")
    assert out["close"].tolist() == pytest.approx([10.0, 10.1])


def test_dividends_enter_through_the_adjusted_close():
    # Before the ex-date the adjusted close sits below the close; the return is the total return.
    out = isa.to_pounds_total_return(_bars([100.0, 98.0], adjusted=[98.0, 98.0]), "GBP")
    assert out["close"].iloc[1] / out["close"].iloc[0] - 1.0 == pytest.approx(0.0)


def test_dollars_become_pounds_at_that_days_rate():
    index = _dates(2)
    out = isa.to_pounds_total_return(_bars([100.0, 100.0], index=index), "USD", pd.Series([1.25, 1.0], index=index))
    assert out["close"].tolist() == pytest.approx([80.0, 100.0])


def test_dollar_line_carries_the_last_rate_over_a_gap_and_never_uses_a_later_one():
    index = _dates(3)
    rate = pd.Series([2.0, 4.0], index=[index[0], index[2]])
    out = isa.to_pounds_total_return(_bars([100.0, 100.0, 100.0], index=index), "USD", rate)
    assert out["close"].tolist() == pytest.approx([50.0, 50.0, 25.0])


def test_dollar_line_without_a_rate_is_refused():
    index = _dates(2)
    with pytest.raises(ValueError, match="no GBPUSD"):
        isa.to_pounds_total_return(_bars([100.0, 100.0], index=index), "USD", pd.Series([1.3], index=[index[1]]))
    with pytest.raises(ValueError, match="needs GBPUSD"):
        isa.to_pounds_total_return(_bars([100.0]), "USD")


def test_unknown_currency_is_refused():
    with pytest.raises(ValueError, match="unknown currency"):
        isa.to_pounds_total_return(_bars([100.0]), "NOK")


def test_implied_rate_from_two_quotes_of_one_security():
    index = _dates(1)
    assert isa.implied_gbpusd(pd.Series([67.0], index=index), pd.Series([5000.0], index=index)).iloc[0] == pytest.approx(1.34)


def test_splice_uses_fred_then_the_implied_rate():
    index = _dates(5)
    fred = pd.Series([1.30, 1.31, 1.32], index=index[:3])
    implied = pd.Series([1.301, 1.311, 1.321, 1.33, 1.34], index=index)
    out = isa.splice_gbpusd(fred, implied)
    assert out.tolist() == pytest.approx([1.30, 1.31, 1.32, 1.33, 1.34])


def test_splice_is_refused_when_the_two_disagree():
    index = _dates(4)
    with pytest.raises(ValueError, match="refusing"):
        isa.splice_gbpusd(pd.Series([1.30, 1.31], index=index[:2]), pd.Series([1.40, 1.41, 1.42, 1.43], index=index))


def test_calendar_carries_a_foreign_holiday_without_volume_and_invents_nothing_before_the_first_bar():
    calendar = _dates(5)
    bars = isa.to_pounds_total_return(_bars([10.0, 12.0], index=[calendar[1], calendar[3]]), "GBP")
    out = isa.on_calendar(bars, calendar)
    assert out.index.tolist() == calendar[1:].tolist()
    assert out["close"].tolist() == pytest.approx([10.0, 10.0, 12.0, 12.0])
    assert out["volume"].tolist() == [1000.0, 0.0, 1000.0, 0.0]


def test_calendar_keeps_a_bar_from_a_day_the_calendar_lacks_only_as_carry():
    calendar = _dates(3)
    extra = calendar[0] + pd.Timedelta(days=1, hours=1)
    bars = isa.to_pounds_total_return(_bars([10.0, 11.0], index=[calendar[0], extra]), "GBP")
    assert isa.on_calendar(bars, calendar)["close"].tolist() == pytest.approx([10.0, 10.0, 11.0])
