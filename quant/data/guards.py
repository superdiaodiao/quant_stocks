"""Date guards: no frame may carry a row dated after a study's end (or before its start).

Two flavours exist in the studies and both are kept, unchanged, because frozen results depend on them:

- ``assert_dev_dates`` / ``truncate_dev`` (from scripts/research_qqq_timing.py): compare parsed dates, end only,
  raise ``FutureDataError``.
- ``assert_window`` / ``truncate_window`` (from scripts/research_canslim_dev.py): ``truncate_window`` compares the
  date strings, both ends, and they raise ``DateGuardError``.

Unlike the originals there is no study-specific default end: the caller always passes it.
"""
from __future__ import annotations

import pandas as pd


class FutureDataError(AssertionError):
    pass


class DateGuardError(AssertionError):
    pass


def assert_dev_dates(dates, end: str) -> None:
    """Raise when any date (YYYY-MM-DD strings or Timestamps) is after ``end``."""
    values = pd.to_datetime(pd.Index(list(dates)) if not isinstance(dates, (pd.Series, pd.Index)) else dates)
    if len(values) and values.max() > pd.Timestamp(end):
        raise FutureDataError(f"date {values.max().date()} is after the development end {end}")


def truncate_dev(frame: pd.DataFrame, column: str, end: str) -> pd.DataFrame:
    """Rows with ``column`` <= end, asserted."""
    out = frame[pd.to_datetime(frame[column]) <= pd.Timestamp(end)].copy()
    assert_dev_dates(out[column], end)
    return out


def assert_window(dates, start: str | None, end: str, what: str = "dates") -> None:
    """Raise when any date is after ``end`` (or before ``start``)."""
    values = pd.to_datetime(pd.Series(list(dates)) if not isinstance(dates, (pd.Series, pd.Index)) else dates)
    values = values.dropna()
    if not len(values):
        return
    if values.max() > pd.Timestamp(end):
        raise DateGuardError(f"{what}: {values.max().date()} is after {end}")
    if start is not None and values.min() < pd.Timestamp(start):
        raise DateGuardError(f"{what}: {values.min().date()} is before {start}")


def truncate_window(frame: pd.DataFrame, column: str, start: str, end: str) -> pd.DataFrame:
    """Rows with start <= ``column`` <= end (string comparison of ISO dates), asserted."""
    s = frame[column].astype(str)
    out = frame.loc[(s >= start) & (s <= end)].copy()
    assert_window(out[column], start, end, column)
    return out
