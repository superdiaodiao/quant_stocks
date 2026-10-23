"""Tests for the buffett study (synthetic data only): 13F parsing (plain-text and XML tables, cover pages), the
"public at the filing date" holding states and their trade timing (no look-ahead), the CUSIP map and the 13F price
check, the B1 target weights and half-spreads, the B2 screen definitions and their point-in-time merge, the FKP
replication formulas and the margin-loan ETF engine."""
from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

from quant.backtest.margin_weights import simulate_margin_weights
from quant.backtest.rebalance import simulate_index_units
from quant.data import berkshire_13f as b13
from quant.data import berkshire_cusips as bc
from quant.data import buffett_prices as bp
from quant.strategies import buffett as bf

# ======================================================================== 13F parsing

TEXT_13F = """<SEC-DOCUMENT>
Report for the Calendar Year or Quarter Ended:  March 31, 2002

Check here if Amendment [ ]; Amendment Number: ______
     This Amendment (Check only one.):    [ ]  is a restatement.
                                          [ ]  adds new holdings entries.

Form 13F Information Table Entry Total:                6
Form 13F Information Table Value Total:           $1,710,104

<TABLE>
<CAPTION>
                        Column 2  Column 3       Market      Shares or
Name of Issuer           Class     CUSIP Number   Value       Amount    Sole   Managers
<S>                      <C>     <C>             <C>         <C>
American Express Co.     Com     025816 10 9       676,097    5,741,800           X              4, 5, 14     5,741,800
                                                   313,789    2,664,878           X              4, 10, 14,
                                                                                                 16, 17       2,664,878
Federal
Home Ln
Mtg Corp.     Com   313400 30 1   1,233
                                  ========
Wells Fargo                Com   949746101   12,000    300,000      X     4, 11          300,000
  & Co                                        7,000    175,000      X     4, 12          175,000
Wellpoint Inc.     Com      949773V 10 7      1,018    979,700        X             4, 9, 10, 11, 13, 16     979,700
Shaw Comm.  Cl B    82028k 20 0   $  700,200     4,281,600           X    4, 8, 9, 11,
                                                                          14, 15        4,281,600
                         GRAND TOTAL           $1,710,104
</TABLE>
"""


def test_text_table_rows_continuations_and_totals():
    cover, h = b13.parse_filing(TEXT_13F)
    assert not cover["is_xml"]
    assert cover["amendment_type"] is None
    assert cover["entry_total"] == 6 and cover["value_total"] == 1_710_104
    # the Freddie Mac line has no share amount: not a row; the manager-continuation lines are not rows either
    assert list(h["cusip"]) == ["025816109", "025816109", "949746101", "949746101", "949773V107", "82028K200"]
    assert list(h["value"]) == [676_097, 313_789, 12_000, 7_000, 1_018, 700_200]
    assert list(h["shares"]) == [5_741_800, 2_664_878, 300_000, 175_000, 979_700, 4_281_600]
    assert h["value"].sum() == cover["value_total"]
    assert b13.check_totals(cover) == {"rows_ok": True, "value_ok": True}
    assert (h["put_call"] == "").all() and (h["sh_prn"] == "SH").all()


def test_cover_amendment_types():
    add = TEXT_13F.replace("[ ]  adds new", "[X]  adds new")
    assert b13.parse_cover(add)["amendment_type"] == "new_holdings"
    rest = TEXT_13F.replace("[ ]  is a restatement", "[X]  is a restatement")
    assert b13.parse_cover(rest)["amendment_type"] == "restatement"
    underscore = TEXT_13F.replace("[ ]  is a restatement", "[_]  is a restatement").replace(
        "[ ]  adds new", "[X]  adds new")
    assert b13.parse_cover(underscore)["amendment_type"] == "new_holdings"


