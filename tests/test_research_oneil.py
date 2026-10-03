"""Offline tests for scripts/research_oneil.py (synthetic data only; no cache files are read)."""
import numpy as np
import pandas as pd
import pytest

from scripts import research_oneil as on


# ---------------------------------------------------------------- synthetic series

def _uptrend(n=150, a=60.0, b=100.0):
    return list(np.linspace(a, b, n))


def _cup(depth=0.25, n=100, top=100.0):
    x = np.linspace(0, 1, n)
    return list(top - depth * top * np.sin(np.pi * x))[1:]          # starts just after the left peak, ends at top


def _handle(n=8, low=94.0, top=100.0):
    return list(np.linspace(top, low, n + 1))[1:]


def cup_series(depth=0.25, handle_low=94.0, handle_vol=0.6, breakout=101.0, cup_n=100):
    c = _uptrend() + _cup(depth, cup_n) + _handle(8, handle_low) + [breakout]
    v = np.ones(len(c)) * 1e6
    v[-9:-1] = handle_vol * 1e6
    v[-1] = 2e6
    return np.array(c), v


def test_cup_with_handle_detected_with_pivot():
    c, v = cup_series()
    d = len(c) - 1
    base = on.find_base(c[:d], v[:d])
    assert base is not None and base["type"] == "cup_handle"
    assert base["pivot"] == pytest.approx(100.0 * 1.001)
    assert base["depth"] == pytest.approx(0.25, abs=0.01)
    assert base["handle_days"] == 8
    assert on.is_breakout(c, v, d, base)
    # the $0.10 tick wins when it is larger than 0.1%
    assert on.cup_with_handle(c[:d], v[:d], tick_abs=0.5)["pivot"] == pytest.approx(100.5)


def test_breakout_needs_volume_and_close_above_pivot():
    c, v = cup_series()
    d = len(c) - 1
    base = on.find_base(c[:d], v[:d])
    v2 = v.copy()
    v2[-1] = 1.2e6
    assert not on.is_breakout(c, v2, d, base)              # volume only 1.2x (< 1.4x)
    c2 = c.copy()
    c2[-1] = 100.05
    assert not on.is_breakout(c2, v, d, base)              # below the pivot (handle high + 0.1%)


def test_v_shape_rejected():
    c = _uptrend() + list(np.linspace(100, 75, 11))[1:] + list(np.linspace(75, 100, 11))[1:] + _handle(8) + [101]
    c = np.array(c)
    v = np.ones(len(c)) * 1e6
    v[-9:-1] = 0.6e6
    assert on.cup_with_handle(c[:-1], v[:-1]) is None


@pytest.mark.parametrize("kw", [dict(depth=0.50, handle_low=96.0),     # cup too deep
                                dict(depth=0.08, handle_low=96.0),     # too shallow for a cup
                                dict(handle_low=80.0),                 # handle decline 20%
                                dict(handle_vol=1.5)])                 # handle volume not drying up
def test_bad_cups_rejected(kw):
    c, v = cup_series(**kw)
    assert on.cup_with_handle(c[:-1], v[:-1]) is None


def test_handle_in_lower_half_rejected():
    c, v = cup_series(depth=0.15, handle_low=89.0)        # bottom 85, mid-point 92.5, handle low 89 (11% decline)
    assert on.cup_with_handle(c[:-1], v[:-1]) is None
    c, v = cup_series(depth=0.15, handle_low=93.0)
    assert on.cup_with_handle(c[:-1], v[:-1]) is not None


def test_handle_too_short_rejected():
    c = _uptrend() + _cup() + _handle(3) + [101]
    c = np.array(c)
    v = np.ones(len(c)) * 1e6
    assert on.cup_with_handle(c[:-1], v[:-1]) is None


def test_bear_market_allows_deeper_cup():
    c, v = cup_series(depth=0.38, handle_low=95.0)
    assert on.cup_with_handle(c[:-1], v[:-1]) is None
    ix = np.ones(len(c) - 1) * 1000.0
    ix[200:230] = 750.0                                     # the index fell 25% during the cup
    assert on.cup_with_handle(c[:-1], v[:-1], ix) is not None


def test_flat_base_detected():
    rng = np.random.default_rng(0)
    c = _uptrend(300, 40, 100) + list(92 + 8 * rng.random(39)) + [100.0] + [100.5]
    c = np.array(c)
    v = np.ones(len(c)) * 1e6
    v[-1] = 2e6
    base = on.find_base(c[:-1], v[:-1])
    assert base is not None and base["type"] == "flat_base"
    assert base["pivot"] == pytest.approx(100.0 * 1.001)
    assert base["base_days"] >= 25
    # a 4-week range is too short
    c2 = np.array(_uptrend(300, 30, 80) + list(92 + 8 * rng.random(19)) + [100.0] + [100.5])
    assert on.flat_base(c2[:-1], np.ones(len(c2) - 1)) is None
    # no prior advance -> no flat base
    c3 = np.array([95.0] * 150 + list(92 + 8 * rng.random(39)) + [100.0] + [100.5])
    assert on.flat_base(c3[:-1], np.ones(len(c3) - 1)) is None


