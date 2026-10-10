from scripts.research_v14_zs_2018_10k_q4 import (
    ANNUAL,
    ANNUAL_FILED,
    EXPECTED_Q4,
    NINE_MONTH,
    NINE_MONTH_FILED,
)


def test_zs_2018_q4_reconciles_before_missing_signal() -> None:
    assert NINE_MONTH_FILED == "2018-06-07"
    assert ANNUAL_FILED == "2018-09-13"
    assert ANNUAL_FILED < "2019-03-29"
    for metric in EXPECTED_Q4:
        assert ANNUAL[metric] - NINE_MONTH[metric] == EXPECTED_Q4[metric]
