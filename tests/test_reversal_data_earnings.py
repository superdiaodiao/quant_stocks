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


def test_label_rule_is_the_majority_label_by_company_then_agent_year_then_month():
    agent, own = "0001157523-14-000001", "0000002488-14-000001"
    rows = [{"json_label": "utc", "filing_date": "2014-02-03", "accession": own, "cik": 2488},
            {"json_label": "utc", "filing_date": "2014-02-10", "accession": own, "cik": 2488},
            {"json_label": "et_labelled_z", "filing_date": "2014-02-11", "accession": agent, "cik": 7},
            {"json_label": "other", "filing_date": "2014-02-12", "accession": agent, "cik": 7}]
    rules = er.label_rules(rows)
    assert rules == {"cik|2488": "utc", "cik|7": "et_labelled_z", "0000002488|2014": "utc",
                     "0001157523|2014": "et_labelled_z", "2014-02": "utc"}
    # a row without a header is read by its company's rule, then its agent's, then the month's
    calendar = er.XnasCloses("2014-01-02", "2014-12-31")
    pending = [{"json_label": "no_header", "filing_date": "2014-02-20", "accession": "0001157523-14-000009", "cik": 2488,
                "acceptance_json_raw": "2014-02-20T21:30:00.000Z", "acceptance_header_et": ""},
               {"json_label": "no_header", "filing_date": "2014-02-20", "accession": "0001157523-14-000010", "cik": 99,
                "acceptance_json_raw": "2014-02-20T07:30:00.000Z", "acceptance_header_et": ""},
               {"json_label": "no_header", "filing_date": "2014-02-20", "accession": "0009999999-14-000009", "cik": 98,
                "acceptance_json_raw": "2014-02-20T21:30:00.000Z", "acceptance_header_et": ""}]
    frame = er.finish_rows(pending, calendar, rules)
    assert frame["acceptance_et"].tolist() == ["2014-02-20 16:30:00", "2014-02-20 07:30:00", "2014-02-20 16:30:00"]
    assert frame["tz_resolution"].tolist() == ["json_utc", "json_et_rule", "json_utc"]
    assert frame["d0_session"].tolist() == ["2014-02-21", "2014-02-20", "2014-02-21"]


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


@pytest.mark.parametrize("dates, report, expected", [
    (["2012-10-18"], "2012-11-01", (0, "single")),
    # AMD 2012-Q3: a preannouncement a week before the results release
    (["2012-10-11", "2012-10-18"], "2012-11-01", (1, "last_run_by_report")),
    # Casey's: the release, then a second 8-K the next day (a run: its first is the release)
    (["2019-06-10", "2019-06-11"], "2019-06-28", (0, "last_run_by_report")),
    # a later event after the report is not the release; one a day after the report may be
    (["2019-03-11", "2019-03-12", "2019-04-20"], "2019-03-11", (0, "last_run_by_report")),
    (["2019-01-02", "2019-01-18", "2019-01-30"], "2019-02-19", (2, "last_run_by_report")),
    (["2019-05-01", "2019-05-20"], "2019-04-30", (0, "last_run_by_report")),      # a day after: allowed
    (["2019-05-01", "2019-05-20"], "2019-04-29", (0, "first_all_after_report")),
    (["2019-05-01", "2019-05-20"], "", (0, "first_no_report")),
])
def test_results_release_is_the_first_of_the_last_run_by_the_report(dates, report, expected):
    assert er.classify_quarter(dates, report) == expected


def _events(rows):
    return pd.DataFrame(rows, columns=["cik", "accession", "form", "filing_date", "acceptance_sort",
                                       "fiscal_quarter_end", "periodic_report_filing_date", "amends_how"])


def test_event_kinds_keep_every_row_and_give_one_release_per_quarter():
    events = _events([
        (1, "a2", "8-K", "2012-10-18", "2012-10-18 16:26:56", "2012-09-29", "2012-11-01", ""),
        (1, "a1", "8-K", "2012-10-11", "2012-10-11 16:50:58", "2012-09-29", "2012-11-01", ""),
        (1, "a3", "8-K/A", "2012-10-19", "2012-10-19 09:00:00", "2012-09-29", "2012-11-01", "report_date"),
        (1, "a4", "8-K", "2012-11-20", "2012-11-20 09:00:00", "2012-09-29", "2012-11-01", ""),
        (1, "b1", "8-K", "2013-01-22", "2013-01-22 16:46:05", "2012-12-29", "2013-02-20", ""),
        (2, "c1", "8-K", "2014-05-07", "2014-05-07 08:00:00", "", "", ""),
        (2, "c2", "8-K/A", "2014-06-07", "2014-06-07 08:00:00", "2014-03-31", "2014-05-09", "none"),
    ])
    kinds = er.classify_events(events)
    assert len(kinds) == len(events) and kinds.index.equals(events.index)
    assert kinds["event_kind"].tolist() == ["results_release", "preannouncement", "amendment", "other",
                                            "results_release", "other", "results_release"]
    assert kinds["event_kind_basis"].tolist()[:2] == ["last_run_by_report", "last_run_by_report"]
    assert kinds.loc[[4, 5, 6], "event_kind_basis"].tolist() == ["single", "no_fiscal_quarter", "single"]
    assert kinds.loc[[1, 0, 2, 3], "n_item202_in_fiscal_quarter"].tolist() == ["4"] * 4
    # days to the next Item 2.02 filing of the same quarter, in acceptance order (a1, a2, a3, a4)
    assert kinds.loc[[1, 0, 2, 3], "days_to_next_item202_in_quarter"].tolist() == ["7", "1", "32", ""]


def test_amendments_link_by_period_of_report_else_the_nearest_item202_8k():
    table = _table([
        ("o1", "2016-04-26", "2016-04-26", "8-K", "2.02,9.01"),
        ("o2", "2016-04-28", "2016-04-28", "8-K", "5.07"),
        ("x1", "2016-04-27", "2016-04-26", "8-K/A", "2.02,9.01"),     # same period of report as o1
        ("x2", "2016-04-29", "2016-04-29", "8-K/A", "2.02"),          # no same-period 8-K: o1, 3 days back
        ("x3", "2016-06-30", "2016-06-30", "8-K/A", "2.02"),          # nothing within 7 days
        ("o3", "2016-07-01", "2016-07-01", "8-K", "2.01,9.01"),
        ("x4", "2016-07-15", "2016-07-01", "8-K/A", "2.02,9.01"),     # adds 2.02 to an acquisition 8-K
    ])
    events = er.item202_filings(table)
    links = er.link_amendments(events, table).set_index(events["accessionNumber"])
    assert links.loc["o1"].tolist() == ["", ""]
    assert links.loc["x1"].tolist() == ["o1", "report_date"]
    assert links.loc["x2"].tolist() == ["o1", "nearest_item202"]
    assert links.loc["x3"].tolist() == ["", "none"]
    assert links.loc["x4"].tolist() == ["o3", "report_date_non202"]


def test_an_unlinked_8ka_is_never_chosen_over_an_original_8k():
    events = _events([
        (1, "r1", "8-K", "2016-05-02", "2016-05-02 16:05:00", "2016-03-31", "2016-05-06", ""),
        (1, "x1", "8-K/A", "2016-05-05", "2016-05-05 09:00:00", "2016-03-31", "2016-05-06", "report_date_non202"),
    ])
    assert er.classify_events(events)["event_kind"].tolist() == ["results_release", "other"]
    # alone in its quarter it is the release
    alone = er.classify_events(events.iloc[[1]])
    assert alone["event_kind"].tolist() == ["results_release"]


def test_an_8ka_far_from_its_8k_links_within_the_fiscal_quarter():
    table = _table([("o1", "2023-10-25", "2023-10-25", "8-K", "2.01,2.02,9.01"),
                    ("x1", "2023-11-15", "2023-11-15", "8-K/A", "2.02,9.01")])
    events = er.item202_filings(table).assign(fiscal_quarter_end="2023-09-30")
    links = er.link_amendments(events, table)
    assert links["amends_accession"].tolist() == ["", "o1"]
    assert links["amends_how"].tolist() == ["", "same_quarter_item202"]


