"""Audit whether historical universe snapshots have reproducible source lineage."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
import json
from pathlib import Path
import re

import pandas as pd

from src.conf import NASDAQ_INDEX_FILE, PROJECT_PATH
from src.research.can_slim import scheduled_signal_dates
from src.research.universe_history import snapshot_directory


COMMIT_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
FLOATING_GIT_REF_RE = re.compile(
    r"/(?:HEAD|main|master)/", re.IGNORECASE
)
IMMUTABLE_RAW_GITHUB_RE = re.compile(
    r"https?://(?:raw|media)\.githubusercontent\.com/"
    r"(?:media/)?([^/]+/[^/]+)/([0-9a-f]{40})/",
    re.IGNORECASE,
)
REPRODUCIBLE_STATUSES = {
    "IMMUTABLE_GIT_COMMIT",
    "IMMUTABLE_WEB_ARCHIVE",
}


def _single_value(frame: pd.DataFrame, column: str) -> tuple[str | None, bool]:
    if column not in frame:
        return None, True
    values = (
        frame[column]
        .dropna()
        .astype(str)
        .str.strip()
    )
    values = values.loc[values.ne("")].unique().tolist()
    return (values[0] if len(values) == 1 else None), len(values) <= 1


def _load_listings_git_manifest(directory: Path) -> dict[str, dict]:
    path = directory / "listings_git_import_manifest.json"
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    repository = payload.get("source_repository")
    return {
        str(item["observed_at"]): {
            "source_repository": item.get(
                "source_repository", repository
            ),
            "source_file": item.get("source_path"),
            "source_commit": item.get("commit"),
            "manifest": path.name,
        }
        for item in payload.get("imported", [])
        if item.get("observed_at")
    }


def _classify_source(
    source_file: str | None,
    source_repository: str | None,
    source_commit: str | None,
    metadata_consistent: bool,
) -> str:
    if not metadata_consistent:
        return "INCONSISTENT_METADATA"
    source_file = source_file or ""
    source_repository = source_repository or ""
    source_commit = source_commit or ""
    remote_repository = source_repository.startswith(
        ("https://", "http://", "git@")
    )
    if COMMIT_RE.fullmatch(source_commit) and remote_repository:
        return "IMMUTABLE_GIT_COMMIT"
    if (
        source_file.startswith("https://data.commoncrawl.org/crawl-data/")
        and "#offset=" in source_file
        and "&length=" in source_file
        and "&timestamp=" in source_file
    ):
        return "IMMUTABLE_WEB_ARCHIVE"
    if FLOATING_GIT_REF_RE.search(source_file):
        return "FLOATING_GIT_REF"
    if source_file.startswith(("https://", "http://")):
        return "REMOTE_SOURCE_WITHOUT_IMMUTABLE_VERSION"
    if source_file.startswith(("/", "./", "../")) or (
        source_file and "://" not in source_file
    ):
        return "LOCAL_OR_RELATIVE_SOURCE"
    if source_commit and not remote_repository:
        return "COMMIT_WITHOUT_PUBLIC_REPOSITORY"
    return "MISSING_SOURCE_LINEAGE"


def audit_snapshot_provenance(
    directory: str | Path,
    signal_dates: list[pd.Timestamp] | None = None,
) -> tuple[pd.DataFrame, dict]:
    """Classify source lineage and show which snapshots affect signals."""
    root = Path(directory)
    normalized_signals = [
        pd.Timestamp(item) for item in (
            [] if signal_dates is None else list(signal_dates)
        )
    ]
    manifest = _load_listings_git_manifest(root)
    rows = []
    for path in sorted(root.glob("nasdaq_listed_*.csv")):
        observed_at = path.stem.removeprefix("nasdaq_listed_")
        try:
            observed_ts = pd.Timestamp(observed_at)
        except ValueError:
            continue
        frame = pd.read_csv(path)
        source_file, file_consistent = _single_value(
            frame, "Source File"
        )
        source_repository, repository_consistent = _single_value(
            frame, "Source Repository"
        )
        source_commit, commit_consistent = _single_value(
            frame, "Source Commit"
        )
        manifest_row = manifest.get(observed_at, {})
        source_file = source_file or manifest_row.get("source_file")
        source_repository = (
            source_repository
            or manifest_row.get("source_repository")
        )
        source_commit = (
            source_commit or manifest_row.get("source_commit")
        )
        raw_github = IMMUTABLE_RAW_GITHUB_RE.match(source_file or "")
        if raw_github is not None:
            source_repository = (
                source_repository
                or f"https://github.com/{raw_github.group(1)}"
            )
            source_commit = source_commit or raw_github.group(2)
        metadata_consistent = bool(
            file_consistent
            and repository_consistent
            and commit_consistent
        )
        status = _classify_source(
            source_file,
            source_repository,
            source_commit,
            metadata_consistent,
        )
        rows.append({
            "observed_at": observed_ts,
            "snapshot": path.name,
            "rows": len(frame),
            "source_status": status,
            "source_reproducible": status in REPRODUCIBLE_STATUSES,
            "source_file": source_file,
            "source_repository": source_repository,
            "source_commit": source_commit,
            "manifest": manifest_row.get("manifest"),
            "metadata_consistent": metadata_consistent,
            "signals_using_snapshot": 0,
            "first_signal_date": None,
            "last_signal_date": None,
        })
    details = pd.DataFrame(rows)
    if details.empty:
        return details, {
            "snapshot_count": 0,
            "signal_count": len(normalized_signals),
            "source_status_counts": {},
            "signal_source_status_counts": {},
            "all_signal_snapshots_reproducible": False,
            "nonreproducible_signal_dates": [],
        }
    details = details.sort_values("observed_at").reset_index(drop=True)
    usage: dict[pd.Timestamp, list[pd.Timestamp]] = {}
    available = details["observed_at"].tolist()
    for signal in normalized_signals:
        eligible = [item for item in available if item <= signal]
        if not eligible:
            continue
        usage.setdefault(max(eligible), []).append(signal)
    for index, row in details.iterrows():
        used = usage.get(row["observed_at"], [])
        if used:
            details.at[index, "signals_using_snapshot"] = len(used)
            details.at[index, "first_signal_date"] = min(used)
            details.at[index, "last_signal_date"] = max(used)
    signal_status_counts = Counter()
    nonreproducible_signal_dates = []
    for row in details.itertuples(index=False):
        if not row.signals_using_snapshot:
            continue
        signal_status_counts[row.source_status] += (
            row.signals_using_snapshot
        )
        if not row.source_reproducible:
            nonreproducible_signal_dates.extend(
                usage[row.observed_at]
            )
    report = {
        "snapshot_count": len(details),
        "signal_count": len(normalized_signals),
        "source_status_counts": dict(sorted(Counter(
            details["source_status"]
        ).items())),
        "signal_source_status_counts": dict(
            sorted(signal_status_counts.items())
        ),
        "snapshots_used_by_signals": int(
            details["signals_using_snapshot"].gt(0).sum()
        ),
        "reproducible_snapshots_used_by_signals": int(
            (
                details["signals_using_snapshot"].gt(0)
                & details["source_reproducible"]
            ).sum()
        ),
        "all_signal_snapshots_reproducible": bool(
            normalized_signals
            and not nonreproducible_signal_dates
        ),
        "nonreproducible_signal_dates": [
            item.strftime("%Y-%m-%d")
            for item in sorted(nonreproducible_signal_dates)
        ],
        "interpretation": (
            "Source reproducibility is a lineage diagnostic, not permission "
            "to backdate a snapshot. A later immutable commit may reproduce "
            "a captured file only when independent embedded/capture timing "
            "supports the earlier observed_at date."
        ),
    }
    return details, report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2021-01-01")
    parser.add_argument("--end", default="2026-07-17")
    parser.add_argument(
        "--output-prefix",
        default="output/nasdaq_universe_snapshot_provenance",
    )
    args = parser.parse_args()
    benchmark = pd.read_csv(
        NASDAQ_INDEX_FILE, usecols=["date"], parse_dates=["date"]
    )
    signals = scheduled_signal_dates(
        benchmark["date"], args.start, args.end, "monthly"
    )
    details, report = audit_snapshot_provenance(
        snapshot_directory(), signals
    )
    prefix = Path(PROJECT_PATH) / args.output_prefix
    prefix.parent.mkdir(parents=True, exist_ok=True)
    details.to_csv(prefix.with_suffix(".csv"), index=False)
    prefix.with_suffix(".json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
