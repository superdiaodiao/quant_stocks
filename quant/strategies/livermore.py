"""Livermore-style breakout trading of weekly relative-strength leaders (pyramiding, trailing exits), and its engine.

Extracted unchanged from scripts/research_livermore.py: the rules (``parse_pivot``, ``breakout_frame``,
``trail_frame``, ``market_state``, ``weekly_leaders``, ``Config``), the engine ``simulate``, ``Runner``, the metrics
``perf_metrics`` / ``sub_metrics`` / ``trade_stats`` and the eligible universe. Also used by research_stops (stop
overlays) and, for the universe / trade statistics, by indicators, oneil, earnings_events and mean_reversion.
The study windows and the window loader are quant.data.panel (``WINDOWS``, ``load_window``).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from quant.backtest import stop_rules
from quant.backtest.costs import rank_half_spread
from quant.data import panel
from quant.data.panel import TERMINAL_D5, WINDOWS, WinData  # noqa: F401  (lv.WINDOWS / lv.WinData)
from quant.evaluation.metrics import max_drawdown_of_returns
from quant.strategies import canslim as cs


def load_window(name: str, terminal_awaiting: float = TERMINAL_D5) -> WinData:
    """One of the study windows ``WINDOWS`` (dev 2017-2022, test1 2023-2026, test2 2012-2016) of the panel."""
    return panel.load_window(name, WINDOWS[name], terminal_awaiting)


INPUTS = cs.INPUTS
CACHE = cs.CACHE
DateGuardError = cs.DateGuardError
def parse_pivot(pivot: str) -> tuple[int, float | None]:
    """'high252' -> (252, None); 'base50_t20' -> (50, 0.20)."""
    if pivot.startswith("high"):
        return int(pivot[4:]), None
    a, b = pivot[4:].split("_t")
    return int(a), int(b) / 100


def breakout_frame(close_adj: pd.DataFrame, vol_adj: pd.DataFrame, pivot: str, vol_mult: float | None) -> pd.DataFrame:
    """True at session d when d's close breaks above the pivotal point built from closes strictly before d."""
    n, tight = parse_pivot(pivot)
    prior = close_adj.shift(1)
    hi = prior.rolling(n, min_periods=n if tight is not None else max(n - 12, 1)).max()
    sig = close_adj > hi
    if tight is not None:
        lo = prior.rolling(n, min_periods=n).min()
        sig &= (hi / lo - 1) <= tight
    if vol_mult is not None:
        avg = vol_adj.shift(1).rolling(50, min_periods=40).mean()
        sig &= vol_adj >= vol_mult * avg
    return sig.fillna(False)


def trail_frame(close_adj: pd.DataFrame, trail: str) -> pd.DataFrame | None:
    """Exit flag at session d for the moving-average / N-day-low trails (pct trails are handled in the engine)."""
    if trail.startswith("sma"):
        n = int(trail[3:])
        return (close_adj < close_adj.rolling(n, min_periods=n).mean()).fillna(False)
    if trail.startswith("low"):
        n = int(trail[3:])
        return (close_adj < close_adj.shift(1).rolling(n, min_periods=n).min()).fillna(False)
    return None


def market_state(qqq_close: pd.Series, rule: str) -> pd.Series:
    if rule == "none":
        return pd.Series(True, index=qqq_close.index)
    n = int(rule[3:])
    return qqq_close > qqq_close.rolling(n, min_periods=n).mean()


def eligible_universe(data: WinData) -> pd.DataFrame:
    """Weekly eligible rows. Weeks after the last universe file week (test1: after 2026-07-17) reuse that week's
    membership (flagged); relative strength is always measured at the actual Friday."""
    uni = data.universe
    ok = (uni["price_ge_10"] == "Y") & (uni["close_on_week_end"] == "Y") & uni["dv50_rank"].notna() & \
        (uni["dv50_rank"] <= 300) & uni["ff49"].notna() & uni["security_id"].isin(data.close_adj.columns)
    u = uni.loc[ok, ["week_end", "security_id", "ticker", "ff49", "ff49_name", "dv50_rank"]].copy()
    last_week = u["week_end"].max()
    fridays = pd.date_range(last_week + pd.Timedelta(days=7), pd.Timestamp(data.spec["effective_end"]), freq="W-FRI")
    fridays = [f for f in fridays if (data.sessions <= f).any() and data.sessions[data.sessions <= f][-1] > last_week]
    extra = []
    base = u[u["week_end"] == last_week]
    for f in fridays:
        extra.append(base.assign(week_end=f, carried=True))
    u["carried"] = False
    if extra:
        u = pd.concat([u] + extra, ignore_index=True)
    cs.assert_window(u["week_end"], data.spec["universe_start"], data.spec["effective_end"], "universe weeks")
    return u


