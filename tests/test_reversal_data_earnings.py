"""Tests for plan step 10, earnings dates and SIC (scripts/reversal_data_earnings.py)."""
import pandas as pd
import pytest

from scripts import reversal_data_earnings as er

HEADER = """<HTML><HEAD><TITLE>SEC EDGAR Submission 0001193125-16-556520</TITLE>
<!--
<SEC-HEADER>0001193125-16-556520.hdr.sgml : 20160426
<ACCEPTANCE-DATETIME>20160426163109
<ACCESSION-NUMBER>0001193125-16-556520
<TYPE>8-K
<PUBLIC-DOCUMENT-COUNT>4
<PERIOD>20160426
<ITEMS>2.02
<ITEMS>9.01
<FILING-DATE>20160426
<FILER>
<COMPANY-DATA>
<CONFORMED-NAME>PARENT CO
<CIK>0000111111
<ASSIGNED-SIC>6770
</COMPANY-DATA>
<FILING-VALUES>
<FORM-TYPE>8-K
</FILING-VALUES>
</FILER>
<FILER>
<COMPANY-DATA>
<CONFORMED-NAME>APPLE INC
<CIK>0000320193
<ASSIGNED-SIC>3571
</COMPANY-DATA>
</FILER>
</SEC-HEADER>
-->
</HEAD><BODY>
<PRE>&lt;SEC-HEADER&gt;0001193125-16-556520.hdr.sgml : 20160426
&lt;ACCEPTANCE-DATETIME&gt;20160426163109
		STANDARD INDUSTRIAL CLASSIFICATION:	BLANK CHECKS [6770]
		STANDARD INDUSTRIAL CLASSIFICATION:	ELECTRONIC COMPUTERS [3571]
&lt;/SEC-HEADER&gt;
</PRE></BODY></HTML>
"""


@pytest.fixture(scope="module")
def calendar():
    return er.XnasCloses("2012-01-03", "2026-12-31")


# ------------------------------------------------------------------ header parsing

def test_header_gives_eastern_acceptance_items_and_the_company_block_sic():
    parsed = er.parse_header(HEADER, 320193)
    assert parsed["acceptance_header_et"] == "2016-04-26 16:31:09"
    assert parsed["header_form"] == "8-K" and parsed["header_items"] == "2.02,9.01"
    assert parsed["header_period"] == "2016-04-26" and parsed["header_filing_date"] == "2016-04-26"
    # the second filer block is the company; the first (a co-registrant) is not used
    assert (parsed["header_sic"], parsed["sic_match"], parsed["n_filers"]) == ("3571", "cik", 2)
    assert parsed["header_sic_description"] == "ELECTRONIC COMPUTERS" and parsed["header_name"] == "APPLE INC"


def test_header_without_the_company_block_uses_the_first_filer_and_says_so():
    parsed = er.parse_header(HEADER, 999)
    assert (parsed["header_sic"], parsed["sic_match"]) == ("6770", "first_filer")


def test_escaped_pre_copy_alone_is_parsed():
    escaped = HEADER.split("-->")[1]
    escaped = escaped.replace("</PRE>", "&lt;FILER&gt;\n&lt;COMPANY-DATA&gt;\n&lt;CIK&gt;0000320193\n"
                                        "&lt;ASSIGNED-SIC&gt;3571\n&lt;/COMPANY-DATA&gt;\n&lt;/FILER&gt;\n</PRE>")
    escaped = escaped.replace("&lt;/SEC-HEADER&gt;\n&lt;FILER", "&lt;FILER")
    parsed = er.parse_header(escaped, 320193)
    assert parsed["acceptance_header_et"] == "2016-04-26 16:31:09"


def test_empty_header_gives_blank_fields():
    parsed = er.parse_header("", 1)
    assert parsed["acceptance_header_et"] == "" and parsed["sic_match"] == "none"


def test_header_url_and_cache_path():
    url = er.header_url(320193, "0001193125-16-556520")
    assert url == ("https://www.sec.gov/Archives/edgar/data/320193/000119312516556520/"
                   "0001193125-16-556520-index-headers.html")
    assert er.header_path(320193, "0001193125-16-556520").parts[-2:] == (
        "320193", "0001193125-16-556520-index-headers.html.gz")


# ------------------------------------------------------------------ times and D0

