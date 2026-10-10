"""Volatility targeting on QQQ, Moreira-Muir (2017) style (pre-registered in docs/research_ledger_voltarget.md, section 0).

Exposure e decided at each month end from QQQ's realised volatility (data up to and including t), traded at the
next session's close; e is implemented with QQQ + a 2x leg (synthetic before 2006-06-22, real QLD after) or
QQQ + a T-bill ETF (the legs of research_sector_lev, quant.data.sector_etfs):

- V1   e = min(1,   20% / vol20)            V1b: vol63 instead of vol20
- V2   e = min(1.5, 20% / vol20)            V2b: cap 2.0
- V3   e = min(1.5, volLR / vol20)          V3b: cap 1.0   (volLR = expanding vol of all QQQ returns up to t)
- V4   QQQ SMA200, 2% band (the B3 signal): on -> V2, off -> min(1, 20% / vol20); also trades on state changes
       V4b: off -> 0 (all T-bill ETF)

Benchmark: ONEQ buy-and-hold (real ONEQ, from its first close 2003-10-01); QQQ buy-and-hold is reported, and every
rule is compared with a constant-exposure portfolio at the rule's own average realised exposure (report only).
Costs: IBKR Pro Tiered, $10,000 start. Raw data local only (research_cache/sector_lev/raw/, reused, not re-downloaded);
every vendor frame is truncated at ``END`` and asserted by the sector_lev loader (quant.data.sector_etfs).
Outputs: output/research_only/voltarget/ (returns and metrics only, no vendor price levels).

Usage:  PYTHONPATH=. .venv/bin/python -m quant study voltarget   (or scripts/research_voltarget.py)
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from quant.backtest import flagged_weights as fw
from quant.data import sector_etfs as etfs
from quant.data.calendar import month_end_mask
from quant.data.guards import assert_dev_dates
from quant.evaluation.criteria import ab_verdict, bonferroni_t
from quant.evaluation.equity import equity0, period_metrics
from quant.evaluation.metrics import TRADING_DAYS, cagr_of, max_drawdown, monthly, yearly
from quant.evaluation.periods import halves
from quant.paths import output_dir
from quant.signals.technical import realised_vol, trend_state

END = "2026-09-30"               # as research_sector_lev (same data, same end)
OUT = output_dir("voltarget")
ONEQ_START = "2003-10-01"
EXPECTED_ENTRY = "2003-10-01"
LEGS = etfs.LEVERAGE_LEGS                 # QQQ, QLDX (2x leg), CASH (T-bill ETF)
TARGET_VOL = 0.20
VOL_SHORT, VOL_QUARTER, VOL_LR_MIN = 20, 63, 252
SMA_LEN, SMA_BAND = 200, 0.02
DAILY_BAND = 0.10                # report-only sensitivity: daily decisions with a 10% relative no-trade band


# ======================================================================== signals (data up to and including t)


def expanding_vol(r: pd.Series, min_obs: int = VOL_LR_MIN) -> pd.Series:
    """Std of every daily return from the first one up to and including t, annualised."""
    return r.expanding(min_periods=min_obs).std() * math.sqrt(TRADING_DAYS)


@dataclass(frozen=True)
class Spec:
    name: str
    description: str


SPECS = (
    Spec("V1", "e = min(1, 20% / vol20), rest T-bill ETF, monthly"),
    Spec("V1b", "e = min(1, 20% / vol63), monthly"),
    Spec("V2", "e = min(1.5, 20% / vol20) via QQQ + QLD leg, monthly"),
    Spec("V2b", "e = min(2.0, 20% / vol20), monthly"),
    Spec("V3", "e = min(1.5, expanding QQQ vol / vol20), monthly"),
    Spec("V3b", "e = min(1.0, expanding QQQ vol / vol20), monthly"),
    Spec("V4", "SMA200 band 2% on -> V2, off -> min(1, 20% / vol20); monthly + on state change"),
    Spec("V4b", "SMA200 band 2% on -> V2, off -> 0 (T-bill ETF); monthly + on state change"),
)
SPEC = {s.name: s for s in SPECS}


def daily_exposure(name: str, r_qqq: pd.Series, p_qqq: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Exposure the rule would choose at each close t, and the trend-state change flag (V4 only, else all False)."""
    v20 = realised_vol(r_qqq, VOL_SHORT)
    no_change = pd.Series(False, index=r_qqq.index)
    if name == "V1":
        return (TARGET_VOL / v20).clip(upper=1.0), no_change
    if name == "V1b":
        return (TARGET_VOL / realised_vol(r_qqq, VOL_QUARTER)).clip(upper=1.0), no_change
    if name in ("V2", "V2b"):
        return (TARGET_VOL / v20).clip(upper=1.5 if name == "V2" else 2.0), no_change
    if name in ("V3", "V3b"):
        return (expanding_vol(r_qqq) / v20).clip(upper=1.5 if name == "V3" else 1.0), no_change
    if name in ("V4", "V4b"):
        st = trend_state(p_qqq, SMA_LEN, SMA_BAND)
        on = (TARGET_VOL / v20).clip(upper=1.5)
        off = (TARGET_VOL / v20).clip(upper=1.0) if name == "V4" else pd.Series(0.0, index=r_qqq.index).where(v20.notna())
        e = on.where(st == 1.0, off).where(st.notna() & v20.notna())
        change = st.ne(st.shift(1)) & st.shift(1).notna() & st.notna()
        return e, change
    raise KeyError(name)


