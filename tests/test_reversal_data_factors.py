"""Offline tests for step 1 (Ken French, Siccodes, CBOE VIX, the QQQ join, tail and dividend evidence)."""
import gzip
import io
import json
import zipfile

import pytest

from scripts import reversal_data_common as common
from scripts import reversal_data_factors as factors

FACTOR_FILE = (
    "This file was created by using the 202608 CRSP database.  It\r\n"
    "contains a momentum factor.\r\n"
    "\r\n"
    "Missing data are indicated by -99.99 or -999.\r\n"
    "\r\n"
    ",Mom\r\n"
    "20260828,   0.55\r\n"
    "20260831,  -0.07\r\n"
    "\r\n"
    "Copyright 2026 Eugene F. Fama and Kenneth R. French\r\n"
)

INDUSTRY_FILE = (
    "This file was created using the 202608 CRSP database.\r\n"
    "It contains value- and equal-weighted returns for 49 industry portfolios.\r\n"
    "\r\n"
    "Missing data are indicated by -99.99 or -999.\r\n"
    "\r\n"
    "\r\n"
    "  Average Value Weighted Returns -- Daily\r\n"
    ",Agric,Food,Soda\r\n"
    "19260701,   0.56,  -0.07, -99.99\r\n"
    "19260702,   0.29,   0.06,   1.20\r\n"
    "\r\n"
    "\r\n"
    "  Average Equal Weighted Returns -- Daily\r\n"
    ",Agric,Food,Soda\r\n"
    "19260701,   0.10,  -999,   0.00\r\n"
    "19260702,  -0.00,   0.06,  12.30\r\n"
    "\r\n"
    "Copyright 2026 Eugene F. Fama and Kenneth R. French\r\n"
)

SIC49 = (
    " 1 Agric  Agriculture\r\n"
    "          0100-0199 Agricultural production - crops\r\n"
    "          2048-2048 Prepared feeds for animals\r\n"
    "\r\n"
    " 2 Food   Food Products\r\n"
    "          2000-2009 Food and kindred products\r\n"
    "          6724-6724 Unit investment trusts                          \r\n"
)

SIC12 = (
    " 1 NoDur  Consumer Nondurables -- Food, Tobacco, Textiles, Apparel, Leather, Toys\r\n"
    "          0100-0999\r\n"
    "          2000-2399\r\n"
    "\r\n"
    " 2 Other  Other -- Mines, Constr, BldMt, Trans, Hotels, Bus Serv, Entertainment\r\n"
)


def test_factor_file_keeps_preamble_stamp_and_footer():
    parsed = factors.parse_kf_daily(FACTOR_FILE)
    assert parsed["crsp_build"] == "202608"
    assert parsed["stamp_line"].startswith("This file was created by using the 202608")
    assert parsed["preamble"][-1] == "Missing data are indicated by -99.99 or -999."
    assert parsed["footer"] == ["Copyright 2026 Eugene F. Fama and Kenneth R. French"]
    (block,) = parsed["blocks"]
    assert block["title"] == "" and block["columns"] == ["Mom"]
    assert block["dates"] == ["20260828", "20260831"] and block["values"] == [["0.55"], ["-0.07"]]


def test_industry_file_has_two_titled_blocks():
    parsed = factors.parse_kf_daily(INDUSTRY_FILE)
    assert [b["title"] for b in parsed["blocks"]] == [
        "Average Value Weighted Returns -- Daily", "Average Equal Weighted Returns -- Daily"]
    assert [factors.weighting(b["title"]) for b in parsed["blocks"]] == ["vw", "ew"]
    # Block titles are not counted as preamble or footer text.
    assert all("Average" not in line for line in parsed["preamble"] + parsed["footer"])
    assert parsed["footer"] == ["Copyright 2026 Eugene F. Fama and Kenneth R. French"]
    assert parsed["crsp_build"] == "202608"


def test_tidy_rows_blank_missing_markers_and_convert_exactly():
    rows = factors.tidy_industry_rows(factors.parse_kf_daily(INDUSTRY_FILE))
    assert rows[0] == ["1926-07-01", "vw", "1", "Agric", "0.56", "0.0056", ""]
    assert rows[2] == ["1926-07-01", "vw", "3", "Soda", "", "", "-99.99"]
    by_key = {(r[0], r[1], r[3]): r for r in rows}
    assert by_key[("1926-07-01", "ew", "Food")][4:] == ["", "", "-999"]
    assert by_key[("1926-07-02", "ew", "Agric")][4:] == ["-0.00", "0.0000", ""]
    assert by_key[("1926-07-02", "ew", "Soda")][4:] == ["12.30", "0.1230", ""]
    assert len(rows) == 2 * 2 * 3
    factor_rows = factors.tidy_factor_rows(factors.parse_kf_daily(FACTOR_FILE))
    assert factor_rows == [["2026-08-28", "Mom", "0.55", "0.0055", ""], ["2026-08-31", "Mom", "-0.07", "-0.0007", ""]]


