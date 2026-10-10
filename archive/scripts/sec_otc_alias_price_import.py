"""Append SEC-verified OTC successor tails from the Edgar Online chart feed.

This fallback only considers aliases absent from the official Stooq archive.
Normal membership repair requires strict OHLCV overlap.  Terminal-tail mode
also accepts a non-overlapping next-session ticker change when SEC evidence
binds both symbols to one CIK. Existing dates are never replaced and
``--apply`` is required to write prices. The result remains research-only
because source redistribution and PIT rights are not verified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import pandas as pd

from scripts.historicaldata_price_import import (
    _atomic_write,
    _atomic_write_json,
    _frame_sha256,
    _normalize_split_scale,
    _read_local,
    _sha256,
    _validate_overlap,
)
from scripts.otc_historical_price_repair import (
    EDGAR_BASE,
    _load_or_fetch,
    _parse_edgar,
)
from scripts.sec_alias_price_import import _membership_ends
from scripts.sec_sina_alias_price_import import (
    _contiguous_sec_validation,
    _select_candidates,
)
from scripts.sina_historical_price_repair import _longest_stable_tail_validation
from scripts.sina_historical_price_repair import _fixed_mirror_sec_cross_validation
from src.conf import CLEANED_PRICE_DATA_DIR, PROJECT_PATH


DEFAULT_STOOQ_REPORT = Path(PROJECT_PATH) / (
    "output/data_provenance/sec_stooq_alias_price_import_retry_dry_run.json"
)
DEFAULT_AUDIT = Path(PROJECT_PATH) / "output/historical_data_audit.json"
DEFAULT_CACHE = Path(PROJECT_PATH) / "output/data_provenance/otc_historical_price_cache"
DEFAULT_OUTPUT = Path(PROJECT_PATH) / (
    "output/data_provenance/sec_otc_alias_price_import.json"
)
DEFAULT_FIXED_MIRROR_PROVENANCE = Path(PROJECT_PATH) / (
    "output/data_provenance/stooq_github_import.json"
)
DEFAULT_SEC_TRANSITION_PROBE = Path(PROJECT_PATH) / (
    "output/data_provenance/"
    "sec_ticker_transition_probe_unresolved_147_2026-08-09.json"
)


def _source_missing_candidates(stooq_report: dict) -> list[dict]:
    keys = (
        "historical_ticker", "successor_ticker", "cik", "sec_search_url",
        "sec_search_payload_sha256", "sec_matches", "sec_issuers",
    )
    return sorted(
        [
            {key: row.get(key) for key in keys}
            for row in stooq_report.get("records") or []
            if row.get("status") == "SOURCE_MISSING"
        ],
        key=lambda row: row["historical_ticker"],
    )


def _carried_terminal_boundary_validation(
    candidate: dict, source: pd.DataFrame, local: pd.DataFrame
) -> dict:
    result: dict[str, object] = {
        "passed": False,
        "validation_scope": "replace_single_carried_terminal_row",
    }
    if len(local) < 21:
        return {**result, "reason": "insufficient_local_history"}
    local = local.sort_values("date")
    last, previous = local.iloc[-1], local.iloc[-2]
    fields = ["open", "high", "low", "close", "volume"]
    repeated_fields = {
        field: bool(float(last[field]) == float(previous[field])) for field in fields
    }
    result["local_terminal_repeated_fields"] = repeated_fields
    if not all(repeated_fields.values()):
        return {**result, "reason": "local_terminal_row_not_exact_carry"}
    source_terminal = source.loc[source["date"].eq(last["date"])]
    if len(source_terminal) != 1:
        return {**result, "reason": "source_terminal_row_not_unique"}
    source_row = source_terminal.iloc[0]
    if float(source_row["volume"]) <= 0:
        return {**result, "reason": "source_terminal_row_has_no_volume"}
    matched_ciks = {
        str(match.get("cik", "")).zfill(10)
        for match in candidate.get("sec_matches") or []
        if match.get("cik")
    }
    issuer_ciks = {
        str(issuer.get("cik", "")).zfill(10)
        for issuer in candidate.get("sec_issuers") or []
        if issuer.get("cik")
    }
    candidate_cik = str(candidate.get("cik", "")).zfill(10)
    if matched_ciks != {candidate_cik} or issuer_ciks != {candidate_cik}:
        return {**result, "reason": "sec_unique_cik_gate_failed"}
    stable = _longest_stable_tail_validation(
        source.loc[source["date"].lt(last["date"])],
        local.iloc[:-1],
        minimum_sessions=20,
    )
    result["prior_stable_tail_validation"] = stable
    if not stable["passed"]:
        return {**result, "reason": "prior_overlap_not_stable"}
    old_row = {field: float(last[field]) for field in fields}
    new_row = {field: float(source_row[field]) for field in fields}
    source_repeats_carried_row = all(
        old_row[field] == new_row[field] for field in fields
    )
    if source_repeats_carried_row:
        return {**result, "reason": "source_terminal_row_matches_carried_row"}
    return {
        **result,
        "passed": True,
        "replacement_date": last["date"].strftime("%Y-%m-%d"),
        "old_local_row": old_row,
        "new_source_row_before_normalization": new_row,
        "price_factor": float(stable["close_median_ratio"]),
        "volume_factor": stable.get("volume_median_ratio"),
        "sec_cik": candidate_cik,
    }


def _sec_unique_cik_exact_tail_validation(
    candidate: dict,
    source: pd.DataFrame,
    local: pd.DataFrame,
) -> dict:
    """Accept a 10--19 session exact tail only with unique SEC identity."""
    result: dict[str, object] = {
        "passed": False,
        "validation_scope": "exact_recent_tail_plus_sec_unique_cik",
    }
    stable = _longest_stable_tail_validation(
        source,
        local,
        minimum_sessions=10,
        maximum_sessions=19,
    )
    result["stable_tail_validation"] = stable
    if not stable["passed"]:
        return {**result, "reason": "fewer_than_10_exact_tail_sessions"}
    candidate_cik = str(candidate.get("cik", "")).zfill(10)
    matched_ciks = {
        str(match.get("cik", "")).zfill(10)
        for match in candidate.get("sec_matches") or []
        if match.get("cik")
    }
    issuer_ciks = {
        str(issuer.get("cik", "")).zfill(10)
        for issuer in candidate.get("sec_issuers") or []
        if issuer.get("cik")
    }
    if matched_ciks != {candidate_cik} or issuer_ciks != {candidate_cik}:
        return {**result, "reason": "sec_unique_cik_gate_failed"}
    local_last = local["date"].max().strftime("%Y-%m-%d")
    if stable.get("tail_last_date") != local_last:
        return {**result, "reason": "stable_tail_does_not_reach_local_end"}
    return {
        **result,
        **stable,
        "passed": True,
        "validation_scope": "exact_recent_tail_plus_sec_unique_cik",
        "sec_cik": candidate_cik,
    }


def import_aliases(
    *,
    stooq_report_path: str | Path = DEFAULT_STOOQ_REPORT,
    audit_path: str | Path = DEFAULT_AUDIT,
    price_dir: str | Path = CLEANED_PRICE_DATA_DIR,
    cache_dir: str | Path = DEFAULT_CACHE,
    output: str | Path = DEFAULT_OUTPUT,
    start: str = "2021-01-01",
    end: str = "2026-07-17",
    apply: bool = False,
    refresh: bool = False,
    terminal_tail: bool = False,
    replace_carried_terminal_row: bool = False,
    fixed_mirror_provenance: str | Path | None = None,
    sec_transition_probe: str | Path | None = None,
    successor_overrides: dict[str, str] | None = None,
) -> dict:
    if replace_carried_terminal_row and not terminal_tail:
        raise ValueError("carried terminal row replacement requires terminal_tail")
    stooq_report_path, audit_path = Path(stooq_report_path), Path(audit_path)
    price_dir, cache_dir, output = Path(price_dir), Path(cache_dir), Path(output)
    stooq_report = json.loads(stooq_report_path.read_text(encoding="utf-8"))
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    overrides = {
        str(key).strip().upper(): str(value).strip().upper()
        for key, value in (successor_overrides or {}).items()
    }
    if overrides:
        if sec_transition_probe is None:
            raise ValueError(
                "historical successor overrides require a SEC transition probe"
            )
        probe_path = Path(sec_transition_probe)
        probe = json.loads(probe_path.read_text(encoding="utf-8"))
        candidates = _select_candidates(
            probe, successor_overrides=overrides
        )
    else:
        probe_path = Path(sec_transition_probe) if sec_transition_probe else None
        candidates = _source_missing_candidates(stooq_report)
    membership_ends = _membership_ends(audit)
    terminal_rows = {
        str(row["ticker"]).strip().upper(): row
        for row in audit.get("unresolved_terminal_return_histories") or []
        if isinstance(row, dict) and row.get("ticker")
    }
    end_ts = pd.Timestamp(end)
    records: list[dict] = []
    report = {
        "schema_version": 1,
        "research_only": True,
        "source_rights_review": "UNVERIFIED_RESEARCH_ONLY",
        "status": "IN_PROGRESS",
        "applied": bool(apply),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "stooq_report_path": str(stooq_report_path),
        "stooq_report_sha256": _sha256(stooq_report_path),
        "audit_path": str(audit_path),
        "audit_sha256": _sha256(audit_path),
        "formal_financial_files_modified": False,
        "terminal_returns_modified": False,
        "candidate_count": len(candidates),
        "tail_mode": "terminal" if terminal_tail else "membership",
        "replace_carried_terminal_row": bool(replace_carried_terminal_row),
        "successor_overrides": overrides,
        "sec_transition_probe_path": str(probe_path) if probe_path else None,
        "sec_transition_probe_sha256": (
            _sha256(probe_path)
            if probe_path is not None and probe_path.exists()
            else None
        ),
        "end": end,
        "records": records,
    }
    _atomic_write_json(output, report)

    def checkpoint(record: dict) -> None:
        records.append(record)
        report["checkpointed_records"] = len(records)
        report["last_checkpoint_ticker"] = record["historical_ticker"]
        report["last_checkpoint_at_utc"] = datetime.now(timezone.utc).isoformat()
        _atomic_write_json(output, report)

    for candidate in candidates:
        historical = candidate["historical_ticker"]
        successor = candidate["successor_ticker"]
        record = dict(candidate)
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
        url = EDGAR_BASE + "?" + urlencode({
            "symbol": successor,
            "frequencyID": 0,
            "date": f"{start}~{end}",
            "includeLatestIntradayData": 1,
        })
        record["source_url"] = url
        try:
            payload, cache_path = _load_or_fetch(
                cache_dir, "edgar", successor, url, refresh
            )
            source, company_name = _parse_edgar(payload, successor)
            record.update({
                "source_payload_sha256": hashlib.sha256(payload).hexdigest(),
                "cache_path": cache_path,
                "source_company_name": company_name,
                "source_rows": int(len(source)),
            })
            if not company_name or source.empty:
                record["status"] = "REJECT_NO_SOURCE_HISTORY"
                checkpoint(record)
                continue
            local = _read_local(local_path)
            local_last = local["date"].max()
            if (
                terminal_tail
                and local_last.strftime("%Y-%m-%d")
                != str(terminal_reference.get("last_price_date"))
            ):
                record.update({
                    "local_last_date": local_last.strftime("%Y-%m-%d"),
                    "status": "REJECT_STALE_TERMINAL_AUDIT",
                })
                checkpoint(record)
                continue
            raw_validation = _validate_overlap(local, source)
            validation = raw_validation
            normalization = None
            if not validation["passed"]:
                normalized, normalization = _normalize_split_scale(local, source)
                if normalized is not None:
                    source = normalized
                    validation = normalization["normalized_cross_validation"]
            stable_tail_validation = None
            sec_exact_tail_validation = None
            fixed_mirror_sec_validation = None
            if terminal_tail and not validation["passed"]:
                stable_tail_validation = _longest_stable_tail_validation(
                    source, local, minimum_sessions=20
                )
                if stable_tail_validation["passed"]:
                    price_factor = float(
                        stable_tail_validation["close_median_ratio"]
                    )
                    for field in ("open", "high", "low", "close"):
                        source[field] = source[field].astype(float) * price_factor
                    volume_factor = stable_tail_validation.get(
                        "volume_median_ratio"
                    )
                    if volume_factor is not None:
                        source["volume"] = source["volume"].astype(float) * float(
                            volume_factor
                        )
                    validation = {
                        **stable_tail_validation,
                        "validation_scope": "recent_stable_overlap_tail",
                        "price_factor": price_factor,
                        "volume_factor": volume_factor,
                    }
            if terminal_tail and not validation["passed"]:
                sec_exact_tail_validation = _sec_unique_cik_exact_tail_validation(
                    candidate, source, local
                )
                if sec_exact_tail_validation["passed"]:
                    stable = sec_exact_tail_validation["stable_tail_validation"]
                    price_factor = float(stable["close_median_ratio"])
                    for field in ("open", "high", "low", "close"):
                        source[field] = source[field].astype(float) * price_factor
                    volume_factor = stable.get("volume_median_ratio")
                    if volume_factor is not None:
                        source["volume"] = source["volume"].astype(float) * float(
                            volume_factor
                        )
                    validation = {
                        **sec_exact_tail_validation,
                        "price_factor": price_factor,
                        "volume_factor": volume_factor,
                    }
            if (
                terminal_tail
                and not validation["passed"]
                and fixed_mirror_provenance is not None
                and sec_transition_probe is not None
            ):
                short_tail = _longest_stable_tail_validation(
                    source,
                    local,
                    minimum_sessions=3,
                    maximum_sessions=9,
                )
                fixed_mirror_sec_validation = _fixed_mirror_sec_cross_validation(
                    ticker=historical,
                    local=local,
                    overlap=short_tail,
                    mirror_provenance_path=fixed_mirror_provenance,
                    sec_probe_path=sec_transition_probe,
                )
                if fixed_mirror_sec_validation["passed"]:
                    price_factor = float(short_tail["close_median_ratio"])
                    for field in ("open", "high", "low", "close"):
                        source[field] = source[field].astype(float) * price_factor
                    volume_factor = short_tail.get("volume_median_ratio")
                    if volume_factor is not None:
                        source["volume"] = source["volume"].astype(float) * float(
                            volume_factor
                        )
                    validation = {
                        **short_tail,
                        "passed": True,
                        "validation_scope": (
                            "exact_short_tail_plus_fixed_git_mirror_plus_sec_identity"
                        ),
                        "price_factor": price_factor,
                        "volume_factor": volume_factor,
                        "fixed_mirror_sec_cross_source": fixed_mirror_sec_validation,
                    }
            carried_boundary_validation = None
            if (
                terminal_tail
                and replace_carried_terminal_row
            ):
                carried_boundary_validation = _carried_terminal_boundary_validation(
                    candidate, source, local
                )
                if carried_boundary_validation["passed"]:
                    price_factor = float(
                        carried_boundary_validation["price_factor"]
                    )
                    for field in ("open", "high", "low", "close"):
                        source[field] = source[field].astype(float) * price_factor
                    volume_factor = carried_boundary_validation.get("volume_factor")
                    if volume_factor is not None:
                        source["volume"] = source["volume"].astype(float) * float(
                            volume_factor
                        )
                    validation = carried_boundary_validation
            contiguous_validation = None
            if terminal_tail and not validation["passed"]:
                contiguous_validation = _contiguous_sec_validation(
                    candidate, source, local, raw_validation
                )
                if contiguous_validation["passed"]:
                    validation = contiguous_validation
            record.update({
                "source_first_date": source["date"].min().strftime("%Y-%m-%d"),
                "source_last_date": source["date"].max().strftime("%Y-%m-%d"),
                "local_rows_before": int(len(local)),
                "local_sha256_before": _sha256(local_path),
                "local_last_date": local["date"].max().strftime("%Y-%m-%d"),
                "raw_cross_validation": raw_validation,
                "cross_validation": validation,
                "scale_normalization": normalization,
                "stable_tail_validation": stable_tail_validation,
                "sec_exact_tail_validation": sec_exact_tail_validation,
                "fixed_mirror_sec_validation": fixed_mirror_sec_validation,
                "carried_terminal_boundary_validation": carried_boundary_validation,
                "contiguous_sec_validation": contiguous_validation,
            })
            if not validation["passed"]:
                record["status"] = "REJECT_CROSS_VALIDATION"
                checkpoint(record)
                continue
            source["ticker"] = historical
            tail_end = (
                end_ts
                if terminal_tail
                else min(pd.Timestamp(membership_end), end_ts)
            )
            replace_boundary = bool(
                carried_boundary_validation
                and carried_boundary_validation.get("passed")
            )
            after_boundary = (
                source["date"].ge(local_last)
                if replace_boundary
                else source["date"].gt(local_last)
            )
            not_already_local = (
                pd.Series(True, index=source.index)
                if replace_boundary
                else ~source["date"].isin(local["date"])
            )
            missing = source.loc[
                after_boundary
                & source["date"].le(tail_end)
                & not_already_local
            ].copy().sort_values("date")
            record.update({
                "rows_missing": int(len(missing)),
                "rows_replaced": 1 if replace_boundary else 0,
                "positive_volume_rows_missing": int(
                    missing["volume"].astype(float).gt(0).sum()
                ),
                "zero_volume_rows_missing": int(
                    missing["volume"].astype(float).eq(0).sum()
                ),
                "unique_close_values_missing": int(
                    missing["close"].astype(float).nunique()
                ),
                "price_change_rows_missing": int(
                    missing["close"].astype(float).diff().ne(0).sum()
                ),
                "last_positive_volume_date": (
                    missing.loc[
                        missing["volume"].astype(float).gt(0), "date"
                    ].max().strftime("%Y-%m-%d")
                    if missing["volume"].astype(float).gt(0).any()
                    else None
                ),
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
                local_to_merge = (
                    local.loc[local["date"].ne(local_last)]
                    if replace_boundary
                    else local
                )
                merged = (
                    pd.concat([local_to_merge, missing], ignore_index=True)
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
        except Exception as exc:
            record["status"] = "SOURCE_OR_PARSE_ERROR"
            record["error"] = repr(exc)
        checkpoint(record)
    report["status"] = "COMPLETE"
    report["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(output, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stooq-report", default=str(DEFAULT_STOOQ_REPORT))
    parser.add_argument("--audit", default=str(DEFAULT_AUDIT))
    parser.add_argument("--price-dir", default=str(CLEANED_PRICE_DATA_DIR))
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--start", default="2021-01-01")
    parser.add_argument("--end", default="2026-07-17")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument(
        "--terminal-tail", action="store_true",
        help="Append a strictly contiguous same-CIK successor tail through --end",
    )
    parser.add_argument(
        "--replace-carried-terminal-row",
        action="store_true",
        help=(
            "In terminal mode only, replace one exact carried-forward final row "
            "when the SEC-bound successor has a real same-day trade and the "
            "preceding 20+ overlap sessions are stable."
        ),
    )
    parser.add_argument(
        "--fixed-mirror-provenance",
        default=str(DEFAULT_FIXED_MIRROR_PROVENANCE),
    )
    parser.add_argument(
        "--sec-transition-probe",
        default=str(DEFAULT_SEC_TRANSITION_PROBE),
    )
    parser.add_argument(
        "--successor-overrides",
        help=(
            "Comma-separated HISTORICAL=SUCCESSOR aliases explicitly present "
            "in SEC search display names"
        ),
    )
    args = parser.parse_args()
    overrides = {}
    if args.successor_overrides:
        for item in args.successor_overrides.split(","):
            historical, separator, successor = item.partition("=")
            if not separator or not historical.strip() or not successor.strip():
                parser.error(f"invalid successor override: {item}")
            overrides[historical] = successor
    report = import_aliases(
        stooq_report_path=args.stooq_report,
        audit_path=args.audit,
        price_dir=args.price_dir,
        cache_dir=args.cache_dir,
        output=args.output,
        start=args.start,
        end=args.end,
        apply=args.apply,
        refresh=args.refresh,
        terminal_tail=args.terminal_tail,
        replace_carried_terminal_row=args.replace_carried_terminal_row,
        fixed_mirror_provenance=args.fixed_mirror_provenance,
        sec_transition_probe=args.sec_transition_probe,
        successor_overrides=overrides,
    )
    counts = pd.Series([row["status"] for row in report["records"]]).value_counts()
    print(json.dumps({"candidate_count": report["candidate_count"], "counts": counts.to_dict()}, indent=2))


if __name__ == "__main__":
    main()
