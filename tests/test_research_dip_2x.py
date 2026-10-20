"""Tests for the dip_2x study on synthetic data only: causal signals (no look-ahead), the 2x daily-reset compounding,
take-profit / stop-loss / time-stop timing, sizing and the slot limit, the margin call, idle QQQ, cash interest, the
synthetic-vs-real check and the quote parser."""
from __future__ import annotations

import json
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from quant.data import leveraged_etfs as le
from quant.strategies import dip_2x as dx

P = dx.Params(account=10_000.0, slot=0.20, k=5, max_hold=20, t1_drop=-0.05, t2_below=0.10, t3_rsi=5.0, sma_short=20,
              sma_long=200, rsi_n=2, etf_spread=0.056, etf_er=0.010, etf_price=50.0, margin_spread=0.015,
              maintenance=0.25, reg_t=2.0, cash_fee=0.0010, hs_stock_bps=2.0, hs_etf_bps=10.0, hs_qqq_bps=1.0)
P0 = replace(P, hs_stock_bps=0.0, hs_etf_bps=0.0, hs_qqq_bps=0.0)


def universe(rets: dict, n: int = 260, start: int = 0, rf: float = 0.0, qqq_ret: float = 0.0,
             lists=None, last_row=None) -> dx.Universe:
    """Daily returns per name (array of length n, the first entry ignored); simulation from session ``start``."""
    idx = pd.bdate_range("2015-01-01", periods=n)
    X = pd.DataFrame({k: np.cumprod(1 + np.r_[0.0, np.asarray(v, float)[1:]]) for k, v in rets.items()}, index=idx)
    P_ = X * 100.0
    q = pd.Series(np.cumprod(np.r_[1.0, np.full(n - 1, 1 + qqq_ret)]), index=idx)
    lists = lists or [(idx[0], list(rets))]
    lr = pd.Series(pd.NaT, index=list(rets)) if last_row is None else last_row
    return dx.Universe("test", X, P_, lr, lists, idx[start:], q, q * 300.0, pd.Series(rf, index=idx),
                       pd.Series(0.03, index=idx), pd.Series(0.005, index=idx), {k: k for k in rets})


def flat_with_drop(n=260, drop_at=30, drop=-0.06, after=0.0):
    r = np.zeros(n)
    r[drop_at] = drop
    r[drop_at + 1:] = after
    return r


CFG = dx.Config("T1", "I1", 0.05, None, "cash")


# ---------------------------------------------------------------- signals

def test_signals_definitions_and_causality():
    rng = np.random.default_rng(3)
    r = rng.normal(0.0005, 0.02, 400)
    u = universe({"A": r}, n=400)
    sig = dx.signals(u.X, P)
    x = u.X["A"]
    t1 = (x / x.shift(1) - 1 <= -0.05)
    assert sig["T1"][0]["A"].equals(t1)
    sma20 = x.rolling(20, min_periods=20).mean()
    assert sig["T2"][0]["A"].equals(x <= 0.9 * sma20)
    # causality: changing the future never changes a past signal
    r2 = r.copy()
    r2[300:] = -0.08
    sig2 = dx.signals(universe({"A": r2}, n=400).X, P)
    for t in ("T1", "T2", "T3"):
        a, b = sig[t][1]["A"].iloc[:300], sig2[t][1]["A"].iloc[:300]
        assert np.allclose(a.fillna(-9), b.fillna(-9)), t
        assert sig[t][0]["A"].iloc[:300].equals(sig2[t][0]["A"].iloc[:300])
    # T3 needs 200 sessions of history
    assert not sig["T3"][0]["A"].iloc[:199].any()


def test_list_in_force_from_its_date():
    u = universe({"A": np.zeros(30), "B": np.zeros(30)}, n=30,
                 lists=[(pd.Timestamp("2015-01-01"), ["A"]), (pd.Timestamp("2015-01-15"), ["B"])])
    li = dx.list_index(u)
    d = list(u.dates)
    k = d.index(pd.Timestamp("2015-01-15"))
    assert li[k - 1] == [0] and li[k] == [1]


# ---------------------------------------------------------------- engine: timing, no look-ahead

