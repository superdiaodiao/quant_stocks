"""Do stop-loss settings help? Static grid (post-hoc) and walk-forward stop selection (judged) on three frozen rules.

Pre-registered in docs/research_ledger_stops.md BEFORE any result was computed.
Base strategies (rules unchanged, only the stop slot is replaced by scripts/stop_rules.stop_grid()):
  livermore  Livermore #20 (research_livermore.simulate), original stop fixed 8%
  o10        indicators stock rule O10, Donchian 20/20 close version (research_indicators.stock_sim), no stop
  canslim    CAN SLIM #215 (research_canslim_dev.simulate), original stop fixed 8%
One continuous data window (frozen data version 1): prices from 2013-06-01 (warm-up), universe from 2014-01-01,
performance from 2015-01-02 to 2026-08-31. Static view: every setting from cash at 2015-01-02 and at the 2017-12-29
close. Walk-forward: each January (2018..2026) the setting with the best trailing-3-year Sharpe (secondary: Calmar,
CAGR) of the stand-alone runs, stitched into one continuous run from the 2017-12-29 close. Judged vs ONEQ.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import research_canslim_dev as cs  # noqa: E402
from scripts import research_indicators as ri  # noqa: E402
from scripts import research_livermore as lv  # noqa: E402
from scripts import stop_rules as sr  # noqa: E402

from scripts import study_data_version as dv  # noqa: E402  (REVERSAL_DATA_VERSION; docs/robustness_data_v2.md)
OUT = dv.versioned(ROOT / "output/research_only/stops")
EPS_ALL = dv.versioned(Path("/Users/bytedance/code/quant_stocks/research_cache/stops/eps_states_filed_all.csv.gz"))
ACCOUNT = 10_000.0
WINDOW = {"perf_start": "2015-01-01", "perf_end": "2026-08-31", "price_start": "2013-06-01",
          "universe_start": "2014-01-01", "judged_from": "2018-01-01"}
WF_START = "2017-12-29"                 # last close of 2017: walk-forward and comparison runs start here from cash
WF_YEARS = range(2018, 2027)
LOOKBACK = 3
PICK_METRICS = ("sharpe", "calmar", "cagr")
PRIMARY = "sharpe"
HALVES = (("2018-01-01", "2021-12-31"), ("2022-01-01", "2026-08-31"))
BONFERRONI_3 = 2.1280                   # one-sided 5% / 3 primary tests (reported only)
STRATEGIES = ("livermore", "o10", "canslim")
BASE_STOP = {"livermore": sr.StopSpec("fixed", 0.08), "o10": sr.NONE, "canslim": sr.StopSpec("fixed", 0.08)}
LIVERMORE_NAME = "U300_RS126_G8_top20_high252_Voff_K3_pyr2x5_stop8_sma50_Msma50x_idleqqq_on"
CANSLIM_NAME = "U300_C25_Acagr25_N15_L252s21t20_Soff_Msma50_idleqqq_core0_K8_M_stop8_ptoff_xfail"
FutureDataError = ri.FutureDataError


# ======================================================================== metrics (pure)

def drawdown(nav0: pd.Series) -> pd.Series:
    return nav0 / nav0.cummax() - 1


def longest_drawdown_days(nav0: pd.Series) -> tuple[int, bool]:
    """Longest peak-to-recovery span in calendar days; an unrecovered final drawdown counts to the last date.
    Returns (days, the longest one is still unrecovered)."""
    peak_v, peak_d = -np.inf, None
    best, open_best = 0, False
    in_dd = False
    for d, v in nav0.items():
        if v >= peak_v:
            if in_dd:
                span = (d - peak_d).days
                if span > best:
                    best, open_best = span, False
            peak_v, peak_d, in_dd = v, d, False
        else:
            in_dd = True
    if in_dd:
        span = (nav0.index[-1] - peak_d).days
        if span > best:
            best, open_best = span, True
    return int(best), bool(open_best)


def ulcer_index(nav0: pd.Series) -> float:
    """sqrt(mean((100 x drawdown)^2)), in percent."""
    return float(np.sqrt(np.mean((100 * drawdown(nav0)) ** 2)))


def sharpe(r: pd.Series) -> float:
    sd = r.std(ddof=1)
    return float(r.mean() / sd * math.sqrt(252)) if sd > 0 else float("nan")


def sortino(r: pd.Series) -> float:
    dd = math.sqrt(float(np.mean(np.minimum(r.to_numpy(), 0.0) ** 2)))
    return float(r.mean() / dd * math.sqrt(252)) if dd > 0 else float("nan")


def cagr_of_returns(r: pd.Series) -> float:
    if len(r) == 0:
        return float("nan")
    return float(np.prod(1 + r.to_numpy()) ** (252 / len(r)) - 1)


def mdd_of_returns(r: pd.Series) -> float:
    v = np.concatenate([[1.0], np.cumprod(1 + r.to_numpy())])
    return float((v / np.maximum.accumulate(v) - 1).min())


def calmar_of_returns(r: pd.Series) -> float:
    m = mdd_of_returns(r)
    return cagr_of_returns(r) / abs(m) if m < 0 else float("inf")


def anchored(s: pd.Series, account: float) -> pd.Series:
    """Prepend the starting value 7 days before the first close (first day / week / month keep their return)."""
    return pd.concat([pd.Series([account], index=[s.index[0] - pd.Timedelta(days=7)]), s])


def trade_metrics(trades: pd.DataFrame) -> dict:
    if trades is None or trades.empty:
        return {"closed_trades": 0, "win_rate": np.nan, "avg_win": np.nan, "avg_loss": np.nan,
                "win_loss_ratio": np.nan, "profit_factor": np.nan, "avg_hold_days": np.nan}
    r = trades["ret"].astype(float)
    w, l_ = r[r > 0], r[r <= 0]
    days = trades["days"].astype(float).dropna() if "days" in trades else pd.Series(dtype=float)
    return {"closed_trades": int(len(r)), "win_rate": float((r > 0).mean()),
            "avg_win": float(w.mean()) if len(w) else np.nan, "avg_loss": float(l_.mean()) if len(l_) else np.nan,
            "win_loss_ratio": float(w.mean() / abs(l_.mean())) if len(w) and len(l_) and l_.mean() < 0 else np.nan,
            "profit_factor": float(w.sum() / abs(l_.sum())) if len(l_) and l_.sum() < 0 else np.inf,
            "avg_hold_days": float(days.mean()) if len(days) else np.nan}


def metrics(nav: pd.Series, oneq: pd.Series, qqq: pd.Series, cost: pd.Series, traded: float,
            exposure: pd.Series, trades: pd.DataFrame | None, n_buy: int, n_stop: int,
            account: float = ACCOUNT) -> dict:
    """The full pre-registered metric set (ledger 0.6) for one daily NAV against ONEQ (and QQQ)."""
    nav0, b0, q0 = anchored(nav, account), anchored(oneq, account), anchored(qqq, account)
    r, b = nav0.pct_change().dropna(), b0.pct_change().dropna()
    years = (nav.index[-1] - nav.index[0]).days / 365.25
    cg = (nav.iloc[-1] / account) ** (1 / years) - 1
    bc = (oneq.iloc[-1] / account) ** (1 / years) - 1
    qc = (qqq.iloc[-1] / account) ** (1 / years) - 1
    mdd = float(drawdown(nav0).min())
    ldd, ldd_open = longest_drawdown_days(nav0)
    mr = nav0.resample("ME").last().pct_change().dropna()
    mb = b0.resample("ME").last().pct_change().dropna()
    mr, mb = mr.align(mb, join="inner")
    ma = mr - mb
    sd = ma.std(ddof=1)
    beta, a_m = np.polyfit(mb.to_numpy(), mr.to_numpy(), 1) if len(mr) > 2 else (np.nan, np.nan)
    by = {}
    for y in sorted(set(nav.index.year)):
        m = nav.index.year == y
        if m.sum() == 1 and nav.index[0].year == y:      # only the starting close (e.g. 2017-12-29): no return
            continue
        pn = nav[nav.index.year < y].iloc[-1] if (nav.index.year < y).any() else account
        pb = oneq[oneq.index.year < y].iloc[-1] if (oneq.index.year < y).any() else account
        pq = qqq[qqq.index.year < y].iloc[-1] if (qqq.index.year < y).any() else account
        by[int(y)] = {"strategy": float(nav[m].iloc[-1] / pn - 1), "oneq": float(oneq[m].iloc[-1] / pb - 1),
                      "qqq": float(qqq[m].iloc[-1] / pq - 1)}
    yr = pd.Series({y: v["strategy"] for y, v in by.items()})
    out = {"start": str(nav.index[0].date()), "end": str(nav.index[-1].date()), "years": years,
           "cagr": cg, "oneq_cagr": bc, "qqq_cagr": qc, "excess_oneq": cg - bc, "excess_qqq": cg - qc,
           "vol": float(r.std(ddof=1) * math.sqrt(252)), "max_dd": mdd,
           "oneq_max_dd": float(drawdown(b0).min()), "qqq_max_dd": float(drawdown(q0).min()),
           "longest_dd_days": ldd, "longest_dd_unrecovered": ldd_open, "ulcer": ulcer_index(nav0),
           "sharpe": sharpe(r), "sortino": sortino(r), "calmar": cg / abs(mdd) if mdd < 0 else np.inf,
           "oneq_sharpe": sharpe(b),
           "ir_oneq": float(ma.mean() / sd * math.sqrt(12)) if sd > 0 else np.nan,
           "t_monthly_excess": float(ma.mean() / sd * math.sqrt(len(ma))) if sd > 0 else np.nan,
           "months": int(len(ma)), "beta_oneq": float(beta), "alpha_oneq": float(a_m * 12),
           "share_years_beating_oneq": float(np.mean([v["strategy"] > v["oneq"] for v in by.values()])),
           "worst_year": float(yr.min()), "worst_year_which": int(yr.idxmin()), "worst_month": float(mr.min()),
           "buys": int(n_buy), "stop_exits": int(n_stop),
           "time_in_market": float((exposure > 0.05).mean()), "avg_exposure": float(exposure.mean()),
           "turnover_one_way_per_year": float(traded / 2 / nav.mean() / years),
           "cost_drag_per_year": float((cost / nav.shift(1).fillna(account)).sum() / years),
           "cost_usd_total": float(cost.sum())}
    out.update(trade_metrics(trades))
    out["by_year"] = by
    out["_monthly_excess"] = ma
    out["_monthly_ret"] = mr
    return out


def sub_window(res: dict, oneq: pd.Series, qqq: pd.Series, start: str, end: str) -> dict:
    """CAGR / max DD / t of a sub-window, all series rebased at the last close before ``start``."""
    nav = res["nav"]
    t0 = nav.index[nav.index < pd.Timestamp(start)][-1]
    keep = (nav.index > t0) & (nav.index <= pd.Timestamp(end))
    f = lambda s: s[keep] / s.loc[t0] * ACCOUNT  # noqa: E731
    tr = res["trades"]
    if tr is not None and len(tr):
        tr = tr[(pd.to_datetime(tr["exit"]) > t0) & (pd.to_datetime(tr["exit"]) <= pd.Timestamp(end))]
    m = metrics(f(nav), f(oneq), f(qqq), res["cost"][keep], 0.0, res["exposure"][keep], tr, 0, 0)
    return {k: m[k] for k in ("start", "end", "cagr", "oneq_cagr", "qqq_cagr", "max_dd", "oneq_max_dd", "sharpe",
                              "t_monthly_excess")}


# ======================================================================== walk-forward picking (past data only)

def trailing_window(r: pd.Series, year: int, L: int = LOOKBACK) -> pd.Series:
    lo, hi = pd.Timestamp(f"{year - L}-01-01"), pd.Timestamp(f"{year}-01-01")
    w = r[(r.index >= lo) & (r.index < hi)].dropna()
    if len(w) and w.index.max() >= hi:
        raise FutureDataError(f"pick for {year} would use {w.index.max().date()}")
    return w


def score(w: pd.Series, metric: str) -> float:
    if len(w) < 20:
        return -np.inf
    if metric == "sharpe":
        v = sharpe(w)
    elif metric == "calmar":
        v = calmar_of_returns(w)
    elif metric == "cagr":
        v = cagr_of_returns(w)
    else:
        raise ValueError(metric)
    return v if np.isfinite(v) or v == np.inf else -np.inf


def pick(rets: dict, year: int, metric: str, L: int = LOOKBACK) -> dict:
    """Best setting (dict order = grid order; ties -> first) by ``metric`` over the trailing L years before ``year``."""
    best, best_v, last = None, -np.inf, None
    for key, r in rets.items():
        w = trailing_window(r, year, L)
        v = score(w, metric)
        if best is None or v > best_v:
            best, best_v = key, v
            last = w.index.max() if len(w) else None
    if last is not None:
        assert last < pd.Timestamp(f"{year}-01-01")
    return {"year": year, "metric": metric, "key": best, "value": best_v,
            "window_last": str(last.date()) if last is not None else ""}


def decision_year(sessions: pd.DatetimeIndex) -> np.ndarray:
    """Decision at close t uses the parameters of the year of the NEXT session (same as research_walk_forward)."""
    yrs = sessions.year.to_numpy()
    out = np.empty_like(yrs)
    out[:-1] = yrs[1:]
    out[-1] = yrs[-1]
    return out


def stop_schedule(sessions: pd.DatetimeIndex, picks: dict) -> list:
    """One StopSpec per session from yearly picks {year: {"key": label}}."""
    dy = decision_year(sessions)
    return [sr.parse(picks[int(y)]["key"]) for y in dy]


def nav_returns(nav: pd.Series) -> pd.Series:
    return anchored(nav, ACCOUNT).pct_change().dropna()


# ======================================================================== strategy runners

def eps_states_all(ciks) -> pd.DataFrame:
    """Point-in-time EPS states for every CIK (local SEC companyfacts files only; cached, not overwriting the
    CAN SLIM development cache)."""
    if EPS_ALL.is_file():
        return pd.read_csv(EPS_ALL, dtype={"filed": str, "avail": str, "q_end": str, "q_ya_end": str,
                                           "fy0_end": str})
    parts = [cs.eps_states(cs.extract_eps_facts(c)) for c in sorted(set(int(x) for x in ciks))]
    out = pd.concat([p for p in parts if len(p)], ignore_index=True)
    EPS_ALL.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(EPS_ALL, index=False)
    return pd.read_csv(EPS_ALL, dtype={"filed": str, "avail": str, "q_end": str, "q_ya_end": str, "fy0_end": str})


def carried_universe(data) -> pd.DataFrame:
    """CAN SLIM universe rows; Fridays after the last universe week reuse its membership (as the other rules do)."""
    uni = data.universe
    last_week = uni["week_end"].max()
    fridays = pd.date_range(last_week + pd.Timedelta(days=7), pd.Timestamp(data.spec["effective_end"]), freq="W-FRI")
    fridays = [f for f in fridays if data.sessions[data.sessions <= f][-1] > last_week]
    base = uni[uni["week_end"] == last_week]
    if not fridays:
        return uni
    return pd.concat([uni] + [base.assign(week_end=f) for f in fridays], ignore_index=True)


class Strategies:
    """Builds the three frozen strategies on one loaded window; ``run(name, stops, start)`` -> result dict."""

    def __init__(self, data, eps_states: pd.DataFrame | None = None, canslim_first_signal: str | None = None):
        self.data = data
        self.sigma = sr.sigma20(data.sig_idx)
        self.end = data.spec["effective_end"]
        self.sr_ = ri.StockRunner(data)                       # ONEQ / QQQ benchmarks, O10 eligibility
        # Livermore #20
        self.lcfg = lv.Config(k=3, market="sma50", idle="qqq_on")
        assert self.lcfg.name == LIVERMORE_NAME, self.lcfg.name
        self.lrun = lv.Runner(data)
        self.lparts = (self.lrun.leaders(self.lcfg), self.lrun.brk(self.lcfg), self.lrun.trail(self.lcfg),
                       self.lrun.m(self.lcfg))
        # O10
        self.o10 = ri.RULE_BY_CODE["O10"]
        self.obuy, self.osell = ri.stock_signals(self.o10, self.sr_.bars)
        # CAN SLIM #215
        self.ccfg = cs.Config(a_rule="cagr25", rebalance="monthly", idle="qqq")
        assert self.ccfg.name == CANSLIM_NAME, self.ccfg.name
        self.cdata = data
        if eps_states is not None:
            uni = carried_universe(data)
            d2 = _with_universe(data, uni)
            first = canslim_first_signal or str(uni["week_end"].min().date())
            feat = cs.build_features(d2, {"filed": eps_states}, first_signal=first)
            used = feat.dropna(subset=["avail"])
            assert (pd.to_datetime(used["avail"]) < used["week_end"]).all(), "EPS look-ahead"
            self.eps_check_rows = int(len(used))
            sel = cs.screen(feat, self.ccfg)
            self.cpicks = {t: list(g["security_id"]) for t, g in sel.groupby("week_end")}
            self.cweeks = sorted(feat["week_end"].unique())
            self.cm = cs.market_filter(data.qqq_close)["sma50"]
            self.crank = {t: dict(zip(g["security_id"], g["dv50_rank"])) for t, g in uni.groupby("week_end")}
            self.c_n_pass = sel.groupby("week_end").size().reindex(self.cweeks).fillna(0)

    def run(self, name: str, stops, start: str | None = None) -> dict:
        d = self.data
        if name == "livermore":
            res = lv.simulate(self.lcfg, d, *self.lparts, self.lrun.rank_of, stops=stops, sigma=self.sigma,
                              start=start)
            n_buy, n_stop = res["counts"]["buy"], res["counts"]["stop"]
        elif name == "o10":
            res = ri.stock_sim(self.o10, d, self.obuy, self.osell, self.sr_.elig, self.sr_.rank_of, stops=stops,
                               sigma=self.sigma, start=start)
            n_buy, n_stop = res["counts"]["buy"], res["counts"].get("stop", 0)
        elif name == "canslim":
            s0 = start or d.spec["perf_start"]
            sched = cs.signal_schedule(self.cweeks, d.sessions, self.cm, self.ccfg.rebalance, start=s0, end=self.end)
            res = cs.simulate(self.ccfg, d.sessions, d.perf_idx, d.close, d.last_row, d.qqq_perf_idx, d.qqq_close,
                              sched, self.cpicks, self.crank, ACCOUNT, stops=stops, sigma=self.sigma, start=s0,
                              end=self.end)
            n_buy, n_stop = res["n_buy"], res["n_stop"]
        else:
            raise ValueError(name)
        lv.cs.assert_window(res["nav"].index, d.spec["perf_start"], self.end, "nav")
        res["n_buy"], res["n_stop"] = n_buy, n_stop
        return res

    def evaluate(self, res: dict) -> dict:
        oneq, qqq = self.sr_.benchmarks(res["dates"], ACCOUNT)
        m = metrics(res["nav"], oneq, qqq, res["cost"], res["traded"], res["exposure"], res["trades"],
                    res["n_buy"], res["n_stop"])
        return m, oneq, qqq


def _with_universe(data, uni):
    from dataclasses import replace
    return replace(data, universe=uni)


def load(window_name: str = "stops", perf_end: str | None = None):
    lv.WINDOWS[window_name] = {**WINDOW, **({"perf_end": perf_end} if perf_end else {})}
    data = lv.load_window(window_name)
    assert data.spec["effective_end"] <= "2026-08-31"
    return data


# ======================================================================== checks (ledger 0.9)

def check_equivalence(st: Strategies) -> dict:
    """stops=None (original code path) == passing the original stop as a StopSpec, on the full window."""
    out = {}
    for name in STRATEGIES:
        a = st.run(name, None)
        b = st.run(name, BASE_STOP[name])
        diff = float((a["nav"] - b["nav"]).abs().max())
        assert diff < 1e-6, (name, diff)
        out[name] = diff
    return out


def check_dev_reproduction() -> dict:
    """The new code paths reproduce the ledgers' development-period numbers."""
    out = {}
    data = lv.load_window("dev")
    runner = lv.Runner(data)
    cfg = lv.Config(k=3, market="sma50", idle="qqq_on")
    m, res, _ = runner.run(cfg)
    res2 = lv.simulate(cfg, data, runner.leaders(cfg), runner.brk(cfg), runner.trail(cfg), runner.m(cfg),
                       runner.rank_of, stops=sr.StopSpec("fixed", 0.08))
    out["livermore_dev_cagr"] = m["cagr"]
    out["livermore_dev_buys"] = m["n_buy"]
    out["livermore_stopspec_maxdiff"] = float((res["nav"] - res2["nav"]).abs().max())
    srn = ri.StockRunner(data)
    mo, reso, _, _ = srn.run(ri.RULE_BY_CODE["O10"])
    b, s = ri.stock_signals(ri.RULE_BY_CODE["O10"], srn.bars)
    reso2 = ri.stock_sim(ri.RULE_BY_CODE["O10"], data, b, s, srn.elig, srn.rank_of, stops=sr.NONE)
    out["o10_dev_cagr"] = mo["cagr"]
    out["o10_dev_buys"] = mo["n_buy"]
    out["o10_stopspec_maxdiff"] = float((reso["nav"] - reso2["nav"]).abs().max())
    cd = cs.load_dev_data()
    states = eps_states_all(pd.read_csv(cs.INPUTS / "weekly_universe_top300.csv.gz", dtype=str,
                                        usecols=["cik"])["cik"].dropna().unique())
    feat = cs.build_features(cd, {"filed": states})
    ccfg = cs.Config(a_rule="cagr25", rebalance="monthly", idle="qqq")
    sel = cs.screen(feat, ccfg)
    picks = {t: list(g["security_id"]) for t, g in sel.groupby("week_end")}
    sched = cs.signal_schedule(sorted(feat["week_end"].unique()), cd.sessions, cs.market_filter(cd.qqq_close)["sma50"],
                               "monthly")
    rank_of = {t: dict(zip(g["security_id"], g["dv50_rank"])) for t, g in cd.universe.groupby("week_end")}
    args = (ccfg, cd.sessions, cd.perf_idx, cd.close, cd.last_row, cd.qqq_perf_idx, cd.qqq_close, sched, picks,
            rank_of, ACCOUNT)
    r1 = cs.simulate(*args)
    r2 = cs.simulate(*args, stops=sr.StopSpec("fixed", 0.08), sigma=sr.sigma20(cd.sig_idx))
    bench = cs.qqq_benchmark(cd.qqq_perf_idx, cd.qqq_close, r1["dates"], ACCOUNT)
    mc = cs.perf_metrics(r1["nav"], bench, ACCOUNT, r1["cost"], r1["traded"], r1["exposure"])
    out["canslim_dev_cagr"] = mc["cagr"]
    out["canslim_dev_buys"] = r1["n_buy"]
    out["canslim_dev_stops"] = r1["n_stop"]
    out["canslim_stopspec_maxdiff"] = float((r1["nav"] - r2["nav"]).abs().max())
    out["ledger"] = {"livermore_dev_cagr": 0.3744, "livermore_dev_buys": 163, "o10_dev_cagr": 0.2983,
                     "o10_dev_buys": 202, "canslim_dev_cagr": 0.1428, "canslim_dev_buys": 134, "canslim_dev_stops": 30}
    return out


