"""Create immutable, content-verified snapshots of the SEC Company Facts cache.

The active Company Facts cache is intentionally refreshable: a successful
network refresh atomically replaces a CIK payload with a newer SEC response.
That is useful operationally, but a released rebuild must retain the exact raw
bytes that produced it.  This tool captures those bytes into independent
snapshot files while the cache lock is held.  Copies are intentional: some
manifest-referenced sidecars can be updated in place, and a hard link would
let that mutate the supposedly immutable snapshot.

Snapshots are input provenance only.  They never modify annual/quarterly
fundamentals, coverage files, security identities, terminal returns, or
validation artifacts.  A rebuild against a snapshot still has to be compared
and explicitly authorized before it can replace a formal output.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.io.fundamentals_update import (
    SEC_COMPANYFACTS_CACHE_DIR,
    COMPANYFACTS_FULL_REBUILD_SCOPE_FORMAT_VERSION,
    companyfacts_full_rebuild_symbol_sha256,
    companyfacts_full_rebuild_recipe,
    companyfacts_full_rebuild_recipe_sha256,
    companyfacts_cache_lock,
    dry_run_companyfacts_full_reparse,
    load_companyfacts_full_rebuild_inputs,
    verify_companyfacts_cache_manifest,
)
from src.conf import (
    POINT_IN_TIME_FUNDAMENTALS_FILE,
    POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE,
)


SNAPSHOT_FORMAT_VERSION = 1
REBUILD_DRY_RUN_REPORT_FORMAT_VERSION = 2
SNAPSHOT_METADATA_NAME = "snapshot.json"
SNAPSHOT_DIRECTORY_NAME = "snapshots"
DEFAULT_REBUILD_REPORT_DIR = Path("output/data_provenance/companyfacts_rebuild_dry_runs")
DEFAULT_REBUILD_SCOPE_DIR = Path("output/data_provenance/companyfacts_rebuild_scopes")
_SAFE_SNAPSHOT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}\Z")


def _companyfacts_full_rebuild_scope_identity(scope: dict[str, Any]) -> str:
    """Fingerprint the semantic rebuild scope, excluding creation metadata."""
    material = {
        "format_version": scope["format_version"],
        "snapshot": scope["snapshot"],
        "formal_outputs": scope["formal_outputs"],
        "required_symbol_count": scope["required_symbol_count"],
        "required_symbols_sha256": scope["required_symbols_sha256"],
        "rebuild_recipe_sha256": scope["rebuild_recipe_sha256"],
    }
    canonical = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _default_rebuild_report_path(
    snapshot_id: str,
    scope: dict[str, Any],
) -> Path:
    """Keep reports for distinct scope/recipe combinations side by side."""
    scope_identity = _companyfacts_full_rebuild_scope_identity(scope)
    return DEFAULT_REBUILD_REPORT_DIR / (
        f"{snapshot_id}-scope-{scope_identity[:16]}.json"
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_referenced_files(manifest: dict[str, Any]) -> dict[str, str]:
    """Return every manifest ``path`` / ``sha256`` pair, rejecting conflicts."""
    files: dict[str, str] = {}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            path = value.get("path")
            sha256 = value.get("sha256")
            if isinstance(path, str) and isinstance(sha256, str):
                previous = files.setdefault(path, sha256)
                if previous != sha256:
                    raise ValueError(
                        f"cache manifest has conflicting hashes for {path}"
                    )
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(manifest)
    if not files:
        raise ValueError("cache manifest has no referenced files")
    return dict(sorted(files.items()))


def _safe_cache_relative_path(path: str) -> Path:
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts or relative == Path("."):
        raise ValueError(f"cache manifest has unsafe referenced path: {path}")
    return relative


def _load_manifest(cache_dir: Path) -> tuple[dict[str, Any], bytes, str]:
    manifest_path = cache_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"missing Company Facts cache manifest: {manifest_path}")
    payload = manifest_path.read_bytes()
    try:
        manifest = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid Company Facts cache manifest: {manifest_path}") from exc
    if not isinstance(manifest, dict):
        raise ValueError("Company Facts cache manifest must be an object")
    return manifest, payload, hashlib.sha256(payload).hexdigest()


def _snapshot_id(manifest_sha256: str, requested: str | None) -> str:
    snapshot_id = requested or f"manifest-{manifest_sha256[:16]}"
    if not _SAFE_SNAPSHOT_ID.fullmatch(snapshot_id):
        raise ValueError(
            "snapshot id must contain only letters, digits, '.', '_' or '-'"
        )
    return snapshot_id


def _snapshot_metadata(
    *,
    snapshot_id: str,
    manifest_sha256: str,
    referenced_files: dict[str, str],
) -> dict[str, Any]:
    return {
        "format_version": SNAPSHOT_FORMAT_VERSION,
        "snapshot_id": snapshot_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "cache_manifest": "manifest.json",
        "cache_manifest_sha256": manifest_sha256,
        "referenced_file_count": len(referenced_files),
        "referenced_files": referenced_files,
        "storage_method": "copy",
        "research_only": True,
        "warning": (
            "This snapshots raw Company Facts inputs only. It does not assert "
            "that any formal annual or quarterly output is reproducible or "
            "authorized for replacement."
        ),
    }


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _result(snapshot_dir: Path, metadata: dict[str, Any], *, reused: bool) -> dict:
    return {
        "snapshot_dir": str(snapshot_dir),
        "snapshot_id": metadata["snapshot_id"],
        "cache_manifest_sha256": metadata["cache_manifest_sha256"],
        "referenced_file_count": metadata["referenced_file_count"],
        "storage_method": metadata["storage_method"],
        "reused": reused,
        "research_only": True,
    }


def _formal_output_scope_entry(path: str | Path) -> tuple[dict[str, str], set[str]]:
    """Return a formal-output fingerprint plus its exact normalized tickers."""
    output = Path(path)
    if not output.is_file():
        raise FileNotFoundError(
            f"formal output required for rebuild scope is missing: {output}"
        )
    try:
        with output.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None or "ticker" not in reader.fieldnames:
                raise ValueError(
                    f"formal output required for rebuild scope has no ticker column: {output}"
                )
            tickers = {
                str(row.get("ticker") or "").strip().upper()
                for row in reader
                if str(row.get("ticker") or "").strip()
            }
    except OSError as exc:
        raise ValueError(
            f"Unable to read formal output required for rebuild scope {output}: {exc}"
        ) from exc
    return {"path": str(output), "sha256": _sha256_file(output)}, tickers


def _assert_formal_output_hashes(
    *,
    annual_output: str | Path,
    quarterly_output: str | Path,
    formal_outputs: dict[str, dict[str, str]],
) -> None:
    """Require the formal CSVs to still match the scope-bound bytes."""
    for label, output in (
        ("annual", Path(annual_output)),
        ("quarterly", Path(quarterly_output)),
    ):
        expected = formal_outputs[label]["sha256"]
        if _sha256_file(output) != expected:
            raise ValueError(
                f"formal {label} output does not match the scope-bound hash"
            )


def create_companyfacts_full_rebuild_scope(
    snapshot_dir: str | Path,
    *,
    scope_path: str | Path,
    annual_output: str | Path = POINT_IN_TIME_FUNDAMENTALS_FILE,
    quarterly_output: str | Path = POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE,
) -> dict:
    """Bind one immutable raw snapshot to the exact formal ticker scope.

    The scope is an input-provenance record, not a claim that the snapshot can
    reproduce the formal output bytes.  It makes any future full rebuild use
    a declared ticker set rather than whichever symbols happen to be in the
    mutable current-universe file on that day.
    """
    verified = verify_companyfacts_cache_snapshot(snapshot_dir)
    annual_entry, annual_tickers = _formal_output_scope_entry(annual_output)
    quarterly_entry, quarterly_tickers = _formal_output_scope_entry(
        quarterly_output
    )
    required_symbols = sorted(annual_tickers | quarterly_tickers)
    if not required_symbols:
        raise ValueError("formal outputs contain no tickers for a rebuild scope")
    rebuild_recipe = companyfacts_full_rebuild_recipe()
    scope = {
        "format_version": COMPANYFACTS_FULL_REBUILD_SCOPE_FORMAT_VERSION,
        "research_only": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "snapshot": {
            "snapshot_id": verified["snapshot_id"],
            "cache_manifest_sha256": verified["cache_manifest_sha256"],
        },
        "formal_outputs": {
            "annual": annual_entry,
            "quarterly": quarterly_entry,
        },
        "required_symbols": required_symbols,
        "required_symbol_count": len(required_symbols),
        "required_symbols_sha256": companyfacts_full_rebuild_symbol_sha256(
            required_symbols
        ),
        "rebuild_recipe": rebuild_recipe,
        "rebuild_recipe_sha256": companyfacts_full_rebuild_recipe_sha256(
            rebuild_recipe
        ),
        "warning": (
            "This scope freezes ticker membership, input identities, and the "
            "local parser recipe. "
            "It does not assert that this raw snapshot can reproduce the "
            "formal annual or quarterly output bytes."
        ),
    }
    destination = Path(scope_path)
    if destination.exists():
        existing = load_companyfacts_full_rebuild_inputs(snapshot_dir, destination)
        if not existing["rebuild_recipe_bound"]:
            raise FileExistsError(
                "full-rebuild scope already exists in legacy unbound format; "
                "choose a new scope path rather than overwriting it"
            )
        if existing["scope"]["required_symbols_sha256"] != scope[
            "required_symbols_sha256"
        ] or existing["scope"]["formal_outputs"] != scope["formal_outputs"] or (
            existing["rebuild_recipe_sha256"] != scope["rebuild_recipe_sha256"]
        ):
            raise FileExistsError(
                f"full-rebuild scope already exists with different content: {destination}"
            )
        return {
            "scope_path": str(destination),
            "snapshot_id": verified["snapshot_id"],
            "cache_manifest_sha256": verified["cache_manifest_sha256"],
            "required_symbol_count": len(required_symbols),
            "required_symbols_sha256": scope["required_symbols_sha256"],
            "rebuild_recipe_sha256": scope["rebuild_recipe_sha256"],
            "reused": True,
            "research_only": True,
        }
    _write_json(destination, scope)
    # Re-read through the production loader so creation and consumption share
    # one schema and snapshot-binding interpretation.
    load_companyfacts_full_rebuild_inputs(snapshot_dir, destination)
    return {
        "scope_path": str(destination),
        "snapshot_id": verified["snapshot_id"],
        "cache_manifest_sha256": verified["cache_manifest_sha256"],
        "required_symbol_count": len(required_symbols),
        "required_symbols_sha256": scope["required_symbols_sha256"],
        "rebuild_recipe_sha256": scope["rebuild_recipe_sha256"],
        "reused": False,
        "research_only": True,
    }


def verify_companyfacts_cache_snapshot(snapshot_dir: str | Path) -> dict:
    """Verify a snapshot's metadata, manifest, and manifest-bound raw files."""
    snapshot = Path(snapshot_dir)
    metadata_path = snapshot / SNAPSHOT_METADATA_NAME
    if not metadata_path.exists():
        raise FileNotFoundError(f"missing Company Facts snapshot metadata: {metadata_path}")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid Company Facts snapshot metadata: {metadata_path}") from exc
    required = {
        "format_version",
        "snapshot_id",
        "cache_manifest",
        "cache_manifest_sha256",
        "referenced_file_count",
        "referenced_files",
        "storage_method",
        "research_only",
    }
    missing = required - set(metadata)
    if missing:
        raise ValueError(f"snapshot metadata missing fields: {sorted(missing)}")
    if metadata["format_version"] != SNAPSHOT_FORMAT_VERSION:
        raise ValueError("unsupported Company Facts snapshot format")
    if metadata["cache_manifest"] != "manifest.json":
        raise ValueError("snapshot metadata must reference manifest.json")
    if metadata["storage_method"] not in {"copy", "hardlink"}:
        raise ValueError("snapshot storage method is not copy or legacy hardlink")
    if metadata["research_only"] is not True:
        raise ValueError("snapshot metadata must remain research-only")

    manifest, _payload, manifest_sha256 = _load_manifest(snapshot)
    if manifest_sha256 != metadata["cache_manifest_sha256"]:
        raise ValueError("snapshot manifest hash does not match snapshot metadata")
    referenced_files = _manifest_referenced_files(manifest)
    if referenced_files != metadata["referenced_files"]:
        raise ValueError("snapshot metadata files do not match cache manifest")
    if len(referenced_files) != int(metadata["referenced_file_count"]):
        raise ValueError("snapshot referenced file count does not match metadata")
    for relative_string, expected_sha256 in referenced_files.items():
        relative = _safe_cache_relative_path(relative_string)
        path = snapshot / relative
        if not path.is_file():
            raise FileNotFoundError(f"snapshot missing referenced file: {relative}")
        if _sha256_file(path) != expected_sha256:
            raise ValueError(f"snapshot file hash mismatch: {relative}")
    manifest_result = verify_companyfacts_cache_manifest(snapshot)
    if not manifest_result.get("verified"):
        raise ValueError("snapshot Company Facts manifest did not verify")
    return {
        **_result(snapshot, metadata, reused=False),
        "verified": True,
        "manifest_verified": True,
    }


