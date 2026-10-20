"""Shorting below the 200-day SMA: long / short and short-only versions of the frozen QQQ rule T01_L200_b2
(pre-registered in docs/research_ledger_sma200_short.md, section 0).

Rules (quant.strategies.sma200_short): signal S2 = the frozen state machine (on above 1.02 x SMA200, off below 0.98 x,
hold in between) or S0 = the plain cross; positions LS1 (+1 / -1), FS1 (0 / -1), LS0.5 (+1 / -0.5); decided at the
close of t, traded at the close of t+1. Implementations (quant.backtest.long_short): (a) a QQQ short sale in an IBKR
Reg T margin account (borrow fee, IBKR interest rules, maintenance check and forced liquidation); (b) the inverse ETF
PSQ (real from 2006-06-22, the calibrated synthetic -1x before). SQQQ (-3x) report only. Index level: ^NDX, ^IXIC,
^GSPC price indices against their own buy-and-hold (short leg credited with the T-bill rate).
Data (local only): research_cache/qqq_timing/raw, benchmarks, international/raw, leverage_methods/raw, sma200_short/raw
(quant.data.sma200_short). Every frame truncated at ``END``. Outputs: output/research_only/sma200_short/ (returns and
metrics only, never vendor price levels). ``--posthoc`` (2026-10-21, after the registered run) writes the review
checks (report only: the T-bill's rounding, the index level on consistent bases, drawdown dates, interest totals) to
posthoc_*.

Usage:  PYTHONPATH=. .venv/bin/python -m quant study sma200_short [--fetch] [--data-only | --register | --run | --posthoc]
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import replace

import numpy as np
import pandas as pd

from quant.backtest.costs import CASH_ETF_FEE
from quant.backtest import exposure
from quant.backtest.long_short import MarginRules, Market, simulate_index_ls, simulate_long_short
from quant.data import inverse_etfs, sma200_short as data
from quant.data.guards import assert_dev_dates
from quant.data.rates import load_dtb3
from quant.evaluation.criteria import bonferroni_t, timing_criteria
from quant.evaluation.episodes import episode_summary, share_in_state, state_episodes
from quant.evaluation.equity import drawdown_info, equity0, t_monthly_excess, window_stats
from quant.evaluation.metrics import TRADING_DAYS, cagr_of, max_drawdown, monthly, yearly
from quant.paths import ROOT, output_dir
from quant.reporting import Preregistration, RunLog, write_json, write_period_tables
from quant.strategies import international as intl_st
from quant.strategies import sma200_short as st

END = "2026-09-30"
FETCH_PERIOD2 = "2026-10-03"
ENTRY = "1999-03-10"                 # QQQ's first close (signal of 1999-03-09, NDX warm-up)
ONEQ_ENTRY = "2003-10-01"            # the ONEQ basis (ONEQ's first close)
RECENT_ENTRY = "2014-12-31"          # the qqq_timing one-shot window
SQQQ_ENTRY = "2010-02-11"            # SQQQ's first close
PSQ_CAL = ("2006-06-22", "2014-12-31")
PSQ_VAL = ("2015-01-02", END)
SQQQ_CAL = ("2010-02-12", "2014-12-31")
PSQ_ACCEPT = {"abs_cagr_gap_max": 0.010, "monthly_te_max": 0.020}
ONESHOT_T01 = (0.146, -0.253)        # T01_L200_b2 2015-01..2026-09 (qqq_timing one-shot), reproduced within 0.05 pp
RULES = MarginRules()                # borrow 0.30%/yr, IBKR rates, Reg T 50% / 95% cap, maintenance 25% / 30%
HS_ONEQ, HS_PSQ, HS_SQQQ = 2e-4, 2e-4, 2e-4
INDEX_COST = 0.0010                  # per unit of index value traded
INDEX_BORROW = 0.0030                # per year, calendar days / 360
GSPC_NOT_BEFORE = "1954-02-01"
N_TRIALS = len(st.qqq_trials()) + len(st.index_trials())
WINDOWS = {"2000-2002": ("2000-01-01", "2002-12-31"), "2008": ("2008-01-01", "2008-12-31"),
           "2020_crash_rebound": ("2020-02-20", "2020-06-30"), "2020": ("2020-01-01", "2020-12-31"),
           "2022": ("2022-01-01", "2022-12-31")}
INDEX_WINDOWS = {"1973-1974": ("1973-01-01", "1974-12-31"), "1987": ("1987-01-01", "1987-12-31"), **WINDOWS}
OUT = output_dir("sma200_short")
LEDGER = ROOT / "docs/research_ledger_sma200_short.md"
# post-hoc review checks (2026-10-21, after the registered run; report only, see ``posthoc``)
POSTHOC_ETF = {"^GSPC": ("SPY", 0.000945), "^NDX": ("QQQ", 0.0020), "^IXIC": ("ONEQ", 0.0021)}  # ETF, expense ratio
POSTHOC_DIV_BEFORE = {"^GSPC": 0.038, "^NDX": 0.006, "^IXIC": 0.012}   # rough yields assumed before the ETF overlap


# ======================================================================== inputs

def markets(d: data.QQQData, psq_spread: float, sqqq_spread: float, synthetic_psq: bool = False) -> dict:
    s = d.sessions
    zero = pd.Series(0.0, index=s)
    r_cash = d.rf - CASH_ETF_FEE / TRADING_DAYS
    base = dict(r_long=d.r1, r_price=d.r_price, div=d.div, price=d.price, r_cash=r_cash, effr=d.effr)
    psq_real = data.fund_returns(d.funds["PSQ"], s)
    sqqq_real = data.fund_returns(d.funds["SQQQ"], s)
    lev_p, _, _, er_p = inverse_etfs.FUNDS["PSQ"]
    lev_s, _, _, er_s = inverse_etfs.FUNDS["SQQQ"]
    psq = st.inverse_leg(d.r1, d.rf, psq_real, lev_p, er_p, psq_spread, synthetic_only=synthetic_psq)
    sqqq = st.inverse_leg(d.r1, d.rf, sqqq_real, lev_s, er_s, sqqq_spread)
    return {
        "margin": Market(**base),
        "psq": Market(**base, r_inv=psq, inv_price=d.funds["PSQ"]["close"].reindex(s), inv_scale=1.0,
                      inv_leverage=lev_p, hs_inv=HS_PSQ),
        "sqqq": Market(**base, r_inv=sqqq, inv_price=d.funds["SQQQ"]["close"].reindex(s), inv_scale=1 / 3,
                       inv_leverage=lev_s, hs_inv=HS_SQQQ),
        "oneq": Market(r_long=d.oneq_star, r_price=zero, div=zero, price=d.oneq_price, r_cash=r_cash, effr=d.effr,
                       hs_long=HS_ONEQ),
    }


def inverse_validation(d: data.QQQData) -> dict:
    s = d.sessions
    out = {}
    for sym, cal in (("PSQ", PSQ_CAL), ("SQQQ", SQQQ_CAL)):
        lev, _, _, er = inverse_etfs.FUNDS[sym]
        real = data.fund_returns(d.funds[sym], s)
        c = st.calibrate(real, d.r1, d.rf, lev, er, *cal)
        val = st.validate(real, d.r1, d.rf, lev, er, c["spread_calibrated"], *PSQ_VAL)
        val0 = st.validate(real, d.r1, d.rf, lev, er, 0.0, *PSQ_VAL)
        ok = abs(val["cagr_gap_syn_minus_real"]) <= PSQ_ACCEPT["abs_cagr_gap_max"] and \
            val["tracking_error_monthly_ann"] <= PSQ_ACCEPT["monthly_te_max"]
        out[sym] = {"leverage": lev, "expense_ratio": er, "calibration": c, "validation_frozen_spread": val,
                    "validation_spread_0": val0, "acceptable": bool(ok)}
    return out


# ======================================================================== QQQ family

def run_one(tgt: pd.Series, mk: Market, start: str, impl: str, rules: MarginRules = RULES) -> dict:
    return simulate_long_short(tgt, mk, start, END, impl=("inverse" if impl in ("psq", "sqqq") else "margin"),
                               rules=rules)


def benchmarks(d: data.QQQData, mks: dict, states: dict, start: str) -> dict:
    one = pd.Series(1.0, index=d.sessions)
    out = {"QQQ": run_one(one, mks["margin"], start, "margin"), "ONEQ*": run_one(one, mks["oneq"], start, "margin")}
    for sig, s in states.items():
        out[f"T01_{sig}"] = run_one(st.target(s, "T01"), mks["margin"], start, "margin")
    return out


def details(sim: dict, d: data.QQQData) -> dict:
    r = sim["ret"]
    yrs = len(r) / TRADING_DAYS
    v0 = sim["value"]
    ep = state_episodes(v0, sim["state"], d.r1)
    nav_mean = float(v0.mean())
    out = {"share_short": share_in_state(sim["state_held"], "short"), "share_long": share_in_state(sim["state_held"], "long"),
           "share_flat": share_in_state(sim["state_held"], "flat"), "switches": int(sim["switches"]),
           "switches_per_year": sim["switches"] / yrs, "orders": int(sim["orders"]), "liquidations": int(sim["liquidations"]),
           "liquidation_dates": sim["liquidation_dates"],
           "short_episodes": episode_summary(ep, "short"), "long_episodes": episode_summary(ep, "long"),
           # dollar flows over the run divided by the mean NAV and the years: an order of magnitude per year
           "per_year_of_mean_nav": {k: sim[k] / nav_mean / yrs for k in
                                    ("costs", "borrow", "dividends", "margin_interest", "credit_interest")},
           "end_nav": float(v0.iloc[-1])}
    return out, ep


def window_table(series: dict, windows: dict) -> list:
    rows = []
    for name, r in series.items():
        for w, (a, b) in windows.items():
            x = window_stats(r, a, b)
            if x is not None:
                rows.append({"series": name, "window": w, **x})
    return rows


def qqq_family(d: data.QQQData, mks: dict, log: RunLog) -> dict:
    states = {sig: st.state(d.level, sig) for sig in st.SIGNALS}
    bases = {"spliced": ENTRY, "oneq": ONEQ_ENTRY}
    res, rows, ver_rows, det_rows, ep_rows = {}, [], [], [], []
    by_year, mon, win_series = {}, {}, {}
    bench = {}
    for basis, start in bases.items():
        bench[basis] = b = benchmarks(d, mks, states, start)
        for k, sim in b.items():
            assert_dev_dates(sim["ret"].index, END)
        log.say(f"[{basis}] entry {start}: QQQ CAGR {cagr_of(b['QQQ']['ret']):+.2%} ONEQ* {cagr_of(b['ONEQ*']['ret']):+.2%} "
                f"T01_S2 {cagr_of(b['T01_S2']['ret']):+.2%} T01_S0 {cagr_of(b['T01_S0']['ret']):+.2%}")
    # the frozen long/flat engine must agree with the new engine on T01_S2 (main basis)
    t01 = st.target(states["S2"], "T01")
    ref = exposure.simulate(t01, d.r1, d.r1 * 2, d.rf - CASH_ETF_FEE / TRADING_DAYS, d.price, ENTRY, END)
    gap = float(np.max(np.abs(ref["value"].values - bench["spliced"]["T01_S2"]["value"].values)))
    assert gap < 1e-6, gap
    b = bench["spliced"]
    for k in ("QQQ", "ONEQ*", "T01_S2", "T01_S0"):
        by_year[k], mon[k] = yearly(b[k]["ret"]), monthly(b[k]["ret"])
        win_series[k] = b[k]["ret"]
    for tr in st.qqq_trials():
        tgt = st.target(states[tr.signal], tr.position)
        res[tr.name] = {"signal": tr.signal, "position": tr.position, "impl": tr.impl}
        for basis, start in bases.items():
            sim = run_one(tgt, mks[tr.impl], start, tr.impl)
            bb = bench[basis]
            r = sim["ret"]
            assert_dev_dates(r.index, END)
            j = st.judge(r, bb["ONEQ*"]["ret"], bb["QQQ"]["ret"], bb[f"T01_{tr.signal}"]["ret"], d.rf, N_TRIALS)
            res[tr.name][basis] = j
            for h in ("full", "H1", "H2"):
                m = j["metrics"][h]
                rows.append({"trial": tr.name, "basis": basis, "period": h, "start": m["start"], "end": m["end"],
                             "cagr": m["cagr"], "oneq_cagr": m["oneq_cagr"], "qqq_cagr": m["qqq_cagr"],
                             "longflat_cagr": m["longflat_cagr"], "cagr_minus_oneq": m["cagr_minus_oneq"],
                             "cagr_minus_qqq": m["cagr_minus_qqq"], "cagr_minus_longflat": m["cagr_minus_longflat"],
                             "max_dd": m["max_dd"], "oneq_max_dd": m["oneq_max_dd"], "qqq_max_dd": m["qqq_max_dd"],
                             "longflat_max_dd": m["longflat_max_dd"], "dd_shallower_than_oneq_pp": m["dd_shallower_than_oneq_pp"],
                             "vol": m["vol"], "sharpe": m["sharpe"], "worst_month": m["worst_month"],
                             "worst_month_ret": m["worst_month_ret"], "t_vs_oneq": m["t_monthly_excess_vs_oneq"],
                             "t_vs_qqq": m["t_monthly_excess_vs_qqq"], "t_vs_longflat": m["t_monthly_excess_vs_longflat"]})
            if basis == "spliced":
                det, ep = details(sim, d)
                res[tr.name]["details"] = det
                det_rows.append({"trial": tr.name, **{k: v for k, v in det.items() if not isinstance(v, (dict, list))},
                                 **{f"short_{k}": v for k, v in det["short_episodes"].items()},
                                 **{f"long_{k}": v for k, v in det["long_episodes"].items()},
                                 **{f"{k}_per_year": v for k, v in det["per_year_of_mean_nav"].items()}})
                for e in ep.to_dict("records"):
                    ep_rows.append({"trial": tr.name, **e})
                by_year[tr.name], mon[tr.name] = yearly(r), monthly(r)
                win_series[tr.name] = r
                f = j["metrics"]["full"]
                log.say(f"{tr.name:18s} CAGR {f['cagr']:+.2%} (ONEQ* {f['oneq_cagr']:+.2%} QQQ {f['qqq_cagr']:+.2%} "
                        f"T01 {f['longflat_cagr']:+.2%}) H1 {j['metrics']['H1']['cagr_minus_oneq']:+.2%} "
                        f"H2 {j['metrics']['H2']['cagr_minus_oneq']:+.2%} MDD {f['max_dd']:.1%} t {f['t_monthly_excess_vs_oneq']:+.2f}"
                        f" short {det['share_short']:.0%} sw {det['switches']} liq {det['liquidations']} "
                        f"| A {j['verdict']['A']} B {j['verdict']['B']} C {j['C_short_leg_adds_value']}")
        v1, v2 = res[tr.name]["spliced"]["verdict"], res[tr.name]["oneq"]["verdict"]
        final = bool(v1["pass"] and v2["pass"])
        res[tr.name]["pass"] = final
        ver_rows.append({"trial": tr.name, "signal": tr.signal, "position": tr.position, "impl": tr.impl,
                         "spliced_A": v1["A"], "spliced_B": v1["B"], "spliced_pass": v1["pass"],
                         "spliced_A_bonferroni": v1["A_bonferroni"], "oneq_A": v2["A"], "oneq_B": v2["B"],
                         "oneq_pass": v2["pass"], "oneq_A_bonferroni": v2["A_bonferroni"], "pass": final,
                         "pass_bonferroni": bool((v1["A_bonferroni"] or v1["B"]) and (v2["A_bonferroni"] or v2["B"])),
                         "spliced_pass_vs_qqq": res[tr.name]["spliced"]["verdict_vs_qqq"]["pass"],
                         "C_spliced": res[tr.name]["spliced"]["C_short_leg_adds_value"],
                         "C_oneq": res[tr.name]["oneq"]["C_short_leg_adds_value"],
                         "split_spliced": res[tr.name]["spliced"]["split"], "split_oneq": res[tr.name]["oneq"]["split"]})
    wins = window_table(win_series, WINDOWS)
    dec = []
    for name, r in win_series.items():
        for x in intl_st.decade_table(r, b["ONEQ*"]["ret"].loc[r.index], min_sessions=240):
            dec.append({"series": name, **x, "oneq_cagr": x["bench_cagr"], "cagr_minus_oneq": x["cagr_minus_bench"]})
        y99 = r.loc[:"1999-12-31"]
        if len(y99):
            dec.append({"series": name, "decade": "1999 (Mar-Dec)", "start": str(y99.index[0].date()),
                        "end": str(y99.index[-1].date()), "cagr": float(np.prod(1 + y99) - 1), "note": "total, not annualised"})
    return {"trials": res, "summary": pd.DataFrame(rows), "verdicts": pd.DataFrame(ver_rows),
            "details": pd.DataFrame(det_rows), "episodes": pd.DataFrame(ep_rows), "windows": pd.DataFrame(wins),
            "by_decade": pd.DataFrame(dec), "by_year": by_year, "monthly": mon, "states": states,
            "t01_engine_gap": gap}


def recent_window(d: data.QQQData, mks: dict, states: dict, log: RunLog) -> pd.DataFrame:
    """2015-01-02 .. END from a fresh $10k at the 2014-12-31 close (the qqq_timing one-shot window), report only."""
    b = benchmarks(d, mks, states, RECENT_ENTRY)
    q = b["QQQ"]["ret"]

    def m(r):
        c, dd = cagr_of(r), max_drawdown(equity0(r))
        return {"cagr": c, "max_dd": dd, "calmar": c / abs(dd) if dd < 0 else float("nan")}
    mq = m(q)
    t01 = m(b["T01_S2"]["ret"])
    log.say(f"[2015-2026] T01_S2 CAGR {t01['cagr']:+.2%} MDD {t01['max_dd']:.1%} (one-shot reproduction: +14.6% / -25.3%); "
            f"QQQ {mq['cagr']:+.2%} / {mq['max_dd']:.1%}")
    if ONESHOT_T01 is not None and (abs(t01["cagr"] - ONESHOT_T01[0]) > 5e-4 or abs(t01["max_dd"] - ONESHOT_T01[1]) > 5e-4):
        raise SystemExit("T01_L200_b2 does not reproduce the one-shot result; stop and investigate")
    rows = []
    for name, sim in list(b.items()) + [(tr.name, run_one(st.target(states[tr.signal], tr.position), mks[tr.impl],
                                                        RECENT_ENTRY, tr.impl)) for tr in st.qqq_trials()]:
        r = sim["ret"]
        x = m(r)
        lf = b[f"T01_{name.split('_')[1]}"]["ret"] if name.count("_") >= 2 else None
        crit = timing_criteria(x, mq)
        rows.append({"series": name, **x, "cagr_minus_qqq": x["cagr"] - mq["cagr"],
                     "cagr_minus_oneq": x["cagr"] - cagr_of(b["ONEQ*"]["ret"]),
                     "cagr_minus_longflat": x["cagr"] - cagr_of(lf) if lf is not None else float("nan"),
                     "c1_dd_10pp": crit["c1_maxdd_at_least_10pp_shallower"], "c2_calmar": crit["c2_calmar_higher"],
                     "c3_cagr_3pp": crit["c3_cagr_shortfall_at_most_3pp"], "oneshot_criteria_pass": crit["pass"],
                     "share_short": share_in_state(sim["state_held"], "short"), "switches": sim["switches"]})
    return pd.DataFrame(rows)


def sqqq_report(d: data.QQQData, mks: dict, states: dict) -> pd.DataFrame:
    b = benchmarks(d, mks, states, SQQQ_ENTRY)
    rows = []
    for name in ("QQQ", "ONEQ*", "T01_S2"):
        r = b[name]["ret"]
        rows.append({"series": name, "cagr": cagr_of(r), "max_dd": max_drawdown(equity0(r))})
    for pos in ("LS1", "FS1"):
        tgt = st.target(states["S2"], pos)
        for impl in ("margin", "psq", "sqqq"):
            sim = run_one(tgt, mks[impl], SQQQ_ENTRY, impl)
            r = sim["ret"]
            rows.append({"series": f"{pos}_S2_{impl}", "cagr": cagr_of(r), "max_dd": max_drawdown(equity0(r)),
                         "cagr_minus_oneq": cagr_of(r) - cagr_of(b["ONEQ*"]["ret"]),
                         "cagr_minus_longflat": cagr_of(r) - cagr_of(b["T01_S2"]["ret"]),
                         "share_short": share_in_state(sim["state_held"], "short"), "liquidations": sim["liquidations"]})
    return pd.DataFrame(rows)


def sensitivities(d: data.QQQData, mks_syn: dict, mks: dict, states: dict) -> pd.DataFrame:
    bb = benchmarks(d, mks, states, ENTRY)
    variants = {"borrow_1pct": ("margin", replace(RULES, borrow_fee=0.01), mks),
                "collateral_cash": ("margin", replace(RULES, collateral_in_bills=False), mks),
                "psq_synthetic_throughout": ("psq", RULES, mks_syn)}
    rows = []
    for tag, (impl, rules, mk) in variants.items():
        for tr in [t for t in st.qqq_trials() if t.impl == impl]:
            sim = run_one(st.target(states[tr.signal], tr.position), mk[impl], ENTRY, impl, rules)
            j = st.judge(sim["ret"], bb["ONEQ*"]["ret"], bb["QQQ"]["ret"], bb[f"T01_{tr.signal}"]["ret"], d.rf, N_TRIALS)
            f, h1, h2 = (j["metrics"][h] for h in ("full", "H1", "H2"))
            rows.append({"sensitivity": tag, "trial": tr.name, "cagr": f["cagr"], "cagr_minus_oneq": f["cagr_minus_oneq"],
                         "h1_cagr_minus_oneq": h1["cagr_minus_oneq"], "h2_cagr_minus_oneq": h2["cagr_minus_oneq"],
                         "cagr_minus_longflat": f["cagr_minus_longflat"], "max_dd": f["max_dd"],
                         "t_vs_oneq": f["t_monthly_excess_vs_oneq"], "pass_spliced": j["verdict"]["pass"],
                         "liquidations": sim["liquidations"]})
    return pd.DataFrame(rows)


# ======================================================================== index level

def index_level(idx: dict, dtb3: pd.Series, log: RunLog) -> dict:
    res, rows, ver_rows, sens_rows, win_rows, dec_rows = {}, [], [], [], [], []
    by_year = {}
    for sym, p in idx.items():
        r = p.pct_change(fill_method=None)
        tb = data.tbill_per_session(p.index, dtb3)
        borrow = data.calendar_days(p.index) * INDEX_BORROW / 360.0
        states = {sig: st.state(p, sig) for sig in st.SIGNALS}
        tg = {(sig, pos): st.target(states[sig], pos) for sig in st.SIGNALS for pos in (*st.POSITIONS, "T01")}
        s_i = intl_st.first_entry(list(tg.values()) + [tb], not_before=GSPC_NOT_BEFORE if sym == "^GSPC" else None)
        one = pd.Series(1.0, index=p.index)
        bh = simulate_index_ls(one, r, s_i, INDEX_COST)["ret"]
        lf = {sig: simulate_index_ls(tg[(sig, "T01")], r, s_i, INDEX_COST)["ret"] for sig in st.SIGNALS}
        log.say(f"{sym} entry {p.index[s_i].date()} BH CAGR {cagr_of(bh):+.2%} MDD {max_drawdown(equity0(bh)):.1%} "
                f"T01_S2 {cagr_of(lf['S2']):+.2%} T01_S0 {cagr_of(lf['S0']):+.2%}")
        series = {f"{sym}_BH": bh, **{f"{sym}_T01_{s}": x for s, x in lf.items()}}
        by_year[f"{sym}_BH"] = yearly(bh)
        for sig in st.SIGNALS:
            for pos in st.POSITIONS:
                name = f"{pos}_{sig}_{sym}"
                sim = simulate_index_ls(tg[(sig, pos)], r, s_i, INDEX_COST, credit=tb, borrow=borrow)
                x = sim["ret"]
                assert x.index.equals(bh.index)
                assert_dev_dates(x.index, END)
                j = st.judge(x, bh, lf[sig], lf[sig], tb, N_TRIALS)
                yrs = len(x) / TRADING_DAYS
                ep = state_episodes(sim["value"], sim["state"], r)
                info = {**j, "entry_close": str(p.index[s_i].date()), "switches": sim["switches"],
                        "switches_per_year": sim["switches"] / yrs,
                        "share_short": share_in_state(sim["state"].shift(1).iloc[1:], "short"),
                        "short_episodes": episode_summary(ep, "short"), "long_episodes": episode_summary(ep, "long")}
                res[name] = info
                v = j["verdict"]
                ver_rows.append({"trial": name, "index": sym, "signal": sig, "position": pos, "A": v["A"], "B": v["B"],
                                 "pass": v["pass"], "A_bonferroni": v["A_bonferroni"],
                                 "C_short_leg_adds_value": j["C_short_leg_adds_value"], "split": j["split"]})
                for h in ("full", "H1", "H2"):
                    m = j["metrics"][h]
                    rows.append({"trial": name, "period": h, "start": m["start"], "end": m["end"], "cagr": m["cagr"],
                                 "bh_cagr": m["oneq_cagr"], "longflat_cagr": m["longflat_cagr"],
                                 "cagr_minus_bh": m["cagr_minus_oneq"], "cagr_minus_longflat": m["cagr_minus_longflat"],
                                 "max_dd": m["max_dd"], "bh_max_dd": m["oneq_max_dd"], "longflat_max_dd": m["longflat_max_dd"],
                                 "dd_shallower_than_bh_pp": m["dd_shallower_than_oneq_pp"], "worst_month": m["worst_month"],
                                 "worst_month_ret": m["worst_month_ret"], "t_vs_bh": m["t_monthly_excess_vs_oneq"],
                                 "t_vs_longflat": m["t_monthly_excess_vs_longflat"],
                                 "share_short": info["share_short"] if h == "full" else None,
                                 "switches_per_year": info["switches_per_year"] if h == "full" else None,
                                 "short_episodes": info["short_episodes"].get("episodes") if h == "full" else None,
                                 "short_losing": info["short_episodes"].get("losing") if h == "full" else None})
                f = j["metrics"]["full"]
                log.say(f"  {name:18s} CAGR {f['cagr']:+.2%} vs BH {f['oneq_cagr']:+.2%} (H1 {j['metrics']['H1']['cagr_minus_oneq']:+.2%}"
                        f" H2 {j['metrics']['H2']['cagr_minus_oneq']:+.2%}) vs T01 {f['cagr_minus_longflat']:+.2%} MDD {f['max_dd']:.1%}"
                        f" (BH {f['oneq_max_dd']:.1%}) t {f['t_monthly_excess_vs_oneq']:+.2f} short {info['share_short']:.0%}"
                        f" sw/yr {info['switches_per_year']:.1f} | A {v['A']} B {v['B']} C {j['C_short_leg_adds_value']}")
                ns = simulate_index_ls(tg[(sig, pos)], r, s_i, INDEX_COST, credit=None, borrow=borrow)["ret"]
                js = st.judge(ns, bh, lf[sig], lf[sig], tb, N_TRIALS)
                fs = js["metrics"]["full"]
                sens_rows.append({"sensitivity": "index_no_tbill_credit", "trial": name, "cagr": fs["cagr"],
                                  "cagr_minus_bh": fs["cagr_minus_oneq"],
                                  "h1_cagr_minus_bh": js["metrics"]["H1"]["cagr_minus_oneq"],
                                  "h2_cagr_minus_bh": js["metrics"]["H2"]["cagr_minus_oneq"],
                                  "cagr_minus_longflat": fs["cagr_minus_longflat"], "max_dd": fs["max_dd"],
                                  "t_vs_bh": fs["t_monthly_excess_vs_oneq"], "pass": js["verdict"]["pass"]})
                series[name] = x
                by_year[name] = yearly(x)
        win_rows += window_table(series, INDEX_WINDOWS)
        for name, x in series.items():
            for row in intl_st.decade_table(x, bh, min_sessions=480):
                dec_rows.append({"series": name, **row})
    return {"trials": res, "summary": pd.DataFrame(rows), "verdicts": pd.DataFrame(ver_rows),
            "sens": pd.DataFrame(sens_rows), "windows": pd.DataFrame(win_rows), "by_decade": pd.DataFrame(dec_rows),
            "by_year": by_year}


# ======================================================================== post-hoc review checks (report only)
# Added 2026-10-21 after the execution and statistics reviews, to back the corrected sentences of the ledger. No new
# trial, no registered rule, result file or verdict changes: everything goes to posthoc_review.json,
# posthoc_qqq.csv and posthoc_index_bases.csv.

def posthoc_rf(d: data.QQQData, log: RunLog) -> tuple[dict, pd.DataFrame]:
    """The registered T-bill (Ken French daily RF, whole basis points per day) against DTB3 / 252: the steps, the
    yearly levels, the PSQ / SQQQ validation and the QQQ-family CAGRs with the unquantised rate."""
    s = d.sessions
    w = s[(s > pd.Timestamp(ENTRY)) & (s <= pd.Timestamp(END))]
    dtb3 = load_dtb3(END)
    d2 = replace(d, rf=data.daily_tbill_unquantised(s, dtb3))
    kf = d.rf.reindex(w)
    years = sorted(set(w.year))
    info = {"french_rf_daily_values_bp": sorted({round(float(x) * 1e4, 6) for x in kf}),
            "by_year": {int(y): {"french_rf_x252": float(kf[kf.index.year == y].mean() * TRADING_DAYS),
                                 "dtb3_mean": float(dtb3[dtb3.index.year == y].mean())} for y in years}}
    invs = {"french_rf": inverse_validation(d), "dtb3": inverse_validation(d2)}
    info["inverse_validation"] = {}
    for tag, inv in invs.items():
        for sym, v in inv.items():
            val = v["validation_frozen_spread"]
            info["inverse_validation"][f"{sym}_{tag}"] = {
                "spread": v["calibration"]["spread_calibrated"], "cagr_gap": val["cagr_gap_syn_minus_real"],
                "te_monthly": val["tracking_error_monthly_ann"], "corr": val["corr"],
                "slope_on_rf": val["gap_slope_on_daily_rf"], "gap_by_year": val["gap_by_year"], "acceptable": v["acceptable"]}
            log.say(f"[posthoc rf] {sym} with {tag}: spread {v['calibration']['spread_calibrated']:+.2%} gap "
                    f"{val['cagr_gap_syn_minus_real']:+.2%} TE m {val['tracking_error_monthly_ann']:.2%} slope(rf) "
                    f"{val['gap_slope_on_daily_rf']:+.2f}")
    rows = []
    for tag, dd_ in (("french_rf", d), ("dtb3", d2)):
        inv = invs[tag]
        mks = markets(dd_, inv["PSQ"]["calibration"]["spread_calibrated"], inv["SQQQ"]["calibration"]["spread_calibrated"])
        states = {sig: st.state(dd_.level, sig) for sig in st.SIGNALS}
        for start in (ENTRY, ONEQ_ENTRY, RECENT_ENTRY):
            b = benchmarks(dd_, mks, states, start)
            sims = {**b, **{tr.name: run_one(st.target(states[tr.signal], tr.position), mks[tr.impl], start, tr.impl)
                            for tr in st.qqq_trials()}}
            rows += [{"rf": tag, "entry": start, "series": k, "cagr": cagr_of(v["ret"])} for k, v in sims.items()]
    df = pd.DataFrame(rows).pivot_table(index=["entry", "series"], columns="rf", values="cagr").reset_index()
    df["dtb3_minus_french"] = df["dtb3"] - df["french_rf"]
    info["qqq_cagr_change_max_abs"] = float(df["dtb3_minus_french"].abs().max())
    log.say(f"[posthoc rf] QQQ-family CAGR change with DTB3 / 252: max |change| {info['qqq_cagr_change_max_abs']:.2%}")
    return info, df


def posthoc_qqq(d: data.QQQData, mks: dict, log: RunLog) -> pd.DataFrame:
    """Per registered QQQ-family series on both bases: the max drawdown with its dates, the drawdown left at the end,
    the first close with NAV >= $100k (IBKR credits short proceeds from there), the dollar interest / fee totals,
    and the comparison with an all-cash account (the T-bill ETF; what the short-only FS1 has to beat)."""
    states = {sig: st.state(d.level, sig) for sig in st.SIGNALS}
    rows = []
    for basis, start in (("spliced", ENTRY), ("oneq", ONEQ_ENTRY)):
        b = benchmarks(d, mks, states, start)
        cash = run_one(pd.Series(0.0, index=d.sessions), mks["margin"], start, "margin")["ret"]
        sims = {k: b[k] for k in ("T01_S2", "T01_S0")}
        sims.update({tr.name: run_one(st.target(states[tr.signal], tr.position), mks[tr.impl], start, tr.impl)
                     for tr in st.qqq_trials()})
        for name, sim in sims.items():
            v = sim["value"]
            over = v[v >= 100_000.0]
            rows.append({"basis": basis, "series": name, "cagr": cagr_of(sim["ret"]), "cash_cagr": cagr_of(cash),
                         "cagr_minus_cash": cagr_of(sim["ret"]) - cagr_of(cash),
                         "t_vs_cash": t_monthly_excess(sim["ret"], cash), **drawdown_info(v),
                         "dd_at_end": float(v.iloc[-1] / v.max() - 1),
                         "first_nav_100k": str(over.index[0].date()) if len(over) else None,
                         **{f"{k}_usd": float(sim[k]) for k in ("margin_interest", "credit_interest", "borrow",
                                                                 "dividends", "costs")}})
    df = pd.DataFrame(rows)
    for r in df[(df["basis"] == "spliced") & df["series"].isin(["LS1_S2_margin", "FS1_S2_margin"])].itertuples():
        log.say(f"[posthoc qqq] {r.series}: max DD {r.max_dd:.1%} peak {r.dd_peak} trough {r.dd_trough} recovered "
                f"{r.dd_recovered}; DD at the end {r.dd_at_end:.1%}; NAV >= 100k from {r.first_nav_100k}")
    return df


def posthoc_index(idx: dict, dtb3: pd.Series, log: RunLog) -> tuple[dict, pd.DataFrame]:
    """The index level on four bases. ``as_registered`` = section 0.5 (price index, cash 0%, short credited with the
    T-bill); ``tbill_cash_no_div`` = cash and the owner's equity earn the T-bill, short proceeds earn nothing (the
    $10k IBKR account), no dividends; ``total_return_10k`` = the same plus a dividend yield (received long, paid
    short: measured from an ETF where one overlaps, a rough assumed yield before); ``total_return_proceeds_tbill`` =
    that with the short proceeds also earning the T-bill (a large account or index futures). T01 rows are the
    long/flat rule judged against buy-and-hold."""
    rows, yields = [], {}
    for sym, p in idx.items():
        etf, fee = POSTHOC_ETF[sym]
        y, since = data.etf_dividend_yield(data.INTL_RAW / f"chart_{etf}.json", p, fee, END)
        yields[sym] = {"etf": etf, "measured_from": since, "measured_yield": y, "assumed_before": POSTHOC_DIV_BEFORE[sym]}
        r = p.pct_change(fill_method=None)
        rtr = r + data.dividend_per_session(p.index, since, y, POSTHOC_DIV_BEFORE[sym])
        tb = data.tbill_per_session(p.index, dtb3)
        tbf = tb.fillna(0.0)
        borrow = data.calendar_days(p.index) * INDEX_BORROW / 360.0
        states = {sig: st.state(p, sig) for sig in st.SIGNALS}
        tg = {(sig, pos): st.target(states[sig], pos) for sig in st.SIGNALS for pos in (*st.POSITIONS, "T01")}
        s_i = intl_st.first_entry(list(tg.values()) + [tb], not_before=GSPC_NOT_BEFORE if sym == "^GSPC" else None)
        one = pd.Series(1.0, index=p.index)
        bases = {"as_registered": (r, 0.0, tb), "tbill_cash_no_div": (r, tbf, -tbf),
                 "total_return_10k": (rtr, tbf, -tbf), "total_return_proceeds_tbill": (rtr, tbf, None)}
        for basis, (rr, cash, credit) in bases.items():
            bh = simulate_index_ls(one, rr, s_i, INDEX_COST, cash=cash)["ret"]
            for sig in st.SIGNALS:
                lf = simulate_index_ls(tg[(sig, "T01")], rr, s_i, INDEX_COST, cash=cash)["ret"]
                runs = {f"T01_{sig}_{sym}": lf}
                for pos in st.POSITIONS:
                    runs[f"{pos}_{sig}_{sym}"] = simulate_index_ls(tg[(sig, pos)], rr, s_i, INDEX_COST, credit=credit,
                                                                    borrow=borrow, cash=cash)["ret"]
                for name, x in runs.items():
                    with np.errstate(invalid="ignore", divide="ignore"):   # T01 rows: t against itself is 0 / 0
                        j = st.judge(x, bh, lf, lf, tb, N_TRIALS)
                    m = j["metrics"]
                    rows.append({"basis": basis, "trial": name, "index": sym, "cagr": m["full"]["cagr"],
                                 "bh_cagr": m["full"]["oneq_cagr"], "cagr_minus_bh": m["full"]["cagr_minus_oneq"],
                                 "h1_minus_bh": m["H1"]["cagr_minus_oneq"], "h2_minus_bh": m["H2"]["cagr_minus_oneq"],
                                 **{f"dd_shallower_pp_{h}": m[h]["dd_shallower_than_oneq_pp"] for h in m},
                                 "cagr_minus_longflat": m["full"]["cagr_minus_longflat"],
                                 "t_vs_longflat": m["full"]["t_monthly_excess_vs_longflat"],
                                 "tbill_cagr": cagr_of(tbf.loc[x.index]), "cagr_minus_tbill": m["full"]["cagr"] -
                                 cagr_of(tbf.loc[x.index]), "A": j["verdict"]["A"], "B": j["verdict"]["B"],
                                 "pass": j["verdict"]["pass"],
                                 "C": j["C_short_leg_adds_value"] if not name.startswith("T01") else None})
    df = pd.DataFrame(rows)
    reg = pd.read_csv(OUT / "index_summary.csv")
    reg = reg[reg["period"] == "full"].set_index("trial")["cagr"]
    mine = df[(df["basis"] == "as_registered") & ~df["trial"].str.startswith("T01")].set_index("trial")["cagr"]
    gap = float((mine - reg.reindex(mine.index)).abs().max())
    assert gap < 2e-6, f"as_registered does not reproduce index_summary.csv ({gap})"
    tr = df[~df["trial"].str.startswith("T01")]
    for basis, g in tr.groupby("basis", sort=False):
        log.say(f"[posthoc index] {basis}: pass {int(g['pass'].sum())}/18 {sorted(g.loc[g['pass'], 'trial'])}, "
                f"C {int(g['C'].sum())}, vs T01 {g['cagr_minus_longflat'].min():+.2%}..{g['cagr_minus_longflat'].max():+.2%}")
    return {"dividend_yields": yields, "as_registered_max_abs_cagr_gap": gap}, df


def posthoc(d: data.QQQData, idx: dict, log: RunLog) -> dict:
    inv = json.loads((OUT / "inverse_validation.json").read_text())
    mks = markets(d, inv["PSQ"]["calibration"]["spread_calibrated"], inv["SQQQ"]["calibration"]["spread_calibrated"])
    rf_info, rf_df = posthoc_rf(d, log)
    q = posthoc_qqq(d, mks, log)
    idx_info, idx_df = posthoc_index(idx, load_dtb3(END), log)
    out = {"note": "post-hoc review checks of 2026-10-21; report only (no trial, no verdict)", "rf": rf_info,
           "index": idx_info}
    write_json(OUT / "posthoc_review.json", out, default=float)
    rf_df.to_csv(OUT / "posthoc_rf_cagr.csv", index=False, float_format="%.6f")
    q.to_csv(OUT / "posthoc_qqq.csv", index=False, float_format="%.6f")
    idx_df.to_csv(OUT / "posthoc_index_bases.csv", index=False, float_format="%.6f")
    log.write(OUT / "run_log_posthoc.txt")
    return out


# ======================================================================== main

def run(args=None) -> dict:
    log = RunLog()
    OUT.mkdir(parents=True, exist_ok=True)
    if getattr(args, "fetch", False):
        inverse_etfs.fetch(data.RAW, data.CACHE, FETCH_PERIOD2, log=log.say)
    d = data.load_qqq(END)
    idx = data.load_indices(END)
    checks = {"guard": d.guard, **data.data_checks(d, idx, ENTRY, END)}
    write_json(OUT / "data_checks.json", checks, default=float)
    log.say("data:", json.dumps({k: v for k, v in checks.items() if k != "guard"}, default=str)[:2000])
    pre = Preregistration(LEDGER, OUT / "prereg_frozen.json", OUT)
    if getattr(args, "data_only", False):
        log.write(OUT / "run_log_data.txt")
        return checks
    if getattr(args, "register", False):
        pre.register()
        return {}
    frozen = json.loads((OUT / "prereg_frozen.json").read_text())
    if frozen["sha256"] != pre.hash():
        raise SystemExit("section 0 of the ledger changed after registration")
    if getattr(args, "posthoc", False):
        return posthoc(d, idx, log)
    log.say(f"trials {N_TRIALS}, Bonferroni t {bonferroni_t(N_TRIALS):.3f}; prereg {frozen['sha256'][:12]}")
    inv = inverse_validation(d)
    for k, v in inv.items():
        val = v["validation_frozen_spread"]
        log.say(f"{k}: spread {v['calibration']['spread_calibrated']:+.4%}/yr on {v['calibration']['window']}; "
                f"2015-2026 CAGR syn {val['cagr_synthetic']:+.2%} real {val['cagr_real']:+.2%} gap {val['cagr_gap_syn_minus_real']:+.2%}"
                f" TE m {val['tracking_error_monthly_ann']:.2%} corr {val['corr']:.4f} slope(rf) {val['gap_slope_on_daily_rf']:+.2f}"
                f" acceptable {v['acceptable']}")
    sp, ss = inv["PSQ"]["calibration"]["spread_calibrated"], inv["SQQQ"]["calibration"]["spread_calibrated"]
    mks = markets(d, sp, ss)
    mks_syn = markets(d, sp, ss, synthetic_psq=True)
    q = qqq_family(d, mks, log)
    recent = recent_window(d, mks, q["states"], log)
    sq = sqqq_report(d, mks, q["states"])
    sens = sensitivities(d, mks_syn, mks, q["states"])
    il = index_level(idx, load_dtb3(END), log)
    n_pass = int(q["verdicts"]["pass"].sum())
    log.say(f"QQQ family: {n_pass}/{len(q['verdicts'])} pass (both bases); C (adds to T01) spliced "
            f"{int(q['verdicts']['C_spliced'].sum())}; index level: {int(il['verdicts']['pass'].sum())}/{len(il['verdicts'])} pass,"
            f" C {int(il['verdicts']['C_short_leg_adds_value'].sum())}")
    res = {"end": END, "n_trials": N_TRIALS, "bonferroni_t_one_sided_5pct": bonferroni_t(N_TRIALS),
           "prereg_sha256": frozen["sha256"], "margin_rules": RULES.__dict__, "inverse_validation": inv,
           "t01_engine_gap_vs_frozen_engine": q["t01_engine_gap"], "qqq_family": q["trials"],
           "qqq_pass_count": n_pass, "index_level": il["trials"]}
    write_json(OUT / "results.json", res, default=float)
    write_json(OUT / "inverse_validation.json", inv, default=float)
    q["summary"].to_csv(OUT / "qqq_summary.csv", index=False, float_format="%.6f")
    q["verdicts"].to_csv(OUT / "qqq_verdicts.csv", index=False)
    q["details"].to_csv(OUT / "qqq_details.csv", index=False, float_format="%.6f")
    q["episodes"].to_csv(OUT / "qqq_episodes.csv", index=False, float_format="%.6f")
    q["windows"].to_csv(OUT / "qqq_windows.csv", index=False, float_format="%.6f")
    q["by_decade"].to_csv(OUT / "qqq_by_decade.csv", index=False, float_format="%.6f")
    recent.to_csv(OUT / "qqq_2015_2026.csv", index=False, float_format="%.6f")
    sq.to_csv(OUT / "sqqq_2010_2026.csv", index=False, float_format="%.6f")
    pd.concat([sens, il["sens"]], ignore_index=True).to_csv(OUT / "sensitivities.csv", index=False, float_format="%.6f")
    il["summary"].to_csv(OUT / "index_summary.csv", index=False, float_format="%.6f")
    il["verdicts"].to_csv(OUT / "index_verdicts.csv", index=False)
    il["windows"].to_csv(OUT / "index_windows.csv", index=False, float_format="%.6f")
    il["by_decade"].to_csv(OUT / "index_by_decade.csv", index=False, float_format="%.6f")
    write_period_tables(OUT, q["by_year"], q["monthly"], prefix="qqq_")
    pd.DataFrame(il["by_year"]).to_csv(OUT / "index_by_year.csv", float_format="%.6f")
    log.write(OUT / "run_log.txt")
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fetch", action="store_true", help="download missing PSQ / SQQQ charts first (cached, logged)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--data-only", action="store_true", help="only the data coverage report (no rule results)")
    g.add_argument("--register", action="store_true", help="write the SHA-256 of ledger section 0")
    g.add_argument("--run", action="store_true", help="the registered run (default)")
    g.add_argument("--posthoc", action="store_true", help="the post-hoc review checks (report only; after --run)")
    run(ap.parse_args(argv))


if __name__ == "__main__":
    main()
