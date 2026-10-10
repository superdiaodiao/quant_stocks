"""Tests for scripts/research_index_exclusion.py on synthetic data: base membership, cap weights, exclusion and tilt
weights, target format, the cost-free replica, relative statistics and the verdict rules."""
import numpy as np
import pandas as pd
import pytest

from scripts import research_index_exclusion as ie


def _scores(n=8, s="2020-01-31", sig=None):
    s = pd.Timestamp(s)
    return pd.DataFrame({"s": [s] * n, "security_id": [f"S{i:02d}" for i in range(n)],
                         "ticker": [f"T{i}" for i in range(n)], "dv50_rank": np.arange(1, n + 1, dtype=float),
                         "mcap": np.arange(n, 0, -1) * 1e9,
                         "S-MISP": sig if sig is not None else np.linspace(0.1, 0.9, n)})


def test_trial_count():
    assert ie.N_TRIALS == 24


def test_base_members_top_and_missing_mcap():
    sc = _scores(6)
    sc.loc[1, "mcap"] = np.nan
    b = ie.base_members(sc, "B100", topn=3)
    assert list(b["security_id"]) == ["S00", "S02", "S03"]       # NaN mcap excluded, largest first
    assert len(ie.base_members(sc, "B300")) == 5
    with pytest.raises(ValueError):
        ie.base_members(sc, "B50")


def test_cap_weights_and_exclusion_renormalise():
    g = _scores(5, sig=[0.5, 0.1, np.nan, 0.9, 0.2])
    w = ie.variant_weights(g, "S-MISP", "BASE")
    assert w.sum() == pytest.approx(1.0)
    assert w.iloc[0] == pytest.approx(5 / 15)
    x = ie.variant_weights(g, "S-MISP", "X2")
    kept = set(g.loc[x.index, "security_id"])
    assert kept == {"S00", "S02", "S03"}                           # worst two scored (0.1, 0.2) dropped; NaN kept
    assert x.sum() == pytest.approx(1.0)
    assert x.loc[0] / x.loc[3] == pytest.approx(5 / 2)              # cap ratios preserved


def test_exclusion_ties_by_security_id():
    g = _scores(4, sig=[0.3, 0.3, 0.5, 0.6])
    x = ie.variant_weights(g, "S-MISP", "X1")
    assert "S00" not in set(g.loc[x.index, "security_id"])


def test_tilt_formula_and_missing_neutral():
    g = _scores(4, sig=[0.1, 0.2, 0.3, np.nan])
    w = ie.variant_weights(g, "S-MISP", "T50")
    p = np.array([1.0, 2 / 3, 1 / 3, 0.5])                          # worst = 1, best = 1/n, NaN -> 0.5
    raw = g["mcap"].to_numpy() * (1 - 0.5 * p)
    assert np.allclose(w.to_numpy(), raw / raw.sum())


def test_badness_pct():
    p = ie.badness_pct(pd.Series([3.0, 1.0, 2.0, np.nan]))
    assert p.iloc[1] == 1.0 and p.iloc[0] == pytest.approx(1 / 3) and np.isnan(p.iloc[3])


def test_make_targets_format_and_first():
    a = _scores(4, "2020-01-31")
    b = _scores(4, "2020-02-28")
    t = ie.make_targets(pd.concat([a, b]), "S-MISP", "X1", first="2020-02-01")
    assert list(t) == [pd.Timestamp("2020-02-28")]
    lst = t[pd.Timestamp("2020-02-28")]
    assert len(lst) == 3 and sum(x[1] for x in lst) == pytest.approx(1.0)
    sid, w, rk, mcap, src = lst[0]
    assert isinstance(sid, str) and rk == 2.0 and src == ""


