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
    ("Depository Receipt", "receipt"),
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
    new_same = {"cik": 5, "tickers": ["ABC"], "new": True, "names_match": True}
    assert f25.classify_row("common", "x", {"successor": new_same})[0] == "reorg"
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
    assert f25.subject_evidence(1, "2014-06-01", payload) == {
        "periodic_after_400d": 0, "tickers_elsewhere": "", "sec_nasdaq_tickers": {"XX"}, "current_tickers": "XX:Nasdaq"}


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
        "cik": [1, 2, 3], "ticker": ["ABC", "ABC", "ABC"], "exchange": "NASDAQ", "n_snapshots": 100,
        "start": ["2018-01-19", "2020-05-08", "2023-01-01"], "end": ["2020-04-28", "2026-07-01", "2026-07-01"]})
    names = {1: ["Abcam Holdings Inc"], 2: ["ABCAM HOLDINGS CORP NEW"], 3: ["Abc Unrelated Inc"]}
    snapshots = ["2020-04-28", "2020-05-08", "2023-01-01"]
    assert f25.successor_evidence(rows, intervals, names, snapshots) == {
        "acc1": {"cik": 2, "tickers": ["ABC"], "new": True, "names_match": True}}
    names[2] = ["Different Co"]
    assert f25.successor_evidence(rows, intervals, names, snapshots)["acc1"]["names_match"] is False


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


def test_snapshot_evidence_around_a_filing():
    days = _monthly("2024-01-01", "2026-07-01")
    rows = _raw([("LSXMA", d, 1) for d in days if d <= "2024-09-01"] + [("FWONA", d, 1) for d in days]
                + [("DISCA", d, 5) for d in days if d <= "2024-09-01"] + [("WBD", d, 5) for d in days if d >= "2024-10-01"])
    snap = f25.SnapshotEvidence(rows, {"repo_symdir": days})
    ev = f25.snapshot_evidence(snap, 1, "2024-09-09")
    assert (ev["tickers_before"], ev["tickers_ended"], ev["tickers_continued"]) == ("FWONA LSXMA", "LSXMA", "FWONA")
    assert f25.snapshot_evidence(snap, 1, "2024-09-09", {"LSXMA"})["tickers_before"] == "FWONA"
    assert f25.snapshot_evidence(snap, 2, "2024-09-09")["snapshot_continues"] is None  # no rows: no evidence
    change = f25.snapshot_evidence(snap, 5, "2024-09-20")
    assert (change["tickers_before"], change["tickers_ended"], change["tickers_new"]) == ("DISCA", "DISCA", "WBD")
    assert f25.classify_row("common", "x", {**change, "still_on_nasdaq_same_cik": True})[0] == "reorg"




# ------------------------------------------------------------------ round-1 review regressions: Form 25

@pytest.mark.parametrize("text,kind", [
    # A filing that removes the common stock together with other classes is a common delisting.
    ("Common stock and preferred stock", "common"),  # SVB 0001354457-23-000327
    ("Common Stock, 3.600% Notes due 2025, 2.125% Notes due 2026", "common"),  # Walgreens 0001354457-25-000854
    ("Common Stock & Associated Purchase Rights", "common"),  # Life Technologies
    ("Common Stock & Perpetual Preferred Series A Fixed-to-floating Rate", "common"),  # People's United
    ("Common Stock & Depositary Shares Each Representing a 1/40th Interest in a Share of 7.75% Fixed Rate "
     "Non-Cumulative Perpetual Preferred Stock, Series A", "common"),  # PacWest
    ("Common Stock & Depositary Shares representing 5.70% Series C Non-Cumulative Preferred Stock", "common"),  # TCF
    ("Common Stock, Depositary Shares, each representing a 1/400th ownership interest in a share of 7.00% "
     "Fixed-Rate Reset Non-Cumulative Perpetual Preferred Stock, Series E", "common"),  # Heartland
    ("Class A Common Stock, Class C Common Stock, and Series A Cumulative Redeemable Preferred Stock", "common"),  # LBRDA/K
    ("Common Stock & Contingent Value Right", "common"),  # Wright Medical
    ("Common Stock and Warrants", "common"),  # US Ecology
    ("Common Stock & Warrant", "common"),  # Velodyne
    ("Common Stock, Warrant", "common"),  # EQRx
    ("Common stock and warrants", "common"),  # Latch
    ("Class A Common Stock, 7.75% Senior Notes due 2033", "common"),  # Cowen
    ("American depositary shares and warrant", "ads"),
    ("Common Stock (par value $.05 per share) with associated Series A Junior Preferred Stock Purchase Rights", "common"),
    ("Units with one share of common stock and one warrant", "unit"),
    ("Unit, Warrant, and Class A Common Stock", "common"),  # a SPAC removing all three
    # The common stock inside another class's description is not a listed item.
    ("Units, each consisting of one share of Class A common stock and one-half of one warrant", "unit"),
    ("Unit consisting of One Common Stock and Half of a Warrant", "unit"),
    ("Units; consisting of 1 share of common stock and a 3 year warrant", "unit"),
    ("Warrants to purchase Common Stock", "warrant"),
    ("Warrants, each whole warrant exercisable for one share of Class A Common Stock at an exercise price of $11.50", "warrant"),
    ("Rights to purchase a share of Liberty Ventures Series A Common Stock", "right"),
    ("Subscription Rights to Purchase Shares of Series C Common Stock", "right"),
    ("Common Stock Purchase Rights", "right"),
    ("Debentures convertible into common stock", "debt"),
    ("Common Units Representing Limited Partner Interests", "lp_units"),
    ("Common Units, Representing Limited Partner Interests, 8.0% Series A Cumulative Redeemable Preferred Units", "unit"),
    ("iShares Dow Jones US Index Fund, Shares of Beneficial Interest", "fund"),
    ("7% Cumulative Trust Preferred Securities - SVB Capital II", "preferred"),
])
def test_multi_item_descriptions(text, kind):
    assert f25.classify_security_class(text) == kind


def test_class_kinds_lists_every_item():
    assert f25.class_kinds("Common Stock, 3.600% Notes due 2025, 2.125% Notes due 2026") == ["common", "debt", "debt"]
    assert f25.class_kinds("Common stock and preferred stock") == ["common", "preferred"]
    assert f25.class_kinds("Units, each consisting of one share of Class A common stock and one-half of one warrant") == ["unit"]


def test_depository_receipt_is_a_preferred_for_a_domestic_filer():
    # Stericycle 2018-09-14 'Depository Receipt' was the SRCLP mandatory convertible preferred.
    assert f25.classify_security_class("Depository Receipt") == "receipt"
    assert f25.resolve_receipt("receipt", "N") == "preferred"
    assert f25.resolve_receipt("receipt", "Y") == "ads"
    assert f25.resolve_receipt("receipt", "Y", "ABCDP") == "preferred"
    assert f25.resolve_receipt("common", "Y") == "common"


def test_float_review_band_and_failures():
    assert f25.check_float_units(4.57e11, 3.0e8) == "review"  # $1,523 a share: real for a few names, mostly x1000
    assert f25.check_float_units(4.57e11, 3.0e7) == "implied_per_share_high"
    assert f25.check_float_units(1e9, 5e7) == "ok"


def _facts(rows):
    return pd.DataFrame([{"cik": 7, "end": pd.Timestamp(e), "val": v, "accn": a, "period": "", "entity_name": ""}
                         for e, v, a in rows])


def test_float_units_are_checked_per_fact_before_the_maximum():
    # CenterState-like: the largest fact is a x1000 error ($2.3T); a plausible $1.96B fact in the window is kept.
    floats = _facts([("2016-06-30", 1.2e9, "a"), ("2017-06-30", 2.3e12, "b"), ("2018-06-30", 1.96e9, "c")])
    shares = _facts([("2016-08-01", 4.8e7, "a"), ("2017-08-01", 6.0e7, "b"), ("2018-08-01", 9.5e7, "c")])
    out = f25.float_for_filing(floats, shares, 7, "2019-03-01")
    assert out["public_float_usd"] == 1.96e9 and out["float_accession"] == "c" and out["float_check_flag"] == "ok"
    assert out["float_obs_3y"] == 3 and out["float_facts_dropped"] == 1


def test_a_review_fact_far_above_the_median_is_dropped():
    # Crosstex-like: $457B at $1,500 a share against floats near $1B: dropped; the clean maximum is kept.
    floats = _facts([("2015-06-30", 9e8, "a"), ("2016-06-30", 4.57e11, "b"), ("2017-06-30", 1.1e9, "c")])
    shares = _facts([("2015-08-01", 3e8, "a"), ("2016-08-01", 3e8, "b"), ("2017-08-01", 3e8, "c")])
    out = f25.float_for_filing(floats, shares, 7, "2018-03-01")
    assert out["public_float_usd"] == 1.1e9 and out["float_facts_dropped"] == 1 and out["float_check_flag"] == "ok"
    # Alone in its window and history, the same fact is kept but flagged for review.
    alone = f25.float_for_filing(floats[floats["accn"] == "b"], shares, 7, "2018-03-01")
    assert alone["public_float_usd"] == 4.57e11 and alone["float_check_flag"] == "review"


def test_the_median_comes_from_clean_facts_when_errors_repeat():
    # Paratek 2019-2021: three x1000 facts in a row ($127B, $231B, $315B) around clean $293M and $101M.
    floats = _facts([("2018-06-30", 2.94e8, "a"), ("2019-06-30", 1.28e11, "b"), ("2020-06-30", 2.31e11, "c"),
                     ("2021-06-30", 3.16e11, "d"), ("2022-06-30", 1.01e8, "e")])
    shares = _facts([("2018-08-01", 3.0e7, "a"), ("2019-08-01", 3.3e7, "b"), ("2020-08-01", 4.6e7, "c"),
                     ("2021-08-01", 4.9e7, "d"), ("2022-08-01", 5.5e7, "e")])
    out = f25.float_for_filing(floats, shares, 7, "2023-09-21")
    assert out["public_float_usd"] == 1.01e8 and out["float_check_flag"] == "ok" and out["float_facts_dropped"] == 1


def test_a_clean_fact_is_kept_however_large_the_growth():
    # A SPAC's float grows 25-fold at its merger; a fact that passes the per-share check is never dropped.
    floats = _facts([("2020-06-30", 3e8, "a"), ("2021-06-30", 8e9, "b")])
    shares = _facts([("2020-08-01", 3e7, "a"), ("2021-08-01", 4e8, "b")])
    out = f25.float_for_filing(floats, shares, 7, "2022-03-01")
    assert out["public_float_usd"] == 8e9 and out["float_facts_dropped"] == 0


def test_every_fact_failing_reports_the_largest_with_its_flag():
    floats = _facts([("2017-06-30", 4.8e12, "a")])
    shares = _facts([("2017-08-01", 1.2e7, "a")])
    out = f25.float_for_filing(floats, shares, 7, "2018-03-01")
    assert out["float_check_flag"] == "implied_per_share_high" and out["float_facts_dropped"] == 1


def _iv(rows):
    return pd.DataFrame([{"cik": r[0], "ticker": r[1], "start": r[2], "end": r[3], "exchange": "NASDAQ",
                          "n_snapshots": r[4] if len(r) > 4 else 50} for r in rows])


def test_a_holding_company_handover_with_a_snapshot_gap_is_found():
    # Broadcom Ltd (1649338) kept AVGO until 2018-05-08; Broadcom Inc (1730168) first seen 2018-05-21,
    # the next snapshot, 47 days after the 2018-04-04 filing.
    rows = pd.DataFrame({"accession": ["acc"], "subject_cik": [1649338], "filing_date": ["2018-04-04"]})
    intervals = _iv([(1649338, "AVGO", "2016-02-26", "2018-05-08"), (1730168, "AVGO", "2018-05-21", "2026-08-01")])
    names = {1649338: ["Broadcom Ltd", "Pavonia Ltd"], 1730168: ["Broadcom Inc."]}
    found = f25.successor_evidence(rows, intervals, names, ["2018-05-08", "2018-05-21", "2018-06-01"])
    assert found == {"acc": {"cik": 1730168, "tickers": ["AVGO"], "new": True, "names_match": True}}
    # With a snapshot in between that lacks the ticker, the next holder is not a handover.
    assert f25.successor_evidence(rows, intervals, names, ["2018-05-08", "2018-05-14", "2018-05-21"]) == {}


