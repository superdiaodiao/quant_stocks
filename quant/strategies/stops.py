"""The stop-loss study's three frozen strategies with a replaceable stop slot, the walk-forward stop picking and the
engine checks (pre-registered in docs/research_ledger_stops.md).

Moved unchanged from studies/stops.py: ``Strategies`` (Livermore #20 via quant.strategies.livermore.simulate, the
indicators rule O10 via quant.strategies.indicators.stock_sim, CAN SLIM #215 via quant.strategies.canslim.simulate,
each taking ``stops=`` from quant.backtest.stop_rules), the point-in-time EPS states of every CIK
(``eps_states_all``, cache path an argument), the CAN SLIM universe carried past its last week, the yearly picks
(``score`` / ``pick``: best trailing Sharpe / Calmar / CAGR, ties keep grid order; the trailing window is
quant.strategies.walk_forward.trailing_window) and the checks of ledger 0.9 (``check_equivalence``,
``check_dev_reproduction``, ``check_causality``). The metrics are quant.evaluation.nav (``oneq_nav_metrics``).
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from quant.backtest import stop_rules as sr
from quant.data import eps as pit_eps
from quant.data.calendar import decision_year
from quant.evaluation.nav import (
    anchored, calmar_of_returns, cagr_of_returns, oneq_nav_metrics, sharpe_daily,
)
from quant.strategies import canslim as cs
from quant.strategies import indicators as ri
from quant.strategies import livermore as lv
from quant.strategies.walk_forward import trailing_window

LIVERMORE_NAME = "U300_RS126_G8_top20_high252_Voff_K3_pyr2x5_stop8_sma50_Msma50x_idleqqq_on"
CANSLIM_NAME = "U300_C25_Acagr25_N15_L252s21t20_Soff_Msma50_idleqqq_core0_K8_M_stop8_ptoff_xfail"
EPS_DTYPES = {"filed": str, "avail": str, "q_end": str, "q_ya_end": str, "fy0_end": str}


# ======================================================================== walk-forward picking (past data only)

def score(w: pd.Series, metric: str) -> float:
    if len(w) < 20:
        return -np.inf
    if metric == "sharpe":
        v = sharpe_daily(w)
    elif metric == "calmar":
        v = calmar_of_returns(w)
    elif metric == "cagr":
        v = cagr_of_returns(w)
    else:
        raise ValueError(metric)
    return v if np.isfinite(v) or v == np.inf else -np.inf


def pick(rets: dict, year: int, metric: str, L: int = 3) -> dict:
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


def stop_schedule(sessions: pd.DatetimeIndex, picks: dict) -> list:
    """One StopSpec per session from yearly picks {year: {"key": label}}."""
    dy = decision_year(sessions)
    return [sr.parse(picks[int(y)]["key"]) for y in dy]


def nav_returns(nav: pd.Series, account: float = 10_000.0) -> pd.Series:
    return anchored(nav, account).pct_change().dropna()


# ======================================================================== strategy runners

def eps_states_all(ciks, path: Path) -> pd.DataFrame:
    """Point-in-time EPS states for every CIK (local SEC companyfacts files only; cached at ``path``, not
    overwriting the CAN SLIM development cache)."""
    if path.is_file():
        return pd.read_csv(path, dtype=EPS_DTYPES)
    parts = [pit_eps.eps_states(pit_eps.extract_eps_facts(c)) for c in sorted(set(int(x) for x in ciks))]
    out = pd.concat([p for p in parts if len(p)], ignore_index=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(path, index=False)
    return pd.read_csv(path, dtype=EPS_DTYPES)


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
    """Builds the three frozen strategies on one loaded window; ``run(name, stops, start)`` -> result dict.
    ``raw``: the indicators study's ^VIX chart folder (O10 signal bars)."""

    def __init__(self, data, raw: Path, eps_states: pd.DataFrame | None = None,
                 canslim_first_signal: str | None = None, account: float = 10_000.0):
        self.data = data
        self.account = account
        self.sigma = sr.sigma20(data.sig_idx)
        self.end = data.spec["effective_end"]
        self.sr_ = ri.StockRunner(data, raw)                  # ONEQ / QQQ benchmarks, O10 eligibility
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
            d2 = replace(data, universe=uni)
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
                              sched, self.cpicks, self.crank, self.account, stops=stops, sigma=self.sigma, start=s0,
                              end=self.end)
            n_buy, n_stop = res["n_buy"], res["n_stop"]
        else:
            raise ValueError(name)
        lv.cs.assert_window(res["nav"].index, d.spec["perf_start"], self.end, "nav")
        res["n_buy"], res["n_stop"] = n_buy, n_stop
        return res

    def evaluate(self, res: dict) -> dict:
        oneq, qqq = self.sr_.benchmarks(res["dates"], self.account)
        m = oneq_nav_metrics(res["nav"], oneq, qqq, res["cost"], res["traded"], res["exposure"], res["trades"],
                             res["n_buy"], res["n_stop"])
        return m, oneq, qqq


