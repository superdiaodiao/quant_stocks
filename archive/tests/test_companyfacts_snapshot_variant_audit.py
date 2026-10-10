from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.companyfacts_snapshot_variant_audit import audit_variants


def _snapshot(root: Path, symbols: list[str], payload_value: int) -> Path:
    root.mkdir()
    body = {
        "cik": 1,
        "fetched_at": "2025-04-14T00:00:00Z",
        "source_url": "https://example.test/CIK1.json",
        "symbols": symbols,
        "payload": {"value": payload_value},
    }
    raw = json.dumps(body, separators=(",", ":")).encode()
    (root / "CIK0000000001.json").write_bytes(raw)
    manifest = {
        "entry_count": 1,
        "entries": [{
            "cik": 1,
            "path": "CIK0000000001.json",
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }],
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    return root


def test_symbol_only_variant_is_safe_to_deduplicate(tmp_path: Path) -> None:
    base = _snapshot(tmp_path / "base", [], 1)
    candidate = _snapshot(tmp_path / "candidate", ["TEST"], 1)

    report = audit_variants(base, candidate)

    assert report["safe_to_archive_only_candidate"] is True
    assert report["symbol_only_difference_count"] == 1
    assert report["semantic_difference_count"] == 0


def test_payload_change_is_not_safe_to_deduplicate(tmp_path: Path) -> None:
    base = _snapshot(tmp_path / "base", [], 1)
    candidate = _snapshot(tmp_path / "candidate", ["TEST"], 2)

    report = audit_variants(base, candidate)

    assert report["safe_to_archive_only_candidate"] is False
    assert report["semantic_difference_count"] == 1