def test_stray_rows_and_late_handovers_are_no_successor():
    # BRCM: one lagging company-list row gave the ticker to Broadcom Ltd the day after Broadcom Corp's Form 25.
    rows = pd.DataFrame({"accession": ["brcm"], "subject_cik": [1054374], "filing_date": ["2016-02-01"]})
    intervals = _iv([(1054374, "BRCM", "2010-12-31", "2016-01-29"), (1649338, "BRCM", "2016-02-02", "2016-02-02", 1),
                     (1649338, "AVGO", "2016-02-02", "2018-03-29")])
    names = {1054374: ["BROADCOM CORP"], 1649338: ["Broadcom Ltd"]}
    assert f25.successor_evidence(rows, intervals, names, ["2016-01-29", "2016-02-02", "2016-02-05"]) == {}
    # Liberty Media 2016-04-18: BATRA passed to Atlanta Braves Holdings in 2023, not at this filing.
    rows = pd.DataFrame({"accession": ["lm"], "subject_cik": [1560385], "filing_date": ["2016-04-18"]})
    intervals = _iv([(1560385, "BATRA", "2016-04-24", "2023-07-08"), (1958140, "BATRA", "2023-07-28", "2026-08-01")])
    names = {1560385: ["Liberty Media Corp"], 1958140: ["Atlanta Braves Holdings, Inc."]}
    assert f25.successor_evidence(rows, intervals, names, ["2023-07-08", "2023-07-28"]) == {}
    # Randgold: Barrick in three company-list rows within a year is no successor.
    rows = pd.DataFrame({"accession": ["gold"], "subject_cik": [1175580], "filing_date": ["2018-12-31"]})
    intervals = _iv([(1175580, "GOLD", "2010-12-31", "2018-11-21"), (756894, "GOLD", "2019-01-03", "2019-01-04", 2),
                     (756894, "GOLD", "2019-06-11", "2019-06-11", 1)])
    assert f25.successor_evidence(rows, intervals, {1175580: ["RANDGOLD RESOURCES LTD"], 756894: ["BARRICK GOLD CORP"]},
                                  ["2018-11-21", "2019-01-03"]) == {}
    # Sonus: Ribbon held SONS for a fortnight, then listed as RBBN; it is the successor.
    rows = pd.DataFrame({"accession": ["sons"], "subject_cik": [1105472], "filing_date": ["2017-10-27"]})
    intervals = _iv([(1105472, "SONS", "2010-12-31", "2017-10-17"), (1708055, "SONS", "2017-10-31", "2017-11-15", 2),
                     (1708055, "RBBN", "2017-11-28", "2026-08-01", 300)])
    found = f25.successor_evidence(rows, intervals, {1105472: ["Sonus, Inc."], 1708055: ["Ribbon Communications Inc."]},
                                   ["2017-10-17", "2017-10-31"])
    assert found["sons"]["cik"] == 1708055 and found["sons"]["new"]
    # An issuer's own withdrawal with a handover is a move, not a reorganisation (MSG 2015 to NYSE).
    assert f25.classify_row("common", "issuer_withdrawal", {"successor": found["sons"]})[0] == "common_delisting"


def test_google_to_alphabet_is_reorg_review_and_an_acquirer_is_a_delisting():
    rows = pd.DataFrame({"accession": ["goog"], "subject_cik": [1288776], "filing_date": ["2015-10-02"]})
    intervals = _iv([(1288776, "GOOG", "2014-04-03", "2015-10-01"), (1288776, "GOOGL", "2014-04-03", "2015-10-01"),
                     (1652044, "GOOG", "2015-10-05", "2026-08-01"), (1652044, "GOOGL", "2015-10-05", "2026-08-01")])
    names = {1288776: ["GOOGLE INC."], 1652044: ["Alphabet Inc."]}
    found = f25.successor_evidence(rows, intervals, names, ["2015-10-01", "2015-10-05"])["goog"]
    assert found["tickers"] == ["GOOG", "GOOGL"] and found["new"] and not found["names_match"]
    label, _ = f25.classify_row("common", "substituted_merger_or_exchange", {"successor": found})
    assert label == "reorg_review"
    assert f25.subject_exit(label, {"successor": found}) == "Y"
    # Tornier, listed as TRNX since 2011, took Wright Medical's WMGI: a delisting of Wright Medical.
    rows = pd.DataFrame({"accession": ["wmgi"], "subject_cik": [1], "filing_date": ["2015-10-01"]})
    intervals = _iv([(1, "WMGI", "2011-01-01", "2015-09-30"), (2, "TRNX", "2011-02-01", "2015-09-30"),
                     (2, "WMGI", "2015-10-02", "2020-11-10")])
    found = f25.successor_evidence(rows, intervals, {1: ["WRIGHT MEDICAL GROUP INC"], 2: ["Wright Medical Group N.V."]},
                                   ["2015-09-30", "2015-10-02"])["wmgi"]
    assert not found["new"]
    assert f25.classify_row("common", "substituted_merger_or_exchange", {"successor": found})[0] == "common_delisting"


def test_a_same_cik_reorganisation_is_no_exit():
    ev = {"still_on_nasdaq_same_cik": True, "tickers_before": "ULTA", "tickers_ended": ""}
    label, _ = f25.classify_row("common", "substituted_merger_or_exchange", ev)
    assert label == "reorg" and f25.subject_exit(label, ev) == "N"
    assert f25.subject_exit("common_delisting", {"tickers_before": "MOLX MOLXA", "tickers_ended": "MOLX MOLXA"}) == "Y"
    assert f25.subject_exit("common_delisting", {"tickers_before": "LSXMA FWONA", "tickers_ended": "LSXMA"}) == "N"
    # Mylan Inc 2015-02-27: the intervals still gave MYL to the old CIK until 2015-08-10; it is an exit.
    assert f25.subject_exit("common_delisting", {"tickers_before": "MYL", "tickers_ended": ""}) == "Y"


ORACLE_25 = """<html><body><p>FORM 25</p><p>NOTIFICATION OF REMOVAL FROM LISTING AND/OR REGISTRATION</p>
<p>Commission File Number 001-35992</p><p>Oracle Corporation / The NASDAQ Stock Market LLC</p>
<p>(Exact name of Issuer as specified in its charter, and name of Exchange where security is listed and/or registered)</p>
<p>500 Oracle Parkway, Redwood City, California 94065</p>
<p>(Address, including zip code, and telephone number, including area code, of Issuer&#8217;s principal executive offices)</p>
<p>Common stock, par value $0.01 per share</p><p>(Description of class of securities)</p>
<p>Please place an X in the box to designate the rule provision relied upon:</p>
<p>&#168; 17 CFR 240.12d2-2(a)(1)</p><p>&#168; 17 CFR 240.12d2-2(a)(3)</p>
<p>&#168; Pursuant to 17 CFR 240.12d2-2(b), the Exchange has complied</p>
<p>x Pursuant to 17 CFR 240.12d2-2(c), the Issuer has complied with the rules of the Exchange</p>
<p>Pursuant to the requirements of the Securities Exchange Act of 1934, Oracle Corporation certifies</p></body></html>"""


def test_issuer_form25_cover_is_parsed():
    doc = f25.parse_issuer_form25(ORACLE_25)
    assert doc["on_nasdaq"] and doc["class_of_security"] == "Common stock, par value $0.01 per share"
    assert doc["rule_provision"] == "17 CFR 240.12d2-2(c)" and doc["rule_parse"] == "checked_box"
    assert f25.delisting_basis(doc["rule_provision"]) == "issuer_withdrawal"
    nyse = ORACLE_25.replace("Oracle Corporation / The NASDAQ Stock Market LLC", "Issuer: VIMPELCOM LTD. Exchange: New York Stock Exchange")
    nyse = nyse.replace("certifies", "certifies. This delisting is in connection with its move to The NASDAQ Stock Market")
    assert not f25.parse_issuer_form25(nyse)["on_nasdaq"]  # removed from NYSE, moving to Nasdaq


def test_issuer_filings_keep_primary_documents_once():
    hits = [{"_id": "0001193125-13-289328:d567579d25.htm",
             "_source": {"ciks": ["0001341439"], "display_names": ["ORACLE CORP  (ORCL)  (CIK 0001341439)"],
                         "file_date": "2013-07-12", "form": "25", "file_type": "25"}},
            {"_id": "0001193125-13-289328:ex99.htm",
             "_source": {"ciks": ["0001341439"], "display_names": [], "file_date": "2013-07-12", "form": "25",
                         "file_type": "EX-99.1"}}]
    rows = f25.issuer_filings(hits)
    assert len(rows) == 1 and rows[0]["subject_cik"] == 1341439 and rows[0]["filer_cik"] == 1341439
    assert rows[0]["subject_tickers_sec"] == "ORCL" and rows[0]["document"] == "d567579d25.htm"


def test_an_issuer_withdrawal_with_a_new_exchange_is_a_transfer():
    ev = {"transfer_hint": "Form 8-A12B filed 2013-07-10 (registration on another exchange)", "tickers_before": "ORCL"}
    assert f25.classify_row("common", "issuer_withdrawal", ev)[0] == "transfer"
    assert f25.classify_row("common", "issuer_withdrawal", {"tickers_before": "VSB"})[0] == "common_delisting"


# ------------------------------------------------------------------ round-1 review regressions: security master

@pytest.mark.parametrize("edgar,listed", [
    ("PRICE T ROWE GROUP INC", "T. Rowe Price Group, Inc. - Common Stock"),
    ("HUNT J B TRANSPORT SERVICES INC", "J.B. Hunt Transport Services, Inc. - Common Stock"),
    ("SANFILIPPO JOHN B & SON INC", "John B. Sanfilippo & Son, Inc. - Common Stock"),
    ("BANK JOS A CLOTHIERS INC /DE/", "Jos. A. Bank Clothiers, Inc. - Common Stock"),
    ("SCHULMAN A INC", "A. Schulman, Inc. - Common Stock"),
    ("ARDEN ELIZABETH INC", "Elizabeth Arden, Inc. - Common Stock"),
    ("OZARKS CORP, BANK OF", "Bank of the Ozarks Corp - Common Stock"),
    ("PMC SIERRA INC", "PMC - Sierra, Inc. - Common Stock"),
])
def test_inverted_edgar_names_match(edgar, listed):
    a, b = sm.normalize_issuer_name(edgar), sm.normalize_issuer_name(listed)
    assert sm.names_match(a, b, reorder=True)
    reordered_only = a.split() != b.split() and sorted(a.split()) == sorted(b.split())
    if sm.reorder_allowed(a.split()) or not reordered_only:  # initials, or no reordering needed
        assert sm.names_match(a, b)
        assert sm.names_in(sm.build_name_index({1: _profile(edgar)}), b) == {1}
    else:
        # A reordering without initials (a person's name, 'Bank of the Ozarks') needs ticker evidence:
        # through SEC's name list alone 'First Bank' would be Bank First Corp (round-2 review, FRBA).
        assert not sm.names_match(a, b)
        assert sm.names_in(sm.build_name_index({1: _profile(edgar)}), b) == set()
        cands = sm.Candidates()
        cands.add("TICK", 1, "repo_historical_ticker_ciks")
        profiles = {1: _issuer(edgar, ["TICK"])}
        assert sm.resolve_listing("TICK", listed, cands, profiles, sm.build_name_index(profiles),
                                  "2014-01-02", "2016-01-04") == (1, "name+ticker")


def test_spaced_hyphen_is_cut_only_before_a_security_phrase():
    assert sm.normalize_issuer_name("PMC - Sierra, Inc. - Common Stock") == "pmc sierra"
    assert sm.normalize_issuer_name("Liberty Media Corporation - Series C Liberty SiriusXM Common Stock") == "liberty media"
    assert sm.normalize_issuer_name("Hennessy Capital Investment Corp. IV - Units") == "hennessy capital investment 4"
    assert sm.normalize_issuer_name("HUNT J B TRANSPORT SERVICES INC") == "hunt jb transport services"
    assert not sm.names_match("first american", "american first financial")


def _issuer(name, tickers=(), entity="operating", periodic=("2012-03-01", "2026-03-01"), former=()):
    return {"name": name, "former_names": list(former), "tickers": list(tickers), "entity_type": entity,
            "older_pages": [], "periodic_dates": list(periodic), "filing_dates": list(periodic)}


