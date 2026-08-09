"""Append SEC-verified successor-ticker tails from an official Stooq archive.

The historical and successor tickers must resolve to one SEC CIK and the
successor must be the issuer's only current ticker.  Stooq successor history
must independently agree with the existing historical-ticker file for at
least 20 sessions.  Existing dates are never replaced and the tail is capped
at the historical ticker's last point-in-time universe membership.  This is a
dry run unless ``--apply`` is supplied.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import zipfile

import pandas as pd

from scripts.historicaldata_price_import import (
    PRICE_COLUMNS,
    _atomic_write,
    _atomic_write_json,
    _frame_sha256,
    _member_sha256,
    _normalize_split_scale,
    _read_local,
    _read_stooq_member,
    _sha256,
    _stooq_member_identity,
    _validate_overlap,
)
from scripts.sec_alias_price_import import _candidates, _membership_ends
from src.conf import CLEANED_PRICE_DATA_DIR, PROJECT_PATH


DEFAULT_PROBE = Path(PROJECT_PATH) / (
    "output/data_provenance/sec_ticker_transition_probe_remaining_2026-08-08.json"
)
DEFAULT_AUDIT = Path(PROJECT_PATH) / "output/historical_data_audit.json"
DEFAULT_OUTPUT = Path(PROJECT_PATH) / (
    "output/data_provenance/sec_stooq_alias_price_import.json"
)
LICENSE_EVIDENCE = Path(PROJECT_PATH) / (
    "output/data_provenance/stooq_license_evidence_2026-08-08.json"
)


def import_aliases(
    archive_path: str | Path,
    *,
    probe_path: str | Path = DEFAULT_PROBE,
    audit_path: str | Path = DEFAULT_AUDIT,
    price_dir: str | Path = CLEANED_PRICE_DATA_DIR,
    output: str | Path = DEFAULT_OUTPUT,
    start: str = "2021-01-01",
    end: str = "2026-07-17",
    apply: bool = False,
    terminal_tail: bool = False,
    allow_multiple_successors: bool = False,
    successor_overrides: dict[str, str] | None = None,
) -> dict:
    archive_path, probe_path, audit_path = (
        Path(archive_path), Path(probe_path), Path(audit_path)
    )
    price_dir, output = Path(price_dir), Path(output)
    probe = json.loads(probe_path.read_text(encoding="utf-8"))
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    candidates = _candidates(
        probe, allow_multiple_successors=allow_multiple_successors
    )
    overrides = {
        str(key).upper().strip(): str(value).upper().strip()
        for key, value in (successor_overrides or {}).items()
    }
    if overrides:
        candidates = [
            candidate for candidate in candidates
            if candidate["historical_ticker"] in overrides
            and overrides[candidate["historical_ticker"]]
            == candidate["successor_ticker"]
        ]
    membership_ends = _membership_ends(audit)
    terminal_rows = {
        str(row["ticker"]).strip().upper(): row
        for row in audit.get("unresolved_terminal_return_histories") or []
        if isinstance(row, dict) and row.get("ticker")
    }
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    archive_digest = _sha256(archive_path)
    records: list[dict] = []
    report = {
        "schema_version": 1,
        "research_only": True,
        "status": "IN_PROGRESS",
        "applied": bool(apply),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "start": start,
        "end": end,
        "archive_path": str(archive_path),
        "archive_size_bytes": archive_path.stat().st_size,
        "archive_sha256": archive_digest,
        "source_url": "https://stooq.com/db/d/?b=d_us_txt",
        "license_evidence_path": str(LICENSE_EVIDENCE),
        "license_evidence_sha256": (
            _sha256(LICENSE_EVIDENCE) if LICENSE_EVIDENCE.exists() else None
        ),
        "probe_path": str(probe_path),
        "probe_sha256": _sha256(probe_path),
        "audit_path": str(audit_path),
        "audit_sha256": _sha256(audit_path),
        "formal_financial_files_modified": False,
        "terminal_returns_modified": False,
        "tail_mode": "terminal" if terminal_tail else "membership",
        "allow_multiple_successors": bool(allow_multiple_successors),
        "successor_overrides": overrides,
        "candidate_count": len(candidates),
        "records": records,
    }
    _atomic_write_json(output, report)

    def checkpoint(record: dict) -> None:
        records.append(record)
        report["checkpointed_records"] = len(records)
        report["last_checkpoint_ticker"] = record["historical_ticker"]
        report["last_checkpoint_at_utc"] = datetime.now(timezone.utc).isoformat()
        _atomic_write_json(output, report)

    with zipfile.ZipFile(archive_path) as archive:
        members_by_ticker: dict[str, list[str]] = {}
        for member in archive.namelist():
            identity = _stooq_member_identity(member)
            if identity is not None:
                members_by_ticker.setdefault(identity[1], []).append(member)
        for candidate in candidates:
            historical = candidate["historical_ticker"]
            successor = candidate["successor_ticker"]
            record = dict(candidate)
            members = members_by_ticker.get(successor, [])
            record["members"] = members
            membership_end = membership_ends.get(historical)
            record["last_membership_date"] = membership_end
            terminal_reference = terminal_rows.get(historical)
            record["terminal_reference"] = terminal_reference
            local_path = price_dir / f"{historical.lower()}.csv"
            record["price_path"] = str(local_path)
            has_audit_reference = (
                terminal_reference is not None if terminal_tail else bool(membership_end)
            )
            if not has_audit_reference or not local_path.exists():
                record["status"] = "REJECT_MISSING_AUDIT_OR_LOCAL_REFERENCE"
                checkpoint(record)
                continue
            if not members:
                record["status"] = "SOURCE_MISSING"
                checkpoint(record)
                continue
            local = _read_local(local_path)
            record.update({
                "local_rows_before": int(len(local)),
                "local_sha256_before": _sha256(local_path),
                "local_last_date": local["date"].max().strftime("%Y-%m-%d"),
            })
            if (
                terminal_tail
                and record["local_last_date"]
                != str(terminal_reference.get("last_price_date"))
            ):
                record["status"] = "REJECT_STALE_TERMINAL_AUDIT"
                checkpoint(record)
                continue
            selected = []
            validations = []
            for member in members:
                source = _read_stooq_member(archive, member, successor)
                source = source.loc[source["date"].between(start_ts, end_ts)]
                raw_validation = _validate_overlap(local, source)
                validation = raw_validation
                normalization = None
                if not validation["passed"]:
                    normalized, normalization = _normalize_split_scale(local, source)
                    if normalized is not None:
                        source = normalized
                        validation = normalization["normalized_cross_validation"]
                validations.append({
                    "member": member,
                    "member_crc32": f"{archive.getinfo(member).CRC:08x}",
                    "member_size_bytes": archive.getinfo(member).file_size,
                    "member_sha256": _member_sha256(archive, member),
                    "source_first_date": (
                        source["date"].min().strftime("%Y-%m-%d")
                        if not source.empty else None
                    ),
                    "source_last_date": (
                        source["date"].max().strftime("%Y-%m-%d")
                        if not source.empty else None
                    ),
                    "raw_cross_validation": raw_validation,
                    "cross_validation": validation,
                    "scale_normalization": normalization,
                })
                if validation["passed"]:
                    selected.append(source)
            record["member_validations"] = validations
            if not selected:
                record["status"] = "REJECT_CROSS_VALIDATION"
                checkpoint(record)
                continue
            source = (
                pd.concat(selected, ignore_index=True)
                .drop_duplicates("date", keep="last")
                .sort_values("date")
            )
            source["ticker"] = historical
            local_last = local["date"].max()
            tail_end = (
                end_ts
                if terminal_tail
                else min(pd.Timestamp(membership_end), end_ts)
            )
            missing = source.loc[
                source["date"].gt(local_last)
                & source["date"].le(tail_end)
                & ~source["date"].isin(local["date"])
            ].copy().sort_values("date")
            if (
                terminal_tail
                and not missing.empty
                and (missing["date"].min() - local_last).days > 7
            ):
                record.update({
                    "status": "REJECT_NONCONTIGUOUS_TERMINAL_TAIL",
                    "first_source_tail_date": missing["date"].min().strftime(
                        "%Y-%m-%d"
                    ),
                    "terminal_tail_gap_days": int(
                        (missing["date"].min() - local_last).days
                    ),
                })
                checkpoint(record)
                continue
            record.update({
                "rows_missing": int(len(missing)),
                "first_missing_date": (
                    missing["date"].min().strftime("%Y-%m-%d")
                    if not missing.empty else None
                ),
                "last_missing_date": (
                    missing["date"].max().strftime("%Y-%m-%d")
                    if not missing.empty else None
                ),
                "missing_dates": missing["date"].dt.strftime("%Y-%m-%d").tolist(),
                "missing_rows_sha256": (
                    _frame_sha256(missing) if not missing.empty else None
                ),
            })
            if apply and not missing.empty:
                merged = (
                    pd.concat([local, missing], ignore_index=True)
                    .drop_duplicates("date", keep="first")
                    .sort_values("date")
                )
                _atomic_write(local_path, merged)
                persisted = _read_local(local_path)
                record.update({
                    "status": "UPDATED",
                    "local_rows_after": int(len(persisted)),
                    "local_sha256_after": _sha256(local_path),
                    "persisted_appended_rows_sha256": _frame_sha256(
                        persisted.loc[persisted["date"].isin(missing["date"])]
                    ),
                })
            else:
                record["status"] = (
                    "DRY_RUN_ELIGIBLE" if not missing.empty else "NO_NEW_ROWS"
                )
                record["local_rows_after"] = int(len(local))
                record["local_sha256_after"] = record["local_sha256_before"]
            checkpoint(record)
    report["status"] = "COMPLETE"
    report["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(output, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True)
    parser.add_argument("--probe", default=str(DEFAULT_PROBE))
    parser.add_argument("--audit", default=str(DEFAULT_AUDIT))
    parser.add_argument("--price-dir", default=str(CLEANED_PRICE_DATA_DIR))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--start", default="2021-01-01")
    parser.add_argument("--end", default="2026-07-17")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--terminal-tail",
        action="store_true",
        help=(
            "Append a strictly contiguous same-CIK successor tail for an "
            "unresolved terminal history instead of capping at PIT membership"
        ),
    )
    parser.add_argument(
        "--allow-multiple-successors",
        action="store_true",
        help=(
            "Dry-run every different current SEC ticker for a unique CIK. "
            "Use --successor-overrides before applying a validated choice."
        ),
    )
    parser.add_argument(
        "--successor-overrides",
        help="Comma-separated HISTORICAL=SUCCESSOR choices validated by dry-run",
    )
    args = parser.parse_args()
    overrides = {}
    if args.successor_overrides:
        for item in args.successor_overrides.split(","):
            historical, separator, successor = item.partition("=")
            if not separator or not historical.strip() or not successor.strip():
                parser.error(f"invalid successor override: {item}")
            overrides[historical] = successor
    if args.apply and args.allow_multiple_successors and not overrides:
        parser.error(
            "--apply with --allow-multiple-successors requires "
            "--successor-overrides"
        )
    report = import_aliases(
        args.archive,
        probe_path=args.probe,
        audit_path=args.audit,
        price_dir=args.price_dir,
        output=args.output,
        start=args.start,
        end=args.end,
        apply=args.apply,
        terminal_tail=args.terminal_tail,
        allow_multiple_successors=args.allow_multiple_successors,
        successor_overrides=overrides,
    )
    counts = pd.Series([row["status"] for row in report["records"]]).value_counts()
    print(json.dumps({"candidate_count": report["candidate_count"], "counts": counts.to_dict()}, indent=2))


if __name__ == "__main__":
    main()
