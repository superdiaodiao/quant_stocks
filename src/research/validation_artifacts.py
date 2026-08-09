"""Integrity manifest for the canonical CAN SLIM validation artifact set."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


VALIDATION_ARTIFACT_NAMES = (
    "can_slim_fixed_top3_backtest.csv",
    "can_slim_fixed_top3_trade_ledger.csv",
    "can_slim_fixed_top3_annual.csv",
    "can_slim_fixed_top3_cost_stress.csv",
    "can_slim_fixed_top3_liquidity_capacity.csv",
    "can_slim_fixed_top3_summary.json",
    "can_slim_technical_candidate_financial_coverage.json",
    "can_slim_technical_candidate_financial_priorities.csv",
)
VALIDATION_ARTIFACT_MANIFEST_NAME = (
    "can_slim_validation_artifacts_manifest.json"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_validation_artifact_manifest(
    artifact_paths: dict[str, Path],
) -> dict:
    """Fingerprint a complete staged validation generation."""
    names = set(artifact_paths)
    expected = set(VALIDATION_ARTIFACT_NAMES)
    if names != expected:
        raise RuntimeError(
            "Validation artifact set is incomplete: "
            f"missing={sorted(expected - names)}, "
            f"unexpected={sorted(names - expected)}"
        )
    return {
        "format_version": 1,
        "artifacts": [
            {
                "path": name,
                "bytes": Path(artifact_paths[name]).stat().st_size,
                "sha256": _sha256(Path(artifact_paths[name])),
            }
            for name in VALIDATION_ARTIFACT_NAMES
        ],
    }


def verify_validation_artifact_manifest(
    output_dir: str | Path = "output",
) -> dict:
    """Reject a missing, malformed, partial, or mixed validation generation."""
    root = Path(output_dir)
    manifest_path = root / VALIDATION_ARTIFACT_MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Validation artifact manifest unavailable or invalid: {exc}"
        ) from exc
    entries = manifest.get("artifacts")
    if (
        manifest.get("format_version") != 1
        or not isinstance(entries, list)
    ):
        raise RuntimeError("Validation artifact manifest has invalid format")
    names = [entry.get("path") for entry in entries if isinstance(entry, dict)]
    if (
        tuple(names) != VALIDATION_ARTIFACT_NAMES
        or len(entries) != len(VALIDATION_ARTIFACT_NAMES)
    ):
        raise RuntimeError(
            "Validation artifact manifest inventory mismatch"
        )
    for entry in entries:
        name = entry["path"]
        target = root / name
        if (
            Path(name).name != name
            or not target.is_file()
            or entry.get("bytes") != target.stat().st_size
            or entry.get("sha256") != _sha256(target)
        ):
            raise RuntimeError(
                f"Validation artifact integrity mismatch: {name}"
            )
    return {
        "verified": True,
        "manifest": str(manifest_path),
        "artifact_count": len(entries),
    }
