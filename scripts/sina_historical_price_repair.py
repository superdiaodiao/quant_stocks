"""Repair historical U.S. price tails from Sina with reproducible evidence.

The project already uses AkShare's ``stock_us_daily`` interface.  This repair
keeps the raw Sina response, SHA-binds the exact AkShare decoder source, and
requires at least 20 stable overlapping OHLC sessions before appending rows.
Existing dates are never replaced.  When driven by the historical audit, rows
are capped at the ticker's last point-in-time universe membership date.  The
command is a dry run unless ``--apply`` is supplied.
"""

from __future__ import annotations

import argparse
import ast
import gzip
import hashlib
import importlib.util
import io
import json
import re
import subprocess
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd

from scripts.historicaldata_price_import import (
    _atomic_write_json,
    _member_sha256,
    _read_stooq_member,
    _sha256,
    _stooq_member_identity,
    _validate_overlap as _stooq_validate_overlap,
)
from scripts.yahoo_historical_price_repair import (
    PRICE_COLUMNS,
    PRICE_FIELDS,
    _merge_missing,
    _overlap_validation,
    _read_prices,
)
from src.conf import CLEANED_PRICE_DATA_DIR, PROJECT_PATH


SOURCE_URL_TEMPLATE = "https://finance.sina.com.cn/staticdata/us/{ticker}"
DEFAULT_AUDIT = Path(PROJECT_PATH) / "output/historical_data_audit.json"
DEFAULT_CACHE_DIR = (
    Path(PROJECT_PATH) / "output/data_provenance/sina_historical_price_cache"
)
DEFAULT_OUTPUT = (
    Path(PROJECT_PATH)
    / "output/data_provenance/sina_historical_price_repair_2026-08-08.json"
)
DEFAULT_START = "2021-01-01"
DEFAULT_END = "2026-07-17"
PINNED_GITHUB_RAW_RE = re.compile(
    r"^https://raw\.githubusercontent\.com/[^/]+/[^/]+/([0-9a-f]{40})/(.+)$"
)


def _decoder_source() -> tuple[str, Path]:
    spec = importlib.util.find_spec("akshare")
    if spec is None or spec.origin is None:
        raise RuntimeError("AkShare is not installed")
    path = Path(spec.origin).parent / "stock" / "cons.py"
    module = ast.parse(path.read_text(encoding="utf-8"))
    for node in module.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(target, ast.Name) and target.id == "zh_js_decode" for target in targets):
            return str(ast.literal_eval(node.value)), path
    raise RuntimeError(f"AkShare Sina decoder not found in {path}")


def _decode_response(payload: bytes, decoder: str) -> list[dict]:
    text = payload.decode("utf-8")
    _, separator, right = text.partition("=")
    if not separator:
        raise ValueError("Sina response does not contain an encoded assignment")
    encoded = json.loads(right.strip().removesuffix(";"))
    runner = (
        decoder
        + '\nconst fs=require("fs");'
        + 'const value=fs.readFileSync(0,"utf8");'
        + 'process.stdout.write(JSON.stringify(d(value)));'
    )
    completed = subprocess.run(
        ["node", "-e", runner],
        input=encoded.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        timeout=30,
    )
    rows = json.loads(completed.stdout.decode("utf-8"))
    if not isinstance(rows, list):
        raise ValueError("AkShare Sina decoder returned a non-list payload")
    return rows


def _parse_prices(payload: bytes, ticker: str, decoder: str) -> pd.DataFrame:
    frame = pd.DataFrame(_decode_response(payload, decoder))
    required = {"date", "open", "high", "low", "close", "volume"}
    if not required.issubset(frame.columns):
        raise ValueError(f"Sina response is missing columns: {sorted(required - set(frame.columns))}")
    result = frame[["date", "open", "high", "low", "close", "volume"]].copy()
    result["date"] = pd.to_datetime(result["date"], utc=True, errors="raise").dt.tz_convert(None).dt.normalize()
    for column in [*PRICE_FIELDS, "volume"]:
        result[column] = pd.to_numeric(result[column], errors="coerce")
    result = result.dropna(subset=["date", *PRICE_FIELDS])
    result = result.loc[result["close"].gt(0)].drop_duplicates("date", keep="last").sort_values("date")
    result.insert(1, "ticker", ticker)
    return result[PRICE_COLUMNS].reset_index(drop=True)


