"""International: do the US index timing rules replicate abroad, and can a country rotation beat ONEQ?
(pre-registered in docs/research_ledger_international.md, section 0).

Part 1 (replication, quant.strategies.international): I1 200-day SMA 2% band, I2 12-month absolute momentum, I3 20%
vol target, on each of 11 local-currency price indices, against that index's own buy-and-hold (same price basis,
cash at 0%), 10 bp per unit traded, decided at the close of t and traded at the next session's close.
Part 2 (rotation vs ONEQ): R1 / R1b / R1c / R2 month-end relative + absolute momentum over US-listed ETFs
(total-return closes), IBKR Tiered + half-spread (``etf_order_cost``), T-bill ETF as the cash leg.
Data (local only): research_cache/international/raw (Yahoo chart API, quant.data.international), Ken French RF +
FRED DTB3 (quant.data.rates). Every frame truncated at ``END``. Outputs: output/research_only/international/
(returns and metrics only, never vendor price levels).

Usage:  PYTHONPATH=. .venv/bin/python -m quant study international [--fetch] [--data-only]
"""
from __future__ import annotations

import argparse
import json
import math
from types import SimpleNamespace

import numpy as np
import pandas as pd

from quant.backtest.target_weights import buy_hold_weights, entry_index, simulate_target_weights
from quant.data import international as intl
from quant.data.guards import assert_dev_dates
from quant.data.rates import daily_rf, load_dtb3
from quant.data.synthetic import CASH_ETF_FEE, NOMINAL_PRICE
from quant.evaluation.criteria import A_T_MIN, ab_criteria, ab_verdict, bonferroni_t
from quant.evaluation.equity import period_metrics
from quant.evaluation.metrics import TRADING_DAYS, max_drawdown, monthly, yearly
from quant.evaluation.periods import halves
from quant.paths import ROOT, output_dir
from quant.reporting import Preregistration, RunLog, write_json, write_period_tables
from quant.signals.technical import trailing_compound
from quant.strategies import international as st

END = "2026-09-30"
FETCH_PERIOD2 = "2026-10-03"
P = st.Params()
NON_US = tuple(s for s in intl.INDICES if s not in intl.US_INDICES)
N_TRIALS = len(st.INDEX_RULES) * len(intl.INDICES) + 4
RECENT = ("2015-01-01", END)
LATE_START = "1980-01-01"
ROTATIONS = {
    "R1": (("QQQ", "SPY", "EFA", "EEM"), 1),
    "R1b": (("QQQ", "SPY", "EFA", "EEM"), 2),
    "R1c": (("QQQ", "SPY", "EWJ", "EWG", "EWU", "EWH", "EWC", "EWA", "EWY"), 1),
    "R2": (("QQQ", "EFA"), 1),
}
HALF_SPREAD = {"QQQ": 1e-4, "SPY": 1e-4, "EFA": 1e-4, "CASH": 1e-4, "EEM": 2e-4, "ONEQ": 2e-4,
               **{t: 3e-4 for t in ("EWJ", "EWG", "EWU", "EWC", "EWA")}, "EWH": 5e-4, "EWY": 5e-4}
EXPECTED_ROT_ENTRY = {"R1": "2004-05-03", "R1b": "2004-05-03", "R1c": "2003-10-01", "R2": "2003-10-01"}
OUT = output_dir("international")
LEDGER = ROOT / "docs/research_ledger_international.md"


# ======================================================================== data report (no rule results)