XML_13F = """<SEC-DOCUMENT><XML><edgarSubmission xmlns="http://www.sec.gov/edgar/thirteenffiler">
<coverPage><amendmentInfo><amendmentType>RESTATEMENT</amendmentType></amendmentInfo></coverPage>
<summaryPage><tableEntryTotal>3</tableEntryTotal><tableValueTotal>1600</tableValueTotal></summaryPage>
</edgarSubmission></XML>
<XML><informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
<infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass><cusip>037833100</cusip>
<value>1000</value><shrsOrPrnAmt><sshPrnamt>10</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt></infoTable>
<infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass><cusip>037833100</cusip>
<value>500</value><shrsOrPrnAmt><sshPrnamt>5</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
<putCall>Put</putCall></infoTable>
<infoTable><nameOfIssuer>SOME BOND</nameOfIssuer><titleOfClass>NOTE</titleOfClass><cusip>123456AB1</cusip>
<value>100</value><shrsOrPrnAmt><sshPrnamt>100000</sshPrnamt><sshPrnamtType>PRN</sshPrnamtType></shrsOrPrnAmt></infoTable>
</informationTable></XML></SEC-DOCUMENT>"""


def test_xml_table_and_equity_positions_drop_options_and_bonds():
    cover, h = b13.parse_filing(XML_13F)
    assert cover["is_xml"] and cover["amendment_type"] == "restatement"
    assert cover["entry_total"] == 3 and cover["value_parsed"] == 1600
    p = b13.equity_positions(h)
    assert list(p["cusip"]) == ["037833100"] and p["value"].iloc[0] == 1000 and p["shares"].iloc[0] == 10


# ======================================================================== holding states and timing

def P(c: str) -> str:
    """A 9-character test CUSIP from a short code (real CUSIPs pass through)."""
    return c if len(c) >= 9 else (c + "000000000")[:9]


def _rows(pairs):
    return pd.DataFrame([{"cusip": P(c), "name": c, "title": "COM", "value": float(v), "shares": 1.0, "sh_prn": "SH",
                          "put_call": ""} for c, v in pairs], columns=b13.HOLDING_COLS)


def _filings():
    f = pd.DataFrame([
        ("A1", "13F-HR", "2010-02-16", "2009-12-31", None),
        ("A2", "13F-HR/A", "2010-03-01", "2009-12-31", "new_holdings"),       # adds X to the current state
        ("A3", "13F-HR/A", "2010-03-05", "2009-09-30", "new_holdings"),       # older period: ignored
        ("A4", "13F-HR", "2010-03-10", "2009-09-30", None),                   # late original, older period: ignored
        ("A5", "13F-HR/A", "2010-03-15", "2009-12-31", "restatement"),        # a "restatement" listing 1 small row
        ("A6", "13F-HR/A", "2010-03-16", "2009-12-31", "restatement"),        # cover only: no rows
        ("A7", "13F-HR", "2010-05-14", "2010-03-31", None),
        ("A8", "13F-HR/A", "2010-05-14", "2010-03-31", "restatement"),        # a real restatement, same day
    ], columns=["accession", "form", "filing_date", "report_date", "amendment_type"])
    tables = {"A1": _rows([("AAA", 600), ("BBB", 400)]), "A2": _rows([("XXX", 100)]),
              "A3": _rows([("OLD", 999)]), "A4": _rows([("OLD", 999)]), "A5": _rows([("YYY", 50)]),
              "A6": _rows([]), "A7": _rows([("AAA", 500), ("CCC", 500)]), "A8": _rows([("AAA", 450), ("DDD", 550)])}
    return f, tables


def test_holding_events_public_rule():
    f, tables = _filings()
    ev = b13.holding_events(f, tables)
    assert [e["accession"] for e in ev] == ["A1", "A2", "A5", "A7", "A8"]
    assert [e["kind"] for e in ev] == ["original", "amend_add", "amend_add", "original", "amend_restate"]
    assert set(ev[1]["positions"]["cusip"]) == {P("AAA"), P("BBB"), P("XXX")}
    assert set(ev[2]["positions"]["cusip"]) == {P("AAA"), P("BBB"), P("XXX"), P("YYY")}   # small "restatement" adds
    assert set(ev[4]["positions"]["cusip"]) == {P("AAA"), P("DDD")}
    assert all(P("OLD") not in set(e["positions"]["cusip"]) for e in ev)
    orig = b13.holding_events(f, tables, mode="originals")
    assert [e["accession"] for e in orig] == ["A1", "A7"]


