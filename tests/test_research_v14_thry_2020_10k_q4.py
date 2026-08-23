from scripts.research_v14_thry_2020_10k_q4 import (
    ANNUAL_FILED,
    ANNUAL_NET_INCOME,
    EXPECTED_Q4,
    NINE_MONTH_FILED,
    NINE_MONTH_NET_INCOME,
)


def test_thry_q4_reconciles_to_contemporaneous_10k() -> None:
    assert ANNUAL_FILED == "2021-03-25"
    assert NINE_MONTH_FILED == "2020-11-12"
    assert NINE_MONTH_FILED < ANNUAL_FILED
    assert ANNUAL_NET_INCOME - NINE_MONTH_NET_INCOME == EXPECTED_Q4
    assert EXPECTED_Q4 == 109_800_000.0