def check_causality(st_full: Strategies, eps: pd.DataFrame, cut: str = "2020-12-31",
                    settings=("fixed8", "trail15", "vol3")) -> dict:
    """Reload the window truncated at ``cut`` and re-run: NAV up to the cut must equal the full-window NAV."""
    data = load("stops_cut", perf_end=cut)
    assert data.spec["effective_end"] <= cut
    st_cut = Strategies(data, eps, canslim_first_signal=str(st_full.cweeks[0].date()))
    out = {}
    for name in STRATEGIES:
        for lab in settings:
            a = st_full.run(name, sr.parse(lab))["nav"]
            b = st_cut.run(name, sr.parse(lab))["nav"]
            diff = float((a.reindex(b.index) - b).abs().max())
            assert diff < 1e-6, (name, lab, diff)
            out[f"{name}_{lab}"] = diff
    return out


# ======================================================================== main study

ROW_KEYS = ["start", "end", "cagr", "oneq_cagr", "qqq_cagr", "excess_oneq", "excess_qqq", "vol", "max_dd",
            "oneq_max_dd", "qqq_max_dd", "longest_dd_days", "longest_dd_unrecovered", "ulcer", "sharpe", "sortino",
            "calmar", "oneq_sharpe", "ir_oneq", "t_monthly_excess", "months", "beta_oneq", "alpha_oneq",
            "share_years_beating_oneq", "worst_year", "worst_year_which", "worst_month", "buys", "closed_trades",
            "stop_exits", "win_rate", "avg_win", "avg_loss", "win_loss_ratio", "profit_factor", "avg_hold_days",
            "time_in_market", "avg_exposure", "turnover_one_way_per_year", "cost_drag_per_year", "cost_usd_total"]


