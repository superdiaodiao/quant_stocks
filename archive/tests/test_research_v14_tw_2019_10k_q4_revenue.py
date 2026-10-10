from scripts.research_v14_tw_2019_10k_q4_revenue import (
    ANNUAL_FILED,
    ANNUAL_REVENUE,
    EXPECTED_Q4_REVENUE,
    LATER_COMPARATIVE_REVENUE,
    NINE_MONTH_FILED,
    NINE_MONTH_REVENUE,
)


def test_tw_q4_revenue_is_contemporaneous_and_cross_checked() -> None:
    assert NINE_MONTH_FILED == "2019-11-08"
    assert ANNUAL_FILED == "2020-02-21"
    assert NINE_MONTH_FILED < ANNUAL_FILED
    assert ANNUAL_REVENUE - NINE_MONTH_REVENUE == EXPECTED_Q4_REVENUE
    assert EXPECTED_Q4_REVENUE == LATER_COMPARATIVE_REVENUE