def test_ideal_nav_drift_and_rebalance():
    sess = pd.bdate_range("2020-01-27", periods=10)
    idx = pd.DataFrame({"A": [1, 1, 2, 2, 2, 2, 2, 2, 2, 2], "B": [1.0] * 10}, index=sess, dtype=float)
    tg = {sess[0]: [("A", 0.5, 1, 1, ""), ("B", 0.5, 1, 1, "")],
          sess[4]: [("A", 0.5, 1, 1, ""), ("B", 0.5, 1, 1, "")]}
    nav = ie.ideal_nav(tg, sess, idx, account=100.0)
    assert nav.index[0] == sess[1]                                  # executes the session after the signal
    assert nav.loc[sess[2]] == pytest.approx(150.0)                 # A doubles on half the money
    assert nav.iloc[-1] == pytest.approx(150.0)
    # a name without an index level at execution is dropped and the rest renormalised
    idx2 = idx.copy()
    idx2.loc[:sess[1], "B"] = np.nan
    nav2 = ie.ideal_nav({sess[0]: tg[sess[0]]}, sess, idx2, account=100.0)
    assert nav2.loc[sess[2]] == pytest.approx(200.0)


def test_relative_stats_identical_and_shifted():
    sess = pd.bdate_range("2020-01-01", periods=300)
    rng = np.random.default_rng(0)
    r = rng.normal(0.0005, 0.01, len(sess))
    ref = pd.Series(np.cumprod(1 + r), index=sess)
    st = ie.relative_stats(ref, ref, "2020-01-01", "2021-12-31")
    assert st["te"] == pytest.approx(0.0) and st["cagr_diff"] == pytest.approx(0.0)
    up = pd.Series(np.cumprod(1 + r + 0.0002), index=sess)
    st2 = ie.relative_stats(up, ref, "2020-01-01", "2021-12-31")
    assert st2["cagr_diff"] > 0 and st2["t"] > 5 and st2["beta"] == pytest.approx(1.0, abs=0.05)


def test_window_rebases_at_previous_value():
    sess = pd.bdate_range("2020-12-28", periods=10)
    nav = pd.Series(np.arange(10, dtype=float), index=sess)
    w = ie.window(nav, "2021-01-01", "2021-01-06")
    assert w.index[0] == pd.Timestamp("2020-12-31")


def test_fold_criteria():
    m = {"cagr": 0.20, "oneq_cagr": 0.18, "t_monthly_excess_vs_oneq": 2.1, "dd_shallower_than_oneq_pp": 0.0}
    assert ie.fold_criteria(m) == {"A": True, "B": False, "pass": True}
    m2 = {"cagr": 0.16, "oneq_cagr": 0.18, "t_monthly_excess_vs_oneq": -1, "dd_shallower_than_oneq_pp": 12.0}
    assert ie.fold_criteria(m2)["B"] and not ie.fold_criteria(m2)["A"]


def test_verdict_requires_both_conditions():
    rng = np.random.default_rng(1)
    strong = [pd.Series(0.02 + rng.normal(0, 0.01, 80)), pd.Series(0.02 + rng.normal(0, 0.01, 90))]
    weak = [pd.Series(rng.normal(0, 0.01, 80)), pd.Series(rng.normal(0, 0.01, 90))]
    pf = {"H1": {"pass": True, "ex_base_ann": 0.05}, "H2": {"pass": True, "ex_base_ann": 0.05}}
    v = ie.verdict_row(pf, strong, strong)
    assert v["cond1_vs_oneq"] and v["cond2_exclusion_alpha"] and v["pass"]
    v2 = ie.verdict_row(pf, strong, weak)
    assert v2["cond1_vs_oneq"] and not v2["pass"]
    pf_neg = {"H1": {"pass": True, "ex_base_ann": -0.01}, "H2": {"pass": True, "ex_base_ann": 0.05}}
    assert not ie.verdict_row(pf_neg, strong, strong)["cond2_exclusion_alpha"]
    assert 2.6 < v["bonferroni_t"] < 3.1


def test_prereg_hash_stable():
    text = "x <!-- PREREG-BEGIN --> rules <!-- PREREG-END --> results"
    assert ie.prereg_hash(text) == ie.prereg_hash(text + " more results")
