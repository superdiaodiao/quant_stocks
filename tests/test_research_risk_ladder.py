"""Tests for studies/risk_ladder.py, quant.strategies.risk_ladder, the generalised leverage engine, the HK ETF cost
model, the block bootstrap and the contribution maths (synthetic data only)."""
from __future__ import annotations

from dataclasses import fields

import numpy as np
import pandas as pd
import pytest

import studies.leverage_methods as lev_study
import studies.risk_ladder as study
from quant.backtest.costs import HKD_PER_USD, HK_COMMISSION_MIN_HKD, ibkr_hk_etf_cost
from quant.data.calendar import month_end_mask
from quant.evaluation.block_bootstrap import circular_block_index, path_outcomes
from quant.evaluation.contributions import account_paths, contribution_stats, irr_annual, longest_run
from quant.signals.technical import trend_state
from quant.strategies import leverage_methods as lm
from quant.strategies import risk_ladder as rl

K = len(lm.LEGS)
P = study.P_LEV


def market(r_qqq, rl_qqq=None, rf=0.0, borrow=0.0, start="2001-01-01") -> lm.Market:
    r_qqq = np.asarray(r_qqq, float)
    T = len(r_qqq)
    idx = pd.bdate_range(start, periods=T)
    R = np.zeros((T, K))
    R[:, lm.LI["QQQ"]] = r_qqq
    R[:, lm.LI["ONEQ"]] = r_qqq
    R[:, lm.LI["BENCH"]] = r_qqq
    R[:, lm.LI["QLDX"]] = 2 * r_qqq
    R[:, lm.LI["TQQQX"]] = 3 * r_qqq
    R[:, lm.LI["TBILL"]] = rf
    low = r_qqq if rl_qqq is None else np.asarray(rl_qqq, float)
    RL = R.copy()
    for k, m in (("QQQ", 1), ("ONEQ", 1), ("BENCH", 1), ("QLDX", 2), ("TQQQX", 3)):
        RL[:, lm.LI[k]] = np.minimum(m * low, R[:, lm.LI[k]])
    return lm.Market(sessions=idx, R=R, RL=RL, P=np.full((T, K), 50.0), fin=np.zeros((T, K)), er=np.zeros((T, K)),
                     r_tr=r_qqq.copy(), rl_tr=np.minimum(low, r_qqq), rf=np.full(T, rf), borrow=np.full(T, borrow),
                     month_end=month_end_mask(idx))


def noisy(T=300, seed=1):
    return np.random.default_rng(seed).normal(0.0005, 0.015, T)


def with_btc(mk, r_btc=None, maint=1.0, hs=0.0015):
    T = len(mk.sessions)
    r = np.zeros(T) if r_btc is None else np.asarray(r_btc, float)
    mk2 = rl.with_leg(mk, rl.BTC, r)
    p2 = rl.params_for(P, mk2, {rl.BTC: maint}, {rl.BTC: hs})
    return mk2, p2


# ---------------------------------------------------------------- frozen parameters

def test_engine_params_equal_the_leverage_ledger():
    for f in fields(lm.Params):
        a, b = getattr(P, f.name), getattr(lev_study.P, f.name)
        assert np.array_equal(a, b) if isinstance(a, np.ndarray) else a == b, f.name


def test_portfolio_weights():
    for spec in study.PORTFOLIOS:
        if spec.after:
            mult = {"QQQ": 1.0, "QLDX": 2.0, rl.BTC: 0.0, "TBILL": 0.0}
            exp = sum(v * mult[k] for k, v in spec.after.items())
            assert exp == pytest.approx(spec.L if spec.L != 1.0 else 1.0 - spec.btc), spec.name
            assert spec.after.get(rl.BTC, 0.0) == pytest.approx(spec.btc)
            before = sum(v * mult[k] for k, v in spec.before.items())
            assert before == pytest.approx(spec.L if spec.L != 1.0 else 1.0)
            if spec.name.startswith("Q") or spec.group == "btc":
                assert sum(spec.after.values()) == pytest.approx(1.0)          # no loan
                assert sum(spec.before.values()) == pytest.approx(1.0)
            if spec.margin:
                assert sum(spec.after.values()) > 1.0
    assert set(study.MARGIN_ROWS) == {"M1.2", "M1.3", "M1.5", "M2.0", "M1.2+BTC10", "M1.3+BTC10"}
    assert len(study.PORTFOLIOS) == 21


# ---------------------------------------------------------------- generalised engine

