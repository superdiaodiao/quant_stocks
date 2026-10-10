#!/usr/bin/env python3
"""Select the latest canonical shadow artifact from GitHub API payloads."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


ARTIFACT_PREFIX = "fixed-top3-shadow-"


def paginated_items(payload: dict | list, key: str) -> list[dict]:
    """Flatten either one GitHub API page or `gh api --slurp` pages."""
    pages = payload if isinstance(payload, list) else [payload]
    items = []
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get(key), list):
            raise ValueError(f"Invalid paginated GitHub payload for {key}")
        items.extend(
            item for item in page[key] if isinstance(item, dict)
        )
    return items


def select_shadow_artifact(
    workflow_runs: dict | list,
    artifacts: dict | list,
    *,
    current_run_id: int,
    default_branch: str,
) -> int | None:
    allowed_runs = [
        run
        for run in paginated_items(workflow_runs, "workflow_runs")
        if (
            int(run["id"]) != current_run_id
            and run.get("conclusion") == "success"
            and run.get("head_branch") == default_branch
        )
    ]
    if not allowed_runs:
        return None
    latest_run = max(
        allowed_runs,
        key=lambda run: (
            int(run.get("run_number") or run["id"]),
            int(run["id"]),
        ),
    )
    latest_run_id = int(latest_run["id"])
    canonical_name = f"{ARTIFACT_PREFIX}{latest_run_id}"
    matching = [
        artifact
        for artifact in paginated_items(artifacts, "artifacts")
        if (
            artifact.get("name") == canonical_name
            and int((artifact.get("workflow_run") or {}).get("id", -1))
            == latest_run_id
        )
    ]
    if not matching:
        raise RuntimeError(
            "Latest successful shadow run "
            f"{latest_run_id} has no canonical artifact; refusing to "
            "fall back to an older ledger."
        )
    available = [
        artifact for artifact in matching if not artifact.get("expired")
    ]
    if not available:
        raise RuntimeError(
            f"Latest canonical shadow artifact for run {latest_run_id} "
            "is expired; refusing to fall back to an older ledger."
        )
    latest = max(
        available,
        key=lambda artifact: (
            str(artifact.get("created_at", "")),
            int(artifact["id"]),
        ),
    )
    return int(latest["id"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-json", required=True)
    parser.add_argument("--artifacts-json", required=True)
    parser.add_argument("--current-run-id", required=True, type=int)
    parser.add_argument("--default-branch", required=True)
    args = parser.parse_args()
    selected = select_shadow_artifact(
        json.loads(Path(args.runs_json).read_text(encoding="utf-8")),
        json.loads(Path(args.artifacts_json).read_text(encoding="utf-8")),
        current_run_id=args.current_run_id,
        default_branch=args.default_branch,
    )
    if selected is not None:
        print(selected)


if __name__ == "__main__":
    main()
