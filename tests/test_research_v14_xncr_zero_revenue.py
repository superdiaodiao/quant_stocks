from __future__ import annotations

import pytest

from scripts.research_v14_xncr_zero_revenue import (
    q4_revenue_fact,
    require_explicit_zero_revenue,
)


def test_explicit_zero_revenue_requires_table_and_explanation() -> None:
    html = b"<p>Total revenues $ -- $ 3.5 $ (3.5)</p><p>Revenues were lower by $3.5 million</p>"
    # SEC HTML renders the em dash as a Unicode character.
    html = html.replace(b"--", "\u2014".encode())
    assert require_explicit_zero_revenue(html, "3.5") == 0.0
    with pytest.raises(RuntimeError, match="changed or is not unique"):
        require_explicit_zero_revenue(html.replace(b"lower", b"higher"), "3.5")


def test_q4_revenue_fact_requires_exact_duration_evidence() -> None:
    row = {
        "start": "2018-10-01", "end": "2018-12-31", "val": 11_564_000,
        "filed": "2019-02-26", "accn": "0001558370-19-001049",
        "form": "10-K",
    }
    payload = {"facts": {"us-gaap": {
        "RevenueFromContractWithCustomerExcludingAssessedTax": {
            "units": {"USD": [row]}
        }
    }}}
    assert q4_revenue_fact(payload) == row
    payload["facts"]["us-gaap"][
        "RevenueFromContractWithCustomerExcludingAssessedTax"
    ]["units"]["USD"].append(dict(row))
    with pytest.raises(RuntimeError, match="evidence changed"):
        q4_revenue_fact(payload)