def judge(m: dict) -> dict:
    a = bool(m["cagr"] > m["oneq_cagr"] and m["t_monthly_excess"] >= 2)
    b = bool(abs(m["max_dd"]) <= abs(m["oneq_max_dd"]) - 0.10 and m["cagr"] >= m["oneq_cagr"] - 0.03)
    return {"A": a, "B": b, "pass": a or b}


def vs_base(m_wf: dict, m_base: dict) -> dict:
    d = (m_wf["_monthly_ret"] - m_base["_monthly_ret"]).dropna()
    t = float(d.mean() / d.std(ddof=1) * math.sqrt(len(d))) if d.std(ddof=1) > 0 else 0.0
    return {"cagr_diff": m_wf["cagr"] - m_base["cagr"], "sharpe_diff": m_wf["sharpe"] - m_base["sharpe"],
            "max_dd_diff": m_wf["max_dd"] - m_base["max_dd"], "t_monthly_diff": t,
            "beats_base": bool(m_wf["cagr"] > m_base["cagr"] and m_wf["sharpe"] > m_base["sharpe"])}


def run_study(only=None) -> dict:
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    data = load()
    print(data.guard["assertion"])
    ciks = data.universe["cik"].dropna().unique()
    eps = eps_states_all(pd.read_csv(cs.INPUTS / "weekly_universe_top300.csv.gz", dtype=str,
                                     usecols=["cik"])["cik"].dropna().unique())
    cover = float(pd.Series(pd.to_numeric(ciks, errors="coerce")).dropna().astype(int).isin(
        set(eps["cik"].astype(int))).mean())
    st = Strategies(data, eps)
    print(f"setup {time.time() - t0:.0f}s; EPS states {len(eps)} rows / {eps['cik'].nunique()} CIKs; "
          f"universe CIK coverage {cover:.1%}; point-in-time EPS rows checked {st.eps_check_rows}")
    meta = {"guard": data.guard["assertion"], "window": WINDOW, "wf_start": WF_START,
            "eps_states_rows": int(len(eps)), "eps_ciks": int(eps["cik"].nunique()), "universe_cik_coverage": cover,
            "canslim_avg_names_passing": float(st.c_n_pass.mean()),
            "canslim_weeks_zero_passing": int((st.c_n_pass == 0).sum())}
    meta["check_equivalence_full_window"] = check_equivalence(st)
    print("equivalence:", meta["check_equivalence_full_window"])

    grid = sr.stop_grid()
    labels = [g.label for g in grid]
    strategies = only or STRATEGIES
    static_rows, wf_rows, pick_rows, yearly = [], [], [], {}
    for name in strategies:
        rets, full_m, wfw_m = {}, {}, {}
        for spec in grid:
            res = st.run(name, spec)
            m, _, _ = st.evaluate(res)
            rets[spec.label] = nav_returns(res["nav"])
            full_m[spec.label] = m
            res2 = st.run(name, spec, start=WF_START)
            m2, oneq2, qqq2 = st.evaluate(res2)
            wfw_m[spec.label] = m2
            for win, mm in (("2015-2026", m), ("2018-2026", m2)):
                static_rows.append({"strategy": name, "window": win, "stop": spec.label, "stop_kind": spec.kind,
                                    "is_base": spec == BASE_STOP[name], **{k: mm[k] for k in ROW_KEYS}})
            print(f"[{time.time() - t0:5.0f}s] {name:9s} {spec.label:8s} full CAGR {m['cagr']:+.3f} SR {m['sharpe']:.2f} "
                  f"| 2018- CAGR {m2['cagr']:+.3f} vs ONEQ {m2['oneq_cagr']:+.3f} DD {m2['max_dd']:.2f}", flush=True)
        # walk-forward
        base_m = wfw_m[BASE_STOP[name].label]
        for metric in PICK_METRICS:
            picks = {y: pick(rets, y, metric) for y in WF_YEARS}
            for y, p in picks.items():
                pick_rows.append({"strategy": name, "metric": metric, "year": y, "stop": p["key"],
                                  "trailing_value": p["value"], "window_last": p["window_last"]})
            sess = st.data.sessions
            perf = sess[(sess >= pd.Timestamp(WF_START)) & (sess <= pd.Timestamp(st.end))]
            sched = stop_schedule(perf, picks)
            res = st.run(name, sched, start=WF_START)
            m, oneq, qqq = st.evaluate(res)
            j = judge(m)
            halves = [sub_window(res, oneq, qqq, a, b) for a, b in HALVES]
            vb = vs_base(m, base_m)
            n_switch = sum(picks[y]["key"] != picks[y - 1]["key"] for y in list(WF_YEARS)[1:])
            fixed_cagrs = {k: v["cagr"] for k, v in wfw_m.items()}
            rank = int(sum(v > m["cagr"] for v in fixed_cagrs.values()) + 1)
            wf_rows.append({"strategy": name, "metric": metric, "primary": metric == PRIMARY,
                            **{k: m[k] for k in ROW_KEYS}, **j,
                            "h1_cagr": halves[0]["cagr"], "h1_oneq": halves[0]["oneq_cagr"],
                            "h2_cagr": halves[1]["cagr"], "h2_oneq": halves[1]["oneq_cagr"],
                            "switches": n_switch, "rank_among_28_fixed_cagr": rank,
                            **{f"vs_base_{k}": v for k, v in vb.items()},
                            "base_stop": BASE_STOP[name].label, "base_cagr": base_m["cagr"],
                            "base_sharpe": base_m["sharpe"], "base_max_dd": base_m["max_dd"]})
            yearly[f"{name}_wf_{metric}"] = m["by_year"]
            if metric == PRIMARY:
                res["nav"].to_frame("nav").assign(oneq=oneq, qqq=qqq, exposure=res["exposure"]).to_csv(
                    OUT / f"wf_{name}_daily_nav.csv")
                res["trades"].to_csv(OUT / f"wf_{name}_trades.csv", index=False)
            print(f"WF {name} {metric}: CAGR {m['cagr']:+.3f} ONEQ {m['oneq_cagr']:+.3f} t {m['t_monthly_excess']:+.2f} "
                  f"DD {m['max_dd']:.2f}/{m['oneq_max_dd']:.2f} pass={j['pass']} | base CAGR {base_m['cagr']:+.3f}",
                  flush=True)
        yearly[f"{name}_base"] = base_m["by_year"]
    meta["runtime_s"] = time.time() - t0
    return {"static": pd.DataFrame(static_rows), "wf": pd.DataFrame(wf_rows), "picks": pd.DataFrame(pick_rows),
            "yearly": yearly, "meta": meta, "eps": eps, "st": st}


