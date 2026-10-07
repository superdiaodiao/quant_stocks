"""Synthetic tests for scripts/research_selective_t.py (no vendor data needed)."""
import math

import numpy as np
import pandas as pd
import pytest

from scripts import research_intraday_t as it
from scripts import research_selective_t as m


def make(o, h, lo, c, sma_now=None, sma_prev=None, rsi=None, split=None, div=None, sym="AAPL"):
    n = len(c)
    dates = pd.bdate_range("2020-01-01", periods=n)
    c = np.asarray(c, float)
    split = np.ones(n) if split is None else np.asarray(split, float)
    s = it.Series_(sym, dates, np.asarray(o, float), np.asarray(h, float), np.asarray(lo, float), c,
                   np.r_[np.nan, c[:-1]] / split, split, np.zeros(n) if div is None else np.asarray(div, float),
                   np.full(n, 0.02), pd.Series(c, index=dates).pct_change(), {})
    sn = np.full(n, 100.0) if sma_now is None else np.asarray(sma_now, float)
    sp = np.r_[np.nan, sn[:-1]] if sma_prev is None else np.asarray(sma_prev, float)
    return m.Market(sym, s, np.ones(n), sn, sp, np.full(n, 50.0) if rsi is None else np.asarray(rsi, float))


def flat(n, p=100.0):
    return [p] * n, [p] * n, [p] * n, [p] * n


def base_shares(p=100.0, sym="AAPL", frac=m.BASE_FRAC):
    return math.floor(frac * (m.START_EQUITY - m.BUFFER) / (p * (1 + m.hs_of(sym, p))))


def test_rsi_wilder_bounds():
    up = np.arange(1, 40, dtype=float)
    assert m.rsi_wilder(up)[-1] == pytest.approx(100.0)
    zig = np.array([100, 101] * 20, float)
    r = m.rsi_wilder(zig)
    assert np.isnan(r[:14]).all() and 40 < r[-1] < 60


def test_hold_has_no_trips_and_flat_value():
    mk = make(*flat(30))
    sim = m.simulate(mk, 1, 29, m.Spec(()), None)
    assert sim["trips"].empty
    assert sim["exposure"] == pytest.approx(base_shares() * 100 / sim["value"].iloc[-1], rel=1e-9)


def test_s1_sell_next_open_buy_back_at_sma_limit():
    # close of day 2 is 6% above SMA (100) -> sell at the open of day 3 (106); day 4 low 99.0 trades through 100
    o, h, lo, c = flat(10)
    c[2], o[3], h[3], lo[3], c[3] = 106, 106, 106.5, 104, 105
    o[4], h[4], lo[4], c[4] = 103, 103, 99.0, 99.5
    mk = make(o, h, lo, c)
    fam = m.Fam("S1", "X", 0.05, max_hold=20)
    sim = m.simulate(mk, 1, 9, m.Spec((fam,), costs=False), None)
    tr = sim["trips"]
    assert len(tr) == 1
    q = base_shares() // 3
    assert tr.iloc[0]["q"] == q and tr.iloc[0]["reason"] == "target"
    assert tr.iloc[0]["gross"] == pytest.approx(q * (106 - 100))
    assert tr.iloc[0]["days"] == 2


def test_s1_touch_is_not_a_fill_and_timeout_closes():
    o, h, lo, c = flat(30)
    c[2] = 106
    for t in range(3, 30):
        o[t] = h[t] = c[t] = 104
        lo[t] = 100.0         # touches 100 but not through 0.05%
    mk = make(o, h, lo, c)
    fam = m.Fam("S1", "X", 0.05, max_hold=20)
    sim = m.simulate(mk, 1, 29, m.Spec((fam,), costs=False), None)
    tr = sim["trips"]
    assert tr.iloc[0]["reason"] == "timeout" and tr.iloc[0]["days"] == 20
    assert tr.iloc[0]["exit"] == mk.s.sessions[3 + 19]
    sim2 = m.simulate(mk, 1, 29, m.Spec((fam,), costs=False, through=False), None)
    assert sim2["trips"].iloc[0]["reason"] == "target"


