"""Ways to hold leveraged Nasdaq exposure: a decision aid (pre-registered in docs/research_ledger_leverage_methods.md, section 0).

Methods at target leverage L in {1.0, 1.1, 1.2, 1.3, 1.5, 1.75, 2.0}:
- M   QQQ + IBKR margin loan (EFFR + 1.5%, actual/360), 25% maintenance, real-time forced liquidation at the day's low
- M1  ONEQ + IBKR margin loan (from ONEQ's first close, 2003-10-01)
- Q2  QQQ + QLD (synthetic 2x before 2006-06-22), no loan
- Q3  QQQ + TQQQ (synthetic 3x before 2010-02-12), reference
- T3  TQQQ + T-bill ETF, reference
- F   Nasdaq-100 futures, synthetic (NDX total return - T-bill - basis), fractional contracts (idealised), margin 7%

Rebalance policies: daily, monthly, band (|E/L - 1| > 10%), never. Periods: 1999-03-10..2026-09-30 and the two sub-periods
split at 2009-12-31, plus 211 rolling 10-year windows (every month end 1999-03..2016-09). Costs: IBKR Pro Tiered, $10,000.

Raw data stays local (research_cache/sector_lev/raw, research_cache/leverage_methods/raw); every frame is truncated at
``END`` and asserted. Outputs: output/research_only/leverage_methods/ (returns and metrics only, no price levels).
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

import scripts.research_sector_lev as sl
from scripts.research_qqq_timing import (
    CASH_ETF_FEE, COMMISSION_MAX_FRAC, COMMISSION_MIN, COMMISSION_PER_SHARE, FEES_PER_SHARE, LEV_ER, NOMINAL_PRICE,
    SELL_REG_FRAC, START_EQUITY, TRADING_DAYS, assert_dev_dates, cagr_of, monthly, parse_chart, truncate_dev,
)
from scripts.research_regime import month_end_mask

ROOT = Path(__file__).resolve().parents[1]
END = sl.END                                   # 2026-09-30
RAW = Path("/Users/bytedance/code/quant_stocks/research_cache/leverage_methods/raw")
OUT = ROOT / "output/research_only/leverage_methods"

LEVELS = (1.0, 1.1, 1.2, 1.3, 1.5, 1.75, 2.0)
POLICIES = ("daily", "monthly", "band", "never")
BAND = 0.10

# ---------------------------------------------------------------- frozen parameters (section 0)
IBKR_SPREAD = 0.015            # IBKR Pro USD tier 0-100k: BM + 1.5% (checked 2026-10-08)
DAYCOUNT = 360.0
SPREAD_2X = sl.SPREAD_2X       # 0.70%, frozen QLD calibration (QQQ timing ledger); also used per borrowed unit in 3x
QQQ_ER = 0.0020
ONEQ_ER = 0.0021
FUT_BASIS = 0.0030             # implied financing above T-bill
FUT_MAINT = 0.07               # maintenance margin, share of notional (approximate)
FUT_INIT = FUT_MAINT * 1.1
FUT_TRADE_COST = 1e-4          # of traded notional
FUT_ROLL_COST = 1e-4           # of notional, per quarterly roll
ROLL_OFFSET = 5                # sessions before expiry
LIQ_SLIP = 0.005
LIQ_CUSHION = 1.10
MIN_ORDER = 1.0
SEED = 20261008

# Fed funds target before the NY Fed EFFR series starts (2000-07-03): (effective date, rate)
FF_TARGET = (("1998-11-17", 4.75), ("1999-06-30", 5.00), ("1999-08-24", 5.25), ("1999-11-16", 5.50),
             ("2000-02-02", 5.75), ("2000-03-21", 6.00), ("2000-05-16", 6.50))

ENTRY_FULL = "1999-03-10"
ONEQ_FIRST = "2003-10-01"
SPLIT_END = "2009-12-31"
ROLL_FIRST, ROLL_LAST, ROLL_MONTHS = "1999-03-31", "2016-09-30", 120

# legs universe
LEGS = ("QQQ", "ONEQ", "QLDX", "TQQQX", "TBILL", "BENCH")
MULT = np.array([1.0, 1.0, 2.0, 3.0, 0.0, 1.0])
MAINT = np.array([0.25, 0.25, 0.50, 0.75, 0.25, 0.25])
HALF_SPREAD = np.array([1e-4, 2e-4, 2e-4, 2e-4, 1e-4, 2e-4])
LI = {k: i for i, k in enumerate(LEGS)}


# ======================================================================== data

def load_effr(end: str = END, raw_dir: Path = RAW) -> pd.Series:
    """Daily EFFR (decimal) from the NY Fed CSV; Fed funds target filled in before the series starts."""
    df = pd.read_csv(raw_dir / "EFFR_nyfed.csv")
    df = df[df["Rate Type"] == "EFFR"]
    out = pd.DataFrame({"date": pd.to_datetime(df["Effective Date"], format="%m/%d/%Y").dt.strftime("%Y-%m-%d"),
                        "rate": pd.to_numeric(df["Rate (%)"], errors="coerce") / 100.0}).dropna()
    out = truncate_dev(out.sort_values("date"), "date", end)
    s = pd.Series(out["rate"].values, index=pd.DatetimeIndex(out["date"]))
    first = s.index[0]
    tgt = pd.Series([r / 100.0 for _, r in FF_TARGET], index=pd.DatetimeIndex([d for d, _ in FF_TARGET]))
    pre_idx = pd.date_range(tgt.index[0], first - pd.Timedelta(days=1), freq="D")
    pre = tgt.reindex(pre_idx.union(tgt.index)).ffill().reindex(pre_idx)
    return pd.concat([pre, s]).sort_index()


def chart_ohlc(path: Path, end: str = END) -> pd.DataFrame:
    """date, close, adjclose, low (low adjusted by adjclose / close), truncated at ``end``."""
    j = json.loads(Path(path).read_text())["chart"]["result"][0]
    ts = pd.to_datetime(j["timestamp"], unit="s", utc=True).tz_convert("America/New_York")
    q = j["indicators"]["quote"][0]
    adj = j["indicators"].get("adjclose", [{}])[0].get("adjclose") or q["close"]
    df = pd.DataFrame({"date": ts.strftime("%Y-%m-%d"), "close": q["close"], "adjclose": adj, "low": q["low"]})
    df = truncate_dev(df.dropna(subset=["close"]), "date", end)
    df["low_adj"] = df["low"] * df["adjclose"] / df["close"]
    return df.reset_index(drop=True)


def synthetic_kx(r1: pd.Series, rf: pd.Series, k: float, spread: float = SPREAD_2X, er: float = LEV_ER) -> pd.Series:
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

    def futures(self, basis: float = FUT_BASIS) -> tuple[np.ndarray, np.ndarray]:
        f = self.r_tr - (self.rf + basis / TRADING_DAYS)
        fl = np.minimum(self.rl_tr - (self.rf + basis / TRADING_DAYS), f)
        return f, fl


def build_market(d: sl.Data, effr: pd.Series, tqqq: pd.DataFrame, lows: dict) -> Market:
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
    syn3 = synthetic_kx(r_q, rf, 3.0)
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
    lend = (rf + SPREAD_2X / TRADING_DAYS).values
    T = len(s)
    fin = np.zeros((T, len(LEGS)))
    fin[:, LI["QLDX"]] = lend
    fin[:, LI["TQQQX"]] = 2 * lend
    er = np.zeros((T, len(LEGS)))
    er[:, LI["QQQ"]] = QQQ_ER / TRADING_DAYS
    er[:, LI["ONEQ"]] = ONEQ_ER / TRADING_DAYS
    er[:, LI["BENCH"]] = ONEQ_ER / TRADING_DAYS
    er[:, LI["QLDX"]] = LEV_ER / TRADING_DAYS
    er[:, LI["TQQQX"]] = LEV_ER / TRADING_DAYS
    er[:, LI["TBILL"]] = CASH_ETF_FEE / TRADING_DAYS
    # margin interest for the period previous session -> t, at the previous session's EFFR + spread, actual/360
    e = effr.reindex(effr.index.union(s)).ffill().reindex(s)
    days = np.r_[1, np.diff(s.values).astype("timedelta64[D]").astype(int)]
    borrow = ((e.shift(1).bfill() + IBKR_SPREAD) * days / DAYCOUNT).values
    r_tr = (r_q + QQQ_ER / TRADING_DAYS).values
    rl_tr = (rl["QQQ"] + QQQ_ER / TRADING_DAYS).values
    info = {"tqqq_first_return": str(ft.date()), "effr_first": str(effr.index[0].date()),
            "effr_last": str(effr.index[-1].date())}
    return Market(sessions=s, R=rets[list(LEGS)].values, RL=rl[list(LEGS)].values, P=px[list(LEGS)].values,
                  fin=fin, er=er, r_tr=r_tr, rl_tr=rl_tr, rf=rf.values, borrow=borrow,
                  month_end=month_end_mask(s), info=info)


# ======================================================================== methods

@dataclass(frozen=True)
class Method:
    name: str
    description: str
    not_before: str = ENTRY_FULL


METHODS = (
    Method("M", "QQQ + IBKR margin loan"),
    Method("M1", "ONEQ + IBKR margin loan", ONEQ_FIRST),
    Method("Q2", "QQQ + QLD (2x), no loan"),
    Method("Q3", "QQQ + TQQQ (3x), reference"),
    Method("T3", "TQQQ + T-bill ETF, reference"),
    Method("F", "Nasdaq-100 futures (synthetic, fractional contracts) + T-bill ETF"),
)
METHOD = {m.name: m for m in METHODS}


def method_weights(name: str, L: float) -> tuple[np.ndarray, float]:
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
        w[LI["TBILL"]] = 1.0 - FUT_INIT * L
    elif name == "QQQ_BH":
        w[LI["QQQ"]] = 1.0
    elif name == "BENCH_BH":
        w[LI["BENCH"]] = 1.0
    else:
        raise KeyError(name)
    return w, n


def order_cost_vec(value: np.ndarray, price: np.ndarray, sell: np.ndarray, half_spread: np.ndarray) -> np.ndarray:
    """Vectorised scripts.research_regime.order_cost (orders of <= MIN_ORDER dollars are not placed)."""
    value = np.abs(value)
    shares = value / price
    comm = np.minimum(np.maximum(COMMISSION_MIN, COMMISSION_PER_SHARE * shares), COMMISSION_MAX_FRAC * value)
    c = comm + FEES_PER_SHARE * shares + half_spread * value + np.where(sell, SELL_REG_FRAC * value, 0.0)
    return np.where(value > MIN_ORDER, c, 0.0)


# ======================================================================== engine (vectorised over start dates)

def simulate_batch(mk: Market, method: str, L: float, policy: str, starts: np.ndarray, ends: np.ndarray,
                   basis: float = FUT_BASIS, roll_offset: int = ROLL_OFFSET, liquidation: bool = True) -> dict:
    """$10,000 cash at the close of each start; returns the value paths (T_span, B) and per-path accounting."""
    starts, ends = np.asarray(starts, int), np.asarray(ends, int)
    B, K = len(starts), len(LEGS)
    w, n = method_weights(method, L)
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
            req_l = (np.abs(hl) * MAINT).sum(1) + np.abs(Nl) * FUT_MAINT
            breach = act & (eq_l < req_l) & (req_l > 0) if liquidation else np.zeros(B, bool)
            normal = act & ~breach
            cash = np.where(normal, cash + N * FR[t], cash)
            h = np.where(normal[:, None], h * (1 + r0), h)
            N = np.where(normal, N * (1 + FR[t]), N)
            for i in np.flatnonzero(breach):
                liq[i] += 1
                gross = hl[i].sum() + abs(Nl[i])
                den = LIQ_CUSHION * req_l[i] - LIQ_SLIP * gross
                f = (eq_l[i] - LIQ_SLIP * gross) / den if den > 0 else 0.0
                f = min(max(f, 0.0), 1.0)
                after = eq_l[i] - LIQ_SLIP * (1 - f) * gross
                acc["liq_loss"][i] += LIQ_SLIP * (1 - f) * gross / max(E_prev[i], 1e-9)
                if f <= 0.0 or after <= 0.0:
                    if after <= 0.0:
                        h[i], cash[i], N[i], dead[i] = 0.0, 0.0, 0.0, True
                    else:
                        h[i], N[i], cash[i] = 0.0, 0.0, after
                    continue
                cash[i] = cash_l[i] + (1 - f) * hl[i].sum() * (1 - LIQ_SLIP) - LIQ_SLIP * (1 - f) * abs(Nl[i])
                h[i] = f * hl[i] * (1 + r0) / (1 + rl0)
                g = (1 + FR[t]) / (1 + FL[t])
                cash[i] += f * Nl[i] * (g - 1)
                N[i] = f * Nl[i] * g
            if roll[t]:
                rc = np.where(act, np.abs(N) * FUT_ROLL_COST, 0.0)
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
            pol = act & (np.abs(ex / L - 1) > BAND)
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
                               np.broadcast_to(HALF_SPREAD, delta.shape)).sum(1)
            c = c + FUT_TRADE_COST * np.abs(Nt - N[reb])
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

def drawdown_info(v: pd.Series) -> dict:
    peak = v.cummax()
    dd = v / peak - 1
    trough = dd.idxmin()
    pk = v.loc[:trough].idxmax()
    after = v.loc[trough:]
    rec = after[after >= v.loc[pk]]
    yrs = lambda a, b: (b - a).days / 365.25
    under = (v < peak).values
    best = cur = 0
    for u in under:
        cur = cur + 1 if u else 0
        best = max(best, cur)
    return {"max_dd": float(dd.min()), "dd_peak": str(pk.date()), "dd_trough": str(trough.date()),
            "dd_recovered": str(rec.index[0].date()) if len(rec) else "not recovered",
            "years_peak_to_recovery": yrs(pk, rec.index[0]) if len(rec) else float("nan"),
            "years_underwater_so_far": float("nan") if len(rec) else yrs(pk, v.index[-1]),
            "longest_underwater_years": best / TRADING_DAYS}


def worst_window(v: pd.Series, n: int) -> float:
    x = v.values
    if len(x) <= n:
        return float("nan")
    with np.errstate(divide="ignore", invalid="ignore"):
        r = x[n:] / x[:-n] - 1
    r = np.where(x[:-n] > 0, r, -1.0)
    return float(np.nanmin(r))


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


def vs_bench(r: pd.Series, b: pd.Series, q: pd.Series, rf: pd.Series) -> dict:
    """sector_lev period metrics + the A/B record (halves = the two sub-periods)."""
    full = sl.period_metrics(r, b, q, rf)
    i1 = r.index[r.index <= SPLIT_END]
    i2 = r.index[r.index > SPLIT_END]
    out = {k: full[k] for k in ("oneq_cagr", "qqq_cagr", "cagr_minus_oneq", "cagr_minus_qqq", "oneq_max_dd",
                                "qqq_max_dd", "vol", "sharpe", "oneq_sharpe", "t_monthly_excess_vs_oneq",
                                "beta_vs_oneq", "alpha_ann_vs_oneq", "worst_year", "worst_year_ret")}
    if len(i1) > 250 and len(i2) > 250:
        h1 = sl.period_metrics(r.loc[i1], b.loc[i1], q.loc[i1], rf)
        h2 = sl.period_metrics(r.loc[i2], b.loc[i2], q.loc[i2], rf)
        out["ab_record"] = sl.evaluate(full, h1, h2)
    return out


# ======================================================================== windows

def session_index(sessions: pd.DatetimeIndex, date: str) -> int:
    i = int(sessions.searchsorted(pd.Timestamp(date)))
    if i >= len(sessions) or sessions[i] != pd.Timestamp(date):
        raise ValueError(f"{date} is not a session")
    return i


def rolling_windows(sessions: pd.DatetimeIndex, first: str = ROLL_FIRST, last: str = ROLL_LAST,
                    months: int = ROLL_MONTHS) -> tuple[np.ndarray, np.ndarray]:
    me = np.flatnonzero(month_end_mask(sessions))
    if sessions[-1].month != sessions[me[-1]].month:      # the data end (END) is itself a month end
        me = np.r_[me, len(sessions) - 1]
    dates = sessions[me]
    sel = np.flatnonzero((dates >= pd.Timestamp(first)) & (dates <= pd.Timestamp(last)))
    st = me[sel]
    en = me[sel + months]
    return st, en


def single_paths(sessions: pd.DatetimeIndex, method: str) -> dict:
    last = len(sessions) - 1
    s_full = session_index(sessions, METHOD[method].not_before if method in METHOD else ENTRY_FULL)
    split = session_index(sessions, SPLIT_END)
    return {"full": (s_full, last), "p1": (s_full, split), "p2": (split, last)}


# ======================================================================== Kelly

def kelly(mk: Market, s: int, e: int, grid=np.arange(0.0, 4.0001, 0.05), n_boot: int = 1000, block: int = 252,
          seed: int = SEED) -> dict:
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

def validate_legs(d: sl.Data, mk: Market) -> dict:
    out = {}
    s = d.sessions
    rf = d.rf.reindex(s)
    for name, k, real in (("QLD", 2.0, d.rets["QLD"]),
                          ("TQQQ", 3.0, pd.Series(mk.R[:, LI["TQQQX"]], index=s))):
        real = real.dropna()
        if name == "TQQQ":
            real = real.loc[mk.info["tqqq_first_return"]:]
        syn = synthetic_kx(d.rets["QQQ"], rf, k).reindex(real.index)
        dd = syn - real
        out[f"synthetic_{name}"] = {"window": [str(real.index[0].date()), str(real.index[-1].date())],
                                    "cagr_real": cagr_of(real), "cagr_syn": cagr_of(syn),
                                    "gap_syn_minus_real": cagr_of(syn) - cagr_of(real),
                                    "te_daily_ann": float(dd.std() * math.sqrt(TRADING_DAYS)),
                                    "te_monthly_ann": float((monthly(syn) - monthly(real)).std() * math.sqrt(12)),
                                    "corr": float(np.corrcoef(syn, real)[0, 1])}
    return out


def validate_futures(d: sl.Data, nq: pd.DataFrame, qqq_raw: pd.DataFrame) -> dict:
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
               "median": float(mid["implied_minus_tbill"].median()), "assumed_basis": FUT_BASIS,
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


def futures_feasibility(d: sl.Data, account: float = START_EQUITY) -> list[dict]:
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


# ======================================================================== main

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


def run(args=None) -> dict:
    d = sl.load_data(END)
    effr = load_effr(END)
    tqqq = parse_chart(RAW / "chart_TQQQ.json", END)
    nq = parse_chart(RAW / "chart_NQ%3DF.json", END)
    lows = {t: chart_ohlc(sl.RAW / f"chart_{t}.json", END) for t in ("QQQ", "ONEQ")}
    for name, ix in (("effr", effr.index), ("tqqq", tqqq["date"]), ("nq", nq["date"])):
        assert_dev_dates(ix, END)
    mk = build_market(d, effr, tqqq, lows)
    s = mk.sessions
    print("DATE GUARD: every frame truncated at", END, mk.info)
    res = {"guard": {**d.guard, **mk.info, "effr_rows": int(len(effr))}, "params": {
        "levels": LEVELS, "policies": POLICIES, "band": BAND, "ibkr_spread": IBKR_SPREAD, "spread_2x": SPREAD_2X,
        "fut_basis": FUT_BASIS, "fut_maint": FUT_MAINT, "fut_init": FUT_INIT, "roll_offset": ROLL_OFFSET,
        "liq_slip": LIQ_SLIP, "liq_cushion": LIQ_CUSHION}}
    res["validation"] = {"legs": validate_legs(d, mk), "futures": validate_futures(d, nq, d.raw["QQQ"])}
    res["validation"]["effr_vs_ibkr_page"] = {"effr_last": float(effr.iloc[-1]), "ibkr_bm_2026_10_08": 0.0388,
                                              "ibkr_tier1_rate": 0.0538}
    print("validation", json.dumps(res["validation"]["legs"], default=float))
    print("futures", json.dumps({k: v for k, v in res["validation"]["futures"].items() if k != "by_year_median"},
                                default=float))

    rs, re_ = rolling_windows(s)
    oneq_i = session_index(s, ONEQ_FIRST)
    rows, roll_rows = [], []
    bench_cache = {}

    def benches(starts, ends):
        key = (tuple(starts), tuple(ends))
        if key not in bench_cache:
            bench_cache[key] = (simulate_batch(mk, "BENCH_BH", 1.0, "never", starts, ends),
                                simulate_batch(mk, "QQQ_BH", 1.0, "never", starts, ends))
        return bench_cache[key]

    def one_config(method, L, policy, tag="main", **kw):
        sp = single_paths(s, method)
        if method == "M1":
            mask = rs >= oneq_i
            st, en = rs[mask], re_[mask]
        else:
            st, en = rs, re_
        labels = list(sp)
        starts = np.r_[[sp[k][0] for k in labels], st]
        ends = np.r_[[sp[k][1] for k in labels], en]
        r = simulate_batch(mk, method, L, policy, starts, ends, **kw)
        bb, qq = benches(starts, ends)
        for bi, lab in enumerate(labels):
            v = path_values(r, bi, s)
            pm = path_metrics(v, r, bi)
            rr = path_returns(v)
            bret = path_returns(path_values(bb, bi, s))
            qret = path_returns(path_values(qq, bi, s))
            vb = vs_bench(rr, bret, qret, d.rf)
            ab = vb.pop("ab_record", None)
            rows.append({"variant": tag, "method": method, "L": L, "policy": policy, "period": lab, **pm, **vb,
                         "ab_A": ab["A"] if ab else None, "ab_B": ab["B"] if ab else None,
                         "ab_pass_record": ab["pass"] if ab else None})
        nl = len(labels)
        sub = {k: (v[nl:] if isinstance(v, np.ndarray) and v.ndim == 1 else v) for k, v in r.items() if k != "V"}
        sub["V"] = r["V"][:, nl:]
        bsub = {"V": bb["V"][:, nl:], "starts": bb["starts"][nl:], "ends": bb["ends"][nl:], "t0": bb["t0"]}
        qsub = {"V": qq["V"][:, nl:], "starts": qq["starts"][nl:], "ends": qq["ends"][nl:], "t0": qq["t0"]}
        c, mdd = window_cagr(sub, s)
        bc, _ = window_cagr(bsub, s)
        qc, _ = window_cagr(qsub, s)
        summ = summarise_rolling(c, bc, qc, sub["liq"], sub["dead"], mdd)
        oneq_only = sub["starts"] >= oneq_i
        so = summarise_rolling(c[oneq_only], bc[oneq_only], qc[oneq_only], sub["liq"][oneq_only],
                               sub["dead"][oneq_only], mdd[oneq_only])
        roll_rows.append({"variant": tag, "method": method, "L": L, "policy": policy, **summ,
                          "oneq_real_starts_n": so["n"], "oneq_real_p_beat_oneq": so["p_beat_oneq"],
                          "oneq_real_cagr_median": so["cagr_median"],
                          "oneq_real_median_minus_oneq": so["median_minus_oneq"]})
        full = [x for x in rows if x["method"] == method and x["L"] == L and x["policy"] == policy
                and x["period"] == "full" and x["variant"] == tag][-1]
        print(f"{tag:10s} {method:2s} L{L:.2f} {policy:7s} CAGR {full['cagr']:+.2%} MDD {full['max_dd']:.1%} "
              f"liq {full['liquidations']} wipe {full['wiped_out']} fin {full['financing_per_year']:.2%} "
              f"| 10y med {summ['cagr_median']:+.2%} p5 {summ['cagr_p5']:+.2%} beatONEQ {summ['p_beat_oneq']:.0%} "
              f"pLiq {summ['p_liquidation']:.0%}")

    for m in METHODS:
        for L in LEVELS:
            for pol in POLICIES:
                one_config(m.name, L, pol)
    # report-only futures sensitivities
    for tag, kw in (("basis_0", dict(basis=0.0)), ("basis_1pct", dict(basis=0.01)),
                    ("roll_1d", dict(roll_offset=1)), ("roll_10d", dict(roll_offset=10))):
        for L in LEVELS:
            for pol in ("monthly", "never"):
                one_config("F", L, pol, tag=tag, **kw)
    # references
    for nm, method in (("ONEQ_spliced_buyhold", "BENCH_BH"), ("QQQ_buyhold", "QQQ_BH")):
        sp = single_paths(s, "M")
        labels = list(sp)
        r = simulate_batch(mk, method, 1.0, "never", [sp[k][0] for k in labels], [sp[k][1] for k in labels])
        for bi, lab in enumerate(labels):
            v = path_values(r, bi, s)
            rows.append({"variant": "reference", "method": nm, "L": 1.0, "policy": "never", "period": lab,
                         **path_metrics(v, r, bi)})
    # Kelly
    last = len(s) - 1
    i0, isp = session_index(s, ENTRY_FULL), session_index(s, SPLIT_END)
    res["kelly"] = {"full": kelly(mk, i0, last), "p1": kelly(mk, i0, isp), "p2": kelly(mk, isp, last)}
    print("kelly", {k: (v["empirical_Lstar"], v["formula_Lstar"], v["bootstrap"]["p5"], v["bootstrap"]["p95"])
                    for k, v in res["kelly"].items()})
    res["futures_feasibility"] = futures_feasibility(d)

    OUT.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(OUT / "summary.csv", index=False, float_format="%.6f")
    pd.DataFrame(roll_rows).to_csv(OUT / "rolling_10y.csv", index=False, float_format="%.6f")
    pd.DataFrame(res["futures_feasibility"]).to_csv(OUT / "futures_feasibility.csv", index=False)
    (OUT / "results.json").write_text(json.dumps(res, indent=2, default=float))
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    run(ap.parse_args(argv))


if __name__ == "__main__":
    main()