# ======================================================================== checks (ledger 0.9)

def check_equivalence(st: Strategies, base_stop: dict) -> dict:
    """stops=None (original code path) == passing the original stop as a StopSpec, on the full window."""
    out = {}
    for name in base_stop:
        a = st.run(name, None)
        b = st.run(name, base_stop[name])
        diff = float((a["nav"] - b["nav"]).abs().max())
        assert diff < 1e-6, (name, diff)
        out[name] = diff
    return out


def check_dev_reproduction(raw: Path, eps_path: Path, account: float = 10_000.0) -> dict:
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
    srn = ri.StockRunner(data, raw)
    mo, reso, _, _ = srn.run(ri.RULE_BY_CODE["O10"])
    b, s = ri.stock_signals(ri.RULE_BY_CODE["O10"], srn.bars)
    reso2 = ri.stock_sim(ri.RULE_BY_CODE["O10"], data, b, s, srn.elig, srn.rank_of, stops=sr.NONE)
    out["o10_dev_cagr"] = mo["cagr"]
    out["o10_dev_buys"] = mo["n_buy"]
    out["o10_stopspec_maxdiff"] = float((reso["nav"] - reso2["nav"]).abs().max())
    cd = cs.load_dev_data()
    states = eps_states_all(pd.read_csv(cs.INPUTS / "weekly_universe_top300.csv.gz", dtype=str,
                                        usecols=["cik"])["cik"].dropna().unique(), eps_path)
    feat = cs.build_features(cd, {"filed": states})
    ccfg = cs.Config(a_rule="cagr25", rebalance="monthly", idle="qqq")
    sel = cs.screen(feat, ccfg)
    picks = {t: list(g["security_id"]) for t, g in sel.groupby("week_end")}
    sched = cs.signal_schedule(sorted(feat["week_end"].unique()), cd.sessions, cs.market_filter(cd.qqq_close)["sma50"],
                               "monthly")
    rank_of = {t: dict(zip(g["security_id"], g["dv50_rank"])) for t, g in cd.universe.groupby("week_end")}
    args = (ccfg, cd.sessions, cd.perf_idx, cd.close, cd.last_row, cd.qqq_perf_idx, cd.qqq_close, sched, picks,
            rank_of, account)
    r1 = cs.simulate(*args)
    r2 = cs.simulate(*args, stops=sr.StopSpec("fixed", 0.08), sigma=sr.sigma20(cd.sig_idx))
    bench = cs.qqq_benchmark(cd.qqq_perf_idx, cd.qqq_close, r1["dates"], account)
    mc = cs.perf_metrics(r1["nav"], bench, account, r1["cost"], r1["traded"], r1["exposure"])
    out["canslim_dev_cagr"] = mc["cagr"]
    out["canslim_dev_buys"] = r1["n_buy"]
    out["canslim_dev_stops"] = r1["n_stop"]
    out["canslim_stopspec_maxdiff"] = float((r1["nav"] - r2["nav"]).abs().max())
    out["ledger"] = {"livermore_dev_cagr": 0.3744, "livermore_dev_buys": 163, "o10_dev_cagr": 0.2983,
                     "o10_dev_buys": 202, "canslim_dev_cagr": 0.1428, "canslim_dev_buys": 134, "canslim_dev_stops": 30}
    return out


def check_causality(st_full: Strategies, st_cut: Strategies, names, settings=("fixed8", "trail15", "vol3")) -> dict:
    """``st_cut`` is built on the window truncated at the cut: NAV up to the cut must equal the full-window NAV."""
    out = {}
    for name in names:
        for lab in settings:
            a = st_full.run(name, sr.parse(lab))["nav"]
            b = st_cut.run(name, sr.parse(lab))["nav"]
            diff = float((a.reindex(b.index) - b).abs().max())
            assert diff < 1e-6, (name, lab, diff)
            out[f"{name}_{lab}"] = diff
    return out
