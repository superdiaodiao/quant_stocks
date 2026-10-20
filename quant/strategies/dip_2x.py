"""Buy the dip of a mega-cap with a 2x daily-reset long and take profit (docs/research_ledger_dip_2x.md, section 0).

Rules (section 0.3): at the close of t a name on the list in force (P10 / OOS: the latest month-end top 10; U18: all
18) "dips" by T1 (one-day total return <= -5%), T2 (total-return index <= 0.9 x its SMA20) or T3 (Wilder RSI(2) <= 5
while above its SMA200); the deepest dips fill the free slots (at most ``k`` positions, ``slot`` of equity each) at the
close of t+1. A position is sold at the close after its return reaches the take-profit ``z``, falls to the stop (when
the configuration has one), or at the close of the ``max_hold``-th session after the entry fill.

Instruments (section 0.4): I1 the stock; I2 a synthetic single-stock 2x daily-reset ETF (``synthetic_2x`` with the
calibrated financing spread, at a nominal price per share); IM 2x the stock on Reg T margin (EFFR + spread on negative
cash, 25% maintenance check at every close). Idle money sits in cash (T-bill rate less a fund fee) or in QQQ (whole
shares, re-set only on days with a trade).

Everything here is a parameter (``Params``, ``Config``); the study binds the frozen values. ``validate_synthetic`` is
the section 0.2(d) data check of the synthetic leg against the real single-stock 2x ETFs. The helpers under "post-hoc
references" (matched leverage twin, constant-exposure holds of the same lists, a financing-fair matched-exposure hold,
the margin cushion, trigger masks and trade contributions) serve the report-only review checks (ledger, 2026-10-21);
they never enter a verdict.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from quant.backtest.costs import ibkr_order_cost
from quant.data.synthetic import synthetic_2x
from quant.evaluation.metrics import TRADING_DAYS, max_drawdown, monthly, yearly
from quant.signals.technical import rsi_wilder

TRIGGERS = ("T1", "T2", "T3")
INSTRUMENTS = ("I2", "I1", "IM")
LEVERAGE = {"I1": 1.0, "I2": 2.0, "IM": 2.0}    # notional exposure per dollar of a position's own money


@dataclass(frozen=True)
class Config:
    trigger: str            # T1 / T2 / T3
    instrument: str         # I1 / I2 / IM
    z: float                # take-profit on the position return
    stop: float | None      # stop-loss on the position return (negative) or None
    idle: str               # "cash" or "qqq"

    @property
    def name(self) -> str:
        return (f"{self.trigger}-{self.instrument}-Z{round(self.z * 100)}-{'SL' if self.stop is not None else 'noSL'}"
                f"-{self.idle}")

    def twin(self, instrument: str = "I1") -> "Config":
        """The registered 1x twin (section 0.6): same trigger, Z, stop and idle money. Z and the stop apply to each
        position's own return, so a 2x position takes profit / stops at about half the stock move of its twin."""
        return Config(self.trigger, instrument, self.z, self.stop, self.idle)

    def matched_twin(self) -> "Config | None":
        """Post-hoc leverage-only pair: the I1 configuration whose take-profit is the same stock move (Z / 2), without
        a stop (a stop at -7.5% of the stock is not in the grid). Exact for IM (its return is 2 x the stock move: the
        trade lists coincide), approximate for I2 (daily reset). None for I1 and for stop variants."""
        if self.instrument == "I1" or self.stop is not None:
            return None
        return Config(self.trigger, "I1", self.z / LEVERAGE[self.instrument], None, self.idle)


@dataclass(frozen=True)
class Params:
    account: float
    slot: float                  # share of equity per position
    k: int                       # maximum concurrent positions
    max_hold: int                # sessions after the entry fill (sold at that close)
    t1_drop: float               # T1: one-day return <= this
    t2_below: float              # T2: X <= (1 - this) x SMA(sma_short)
    t3_rsi: float                # T3: RSI(rsi_n) <= this and X > SMA(sma_long)
    sma_short: int
    sma_long: int
    rsi_n: int
    etf_spread: float            # I2 financing spread over the T-bill, per year
    etf_er: float                # I2 expense ratio, per year
    etf_price: float             # I2 nominal price per share
    margin_spread: float         # IM / negative cash: EFFR + this (actual / 360)
    maintenance: float           # IM: liquidate when equity < this x gross long value
    reg_t: float                 # gross long value <= this x equity after a purchase
    cash_fee: float              # positive cash earns the T-bill rate less this (per year)
    hs_stock_bps: float
    hs_etf_bps: float
    hs_qqq_bps: float
    spread_mult: float = 1.0
    cash_interest: bool = True


