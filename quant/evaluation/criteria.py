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