def test_prior_quarter_report_pending_flags_a_late_filers_release():
    ends = ["2017-12-31", "2018-03-31", "2018-06-30"]
    reports = pd.DataFrame({"fiscal_quarter_end": ends, "periodic_report_filing_date": ["2018-04-02", "2018-05-10",
                                                                                      "2018-08-09"]})
    events = pd.DataFrame({"filingDate": ["2018-04-02", "2018-05-01", "2018-07-30"],
                           "fiscal_quarter_end": ["2018-03-31", "2018-03-31", "2018-06-30"],
                           "fiscal_quarter_how": ["report_date"] * 3})
    # the 2018-04-02 release came with the late 10-K for 2017-12-31, a quarter without an event
    assert er.prior_quarter_pending(events, ends, reports) == ["Y", "N", "N"]
    covered = pd.concat([events, pd.DataFrame({"filingDate": ["2018-02-01"], "fiscal_quarter_end": ["2017-12-31"],
                                               "fiscal_quarter_how": ["report_date"]})], ignore_index=True)
    assert er.prior_quarter_pending(covered, ends, reports) == ["N", "N", "N", "N"]


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
    assert fallback["days_after_period_end"].tolist() == [38, 60]
    assert fallback["past_due"].tolist() == [False, False]


def test_fallback_flags_late_reports_shared_days_and_item202_between():
    table = _table([
        ("q1", "2014-09-12", "2013-03-31", "10-Q", ""),            # 530 days late, filed with q2
        ("q2", "2014-09-12", "2013-06-30", "10-Q", ""),
        ("q3", "2014-11-24", "2014-09-30", "10-Q", ""),            # 55 days: past the 53-day limit
        ("k1", "2015-04-17", "2014-12-31", "10-K", ""),            # 107 days: inside the 108-day limit
        ("q4", "2015-07-01", "2015-03-31", "10-Q", ""),
        ("e1", "2015-04-20", "2015-04-20", "8-K", "2.02,9.01"),    # between 2015-03-31 and q4, assigned later
    ])
    fallback = er.fallback_filings(table, set())
    assert fallback["accessionNumber"].tolist() == ["q1", "q2", "q3", "k1", "q4"]
    assert fallback["past_due"].tolist() == [True, True, True, False, True]
    assert fallback["item202_between"].tolist() == ["", "", "", "", "2015-04-20:e1"]
    rows = fallback.rename(columns={"accessionNumber": "accession", "filingDate": "filing_date"})
    rows = rows.assign(cik=5, d0_session=["2014-09-15", "2014-09-15", "2014-11-25", "2015-04-20", "2015-07-02"])
    marked = er.mark_fallback(rows)
    assert marked["catch_up_reason"].tolist() == ["past_due;shared_d0", "past_due;shared_d0", "past_due", "", "past_due"]
    assert marked["catch_up_filing"].tolist() == ["Y", "Y", "Y", "N", "Y"]
    assert marked["usable_as_announcement"].tolist() == ["N", "N", "N", "Y", "N"]
    assert set(marked["event_kind"]) == {"periodic_report"}


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

def _top(rows):
    """Rows of the weekly top-300 file: (security_id, cik, week_end)."""
    return pd.DataFrame({"security_id": [r[0] for r in rows], "cik": pd.array([r[1] for r in rows], dtype="Int64"),
                         "week_end": pd.to_datetime([r[2] for r in rows])})


def _listed(rows, universe=None):
    """Step-6 rows: (security_id, cik, week_end), all listed unless ``universe`` says otherwise."""
    frame = _top(rows)
    return frame.assign(universe=universe if universe is not None else [True] * len(frame))


def test_scope_is_the_top300_file_or_candidates_and_drops_foreign_filers():
    top = _top([("10", 10, "2012-01-06"), ("10", 10, "2012-01-13"), ("30", 30, "2012-01-06"), ("40", 40, "2012-01-06")])
    weekly = _listed([("10", 10, "2012-01-06"), ("10", 10, "2012-01-13"), ("20", 20, "2012-01-06"),
                      ("30", 30, "2012-01-06"), ("40", 40, "2012-01-06")])
    candidates = pd.DataFrame({"security_id": ["20", "50"], "cik": ["20", "50"],
                               "needed_start": ["2018-01-21", "2023-05-01"], "needed_end": ["2018-05-07", "2026-08-31"]})
    master = pd.DataFrame({"security_id": ["10", "20", "30", "40", "50"], "cik": ["10", "20", "30", "40", "50"],
                           "name": list("ABCDE"), "foreign_filer": ["N", "N", "MIXED", "Y", "N"]})
    scope = er.build_scope(top, weekly, candidates, master).set_index("cik")
    # CIK 40 (Y) is foreign in every week, so its top-300 week does not bring it in
    assert scope.index.tolist() == [10, 20, 30, 50]
    assert scope.loc[10, "top300_weeks"] == 2 and scope.loc[10, "in_candidates"] == "N"
    assert scope.loc[20, "in_top300"] == "N" and scope.loc[20, "in_candidates"] == "Y"
    assert scope.loc[30, "excluded_foreign"] == "N"
    assert scope.loc[50, "listed_first_week"] == ""
    # windows: the Monday of the first top-300 week (or needed_start) less 120 days, never before
    # 2011-10-01, to the last top-300 week (or needed_end) plus 35 days, at least to its quarter's end
    assert scope.loc[10, ["window_start", "window_end"]].tolist() == ["2011-10-01", "2012-03-31"]
    assert scope.loc[20, ["window_start", "window_end"]].tolist() == ["2017-09-23", "2018-06-30"]
    assert scope.loc[20, ["candidate_first_day", "candidate_last_day"]].tolist() == ["2018-01-21", "2018-05-07"]
    # named by a candidate row it is listed, and excluded
    named = er.build_scope(top, weekly, pd.concat([candidates, pd.DataFrame({"security_id": ["40"], "cik": ["40"]})]),
                           master).set_index("cik")
    assert named.loc[40, ["excluded_foreign", "in_top300", "top300_weeks_foreign"]].tolist() == ["Y", "N", 1]
    # a candidate without a needed window keeps the whole span from 2011-10-01
    assert named.loc[40, ["window_start", "window_end"]].tolist() == ["2011-10-01", er.WINDOW_OPEN_END]


def test_one_window_spans_both_the_top300_weeks_and_the_candidate_window():
    top = _top([("7", 7, "2014-06-06"), ("7", 7, "2015-01-09")])
    candidates = pd.DataFrame({"security_id": ["7.B"], "cik": ["7"], "needed_start": ["2020-03-01"],
                               "needed_end": ["2021-02-28"]})
    master = pd.DataFrame({"security_id": ["7", "7.B"], "cik": ["7", "7"], "name": ["S", "S"], "foreign_filer": ["N", "N"]})
    scope = er.build_scope(top, _listed([("7", 7, "2014-06-06")]), candidates, master).iloc[0]
    # 2021-02-28 + 35 days is past the quarter's end
    assert (scope["window_start"], scope["window_end"]) == ("2014-01-31", "2021-04-04")
    assert scope["security_ids"] == "7 7.B"


def test_scope_weeks_are_the_top300_weeks_and_listed_weeks_inside_a_candidate_window():
    top = _top([("1", 1, "2015-01-09")])
    weekly = _listed([("1", 1, "2015-01-16"), ("2", 2, "2015-01-09"), ("2", 2, "2015-01-16"), ("2", 2, "2015-01-23"),
                      ("2", 2, "2015-01-30"), ("3", 3, "2015-01-09")], universe=[True, True, True, False, True, True])
    windows = er.candidate_windows(pd.DataFrame({"security_id": ["2", "3"], "cik": ["2", "3"],
                                                 "needed_start": ["2015-01-12", "2015-01-12"],
                                                 "needed_end": ["2015-01-30", "2015-01-30"]}))
    flags = pd.Series({1: "N", 2: "N", 3: "Y"})
    weeks = er.scope_weeks(top, weekly, windows, flags)
    # CIK 1: its top-300 week only (2015-01-16 is listed but in no window); CIK 2: listed weeks in the
    # window (2015-01-09 is before it, 2015-01-23 not listed); CIK 3 is foreign
    assert list(zip(weeks["cik"], weeks["week_end"].dt.strftime("%Y-%m-%d"))) == [
        (1, "2015-01-09"), (2, "2015-01-16"), (2, "2015-01-30")]


