#!/usr/bin/env python3
"""Recover SNY quarters from SHA-bound SEC-filed Sanofi IFRS result releases."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

import pandas as pd
from bs4 import BeautifulSoup

from scripts.research_v14_sec_filing_exhibit_financials import (
    _parse_accounting_number,
)
from scripts.research_v14_team_sec_quarterly_filings import _longest_chain


DEFAULT_REGISTRY = Path("stocks_list_dir/nasdaq/sny_quarterly_reports.csv")
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/sny_sec_quarterly_reports_2017_2021"
)
METRIC_LABELS = {
    "revenue": "Net sales",
    "net_income": "Net income attributable to equity holders of Sanofi",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normal(value: Any) -> str:
    return " ".join(str(value).replace("\xa0", " ").strip().split()).casefold()


def _statement_table(path: Path) -> pd.DataFrame:
    document = " ".join(
        BeautifulSoup(path.read_bytes(), "html.parser")
        .get_text(" ", strip=True)
        .split()
    )
    if not re.search(r"€\s*million", document, flags=re.IGNORECASE):
        raise ValueError(f"SNY filing does not prove EUR millions: {path}")
    candidates = []
    for table in pd.read_html(path):
        first = table.iloc[:, 0].fillna("").map(_normal)
        if all(first.eq(_normal(label)).any() for label in METRIC_LABELS.values()):
            candidates.append(table)
    if len(candidates) != 1:
        raise ValueError(
            f"expected one SNY consolidated income statement in {path}, "
            f"found {len(candidates)}"
        )
    return candidates[0]


def _slot_columns(table: pd.DataFrame, slot: str) -> list[Any]:
    normalized_slot = _normal(slot)
    selected = []
    for column in table.columns:
        headers = [_normal(value) for value in table[column].head(6)]
        if any(value.startswith(normalized_slot) for value in headers):
            selected.append(column)
    if not selected:
        raise ValueError(f"SNY statement has no columns for {slot}")
    return selected


def _annual_columns(table: pd.DataFrame, year: int) -> list[Any]:
    candidates = (f"12m {year}", f"fy {year}", str(year), f"{year}.0")
    selected = []
    for column in table.columns:
        headers = [_normal(value) for value in table[column].head(6)]
        if any(
            value.startswith(candidate) for value in headers
            for candidate in candidates
        ):
            selected.append(column)
    if not selected:
        raise ValueError(f"SNY Q4 statement has no annual columns for {year}")
    return selected


def _row_value(table: pd.DataFrame, label: str, columns: list[Any]) -> float:
    first = table.iloc[:, 0].fillna("").map(_normal)
    rows = table.loc[first.eq(_normal(label))]
    if len(rows) != 1:
        raise ValueError(f"expected one SNY row for {label!r}")
    parsed = [
        value
        for column in columns
        if (value := _parse_accounting_number(rows.iloc[0][column])) is not None
    ]
    values = sorted(set(parsed))
    if len(values) != 1:
        raise ValueError(f"expected one repeated SNY value for {label!r}: {values}")
    return values[0] * 1_000_000.0


def parse_sny_quarter(
    path: Path,
    *,
    fiscal_year: int,
    fiscal_quarter: int,
) -> dict[str, Any]:
    table = _statement_table(path)
    current_columns = _slot_columns(table, f"Q{fiscal_quarter} {fiscal_year}")
    prior_columns = _slot_columns(table, f"Q{fiscal_quarter} {fiscal_year - 1}")
    current = {
        metric: _row_value(table, label, current_columns)
        for metric, label in METRIC_LABELS.items()
    }
    prior = {
        metric: _row_value(table, label, prior_columns)
        for metric, label in METRIC_LABELS.items()
    }
    annual = None
    if fiscal_quarter == 4:
        annual_columns = _annual_columns(table, fiscal_year)
        annual = {
            metric: _row_value(table, label, annual_columns)
            for metric, label in METRIC_LABELS.items()
        }
    return {"current": current, "prior_year_comparison": prior, "annual": annual}


def _values_agree(
    left: dict[str, float],
    right: dict[str, float],
    *,
    tolerance: float = 0.01,
) -> bool:
    return all(
        math.isclose(left[metric], right[metric], rel_tol=0, abs_tol=tolerance)
        for metric in METRIC_LABELS
    )


def run(
    *,
    registry_path: Path = DEFAULT_REGISTRY,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> dict[str, Any]:
    registry = pd.read_csv(
        registry_path,
        dtype={"ticker": str, "cik": int, "accession": str},
        parse_dates=["fiscal_end", "available_date"],
    )
    if set(registry["ticker"]) != {"SNY"} or set(registry["cik"]) != {1121404}:
        raise ValueError("SNY registry contains another issuer")
    expected = {(year, quarter) for year in range(2017, 2022) for quarter in range(1, 5)}
    observed = set(zip(registry["fiscal_year"], registry["fiscal_quarter"]))
    if observed != expected or len(registry) != 20:
        raise ValueError("SNY registry is not the complete 2017Q1-2021Q4 grid")
    if registry.duplicated(["fiscal_year", "fiscal_quarter"]).any():
        raise ValueError("SNY registry contains duplicate fiscal quarters")

    rows = []
    recovered = []
    bindings = []
    parsed_by_slot = {}
    for entry in registry.sort_values(["fiscal_year", "fiscal_quarter"]).itertuples(index=False):
        path = Path(entry.local_path)
        parsed = parse_sny_quarter(
            path,
            fiscal_year=int(entry.fiscal_year),
            fiscal_quarter=int(entry.fiscal_quarter),
        )
        values = parsed["current"]
        if values["revenue"] <= 0:
            raise ValueError(f"SNY quarterly revenue is not positive: {entry.accession}")
        lag_days = int((entry.available_date - entry.fiscal_end).days)
        if not 0 <= lag_days <= 150:
            raise ValueError(f"SNY report is not timely: {entry.accession}")
        common = {
            "ticker": "SNY",
            "fiscal_end": entry.fiscal_end,
            "available_date": entry.available_date,
            "taxonomy": "ifrs-full",
            "form": entry.form,
            "accession": entry.accession,
            "unit": "EUR",
            "source": "explicit_sec_filed_sanofi_ifrs_quarterly_statement",
            "source_archive": path.name,
            "source_archive_sha256": _sha256(path),
            "derivation_prior_accession": "",
        }
        for metric, concept in (
            ("revenue", "Revenue"),
            ("net_income", "ProfitLossAttributableToOwnersOfParent"),
        ):
            rows.append({**common, "metric": metric, "value": values[metric], "concept": concept})
        recovered.append({
            "ticker": "SNY",
            "fiscal_year": int(entry.fiscal_year),
            "fiscal_quarter": int(entry.fiscal_quarter),
            "fiscal_end": entry.fiscal_end.strftime("%Y-%m-%d"),
            "available_date": entry.available_date.strftime("%Y-%m-%d"),
            "availability_lag_days": lag_days,
            **values,
            "derivation": "direct_quarter_in_sec_filed_ifrs_statement",
            "accession": entry.accession,
        })
        parsed_by_slot[(int(entry.fiscal_year), int(entry.fiscal_quarter))] = parsed
        bindings.append({
            "accession": entry.accession,
            "fiscal_end": entry.fiscal_end.strftime("%Y-%m-%d"),
            "available_date": entry.available_date.strftime("%Y-%m-%d"),
            "path": str(path),
            "sha256": _sha256(path),
            "source_url": entry.source_url,
            "availability_evidence": "sec_filing_date",
        })

    recovered_frame = pd.DataFrame(recovered)
    prior_year_cross_checks = []
    for (year, quarter), parsed in sorted(parsed_by_slot.items()):
        if year == 2017:
            continue
        original = recovered_frame.loc[
            recovered_frame["fiscal_year"].eq(year - 1)
            & recovered_frame["fiscal_quarter"].eq(quarter)
        ].iloc[0]
        original_values = {metric: float(original[metric]) for metric in METRIC_LABELS}
        comparison = parsed["prior_year_comparison"]
        differences = {
            metric: comparison[metric] - original_values[metric]
            for metric in METRIC_LABELS
        }
        prior_year_cross_checks.append({
            "reporting_slot": f"{year}Q{quarter}",
            "compared_slot": f"{year - 1}Q{quarter}",
            "later_report_comparison": comparison,
            "contemporaneous_original": original_values,
            "difference": differences,
            "exact_match": _values_agree(comparison, original_values),
            "later_comparison_used_to_replace_original": False,
        })

    annual_cross_checks = []
    for year in range(2017, 2022):
        year_rows = recovered_frame.loc[recovered_frame["fiscal_year"].eq(year)]
        quarter_sum = {metric: float(year_rows[metric].sum()) for metric in METRIC_LABELS}
        annual = parsed_by_slot[(year, 4)]["annual"]
        tolerance = 1_000_000.01
        if annual is None or not _values_agree(quarter_sum, annual, tolerance=tolerance):
            raise RuntimeError(f"SNY {year} quarter sum disagrees with Q4 annual columns")
        differences = {metric: quarter_sum[metric] - annual[metric] for metric in METRIC_LABELS}
        annual_cross_checks.append({
            "fiscal_year": year,
            "quarter_sum": quarter_sum,
            "q4_reported_annual": annual,
            "difference": differences,
            "exact_match": all(value == 0 for value in differences.values()),
            "within_eur_million_reporting_precision": True,
            "absolute_tolerance": tolerance,
        })

    quarters = pd.DataFrame(rows).sort_values(["fiscal_end", "metric"])
    paired = quarters.groupby("fiscal_end")["metric"].nunique()
    paired_ends = paired.loc[paired.eq(2)].index.tolist()
    longest = _longest_chain(paired_ends)
    if longest != 20:
        raise RuntimeError(f"SNY timely paired quarterly chain is not continuous: {longest}/20")

    output_dir.mkdir(parents=True, exist_ok=True)
    quarters_path = output_dir / "strict_quarterly_facts.csv"
    quarters.to_csv(quarters_path, index=False)
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "promotion_eligible": False,
        "release_status": "BLOCKED",
        "ticker": "SNY",
        "issuer": "Sanofi",
        "currency": "EUR",
        "quarter_count": 20,
        "longest_continuous_timely_paired_quarters": longest,
        "recovered_quarters": recovered,
        "prior_year_cross_checks": prior_year_cross_checks,
        "annual_cross_checks": annual_cross_checks,
        "registry": {"path": str(registry_path), "sha256": _sha256(registry_path)},
        "reports": bindings,
        "outputs": {"quarters": {"path": str(quarters_path), "sha256": _sha256(quarters_path)}},
        "guardrail": (
            "Only direct IFRS net sales and net income attributable to equity "
            "holders of Sanofi from SEC-filed contemporaneous releases are used. "
            "Adjusted business net income is excluded. Later comparison columns "
            "are diagnostic only and never replace contemporaneous values."
        ),
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["manifest"] = str(manifest_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = run(registry_path=args.registry, output_dir=args.output_dir)
    print(json.dumps({
        "manifest": result["manifest"],
        "quarter_count": result["quarter_count"],
        "longest_continuous_timely_paired_quarters": result[
            "longest_continuous_timely_paired_quarters"
        ],
        "promotion_eligible": False,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