def weekly_leaders(u: pd.DataFrame, data: WinData, rs_lb: int, n_groups: int | None, rs_top: float | None,
                   per_group: int | None, min_members: int = 3, n_universe: int = 300) -> pd.DataFrame:
    """Rows (week_end, security_id, rs, rs_pct, group_rank) of the leading stocks of the leading groups."""
    g = u[u["dv50_rank"] <= n_universe].copy()
    sess = data.sessions
    pos = sess.searchsorted(g["week_end"], side="right") - 1
    g = g[pos >= 0].copy()
    pos = pos[pos >= 0]
    g["session"] = sess[pos]
    cols = {s: i for i, s in enumerate(data.sig_idx.columns)}
    ci = g["security_id"].map(cols).to_numpy()
    rs_fr = (data.sig_idx / data.sig_idx.shift(rs_lb) - 1).to_numpy()
    g["rs"] = rs_fr[pos, ci]
    g = g[np.isfinite(g["rs"])].copy()
    g["rs_pct"] = g.groupby("week_end")["rs"].rank(pct=True)
    grp = g.groupby(["week_end", "ff49"]).agg(score=("rs", "median"), n=("rs", "size")).reset_index()
    grp = grp[grp["n"] >= min_members].copy()
    grp["group_rank"] = grp.groupby("week_end")["score"].rank(ascending=False, method="first")
    g = g.merge(grp[["week_end", "ff49", "group_rank"]], on=["week_end", "ff49"], how="left")
    keep = pd.Series(True, index=g.index)
    if n_groups is not None:
        keep &= g["group_rank"] <= n_groups
    if per_group is not None:
        r = g[keep].groupby(["week_end", "ff49"])["rs"].rank(ascending=False, method="first")
        keep &= g.index.isin(r[r <= per_group].index)
    if rs_top is not None:
        keep &= g["rs_pct"] > 1 - rs_top + 1e-9
    out = g[keep].sort_values(["week_end", "rs"], ascending=[True, False])
    return out[["week_end", "session", "security_id", "ticker", "ff49_name", "rs", "rs_pct", "group_rank", "carried"]]


@dataclass(frozen=True)
class Config:
    n_universe: int = 300
    rs_lb: int = 126
    n_groups: int | None = 8
    rs_top: float | None = 0.20
    per_group: int | None = None
    pivot: str = "high252"            # high252 | baseL_tT
    vol_mult: float | None = None
    k: int = 5
    tranches: tuple = (0.5, 0.5)
    add_step: float = 0.05
    stop: float | None = 0.08
    trail: str = "sma50"              # smaN | lowN | pctX
    market: str = "sma200"            # sma200 | sma50 | none
    m_exit: bool = True
    idle: str = "cash"                # cash | qqq_on
    spread_mult: float = 1.0
    account: float = 10_000.0

    @property
    def name(self) -> str:
        f = lambda x: "off" if x is None else f"{int(round(x * 100))}"  # noqa: E731
        tr = "full" if len(self.tranches) == 1 else f"{len(self.tranches)}x{f(self.add_step)}"
        return "_".join([f"U{self.n_universe}", f"RS{self.rs_lb}", f"G{self.n_groups or 'all'}",
                         f"top{f(self.rs_top)}" + (f"pg{self.per_group}" if self.per_group else ""),
                         self.pivot, f"V{self.vol_mult or 'off'}", f"K{self.k}", f"pyr{tr}", f"stop{f(self.stop)}",
                         self.trail, f"M{self.market}{'x' if self.m_exit else 'e'}", f"idle{self.idle}"])


RULE_FIELDS = [f for f in Config.__dataclass_fields__ if f not in ("spread_mult", "account")]


