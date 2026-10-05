"""Offline tests for scripts/research_megacap.py (synthetic data only; no cache files are read)."""
import numpy as np
import pandas as pd
import pytest

from scripts import research_megacap as mc


def _sessions(start="2020-01-01", n=300):
    return pd.bdate_range(start, periods=n)


# ---------------------------------------------------------------- calendar

def test_signal_sessions_are_month_ends_and_skip_incomplete_last_month():
    s = pd.bdate_range("2020-01-01", "2020-04-15")
    sig = mc.signal_sessions(s)
    assert sig == [pd.Timestamp("2020-01-31"), pd.Timestamp("2020-02-28"), pd.Timestamp("2020-03-31")]
    s2 = pd.bdate_range("2020-01-01", "2020-03-31")
    # the last loaded session is a month end but nothing follows it to trade on: not a signal
    assert mc.signal_sessions(s2)[-1] == pd.Timestamp("2020-02-28")


# ---------------------------------------------------------------- point-in-time facts

def test_latest_fact_uses_only_filings_strictly_before_the_signal():
    facts = pd.DataFrame({"cik": [1, 1, 1], "pri": [0, 0, 0],
                          "end": ["2019-09-30", "2019-12-31", "2020-03-31"],
                          "filed": ["2019-11-01", "2020-02-01", "2020-05-01"],
                          "val": [100.0, 110.0, 120.0], "start": ["", "", ""]})
    q = pd.DataFrame({"qid": [0, 1, 2], "cik": [1, 1, 1],
                      "s": pd.to_datetime(["2020-01-31", "2020-05-01", "2020-05-29"])})
    got = mc.latest_fact_asof(q, facts).set_index("qid")["val"]
    assert got[0] == 100.0                 # the 2020-02-01 filing is not yet public on 2020-01-31
    assert got[1] == 110.0                 # filed on the signal day itself: not used (strictly before)
    assert got[2] == 120.0


def test_latest_fact_prefers_latest_period_and_drops_stale_filings():
    facts = pd.DataFrame({"cik": [1, 1], "pri": [0, 0], "end": ["2019-12-31", "2018-12-31"],
                          "filed": ["2020-02-01", "2020-03-01"], "val": [110.0, 90.0], "start": ["", ""]})
    q = pd.DataFrame({"qid": [0, 1], "cik": [1, 1], "s": pd.to_datetime(["2020-04-30", "2021-06-30"])})
    got = mc.latest_fact_asof(q, facts).set_index("qid")["val"]
    assert got[0] == 110.0                 # a later re-report of an older period does not replace it
    assert 1 not in got.index              # > 400 days after the filing: too stale


# ---------------------------------------------------------------- market caps

def _price_frames(sessions, cols, prices):
    close = pd.DataFrame(prices, index=sessions, columns=cols)
    idx = close / close.iloc[0]
    return close, idx


def test_market_cap_shares_times_price_and_split_after_filing():
    s = _sessions(n=120)
    # A: $100 then a 2:1 split on day 60 (raw close halves, total return unchanged)
    raw = np.where(np.arange(120) < 60, 100.0, 50.0)
    close = pd.DataFrame({"A": raw}, index=s)
    idx = pd.DataFrame({"A": np.ones(120)}, index=s)
    facts = pd.DataFrame({"cik": [1], "pri": [0], "end": [str(s[5].date())], "filed": [str(s[10].date())],
                          "val": [1e9], "start": [""]})
    cand = pd.DataFrame({"s": [s[30], s[90]], "security_id": ["A", "A"], "cik": pd.array([1, 1], dtype="Int64"),
                         "dv50_rank": [1.0, 1.0]})
    dv = pd.DataFrame({"A": np.full(120, 1e9)}, index=s)
    out = mc.market_caps(cand, facts, pd.DataFrame(columns=["security_id", "as_of", "market_cap"]), close, idx, dv)
    assert out["mcap"].tolist() == pytest.approx([1e11, 1e11])     # the split does not halve the market cap
    assert set(out["mcap_src"]) == {"sec_shares"}


