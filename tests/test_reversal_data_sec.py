"""Offline tests for the SEC builders of the reversal data plan (steps 3-4)."""
import pandas as pd
import pytest

from scripts import reversal_data_form25 as f25
from scripts import reversal_data_security_master as sm


FORM25_XML = b"""<?xml version="1.0"?>
<notificationOfRemoval>
    <schemaVersion>X0203</schemaVersion>
    <exchange><cik>0001354457</cik><entityName>NASDAQ Stock Market LLC</entityName></exchange>
    <issuer><cik>0000798955</cik><entityName>COST PLUS INC/CA/</entityName><fileNumber>000-14970</fileNumber></issuer>
    <descriptionClassSecurity>Common Stock</descriptionClassSecurity>
    <ruleProvision>17 CFR 240.12d2-2(a)(3)</ruleProvision>
    <signatureData><signatureName>X</signatureName><signatureDate>2012-06-29</signatureDate></signatureData>
</notificationOfRemoval>
"""


def _hit(accession, ciks, names, file_date="2012-06-29", form="25-NSE"):
    return {"_id": f"{accession}:primary_doc.xml",
            "_source": {"ciks": ciks, "display_names": names, "file_date": file_date, "form": form}}


# ------------------------------------------------------------------ Form 25

def test_half_year_slices_are_clipped_and_contiguous():
    slices = f25.half_year_slices("2012-01-01", "2013-09-30")
    assert slices == [("2012-01-01", "2012-06-30"), ("2012-07-01", "2012-12-31"),
                      ("2013-01-01", "2013-06-30"), ("2013-07-01", "2013-09-30")]
    assert len(f25.half_year_slices()) == 30


def test_frame_periods_run_from_2011q1_to_2026q2():
    periods = f25.frame_periods()
    assert periods[0] == "CY2011Q1I" and periods[-1] == "CY2026Q2I" and len(periods) == 62


def test_display_name_parsing():
    assert f25.parse_display_name("Carlyle Group Inc.  (CG, CGABL)  (CIK 0001527166)") == {
        "name": "Carlyle Group Inc.", "tickers": ["CG", "CGABL"], "cik": 1527166}
    assert f25.parse_display_name("COST PLUS INC/CA/  (CIK 0000798955)")["tickers"] == []
    odd = f25.parse_display_name("Holdings (Bermuda) Ltd  (CIK 0000000001)")
    assert odd["cik"] == 1 and odd["tickers"] == []


def test_filter_keeps_only_nasdaq_filings_once():
    hits = [
        _hit("0001354457-12-000140", ["0000798955", "0001354457"],
             ["COST PLUS INC/CA/  (CIK 0000798955)", "NASDAQ Stock Market LLC  (CIK 0001354457)"]),
        _hit("0000876661-12-000340", ["0000095304", "0000876661"],
             ["SUNOCO INC  (CIK 0000095304)", "NEW YORK STOCK EXCHANGE LLC  (CIK 0000876661)"]),
        _hit("0001354457-12-000140", ["0000798955", "0001354457"], ["dup"]),
    ]
    rows = f25.filter_nasdaq_filer(hits)
    assert len(rows) == 1
    row = rows[0]
    assert row["subject_cik"] == 798955 and row["filer_cik"] == 1354457
    assert row["document"] == "primary_doc.xml" and row["accession_prefix_is_filer"]


def test_all_hits_pages_until_total():
    def page(hits, total):
        return {"hits": {"total": {"value": total, "relation": "eq"}, "hits": hits}}

    calls = []

    def search(form, start, end, from_, offline=False):
        calls.append((start, from_))
        n = 150 if start == "2012-01-01" else 0
        ids = [{"_id": f"{start}-{i}:d.xml", "_source": {}} for i in range(n)]
        return page(ids[from_:from_ + 100], n)

    hits, log = f25.all_hits("25-NSE", "2012-01-01", "2012-12-31", search=search)
    assert len(hits) == 150
    assert calls == [("2012-01-01", 0), ("2012-01-01", 100), ("2012-07-01", 0)]
    assert log[0]["pages"] == 2