def test_trade_session_is_strictly_after_the_filing_date():
    sessions = pd.bdate_range("2010-02-01", "2010-06-30")
    f, tables = _filings()
    st = b13.states_by_trade_session(b13.holding_events(f, tables), sessions)
    for t, e in st.items():
        assert t > pd.Timestamp(e["filing_date"])
        assert sessions[sessions.get_loc(t) - 1] <= pd.Timestamp(e["filing_date"])
    # A7 and A8 are filed the same day: one state, the later one
    assert st[pd.Timestamp("2010-05-17")]["accession"] == "A8"
    assert b13.trade_session("2010-02-12", sessions) == pd.Timestamp("2010-02-15")    # Friday -> Monday
    assert b13.trade_session("2010-06-30", sessions) is None


def test_b1_targets_signal_is_the_session_before_trading_and_no_lookahead():
    sessions = pd.bdate_range("2010-02-01", "2010-06-30")
    f, tables = _filings()
    st = b13.states_by_trade_session(b13.holding_events(f, tables), sessions)
    tg, cov = bf.b1_targets(st, sessions, lambda c, t: c != P("BBB"), "all_vw")
    for sig, t in zip(tg, st):
        assert sessions[sessions.get_loc(sig) + 1] == t
    first = tg[min(tg)]
    assert dict((c, w) for c, w, *_ in first) == {P("AAA"): 1.0}        # BBB unpriced: weight to the priced one
    assert cov["value_share_priced"].iloc[0] == pytest.approx(0.6)
    # the engine trades a target at the close after its signal: nothing is held before the first trade session
    idx = pd.DataFrame(1.0, index=sessions, columns=[P(c) for c in ("AAA", "BBB", "CCC", "DDD", "XXX", "YYY")])
    idx = idx.mul(np.linspace(1, 2, len(sessions)), axis=0)
    sim = simulate_index_units(tg, sessions, idx, idx * 50, pd.Series(dtype="datetime64[ns]"), idx[P("AAA")],
                               idx[P("AAA")])
    assert sim["nav"].index[0] == min(st)


def test_b1_weight_rules():
    pos = pd.DataFrame({"cusip": [f"C{k:02d}" for k in range(12)], "value": [100.0 - k for k in range(12)]})
    priced = set(pos["cusip"]) - {"C01"}
    w, cov = bf.b1_weights(pos, priced, "top10_ew")
    assert len(w) == 9 and all(x == pytest.approx(1 / 9) for _, x in w)     # the top 10 of all, priced ones only
    assert cov["top10_priced"] == 9
    w, _ = bf.b1_weights(pos, priced, "all_vw")
    assert sum(x for _, x in w) == pytest.approx(1.0) and len(w) == 11
    assert dict(w)["C00"] == pytest.approx(100 / (pos["value"].sum() - 99))
    w, _ = bf.b1_weights(pos, priced, "top10_vw")
    assert len(w) == 9 and sum(x for _, x in w) == pytest.approx(1.0)


def test_b1_half_spread_tick_floor():
    assert bf.b1_half_spread(100.0, pd.Timestamp("2005-01-03")) == pytest.approx(5e-4)
    assert bf.b1_half_spread(5.0, pd.Timestamp("2005-01-03")) == pytest.approx(0.005 / 5)
    assert bf.b1_half_spread(50.0, pd.Timestamp("2000-06-01")) == pytest.approx(1 / 32 / 50)
    assert bf.b1_half_spread(float("nan"), pd.Timestamp("2000-06-01")) == pytest.approx(5e-4)


def test_engine_uses_the_half_spread_function():
    sessions = pd.bdate_range("2020-01-01", periods=10)
    idx = pd.DataFrame({"A": 1.0}, index=sessions)
    tg = {sessions[0]: [("A", 1.0, np.nan, np.nan, "")]}
    lr = pd.Series(dtype="datetime64[ns]")
    a = simulate_index_units(tg, sessions, idx, idx * 100, lr, idx["A"], idx["A"], hs_fn=lambda s, d, p: 0.0)
    b = simulate_index_units(tg, sessions, idx, idx * 100, lr, idx["A"], idx["A"], hs_fn=lambda s, d, p: 0.01)
    assert a["cost"].sum() < b["cost"].sum()
    assert b["cost"].sum() - a["cost"].sum() == pytest.approx(0.01 * 10_000, rel=1e-3)


# ======================================================================== CUSIP map and price check

