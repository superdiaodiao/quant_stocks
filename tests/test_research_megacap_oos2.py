"""Offline tests for scripts/research_megacap_oos2.py and scripts/megacap_oos2_data.py (synthetic data only)."""
import numpy as np
import pandas as pd

from scripts import megacap_oos2_data as md
from scripts import research_megacap_oos2 as oo


# ---------------------------------------------------------------- cover parsing

def test_cover_shares_single_class_with_date():
    t = ("Yes X No . The number of shares outstanding of the registrant's common stock as of September 30, 1997 "
         "was 1,206,691,442. MICROSOFT CORPORATION FORM 10-Q")
    got = md.parse_cover_shares(t)
    assert got["shares"] == 1_206_691_442
    assert got["asof"] == pd.Timestamp("1997-09-30")


def test_cover_shares_sums_classes_and_ignores_dollar_values():
    t = ("The aggregate market value of voting stock held by non-affiliates was $21,533,000,000. "
         "As of December 31, 2002, there were 1,355,373,648 shares of Class A Common Stock, 883,343,590 shares of "
         "Class A Special Common Stock and 9,444,375 shares of Class B Common Stock outstanding.")
    got = md.parse_cover_shares(t)
    assert got["shares"] == 1_355_373_648 + 883_343_590 + 9_444_375
    assert got["multi_class"]


def test_cover_shares_million_and_preferred_respectively():
    t = "Shares outstanding of the Registrant's common stock: Class Outstanding at September 27, 1997 Common Stock, $.001 par value 1,636 million"
    assert md.parse_cover_shares(t)["shares"] == 1_636_000_000
    t2 = ("At July 31, 2004, the number of shares outstanding of the registrant's Class A common stock, $0.001 par value, "
          "Class B common stock, $0.001 par value, and preferred stock was 12,398,854, 162,861,743 and 79,099,884, respectively.")
    assert md.parse_cover_shares(t2)["shares"] == 12_398_854 + 162_861_743


def test_sept_abbreviation_does_not_break_september():
    assert md.parse_date("as of September 30, 2005 was") == pd.Timestamp("2005-09-30")
    assert md.parse_date("as of Sept. 30, 2005") == pd.Timestamp("2005-09-30")


def test_venue_from_12b_section():
    t = ("Securities registered pursuant to Section 12(b) of the Act: Common Stock New York Stock Exchange "
         "Securities registered pursuant to Section 12(g) of the Act: None")
    assert md.parse_cover_venue(t)["venue_12b"] == "NYSE"
    t2 = "Securities registered pursuant to Section 12(b) of the Act: None Securities registered pursuant to Section 12(g) of the Act: Common Stock"
    assert md.parse_cover_venue(t2)["venue_12b"] == "none_12b"


def test_yahoo_csv_span_from_url():
    u = "http://ichart.finance.yahoo.com/table.csv?s=BRCM&d=4&e=9&f=2013&g=d&a=3&b=17&c=1998&ignore=.csv"
    assert md._yahoo_csv_span(u) == ("1998-04-17", "2013-05-09")


def test_plan_sources_and_cik_spans():
    assert md.plan_sources("wayback:SUNW..2007-03-01;cmc:sun-microsystems") == [
        ("wayback", "SUNW", "2007-03-01"), ("cmc", "sun-microsystems", None)]
    assert md.cik_spans("777676..2005-01-31;1341439") == [(777676, None, "2005-01-31"), (1341439, "2005-01-31", None)]


# ---------------------------------------------------------------- split units

def _facts(key, asofs, shares):
    return pd.DataFrame({"key": key, "accession": [f"a{i}" for i in range(len(asofs))], "form": "10-Q",
                         "filed": pd.to_datetime(asofs) + pd.Timedelta(days=10), "asof": pd.to_datetime(asofs),
                         "shares": shares, "src": "cover"})


def test_split_units_from_known_events():
    f = _facts("A", ["2000-01-31", "2000-04-30", "2000-07-31"], [100.0, 200.0, 201.0])
    out = oo.split_units(f, {"A": [(pd.Timestamp("2000-03-15"), 2.0), (pd.Timestamp("2010-01-04"), 3.0)]})
    assert list(out["F"]) == [6.0, 3.0, 3.0]          # every split after the as-of date, to the price basis date


def test_split_units_inferred_from_share_jumps_and_basis_date():
    f = _facts("B", ["2000-01-31", "2000-04-30", "2000-07-31", "2006-01-31", "2006-04-30"], [100.0, 101.0, 202.0, 205.0, 310.0])
    out = oo.split_units(f, None)
    assert list(out["inferred_split"].fillna(0)) == [0, 0, 2.0, 0, 1.5]
    assert list(out["F"]) == [3.0, 3.0, 1.5, 1.5, 1.0]
    # the price basis (e.g. an archived CSV) ends in 2005: the 2006 split is not in the prices -> not in F
    out2 = oo.split_units(f, None, {"B": pd.Timestamp("2005-06-30")})
    assert list(out2["F"]) == [2.0, 2.0, 1.0, 1.0, 1.0]