def test_parse_form25_xml_reads_class_and_rule():
    doc = f25.parse_form25_xml(FORM25_XML)
    assert doc["exchange_cik"] == 1354457 and doc["issuer_cik"] == 798955
    assert doc["class_of_security"] == "Common Stock"
    assert f25.delisting_basis(doc["rule_provision"]) == "substituted_merger_or_exchange"
    broken = f25.parse_form25_xml("<x><descriptionClassSecurity>Warrants</descriptionClassSecurity><ruleProvision>"
                                  "17 CFR 240.12d2-2(b)</ruleProvision>")
    assert broken["parse"] == "regex" and broken["class_of_security"] == "Warrants"
    assert f25.delisting_basis(broken["rule_provision"]) == "exchange_removal"


@pytest.mark.parametrize("text,kind", [
    ("Common Stock", "common"),
    ("Class A Common Stock, par value $0.0001", "common"),
    ("Ordinary Shares", "common"),
    ("Common Shares of Beneficial Interest", "common"),
    ("American Depositary Shares, each representing two ordinary shares", "ads"),
    ("Units, each consisting of one share of Class A common stock and one-half of one warrant", "unit"),
    ("Warrants to purchase Common Stock", "warrant"),
    ("Rights", "right"),
    ("Preferred Stock, convertible", "preferred"),
    ("Depositary Shares, each representing 1/1000th of a share of 7.00% Series A Preferred Stock", "preferred"),
    ("6.50% Senior Notes due 2026", "debt"),
    ("Common Units representing limited partner interests", "lp_units"),
    ("Warant", "warrant"),
    ("ADSs", "ads"),
    ("Depository Receipt", "ads"),
    ("Dorsey Wright MLP Index ETNs due December 10, 2036", "fund"),
    ("PowerShares Global Wind Energy Portfolio", "fund"),
    ("Brandes Value NextShares", "fund"),
    ("Income Deposit Securities", "other"),
    ("Depository Shares", "preferred"),
    ("Depositary Shares Each Representing a 1/40th Interest in a Share of Series B Fixed-to-Floating Rate", "preferred"),
    ("", "unknown"),
])
def test_classify_security_class(text, kind):
    assert f25.classify_security_class(text) == kind


def test_classify_row_uses_evidence_only_for_common():
    assert f25.classify_row("warrant", "exchange_removal", {"still_on_nasdaq_same_cik": True})[0] == "other_class"
    assert f25.classify_row("common", "substituted_merger_or_exchange", {})[0] == "common_delisting"
    assert f25.classify_row("common", "x", {"still_on_nasdaq_same_cik": True})[0] == "reorg"
    assert f25.classify_row("common", "x", {"still_on_nasdaq_same_cik": True, "tickers_before": "LSXMA FWONA",
                                            "tickers_ended": "LSXMA"})[0] == "common_delisting"
    assert f25.classify_row("common", "x", {"listed_elsewhere_now": "NYSE", "tickers_before": "ACXM",
                                            "tickers_elsewhere": "RAMP"})[0] == "common_delisting"
    assert f25.classify_row("common", "x", {"successor_same_ticker": 5})[0] == "reorg"
    assert f25.classify_row("ads", "x", {"listed_elsewhere_now": "NYSE", "tickers_before": "ABC",
                                         "tickers_elsewhere": "ABC"})[0] == "transfer"


def test_subject_evidence_from_submissions():
    payload = {"cik": "1", "name": "X", "tickers": ["XX"], "exchanges": ["NYSE"],
               "filings": {"recent": {"form": ["10-K", "10-Q", "25-NSE"],
                                      "filingDate": ["2016-03-01", "2015-11-01", "2014-06-01"]}}}
    ev = f25.subject_evidence(1, "2014-06-01", payload)
    assert ev["listed_elsewhere_now"] == "NYSE" and ev["periodic_after_400d"] == 2
    payload["exchanges"] = ["Nasdaq"]
    assert f25.subject_evidence(1, "2014-06-01", payload).get("still_on_nasdaq_same_cik")
    payload["filings"]["recent"]["filingDate"] = ["2014-09-01", "2014-08-01", "2014-06-01"]
    assert f25.subject_evidence(1, "2014-06-01", payload) == {"periodic_after_400d": 0, "tickers_elsewhere": ""}


