"""Cap-weighted Nasdaq replica minus predicted losers: a long-only index enhancement
(pre-registered in docs/research_ledger_index_exclusion.md, section 0).

Two base portfolios built monthly from the data-v2 U300 panel and its point-in-time market caps: B100 (the 100
largest non-financial names, cap-weighted, QQQ-like) and B300 (every U300 name with a market cap, cap-weighted).
Three loser signals reused from earlier studies (S-MISP mispricing composite of research_short_overlay, S-ML GBRT
walk-forward predictions of research_ml_cross_section, S-PROF profitability composite of research_fundamentals).
Variants: drop the worst 5 / 10 / 20 base members by signal and renormalise, or tilt weights by (1 - 0.5 x badness
percentile). $10k IBKR Tiered fractional-share simulation with the engine of research_megacap (next-close
execution, 25% rebalance band). Judged against ONEQ and, as the key diagnostic, against the matching base replica.

Usage:
  REVERSAL_DATA_VERSION=v2 PYTHONPATH=. .venv/bin/python scripts/research_index_exclusion.py --check
  REVERSAL_DATA_VERSION=v2 PYTHONPATH=. .venv/bin/python scripts/research_index_exclusion.py --register
  REVERSAL_DATA_VERSION=v2 PYTHONPATH=. .venv/bin/python scripts/research_index_exclusion.py --run
"""
from __future__ import annotations

import os

os.environ.setdefault("REVERSAL_DATA_VERSION", "v2")      # the study is defined on frozen data v2 only

import argparse  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import time  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from quant.backtest.costs import REBALANCE_BAND  # noqa: E402
from quant.evaluation.metrics import monthly as monthly_of_returns, yearly  # noqa: E402
from quant.evaluation.periods import nav_window_metrics  # noqa: E402
from quant.evaluation.stats import t_sf  # noqa: E402
from quant.prereg import Preregistration  # noqa: E402
from quant.paths import ROOT  # noqa: E402
from quant.strategies import megacap as mc  # noqa: E402
from quant.strategies import short_overlay as so  # noqa: E402

OUT = ROOT / "output/research_only/index_exclusion"
LEDGER = ROOT / "docs/research_ledger_index_exclusion.md"
FROZEN = OUT / "frozen_prereg.json"
PREREG = Preregistration(LEDGER, FROZEN, OUT)
prereg_block, prereg_hash, register = PREREG.block, PREREG.hash, PREREG.register
PRED = ROOT / "output/research_only/ml_cross_section/predictions.csv.gz"   # research_ml_cross_section output

SIGNALS = ("S-MISP", "S-ML", "S-PROF")
BASES = ("B100", "B300")
VARIANTS = ("X5", "X10", "X20", "T50")
N_TRIALS = len(SIGNALS) * len(BASES) * len(VARIANTS)       # 24
ALPHA_ONE_SIDED = 0.05
TOPN = 100
LAMBDA = 0.5
ACCOUNT = 10_000.0
BAND = REBALANCE_BAND                                   # 0.25
EXECUTIONS = {  # name -> (account, band); 'ideal' is the cost-free target-weight replica
    "main": (ACCOUNT, BAND),
    "band0": (ACCOUNT, 0.0),
    "acct100k": (100_000.0, BAND),
    "ideal": (None, None),
}
FOLDS = {  # signal -> {fold: (window start, window end)}; first signal used
    "default": {"H1": ("2012-02-01", "2018-12-31"), "H2": ("2019-01-01", "2026-08-31")},
    "S-ML": {"H1": ("2016-01-04", "2020-12-31"), "H2": ("2021-01-01", "2026-08-31")},
}
FIRST_SIGNAL = {"default": "2012-01-31", "S-ML": "2015-12-31"}


def key_of(signal: str) -> str:
    return "S-ML" if signal == "S-ML" else "default"


# ======================================================================== portfolios (sections 0.2 and 0.4)

