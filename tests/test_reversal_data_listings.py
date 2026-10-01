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
    ("http://www.nasdaq.com/screening/companies-by-name.aspx?exchange=nasdaq&market=ADR&render=download", False),
    ("http://www.nasdaq.com/screening/companies-by-name.aspx?exchange=nasdaq&market=NCM&render=download", False),
    # render=download ignores paging, and market=NASDAQ is the whole exchange
    ("http://www.nasdaq.com/screening/companies-by-name.aspx?exchange=nasdaq&page=2&render=download", True),
    ("http://www.nasdaq.com:80/screening/companies-by-name.aspx?exchange=NASDAQ&page=59&render=download", True),
    ("http://www.nasdaq.com/screening/companies-by-name.aspx?exchange=nasdaq&pagesize=200&render=download", True),
    ("http://www.nasdaq.com/screening/companies-by-industry.aspx?exchange=NASDAQ&market=NASDAQ&render=download", True),
    ("http://www.nasdaq.com/screening/companies-by-name.aspx?exchange=nasdaq&page=2", False),
    ("http://www.nasdaq.com/screening/companies-by-name.aspx?exchange=nasdaq&sector=Technology&render=download",
     False),
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
    (" $3,000 ", 3000.0), ("0", None), ("0.0", None), ("$0", None), ("$0.00M", None),
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
    zero = L.parse_company_list(CL_2011.replace('"60755520"', '"0"')).set_index("Symbol")
    assert pd.isna(zero.loc["FLWS", "MarketCap"]) and zero.loc["FLWS", "MarketCap Raw"] == "0"


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
                             "original": "http://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
                             "digest": L.cdx_digest(payload["symdir"])})
    assert sym["parsed_date"] == "2016-03-11" and sym["footer_lag_days"] == 1 and sym["rows"] == 3
    assert sym["digest_match"] == "Y" and sym["capture_time_et"] == "2016-03-12T10:26:15-05:00"
    assert "as_of_session" not in sym
    original = "http://www.nasdaq.com/screening/companies-by-name.aspx?exchange=NASDAQ&render=download"
    # Tuesday 14:03 ET: mid-session, so the values are Monday's close; AAPL's LastSale matches it.
    closes = {"AAPL": {"2016-04-08": 108.66, "2016-04-11": 110.5}, "TFSC": {"2016-04-11": 9.89}}
    cl = L.process_capture({"kind": "companylist", "timestamp": "20160412180351", "original": original,
                            "digest": "NOTTHEDIGEST"}, closes)
    assert cl["parsed_date"] == "2016-04-12" and cl["rows"] == 3 and cl["market_cap_share"] == pytest.approx(2 / 3, abs=1e-4)
    assert (cl["as_of_rule"], cl["capture_phase"]) == ("2016-04-11", "intraday")
    assert (cl["as_of_session"], cl["as_of_check"]) == ("2016-04-11", "inconclusive")  # only 2 names compared
    assert cl["close_matches"] == "2016-04-08:0/1;2016-04-11:2/2"
    assert cl["digest_match"] == "N" and "digest" in cl["notes"]
    unverified = L.process_capture({"kind": "companylist", "timestamp": "20160412180351", "original": original})
    assert (unverified["as_of_session"], unverified["as_of_check"]) == ("2016-04-11", "unverified")


def test_process_capture_records_network_errors(monkeypatch):
    def broken(kind, ts, original):
        raise RuntimeError("https://web.archive.org/web/x: HTTP Error 503: Service Unavailable")
    monkeypatch.setattr(L, "fetch_wayback_raw", broken)
    out = L.process_capture({"kind": "companylist", "timestamp": "20160412180351", "original": "http://x"})
    assert out["parse_error"] == "network_error" and out.get("_frame") is None


