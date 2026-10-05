"""Offline tests for scripts/research_earnings_events.py (synthetic data only; no cache files are read)."""
import numpy as np
import pandas as pd
import pytest

from scripts import research_earnings_events as ee


def _sessions(start="2015-01-01", end="2017-12-31"):
    return pd.bdate_range(start, end)


def _universe(sessions, sids=("A", "B"), ciks=None):
    ciks = ciks or {s: s for s in sids}
    fridays = [d for d in sessions if d.weekday() == 4]
    rows = [{"week_end": f, "security_id": s, "dv50_rank": float(k + 1), "cik": ciks[s], "ticker": s}
            for f in fridays for k, s in enumerate(sids)]
    return pd.DataFrame(rows)


def _events(rows):
    e = pd.DataFrame(rows, columns=["cik", "security_id", "d0", "acc"])
    e["d0"], e["acc"] = pd.to_datetime(e["d0"]), pd.to_datetime(e["acc"])
    e["late_furnished"] = "N"
    return e


def test_expected_date_is_364_days_later_rolled_to_a_session():
    s = _sessions()
    i = ee.expected_dates(s, pd.Series(pd.to_datetime(["2015-04-23"])))[0]   # a Thursday
    assert s[i] == pd.Timestamp("2016-04-21")                                   # same weekday one year on
    j = ee.expected_dates(s, pd.Series(pd.to_datetime(["2015-04-25"])))[0]     # Saturday + 364 -> next Monday
    assert s[j] == pd.Timestamp("2016-04-25")


def test_premium_candidate_entry_exit_and_point_in_time_checks():
    s = _sessions()
    memb = ee.Membership(_universe(s), s)
    ev = _events([("A", "A", "2015-04-23", "2015-04-23"), ("A", "A", "2016-01-28", "2016-01-28"),
                  ("B", "B", "2015-04-23", "2015-04-23"), ("B", "B", "2016-01-28", "2016-01-28"),
                  # B already reported this year's quarter early and it is public before the entry day
                  ("B", "B", "2016-04-12", "2016-04-12")])
    plan = ee.premium_candidates(ev, s, memb, lead=5, lag=1, first_i=0, last_i=len(s) - 1)
    exp_i = int(np.where(s == pd.Timestamp("2016-04-21"))[0][0])
    assert list(plan) == [exp_i - 5]
    (sid, exit_i, prio, e_i), = plan[exp_i - 5]
    assert sid == "A" and exit_i == exp_i + 1 and e_i == exp_i
    # if B's early report becomes public only after the entry session, B stays a candidate (no look-ahead)
    ev2 = ev.copy()
    ev2.loc[4, "acc"] = pd.Timestamp("2016-04-20")
    ev2.loc[4, "d0"] = pd.Timestamp("2016-04-20")
    plan2 = ee.premium_candidates(ev2, s, memb, 5, 1, 0, len(s) - 1)
    assert [c[0] for c in plan2[exp_i - 5]] == ["A", "B"]                      # ordered by dv50 rank


def test_premium_requires_recent_reporting_and_membership():
    s = _sessions()
    memb = ee.Membership(_universe(s, sids=("A",)), s)
    ev = _events([("A", "A", "2015-04-23", "2015-04-23"),       # no event in the 200 days before the entry
                  ("C", "C", "2015-04-23", "2015-04-23"), ("C", "C", "2016-01-28", "2016-01-28")])   # C not ranked
    assert ee.premium_candidates(ev, s, memb, 5, 1, 0, len(s) - 1) == {}


def test_trailing_threshold_uses_only_earlier_signals():
    d = pd.to_datetime(["2016-01-04"] * 5 + ["2016-01-05"] * 5 + ["2016-01-06"])
    t = pd.DataFrame({"s_date": d, "ear": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 100.0]})
    thr = ee.trailing_threshold(t, q=0.9, window_days=365, min_n=5)
    assert thr.iloc[:5].isna().all()                                   # nothing earlier
    assert thr.iloc[5] == pytest.approx(np.quantile([1, 2, 3, 4, 5], 0.9))
    assert thr.iloc[10] == pytest.approx(np.quantile(range(1, 11), 0.9))   # the 100 itself is not used


