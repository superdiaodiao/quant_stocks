"""Risk ladder (docs/research_ledger_risk_ladder.md): the portfolio target weights, the BTC leg added to the leverage
market, the mixed US / Hong Kong cost function, the forced-liquidation threshold, rolling-window outcomes and the
monthly-return helpers of studies/risk_ladder.py.

The engine is quant.strategies.leverage_methods.simulate_batch (margin loan at EFFR + spread, forced liquidation at
the day's low); this module only builds its inputs: a ``Market`` with one extra leg (``BTCX``), per-session target
weights (constant, switched once when BTC becomes available, or by the QQQ 200-day SMA state with a one-session
lag) and the rebalance sessions. Every study-specific number (fee, spreads, dates, SMA length / band, exposures) is an
argument.
"""
from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd

from quant.backtest.costs import ibkr_crypto_cost, ibkr_hk_etf_cost
from quant.data.calendar import month_end_mask
from quant.data.synthetic import NOMINAL_PRICE
from quant.evaluation.metrics import TRADING_DAYS
from quant.signals.technical import trend_state
from quant.strategies import leverage_methods as lm
from quant.strategies.sector_lev import exposure_weights as sector_exposure_weights

BTC = "BTCX"


# ======================================================================== market with a BTC leg

def with_leg(mk: lm.Market, name: str, r: np.ndarray, er_per_session: np.ndarray | float = 0.0,
             mult: float = 1.0, price: float = NOMINAL_PRICE) -> lm.Market:
    """``mk`` with one more leg ``name``: close-to-close returns ``r`` (NaN where the leg does not exist); the
    'to the low' return is the close return (used only for the margin check, see ``margin_threshold``), share price
    nominal, no embedded financing, ``er_per_session`` recorded as fund expense (accounting only)."""
    r = np.asarray(r, float)
    T = len(mk.sessions)
    if r.shape != (T,):
        raise ValueError("leg returns must have one value per session")
    col = lambda a, x: np.column_stack([a, np.broadcast_to(np.asarray(x, float), (T,))])
    return replace(mk, R=col(mk.R, r), RL=col(mk.RL, r), P=col(mk.P, price), fin=col(mk.fin, 0.0),
                   er=col(mk.er, er_per_session), legs=tuple(mk.legs) + (name,), mult=np.r_[mk.mult, mult])


def fund_returns(r_spot: pd.Series, sessions: pd.DatetimeIndex, fee: float, first_close: str) -> np.ndarray:
    """Session returns of a spot-tracking fund: spot return - ``fee`` / 252, NaN up to and including the session
    ``first_close`` (the leg can be bought at that close; its first return is the next session's)."""
    r = r_spot.reindex(sessions).astype(float) - fee / TRADING_DAYS
    r[sessions <= pd.Timestamp(first_close)] = np.nan
    if r[sessions > pd.Timestamp(first_close)].isna().any():
        raise ValueError("missing spot return after the first close")
    return r.values


def params_for(p: lm.Params, mk: lm.Market, maint: dict, half_spread: dict) -> lm.Params:
    """``p`` with ``maint`` / ``half_spread`` extended to the legs of ``mk`` (``maint`` / ``half_spread``: {leg: value}
    for the legs beyond ``p``'s, and overrides for any leg)."""
    m, h = list(p.maint), list(p.half_spread)
    for leg in mk.legs[len(m):]:
        m.append(np.nan)
        h.append(np.nan)
    for leg, v in maint.items():
        m[mk.legs.index(leg)] = v
    for leg, v in half_spread.items():
        h[mk.legs.index(leg)] = v
    if np.isnan(m).any() or np.isnan(h).any():
        raise ValueError("maintenance / half-spread missing for a leg")
    return replace(p, maint=np.array(m), half_spread=np.array(h))


# ======================================================================== target weights

def weight_row(mk: lm.Market, weights: dict) -> np.ndarray:
    w = np.zeros(len(mk.legs))
    for k, v in weights.items():
        w[mk.legs.index(k)] = v
    return w


def constant_targets(mk: lm.Market, weights: dict) -> np.ndarray:
    return np.tile(weight_row(mk, weights), (len(mk.sessions), 1))


def switched_targets(mk: lm.Market, before: dict, after: dict, switch_close: str) -> tuple[np.ndarray, np.ndarray]:
    """``before`` up to the session before ``switch_close``, ``after`` from the ``switch_close`` session on (traded at
    that close); the flags mark ``switch_close``."""
    i = lm.session_index(mk.sessions, switch_close)
    W = constant_targets(mk, before)
    W[i:] = weight_row(mk, after)
    flags = np.zeros(len(mk.sessions), bool)
    flags[i] = True
    return W, flags


def exposure_legs(e: float, cash_leg: str = "TBILL") -> dict:
    """QQQ + 2x leg for e >= 1, QQQ + T-bill ETF below (quant.strategies.sector_lev.exposure_weights, its CASH leg
    named ``cash_leg``)."""
    w = sector_exposure_weights(e)
    return {"QQQ": w["QQQ"], "QLDX": w["QLDX"], cash_leg: w["CASH"]}


