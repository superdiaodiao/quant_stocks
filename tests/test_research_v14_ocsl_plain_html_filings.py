from scripts.research_v14_ocsl_plain_html_filings import (
    FILINGS,
    _number,
)


def test_ocsl_plain_html_sources_are_contemporaneous() -> None:
    assert FILINGS["2020Q4"]["filed"] == "2021-02-04"
    assert FILINGS["2021Q1"]["filed"] == "2021-05-06"
    assert FILINGS["2021Q2"]["filed"] == "2021-08-05"
    assert FILINGS["2021FY"]["filed"] == "2021-11-16"
    assert all(
        item["accession"].startswith("0001414932-21-")
        for item in FILINGS.values()
    )


def test_ocsl_plain_html_number_parser_handles_statement_format() -> None:
    assert _number("38,204") == 38_204_000.0
    assert _number("(165,467)") == -165_467_000.0
    assert _number("$") is None
    assert _number("—") is None


def test_ocsl_prior_comparisons_bind_existing_contemporaneous_history() -> None:
    assert FILINGS["2020Q4"]["expected_prior"] == (
        30_960_000.0, 13_843_000.0
    )
    assert FILINGS["2021Q1"]["expected_prior"] == (
        34_171_000.0, -165_467_000.0
    )
    assert FILINGS["2021Q2"]["expected_prior"] == (
        34_403_000.0, 120_231_000.0
    )
    assert FILINGS["2021FY"]["expected_prior"] == (
        143_133_000.0, 39_224_000.0
    )