def data_report(d: intl.Data, log: RunLog) -> dict:
    checks = {s: intl.series_checks(p) | {"zero_return_share_by_decade": intl.zero_return_share_by_decade(p)}
              for s, p in d.index.items()}
    etf_checks = {s: intl.series_checks(d.etf_adj[s]) for s in intl.ETFS}
    cur = {}
    for s, (market, ccy, fx, inv) in intl.INDICES.items():
        e = intl.INDEX_ETF[s]
        cur[s] = {"market": market, "currency": ccy, "etf": e,
                  **intl.etf_vs_index(d.index[s], d.etf_adj[e], d.fx.get(fx) if fx else None, inv)}
    for s, c in checks.items():
        log.say(f"{s:10s} {c['first']}..{c['last']} rows {c['rows']} sessions/yr {c['sessions_per_full_year_min']}-"
                f"{c['sessions_per_full_year_max']} gap {c['longest_gap_calendar_days']}d zero-ret {c['share_zero_returns']:.2%}"
                f" |move|>15% {c['days_abs_move_gt_15pct']}")
    return {"guard": d.guard, "index_checks": checks, "etf_checks": etf_checks, "currency_decomposition": cur}


# ======================================================================== part 1: index-level metrics

def cal_cagr(r: pd.Series) -> float:
    """CAGR by calendar time (the markets have 226-259 sessions a year): days from the session before the first
    return to the last one, approximated by adding one average session gap."""
    span = (r.index[-1] - r.index[0]).days
    days = span + (span / (len(r) - 1) if len(r) > 1 else 1.0)
    return float(np.prod(1 + r.values) ** (365.25 / days) - 1)


def value_dd(r: pd.Series) -> float:
    v = np.concatenate([[1.0], np.cumprod(1 + r.values)])
    return float((v / np.maximum.accumulate(v) - 1).min())


def t_monthly(r: pd.Series, b: pd.Series) -> float:
    x = monthly(r) - monthly(b)
    s = x.std()
    return float(x.mean() / s * math.sqrt(len(x))) if s > 0 else float("nan")


def idx_metrics(r: pd.Series, b: pd.Series, held: pd.Series) -> dict:
    c, bc, dd, bdd = cal_cagr(r), cal_cagr(b), value_dd(r), value_dd(b)
    spy = len(r) / ((r.index[-1] - r.index[0]).days / 365.25)
    return {"start": str(r.index[0].date()), "end": str(r.index[-1].date()), "sessions": int(len(r)),
            "cagr": c, "bh_cagr": bc, "cagr_minus_bh": c - bc, "max_dd": dd, "bh_max_dd": bdd,
            "dd_shallower_pp": (abs(bdd) - abs(dd)) * 100, "vol": float(r.std() * math.sqrt(spy)),
            "bh_vol": float(b.std() * math.sqrt(spy)), "t_monthly_excess": t_monthly(r, b),
            "avg_exposure": float(held.loc[r.index].mean()), "share_out": float((held.loc[r.index] < 1e-9).mean())}


def judge_index(r: pd.Series, b: pd.Series, held: pd.Series) -> dict:
    i1, i2 = halves(r.index)
    m = {"full": idx_metrics(r, b, held), "H1": idx_metrics(r.loc[i1], b.loc[i1], held),
         "H2": idx_metrics(r.loc[i2], b.loc[i2], held)}
    parts = (m["full"], m["H1"], m["H2"])
    a, bb = ab_criteria(parts, m["full"], cagr="cagr", bench="bh_cagr", t="t_monthly_excess", dd="dd_shallower_pp")
    a_bonf = all(p["cagr"] > p["bh_cagr"] for p in parts) and m["full"]["t_monthly_excess"] >= bonferroni_t(N_TRIALS)
    return {"metrics": m, "verdict": {"A": a, "B": bb, "pass": bool(a or bb), "A_bonferroni": bool(a_bonf),
                                      "pass_bonferroni": bool(a_bonf or bb)}}


def cash_tbill(p: pd.Series, dtb3: pd.Series) -> pd.Series:
    """US T-bill accrued per calendar day between sessions of ``p``'s calendar (0 before DTB3 starts in 1954)."""
    y = dtb3.reindex(dtb3.index.union(p.index)).ffill().reindex(p.index).fillna(0.0)
    gap = pd.Series(p.index, index=p.index).diff().dt.days.fillna(1).values
    return pd.Series((1 + y.shift(1).fillna(0.0).values) ** (gap / 365.0) - 1, index=p.index)