def test_entry_next_close_and_take_profit_1x():
    r = flat_with_drop(after=0.01)
    u = universe({"A": r})
    res = dx.simulate(u, CFG, P0)
    t = res["trades"].iloc[0]
    d = list(u.dates)
    assert t["entry"] == d[31].date().isoformat()          # drop at the close of 30, bought at the close of 31
    # R >= 5% first at the close of 31 + 5 (1.01^5 = 1.051), sold at the next close
    assert t["exit"] == d[37].date().isoformat() and t["reason"] == "take_profit"
    assert t["ret_gross"] == pytest.approx(1.01 ** 6 - 1, rel=1e-9)


def test_2x_daily_reset_compounding_and_faster_take_profit():
    r = flat_with_drop(after=0.01)
    u = universe({"A": r}, rf=0.0002)
    cfg = replace(CFG, instrument="I2")
    res = dx.simulate(u, cfg, P0)
    t = res["trades"].iloc[0]
    d = list(u.dates)
    g = 2 * 0.01 - (0.0002 + 0.056 / 252) - 0.010 / 252
    k = int(np.ceil(np.log(1.05) / np.log(1 + g)))          # first close with R >= 5%
    assert t["entry"] == d[31].date().isoformat()
    assert t["exit"] == d[31 + k + 1].date().isoformat()
    assert t["ret_gross"] == pytest.approx((1 + g) ** (k + 1) - 1, rel=1e-9)
    assert t["under_ret"] == pytest.approx(1.01 ** (k + 1) - 1, rel=1e-9)


def test_2x_value_path_matches_compounded_formula_on_random_returns():
    rng = np.random.default_rng(7)
    r = rng.normal(0.0, 0.005, 260)                          # no other -5% day
    r[30] = -0.07
    r[31:52] = rng.normal(0.0, 0.03, 21)
    u = universe({"A": r}, rf=0.0001)
    cfg = dx.Config("T1", "I2", 5.0, None, "cash")           # unreachable take-profit: held to the time stop
    res = dx.simulate(u, cfg, P0)
    t = res["trades"].iloc[0]
    assert t["reason"] == "time_stop" and t["sessions"] == 20
    g = np.maximum(2 * r[32:52] - (0.0001 + 0.056 / 252) - 0.010 / 252, -1.0)
    assert t["ret_gross"] == pytest.approx(np.prod(1 + g) - 1, rel=1e-9)


def test_time_stop_twentieth_session():
    u = universe({"A": flat_with_drop()})
    res = dx.simulate(u, CFG, P0)
    t = res["trades"].iloc[0]
    d = list(u.dates)
    assert t["reason"] == "time_stop" and t["sessions"] == 20 and t["exit"] == d[51].date().isoformat()


def test_stop_loss_only_in_the_stop_variant():
    u = universe({"A": flat_with_drop(after=-0.02)})
    cfg = dx.Config("T1", "I2", 0.05, -0.15, "cash")
    t = dx.simulate(u, cfg, P0)["trades"].iloc[0]
    assert t["reason"] == "stop_loss"
    g = 2 * -0.02 - 0.056 / 252 - 0.010 / 252
    k = int(np.ceil(np.log(0.85) / np.log(1 + g)))
    assert t["sessions"] == k + 1
    t2 = dx.simulate(u, replace(cfg, stop=None), P0)["trades"].iloc[0]
    assert t2["reason"] == "time_stop"


def test_im_return_is_twice_the_stock_and_take_profit_on_it():
    u = universe({"A": flat_with_drop(after=0.01)})
    cfg = replace(CFG, instrument="IM")
    t = dx.simulate(u, cfg, P0)["trades"].iloc[0]
    # 2 x (1.01^k - 1) >= 5% first at k = 3
    assert t["sessions"] == 4 and t["reason"] == "take_profit"
    assert t["ret_gross"] == pytest.approx(2 * (1.01 ** 4 - 1), rel=1e-9)


def test_no_look_ahead_in_the_engine():
    rng = np.random.default_rng(11)
    rets = {k: rng.normal(0.0003, 0.03, 300) for k in "ABCDEFG"}
    u = universe(rets, n=300)
    for cfg in (dx.Config("T1", "I2", 0.05, -0.15, "qqq"), dx.Config("T2", "IM", 0.10, None, "cash")):
        a = dx.simulate(u, cfg, P)
        rets2 = {k: v.copy() for k, v in rets.items()}
        for v in rets2.values():
            v[200:] = rng.normal(-0.01, 0.05, 100)
        b = dx.simulate(universe(rets2, n=300), cfg, P)
        cut = u.dates[199]
        assert np.allclose(a["nav"][:cut], b["nav"][:cut])
        ta = a["trades"][pd.to_datetime(a["trades"]["exit"]) <= cut]
        tb = b["trades"][pd.to_datetime(b["trades"]["exit"]) <= cut]
        assert ta[["sid", "entry", "exit", "reason"]].equals(tb[["sid", "entry", "exit", "reason"]])


