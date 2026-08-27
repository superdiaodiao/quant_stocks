from decimal import Decimal

import pandas as pd
import pytest

from scripts.research_v14_ggal_ias29_quarters import (
    AVAILABLE_DATE,
    BLOCKED_AUDIT_OBSERVATIONS,
    BLOCKED_SIGNALS,
    BLOCKED_TEXT_CHECKS,
    EXPECTED_ANNUAL,
    EXPECTED_REPORTS,
    SOURCES,
    TARGET_CPI,
    TARGET_FISCAL_ENDS,
    build_facts,
    blocked_rejected_derivations,
    derive_quarters,
    parse_annual_xbrl,
    parse_report,
    resolve_blocked_observations,
    validate_audit_binding,
    validate_blocked_source_text,
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


def test_blocked_sources_lock_annual_basis_and_late_q2_report() -> None:
    expected = {
        "blocked_2018_20f": (
            "2019-05-15",
            "0001193125-19-146966",
            "9d7bf111f3fea13dc0a6d5635646c34fe697b2faa97aa5e224b3aa01cdb78331",
        ),
        "blocked_2019_q1_report": (
            "2019-05-16",
            "0001193125-19-149579",
            "5d065ea10e730deaa4ce44e97c047fe15a8c6ac9d20a354f12af1474b95a50b0",
        ),
        "blocked_2019_q2_report": (
            "2019-08-14",
            "0001193125-19-220829",
            "948d3c38072ceef454db01c09e53837bd74be2af51f0cfe4f018ac9d9d046c47",
        ),
    }
    assert {
        name: (source["filed"], source["accession"], source["sha256"])
        for name, source in SOURCES.items()
        if name in expected
    } == expected


def test_blocked_disclosures_are_required_verbatim() -> None:
    raw_by_source = {
        name: (
            "<html><body>" + " ".join(fragments) + "</body></html>"
        ).encode()
        for name, fragments in BLOCKED_TEXT_CHECKS.items()
    }
    assert len(validate_blocked_source_text(raw_by_source)) == 7
    raw_by_source["blocked_2019_q1_report"] = b"<html>changed</html>"
    with pytest.raises(RuntimeError, match="blocked-source disclosure changed"):
        validate_blocked_source_text(raw_by_source)


def test_all_twelve_2019_observations_are_explicit_unrecoverable() -> None:
    assert len(BLOCKED_AUDIT_OBSERVATIONS) == 12
    observations = resolve_blocked_observations()
    assert len(observations) == 12
    assert observations.groupby("scenario").size().eq(2).all()
    assert not observations["resolved"].any()
    assert set(observations["decision"]) == {
        "unrecoverable_ias29_measurement_basis_conflict"
    }
    assert all(item["rejected"] for item in blocked_rejected_derivations())


def test_current_audit_binding_covers_exact_twelve_observations() -> None:
    from scripts import research_v14_ggal_ias29_quarters as ggal

    binding = validate_audit_binding(
        ggal.AUDIT_PATH,
        ggal.EXPECTED_AUDIT_SHA256,
    )
    assert binding["scenario_count"] == 6
    assert binding["missing_observation_count"] == 12
    assert binding["signals"] == ["2019-06-28", "2019-07-31"]
    SOURCES,
    blocked_rejected_derivations,