def run_market(sym: str, p: pd.Series, dtb3: pd.Series, *, cost=P.switch_cost, tbill=False,
               not_before: str | None = None) -> dict:
    r = p.pct_change(fill_method=None)
    cash = cash_tbill(p, dtb3) if tbill else 0.0
    hurdle = trailing_compound(cash, P.mom_len) if tbill else None
    tg = {k: st.index_target(k, p, P, hurdle if k == "I2" else None) for k in st.INDEX_RULES}
    s_i = st.first_entry(list(tg.values()), not_before=not_before)
    bh = st.simulate_exposure(pd.Series(1.0, index=p.index), r, cash, s_i, cost)
    out = {"entry_close": str(p.index[s_i].date()), "bh": bh, "rules": {}}
    for k, e in tg.items():
        out["rules"][k] = st.simulate_exposure(e, r, cash, s_i, cost)
    return out


def part1(d: intl.Data, log: RunLog) -> dict:
    dtb3 = load_dtb3(END)
    res, rows, dec_rows, recent_rows, sens_rows = {}, [], [], [], []
    by_year, mon = {}, {}
    for sym in intl.INDICES:
        p = d.index[sym]
        market = intl.INDICES[sym][0]
        m = run_market(sym, p, dtb3)
        b = m["bh"]["ret"]
        i1, i2 = halves(b.index)
        res[sym] = {"market": market, "entry_close": m["entry_close"], "halves_split": str(i2[0].date()), "rules": {}}
        log.say(f"{sym} ({market}) entry {m['entry_close']} split {i2[0].date()} BH CAGR {cal_cagr(b):+.2%} MDD {value_dd(b):.1%}")
        by_year[f"{sym}_BH"], mon[f"{sym}_BH"] = yearly(b), monthly(b)
        for k, sim in m["rules"].items():
            r = sim["ret"]
            assert r.index.equals(b.index)
            assert_dev_dates(r.index, END)
            j = judge_index(r, b, sim["held"])
            yrs = (r.index[-1] - r.index[0]).days / 365.25
            info = {**j, "switches": sim["switches"], "switches_per_year": sim["switches"] / yrs,
                    "turnover_per_year": sim["turnover"] / yrs}
            # report only: decades, the 2015-2026 window, sensitivities
            dec = st.decade_table(r, b)
            info["by_decade"] = dec
            for x in dec:
                dec_rows.append({"index": sym, "market": market, "rule": k, **x})
            rr, bb = r.loc[RECENT[0]:RECENT[1]], b.loc[RECENT[0]:RECENT[1]]
            rec = {"cagr": cal_cagr(rr), "bh_cagr": cal_cagr(bb), "cagr_minus_bh": cal_cagr(rr) - cal_cagr(bb),
                   "max_dd": value_dd(rr), "bh_max_dd": value_dd(bb),
                   "dd_shallower_pp": (abs(value_dd(bb)) - abs(value_dd(rr))) * 100, "t_monthly_excess": t_monthly(rr, bb)}
            info["window_2015_2026"] = rec
            recent_rows.append({"index": sym, "market": market, "rule": k, **rec})
            res[sym]["rules"][k] = info
            f, h1, h2 = j["metrics"]["full"], j["metrics"]["H1"], j["metrics"]["H2"]
            v = j["verdict"]
            rows.append({"index": sym, "market": market, "us": sym in intl.US_INDICES, "rule": k,
                         "start": f["start"], "end": f["end"], "years": yrs, "cagr": f["cagr"], "bh_cagr": f["bh_cagr"],
                         "cagr_minus_bh": f["cagr_minus_bh"], "h1_cagr_minus_bh": h1["cagr_minus_bh"],
                         "h2_cagr_minus_bh": h2["cagr_minus_bh"], "max_dd": f["max_dd"], "bh_max_dd": f["bh_max_dd"],
                         "dd_shallower_pp": f["dd_shallower_pp"], "h1_dd_shallower_pp": h1["dd_shallower_pp"],
                         "h2_dd_shallower_pp": h2["dd_shallower_pp"], "t_monthly_excess": f["t_monthly_excess"],
                         "vol": f["vol"], "bh_vol": f["bh_vol"], "avg_exposure": f["avg_exposure"],
                         "share_out": f["share_out"], "switches_per_year": info["switches_per_year"],
                         "A": v["A"], "B": v["B"], "pass": v["pass"], "A_bonferroni": v["A_bonferroni"],
                         "pass_bonferroni": v["pass_bonferroni"]})
            by_year[f"{sym}_{k}"], mon[f"{sym}_{k}"] = yearly(r), monthly(r)
            log.say(f"  {k} CAGR {f['cagr']:+.2%} vs BH {f['bh_cagr']:+.2%} (H1 {h1['cagr_minus_bh']:+.2%} H2 {h2['cagr_minus_bh']:+.2%})"
                    f" MDD {f['max_dd']:.1%} vs {f['bh_max_dd']:.1%} (+{f['dd_shallower_pp']:.1f}pp; H1 {h1['dd_shallower_pp']:+.1f}"
                    f" H2 {h2['dd_shallower_pp']:+.1f}) t {f['t_monthly_excess']:+.2f} out {f['share_out']:.0%}"
                    f" sw/yr {info['switches_per_year']:.1f} | A {v['A']} B {v['B']} pass {v['pass']}")
        # sensitivities (report only)
        variants = {"cost_0bp": dict(cost=0.0), "cost_25bp": dict(cost=0.0025), "cash_us_tbill": dict(tbill=True)}
        if pd.Timestamp(m["entry_close"]) < pd.Timestamp(LATE_START):
            variants["start_1980"] = dict(not_before=LATE_START)
        for tag, kw in variants.items():
            s = run_market(sym, p, dtb3, **kw)
            bs = s["bh"]["ret"]
            for k, sim in s["rules"].items():
                js = judge_index(sim["ret"], bs, sim["held"])
                fs = js["metrics"]["full"]
                sens_rows.append({"index": sym, "rule": k, "sensitivity": tag, "entry_close": s["entry_close"],
                                  "cagr": fs["cagr"], "bh_cagr": fs["bh_cagr"], "cagr_minus_bh": fs["cagr_minus_bh"],
                                  "h1_cagr_minus_bh": js["metrics"]["H1"]["cagr_minus_bh"],
                                  "h2_cagr_minus_bh": js["metrics"]["H2"]["cagr_minus_bh"],
                                  "dd_shallower_pp": fs["dd_shallower_pp"], "t_monthly_excess": fs["t_monthly_excess"],
                                  "A": js["verdict"]["A"], "B": js["verdict"]["B"], "pass": js["verdict"]["pass"]})
                res[sym]["rules"][k].setdefault("sensitivities", {})[tag] = sens_rows[-1]
    table = pd.DataFrame(rows)
    counts = []
    for k in st.INDEX_RULES:
        t = table[table["rule"] == k]
        nu, us = t[~t["us"]], t[t["us"]]
        n_pass = int(nu["pass"].sum())
        verdict = "broad" if n_pass >= 5 else ("mixed" if n_pass >= 3 else "does_not_replicate")
        counts.append({"rule": k, "non_us_markets": len(nu), "non_us_pass": n_pass, "non_us_A": int(nu["A"].sum()),
                       "non_us_B": int(nu["B"].sum()), "non_us_pass_bonferroni": int(nu["pass_bonferroni"].sum()),
                       "non_us_cagr_above_bh_full": int((nu["cagr_minus_bh"] > 0).sum()),
                       "non_us_dd_10pp_shallower_full": int((nu["dd_shallower_pp"] >= 10).sum()),
                       "non_us_median_cagr_minus_bh": float(nu["cagr_minus_bh"].median()),
                       "non_us_median_dd_shallower_pp": float(nu["dd_shallower_pp"].median()),
                       "non_us_median_t": float(nu["t_monthly_excess"].median()),
                       "us_pass": int(us["pass"].sum()), "us_markets": len(us),
                       "replication_verdict": verdict})
        log.say(f"{k}: passes in {n_pass}/{len(nu)} non-US markets (A {int(nu['A'].sum())}, B {int(nu['B'].sum())});"
                f" US {int(us['pass'].sum())}/{len(us)} -> {verdict}")
    return {"markets": res, "table": table, "counts": pd.DataFrame(counts), "by_decade": pd.DataFrame(dec_rows),
            "recent": pd.DataFrame(recent_rows), "sens": pd.DataFrame(sens_rows), "by_year": by_year, "monthly": mon}


