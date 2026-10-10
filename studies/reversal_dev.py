"""Development-period research (2012-2016 only) for the weekly, industry-adjusted short-term reversal.

Rules studied (Dai, Medhat, Novy-Marx and Rizova, NBER w30917, industry-adjusted weekly reversal):

- Signal at each week end t (the Friday, or the last session of the week): the stock's total return over
  the sessions after the previous week end up to t, minus the compounded Ken French 49-industry value-weighted
  return of its FF49 industry over the same sessions.
- Eligible: top N by dv20 or dv50 rank (``weekly_universe_top300.csv.gz``, whose ranks already require a raw
  close >= $10), ``price_ge_10`` = Y, a close on t, an FF49 code, not a foreign filer, and no earnings event
  near the formation window: ``earnings_event_within_3_sessions`` = Y (D0 within 3 sessions of t) or
  ``earnings_nearest_d0_offset`` in [-7, +3] (the 5 formation sessions t-4..t widened by 3 sessions on
  each side), so an event early in the formation week is also excluded.
- Long the K most negative signals, short the K most positive, equal target weights, held one week.
  Entry (a) at the close of t (upper bound, not tradable by hand) or (b) at the close of the next session
  (Monday). Exit at the same point one week later; continuing names are not re-traded unless the position is
  more than 25% away from its target (no-trade band).
- Implementation on a $10,000 IBKR Pro Tiered account: QQQ for 100% of equity plus a long / short overlay of
  30% / 30% (and 50% / 50% for reference). Short proceeds pay for the long leg, so there is no margin loan.

HARD RULE: only data dated on or before 2016-12-31 is used for any return, price or outcome. Every frame is
read through ``load_dev_data``, which truncates at ``DEV_END`` and asserts that no later row survives; every
entry and exit date passes ``assert_dev_dates``. Holding weeks whose exit would fall after 2016-12-31 are
dropped (the next session after the last week end is not in the truncated calendar).

Outputs: output/research_only/reversal_dev_2012_2016/{summary.json, grid.csv, extra_tries.csv, weekly/*.csv}.
The IBKR cost model, the total-return index and the statistics are in quant (quant.backtest.costs,
quant.data.panel, quant.evaluation).

Usage:  PYTHONPATH=. .venv/bin/python -m quant study reversal_dev   (or scripts/research_reversal_dev.py)
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass, field, replace

import numpy as np
import pandas as pd

from quant.backtest.costs import (  # noqa: F401  (rev.* names read by tests and the cost-model docs)
    CLEARING_FEE_PER_SHARE, EXCHANGE_FEE_PER_SHARE, FINRA_TAF_PER_SHARE_SOLD, HALF_SPREAD_BPS,
    HALF_SPREAD_UNRANKED_BPS, PASS_THROUGH_OF_COMMISSION, REBALANCE_BAND, SEC_FEE_PER_DOLLAR_SOLD,
    ibkr_order_cost as order_cost, rank_half_spread as half_spread,
)
from quant.data import version as dv
from quant.data.guards import FutureDataError
from quant.data.panel import TERMINAL_D5, TERMINAL_D5_STRESS, make_index  # noqa: F401
from quant.evaluation.criteria import bonferroni_t, weekly_deflated_sharpe as deflated_sharpe
from quant.evaluation.metrics import max_drawdown_of_returns as max_drawdown
from quant.paths import ROOT

DEV_END = "2016-12-31"
WARMUP_START = "2011-06-01"
EVAL_START = "2012-01-01"
JUDGED_YEARS = (2014, 2015, 2016)   # 2012 and 2013 exceed the 2% missing-slot rule (data report): reported only

# data version: REVERSAL_DATA_VERSION=v2 reads the v2 copies and writes <OUT>_v2 (docs/robustness_data_v2.md)
INPUTS = dv.INPUTS
CACHE = dv.CACHE
OUT = dv.versioned(ROOT / "output/research_only/reversal_dev_2012_2016")
# cost assumptions (IBKR Pro Tiered, 2012-2016): quant.backtest.costs (ibkr_order_cost, rank_half_spread)


# ======================================================================== the date guard


def assert_dev_dates(dates, end: str = DEV_END) -> None:
    """Raise when any date (string YYYY-MM-DD or Timestamp) is after the development end."""
    values = pd.to_datetime(pd.Series(list(dates)) if not isinstance(dates, (pd.Series, pd.Index)) else dates)
    if len(values) and values.max() > pd.Timestamp(end):
        raise FutureDataError(f"date {values.max().date()} is after the development end {end}")


def truncate_dev(frame: pd.DataFrame, column: str, end: str = DEV_END, start: str | None = None) -> pd.DataFrame:
    """Keep rows with ``column`` <= end (and >= start); assert that nothing later survives."""
    keep = frame[column].astype(str) <= end
    if start is not None:
        keep &= frame[column].astype(str) >= start
    out = frame.loc[keep].copy()
    assert_dev_dates(out[column], end)
    return out


@dataclass
class DevData:
    sessions: pd.DatetimeIndex
    universe: pd.DataFrame
    tr: pd.DataFrame        # sessions x security_id, total return (successor spliced, terminal booked)
    close: pd.DataFrame     # raw close
    split: pd.DataFrame     # split factor (new shares per old share), 1 elsewhere
    last_row: pd.Series     # last session with a series row (after successor splicing)
    terminal_events: pd.DataFrame
    ind: pd.DataFrame       # sessions x FF49 id, decimal vw returns
    qqq: pd.Series          # QQQ daily total return
    guard: dict = field(default_factory=dict)


def load_dev_data(end: str = DEV_END, terminal_awaiting: float = TERMINAL_D5) -> DevData:
    """The single loader: every return, price and outcome frame is truncated at ``end`` and checked."""
    guard = {"dev_end": end, "frames": {}}

    def note(name, frame, column):
        out = truncate_dev(frame, column, end, WARMUP_START)
        guard["frames"][name] = {"rows_kept": int(len(out)), "rows_dropped_after_end": int((frame[column].astype(str) > end).sum()),
                                 "max_date": str(out[column].max())}
        return out

    uni = pd.read_csv(INPUTS / "weekly_universe_top300.csv.gz", dtype=str)
    uni = note("weekly_universe_top300", uni, "week_end")
    for c in ("dv50_rank", "dv20_rank", "ff49", "earnings_nearest_d0_offset"):
        uni[c] = pd.to_numeric(uni[c], errors="coerce")

    panel = pd.read_csv(CACHE / "prices/daily_panel.csv.gz", dtype={"security_id": str, "date": str},
                        usecols=["security_id", "date", "close_raw", "split_factor", "tr"])
    panel = note("daily_panel", panel, "date")

    qqq = pd.read_csv(CACHE / "factors/qqq_joined.csv", dtype={"date": str})
    qqq = note("qqq_joined", qqq, "date")
    qqq = qqq.sort_values("date")
    qqq_tr = (qqq["close"] + qqq["dividend"].fillna(0)) / qqq["close"].shift(1) - 1
    sessions = pd.DatetimeIndex(pd.to_datetime(qqq["date"]))
    qqq_tr.index = sessions

    ind = pd.read_csv(CACHE / "factors/ind49_daily.csv.gz", dtype={"date": str})
    ind = ind[ind["weighting"] == "vw"]
    ind = note("ind49_daily_vw", ind, "date")
    if ind["missing"].notna().any():
        raise ValueError("missing Ken French industry values inside the development window")
    ind_w = ind.pivot(index="date", columns="industry_id", values="value_dec")
    ind_w.index = pd.to_datetime(ind_w.index)
    ind_w = ind_w.reindex(sessions)

    term = pd.read_csv(INPUTS / "terminal_returns_2012_2026.csv", dtype=str)
    term = term[term["last_price_date"].notna()]
    term = note("terminal_returns (by last_price_date)", term, "last_price_date")

    # securities needed: every name ranked <= 300 in the window, plus successors they continue into
    ids = set(uni["security_id"])
    successor = {r.security_id: r.continued_as for r in term.itertuples() if isinstance(r.continued_as, str)}
    ids |= {successor[s] for s in list(ids) if s in successor}
    panel = panel[panel["security_id"].isin(ids)]
    panel["date"] = pd.to_datetime(panel["date"])
    tr = panel.pivot(index="date", columns="security_id", values="tr").reindex(sessions)
    close = panel.pivot(index="date", columns="security_id", values="close_raw").reindex(sessions)
    split = panel.pivot(index="date", columns="security_id", values="split_factor").reindex(sessions).fillna(1.0)
    has_row = close.notna()
    last_row = has_row[::-1].idxmax()           # last session with a row
    last_row[~has_row.any()] = pd.NaT

    # successor splicing (1:1 reorganisations; CRSP keeps one PERMNO)
    for pred, succ in successor.items():
        if pred not in tr.columns or succ not in tr.columns or pd.isna(last_row.get(pred)):
            continue
        after = tr.index > last_row[pred]
        tr.loc[after, pred] = tr.loc[after, succ]
        close.loc[after, pred] = close.loc[after, succ]
        split.loc[after, pred] = split.loc[after, succ]
        last_row[pred] = last_row[succ]

    # terminal returns booked on the session after the last row; then the position is cash
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
        if r is not None and isinstance(r.continued_as, str) and r.continued_as in tr.columns:
            status = "continued_as_successor_ended"
        if status == "computed":
            value = float(r.terminal_return)
        elif status == "awaiting_d5":
            value = terminal_awaiting
        else:
            value = 0.0     # no value found in data version 1: exit at the last close (0%), counted below
        tr.loc[nxt, sid] = value
        events.append({"security_id": sid, "ticker": getattr(r, "ticker", ""), "last_row": lr.date().isoformat(),
                       "booked_on": nxt.date().isoformat(), "status": status, "terminal_return": value,
                       "terminal_type": getattr(r, "terminal_type", "")})
    events = pd.DataFrame(events)

    for name, frame in (("tr", tr), ("close", close), ("ind", ind_w)):
        assert_dev_dates(frame.index, end)
    assert_dev_dates(qqq_tr.index, end)
    guard["max_session_used"] = str(sessions.max().date())
    guard["assertion"] = "PASS: no row dated after %s in any return, price, industry, QQQ or terminal frame" % end
    return DevData(sessions=sessions, universe=uni, tr=tr, close=close, split=split, last_row=last_row,
                   terminal_events=events, ind=ind_w, qqq=qqq_tr, guard=guard)


# ======================================================================== signal

def week_windows(sessions: pd.DatetimeIndex, week_ends: list) -> dict:
    """Map each week end to the sessions after the previous week end up to and including it."""
    out, ends = {}, sorted(pd.to_datetime(week_ends))
    for i, t in enumerate(ends):
        prev = ends[i - 1] if i else t - pd.Timedelta(days=7)
        out[t] = sessions[(sessions > prev) & (sessions <= t)]
    return out


def industry_adjusted_signal(stock_tr: pd.DataFrame, ind_tr: pd.DataFrame, window: pd.DatetimeIndex,
                             ff49: pd.Series) -> pd.Series:
    """Compounded stock total return over ``window`` minus its FF49 industry's compounded return."""
    stock = (1 + stock_tr.loc[window, ff49.index].fillna(0.0)).prod() - 1
    industry = (1 + ind_tr.loc[window]).prod() - 1
    return stock - ff49.map(industry).astype(float)


