#!/usr/bin/env python3
"""Recover strict research-only foreign quarters from SEC filing datasets.

The SEC Financial Statement Data Sets retain filing dates, presentation roles,
and an official ``qtrs`` duration count.  This adapter only accepts unsegmented
income-statement facts and reconstructs a single quarter when the filing
contains either a direct ``qtrs=1`` value or an adjacent cumulative fact needed
for an exact difference.  An isolated YTD value is never relabelled as a
quarter.

Outputs are immutable research evidence.  They do not modify the active SEC
cache, formal fundamentals, a frozen policy, or release eligibility.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path
from typing import Any

import pandas as pd

from src.research.foreign_quarterly_diagnostics import (
    FOREIGN_FORMS,
    FOREIGN_METRIC_CONCEPTS,
)


DEFAULT_ARCHIVE_DIR = Path("output/data_provenance/sec_filing_dataset_cache")
DEFAULT_DIAGNOSTIC = Path(
    "output/research_only/v14/"
    "foreign_quarterly_diagnostics_existing_reparse5_foreign_ay_broad_formal.csv"
)
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/sec_filing_dataset_foreign_quarters_2019_2021"
)
SUPPORTED_METRICS = ("revenue", "net_income")
OUTPUT_COLUMNS = [
    "ticker",
    "fiscal_end",
    "available_date",
    "metric",
    "value",
    "taxonomy",
    "concept",
    "form",
    "accession",
    "unit",
    "source",
    "source_archive",
    "source_archive_sha256",
    "derivation_prior_accession",
]
UNMAPPED_LABEL_PATTERN = re.compile(
    r"revenue|sales|turnover|net income|net loss|profit.*loss|loss.*profit|"
    r"profit for|loss for|income for",
    flags=re.IGNORECASE,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _taxonomy(version: Any) -> str:
    value = str(version or "")
    if "/" in value:
        return value.split("/", 1)[0]
    return "custom"


def _standard_metric_map() -> dict[str, tuple[str, int]]:
    result: dict[str, tuple[str, int]] = {}
    for metric in SUPPORTED_METRICS:
        for priority, concept in enumerate(FOREIGN_METRIC_CONCEPTS[metric]):
            result.setdefault(concept, (metric, priority))
    return result


def load_custom_registry(path: Path | None) -> pd.DataFrame:
    columns = ["ticker", "cik", "tag", "metric", "statement_label"]
    if path is None:
        return pd.DataFrame(columns=columns)
    frame = pd.read_csv(path, dtype=str).fillna("")
    missing = sorted(set(columns) - set(frame.columns))
    if missing:
        raise ValueError(f"custom registry missing columns: {missing}")
    frame = frame[columns].copy()
    frame["ticker"] = frame["ticker"].str.upper().str.strip()
    frame["tag"] = frame["tag"].str.strip()
    frame["metric"] = frame["metric"].str.strip()
    frame["statement_label"] = frame["statement_label"].str.strip()
    frame["cik"] = pd.to_numeric(frame["cik"], errors="raise").astype(int)
    unsupported = sorted(set(frame["metric"]) - set(SUPPORTED_METRICS))
    if unsupported:
        raise ValueError(f"custom registry has unsupported metrics: {unsupported}")
    if frame[["ticker", "tag", "statement_label"]].eq("").any().any():
        raise ValueError("custom registry keys and exact labels must be non-empty")
    if frame.duplicated(["cik", "tag"]).any():
        raise ValueError("custom registry contains duplicate CIK/tag mappings")
    return frame


def _archive_metric_rows(
    archive: Path,
    symbol_by_cik: dict[int, str],
    custom_registry: pd.DataFrame,
) -> pd.DataFrame:
    standard = _standard_metric_map()
    archive_sha = _sha256(archive)
    with zipfile.ZipFile(archive) as source:
        submissions = pd.read_csv(
            source.open("sub.txt"),
            sep="\t",
            dtype=str,
            usecols=["adsh", "cik", "name", "form", "filed", "fy", "fp"],
        )
        submissions["cik"] = pd.to_numeric(submissions["cik"], errors="coerce")
        submissions = submissions.loc[
            submissions["cik"].isin(symbol_by_cik)
            & submissions["form"].isin(FOREIGN_FORMS)
        ].copy()
        if submissions.empty:
            return pd.DataFrame()
        submissions["cik"] = submissions["cik"].astype(int)
        submissions["ticker"] = submissions["cik"].map(symbol_by_cik)

        presentation = pd.read_csv(
            source.open("pre.txt"),
            sep="\t",
            dtype=str,
            usecols=["adsh", "stmt", "tag", "version", "plabel"],
        )
        presentation = presentation.loc[
            presentation["adsh"].isin(submissions["adsh"])
            & presentation["stmt"].eq("IS")
        ].copy()
        if presentation.empty:
            return pd.DataFrame()
        presentation = presentation.merge(
            submissions[["adsh", "cik", "ticker"]], on="adsh", how="inner"
        )
        presentation["metric"] = presentation["tag"].map(
            lambda value: standard.get(str(value), (None, None))[0]
        )
        presentation["concept_priority"] = presentation["tag"].map(
            lambda value: standard.get(str(value), (None, None))[1]
        )
        if not custom_registry.empty:
            custom = presentation.merge(
                custom_registry,
                on=["ticker", "cik", "tag"],
                how="left",
                suffixes=("", "_registry"),
            )
            exact_label = custom["plabel"].fillna("").str.strip().eq(
                custom["statement_label"].fillna("").str.strip()
            )
            use_custom = custom["metric"].isna() & exact_label & custom[
                "metric_registry"
            ].notna()
            custom.loc[use_custom, "metric"] = custom.loc[
                use_custom, "metric_registry"
            ]
            custom.loc[use_custom, "concept_priority"] = 10_000
            presentation = custom[presentation.columns.tolist() + [
                "metric", "concept_priority"
            ]].loc[:, ~pd.Index(presentation.columns.tolist() + [
                "metric", "concept_priority"
            ]).duplicated()]
        presentation = presentation.loc[presentation["metric"].notna()].copy()
        presentation = presentation.drop_duplicates(
            ["adsh", "tag", "version", "metric"]
        )
        if presentation.empty:
            return pd.DataFrame()

        accession_set = set(presentation["adsh"])
        tag_set = set(presentation["tag"])
        number_chunks = []
        for chunk in pd.read_csv(
            source.open("num.txt"), sep="\t", dtype=str, chunksize=250_000
        ):
            segment_empty = chunk["segments"].fillna("").str.strip().eq("")
            selected = chunk.loc[
                chunk["adsh"].isin(accession_set)
                & chunk["tag"].isin(tag_set)
                & segment_empty
            ]
            if not selected.empty:
                number_chunks.append(selected)
        if not number_chunks:
            return pd.DataFrame()
        numbers = pd.concat(number_chunks, ignore_index=True)

    joined = numbers.merge(
        presentation[
            [
                "adsh", "tag", "version", "metric", "concept_priority",
                "plabel",
            ]
        ],
        on=["adsh", "tag", "version"],
        how="inner",
    ).merge(submissions, on="adsh", how="inner")
    joined["qtrs"] = pd.to_numeric(joined["qtrs"], errors="coerce")
    joined["value"] = pd.to_numeric(joined["value"], errors="coerce")
    joined["end"] = pd.to_datetime(
        joined["ddate"], format="%Y%m%d", errors="coerce"
    )
    joined["filed_date"] = pd.to_datetime(
        joined["filed"], format="%Y%m%d", errors="coerce"
    )
    joined["unit"] = joined["uom"].fillna("").str.strip()
    joined = joined.loc[
        joined["qtrs"].isin([1, 2, 3, 4])
        & joined["value"].notna()
        & joined["end"].notna()
        & joined["filed_date"].notna()
        & joined["unit"].str.match(re.compile(r"^[A-Z]{3}$"))
    ].copy()
    joined["qtrs"] = joined["qtrs"].astype(int)
    joined["taxonomy"] = joined["version"].map(_taxonomy)
    joined["source_archive"] = archive.name
    joined["source_archive_sha256"] = archive_sha
    return joined[
        [
            "ticker", "cik", "metric", "taxonomy", "tag", "plabel",
            "concept_priority", "unit", "end", "filed_date", "value",
            "qtrs", "form", "adsh", "source_archive",
            "source_archive_sha256",
        ]
    ].drop_duplicates()


def scan_archives(
    archives: list[Path],
    symbol_by_cik: dict[int, str],
    custom_registry: pd.DataFrame | None = None,
) -> pd.DataFrame:
    registry = custom_registry if custom_registry is not None else load_custom_registry(None)
    frames = [
        _archive_metric_rows(Path(archive), symbol_by_cik, registry)
        for archive in archives
    ]
    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        return pd.DataFrame(columns=[
            "ticker", "cik", "metric", "taxonomy", "tag", "plabel",
            "concept_priority", "unit", "end", "filed_date", "value",
            "qtrs", "form", "adsh", "source_archive",
            "source_archive_sha256",
        ])
    return pd.concat(frames, ignore_index=True).drop_duplicates()


def _archive_unmapped_candidates(
    archive: Path,
    symbol_by_cik: dict[int, str],
    custom_registry: pd.DataFrame,
) -> pd.DataFrame:
    standard_tags = set(_standard_metric_map())
    archive_sha = _sha256(archive)
    with zipfile.ZipFile(archive) as source:
        submissions = pd.read_csv(
            source.open("sub.txt"),
            sep="\t",
            dtype=str,
            usecols=["adsh", "cik", "form", "filed"],
        )
        submissions["cik"] = pd.to_numeric(submissions["cik"], errors="coerce")
        submissions = submissions.loc[
            submissions["cik"].isin(symbol_by_cik)
            & submissions["form"].isin(FOREIGN_FORMS)
        ].copy()
        if submissions.empty:
            return pd.DataFrame()
        submissions["cik"] = submissions["cik"].astype(int)
        submissions["ticker"] = submissions["cik"].map(symbol_by_cik)
        presentation = pd.read_csv(
            source.open("pre.txt"),
            sep="\t",
            dtype=str,
            usecols=["adsh", "stmt", "tag", "version", "plabel"],
        )
        presentation = presentation.loc[
            presentation["adsh"].isin(submissions["adsh"])
            & presentation["stmt"].eq("IS")
            & ~presentation["tag"].isin(standard_tags)
            & presentation["plabel"].fillna("").str.contains(
                UNMAPPED_LABEL_PATTERN, regex=True
            )
        ].copy()
        if presentation.empty:
            return pd.DataFrame()
        presentation = presentation.merge(
            submissions, on="adsh", how="inner"
        )
        if not custom_registry.empty:
            accepted = presentation.merge(
                custom_registry,
                on=["ticker", "cik", "tag"],
                how="left",
            )
            mapped = accepted["plabel"].fillna("").str.strip().eq(
                accepted["statement_label"].fillna("").str.strip()
            ) & accepted["metric"].notna()
            presentation = accepted.loc[~mapped, presentation.columns]
        presentation = presentation.drop_duplicates(
            ["adsh", "tag", "version", "plabel"]
        )
        accession_set = set(presentation["adsh"])
        tag_set = set(presentation["tag"])
        chunks = []
        for chunk in pd.read_csv(
            source.open("num.txt"), sep="\t", dtype=str, chunksize=250_000
        ):
            selected = chunk.loc[
                chunk["adsh"].isin(accession_set)
                & chunk["tag"].isin(tag_set)
                & chunk["segments"].fillna("").str.strip().eq("")
            ]
            if not selected.empty:
                chunks.append(selected)
        if not chunks:
            return pd.DataFrame()
        numbers = pd.concat(chunks, ignore_index=True)
    joined = numbers.merge(
        presentation,
        on=["adsh", "tag", "version"],
        how="inner",
    )
    joined["qtrs"] = pd.to_numeric(joined["qtrs"], errors="coerce")
    joined["value"] = pd.to_numeric(joined["value"], errors="coerce")
    joined = joined.loc[
        joined["qtrs"].isin([1, 2, 3, 4])
        & joined["value"].notna()
        & joined["uom"].fillna("").str.match(r"^[A-Z]{3}$")
    ].copy()
    joined["source_archive"] = archive.name
    joined["source_archive_sha256"] = archive_sha
    return joined[
        [
            "ticker", "cik", "form", "filed", "tag", "version", "plabel",
            "ddate", "qtrs", "uom", "value", "adsh", "source_archive",
            "source_archive_sha256",
        ]
    ].drop_duplicates()


def scan_unmapped_candidates(
    archives: list[Path],
    symbol_by_cik: dict[int, str],
    custom_registry: pd.DataFrame | None = None,
) -> pd.DataFrame:
    registry = custom_registry if custom_registry is not None else load_custom_registry(None)
    frames = [
        _archive_unmapped_candidates(Path(archive), symbol_by_cik, registry)
        for archive in archives
    ]
    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        return pd.DataFrame(columns=[
            "ticker", "cik", "form", "filed", "tag", "version", "plabel",
            "ddate", "qtrs", "uom", "value", "adsh", "source_archive",
            "source_archive_sha256",
        ])
    return pd.concat(frames, ignore_index=True).drop_duplicates()


def reconstruct_quarters(
    raw: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return strict quarters plus unresolved same-date value conflicts."""
    candidates: list[dict[str, Any]] = []
    if raw.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS), pd.DataFrame()
    ordered = raw.sort_values(["ticker", "metric", "tag", "unit", "end", "filed_date"])
    for current in ordered.to_dict("records"):
        base = {
            "ticker": current["ticker"],
            "fiscal_end": current["end"],
            "available_date": current["filed_date"],
            "metric": current["metric"],
            "taxonomy": current["taxonomy"],
            "concept": current["tag"],
            "form": current["form"],
            "accession": current["adsh"],
            "unit": current["unit"],
            "source_archive": current["source_archive"],
            "source_archive_sha256": current["source_archive_sha256"],
            "concept_priority": int(current["concept_priority"]),
            "derivation_prior_accession": "",
        }
        if current["qtrs"] == 1:
            candidates.append({
                **base,
                "value": float(current["value"]),
                "source": "explicit_qtrs_1",
                "source_rank": 0,
            })
            continue
        prior = ordered.loc[
            ordered["ticker"].eq(current["ticker"])
            & ordered["metric"].eq(current["metric"])
            & ordered["taxonomy"].eq(current["taxonomy"])
            & ordered["tag"].eq(current["tag"])
            & ordered["unit"].eq(current["unit"])
            & ordered["qtrs"].eq(current["qtrs"] - 1)
            & ordered["end"].lt(current["end"])
            & ordered["filed_date"].le(current["filed_date"])
        ].copy()
        if prior.empty:
            continue
        prior["gap_days"] = (current["end"] - prior["end"]).dt.days
        prior = prior.loc[prior["gap_days"].between(60, 135)]
        if prior.empty:
            continue
        previous = prior.sort_values(["end", "filed_date", "adsh"]).iloc[-1]
        source = "derived_q4" if current["qtrs"] == 4 else "derived_ytd"
        candidates.append({
            **base,
            "value": float(current["value"] - previous["value"]),
            "source": source,
            "source_rank": 1 if source == "derived_ytd" else 2,
            "derivation_prior_accession": previous["adsh"],
        })
    if not candidates:
        return pd.DataFrame(columns=OUTPUT_COLUMNS), pd.DataFrame()
    candidate = pd.DataFrame(candidates).sort_values(
        [
            "ticker", "metric", "unit", "fiscal_end", "available_date",
            "source_rank", "concept_priority", "accession",
        ],
        kind="stable",
    )
    accepted = []
    conflicts = []
    keys = ["ticker", "metric", "unit", "fiscal_end"]
    for key, group in candidate.groupby(keys, sort=False):
        earliest = group.loc[group["available_date"].eq(group["available_date"].min())]
        best_rank = earliest[["source_rank", "concept_priority"]].apply(tuple, axis=1).min()
        ranks = earliest[["source_rank", "concept_priority"]].apply(tuple, axis=1)
        best = earliest.loc[ranks.map(lambda value: value == best_rank)]
        values = best["value"].round(6).drop_duplicates()
        if len(values) > 1:
            conflicts.append({
                **dict(zip(keys, key)),
                "available_date": best["available_date"].min(),
                "values": "|".join(map(str, sorted(values.tolist()))),
                "accessions": "|".join(sorted(set(best["accession"]))),
            })
            continue
        accepted.append(best.iloc[0])
    result = pd.DataFrame(accepted)
    if result.empty:
        result = pd.DataFrame(columns=OUTPUT_COLUMNS)
    else:
        result = result[OUTPUT_COLUMNS].sort_values(
            ["ticker", "fiscal_end", "metric"]
        ).reset_index(drop=True)
    return result, pd.DataFrame(conflicts)


