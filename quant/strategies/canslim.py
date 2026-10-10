"""CAN SLIM (O'Neil) weekly stock screen and its portfolio engine, development window 2017-2022.

Extracted unchanged from scripts/research_canslim_dev.py (the single loader ``load_dev_data``, the signals
``price_features`` / ``market_filter`` / ``build_features``, the rule ``Config`` / ``screen`` / ``signal_schedule``, the
engine ``simulate``, ``perf_metrics`` / ``qqq_benchmark`` / ``weekly`` and the date guard ``assert_window`` /
``truncate`` with this study's defaults). Used by studies/canslim_dev.py and, through ``stops=``, by
studies/stops.py; livermore / indicators / mean_reversion / earnings_events / osap_backtest / sec_alt / fundamentals
reuse the guard, the QQQ benchmark and the order cost. Point-in-time EPS: quant.data.eps.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from quant.backtest import stop_rules
from quant.backtest.costs import ibkr_order_cost, rank_half_spread
from quant.data import version as dv
from quant.data.eps import attach_eps
from quant.data.panel import TERMINAL_D5, make_index
from quant.evaluation.metrics import max_drawdown_of_returns

DEV_START = "2017-01-01"
DEV_END = "2022-12-31"
PRICE_START = "2015-10-01"     # signal warm-up only (52-week high, 12-month RS, 200-day MA)
UNIVERSE_START = "2016-01-01"
FIRST_SIGNAL = "2016-12-30"    # its trades execute at the close of 2017-01-03
INPUTS = dv.INPUTS
CACHE = dv.CACHE
QQQ_HALF_SPREAD = 0.0001
CASH_RATE = 0.0   # IBKR pays no interest on the first $10,000 of cash


class DateGuardError(AssertionError):
    pass


def assert_window(dates, start: str | None = None, end: str = DEV_END, what: str = "dates") -> None:
    """Raise when any date is after ``end`` (or before ``start``)."""
    values = pd.to_datetime(pd.Series(list(dates)) if not isinstance(dates, (pd.Series, pd.Index)) else dates)
    values = values.dropna()
    if not len(values):
        return
    if values.max() > pd.Timestamp(end):
        raise DateGuardError(f"{what}: {values.max().date()} is after {end}")
    if start is not None and values.min() < pd.Timestamp(start):
        raise DateGuardError(f"{what}: {values.min().date()} is before {start}")


def truncate(frame: pd.DataFrame, column: str, start: str, end: str = DEV_END) -> pd.DataFrame:
    s = frame[column].astype(str)
    out = frame.loc[(s >= start) & (s <= end)].copy()
    assert_window(out[column], start, end, column)
    return out


@dataclass
class DevData:
    sessions: pd.DatetimeIndex        # PRICE_START .. DEV_END
    universe: pd.DataFrame
    sig_idx: pd.DataFrame             # total-return index for signals (warm-up included)
    perf_idx: pd.DataFrame            # total-return index from PERF_START only (earlier returns masked)
    close: pd.DataFrame               # raw close, forward filled (order sizing only)
    close_adj: pd.DataFrame           # split-adjusted close (signals)
    vol_adj: pd.DataFrame             # split-adjusted volume (signals)
    last_row: pd.Series
    qqq_close: pd.Series
    qqq_perf_idx: pd.Series
    terminal_events: pd.DataFrame
    guard: dict


def load_dev_data(terminal_awaiting: float = TERMINAL_D5) -> DevData:
    guard = {"dev_start": DEV_START, "dev_end": DEV_END, "price_start_signals_only": PRICE_START, "frames": {}}

    def note(name, frame, column, start):
        out = truncate(frame, column, start)
        guard["frames"][name] = {"rows_kept": int(len(out)),
                                 "rows_dropped_after_end": int((frame[column].astype(str) > DEV_END).sum()),
                                 "rows_dropped_before_start": int((frame[column].astype(str) < start).sum()),
                                 "min_date": str(out[column].min()), "max_date": str(out[column].max())}
        return out

    uni = pd.read_csv(INPUTS / "weekly_universe_top300.csv.gz", dtype=str)
    uni = note("weekly_universe_top300", uni, "week_end", UNIVERSE_START)
    uni["dv50_rank"] = pd.to_numeric(uni["dv50_rank"], errors="coerce")
    uni["week_end"] = pd.to_datetime(uni["week_end"])

    panel = pd.read_csv(CACHE / "prices/daily_panel.csv.gz", dtype={"security_id": str, "date": str},
                        usecols=["security_id", "date", "close_raw", "volume_raw", "split_factor", "tr"])
    qqq = pd.read_csv(CACHE / "factors/qqq_joined.csv", dtype={"date": str})
    term = pd.read_csv(INPUTS / "terminal_returns_2012_2026.csv", dtype=str)
    term = term[term["last_price_date"].notna()]
    term = note("terminal_returns (by last_price_date)", term, "last_price_date", PRICE_START)

    ids = set(uni["security_id"])
    successor = {r.security_id: r.continued_as for r in term.itertuples() if isinstance(r.continued_as, str)}
    ids |= {successor[s] for s in list(ids) if s in successor}
    panel = panel[panel["security_id"].isin(ids)]
    panel = note("daily_panel", panel, "date", PRICE_START)
    qqq = note("qqq_joined", qqq, "date", PRICE_START).sort_values("date")
    sessions = pd.DatetimeIndex(pd.to_datetime(qqq["date"]))
    qqq_close = pd.Series(qqq["close"].to_numpy(float), index=sessions)
    qqq_tr = pd.Series(((qqq["close"] + qqq["dividend"].fillna(0)) / qqq["close"].shift(1) - 1).to_numpy(), index=sessions)

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
        if status == "computed":
            value = float(r.terminal_return)
        elif status == "awaiting_d5":
            value = terminal_awaiting
        else:
            value = 0.0
        tr.loc[nxt, sid] = value
        events.append({"security_id": sid, "ticker": getattr(r, "ticker", ""), "last_row": lr.date().isoformat(),
                       "booked_on": nxt.date().isoformat(), "status": status, "terminal_return": value})
    events = pd.DataFrame(events)

    cum_split = split.cumprod()
    close_adj = close * cum_split
    vol_adj = vol / cum_split
    sig_idx = make_index(tr, close)
    perf_start = sessions[sessions >= pd.Timestamp(DEV_START)][0]
    tr_perf = tr.copy()
    tr_perf.loc[tr_perf.index <= perf_start] = np.nan       # positions start at the close of the first 2017 session
    started = close.notna().mul(np.asarray(close.index >= perf_start), axis=0).cumsum() > 0
    perf_idx = (1 + tr_perf.fillna(0.0)).cumprod().where(started)
    qtr = qqq_tr.copy()
    qtr[qtr.index <= perf_start] = np.nan
    qqq_perf_idx = (1 + qtr.fillna(0.0)).cumprod()
    assert_window(tr_perf.dropna(how="all").index, perf_start.strftime("%Y-%m-%d"), DEV_END, "performance returns")
    for name, fr in (("tr", tr), ("close", close), ("volume", vol), ("qqq", qqq_close)):
        assert_window(fr.index, PRICE_START, DEV_END, name)
    guard["perf_start_session"] = str(perf_start.date())
    guard["max_session_loaded"] = str(sessions.max().date())
    guard["assertion"] = (f"PASS: every price/return frame lies in [{PRICE_START}, {DEV_END}]; performance returns are "
                          f"masked before {perf_start.date()} (first return used: the session after it)")
    return DevData(sessions=sessions, universe=uni, sig_idx=sig_idx, perf_idx=perf_idx, close=close.ffill(),
                   close_adj=close_adj, vol_adj=vol_adj, last_row=last_row, qqq_close=qqq_close,
                   qqq_perf_idx=qqq_perf_idx, terminal_events=events, guard=guard)


def price_features(close_adj: pd.DataFrame, vol_adj: pd.DataFrame, sig_idx: pd.DataFrame) -> dict:
    """Wide signal frames (sessions x securities)."""
    high = close_adj.rolling(252, min_periods=200).max()
    avg50 = vol_adj.rolling(50, min_periods=40).mean().shift(1)
    up = close_adj > close_adj.shift(1)
    ratio = (vol_adj / avg50).where(up)
    out = {"off_high": 1 - close_adj / high, "vol_ratio5": ratio.rolling(5, min_periods=1).max()}
    for lb, sk in ((252, 21), (126, 21), (252, 0)):
        out[f"rs_{lb}_{sk}"] = sig_idx.shift(sk) / sig_idx.shift(lb) - 1
    return out


def market_filter(qqq_close: pd.Series) -> pd.DataFrame:
    return pd.DataFrame({"sma50": qqq_close > qqq_close.rolling(50).mean(),
                         "sma200": qqq_close > qqq_close.rolling(200).mean()})


def build_features(data: DevData, states: dict, first_signal: str = FIRST_SIGNAL) -> pd.DataFrame:
    pf = price_features(data.close_adj, data.vol_adj, data.sig_idx)
    uni = data.universe
    ok = (uni["price_ge_10"] == "Y") & (uni["close_on_week_end"] == "Y") & (uni["foreign_filer"] != "Y") & \
        uni["dv50_rank"].notna() & uni["security_id"].isin(data.close_adj.columns) & \
        (uni["week_end"] >= pd.Timestamp(first_signal))
    u = uni.loc[ok, ["week_end", "security_id", "ticker", "cik", "dv50_rank"]].copy()
    sess = data.sessions
    pos = sess.searchsorted(u["week_end"], side="right") - 1
    u["session"] = sess[pos]
    assert (u["session"] <= u["week_end"]).all()
    cols = {sid: i for i, sid in enumerate(data.close_adj.columns)}
    ci = u["security_id"].map(cols).to_numpy()
    for name, fr in pf.items():
        u[name] = fr.to_numpy()[pos, ci]
    feat = attach_eps(u, states["filed"])
    if "d0" in states:
        feat = attach_eps(feat, states["d0"], prefix="d0_")
    return feat


@dataclass(frozen=True)
class Config:
    n_universe: int = 300
    c_min: float | None = 0.25
    a_rule: str = "up3"            # up3 | cagr25 | off
    n_within: float | None = 0.15
    rs_lookback: int = 252
    rs_skip: int = 21
    rs_top: float | None = 0.20
    s_min: float | None = None
    m_rule: str = "sma50"          # none | sma50 | sma200 | sma50_200
    idle: str = "cash"             # cash | qqq
    core: float = 0.0
    k: int = 8
    rebalance: str = "weekly"      # weekly | monthly
    stop: float | None = 0.08
    profit: float | None = None
    exit_on_fail: bool = True
    eps_timing: str = "filed"      # filed | d0
    spread_mult: float = 1.0
    account: float = 10_000.0

    @property
    def name(self) -> str:
        f = lambda x: "off" if x is None else f"{int(round(x * 100))}"  # noqa: E731
        parts = [f"U{self.n_universe}", f"C{f(self.c_min)}", f"A{self.a_rule}", f"N{f(self.n_within)}",
                 f"L{self.rs_lookback}s{self.rs_skip}t{f(self.rs_top)}", f"S{'off' if self.s_min is None else self.s_min}",
                 f"M{self.m_rule}", f"idle{self.idle}", f"core{f(self.core)}", f"K{self.k}", self.rebalance[0].upper(),
                 f"stop{f(self.stop)}", f"pt{f(self.profit)}", "xfail" if self.exit_on_fail else "xhold"]
        if self.eps_timing != "filed":
            parts.append(f"eps{self.eps_timing}")
        return "_".join(parts)


def screen(feat: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Rows of ``feat`` passing the screen, with the RS percentile, sorted by week then RS descending."""
    g = feat[feat["dv50_rank"] <= cfg.n_universe].copy()
    rs = g[f"rs_{cfg.rs_lookback}_{cfg.rs_skip}"]
    g = g[rs.notna()].copy()
    g["rs_pct"] = g.groupby("week_end")[f"rs_{cfg.rs_lookback}_{cfg.rs_skip}"].rank(pct=True)
    p = "d0_" if cfg.eps_timing == "d0" else ""
    keep = pd.Series(True, index=g.index)
    stale_q = (g["week_end"] - pd.to_datetime(g[p + "q_end"])).dt.days
    stale_y = (g["week_end"] - pd.to_datetime(g[p + "fy0_end"])).dt.days
    if cfg.c_min is not None:
        keep &= (g[p + "c_growth"] >= cfg.c_min) & (stale_q <= 200)
    if cfg.a_rule == "up3":
        keep &= (g[p + "a_up3"].astype(str) == "True") & (stale_y <= 550)
    elif cfg.a_rule == "cagr25":
        keep &= (g[p + "a_cagr3"] >= 0.25) & (stale_y <= 550)
    if cfg.n_within is not None:
        keep &= g["off_high"] <= cfg.n_within
    if cfg.rs_top is not None:
        keep &= g["rs_pct"] > 1 - cfg.rs_top + 1e-9   # top share by rank / n
    if cfg.s_min is not None:
        keep &= g["vol_ratio5"] >= cfg.s_min
    out = g[keep]
    return out.sort_values(["week_end", "rs_pct", "security_id"], ascending=[True, False, True])


