"""Offline tests for scripts/research_dow_theory.py (no cache files are read)."""
import json

import numpy as np
import pandas as pd
import pytest

from scripts import research_dow_theory as dt
from scripts import research_qqq_timing as qt


def _idx(n):
    return pd.bdate_range("2001-01-01", periods=n)


def _path(points, steps=5):
    """Piecewise-linear closes through ``points`` with ``steps`` sessions per segment."""
    out = []
    for a, b in zip(points[:-1], points[1:]):
        out += list(np.linspace(a, b, steps + 1)[:-1])
    out.append(points[-1])
    return pd.Series(out, index=_idx(len(out)))


# ---------------------------------------------------------------- swing detection on a synthetic series

def test_zigzag_confirms_swings_on_the_day_the_reversal_reaches_pct():
    # 100 -> 120 (high) -> 108 (-10%) -> 130 -> 117 (-10%)
    c = _path([100, 120, 108, 130, 117])
    lv = dt.swing_levels(c, 0.05)
    # the first close at least 5% below 120 is 114 (session 7: 120 -> 108 in 5 steps of -2.4 => 117.6, 115.2, ...)
    first_h = lv.index[lv["confirm"] == "H"][0]
    assert c.loc[first_h] <= 120 * 0.95 and c.loc[:first_h].iloc[:-1].iloc[-1] > 120 * 0.95
    assert np.isnan(lv["last_high"].loc[:first_h].iloc[:-1]).all()      # never back-filled before confirmation
    assert lv.loc[first_h, "last_high"] == pytest.approx(120)
    # the starting low 100 is confirmed first (once the rise reaches 5%), then 108 after the 120 high
    assert lv["last_low"].dropna().unique().tolist() == pytest.approx([100, 108])
    assert lv["last_high"].iloc[-1] == pytest.approx(130)
    assert lv["prev_high"].iloc[-1] == pytest.approx(120)
    assert lv["last_low"].iloc[-1] == pytest.approx(108)


def test_small_wiggles_below_threshold_are_not_swings():
    c = _path([100, 104, 101, 105, 102, 106])     # every reaction < 5%
    lv = dt.swing_levels(c, 0.05)
    assert lv["last_high"].isna().all()


def test_rhea_duration_filter_delays_or_rejects_confirmation():
    # a 10% drop that lasts only 4 sessions and recovers to new highs: Z confirms it, R with D=15 does not
    c = _path([100, 120, 108, 125], steps=4)
    assert (dt.swing_levels(c, 0.05)["confirm"] == "H").any()
    assert not (dt.swing_levels(c, 0.05, min_days=15, retrace=1 / 3)["confirm"] == "H").any()
    # a slow 20-session reaction from 120 to 108 is confirmed once 15 sessions have passed since the high
    c2 = _path([100, 120], steps=10)
    c2 = pd.concat([c2, pd.Series(np.linspace(119.4, 108, 20), index=pd.RangeIndex(20))]).reset_index(drop=True)
    lv = dt.swing_levels(c2.values, 0.05, min_days=15, retrace=1 / 3)
    ih = 10
    t = int(np.flatnonzero(lv["confirm"].values == "H")[0])
    assert t == ih + 15


def test_rhea_retracement_filter():
    # leg 100 -> 200 (after a confirmed low at 100); a 6% / 20-session reaction is < 1/3 of the 100-point leg
    up = list(np.linspace(110, 100, 6)) + list(np.linspace(100, 200, 40))
    down = list(np.linspace(200, 188, 21)[1:])
    c = np.array(up + down)
    lv = dt.swing_levels(c, 0.05, min_days=15, retrace=1 / 3)
    assert lv["last_low"].dropna().iloc[0] == pytest.approx(100)
    assert not (lv["confirm"].values[46:] == "H").any()
    lv_z = dt.swing_levels(c, 0.05)
    assert (lv_z["confirm"].values[46:] == "H").any()


def test_version2_reference_leg_does_not_lock_up_in_long_trends():
    # 100 -> 200 with no reaction, 200 -> 185 (7.5%, 20 sessions), 185 -> 260, 260 -> 230 (11.5%, 20 sessions).
    # 260 -> 230 is < 1/3 of the whole 100 -> 260 leg (version 1 rejects it) but > 1/3 of 185 -> 260 (version 2).
    c = np.array(list(np.linspace(110, 100, 6)) + list(np.linspace(100, 200, 60))[1:]
                 + list(np.linspace(200, 185, 21))[1:] + list(np.linspace(185, 260, 41))[1:]
                 + list(np.linspace(260, 230, 21))[1:])
    i260 = int(np.argmax(c))
    v1 = dt.swing_levels(c, 0.05, 15, 1 / 3, ref="leg")
    v2 = dt.swing_levels(c, 0.05, 15, 1 / 3, ref="intermediate")
    assert not (v1["confirm"].values[i260:] == "H").any()
    t = int(np.flatnonzero(v2["confirm"].values == "H")[-1])
    assert t > i260 and v2["last_high"].iloc[t] == pytest.approx(260)
    assert v2["last_low"].iloc[-1] == pytest.approx(100)   # the 185 low was never an R swing low