def test_s2_gap_fill_same_day_uses_reserve_and_slippage():
    o, h, lo, c = flat(10)
    o[3], h[3], lo[3], c[3] = 105, 105, 99.0, 99.5      # gap up 5% then fills the gap the same day
    mk = make(o, h, lo, c)
    fam = m.Fam("S2", "G", 0.04, max_hold=6)
    sim = m.simulate(mk, 1, 9, m.Spec((fam,), costs=False, slip=0.001), None)
    tr = sim["trips"]
    q = base_shares() // 3
    assert len(tr) == 1 and tr.iloc[0]["same_day"]
    assert tr.iloc[0]["gross"] == pytest.approx(q * (105 * 0.999 - 100))
    assert sim["day_trades"] == 1


def test_s2_timeout_on_fifth_session_after_gap():
    o, h, lo, c = flat(15)
    for t in range(3, 15):
        o[t] = h[t] = lo[t] = c[t] = 106
    mk = make(o, h, lo, c)
    fam = m.Fam("S2", "G", 0.04, max_hold=6)
    tr = m.simulate(mk, 1, 14, m.Spec((fam,), costs=False), None)["trips"]
    assert tr.iloc[0]["reason"] == "timeout" and tr.iloc[0]["exit"] == mk.s.sessions[3 + 5]