def signal_schedule(feat_weeks, sessions: pd.DatetimeIndex, m_on: pd.Series, rebalance: str,
                    start: str = DEV_START, end: str = DEV_END) -> list:
    """(week_end t, execution session e, is_rebalance, m_on) for each week; e is the next session after t."""
    weeks = sorted(pd.to_datetime(pd.Series(list(feat_weeks)).unique()))
    out = []
    for i, t in enumerate(weeks):
        later = sessions[sessions > t]
        if not len(later):
            continue
        e = later[0]
        if e < pd.Timestamp(start):
            continue
        if rebalance == "weekly":
            reb = True
        else:
            reb = i == len(weeks) - 1 or weeks[i + 1].month != t.month
        sig_session = sessions[sessions <= t][-1]
        out.append((t, e, reb, bool(m_on.loc[sig_session])))
    assert_window([e for _, e, _, _ in out], start, end, "execution sessions")
    return out


def order_cost(value: float, price: float, sell: bool, hs: float) -> float:
    if value <= 0:
        return 0.0
    return ibkr_order_cost(value / price, price, sell=sell, hs=hs)["total"]


def simulate(cfg: Config, sessions: pd.DatetimeIndex, idx: pd.DataFrame, close: pd.DataFrame, last_row: pd.Series,
             qqq_idx: pd.Series, qqq_close: pd.Series, sched: list, picks: dict, rank_of: dict,
             account: float, stops=None, sigma: pd.DataFrame | None = None, start: str = DEV_START,
             end: str = DEV_END) -> dict:
    """Daily simulation of the CAN SLIM sleeve with ``account`` dollars.

    ``picks``: week_end -> list of passing security ids, best first. ``rank_of``: week_end -> {sid: dv50 rank}.
    Positions are held as units of the total-return index (value = units x idx). Returns daily NAV etc.
    Optional (defaults keep the original behaviour): ``stops`` replaces ``cfg.stop`` with a stop_rules.StopSpec (or
    one per simulated session); ``sigma`` (20-day return stdev frame) is required for vol stops; ``start`` / ``end``
    set the simulated window.
    """
    perf = sessions[(sessions >= pd.Timestamp(start)) & (sessions <= pd.Timestamp(end))]
    stop_sched = stop_rules.schedule(stops, len(perf)) if stops is not None else None
    SIG = sigma.reindex(index=perf, columns=idx.columns).to_numpy() if sigma is not None else None
    if stop_sched is not None and any(s_.kind == "vol" for s_ in stop_sched) and SIG is None:
        raise ValueError("vol stops need ``sigma``")
    extra: dict = {}        # sid -> [peak_idx, sigma at entry] (only used with ``stops``)
    col = {s: i for i, s in enumerate(idx.columns)}
    I = idx.reindex(perf).to_numpy()
    P = close.reindex(perf).to_numpy()
    Q = qqq_idx.reindex(perf).to_numpy()
    QP = qqq_close.reindex(perf).to_numpy()
    lr = last_row.reindex(idx.columns)
    exec_map = {e: (t, reb, m) for t, e, reb, m in sched}
    cash, qunits = account, 0.0
    pos: dict = {}          # sid -> [units, entry_day_i, entry_idx]
    pending: set = set()
    navs, expo, costs = np.zeros(len(perf)), np.zeros(len(perf)), np.zeros(len(perf))
    traded = 0.0
    n_buy = n_sell = n_stop = n_profit = n_fail = n_m = n_qqq = 0
    trades = []
    last_week = None
    first_day = True

    def hs(sid, price):
        r = rank_of.get(last_week, {}).get(sid, np.nan) if last_week is not None else np.nan
        return rank_half_spread(r, price, cfg.spread_mult)

    for i, d in enumerate(perf):
        day_cost = 0.0
        if first_day:
            first_day = False
        # delisted / ended series: value (terminal return booked) moves to cash, no order
        for sid in [s for s in pos if pd.notna(lr[s]) and d > lr[s]]:
            u, ei, eidx = pos.pop(sid)
            val = u * I[i, col[sid]]
            cash += val
            trades.append({"sid": sid, "exit": d, "reason": "delisted", "ret": I[i, col[sid]] / eidx - 1})
            pending.discard(sid)

        def sell(sid, reason):
            nonlocal cash, traded, day_cost, n_sell
            u, ei, eidx = pos.pop(sid)
            c = col[sid]
            val = u * I[i, c]
            k = order_cost(val, P[i, c], True, hs(sid, P[i, c]))
            cash += val - k
            day_cost += k
            traded += val
            n_sell += 1
            trades.append({"sid": sid, "exit": d, "reason": reason, "ret": I[i, c] / eidx - 1, "days": i - ei})

        for sid in sorted(pending):
            if sid in pos:
                reason = "stop" if I[i, col[sid]] / pos[sid][2] - 1 < 0 or stop_sched is not None else "profit"
                sell(sid, "pending_" + reason)
        pending = set()
        stock_val = sum(u * I[i, col[s]] for s, (u, _, _) in pos.items())
        touched = False
        if d in exec_map:
            t, reb, m_ok = exec_map[d]
            last_week = t
            if not m_ok:
                for sid in sorted(pos):
                    sell(sid, "market_off")
                    n_m += 1
                touched = True
            elif reb:
                passing = picks.get(t, [])
                pset = set(passing)
                if cfg.exit_on_fail:
                    for sid in sorted(pos):
                        if sid not in pset:
                            sell(sid, "fail_screen")
                            n_fail += 1
                stock_val = sum(u * I[i, col[s]] for s, (u, _, _) in pos.items())
                nav = cash + qunits * Q[i] + stock_val
                size = nav / cfg.k
                avail = cash + (qunits * Q[i] if cfg.idle == "qqq" else 0.0)
                for sid in passing:
                    if len(pos) >= cfg.k:
                        break
                    c = col.get(sid)
                    if sid in pos or c is None or not np.isfinite(I[i, c]) or not np.isfinite(P[i, c]):
                        continue
                    if pd.notna(lr[sid]) and lr[sid] < d:      # series already ended (no look-ahead on future delistings)
                        continue
                    amt = min(size, avail)
                    if amt < 0.2 * size or amt < 100:
                        break
                    k = order_cost(amt, P[i, c], False, hs(sid, P[i, c]))
                    pos[sid] = [(amt - k) / I[i, c], i, I[i, c]]
                    extra[sid] = [I[i, c], SIG[i, c] if SIG is not None else np.nan]
                    cash -= amt
                    avail -= amt
                    day_cost += k
                    traded += amt
                    n_buy += 1
                touched = True
        if cfg.idle == "qqq" and (touched or cash > 0.02 * (cash + qunits * Q[i] + 1e-9)):
            stock_val = sum(u * I[i, col[s]] for s, (u, _, _) in pos.items())
            qv = qunits * Q[i]
            nav = cash + qv + stock_val
            target = max(cash + qv, 0.0)
            delta = target - qv
            if abs(delta) > 0.02 * nav or (target == 0 and qv > 0):
                k = order_cost(abs(delta), QP[i], delta < 0, max(QQQ_HALF_SPREAD, 0.005 / QP[i]))
                qunits += delta / Q[i]
                cash -= delta + k
                day_cost += k
                traded += abs(delta)
                n_qqq += 1
        # stop / profit triggers at today's close -> sell at the next session's close
        for sid, (u, ei, eidx) in pos.items():
            if stop_sched is not None and np.isfinite(I[i, col[sid]]):
                extra[sid][0] = max(extra[sid][0], I[i, col[sid]])
            if ei == i:
                continue
            r = I[i, col[sid]] / eidx - 1
            if stop_sched is not None:
                hit = stop_rules.triggered(stop_sched[i], I[i, col[sid]], eidx, extra[sid][0], extra[sid][1])
            else:
                hit = cfg.stop is not None and r <= -cfg.stop
            if hit:
                pending.add(sid)
                n_stop += 1
            elif cfg.profit is not None and r >= cfg.profit:
                pending.add(sid)
                n_profit += 1
        stock_val = sum(u * I[i, col[s]] for s, (u, _, _) in pos.items())
        cash *= 1 + CASH_RATE / 252
        navs[i] = cash + qunits * Q[i] + stock_val
        expo[i] = stock_val / navs[i] if navs[i] > 0 else 0.0
        costs[i] = day_cost
    return {"dates": perf, "nav": pd.Series(navs, index=perf), "exposure": pd.Series(expo, index=perf),
            "cost": pd.Series(costs, index=perf), "traded": traded, "n_buy": n_buy, "n_sell": n_sell,
            "n_stop": n_stop, "n_profit": n_profit, "n_fail": n_fail, "n_market_off": n_m, "n_qqq_orders": n_qqq,
            "trades": pd.DataFrame(trades)}