def base_members(scores: pd.DataFrame, base: str, topn: int = TOPN) -> pd.DataFrame:
    """Rows of the base portfolio per signal date: names with a positive market cap; B100 keeps the ``topn``
    largest (ties: security id)."""
    p = scores[scores["mcap"].notna() & (scores["mcap"] > 0)]
    p = p.sort_values(["s", "mcap", "security_id"], ascending=[True, False, True])
    if base == "B100":
        return p.groupby("s", sort=True).head(topn).reset_index(drop=True)
    if base == "B300":
        return p.reset_index(drop=True)
    raise ValueError(base)


def badness_pct(score: pd.Series) -> pd.Series:
    """Percentile of 'badness' among the scored names: worst (lowest score) = 1, best = 1/n, ties averaged; NaN
    for unscored names."""
    return (-pd.to_numeric(score, errors="coerce")).rank(pct=True, method="average")


def variant_weights(g: pd.DataFrame, col: str | None, variant: str, lam: float = LAMBDA) -> pd.Series:
    """Weights (sum 1) of one signal date's base members ``g`` (columns security_id, mcap, ``col``).
    variant: 'BASE' (cap weight), 'X<k>' (drop the k worst scored members), 'T50' (tilt)."""
    w = g["mcap"].astype(float).copy()
    if variant == "BASE" or col is None:
        pass
    elif variant.startswith("X"):
        k = int(variant[1:])
        sc = g[[col, "security_id"]].dropna(subset=[col]).sort_values([col, "security_id"])
        drop = set(sc["security_id"].head(k))
        w = w[~g["security_id"].isin(drop)]
    elif variant.startswith("T"):
        p = badness_pct(g[col]).fillna(0.5)
        w = w * (1 - lam * p)
    else:
        raise ValueError(variant)
    return w / w.sum()


def make_targets(members: pd.DataFrame, col: str | None, variant: str, first: str | None = None) -> dict:
    """signal date -> [(sid, w, dv50_rank, mcap, '')] in the format of research_megacap.simulate."""
    out = {}
    for s, g in members.groupby("s", sort=True):
        if first is not None and s < pd.Timestamp(first):
            continue
        w = variant_weights(g, col, variant)
        gg = g.loc[w.index]
        out[s] = [(r.security_id, float(wi), float(r.dv50_rank) if pd.notna(r.dv50_rank) else np.nan,
                   float(r.mcap), "") for r, wi in zip(gg.itertuples(), w.to_numpy())]
    return out


# ======================================================================== simulations

def ideal_nav(targets: dict, sessions: pd.DatetimeIndex, idx: pd.DataFrame, account: float = ACCOUNT) -> pd.Series:
    """Cost-free replica: from each execution session (the session after the signal) hold the target weights,
    letting them drift until the next execution. Names without an index level at execution are dropped and the
    rest renormalised; ended series stay flat (forward-filled index, terminal value booked)."""
    sigs = sorted(targets)
    ex = [int(sessions.searchsorted(s, side="right")) for s in sigs]
    keep = [(e, s) for e, s in zip(ex, sigs) if e < len(sessions)]
    sids = sorted({sid for s in sigs for sid, *_ in targets[s]})
    I = idx[sids].ffill().to_numpy(float)
    col = {c: i for i, c in enumerate(sids)}
    out = np.full(len(sessions), np.nan)
    nav = account
    for j, (e, s) in enumerate(keep):
        e1 = keep[j + 1][0] if j + 1 < len(keep) else len(sessions) - 1
        c = np.array([col[sid] for sid, *_ in targets[s]])
        w = np.array([x[1] for x in targets[s]], float)
        base = I[e, c]
        ok = np.isfinite(base) & (base > 0)
        w, c, base = w[ok] / w[ok].sum(), c[ok], base[ok]
        rel = I[e:e1 + 1, c] / base
        rel = np.where(np.isfinite(rel), rel, np.nan)
        rel = pd.DataFrame(rel).ffill().fillna(1.0).to_numpy()
        path = nav * (rel @ w)
        out[e:e1 + 1] = path
        nav = path[-1]
    s = pd.Series(out, index=sessions).dropna()
    return s