@pytest.mark.parametrize("stamp, session, timing", [
    ("2016-04-26 15:59:59", "2016-04-26", "intraday"),
    ("2016-04-26 16:00:00", "2016-04-27", "after_close"),   # at the close is not before it
    ("2016-04-26 16:31:09", "2016-04-27", "after_close"),
    ("2016-04-26 07:00:00", "2016-04-26", "pre_open"),
    ("2016-04-30 10:00:00", "2016-05-02", "non_session"),   # Saturday
    ("2012-11-23 12:59:00", "2012-11-23", "intraday"),      # early close at 13:00
    ("2012-11-23 13:05:00", "2012-11-26", "after_close"),
    ("2012-10-29 08:00:00", "2012-10-31", "non_session"),   # Hurricane Sandy closure
    ("2012-04-05 17:00:00", "2012-04-09", "after_close"),   # Good Friday holiday
])
def test_d0_is_the_first_session_closing_after_the_acceptance(calendar, stamp, session, timing):
    assert calendar.d0(stamp) == (session, timing)


def test_json_label_tells_utc_from_eastern_labelled_z():
    assert er.json_label("2016-04-26T20:31:09.000Z", "2016-04-26 16:31:09") == "utc"
    assert er.json_label("2016-04-26T16:31:09.000Z", "2016-04-26 16:31:09") == "et_labelled_z"
    assert er.json_label("2016-01-26T21:31:06.000Z", "2016-01-26 16:31:06") == "utc"  # EST, five hours
    assert er.json_label("2016-04-26T18:00:00.000Z", "2016-04-26 16:31:09") == "other"
    assert er.json_label("2016-04-26T18:00:00.000Z", "") == "no_header"


def test_header_wins_and_the_json_is_read_by_the_month_rule_without_one():
    assert er.resolve_acceptance("2016-04-26T20:31:09.000Z", "2016-04-26 16:31:09") == ("2016-04-26 16:31:09", "header")
    assert er.resolve_acceptance("2016-04-26T20:31:09.000Z", "", "utc") == ("2016-04-26 16:31:09", "json_utc")
    assert er.resolve_acceptance("2016-04-26T16:31:09.000Z", "", "et_labelled_z") == ("2016-04-26 16:31:09", "json_et_rule")


def test_label_rule_is_the_majority_label_by_agent_year_and_by_month():
    agent, own = "0001157523-14-000001", "0000002488-14-000001"
    rows = [{"json_label": "utc", "filing_date": "2014-02-03", "accession": own},
            {"json_label": "utc", "filing_date": "2014-02-10", "accession": own},
            {"json_label": "et_labelled_z", "filing_date": "2014-02-11", "accession": agent},
            {"json_label": "other", "filing_date": "2014-02-12", "accession": agent}]
    rules = er.label_rules(rows)
    assert rules == {"0000002488|2014": "utc", "0001157523|2014": "et_labelled_z", "2014-02": "utc"}
    # a row without a header is read by its agent's rule first, then by the month's
    calendar = er.XnasCloses("2014-01-02", "2014-12-31")
    pending = [{"json_label": "no_header", "filing_date": "2014-02-20", "accession": "0001157523-14-000009",
                "acceptance_json_raw": "2014-02-20T07:30:00.000Z", "acceptance_header_et": ""},
               {"json_label": "no_header", "filing_date": "2014-02-20", "accession": "0009999999-14-000009",
                "acceptance_json_raw": "2014-02-20T21:30:00.000Z", "acceptance_header_et": ""}]
    frame = er.finish_rows(pending, calendar, rules)
    assert frame["acceptance_et"].tolist() == ["2014-02-20 07:30:00", "2014-02-20 16:30:00"]
    assert frame["tz_resolution"].tolist() == ["json_et_rule", "json_utc"]
    assert frame["d0_session"].tolist() == ["2014-02-20", "2014-02-21"]


# ------------------------------------------------------------------ events, quarters, fallback

def _table(rows):
    frame = pd.DataFrame(rows, columns=["accessionNumber", "filingDate", "reportDate", "form", "items"])
    frame["acceptanceDateTime"] = frame["filingDate"] + "T21:00:00.000Z"
    frame["primaryDocument"] = ""
    frame["primaryDocDescription"] = ""
    return frame


def test_item202_needs_the_item_in_the_list_an_8k_form_and_the_window():
    table = _table([
        ("a1", "2011-09-30", "2011-09-30", "8-K", "2.02,9.01"),   # before the window
        ("a2", "2011-10-03", "2011-10-03", "8-K", "2.02,9.01"),
        ("a3", "2012-01-10", "2012-01-10", "8-K/A", "2.02"),
        ("a4", "2012-02-10", "2012-02-10", "8-K", "5.02,9.01"),   # no 2.02
        ("a5", "2012-02-11", "2012-02-11", "6-K", "2.02"),        # not an 8-K
        ("a6", "2012-02-12", "2012-02-12", "8-K", "12.02"),       # not item 2.02
    ])
    assert er.item202_filings(table)["accessionNumber"].tolist() == ["a2", "a3"]