def test_get_backs_off_on_503_and_gives_up(monkeypatch):
    calls, sleeps = [], []

    def fake_cached_get(url, cache, **kwargs):
        calls.append(url)
        raise RuntimeError(f"{url}: HTTP Error 503: Service Unavailable")
    monkeypatch.setattr(L, "cached_get", fake_cached_get)
    monkeypatch.setattr(L.time, "sleep", sleeps.append)
    with pytest.raises(RuntimeError):
        L._get("https://web.archive.org/web/1id_/x", L.CACHE / "never_written")
    assert len(calls) == 3 and sleeps == [90, 180]
    calls.clear(), sleeps.clear()

    def timeout(url, cache, **kwargs):
        calls.append(url)
        raise RuntimeError(f"{url}: timed out")
    monkeypatch.setattr(L, "cached_get", timeout)
    with pytest.raises(RuntimeError):
        L._get("https://web.archive.org/web/1id_/x", L.CACHE / "never_written")
    assert len(calls) == 2 and sleeps == [60]
    assert L.throttled(RuntimeError("u: HTTP Error 429: Too Many Requests"))
    assert not L.throttled(RuntimeError("u: HTTP Error 500: oops"))


@pytest.mark.parametrize("timestamp, et, phase, as_of", [
    ("20160412180351", "2016-04-12T14:03:51-04:00", "intraday", "2016-04-11"),      # Tuesday mid-session
    ("20160412120000", "2016-04-12T08:00:00-04:00", "pre_open", "2016-04-11"),
    ("20151009224448", "2015-10-09T18:44:48-04:00", "after_close", "2015-10-08"),   # latest capture still stale
    ("20170617003302", "2017-06-16T20:33:02-04:00", "evening", "2017-06-16"),       # earliest one fresh
    ("20160412010000", "2016-04-11T21:00:00-04:00", "evening", "2016-04-11"),       # UTC date is a day later
    ("20121111010514", "2012-11-10T20:05:14-05:00", "non_session_day", "2012-11-09"),  # Saturday
    ("20121030150000", "2012-10-30T11:00:00-04:00", "non_session_day", "2012-10-26"),  # Hurricane Sandy closure
    ("20171124190000", "2017-11-24T14:00:00-05:00", "after_close", "2017-11-22"),   # 13:00 early close
    ("20171124210000", "2017-11-24T16:00:00-05:00", "evening", "2017-11-24"),
])
def test_session_facts(timestamp, et, phase, as_of):
    facts = L.session_facts(timestamp)
    assert (facts["capture_time_et"], facts["capture_phase"], facts["as_of_rule"]) == (et, phase, as_of)
    assert facts["candidate_sessions"][-1] >= as_of and len(facts["candidate_sessions"]) == 4


def test_resolve_as_of():
    assert L.resolve_as_of("2016-04-11", {"2016-04-08": (0, 20), "2016-04-11": (20, 20)}) == ("2016-04-11", "confirmed")
    assert L.resolve_as_of("2016-04-11", {"2016-04-08": (19, 20), "2016-04-11": (1, 20)}) == ("2016-04-08", "overridden")
    assert L.resolve_as_of("2016-04-11", {"2016-04-08": (8, 20), "2016-04-11": (5, 20)}) == ("2016-04-11", "inconclusive")
    assert L.resolve_as_of("2016-04-11", {"2016-04-11": (2, 2)}) == ("2016-04-11", "inconclusive")
    assert L.resolve_as_of("2016-04-11", {"2016-04-11": (6, 20), "2016-04-08": (0, 20)}) == ("2016-04-11", "confirmed")
    assert L.resolve_as_of("2016-04-11", {"2016-04-11": (0, 0)}) == ("2016-04-11", "unverified")


def test_close_matches_and_reference_loading(tmp_path):
    pd.DataFrame({"date": ["2016-04-08", "2016-04-11"], "close": [108.66, 110.5]}).to_csv(
        tmp_path / "AAPL.csv.gz", index=False)
    closes = L.load_reference_closes(tmp_path, ("AAPL", "MSFT"))
    assert closes == {"AAPL": {"2016-04-08": 108.66, "2016-04-11": 110.5}}
    frame = pd.DataFrame({"Symbol": ["AAPL", "ZZZ"], "LastSale": [110.5, None]})
    assert L.close_matches(frame, ["2016-04-08", "2016-04-11", "2016-04-12"], closes) == {
        "2016-04-08": (0, 1), "2016-04-11": (1, 1), "2016-04-12": (0, 0)}
    assert L.format_matches({"2016-04-11": (1, 1), "2016-04-12": (0, 0)}) == "2016-04-11:1/1"