def test_cusip_map_precedence_and_fixes():
    assert bc.spec_for("191216100", "KO") == [("yahoo", "KO", None, None)]
    assert bc.spec_for("375766102", None) == [("wayback", "G", None, None)]       # hand map wins
    assert bc.spec_for("422806208", "HEI/A") == [("yahoo", "HEI-A", None, None)]
    assert bc.spec_for("200334100", "CDCO") is None                             # skipped on purpose
    assert bc.spec_for("999999999", None) is None
    h = _rows([("949773V107", 10), ("94973V107", 5)]).assign(
        cusip=lambda d: d["cusip"].replace(bc.CUSIP_FIXES))
    p = b13.equity_positions(h)
    assert list(p["cusip"]) == ["94973V107"] and p["value"].iloc[0] == 15
    for seg in bc.HAND.values():
        assert all(src in ("yahoo", "wayback", "wiki", "alpaca") for src, *_ in seg)


def test_price_check_units_and_acceptance():
    d = pd.bdate_range("2021-12-27", "2023-04-10")
    raw = pd.Series(50.0, index=d)
    s = bp.Series_(raw, raw, "yahoo", "X")
    checks = pd.DataFrame([
        {"cusip": "X", "report_date": "2021-12-31", "filing_date": "2022-02-14", "value": 500.0, "shares": 10_000.0},
        {"cusip": "X", "report_date": "2022-12-31", "filing_date": "2023-02-14", "value": 500_000.0, "shares": 10_000.0},
        {"cusip": "X", "report_date": "2023-03-31", "filing_date": "2023-05-15", "value": 900_000.0, "shares": 10_000.0},
        {"cusip": "Y", "report_date": "2023-03-31", "filing_date": "2023-05-15", "value": 1.0, "shares": 1.0}])
    pc = bp.price_check(checks, {"X": s})
    assert list(pc["ok"]) == [True, True, False, False]
    assert pc["ratio"].iloc[0] == pytest.approx(1.0) and pc["ratio"].iloc[1] == pytest.approx(1.0)
    acc = bp.accept(pc, 0.20)
    assert not acc.at["X", "accepted"] and acc.at["X", "failed_share"] == pytest.approx(1 / 3)
    assert "Y" not in acc.index                                                 # no close: not checked
    assert bp.accept(pc[pc.index < 2], 0.20).at["X", "accepted"]


def test_yahoo_raw_close_undoes_later_splits(tmp_path):
    ts = [int(pd.Timestamp(x, tz="America/New_York").timestamp()) for x in ("2020-01-02 16:00", "2020-01-03 16:00",
                                                                            "2020-01-06 16:00")]
    j = {"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [{"close": [50.0, 51.0, 52.0]}],
                                                                   "adjclose": [{"adjclose": [49.0, 50.0, 52.0]}]},
                              "events": {"splits": {"x": {"date": ts[2], "numerator": 2, "denominator": 1}}}}]}}
    p = bp.yahoo_path(tmp_path, "ZZZ")
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps(j))
    s = bp.yahoo_series(tmp_path, "ZZZ", "2020-12-31")
    assert list(s.raw) == [100.0, 102.0, 52.0]
    assert list(s.tr) == [49.0, 50.0, 52.0]


def test_splice_chains_returns():
    a = bp.Series_(pd.Series([1.0, 1.1], index=pd.to_datetime(["2016-12-29", "2016-12-30"])), pd.Series([10.0, 11.0],
                   index=pd.to_datetime(["2016-12-29", "2016-12-30"])), "wayback", "X")
    b = bp.Series_(pd.Series([20.0, 22.0, 33.0], index=pd.to_datetime(["2016-12-30", "2017-01-03", "2017-01-04"])),
                   pd.Series([20.0, 22.0, 33.0], index=pd.to_datetime(["2016-12-30", "2017-01-03", "2017-01-04"])),
                   "alpaca", "X")
    s = bp.splice([a, b])
    assert list(s.tr.round(6)) == [1.0, 1.1, 1.21, 1.815]


# ======================================================================== B2 screen

def _facts(cik, years, ni, be, debt):
    rows = []
    for y, n, e, d in zip(years, ni, be, debt):
        end, filed = f"{y}-12-31", f"{y + 1}-02-20"
        rows += [(cik, "ni", 0, "F", end, filed, n), (cik, "be", 0, "S", end, filed, e),
                 (cik, "ltd_nc", 0, "S", end, filed, d), (cik, "rev", 0, "F", end, filed, 10 * n)]
    return pd.DataFrame(rows, columns=["cik", "group", "prio", "kind", "end", "filed", "val"])


