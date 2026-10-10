"""Safely append same-CIK successor-ticker prices to historical ticker tails.

Candidates come from ``sec_ticker_transition_probe.py``.  A candidate is only
eligible when SEC search evidence resolves one CIK and that issuer currently
has exactly one, different ticker.  Yahoo prices under the successor ticker
must overlap the existing historical-ticker file for at least 20 sessions and
pass the established OHLC scale validation.  Rows are limited to dates after
the local tail and no later than the historical ticker's last PIT membership.
The command is a dry run unless ``--apply`` is explicit.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from scripts.historicaldata_price_import import PRICE_COLUMNS, _frame_sha256
from scripts.yahoo_historical_price_repair import (
    _overlap_validation,
    _parse_yahoo,
    _read_prices,
    _request_bytes,
    _yahoo_url,
)
from src.conf import CLEANED_PRICE_DATA_DIR, PROJECT_PATH


DEFAULT_PROBE = Path(PROJECT_PATH) / (
    "output/data_provenance/sec_ticker_transition_probe_remaining_2026-08-08.json"
)
DEFAULT_AUDIT = Path(PROJECT_PATH) / "output/historical_data_audit.json"
DEFAULT_CACHE = Path(PROJECT_PATH) / "output/data_provenance/yahoo_sec_alias_cache"
DEFAULT_OUTPUT = Path(PROJECT_PATH) / (
    "output/data_provenance/sec_alias_price_import.json"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _atomic_write_prices(path: Path, frame: pd.DataFrame) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame[PRICE_COLUMNS].to_csv(temporary, index=False, date_format="%Y-%m-%d")
    os.replace(temporary, path)


def _candidates(
    probe: dict, *, allow_multiple_successors: bool = False
) -> list[dict]:
    candidates = []
    for row in probe.get("results") or []:
        if row.get("status") != "ok":
            continue
        ciks = sorted({str(match["cik"]) for match in row.get("matches") or []})
        current_tickers = sorted({
            str(ticker).strip().upper()
            for issuer in row.get("issuers") or []
            for ticker in issuer.get("current_tickers") or []
            if ticker
        })
        historical = str(row.get("ticker") or "").strip().upper()
        if len(ciks) != 1 or not current_tickers:
            continue
        successors = [ticker for ticker in current_tickers if ticker != historical]
        if (
            not historical
            or not successors
            or (not allow_multiple_successors and len(current_tickers) != 1)
        ):
            continue
        for successor in successors:
            candidates.append({
                "historical_ticker": historical,
                "successor_ticker": successor,
                "successor_candidate_count": len(successors),
                "cik": ciks[0],
                "sec_search_url": row.get("search_url"),
                "sec_search_payload_sha256": row.get("search_payload_sha256"),
                "sec_matches": row.get("matches") or [],
                "sec_issuers": row.get("issuers") or [],
            })
    return sorted(candidates, key=lambda item: item["historical_ticker"])


def _audit_tail_ends(
    audit: dict, *, terminal_tail: bool, analysis_end: str
) -> dict[str, str]:
    row_key = (
        "unresolved_terminal_return_histories"
        if terminal_tail
        else "missing_price_while_listed_histories"
    )
    rows = audit.get(row_key) or []
    return {
        str(row["ticker"]).strip().upper(): (
            analysis_end if terminal_tail else str(row["last_membership_date"])
        )
        for row in rows
        if row.get("ticker")
    }


def _membership_ends(audit: dict) -> dict[str, str]:
    """Backward-compatible membership-gap view used by OTC/Sina importers."""
    return _audit_tail_ends(audit, terminal_tail=False, analysis_end="")


def _load_or_fetch(
    cache_dir: Path, historical: str, successor: str, url: str, refresh: bool
) -> tuple[bytes, Path]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"{historical}__{successor}.json.gz"
    if target.exists() and not refresh:
        return gzip.decompress(target.read_bytes()), target
    raw = _request_bytes(url)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(gzip.compress(raw, mtime=0))
    os.replace(temporary, target)
    return raw, target


def import_aliases(
    *,
    probe_path: str | Path = DEFAULT_PROBE,
    audit_path: str | Path = DEFAULT_AUDIT,
    price_dir: str | Path = CLEANED_PRICE_DATA_DIR,
    cache_dir: str | Path = DEFAULT_CACHE,
    output: str | Path = DEFAULT_OUTPUT,
    start: str = "2021-01-01",
    end: str = "2026-07-17",
    apply: bool = False,
    refresh: bool = False,
    offset: int = 0,
    limit: int | None = None,
    terminal_tail: bool = False,
) -> dict:
    probe_path, audit_path = Path(probe_path), Path(audit_path)
    price_dir, cache_dir, output = Path(price_dir), Path(cache_dir), Path(output)
    probe = json.loads(probe_path.read_text(encoding="utf-8"))
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    candidates = _candidates(probe)
    candidates = candidates[offset:]
    if limit is not None:
        candidates = candidates[:limit]
    tail_ends = _audit_tail_ends(
        audit, terminal_tail=terminal_tail, analysis_end=end
    )
    records: list[dict] = []
    report = {
        "schema_version": 1,
        "research_only": True,
        "status": "IN_PROGRESS",
        "applied": bool(apply),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "probe_path": str(probe_path),
        "probe_sha256": _sha256(probe_path),
        "audit_path": str(audit_path),
        "audit_sha256": _sha256(audit_path),
        "formal_financial_files_modified": False,
        "terminal_returns_modified": False,
        "terminal_tail": bool(terminal_tail),
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

    for candidate in candidates:
        historical = candidate["historical_ticker"]
        successor = candidate["successor_ticker"]
        record = dict(candidate)
        price_path = price_dir / f"{historical.lower()}.csv"
        record["price_path"] = str(price_path)
        tail_end_value = tail_ends.get(historical)
        record["tail_end_date"] = tail_end_value
        if not tail_end_value or not price_path.exists():
            record["status"] = "REJECT_MISSING_AUDIT_OR_LOCAL_REFERENCE"
            checkpoint(record)
            continue
        url = _yahoo_url(successor, start, end)
        record["source_url"] = url
        try:
            payload, cache_path = _load_or_fetch(
                cache_dir, historical, successor, url, refresh
            )
            source, metadata = _parse_yahoo(payload, successor)
            local = _read_prices(price_path)
            validation = _overlap_validation(source, local)
            record.update({
                "source_payload_sha256": hashlib.sha256(payload).hexdigest(),
                "cache_path": str(cache_path),
                "metadata": metadata,
                "source_rows": int(len(source)),
                "source_first_date": source["date"].min().strftime("%Y-%m-%d"),
                "source_last_date": source["date"].max().strftime("%Y-%m-%d"),
                "local_rows_before": int(len(local)),
                "local_sha256_before": _sha256(price_path),
                "local_last_date": local["date"].max().strftime("%Y-%m-%d"),
                "cross_validation": validation,
            })
            if metadata.get("instrument_type") not in {None, "EQUITY"}:
                record["status"] = "REJECT_NON_EQUITY"
                checkpoint(record)
                continue
            if not validation.get("passed"):
                record["status"] = "REJECT_CROSS_VALIDATION"
                checkpoint(record)
                continue
            normalized = source.copy()
            price_factor = float(validation["close_median_ratio"])
            for field in ("open", "high", "low", "close"):
                normalized[field] = normalized[field].astype(float) * price_factor
            volume_factor = validation.get("volume_median_ratio")
            if volume_factor is not None:
                normalized["volume"] = normalized["volume"].astype(float) * float(
                    volume_factor
                )
            normalized["ticker"] = historical
            local_last = local["date"].max()
            tail_end = min(pd.Timestamp(tail_end_value), pd.Timestamp(end))
            missing = normalized.loc[
                normalized["date"].gt(local_last)
                & normalized["date"].le(tail_end)
                & ~normalized["date"].isin(local["date"])
            ].copy().sort_values("date")
            record.update({
                "price_factor": price_factor,
                "volume_factor": volume_factor,
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
                _atomic_write_prices(price_path, merged)
                persisted = _read_prices(price_path)
                record.update({
                    "status": "UPDATED",
                    "local_rows_after": int(len(persisted)),
                    "local_sha256_after": _sha256(price_path),
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
    parser.add_argument("--probe", default=str(DEFAULT_PROBE))
    parser.add_argument("--audit", default=str(DEFAULT_AUDIT))
    parser.add_argument("--price-dir", default=str(CLEANED_PRICE_DATA_DIR))
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--start", default="2021-01-01")
    parser.add_argument("--end", default="2026-07-17")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--terminal-tail",
        action="store_true",
        help=(
            "Use unresolved terminal histories and allow a validated same-CIK "
            "successor tail through --end instead of stopping at membership end."
        ),
    )
    args = parser.parse_args()
    report = import_aliases(
        probe_path=args.probe,
        audit_path=args.audit,
        price_dir=args.price_dir,
        cache_dir=args.cache_dir,
        output=args.output,
        start=args.start,
        end=args.end,
        apply=args.apply,
        refresh=args.refresh,
        offset=args.offset,
        limit=args.limit,
        terminal_tail=args.terminal_tail,
    )
    counts = pd.Series([row["status"] for row in report["records"]]).value_counts()
    print(json.dumps({"candidate_count": report["candidate_count"], "counts": counts.to_dict()}, indent=2))


if __name__ == "__main__":
    main()