@pytest.mark.parametrize("symbol,listed,right,wrong", [
    ("TROW", "T. Rowe Price Group, Inc. - Common Stock",
     (1113169, "PRICE T ROWE GROUP INC"), (1214556, "T ROWE PRICE GROUP INC")),
    ("JBHT", "J.B. Hunt Transport Services, Inc. - Common Stock",
     (728535, "HUNT J B TRANSPORT SERVICES INC"), (1501362, "J.B. Hunt Transport, Inc.")),
    ("JBSS", "John B. Sanfilippo & Son, Inc. - Common Stock",
     (880117, "SANFILIPPO JOHN B & SON INC"), (889867, "JOHN B SANFILIPPO & SON INC")),
])
def test_the_issuer_beats_a_same_name_entity_from_the_name_list(symbol, listed, right, wrong):
    # The wrong CIKs have entity type 'other' and no 10-K, 10-Q, 20-F or 40-F.
    profiles = {right[0]: _issuer(right[1], [symbol]), wrong[0]: _issuer(wrong[1], entity="other", periodic=())}
    cands = sm.Candidates()
    cands.add(symbol, right[0], "sec_company_tickers_exchange")
    cands.add(symbol, wrong[0], "sec_cik_lookup")
    index = sm.build_name_index(profiles)
    lookup = {k: {wrong[0]} for k in sm.index_keys(sm.normalize_issuer_name(wrong[1]))}
    assert sm.resolve_listing(symbol, listed, cands, profiles, index, "2011-01-01", "2026-07-01", lookup) == (
        right[0], "name+ticker")
    # Even when only the non-issuer is a ticker candidate, the issuer bearing the name wins.
    only = sm.Candidates()
    only.add(symbol, wrong[0], "sec_cik_lookup")
    assert sm.resolve_listing(symbol, listed, only, profiles, index, "2011-01-01", "2026-07-01", lookup) == (
        right[0], "name_only")
    # With no issuer of that name at all, a distinctive name keeps the non-filer, labelled as such
    # (Signature Bank and Hingham file with the FDIC, not SEC).
    alone = {wrong[0]: profiles[wrong[0]]}
    assert sm.resolve_listing(symbol, listed, only, alone, sm.build_name_index(alone), "2011-01-01", "2026-07-01",
                              lookup) == (wrong[0], "name+lookup_nonfiler")


def test_a_non_filer_never_beats_a_mapped_issuer():
    # JBHT 2024-01-26 'J B Hunt Transport': the subsidiary 1501362 must not take the pair from 728535.
    profiles = {728535: _issuer("HUNT J B TRANSPORT SERVICES INC", ["JBHT"], periodic=("2012-03-01", "2024-02-15")),
                1501362: _issuer("J.B. Hunt Transport, Inc.", entity="other", periodic=())}
    cands = sm.Candidates()
    cands.add("JBHT", 728535, "sec_company_tickers_exchange")
    cands.add("JBHT", 1501362, "sec_cik_lookup")
    index = sm.build_name_index(profiles)
    assert sm.resolve_listing("JBHT", "J B Hunt Transport", cands, profiles, index, "2024-01-26", "2024-02-27") == (
        728535, "ticker_only")


def test_large_filers_report_before_their_recent_block():
    # Comcast's recent submissions block starts years after 2015; its older pages are not fetched.
    comcast = {**_issuer("COMCAST CORP", ["CMCSA"], periodic=("2022-02-01", "2026-07-30")),
               "older_pages": [{"name": "p1"}], "coverage_start": "2021-06-01"}
    assert sm.reported_during(comcast, "2010-12-31", "2015-12-10")
    assert not sm.periodic_during(comcast, "2010-12-31", "2015-12-10")
    cands = sm.Candidates()
    cands.add("CMCSK", 1166691, "sec_cik_lookup")
    profiles = {1166691: comcast}
    assert sm.resolve_listing("CMCSK", "Comcast Corporation - Class A Special Common Stock", cands, profiles,
                              sm.build_name_index(profiles), "2010-12-31", "2015-12-10") == (1166691, "name+lookup")


def test_short_names_from_the_name_list_need_one_reporting_issuer():
    # Conn's ('conns'), Ebix, Zix: one-token names found only through SEC's name list.
    profiles = {1223389: _issuer("CONNS INC", periodic=("2011-03-01", "2024-04-18"))}
    cands = sm.Candidates()
    cands.add("CONN", 1223389, "sec_cik_lookup")
    index = sm.build_name_index(profiles)
    assert sm.resolve_listing("CONN", "Conn's, Inc. - Common Stock", cands, profiles, index,
                              "2010-12-31", "2024-07-10") == (1223389, "name+lookup")
    profiles[2] = _issuer("Conns Holdings Inc", periodic=("2015-01-01", "2020-01-01"))
    profiles[3] = _issuer("CONNS CORP", periodic=("2012-01-01", "2023-01-01"))
    cands.add("CONN", 3, "sec_cik_lookup")
    assert sm.resolve_listing("CONN", "Conn's, Inc. - Common Stock", cands, profiles, sm.build_name_index(profiles),
                              "2010-12-31", "2024-07-10") == (None, "ambiguous")
    # A non-filer never takes a short name.
    other = {9: _issuer("CONNS INC", entity="other", periodic=())}
    lone = sm.Candidates()
    lone.add("CONN", 9, "sec_cik_lookup")
    assert sm.resolve_listing("CONN", "Conn's, Inc. - Common Stock", lone, other, sm.build_name_index(other),
                              "2010-12-31", "2024-07-10")[0] is None


def test_an_inverted_form25_subject_resolves_by_name():
    # A. Schulman (87565): no ticker map; the EDGAR name is inverted.
    profiles = {87565: _issuer("SCHULMAN A INC", periodic=("2011-10-31", "2018-06-28"))}
    index = sm.build_name_index(profiles)
    assert sm.resolve_listing("SHLM", "A. Schulman, Inc. - Common Stock", sm.Candidates(), profiles, index,
                              "2011-01-01", "2018-08-20") == (87565, "name_only")


def test_short_or_truncated_names_give_no_name_match():
    # Truncated repo names ('G' for G-III, 'Park' for Park-Ohio) must not resolve through the name list.
    profiles = {5: _issuer("PARK CORP", periodic=("2012-01-01", "2026-01-01")),
                6: _issuer("Heritage Oaks Bank", periodic=("2012-01-01", "2026-01-01"))}
    cands = sm.Candidates()
    cands.add("PKOH", 5, "sec_cik_lookup")
    index = sm.build_name_index(profiles)
    assert sm.resolve_listing("PKOH", "Park", cands, profiles, index, "2018-08-22", "2018-08-22")[0] is None
    assert sm.resolve_listing("HCCI", "Heritage", sm.Candidates(), profiles, index, "2018-08-22", "2018-08-22")[0] is None
    assert sm.resolve_listing("HEOP", "Heritage Oaks Bank", sm.Candidates(), profiles, index, "2018-08-22", "2018-08-22",
                              truncated=True)[0] is None


def test_a_name_valid_for_part_of_the_listing_loses_to_one_valid_throughout():
    # COHR: Coherent Inc (21510) until 2022; II-VI (820318) took the name Coherent Corp in 2022-09.
    profiles = {21510: _issuer("COHERENT INC", periodic=("2011-01-01", "2022-05-11", "2022-07-01")),
                820318: _issuer("COHERENT CORP.", ["COHR"], former=[{"name": "II-VI INC", "from": "2000-01-01",
                                                                     "to": "2022-09-08"}])}
    cands = sm.Candidates()
    cands.add("COHR", 21510, "repo_historical_ticker_ciks")
    cands.add("COHR", 820318, "sec_company_tickers_exchange")
    index = sm.build_name_index(profiles)
    assert sm.resolve_listing("COHR", "Coherent, Inc. - Common Stock", cands, profiles, index,
                              "2010-12-31", "2022-07-01") == (21510, "name+ticker")
    # A name valid for part of the dates is split by date when a rival could hold the rest of them...
    assert sm.resolve_listing("COHR", "Coherent Corp. - Common Stock", cands, profiles, index,
                              "2018-01-19", "2026-07-01") == (None, "ambiguous")
    # ...or when the candidate came from the name list only; a sole mapped holder with no rival keeps
    # the pair (ZION: EDGAR's 'ZIONS BANCORPORATION /UT/' ended 2018, 'N.A.' is not matched after).
    alone = sm.Candidates()
    alone.add("COHR", 820318, "sec_cik_lookup")
    solo = {820318: profiles[820318]}
    assert sm.resolve_listing("COHR", "Coherent Corp. - Common Stock", alone, solo, sm.build_name_index(solo),
                              "2018-01-19", "2026-07-01") == (None, "ambiguous")
    zion = {109380: _issuer("ZIONS BANCORPORATION, NATIONAL ASSOCIATION /UT/", ["ZION"], former=[
        {"name": "ZIONS BANCORPORATION /UT/", "from": "1994-03-29", "to": "2018-09-17"}])}
    mapped = sm.Candidates()
    mapped.add("ZION", 109380, "sec_company_tickers_exchange")
    assert sm.resolve_listing("ZION", "Zions Bancorporation N.A. - Common Stock", mapped, zion, sm.build_name_index(zion),
                              "2018-10-09", "2026-07-01") == (109380, "name+ticker")


def test_resolve_by_date_ends_a_cik_at_its_terminal_form25():
    # Mylan Inc (69499) kept filing after its 2015-02-27 Form 25; Mylan N.V. (1623613) took MYL.
    old = _issuer("MYLAN INC.", periodic=("2012-02-28", "2015-11-01", "2016-02-01"))
    new = _issuer("Mylan N.V.", ["MYL"], periodic=("2015-06-01", "2020-11-01"))
    new["filing_dates"] = ["2015-02-01", "2020-11-01"]
    profiles = {69499: old, 1623613: new}
    cands = sm.Candidates()
    cands.add("MYL", 69499, "repo_historical_ticker_ciks")
    cands.add("MYL", 1623613, "repo_historical_ticker_ciks")
    index = sm.build_name_index(profiles)
    days = ["2015-01-10", "2015-03-02", "2015-08-10"]
    name = "Mylan N.V. - Ordinary Shares"
    assert sm.resolve_by_date("MYL", name, days, cands, profiles, index)["2015-08-10"] == 69499  # incumbent
    out = sm.resolve_by_date("MYL", name, days, cands, profiles, index, exits={69499: "2015-02-27"})
    assert out == {"2015-01-10": 69499, "2015-03-02": 1623613, "2015-08-10": 1623613}


def _rows(records):
    return pd.DataFrame([{"symbol": s, "date": d, "cik": c, "share_class": "COMMON", "family": f, "source_url": "u",
                          "how": "name+ticker", "name": "n"} for s, d, c, f in records])


def test_intervals_bridge_a_coverage_gap():
    # No snapshot of any family between 2012-06-22 and 2012-10-24: one interval, with the gap recorded.
    dates = {"wayback_symdir": ["2012-05-01", "2012-06-22", "2012-10-24", "2012-12-01"],
             "wayback_companylist": ["2012-05-15", "2012-11-15"]}
    rows = _rows([("GOOG", d, 1288776, "wayback_symdir") for d in dates["wayback_symdir"]]
                 + [("GOOG", d, 1288776, "wayback_companylist") for d in dates["wayback_companylist"]])
    out = sm.build_intervals(rows, dates)
    assert out[["start", "end", "n_snapshots", "coverage_gap_days"]].values.tolist() == [["2012-05-01", "2012-12-01", 6, 124]]
    assert out.iloc[0]["sources"] == "wayback_companylist wayback_symdir" and out.iloc[0]["n_evidence_rows"] == 6


def test_intervals_break_on_two_absent_snapshots_or_another_holder():
    dates = {"repo_symdir": ["2018-01-01", "2018-01-02", "2018-01-03", "2018-01-04", "2018-01-05"],
             "repo_screener_300M": ["2018-01-03"]}
    present = {"2018-01-01": {"ABC"}, "2018-01-02": {"ABC"}, "2018-01-03": {"XYZ"}, "2018-01-04": {"ABC"},
               "2018-01-05": {"ABC"}}
    rows = _rows([("ABC", d, 1, "repo_symdir") for d in ["2018-01-01", "2018-01-02", "2018-01-04", "2018-01-05"]])
    assert len(sm.build_intervals(rows, dates, present)) == 1  # one missing snapshot (and the screener) tolerated
    present["2018-01-02"] = set()
    rows = rows[rows["date"] != "2018-01-02"]
    assert len(sm.build_intervals(rows, dates, present)) == 2  # missing twice in a row
    other = _rows([("ABC", d, 1, "repo_symdir") for d in ["2018-01-01", "2018-01-05"]] + [("ABC", "2018-01-03", 2, "repo_symdir")])
    out = sm.build_intervals(other, dates, {d: {"ABC"} for d in dates["repo_symdir"]})
    assert sorted(zip(out["cik"], out["start"], out["end"])) == [
        (1, "2018-01-01", "2018-01-01"), (1, "2018-01-05", "2018-01-05"), (2, "2018-01-03", "2018-01-03")]