def heat_tables(static: pd.DataFrame) -> None:
    hd = OUT / "heat"
    hd.mkdir(exist_ok=True)
    order = [g.label for g in sr.stop_grid()]
    for win in static["window"].unique():
        s = static[static["window"] == win]
        for col in ("cagr", "excess_oneq", "sharpe", "calmar", "max_dd", "ulcer", "t_monthly_excess",
                    "cost_drag_per_year", "closed_trades"):
            pv = s.pivot(index="stop", columns="strategy", values=col).reindex(order)
            pv.to_csv(hd / f"{win}_{col}.csv", float_format="%.4f")
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        cols = ["cagr", "excess_oneq", "sharpe", "calmar", "max_dd", "ulcer"]
        for win in static["window"].unique():
            fig, axes = plt.subplots(1, 3, figsize=(18, 9))
            for ax, name in zip(axes, STRATEGIES):
                s = static[(static["window"] == win) & (static["strategy"] == name)].set_index("stop").reindex(order)
                z = s[cols].astype(float)
                zn = (z - z.mean()) / z.std().replace(0, 1)
                zn["max_dd"], zn["ulcer"] = zn["max_dd"], -zn["ulcer"]
                ax.imshow(zn.to_numpy(), aspect="auto", cmap="RdYlGn")
                for (i, j), v in np.ndenumerate(z.to_numpy()):
                    ax.text(j, i, f"{v:.2f}" if cols[j] in ("sharpe", "calmar", "ulcer") else f"{v:+.0%}",
                            ha="center", va="center", fontsize=7)
                ax.set_xticks(range(len(cols)), cols, rotation=30)
                ax.set_yticks(range(len(order)), order, fontsize=7)
                ax.set_title(f"{name} {win}")
            fig.tight_layout()
            fig.savefig(hd / f"heat_{win}.png", dpi=110)
            plt.close(fig)
    except Exception as exc:  # noqa: BLE001
        print("heatmap png skipped:", exc)