def test_check_float_units():
    assert f25.check_float_units(None, 1e6) == "no_float"
    assert f25.check_float_units(1e9, None) == "no_shares"
    assert f25.check_float_units(1e9, 5e7) == "ok"  # $20 a share
    assert f25.check_float_units(4.8e12, 1.2e7) == "implied_per_share_high"  # SeqLL-like
    assert f25.check_float_units(2.3e12, 1.2e8) == "implied_per_share_high"  # CenterState-like
    assert f25.check_float_units(6e12, 1e10) == "float_above_5T"
    assert f25.check_float_units(4.8e3, 1.2e7) == "implied_per_share_low"  # float in thousands
    assert f25.check_float_units(1e9, 5e7, raw_close=20.0) == "ok"
    assert f25.check_float_units(1e9, 5e7, raw_close=2000.0) == "ratio_out_of_band"


def test_float_for_filing_takes_the_three_year_maximum_and_matching_shares():
    floats = pd.DataFrame({"cik": [7, 7, 7, 8], "end": pd.to_datetime(["2015-06-30", "2017-06-30", "2018-06-30", "2018-06-30"]),
                           "val": [9e9, 2e9, 1e9, 5e9], "accn": ["a", "b", "c", "d"], "period": "", "entity_name": ""})
    shares = pd.DataFrame({"cik": [7, 7], "end": pd.to_datetime(["2017-08-01", "2018-08-01"]), "val": [1e8, 1e8],
                           "accn": ["b", "c"], "period": "", "entity_name": ""})
    out = f25.float_for_filing(floats, shares, 7, "2019-03-01")
    assert out["public_float_usd"] == 2e9 and out["float_accession"] == "b" and out["float_obs_3y"] == 2
    assert out["shares_outstanding"] == 1e8 and out["implied_float_per_share"] == 20.0
    assert out["float_check_flag"] == "ok"
    assert f25.float_for_filing(floats, shares, 9, "2019-03-01")["float_check_flag"] == "no_float"


def test_successor_evidence_finds_same_ticker_new_cik():
    rows = pd.DataFrame({"accession": ["acc1"], "subject_cik": [1], "filing_date": ["2020-05-01"]})
    intervals = pd.DataFrame({
        "cik": [1, 2, 3], "ticker": ["ABC", "ABC", "ABC"], "exchange": "NASDAQ",
        "start": ["2018-01-19", "2020-05-08", "2023-01-01"], "end": ["2020-04-28", "2026-07-01", "2026-07-01"]})
    names = {1: ["Abcam Holdings Inc"], 2: ["ABCAM HOLDINGS CORP NEW"], 3: ["Abc Unrelated Inc"]}
    assert f25.successor_evidence(rows, intervals, names) == {"acc1": 2}
    names[2] = ["Different Co"]
    assert f25.successor_evidence(rows, intervals, names) == {}


# ------------------------------------------------------------------ security master

SUBMISSIONS = {
    "cik": "1326801", "name": "Meta Platforms, Inc.", "tickers": ["META"], "exchanges": ["Nasdaq"],
    "formerNames": [{"name": "FACEBOOK INC", "from": "2012-01-01T00:00:00.000Z", "to": "2022-06-08T00:00:00.000Z"}],
    "sic": "7370", "sicDescription": "Services-Computer Programming", "stateOfIncorporation": "DE",
    "entityType": "operating", "category": "Large accelerated filer",
    "filings": {"recent": {"form": ["10-K", "10-Q", "8-K", "10-K/A"],
                           "filingDate": ["2026-01-30", "2025-10-30", "2025-10-29", "2010-01-01"]},
                "files": [{"name": "CIK0001326801-submissions-001.json", "filingFrom": "2012-01-01", "filingTo": "2015-01-01"}]},
}