def weekly(series: pd.Series) -> pd.Series:
    return series.resample("W-FRI").last().dropna()


def perf_metrics(nav: pd.Series, bench: pd.Series, account: float, cost: pd.Series, traded: float,
                 exposure: pd.Series) -> dict:
    assert_window(nav.index, DEV_START, DEV_END, "performance NAV")
    r = nav.pct_change()
    r.iloc[0] = nav.iloc[0] / account - 1
    b = bench.pct_change()
    b.iloc[0] = bench.iloc[0] / account - 1
    years = (nav.index[-1] - pd.Timestamp("2016-12-30")).days / 365.25
    cagr = (nav.iloc[-1] / account) ** (1 / years) - 1
    bcagr = (bench.iloc[-1] / account) ** (1 / years) - 1
    a = r - b
    wn = weekly(pd.concat([pd.Series([account], index=[pd.Timestamp("2016-12-30")]), nav]))
    wb = weekly(pd.concat([pd.Series([account], index=[pd.Timestamp("2016-12-30")]), bench]))
    wa = (wn.pct_change() - wb.pct_change()).dropna()
    sd = a.std(ddof=1)
    out = {
        "cagr": cagr, "qqq_cagr": bcagr, "excess_cagr": cagr - bcagr,
        "vol": r.std(ddof=1) * math.sqrt(252), "max_dd": max_drawdown_of_returns(r),
        "qqq_max_dd": max_drawdown_of_returns(b),
        "sharpe": r.mean() / r.std(ddof=1) * math.sqrt(252) if r.std() > 0 else np.nan,
        "qqq_sharpe": b.mean() / b.std(ddof=1) * math.sqrt(252),
        "ir": a.mean() / sd * math.sqrt(252) if sd > 0 else np.nan,
        "t_excess_daily": a.mean() / sd * math.sqrt(len(a)) if sd > 0 else np.nan,
        "t_excess_weekly": wa.mean() / wa.std(ddof=1) * math.sqrt(len(wa)) if wa.std() > 0 else np.nan,
        "ir_weekly_per_period": wa.mean() / wa.std(ddof=1) if wa.std() > 0 else np.nan,
        "weekly_active_skew": float(wa.skew()), "weekly_active_kurt": float(wa.kurt() + 3), "weeks": len(wa),
        "tracking_error": sd * math.sqrt(252),
        "turnover_one_way_per_year": traded / 2 / nav.mean() / years,
        "cost_drag_per_year": float((cost / nav.shift(1).fillna(account)).sum() / years),
        "cost_usd_total": float(cost.sum()),
        "time_in_market": float((exposure > 0.05).mean()), "avg_exposure": float(exposure.mean()),
        "active_weekly_mean_ann": wa.mean() * 52,
    }
    for n in (5, 10):
        out[f"active_ann_drop_best{n}w"] = wa.drop(wa.nlargest(n).index).mean() * 52
    by = {}
    for y in range(2017, 2023):
        m = nav.index.year == y
        if not m.any():
            continue
        prev_n = nav[nav.index.year < y].iloc[-1] if (nav.index.year < y).any() else account
        prev_b = bench[bench.index.year < y].iloc[-1] if (bench.index.year < y).any() else account
        sy = nav[m].iloc[-1] / prev_n - 1
        by_ = bench[m].iloc[-1] / prev_b - 1
        by[y] = {"strategy": round(float(sy), 4), "qqq": round(float(by_), 4), "excess": round(float(sy - by_), 4)}
    out["by_year"] = by
    out["years_beating_qqq"] = sum(v["excess"] > 0 for v in by.values())
    return out


def qqq_benchmark(qqq_idx: pd.Series, qqq_close: pd.Series, dates: pd.DatetimeIndex, account: float) -> pd.Series:
    q = qqq_idx.reindex(dates)
    k = order_cost(account, float(qqq_close.reindex(dates).iloc[0]), False, QQQ_HALF_SPREAD)
    return (account - k) * q / q.iloc[0]
