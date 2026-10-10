"""Tests for scripts/research_ml_cross_section.py: no look-ahead in price features and in the walk-forward split,
rank normalisation, FF12 mapping, the numpy models (elastic net vs closed form, trees and MLP on synthetic data),
the buffered TopN targets and the Bonferroni criterion."""
import math

import numpy as np
import pandas as pd
import pytest

from scripts import research_ml_cross_section as ml


def _sessions(n=600, start="2014-01-02"):
    return pd.bdate_range(start, periods=n)


def _price_frames(n=600, k=6, seed=0):
    rng = np.random.default_rng(seed)
    d = _sessions(n)
    r = pd.DataFrame(rng.normal(0.0005, 0.02, (n, k)), index=d, columns=[f"S{i}" for i in range(k)])
    idx = (1 + r).cumprod()
    close = idx * 50
    vol = pd.DataFrame(rng.integers(1e5, 1e6, (n, k)).astype(float), index=d, columns=r.columns)
    mkt = pd.Series(rng.normal(0.0004, 0.01, n), index=d)
    return idx, close, vol, mkt


# ------------------------------------------------------------------ no look-ahead

def test_price_features_ignore_everything_after_the_signal():
    idx, close, vol, mkt = _price_frames()
    s = idx.index[400]
    sids = list(idx.columns)
    base = ml.price_features(idx, close, vol, mkt, sids, [s] * len(sids))
    # scramble every value strictly after s (prices, volumes, market) -> features at s unchanged
    later = idx.index > s
    idx2, close2, vol2, mkt2 = idx.copy(), close.copy(), vol.copy(), mkt.copy()
    idx2.loc[later] *= 3.7
    close2.loc[later] = 1.0
    vol2.loc[later] = 1e12
    mkt2.loc[later] = 0.5
    after = ml.price_features(idx2, close2, vol2, mkt2, sids, [s] * len(sids))
    pd.testing.assert_frame_equal(base, after)
    # and the features do respond to data on or before s
    idx3 = idx.copy()
    idx3.loc[s] *= 1.1
    changed = ml.price_features(idx3, close, vol, mkt, sids, [s] * len(sids))
    assert not np.allclose(changed["r_1m"], base["r_1m"])


def test_price_feature_definitions():
    idx, close, vol, mkt = _price_frames()
    s = idx.index[300]
    f = ml.price_features(idx, close, vol, mkt, ["S0"], [s]).iloc[0]
    i = idx["S0"]
    assert f["r_1m"] == pytest.approx(i.iloc[300] / i.iloc[279] - 1)
    assert f["mom_12_1"] == pytest.approx(i.iloc[279] / i.iloc[48] - 1)
    assert f["mom_6_1"] == pytest.approx(i.iloc[279] / i.iloc[174] - 1)
    r = i.pct_change()
    assert f["max_ret_1m"] == pytest.approx(r.iloc[280:301].max())
    assert f["vol_1m"] == pytest.approx(r.iloc[280:301].std())
    c = close["S0"]
    assert f["dist_52wh"] == pytest.approx(c.iloc[300] / c.iloc[49:301].max() - 1)
    assert f["sma200_ratio"] == pytest.approx(c.iloc[300] / c.iloc[101:301].mean() - 1)
    rr, mm = r.iloc[49:301], mkt.iloc[49:301]
    beta = np.cov(rr, mm, ddof=0)[0, 1] / mm.var(ddof=0)
    assert f["beta_1y"] == pytest.approx(beta, rel=1e-6)


def test_split_uses_only_labels_realised_before_the_test_year():
    sess = pd.bdate_range("2012-01-02", "2026-08-31")
    month_ends = [g.max() for _, g in pd.Series(sess, index=sess).groupby(sess.to_period("M"))][:-1]
    for Y in (2016, 2020, 2026):
        sp = ml.split_for_year(month_ends, Y)
        nxt = dict(zip(month_ends[:-1], month_ends[1:]))
        assert sp["s0"].year == Y - 1 and sp["s0"].month == 12
        assert all(nxt[s] <= sp["s0"] for s in sp["train_all"])          # labels realised by s0
        assert max(sp["train_all"]) < min(sp["test"])
        assert len(sp["val"]) == 12 and sp["val"][-1] == sp["train_all"][-1]
        assert not set(sp["train"]) & set(sp["val"])
        assert all((s.year == Y) or (s == sp["s0"]) for s in sp["test"])
        assert sp["train_all"][0] == month_ends[0]