@dataclass
class Universe:
    """One sample. ``X`` (history index x securities): total-return level, NaN before a name's first price, flat after
    its terminal value is booked; ``P``: real price per share (forward-filled) for share counts and commissions;
    ``last_row``: last priced session (NaT = runs to the end); ``lists``: (list date, members) in date order, a list
    being in force for decisions at closes on or after its date; ``dates``: the simulation sessions (the account
    starts at the close of dates[0]); QQQ total-return level / price, the T-bill (per session), EFFR (annual) and
    half the minimum tick in dollars on the history index. ``has_price`` (history index x securities, bool): the
    session has a price of record (not a forward-filled one); a purchase needs it at the fill (section 0.3). None =
    every finite ``X`` counts."""
    name: str
    X: pd.DataFrame
    P: pd.DataFrame
    last_row: pd.Series
    lists: list
    dates: pd.DatetimeIndex
    qqq_level: pd.Series
    qqq_price: pd.Series
    rf: pd.Series
    effr: pd.Series
    half_tick: pd.Series
    labels: dict = field(default_factory=dict)
    has_price: pd.DataFrame | None = None


# ======================================================================== signals (causal: data up to the close of t)

def signals(X: pd.DataFrame, p: Params) -> dict:
    """trigger -> (bool frame, depth score frame; smaller = deeper dip), each on X's index and columns."""
    ret1 = X / X.shift(1) - 1
    sma_s = X.rolling(p.sma_short, min_periods=p.sma_short).mean()
    sma_l = X.rolling(p.sma_long, min_periods=p.sma_long).mean()
    rsi = pd.DataFrame(np.nan, index=X.index, columns=X.columns)
    for c in X.columns:
        x = X[c]
        ok = x.notna().to_numpy()
        if ok.sum() > p.rsi_n:
            first = int(np.argmax(ok))
            seg = x.iloc[first:].ffill().to_numpy(float)
            rsi.iloc[first:, rsi.columns.get_loc(c)] = rsi_wilder(seg, p.rsi_n)
    ratio = X / sma_s
    return {"T1": (ret1 <= p.t1_drop, ret1),
            "T2": (X <= (1 - p.t2_below) * sma_s, ratio),
            "T3": ((rsi <= p.t3_rsi) & (X > sma_l), rsi)}


def list_index(u: Universe) -> list:
    """For each simulation session: the column positions of the list in force at its close."""
    cols = {c: j for j, c in enumerate(u.X.columns)}
    dates = [pd.Timestamp(d) for d, _ in u.lists]
    out = []
    for d in u.dates:
        k = int(np.searchsorted(np.array(dates, dtype="datetime64[ns]"), np.datetime64(d), side="right")) - 1
        out.append([cols[s] for s in u.lists[k][1] if s in cols] if k >= 0 else [])
    return out


def ew_returns(u: Universe) -> pd.Series:
    """Equal-weight daily total return of the list in force at the previous close (no costs), on ``u.dates``."""
    r = (u.X / u.X.shift(1) - 1).reindex(u.dates).to_numpy()
    li = list_index(u)
    out = np.zeros(len(u.dates))
    for i in range(1, len(u.dates)):
        v = r[i, li[i - 1]]
        v = v[np.isfinite(v)]
        out[i] = v.mean() if len(v) else 0.0
    return pd.Series(out, index=u.dates)


# ======================================================================== engine

def _hs(bps: float, px: float, half_tick: float, mult: float) -> float:
    return max(bps * mult / 1e4, half_tick * mult / px)


