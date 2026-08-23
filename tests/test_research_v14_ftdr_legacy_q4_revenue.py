from __future__ import annotations

import pytest

from scripts.research_v14_ftdr_legacy_q4_revenue import (
    ACCESSION,
    AVAILABLE_DATE,
    CONCEPT,
    EXPECTED,
    derived_q4_revenue,
)


def _payload() -> dict:
    rows = []
    values = {
        2017: (899_000_000, 1_157_000_000),
        2018: (979_000_000, 1_258_000_000),
    }
    for year, (nine_months, annual) in values.items():
        rows.extend(
            [
                {
                    "start": f"{year}-01-01",
                    "end": f"{year}-09-30",
                    "val": nine_months,
                    "filed": "2018-11-06",
                    "form": "10-Q",
                    "accn": "0001727263-18-000018",
                },
                {
                    "start": f"{year}-01-01",
                    "end": f"{year}-12-31",
                    "val": annual,
                    "filed": AVAILABLE_DATE,
                    "form": "10-K",
                    "accn": ACCESSION,
                },
                {
                    "start": f"{year}-01-01",
                    "end": f"{year}-12-31",
                    "val": annual,
                    "filed": "2020-02-28",
                    "form": "10-K",
                    "accn": "0001562762-20-000077",
                },
            ]
        )
    return {
        "facts": {"us-gaap": {CONCEPT: {"units": {"USD": rows}}}}
    }


def test_derived_q4_revenue_uses_only_contemporaneous_exact_facts() -> None:
    rows, bindings = derived_q4_revenue(_payload())
    assert {int(row["fiscal_end"][:4]): row["value"] for row in rows} == EXPECTED
    assert len(rows) == len(bindings) == 2
    assert {row["available_date"] for row in rows} == {AVAILABLE_DATE}
    assert {row["accession"] for row in rows} == {ACCESSION}
    assert all(row["metric"] == "revenue" for row in rows)


def test_derived_q4_revenue_rejects_duplicate_duration_fact() -> None:
    payload = _payload()
    rows = payload["facts"]["us-gaap"][CONCEPT]["units"]["USD"]
    rows.append(dict(rows[0]))
    with pytest.raises(RuntimeError, match="not unique"):
        derived_q4_revenue(payload)
