"""Tests for scripts/research_mean_reversion.py: causal signals, next-close execution, slot / share / time-stop rules."""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import scripts.research_mean_reversion as mr


def walk(n=600, seed=0, drift=0.0006, vol=0.012):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2010-01-01", periods=n)
    return pd.Series(100 * np.cumprod(1 + rng.normal(drift, vol, n)), index=idx)


# ---------------------------------------------------------------- signals

def test_rsi2_textbook_values():
    p = pd.Series([10.0, 11.0, 12.0, 11.0], index=pd.bdate_range("2020-01-01", periods=4))
    r = mr.rsi2(p)
    # gains 1, 1 -> avg up 1, avg down 0 -> 100; then a loss of 1: up 0.5, down 0.5 -> 50
    assert np.isnan(r.iloc[0])
    assert r.iloc[2] == pytest.approx(100.0)
    assert r.iloc[3] == pytest.approx(50.0)


def test_state_enters_on_pullback_and_exits_above_sma5():
    up = list(np.linspace(50, 150, 230))
    path = up + [149, 146, 143, 147, 152, 153]          # pullback, then a rebound above the 5-day average
    p = pd.Series(path, index=pd.bdate_range("2015-01-01", periods=len(path)))
    st = mr.connors_state(p)
    assert st.iloc[229] == 0.0
    assert st.iloc[231] == 1.0                           # RSI(2) < 10 while above SMA200
    assert st.iloc[-1] == 0.0                            # back above SMA5 -> flat


def test_state_never_enters_below_sma200():
    p = pd.Series(np.linspace(200, 50, 400), index=pd.bdate_range("2015-01-01", periods=400))
    st = mr.connors_state(p)
    assert (st.dropna() == 0).all()


@pytest.mark.parametrize("k", [250, 333, 480])
def test_state_unchanged_by_truncation_and_future_shocks(k):
    p = walk()
    full = mr.connors_state(p)
    part = mr.connors_state(p.iloc[:k + 1])
    pd.testing.assert_series_equal(full.iloc[:k + 1], part)
    p2 = p.copy()
    p2.iloc[k + 1:] *= 0.5
    pd.testing.assert_series_equal(mr.connors_state(p2).iloc[:k + 1], full.iloc[:k + 1])


def test_episodes():
    s = pd.Series([0, 1, 1, 0, 0, 1, 0, 1], index=pd.bdate_range("2020-01-01", periods=8))
    assert mr.episodes(s) == [(1, 3), (5, 6)]


# ---------------------------------------------------------------- ETF engine

def etf_frames(n=50):
    idx = pd.bdate_range("2020-01-01", periods=n)
    rets = pd.DataFrame({"QQQ": 0.01, "CASH": 0.0001}, index=idx)
    close = pd.DataFrame({"QQQ": 100.0, "CASH": 1.0}, index=idx)
    return idx, rets, close


def test_etf_next_close_execution_and_free_cash():
    idx, rets, close = etf_frames()
    state = pd.Series(0.0, index=idx)
    state.iloc[10:15] = 1.0
    w = mr.etf_weights("R1", state)
    sim = mr.etf_sim(w, rets, close, start_i=1)
    ex = sim["executed"]["QQQ"]
    assert ex.loc[idx[10]] == 0 and ex.loc[idx[11]] == 1      # decided at 10, filled at the close of 11
    assert ex.loc[idx[15]] == 1 and ex.loc[idx[16]] == 0
    nocost = mr.etf_sim(w, rets, close, start_i=1, costs=False)
    r = nocost["ret"]
    assert r.loc[idx[11]] == pytest.approx(0.0001)             # still cash on the fill day's return
    assert r.loc[idx[12]] == pytest.approx(0.01)
    assert sim["ret"].loc[idx[11]] < 0.0001                    # the entry cost is booked on the fill day
    assert nocost["value"].iloc[0] == pytest.approx(mr.ACCOUNT)   # starting in cash costs nothing
    assert sim["value"].iloc[-1] < nocost["value"].iloc[-1]


def test_r2_weights():
    idx = pd.bdate_range("2020-01-01", periods=3)
    w = mr.etf_weights("R2", pd.Series([0.0, 1.0, np.nan], index=idx))
    assert w.iloc[0].to_dict() == {"QQQ": 1.0, "QLD": 0.0}
    assert w.iloc[1].to_dict() == {"QQQ": 0.5, "QLD": 0.5}
    assert w.iloc[2].isna().all()


# ---------------------------------------------------------------- stock engine

