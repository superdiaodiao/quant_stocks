"""Frozen reference copy from scripts/research_livermore.py at master d91c71a (before phase 2): the original
functions tests/quant/test_quant_core.py compares the quant core with. Do not edit."""
from __future__ import annotations
from dataclasses import dataclass
import numpy as np
import pandas as pd
from originals import canslim_dev as cs
from originals import reversal_dev as rev
from scripts import study_data_version as dv


INPUTS = dv.INPUTS
CACHE = dv.CACHE
WINDOWS = {
    "dev": {"perf_start": "2017-01-01", "perf_end": "2022-12-31", "price_start": "2015-10-01",
            "universe_start": "2016-01-01", "judged_from": "2017-01-01"},
    "test1": {"perf_start": "2023-01-01", "perf_end": "2026-09-30", "price_start": "2021-10-01",
              "universe_start": "2022-01-01", "judged_from": "2023-01-01"},
    "test2": {"perf_start": "2012-01-01", "perf_end": "2016-12-31", "price_start": "2011-06-01",
              "universe_start": "2012-01-01", "judged_from": "2014-01-01"},   # 2012-2013: >2% slots missing
}


@dataclass
class WinData:
    name: str
    spec: dict
    sessions: pd.DatetimeIndex
    universe: pd.DataFrame
    sig_idx: pd.DataFrame
    perf_idx: pd.DataFrame
    close: pd.DataFrame
    close_adj: pd.DataFrame
    vol_adj: pd.DataFrame
    last_row: pd.Series
    qqq_close: pd.Series
    qqq_perf_idx: pd.Series
    terminal_events: pd.DataFrame
    guard: dict


def load_window(name: str, terminal_awaiting: float = rev.TERMINAL_D5) -> WinData:
    """Load one window. Every frame is cut to [price_start, end] and asserted; ``end`` is the window end or the last
    session with stock prices, whichever is earlier."""
    spec = dict(WINDOWS[name])
    ps, end = spec["price_start"], spec["perf_end"]
    guard = {"window": name, **spec, "frames": {}}

    def note(label, frame, column, start, stop):
        out = cs.truncate(frame, column, start, stop)
        guard["frames"][label] = {"rows_kept": int(len(out)),
                                  "rows_dropped_after_end": int((frame[column].astype(str) > stop).sum()),
                                  "rows_dropped_before_start": int((frame[column].astype(str) < start).sum()),
                                  "min_date": str(out[column].min()), "max_date": str(out[column].max())}
        return out

    panel = pd.read_csv(CACHE / "prices/daily_panel.csv.gz", dtype={"security_id": str, "date": str},
                        usecols=["security_id", "date", "close_raw", "volume_raw", "split_factor", "tr"])
    panel = note("daily_panel", panel, "date", ps, end)
    end = min(end, str(panel["date"].max()))           # test1: stock prices stop at 2026-08-31
    spec["effective_end"] = end
    guard["effective_end"] = end
    uni = pd.read_csv(INPUTS / "weekly_universe_top300.csv.gz", dtype=str)
    uni = note("weekly_universe_top300", uni, "week_end", spec["universe_start"], end)
    uni["dv50_rank"] = pd.to_numeric(uni["dv50_rank"], errors="coerce")
    uni["week_end"] = pd.to_datetime(uni["week_end"])
    qqq = pd.read_csv(CACHE / "factors/qqq_joined.csv", dtype={"date": str})
    qqq = note("qqq_joined", qqq, "date", ps, end).sort_values("date")
    term = pd.read_csv(INPUTS / "terminal_returns_2012_2026.csv", dtype=str)
    term = term[term["last_price_date"].notna()]
    term = note("terminal_returns (by last_price_date)", term, "last_price_date", ps, end)

    ids = set(uni["security_id"])
    successor = {r.security_id: r.continued_as for r in term.itertuples() if isinstance(r.continued_as, str)}
    ids |= {successor[s] for s in list(ids) if s in successor}
    panel = panel[panel["security_id"].isin(ids)].copy()
    sessions = pd.DatetimeIndex(pd.to_datetime(qqq["date"]))
    qqq_close = pd.Series(qqq["close"].to_numpy(float), index=sessions)
    qqq_tr = pd.Series(((qqq["close"] + qqq["dividend"].fillna(0)) / qqq["close"].shift(1) - 1).to_numpy(),
                       index=sessions)
    panel["date"] = pd.to_datetime(panel["date"])
    piv = {c: panel.pivot(index="date", columns="security_id", values=c).reindex(sessions)
           for c in ("tr", "close_raw", "volume_raw", "split_factor")}
    tr, close, vol, split = piv["tr"], piv["close_raw"], piv["volume_raw"], piv["split_factor"].fillna(1.0)
    has_row = close.notna()
    last_row = has_row[::-1].idxmax()
    last_row[~has_row.any()] = pd.NaT
    for pred, succ in successor.items():
        if pred not in tr.columns or succ not in tr.columns or pd.isna(last_row.get(pred)):
            continue
        after = tr.index > last_row[pred]
        for fr in (tr, close, vol, split):
            fr.loc[after, pred] = fr.loc[after, succ]
        last_row[pred] = last_row[succ]

    events = []
    last_session = sessions[-1]
    term_by_id = {r.security_id: r for r in term.itertuples()}
    for sid in tr.columns:
        lr = last_row[sid]
        if pd.isna(lr) or lr >= last_session:
            continue
        nxt = sessions[sessions > lr][0]
        r = term_by_id.get(sid)
        status = r.status if r is not None else "no_terminal_record"
        value = float(r.terminal_return) if status == "computed" else (terminal_awaiting if status == "awaiting_d5"
                                                                        else 0.0)
        tr.loc[nxt, sid] = value
        events.append({"security_id": sid, "last_row": lr.date().isoformat(), "booked_on": nxt.date().isoformat(),
                       "status": status, "terminal_return": value})
    events = pd.DataFrame(events)

    cum_split = split.cumprod()
    close_adj = close * cum_split
    vol_adj = vol / cum_split
    sig_idx = rev.make_index(tr, close)
    perf_start = sessions[sessions >= pd.Timestamp(spec["perf_start"])][0]
    tr_perf = tr.copy()
    tr_perf.loc[tr_perf.index <= perf_start] = np.nan
    started = close.notna().mul(np.asarray(close.index >= perf_start), axis=0).cumsum() > 0
    perf_idx = (1 + tr_perf.fillna(0.0)).cumprod().where(started)
    qtr = qqq_tr.copy()
    qtr[qtr.index <= perf_start] = np.nan
    qqq_perf_idx = (1 + qtr.fillna(0.0)).cumprod()
    cs.assert_window(tr_perf.dropna(how="all").index, perf_start.strftime("%Y-%m-%d"), end, "performance returns")
    for label, fr in (("tr", tr), ("close", close), ("volume", vol), ("qqq", qqq_close)):
        cs.assert_window(fr.index, ps, end, label)
    guard["perf_start_session"] = str(perf_start.date())
    guard["max_session_loaded"] = str(sessions.max().date())
    guard["assertion"] = (f"PASS [{name}]: every price/return frame lies in [{ps}, {end}]; performance returns are "
                          f"masked up to {perf_start.date()} (first return used: the session after it)")
    return WinData(name=name, spec=spec, sessions=sessions, universe=uni, sig_idx=sig_idx, perf_idx=perf_idx,
                   close=close.ffill(), close_adj=close_adj, vol_adj=vol_adj, last_row=last_row, qqq_close=qqq_close,
                   qqq_perf_idx=qqq_perf_idx, terminal_events=events, guard=guard)