# ======================================================================== part 2: rotation vs ONEQ

def etf_panel(d: intl.Data):
    sessions = d.etf_adj["QQQ"].index
    adj, close = {}, {}
    for s in intl.ETFS:
        a = d.etf_adj[s]
        a = a[a.index >= sessions[0]]
        extra = a.index.difference(sessions)
        if len(extra):
            raise ValueError(f"{s} has sessions not in the QQQ calendar: {list(extra[:5])}")
        adj[s] = a.reindex(sessions)
        close[s] = d.etf_close[s].reindex(sessions)
    adj, close = pd.DataFrame(adj), pd.DataFrame(close)
    rf = daily_rf(sessions, END)
    rets = adj.pct_change(fill_method=None)
    rets["CASH"] = rf - CASH_ETF_FEE / TRADING_DAYS
    close["CASH"] = NOMINAL_PRICE
    adj["CASH"] = (1 + rets["CASH"].fillna(0)).cumprod()
    assert_dev_dates(sessions, END)
    return SimpleNamespace(sessions=sessions, adj=adj, close=close, rets=rets, rf=rf)


def rotation_one(name: str, e, *, hs=None, costs=True, cash_zero=False) -> dict:
    uni, n = ROTATIONS[name]
    w = st.rotation_weights(e.adj, e.rf, uni, n, P.mom_len)
    s_i = entry_index(w, e, uni, True, bench="ONEQ")
    rets = e.rets.copy()
    if cash_zero:
        rets["CASH"] = 0.0
    hs = hs or HALF_SPREAD
    sim = simulate_target_weights(w, rets, e.close, s_i, hs, costs=costs)
    oneq = buy_hold_weights("ONEQ", e.sessions, rets, e.close, s_i, hs)["ret"]
    qqq = buy_hold_weights("QQQ", e.sessions, rets, e.close, s_i, hs)["ret"]
    return {"w": w, "s_i": s_i, "sim": sim, "r": sim["ret"], "oneq": oneq, "qqq": qqq}


