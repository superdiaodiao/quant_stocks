"""Concentrated mega-cap rules M1-M6 (frozen in docs/research_ledger_megacap.md) and their engine bindings.

``RULES`` / ``Rule`` / ``build_targets`` were extracted unchanged from studies/megacap.py; ``simulate`` /
``buy_hold`` bind the monthly index-unit engine (quant.backtest.rebalance) to the study's $10,000 account, 25% band
and half-spreads, which used to be read from that module's globals (and were overridden temporarily by
research_ml_cross_section; the half-spread is now the ``qqq_hs`` argument). Used by studies/megacap.py,
megacap_oos2 / megacap_oos3, fundamentals, ml_cross_section, index_exclusion and short_overlay.

Slimming pass (docs/architecture.md section 12), moved unchanged with the window as a parameter: the month-end
candidates and the market-cap ranking of studies/megacap.py (``candidates``, ``prepare``, ``rank_check``); the M5
trend filter with the ^NDX warm-up and the cost-free cross-check of studies/megacap_oos3.py (``trend_series``,
``apply_trend``, ``vector_gross``); the crash-window return of both out-of-sample studies (``crash_returns``).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from quant.backtest import costs
from quant.backtest.rebalance import buy_hold_level, simulate_index_units
from quant.data.benchmarks import oneq_on_sessions
from quant.data.calendar import last_session_of_each_month
from quant.data.guards import assert_window
from quant.data.market_cap import (
    add_predecessor_facts, extract_share_facts, load_company_lists, market_caps, successor_ciks,
)
from quant.data.market_cap import value_at as _value_at
from quant.data.panel import load_window
from quant.strategies import registry

ACCOUNT = 10_000.0
ONEQ_HS = 2e-4
QQQ_HS = 1e-4


@dataclass(frozen=True)
class Rule:
    name: str
    pool: int                 # top-N by market cap
    hold: int                 # names held
    mom: str | None = None    # None, "m6" (126 sessions) or "m12_1"
    trend: bool = False       # 100% QQQ when QQQ < SMA200 at the signal
    capw: bool = False        # cap-weighted instead of equal weight


RULES = tuple(Rule(name, **params) for name, params in registry.MEGACAP.items())   # M1-M6, frozen in the registry


def simulate(targets: dict, sessions: pd.DatetimeIndex, idx: pd.DataFrame, close: pd.DataFrame,
             last_row: pd.Series, qqq_idx: pd.Series, qqq_close: pd.Series, account: float = ACCOUNT,
             band: float = costs.REBALANCE_BAND, end: str | None = None, qqq_hs: float = QQQ_HS) -> dict:
    """``simulate_index_units`` with the mega-cap study's account, band and QQQ half-spread."""
    return simulate_index_units(targets, sessions, idx, close, last_row, qqq_idx, qqq_close, account=account,
                                band=band, end=end, qqq_hs=qqq_hs)


def buy_hold(level: pd.Series, price: pd.Series, dates: pd.DatetimeIndex, hs: float, account: float = ACCOUNT):
    return buy_hold_level(level, price, dates, hs, account)


def build_targets(rule: Rule, ranked: pd.DataFrame, mom: dict, qqq_close: pd.Series) -> dict:
    """signal session -> list of (sid, weight, dv50_rank, mcap, mcap_src); 'QQQ' for the trend filter's QQQ leg.
    ``ranked``: candidates with mcap, one row per company. Signals with too few names are skipped."""
    sma = qqq_close.rolling(200, min_periods=200).mean()
    out = {}
    for s, g in ranked.groupby("s"):
        g = g[g["mcap"].notna()].sort_values(["mcap", "security_id"], ascending=[False, True]).head(rule.pool)
        if len(g) < rule.pool:
            continue
        if rule.mom is not None:
            m = mom[rule.mom]
            g = g.assign(mom=_value_at(m, g["security_id"], [s] * len(g)))
            if g["mom"].isna().all():
                continue
            g = g[g["mom"].notna()].sort_values(["mom", "security_id"], ascending=[False, True]).head(rule.hold)
            if len(g) < rule.hold:
                continue
        if rule.trend:
            if not np.isfinite(sma.loc[s]):
                continue
            if qqq_close.loc[s] < sma.loc[s]:
                out[s] = [("QQQ", 1.0, np.nan, np.nan, "qqq")]
                continue
        w = (g["mcap"] / g["mcap"].sum()).to_numpy() if rule.capw else np.full(len(g), 1.0 / len(g))
        out[s] = [(r.security_id, float(wi), float(r.dv50_rank), float(r.mcap), r.mcap_src)
                  for r, wi in zip(g.itertuples(), w)]
    return out


