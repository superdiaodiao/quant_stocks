"""Offline tests for scripts/reversal_data_listings.py (inline fixtures, no network)."""
import json

import pandas as pd
import pytest

from scripts import reversal_data_listings as L


SYMDIR_2011 = (
    "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size\n"
    "AAPL|Apple Inc. - Common Stock|Q|N|N|100\n"
    "AACOW|Australia Acquisition Corp. - Warrant|S|N|N|100\n"
    "AAXJ|iShares MSCI All Country Asia ex Japan Index Fund|G|N|N|100\n"
    "ZVZZT|NASDAQ TEST STOCK|G|Y|N|100\n"
    "File Creation Time: 1231201018:04|||||\n"
)
SYMDIR_2016 = (
    "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\n"
    "AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N\n"
    "QQQ|PowerShares QQQ Trust, Series 1|G|N|N|100|Y|N\n"
    "MSFT|Microsoft Corporation - Common Stock|Q|N|N|100|N|N\n"
    "File Creation Time: 0311201622:01||||||\n"
)
CL_2011 = (
    '"Symbol","Name","LastSale","MarketCap","IPOyear","Sector","industry","Summary Quote",\n'
    '"FLWS","1-800 FLOWERS.COM, Inc.","2.24","60755520","1999","Consumer Services","Other Specialty Stores","http://x",\n'
    '"AAPL","Apple Inc.","343.21","316000000000","1980","Technology","Computer Manufacturing","http://x",\n'
    '"ZIGO","Zygo Corporation","11.17","n/a","n/a","Capital Goods","Electronic Components","http://x",\n'
)
CL_2016 = (
    '"Symbol","Name","LastSale","MarketCap","IPOyear","Sector","industry","Summary Quote",\n'
    '"TFSC","1347 Capital Corp.","9.89","$58.83M","2014","Finance","Business Services","http://x",\n'
    '"TFSCW","1347 Capital Corp.","0.17","n/a","2014","Finance","Business Services","http://x",\n'
    '"AAPL","Apple Inc.","110.5","$612.3B","1980","Technology","Computer Manufacturing","http://x",\n'
)
CL_2019 = (
    '"Symbol","Name","LastSale","MarketCap","ADR TSO","IPOyear","Sector","Industry","Summary Quote",\n'
    '"YI","111, Inc.","7.4","95513243","12907195","2018","Health Care","Medical/Nursing Services","https://x",\n'
    '"ZNGA","Zynga Inc.","6.3","5892281526.6","n/a","2011","Technology","EDP Services","https://x",\n'
)


def test_cdx_json_rows_become_dicts():
    data = json.dumps([L.CDX_FIELDS, ["20110101155420", "http://a/b", "200", "text/plain", "D1", "39541"]])
    rows = L.parse_cdx_json(data.encode())
    assert rows == [{"timestamp": "20110101155420", "original": "http://a/b", "statuscode": "200",
                     "mimetype": "text/plain", "digest": "D1", "length": "39541"}]
    assert L.parse_cdx_json(b"") == [] and L.parse_cdx_json("[]") == []


def test_cdx_url_and_raw_url():
    url = L.cdx_url("nasdaq.com/screening/companies-by-name.aspx", "prefix", "2011", "2019")
    assert url.startswith("https://web.archive.org/cdx/search/cdx?url=nasdaq.com/screening/companies-by-name.aspx")
    assert "matchType=prefix" in url and "from=2011" in url and "to=2019" in url and "output=json" in url
    assert (L.raw_capture_url("20110101155420", "http://www.nasdaqtrader.com:80/dynamic/SymDir/nasdaqlisted.txt")
            == "https://web.archive.org/web/20110101155420id_/http://www.nasdaqtrader.com:80/dynamic/SymDir/nasdaqlisted.txt")