def judge_rot(r, b, q, rf) -> dict:
    i1, i2 = halves(r.index)
    m = {"full": period_metrics(r, b, q, rf), "H1": period_metrics(r.loc[i1], b.loc[i1], q.loc[i1], rf),
         "H2": period_metrics(r.loc[i2], b.loc[i2], q.loc[i2], rf)}
    v = ab_verdict(m["full"], m["H1"], m["H2"])
    v["A_bonferroni"] = bool(all(m[h]["cagr"] > m[h]["oneq_cagr"] for h in m)
                             and m["full"]["t_monthly_excess_vs_oneq"] >= bonferroni_t(N_TRIALS))
    v["pass_bonferroni"] = bool(v["A_bonferroni"] or v["B"])
    # same A / B against QQQ (report only)
    rk = lambda x: {"cagr": x["cagr"], "oneq_cagr": x["qqq_cagr"], "t_monthly_excess_vs_oneq": x["t_monthly_excess_vs_qqq"],
                    "dd_shallower_than_oneq_pp": x["dd_shallower_than_qqq_pp"]}
    vq = ab_verdict(rk(m["full"]), rk(m["H1"]), rk(m["H2"]))
    return {"metrics": m, "verdict": v, "verdict_vs_qqq": vq}


def part2(d: intl.Data, log: RunLog) -> dict:
    e = etf_panel(d)
    res, rows, dec_rows, sens_rows, by_year, mon = {}, [], [], [], {}, {}
    for name in ROTATIONS:
        o = rotation_one(name, e)
        r, b, q, sim = o["r"], o["oneq"], o["qqq"], o["sim"]
        entry = str(e.sessions[o["s_i"]].date())
        assert entry == EXPECTED_ROT_ENTRY[name], (name, entry)
        assert r.index.equals(b.index) and r.index.equals(q.index)
        assert_dev_dates(r.index, END)
        j = judge_rot(r, b, q, e.rf)
        yrs = len(r) / TRADING_DAYS
        held = sim["held"].mean()
        info = {**j, "entry_close": entry, "universe": ROTATIONS[name][0], "top_n": ROTATIONS[name][1],
                "trades": sim["trades"], "trades_per_year": sim["trades"] / yrs, "orders": sim["orders"],
                "cost_drag_per_year": sim["cost_frac"] / yrs,
                "avg_weight": {k: float(v) for k, v in held.items() if v > 0}}
        dec = st.decade_table(r, b, min_sessions=240)
        decq = st.decade_table(r, q, min_sessions=240)
        for x, y in zip(dec, decq):
            dec_rows.append({"rule": name, **x, "oneq_cagr": x["bench_cagr"], "qqq_cagr": y["bench_cagr"],
                             "cagr_minus_oneq": x["cagr_minus_bench"], "cagr_minus_qqq": y["cagr_minus_bench"],
                             "qqq_max_dd": y["bench_max_dd"]})
        info["by_decade"] = dec_rows[-len(dec):]
        sens = {"no_costs": dict(costs=False), "half_spread_x2": dict(hs={k: 2 * v for k, v in HALF_SPREAD.items()}),
                "cash_zero_rate": dict(cash_zero=True)}
        info["sensitivities"] = {}
        for tag, kw in sens.items():
            s = rotation_one(name, e, **kw)
            js = judge_rot(s["r"], s["oneq"], s["qqq"], e.rf)
            f = js["metrics"]["full"]
            row = {"rule": name, "sensitivity": tag, "cagr": f["cagr"], "cagr_minus_oneq": f["cagr_minus_oneq"],
                   "cagr_minus_qqq": f["cagr_minus_qqq"], "max_dd": f["max_dd"],
                   "t_vs_oneq": f["t_monthly_excess_vs_oneq"], "pass": js["verdict"]["pass"]}
            sens_rows.append(row)
            info["sensitivities"][tag] = row
        res[name] = info
        for h in ("full", "H1", "H2"):
            m = j["metrics"][h]
            rows.append({"rule": name, "period": h, "start": m["start"], "end": m["end"], "cagr": m["cagr"],
                         "oneq_cagr": m["oneq_cagr"], "qqq_cagr": m["qqq_cagr"], "cagr_minus_oneq": m["cagr_minus_oneq"],
                         "cagr_minus_qqq": m["cagr_minus_qqq"], "vol": m["vol"], "max_dd": m["max_dd"],
                         "oneq_max_dd": m["oneq_max_dd"], "qqq_max_dd": m["qqq_max_dd"],
                         "dd_shallower_than_oneq_pp": m["dd_shallower_than_oneq_pp"], "sharpe": m["sharpe"],
                         "oneq_sharpe": m["oneq_sharpe"], "t_vs_oneq": m["t_monthly_excess_vs_oneq"],
                         "t_vs_qqq": m["t_monthly_excess_vs_qqq"],
                         "A": j["verdict"]["A"] if h == "full" else None, "B": j["verdict"]["B"] if h == "full" else None,
                         "pass": j["verdict"]["pass"] if h == "full" else None,
                         "pass_bonferroni": j["verdict"]["pass_bonferroni"] if h == "full" else None,
                         "pass_vs_qqq": j["verdict_vs_qqq"]["pass"] if h == "full" else None})
        by_year[name], mon[name] = yearly(r), monthly(r)
        by_year[f"{name}_ONEQ"], by_year[f"{name}_QQQ"] = yearly(b), yearly(q)
        f, h1, h2 = j["metrics"]["full"], j["metrics"]["H1"], j["metrics"]["H2"]
        log.say(f"{name:4s} entry {entry} CAGR {f['cagr']:+.2%} (ONEQ {f['oneq_cagr']:+.2%} QQQ {f['qqq_cagr']:+.2%})"
                f" H1 {h1['cagr_minus_oneq']:+.2%} H2 {h2['cagr_minus_oneq']:+.2%} MDD {f['max_dd']:.1%} (ONEQ {f['oneq_max_dd']:.1%})"
                f" t {f['t_monthly_excess_vs_oneq']:+.2f} | A {j['verdict']['A']} B {j['verdict']['B']} pass {j['verdict']['pass']}"
                f" | vs QQQ pass {j['verdict_vs_qqq']['pass']} | trades/yr {info['trades_per_year']:.1f}"
                f" | weights {json.dumps({k: round(v, 2) for k, v in info['avg_weight'].items()})}")
    return {"rules": res, "table": pd.DataFrame(rows), "by_decade": pd.DataFrame(dec_rows),
            "sens": pd.DataFrame(sens_rows), "by_year": by_year, "monthly": mon}