@pytest.mark.parametrize("method,L,policy", [("M", 1.5, "monthly"), ("Q2", 1.3, "monthly"), ("M", 2.0, "never"),
                                             ("M", 1.3, "band"), ("T3", 1.2, "daily")])
def test_targets_reproduce_method_weights(method, L, policy):
    r = noisy(400)
    low = r - np.abs(np.random.default_rng(3).normal(0, 0.02, len(r)))
    mk = market(r, low, rf=0.0001, borrow=0.0002)
    a = lm.simulate_batch(mk, method, L, policy, [0, 50], [399, 399], P)
    w, _ = lm.method_weights(method, L, P.fut_init)
    b = lm.simulate_batch(mk, method, L, policy, [0, 50], [399, 399], P, targets=np.tile(w, (400, 1)))
    assert np.array_equal(a["V"], b["V"], equal_nan=True)
    assert np.array_equal(a["liq"], b["liq"])


def test_an_unused_extra_leg_changes_nothing():
    r = noisy(300)
    mk = market(r, r - 0.03, borrow=0.0002)
    mk2, p2 = with_btc(mk, noisy(300, 9))
    cost = rl.mixed_cost_fn({mk2.legs.index(rl.BTC): rl.hk_etf_cost})
    for method, L in (("M", 2.0), ("Q2", 1.5), ("BENCH_BH", 1.0)):
        a = lm.simulate_batch(mk, method, L, "monthly", [0], [299], P)
        b = lm.simulate_batch(mk2, method, L, "monthly", [0], [299], p2, cost_fn=cost)
        assert np.array_equal(a["V"], b["V"])


def test_extra_flags_force_a_rebalance():
    r = np.r_[0.0, np.full(20, 0.01)]
    mk = market(r)
    flags = np.zeros(len(r), bool)
    flags[10] = True
    a = lm.simulate_batch(mk, "Q2", 1.5, "never", [0], [20], P)
    b = lm.simulate_batch(mk, "Q2", 1.5, "never", [0], [20], P, extra_flags=flags)
    assert a["trades"][0] == 0 and b["trades"][0] == 1


def test_targets_shape_and_nan_are_checked():
    mk = market(noisy(50))
    with pytest.raises(ValueError):
        lm.simulate_batch(mk, "x", 1.0, "monthly", [0], [49], P, targets=np.zeros((50, K + 1)))
    W = np.full((50, K), np.nan)
    with pytest.raises(ValueError):
        lm.simulate_batch(mk, "x", 1.0, "monthly", [0], [49], P, targets=W)


# ---------------------------------------------------------------- BTC leg

def test_fund_returns_fee_and_first_close():
    s = pd.bdate_range("2014-09-25", periods=10)
    spot = pd.Series(0.01, index=s)
    r = rl.fund_returns(spot, s, 0.0252, "2014-09-30")
    assert np.isnan(r[s <= "2014-09-30"]).all()
    assert r[s > "2014-09-30"] == pytest.approx(0.01 - 0.0001)
    spot.iloc[-1] = np.nan
    with pytest.raises(ValueError):
        rl.fund_returns(spot, s, 0.01, "2014-09-30")


def test_holding_a_missing_leg_raises():
    mk = market(noisy(40))
    r = np.full(40, np.nan)
    mk2, p2 = with_btc(mk, r)
    W = rl.constant_targets(mk2, {"QQQ": 0.9, rl.BTC: 0.1})
    with pytest.raises(ValueError):
        lm.simulate_batch(mk2, "x", 1.0, "monthly", [0], [39], p2, targets=W)


def test_switched_targets():
    mk = market(noisy(60), start="2014-09-01")
    mk2, _ = with_btc(mk)
    switch = str(mk2.sessions[21].date())
    W, f = rl.switched_targets(mk2, {"QQQ": 1.0}, {"QQQ": 0.9, rl.BTC: 0.1}, switch)
    q, b = mk2.legs.index("QQQ"), mk2.legs.index(rl.BTC)
    assert (W[:21, q] == 1.0).all() and (W[:21, b] == 0).all()
    assert (W[21:, q] == 0.9).all() and (W[21:, b] == 0.1).all()
    assert f.sum() == 1 and f[21]