def test_s3_add_on_drop_sell_at_target_and_cash_limited():
    o, h, lo, c = flat(12)
    c[2] = 95                                    # -5% day -> buy at the open of day 3
    o[3], h[3], lo[3], c[3] = 94, 95, 93, 94
    o[4], h[4], lo[4], c[4] = 96, 100.2, 95, 99  # SMA 100 > entry 94 -> limit min(100, 94*1.06=99.64) = 99.64
    mk = make(o, h, lo, c)
    fam = m.Fam("S3", "D", 0.04, z=0.06, max_hold=20)
    sim = m.simulate(mk, 1, 11, m.Spec((fam,), costs=False), None)
    tr = sim["trips"]
    assert len(tr) == 1 and tr.iloc[0]["reason"] == "target"
    q = tr.iloc[0]["q"]
    b = base_shares()
    cash = m.START_EQUITY - b * 100
    assert q == min(b // 3, math.floor((cash - m.BUFFER) / (m.CAP_MULT * 95)))
    assert tr.iloc[0]["gross"] == pytest.approx(q * (it.ceil_cent(94 * 1.06) - 94))


def test_s3_limit_uses_target_when_sma_below_entry():
    mk = make(*flat(5), sma_now=[90] * 5)
    cy = m.Cycle(m.Fam("S3", "D", 0.04, z=0.06), 10, 95.0, 1)
    assert m.exit_limit(mk, 2, cy) == pytest.approx(it.ceil_cent(95 * 1.06))


def test_cash_account_buyback_needs_settled_cash():
    # S1 sells at open of day 3; the price collapses below the SMA the same day. With no reserve (base 100%) the
    # cash account cannot buy back on the sale day (proceeds settle T+1) and buys back on day 4.
    o, h, lo, c = flat(10)
    c[2] = 106
    o[3], h[3], lo[3], c[3] = 106, 106, 98, 99
    o[4], h[4], lo[4], c[4] = 99, 99, 98, 98.5
    mk = make(o, h, lo, c)
    fam = m.Fam("S1", "X", 0.05)
    cash = m.simulate(mk, 1, 9, m.Spec((fam,), costs=False, base_frac=1.0, rebalance=False), None)["trips"]
    assert cash.iloc[0]["exit"] == mk.s.sessions[4] and cash.iloc[0]["gross"] > 0
    marg = m.simulate(mk, 1, 9, m.Spec((fam,), costs=False, mode="margin", base_frac=1.0, rebalance=False),
                      None)["trips"]
    assert marg.iloc[0]["exit"] == mk.s.sessions[3]


def test_dividend_missed_by_sold_shares_and_split_scales_cycle():
    o, h, lo, c = flat(10)
    c[2] = 106
    for t in range(3, 10):
        o[t] = h[t] = lo[t] = c[t] = 106
    div = np.zeros(10)
    div[5] = 1.0
    mk = make(o, h, lo, c, div=div)
    fam = m.Fam("S1", "X", 0.05)
    s_t = m.simulate(mk, 1, 9, m.Spec((fam,), costs=False), None)
    s_h = m.simulate(mk, 1, 9, m.Spec((), costs=False), None)
    q = base_shares() // 3
    # T value = hold value - dividend missed on q shares (prices flat at 106 after the sale at 106)
    assert s_h["value"].iloc[-1] - s_t["value"].iloc[-1] == pytest.approx(q * 1.0)
    # split 2:1 on day 5 while the cycle is open: buy-back quantity doubles
    sp = np.ones(10)
    sp[5] = 2.0
    o2, h2, l2, c2 = (np.array(x, float) for x in (o, h, lo, c))
    for arr in (o2, h2, l2, c2):
        arr[5:] /= 2
    mk2 = make(o2, h2, l2, c2, sma_now=np.r_[[100] * 5, [50] * 5], split=sp)
    tr = m.simulate(mk2, 1, 9, m.Spec((fam,), costs=False), None)["trips"]
    assert tr.iloc[0]["q"] == 2 * q


def test_s4_runs_two_independent_cycles():
    o, h, lo, c = flat(12)
    c[2] = 106                                   # S1 signal
    o[3], h[3], lo[3], c[3] = 106, 106, 94, 94   # -11% day: S3-D signal; S1 buy-back fills at 100 intraday
    o[4], h[4], lo[4], c[4] = 94, 101, 93, 100
    mk = make(o, h, lo, c)
    fams = (m.Fam("S1", "X", 0.05), m.Fam("S3", "D", 0.04, z=0.06))
    tr = m.simulate(mk, 1, 11, m.Spec(fams, costs=False), None)["trips"]
    assert sorted(tr["family"]) == ["S1", "S3"]


def test_costs_reduce_value():
    o, h, lo, c = flat(10)
    c[2] = 106
    o[3], h[3], lo[3], c[3] = 106, 106, 99, 99.5
    mk = make(o, h, lo, c)
    fam = m.Fam("S1", "X", 0.05)
    a = m.simulate(mk, 1, 9, m.Spec((fam,), costs=False), None)
    b = m.simulate(mk, 1, 9, m.Spec((fam,)), None)
    assert b["value"].iloc[-1] < a["value"].iloc[-1]
    tr = b["trips"].iloc[0]
    assert tr["cost"] > 0.7      # two orders, at least the $0.35 minimum each


def test_bh_and_configs():
    assert list(m.bh_reject([0.001, 0.5, 0.02, 0.9])) == [True, False, True, False]
    assert len(m.CONFIGS) * len(m.ASSETS) == 18
    assert m.fams_of("S4b", "stock")[1].value == 0.04 and m.fams_of("S1-Xa", "QQQ")[0].value == 0.03


def test_ohlc_frame_truncates_and_parses():
    ts = [int(pd.Timestamp(d, tz="America/New_York").replace(hour=9, minute=30).timestamp())
          for d in ("2026-09-29", "2026-09-30", "2026-10-01")]
    payload = {"chart": {"result": [{"timestamp": ts, "meta": {"dataGranularity": "1d"},
                                     "indicators": {"quote": [{"open": [1, 2, 3], "high": [1, 2, 3], "low": [1, 2, 3],
                                                               "close": [1, 2, 3]}],
                                                    "adjclose": [{"adjclose": [1, 2, 3]}]},
                                     "events": {"splits": {"x": {"date": ts[1], "numerator": 2, "denominator": 1}}}}]}}
    df, splits = m.ohlc_frame(payload, "2026-09-30")
    assert list(df["date"]) == ["2026-09-29", "2026-09-30"] and splits == {"2026-09-30": 2.0}