def _w(second_low=77.0, mid=92.0):
    up = _uptrend(150, 60, 100)
    seg = lambda a, b, n: list(np.linspace(a, b, n + 1))[1:]  # noqa: E731
    return np.array(up + seg(100, 80, 20) + seg(80, mid, 15) + seg(mid, second_low, 15) + seg(second_low, 91, 15)
                    + [mid + 0.5])


def test_double_bottom_detected_pivot_at_middle_peak():
    c = _w()
    v = np.ones(len(c)) * 1e6
    v[-1] = 2e6
    base = on.find_base(c[:-1], v[:-1])
    assert base is not None and base["type"] == "double_bottom"
    assert base["pivot"] == pytest.approx(92.0 * 1.001)
    assert on.is_breakout(c, v, len(c) - 1, base)


def test_double_bottom_needs_undercut():
    c = _w(second_low=83.0)                                # second low above the first: no undercut
    assert on.double_bottom(c[:-1], np.ones(len(c) - 1)) is None


# ---------------------------------------------------------------- no look-ahead in the event scan

def test_breakout_events_use_only_past_data():
    c1, v1 = cup_series()
    n = len(c1) + 40
    idx = pd.bdate_range("2015-01-01", periods=n)
    rng = np.random.default_rng(1)
    c = pd.DataFrame({"X": np.r_[c1, 101 + np.cumsum(rng.normal(0, 1, 40))],
                      "Y": 50 + np.cumsum(rng.normal(0, 1, n))}, index=idx)
    v = pd.DataFrame({"X": np.r_[v1, 1e6 * (1 + rng.random(40))], "Y": 1e6 * (1 + rng.random(n))}, index=idx)
    el = pd.DataFrame(True, index=idx, columns=c.columns)
    ix = pd.Series(1000.0, index=idx)
    full = on.breakout_events(c, v, c, ix, el, idx[0])
    assert ((full["security_id"] == "X") & (full["session"] == idx[len(c1) - 1]) & (full["type"] == "cup_handle")).any()
    cut = idx[len(c1) + 10]
    part = on.breakout_events(c.loc[:cut], v.loc[:cut], c.loc[:cut], ix.loc[:cut], el.loc[:cut], idx[0])
    a = full[full["session"] <= cut].reset_index(drop=True)
    pd.testing.assert_frame_equal(a, part.reset_index(drop=True))
    # changing the future does not change past events
    c_f = c.copy()
    c_f.loc[c_f.index > cut, "X"] *= 3
    fut = on.breakout_events(c_f, v, c_f, ix, el, idx[0])
    pd.testing.assert_frame_equal(fut[fut["session"] <= cut].reset_index(drop=True), a)


# ---------------------------------------------------------------- M: distribution / follow-through days

def _index_path(changes, vols, start=1000.0):
    c = start * np.cumprod(1 + np.asarray(changes, float))
    h, l_ = c * 1.002, c * 0.998
    o = np.r_[start, c[:-1]]
    return o, h, l_, c, np.asarray(vols, float)


def test_follow_through_day_turns_market_on():
    ch = [0.0] + [-0.01] * 10 + [0.002, 0.001, 0.001, 0.015] + [0.001] * 5
    vol = [1e9] * len(ch)
    vol[14] = 1.2e9                                        # FTD day volume above the prior day
    m = on.market_state(*_index_path(ch, vol), dd_limit=5, ftd_gain=0.0125)
    assert not m["uptrend"].iloc[13]
    assert m["ftd"].iloc[14] and m["uptrend"].iloc[14]    # day 4 of the rally attempt, +1.5% on higher volume
    m2 = on.market_state(*_index_path(ch, vol), dd_limit=5, ftd_gain=0.017)
    assert not m2["uptrend"].any()                        # +1.5% is below the 1.7% threshold
    vol2 = list(vol)
    vol2[14] = 0.9e9
    assert not on.market_state(*_index_path(ch, vol2))["uptrend"].any()   # lower volume: no FTD


def test_ftd_not_before_day_four():
    ch = [0.0] + [-0.01] * 10 + [0.002, 0.015] + [0.0] * 3
    vol = [1e9] * len(ch)
    vol[12] = 1.2e9
    assert not on.market_state(*_index_path(ch, vol))["uptrend"].any()     # day 2 of the rally


