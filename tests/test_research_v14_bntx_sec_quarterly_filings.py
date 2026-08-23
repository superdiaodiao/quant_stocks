import pytest

from scripts.research_v14_bntx_sec_quarterly_filings import (
    PERIOD_EVIDENCE,
    validate_filing,
)


def _filing() -> bytes:
    return b"""
    <html><body>BioNTech SE interim consolidated financial statements
    <p>in thousands of euros</p>
    <p>Total revenues 482,325</p>
    <p>Profit / (Loss) for the period 15,198</p>
    </body></html>
    """


def test_validate_filing_accepts_identity_currency_and_values() -> None:
    validate_filing(_filing(), expected_values=(482_325_000, 15_198_000))


def test_validate_filing_rejects_identity_currency_and_value_mismatch() -> None:
    with pytest.raises(ValueError, match="identity"):
        validate_filing(_filing().replace(b"BioNTech", b"Other"),
                        expected_values=(482_325_000, 15_198_000))
    with pytest.raises(ValueError, match="currency"):
        validate_filing(_filing().replace(b"euros", b"units"),
                        expected_values=(482_325_000, 15_198_000))
    with pytest.raises(ValueError, match="expected value"):
        validate_filing(_filing(), expected_values=(482_325_000, 1_234_567))


def test_q4_values_are_exact_contemporaneous_differences() -> None:
    assert len(PERIOD_EVIDENCE) == 12
    for item in PERIOD_EVIDENCE.values():
        prior = item["prior"]
        if prior is None:
            assert item["derivation"] in {
                "direct_three_month_ifrs_statement",
                "direct_comparative_three_month_statement",
            }
            continue
        assert item["current"][2] - prior[2] == item["revenue"]
        assert item["current"][3] - prior[3] == item["profit"]


def test_pit_dates_are_original_filing_dates() -> None:
    assert PERIOD_EVIDENCE["2019-03-31"]["available_date"] == "2020-05-12"
    assert PERIOD_EVIDENCE["2019-12-31"]["available_date"] == "2020-11-10"
    assert PERIOD_EVIDENCE["2020-12-31"]["available_date"] == "2021-03-30"
    assert PERIOD_EVIDENCE["2021-12-31"]["available_date"] == "2022-03-30"
    assert all(item["available_date"] > end
               for end, item in PERIOD_EVIDENCE.items())