def test_one_cik_per_ticker_and_date():
    # Company lists gave COHR to 21510 while the symbol files gave it to 820318 over the same years.
    profiles = {21510: _issuer("COHERENT INC"),
                820318: _issuer("COHERENT CORP.", former=[{"name": "II-VI INC", "from": "2000-01-01", "to": "2022-09-08"}])}
    rows = _rows([("COHR", d, 21510, "wayback_companylist") for d in ["2012-01-01", "2012-03-01", "2012-05-01"]]
                 + [("COHR", d, 820318, "wayback_symdir") for d in ["2012-02-01", "2012-04-01"]])
    rows["name"] = "Coherent, Inc."
    assert sm.ticker_conflicts(rows) == [("COHR", 21510, 820318, "2012-02-01", "2012-04-01")]
    kept, dropped = sm.resolve_ticker_conflicts(rows, profiles)
    assert dropped == 2 and set(kept.loc[kept["how"] == "conflict", "date"]) == {"2012-02-01", "2012-04-01"}
    clean = kept[kept["how"] != "conflict"].astype({"cik": int})
    assert not sm.ticker_conflicts(clean)
    dates = {"wayback_companylist": ["2012-01-01", "2012-03-01", "2012-05-01"], "wayback_symdir": ["2012-02-01", "2012-04-01"]}
    assert len(sm.build_intervals(clean, dates)) == 1
    # MTCH: alternating assignments are settled date by date by the EDGAR name each CIK bore.
    profiles = {1575189: _issuer("Match Group Holdings II, LLC", former=[
                    {"name": "Match Group, Inc.", "from": "2015-01-01", "to": "2020-07-01"}]),
                891103: _issuer("Match Group, Inc.", ["MTCH"], former=[
                    {"name": "IAC/INTERACTIVECORP", "from": "2003-01-01", "to": "2020-07-01"}])}
    rows = _rows([("MTCH", "2016-01-04", 1575189, "x"), ("MTCH", "2018-06-01", 891103, "x"),
                  ("MTCH", "2019-06-03", 1575189, "x"), ("MTCH", "2021-01-04", 891103, "x"),
                  ("MTCH", "2022-01-03", 1575189, "x")])
    rows["name"] = "Match Group, Inc. - Common Stock"
    kept, dropped = sm.resolve_ticker_conflicts(rows, profiles)
    assert kept["cik"].fillna(0).astype(int).tolist() == [1575189, 0, 1575189, 891103, 0] and dropped == 2


def test_rows_after_a_terminal_form25_go_to_the_successor_or_nobody():
    # The tail of the run that held the ticker at the filing (sightings within the lists' lag) goes to
    # the successor that took the ticker, or to nobody.
    rows = _rows([("MYL", "2015-02-20", 69499, "x"), ("MYL", "2015-03-02", 69499, "x"), ("MYL", "2015-03-20", 69499, "x"),
                  ("ABC", "2014-01-01", 5, "x"), ("ABC", "2014-01-20", 5, "x")])
    days = ["2014-01-01", "2014-01-20", "2014-02-01", "2014-03-01", "2015-02-20", "2015-03-02", "2015-03-20", "2015-04-01",
            "2015-05-01"]
    out, counts = sm.drop_rows_after_exit(rows, {69499: "2015-02-27", 5: "2014-01-05"}, {(69499, "MYL"): 1623613},
                                          days=days, full=set(days))
    assert out["cik"].tolist()[:4] == [69499, 69499, 1623613, 5] and pd.isna(out["cik"].tolist()[4])
    assert (counts["dropped"], counts["moved"], counts["contradicted"]) == (1, 1, [])
    assert out["how"].tolist()[2] == "successor" and out["how"].tolist()[4] == "after_exit"


def test_multi_class_exits_match_their_class():
    classes = {"COMMON", "A"}
    # Molex 2013-12-09: one filing for 'Common Stock' (MOLX), one for 'Class A Common Stock' (MOLXA).
    assert sm.exit_matches_class("Common Stock", "", "COMMON", {"MOLX"}, classes)
    assert not sm.exit_matches_class("Common Stock", "", "A", {"MOLXA"}, classes)
    assert sm.exit_matches_class("Class A Common Stock", "", "A", {"MOLXA"}, classes)
    assert not sm.exit_matches_class("Class A Common Stock", "", "COMMON", {"MOLX"}, classes)
    # Comcast 2015-12-11 'Class A Special Common Stock': ticker keys, matched by the ticker that ended.
    keyed = {"T-CMCSA", "T-CMCSK"}
    assert sm.exit_matches_class("Class A Special Common Stock", "CMCSK", "T-CMCSK", {"CMCSK"}, keyed)
    assert not sm.exit_matches_class("Class A Special Common Stock", "CMCSK", "T-CMCSA", {"CMCSA"}, keyed)
    # Liberty-style series wording and a filing naming two classes.
    assert sm.exit_matches_class("Series C Liberty SiriusXM Common Stock", "", "C", {"LSXMK"}, {"A", "B", "C"})
    assert sm.exit_matches_class("Class A Common Stock and Class C Capital Stock", "", "C", {"GOOG"}, {"A", "C"})
    # Generic 'Common Stock' applies to every ticker-keyed class when no ticker evidence says otherwise.
    assert sm.exit_matches_class("Common Stock", "", "T-SRCL", {"SRCL"}, {"T-SRCL", "T-SRCLX"})


def test_preferred_depositary_symbols_are_not_common():
    assert not sm.is_common_equity("Stericycle, Inc. - Depository Receipt")  # SRCLP
    assert not sm.is_common_equity("IBERIABANK Corporation - Depositary Shares Representing Series B Fixed to Floating")
    assert not sm.is_common_equity("Huntington Bancshares Incorporated - Depositary Shares")
    assert sm.is_common_equity("Celsus Therapeutics Plc - Amercan Depositary Shares")  # sic, an ADR
    assert sm.is_common_equity("Baidu, Inc. - American Depositary Shares")


def test_nan_is_never_a_ticker():
    cands = sm.Candidates()
    cands.add(float("nan"), 920033, "form25_efts_display_name")
    cands.add("nan", 920033, "x")
    assert dict(cands.map) == {}
    assert sm.text_value(float("nan")) == "" and sm.text_value(None) == "" and sm.text_value("JOSB") == "JOSB"


def test_company_list_rows_without_a_last_sale_are_no_listing_evidence_but_present(tmp_path):
    hook = tmp_path / "companylist"
    hook.mkdir()
    (hook / "nasdaq_companylist_2019-01-03.csv").write_text(
        "Symbol,Name,LastSale,MarketCap,Observed At\nAAPL,Apple Inc.,157.92,7.5E11,2019-01-03\n"
        "GNST,GenSight Inc.,,,2019-01-03\nSCCI,Some IPO Corp,n/a,n/a,2019-01-03\n")
    snaps = sm.load_listing_snapshots(tmp_path / "none", {"wayback_companylist": hook})
    assert snaps["no_last_sale"].tolist() == [False, True, True]
    assert sm.common_snapshot_rows(snaps)["symbol"].tolist() == ["AAPL"]  # no listing evidence
    assert sm.presence_by_date(snaps)["2019-01-03"] == {"AAPL", "GNST", "SCCI"}  # but no absence either


def test_truncated_file_names_borrow_the_nearest_full_name():
    snaps = pd.DataFrame({"symbol": ["COKE", "COKE", "GIII", "ABCD"],
                          "name": ["Coca-Cola Consolidated, Inc. - Common Stock", "Coca", "G", "Other Name"],
                          "date": ["2018-08-13", "2018-08-22", "2018-08-22", "2018-08-22"],
                          "name_truncated": [False, True, True, True]})
    out = sm.fill_symbol_only_names(snaps)
    assert out["name"].tolist()[:2] == ["Coca-Cola Consolidated, Inc. - Common Stock"] * 2
    assert out["name_truncated"].tolist() == [False, False, True, True]  # no full name near: stays flagged


def test_build_master_takes_delist_dates_from_delistings_only():
    profiles = {1: {**_issuer("ULTA BEAUTY INC", ["ULTA"]), **sm.parse_submissions({})},
                2: {**_issuer("GOOGLE INC."), **sm.parse_submissions({})},
                3: {**_issuer("Alphabet Inc.", ["GOOG"]), **sm.parse_submissions({})},
                4: {**_issuer("BENIHANA INC"), **sm.parse_submissions({})}}
    for cik, name in [(1, "ULTA BEAUTY INC"), (2, "GOOGLE INC."), (3, "Alphabet Inc."), (4, "BENIHANA INC")]:
        profiles[cik]["name"] = name
    iv = pd.DataFrame([
        {"security_id": "1", "cik": 1, "ticker": "ULTA", "start": "2011-01-01", "end": "2026-08-01", "share_class": "COMMON"},
        {"security_id": "2", "cik": 2, "ticker": "GOOG", "start": "2011-01-01", "end": "2015-10-01", "share_class": "COMMON"},
        {"security_id": "3", "cik": 3, "ticker": "GOOG", "start": "2015-10-05", "end": "2026-08-01", "share_class": "COMMON"},
        {"security_id": "4", "cik": 4, "ticker": "BNHN", "start": "2011-01-01", "end": "2012-08-20", "share_class": "COMMON"},
    ]).assign(source="repo_symdir", match="name+ticker", exchange="NASDAQ")
    f25_rows = pd.DataFrame([
        {"subject_cik": 1, "filing_date": "2017-01-30", "effective_date": "2017-02-09", "accession": "a1",
         "classification": "reorg", "class_kind": "common", "class_of_security": "Common Stock", "successor_cik": None},
        {"subject_cik": 2, "filing_date": "2015-10-02", "effective_date": "2015-10-12", "accession": "a2",
         "classification": "reorg_review", "class_kind": "common", "class_of_security": "Common Stock",
         "successor_cik": 3, "successor_tickers": "GOOG"},
        {"subject_cik": 4, "filing_date": "2012-08-21", "effective_date": "2012-08-31", "accession": "a4",
         "classification": "common_delisting", "class_kind": "common", "class_of_security": "Common Stock",
         "successor_cik": None},
    ])
    for column in sm.FORM25_TEXT_COLUMNS:
        f25_rows[column] = f25_rows.get(column, pd.Series([""] * len(f25_rows))).fillna("")
    f25_rows["successor_cik"] = f25_rows["successor_cik"].astype("Int64")
    prices = pd.DataFrame(columns=["ticker", "file", "first_date", "last_date", "rows", "cik", "how",
                                   "ciks_on_ticker_in_file_range"])
    master = sm.build_master(profiles, iv, iv, f25_rows, prices, set(), []).set_index("security_id")
    assert master.loc["1", "delist_date"] == ""  # ULTA: a same-CIK reorganisation
    assert master.loc["2", "delist_date"] == "" and master.loc["2", "successor_security_id"] == "3"
    assert master.loc["2", "successor_date"] == "2015-10-02"
    assert master.loc["4", "delist_date"] == "2012-08-31" and master.loc["4", "delist_form25_accession"] == "a4"
    assert "nan" not in master["first_ticker"].str.lower().tolist()


def test_the_earliest_edgar_name_is_open_at_the_start():
    # EDGAR dates MicroStrategy's first recorded name from 2018-10-25; the company bore it long before.
    mstr = _issuer("Strategy Inc", ["MSTR"], former=[{"name": "MICROSTRATEGY INC", "from": "2018-10-25", "to": "2025-08-11"}])
    mstr["older_pages"] = [{"name": "p1", "from": "1997-05-01", "to": "2019-01-01"}]
    mstr["coverage_start"] = "2019-01-02"
    assert sm.name_valid(mstr, "microstrategy", "2011-01-25", "2018-02-17")
    assert not sm.name_valid(mstr, "strategy", "2011-01-25", "2018-02-17")
    # Liberty Global plc (CIK 1570585, first filing 2013) never bore its name before 2013.
    plc = {**_issuer("Liberty Global Ltd.", ["LBTYA"], former=[{"name": "Liberty Global plc", "from": "2013-06-07",
                                                                "to": "2023-11-23"}]), "coverage_start": "2013-02-01"}
    assert not sm.name_valid(plc, "liberty global", "2010-12-31", "2012-06-01")
    assert sm.name_valid(plc, "liberty global", "2013-03-01", "2013-05-01")


def test_filler_words_and_sub_entities():
    assert sm.names_match(sm.normalize_issuer_name("Motorcar Parts of America, Inc. - Common Stock"),
                          sm.normalize_issuer_name("MOTORCAR PARTS AMERICA INC"))
    # ZION: an EDGAR sub-entity that lists the ticker does not compete with the issuer.
    profiles = {109380: _issuer("ZIONS BANCORPORATION, NATIONAL ASSOCIATION /UT/", ["ZION"]),
                1666757: _issuer("Zions Bancorporation, N.A.", ["ZION"], entity="other", periodic=())}
    cands = sm.Candidates()
    cands.add("ZION", 109380, "sec_company_tickers_exchange")
    cands.add("ZION", 1666757, "sec_submissions_tickers")
    index = sm.build_name_index(profiles)
    assert sm.resolve_listing("ZION", "Zions Bancorporation N.A. - Common Stock", cands, profiles, index,
                              "2019-05-06", "2026-07-01") == (109380, "ticker_only")