def simulate(u: Universe, cfg: Config, p: Params, sig: dict | None = None) -> dict:
    """One configuration on one sample (section 0.3 / 0.4). Returns daily NAV, exposures, costs, the trade log and
    counters."""
    sig = signals(u.X, p) if sig is None else sig
    trig_f, score_f = sig[cfg.trigger]
    D = u.dates
    n, cols = len(D), list(u.X.columns)
    r_full = (u.X / u.X.shift(1) - 1)
    R = r_full.reindex(D).fillna(0.0).to_numpy()
    rf = u.rf.reindex(D).fillna(0.0).to_numpy()
    effr = u.effr.reindex(D).ffill().bfill().to_numpy()
    if cfg.instrument == "I2":
        G = synthetic_2x(r_full.reindex(D).fillna(0.0), pd.DataFrame(np.repeat(rf[:, None], len(cols), 1), index=D,
                                                                    columns=cols), p.etf_spread, p.etf_er)
        F = np.maximum(1.0 + G.to_numpy(), 0.0)
    else:
        F = 1.0 + R
    PX = u.P.reindex(D).to_numpy()
    XA = u.X.reindex(D).to_numpy()
    HP = (np.isfinite(XA) if u.has_price is None else
          u.has_price.reindex(index=D, columns=cols).fillna(False).to_numpy(bool) & np.isfinite(XA))
    TRG = trig_f.reindex(D).fillna(False).to_numpy(bool)
    SC = score_f.reindex(D).to_numpy(float)
    QL = u.qqq_level.reindex(D).ffill().to_numpy()
    QP = u.qqq_price.reindex(D).ffill().to_numpy()
    HT = u.half_tick.reindex(D).ffill().to_numpy()
    lr = u.last_row.reindex(cols)
    LR = np.array([n if pd.isna(x) or pd.Timestamp(x) >= D[-1] else
                   int(np.searchsorted(D.values, np.datetime64(pd.Timestamp(x)), side="right")) - 1 for x in lr])
    li = list_index(u)
    days = np.r_[1, np.diff(D.values).astype("timedelta64[D]").astype(int)]
    lev_cash = 2.0 if cfg.instrument == "IM" else 1.0      # position value per dollar of own money
    notional = LEVERAGE[cfg.instrument] if cfg.instrument == "I2" else 1.0   # stock exposure per dollar of value
    mult = p.spread_mult

    cash, q_units = p.account, 0.0
    pos: dict = {}
    pend_sell: dict = {}
    pend_buy: list = []
    nav = np.zeros(n)
    expo = np.zeros(n)
    qw = np.zeros(n)
    npos = np.zeros(n, dtype=int)
    cost_day = np.zeros(n)
    trades = []
    cnt = {"buys": 0, "skipped_no_slot": 0, "skipped_no_cash": 0, "skipped_no_price": 0, "regt_limited": 0,
           "margin_calls": 0, "qqq_orders": 0, "blown": 0}
    blown = False

    def order_cost(shares, px, sell, hs):
        return ibkr_order_cost(shares, px, sell, hs)["total"] if shares > 0 else 0.0

    def close(i, c, reason, hs_mult=1.0):
        nonlocal cash
        ps = pos.pop(c)
        if reason == "delisted" or ps["V"] <= 0:
            k = 0.0
        else:
            px = p.etf_price * ps["V"] / ps["A"] if cfg.instrument == "I2" else PX[i, c]
            bps = p.hs_etf_bps if cfg.instrument == "I2" else p.hs_stock_bps
            if not (np.isfinite(px) and px > 0):
                px = p.etf_price
            sh = ps["shares"] if cfg.instrument == "I2" else ps["V"] / px
            k = order_cost(sh, px, True, _hs(bps, px, HT[i], mult * hs_mult))
        cash += ps["V"] - k
        own = ps["A"] / lev_cash
        pnl = ps["V"] - k - ps["A"] - ps["k_buy"]
        ret_gross = ps["V"] / ps["A"] - 1
        trades.append({"sid": cols[c], "entry": D[ps["i"]].date().isoformat(), "exit": D[i].date().isoformat(),
                       "reason": reason, "sessions": i - ps["i"],
                       "ret_gross": ret_gross * lev_cash, "ret_net": pnl / (own + ps["k_buy"]),
                       "under_ret": XA[i, c] / ps["X0"] - 1, "pnl": pnl, "cost": k + ps["k_buy"]})
        return k

    def qqq_value(i):
        return q_units * QL[i]

    def set_qqq(i, target_value):
        """Trade QQQ toward ``target_value`` (whole shares bought; enough shares sold to get at or below it, all
        of them for a target of 0); purchases are limited by cash (I1 / I2) or by Reg T (IM). Returns the cost."""
        nonlocal cash, q_units
        px = QP[i]
        hs = _hs(p.hs_qqq_bps, px, HT[i], mult)
        cur_sh = q_units * QL[i] / px
        tgt = max(target_value, 0.0)
        if tgt < cur_sh * px:
            d_sh = -cur_sh if tgt <= 0 else -min(cur_sh, math.ceil(cur_sh - tgt / px))
        else:
            d_sh = math.floor((tgt - cur_sh * px) / (px * (1 + hs)))
            if cfg.instrument == "IM":
                sv_ = sum(ps["V"] for ps in pos.values())
                room = p.reg_t * (cash + cur_sh * px + sv_) - sv_ - cur_sh * px
                d_sh = min(d_sh, math.floor(max(room, 0.0) / (px * (1 + hs))))
            else:
                while d_sh >= 1 and d_sh * px + order_cost(d_sh, px, False, hs) > cash:
                    d_sh -= 1
        if d_sh == 0 or (0 < abs(d_sh) < 1 and tgt > 0):
            return 0.0
        k = order_cost(abs(d_sh), px, d_sh < 0, hs)
        q_units += d_sh * px / QL[i]
        cash -= d_sh * px + k
        cnt["qqq_orders"] += 1
        return k

    for i in range(n):
        if blown:
            nav[i] = 0.0
            continue
        day_cost, acted = 0.0, i == 0
        if i > 0:
            if cash > 0 and p.cash_interest:
                cash *= 1 + rf[i] - p.cash_fee / TRADING_DAYS
            elif cash < 0:
                cash *= 1 + (effr[i - 1] + p.margin_spread) * days[i] / 360.0
            for c, ps in pos.items():
                ps["V"] *= F[i, c]
        for c in [c for c in pos if i > LR[c]]:          # terminal value booked: to cash
            close(i, c, "delisted")
            pend_sell.pop(c, None)
            acted = True
        sv = sum(ps["V"] for ps in pos.values())
        nv = cash + qqq_value(i) + sv
        if cfg.instrument == "IM" and pos and nv < p.maintenance * (sv + qqq_value(i)):
            for c in list(pos):
                day_cost += close(i, c, "margin_call", hs_mult=2.0)
            pend_sell, pend_buy = {}, []
            cnt["margin_calls"] += 1
            acted = True
        if cash + qqq_value(i) + sum(ps["V"] for ps in pos.values()) <= 0:
            blown = True
            cnt["blown"] = 1
            nav[i] = 0.0
            continue
        for c, reason in sorted(pend_sell.items()):
            if c in pos:
                day_cost += close(i, c, reason)
                acted = True
        pend_sell = {}
        if pend_buy:
            nav_now = cash + qqq_value(i) + sum(ps["V"] for ps in pos.values())
            plan = []
            for c in pend_buy:
                if c in pos or len(pos) + len(plan) >= p.k:
                    continue
                if i > LR[c] or not HP[i, c]:                # no price at t+1 / series ended: known only at t+1
                    cnt["skipped_no_price"] += 1
                    continue
                tgt = p.slot * nav_now
                if cfg.instrument == "I2":
                    px, bps = p.etf_price, p.hs_etf_bps
                    sh = math.floor(tgt / px)
                else:
                    px, bps = PX[i, c], p.hs_stock_bps
                    if not (np.isfinite(px) and px > 0):
                        continue
                    sh = math.floor(lev_cash * tgt / px)
                plan.append([c, sh, px, _hs(bps, px, HT[i], mult)])
            if plan and cfg.idle == "qqq":
                own_new = sum(sh * px for _, sh, px, _ in plan) / lev_cash
                held = sum(ps["V"] for ps in pos.values()) / lev_cash
                q_target = nav_now - held - own_new - 5.0
                if q_target < qqq_value(i):
                    day_cost += set_qqq(i, q_target)
            for c, sh, px, hs in plan:
                if cfg.instrument == "IM":
                    nav_now2 = cash + qqq_value(i) + sum(ps["V"] for ps in pos.values())
                    room = p.reg_t * nav_now2 - sum(ps["V"] for ps in pos.values()) - qqq_value(i)
                    if sh * px > room:
                        sh = math.floor(max(room, 0.0) / (px * (1 + hs)))
                        cnt["regt_limited"] += 1
                else:
                    while sh >= 1 and sh * px + order_cost(sh, px, False, hs) > cash:
                        sh = math.floor(min(sh - 1, (cash - 1.0) / px))
                if sh < 1:
                    cnt["skipped_no_cash"] += 1
                    continue
                amt = sh * px
                k = order_cost(sh, px, False, hs)
                cash -= amt + k
                day_cost += k
                pos[c] = {"V": amt, "A": amt, "shares": sh, "i": i, "X0": XA[i, c], "k_buy": k}
                cnt["buys"] += 1
                acted = True
            pend_buy = []
        if cfg.idle == "qqq" and acted:
            nv = cash + qqq_value(i) + sum(ps["V"] for ps in pos.values())
            day_cost += set_qqq(i, nv - sum(ps["V"] for ps in pos.values()) / lev_cash - 5.0)
        # decisions at the close of i (filled at i + 1)
        if i < n - 1:
            for c, ps in pos.items():
                rr = (ps["V"] / ps["A"] - 1) * lev_cash
                held = i - ps["i"]
                if held >= 1 and rr >= cfg.z:
                    pend_sell[c] = "take_profit"
                elif held >= 1 and cfg.stop is not None and rr <= cfg.stop:
                    pend_sell[c] = "stop_loss"
                elif held >= p.max_hold - 1:
                    pend_sell[c] = "time_stop"
            slots = p.k - (len(pos) - len(pend_sell))
            # LR[c] >= i: priced at the close of t; whether t is its last price is not known until t+1
            cand = [(SC[i, c], cols[c], c) for c in li[i]
                    if TRG[i, c] and c not in pos and LR[c] >= i and np.isfinite(SC[i, c])]
            cand.sort()
            pend_buy = [c for _, _, c in cand[:max(slots, 0)]]
            cnt["skipped_no_slot"] += max(0, len(cand) - max(slots, 0))
        sv = sum(ps["V"] for ps in pos.values())
        nav[i] = cash + qqq_value(i) + sv
        expo[i] = notional * sv / nav[i] if nav[i] > 0 else 0.0
        qw[i] = qqq_value(i) / nav[i] if nav[i] > 0 else 0.0
        npos[i] = len(pos)
        cost_day[i] = day_cost
    tr = pd.DataFrame(trades, columns=["sid", "entry", "exit", "reason", "sessions", "ret_gross", "ret_net",
                                       "under_ret", "pnl", "cost"])
    if len(tr):
        tr["ticker"] = tr["sid"].map(u.labels).fillna(tr["sid"])
    return {"nav": pd.Series(nav, index=D), "exposure": pd.Series(expo, index=D), "qqq_weight": pd.Series(qw, index=D),
            "positions": pd.Series(npos, index=D), "cost": pd.Series(cost_day, index=D), "trades": tr,
            "open_at_end": [cols[c] for c in pos], "counts": cnt}


