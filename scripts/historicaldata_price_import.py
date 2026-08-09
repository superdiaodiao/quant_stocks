"""Audit or import HistoricalData.net daily CSV archives.

The archive contains active and delisted US ticker files.  This importer is
deliberately conservative: it requires an existing local price file and at
least 20 overlapping sessions whose OHLC values agree within 1%.  Existing
dates are never replaced.  Without ``--apply`` the command is read-only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import zipfile
from datetime import datetime, timezone
from io import TextIOWrapper
from pathlib import Path

import pandas as pd

from src.conf import CLEANED_PRICE_DATA_DIR, PROJECT_PATH


PRICE_COLUMNS = ["date", "ticker", "open", "high", "low", "close", "volume"]
OHLC = ("open", "high", "low", "close")
MIN_OVERLAP_SESSIONS = 20
OHLC_TOLERANCE = 0.01
MIN_OHLC_PASS_FRACTION = 0.99
VOLUME_TOLERANCE = 0.05
MIN_VOLUME_PASS_FRACTION = 0.95
MAX_SCALE_FACTOR_SPREAD = 0.002
MAX_PRICE_VOLUME_RECIPROCAL_ERROR = 0.02
SOURCE_URL = "https://historicaldata.net/stocks.html"
LICENSE_URL = "https://historicaldata.net/about.html#licenseID"
LICENSE_EVIDENCE_PATH = (
    Path(PROJECT_PATH)
    / "output/data_provenance/historicaldata_license_evidence_2026-08-08.json"
)
DELISTED_DATE_RE = re.compile(r"_(\d{4}-\d{2}-\d{2})$")
STOOQ_SOURCE_URL = "https://stooq.com/db/h/"
STOOQ_LICENSE_URL = "https://stooq.com/db/h/"
STOOQ_LICENSE_EVIDENCE_PATH = (
    Path(PROJECT_PATH) / "output/data_provenance/stooq_license_evidence_2026-08-08.json"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _member_sha256(archive: zipfile.ZipFile, member: str) -> str:
    digest = hashlib.sha256()
    with archive.open(member) as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _frame_sha256(frame: pd.DataFrame) -> str:
    canonical = frame[PRICE_COLUMNS].copy()
    canonical["date"] = pd.to_datetime(canonical["date"]).dt.strftime("%Y-%m-%d")
    payload = canonical.to_csv(index=False, lineterminator="\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _archive_member_identity(name: str) -> tuple[str, str] | None:
    filename = Path(name).name
    if not filename.endswith("_day.csv"):
        return None
    stem = filename.removesuffix("_day.csv")
    if stem.startswith("delisted_"):
        stem = stem.removeprefix("delisted_")
        stem = DELISTED_DATE_RE.sub("", stem)
    security_type, separator, ticker = stem.partition("_")
    if not separator or not ticker:
        return None
    return security_type.upper(), ticker.upper()


def _stooq_member_identity(name: str) -> tuple[str, str] | None:
    path = name.replace("\\", "/")
    if not path.lower().endswith(".txt") or "/daily/us/" not in f"/{path.lower()}":
        return None
    ticker = Path(path).stem.upper()
    if ticker.endswith(".US"):
        ticker = ticker.removesuffix(".US")
    return "STOOQ_US", ticker


def _read_member(archive: zipfile.ZipFile, member: str, ticker: str) -> pd.DataFrame:
    with archive.open(member) as raw:
        frame = pd.read_csv(TextIOWrapper(raw, encoding="utf-8-sig"))
    required = {"Time", "Open", "High", "Low", "Close", "Volume"}
    if not required.issubset(frame.columns):
        raise ValueError(f"{member} is missing columns: {sorted(required - set(frame.columns))}")
    result = frame.rename(
        columns={
            "Time": "date",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        }
    )[["date", "open", "high", "low", "close", "volume"]].copy()
    result["date"] = pd.to_datetime(result["date"], errors="raise")
    for column in [*OHLC, "volume"]:
        result[column] = pd.to_numeric(result[column], errors="coerce")
    result = result.dropna(subset=["date", *OHLC]).sort_values("date")
    result.insert(1, "ticker", ticker)
    return result[PRICE_COLUMNS]


def _read_stooq_member(
    archive: zipfile.ZipFile, member: str, ticker: str
) -> pd.DataFrame:
    names = [
        "source_ticker", "frequency", "date", "time", "open", "high",
        "low", "close", "volume", "open_interest",
    ]
    with archive.open(member) as raw:
        frame = pd.read_csv(
            TextIOWrapper(raw, encoding="utf-8-sig"), names=names, header=0
        )
    result = frame[["date", "open", "high", "low", "close", "volume"]].copy()
    result["date"] = pd.to_datetime(
        result["date"].astype(str), format="%Y%m%d", errors="raise"
    )
    for column in [*OHLC, "volume"]:
        result[column] = pd.to_numeric(result[column], errors="coerce")
    result = result.dropna(subset=["date", *OHLC]).sort_values("date")
    result.insert(1, "ticker", ticker)
    return result[PRICE_COLUMNS]


def _detect_source_format(archive: zipfile.ZipFile) -> str:
    names = archive.namelist()
    if any(_archive_member_identity(name) is not None for name in names):
        return "historicaldata"
    if any(_stooq_member_identity(name) is not None for name in names):
        return "stooq"
    raise ValueError("Archive is neither a supported HistoricalData.net nor Stooq daily bundle")


def _load_audit_tickers(audit_path: str | Path, row_key: str) -> list[str]:
    payload = json.loads(Path(audit_path).read_text(encoding="utf-8"))
    rows = payload.get(row_key)
    if not isinstance(rows, list):
        raise ValueError(f"Historical audit is missing {row_key}")
    return sorted(
        {
            str(row["ticker"]).strip().upper()
            for row in rows
            if isinstance(row, dict) and row.get("ticker")
        }
    )


def _load_missing_price_tickers(audit_path: str | Path) -> list[str]:
    return _load_audit_tickers(
        audit_path, "missing_price_while_listed_histories"
    )


def _load_unresolved_terminal_tickers(audit_path: str | Path) -> list[str]:
    return _load_audit_tickers(
        audit_path, "unresolved_terminal_return_histories"
    )


def _read_local(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    frame["date"] = pd.to_datetime(frame["date"], errors="raise")
    return frame[PRICE_COLUMNS].sort_values("date")


def _validate_overlap(local: pd.DataFrame, source: pd.DataFrame) -> dict:
    overlap = local.merge(source, on="date", suffixes=("_local", "_source"))
    result: dict[str, object] = {"sessions": int(len(overlap)), "fields": {}}
    if len(overlap) < MIN_OVERLAP_SESSIONS:
        result["passed"] = False
        result["reason"] = "INSUFFICIENT_OVERLAP"
        return result
    passed = True
    for column in OHLC:
        source_values = overlap[f"{column}_source"].astype(float)
        ratio = overlap[f"{column}_local"].astype(float) / source_values
        valid = source_values.ne(0) & ratio.notna()
        fraction = float((ratio[valid].sub(1).abs() <= OHLC_TOLERANCE).mean())
        result["fields"][column] = {
            "median_ratio": float(ratio[valid].median()),
            "within_1pct": fraction,
        }
        passed = passed and fraction >= MIN_OHLC_PASS_FRACTION
    source_volume = overlap["volume_source"].astype(float)
    volume_ratio = overlap["volume_local"].astype(float) / source_volume
    valid_volume = source_volume.gt(0) & volume_ratio.notna()
    volume_fraction = float(
        (volume_ratio[valid_volume].sub(1).abs() <= VOLUME_TOLERANCE).mean()
    ) if valid_volume.any() else 0.0
    result["fields"]["volume"] = {
        "median_ratio": (
            float(volume_ratio[valid_volume].median()) if valid_volume.any() else None
        ),
        "within_5pct": volume_fraction,
    }
    passed = passed and volume_fraction >= MIN_VOLUME_PASS_FRACTION
    result["passed"] = bool(passed)
    if not passed:
        result["reason"] = "OHLC_MISMATCH"
    return result


def _normalize_split_scale(
    local: pd.DataFrame, source: pd.DataFrame
) -> tuple[pd.DataFrame | None, dict | None]:
    overlap = local.merge(source, on="date", suffixes=("_local", "_source"))
    if len(overlap) < MIN_OVERLAP_SESSIONS:
        return None, None
    price_factors = {}
    for column in OHLC:
        source_values = overlap[f"{column}_source"].astype(float)
        valid = source_values.ne(0) & source_values.notna()
        if not valid.any():
            return None, None
        price_factors[column] = float(
            (
                overlap.loc[valid, f"{column}_local"].astype(float)
                / source_values[valid]
            ).median()
        )
    price_factor = float(pd.Series(price_factors).median())
    if not math.isfinite(price_factor) or price_factor <= 0:
        return None, None
    factor_spread = max(
        abs(value / price_factor - 1) for value in price_factors.values()
    )
    source_volume = overlap["volume_source"].astype(float)
    valid_volume = source_volume.gt(0) & source_volume.notna()
    if not valid_volume.any():
        return None, None
    volume_factor = float(
        (
            overlap.loc[valid_volume, "volume_local"].astype(float)
            / source_volume[valid_volume]
        ).median()
    )
    reciprocal_error = abs(price_factor * volume_factor - 1)
    if (
        factor_spread > MAX_SCALE_FACTOR_SPREAD
        or reciprocal_error > MAX_PRICE_VOLUME_RECIPROCAL_ERROR
    ):
        return None, None
    normalized = source.copy()
    normalized[list(OHLC)] = normalized[list(OHLC)].astype(float) * price_factor
    normalized["volume"] = normalized["volume"].astype(float) * volume_factor
    validation = _validate_overlap(local, normalized)
    if not validation["passed"]:
        return None, None
    evidence = {
        "method": "CONSTANT_SPLIT_SCALE_FROM_OVERLAP",
        "price_factor": price_factor,
        "volume_factor": volume_factor,
        "price_factors_by_field": price_factors,
        "price_factor_relative_spread": factor_spread,
        "price_volume_reciprocal_error": reciprocal_error,
        "normalized_cross_validation": validation,
    }
    return normalized, evidence


def _atomic_write(path: Path, frame: pd.DataFrame) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(temporary, index=False, date_format="%Y-%m-%d")
    os.replace(temporary, path)


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def import_archive(
    archive_path: str | Path,
    tickers: list[str],
    *,
    price_dir: str | Path = CLEANED_PRICE_DATA_DIR,
    output: str | Path = Path(PROJECT_PATH) / "output/data_provenance/historicaldata_price_import.json",
    start: str = "2021-01-01",
    end: str = "2026-07-17",
    apply: bool = False,
    source_format: str = "auto",
    selection_source_path: str | Path | None = None,
    selection_source_key: str | None = None,
) -> dict:
    archive_path, price_dir, output = Path(archive_path), Path(price_dir), Path(output)
    selection_source = (
        Path(selection_source_path) if selection_source_path is not None else None
    )
    normalized = sorted({ticker.strip().upper() for ticker in tickers if ticker.strip()})
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    records = []
    with zipfile.ZipFile(archive_path) as archive:
        detected_source_format = (
            _detect_source_format(archive) if source_format == "auto" else source_format
        )
    if detected_source_format not in {"historicaldata", "stooq"}:
        raise ValueError(f"Unsupported source format: {detected_source_format}")
    if detected_source_format == "stooq":
        source_url = STOOQ_SOURCE_URL
        license_url = STOOQ_LICENSE_URL
        license_evidence_path = STOOQ_LICENSE_EVIDENCE_PATH
    else:
        source_url = SOURCE_URL
        license_url = LICENSE_URL
        license_evidence_path = LICENSE_EVIDENCE_PATH
    payload = {
        "schema_version": 3,
        "research_only": True,
        "status": "IN_PROGRESS",
        "applied": bool(apply),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "archive_path": str(archive_path),
        "archive_filename": archive_path.name,
        "archive_size_bytes": archive_path.stat().st_size,
        "archive_sha256": _sha256(archive_path),
        "source_format": detected_source_format,
        "source_url": source_url,
        "license_url": license_url,
        "license_evidence_path": str(license_evidence_path),
        "license_evidence_sha256": (
            _sha256(license_evidence_path) if license_evidence_path.exists() else None
        ),
        "formal_financial_files_modified": False,
        "terminal_returns_modified": False,
        "start": start,
        "end": end,
        "requested_tickers": normalized,
        "selection_source_path": (
            str(selection_source) if selection_source is not None else None
        ),
        "selection_source_sha256": (
            _sha256(selection_source) if selection_source is not None else None
        ),
        "selection_source_key": selection_source_key,
        "records": records,
    }
    _atomic_write_json(output, payload)

    def checkpoint(record: dict[str, object]) -> None:
        records.append(record)
        payload["last_checkpoint_ticker"] = record["ticker"]
        payload["checkpointed_records"] = len(records)
        _atomic_write_json(output, payload)

    with zipfile.ZipFile(archive_path) as archive:
        members_by_ticker: dict[str, list[tuple[str, str]]] = {}
        for member in archive.namelist():
            identity = (
                _stooq_member_identity(member)
                if detected_source_format == "stooq"
                else _archive_member_identity(member)
            )
            if identity is not None:
                security_type, ticker = identity
                members_by_ticker.setdefault(ticker, []).append((security_type, member))
        for ticker in normalized:
            local_path = price_dir / f"{ticker.lower()}.csv"
            candidates = members_by_ticker.get(ticker, [])
            record: dict[str, object] = {"ticker": ticker, "members": [item[1] for item in candidates]}
            if not local_path.exists():
                record["status"] = "REJECT_NO_LOCAL_PRICE_FILE"
                checkpoint(record)
                continue
            if not candidates:
                record["status"] = "SOURCE_MISSING"
                checkpoint(record)
                continue
            local = _read_local(local_path)
            record["local_rows_before"] = int(len(local))
            record["local_sha256_before"] = _sha256(local_path)
            selected_frames = []
            member_validations = []
            for security_type, member in candidates:
                member_frame = (
                    _read_stooq_member(archive, member, ticker)
                    if detected_source_format == "stooq"
                    else _read_member(archive, member, ticker)
                )
                member_frame = member_frame.loc[
                    member_frame["date"].between(start_ts, end_ts)
                ]
                validation = _validate_overlap(local, member_frame)
                raw_validation = validation
                scale_normalization = None
                if not validation["passed"]:
                    normalized_frame, scale_normalization = _normalize_split_scale(
                        local, member_frame
                    )
                    if normalized_frame is not None:
                        member_frame = normalized_frame
                        validation = scale_normalization[
                            "normalized_cross_validation"
                        ]
                member_validations.append(
                    {
                        "member": member,
                        "security_type": security_type,
                        "member_crc32": f"{archive.getinfo(member).CRC:08x}",
                        "member_size_bytes": archive.getinfo(member).file_size,
                        "member_sha256": _member_sha256(archive, member),
                        "source_first_date": (
                            member_frame["date"].min().strftime("%Y-%m-%d")
                            if not member_frame.empty else None
                        ),
                        "source_last_date": (
                            member_frame["date"].max().strftime("%Y-%m-%d")
                            if not member_frame.empty else None
                        ),
                        "raw_cross_validation": raw_validation,
                        "cross_validation": validation,
                        "scale_normalization": scale_normalization,
                    }
                )
                if validation["passed"]:
                    selected_frames.append(member_frame)
            record["member_validations"] = member_validations
            if not selected_frames:
                record["status"] = "REJECT_CROSS_VALIDATION"
                checkpoint(record)
                continue
            source = (
                pd.concat(selected_frames, ignore_index=True)
                .sort_values("date")
                .drop_duplicates("date", keep="last")
            )
            record["cross_validation"] = _validate_overlap(local, source)
            missing = source.loc[~source["date"].isin(local["date"])].copy()
            missing = missing.sort_values("date")
            record.update(
                {
                    "status": (
                        "UPDATED"
                        if apply and not missing.empty
                        else "DRY_RUN_ELIGIBLE"
                        if not missing.empty
                        else "NO_NEW_ROWS"
                    ),
                    "rows_available": int(len(source)),
                    "rows_missing": int(len(missing)),
                    "first_missing_date": missing["date"].min().strftime("%Y-%m-%d") if not missing.empty else None,
                    "last_missing_date": missing["date"].max().strftime("%Y-%m-%d") if not missing.empty else None,
                    "missing_dates": missing["date"].dt.strftime("%Y-%m-%d").tolist(),
                    "missing_rows_sha256": _frame_sha256(missing) if not missing.empty else None,
                }
            )
            if apply and not missing.empty:
                merged = (
                    pd.concat([local, missing], ignore_index=True)
                    .sort_values("date")
                    .drop_duplicates("date", keep="first")
                )
                _atomic_write(local_path, merged[PRICE_COLUMNS])
                persisted = _read_local(local_path)
                record["local_rows_after"] = int(len(persisted))
                record["local_sha256_after"] = _sha256(local_path)
                record["persisted_appended_rows_sha256"] = _frame_sha256(
                    persisted.loc[persisted["date"].isin(missing["date"])]
                )
            else:
                record["local_rows_after"] = int(len(local))
                record["local_sha256_after"] = record["local_sha256_before"]
            checkpoint(record)
    payload["status"] = "COMPLETE"
    payload["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(output, payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True)
    ticker_source = parser.add_mutually_exclusive_group(required=True)
    ticker_source.add_argument(
        "--tickers", help="Comma-separated historical tickers"
    )
    ticker_source.add_argument(
        "--historical-audit",
        help="Use missing_price_while_listed_histories from this audit JSON",
    )
    ticker_source.add_argument(
        "--unresolved-terminal-audit",
        help="Use unresolved_terminal_return_histories from this audit JSON",
    )
    parser.add_argument("--price-dir", default=str(CLEANED_PRICE_DATA_DIR))
    parser.add_argument("--output", default=str(Path(PROJECT_PATH) / "output/data_provenance/historicaldata_price_import.json"))
    parser.add_argument("--start", default="2021-01-01")
    parser.add_argument("--end", default="2026-07-17")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--source-format",
        choices=("auto", "historicaldata", "stooq"),
        default="auto",
    )
    args = parser.parse_args()
    if args.historical_audit:
        tickers = _load_missing_price_tickers(args.historical_audit)
        selection_source_path = args.historical_audit
        selection_source_key = "missing_price_while_listed_histories"
    elif args.unresolved_terminal_audit:
        tickers = _load_unresolved_terminal_tickers(
            args.unresolved_terminal_audit
        )
        selection_source_path = args.unresolved_terminal_audit
        selection_source_key = "unresolved_terminal_return_histories"
    else:
        tickers = args.tickers.split(",")
        selection_source_path = None
        selection_source_key = None
    report = import_archive(
        args.archive,
        tickers,
        price_dir=args.price_dir,
        output=args.output,
        start=args.start,
        end=args.end,
        apply=args.apply,
        source_format=args.source_format,
        selection_source_path=selection_source_path,
        selection_source_key=selection_source_key,
    )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