def test_quarter_is_the_latest_period_end_before_the_filing():
    ends = ["2011-12-31", "2012-03-31", "2012-06-30"]
    assert er.assign_quarter("2012-01-25", ends) == ("2011-12-31", "report_date")
    assert er.assign_quarter("2012-03-31", ends) == ("2011-12-31", "report_date")  # strictly before
    assert er.assign_quarter("2012-04-20", ends) == ("2012-03-31", "report_date")
    assert er.assign_quarter("2011-12-01", ends) == ("", "none")


def test_quarter_grid_extends_past_the_last_known_period_end():
    ends = ["2015-12-31"]
    assert er.assign_quarter("2016-04-28", ends) == ("2016-03-31", "extended")
    assert er.assign_quarter("2016-12-30", ends) == ("", "none")  # too far from any known end


def test_first_in_fiscal_quarter_marks_the_earliest_event_of_each_company_quarter():
    events = pd.DataFrame({
        "cik": [1, 1, 1, 2],
        "fiscal_quarter_end": ["2012-09-30", "2012-09-30", "2012-12-31", "2012-09-30"],
        "acceptance_sort": ["2012-10-25 16:05:00", "2012-10-11 08:00:00", "2013-01-24 16:01:00", "2012-10-30 07:00:00"],
        "accession": ["x2", "x1", "x3", "y1"]})
    assert er.mark_first_in_quarter(events).tolist() == ["N", "Y", "Y", "Y"]


def test_fallback_is_the_first_original_periodic_filing_of_a_quarter_without_an_event():
    table = _table([
        ("q1", "2012-05-08", "2012-03-31", "10-Q", ""),
        ("q1a", "2012-06-01", "2012-03-31", "10-Q/A", ""),         # amendment: never a fallback
        ("q2", "2012-08-07", "2012-06-30", "10-Q", ""),
        ("k1", "2013-03-01", "2012-12-31", "10-K", ""),
        ("old", "2011-08-05", "2011-06-30", "10-Q", ""),          # filed before the window
    ])
    table = pd.concat([table, _table([("r1", "2013-02-05", "2013-02-05", "8-K", "8.01,9.01"),
                                      ("r0", "2012-12-31", "2012-12-31", "8-K", "7.01")])], ignore_index=True)
    fallback = er.fallback_filings(table, {"2012-06-30"})
    assert fallback["accessionNumber"].tolist() == ["q1", "k1"]
    # an 8.01 8-K after the period end and before the 10-K is flagged; one on the period end is not
    assert fallback["other_8k_between"].tolist() == ["", "2013-02-05:8.01,9.01"]


def test_period_ends_include_amended_reports():
    table = _table([("q1a", "2012-06-01", "2012-03-31", "10-Q/A", ""), ("k1", "2013-03-01", "2012-12-31", "10-K", ""),
                    ("e1", "2013-02-01", "2013-02-01", "8-K", "2.02")])
    assert er.period_ends(table) == ["2012-03-31", "2012-12-31"]


def test_pages_needed_are_those_reaching_into_the_window():
    payload = {"filings": {"files": [
        {"name": "CIK0000000001-submissions-001.json", "filingFrom": "2011-11-01", "filingTo": "2014-05-01"},
        {"name": "CIK0000000001-submissions-002.json", "filingFrom": "2005-01-01", "filingTo": "2011-09-30"}]}}
    assert er.pages_needed(payload) == ["CIK0000000001-submissions-001.json"]


def test_filing_table_joins_recent_and_pages_once_per_accession():
    recent = {"accessionNumber": ["a", "b"], "filingDate": ["2020-01-02", "2019-01-02"], "form": ["8-K", "10-Q"],
              "items": ["2.02", ""], "reportDate": ["2020-01-02", "2018-12-31"],
              "acceptanceDateTime": ["", ""], "primaryDocument": ["", ""], "primaryDocDescription": ["", ""]}
    page = {k: [v[-1]] for k, v in recent.items()}
    table = er.filing_table({"filings": {"recent": recent}}, [page])
    assert table["accessionNumber"].tolist() == ["a", "b"]


# ------------------------------------------------------------------ scope, header files, SIC

