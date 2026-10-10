#!/usr/bin/env python3
"""Recover ASML quarters from SHA-bound official US GAAP result PDFs."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

import pandas as pd
import pdfplumber

from scripts.research_v14_team_sec_quarterly_filings import _longest_chain


DEFAULT_REGISTRY = Path("stocks_list_dir/nasdaq/asml_quarterly_reports.csv")
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/asml_official_quarterly_reports_2018_2021"
)
METRIC_LABELS = {"revenue": "Total net sales", "net_income": "Net income"}
NUMBER_RE = re.compile(r"\(?-?\d[\d,]*\.\d+\)?")
DATE_LABEL_RE = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|"
    r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2}\b",
    flags=re.IGNORECASE,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _accounting_number(value: str) -> float:
    normalized = value.replace(",", "").strip()
    negative = normalized.startswith("(") and normalized.endswith(")")
    normalized = normalized.strip("()")
    parsed = float(normalized)
    return -parsed if negative else parsed


def _metric_values(text: str, label: str) -> list[float]:
    matches = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith(f"{label} ")
    ]
    # The ratios section also starts with "Net income".  Statement rows have
    # plain decimal amounts rather than percentage signs.
    matches = [line for line in matches if "%" not in line]
    if len(matches) != 1:
        raise ValueError(f"expected one ASML statement row for {label!r}")
    values = [_accounting_number(value) for value in NUMBER_RE.findall(matches[0])]
    if len(values) not in (2, 4):
        raise ValueError(f"unexpected ASML value count for {label!r}: {values}")
    return values


def parse_statement_text(
    text: str,
    *,
    fiscal_end: pd.Timestamp,
    fiscal_quarter: int,
) -> dict[str, Any]:
    header = " ".join(text.splitlines()[:7])
    if "ASML - Summary US GAAP Consolidated Statements of Operations" not in header:
        raise ValueError("not an ASML US GAAP statement")
    if not re.search(r"in millions (?:EUR|€)", text, flags=re.IGNORECASE):
        raise ValueError("ASML statement does not prove EUR millions")
    date_labels = {
        f"{fiscal_end.strftime('%b')} {fiscal_end.day}",
        f"{fiscal_end.strftime('%B')} {fiscal_end.day}",
    }
    if not any(label.casefold() in header.casefold() for label in date_labels):
        raise ValueError(
            f"ASML statement header does not contain {fiscal_end.date()}"
        )
    if str(fiscal_end.year) not in header:
        raise ValueError("ASML statement header does not contain fiscal year")
    header_dates = DATE_LABEL_RE.findall(header)
    if len(header_dates) < 2:
        raise ValueError("ASML statement header lacks comparison period dates")
    prior_year_fiscal_end = pd.Timestamp(
        pd.to_datetime(f"{header_dates[0]} {fiscal_end.year - 1}")
    )

    parsed = {
        metric: _metric_values(text, label)
        for metric, label in METRIC_LABELS.items()
    }
    counts = {len(values) for values in parsed.values()}
    expected_count = 2 if fiscal_quarter == 1 else 4
    if counts != {expected_count}:
        raise ValueError(
            f"ASML Q{fiscal_quarter} statement columns differ from expected shape"
        )
    current = {
        metric: round(values[1] * 1_000_000.0, 2)
        for metric, values in parsed.items()
    }
    comparison = {
        metric: round(values[0] * 1_000_000.0, 2)
        for metric, values in parsed.items()
    }
    annual = None
    if fiscal_quarter == 4:
        if "Twelve months ended" not in header:
            raise ValueError("ASML Q4 statement lacks twelve-month columns")
        annual = {
            metric: round(values[-1] * 1_000_000.0, 2)
            for metric, values in parsed.items()
        }
    return {
        "current": current,
        "prior_year_comparison": comparison,
        "prior_year_fiscal_end": prior_year_fiscal_end,
        "annual": annual,
    }


def parse_asml_quarter(
    path: Path,
    *,
    fiscal_end: pd.Timestamp,
    fiscal_quarter: int,
) -> dict[str, Any]:
    with pdfplumber.open(path) as pdf:
        if not pdf.pages:
            raise ValueError(f"ASML PDF has no pages: {path}")
        text = pdf.pages[0].extract_text(x_tolerance=2, y_tolerance=2) or ""
    return parse_statement_text(
        text, fiscal_end=fiscal_end, fiscal_quarter=fiscal_quarter
    )


def _values_agree(
    left: dict[str, float],
    right: dict[str, float],
    *,
    absolute_tolerance: float = 0.01,
) -> bool:
    return all(
        math.isclose(
            left[metric], right[metric], rel_tol=0, abs_tol=absolute_tolerance
        )
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
    if set(registry["ticker"]) != {"ASML"} or set(registry["cik"]) != {937966}:
        raise ValueError("ASML registry contains another issuer")
    if len(registry) != 16:
        raise ValueError("ASML registry must contain exactly 2018Q1-2021Q4")
    if registry.duplicated(["fiscal_year", "fiscal_quarter"]).any():
        raise ValueError("ASML registry contains duplicate fiscal quarters")
    expected_slots = {(year, quarter) for year in range(2018, 2022) for quarter in range(1, 5)}
    observed_slots = set(
        zip(registry["fiscal_year"].astype(int), registry["fiscal_quarter"].astype(int))
    )
    if observed_slots != expected_slots:
        raise ValueError("ASML registry is not the complete 2018Q1-2021Q4 grid")

    rows: list[dict[str, Any]] = []
    recovered: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    parsed_by_slot: dict[tuple[int, int], dict[str, Any]] = {}
    for entry in registry.sort_values(["fiscal_year", "fiscal_quarter"]).itertuples(index=False):
        path = Path(entry.local_path)
        parsed = parse_asml_quarter(
            path,
            fiscal_end=pd.Timestamp(entry.fiscal_end),
            fiscal_quarter=int(entry.fiscal_quarter),
        )
        values = parsed["current"]
        if values["revenue"] <= 0:
            raise ValueError(f"ASML quarterly revenue is not positive: {entry.accession}")
        lag_days = int((entry.available_date - entry.fiscal_end).days)
        if not 0 <= lag_days <= 150:
            raise ValueError(f"ASML report is not timely: {entry.accession}")
        common = {
            "ticker": "ASML",
            "fiscal_end": entry.fiscal_end,
            "available_date": entry.available_date,
            "taxonomy": "us-gaap",
            "form": entry.form,
            "accession": entry.accession,
            "unit": "EUR",
            "source": "official_asml_quarterly_us_gaap_statement",
            "source_archive": path.name,
            "source_archive_sha256": _sha256(path),
            "derivation_prior_accession": "",
        }
        for metric, concept in (
            ("revenue", "SalesRevenueNet"),
            ("net_income", "NetIncomeLoss"),
        ):
            rows.append({**common, "metric": metric, "value": values[metric], "concept": concept})
        recovered.append({
            "ticker": "ASML",
            "fiscal_year": int(entry.fiscal_year),
            "fiscal_quarter": int(entry.fiscal_quarter),
            "fiscal_end": entry.fiscal_end.strftime("%Y-%m-%d"),
            "available_date": entry.available_date.strftime("%Y-%m-%d"),
            "availability_lag_days": lag_days,
            **values,
            "derivation": "direct_current_quarter_official_statement",
            "accession": entry.accession,
        })
        parsed_by_slot[(int(entry.fiscal_year), int(entry.fiscal_quarter))] = parsed
        if int(entry.fiscal_year) == 2018:
            comparison_accession = f"{entry.accession}-COMPARATIVE-2017Q{entry.fiscal_quarter}"
            comparison_end = parsed["prior_year_fiscal_end"]
            comparison = parsed["prior_year_comparison"]
            comparison_common = {
                **common,
                "fiscal_end": comparison_end,
                "accession": comparison_accession,
                "source": "prior_year_comparison_in_official_asml_statement",
            }
            for metric, concept in (
                ("revenue", "SalesRevenueNet"),
                ("net_income", "NetIncomeLoss"),
            ):
                rows.append({
                    **comparison_common,
                    "metric": metric,
                    "value": comparison[metric],
                    "concept": concept,
                })
            recovered.append({
                "ticker": "ASML",
                "fiscal_year": 2017,
                "fiscal_quarter": int(entry.fiscal_quarter),
                "fiscal_end": comparison_end.strftime("%Y-%m-%d"),
                "available_date": entry.available_date.strftime("%Y-%m-%d"),
                "availability_lag_days": int(
                    (entry.available_date - comparison_end).days
                ),
                **comparison,
                "derivation": "prior_year_comparison_in_2018_official_statement",
                "accession": comparison_accession,
            })
        bindings.append({
            "accession": entry.accession,
            "fiscal_end": entry.fiscal_end.strftime("%Y-%m-%d"),
            "available_date": entry.available_date.strftime("%Y-%m-%d"),
            "path": str(path),
            "sha256": _sha256(path),
            "source_url": entry.source_url,
            "source_page_url": entry.source_page_url,
            "availability_evidence": "official_result_page_publication_date",
        })

    quarter_frame = pd.DataFrame(recovered)
    prior_year_cross_checks = []
    for (year, quarter), parsed in sorted(parsed_by_slot.items()):
        prior = quarter_frame.loc[
            quarter_frame["fiscal_year"].eq(year - 1)
            & quarter_frame["fiscal_quarter"].eq(quarter)
        ]
        if prior.empty:
            continue
        expected = {metric: float(prior.iloc[0][metric]) for metric in METRIC_LABELS}
        observed = parsed["prior_year_comparison"]
        if not _values_agree(expected, observed):
            raise RuntimeError(
                f"ASML {year}Q{quarter} prior-year comparator disagrees: "
                f"{observed} != {expected}"
            )
        prior_year_cross_checks.append({
            "reporting_slot": f"{year}Q{quarter}",
            "compared_slot": f"{year - 1}Q{quarter}",
            "reported_comparison": observed,
            "original_quarter": expected,
            "exact_match": True,
        })

    annual_cross_checks = []
    for year in range(2018, 2022):
        year_rows = quarter_frame.loc[quarter_frame["fiscal_year"].eq(year)]
        observed = {metric: float(year_rows[metric].sum()) for metric in METRIC_LABELS}
        annual = parsed_by_slot[(year, 4)]["annual"]
        # Both quarterly and annual columns are stated to EUR 0.1m, so adding
        # four rounded quarters can differ from the rounded annual by EUR 0.1m.
        reporting_precision_tolerance = 100_000.01
        if annual is None or not _values_agree(
            observed,
            annual,
            absolute_tolerance=reporting_precision_tolerance,
        ):
            raise RuntimeError(f"ASML {year} quarter sum disagrees with Q4 annual columns")
        differences = {
            metric: observed[metric] - annual[metric] for metric in METRIC_LABELS
        }
        annual_cross_checks.append({
            "fiscal_year": year,
            "quarter_sum": observed,
            "q4_reported_annual": annual,
            "difference": differences,
            "exact_match": all(value == 0 for value in differences.values()),
            "within_stated_rounding_precision": True,
            "absolute_tolerance": reporting_precision_tolerance,
        })

    quarters = pd.DataFrame(rows).sort_values(["fiscal_end", "metric"])
    paired = quarters.groupby("fiscal_end")["metric"].nunique()
    paired_ends = paired.loc[paired.eq(2)].index.tolist()
    longest = _longest_chain(paired_ends)
    if longest != 20:
        raise RuntimeError(f"ASML quarterly chain is not continuous: {longest}/20")
    direct_ends = quarter_frame.loc[
        quarter_frame["fiscal_year"].between(2018, 2021), "fiscal_end"
    ].tolist()
    direct_timely_longest = _longest_chain(
        [pd.Timestamp(value) for value in direct_ends]
    )
    if direct_timely_longest != 16:
        raise RuntimeError("ASML direct timely quarterly chain is not 16 quarters")

    output_dir.mkdir(parents=True, exist_ok=True)
    quarters_path = output_dir / "strict_quarterly_facts.csv"
    quarters.to_csv(quarters_path, index=False)
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "promotion_eligible": False,
        "release_status": "BLOCKED",
        "ticker": "ASML",
        "currency": "EUR",
        "quarter_count": 20,
        "direct_current_quarter_count": 16,
        "prior_year_comparison_quarter_count": 4,
        "longest_continuous_paired_quarters": longest,
        "longest_continuous_timely_paired_quarters": direct_timely_longest,
        "longest_growth_usable_paired_quarters": longest,
        "recovered_quarters": recovered,
        "prior_year_cross_checks": prior_year_cross_checks,
        "annual_cross_checks": annual_cross_checks,
        "registry": {"path": str(registry_path), "sha256": _sha256(registry_path)},
        "reports": bindings,
        "outputs": {"quarters": {"path": str(quarters_path), "sha256": _sha256(quarters_path)}},
        "guardrail": (
            "Only values in each contemporaneous official ASML US GAAP quarterly "
            "statement are accepted. The four 2017 comparison quarters become "
            "available only on their respective 2018 report dates; no earlier "
            "availability is inferred. Later comparatives are cross-checks only. "
            "This artifact is research-only and does not authorize trading."
        ),
    }
    manifest = output_dir / "manifest.json"
    manifest.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["manifest"] = str(manifest)
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
