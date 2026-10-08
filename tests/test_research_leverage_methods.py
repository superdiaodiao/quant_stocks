"""Tests for scripts/research_leverage_methods.py: margin interest, forced liquidation, rebalance policies, legs,
futures, windows, Kelly, the date guard."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

import scripts.research_leverage_methods as lm
from scripts.research_qqq_timing import FutureDataError, TRADING_DAYS

K = len(lm.LEGS)


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
    rl = r_qqq if rl_qqq is None else np.asarray(rl_qqq, float)
    RL = R.copy()
    for k, m in (("QQQ", 1), ("ONEQ", 1), ("BENCH", 1), ("QLDX", 2), ("TQQQX", 3)):
        RL[:, lm.LI[k]] = np.minimum(m * rl, R[:, lm.LI[k]])
    P = np.full((T, K), 50.0)
    return lm.Market(sessions=idx, R=R, RL=RL, P=P, fin=np.zeros((T, K)), er=np.zeros((T, K)),
                     r_tr=r_qqq.copy(), rl_tr=np.minimum(rl, r_qqq), rf=np.full(T, rf), borrow=np.full(T, borrow),
                     month_end=lm.month_end_mask(idx))


def run(mk, method, L, policy, s=0, e=None, **kw):
    e = len(mk.sessions) - 1 if e is None else e
    res = lm.simulate_batch(mk, method, L, policy, [s], [e], **kw)
    return res, lm.path_values(res, 0, mk.sessions)


# ---------------------------------------------------------------- order costs

def test_order_cost_vec_matches_scalar():
    from scripts.research_regime import order_cost
    for v, p, sell, hs in ((1234.0, 50.0, False, 1e-4), (20.0, 300.0, True, 2e-4), (90000.0, 400.0, True, 1e-4)):
        a = lm.order_cost_vec(np.array([v]), np.array([p]), np.array([sell]), np.array([hs]))[0]
        assert a == pytest.approx(order_cost(v, p, sell, hs))
    assert lm.order_cost_vec(np.array([0.5]), np.array([50.0]), np.array([False]), np.array([1e-4]))[0] == 0.0


# ---------------------------------------------------------------- method weights

@pytest.mark.parametrize("L", lm.LEVELS)
@pytest.mark.parametrize("m", ["M", "M1", "Q2", "Q3", "T3", "F"])
def test_weights_give_target_exposure(m, L):
    w, n = lm.method_weights(m, L)
    assert (w * lm.MULT).sum() + n == pytest.approx(L)
    if m in ("Q2", "Q3", "T3"):
        assert w.sum() == pytest.approx(1.0)          # no loan
        assert (w >= -1e-12).all()


# ---------------------------------------------------------------- margin loan

def test_margin_interest_accrues_actual_over_360():
    T = 30
    mk = market(np.zeros(T), borrow=0.05 / 360)       # 5% a year per calendar day, here per session for the test
    res, v = run(mk, "M", 1.5, "never")
    loan0 = 5000.0
    # equity falls by the interest on a growing loan (plus a tiny entry cost)
    expected = loan0 * ((1 + 0.05 / 360) ** (T - 1) - 1)
    assert v.iloc[0] - v.iloc[-1] == pytest.approx(expected, rel=0.02)
    assert res["liq"][0] == 0


def test_borrow_uses_calendar_days():
    s = pd.DatetimeIndex(["2024-01-05", "2024-01-08", "2024-01-09"])          # Fri, Mon, Tue
    days = np.r_[1, np.diff(s.values).astype("timedelta64[D]").astype(int)]
    assert list(days) == [1, 3, 1]


def test_daily_rebalance_holds_leverage():
    rng = np.random.default_rng(0)
    r = rng.normal(0.0005, 0.01, 300)
    mk = market(r)
    res, v = run(mk, "M", 1.5, "daily")
    # without costs or interest, daily-rebalanced 1.5x compounds 1 + 1.5 r
    ideal = np.prod(1 + 1.5 * r[1:])
    assert v.iloc[-1] / v.iloc[0] == pytest.approx(ideal, rel=0.03)


def test_never_rebalance_leverage_drifts_down_in_a_rally():
    mk = market(np.full(200, 0.005))
    res, v = run(mk, "M", 2.0, "never")
    E = v.iloc[-1]
    pos = 20000 * 1.005 ** 199
    assert pos / E < 1.8                              # leverage drifted below the initial 2.0
    assert res["trades"][0] == 0


def test_band_rebalances_only_outside_band():
    r = np.zeros(100)
    r[50] = 0.02                                      # 1.5x -> exposure moves by less than 10% of L
    _, _ = run(market(r), "M", 1.5, "band")
    res, _ = run(market(r), "M", 1.5, "band")
    assert res["trades"][0] == 0
    r[60] = -0.25                                     # 1.5x -> about 1.85x, outside the 10% band
    res2, _ = run(market(r), "M", 1.5, "band")
    assert res2["trades"][0] >= 1


# ---------------------------------------------------------------- forced liquidation

def test_liquidation_triggers_at_intraday_low_and_restores_cushion():
    T = 10
    r = np.zeros(T)
    rl = np.zeros(T)
    rl[5] = -0.40                                     # intraday low -40%, close back to -10%
    r[5] = -0.10
    mk = market(r, rl)
    res, v = run(mk, "M", 2.0, "never")
    assert res["liq"][0] == 1
    assert not res["dead"][0]
    # at the low: position 20000*0.6 = 12000, loan 10000 -> equity 2000 < 25% * 12000 = 3000
    # after selling, requirement = equity / 1.1 at the low
    gross = 12000.0
    eq_l = 2000.0 - lm.START_EQUITY * 0                 # equity at the low (entry cost is tiny)
    f = (eq_l - lm.LIQ_SLIP * gross) / (lm.LIQ_CUSHION * 0.25 * gross - lm.LIQ_SLIP * gross)
    assert 0 < f < 1
    assert v.iloc[-1] < v.iloc[4]


def test_no_liquidation_when_close_only_counts():
    r = np.zeros(10)
    rl = np.zeros(10)
    rl[5] = -0.40
    r[5] = -0.10
    res, _ = run(market(r, rl), "M", 2.0, "never", liquidation=False)
    assert res["liq"][0] == 0


def test_wipeout_is_absorbing():
    r = np.zeros(10)
    r[4] = -0.70
    mk = market(r)
    res, v = run(mk, "M", 2.0, "never")
    assert res["dead"][0]
    assert (v.iloc[4:] == 0).all()
    pm = lm.path_metrics(v, res, 0)
    assert pm["cagr"] == -1.0 and pm["wiped_out"]


def test_unlevered_etf_mix_never_liquidates():
    r = np.zeros(10)
    r[4] = -0.30                                      # QLD -60%, TQQQ -90%, no loan
    for m in ("Q2", "Q3", "T3"):
        res, v = run(market(r), m, 2.0, "never")
        assert res["liq"][0] == 0 and not res["dead"][0] and v.iloc[-1] > 0


# ---------------------------------------------------------------- legs and futures

def test_synthetic_kx_formula():
    r = pd.Series([0.01, -0.02])
    rf = pd.Series([0.0001, 0.0001])
    x = lm.synthetic_kx(r, rf, 3.0, spread=0.007, er=0.0095)
    assert x.iloc[0] == pytest.approx(0.03 - 2 * (0.0001 + 0.007 / 252) - 0.0095 / 252)


def test_futures_return_is_total_return_minus_financing():
    mk = market(np.full(5, 0.01), rf=0.0002)
    f, fl = mk.futures(basis=0.003)
    assert f[0] == pytest.approx(0.01 - 0.0002 - 0.003 / 252)
    assert (fl <= f).all()


def test_futures_unlevered_matches_index_plus_cash_when_cash_earns_rf():
    rng = np.random.default_rng(3)
    r = rng.normal(0.0004, 0.01, 250)
    mk = market(r, rf=0.0)
    res, v = run(mk, "F", 1.0, "daily", basis=0.0)
    ideal = np.prod(1 + r[1:])
    assert v.iloc[-1] / v.iloc[0] == pytest.approx(ideal, rel=0.03)


def test_expiries_are_third_fridays():
    s = pd.bdate_range("2024-01-01", "2024-12-31")
    ex = s[lm.expiry_sessions(s)]
    assert [str(x.date()) for x in ex] == ["2024-03-15", "2024-06-21", "2024-09-20", "2024-12-20"]
    f = lm.roll_flags(s, 5)
    assert f.sum() == 4 and f[lm.expiry_sessions(s)[0] - 5]


# ---------------------------------------------------------------- windows

def test_rolling_windows_are_120_months():
    s = pd.bdate_range("1999-01-01", "2026-09-30")
    st, en = lm.rolling_windows(s)
    assert len(st) == 211
    assert s[st[0]] == pd.Timestamp("1999-03-31") and s[en[-1]] == pd.Timestamp("2026-09-30")
    for a, b in zip(st[:5], en[:5]):
        assert (s[b].year - s[a].year) * 12 + s[b].month - s[a].month == 120


def test_batch_paths_are_independent():
    rng = np.random.default_rng(1)
    mk = market(rng.normal(0.0004, 0.012, 400))
    together = lm.simulate_batch(mk, "Q2", 1.5, "monthly", [10, 100], [300, 399])
    alone = lm.simulate_batch(mk, "Q2", 1.5, "monthly", [100], [399])
    a = lm.path_values(together, 1, mk.sessions)
    b = lm.path_values(alone, 0, mk.sessions)
    pd.testing.assert_series_equal(a, b)


def test_worst_window_and_drawdown():
    v = pd.Series([1.0, 2.0, 1.0, 1.5, 2.5], index=pd.bdate_range("2020-01-01", periods=5))
    assert lm.worst_window(v, 1) == pytest.approx(-0.5)
    dd = lm.drawdown_info(v)
    assert dd["max_dd"] == pytest.approx(-0.5) and dd["dd_recovered"] == str(v.index[4].date())


# ---------------------------------------------------------------- Kelly

def test_kelly_recovers_known_optimum():
    rng = np.random.default_rng(7)
    mu, sig = 0.10 / 252, 0.20 / math.sqrt(252)
    r = rng.normal(mu, sig, 252 * 40)
    mk = market(r)
    k = lm.kelly(mk, 0, len(r) - 1, n_boot=20)
    assert k["formula_Lstar"] == pytest.approx(r[1:].mean() * 252 / (r[1:].var() * 252), rel=1e-6)
    assert abs(k["empirical_Lstar"] - k["formula_Lstar"]) < 0.3


# ---------------------------------------------------------------- date guard

def test_effr_loader_truncates_and_fills_target(tmp_path):
    rows = ["Effective Date,Rate Type,Rate (%)", "10/01/2026,EFFR,3.90", "09/30/2026,EFFR,3.88",
            "07/05/2000,EFFR,6.80", "07/03/2000,EFFR,7.03"]
    (tmp_path / "EFFR_nyfed.csv").write_text("\n".join(rows))
    s = lm.load_effr("2026-09-30", tmp_path)
    assert s.index.max() == pd.Timestamp("2026-09-30")
    assert s.loc["1999-07-01"] == pytest.approx(0.05)
    assert s.loc["2000-06-30"] == pytest.approx(0.065)


def test_assert_dev_dates_rejects_future():
    with pytest.raises(FutureDataError):
        lm.assert_dev_dates(["2026-10-01"], lm.END)
