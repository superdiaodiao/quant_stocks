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
no vendor price levels).

Usage:  PYTHONPATH=. .venv/bin/python -m quant study regime   (or scripts/research_regime.py)
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass

import numpy as np
import pandas as pd

from quant.backtest.costs import etf_order_cost as order_cost  # noqa: F401  (rr.order_cost: sector_lev, tests)
from quant.backtest.target_weights import entry_index, simulate_target_weights, two_state
from quant.data.calendar import month_end_mask
from quant.data.etf_panel import BENCH, HALF_SPREAD, TICKERS, Data, load_data  # noqa: F401  (rr.* for tests)
from quant.data.guards import assert_dev_dates
from quant.data.sources.yahoo import parse_chart
from quant.evaluation.criteria import REGIME_AB_LABELS, ab_verdict, bonferroni_t
from quant.evaluation.metrics import TRADING_DAYS, cagr_of, max_drawdown, monthly, yearly
from quant.evaluation.periods import halves, return_period_metrics as period_metrics  # noqa: F401  (rr.halves)
from quant.paths import CACHE_ROOT, output_dir
from quant.signals import technical

END = "2026-09-30"
RAW = CACHE_ROOT / "regime/raw"
PRIOR_QQQ = CACHE_ROOT / "qqq_timing/raw/chart_QQQ.json"
OUT = output_dir("regime")

SMA_LEN = 200
MOM_LEN = 252
VOL_LEN = 20
VOL_MED_LEN = 252
RP_LEN = 63


# ======================================================================== data


def data_checks(d: Data) -> dict:
    out = {}
    for t, df in d.raw.items():
        a = df["adjclose"].pct_change()
        c = (df["close"] + df["dividend"]) / df["close"].shift(1) - 1
        diff = (a - c).dropna()
        first = pd.Timestamp(df["date"].iloc[0])
        listed = d.sessions[d.sessions >= first]
        out[t] = {"first": df["date"].iloc[0], "missing_sessions_after_listing": int(d.adj[t].reindex(listed).isna().sum()),
                  "max_abs_adj_vs_close_div": float(diff.abs().max()), "dividends": int((df["dividend"] > 0).sum()),
                  "max_abs_daily_return": float(a.abs().max())}
    bil = d.rets["BIL"].dropna()
    rf = d.rf.reindex(bil.index)
    out["BIL_vs_KF_rf_annualised"] = {"bil": cagr_of(bil), "kf_rf": cagr_of(rf), "window": [str(bil.index[0].date()),
                                                                                          str(bil.index[-1].date())]}
    if PRIOR_QQQ.exists():
        p = parse_chart(PRIOR_QQQ, END)
        pr = pd.Series(p["adjclose"].values, index=pd.DatetimeIndex(p["date"])).pct_change()
        j = pd.concat([pr, d.rets["QQQ"]], axis=1, join="inner").dropna()
        out["QQQ_vs_prior_cache"] = {"sessions": int(len(j)), "max_abs_daily_diff": float((j.iloc[:, 0] - j.iloc[:, 1]).abs().max())}
    return out


# ======================================================================== signals (data up to and including t)

def sma_state(p: pd.Series, length: int = SMA_LEN) -> pd.Series:
    return technical.sma_state(p, length)


def trailing_return(p: pd.Series, n: int = MOM_LEN) -> pd.Series:
    return technical.trailing_return(p, n)


def trailing_rf(rf: pd.Series, n: int = MOM_LEN) -> pd.Series:
    return technical.trailing_compound(rf, n)


def vol_state(r: pd.Series, window: int = VOL_LEN, med_len: int = VOL_MED_LEN) -> pd.Series:
    return technical.vol_state(r, window, med_len)


def inverse_vol_weights(rets: pd.DataFrame, length: int = RP_LEN) -> pd.DataFrame:
    return technical.inverse_vol_weights(rets, length)


def monthly_only(w: pd.DataFrame) -> pd.DataFrame:
    """Keep the month-end decisions and carry them until the next month end."""
    me = month_end_mask(w.index)
    keep = np.repeat(me[:, None], w.shape[1], axis=1)
    return w.where(keep, np.nan).ffill()


@dataclass(frozen=True)
class RuleSpec:
    name: str
    assets: tuple          # every ETF the rule can hold
    monthly: bool
    description: str


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