def _parsed(ts, rows, session, date=None):
    return {"kind": "companylist", "timestamp": ts, "rows": rows, "as_of_session": session,
            "parsed_date": date or f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}", "_frame": pd.DataFrame({"Symbol": ["A"]})}


def test_choose_snapshots_keeps_one_company_list_per_session():
    rows = [
        _parsed("20160412050051", 2950, "2016-04-11"),
        _parsed("20160412173931", 2990, "2016-04-11"),   # same session, more rows: kept
        _parsed("20160412180351", 2990, "2016-04-11"),
        _parsed("20160409120000", 2900, "2016-04-08"),   # Saturday and Sunday captures of Friday's values
        _parsed("20160410120000", 2900, "2016-04-08"),
        _parsed("20160413010000", 2900, "2016-04-12"),   # Tuesday 21:00 ET ...
        _parsed("20160413150000", 2990, "2016-04-12"),   # ... and Wednesday mid-session: same session
        _parsed("20160414030000", 2900, "2016-04-13"),   # Wednesday 23:00 ET ...
        _parsed("20160414150000", 2900, "2016-04-13"),
        _parsed("20160414233000", 2950, "2016-04-14"),   # ... and Thursday 19:30 ET: one UTC date, two sessions
        {**_parsed("20160415000000", 10, "2016-04-14"), "_frame": None},
    ]
    best = L.choose_snapshots(rows)
    kept = {key[1]: (row["timestamp"], row["as_of_session"]) for key, row in best.items()}
    assert kept == {"2016-04-12": ("20160412173931", "2016-04-11"),
                    "2016-04-09": ("20160409120000", "2016-04-08"),
                    "2016-04-13": ("20160413150000", "2016-04-12"),
                    "2016-04-14": ("20160414233000", "2016-04-14")}


def test_choose_snapshots_symbol_files_per_footer_date():
    frame = pd.DataFrame({"Symbol": ["A"]})
    rows = [{"kind": "symdir", "parsed_date": "2016-03-11", "rows": 3090, "timestamp": "20160312152615", "_frame": frame},
            {"kind": "symdir", "parsed_date": "2016-03-11", "rows": 3090, "timestamp": "20160312000000", "_frame": frame},
            {"kind": "symdir", "parsed_date": "2016-03-14", "rows": 3091, "timestamp": "20160315000000", "_frame": frame}]
    best = L.choose_snapshots(rows)
    assert {k: v["timestamp"] for k, v in best.items()} == {("symdir", "2016-03-11"): "20160312000000",
                                                            ("symdir", "2016-03-14"): "20160315000000"}


def test_short_company_lists_are_flagged_against_neighbours():
    ratios = L.neighbour_row_ratios({"1": 3000, "2": 3010, "3": 2000, "4": 3020, "5": 3030})
    assert ratios["3"] == pytest.approx(2000 / 3015, abs=1e-4) and ratios["1"] == pytest.approx(1.0, abs=0.02)
    assert L.neighbour_row_ratios({"1": 3000}) == {}
    processed = [_parsed(ts, n, "") for ts, n in
                 [("20160101000000", 3000), ("20160201000000", 3010), ("20160301000000", 2800),
                  ("20160401000000", 3020), ("20160501000000", 3030)]]
    processed.append({"kind": "symdir", "timestamp": "20160301000000", "rows": 10, "_frame": pd.DataFrame()})
    L.flag_short_company_lists(processed)
    short = processed[2]
    assert short["parse_error"] == "short_vs_neighbours_0.929" and short["_frame"] is None
    assert all(r.get("_frame") is not None for r in processed[:2] + processed[3:])
    assert "rows_vs_neighbours" not in processed[-1]


