import pandas as pd

from scripts.companyfacts_cache_snapshot import (
    create_companyfacts_cache_snapshot,
)
from scripts.companyfacts_formal_source_audit import (
    audit_companyfacts_formal_sources,
)
from src.io.fundamentals_update import (
    OUTPUT_COLUMNS,
    _write_companyfacts_cache,
    write_companyfacts_cache_manifest,
)


def _row(concept, value, *, dataset="annual"):
    return {
        "ticker": "EXMP",
        "fiscal_end": "2024-12-31",
        "available_date": "2025-02-01",
        "metric": "revenue",
        "value": str(value),
        "taxonomy": "us-gaap",
        "concept": concept,
        "form": "10-K" if dataset == "annual" else "10-Q",
        "accession": "0000000123-25-000001",
        "fetched_at": "2025-02-02",
    }


def test_formal_source_audit_separates_direct_derived_and_missing_rows(tmp_path):
    cache_dir = tmp_path / "cache"
    payload = {
        "cik": "0000000123",
        "entityName": "Example Corp",
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "end": "2024-12-31",
                                "filed": "2025-02-01",
                                "form": "10-K",
                                "accn": "0000000123-25-000001",
                                "val": 100,
                            }
                        ]
                    }
                }
            }
        },
    }
    _write_companyfacts_cache(
        "EXMP",
        123,
        payload,
        pd.Timestamp("2025-02-02T00:00:00Z"),
        cache_dir,
    )
    write_companyfacts_cache_manifest(cache_dir)
    snapshot = create_companyfacts_cache_snapshot(
        cache_dir=cache_dir,
        snapshot_root=tmp_path / "snapshots",
    )["snapshot_dir"]

    annual = tmp_path / "annual.csv"
    quarterly = tmp_path / "quarterly.csv"
    pd.DataFrame(
        [
            _row("Revenues", 100),
            _row("MissingConcept", 200),
            _row("derived_q4:Revenues", 25),
        ],
        columns=OUTPUT_COLUMNS,
    ).to_csv(annual, index=False)
    pd.DataFrame(columns=OUTPUT_COLUMNS).to_csv(quarterly, index=False)

    report = audit_companyfacts_formal_sources(
        snapshot,
        annual_output=annual,
        quarterly_output=quarterly,
    )

    summary = report["datasets"]["annual"]
    assert summary["formal_row_count"] == 3
    assert summary["direct_row_count"] == 2
    assert summary["direct_raw_match_count"] == 1
    assert summary["direct_raw_missing_count"] == 1
    assert summary["transformed_row_count"] == 1
    assert summary["direct_raw_match_coverage"] == 0.5
    assert report["direct_raw_missing_by_ticker"] == [
        {"ticker": "EXMP", "missing_rows": 1}
    ]
    assert report["missing_cik_ticker_count"] == 0
