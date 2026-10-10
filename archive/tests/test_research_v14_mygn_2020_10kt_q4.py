from scripts.research_v14_mygn_2020_10kt_q4 import (
    ACCESSION,
    EXPECTED,
    FILED,
    HALF_YEAR_EXPECTED,
    Q3_EXPECTED,
)


def test_mygn_10kt_direct_q4_is_contemporaneous() -> None:
    assert ACCESSION == "0000899923-21-000021"
    assert FILED == "2021-03-16"
    assert EXPECTED["revenue"]["value"] == 154_600_000.0
    assert EXPECTED["net_income"]["value"] == -37_900_000.0


def test_mygn_direct_q4_reconciles_to_transition_period() -> None:
    for metric, spec in EXPECTED.items():
        assert HALF_YEAR_EXPECTED[metric] - Q3_EXPECTED[metric] == spec["value"]
