#!/usr/bin/env python3
"""Recover CHKP quarters from SHA-bound SEC-filed GAAP income statements."""

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

from scripts.research_v14_sec_filing_exhibit_financials import _parse_accounting_number
from scripts.research_v14_team_sec_quarterly_filings import _longest_chain


DEFAULT_REGISTRY = Path("stocks_list_dir/nasdaq/chkp_quarterly_reports.csv")
DEFAULT_OUTPUT_DIR = Path("output/research_only/v14/chkp_sec_quarterly_reports_2017_2021")
METRIC_LABELS = {"revenue": "Total revenues", "net_income": "Net income"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normal(value: Any) -> str:
    return " ".join(str(value).replace("\xa0", " ").split()).casefold()


def _statement_table(path: Path) -> tuple[pd.DataFrame, float, str]:
    document = " ".join(
        BeautifulSoup(path.read_bytes(), "html.parser").get_text(" ", strip=True).split()
    )
    unit_match = re.search(
        r"(?:Unaudited,\s*)?in (thousands|millions), except per share amounts",
        document,
        flags=re.IGNORECASE,
    )
    if not unit_match:
        raise ValueError(f"CHKP statement does not prove scaling units: {path}")
    unit_name = unit_match.group(1).casefold()
    multiplier = 1_000.0 if unit_name == "thousands" else 1_000_000.0
    candidates = []
    for table in pd.read_html(path):
        first = table.iloc[:, 0].fillna("").map(_normal)
        if all(first.eq(_normal(label)).any() for label in METRIC_LABELS.values()):
            candidates.append(table)
    if len(candidates) != 1:
        raise ValueError(f"expected one CHKP GAAP income statement in {path}, found {len(candidates)}")
    return candidates[0], multiplier, unit_name


def _columns(table: pd.DataFrame, *, year: int, period_phrase: str) -> list[Any]:
    period_columns = set()
    year_columns = set()
    phrase = period_phrase.casefold()
    for _, row in table.head(6).iterrows():
        for column, value in row.items():
            text = _normal(value)
            if phrase in text:
                period_columns.add(column)
            if text == str(year):
                year_columns.add(column)
    selected = [column for column in table.columns if column in period_columns and column in year_columns]
    if not selected:
        raise ValueError(f"CHKP table has no {period_phrase} {year} columns")
    return selected


def _row_value(table: pd.DataFrame, label: str, columns: list[Any], multiplier: float) -> float:
    first = table.iloc[:, 0].fillna("").map(_normal)
    rows = table.loc[first.eq(_normal(label))]
    if len(rows) != 1:
        raise ValueError(f"expected one CHKP row for {label!r}")
    parsed = [
        value for column in columns
        if (value := _parse_accounting_number(rows.iloc[0][column])) is not None
    ]
    values = sorted(set(parsed))
    if len(values) != 1:
        raise ValueError(f"expected one repeated CHKP value for {label!r}: {values}")
    return round(values[0] * multiplier, 2)


def parse_chkp_quarter(path: Path, *, fiscal_year: int, fiscal_quarter: int) -> dict[str, Any]:
    table, multiplier, unit_name = _statement_table(path)
    current_columns = _columns(table, year=fiscal_year, period_phrase="Three Months Ended")
    prior_columns = _columns(table, year=fiscal_year - 1, period_phrase="Three Months Ended")
    current = {
        metric: _row_value(table, label, current_columns, multiplier)
        for metric, label in METRIC_LABELS.items()
    }
    prior = {
        metric: _row_value(table, label, prior_columns, multiplier)
        for metric, label in METRIC_LABELS.items()
    }
    annual = None
    if fiscal_quarter == 4:
        annual_columns = _columns(table, year=fiscal_year, period_phrase="Year Ended")
        annual = {
            metric: _row_value(table, label, annual_columns, multiplier)
            for metric, label in METRIC_LABELS.items()
        }
    return {
        "current": current,
        "prior_year_comparison": prior,
        "annual": annual,
        "reported_scale": unit_name,
        "multiplier": multiplier,
    }


def _values_agree(left: dict[str, float], right: dict[str, float], *, tolerance: float = 0.01) -> bool:
    return all(math.isclose(left[m], right[m], rel_tol=0, abs_tol=tolerance) for m in METRIC_LABELS)


def run(*, registry_path: Path = DEFAULT_REGISTRY, output_dir: Path = DEFAULT_OUTPUT_DIR) -> dict[str, Any]:
    registry = pd.read_csv(
        registry_path,
        dtype={"ticker": str, "cik": int, "accession": str},
        parse_dates=["fiscal_end", "available_date"],
    )
    if set(registry["ticker"]) != {"CHKP"} or set(registry["cik"]) != {1015922}:
        raise ValueError("CHKP registry contains another issuer")
    expected = {(year, quarter) for year in range(2017, 2022) for quarter in range(1, 5)}
    observed = set(zip(registry["fiscal_year"], registry["fiscal_quarter"]))
    if observed != expected or len(registry) != 20:
        raise ValueError("CHKP registry is not the complete 2017Q1-2021Q4 grid")

    rows = []
    recovered = []
    bindings = []
    parsed_by_slot = {}
    for entry in registry.sort_values(["fiscal_year", "fiscal_quarter"]).itertuples(index=False):
        path = Path(entry.local_path)
        parsed = parse_chkp_quarter(
            path, fiscal_year=int(entry.fiscal_year), fiscal_quarter=int(entry.fiscal_quarter)
        )
        values = parsed["current"]
        if values["revenue"] <= 0:
            raise ValueError(f"CHKP quarterly revenue is not positive: {entry.accession}")
        lag_days = int((entry.available_date - entry.fiscal_end).days)
        if not 0 <= lag_days <= 150:
            raise ValueError(f"CHKP report is not timely: {entry.accession}")
        common = {
            "ticker": "CHKP", "fiscal_end": entry.fiscal_end,
            "available_date": entry.available_date, "taxonomy": "us-gaap",
            "form": entry.form, "accession": entry.accession, "unit": "USD",
            "source": "explicit_sec_filed_chkp_gaap_three_month_statement",
            "source_archive": path.name, "source_archive_sha256": _sha256(path),
            "derivation_prior_accession": "",
        }
        for metric, concept in (("revenue", "Revenues"), ("net_income", "NetIncomeLoss")):
            rows.append({**common, "metric": metric, "value": values[metric], "concept": concept})
        recovered.append({
            "ticker": "CHKP", "fiscal_year": int(entry.fiscal_year),
            "fiscal_quarter": int(entry.fiscal_quarter),
            "fiscal_end": entry.fiscal_end.strftime("%Y-%m-%d"),
            "available_date": entry.available_date.strftime("%Y-%m-%d"),
            "availability_lag_days": lag_days, **values,
            "reported_scale": parsed["reported_scale"],
            "derivation": "direct_three_month_sec_filed_gaap_statement",
            "accession": entry.accession,
        })
        parsed_by_slot[(int(entry.fiscal_year), int(entry.fiscal_quarter))] = parsed
        bindings.append({
            "accession": entry.accession, "fiscal_end": entry.fiscal_end.strftime("%Y-%m-%d"),
            "available_date": entry.available_date.strftime("%Y-%m-%d"),
            "path": str(path), "sha256": _sha256(path), "source_url": entry.source_url,
            "availability_evidence": "sec_filing_date",
        })

    frame = pd.DataFrame(recovered)
    comparator_checks = []
    for (year, quarter), parsed in sorted(parsed_by_slot.items()):
        if year == 2017:
            continue
        original = frame.loc[frame["fiscal_year"].eq(year - 1) & frame["fiscal_quarter"].eq(quarter)].iloc[0]
        original_values = {metric: float(original[metric]) for metric in METRIC_LABELS}
        comparison = parsed["prior_year_comparison"]
        comparison_tolerance = (
            100_000.01 if parsed["reported_scale"] == "millions" else 0.01
        )
        if not _values_agree(
            original_values, comparison, tolerance=comparison_tolerance
        ):
            raise RuntimeError(f"CHKP {year}Q{quarter} prior comparator disagrees with original")
        differences = {
            metric: comparison[metric] - original_values[metric]
            for metric in METRIC_LABELS
        }
        comparator_checks.append({
            "reporting_slot": f"{year}Q{quarter}", "compared_slot": f"{year - 1}Q{quarter}",
            "later_report_comparison": comparison, "contemporaneous_original": original_values,
            "difference": differences,
            "exact_match": all(value == 0 for value in differences.values()),
            "within_later_report_precision": True,
            "absolute_tolerance": comparison_tolerance,
        })

    annual_checks = []
    for year in range(2017, 2022):
        year_rows = frame.loc[frame["fiscal_year"].eq(year)]
        quarter_sum = {metric: float(year_rows[metric].sum()) for metric in METRIC_LABELS}
        annual = parsed_by_slot[(year, 4)]["annual"]
        # A rounded annual and four independently rounded quarters can differ
        # by more than one 0.1m display unit; two units is the strict bound here.
        tolerance = 200_000.01
        if annual is None or not _values_agree(quarter_sum, annual, tolerance=tolerance):
            raise RuntimeError(f"CHKP {year} quarter sum disagrees with Q4 annual columns")
        differences = {metric: quarter_sum[metric] - annual[metric] for metric in METRIC_LABELS}
        annual_checks.append({
            "fiscal_year": year, "quarter_sum": quarter_sum, "q4_reported_annual": annual,
            "difference": differences, "within_reporting_precision": True,
            "absolute_tolerance": tolerance,
        })

    quarters = pd.DataFrame(rows).sort_values(["fiscal_end", "metric"])
    paired = quarters.groupby("fiscal_end")["metric"].nunique()
    longest = _longest_chain(paired.loc[paired.eq(2)].index.tolist())
    if longest != 20:
        raise RuntimeError(f"CHKP timely paired quarterly chain is not continuous: {longest}/20")
    output_dir.mkdir(parents=True, exist_ok=True)
    quarters_path = output_dir / "strict_quarterly_facts.csv"
    quarters.to_csv(quarters_path, index=False)
    report = {
        "schema_version": 1, "research_only": True, "point_in_time_proven": True,
        "promotion_eligible": False, "release_status": "BLOCKED", "ticker": "CHKP",
        "currency": "USD", "quarter_count": 20,
        "longest_continuous_timely_paired_quarters": longest,
        "recovered_quarters": recovered, "prior_year_cross_checks": comparator_checks,
        "annual_cross_checks": annual_checks,
        "registry": {"path": str(registry_path), "sha256": _sha256(registry_path)},
        "reports": bindings,
        "outputs": {"quarters": {"path": str(quarters_path), "sha256": _sha256(quarters_path)}},
        "guardrail": (
            "Only direct three-month GAAP Total revenues and Net income from SEC-filed "
            "CHKP statements are accepted. Per-file thousand/million scaling is proven "
            "from the statement. Non-GAAP measures are excluded."
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
        "manifest": result["manifest"], "quarter_count": result["quarter_count"],
        "longest_continuous_timely_paired_quarters": result["longest_continuous_timely_paired_quarters"],
        "promotion_eligible": False,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
