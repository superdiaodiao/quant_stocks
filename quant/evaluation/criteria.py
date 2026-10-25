"""The pre-registered pass criteria and the multiple-testing corrections.

Every October-2026 ledger judges a strategy against ONEQ (total return) with the same two alternatives:

- A (higher return): CAGR above the benchmark's in every judged part, and the t-statistic of the monthly excess
  over the full period >= 2.0;
- B (shallower drawdown): in every judged part, a maximum drawdown at least 10 percentage points shallower than the
  benchmark's while the CAGR is no more than 3 points below it.

A study passes on A or B. Which parts are judged (two halves, or full + halves) and the output labels stay in
the study. ``ab_criteria`` was factored out of scripts/research_regime.py ``evaluate``,
scripts/research_megacap.py ``criteria`` and scripts/research_selective_t.py ``judge`` without changing a
comparison. ``bonferroni_t`` / ``deflated_sharpe`` come from scripts/research_qqq_timing.py, ``bh_reject`` from
scripts/research_selective_t.py.
"""
from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np

from quant.evaluation.stats import stationary_bootstrap_index, t_sf

NORM = NormalDist()
A_T_MIN = 2.0
B_DD_PP = 10.0
B_CAGR_TOL = 0.03
# the A / B labels of the regime ledger (also research_mean_reversion)
# the A / B labels of the megacap ledger, judged on two halves (also fundamentals)
HALVES_AB_LABELS = ("A_cagr_above_oneq_both_halves_and_full_t_ge_2", "B_dd_10pp_shallower_and_cagr_within_3pp_both_halves")
REGIME_AB_LABELS = ("A_higher_cagr_all_three_and_t_ge_2", "B_dd_10pp_shallower_and_cagr_within_3pp_all_three")


def ab_criteria(parts, full: dict, *, cagr: str = "cagr", bench: str | None = "oneq_cagr",
                t: str = "t_monthly_excess_vs_oneq", dd: str = "dd_shallower_than_oneq_pp") -> tuple[bool, bool]:
    """(A, B) for the judged ``parts`` (metric dicts) and the ``full``-period dict.

    ``cagr`` / ``bench`` name the strategy's and the benchmark's CAGR; with ``bench=None`` the ``cagr`` key is
    already an excess return and is compared with 0."""
    def base(p):
        return 0.0 if bench is None else p[bench]
    a = all(p[cagr] > base(p) for p in parts) and full[t] >= A_T_MIN
    b = all(p[dd] >= B_DD_PP and p[cagr] >= base(p) - B_CAGR_TOL for p in parts)
    return bool(a), bool(b)


def ab_verdict(full: dict, h1: dict, h2: dict, labels: tuple = ("A", "B")) -> dict:
    """{A, B, "pass"} of ``ab_criteria`` on the full period and both halves (default ONEQ keys), the A / B keys
    named ``labels``: the ``evaluate`` of scripts/research_sector_lev.py (also research_voltarget,
    research_leverage_methods) and of studies/regime.py (also research_mean_reversion, with the long labels)."""
    return ab_verdict_on((full, h1, h2), full, labels)


def ab_verdict_on(parts, full: dict, labels: tuple = ("A", "B")) -> dict:
    """{A, B, "pass"} of ``ab_criteria(parts, full)`` with the A / B keys named ``labels`` (e.g. the two halves of
    the megacap ledger, ``HALVES_AB_LABELS``)."""
    a, b = ab_criteria(parts, full)
    return {labels[0]: a, labels[1]: b, "pass": bool(a or b)}


def bonferroni_t(n: int, alpha: float = 0.05) -> float:
    """One-sided normal critical value for ``n`` trials at family-wise level ``alpha``."""
    return NORM.inv_cdf(1 - alpha / n)


def bh_reject(p, q: float = 0.05) -> np.ndarray:
    """Benjamini-Hochberg: which p-values are rejected at false-discovery rate ``q``."""
    p = np.asarray(p, float)
    order = np.argsort(p)
    below = p[order] <= q * np.arange(1, len(p) + 1) / len(p)
    rej = np.zeros(len(p), bool)
    if below.any():
        rej[order[: np.max(np.where(below)[0]) + 1]] = True
    return rej