# ======================================================================== evaluation helpers (section 0.6)

def h_match_returns(r_ew: pd.Series, r_qqq: pd.Series, rf: pd.Series, e: float, q: float) -> pd.Series:
    """Matched-exposure hold: e x list equal-weight + q x QQQ + (1 - e - q) x T-bill (daily, no costs)."""
    return e * r_ew + q * r_qqq + (1 - e - q) * rf


def t_monthly(a: pd.Series, b: pd.Series) -> float:
    """t-statistic of the mean monthly difference of two daily return series (same index)."""
    m = monthly(a) - monthly(b)
    sd = m.std(ddof=1)
    return float(m.mean() / sd * math.sqrt(len(m))) if sd > 0 else float("nan")


def cagr_window(nav: pd.Series, a: str, b: str) -> float:
    """Calendar CAGR of ``nav`` over [a, b], based at the last close before a."""
    a_, b_ = pd.Timestamp(a), pd.Timestamp(b)
    before = nav.index[nav.index < a_]
    t0 = before[-1] if len(before) else nav.index[0]
    v = nav[(nav.index >= t0) & (nav.index <= b_)]
    yrs = (v.index[-1] - v.index[0]).days / 365.25
    return float((v.iloc[-1] / v.iloc[0]) ** (1 / yrs) - 1) if v.iloc[0] > 0 and v.iloc[-1] > 0 else -1.0