@pytest.mark.parametrize("url, full", [
    ("http://www.nasdaq.com:80/screening/companies-by-name.aspx?exchange=NASDAQ&render=download", True),
    ("http://www.nasdaq.com/screening/companies-by-name.aspx?letter=0&exchange=nasdaq&render=download", True),
    ("http://www.nasdaq.com/screening/companies-by-name.aspx?letter=0!exchange=nasdaq!render=download", True),
    ("http://www.nasdaq.com/screening/companies-by-industry.aspx?industry=ALL&exchange=NASDAQ&sortname=marketcap"
     "&sorttype=1&render=download", True),
    ("http://www.nasdaq.com/screening/companies-by-name.aspx?exchange=NYSE&render=download", False),
    ("http://www.nasdaq.com/screening/companies-by-name.aspx?letter=a&exchange=nasdaq&render=download", False),
    ("http://www.nasdaq.com/screening/companies-by-industry.aspx?industry=Technology&exchange=nasdaq&render=download",
     False),
    ("http://www.nasdaq.com/screening/companies-by-industry.aspx?industry=ALL&exchange=NASDAQ&market=NGS"
     "&render=download", False),
    ("http://www.nasdaq.com/screening/companies-by-name.aspx?exchange=nasdaq&page=2&render=download", False),
    ("http://www.nasdaq.com/screening/companies-by-name.aspx?exchange=nasdaq", False),
])
def test_full_nasdaq_company_list_filter(url, full):
    assert L.is_full_nasdaq_company_list(url) is full


def test_select_captures_keeps_first_of_each_digest_and_skips_bad_rows():
    base = "http://www.nasdaq.com/screening/companies-by-name.aspx?exchange=NASDAQ&render=download"
    rows = [
        {"timestamp": "20120102000000", "original": base, "statuscode": "200", "mimetype": "application/text",
         "digest": "X", "length": "1"},
        {"timestamp": "20120101000000", "original": base, "statuscode": "200", "mimetype": "application/text",
         "digest": "X", "length": "1"},
        {"timestamp": "20120103000000", "original": base, "statuscode": "301", "mimetype": "text/plain",
         "digest": "Y", "length": "1"},
        {"timestamp": "20120104000000", "original": base, "statuscode": "200", "mimetype": "text/html",
         "digest": "Z", "length": "1"},
        {"timestamp": "20120105000000", "original": base.replace("NASDAQ", "NYSE"), "statuscode": "200",
         "mimetype": "application/text", "digest": "W", "length": "1"},
    ]
    out = {r["timestamp"]: r for r in L.select_captures("companylist", rows)}
    assert out["20120101000000"]["action"] == "fetch"
    assert out["20120102000000"]["reason"] == "duplicate_digest_of_20120101000000"
    assert out["20120103000000"]["reason"] == "status_301"
    assert out["20120104000000"]["reason"] == "mimetype_text/html"
    assert out["20120105000000"]["reason"] == "not_full_nasdaq_list"


@pytest.mark.parametrize("raw, dollars", [
    ("60755520", 60755520.0), ("33430967.84", 33430967.84), ("$58.83M", 58.83e6), ("$1.2B", 1.2e9),
    ("$612.3B", 612.3e9), ("$120K", 120e3), ("$1.01T", 1.01e12), ("n/a", None), ("", None), ("abc", None),
    (" $3,000 ", 3000.0),
])
def test_market_cap_parsing(raw, dollars):
    value = L.parse_market_cap(raw)
    assert (value is None and dollars is None) or value == pytest.approx(dollars)


@pytest.mark.parametrize("text", [CL_2011, CL_2016, CL_2019])
def test_company_list_formats(text):
    frame = L.parse_company_list(text)
    assert list(frame.columns) == L.COMPANYLIST_COLUMNS
    assert frame["Symbol"].is_monotonic_increasing and frame["Symbol"].ne("").all()
    assert not any(c.startswith("Unnamed") for c in frame.columns)
    assert frame["Industry"].ne("").all()


