"""Connors-style short-term daily mean reversion (RSI(2) pullback in an uptrend).

Pre-registered in docs/research_ledger_mean_reversion.md, section 0 (written before any result). Four rules, run once:

- R1 (QQQ):  flat -> in when QQQ > SMA200 and RSI(2) < 10; in -> flat when QQQ > SMA5. In = 100% QQQ, flat = cash
             earning the T-bill rate (no cost to switch into cash).
- R2 (QQQ, leveraged): same state machine; in = 50% QQQ + 50% QLD (1.5x), flat = 100% QQQ.
- R3 (stocks): weekly dv50 top-100 Nasdaq common stocks (frozen data version 1). Entry close > SMA200 and RSI(2) < 5;
             exit close > SMA5 or at the latest at the close of the 10th session after the entry fill. Up to 5
             names at 20% of equity, whole shares, lowest RSI(2) first. Idle money = cash at 0%.
- R4 (stocks): R3 with idle money parked in ONEQ (whole shares, adjusted only on days with stock fills).

Timing: every signal uses closes up to t; orders fill at the close of t+1 (MOC). Same-close fills are a report-only
sensitivity. Costs: IBKR Pro Tiered (ETF model of scripts/research_regime.py; stock model of
scripts/research_reversal_dev.py). Benchmark: ONEQ total return. Outputs carry returns and metrics only.

The state machine and both engines are quant.strategies.mean_reversion; this file holds the frozen parameters
(``CFG``, the stock windows) and the runs.

Usage:  PYTHONPATH=. .venv/bin/python -m quant study mean_reversion [--etf-only | --stocks-only]
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from quant.backtest.target_weights import buy_hold_weights, entry_index
from quant.data import etf_panel
from quant.data import panel
from quant.data import version as dv
from quant.data.guards import assert_dev_dates
from quant.data.panel import TERMINAL_D5_STRESS
from quant.evaluation.criteria import REGIME_AB_LABELS, ab_verdict, bonferroni_t
from quant.evaluation.metrics import TRADING_DAYS, cagr_of, max_drawdown, monthly, yearly
from quant.evaluation.periods import halves, return_period_metrics
from quant.paths import CACHE_ROOT, ROOT
from quant.reporting import RunLog, write_json, write_monthly_csv, write_period_tables
from quant.strategies import indicators as ind
from quant.strategies.mean_reversion import (
    Params, StockInputs, connors_state, etf_sim, etf_trade_stats, etf_weights, load_etf, stock_result,
)

END = "2026-09-30"
OUT = dv.versioned(ROOT / "output/research_only/mean_reversion")
QLD_PATH = CACHE_ROOT / "qqq_timing/raw/chart_QLD.json"
CFG = Params(rsi_n=2, sma_long=200, sma_exit=5, qqq_entry_rsi=10.0, stock_entry_rsi=5.0,
             max_hold=10,               # sessions after the entry fill
             k=5, slot=0.20, n_universe=100, account=10_000.0,
             etf_half_spread={"QQQ": 1e-4, "QLD": 2e-4, "ONEQ": 2e-4, "CASH": 0.0}, oneq_half_spread=2e-4)
N_TRIALS = 4

STOCK_WINDOWS = {
    "F1": {"perf_start": "2012-04-02", "perf_end": "2018-12-31", "price_start": "2011-06-01",
           "universe_start": "2012-01-01", "judged_from": "2012-04-02"},
    "F2": {"perf_start": "2019-01-02", "perf_end": "2026-08-31", "price_start": "2017-06-01",
           "universe_start": "2018-12-01", "judged_from": "2019-01-02"},
}


# ======================================================================== QQQ rules

def run_etf(log: RunLog) -> dict:
    d = load_etf(END, QLD_PATH)
    print("DATE GUARD: every ETF frame truncated at", END, "and asserted;",
          {k: v["last"] for k, v in d.guard.items() if isinstance(v, dict)})
    p = d.adj["QQQ"]
    state = connors_state(p, CFG)
    rets = d.rets.copy()
    rets["CASH"] = d.rf
    close = d.close.copy()
    close["CASH"] = 1.0
    res, rows, by_year, mon = {}, [], {}, {}
    for rule, assets, default in (("R1", ("QQQ",), "CASH"), ("R2", ("QQQ", "QLD"), "QQQ")):
        w = etf_weights(rule, state)
        s_i = entry_index(w, d, assets, False)
        sim = etf_sim(w, rets, close, s_i, CFG)
        bq = buy_hold_weights("ONEQ", d.sessions, d.rets, d.close, s_i, etf_panel.HALF_SPREAD)
        qq = buy_hold_weights("QQQ", d.sessions, d.rets, d.close, s_i, etf_panel.HALF_SPREAD)
        r, b, q = sim["ret"], bq["ret"], qq["ret"]
        assert r.index.equals(b.index) and r.index.equals(q.index)
        assert_dev_dates(r.index, END)
        i1, i2 = halves(r.index)
        full = return_period_metrics(r, b, q, d.rf)
        h1 = return_period_metrics(r.loc[i1], b.loc[i1], q.loc[i1], d.rf)
        h2 = return_period_metrics(r.loc[i2], b.loc[i2], q.loc[i2], d.rf)
        crit = ab_verdict(full, h1, h2, REGIME_AB_LABELS)
        in_state = (sim["executed"]["QLD" if rule == "R2" else "QQQ"] > 0).astype(int)
        ts = etf_trade_stats(sim, in_state, rets[default].loc[in_state.index])
        years = len(r) / TRADING_DAYS
        info = {"entry_close": str(d.sessions[s_i].date()), "full": full, "half1": h1, "half2": h2, "criteria": crit,
                "switches_per_year": sim["switches"] / years, "cost_drag_per_year": sim["cost_frac"] / years,
                **{k: v for k, v in ts.items() if not k.startswith("_")}}
        for tag, kw in (("same_close", dict(lag=0)), ("no_costs", dict(costs=False))):
            s2 = etf_sim(w, rets, close, s_i, CFG, **kw)
            info[f"sens_{tag}"] = {"cagr": cagr_of(s2["ret"]), "max_dd": max_drawdown(s2["value"]),
                                   "cagr_minus_oneq": cagr_of(s2["ret"]) - full["oneq_cagr"]}
        if rule == "R1":
            r0 = rets.copy()
            r0["CASH"] = 0.0
            s3 = etf_sim(w, r0, close, s_i, CFG)
            info["sens_cash_0pct"] = {"cagr": cagr_of(s3["ret"]), "max_dd": max_drawdown(s3["value"]),
                                      "cagr_minus_oneq": cagr_of(s3["ret"]) - full["oneq_cagr"]}
        res[rule] = info
        ts["_trades"].to_csv(OUT / f"trades_{rule}.csv", index=False, float_format="%.6f")
        for tag, pm in (("full", full), ("half1", h1), ("half2", h2)):
            rows.append({"rule": rule, "period": tag, **pm, "pass": crit["pass"] if tag == "full" else None})
        by_year[rule] = yearly(r)
        by_year[f"ONEQ_from_{rule}_entry"] = yearly(b)
        by_year[f"QQQ_from_{rule}_entry"] = yearly(q)
        by_year[f"{rule}_time_in_market"] = in_state.iloc[:-1].groupby(in_state.index[:-1].year).mean()
        mon[rule] = monthly(r)
        mon[f"ONEQ_from_{rule}_entry"] = monthly(b)
        line = (f"{rule} {full['start']}..{full['end']} CAGR {full['cagr']:+.2%} (ONEQ {full['oneq_cagr']:+.2%}, "
                f"QQQ {full['qqq_cagr']:+.2%}) MDD {full['max_dd']:.1%} (ONEQ {full['oneq_max_dd']:.1%}) "
                f"t {full['t_monthly_excess_vs_oneq']:+.2f} trades {ts['trades']} win {ts['win_rate']:.0%} "
                f"in-market {ts['time_in_market']:.0%} pass={crit['pass']}")
        log.say(line)
    pd.DataFrame(rows).to_csv(OUT / "etf_summary.csv", index=False, float_format="%.6f")
    write_period_tables(OUT, by_year, mon, prefix="etf_")
    return {"guard": d.guard, "rules": res}


# ======================================================================== stock rules

def run_stocks(log: RunLog) -> dict:
    out = {}
    year_rows, mon = [], {}
    for fold in STOCK_WINDOWS:
        data = panel.load_window(f"mr_{fold}", STOCK_WINDOWS[fold])
        print(data.guard["assertion"])
        inp = StockInputs(data, CFG)
        stress = StockInputs(panel.load_window(f"mr_{fold}", STOCK_WINDOWS[fold], terminal_awaiting=TERMINAL_D5_STRESS),
                             CFG)
        for rule, idle in (("R3", "cash"), ("R4", "oneq")):
            m, res = stock_result(inp, idle, CFG)
            crit = ind.criteria(m["cagr"], m["max_dd"], m["bench_cagr"], m["bench_max_dd"], m["t_excess_monthly"])
            sens = {}
            for tag, src, kw in (("spread2x", inp, dict(spread_mult=2.0)), ("same_close", inp, dict(lag=0)),
                                 ("terminal_minus100", stress, {})):
                ms, _ = stock_result(src, idle, CFG, **kw)
                sens[tag] = {"cagr": ms["cagr"], "excess_cagr": ms["excess_cagr"], "max_dd": ms["max_dd"],
                             "t_excess_monthly": ms["t_excess_monthly"]}
            if fold == "F1":
                sub = ind.stock_sub_metrics(res["nav"], res["oneq"], res["qqq"], "2014-01-01", res["cost"],
                                            res["exposure"])
                sens["judged_2014_2018_report_only"] = {k: sub[k] for k in ("cagr", "bench_cagr", "excess_cagr",
                                                                            "max_dd", "bench_max_dd",
                                                                            "t_excess_monthly")}
            key = f"{rule}_{fold}"
            out[key] = {"metrics": {k: v for k, v in m.items() if not k.startswith("_")}, "criteria": crit,
                        "sensitivities": sens, "carried_universe_weeks": inp.carried_weeks,
                        "open_positions_at_end": res["open"], "guard": data.guard}
            res["trades"].to_csv(OUT / f"trades_{key}.csv", index=False, float_format="%.6f")
            nav0 = pd.concat([pd.Series([CFG.account], index=[res["nav"].index[0] - pd.Timedelta(days=1)]), res["nav"]])
            bn0 = pd.concat([pd.Series([CFG.account], index=[res["oneq"].index[0] - pd.Timedelta(days=1)]), res["oneq"]])
            mon[key] = monthly(nav0.pct_change().dropna())
            mon[f"ONEQ_{fold}"] = monthly(bn0.pct_change().dropna())
            tim = res["in_market"].groupby(res["in_market"].index.year).mean()
            nyr = res["trades"].assign(y=pd.to_datetime(res["trades"]["exit"]).dt.year).groupby("y") \
                if len(res["trades"]) else None
            for y, v in m["by_year"].items():
                g = nyr.get_group(y) if nyr is not None and y in nyr.groups else pd.DataFrame(columns=["ret"])
                year_rows.append({"rule": rule, "fold": fold, "year": y, "strategy": v["strategy"], "oneq": v["oneq"],
                                  "excess": v["excess"], "time_in_market": float(tim.get(y, np.nan)),
                                  "trades_closed": int(len(g)),
                                  "win_rate": float((g["ret"] > 0).mean()) if len(g) else float("nan")})
            line = (f"{key} {m['start']}..{m['end']} CAGR {m['cagr']:+.2%} (ONEQ {m['bench_cagr']:+.2%}, QQQ "
                    f"{m['qqq_cagr']:+.2%}) MDD {m['max_dd']:.1%} (ONEQ {m['bench_max_dd']:.1%}) t "
                    f"{m['t_excess_monthly']:+.2f} trades {m['closed_trades']} win {m['win_rate']:.0%} in-market "
                    f"{m['time_in_market']:.0%} cost {m['cost_drag_per_year']:.2%}/yr pass={crit['pass']}")
            log.say(line)
    for rule in ("R3", "R4"):
        f1, f2 = out[f"{rule}_F1"]["criteria"]["pass"], out[f"{rule}_F2"]["criteria"]["pass"]
        out[f"{rule}_verdict"] = "pass" if (f1 and f2) else ("unstable (one fold only)" if (f1 or f2) else "fail")
    pd.DataFrame(year_rows).to_csv(OUT / "stock_by_year.csv", index=False, float_format="%.6f")
    write_monthly_csv(OUT / "stock_monthly_returns.csv", mon)
    return out


# ======================================================================== main

def run(args) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    log = RunLog()
    res = {"n_trials": N_TRIALS, "bonferroni_t_one_sided_5pct": bonferroni_t(N_TRIALS)}
    if not args.stocks_only:
        res["etf"] = run_etf(log)
    if not args.etf_only:
        res["stocks"] = run_stocks(log)
    write_json(OUT / "results.json", res)
    log.write(OUT / "run_log.txt")
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--etf-only", action="store_true")
    ap.add_argument("--stocks-only", action="store_true")
    run(ap.parse_args(argv))


if __name__ == "__main__":
    main()
