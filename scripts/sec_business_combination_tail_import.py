"""Append an explicitly sourced cross-CIK business-combination price tail.

Unlike a same-issuer ticker rename, a SPAC combination commonly moves the
listed security to a new SEC CIK.  This importer therefore requires the exact
closing filing, caller-supplied phrases that bind the conversion and successor
ticker, a hash-bound successor series (official Stooq archive or cached Nasdaq
snapshot), and the exchange ratio.  It is a dry run unless ``--apply`` is supplied.
"""

from __future__ import annotations

import argparse
import gzip
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
    _read_local,
    _read_stooq_member,
    _sha256,
    _validate_overlap,
)
from scripts.sec_terminal_filing_evidence import _filing_text
from src.conf import CLEANED_PRICE_DATA_DIR, PROJECT_PATH


def import_tail(
    *,
    historical_ticker: str,
    successor_ticker: str,
    effective_date: str,
    source_start_date: str | None = None,
    allow_pre_effective_source_overlap: bool = False,
    exchange_ratio: float,
    sec_cache_path: str | Path,
    expected_filing_phrases: list[str],
    archive_path: str | Path | None,
    archive_member: str | None,
    source_snapshot_path: str | Path | None = None,
    audit_path: str | Path,
    output: str | Path,
    price_dir: str | Path = CLEANED_PRICE_DATA_DIR,
    end: str = "2026-07-17",
    apply: bool = False,
) -> dict:
    historical = historical_ticker.upper().strip()
    successor = successor_ticker.upper().strip()
    if not historical or not successor or exchange_ratio <= 0:
        raise ValueError("tickers and a positive exchange ratio are required")
    sec_cache_path = Path(sec_cache_path)
    archive_path = Path(archive_path) if archive_path else None
    source_snapshot_path = Path(source_snapshot_path) if source_snapshot_path else None
    audit_path, output, price_dir = Path(audit_path), Path(output), Path(price_dir)
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    terminal = next(
        (
            row
            for row in audit.get("unresolved_terminal_return_histories") or []
            if str(row.get("ticker", "")).upper() == historical
        ),
        None,
    )
    if terminal is None:
        raise ValueError(f"{historical} is not unresolved in the supplied audit")

    with gzip.open(sec_cache_path, "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    filing_payload = bytes.fromhex(envelope["payload_hex"])
    filing_text = _filing_text(filing_payload)
    missing_phrases = [
        phrase
        for phrase in expected_filing_phrases
        if phrase.lower() not in filing_text.lower()
    ]
    if missing_phrases:
        raise ValueError(f"SEC filing is missing expected phrases: {missing_phrases}")

    local_path = price_dir / f"{historical.lower()}.csv"
    local = _read_local(local_path)
    local_last = local["date"].max()
    if local_last.strftime("%Y-%m-%d") != terminal["last_price_date"]:
        raise ValueError("historical audit is stale for the local price file")

    effective = pd.Timestamp(effective_date)
    source_start = pd.Timestamp(source_start_date or effective_date)
    if source_start < effective and not allow_pre_effective_source_overlap:
        raise ValueError("source start cannot precede the sourced effective date")
    source_provenance: dict[str, object]
    if source_snapshot_path:
        snapshot = json.loads(source_snapshot_path.read_text(encoding="utf-8"))
        if snapshot.get("ticker") != successor:
            raise ValueError("source snapshot ticker does not match successor")
        archive_source = pd.DataFrame(snapshot.get("records") or [])
        archive_source["date"] = pd.to_datetime(archive_source["date"], errors="raise")
        archive_source = archive_source[PRICE_COLUMNS].sort_values("date")
        if _frame_sha256(archive_source) != snapshot.get("frame_sha256"):
            raise ValueError("source snapshot frame hash mismatch")
        source_provenance = {
            "source_snapshot_path": str(source_snapshot_path),
            "source_snapshot_sha256": _sha256(source_snapshot_path),
            "source_url": snapshot.get("source_url"),
            "source_frame_sha256": snapshot.get("frame_sha256"),
            "source_provider": snapshot.get("provider"),
        }
    else:
        if archive_path is None or not archive_member:
            raise ValueError("archive and archive member are required without a source snapshot")
        with zipfile.ZipFile(archive_path) as archive:
            archive_source = _read_stooq_member(archive, archive_member, successor)
            source_provenance = {
                "archive_path": str(archive_path),
                "archive_sha256": _sha256(archive_path),
                "archive_member": archive_member,
                "archive_member_sha256": _member_sha256(archive, archive_member),
                "archive_member_crc32": f"{archive.getinfo(archive_member).CRC:08x}",
            }
    pre_effective_overlap = None
    if source_start < effective:
        pre_effective_overlap = _validate_overlap(local, archive_source)
        if not pre_effective_overlap.get("passed"):
            raise ValueError(
                "pre-effective successor history does not validate against "
                "the historical ticker"
            )
    source = archive_source.loc[
        archive_source["date"].between(source_start, pd.Timestamp(end))
    ].copy()
    first = source["date"].min()
    gap_days = int((first - local_last).days)
    if first != source_start or not 0 < gap_days <= 7:
        raise ValueError("successor series is not contiguous with the sourced effective date")
    if source["date"].isin(local["date"]).any():
        raise ValueError("cross-CIK successor unexpectedly overlaps historical dates")

    tail = source.copy()
    for field in ("open", "high", "low", "close"):
        tail[field] = tail[field].astype(float) * exchange_ratio
    tail["volume"] = tail["volume"].astype(float) / exchange_ratio
    tail["ticker"] = historical
    tail = tail[PRICE_COLUMNS]
    report = {
        "schema_version": 1,
        "research_only": True,
        "status": "DRY_RUN_ELIGIBLE" if not apply else "UPDATED",
        "applied": bool(apply),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "historical_ticker": historical,
        "successor_ticker": successor,
        "effective_date": effective.strftime("%Y-%m-%d"),
        "source_start_date": source_start.strftime("%Y-%m-%d"),
        "pre_effective_source_overlap": pre_effective_overlap,
        "exchange_ratio": exchange_ratio,
        "terminal_reference": terminal,
        "terminal_tail_gap_days": gap_days,
        "rows_added": int(len(tail)),
        "first_added_date": tail["date"].min().strftime("%Y-%m-%d"),
        "last_added_date": tail["date"].max().strftime("%Y-%m-%d"),
        "appended_rows_sha256": _frame_sha256(tail),
        "price_path": str(local_path),
        "local_sha256_before": _sha256(local_path),
        "sec_cache_path": str(sec_cache_path),
        "sec_source_url": envelope["source_url"],
        "sec_payload_sha256": hashlib.sha256(filing_payload).hexdigest(),
        "expected_filing_phrases": expected_filing_phrases,
        **source_provenance,
        "formal_financial_files_modified": False,
        "terminal_returns_modified": False,
    }
    if apply:
        merged = (
            pd.concat([local, tail], ignore_index=True)
            .sort_values("date")
            .drop_duplicates("date", keep="first")
        )
        _atomic_write(local_path, merged[PRICE_COLUMNS])
        report["local_sha256_after"] = _sha256(local_path)
    else:
        report["local_sha256_after"] = report["local_sha256_before"]
    _atomic_write_json(output, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--historical", required=True)
    parser.add_argument("--successor", required=True)
    parser.add_argument("--effective-date", required=True)
    parser.add_argument(
        "--source-start-date",
        help="First successor row to append; defaults to the effective date",
    )
    parser.add_argument(
        "--allow-pre-effective-source-overlap",
        action="store_true",
        help=(
            "Allow a renamed archive series to start before the SEC effective "
            "date only after strict local OHLCV overlap validation"
        ),
    )
    parser.add_argument("--exchange-ratio", type=float, required=True)
    parser.add_argument("--sec-cache", required=True)
    parser.add_argument("--expected-filing-phrase", action="append", required=True)
    parser.add_argument("--archive")
    parser.add_argument("--archive-member")
    parser.add_argument("--source-snapshot")
    parser.add_argument("--audit", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--price-dir", default=str(CLEANED_PRICE_DATA_DIR))
    parser.add_argument("--end", default="2026-07-17")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    report = import_tail(
        historical_ticker=args.historical,
        successor_ticker=args.successor,
        effective_date=args.effective_date,
        source_start_date=args.source_start_date,
        allow_pre_effective_source_overlap=args.allow_pre_effective_source_overlap,
        exchange_ratio=args.exchange_ratio,
        sec_cache_path=args.sec_cache,
        expected_filing_phrases=args.expected_filing_phrase,
        archive_path=args.archive,
        archive_member=args.archive_member,
        source_snapshot_path=args.source_snapshot,
        audit_path=args.audit,
        output=args.output,
        price_dir=args.price_dir,
        end=args.end,
        apply=args.apply,
    )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