def build_signals(data: DevData) -> pd.DataFrame:
    uni = data.universe.copy()
    week_ends = sorted(uni["week_end"].unique())
    windows = week_windows(data.sessions, week_ends)
    uni["week_end"] = pd.to_datetime(uni["week_end"])
    off = uni["earnings_nearest_d0_offset"]
    uni["earnings_excluded"] = (uni["earnings_event_within_3_sessions"] == "Y") | off.between(-7, 3)
    ok = (uni["price_ge_10"] == "Y") & (uni["close_on_week_end"] == "Y") & uni["ff49"].notna() & \
         (uni["foreign_filer"] != "Y") & ~uni["earnings_excluded"] & uni["security_id"].isin(data.tr.columns)
    uni = uni[ok]
    parts = []
    for t, g in uni.groupby("week_end"):
        ff = g.set_index("security_id")["ff49"].astype(int)
        sig = industry_adjusted_signal(data.tr, data.ind, windows[t], ff)
        parts.append(g.assign(signal=g["security_id"].map(sig).values))
    sig = pd.concat(parts)
    return sig[sig["signal"].notna()][["week_end", "security_id", "ticker", "dv20_rank", "dv50_rank", "signal"]]


# ======================================================================== costs and orders


def plan_orders(current: dict, target: dict, price: dict, band: float = REBALANCE_BAND) -> tuple[dict, list]:
    """Orders that move ``current`` (sid -> signed shares) to ``target``. A continuing position on the same side
    is left alone when within ``band`` of its target value. A side flip counts as two orders (close, then open).
    Returns the new holdings and the list of orders (sid, signed shares)."""
    new, orders = {}, []
    for sid in sorted(set(current) | set(target)):
        cur, tgt = current.get(sid, 0.0), target.get(sid, 0.0)
        if cur and tgt and np.sign(cur) == np.sign(tgt) and abs(tgt - cur) <= band * abs(tgt):
            new[sid] = cur
            continue
        if cur and tgt and np.sign(cur) != np.sign(tgt):
            orders += [(sid, -cur), (sid, tgt)]
        elif tgt - cur != 0:
            orders.append((sid, tgt - cur))
        if tgt:
            new[sid] = tgt
    return new, orders


