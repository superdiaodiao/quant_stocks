"""Bitcoin rules (pre-registered in docs/research_ledger_bitcoin.md section 0) and their engine.

Two grids:

- **stand-alone BTC rules** (BT1, BT2, BT3) run on the 7-day UTC calendar with two legs, BTC and CASH (T-bill rate
  on US session days, 0 otherwise: ``quant.data.bitcoin.calendar_rf``);
- **portfolio sleeves** (BT4, BT5) run on US sessions with QQQ, BTC (the UTC-day-S close, ``quant.data.bitcoin
  .on_sessions``) and CASH.

Execution (both grids): the target decided at the close of row t (data up to t) is traded at the close of row t+1
(``lag`` = 1), so the new weights earn from row t+2. Holdings drift between trades. A trade happens when the target
differs from the last executed one, on a rebalance flag, or (``band``) when a held weight at the decision row is more
than ``band`` away from its target. Orders of $1 or less are skipped. Costs are per-leg functions of
(value, price, sell, row date) passed in by the caller; a fixed monthly account fee can be deducted.

Everything here is pure: no files, no network. Parameters come from the study.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from quant.data.calendar import month_end_mask
from quant.signals.technical import month_end_momentum_state, realised_vol_ann, sma_cross_state, sma_state


@dataclass(frozen=True)
class Params:
    sma_long: int = 200          # calendar days
    sma_fast: int = 50
    mom_short_months: int = 3
    mom_long_months: int = 12
    vol_target: float = 0.40
    vol_window: int = 30         # calendar days
    vol_periods: float = 365.0
    vol_band: float = 0.10       # BT3: re-trade only when the target moved > 10 points from the last executed one
    sleeve_band_abs: float = 0.05    # BT4d: Swedroe 5/25 band = min(5 points, 25% of the target)
    sleeve_band_rel: float = 0.25


@dataclass(frozen=True)
class Spec:
    name: str
    family: str      # "standalone" | "sleeve"
    description: str
    sleeve: float = 0.0


SPECS = (
    Spec("BT1a", "standalone", "BTC when close > 200-day SMA (calendar days), else cash; daily, trade on change"),
    Spec("BT1b", "standalone", "BTC when 50-day SMA > 200-day SMA, else cash; daily, trade on change"),
    Spec("BT2a", "standalone", "BTC when the 3-month return at month end > 0, else cash; monthly"),
    Spec("BT2b", "standalone", "BTC when the 12-month return at month end > 0, else cash; monthly"),
    Spec("BT3", "standalone", "BTC weight = min(1, 40% / 30-day vol), rest cash; daily, re-trade if target moved > 10 pts"),
    Spec("BT4a", "sleeve", "95% QQQ + 5% BTC, rebalanced at each month end", 0.05),
    Spec("BT4b", "sleeve", "90% QQQ + 10% BTC, rebalanced at each month end", 0.10),
    Spec("BT4c", "sleeve", "80% QQQ + 20% BTC, rebalanced at each month end", 0.20),
    Spec("BT4d", "sleeve", "90% QQQ + 10% BTC, rebalanced when BTC weight leaves 7.5%-12.5% (5/25 band)", 0.10),
    Spec("BT5a", "sleeve", "BT4a with the BT1a trend filter on the BTC part (off: that part in cash)", 0.05),
    Spec("BT5b", "sleeve", "BT4b with the BT1a trend filter on the BTC part", 0.10),
    Spec("BT5c", "sleeve", "BT4c with the BT1a trend filter on the BTC part", 0.20),
)
SPEC = {s.name: s for s in SPECS}
DIAGNOSTICS = ("BT6",)          # weekend vs weekday returns: a test, counted as a trial, not a strategy


# ======================================================================== signals -> targets

def btc_state(name: str, p: pd.Series, P: Params) -> pd.Series:
    """BTC exposure (0..1) of a stand-alone rule decided at each calendar row (NaN before it can be computed)."""
    if name == "BT1a":
        return sma_state(p, P.sma_long)
    if name == "BT1b":
        return sma_cross_state(p, P.sma_fast, P.sma_long)
    if name == "BT2a":
        return month_end_momentum_state(p, P.mom_short_months)
    if name == "BT2b":
        return month_end_momentum_state(p, P.mom_long_months)
    if name == "BT3":
        vol = realised_vol_ann(p.pct_change(fill_method=None), P.vol_window, P.vol_periods)
        raw = np.minimum(1.0, P.vol_target / vol)
        return banded(raw, P.vol_band)
    raise KeyError(name)


def banded(target: pd.Series, band: float) -> pd.Series:
    """Keep the last accepted target until the new one differs from it by more than ``band`` (absolute)."""
    out = np.full(len(target), np.nan)
    last = np.nan
    for i, x in enumerate(target.values):
        if np.isnan(x):
            continue
        if np.isnan(last) or abs(x - last) > band:
            last = x
        out[i] = last
    return pd.Series(out, index=target.index)


def standalone_targets(name: str, p: pd.Series, P: Params) -> tuple[pd.DataFrame, pd.Series]:
    e = btc_state(name, p, P)
    w = pd.DataFrame({"BTC": e, "CASH": 1 - e}, index=p.index)
    return w, pd.Series(False, index=p.index)


def sleeve_targets(spec: Spec, sessions: pd.DatetimeIndex, trend_on_sessions: pd.Series | None = None
                   ) -> tuple[pd.DataFrame, pd.Series]:
    """QQQ / BTC / CASH targets on US sessions and the month-end rebalance flags (BT4d: no flags, band only).
    ``trend_on_sessions``: the BT1a state on each session (BT5)."""
    s = spec.sleeve
    on = pd.Series(1.0, index=sessions) if trend_on_sessions is None else trend_on_sessions.reindex(sessions)
    w = pd.DataFrame({"QQQ": 1 - s, "BTC": s * on, "CASH": s * (1 - on)}, index=sessions)
    flag = pd.Series(month_end_mask(sessions), index=sessions)
    if spec.name == "BT4d":
        flag[:] = False
    return w, flag


def band_width(P: Params, sleeve: float) -> float:
    return min(P.sleeve_band_abs, P.sleeve_band_rel * sleeve)


# ======================================================================== engine

def simulate(w: pd.DataFrame, flag: pd.Series, rets: pd.DataFrame, prices: pd.DataFrame, start_i: int,
             cost_fns: dict, *, start_value: float = 10_000.0, lag: int = 1, band: dict | None = None,
             monthly_fee: float = 0.0, costs: bool = True) -> dict:
    """Dollar simulation from ``start_value`` in cash at the close of row ``start_i`` (traded to the target decided
    at ``start_i - lag``). ``cost_fns``: {leg: f(value, price, sell, date) -> dollars}; ``band``: {leg: width}.
    ``monthly_fee``: dollars deducted on the first row of every new calendar month."""
    cols = list(w.columns)
    W = w[cols].values
    F = flag.reindex(w.index).fillna(False).values
    R = rets.reindex(w.index)[cols].values
    PX = prices.reindex(w.index).reindex(columns=cols).values
    idx = w.index
    h = np.zeros(len(cols))
    cash0 = start_value
    last = None
    held_hist = {}
    values, held, trades, orders, cost_total, fees = [], [], 0, 0, 0.0, 0.0
    for i in range(start_i, len(w)):
        if i > start_i:
            r = R[i]
            if np.any(np.isnan(r[h > 0])):
                raise ValueError(f"missing return for a held leg on {idx[i].date()}")
            h = h * (1 + np.nan_to_num(r))
            if monthly_fee and idx[i].month != idx[i - 1].month:
                tot = h.sum()
                h = h * (1 - monthly_fee / tot)
                fees += monthly_fee
        total = h.sum() + cash0
        tgt = W[i - lag]
        if np.any(np.isnan(tgt)):
            raise ValueError(f"no target on {idx[i - lag].date()}")
        out_of_band = False
        if band and last is not None and (i - lag) in held_hist:
            hw = held_hist[i - lag]
            out_of_band = any(abs(hw[cols.index(k)] - tgt[cols.index(k)]) > b for k, b in band.items())
        if last is None or F[i - lag] or out_of_band or not np.allclose(tgt, last, atol=1e-12, rtol=0):
            new = tgt * total
            delta = new - h
            c = 0.0
            traded = False
            for k in range(len(cols)):
                if abs(delta[k]) > 1.0 or (last is None and new[k] > 0):
                    if costs and cols[k] in cost_fns:
                        c += cost_fns[cols[k]](abs(delta[k]), PX[i, k], delta[k] < 0, idx[i])
                    orders += 1
                    traded = True
            if traded or last is None:
                if last is not None:
                    trades += 1
                h = new * (1 - c / total)
                cash0 = 0.0
                cost_total += c
            last = tgt.copy()
        v = h.sum() + cash0
        values.append(v)
        held_hist[i] = h / v if v > 0 else h
        held.append(held_hist[i])
    vser = pd.Series(values, index=idx[start_i:])
    hw = pd.DataFrame(held, index=idx[start_i:], columns=cols).shift(1).iloc[1:]
    return {"value": vser, "ret": vser.pct_change().iloc[1:], "held": hw, "trades": trades, "orders": orders,
            "cost_dollars": cost_total, "fee_dollars": fees}


def buy_hold(leg: str, rets: pd.DataFrame, prices: pd.DataFrame, start_i: int, cost_fns: dict, **kw) -> dict:
    w = pd.DataFrame({leg: 1.0}, index=rets.index)
    return simulate(w, pd.Series(False, index=rets.index), rets, prices, start_i, cost_fns, **kw)


# ======================================================================== grids and diagnostics

def to_sessions(r_cal: pd.Series, sessions: pd.DatetimeIndex) -> pd.Series:
    """Compound 7-day-calendar returns into US-session returns: session S gets every calendar day in
    (previous session, S]. Only sessions after the first calendar return and up to the last are returned."""
    s = sessions[(sessions > r_cal.index[0] - pd.Timedelta(days=1)) & (sessions <= r_cal.index[-1])]
    grp = s.searchsorted(r_cal.index, side="left")
    ok = grp < len(s)
    g = pd.Series(np.log1p(r_cal.values[ok]), index=grp[ok]).groupby(level=0).sum()
    out = pd.Series(np.expm1(g.values), index=s[g.index])
    # drop the first session if it did not get a full (previous session, S] window
    if len(out) and out.index[0] == s[0]:
        prev = sessions[sessions < s[0]]
        if len(prev) and r_cal.index[0] > prev[-1] + pd.Timedelta(days=1):
            out = out.iloc[1:]
    return out


def risk_contribution(r_leg: pd.Series, r_port: pd.Series, w_leg: pd.Series) -> float:
    """Euler share of portfolio variance from one leg: mean held weight x cov(leg, portfolio) / var(portfolio)."""
    a = pd.concat([r_leg, r_port, w_leg], axis=1, join="inner").dropna()
    c = np.cov(a.iloc[:, 0], a.iloc[:, 1], ddof=1)
    return float(a.iloc[:, 2].mean() * c[0, 1] / c[1, 1])


def weekend_test(r_cal: pd.Series) -> dict:
    """BT6: mean UTC-day return on Saturday / Sunday vs Monday-Friday, Welch t, and the weekend share of the total
    log return (diagnostic only)."""
    wk = r_cal.index.dayofweek >= 5
    a, b = r_cal[wk].dropna(), r_cal[~wk].dropna()
    se = math.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b))
    lg = np.log1p(r_cal.dropna())
    lw = lg[lg.index.dayofweek >= 5].sum()
    return {"weekend_days": int(len(a)), "weekday_days": int(len(b)),
            "mean_weekend": float(a.mean()), "mean_weekday": float(b.mean()),
            "sd_weekend": float(a.std()), "sd_weekday": float(b.std()),
            "welch_t_weekend_minus_weekday": float((a.mean() - b.mean()) / se),
            "weekend_share_of_log_return": float(lw / lg.sum()) if lg.sum() != 0 else float("nan"),
            "weekend_share_of_days": float(len(a) / (len(a) + len(b)))}


def halving_table(p: pd.Series, halvings: dict, horizons=(-365, 180, 365, 540)) -> list[dict]:
    """Price change from each halving day's close to the close ``h`` days later (negative: before), and the max
    drawdown in the 540 days after; None where the data do not reach. Descriptive only."""
    rows = []
    for day, height in halvings.items():
        d = pd.Timestamp(day)
        if d not in p.index:
            rows.append({"halving": day, "block": height, "note": "before the data"})
            continue
        row = {"halving": day, "block": height, "close": None}
        for h in horizons:
            e = d + pd.Timedelta(days=h)
            ok = e in p.index
            row[f"ret_{'m' if h < 0 else 'p'}{abs(h)}d"] = (float(p[e] / p[d] - 1) if h > 0 else float(p[d] / p[e] - 1)) \
                if ok else None
        after = p.loc[d:d + pd.Timedelta(days=540)]
        row["max_dd_next_540d"] = float((after / after.cummax() - 1).min())
        row["days_of_data_after"] = int((p.index[-1] - d).days)
        del row["close"]
        rows.append(row)
    return rows


def rolling_corr(a: pd.Series, b: pd.Series, window: int) -> pd.Series:
    x = pd.concat([a, b], axis=1, join="inner").dropna()
    return x.iloc[:, 0].rolling(window, min_periods=window).corr(x.iloc[:, 1])
