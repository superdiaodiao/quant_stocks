"""Frozen reference copy from scripts/research_canslim_dev.py at master d91c71a (before phase 2): the original
functions tests/quant/test_quant_core.py compares the quant core with. Do not edit."""
from __future__ import annotations
import pandas as pd
from tests.quant.originals import reversal_dev as rev
from scripts import study_data_version as dv  # noqa: E402


DEV_END = "2022-12-31"


class DateGuardError(AssertionError):
    pass


def assert_window(dates, start: str | None = None, end: str = DEV_END, what: str = "dates") -> None:
    """Raise when any date is after ``end`` (or before ``start``)."""
    values = pd.to_datetime(pd.Series(list(dates)) if not isinstance(dates, (pd.Series, pd.Index)) else dates)
    values = values.dropna()
    if not len(values):
        return
    if values.max() > pd.Timestamp(end):
        raise DateGuardError(f"{what}: {values.max().date()} is after {end}")
    if start is not None and values.min() < pd.Timestamp(start):
        raise DateGuardError(f"{what}: {values.min().date()} is before {start}")


def truncate(frame: pd.DataFrame, column: str, start: str, end: str = DEV_END) -> pd.DataFrame:
    s = frame[column].astype(str)
    out = frame.loc[(s >= start) & (s <= end)].copy()
    assert_window(out[column], start, end, column)
    return out


def order_cost(value: float, price: float, sell: bool, hs: float) -> float:
    if value <= 0:
        return 0.0
    return rev.order_cost(value / price, price, sell=sell, hs=hs)["total"]
