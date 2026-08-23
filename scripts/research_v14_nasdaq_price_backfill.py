#!/usr/bin/env python3
"""Checkpoint and validate research-only Nasdaq price-head repairs.

The formal price directory is treated as immutable.  A copy-on-write overlay
starts as symlinks to every formal CSV; only a ticker that passes exact source
snapshot validation and cross-provider overlap checks is materialized and
extended.  Existing dates are never replaced.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
from urllib.parse import urlencode

import pandas as pd

from scripts.historicaldata_price_import import (
    MIN_OVERLAP_SESSIONS,
    PRICE_COLUMNS,
    _atomic_write,
    _atomic_write_json,
    _frame_sha256,
    _normalize_split_scale,
    _read_local,
    _validate_overlap,
)
from src.conf import CLEANED_PRICE_DATA_DIR
from src.io.nasdaq_update import API, fetch_history


DEFAULT_PRIORITY_PATH = Path(
    "output/research_only/v14/"
    "candidate_path_audit_after_foreign_price_priorities.csv"
)
DEFAULT_OVERLAY_DIR = Path("output/research_only/v14/price_overlay")
DEFAULT_SNAPSHOT_DIR = Path(
    "output/research_only/v14/nasdaq_history_snapshots"
)
DEFAULT_STATE_PATH = Path(
    "output/research_only/v14/nasdaq_price_backfill_state.json"
)
HEAD_REMEDIATION_SCOPE = "BACKFILL_PRICE_HEAD_PLUS_PIT_FINANCIAL"
INTERNAL_GAP_REMEDIATION_SCOPE = "FILL_INTERNAL_PRICE_GAPS_PLUS_PIT_FINANCIAL"
REMEDIATION_SCOPES = (
    HEAD_REMEDIATION_SCOPE,
    INTERNAL_GAP_REMEDIATION_SCOPE,
)
TERMINAL_STATUSES = {
    "IMPORTED",
    "NO_HEAD_EXTENSION",
    "REJECT_CROSS_VALIDATION",
    "REJECT_EMPTY_SOURCE",
    "REJECT_INVALID_SOURCE",
    "REJECT_NO_LOCAL_PRICE_FILE",
    "REJECT_RESPONSE_LIMIT_CLAMPED",
    "REJECT_SOURCE_START_AFTER_REQUIRED_SIGNAL",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inventory(price_dir: Path) -> dict:
    entries = []
    for path in sorted(price_dir.glob("*.csv")):
        entries.append(
            {
                "name": path.name,
                "size_bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    canonical = json.dumps(
        entries, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "file_count": len(entries),
        "sha256": hashlib.sha256(canonical).hexdigest(),
    }


def _canonical_provider_frame(
    frame: pd.DataFrame,
    provider_ticker: str,
    *,
    drop_incomplete: bool = False,
) -> pd.DataFrame:
    required = {"date", "open", "high", "low", "close", "volume"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(
            "Nasdaq response missing columns: " + ", ".join(sorted(missing))
        )
    result = frame[list(required)].copy()
    result["date"] = pd.to_datetime(result["date"], errors="raise").dt.normalize()
    for column in ("open", "high", "low", "close", "volume"):
        result[column] = pd.to_numeric(result[column], errors="coerce")
    incomplete = result[["open", "high", "low", "close", "volume"]].isna().any(
        axis=1
    )
    nonpositive_ohlc = result[["open", "high", "low", "close"]].le(0).any(
        axis=1
    )
    unusable = incomplete | nonpositive_ohlc
    if unusable.any() and not drop_incomplete:
        raise ValueError("Nasdaq response contains unusable OHLCV rows")
    result = result.loc[~unusable].copy()
    if result["date"].duplicated().any():
        raise ValueError("Nasdaq response contains duplicate dates")
    if (result["volume"] < 0).any():
        raise ValueError("Nasdaq response contains negative volume")
    tolerance = 1e-9
    invalid_range = (
        result["high"] + tolerance < result[["open", "low", "close"]].max(axis=1)
    ) | (
        result["low"] - tolerance > result[["open", "high", "close"]].min(axis=1)
    )
    if invalid_range.any():
        raise ValueError("Nasdaq response contains contradictory OHLC ranges")
    result.insert(1, "ticker", provider_ticker)
    return result[PRICE_COLUMNS].sort_values("date").reset_index(drop=True)


def _response_records_sha256(frame: pd.DataFrame, provider_ticker: str) -> str:
    response = frame[["date", "open", "high", "low", "close", "volume"]].copy()
    response["date"] = pd.to_datetime(response["date"], errors="raise")
    response.insert(1, "ticker", provider_ticker)
    records = json.loads(
        response[PRICE_COLUMNS].to_json(
            orient="records", date_format="iso", double_precision=15
        )
    )
    canonical = json.dumps(
        records, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _source_url(provider_ticker: str, start: date, end: date) -> str:
    return API.format(symbol=provider_ticker) + "?" + urlencode(
        {
            "assetclass": "stocks",
            "fromdate": start.isoformat(),
            "todate": end.isoformat(),
            "limit": 5000,
        }
    )


def _validate_snapshot(
    payload: dict,
    *,
    provider_ticker: str,
    start: date,
    end: date,
) -> pd.DataFrame:
    expected = {
        "provider_ticker": provider_ticker,
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "source_url": _source_url(provider_ticker, start, end),
    }
    for key, value in expected.items():
        observed = payload.get(key)
        if key == "provider_ticker" and observed is None:
            observed = payload.get("ticker")
        if observed != value:
            raise ValueError(
                f"snapshot {key} mismatch: expected {value!r}, got {observed!r}"
            )
    records = payload.get("records")
    if not isinstance(records, list):
        raise ValueError("snapshot records must be a list")
    if not records:
        return pd.DataFrame(columns=PRICE_COLUMNS)
    frame = _canonical_provider_frame(pd.DataFrame(records), provider_ticker)
    if payload.get("rows") != len(frame):
        raise ValueError("snapshot row count mismatch")
    if payload.get("frame_sha256") != _frame_sha256(frame):
        raise ValueError("snapshot frame SHA mismatch")
    if frame["date"].min().date() < start or frame["date"].max().date() > end:
        raise ValueError("snapshot contains rows outside the requested range")
    return frame


def _snapshot_path(
    snapshot_dir: Path, provider_ticker: str, start: date, end: date
) -> Path:
    return snapshot_dir / (
        f"{provider_ticker.lower()}_{start.isoformat()}_{end.isoformat()}.json"
    )


def _fetch_or_replay_snapshot(
    provider_ticker: str,
    start: date,
    end: date,
    snapshot_dir: Path,
) -> tuple[pd.DataFrame, dict, Path, bool]:
    path = _snapshot_path(snapshot_dir, provider_ticker, start, end)
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        return (
            _validate_snapshot(
                payload,
                provider_ticker=provider_ticker,
                start=start,
                end=end,
            ),
            payload,
            path,
            False,
        )
    fetched = fetch_history(provider_ticker, start, end, retries=3)
    raw_rows = len(fetched)
    numeric = (
        fetched[["open", "high", "low", "close", "volume"]].apply(
            pd.to_numeric, errors="coerce"
        )
        if len(fetched) else pd.DataFrame()
    )
    incomplete = (
        numeric.isna().any(axis=1) if len(fetched) else pd.Series(dtype=bool)
    )
    nonpositive_ohlc = (
        numeric[["open", "high", "low", "close"]].le(0).any(axis=1)
        if len(fetched) else pd.Series(dtype=bool)
    )
    incomplete_dates = (
        pd.to_datetime(fetched.loc[incomplete, "date"], errors="coerce")
        .dropna()
        .dt.strftime("%Y-%m-%d")
        .tolist()
        if len(fetched) else []
    )
    nonpositive_ohlc_dates = (
        pd.to_datetime(
            fetched.loc[nonpositive_ohlc, "date"], errors="coerce"
        )
        .dropna()
        .dt.strftime("%Y-%m-%d")
        .tolist()
        if len(fetched) else []
    )
    response_records_sha256 = (
        _response_records_sha256(fetched, provider_ticker)
        if len(fetched) else None
    )
    frame = (
        _canonical_provider_frame(
            fetched, provider_ticker, drop_incomplete=True
        )
        if len(fetched)
        else pd.DataFrame(columns=PRICE_COLUMNS)
    )
    source_url = _source_url(provider_ticker, start, end)
    records = json.loads(
        frame.to_json(
            orient="records", date_format="iso", double_precision=15
        )
    )
    replay_frame = (
        _canonical_provider_frame(pd.DataFrame(records), provider_ticker)
        if records else pd.DataFrame(columns=PRICE_COLUMNS)
    )
    payload = {
        "schema_version": 2,
        "research_only": True,
        "provider": "Nasdaq public historical API",
        "provider_ticker": provider_ticker,
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "source_url": source_url,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "raw_rows": raw_rows,
        "response_records_sha256": response_records_sha256,
        "dropped_incomplete_rows": int(incomplete.sum()) if len(fetched) else 0,
        "dropped_incomplete_dates": incomplete_dates,
        "dropped_nonpositive_ohlc_rows": (
            int(nonpositive_ohlc.sum()) if len(fetched) else 0
        ),
        "dropped_nonpositive_ohlc_dates": nonpositive_ohlc_dates,
        "rows": len(replay_frame),
        "first_date": (
            replay_frame["date"].min().strftime("%Y-%m-%d")
            if len(replay_frame) else None
        ),
        "last_date": (
            replay_frame["date"].max().strftime("%Y-%m-%d")
            if len(replay_frame) else None
        ),
        "frame_sha256": (
            _frame_sha256(replay_frame) if len(replay_frame) else None
        ),
        "records": records,
    }
    _atomic_write_json(path, payload)
    return replay_frame, payload, path, True


def prepare_overlay(formal_price_dir: Path, overlay_dir: Path) -> dict:
    """Create or verify a symlink-only baseline for copy-on-write repairs."""
    overlay_dir.mkdir(parents=True, exist_ok=True)
    formal_names = {path.name for path in formal_price_dir.glob("*.csv")}
    extra = sorted(
        path.name for path in overlay_dir.glob("*.csv")
        if path.name not in formal_names
    )
    if extra:
        raise ValueError(f"overlay contains files absent from formal data: {extra[:5]}")
    created = 0
    for source in sorted(formal_price_dir.glob("*.csv")):
        target = overlay_dir / source.name
        if target.is_symlink():
            if target.resolve() != source.resolve():
                raise ValueError(f"overlay symlink points at unexpected file: {target}")
            continue
        if target.exists():
            continue
        relative_source = os.path.relpath(source, start=overlay_dir)
        target.symlink_to(relative_source)
        created += 1
    paths = list(overlay_dir.glob("*.csv"))
    return {
        "created_symlinks": created,
        "file_count": len(paths),
        "symlink_count": sum(path.is_symlink() for path in paths),
        "materialized_count": sum(not path.is_symlink() for path in paths),
    }


def _materialize(formal_path: Path, overlay_path: Path) -> None:
    if not overlay_path.is_symlink():
        if overlay_path.exists():
            return
        raise FileNotFoundError(overlay_path)
    temporary = overlay_path.with_suffix(overlay_path.suffix + ".materialize.tmp")
    shutil.copyfile(formal_path, temporary)
    os.replace(temporary, overlay_path)


def _same_price_values(
    left: pd.DataFrame,
    right: pd.DataFrame,
    *,
    absolute_tolerance: float = 1e-12,
    relative_tolerance: float = 1e-12,
) -> bool:
    """Compare canonical price values across harmless CSV float round-trips."""
    left = left[PRICE_COLUMNS].sort_values("date").reset_index(drop=True)
    right = right[PRICE_COLUMNS].sort_values("date").reset_index(drop=True)
    if len(left) != len(right):
        return False
    if not left["date"].equals(right["date"]):
        return False
    if not left["ticker"].astype(str).equals(right["ticker"].astype(str)):
        return False
    left_values = left[["open", "high", "low", "close", "volume"]].astype(float)
    right_values = right[["open", "high", "low", "close", "volume"]].astype(float)
    if not left_values.isna().equals(right_values.isna()):
        return False
    difference = (left_values - right_values).abs().fillna(0.0)
    allowance = (
        absolute_tolerance + relative_tolerance * right_values.abs()
    ).fillna(absolute_tolerance)
    return bool(difference.le(allowance).all().all())


def _load_priority_rows(
    path: Path, remediation_scope: str = HEAD_REMEDIATION_SCOPE
) -> list[dict]:
    frame = pd.read_csv(path, keep_default_na=False)
    required = {
        "ticker",
        "provider_ticker",
        "first_missing_signal_date",
        "remediation_scope",
        "priority_rank",
        "recovery_priority_rank",
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(
            "price priority file missing columns: " + ", ".join(sorted(missing))
        )
    if remediation_scope not in REMEDIATION_SCOPES:
        raise ValueError(f"unsupported remediation scope: {remediation_scope}")
    frame = frame.loc[
        frame["remediation_scope"].eq(remediation_scope)
    ].copy()
    frame["ticker"] = frame["ticker"].astype(str).str.upper().str.strip()
    frame["provider_ticker"] = (
        frame["provider_ticker"].astype(str).str.upper().str.strip()
    )
    if frame[["ticker", "provider_ticker"]].eq("").any().any():
        raise ValueError("price priority file contains blank tickers")
    if frame["ticker"].duplicated().any():
        raise ValueError("price priority file contains duplicate target tickers")
    frame = frame.sort_values(
        ["recovery_priority_rank", "priority_rank", "ticker"]
    )
    return frame.to_dict("records")


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {
            "schema_version": 1,
            "research_only": True,
            "release_status": "BLOCKED",
            "records": {},
            "runs": [],
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not payload.get("research_only") or not isinstance(payload.get("records"), dict):
        raise ValueError("invalid v14 Nasdaq backfill state")
    return payload


def _checkpoint(path: Path, state: dict) -> None:
    state["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(path, state)


def _same_completed_request(
    previous: dict | None,
    *,
    provider_ticker: str,
    start: date,
    end: date,
    formal_sha256: str,
    remediation_scope: str,
) -> bool:
    return bool(
        previous
        and previous.get("status") in TERMINAL_STATUSES
        and previous.get("provider_ticker") == provider_ticker
        and previous.get("requested_start") == start.isoformat()
        and previous.get("requested_end") == end.isoformat()
        and previous.get("formal_price_sha256_before") == formal_sha256
        and previous.get(
            "remediation_scope", HEAD_REMEDIATION_SCOPE
        ) == remediation_scope
    )


def _process_one(
    row: dict,
    *,
    start: date,
    end: date,
    formal_price_dir: Path,
    overlay_dir: Path,
    snapshot_dir: Path,
    apply: bool,
) -> tuple[dict, bool]:
    ticker = str(row["ticker"])
    provider_ticker = str(row["provider_ticker"])
    formal_path = formal_price_dir / f"{ticker.lower()}.csv"
    overlay_path = overlay_dir / formal_path.name
    record = {
        "ticker": ticker,
        "provider_ticker": provider_ticker,
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "priority_rank": int(row["priority_rank"]),
        "recovery_priority_rank": int(row["recovery_priority_rank"]),
        "first_missing_signal_date": row["first_missing_signal_date"],
        "remediation_scope": row["remediation_scope"],
    }
    if not formal_path.exists():
        record["status"] = "REJECT_NO_LOCAL_PRICE_FILE"
        return record, False
    local = _read_local(formal_path)
    formal_sha = _sha256(formal_path)
    record.update(
        {
            "formal_price_path": str(formal_path),
            "formal_price_sha256_before": formal_sha,
            "local_rows_before": len(local),
            "local_first_date": local["date"].min().strftime("%Y-%m-%d"),
            "local_last_date": local["date"].max().strftime("%Y-%m-%d"),
        }
    )
    try:
        source, snapshot, snapshot_path, fetched = _fetch_or_replay_snapshot(
            provider_ticker, start, end, snapshot_dir
        )
    except Exception as exc:
        record.update({"status": "FAILED_FETCH_OR_SNAPSHOT", "error": repr(exc)})
        return record, False
    record.update(
        {
            "source_url": snapshot["source_url"],
            "snapshot_path": str(snapshot_path),
            "snapshot_sha256": _sha256(snapshot_path),
            "response_frame_sha256": snapshot.get("frame_sha256"),
            "response_records_sha256": snapshot.get(
                "response_records_sha256"
            ),
            "dropped_incomplete_rows": snapshot.get(
                "dropped_incomplete_rows", 0
            ),
            "dropped_incomplete_dates": snapshot.get(
                "dropped_incomplete_dates", []
            ),
            "dropped_nonpositive_ohlc_rows": snapshot.get(
                "dropped_nonpositive_ohlc_rows", 0
            ),
            "dropped_nonpositive_ohlc_dates": snapshot.get(
                "dropped_nonpositive_ohlc_dates", []
            ),
            "source_rows": len(source),
            "source_first_date": (
                source["date"].min().strftime("%Y-%m-%d") if len(source) else None
            ),
            "source_last_date": (
                source["date"].max().strftime("%Y-%m-%d") if len(source) else None
            ),
            "snapshot_replayed": not fetched,
        }
    )
    if source.empty:
        record["status"] = "REJECT_EMPTY_SOURCE"
        return record, fetched
    if len(source) >= 5000:
        record["status"] = "REJECT_RESPONSE_LIMIT_CLAMPED"
        return record, fetched
    first_missing_signal = pd.Timestamp(row["first_missing_signal_date"])
    if source["date"].min() > first_missing_signal:
        record["status"] = "REJECT_SOURCE_START_AFTER_REQUIRED_SIGNAL"
        return record, fetched
    import_source = source.copy()
    import_source["ticker"] = ticker
    raw_overlap = _validate_overlap(local, import_source)
    overlap = raw_overlap
    scale_normalization = None
    if not overlap["passed"]:
        normalized, scale_normalization = _normalize_split_scale(
            local, import_source
        )
        if normalized is not None:
            import_source = normalized
            overlap = scale_normalization["normalized_cross_validation"]
    record["raw_cross_validation"] = raw_overlap
    record["cross_validation"] = overlap
    record["scale_normalization"] = scale_normalization
    if not overlap["passed"]:
        record["status"] = "REJECT_CROSS_VALIDATION"
        return record, fetched
    local_first = local["date"].min()
    local_last = local["date"].max()
    if row["remediation_scope"] == HEAD_REMEDIATION_SCOPE:
        missing = import_source.loc[import_source["date"] < local_first].copy()
    elif row["remediation_scope"] == INTERNAL_GAP_REMEDIATION_SCOPE:
        missing = import_source.loc[
            import_source["date"].between(local_first, local_last)
        ].copy()
    else:  # already validated while loading priorities
        raise ValueError(f"unsupported remediation scope: {row['remediation_scope']}")
    missing = missing.loc[~missing["date"].isin(local["date"])].sort_values("date")
    record.update(
        {
            "head_rows_available": len(missing),
            "head_first_date": (
                missing["date"].min().strftime("%Y-%m-%d") if len(missing) else None
            ),
            "head_last_date": (
                missing["date"].max().strftime("%Y-%m-%d") if len(missing) else None
            ),
            "head_rows_sha256": _frame_sha256(missing) if len(missing) else None,
            "repair_rows_available": len(missing),
            "repair_first_date": (
                missing["date"].min().strftime("%Y-%m-%d") if len(missing) else None
            ),
            "repair_last_date": (
                missing["date"].max().strftime("%Y-%m-%d") if len(missing) else None
            ),
        }
    )
    if missing.empty:
        record["status"] = "NO_HEAD_EXTENSION"
        return record, fetched
    if not apply:
        record["status"] = "DRY_RUN_ELIGIBLE"
        return record, fetched
    _materialize(formal_path, overlay_path)
    overlay_before = _read_local(overlay_path)
    formal_rows_in_overlay = overlay_before.loc[
        overlay_before["date"].isin(local["date"])
    ]
    if not _same_price_values(formal_rows_in_overlay, local):
        raise ValueError(f"overlay formal rows diverged before importing {ticker}")
    already_persisted = overlay_before.loc[
        overlay_before["date"].isin(missing["date"])
    ]
    if len(already_persisted) and not _same_price_values(
        already_persisted,
        missing.loc[missing["date"].isin(already_persisted["date"])],
    ):
        raise ValueError(f"overlay repair rows conflict before importing {ticker}")
    missing_to_add = missing.loc[
        ~missing["date"].isin(overlay_before["date"])
    ]
    merged = (
        pd.concat([overlay_before, missing_to_add], ignore_index=True)
        .sort_values("date")
        .drop_duplicates("date", keep="first")
    )
    _atomic_write(overlay_path, merged[PRICE_COLUMNS])
    persisted = _read_local(overlay_path)
    old_persisted = persisted.loc[persisted["date"].isin(local["date"])]
    if _frame_sha256(old_persisted) != _frame_sha256(local):
        raise ValueError(f"existing rows changed while importing {ticker}")
    persisted_head = persisted.loc[persisted["date"].isin(missing["date"])]
    if not _same_price_values(persisted_head, missing):
        raise ValueError(f"persisted head rows do not match source for {ticker}")
    record.update(
        {
            "status": "IMPORTED",
            "overlay_price_path": str(overlay_path),
            "overlay_rows_after": len(persisted),
            "overlay_first_date_after": persisted["date"].min().strftime("%Y-%m-%d"),
            "overlay_last_date_after": persisted["date"].max().strftime("%Y-%m-%d"),
            "overlay_price_sha256_after": _sha256(overlay_path),
            "persisted_head_rows_sha256": _frame_sha256(persisted_head),
            "repair_rows_added": len(missing_to_add),
            "checkpoint_recovered": bool(
                missing.empty is False and missing_to_add.empty
            ),
        }
    )
    if _sha256(formal_path) != formal_sha:
        raise RuntimeError(f"formal price file changed while importing {ticker}")
    record["formal_price_sha256_after"] = _sha256(formal_path)
    return record, fetched


def backfill_batch(
    *,
    priority_path: Path = DEFAULT_PRIORITY_PATH,
    formal_price_dir: Path = Path(CLEANED_PRICE_DATA_DIR),
    overlay_dir: Path = DEFAULT_OVERLAY_DIR,
    snapshot_dir: Path = DEFAULT_SNAPSHOT_DIR,
    state_path: Path = DEFAULT_STATE_PATH,
    start: date = date(2017, 1, 1),
    end: date = date(2026, 8, 11),
    limit: int = 25,
    delay_seconds: float = 1.0,
    apply: bool = False,
    remediation_scope: str = HEAD_REMEDIATION_SCOPE,
) -> dict:
    if limit <= 0:
        raise ValueError("limit must be positive")
    if delay_seconds < 0:
        raise ValueError("delay_seconds must be non-negative")
    priority_path = Path(priority_path)
    formal_price_dir = Path(formal_price_dir)
    overlay_dir = Path(overlay_dir)
    snapshot_dir = Path(snapshot_dir)
    state_path = Path(state_path)
    priority_sha = _sha256(priority_path)
    formal_before = _inventory(formal_price_dir)
    overlay = prepare_overlay(formal_price_dir, overlay_dir) if apply else None
    state = _load_state(state_path)
    state["priority_source"] = {
        "path": str(priority_path),
        "sha256": priority_sha,
    }
    state["formal_price_dir"] = str(formal_price_dir)
    state["overlay_price_dir"] = str(overlay_dir)
    state["formal_inventory_before"] = formal_before
    rows = _load_priority_rows(priority_path, remediation_scope)
    selected = []
    for row in rows:
        ticker = str(row["ticker"])
        provider_ticker = str(row["provider_ticker"])
        formal_path = formal_price_dir / f"{ticker.lower()}.csv"
        formal_sha = _sha256(formal_path) if formal_path.exists() else "MISSING"
        if _same_completed_request(
            state["records"].get(ticker),
            provider_ticker=provider_ticker,
            start=start,
            end=end,
            formal_sha256=formal_sha,
            remediation_scope=remediation_scope,
        ):
            continue
        selected.append(row)
        if len(selected) >= limit:
            break
    run_records = []
    for index, row in enumerate(selected):
        record, fetched = _process_one(
            row,
            start=start,
            end=end,
            formal_price_dir=formal_price_dir,
            overlay_dir=overlay_dir,
            snapshot_dir=snapshot_dir,
            apply=apply,
        )
        record["selection_source_sha256"] = priority_sha
        state["records"][record["ticker"]] = record
        run_records.append(record)
        state["last_checkpoint_ticker"] = record["ticker"]
        state["checkpointed_record_count"] = len(state["records"])
        _checkpoint(state_path, state)
        if fetched and delay_seconds and index + 1 < len(selected):
            time.sleep(delay_seconds)
    formal_after = _inventory(formal_price_dir)
    if formal_after != formal_before:
        raise RuntimeError("formal price inventory changed during v14 backfill")
    if apply:
        overlay = prepare_overlay(formal_price_dir, overlay_dir)
    run = {
        "started_from_checkpoint_record_count": (
            len(state["records"]) - len(run_records)
        ),
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "research_only": True,
        "release_status": "BLOCKED",
        "applied_to_overlay": apply,
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "requested_limit": limit,
        "remediation_scope": remediation_scope,
        "selected_tickers": [str(row["ticker"]) for row in selected],
        "result_status_counts": pd.Series(
            [record["status"] for record in run_records], dtype="object"
        ).value_counts().to_dict(),
        "formal_inventory": formal_after,
        "overlay": overlay,
    }
    state["formal_inventory_after"] = formal_after
    state["runs"].append(run)
    state["status"] = "COMPLETE"
    _checkpoint(state_path, state)
    return {**run, "state_path": str(state_path), "records": run_records}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--priority-path", type=Path, default=DEFAULT_PRIORITY_PATH)
    parser.add_argument(
        "--formal-price-dir", type=Path, default=Path(CLEANED_PRICE_DATA_DIR)
    )
    parser.add_argument("--overlay-dir", type=Path, default=DEFAULT_OVERLAY_DIR)
    parser.add_argument("--snapshot-dir", type=Path, default=DEFAULT_SNAPSHOT_DIR)
    parser.add_argument("--state-path", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--start", default="2017-01-01")
    parser.add_argument("--end", default="2026-08-11")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--delay-seconds", type=float, default=1.0)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--remediation-scope",
        choices=REMEDIATION_SCOPES,
        default=HEAD_REMEDIATION_SCOPE,
    )
    args = parser.parse_args()
    result = backfill_batch(
        priority_path=args.priority_path,
        formal_price_dir=args.formal_price_dir,
        overlay_dir=args.overlay_dir,
        snapshot_dir=args.snapshot_dir,
        state_path=args.state_path,
        start=date.fromisoformat(args.start),
        end=date.fromisoformat(args.end),
        limit=args.limit,
        delay_seconds=args.delay_seconds,
        apply=args.apply,
        remediation_scope=args.remediation_scope,
    )
    print(
        json.dumps(
            {
                "state_path": result["state_path"],
                "selected_tickers": result["selected_tickers"],
                "result_status_counts": result["result_status_counts"],
                "formal_inventory": result["formal_inventory"],
                "overlay": result["overlay"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