def test_parse_submissions_and_foreign_flag():
    profile = sm.parse_submissions(SUBMISSIONS)
    assert profile["name"] == "Meta Platforms, Inc." and profile["tickers"] == ["META"]
    assert profile["former_names"][0] == {"name": "FACEBOOK INC", "from": "2012-01-01", "to": "2022-06-08"}
    assert profile["n_domestic"] == 2 and profile["domestic_first"] == "2025-10-30"  # the 2010 10-K/A is before 2011-06
    assert sm.foreign_filer_flag(profile) == "N"
    assert not sm.needs_older_pages(profile)
    assert profile["older_pages_unfetched_since"] == ["CIK0001326801-submissions-001.json"]
    foreign = sm.parse_submissions({"filings": {"recent": {"form": ["20-F", "6-K"], "filingDate": ["2020-04-01", "2020-05-01"]}}})
    assert sm.foreign_filer_flag(foreign) == "Y"
    mixed = sm.parse_submissions({"filings": {"recent": {"form": ["20-F", "10-K"], "filingDate": ["2018-04-01", "2021-03-01"]}}})
    assert sm.foreign_filer_flag(mixed) == "MIXED"
    nothing = sm.parse_submissions({"filings": {"recent": {"form": ["8-K"], "filingDate": ["2013-01-01"]},
                                                "files": [{"name": "p1.json", "filingTo": "2012-01-01"}]}})
    assert sm.foreign_filer_flag(nothing) == "UNKNOWN" and sm.needs_older_pages(nothing)
    with_page = sm.parse_submissions({"filings": {"recent": {"form": ["8-K"], "filingDate": ["2013-01-01"]},
                                                  "files": [{"name": "p1.json", "filingTo": "2012-01-01"}]}},
                                     [{"form": ["10-K"], "filingDate": ["2012-03-01"], "_page_name": "p1.json"}])
    assert with_page["older_pages_unfetched_since"] == []
    assert sm.foreign_filer_flag(with_page) == "N"


@pytest.mark.parametrize("raw,norm", [
    ("Achillion Pharmaceuticals, Inc. - Common Stock", "achillion pharmaceuticals"),
    ("ACHILLION PHARMACEUTICALS INC", "achillion pharmaceuticals"),
    ("COST PLUS INC/CA/", "cost plus"),
    ("Alphabet Inc. - Class C Capital Stock", "alphabet"),
    ("American Airlines Group, Inc. ", "american airlines group"),
    ("EDAP TMS S.A. - American Depositary Shares", "edap tms"),
    ("EDAP TMS SA /ADR/", "edap tms"),
    ("Conn's, Inc. - Common Stock", "conns"),
    ("CONNS INC", "conns"),
    ("LANDEC CORP \\CA\\", "landec"),
    ("CONNECTICUT WATER SERVICE INC / CT", "connecticut water service"),
    ("Bank of Commerce Holdings (CA) - Common Stock", "bank of commerce holdings"),
    ("Health Sciences Acquisitions Corporation II - Ordinary Shares", "health sciences acquisitions 2"),
    ("Baidu, Inc. - American Depositary Shares, each representing one tenth Class A ordinary share", "baidu"),
    ("AAON, Inc. Common Stock", "aaon"),
    ("The Kraft Heinz Company - Common Stock", "kraft heinz"),
])
def test_normalize_issuer_name(raw, norm):
    assert sm.normalize_issuer_name(raw) == norm


def test_names_match_rules():
    assert sm.names_match("facebook", "facebook")
    assert sm.names_match("costco wholesale", "costco wholesale")
    assert sm.names_match("liberty global", "liberty global europe")  # two-token prefix
    assert sm.names_match("nvidia", "nvidia graphics")  # one distinctive token
    assert not sm.names_match("first", "first solar")  # generic single token
    assert sm.names_match("first solar", "first solar")
    assert not sm.names_match("abc", "xyz")


@pytest.mark.parametrize("name,klass", [
    ("Alphabet Inc. - Class C Capital Stock", "C"),
    ("Liberty Global plc - Class A Ordinary Shares", "A"),
    ("Baidu, Inc. - American Depositary Shares", "ADS"),
    ("Apple Inc. - Common Stock", "COMMON"),
    ("Liberty Media Corporation - Series C Liberty SiriusXM Common Stock", "C"),
])
def test_share_class_from_name(name, klass):
    assert sm.share_class_from_name(name) == klass