def test_presence_counts_listed_and_in_scope_weeks_per_quarter():
    weekly = _listed([("1", 1, d) for d in ("2015-01-09", "2015-02-06", "2015-03-06")] + [("2", 2, "2015-01-09")])
    weeks = pd.DataFrame({"cik": [1, 1], "week_end": pd.to_datetime(["2015-02-06", "2015-04-03"])})
    present = er.presence(weeks, weekly, {1}).set_index("quarter")
    # 2015Q2 holds a scope week the step-6 table does not list: kept, with no listed week
    assert present.loc["2015Q1", ["listed_weeks", "scope_weeks", "quarter_weeks"]].tolist() == [3, 1, 3]
    assert present.loc["2015Q2", ["listed_weeks", "scope_weeks", "quarter_weeks"]].tolist() == [0, 1, 0]


def _history(rows):
    frame = pd.DataFrame(rows, columns=["cik", "filing_date", "form", "regime_in_force"])
    return frame.assign(accession="", form_regime=frame["regime_in_force"], sets_regime=frame["regime_in_force"])


def test_foreign_regime_comes_from_the_flag_and_the_mixed_history():
    flags = pd.Series({1: "N", 2: "Y", 3: "MIXED", 4: "MIXED"})
    # CIK 3 files 20-Fs until its first 10-Q on 2022-11-04 (Atlassian's pattern); CIK 4 is not in the table
    history = _history([("3", "2021-08-19", "20-F", "F"), ("3", "2022-08-19", "20-F", "F"),
                        ("3", "2022-11-04", "10-Q", "D")])
    ciks = [1, 2, 3, 3, 3, 3, 4]
    days = ["2020-01-03", "2020-01-03", "2020-01-03", "2022-11-03", "2022-11-04", "2023-06-02", "2020-01-03"]
    assert er.regime_foreign(flags, history, ciks, days).tolist() == [False, True, True, True, False, False, False]
    # without the table a MIXED CIK counts as domestic (and Y stays foreign)
    assert er.regime_foreign(flags, None, ciks, days).tolist() == [False, True] + [False] * 5
    weeks = pd.to_datetime(pd.Series(days))
    assert er.regime_foreign(flags, history, ciks, weeks).tolist()[2:6] == [True, True, False, False]


def test_scope_counts_only_domestic_top300_weeks_of_a_mixed_filer():
    top = _top([("3", 3, "2022-10-28"), ("3", 3, "2022-11-04"), ("3", 3, "2022-11-11"), ("6", 6, "2020-01-03")])
    weekly = _listed([("3", 3, "2022-10-28"), ("3", 3, "2022-11-04"), ("3", 3, "2022-11-11"), ("6", 6, "2020-01-03"),
                      ("6", 6, "2022-11-11")])
    candidates = pd.DataFrame({"security_id": [], "cik": []})
    master = pd.DataFrame({"security_id": ["3", "6"], "cik": ["3", "6"], "name": ["T", "G"],
                           "foreign_filer": ["MIXED", "MIXED"]})
    history = _history([("3", "2022-08-19", "20-F", "F"), ("3", "2022-11-04", "10-Q", "D"),
                        ("6", "2019-03-01", "20-F", "F"), ("6", "2022-05-01", "10-Q", "D")])
    scope = er.build_scope(top, weekly, candidates, master, history).set_index("cik")
    assert scope.loc[3, ["top300_weeks", "top300_weeks_foreign", "top300_first_week", "listed_first_week"]].tolist() == [
        2, 1, "2022-11-04", "2022-11-04"]
    # the window starts from the first domestic top-300 week
    assert scope.loc[3, "window_start"] == "2022-07-01"
    # CIK 6's only top-300 week is foreign: out of scope, and reported as such
    assert 6 not in scope.index
    assert er.foreign_only_top300(top, scope.reset_index()) == [6]
    # without the history table every week counts
    assert er.build_scope(top, weekly, candidates, master).set_index("cik").loc[3, "top300_weeks"] == 3


def test_header_cache_scan_reads_the_cache_not_the_log(tmp_path, monkeypatch):
    import gzip as gz
    monkeypatch.setattr(er, "HEADER_DIR", tmp_path)
    good = "0000000001-16-000001"
    path = er.header_path(1, good)
    path.parent.mkdir(parents=True)
    path.write_bytes(gz.compress(f"<SEC-HEADER>{good}.hdr.sgml : 20160426\n<ACCEPTANCE-DATETIME>20160426163109\n"
                                 f"<ACCESSION-NUMBER>{good}\n</SEC-HEADER>\n".encode()))
    blank = "0000000001-16-000002"
    er.header_path(1, blank, "hdr_sgml").write_bytes(gz.compress(
        f"<SEC-HEADER>{blank}.hdr.sgml : 20181001\n<ACCEPTANCE-DATETIME>\n</SEC-HEADER>\n".encode()))
    er.header_path(1, "0000000001-16-000003").write_bytes(b"not gzip")
    er.header_path(1, "0000000001-16-000004").write_bytes(gz.compress(
        b"<SEC-HEADER>0000000009-16-000009.hdr.sgml : 20160426\n<ACCESSION-NUMBER>0000000009-16-000009\n"))
    jobs = [(1, f"0000000001-16-00000{i}", "2016-04-26", "item202") for i in range(1, 6)]
    scan = er.header_cache_scan(jobs)
    assert scan["by_status"] == {"ok": 1, "ok_no_acceptance_time": 1, "unreadable_gzip": 1,
                                 "accession_mismatch": 1, "missing": 1}
    assert not scan["complete"]
    assert scan["problems"]["missing"] == ["1/0000000001-16-000005"]


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
                            "scope_weeks": [13, 0], "quarter_weeks": [13, 13]})
    events = pd.DataFrame({"cik": [7, 7], "d0_session": ["2012-01-26", "2012-03-30"],
                           "event_kind": ["results_release", "other"]})
    fallback = pd.DataFrame({"cik": [7], "d0_session": ["2012-05-08"], "usable_as_announcement": ["N"]})
    q = er.quarter_coverage(present, events, fallback).set_index("quarter")
    assert q.loc["2012Q1", ["n_item202", "n_results_release", "n_fallback"]].tolist() == [2, 1, 0]
    assert q.loc["2012Q2", ["n_fallback", "n_fallback_usable"]].tolist() == [1, 0]
    summary = er.coverage_summary(q.reset_index())
    assert summary["company_quarters"] == 1 and summary["share_any"] == 1.0  # Q2 is not an in-scope quarter
    assert summary["share_usable_date"] == 1.0 and summary["by_year"]["2012"]["with_usable_date"] == 1