def test_buffett_states_and_point_in_time_merge():
    f = _facts(7, range(2009, 2015), [10, 12, 14, 13, 16, 18], [50, 55, 60, 65, 70, 75], [5] * 6)
    st = bf.buffett_states(f)
    last = st.iloc[-1]
    assert last["fy0_end"] == "2014-12-31"
    assert [last[f"ni{j}"] for j in range(5)] == [18, 16, 13, 14, 12]
    assert [last[f"be{j}"] for j in range(5)] == [75, 70, 65, 60, 55]
    assert last["debt0"] == 5
    rows = pd.DataFrame({"s": pd.to_datetime(["2015-02-20", "2015-02-23"]), "cik": [7, 7], "security_id": ["a", "a"]})
    m = bf.asof_buffett(rows, st)
    assert m.iloc[0]["fy0_end"] == "2013-12-31"      # the 2014 10-K filed that day is not usable yet
    assert m.iloc[1]["fy0_end"] == "2014-12-31"


def _screen_rows(**over):
    base = {"ni0": 20.0, "ni1": 18.0, "ni2": 16.0, "ni3": 14.0, "ni4": 12.0, "be0": 100.0, "be1": 100.0, "be2": 100.0,
            "be3": 90.0, "be4": 70.0, "debt0": 10.0}
    base.update(over)
    return pd.DataFrame([base])


def test_screen_definitions():
    q = bf.qualifies(_screen_rows())
    assert bool(q["q_roe"].iloc[0]) and bool(q["q_de"].iloc[0]) and bool(q["q_growth"].iloc[0])
    assert q["roe_min"].iloc[0] == pytest.approx(14 / 90)
    assert not bool(bf.qualifies(_screen_rows(ni0=15.0))["q_roe"].iloc[0])           # ROE exactly 15%: not > 15%
    assert not bool(bf.qualifies(_screen_rows(be4=-5.0))["q_roe"].iloc[0])            # negative equity
    assert not bool(bf.qualifies(_screen_rows(ni3=None))["q_roe"].iloc[0])            # five years needed
    assert not bool(bf.qualifies(_screen_rows(debt0=50.0))["q_de"].iloc[0])           # D/E 0.5: not < 0.5
    assert bool(bf.qualifies(_screen_rows(debt0=None))["q_de"].iloc[0])               # missing debt counts as 0
    one = bf.qualifies(_screen_rows(ni2=19.0))                                        # one decline (FY-2 -> FY-1)
    assert one["declines"].iloc[0] == 1 and bool(one["q_growth"].iloc[0])
    two = bf.qualifies(_screen_rows(ni2=19.0, ni0=17.0))
    assert two["declines"].iloc[0] == 2 and not bool(two["q_growth"].iloc[0])
    assert not bool(bf.qualifies(_screen_rows(ni0=19.0, ni4=19.5, ni1=20.5, be0=100.0))["q_growth"].iloc[0])


def test_b2_select_valuation_variants():
    g = pd.DataFrame({"security_id": list("abcd"), "ni0": [10.0, 10.0, 10.0, 10.0], "mcap": [100.0, 200.0, 400.0, 50.0],
                      "q_roe": [True] * 4, "q_de": [True] * 4, "q_growth": [True, True, True, False]})
    ey = bf.b2_select(g, "EY", 10, y10=0.04)
    assert list(ey["security_id"]) == ["a", "b"]                 # E/P 10%, 5% > 4%; c is 2.5%; d fails growth
    pe = bf.b2_select(g, "PE", 2, y10=0.04)
    assert list(pe["security_id"]) == ["a", "b"]
    pe3 = bf.b2_select(g, "PE", 3, y10=0.99)
    assert list(pe3["security_id"]) == ["a", "b", "c"]           # no yield threshold for PE
    assert bf.b2_select(g, "EY", 10, y10=0.5).empty


# ======================================================================== B3

