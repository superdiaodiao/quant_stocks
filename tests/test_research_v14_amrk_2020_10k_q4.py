from scripts.research_v14_amrk_2020_10k_q4 import (
    ANNUAL_FILED,
    ANNUAL_NET_INCOME,
    ANNUAL_REVENUE,
    EXPECTED_Q4,
    PRIOR_QUARTERS,
)


def test_amrk_q4_residual_uses_only_contemporaneous_inputs() -> None:
    assert ANNUAL_FILED == "2020-09-14"
    for quarter in PRIOR_QUARTERS.values():
        assert quarter["filed"] <= ANNUAL_FILED


def test_amrk_q4_reconciles_to_original_fiscal_year() -> None:
    assert ANNUAL_NET_INCOME - sum(
        quarter["net_income"] for quarter in PRIOR_QUARTERS.values()
    ) == EXPECTED_Q4["net_income"]
    assert ANNUAL_REVENUE - sum(
        quarter["revenue"] for quarter in PRIOR_QUARTERS.values()
    ) == EXPECTED_Q4["revenue"]
    assert EXPECTED_Q4["net_income"] == 17_826_000.0
