"""Create and verify provenance metadata for the cumulative shadow ledger."""

from __future__ import annotations

import hashlib
import csv
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping


MANIFEST_NAME = "recommendation_history.provenance.json"
SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = {1, SCHEMA_VERSION}
PORTFOLIO_SOURCE_COLUMN_MAP = {
    "kind": "portfolio_source_kind",
    "repository": "portfolio_repository",
    "workflow": "portfolio_workflow",
    "run_id": "portfolio_run_id",
    "run_attempt": "portfolio_run_attempt",
    "run_url": "portfolio_run_url",
    "default_branch": "portfolio_default_branch",
    "git_ref": "portfolio_git_ref",
    "git_sha": "portfolio_git_sha",
    "event_name": "portfolio_event_name",
}
SOURCE_IDENTITY_FIELDS = tuple(PORTFOLIO_SOURCE_COLUMN_MAP)


def current_run_source(
    environment: Mapping[str, str] | None = None,
) -> dict:
    env = os.environ if environment is None else environment
    in_github_actions = env.get("GITHUB_ACTIONS", "").lower() == "true"
    repository = env.get("GITHUB_REPOSITORY")
    run_id = env.get("GITHUB_RUN_ID")
    return {
        "kind": (
            "github_actions_run" if in_github_actions
            else "local_unanchored"
        ),
        "repository": repository,
        "workflow": env.get("GITHUB_WORKFLOW"),
        "run_id": run_id,
        "run_attempt": env.get("GITHUB_RUN_ATTEMPT"),
        "run_url": (
            f"{env.get('GITHUB_SERVER_URL', 'https://github.com')}/"
            f"{repository}/actions/runs/{run_id}"
            if in_github_actions
            else None
        ),
        "previous_artifact_id": (
            env.get("SHADOW_PREVIOUS_ARTIFACT_ID") or None
        ),
        "default_branch": env.get("SHADOW_DEFAULT_BRANCH"),
        "git_ref": env.get("GITHUB_REF"),
        "git_sha": env.get("GITHUB_SHA"),
        "event_name": env.get("GITHUB_EVENT_NAME"),
    }


def portfolio_source_columns(
    environment: Mapping[str, str] | None = None,
) -> dict:
    source = current_run_source(environment)
    return {
        column: source[field]
        for field, column in PORTFOLIO_SOURCE_COLUMN_MAP.items()
    }


def github_actions_source_is_valid(source: Mapping[str, object]) -> bool:
    repository = str(source.get("repository") or "")
    workflow = str(source.get("workflow") or "")
    run_id = str(source.get("run_id") or "")
    run_attempt = str(source.get("run_attempt") or "")
    run_url = str(source.get("run_url") or "")
    default_branch = str(source.get("default_branch") or "")
    git_ref = str(source.get("git_ref") or "")
    git_sha = str(source.get("git_sha") or "")
    event_name = str(source.get("event_name") or "")
    return (
        source.get("kind") == "github_actions_run"
        and "/" in repository
        and bool(workflow)
        and run_id.isdigit()
        and int(run_id) > 0
        and run_attempt.isdigit()
        and int(run_attempt) > 0
        and run_url
        == f"https://github.com/{repository}/actions/runs/{run_id}"
        and bool(default_branch)
        and git_ref == f"refs/heads/{default_branch}"
        and re.fullmatch(r"[0-9a-fA-F]{40}", git_sha) is not None
        and event_name in {"schedule", "workflow_dispatch"}
    )


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_ancestry_entry(
    manifest_path: Path,
    manifest: Mapping[str, object],
    artifact_id: str,
) -> dict:
    return {
        "artifact_id": artifact_id,
        "manifest_sha256": file_sha256(manifest_path),
        "ledger_sha256": manifest.get("ledger_sha256"),
        "ledger_bytes": manifest.get("ledger_bytes"),
        "sealed_at_utc": manifest.get("sealed_at_utc"),
        "source": manifest.get("source"),
    }


