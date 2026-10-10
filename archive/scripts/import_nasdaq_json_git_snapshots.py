"""Import selected immutable Nasdaq JSON snapshots from a Git repository.

The supported JSON is the raw-list shape used by rreichel3/US-Stock-Symbols:
an array of objects containing ``symbol`` and ``name``.  A commit's author
date is used conservatively as the observation date.  The importer never
backdates a snapshot and defaults to dry-run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.conf import PROJECT_PATH


DEFAULT_SOURCE_PATH = "nasdaq/nasdaq_full_tickers.json"
DEFAULT_SNAPSHOT_DIR = Path(PROJECT_PATH) / "stocks_list_dir/nasdaq/snapshots"
DEFAULT_OUTPUT = (
    Path(PROJECT_PATH)
    / "output/data_provenance/us_stock_symbols_snapshot_import_2026-08-08.json"
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _run_git(repository: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", *args], cwd=repository, check=True, capture_output=True
    )
    return result.stdout


def import_snapshots(
    *,
    repository: str | Path,
    dates: list[str],
    source_path: str = DEFAULT_SOURCE_PATH,
    snapshot_dir: str | Path = DEFAULT_SNAPSHOT_DIR,
    output: str | Path = DEFAULT_OUTPUT,
    minimum_rows: int = 1000,
    apply: bool = False,
) -> dict:
    repository = Path(repository).resolve()
    snapshot_dir, output = Path(snapshot_dir), Path(output)
    if not (repository / ".git").exists():
        raise ValueError(f"Not a Git repository: {repository}")
    remote = _run_git(repository, "remote", "get-url", "origin").decode().strip()
    records: list[dict] = []
    for requested_date in sorted(set(dates)):
        commit_line = _run_git(
            repository,
            "log",
            "-1",
            "--format=%H|%ad",
            "--date=short",
            f"--until={requested_date}T23:59:59",
            "--",
            source_path,
        ).decode().strip()
        if not commit_line:
            records.append({
                "requested_date": requested_date,
                "status": "NO_COMMIT_ON_OR_BEFORE_DATE",
            })
            continue
        commit, observed_at = commit_line.split("|", 1)
        payload = _run_git(repository, "show", f"{commit}:{source_path}")
        document = json.loads(payload)
        if not isinstance(document, list):
            raise ValueError(f"{commit}: expected a JSON array")
        frame = pd.DataFrame(document)
        if not {"symbol", "name"}.issubset(frame.columns):
            raise ValueError(f"{commit}: missing symbol/name columns")
        normalized = frame.rename(columns={"symbol": "Symbol", "name": "Name"})
        normalized["Symbol"] = normalized["Symbol"].astype(str).str.upper().str.strip()
        normalized = normalized.loc[
            normalized["Symbol"].ne("") & normalized["Name"].notna()
        ].drop_duplicates("Symbol")
        if len(normalized) < minimum_rows:
            raise ValueError(f"{commit}: only {len(normalized)} rows")
        normalized = normalized[["Symbol", "Name"]].sort_values("Symbol")
        normalized["Source Repository"] = remote
        normalized["Source Commit"] = commit
        normalized["Source File"] = source_path
        normalized["Observed At"] = observed_at
        target = snapshot_dir / f"nasdaq_listed_{observed_at}.csv"
        record = {
            "requested_date": requested_date,
            "observed_at": observed_at,
            "source_repository": remote,
            "source_commit": commit,
            "source_path": source_path,
            "source_payload_sha256": _sha256(payload),
            "rows": int(len(normalized)),
            "snapshot": str(target),
            "status": "DRY_RUN_ELIGIBLE",
        }
        if apply:
            snapshot_dir.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(target.suffix + ".tmp")
            normalized.to_csv(temporary, index=False)
            os.replace(temporary, target)
            record["snapshot_sha256"] = _sha256(target.read_bytes())
            record["status"] = "IMPORTED"
        records.append(record)
    report = {
        "schema_version": 1,
        "research_only": True,
        "source_description": (
            "Selected immutable Git commits of the raw Nasdaq list mirrored "
            "by rreichel3/US-Stock-Symbols; commit date is the conservative "
            "observation date."
        ),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "applied": bool(apply),
        "source_repository": remote,
        "source_path": source_path,
        "records": records,
        "formal_financial_files_modified": False,
        "validation_artifacts_modified": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2), encoding="utf-8")
    os.replace(temporary, output)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--dates", required=True, help="Comma-separated dates")
    parser.add_argument("--source-path", default=DEFAULT_SOURCE_PATH)
    parser.add_argument("--snapshot-dir", default=str(DEFAULT_SNAPSHOT_DIR))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--minimum-rows", type=int, default=1000)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    report = import_snapshots(
        repository=args.repository,
        dates=[item.strip() for item in args.dates.split(",") if item.strip()],
        source_path=args.source_path,
        snapshot_dir=args.snapshot_dir,
        output=args.output,
        minimum_rows=args.minimum_rows,
        apply=args.apply,
    )
    counts = pd.Series([row["status"] for row in report["records"]]).value_counts()
    print(json.dumps({"counts": counts.to_dict(), "records": report["records"]}, indent=2))


if __name__ == "__main__":
    main()
