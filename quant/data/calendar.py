"""Trading-calendar helpers (only the calendar dates of the sessions are used, never prices).

``month_end_mask`` comes from scripts/research_regime.py, ``last_session_of_each_month`` from
scripts/research_megacap.py (there ``signal_sessions``).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def month_end_mask(idx: pd.DatetimeIndex) -> np.ndarray:
    """True on the last session of each month (uses only the calendar date of the next session).

    The last session in the data is never treated as a month end (the next session is unknown)."""
    per = idx.to_period("M")
    m = np.zeros(len(idx), dtype=bool)
    m[:-1] = per[:-1] != per[1:]
    return m


def last_session_of_each_month(sessions: pd.DatetimeIndex) -> list:
    """Last session of every month except the last loaded month when it is incomplete (no later session)."""
    s = pd.Series(sessions, index=sessions)
    last = s.groupby(sessions.to_period("M")).max()
    return [d for d in last if d < sessions[-1]]
