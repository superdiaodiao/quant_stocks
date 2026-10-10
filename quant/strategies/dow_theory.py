"""Dow Theory primary trend from swing highs / lows of the Dow Jones Industrial and Transportation averages with
mutual confirmation, the rule grid and the Dow data loader of studies/dow_theory.py.

Extracted unchanged from studies/dow_theory.py in the slimming pass (``swing_levels``, ``break_direction``,
``confirm_trend``, ``DowRule``, ``rule_grid``, ``dow_state``, ``target_on_sessions``, ``load_dow``, ``dow_checks``,
``signal_list``). The chart files and the date guard end are arguments of ``load_dow``. The definitions (swing
confirmation dated t, family R's retracement test against the intermediate pivots, family P's strict pattern) are
in the study's docstring and docs/research_ledger_dow_theory.md section 0.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from quant.data.guards import assert_dev_dates
from quant.data.sources.yahoo import parse_chart


# ======================================================================== swing detection

def _ref_point(events: list, kind: str, t: int, before: int, own: tuple) -> float:
    """Most recent pivot of ``kind`` ('L' or 'H') confirmed by t whose extreme lies before index ``before``;
    ``own`` = (extreme index, value) of the tracker's own last pivot of that kind. The later of the two wins."""
    best_i, best_v = own
    for ct, k, ei, v in reversed(events):
        if ct > t or k != kind or ei >= before:
            continue
        if np.isnan(best_v) or ei > best_i:
            best_i, best_v = ei, v
        break
    return best_v


def swing_levels(close, pct: float, min_days: int = 0, retrace: float = 0.0, ref: str = "intermediate") -> pd.DataFrame:
    """Causal swing points. Returns, for every session t, the swing levels known at the close of t:
    last_high, prev_high, last_low, prev_low (confirmed points) and a 'confirm' column ('H', 'L' or '').

    With ``retrace`` > 0 a reaction must also retrace that fraction of the preceding leg. ``ref`` sets where the
    preceding leg starts: "intermediate" (version 2, the frozen definition) = the latest pivot before the current
    extreme among this tracker's own pivots and the pivots of a pct + min_days tracker without the retracement test;
    "leg" (version 1, kept for the record) = this tracker's own last pivot only, which can lock up in long trends.
    ``df.attrs['events']`` lists (confirm index, kind, extreme index, value)."""
    s = pd.Series(close, dtype=float)
    c = s.values
    n = len(c)
    inter = []
    if retrace > 0 and ref == "intermediate":
        inter = swing_levels(c, pct, min_days, 0.0).attrs["events"]
    out = np.full((n, 4), np.nan)
    conf = np.array([""] * n, dtype=object)
    events = []
    last_h = prev_h = last_l = prev_l = np.nan
    i_last_h = i_last_l = -1
    mode = 0                         # 0 = no swing yet, +1 = up-leg (tracking a high), -1 = down-leg
    hi = lo = c[0] if n else np.nan
    ihi = ilo = 0
    for t in range(n):
        x = c[t]
        if mode == 0:
            if x > hi:
                hi, ihi = x, t
            if x < lo:
                lo, ilo = x, t
            if (hi - x) / hi >= pct and t - ihi >= min_days:
                last_h, i_last_h, mode = hi, ihi, -1
                events.append((t, "H", ihi, hi))
                seg = c[ihi:t + 1]
                ilo = ihi + int(np.argmin(seg))
                lo = c[ilo]
                conf[t] = "H"
            elif (x - lo) / lo >= pct and t - ilo >= min_days:
                last_l, i_last_l, mode = lo, ilo, +1
                events.append((t, "L", ilo, lo))
                seg = c[ilo:t + 1]
                ihi = ilo + int(np.argmax(seg))
                hi = c[ihi]
                conf[t] = "L"
        elif mode == +1:
            if x > hi:
                hi, ihi = x, t
            seg = c[ihi:t + 1]
            j = int(np.argmin(seg))
            rmin = seg[j]
            ok = (hi - rmin) / hi >= pct and t - ihi >= min_days
            if ok and retrace > 0:
                base = _ref_point(inter, "L", t, ihi, (i_last_l, last_l))
                ok = np.isnan(base) or (hi - rmin) >= retrace * (hi - base)
            if ok:
                prev_h, last_h, i_last_h = last_h, hi, ihi
                events.append((t, "H", ihi, hi))
                mode, lo, ilo = -1, rmin, ihi + j
                conf[t] = "H"
        else:
            if x < lo:
                lo, ilo = x, t
            seg = c[ilo:t + 1]
            j = int(np.argmax(seg))
            rmax = seg[j]
            ok = (rmax - lo) / lo >= pct and t - ilo >= min_days
            if ok and retrace > 0:
                base = _ref_point(inter, "H", t, ilo, (i_last_h, last_h))
                ok = np.isnan(base) or (rmax - lo) >= retrace * (base - lo)
            if ok:
                prev_l, last_l, i_last_l = last_l, lo, ilo
                events.append((t, "L", ilo, lo))
                mode, hi, ihi = +1, rmax, ilo + j
                conf[t] = "L"
        out[t] = (last_h, prev_h, last_l, prev_l)
    df = pd.DataFrame(out, index=s.index, columns=["last_high", "prev_high", "last_low", "prev_low"])
    df["confirm"] = conf
    df.attrs["events"] = events
    return df