def weights_from_exposure(e: pd.Series) -> pd.DataFrame:
    w = pd.DataFrame(np.nan, index=e.index, columns=LEGS)
    ok = e.notna()
    ev = e[ok].values
    w.loc[ok, "QQQ"] = np.where(ev >= 1.0, 2.0 - ev, ev)
    w.loc[ok, "QLDX"] = np.where(ev >= 1.0, ev - 1.0, 0.0)
    w.loc[ok, "CASH"] = np.where(ev >= 1.0, 0.0, 1.0 - ev)
    return w


def targets(name: str, d: etfs.Data) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """Monthly decisions (plus V4 state changes) carried forward; returns (weights, rebalance flag, exposure)."""
    e, change = daily_exposure(name, d.rets["QQQ"], d.p_qqq)
    me = pd.Series(month_end_mask(e.index), index=e.index)
    decide = (me | change) & e.notna()
    e_t = e.where(decide).ffill()
    return weights_from_exposure(e_t), decide, e_t


def banded_targets(name: str, d: etfs.Data, band: float = DAILY_BAND) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """Report-only: decide every day, act only when |e - last acted e| > band * last acted e (or on a state change)."""
    e, change = daily_exposure(name, d.rets["QQQ"], d.p_qqq)
    ev, ch = e.values, change.values
    out = np.full(len(e), np.nan)
    last = np.nan
    for i in range(len(e)):
        if np.isnan(ev[i]):
            if not np.isnan(last):
                out[i] = last
            continue
        if np.isnan(last) or ch[i] or abs(ev[i] - last) > band * last:
            last = ev[i]
        out[i] = last
    e_t = pd.Series(out, index=e.index)
    return weights_from_exposure(e_t), pd.Series(False, index=e.index), e_t


def held_exposure(held: pd.DataFrame) -> pd.Series:
    return held["QQQ"] + 2.0 * held["QLDX"]


def constant_targets(e_bar: float, index: pd.DatetimeIndex) -> tuple[pd.DataFrame, pd.Series]:
    w = weights_from_exposure(pd.Series(e_bar, index=index))
    return w, pd.Series(month_end_mask(index), index=index)


# ======================================================================== metrics

def tstat_diff(a: pd.Series, b: pd.Series) -> float:
    x = monthly(a) - monthly(b)
    return float(x.mean() / x.std() * math.sqrt(len(x)))


def mm_alpha(r: pd.Series, q: pd.Series, rf: pd.Series) -> dict:
    """Moreira-Muir regression: monthly (rule - rf) on (QQQ - rf); annualised alpha and its OLS t."""
    mrf = monthly(rf.reindex(r.index))
    y = (monthly(r) - mrf).values
    x = (monthly(q) - mrf).values
    X = np.column_stack([np.ones(len(x)), x])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ coef
    s2 = resid @ resid / (len(y) - 2)
    se = np.sqrt(np.diag(s2 * np.linalg.inv(X.T @ X)))
    return {"alpha_ann": float(coef[0] * 12), "t_alpha": float(coef[0] / se[0]), "beta": float(coef[1])}