def test_company_list_values():
    old = L.parse_company_list(CL_2011).set_index("Symbol")
    assert old.loc["AAPL", "MarketCap"] == pytest.approx(316e9)
    assert pd.isna(old.loc["ZIGO", "MarketCap"]) and old.loc["ZIGO", "MarketCap Raw"] == "n/a"
    assert old.loc["FLWS", "LastSale"] == pytest.approx(2.24)
    new = L.parse_company_list(CL_2016).set_index("Symbol")
    assert new.loc["TFSC", "MarketCap"] == pytest.approx(58.83e6)
    assert new.loc["AAPL", "MarketCap"] == pytest.approx(612.3e9)
    adr = L.parse_company_list(CL_2019).set_index("Symbol")
    assert adr.loc["YI", "ADR TSO"] == "12907195"
    assert L.market_cap_checks(L.parse_company_list(CL_2016)) == {"aapl_mcap_bn": 612.3, "mcap_over_1p5t": ""}


def test_html_is_not_a_company_list():
    with pytest.raises(ValueError):
        L.parse_company_list("<!DOCTYPE html><html><body>Symbol</body></html>")
    with pytest.raises(ValueError):
        L.parse_company_list("")


def test_company_list_completeness():
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    rows = [f'"S{i}","{letters[i % 26]}name {i}","1","$1M","n/a","x","y","z",' for i in range(30)]
    text = '"Symbol","Name","LastSale","MarketCap","IPOyear","Sector","industry","Summary Quote",\n' + "\n".join(rows)
    frame = L.parse_company_list(text)
    assert L.company_list_problem(text, frame, minimum_rows=10) == ""
    assert L.company_list_problem(text, frame, minimum_rows=100) == "too_few_rows_30"
    cut = text + '\n"S99","Zeta Co","1","$1M",'
    assert L.company_list_problem(cut, L.parse_company_list(cut), minimum_rows=10) == "truncated_last_row"
    with pytest.raises(Exception):  # a cut inside a quoted field does not parse at all
        L.parse_company_list(text + '\n"S99","Zeta Co","1","$1')
    early = "\n".join(text.splitlines()[:12])
    assert L.company_list_problem(early, L.parse_company_list(early), minimum_rows=5).startswith("names_cover_")


def test_symdir_footer_dates_the_file():
    assert L.symdir_footer(SYMDIR_2011)[0].isoformat() == "2010-12-31"
    assert L.symdir_footer("no footer") == (None, "")


def test_symdir_is_normalised_by_the_repo_importer():
    frame, reason = L.normalise_symdir(SYMDIR_2011, minimum_rows=2)
    assert reason == ""
    assert list(frame["Symbol"]) == ["AACOW", "AAPL", "AAXJ", "ZVZZT"]
    assert set(frame["Observed At"]) == {"2010-12-31"}
    assert "Source Commit" not in frame and "Test Issue" in frame
    # warrant, index fund and test issue are dropped by the common filter
    assert L.common_rows(frame) == 1
    newer, _ = L.normalise_symdir(SYMDIR_2016, minimum_rows=2)
    assert set(newer["Observed At"]) == {"2016-03-11"}
    assert L.common_rows(newer) == 2  # QQQ is flagged ETF


def test_symdir_without_footer_or_rows_is_rejected():
    assert L.normalise_symdir(SYMDIR_2011.rsplit("File", 1)[0], minimum_rows=2) == (None, "missing_file_creation_time")
    assert L.normalise_symdir(SYMDIR_2011, minimum_rows=50)[1] == "too_few_rows"


def test_slug_is_safe_and_distinct():
    a = L.slug("http://www.nasdaq.com:80/screening/companies-by-name.aspx?exchange=NASDAQ&render=download")
    b = L.slug("http://www.nasdaq.com:80/screening/companies-by-name.aspx?letter=0&exchange=nasdaq&render=download")
    assert a != b
    assert all(ch.isalnum() or ch in "._-" for ch in a)
    assert a.startswith("nasdaq.com")


