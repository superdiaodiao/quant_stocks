import gzip
import hashlib
import json

from scripts.sec_filing_dataset_supplement_impact import (
    _parse_quarterly_with_targeted_sec_facts,
    audit_supplement_impact,
)


def _payload(include_new=False):
    rows = [{
        "start": "2019-01-01",
        "end": "2019-12-31",
        "val": 10,
        "accn": "a",
        "form": "10-K",
        "filed": "2020-02-01",
        "fy": 2019,
        "fp": "FY",
    }]
    if include_new:
        rows.append({
            "start": "2020-01-01",
            "end": "2020-12-31",
            "val": 20,
            "accn": "b",
            "form": "10-K",
            "filed": "2021-02-01",
            "fy": 2020,
            "fp": "FY",
        })
    return {
        "cik": 1,
        "facts": {
            "us-gaap": {
                "NetIncomeLoss": {"units": {"USD": rows}},
            }
        },
    }


def test_impact_report_replays_base_and_supplemented_payloads(tmp_path):
    base_path = tmp_path / "base.json.gz"
    base_path.write_bytes(gzip.compress(json.dumps({"payload": _payload()}).encode(), mtime=0))
    supplemented_dir = tmp_path / "supplemented"
    supplemented_dir.mkdir()
    merged_path = supplemented_dir / "CIK0000000001.json.gz"
    merged_payload = _payload(include_new=True)
    merged_path.write_bytes(gzip.compress(json.dumps({"payload": merged_payload}).encode(), mtime=0))
    provenance = {
        "entries": [{
            "cik": 1,
            "symbols": ["EXM"],
            "source_snapshot": str(base_path),
            "output_path": str(merged_path),
            "merged_payload_sha256": hashlib.sha256(b"merged").hexdigest(),
        }]
    }
    (supplemented_dir / "provenance.json").write_text(json.dumps(provenance))

    report = audit_supplement_impact(
        supplemented_dir, tmp_path / "impact.json"
    )

    annual = report["entries"][0]["datasets"]["annual"]
    assert annual["added_row_count"] == 1
    assert annual["removed_row_count"] == 0
    assert annual["added_coordinate_value_count"] == 1
    assert annual["removed_coordinate_value_count"] == 0
    assert annual["added_rows"][0]["fiscal_end"] == "2020-12-31"
    assert report["formal_financial_files_modified"] is False


def test_research_parser_adds_only_explicitly_targeted_sec_fact():
    payload = _payload()
    row = payload["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"][0]
    row.update({
        "start": "2019-10-01",
        "_sec_filing_dataset_qtrs": 1,
        "_sec_filing_dataset_target_metric": "net_income",
    })

    parsed = _parse_quarterly_with_targeted_sec_facts(
        "EXM", payload, "2026-08-01"
    )

    assert len(parsed) == 1
    assert parsed.iloc[0]["metric"] == "net_income"
    assert parsed.iloc[0]["value"] == 10