def target_weights(name: str, d: Data) -> pd.DataFrame:
    """Target weights decided at each close t (NaN rows = no decision possible yet)."""
    a = d.adj
    cols = SPEC[name].assets
    if name in ("R1", "R1b"):
        alt = "IEF" if name == "R1" else "USMV"
        return two_state(sma_state(a["QQQ"]), {"QQQ": 1.0}, {alt: 1.0}, cols)
    if name == "R2":
        q, tb = trailing_return(a["QQQ"]), trailing_rf(d.rf)
        st = (q > tb).astype(float).where(q.notna() & tb.notna())
        return monthly_only(two_state(st, {"QQQ": 1.0}, {"IEF": 1.0}, cols))
    if name == "R2b":
        q, s, tb = trailing_return(a["QQQ"]), trailing_return(a["SPY"]), trailing_rf(d.rf)
        ok = q.notna() & s.notna() & tb.notna()
        w = pd.DataFrame(np.nan, index=a.index, columns=list(cols))
        best_q = q >= s
        best_ret = q.where(best_q, s)
        w.loc[ok, :] = 0.0
        w.loc[ok & best_q & (best_ret > tb), "QQQ"] = 1.0
        w.loc[ok & ~best_q & (best_ret > tb), "SPY"] = 1.0
        w.loc[ok & ~(best_ret > tb), "IEF"] = 1.0
        return monthly_only(w)
    if name in ("R3", "R3b"):
        st = vol_state(d.rets["QQQ"])
        off = {"USMV": 1.0} if name == "R3" else {"QQQ": 0.5, "IEF": 0.5}
        return two_state(st, {"QQQ": 1.0}, off, cols)
    if name == "R4":
        return two_state(sma_state(a["SPY"]), {"MTUM": 1.0}, {"USMV": 1.0}, cols)
    if name == "R5":
        return monthly_only(inverse_vol_weights(d.rets[["QQQ", "IEF", "GLD"]]))
    raise KeyError(name)


# ======================================================================== simulation (quant.backtest.target_weights)

def simulate(w: pd.DataFrame, rets: pd.DataFrame, close: pd.DataFrame, start_i: int, end_i: int | None = None,
             lag: int = 1, costs: bool = True) -> dict:
    return simulate_target_weights(w, rets, close, start_i, HALF_SPREAD, end_i=end_i, lag=lag, costs=costs)


def buy_hold(asset: str, d: Data, start_i: int) -> dict:
    w = pd.DataFrame({asset: 1.0}, index=d.sessions)
    return simulate(w, d.rets, d.close, start_i)


# ======================================================================== criteria

def evaluate(full: dict, h1: dict, h2: dict) -> dict:
    """Pre-registered pass criteria (section 0.6): A or B, each on the full period and both halves."""
    return ab_verdict(full, h1, h2, REGIME_AB_LABELS)


# ======================================================================== main

def run(args=None) -> dict:
    d = load_data(END)
    print("DATE GUARD: every frame truncated at", END, "and asserted;", {k: v["last"] for k, v in d.guard.items()
                                                                       if isinstance(v, dict)})
    checks = data_checks(d)
    res = {"guard": d.guard, "data_checks": checks, "n_trials": len(RULES),
           "bonferroni_t_one_sided_5pct": bonferroni_t(len(RULES)), "rules": {}}
    rows, by_year, mon = [], {}, {}
    for spec in RULES:
        w = target_weights(spec.name, d)
        s_i = entry_index(w, d, spec.assets, spec.monthly)
        sim = simulate(w, d.rets, d.close, s_i)
        bq = buy_hold(BENCH, d, s_i)
        qq = buy_hold("QQQ", d, s_i)
        r, b, q = sim["ret"], bq["ret"], qq["ret"]
        assert r.index.equals(b.index) and r.index.equals(q.index)
        assert_dev_dates(r.index, END)
        i1, i2 = halves(r.index)
        full = period_metrics(r, b, q, d.rf)
        h1 = period_metrics(r.loc[i1], b.loc[i1], q.loc[i1], d.rf)
        h2 = period_metrics(r.loc[i2], b.loc[i2], q.loc[i2], d.rf)
        crit = evaluate(full, h1, h2)
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
            s2 = simulate(w, d.rets, d.close, s_i, **kw)
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
    s0 = min(entry_index(target_weights(n, d), d, SPEC[n].assets, SPEC[n].monthly) for n in SPEC)
    for a in (BENCH, "QQQ"):
        rr = buy_hold(a, d, s0)["ret"]
        by_year[f"{a}_buyhold"] = yearly(rr)
        mon[f"{a}_buyhold"] = monthly(rr)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "results.json").write_text(json.dumps(res, indent=2, default=float))
    pd.DataFrame(rows).to_csv(OUT / "summary.csv", index=False, float_format="%.6f")
    pd.DataFrame(by_year).to_csv(OUT / "by_year.csv", float_format="%.6f")
    mk = pd.DataFrame(mon)
    mk.index = mk.index.astype(str)
    mk.to_csv(OUT / "monthly_returns.csv", float_format="%.6f")
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    run(ap.parse_args(argv))


if __name__ == "__main__":
    main()
