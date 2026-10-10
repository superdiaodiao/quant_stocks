import pytest

from scripts.research_v14_sblk_quarterly_reports import (
    PERIOD_EVIDENCE,
    Q3_PDF_SHA256,
    Q4_2019_PDF_SHA256,
    SOURCES,
    validate_q3_pdf_text,
    validate_q4_2019_pdf_text,
    validate_2019_quarter_pdf_text,
    validate_sec_html,
)


def _sec_filing() -> bytes:
    return b"""
    <html><body>Star Bulk Carriers Corp.
    <p>Expressed in thousands of U.S. Dollars</p>
    <p>Voyage revenues 306,996 511,878</p>
    <p>Net income/(loss) ( 41,365 ) 159,972</p>
    </body></html>
    """


def _q3_pdf_text() -> str:
    return """
    Star Bulk Carriers Corp. November 16, 2021
    Expressed in thousands of U.S. dollars
    Voyage Revenues $415,688 $200,222 $927,566 $507,218
    Net income/(loss) $220,407 $23,251 $380,379 ($18,114)
    Adjusted Net income / (loss) $224,671 $27,484 $389,314 ($12,756)
    """


def _q4_2019_pdf_text() -> str:
    return """
    Star Bulk Carriers Corp. February 19, 2020
    Expressed in thousands of U.S. dollars
    Voyage Revenues $248,639 $209,433 $821,365 $651,561
    Net income/(loss) $23,499 $11,715 ($16,201) $58,397
    Adjusted Net income / (loss) $34,500 $30,316 $24,229 $86,098
    """


def test_validate_sec_html_accepts_identity_currency_rows_and_values() -> None:
    validate_sec_html(
        _sec_filing(), expected_values=(306_996, 511_878, -41_365, 159_972)
    )


def test_validate_sec_html_rejects_identity_currency_and_value_mismatch() -> None:
    with pytest.raises(ValueError, match="identity"):
        validate_sec_html(
            _sec_filing().replace(b"Star Bulk", b"Other"),
            expected_values=(306_996,),
        )
    with pytest.raises(ValueError, match="currency"):
        validate_sec_html(
            _sec_filing().replace(b"U.S. Dollars", b"units"),
            expected_values=(306_996,),
        )
    with pytest.raises(ValueError, match="expected value"):
        validate_sec_html(_sec_filing(), expected_values=(1_234_567,))


def test_q3_pdf_validator_keeps_gaap_and_adjusted_net_income_separate() -> None:
    validate_q3_pdf_text(_q3_pdf_text())
    with pytest.raises(ValueError, match="evidence is not proven"):
        validate_q3_pdf_text(
            _q3_pdf_text().replace("$220,407", "$224,671", 1)
        )


def test_q4_2019_pdf_validator_keeps_gaap_and_adjusted_net_income_separate() -> None:
    validate_q4_2019_pdf_text(_q4_2019_pdf_text())
    with pytest.raises(ValueError, match="evidence is not proven"):
        validate_q4_2019_pdf_text(
            _q4_2019_pdf_text().replace("$23,499", "$34,500", 1)
        )


def test_all_derived_quarters_are_exact_same_scope_differences() -> None:
    assert len(PERIOD_EVIDENCE) == 12
    for item in PERIOD_EVIDENCE.values():
        prior = item["prior"]
        if prior is None:
            assert item["derivation"].startswith("direct_")
            continue
        assert item["current"][1] - prior[1] == item["revenue"]
        assert item["current"][2] - prior[2] == item["profit"]


def test_pit_dates_are_first_proven_dates_not_comparative_period_ends() -> None:
    assert PERIOD_EVIDENCE["2019-03-31"]["available_date"] == "2019-05-22"
    assert PERIOD_EVIDENCE["2019-06-30"]["available_date"] == "2019-08-07"
    assert PERIOD_EVIDENCE["2019-09-30"]["available_date"] == "2019-11-20"
    assert PERIOD_EVIDENCE["2019-12-31"]["available_date"] == "2020-02-19"
    assert PERIOD_EVIDENCE["2020-03-31"]["available_date"] == "2020-05-26"
    assert PERIOD_EVIDENCE["2020-06-30"]["available_date"] == "2020-08-05"
    assert PERIOD_EVIDENCE["2020-09-30"]["available_date"] == "2020-11-16"
    assert PERIOD_EVIDENCE["2020-12-31"]["available_date"] == "2021-02-17"
    assert PERIOD_EVIDENCE["2021-12-31"]["available_date"] == "2022-03-15"
    assert all(item["available_date"] > end
               for end, item in PERIOD_EVIDENCE.items())


def test_official_q3_source_is_sha_bound() -> None:
    source = SOURCES["q3_2021_ir_pdf"]
    assert source["expected_sha256"] == Q3_PDF_SHA256
    assert source["url"].startswith("https://www.starbulk.com/")
    assert source["available_date"] == "2021-11-16"


def test_official_q4_2019_source_is_sha_bound() -> None:
    source = SOURCES["q4_2019_ir_pdf"]
    assert source["expected_sha256"] == Q4_2019_PDF_SHA256
    assert source["url"].startswith("https://www.starbulk.com/")
    assert source["available_date"] == "2020-02-19"


@pytest.mark.parametrize("source_id", [
    "q1_2019_ir_pdf", "q2_2019_ir_pdf", "q3_2019_ir_pdf",
])
def test_early_2019_official_sources_are_sha_and_pit_bound(source_id: str) -> None:
    source = SOURCES[source_id]
    assert len(source["expected_sha256"]) == 64
    assert source["url"].startswith("https://www.starbulk.com/")
    assert source["available_date"].startswith("2019-")


def test_early_2019_validator_rejects_gaap_value_replacement() -> None:
    text = """
    Star Bulk Carriers Corp. May 22, 2019
    Expressed in thousands of U.S. dollars
    Voyage Revenues $166,490 $121,057
    Net income/(loss) ($5,342) $9,900
    Adjusted Net income / (loss) ($8,532)
    """
    validate_2019_quarter_pdf_text(
        text, SOURCES["q1_2019_ir_pdf"]["validation_patterns"]
    )
    with pytest.raises(ValueError, match="evidence is not proven"):
        validate_2019_quarter_pdf_text(
            text.replace("($5,342)", "($8,532)", 1),
            SOURCES["q1_2019_ir_pdf"]["validation_patterns"],
        )
