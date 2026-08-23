from decimal import Decimal

import pandas as pd
import pytest

from scripts.research_v14_ggal_ias29_quarters import (
    AVAILABLE_DATE,
    BLOCKED_SIGNALS,
    EXPECTED_ANNUAL,
    EXPECTED_REPORTS,
    TARGET_CPI,
    TARGET_FISCAL_ENDS,
    build_facts,
    derive_quarters,
    parse_annual_xbrl,
    parse_report,
)


def test_ias29_quarters_close_audited_years_and_produce_two_ttms() -> None:
    quarters, identities, ttm = derive_quarters()

    assert tuple(quarters) == TARGET_FISCAL_ENDS
    assert identities[2019]["revenue"] == (
        EXPECTED_ANNUAL[2019]["revenue"] * TARGET_CPI / Decimal("385.8826")
    )
    assert identities[2020]["net_income"] == (
        EXPECTED_ANNUAL[2020]["net_income"] * TARGET_CPI / Decimal("385.8826")
    )
    assert float(quarters["2019-09-30"]["revenue"]) == pytest.approx(
        56_412_086_530.60824
    )
    assert float(quarters["2020-12-31"]["net_income"]) == pytest.approx(
        2_685_468_870.264943
    )
    assert float(ttm["prior"]["revenue"]) == pytest.approx(
        239_781_619_346.6667
    )
    assert float(ttm["current"]["revenue"]) == pytest.approx(
        219_316_370_688.2922
    )
    assert float(ttm["growth"]["revenue"]) == pytest.approx(-0.0853495306)
    assert float(ttm["growth"]["net_income"]) == pytest.approx(-0.2895255833)


def test_report_parser_requires_ias29_and_exact_consolidated_triplets() -> None:
    expected = EXPECTED_REPORTS["2021_q2_report"]
    raw = b"""
    <html><body>
      <p>Adjusted and restated in constant currency pursuant to IAS 29.</p>
      <p>Net operating income 162 99 117</p>
      <p>Net income 35 10 3</p>
      <p>Net operating income 61,537 55,086 65,470 12 (6)</p>
      <p>Net income 8,884 2,381 8,764 273 1</p>
    </body></html>
    """
    parsed = parse_report(raw, expected)
    assert parsed["revenue"] == expected["revenue"]
    assert parsed["net_income"] == expected["net_income"]

    with pytest.raises(RuntimeError, match="IAS 29"):
        parse_report(raw.replace(b"IAS 29", b"historical cost"), expected)


def test_annual_xbrl_uses_dimensionless_ars_original_20f_facts() -> None:
    xml = f"""<xbrl xmlns=\"http://www.xbrl.org/2003/instance\"
      xmlns:ifrs=\"urn:ifrs\" xmlns:xbrldi=\"http://xbrl.org/2006/xbrldi\">
      <context id=\"y2019\"><entity><identifier scheme=\"sec\">{1114700}</identifier></entity>
        <period><startDate>2019-01-01</startDate><endDate>2019-12-31</endDate></period></context>
      <context id=\"y2020\"><entity><identifier scheme=\"sec\">{1114700}</identifier></entity>
        <period><startDate>2020-01-01</startDate><endDate>2020-12-31</endDate></period></context>
      <context id=\"dimensioned\"><entity><identifier scheme=\"sec\">{1114700}</identifier>
        <segment><xbrldi:explicitMember dimension=\"ifrs:Axis\">ifrs:Member</xbrldi:explicitMember></segment></entity>
        <period><startDate>2020-01-01</startDate><endDate>2020-12-31</endDate></period></context>
      <unit id=\"Unit_ARS\"><measure>iso4217:ARS</measure></unit>
      <ifrs:RevenueAndOperatingIncome contextRef=\"y2019\" unitRef=\"Unit_ARS\">200474991000</ifrs:RevenueAndOperatingIncome>
      <ifrs:ProfitLoss contextRef=\"y2019\" unitRef=\"Unit_ARS\">32427485000</ifrs:ProfitLoss>
      <ifrs:RevenueAndOperatingIncome contextRef=\"y2020\" unitRef=\"Unit_ARS\">182710688000</ifrs:RevenueAndOperatingIncome>
      <ifrs:ProfitLoss contextRef=\"y2020\" unitRef=\"Unit_ARS\">25532780000</ifrs:ProfitLoss>
      <ifrs:ProfitLoss contextRef=\"dimensioned\" unitRef=\"Unit_ARS\">999</ifrs:ProfitLoss>
    </xbrl>""".encode()

    assert parse_annual_xbrl(xml) == EXPECTED_ANNUAL


def test_supplement_is_not_backdated_into_blocked_2019_signals() -> None:
    quarters, _, _ = derive_quarters()
    facts = build_facts(quarters, "2026-08-23")

    assert len(facts) == 16
    assert facts["available_date"].eq(AVAILABLE_DATE).all()
    assert set(facts["fiscal_end"]) == set(TARGET_FISCAL_ENDS)
    assert not (
        pd.to_datetime(facts["available_date"]) <= pd.Timestamp("2019-07-31")
    ).any()
    assert set(BLOCKED_SIGNALS) == {"2019-06-28", "2019-07-31"}
    assert all(not row["recoverable"] for row in BLOCKED_SIGNALS.values())
