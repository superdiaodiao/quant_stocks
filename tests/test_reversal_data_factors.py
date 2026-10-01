"""Offline tests for the step 1 parsers (Ken French, Siccodes, CBOE VIX, QQQ coverage)."""
import gzip
import io
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