def _factors(n=600, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.period_range("1970-01", periods=n, freq="M")
    f = pd.DataFrame(rng.normal(0.005, 0.03, (n, 6)), index=idx, columns=["MKT", "SMB", "HML", "UMD", "BAB", "QMJ"])
    f["RF"] = 0.003
    return f


def test_fkp_section7_scales_active_volatility():
    f = _factors()
    r1, k = bf.fkp_section7(f)
    ra = (r1 - f["RF"] - bf.FKP_BETA * f["MKT"]) / k
    w = ra[(ra.index >= pd.Period("1976-01", "M")) & (ra.index <= pd.Period("2011-12", "M"))]
    assert (k * w).std() * math.sqrt(12) == pytest.approx(bf.FKP_SIGMA_I)
    expect = (bf.FKP_COL3["MKT"] - bf.FKP_BETA) * f["MKT"] + sum(bf.FKP_COL3[c] * f[c]
                                                                  for c in ("SMB", "HML", "UMD", "BAB", "QMJ"))
    assert np.allclose(ra, expect)


def test_fkp_levered_financing():
    f = _factors()
    r2 = bf.fkp_levered(f, {"MKT": 0.98, "BAB": 0.15, "QMJ": 0.44}, 1.6, 0.015)
    manual = f["RF"] + 1.6 * (0.98 * f["MKT"] + 0.15 * f["BAB"] + 0.44 * f["QMJ"]) - 0.6 * 0.015 / 12
    assert np.allclose(r2, manual)


def test_margin_engine_interest_rebalance_and_maintenance():
    s = pd.bdate_range("2020-01-01", periods=60)
    rets = pd.DataFrame(0.0, index=s, columns=["A", "B"])
    close = pd.DataFrame(50.0, index=s, columns=["A", "B"])
    bm = pd.Series(0.02, index=s)
    hs = {"A": 0.0, "B": 0.0}
    sim = simulate_margin_weights({"A": 0.5, "B": 0.5}, 1.6, rets, close, bm, [], s[0], s[-1], hs)
    loan0 = 0.6 * (10_000 - sim["cost"].iloc[0])
    days = (s[-1] - s[0]).days
    assert sim["interest"].sum() == pytest.approx(loan0 * 0.035 * days / 360, rel=1e-2)
    assert sim["interest"].iloc[1] == pytest.approx(loan0 * 0.035 * (s[1] - s[0]).days / 360)
    assert sim["gross"].iloc[0] == pytest.approx(1.6)
    one = simulate_margin_weights({"A": 0.5, "B": 0.5}, 1.0, rets, close, bm, [], s[0], s[-1], hs)
    assert one["interest"].sum() == 0.0
    crash = rets.copy()
    crash.iloc[10] = -0.55                                       # 1.6x: equity / holdings falls below 25%
    sim = simulate_margin_weights({"A": 0.5, "B": 0.5}, 1.6, crash, close, bm, [s[30]], s[0], s[-1], hs)
    assert sim["liquidations"] == [str(s[10].date())]
    assert sim["gross"].iloc[11] == pytest.approx(1.0)
    assert sim["gross"].iloc[31] == pytest.approx(1.6, rel=1e-3)   # back to 1.6x at the next execution


def test_halves_and_window_helpers():
    s = pd.bdate_range("2020-01-01", periods=11)
    nav = pd.Series(np.arange(1, 12, dtype=float), index=s)
    h1, h2 = bf.halves_by_sessions(nav, str(s[1].date()), str(s[-1].date()))
    assert h1 == (str(s[1].date()), str(s[5].date())) and h2 == (str(s[6].date()), str(s[10].date()))
    assert bf.window_return(nav, str(s[3].date()), str(s[5].date())) == pytest.approx(6 / 3 - 1)
    assert np.isnan(bf.window_return(nav, "2019-01-01", "2019-12-31"))


def test_vs_and_judge_on_synthetic_navs():
    s = pd.bdate_range("2010-01-01", periods=1300)
    rng = np.random.default_rng(3)
    b = pd.Series(np.cumprod(1 + rng.normal(0.0004, 0.01, len(s))), index=s)
    better = b * np.exp(np.linspace(0, 0.6, len(s)))          # steadily ahead: A holds with a big t
    rf = pd.Series(0.0, index=s)
    h1, h2 = bf.halves_by_sessions(better, str(s[1].date()), str(s[-1].date()))
    full = bf.vs(better, b, rf, str(s[1].date()), str(s[-1].date()))
    parts = [bf.vs(better, b, rf, *h1), bf.vs(better, b, rf, *h2)]
    v = bf.judge(parts, full)
    assert v["A"] and v["pass"] and v["t_full"] > 2
    worse = b * np.exp(np.linspace(0, -0.6, len(s)))
    full = bf.vs(worse, b, rf, str(s[1].date()), str(s[-1].date()))
    v = bf.judge([bf.vs(worse, b, rf, *h1), bf.vs(worse, b, rf, *h2)], full)
    assert not v["A"] and not v["B"] and not v["pass"]
    assert full["excess_vs_oneq"] < 0


# ======================================================================== post-run fixes (2026-10-23 review)

def test_small_restatement_replaces_a_name_already_in_the_state():
    """A "restatement" too small to replace the state is an addition, but a CUSIP it lists that the state already
    holds replaces that CUSIP's rows (the 2000-09-06 Shaw Communications amendment was counted twice)."""
    f = pd.DataFrame([("A1", "13F-HR", "2010-02-16", "2009-12-31", None),
                      ("A2", "13F-HR/A", "2010-03-01", "2009-12-31", "restatement"),
                      ("A3", "13F-HR/A", "2010-03-02", "2009-12-31", "new_holdings")],
                     columns=["accession", "form", "filing_date", "report_date", "amendment_type"])
    tables = {"A1": _rows([("AAA", 600), ("BBB", 400)]), "A2": _rows([("BBB", 400), ("ZZZ", 10)]),
              "A3": _rows([("BBB", 5)])}
    ev = b13.holding_events(f, tables)
    assert [e["kind"] for e in ev] == ["original", "amend_add", "amend_add"]
    pos = ev[1]["positions"].set_index("cusip")["value"].to_dict()
    assert pos == {P("AAA"): 600.0, P("BBB"): 400.0, P("ZZZ"): 10.0}             # BBB not doubled
    assert ev[2]["positions"].set_index("cusip")["value"][P("BBB")] == 405.0     # "adds new holdings": summed


def _alpaca_page(tmp_path, symbol, adjustment, bars):
    import gzip
    p = tmp_path / "alpaca" / f"{symbol}_{adjustment}_2016-01-01_2016-12-31_p0.json.gz"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(gzip.compress(json.dumps({"bars": {symbol: bars}, "next_page_token": None}).encode()))


def test_alpaca_zero_volume_placeholder_bars_are_dropped(tmp_path):
    bars = [{"t": "2016-01-04T05:00:00Z", "c": 10.0, "v": 100}, {"t": "2016-01-05T05:00:00Z", "c": 10.0, "v": 0},
            {"t": "2016-01-06T05:00:00Z", "c": 11.0, "v": 50}]
    for adj in ("all", "raw"):
        _alpaca_page(tmp_path, "ZZ", adj, bars)
    led = bp.ledger(tmp_path)
    f = bp.alpaca_frame(tmp_path, "ZZ", "2016-01-01", "2016-12-31", "all", led, {}, offline=True)
    assert list(f["date"].dt.strftime("%Y-%m-%d")) == ["2016-01-04", "2016-01-06"]
    s = bp.alpaca_series(tmp_path, "ZZ", "2016-12-31", led, {}, start="2016-01-01", offline=True)
    assert list(s.tr) == [10.0, 11.0] and list(s.raw) == [10.0, 11.0]


def test_cut_stale_ends_at_the_last_real_trade():
    d = pd.bdate_range("2018-01-01", periods=60)
    lv = np.r_[np.linspace(1.0, 1.3, 30), np.full(25, 1.3), np.linspace(1.31, 1.4, 5)]
    s = bp.Series_(pd.Series(lv, index=d), pd.Series(lv * 10, index=d), "alpaca", "X")
    c = bp.cut_stale(s, run=20)
    assert c.tr.index[-1] == d[29] and len(c.raw) == 30                          # the run starts at row 30
    short = bp.Series_(pd.Series(np.r_[lv[:30], np.full(19, 1.3), lv[55:]], index=d[:54]), pd.Series(1.0, index=d[:54]),
                       "wiki", "X")
    assert len(bp.cut_stale(short, run=20).tr) == 54                             # 19 flat sessions: kept
    assert bp.longest_flat_run(s.tr) == 25


def test_apply_distributions_adds_the_distributed_value_back():
    d = pd.bdate_range("2014-07-21", periods=6)
    raw = pd.Series([140.0, 141.0, 141.0, 47.0, 47.47, 48.0], index=d)
    s = bp.Series_(raw / 10, raw, "wiki", "LMCA")
    ev = [(str(d[3].date()), 2.0, None, "C-share dividend"), ("2020-01-02", 1.0, None, "outside: skipped")]
    out = bp.apply_distributions(s, ev, lambda src, t: np.nan)
    r = out.tr.pct_change()
    assert r.iloc[3] == pytest.approx(47.0 * 3 / 141.0 - 1)
    assert r.iloc[4] == pytest.approx(47.47 / 47.0 - 1) and r.iloc[1] == pytest.approx(141.0 / 140.0 - 1)
    out = bp.apply_distributions(s, [(str(d[3].date()), 0.25, ("yahoo", "Q"), "spin-off")], lambda src, t: 376.0)
    assert out.tr.pct_change().iloc[3] == pytest.approx((47.0 + 0.25 * 376.0) / 141.0 - 1)
    assert bp.apply_distributions(s, [("2020-01-02", 1.0, None, "")], None) is s


def test_offline_rebuild_sends_no_request(tmp_path, monkeypatch):
    from quant.data.sources import http
    calls = []
    monkeypatch.setattr(http, "urlopen", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(OSError("net")))
    led = bp.ledger(tmp_path)
    with pytest.raises(bp.OfflineMiss):
        bp.fetch_yahoo(tmp_path, "NOPE", led, "2026-10-03", offline=True)
    marker = bp.yahoo_path(tmp_path, "GONE")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.with_name(marker.name + ".404").write_bytes(b"")
    assert bp.fetch_yahoo(tmp_path, "GONE", led, "2026-10-03", offline=True) is False      # a cached 404
    win = pd.DataFrame({"need_a": ["2000-01-01"], "need_b": ["2001-01-01"]}, index=["C1"])
    out, st = bp.build_series({"C1": [("wayback", "OLDX", None, None)]}, win, tmp_path, led, "2026-09-30",
                               "2026-10-03", lambda: {}, log=lambda *a: None, offline=True)
    assert out == {} and st["status"].tolist() == ["no_data"] and calls == []


def test_ols_multi_and_extended_debt():
    rng = np.random.default_rng(5)
    idx = pd.period_range("2000-01", periods=240, freq="M")
    X = pd.DataFrame(rng.normal(0, 0.04, (240, 2)), index=idx, columns=["MKT", "QMJ"])
    y = 0.002 + 0.9 * X["MKT"] + 0.5 * X["QMJ"] + rng.normal(0, 0.001, 240)
    o = bf.ols_multi(pd.Series(y, index=idx), X)
    assert o["months"] == 240 and o["alpha_ann"] == pytest.approx(0.024, abs=0.003)
    assert o["b_MKT"] == pytest.approx(0.9, abs=0.01) and o["b_QMJ"] == pytest.approx(0.5, abs=0.01) and o["r2"] > 0.99
    rows = pd.DataFrame({"s": pd.to_datetime(["2016-04-29", "2016-04-29", "2016-04-29"]), "cik_i": [1, 2, 3],
                         "fy0_end": ["2015-09-26", "2015-12-31", "2015-12-31"], "debt0": [50.0, None, None]})
    facts = pd.DataFrame([(1, "CommercialPaper", "2015-09-26", "2015-10-28", 8.5),
                          (2, "LongTermDebtAndCapitalLeaseObligations", "2015-12-31", "2016-02-10", 900.0),
                          (2, "ShortTermBorrowings", "2015-12-31", "2016-02-10", 20.0),
                          (2, "DebtLongtermAndShorttermCombinedAmount", "2015-12-31", "2016-04-29", 5000.0),  # on s
                          (3, "LongTermNotesPayable", "2014-12-31", "2015-02-10", 70.0)],                  # wrong year
                         columns=["cik", "concept", "end", "filed", "val"])
    e = bf.debt_extended(rows, facts)
    assert e["debt_ext"].tolist() == [58.5, 920.0, 0.0]
    assert e["debt_ext_source"].tolist() == ["registered_plus_cp", "ltdcl", "registered"]
