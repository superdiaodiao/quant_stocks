"""Weekly industry-adjusted short-term reversal on a QQQ base with a long / short overlay
(docs/research_ledger_reversal_dev.md): the development-period loader (every frame truncated at the end date and
asserted, successor splicing, terminal returns), the signal, the order plan with the no-trade band, the weekly
schedule and the overlay engine.

Moved unchanged from studies/reversal_dev.py in the slimming pass (``DevData`` / ``load_dev_data``,
``week_windows``, ``industry_adjusted_signal``, ``build_signals``, ``plan_orders``, ``Config``, ``schedule``,
``simulate``). The input folders and the dates (end, warm-up start, first evaluated week end) are arguments; the
weekly metrics are quant.evaluation.events ``weekly_overlay_metrics``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from quant.backtest.costs import REBALANCE_BAND, ibkr_order_cost as order_cost, rank_half_spread as half_spread
from quant.data.guards import assert_dev_dates
from quant.data.panel import TERMINAL_D5


# ======================================================================== the date guard

def truncate_dev_strings(frame: pd.DataFrame, column: str, end: str, start: str | None = None) -> pd.DataFrame:
    """Keep rows with ``column`` <= end (and >= start), comparing the date STRINGS; assert that nothing later
    survives (quant.data.guards ``truncate_dev`` compares parsed dates and has no start)."""
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


def load_dev_data(inputs: Path, cache: Path, end: str, warmup_start: str,
                  terminal_awaiting: float = TERMINAL_D5) -> DevData:
    """The single loader: every return, price and outcome frame (``inputs`` = the data version's inputs folder,
    ``cache`` = its derived cache) is truncated at ``end`` (from ``warmup_start``) and checked."""
    guard = {"dev_end": end, "frames": {}}

    def note(name, frame, column):
        out = truncate_dev_strings(frame, column, end, warmup_start)
        guard["frames"][name] = {"rows_kept": int(len(out)), "rows_dropped_after_end": int((frame[column].astype(str) > end).sum()),
                                 "max_date": str(out[column].max())}
        return out

    uni = pd.read_csv(inputs / "weekly_universe_top300.csv.gz", dtype=str)
    uni = note("weekly_universe_top300", uni, "week_end")
    for c in ("dv50_rank", "dv20_rank", "ff49", "earnings_nearest_d0_offset"):
        uni[c] = pd.to_numeric(uni[c], errors="coerce")

    panel = pd.read_csv(cache / "prices/daily_panel.csv.gz", dtype={"security_id": str, "date": str},
                        usecols=["security_id", "date", "close_raw", "split_factor", "tr"])
    panel = note("daily_panel", panel, "date")

    qqq = pd.read_csv(cache / "factors/qqq_joined.csv", dtype={"date": str})
    qqq = note("qqq_joined", qqq, "date")
    qqq = qqq.sort_values("date")
    qqq_tr = (qqq["close"] + qqq["dividend"].fillna(0)) / qqq["close"].shift(1) - 1
    sessions = pd.DatetimeIndex(pd.to_datetime(qqq["date"]))
    qqq_tr.index = sessions

    ind = pd.read_csv(cache / "factors/ind49_daily.csv.gz", dtype={"date": str})
    ind = ind[ind["weighting"] == "vw"]
    ind = note("ind49_daily_vw", ind, "date")
    if ind["missing"].notna().any():
        raise ValueError("missing Ken French industry values inside the development window")
    ind_w = ind.pivot(index="date", columns="industry_id", values="value_dec")
    ind_w.index = pd.to_datetime(ind_w.index)
    ind_w = ind_w.reindex(sessions)

    term = pd.read_csv(inputs / "terminal_returns_2012_2026.csv", dtype=str)
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


# ======================================================================== orders

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


def schedule(data: DevData, week_ends: list, entry: str, hold_weeks: int, eval_start: str, end: str) -> list:
    """(formation week end, entry session, exit session) for each rebalance from ``eval_start``, exits inside the
    window only (asserted <= ``end``)."""
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
        if t < pd.Timestamp(eval_start) or e is None or x is None:
            continue
        assert_dev_dates([e, x], end)
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
