"""Create and verify a machine-readable catalog for split research archives."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", dir=path.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.chmod(0o644)
    os.replace(temporary, path)


def create_catalog(
    *,
    snapshot: Path,
    archive_id: str,
    archive_sha256: str,
    archive_bytes: int,
    parts: list[Path],
    repository: str,
    release_tag: str,
    source_evidence: Path,
    variant_audit: Path,
) -> dict:
    manifest_path = snapshot / "manifest.json"
    snapshot_path = snapshot / "snapshot.json"
    manifest = json.loads(manifest_path.read_text())
    evidence = json.loads(source_evidence.read_text())
    audit = json.loads(variant_audit.read_text())
    if audit.get("safe_to_archive_only_candidate") is not True:
        raise ValueError("variant audit does not authorize the canonical snapshot")
    if Path(audit["canonical_snapshot"]).resolve() != snapshot.resolve():
        raise ValueError("variant audit canonical snapshot does not match")
    return {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "archive_id": archive_id,
        "archive_format": "deterministic-tar+zstd+split",
        "repository": repository,
        "release_tag": release_tag,
        "research_only": True,
        "snapshot": {
            "id": snapshot.name,
            "relative_restore_path": (
                "output/data_provenance/companyfacts_historical_snapshots/"
                + snapshot.name
            ),
            "entry_count": int(manifest["entry_count"]),
            "entry_bytes": sum(int(row["bytes"]) for row in manifest["entries"]),
            "manifest_sha256": sha256(manifest_path),
            "snapshot_metadata_sha256": sha256(snapshot_path),
        },
        "source": {
            "archive_item": evidence["archive_item"],
            "capture_timestamp": evidence["capture_timestamp"],
            "warc_url": evidence["warc_url"],
            "zip_sha256": evidence["zip_sha256"],
            "extraction_evidence_asset": source_evidence.name,
            "extraction_evidence_sha256": sha256(source_evidence),
            "variant_audit_asset": variant_audit.name,
            "variant_audit_sha256": sha256(variant_audit),
        },
        "archive": {
            "sha256": archive_sha256,
            "bytes": archive_bytes,
            "parts": [
                {
                    "name": part.name,
                    "bytes": part.stat().st_size,
                    "sha256": sha256(part),
                }
                for part in parts
            ],
        },
        "restore": {
            "script": "scripts/restore_research_cache_archive.sh",
            "command": (
                "scripts/restore_research_cache_archive.sh "
                "research_cache/sec-companyfacts-2025-04-14.json "
                "dist/research-cache-restore <restore-parent>"
            ),
            "post_restore_verifier": (
                "PYTHONPATH=. .venv/bin/python "
                "scripts/companyfacts_cache_snapshot.py --verify-snapshot "
                "output/data_provenance/companyfacts_historical_snapshots/"
                + snapshot.name
            ),
        },
        "warning": (
            "This immutable 2025-04-14 research snapshot is incomplete for 104 "
            "symbols in the later formal scope and cannot replace current formal data."
        ),
    }


def verify_parts(catalog: dict, directory: Path) -> dict:
    checked = []
    for row in catalog["archive"]["parts"]:
        path = directory / row["name"]
        if not path.is_file():
            raise ValueError(f"missing archive part: {path}")
        actual = sha256(path)
        if actual != row["sha256"] or path.stat().st_size != int(row["bytes"]):
            raise ValueError(f"archive part verification failed: {path}")
        checked.append(path.name)
    return {"verified": True, "part_count": len(checked), "parts": checked}


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--snapshot", type=Path, required=True)
    create.add_argument("--archive-id", required=True)
    create.add_argument("--archive-sha256", required=True)
    create.add_argument("--archive-bytes", type=int, required=True)
    create.add_argument("--parts-dir", type=Path, required=True)
    create.add_argument("--parts-prefix", required=True)
    create.add_argument("--repository", required=True)
    create.add_argument("--release-tag", required=True)
    create.add_argument("--source-evidence", type=Path, required=True)
    create.add_argument("--variant-audit", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("verify-parts")
    verify.add_argument("--catalog", type=Path, required=True)
    verify.add_argument("--parts-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "create":
        parts = sorted(args.parts_dir.glob(args.parts_prefix + "*"))
        if not parts:
            raise ValueError("no archive parts found")
        payload = create_catalog(
            snapshot=args.snapshot,
            archive_id=args.archive_id,
            archive_sha256=args.archive_sha256,
            archive_bytes=args.archive_bytes,
            parts=parts,
            repository=args.repository,
            release_tag=args.release_tag,
            source_evidence=args.source_evidence,
            variant_audit=args.variant_audit,
        )
        atomic_json(args.output, payload)
        print(json.dumps({
            "archive_id": payload["archive_id"],
            "archive_bytes": payload["archive"]["bytes"],
            "part_count": len(payload["archive"]["parts"]),
        }, sort_keys=True))
    else:
        payload = json.loads(args.catalog.read_text())
        print(json.dumps(verify_parts(payload, args.parts_dir), sort_keys=True))


if __name__ == "__main__":
    main()