def test_ear_and_signal_session():
    s = _sessions("2016-01-01", "2016-03-31")
    memb = ee.Membership(_universe(s, sids=("A",)), s)
    px = pd.DataFrame({"A": np.linspace(100, 120, len(s))}, index=s)
    oneq = pd.Series(np.linspace(50, 51, len(s)), index=s)
    d0 = s[20]
    ev = _events([("A", "A", d0, s[25])])                          # 8-K accepted late: signal waits for it
    t = ee.ear_table(ev, s, px, oneq, memb, None)
    want = (px["A"].iloc[21] / px["A"].iloc[18] - 1) - (oneq.iloc[21] / oneq.iloc[18] - 1)
    assert t.loc[0, "ear"] == pytest.approx(want)
    assert t.loc[0, "s_i"] == 25
    ev = _events([("A", "A", d0, d0)])
    assert ee.ear_table(ev, s, px, oneq, memb, None).loc[0, "s_i"] == 21


def test_dedupe_by_issuer_keeps_first_share_class():
    s = _sessions("2016-01-01", "2016-02-28")
    memb = ee.Membership(_universe(s, sids=("GOOG", "GOOGL", "X"), ciks={"GOOG": "1", "GOOGL": "1", "X": "2"}), s)
    out = ee.dedupe_by_issuer([("GOOGL", 1, 1, 0), ("GOOG", 1, 2, 0), ("X", 1, 3, 0)], memb)
    assert [c[0] for c in out] == ["GOOGL", "X"]


def _flat_inputs(n=30, price=37.0):
    s = _sessions("2016-01-01", "2016-03-31")[:n]
    I = np.ones((n, 1))
    P = np.full((n, 1), price)
    memb = ee.Membership(_universe(s, sids=("A",)), s)
    lvl = np.ones(n) * 1.0
    px = np.ones(n) * 61.0
    last_row = pd.Series({"A": s[-1]})
    return s, I, P, memb, lvl, px, last_row


def test_simulate_whole_shares_oneq_funding_and_costs():
    s, I, P, memb, lvl, px, last_row = _flat_inputs()
    cfg = ee.Config("t", "drift_slot")
    plan = {3: [("A", 8, 0, 0)]}
    res = ee.simulate(plan, cfg, s, np.arange(len(s)), I, P, {"A": 0}, last_row, lvl, px, memb)
    assert res["counts"]["buy"] == 1 and res["counts"]["sell"] == 1
    o = res["orders"]
    buy = o[o["side"] == "buy"].iloc[0]
    assert buy["value"] == pytest.approx(27 * 37.0)                         # floor($1,000 / $37) whole shares
    tr = res["trades"]
    assert tr.iloc[0]["days"] == 5 and tr.iloc[0]["ret"] < 0               # flat prices: only costs
    nav = res["nav"]
    assert nav.iloc[-1] < 10_000 and nav.iloc[-1] > 9_950                  # a few dollars of fees
    assert res["counts"]["oneq_orders"] >= 2                                # initial buy + a sale to fund the stock


def test_simulate_skips_names_above_the_slot_size():
    s, I, P, memb, lvl, px, last_row = _flat_inputs(price=1_500.0)
    res = ee.simulate({3: [("A", 8, 0, 0)]}, ee.Config("t", "drift_slot"), s, np.arange(len(s)), I, P, {"A": 0},
                      last_row, lvl, px, memb)
    assert res["counts"]["buy"] == 0 and res["counts"]["skip_lt_1_share"] == 1


def test_slots_are_capped():
    s, I, P, memb, lvl, px, last_row = _flat_inputs()
    I2, P2 = np.ones((len(s), 3)), np.full((len(s), 3), 20.0)
    cols = {"A": 0, "B": 1, "C": 2}
    lr = pd.Series({k: s[-1] for k in cols})
    cfg = ee.Config("t", "drift_slot", k=2)
    res = ee.simulate({3: [("A", 10, 0, 0), ("B", 10, 1, 0), ("C", 10, 2, 0)]}, cfg, s, np.arange(len(s)), I2, P2,
                      cols, lr, lvl, px, memb)
    assert res["counts"]["buy"] == 2 and res["counts"]["skip_full"] == 1


def test_load_events_drops_acceptance_after_the_end(tmp_path):
    p = tmp_path / "ev.csv"
    pd.DataFrame({"cik": ["1", "1", "1"], "security_id": ["1", "1", "1"],
                  "event_kind": ["results_release", "results_release", "preannouncement"],
                  "first_in_fiscal_quarter": ["Y", "Y", "Y"],
                  "d0_session": ["2018-12-20", "2018-12-28", "2018-11-01"],
                  "d0_session_acceptance": ["2018-12-20", "2019-01-03", "2018-11-01"],
                  "late_furnished": ["N", "Y", "N"]}).to_csv(p, index=False)
    e = ee.load_events("2018-12-31", p)
    assert len(e) == 1 and e.attrs["dropped_after_end"] == 1