def test_market_cap_rejects_adr_like_share_counts_and_falls_back_to_float():
    s = _sessions(n=60)
    close = pd.DataFrame({"A": np.full(60, 100.0)}, index=s)
    idx = pd.DataFrame({"A": np.ones(60)}, index=s)
    facts = pd.DataFrame({"cik": [1, 1], "pri": [0, 9], "end": [str(s[1].date())] * 2,
                          "filed": [str(s[2].date())] * 2, "val": [1.3e9, 1e10], "start": ["", ""]})
    cand = pd.DataFrame({"s": [s[40]], "security_id": ["A"], "cik": pd.array([1], dtype="Int64"), "dv50_rank": [1.0]})
    dv = pd.DataFrame({"A": np.full(60, 1e8)}, index=s)
    out = mc.market_caps(cand, facts, pd.DataFrame(columns=["security_id", "as_of", "market_cap"]), close, idx, dv)
    # shares x price = 130bn > 2.5 x float 10bn -> the float value is used
    assert out["mcap"].iloc[0] == pytest.approx(1e10)
    assert out["mcap_src"].iloc[0] == "public_float_shares_rejected"


def test_market_cap_dollar_volume_fallback_on_the_market_cap_scale():
    s = _sessions(n=60)
    close = pd.DataFrame({"A": np.full(60, 10.0), "B": np.full(60, 10.0)}, index=s)
    idx = close / 10.0
    facts = pd.DataFrame({"cik": [1], "pri": [0], "end": [str(s[1].date())], "filed": [str(s[2].date())],
                          "val": [1e9], "start": [""]})
    cand = pd.DataFrame({"s": [s[40], s[40]], "security_id": ["A", "B"], "cik": pd.array([1, 2], dtype="Int64"),
                         "dv50_rank": [1.0, 2.0]})
    dv = pd.DataFrame({"A": np.full(60, 1e8), "B": np.full(60, 3e8)}, index=s)
    out = mc.market_caps(cand, facts, pd.DataFrame(columns=["security_id", "as_of", "market_cap"]), close, idx, dv)
    a, b = out.set_index("security_id").loc["A"], out.set_index("security_id").loc["B"]
    assert a["mcap"] == pytest.approx(1e10) and a["mcap_src"] == "sec_shares"
    assert b["mcap_src"] == "dollar_volume" and b["mcap"] == pytest.approx(3e8 * 100)    # ratio 1e10 / 1e8


def test_float_dated_before_the_price_series_is_not_used():
    s = _sessions(n=60)
    close = pd.DataFrame({"A": [np.nan] * 20 + [100.0] * 40}, index=s)
    idx = close / 100.0
    facts = pd.DataFrame({"cik": [1], "pri": [9], "end": [str(s[1].date())], "filed": [str(s[2].date())],
                          "val": [1e10], "start": [""]})
    cand = pd.DataFrame({"s": [s[40]], "security_id": ["A"], "cik": pd.array([1], dtype="Int64"), "dv50_rank": [1.0]})
    dv = pd.DataFrame({"A": np.full(60, np.nan)}, index=s)
    out = mc.market_caps(cand, facts, pd.DataFrame(columns=["security_id", "as_of", "market_cap"]), close, idx, dv)
    assert np.isnan(out["mcap"].iloc[0])


# ---------------------------------------------------------------- candidates (no future universe week)

def test_candidates_use_latest_past_week_and_dedupe_share_classes():
    s = pd.bdate_range("2020-01-01", "2020-03-31")
    close = pd.DataFrame(1.0, index=s, columns=["A", "B", "C", "D"])
    uni = pd.DataFrame({"week_end": pd.to_datetime(["2020-01-24", "2020-01-24", "2020-01-24", "2020-02-07"]),
                        "security_id": ["A", "B", "C", "D"], "ticker": ["A", "B", "C", "D"],
                        "dv50_rank": [1.0, 2.0, 3.0, 4.0], "cik": ["1", "1", "3", "4"],
                        "multi_class_group": ["1", "1", np.nan, np.nan]})
    last_row = pd.Series({"A": s[-1], "B": s[-1], "C": pd.Timestamp("2020-01-15"), "D": s[-1]})
    c = mc.candidates(uni, [pd.Timestamp("2020-01-31")], close, last_row)
    assert c["security_id"].tolist() == ["A"]          # B: same company as A (worse rank); C: ended; D: future week
    assert (c["universe_week"] <= c["s"]).all()