# ======================================================================== candidates and the market-cap ranking
# (moved unchanged from studies/megacap.py; the window and its dates are parameters)

def candidates(uni: pd.DataFrame, signals: list, close: pd.DataFrame, last_row: pd.Series,
               universe_start: str, perf_end: str) -> pd.DataFrame:
    """For each signal s: the universe week with week_end <= s (latest), rows with a dv50 rank, a close at s and an
    unfinished series. One row per company (multi-class group, else CIK): the class with the best dv50 rank.
    The universe weeks used are asserted inside [universe_start, perf_end]."""
    u = uni[uni["dv50_rank"].notna() & uni["security_id"].isin(close.columns)].copy()
    weeks = np.array(sorted(u["week_end"].unique()), dtype="datetime64[ns]")
    by_week = {w: g for w, g in u.groupby("week_end")}
    rows = []
    for s in signals:
        k = weeks.searchsorted(np.datetime64(s), side="right") - 1
        if k < 0:
            continue
        g = by_week[pd.Timestamp(weeks[k])].copy()
        g["s"] = s
        g["universe_week"] = pd.Timestamp(weeks[k])
        rows.append(g)
    c = pd.concat(rows, ignore_index=True)
    px = _value_at(close, c["security_id"], c["s"])
    lr = pd.to_datetime(c["security_id"].map(last_row))
    c = c[np.isfinite(px) & (lr.isna() | (lr >= c["s"])).to_numpy()].copy()
    c["cik"] = pd.to_numeric(c["cik"], errors="coerce").astype("Int64")
    c["company"] = c["multi_class_group"].where(c["multi_class_group"].notna() & (c["multi_class_group"] != ""),
                                                c["cik"].astype(str))
    c["company"] = c["company"].where(c["company"] != "<NA>", c["security_id"])
    c = c.sort_values(["s", "company", "dv50_rank", "security_id"]).drop_duplicates(["s", "company"])
    assert_window(c["universe_week"], universe_start, perf_end, "universe weeks used")
    return c.reset_index(drop=True)


def prepare(window: str, spec: dict):
    """Load the panel window ``spec``, rank every month-end candidate by point-in-time market cap and load ONEQ.
    Returns (data, ranked, oneq_lvl, oneq_px, effective end)."""
    data = load_window(window, spec)
    end = data.spec["effective_end"]
    print("DATE GUARD:", data.guard["assertion"])
    dv50 = (data.close_adj * data.vol_adj).rolling(50, min_periods=20).median()
    sigs = last_session_of_each_month(data.sessions)
    cand = candidates(data.universe, sigs, data.close, data.last_row, spec["universe_start"], spec["perf_end"])
    ciks = cand["cik"].dropna().astype(int).unique()
    alias = successor_ciks(data.universe)
    facts = extract_share_facts(sorted(set(ciks) | set(alias)))
    facts = add_predecessor_facts(facts, alias)
    lists = load_company_lists()
    ranked = market_caps(cand[["s", "security_id", "ticker", "cik", "dv50_rank", "universe_week"]], facts, lists,
                         data.close, data.sig_idx, dv50)
    oneq_lvl, oneq_px = oneq_on_sessions(data.sessions, spec["price_start"], end)
    return data, ranked, oneq_lvl, oneq_px, end