@pytest.mark.parametrize("name,common_equity", [
    ("Apple Inc. - Common Stock", True),
    ("Baidu, Inc. - American Depositary Shares, each representing one tenth Class A ordinary share", True),
    ("Some Bank - Depositary Shares, each representing a 1/40th interest in a share of Series A Preferred", False),
    ("XYZ Acquisition Corp - Warrant", False),
    ("XYZ Acquisition Corp - Units", False),
    ("Invesco QQQ Trust, Series 1 ETF", False),
    ("Golar LNG Partners LP - Common Units", False),
])
def test_is_common_equity(name, common_equity):
    assert sm.is_common_equity(name) is common_equity


def _profile(name, former=(), tickers=()):
    return {"name": name, "former_names": [{"name": n, "from": "", "to": ""} for n in former], "tickers": list(tickers)}


def test_resolve_listing_prefers_name_match_and_handles_reuse():
    profiles = {1: _profile("Meta Platforms, Inc.", ["FACEBOOK INC"], ["META"]),
                2: _profile("Radius Recycling", ["SCHNITZER STEEL INDUSTRIES INC"], ["RDUS"]),
                3: _profile("Radius Health, Inc.")}
    cands = sm.Candidates()
    cands.add("FB", 1, "hist")
    cands.add("RDUS", 2, "map")
    cands.add("RDUS", 3, "map_old")
    index = sm.build_name_index(profiles)
    assert sm.resolve_listing("FB", "Facebook, Inc. - Class A Common Stock", cands, profiles, index) == (1, "name+ticker")
    assert sm.resolve_listing("RDUS", "Radius Health, Inc. - Common Stock", cands, profiles, index) == (3, "name+ticker")
    assert sm.resolve_listing("RDUS", "Radius Recycling, Inc. - Class A Common Stock", cands, profiles, index) == (2, "name+ticker")
    assert sm.resolve_listing("ZZZ", "Meta Platforms, Inc. - Class A Common Stock", cands, profiles, index) == (1, "name_only")
    cands.add("QQQQ", 2, "map")
    assert sm.resolve_listing("QQQQ", "Totally Different Name Inc", cands, profiles, index) == (2, "ticker_only")
    assert sm.resolve_listing("NONE", "Nobody Inc", cands, profiles, index) == (None, "unresolved")


def test_build_intervals_breaks_on_gaps_and_cik_change():
    dates = ["2018-01-19", "2018-02-01", "2018-03-01", "2018-04-01", "2018-05-01", "2018-06-01", "2018-07-01"]
    rows = pd.DataFrame({
        "family": "repo_symdir", "symbol": "ABC",
        "date": ["2018-01-19", "2018-02-01", "2018-04-01", "2018-05-01", "2018-06-01", "2018-07-01"],
        "cik": [1, 1, 1, 1, 2, 2], "share_class": "COMMON", "source_url": "x", "how": "name+ticker", "name": "n"})
    # 2018-03-01 missing once: tolerated; CIK changes at 2018-06-01.
    out = sm.build_intervals(rows, {"repo_symdir": dates})
    assert out[["cik", "start", "end", "n_snapshots"]].values.tolist() == [
        [1, "2018-01-19", "2018-05-01", 4], [2, "2018-06-01", "2018-07-01", 2]]
    assert out.iloc[0]["start_prev_absent"] == "" and out.iloc[0]["end_next_absent"] == "2018-06-01"
    gap = rows[rows["date"].isin(["2018-01-19", "2018-05-01"])]
    out = sm.build_intervals(gap, {"repo_symdir": dates})
    assert len(out) == 2  # missing from three snapshots in a row


def test_detect_ticker_reuse():
    intervals = pd.DataFrame({"ticker": ["A", "A", "B"], "cik": [1, 2, 3]})
    assert sm.detect_ticker_reuse(intervals) == {"A": [1, 2]}