def window_return(nav: pd.Series, a: str, b: str) -> dict:
    """Return and max drawdown of ``nav`` from the last close before a to the last close <= b."""
    a_, b_ = pd.Timestamp(a), pd.Timestamp(b)
    before = nav.index[nav.index < a_]
    t0 = before[-1] if len(before) else nav.index[0]
    v = nav[(nav.index >= t0) & (nav.index <= b_)]
    if len(v) < 2 or v.iloc[0] <= 0:
        return {"ret": float("nan"), "max_dd": float("nan")}
    return {"ret": float(v.iloc[-1] / v.iloc[0] - 1), "max_dd": max_drawdown(v)}


def trade_stats(tr: pd.DataFrame, years: float) -> dict:
    """Per-trade statistics of the closed trades in ``tr``."""
    if not len(tr):
        return {"trades": 0}
    w, lo = tr[tr["ret_net"] > 0], tr[tr["ret_net"] <= 0]
    share = tr["reason"].value_counts(normalize=True)
    worst = tr.loc[tr["ret_net"].idxmin()]
    return {"trades": int(len(tr)), "trades_per_year": len(tr) / years, "win_rate": float((tr["ret_net"] > 0).mean()),
            "avg_ret_net": float(tr["ret_net"].mean()), "avg_win": float(w["ret_net"].mean()) if len(w) else np.nan,
            "avg_loss": float(lo["ret_net"].mean()) if len(lo) else np.nan,
            "avg_under_ret": float(tr["under_ret"].mean()), "avg_sessions": float(tr["sessions"].mean()),
            **{f"share_{k}": float(share.get(k, 0.0)) for k in ("take_profit", "stop_loss", "time_stop", "delisted",
                                                                 "margin_call")},
            "worst_trade": float(worst["ret_net"]), "worst_trade_which": f"{worst['ticker']} {worst['entry']}",
            "best_trade": float(tr["ret_net"].max())}