def rank_check(ranked: pd.DataFrame) -> pd.DataFrame:
    """Top 20 by market cap at each December signal (for eyeballing against known history) + source shares."""
    rows = []
    for s, g in ranked.groupby("s"):
        g = g[g["mcap"].notna()].sort_values("mcap", ascending=False).head(20)
        for k, r in enumerate(g.itertuples(), 1):
            rows.append({"s": s.date().isoformat(), "rank": k, "ticker": r.ticker, "security_id": r.security_id,
                         "mcap_bn": round(r.mcap / 1e9, 1), "src": r.mcap_src, "dv50_rank": r.dv50_rank})
    return pd.DataFrame(rows)


# ======================================================================== M5 filter with the ^NDX warm-up (OOS.3)
# (moved unchanged from studies/megacap_oos3.py)

def trend_series(qqq_close: pd.Series, ndx_close: pd.Series, first: pd.Timestamp) -> pd.DataFrame:
    """X = QQQ close from ``first``; before it ^NDX close x (QQQ / NDX on ``first``). sma = mean of the last 200 X."""
    q = qqq_close.dropna()
    q = q[q.index >= first]
    n = ndx_close.dropna()
    k = float(q.loc[first] / n.loc[first])
    x = pd.concat([n[n.index < first] * k, q]).sort_index()
    src = pd.Series(np.where(x.index < first, "ndx", "qqq"), index=x.index)
    return pd.DataFrame({"x": x, "sma": x.rolling(200, min_periods=200).mean(), "src": src})


def apply_trend(m3_targets: dict, tr: pd.DataFrame, first_qqq_exec: pd.Timestamp, sessions: pd.DatetimeIndex):
    """M5 targets from M3's: below the SMA -> QQQ, or cash ([]) when the trade date is before QQQ's first close."""
    out, log = {}, []
    for s, lst in m3_targets.items():
        row = tr.loc[:s].iloc[-1]
        later = sessions[sessions > s]
        exec_d = later[0] if len(later) else pd.NaT
        if not np.isfinite(row["sma"]):
            continue
        above = bool(row["x"] >= row["sma"])
        if above:
            out[s], act = lst, "M3 stocks"
        elif pd.notna(exec_d) and exec_d < first_qqq_exec:
            out[s], act = [], "cash (QQQ not yet trading)"
        else:
            out[s], act = [("QQQ", 1.0, np.nan, np.nan, "qqq")], "QQQ"
        log.append({"signal": s.date().isoformat(), "sma_from": row["src"] if tr.loc[:s].tail(200)["src"].eq(row["src"]).all()
                    else "ndx+qqq", "qqq_at_or_above_sma200": above, "action": act})
    return out, pd.DataFrame(log)


def vector_gross(targets: dict, sessions: pd.DatetimeIndex, idx: pd.DataFrame, qqq: pd.Series) -> float:
    """Gross growth when each target is bought at the next close and held (weights drift) to the next trade
    (a cost-free cross-check of the engine)."""
    execs = []
    for s in sorted(targets):
        later = sessions[sessions > s]
        if len(later):
            execs.append((later[0], targets[s]))
    g = 1.0
    for (d0, tgt), (d1, _) in zip(execs, execs[1:] + [(sessions[-1], None)]):
        if not tgt:
            continue
        r = 0.0
        for sid, w, *_ in tgt:
            lv = qqq if sid == "QQQ" else idx[sid]
            r += w * (lv.loc[d1] / lv.loc[d0])
        g *= r + (1 - sum(w for _, w, *_ in tgt))
    return g


def crash_returns(series: dict, start: str, end: str) -> dict:
    """name -> return of each value series from its first to its last value inside [start, end] (megacap_oos2 /
    megacap_oos3: the 2000-03-10 .. 2002-10-09 Nasdaq crash)."""
    crash = {}
    for nm, s_ in series.items():
        v = s_[(s_.index >= pd.Timestamp(start)) & (s_.index <= pd.Timestamp(end))]
        crash[nm] = float(v.iloc[-1] / v.iloc[0] - 1)
    return crash
