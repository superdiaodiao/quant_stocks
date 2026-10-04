"""Offline tests for scripts/research_osap_screen.py and scripts/research_osap_backtest.py (synthetic data only)."""
import math

import numpy as np
import pandas as pd
import pytest

from scripts import research_livermore as lv
from scripts import research_osap_backtest as bt
from scripts import research_osap_screen as sc


# ================================================================ screen math

def test_tstat_matches_formula():
    x = pd.Series([1.0, 2.0, 3.0, 4.0])
    m, t, n = sc.tstat(x)
    assert m == 2.5 and n == 4
    assert t == pytest.approx(2.5 / x.std(ddof=1) * 2.0)
    assert math.isnan(sc.tstat(pd.Series([1.0]))[1])


def test_periods_and_window():
    row = pd.Series({"SampleStartYear": 1970, "SampleEndYear": 1990, "Year": 1995})
    p = sc.periods_for(row)
    assert p["is"] == ("1970-01-01", "1990-12-31")
    assert p["postpub"] == ("1996-01-01", sc.DATA_END)
    assert p["post2015"] == ("2015-01-01", sc.DATA_END)
    idx = pd.date_range("2014-11-30", periods=4, freq="ME")
    w = sc.window(pd.Series(range(4), index=idx), *p["post2015"])
    assert list(w.index.strftime("%Y-%m")) == ["2015-01", "2015-02"]


def test_bh_reject():
    p = np.array([0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205, np.nan])
    rej = sc.bh_reject(p, 0.05)
    assert rej.tolist() == [True, True] + [False] * 7
    assert not sc.bh_reject([np.nan, np.nan]).any()


def _ports(sig, values_by_port, start="2014-01-31"):
    idx = pd.date_range(start, periods=len(next(iter(values_by_port.values()))), freq="ME")
    rows = []
    for port, vals in values_by_port.items():
        rows += [{"signalname": sig, "port": port, "date": d, "ret": v} for d, v in zip(idx, vals)]
    return pd.DataFrame(rows)


def test_legs_long_is_top_port_and_ls_identity():
    p = _ports("X", {"01": [1, 2, 3], "02": [2, 2, 2], "03": [4, 4, 4], "LS": [3, 2, 1]})
    leg = sc.legs(p)["X"]
    assert leg["long"].tolist() == [4, 4, 4] and leg["short"].tolist() == [1, 2, 3]
    assert sc.check_ls_identity({"X": leg}) == []
    p2 = _ports("Y", {"01": [1, 1], "05": [2, 2], "LS": [5, 5]})
    assert sc.check_ls_identity(sc.legs(p2)) == ["Y"]


def test_screen_hurdle_flags():
    n = 120
    idx = pd.date_range("2015-01-31", periods=n, freq="ME")
    rng = np.random.default_rng(0)
    mkt = pd.Series(1.0, index=idx)
    doc = pd.DataFrame({"Authors": ["a", "b"], "Year": [2000, 2000], "Journal": ["J", "J"],
                        "SampleStartYear": [1970, 1970], "SampleEndYear": [1990, 1990], "Cat.Data": ["Price"] * 2,
                        "Cat.Economic": ["x"] * 2, "LongDescription": ["good", "bad"], "Stock Weight": ["EW"] * 2,
                        "Portfolio Period": [1, 1], "Return": [1, 1], "T-Stat": [3, 3]}, index=["GOOD", "BAD"])
    noise = rng.normal(0, 1, n)
    good = pd.DataFrame({"long": 1.0 + 0.8 + noise, "short": 1.0 + noise * 0.5, "allavg": 1.0 + noise * 0.7}, index=idx)
    bad = pd.DataFrame({"long": 1.0 - 0.1 + noise, "short": 1.0 + noise, "allavg": 1.0 + noise}, index=idx)
    for f in (good, bad):
        f["ls"] = f["long"] - f["short"]
    legs = {"GOOD": good, "BAD": bad}
    df = sc.screen(doc, legs, legs, legs, mkt).set_index("signal")
    assert df.loc["GOOD", "lc_long_x_post2015_t"] >= 3 and bool(df.loc["GOOD", "pass_hurdle"])
    assert bool(df.loc["GOOD", "still_works"])
    assert not bool(df.loc["BAD", "pass_hurdle"]) and not bool(df.loc["BAD", "still_works"])
    assert df.loc["GOOD", "lc_long_x_post2015_n"] == n