def test_edgar_name_spans_are_chained():
    # eXp: 'eXp World Holdings' is dated from 2025-02-19 although the name before it ended 2016-04-27.
    exp = {"name": "AGNT, Inc.", "older_pages": [{"from": "2010-07-07"}], "coverage_start": "2019-07-31",
           "former_names": [{"name": "EXP World Holdings, Inc.", "from": "2025-02-19", "to": "2026-06-01"},
                            {"name": "eXp Realty International", "from": "2013-09-09", "to": "2016-04-27"},
                            {"name": "Desert Canadians", "from": "2010-07-07", "to": "2013-08-20"}]}
    assert sm.profile_name_spans(exp) == [("agnt", "2026-06-01", "9999-12-31"),
                                          ("desert canadians", "2010-07-07", "2013-08-20"),
                                          ("exp realty international", "2013-08-20", "2016-04-27"),
                                          ("exp world holdings", "2016-04-27", "2026-06-01")]
    assert sm.name_valid_throughout(exp, "exp world holdings", "2018-05-21", "2026-05-01")


# ------------------------------------------------------------------ round-2 review regressions

def _monthly(start, end):
    return [d.date().isoformat() for d in pd.date_range(start, end, freq="MS")]


def _raw(records):
    """ticker_rows_raw-style rows (date, symbol, cik as Int64; None for a listed symbol with no CIK)."""
    frame = pd.DataFrame([{"date": d, "symbol": s, "cik": c} for s, d, c in records], columns=["date", "symbol", "cik"])
    frame["cik"] = frame["cik"].astype("Int64")
    return frame


def _f25(rows):
    frame = pd.DataFrame(rows)
    for column in sm.FORM25_TEXT_COLUMNS:
        frame[column] = frame.get(column, pd.Series([""] * len(frame))).fillna("")
    frame["successor_cik"] = pd.array([r.get("successor_cik") for r in rows], dtype="Int64")
    return frame


NO_PRICES = pd.DataFrame(columns=["ticker", "file", "first_date", "last_date", "rows", "cik", "how",
                                  "ciks_on_ticker_in_file_range"])


def _master(intervals, form25, profiles, multi=frozenset(), full=()):
    return sm.build_master(profiles, intervals, intervals, form25, NO_PRICES, set(multi), [], list(full)).set_index("security_id")


def _profiles(*ciks):
    return {c: {**sm.parse_submissions({}), "name": f"Issuer {c}"} for c in ciks}


def test_the_continuation_horizon_is_the_delist_tolerance():
    # A ticker seen at the horizon continued, so it can keep no delist date; before it is the lists' lag.
    assert sm.CONTINUE_DAYS == f25.EFFECTIVE_LAG_DAYS + sm.DELIST_TOLERANCE_DAYS
    days = ["2024-01-01", "2024-02-01", "2024-02-20", "2024-03-01", "2024-04-01"]
    assert sm.continuation_after(["2024-01-01", "2024-02-20"], "2024-01-10", days, set(days), {}, "X")["status"] == "continued"
    lag = sm.continuation_after(["2024-01-01", "2024-02-01"], "2024-01-10", days, set(days), {}, "X")
    assert lag["status"] == "ended" and lag["after"] == ["2024-02-01"]


def test_liberty_live_split_off_does_not_end_fwona_fwonk():
    # Form 25 0001354457-25-001270 (2025-12-15) removed LLYVA/LLYVK only; Liberty Live Holdings (2078416)
    # listed them from 2026-01. FWONA/FWONK kept trading under 1560385.
    days = _monthly("2025-06-01", "2026-08-01")
    rows = _raw([(t, d, 1560385) for t in ("FWONA", "FWONK") for d in days]
                + [(t, d, 1560385 if d <= "2025-12-01" else 2078416) for t in ("LLYVA", "LLYVK") for d in days])
    snap = f25.SnapshotEvidence(rows, {"repo_symdir": days})
    ev = f25.snapshot_evidence(snap, 1560385, "2025-12-15", sec_nasdaq_tickers={"FWONA", "FWONK"}, sec_nasdaq=True,
                               recent=True)
    assert ev["tickers_before"] == "FWONA FWONK LLYVA LLYVK"
    assert (ev["tickers_continued"], ev["tickers_ended"], ev["tickers_handed_over"]) == ("FWONA FWONK", "LLYVA LLYVK",
                                                                                        "LLYVA LLYVK")
    successor = f25.successor_evidence(pd.DataFrame({"accession": ["a"], "subject_cik": [1560385],
                                                     "filing_date": ["2025-12-15"]}),
                                       snap.runs, {1560385: ["Liberty Media Corp"], 2078416: ["Liberty Live Holdings, Inc."]},
                                       days)["a"]
    assert successor["cik"] == 2078416 and successor["tickers"] == ["LLYVA", "LLYVK"]
    ev = {**ev, "still_on_nasdaq_same_cik": ev["snapshot_continues"], "successor": successor}
    label, _ = f25.classify_row("common", "redeemed_or_matured", ev)
    assert label == "reorg_review" and f25.subject_exit(label, ev) == "N"
    # Even a wrong exit (round 2's) cannot delete FWONK's later rows: the run continued, so it contradicts it.
    sm_rows = _rows([("FWONK", d, 1560385, "repo_symdir") for d in days])
    out, counts = sm.drop_rows_after_exit(sm_rows, {1560385: "2025-12-15"}, {}, {1560385: {"FWONA", "FWONK"}},
                                          days=days, full=set(days))
    assert out["cik"].notna().all() and counts["contradicted"] == [(1560385, "FWONK")]
    # Only the tickers the filing covered lose rows.
    out, counts = sm.drop_rows_after_exit(sm_rows, {1560385: "2025-12-15"}, {}, {1560385: {"LLYVA", "LLYVK"}},
                                          days=days, full=set(days))
    assert out["cik"].notna().all() and counts["contradicted"] == []


def test_a_security_seen_after_its_delist_date_keeps_no_delist_date():
    # The rule every delist date must pass: FWONK listed through 2026-08 in the run that held it at the
    # 2025-12-15 filing cannot take that filing's delist date; a relisting after two full-list
    # snapshots without it may (Windtree, 2017 removal, relisted 2020).
    days = _monthly("2016-01-01", "2026-08-01")
    iv = pd.DataFrame([
        {"security_id": "1560385.T-FWONK", "cik": 1560385, "ticker": "FWONK", "start": "2017-01-01", "end": "2026-08-01",
         "share_class": "T-FWONK"},
        {"security_id": "946486", "cik": 946486, "ticker": "WINT", "start": "2016-04-01", "end": "2017-04-01",
         "share_class": "COMMON"},
        {"security_id": "946486", "cik": 946486, "ticker": "WINT", "start": "2020-06-01", "end": "2025-08-01",
         "share_class": "COMMON"},
    ]).assign(source="repo_symdir", match="name+ticker", exchange="NASDAQ")
    form25 = _f25([
        {"subject_cik": 1560385, "filing_date": "2025-12-15", "effective_date": "2025-12-25", "accession": "llyv",
         "classification": "common_delisting", "class_kind": "common", "class_of_security": "Common Stock",
         "tickers_ended": "FWONK"},
        {"subject_cik": 946486, "filing_date": "2017-07-21", "effective_date": "2017-07-31", "accession": "wint",
         "classification": "common_delisting", "class_kind": "common", "class_of_security": "Common Stock"}])
    master = _master(iv, form25, _profiles(1560385, 946486), multi={1560385}, full=days)
    assert master.loc["1560385.T-FWONK", "delist_date"] == ""
    assert "gives no delist date: listed through 2026-08-01" in master.loc["1560385.T-FWONK", "identity_notes"]
    assert master.loc["946486", "delist_date"] == "2017-07-31"
    assert "listed on Nasdaq again" in master.loc["946486", "identity_notes"]
    sightings = _raw([("FWONK", d, 1560385) for d in days if d >= "2017-01-01"]
                     + [("WINT", d, 946486) for d in days if "2016-04-01" <= d <= "2017-04-01" or "2020-06-01" <= d <= "2025-08-01"])
    assert sm.delist_date_violations(master.reset_index(), iv, sightings, days).empty
    # A delist date kept while the security was still seen (its rows dropped after the exit) is reported.
    forced = master.reset_index().assign(delist_date=lambda m: m["delist_date"].where(m["security_id"] != "1560385.T-FWONK",
                                                                                          "2025-12-25"))
    cut = iv.assign(end=lambda f: f["end"].where(f["ticker"] != "FWONK", "2025-12-01"))
    bad = sm.delist_date_violations(forced, cut, sightings, days)
    assert bad[["security_id", "first_seen_after", "last_seen_after"]].values.tolist() == [
        ["1560385.T-FWONK", "2026-02-01", "2026-08-01"]]


def test_windtree_relisting_is_a_separate_interval():
    # Windtree 946486: suspended in 2017-04, removed by Form 25 2017-07-21, back on Nasdaq 2020-05-22.
    days = _monthly("2016-01-01", "2025-09-01")
    seen = [d for d in days if "2016-04-01" <= d <= "2017-04-01" or "2020-06-01" <= d <= "2025-08-01"]
    snap = f25.SnapshotEvidence(_raw([("WINT", d, 946486) for d in seen]), {"repo_symdir": days})
    ev = f25.snapshot_evidence(snap, 946486, "2017-07-21")
    assert (ev["tickers_before"], ev["tickers_ended"], ev["tickers_continued"]) == ("WINT", "WINT", "")
    assert f25.subject_exit("common_delisting", ev) == "Y"
    rows = _rows([("WINT", d, 946486, "repo_symdir") for d in seen])
    out, counts = sm.drop_rows_after_exit(rows, {946486: "2017-07-21"}, {}, {946486: {"WINT"}}, days=days, full=set(days))
    assert out["cik"].notna().all() and counts["kept_after_exit"] == len([d for d in seen if d >= "2020-06-01"])
    intervals = sm.build_intervals(out.astype({"cik": int}), {"repo_symdir": days})
    assert intervals[["start", "end"]].values.tolist() == [["2016-04-01", "2017-04-01"], ["2020-06-01", "2025-08-01"]]
    assert sm.listed_past_delisting(list(zip(intervals["start"], intervals["end"])), "2017-07-31", days) == ""


def test_shiftpixy_form25_before_its_ipo_ends_nothing():
    # ShiftPixy 1675634 filed an issuer Form 25 on 2017-02-14; its first Nasdaq row is 2017-07-01.
    days = _monthly("2016-06-01", "2024-11-01")
    seen = [d for d in days if "2017-07-01" <= d <= "2024-10-01"]
    snap = f25.SnapshotEvidence(_raw([("PIXY", d, 1675634) for d in seen]), {"repo_symdir": days})
    ev = f25.snapshot_evidence(snap, 1675634, "2017-02-14")
    assert ev["rows_any"] and not ev["rows_before"] and f25.issuer_form25_before_listing("25", ev)
    assert not f25.issuer_form25_before_listing("25-NSE", ev)  # an exchange's own 25-NSE is never ignored
    label, _ = f25.classify_row("common", "issuer_withdrawal", {**ev, "unlisted": True})
    assert label == "unlisted_withdrawal" and f25.subject_exit(label, ev) == "N"
    form25 = _f25([{"subject_cik": 1675634, "filing_date": "2017-02-14", "classification": label, "class_kind": "common"}])
    assert sm.form25_cuts(form25) == {}  # nor does it split a shared name by date
    rows = _rows([("PIXY", d, 1675634, "repo_symdir") for d in seen])
    out, counts = sm.drop_rows_after_exit(rows, {1675634: "2017-02-14"}, {}, days=days, full=set(days))
    assert out["cik"].notna().all() and counts["kept_after_exit"] == len(seen)


def test_ww_bankruptcy_relisting_keeps_its_rows_and_its_delist_date():
    # WW 105319: last seen 2025-05-01, Form 25 2025-07-03, the new stock listed 2025-08 under the same CIK.
    days = _monthly("2025-01-01", "2026-07-01")
    seen = [d for d in days if d <= "2025-05-01" or d >= "2025-08-01"]
    snap = f25.SnapshotEvidence(_raw([("WW", d, 105319) for d in seen]), {"repo_symdir": days})
    ev = f25.snapshot_evidence(snap, 105319, "2025-07-03", sec_nasdaq_tickers={"WW"}, sec_nasdaq=True, recent=True)
    assert (ev["tickers_ended"], ev["tickers_continued"]) == ("WW", "") and f25.subject_exit("common_delisting", ev) == "Y"
    rows = _rows([("WW", d, 105319, "repo_symdir") for d in seen])
    out, counts = sm.drop_rows_after_exit(rows, {105319: "2025-07-03"}, {}, {105319: {"WW"}}, days=days, full=set(days))
    assert out["cik"].notna().all()
    iv = sm.build_intervals(out.astype({"cik": int}), {"repo_symdir": days}).assign(security_id="105319")
    assert iv[["start", "end"]].values.tolist() == [["2025-01-01", "2025-05-01"], ["2025-08-01", "2026-07-01"]]
    # The new listing starts 19 days after the delist date, two monthly lists after the last sighting.
    assert sm.listed_past_delisting(list(zip(iv["start"], iv["end"])), "2025-07-13", days) == ""
    form25 = _f25([{"subject_cik": 105319, "filing_date": "2025-07-03", "effective_date": "2025-07-13", "accession": "ww",
                    "classification": "common_delisting", "class_kind": "common", "class_of_security": "Common Stock"}])
    master = _master(iv.assign(match="name+ticker"), form25, _profiles(105319), full=days)
    assert master.loc["105319", "delist_date"] == "2025-07-13" and master.loc["105319", "last_listed"] == "2026-07-01"


