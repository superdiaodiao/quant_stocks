import pandas as pd
import pytest

from scripts.research_v14_sbgi_predecessor_quarters import (
    ORIGINAL_ACCESSIONS,
    select_original_rows,
    validate_transition_filing,
)


def _parsed_rows() -> pd.DataFrame:
    rows = []
    for fiscal_end, accession in ORIGINAL_ACCESSIONS.items():
        available = pd.Timestamp(fiscal_end) + pd.Timedelta(days=60)
        for metric, value in (("revenue", 10.0), ("net_income", 2.0)):
            rows.append({
                "ticker": "SBGI",
                "fiscal_end": fiscal_end,
                "available_date": available,
                "metric": metric,
                "value": value,
                "taxonomy": "us-gaap",
                "concept": "Revenues" if metric == "revenue" else "ProfitLoss",
                "form": "10-Q",
                "accession": accession,
                "fetched_at": "2026-08-13",
            })
        rows.append({**rows[-2], "accession": "later-comparative"})
    return pd.DataFrame(rows)


def test_select_original_rows_excludes_later_comparatives() -> None:
    selected = select_original_rows(_parsed_rows())
    assert len(selected) == 48
    assert selected["fiscal_end"].nunique() == 24
    assert "later-comparative" not in set(selected["accession"])


def test_select_original_rows_requires_paired_metrics() -> None:
    rows = _parsed_rows()
    first_accession = next(iter(ORIGINAL_ACCESSIONS.values()))
    rows = rows.loc[
        ~(
            rows["accession"].eq(first_accession)
            & rows["metric"].eq("net_income")
        )
    ]
    with pytest.raises(ValueError, match="exact revenue/net-income pair"):
        select_original_rows(rows)


def test_transition_filing_requires_holding_company_continuity() -> None:
    proven = b"""
      company formerly known as Sinclair Broadcast Group, Inc.
      holding company reorganization
      New Sinclair would become the publicly-traded parent company of SBG
      Effective at 12:00 am Eastern U.S. time on June 1, 2023
      exchanged on a one-for-one basis
    """
    assert "holding company" in validate_transition_filing(proven).lower()
    with pytest.raises(ValueError, match="does not prove successor continuity"):
        validate_transition_filing(b"Sinclair")
