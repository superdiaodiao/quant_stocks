"""Offline tests for scripts/research_canslim_dev.py (synthetic data only; no cache files are read)."""
import numpy as np
import pandas as pd
import pytest

from scripts import research_canslim_dev as cs


# ---------------------------------------------------------------- date guard

def test_window_constants():
    assert cs.DEV_START == "2017-01-01" and cs.DEV_END == "2022-12-31"
    assert cs.PRICE_START >= "2015-01-01"


def test_truncate_and_guard():
    frame = pd.DataFrame({"date": ["2015-09-30", "2015-10-01", "2022-12-30", "2023-01-03"], "x": [1, 2, 3, 4]})
    out = cs.truncate(frame, "date", cs.PRICE_START)
    assert out["date"].tolist() == ["2015-10-01", "2022-12-30"]
    with pytest.raises(cs.DateGuardError):
        cs.assert_window(["2022-12-30", "2023-01-03"])
    with pytest.raises(cs.DateGuardError):
        cs.assert_window(pd.DatetimeIndex(["2016-12-30"]), cs.DEV_START, cs.DEV_END)
    cs.assert_window(pd.DatetimeIndex(["2017-01-03", "2022-12-30"]), cs.DEV_START, cs.DEV_END)


def test_signal_schedule_executes_next_session_inside_window():
    sessions = pd.bdate_range("2016-12-26", "2017-01-20")
    m = pd.Series(True, index=sessions)
    weeks = [pd.Timestamp("2016-12-23"), pd.Timestamp("2016-12-30"), pd.Timestamp("2017-01-06"),
             pd.Timestamp("2017-01-13"), pd.Timestamp("2017-01-20")]
    sched = cs.signal_schedule(weeks[1:], sessions, m, "weekly")
    assert sched[0][0] == pd.Timestamp("2016-12-30") and sched[0][1] == pd.Timestamp("2017-01-02")
    assert all(e > t for t, e, _, _ in sched)
    assert sched[-1][0] == pd.Timestamp("2017-01-13")    # 2017-01-20 has no next session in the calendar
    monthly = cs.signal_schedule(weeks[1:], sessions, m, "monthly")
    assert [t for t, _, reb, _ in monthly if reb] == [pd.Timestamp("2016-12-30")]


# ---------------------------------------------------------------- point-in-time EPS

def _facts(rows):
    return pd.DataFrame([dict(cik=1, concept="EarningsPerShareDiluted", priority=0, start=s, end=e, val=v, filed=f,
                              form="10-Q", accn="x") for s, e, v, f in rows])


def _quarter_rows():
    """Calendar-year company. Quarterly EPS 2014: 0.50 each; 2015: 0.60; 2016: 0.70, 0.80, 0.90 (Q4 derived);
    FY: 2012 1.00, 2013 1.50, 2014 2.00, 2015 2.40, 2016 3.40."""
    rows = []
    q = {2014: [0.5] * 4, 2015: [0.6] * 4, 2016: [0.7, 0.8, 0.9]}
    ends = ["03-31", "06-30", "09-30"]
    starts = ["01-01", "04-01", "07-01"]
    for y, vals in q.items():
        for i, v in enumerate(vals[:3]):
            rows.append((f"{y}-{starts[i]}", f"{y}-{ends[i]}", v, f"{y}-{['05-05', '08-05', '11-05'][i]}"))
        if len(vals) == 4:
            rows.append((f"{y}-10-01", f"{y}-12-31", vals[3], f"{y + 1}-02-20"))
    for y, v in {2012: 1.0, 2013: 1.5, 2014: 2.0, 2015: 2.4, 2016: 3.4}.items():
        rows.append((f"{y}-01-01", f"{y}-12-31", v, f"{max(y + 1, 2014)}-02-20"))
    rows.append(("2016-01-01", "2016-09-30", 2.4, "2016-11-05"))   # nine-month YTD 2016
    return rows