def test_snapshot_reader_takes_date_from_column_or_filename(tmp_path):
    a = tmp_path / "nasdaq_listed_2019-03-01.csv"
    a.write_text("Symbol,Name,ETF,Test Issue,NextShares\nAAPL,Apple Inc. - Common Stock,N,N,N\nQQQ,Invesco QQQ,Y,N,N\n")
    b = tmp_path / "hook"
    b.mkdir()
    (b / "capture_20130102.csv").write_text("Symbol,Name,Observed At\nDELL,Dell Inc.,2013-01-02T00:00:00\n")
    snaps = sm.load_listing_snapshots(tmp_path, {"wayback_symdir": b})
    assert set(snaps["date"]) == {"2019-03-01", "2013-01-02"}
    common_rows = sm.common_snapshot_rows(snaps)
    assert set(common_rows["symbol"]) == {"AAPL", "DELL"}
    assert set(snaps["source"]) == {"repo_symdir", "wayback_symdir"}


def test_security_id_suffix_only_for_multi_class():
    assert sm.security_id(1652044, "C", True) == "1652044.C"
    assert sm.security_id(320193, "COMMON", False) == "320193"


def test_resolve_by_date_splits_a_holding_company_reorganisation():
    old = {"name": "Example Bancorp", "former_names": [], "tickers": [], "older_pages": [],
           "periodic_dates": ["2016-03-01", "2018-03-01", "2019-08-01"], "filing_dates": ["2016-03-01", "2019-08-10"]}
    new = {"name": "Example Bancorp", "former_names": [], "tickers": ["EXB"], "older_pages": [],
           "periodic_dates": ["2019-11-01", "2026-08-01"], "filing_dates": ["2019-10-01", "2026-08-01"]}
    profiles = {1: old, 2: new}
    cands = sm.Candidates()
    cands.add("EXB", 2, "map")
    index = sm.build_name_index(profiles)
    assert sm.resolve_listing("EXB", "Example Bancorp - Common Stock", cands, profiles, index,
                              "2018-01-19", "2026-07-01") == (None, "ambiguous")
    out = sm.resolve_by_date("EXB", "Example Bancorp - Common Stock", ["2018-01-19", "2019-09-01", "2020-06-01"],
                             cands, profiles, index)
    assert out == {"2018-01-19": 1, "2019-09-01": 1, "2020-06-01": 2}  # overlap goes to the incumbent


def test_bare_company_list_names_are_judged_by_the_nearest_typed_row():
    snaps = pd.DataFrame({
        "symbol": ["ABCDW", "ABCDW", "ABCD", "ABCD", "XYZWW", "QRST", "SPY"],
        "name": ["Abcd Corp - Warrant", "Abcd Corp", "Abcd Corp - Common Stock", "Abcd Corp", "Xyz Corp",
                 "Qrst Inc", "SPDR"],
        "date": ["2013-01-01", "2013-06-01", "2013-01-01", "2013-06-01", "2013-06-01", "2013-06-01", "2013-06-01"],
        "ETF": ["N", "", "N", "", "", "", ""], "Test Issue": "", "NextShares": "", "source": "x", "source_url": "x"})
    kept = sm.common_snapshot_rows(snaps)
    assert sorted(zip(kept["symbol"], kept["date"])) == [
        ("ABCD", "2013-01-01"), ("ABCD", "2013-06-01"), ("QRST", "2013-06-01"), ("SPY", "2013-06-01")]


def test_two_live_companies_sharing_a_name_keep_the_mapped_ticker():
    live = {"older_pages": [], "periodic_dates": ["2011-03-01", "2026-08-01"], "filing_dates": ["2011-03-01", "2026-08-01"]}
    profiles = {10: {**live, "name": "First Bancorp /NC/", "former_names": [], "tickers": ["FBNC"]},
                20: {**live, "name": "FIRST BANCORP /PR/", "former_names": [], "tickers": ["FBP"]}}
    cands = sm.Candidates()
    cands.add("FBNC", 10, "sec_company_tickers_exchange")
    cands.add("FBNC", 20, "sec_cik_lookup")
    index = sm.build_name_index(profiles)
    lookup = {"first bancorp": {10, 20}}
    assert sm.resolve_listing("FBNC", "First Bancorp - Common Stock", cands, profiles, index,
                              "2018-01-19", "2026-07-01", lookup) == (10, "name+ticker")
    # Without a mapped ticker the shared name resolves nothing.
    assert sm.resolve_listing("ZZZZ", "First Bancorp - Common Stock", sm.Candidates(), profiles, index,
                              "2018-01-19", "2026-07-01", lookup) == (None, "ambiguous")


