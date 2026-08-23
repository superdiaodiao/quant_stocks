import pytest

from scripts.research_v14_mmyt_quarterly_pit import (
    EXPECTED_QUARTERS,
    SOURCES,
    audit_signals,
    parse_statement,
    validate_snapshots,
)


def _source(revenue: tuple[str, ...], net_income: tuple[str, ...]) -> bytes:
    revenue_cells = "".join(f"<td>{value}</td>" for value in revenue)
    income_cells = "".join(f"<td>{value}</td>" for value in net_income)
    return f"""
    <html><body>
      <p>MakeMyTrip Limited</p><p>Financial Summary as per IFRS</p>
      <p>(Amounts in USD thousands)</p>
      <table>
        <tr><td>For the three months ended</td></tr>
        <tr><td>Revenue</td><td>999,999</td><td>999,999</td></tr>
        <tr><td>Total revenue</td>{revenue_cells}</tr>
        <tr><td>Loss for the period</td>{income_cells}</tr>
        <tr><td>Loss attributable to owners</td><td>(9,999)</td></tr>
      </table>
      <table><tr><td>Adjusted Revenue (Non-IFRS)</td><td>8,888</td></tr></table>
    </body></html>
    """.encode()


def test_parser_selects_total_ifrs_usd_quarter_not_adjusted_metrics() -> None:
    raw = _source(("1,000", "2,000"), ("(3,000", "(4,000"))
    assert parse_statement(raw, 2) == (
        (1_000_000.0, 2_000_000.0),
        (-3_000_000.0, -4_000_000.0),
    )
    with pytest.raises(RuntimeError, match="currency marker"):
        parse_statement(raw.replace(b"USD", b"EUR").replace(b"$", b""), 2)


def test_validation_enforces_original_values_identities_and_comparatives() -> None:
    snapshots = {
        key: (spec["expected_revenue"], spec["expected_net_income"])
        for key, spec in SOURCES.items()
    }
    assert validate_snapshots(snapshots) == EXPECTED_QUARTERS

    changed = dict(snapshots)
    revenue, net_income = changed["2020-12-31"]
    changed["2020-12-31"] = (
        (revenue[0] + 1_000.0,) + revenue[1:],
        net_income,
    )
    with pytest.raises(RuntimeError, match="source values changed"):
        validate_snapshots(changed)


def test_signal_audit_recovers_exact_three_observations_before_signals() -> None:
    audits = audit_signals(EXPECTED_QUARTERS)
    assert [row["signal_date"] for row in audits] == ["2021-01-29", "2021-02-26"]
    assert [row["missing_observation_count"] for row in audits] == [1, 2]
    for row in audits:
        assert row["quarter_window"] == list(EXPECTED_QUARTERS)
        assert row["last_available_financial_filing"]["filed"] == "2021-01-28"
        assert row["last_available_financial_filing"]["accession"] == (
            "0001564590-21-002744"
        )
        assert row["deterministic_result"] == (
            "EXCLUDE_EXACT_NEGATIVE_NET_INCOME_TTM"
        )


def test_exact_ttm_math_preserves_negative_income_exclusion() -> None:
    first = audit_signals(EXPECTED_QUARTERS)[0]
    assert first["previous_ttm"] == {
        "revenue": 526_760_000.0,
        "net_income": -149_299_000.0,
    }
    assert first["current_ttm"] == {
        "revenue": 189_165_000.0,
        "net_income": -397_854_000.0,
    }
    assert first["growth"]["revenue"] == pytest.approx(-0.6408895891867264)
    assert first["growth"]["net_income"] == pytest.approx(-1.6648135620466313)
