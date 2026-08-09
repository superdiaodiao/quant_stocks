"""Audit recoverable sources for a missing historical SEC raw-cache snapshot.

This is deliberately a read-only forensic tool.  It never restores Git
objects, downloads release/artifact archives, rewrites an active cache, or
changes formal fundamentals.  It only records whether the local repository or
public GitHub inventory exposes a plausible historical Company Facts source.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable
from urllib.error import URLError
from urllib.request import Request, urlopen


DEFAULT_GITHUB_REPOSITORY = "superdiaodiao/quant_stocks"
DEFAULT_EXTERNAL_GITHUB_REPOSITORY = "etzhayyim/gov.sec.edgar"
DEFAULT_EXTERNAL_GITHUB_REF = "main"
DEFAULT_WAYBACK_COMPANYFACTS_URL = (
    "www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
)
DEFAULT_OUTPUT = Path("output/data_provenance/companyfacts_historical_source_recovery.json")
_CIK_PAYLOAD_PATH = re.compile(r"(?:^|/)CIK\d{10}\.json(?:\.gz)?$")
_RAW_STATE_FILENAMES = {
    "raw_cache_refresh_state.json",
    "historical_ticker_ciks.json",
    "reparse_state.json",
}


def _run_git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def _parse_unreachable_objects(lines: list[str]) -> dict[str, list[str]]:
    objects: dict[str, list[str]] = {"blob": [], "tree": []}
    for line in lines:
        parts = line.split()
        if len(parts) == 3 and parts[0] == "unreachable" and parts[1] in objects:
            objects[parts[1]].append(parts[2])
    return objects


def _looks_like_companyfacts_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    lowered = path.lower()
    return bool(
        _CIK_PAYLOAD_PATH.search(path)
        or name in _RAW_STATE_FILENAMES
        or "sec_companyfacts_cache/" in lowered
    )


def _blob_metadata(repo: Path, blob_ids: list[str]) -> list[tuple[str, int]]:
    if not blob_ids:
        return []
    completed = subprocess.run(
        ["git", "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
        cwd=repo,
        input="\n".join(blob_ids) + "\n",
        check=True,
        capture_output=True,
        text=True,
    )
    metadata = []
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[1] == "blob":
            metadata.append((parts[0], int(parts[2])))
    return metadata


def _drain(handle, byte_count: int) -> None:
    remaining = byte_count
    while remaining:
        piece = handle.read(min(64 * 1024, remaining))
        if not piece:
            raise RuntimeError("unexpected EOF while scanning unreachable Git blob")
        remaining -= len(piece)


def scan_unreachable_blob_signatures(
    repo: Path,
    blob_ids: list[str],
) -> dict[str, Any]:
    """Classify potential raw payloads without exposing their contents."""
    metadata = _blob_metadata(repo, blob_ids)
    process = subprocess.Popen(
        ["git", "cat-file", "--batch"],
        cwd=repo,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    gzip_candidates = []
    json_candidates = []
    scanned_bytes = 0
    try:
        assert process.stdin is not None
        assert process.stdout is not None
        for object_id, _expected_size in metadata:
            process.stdin.write((object_id + "\n").encode())
            process.stdin.flush()
            header = process.stdout.readline().decode().split()
            if len(header) != 3 or header[1] != "blob":
                continue
            size = int(header[2])
            prefix = process.stdout.read(min(4096, size))
            _drain(process.stdout, size - len(prefix))
            terminator = process.stdout.read(1)
            if terminator != b"\n":
                raise RuntimeError("unexpected Git blob terminator")
            scanned_bytes += size
            if prefix.startswith(b"\x1f\x8b"):
                gzip_candidates.append({"object_id": object_id, "bytes": size})
            stripped = prefix.lstrip()
            if stripped.startswith(b"{") and (
                b'"facts"' in prefix
                or (b'"cik"' in prefix and b'"entity' in prefix)
                or (b'"payload"' in prefix and b'"symbols"' in prefix)
            ):
                json_candidates.append({"object_id": object_id, "bytes": size})
    finally:
        if process.stdin is not None:
            process.stdin.close()
        if process.stdout is not None:
            process.stdout.close()
        process.wait()
    return {
        "unreachable_blob_count": len(metadata),
        "unreachable_blob_bytes": sum(size for _object_id, size in metadata),
        "scanned_blob_bytes": scanned_bytes,
        "gzip_blob_candidate_count": len(gzip_candidates),
        "gzip_blob_candidates": gzip_candidates,
        "companyfacts_json_candidate_count": len(json_candidates),
        "companyfacts_json_candidates": json_candidates,
    }


def _github_json(url: str) -> Any:
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "quant-stocks-historical-source-audit",
        },
    )
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def _asset_inventory(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    inventory = []
    for item in items:
        inventory.append({
            "name": item.get("name"),
            "bytes": item.get("size") or item.get("size_in_bytes"),
            "expired": item.get("expired"),
        })
    return inventory


def _is_raw_cache_asset(item: dict[str, Any]) -> bool:
    name = str(item.get("name") or "").lower()
    return any(token in name for token in ("companyfacts", "sec-cache", "raw-cache"))


def audit_external_github_companyfacts_archive(
    *,
    github_repository: str = DEFAULT_EXTERNAL_GITHUB_REPOSITORY,
    github_ref: str = DEFAULT_EXTERNAL_GITHUB_REF,
    required_ciks: set[int] | None = None,
    github_fetcher: Callable[[str], Any] = _github_json,
) -> dict[str, Any]:
    """Inventory a fixed GitHub raw Company Facts archive without importing it.

    A tree entry is only treated as a candidate when it has the canonical
    ``raw/companyfacts/CIK##########.json`` name.  This deliberately records
    source coverage and commit identity, but does not silently combine a
    partial external archive with the active cache.
    """
    tree_url = (
        f"https://api.github.com/repos/{github_repository}/git/trees/"
        f"{github_ref}?recursive=1"
    )
    commit_url = (
        f"https://api.github.com/repos/{github_repository}/commits/"
        f"{github_ref}"
    )
    tree = github_fetcher(tree_url)
    commit = github_fetcher(commit_url)
    entries = []
    for item in tree.get("tree", []):
        path = str(item.get("path") or "")
        match = re.fullmatch(r"raw/companyfacts/CIK(\d{10})\.json", path)
        if match is None:
            continue
        entries.append({
            "path": path,
            "cik": int(match.group(1)),
            "bytes": item.get("size"),
            "blob_sha": item.get("sha"),
            "raw_url": (
                f"https://raw.githubusercontent.com/{github_repository}/"
                f"{github_ref}/{path}"
            ),
        })
    available = {entry["cik"] for entry in entries}
    required = set(required_ciks or set())
    return {
        "repository": github_repository,
        "ref": github_ref,
        "commit_sha": commit.get("sha"),
        "commit_date": (commit.get("commit") or {}).get("author", {}).get("date"),
        "tree_truncated": bool(tree.get("truncated")),
        "candidate_count": len(entries),
        "candidate_entries": entries,
        "required_cik_count": len(required),
        "required_cik_overlap_count": len(required & available),
        "required_cik_overlap": sorted(required & available),
        "coverage": (
            len(required & available) / len(required) if required else None
        ),
        "research_only": True,
        "warning": (
            "This inventory does not import or merge the external payloads. "
            "Each payload still requires envelope, CIK, SHA, and recipe checks."
        ),
    }


def audit_wayback_companyfacts_captures(
    *,
    url: str = DEFAULT_WAYBACK_COMPANYFACTS_URL,
    fetcher: Callable[[str], Any] = _github_json,
) -> dict[str, Any]:
    """Inventory archived SEC Company Facts ZIP captures without downloading."""
    query = (
        "https://web.archive.org/cdx/search/cdx?"
        f"url={url}&output=json&filter=statuscode:200&"
        "fl=timestamp,original,statuscode,digest,length&collapse=digest"
    )
    response = fetcher(query)
    if not isinstance(response, list) or not response:
        return {"url": url, "capture_count": 0, "captures": [], "research_only": True}
    header, *rows = response
    captures = [dict(zip(header, row)) for row in rows]
    return {
        "url": url,
        "capture_count": len(captures),
        "captures": captures,
        "research_only": True,
        "warning": "Captures are indexed evidence; ZIP bytes were not imported.",
    }


def audit_historical_companyfacts_sources(
    repo: str | Path,
    *,
    github_repository: str = DEFAULT_GITHUB_REPOSITORY,
    include_network: bool = True,
    git_runner: Callable[..., str] = _run_git,
    blob_scanner: Callable[[Path, list[str]], dict[str, Any]] = scan_unreachable_blob_signatures,
    github_fetcher: Callable[[str], Any] = _github_json,
    external_github_repository: str | None = None,
    external_github_ref: str = DEFAULT_EXTERNAL_GITHUB_REF,
    required_ciks: set[int] | None = None,
    wayback_fetcher: Callable[[str], Any] = _github_json,
    include_wayback: bool = False,
) -> dict[str, Any]:
    """Return a reproducible, non-mutating historical raw-source inventory."""
    root = Path(repo)
    fsck_lines = git_runner(root, "fsck", "--no-reflogs", "--unreachable").splitlines()
    objects = _parse_unreachable_objects(fsck_lines)
    tree_matches = []
    for tree in objects["tree"]:
        paths = git_runner(root, "ls-tree", "-r", "--name-only", tree).splitlines()
        matching_paths = [path for path in paths if _looks_like_companyfacts_path(path)]
        if matching_paths:
            tree_matches.append({"tree": tree, "paths": matching_paths})
    blob_summary = blob_scanner(root, objects["blob"])
    remote: dict[str, Any] = {"checked": include_network}
    if include_network:
        try:
            releases = github_fetcher(
                f"https://api.github.com/repos/{github_repository}/releases"
            )
            artifacts = github_fetcher(
                f"https://api.github.com/repos/{github_repository}/actions/artifacts?per_page=100"
            )
            release_assets = [
                asset
                for release in releases
                for asset in release.get("assets", [])
            ]
            artifact_items = artifacts.get("artifacts", [])
            remote.update({
                "release_count": len(releases),
                "release_assets": _asset_inventory(release_assets),
                "workflow_artifact_count": len(artifact_items),
                "workflow_artifacts": _asset_inventory(artifact_items),
                "raw_cache_release_asset_count": sum(
                    _is_raw_cache_asset(asset) for asset in release_assets
                ),
                "raw_cache_workflow_artifact_count": sum(
                    _is_raw_cache_asset(artifact) for artifact in artifact_items
                ),
            })
        except (URLError, OSError, ValueError) as exc:
            remote["error"] = f"{type(exc).__name__}: {exc}"
    external_archive = None
    if include_network and external_github_repository:
        try:
            external_archive = audit_external_github_companyfacts_archive(
                github_repository=external_github_repository,
                github_ref=external_github_ref,
                required_ciks=required_ciks,
                github_fetcher=github_fetcher,
            )
        except (URLError, OSError, ValueError) as exc:
            external_archive = {
                "repository": external_github_repository,
                "ref": external_github_ref,
                "error": f"{type(exc).__name__}: {exc}",
                "research_only": True,
            }
    wayback = None
    if include_network and include_wayback:
        try:
            wayback = audit_wayback_companyfacts_captures(fetcher=wayback_fetcher)
        except (URLError, OSError, ValueError) as exc:
            wayback = {
                "url": DEFAULT_WAYBACK_COMPANYFACTS_URL,
                "error": f"{type(exc).__name__}: {exc}",
                "research_only": True,
            }
    candidate_count = (
        len(tree_matches)
        + blob_summary["gzip_blob_candidate_count"]
        + blob_summary["companyfacts_json_candidate_count"]
        + int(remote.get("raw_cache_release_asset_count", 0))
        + int(remote.get("raw_cache_workflow_artifact_count", 0))
        + int((external_archive or {}).get("candidate_count", 0))
        + int((wayback or {}).get("capture_count", 0))
    )
    return {
        "format_version": 1,
        "research_only": True,
        "method": (
            "Read-only local Git unreachable-object/path inventory plus public "
            "GitHub release and Actions-artifact metadata. No object, archive, "
            "or formal data is restored or modified."
        ),
        "repository": str(root.resolve()),
        "github_repository": github_repository,
        "unreachable_tree_count": len(objects["tree"]),
        "unreachable_tree_raw_cache_matches": tree_matches,
        "blob_scan": blob_summary,
        "remote": remote,
        "external_archive": external_archive,
        "wayback": wayback,
        "candidate_source_count": candidate_count,
        "recovery_status": (
            "CANDIDATE_SOURCES_FOUND"
            if candidate_count
            else "NO_HISTORICAL_RAW_SOURCE_FOUND"
        ),
        "warning": (
            "A no-source result proves only that this local Git object database "
            "and the queried public GitHub inventories exposed no candidate. It "
            "does not rule out a private backup, expired artifact, or another "
            "authorized storage location."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--github-repository", default=DEFAULT_GITHUB_REPOSITORY)
    parser.add_argument(
        "--external-github-repository",
        default=None,
        help="Optional public raw Company Facts archive to inventory",
    )
    parser.add_argument("--external-github-ref", default=DEFAULT_EXTERNAL_GITHUB_REF)
    parser.add_argument(
        "--required-cik-manifest",
        type=Path,
        default=None,
        help="Manifest JSON whose entries define required CIK coverage",
    )
    parser.add_argument("--skip-network", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    required_ciks = None
    if args.required_cik_manifest is not None:
        manifest = json.loads(
            args.required_cik_manifest.read_text(encoding="utf-8")
        )
        required_ciks = {
            int(entry["cik"])
            for entry in manifest.get("entries", [])
            if str(entry.get("cik", "")).isdigit()
        }
    result = audit_historical_companyfacts_sources(
        args.repo,
        github_repository=args.github_repository,
        include_network=not args.skip_network,
        external_github_repository=args.external_github_repository,
        external_github_ref=args.external_github_ref,
        required_ciks=required_ciks,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "output": str(args.output),
        "candidate_source_count": result["candidate_source_count"],
        "recovery_status": result["recovery_status"],
        "research_only": True,
    }, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
