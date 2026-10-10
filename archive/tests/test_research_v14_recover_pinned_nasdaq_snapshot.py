import hashlib
import json

import pytest

from scripts import research_v14_recover_pinned_nasdaq_snapshot as recovery


def _payload() -> bytes:
    rows = [recovery.EXPECTED_HEADER]
    rows.extend(f"S{i}|Stock {i}|Q|N|N|100|N|N" for i in range(3))
    rows.append("File Creation Time: 0715201921:32|||||||")
    return ("\n".join(rows) + "\n").encode()


def test_verify_payload_locks_sha_header_date_and_rows():
    payload = _payload()
    result = recovery.verify_payload(
        payload,
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        observed_at="2019-07-15",
        minimum_rows=3,
    )
    assert result["data_rows"] == 3
    assert result["footer"].startswith("File Creation Time: 07152019")


def test_verify_payload_rejects_sha_or_observation_date_mismatch():
    payload = _payload()
    with pytest.raises(ValueError, match="SHA mismatch"):
        recovery.verify_payload(
            payload,
            expected_sha256="0" * 64,
            observed_at="2019-07-15",
            minimum_rows=3,
        )
    with pytest.raises(ValueError, match="observation date mismatch"):
        recovery.verify_payload(
            payload,
            expected_sha256=hashlib.sha256(payload).hexdigest(),
            observed_at="2019-07-16",
            minimum_rows=3,
        )


def test_recover_writes_research_only_manifest(tmp_path, monkeypatch):
    payload = _payload()
    payload_sha = hashlib.sha256(payload).hexdigest()
    snapshot_dir = tmp_path / "snapshots"

    monkeypatch.setattr(recovery, "_download", lambda source_url: payload)

    def fake_import(paths, minimum_rows, snapshot_dir):
        snapshot = snapshot_dir / "nasdaq_listed_2019-07-15.csv"
        snapshot_dir.mkdir(parents=True)
        snapshot.write_text("Symbol,Name\nS1,Stock 1\n", encoding="utf-8")
        return {
            "imported": [{
                "observed_at": "2019-07-15",
                "rows": 3,
                "source_file": paths[0],
                "snapshot": str(snapshot),
            }],
            "skipped": [],
        }

    monkeypatch.setattr(recovery, "import_nasdaq_trader_files", fake_import)
    result = recovery.recover(
        snapshot_dir=snapshot_dir,
        source_url="https://example.test/pinned.txt",
        expected_sha256=payload_sha,
        observed_at="2019-07-15",
        minimum_rows=3,
    )
    manifest = json.loads((snapshot_dir / "github_pinned_gap_recovery_manifest.json").read_text())
    assert result["formal_universe_modified"] is False
    assert manifest["release_status"] == "BLOCKED"
    assert manifest["source"]["payload_sha256"] == payload_sha
    assert manifest["snapshot"]["rows"] == 3
