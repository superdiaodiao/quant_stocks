"""Build immutable, research-only Company Facts supplements from SEC archives.

The active Company Facts cache and formal annual/quarterly CSVs are never
modified.  Each output binds an immutable snapshot payload to the exact SHA-256
of every SEC Financial Statement Data Set ZIP used to add filing-level facts.
"""

from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd

from scripts.sec_filing_dataset_to_companyfacts import (
    _sha256,
    convert_zip_archive,
)
from src.io.fundamentals_update import QUARTERLY_METRICS


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _read_snapshot(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if path.suffix == ".gz":
        raw = gzip.decompress(raw)
    value = json.loads(raw)
    if not isinstance(value, dict) or not isinstance(value.get("payload"), dict):
        raise ValueError(f"snapshot envelope has no payload object: {path}")
    return value


def _snapshot_path(snapshot_dir: Path, cik: int) -> Path:
    stem = f"CIK{cik:010d}.json"
    candidates = [snapshot_dir / f"{stem}.gz", snapshot_dir / stem]
    existing = [path for path in candidates if path.exists()]
    if len(existing) != 1:
        raise ValueError(
            f"expected exactly one immutable snapshot payload for CIK {cik}: "
            f"{[str(path) for path in existing]}"
        )
    return existing[0]


def _fact_count(payload: dict[str, Any]) -> int:
    return sum(
        len(rows)
        for concepts in payload.get("facts", {}).values()
        for concept in concepts.values()
        for rows in concept.get("units", {}).values()
    )


def _merge_fact_payload(
    base: dict[str, Any], supplement: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    """Append only facts absent from the immutable base payload."""
    merged = copy.deepcopy(base)
    added = 0
    target_facts = merged.setdefault("facts", {})
    for taxonomy, concepts in supplement.get("facts", {}).items():
        target_concepts = target_facts.setdefault(taxonomy, {})
        for concept_name, concept in concepts.items():
            target_concept = target_concepts.setdefault(
                concept_name,
                {
                    key: copy.deepcopy(value)
                    for key, value in concept.items()
                    if key != "units"
                },
            )
            target_units = target_concept.setdefault("units", {})
            for unit, rows in concept.get("units", {}).items():
                target_rows = target_units.setdefault(unit, [])
                known = {_canonical_sha256(row) for row in target_rows}
                for row in rows:
                    fingerprint = _canonical_sha256(row)
                    if fingerprint in known:
                        continue
                    target_rows.append(copy.deepcopy(row))
                    known.add(fingerprint)
                    added += 1
    return merged, added


def _duration_matches_qtrs(row: dict[str, Any], qtrs: int) -> bool:
    start = row.get("start")
    end = row.get("end")
    if not start or not end:
        return False
    try:
        days = (pd.Timestamp(end) - pd.Timestamp(start)).days
    except (TypeError, ValueError):
        return False
    bounds = {1: (60, 135), 2: (136, 220), 3: (221, 320), 4: (250, 450)}
    low, high = bounds.get(qtrs, (-1, -1))
    return low <= days <= high


def _align_candidate_ends_to_snapshot(
    base: dict[str, Any], candidate: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    """Restore exact 52/53-week ends from same-accession Company Facts rows."""
    anchors: dict[str, list[dict[str, Any]]] = {}
    for concepts in base.get("facts", {}).values():
        for concept in concepts.values():
            for rows in concept.get("units", {}).values():
                for row in rows:
                    accession = str(row.get("accn") or "")
                    if accession:
                        anchors.setdefault(accession, []).append(row)

    aligned = copy.deepcopy(candidate)
    aligned_count = 0
    for concepts in aligned.get("facts", {}).values():
        for concept in concepts.values():
            for rows in concept.get("units", {}).values():
                for row in rows:
                    qtrs = row.get("_sec_filing_dataset_qtrs")
                    # Filing datasets standardize annual dates to calendar
                    # month-end for some 52/53-week issuers. Quarter-length
                    # rows already carry their direct ddate and must retain it.
                    if qtrs != 4:
                        continue
                    original_end = pd.to_datetime(row.get("end"), errors="coerce")
                    if pd.isna(original_end):
                        continue
                    candidates = []
                    for anchor in anchors.get(str(row.get("accn") or ""), []):
                        if not _duration_matches_qtrs(anchor, int(qtrs)):
                            continue
                        anchor_end = pd.to_datetime(anchor.get("end"), errors="coerce")
                        if pd.isna(anchor_end) or anchor_end.year != original_end.year:
                            continue
                        distance = abs((anchor_end - original_end).days)
                        if distance <= 7:
                            candidates.append((anchor_end, distance))
                    if not candidates:
                        continue
                    frequencies = Counter(item[0] for item in candidates)
                    anchor_end = min(
                        frequencies,
                        key=lambda value: (
                            -frequencies[value],
                            abs((value - original_end).days),
                            value,
                        ),
                    )
                    if anchor_end == original_end:
                        continue
                    delta = anchor_end - original_end
                    row["_sec_filing_dataset_original_end"] = row["end"]
                    row["_sec_filing_dataset_end_aligned_to_snapshot"] = True
                    row["end"] = anchor_end.strftime("%Y-%m-%d")
                    if row.get("start"):
                        start = pd.to_datetime(row["start"], errors="coerce")
                        if not pd.isna(start):
                            row["start"] = (start + delta).strftime("%Y-%m-%d")
                    aligned_count += 1
    return aligned, aligned_count


def _target_identity(target: dict[str, Any]) -> str:
    return _canonical_sha256({
        key: target.get(key)
        for key in (
            "ticker", "fiscal_end", "available_date", "metric", "value",
            "accession",
        )
    })


def _filter_candidate_to_targets(
    candidate: dict[str, Any],
    *,
    symbol: str,
    targets: list[dict[str, Any]],
) -> tuple[dict[str, Any], set[str]]:
    """Keep only direct qtrs=1 facts that prove a declared missing row."""
    concepts_by_metric = {
        metric: set(concepts) for metric, concepts in QUARTERLY_METRICS.items()
    }
    selected = {
        "cik": candidate.get("cik"),
        "entityName": candidate.get("entityName"),
        "facts": {},
    }
    matched: set[str] = set()
    symbol_targets = [
        target for target in targets
        if str(target.get("ticker") or "").upper() == symbol.upper()
    ]
    for taxonomy, concepts in candidate.get("facts", {}).items():
        for concept_name, concept in concepts.items():
            possible = [
                target for target in symbol_targets
                if concept_name in concepts_by_metric.get(target.get("metric"), set())
            ]
            if not possible:
                continue
            for unit, rows in concept.get("units", {}).items():
                for row in rows:
                    if row.get("_sec_filing_dataset_qtrs") != 1:
                        continue
                    row_end = pd.to_datetime(row.get("end"), errors="coerce")
                    if pd.isna(row_end):
                        continue
                    row_matches = []
                    for target in possible:
                        target_end = pd.to_datetime(
                            target.get("fiscal_end"), errors="coerce"
                        )
                        if pd.isna(target_end) or abs((row_end - target_end).days) > 7:
                            continue
                        if row.get("accn") != target.get("accession"):
                            continue
                        if row.get("filed") != target.get("available_date"):
                            continue
                        if not math.isclose(
                            float(row.get("val")), float(target.get("value")),
                            rel_tol=1e-9, abs_tol=1e-6,
                        ):
                            continue
                        row_matches.append(target)
                    if not row_matches:
                        continue
                    namespace = selected["facts"].setdefault(taxonomy, {})
                    selected_concept = namespace.setdefault(
                        concept_name,
                        {
                            key: copy.deepcopy(value)
                            for key, value in concept.items()
                            if key != "units"
                        },
                    )
                    selected_row = copy.deepcopy(row)
                    selected_row["_sec_filing_dataset_target_metric"] = (
                        row_matches[0]["metric"]
                    )
                    selected_concept.setdefault("units", {}).setdefault(
                        unit, []
                    ).append(selected_row)
                    matched.update(_target_identity(target) for target in row_matches)
    return selected, matched


def build_supplemented_snapshots(
    snapshot_dir: str | Path,
    archives: list[str | Path],
    ciks: list[int],
    output_dir: str | Path,
    target_manifest: str | Path | None = None,
    target_policy: str = "all",
) -> dict[str, Any]:
    snapshot_dir = Path(snapshot_dir).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_paths = [Path(path).resolve() for path in archives]
    target_manifest_path = (
        Path(target_manifest).resolve() if target_manifest is not None else None
    )
    targets: list[dict[str, Any]] = []
    if target_manifest_path is not None:
        target_document = json.loads(
            target_manifest_path.read_text(encoding="utf-8")
        )
        targets = [
            row for row in target_document.get("records", [])
            if row.get("current_payload_status") == "CONCEPT_ABSENT"
        ]
        if target_policy == "earliest_semantic_fact":
            earliest: dict[tuple[Any, ...], dict[str, Any]] = {}
            for target in sorted(targets, key=lambda row: row["available_date"]):
                key = (
                    target.get("ticker"),
                    target.get("fiscal_end"),
                    target.get("metric"),
                    float(target.get("value")),
                )
                earliest.setdefault(key, target)
            targets = list(earliest.values())
        elif target_policy != "all":
            raise ValueError(f"unsupported target policy: {target_policy}")
    requested = sorted({int(cik) for cik in ciks})
    unresolved_target_symbols: list[str] = []
    if not requested and targets:
        snapshot_manifest = json.loads(
            (snapshot_dir / "manifest.json").read_text(encoding="utf-8")
        )
        target_symbols = {
            str(target.get("ticker") or "").upper() for target in targets
        }
        symbol_to_cik = {
            str(symbol).upper(): int(entry["cik"])
            for entry in snapshot_manifest.get("entries", [])
            for symbol in entry.get("symbols", [])
        }
        requested = sorted({
            symbol_to_cik[symbol]
            for symbol in target_symbols
            if symbol in symbol_to_cik
        })
        unresolved_target_symbols = sorted(target_symbols - symbol_to_cik.keys())
    if not requested:
        raise ValueError("no CIKs were supplied or resolved from the target manifest")
    matched_targets: set[str] = set()

    archive_entries = []
    candidate_dirs = []
    for archive in archive_paths:
        candidate_dir = output_dir / "derived_candidates" / archive.stem
        result = convert_zip_archive(archive, requested, candidate_dir)
        candidate_dirs.append(candidate_dir)
        archive_entries.append({
            "path": str(archive),
            "sha256": result["archive_sha256"],
            "present_ciks": [entry["cik"] for entry in result["entries"]],
            "missing_ciks": result["missing_ciks"],
        })

    entries = []
    for cik in requested:
        source_path = _snapshot_path(snapshot_dir, cik)
        envelope = _read_snapshot(source_path)
        symbol = (envelope.get("symbols") or [""])[0]
        base_payload = envelope["payload"]
        merged_payload = copy.deepcopy(base_payload)
        supplements = []
        for archive, candidate_dir in zip(archive_entries, candidate_dirs):
            candidate_path = candidate_dir / f"CIK{cik:010d}.json"
            if not candidate_path.exists():
                supplements.append({
                    "archive_sha256": archive["sha256"],
                    "candidate_present": False,
                    "facts_added": 0,
                })
                continue
            candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
            if target_manifest_path is not None:
                candidate, matched = _filter_candidate_to_targets(
                    candidate, symbol=symbol, targets=targets
                )
                matched_targets.update(matched)
            candidate, aligned_count = _align_candidate_ends_to_snapshot(
                base_payload, candidate
            )
            before = _fact_count(merged_payload)
            merged_payload, added = _merge_fact_payload(merged_payload, candidate)
            supplements.append({
                "archive_sha256": archive["sha256"],
                "candidate_present": True,
                "candidate_path": str(candidate_path),
                "candidate_payload_sha256": _canonical_sha256(candidate),
                "candidate_fact_count": _fact_count(candidate),
                "facts_before": before,
                "facts_added": added,
                "facts_after": _fact_count(merged_payload),
                "facts_end_aligned_to_snapshot": aligned_count,
            })

        output_envelope = {
            "format_version": 1,
            "research_only": True,
            "derived": True,
            "active_cache_input": False,
            "formal_financial_input": False,
            "source_snapshot": str(source_path),
            "source_snapshot_file_sha256": _sha256(source_path),
            "source_payload_sha256": _canonical_sha256(base_payload),
            "symbols": envelope.get("symbols", []),
            "payload": merged_payload,
            "supplements": supplements,
        }
        output_path = output_dir / f"CIK{cik:010d}.json.gz"
        encoded = (
            json.dumps(output_envelope, ensure_ascii=False, sort_keys=True)
            + "\n"
        ).encode()
        output_path.write_bytes(gzip.compress(encoded, mtime=0))
        entries.append({
            "cik": cik,
            "symbols": envelope.get("symbols", []),
            "source_snapshot": str(source_path),
            "source_snapshot_file_sha256": _sha256(source_path),
            "source_payload_sha256": _canonical_sha256(base_payload),
            "base_fact_count": _fact_count(base_payload),
            "merged_fact_count": _fact_count(merged_payload),
            "facts_added": _fact_count(merged_payload) - _fact_count(base_payload),
            "output_path": str(output_path),
            "output_file_sha256": _sha256(output_path),
            "merged_payload_sha256": _canonical_sha256(merged_payload),
            "supplements": supplements,
        })

    manifest = {
        "format_version": 1,
        "research_only": True,
        "derived": True,
        "active_cache_modified": False,
        "formal_financial_files_modified": False,
        "snapshot_dir": str(snapshot_dir),
        "requested_ciks": requested,
        "unresolved_target_symbols": unresolved_target_symbols,
        "target_manifest": str(target_manifest_path) if target_manifest_path else None,
        "target_manifest_sha256": (
            _sha256(target_manifest_path) if target_manifest_path else None
        ),
        "target_policy": target_policy,
        "target_row_count": len(targets),
        "matched_target_row_count": len(matched_targets),
        "unmatched_target_rows": [
            target for target in targets
            if _target_identity(target) not in matched_targets
        ],
        "archives": archive_entries,
        "entries": entries,
        "warning": (
            "SEC filing datasets are filing-level sources. Duration starts are "
            "derived from qtrs; outputs must remain research-only candidates."
        ),
    }
    manifest_path = output_dir / "provenance.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-dir", type=Path, required=True)
    parser.add_argument("--archive", type=Path, action="append", required=True)
    parser.add_argument("--cik", type=int, action="append", default=[])
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--target-manifest",
        type=Path,
        help="Restrict supplements to direct qtrs=1 proofs of declared missing rows",
    )
    parser.add_argument(
        "--target-policy",
        choices=("all", "earliest_semantic_fact"),
        default="all",
    )
    args = parser.parse_args()
    result = build_supplemented_snapshots(
        args.snapshot_dir, args.archive, args.cik, args.output_dir,
        target_manifest=args.target_manifest,
        target_policy=args.target_policy,
    )
    print(json.dumps({
        "output_dir": str(args.output_dir.resolve()),
        "entries": len(result["entries"]),
        "facts_added": sum(entry["facts_added"] for entry in result["entries"]),
        "research_only": True,
    }, indent=2))


if __name__ == "__main__":
    main()
