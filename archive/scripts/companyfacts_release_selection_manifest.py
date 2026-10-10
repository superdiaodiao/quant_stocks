"""Create and replay a research-only Company Facts release selection lockfile.

The active Company Facts parser is intentionally refreshable.  A historical
formal release, however, may have selected a different concept generation or
quarterly derivation from the same immutable SEC facts.  This tool records the
selection made by that release without changing the formal annual/quarterly
files:

* every non-derived row is bound to an exact raw SEC fact and CIK;
* transformed rows retain their release row and are marked as derived until a
  formula proof is available;
* replay is fail-closed when any raw source is absent or a derived row has not
  been proven.

The manifest is a research lockfile, not permission to replace a formal
release.  Replayed outputs must still be compared and explicitly authorized.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable

import pandas as pd

from scripts.companyfacts_cache_snapshot import verify_companyfacts_cache_snapshot
from src.io.fundamentals_update import (
    OUTPUT_COLUMNS,
    _read_companyfacts_cache,
    cached_companyfacts_cik_chains_for_symbols,
    companyfacts_full_rebuild_recipe_sha256,
)


FORMAT_VERSION = 1
FORMULA_AUDIT_FORMAT_VERSION = 2
TRANSFORMED_PREFIXES = ("derived_", "foreign_")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _normalized_number(value: object) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid numeric value: {value!r}") from exc
    if not number.is_finite():
        raise ValueError(f"non-finite numeric value: {value!r}")
    return format(number, "f")


def _raw_numeric_key(value: object) -> str:
    """Normalize SEC integer/decimal spellings to the same raw identity."""
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid numeric value: {value!r}") from exc
    if not number.is_finite():
        raise ValueError(f"non-finite numeric value: {value!r}")
    if number == 0:
        return "0"
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _date_text(value: object) -> str:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"invalid date value: {value!r}")
    return pd.Timestamp(parsed).date().isoformat()


def _row_values(row: dict[str, object]) -> dict[str, str]:
    values: dict[str, str] = {}
    for column in OUTPUT_COLUMNS:
        value = row.get(column, "")
        if column in {"fiscal_end", "available_date", "fetched_at"}:
            values[column] = _date_text(value)
        elif column == "value":
            values[column] = _normalized_number(value)
        else:
            values[column] = "" if pd.isna(value) else str(value)
    return values


def _row_sha256(values: dict[str, str]) -> str:
    return hashlib.sha256(_canonical_json(values)).hexdigest()


def _row_identity(dataset: str, ordinal: int, values: dict[str, str]) -> tuple:
    """Return the stable identity shared by formula and release audits."""
    return (str(dataset), int(ordinal), _row_sha256(values))


def _raw_key(
    taxonomy: object,
    concept: object,
    fiscal_end: object,
    available_date: object,
    form: object,
    accession: object,
    value: object,
) -> tuple[str, ...]:
    return (
        str(taxonomy).strip(),
        str(concept).strip(),
        _date_text(fiscal_end),
        _date_text(available_date),
        str(form).strip(),
        str(accession).strip(),
        _raw_numeric_key(value),
    )


def _payload_raw_index(
    payload: dict,
    cik: int,
) -> dict[tuple[str, ...], int]:
    index: dict[tuple[str, ...], int] = {}
    for taxonomy, namespace in (payload.get("facts") or {}).items():
        if not isinstance(namespace, dict):
            continue
        for concept, definition in namespace.items():
            units = (definition or {}).get("units", {})
            for unit_rows in units.values():
                if not isinstance(unit_rows, list):
                    continue
                for raw in unit_rows:
                    if not isinstance(raw, dict):
                        continue
                    required = (
                        raw.get("end"),
                        raw.get("filed"),
                        raw.get("form"),
                        raw.get("accn"),
                        raw.get("val"),
                    )
                    if any(value is None for value in required):
                        continue
                    key = _raw_key(
                        taxonomy,
                        concept,
                        raw.get("end"),
                        raw.get("filed"),
                        raw.get("form"),
                        raw.get("accn"),
                        raw.get("val"),
                    )
                    index.setdefault(key, cik)
    return index


def _payload_raw_matches(
    payload: dict,
    needed_keys: set[tuple[str, ...]],
) -> set[tuple[str, ...]]:
    """Find only requested raw facts, without retaining the whole payload index."""
    matches: set[tuple[str, ...]] = set()
    if not needed_keys:
        return matches
    needed_by_taxonomy: dict[str, set[str]] = defaultdict(set)
    for key in needed_keys:
        needed_by_taxonomy[key[0]].add(key[1])
    for taxonomy, namespace in (payload.get("facts") or {}).items():
        if not isinstance(namespace, dict):
            continue
        needed_concepts = needed_by_taxonomy.get(str(taxonomy), set())
        for concept in needed_concepts:
            definition = namespace.get(concept)
            if not isinstance(definition, dict):
                continue
            units = (definition or {}).get("units", {})
            for unit_rows in units.values():
                if not isinstance(unit_rows, list):
                    continue
                for raw in unit_rows:
                    if not isinstance(raw, dict):
                        continue
                    required = (
                        raw.get("end"),
                        raw.get("filed"),
                        raw.get("form"),
                        raw.get("accn"),
                        raw.get("val"),
                    )
                    if any(value is None for value in required):
                        continue
                    try:
                        key = _raw_key(
                            taxonomy,
                            concept,
                            raw.get("end"),
                            raw.get("filed"),
                            raw.get("form"),
                            raw.get("accn"),
                            raw.get("val"),
                        )
                    except ValueError:
                        # SEC occasionally carries non-numeric or malformed
                        # observations.  They cannot bind a formal numeric row.
                        continue
                    if key in needed_keys:
                        matches.add(key)
    return matches


def _raw_requests_for_frames(
    frames: Iterable[tuple[str, pd.DataFrame]],
) -> dict[str, set[tuple[str, ...]]]:
    """Collect exact raw keys required by non-derived formal rows."""
    requests: dict[str, set[tuple[str, ...]]] = defaultdict(set)
    for _dataset, frame in frames:
        for row in frame.to_dict("records"):
            values = _row_values(row)
            if values["concept"].startswith(TRANSFORMED_PREFIXES):
                continue
            requests[values["ticker"].strip().upper()].add(
                _raw_key(
                    values["taxonomy"],
                    values["concept"],
                    values["fiscal_end"],
                    values["available_date"],
                    values["form"],
                    values["accession"],
                    values["value"],
                )
            )
    return dict(requests)


def _raw_sources_for_requests(
    snapshot_dir: Path,
    requests: dict[str, set[tuple[str, ...]]],
) -> dict[str, dict[tuple[str, ...], int]]:
    """Resolve requested raw facts by streaming one SEC payload at a time.

    The previous implementation materialized every raw fact for every ticker,
    which duplicated large CIK indexes and could exceed 1 GB.  This function
    retains only formal-row keys and the CIK that proved each key.
    """
    normalized = {
        str(ticker).strip().upper(): set(keys)
        for ticker, keys in requests.items()
        if str(ticker).strip() and keys
    }
    if not normalized:
        return {}
    chains = cached_companyfacts_cik_chains_for_symbols(
        list(normalized), snapshot_dir
    )
    by_cik: dict[int, dict[tuple[str, ...], list[tuple[str, int]]]] = {}
    for ticker, keys in normalized.items():
        for rank, cik in enumerate(chains[ticker]):
            cik_requests = by_cik.setdefault(int(cik), {})
            for key in keys:
                cik_requests.setdefault(key, []).append((ticker, rank))

    # A lower chain rank is the historical binding preferred by the parser.
    # The CIK tie-break makes the scan deterministic when chains overlap.
    ordered_ciks = sorted(
        by_cik,
        key=lambda cik: (
            min(rank for values in by_cik[cik].values() for _ticker, rank in values),
            cik,
        ),
    )
    selected: dict[str, dict[tuple[str, ...], tuple[int, int]]] = defaultdict(dict)
    for cik in ordered_ciks:
        key_requests = by_cik[cik]
        payload, _fetched_at = _read_companyfacts_cache(cik, snapshot_dir)
        matches = _payload_raw_matches(payload, set(key_requests))
        for key in matches:
            for ticker, rank in key_requests[key]:
                prior = selected[ticker].get(key)
                if prior is None or rank < prior[0]:
                    selected[ticker][key] = (rank, cik)
    return {
        ticker: {key: source[1] for key, source in values.items()}
        for ticker, values in selected.items()
    }


def _raw_index_for_symbols(
    snapshot_dir: Path,
    symbols: Iterable[str],
) -> dict[str, dict[tuple[str, ...], int]]:
    chains = cached_companyfacts_cik_chains_for_symbols(
        list(symbols), snapshot_dir
    )
    by_cik: dict[int, dict[tuple[str, ...], int]] = {}
    result: dict[str, dict[tuple[str, ...], int]] = {}
    for symbol, chain in chains.items():
        symbol_index: dict[tuple[str, ...], int] = {}
        for cik in chain:
            if cik not in by_cik:
                payload, _ = _read_companyfacts_cache(cik, snapshot_dir)
                by_cik[cik] = _payload_raw_index(payload, cik)
            for key, source_cik in by_cik[cik].items():
                symbol_index.setdefault(key, source_cik)
        result[symbol] = symbol_index
    return result


def _read_formal(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    missing = set(OUTPUT_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError(f"{path} is missing output columns: {sorted(missing)}")
    return frame[OUTPUT_COLUMNS]


def _row_record(
    dataset: str,
    ordinal: int,
    row: dict[str, object],
    source_cik: int | None,
    formula_proof: dict | None = None,
) -> dict:
    values = _row_values(row)
    concept = values["concept"]
    transformed = concept.startswith(TRANSFORMED_PREFIXES)
    evidence = {
        "type": (
            "derived_proven"
            if transformed and formula_proof and formula_proof.get("matched")
            else "derived_unproven"
            if transformed
            else "raw"
        ),
        "source_cik": source_cik,
        "formula": concept if transformed else None,
    }
    if formula_proof is not None:
        evidence["proof"] = formula_proof
    return {
        "dataset": dataset,
        "ordinal": ordinal,
        "values": values,
        "row_sha256": _row_sha256(values),
        "evidence": evidence,
    }


def _load_formula_audit_proofs(
    path: str | Path,
    *,
    verified_snapshot: dict,
    annual_path: Path,
    quarterly_path: Path,
) -> tuple[dict[tuple, dict], dict]:
    """Load and bind per-row formula proofs to this snapshot and release."""
    audit_path = Path(path)
    try:
        report = json.loads(audit_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid formula audit report: {audit_path}: {exc}") from exc
    if report.get("format_version") != FORMULA_AUDIT_FORMAT_VERSION:
        raise RuntimeError(
            "formula audit report lacks row-proof format "
            f"{FORMULA_AUDIT_FORMAT_VERSION}"
        )
    if report.get("research_only") is not True:
        raise RuntimeError("formula audit report is not marked research-only")
    snapshot = report.get("snapshot") or {}
    if (
        snapshot.get("snapshot_id") != verified_snapshot["snapshot_id"]
        or snapshot.get("cache_manifest_sha256")
        != verified_snapshot["cache_manifest_sha256"]
        or snapshot.get("verified") is not True
    ):
        raise RuntimeError("formula audit report is bound to another snapshot")
    outputs = report.get("formal_outputs") or {}
    for name, output_path in (("annual", annual_path), ("quarterly", quarterly_path)):
        if (outputs.get(name) or {}).get("sha256") != _sha256_file(output_path):
            raise RuntimeError(
                f"formula audit report {name} output hash does not match current formal file"
            )
    expected_recipe = report.get("rebuild_recipe_sha256")
    if expected_recipe != companyfacts_full_rebuild_recipe_sha256():
        raise RuntimeError("formula audit report parser recipe mismatch")
    proofs: dict[tuple, dict] = {}
    raw_proofs = report.get("row_proofs")
    if not isinstance(raw_proofs, list):
        raise RuntimeError("formula audit report has no row proofs")
    for proof in raw_proofs:
        if not isinstance(proof, dict):
            raise RuntimeError("formula audit report contains an invalid row proof")
        try:
            identity = (
                str(proof["dataset"]),
                int(proof["ordinal"]),
                str(proof["row_sha256"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("formula audit report contains an invalid row identity") from exc
        if not _is_sha256(identity[2]) or not isinstance(proof.get("matched"), bool):
            raise RuntimeError("formula audit report contains an invalid row proof")
        if identity in proofs:
            raise RuntimeError(f"duplicate formula proof for {identity[:2]}")
        proofs[identity] = {
            "dataset": identity[0],
            "ordinal": identity[1],
            "row_sha256": identity[2],
            "matched": bool(proof["matched"]),
            "reason": str(proof.get("reason") or ""),
            "operand_count": int(proof.get("operand_count") or 0),
        }
    metadata = {
        "path": str(audit_path),
        "sha256": _sha256_file(audit_path),
        "format_version": report["format_version"],
        "row_proof_count": len(proofs),
    }
    return proofs, metadata


def create_release_selection_manifest(
    snapshot_dir: str | Path,
    *,
    annual_output: str | Path,
    quarterly_output: str | Path,
    output: str | Path,
    formula_audit: str | Path | None = None,
) -> dict:
    verified = verify_companyfacts_cache_snapshot(snapshot_dir)
    annual_path = Path(annual_output)
    quarterly_path = Path(quarterly_output)
    annual = _read_formal(annual_path)
    quarterly = _read_formal(quarterly_path)
    formula_proofs: dict[tuple, dict] = {}
    formula_metadata = None
    if formula_audit is not None:
        formula_proofs, formula_metadata = _load_formula_audit_proofs(
            formula_audit,
            verified_snapshot=verified,
            annual_path=annual_path,
            quarterly_path=quarterly_path,
        )
    raw_sources = _raw_sources_for_requests(
        Path(snapshot_dir),
        _raw_requests_for_frames(
            (("annual", annual), ("quarterly", quarterly))
        ),
    )
    counts: dict[str, dict[str, int]] = {}
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    try:
        with gzip.open(temporary, "wt", encoding="utf-8", newline="\n") as handle:
            header = {
                "type": "header",
                "format_version": FORMAT_VERSION,
                "research_only": True,
                "snapshot": {
                    "snapshot_id": verified["snapshot_id"],
                    "cache_manifest_sha256": verified["cache_manifest_sha256"],
                    "referenced_file_count": verified["referenced_file_count"],
                },
                "formal_outputs": {
                    "annual": {
                        "path": str(annual_path),
                        "sha256": _sha256_file(annual_path),
                        "row_count": len(annual),
                    },
                    "quarterly": {
                        "path": str(quarterly_path),
                        "sha256": _sha256_file(quarterly_path),
                        "row_count": len(quarterly),
                    },
                },
                "rebuild_recipe_sha256": companyfacts_full_rebuild_recipe_sha256(),
                "output_columns": OUTPUT_COLUMNS,
            }
            if formula_metadata is not None:
                header["formula_audit"] = formula_metadata
            handle.write(json.dumps(header, sort_keys=True) + "\n")
            for dataset, frame in (("annual", annual), ("quarterly", quarterly)):
                dataset_counts = defaultdict(int)
                for ordinal, row in enumerate(frame.to_dict("records")):
                    values = _row_values(row)
                    transformed = values["concept"].startswith(TRANSFORMED_PREFIXES)
                    source_key = (
                        _raw_key(
                            values["taxonomy"],
                            values["concept"],
                            values["fiscal_end"],
                            values["available_date"],
                            values["form"],
                            values["accession"],
                            values["value"],
                        )
                        if not transformed
                        else None
                    )
                    source_cik = (
                        None
                        if transformed
                        else raw_sources.get(values["ticker"].strip().upper(), {}).get(
                            source_key
                        )
                    )
                    if source_cik is None and not transformed:
                        raise RuntimeError(
                            f"formal {dataset} row {ordinal} has no raw source: "
                            f"{values['ticker']} {values['concept']} "
                            f"{values['accession']}"
                        )
                    proof = formula_proofs.get(
                        _row_identity(dataset, ordinal, values)
                    ) if transformed else None
                    record = _row_record(
                        dataset, ordinal, row, source_cik, formula_proof=proof
                    )
                    dataset_counts[record["evidence"]["type"]] += 1
                    handle.write(json.dumps(record, sort_keys=True) + "\n")
                counts[dataset] = dict(dataset_counts)
        os.replace(temporary, output_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return {
        "manifest": str(output_path),
        "format_version": FORMAT_VERSION,
        "research_only": True,
        "snapshot_id": verified["snapshot_id"],
        "cache_manifest_sha256": verified["cache_manifest_sha256"],
        "formal_output_sha256": {
            "annual": _sha256_file(annual_path),
            "quarterly": _sha256_file(quarterly_path),
        },
        "row_counts": {
            "annual": len(annual),
            "quarterly": len(quarterly),
        },
        "evidence_counts": counts,
        "formula_audit": formula_metadata,
    }


def _iter_manifest(path: Path) -> tuple[dict, Iterable[dict]]:
    handle = gzip.open(path, "rt", encoding="utf-8")
    first = handle.readline()
    if not first:
        handle.close()
        raise ValueError(f"empty release selection manifest: {path}")
    header = json.loads(first)
    if header.get("type") != "header" or header.get("format_version") != FORMAT_VERSION:
        handle.close()
        raise ValueError(f"unsupported release selection manifest: {path}")

    def records():
        try:
            for line in handle:
                if line.strip():
                    record = json.loads(line)
                    if record.get("type") == "header":
                        raise ValueError("duplicate manifest header")
                    yield record
        finally:
            handle.close()

    return header, records()


def _validate_derived_proof(record: dict, header: dict) -> None:
    evidence = record.get("evidence") or {}
    formula_audit = header.get("formula_audit") or {}
    proof = evidence.get("proof") or {}
    if (
        not _is_sha256(formula_audit.get("sha256"))
        or formula_audit.get("format_version") != FORMULA_AUDIT_FORMAT_VERSION
    ):
        raise RuntimeError("proven derived row has no formula-audit binding")
    if evidence.get("type") != "derived_proven" or proof.get("matched") is not True:
        raise RuntimeError("derived row proof is not marked matched")
    if (
        proof.get("dataset") != record.get("dataset")
        or int(proof.get("ordinal", -1)) != int(record.get("ordinal", -2))
        or proof.get("row_sha256") != record.get("row_sha256")
    ):
        raise RuntimeError("derived row proof identity mismatch")


def _append_output_rows(
    path: Path,
    rows: list[dict[str, str]],
    *,
    header: bool,
) -> None:
    if not rows:
        return
    pd.DataFrame(rows, columns=OUTPUT_COLUMNS).to_csv(
        path,
        index=False,
        mode="a" if path.exists() else "w",
        header=header,
    )


def replay_release_selection_manifest(
    snapshot_dir: str | Path,
    *,
    manifest: str | Path,
    annual_output: str | Path,
    quarterly_output: str | Path,
    allow_unproven_derived: bool = False,
    exclude_unproven_derived: bool = False,
) -> dict:
    if allow_unproven_derived and exclude_unproven_derived:
        raise ValueError(
            "allow_unproven_derived and exclude_unproven_derived are "
            "mutually exclusive"
        )
    verified = verify_companyfacts_cache_snapshot(snapshot_dir)
    header, records = _iter_manifest(Path(manifest))
    snapshot = header.get("snapshot", {})
    if snapshot.get("snapshot_id") != verified["snapshot_id"] or snapshot.get(
        "cache_manifest_sha256"
    ) != verified["cache_manifest_sha256"]:
        raise RuntimeError("release selection manifest is bound to another snapshot")
    expected_recipe = header.get("rebuild_recipe_sha256")
    actual_recipe = companyfacts_full_rebuild_recipe_sha256()
    if expected_recipe != actual_recipe:
        raise RuntimeError("release selection manifest parser recipe mismatch")
    raw_requests: dict[str, set[tuple[str, ...]]] = defaultdict(set)
    unresolved_count = 0
    manifest_row_counts = {"annual": 0, "quarterly": 0}
    output_row_counts = {"annual": 0, "quarterly": 0}
    for record in records:
        dataset = record.get("dataset")
        values = record.get("values")
        evidence = record.get("evidence") or {}
        if dataset not in {"annual", "quarterly"} or not isinstance(values, dict):
            raise ValueError("invalid release selection row")
        if set(OUTPUT_COLUMNS) - set(values):
            raise ValueError("release selection row is missing output columns")
        if _row_sha256(values) != record.get("row_sha256"):
            raise RuntimeError("release selection row hash mismatch")
        manifest_row_counts[dataset] += 1
        evidence_type = evidence.get("type")
        if evidence_type == "raw":
            raw_requests[values["ticker"].strip().upper()].add(
                _raw_key(
                    values["taxonomy"],
                    values["concept"],
                    values["fiscal_end"],
                    values["available_date"],
                    values["form"],
                    values["accession"],
                    values["value"],
                )
            )
        elif evidence_type == "derived_proven":
            _validate_derived_proof(record, header)
        elif evidence_type == "derived_unproven":
            unresolved_count += 1
        else:
            raise ValueError("invalid release selection evidence type")
        if not (
            evidence_type == "derived_unproven"
            and exclude_unproven_derived
        ):
            output_row_counts[dataset] += 1
    if unresolved_count and not (
        allow_unproven_derived or exclude_unproven_derived
    ):
        raise RuntimeError(
            "release selection contains unproven derived rows "
            f"({unresolved_count}); formula evidence is required before replay"
        )
    raw_sources = _raw_sources_for_requests(Path(snapshot_dir), dict(raw_requests))
    annual_path = Path(annual_output)
    quarterly_path = Path(quarterly_output)
    output_paths = {"annual": annual_path, "quarterly": quarterly_path}
    temporary_paths = {
        dataset: output.with_suffix(output.suffix + ".tmp")
        for dataset, output in output_paths.items()
    }
    for path in temporary_paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
    buffers: dict[str, list[dict[str, str]]] = {"annual": [], "quarterly": []}
    headers_written = {"annual": False, "quarterly": False}
    try:
        second_header, second_records = _iter_manifest(Path(manifest))
        if second_header != header:
            raise RuntimeError("release selection manifest changed during replay")
        for record in second_records:
            if (
                record["evidence"].get("type") == "derived_unproven"
                and exclude_unproven_derived
            ):
                continue
            values = record["values"]
            if record["evidence"].get("type") == "raw":
                key = _raw_key(
                    values["taxonomy"],
                    values["concept"],
                    values["fiscal_end"],
                    values["available_date"],
                    values["form"],
                    values["accession"],
                    values["value"],
                )
                source_cik = raw_sources.get(
                    values["ticker"].strip().upper(), {}
                ).get(key)
                if source_cik != record["evidence"].get("source_cik"):
                    raise RuntimeError("release selection raw source mismatch")
            buffers[record["dataset"]].append(values)
            if len(buffers[record["dataset"]]) >= 8192:
                dataset = record["dataset"]
                _append_output_rows(
                    temporary_paths[dataset],
                    buffers[dataset],
                    header=not headers_written[dataset],
                )
                headers_written[dataset] = True
                buffers[dataset].clear()
        for dataset, rows in buffers.items():
            _append_output_rows(
                temporary_paths[dataset],
                rows,
                header=not headers_written[dataset],
            )
            if rows:
                headers_written[dataset] = True
            rows.clear()
        for dataset, path in temporary_paths.items():
            if not headers_written[dataset]:
                pd.DataFrame(columns=OUTPUT_COLUMNS).to_csv(path, index=False)
        for dataset, output in output_paths.items():
            os.replace(temporary_paths[dataset], output)
    except Exception:
        for path in temporary_paths.values():
            path.unlink(missing_ok=True)
        raise
    return {
        "research_only": True,
        "replayed": True,
        "snapshot_id": verified["snapshot_id"],
        "annual_rows": output_row_counts["annual"],
        "quarterly_rows": output_row_counts["quarterly"],
        "manifest_annual_rows": manifest_row_counts["annual"],
        "manifest_quarterly_rows": manifest_row_counts["quarterly"],
        "unproven_derived_rows": unresolved_count,
        "allow_unproven_derived": allow_unproven_derived,
        "exclude_unproven_derived": exclude_unproven_derived,
        "excluded_unproven_derived_rows": (
            unresolved_count if exclude_unproven_derived else 0
        ),
        "annual_sha256": _sha256_file(annual_path),
        "quarterly_sha256": _sha256_file(quarterly_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--annual-output", required=True)
    parser.add_argument("--quarterly-output", required=True)
    parser.add_argument("--formula-audit")
    parser.add_argument("--create", action="store_true")
    parser.add_argument("--replay", action="store_true")
    parser.add_argument("--allow-unproven-derived", action="store_true")
    parser.add_argument("--exclude-unproven-derived", action="store_true")
    args = parser.parse_args()
    if args.create == args.replay:
        parser.error("choose exactly one of --create or --replay")
    if args.create:
        result = create_release_selection_manifest(
            args.snapshot,
            annual_output=args.annual_output,
            quarterly_output=args.quarterly_output,
            output=args.manifest,
            formula_audit=args.formula_audit,
        )
    else:
        result = replay_release_selection_manifest(
            args.snapshot,
            manifest=args.manifest,
            annual_output=args.annual_output,
            quarterly_output=args.quarterly_output,
            allow_unproven_derived=args.allow_unproven_derived,
            exclude_unproven_derived=args.exclude_unproven_derived,
        )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