def _request_bytes(url: str, retries: int = 3) -> bytes:
    error: Exception | None = None
    for attempt in range(retries):
        try:
            request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            return urlopen(request, timeout=30).read()
        except Exception as exc:  # pragma: no cover - network-dependent
            error = exc
            if attempt + 1 < retries:
                time.sleep(2**attempt)
    raise RuntimeError(f"Sina request failed: {error}")


def _load_or_fetch(
    cache_dir: Path, ticker: str, url: str, refresh: bool
) -> tuple[bytes, Path]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{ticker.lower()}.txt.gz"
    if path.exists() and not refresh:
        return gzip.decompress(path.read_bytes()), path
    payload = _request_bytes(url)
    path.write_bytes(gzip.compress(payload, mtime=0))
    return payload, path


def _audit_targets(path: str | Path) -> dict[str, str]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = payload.get("missing_price_while_listed_histories")
    if not isinstance(rows, list):
        raise ValueError("Historical audit is missing price-gap rows")
    return {
        str(row["ticker"]).upper(): str(row["last_membership_date"])
        for row in rows
        if row.get("ticker") and row.get("last_membership_date")
    }


def _stooq_cross_validation(
    archive_path: str | Path, ticker: str, sina: pd.DataFrame
) -> dict:
    archive_path = Path(archive_path)
    candidates: list[str] = []
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.namelist():
            identity = _stooq_member_identity(member)
            if identity is not None and identity[1] == ticker:
                candidates.append(member)
        validations = []
        for member in candidates:
            stooq = _read_stooq_member(archive, member, ticker)
            validation = _stooq_validate_overlap(sina, stooq)
            validations.append({
                "member": member,
                "member_crc32": f"{archive.getinfo(member).CRC:08x}",
                "member_size_bytes": archive.getinfo(member).file_size,
                "member_sha256": _member_sha256(archive, member),
                "source_first_date": stooq["date"].min().strftime("%Y-%m-%d") if not stooq.empty else None,
                "source_last_date": stooq["date"].max().strftime("%Y-%m-%d") if not stooq.empty else None,
                "cross_validation": validation,
            })
    passed = [item for item in validations if item["cross_validation"]["passed"]]
    return {
        "archive_path": str(archive_path),
        "archive_size_bytes": archive_path.stat().st_size,
        "archive_sha256": _sha256(archive_path),
        "members": candidates,
        "member_validations": validations,
        "passed": bool(passed),
    }


def _longest_stable_tail_validation(
    source: pd.DataFrame,
    local: pd.DataFrame,
    *,
    minimum_sessions: int = 20,
    maximum_sessions: int = 252,
) -> dict:
    """Find the longest recent overlap suffix with one stable price scale."""
    overlap_dates = sorted(set(source["date"]) & set(local["date"]))
    upper = min(len(overlap_dates), maximum_sessions)
    for sessions in range(upper, minimum_sessions - 1, -1):
        dates = set(overlap_dates[-sessions:])
        validation = _overlap_validation(
            source.loc[source["date"].isin(dates)],
            local.loc[local["date"].isin(dates)],
        )
        exact_fields = all(
            float(value or 0) == 1.0
            for value in (validation.get("field_within_1pct") or {}).values()
        )
        if exact_fields and bool(validation.get("scale_consistent")):
            source_tail = source.loc[source["date"].isin(dates)]
            local_tail = local.loc[local["date"].isin(dates)]
            volume_overlap = local_tail[["date", "volume"]].merge(
                source_tail[["date", "volume"]],
                on="date",
                suffixes=("_local", "_source"),
            )
            local_volume = pd.to_numeric(
                volume_overlap["volume_local"], errors="coerce"
            )
            source_volume = pd.to_numeric(
                volume_overlap["volume_source"], errors="coerce"
            )
            valid_volume = local_volume.gt(0) & source_volume.gt(0)
            volume_ratios = (
                local_volume[valid_volume] / source_volume[valid_volume]
            ).dropna()
            volume_median = validation.get("volume_median_ratio")
            volume_within_0_1pct = (
                float(
                    (
                        volume_ratios / float(volume_median) - 1.0
                    ).abs().le(0.001).mean()
                )
                if not volume_ratios.empty and volume_median
                else 0.0
            )
            return {
                **validation,
                "passed": True,
                "tail_first_date": overlap_dates[-sessions].strftime("%Y-%m-%d"),
                "tail_last_date": overlap_dates[-1].strftime("%Y-%m-%d"),
                "volume_within_0_1pct": volume_within_0_1pct,
            }
    return {
        "passed": False,
        "sessions": min(upper, len(overlap_dates)),
        "minimum_sessions": minimum_sessions,
    }


