from scripts.research_v14_epix_transition_quarters import EXPECTED, SOURCES, _number


def test_epix_transition_quarters_are_contemporaneous() -> None:
    assert SOURCES["2020_q2"]["filed"] == "2020-05-07"
    assert SOURCES["2020_q3"]["filed"] == "2020-08-07"
    assert SOURCES["2020_fy"]["filed"] == "2020-12-15"
    assert SOURCES["2021_q1"]["filed"] == "2021-02-11"
    assert EXPECTED["2020-09-30"] == -4_534_289.0


def test_epix_plain_html_numbers_are_unscaled_us_dollars() -> None:
    assert _number("(9,356,174)") == -9_356_174.0
    assert _number("$") is None