def test_company_year_counts_quarterly_events_and_flags_full_years():
    q = pd.DataFrame({"cik": [7, 7, 7, 7, 8], "quarter": ["2012Q1", "2012Q2", "2012Q3", "2012Q4", "2012Q1"],
                      "listed_weeks": [13, 13, 13, 13, 5], "scope_weeks": [1, 0, 0, 0, 5], "quarter_weeks": [13] * 5,
                      "n_item202": [1, 1, 2, 1, 0], "n_results_release": [1, 1, 1, 1, 0], "n_fallback": [0, 0, 0, 0, 1],
                      "n_fallback_usable": [0, 0, 0, 0, 1]})
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
    assert list(events.columns) == er.EVENT_COLUMNS
    assert set(events["form"]) <= er.EVENT_FORMS
    assert events["items"].map(er.has_item).all()
    assert (events["filing_date"] >= er.EVENTS_FROM).all()
    assert not events.duplicated(["cik", "accession"]).any()
    assert set(events["tz_resolution"]) <= {"header", "json_utc", "json_et_rule"}
    assert (events["tz_resolution"] == "header").mean() >= 0.99
    sessions = set(er.XnasCloses().sessions.strftime("%Y-%m-%d"))
    dated = events[events["d0_session"] != ""]
    assert dated["d0_session"].isin(sessions).all() and dated["d0_session_acceptance"].isin(sessions).all()
    assert (dated["d0_session_acceptance"] >= dated["acceptance_et"].str[:10]).all()
    # the acceptance D0 is the acceptance day exactly when the acceptance is before that session's close
    same_day = dated["d0_session_acceptance"] == dated["acceptance_et"].str[:10]
    assert dated.loc[same_day, "acceptance_timing"].isin(["pre_open", "intraday"]).all()
    # D0 differs from it only on late-furnished rows, and is then earlier
    late = dated["late_furnished"] == "Y"
    assert ((dated["d0_session"] != dated["d0_session_acceptance"]) == late).all()
    assert (dated.loc[late, "d0_session"] < dated.loc[late, "d0_session_acceptance"]).all()
    assert (dated.loc[late, "d0_basis"] == "release_date_latest").all() and (dated.loc[~late, "d0_basis"] == "acceptance").all()


def test_built_events_keep_every_item202_8k_with_one_release_per_quarter():
    events = _built(er.EVENTS_OUT)
    assert set(events["event_kind"]) <= er.EVENT_KINDS and (events["event_kind"] != "").all()
    assigned = events[events["fiscal_quarter_end"] != ""]
    releases = assigned[assigned["event_kind"] == "results_release"].groupby(["cik", "fiscal_quarter_end"]).size()
    assert (releases == 1).all()
    members = assigned[assigned["event_kind"] != "amendment"].groupby(["cik", "fiscal_quarter_end"]).size()
    assert set(members.index) == set(releases.index)
    assert (events.loc[events["fiscal_quarter_end"] == "", "event_kind"] == "other").all()
    # n_item202_in_fiscal_quarter counts the rows of the quarter
    sizes = assigned.groupby(["cik", "fiscal_quarter_end"])["accession"].transform("size").astype(str)
    assert (assigned["n_item202_in_fiscal_quarter"] == sizes).all()
    # a preannouncement comes before its quarter's release, an 'other' event after it
    release_at = assigned[assigned["event_kind"] == "results_release"].set_index(["cik", "fiscal_quarter_end"])["acceptance_et"]
    for kind, before in (("preannouncement", True), ("other", False)):
        rows = assigned[assigned["event_kind"] == kind]
        at = pd.Series([release_at[k] for k in zip(rows["cik"], rows["fiscal_quarter_end"])], index=rows.index)
        assert ((rows["acceptance_et"] <= at) if before else (rows["acceptance_et"] >= at)).all()
    # amendments: only 8-K/As, linked to an earlier 8-K
    amend = events[events["event_kind"] == "amendment"]
    assert set(amend["form"]) == {"8-K/A"} and set(amend["amends_how"]) <= set(er.AMEND_LINKS)
    assert (events.loc[events["form"] == "8-K", ["amends_accession", "amends_how"]] == "").all().all()
    assert (amend["amends_accession"] != "").all()


def test_built_scope_has_no_foreign_filer_and_fallback_rows_are_periodic():
    events, fallback = _built(er.EVENTS_OUT), _built(er.FALLBACK_OUT)
    master = pd.read_csv(er.MASTER, dtype=str, keep_default_na=False)
    foreign = set(master.loc[master["foreign_filer"] == "Y", "cik"])
    assert not (set(events["cik"]) | set(fallback["cik"])) & foreign
    assert list(fallback.columns) == er.FALLBACK_COLUMNS
    assert set(fallback["form"]) <= er.PERIODIC_FORMS and set(fallback["event_kind"]) == {"periodic_report"}
    assert not fallback.duplicated(["cik", "report_date"]).any()
    # a fallback quarter has no non-amendment Item 2.02 event assigned to it
    quarters = set(zip(events.loc[events["event_kind"] != "amendment", "cik"],
                       events.loc[events["event_kind"] != "amendment", "fiscal_quarter_end"]))
    assert not any((c, q) in quarters for c, q in zip(fallback["cik"], fallback["report_date"]))
    # catch-up, Item 2.02-between and 7.01/8.01-between rows are never usable as announcement dates
    days = fallback["days_after_period_end"].astype(int)
    limit = fallback["form"].map(er.CATCH_UP_DAYS)
    assert ((days > limit) == fallback["catch_up_reason"].str.contains("past_due")).all()
    shared = fallback.duplicated(["cik", "d0_session"], keep=False)
    assert (shared == fallback["catch_up_reason"].str.contains("shared_d0")).all()
    usable = ((fallback["catch_up_filing"] == "N") & (fallback["item202_between"] == "")
              & (fallback["other_8k_between"] == ""))
    assert (usable == (fallback["usable_as_announcement"] == "Y")).all()


def test_built_rows_of_mixed_filers_carry_the_regime_on_d0():
    events = _built(er.EVENTS_OUT)
    if not er.PERIODIC_HISTORY.exists():
        pytest.skip("periodic_form_history.csv not built")
    history = er.load_history()
    mixed = events[events["foreign_filer"].str.contains("MIXED")]
    assert set(mixed["cik"].astype(int)) <= set(history["cik"].astype(int))
    assert (events.loc[~events["foreign_filer"].str.contains("MIXED"), "foreign_regime_on_d0"] == "N").all()
    flags = pd.Series(dict(zip(mixed["cik"].astype(int), mixed["foreign_filer"])), dtype=object)
    expected = er.regime_foreign(flags, history, mixed["cik"].astype(int), mixed["d0_session"])
    assert ((mixed["foreign_regime_on_d0"] == "Y") == expected).all()
    # Atlassian: 20-F filer until its first 10-Q on 2022-11-04; no week before that counts (whether
    # the prefilter already left those weeks out of its ranks or this step drops them)
    scope = pd.read_csv(er.OUT / "scope.csv", dtype=str, keep_default_na=False).set_index("cik")
    if "1650372" in scope.index:
        assert scope.loc["1650372", "top300_first_week"] >= "2022-11-04"
        assert scope.loc["1650372", "listed_first_week"] >= "2022-11-04"


def test_built_sic_history_has_four_digit_codes_from_headers():
    sic = _built(er.SIC_OUT)
    assert list(sic.columns) == er.SIC_COLUMNS
    assert sic["sic"].str.fullmatch(r"\d{4}").all()
    assert not sic.duplicated(["cik", "source_accession"]).any()
    assert set(sic["blank_check_6770"]) <= {"Y", "N"}


def test_a_report_after_an_801_release_is_not_an_announcement_date():
    table = _table([
        ("k1", "2018-03-30", "2018-01-31", "10-K", ""),
        ("r1", "2018-03-06", "2018-03-06", "8-K", "8.01,9.01"),   # the release, filed under 8.01
        ("q1", "2018-06-08", "2018-04-30", "10-Q", ""),           # no 8-K between: usable
    ])
    fallback = er.fallback_filings(table, set())
    rows = fallback.rename(columns={"accessionNumber": "accession", "filingDate": "filing_date"})
    rows = rows.assign(cik=7, d0_session=["2018-04-02", "2018-06-11"])
    marked = er.mark_fallback(rows)
    assert marked["other_8k_between"].tolist() == ["2018-03-06:8.01,9.01", ""]
    assert marked["usable_as_announcement"].tolist() == ["N", "Y"]


# ------------------------------------------------------------------ event windows and the SEC rate

def _company(monkeypatch, table):
    monkeypatch.setattr(er, "company_filings", lambda cik, offline=False: {
        "cik": cik, "table": table, "pages_needed": 0, "pages_read": 0, "missing": "", "fiscal_year_end": ""})