def by_year(nav: pd.Series) -> pd.Series:
    return yearly(nav.pct_change().dropna())


# ======================================================================== post-hoc references (report only)

def margin_borrow_rate(u: Universe, dates: pd.DatetimeIndex, p: Params) -> pd.Series:
    """Per-session rate on negative cash, as in ``simulate``: (EFFR of the previous session + margin spread) x
    calendar days / 360."""
    effr = u.effr.reindex(dates).ffill().bfill()
    days = pd.Series(np.r_[1, np.diff(dates.values).astype("timedelta64[D]").astype(int)], index=dates)
    return (effr.shift(1).bfill() + p.margin_spread) * days / 360.0


def hold_list_returns(u: Universe, r_ew: pd.Series, p: Params) -> dict:
    """Constant-exposure holds of the same lists (daily, no costs, floored at -100%): 1x = the list equal-weight
    ``r_ew``; 2x ETF = ``synthetic_2x`` of it with the I2 financing spread and fee (an equal-weight basket of the
    single-stock 2x funds, rebalanced daily); 2x margin = 2 x ``r_ew`` less ``margin_borrow_rate`` on one unit
    borrowed per unit of equity."""
    D = r_ew.index
    rf = u.rf.reindex(D).fillna(0.0)
    return {"1x": r_ew, "2x_etf": synthetic_2x(r_ew, rf, p.etf_spread, p.etf_er).clip(lower=-1.0),
            "2x_margin": (2.0 * r_ew - margin_borrow_rate(u, D, p)).clip(lower=-1.0)}


def h_match_financed_returns(r_risk: pd.Series, r_qqq: pd.Series, rf: pd.Series, borrow: pd.Series, w: float,
                             q: float) -> pd.Series:
    """A matched-exposure hold that pays the configuration's own financing: w x ``r_risk`` + q x QQQ + the rest in
    T-bills, or borrowed at ``borrow`` when the rest is negative. For I2 ``r_risk`` is the 2x ETF basket and w half the
    average stock exposure (the money in the funds); for IM ``r_risk`` is the 1x list and w the average exposure."""
    rest = 1.0 - w - q
    return w * r_risk + q * r_qqq + rest * (rf if rest >= 0 else borrow)


def margin_cushion(res: dict, maintenance: float) -> dict:
    """IM: gross long value (positions + QQQ) over equity at each close after the day's trades; its maximum, the
    lowest equity / gross long value and the cushion over the maintenance requirement."""
    g = (res["exposure"] + res["qqq_weight"])
    g = g[g > 0]
    if not len(g):
        return {"max_gross": 0.0, "max_gross_date": "", "min_equity_over_gross": float("nan"), "cushion": float("nan")}
    lo = float((1.0 / g).min())
    return {"max_gross": float(g.max()), "max_gross_date": str(g.idxmax().date()), "min_equity_over_gross": lo,
            "cushion": lo - maintenance}


def mask_trigger(sig: dict, trigger: str, blocks) -> dict:
    """Copy of ``signals`` output with ``trigger`` switched off for (column, first, last) date blocks."""
    trig, score = sig[trigger]
    trig = trig.copy()
    for col, a, b in blocks:
        m = (trig.index >= pd.Timestamp(a)) & (trig.index <= pd.Timestamp(b))
        trig.loc[m, col] = False
    out = dict(sig)
    out[trigger] = (trig, score)
    return out


def trade_contributions(trades: pd.DataFrame, nav: pd.Series) -> pd.Series:
    """Log contribution of each closed trade: log(1 + pnl / NAV at the close before its exit)."""
    prev = nav.shift(1).reindex(pd.to_datetime(trades["exit"])).to_numpy()
    return pd.Series(np.log1p(trades["pnl"].to_numpy() / prev), index=trades.index)


# ======================================================================== section 0.2(d): the synthetic 2x leg

