"""Regime-switching allocation across long-only ETFs (pre-registered in docs/research_ledger_regime.md, section 0).

Instead of in / out timing, each rule switches WHAT it holds according to a textbook market-regime signal:

- R1  trend:        QQQ > SMA200(QQQ) -> QQQ, else IEF           (R1b: else USMV)
- R2  dual momentum: month end, QQQ 12m return > T-bill 12m -> QQQ, else IEF
                     (R2b: the stronger of QQQ / SPY over 12m if it beats T-bills, else IEF)
- R3  volatility:   QQQ 20d realised vol < its own 252d median -> QQQ, else USMV  (R3b: else 50% QQQ + 50% IEF)
- R4  factor:       SPY > SMA200(SPY) -> MTUM, else USMV
- R5  risk parity lite: month end, weights proportional to 1 / (63d vol) across QQQ, IEF, GLD

Execution: the target decided at the close of t (data up to and including t) is traded at the close of t+1, so the
new weights earn from t+2. Trades happen only when the target changes; weights drift in between.
Benchmark: ONEQ buy-and-hold (total return); QQQ buy-and-hold is reported. Costs: IBKR Pro Tiered, $10,000 start.

Every vendor frame is truncated at ``END`` (latest complete month end) immediately after parsing and asserted.
Raw data local only: research_cache/regime/raw/. Outputs: output/research_only/regime/ (returns and metrics only,
no vendor price levels). The rule signals and data checks are quant.strategies.regime, the engine
quant.backtest.target_weights, the data quant.data.etf_panel.

Usage:  PYTHONPATH=. .venv/bin/python -m quant study regime   (or scripts/research_regime.py)
"""
from __future__ import annotations

import argparse

import pandas as pd

from quant.backtest.target_weights import buy_hold_weights, entry_index, simulate_target_weights
from quant.data.etf_panel import BENCH, HALF_SPREAD, load_data
from quant.data.guards import assert_dev_dates
from quant.evaluation.criteria import REGIME_AB_LABELS, ab_verdict, bonferroni_t
from quant.evaluation.metrics import TRADING_DAYS, cagr_of, max_drawdown, monthly, yearly
from quant.evaluation.periods import halves, return_period_metrics as period_metrics
from quant.paths import CACHE_ROOT, output_dir
from quant.reporting import write_json, write_period_tables
from quant.strategies.regime import RegimeParams, RuleSpec, data_checks, target_weights

END = "2026-09-30"
RAW = CACHE_ROOT / "regime/raw"
PRIOR_QQQ = CACHE_ROOT / "qqq_timing/raw/chart_QQQ.json"
OUT = output_dir("regime")

PARAMS = RegimeParams(sma_len=200, mom_len=252, vol_len=20, vol_med_len=252, rp_len=63)

RULES = (
    RuleSpec("R1", ("QQQ", "IEF"), False, "QQQ > SMA200 -> QQQ, else IEF"),
    RuleSpec("R1b", ("QQQ", "USMV"), False, "QQQ > SMA200 -> QQQ, else USMV"),
    RuleSpec("R2", ("QQQ", "IEF"), True, "month end: QQQ 12m > T-bill 12m -> QQQ, else IEF"),
    RuleSpec("R2b", ("QQQ", "SPY", "IEF"), True, "month end: stronger of QQQ/SPY 12m, if > T-bill -> it, else IEF"),
    RuleSpec("R3", ("QQQ", "USMV"), False, "QQQ 20d vol < 252d median -> QQQ, else USMV"),
    RuleSpec("R3b", ("QQQ", "IEF"), False, "QQQ 20d vol < 252d median -> QQQ, else 50% QQQ + 50% IEF"),
    RuleSpec("R4", ("MTUM", "USMV"), False, "SPY > SMA200 -> MTUM, else USMV"),
    RuleSpec("R5", ("QQQ", "IEF", "GLD"), True, "month end: inverse 63d-vol weights across QQQ, IEF, GLD"),
)
SPEC = {r.name: r for r in RULES}




def targets(name: str, d) -> pd.DataFrame:
    """Target weights of rule ``name`` (quant.strategies.regime.target_weights with the frozen windows)."""
    return target_weights(name, d, SPEC[name].assets, PARAMS)


# ======================================================================== main

