import pytest

from scripts.research_v14_grfs_sec_quarterly_filings import (
    PERIOD_EVIDENCE,
    validate_filing,
)


def _filing() -> bytes:
    return b"""
    <html><body>Grifols, S.A. consolidated financial statements
    <p>Expressed in thousands of Euros</p>
    <p>Net revenues 2,423,360</p>
    <p>Consolidated profit for the period 294,639</p>
    </body></html>
    """


def test_validate_filing_accepts_identity_currency_and_values() -> None:
    validate_filing(_filing(), expected_values=(2_423_360, 294_639))


def test_validate_filing_rejects_identity_currency_and_value_mismatch() -> None:
    with pytest.raises(ValueError, match="identity"):
        validate_filing(_filing().replace(b"Grifols", b"Other"),
                        expected_values=(2_423_360, 294_639))
    with pytest.raises(ValueError, match="currency"):
        validate_filing(_filing().replace(b"Euros", b"units"),
                        expected_values=(2_423_360, 294_639))
    with pytest.raises(ValueError, match="expected value"):
        validate_filing(_filing(), expected_values=(2_423_360, 1))


def test_all_derived_quarters_are_exact_differences() -> None:
    assert len(PERIOD_EVIDENCE) == 16
    for item in PERIOD_EVIDENCE.values():
        prior = item["prior"]
        if prior is None:
            assert item["derivation"] == "direct_three_month_statement"
            continue
        assert item["current"][2] - prior[2] == item["revenue"]
        assert item["current"][3] - prior[3] == item["profit"]


def test_pit_dates_are_not_backdated_to_quarter_end() -> None:
    assert PERIOD_EVIDENCE["2017-03-31"]["available_date"] == "2017-07-28"
    assert PERIOD_EVIDENCE["2018-03-31"]["available_date"] == "2018-07-27"
    assert PERIOD_EVIDENCE["2019-03-31"]["available_date"] == "2019-07-31"
    assert PERIOD_EVIDENCE["2020-03-31"]["available_date"] == "2020-07-30"
    assert all(item["available_date"] > end
               for end, item in PERIOD_EVIDENCE.items())


def test_each_recovered_year_closes_to_its_contemporaneous_fy() -> None:
    expected = {
        2017: (4_318_073, 661_314),
        2018: (4_486_724, 594_406),
        2019: (5_098_691, 648_644),
        2020: (5_340_038, 708_990),
    }
    for year, (revenue, profit) in expected.items():
        items = [item for end, item in PERIOD_EVIDENCE.items()
                 if end.startswith(str(year))]
        assert len(items) == 4
        assert sum(item["revenue"] for item in items) == revenue
        assert sum(item["profit"] for item in items) == profit