def break_direction(close, levels: pd.DataFrame, strict: bool = False) -> pd.Series:
    """+1 after a close above the last confirmed swing high, -1 after a close below the last swing low, else
    the previous value (0 before any break)."""
    c = pd.Series(close, dtype=float).values
    lh, ph, ll, pl = (levels[k].values for k in ("last_high", "prev_high", "last_low", "prev_low"))
    d, out = 0, np.zeros(len(c), dtype=int)
    for t in range(len(c)):
        up = not np.isnan(lh[t]) and c[t] > lh[t] and (not strict or ll[t] > pl[t])
        dn = not np.isnan(ll[t]) and c[t] < ll[t] and (not strict or lh[t] < ph[t])
        if up:
            d = 1
        elif dn:
            d = -1
        out[t] = d
    return pd.Series(out, index=levels.index)


def confirm_trend(dirs: list[pd.Series], initial: int = 1) -> pd.Series:
    """Primary trend: +1 when every average's direction is +1, -1 when every one is -1, else unchanged."""
    m = pd.concat(dirs, axis=1).values
    s, out = initial, np.zeros(len(m), dtype=int)
    for t in range(len(m)):
        if (m[t] == 1).all():
            s = 1
        elif (m[t] == -1).all():
            s = -1
        out[t] = s
    return pd.Series(out, index=dirs[0].index)


# ======================================================================== rules

@dataclass(frozen=True)
class DowRule:
    family: str                       # Z, R (version 2), R1 (version 1, first run), P, N
    pct: float
    min_days: int = 0
    retrace: float = 0.0
    averages: tuple = ("DJI", "DJT")

    @property
    def strict(self) -> bool:
        return self.family == "P"

    @property
    def name(self) -> str:
        base = f"{self.family}_p{int(round(self.pct * 100))}"
        if self.family in ("R", "R1"):
            base += f"_D{self.min_days}"
        if self.family == "N":
            base += "_" + "".join(self.averages)
        return base


def rule_grid() -> list[DowRule]:
    rules = [DowRule("Z", p) for p in (0.03, 0.05, 0.08)]
    rules += [DowRule("R", p, 15, 1 / 3) for p in (0.03, 0.05, 0.08)]
    rules += [DowRule("R", 0.05, d, 1 / 3) for d in (10, 20)]
    rules += [DowRule("P", p) for p in (0.03, 0.05, 0.08)]
    # version 1 of family R (first run, definition error logged in the ledger); counted as trials
    rules += [DowRule("R1", p, 15, 1 / 3) for p in (0.03, 0.05, 0.08)]
    rules += [DowRule("R1", 0.05, d, 1 / 3) for d in (10, 20)]
    rules += [DowRule("N", 0.05, averages=("DJI",)), DowRule("N", 0.05, averages=("DJT",))]
    return rules


def dow_state(rule: DowRule, closes: pd.DataFrame) -> pd.Series:
    """+1 bull / -1 bear at each close of ``closes`` (columns DJI, DJT), using data up to that close only."""
    dirs = []
    for a in rule.averages:
        c = closes[a].dropna()
        lv = swing_levels(c, rule.pct, rule.min_days, rule.retrace, "leg" if rule.family == "R1" else "intermediate")
        dirs.append(break_direction(c, lv, rule.strict).reindex(closes.index).ffill().fillna(0).astype(int))
    if len(dirs) == 1:
        return dirs[0].replace(0, 1)
    return confirm_trend(dirs)


def target_on_sessions(state: pd.Series, sessions: pd.DatetimeIndex) -> pd.Series:
    """Exposure (1 bull, 0 bear) decided at each trading session's close; Dow dates carried forward only."""
    e = (state > 0).astype(float)
    full = e.reindex(e.index.union(sessions)).ffill()
    return full.reindex(sessions).fillna(1.0)


# ======================================================================== data

def load_dow(files: dict, end: str) -> tuple[pd.DataFrame, dict]:
    """Closes of the averages in ``files`` ({name: Yahoo chart JSON}), truncated at ``end`` and asserted."""
    cols, guard = {}, {}
    for k, path in files.items():
        df = parse_chart(path, end)          # truncated at ``end`` immediately and asserted
        s = pd.Series(df["close"].values, index=pd.DatetimeIndex(df["date"]))
        assert_dev_dates(s.index, end)
        cols[k] = s
        guard[k] = {"first": str(s.index.min().date()), "last": str(s.index.max().date()), "rows": int(len(s))}
    return pd.DataFrame(cols), guard


def dow_checks(closes: pd.DataFrame, sessions: pd.DatetimeIndex, start: str, end: str) -> dict:
    out = {}
    for k in closes:
        s = closes[k].dropna()
        r = s.pct_change().dropna()
        gaps = pd.Series(s.index[1:] - s.index[:-1]).dt.days
        py = s.groupby(s.index.year).size()
        out[k] = {"rows": int(len(s)), "sessions_per_year_min": int(py.iloc[:-1].min()) if len(py) > 1 else int(py.min()),
                  "max_calendar_gap_days": int(gaps.max()),
                  "abs_daily_moves_over_7pct": [f"{d.date()} {v:+.1%}" for d, v in r[r.abs() > 0.07].items()],
                  "zero_return_days": int((r == 0).sum())}
    tr = sessions[(sessions >= pd.Timestamp(start)) & (sessions <= pd.Timestamp(end))]
    out["trading_sessions_missing_in_dow"] = int(len(tr.difference(closes.dropna().index)))
    out["dow_dates_not_trading_sessions"] = int(len(closes.loc[start:end].dropna().index.difference(tr)))
    return out


# ======================================================================== signal lists

def signal_list(state: pd.Series, start: str, end: str) -> list[str]:
    s = state.loc[:end]
    ch = s[s != s.shift(1)].iloc[1:]
    ch = ch.loc[start:]
    return [f"{d.date()} {'bull' if v > 0 else 'bear'}" for d, v in ch.items()]
