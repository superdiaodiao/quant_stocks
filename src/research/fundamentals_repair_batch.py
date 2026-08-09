"""Run one bounded Company Facts repair batch and measure selector-level yield."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile

import pandas as pd

from src.io.fundamentals_update import (
    SEC_COMPANYFACTS_CACHE_DIR,
    cached_companyfacts_symbol_payload_profiles,
    load_refresh_priority_file,
    update_fundamentals,
)
from src.research.can_slim_validation import (
    run_can_slim_validation,
    write_can_slim_validation_outputs,
)


DEFAULT_PRIORITY_FILE = Path(
    "output/can_slim_technical_candidate_financial_priorities.csv"
)
DEFAULT_AUDIT_DIR = Path("output/fundamentals_repair_batches")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def coverage_snapshot(coverage: dict) -> dict:
    missing_symbols = coverage.get("missing_financial_symbols", [])
    return {
        "missing_financial_observations": int(
            coverage["missing_financial_observations"]
        ),
        "missing_financial_symbol_count": int(
            coverage.get(
                "missing_financial_symbol_count", len(missing_symbols)
            )
        ),
        "financial_coverage": float(coverage["financial_coverage"]),
    }


def repair_batch_decision(
    requested_ciks: int,
    recovered_observations: int,
    failure_count: int,
) -> dict:
    yield_per_cik = (
        recovered_observations / requested_ciks
        if requested_ciks else None
    )
    if failure_count:
        action = "REVIEW_FAILURES"
    elif not requested_ciks:
        action = "NO_FETCH_WORK"
    elif yield_per_cik >= 2:
        action = "CONTINUE_SAME_BATCH_SIZE"
    elif yield_per_cik >= 0.5:
        action = "REDUCE_BATCH_SIZE"
    else:
        action = "PAUSE_FETCH_AND_REVIEW_SOURCES"
    return {
        "recovered_observations_per_requested_cik": yield_per_cik,
        "recommended_action": action,
        "thresholds": {
            "continue_same_batch_size": 2.0,
            "reduce_batch_size": 0.5,
        },
    }


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(
            json.dumps(value, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _repair_batch_index_row(audit: dict) -> dict:
    try:
        return {
            "batch_id": audit["batch_id"],
            "as_of": audit["as_of"],
            "requested_ciks": audit["update"]["requested_ciks"],
            "failure_count": audit["update"]["failure_count"],
            "missing_before": audit["before"][
                "missing_financial_observations"
            ],
            "missing_after": audit["after"][
                "missing_financial_observations"
            ],
            "recovered_observations": audit["delta"][
                "recovered_observations"
            ],
            "yield_per_cik": audit["decision"][
                "recovered_observations_per_requested_cik"
            ],
            "recommended_action": audit["decision"][
                "recommended_action"
            ],
            "audit_path": audit["audit_path"],
        }
    except (KeyError, TypeError) as exc:
        raise RuntimeError("Invalid fundamentals repair batch audit") from exc


def _load_repair_batch_audits(audit_dir: Path) -> list[dict]:
    """Load complete atomic JSON audits, which are the authoritative log."""
    audits = []
    for path in sorted(Path(audit_dir).glob("*.json")):
        try:
            audit = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Invalid fundamentals repair batch audit {path}: {exc}"
            ) from exc
        row = _repair_batch_index_row(audit)
        if str(row["batch_id"]) != path.stem:
            raise RuntimeError(
                f"Repair batch id does not match audit filename {path}"
            )
        audits.append(audit)
    return audits


def _update_index(audit_dir: Path, audit: dict) -> Path:
    """Rebuild the derived CSV index without losing unindexed JSON audits."""
    audit_dir = Path(audit_dir)
    path = audit_dir / "index.csv"
    old = pd.read_csv(path) if path.exists() else pd.DataFrame()
    rows_by_id = {
        str(row["batch_id"]): row
        for row in old.to_dict("records")
        if str(row.get("batch_id", "")).strip()
    }
    for recorded in _load_repair_batch_audits(audit_dir):
        row = _repair_batch_index_row(recorded)
        rows_by_id[str(row["batch_id"])] = row
    current = _repair_batch_index_row(audit)
    rows_by_id[str(current["batch_id"])] = current
    combined = pd.DataFrame(
        [rows_by_id[batch_id] for batch_id in sorted(rows_by_id)]
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=audit_dir
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        combined.to_csv(temporary, index=False)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def enforce_previous_batch_decision(
    audit_dir: Path,
    requested_limit: int,
    *,
    override_stop: bool,
) -> None:
    """Prevent blind continuation after measured low-yield batches."""
    if override_stop:
        return
    audits = _load_repair_batch_audits(audit_dir)
    if audits:
        latest_audit = audits[-1]
        latest = _repair_batch_index_row(latest_audit)
    else:
        path = Path(audit_dir) / "index.csv"
        if not path.exists():
            return
        history = pd.read_csv(path)
        if history.empty:
            return
        latest = history.iloc[-1].to_dict()
    action = str(latest["recommended_action"])
    if action in {"PAUSE_FETCH_AND_REVIEW_SOURCES", "REVIEW_FAILURES"}:
        raise RuntimeError(
            f"Previous repair batch requires {action}; use "
            "--override-stop only after reviewing sources"
        )
    if (
        action == "REDUCE_BATCH_SIZE"
        and requested_limit >= int(latest["requested_ciks"])
    ):
        raise RuntimeError(
            "Previous repair batch requires a smaller --limit than "
            f"{int(latest['requested_ciks'])}"
        )


def run_repair_batch(
    *,
    as_of: date,
    limit: int,
    workers: int,
    priority_file: Path,
    audit_dir: Path,
    override_stop: bool = False,
) -> dict:
    if limit <= 0:
        raise ValueError("limit must be a positive integer")
    enforce_previous_batch_decision(
        audit_dir, limit, override_stop=override_stop
    )
    before_coverage = json.loads(
        Path(
            "output/can_slim_technical_candidate_financial_coverage.json"
        ).read_text(encoding="utf-8")
    )
    before = coverage_snapshot(before_coverage)
    priority = load_refresh_priority_file(priority_file)
    update = update_fundamentals(
        as_of,
        workers=workers,
        limit=limit,
        cache_missing_only=True,
        refresh_priority=priority,
    )
    validation = run_can_slim_validation()
    write_can_slim_validation_outputs(validation)
    after_coverage = validation[6]
    after = coverage_snapshot(after_coverage)
    requested_tickers = update.get("requested_tickers", [])
    profiles = cached_companyfacts_symbol_payload_profiles(
        SEC_COMPANYFACTS_CACHE_DIR,
        requested_tickers,
    )
    profile_counts = Counter(
        (profiles.get(ticker) or {}).get("profile", "NOT_CACHED")
        for ticker in requested_tickers
    )
    recovered = (
        before["missing_financial_observations"]
        - after["missing_financial_observations"]
    )
    requested_ciks = int(update.get("requested_ciks", 0))
    failures = update.get("failures", [])
    batch_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    audit_dir = Path(audit_dir)
    audit_path = audit_dir / f"{batch_id}.json"
    audit = {
        "batch_id": batch_id,
        "as_of": as_of.isoformat(),
        "purpose": "selector_level_companyfacts_repair_yield",
        "priority_file": str(priority_file),
        "priority_file_sha256": _sha256(priority_file),
        "before": before,
        "after": after,
        "delta": {
            "recovered_observations": recovered,
            "resolved_missing_symbols": (
                before["missing_financial_symbol_count"]
                - after["missing_financial_symbol_count"]
            ),
            "financial_coverage_change": (
                after["financial_coverage"] - before["financial_coverage"]
            ),
        },
        "update": {
            "requested_ciks": requested_ciks,
            "requested_ticker_count": len(requested_tickers),
            "requested_tickers": requested_tickers,
            "failure_count": len(failures),
            "failures": failures,
            "raw_cache_profile_counts_after": dict(profile_counts),
            "cached_symbol_count_before": update.get(
                "cached_symbol_count_before"
            ),
            "cached_symbol_count_after": update.get(
                "cached_symbol_count_after"
            ),
        },
        "decision": repair_batch_decision(
            requested_ciks, recovered, len(failures)
        ),
        "strategy_fingerprint": validation[5]["input_fingerprints"][
            "strategy_code"
        ]["sha256"],
        "quarterly_fundamentals_fingerprint": validation[5][
            "input_fingerprints"
        ]["quarterly_fundamentals"]["sha256"],
        "audit_path": str(audit_path),
    }
    _atomic_json(audit_path, audit)
    audit["index_path"] = str(_update_index(audit_dir, audit))
    return audit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", default=date.today().isoformat())
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--priority-file", type=Path, default=DEFAULT_PRIORITY_FILE
    )
    parser.add_argument("--audit-dir", type=Path, default=DEFAULT_AUDIT_DIR)
    parser.add_argument(
        "--override-stop",
        action="store_true",
        help=(
            "Run despite the previous batch's pause/failure decision. "
            "Use only after reviewing source or parser changes."
        ),
    )
    args = parser.parse_args()
    result = run_repair_batch(
        as_of=date.fromisoformat(args.as_of),
        limit=args.limit,
        workers=args.workers,
        priority_file=args.priority_file,
        audit_dir=args.audit_dir,
        override_stop=args.override_stop,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