def test_process_capture_parses_without_network(monkeypatch):
    monkeypatch.setattr(L, "MINIMUM_ROWS", 2)
    payload = {"symdir": SYMDIR_2016.encode(), "companylist": CL_2016.encode()}
    monkeypatch.setattr(L, "fetch_wayback_raw", lambda kind, ts, original: payload[kind])
    monkeypatch.setattr(L, "company_list_problem", lambda text, frame: "")
    sym = L.process_capture({"kind": "symdir", "timestamp": "20160312152615",
                             "original": "http://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"})
    assert sym["parsed_date"] == "2016-03-11" and sym["footer_lag_days"] == 1 and sym["rows"] == 3
    cl = L.process_capture({"kind": "companylist", "timestamp": "20160412180351",
                            "original": "http://www.nasdaq.com/screening/companies-by-name.aspx?exchange=NASDAQ"
                                        "&render=download"})
    assert cl["parsed_date"] == "2016-04-12" and cl["rows"] == 3 and cl["market_cap_share"] == pytest.approx(2 / 3, abs=1e-4)


def test_choose_per_date_prefers_more_rows_then_earlier():
    frame = pd.DataFrame({"Symbol": ["A"], "Name": ["a"]})
    rows = [
        {"kind": "companylist", "parsed_date": "2016-04-12", "rows": 2900, "timestamp": "20160412180351", "_frame": frame},
        {"kind": "companylist", "parsed_date": "2016-04-12", "rows": 2950, "timestamp": "20160412173931", "_frame": frame},
        {"kind": "companylist", "parsed_date": "2016-04-12", "rows": 2950, "timestamp": "20160412050051", "_frame": frame},
        {"kind": "companylist", "parsed_date": "2016-04-13", "rows": 10, "timestamp": "20160413000000", "_frame": None},
    ]
    best = L.choose_per_date(rows)
    assert list(best) == [("companylist", "2016-04-12")]
    assert best[("companylist", "2016-04-12")]["timestamp"] == "20160412050051"


def test_gap_table_includes_window_edges():
    gaps = L.gap_table(["2011-03-01", "2011-12-31", "2019-06-30"], top=2)
    assert gaps[0] == {"from": "2011-12-31", "to": "2019-06-30", "days": 2738}
    assert gaps[1] == {"from": "2011-03-01", "to": "2011-12-31", "days": 305}
    assert L.gap_table([]) == []
    edged = L.gap_table(["2010-12-31", "2011-10-07", "2020-01-02"], top=5)
    assert edged == [{"from": "2011-10-07", "to": "2020-01-02", "days": 3009},
                     {"from": "2010-12-31", "to": "2011-10-07", "days": 280}]


def test_compare_snapshots():
    a = pd.DataFrame({"Symbol": ["AAPL", "MSFT", "OLD"], "Name": ["Apple Inc. - Common Stock", "Microsoft", "Old Co"]})
    b = pd.DataFrame({"Symbol": ["AAPL", "MSFT", "NEW"], "Name": ["Apple  Inc. - Common Stock", "Microsoft Corp", "New"]})
    out = L.compare_snapshots(a, b)
    assert (out["in_both"], out["only_a"], out["only_b"]) == (2, 1, 1)
    assert out["name_mismatch_in_both"] == 1 and out["examples_name_mismatch"][0][0] == "MSFT"
    assert out["jaccard"] == 0.5


def test_index_snapshot_paths_resolve_from_the_repo_root():
    assert L.resolve_snapshot("research_cache/reversal_2012_2026/listings/symdir/x.csv") == (
        L.CACHE / "listings" / "symdir" / "x.csv")
    assert str(L.resolve_snapshot("stocks_list_dir/nasdaq/snapshots/x.csv")) == "stocks_list_dir/nasdaq/snapshots/x.csv"