def bh_reject_finite(pvals, q: float = 0.05) -> np.ndarray:
    """Benjamini-Hochberg over the finite p-values only (the count m excludes NaNs, which are never rejected),
    unlike ``bh_reject`` which counts every entry (osap_screen)."""
    p = np.asarray(pvals, float)
    ok = np.isfinite(p)
    m = ok.sum()
    rej = np.zeros(len(p), bool)
    if m == 0:
        return rej
    idx = np.where(ok)[0][np.argsort(p[ok])]
    ranked = p[idx]
    below = ranked <= q * np.arange(1, m + 1) / m
    if below.any():
        k = np.max(np.where(below)[0])
        rej[idx[:k + 1]] = True
    return rej


def bonferroni_t_student(df: int, n: int, alpha: float = 0.05) -> float:
    """One-sided Student-t critical value with ``df`` degrees of freedom for ``n`` trials at family-wise level
    ``alpha`` (bisection on ``stats.t_sf``, 100 steps on [0, 10]; the data-v2 studies)."""
    lo, hi = 0.0, 10.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if t_sf(mid, df) > alpha / n:
            lo = mid
        else:
            hi = mid
    return hi


def deflated_sharpe(sr: float, n_obs: int, skew: float, kurt: float, sr_trials: np.ndarray,
                    period: str = "monthly") -> dict:
    """Bailey and Lopez de Prado (2014), per-period Sharpe ratios (``period`` only names the ``sr0_<period>`` key)."""
    n = len(sr_trials)
    var = float(np.var(sr_trials, ddof=1))
    g = 0.5772156649
    sr0 = math.sqrt(var) * ((1 - g) * NORM.inv_cdf(1 - 1 / n) + g * NORM.inv_cdf(1 - 1 / (n * math.e)))
    denom = math.sqrt(max(1 - skew * sr + (kurt - 1) / 4 * sr ** 2, 1e-12))
    z = (sr - sr0) * math.sqrt(n_obs - 1) / denom
    return {"n_trials": n, f"sr0_{period}": sr0, "dsr": float(NORM.cdf(z)), "z": z}


def deflated_sharpe_n(sr: float, n_obs: int, skew: float, kurt: float, n_trials: int, var_trials: float) -> dict:
    """``deflated_sharpe`` with the trial count and the variance of the trials' Sharpe ratios given directly (for a
    family of trials too large to list; signal_combo). Same formula and per-period Sharpe ratios."""
    g = 0.5772156649
    sr0 = math.sqrt(var_trials) * ((1 - g) * NORM.inv_cdf(1 - 1 / n_trials)
                                   + g * NORM.inv_cdf(1 - 1 / (n_trials * math.e)))
    denom = math.sqrt(max(1 - skew * sr + (kurt - 1) / 4 * sr ** 2, 1e-12))
    z = (sr - sr0) * math.sqrt(n_obs - 1) / denom
    return {"n_trials": int(n_trials), "var_trials": float(var_trials), "sr0": sr0, "dsr": float(NORM.cdf(z)), "z": z}


def weekly_deflated_sharpe(sr: float, n_obs: int, skew: float, kurt: float, sr_trials: np.ndarray) -> dict:
    """``deflated_sharpe`` of weekly Sharpe ratios (key ``sr0_weekly``): scripts/research_reversal_dev.py's version,
    also used by the stock studies that took it from there."""
    return deflated_sharpe(sr, n_obs, skew, kurt, sr_trials, period="weekly")