def test_decimal_conversion_has_no_float_noise():
    assert factors.to_decimal_text("0.07") == "0.0007"
    assert factors.to_decimal_text("-12.34") == "-0.1234"
    assert factors.to_decimal_text("-0.00") == "0.0000"
    assert factors.is_missing("-99.99") and factors.is_missing("-999") and factors.is_missing("-999.00")
    assert not factors.is_missing("-99.98") and not factors.is_missing("abc")


@pytest.mark.parametrize("bad, message", [
    (",A,B\n20260101, 1.0\n", "values for 2 columns"),
    (",A\n20260102, 1.0\n20260101, 1.0\n", "does not follow"),
    (",A\n20260102, 1.0\n20260102, 1.0\n", "does not follow"),
    ("20260102, 1.0\n", "before any header"),
    ("just text\n", "no data block"),
])
def test_malformed_factor_files_raise(bad, message):
    with pytest.raises(ValueError, match=message):
        factors.parse_kf_daily(bad)


def test_non_numeric_value_raises():
    with pytest.raises(Exception):
        factors.parse_kf_daily(",A\n20260102, n/a\n")


def test_tidy_factor_rows_rejects_multi_block_files():
    with pytest.raises(ValueError, match="2 blocks"):
        factors.tidy_factor_rows(factors.parse_kf_daily(INDUSTRY_FILE))


def test_siccodes_ranges_and_header_only_industry():
    rows = factors.parse_siccodes(SIC49, "FF49")
    assert [(r["industry_id"], r["short_name"], r["sic_lo"], r["sic_hi"]) for r in rows] == [
        (1, "Agric", "0100", "0199"), (1, "Agric", "2048", "2048"), (2, "Food", "2000", "2009"),
        (2, "Food", "6724", "6724")]
    assert rows[0]["industry_name"] == "Agriculture"
    assert rows[3]["range_description"] == "Unit investment trusts"
    assert factors.sic_overlaps(rows) == []
    rows12 = factors.parse_siccodes(SIC12, "FF12")
    assert rows12[-1] == {"scheme": "FF12", "industry_id": 2, "short_name": "Other",
                          "industry_name": "Other -- Mines, Constr, BldMt, Trans, Hotels, Bus Serv, Entertainment",
                          "sic_lo": "", "sic_hi": "", "range_description": ""}
    assert rows12[0]["range_description"] == ""


def test_siccodes_errors_and_overlaps():
    with pytest.raises(ValueError, match="not 1..n"):
        factors.parse_siccodes(" 2 Food   Food\n          2000-2009 x\n", "FF49")
    with pytest.raises(ValueError, match="reversed"):
        factors.parse_siccodes(" 1 Food   Food\n          2009-2000 x\n", "FF49")
    with pytest.raises(ValueError, match="before any industry"):
        factors.parse_siccodes("          2000-2009 x\n", "FF49")
    rows = factors.parse_siccodes(" 1 A  a\n          2000-2009 x\n 2 B  b\n          2005-2005 y\n", "FFX")
    overlaps = factors.sic_overlaps(rows)
    assert overlaps == [{"a": "2000-2009 (1)", "b": "2005-2005 (2)", "same_industry": False}]


def test_vix_csv_parses_us_dates_and_rejects_bad_files():
    text = "DATE,OPEN,HIGH,LOW,CLOSE\r\n01/02/1990,17.240000,17.240000,17.240000,17.240000\r\n" \
           "03/16/2020,57.830002,83.559998,57.830002,82.690002\r\n\r\n"
    rows = factors.parse_vix_csv(text)
    assert rows == [{"date": "1990-01-02", "open": "17.240000", "high": "17.240000", "low": "17.240000",
                     "close": "17.240000"},
                    {"date": "2020-03-16", "open": "57.830002", "high": "83.559998", "low": "57.830002",
                     "close": "82.690002"}]
    with pytest.raises(ValueError, match="header"):
        factors.parse_vix_csv("Date,Close\n01/02/1990,1\n")
    with pytest.raises(ValueError, match="does not follow"):
        factors.parse_vix_csv("DATE,OPEN,HIGH,LOW,CLOSE\n01/03/1990,1,1,1,1\n01/02/1990,1,1,1,1\n")


