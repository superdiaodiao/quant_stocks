from scripts.research_v14_ssrm_2021_6k_quarters import FILINGS, _number


def test_ssrm_6k_quarters_are_direct_and_contemporaneous() -> None:
    assert FILINGS["2021-03-31"]["filed"] == "2021-05-06"
    assert FILINGS["2021-06-30"]["filed"] == "2021-08-04"
    assert FILINGS["2021-09-30"]["filed"] == "2021-11-03"
    assert FILINGS["2021-09-30"]["filed"] < "2021-11-30"
    assert FILINGS["2021-09-30"]["net_income"] == 62_454_000.0


def test_ssrm_plain_html_numbers_are_thousands_of_us_dollars() -> None:
    assert _number("322846") == 322_846_000.0
    assert _number("(24,663)") == -24_663_000.0
    assert _number("$") is None