def test_only_filings_inside_the_window_get_a_header_job(monkeypatch):
    table = _table([("q0", "2015-11-05", "2015-09-30", "10-Q", ""),
                    ("e0", "2016-02-01", "2016-02-01", "8-K", "2.02,9.01"),
                    ("k0", "2016-02-20", "2015-12-31", "10-K", ""),
                    ("q1", "2016-05-05", "2016-03-31", "10-Q", ""),
                    ("e1", "2016-04-26", "2016-04-26", "8-K", "2.02,9.01"),
                    ("e2", "2016-04-27", "2016-04-27", "8-K", "2.02,9.01"),
                    ("q2", "2016-08-04", "2016-06-30", "10-Q", "")])
    _company(monkeypatch, table)
    plan = er.plan_company(7, offline=True, window=("2016-04-27", "2016-12-31"))
    # e1 is filed a day before the window but belongs to the fiscal quarter of e2, so it comes too
    assert plan["events"].set_index("accessionNumber")["in_window"].to_dict() == {"e0": False, "e1": True, "e2": True}
    assert plan["fallback"].set_index("accessionNumber")["in_window"].to_dict() == {"q0": False, "q2": True}
    assert er.header_jobs({7: plan}) == [(7, "e1", "2016-04-26", "item202"), (7, "e2", "2016-04-27", "item202"),
                                         (7, "q2", "2016-08-04", "periodic_fallback")]
    # without a window everything from EVENTS_FROM counts
    assert len(er.header_jobs({7: er.plan_company(7, offline=True)})) == 5


def test_rows_outside_the_window_are_dropped_after_the_whole_sequence_is_classified(monkeypatch, tmp_path, calendar):
    monkeypatch.setattr(er, "HEADER_DIR", tmp_path)   # no header cached: times come from the JSON
    table = _table([("q1", "2016-05-05", "2016-03-31", "10-Q", ""),
                    ("e1", "2016-04-26", "2016-04-26", "8-K", "2.02,9.01"),
                    ("e2", "2016-04-27", "2016-04-27", "8-K", "2.02,9.01")])
    _company(monkeypatch, pd.concat([table, _table([("k0", "2016-02-20", "2015-12-31", "10-K", "")])], ignore_index=True))
    plan = er.plan_company(7, offline=True, window=("2016-04-27", "2016-12-31"))
    plan["events"]["in_window"] = [False, True]   # as if the fiscal quarter were not brought in whole
    scope = pd.DataFrame({"cik": [7], "security_ids": ["7"], "foreign_filer": ["N"]})
    events, fallback = er.build_tables({7: plan}, scope, calendar)
    # e1 (outside) is the release of the quarter; e2, the next day, is 'other', not a lone release
    assert events["accession"].tolist() == ["e2"]
    assert events[["event_kind", "n_item202_in_fiscal_quarter"]].iloc[0].tolist() == ["other", "2"]
    assert fallback.empty   # k0 is filed before the window


def test_sec_rate_is_capped_at_four_a_second():
    er.set_sec_rate(3)
    assert er.SEC_LIMITER.windows == {1: 3}
    er.set_sec_rate(0.5)
    assert er.SEC_LIMITER.windows == {2.0: 1}
    with pytest.raises(ValueError):
        er.set_sec_rate(5)
    er.set_sec_rate(er.SEC_RATE_MAX)
    assert er.SEC_LIMITER.windows == {1: 4} and er.SEC_LIMITER is not er.common.SEC_LIMITER


def test_missing_quarters_are_in_scope_full_quarters_without_a_usable_date():
    quarters = pd.DataFrame({"cik": [7, 7, 7, 8], "quarter": ["2016Q1", "2016Q2", "2016Q3", "2016Q1"],
                             "listed_weeks": [13, 13, 13, 4], "scope_weeks": [13, 13, 0, 4], "quarter_weeks": [13] * 4,
                             "n_item202": [1, 1, 0, 0], "n_item202_original": [1, 0, 0, 0], "n_results_release": [1, 0, 0, 0],
                             "n_fallback": [0, 1, 0, 0], "n_fallback_usable": [0, 0, 0, 0]})
    quarters["full_quarter"] = quarters["listed_weeks"] >= quarters["quarter_weeks"]
    quarters["usable_date"] = (quarters["n_item202_original"] > 0) | (quarters["n_fallback_usable"] > 0)
    scope = pd.DataFrame({"cik": [7, 8], "name": ["S", "T"], "security_ids": ["7", "8"], "in_top300": ["Y", "N"],
                          "in_candidates": ["N", "Y"]})
    # 2016Q2 has only an amendment and an unusable fallback; Q3 is out of scope; CIK 8 is not listed all quarter
    assert er.missing_quarters(quarters, scope)[["cik", "quarter"]].values.tolist() == [[7, "2016Q2"]]
    assert er.coverage_summary(quarters)["share_usable_date"] == 0.5


# ------------------------------------------------------------------ hand sample

INDEX_PAGE = """<div class="formGrouping"><div class="infoHead">Filing Date</div>
<div class="info">2016-04-26</div><div class="infoHead">Accepted</div>
<div class="info">2016-04-26 16:31:09</div></div>
<div class="infoHead">Items</div><div class="info">Item 2.02: Results of Operations and Financial Condition<br>Item 9.01: Exhibits</div>
<table class="tableFile" summary="Document Format Files">
<tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
<tr><td>1</td><td>FORM 8-K</td><td><a href="/ix?doc=/Archives/edgar/data/1/000000000116000001/a8k.htm">a8k.htm</a> &nbsp;iXBRL</td><td>8-K</td><td>1</td></tr>
<tr class="blueRow"><td>2</td><td>EX-99.1</td><td><a href="/Archives/edgar/data/1/000000000116000001/ex991.htm">ex991.htm</a></td><td>EX-99.1</td><td>2</td></tr>
</table>"""


def test_filing_index_gives_accepted_time_items_and_documents():
    parsed = er.parse_filing_index(INDEX_PAGE)
    assert parsed["info"]["Accepted"] == "2016-04-26 16:31:09"
    assert parsed["info"]["Items"].startswith("Item 2.02: Results of Operations")
    assert [(d["type"], d["document"]) for d in parsed["documents"]] == [("8-K", "a8k.htm"), ("EX-99.1", "ex991.htm")]
    assert parsed["documents"][0]["url"] == "https://www.sec.gov/Archives/edgar/data/1/000000000116000001/a8k.htm"


def test_release_evidence_finds_the_results_sentence_session_and_call_time():
    text = er.html_text(b"<p>ACME Reports Third Quarter Results</p><p>BOSTON, Oct. 20, 2015 -- ACME Corp. today "
                        b"announced results for its third quarter ended September 30, 2015, after the market close.</p>"
                        b"<p>The company will host a call at 5:00 p.m. ET today.</p>")
    evidence = er.release_evidence(text)
    assert "today announced results for its third quarter ended September 30" in evidence["results_sentence"]
    assert "after the market close" in evidence["session_phrases"]
    assert "5:00 p.m. ET" in evidence["call_times"]


def test_hand_sample_draw_is_fixed_by_the_seed_and_takes_one_release_per_quarter():
    events = pd.DataFrame({"cik": ["1", "1", "1", "2"], "accession": ["a", "b", "c", "d"],
                           "event_kind": ["results_release", "results_release", "preannouncement", "results_release"],
                           "d0_session": ["2016-01-05", "2016-03-30", "2016-01-02", "2016-04-28"],
                           "acceptance_et": ["2016-01-04 16:05:00", "2016-03-30 07:00:00", "2016-01-02 08:00:00",
                                             "2016-04-27 16:10:00"]})
    quarters = pd.DataFrame({"cik": [1, 2, 2], "quarter": ["2016Q1", "2016Q2", "2016Q3"], "scope_weeks": [3, 1, 0],
                             "full_quarter": [True, True, True]})
    population = er.hand_sample_population(events, quarters)
    assert population[["cik", "quarter", "accession"]].values.tolist() == [[1, "2016Q1", "a"], [2, "2016Q2", "d"]]
    first, again = er.draw_hand_sample(population, seed=7, n=2), er.draw_hand_sample(population, seed=7, n=2)
    assert first["accession"].tolist() == again["accession"].tolist()
    assert sorted(first["accession"]) == ["a", "d"] and first["draw_order"].tolist() == [1, 2]
    assert set(first["population"]) == {2}