def test_c_and_a_computation_and_derived_q4():
    s = cs.eps_states(_facts(_quarter_rows()))
    last = s.iloc[-1]
    assert last["filed"] == "2017-02-20"
    assert last["q_end"] == "2016-12-31" and bool(last["q_derived"])
    assert last["q_eps"] == pytest.approx(3.4 - 2.4)                       # FY minus nine-month YTD
    assert last["q_ya_eps"] == pytest.approx(0.6)
    assert last["c_growth"] == pytest.approx(1.0 / 0.6 - 1)
    assert bool(last["a_up3"])                                            # 2.0 > 1.5? 2.4 > 2.0 > 1.5 and 3.4
    assert last["a_cagr3"] == pytest.approx((3.4 / 1.5) ** (1 / 3) - 1)
    q3 = s[s["filed"] == "2016-11-05"].iloc[0]
    assert q3["q_end"] == "2016-09-30" and q3["c_growth"] == pytest.approx(0.9 / 0.6 - 1)


def test_negative_base_gives_no_c_growth():
    rows = [("2015-07-01", "2015-09-30", -0.2, "2015-11-01"), ("2016-07-01", "2016-09-30", 0.5, "2016-11-01")]
    s = cs.eps_states(_facts(rows))
    assert np.isnan(s.iloc[-1]["c_growth"])


def test_point_in_time_no_look_ahead_and_restatement():
    rows = _quarter_rows() + [("2016-07-01", "2016-09-30", 0.45, "2017-03-15")]   # later restatement (split 2:1)
    s = cs.eps_states(_facts(rows))
    before = cs.eps_asof(s, 1, "2016-11-05")        # same day as the Q3 filing: not yet usable
    assert before["q_end"] == "2016-06-30"
    after = cs.eps_asof(s, 1, "2016-11-07")
    assert after["q_end"] == "2016-09-30" and after["q_eps"] == pytest.approx(0.9)
    late = cs.eps_asof(s, 1, "2017-03-20")
    assert late["filed"] == "2017-03-15"
    # attach_eps (as used by the backtest): a week ending on the filing day must not see it
    feat = pd.DataFrame({"week_end": pd.to_datetime(["2016-11-04", "2016-11-05", "2016-11-11"]),
                         "security_id": "A", "cik": "1"})
    out = cs.attach_eps(feat, s)
    assert out["q_end"].tolist() == ["2016-06-30", "2016-06-30", "2016-09-30"]
    assert (pd.to_datetime(out["avail"]) < out["week_end"]).all()


def test_d0_timing_uses_release_when_earlier_than_filing():
    s = cs.eps_states(_facts(_quarter_rows()), d0_by_qend={"2016-09-30": "2016-10-25"})
    row = s[s["q_end"] == "2016-09-30"].iloc[0]
    assert row["avail"] == "2016-10-25" and row["filed"] == "2016-11-05"


# ---------------------------------------------------------------- N and L

def test_new_high_and_relative_strength():
    dates = pd.bdate_range("2015-01-01", periods=300)
    up = pd.Series(np.linspace(10, 40, 300), index=dates)                 # steady riser at its high
    fall = pd.Series(np.r_[np.linspace(10, 40, 250), np.linspace(40, 30, 50)], index=dates)   # 25% off high
    close = pd.DataFrame({"UP": up, "FALL": fall})
    vol = pd.DataFrame(1000.0, index=dates, columns=close.columns)
    vol.iloc[-1, 0] = 2000.0
    idx = close / close.iloc[0]
    pf = cs.price_features(close, vol, idx)
    assert pf["off_high"]["UP"].iloc[-1] == pytest.approx(0.0)
    assert pf["off_high"]["FALL"].iloc[-1] == pytest.approx(0.25)
    assert pf["rs_252_21"]["UP"].iloc[-1] == pytest.approx(up.iloc[-22] / up.iloc[-253] - 1)
    assert pf["vol_ratio5"]["UP"].iloc[-1] == pytest.approx(2.0)