# ======================================================================== main

def run(args=None) -> dict:
    log = RunLog()
    if getattr(args, "fetch", False):
        intl.fetch(FETCH_PERIOD2)
    d = intl.load(END)
    data_info = data_report(d, log)
    OUT.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "data_checks.json", data_info, default=float)
    if getattr(args, "data_only", False):
        log.write(OUT / "run_log_data.txt")
        return data_info
    pre = Preregistration(LEDGER, OUT / "prereg_frozen.json", OUT)
    if not (OUT / "prereg_frozen.json").exists():
        pre.register()
    frozen = json.loads((OUT / "prereg_frozen.json").read_text())
    if frozen["sha256"] != pre.hash():
        raise SystemExit("section 0 of the ledger changed after registration")
    log.say(f"trials {N_TRIALS}, Bonferroni t {bonferroni_t(N_TRIALS):.3f}, A t >= {A_T_MIN}; prereg {frozen['sha256'][:12]}")
    p1 = part1(d, log)
    p2 = part2(d, log)
    res = {"end": END, "n_trials": N_TRIALS, "bonferroni_t_one_sided_5pct": bonferroni_t(N_TRIALS),
           "prereg_sha256": frozen["sha256"], "params": P.__dict__, "rotation_half_spread": HALF_SPREAD,
           "replication_counts": p1["counts"].to_dict("records"), "index_level": p1["markets"],
           "rotation": p2["rules"]}
    write_json(OUT / "results.json", res, default=float)
    p1["table"].to_csv(OUT / "per_market.csv", index=False, float_format="%.6f")
    p1["counts"].to_csv(OUT / "replication_counts.csv", index=False, float_format="%.6f")
    p1["by_decade"].to_csv(OUT / "per_market_by_decade.csv", index=False, float_format="%.6f")
    p1["recent"].to_csv(OUT / "per_market_2015_2026.csv", index=False, float_format="%.6f")
    p1["sens"].to_csv(OUT / "per_market_sensitivities.csv", index=False, float_format="%.6f")
    p2["table"].to_csv(OUT / "rotation_summary.csv", index=False, float_format="%.6f")
    p2["by_decade"].to_csv(OUT / "rotation_by_decade.csv", index=False, float_format="%.6f")
    p2["sens"].to_csv(OUT / "rotation_sensitivities.csv", index=False, float_format="%.6f")
    write_period_tables(OUT, p1["by_year"], p1["monthly"], prefix="index_")
    write_period_tables(OUT, p2["by_year"], p2["monthly"], prefix="rotation_")
    log.write(OUT / "run_log.txt")
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fetch", action="store_true", help="download missing raw charts first (cached, logged)")
    ap.add_argument("--data-only", action="store_true", help="only the coverage / currency report")
    run(ap.parse_args(argv))


if __name__ == "__main__":
    main()