def test_market_cap_needs_no_split_date():
    # 2:1 split on 2000-03-15; count of 100 measured before it; price basis after the split
    f = oo.split_units(_facts("A", ["2000-01-31"], [100.0]), {"A": [(pd.Timestamp("2000-03-15"), 2.0)]})
    # raw close at s=2000-04-28 is 30 (post split) -> adj = 30 on the post-split basis; true cap = 200 x 30
    assert float(f["shares"].iloc[0] * f["F"].iloc[0] * 30.0) == 6000.0


# ---------------------------------------------------------------- point-in-time market caps

def _panel(dates, adj):
    return pd.DataFrame({"adj": adj, "tr": np.array(adj) / adj[0], "close_raw": adj, "cmc_mcap": np.nan,
                         "source": "yahoo:X"}, index=pd.DatetimeIndex(dates))


def test_market_caps_filed_strictly_before_signal_and_freshness_and_venue():
    cand = pd.DataFrame({"key": ["A", "B"], "nasdaq_from": ["", "2000-03-01"], "nasdaq_to": ["", ""]})
    facts = pd.DataFrame({"key": ["A", "A", "B"], "accession": ["1", "2", "3"], "form": "10-Q",
                          "filed": pd.to_datetime(["1999-01-15", "2000-01-31", "1999-12-01"]),
                          "asof": pd.to_datetime(["1999-01-05", "2000-01-20", "1999-11-20"]),
                          "shares": [10.0, 20.0, 5.0], "src": "cover", "F": 1.0})
    days = pd.to_datetime(["2000-01-31", "2000-02-29"])
    panel = {"A": _panel(days, [1.0, 1.0]), "B": _panel(days, [1.0, 1.0])}
    m = oo.market_caps(cand, facts, panel, list(days)).set_index(["s", "key"])["mcap"]
    assert m[(days[0], "A")] == 10.0                   # filed on the signal day itself: not yet usable
    assert m[(days[1], "A")] == 20.0                   # the 2000-01-31 filing is usable from the next signal on
    assert (days[0], "B") not in m.index               # B is not on Nasdaq before 2000-03-01
    stale = oo.market_caps(cand, facts.iloc[[0]], panel, [days[1]])
    assert stale.empty                                 # the 1999-01-15 filing is more than 400 days old


def test_criteria_threshold_and_b_rule():
    full = {"cagr": 0.10, "bench_cagr": 0.08, "t_monthly_excess": 2.05, "max_dd": -0.5, "bench_max_dd": -0.55}
    c = oo.criteria(full)
    assert not c["A"] and c["A_nominal_t_ge_2"] and not c["B"]
    full2 = {"cagr": 0.06, "bench_cagr": 0.08, "t_monthly_excess": 0.0, "max_dd": -0.40, "bench_max_dd": -0.83}
    assert oo.criteria(full2)["B"]


def test_period_stats_rebases_at_the_window_start():
    d = pd.bdate_range("2001-01-01", periods=300)
    nav = pd.Series(np.linspace(100, 130, 300), index=d)
    bench = pd.Series(np.linspace(50, 55, 300), index=d)
    st = oo.period_stats(nav, bench, "2001-03-01", "2001-12-31")
    assert st["start"] <= "2001-03-01" and st["cagr"] > st["bench_cagr"]


def test_fix_share_outliers_split_side_and_parse_error():
    f = _facts("A", ["1999-01-31", "1999-04-30", "1999-07-31", "1999-10-31", "2000-01-31", "2000-04-30"],
               [100.0, 101.0, 204.0, 102.0, 5000.0, 103.0])
    f["F"] = 1.0
    out, log = oo.fix_share_outliers(f)
    u = (out["shares"] * out["F"]).round(1).tolist()
    assert u == [100.0, 101.0, 102.0, 102.0, 103.0]       # 204 was counted after a 2:1 split; 5000 is dropped
    assert set(log["fix"]) == {"split side: F / 2", "dropped (not a split ratio)"}


def test_fix_share_outliers_keeps_a_real_level_shift():
    f = _facts("C", ["2002-03-31", "2002-06-30", "2002-09-30", "2002-12-31", "2003-03-31"],
               [950.0, 955.0, 960.0, 2250.0, 2260.0])     # Comcast + AT&T Broadband: a merger, not an error
    f["F"] = 1.0
    out, log = oo.fix_share_outliers(f)
    assert len(out) == 5 and log.empty
