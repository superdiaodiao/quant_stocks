"""Prove whether two Company Facts snapshots differ only in symbol metadata."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", dir=path.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


def _entries(snapshot: Path) -> tuple[dict, dict[int, dict]]:
    manifest_path = snapshot / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    entries = {int(row["cik"]): row for row in manifest["entries"]}
    if len(entries) != int(manifest["entry_count"]):
        raise ValueError(f"duplicate or missing CIK entries in {manifest_path}")
    return manifest, entries


def audit_variants(base: Path, candidate: Path) -> dict:
    base_manifest, base_entries = _entries(base)
    candidate_manifest, candidate_entries = _entries(candidate)
    base_ciks = set(base_entries)
    candidate_ciks = set(candidate_entries)
    missing_in_candidate = sorted(base_ciks - candidate_ciks)
    added_in_candidate = sorted(candidate_ciks - base_ciks)
    same_file_sha_count = 0
    symbol_only_difference_count = 0
    semantic_differences: list[dict] = []
    changed_symbol_examples: list[dict] = []

    for cik in sorted(base_ciks & candidate_ciks):
        base_entry = base_entries[cik]
        candidate_entry = candidate_entries[cik]
        if base_entry["sha256"] == candidate_entry["sha256"]:
            same_file_sha_count += 1
            continue
        base_payload = json.loads((base / base_entry["path"]).read_text())
        candidate_payload = json.loads(
            (candidate / candidate_entry["path"]).read_text()
        )
        base_symbols = base_payload.pop("symbols", [])
        candidate_symbols = candidate_payload.pop("symbols", [])
        if base_payload != candidate_payload:
            semantic_differences.append(
                {"cik": cik, "path": base_entry["path"]}
            )
            continue
        symbol_only_difference_count += 1
        if len(changed_symbol_examples) < 20:
            changed_symbol_examples.append(
                {
                    "cik": cik,
                    "base_symbols": base_symbols,
                    "candidate_symbols": candidate_symbols,
                }
            )

    safe = not missing_in_candidate and not added_in_candidate and not semantic_differences
    return {
        "format_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "purpose": "companyfacts_snapshot_variant_deduplication_audit",
        "base_snapshot": {
            "path": str(base.resolve()),
            "manifest_sha256": _sha256(base / "manifest.json"),
            "entry_count": len(base_entries),
            "entry_bytes": sum(int(row["bytes"]) for row in base_entries.values()),
        },
        "candidate_snapshot": {
            "path": str(candidate.resolve()),
            "manifest_sha256": _sha256(candidate / "manifest.json"),
            "entry_count": len(candidate_entries),
            "entry_bytes": sum(
                int(row["bytes"]) for row in candidate_entries.values()
            ),
        },
        "same_file_sha_count": same_file_sha_count,
        "symbol_only_difference_count": symbol_only_difference_count,
        "semantic_difference_count": len(semantic_differences),
        "semantic_differences": semantic_differences,
        "missing_in_candidate": missing_in_candidate,
        "added_in_candidate": added_in_candidate,
        "changed_symbol_examples": changed_symbol_examples,
        "safe_to_archive_only_candidate": safe,
        "canonical_snapshot": str(candidate.resolve()) if safe else None,
        "interpretation": (
            "Every changed entry differs only in the symbols field; the candidate "
            "preserves all Company Facts payload and fetch provenance bytes."
            if safe
            else "Snapshot variants contain scope or non-symbol semantic differences."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit_variants(args.base, args.candidate)
    _atomic_json(args.output, report)
    print(json.dumps({
        "same_file_sha_count": report["same_file_sha_count"],
        "symbol_only_difference_count": report["symbol_only_difference_count"],
        "semantic_difference_count": report["semantic_difference_count"],
        "safe_to_archive_only_candidate": report["safe_to_archive_only_candidate"],
    }, sort_keys=True))
    if not report["safe_to_archive_only_candidate"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