def test_cdx_digest_is_base32_sha1():
    assert L.cdx_digest(b"") == "3I42H3S6NNFQ2MSVX7XZKYAYSCX5QBYJ"


def _symdir_frame(names, etf=None, test_issue=True):
    frame = pd.DataFrame({"Symbol": [f"S{i}" for i in range(len(names))], "Name": names})
    if etf is not None:
        frame["ETF"] = etf
    if test_issue:
        frame["Test Issue"] = "N"
    return frame


def test_snapshot_quality_levels():
    typed = ["Apple Inc. - Common Stock", "PowerShares QQQ Trust, Series 1", "Foo Corp. - Warrant", "Bar - Units"]
    full = L.snapshot_quality(_symdir_frame(typed, etf=["N", "Y", "N", "N"]), "symdir")
    assert (full["quality"], full["has_names"], full["has_security_type"], full["has_etf_flag"]) == ("full", "Y", "Y", "Y")
    assert full["quality_note"] == ""
    no_etf = L.snapshot_quality(_symdir_frame(typed), "symdir")
    assert no_etf["quality"] == "no_etf_flag" and "no ETF column" in no_etf["quality_note"]
    catalog = L.snapshot_quality(_symdir_frame(["Armada Acquisition Corp. I Common Stock", "Armada Warrant",
                                                "X Corp Ordinary Shares", "Y Inc"], test_issue=False),
                                 "symdir", "json_catalog")
    assert catalog["quality"] == "no_etf_flag" and catalog["quality_note"].startswith("Source Format=json_catalog")
    issuers = L.snapshot_quality(_symdir_frame(["Apple Inc.", "American Airlines Group, Inc.",
                                                "iShares MSCI Asia Index Fund", "Atlantic American Corporation"],
                                               test_issue=False), "symdir")
    assert (issuers["quality"], issuers["has_security_type"]) == ("name_only", "N")
    assert "no Test Issue column" in issuers["quality_note"]
    bare = pd.DataFrame({"Symbol": ["AABA", "AAL", "F", "ZXYZ"], "Name": ["AABA", "AAL", "F", "ZXYZ"]})
    symbol_only = L.snapshot_quality(bare, "symdir", "symbol_only")
    assert (symbol_only["quality"], symbol_only["has_names"]) == ("symbol_only", "N")
    assert L.snapshot_quality(_symdir_frame(typed, etf=["N"] * 4), "symdir", "symbol_only")["quality"] == "symbol_only"
    company = L.snapshot_quality(L.parse_company_list(CL_2016), "companylist")
    assert (company["quality"], company["has_etf_flag"]) == ("name_only", "N")
    assert company["quality_note"].startswith("company list")
    assert list(L.QUALITY_LEVELS) == ["full", "no_etf_flag", "name_only", "symbol_only"]


@pytest.mark.parametrize("source_file, repository, commit, imported, expected", [
    ("https://raw.githubusercontent.com/a/b/abc/nasdaqlisted.txt", "https://github.com/a/b", "abc", None,
     "https://raw.githubusercontent.com/a/b/abc/nasdaqlisted.txt"),
    ("data/nasdaqlisted.txt", "https://github.com/SamPom100/UnusualVolumeDetector.git", "9be4c99", None,
     "https://github.com/SamPom100/UnusualVolumeDetector.git@9be4c99:data/nasdaqlisted.txt"),
    ("/private/tmp/quant-nasdaq-2019/Trabalho 2/nasdaqlisted.txt",
     "https://github.com/leotavares/machinelearning-2019.1.git", "02d1150", None,
     "https://github.com/leotavares/machinelearning-2019.1.git@02d1150:Trabalho 2/nasdaqlisted.txt"),
    ("/tmp/clone/x.txt", "https://github.com/a/b", "", None, "https://github.com/a/b:x.txt"),
    ("", "", "f533e2a", ("https://github.com/datasets/nasdaq-listings", "f533e2a", "data/nasdaq-listed-symbols.csv"),
     "https://github.com/datasets/nasdaq-listings@f533e2a:data/nasdaq-listed-symbols.csv"),
    ("", "", "f533e2a", None, "git:f533e2a"),
    ("", "", "", None, ""),
])
def test_repo_provenance(source_file, repository, commit, imported, expected):
    assert L.repo_provenance(source_file, repository, commit, imported) == expected