def test_freenome_ticker_change_is_a_reorganisation():
    # Freenome 2017526: the SPAC's PCSC became FRNM under the same CIK; the Form 25 of 2026-07-20 removed PCSC.
    symdir, screener = _monthly("2024-07-01", "2026-07-01"), ["2026-07-17", "2026-07-27", "2026-07-31", "2026-08-01"]
    rows = _raw([("PCSC", d, 2017526) for d in symdir] + [("FRNM", d, 2017526) for d in screener[1:]])
    snap = f25.SnapshotEvidence(rows, {"repo_symdir": symdir, "repo_screener_300M": screener}, {"repo_screener_300M"})
    ev = f25.snapshot_evidence(snap, 2017526, "2026-07-20", sec_nasdaq_tickers={"FRNM"}, sec_nasdaq=True, recent=True)
    assert (ev["tickers_before"], ev["tickers_new"], ev["tickers_continued"], ev["tickers_ended"]) == (
        "PCSC", "FRNM", "FRNM", "PCSC")
    label, note = f25.classify_row("common", "substituted_merger_or_exchange", {**ev, "still_on_nasdaq_same_cik": True})
    assert label == "reorg" and "PCSC ended and FRNM began" in note and f25.subject_exit(label, ev) == "N"


def test_share_class_flip_flops_do_not_break_a_run():
    # FLWS 2022-06: symbol files alternate 'Class A Common Stock' and plain 'Common Stock' names.
    days = [d.date().isoformat() for d in pd.bdate_range("2022-06-07", "2022-06-24")]
    rows = _rows([("FLWS", d, 1084869, "repo_symdir") for d in days])
    rows["share_class"] = ["A" if i % 2 else "COMMON" for i in range(len(days))]
    out = sm.build_intervals(rows, {"repo_symdir": days})
    assert out[["start", "end", "share_class", "n_snapshots"]].values.tolist() == [[days[0], days[-1], "A", len(days)]]
    # ICLR: ADS names until the 2012 coverage gap, ordinary shares after it: one interval.
    dates = {"wayback_symdir": ["2012-05-01", "2012-06-22", "2012-10-24", "2012-12-01"]}
    iclr = _rows([("ICLR", d, 1060955, "wayback_symdir") for d in dates["wayback_symdir"]])
    iclr["share_class"] = ["ADS", "ADS", "COMMON", "COMMON"]
    out = sm.build_intervals(iclr, dates)
    assert len(out) == 1 and out.iloc[0]["coverage_gap_days"] == 124


def test_rush_cl_a_is_class_a():
    assert sm.share_class_from_name("Rush Enterprises, Inc. - Common Stock Cl A") == "A"
    assert sm.share_class_from_name("Rush Enterprises Inc Cl B") == "B"
    assert sm.share_class_from_name("Clarus Corporation - Common Stock") == "COMMON"
    days = ["2015-01-10", "2015-02-01", "2015-03-01", "2015-04-01"]
    rows = _rows([("RUSHA", d, 1012019, "repo_symdir") for d in days] + [("RUSHB", d, 1012019, "repo_symdir") for d in days])
    rows["name"] = ["Rush Enterprises, Inc. - Class A Common Stock", "Rush Enterprises Inc. Common Stock Cl A",
                    "Rush Enterprises, Inc.", "Rush Enterprises Inc. Common Stock Cl A"] + ["Rush Enterprises Inc Cl B"] * 4
    rows["share_class"] = sm.fill_classes(rows.assign(share_class=rows["name"].map(sm.share_class_from_name)))
    out = sm.build_intervals(rows, {"repo_symdir": days}, split_classes={1012019})
    assert sorted(zip(out["ticker"], out["share_class"])) == [("RUSHA", "A"), ("RUSHB", "B")]


def test_blank_last_sale_rows_count_as_present(tmp_path):
    # SunPower in every 2015-2016 company list with a blank LastSale: no break between symbol files.
    repo, hook = tmp_path / "repo", tmp_path / "companylist"
    repo.mkdir(), hook.mkdir()
    for day in ("2015-03-01", "2016-03-01"):
        (repo / f"nasdaq_listed_{day}.csv").write_text("Symbol,Name,ETF,Test Issue,NextShares\n"
                                                       "SPWR,SunPower Corporation - Class A Common Stock,N,N,N\n")
    for day in ("2015-03-05", "2015-08-10", "2016-02-02"):
        (hook / f"nasdaq_companylist_{day}.csv").write_text(
            f"Symbol,Name,LastSale,MarketCap,Observed At\nSPWR,SunPower Corporation,,,{day}\nAAPL,Apple Inc.,1,1,{day}\n")
    snaps = sm.load_listing_snapshots(repo, {"wayback_companylist": hook})
    listed = sm.common_snapshot_rows(snaps)
    rows = listed[listed["symbol"] == "SPWR"].assign(cik=867773, share_class="A", family=lambda f: f["source"], how="x")
    dates = {f: sorted(g["date"].unique()) for f, g in snaps.groupby("source")}
    assert len(sm.build_intervals(rows, dates, sm.presence_by_date(snaps))) == 1
    assert len(sm.build_intervals(rows, dates, sm.presence_by_date(snaps[~snaps["no_last_sale"]]))) == 2  # round 2


def test_breaks_count_another_class_key_of_the_same_security_as_the_same_holder():
    iv = pd.DataFrame([{"security_id": "1", "cik": 1, "ticker": "X", "start": "2020-01-01", "end": "2020-03-01",
                        "share_class": "A", "source": "repo_symdir"},
                       {"security_id": "1", "cik": 1, "ticker": "X", "start": "2020-04-01", "end": "2020-06-01",
                        "share_class": "COMMON", "source": "repo_symdir"}])
    full = _monthly("2020-01-01", "2020-06-01")
    stats = sm.break_stats(iv, full, {d: {"X"} for d in full})
    assert (stats["breaks"], stats["unexplained"], stats["uncovered_days"]) == (1, 1, 30)


# ------------------------------------------------------------------ round-2 review regressions: listing evidence

def test_stale_copies_of_an_older_symbol_file_are_dropped():
    # nasdaq_listed_2024-05-18 repeats 2024-03-28 (same source file and commit); a weekend repeat is real.
    frame = pd.DataFrame([{"source": "repo_symdir", "date": d, "symbol": s}
                          for d, syms in [("2024-03-28", "AB"), ("2024-04-20", "A"), ("2024-05-18", "AB"),
                                          ("2024-10-05", "C"), ("2024-10-08", "C")] for s in syms])
    assert sm.stale_duplicate_dates(frame) == {("repo_symdir", "2024-05-18"): "2024-03-28"}


def test_a_short_file_is_presence_not_absence():
    counts = [3800, 3770, 3037, 3780, 3790]
    days = _monthly("2022-04-01", "2022-08-01")
    listings = pd.DataFrame([{"source": "repo_symdir", "date": d, "symbol": f"S{i}"} for d, n in zip(days, counts)
                             for i in range(n)])
    assert sm.partial_snapshot_dates(listings) == {"repo_symdir": ["2022-06-01"]}
    assert sm.is_partial_family("repo_symdir" + sm.PARTIAL_SUFFIX) and sm.is_partial_family("repo_screener_300M")


def test_company_list_sightings_need_a_symbol_directory():
    # America Movil (NYSE from 2016-12) stayed in nasdaq.com company lists; a symbol directory on either
    # side within 70 days that lacks it makes the row no listing evidence.
    rows = [("wayback_symdir", "2017-09-12", "http://nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt", "AAPL"),
            ("wayback_symdir", "2017-10-13", "http://nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt", "AAPL"),
            ("wayback_companylist", "2017-09-30", "http://www.nasdaq.com/screening/companies-by-name.aspx", "AAPL"),
            ("wayback_companylist", "2017-09-30", "http://www.nasdaq.com/screening/companies-by-name.aspx", "AMOV"),
            ("wayback_companylist", "2017-03-16", "http://www.nasdaq.com/screening/companies-by-name.aspx", "AMOV")]
    frame = pd.DataFrame(rows, columns=["source", "date", "source_url", "symbol"])
    mask = sm.uncorroborated_sightings(frame)
    assert mask.tolist() == [False, False, False, True, False]  # 2017-03-16: no symbol directory near to check it


def test_abbreviated_preferreds_notes_and_warrants_are_not_common():
    for name in ["Huntington Bancshares Incorporated - Non Cumulative Perp Conv Pfd Ser A",
                 "TriState Capital Holdings, Inc. - Dep Shs Rep 1/40th Int 6.75% Srs A Non-Cum Pfd",
                 "Banc of California, Inc. - Baby Bond", "Conifer Holdings, Inc. - Senior Unsecured Notes",
                 "Great Elm Capital Corp. - Notes", "Polestar Automotive Holding UK Limited - Class C-1 ADS (ADW)",
                 "PepperLime Health Acquisition Corporation - Warrrant"]:
        assert not sm.is_common_equity(name), name
    assert sm.is_common_equity("Biodexa Pharmaceuticals plc American Depositary Shs")
    assert sm.is_common_equity("Alphabet Inc. - Class A Common Stock")


# ------------------------------------------------------------------ round-2 review regressions: Form 25

@pytest.mark.parametrize("text,kinds,kind", [
    ("Preferred Stock Purchase Rights, par value $0.001 per share", ["right"], "right"),  # Masimo 2016
    ("8.75% Series A Cumulative Redeemable Perpetual Preferred Stock, par value $0.001 per share (Nasdaq : CCLDP)",
     ["preferred"], "preferred"),  # CareCloud 2025-03-21
    ("Units of beneficial interest, no par value per share (See exhibit A)", ["unit"], "unit"),  # Global X 2013
    ("Common shares without par value Warrants, each warrant exercisable for 1/30th common share", ["common", "warrant"],
     "common"),  # Clever Leaves 2024
    ("Common Stock, $0.01 par value per share", ["common"], "common"),
    ("Ordinary Shares, nominal value €0.12 per share", ["common"], "common"),
    ("Common Stock, Series A", ["common"], "common"),
])
def test_par_value_phrases_are_no_class(text, kinds, kind):
    assert f25.class_kinds(text) == kinds and f25.classify_security_class(text) == kind


def test_a_withdrawal_followed_by_a_relisting_is_a_delisting():
    # Interlink 828146: Form 25 2019-02-04, LINK out of the lists until 2021-03-25.
    days = _monthly("2018-06-01", "2021-06-01")
    seen = [d for d in days if d <= "2019-01-01" or d >= "2021-04-01"]
    snap = f25.SnapshotEvidence(_raw([("LINK", d, 828146) for d in seen]), {"repo_symdir": days})
    ev = f25.snapshot_evidence(snap, 828146, "2019-02-04", sec_nasdaq_tickers={"LINK"}, sec_nasdaq=True,
                               periodic_later=True)
    assert ev["tickers_ended"] == "LINK" and not ev["snapshot_continues"]
    label, note = f25.classify_row("common", "issuer_withdrawal", {**ev, "relisted_later": True, "current_tickers": "LINK:Nasdaq"})
    assert label == "common_delisting" and "relisted on Nasdaq later" in note
    assert f25.subject_exit(label, ev) == "Y"


def test_a_documented_transfer_ends_the_listing_despite_company_list_sightings():
    ev = {"transfer_hint": "Form 8-A12B filed 2016-12-01 (registration on another exchange)", "tickers_before": "AMOV",
          "tickers_continued": "AMOV", "still_on_nasdaq_same_cik": True, "nasdaq_now": ""}
    assert f25.classify_row("common", "issuer_withdrawal", ev)[0] == "transfer"
    assert f25.subject_exit("transfer", ev) == "Y"
    assert f25.subject_exit("transfer", {**ev, "nasdaq_now": "AMOV"}) == "N"  # SEC still lists it on Nasdaq


def _caps(rows):
    return pd.Series([v for _, v in rows], index=pd.to_datetime([d for d, _ in rows]))