def test_btc_moves_do_not_trigger_margin_calls_at_full_maintenance():
    T = 30
    d_full = rl.margin_threshold(1.3, 0.25, 0.1, 1.0)            # 0.590
    d_coll = rl.margin_threshold(1.3, 0.25, 0.1, 0.25)           # 0.667 if BTC were collateral at 25%

    def liq(qqq_low, r_btc_day, btc_maint):
        r, low, rb = np.zeros(T), np.zeros(T), np.zeros(T)
        low[10], rb[10] = qqq_low, r_btc_day
        mk2, p2 = with_btc(market(r, low), rb, maint=btc_maint)
        W = rl.constant_targets(mk2, {"QQQ": 1.3, rl.BTC: 0.1})
        return lm.simulate_batch(mk2, "x", 1.3, "never", [0], [T - 1], p2, targets=W)["liq"][0]

    assert liq(-(d_full - 0.01), -0.95, 1.0) == 0                 # a BTC crash does not move the margin excess
    assert liq(-(d_full - 0.01), 2.0, 1.0) == 0
    assert liq(-(d_full + 0.01), 0.0, 1.0) == 1
    assert liq(-(d_full + 0.01), 0.0, 0.25) == 0                  # it would be safe if BTC were collateral
    assert d_coll > d_full


# ---------------------------------------------------------------- SMA rule

def test_sma_targets_lag_and_flags():
    T = 400
    rng = np.random.default_rng(5)
    s = pd.bdate_range("2000-01-03", periods=T)
    p = pd.Series(np.exp(np.cumsum(np.r_[np.full(250, 0.002), np.full(150, -0.004)] + rng.normal(0, 0.003, T))), index=s)
    mk = market(np.zeros(T), start="2000-01-03")
    on, off = rl.exposure_legs(1.5), rl.exposure_legs(1.0)
    W, flags, st = rl.sma_targets(mk, p, on, off, 200, 0.02)
    assert st.equals(trend_state(p, 200, 0.02))
    q, x = mk.legs.index("QQQ"), mk.legs.index("QLDX")
    for t in range(201, T):
        want = 0.5 if st.iloc[t - 1] == 1.0 else 1.0
        assert W[t, q] == pytest.approx(want)
        assert W[t, x] == pytest.approx(1.5 - 1.0 if st.iloc[t - 1] == 1.0 else 0.0)
    me = month_end_mask(s)
    ch = (st.ne(st.shift(1)) & st.shift(1).notna() & st.notna()).values
    assert np.array_equal(flags[1:], (me | ch)[:-1]) and not flags[0]
    assert ch.any()                                                       # the path does switch off


def test_exposure_legs():
    assert rl.exposure_legs(1.25) == {"QQQ": 0.75, "QLDX": 0.25, "TBILL": 0.0}
    assert rl.exposure_legs(0.5) == {"QQQ": 0.5, "QLDX": 0.0, "TBILL": 0.5}
    assert rl.exposure_legs(2.0) == {"QQQ": 0.0, "QLDX": 1.0, "TBILL": 0.0}
    assert rl.exposure_legs(0.0) == {"QQQ": 0.0, "QLDX": 0.0, "TBILL": 1.0}


# ---------------------------------------------------------------- costs

def test_hk_etf_cost():
    mn = HK_COMMISSION_MIN_HKD / HKD_PER_USD
    small = ibkr_hk_etf_cost(100.0, 0.0015)
    assert small == pytest.approx(mn + 100 * (0.000127 + 0.0015) + 0.5 / HKD_PER_USD)
    big = ibkr_hk_etf_cost(100_000.0, 0.0)
    assert big == pytest.approx(50.0 + 100_000 * 0.000127 + 0.5 / HKD_PER_USD)
    assert ibkr_hk_etf_cost(0.0, 0.001) == 0.0
    assert np.allclose(ibkr_hk_etf_cost(np.array([0.0, 100.0]), 0.0015), [0.0, small])


def test_mixed_cost_fn_only_replaces_the_special_leg():
    v = np.array([[500.0, 0.5, 300.0], [-200.0, 0.0, -2.0]])
    px = np.full_like(v, 50.0)
    hs = np.tile([1e-4, 1e-4, 0.0015], (2, 1))
    base = lm.order_cost_vec(v, px, v < 0, hs, 1.0)
    c = rl.mixed_cost_fn({2: rl.hk_etf_cost})(v, px, v < 0, hs, 1.0)
    assert np.array_equal(c[:, :2], base[:, :2])
    assert c[0, 2] == pytest.approx(ibkr_hk_etf_cost(300.0, 0.0015))
    assert c[1, 2] == pytest.approx(ibkr_hk_etf_cost(2.0, 0.0015))
    assert rl.mixed_cost_fn({2: rl.hk_etf_cost})(np.array([[0, 0, 0.9]]), px[:1], np.array([[0, 0, 0]]) > 0,
                                                 hs[:1], 1.0)[0, 2] == 0.0


