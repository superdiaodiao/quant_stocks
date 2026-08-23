import pytest

from scripts.research_v14_iclk_quarterly_pit import (
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
      <p>iClick Interactive Asia Group Limited</p><p>(US$ in thousands)</p>
      <p>Unaudited Reconciliations of GAAP and Non-GAAP Results</p>
      <table>
        <tr><td>Three Months Ended</td></tr>
        <tr><td>Revenue</td>{revenue_cells}</tr>
        <tr><td>Net loss</td><td>(9,999)</td><td>(9,999)</td></tr>
        <tr><td>Net loss attributable to iClick Interactive Asia Group Limited's ordinary shareholders</td>{income_cells}</tr>
        <tr><td>Adjusted net loss</td><td>(8,888)</td><td>(8,888)</td></tr>
      </table>
    </body></html>
    """.encode()


def test_parser_selects_parent_attributable_gaap_usd_values() -> None:
    raw = _source(("1,000", "2,000"), ("(3,000", "400"))
    assert parse_statement(
        raw,
        2,
        "revenue",
        (
            "net loss attributable to iclick interactive asia group limited's "
            "ordinary shareholders"
        ),
    ) == ((1_000_000.0, 2_000_000.0), (-3_000_000.0, 400_000.0))
    with pytest.raises(RuntimeError, match="currency marker"):
        parse_statement(
            raw.replace(b"US$", b"EUR"),
            2,
            "revenue",
            (
                "net loss attributable to iclick interactive asia group limited's "
                "ordinary shareholders"
            ),
        )


def test_validation_enforces_ownership_identities_and_comparatives() -> None:
    snapshots = {
        key: (spec["expected_revenue"], spec["expected_net_income"])
        for key, spec in SOURCES.items()
    }
    assert validate_snapshots(snapshots) == EXPECTED_QUARTERS

    changed = dict(snapshots)
    revenue, net_income = changed["2020-09-30"]
    changed["2020-09-30"] = (
        revenue,
        (net_income[0], net_income[1] + 1_000.0) + net_income[2:],
    )
    with pytest.raises(RuntimeError, match="source values changed"):
        validate_snapshots(changed)


def test_signal_audit_recovers_exact_two_observations_before_signals() -> None:
    audits = audit_signals(EXPECTED_QUARTERS)
    assert [row["signal_date"] for row in audits] == ["2020-12-31", "2021-01-29"]
    assert sum(row["missing_observation_count"] for row in audits) == 2
    for row in audits:
        assert row["quarter_window"] == list(EXPECTED_QUARTERS)
        assert row["last_available_financial_filing"]["filed"] == "2020-11-24"
        assert row["last_available_financial_filing"]["accession"] == (
            "0001564590-20-055057"
        )
        assert row["deterministic_result"] == (
            "EXCLUDE_EXACT_NEGATIVE_NET_INCOME_TTM"
        )


def test_exact_ttm_math_preserves_negative_income_exclusion() -> None:
    first = audit_signals(EXPECTED_QUARTERS)[0]
    assert first["previous_ttm"] == {
        "revenue": 182_237_000.0,
        "net_income": -11_800_000.0,
    }
    assert first["current_ttm"] == {
        "revenue": 232_728_000.0,
        "net_income": -19_644_000.0,
    }
    assert first["growth"]["revenue"] == pytest.approx(0.27706228702184515)
    assert first["growth"]["net_income"] == pytest.approx(-0.6647457627118644)