def validate_synthetic(r_etf: pd.Series, r_under: pd.Series, rf: pd.Series, spread: float, er: float) -> dict:
    """Real 2x ETF daily total returns ``r_etf`` vs ``synthetic_2x(r_under, rf, spread, er)`` on the ETF's dates.

    Returns annualised growth of both (and of the cost-free 2 x underlying), their difference, the daily correlation,
    daily and monthly tracking error, the OLS beta of the ETF on the underlying, the implied yearly drag
    252 * mean(2 r_u - r_etf) against the model's 252 * mean(rf) + spread + er, and the implied financing spread over the
    T-bill (implied drag - er - 252 * mean(rf))."""
    d = pd.concat({"e": r_etf, "u": r_under, "rf": rf}, axis=1, sort=True).dropna()
    e, u, f = d["e"], d["u"], d["rf"]
    s = synthetic_2x(u, f, spread, er)
    n = len(d)

    def ann(r):
        return float(np.prod(1 + r.to_numpy()) ** (TRADING_DAYS / n) - 1)

    diff = e - s
    md = monthly(e) - monthly(s)
    beta = float(np.cov(e, u, ddof=1)[0, 1] / np.var(u, ddof=1))
    implied = float(TRADING_DAYS * (2 * u - e).mean())
    rf_ann = float(TRADING_DAYS * f.mean())
    return {"first": str(d.index[0].date()), "last": str(d.index[-1].date()), "sessions": int(n),
            "ann_real": ann(e), "ann_synthetic": ann(s), "ann_2x_no_cost": ann(2 * u),
            "ann_real_minus_synthetic": ann(e) - ann(s),
            "daily_corr": float(np.corrcoef(e, s)[0, 1]),
            "daily_te": float(diff.std(ddof=1) * math.sqrt(TRADING_DAYS)),
            "monthly_te": float(md.std(ddof=1) * math.sqrt(12)) if len(md) > 2 else float("nan"),
            "beta_on_underlying": beta,
            "implied_drag": implied, "model_drag": rf_ann + spread + er, "rf_ann": rf_ann,
            "implied_spread_over_tbill": implied - er - rf_ann,
            "share_days_abs_diff_gt_1pct": float((diff.abs() > 0.01).mean())}


# ======================================================================== samples (section 0.2 (a) - (c))

DECIMAL_TICKS_FROM = "2001-04-09"      # Nasdaq finished decimal quoting; before it the tick was 1/16 dollar


def half_tick_series(index: pd.DatetimeIndex) -> pd.Series:
    return pd.Series(np.where(index < pd.Timestamp(DECIMAL_TICKS_FROM), 1 / 32, 0.005), index=index)


def _effr_on(sessions: pd.DatetimeIndex, end: str, raw_dir) -> pd.Series:
    from quant.data.effr import load_effr
    e = load_effr(end, raw_dir)
    return e.reindex(e.index.union(sessions)).ffill().reindex(sessions).bfill()


def load_p10(top20, window: dict, effr_raw) -> tuple[Universe, dict]:
    """P10: the megacap v2 point-in-time top 10 (``top20`` file, rank <= 10) on the selected panel version (the
    caller selects data v2). Returns the universe and the ONEQ (level, price) on the panel sessions."""
    from quant.data.benchmarks import oneq_on_sessions
    from quant.data.panel import load_window
    from quant.data.rates import daily_rf
    data = load_window("dip_2x_p10", window)
    end = data.spec["effective_end"]
    top = pd.read_csv(top20, dtype={"security_id": str})
    top = top[top["rank"] <= 10]
    lists = [(pd.Timestamp(s), list(g.sort_values("rank")["security_id"])) for s, g in top.groupby("s")]
    ids = sorted(top["security_id"].unique())
    missing = sorted(set(ids) - set(data.sig_idx.columns))
    if missing:
        raise SystemExit(f"P10 ids without panel prices: {missing}")
    s = data.sessions
    u = Universe("P10", data.sig_idx[ids], data.close[ids], data.last_row.reindex(ids), lists,
                 s[(s >= lists[0][0]) & (s <= pd.Timestamp(end))], data.qqq_perf_idx, data.qqq_close,
                 daily_rf(s, end), _effr_on(s, end, effr_raw), half_tick_series(s),
                 dict(zip(top["security_id"], top["ticker"])),
                 has_price=data.close_adj[ids].notna())       # raw close x split factor: NaN where no raw close
    oneq_lvl, oneq_px = oneq_on_sessions(s, window["price_start"], end)
    return u, {"oneq_level": oneq_lvl, "oneq_price": oneq_px, "guard": data.guard["assertion"],
               "terminal_events": data.terminal_events}


