"""Audit whether frozen formal rows remain backed by immutable SEC payloads.

This is a research-only provenance check.  It never reparses, merges, or
writes annual/quarterly formal data.  Non-derived formal rows are matched to
their exact SEC Company Facts taxonomy, concept, fiscal end, filing date,
form, accession, and numeric value within the ticker's manifest-bound CIK
chain.  Derived rows are reported separately for a later formula audit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd

from scripts.companyfacts_cache_snapshot import (
    verify_companyfacts_cache_snapshot,
)
from src.io.fundamentals_update import (
    OUTPUT_COLUMNS,
    _read_companyfacts_cache,
    cached_companyfacts_cik_chains_for_symbols,
)


REPORT_FORMAT_VERSION = 1
TRANSFORMED_CONCEPT_PREFIXES = ("derived_", "foreign_")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_number(value) -> str:
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"invalid formal/raw numeric value: {value!r}") from exc
    if not number.is_finite():
        raise ValueError(f"non-finite formal/raw numeric value: {value!r}")
    return str(number.normalize())


def _fact_key(
    taxonomy,
    concept,
    fiscal_end,
    available_date,
    form,
    accession,
    value,
) -> tuple[str, ...]:
    return (
        str(taxonomy).strip(),
        str(concept).strip(),
        str(fiscal_end).strip(),
        str(available_date).strip(),
        str(form).strip(),
        str(accession).strip(),
        _normalized_number(value),
    )


def _raw_fact_keys(payload: dict) -> set[tuple[str, ...]]:
    keys: set[tuple[str, ...]] = set()
    for taxonomy, concepts in payload.get("facts", {}).items():
        for concept, fact in concepts.items():
            for rows in fact.get("units", {}).values():
                for row in rows:
                    required = (
                        row.get("end"),
                        row.get("filed"),
                        row.get("form"),
                        row.get("accn"),
                        row.get("val"),
                    )
                    if any(value is None for value in required):
                        continue
                    try:
                        keys.add(
                            _fact_key(
                                taxonomy,
                                concept,
                                row["end"],
                                row["filed"],
                                row["form"],
                                row["accn"],
                                row["val"],
                            )
                        )
                    except ValueError:
                        continue
    return keys


def _read_formal(path: Path, dataset: str) -> pd.DataFrame:
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    missing = set(OUTPUT_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError(
            f"formal {dataset} file is missing columns: {sorted(missing)}"
        )
    frame = frame[OUTPUT_COLUMNS].copy()
    frame["ticker"] = frame["ticker"].str.strip().str.upper()
    frame["source_dataset"] = dataset
    frame["source_row_number"] = range(2, len(frame) + 2)
    return frame


def audit_companyfacts_formal_sources(
    snapshot_dir: str | Path,
    *,
    annual_output: str | Path,
    quarterly_output: str | Path,
    sample_limit: int = 20,
) -> dict:
    """Match every non-derived formal row to exact immutable raw SEC facts."""
    if sample_limit < 0:
        raise ValueError("sample_limit must be non-negative")
    snapshot = Path(snapshot_dir)
    verified = verify_companyfacts_cache_snapshot(snapshot)
    annual_path = Path(annual_output)
    quarterly_path = Path(quarterly_output)
    formal = pd.concat(
        [
            _read_formal(annual_path, "annual"),
            _read_formal(quarterly_path, "quarterly"),
        ],
        ignore_index=True,
    )
    tickers = sorted(formal["ticker"].unique())
    cik_chains = cached_companyfacts_cik_chains_for_symbols(tickers, snapshot)
    missing_cik_tickers = sorted(
        ticker for ticker in tickers if not cik_chains.get(ticker)
    )

    rows_by_ticker = {
        ticker: group
        for ticker, group in formal.groupby("ticker", sort=False)
    }
    payload_fact_cache: dict[int, set[tuple[str, ...]]] = {}
    summaries = {
        dataset: {
            "formal_row_count": 0,
            "direct_row_count": 0,
            "direct_raw_match_count": 0,
            "direct_raw_missing_count": 0,
            "transformed_row_count": 0,
            "direct_raw_missing_sample": [],
            "transformed_sample": [],
        }
        for dataset in ("annual", "quarterly")
    }
    missing_by_ticker: dict[str, int] = defaultdict(int)

    for ticker in tickers:
        chain = cik_chains.get(ticker, ())
        raw_keys: set[tuple[str, ...]] = set()
        for cik in chain:
            if cik not in payload_fact_cache:
                payload, _ = _read_companyfacts_cache(cik, snapshot)
                payload_fact_cache[cik] = _raw_fact_keys(payload)
            raw_keys.update(payload_fact_cache[cik])

        for row in rows_by_ticker[ticker].itertuples(index=False):
            summary = summaries[row.source_dataset]
            summary["formal_row_count"] += 1
            record = {
                "ticker": ticker,
                "row_number": int(row.source_row_number),
                "fiscal_end": row.fiscal_end,
                "available_date": row.available_date,
                "metric": row.metric,
                "value": row.value,
                "taxonomy": row.taxonomy,
                "concept": row.concept,
                "form": row.form,
                "accession": row.accession,
                "cik_chain": list(chain),
            }
            if row.concept.startswith(TRANSFORMED_CONCEPT_PREFIXES):
                summary["transformed_row_count"] += 1
                if len(summary["transformed_sample"]) < sample_limit:
                    summary["transformed_sample"].append(record)
                continue
            summary["direct_row_count"] += 1
            key = _fact_key(
                row.taxonomy,
                row.concept,
                row.fiscal_end,
                row.available_date,
                row.form,
                row.accession,
                row.value,
            )
            if key in raw_keys:
                summary["direct_raw_match_count"] += 1
            else:
                summary["direct_raw_missing_count"] += 1
                missing_by_ticker[ticker] += 1
                if len(summary["direct_raw_missing_sample"]) < sample_limit:
                    summary["direct_raw_missing_sample"].append(record)

    for summary in summaries.values():
        direct = summary["direct_row_count"]
        summary["direct_raw_match_coverage"] = (
            summary["direct_raw_match_count"] / direct if direct else 1.0
        )

    return {
        "format_version": REPORT_FORMAT_VERSION,
        "research_only": True,
        "snapshot": {
            "snapshot_dir": str(snapshot),
            "snapshot_id": verified["snapshot_id"],
            "cache_manifest_sha256": verified["cache_manifest_sha256"],
            "verified": verified["verified"],
        },
        "formal_outputs": {
            "annual": {
                "path": str(annual_path),
                "sha256": _sha256_file(annual_path),
            },
            "quarterly": {
                "path": str(quarterly_path),
                "sha256": _sha256_file(quarterly_path),
            },
        },
        "ticker_count": len(tickers),
        "missing_cik_ticker_count": len(missing_cik_tickers),
        "missing_cik_tickers": missing_cik_tickers,
        "datasets": summaries,
        "direct_raw_missing_ticker_count": len(missing_by_ticker),
        "direct_raw_missing_by_ticker": [
            {"ticker": ticker, "missing_rows": count}
            for ticker, count in sorted(
                missing_by_ticker.items(),
                key=lambda item: (-item[1], item[0]),
            )
        ],
        "warning": (
            "Direct matches prove that the exact formal source fact remains "
            "in the immutable SEC payload. Derived or foreign-transformed "
            "rows require a separate operand/formula audit before exact "
            "reconstruction can be claimed."
        ),
    }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--annual-output", required=True)
    parser.add_argument("--quarterly-output", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--sample-limit", type=int, default=20)
    args = parser.parse_args()
    report = audit_companyfacts_formal_sources(
        args.snapshot,
        annual_output=args.annual_output,
        quarterly_output=args.quarterly_output,
        sample_limit=args.sample_limit,
    )
    _write_json(Path(args.output), report)
    print(
        json.dumps(
            {
                "output": args.output,
                "snapshot_id": report["snapshot"]["snapshot_id"],
                "annual_direct_match_coverage": report["datasets"]["annual"][
                    "direct_raw_match_coverage"
                ],
                "quarterly_direct_match_coverage": report["datasets"][
                    "quarterly"
                ]["direct_raw_match_coverage"],
                "transformed_row_count": sum(
                    dataset["transformed_row_count"]
                    for dataset in report["datasets"].values()
                ),
                "research_only": True,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