def test_scope_is_top300_or_candidates_and_drops_foreign_filers():
    weekly = pd.DataFrame({
        "security_id": ["10", "10", "20", "30", "40"], "cik": pd.array([10, 10, 20, 30, 40], dtype="Int64"),
        "week_end": pd.to_datetime(["2012-01-06", "2012-01-13", "2012-01-06", "2012-01-06", "2012-01-06"]),
        "universe": [True, True, True, True, True],
        "dv50_rank": [250.0, 320.0, 301.0, float("nan"), 5.0], "dv20_rank": [float("nan"), 290.0, 302.0, 10.0, 5.0]})
    candidates = pd.DataFrame({"security_id": ["20", "50"], "cik": ["20", "50"]})
    master = pd.DataFrame({"security_id": ["10", "20", "30", "40", "50"], "cik": ["10", "20", "30", "40", "50"],
                           "name": list("ABCDE"), "foreign_filer": ["N", "N", "MIXED", "Y", "N"]})
    scope = er.build_scope(weekly, candidates, master).set_index("cik")
    assert scope.index.tolist() == [10, 20, 30, 40, 50]
    assert scope.loc[10, "top300_weeks"] == 2 and scope.loc[10, "in_candidates"] == "N"
    assert scope.loc[20, "in_top300"] == "N" and scope.loc[20, "in_candidates"] == "Y"
    assert scope.loc[30, "excluded_foreign"] == "N" and scope.loc[40, "excluded_foreign"] == "Y"
    assert scope.loc[50, "listed_first_week"] == ""


def test_older_filings_ask_for_the_sgml_header_first():
    assert er.header_kinds("2013-02-26") == ("hdr_sgml", "index_headers")
    assert er.header_kinds("2016-04-26") == ("index_headers", "hdr_sgml")
    assert er.header_url(3116, "0001157523-13-001026", "hdr_sgml").endswith(
        "/3116/000115752313001026/0001157523-13-001026.hdr.sgml")


def test_load_header_falls_back_to_the_other_file_on_a_404(tmp_path, monkeypatch):
    monkeypatch.setattr(er, "HEADER_DIR", tmp_path)
    asked = []

    def fake_get(url, path, source, symbol, offline):
        asked.append(url.rsplit("/", 1)[1])
        return None if url.endswith("-index-headers.html") else b"<SEC-HEADER>\n<ACCEPTANCE-DATETIME>20160426163109\n"

    monkeypatch.setattr(er, "_sec_get", fake_get)
    text, kind = er.load_header(1, "0000000001-16-000001", filing_date="2016-04-26")
    assert kind == "hdr_sgml" and "20160426163109" in text
    assert asked == ["0000000001-16-000001-index-headers.html", "0000000001-16-000001.hdr.sgml"]


def test_sic_history_flags_changes_and_blank_checks():
    events = pd.DataFrame({
        "cik": [7, 7, 7, 7], "accession": ["a", "b", "c", "d"], "form": ["8-K"] * 4,
        "acceptance_header_et": ["2019-01-10 08:00:00", "2020-11-02 16:05:00", "2021-03-01 07:00:00", "2021-05-01 07:00:00"],
        "header_sic": ["6770", "6770", "3711", "3711"], "header_sic_description": ["BLANK CHECKS"] * 2 + ["MOTOR"] * 2,
        "sic_match": ["cik"] * 4})
    sic = er.sic_history(events, events.iloc[0:0])
    assert sic["observed_date"].tolist() == ["2019-01-10", "2020-11-02", "2021-03-01", "2021-05-01"]
    assert sic["blank_check_6770"].tolist() == ["Y", "Y", "N", "N"]
    assert sic["sic_changed"].tolist() == ["N", "N", "Y", "N"]
    assert sic["operating_sic_after_6770"].tolist() == ["3711", "3711", "", ""]
    assert list(sic.columns[:4]) == ["cik", "observed_date", "sic", "source_accession"]
    # compact: the first header of each year plus every change (2021-05-01 repeats 2021-03-01)
    assert er.compact_sic_history(sic)["source_accession"].tolist() == ["a", "b", "c"]


def test_sic_week_coverage_uses_the_latest_header_on_or_before_the_week():
    weekly = pd.DataFrame({"cik": pd.array([7, 7, 8], dtype="Int64"),
                           "week_end": pd.to_datetime(["2019-01-04", "2019-01-11", "2019-01-11"]),
                           "dv50_rank": [5.0, 5.0, 6.0], "dv20_rank": [5.0, 5.0, 6.0]})
    sic = pd.DataFrame({"cik": [7], "observed_date": ["2019-01-10"], "sic": ["9995"]})
    facts = er.sic_week_coverage(weekly, sic, {"3711": "Autos"}, {7, 8})
    assert facts["sic_status"] == {"earliest_after": 1, "on_or_before": 1, "none": 1}
    assert facts["no_ff49_range_codes"] == {"9995": 2}