def test_repo_index_rows_carry_provenance_and_quality(tmp_path):
    pd.DataFrame({"Symbol": ["AABA", "F"], "Name": ["AABA", "F"],
                  "Source File": "/private/tmp/quant-nasdaq-2019/Trabalho 2/nasdaqlisted.txt",
                  "Source Repository": "https://github.com/leotavares/machinelearning-2019.1.git",
                  "Source Commit": "02d1150", "Observed At": "2019-06-17", "Source Format": "symbol_only"}
                 ).to_csv(tmp_path / "nasdaq_listed_2019-06-17.csv", index=False)
    pd.DataFrame({"Symbol": ["AAL"], "Name": ["American Airlines Group, Inc."], "Source Commit": "f533e2a",
                  "Observed At": "2015-01-10"}).to_csv(tmp_path / "nasdaq_listed_2015-01-10.csv", index=False)
    pd.DataFrame({"Symbol": ["AAPL"], "Name": ["Apple Inc. - Common Stock"], "ETF": "N", "Test Issue": "N",
                  "Source File": "https://data.commoncrawl.org/x.warc.gz#offset=1&length=2&timestamp=20180121063325",
                  "Source Repository": "", "Source Commit": "", "Observed At": "2018-01-19"}
                 ).to_csv(tmp_path / "nasdaq_listed_2018-01-19.csv", index=False)
    (tmp_path / "listings_git_import_manifest.json").write_text(json.dumps({
        "source_repository": "https://github.com/datasets/nasdaq-listings",
        "imported": [{"commit": "f533e2a", "source_path": "data/nasdaq-listed-symbols.csv",
                      "snapshot": "stocks_list_dir/nasdaq/snapshots/nasdaq_listed_2015-01-10.csv"}]}))
    rows = {r["snapshot_date"]: r for r in L.repo_index_rows(tmp_path)}
    assert rows["2019-06-17"]["original_url"] == (
        "https://github.com/leotavares/machinelearning-2019.1.git@02d1150:Trabalho 2/nasdaqlisted.txt")
    assert rows["2019-06-17"]["quality"] == "symbol_only"
    assert rows["2015-01-10"]["original_url"] == (
        "https://github.com/datasets/nasdaq-listings@f533e2a:data/nasdaq-listed-symbols.csv")
    assert rows["2015-01-10"]["quality"] == "name_only"
    cc = rows["2018-01-19"]
    assert (cc["quality"], cc["capture_timestamp"], cc["capture_time_et"]) == (
        "full", "20180121063325", "2018-01-21T01:33:25-05:00")
    assert not any("/tmp/" in r["original_url"] for r in rows.values())
    assert set(L.INDEX_COLUMNS) >= set(cc)


def test_retire_stale_snapshots(tmp_path):
    for name in ("nasdaq_companylist_2016-04-11.csv", "nasdaq_companylist_2016-04-12.csv", "other.csv"):
        (tmp_path / name).write_text("x")
    moved = L.retire_stale_snapshots(tmp_path, "nasdaq_companylist_*.csv", {tmp_path / "nasdaq_companylist_2016-04-12.csv"})
    assert moved == ["nasdaq_companylist_2016-04-11.csv"]
    assert (tmp_path / "superseded" / "nasdaq_companylist_2016-04-11.csv").exists()
    assert sorted(p.name for p in tmp_path.glob("*.csv")) == ["nasdaq_companylist_2016-04-12.csv", "other.csv"]


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