def test_links_and_zip_members():
    html = '<a href="ftp/F-F_Momentum_Factor_daily_CSV.zip">x</a><a href="../ftp/Siccodes49.zip">'
    assert factors.linked(html, "F-F_Momentum_Factor_daily_CSV.zip")
    assert factors.linked(html, "Siccodes49.zip")
    assert not factors.linked(html, "Siccodes17.zip")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(zipfile.ZipInfo("Siccodes12.txt", date_time=(2020, 1, 8, 16, 3, 48)), SIC12)
    facts, text = factors.zip_text(buffer.getvalue())
    assert text == SIC12 and facts["member"] == "Siccodes12.txt"
    assert facts["member_zip_datetime"] == "2020-01-08T16:03:48"
    with zipfile.ZipFile(buffer, "a") as archive:
        archive.writestr("second.txt", "x")
    with pytest.raises(ValueError, match="expected one member"):
        factors.zip_text(buffer.getvalue())


def test_session_diff_and_window_facts(monkeypatch):
    sessions = ["2011-06-01", "2011-06-02", "2011-06-03", "2011-06-06"]
    diff = factors.session_diff(["2011-05-31", "2011-06-01", "2011-06-03", "2011-06-04"], sessions)
    assert diff == {"missing_sessions": ["2011-06-02", "2011-06-06"], "extra_dates": ["2011-06-04"]}
    monkeypatch.setattr(factors, "ROW_CHECK", ("2011-06-02", "2011-06-03", 2))
    facts = factors.window_facts(["2011-05-31", "2011-06-01", "2011-06-02", "2011-06-03"], {"2011-06-02"}, sessions)
    assert facts["covers_window_start"] and not facts["covers_window_end"]
    assert facts["rows_in_window"] == 3 and facts["rows_with_missing_marker_in_window"] == 1
    assert facts["missing_sessions"] == [] and facts["sessions_after_last_date_to_window_end"] == 1
    assert facts["row_check"]["found"] == 2 and facts["last_month"] == "2011-06"


def test_qqq_check_reports_gaps_and_dividend_agreement(tmp_path, monkeypatch):
    tiingo = tmp_path / "t.csv"
    tiingo.write_text("date,close,adjClose,divCash,splitFactor,volume\n"
                      "2017-12-28,157.0,150.0,0.0,1.0,1\n"
                      "2017-12-29,156.0,149.0,0.4,1.0,1\n"
                      "2018-01-02,158.49,151.0,0.0,1.0,1\n"
                      "2018-01-03,160.03,152.0,0.0,1.0,1\n")
    nasdaq = tmp_path / "n.csv"
    nasdaq.write_text("date,open,high,low,close,volume,cash_dividend\n"
                      "2018-01-02,1,1,1,158.49,1,0.0\n"
                      "2018-01-03,1,1,1,160.03,1,0.0\n"
                      "2018-01-05,1,1,1,161.92,1,0.2\n")
    sessions = ["2017-12-28", "2017-12-29", "2018-01-02", "2018-01-03", "2018-01-04", "2018-01-05", "2018-01-08"]
    monkeypatch.setattr(factors, "WINDOW_START", "2017-12-28")
    monkeypatch.setattr(factors, "QQQ_DIVIDEND_COUNT", ("2017-12-01", "2018-01-31", 2))
    result = factors.qqq_coverage_check(tiingo, nasdaq, sessions=sessions)
    assert result["files"]["nasdaq"]["missing_sessions_in_span"] == ["2018-01-04"]
    assert result["files"]["tiingo"]["dividends"] == {"2017-12-29": "0.4"}
    assert result["files"]["tiingo"]["sha256"] == common.sha256_file(tiingo)
    overlap = result["overlap"]
    assert overlap["common_dates"] == 2 and overlap["max_abs_close_diff"] == 0.0
    assert overlap["from"] == "2018-01-02" and overlap["to"] == "2018-01-03"
    joined = result["joined"]
    assert joined["last_date"] == "2018-01-05" and not joined["covers_window"]
    assert joined["missing_sessions_to_window_end"] == ["2018-01-04", "2018-01-08"]
    assert joined["missing_sessions_before_last_date"] == ["2018-01-04"]
    assert joined["dividend_count_check"]["found"] == 2
    assert joined["quarters_without_dividend"] == []