def test_quarter_coverage_counts_events_by_d0_calendar_quarter():
    present = pd.DataFrame({"cik": [7, 7], "quarter": ["2012Q1", "2012Q2"], "listed_weeks": [13, 13],
                            "top_weeks": [13, 0], "quarter_weeks": [13, 13]})
    events = pd.DataFrame({"cik": [7, 7], "d0_session": ["2012-01-26", "2012-03-30"],
                           "first_in_fiscal_quarter": ["Y", "N"]})
    fallback = pd.DataFrame({"cik": [7], "d0_session": ["2012-05-08"]})
    q = er.quarter_coverage(present, events, fallback).set_index("quarter")
    assert q.loc["2012Q1", ["n_item202", "n_item202_first", "n_fallback"]].tolist() == [2, 1, 0]
    assert q.loc["2012Q2", "n_fallback"] == 1
    summary = er.coverage_summary(q.reset_index())
    assert summary["company_quarters"] == 1 and summary["share_any"] == 1.0  # Q2 is not a top-300 quarter



def test_company_year_counts_quarterly_events_and_flags_full_years():
    q = pd.DataFrame({"cik": [7, 7, 7, 7, 8], "quarter": ["2012Q1", "2012Q2", "2012Q3", "2012Q4", "2012Q1"],
                      "listed_weeks": [13, 13, 13, 13, 5], "top_weeks": [1, 0, 0, 0, 5], "quarter_weeks": [13] * 5,
                      "n_item202": [1, 1, 2, 1, 0], "n_item202_first": [1, 1, 1, 1, 0], "n_fallback": [0, 0, 0, 0, 1]})
    years = er.company_year_table(q).set_index("cik")
    assert years.loc[7, "n_quarterly_events"] == 4 and years.loc[7, "full_year"] == "Y"
    assert years.loc[8, "n_quarterly_events"] == 1 and years.loc[8, "full_year"] == "N"
    summary = er.company_year_summary(years.reset_index())
    assert summary["events_per_full_company_year"] == {"4": 1}
    assert summary["zero_item202_company_years"] == 1 and summary["zero_any_company_years"] == 0

# ------------------------------------------------------------------ the built outputs

def _built(path):
    if not path.exists():
        pytest.skip(f"{path} not built")
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def test_built_events_are_item202_8ks_with_header_times_and_xnas_d0():
    events = _built(er.EVENTS_OUT)
    assert list(events.columns[:12]) == er.EVENT_COLUMNS[:12]
    assert set(events["form"]) <= er.EVENT_FORMS
    assert events["items"].map(er.has_item).all()
    assert (events["filing_date"] >= er.EVENTS_FROM).all()
    assert not events.duplicated(["cik", "accession"]).any()
    assert set(events["first_in_fiscal_quarter"]) <= {"Y", "N"}
    assert set(events["tz_resolution"]) <= {"header", "json_utc", "json_et_rule"}
    assert (events["tz_resolution"] == "header").mean() >= 0.99
    sessions = set(er.XnasCloses().sessions.strftime("%Y-%m-%d"))
    dated = events[events["d0_session"] != ""]
    assert dated["d0_session"].isin(sessions).all()
    assert (dated["d0_session"] >= dated["acceptance_et"].str[:10]).all()
    # D0 is the acceptance day exactly when the acceptance is before that session's close
    same_day = dated["d0_session"] == dated["acceptance_et"].str[:10]
    assert dated.loc[same_day, "acceptance_timing"].isin(["pre_open", "intraday"]).all()


def test_built_scope_has_no_foreign_filer_and_fallback_rows_are_periodic():
    events, fallback = _built(er.EVENTS_OUT), _built(er.FALLBACK_OUT)
    master = pd.read_csv(er.MASTER, dtype=str, keep_default_na=False)
    foreign = set(master.loc[master["foreign_filer"] == "Y", "cik"])
    assert not (set(events["cik"]) | set(fallback["cik"])) & foreign
    assert set(fallback["form"]) <= er.PERIODIC_FORMS
    assert not fallback.duplicated(["cik", "report_date"]).any()
    # a fallback quarter never also has an Item 2.02 event assigned to it
    quarters = set(zip(events["cik"], events["fiscal_quarter_end"]))
    assert not any((c, q) in quarters for c, q in zip(fallback["cik"], fallback["report_date"]))


def test_built_sic_history_has_four_digit_codes_from_headers():
    sic = _built(er.SIC_OUT)
    assert list(sic.columns) == er.SIC_COLUMNS
    assert sic["sic"].str.fullmatch(r"\d{4}").all()
    assert not sic.duplicated(["cik", "source_accession"]).any()
    assert set(sic["blank_check_6770"]) <= {"Y", "N"}