def _walk_ticker_records(value: object, ticker: str) -> list[dict]:
    records: list[dict] = []
    if isinstance(value, dict):
        if str(value.get("ticker", "")).upper() == ticker:
            records.append(value)
        for child in value.values():
            records.extend(_walk_ticker_records(child, ticker))
    elif isinstance(value, list):
        for child in value:
            records.extend(_walk_ticker_records(child, ticker))
    return records


def _sec_identity_cross_validation(
    ticker: str, sec_probe_path: str | Path
) -> dict:
    ticker = ticker.upper()
    sec_path = Path(sec_probe_path)
    result: dict[str, object] = {
        "passed": False,
        "sec_probe_path": str(sec_path),
        "sec_probe_sha256": _sha256(sec_path),
    }
    sec_payload = json.loads(sec_path.read_text(encoding="utf-8"))
    sec_records = [
        record for record in _walk_ticker_records(sec_payload, ticker)
        if "matches" in record and "issuers" in record
    ]
    if len(sec_records) != 1:
        return {**result, "reason": "sec_record_not_unique", "sec_candidates": len(sec_records)}
    sec_record = sec_records[0]
    matched_ciks = {
        str(match.get("cik", "")).zfill(10) for match in sec_record.get("matches", [])
        if match.get("cik")
    }
    issuer_ciks = {
        str(issuer.get("cik", "")).zfill(10) for issuer in sec_record.get("issuers", [])
        if issuer.get("cik")
    }
    display_exact = any(
        re.search(
            rf"\([^)]*(?<![A-Z0-9.\-]){re.escape(ticker)}(?![A-Z0-9.\-])[^)]*\)",
            str(match.get("display_name", "")),
            re.I,
        )
        for match in sec_record.get("matches", [])
    )
    passed = (
        sec_record.get("status") == "ok"
        and len(matched_ciks) == 1
        and matched_ciks == issuer_ciks
        and display_exact
        and bool(sec_record.get("search_payload_sha256"))
        and all(
            issuer.get("submission_payload_sha256")
            for issuer in sec_record.get("issuers", [])
        )
    )
    result.update({
        "passed": passed,
        "sec_search_url": sec_record.get("search_url"),
        "sec_search_query": sec_record.get("search_query"),
        "sec_search_payload_sha256": sec_record.get("search_payload_sha256"),
        "sec_cik": next(iter(matched_ciks), None),
        "current_tickers": sorted({
            current
            for issuer in sec_record.get("issuers", [])
            for current in issuer.get("current_tickers", [])
        }),
    })
    if not passed:
        result["reason"] = "sec_identity_gate_failed"
    return result