# ------------------------------------------------------------------ rules (a) and (b): the 8-K's own Item 2.02 text

T2_8K = ("FORM 8-K Date of Report (Date of earliest event reported): May 5, 2022 T2 Biosystems, Inc. "
         "Item 2.02 Results of Operations and Financial Condition On May 5, 2022, the Company issued a press release "
         "announcing its financial results for its fiscal quarter ended March 31, 2022, and held a conference call to "
         "discuss those results. A copy of the Company's press release and a copy of the transcript of the conference "
         "call are furnished with this report as Exhibits 99.1 and 99.2, respectively. The information in this Item "
         "2.02 shall not be deemed filed. Item 3.01. Notice of Delisting. On November 5, 2021, we received a letter "
         "from The Nasdaq Stock Market. Item 9.01 Financial Statements and Exhibits.")
INTERFACE_PRES = ("Item 2.02 Results of Operations and Financial Condition. During May 1-2, 2018, Interface, Inc. (the "
                  "\"Company\") will meet and present to investors and potential investors. A copy of the slide "
                  "presentation is attached as Exhibit 99.1. Item 9.01 Financial Statements and Exhibits.")
INTERFACE_RELEASE = ("ITEM 2.02 RESULTS OF OPERATIONS AND FINANCIAL CONDITION. On April 25, 2018, Interface, Inc. (the "
                     "\"Company\") issued a press release reporting its financial results for the first quarter of 2018 "
                     "(the \"Earnings Release\"). ITEM 5.02 DEPARTURE OF DIRECTORS. On April 24, 2018 the Board ...")
AMD_JOINED = ("Item 2.02 Results of Operations and Financial Condition. Item 7.01 Regulation FD Disclosure. The "
              "information in this report furnished pursuant to Items 2.02 and 7.01 shall not be deemed filed. On "
              "October 16, 2014, Advanced Micro Devices, Inc. (the Company) announced its financial position and "
              "results of operations as of and for its fiscal quarter ended September 27, 2014 in a press release. "
              "Item 9.01 Financial Statements and Exhibits.")


def test_calendar_gives_the_latest_and_earliest_d0_of_a_release_day(calendar):
    # a session day: before its close the day itself, after it the next session
    assert calendar.latest_d0_on_date("2022-05-05") == ("2022-05-06", "2022-05-05")
    # a Saturday: the Monday either way
    assert calendar.latest_d0_on_date("2022-05-07") == ("2022-05-09", "2022-05-09")
    assert calendar.is_session("2022-05-06") and not calendar.is_session("2022-05-07")


def test_item_section_skips_a_cover_page_heading():
    text = ("Item 2.02 Results of Operations UNITED STATES SECURITIES AND EXCHANGE COMMISSION FORM 8-K Date of Report: "
            "July 25, 2013 KLA-TENCOR Item 2.02 Results of Operations and Financial Condition. On July 25, 2013, the "
            "Company issued a press release announcing its results for the fourth quarter. Item 9.01 Exhibits.")
    assert er.item_section(text).startswith("On July 25, 2013, the Company issued")


def test_item_section_skips_mentions_and_joins_run_together_headings():
    assert er.item_section(T2_8K).startswith("On May 5, 2022, the Company issued a press release")
    assert "Nasdaq" not in er.item_section(T2_8K)      # stops at the Item 3.01 heading
    joined = er.item_section(AMD_JOINED)              # the 2.02 heading is followed at once by 7.01's
    assert "On October 16, 2014, Advanced Micro Devices" in joined and "Item 9.01" not in joined
    assert er.item_section("no items here") == ""


@pytest.mark.parametrize("text, low, high, kind, date", [
    (T2_8K, "2022-03-31", "2022-05-11", "results_release", "2022-05-05"),        # press release and a transcript
    (INTERFACE_PRES, "2018-04-01", "2018-04-30", "presentation", ""),            # May 1-2 is after the acceptance
    (INTERFACE_RELEASE, "2018-04-01", "2018-04-25", "results_release", "2018-04-25"),
    (AMD_JOINED, "2014-09-27", "2014-10-16", "results_release", "2014-10-16"),
    ("Item 2.02. On May 3, 2018, Aceto Corporation issued the attached press release that included financial "
     "information for its third quarter ended March 31, 2018.", "2018-03-31", "2018-05-04", "results_release", "2018-05-03"),
    ("Item 2.02. On February 10, 2014, American Airlines Group Inc. announced via press release certain traffic "
     "statistics for January 2014.", "2013-12-31", "2014-02-10", "statistics", "2014-02-10"),
    # a bare or boilerplate-only paragraph says nothing either way
    ("Item 2.02. On October 22, 2019, Simmons First National Corporation issued a press release, a copy of which is "
     "attached hereto as Exhibit 99.1.", "2019-09-30", "2019-10-22", "release_unspecified", "2019-10-22"),
    ("Item 2.02. As previously reported, effective May 3, 2024, Quantum Computing Inc. dismissed its auditor. On May 3, "
     "2024, the Securities and Exchange Commission issued an order.", "2024-03-31", "2024-06-11", "other", "2024-05-03"),
    ("Item 2.02. On January 23, 2014, Quality Systems, Inc. issued a press release announcing its financial performance "
     "for the period ended December 31, 2013.", "2013-12-31", "2014-01-23", "results_release", "2014-01-23"),
    ("Item 2.02. On February 3, 2020, Astronics issued a news release providing its preliminary revenue for the "
     "fourth quarter.", "2019-12-31", "2020-02-04", "preliminary", "2020-02-03"),
    ("Item 2.02. The Company furnishes the transcript of its conference call held on January 25, 2019.",
     "2018-12-31", "2019-01-31", "transcript", "2019-01-25"),
])
def test_item202_evidence_reads_the_release_date_and_what_is_furnished(text, low, high, kind, date):
    found = er.item202_evidence(text, low, high)
    assert (found["item202_kind"], found["item202_date"]) == (kind, date)


@pytest.mark.parametrize("text, low, high, date", [
    # dates cited from an earlier filing or event are not the release date
    ("Item 2.02. Explanatory Note: On May 14, 2018, Mimecast Limited (the Company) filed a Current Report on Form 8-K "
     "reporting its results.", "2018-03-31", "2018-05-29", ""),
    ("Item 2.02. As previously announced on April 16, 2012, we entered into an Agreement and Plan of Merger.",
     "2012-03-31", "2012-06-14", ""),
    ("Item 2.02. Certain financial information was included in the Proxy Statement filed on December 4, 2013, in "
     "connection with the merger, and is reported here.", "2013-09-30", "2013-12-27", ""),
    ("Item 2.02. As previously reported, effective May 3, 2024, the Company dismissed its auditor.", "2024-03-31",
     "2024-06-11", ""),
    ("Item 2.02. As previously reported in the Current Report on Form 8-K filed with the U.S. Securities and Exchange "
     "Commission on April 29, 2020 by ATN International, the Company reported its results.", "2020-03-31", "2020-06-08", ""),
    ("Item 2.02. As previously announced on April 16, 2012, we entered into an Agreement and Plan of Merger on April 13, "
     "2012, with Objet Ltd., under which Objet reported results.", "2012-03-31", "2012-06-14", ""),
    ("Item 2.02. The Company expects to report that it had approximately $132.0 million in cash as of December 31, "
     "2021, which it disclosed today.", "2021-09-30", "2022-02-01", ""),
    # a release issued on a date, a call held on a date
    ("Item 2.02. Aegion Corporation issued an earnings release on May 2, 2018 to announce its financial results for "
     "the quarter ended March 31, 2018.", "2018-03-31", "2018-05-08", "2018-05-02"),
    ("Item 2.02. On April 20, 2021, management of Peoples Bancorp Inc. conducted a facilitated conference call to "
     "discuss results of operations.", "2021-03-31", "2021-04-23", "2021-04-20"),
])
def test_item202_date_is_the_release_not_an_earlier_filing(text, low, high, date):
    assert er.item202_evidence(text, low, high)["item202_date"] == date


