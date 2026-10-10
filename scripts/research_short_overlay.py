"""Short-loser overlay on QQQ (pre-registered in docs/research_ledger_short_overlay.md, section 0).

A margin account holds QQQ (100% of equity, or 100% + k for the market-neutral variant) and shorts the N worst names
of a monthly "loser" ranking in the data-v2 U300 universe, total short notional k of equity. Four loser signals
(GBRT walk-forward scores of research_ml_cross_section, the profitability composite, a Stambaugh-Yu-Yuan style
mispricing composite, 12-1 momentum). Daily simulation with whole-share shorts, IBKR Tiered commissions and fees,
half-spreads, tiered borrow fees, IBKR margin-loan and credit-interest rules, Reg T initial / maintenance margin with
forced liquidation, and terminal values for delisted shorts. Judged against ONEQ total return on two folds.

Usage:
  REVERSAL_DATA_VERSION=v2 PYTHONPATH=. .venv/bin/python scripts/research_short_overlay.py --check
  REVERSAL_DATA_VERSION=v2 PYTHONPATH=. .venv/bin/python scripts/research_short_overlay.py --register
  REVERSAL_DATA_VERSION=v2 PYTHONPATH=. .venv/bin/python scripts/research_short_overlay.py --run
"""
from __future__ import annotations

import os

os.environ.setdefault("REVERSAL_DATA_VERSION", "v2")      # the study is defined on frozen data v2 only

import argparse  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from dataclasses import dataclass, field, replace  # noqa: E402
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import research_fundamentals as rf  # noqa: E402
from scripts import research_megacap as mc  # noqa: E402
from scripts import research_reversal_dev as rev  # noqa: E402

OUT = ROOT / "output/research_only/short_overlay"
LEDGER = ROOT / "docs/research_ledger_short_overlay.md"
FROZEN = OUT / "frozen_prereg.json"
PRED = ROOT / "output/research_only/ml_cross_section/predictions.csv.gz"
EFFR_CSV = Path("/Users/bytedance/code/quant_stocks/research_cache/leverage_methods/raw/EFFR_nyfed.csv")

SIGNALS = ("S-ML", "S-PROF", "S-MISP", "S-MOM")
NS = (10, 20)
KS = (0.20, 0.50)
MODES = ("OV", "MN")
N_TRIALS = len(SIGNALS) * len(NS) * len(KS) * len(MODES)      # 32
ALPHA_ONE_SIDED = 0.05
BUFFER_MULT = 2
ACCOUNT = 10_000.0
BAND = rev.REBALANCE_BAND        # 0.25
QQQ_BAND = 0.02                  # of equity
QQQ_HALF_SPREAD = 1e-4
MARGIN_SPREAD = 0.015            # IBKR Pro USD tier 1: BM + 1.5%
CREDIT_SPREAD = 0.005            # IBKR Pro USD: BM - 0.5% above the first $10k
CREDIT_FREE = 10_000.0           # no interest on the first $10k of cash
FULL_RATE_NAV = 100_000.0        # NAV below this: credit rate scaled by NAV / 100k; short proceeds earn nothing
DAYCOUNT = 360.0
REGT_INIT = 0.50
INIT_CAP = 0.95                  # initial requirement capped at 95% of equity
MAINT_LONG = 0.25
LIQ_SLIP = 0.005
BORROW_BIG, BORROW_REST, BORROW_HTB = 0.005, 0.02, 0.10
BIG_RANK = 100
HTB_VOL_Q = 0.90

MISP_PARTS = {"accruals": -1, "share_iss": -1, "asset_g": -1, "noa": -1, "gpa": 1, "roa": 1, "mom_12_1": 1,
              "altman_z": 1}
MISP_MIN = 4

FOLDS = {  # signal -> {fold: (first trade-day on or after, NAV end)}
    "default": {"H1": ("2012-02-01", "2018-12-31"), "H2": ("2019-01-02", "2026-08-31")},
    "S-ML": {"H1": ("2016-01-04", "2020-12-31"), "H2": ("2021-01-04", "2026-08-31")},
}

SENSITIVITIES = {
    "borrow_x2": {"borrow_mult": 2.0},
    "d5_minus100": {"idx": "d5_minus100"},
    "delist_0pct": {"idx": "delist_0pct"},
    "spread_x2": {"spread_mult": 2.0},
    "account_100k": {"account": 100_000.0},
    "account_1m": {"account": 1_000_000.0},
    "ideal": {"ideal": True},
}


def folds_for(signal: str) -> dict:
    return FOLDS.get(signal, FOLDS["default"])


# ======================================================================== signals (section 0.3)

def pct_rank(values: pd.Series, groups: pd.Series) -> pd.Series:
    """Per group percentile rank in (0, 1] with average ranks for ties; NaN stays NaN."""
    v = pd.to_numeric(values, errors="coerce").replace([np.inf, -np.inf], np.nan)
    return v.groupby(groups).rank(pct=True, method="average")


def misp_score(panel: pd.DataFrame, parts: dict = MISP_PARTS, min_parts: int = MISP_MIN) -> pd.Series:
    """Average of the anomalies' percentile ranks in their 'good' direction; low = most overpriced. Needs at least
    ``min_parts`` available anomalies."""
    ranks = pd.DataFrame({f: pct_rank(panel[f] * sgn, panel["s"]) for f, sgn in parts.items()}, index=panel.index)
    n = ranks.notna().sum(axis=1)
    return ranks.mean(axis=1).where(n >= min_parts)