def test_load_ports_rejects_data_after_end(tmp_path):
    f = tmp_path / "p.csv"
    pd.DataFrame({"signalname": ["X"], "port": ["01"], "date": ["2025-01-31"], "ret": [1.0]}).to_csv(f, index=False)
    with pytest.raises(AssertionError):
        sc.load_ports(f)


def test_load_market_parses_monthly_block(tmp_path):
    (tmp_path / "F-F_Research_Data_Factors.csv").write_text(
        "header\n\n,Mkt-RF,SMB,HML,RF\n201501,  -3.00, 0, 0,  0.10\n201502,   6.00, 0, 0, 0.00\n\n"
        " Annual Factors: January-December \n,Mkt-RF,SMB,HML,RF\n2015,  1.0, 0, 0, 0.0\n")
    m = sc.load_market(tmp_path)
    assert m.tolist() == pytest.approx([-2.9, 6.0])
    assert list(m.index.strftime("%Y-%m-%d")) == ["2015-01-31", "2015-02-28"]


# ================================================================ point-in-time fundamentals

def _facts(rows):
    return pd.DataFrame(rows, columns=["cik", "group", "concept", "prio", "end", "val", "filed"])


BASE = [
    (1, "rev", "Revenues", 0, "2018-12-31", 100.0, "2019-02-20"),
    (1, "cogs", "CostOfRevenue", 0, "2018-12-31", 60.0, "2019-02-20"),
    (1, "assets", "Assets", 0, "2018-12-31", 200.0, "2019-02-20"),
    (1, "opinc", "OperatingIncomeLoss", 0, "2018-12-31", 10.0, "2019-02-20"),
    (1, "da", "DepreciationDepletionAndAmortization", 0, "2018-12-31", 4.0, "2019-02-20"),
    (1, "rd", "ResearchAndDevelopmentExpense", 0, "2018-12-31", 6.0, "2019-02-20"),
]


def test_signal_values_and_restatement_from_filing_date():
    rows = BASE + [
        (1, "rev", "Revenues", 0, "2019-12-31", 120.0, "2020-02-25"),
        (1, "gross", "GrossProfit", 0, "2019-12-31", 66.0, "2020-02-25"),
        (1, "assets", "Assets", 0, "2019-12-30", 220.0, "2020-02-25"),        # instant within 7 days
        (1, "opinc", "OperatingIncomeLoss", 0, "2019-12-31", 11.0, "2020-02-25"),
        (1, "gross", "GrossProfit", 0, "2019-12-31", 55.0, "2021-03-01"),       # restated a year later
    ]
    st = bt.fundamental_states(_facts(rows))
    assert st["filed"].tolist() == ["2019-02-20", "2020-02-25", "2021-03-01"]
    first = st.iloc[0]
    assert first["gp"] == pytest.approx(40 / 200) and first["opr"] == pytest.approx(20 / 200)
    second = st.iloc[1]
    assert second["gp_fy_end"] == "2019-12-31" and second["gp"] == pytest.approx(66 / 220)
    assert second["opr"] == pytest.approx(11 / 220)          # D&A and R&D missing for 2019 -> 0
    assert st.iloc[2]["gp"] == pytest.approx(55 / 220)

    req = pd.DataFrame({"t": pd.to_datetime(["2019-02-20", "2019-02-22", "2020-02-25", "2020-02-28", "2021-03-05"]),
                        "cik": ["1"] * 5, "security_id": ["s"] * 5})
    m = bt.asof_states(req, st, "2026-08-31").set_index("t")
    assert np.isnan(m.loc["2019-02-20", "gp"])                # filed that very day: not usable yet
    assert m.loc["2019-02-22", "gp"] == pytest.approx(0.2)
    assert m.loc["2020-02-25", "gp"] == pytest.approx(0.2)    # new 10-K filed on t: still the old value
    assert m.loc["2020-02-28", "gp"] == pytest.approx(0.3)
    assert m.loc["2021-03-05", "gp"] == pytest.approx(0.25)   # restatement only after its own filing


