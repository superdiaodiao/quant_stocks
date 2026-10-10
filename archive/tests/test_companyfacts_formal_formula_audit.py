import pandas as pd

from scripts.companyfacts_formal_formula_audit import (
    _derived_ytd_value,
    _explicit_quarter_operand,
    _q4_value,
    _record_result,
)


def _row(**overrides):
    value = {
        "ticker": "ABC",
        "fiscal_end_s": "2024-06-30",
        "available_s": "2024-08-01",
        "metric": "revenue",
        "value_num": 40.0,
        "taxonomy": "us-gaap",
        "concept": "derived_ytd:Revenue",
        "form": "10-Q",
        "accession": "acc-current",
        "dataset": "quarterly",
    }
    value.update(overrides)
    return value


def test_derived_ytd_formula_uses_same_start_and_latest_prior_period():
    current = {
        "taxonomy": "us-gaap", "concept": "Revenue", "start": "2024-01-01",
        "end": "2024-06-30", "filed": "2024-08-01", "value": 100.0,
        "form": "10-Q", "accession": "acc-current", "unit": "USD",
    }
    previous = {**current, "end": "2024-03-31", "filed": "2024-05-01", "value": 60.0,
                "accession": "acc-prior"}
    exact = {(
        "us-gaap", "Revenue", "2024-06-30", "2024-08-01", "10-Q", "acc-current",
    ): [current]}
    any_taxonomy = {}
    ytd = {("us-gaap", "Revenue", "2024-01-01", "USD"): [previous, current]}

    result = _derived_ytd_value(_row(), "Revenue", exact, any_taxonomy, ytd)

    assert result is not None
    assert result[0] == 40.0
    assert [operand["accession"] for operand in result[1]] == [
        "acc-current", "acc-prior"
    ]


def test_q4_formula_selects_three_latest_eligible_quarters():
    row = _row(
        fiscal_end_s="2024-12-31", available_s="2025-02-15",
        metric="net_income", concept="derived_q4:NetIncomeLoss", value_num=10.0,
        form="10-K", accession="acc-fy",
    )
    annual = {
        "taxonomy": "us-gaap", "concept": "NetIncomeLoss", "end": "2024-12-31",
        "filed": "2025-02-15", "value": 100.0, "form": "10-K", "accession": "acc-fy",
        "unit": "USD",
    }
    exact = {(
        "us-gaap", "NetIncomeLoss", "2024-12-31", "2025-02-15", "10-K", "acc-fy",
    ): [annual]}
    quarters = pd.DataFrame([
        {**row, "fiscal_end_s": end, "available_s": filed, "value_num": value,
         "concept": "NetIncomeLoss"}
        for end, filed, value in [
            ("2024-03-31", "2024-05-01", 20.0),
            ("2024-06-30", "2024-08-01", 30.0),
            ("2024-09-30", "2024-11-01", 40.0),
            ("2024-10-01", "2024-11-01", 40.0),
        ]
    ]).to_dict("records")
    groups = {("ABC", "net_income"): quarters}

    result = _q4_value(row, "NetIncomeLoss", groups, exact, {})

    assert result is not None
    assert result[0] == 10.0
    assert len(result[1]) == 4


def test_record_result_distinguishes_unresolved_from_value_mismatch():
    row = _row(value_num=41.0)
    mismatch = _record_result(row, "derived_ytd", 40.0)
    unresolved = _record_result(row, "derived_ytd", None, "ytd_operands_unresolved")

    assert mismatch["matched"] is False
    assert mismatch["reason"] == "formula_value_mismatch"
    assert unresolved["matched"] is False
    assert unresolved["reason"] == "ytd_operands_unresolved"


def test_bank_operand_ignores_ytd_fact_before_explicit_quarter() -> None:
    candidates = [
        {
            "start": "2023-01-01",
            "end": "2023-06-30",
            "value": 2_811_000_000.0,
        },
        {
            "start": "2023-04-01",
            "end": "2023-06-30",
            "value": 1_961_000_000.0,
        },
    ]

    selected = _explicit_quarter_operand(candidates)

    assert selected is candidates[1]


def test_bank_operand_accepts_full_year_for_annual_dataset() -> None:
    candidate = {
        "start": "2022-01-01",
        "end": "2022-12-31",
        "value": 4_000_000_000.0,
    }

    selected = _explicit_quarter_operand([candidate], dataset="annual")

    assert selected is candidate