def test_name_key_ignores_spacing():
    assert sm.name_key("U S Concrete, Inc. - Common Stock") == sm.name_key("U.S. CONCRETE, INC.") == "usconcrete"


def test_names_are_matched_with_their_edgar_dates():
    # Overstock took the name Bed Bath & Beyond in 2025; the old BBBY carried it until 2023.
    overstock = {"name": "Bed Bath & Beyond, Inc.", "tickers": ["BBBY"], "older_pages": ["p"],
                 "former_names": [{"name": "Beyond, Inc.", "from": "2023-11-06", "to": "2025-08-20"},
                                  {"name": "OVERSTOCK COM INC", "from": "2002-01-01", "to": "2023-11-06"}],
                 "periodic_dates": ["2026-08-01"], "filing_dates": ["2026-08-01"]}
    old = {"name": "20230930-DK-Butterfly-1, Inc.", "tickers": [], "older_pages": ["p"],
           "former_names": [{"name": "BED BATH & BEYOND INC", "from": "1994-01-01", "to": "2023-10-04"}],
           "periodic_dates": ["2023-01-10"], "filing_dates": ["2023-01-10", "2026-02-01"]}
    assert sm.name_valid(old, "bed bath and beyond", "2012-01-06", "2023-04-18")
    assert not sm.name_valid(overstock, "bed bath and beyond", "2012-01-06", "2023-04-18")
    profiles = {1130713: overstock, 886158: old}
    cands = sm.Candidates()
    cands.add("BBBY", 1130713, "sec_company_tickers_exchange")
    cands.add("BBBY", 886158, "repo_sec_cik_map_2020.csv")
    index = sm.build_name_index(profiles)
    assert sm.resolve_listing("BBBY", "Bed Bath & Beyond Inc. - Common Stock", cands, profiles, index,
                              "2010-12-31", "2023-04-18") == (886158, "name+ticker")


def test_symbol_only_rows_borrow_the_nearest_name():
    snaps = pd.DataFrame({"symbol": ["AAL", "AAL", "ZZZ"], "name": ["American Airlines Group, Inc. - Common Stock", "AAL", "ZZZ"],
                          "date": ["2019-06-11", "2019-06-17", "2019-06-17"]})
    out = sm.fill_symbol_only_names(snaps)
    assert out["name"].tolist() == ["American Airlines Group, Inc. - Common Stock",
                                    "American Airlines Group, Inc. - Common Stock", ""]


def test_bare_rows_take_the_class_of_the_nearest_typed_row():
    rows = pd.DataFrame({"cik": 1288776, "symbol": "GOOG",
                         "name": ["Google Inc. - Class A Common Stock", "Google Inc.", "Google Inc. - Class C Capital Stock",
                                  "Google Inc."],
                         "date": ["2013-10-31", "2013-12-01", "2014-07-11", "2015-01-10"]})
    assert sm.fill_classes(rows).tolist() == ["A", "A", "C", "C"]