def test_date_guard_drops_facts_filed_after_period_end():
    rows = BASE + [(1, "rev", "Revenues", 0, "2019-12-31", 120.0, "2020-02-25"),
                   (1, "gross", "GrossProfit", 0, "2019-12-31", 90.0, "2020-02-25"),
                   (1, "assets", "Assets", 0, "2019-12-31", 300.0, "2020-02-25")]
    st = bt.fundamental_states(_facts(rows))
    req = pd.DataFrame({"t": pd.to_datetime(["2020-03-31"]), "cik": ["1"], "security_id": ["s"]})
    m = bt.asof_states(req, st, "2019-12-31")
    assert m["gp"].iloc[0] == pytest.approx(0.2)              # the 2020 filing is invisible in a 2019 window
    assert m["filed"].iloc[0] == "2019-02-20"


def test_stale_and_bad_values_are_dropped():
    st = bt.fundamental_states(_facts(BASE))
    req = pd.DataFrame({"t": pd.to_datetime(["2020-07-15"]), "cik": ["1"], "security_id": ["s"]})
    m = bt.asof_states(req, st, "2026-08-31")                 # fiscal year ended > 550 days earlier
    assert np.isnan(m["gp"].iloc[0]) and np.isnan(m["opr"].iloc[0])
    bad = [r for r in BASE if r[1] != "cogs"] + [(1, "gross", "GrossProfit", 0, "2018-12-31", 150.0, "2019-02-20"),
                                                 (1, "da", "DepreciationAndAmortization", 1, "2018-12-31", 9e9, "2019-02-20")]
    v = bt.fundamental_states(_facts(bad)).iloc[0]
    assert np.isnan(v["gp"])                                   # gross profit above revenue
    assert v["opr"] == pytest.approx(20 / 200)                 # priority: DepreciationDepletionAndAmortization wins
    huge = [r if r[1] != "da" else (1, "da", r[2], 0, r[4], 5000.0, r[6]) for r in BASE]
    assert np.isnan(bt.fundamental_states(_facts(huge)).iloc[0]["opr"])   # numerator above revenue


def test_concept_priority_revenue():
    rows = BASE + [(1, "rev", "SalesRevenueNet", 3, "2018-12-31", 999.0, "2019-02-20")]
    assert bt.fundamental_states(_facts(rows)).iloc[0]["gp"] == pytest.approx(40 / 200)


# ================================================================ schedule, engine, freeze

def test_schedule_maps_first_signal_to_period_start_and_guards_end():
    sessions = pd.bdate_range("2013-12-20", "2014-03-05")
    ts = pd.to_datetime(["2013-11-29", "2013-12-27", "2014-01-31", "2014-02-28"])
    s = bt.schedule(ts, sessions, pd.Timestamp("2014-01-02"), pd.Timestamp("2014-03-03"))
    assert s[pd.Timestamp("2014-01-02")] == pd.Timestamp("2013-12-27")
    assert s[pd.Timestamp("2014-02-03")] == pd.Timestamp("2014-01-31")
    assert s[pd.Timestamp("2014-03-03")] == pd.Timestamp("2014-02-28")
    s2 = bt.schedule(ts, sessions, pd.Timestamp("2014-01-02"), pd.Timestamp("2014-02-27"))
    assert max(s2) <= pd.Timestamp("2014-02-27")