# ---------------------------------------------------------------- sizing, slots, accounts

def test_slot_limit_deepest_first_and_twenty_percent():
    rets = {}
    for j, k in enumerate("ABCDEFG"):
        r = np.zeros(80)
        r[30] = -0.06 - 0.01 * j                              # G drops most
        rets[k] = r
    u = universe(rets, n=80)
    res = dx.simulate(u, dx.Config("T1", "I1", 0.05, None, "cash"), P0)
    first = res["trades"][res["trades"]["entry"] == u.dates[31].date().isoformat()]
    assert sorted(first["sid"]) == ["C", "D", "E", "F", "G"]
    assert res["counts"]["skipped_no_slot"] == 2
    assert res["positions"].max() == 5
    # each position 20% of equity (whole shares at $100 x the index)
    nav31 = res["nav"].iloc[31]
    assert res["exposure"].iloc[31] == pytest.approx(1.0, abs=0.03)
    assert nav31 == pytest.approx(10_000, rel=1e-3)


def test_margin_call_liquidates_im_positions():
    rets = {}
    for k in "ABCDE":
        r = np.zeros(80)
        r[30] = -0.06
        r[35] = -0.40                                         # gross 2 x NAV, a 40% fall -> equity < 25% of longs
        rets[k] = r
    u = universe(rets, n=80)
    res = dx.simulate(u, dx.Config("T1", "IM", 0.10, None, "cash"), P0)
    assert res["counts"]["margin_calls"] == 1
    d35 = u.dates[35].date().isoformat()
    mc_ = res["trades"][res["trades"]["exit"] == d35]
    assert len(mc_) == 5 and set(mc_["reason"]) == {"margin_call"}
    assert res["positions"].iloc[35] == 0                     # (the -40% day is itself a new T1 signal)
    nav = res["nav"]
    assert nav.iloc[35] == pytest.approx(10_000 * (1 - 2 * 0.40), rel=0.06)   # whole shares: gross ~1.97 x NAV
    assert dx.simulate(u, dx.Config("T1", "I1", 0.10, None, "cash"), P0)["counts"]["margin_calls"] == 0


def test_idle_qqq_tracks_qqq_and_is_sold_for_entries():
    u = universe({"A": flat_with_drop()}, qqq_ret=0.001)
    res = dx.simulate(u, dx.Config("T1", "I2", 0.05, None, "qqq"), P0)
    nav = res["nav"]
    assert nav.iloc[20] / nav.iloc[0] == pytest.approx(1.001 ** 20, rel=2e-3)    # whole QQQ shares at $300
    assert res["qqq_weight"].iloc[20] > 0.97
    assert res["qqq_weight"].iloc[31] == pytest.approx(0.8, abs=0.04)            # 20% moved into the 2x leg
    assert res["exposure"].iloc[31] == pytest.approx(0.4, abs=0.02)


def test_cash_interest_and_zero_interest_variant():
    u = universe({"A": np.zeros(100)}, n=100, rf=0.0002)
    res = dx.simulate(u, CFG, P0)
    assert res["nav"].iloc[-1] / res["nav"].iloc[0] == pytest.approx((1 + 0.0002 - 0.001 / 252) ** 99, rel=1e-9)
    res0 = dx.simulate(u, CFG, replace(P0, cash_interest=False))
    assert res0["nav"].iloc[-1] == pytest.approx(10_000.0)


def test_costs_reduce_net_return():
    u = universe({"A": flat_with_drop(after=0.01)})
    t0 = dx.simulate(u, CFG, P0)["trades"].iloc[0]
    t1 = dx.simulate(u, CFG, P)["trades"].iloc[0]
    assert t1["ret_net"] < t0["ret_net"] and t1["cost"] > t0["cost"] > 0


def test_delisting_converts_to_cash():
    r = flat_with_drop()
    r[40] = -0.5                                              # terminal value booked the session after the last row
    idx = pd.bdate_range("2015-01-01", periods=260)
    u = universe({"A": r}, last_row=pd.Series([idx[39]], index=["A"]))
    t = dx.simulate(u, CFG, P0)["trades"].iloc[0]
    assert t["reason"] == "delisted" and t["exit"] == idx[40].date().isoformat()
    assert t["ret_gross"] == pytest.approx(-0.5)


