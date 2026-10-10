import pytest

from scripts.research_v14_li_quarterly_pit import (
    EXPECTED_QUARTERS,
    SOURCES,
    audit_signals,
    parse_statement,
    validate_snapshots,
)


def _source(
    revenue: tuple[str, ...], net_income: tuple[str, ...], *, pro_forma: bool = False
) -> bytes:
    label = "Pro forma" if pro_forma else "For the Three Months Ended"
    revenue_cells = "".join(f"<td>{value}</td>" for value in revenue)
    income_cells = "".join(f"<td>{value}</td>" for value in net_income)
    return f"""
    <html><body>
      <p>Li Auto Inc.</p><p>Unaudited Condensed Consolidated Statements of Loss</p>
      <p>(All amounts in thousands)</p>
      <table>
        <tr><td>{label}</td></tr>
        <tr><td>Total revenues</td>{revenue_cells}</tr>
        <tr><td>Net loss</td>{income_cells}</tr>
        <tr><td>Non-GAAP net loss</td><td>999,999</td><td>999,999</td></tr>
      </table>
    </body></html>
    """.encode()


def test_parser_selects_exact_gaap_cny_thousands_and_rejects_pro_forma() -> None:
    pro_forma = _source(("9,000", "8,000"), ("7,000", "6,000"), pro_forma=True)
    actual = _source(("1,000", "2,000"), ("(3,000", "(4,000"))
    pro_forma_table = pro_forma[
        pro_forma.index(b"<table>") : pro_forma.index(b"</table>") + len(b"</table>")
    ]
    raw = actual.replace(b"<table>", pro_forma_table + b"<table>", 1)
    assert parse_statement(raw, 2, "net loss") == (
        (1_000_000.0, 2_000_000.0),
        (-3_000_000.0, -4_000_000.0),
    )
    with pytest.raises(RuntimeError, match="ambiguous"):
        parse_statement(pro_forma, 2, "net loss")


def test_validation_enforces_original_values_identities_and_comparatives() -> None:
    snapshots = {
        key: (spec["expected_revenue"], spec["expected_net_income"])
        for key, spec in SOURCES.items()
    }
    assert validate_snapshots(snapshots) == EXPECTED_QUARTERS

    changed = dict(snapshots)
    revenue, net_income = changed["2021q3"]
    changed["2021q3"] = (
        (revenue[0] + 1_000.0,) + revenue[1:],
        net_income,
    )
    with pytest.raises(RuntimeError, match="source values changed"):
        validate_snapshots(changed)


def test_signal_audit_recovers_all_four_observations_before_each_signal() -> None:
    audits = audit_signals(EXPECTED_QUARTERS)
    assert [row["signal_date"] for row in audits] == ["2021-11-30", "2021-12-31"]
    assert sum(row["missing_observation_count"] for row in audits) == 4
    for row in audits:
        assert row["quarter_window"] == list(EXPECTED_QUARTERS)
        assert row["last_available_financial_filing"]["filed"] == "2021-11-29"
        assert row["last_available_financial_filing"]["accession"] == (
            "0001104659-21-144060"
        )
        assert row["deterministic_result"] == (
            "EXCLUDE_EXACT_NEGATIVE_NET_INCOME_TTM"
        )


def test_exact_ttm_math_preserves_negative_income_exclusion() -> None:
    first = audit_signals(EXPECTED_QUARTERS)[0]
    assert first["previous_ttm"] == {
        "revenue": 5_594_079_000.0,
        "net_income": -985_284_000.0,
    }
    assert first["current_ttm"] == {
        "revenue": 20_536_224_000.0,
        "net_income": -509_419_000.0,
    }
    assert first["growth"]["revenue"] == pytest.approx(2.671064352148048)
    assert first["growth"]["net_income"] == pytest.approx(0.48297242216457387)