def simulate(cfg: Config, data: WinData, leaders: pd.DataFrame, brk: pd.DataFrame, trail_fr: pd.DataFrame | None,
             m_on: pd.Series, rank_of: dict, account: float | None = None, stops=None,
             sigma: pd.DataFrame | None = None, start: str | None = None) -> dict:
    """Daily simulation. Decisions at the close of session d, orders filled at the close of session d+1.

    Optional (defaults keep the original behaviour): ``stops`` replaces ``cfg.stop`` with a stop_rules.StopSpec (or one
    per simulated session); ``sigma`` is the 20-day return stdev frame for vol stops (computed from ``data.sig_idx``
    when needed); ``start`` starts the simulation (from cash) at a later session than ``perf_start``."""
    account = cfg.account if account is None else account
    sessions = data.sessions
    eff_end = pd.Timestamp(data.spec["effective_end"])
    first = pd.Timestamp(start or data.spec["perf_start"])
    assert first >= pd.Timestamp(data.spec["perf_start"])
    perf = sessions[(sessions >= first) & (sessions <= eff_end)]
    cs.assert_window(perf, data.spec["perf_start"], data.spec["effective_end"], "simulation sessions")
    cols = list(data.perf_idx.columns)
    col = {s: i for i, s in enumerate(cols)}
    I = data.perf_idx.reindex(perf).to_numpy()
    P = data.close.reindex(perf).to_numpy()
    B = brk.reindex(index=perf, columns=cols).to_numpy()
    T = trail_fr.reindex(index=perf, columns=cols).to_numpy() if trail_fr is not None else None
    Q = data.qqq_perf_idx.reindex(perf).to_numpy()
    QP = data.qqq_close.reindex(perf).to_numpy()
    M = m_on.reindex(perf).to_numpy().astype(bool)
    prev_session = sessions[sessions < perf[0]][-1]
    m_before = bool(m_on.loc[prev_session])
    lr = data.last_row.reindex(cols)
    lr_arr = lr.to_numpy()
    pct_trail = float(cfg.trail[3:]) / 100 if cfg.trail.startswith("pct") else None
    stop_sched = stop_rules.schedule(stops, len(perf)) if stops is not None else None
    SIG = None
    if stop_sched is not None and any(s.kind == "vol" for s in stop_sched):
        sg = stop_rules.sigma20(data.sig_idx) if sigma is None else sigma
        SIG = sg.reindex(index=perf, columns=cols).to_numpy()

    # leaders known at the close of each session: those of the latest Friday on or before it
    lead_by_week = {t: list(zip(g["security_id"], g["rs"])) for t, g in leaders.groupby("week_end")}
    weeks = sorted(lead_by_week)
    wk_pos = np.searchsorted(np.array(weeks, dtype="datetime64[ns]"), perf.values, side="right") - 1

    cash, qunits = account, 0.0
    pos: dict = {}     # sid -> dict(units, entry_i, entry_idx, peak_idx, full, filled, cost_basis)
    pend_sell: dict = {}
    pend_add: set = set()
    pend_buy: list = []
    navs, expo, costs = np.zeros(len(perf)), np.zeros(len(perf)), np.zeros(len(perf))
    traded = 0.0
    counts = {"buy": 0, "add": 0, "sell": 0, "stop": 0, "trail": 0, "market": 0, "delisted": 0, "qqq_orders": 0}
    trades, orders = [], []
    week_for_spread = None

    def hs(sid, price):
        r = rank_of.get(week_for_spread, {}).get(sid, np.nan) if week_for_spread is not None else np.nan
        return rank_half_spread(r, price, cfg.spread_mult)

    for i, d in enumerate(perf):
        day_cost = 0.0
        week_for_spread = weeks[wk_pos[i]] if wk_pos[i] >= 0 else None
        # ended series: the booked terminal value goes to cash, no order
        for sid in [s for s in pos if pd.notna(lr[s]) and d > lr[s]]:
            p = pos.pop(sid)
            val = p["units"] * I[i, col[sid]]
            cash += val
            counts["delisted"] += 1
            trades.append({"sid": sid, "entry": perf[p["entry_i"]], "exit": d, "reason": "delisted",
                           "ret": val / p["cost_basis"] - 1, "tranches": p["filled"], "days": i - p["entry_i"]})
            pend_sell.pop(sid, None)
            pend_add.discard(sid)

        def spend_capacity():
            return cash + (qunits * Q[i] if cfg.idle == "qqq_on" else 0.0)

        # 1. sells decided yesterday
        for sid, reason in sorted(pend_sell.items()):
            if sid not in pos:
                continue
            p = pos.pop(sid)
            c = col[sid]
            val = p["units"] * I[i, c]
            k = cs.order_cost(val, P[i, c], True, hs(sid, P[i, c]))
            cash += val - k
            day_cost += k
            traded += val
            counts["sell"] += 1
            trades.append({"sid": sid, "entry": perf[p["entry_i"]], "exit": d, "reason": reason,
                           "ret": (val - k) / p["cost_basis"] - 1, "tranches": p["filled"], "days": i - p["entry_i"]})
            orders.append({"date": d, "sid": sid, "side": "sell", "value": val, "reason": reason})
        pend_sell = {}
        # 2. pyramid adds decided yesterday (only ever after a profit)
        for sid in sorted(pend_add):
            if sid not in pos:
                continue
            p = pos[sid]
            c = col[sid]
            if not (np.isfinite(I[i, c]) and np.isfinite(P[i, c])):
                continue
            amt = min(p["full"] * cfg.tranches[p["filled"]], spend_capacity())
            if amt < 100:
                continue
            k = cs.order_cost(amt, P[i, c], False, hs(sid, P[i, c]))
            if cash < amt:      # fund from the QQQ parking position at the same close
                need = amt - cash
                qunits -= need / Q[i]
                cash += need
                kq = cs.order_cost(need, QP[i], True, max(cs.QQQ_HALF_SPREAD, 0.005 / QP[i]))
                cash -= kq
                day_cost += kq
                traded += need
            p["units"] += (amt - k) / I[i, c]
            p["filled"] += 1
            p["cost_basis"] += amt
            cash -= amt
            day_cost += k
            traded += amt
            counts["add"] += 1
            orders.append({"date": d, "sid": sid, "side": "add", "value": amt, "reason": f"tranche{p['filled']}"})
        pend_add = set()
        # 3. new entries decided yesterday (first tranche)
        nav_now = cash + qunits * Q[i] + sum(p["units"] * I[i, col[s]] for s, p in pos.items())
        for sid in pend_buy:
            if len(pos) >= cfg.k or sid in pos:
                continue
            c = col[sid]
            if not (np.isfinite(I[i, c]) and np.isfinite(P[i, c])) or (pd.notna(lr[sid]) and lr[sid] < d):
                continue
            full = nav_now / cfg.k
            amt = min(full * cfg.tranches[0], spend_capacity())
            if amt < 100:
                break
            k = cs.order_cost(amt, P[i, c], False, hs(sid, P[i, c]))
            if cash < amt:
                need = amt - cash
                qunits -= need / Q[i]
                cash += need
                kq = cs.order_cost(need, QP[i], True, max(cs.QQQ_HALF_SPREAD, 0.005 / QP[i]))
                cash -= kq
                day_cost += kq
                traded += need
            pos[sid] = {"units": (amt - k) / I[i, c], "entry_i": i, "entry_idx": I[i, c], "peak_idx": I[i, c],
                        "full": full, "filled": 1, "cost_basis": amt,
                        "sigma": SIG[i, c] if SIG is not None else np.nan}
            cash -= amt
            day_cost += k
            traded += amt
            counts["buy"] += 1
            orders.append({"date": d, "sid": sid, "side": "buy", "value": amt, "reason": "breakout"})
        pend_buy = []
        # 4. idle money: QQQ while yesterday's market read was on, cash otherwise
        m_yday = M[i - 1] if i > 0 else m_before
        if cfg.idle == "qqq_on":
            stock_val = sum(p["units"] * I[i, col[s]] for s, p in pos.items())
            qv = qunits * Q[i]
            nav = cash + qv + stock_val
            target = max(cash + qv, 0.0) if m_yday else 0.0
            delta = target - qv
            if abs(delta) > 0.02 * nav or (target == 0 and qv > 0):
                k = cs.order_cost(abs(delta), QP[i], delta < 0, max(cs.QQQ_HALF_SPREAD, 0.005 / QP[i]))
                qunits += delta / Q[i]
                cash -= delta + k
                day_cost += k
                traded += abs(delta)
                counts["qqq_orders"] += 1
        # 5. decisions at today's close (filled tomorrow)
        if i < len(perf) - 1:
            for sid, p in pos.items():
                c = col[sid]
                p["peak_idx"] = max(p["peak_idx"], I[i, c]) if np.isfinite(I[i, c]) else p["peak_idx"]
                if p["entry_i"] == i:
                    continue
                r = I[i, c] / p["entry_idx"] - 1
                if stop_sched is not None:
                    hit = stop_rules.triggered(stop_sched[i], I[i, c], p["entry_idx"], p["peak_idx"], p["sigma"])
                else:
                    hit = cfg.stop is not None and r <= -cfg.stop
                if hit:
                    pend_sell[sid] = "stop"
                    counts["stop"] += 1
                elif (T is not None and T[i, c]) or (pct_trail is not None and I[i, c] / p["peak_idx"] - 1 <= -pct_trail):
                    pend_sell[sid] = "trail"
                    counts["trail"] += 1
                elif cfg.market != "none" and cfg.m_exit and not M[i]:
                    pend_sell[sid] = "market_off"
                    counts["market"] += 1
                elif p["filled"] < len(cfg.tranches) and r >= cfg.add_step * p["filled"]:
                    pend_add.add(sid)
            if M[i] and wk_pos[i] >= 0:
                slots = cfg.k - (len(pos) - len(pend_sell))
                if slots > 0:
                    for sid, _rs in lead_by_week[weeks[wk_pos[i]]]:
                        c = col.get(sid)
                        if c is None or sid in pos or not B[i, c]:
                            continue
                        if pd.notna(lr_arr[c]) and lr_arr[c] <= d:      # no data after today: cannot be bought
                            continue
                        pend_buy.append(sid)
                        if len(pend_buy) >= slots:
                            break
        stock_val = sum(p["units"] * I[i, col[s]] for s, p in pos.items())
        navs[i] = cash + qunits * Q[i] + stock_val
        expo[i] = stock_val / navs[i] if navs[i] > 0 else 0.0
        costs[i] = day_cost
    return {"dates": perf, "nav": pd.Series(navs, index=perf), "exposure": pd.Series(expo, index=perf),
            "cost": pd.Series(costs, index=perf), "traded": traded, "counts": counts,
            "trades": pd.DataFrame(trades), "orders": pd.DataFrame(orders), "open": sorted(pos)}