# ---------------------------------------------------------------- the synthetic check and the data layer

def test_validate_synthetic_recovers_spread():
    idx = pd.bdate_range("2024-01-01", periods=300)
    rng = np.random.default_rng(5)
    u = pd.Series(rng.normal(0.001, 0.02, 300), index=idx)
    rf = pd.Series(0.0002, index=idx)
    etf = 2 * u - (rf + 0.05 / 252) - 0.01 / 252
    v = dx.validate_synthetic(etf, u, rf, 0.05, 0.01)
    assert v["ann_real_minus_synthetic"] == pytest.approx(0.0, abs=1e-12)
    assert v["implied_spread_over_tbill"] == pytest.approx(0.05, abs=1e-9)
    assert v["beta_on_underlying"] == pytest.approx(2.0, abs=1e-9)


def test_quote_spreads_and_window():
    body = json.dumps({"quotes": [{"bp": 99.99, "ap": 100.01}, {"bp": 0, "ap": 100.0}, {"bp": 50.0, "ap": 50.05}],
                       "next_page_token": None}).encode()
    sp = le.quote_spreads(body)
    assert np.allclose(sp, [0.02 / 100.0, 0.05 / 50.025])
    s, e = le.utc_window("2025-07-31", "15:55:00", "15:55:10")
    assert (s, e) == ("2025-07-31T19:55:00Z", "2025-07-31T19:55:10Z")
    assert le.ETFS["TSLL"][3] == "2024-04-02" and le.ETFS["METU"][2] == le.ETFS["METU"][3]


def test_half_tick_before_decimalisation():
    h = dx.half_tick_series(pd.DatetimeIndex(["2001-04-06", "2001-04-09"]))
    assert list(h) == [1 / 32, 0.005]


# ---------------------------------------------------------------- review fixes (2026-10-21)

def test_no_price_at_the_fill_blocks_the_purchase():
    """A trigger at t whose name has no price of record at t+1 is not bought (the forward-filled X does not count)."""
    r = flat_with_drop()
    u = universe({"A": r})
    hp = pd.DataFrame(True, index=u.X.index, columns=["A"])
    hp.iloc[31, 0] = False                                    # the fill session of the drop at 30
    u.has_price = hp
    res = dx.simulate(u, CFG, P0)
    assert res["counts"]["skipped_no_price"] == 1 and res["counts"]["buys"] == 0 and len(res["trades"]) == 0
    u.has_price = None
    assert dx.simulate(u, CFG, P0)["counts"]["buys"] == 1


def test_last_priced_session_is_a_candidate_without_look_ahead():
    """A name that dips on its last priced session takes a slot at t (whether t is its last price is unknown) and is
    then not bought at t+1; with one slot the shallower dip of another name is not bought either."""
    idx = pd.bdate_range("2015-01-01", periods=80)
    a, b = flat_with_drop(80, drop_at=30, drop=-0.09), flat_with_drop(80, drop_at=30, drop=-0.06)
    u = universe({"A": a, "B": b}, n=80, last_row=pd.Series([idx[30], pd.NaT], index=["A", "B"]))
    p1 = replace(P0, k=1)
    res = dx.simulate(u, CFG, p1)
    assert res["counts"]["skipped_no_price"] == 1 and res["counts"]["skipped_no_slot"] == 1
    assert res["counts"]["buys"] == 0
    u2 = universe({"A": a, "B": b}, n=80, last_row=pd.Series([idx[40], pd.NaT], index=["A", "B"]))
    t = dx.simulate(u2, CFG, p1)["trades"]
    assert t.iloc[0]["sid"] == "A" and t.iloc[0]["entry"] == idx[31].date().isoformat()


def test_matched_twin_has_the_same_trades_as_im():
    rng = np.random.default_rng(13)
    rets = {k: rng.normal(0.0005, 0.03, 300) for k in "ABCDEF"}
    u = universe(rets, n=300)
    cfg = dx.Config("T1", "IM", 0.10, None, "cash")
    assert cfg.matched_twin() == dx.Config("T1", "I1", 0.05, None, "cash")
    assert dx.Config("T1", "IM", 0.10, -0.15, "cash").matched_twin() is None
    assert dx.Config("T1", "I1", 0.05, None, "cash").matched_twin() is None
    a, b = dx.simulate(u, cfg, P0), dx.simulate(u, cfg.matched_twin(), P0)
    key = ["sid", "entry", "exit", "reason"]
    assert len(a["trades"]) > 5 and a["trades"][key].equals(b["trades"][key])


