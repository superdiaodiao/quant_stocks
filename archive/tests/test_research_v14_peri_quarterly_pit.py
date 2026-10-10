import pytest

from scripts.research_v14_peri_quarterly_pit import (
    EXPECTED_ANNUALS,
    EXPECTED_CUMULATIVE,
    EXPECTED_QUARTERS,
    audit_signals,
    parse_period,
    validate_quarters,
)


def _table(period: str, date: str, year: str, revenue: str, income: str) -> bytes:
    return f"""
    <html><body>
      <p>PERION NETWORK LTD. AND ITS SUBSIDIARIES</p><p>In thousands</p>
      <table>
        <tr><td></td><td>{period}</td><td>{period}</td></tr>
        <tr><td></td><td>{date}</td><td>{date}</td></tr>
        <tr><td></td><td>{year}</td><td>{year}</td></tr>
        <tr><td>Advertising</td><td>$</td><td>1,000</td></tr>
        <tr><td>Total Revenues</td><td></td><td>{revenue}</td></tr>
        <tr><td>Net Income (Loss)</td><td>$</td><td>{income}</td></tr>
        <tr><td>Adjusted Net Income</td><td>$</td><td>999,999</td></tr>
        <tr><td>Net Income (Loss)</td><td>$</td><td>0.50</td></tr>
      </table>
    </body></html>
    """.encode()


def test_parser_selects_exact_gaap_usd_period_not_non_gaap_or_eps() -> None:
    raw = _table(
        "Three months ended", "June 30,", "2020", "60,341", "(2,239)"
    )
    assert parse_period(raw, "2020-06-30", "three months ended") == (
        60_341_000.0,
        -2_239_000.0,
    )
    with pytest.raises(RuntimeError, match="ambiguous"):
        parse_period(raw, "2020-06-30", "six months ended")


def test_validation_requires_annual_h1_and_nine_month_identities() -> None:
    validate_quarters(EXPECTED_QUARTERS, EXPECTED_ANNUALS, EXPECTED_CUMULATIVE)
    changed = dict(EXPECTED_CUMULATIVE)
    changed["2020_h1"] = (126_395_000.0, -905_000.0)
    with pytest.raises(RuntimeError, match="cumulative periods changed"):
        validate_quarters(EXPECTED_QUARTERS, EXPECTED_ANNUALS, changed)


def test_signal_audit_uses_different_last_available_filings() -> None:
    audits = audit_signals(EXPECTED_QUARTERS)
    first, second = audits
    assert first["signal_date"] == "2021-01-29"
    assert first["missing_observation_count"] == 2
    assert first["quarter_window"][-1] == "2020-09-30"
    assert first["last_available_financial_filing"]["filed"] == "2020-10-28"
    assert first["deterministic_result"] == "FAIL_NET_INCOME_GROWTH"
    assert second["signal_date"] == "2021-10-29"
    assert second["missing_observation_count"] == 3
    assert second["quarter_window"][-1] == "2021-09-30"
    assert second["last_available_financial_filing"]["filed"] == "2021-10-26"
    assert second["deterministic_result"].startswith("PASS_")


def test_ttm_growth_is_exact_for_each_signal() -> None:
    first, second = audit_signals(EXPECTED_QUARTERS)
    assert first["previous_ttm"] == {
        "revenue": 255_155_000.0,
        "net_income": 11_893_000.0,
    }
    assert first["current_ttm"] == {
        "revenue": 288_064_000.0,
        "net_income": 7_110_000.0,
    }
    assert first["growth"]["revenue"] == pytest.approx(0.12897650447767042)
    assert first["growth"]["net_income"] == pytest.approx(-0.4021693433111915)
    assert second["previous_ttm"] == first["current_ttm"]
    assert second["current_ttm"] == {
        "revenue": 438_779_000.0,
        "net_income": 30_013_000.0,
    }
    assert second["growth"]["revenue"] == pytest.approx(0.5231997056209731)
    assert second["growth"]["net_income"] == pytest.approx(3.2212376933895923)