# ======================================================================== simulation

@dataclass(frozen=True)
class Config:
    rank_var: str = "dv20"
    n_universe: int = 150
    k: int = 5
    entry: str = "monday"          # 'friday' (a) or 'monday' (b)
    overlay: float = 0.30
    account: float = 10_000.0
    hold_weeks: int = 1
    spread_mult: float = 1.0
    borrow_rate: float = 0.003
    terminal_stress: bool = False

    @property
    def name(self) -> str:
        base = f"{self.rank_var}_N{self.n_universe}_K{self.k}_{self.entry}_ov{int(round(self.overlay * 100))}"
        if self.account != 10_000:
            base += f"_acct{int(self.account / 1000)}k"
        if self.hold_weeks != 1:
            base += f"_hold{self.hold_weeks}w"
        return base


def schedule(data: DevData, week_ends: list, entry: str, hold_weeks: int) -> list:
    """(formation week end, entry session, exit session) for each rebalance, exits inside the window only."""
    ends = sorted(pd.to_datetime(week_ends))
    sessions = data.sessions

    def point(t):
        if entry == "friday":
            return t
        later = sessions[sessions > t]
        return later[0] if len(later) else None

    rows = []
    for i in range(0, len(ends) - hold_weeks, hold_weeks):
        t, t_next = ends[i], ends[i + hold_weeks]
        e, x = point(t), point(t_next)
        if t < pd.Timestamp(EVAL_START) or e is None or x is None:
            continue
        assert_dev_dates([e, x])
        rows.append((t, e, x))
    return rows