def create_companyfacts_cache_snapshot(
    cache_dir: str | Path = SEC_COMPANYFACTS_CACHE_DIR,
    *,
    snapshot_root: str | Path | None = None,
    snapshot_id: str | None = None,
) -> dict:
    """Atomically capture a verified, copied snapshot of an active cache."""
    cache = Path(cache_dir)
    root = Path(snapshot_root) if snapshot_root is not None else cache / SNAPSHOT_DIRECTORY_NAME
    root.mkdir(parents=True, exist_ok=True)
    with companyfacts_cache_lock(cache):
        manifest_result = verify_companyfacts_cache_manifest(cache)
        if not manifest_result.get("verified"):
            raise ValueError("active Company Facts cache manifest did not verify")
        manifest, manifest_payload, manifest_sha256 = _load_manifest(cache)
        referenced_files = _manifest_referenced_files(manifest)
        resolved_id = _snapshot_id(manifest_sha256, snapshot_id)
        target = root / resolved_id
        if target.exists():
            verified = verify_companyfacts_cache_snapshot(target)
            if verified["cache_manifest_sha256"] != manifest_sha256:
                raise FileExistsError(
                    f"snapshot id {resolved_id} already exists for another manifest"
                )
            return _result(target, json.loads((target / SNAPSHOT_METADATA_NAME).read_text(encoding="utf-8")), reused=True)

        temporary = Path(tempfile.mkdtemp(prefix=f".{resolved_id}.", dir=root))
        try:
            for relative_string, expected_sha256 in referenced_files.items():
                relative = _safe_cache_relative_path(relative_string)
                source = cache / relative
                if not source.is_file():
                    raise FileNotFoundError(f"active cache missing referenced file: {relative}")
                if _sha256_file(source) != expected_sha256:
                    raise ValueError(f"active cache file hash mismatch: {relative}")
                destination = temporary / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                # Never hard-link mutable cache sidecars into an immutable
                # snapshot: an in-place refresh would otherwise mutate the
                # snapshot through the shared inode.
                shutil.copyfile(source, destination)
            (temporary / "manifest.json").write_bytes(manifest_payload)
            metadata = _snapshot_metadata(
                snapshot_id=resolved_id,
                manifest_sha256=manifest_sha256,
                referenced_files=referenced_files,
            )
            _write_json(temporary / SNAPSHOT_METADATA_NAME, metadata)
            os.replace(temporary, target)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
    verify_companyfacts_cache_snapshot(target)
    return _result(target, metadata, reused=False)


