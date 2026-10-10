from __future__ import annotations

import pytest

from scripts.research_v14_zi_2019_q4_revenue import (
    ANNUAL_ACCESSION,
    AVAILABLE_DATE,
    CONCEPT,
    EXPECTED,
    NINE_MONTH_ACCESSION,
    derived_q4_revenue,
)


def _payload() -> dict:
    rows = []
    values = {
        2019: (202_200_000, 293_300_000),
        2020: (336_500_000, 476_200_000),
    }
    for year, (nine_months, annual) in values.items():
        rows.extend(
            [
                {
                    "start": f"{year}-01-01",
                    "end": f"{year}-09-30",
                    "val": nine_months,
                    "filed": "2020-11-13",
                    "form": "10-Q",
                    "accn": NINE_MONTH_ACCESSION,
                },
                {
                    "start": f"{year}-01-01",
                    "end": f"{year}-12-31",
                    "val": annual,
                    "filed": AVAILABLE_DATE,
                    "form": "10-K",
                    "accn": ANNUAL_ACCESSION,
                },
                {
                    "start": f"{year}-01-01",
                    "end": f"{year}-12-31",
                    "val": annual,
                    "filed": "2022-02-24",
                    "form": "10-K",
                    "accn": "0001794515-22-000015",
                },
            ]
        )
    return {
        "facts": {"us-gaap": {CONCEPT: {"units": {"USD": rows}}}}
    }


def test_derived_q4_revenue_uses_only_exact_contemporaneous_facts() -> None:
    rows, bindings = derived_q4_revenue(_payload())
    assert {int(row["fiscal_end"][:4]): row["value"] for row in rows} == EXPECTED
    assert len(rows) == len(bindings) == 2
    assert {row["available_date"] for row in rows} == {AVAILABLE_DATE}
    assert {row["metric"] for row in rows} == {"revenue"}
    assert all(
        binding["source_accessions"]
        == [NINE_MONTH_ACCESSION, ANNUAL_ACCESSION]
        for binding in bindings
    )


def test_derived_q4_revenue_rejects_duplicate_duration_fact() -> None:
    payload = _payload()
    rows = payload["facts"]["us-gaap"][CONCEPT]["units"]["USD"]
    rows.append(dict(rows[0]))
    with pytest.raises(RuntimeError, match="not unique"):
        derived_q4_revenue(payload)