# ---------------------------------------------------------------- targets

def _ranked(s, mcaps: dict):
    return pd.DataFrame({"s": [s] * len(mcaps), "security_id": list(mcaps), "dv50_rank": range(1, len(mcaps) + 1),
                         "mcap": list(mcaps.values()), "mcap_src": "sec_shares"})


def test_top_n_equal_and_cap_weight():
    sess = _sessions(n=300)
    s = sess[260]
    ranked = _ranked(s, {"A": 50.0, "B": 30.0, "C": 20.0, "D": 10.0})
    q = pd.Series(np.linspace(100, 200, 300), index=sess)
    eq = mc.build_targets(mc.Rule("t", 2, 2), ranked, {}, q)[s]
    assert [x[0] for x in eq] == ["A", "B"] and [x[1] for x in eq] == [0.5, 0.5]
    cw = mc.build_targets(mc.Rule("t", 3, 3, capw=True), ranked, {}, q)[s]
    assert [round(x[1], 3) for x in cw] == [0.5, 0.3, 0.2]


def test_momentum_pick_and_no_look_ahead():
    sess = _sessions(n=300)
    s = sess[260]
    idx = pd.DataFrame({"A": np.linspace(1, 1.1, 300), "B": np.linspace(1, 2.0, 300), "C": np.linspace(1, 1.5, 300)},
                       index=sess)
    ranked = _ranked(s, {"A": 50.0, "B": 30.0, "C": 20.0})
    q = pd.Series(np.linspace(100, 200, 300), index=sess)
    rule = mc.Rule("t", 3, 1, "m6")
    t1 = mc.build_targets(rule, ranked, mc.momentum_frames(idx), q)[s]
    assert t1[0][0] == "B"
    idx2 = idx.copy()
    idx2.loc[idx2.index > s, "A"] = 100.0               # A explodes only after the signal
    t2 = mc.build_targets(rule, ranked, mc.momentum_frames(idx2), q)[s]
    assert t2 == t1
    m = mc.momentum_frames(idx)["m12_1"]
    assert m.loc[sess[260], "B"] == pytest.approx(idx.loc[sess[239], "B"] / idx.loc[sess[8], "B"] - 1)


def test_trend_filter_holds_qqq_below_sma200():
    sess = _sessions(n=300)
    s = sess[260]
    ranked = _ranked(s, {"A": 50.0, "B": 30.0})
    idx = pd.DataFrame({"A": np.linspace(1, 2, 300), "B": np.linspace(1, 1.5, 300)}, index=sess)
    falling = pd.Series(np.linspace(200, 100, 300), index=sess)
    rising = pd.Series(np.linspace(100, 200, 300), index=sess)
    rule = mc.Rule("t", 2, 1, "m6", trend=True)
    off = mc.build_targets(rule, ranked, mc.momentum_frames(idx), falling)[s]
    assert len(off) == 1 and off[0][0] == "QQQ" and off[0][1] == 1.0
    assert mc.build_targets(rule, ranked, mc.momentum_frames(idx), rising)[s][0][0] == "A"


# ---------------------------------------------------------------- engine

def _engine_inputs(n=60):
    sess = _sessions(n=n)
    idx = pd.DataFrame({"A": np.ones(n), "B": np.ones(n)}, index=sess)
    close = pd.DataFrame({"A": np.full(n, 100.0), "B": np.full(n, 100.0)}, index=sess)
    q = pd.Series(np.ones(n), index=sess)
    qc = pd.Series(np.full(n, 300.0), index=sess)
    last = pd.Series({"A": sess[-1], "B": sess[-1]})
    return sess, idx, close, q, qc, last