def test_item202_evidence_without_a_document_or_an_item202_heading():
    assert er.item202_evidence(None, "2020-01-01", "2020-02-01")["item202_kind"] == "no_document"
    assert er.item202_evidence("Item 8.01 Other events.", "2020-01-01", "2020-02-01")["item202_kind"] == "no_item202_text"


def test_report_date_timing_says_how_far_the_period_of_report_lies_before_d0(calendar):
    frame = pd.DataFrame({
        "cik": [1, 1, 1, 1, 1], "report_date": ["2022-05-05", "2022-03-31", "2022-04-30", "", "2022-05-12"],
        "acceptance_et": ["2022-05-11 16:25:03"] * 3 + ["2022-05-11 16:25:03", "2022-05-11 16:25:03"],
        "d0_session": ["2022-05-12"] * 5, "fiscal_quarter_end": ["2022-03-31"] * 5})
    out = er.report_date_timing(frame, calendar, {1: {"2022-03-31"}})
    assert out["report_date_kind"].tolist() == ["event_date", "period_end", "month_end", "blank", "after_acceptance"]
    assert out.loc[0, ["report_date_lag_days", "report_date_lag_sessions"]].tolist() == ["6", "5"]


def _quarter(rows):
    """Event rows of one company as build_tables has them before rules (a) and (b)."""
    frame = pd.DataFrame(rows, columns=["accession", "form", "items", "acceptance_sort", "report_date", "d0_session",
                                        "event_kind", "pick_not_first", "item202_kind", "item202_date",
                                        "report_date_lag_sessions", "report_date_kind"])
    return frame.assign(cik=7, fiscal_quarter_end="2018-04-01", event_kind_basis="last_run_by_report",
                        d0_session_acceptance=frame["d0_session"])


def test_a_pick_that_furnishes_slides_gives_way_to_the_earlier_results_release():
    events = _quarter([   # Interface Q1 2018
        ("p0", "8-K", "2.02,9.01", "2018-03-01 08:00:00", "2018-03-01", "2018-03-01", "preannouncement", "N",
         "preliminary", "", "0", "event_date"),
        ("r1", "8-K", "2.02,5.02,9.01", "2018-04-25 16:16:21", "2018-04-24", "2018-04-26", "preannouncement", "N",
         "results_release", "2018-04-25", "2", "event_date"),
        ("s2", "8-K", "2.02,9.01", "2018-04-30 15:48:35", "2018-04-30", "2018-04-30", "results_release", "Y",
         "presentation", "", "0", "event_date")])
    out = er.reclassify_by_text(events)
    assert out["event_kind"].tolist() == ["preannouncement", "results_release", "other"]
    assert set(out["event_kind_basis"]) == {"item202_text"} and set(out["release_check"]) == {"moved_from:s2"}
    assert out["pick_not_first"].tolist() == ["N", "Y", "N"]
    # without an earlier results release the pick stays, flagged
    kept = er.reclassify_by_text(events.assign(item202_kind=["preliminary", "preliminary", "transcript"]))
    assert kept["event_kind"].tolist() == events["event_kind"].tolist()
    assert kept["release_check"].tolist() == ["", "", "pick_furnishes_no_release"]
    # a pick whose paragraph says nothing either way ('other') never moves the release
    bare = er.reclassify_by_text(events.assign(item202_kind=["preliminary", "results_release", "other"]))
    assert bare["event_kind"].tolist() == events["event_kind"].tolist() and set(bare["release_check"]) == {""}


def test_a_late_furnished_release_takes_the_latest_d0_of_its_release_day(calendar):
    events = _quarter([
        # T2: released 2022-05-05 (its text), accepted 2022-05-11: D0 the session after the release day
        ("t2", "8-K", "2.02,3.01,9.01", "2022-05-11 16:25:03", "2022-05-05", "2022-05-12", "results_release", "N",
         "results_release", "2022-05-05", "5", "event_date"),
        # Interface: the period of report is the 5.02 date; the text's release day gives the acceptance D0 back
        ("r1", "8-K", "2.02,5.02,9.01", "2018-04-25 16:16:21", "2018-04-24", "2018-04-26", "results_release", "N",
         "results_release", "2018-04-25", "2", "event_date"),
        # no text date, only release items: the period of report is the release day
        ("u1", "8-K", "2.02,9.01", "2020-02-04 16:42:28", "2020-02-03", "2020-02-05", "results_release", "N",
         "no_item202_text", "", "2", "event_date"),
        # ... unless the text describes something other than a release (an acquired business's statements)
        ("s1", "8-K", "2.02,9.01", "2026-03-31 16:00:00", "2026-02-13", "2026-04-01", "results_release", "N",
         "other", "", "31", "event_date"),
        # no text date and another item (AMD 2014: a 2.05 date): D0 stays
        ("a1", "8-K", "2.02,2.05,7.01,9.01", "2014-10-16 16:21:56", "2014-10-10", "2014-10-17", "results_release", "N",
         "no_item202_text", "", "5", "event_date"),
        # a period end as the period of report and no text date: unresolved, D0 stays
        ("c1", "8-K", "2.02,9.01", "2020-08-05 07:00:18", "2020-06-30", "2020-08-05", "results_release", "N",
         "no_document", "", "25", "period_end"),
        # a text about something else (an auditor change): its date is not the release's
        ("q1", "8-K", "2.02,4.01,9.01", "2024-06-11 17:26:37", "2024-06-06", "2024-06-12", "results_release", "N",
         "other", "2024-05-03", "4", "event_date"),
        # an amendment is never moved
        ("m1", "8-K/A", "2.02,9.01", "2022-05-20 16:00:00", "2022-05-05", "2022-05-23", "amendment", "N",
         "", "", "12", "event_date")])
    out = er.late_furnished_d0(events, calendar).set_index("accession")
    assert out["d0_session"].to_dict() == {"t2": "2022-05-06", "r1": "2018-04-26", "u1": "2020-02-04", "s1": "2026-04-01",
                                           "a1": "2014-10-17", "c1": "2020-08-05", "q1": "2024-06-12", "m1": "2022-05-23"}
    assert out["late_furnished"].to_dict() == {"t2": "Y", "r1": "N", "u1": "Y", "s1": "N", "a1": "N", "c1": "N", "q1": "N",
                                               "m1": "N"}
    assert out["release_date_basis"].to_dict() == {"t2": "item202_text", "r1": "item202_text", "u1": "report_date",
                                                   "s1": "unresolved_not_a_release", "a1": "unresolved_other_items",
                                                   "c1": "unresolved", "q1": "unresolved_other_items", "m1": ""}
    assert out.loc["t2", "d0_basis"] == "release_date_latest" and out.loc["r1", "d0_basis"] == "acceptance"
    assert out.loc["t2", "d0_session_acceptance"] == "2022-05-12"   # kept


def test_evidence_rows_take_late_candidates_and_picks_then_the_earlier_8ks_of_a_slide_pick():
    events = _quarter([
        ("r1", "8-K", "2.02,9.01", "2018-04-25 16:16:21", "2018-04-25", "2018-04-26", "preannouncement", "N",
         "", "", "0", "event_date"),
        ("s2", "8-K", "2.02,9.01", "2018-04-30 15:48:35", "2018-04-30", "2018-04-30", "results_release", "Y",
         "", "", "0", "event_date"),
        ("t3", "8-K", "2.02,9.01", "2018-05-09 16:00:00", "2018-05-02", "2018-05-10", "other", "N",
         "", "", "6", "event_date")]).assign(in_window=True)
    first = er.evidence_rows(events, 1)
    assert first["accession"].tolist() == ["s2", "t3"]
    assert first["evidence_reason"].tolist() == ["release_not_first", "late_candidate"]
    assert er.evidence_rows(events, 2).empty                     # the pick's kind is not known yet
    second = er.evidence_rows(events.assign(item202_kind=["", "presentation", ""]), 2)
    assert second["accession"].tolist() == ["r1"]