def sma_targets(mk: lm.Market, p_signal: pd.Series, on: dict, off: dict, length: int, band: float,
                lag: int = 1) -> tuple[np.ndarray, np.ndarray, pd.Series]:
    """Targets in force at each close and the rebalance sessions of an SMA on / off rule: the state decided at the
    close of t (``trend_state`` of ``p_signal``) sets the target traded at the close of t + ``lag``; a trade also
    happens ``lag`` sessions after every month end and after every state change. Returns (targets, flags, state)."""
    s = mk.sessions
    st = trend_state(p_signal.reindex(s), length, band)
    dec = np.full((len(s), len(mk.legs)), np.nan)
    dec[(st == 1.0).values] = weight_row(mk, on)
    dec[(st == 0.0).values] = weight_row(mk, off)
    me = month_end_mask(s)
    change = (st.ne(st.shift(1)) & st.shift(1).notna() & st.notna()).values
    W = np.full_like(dec, np.nan)
    W[lag:] = dec[:-lag]
    flags = np.zeros(len(s), bool)
    flags[lag:] = (me | change)[:-lag]
    return W, flags, st


# ======================================================================== costs

def mixed_cost_fn(special: dict):
    """A ``simulate_batch`` cost function: ``leverage_methods.order_cost_vec`` for every leg, replaced on the leg
    positions in ``special`` ({position: f(value, sell, half_spread) -> dollars, vectorised}); orders of at most
    ``min_order`` dollars are not placed on any leg."""
    def cost(value, price, sell, half_spread, min_order):
        c = lm.order_cost_vec(value, price, sell, half_spread, min_order)
        for k, f in special.items():
            v = np.abs(value[:, k])
            c[:, k] = np.where(v > min_order, f(v, sell[:, k], half_spread[:, k]), 0.0)
        return c
    return cost


def hk_etf_cost(v, sell, hs):
    return ibkr_hk_etf_cost(v, hs)


def spot_crypto_cost(v, sell, hs):
    return np.array([ibkr_crypto_cost(float(x), float(h)) for x, h in zip(np.ravel(v), np.ravel(hs))]).reshape(
        np.shape(v))


# ======================================================================== forced liquidation

def margin_threshold(L: float, maint: float, btc: float = 0.0, btc_maint: float = 1.0) -> float:
    """Cumulative QQQ fall from the last rebalance close (BTC unchanged, no interest) at which the margin check fails:
    equity 1 - L d versus requirement m L (1 - d) + m_b b, i.e. d* = (1 - m_b b - m L) / (L (1 - m)); NaN without a
    loan (L + b <= 1)."""
    if L + btc <= 1.0 + 1e-12:
        return float("nan")
    return (1.0 - btc_maint * btc - maint * L) / (L * (1.0 - maint))


def month_end_to_low(r: np.ndarray, rl: np.ndarray, sessions: pd.DatetimeIndex) -> pd.Series:
    """For each month: the worst fall from the previous month-end close to any intraday low inside the month
    (close-to-close returns ``r``, previous close -> low returns ``rl``)."""
    me = month_end_mask(sessions)
    out, cum, label = {}, 1.0, None
    worst = 0.0
    for t in range(1, len(sessions)):
        if np.isnan(r[t]):
            continue
        low = cum * (1.0 + min(rl[t], r[t]))
        worst = min(worst, low - 1.0)
        cum *= 1.0 + r[t]
        label = sessions[t].strftime("%Y-%m")
        if me[t]:
            out[label] = worst
            cum, worst = 1.0, 0.0
    return pd.Series(out)


# ======================================================================== paths, windows, monthly returns

def window_outcomes(res: dict, sessions: pd.DatetimeIndex, n1y: int = 252) -> pd.DataFrame:
    """Per path of a ``simulate_batch`` result: CAGR (session count / 252), max drawdown, the worst ``n1y``-session
    return, liquidations, wipe-out."""
    rows = []
    for b in range(len(res["starts"])):
        v = lm.path_values(res, b, sessions).values
        yrs = (len(v) - 1) / TRADING_DAYS
        cagr = (v[-1] / v[0]) ** (1 / yrs) - 1 if v[-1] > 0 else -1.0
        dd = float((v / np.maximum.accumulate(v) - 1).min())
        with np.errstate(divide="ignore", invalid="ignore"):
            w1 = v[n1y:] / v[:-n1y] - 1
        w1 = np.where(v[:-n1y] > 0, w1, -1.0)
        rows.append({"start": str(sessions[res["starts"][b]].date()), "cagr": float(cagr), "max_dd": dd,
                     "worst_1y": float(np.nanmin(w1)), "liquidations": int(res["liq"][b]),
                     "wiped_out": bool(res["dead"][b])})
    return pd.DataFrame(rows)


def monthly_returns(v: pd.Series) -> pd.Series:
    """Month-end to month-end returns of a value path (the entry month, partial unless the entry is a month end,
    is the base and gets no return); index 'YYYY-MM'."""
    me = v.groupby(v.index.to_period("M")).last()
    r = me.pct_change().iloc[1:]
    r.index = r.index.astype(str)
    return r


def drag_row(r_m: pd.Series) -> dict:
    """Arithmetic (12 x mean monthly) vs geometric (compounded) annual return, their gap and the sigma^2 / 2 rule."""
    mu = float(r_m.mean() * 12)
    vol = float(r_m.std() * np.sqrt(12))
    g = float(np.prod(1 + r_m.values) ** (12 / len(r_m)) - 1)
    return {"arith_annual": mu, "geom_annual": g, "gap": mu - g, "vol_annual": vol, "half_var": vol ** 2 / 2,
            "months": int(len(r_m))}