def test_walk_forward_refuses_a_label_that_ends_after_s0():
    sess = pd.bdate_range("2012-01-02", "2017-12-29")
    sigs = [g.max() for _, g in pd.Series(sess, index=sess).groupby(sess.to_period("M"))][:-1]
    rows = []
    rng = np.random.default_rng(1)
    nxt = dict(zip(sigs[:-1], sigs[1:]))
    for s in sigs:
        for k in range(30):
            rows.append({"s": s, "s_next": nxt.get(s, pd.NaT), "security_id": f"S{k}", "ticker": f"S{k}",
                         "sic": 3570, "dv50_rank": k + 1, "mcap": 1e9 * (k + 1), "fwd_ret": rng.normal(0, 0.05)})
    p = pd.DataFrame(rows)
    for f in ml.CONT_FEATURES:
        p[f] = rng.normal(size=len(p))
    p.loc[p["s"] == sigs[40], "s_next"] = pd.Timestamp("2030-01-01")      # a corrupted, future-dated label
    with pytest.raises(ml.cs.DateGuardError):
        ml.walk_forward(p, sigs, years=(2016,), log=lambda *_: None)


# ------------------------------------------------------------------ normalisation, industries

def test_rank_normalise_maps_to_half_interval_and_fills_missing_with_zero():
    v = pd.Series([3.0, 1.0, np.nan, 2.0, 10.0, np.nan])
    g = pd.Series(["a", "a", "a", "a", "b", "b"])
    out = ml.rank_normalise(v, g)
    assert list(out) == [0.5, -0.5, 0.0, 0.0, 0.0, 0.0]


def test_ff12():
    assert ml.ff12(7372) == "BusEq" and ml.ff12(2834) == "Hlth" and ml.ff12(5961) == "Shops"
    assert ml.ff12(3674) == "BusEq" and ml.ff12(1311) == "Enrgy" and ml.ff12(4813) == "Telcm"
    assert ml.ff12(6022) == "Money" and ml.ff12(1540) == "Other" and ml.ff12(np.nan) is None
    assert ml.ff12(3711) == "Durbl" and ml.ff12(2011) == "NoDur" and ml.ff12(3841) == "Hlth"


# ------------------------------------------------------------------ models

def test_elastic_net_matches_ridge_closed_form_and_lasso_sparsity():
    rng = np.random.default_rng(3)
    X = rng.normal(size=(500, 6))
    y = X @ np.array([1.0, -0.5, 0, 0, 0.2, 0]) + rng.normal(0, 0.1, 500)
    a = 0.05
    m = ml.ElasticNet(a, 0.0).fit(X, y)
    Xc, yc = X - X.mean(0), y - y.mean()
    n = len(y)
    b = np.linalg.solve(Xc.T @ Xc / n + a * np.eye(6), Xc.T @ yc / n)
    assert np.allclose(m.coef_, b, atol=1e-6)
    lasso = ml.ElasticNet(0.1, 1.0).fit(X, y)
    assert (lasso.coef_[[2, 3, 5]] == 0).all() and lasso.coef_[0] > 0.5


def test_gbrt_learns_a_nonlinear_rank_signal():
    rng = np.random.default_rng(4)
    X = rng.uniform(-0.5, 0.5, (4000, 5))
    y = np.where(X[:, 0] > 0.1, 0.2, -0.1) + 0.3 * X[:, 1] * (X[:, 2] > 0) + rng.normal(0, 0.05, 4000)
    m = ml.GBRT(150, seed=1, min_leaf=20).fit(X[:3000], y[:3000])
    p = m.predict(X[3000:])
    assert np.corrcoef(p, y[3000:])[0, 1] > 0.9
    assert m.gain_[0] == m.gain_.max() and m.gain_[3] < 0.05 * m.gain_.sum()


def test_tree_respects_min_leaf():
    rng = np.random.default_rng(5)
    X = rng.uniform(-0.5, 0.5, (300, 3))
    g = rng.normal(size=300)
    B = ml.to_bins(X)
    t = ml.grow_tree(B, g, 3, 32, 60, 1.0, np.zeros(3))
    leaves = t.predict(B)
    counts = pd.Series(leaves).value_counts()
    assert counts.min() >= 60


