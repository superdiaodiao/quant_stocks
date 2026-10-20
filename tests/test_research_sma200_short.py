"""Tests for the 200-day-SMA short study (synthetic data only): the state machine, next-close execution, the short's
P&L with dividends and the borrow fee, IBKR interest and margin rules, forced liquidation, the inverse fund's daily
reset, the index-level engine, and the episode (whipsaw) counts."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant.backtest import exposure
from quant.backtest.costs import START_EQUITY
from quant.backtest.long_short import (MarginRules, Market, daily_interest, short_maint, simulate_index_ls,
                                       simulate_long_short)
from quant.data import inverse_etfs as ie
from quant.data.sma200_short import daily_tbill_unquantised, dividend_per_session, tbill_per_session
from quant.evaluation.episodes import episode_summary, share_in_state, state_episodes
from quant.strategies import international as intl_st
from quant.strategies import sma200_short as st
from quant.strategies import short_overlay as so

NO_COST = MarginRules(borrow_fee=0.0)


def market(r, price=None, div=None, rc=0.0, effr=0.0, r_inv=None, inv_scale=1.0, inv_lev=-1.0, hs=1e-4):
    idx = r.index
    price = price if price is not None else 100 * (1 + r).cumprod()
    return Market(r_long=r, r_price=price.pct_change().fillna(0.0), div=div if div is not None else pd.Series(0.0, index=idx),
                  price=price, r_cash=pd.Series(rc, index=idx), effr=pd.Series(effr, index=idx), r_inv=r_inv,
                  inv_price=pd.Series(50.0, index=idx), inv_scale=inv_scale, inv_leverage=inv_lev,
                  hs_long=hs, hs_cash=hs, hs_inv=hs)


def rand_returns(n=900, seed=0, start="2001-01-02"):
    rng = np.random.default_rng(seed)
    x = rng.normal(0.0004, 0.012, n)
    if n >= 420:
        x[300:420] = rng.normal(-0.004, 0.02, 120)
    return pd.Series(x, index=pd.bdate_range(start, periods=n))


# ---------------------------------------------------------------- signal

def test_state_machine_band_and_hold():
    idx = pd.bdate_range("2000-01-03", periods=260)
    p = pd.Series(100.0, index=idx)
    p.iloc[220] = 101.5       # above the SMA but inside the 2% band: S2 keeps the previous state, S0 turns on
    p.iloc[221:] = 103.0      # above 1.02 x SMA: on
    p.iloc[240:] = 96.0       # below 0.98 x SMA: off
    s2, s0 = st.state(p, "S2"), st.state(p, "S0")
    assert s2.iloc[:199].isna().all() and s2.iloc[199] == 0.0      # first state: P > SMA? (equal -> off)
    assert s2.iloc[220] == 0.0 and s0.iloc[220] == 1.0
    assert s2.iloc[221] == 1.0 and s2.iloc[239] == 1.0 and s2.iloc[240] == 0.0


def test_state_uses_no_future_data():
    p = 100 * (1 + rand_returns()).cumprod()
    base = st.state(p, "S2")
    q = p.copy()
    q.iloc[600:] *= 1.5
    assert base.iloc[:600].equals(st.state(q, "S2").iloc[:600])


def test_targets_per_position():
    s = pd.Series([np.nan, 1.0, 0.0])
    assert st.target(s, "LS1").tolist()[1:] == [1.0, -1.0]
    assert st.target(s, "FS1").tolist()[1:] == [0.0, -1.0]
    assert st.target(s, "LS0.5").tolist()[1:] == [1.0, -0.5]
    assert st.target(s, "T01").tolist()[1:] == [1.0, 0.0]
    assert len(st.qqq_trials()) == 12 and len(st.index_trials()) == 18


# ---------------------------------------------------------------- engine: execution and equivalence

def test_long_flat_reproduces_the_frozen_exposure_engine():
    r = rand_returns()
    rf = pd.Series(0.0001, index=r.index)
    price = 100 * (1 + r).cumprod()
    tgt = st.target(st.state(price, "S2"), "T01")
    mk = market(r, price=price, rc=0.0001)
    a = simulate_long_short(tgt, mk, str(r.index[210].date()), str(r.index[-1].date()))
    b = exposure.simulate(tgt, r, r * 2, rf, price, str(r.index[210].date()), str(r.index[-1].date()))
    assert np.allclose(a["value"].values, b["value"].values, rtol=0, atol=1e-9)
    assert a["switches"] == b["rebalances"] and a["orders"] == b["orders"]


def test_next_close_execution():
    idx = pd.bdate_range("2020-01-01", periods=6)
    r = pd.Series([0.0, 0.0, 0.10, 0.10, 0.10, 0.10], index=idx)
    tgt = pd.Series([0.0, 0.0, -1.0, -1.0, -1.0, -1.0], index=idx)      # decided short at the close of day 2
    out = simulate_long_short(tgt, market(r, hs=0.0), str(idx[0].date()), str(idx[-1].date()),
                              rules=NO_COST)
    held = out["exposure_held"]
    assert held.iloc[:3].abs().max() < 1e-12      # traded at the close of day 3 -> first short return on day 4
    assert held.iloc[3] < -0.99


def test_short_pnl_dividend_and_borrow_fee():
    idx = pd.DatetimeIndex(["2020-01-02", "2020-01-03", "2020-01-06"])     # Friday -> Monday: 3 calendar days
    r_price = pd.Series([0.0, 0.0, -0.10], index=idx)
    price = 100 * (1 + r_price).cumprod()
    div = pd.Series([0.0, 0.0, 1.0], index=idx)                            # $1 a share on the ex-date
    mk = Market(r_long=r_price + div / price.shift(1).fillna(100), r_price=r_price, div=div, price=price,
                r_cash=pd.Series(0.0, index=idx), effr=pd.Series(0.0, index=idx), hs_long=0.0, hs_cash=0.0)
    tgt = pd.Series(-1.0, index=idx)
    rules = MarginRules(borrow_fee=0.036, collateral_in_bills=False)
    out = simulate_long_short(tgt, mk, "2020-01-02", "2020-01-06", rules=rules, lag=0)
    s = out["value"].iloc[0]                     # short of 1 x equity opened at the first close (after costs)
    e1 = out["value"].iloc[1]
    assert e1 == pytest.approx(s - s * 0.036 * 1 / 360, rel=1e-12)     # one calendar day of borrow fee
    # then: gains 10% of the short, pays $1 per share on s / 100 shares, borrow 3.6% x 3 / 360
    expected = e1 + 0.10 * s - s / 100 * 1.0 - s * 0.036 * 3 / 360
    assert out["value"].iloc[2] == pytest.approx(expected, rel=1e-12)
    assert out["borrow"] == pytest.approx(s * 0.036 * 4 / 360, rel=1e-12)
    assert out["dividends"] == pytest.approx(s / 100, rel=1e-12)


def test_collateral_sized_by_reg_t_initial_margin():
    idx = pd.bdate_range("2020-01-01", periods=3)
    r = pd.Series(0.0, index=idx)
    for e, wb in [(-1.0, 0.90), (-0.5, 1.0)]:
        out = simulate_long_short(pd.Series(e, index=idx), market(r, hs=0.0, rc=0.001), str(idx[0].date()),
                                  str(idx[-1].date()), rules=NO_COST, lag=0)
        # the T-bill ETF part earns 0.1% a day; nothing else moves
        g = out["value"].iloc[1] / out["value"].iloc[0] - 1
        assert g == pytest.approx(0.001 * wb, rel=1e-6)


def test_margin_loan_when_the_short_outgrows_cash():
    idx = pd.bdate_range("2020-01-01", periods=4)
    r = pd.Series([0.0, 0.0, 0.20, 0.0], index=idx)           # +20%: the short (1.2 x) exceeds cash (1.1 x)
    out = simulate_long_short(pd.Series(-1.0, index=idx), market(r, hs=0.0, effr=0.05), str(idx[0].date()),
                              str(idx[-1].date()), rules=NO_COST, lag=0)
    assert out["margin_interest"] > 0 and out["liquidations"] == 0


def test_forced_liquidation_blocks_until_the_signal_turns():
    idx = pd.bdate_range("2020-01-01", periods=8)
    r = pd.Series([0.0, 0.0, 0.20, 0.20, 0.0, 0.0, 0.0, 0.0], index=idx)   # +44%: equity 0.56 < 0.225 + 0.432
    tgt = pd.Series([-1.0] * 6 + [1.0, 1.0], index=idx)
    out = simulate_long_short(tgt, market(r, hs=0.0), str(idx[0].date()), str(idx[-1].date()), rules=NO_COST, lag=0)
    assert out["liquidations"] == 1 and out["liquidation_dates"] == [str(idx[3].date())]
    assert out["state"].iloc[4] == 0.0 and out["state"].iloc[5] == 0.0      # no re-short in the same off spell
    assert out["state"].iloc[6] == 1.0


def test_inverse_fund_leg_and_scale():
    r = rand_returns(300)
    inv = -r
    mk = market(r, r_inv=inv, hs=0.0)
    out = simulate_long_short(pd.Series(-1.0, index=r.index), mk, str(r.index[0].date()), str(r.index[-1].date()),
                              impl="inverse", lag=0)
    assert np.allclose(out["ret"].values, inv.iloc[1:].values, atol=1e-12)
    mk3 = market(r, r_inv=-3 * r, inv_scale=1 / 3, inv_lev=-3.0, hs=0.0)
    out3 = simulate_long_short(pd.Series(-1.0, index=r.index), mk3, str(r.index[0].date()), str(r.index[-1].date()),
                               impl="inverse", lag=0)
    assert out3["ret"].iloc[0] == pytest.approx(-r.iloc[1], abs=1e-12)      # -1x on the first day, then drifts
    assert out3["exposure_held"].iloc[0] == pytest.approx(-1.0)


def test_ibkr_rules_match_the_short_overlay_copy():
    for p, sh in [(30.0, 10.0), (10.0, 10.0), (3.0, 10.0), (1.0, 10.0)]:
        assert short_maint(p, sh, p * sh) == so.short_maint(p, sh, p * sh)
    for cash, smv, nav, effr, days in [(20_000, 10_000, 10_000, 0.05, 3), (5_000, 12_000, 8_000, 0.02, 1),
                                       (300_000, 150_000, 150_000, 0.04, 1), (60_000, 0, 60_000, 0.03, 2)]:
        assert daily_interest(cash, smv, nav, effr, days) == so.daily_interest(cash, smv, nav, effr, days)


# ---------------------------------------------------------------- inverse fund model

def test_synthetic_inverse_daily_reset_and_decay():
    idx = pd.bdate_range("2020-01-01", periods=4)
    r = pd.Series([0.0, 0.10, -0.0909090909, 0.0], index=idx)   # up 10% then back to the start
    rf = pd.Series(0.0, index=idx)
    syn = ie.synthetic_inverse(r, rf, -1.0, 0.0)
    assert np.prod(1 + r) == pytest.approx(1.0, abs=1e-9)
    assert np.prod(1 + syn) < 1.0                                # daily reset: the inverse fund loses on the round trip
    assert syn.iloc[1] == pytest.approx(-0.10)
    rf2 = pd.Series(0.0002, index=idx)
    assert ie.synthetic_inverse(r, rf2, -1.0, 0.0252).iloc[0] == pytest.approx(2 * 0.0002 - 0.0001)
    assert ie.synthetic_inverse(r, rf2, -3.0, 0.0).iloc[1] == pytest.approx(-0.30 + 4 * 0.0002)


def test_calibration_recovers_a_known_spread():
    r = rand_returns(1500, seed=3)
    rf = pd.Series(0.00008, index=r.index)
    real = ie.synthetic_inverse(r, rf, -1.0, 0.0095, 0.004)
    c = st.calibrate(real, r, rf, -1.0, 0.0095, str(r.index[0].date()), str(r.index[-1].date()))
    assert c["spread_calibrated"] == pytest.approx(0.004, abs=1e-6)
    assert c["calibrated"]["tracking_error_daily_ann"] < 1e-9


def test_inverse_leg_splices_real_after_its_start():
    r = rand_returns(100)
    rf = pd.Series(0.0, index=r.index)
    real = pd.Series(np.nan, index=r.index)
    real.iloc[50:] = 0.01
    leg = st.inverse_leg(r, rf, real, -1.0, 0.0, 0.0)
    assert np.allclose(leg.iloc[:50], -r.iloc[:50]) and (leg.iloc[50:] == 0.01).all()


# ---------------------------------------------------------------- index level

def test_index_engine_reproduces_the_international_engine_for_long_flat():
    p = 100 * (1 + rand_returns(800, seed=5)).cumprod()
    e = st.target(st.state(p, "S2"), "T01")
    r = p.pct_change()
    s_i = intl_st.first_entry([e])
    a = simulate_index_ls(e, r, s_i, 0.001)
    b = intl_st.simulate_exposure(e, r, 0.0, s_i, 0.001)
    assert np.allclose(a["value"].values, b["value"].values, atol=1e-12) and a["switches"] == b["switches"]


def test_index_short_earns_minus_r_plus_credit_minus_borrow():
    idx = pd.bdate_range("2000-01-03", periods=5)
    r = pd.Series([0.0, 0.01, -0.02, 0.03, 0.0], index=idx)
    cr = pd.Series(0.0002, index=idx)
    bw = pd.Series(0.00001, index=idx)
    out = simulate_index_ls(pd.Series(-1.0, index=idx), r, 0, 0.0, cr, bw, lag=0)
    # first day exactly: -r + credit - borrow on a short of 1.0; later days on the drifted short notional
    assert out["ret"].iloc[0] == pytest.approx(-0.01 + 0.0002 - 0.00001)
    a, c = -1.0, 2.0
    for t in range(1, 5):
        c += -a * (cr.iloc[t] - bw.iloc[t])
        a *= 1 + r.iloc[t]
    assert out["value"].iloc[-1] == pytest.approx(a + c)
    assert (out["held"] < 0).all()


def test_tbill_per_session_calendar_days():
    idx = pd.DatetimeIndex(["2020-01-02", "2020-01-03", "2020-01-06"])
    y = pd.Series([0.0365, 0.0365, 0.0365], index=idx)
    t = tbill_per_session(idx, y)
    assert t.iloc[2] == pytest.approx(1.0365 ** (3 / 365) - 1)


# ---------------------------------------------------------------- post-hoc review helpers (report only)

def test_posthoc_rate_and_dividend_helpers():
    idx = pd.DatetimeIndex(["2020-01-02", "2020-01-03", "2020-01-06", "2020-01-07"])
    y = pd.Series([0.0252, 0.0504], index=pd.DatetimeIndex(["2020-01-01", "2020-01-06"]))
    rf = daily_tbill_unquantised(idx, y)                      # the previous session's yield / 252
    assert rf.tolist() == pytest.approx([0.0, 0.0001, 0.0001, 0.0002])
    dv = dividend_per_session(idx, "2020-01-06", 0.0365, 0.073)
    assert dv.tolist() == pytest.approx([0.073 / 365, 0.073 / 365, 0.0365 * 3 / 365, 0.0365 / 365])


def test_index_short_with_proceeds_unpaid_earns_the_tbill_on_own_equity_only():
    idx = pd.bdate_range("2000-01-03", periods=3)
    r = pd.Series(0.0, index=idx)
    tb = pd.Series(0.001, index=idx)
    out = simulate_index_ls(pd.Series(-1.0, index=idx), r, 0, 0.0, credit=-tb, cash=tb, lag=0)
    # own equity 1.0 earns the T-bill, the short's proceeds (another 1.0) earn nothing (second-order term aside)
    assert out["ret"].iloc[0] == pytest.approx(0.001, abs=2e-6)
    full = simulate_index_ls(pd.Series(-1.0, index=idx), r, 0, 0.0, credit=None, cash=tb, lag=0)
    assert full["ret"].iloc[0] == pytest.approx(0.002, abs=1e-12)


# ---------------------------------------------------------------- episodes

def test_episodes_and_whipsaw_counts():
    idx = pd.bdate_range("2020-01-01", periods=7)
    value = pd.Series([100, 101, 99, 98, 97, 99, 100.0], index=idx)
    state = pd.Series([1, 1, -1, -1, 1, -1, -1.0], index=idx)
    ep = state_episodes(value, state)
    assert ep["state"].tolist() == [1, -1, 1, -1]
    assert ep["ret"].round(6).tolist() == [round(99 / 100 - 1, 6), round(97 / 99 - 1, 6), round(99 / 97 - 1, 6),
                                           round(100 / 99 - 1, 6)]
    s = episode_summary(ep, "short")
    assert s["episodes"] == 2 and s["losing"] == 1
    assert share_in_state(state.shift(1).iloc[1:], "short") == pytest.approx(3 / 6)


# ---------------------------------------------------------------- the study end to end on synthetic inputs

def _synthetic_inputs():
    from quant.data.sma200_short import QQQData
    rng = np.random.default_rng(11)
    s = pd.bdate_range("1998-01-02", "2026-09-30")
    r1 = pd.Series(rng.normal(0.0005, 0.015, len(s)), index=s)
    for a, b in (("2000-03-01", "2002-10-01"), ("2008-01-01", "2009-03-01"), ("2022-01-01", "2022-10-01")):
        m = (s >= a) & (s <= b)
        r1[m] = rng.normal(-0.002, 0.025, m.sum())
    div = pd.Series(0.0, index=s)
    div[(s.month % 3 == 0) & (s.day >= 20) & (s.day <= 22)] = 0.1
    price = 100 * (1 + r1).cumprod()
    r_price = price.pct_change().fillna(0.0)
    r1 = r_price + div / price.shift(1).fillna(100.0)
    rf = pd.Series(0.0001, index=s)
    funds = {}
    for sym, first, lev in (("PSQ", "2006-06-21", -1.0), ("SQQQ", "2010-02-11", -3.0)):
        x = ie.synthetic_inverse(r1, rf, lev, 0.0095, 0.002)[s >= first]
        lvl = 30 * (1 + x).cumprod()
        funds[sym] = pd.DataFrame({"close": lvl, "adjclose": lvl, "dividend": 0.0})
    d = QQQData(sessions=s, r1=r1, level=(1 + r1).cumprod(), r_price=r_price, div=div, price=price, rf=rf,
                effr=pd.Series(0.03, index=s), oneq_star=r1 * 0.95, oneq_price=pd.Series(50.0, index=s),
                qld=2 * r1, funds=funds, guard={})
    idx = {}
    for k, (sym, first) in enumerate((("^NDX", "1985-10-01"), ("^IXIC", "1971-02-05"), ("^GSPC", "1950-01-03"))):
        ix = pd.bdate_range(first, "2026-09-30")
        idx[sym] = pd.Series(100 * np.cumprod(1 + rng.normal(0.0003, 0.011, len(ix))), index=ix)
    dtb3 = pd.Series(0.04, index=pd.bdate_range("1954-01-04", "2026-09-30"))
    return d, idx, dtb3


def test_study_runs_end_to_end_on_synthetic_inputs(tmp_path, monkeypatch):
    from studies import sma200_short as study
    d, idx, dtb3 = _synthetic_inputs()
    monkeypatch.setattr(study.data, "load_qqq", lambda end: d)
    monkeypatch.setattr(study.data, "load_indices", lambda end: idx)
    monkeypatch.setattr(study, "load_dtb3", lambda end: dtb3)
    monkeypatch.setattr(study, "OUT", tmp_path / "out")
    ledger = tmp_path / "ledger.md"
    ledger.write_text("x\n<!-- PREREG-BEGIN -->\nsection 0\n<!-- PREREG-END -->\n")
    monkeypatch.setattr(study, "LEDGER", ledger)
    monkeypatch.setattr(study, "ONESHOT_T01", None)
    study.main(["--register"])
    res = study.run(study.argparse.Namespace(fetch=False, data_only=False, register=False, run=True))
    out = tmp_path / "out"
    v = pd.read_csv(out / "qqq_verdicts.csv")
    assert len(v) == 12 and set(v["impl"]) == {"margin", "psq"}
    assert len(pd.read_csv(out / "index_verdicts.csv")) == 18
    assert res["n_trials"] == 30 and res["t01_engine_gap_vs_frozen_engine"] < 1e-6
    for f in ("qqq_summary.csv", "qqq_details.csv", "qqq_episodes.csv", "qqq_windows.csv", "qqq_by_decade.csv",
              "qqq_2015_2026.csv", "sqqq_2010_2026.csv", "sensitivities.csv", "index_summary.csv", "index_windows.csv",
              "inverse_validation.json", "run_log.txt"):
        assert (out / f).exists(), f
    inv = res["inverse_validation"]["PSQ"]
    assert inv["calibration"]["spread_calibrated"] == pytest.approx(0.002, abs=1e-6) and inv["acceptable"]
    # the post-hoc review checks (report only) run on the registered outputs and reproduce the registered index level
    monkeypatch.setattr(study.data, "etf_dividend_yield", lambda chart, index, fee, end: (0.01, "2000-01-03"))
    before = {f: (out / f).read_bytes() for f in ("qqq_summary.csv", "index_summary.csv", "results.json")}
    ph = study.run(study.argparse.Namespace(fetch=False, data_only=False, register=False, run=False, posthoc=True))
    assert ph["index"]["as_registered_max_abs_cagr_gap"] < 2e-6
    b = pd.read_csv(out / "posthoc_index_bases.csv")
    assert set(b["basis"]) == {"as_registered", "tbill_cash_no_div", "total_return_10k", "total_return_proceeds_tbill"}
    assert len(b) == 4 * 3 * 2 * 4                            # bases x indices x signals x (T01 + 3 positions)
    assert len(pd.read_csv(out / "posthoc_qqq.csv")) == 2 * 14 and (out / "posthoc_rf_cagr.csv").exists()
    assert all((out / f).read_bytes() == v for f, v in before.items())     # registered outputs untouched
    ledger.write_text("x\n<!-- PREREG-BEGIN -->\nsection 0 edited\n<!-- PREREG-END -->\n")
    with pytest.raises(SystemExit):
        study.run(study.argparse.Namespace(fetch=False, data_only=False, register=False, run=True))
    with pytest.raises(SystemExit):
        study.run(study.argparse.Namespace(fetch=False, data_only=False, register=False, run=False, posthoc=True))