def _ancestry_is_valid(
    ancestry: object,
    previous_artifact_id: object,
) -> bool:
    if not isinstance(ancestry, list):
        return False
    previous_id = str(previous_artifact_id or "")
    if bool(ancestry) != bool(previous_id):
        return False
    artifact_ids = []
    for entry in ancestry:
        if not isinstance(entry, dict):
            return False
        artifact_id = str(entry.get("artifact_id") or "")
        manifest_sha256 = str(entry.get("manifest_sha256") or "")
        ledger_sha256 = str(entry.get("ledger_sha256") or "")
        ledger_bytes = entry.get("ledger_bytes")
        sealed_at = entry.get("sealed_at_utc")
        source = entry.get("source")
        try:
            timestamp_valid = (
                datetime.fromisoformat(str(sealed_at)).tzinfo is not None
            )
        except ValueError:
            timestamp_valid = False
        if (
            not artifact_id.isdigit()
            or int(artifact_id) <= 0
            or re.fullmatch(r"[0-9a-fA-F]{64}", manifest_sha256) is None
            or re.fullmatch(r"[0-9a-fA-F]{64}", ledger_sha256) is None
            or not isinstance(ledger_bytes, int)
            or ledger_bytes <= 0
            or not timestamp_valid
            or not isinstance(source, dict)
            or not github_actions_source_is_valid(source)
        ):
            return False
        artifact_ids.append(artifact_id)
    return (
        len(artifact_ids) == len(set(artifact_ids))
        and (not artifact_ids or artifact_ids[-1] == previous_id)
    )


def build_shadow_ledger_manifest(
    history_file: str | Path,
    *,
    environment: Mapping[str, str] | None = None,
    sealed_at: datetime | None = None,
    ancestry: list[dict] | None = None,
) -> dict:
    history_path = Path(history_file)
    if not history_path.is_file():
        raise FileNotFoundError(history_path)
    source = current_run_source(environment)
    timestamp = sealed_at or datetime.now(timezone.utc)
    return {
        "schema_version": SCHEMA_VERSION,
        "ledger_file": history_path.name,
        "ledger_sha256": file_sha256(history_path),
        "ledger_bytes": history_path.stat().st_size,
        "sealed_at_utc": timestamp.astimezone(timezone.utc).isoformat(),
        "source": source,
        "ancestry": list(ancestry or []),
    }


def write_shadow_ledger_manifest(
    history_file: str | Path,
    manifest_file: str | Path | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    sealed_at: datetime | None = None,
) -> Path:
    history_path = Path(history_file)
    output = (
        Path(manifest_file)
        if manifest_file is not None
        else history_path.with_name(MANIFEST_NAME)
    )
    source = current_run_source(environment)
    ancestry = []
    previous_artifact_id = str(source.get("previous_artifact_id") or "")
    if previous_artifact_id:
        if not output.is_file():
            raise RuntimeError(
                "A previous shadow artifact was restored without its "
                "provenance manifest"
            )
        try:
            previous_manifest = json.loads(
                output.read_text(encoding="utf-8")
            )
        except (json.JSONDecodeError, OSError) as error:
            raise RuntimeError(
                "Previous shadow provenance manifest is invalid"
            ) from error
        if not isinstance(previous_manifest, dict):
            raise RuntimeError(
                "Previous shadow provenance manifest is invalid"
            )
        previous_ancestry = previous_manifest.get("ancestry", [])
        previous_source = previous_manifest.get("source", {})
        if (
            previous_manifest.get("schema_version") == SCHEMA_VERSION
            and not _ancestry_is_valid(
                previous_ancestry,
                (
                    previous_source.get("previous_artifact_id")
                    if isinstance(previous_source, dict)
                    else None
                ),
            )
        ):
            raise RuntimeError(
                "Previous shadow provenance ancestry is invalid"
            )
        ancestry = list(previous_ancestry)
        ancestry.append(
            _manifest_ancestry_entry(
                output, previous_manifest, previous_artifact_id
            )
        )
    manifest = build_shadow_ledger_manifest(
        history_path,
        environment=environment,
        sealed_at=sealed_at,
        ancestry=ancestry,
    )
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, output)
    return output