def test_mlp_fits_a_simple_function():
    rng = np.random.default_rng(6)
    X = rng.uniform(-0.5, 0.5, (3000, 4))
    y = 0.4 * X[:, 0] - 0.3 * np.abs(X[:, 1])
    ens = ml.MLPEnsemble(4, seed=0, n_seeds=2).train(X[:2500], y[:2500], 25)
    assert np.corrcoef(ens.predict(X[2500:]), y[2500:])[0, 1] > 0.9


def test_monthly_ic():
    p = np.array([1, 2, 3, 4, 5, 6, 7, 8, 9, 10] * 2, float)
    t = np.concatenate([p[:10] * 2, -p[:10]])
    m = ["a"] * 10 + ["b"] * 10
    ic = ml.monthly_ic(p, t, m)
    assert ic["a"] == pytest.approx(1.0) and ic["b"] == pytest.approx(-1.0)


# ------------------------------------------------------------------ portfolios

def _scores(month_scores):
    rows = []
    for s, sc in month_scores.items():
        for sid, v in sc.items():
            rows.append({"s": pd.Timestamp(s), "security_id": sid, "score": v, "dv50_rank": 10.0, "mcap": 1e10})
    return pd.DataFrame(rows)


def test_buffer_keeps_names_still_in_top_2n():
    # month 1 top-2: A, B. Month 2 ranking: C, D, B, A, E -> B and A are within top 4 -> kept; no new names.
    sc = _scores({"2020-01-31": {"A": 5, "B": 4, "C": 3, "D": 2, "E": 1},
                  "2020-02-28": {"C": 5, "D": 4, "B": 3, "A": 2, "E": 1},
                  "2020-03-31": {"C": 5, "D": 4, "E": 3, "F": 2.5, "B": 2, "A": 1}})
    tg = ml.buffered_targets(sc, 2)
    names = {s: sorted(x[0] for x in v) for s, v in tg.items()}
    assert names[pd.Timestamp("2020-01-31")] == ["A", "B"]
    assert names[pd.Timestamp("2020-02-28")] == ["A", "B"]
    # month 3: A, B fall outside the top 4 (C, D, E, F) -> replaced by C, D
    assert names[pd.Timestamp("2020-03-31")] == ["C", "D"]
    assert all(abs(w - 0.5) < 1e-12 for v in tg.values() for _, w, *_ in v)


def test_blend_targets_sum_to_one():
    sc = _scores({"2020-01-31": {k: float(i) for i, k in enumerate("ABCDEFGHIJKL")}})
    tg = ml.buffered_targets(sc, 10, weight=0.05, extra=[("QQQ", 0.5, np.nan, np.nan, "oneq")])
    v = tg[pd.Timestamp("2020-01-31")]
    assert len(v) == 11 and sum(w for _, w, *_ in v) == pytest.approx(1.0)


# ------------------------------------------------------------------ criteria

def test_bonferroni_threshold_for_8_trials():
    t = ml.bonferroni_t(127, 8)
    assert 2.45 < t < 2.56
    assert ml.t_sf(t, 127) == pytest.approx(0.05 / 8, rel=1e-3)


def test_criteria_needs_both_halves_and_bonferroni():
    def per(c1, c2, t, months=128):
        mk = lambda c: {"cagr": c, "oneq_cagr": 0.15, "dd_shallower_than_oneq_pp": 0.0}  # noqa: E731
        return {"P1 2016-2020": mk(c1), "P2 2021-2026-08": mk(c2),
                "Full 2016-2026-08": {"t_monthly_excess_vs_oneq": t, "months": months}}
    assert ml.criteria(per(0.2, 0.2, 2.6))["pass"]
    assert not ml.criteria(per(0.2, 0.2, 2.2))["pass"]        # nominally significant, below the 8-trial hurdle
    assert not ml.criteria(per(0.2, 0.1, 3.5))["pass"]


def test_prereg_block_present():
    blk = ml.prereg_block()
    assert "PREREG-BEGIN" in blk and "试验次数：8" in blk
    assert len(ml.prereg_hash()) == 64


def test_feature_count():
    assert len(ml.FEATURES) == 64 and len(set(ml.FEATURES)) == 64
    assert math.isclose(len(ml.DUMMIES), 11)