def test_float_cross_checks_catch_x1000_errors_only():
    end = pd.Timestamp("2022-07-29")
    # SeaChange 2022: $23.1B at $457 a share after $46M a year before, no listed cap near: a x1000 error.
    assert f25.float_unit_flag(2.31e10, end, 5.05e7, 4.57e7) == "x1000_vs_other_floats"
    # A SPAC's float growing 50-fold at about $10 a share is real.
    assert f25.float_unit_flag(5e9, end, 5e8, 1e8) == "ok"
    # National American University 2016: $19.7B against listed caps near $70M.
    assert f25.float_unit_flag(1.97e10, pd.Timestamp("2016-11-30"), 2.42e7, None,
                               _caps([("2016-06-01", 7e7), ("2017-06-01", 6e7)])) == "above_listed_market_cap"
    # Trillium 2020 at 14 times its 2019 cap, Nikola at 54 times VectoIQ's but $34 a share: kept.
    assert f25.float_unit_flag(6.84e8, pd.Timestamp("2020-06-30"), 1.03e8, None, _caps([("2019-06-11", 5e7)])) == "ok"
    assert f25.float_unit_flag(1.25e10, pd.Timestamp("2020-06-30"), 3.66e8, None, _caps([("2019-06-11", 2.3e8)])) == "ok"
    # A listed cap near the float settles it: no jump test (Electronic Arts with a /1000 fact in its history).
    assert f25.float_unit_flag(4.8e10, pd.Timestamp("2025-09-30"), 2.5e8, 4.8e7, _caps([("2026-07-17", 4.0e10)])) == "ok"


def test_a_tiny_float_fact_is_no_reference_for_the_jump_test():
    floats = _facts([("2021-06-30", 3.5e10, "a"), ("2022-06-30", 4.0e10, "b"), ("2023-06-30", 4.0e7, "c"),
                     ("2024-06-30", 4.8e10, "d")])
    shares = _facts([("2021-08-01", 2.6e8, "a"), ("2022-08-01", 2.6e8, "b"), ("2023-08-01", 2.6e8, "c"),
                     ("2024-08-01", 2.5e8, "d")])
    out = f25.float_for_filing(floats, shares, 7, "2025-03-01")
    assert out["public_float_usd"] == 4.8e10 and out["float_check_flag"] == "ok"


# ------------------------------------------------------------------ round-3 review regressions: low items

def test_names_in_another_word_order_need_initials():
    # FRBA (First Bank, NJ, an FDIC filer) is not Bank First Corp (1746109); EDGAR's inversions of
    # initials still match ('PRICE T ROWE GROUP', 'FOSTER L B', 'BARRY R G', 'MAYS J W').
    norm = sm.normalize_issuer_name
    assert not sm.names_match(norm("First Bank"), norm("BANK FIRST CORP"))
    assert not any(k.startswith("~") for k in sm.index_keys(norm("First Bank")))
    assert sm.names_in(sm.build_name_index({1746109: _issuer("Bank First Corp")}), norm("First Bank - Common Stock")) == set()
    for listed, edgar in [("T. Rowe Price Group, Inc.", "PRICE T ROWE GROUP INC"), ("L.B. Foster Company", "FOSTER L B CO"),
                          ("R.G. Barry Corporation", "BARRY R G CORP /OH/"), ("J. W. Mays, Inc.", "MAYS J W INC")]:
        assert sm.names_match(norm(listed), norm(edgar)), listed
        assert sm.names_in(sm.build_name_index({1: _issuer(edgar)}), norm(listed)) == {1}, listed


def test_the_first_bearer_of_a_name_keeps_the_ticker_until_its_form25():
    # Old Dime (1005409) listed DCOM until its Form 25 of 2021-01-29; Bridge Bancorp (846617, filing
    # since 1988 with unfetched older pages) took the name Dime Community Bancshares at the merger.
    old = _issuer("DIME COMMUNITY BANCSHARES INC", periodic=("2012-03-01", "2020-11-06"),
                  former=[{"name": "DIME COMMUNITY BANCORP INC", "from": "1996-05-20", "to": "1998-03-10"}])
    old["older_pages"], old["coverage_start"] = [{"from": "1996-05-20"}], "2012-01-01"
    old["filing_dates"] = ["2012-03-01", "2021-02-04"]  # its window runs to 2021-03-06, past the Form 25
    new = _issuer("Dime Community Bancshares, Inc. /NY/", ["DCOM"],
                  former=[{"name": "BRIDGE BANCORP INC", "from": "1996-08-05", "to": "2021-01-29"}])
    new["older_pages"] = [{"from": "1990-01-01"}]
    profiles = {1005409: old, 846617: new}
    cands = sm.Candidates()
    cands.add("DCOM", 846617, "sec_company_tickers_exchange")
    cands.add("DCOM", 1005409, "repo_historical_ticker_ciks")
    index = sm.build_name_index(profiles)
    days = ["2020-11-02", "2020-12-30", "2021-01-15", "2021-02-26"]
    out = sm.resolve_by_date("DCOM", "Dime Community Bancshares, Inc. - Common Stock", days, cands, profiles, index,
                             exits={1005409: "2021-01-29"})
    assert out == {"2020-11-02": 1005409, "2020-12-30": 1005409, "2021-01-15": 1005409, "2021-02-26": 846617}


def test_a_form25_cuts_only_a_ticker_its_cik_held():
    # Match Group Inc (1575189) listed MTCH until its Form 25 of 2020-07-01; old IAC (891103) filed a
    # Form 25 for IAC on 2020-06-30, renamed itself Match Group and took MTCH. Its own cut must not end
    # its MTCH dates (round 3 first gave 2020-07-29..08-07 to 1575189 and dropped them after its exit).
    old = _issuer("Match Group Holdings II, LLC", periodic=("2016-03-01", "2020-05-08"),
                  former=[{"name": "MATCH GROUP, INC.", "from": "2014-02-07", "to": "2020-07-01"}])
    old["filing_dates"] = ["2016-03-01", "2020-07-10"]
    new = _issuer("Match Group, Inc.", ["MTCH"],
                  former=[{"name": "IAC/InterActiveCorp", "from": "2008-08-20", "to": "2020-07-01"}])
    profiles = {1575189: old, 891103: new}
    cands = sm.Candidates()
    cands.add("MTCH", 891103, "sec_company_tickers_exchange")
    cands.add("MTCH", 1575189, "repo_historical_ticker_ciks")
    index = sm.build_name_index(profiles)
    days = ["2020-05-22", "2020-06-29", "2020-07-29", "2020-08-10"]
    out = sm.resolve_by_date("MTCH", "Match Group, Inc. - Common Stock", days, cands, profiles, index,
                             exits={1575189: "2020-07-01", 891103: "2020-06-30"})
    assert out == {"2020-05-22": 1575189, "2020-06-29": 1575189, "2020-07-29": 891103, "2020-08-10": 891103}


def test_an_edgar_inversion_of_the_cik_s_own_name_matches_by_name():
    # Bob Evans Farms (33769) was 'EVANS BOB FARMS INC' in EDGAR until 2011-08-01 and is found only
    # through SEC's name list: its own current name vouches for the inverted one.
    bob = _issuer("BOB EVANS FARMS INC", periodic=("2011-03-01", "2018-01-10"),
                  former=[{"name": "EVANS BOB FARMS INC", "from": "1994-03-08", "to": "2011-08-01"}])
    assert sm.name_valid(bob, "bob evans farms", "2011-01-25", "2011-01-25", slack=30)
    assert not sm.name_valid(_issuer("BANK FIRST CORP"), "first bank", "2020-01-02", "2020-01-02")
    cands = sm.Candidates()
    cands.add("BOBE", 33769, "sec_cik_lookup")
    profiles = {33769: bob}
    assert sm.resolve_listing("BOBE", "Bob Evans Farms, Inc. - Common Stock", cands, profiles,
                              sm.build_name_index(profiles), "2010-12-31", "2017-12-15") == (33769, "name+lookup")


def test_a_name_list_match_active_for_a_small_part_of_the_listing_is_split_by_date():
    # 'First Bank' (FRBA, 2014-2024) and FIRSTBANK CORP (778972), which stopped filing in 2014.
    firstbank = _issuer("FIRSTBANK CORP", periodic=("2011-03-01", "2014-03-14"))
    cands = sm.Candidates()
    cands.add("FRBA", 778972, "sec_cik_lookup")
    profiles = {778972: firstbank}
    index = sm.build_name_index(profiles)
    assert sm.resolve_listing("FRBA", "First Bank", cands, profiles, index, "2014-01-22", "2024-02-27") == (
        None, "ambiguous")
    assert sm.resolve_by_date("FRBA", "First Bank", ["2014-01-22", "2019-01-03", "2024-02-27"], cands, profiles,
                              index) == {"2014-01-22": 778972}
    # A listing that outlives the last filing by months (a bankruptcy) stays whole.
    assert sm.resolve_listing("FRBA", "First Bank", cands, profiles, index, "2012-01-22", "2014-12-01") == (
        778972, "name+lookup")


def test_x1000_shares_facts_expose_x1000_floats():
    # Crosstex 2011: $364B float against 47.4B shares ($7.68 a share), while the 2012 10-K reports 47.6M
    # shares; the 2013 float of $652M against 47.7M shares is the right one.
    floats = _facts([("2011-12-31", 3.639612e11, "a"), ("2012-12-31", 4.574057e11, "b"), ("2013-06-30", 6.517286e8, "c")])
    shares = _facts([("2011-12-31", 4.738972e10, "a"), ("2012-12-31", 4.755868e7, "b"), ("2013-03-31", 4.759951e10, "x"),
                     ("2013-07-25", 4.772358e7, "c")])
    assert f25.shares_x1000(shares, shares.iloc[0]) and not f25.shares_x1000(shares, shares.iloc[3])
    out = f25.float_for_filing(floats, shares, 7, "2014-03-07")
    assert out["public_float_usd"] == 6.517286e8 and out["float_check_flag"] == "ok" and out["float_facts_dropped"] == 2
    # A 1-for-40 reverse split is no x1000 error (Lyra 2025: 65.9M shares, then 1.6M).
    split = _facts([("2025-02-28", 6.588e7, "a"), ("2025-08-08", 1.644e6, "b")])
    assert not f25.shares_x1000(split, split.iloc[0]) and not f25.shares_x1000(split, split.iloc[1])
    # Below $20B the other shares fact is as likely the one in error (in thousands): Yellow 2023 keeps
    # its $227M float at $4.4 a share although a nearby shares fact reads 51 thousand.
    floats = _facts([("2023-06-30", 2.273e8, "a")])
    shares = _facts([("2023-08-01", 5.15e7, "a"), ("2023-05-01", 5.15e4, "q")])
    out = f25.float_for_filing(floats, shares, 7, "2023-09-08")
    assert out["public_float_usd"] == 2.273e8 and out["float_check_flag"] == "ok"


def test_absence_from_the_300m_screener_bounds_the_market_cap(tmp_path):
    # Lyra 1327273 was in the Nasdaq lists in 2025-04 but not in the $300M-and-up screener: its 2024-06
    # float of $13.8B ($209 a share) is a x1000 error. A CIK last seen before the screener date (GONE) or
    # long before it (OLD) gives no bound.
    screener = tmp_path / "nasdaq_300M_2025-04-14.csv"
    screener.write_text("Symbol,Name,Market Cap\nAAPL,Apple Inc. Common Stock,3e12\n")
    resolved = _raw([("LYRA", "2025-04-01", 1327273), ("LYRA", "2025-05-01", 1327273), ("AAPL", "2025-04-11", 320193),
                     ("AAPL", "2025-04-18", 320193), ("OLD", "2025-01-02", 9), ("GONE", "2025-04-01", 8)])
    bounds = f25.screener_bounds(resolved, [screener])
    assert bounds[["date", "cik", "cap"]].values.tolist() == [["2025-04-14", 1327273, 3e8]]
    caps = _caps([("2025-04-14", 3e8)])
    assert f25.float_unit_flag(1.380319e10, pd.Timestamp("2024-06-28"), 6.588e7, None, caps) == "above_listed_market_cap"
    assert f25.float_unit_flag(1.112e7, pd.Timestamp("2025-06-30"), 1.644e6, None, caps) == "ok"


# ------------------------------------------------------------------ round-3 review regressions

def _ivs(rows):
    return pd.DataFrame([{"security_id": s, "cik": c, "ticker": t, "start": a, "end": b, "share_class": k}
                         for s, c, t, a, b, k in rows]).assign(source="repo_symdir", match="name+ticker", exchange="NASDAQ")