# ---------------------------------------------------------------- break direction and confirmation

def test_break_direction_up_and_down():
    c = _path([100, 120, 108, 125, 110, 100])
    lv = dt.swing_levels(c, 0.05)
    d = dt.break_direction(c, lv)
    t_up = c.index[(c > 120) & lv["last_high"].eq(120)][0]
    assert d.loc[t_up] == 1
    assert d.loc[c.index[c.index < t_up]].isin([0, 1]).all()
    assert d.iloc[-1] == -1                                       # fell below the 108 swing low


def test_strict_pattern_needs_higher_low():
    # lower path: the drop to 104 breaks the 108 low after a lower high (118 < 120) -> strict down break; the
    # rally through 118 comes after a lower low (104 < 108) -> not a strict up break, so it stays down.
    # higher path: low 112 > 108, so breaking the 118 high is a strict up break.
    lower = _path([100, 120, 108, 118, 104, 125])
    higher = _path([100, 120, 108, 118, 112, 125])
    for c, expect in [(lower, -1), (higher, 1)]:
        lv = dt.swing_levels(c, 0.05)
        assert dt.break_direction(c, lv, strict=True).iloc[-1] == expect
        assert dt.break_direction(c, lv, strict=False).iloc[-1] == 1


def test_confirmation_requires_both_averages():
    idx = _idx(6)
    a = pd.Series([0, 1, 1, 1, -1, -1], index=idx)
    b = pd.Series([0, 0, 0, 1, 1, -1], index=idx)
    s = dt.confirm_trend([a, b], initial=-1)
    # bull only when b confirms on day 3; bear only when b confirms on day 5; non-confirmation keeps the trend
    assert s.tolist() == [-1, -1, -1, 1, 1, -1]


def test_target_on_sessions_carries_forward():
    dow = pd.Series([1, -1, 1], index=pd.DatetimeIndex(["2001-01-02", "2001-01-03", "2001-01-05"]))
    sessions = pd.DatetimeIndex(["2001-01-01", "2001-01-02", "2001-01-03", "2001-01-04", "2001-01-05"])
    t = dt.target_on_sessions(dow, sessions)
    assert t.tolist() == [1.0, 1.0, 0.0, 0.0, 1.0]


# ---------------------------------------------------------------- no look-ahead

def _closes(n=1200, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("1995-01-02", periods=n)
    a = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.012, n)))
    b = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.015, n)))
    return pd.DataFrame({"DJI": a, "DJT": b}, index=idx)


@pytest.mark.parametrize("rule", dt.rule_grid())
def test_no_look_ahead(rule):
    closes = _closes()
    base = dt.dow_state(rule, closes)
    k = 700
    pert = closes.copy()
    pert.iloc[k + 1:] = pert.iloc[k + 1:].values[::-1] * 0.5       # rewrite the future
    out = dt.dow_state(rule, pert)
    pd.testing.assert_series_equal(base.iloc[:k + 1], out.iloc[:k + 1])
    ref = "leg" if rule.family == "R1" else "intermediate"
    lv = dt.swing_levels(closes["DJI"], rule.pct, rule.min_days, rule.retrace, ref)
    lv2 = dt.swing_levels(pert["DJI"], rule.pct, rule.min_days, rule.retrace, ref)
    pd.testing.assert_frame_equal(lv.iloc[:k + 1], lv2.iloc[:k + 1])


def test_grid_matches_preregistration():
    names = [r.name for r in dt.rule_grid()]
    assert len(names) == 18 and len(set(names)) == 18
    assert dt.FROZEN in names
    fz = {r.name: r for r in dt.rule_grid()}[dt.FROZEN]
    assert (fz.family, fz.pct, fz.min_days, round(fz.retrace, 4), fz.averages) == ("R", 0.05, 15, 0.3333, ("DJI", "DJT"))


# ---------------------------------------------------------------- date guard

def test_load_dow_truncates_at_dev_end(tmp_path, monkeypatch):
    days = ["2014-12-30", "2014-12-31", "2015-01-02", "2020-03-16"]
    ts = [int(pd.Timestamp(d + " 09:30", tz="America/New_York").timestamp()) for d in days]
    files = {}
    for k in ("DJI", "DJT"):
        doc = {"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [{"close": [1.0, 2.0, 3.0, 4.0]}]}}]}}
        p = tmp_path / f"{k}.json"
        p.write_text(json.dumps(doc))
        files[k] = p
    monkeypatch.setattr(dt, "DJ_FILES", files)
    closes, guard = dt.load_dow()
    assert closes.index.max() == pd.Timestamp("2014-12-31")
    assert guard["DJI"]["last"] == "2014-12-31"
    with pytest.raises(qt.FutureDataError):
        qt.assert_dev_dates(pd.DatetimeIndex(["2015-01-02"]))


def test_oneshot_window_is_fixed():
    class A:
        test_end = "2026-08-31"
    with pytest.raises(ValueError):
        dt.run_oneshot(A())
