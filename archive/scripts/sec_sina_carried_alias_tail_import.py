"""Replace a proven carried-forward ticker tail with same-CIK Sina prices."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from scripts.historicaldata_price_import import PRICE_COLUMNS, _frame_sha256, _sha256
from scripts.sec_sina_alias_price_import import (
    DEFAULT_CACHE,
    _atomic_write_json,
    _atomic_write_prices,
    _select_candidates,
)
from scripts.sina_historical_price_repair import _decoder_source, _parse_prices
from scripts.yahoo_historical_price_repair import _read_prices
from src.conf import CLEANED_PRICE_DATA_DIR, PROJECT_PATH


DEFAULT_AUDIT = Path(PROJECT_PATH) / "output/historical_data_audit.json"
DEFAULT_OUTPUT = Path(PROJECT_PATH) / (
    "output/data_provenance/sec_sina_carried_alias_tail_import.json"
)
VALUE_COLUMNS = ["open", "high", "low", "close", "volume"]


def _carried_suffix_validation(
    local: pd.DataFrame, source: pd.DataFrame, *, maximum_carried_rows: int = 5
) -> dict[str, Any]:
    """Prove that source replaces only a short mechanically repeated suffix."""
    local = local.sort_values("date").reset_index(drop=True)
    source = source.sort_values("date").reset_index(drop=True)
    if local.empty or source.empty:
        return {"passed": False, "reason": "empty_source_or_local"}
    # Provider symbols can be reused decades later.  Only a segment beginning
    # near the observed historical boundary may participate in this gate.
    boundary_floor = pd.Timestamp(local["date"].max()) - pd.Timedelta(days=10)
    source = source.loc[source["date"].ge(boundary_floor)].reset_index(drop=True)
    if source.empty:
        return {"passed": False, "reason": "no_source_segment_near_boundary"}
    source_first = pd.Timestamp(source["date"].min())
    before = local.loc[local["date"].lt(source_first)]
    overlap = local.loc[local["date"].ge(source_first)]
    if before.empty or overlap.empty:
        return {"passed": False, "reason": "no_anchor_or_overlapping_suffix"}
    if len(overlap) > maximum_carried_rows:
        return {"passed": False, "reason": "carried_suffix_too_long"}
    anchor = before.iloc[-1]
    carried = np.isclose(
        overlap[VALUE_COLUMNS].astype(float).to_numpy(),
        anchor[VALUE_COLUMNS].astype(float).to_numpy(),
        rtol=0.0,
        atol=1e-12,
        equal_nan=False,
    ).all()
    if not carried:
        return {"passed": False, "reason": "suffix_is_not_exactly_carried"}
    overlap_dates = set(overlap["date"])
    source_overlap = source.loc[source["date"].isin(overlap_dates)]
    if set(source_overlap["date"]) != overlap_dates:
        return {"passed": False, "reason": "source_does_not_cover_suffix_dates"}
    source_is_distinct = not np.isclose(
        source_overlap[VALUE_COLUMNS].astype(float).to_numpy(),
        anchor[VALUE_COLUMNS].astype(float).to_numpy(),
        rtol=0.0,
        atol=1e-12,
        equal_nan=False,
    ).all()
    if not source_is_distinct:
        return {"passed": False, "reason": "source_is_also_carried"}
    gap_days = int((source_first - pd.Timestamp(anchor["date"])).days)
    if not 1 <= gap_days <= 7:
        return {"passed": False, "reason": "source_boundary_not_contiguous"}
    return {
        "passed": True,
        "validation_scope": "sec_unique_cik_exact_carried_suffix_replacement",
        "anchor_date": pd.Timestamp(anchor["date"]).strftime("%Y-%m-%d"),
        "source_first_date": source_first.strftime("%Y-%m-%d"),
        "local_last_date": pd.Timestamp(local["date"].max()).strftime("%Y-%m-%d"),
        "carried_rows": int(len(overlap)),
        "carried_dates": [pd.Timestamp(value).strftime("%Y-%m-%d") for value in overlap["date"]],
        "boundary_gap_days": gap_days,
        "source_boundary_floor": boundary_floor.strftime("%Y-%m-%d"),
        "carried_frame_sha256": _frame_sha256(overlap),
        "source_overlap_frame_sha256": _frame_sha256(source_overlap),
    }


def import_carried_alias_tail(
    *,
    probe_path: str | Path,
    audit_path: str | Path = DEFAULT_AUDIT,
    historical_ticker: str,
    successor_ticker: str,
    end: str,
    price_dir: str | Path = CLEANED_PRICE_DATA_DIR,
    cache_dir: str | Path = DEFAULT_CACHE,
    output: str | Path = DEFAULT_OUTPUT,
    apply: bool = False,
) -> dict[str, Any]:
    probe_path, audit_path = Path(probe_path).resolve(), Path(audit_path).resolve()
    price_dir, cache_dir, output = Path(price_dir), Path(cache_dir), Path(output)
    historical, successor = historical_ticker.upper(), successor_ticker.upper()
    probe = json.loads(probe_path.read_text(encoding="utf-8"))
    candidates = _select_candidates(
        probe,
        allow_multiple_successors=True,
        successor_overrides={historical: successor},
    )
    if len(candidates) != 1:
        raise ValueError("SEC probe must resolve exactly one requested alias candidate")
    candidate = candidates[0]
    issuer_ciks = {
        str(row.get("cik") or "").zfill(10)
        for row in candidate.get("sec_issuers", [])
        if row.get("cik")
    }
    if issuer_ciks != {str(candidate["cik"]).zfill(10)}:
        raise ValueError("SEC issuer CIK does not uniquely match the historical ticker")

    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    unresolved = {
        str(row["ticker"]).upper(): row
        for row in audit.get("ended_histories", [])
        if row.get("observed_terminal_return") is False
    }
    terminal = unresolved.get(historical)
    if terminal is None:
        raise ValueError("historical ticker is not in the current unresolved audit")
    price_path = price_dir / f"{historical.lower()}.csv"
    local = _read_prices(price_path)
    if pd.Timestamp(local["date"].max()).strftime("%Y-%m-%d") != str(
        terminal["last_price_date"]
    ):
        raise ValueError("historical audit is stale for the local price file")

    cache_path = cache_dir / f"{successor.lower()}.txt.gz"
    raw = gzip.decompress(cache_path.read_bytes())
    decoder, decoder_path = _decoder_source()
    source = _parse_prices(raw, successor, decoder)
    source = source.loc[source["date"].le(pd.Timestamp(end))].copy()
    validation = _carried_suffix_validation(local, source)
    if not validation["passed"]:
        raise ValueError(f"carried suffix validation failed: {validation['reason']}")

    anchor_date = pd.Timestamp(validation["anchor_date"])
    replacement = source.loc[source["date"].gt(anchor_date)].copy()
    replacement["ticker"] = historical
    kept = local.loc[local["date"].le(anchor_date)].copy()
    merged = pd.concat([kept, replacement], ignore_index=True)
    merged = merged[PRICE_COLUMNS].drop_duplicates("date", keep="last").sort_values("date")
    before_sha = _sha256(price_path)
    report = {
        "schema_version": 1,
        "research_only": True,
        "formal_financial_files_modified": False,
        "status": "DRY_RUN_ELIGIBLE" if not apply else "UPDATED",
        "applied": bool(apply),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "historical_ticker": historical,
        "successor_ticker": successor,
        "sec_cik": str(candidate["cik"]).zfill(10),
        "sec_search_url": candidate.get("sec_search_url"),
        "sec_search_payload_sha256": candidate.get("sec_search_payload_sha256"),
        "probe_path": str(probe_path),
        "probe_sha256": _sha256(probe_path),
        "audit_path": str(audit_path),
        "audit_sha256": _sha256(audit_path),
        "raw_cache_path": str(cache_path.resolve()),
        "raw_payload_sha256": hashlib.sha256(raw).hexdigest(),
        "akshare_decoder_path": str(decoder_path),
        "akshare_decoder_sha256": _sha256(decoder_path),
        "price_path": str(price_path.resolve()),
        "local_sha256_before": before_sha,
        "local_rows_before": int(len(local)),
        "replacement_rows": int(len(replacement)),
        "replacement_frame_sha256": _frame_sha256(replacement),
        "validation": validation,
    }
    if apply:
        _atomic_write_prices(price_path, merged)
    report.update({
        "local_sha256_after": _sha256(price_path),
        "local_rows_after": int(len(_read_prices(price_path))),
    })
    _atomic_write_json(output, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", required=True)
    parser.add_argument("--audit", default=str(DEFAULT_AUDIT))
    parser.add_argument("--historical", required=True)
    parser.add_argument("--successor", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--price-dir", default=str(CLEANED_PRICE_DATA_DIR))
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    report = import_carried_alias_tail(
        probe_path=args.probe,
        audit_path=args.audit,
        historical_ticker=args.historical,
        successor_ticker=args.successor,
        end=args.end,
        price_dir=args.price_dir,
        cache_dir=args.cache_dir,
        output=args.output,
        apply=args.apply,
    )
    print(json.dumps({
        "status": report["status"],
        "replacement_rows": report["replacement_rows"],
        "validation_scope": report["validation"]["validation_scope"],
    }, indent=2))


if __name__ == "__main__":
    main()