def test_kf_zip_is_read_from_cache_without_a_request(tmp_path, monkeypatch):
    monkeypatch.setattr(factors, "RAW_KF", tmp_path)
    (tmp_path / "Siccodes12.zip").write_bytes(b"cached zip bytes")
    monkeypatch.setattr(common, "urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no request")))
    data, path = factors.fetch_kf("Siccodes12.zip")
    assert data == b"cached zip bytes" and path == tmp_path / "Siccodes12.zip"


def test_write_csv_gzips_deterministically(tmp_path):
    path = tmp_path / "x.csv.gz"
    first = factors.write_csv(path, ["a", "b"], [["1", "2"], {"a": "3", "b": "4"}])
    assert gzip.decompress(path.read_bytes()) == b"a,b\n1,2\n3,4\n"
    assert factors.write_csv(path, ["a", "b"], [["1", "2"], {"a": "3", "b": "4"}]) == first


# ---------------------------------------------------------------- round 2: low defects


def test_industry_file_needs_one_vw_and_one_ew_block_with_the_same_columns():
    unindented = INDUSTRY_FILE.replace("  Average Equal Weighted", "Average Equal Weighted")
    parsed = factors.parse_kf_daily(unindented)
    assert [factors.weighting(b["title"]) for b in parsed["blocks"]] == ["vw", "main"]
    with pytest.raises(ValueError, match="expected one 'vw' and one 'ew'"):
        factors.tidy_industry_rows(parsed)
    both_vw = INDUSTRY_FILE.replace("Equal Weighted", "Value Weighted")
    with pytest.raises(ValueError, match="expected one 'vw' and one 'ew'"):
        factors.tidy_industry_rows(factors.parse_kf_daily(both_vw))
    other_columns = INDUSTRY_FILE.replace(",Agric,Food,Soda\r\n19260701,   0.10", ",Agric,Food,Beer\r\n19260701,   0.10")
    with pytest.raises(ValueError, match="different columns"):
        factors.tidy_industry_rows(factors.parse_kf_daily(other_columns))
    factors.check_industry_blocks(factors.parse_kf_daily(INDUSTRY_FILE))  # the published layout passes


def test_scout_headers_are_attached_only_when_the_bytes_match(tmp_path, monkeypatch):
    monkeypatch.setattr(factors, "SCOUT_KF", tmp_path)
    (tmp_path / "a.zip").write_bytes(b"same")
    (tmp_path / "a.headers").write_text("Last-Modified: Wed, 01 Jul 2026 00:00:00 GMT\nETag: \"x\"\n")
    (tmp_path / "b.zip").write_bytes(b"older copy")
    (tmp_path / "b.headers").write_text("Last-Modified: Mon, 01 Jun 2026 00:00:00 GMT\n")
    matched = factors.scout_comparison("a.zip", common.sha256_bytes(b"same"))
    assert matched["scout_sha256_matches"] and matched["scout_http_last_modified"].startswith("Wed")
    assert matched["scout_http_etag"] == '"x"'
    differs = factors.scout_comparison("b.zip", common.sha256_bytes(b"this download"))
    assert differs["scout_sha256_matches"] is False
    assert not any(k.startswith("scout_http_") and k != "scout_http_headers" for k in differs)
    assert differs["scout_http_headers"].startswith("not attached")
    assert factors.scout_comparison("c.zip", "x") == {"scout_copy": None}


def test_vix_window_facts_count_sessions_only(monkeypatch):
    monkeypatch.setattr(factors, "WINDOW_START", "2026-08-27")
    monkeypatch.setattr(factors, "WINDOW_END", "2026-08-31")
    monkeypatch.setattr(factors, "ROW_CHECK", ("2026-08-27", "2026-08-31", 3))
    sessions = ["2026-08-27", "2026-08-28", "2026-08-31"]
    rows = [{"date": d, "xnas_session": s} for d, s in [
        ("2026-08-26", "Y"), ("2026-08-27", "Y"), ("2026-08-28", "Y"), ("2026-08-29", "N"),
        ("2026-08-31", "Y"), ("2026-09-01", "Y"), ("2026-09-07", "N")]]
    facts = factors.vix_window_checks(rows, sessions)
    assert facts["rows_in_window"] == 3 and facts["row_check"]["found"] == 3
    assert facts["missing_sessions"] == [] and facts["extra_dates"] == []
    assert facts["last_date"] == "2026-08-31" and facts["covers_window_end"]
    assert facts["non_session_rows_in_window"] == ["2026-08-29"]
    assert facts["non_session_rows_in_window_on_weekends"] == ["2026-08-29"]
    assert facts["rows_after_window_end"] == 2 and facts["last_published_date"] == "2026-09-07"


# ---------------------------------------------------------------- round 2: QQQ join and tail