def stock_inputs(n=60, names=8, entry_days=None, rsi=None, exit_days=None, price=37.0):
    idx = pd.bdate_range("2019-01-01", periods=n)
    sids = [f"S{j}" for j in range(names)]
    I = pd.DataFrame(1.0, index=idx, columns=sids)
    close = pd.DataFrame(price, index=idx, columns=sids)
    data = SimpleNamespace(sessions=idx, spec={"perf_start": str(idx[0].date()), "effective_end": str(idx[-1].date())},
                           perf_idx=I, close=close, last_row=pd.Series(idx[-1], index=sids))
    inp = mr.StockInputs.__new__(mr.StockInputs)
    inp.data = data
    weeks = [d for d in idx if d.weekday() == 4]
    inp.elig = {w: list(sids) for w in [idx[0]] + weeks}
    inp.rank_of = {w: {s: 10 for s in sids} for w in inp.elig}
    inp.entry = pd.DataFrame(False, index=idx, columns=sids)
    for d, s in (entry_days or []):
        inp.entry.loc[idx[d], s] = True
    inp.rsi = pd.DataFrame(50.0, index=idx, columns=sids) if rsi is None else rsi
    inp.exit = pd.DataFrame(False, index=idx, columns=sids)
    for d, s in (exit_days or []):
        inp.exit.loc[idx[d], s] = True
    inp.oneq_lvl = pd.Series(1.0, index=idx)
    inp.oneq_px = pd.Series(50.0, index=idx)
    return inp, idx


def test_stock_time_stop_and_whole_shares():
    inp, idx = stock_inputs(entry_days=[(3, "S0")])
    res = mr.stock_sim(inp, "cash")
    t = res["trades"]
    assert len(t) == 1
    row = t.iloc[0]
    assert row["entry"] == str(idx[4].date())               # signal at 3, filled at the close of 4
    assert row["reason"] == "time" and row["sessions"] == mr.MAX_HOLD
    assert row["shares"] == int(row["shares"]) == int(0.2 * mr.ACCOUNT // 37.0)


def test_stock_exit_above_sma5_next_close():
    inp, idx = stock_inputs(entry_days=[(3, "S0")], exit_days=[(6, "S0")])
    t = mr.stock_sim(inp, "cash")["trades"]
    assert t.iloc[0]["exit"] == str(idx[7].date()) and t.iloc[0]["reason"] == "sma5"


def test_stock_slots_and_lowest_rsi_priority():
    rsi = pd.DataFrame(50.0, index=pd.bdate_range("2019-01-01", periods=60), columns=[f"S{j}" for j in range(8)])
    rsi.iloc[3] = [4.0, 1.0, 3.0, 0.5, 2.0, 4.5, 0.1, 3.5]
    inp, idx = stock_inputs(entry_days=[(3, f"S{j}") for j in range(8)], rsi=rsi)
    res = mr.stock_sim(inp, "cash")
    bought = set(res["trades"]["sid"])
    assert bought == {"S6", "S3", "S1", "S4", "S2"}          # the five lowest RSI(2) values
    assert res["counts"]["buy"] == mr.K


def test_stock_same_close_sensitivity_fills_on_signal_day():
    inp, idx = stock_inputs(entry_days=[(3, "S0")])
    t = mr.stock_sim(inp, "cash", lag=0)["trades"]
    assert t.iloc[0]["entry"] == str(idx[3].date()) and t.iloc[0]["sessions"] == mr.MAX_HOLD


def test_stock_oneq_parking_keeps_money_invested():
    inp, idx = stock_inputs(entry_days=[(3, "S0")])
    res = mr.stock_sim(inp, "oneq")
    assert res["counts"]["oneq_orders"] >= 3                 # initial park, fund the buy, park the proceeds
    inp.oneq_lvl = pd.Series(np.linspace(1.0, 1.2, len(idx)), index=idx)
    res2 = mr.stock_sim(inp, "oneq")
    cash_res = mr.stock_sim(inp, "cash")
    assert res2["nav"].iloc[-1] > cash_res["nav"].iloc[-1] * 1.1


def test_stock_decisions_ignore_future_prices():
    inp, idx = stock_inputs(entry_days=[(3, "S0"), (20, "S1")])
    a = mr.stock_sim(inp, "cash")["nav"]
    inp.data.perf_idx.iloc[30:, :] *= 3.0                    # a future jump changes nothing before it
    b = mr.stock_sim(inp, "cash")["nav"]
    pd.testing.assert_series_equal(a.iloc[:30], b.iloc[:30])