_RANK_CACHE: dict = {}


def simulate(cfg: Config, data: DevData, signals: pd.DataFrame, sched: list, tr_index: pd.DataFrame,
             keep_detail: bool = False) -> pd.DataFrame:
    rank_col = f"{cfg.rank_var}_rank"
    sig = signals[signals[rank_col] <= cfg.n_universe]
    by_week = {t: g.sort_values(["signal", "security_id"]) for t, g in sig.groupby("week_end")}
    key = (id(data), rank_col)
    if key not in _RANK_CACHE:
        u = data.universe
        _RANK_CACHE[key] = dict(zip(zip(pd.to_datetime(u["week_end"]), u["security_id"]), u[rank_col]))
        _RANK_CACHE[(id(data), "close_ff")] = data.close.ffill()
    rank_lookup = _RANK_CACHE[key]
    close_ff = _RANK_CACHE[(id(data), "close_ff")]
    idx = tr_index
    close = data.close
    nav = cfg.account
    holdings: dict = {}
    rows = []
    for t, e, x in sched:
        g = by_week.get(t)
        if g is None or len(g) < 2 * cfg.k:
            continue
        has_price = close.loc[e].notna()
        price_e = close_ff.loc[e]
        # drop positions whose series ended before this entry (cash, no order)
        holdings = {s: q for s, q in holdings.items() if data.last_row[s] >= e}
        per_name = cfg.overlay * nav / cfg.k
        tradable = g[g["security_id"].map(lambda s: bool(has_price.get(s, False)))]
        longs, shorts, target = [], [], {}
        for s in tradable["security_id"]:
            if len(longs) == cfg.k:
                break
            longs.append(s)
            target[s] = per_name / price_e[s]                      # fractional long shares (IBKR allows)
        for s in tradable["security_id"][::-1]:
            if len(shorts) == cfg.k:
                break
            if s in target:
                continue
            q = math.floor(per_name / price_e[s] + 0.5)            # whole shares for shorts
            if q < 1:
                continue
            shorts.append(s)
            target[s] = -float(q)
        new, orders = plan_orders(holdings, target, price_e.to_dict())
        cost = {"long": 0.0, "short": 0.0}
        parts = {"commission": 0.0, "fees": 0.0, "spread": 0.0}
        traded = 0.0
        n_partial = 0
        running = dict(holdings)
        for s, d in orders:
            p = price_e[s]
            hs = half_spread(rank_lookup.get((t, s), np.nan), p, cfg.spread_mult)
            c = order_cost(d, p, sell=d < 0, hs=hs)
            before = running.get(s, 0.0)
            side = "long" if (before > 0 or (before == 0 and d > 0)) else "short"
            cost[side] += c["total"]
            for k_ in parts:
                parts[k_] += c[k_]
            traded += abs(d) * p
            if before and (before + d) and np.sign(before) == np.sign(before + d):
                n_partial += 1
            running[s] = before + d
        holdings = new
        growth = idx.loc[x] / idx.loc[e]
        pnl = {"long": 0.0, "short": 0.0}
        ideal_l = np.mean([growth[s] - 1 for s in longs]) if longs else 0.0
        ideal_s = np.mean([growth[s] - 1 for s in shorts]) if shorts else 0.0
        short_value = 0.0
        for s, q in holdings.items():
            v = q * price_e[s]
            pnl["long" if q > 0 else "short"] += v * (growth[s] - 1)
            if q < 0:
                short_value += -v
        days = (x - e).days
        borrow = short_value * cfg.borrow_rate * days / 365
        cost["short"] += borrow
        r_q = float((1 + data.qqq.loc[(data.qqq.index > e) & (data.qqq.index <= x)]).prod() - 1)
        gross = pnl["long"] + pnl["short"]
        net = gross - cost["long"] - cost["short"]
        rows.append({
            "week_end": t, "entry": e, "exit": x, "nav": nav, "qqq": r_q,
            "excess_net": net / nav, "excess_gross": gross / nav,
            "excess_ideal": cfg.overlay * (ideal_l - ideal_s),
            "long_gross": pnl["long"] / nav, "short_gross": pnl["short"] / nav,
            "long_net": (pnl["long"] - cost["long"]) / nav, "short_net": (pnl["short"] - cost["short"]) / nav,
            "long_leg_ret": ideal_l, "short_leg_ret": ideal_s,
            "cost": cost["long"] + cost["short"], "commission": parts["commission"], "fees": parts["fees"],
            "spread": parts["spread"], "borrow": borrow, "orders": len(orders), "partial_orders": n_partial,
            "traded_value": traded, "gross_book": sum(abs(q) * price_e[s] for s, q in holdings.items()),
            "n_long": len(longs), "n_short": len(shorts),
            "long_ids": ";".join(longs) if keep_detail else "", "short_ids": ";".join(shorts) if keep_detail else "",
        })
        # carry holdings to the exit: value grows with total return, shares follow splits
        mask = (data.sessions > e) & (data.sessions <= x)
        sf = data.split.loc[mask]
        holdings = {s: q * float(sf[s].prod()) for s, q in holdings.items()}
        nav = nav * (1 + r_q) + net
    return pd.DataFrame(rows)