def verify_shadow_ledger(
    history_file: str | Path,
    manifest_file: str | Path | None = None,
) -> dict:
    history_path = Path(history_file)
    manifest_path = (
        Path(manifest_file)
        if manifest_file is not None
        else history_path.with_name(MANIFEST_NAME)
    )
    base = {
        "manifest_file": manifest_path.name,
        "integrity_verified": False,
        "externally_anchored": False,
        "ancestry_verified": False,
        "trusted_sources": [],
    }
    if not history_path.is_file():
        return {**base, "status": "NO_LEDGER"}
    if not manifest_path.is_file():
        return {**base, "status": "MISSING_MANIFEST"}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        return {
            **base,
            "status": "INVALID_MANIFEST",
            "error": str(error),
        }
    if not isinstance(manifest, dict):
        return {**base, "status": "INVALID_MANIFEST"}
    required = {
        "schema_version",
        "ledger_file",
        "ledger_sha256",
        "ledger_bytes",
        "sealed_at_utc",
        "source",
    }
    missing = sorted(required - set(manifest))
    schema_version = manifest.get("schema_version")
    if missing or schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        return {
            **base,
            "status": "INVALID_MANIFEST",
            "missing_fields": missing,
            "schema_version": manifest.get("schema_version"),
        }
    actual_sha256 = file_sha256(history_path)
    actual_bytes = history_path.stat().st_size
    integrity_verified = (
        manifest["ledger_file"] == history_path.name
        and manifest["ledger_sha256"] == actual_sha256
        and manifest["ledger_bytes"] == actual_bytes
    )
    if integrity_verified:
        # A valid checksum is not sufficient evidence: a repeated signal can
        # silently inflate forward-session counts.  Validate the canonical
        # recommendation key when this is a full shadow ledger; preserve
        # compatibility with older minimal ledgers that predate model_version.
        duplicate_keys = []
        try:
            with history_path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                required_columns = {"as_of", "model_version", "ticker"}
                if required_columns.issubset(reader.fieldnames or set()):
                    seen = set()
                    for row in reader:
                        key = (
                            str(row.get("as_of") or ""),
                            str(row.get("model_version") or ""),
                            str(row.get("ticker") or "").upper(),
                        )
                        if key in seen and key not in duplicate_keys:
                            duplicate_keys.append(key)
                        seen.add(key)
        except (OSError, csv.Error) as error:
            return {
                **base,
                "status": "INVALID_LEDGER",
                "integrity_verified": False,
                "hash_verified": True,
                "error": str(error),
            }
        if duplicate_keys:
            return {
                **base,
                "status": "INVALID_LEDGER",
                "integrity_verified": False,
                "hash_verified": True,
                "duplicate_keys": [list(key) for key in duplicate_keys[:20]],
                "duplicate_key_count": len(duplicate_keys),
            }
    source = (
        manifest["source"] if isinstance(manifest["source"], dict) else {}
    )
    try:
        sealed_at = datetime.fromisoformat(manifest["sealed_at_utc"])
        timestamp_valid = sealed_at.tzinfo is not None
    except (TypeError, ValueError):
        timestamp_valid = False
    ancestry_verified = (
        True
        if schema_version == 1
        else _ancestry_is_valid(
            manifest.get("ancestry"),
            source.get("previous_artifact_id"),
        )
    )
    externally_anchored = (
        integrity_verified
        and github_actions_source_is_valid(source)
        and timestamp_valid
        and ancestry_verified
    )
    ancestry = manifest.get("ancestry", [])
    trusted_sources = []
    if integrity_verified and ancestry_verified:
        candidates = [
            entry.get("source")
            for entry in ancestry
            if isinstance(entry, dict)
        ] + [source]
        trusted_sources = [
            candidate
            for candidate in candidates
            if (
                isinstance(candidate, dict)
                and github_actions_source_is_valid(candidate)
            )
        ]
    return {
        **base,
        "status": (
            "VERIFIED_GITHUB_ACTIONS"
            if externally_anchored
            else (
                "VERIFIED_LOCAL_UNANCHORED"
                if integrity_verified
                else "LEDGER_MISMATCH"
            )
        ),
        "integrity_verified": integrity_verified,
        "externally_anchored": externally_anchored,
        "ancestry_verified": ancestry_verified,
        "ancestry_depth": len(ancestry) if isinstance(ancestry, list) else 0,
        "trusted_sources": trusted_sources,
        "ledger_sha256": actual_sha256,
        "ledger_bytes": actual_bytes,
        "sealed_at_utc": manifest.get("sealed_at_utc"),
        "source": source,
    }