# ---------------------------------------------------------------- forced liquidation threshold

@pytest.mark.parametrize("L,b", [(2.0, 0.0), (1.5, 0.0), (1.3, 0.1)])
def test_margin_threshold_matches_the_engine(L, b):
    d = rl.margin_threshold(L, 0.25, b)
    for eps, want in ((-0.005, 0), (0.005, 1)):
        T = 5
        r = np.zeros(T)
        low = np.zeros(T)
        low[2] = -(d + eps)
        mk = market(r, low)
        mk2, p2 = with_btc(mk)
        W = rl.constant_targets(mk2, {"QQQ": L, rl.BTC: b})
        res = lm.simulate_batch(mk2, "x", L, "never", [0], [T - 1], p2, targets=W)
        assert res["liq"][0] == want


def test_margin_threshold_values():
    assert rl.margin_threshold(2.0, 0.25) == pytest.approx(1 / 3)
    assert rl.margin_threshold(1.3, 0.25, 0.1) == pytest.approx(0.575 / 0.975)
    assert np.isnan(rl.margin_threshold(1.0, 0.25))
    assert np.isnan(rl.margin_threshold(0.9, 0.25, 0.1))


def test_month_end_to_low():
    s = pd.bdate_range("2001-01-29", periods=8)                 # Jan 29..Feb 7 (Jan 31 is a month end)
    r = np.array([0.0, 0.0, 0.0, -0.1, 0.0, 0.0, 0.0, 0.0])
    low = np.array([0.0, 0.0, -0.05, -0.15, 0.0, -0.02, 0.0, 0.0])
    out = rl.month_end_to_low(r, low, s)
    assert out["2001-01"] == pytest.approx(-0.05)
    assert "2001-02" not in out                                  # month not finished


# ---------------------------------------------------------------- windows, monthly returns, drag

def test_monthly_returns_drop_the_partial_entry_month():
    s = pd.bdate_range("2014-09-15", "2014-12-31")
    v = pd.Series(np.linspace(100, 130, len(s)), index=s)
    r = rl.monthly_returns(v)
    assert list(r.index) == ["2014-10", "2014-11", "2014-12"]
    sep = v.loc[:"2014-09-30"].iloc[-1]
    assert r["2014-10"] == pytest.approx(v.loc[:"2014-10-31"].iloc[-1] / sep - 1)


def test_drag_row_constant_returns_have_no_gap():
    d = rl.drag_row(pd.Series(np.full(24, 0.01)))
    assert d["gap"] == pytest.approx(12 * 0.01 - (1.01 ** 12 - 1))
    assert d["vol_annual"] == pytest.approx(0.0)


def test_window_outcomes():
    mk = market(np.r_[0.0, np.full(300, 0.001)])
    res = lm.simulate_batch(mk, "QQQ_BH", 1.0, "never", [0, 10], [300, 300], P)
    w = rl.window_outcomes(res, mk.sessions)
    assert len(w) == 2 and (w["max_dd"] <= 0).all() and (w["cagr"] > 0).all() and (w["liquidations"] == 0).all()


# ---------------------------------------------------------------- bootstrap

def test_circular_block_index():
    rng = np.random.default_rng(0)
    idx = circular_block_index(10, 25, 4, 500, rng)
    assert idx.shape == (500, 25) and idx.min() >= 0 and idx.max() <= 9
    d = (np.diff(idx, axis=1) % 10)
    inside = np.ones(24, bool)
    inside[3::4] = False                                         # positions 3->4, 7->8 ... are block joins
    assert (d[:, inside] == 1).all()                             # consecutive (wrapping) inside a block
    assert ((idx[:, 0] == 9) & (idx[:, 1] == 0)).any()           # wrap-around happens
    again = circular_block_index(10, 25, 4, 500, np.random.default_rng(0))
    assert np.array_equal(idx, again)


def test_kelly_block_index_circular_samples_the_ends():
    n, block = 50, 10
    rng = np.random.default_rng(0)
    old = np.concatenate([lm.block_index(rng, n, block) for _ in range(4000)])
    rng = np.random.default_rng(0)
    new = np.concatenate([lm.block_index(rng, n, block, circular=True) for _ in range(4000)])
    f_old, f_new = np.bincount(old, minlength=n) / len(old), np.bincount(new, minlength=n) / len(new)
    assert f_old[-1] == 0 and f_old[0] < 0.5 / n                 # the original draw never reaches the last session
    assert np.allclose(f_new, 1 / n, rtol=0.1)                   # circular: every session equally likely
    one = lm.block_index(np.random.default_rng(1), n, block, circular=True)
    assert len(one) == n and (np.diff(one.reshape(-1, block), axis=1) % n == 1).all()