TIINGO_ROWS = [{"date": "2011-05-27", "close": "57.0", "divCash": "0.0"},
               {"date": "2011-05-31", "close": "58.36", "divCash": "0.0"},
               {"date": "2011-06-01", "close": "57.09", "divCash": "0.0"},
               {"date": "2017-12-29", "close": "156.0", "divCash": "0.4"},
               {"date": "2018-01-02", "close": "158.49", "divCash": "0.0"}]
NASDAQ_ROWS = [{"date": "2018-01-02", "close": "158.49", "cash_dividend": "0.0"},
               {"date": "2026-08-13", "close": "732.07", "cash_dividend": "0.0"},
               {"date": "2026-08-14", "close": "731.07", "cash_dividend": "0.0"}]
TAIL_ROWS = [{"date": "2026-08-13", "close": "732.07"}, {"date": "2026-08-14", "close": "731.07"},
             {"date": "2026-08-17", "close": "729.87"}, {"date": "2026-09-21", "close": "741.47"}]


def test_join_takes_each_source_in_its_span_and_keeps_one_row_before_the_window():
    joined = factors.qqq_total_return_join(TIINGO_ROWS, NASDAQ_ROWS, TAIL_ROWS, {"2026-09-21": "0.75143"})
    assert [r["date"] for r in joined] == ["2011-05-31", "2011-06-01", "2017-12-29", "2018-01-02", "2026-08-13",
                                           "2026-08-14", "2026-08-17", "2026-09-21"]
    assert [r["close_source"] for r in joined] == ["tiingo_file"] * 3 + ["nasdaq_file"] * 3 + ["nasdaq_api_history"] * 2
    assert [r["in_window"] for r in joined] == ["N"] + ["Y"] * 6 + ["N"]
    assert joined[2]["dividend"] == "0.4" and joined[0]["dividend"] == "0" and joined[3]["dividend"] == "0"
    assert joined[-1] == {"date": "2026-09-21", "close": "741.47", "dividend": "0.75143",
                          "close_source": "nasdaq_api_history", "dividend_source": "nasdaq_api_dividends",
                          "in_window": "N"}
    assert set(joined[0]) == set(factors.QQQ_JOINED_COLUMNS)
    dropped = factors.qqq_total_return_join(TIINGO_ROWS, NASDAQ_ROWS, drop={"2017-12-29": "unconfirmed"})
    row = next(r for r in dropped if r["date"] == "2017-12-29")
    assert row["dividend"] == "0" and row["dividend_source"] == "dropped (unconfirmed)"
    with pytest.raises(ValueError, match="strictly increasing"):
        factors.qqq_total_return_join(TIINGO_ROWS[::-1], NASDAQ_ROWS)


def test_qqq_check_with_the_tail_covers_the_window(tmp_path, monkeypatch):
    tiingo = tmp_path / "t.csv"
    tiingo.write_text("date,close,adjClose,divCash,splitFactor,volume\n2017-12-29,156.0,149.0,0.4,1.0,1\n")
    nasdaq = tmp_path / "n.csv"
    nasdaq.write_text("date,open,high,low,close,volume,cash_dividend\n"
                      "2018-01-02,1,1,1,158.49,1,0.0\n2018-01-03,1,1,1,160.03,1,0.0\n")
    tail = [{"date": "2018-01-02", "close": "158.49"}, {"date": "2018-01-03", "close": "160.04"},
            {"date": "2018-01-04", "close": "161.0"}, {"date": "2018-01-05", "close": "162.0"},
            {"date": "2018-01-08", "close": "163.0"}]
    monkeypatch.setattr(factors, "WINDOW_START", "2017-12-29")
    monkeypatch.setattr(factors, "WINDOW_END", "2018-01-04")
    sessions = ["2017-12-29", "2018-01-02", "2018-01-03", "2018-01-04"]
    result = factors.qqq_coverage_check(tiingo, nasdaq, sessions=sessions, tail=tail,
                                        tail_dividends={"2018-01-05": "0.5", "2018-01-06": "0.1"},
                                        sessions_after=["2018-01-05", "2018-01-08", "2018-01-09"])
    joined = result["joined"]
    assert joined["covers_window"] and joined["missing_sessions_to_window_end"] == []
    assert joined["rows_by_close_source"] == {"tiingo_file": 1, "nasdaq_file": 2, "nasdaq_api_history": 3}
    assert joined["dividends_after_window_end"] == {"2018-01-05": "0.5"}
    assert joined["sessions_after_window_end"] == {"missing_sessions": ["2018-01-09"], "extra_dates": []}
    overlap = result["tail"]["overlap_with_stored_file"]
    assert overlap["common_dates"] == 2 and overlap["max_abs_close_diff"] == "0.01"
    assert overlap["dates_close_diff_over_1c"] == []
    assert result["tail"]["rows_after_stored_file"] == 3
    # 2018-01-06 is a Saturday inside the tail: its dividend would be lost; one after the tail's end would not be.
    assert result["tail"]["dividend_dates_after_stored_file_without_a_price_row"] == ["2018-01-06"]