def borrow_rates(panel: pd.DataFrame, big_rank: int = BIG_RANK, htb_q: float = HTB_VOL_Q) -> pd.Series:
    """Annual borrow fee per (s, security): 0.5% for the top ``big_rank`` by market cap in that month's U300,
    10% for the top decile of 3-month volatility outside them (hard-to-borrow proxy), 2% otherwise."""
    mrank = panel["mcap"].groupby(panel["s"]).rank(ascending=False, method="first")
    big = mrank <= big_rank
    vq = panel.groupby("s")["vol_3m"].transform(lambda x: x.quantile(htb_q))
    htb = (panel["vol_3m"] >= vq) & ~big
    out = pd.Series(BORROW_REST, index=panel.index)
    out[htb] = BORROW_HTB
    out[big] = BORROW_BIG
    return out


def signal_scores(panel: pd.DataFrame, pred: pd.DataFrame) -> pd.DataFrame:
    """One row per (s, security) in U300 with a 'badness' score per signal (low = worse = short candidate)."""
    p = panel[["s", "security_id", "ticker", "dv50_rank", "mcap", "vol_3m", "mom_12_1", "C_profitability"]].copy()
    p["borrow"] = borrow_rates(panel).to_numpy()
    p["S-PROF"] = panel["C_profitability"].to_numpy()
    p["S-MISP"] = misp_score(panel).to_numpy()
    p["S-MOM"] = panel["mom_12_1"].to_numpy()
    g = pred[["s", "security_id", "GBRT"]].rename(columns={"GBRT": "S-ML"})
    p = p.merge(g, on=["s", "security_id"], how="left")
    return p


def loser_orders(scores: pd.DataFrame, col: str, n: int, buffer_mult: int = BUFFER_MULT) -> dict:
    """signal -> list of the worst ``buffer_mult * n`` security ids, worst first (ties: security id)."""
    out = {}
    v = scores.dropna(subset=[col])
    for s, g in v.groupby("s", sort=True):
        if len(g) < n:
            continue
        g = g.sort_values([col, "security_id"], ascending=[True, True])
        out[s] = list(g["security_id"].head(buffer_mult * n))
    return out


def select_shorts(order: list, held: set, n: int, ok=lambda sid: True) -> list:
    """Buffered selection: names already held that are still in ``order`` (the worst 2N) are kept, the rest is
    filled from the worst down, skipping names for which ``ok(sid)`` is False (e.g. zero whole shares)."""
    out = [sid for sid in order if sid in held][:n]
    for sid in order:
        if len(out) >= n:
            break
        if sid in held:
            continue
        if ok(sid):
            out.append(sid)
    return out


# ======================================================================== market container

@dataclass
class Market:
    sessions: pd.DatetimeIndex
    sids: list
    col: dict
    idx: np.ndarray             # T x M total-return index, forward-filled (flat after the terminal value)
    close: np.ndarray           # T x M raw close, forward-filled
    booked: dict                # col -> session index where the terminal value is booked
    qqq_tr: np.ndarray          # T daily QQQ total return
    qqq_px: np.ndarray          # T QQQ raw close
    effr: np.ndarray            # T EFFR (decimal)
    idx_variants: dict = field(default_factory=dict)


def load_effr(path: Path = EFFR_CSV) -> pd.Series:
    df = pd.read_csv(path)
    df = df[df["Rate Type"] == "EFFR"]
    s = pd.Series(pd.to_numeric(df["Rate (%)"], errors="coerce").to_numpy() / 100.0,
                  index=pd.DatetimeIndex(pd.to_datetime(df["Effective Date"], format="%m/%d/%Y"))).dropna()
    return s.sort_index()