def _win(n_stocks=12, days=70):
    sessions = pd.bdate_range("2014-01-02", periods=days)
    cols = [f"s{i}" for i in range(n_stocks)]
    growth = np.array([1.001] * n_stocks)
    idx = pd.DataFrame(np.cumprod(np.tile(growth, (days, 1)), axis=0), index=sessions, columns=cols)
    idx.iloc[0] = 1.0
    close = idx * 50
    last = pd.Series(sessions[-1], index=cols)
    spec = {"perf_start": "2014-01-01", "effective_end": str(sessions[-1].date()), "price_start": "2014-01-01"}
    return lv.WinData(name="t", spec=spec, sessions=sessions, universe=pd.DataFrame(), sig_idx=idx, perf_idx=idx,
                      close=close, close_adj=close, vol_adj=close, last_row=last, qqq_close=close["s0"],
                      qqq_perf_idx=idx["s0"], terminal_events=pd.DataFrame(),
                      guard={"perf_start_session": str(sessions[0].date())})


def test_engine_buys_top10_keeps_buffer_and_sells_outside_top20():
    w = _win(n_stocks=30)
    s = w.sessions
    order1 = [f"s{i}" for i in range(30)]
    order2 = [f"s{i}" for i in range(10, 30)] + [f"s{i}" for i in range(10)]   # s0-s9 fall to ranks 21-30
    order3 = [f"s{i}" for i in range(10, 20)] + [f"s{i}" for i in range(10)] + [f"s{i}" for i in range(20, 30)]
    # holdings s0-s9 sit at ranks 11-20 in order3: all kept, nothing bought
    picks = {pd.Timestamp("2013-12-31"): [(x, 1) for x in order1],
             pd.Timestamp("2014-01-31"): [(x, 1) for x in order3],
             pd.Timestamp("2014-02-28"): [(x, 1) for x in order2]}
    sched = {s[0]: pd.Timestamp("2013-12-31"), s[22]: pd.Timestamp("2014-01-31"), s[42]: pd.Timestamp("2014-02-28")}
    res = bt.simulate(w, sched, picks)
    assert res["n_buy"] == 10 + 10 and res["n_sell"] == 10
    tr = res["trades"]
    sold = set(tr.loc[tr["reason"] == "out_of_top20", "sid"])
    assert sold == {f"s{i}" for i in range(10)}
    held = set(tr.loc[tr["reason"] == "held_at_end", "sid"])
    assert held == {f"s{i}" for i in range(10, 20)}
    assert 0.97 < res["exposure"].iloc[-1] <= 1.0
    assert res["cost"].sum() > 0


def test_engine_rejects_sessions_outside_window():
    w = _win()
    w.spec["effective_end"] = str(w.sessions[10].date())
    res = bt.simulate(w, {w.sessions[0]: pd.Timestamp("2013-12-31")},
                      {pd.Timestamp("2013-12-31"): [(f"s{i}", 1) for i in range(12)]})
    assert res["dates"].max() == w.sessions[10]


def test_prereg_hash_detects_edits():
    text = "intro\n<!-- PREREG-BEGIN -->\nrule A\n<!-- PREREG-END -->\nresults later\n"
    h = bt.prereg_hash(text)
    assert h == bt.prereg_hash(text.replace("results later", "results changed"))
    assert h != bt.prereg_hash(text.replace("rule A", "rule B"))


def test_pass_rule():
    assert bt.passes({"cagr": 0.20, "bench_cagr": 0.15, "t_excess_monthly": 2.1})
    assert not bt.passes({"cagr": 0.20, "bench_cagr": 0.15, "t_excess_monthly": 1.9})
    assert not bt.passes({"cagr": 0.10, "bench_cagr": 0.15, "t_excess_monthly": 3.0})