def timing_criteria(rule: dict, qqq: dict) -> dict:
    """The QQQ timing one-shot criteria (all net of costs; all must hold): maximum drawdown at least 10 points
    shallower than QQQ's, a higher Calmar, CAGR at most 3 points below (scripts/research_qqq_timing.py
    ``evaluate_criteria``)."""
    c1 = abs(rule["max_dd"]) <= abs(qqq["max_dd"]) - 0.10
    c2 = rule["calmar"] > qqq["calmar"]
    c3 = rule["cagr"] >= qqq["cagr"] - 0.03
    return {"c1_maxdd_at_least_10pp_shallower": bool(c1), "c2_calmar_higher": bool(c2),
            "c3_cagr_shortfall_at_most_3pp": bool(c3), "pass": bool(c1 and c2 and c3)}


# ---- stock-panel signal studies (indicators, oneil, earnings_events, stops, walk_forward): the A / B rule on plain
# numbers, and on a full period plus its halves. Same comparisons as ``ab_criteria`` but on absolute drawdowns
# (``abs(mdd) <= abs(b_mdd) - 0.10``), a different floating-point expression, so they are kept apart.

def ab_oneq(cagr: float, mdd: float, b_cagr: float, b_mdd: float, t: float) -> dict:
    """{A, B, pass}: A = CAGR above the benchmark's and t >= 2; B = |max DD| at least 10 points below the
    benchmark's and CAGR at most 3 points below (was ``strategies.indicators.criteria``; also ``stops.judge``)."""
    a = bool(cagr > b_cagr and t >= 2)
    b = bool(abs(mdd) <= abs(b_mdd) - 0.10 and cagr >= b_cagr - 0.03)
    return {"A": a, "B": b, "pass": a or b}


def ab_oneq_halves(full: dict, halves: list[dict]) -> dict:
    """``ab_oneq`` on the full period whose A also needs CAGR above the benchmark in every half (keys ``cagr``,
    ``bench_cagr``, ``max_dd``, ``bench_max_dd``, ``t_monthly_excess``; was ``studies/walk_forward.py`` ``judge``)."""
    a = bool(full["cagr"] > full["bench_cagr"] and all(h["cagr"] > h["bench_cagr"] for h in halves)
             and full["t_monthly_excess"] >= 2)
    b = bool(abs(full["max_dd"]) <= abs(full["bench_max_dd"]) - 0.10 and full["cagr"] >= full["bench_cagr"] - 0.03)
    return {"A": a, "B": b, "pass": a or b}


def oneshots_ab_vs_qqq(judged: dict, monthly_active) -> tuple[dict, dict]:
    """(A, B) of the livermore one-shots: ``judged`` = {window: metrics with ``excess_cagr``, ``max_dd``,
    ``qqq_max_dd``}, ``monthly_active`` = both windows' monthly active returns. A: excess > 0 in each window and the
    combined monthly t >= 2; B: in each window ``max_dd - qqq_max_dd >= 0.10`` and excess >= -0.03."""
    ma, j = monthly_active, judged
    t = float(ma.mean() / ma.std(ddof=1) * math.sqrt(len(ma)))
    crit_a = {"excess_positive_each": {n: j[n]["excess_cagr"] > 0 for n in j}, "combined_months": len(ma),
              "combined_t_monthly": t}
    crit_a["pass"] = all(crit_a["excess_positive_each"].values()) and t >= 2
    crit_b = {n: {"dd_shallower_pts": j[n]["max_dd"] - j[n]["qqq_max_dd"],
                  "cagr_gap_pts": j[n]["excess_cagr"],
                  "pass": (j[n]["max_dd"] - j[n]["qqq_max_dd"] >= 0.10) and (j[n]["excess_cagr"] >= -0.03)} for n in j}
    crit_b["pass"] = all(v["pass"] for v in crit_b.values() if isinstance(v, dict))
    return crit_a, crit_b