def _longest_chain(values: list[pd.Timestamp]) -> int:
    ordered = sorted(set(values))
    if not ordered:
        return 0
    longest = current = 1
    for left, right in zip(ordered, ordered[1:]):
        if 60 <= (right - left).days <= 135:
            current += 1
            longest = max(longest, current)
        else:
            current = 1
    return longest


def _ticker_diagnostics(frame: pd.DataFrame, symbols: list[str]) -> list[dict[str, Any]]:
    rows = []
    for symbol in symbols:
        selected = frame.loc[frame["ticker"].eq(symbol)]
        revenue = selected.loc[selected["metric"].eq("revenue")]
        income = selected.loc[selected["metric"].eq("net_income")]
        common_units = sorted(set(revenue["unit"]) & set(income["unit"]))
        paired = []
        timely_paired = []
        for unit in common_units:
            revenue_unit = revenue.loc[revenue["unit"].eq(unit), [
                "fiscal_end", "available_date"
            ]]
            income_unit = income.loc[income["unit"].eq(unit), [
                "fiscal_end", "available_date"
            ]]
            matched = revenue_unit.merge(
                income_unit,
                on="fiscal_end",
                suffixes=("_revenue", "_income"),
            )
            matched["available_date"] = matched[
                ["available_date_revenue", "available_date_income"]
            ].max(axis=1)
            matched["lag_days"] = (
                matched["available_date"] - matched["fiscal_end"]
            ).dt.days
            paired.extend(matched["fiscal_end"].tolist())
            timely_paired.extend(
                matched.loc[matched["lag_days"].between(0, 150), "fiscal_end"].tolist()
            )
        rows.append({
            "ticker": symbol,
            "quarter_row_count": len(selected),
            "revenue_quarter_count": len(revenue),
            "net_income_quarter_count": len(income),
            "paired_quarter_count": len(set(paired)),
            "longest_continuous_paired_quarters": _longest_chain(paired),
            "timely_paired_quarter_count": len(set(timely_paired)),
            "longest_continuous_timely_paired_quarters": _longest_chain(
                timely_paired
            ),
            "currencies": common_units,
        })
    return rows