def test_avidity_cash_out_is_a_delisting_with_no_successor_link_to_atrium():
    # Form 25 0001354457-26-000224 (2026-02-27, Rule 12d2-2(a)(2)): Novartis bought Avidity (1599901) for
    # cash; Atrium Therapeutics (2093101), spun off just before, listed under RNA from 2026-03.
    days = _monthly("2025-06-01", "2026-07-01")
    snap = f25.SnapshotEvidence(_raw([("RNA", d, 1599901 if d <= "2026-02-01" else 2093101) for d in days]),
                                {"repo_symdir": days})
    ev = f25.snapshot_evidence(snap, 1599901, "2026-02-27")
    assert (ev["tickers_before"], ev["tickers_ended"], ev["tickers_continued"]) == ("RNA", "RNA", "")
    names = {1599901: ["Avidity Biosciences, Inc."], 2093101: ["Atrium Therapeutics, Inc."]}
    filing = pd.DataFrame({"accession": ["rna"], "subject_cik": [1599901], "filing_date": ["2026-02-27"]})
    successor = f25.successor_evidence(filing, snap.runs, names, days)["rna"]
    assert successor["cik"] == 2093101 and successor["new"] and not successor["names_match"]
    label, note = f25.classify_row("common", "redeemed_or_matured", {**ev, "successor": successor})
    assert label == "common_delisting" and "no successor" in note and f25.subject_exit(label, ev) == "Y"
    # The same handover under a merger substitution, or to a registrant of the same name, stays a reorganisation;
    # so does a redemption while the subject keeps another ticker (Liberty, test_liberty_live_split_off_...).
    assert f25.classify_row("common", "substituted_merger_or_exchange", {**ev, "successor": successor})[0] == "reorg_review"
    same_name = {**ev, "successor": {**successor, "names_match": True}}
    assert f25.classify_row("common", "redeemed_or_matured", same_name)[0] == "reorg"
    kept = {**ev, "tickers_continued": "RNAX", "successor": successor}
    assert f25.classify_row("common", "redeemed_or_matured", kept)[0] == "reorg_review"
    # The master keeps Avidity's delist date and links it to no successor.
    iv = _ivs([("1599901", 1599901, "RNA", "2025-06-01", "2026-02-01", "COMMON"),
               ("2093101", 2093101, "RNA", "2026-03-01", "2026-07-01", "COMMON")])
    form25 = _f25([{"subject_cik": 1599901, "filing_date": "2026-02-27", "effective_date": "2026-03-09",
                    "accession": "rna", "classification": label, "class_kind": "common", "class_of_security": "Common Stock"}])
    master = _master(iv, form25, _profiles(1599901, 2093101), full=days)
    assert master.loc["1599901", "delist_date"] == "2026-03-09"
    assert master.loc["1599901", "successor_security_id"] == "" and master.loc["2093101", "delist_date"] == ""


def test_tickers_renamed_before_a_filing_did_not_end_at_it():
    # QVC Group 1355096: QRTEA/QRTEB became QVCGA/QVCGB in 2025-02/03; its own Form 25 of 2025-05-27
    # (0001104659-25-052986) withdrew the Series B stock (QVCGB) only. QVCGA went on to 2026-04.
    days = _monthly("2024-06-01", "2026-05-01")
    rows = _raw([(t, d, 1355096) for t in ("QRTEA", "QRTEB") for d in days if d <= "2025-02-01"]
                + [("QVCGA", d, 1355096) for d in days if d >= "2025-03-01"]
                + [("QVCGB", d, 1355096) for d in days if "2025-03-01" <= d <= "2025-05-01"])
    snap = f25.SnapshotEvidence(rows, {"repo_symdir": days})
    ev = f25.snapshot_evidence(snap, 1355096, "2025-05-27")
    assert (ev["tickers_before"], ev["tickers_ended"], ev["tickers_continued"], ev["tickers_ended_earlier"]) == (
        "QVCGA QVCGB", "QVCGB", "QVCGA", "QRTEA QRTEB")
    # A CIK with no ticker on the last full list before the filing keeps the window (a suspension, as
    # Windtree's in test_windtree_relisting_is_a_separate_interval).
    suspended = f25.SnapshotEvidence(_raw([("SUSP", d, 7) for d in days if d <= "2025-02-01"]), {"repo_symdir": days})
    assert f25.snapshot_evidence(suspended, 7, "2025-05-27")["tickers_ended"] == "SUSP"
    # The Series B filing gives a ticker-keyed class ending in another class letter no delist date,
    # even with the round-3 ticker evidence that listed QRTEA among the tickers ended.
    keyed = {"T-QRTEA", "T-QRTEB", "T-QVCGA", "T-QVCGB"}
    series_b = "Series B Common Stock, par value $0.01 per share"
    assert sm.exit_matches_class(series_b, ev["tickers_ended"], "T-QVCGB", {"QVCGB"}, keyed)
    for key in ("T-QRTEA", "T-QRTEB", "T-QVCGA"):
        assert not sm.exit_matches_class(series_b, ev["tickers_ended"], key, {key[2:]}, keyed)
    assert not sm.exit_matches_class(series_b, "QRTEA QRTEB QVCGB", "T-QRTEA", {"QRTEA"}, keyed)
    # A K suffix is not compared (Comcast's Class A Special is CMCSK; Liberty's Series C is LSXMK).
    assert sm.exit_matches_class("Class A Special Common Stock", "CMCSK", "T-CMCSK", {"CMCSK"}, {"T-CMCSA", "T-CMCSK"})
    assert sm.exit_matches_class("Series C Liberty SiriusXM Common Stock", "LSXMK", "T-LSXMK", {"LSXMK"},
                                 {"T-LSXMA", "T-LSXMB", "T-LSXMK"})


def test_successor_links_follow_the_ticker_then_the_class_letter():
    # Liberty Live split-off (2025-12-15): LLYVA went to Liberty Live Holdings' class A, LLYVK to its class C.
    # 7 -> 8 (made up): the successor holds neither old ticker, so the class letter decides.
    days = _monthly("2014-01-01", "2026-08-01")
    iv = _ivs([("1560385.T-LLYVA", 1560385, "LLYVA", "2023-08-01", "2025-12-01", "T-LLYVA"),
               ("1560385.T-LLYVK", 1560385, "LLYVK", "2023-08-01", "2025-12-01", "T-LLYVK"),
               ("2078416.A", 2078416, "LLYVA", "2026-01-01", "2026-08-01", "A"),
               ("2078416.C", 2078416, "LLYVK", "2026-01-01", "2026-08-01", "C"),
               ("7.A", 7, "OLDA", "2014-01-01", "2015-06-01", "A"), ("7.C", 7, "OLDK", "2014-01-01", "2015-06-01", "C"),
               ("8.A", 8, "NEWA", "2015-07-01", "2026-08-01", "A"), ("8.C", 8, "NEWK", "2015-07-01", "2026-08-01", "C")])
    form25 = _f25([
        {"subject_cik": 1560385, "filing_date": "2025-12-15", "effective_date": "2025-12-25", "accession": "llyv",
         "classification": "reorg_review", "class_kind": "common", "tickers_ended": "LLYVA LLYVK",
         "class_of_security": "Liberty Media Corporation Series A Liberty Live Common Stock & Liberty Media "
                              "Corporation Series C Liberty Live Common Stock",
         "successor_cik": 2078416, "successor_tickers": "LLYVA LLYVK"},
        {"subject_cik": 7, "filing_date": "2015-06-15", "effective_date": "2015-06-25", "accession": "seven",
         "classification": "reorg", "class_kind": "common", "class_of_security": "Class A and Class C Common Stock",
         "successor_cik": 8, "successor_tickers": "OLDA OLDK"}])
    master = _master(iv, form25, _profiles(1560385, 2078416, 7, 8), multi={1560385, 2078416, 7, 8}, full=days)
    assert master.loc["1560385.T-LLYVA", "successor_security_id"] == "2078416.A"
    assert master.loc["1560385.T-LLYVK", "successor_security_id"] == "2078416.C"
    assert master.loc["7.A", "successor_security_id"] == "8.A" and master.loc["7.C", "successor_security_id"] == "8.C"
    assert (sm.class_letter("T-BATRK"), sm.class_letter("T-QRTEA"), sm.class_letter("C"), sm.class_letter("COMMON"),
            sm.class_letter("T-SRCL")) == ("C", "A", "C", "", "")


# ------------------------------------------------------------------ the built tables (skipped when absent)

BUILT = [sm.MASTER, sm.INTERVALS, sm.RAW_ROWS, sm.WORK / "snapshot_dates.json", f25.OUTPUT]


@pytest.mark.skipif(not all(p.exists() for p in BUILT), reason="step 3/4 outputs not built")
def test_built_tables_respect_every_delist_date_and_the_named_cases():
    import json
    master = pd.read_csv(sm.MASTER, dtype=str, keep_default_na=False)
    iv = pd.read_csv(sm.INTERVALS, dtype=str, keep_default_na=False)
    raw = sm.read_raw_rows()
    dates = json.loads((sm.WORK / "snapshot_dates.json").read_text())
    full = sorted({d for f, ds in dates["families"].items() if not sm.is_partial_family(f) for d in ds})
    assert sm.delist_date_violations(master, iv[iv["source"] != "sec_company_tickers_exchange"], raw, full).empty
    by_sid = master.set_index("security_id")
    for sid in ("1560385.T-FWONA", "1560385.T-FWONK"):
        assert by_sid.loc[sid, "delist_date"] == "" and by_sid.loc[sid, "last_listed"] >= "2026-07-01"
    snap = iv[iv["source"] != "sec_company_tickers_exchange"]
    spans = lambda t, c: snap[(snap["ticker"] == t) & (snap["cik"] == c)][["start", "end"]].values.tolist()
    assert len(spans("WINT", "946486")) == 2 and spans("WINT", "946486")[1][0] >= "2020-05-01"
    assert by_sid.loc["946486", "delist_date"] == "2017-07-31"
    assert spans("PIXY", "1675634") and by_sid.loc["1675634", "delist_date"] == ""
    assert spans("WW", "105319")[-1][1] >= "2026-07-01" and by_sid.loc["105319", "delist_date"] == "2025-07-13"
    assert spans("FRNM", "2017526") and by_sid.loc["2017526", "delist_date"] == ""
    for ticker, cik in [("FLWS", "1084869"), ("ICLR", "1060955"), ("RUSHA", "1012019")]:
        assert len(spans(ticker, cik)) == 1, ticker
    assert len(spans("SPWR", "867773")) == 1
    form25 = pd.read_csv(f25.OUTPUT, dtype=str, keep_default_na=False).set_index("accession")
    assert form25.loc["0001354457-25-001270", "subject_exit"] == "N"
    assert form25.loc["0000937556-16-000212", "class_kind"] == "right"
    # Round-3 review: Avidity's cash-out, QVC Group's Series B withdrawal, successor classes.
    avidity = form25.loc["0001354457-26-000224"]
    assert (avidity["classification"], avidity["successor_cik"], avidity["subject_exit"]) == ("common_delisting", "", "Y")
    assert by_sid.loc["1599901", "delist_date"] == "2026-03-09" and by_sid.loc["1599901", "successor_security_id"] == ""
    assert form25.loc["0001104659-25-052986", "tickers_ended"] == "QVCGB"
    assert by_sid.loc["1355096.T-QVCGB", "delist_date"] == "2025-06-06"
    assert by_sid.loc["1355096.T-QRTEA", "delist_date"] == "" and by_sid.loc["1355096.T-QRTEB", "delist_date"] == ""
    for sid, successor in [("1560385.T-LLYVK", "2078416.C"), ("1560385.T-LLYVA", "2078416.A"),
                           ("1560385.T-BATRK", "1958140.C"), ("1288776.A", "1652044.A"), ("1288776.C", "1652044.C"),
                           ("1308161.A", "1754301.A"), ("1316631.C", "1570585.T-LBTYK"), ("1570585.T-LILAK", "1712184.C")]:
        assert by_sid.loc[sid, "successor_security_id"] == successor, sid


def test_a_delist_date_long_after_the_last_listing_is_flagged_for_review():
    # Community First Bancshares 1691507 (CFBI) left the lists at its 2021-01 conversion to Affinity
    # Bancshares; Nasdaq's Form 25 for it came with Affinity's in 2026-08. Avidity's comes 8 days after.
    days = _monthly("2020-01-01", "2026-08-01")
    iv = _ivs([("1691507", 1691507, "CFBI", "2020-01-01", "2021-01-01", "COMMON"),
               ("1599901", 1599901, "RNA", "2020-01-01", "2026-03-01", "COMMON")])
    form25 = _f25([{"subject_cik": 1691507, "filing_date": "2026-08-17", "effective_date": "2026-08-27",
                    "accession": "cfbi", "classification": "common_delisting", "class_kind": "common"},
                   {"subject_cik": 1599901, "filing_date": "2026-02-27", "effective_date": "2026-03-09",
                    "accession": "rna", "classification": "common_delisting", "class_kind": "common"}])
    master = _master(iv, form25, _profiles(1691507, 1599901), full=days)
    assert master.loc["1691507", "delist_date"] == "2026-08-27"
    assert "review: delist date 2064 days after the last Nasdaq listing 2021-01-01" in master.loc["1691507", "identity_notes"]
    assert "review" not in master.loc["1599901", "identity_notes"]