def main(argv=None):
    p = argparse.ArgumentParser(description="stop-loss study (pre-registered in docs/research_ledger_stops.md)")
    p.add_argument("--checks-only", action="store_true", help="dev reproduction + causality checks only")
    p.add_argument("--skip-dev-check", action="store_true")
    a = p.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    if a.checks_only:
        chk = check_dev_reproduction()
        (OUT / "check_dev_reproduction.json").write_text(json.dumps(chk, indent=1, default=str))
        print(json.dumps(chk, indent=1, default=str))
        return
    out = run_study()
    meta = out["meta"]
    meta["check_causality_cut_2020_12_31"] = check_causality(out["st"], out["eps"])
    print("causality:", meta["check_causality_cut_2020_12_31"])
    if not a.skip_dev_check:
        meta["check_dev_reproduction"] = check_dev_reproduction()
        print("dev reproduction:", meta["check_dev_reproduction"])
    out["static"].to_csv(OUT / "static_all.csv", index=False, float_format="%.6f")
    out["wf"].to_csv(OUT / "walk_forward.csv", index=False, float_format="%.6f")
    out["picks"].to_csv(OUT / "walk_forward_picks.csv", index=False, float_format="%.6f")
    heat_tables(out["static"])
    meta["bonferroni_t_3_primary_one_sided_5pct"] = BONFERRONI_3
    meta["trial_count"] = {"static_post_hoc": int(len(out["static"]) // 2), "walk_forward_configs": int(len(out["wf"])),
                           "primary_tests": len(STRATEGIES)}
    (OUT / "summary.json").write_text(json.dumps({"meta": meta, "yearly": out["yearly"]}, indent=1, default=str))
    pd.set_option("display.width", 250)
    cols = ["strategy", "metric", "cagr", "oneq_cagr", "qqq_cagr", "max_dd", "oneq_max_dd", "sharpe",
            "t_monthly_excess", "A", "B", "pass", "base_cagr", "vs_base_cagr_diff", "vs_base_sharpe_diff",
            "vs_base_beats_base", "switches"]
    print(out["wf"][cols].to_string(float_format=lambda v: f"{v:+.3f}"))
    print(out["picks"][out["picks"]["metric"] == PRIMARY].pivot(index="year", columns="strategy",
                                                                 values="stop").to_string())


if __name__ == "__main__":
    main()