def run_sim(targets: dict, data, qqq_lvl: pd.Series, execution: str) -> dict:
    """One portfolio under one execution; returns nav, cost, traded, orders, names (cost series zero for ideal)."""
    account, band = EXECUTIONS[execution]
    if account is None:
        nav = ideal_nav(targets, data.sessions, data.sig_idx)
        z = pd.Series(0.0, index=nav.index)
        return {"nav": nav, "cost": z, "traded": z, "orders": z.astype(int), "names": z}
    sids = sorted({sid for lst in targets.values() for sid, *_ in lst})
    sim = mc.simulate(targets, data.sessions, data.sig_idx[sids], data.close[sids], data.last_row,
                      qqq_lvl, data.qqq_close, account=account, band=band)
    return sim


# ======================================================================== statistics

def monthly(nav: pd.Series) -> pd.Series:
    return monthly_of_returns(nav.pct_change().dropna())


def window(nav: pd.Series, a: str, b: str) -> pd.Series:
    """``nav`` over [a, b], starting at the last value before a (or the first value inside)."""
    a_, b_ = pd.Timestamp(a), pd.Timestamp(b)
    before = nav.index[nav.index < a_]
    t0 = before[-1] if len(before) else nav.index[0]
    return nav[(nav.index >= t0) & (nav.index <= b_)]


def relative_stats(nav: pd.Series, ref: pd.Series, a: str, b: str) -> dict:
    """Excess of ``nav`` over ``ref`` within [a, b]: CAGR difference, monthly excess mean (annualised) and t,
    daily tracking error, OLS alpha / beta on monthly returns, correlation of daily returns."""
    v = window(nav, a, b)
    r = ref.reindex(v.index)
    years = (v.index[-1] - v.index[0]).days / 365.25
    cg = (v.iloc[-1] / v.iloc[0]) ** (1 / years) - 1
    cr = (r.iloc[-1] / r.iloc[0]) ** (1 / years) - 1
    dv, dr = v.pct_change().iloc[1:], r.pct_change().iloc[1:]
    act = dv - dr
    mv, mr = monthly_of_returns(dv), monthly_of_returns(dr)
    ex = mv - mr
    ols = so.ols_alpha(mv, mr)
    return {"cagr_diff": cg - cr, "ex_ann": float(ex.mean() * 12), "t": so.tstat(ex), "months": int(len(ex)),
            "te": float(act.std(ddof=1) * math.sqrt(252)), "alpha_ann": ols["alpha_ann"], "alpha_t": ols["alpha_t"],
            "beta": ols["beta"], "corr": float(np.corrcoef(dv, dr)[0, 1]), "ex_monthly": ex}


def fold_criteria(m: dict) -> dict:
    a = (m["cagr"] > m["oneq_cagr"]) and (m["t_monthly_excess_vs_oneq"] >= 2.0)
    b = (m["dd_shallower_than_oneq_pp"] >= 10.0) and (m["cagr"] - m["oneq_cagr"] >= -0.03)
    return {"A": bool(a), "B": bool(b), "pass": bool(a or b)}


def bonferroni_t(df: int, n: int = N_TRIALS) -> float:
    return so.bonferroni_t(df, n=n, alpha=ALPHA_ONE_SIDED)