def oneshots_ab_vs_oneq(judged: dict, monthly_active) -> dict:
    """{A, B, pass} of the indicators stock one-shots: ``judged`` = {window: metrics with ``cagr``, ``bench_cagr``,
    ``max_dd``, ``bench_max_dd``}. A: CAGR above ONEQ in each window and the combined monthly t >= 2; B: in each
    window ``|ONEQ DD| - |DD| >= 0.10`` and ``CAGR - ONEQ CAGR >= -0.03``."""
    ma, j = monthly_active, judged
    t = float(ma.mean() / ma.std(ddof=1) * math.sqrt(len(ma)))
    a = {"cagr_above_oneq_each": {n: j[n]["cagr"] > j[n]["bench_cagr"] for n in j}, "combined_months": len(ma),
         "combined_t_monthly": t}
    a["pass"] = all(a["cagr_above_oneq_each"].values()) and t >= 2
    b = {n: {"dd_shallower_pts": abs(j[n]["bench_max_dd"]) - abs(j[n]["max_dd"]),
             "cagr_gap_pts": j[n]["cagr"] - j[n]["bench_cagr"]} for n in j}
    for n in j:
        b[n]["pass"] = b[n]["dd_shallower_pts"] >= 0.10 and b[n]["cagr_gap_pts"] >= -0.03
    b["pass"] = all(v["pass"] for v in b.values() if isinstance(v, dict))
    return {"A": a, "B": b, "pass": a["pass"] or b["pass"]}


# ---- G3 (fundamentals): Benjamini-Hochberg with the step-up threshold computed as q x (rank / m). ``bh_reject``
# computes (q x rank) / m: the same rule, but a different floating-point expression, so a p-value exactly at a
# threshold can be decided differently; kept apart.

def bh_reject_rank_share(p, q: float = 0.05) -> np.ndarray:
    """Benjamini-Hochberg step-up over every entry: reject the k smallest p-values where k is the largest rank with
    p_(k) <= q * (k / m) (was ``studies/fundamentals.py`` ``bh_reject``)."""
    p = np.asarray(p, float)
    n = len(p)
    order = np.argsort(p)
    ranked = p[order]
    ok = ranked <= q * (np.arange(1, n + 1) / n)
    rej = np.zeros(n, bool)
    if ok.any():
        k = np.max(np.nonzero(ok)[0])
        rej[order[:k + 1]] = True
    return rej


# ---- correlated trials (dip_2x review, report only): the number of effectively independent tests and a family-wise
# max-t hurdle that keeps the correlation between configurations.

def effective_trials(x) -> dict:
    """Effective number of independent tests among the columns of ``x`` (observations x tests), from the eigenvalues
    of their correlation matrix: Nyholt (2004) 1 + (M - 1)(1 - var(lambda) / M) and Li & Ji (2005)
    sum(1[lambda >= 1] + lambda - floor(lambda))."""
    lam = np.clip(np.linalg.eigvalsh(np.corrcoef(np.asarray(x, float).T)), 0.0, None)
    m = len(lam)
    return {"tests": m, "nyholt": float(1 + (m - 1) * (1 - np.var(lam, ddof=1) / m)),
            "li_ji": float(np.sum((lam >= 1).astype(float) + (lam - np.floor(lam))))}


def sidak_t(n_eff: float, alpha: float = 0.05) -> float:
    """One-sided normal critical value for ``n_eff`` independent trials at family-wise level ``alpha`` (Sidak)."""
    return NORM.inv_cdf((1 - alpha) ** (1 / n_eff))


def bootstrap_max_t(x, mean_block: float, n_boot: int, seed: int, q: float = 0.95) -> float:
    """``q`` quantile of the largest column t-statistic of ``x`` (observations x tests) under zero means: stationary
    bootstrap (Politis-Romano, geometric blocks of mean length ``mean_block``, wrapping around) of the demeaned rows,
    the same rows drawn for every column so the correlation between tests is kept."""
    y = np.asarray(x, float)
    y = y - y.mean(axis=0)
    n = len(y)
    rng = np.random.default_rng(seed)
    out = np.empty(n_boot)
    for b in range(n_boot):
        z = y[stationary_bootstrap_index(n, mean_block, rng)]
        out[b] = np.max(z.mean(axis=0) / z.std(axis=0, ddof=1) * math.sqrt(n))
    return float(np.quantile(out, q))