def record_snapshot_full_rebuild_dry_run(
    snapshot_dir: str | Path,
    *,
    scope_path: str | Path,
    report_path: str | Path,
    annual_output: str | Path = POINT_IN_TIME_FUNDAMENTALS_FILE,
    quarterly_output: str | Path = POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE,
    runner=dry_run_companyfacts_full_reparse,
) -> dict:
    """Persist a non-mutating full-rebuild comparison bound to one snapshot.

    ``runner`` is injectable for tests. A valid runner must explicitly report
    dry-run behavior and no formal-output write; this prevents a provenance
    report from laundering a mutating rebuild into a research artifact.
    """
    verified = verify_companyfacts_cache_snapshot(snapshot_dir)
    inputs = load_companyfacts_full_rebuild_inputs(snapshot_dir, scope_path)
    if not inputs["rebuild_recipe_bound"]:
        raise ValueError(
            "snapshot full-rebuild dry run requires a parser-recipe-bound scope"
        )
    snapshot = Path(snapshot_dir)
    formal_outputs = inputs["scope"]["formal_outputs"]
    _assert_formal_output_hashes(
        annual_output=annual_output,
        quarterly_output=quarterly_output,
        formal_outputs=formal_outputs,
    )
    # The production dry-run only reads these paths.  Use byte-for-byte copies
    # anyway: this keeps an injected or future runner from receiving a writable
    # path to a formal output, while preserving the exact comparison baseline.
    with tempfile.TemporaryDirectory(prefix="companyfacts-rebuild-formal-") as temp:
        temporary = Path(temp)
        comparison_annual = temporary / "annual.csv"
        comparison_quarterly = temporary / "quarterly.csv"
        shutil.copyfile(annual_output, comparison_annual)
        shutil.copyfile(quarterly_output, comparison_quarterly)
        result = runner(
            cache_dir=inputs["cache_dir"],
            output=comparison_annual,
            quarterly_output=comparison_quarterly,
            required_symbols=inputs["required_symbols"],
            expected_cache_manifest_sha256=inputs["cache_manifest_sha256"],
            expected_rebuild_recipe_sha256=inputs["rebuild_recipe_sha256"],
            include_ticker_deltas=True,
        )
    _assert_formal_output_hashes(
        annual_output=annual_output,
        quarterly_output=quarterly_output,
        formal_outputs=formal_outputs,
    )
    if not result.get("dry_run"):
        raise ValueError("snapshot rebuild runner did not report dry_run=True")
    write_flags = (
        "formal_outputs_written",
        "annual_output_written",
        "quarterly_output_written",
        "parsed_outputs_written",
    )
    if any(result.get(flag) for flag in write_flags):
        raise ValueError("snapshot rebuild runner reported a formal output write")
    if result.get("rebuild_recipe_matched") is not True:
        raise ValueError(
            "snapshot rebuild runner did not verify the declared parser recipe"
        )
    report = {
        "format_version": REBUILD_DRY_RUN_REPORT_FORMAT_VERSION,
        "research_only": True,
        "snapshot": {
            "snapshot_dir": str(snapshot),
            "snapshot_id": verified["snapshot_id"],
            "cache_manifest_sha256": verified["cache_manifest_sha256"],
            "referenced_file_count": verified["referenced_file_count"],
            "verified": True,
        },
        "scope": {
            "scope_path": str(scope_path),
            "required_symbol_count": len(inputs["required_symbols"]),
            "required_symbols_sha256": inputs["scope"]["required_symbols_sha256"],
            "formal_outputs": inputs["scope"]["formal_outputs"],
            "rebuild_recipe": inputs["rebuild_recipe"],
            "rebuild_recipe_sha256": inputs["rebuild_recipe_sha256"],
            "verified": True,
        },
        "dry_run": result,
        "warning": (
            "This records a non-mutating comparison only. A mismatch blocks "
            "formal replacement; an exact match still requires explicit "
            "authorization before any release-data mutation."
        ),
    }
    _write_json(Path(report_path), report)
    return {
        "report_path": str(Path(report_path)),
        "snapshot_id": verified["snapshot_id"],
        "cache_manifest_sha256": verified["cache_manifest_sha256"],
        "research_only": True,
        "dry_run": True,
        "formal_outputs_written": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", default=str(SEC_COMPANYFACTS_CACHE_DIR))
    parser.add_argument("--snapshot-root")
    parser.add_argument("--snapshot-id")
    parser.add_argument(
        "--record-full-dry-run",
        help="Run and record a non-mutating full rebuild comparison for this snapshot.",
    )
    parser.add_argument(
        "--create-full-rebuild-scope",
        help=(
            "Create an explicit frozen ticker scope bound to this immutable "
            "snapshot and the supplied formal annual/quarterly outputs."
        ),
    )
    parser.add_argument(
        "--rebuild-scope",
        help=(
            "Scope JSON to create with --create-full-rebuild-scope or to use "
            "with --record-full-dry-run."
        ),
    )
    parser.add_argument("--rebuild-report")
    parser.add_argument("--annual-output", default=POINT_IN_TIME_FUNDAMENTALS_FILE)
    parser.add_argument(
        "--quarterly-output", default=POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE
    )
    parser.add_argument(
        "--verify-snapshot",
        help="Verify an existing snapshot instead of creating one.",
    )
    args = parser.parse_args()
    selected_modes = sum(
        bool(value)
        for value in (
            args.verify_snapshot,
            args.record_full_dry_run,
            args.create_full_rebuild_scope,
        )
    )
    if selected_modes > 1:
        parser.error(
            "select at most one of --verify-snapshot, --record-full-dry-run, "
            "or --create-full-rebuild-scope"
        )
    if args.verify_snapshot:
        if (
            args.snapshot_root
            or args.snapshot_id
            or args.rebuild_report
            or args.rebuild_scope
        ):
            parser.error("--verify-snapshot cannot be combined with creation/report options")
        result = verify_companyfacts_cache_snapshot(args.verify_snapshot)
    elif args.record_full_dry_run:
        if not args.rebuild_scope:
            parser.error("--record-full-dry-run requires --rebuild-scope")
        snapshot = Path(args.record_full_dry_run)
        verification = verify_companyfacts_cache_snapshot(snapshot)
        rebuild_inputs = load_companyfacts_full_rebuild_inputs(
            snapshot,
            args.rebuild_scope,
        )
        report_path = (
            Path(args.rebuild_report)
            if args.rebuild_report
            else _default_rebuild_report_path(
                verification["snapshot_id"],
                rebuild_inputs["scope"],
            )
        )
        result = record_snapshot_full_rebuild_dry_run(
            snapshot,
            scope_path=args.rebuild_scope,
            report_path=report_path,
            annual_output=args.annual_output,
            quarterly_output=args.quarterly_output,
        )
    elif args.create_full_rebuild_scope:
        if args.snapshot_root or args.snapshot_id or args.rebuild_report:
            parser.error(
                "--create-full-rebuild-scope cannot be combined with snapshot "
                "creation or report options"
            )
        snapshot = Path(args.create_full_rebuild_scope)
        verification = verify_companyfacts_cache_snapshot(snapshot)
        scope_path = (
            Path(args.rebuild_scope)
            if args.rebuild_scope
            else DEFAULT_REBUILD_SCOPE_DIR / f"{verification['snapshot_id']}.json"
        )
        result = create_companyfacts_full_rebuild_scope(
            snapshot,
            scope_path=scope_path,
            annual_output=args.annual_output,
            quarterly_output=args.quarterly_output,
        )
    else:
        if args.rebuild_report or args.rebuild_scope:
            parser.error(
                "--rebuild-report requires --record-full-dry-run and "
                "--rebuild-scope requires a scope creation or dry-run mode"
            )
        result = create_companyfacts_cache_snapshot(
            args.cache_dir,
            snapshot_root=args.snapshot_root,
            snapshot_id=args.snapshot_id,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