class _Response(io.BytesIO):
    status = 200


def _isolate_request_log(tmp_path, monkeypatch, body: bytes | None):
    monkeypatch.setattr(common, "RAW_INDEX", tmp_path / "raw_index.csv.gz")
    monkeypatch.setattr(common, "QUOTA_LEDGER", tmp_path / "quota_ledger.csv")
    monkeypatch.setattr(factors, "NASDAQ_QQQ", tmp_path / "nasdaq")
    requests = []

    def fake_urlopen(request, timeout=None):
        if body is None:
            raise AssertionError("no request expected")
        requests.append(request)
        return _Response(body)

    monkeypatch.setattr(common, "urlopen", fake_urlopen)
    return requests


HISTORY_PAYLOAD = json.dumps({"data": {"tradesTable": {"rows": [
    {"date": "08/17/2026", "open": "$1", "high": "$1", "low": "$1", "close": "$729.87", "volume": "1,000"},
    {"date": "06/30/2026", "open": "$1", "high": "$1", "low": "$1", "close": "$700.00", "volume": "1,000"},
    {"date": "07/01/2026", "open": "$1", "high": "$1", "low": "$1", "close": "$733.10", "volume": "1,000"}]}}})
DIVIDEND_PAYLOAD = json.dumps({"data": {"dividends": {"rows": [
    {"exOrEffDate": "09/21/2026", "type": "Cash", "amount": "$0.75143", "declarationDate": "09/19/2026",
     "recordDate": "09/21/2026", "paymentDate": "10/08/2026", "currency": "USD"},
    {"exOrEffDate": "06/22/2026", "type": "Cash", "amount": "$0.81349", "declarationDate": "N/A",
     "recordDate": "06/22/2026", "paymentDate": "07/10/2026", "currency": "USD"}]}}})


def test_fetch_history_runs_unchanged_through_the_cache(tmp_path, monkeypatch):
    from src.io import nasdaq_update

    original = nasdaq_update.urlopen
    requests = _isolate_request_log(tmp_path, monkeypatch, HISTORY_PAYLOAD.encode())
    rows, facts = factors.fetch_qqq_tail("2026-07-01", "2026-09-30")
    assert rows == [{"date": "2026-07-01", "close": "733.1"}, {"date": "2026-08-17", "close": "729.87"}]
    assert len(requests) == 1 and "assetclass=etf" in requests[0].full_url and "_=" in requests[0].full_url
    assert facts["url"].endswith("todate=2026-09-30&limit=5000") and not facts["from_cache"]
    assert nasdaq_update.urlopen is original  # restored after the block
    assert gzip.decompress(common.RAW_INDEX.read_bytes()).decode().count("api.nasdaq.com") == 1
    _isolate_request_log(tmp_path, monkeypatch, None)  # a second run reads the cache
    again, facts = factors.fetch_qqq_tail("2026-07-01", "2026-09-30")
    assert again == rows and facts["from_cache"]


def test_an_empty_history_answer_is_not_pinned(tmp_path, monkeypatch):
    _isolate_request_log(tmp_path, monkeypatch, b'{"data": {"tradesTable": {"rows": []}}}')
    with pytest.raises(ValueError, match="no QQQ rows"):
        factors.fetch_qqq_tail("2026-07-01", "2026-09-30")
    assert not (tmp_path / "nasdaq" / "history_etf_2026-07-01_2026-09-30.json").exists()


def test_dividend_history_runs_unchanged_through_the_cache(tmp_path, monkeypatch):
    from scripts import research_v5_trend_core_satellite as v5

    original = v5.urlopen
    requests = _isolate_request_log(tmp_path, monkeypatch, DIVIDEND_PAYLOAD.encode())
    dividends, facts = factors.fetch_qqq_dividends("2026-10-01")
    assert list(dividends) == ["2026-06-22", "2026-09-21"]
    assert dividends["2026-09-21"] == {"amount": "0.75143", "type": "Cash", "declaration_date": "2026-09-19",
                                       "record_date": "2026-09-21", "payment_date": "2026-10-08"}
    assert dividends["2026-06-22"]["declaration_date"] == ""
    assert len(requests) == 1 and v5.urlopen is original
    assert facts["first"] == "2026-06-22" and facts["url"].endswith("assetclass=etf&limit=500")