def test_first_in_fiscal_quarter_is_the_first_non_amendment_event_by_d0():
    events = pd.DataFrame({"cik": [7] * 5, "fiscal_quarter_end": ["2022-03-31"] * 3 + ["2022-06-30", ""],
                           "event_kind": ["amendment", "results_release", "other", "results_release", "other"],
                           "d0_session": ["2022-05-02", "2022-05-06", "2022-05-10", "2022-08-05", "2022-09-01"],
                           "acceptance_sort": ["2022-05-02"] * 5, "accession": list("abcde")})
    assert er.first_in_fiscal_quarter(events).tolist() == ["N", "Y", "N", "Y", "N"]


def test_presence_counts_extra_weeks_and_gaps_use_the_same_population():
    weekly = pd.DataFrame({"cik": [7] * 4, "week_end": pd.to_datetime(["2016-01-08", "2016-04-08", "2016-07-08",
                                                                         "2016-10-07"]), "universe": True})
    weeks = weekly[["cik", "week_end"]]
    top = weeks.iloc[[2, 3]]
    present = er.presence(weeks, weekly, {7}, extra={"top300_weeks": top})
    assert present.set_index("quarter")["top300_weeks"].to_dict() == {"2016Q1": 0, "2016Q2": 0, "2016Q3": 1, "2016Q4": 1}
    quarters = present.assign(full_quarter=True)
    events = pd.DataFrame({"cik": [7] * 4, "event_kind": ["results_release"] * 4,
                           "d0_session": ["2016-02-01", "2016-05-02", "2016-08-01", "2016-12-05"]})
    fallback = pd.DataFrame(columns=["cik", "d0_session", "usable_as_announcement"])
    on_top = er.gap_summary_population(events, fallback, quarters, "top300_weeks")
    assert on_top["gaps"] == 2 and on_top["over_120"] == 1     # the gaps ending in 2016Q3 (91) and 2016Q4 (126)
    assert er.gap_summary_population(events, fallback, quarters, "scope_weeks")["gaps"] == 3
    assert er.coverage_summary(quarters.assign(n_item202=1, n_fallback=0, n_fallback_usable=0, usable_date=True),
                               "top300_weeks")["company_quarters"] == 2


def test_round7_is_rescored_on_the_current_release_of_each_company_quarter():
    events = pd.DataFrame({"cik": ["715787", "715787"], "fiscal_quarter_end": ["2018-04-01"] * 2,
                           "accession": ["r1", "s2"], "event_kind": ["results_release", "other"],
                           "d0_session": ["2018-04-26", "2018-04-30"]})
    keys = pd.DataFrame({"cik": ["715787"], "fiscal_quarter_end": ["2018-04-01"], "draw_order": ["20"],
                         "seed": ["20261002"], "population": ["76064"], "accession_drawn": ["s2"]})
    rows = er.round7_rescored(events, keys)
    assert rows[["accession", "accession_drawn_round7", "quarter", "draw_order"]].values.tolist() == [["r1", "s2", "2018Q2", 20]]


def test_hand_check_table_has_the_columns_validate_reads():
    rows = pd.DataFrame({c: ["x"] for c in er.HAND_CHECK_COLUMNS if c not in er.HAND_VERDICT_FIELDS})
    rows["accession"] = ["0001-22-000001"]
    out = er.hand_check_table(rows, {"0001-22-000001": {"d0_matches": "Y", "ir_url": "u", "ir_release_et": "2022-05-05",
                                                         "checked_at": "2026-10-02T18:00:00+00:00"}})
    assert {"accession", "security_id", "ir_url", "ir_release_et", "d0_matches", "checked_at"} <= set(out.columns)
    assert out.loc[0, "d0_matches"] == "Y" and out.loc[0, "match_type"] == ""
    assert er.HAND_SAMPLE_SEED_FRESH != er.HAND_SAMPLE_SEED


def test_built_late_and_moved_rows_are_consistent():
    events = _built(er.EVENTS_OUT)
    late = events[events["late_furnished"] == "Y"]
    assert (late["event_kind"] != "amendment").all()
    assert (late["report_date_lag_sessions"].astype(int) >= er.LATE_MIN_SESSIONS).all()
    assert late["release_date_basis"].isin(["item202_text", "report_date"]).all()
    # D0 is the session after a session-day release date (or the first session after a non-session day)
    calendar = er.XnasCloses()
    assert all(calendar.latest_d0_on_date(day)[0] == d0 for day, d0 in zip(late["release_date"], late["d0_session"]))
    assert set(events["first_in_fiscal_quarter"]) <= {"Y", "N"}
    fallback = _built(er.FALLBACK_OUT)
    assert set(fallback["first_in_fiscal_quarter"]) == {"Y"}
    moved = events[events["release_check"].str.startswith("moved_from")]
    assert (moved["event_kind_basis"] == "item202_text").all()
    releases = moved[moved["event_kind"] == "results_release"]
    by_text = releases[releases["release_check"].str.startswith("moved_from:")]
    assert by_text["item202_kind"].eq("results_release").all()   # the text says it reports results
    # a re-furnishing's release: accepted on or after the date the old pick gives, first traded on it
    old = events.set_index("accession")
    for row in releases[releases["release_check"].str.startswith("moved_from_refurnished:")].itertuples():
        pick = old.loc[row.release_check.split(":")[1]]
        latest, earliest = calendar.latest_d0_on_date(pick["item202_date"])
        assert row.acceptance_et[:10] >= pick["item202_date"] and earliest <= row.d0_session_acceptance <= latest
        assert pick["event_kind"] == "other"


def test_a_pick_that_dates_the_release_to_an_earlier_8k_gives_way_to_it(calendar):
    events = _quarter([   # Western Digital FY2013 Q4: the release, then a re-furnishing that the last-run rule picked
        ("r1", "8-K", "2.02,9.01", "2013-07-24 16:05:00", "2013-07-24", "2013-07-25", "preannouncement", "N",
         "", "", "0", "event_date"),
        ("f2", "8-K", "2.02,9.01", "2013-08-15 08:00:00", "2013-08-14", "2013-08-15", "results_release", "Y",
         "results_release", "2013-07-24", "16", "event_date")])
    out = er.reclassify_by_text(events, calendar)
    assert out["event_kind"].tolist() == ["results_release", "other"]
    assert set(out["release_check"]) == {"moved_from_refurnished:f2"}
    assert er.reclassify_by_text(events)["event_kind"].tolist() == ["preannouncement", "results_release"]  # no calendar


def test_a_late_refurnishing_of_a_release_with_its_own_8k_keeps_its_d0(calendar):
    events = _quarter([   # Texas Capital: released 2015-01-21 in its own 8-K; a 2015-03-10 8-K re-furnishes it
        ("r1", "8-K", "2.02,9.01", "2015-01-21 16:05:00", "2015-01-21", "2015-01-22", "results_release", "N",
         "", "", "1", "event_date"),
        ("f2", "8-K", "2.02,9.01", "2015-03-10 16:30:00", "2015-01-21", "2015-03-11", "other", "N",
         "results_release", "2015-01-21", "33", "event_date")])
    out = er.late_furnished_d0(events, calendar).set_index("accession")
    assert out.loc["f2", ["d0_session", "late_furnished", "release_date_basis"]].tolist() == ["2015-03-11", "N",
                                                                                             "release_has_own_8k"]


def test_an_8k_accepted_before_the_stated_release_day_is_not_that_release(calendar):
    events = _quarter([   # America's Car-Mart: a debt 8-K accepted 2025-03-05 after the close; release 2025-03-06
        ("d1", "8-K", "2.02,9.01", "2025-03-05 17:41:00", "2025-03-03", "2025-03-06", "preannouncement", "N",
         "other", "2025-03-03", "2", "event_date"),
        ("r2", "8-K", "2.02,9.01", "2025-03-10 09:00:00", "2025-03-06", "2025-03-10", "results_release", "Y",
         "results_release", "2025-03-06", "2", "event_date")])
    assert er.reclassify_by_text(events, calendar)["event_kind"].tolist() == ["preannouncement", "results_release"]
    out = er.late_furnished_d0(events, calendar).set_index("accession")
    assert out.loc["r2", ["d0_session", "late_furnished", "release_date_basis"]].tolist() == ["2025-03-07", "Y", "item202_text"]