def test_trades_at_the_next_session_close():
    sess, idx, close, q, qc, last = _engine_inputs()
    idx.loc[sess[11]:, "A"] = 2.0                        # A doubles at the close of the execution session
    tg = {sess[10]: [("A", 1.0, 1.0, 1.0, "sec_shares")]}
    sim = mc.simulate(tg, sess, idx, close, last, q, qc)
    assert sim["start"] == sess[11]
    assert sim["nav"].iloc[0] < 10_000                   # bought at the close of sess[11] (after the jump): costs only
    assert sim["orders"].iloc[0] == 1


def test_equal_weight_rebalance_band_and_delisting():
    sess, idx, close, q, qc, last = _engine_inputs()
    tg = {sess[5]: [("A", 0.5, 1.0, 1.0, "x"), ("B", 0.5, 2.0, 1.0, "x")],
          sess[25]: [("A", 0.5, 1.0, 1.0, "x"), ("B", 0.5, 2.0, 1.0, "x")]}
    idx.loc[sess[10]:, "A"] = 1.1                        # small drift: within the 25% band -> no trade
    sim = mc.simulate(tg, sess, idx, close, last, q, qc)
    assert sim["orders"].loc[sess[26]] == 0
    sim0 = mc.simulate(tg, sess, idx, close, last, q, qc, band=0.0)
    assert sim0["orders"].loc[sess[26]] == 2             # band 0: both resized back to 50/50
    # delisting: B's series ends; its value goes to cash and stays (no rebalance)
    last2 = last.copy()
    last2["B"] = sess[30]
    idx2 = idx.copy()
    idx2.loc[sess[31]:, "B"] = 0.5                       # terminal value booked on the next session
    sim2 = mc.simulate({sess[5]: tg[sess[5]]}, sess, idx2, close, last2, q, qc)
    assert sim2["names"].iloc[-1] == 1
    assert sim2["nav"].iloc[-1] == pytest.approx(sim2["nav"].loc[sess[31]])


def test_switch_to_qqq_sells_stocks():
    sess, idx, close, q, qc, last = _engine_inputs()
    tg = {sess[5]: [("A", 1.0, 1.0, 1.0, "x")], sess[20]: [("QQQ", 1.0, np.nan, np.nan, "qqq")]}
    sim = mc.simulate(tg, sess, idx, close, last, q, qc)
    assert sim["orders"].loc[sess[21]] == 2 and sim["names"].iloc[-1] == 1
    assert sim["cost"].sum() > 0


# ---------------------------------------------------------------- metrics and criteria

def test_longest_drawdown_and_window_rebasing():
    d = pd.to_datetime(["2020-01-01", "2020-01-11", "2020-01-21", "2020-02-10", "2020-03-01"])
    v = pd.Series([100, 90, 95, 101, 99.0], index=d)
    assert mc.longest_drawdown_days(v) == 40              # 01-01 -> 02-10
    sess = pd.bdate_range("2019-12-02", "2020-12-31")
    nav = pd.Series(np.linspace(1, 2, len(sess)), index=sess) * 10_000
    flat = pd.Series(10_000.0, index=sess)
    m = mc.window_metrics(nav, flat, flat, "2020-01-01", "2020-12-31")
    assert m["start"] >= "2020-01-01" and m["oneq_cagr"] == pytest.approx(0.0)
    assert m["cagr"] > 0 and m["max_dd"] == pytest.approx(0.0)


def test_criteria():
    base = {"cagr": 0.12, "oneq_cagr": 0.10, "t_monthly_excess_vs_oneq": 2.1, "dd_shallower_than_oneq_pp": 0.0}
    per = {p: dict(base) for p in mc.PERIODS}
    assert mc.criteria(per)["A_cagr_above_oneq_both_halves_and_full_t_ge_2"]
    per[mc.FULL]["t_monthly_excess_vs_oneq"] = 1.9
    assert not mc.criteria(per)["pass"]
    per = {p: {"cagr": 0.08, "oneq_cagr": 0.10, "t_monthly_excess_vs_oneq": -1.0, "dd_shallower_than_oneq_pp": 12.0}
           for p in mc.PERIODS}
    assert mc.criteria(per)["B_dd_10pp_shallower_and_cagr_within_3pp_both_halves"]
    per["H2 2020-2026-08"]["cagr"] = 0.06
    assert not mc.criteria(per)["pass"]