def vs_constant(r: pd.Series, c: pd.Series, i1, i2) -> dict:
    out = {}
    for tag, ix in (("full", r.index), ("half1", i1), ("half2", i2)):
        rr, cc = r.loc[ix], c.loc[ix]
        out[tag] = {"cagr": cagr_of(rr), "const_cagr": cagr_of(cc), "cagr_minus_const": cagr_of(rr) - cagr_of(cc),
                    "max_dd": max_drawdown(equity0(rr)), "const_max_dd": max_drawdown(equity0(cc)),
                    "t_monthly_excess_vs_const": tstat_diff(rr, cc)}
    f = out["full"]
    out["timing_adds_value"] = bool(f["t_monthly_excess_vs_const"] >= 2.0 and out["half1"]["cagr_minus_const"] > 0
                                    and out["half2"]["cagr_minus_const"] > 0)
    return out


def exposure_stats(e_held: pd.Series, e_tgt: pd.Series, cap: float | None) -> dict:
    out = {"mean": float(e_held.mean()), "min": float(e_held.min()), "median": float(e_held.median()),
           "max": float(e_held.max()), "target_mean": float(e_tgt.mean())}
    if cap is not None:
        out["share_target_at_cap"] = float((e_tgt >= cap - 1e-12).mean())
    out["share_target_zero"] = float((e_tgt <= 1e-12).mean())
    return out


CAP = {"V1": 1.0, "V1b": 1.0, "V2": 1.5, "V2b": 2.0, "V3": 1.5, "V3b": 1.0, "V4": 1.5, "V4b": 1.5}


# ======================================================================== main

def run_one(spec: Spec, d: etfs.Data, not_before: str, target_fn=targets) -> dict:
    w, flag, e_t = target_fn(spec.name, d)
    s_i = fw.entry_index(w, flag, d, target_fn is targets, not_before)
    sim = fw.simulate(w, flag, d.rets, d.close, s_i)
    return {"w": w, "flag": flag, "e_t": e_t, "s_i": s_i, "sim": sim, "r": sim["ret"]}