def test_undercutting_rally_low_resets_attempt():
    ch = [0.0] + [-0.01] * 10 + [0.002, 0.001, -0.03, 0.002, 0.001, 0.001, 0.015]
    vol = [1e9] * len(ch)
    vol[-1] = 1.2e9
    m = on.market_state(*_index_path(ch, vol))
    assert m["ftd"].iloc[-1]                              # counted from the new day 1 (index 14): day 4 at index 17
    ch2 = [0.0] + [-0.01] * 10 + [0.002, 0.001, -0.03, 0.002, 0.015]
    vol2 = [1e9] * len(ch2)
    vol2[-1] = 1.2e9
    assert not on.market_state(*_index_path(ch2, vol2))["uptrend"].any()   # only day 2 after the reset


def _uptrend_then_dd(n_dd, gap=2, dd_vol_up=True):
    ch = [0.0] + [-0.01] * 5 + [0.002, 0.001, 0.001, 0.02]          # FTD at index 9
    vol = [1e9] * len(ch)
    vol[9] = 1.5e9
    for _ in range(n_dd):
        ch += [0.0005] * gap + [-0.003]
        vol += [1e9] * gap + [1.2e9 if dd_vol_up else 0.8e9]
    return ch, vol


def test_distribution_days_turn_market_off():
    ch, vol = _uptrend_then_dd(5)
    m = on.market_state(*_index_path(ch, vol), dd_limit=5)
    assert m["uptrend"].iloc[9]
    assert m["dd_day"].sum() == 5 or m["dd_count"].max() >= 4
    assert not m["uptrend"].iloc[-1]                      # the 5th distribution day within 25 sessions
    assert m["uptrend"].iloc[-2]
    assert on.market_state(*_index_path(ch, vol), dd_limit=6)["uptrend"].iloc[-1]
    ch, vol = _uptrend_then_dd(5, dd_vol_up=False)        # declines on lower volume are not distribution
    assert on.market_state(*_index_path(ch, vol), dd_limit=5)["uptrend"].iloc[-1]


def test_distribution_days_expire_after_25_sessions():
    ch, vol = _uptrend_then_dd(5, gap=6)                  # 7 sessions apart: the first drops out before the 5th
    m = on.market_state(*_index_path(ch, vol), dd_limit=5)
    assert m["uptrend"].iloc[-1]
    assert m["dd_count"].iloc[-1] == 4


def test_market_state_is_causal():
    rng = np.random.default_rng(3)
    ch = rng.normal(0.0003, 0.012, 600)
    vol = 1e9 * (1 + rng.random(600))
    full = on.market_state(*_index_path(ch, vol))
    part = on.market_state(*_index_path(ch[:400], vol[:400]))
    pd.testing.assert_frame_equal(full.iloc[:400].reset_index(drop=True), part.reset_index(drop=True))


# ---------------------------------------------------------------- sell rules

CFG = on.Config()


def _pos(entry_i=10):
    return {"entry_i": entry_i, "hold_until": None, "big": False}


def test_stop_and_profit_and_50_day_line():
    assert on.position_exit(_pos(), 20, -0.08, False, False, CFG) == "stop"
    assert on.position_exit(_pos(), 20, -0.07, False, False, CFG) is None
    assert on.position_exit(_pos(), 40, 0.21, False, False, CFG) == "profit"        # +21% after week 3
    assert on.position_exit(_pos(), 40, 0.05, True, False, CFG) == "sma50_heavy"
    assert on.position_exit(_pos(), 40, -0.02, False, True, CFG) == "market_correction"
    assert on.position_exit(_pos(), 40, 0.03, False, True, CFG) is None             # winners kept in a correction


def test_eight_week_hold():
    p = _pos(entry_i=10)                                   # breakout at session 9
    assert on.position_exit(p, 20, 0.22, False, False, CFG) is None                 # +22% within 3 weeks -> hold
    assert p["hold_until"] == 9 + 40
    assert on.position_exit(p, 30, 0.35, True, False, CFG) is None                  # no profit taking, no 50-day rule
    assert on.position_exit(p, 45, -0.09, False, False, CFG) == "stop"              # the stop still applies
    p = _pos(entry_i=10)
    on.position_exit(p, 20, 0.22, False, False, CFG)
    assert on.position_exit(p, 49, 0.40, False, False, CFG) is None                 # hold over -> big winner
    assert p["big"] and p["hold_until"] is None
    assert on.position_exit(p, 60, 0.50, False, False, CFG) is None                 # no 20% target any more
    assert on.position_exit(p, 61, 0.30, True, False, CFG) == "sma50_heavy"
    assert on.position_exit(p, 62, 0.0, False, False, CFG) == "breakeven"
    q = _pos(entry_i=10)
    assert on.position_exit(q, 26, 0.22, False, False, CFG) == "profit"             # day 17 after breakout: too late