# ---------------------------------------------------------------- round 2: QQQ dividend evidence


ISSUER_PAGE = """<div><table id="distributionTable"><thead><tr><th>Ex-Date</th><th>Record Date</th>
<th>Pay Date</th><th>$/Share</th></tr></thead><tbody>
<tr><td>12/27/2023</td><td>12/28/2023</td><td>01/15/2024</td><td> 0.21584 </td><td>0.00000</td></tr>
<tr><td>
  02/27/2014</td><td>03/03/2014</td><td>03/07/2014</td><td>0.37313</td><td>0.37313</td></tr>
</tbody></table><table><tr><td>01/02/2020</td><td>a</td><td>b</td><td>9.99</td></tr></table></div>"""


def test_issuer_distribution_table_is_parsed_and_other_tables_ignored():
    assert factors.parse_invesco_distributions(ISSUER_PAGE) == [
        {"ex_date": "2023-12-27", "record_date": "2023-12-28", "pay_date": "2024-01-15", "amount": "0.21584"},
        {"ex_date": "2014-02-27", "record_date": "2014-03-03", "pay_date": "2014-03-07", "amount": "0.37313"}]
    assert factors.parse_invesco_distributions("<html><body>app shell</body></html>") == []
    with pytest.raises(Exception):
        factors.parse_invesco_distributions(ISSUER_PAGE.replace("0.37313</td><td>0.37313", "n/a</td><td>x"))


def test_financial_highlights_distributions_by_period():
    old = ("Financial Highlights 9 Notes to Financial Statements 10 ... Financial Highlights Year Ended "
           "September 30, 2014 2013 Net Asset Value, beginning of year $ 78.80 $ 68.61 Net investment income (1) "
           "1.41 1.02 Less Distributions from: Net investment income (1.34 ) (0.99 ) Net Asset Value, end of year")
    assert factors.parse_distribution_highlights(old) == [
        {"period": "FY2014", "start": "2013-10-01", "end": "2014-09-30", "per_share": "1.34"},
        {"period": "FY2013", "start": "2012-10-01", "end": "2013-09-30", "per_share": "0.99"}]
    semi = ("Financial Highlights Invesco QQQ Trust SM , Series 1 (QQQ) Six Months Ended March 31, 2026 "
            "(Unaudited) Years Ended September 30, 2025 2024 Per Share Operating Performance: Net investment "
            "income (a) 1.44 2.83 3.03 Distributions to shareholders from: Net investment income (1.53 ) (2.84 ) "
            "(3.04 ) Net asset value at end of period")
    assert [(p["period"], p["start"], p["end"], p["per_share"]) for p in factors.parse_distribution_highlights(semi)] == [
        ("six months to 2026-03-31", "2025-10-01", "2026-03-31", "1.53"),
        ("FY2025", "2024-10-01", "2025-09-30", "2.84"), ("FY2024", "2023-10-01", "2024-09-30", "3.04")]
    assert factors.parse_distribution_highlights(old.replace("(0.99 ) ", "")) == []  # counts must agree
    assert factors.html_text(b"<p>a&nbsp;<b>b</b></p><script>x()</script> c") == "a b c"


