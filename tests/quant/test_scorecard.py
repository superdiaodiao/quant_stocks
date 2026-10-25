"""quant.evaluation.scorecard: hand-computed values on small synthetic series, known identities (Calmar = CAGR /
|max DD|, capture ratios of r = k * b, CRRA with gamma = 1 = log growth = CAGR, Ulcer >= pain index, ...) and exact
agreement with the metrics.core_metrics / relative_metrics conventions on daily data."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from quant.evaluation import criteria, metrics, stats
from quant.evaluation import scorecard as sc

RNG = np.random.default_rng(20261026)


def months(values, start="2020-01"):
    return pd.Series(values, index=pd.period_range(start, periods=len(values), freq="M"), dtype=float)


def daily(n=1500, start="2010-01-04", drift=0.0004, vol=0.012, seed=1):
    rng = np.random.default_rng(seed)
    return pd.Series(rng.normal(drift, vol, n), index=pd.bdate_range(start, periods=n))


def approx(x, rel=1e-12, abs_=1e-15):
    return pytest.approx(x, rel=rel, abs=abs_)


# ======================================================================== inputs

def test_prepare_infers_the_frequency_and_rejects_bad_input():
    assert sc.prepare(daily(30))[1:] == ("D", 252)
    m_end = pd.Series([0.01, 0.02, 0.03], index=pd.date_range("2020-01-31", periods=3, freq="ME"))
    r, f, p = sc.prepare(m_end)
    assert (f, p) == ("M", 12) and isinstance(r.index, pd.PeriodIndex) and str(r.index[0]) == "2020-01"
    assert sc.prepare(months([0.01, 0.02]))[1] == "M"
    assert isinstance(m_end.index, pd.DatetimeIndex)                        # the caller's series is not changed
    with pytest.raises(ValueError, match="NaN"):
        sc.prepare(months([0.01, np.nan, 0.02]))
    with pytest.raises(ValueError, match="neither daily nor monthly"):
        sc.prepare(pd.Series([0.01] * 5, index=pd.date_range("2020-01-03", periods=5, freq="W-FRI")))
    with pytest.raises(ValueError, match="cannot be spread"):
        sc.rf_on(months([0.001] * 3), daily(30), "D")
    with pytest.raises(ValueError, match="misses"):
        sc.rf_on(daily(10), daily(30), "D")


# ======================================================================== 1. return

def test_return_metrics_by_hand():
    r, b = months([0.10, -0.05, 0.20]), months([0.05, 0.0, 0.05])
    out = sc.return_metrics(r, oneq=b, qqq=r)
    assert out["cagr"] == approx(1.254 ** 4 - 1)
    assert out["mean_return_ann"] == approx((1 + 0.25 / 3) ** 12 - 1)
    assert out["terminal_wealth"] == approx(12_540.0)
    assert out["excess_cagr_vs_oneq"] == approx((1.254 ** 4 - 1) - (1.1025 ** 4 - 1))
    assert out["excess_cagr_vs_qqq"] == 0.0


def test_benchmark_alignment_uses_the_common_periods():
    r = daily(300)
    b = r.iloc[100:].copy() * 0.5                                           # starts later
    rc, bc, fc = sc.align(r, "D", b)
    assert fc == "D" and rc.index.equals(b.index)
    out = sc.return_metrics(r, oneq=b)
    assert out["excess_cagr_vs_oneq"] == approx(metrics.cagr_of(r.iloc[100:]) - metrics.cagr_of(b))
    # a daily strategy against a monthly benchmark: both on calendar months
    bm = metrics.monthly(b)
    rc, bc, fc = sc.align(r, "D", bm)
    # the daily side r covers whole months; a monthly benchmark is taken as given (whole months)
    assert fc == "M" and rc.index.equals(bc.index) and len(rc) == len(bm)


def test_mixed_frequencies_drop_the_daily_sides_part_months():
    m = months([0.01] * 14, "2020-01")                                      # Jan 2020 .. Feb 2021, whole months
    d = pd.Series(0.001, index=pd.bdate_range("2020-01-20", "2020-12-15"))  # starts and ends mid-month
    rc, bc, fc = sc.align(d, "D", m)
    assert fc == "M" and [str(x) for x in (rc.index[0], rc.index[-1])] == ["2020-02", "2020-11"]
    assert rc.index.equals(bc.index) and len(rc) == 10
    rc, bc, _ = sc.align(m, "M", d)                                         # the same when the daily side is b
    assert [str(x) for x in (rc.index[0], rc.index[-1])] == ["2020-02", "2020-11"]
    whole = pd.Series(0.001, index=pd.bdate_range("2020-01-02", "2020-12-31"))  # within 4 days of both ends: kept
    assert len(sc.align(whole, "D", m)[0]) == 12
    out = sc.return_metrics(d, oneq=m)
    want = (1.001 ** len(d.loc["2020-02":"2020-11"])) ** (12 / 10) - 1.01 ** 12
    assert out["excess_cagr_vs_oneq"] == approx(want, rel=1e-9)


# ======================================================================== 2. risk-adjusted

def test_sharpe_sortino_by_hand():
    r, rf = months([0.03, 0.01, 0.04, 0.00]), months([0.01] * 4)
    out = sc.risk_adjusted_metrics(r, rf=rf)
    # excess [0.02, 0, 0.03, -0.01]: mean 0.01, sample variance 0.001 / 3, downside deviation sqrt(0.0001 / 4)
    assert out["sharpe"] == approx(0.01 / math.sqrt(0.001 / 3) * math.sqrt(12))
    assert out["sortino"] == approx(0.01 / 0.005 * math.sqrt(12))
    assert out["volatility_ann"] == approx(math.sqrt(0.001 / 3) * math.sqrt(12))
    assert math.isnan(out["calmar_mar"]) and out["omega_0"] == math.inf    # no drawdown, no losing month


def test_omega_ratio_by_hand():
    assert sc.omega_ratio([0.02, -0.01, 0.03, -0.02]) == approx(0.05 / 0.03)
    assert sc.omega_ratio([0.02, -0.01, 0.03, -0.02], threshold=0.01) == approx(0.03 / 0.05)
    assert math.isnan(sc.omega_ratio([0.0, 0.0]))


def test_calmar_identity():
    r = daily(900, seed=3)
    out = sc.risk_adjusted_metrics(r)
    assert out["calmar_mar"] == approx(metrics.cagr_of(r) / abs(sc.drawdown_metrics(r)["max_drawdown"]))


def test_deflated_sharpe_is_criteria_deflated_sharpe_n_on_monthly_excess():
    r, rf = daily(1200, seed=4), pd.Series(0.0001, index=daily(1200).index)
    mex = metrics.monthly(r) - metrics.monthly(rf)
    want = criteria.deflated_sharpe_n(float(mex.mean() / mex.std()), len(mex), float(mex.skew()),
                                      float(mex.kurt() + 3), 25, 0.004)["dsr"]
    assert sc.risk_adjusted_metrics(r, rf=rf, n_trials=25, sr_trials_var=0.004)["deflated_sharpe"] == want
    default = criteria.deflated_sharpe_n(float(mex.mean() / mex.std()), len(mex), float(mex.skew()),
                                         float(mex.kurt() + 3), 25, 1 / len(mex))["dsr"]
    assert sc.risk_adjusted_metrics(r, rf=rf, n_trials=25)["deflated_sharpe"] == default
    assert math.isnan(sc.risk_adjusted_metrics(r, rf=rf)["deflated_sharpe"])
    assert math.isnan(sc.risk_adjusted_metrics(r, rf=rf, n_trials=1)["deflated_sharpe"])


def test_daily_conventions_agree_exactly_with_core_and_relative_metrics():
    idx = pd.bdate_range("2012-01-03", periods=1800)
    b = pd.Series(RNG.normal(0.0004, 0.013, len(idx)), index=idx)
    r = 1.2 * b + pd.Series(RNG.normal(0.0001, 0.005, len(idx)), index=idx)
    rf = pd.Series(RNG.uniform(0, 0.0002, len(idx)), index=idx)
    core, rel = metrics.core_metrics(r, rf), metrics.relative_metrics(r, b, rf, "oneq")
    card = sc.scorecard(r, oneq=b, rf=rf, n_boot=0).set_index("key")["value"]
    for mine, theirs in [("cagr", "cagr"), ("volatility_ann", "vol"), ("max_drawdown", "max_dd"), ("sharpe", "sharpe"),
                         ("sortino", "sortino"), ("calmar_mar", "calmar"), ("worst_year", "worst_year"),
                         ("worst_month", "worst_month")]:
        assert card[mine] == core[theirs], mine
    assert card["longest_underwater_years"] * 252 == approx(core["longest_dd_sessions"])
    for mine, theirs in [("excess_cagr_vs_oneq", "excess_vs_oneq"), ("t_vs_oneq", "t_monthly_excess_vs_oneq"),
                         ("ir_vs_oneq", "ir_vs_oneq"), ("beta_vs_oneq", "beta_vs_oneq"),
                         ("years_beating_oneq_share", "share_years_beating_oneq")]:
        assert card[mine] == rel[theirs], mine


# ======================================================================== 3. drawdown

def test_drawdown_metrics_and_episodes_by_hand():
    r = months([0.10, -0.60, 0.50, 1.00, -0.25])        # value 1, 1.1, 0.44, 0.66, 1.32, 0.99
    ep = sc.drawdown_episodes(r)
    assert ep[["peak", "trough", "recovery", "underwater_periods", "recovered"]].values.tolist() == \
        [[1, 2, 4, 2, True], [4, 5, -1, 1, False]]
    assert ep["depth"].tolist() == [approx(-0.6), approx(-0.25)]
    assert ep["peak_label"].tolist() == ["2020-01", "2020-04"] and ep["recovery_label"].tolist() == ["2020-04", ""]
    assert ep["trough_to_recovery_years"].iloc[0] == approx(2 / 12)
    # peak at the end of 2020-01, back at it at the end of 2020-04: three months from peak to recovery (two of them
    # below the peak); the open episode runs from its peak (2020-04) to the last value (2020-05)
    assert ep["peak_to_recovery_years"].tolist() == [approx(3 / 12), approx(1 / 12)]
    out = sc.drawdown_metrics(r)
    assert out["max_drawdown"] == approx(-0.6)
    assert out["max_dd_duration_years"] == approx(3 / 12) and out["max_dd_recovered"] is True
    assert out["longest_underwater_years"] == approx(2 / 12)
    assert out["ulcer_index"] == approx(math.sqrt((60 ** 2 + 40 ** 2 + 25 ** 2) / 6))
    assert out["pain_index"] == approx((60 + 40 + 25) / 6)
    assert (out["drawdowns_over_20"], out["drawdowns_over_30"], out["drawdowns_over_50"]) == (2, 1, 1)


def test_drawdown_thresholds_are_strict_and_first_period_counts():
    out = sc.drawdown_metrics(months([-0.5, 0.0]))      # exactly -50% from the starting value
    assert out["max_drawdown"] == -0.5 and out["drawdowns_over_50"] == 0 and out["drawdowns_over_30"] == 1
    assert out["max_dd_recovered"] is False and out["max_dd_duration_years"] == approx(2 / 12)


def test_drawdown_identities_on_random_paths():
    for seed in range(5):
        r = daily(700, seed=seed, drift=0.0, vol=0.02)
        v0 = sc.value_path(r)
        ep = sc.drawdown_episodes(r)
        out = sc.drawdown_metrics(r)
        assert ep["depth"].min() == metrics.max_drawdown(v0) == out["max_drawdown"]
        assert ep["underwater_periods"].max() == metrics.longest_drawdown(v0)
        assert out["ulcer_index"] >= out["pain_index"] > 0
        # peak-to-recovery counts the recovery period too, so it can exceed the periods below the peak by one
        assert out["longest_underwater_years"] + 1 / 252 >= out["max_dd_duration_years"] - 1e-12
        rec = ep[ep["recovered"]]
        assert (rec["peak_to_recovery_years"] * 252).round(9).tolist() == (rec["underwater_periods"] + 1).tolist()
        assert ep.loc[~ep["recovered"], "peak_to_recovery_years"].tolist() == \
            ep.loc[~ep["recovered"], "underwater_years"].tolist()


# ======================================================================== 4. tails

def test_tail_metrics_by_hand():
    r = months([-0.10, -0.05, 0.0, 0.05, 0.10])
    out = sc.tail_metrics(r)
    assert out["var_5_monthly"] == approx(-0.09) and out["cvar_5_monthly"] == approx(-0.10)
    assert out["tail_ratio_monthly"] == approx(1.0)
    assert out["worst_month"] == -0.10 and out["worst_year"] == approx(0.9 * 0.95 * 1.05 * 1.1 - 1)
    assert out["skew_monthly"] == approx(0.0, abs_=1e-12)
    assert out["excess_kurtosis_monthly"] == approx(-1.2)               # bias-corrected G2 of -2, -1, 0, 1, 2
    assert math.isnan(out["worst_day"])
    d = daily(60)
    assert sc.tail_metrics(d)["worst_day"] == d.min()


# ======================================================================== 5. consistency

def test_rolling_annualized_by_hand():
    roll = sc.rolling_annualized(months([0.1, -0.1, 0.2]), 2)
    assert roll.tolist() == [approx(0.99 ** 6 - 1), approx(1.08 ** 6 - 1)]
    assert sc.rolling_annualized(months([0.1]), 2).empty


def test_consistency_metrics_on_constant_series():
    r, b = months([0.01] * 132, "2010-01"), months([0.005] * 132, "2010-01")
    out = sc.consistency_metrics(r, oneq=b)
    for k in (3, 5, 10):
        for s in ("min", "p10", "median"):
            assert out[f"rolling_{k}y_{s}"] == approx(1.01 ** 12 - 1)
    assert out["positive_months_share"] == out["positive_years_share"] == 1.0
    assert out["years_beating_oneq_share"] == 1.0 and out["rolling_5y_win_rate_vs_oneq"] == 1.0
    short = sc.consistency_metrics(months([0.01] * 40))
    assert not math.isnan(short["rolling_3y_min"]) and math.isnan(short["rolling_5y_min"])


def test_share_of_positive_months_and_years_by_hand():
    r = months([0.02, -0.01] * 6 + [-0.05] * 12, "2018-01")
    out = sc.consistency_metrics(r)
    assert out["positive_months_share"] == approx(6 / 24) and out["positive_years_share"] == 0.5


# ======================================================================== 6. market relationship

def test_capture_ratios_by_hand_and_identities():
    b, r = months([0.02, -0.01, 0.04, -0.03]), months([0.01, -0.02, 0.03, 0.0])
    up, down = sc.capture_ratios(r, b)
    assert up == approx(0.02 / 0.03) and down == approx(0.01 / 0.02)
    bm = months(RNG.normal(0.01, 0.05, 60), "2015-01")
    for k in (0.5, 1.0, 2.0):
        out = sc.market_metrics(k * bm, oneq=bm)
        assert out["up_capture_vs_oneq"] == approx(k) and out["down_capture_vs_oneq"] == approx(k)
        assert out["beta_vs_oneq"] == approx(k) and out["corr_vs_oneq"] == approx(1.0)


def test_crisis_windows_need_full_coverage():
    m = sc.market_metrics(months([0.10, 0.0, -0.20], "2020-01"), oneq=months([0.05, 0.0, -0.10], "2020-01"))
    assert m["crisis_covid_2020q1"] == approx(1.1 * 0.8 - 1)
    assert m["crisis_covid_2020q1_vs_oneq"] == approx((1.1 * 0.8 - 1) - (1.05 * 0.9 - 1))
    assert math.isnan(m["crisis_gfc_2008"]) and math.isnan(m["crisis_bear_2022"])
    idx = pd.bdate_range("2019-12-02", "2020-06-30")
    d = pd.Series(0.001, index=idx)
    q1 = ((idx >= "2020-01-01") & (idx <= "2020-03-31")).sum()
    assert sc.market_metrics(d)["crisis_covid_2020q1"] == approx(1.001 ** q1 - 1)
    assert math.isnan(sc.market_metrics(d.loc["2020-02-03":])["crisis_covid_2020q1"])   # starts inside the window
    assert math.isnan(sc.market_metrics(d.loc[:"2020-03-20"])["crisis_covid_2020q1"])   # ends inside the window


# ======================================================================== 7. investor utility

def test_crra_identities():
    for r, p in ((daily(800, seed=7), 252), (months(RNG.normal(0.01, 0.06, 90)), 12)):
        assert sc.ce_crra(r, 1, p) == approx(metrics.cagr_of(r, p), rel=1e-11)        # log utility = log growth
        assert sc.ce_crra(r, 0, p) == approx((1 + r.mean()) ** p - 1)                    # risk neutral
        ces = [sc.ce_crra(r, g, p) for g in (0, 1, 2, 3, 5)]
        assert all(a > b for a, b in zip(ces, ces[1:]))                                  # more averse, lower CE
    flat = months([0.01] * 24)
    assert all(sc.ce_crra(flat, g, 12) == approx(1.01 ** 12 - 1) for g in (1, 2, 5))
    assert sc.ce_crra(months([0.1, -1.0]), 2, 12) == -1.0


def test_crra_gamma_2_by_hand():
    # the gamma = 2 certainty equivalent is the harmonic mean of the gross returns: 2 / (1/1.1 + 1/0.9) = 0.99
    assert sc.ce_crra(months([0.1, -0.1]), 2, 12) == approx(0.99 ** 12 - 1)


def test_kelly_by_hand_and_bootstrap_interval():
    assert sc.kelly_fraction([0.02, 0.0, 0.04, -0.02]) == approx(15.0)
    assert math.isnan(sc.kelly_fraction([0.01, 0.01]))
    r, rf = months(RNG.normal(0.012, 0.04, 120), "2010-01"), months([0.002] * 120, "2010-01")
    out = sc.utility_metrics(r, rf=rf, n_boot=400, seed=5)
    ex = r - rf
    assert out["kelly_fraction"] == approx(ex.mean() / ex.var(ddof=1))
    assert out["kelly_ci_lo"] < out["kelly_fraction"] < out["kelly_ci_hi"]
    assert out == sc.utility_metrics(r, rf=rf, n_boot=400, seed=5)                       # seeded
    mean_ann = (1 + r.mean()) ** 12 - 1
    assert out["volatility_drag"] == approx(math.log1p(mean_ann) - math.log1p(metrics.cagr_of(r, 12)))
    assert out["geometric_efficiency"] == approx(metrics.cagr_of(r, 12) / mean_ann)


def test_volatility_drag_is_zero_without_dispersion_and_about_half_the_variance():
    for flat, p in ((months([0.01] * 36), 12), (daily(500, drift=0.0004, vol=0.0), 252)):
        out = sc.utility_metrics(flat, n_boot=0)
        assert out["volatility_drag"] == approx(0.0, abs_=1e-12) and out["geometric_efficiency"] == approx(1.0)
        assert sc.return_metrics(flat)["mean_return_ann"] == approx(sc.return_metrics(flat)["cagr"])
    r = daily(5000, drift=0.0005, vol=0.015, seed=21)
    out, ret = sc.utility_metrics(r, n_boot=0), sc.return_metrics(r)
    vol = r.std() * math.sqrt(252)
    assert out["volatility_drag"] > 0 and out["geometric_efficiency"] < 1
    assert ret["mean_return_ann"] > ret["cagr"]
    assert out["volatility_drag"] == pytest.approx(vol ** 2 / 2, rel=0.02)
    assert ret["mean_return_ann"] == approx(sc.ce_crra(r, 0, 252))                       # risk-neutral CE


# ======================================================================== 8. path experience

def test_contribution_path_by_hand():
    r = months([0.0, -0.5, 0.0])
    w = sc.contribution_path(r, 10_000, 300)
    assert w.tolist() == [10_000, 10_000, 10_300, 5_150, 5_450, 5_450]
    out = sc.path_metrics(r, n_boot=0)
    assert out["contrib_invested_usd"] == 10_600 and out["contrib_terminal_usd"] == 5_450
    assert out["contrib_max_dd_usd"] == 5_150 and out["contrib_max_dd_pct"] == -0.5
    assert out["lump_max_dd_usd"] == 5_000
    assert all(math.isnan(out[f"p_dd{lvl}_{y}y"]) for lvl in (30, 50) for y in (5, 10))


def test_daily_contributions_land_on_month_ends_and_lump_sum_matches_max_drawdown():
    r = daily(130, start="2021-01-04", seed=9)                               # Jan .. early Jul 2021
    w = sc.contribution_path(r, 10_000, 300)
    n_months = r.index.to_period("M").nunique()
    assert len(w) == 1 + len(r) + (n_months - 1)
    lump = sc.contribution_path(r, 10_000, 0.0)
    assert sc.dollar_and_pct_drawdown(lump)[1] == approx(sc.drawdown_metrics(r)["max_drawdown"])


def test_recovery_statistics_by_hand():
    r = months([0.10, -0.60, 0.50, 1.00, -0.25])
    out = sc.path_metrics(r, n_boot=0)
    assert out["dd10_count"] == 2
    assert out["recovery_years_median"] == approx(3 / 12) == out["recovery_years_max"]
    assert out["underwater_now_years"] == approx(1 / 12)


def test_drawdown_probabilities_extremes_and_nesting():
    up = sc.path_metrics(months([0.01] * 60), n_boot=50)
    assert all(up[f"p_dd{lvl}_{y}y"] == 0.0 for lvl in (30, 50) for y in (5, 10))
    down = sc.path_metrics(months([-0.05] * 60), n_boot=50)
    assert all(down[f"p_dd{lvl}_{y}y"] == 1.0 for lvl in (30, 50) for y in (5, 10))
    out = sc.path_metrics(daily(2000, vol=0.02, seed=11), n_boot=200)
    assert out["p_dd30_5y"] <= out["p_dd30_10y"] and out["p_dd50_5y"] <= out["p_dd50_10y"]
    assert out["p_dd50_5y"] <= out["p_dd30_5y"]


def test_stationary_bootstrap_index_and_unchanged_max_t():
    rng = np.random.default_rng(0)
    idx = stats.stationary_bootstrap_index(50, 5.0, rng, size=200)
    assert idx.shape == (200,) and idx.min() >= 0 and idx.max() < 50
    steps = np.diff(idx)
    assert ((steps == 1) | (steps == -49)).mean() > 0.6                   # mostly consecutive (wrapping) rows
    # bootstrap_max_t gives the numbers it gave before the index helper was factored out of it
    x = np.random.default_rng(7).normal(0.001, 0.02, size=(150, 4))
    assert criteria.bootstrap_max_t(x, 6.0, 300, 11) == 2.1365637945472704
    assert criteria.bootstrap_max_t(x[:, :1], 2.5, 200, 3, q=0.9) == 1.0374425101053022


# ======================================================================== 9. practicality

def test_practicality_from_trade_log_and_meta():
    r = months([0.01] * 24, "2020-01")
    trades = pd.DataFrame({"date": pd.date_range("2020-01-31", periods=24, freq="ME"), "traded": 0.5, "cost": 0.001})
    out = sc.practicality_metrics(r, trades=trades, meta={"permissions": ["leveraged_etf"], "positions": 3,
                                                          "min_account_usd": 5_000, "data_dependence": "prices"})
    assert out["trades_per_year"] == 12 and out["turnover_one_way_per_year"] == approx(3.0)
    assert out["cost_drag_per_year"] == approx(0.012)
    assert out["permissions"] == "leveraged_etf" and out["operational_complexity"] == 2.0   # 1 + permission
    assert out["min_account_usd"] == 5_000 and out["data_dependence"] == "prices"
    assert math.isnan(out["margin_call_drop"])
    meta_only = sc.practicality_metrics(r, meta={"trades_per_year": 4, "permissions": []})
    assert meta_only["permissions"] == "none" and meta_only["operational_complexity"] == 1.0
    assert math.isnan(sc.practicality_metrics(r)["operational_complexity"])
    with pytest.raises(ValueError, match="unknown permissions"):
        sc.practicality_metrics(r, meta={"permissions": ["yolo"]})


def test_operational_complexity_rule_and_margin_threshold():
    assert sc.operational_complexity(1) == 1.0
    assert sc.operational_complexity(13) == 2.0 and sc.operational_complexity(53) == 3.0
    assert sc.operational_complexity(12, positions=11) == 2.0
    assert sc.operational_complexity(60, positions=20, permissions=("margin",), timing_sensitive=True) == 5.0  # capped
    assert sc.margin_call_drop(2.0, 0.25) == approx(1 / 3)
    # check: after a 1/3 fall a 2x account holds 4/3 of equity in stock against 1/3 of equity left, i.e. 25%
    assert sc.margin_call_drop(4.0, 0.25) == 0.0 and math.isnan(sc.margin_call_drop(1.0))
    assert math.isnan(sc.margin_call_drop(None))


# ======================================================================== 10. statistical confidence

def test_confidence_metrics():
    b = months(RNG.normal(0.01, 0.05, 96), "2015-01")
    r = b + months(RNG.normal(0.004, 0.01, 96), "2015-01")
    out = sc.confidence_metrics(r, oneq=b, meta={"n_trials": 12, "oos_status": "passed"}, n_boot=300)
    t, _ = metrics.t_and_ir(r - b)
    assert out["t_vs_oneq"] == t and out["p_vs_oneq"] == stats.t_sf(t, 95)
    assert out["excess_cagr_ci_lo"] < out["excess_cagr_ci_hi"]
    assert out["years_of_data"] == 8.0 and out["years_vs_oneq"] == 8.0
    assert (out["n_trials"], out["oos_status"]) == (12.0, "passed")
    sure = sc.confidence_metrics(b + 0.01, oneq=b, n_boot=200)
    assert sure["excess_cagr_boot_share_positive"] == 1.0 and sure["excess_cagr_ci_lo"] > 0
    assert sc.confidence_metrics(r)["oos_status"] == "none"
    with pytest.raises(ValueError, match="oos_status"):
        sc.confidence_metrics(r, meta={"oos_status": "maybe"})


# ======================================================================== the table

def test_scorecard_table_covers_all_ten_families_with_units_and_meanings():
    idx = pd.bdate_range("2004-01-02", periods=252 * 11)
    b = pd.Series(RNG.normal(0.0004, 0.013, len(idx)), index=idx)
    r = 1.5 * b
    card = sc.scorecard(r, oneq=b, qqq=b, rf=pd.Series(0.0001, index=idx), n_boot=100,
                        meta={"n_trials": 30, "permissions": ["margin"], "margin_leverage": 1.5, "positions": 1})
    assert list(card.columns) == sc.COLUMNS and card["key"].is_unique
    assert list(dict.fromkeys(card["family"])) == list(sc.FAMILIES)
    assert (card["unit"] != "").all() and (card["meaning_zh"].str.len() > 4).all()
    assert set(card["freq"]) <= {"D", "M", "Y", "D bootstrap", "M bootstrap", "trades", "meta", "rule"}
    v = card.set_index("key")["value"]
    assert v["ce_crra_1"] == approx(v["cagr"], rel=1e-11)
    # 1.5x DAILY compounds into months non-linearly, so the captures are near (not exactly) 1.5 here; the exact
    # identity on monthly returns is test_capture_ratios_by_hand_and_identities
    assert v["up_capture_vs_oneq"] == pytest.approx(1.5, abs=0.05) and v["corr_vs_oneq"] > 0.99
    assert v["calmar_mar"] == approx(v["cagr"] / abs(v["max_drawdown"]))
    assert v["margin_call_drop"] == approx((1 - 0.25 * 1.5) / (1.5 * 0.75))
    assert len(card) == 93


def test_scorecard_notes_and_monthly_input():
    m = months(RNG.normal(0.008, 0.05, 30), "2019-01")
    card = sc.scorecard(m, n_boot=20).set_index("key")
    assert "按 0 计" in card.loc["sharpe", "note"] and card.loc["excess_cagr_vs_oneq", "note"] == "未提供该基准"
    assert card.loc["worst_day", "note"] == "月度输入没有日数据"
    assert "不足 5 年" in card.loc["p_dd30_5y", "note"] and card.loc["rolling_3y_min", "note"] == "数据不够一个窗口"
    assert card.loc["crisis_gfc_2008", "note"] == "数据不覆盖该时段"
    assert card.loc["cagr", "freq"] == "M" and card.loc["p_dd30_5y", "freq"] == "M bootstrap"
    assert card.loc["oos_status", "value"] == "none"


def test_compare_lines_scorecards_up():
    a = sc.scorecard(months([0.01, -0.02, 0.03] * 8), n_boot=0)
    b = sc.scorecard(months([0.02, -0.01, 0.0] * 8), n_boot=0)
    both = sc.compare({"A": a, "B": b})
    assert list(both.columns) == ["family", "unit", "A", "B"] and list(both.index) == list(a["key"])
    assert both.loc["cagr", "A"] == a.set_index("key").loc["cagr", "value"]
