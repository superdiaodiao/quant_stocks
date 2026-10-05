"""Unit tests for scripts/research_sec_alt.py (synthetic data only, no network)."""
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from scripts import research_sec_alt as m

T = pd.Timestamp


def trade(owner, tdate, fdate, code="P", rel="Director", title="Common Stock", shares=1000, price=60.0,
          acc=None, issuer=111, symbol="ABC", ad="A"):
    return {"accession": acc or f"{owner}-{fdate}", "filing_date": T(fdate), "issuer_cik": issuer, "symbol": symbol,
            "owner_cik": owner, "relationship": rel, "title": title, "trans_date": T(tdate), "code": code, "ad": ad,
            "shares": shares, "price": price}


def test_routine_needs_same_month_in_each_of_three_prior_years_known_before_jan1():
    rows = [trade(1, f"{y}-03-10", f"{y}-03-12", code="S") for y in (2010, 2011, 2012)]
    rows += [trade(2, "2010-03-10", "2010-03-12", code="S"), trade(2, "2011-04-10", "2011-04-12", code="S"),
             trade(2, "2012-03-10", "2012-03-12", code="S")]
    # owner 3: the 2012 trade was filed only in 2013 -> not known on 1 Jan 2013
    rows += [trade(3, f"{y}-05-10", f"{y}-05-12") for y in (2010, 2011)] + [trade(3, "2012-05-10", "2013-01-05")]
    r = m.routine_owners(pd.DataFrame(rows))
    assert (1, 2013) in r
    assert (2, 2013) not in r
    assert (3, 2013) not in r


def test_qualified_purchases_filters_and_sums_per_form():
    rows = [trade(1, "2015-01-05", "2015-01-07", acc="a1"), trade(1, "2015-01-06", "2015-01-07", acc="a1"),
            trade(2, "2015-01-05", "2015-01-07", code="S", acc="a2"),
            trade(3, "2015-01-05", "2015-01-07", rel="TenPercentOwner", acc="a3"),
            trade(4, "2015-01-05", "2015-01-07", title="Series A Preferred Stock", acc="a4"),
            trade(5, "2015-01-09", "2015-01-07", acc="a5"),                      # trade after filing: invalid
            trade(6, "2015-01-05", "2015-01-07", rel="Officer", acc="a6", price=0.0)]
    q = m.qualified_purchases(pd.DataFrame(rows), routine={(1, 2014)})
    assert list(q["accession"]) == ["a1"]
    assert q["value"].iloc[0] == pytest.approx(120_000)
    assert not q["routine"].iloc[0]          # routine set is for 2014, the trade is in 2015


def _q(owner, tdate, fdate, value=30_000, issuer=111):
    return {"accession": f"{owner}{fdate}", "owner_cik": owner, "filing_date": T(fdate), "issuer_cik": issuer,
            "symbol": "ABC", "trans_date": T(tdate), "value": value, "routine": False}


def test_cluster_triggers_on_the_third_insiders_filing_only():
    q = pd.DataFrame([_q(1, "2015-03-01", "2015-03-03"), _q(2, "2015-03-10", "2015-03-12"),
                      _q(3, "2015-03-20", "2015-03-24"), _q(4, "2015-03-21", "2015-03-25")])
    c = m.cluster_signals(q)
    assert len(c) == 1                           # the 4th falls in the 30-day quiet period
    assert c["filing_date"].iloc[0] == T("2015-03-24")
    assert c["n_insiders"].iloc[0] == 3


def test_cluster_needs_trades_within_30_days_and_known_filings():
    far = pd.DataFrame([_q(1, "2015-01-01", "2015-01-03"), _q(2, "2015-01-20", "2015-01-22"),
                        _q(3, "2015-02-15", "2015-02-17")])
    assert m.cluster_signals(far).empty          # 45-day spread
    same_owner = pd.DataFrame([_q(1, "2015-03-01", "2015-03-03"), _q(1, "2015-03-05", "2015-03-07"),
                               _q(2, "2015-03-06", "2015-03-08")])
    assert m.cluster_signals(same_owner).empty   # only two distinct insiders
    late = pd.DataFrame([_q(1, "2015-03-01", "2015-03-03"), _q(2, "2015-03-02", "2015-03-04"),
                         _q(3, "2015-03-03", "2015-06-30")])
    c = m.cluster_signals(late)
    assert c["filing_date"].iloc[0] == T("2015-06-30")   # not before the third form was filed


def test_entry_is_the_session_after_the_filing_date():
    s = pd.DatetimeIndex(["2015-01-05", "2015-01-06", "2015-01-07"])
    assert m.entry_session(s, T("2015-01-05")) == T("2015-01-06")
    assert m.entry_session(s, T("2015-01-03")) == T("2015-01-05")
    assert m.entry_session(s, T("2015-01-07")) is None