def run(args=None) -> dict:
    d = etfs.load_data(END)
    print("DATE GUARD: every frame truncated at", END, "and asserted;",
          {k: v["last"] for k, v in d.guard.items() if isinstance(v, dict)})
    checks = etfs.data_checks(d, END)
    res = {"guard": d.guard, "data_checks": checks, "n_trials": len(SPECS),
           "bonferroni_t_one_sided_5pct": bonferroni_t(len(SPECS)), "rules": {}}
    rows, const_rows, by_year, mon = [], [], {}, {}
    starts = []
    long_entries = []
    for spec in SPECS:
        m = run_one(spec, d, ONEQ_START)
        s_i, sim, r = m["s_i"], m["sim"], m["r"]
        starts.append(s_i)
        assert str(d.sessions[s_i].date()) == EXPECTED_ENTRY, (spec.name, d.sessions[s_i])
        b = fw.buy_hold("BENCH", d, s_i)["ret"]
        q = fw.buy_hold("QQQ", d, s_i)["ret"]
        assert r.index.equals(b.index) and r.index.equals(q.index)
        assert (d.rets["BENCH"].reindex(r.index) == d.rets["ONEQ"].reindex(r.index)).all(), "benchmark must be real ONEQ"
        assert_dev_dates(r.index, END)
        i1, i2 = halves(r.index)
        full = period_metrics(r, b, q, d.rf)
        h1 = period_metrics(r.loc[i1], b.loc[i1], q.loc[i1], d.rf)
        h2 = period_metrics(r.loc[i2], b.loc[i2], q.loc[i2], d.rf)
        crit = ab_verdict(full, h1, h2)
        years = len(r) / TRADING_DAYS
        e_held = held_exposure(sim["held"])
        e_tgt = m["e_t"].iloc[s_i - 1:-2]          # target in force on each return day (decided two sessions earlier)
        e_bar = float(e_held.mean())
        cw, cf = constant_targets(e_bar, d.sessions)
        csim = fw.simulate(cw, cf, d.rets, d.close, s_i)
        c = csim["ret"]
        assert c.index.equals(r.index)
        vc = vs_constant(r, c, i1, i2)
        mm = mm_alpha(r, q, d.rf)
        info = {"description": spec.description, "entry_close": str(d.sessions[s_i].date()),
                "full": full, "half1": h1, "half2": h2, "criteria": crit,
                "trades": sim["trades"], "trades_per_year": sim["trades"] / years, "orders": sim["orders"],
                "cost_drag_per_year": sim["cost_frac"] / years,
                "exposure": exposure_stats(e_held, e_tgt, CAP[spec.name]),
                "avg_weight": {k: float(v) for k, v in sim["held"].mean().items()},
                "constant_exposure": {"e_bar": e_bar, "trades": csim["trades"],
                                      "cost_drag_per_year": csim["cost_frac"] / years, **vc},
                "mm_alpha_vs_qqq": mm}
        # report-only sensitivities
        sens = {}
        for tag, kw in (("same_close", dict(lag=0)), ("no_costs", dict(costs=False))):
            s2 = fw.simulate(m["w"], m["flag"], d.rets, d.close, s_i, **kw)
            sens[tag] = {"cagr": cagr_of(s2["ret"]), "max_dd": max_drawdown(s2["value"]),
                         "cagr_minus_oneq": cagr_of(s2["ret"]) - full["oneq_cagr"]}
        bw, bf, _ = banded_targets(spec.name, d)
        s3 = fw.simulate(bw, bf, d.rets, d.close, s_i)
        r3 = s3["ret"]
        sens["daily_band_10pct"] = {"cagr": cagr_of(r3), "max_dd": max_drawdown(s3["value"]),
                                    "cagr_minus_oneq": cagr_of(r3) - full["oneq_cagr"],
                                    "t_vs_oneq": tstat_diff(r3, b), "trades": s3["trades"],
                                    "cost_drag_per_year": s3["cost_frac"] / years,
                                    "half1_cagr_minus_oneq": cagr_of(r3.loc[i1]) - cagr_of(b.loc[i1]),
                                    "half2_cagr_minus_oneq": cagr_of(r3.loc[i2]) - cagr_of(b.loc[i2])}
        info["sensitivities"] = sens
        # report-only long window on the spliced benchmark (includes 2000-2002)
        long_entries.append(fw.entry_index(m["w"], m["flag"], d, True))
        res["rules"][spec.name] = info
        for tag, p in (("full", full), ("half1", h1), ("half2", h2)):
            rows.append({"rule": spec.name, "period": tag, **p, "pass": crit["pass"] if tag == "full" else None})
            v = vc[tag]
            const_rows.append({"rule": spec.name, "period": tag, "e_bar": e_bar, **v})
        by_year[spec.name] = yearly(r)
        by_year[f"{spec.name}_const"] = yearly(c)
        mon[spec.name] = monthly(r)
        print(f"{spec.name:4s} {full['start']}..{full['end']} CAGR {full['cagr']:+.2%} (ONEQ {full['oneq_cagr']:+.2%}, "
              f"QQQ {full['qqq_cagr']:+.2%}) MDD {full['max_dd']:.1%} (ONEQ {full['oneq_max_dd']:.1%}) "
              f"t {full['t_monthly_excess_vs_oneq']:+.2f} | H1 {h1['cagr_minus_oneq']:+.2%} H2 {h2['cagr_minus_oneq']:+.2%}"
              f" | e_bar {e_bar:.2f} vs const {vc['full']['cagr_minus_const']:+.2%} t {vc['full']['t_monthly_excess_vs_const']:+.2f}"
              f" pass={crit['pass']}")
    # long window: common entry (the latest of the rules' first possible entries), spliced benchmark
    sL = max(long_entries)
    long = {"entry_close": str(d.sessions[sL].date())}
    bL = fw.buy_hold("BENCH", d, sL)["ret"]
    qL = fw.buy_hold("QQQ", d, sL)["ret"]
    for spec in SPECS:
        w, flag, _ = targets(spec.name, d)
        rL = fw.simulate(w, flag, d.rets, d.close, sL)["ret"]
        pm = period_metrics(rL, bL, qL, d.rf)
        long[spec.name] = {k: pm[k] for k in ("start", "end", "cagr", "oneq_cagr", "qqq_cagr", "max_dd", "oneq_max_dd",
                                              "qqq_max_dd", "t_monthly_excess_vs_oneq", "sharpe", "oneq_sharpe")}
    res["long_window_spliced_report_only"] = long
    s0 = starts[0]
    for leg, tag in (("BENCH", "ONEQ_buyhold"), ("QQQ", "QQQ_buyhold")):
        rr = fw.buy_hold(leg, d, s0)["ret"]
        by_year[tag] = yearly(rr)
        mon[tag] = monthly(rr)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "results.json").write_text(json.dumps(res, indent=2, default=float))
    pd.DataFrame(rows).to_csv(OUT / "summary.csv", index=False, float_format="%.6f")
    pd.DataFrame(const_rows).to_csv(OUT / "vs_constant_exposure.csv", index=False, float_format="%.6f")
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
