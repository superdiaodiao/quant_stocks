"""Cap-weighted Nasdaq replica minus predicted losers (docs/research_ledger_index_exclusion.md, section 0): base
portfolios (B100 / B300), drop-the-worst-k and tilt variants, the cost-free replica, the relative statistics against
a reference series and the section 0.7 verdict.

Moved unchanged from studies/index_exclusion.py (``base_members``, ``badness_pct``, ``variant_weights``,
``make_targets``, ``ideal_nav``, ``run_sim``, ``window``, ``relative_stats``, ``fold_criteria``, ``verdict_row``,
``coverage``, ``excluded_names`` and the per-portfolio evaluation loop of ``run`` as ``evaluate``). The study's
frozen numbers (top N, tilt lambda, account, executions, trials, folds) are parameters. The engine is
quant.strategies.megacap.simulate; t-statistics are ``stats.t_of_mean`` (min 3 values), as in short_overlay.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from quant.evaluation.criteria import bonferroni_t_student
from quant.evaluation.metrics import monthly as monthly_of_returns, yearly
from quant.evaluation.periods import nav_window_metrics
from quant.evaluation.stats import ols_alpha, t_of_mean, t_sf
from quant.strategies import megacap as mc


# ======================================================================== portfolios (sections 0.2 and 0.4)

def base_members(scores: pd.DataFrame, base: str, topn: int) -> pd.DataFrame:
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


def variant_weights(g: pd.DataFrame, col: str | None, variant: str, lam: float) -> pd.Series:
    """Weights (sum 1) of one signal date's base members ``g`` (columns security_id, mcap, ``col``).
    variant: 'BASE' (cap weight), 'X<k>' (drop the k worst scored members), 'T50' (tilt by 1 - lam x badness)."""
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


def make_targets(members: pd.DataFrame, col: str | None, variant: str, lam: float,
                 first: str | None = None) -> dict:
    """signal date -> [(sid, w, dv50_rank, mcap, '')] in the format of quant.strategies.megacap.simulate."""
    out = {}
    for s, g in members.groupby("s", sort=True):
        if first is not None and s < pd.Timestamp(first):
            continue
        w = variant_weights(g, col, variant, lam)
        gg = g.loc[w.index]
        out[s] = [(r.security_id, float(wi), float(r.dv50_rank) if pd.notna(r.dv50_rank) else np.nan,
                   float(r.mcap), "") for r, wi in zip(gg.itertuples(), w.to_numpy())]
    return out


# ======================================================================== simulations

def ideal_nav(targets: dict, sessions: pd.DatetimeIndex, idx: pd.DataFrame, account: float) -> pd.Series:
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


def run_sim(targets: dict, data, qqq_lvl: pd.Series, account: float | None, band: float | None,
            ideal_account: float) -> dict:
    """One portfolio under one execution (``account`` / ``band``; ``account`` None = the cost-free replica of
    ``ideal_account``); returns nav, cost, traded, orders, names (cost series zero for ideal)."""
    if account is None:
        nav = ideal_nav(targets, data.sessions, data.sig_idx, ideal_account)
        z = pd.Series(0.0, index=nav.index)
        return {"nav": nav, "cost": z, "traded": z, "orders": z.astype(int), "names": z}
    sids = sorted({sid for lst in targets.values() for sid, *_ in lst})
    sim = mc.simulate(targets, data.sessions, data.sig_idx[sids], data.close[sids], data.last_row,
                      qqq_lvl, data.qqq_close, account=account, band=band)
    return sim


# ======================================================================== statistics

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
    ols = ols_alpha(mv, mr)
    return {"cagr_diff": cg - cr, "ex_ann": float(ex.mean() * 12), "t": t_of_mean(ex), "months": int(len(ex)),
            "te": float(act.std(ddof=1) * math.sqrt(252)), "alpha_ann": ols["alpha_ann"], "alpha_t": ols["alpha_t"],
            "beta": ols["beta"], "corr": float(np.corrcoef(dv, dr)[0, 1]), "ex_monthly": ex}


def fold_criteria(m: dict) -> dict:
    """A: CAGR above ONEQ's and monthly-excess t >= 2; B: drawdown >= 10 points shallower and CAGR - ONEQ CAGR
    >= -0.03 (``nav_window_metrics`` keys)."""
    a = (m["cagr"] > m["oneq_cagr"]) and (m["t_monthly_excess_vs_oneq"] >= 2.0)
    b = (m["dd_shallower_than_oneq_pp"] >= 10.0) and (m["cagr"] - m["oneq_cagr"] >= -0.03)
    return {"A": bool(a), "B": bool(b), "pass": bool(a or b)}


def verdict_row(per_fold: dict, ex_oneq: list, ex_base: list, n_trials: int, alpha: float) -> dict:
    """Section 0.7: per_fold = {fold: metrics with A/B and base excess mean}; ex_* = monthly excess series per fold.
    The hurdle is the one-sided Student-t Bonferroni value for ``n_trials`` at ``alpha``."""
    eo, eb = pd.concat(ex_oneq), pd.concat(ex_base)
    t_o, t_b = t_of_mean(eo), t_of_mean(eb)
    hurdle = bonferroni_t_student(len(eo) - 1, n_trials, alpha)
    folds_ok = all(m["pass"] for m in per_fold.values())
    c1 = bool(folds_ok and t_o >= hurdle)
    pos = all(m["ex_base_ann"] > 0 for m in per_fold.values())
    c2 = bool(pos and t_b >= hurdle)
    return {"folds_pass_vs_oneq": bool(folds_ok), "t_pooled_vs_oneq": t_o, "t_pooled_vs_base": t_b,
            "p_vs_base_one_sided": t_sf(t_b, len(eb) - 1) if np.isfinite(t_b) else np.nan,
            "ex_base_positive_both": bool(pos), "bonferroni_t": hurdle, "cond1_vs_oneq": c1,
            "cond2_exclusion_alpha": c2, "nominal_cond2_t2": bool(pos and t_b >= 2.0), "pass": bool(c1 and c2)}


# ======================================================================== checks

def coverage(scores: pd.DataFrame, bases: tuple, signals: tuple, topn: int) -> pd.DataFrame:
    rows = []
    for base in bases:
        m = base_members(scores, base, topn)
        for y, g in m.groupby(m["s"].dt.year):
            r = {"base": base, "year": int(y), "avg_members": round(len(g) / g["s"].nunique(), 1),
                 "u300_avg": round(len(scores[scores["s"].dt.year == y]) / g["s"].nunique(), 1),
                 "u300_mcap_coverage": round(float(scores.loc[scores["s"].dt.year == y, "mcap"].notna().mean()), 3)}
            for c in signals:
                r[f"{c}_cov"] = round(float(g[c].notna().mean()), 3)
                r[f"{c}_cov_capw"] = round(float((g["mcap"] * g[c].notna()).sum() / g["mcap"].sum()), 3)
            rows.append(r)
    return pd.DataFrame(rows)


def excluded_names(scores: pd.DataFrame, base: str, signal: str, k: int, topn: int) -> pd.DataFrame:
    m = base_members(scores, base, topn)
    rows = []
    for s, g in m.groupby("s", sort=True):
        tot = g["mcap"].sum()
        sc = g.dropna(subset=[signal]).sort_values([signal, "security_id"]).head(k)
        rows.append({"s": s.date().isoformat(), "base": base, "signal": signal, "k": k,
                     "names": " ".join(sc["ticker"].astype(str)), "excluded_capw": float(sc["mcap"].sum() / tot),
                     "median_member_rank": float(np.median([list(g["security_id"]).index(x) + 1
                                                            for x in sc["security_id"]])) if len(sc) else np.nan})
    return pd.DataFrame(rows)


# ======================================================================== evaluation of every simulated portfolio

def evaluate(sims: dict, data, oneq_lvl, oneq_px, qqq_lvl, folds: dict, n_trials: int, alpha: float) -> tuple:
    """For each (execution, base, portfolio, folds key) simulation: per-fold metrics vs ONEQ / QQQ / its base
    replica, the section 0.7 verdict (trials and the default-start bases), calendar-year returns and NAVs of the
    main execution, and the replica validation of the default-start bases. ``folds`` = {key: {H1, H2}}.
    Returns (rows, verdict rows, year rows, validation rows, nav frames)."""
    rows, ver_rows, years_rows, val_rows, nav_out = [], [], [], [], []
    for (ex, b, name, key), sim in sims.items():
        nav = sim["nav"]
        dates = nav.index
        oneq = mc.buy_hold(oneq_lvl, oneq_px, dates, mc.ONEQ_HS)
        qqq = mc.buy_hold(qqq_lvl, data.qqq_close, dates, mc.QQQ_HS)
        base_nav = sims[(ex, b, "BASE", key)]["nav"]
        per_fold, ex_o, ex_b = {}, [], []
        fds = dict(folds[key])
        fds["ALL"] = (fds["H1"][0], fds["H2"][1])
        for fold, (a, bb) in fds.items():
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
            v = verdict_row(per_fold, ex_o, ex_b, n_trials, alpha)
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
            for fold, (a, bb) in fds.items():
                for ref_name, ref in (("QQQ", qqq), ("ONEQ", oneq)):
                    r = relative_stats(nav, ref, a, bb)
                    val_rows.append({"execution": ex, "base": b, "vs": ref_name, "fold": fold,
                                     "cagr_diff": r["cagr_diff"], "ex_ann": r["ex_ann"], "t": r["t"],
                                     "tracking_error": r["te"], "corr": r["corr"], "beta": r["beta"]})
    return rows, ver_rows, years_rows, val_rows, nav_out
