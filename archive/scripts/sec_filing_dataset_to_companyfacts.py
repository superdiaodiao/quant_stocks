"""Build research-only Company Facts candidates from SEC filing data sets.

SEC Financial Statement Data Sets are filing-level archives, not historical
Company Facts responses.  This converter therefore emits a derived candidate
payload with explicit provenance and never writes the active raw cache or
formal fundamentals.  Duration starts are inferred from ``ddate`` and
``qtrs``; callers must validate the candidate against an authenticated raw
Company Facts source before promoting it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _duration_start(end: pd.Timestamp, qtrs: int) -> pd.Timestamp | None:
    if qtrs <= 0:
        return None
    # Filing datasets expose qtrs but not the XBRL start date.  This is an
    # intentionally conservative calendar approximation for a candidate.
    month_index = end.year * 12 + (end.month - 1) - (3 * qtrs - 1)
    year, month_zero = divmod(month_index, 12)
    return pd.Timestamp(year=year, month=month_zero + 1, day=1)


def _segment_is_empty(value: Any) -> bool:
    return pd.isna(value) or not str(value).strip()


def _taxonomy(version: Any) -> str:
    value = str(version or "")
    return value.split("/", 1)[0] if "/" in value else value or "us-gaap"


def _safe_number(value: Any) -> int | float | None:
    number = pd.to_numeric(value, errors="coerce")
    if pd.isna(number):
        return None
    result = float(number)
    return int(result) if result.is_integer() else result


def _date_string(value: Any) -> str:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return ""
    return parsed.strftime("%Y-%m-%d")


def build_companyfacts_candidate(
    submissions: pd.DataFrame,
    numbers: pd.DataFrame,
    cik: int,
    *,
    source_archive: str,
    source_sha256: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Convert rows for one CIK into a derived Company Facts-shaped payload."""
    submissions = submissions.copy()
    submissions["cik"] = submissions["cik"].astype(str).str.lstrip("0").replace("", "0")
    selected = submissions[submissions["cik"] == str(int(cik))].copy()
    if selected.empty:
        raise ValueError(f"filing dataset has no submissions for CIK {cik}")
    by_accession = selected.set_index("adsh").to_dict("index")
    numbers = numbers[numbers["adsh"].isin(by_accession)].copy()
    facts: dict[str, dict[str, dict[str, dict[str, list[dict[str, Any]]]]]] = defaultdict(
        lambda: defaultdict(lambda: {"units": defaultdict(list)})
    )
    skipped = {"invalid_value": 0, "invalid_end": 0, "segmented": 0}
    for row in numbers.to_dict("records"):
        meta = by_accession[row["adsh"]]
        value = _safe_number(row.get("value"))
        end = pd.to_datetime(row.get("ddate"), format="%Y%m%d", errors="coerce")
        if value is None:
            skipped["invalid_value"] += 1
            continue
        if pd.isna(end):
            skipped["invalid_end"] += 1
            continue
        if not _segment_is_empty(row.get("segments")):
            skipped["segmented"] += 1
            continue
        qtrs = int(float(row.get("qtrs") or 0))
        item: dict[str, Any] = {
            "end": end.strftime("%Y-%m-%d"),
            "val": value,
            "accn": row["adsh"],
            "form": meta.get("form"),
            "filed": _date_string(meta.get("filed")),
            "fy": _safe_number(meta.get("fy")),
            "fp": meta.get("fp"),
            "_sec_filing_dataset_qtrs": qtrs,
        }
        start = _duration_start(end, qtrs)
        if start is not None:
            item["start"] = start.strftime("%Y-%m-%d")
            item["_sec_filing_dataset_duration_start_derived"] = True
        facts[_taxonomy(row.get("version"))][str(row["tag"])]["units"][str(row["uom"])].append(item)
    payload = {
        "cik": int(cik),
        "entityName": str(selected.iloc[0].get("name") or ""),
        "facts": {taxonomy: dict(tags) for taxonomy, tags in facts.items()},
    }
    provenance = {
        "format_version": 1,
        "research_only": True,
        "derived": True,
        "source_archive": source_archive,
        "source_sha256": source_sha256,
        "cik": int(cik),
        "submission_count": len(selected),
        "number_row_count": len(numbers),
        "fact_count": sum(
            len(values)
            for tags in facts.values()
            for concept in tags.values()
            for values in concept["units"].values()
        ),
        "skipped_rows": skipped,
        "warning": (
            "Duration starts are inferred from qtrs; this payload is not an "
            "original Company Facts response and must not enter the active cache."
        ),
    }
    return payload, provenance


def convert_zip_archive(
    archive: Path,
    ciks: list[int],
    output_dir: Path,
) -> dict[str, Any]:
    archive = Path(archive)
    source_sha256 = _sha256(archive)
    output_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as source:
        submissions = pd.read_csv(source.open("sub.txt"), sep="\t", dtype=str)
        requested = {int(cik) for cik in ciks}
        adsh = set(
            submissions.loc[
                submissions["cik"].astype(str).isin({str(cik) for cik in requested}),
                "adsh",
            ]
        )
        chunks = []
        for chunk in pd.read_csv(source.open("num.txt"), sep="\t", dtype=str, chunksize=250_000):
            selected = chunk[chunk["adsh"].isin(adsh)]
            if not selected.empty:
                chunks.append(selected)
        numbers = pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame()
    entries = []
    missing_ciks = []
    for cik in sorted(requested):
        try:
            payload, provenance = build_companyfacts_candidate(
                submissions,
                numbers,
                cik,
                source_archive=str(archive.resolve()),
                source_sha256=source_sha256,
            )
        except ValueError as exc:
            if str(exc) != f"filing dataset has no submissions for CIK {cik}":
                raise
            missing_ciks.append(cik)
            continue
        path = output_dir / f"CIK{cik:010d}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        provenance["path"] = str(path)
        entries.append(provenance)
    result = {
        "format_version": 1,
        "research_only": True,
        "derived": True,
        "archive": str(archive.resolve()),
        "archive_sha256": source_sha256,
        "output_dir": str(output_dir.resolve()),
        "requested_ciks": sorted(requested),
        "missing_ciks": missing_ciks,
        "entries": entries,
        "warning": "Candidate payloads are not active-cache inputs.",
    }
    (output_dir / "provenance.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--cik", type=int, action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = convert_zip_archive(args.archive, args.cik, args.output_dir)
    print(json.dumps({
        "output_dir": result["output_dir"],
        "entries": len(result["entries"]),
        "missing_ciks": result["missing_ciks"],
        "research_only": True,
    }, indent=2))


if __name__ == "__main__":
    main()