def terminal_variant(idx: pd.DataFrame, events: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Rescale each delisted security's index from its booking day on so the booked terminal return becomes the one
    of ``rule``: 'd5_minus100' (awaiting_d5 -> -100%) or 'delist_0pct' (every terminal return that is not an
    actually computed value -> 0%)."""
    out = idx.copy()
    for e in events.itertuples():
        if e.security_id not in out.columns:
            continue
        old = float(e.terminal_return)
        if rule == "d5_minus100":
            new = -1.0 if e.status == "awaiting_d5" else old
        elif rule == "delist_0pct":
            new = old if e.status == "computed" else 0.0
        else:
            raise ValueError(rule)
        if new == old:
            continue
        d = pd.Timestamp(e.booked_on)
        mask = out.index >= d
        factor = (1 + new) / (1 + old) if (1 + old) != 0 else np.nan
        if not np.isfinite(factor):            # old = -100%: rebuild from the previous level
            prev = out.loc[out.index < d, e.security_id].ffill().iloc[-1]
            out.loc[mask, e.security_id] = prev * (1 + new)
        else:
            out.loc[mask, e.security_id] = out.loc[mask, e.security_id] * factor
    return out


def build_market(data, sids: list, effr: pd.Series, qqq_tr: pd.Series) -> Market:
    sessions = data.sessions
    sids = sorted(set(sids))
    idx = data.sig_idx[sids].ffill()
    close = data.close[sids].ffill()
    ev = data.terminal_events
    ev = ev[ev["security_id"].isin(sids)] if len(ev) else ev
    col = {s: i for i, s in enumerate(sids)}
    pos = {d: i for i, d in enumerate(sessions)}
    booked = {col[e.security_id]: pos[pd.Timestamp(e.booked_on)] for e in ev.itertuples()}
    variants = {"base": idx.to_numpy(float)}
    for rule in ("d5_minus100", "delist_0pct"):
        variants[rule] = terminal_variant(idx, ev, rule).to_numpy(float)
    e = effr.reindex(effr.index.union(sessions)).ffill().reindex(sessions).fillna(0.0)
    return Market(sessions=sessions, sids=sids, col=col, idx=variants["base"], close=close.to_numpy(float),
                  booked=booked, qqq_tr=qqq_tr.reindex(sessions).fillna(0.0).to_numpy(float),
                  qqq_px=data.qqq_close.reindex(sessions).ffill().to_numpy(float), effr=e.to_numpy(float),
                  idx_variants=variants)


# ======================================================================== simulation (sections 0.4-0.5)

@dataclass(frozen=True)
class Cfg:
    signal: str = "S-PROF"
    n: int = 10
    k: float = 0.2
    mode: str = "OV"
    account: float = ACCOUNT
    borrow_mult: float = 1.0
    spread_mult: float = 1.0
    ideal: bool = False
    idx: str = "base"

    @property
    def name(self) -> str:
        return f"{self.signal}_N{self.n}_k{int(round(self.k * 100))}_{self.mode}"


def short_maint(price: float, shares: float, value: float) -> float:
    """IBKR Reg T maintenance requirement of one short stock position."""
    if price > 16.67:
        return 0.30 * value
    if price >= 5.0:
        return 5.0 * shares
    if price >= 2.5:
        return value
    return 2.5 * shares


def daily_interest(cash: float, short_mv: float, nav: float, effr: float, days: int) -> tuple[float, float]:
    """(margin interest charged, credit interest earned) for ``days`` calendar days under the IBKR rules of 0.5:
    loan = max(0, -(cash - short value)) at BM + 1.5%; free cash above $10k earns BM - 0.5% scaled by NAV / 100k;
    short proceeds earn BM - 0.5% only when NAV >= 100k."""
    own = cash - short_mv
    loan = max(0.0, -own)
    debit = loan * (effr + MARGIN_SPREAD) * days / DAYCOUNT
    cr = max(effr - CREDIT_SPREAD, 0.0)
    scale = min(1.0, max(nav, 0.0) / FULL_RATE_NAV)
    credit = max(0.0, own - CREDIT_FREE) * cr * scale * days / DAYCOUNT
    if nav >= FULL_RATE_NAV:
        credit += short_mv * cr * days / DAYCOUNT
    return debit, credit


def simulate(mk: Market, sched: list, cfg: Cfg, start: int, end: int) -> dict:
    """Daily simulation over sessions [start, end]. ``sched`` = list of (trade session index, signal date, order of
    the worst 2N ids, {sid: (dv50_rank, borrow rate)}); the first entry must trade at ``start``."""
    idx = mk.idx_variants.get(cfg.idx, mk.idx)
    close = mk.close
    trade_at = {t: (s, order, info) for t, s, order, info in sched if start <= t <= end}
    if start not in trade_at:
        raise ValueError("first schedule entry must trade at the start session")
    cash, qv = cfg.account, 0.0
    pos: dict = {}             # col -> [liability value, shares, borrow rate]
    acc = {"commission": 0.0, "fees": 0.0, "spread": 0.0, "borrow": 0.0, "margin_int": 0.0, "credit_int": 0.0,
           "short_traded": 0.0, "qqq_traded": 0.0, "orders": 0, "liquidations": 0, "zero_share_skips": 0,
           "delisted_covered": 0, "scaled_rebalances": 0, "dead": False}
    rows = []
    short_ret_log = []         # (trade t, sids) actually held after each rebalance

    def order(shares, price, sell, hs):
        c = rev.order_cost(shares, price, sell, hs) if not cfg.ideal else {"commission": 0.0, "fees": 0.0,
                                                                          "spread": 0.0, "total": 0.0}
        acc["commission"] += c["commission"]
        acc["fees"] += c["fees"]
        acc["spread"] += c["spread"]
        acc["orders"] += int(shares != 0 and not cfg.ideal)
        return c["total"]

    def short_mv():
        return sum(p[0] for p in pos.values())

    def cover(c, t, slip=0.0):
        nonlocal cash
        lv, _, _ = pos.pop(c)
        price = close[t, c]
        shares = lv / price if price > 0 else 0.0
        cost = lv * slip + order(round(shares) if not cfg.ideal else shares, price, False,
                                 rev.half_spread(np.nan, price, cfg.spread_mult))
        cash -= lv + cost
        acc["short_traded"] += lv

    def trade_qqq(target, t, slip=0.0):
        nonlocal cash, qv
        d = target - qv
        if d == 0:
            return
        px = mk.qqq_px[t]
        cost = abs(d) * slip + order(abs(d) / px, px, d < 0, QQQ_HALF_SPREAD * cfg.spread_mult)
        cash -= d + cost
        qv = target
        acc["qqq_traded"] += abs(d)

    def rebalance(t, order_ids, info):
        nonlocal cash, qv
        nav = qv + cash - short_mv()
        if nav <= 0:
            acc["dead"] = True
            return
        tgt = cfg.k * nav / cfg.n
        held = {mk.sids[c] for c in pos}

        def n_shares(sid, scale=1.0):
            c = mk.col[sid]
            p = close[t, c]
            if not np.isfinite(p) or p <= 0:
                return 0.0
            x = tgt * scale / p
            return x if cfg.ideal else float(round(x))

        def tradable(sid):
            c = mk.col.get(sid)
            if c is None or (c in mk.booked and mk.booked[c] <= t):
                return False
            ok = n_shares(sid) > 0
            if not ok:
                acc["zero_share_skips"] += 1
            return ok

        chosen = select_shorts(order_ids, held, cfg.n, tradable)
        # target shares per chosen name (continuing names inside the band keep their shares)
        plan = {}
        for sid in chosen:
            c = mk.col[sid]
            if c in pos and abs(pos[c][0] - tgt) <= BAND * tgt:
                plan[c] = pos[c][0] / close[t, c] if cfg.ideal else float(round(pos[c][0] / close[t, c]))
            else:
                plan[c] = n_shares(sid)
        s_tot = sum(plan[c] * close[t, c] for c in plan)
        extra = cfg.k * nav if cfg.mode == "MN" else 0.0
        x = extra + s_tot
        if REGT_INIT * (nav + x) > INIT_CAP * nav and x > 0:
            f = (INIT_CAP / REGT_INIT - 1.0) * nav / x
            acc["scaled_rebalances"] += 1
            extra *= f
            for c in list(plan):
                plan[c] = n_shares(mk.sids[c], f) if not cfg.ideal else plan[c] * f
        # covers first (names leaving, then size changes), then QQQ, then new shorts
        for c in list(pos):
            if c not in plan or plan[c] == 0:
                cover(c, t)
        for c, sh in plan.items():
            if sh <= 0:
                continue
            price = close[t, c]
            rate = info.get(mk.sids[c], (np.nan, BORROW_REST))[1] * cfg.borrow_mult
            rank = info.get(mk.sids[c], (np.nan, BORROW_REST))[0]
            hs = rev.half_spread(rank, price, cfg.spread_mult)
            if c in pos:
                cur_sh = pos[c][0] / price if cfg.ideal else float(round(pos[c][0] / price))
                d = sh - cur_sh
                if abs(d) > 1e-9:
                    cost = order(abs(d), price, d > 0, hs)
                    cash += d * price - cost
                    acc["short_traded"] += abs(d) * price
                    pos[c][0] += d * price
                pos[c][1], pos[c][2] = sh, rate
            else:
                cost = order(sh, price, True, hs)
                cash += sh * price - cost
                acc["short_traded"] += sh * price
                pos[c] = [sh * price, sh, rate]
        nav2 = qv + cash - short_mv()
        q_target = nav2 + extra
        if abs(q_target - qv) > QQQ_BAND * nav2 or qv == 0:
            trade_qqq(q_target, t)
        short_ret_log.append((t, [mk.sids[c] for c in pos]))

    s0, o0, i0 = trade_at[start]
    rebalance(start, o0, i0)
    rows.append((mk.sessions[start], qv + cash - short_mv(), qv, short_mv(), cash, len(pos)))
    for t in range(start + 1, end + 1):
        if acc["dead"]:
            rows.append((mk.sessions[t], 0.0, 0.0, 0.0, 0.0, 0))
            continue
        days = (mk.sessions[t] - mk.sessions[t - 1]).days
        smv = short_mv()
        nav_prev = qv + cash - smv
        if not cfg.ideal:
            debit, credit = daily_interest(cash, smv, nav_prev, mk.effr[t - 1], days)
            borrow = sum(p[0] * p[2] for p in pos.values()) * days / DAYCOUNT
            cash += credit - debit - borrow
            acc["margin_int"] += debit
            acc["credit_int"] += credit
            acc["borrow"] += borrow
        qv *= 1 + mk.qqq_tr[t]
        for c, p in list(pos.items()):
            a, b = idx[t - 1, c], idx[t, c]
            if np.isfinite(a) and np.isfinite(b) and a > 0:
                p[0] *= b / a
            if mk.booked.get(c) == t:            # terminal value booked today: cover at it, no order
                cash -= p[0]
                pos.pop(c)
                acc["delisted_covered"] += 1
        if not cfg.ideal and pos:
            eq = qv + cash - short_mv()
            req = MAINT_LONG * qv + sum(short_maint(close[t, c], p[0] / close[t, c], p[0]) for c, p in pos.items())
            if eq < req:
                acc["liquidations"] += 1
                for c in list(pos):
                    cover(c, t, LIQ_SLIP)
                eq = qv + cash
                trade_qqq(max(eq, 0.0), t, LIQ_SLIP)
        if t in trade_at:
            _, o, inf = trade_at[t]
            rebalance(t, o, inf)
        rows.append((mk.sessions[t], qv + cash - short_mv(), qv, short_mv(), cash, len(pos)))
    nav = pd.DataFrame(rows, columns=["date", "nav", "qqq", "short", "cash", "n_short"]).set_index("date")
    return {"nav": nav, "acc": acc, "held": short_ret_log}


# ======================================================================== schedules

def schedule(mk: Market, scores: pd.DataFrame, signal: str, n: int) -> list:
    """(trade session index, s, worst-2N order, {sid: (dv50_rank, borrow)}) for every signal with >= n scores."""
    orders = loser_orders(scores, signal, n)
    pos = {d: i for i, d in enumerate(mk.sessions)}
    info = {s: {r.security_id: (float(r.dv50_rank) if pd.notna(r.dv50_rank) else np.nan, float(r.borrow))
                for r in g.itertuples()} for s, g in scores.groupby("s")}
    out = []
    for s, order in orders.items():
        i = pos.get(pd.Timestamp(s))
        if i is None or i + 1 >= len(mk.sessions):
            continue
        out.append((i + 1, pd.Timestamp(s), order, info[s]))
    return out


def fold_bounds(mk: Market, sched: list, a: str, b: str) -> tuple[int, int, list]:
    entries = [e for e in sched if pd.Timestamp(a) <= mk.sessions[e[0]] <= pd.Timestamp(b)]
    start = entries[0][0]
    end = int(np.searchsorted(mk.sessions, pd.Timestamp(b), side="right") - 1)
    return start, end, entries


# ======================================================================== metrics

def month_returns(level: pd.Series) -> pd.Series:
    """Calendar-month returns of a level series; the first month is measured from the first value."""
    me = level.groupby(level.index.to_period("M")).last()
    prev = me.shift(1)
    prev.iloc[0] = level.iloc[0]
    return me / prev - 1


def tstat(x: pd.Series) -> float:
    x = pd.Series(x).dropna()
    if len(x) < 3 or x.std(ddof=1) == 0:
        return np.nan
    return float(x.mean() / x.std(ddof=1) * math.sqrt(len(x)))


def bonferroni_t(df: int, n: int = N_TRIALS, alpha: float = ALPHA_ONE_SIDED) -> float:
    lo, hi = 0.0, 10.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if rf.t_sf(mid, df) > alpha / n:
            lo = mid
        else:
            hi = mid
    return hi


def ols_alpha(y: pd.Series, x: pd.Series) -> dict:
    d = pd.DataFrame({"y": y, "x": x}).dropna()
    if len(d) < 6:
        return {"alpha_ann": np.nan, "alpha_t": np.nan, "beta": np.nan, "n": len(d)}
    X = np.column_stack([np.ones(len(d)), d["x"].to_numpy()])
    b, *_ = np.linalg.lstsq(X, d["y"].to_numpy(), rcond=None)
    e = d["y"].to_numpy() - X @ b
    s2 = (e @ e) / (len(d) - 2)
    cov = s2 * np.linalg.inv(X.T @ X)
    return {"alpha_ann": float(b[0] * 12), "alpha_t": float(b[0] / math.sqrt(cov[0, 0])), "beta": float(b[1]),
            "n": int(len(d))}


def fold_criteria(m: dict) -> dict:
    a = (m["cagr"] > m["oneq_cagr"]) and (m["t_ex_oneq"] >= 2.0)
    b = (m["dd_shallower_pp"] >= 10.0) and (m["cagr"] - m["oneq_cagr"] >= -0.03)
    return {"A": bool(a), "B": bool(b), "pass": bool(a or b)}


def fold_metrics(res: dict, oneq: pd.Series, qqq_lvl: pd.Series, account: float) -> dict:
    nav = res["nav"]["nav"]
    v = nav.where(nav > 0)
    years = (nav.index[-1] - nav.index[0]).days / 365.25
    o = oneq.reindex(nav.index)
    q = qqq_lvl.reindex(nav.index)
    cagr = (nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1 if nav.iloc[-1] > 0 else -1.0
    oc = (o.iloc[-1] / o.iloc[0]) ** (1 / years) - 1
    qc = (q.iloc[-1] / q.iloc[0]) ** (1 / years) - 1
    mr, mo, mq = month_returns(nav), month_returns(o), month_returns(q)
    ex, exq = mr - mo, mr - mq
    dd, odd = mc.max_drawdown(v.ffill().fillna(0)), mc.max_drawdown(o)
    r, rb = nav.pct_change().iloc[1:], o.pct_change().iloc[1:]
    beta = float(np.cov(r, rb, ddof=1)[0, 1] / rb.var(ddof=1))
    acc = res["acc"]
    avg_nav = float(nav.mean())
    short = res["nav"]["short"]
    m = {"start": str(nav.index[0].date()), "end": str(nav.index[-1].date()), "years": round(years, 2),
         "cagr": cagr, "oneq_cagr": oc, "qqq_cagr": qc, "excess_vs_oneq": cagr - oc, "excess_vs_qqq": cagr - qc,
         "t_ex_oneq": tstat(ex), "t_ex_qqq": tstat(exq), "ex_qqq_ann": float(exq.mean() * 12),
         "max_dd": dd, "oneq_max_dd": odd, "qqq_max_dd": mc.max_drawdown(q), "dd_shallower_pp": (abs(odd) - abs(dd)) * 100,
         "beta_vs_oneq": beta, "months": int(len(ex)),
         "borrow_pct_yr": acc["borrow"] / avg_nav / years, "margin_int_pct_yr": acc["margin_int"] / avg_nav / years,
         "credit_int_pct_yr": acc["credit_int"] / avg_nav / years,
         "trade_cost_pct_yr": (acc["commission"] + acc["fees"] + acc["spread"]) / avg_nav / years,
         "short_turnover_yr": (acc["short_traded"] / 2) / max(float(short.mean()), 1e-9) / years,
         "short_exposure": float((short / nav.where(nav > 0)).mean()),
         "avg_n_short": float(res["nav"]["n_short"].mean()),
         "orders_per_month": acc["orders"] / max(len(ex), 1),
         "liquidations": acc["liquidations"], "zero_share_skips": acc["zero_share_skips"],
         "delisted_covered": acc["delisted_covered"], "scaled_rebalances": acc["scaled_rebalances"],
         "dead": acc["dead"], "final_nav": float(nav.iloc[-1]), "start_nav": account}
    m.update(fold_criteria(m))
    return m, ex


def basket_diagnostics(mk: Market, sched: list, n: int, start: int, end: int) -> pd.DataFrame:
    """Ideal equal-weight loser basket with the 2N buffer (no rounding, no costs): one row per holding period
    (trade day -> next trade day), with the basket, QQQ and per-name returns and the average borrow rate."""
    entries = [e for e in sched if start <= e[0] <= end]
    held: set = set()
    rows = []
    for j, (t, s, order, info) in enumerate(entries):
        t1 = entries[j + 1][0] if j + 1 < len(entries) else end
        if t1 <= t:
            continue

        def ok(sid):
            c = mk.col.get(sid)
            return c is not None and not (c in mk.booked and mk.booked[c] <= t)

        chosen = select_shorts(order, held, n, ok)
        held = set(chosen)
        rets = {}
        for sid in chosen:
            c = mk.col[sid]
            a, b = mk.idx[t, c], mk.idx[t1, c]
            rets[sid] = b / a - 1 if a > 0 else np.nan
        q = float(np.prod(1 + mk.qqq_tr[t + 1:t1 + 1]) - 1)
        days = (mk.sessions[t1] - mk.sessions[t]).days
        br = float(np.mean([info.get(sid, (np.nan, BORROW_REST))[1] for sid in chosen])) if chosen else np.nan
        px = [mk.close[t, mk.col[sid]] for sid in chosen]
        rows.append({"signal": s, "t0": mk.sessions[t], "t1": mk.sessions[t1], "n": len(chosen),
                     "basket": float(np.nanmean(list(rets.values()))) if rets else np.nan, "qqq": q,
                     "borrow_rate": br, "borrow_drag": br * days / DAYCOUNT, "names": rets,
                     "median_price": float(np.median(px)) if px else np.nan, "prices": px})
    return pd.DataFrame(rows)


def leg_summary(b: pd.DataFrame) -> dict:
    d = b.dropna(subset=["basket"])
    diff = d["basket"] - d["qqq"]
    net = d["basket"] + d["borrow_drag"] - d["qqq"]        # what the short pays: basket return plus the fee
    a = ols_alpha(d["basket"], d["qqq"])
    an = ols_alpha(d["basket"] + d["borrow_drag"], d["qqq"])
    return {"months": int(len(d)), "basket_ann": float(d["basket"].mean() * 12), "qqq_ann": float(d["qqq"].mean() * 12),
            "basket_minus_qqq_ann": float(diff.mean() * 12), "t_basket_minus_qqq": tstat(diff),
            "alpha_ann": a["alpha_ann"], "alpha_t": a["alpha_t"], "beta": a["beta"],
            "borrow_ann": float(d["borrow_drag"].mean() * 12),
            "basket_plus_borrow_minus_qqq_ann": float(net.mean() * 12), "t_net": tstat(net),
            "alpha_net_ann": an["alpha_ann"], "alpha_net_t": an["alpha_t"]}


# ======================================================================== data loading

def load():
    from scripts import research_ml_cross_section as ml
    from scripts import study_data_version as dv
    if not dv.IS_V2:
        raise SystemExit("set REVERSAL_DATA_VERSION=v2")
    data, sigs, panel, oneq_lvl, _ = ml.build_panel()
    pred = pd.read_csv(PRED, dtype={"security_id": str}, parse_dates=["s"])
    panel = panel.copy()
    panel["security_id"] = panel["security_id"].astype(str)
    end = data.spec["effective_end"]
    qtr = ml.qqq_total_returns(end).reindex(data.sessions).fillna(0.0)
    qqq_lvl = (1 + qtr).cumprod()
    effr = load_effr()
    effr = effr[effr.index <= pd.Timestamp(end)]
    scores = signal_scores(panel, pred)
    mk = build_market(data, list(scores["security_id"].unique()), effr, qtr)
    for name, s in (("sessions", pd.Series(data.sessions)), ("effr", pd.Series(effr.index))):
        if pd.Series(s).max() > pd.Timestamp(end):
            raise RuntimeError(f"{name} beyond {end}")
    return data, sigs, panel, scores, mk, oneq_lvl, qqq_lvl


def coverage(scores: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for y, g in scores.groupby(scores["s"].dt.year):
        r = {"year": int(y), "avg_u300": round(len(g) / g["s"].nunique(), 1)}
        for c in SIGNALS:
            r[c] = round(float(g[c].notna().mean()), 3)
        for v, lab in ((BORROW_BIG, "borrow_0.5"), (BORROW_REST, "borrow_2"), (BORROW_HTB, "borrow_10")):
            r[lab] = round(float((g["borrow"] == v).mean()), 3)
        rows.append(r)
    return pd.DataFrame(rows)


# ======================================================================== registration

def prereg_block(text: str | None = None) -> str:
    t = text if text is not None else LEDGER.read_text()
    a, b = t.index("<!-- PREREG-BEGIN -->"), t.index("<!-- PREREG-END -->")
    return t[a:b]


def prereg_hash(text: str | None = None) -> str:
    return hashlib.sha256(prereg_block(text).encode()).hexdigest()


def register():
    OUT.mkdir(parents=True, exist_ok=True)
    if FROZEN.exists():
        raise SystemExit(f"already registered: {json.loads(FROZEN.read_text())}")
    FROZEN.write_text(json.dumps({"sha256": prereg_hash(), "registered_at": pd.Timestamp.now("UTC").isoformat()},
                                 indent=1) + "\n")
    print("registered", prereg_hash())


def check(args):
    OUT.mkdir(parents=True, exist_ok=True)
    data, sigs, panel, scores, mk, oneq, qqq = load()
    cov = coverage(scores)
    cov.to_csv(OUT / "check_coverage.csv", index=False)
    pd.set_option("display.width", 250)
    print(cov.to_string())
    for sig in SIGNALS:
        sched = schedule(mk, scores, sig, 20)
        print(sig, "signals", len(sched), "first trade", mk.sessions[sched[0][0]].date(),
              "last trade", mk.sessions[sched[-1][0]].date())
    # where do the worst names sit: average borrow tier of the worst 20 (no returns)
    for sig in SIGNALS:
        w = scores.dropna(subset=[sig]).sort_values(["s", sig]).groupby("s").head(20)
        print(sig, "worst-20 borrow tier mix", w["borrow"].value_counts(normalize=True).round(3).to_dict(),
              "median dv50 rank", float(w["dv50_rank"].median()))
    print("EFFR range", float(mk.effr.min()), float(mk.effr.max()), "terminal events in market", len(mk.booked))


# ======================================================================== run

def run(args):
    if not FROZEN.exists():
        raise SystemExit("not registered")
    if json.loads(FROZEN.read_text())["sha256"] != prereg_hash():
        raise SystemExit("pre-registration text changed since --register")
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    data, sigs, panel, scores, mk, oneq, qqq = load()
    log = []

    def say(*a):
        msg = " ".join(str(x) for x in a)
        print(msg, flush=True)
        log.append(msg)

    say("DATE GUARD:", data.guard["assertion"])
    rows, months_ex, nav_out = [], {}, []
    scheds = {(sig, n): schedule(mk, scores, sig, n) for sig in SIGNALS for n in NS}
    variants = [("main", {})] + list(SENSITIVITIES.items())
    for sig in SIGNALS:
        for n in NS:
            sched = scheds[(sig, n)]
            for k in KS:
                for mode in MODES:
                    for vname, mods in variants:
                        cfg = replace(Cfg(signal=sig, n=n, k=k, mode=mode), **mods)
                        for fold, (a, b) in folds_for(sig).items():
                            start, end, entries = fold_bounds(mk, sched, a, b)
                            res = simulate(mk, entries, cfg, start, end)
                            m, ex = fold_metrics(res, oneq, qqq, cfg.account)
                            rows.append({"config": cfg.name, "signal": sig, "n": n, "k": k, "mode": mode,
                                         "variant": vname, "fold": fold, **m})
                            if vname == "main":
                                months_ex[(cfg.name, fold)] = ex
                                nv = res["nav"][["nav", "short", "n_short"]].copy()
                                nv["config"], nv["fold"] = cfg.name, fold
                                nav_out.append(nv.reset_index())
                say(f"{sig} N{n}: done ({time.time() - t0:.0f}s)")
    summ = pd.DataFrame(rows)
    summ.to_csv(OUT / "summary.csv", index=False)
    pd.concat(nav_out, ignore_index=True).to_csv(OUT / "nav_daily.csv.gz", index=False)

    # ---- judgement
    main = summ[summ["variant"] == "main"]
    verdict = []
    for cfgname, g in main.groupby("config", sort=False):
        ex = pd.concat([months_ex[(cfgname, f)] for f in ("H1", "H2")])
        t_all = tstat(ex)
        hurdle = bonferroni_t(len(ex) - 1)
        h1, h2 = g[g["fold"] == "H1"].iloc[0], g[g["fold"] == "H2"].iloc[0]
        both = bool(h1["pass"] and h2["pass"])
        verdict.append({"config": cfgname, "H1_pass": bool(h1["pass"]), "H2_pass": bool(h2["pass"]),
                        "H1_A": bool(h1["A"]), "H1_B": bool(h1["B"]), "H2_A": bool(h2["A"]), "H2_B": bool(h2["B"]),
                        "t_pooled_vs_oneq": t_all, "p_one_sided": rf.t_sf(t_all, len(ex) - 1),
                        "bonferroni_t": hurdle, "nominal_pass": both, "pass": bool(both and t_all >= hurdle)})
    verdict = pd.DataFrame(verdict)
    verdict.to_csv(OUT / "verdict.csv", index=False)

    # ---- diagnostics: the loser basket alone
    legs, squeeze, names, feas = [], [], [], []
    tick = scores.drop_duplicates(["s", "security_id"]).set_index(["s", "security_id"])["ticker"]
    for sig in SIGNALS:
        for n in NS:
            sched = scheds[(sig, n)]
            parts = []
            for fold, (a, b) in folds_for(sig).items():
                start, end, _ = fold_bounds(mk, sched, a, b)
                bd = basket_diagnostics(mk, sched, n, start, end)
                bd["fold"] = fold
                parts.append(bd)
                legs.append({"signal": sig, "n": n, "fold": fold, **leg_summary(bd)})
            allb = pd.concat(parts, ignore_index=True)
            legs.append({"signal": sig, "n": n, "fold": "ALL", **leg_summary(allb)})
            if n == 20:
                d = allb.assign(rel=allb["basket"] - allb["qqq"]).sort_values("rel", ascending=False).head(5)
                for r in d.itertuples():
                    top = sorted(r.names.items(), key=lambda kv: -kv[1] if np.isfinite(kv[1]) else 0)[:3]
                    squeeze.append({"signal": sig, "month_start": str(r.t0.date()), "month_end": str(r.t1.date()),
                                    "basket": r.basket, "qqq": r.qqq, "basket_minus_qqq": r.rel,
                                    "top_names": "; ".join(f"{tick.get((r.signal, s_), s_)} {v:+.0%}" for s_, v in top)})
                for r in allb.itertuples():
                    for s_, v in r.names.items():
                        names.append({"signal": sig, "t0": str(r.t0.date()), "t1": str(r.t1.date()),
                                      "security_id": s_, "ret": v, "s": r.signal})
            pr = np.concatenate([np.asarray(p, float) for p in allb["prices"]]) if len(allb) else np.array([])
            p50, p90 = float(np.nanmedian(pr)), float(np.nanpercentile(pr, 90))
            for k in KS:
                per_name = k * ACCOUNT / n
                need = max(5 * p90, 350.0) * n / k
                x = per_name / pr
                rnd = np.round(x)
                err = np.abs(rnd - x)[x >= 0.5] / x[x >= 0.5]
                feas.append({"signal": sig, "n": n, "k": k, "target_per_short_at_10k": per_name,
                             "median_short_price": p50, "p90_short_price": p90,
                             "share_rounding_to_zero_at_10k": float(np.mean(x < 0.5)),
                             "mean_abs_rounding_error_at_10k": float(np.mean(err)) if len(err) else np.nan,
                             "min_commission_bp_at_10k": 0.35 / per_name * 1e4, "min_account_needed": need})
    legs = pd.DataFrame(legs)
    legs.to_csv(OUT / "short_leg.csv", index=False)
    pd.DataFrame(squeeze).to_csv(OUT / "worst_squeeze_months.csv", index=False)
    nm = pd.DataFrame(names)
    if len(nm):
        nm["ticker"] = [tick.get((s_, i), i) for s_, i in zip(nm["s"], nm["security_id"])]
        nm = nm.drop(columns=["s"]).sort_values("ret", ascending=False)
        nm.drop_duplicates(["t0", "security_id"]).head(30).to_csv(OUT / "worst_single_name_squeezes.csv", index=False)
    pd.DataFrame(feas).to_csv(OUT / "feasibility.csv", index=False)

    # ---- benchmarks per fold
    bench = []
    for key, fs in FOLDS.items():
        for fold, (a, b) in fs.items():
            o = oneq[(oneq.index >= pd.Timestamp(a)) & (oneq.index <= pd.Timestamp(b))]
            q = qqq[(qqq.index >= pd.Timestamp(a)) & (qqq.index <= pd.Timestamp(b))]
            yrs = (o.index[-1] - o.index[0]).days / 365.25
            bench.append({"folds": key, "fold": fold, "oneq_cagr": (o.iloc[-1] / o.iloc[0]) ** (1 / yrs) - 1,
                          "qqq_cagr": (q.iloc[-1] / q.iloc[0]) ** (1 / yrs) - 1,
                          "oneq_max_dd": mc.max_drawdown(o), "qqq_max_dd": mc.max_drawdown(q)})
    pd.DataFrame(bench).to_csv(OUT / "benchmarks.csv", index=False)
    res = {"prereg_sha256": prereg_hash(), "n_trials": N_TRIALS, "verdict": "PASS" if verdict["pass"].any() else "FAIL",
           "n_pass": int(verdict["pass"].sum()), "n_nominal_pass": int(verdict["nominal_pass"].sum()),
           "bonferroni_t_typical": float(verdict["bonferroni_t"].median()),
           "guard": data.guard["assertion"], "runtime_s": round(time.time() - t0, 1)}
    (OUT / "results.json").write_text(json.dumps(res, indent=1, default=str) + "\n")
    say(json.dumps(res, default=str))
    (OUT / "run_log.txt").write_text("\n".join(log) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--register", action="store_true")
    g.add_argument("--run", action="store_true")
    args = ap.parse_args(argv)
    if args.check:
        check(args)
    elif args.register:
        register()
    else:
        run(args)


if __name__ == "__main__":
    main()
