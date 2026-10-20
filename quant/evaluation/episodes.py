"""State episodes of a switching strategy: how often each state (long / flat / short) was entered, how long it was
held and how many of those holdings ("round trips") lost money - the whipsaw count of docs/research_ledger_sma200_short.md.

Pure functions on a value path and the state executed at each close (the target held into the next session), as
returned by quant.backtest.long_short. An episode starts at the close where a state is entered (value after that
trade's costs) and ends at the close where the next state is entered (value after the exit trade's costs, so every
switch's cost is charged to the episode it ends); the last episode ends at the last close.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def state_episodes(value: pd.Series, state: pd.Series, underlying: pd.Series | None = None) -> pd.DataFrame:
    """One row per episode: state, start / end dates, sessions, the strategy's return over the episode and (when the
    underlying's daily returns are given, same index as ``value``) the underlying's return over the same sessions."""
    st = state.reindex(value.index).values
    v = value.values
    starts = [0] + [i for i in range(1, len(st)) if st[i] != st[i - 1]]
    rows = []
    for k, a in enumerate(starts):
        z = starts[k + 1] if k + 1 < len(starts) else len(st) - 1
        if z <= a:
            continue
        row = {"state": float(st[a]), "start": str(value.index[a].date()), "end": str(value.index[z].date()),
               "sessions": int(z - a), "ret": float(v[z] / v[a] - 1), "complete": bool(k + 1 < len(starts))}
        if underlying is not None:
            u = underlying.reindex(value.index).values[a + 1: z + 1]
            row["underlying_ret"] = float(np.prod(1 + u) - 1)
        rows.append(row)
    return pd.DataFrame(rows, columns=["state", "start", "end", "sessions", "ret", "complete", "underlying_ret"]
                        if underlying is not None else ["state", "start", "end", "sessions", "ret", "complete"])


def episode_summary(ep: pd.DataFrame, side: str) -> dict:
    """Counts for the episodes of one side (``short``: state < 0, ``long``: state > 0, ``flat``: state == 0):
    number, losing ones (return < 0), share losing, median / worst / best return, the summed log return of the losing
    ones (the whipsaw cost), mean sessions held."""
    if side == "short":
        x = ep[ep["state"] < 0]
    elif side == "long":
        x = ep[ep["state"] > 0]
    else:
        x = ep[ep["state"] == 0]
    if not len(x):
        return {"episodes": 0}
    lose = x[x["ret"] < 0]
    return {"episodes": int(len(x)), "losing": int(len(lose)), "share_losing": float(len(lose) / len(x)),
            "median_ret": float(x["ret"].median()), "worst_ret": float(x["ret"].min()), "best_ret": float(x["ret"].max()),
            "sum_log_losing": float(np.log1p(lose["ret"]).sum()), "sum_log_all": float(np.log1p(x["ret"]).sum()),
            "mean_sessions": float(x["sessions"].mean())}


def share_in_state(state_held: pd.Series, side: str) -> float:
    """Share of sessions whose return was earned in ``side`` (short: state < 0, long: > 0, flat: == 0)."""
    s = state_held.dropna()
    m = (s < 0) if side == "short" else (s > 0) if side == "long" else (s == 0)
    return float(m.mean()) if len(s) else float("nan")