def test_a_name_handed_from_one_cik_to_another_is_split_by_date():
    # IAC/InterActiveCorp: CIK 891103 until mid-2020 (then Match Group), CIK 1800227 after.
    old = {"name": "Match Group, Inc.", "tickers": ["MTCH"], "older_pages": ["p"],
           "former_names": [{"name": "IAC/INTERACTIVECORP", "from": "2003-01-01", "to": "2020-06-30"}],
           "periodic_dates": ["2017-03-01", "2019-03-01", "2021-03-01", "2026-08-01"],
           "filing_dates": ["2017-03-01", "2019-03-01", "2021-03-01", "2026-08-01"]}
    new = {"name": "IAC Inc.", "tickers": ["IAC"], "older_pages": [],
           "former_names": [{"name": "IAC/InterActiveCorp", "from": "2020-06-30", "to": "2021-08-01"},
                            {"name": "IAC Holdings, Inc.", "from": "2019-12-01", "to": "2020-06-30"}],
           "periodic_dates": ["2020-08-01", "2026-08-01"], "filing_dates": ["2019-12-20", "2026-08-01"]}
    profiles = {891103: old, 1800227: new}
    cands = sm.Candidates()
    cands.add("IAC", 1800227, "sec_company_tickers_exchange")
    index = sm.build_name_index(profiles)
    name = "IAC/InterActiveCorp - Common Stock"
    assert sm.resolve_listing("IAC", name, cands, profiles, index, "2018-01-19", "2021-06-01") == (None, "ambiguous")
    out = sm.resolve_by_date("IAC", name, ["2018-01-19", "2019-06-17", "2020-09-01", "2021-06-01"], cands, profiles, index)
    assert out == {"2018-01-19": 891103, "2019-06-17": 891103, "2020-09-01": 1800227, "2021-06-01": 1800227}


def test_nasdaq_tickers_around_a_filing():
    intervals = pd.DataFrame({"cik": [1, 1, 1], "ticker": ["LSXMA", "FWONA", "FWONA"],
                              "start": pd.to_datetime(["2020-01-01", "2020-01-01", "2024-09-20"]),
                              "end": pd.to_datetime(["2024-09-01", "2024-09-01", "2026-07-01"])})
    assert f25.nasdaq_tickers_around(intervals, 1, "2024-09-09") == (["FWONA", "LSXMA"], ["LSXMA"], [])
    assert f25.nasdaq_tickers_around(intervals, 1, "2024-09-09", {"LSXMA"}) == (["FWONA"], [], [])
    assert f25.nasdaq_tickers_around(intervals, 2, "2024-09-09") == ([], [], [])
    change = pd.DataFrame({"cik": [5, 5], "ticker": ["DISCA", "WBD"],
                           "start": pd.to_datetime(["2018-01-01", "2022-04-11"]),
                           "end": pd.to_datetime(["2022-04-01", "2026-07-01"])})
    assert f25.nasdaq_tickers_around(change, 5, "2022-04-08") == (["DISCA"], ["DISCA"], ["WBD"])
    assert f25.classify_row("common", "x", {"still_on_nasdaq_same_cik": True, "tickers_before": "DISCA",
                                            "tickers_ended": "DISCA", "tickers_new": "WBD"})[0] == "reorg"


def test_merge_intervals_joins_families_within_the_gap():
    base = {"security_id": "1", "ticker": "ABC", "exchange": "NASDAQ", "source_url": "u", "cik": 1,
            "share_class": "COMMON", "start_prev_absent": "", "end_next_absent": "", "name_in_source": "n"}
    evidence = pd.DataFrame([
        {**base, "start": "2012-01-01", "end": "2014-06-01", "source": "wayback_companylist", "n_snapshots": 30, "match": "name+ticker"},
        {**base, "start": "2013-02-19", "end": "2013-02-19", "source": "wayback_symdir", "n_snapshots": 1, "match": "name+lookup"},
        {**base, "start": "2014-08-01", "end": "2016-01-01", "source": "wayback_symdir", "n_snapshots": 9, "match": "name+ticker"},
        {**base, "start": "2018-01-19", "end": "2026-07-01", "source": "repo_symdir", "n_snapshots": 300, "match": "name+ticker"},
    ])
    out = sm.merge_intervals(evidence)
    assert out[["start", "end", "n_snapshots", "match", "n_evidence_rows"]].values.tolist() == [
        ["2012-01-01", "2016-01-01", 40, "name+lookup", 3], ["2018-01-19", "2026-07-01", 300, "name+ticker", 1]]
    assert out.iloc[0]["sources"] == "wayback_companylist wayback_symdir"
