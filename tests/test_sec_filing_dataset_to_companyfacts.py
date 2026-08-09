import json
import zipfile

import pandas as pd

from scripts.sec_filing_dataset_to_companyfacts import (
    _duration_start,
    build_companyfacts_candidate,
    convert_zip_archive,
)


def test_duration_start_is_explicitly_derived_from_qtrs():
    end = pd.Timestamp("2025-09-30")
    assert _duration_start(end, 0) is None
    assert _duration_start(end, 1) == pd.Timestamp("2025-07-01")
    assert _duration_start(end, 3) == pd.Timestamp("2025-01-01")


def test_build_candidate_preserves_accession_and_skips_segments():
    submissions = pd.DataFrame([
        {
            "adsh": "0000000001-25-000001",
            "cik": "715579",
            "name": "Example Bank",
            "form": "10-Q",
            "filed": "2025-11-06",
            "fy": "2025",
            "fp": "Q3",
        }
    ])
    numbers = pd.DataFrame([
        {
            "adsh": "0000000001-25-000001",
            "tag": "Assets",
            "version": "us-gaap/2025",
            "ddate": "20250930",
            "qtrs": "0",
            "uom": "USD",
            "value": "123.0",
            "segments": "",
        },
        {
            "adsh": "0000000001-25-000001",
            "tag": "Assets",
            "version": "us-gaap/2025",
            "ddate": "20250930",
            "qtrs": "0",
            "uom": "USD",
            "value": "4.0",
            "segments": "BusinessSegments=Banking",
        },
    ])
    payload, provenance = build_companyfacts_candidate(
        submissions,
        numbers,
        715579,
        source_archive="2025q4.zip",
        source_sha256="archive-sha",
    )
    row = payload["facts"]["us-gaap"]["Assets"]["units"]["USD"][0]
    assert row["accn"] == "0000000001-25-000001"
    assert row["end"] == "2025-09-30"
    assert row["val"] == 123
    assert row["_sec_filing_dataset_qtrs"] == 0
    assert provenance["skipped_rows"]["segmented"] == 1
    assert provenance["research_only"] is True


def test_convert_archive_checkpoints_present_ciks_and_records_missing(tmp_path):
    archive = tmp_path / "2025q4.zip"
    submissions = pd.DataFrame([
        {
            "adsh": "0000000001-25-000001",
            "cik": "715579",
            "name": "Example Bank",
            "form": "10-K",
            "filed": "20251106",
            "fy": "2025",
            "fp": "FY",
        }
    ])
    numbers = pd.DataFrame([
        {
            "adsh": "0000000001-25-000001",
            "tag": "Assets",
            "version": "us-gaap/2025",
            "ddate": "20250930",
            "qtrs": "0",
            "uom": "USD",
            "value": "123",
            "segments": "",
        }
    ])
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr("sub.txt", submissions.to_csv(sep="\t", index=False))
        target.writestr("num.txt", numbers.to_csv(sep="\t", index=False))

    output_dir = tmp_path / "candidate"
    result = convert_zip_archive(archive, [715579, 999999], output_dir)

    assert result["missing_ciks"] == [999999]
    assert [entry["cik"] for entry in result["entries"]] == [715579]
    assert (output_dir / "CIK0000715579.json").exists()
    assert not (output_dir / "CIK0000999999.json").exists()
    persisted = json.loads((output_dir / "provenance.json").read_text())
    assert persisted["archive_sha256"] == result["archive_sha256"]