def run(args=None) -> dict:
    d = load_data(END)
    print("DATE GUARD: every frame truncated at", END, "and asserted;", {k: v["last"] for k, v in d.guard.items()
                                                                       if isinstance(v, dict)})
    checks = data_checks(d, PRIOR_QQQ, END)
    res = {"guard": d.guard, "data_checks": checks, "n_trials": len(RULES),
           "bonferroni_t_one_sided_5pct": bonferroni_t(len(RULES)), "rules": {}}
    rows, by_year, mon = [], {}, {}
    for spec in RULES:
        w = targets(spec.name, d)
        s_i = entry_index(w, d, spec.assets, spec.monthly)
        sim = simulate_target_weights(w, d.rets, d.close, s_i, HALF_SPREAD)
        bq = buy_hold_weights(BENCH, d.sessions, d.rets, d.close, s_i, HALF_SPREAD)
        qq = buy_hold_weights("QQQ", d.sessions, d.rets, d.close, s_i, HALF_SPREAD)
        r, b, q = sim["ret"], bq["ret"], qq["ret"]
        assert r.index.equals(b.index) and r.index.equals(q.index)
        assert_dev_dates(r.index, END)
        i1, i2 = halves(r.index)
        full = period_metrics(r, b, q, d.rf)
        h1 = period_metrics(r.loc[i1], b.loc[i1], q.loc[i1], d.rf)
        h2 = period_metrics(r.loc[i2], b.loc[i2], q.loc[i2], d.rf)
        crit = ab_verdict(full, h1, h2, REGIME_AB_LABELS)     # pre-registered pass criteria (section 0.6)
        years = len(r) / TRADING_DAYS
        held = sim["held"]
        info = {"description": spec.description, "entry_close": str(d.sessions[s_i].date()),
                "signal_at_entry": {k: float(v) for k, v in w.iloc[s_i - 1].items()},
                "full": full, "half1": h1, "half2": h2, "criteria": crit,
                "trades": sim["trades"], "trades_per_year": sim["trades"] / years, "orders": sim["orders"],
                "cost_drag_per_year": sim["cost_frac"] / years,
                "avg_weight": {k: float(v) for k, v in held.mean().items()},
                "time_mostly_in": {k: float((held[k] > 0.5).mean()) for k in held.columns}}
        # report-only sensitivities: trade at the close of t; no costs
        for tag, kw in [("same_close", dict(lag=0)), ("no_costs", dict(costs=False))]:
            s2 = simulate_target_weights(w, d.rets, d.close, s_i, HALF_SPREAD, **kw)
            info[f"sens_{tag}"] = {"cagr": cagr_of(s2["ret"]), "max_dd": max_drawdown(s2["value"]),
                                   "cagr_minus_oneq": cagr_of(s2["ret"]) - full["oneq_cagr"]}
        res["rules"][spec.name] = info
        for tag, p in [("full", full), ("half1", h1), ("half2", h2)]:
            rows.append({"rule": spec.name, "period": tag, **p, "pass": crit["pass"] if tag == "full" else None})
        by_year[spec.name] = yearly(r)
        mon[spec.name] = monthly(r)
        print(f"{spec.name:4s} {full['start']}..{full['end']} CAGR {full['cagr']:+.2%} (ONEQ {full['oneq_cagr']:+.2%}, "
              f"QQQ {full['qqq_cagr']:+.2%}) MDD {full['max_dd']:.1%} (ONEQ {full['oneq_max_dd']:.1%}) "
              f"t {full['t_monthly_excess_vs_oneq']:+.2f} pass={crit['pass']}")
    s0 = min(entry_index(targets(n, d), d, SPEC[n].assets, SPEC[n].monthly) for n in SPEC)
    for a in (BENCH, "QQQ"):
        rr = buy_hold_weights(a, d.sessions, d.rets, d.close, s0, HALF_SPREAD)["ret"]
        by_year[f"{a}_buyhold"] = yearly(rr)
        mon[f"{a}_buyhold"] = monthly(rr)
    OUT.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "results.json", res, default=float)
    pd.DataFrame(rows).to_csv(OUT / "summary.csv", index=False, float_format="%.6f")
    write_period_tables(OUT, by_year, mon)
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    run(ap.parse_args(argv))


if __name__ == "__main__":
    main()
