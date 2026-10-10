"""Ways to hold leveraged Nasdaq exposure (margin loan, 2x / 3x ETF mixes, synthetic futures): the market arrays,
the vectorised multi-path engine with forced liquidation, the path metrics, the rolling windows, the Kelly estimate
and the leg / futures validations of studies/leverage_methods.py.

Extracted unchanged from studies/leverage_methods.py in the slimming pass. The leg universe (``LEGS`` and their
exposure multiples ``MULT``) is fixed here; every frozen assumption (margin spread and day count, 2x spread, fund
expenses, maintenance margins, half-spreads, futures basis / margin / costs / roll, liquidation slippage and cushion,
minimum order, rebalance band) is a field of ``Params``, given by the study. The EFFR loader is quant.data.effr;
``drawdown_info`` / ``worst_window`` are quant.evaluation.equity.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from quant.backtest.costs import (
    CASH_ETF_FEE, COMMISSION_MAX_FRAC, COMMISSION_MIN, COMMISSION_PER_SHARE, FEES_PER_SHARE, LEV_ER, NOMINAL_PRICE,
    SELL_REG_FRAC, START_EQUITY,
)
from quant.data import sector_etfs as etfs
from quant.data.calendar import month_end_mask
from quant.data.guards import truncate_dev
from quant.evaluation.criteria import ab_verdict
from quant.evaluation.equity import drawdown_info, period_metrics, worst_window
from quant.evaluation.metrics import TRADING_DAYS, cagr_of, monthly

# legs universe
LEGS = ("QQQ", "ONEQ", "QLDX", "TQQQX", "TBILL", "BENCH")
MULT = np.array([1.0, 1.0, 2.0, 3.0, 0.0, 1.0])
LI = {k: i for i, k in enumerate(LEGS)}


@dataclass(frozen=True, eq=False)
class Params:
    """The frozen assumptions of the leverage ledger (section 0); per-leg arrays are in ``LEGS`` order."""
    ibkr_spread: float            # margin loan rate = EFFR + this
    daycount: float               # margin interest day count (actual / daycount)
    spread_2x: float              # financing spread of the synthetic 2x (per borrowed unit also in 3x)
    qqq_er: float
    oneq_er: float
    maint: np.ndarray             # maintenance margin per leg (share of position)
    half_spread: np.ndarray       # half-spread per leg
    fut_basis: float              # futures financing above T-bill
    fut_maint: float              # futures maintenance margin (share of notional)
    fut_init: float               # futures initial margin (cash set aside, share of notional)
    fut_trade_cost: float         # of traded notional
    fut_roll_cost: float          # of notional, per quarterly roll
    roll_offset: int              # roll this many sessions before expiry
    liq_slip: float               # slippage of forced sales
    liq_cushion: float            # forced sales restore equity to this multiple of the requirement
    min_order: float              # orders of at most this many dollars are not placed
    band: float                   # "band" policy: rebalance when |exposure / L - 1| > band


# ======================================================================== data

def chart_ohlc(path: Path, end: str) -> pd.DataFrame:
    """date, close, adjclose, low (low adjusted by adjclose / close), truncated at ``end``."""
    j = json.loads(Path(path).read_text())["chart"]["result"][0]
    ts = pd.to_datetime(j["timestamp"], unit="s", utc=True).tz_convert("America/New_York")
    q = j["indicators"]["quote"][0]
    adj = j["indicators"].get("adjclose", [{}])[0].get("adjclose") or q["close"]
    df = pd.DataFrame({"date": ts.strftime("%Y-%m-%d"), "close": q["close"], "adjclose": adj, "low": q["low"]})
    df = truncate_dev(df.dropna(subset=["close"]), "date", end)
    df["low_adj"] = df["low"] * df["adjclose"] / df["close"]
    return df.reset_index(drop=True)


def synthetic_kx(r1: pd.Series, rf: pd.Series, k: float, spread: float, er: float) -> pd.Series:
    """Daily-reset k-times fund: k r - (k - 1)(rf + spread / 252) - er / 252."""
    return k * r1 - (k - 1.0) * (rf + spread / TRADING_DAYS) - er / TRADING_DAYS


def third_fridays(start: str, end: str) -> list[pd.Timestamp]:
    out = []
    for y in range(pd.Timestamp(start).year, pd.Timestamp(end).year + 2):
        for m in (3, 6, 9, 12):
            d = pd.Timestamp(year=y, month=m, day=1)
            d = d + pd.Timedelta(days=(4 - d.weekday()) % 7 + 14)
            out.append(d)
    return out


def expiry_sessions(sessions: pd.DatetimeIndex) -> np.ndarray:
    """Session index of each quarterly expiry (the third Friday, or the session before it when closed)."""
    out = []
    for d in third_fridays(str(sessions[0].date()), str(sessions[-1].date())):
        i = sessions.searchsorted(d, side="right") - 1
        if 0 <= i < len(sessions) and d <= sessions[-1]:
            out.append(i)
    return np.array(sorted(set(out)), dtype=int)


def roll_flags(sessions: pd.DatetimeIndex, offset: int) -> np.ndarray:
    f = np.zeros(len(sessions), dtype=bool)
    for i in expiry_sessions(sessions):
        if i - offset >= 0:
            f[i - offset] = True
    return f


@dataclass
class Market:
    sessions: pd.DatetimeIndex
    R: np.ndarray            # (T, K) close-to-close returns (NaN where a leg does not exist)
    RL: np.ndarray           # (T, K) previous close -> today's low
    P: np.ndarray            # (T, K) share price for order sizes
    fin: np.ndarray          # (T, K) estimated embedded financing per unit held (leveraged ETFs)
    er: np.ndarray           # (T, K) fund expense per unit held
    r_tr: np.ndarray         # (T,) Nasdaq-100 total return (QQQ + its expense)
    rl_tr: np.ndarray        # (T,) to the low
    rf: np.ndarray           # (T,) T-bill per session
    borrow: np.ndarray       # (T,) margin interest per unit borrowed, previous session -> t
    month_end: np.ndarray
    info: dict = field(default_factory=dict)

    def futures(self, basis: float) -> tuple[np.ndarray, np.ndarray]:
        f = self.r_tr - (self.rf + basis / TRADING_DAYS)
        fl = np.minimum(self.rl_tr - (self.rf + basis / TRADING_DAYS), f)
        return f, fl


def build_market(d: etfs.Data, effr: pd.Series, tqqq: pd.DataFrame, lows: dict, p: Params) -> Market:
    s = d.sessions
    rf = d.rf.reindex(s)
    r_q = d.rets["QQQ"]
    # 3x leg: synthetic before TQQQ's first daily return
    t_adj = pd.Series(tqqq["adjclose"].values, index=pd.DatetimeIndex(tqqq["date"]))
    extra = t_adj.index.difference(s)
    if len(extra):
        raise ValueError(f"TQQQ sessions not in the calendar: {list(extra[:5])}")
    t_r = t_adj.reindex(s).pct_change(fill_method=None)
    ft = t_r.first_valid_index()
    syn3 = synthetic_kx(r_q, rf, 3.0, p.spread_2x, LEV_ER)
    tqqqx = syn3.where(s < ft, t_r)
    rets = pd.DataFrame({"QQQ": r_q, "ONEQ": d.rets["ONEQ"], "QLDX": d.rets["QLDX"], "TQQQX": tqqqx,
                         "TBILL": d.rets["CASH"], "BENCH": d.rets["BENCH"]}, index=s)
    # intraday lows
    rl = rets.copy()
    for t in ("QQQ", "ONEQ"):
        lo = lows[t].set_index(pd.DatetimeIndex(lows[t]["date"]))
        prev = lo["adjclose"].reindex(s).shift(1)
        x = lo["low_adj"].reindex(s) / prev - 1
        rl[t] = np.minimum(x.fillna(rets[t]), rets[t])
    rl["QLDX"] = np.minimum(2 * rl["QQQ"], rets["QLDX"])
    rl["TQQQX"] = np.minimum(3 * rl["QQQ"], rets["TQQQX"])
    px = pd.DataFrame({"QQQ": d.close["QQQ"], "ONEQ": d.close["ONEQ"], "QLDX": NOMINAL_PRICE, "TQQQX": NOMINAL_PRICE,
                       "TBILL": NOMINAL_PRICE, "BENCH": d.close["ONEQ"]}, index=s).fillna(NOMINAL_PRICE)
    lend = (rf + p.spread_2x / TRADING_DAYS).values
    T = len(s)
    fin = np.zeros((T, len(LEGS)))
    fin[:, LI["QLDX"]] = lend
    fin[:, LI["TQQQX"]] = 2 * lend
    er = np.zeros((T, len(LEGS)))
    er[:, LI["QQQ"]] = p.qqq_er / TRADING_DAYS
    er[:, LI["ONEQ"]] = p.oneq_er / TRADING_DAYS
    er[:, LI["BENCH"]] = p.oneq_er / TRADING_DAYS
    er[:, LI["QLDX"]] = LEV_ER / TRADING_DAYS
    er[:, LI["TQQQX"]] = LEV_ER / TRADING_DAYS
    er[:, LI["TBILL"]] = CASH_ETF_FEE / TRADING_DAYS
    # margin interest for the period previous session -> t, at the previous session's EFFR + spread, actual/360
    e = effr.reindex(effr.index.union(s)).ffill().reindex(s)
    days = np.r_[1, np.diff(s.values).astype("timedelta64[D]").astype(int)]
    borrow = ((e.shift(1).bfill() + p.ibkr_spread) * days / p.daycount).values
    r_tr = (r_q + p.qqq_er / TRADING_DAYS).values
    rl_tr = (rl["QQQ"] + p.qqq_er / TRADING_DAYS).values
    info = {"tqqq_first_return": str(ft.date()), "effr_first": str(effr.index[0].date()),
            "effr_last": str(effr.index[-1].date())}
    return Market(sessions=s, R=rets[list(LEGS)].values, RL=rl[list(LEGS)].values, P=px[list(LEGS)].values,
                  fin=fin, er=er, r_tr=r_tr, rl_tr=rl_tr, rf=rf.values, borrow=borrow,
                  month_end=month_end_mask(s), info=info)


# ======================================================================== methods

def method_weights(name: str, L: float, fut_init: float) -> tuple[np.ndarray, float]:
    """Target weights per leg (share of equity; cash = 1 - sum, negative = loan) and futures notional / equity."""
    w = np.zeros(len(LEGS))
    n = 0.0
    if name == "M":
        w[LI["QQQ"]] = L
    elif name == "M1":
        w[LI["ONEQ"]] = L
    elif name == "Q2":
        w[LI["QQQ"]], w[LI["QLDX"]] = 2.0 - L, L - 1.0
    elif name == "Q3":
        w[LI["QQQ"]], w[LI["TQQQX"]] = (3.0 - L) / 2.0, (L - 1.0) / 2.0
    elif name == "T3":
        w[LI["TQQQX"]], w[LI["TBILL"]] = L / 3.0, 1.0 - L / 3.0
    elif name == "F":
        n = L
        w[LI["TBILL"]] = 1.0 - fut_init * L
    elif name == "QQQ_BH":
        w[LI["QQQ"]] = 1.0
    elif name == "BENCH_BH":
        w[LI["BENCH"]] = 1.0
    else:
        raise KeyError(name)
    return w, n


def order_cost_vec(value: np.ndarray, price: np.ndarray, sell: np.ndarray, half_spread: np.ndarray,
                   min_order: float) -> np.ndarray:
    """Vectorised quant.backtest.costs.etf_order_cost (orders of <= ``min_order`` dollars are not placed)."""
    value = np.abs(value)
    shares = value / price
    comm = np.minimum(np.maximum(COMMISSION_MIN, COMMISSION_PER_SHARE * shares), COMMISSION_MAX_FRAC * value)
    c = comm + FEES_PER_SHARE * shares + half_spread * value + np.where(sell, SELL_REG_FRAC * value, 0.0)
    return np.where(value > min_order, c, 0.0)


# ======================================================================== engine (vectorised over start dates)

def simulate_batch(mk: Market, method: str, L: float, policy: str, starts: np.ndarray, ends: np.ndarray, p: Params,
                   liquidation: bool = True) -> dict:
    """$10,000 cash at the close of each start; returns the value paths (T_span, B) and per-path accounting.
    Margin, liquidation, futures and cost assumptions: ``p`` (futures basis ``p.fut_basis``, roll ``p.roll_offset``
    sessions before expiry)."""
    basis, roll_offset = p.fut_basis, p.roll_offset
    starts, ends = np.asarray(starts, int), np.asarray(ends, int)
    B, K = len(starts), len(LEGS)
    w, n = method_weights(method, L, p.fut_init)
    FR, FL = mk.futures(basis)
    roll = roll_flags(mk.sessions, roll_offset) if n else np.zeros(len(mk.sessions), bool)
    t0, t1 = int(starts.min()), int(ends.max())
    h = np.zeros((B, K))
    cash = np.full(B, START_EQUITY)
    N = np.zeros(B)
    dead = np.zeros(B, bool)
    liq = np.zeros(B, int)
    acc = {k: np.zeros(B) for k in ("interest", "fin", "er", "trade", "fut_fin", "liq_loss")}
    trades = np.zeros(B, int)
    V = np.full((t1 - t0 + 1, B), np.nan)
    for t in range(t0, t1 + 1):
        act = (t > starts) & (t <= ends) & ~dead
        entry = t == starts
        if act.any():
            E_prev = h.sum(1) + cash
            safe = np.where(E_prev > 0, E_prev, np.nan)
            neg = act & (cash < 0)
            it = np.where(neg, -cash * mk.borrow[t], 0.0)
            cash = cash - it
            acc["interest"] += np.nan_to_num(it / safe)
            acc["fin"] += np.where(act, np.nan_to_num((h * mk.fin[t]).sum(1) / safe), 0.0)
            acc["er"] += np.where(act, np.nan_to_num((h * mk.er[t]).sum(1) / safe), 0.0)
            acc["fut_fin"] += np.where(act, np.nan_to_num(N * (mk.rf[t] + basis / TRADING_DAYS) / safe), 0.0)
            r, rl = mk.R[t], mk.RL[t]
            bad = np.isnan(r) & (np.abs(h[act]) > 0).any(0)
            if bad.any():
                raise ValueError(f"missing return for a held leg on {mk.sessions[t].date()}: {np.array(LEGS)[bad]}")
            r0, rl0 = np.nan_to_num(r), np.nan_to_num(rl)
            hl = h * (1 + rl0)
            Nl = N * (1 + FL[t])
            cash_l = cash + N * FL[t]
            eq_l = hl.sum(1) + cash_l
            req_l = (np.abs(hl) * p.maint).sum(1) + np.abs(Nl) * p.fut_maint
            breach = act & (eq_l < req_l) & (req_l > 0) if liquidation else np.zeros(B, bool)
            normal = act & ~breach
            cash = np.where(normal, cash + N * FR[t], cash)
            h = np.where(normal[:, None], h * (1 + r0), h)
            N = np.where(normal, N * (1 + FR[t]), N)
            for i in np.flatnonzero(breach):
                liq[i] += 1
                gross = hl[i].sum() + abs(Nl[i])
                den = p.liq_cushion * req_l[i] - p.liq_slip * gross
                f = (eq_l[i] - p.liq_slip * gross) / den if den > 0 else 0.0
                f = min(max(f, 0.0), 1.0)
                after = eq_l[i] - p.liq_slip * (1 - f) * gross
                acc["liq_loss"][i] += p.liq_slip * (1 - f) * gross / max(E_prev[i], 1e-9)
                if f <= 0.0 or after <= 0.0:
                    if after <= 0.0:
                        h[i], cash[i], N[i], dead[i] = 0.0, 0.0, 0.0, True
                    else:
                        h[i], N[i], cash[i] = 0.0, 0.0, after
                    continue
                cash[i] = cash_l[i] + (1 - f) * hl[i].sum() * (1 - p.liq_slip) - p.liq_slip * (1 - f) * abs(Nl[i])
                h[i] = f * hl[i] * (1 + r0) / (1 + rl0)
                g = (1 + FR[t]) / (1 + FL[t])
                cash[i] += f * Nl[i] * (g - 1)
                N[i] = f * Nl[i] * g
            if roll[t]:
                rc = np.where(act, np.abs(N) * p.fut_roll_cost, 0.0)
                cash = cash - rc
                acc["trade"] += np.nan_to_num(rc / safe)
        E = h.sum(1) + cash
        newly_dead = act & (E <= 0)
        if newly_dead.any():
            h[newly_dead], cash[newly_dead], N[newly_dead] = 0.0, 0.0, 0.0
            dead |= newly_dead
            E = np.where(newly_dead, 0.0, E)
        if policy == "daily":
            pol = act.copy()
        elif policy == "monthly":
            pol = act & bool(mk.month_end[t])
        elif policy == "band":
            with np.errstate(divide="ignore", invalid="ignore"):
                ex = ((h * MULT).sum(1) + N) / E
            pol = act & (np.abs(ex / L - 1) > p.band)
        elif policy == "never":
            pol = np.zeros(B, bool)
        else:
            raise KeyError(policy)
        reb = (entry | pol) & ~dead & (E > 0)
        if reb.any():
            Er = E[reb]
            ht = Er[:, None] * w
            Nt = Er * n
            delta = ht - h[reb]
            P = mk.P[t]
            c = order_cost_vec(delta, np.broadcast_to(P, delta.shape), delta < 0,
                               np.broadcast_to(p.half_spread, delta.shape), p.min_order).sum(1)
            c = c + p.fut_trade_cost * np.abs(Nt - N[reb])
            h[reb] = ht
            N[reb] = Nt
            cash[reb] = Er - ht.sum(1) - c
            acc["trade"][reb] += c / Er
            trades[reb & ~entry] += 1
            E = h.sum(1) + cash
        inwin = (t >= starts) & (t <= ends)
        V[t - t0, inwin] = E[inwin]
    return {"V": V, "t0": t0, "starts": starts, "ends": ends, "liq": liq, "dead": dead, "trades": trades,
            **{k: v for k, v in acc.items()}}


def path_values(res: dict, b: int, sessions: pd.DatetimeIndex) -> pd.Series:
    s, e, t0 = res["starts"][b], res["ends"][b], res["t0"]
    v = res["V"][s - t0:e - t0 + 1, b]
    return pd.Series(v, index=sessions[s:e + 1])


# ======================================================================== metrics

def path_metrics(v: pd.Series, res: dict, b: int) -> dict:
    years = (len(v) - 1) / TRADING_DAYS
    end_v = v.iloc[-1]
    cagr = (end_v / v.iloc[0]) ** (1 / years) - 1 if end_v > 0 else -1.0
    out = {"start": str(v.index[0].date()), "end": str(v.index[-1].date()), "years": years, "cagr": float(cagr),
           "final_multiple": float(end_v / v.iloc[0]), "min_equity_frac": float(v.min() / v.iloc[0]),
           **drawdown_info(v), "worst_1y": worst_window(v, 252), "worst_3y": worst_window(v, 756),
           "liquidations": int(res["liq"][b]), "wiped_out": bool(res["dead"][b]), "rebalances": int(res["trades"][b])}
    for k in ("interest", "fin", "er", "trade", "fut_fin", "liq_loss"):
        out[f"{k}_per_year"] = float(res[k][b] / years)
    out["financing_per_year"] = out["interest_per_year"] + out["fin_per_year"] + out["fut_fin_per_year"]
    return out


def path_returns(v: pd.Series) -> pd.Series:
    r = v.pct_change().iloc[1:]
    return r.fillna(0.0)


def vs_bench(r: pd.Series, b: pd.Series, q: pd.Series, rf: pd.Series, split_end: str) -> dict:
    """sector_lev period metrics + the A/B record (halves = the two sub-periods, split after ``split_end``)."""
    full = period_metrics(r, b, q, rf)
    i1 = r.index[r.index <= split_end]
    i2 = r.index[r.index > split_end]
    out = {k: full[k] for k in ("oneq_cagr", "qqq_cagr", "cagr_minus_oneq", "cagr_minus_qqq", "oneq_max_dd",
                                "qqq_max_dd", "vol", "sharpe", "oneq_sharpe", "t_monthly_excess_vs_oneq",
                                "beta_vs_oneq", "alpha_ann_vs_oneq", "worst_year", "worst_year_ret")}
    if len(i1) > 250 and len(i2) > 250:
        h1 = period_metrics(r.loc[i1], b.loc[i1], q.loc[i1], rf)
        h2 = period_metrics(r.loc[i2], b.loc[i2], q.loc[i2], rf)
        out["ab_record"] = ab_verdict(full, h1, h2)
    return out


# ======================================================================== windows

def session_index(sessions: pd.DatetimeIndex, date: str) -> int:
    i = int(sessions.searchsorted(pd.Timestamp(date)))
    if i >= len(sessions) or sessions[i] != pd.Timestamp(date):
        raise ValueError(f"{date} is not a session")
    return i


def rolling_windows(sessions: pd.DatetimeIndex, first: str, last: str, months: int) -> tuple[np.ndarray, np.ndarray]:
    """(start, end) session positions of the ``months``-month windows starting at each month end in [first, last]."""
    me = np.flatnonzero(month_end_mask(sessions))
    if sessions[-1].month != sessions[me[-1]].month:      # the data end (END) is itself a month end
        me = np.r_[me, len(sessions) - 1]
    dates = sessions[me]
    sel = np.flatnonzero((dates >= pd.Timestamp(first)) & (dates <= pd.Timestamp(last)))
    st = me[sel]
    en = me[sel + months]
    return st, en


def single_paths(sessions: pd.DatetimeIndex, entry: str, split_end: str) -> dict:
    """(start, end) positions of the full period from ``entry`` and of the two sub-periods split at ``split_end``."""
    last = len(sessions) - 1
    s_full = session_index(sessions, entry)
    split = session_index(sessions, split_end)
    return {"full": (s_full, last), "p1": (s_full, split), "p2": (split, last)}


# ======================================================================== Kelly

def kelly(mk: Market, s: int, e: int, seed: int, grid=np.arange(0.0, 4.0001, 0.05), n_boot: int = 1000,
          block: int = 252) -> dict:
    """Growth-optimal QQQ leverage over sessions s+1..e (margin cost above 1x, T-bill below), the formula value and a
    block bootstrap (``n_boot`` draws of ``block``-session blocks, ``seed``)."""
    r = np.nan_to_num(mk.R[s + 1:e + 1, LI["QQQ"]])
    rf = mk.rf[s + 1:e + 1]
    bo = mk.borrow[s + 1:e + 1]

    def growth(idx):
        rr, ff, bb = r[idx], rf[idx], bo[idx]
        out = np.empty(len(grid))
        for j, L in enumerate(grid):
            c = np.where(L >= 1, -(L - 1) * bb, (1 - L) * ff)
            x = 1 + L * rr + c
            out[j] = np.log(x).mean() * TRADING_DAYS if (x > 0).all() else -np.inf
        return out

    base = np.arange(len(r))
    g = growth(base)
    lstar = float(grid[np.argmax(g)])
    mu = r.mean() * TRADING_DAYS
    var = r.var() * TRADING_DAYS
    rb = bo.sum() / (len(bo) / TRADING_DAYS)          # average annual borrowing cost per unit (actual/360)
    rng = np.random.default_rng(seed)
    nb = int(math.ceil(len(r) / block))
    boots = []
    for _ in range(n_boot):
        st = rng.integers(0, len(r) - block, nb)
        idx = (st[:, None] + np.arange(block)).ravel()[:len(r)]
        boots.append(grid[np.argmax(growth(idx))])
    boots = np.array(boots)
    return {"start": str(mk.sessions[s].date()), "end": str(mk.sessions[e].date()), "empirical_Lstar": lstar,
            "growth_at": {f"{L:.2f}": float(g[np.argmin(np.abs(grid - L))]) for L in (1.0, 1.3, 1.5, 2.0, 3.0)},
            "mu_arith": float(mu), "vol": float(math.sqrt(var)), "borrow_rate_avg": float(rb),
            "formula_Lstar": float((mu - rb) / var),
            "se_mu": float(math.sqrt(var / (len(r) / TRADING_DAYS))),
            "bootstrap": {"n": n_boot, "block": block, "p5": float(np.percentile(boots, 5)),
                          "p25": float(np.percentile(boots, 25)), "median": float(np.median(boots)),
                          "p75": float(np.percentile(boots, 75)), "p95": float(np.percentile(boots, 95)),
                          "share_at_or_below_1": float((boots <= 1.0).mean()),
                          "share_at_grid_max": float((boots >= grid[-1] - 1e-9).mean())}}


# ======================================================================== validation

def validate_legs(d: etfs.Data, mk: Market, spread_2x: float) -> dict:
    out = {}
    s = d.sessions
    rf = d.rf.reindex(s)
    for name, k, real in (("QLD", 2.0, d.rets["QLD"]),
                          ("TQQQ", 3.0, pd.Series(mk.R[:, LI["TQQQX"]], index=s))):
        real = real.dropna()
        if name == "TQQQ":
            real = real.loc[mk.info["tqqq_first_return"]:]
        syn = synthetic_kx(d.rets["QQQ"], rf, k, spread_2x, LEV_ER).reindex(real.index)
        dd = syn - real
        out[f"synthetic_{name}"] = {"window": [str(real.index[0].date()), str(real.index[-1].date())],
                                    "cagr_real": cagr_of(real), "cagr_syn": cagr_of(syn),
                                    "gap_syn_minus_real": cagr_of(syn) - cagr_of(real),
                                    "te_daily_ann": float(dd.std() * math.sqrt(TRADING_DAYS)),
                                    "te_monthly_ann": float((monthly(syn) - monthly(real)).std() * math.sqrt(12)),
                                    "corr": float(np.corrcoef(syn, real)[0, 1])}
    return out


def validate_futures(d: etfs.Data, nq: pd.DataFrame, qqq_raw: pd.DataFrame, assumed_basis: float) -> dict:
    s = d.sessions
    F = pd.Series(nq["close"].values, index=pd.DatetimeIndex(nq["date"]))
    ndx = d.raw["NDX"].set_index(pd.DatetimeIndex(d.raw["NDX"]["date"]))["close"]
    j = pd.concat({"F": F, "S": ndx}, axis=1, join="inner").reindex(s).dropna()
    exp = expiry_sessions(s)
    exp_dates = s[exp]
    pos = s.get_indexer(j.index)
    nxt = np.searchsorted(exp, pos, side="left")
    ok = nxt < len(exp)
    j = j[ok]
    pos, nxt = pos[ok], nxt[ok]
    j["dte"] = exp[nxt] - pos
    j["tau"] = (exp_dates[nxt] - j.index).days / 365.0
    # dividend yield: QQQ trailing 12-month dividends / close
    q = qqq_raw.set_index(pd.DatetimeIndex(qqq_raw["date"]))
    div12 = q["dividend"].rolling("365D").sum()
    yld = (div12 / q["close"]).reindex(j.index).ffill()
    rf_ann = (d.rf.reindex(j.index) * TRADING_DAYS)
    j["implied_r"] = np.log(j["F"] / j["S"]) / j["tau"] + yld
    j["implied_minus_tbill"] = j["implied_r"] - rf_ann
    mid = j[(j["dte"] >= 20) & (j["dte"] <= 60)]
    rS = j["S"].pct_change()
    rF = j["F"].pct_change()
    m2 = (j["dte"] >= 15) & (j["dte"] <= 55) & (j["dte"].shift(1) >= 15) & (j["dte"].shift(1) <= 56)
    a, b = rF[m2].dropna(), rS[m2].dropna()
    a, b = a.align(b, join="inner")
    out = {"window": [str(j.index[0].date()), str(j.index[-1].date())], "sessions_joined": int(len(j)),
           "mid_quarter_daily": {"n": int(len(a)), "corr_F_vs_NDX_price": float(np.corrcoef(a, b)[0, 1]),
                                 "te_daily_ann": float((a - b).std() * math.sqrt(TRADING_DAYS)),
                                 "mean_diff_ann": float((a - b).mean() * TRADING_DAYS),
                                 "model_mean_diff_ann": float((yld.reindex(a.index) - rf_ann.reindex(a.index)).mean())},
           "implied_financing_minus_tbill_mid_quarter": {
               "n": int(len(mid)), "mean": float(mid["implied_minus_tbill"].mean()),
               "median": float(mid["implied_minus_tbill"].median()), "assumed_basis": assumed_basis,
               "by_era": {k: float(mid.loc[a_:b_, "implied_minus_tbill"].median())
                          for k, (a_, b_) in {"2000_2007": ("2000", "2007"), "2008_2015": ("2008", "2015"),
                                              "2016_2021": ("2016", "2021"), "2022_2026": ("2022", "2026")}.items()}},
           "by_days_to_expiry_median": {}}
    for lo, hi in ((11, 20), (21, 40), (41, 63)):
        x = j[(j["dte"] >= lo) & (j["dte"] <= hi)]["implied_minus_tbill"]
        out["by_days_to_expiry_median"][f"{lo}-{hi}"] = {"n": int(len(x)), "median": float(x.median())}
    by_year = mid.groupby(mid.index.year)["implied_minus_tbill"].median()
    out["by_year_median"] = {int(k): float(v) for k, v in by_year.items()}
    return out


def futures_feasibility(d: etfs.Data, account: float = START_EQUITY) -> list[dict]:
    ndx = d.raw["NDX"].set_index(pd.DatetimeIndex(d.raw["NDX"]["date"]))["close"]
    ye = ndx.groupby(ndx.index.year).last()
    rows = []
    for y, lvl in ye.items():
        if y < 1999:
            continue
        mnq = 2 * lvl
        nq = 20 * lvl
        rows.append({"year_end": int(y), "mnq_listed": bool(y >= 2019), "mnq_notional_k": round(mnq / 1000),
                     "nq_notional_k": round(nq / 1000),
                     "min_leverage_10k_mnq": round(mnq / account, 1) if y >= 2019 else None,
                     "min_leverage_10k_nq": round(nq / account, 1),
                     "account_for_L_step_0p1_mnq_k": round(10 * mnq / 1000) if y >= 2019 else None,
                     "account_for_1mnq_as_extra_0p5x_k": round(mnq / 0.5 / 1000) if y >= 2019 else None,
                     "account_for_1mnq_as_extra_0p3x_k": round(mnq / 0.3 / 1000) if y >= 2019 else None})
    return rows


# ======================================================================== rolling-window summaries

def summarise_rolling(cagr: np.ndarray, bc: np.ndarray, qc: np.ndarray, liq: np.ndarray, dead: np.ndarray,
                      mdd: np.ndarray) -> dict:
    return {"n": int(len(cagr)), "cagr_min": float(cagr.min()), "cagr_p5": float(np.percentile(cagr, 5)),
            "cagr_p25": float(np.percentile(cagr, 25)), "cagr_median": float(np.median(cagr)),
            "cagr_p75": float(np.percentile(cagr, 75)), "cagr_p95": float(np.percentile(cagr, 95)),
            "p_beat_oneq": float((cagr > bc).mean()), "p_beat_qqq": float((cagr > qc).mean()),
            "median_minus_oneq": float(np.median(cagr - bc)), "p_liquidation": float((liq > 0).mean()),
            "p_wipeout": float(dead.mean()), "max_dd_median": float(np.median(mdd)), "max_dd_worst": float(mdd.min())}


def window_cagr(res: dict, sessions: pd.DatetimeIndex) -> tuple[np.ndarray, np.ndarray]:
    c, m = [], []
    for b in range(len(res["starts"])):
        v = path_values(res, b, sessions)
        yrs = (len(v) - 1) / TRADING_DAYS
        c.append((v.iloc[-1] / v.iloc[0]) ** (1 / yrs) - 1 if v.iloc[-1] > 0 else -1.0)
        m.append(float((v / v.cummax() - 1).min()))
    return np.array(c), np.array(m)