def test_kelly_default_draw_is_unchanged_and_circular_is_flagged():
    r = noisy(600, seed=3)
    mk = market(r, rf=0.0001, borrow=0.0002)
    a = lm.kelly(mk, 0, len(r) - 1, 7, n_boot=30, block=50)
    rng = np.random.default_rng(7)                               # the leverage ledger's original loop, inlined
    nb = int(np.ceil((len(r) - 1) / 50))
    grid = np.arange(0.0, 4.0001, 0.05)
    rr, ff, bb = r[1:], np.full(len(r) - 1, 0.0001), np.full(len(r) - 1, 0.0002)
    boots = []
    for _ in range(30):
        st = rng.integers(0, len(rr) - 50, nb)
        idx = (st[:, None] + np.arange(50)).ravel()[:len(rr)]
        g = [np.log(1 + L * rr[idx] + np.where(L >= 1, -(L - 1) * bb[idx], (1 - L) * ff[idx])).mean() for L in grid]
        boots.append(grid[int(np.argmax(g))])
    assert a["bootstrap"]["median"] == pytest.approx(float(np.median(boots)))
    assert "circular" not in a["bootstrap"]
    c = lm.kelly(mk, 0, len(r) - 1, 7, n_boot=30, block=50, circular=True)
    assert c["bootstrap"]["circular"] is True and c["empirical_Lstar"] == a["empirical_Lstar"]
    assert study.KELLY_CIRCULAR is True


def test_path_outcomes():
    r = np.r_[np.full(12, 0.01), -0.5, np.full(11, 0.0)]
    idx = np.arange(24)[None, :]
    o = path_outcomes(r, idx)
    w = 1.01 ** 12 * 0.5
    assert o["multiple"][0] == pytest.approx(w)
    assert o["cagr"][0] == pytest.approx(w ** 0.5 - 1)
    assert o["max_dd"][0] == pytest.approx(-0.5)
    assert o["worst_12m"][0] == pytest.approx(-0.5)
    with pytest.raises(ValueError):
        path_outcomes(np.array([np.nan, 0.0]), np.array([[0, 1]]))


# ---------------------------------------------------------------- contributions

def test_account_paths_and_irr():
    r = np.zeros(24)
    v, c = account_paths(r, 10_000, 300)
    assert np.allclose(v, c) and c[-1] == 10_000 + 24 * 300
    assert irr_annual(np.array([v[-1]]), 10_000, 300, 24)[0] == pytest.approx(0.0, abs=1e-9)
    g = 0.01
    v, c = account_paths(np.full(36, g), 10_000, 300)
    assert v[-1] == pytest.approx(10_000 * 1.01 ** 36 + 300 * ((1.01 ** 36 - 1) / 0.01))
    assert irr_annual(np.array([v[-1]]), 10_000, 300, 36)[0] == pytest.approx(1.01 ** 12 - 1, abs=1e-9)


def test_contribution_stats_soften_the_drawdown_vs_money_put_in():
    r = np.r_[np.full(12, -0.05), np.full(48, 0.02)]
    s = contribution_stats(r, 10_000, 300)
    assert s["twr_max_dd"] == pytest.approx(0.95 ** 12 - 1)
    assert s["account_max_dd"] > s["twr_max_dd"]                 # contributions refill the account
    assert s["worst_vs_contributed"] < 0 and s["months_below_contributed"] > 0
    assert longest_run(np.array([1, 1, 0, 1, 1, 1, 0], bool)) == 3


# ---------------------------------------------------------------- dates and outputs

def test_study_dates_and_periods():
    assert study.END == "2026-09-30"
    assert study.PERIODS["2014_2026"] == study.BTC_FIRST_CLOSE
    assert study.BOOT_BLOCK == 24 and study.BOOT_DRAWS == 10_000
    assert study.BTC_FEE == 0.015 and study.BTC_MAINT == 1.0


def test_growth_curve_on_constant_returns():
    T = 300
    mk = market(np.full(T, 0.001), rf=0.0001, borrow=0.0002)
    g = study.growth_curve(mk, 0, T - 1, grid=(1.0, 2.0))
    assert g["curve"]["1.00"]["log_growth"] == pytest.approx(np.log(1.001) * 252)
    assert g["curve"]["2.00"]["log_growth"] == pytest.approx(np.log(1 + 0.002 - 0.0002) * 252)
    assert g["vol"] == pytest.approx(0.0, abs=1e-12)