def test_infotable_parse_and_top10_drop_options_and_bonds():
    xml = b"""<?xml version="1.0"?><informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
    <infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass><cusip>037833100</cusip>
      <value>100</value><shrsOrPrnAmt><sshPrnamt>10</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt></infoTable>
    <infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass><cusip>037833100</cusip>
      <value>50</value><shrsOrPrnAmt><sshPrnamt>5</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt></infoTable>
    <infoTable><nameOfIssuer>BIG PUT</nameOfIssuer><titleOfClass>PUT</titleOfClass><cusip>999999999</cusip>
      <value>1000</value><shrsOrPrnAmt><sshPrnamt>5</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
      <putCall>Put</putCall></infoTable>
    <infoTable><nameOfIssuer>BOND CO</nameOfIssuer><titleOfClass>NOTE</titleOfClass><cusip>888888888</cusip>
      <value>900</value><shrsOrPrnAmt><sshPrnamt>5</sshPrnamt><sshPrnamtType>PRN</sshPrnamtType></shrsOrPrnAmt></infoTable>
    <infoTable><nameOfIssuer>KO</nameOfIssuer><titleOfClass>COM</titleOfClass><cusip>191216100</cusip>
      <value>120</value><shrsOrPrnAmt><sshPrnamt>5</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt></infoTable>
    </informationTable>"""
    t = m.top10(m.parse_infotable(xml))
    assert list(t["cusip"]) == ["037833100", "191216100"]
    assert t["value"].iloc[0] == 150
    assert t["weight_in_top10"].sum() == pytest.approx(1.0)


def test_norm_name():
    assert m.norm_name("Apple Inc.") == m.norm_name("APPLE INC") == "APPLE"
    assert m.norm_name("KRAFT HEINZ CO") == m.norm_name("The Kraft Heinz Company")


def _toy(n_days=120, n_names=12, drift=0.001):
    s = pd.bdate_range("2013-12-31", periods=n_days)
    cols = [f"S{i}" for i in range(n_names)]
    idx = pd.DataFrame(np.cumprod(np.full((n_days, n_names), 1 + drift), axis=0), index=s, columns=cols)
    close = idx * 50
    last_row = pd.Series(pd.NaT, index=cols)
    return SimpleNamespace(sessions=s, perf_idx=idx, close=close, last_row=last_row)


def test_insider_engine_holds_63_sessions_and_caps_at_k():
    d = _toy()
    e = pd.DataFrame({"entry": [d.sessions[1]] * 12, "security_id": [f"S{i}" for i in range(12)],
                      "value": np.arange(12, 0, -1) * 1e5})
    r = m.run_insider(e, d, lambda day, sid: 1.0, k=10, hold=63)
    assert r["skipped_full"] == 2
    t = r["trades"]
    closed = t[t["reason"] == "held_63"]
    assert len(closed) == 10
    assert all(d.sessions.get_loc(x) - d.sessions.get_loc(y) == 63 for x, y in zip(closed["exit"], closed["entry"]))
    assert r["n_positions"].iloc[2] == 10 and r["n_positions"].iloc[70] == 0
    assert r["nav"].iloc[0] == m.ACCOUNT          # nothing traded before the entry session


def test_target_engine_equal_weights_and_sells_leavers():
    d = _toy(n_names=4, drift=0.0)
    r = m.run_targets({d.sessions[0]: ["S0", "S1"], d.sessions[10]: ["S1", "S2"]}, d, lambda day, sid: 1.0)
    t = r["trades"]
    assert set(t.loc[t["reason"] == "left_target", "sid"]) == {"S0"}
    assert r["n_positions"].iloc[5] == 2 and r["n_positions"].iloc[11] == 2
    assert r["nav"].iloc[-1] < m.ACCOUNT          # flat prices: only costs


def test_judge_rules():
    h_good = {"cagr": 0.20, "oneq_cagr": 0.15, "dd_shallower_than_oneq_pp": 0.0}
    h_bad = {"cagr": 0.10, "oneq_cagr": 0.15, "dd_shallower_than_oneq_pp": 0.0}
    assert m.judge([h_good, h_good], {"t_monthly_excess_vs_oneq": 2.1})["A"]
    assert not m.judge([h_good, h_bad], {"t_monthly_excess_vs_oneq": 3.0})["pass"]
    h_safe = {"cagr": 0.13, "oneq_cagr": 0.15, "dd_shallower_than_oneq_pp": 12.0}
    assert m.judge([h_safe, h_safe], {"t_monthly_excess_vs_oneq": -1.0})["B"]


def test_period_metrics_uses_previous_close_as_base():
    s = pd.bdate_range("2013-12-31", "2015-12-31")
    nav = pd.Series(np.linspace(10_000, 12_000, len(s)), index=s)
    rf = pd.Series(0.0, index=s)
    mt = m.period_metrics(nav, nav, nav, rf, "2015-01-01", "2015-12-31")
    base = nav[nav.index < "2015-01-01"].iloc[-1]
    yrs = mt["sessions"] / 252
    assert mt["cagr"] == pytest.approx((nav.iloc[-1] / base) ** (1 / yrs) - 1)
    assert mt["excess_vs_oneq"] == pytest.approx(0.0)
    assert mt["beta_vs_oneq"] == pytest.approx(1.0)