def run(
    *,
    diagnostic_path: Path = DEFAULT_DIAGNOSTIC,
    archive_dir: Path = DEFAULT_ARCHIVE_DIR,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    start_year: int = 2019,
    end_year: int = 2021,
    custom_registry_path: Path | None = None,
) -> dict[str, Any]:
    diagnostic = pd.read_csv(diagnostic_path)
    targets = diagnostic[["ticker", "cik"]].dropna().copy()
    targets["ticker"] = targets["ticker"].astype(str).str.upper()
    targets["cik"] = pd.to_numeric(targets["cik"], errors="raise").astype(int)
    if targets.duplicated("cik").any():
        raise ValueError("diagnostic maps one CIK to multiple target rows")
    symbol_by_cik = targets.set_index("cik")["ticker"].to_dict()
    archives = [
        archive_dir / f"{year}q{quarter}.zip"
        for year in range(start_year, end_year + 1)
        for quarter in range(1, 5)
    ]
    missing = [str(path) for path in archives if not path.exists()]
    if missing:
        raise FileNotFoundError(f"required SEC filing datasets missing: {missing}")
    registry = load_custom_registry(custom_registry_path)
    raw = scan_archives(archives, symbol_by_cik, registry)
    unmapped = scan_unmapped_candidates(archives, symbol_by_cik, registry)
    quarters, conflicts = reconstruct_quarters(raw)

    output_dir.mkdir(parents=True, exist_ok=True)
    raw_path = output_dir / "raw_income_statement_facts.csv"
    quarters_path = output_dir / "strict_quarterly_facts.csv"
    conflicts_path = output_dir / "conflicts.csv"
    unmapped_path = output_dir / "unmapped_income_statement_candidates.csv"
    raw.to_csv(raw_path, index=False)
    quarters.to_csv(quarters_path, index=False)
    conflicts.to_csv(conflicts_path, index=False)
    unmapped.to_csv(unmapped_path, index=False)
    archive_bindings = [
        {"path": str(path), "sha256": _sha256(path)} for path in archives
    ]
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "promotion_eligible": False,
        "release_status": "BLOCKED",
        "period": {"start_year": start_year, "end_year": end_year},
        "method": (
            "Unsegmented SEC filing-dataset income-statement facts; qtrs=1 "
            "direct quarters or exact adjacent cumulative differences only."
        ),
        "isolated_ytd_values_relabelled": False,
        "target_count": len(symbol_by_cik),
        "raw_fact_count": len(raw),
        "strict_quarter_row_count": len(quarters),
        "conflict_count": len(conflicts),
        "unmapped_candidate_row_count": len(unmapped),
        "ticker_diagnostics": _ticker_diagnostics(
            quarters, sorted(symbol_by_cik.values())
        ),
        "inputs": {
            "diagnostic": {
                "path": str(diagnostic_path), "sha256": _sha256(diagnostic_path)
            },
            "custom_registry": (
                {
                    "path": str(custom_registry_path),
                    "sha256": _sha256(custom_registry_path),
                    "row_count": len(registry),
                }
                if custom_registry_path is not None else None
            ),
            "archives": archive_bindings,
        },
        "outputs": {
            "raw": {"path": str(raw_path), "sha256": _sha256(raw_path)},
            "quarters": {
                "path": str(quarters_path), "sha256": _sha256(quarters_path)
            },
            "conflicts": {
                "path": str(conflicts_path), "sha256": _sha256(conflicts_path)
            },
            "unmapped_candidates": {
                "path": str(unmapped_path), "sha256": _sha256(unmapped_path)
            },
        },
        "guardrail": (
            "This artifact may repair research inputs only. It cannot modify "
            "formal fundamentals, freeze adaptive parameters, or authorize trading."
        ),
    }
    manifest = output_dir / "manifest.json"
    manifest.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["manifest"] = str(manifest)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diagnostic", type=Path, default=DEFAULT_DIAGNOSTIC)
    parser.add_argument("--archive-dir", type=Path, default=DEFAULT_ARCHIVE_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--start-year", type=int, default=2019)
    parser.add_argument("--end-year", type=int, default=2021)
    parser.add_argument("--custom-registry", type=Path)
    args = parser.parse_args()
    result = run(
        diagnostic_path=args.diagnostic,
        archive_dir=args.archive_dir,
        output_dir=args.output_dir,
        start_year=args.start_year,
        end_year=args.end_year,
        custom_registry_path=args.custom_registry,
    )
    print(json.dumps({
        "manifest": result["manifest"],
        "raw_fact_count": result["raw_fact_count"],
        "strict_quarter_row_count": result["strict_quarter_row_count"],
        "conflict_count": result["conflict_count"],
        "unmapped_candidate_row_count": result["unmapped_candidate_row_count"],
        "promotion_eligible": False,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
