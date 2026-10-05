"""Tests for scripts/research_voltarget.py: no look-ahead, exposure formulas, monthly decisions, constant comparison."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

import scripts.research_sector_lev as sl
import scripts.research_voltarget as vt
from tests.test_research_sector_lev import synthetic_data, truncated


# ---------------------------------------------------------------- no look-ahead

@pytest.mark.parametrize("name", [s.name for s in vt.SPECS])
def test_targets_unchanged_by_truncation(name):
    d = synthetic_data()
    w_full, f_full, _ = vt.targets(name, d)
    for cut in (d.sessions[700], d.sessions[1001], d.sessions[1250]):
        w_cut, f_cut, _ = vt.targets(name, truncated(d, cut))
        a, b = w_full.loc[:cut].iloc[:-1], w_cut.iloc[:-1]       # the last row's month-end status is unknown
        pd.testing.assert_frame_equal(a, b, check_freq=False)
        pd.testing.assert_series_equal(f_full.loc[:cut].iloc[:-1], f_cut.iloc[:-1], check_freq=False, check_names=False)


def test_future_shock_does_not_change_past_targets():
    d0, d1 = synthetic_data(), synthetic_data()
    d1.rets.iloc[1000:, d1.rets.columns.get_loc("QQQ")] *= 4.0
    for name in ("V1", "V3", "V4"):
        w0, _, _ = vt.targets(name, d0)
        w1, _, _ = vt.targets(name, d1)
        pd.testing.assert_frame_equal(w0.iloc[:999], w1.iloc[:999])


# ---------------------------------------------------------------- exposure formulas

def test_exposure_formulas_and_caps():
    idx = pd.bdate_range("2001-01-01", periods=400)
    r = pd.Series(np.where(np.arange(400) % 2 == 0, 0.01, -0.01), index=idx)   # daily std ~ 0.01 -> vol ~ 15.9%
    p = (1 + r).cumprod()
    v = r.iloc[-20:].std() * math.sqrt(252)
    e1, _ = vt.daily_exposure("V1", r, p)
    e2, _ = vt.daily_exposure("V2", r, p)
    e2b, _ = vt.daily_exposure("V2b", r, p)
    assert e1.iloc[-1] == pytest.approx(1.0)                                  # 20% / 15.9% > 1 -> capped
    assert e2.iloc[-1] == pytest.approx(min(1.5, 0.20 / v))
    assert e2b.iloc[-1] == pytest.approx(min(2.0, 0.20 / v))
    loud = r * 3                                                              # vol ~ 48%
    e1l, _ = vt.daily_exposure("V1", loud, p)
    assert e1l.iloc[-1] == pytest.approx(0.20 / (loud.iloc[-20:].std() * math.sqrt(252)))
    assert e1.iloc[:19].isna().all() and e1.iloc[19:].notna().all()


def test_v3_uses_expanding_vol_only_up_to_t():
    idx = pd.bdate_range("2001-01-01", periods=600)
    rng = np.random.default_rng(1)
    r = pd.Series(rng.normal(0, 0.01, 600), index=idx)
    e, _ = vt.daily_exposure("V3", r, (1 + r).cumprod())
    assert e.iloc[:251].isna().all()
    t = 500
    lr = r.iloc[: t + 1].std() * math.sqrt(252)
    s20 = r.iloc[t - 19: t + 1].std() * math.sqrt(252)
    assert e.iloc[t] == pytest.approx(min(1.5, lr / s20))


def test_weights_from_exposure():
    w = vt.weights_from_exposure(pd.Series([0.4, 1.0, 1.5, 2.0]))
    assert w.iloc[0].tolist() == pytest.approx([0.4, 0.0, 0.6])
    assert w.iloc[1].tolist() == pytest.approx([1.0, 0.0, 0.0])
    assert w.iloc[2].tolist() == pytest.approx([0.5, 0.5, 0.0])
    assert w.iloc[3].tolist() == pytest.approx([0.0, 1.0, 0.0])
    assert (vt.held_exposure(w) == pd.Series([0.4, 1.0, 1.5, 2.0])).all()


# ---------------------------------------------------------------- decision timing

def test_v1_changes_only_at_month_ends():
    d = synthetic_data()
    w, flag, _ = vt.targets("V1", d)
    me = pd.Series(sl.month_end_mask(d.sessions), index=d.sessions)
    changed = w["QQQ"].diff().fillna(0).abs() > 0
    assert me[changed].all()
    assert (flag <= me).all()


def test_v4_switches_with_trend():
    d = synthetic_data()
    d.p_qqq = pd.Series(np.r_[np.linspace(100, 200, 700), np.linspace(200, 80, 700)], index=d.sessions)
    w, flag, e = vt.targets("V4b", d)
    assert e.iloc[1300] == 0.0 and w.iloc[1300]["CASH"] == 1.0
    assert e.iloc[650] > 0
    st = sl.trend_state(d.p_qqq, 200, 0.02)
    first_off = st[(st == 0.0) & (st.shift(1) == 1.0)].index[0]
    assert flag.loc[first_off]                       # a state change is a decision day even mid-month


def test_banded_targets_respect_band():
    d = synthetic_data()
    w, _, e = vt.banded_targets("V2", d)
    ch = e.diff().abs().fillna(0) > 0
    prev = e.shift(1)
    assert ((e[ch] - prev[ch]).abs() > vt.DAILY_BAND * prev[ch] - 1e-12).all()


# ---------------------------------------------------------------- comparisons

def test_constant_exposure_and_alpha():
    d = synthetic_data()
    w, f = vt.constant_targets(1.25, d.sessions)
    assert (w["QQQ"] == 0.75).all() and (w["QLDX"] == 0.25).all()
    q = d.rets["QQQ"].iloc[1:]
    mm = vt.mm_alpha(q, q, d.rf)
    assert mm["beta"] == pytest.approx(1.0) and mm["alpha_ann"] == pytest.approx(0.0, abs=1e-12)
    i1, i2 = q.index[:600], q.index[600:]
    vc = vt.vs_constant(q * 1.0, q, i1, i2)
    assert vc["full"]["cagr_minus_const"] == pytest.approx(0.0)
    assert not vc["timing_adds_value"]