# ======================================================================== metrics


def metrics(w: pd.DataFrame, periods: float = 52.0) -> dict:
    if w.empty:
        return {}
    ex = w["excess_net"]
    n = len(ex)
    mean, sd = ex.mean(), ex.std(ddof=1)
    strat = (1 + w["qqq"] + ex).prod()
    bench = (1 + w["qqq"]).prod()
    years = n / periods
    gross_ann = w["excess_gross"].mean() * periods
    out = {
        "weeks": n,
        "excess_net_ann": mean * periods,
        "excess_net_geo_ann": (strat / bench) ** (1 / years) - 1,
        "tracking_error": sd * math.sqrt(periods),
        "ir": mean / sd * math.sqrt(periods) if sd > 0 else float("nan"),
        "t_stat": mean / sd * math.sqrt(n) if sd > 0 else float("nan"),
        "max_dd_overlay": max_drawdown(ex),
        "hit_rate": float((ex > 0).mean()),
        "excess_gross_ann": gross_ann,
        "excess_ideal_ann": w["excess_ideal"].mean() * periods,
        "t_ideal": w["excess_ideal"].mean() / w["excess_ideal"].std(ddof=1) * math.sqrt(n),
        "cost_drag_ann": gross_ann - mean * periods,
        "cost_per_year_usd_at_start": w["cost"].mean() * periods,
        "commission_share_of_cost": w["commission"].sum() / w["cost"].sum() if w["cost"].sum() else float("nan"),
        "orders_per_week": w["orders"].mean(),
        "partial_orders_per_week": w["partial_orders"].mean(),
        "turnover_traded_over_book": (w["traded_value"] / w["gross_book"]).mean(),
        "long_net_ann": w["long_net"].mean() * periods, "short_net_ann": w["short_net"].mean() * periods,
        "long_gross_ann": w["long_gross"].mean() * periods, "short_gross_ann": w["short_gross"].mean() * periods,
        "long_leg_raw_ann": w["long_leg_ret"].mean() * periods, "short_leg_raw_ann": w["short_leg_ret"].mean() * periods,
        "qqq_ann": w["qqq"].mean() * periods,
        "strategy_cagr": strat ** (1 / years) - 1, "qqq_cagr": bench ** (1 / years) - 1,
        "skew": float(ex.skew()), "kurt": float(ex.kurt() + 3),
        "sr_weekly": mean / sd if sd > 0 else float("nan"),
    }
    return out