def _fixed_mirror_sec_cross_validation(
    *,
    ticker: str,
    local: pd.DataFrame,
    overlap: dict,
    mirror_provenance_path: str | Path,
    sec_probe_path: str | Path,
) -> dict:
    """Validate an otherwise-too-short overlap with identity-bound evidence.

    This is deliberately narrower than lowering the normal 20-session gate: the
    complete local file must come from a commit-pinned raw GitHub mirror, all of
    a recent 3+ session suffix must match Sina exactly at one price/volume
    scale, and SEC search/submissions evidence must bind the ticker to one CIK
    and a resolved issuer. Older split-adjustment regimes may differ.
    """
    ticker = ticker.upper()
    mirror_path, sec_path = Path(mirror_provenance_path), Path(sec_probe_path)
    result: dict[str, object] = {
        "passed": False,
        "mirror_provenance_path": str(mirror_path),
        "mirror_provenance_sha256": _sha256(mirror_path),
        "sec_probe_path": str(sec_path),
        "sec_probe_sha256": _sha256(sec_path),
    }
    mirror_payload = json.loads(mirror_path.read_text(encoding="utf-8"))
    mirror_candidates = []
    for record in _walk_ticker_records(mirror_payload, ticker):
        source_url = str(record.get("source_url", ""))
        match = PINNED_GITHUB_RAW_RE.match(source_url)
        if not match:
            continue
        if not match.group(2).lower().endswith(f"/{ticker.lower()}.us.txt"):
            continue
        mirror_candidates.append((record, match.group(1)))
    distinct_mirror_candidates = {
        str(record.get("source_url")): (record, commit)
        for record, commit in mirror_candidates
    }
    if len(distinct_mirror_candidates) != 1:
        return {
            **result,
            "reason": "mirror_record_not_unique",
            "mirror_candidates": len(mirror_candidates),
            "distinct_mirror_candidates": len(distinct_mirror_candidates),
        }
    mirror_record, commit = next(iter(distinct_mirror_candidates.values()))
    local_first = local["date"].min().strftime("%Y-%m-%d")
    local_last = local["date"].max().strftime("%Y-%m-%d")
    mirror_matches_complete_local = (
        int(mirror_record.get("rows") or -1) == len(local)
        and mirror_record.get("first_date") == local_first
        and mirror_record.get("last_date") == local_last
    )
    mirror_historical_overlap = None
    mirror_payload_sha256 = None
    if not mirror_matches_complete_local:
        try:
            mirror_raw = _request_bytes(str(mirror_record["source_url"]))
            mirror_payload_sha256 = hashlib.sha256(mirror_raw).hexdigest()
            mirror_frame = pd.read_csv(io.BytesIO(mirror_raw))
            mirror_frame.columns = [
                str(column).strip("<>").lower() for column in mirror_frame.columns
            ]
            mirror_frame["date"] = pd.to_datetime(
                mirror_frame["date"].astype(str), format="%Y%m%d", errors="coerce"
            )
            mirror_frame["ticker"] = ticker
            mirror_frame = mirror_frame.rename(columns={"vol": "volume"})[
                PRICE_COLUMNS
            ].dropna(subset=["date", "open", "high", "low", "close"])
            mirror_historical_overlap = _overlap_validation(mirror_frame, local)
        except Exception as exc:
            mirror_historical_overlap = {
                "passed": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
    mirror_identity_validated = mirror_matches_complete_local or bool(
        (mirror_historical_overlap or {}).get("passed")
    )
    exact_overlap = (
        int(overlap.get("sessions") or 0) >= 3
        and float(overlap.get("ohlc_within_1pct") or 0) == 1.0
        and all(
            float(value or 0) == 1.0
            for value in (overlap.get("field_within_1pct") or {}).values()
        )
        and abs(float(overlap.get("volume_median_ratio") or 0) - 1.0) <= 0.001
        and float(overlap.get("volume_within_0_1pct") or 0) == 1.0
    )
    sec_payload = json.loads(sec_path.read_text(encoding="utf-8"))
    sec_records = [
        record for record in _walk_ticker_records(sec_payload, ticker)
        if "matches" in record and "issuers" in record
    ]
    if len(sec_records) != 1:
        return {**result, "reason": "sec_record_not_unique", "sec_candidates": len(sec_records)}
    sec_record = sec_records[0]
    matched_ciks = {
        str(match.get("cik", "")).zfill(10) for match in sec_record.get("matches", [])
        if match.get("cik")
    }
    issuer_ciks = {
        str(issuer.get("cik", "")).zfill(10) for issuer in sec_record.get("issuers", [])
        if issuer.get("cik")
    }
    display_exact = any(
        re.search(
            rf"\([^)]*(?<![A-Z0-9.\-]){re.escape(ticker)}(?![A-Z0-9.\-])[^)]*\)",
            str(match.get("display_name", "")),
            re.I,
        )
        for match in sec_record.get("matches", [])
    )
    sec_identity_bound = (
        sec_record.get("status") == "ok"
        and len(matched_ciks) == 1
        and matched_ciks == issuer_ciks
        and display_exact
        and bool(sec_record.get("search_payload_sha256"))
        and all(
            issuer.get("submission_payload_sha256")
            for issuer in sec_record.get("issuers", [])
        )
    )
    result.update({
        "mirror_source_url": mirror_record.get("source_url"),
        "mirror_commit": commit,
        "mirror_matches_complete_local": mirror_matches_complete_local,
        "mirror_payload_sha256": mirror_payload_sha256,
        "mirror_historical_overlap": mirror_historical_overlap,
        "mirror_identity_validated": mirror_identity_validated,
        "exact_recent_overlap": exact_overlap,
        "sec_search_url": sec_record.get("search_url"),
        "sec_search_payload_sha256": sec_record.get("search_payload_sha256"),
        "sec_cik": next(iter(matched_ciks), None),
        "sec_identity_bound": sec_identity_bound,
        "passed": bool(mirror_identity_validated and exact_overlap and sec_identity_bound),
    })
    if not result["passed"]:
        result["reason"] = "evidence_gate_failed"
    return result


def repair_tickers(
    tickers: list[str],
    *,
    membership_ends: dict[str, str] | None = None,
    start: str = DEFAULT_START,
    end: str = DEFAULT_END,
    price_dir: str | Path = CLEANED_PRICE_DATA_DIR,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    output: str | Path = DEFAULT_OUTPUT,
    refresh: bool = False,
    apply: bool = False,
    stooq_archive: str | Path | None = None,
    fixed_mirror_provenance: str | Path | None = None,
    sec_transition_probe: str | Path | None = None,
) -> dict:
    normalized = sorted({ticker.strip().upper() for ticker in tickers if ticker.strip()})
    membership_ends = membership_ends or {}
    price_dir, cache_dir, output = Path(price_dir), Path(cache_dir), Path(output)
    decoder, decoder_path = _decoder_source()
    report = {
        "schema_version": 1,
        "research_only": True,
        "status": "IN_PROGRESS",
        "applied": bool(apply),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_provider": "Sina Finance via the AkShare stock_us_daily interface",
        "source_url_template": SOURCE_URL_TEMPLATE,
        "akshare_decoder_path": str(decoder_path),
        "akshare_decoder_file_sha256": _sha256(decoder_path),
        "akshare_decoder_source_sha256": hashlib.sha256(decoder.encode()).hexdigest(),
        "node_version": subprocess.check_output(["node", "--version"], text=True).strip(),
        "formal_financial_files_modified": False,
        "terminal_returns_modified": False,
        "start": start,
        "end": end,
        "requested_tickers": normalized,
        "records": [],
    }
    _atomic_write_json(output, report)

    for ticker in normalized:
        url = SOURCE_URL_TEMPLATE.format(ticker=ticker)
        price_path = price_dir / f"{ticker.lower()}.csv"
        record: dict[str, object] = {
            "ticker": ticker,
            "source_url": url,
            "price_path": str(price_path),
            "last_membership_date": membership_ends.get(ticker),
        }
        try:
            if not price_path.exists():
                record["status"] = "REJECT_NO_LOCAL_PRICE_FILE"
            else:
                payload, cache_path = _load_or_fetch(cache_dir, ticker, url, refresh)
                source = _parse_prices(payload, ticker, decoder)
                local = _read_prices(price_path)
                source = source.loc[source["date"].between(pd.Timestamp(start), pd.Timestamp(end))]
                full_overlap = _overlap_validation(source, local)
                overlap = full_overlap
                if not full_overlap["passed"]:
                    tail_dates = set(local["date"].sort_values().tail(60))
                    tail_overlap = _overlap_validation(
                        source.loc[source["date"].isin(tail_dates)],
                        local.loc[local["date"].isin(tail_dates)],
                    )
                    overlap = {
                        **(tail_overlap if tail_overlap["passed"] else full_overlap),
                        "validation_scope": "recent_tail_60" if tail_overlap["passed"] else "full_history",
                        "full_history_overlap": full_overlap,
                        "recent_tail_overlap": tail_overlap,
                    }
                    if not tail_overlap["passed"]:
                        stable_tail = _longest_stable_tail_validation(source, local)
                        if stable_tail["passed"]:
                            overlap = {
                                **stable_tail,
                                "validation_scope": "longest_stable_recent_tail",
                                "full_history_overlap": full_overlap,
                                "recent_tail_overlap": tail_overlap,
                            }
                else:
                    overlap = {**full_overlap, "validation_scope": "full_history"}
                stooq_cross = None
                fixed_mirror_sec_cross = None
                sec_exact_tail_cross = None
                if (
                    not overlap["passed"]
                    and 0 < int(overlap["sessions"]) < 20
                    and float(overlap.get("ohlc_within_1pct") or 0) == 1.0
                    and stooq_archive is not None
                ):
                    stooq_cross = _stooq_cross_validation(
                        stooq_archive, ticker, source
                    )
                    if stooq_cross["passed"]:
                        overlap = {
                            **overlap,
                            "passed": True,
                            "validation_scope": "local_short_overlap_plus_stooq_cross_source",
                            "stooq_cross_source": stooq_cross,
                        }
                if (
                    not overlap["passed"]
                    and fixed_mirror_provenance is not None
                    and sec_transition_probe is not None
                ):
                    identity_tail = _longest_stable_tail_validation(
                        source, local, minimum_sessions=3
                    )
                    fixed_mirror_sec_cross = _fixed_mirror_sec_cross_validation(
                        ticker=ticker,
                        local=local,
                        overlap=identity_tail,
                        mirror_provenance_path=fixed_mirror_provenance,
                        sec_probe_path=sec_transition_probe,
                    )
                    if fixed_mirror_sec_cross["passed"]:
                        overlap = {
                            **identity_tail,
                            "passed": True,
                            "validation_scope": "complete_fixed_mirror_recent_exact_tail_plus_sec_identity",
                            "fixed_mirror_sec_cross_source": fixed_mirror_sec_cross,
                        }
                if not overlap["passed"] and sec_transition_probe is not None:
                    exact_tail = _longest_stable_tail_validation(
                        source, local, minimum_sessions=10
                    )
                    if exact_tail["passed"]:
                        sec_exact_tail_cross = _sec_identity_cross_validation(
                            ticker, sec_transition_probe
                        )
                        if sec_exact_tail_cross["passed"]:
                            overlap = {
                                **exact_tail,
                                "passed": True,
                                "validation_scope": "exact_recent_tail_plus_sec_identity",
                                "sec_identity": sec_exact_tail_cross,
                            }
                record.update({
                    "raw_cache_path": str(cache_path),
                    "raw_payload_size_bytes": len(payload),
                    "raw_payload_sha256": hashlib.sha256(payload).hexdigest(),
                    "source_rows": int(len(source)),
                    "source_first_date": source["date"].min().strftime("%Y-%m-%d") if not source.empty else None,
                    "source_last_date": source["date"].max().strftime("%Y-%m-%d") if not source.empty else None,
                    "local_rows_before": int(len(local)),
                    "local_sha256_before": _sha256(price_path),
                    "local_last_date": local["date"].max().strftime("%Y-%m-%d"),
                    "cross_validation": overlap,
                    "stooq_cross_source": stooq_cross,
                    "fixed_mirror_sec_cross_source": fixed_mirror_sec_cross,
                    "sec_exact_tail_cross_source": sec_exact_tail_cross,
                })
                if not overlap["passed"]:
                    record["status"] = "REJECT_CROSS_VALIDATION"
                else:
                    normalized_source = source.copy()
                    price_scale = float(overlap["close_median_ratio"])
                    for field in PRICE_FIELDS:
                        normalized_source[field] *= price_scale
                    if overlap.get("volume_median_ratio") is not None:
                        normalized_source["volume"] *= float(overlap["volume_median_ratio"])
                    cutoff = min(
                        pd.Timestamp(end),
                        pd.Timestamp(membership_ends[ticker])
                        if ticker in membership_ends else pd.Timestamp(end),
                    )
                    missing = normalized_source.loc[
                        normalized_source["date"].le(cutoff)
                        & ~normalized_source["date"].isin(local["date"])
                    ].copy()
                    record.update({
                        "append_cutoff": cutoff.strftime("%Y-%m-%d"),
                        "rows_missing": int(len(missing)),
                        "first_missing_date": missing["date"].min().strftime("%Y-%m-%d") if not missing.empty else None,
                        "last_missing_date": missing["date"].max().strftime("%Y-%m-%d") if not missing.empty else None,
                    })
                    if apply and not missing.empty:
                        rows_added = _merge_missing(price_path, missing, ticker)
                        record.update({
                            "status": "UPDATED",
                            "rows_added": rows_added,
                            "local_sha256_after": _sha256(price_path),
                            "local_rows_after": int(len(_read_prices(price_path))),
                        })
                    else:
                        record["status"] = "DRY_RUN_ELIGIBLE" if not missing.empty else "NO_NEW_ROWS"
                        record["local_sha256_after"] = record["local_sha256_before"]
                        record["local_rows_after"] = record["local_rows_before"]
        except Exception as exc:  # pragma: no cover - network-dependent
            record.update({"status": "ERROR", "error": repr(exc)})
        report["records"].append(record)
        report["checkpointed_records"] = len(report["records"])
        report["last_checkpoint_ticker"] = ticker
        report["last_checkpoint_at_utc"] = datetime.now(timezone.utc).isoformat()
        _atomic_write_json(output, report)

    report["status"] = "COMPLETE"
    report["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(output, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers", help="Optional comma-separated ticker subset")
    parser.add_argument("--historical-audit", help="Use current missing-price rows and PIT cutoffs")
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--price-dir", default=str(CLEANED_PRICE_DATA_DIR))
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--stooq-archive",
        help="Allow a Stooq cross-source fallback only for exact but short local overlap",
    )
    parser.add_argument(
        "--fixed-mirror-provenance",
        help="Commit-pinned mirror provenance for the strict complete-local short-overlap fallback",
    )
    parser.add_argument(
        "--sec-transition-probe",
        help="SEC identity probe required with --fixed-mirror-provenance",
    )
    args = parser.parse_args()
    if not args.tickers and not args.historical_audit:
        parser.error("one of --tickers or --historical-audit is required")
    membership_ends = _audit_targets(args.historical_audit) if args.historical_audit else {}
    tickers = args.tickers.split(",") if args.tickers else list(membership_ends)
    if args.historical_audit:
        requested = {ticker.strip().upper() for ticker in tickers}
        membership_ends = {
            ticker: cutoff
            for ticker, cutoff in membership_ends.items()
            if ticker in requested
        }
    report = repair_tickers(
        tickers,
        membership_ends=membership_ends,
        start=args.start,
        end=args.end,
        price_dir=args.price_dir,
        cache_dir=args.cache_dir,
        output=args.output,
        refresh=args.refresh,
        apply=args.apply,
        stooq_archive=args.stooq_archive,
        fixed_mirror_provenance=args.fixed_mirror_provenance,
        sec_transition_probe=args.sec_transition_probe,
    )
    counts = pd.Series([row["status"] for row in report["records"]]).value_counts()
    print(json.dumps({"requested": len(tickers), "counts": counts.to_dict()}, indent=2))


if __name__ == "__main__":
    main()