def test_hold_list_returns_and_financed_h_match():
    u = universe({"A": np.full(30, 0.01), "B": np.full(30, -0.01)}, n=30, rf=0.0001)
    r_ew = dx.ew_returns(u)
    h = dx.hold_list_returns(u, r_ew, P)
    assert np.allclose(h["1x"].iloc[1:], 0.0)
    assert np.allclose(h["2x_etf"].iloc[1:], -(0.0001 + 0.056 / 252) - 0.010 / 252)
    borrow = dx.margin_borrow_rate(u, u.dates, P)
    days = np.diff(u.dates.values).astype("timedelta64[D]").astype(int)
    assert np.allclose(borrow.iloc[1:], (0.03 + 0.015) * days / 360)
    assert np.allclose(h["2x_margin"].iloc[1:], -borrow.iloc[1:])
    rq, rf = pd.Series(0.002, index=u.dates), pd.Series(0.0001, index=u.dates)
    assert np.allclose(dx.h_match_financed_returns(r_ew, rq, rf, borrow, 0.3, 0.5), 0.5 * 0.002 + 0.2 * 0.0001)
    assert np.allclose(dx.h_match_financed_returns(r_ew, rq, rf, borrow, 0.6, 0.9), 0.9 * 0.002 - 0.5 * borrow)


def test_margin_cushion_mask_trigger_and_trade_contributions():
    idx = pd.bdate_range("2015-01-01", periods=4)
    res = {"exposure": pd.Series([0.0, 1.0, 2.5, 1.2], index=idx), "qqq_weight": pd.Series([1.0, 0.5, 0.5, 0.0], index=idx)}
    m = dx.margin_cushion(res, 0.25)
    assert m["max_gross"] == pytest.approx(3.0) and m["max_gross_date"] == "2015-01-05"
    assert m["min_equity_over_gross"] == pytest.approx(1 / 3) and m["cushion"] == pytest.approx(1 / 3 - 0.25)
    u = universe({"A": flat_with_drop()})
    sig = dx.signals(u.X, P)
    off = dx.mask_trigger(sig, "T1", [("A", u.dates[0], u.dates[-1])])
    assert sig["T1"][0]["A"].any() and not off["T1"][0]["A"].any() and off["T2"] is sig["T2"]
    assert dx.simulate(u, CFG, P0, off)["counts"]["buys"] == 0
    u2 = universe({"A": flat_with_drop(after=0.01)})
    res = dx.simulate(u2, CFG, P0)
    c = dx.trade_contributions(res["trades"], res["nav"])
    prev = res["nav"].shift(1)[pd.Timestamp(res["trades"].iloc[0]["exit"])]
    assert c.iloc[0] == pytest.approx(np.log1p(res["trades"].iloc[0]["pnl"] / prev))


def test_correlated_trial_hurdles():
    from quant.evaluation.criteria import bonferroni_t, bootstrap_max_t, effective_trials, sidak_t
    rng = np.random.default_rng(17)
    z = rng.normal(size=(400, 1))
    same = np.hstack([z + 1e-9 * rng.normal(size=(400, 1)) for _ in range(10)])
    assert effective_trials(same)["nyholt"] == pytest.approx(1.0, abs=0.01)
    assert effective_trials(same)["li_ji"] == pytest.approx(1.0, abs=0.01)
    indep = rng.normal(size=(4000, 10))
    assert effective_trials(indep)["nyholt"] > 9.5 and effective_trials(indep)["li_ji"] > 9.0
    assert sidak_t(72) == pytest.approx(bonferroni_t(72), abs=0.01) and sidak_t(1) == pytest.approx(1.645, abs=1e-3)
    one = bootstrap_max_t(rng.normal(size=(300, 1)), 1, 2000, 1)
    assert one == pytest.approx(1.645, abs=0.15)
    ten_same = bootstrap_max_t(same[:300], 6, 1000, 2)
    ten_indep = bootstrap_max_t(indep[:300], 6, 1000, 2)
    assert ten_same < ten_indep and ten_indep == pytest.approx(sidak_t(10), abs=0.25)