def test_screen_rs_top_and_letters():
    t = pd.Timestamp("2017-03-03")
    n = 10
    feat = pd.DataFrame({
        "week_end": t, "security_id": [f"S{i}" for i in range(n)], "dv50_rank": range(1, n + 1),
        "rs_252_21": np.arange(n, dtype=float), "off_high": 0.05, "vol_ratio5": 2.0,
        "c_growth": 0.30, "q_end": "2016-12-31", "fy0_end": "2016-12-31", "a_up3": True, "a_cagr3": 0.30})
    feat.loc[9, "c_growth"] = 0.10           # best RS fails C
    feat.loc[8, "off_high"] = 0.30           # second best fails N
    cfg = cs.Config(rs_top=0.30)
    out = cs.screen(feat, cfg)
    assert out["security_id"].tolist() == ["S7"]
    out2 = cs.screen(feat, cs.Config(rs_top=0.30, c_min=None, n_within=None))
    assert out2["security_id"].tolist() == ["S9", "S8", "S7"]
    stale = feat.assign(q_end="2016-06-30")
    assert cs.screen(stale, cfg).empty           # quarter older than 200 days fails C


# ---------------------------------------------------------------- engine: stop loss, costs

def _engine_inputs(path):
    sessions = pd.bdate_range("2017-01-02", periods=len(path))
    idx = pd.DataFrame({"X": path}, index=sessions)
    close = idx * 50
    last_row = pd.Series({"X": sessions[-1]})
    qqq = pd.Series(1.0, index=sessions)
    sched = [(pd.Timestamp("2016-12-30"), sessions[0], True, True)]
    return sessions, idx, close, last_row, qqq, sched


def test_stop_loss_sells_at_next_close():
    path = [1.0, 1.0, 0.95, 0.91, 0.80, 0.70]       # -9% on day 3 -> sold at day 4's close (-20%)
    sessions, idx, close, last_row, qqq, sched = _engine_inputs(path)
    cfg = cs.Config(k=1, stop=0.08, m_rule="none")
    res = cs.simulate(cfg, sessions, idx, close, last_row, qqq, qqq * 300, sched, {pd.Timestamp("2016-12-30"): ["X"]},
                      {}, 10_000.0)
    tr = res["trades"]
    assert tr["reason"].tolist() == ["pending_stop"]
    assert tr["exit"].iloc[0] == sessions[4]
    assert tr["ret"].iloc[0] == pytest.approx(-0.20)
    assert res["n_buy"] == 1 and res["n_sell"] == 1
    nav = res["nav"]
    assert nav.iloc[-1] == pytest.approx(nav.iloc[4])          # in cash after the stop
    assert 7_960 < nav.iloc[-1] < 8_000                         # -20% minus costs


def test_no_stop_holds_and_profit_target():
    path = [1.0, 1.1, 1.3, 1.2]
    sessions, idx, close, last_row, qqq, sched = _engine_inputs(path)
    picks = {pd.Timestamp("2016-12-30"): ["X"]}
    res = cs.simulate(cs.Config(k=1, stop=None, m_rule="none"), sessions, idx, close, last_row, qqq, qqq * 300, sched,
                      picks, {}, 10_000.0)
    assert res["n_sell"] == 0
    res = cs.simulate(cs.Config(k=1, stop=0.08, profit=0.25, m_rule="none"), sessions, idx, close, last_row, qqq,
                      qqq * 300, sched, picks, {}, 10_000.0)
    assert res["trades"]["reason"].tolist() == ["pending_profit"] and res["trades"]["exit"].iloc[0] == sessions[3]


def test_cost_minimum_commission():
    from src.research.ibkr_cost_calibration import base_stock_commission_usd
    assert base_stock_commission_usd(10, 100.0, pricing_plan="tiered") == pytest.approx(0.35)
    c = cs.rev.order_cost(10, 100.0, sell=False, hs=0.0)
    assert c["commission"] == pytest.approx(0.35)
    assert c["total"] == pytest.approx(0.35 + 10 * (0.0010 + 0.0002) + 0.35 * cs.rev.PASS_THROUGH_OF_COMMISSION)
    # 1,000 shares at $50: 0.0035 x 1000 = $3.50 > minimum
    assert cs.rev.order_cost(1000, 50.0, sell=False, hs=0.0)["commission"] == pytest.approx(3.5)
    # value-based helper used by the engine
    assert cs.order_cost(1000.0, 100.0, False, 0.0) == pytest.approx(c["total"])
    assert cs.order_cost(0.0, 100.0, False, 0.0) == 0.0
