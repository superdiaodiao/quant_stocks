from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.research_cache_catalog import create_catalog, verify_parts


def test_catalog_binds_parts_and_canonical_snapshot(tmp_path: Path) -> None:
    snapshot = tmp_path / "canonical"
    snapshot.mkdir()
    (snapshot / "manifest.json").write_text(json.dumps({
        "entry_count": 1,
        "entries": [{"bytes": 3}],
    }))
    (snapshot / "snapshot.json").write_text("{}")
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({
        "archive_item": "archive",
        "capture_timestamp": "20250414082852",
        "warc_url": "https://example.test/archive",
        "zip_sha256": "zip-sha",
    }))
    audit = tmp_path / "audit.json"
    audit.write_text(json.dumps({
        "safe_to_archive_only_candidate": True,
        "canonical_snapshot": str(snapshot.resolve()),
    }))
    part = tmp_path / "archive.part-000"
    part.write_bytes(b"part")

    catalog = create_catalog(
        snapshot=snapshot,
        archive_id="archive",
        archive_sha256="whole",
        archive_bytes=4,
        parts=[part],
        repository="owner/repo",
        release_tag="tag",
        source_evidence=evidence,
        variant_audit=audit,
    )

    assert catalog["snapshot"]["entry_count"] == 1
    assert catalog["source"]["extraction_evidence_asset"] == "evidence.json"
    assert catalog["source"]["variant_audit_asset"] == "audit.json"
    assert verify_parts(catalog, tmp_path)["verified"] is True
    part.write_bytes(b"changed")
    with pytest.raises(ValueError, match="verification failed"):
        verify_parts(catalog, tmp_path)