def verdict_row(per_fold: dict, ex_oneq: list, ex_base: list, n_trials: int = N_TRIALS) -> dict:
    """Section 0.7: per_fold = {fold: metrics with A/B and base excess mean}; ex_* = monthly excess series per fold."""
    eo, eb = pd.concat(ex_oneq), pd.concat(ex_base)
    t_o, t_b = so.tstat(eo), so.tstat(eb)
    hurdle = bonferroni_t(len(eo) - 1, n_trials)
    folds_ok = all(m["pass"] for m in per_fold.values())
    c1 = bool(folds_ok and t_o >= hurdle)
    pos = all(m["ex_base_ann"] > 0 for m in per_fold.values())
    c2 = bool(pos and t_b >= hurdle)
    return {"folds_pass_vs_oneq": bool(folds_ok), "t_pooled_vs_oneq": t_o, "t_pooled_vs_base": t_b,
            "p_vs_base_one_sided": t_sf(t_b, len(eb) - 1) if np.isfinite(t_b) else np.nan,
            "ex_base_positive_both": bool(pos), "bonferroni_t": hurdle, "cond1_vs_oneq": c1,
            "cond2_exclusion_alpha": c2, "nominal_cond2_t2": bool(pos and t_b >= 2.0), "pass": bool(c1 and c2)}


# ======================================================================== data loading

def load():
    from quant.strategies import ml_features as ml
    from quant.data import version as dv
    if not dv.IS_V2:
        raise SystemExit("set REVERSAL_DATA_VERSION=v2")
    data, sigs, panel, oneq_lvl, oneq_px = ml.build_panel()
    pred = pd.read_csv(PRED, dtype={"security_id": str}, parse_dates=["s"])
    panel = panel.copy()
    panel["security_id"] = panel["security_id"].astype(str)
    end = data.spec["effective_end"]
    qtr = ml.qqq_total_returns(end).reindex(data.sessions).fillna(0.0)
    qqq_lvl = (1 + qtr).cumprod()
    scores = so.signal_scores(panel, pred)
    if pd.Series(data.sessions).max() > pd.Timestamp(end):
        raise RuntimeError("sessions beyond end")
    return data, scores, oneq_lvl, oneq_px, qqq_lvl


