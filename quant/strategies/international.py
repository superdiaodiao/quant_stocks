"""International replication and country rotation (docs/research_ledger_international.md, section 0).

Index-level rules on one market's price index ``p`` (local currency, no dividends); each returns the target
exposure to the index decided at the close of t (0..1, the rest in cash), NaN before it can be computed:

- I1  200-day SMA with a 2% band (the frozen US rule ``T01_L200_b2``): on above 1.02 x SMA, off below 0.98 x SMA,
      otherwise the previous state; checked every session.
- I2  12-month absolute momentum (Antonacci): at each month end, 252-session price return > the hurdle (0 on the
      price basis; the trailing T-bill return in the cash-earns-T-bill sensitivity) -> 1, else 0.
- I3  vol target 20% (voltarget V1): at each month end e = min(1, 20% / 20-session realised vol).

``simulate_exposure`` is the engine for those: trade at the close of t+1 to the target decided at t, a fixed cost
per unit of index value traded, the index part drifts between trades.

Rotation rules on the USD ETFs (total-return closes) decided at each month end, executed with
``quant.backtest.target_weights.simulate_target_weights`` (IBKR Tiered + half-spread):

- ``rotation_weights``: rank the universe by 252-session total return, hold the top ``n`` with 1/n each; a pick
  whose return is not above the trailing 252-session T-bill return goes to the T-bill ETF instead.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from quant.signals.technical import month_end_only, realised_vol, trailing_compound, trailing_return, trend_state


@dataclass(frozen=True)
class Params:
    sma_len: int = 200
    sma_band: float = 0.02
    mom_len: int = 252
    vol_target: float = 0.20
    vol_window: int = 20
    switch_cost: float = 0.0010          # per unit of index value traded (one way): 10 bp


INDEX_RULES = ("I1", "I2", "I3")


def index_target(rule: str, p: pd.Series, prm: Params, hurdle: pd.Series | None = None) -> pd.Series:
    """Target exposure decided at each close (NaN before defined). ``hurdle``: I2's 12-month threshold (a return
    series on p's index; None = 0)."""
    if rule == "I1":
        return trend_state(p, prm.sma_len, prm.sma_band)
    if rule == "I2":
        mom = trailing_return(p, prm.mom_len)
        h = pd.Series(0.0, index=p.index) if hurdle is None else hurdle.reindex(p.index)
        st = (mom > h).astype(float).where(mom.notna() & h.notna())
        return month_end_only(st)
    if rule == "I3":
        vol = realised_vol(p.pct_change(fill_method=None), prm.vol_window)
        e = np.minimum(1.0, prm.vol_target / vol).where(vol.notna() & (vol > 0))
        return month_end_only(e)
    raise ValueError(rule)


def simulate_exposure(e: pd.Series, r: pd.Series, cash: pd.Series | float, start_i: int, cost: float,
                      lag: int = 1) -> dict:
    """Value path that starts as 1.0 cash at the close of ``start_i`` and trades there to the target decided
    ``lag`` sessions earlier; afterwards it trades at the close of i whenever the target decided at i-lag differs
    from the last executed one. Between trades the index part and the cash part grow with their own returns.
    Cost = ``cost`` x |change in index value| (the entry buy included). Returns value, ret (from start_i+1),
    held (index weight earning each day's return), switches (trades after the entry), turnover."""
    E = e.values
    R = r.values
    C = cash.reindex(r.index).values if isinstance(cash, pd.Series) else np.full(len(r), float(cash))
    a, c = 0.0, 1.0
    last = None
    vals, held, switches, turnover = [], [], 0, 0.0
    for i in range(start_i, len(r)):
        if i > start_i:
            if a > 0 and np.isnan(R[i]):
                raise ValueError(f"missing index return on {r.index[i].date()}")
            a *= 1 + (0.0 if np.isnan(R[i]) else R[i])
            c *= 1 + C[i]
        tot = a + c
        tgt = E[i - lag]
        if np.isnan(tgt):
            raise ValueError(f"no target on {r.index[i - lag].date()}")
        if last is None or tgt != last:
            new_a = tgt * tot
            fee = cost * abs(new_a - a)
            turnover += abs(new_a - a) / tot
            tot -= fee
            a, c = tgt * tot, (1 - tgt) * tot
            if last is not None:
                switches += 1
            last = tgt
        vals.append(a + c)
        held.append(a / (a + c))
    idx = r.index[start_i:]
    v = pd.Series(vals, index=idx)
    h = pd.Series(held, index=idx).shift(1).iloc[1:]
    return {"value": v, "ret": v.pct_change().iloc[1:], "held": h, "switches": switches, "turnover": turnover}


def first_entry(targets: list[pd.Series], monthly: bool = True, not_before: str | None = None) -> int:
    """First session S (on or after ``not_before``) where every target decided at S-1 exists and (``monthly``)
    S-1 is a month end."""
    idx = targets[0].index
    ok = np.logical_and.reduce([t.notna().values for t in targets])
    per = idx.to_period("M")
    me = np.zeros(len(idx), bool)
    me[:-1] = per[:-1] != per[1:]
    lo = pd.Timestamp(not_before) if not_before is not None else idx[0]
    for i in range(1, len(idx)):
        if idx[i] >= lo and ok[i - 1] and (me[i - 1] or not monthly):
            return i
    raise ValueError("no entry possible")


def rotation_weights(adj: pd.DataFrame, rf: pd.Series, universe: tuple, top_n: int, mom_len: int,
                     cash: str = "CASH") -> pd.DataFrame:
    """Month-end relative + absolute momentum weights over ``universe`` (+ ``cash``), NaN until every member has
    a 252-session return. Ties keep the universe order."""
    mom = pd.DataFrame({u: trailing_return(adj[u], mom_len) for u in universe})
    hurdle = trailing_compound(rf, mom_len).reindex(adj.index)
    cols = list(universe) + [cash]
    w = pd.DataFrame(np.nan, index=adj.index, columns=cols)
    ok = mom.notna().all(axis=1) & hurdle.notna()
    M, H = mom.values, hurdle.values
    out = np.full((len(adj), len(cols)), np.nan)
    for i in np.where(ok.values)[0]:
        order = sorted(range(len(universe)), key=lambda k: (-M[i, k], k))[:top_n]
        row = np.zeros(len(cols))
        for k in order:
            if M[i, k] > H[i]:
                row[k] += 1.0 / top_n
            else:
                row[-1] += 1.0 / top_n
        out[i] = row
    w.loc[:, :] = out
    return w.apply(month_end_only)


def decade_table(r: pd.Series, b: pd.Series, min_sessions: int = 480) -> list[dict]:
    """Per calendar decade (only decades with at least ``min_sessions`` returns): calendar-time CAGR and max
    drawdown of ``r`` and the benchmark ``b`` (same index)."""
    out = []
    for dec, x in r.groupby((r.index.year // 10) * 10):
        if len(x) < min_sessions:
            continue
        y = b.loc[x.index]
        yrs = (x.index[-1] - x.index[0]).days / 365.25 + 1 / 252
        g = lambda s: float(np.prod(1 + s.values) ** (1 / yrs) - 1)
        def dd(s):
            v = np.concatenate([[1.0], np.cumprod(1 + s.values)])
            return float((v / np.maximum.accumulate(v) - 1).min())
        out.append({"decade": f"{dec}s", "start": str(x.index[0].date()), "end": str(x.index[-1].date()),
                    "cagr": g(x), "bench_cagr": g(y), "cagr_minus_bench": g(x) - g(y),
                    "max_dd": dd(x), "bench_max_dd": dd(y)})
    return out