def perf_metrics(nav: pd.Series, bench: pd.Series, account: float, cost: pd.Series, traded: float,
                 exposure: pd.Series) -> dict:
    # the starting value sits 7 days before the first close so that the first week and month keep their return
    anchor = nav.index[0] - pd.Timedelta(days=7)
    nav0 = pd.concat([pd.Series([account], index=[anchor]), nav])
    bn0 = pd.concat([pd.Series([account], index=[anchor]), bench])
    r, b = nav0.pct_change().dropna(), bn0.pct_change().dropna()
    years = (nav.index[-1] - nav.index[0]).days / 365.25
    cagr = (nav.iloc[-1] / account) ** (1 / years) - 1
    bcagr = (bench.iloc[-1] / account) ** (1 / years) - 1
    a = r - b
    wa = (cs.weekly(nav0).pct_change() - cs.weekly(bn0).pct_change()).dropna()
    mn, mb = nav0.resample("ME").last().dropna(), bn0.resample("ME").last().dropna()
    ma = (mn.pct_change() - mb.pct_change()).dropna()
    sd = a.std(ddof=1)
    out = {
        "start": str(nav.index[0].date()), "end": str(nav.index[-1].date()),
        "cagr": cagr, "qqq_cagr": bcagr, "excess_cagr": cagr - bcagr,
        "vol": r.std(ddof=1) * math.sqrt(252), "max_dd": max_drawdown_of_returns(r), "qqq_max_dd": max_drawdown_of_returns(b),
        "sharpe": r.mean() / r.std(ddof=1) * math.sqrt(252) if r.std() > 0 else np.nan,
        "qqq_sharpe": b.mean() / b.std(ddof=1) * math.sqrt(252),
        "ir": a.mean() / sd * math.sqrt(252) if sd > 0 else np.nan,
        "t_excess_weekly": wa.mean() / wa.std(ddof=1) * math.sqrt(len(wa)) if wa.std() > 0 else np.nan,
        "t_excess_monthly": ma.mean() / ma.std(ddof=1) * math.sqrt(len(ma)) if ma.std() > 0 else np.nan,
        "ir_weekly_per_period": wa.mean() / wa.std(ddof=1) if wa.std() > 0 else np.nan,
        "weekly_active_skew": float(wa.skew()), "weekly_active_kurt": float(wa.kurt() + 3), "weeks": len(wa),
        "months": len(ma), "tracking_error": sd * math.sqrt(252),
        "turnover_one_way_per_year": traded / 2 / nav.mean() / years,
        "cost_drag_per_year": float((cost / nav.shift(1).fillna(account)).sum() / years),
        "cost_usd_total": float(cost.sum()),
        "time_in_stocks": float((exposure > 0.05).mean()), "avg_exposure": float(exposure.mean()),
        "active_weekly_mean_ann": wa.mean() * 52,
    }
    for n in (5, 10):
        out[f"active_ann_drop_best{n}w"] = wa.drop(wa.nlargest(n).index).mean() * 52
    by = {}
    for y in sorted(set(nav.index.year)):
        m = nav.index.year == y
        prev_n = nav[nav.index.year < y].iloc[-1] if (nav.index.year < y).any() else account
        prev_b = bench[bench.index.year < y].iloc[-1] if (bench.index.year < y).any() else account
        sy, by_ = nav[m].iloc[-1] / prev_n - 1, bench[m].iloc[-1] / prev_b - 1
        by[int(y)] = {"strategy": round(float(sy), 4), "qqq": round(float(by_), 4), "excess": round(float(sy - by_), 4)}
    out["by_year"] = by
    out["years_beating_qqq"] = sum(v["excess"] > 0 for v in by.values())
    out["_monthly_active"] = ma
    return out