def load_u18(symbols, raw_dir, qqq_chart, first: str, end: str, effr_raw) -> tuple[Universe, dict]:
    """U18: the 18 large caps on Yahoo (studies/selective_t's cache), history from their first close, simulation from
    ``first``; QQQ from ``qqq_chart``; ONEQ from the benchmark chart."""
    from quant.data.benchmarks import oneq_on_sessions
    from quant.data.rates import daily_rf
    from quant.strategies import selective_t as st
    mk = {sym: st.load_stock(sym, raw_dir) for sym in symbols}
    q = st.load_qqq_like("QQQ", qqq_chart)
    s = q.s.sessions[q.s.sessions <= pd.Timestamp(end)]
    X, P, H = {}, {}, {}
    for sym, m in mk.items():
        tr = pd.Series(m.s.tr.to_numpy(float), index=m.s.sessions).fillna(0.0)
        X[sym] = (1 + tr).cumprod().reindex(s)
        close = pd.Series(m.s.close, index=m.s.sessions).reindex(s)
        P[sym] = close.ffill()
        H[sym] = close.notna()
    qtr = pd.Series(q.s.tr.to_numpy(float), index=q.s.sessions).fillna(0.0).reindex(s)
    u = Universe("U18", pd.DataFrame(X), pd.DataFrame(P), pd.Series(pd.NaT, index=list(symbols)),
                 [(s[0], list(symbols))], s[(s >= pd.Timestamp(first))], (1 + qtr).cumprod(),
                 pd.Series(q.s.close, index=q.s.sessions).reindex(s), daily_rf(s, end), _effr_on(s, end, effr_raw),
                 half_tick_series(s), {sym: sym for sym in symbols}, has_price=pd.DataFrame(H))
    oneq_lvl, oneq_px = oneq_on_sessions(s, str(s[0].date()), end)
    checks = {sym: {"first": str(m.s.sessions[0].date()), "last": str(m.s.sessions[-1].date())} for sym, m in mk.items()}
    return u, {"oneq_level": oneq_lvl, "oneq_price": oneq_px, "checks": checks}


def load_oos(first: str, ranks_ref, effr_raw) -> tuple[Universe, dict]:
    """OOS: the 1999-2013 point-in-time top 10 of the megacap OOS.2 build (stops when the ranking differs from the
    OOS.2 file ``ranks_ref``), total-return levels and per-share prices of quant.data.megacap_history.engine_panels."""
    from quant.data import megacap_history as o2
    from quant.data.rates import daily_rf
    cand, sessions, panel, srcs, facts, metas = o2.build(write_fixes=False)
    signals_ = o2.signal_sessions(sessions)
    mcaps = o2.market_caps(cand, facts, panel, signals_)
    ranks = o2.rank_table(mcaps, cand)
    ref = pd.read_csv(ranks_ref)
    same = len(ref) == len(ranks) and (ranks[["s", "rank", "key"]].to_numpy() == ref[["s", "rank", "key"]].to_numpy()).all()
    if not same:
        raise SystemExit("OOS ranking differs from the OOS.2 ranks_top20_by_month.csv: stop (section 0.2 (c))")
    idx, close, last_row, term = o2.engine_panels(cand, sessions, panel, facts)
    top = ranks[ranks["rank"] <= 10]
    lists = [(pd.Timestamp(s), list(g.sort_values("rank")["key"])) for s, g in top.groupby("s")]
    keys = sorted(top["key"].unique())
    qqq, oneq = o2.load_qqq(), o2.load_oneq()
    end = o2.END
    # a price of record = a total-return level on that session (engine_panels forward-fills it into idx; a missing
    # raw close with a level is priced from the adjusted close there)
    has_price = pd.DataFrame({k: panel[k]["tr"].reindex(sessions).notna() for k in keys}, index=sessions)
    u = Universe("OOS", idx[keys], close[keys], last_row.reindex(keys), lists,
                 sessions[sessions >= pd.Timestamp(first)], qqq["adjclose"].reindex(sessions).ffill(),
                 qqq["close"].reindex(sessions).ffill(), daily_rf(sessions, end), _effr_on(sessions, end, effr_raw),
                 half_tick_series(sessions), {k: k for k in keys}, has_price=has_price)
    return u, {"qqq": qqq, "oneq": oneq, "terminal": term, "ranking_identical_to_oos2": bool(same),
               "price_sources": srcs}