def by_year(w: pd.DataFrame) -> dict:
    y = w.assign(year=pd.to_datetime(w["entry"]).dt.year)
    return {int(k): round(float(g["excess_net"].sum()), 4) for k, g in y.groupby("year")}


# ======================================================================== main

def run(args) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "weekly").mkdir(exist_ok=True)
    data = load_dev_data()
    print(data.guard["assertion"])
    data_stress = None
    signals = build_signals(data)
    assert_dev_dates(signals["week_end"])
    week_ends = sorted(data.universe["week_end"].unique())
    idx = make_index(data.tr, data.close)
    sched = {(e, h): schedule(data, week_ends, e, h) for e in ("friday", "monday") for h in (1, 2)}
    max_used = max(x for s in sched.values() for _, _, x in s)
    assert_dev_dates([max_used])
    print(f"date guard: latest exit used {max_used.date()} <= {DEV_END}: PASS")

    stress = load_dev_data(terminal_awaiting=TERMINAL_D5_STRESS)
    idx_stress = make_index(stress.tr, stress.close)

    rows, ledger = [], []
    grid = [Config(rank_var=rv, n_universe=n, k=k, entry=en, overlay=ov)
            for rv in ("dv20", "dv50") for n in (100, 150, 200, 250) for k in (3, 5, 10, 20)
            for en in ("friday", "monday") for ov in (0.30, 0.50)]

    def one(cfg, tag):
        w = simulate(cfg, data, signals, sched[(cfg.entry, cfg.hold_weeks)], idx, keep_detail=True)
        per = 52.0 / cfg.hold_weeks
        m = metrics(w, per)
        m_j = metrics(w[pd.to_datetime(w["entry"]).dt.year.isin(JUDGED_YEARS)], per)
        w2 = simulate(replace(cfg, spread_mult=2.0), data, signals, sched[(cfg.entry, cfg.hold_weeks)], idx)
        w3 = simulate(replace(cfg, borrow_rate=0.02), data, signals, sched[(cfg.entry, cfg.hold_weeks)], idx)
        w4 = simulate(cfg, stress, signals, sched[(cfg.entry, cfg.hold_weeks)], idx_stress)
        row = {"config": cfg.name, "tag": tag, **{k: v for k, v in asdict(cfg).items()}, **m,
               "excess_net_ann_2014_2016": m_j.get("excess_net_ann"), "t_stat_2014_2016": m_j.get("t_stat"),
               "excess_ideal_ann_2014_2016": m_j.get("excess_ideal_ann"),
               "excess_net_ann_spread2x": metrics(w2, per)["excess_net_ann"],
               "excess_net_ann_borrow2pct": metrics(w3, per)["excess_net_ann"],
               "excess_net_ann_terminal_minus100": metrics(w4, per)["excess_net_ann"],
               "by_year_net": json.dumps(by_year(w))}
        w.to_csv(OUT / "weekly" / f"{cfg.name}.csv", index=False)
        return row

    for i, cfg in enumerate(grid, 1):
        rows.append(one(cfg, "grid"))
        print(f"[{i}/{len(grid)}] {cfg.name}: net {rows[-1]['excess_net_ann']:+.2%} t {rows[-1]['t_stat']:.2f} "
              f"ideal {rows[-1]['excess_ideal_ann']:+.2%}", flush=True)
    grid_df = pd.DataFrame(rows)
    grid_df.to_csv(OUT / "grid.csv", index=False)

    # extra feasibility tries, logged separately (all count toward the multiple-testing total)
    extras = []
    base_mon = grid_df[(grid_df["entry"] == "monday") & (grid_df["overlay"] == 0.30)]
    best = base_mon.sort_values("excess_ideal_ann", ascending=False).iloc[0]
    best_cfg = Config(rank_var=best["rank_var"], n_universe=int(best["n_universe"]), k=int(best["k"]),
                      entry="monday", overlay=0.30)
    extra_cfgs = []
    for acct in (25_000, 50_000, 100_000, 250_000):
        for k in (3, 5, 10, 20):
            extra_cfgs.append(replace(best_cfg, k=k, account=float(acct)))
    for k in (3, 5, 10, 20):
        extra_cfgs.append(replace(best_cfg, k=k, hold_weeks=2))
    for cfg in extra_cfgs:
        extras.append(one(cfg, "extra"))
        print(f"[extra] {cfg.name}: net {extras[-1]['excess_net_ann']:+.2%} t {extras[-1]['t_stat']:.2f}", flush=True)
    extra_df = pd.DataFrame(extras)
    extra_df.to_csv(OUT / "extra_tries.csv", index=False)

    all_df = pd.concat([grid_df, extra_df])
    trials = (all_df["sr_weekly"]).to_numpy()
    mon = grid_df[grid_df["entry"] == "monday"]
    top = mon.sort_values("ir", ascending=False).iloc[0]
    dsr = deflated_sharpe(top["sr_weekly"], int(top["weeks"]), top["skew"], top["kurt"], trials)
    n_tried = len(all_df)
    summary = {
        "date_guard": data.guard | {"latest_exit_used": str(max_used.date())},
        "configurations_tried": {"grid": len(grid_df), "extra": len(extra_df), "total": n_tried,
                                 "sensitivity_reruns_per_config": 3},
        "bonferroni_t_one_sided_5pct": float(bonferroni_t(n_tried)),
        "best_monday_by_ir": {k: (float(v) if isinstance(v, (np.floating, float, int, np.integer)) else v)
                              for k, v in top.items()},
        "deflated_sharpe_best_monday": dsr,
        "terminal_events_in_window": data.terminal_events["status"].value_counts().to_dict(),
        "cost_assumptions": {
            "commission": "IBKR Pro Tiered $0.0035/share, min $0.35/order, max 1% of trade value",
            "exchange_fee_per_share": EXCHANGE_FEE_PER_SHARE, "clearing_fee_per_share": CLEARING_FEE_PER_SHARE,
            "pass_through_fraction_of_commission": PASS_THROUGH_OF_COMMISSION,
            "sec_fee_per_dollar_sold": SEC_FEE_PER_DOLLAR_SOLD, "finra_taf_per_share_sold": FINRA_TAF_PER_SHARE_SOLD,
            "half_spread_bps_by_rank": HALF_SPREAD_BPS, "half_spread_unranked_bps": HALF_SPREAD_UNRANKED_BPS,
            "half_spread_floor": "half a 1-cent tick / price", "rebalance_band": REBALANCE_BAND,
            "borrow": "0.3%/yr general collateral (2%/yr sensitivity)",
            "shares": "long fractional, short whole shares (rounded to nearest; a name needing <1 share is skipped)",
        },
        "judged_years": JUDGED_YEARS,
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    data.terminal_events.to_csv(OUT / "terminal_events_2012_2016.csv", index=False)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    args = parser.parse_args(argv)
    s = run(args)
    print(json.dumps(s["configurations_tried"]), "Bonferroni t:", round(s["bonferroni_t_one_sided_5pct"], 2))


if __name__ == "__main__":
    main()