def trade_stats(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {"closed_trades": 0}
    w, l_ = trades[trades["ret"] > 0], trades[trades["ret"] <= 0]
    return {"closed_trades": int(len(trades)), "win_rate": float((trades["ret"] > 0).mean()),
            "avg_trade_ret": float(trades["ret"].mean()), "avg_win": float(w["ret"].mean()) if len(w) else np.nan,
            "avg_loss": float(l_["ret"].mean()) if len(l_) else np.nan,
            "median_days": float(trades["days"].median()) if "days" in trades else np.nan,
            "share_pyramided": float((trades["tranches"] > 1).mean()), "best_trade": float(trades["ret"].max()),
            "worst_trade": float(trades["ret"].min())}


class Runner:
    """Caches the frames that several configs share; runs one config on one window."""

    def __init__(self, data: WinData, exclude: frozenset = frozenset()):
        self.data = data
        self.exclude = exclude
        self.u = eligible_universe(data)
        self.rank_of = {t: dict(zip(g["security_id"], g["dv50_rank"])) for t, g in self.u.groupby("week_end")}
        self._lead, self._brk, self._trail, self._m = {}, {}, {}, {}

    def leaders(self, cfg):
        key = (cfg.n_universe, cfg.rs_lb, cfg.n_groups, cfg.rs_top, cfg.per_group)
        if key not in self._lead:
            lead = weekly_leaders(self.u, self.data, cfg.rs_lb, cfg.n_groups, cfg.rs_top, cfg.per_group,
                                  n_universe=cfg.n_universe)
            self._lead[key] = lead[~lead["security_id"].isin(self.exclude)]
        return self._lead[key]

    def brk(self, cfg):
        key = (cfg.pivot, cfg.vol_mult)
        if key not in self._brk:
            self._brk[key] = breakout_frame(self.data.close_adj, self.data.vol_adj, cfg.pivot, cfg.vol_mult)
        return self._brk[key]

    def trail(self, cfg):
        if cfg.trail not in self._trail:
            self._trail[cfg.trail] = trail_frame(self.data.close_adj, cfg.trail)
        return self._trail[cfg.trail]

    def m(self, cfg):
        if cfg.market not in self._m:
            self._m[cfg.market] = market_state(self.data.qqq_close, cfg.market)
        return self._m[cfg.market]

    def run(self, cfg: Config):
        d = self.data
        res = simulate(cfg, d, self.leaders(cfg), self.brk(cfg), self.trail(cfg), self.m(cfg), self.rank_of)
        bench = cs.qqq_benchmark(d.qqq_perf_idx, d.qqq_close, res["dates"], cfg.account)
        m = perf_metrics(res["nav"], bench, cfg.account, res["cost"], res["traded"], res["exposure"])
        m.update({f"n_{k}": v for k, v in res["counts"].items()})
        m.update(trade_stats(res["trades"]))
        return m, res, bench


def sub_metrics(nav: pd.Series, bench: pd.Series, start: str, cost: pd.Series, exposure: pd.Series,
                account: float = 10_000.0) -> dict:
    """Metrics on a sub-window, both series rebased to ``account`` at the last close before ``start``."""
    before = nav.index[nav.index < pd.Timestamp(start)]
    if not len(before):
        return perf_metrics(nav, bench, account, cost, 0.0, exposure)
    t0 = before[-1]
    keep = nav.index > t0
    n = nav[keep] / nav.loc[t0] * account
    b = bench[keep] / bench.loc[t0] * account
    return perf_metrics(n, b, account, cost[keep], 0.0, exposure[keep])
