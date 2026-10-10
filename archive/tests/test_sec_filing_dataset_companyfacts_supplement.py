import gzip
import json
import zipfile

import pandas as pd

from scripts.sec_filing_dataset_companyfacts_supplement import (
    _align_candidate_ends_to_snapshot,
    _merge_fact_payload,
    _filter_candidate_to_targets,
    _target_identity,
    build_supplemented_snapshots,
)


def _fact(value, accession="a"):
    return {
        "end": "2020-12-31",
        "val": value,
        "accn": accession,
        "form": "10-K",
        "filed": "2021-03-01",
        "fy": 2020,
        "fp": "FY",
    }


def _payload(rows):
    return {
        "cik": 1,
        "entityName": "Example",
        "facts": {"us-gaap": {"Assets": {"units": {"USD": rows}}}},
    }


def test_merge_appends_only_exactly_missing_facts():
    merged, added = _merge_fact_payload(
        _payload([_fact(10)]),
        _payload([_fact(10), _fact(20, accession="b")]),
    )
    rows = merged["facts"]["us-gaap"]["Assets"]["units"]["USD"]
    assert added == 1
    assert [row["val"] for row in rows] == [10, 20]


def test_aligns_standardized_dataset_end_to_same_accession_snapshot_end():
    anchor = _fact(10)
    anchor.update({"start": "2019-12-29", "end": "2020-12-26"})
    candidate = _fact(20)
    candidate.update({
        "start": "2020-01-01",
        "end": "2020-12-31",
        "_sec_filing_dataset_qtrs": 4,
        "_sec_filing_dataset_duration_start_derived": True,
    })
    aligned, count = _align_candidate_ends_to_snapshot(
        _payload([anchor]), _payload([candidate])
    )
    row = aligned["facts"]["us-gaap"]["Assets"]["units"]["USD"][0]
    assert count == 1
    assert row["_sec_filing_dataset_original_end"] == "2020-12-31"
    assert row["end"] == "2020-12-26"
    assert row["start"] == "2019-12-27"


def test_target_filter_keeps_only_exact_direct_q4_proof():
    direct = _fact(25_516_000, accession="direct")
    direct.update({
        "start": "2019-11-01",
        "end": "2020-01-31",
        "filed": "2020-03-23",
        "_sec_filing_dataset_qtrs": 1,
    })
    unrelated = _fact(99, accession="other")
    unrelated["_sec_filing_dataset_qtrs"] = 1
    candidate = {
        "cik": 1,
        "facts": {"us-gaap": {
            "NetIncomeLoss": {"units": {"USD": [direct, unrelated]}}
        }},
    }
    target = {
        "ticker": "LE",
        "fiscal_end": "2020-01-31",
        "available_date": "2020-03-23",
        "metric": "net_income",
        "value": 25_516_000,
        "accession": "direct",
    }

    filtered, matched = _filter_candidate_to_targets(
        candidate, symbol="LE", targets=[target]
    )

    rows = filtered["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"]
    assert len(rows) == 1
    assert rows[0]["val"] == direct["val"]
    assert rows[0]["_sec_filing_dataset_target_metric"] == "net_income"
    assert matched == {_target_identity(target)}


def test_build_supplement_binds_snapshot_archive_and_output_hashes(tmp_path):
    snapshot_dir = tmp_path / "snapshot"
    snapshot_dir.mkdir()
    envelope = {"symbols": ["EXM"], "payload": _payload([_fact(10)])}
    snapshot_path = snapshot_dir / "CIK0000000001.json.gz"
    snapshot_path.write_bytes(
        gzip.compress(json.dumps(envelope).encode(), mtime=0)
    )

    archive = tmp_path / "2021q1.zip"
    submissions = pd.DataFrame([{
        "adsh": "b",
        "cik": "1",
        "name": "Example",
        "form": "10-K",
        "filed": "20210301",
        "fy": "2020",
        "fp": "FY",
    }])
    numbers = pd.DataFrame([{
        "adsh": "b",
        "tag": "Assets",
        "version": "us-gaap/2020",
        "ddate": "20201231",
        "qtrs": "0",
        "uom": "USD",
        "value": "20",
        "segments": "",
    }])
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr("sub.txt", submissions.to_csv(sep="\t", index=False))
        target.writestr("num.txt", numbers.to_csv(sep="\t", index=False))

    output_dir = tmp_path / "supplemented"
    result = build_supplemented_snapshots(
        snapshot_dir, [archive], [1], output_dir
    )

    entry = result["entries"][0]
    assert result["research_only"] is True
    assert result["active_cache_modified"] is False
    assert result["formal_financial_files_modified"] is False
    assert entry["facts_added"] == 1
    assert len(entry["source_snapshot_file_sha256"]) == 64
    assert len(result["archives"][0]["sha256"]) == 64
    assert len(entry["output_file_sha256"]) == 64
    output = json.loads(gzip.decompress(
        (output_dir / "CIK0000000001.json.gz").read_bytes()
    ))
    assert output["active_cache_input"] is False
    rows = output["payload"]["facts"]["us-gaap"]["Assets"]["units"]["USD"]
    assert [row["val"] for row in rows] == [10, 20]
