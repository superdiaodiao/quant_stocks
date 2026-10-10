"""Append SEC-verified successor-ticker tails using Sina/AkShare prices.

The historical and successor ticker must resolve to one SEC CIK.  Sina prices
must overlap the existing historical ticker at a stable OHLC scale, and a
terminal tail must begin within seven calendar days of the local final row.
Raw responses and the exact AkShare decoder are SHA-bound in the report.  The
command is a dry run unless ``--apply`` is supplied.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from scripts.historicaldata_price_import import PRICE_COLUMNS, _frame_sha256, _sha256
from scripts.sec_alias_price_import import _candidates
from scripts.sina_historical_price_repair import (
    SOURCE_URL_TEMPLATE,
    _decoder_source,
    _load_or_fetch,
    _longest_stable_tail_validation,
    _parse_prices,
)
from scripts.yahoo_historical_price_repair import _read_prices
from src.conf import CLEANED_PRICE_DATA_DIR, PROJECT_PATH


DEFAULT_AUDIT = Path(PROJECT_PATH) / "output/historical_data_audit.json"
DEFAULT_CACHE = Path(PROJECT_PATH) / "output/data_provenance/sina_sec_alias_cache"
DEFAULT_OUTPUT = Path(PROJECT_PATH) / "output/data_provenance/sec_sina_alias_price_import.json"


def _contiguous_sec_validation(
    candidate: dict, source: pd.DataFrame, local: pd.DataFrame, overlap: dict
) -> dict:
    """Accept a non-overlapping next-session ticker change bound to one CIK."""
    matched_cik = str(candidate.get("cik") or "").zfill(10)
    issuer_ciks = {
        str(issuer.get("cik") or "").zfill(10)
        for issuer in candidate.get("sec_issuers") or []
        if issuer.get("cik")
    }
    local_last = local["date"].max()
    source_first = source["date"].min() if not source.empty else pd.NaT
    gap_days = (
        int((source_first - local_last).days)
        if pd.notna(source_first) and pd.notna(local_last)
        else None
    )
    passed = bool(
        matched_cik.strip("0")
        and issuer_ciks == {matched_cik}
        and int(overlap.get("sessions") or 0) == 0
        and gap_days is not None
        and 0 < gap_days <= 7
    )
    return {
        "passed": passed,
        "validation_scope": "sec_unique_cik_contiguous_successor_no_overlap",
        "sec_cik": matched_cik,
        "issuer_ciks": sorted(issuer_ciks),
        "overlap_sessions": int(overlap.get("sessions") or 0),
        "local_last_date": local_last.strftime("%Y-%m-%d"),
        "source_first_date": source_first.strftime("%Y-%m-%d") if pd.notna(source_first) else None,
        "terminal_tail_gap_days": gap_days,
    }


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def _atomic_write_prices(path: Path, frame: pd.DataFrame) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame[PRICE_COLUMNS].to_csv(temporary, index=False, date_format="%Y-%m-%d")
    os.replace(temporary, path)


def _select_candidates(
    probe: dict,
    *,
    allow_multiple_successors: bool = False,
    successor_overrides: dict[str, str] | None = None,
) -> list[dict]:
    candidates = _candidates(
        probe, allow_multiple_successors=allow_multiple_successors
    )
    overrides = {
        str(key).upper().strip(): str(value).upper().strip()
        for key, value in (successor_overrides or {}).items()
    }
    if not overrides:
        return candidates
    selected = [
        candidate
        for candidate in candidates
        if candidate["historical_ticker"] in overrides
        and overrides[candidate["historical_ticker"]]
        == candidate["successor_ticker"]
    ]
    selected_historical = {
        candidate["historical_ticker"] for candidate in selected
    }
    for row in probe.get("results") or []:
        historical = str(row.get("ticker") or "").strip().upper()
        successor = overrides.get(historical)
        if not successor or historical in selected_historical:
            continue
        matches = row.get("matches") or []
        issuers = row.get("issuers") or []
        matched_ciks = {
            str(item.get("cik") or "").zfill(10)
            for item in matches
            if item.get("cik")
        }
        issuer_ciks = {
            str(item.get("cik") or "").zfill(10)
            for item in issuers
            if item.get("cik")
        }
        if len(matched_ciks) != 1 or issuer_ciks != matched_ciks:
            continue
        display_names = sorted({
            str(item.get("display_name") or "")
            for item in [*matches, *issuers]
            if item.get("display_name")
        })
        alias_tickers = {
            token
            for display_name in display_names
            for group in re.findall(r"\(([^()]*)\)", display_name.upper())
            if not group.strip().startswith("CIK ")
            for token in re.findall(r"[A-Z][A-Z0-9.-]{0,9}", group)
        }
        if successor not in alias_tickers:
            continue
        selected.append({
            "historical_ticker": historical,
            "successor_ticker": successor,
            "successor_candidate_count": 1,
            "cik": next(iter(matched_ciks)),
            "sec_search_url": row.get("search_url"),
            "sec_search_payload_sha256": row.get("search_payload_sha256"),
            "sec_matches": matches,
            "sec_issuers": issuers,
            "successor_resolution_scope": (
                "sec_search_historical_display_alias_override"
            ),
            "successor_alias_display_names": display_names,
        })
    return sorted(
        selected,
        key=lambda candidate: (
            candidate["historical_ticker"], candidate["successor_ticker"]
        ),
    )


def import_terminal_tails(
    *,
    probe_path: str | Path,
    audit_path: str | Path = DEFAULT_AUDIT,
    price_dir: str | Path = CLEANED_PRICE_DATA_DIR,
    cache_dir: str | Path = DEFAULT_CACHE,
    output: str | Path = DEFAULT_OUTPUT,
    start: str = "2021-01-01",
    end: str = "2026-07-17",
    apply: bool = False,
    refresh: bool = False,
    allow_multiple_successors: bool = False,
    successor_overrides: dict[str, str] | None = None,
) -> dict:
    probe_path, audit_path = Path(probe_path), Path(audit_path)
    price_dir, cache_dir, output = Path(price_dir), Path(cache_dir), Path(output)
    probe = json.loads(probe_path.read_text())
    audit = json.loads(audit_path.read_text())
    terminal_rows = {
        str(row["ticker"]).upper(): row
        for row in audit.get("unresolved_terminal_return_histories") or []
    }
    decoder, decoder_path = _decoder_source()
    overrides = {
        str(key).upper().strip(): str(value).upper().strip()
        for key, value in (successor_overrides or {}).items()
    }
    candidates = _select_candidates(
        probe,
        allow_multiple_successors=allow_multiple_successors,
        successor_overrides=overrides,
    )
    report = {
        "schema_version": 1,
        "research_only": True,
        "status": "IN_PROGRESS",
        "applied": bool(apply),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_provider": "Sina Finance via AkShare stock_us_daily",
        "source_url_template": SOURCE_URL_TEMPLATE,
        "probe_path": str(probe_path),
        "probe_sha256": _sha256(probe_path),
        "audit_path": str(audit_path),
        "audit_sha256": _sha256(audit_path),
        "akshare_decoder_path": str(decoder_path),
        "akshare_decoder_file_sha256": _sha256(decoder_path),
        "akshare_decoder_source_sha256": hashlib.sha256(decoder.encode()).hexdigest(),
        "formal_financial_files_modified": False,
        "terminal_returns_modified": False,
        "candidate_count": len(candidates),
        "allow_multiple_successors": bool(allow_multiple_successors),
        "successor_overrides": overrides,
        "records": [],
    }
    _atomic_write_json(output, report)

    for candidate in candidates:
        historical = candidate["historical_ticker"]
        successor = candidate["successor_ticker"]
        terminal = terminal_rows.get(historical)
        price_path = price_dir / f"{historical.lower()}.csv"
        record = {**candidate, "terminal_reference": terminal, "price_path": str(price_path)}
        try:
            if terminal is None or not price_path.exists():
                record["status"] = "REJECT_MISSING_AUDIT_OR_LOCAL_REFERENCE"
            else:
                local = _read_prices(price_path)
                local_last = local["date"].max()
                record.update({
                    "local_rows_before": int(len(local)),
                    "local_last_date": local_last.strftime("%Y-%m-%d"),
                    "local_sha256_before": _sha256(price_path),
                })
                if record["local_last_date"] != str(terminal.get("last_price_date")):
                    record["status"] = "REJECT_STALE_TERMINAL_AUDIT"
                else:
                    url = SOURCE_URL_TEMPLATE.format(ticker=successor)
                    payload, cache_path = _load_or_fetch(cache_dir, successor, url, refresh)
                    source = _parse_prices(payload, successor, decoder)
                    source = source.loc[source["date"].between(pd.Timestamp(start), pd.Timestamp(end))]
                    validation = _longest_stable_tail_validation(source, local)
                    contiguous_validation = None
                    if not validation.get("passed"):
                        contiguous_validation = _contiguous_sec_validation(
                            candidate, source, local, validation
                        )
                        if contiguous_validation["passed"]:
                            validation = contiguous_validation
                    record.update({
                        "source_url": url,
                        "raw_cache_path": str(cache_path),
                        "raw_payload_size_bytes": len(payload),
                        "raw_payload_sha256": hashlib.sha256(payload).hexdigest(),
                        "source_rows": int(len(source)),
                        "source_first_date": source["date"].min().strftime("%Y-%m-%d") if not source.empty else None,
                        "source_last_date": source["date"].max().strftime("%Y-%m-%d") if not source.empty else None,
                        "cross_validation": validation,
                        "contiguous_sec_validation": contiguous_validation,
                    })
                    if not validation.get("passed"):
                        record["status"] = "REJECT_CROSS_VALIDATION"
                    else:
                        normalized = source.copy()
                        price_factor = float(validation.get("close_median_ratio", 1.0))
                        for field in ("open", "high", "low", "close"):
                            normalized[field] = normalized[field].astype(float) * price_factor
                        volume_factor = validation.get("volume_median_ratio")
                        if volume_factor is not None:
                            normalized["volume"] = normalized["volume"].astype(float) * float(volume_factor)
                        normalized["ticker"] = historical
                        missing = normalized.loc[
                            normalized["date"].gt(local_last)
                            & ~normalized["date"].isin(local["date"])
                        ].copy().sort_values("date")
                        record.update({
                            "price_factor": price_factor,
                            "volume_factor": volume_factor,
                            "rows_missing": int(len(missing)),
                            "first_missing_date": missing["date"].min().strftime("%Y-%m-%d") if not missing.empty else None,
                            "last_missing_date": missing["date"].max().strftime("%Y-%m-%d") if not missing.empty else None,
                            "missing_rows_sha256": _frame_sha256(missing) if not missing.empty else None,
                        })
                        if not missing.empty and (missing["date"].min() - local_last).days > 7:
                            record["status"] = "REJECT_NONCONTIGUOUS_TERMINAL_TAIL"
                            record["terminal_tail_gap_days"] = int((missing["date"].min() - local_last).days)
                        elif apply and not missing.empty:
                            merged = pd.concat([local, missing], ignore_index=True).drop_duplicates("date", keep="first").sort_values("date")
                            _atomic_write_prices(price_path, merged)
                            persisted = _read_prices(price_path)
                            record.update({
                                "status": "UPDATED",
                                "local_rows_after": int(len(persisted)),
                                "local_sha256_after": _sha256(price_path),
                                "persisted_appended_rows_sha256": _frame_sha256(persisted.loc[persisted["date"].isin(missing["date"])])
                            })
                        else:
                            record["status"] = "DRY_RUN_ELIGIBLE" if not missing.empty else "NO_NEW_ROWS"
                            record["local_rows_after"] = int(len(local))
                            record["local_sha256_after"] = record["local_sha256_before"]
        except Exception as exc:
            record.update({"status": "SOURCE_OR_PARSE_ERROR", "error": repr(exc)})
        report["records"].append(record)
        report["checkpointed_records"] = len(report["records"])
        report["last_checkpoint_ticker"] = historical
        report["last_checkpoint_at_utc"] = datetime.now(timezone.utc).isoformat()
        _atomic_write_json(output, report)

    report["status"] = "COMPLETE"
    report["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(output, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", required=True)
    parser.add_argument("--audit", default=str(DEFAULT_AUDIT))
    parser.add_argument("--price-dir", default=str(CLEANED_PRICE_DATA_DIR))
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--start", default="2021-01-01")
    parser.add_argument("--end", default="2026-07-17")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--allow-multiple-successors",
        action="store_true",
        help="Dry-run every SEC current ticker for a unique CIK",
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
    report = import_terminal_tails(
        probe_path=args.probe, audit_path=args.audit, price_dir=args.price_dir,
        cache_dir=args.cache_dir, output=args.output, start=args.start,
        end=args.end, refresh=args.refresh, apply=args.apply,
        allow_multiple_successors=args.allow_multiple_successors,
        successor_overrides=overrides,
    )
    counts = pd.Series([row["status"] for row in report["records"]]).value_counts()
    print(json.dumps({"candidate_count": report["candidate_count"], "counts": counts.to_dict()}, indent=2))


if __name__ == "__main__":
    main()
