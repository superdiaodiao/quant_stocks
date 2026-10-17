"""Tests for the intraday_minute study (synthetic minute data only): RTH filter and early closes, no look-ahead in
the rules, fill conventions, slippage / costs / integer shares, cash settlement, the PDT counter, the combo wrapper."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from quant.data.sources import alpaca
from quant.strategies import intraday as it


def day(c, o=None, h=None, l=None, v=None, prev=100.0, date="2020-03-02", factor=1.0) -> it.Day:
    c = np.asarray(c, dtype=float)
    n = len(c)
    o = c.copy() if o is None else np.asarray(o, float)
    h = np.maximum(o, c) if h is None else np.asarray(h, float)
    l = np.minimum(o, c) if l is None else np.asarray(l, float)
    v = np.full(n, 1000.0) if v is None else np.asarray(v, float)
    return it.make_day(date, np.arange(n), o, h, l, c, v, c, n, prev, factor)


def flat(n=390, px=100.0):
    return np.full(n, px)


# ---------------------------------------------------------------- data layer

def test_minute_frame_and_rth_filter_with_early_close():
    def bar(t, c):
        return {"t": t, "o": c, "h": c, "l": c, "c": c, "v": 10, "vw": c, "n": 1}
    body = json.dumps({"bars": {"QQQ": [bar("2020-11-27T14:29:00Z", 1), bar("2020-11-27T14:30:00Z", 2),
                                        bar("2020-11-27T17:59:00Z", 3), bar("2020-11-27T18:00:00Z", 4),
                                        bar("2020-11-30T20:59:00Z", 5), bar("2020-11-30T21:00:00Z", 6)]},
                       "next_page_token": None}).encode()
    f = alpaca.minute_frame([body], "QQQ")
    r = alpaca.rth_frame(f, {"2020-11-27"})
    # 09:29 ET excluded; 12:59 ET kept on the early close, 13:00 dropped; 15:59 kept, 16:00 dropped
    assert list(zip(r["date"], r["minute"], r["close"])) == [("2020-11-27", 0, 2.0), ("2020-11-27", 209, 3.0),
                                                            ("2020-11-30", 389, 5.0)]


def test_month_windows_cover_months():
    w = alpaca.month_windows("2016-01", "2016-03")
    assert w[0] == ("2016-01", "2016-01-01T00:00:00Z", "2016-02-01T00:00:00Z") and len(w) == 3


def test_make_day_fills_missing_minutes_flat():
    d = it.make_day("2020-01-02", np.array([0, 2]), [1, 3], [1, 3], [1, 3], [1, 3], [5, 5], [1, 3], 4, 0.5)
    assert list(d.c) == [1, 1, 3, 3] and list(d.v) == [5, 0, 5, 0] and d.bars_present == 2


# ---------------------------------------------------------------- no look-ahead

def _future_shock_invariant(fn, d: it.Day, cut: int):
    """Trades whose decision happened before ``cut`` must not change when bars >= cut change."""
    base = [t for t in fn(d) if t.entry_i <= cut - 1]
    d2 = it.Day(d.date, d.o.copy(), d.h.copy(), d.l.copy(), d.c.copy(), d.v.copy(), d.vw.copy(), d.prev_close)
    rng = np.random.default_rng(1)
    shock = rng.normal(0, 3, d.n - cut)
    for a in (d2.o, d2.h, d2.l, d2.c, d2.vw):
        a[cut:] += shock
    d2.h[cut:] = np.maximum.reduce([d2.o[cut:], d2.c[cut:], d2.h[cut:]]) + 1
    d2.l[cut:] = np.minimum.reduce([d2.o[cut:], d2.c[cut:], d2.l[cut:]]) - 1
    new = [t for t in fn(d2) if t.entry_i <= cut - 1]
    assert [(t.side, t.entry_i, t.entry_px) for t in base] == [(t.side, t.entry_i, t.entry_px) for t in new]


def test_rules_entries_do_not_use_future_bars():
    rng = np.random.default_rng(0)
    for seed in range(5):
        c = 100 + np.cumsum(np.random.default_rng(seed).normal(0, 0.1, 390))
        o = np.r_[100, c[:-1]]
        d = day(c, o=o, h=np.maximum(o, c) + 0.05, l=np.minimum(o, c) - 0.05,
                v=rng.uniform(500, 1500, 390))
        sig = np.full(390, 0.002)
        for fn in (it.orb, lambda x: it.orb(x, long_only=False), it.orb5_direction, it.last_half_hour,
                   lambda x: it.last_half_hour(x, "first", False), it.vwap_reversion,
                   lambda x: it.noise_area(x, sig)):
            for cut in (40, 120, 200, 365):
                _future_shock_invariant(fn, d, cut)


def test_orb_enters_next_open_after_completed_breakout_and_stops():
    c = flat()
    h, l = flat() + 0.5, flat() - 0.5             # range 99.5 .. 100.5
    o = flat()
    c[50] = 101.0                                  # bar 50 closes above the range high
    o[51], c[51] = 101.2, 101.3
    h[51] = 101.4
    l[51] = 101.1
    o[52:], c[52:] = 101.0, 101.0
    h[52:] = 101.1
    l[52:] = 100.9
    l[100] = 99.0                                  # touches the stop (range low 99.5); opens above it
    t = it.orb(day(c, o=o, h=h, l=l))
    assert len(t) == 1 and t[0].entry_i == 51 and t[0].entry_px == 101.2
    assert t[0].exit_i == 100 and t[0].exit_px == 99.5 and t[0].exit_kind == "stop"
    o[100] = 99.2                                  # gap through the stop: filled at the open
    assert it.orb(day(c, o=o, h=h, l=l))[0].exit_px == 99.2


def test_orb5_direction_and_last_half_hour():
    c = flat()
    c[4] = 101                                     # first 5-min candle up
    c[5:] = 101.0                                  # never back to the 5-min low
    t = it.orb5_direction(day(c))
    assert t[0].side == 1 and t[0].entry_i == 5 and t[0].exit_kind == "close"
    c = flat(390, 101.0)
    c[359] = 102.0                                 # 15:29 bar above the previous close (100)
    t = it.last_half_hour(day(c))
    assert t[0].side == 1 and t[0].entry_i == 360 and t[0].exit_i == 389
    c[359] = 99.0
    assert it.last_half_hour(day(c)) == []
    assert it.last_half_hour(day(c), "rod", long_only=False)[0].side == -1
    # early close: 210 bars, last half hour starts at bar 180, signal bar 179
    c = flat(210, 101.0)
    assert it.last_half_hour(day(c))[0].entry_i == 180


def test_vwap_reversion_and_noise_area_basic():
    c = flat()
    c[100] = 95.0                                  # far below VWAP
    c[101:103] = 95.5
    t = it.vwap_reversion(day(c), long_only=True)
    assert t[0].side == 1 and t[0].entry_i == 101 and t[0].exit_kind == "signal" and t[0].exit_i == 104
    c = flat()
    c[29:] = 103.0                                 # above UB = 100 * 1.01 at 10:00
    t = it.noise_area(day(c, prev=100.0), np.full(390, 0.01))
    assert t[0].side == 1 and t[0].entry_i == 30 and t[0].exit_kind == "close"


def test_regime_flags_use_previous_close_only():
    p = pd.Series(np.r_[np.full(200, 100.0), 110.0, 90.0, 90.0], index=pd.date_range("2020-01-01", periods=203))
    f = it.regime_flags(p, 200)
    assert not f.iloc[200] and f.iloc[201] and not f.iloc[202]


# ---------------------------------------------------------------- engine

def _days(n_days, dates=None, px=100.0):
    dates = dates or [str(d.date()) for d in pd.bdate_range("2020-03-02", periods=n_days)]
    return [day(flat(390, px), prev=px, date=x) for x in dates]


def test_fills_slippage_costs_and_integer_shares():
    days = _days(2)
    tr = [[], [it.Trade(1, 31, 100.0, 389, 101.0, "close", True)]]
    r = it.simulate(days, tr, np.zeros(2), slip_mult=1.0)
    row = r.trades.iloc[0]
    fill_in, fill_out = 100.01, 101 - 0.0101        # 1 cent > 1 bp of 100; 1 bp of 101 > 1 cent
    n = int((10_000 - it.CASH_BUFFER) // fill_in)
    assert row["shares"] == n
    expect = n * (fill_out - fill_in) - it.order_cost(n, 100.0, False) - it.order_cost(n, 101.0, True)
    assert abs(r.equity.iloc[-1] - (10_000 + expect)) < 1e-6
    assert it.slippage(500.0, 1.0) == 0.05 and it.slippage(50.0, 3.0) == 0.03


def test_pdt_counter_caps_three_day_trades_in_five_sessions():
    days = _days(8)
    tr = [[it.Trade(1, 31, 100.0, 389, 100.0, "close", True)] for _ in days]
    r = it.simulate(days, tr, np.zeros(8), account=it.Account("margin", pdt=True), start=0)
    taken = r.trades["date"].tolist()
    # sessions 0,1,2 trade; 3,4 blocked (window of 5 incl. today); 5, 6, 7 see only two day trades in their window
    assert len(taken) == 6 and r.stats["skipped_pdt"] == 2
    hist = [1 if str(d.date) in taken else 0 for d in days]
    for i in range(len(hist)):
        assert sum(hist[max(0, i - 4):i + 1]) <= 3
    free = it.simulate(days, tr, np.zeros(8), account=it.Account("margin", pdt=False), start=0)
    assert len(free.trades) == 8


def test_cash_account_settlement_and_no_shorts():
    days = _days(4, ["2020-03-02", "2020-03-03", "2020-03-04", "2020-03-05"])
    tr = [[it.Trade(1, 31, 100.0, 389, 100.0, "close", True)] for _ in days]
    r = it.simulate(days, tr, np.zeros(4), account=it.Account("cash"), start=0)
    # T+2: proceeds of day 0 settle on day 2, so day 1 has no settled cash
    assert r.trades["date"].tolist() == ["2020-03-02", "2020-03-04"] and r.stats["skipped_no_cash"] == 2
    days = _days(3, ["2024-06-03", "2024-06-04", "2024-06-05"])
    tr = [[it.Trade(1, 31, 100.0, 389, 100.0, "close", True), it.Trade(-1, 380, 100.0, 389, 100.0, "close", True)]
          for _ in days]
    r = it.simulate(days, tr, np.zeros(3), account=it.Account("cash"), start=0)
    assert len(r.trades) == 3 and r.stats["skipped_short_cash"] == 3      # T+1: every day; shorts refused


def test_combo_holds_overnight_and_carries_long_to_close():
    d0 = day(flat(390, 100.0), prev=100.0, date="2020-03-02")
    d1 = day(flat(390, 102.0), prev=100.0, date="2020-03-03")      # overnight +2%, flat day
    r = it.simulate([d0, d1], [[], []], np.zeros(2), combo=True, slip_mult=0.0)
    assert 0.0195 < r.ret.iloc[0] < 0.0201                          # overnight gain, sold at open, rebought at close
    tr = [[], [it.Trade(1, 100, 102.0, 389, 102.0, "close", False)]]
    r2 = it.simulate([d0, d1], tr, np.zeros(2), combo=True, slip_mult=0.0)
    assert bool(r2.trades.iloc[0]["carried"]) and r2.stats["day_trades"] == 0
    assert r2.stats["orders"] == r.stats["orders"]                   # carried: replaces the MOC buy, no sell


def test_interest_on_cash_overnight():
    days = _days(2)
    r = it.simulate(days, [[], []], np.array([0.0, 0.001]))
    assert abs(r.ret.iloc[0] - 0.001) < 1e-12


def test_session_with_missing_minutes_is_not_traded():
    d = it.make_day("2020-01-02", np.arange(300), *(np.full(300, 100.0),) * 4, np.full(300, 1.0),
                    np.full(300, 100.0), 390, 99.0)
    assert not d.usable
    assert all(it.rule_trades(s, d, np.full(390, 0.001), True) == [] for s in it.RULES)