# ---------------------------------------------------------------- engine: next-close execution

def _inputs(prices, events, up=True, below50=None):
    idx = pd.bdate_range("2021-01-04", periods=len(prices))
    px = pd.DataFrame({"S": np.asarray(prices, float)}, index=idx)
    I = px / px.iloc[0]
    b50 = pd.DataFrame(False, index=idx, columns=["S"]) if below50 is None else below50
    upser = pd.Series(up if isinstance(up, (list, np.ndarray)) else [up] * len(idx), index=idx)
    ev = {idx[i]: [("S", pv, 1.0, "cup_handle")] for i, pv in events.items()}
    return on.SimInputs(perf=idx, I=I, C=px, P=px, below50=b50, last_row=pd.Series({"S": idx[-1]}), events=ev,
                        uptrend=upser, rank_of={}, weeks=[idx[0]]), idx


def test_buy_fills_at_next_close_within_five_percent():
    prices = [100, 100, 100, 104, 104.5, 105, 105, 105]
    s, idx = _inputs(prices, {3: 103.0})
    res = on.simulate(CFG, s)
    o = res["orders"]
    assert len(o) == 1 and o.iloc[0]["date"] == idx[4] and o.iloc[0]["side"] == "buy"
    assert o.iloc[0]["value"] == pytest.approx(2000.0)                              # NAV / 5
    s, idx = _inputs([100, 100, 100, 104, 108.5, 109, 109, 109], {3: 103.0})        # 5.3% above the pivot
    res = on.simulate(CFG, s)
    assert res["orders"].empty and res["counts"]["skipped_extended"] == 1


def test_no_buy_outside_confirmed_uptrend():
    s, idx = _inputs([100, 100, 100, 104, 104.5, 105], {3: 103.0}, up=[True, True, True, False, True, True])
    assert on.simulate(CFG, s)["orders"].empty


def test_stop_decided_at_close_filled_next_close():
    prices = [100, 100, 100, 104, 104, 100, 95.5, 95, 94, 94]
    s, idx = _inputs(prices, {3: 103.0})
    res = on.simulate(CFG, s)
    o = res["orders"]
    assert list(o["side"]) == ["buy", "sell"]
    assert o.iloc[1]["date"] == idx[7] and o.iloc[1]["reason"] == "stop"            # -8.2% at idx 6 -> sold at idx 7
    t = res["trades"].iloc[0]
    assert t["ret"] == pytest.approx(95 / 104 - 1, abs=0.004)          # incl. both order costs


def test_eight_week_hold_in_engine():
    prices = [100, 100, 100, 104, 104] + [104 * (1 + 0.025 * k) for k in range(1, 11)] + [130] * 50
    s, idx = _inputs(prices, {3: 103.0})
    res = on.simulate(CFG, s)
    o = res["orders"]
    assert list(o["side"]) == ["buy"]                       # +20% in 8 sessions -> held through the 8 weeks
    sell = res["nav"]
    assert len(sell) == len(prices)


# ---------------------------------------------------------------- periods, folds and guards

def test_periods_and_folds_fixed():
    assert on.PERIODS["A"]["perf_start"] == "2014-01-01" and on.PERIODS["A"]["perf_end"] == "2019-12-31"
    assert on.PERIODS["B"]["perf_start"] == "2020-01-01" and on.PERIODS["B"]["perf_end"] == "2026-08-31"
    assert on.FOLDS["fold1"] == {"calibrate": "A", "test": "B"}
    assert on.FOLDS["fold2"] == {"calibrate": "B", "test": "A"}
    assert on.PERIODS["A"]["price_start"] < on.PERIODS["A"]["perf_start"]
    assert len(on.calibration_grid()) == 12
    assert len({c.name for c in on.calibration_grid()}) == 12


def test_test_refuses_without_frozen_rule(tmp_path, monkeypatch):
    monkeypatch.setattr(on, "OUT", tmp_path)
    with pytest.raises(SystemExit):
        on.run_test("fold1")


def test_plateau_pick_uses_neighbours():
    cfgs = on.calibration_grid()
    rows = [{"ir": 0.0, "n_buy": 20} for _ in cfgs]
    rows[0]["ir"] = 5.0                                    # an isolated spike
    for j in on._neighbours(cfgs[11], cfgs) + [11]:
        rows[j]["ir"] = 1.0                                # a plateau around variant 12
    rows[11]["ir"] = 1.5
    pick, tab, note = on.plateau_pick(rows, cfgs)
    assert pick == 12