def coverage(scores: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for base in BASES:
        m = base_members(scores, base)
        for y, g in m.groupby(m["s"].dt.year):
            r = {"base": base, "year": int(y), "avg_members": round(len(g) / g["s"].nunique(), 1),
                 "u300_avg": round(len(scores[scores["s"].dt.year == y]) / g["s"].nunique(), 1),
                 "u300_mcap_coverage": round(float(scores.loc[scores["s"].dt.year == y, "mcap"].notna().mean()), 3)}
            for c in SIGNALS:
                r[f"{c}_cov"] = round(float(g[c].notna().mean()), 3)
                r[f"{c}_cov_capw"] = round(float((g["mcap"] * g[c].notna()).sum() / g["mcap"].sum()), 3)
            rows.append(r)
    return pd.DataFrame(rows)


# ======================================================================== registration


def excluded_names(scores: pd.DataFrame, base: str, signal: str, k: int) -> pd.DataFrame:
    m = base_members(scores, base)
    rows = []
    for s, g in m.groupby("s", sort=True):
        tot = g["mcap"].sum()
        sc = g.dropna(subset=[signal]).sort_values([signal, "security_id"]).head(k)
        rows.append({"s": s.date().isoformat(), "base": base, "signal": signal, "k": k,
                     "names": " ".join(sc["ticker"].astype(str)), "excluded_capw": float(sc["mcap"].sum() / tot),
                     "median_member_rank": float(np.median([list(g["security_id"]).index(x) + 1
                                                            for x in sc["security_id"]])) if len(sc) else np.nan})
    return pd.DataFrame(rows)


def check(args):
    OUT.mkdir(parents=True, exist_ok=True)
    data, scores, oneq, oneq_px, qqq = load()
    cov = coverage(scores)
    cov.to_csv(OUT / "check_coverage.csv", index=False)
    pd.set_option("display.width", 250)
    print(cov.to_string())
    ex = pd.concat([excluded_names(scores, b, sig, 10) for b in BASES for sig in SIGNALS], ignore_index=True)
    ex.to_csv(OUT / "check_excluded_X10.csv", index=False)
    print(ex.groupby(["base", "signal"])["excluded_capw"].describe().to_string())
    for s in ("2014-12-31", "2020-12-31", "2026-07-31"):
        print(ex[ex["s"] == s][["base", "signal", "names", "excluded_capw"]].to_string())
    for b in BASES:
        m = base_members(scores, b)
        last = m[m["s"] == m["s"].max()]
        print(b, "last month top-10 weight", round(float(last["mcap"].head(10).sum() / last["mcap"].sum()), 3),
              "n", len(last))


# ======================================================================== run

def run(args):
    if not FROZEN.exists():
        raise SystemExit("not registered")
    if json.loads(FROZEN.read_text())["sha256"] != prereg_hash():
        raise SystemExit("pre-registration text changed since --register")
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    data, scores, oneq_lvl, oneq_px, qqq_lvl = load()
    log = []

    def say(*a):
        msg = " ".join(str(x) for x in a)
        print(msg, flush=True)
        log.append(msg)

    say("DATE GUARD:", data.guard["assertion"])
    members = {b: base_members(scores, b) for b in BASES}
    # ---- targets: bases from both first-signal dates, variants per signal
    tgts = {}
    for key, first in FIRST_SIGNAL.items():
        for b in BASES:
            tgts[(b, "BASE", key)] = make_targets(members[b], None, "BASE", first)
    for sig in SIGNALS:
        key = key_of(sig)
        for b in BASES:
            for v in VARIANTS:
                tgts[(b, f"{sig}_{v}", key)] = make_targets(members[b], sig, v, FIRST_SIGNAL[key])
    # ---- simulations
    sims = {}
    for ex in EXECUTIONS:
        for k_, tg in tgts.items():
            sims[(ex,) + k_] = run_sim(tg, data, qqq_lvl, ex)
        say(f"execution {ex}: {len(tgts)} portfolios ({time.time() - t0:.0f}s)")

    rows, ver_rows, years_rows, val_rows, nav_out = [], [], [], [], []
    for (ex, b, name, key), sim in sims.items():
        nav = sim["nav"]
        dates = nav.index
        oneq = mc.buy_hold(oneq_lvl, oneq_px, dates, mc.ONEQ_HS)
        qqq = mc.buy_hold(qqq_lvl, data.qqq_close, dates, mc.QQQ_HS)
        base_nav = sims[(ex, b, "BASE", key)]["nav"]
        per_fold, ex_o, ex_b = {}, [], []
        folds = dict(FOLDS[key])
        folds["ALL"] = (folds["H1"][0], folds["H2"][1])
        for fold, (a, bb) in folds.items():
            m = nav_window_metrics(nav, oneq, qqq, a, bb, sim["cost"], sim["traded"], sim["orders"])
            rq = relative_stats(nav, qqq, a, bb)
            rb = relative_stats(nav, base_nav, a, bb)
            ro = relative_stats(nav, oneq, a, bb)
            w = window(nav, a, bb)
            names = sim["names"].reindex(w.index)
            orders = m.get("orders", 0) or 0
            row = {"execution": ex, "base": b, "portfolio": name, "signal": name.split("_")[0] if name != "BASE" else "",
                   "variant": name.split("_")[1] if name != "BASE" else "BASE", "folds": key, "fold": fold, **m,
                   "te_vs_qqq": rq["te"], "corr_vs_qqq": rq["corr"], "corr_vs_oneq": ro["corr"],
                   "ex_base_cagr": rb["cagr_diff"], "ex_base_ann": rb["ex_ann"], "t_vs_base": rb["t"],
                   "te_vs_base": rb["te"], "alpha_vs_base_ann": rb["alpha_ann"], "alpha_vs_base_t": rb["alpha_t"],
                   "beta_vs_base": rb["beta"], "avg_names": float(names.mean()) if len(names) else np.nan,
                   "avg_order_usd": float(sim["traded"].reindex(w.index).iloc[1:].sum() / orders) if orders else np.nan,
                   "orders_per_year": orders / m["years"] if m["years"] else np.nan}
            if fold != "ALL":
                row.update(fold_criteria(m))
                per_fold[fold] = row
                ex_o.append(ro["ex_monthly"])
                ex_b.append(rb["ex_monthly"])
            rows.append(row)
        is_trial = name != "BASE"
        if (key == "default") or is_trial:          # S-ML-start bases are only comparators
            v = verdict_row(per_fold, ex_o, ex_b)
            ver_rows.append({"execution": ex, "base": b, "portfolio": name, "trial": is_trial, "folds": key,
                             **{f"{f}_{c}": per_fold[f][c] for f in ("H1", "H2") for c in ("A", "B", "pass")},
                             **{f"{f}_ex_base_ann": per_fold[f]["ex_base_ann"] for f in ("H1", "H2")},
                             **{f"{f}_t_vs_base": per_fold[f]["t_vs_base"] for f in ("H1", "H2")}, **v})
        if ex == "main":
            yb = yearly(base_nav.pct_change().dropna())
            yo, yq = yearly(oneq.pct_change().dropna()), yearly(qqq.pct_change().dropna())
            for yy, val in yearly(nav.pct_change().dropna()).items():
                years_rows.append({"base": b, "portfolio": name, "folds": key, "year": yy, "strategy": val,
                                   "base_ret": yb.get(yy), "oneq": yo.get(yy), "qqq": yq.get(yy)})
            nv = nav.rename("nav").to_frame()
            nv["base"], nv["portfolio"], nv["folds"] = b, name, key
            nav_out.append(nv.reset_index().rename(columns={"index": "date"}))
        if name == "BASE" and key == "default":     # replica validation (section 0.2)
            for fold, (a, bb) in folds.items():
                for ref_name, ref in (("QQQ", qqq), ("ONEQ", oneq)):
                    r = relative_stats(nav, ref, a, bb)
                    val_rows.append({"execution": ex, "base": b, "vs": ref_name, "fold": fold,
                                     "cagr_diff": r["cagr_diff"], "ex_ann": r["ex_ann"], "t": r["t"],
                                     "tracking_error": r["te"], "corr": r["corr"], "beta": r["beta"]})
    summ = pd.DataFrame(rows)
    summ.to_csv(OUT / "summary.csv", index=False)
    ver = pd.DataFrame(ver_rows)
    ver.to_csv(OUT / "verdict.csv", index=False)
    pd.DataFrame(years_rows).to_csv(OUT / "by_year.csv", index=False)
    pd.DataFrame(val_rows).to_csv(OUT / "replica_validation.csv", index=False)
    pd.concat(nav_out, ignore_index=True).to_csv(OUT / "nav_daily.csv.gz", index=False)
    pd.concat([excluded_names(scores, b, sig, 10) for b in BASES for sig in SIGNALS],
              ignore_index=True).to_csv(OUT / "excluded_X10.csv", index=False)
    main = ver[(ver["execution"] == "main") & ver["trial"]]
    res = {"prereg_sha256": prereg_hash(), "n_trials": N_TRIALS,
           "verdict": "PASS" if main["pass"].any() else "FAIL", "n_pass": int(main["pass"].sum()),
           "n_cond1": int(main["cond1_vs_oneq"].sum()), "n_cond2": int(main["cond2_exclusion_alpha"].sum()),
           "n_nominal_cond2": int(main["nominal_cond2_t2"].sum()),
           "max_t_vs_base": float(main["t_pooled_vs_base"].max()),
           "bonferroni_t_typical": float(main["bonferroni_t"].median()),
           "guard": data.guard["assertion"], "runtime_s": round(time.time() - t0, 1)}
    (OUT / "results.json").write_text(json.dumps(res, indent=1, default=str) + "\n")
    pd.set_option("display.width", 250)
    say(main[["base", "portfolio", "H1_pass", "H2_pass", "t_pooled_vs_oneq", "H1_ex_base_ann", "H2_ex_base_ann",
              "t_pooled_vs_base", "pass"]].to_string())
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