def test_dividend_verification_statuses_and_fiscal_totals(monkeypatch):
    monkeypatch.setattr(factors, "WINDOW_START", "2013-10-01")
    joined = [{"date": d, "dividend": v, "dividend_source": "s"} for d, v in [
        ("2013-10-01", "0"), ("2013-12-20", "0.27241"), ("2014-02-27", "0.37313"), ("2014-03-21", "0.20606"),
        ("2014-06-20", "0.2491"), ("2014-09-19", "0.2378"), ("2018-12-24", "0.42061"), ("2026-06-22", "0.81349"),
        ("2026-06-30", "0")]]
    issuer = {d: {"amount": a, "record_date": "r", "pay_date": "p", "captures": ["20250827045133"],
                  "evidence_urls": ["https://web.archive.org/web/20250827045133/x"]}
              for d, a in [("2013-12-20", "0.27241"), ("2014-02-27", "0.37313"), ("2014-03-21", "0.20606"),
                           ("2014-06-20", "0.24910"), ("2014-09-19", "0.23780"), ("2018-12-24", "0.42060"),
                           ("2019-03-18", "0.3")]}
    periods = [{"period": "FY2014", "start": "2013-10-01", "end": "2014-09-30", "per_share": "1.34", "accession": "a"},
               {"period": "FY2014", "start": "2013-10-01", "end": "2014-09-30", "per_share": "1.34", "accession": "b"},
               {"period": "FY2013", "start": "2012-10-01", "end": "2013-09-30", "per_share": "0.99", "accession": "a"}]
    period_checks = factors.fiscal_period_checks(joined, periods)
    assert [p["period"] for p in period_checks] == ["FY2014"]  # FY2013 starts before the join
    assert period_checks[0]["dividend_sum"] == "1.33850" and period_checks[0]["matches"]
    assert period_checks[0]["reports_agree"] and period_checks[0]["reports"] == {"a": "1.34", "b": "1.34"}
    api = {"2014-02-27": {"amount": "0.37313"}, "2014-03-21": {"amount": "0.2"}}
    tiingo = [{"date": "2013-12-20", "divCash": "0.27241"}, {"date": "2014-02-27", "divCash": "0.37313"},
              {"date": "2014-03-21", "divCash": "0.20606"}, {"date": "2014-12-31", "divCash": "0.0"}]
    rows, summary = factors.qqq_dividend_verification(joined, issuer, "2025-08-27", api, tiingo, period_checks)
    by_date = {r["ex_date"]: r for r in rows}
    assert by_date["2014-02-27"]["status"] == "issuer_confirmed" and by_date["2014-02-27"]["dividends_in_quarter"] == 2
    assert by_date["2014-02-27"]["nasdaq_api_match"] == "Y" and by_date["2014-03-21"]["nasdaq_api_match"] == "N"
    assert by_date["2013-12-20"]["nasdaq_api_match"] == ""  # before the API's first ex-date
    assert by_date["2014-06-20"]["tiingo_match"] == "N" and by_date["2018-12-24"]["tiingo_match"] == ""
    assert by_date["2018-12-24"]["issuer_match"] == "Y" and by_date["2018-12-24"]["issuer_diff"] == "0.00001"
    assert by_date["2026-06-22"]["status"] == "vendor_only" and by_date["2026-06-22"]["issuer_match"] == ""
    assert summary["issuer_dividends_missing_from_join"] == ["2019-03-18"]
    assert summary["issuer_nonzero_differences_within_tolerance"][0]["ex_date"] == "2018-12-24"
    assert summary["vendor_only"] == ["2026-06-22"] and summary["fiscal_periods_not_matching"] == []
    several = summary["quarters_with_several_dividends"]["2014Q1"]
    assert [i["ex_date"] for i in several] == ["2014-02-27", "2014-03-21"]
    assert all(i["fiscal_period_matches_without_it"] is False for i in several)
    assert several[0]["issuer_evidence_urls"] == ["https://web.archive.org/web/20250827045133/x"]
    assert set(rows[0]) == set(factors.QQQ_CHECK_COLUMNS)
    joined[2]["dividend"] = "0.5"  # a vendor amount the issuer does not support
    rows, summary = factors.qqq_dividend_verification(joined, issuer, "2025-08-27", api, tiingo, period_checks)
    assert {r["ex_date"]: r["status"] for r in rows}["2014-02-27"] == "issuer_differs"


def test_fresh_dividend_payload_is_compared_with_each_stored_file():
    tiingo = [{"date": "2012-06-15", "divCash": "0.1"}, {"date": "2012-09-21", "divCash": "0.2"},
              {"date": "2017-12-18", "divCash": "0.3"}, {"date": "2018-03-19", "divCash": "0.4"}]
    nasdaq = [{"date": "2018-01-02", "cash_dividend": "0.0"}, {"date": "2018-03-19", "cash_dividend": "0.4"},
              {"date": "2026-08-14", "cash_dividend": "0.0"}]
    api = {"2012-06-15": {"amount": "0.1"}, "2017-12-18": {"amount": "0.31"}, "2018-03-19": {"amount": "0.4"},
           "2026-09-21": {"amount": "0.7"}}
    result = factors.qqq_dividend_sources_check(tiingo, nasdaq, api)
    assert result["tiingo_file_vs_api"]["dates_only_file"] == ["2012-09-21"]
    assert result["tiingo_file_vs_api"]["amounts_differ"] == ["2017-12-18"] and result["tiingo_file_vs_api"]["to"] == "2017-12-31"
    assert result["nasdaq_file_vs_api"]["matched"] == 1 and result["nasdaq_file_vs_api"]["dates_only_api"] == []
    assert result["api_last_ex_date"] == "2026-09-21"
