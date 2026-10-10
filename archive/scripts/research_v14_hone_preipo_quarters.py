#!/usr/bin/env python3
"""Recover HONE's 2017-2018 direct quarters from its original S-1."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd
from bs4 import BeautifulSoup


REGISTRY = Path("stocks_list_dir/nasdaq/hone_preipo_quarterly_reports.csv")
OUTPUT_DIR = Path("output/research_only/v14/hone_preipo_quarters_2017q1_2018q4")
HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
QUARTERS = (
    ("First Quarter", 1, "03-31"),
    ("Second Quarter", 2, "06-30"),
    ("Third Quarter", 3, "09-30"),
    ("Fourth Quarter", 4, "12-31"),
)
ANNUAL_EXPECTED = {
    2017: {"revenue": 128_882_000.0, "net_income": 10_379_000.0},
    2018: {"revenue": 138_128_000.0, "net_income": 11_394_000.0},
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normal(value: object) -> str:
    return " ".join(str(value).replace("\xa0", " ").split()).casefold()


def _accounting_value(value: object) -> float:
    text = str(value).strip().replace(",", "").replace("$", "")
    if text in {"—", "-", "–"}:
        return 0.0
    match = re.search(r"\d+(?:\.\d+)?", text)
    if match is None:
        raise ValueError(f"no accounting value in {value!r}")
    result = float(match.group())
    return -result if "(" in text else result


def _quarterly_table(path: Path) -> pd.DataFrame:
    candidates = []
    expected_headers = {label.casefold() for label, _, _ in QUARTERS}
    for table in pd.read_html(path):
        if len(table) < 17:
            continue
        headers = {_normal(value) for value in table.head(4).to_numpy().ravel()}
        labels = table.iloc[:, 0].map(_normal)
        if not expected_headers.issubset(headers):
            continue
        if labels.eq("net interest and dividend income").sum() != 1:
            continue
        if labels.eq("total noninterest income").sum() != 1:
            continue
        if labels.eq("net income (loss)").sum() != 1:
            continue
        candidates.append(table)
    if len(candidates) != 1:
        raise ValueError(
            f"expected one HONE quarterly-results table, found {len(candidates)}"
        )
    return candidates[0]


def _value_column(
    table: pd.DataFrame,
    *,
    quarter_label: str,
    year: int,
    value_row: pd.Series,
) -> int:
    quarter_headers = table.iloc[1].map(_normal)
    year_headers = table.iloc[2].map(_normal)
    candidates = [
        int(column)
        for column in table.columns
        if quarter_headers[column] == quarter_label.casefold()
        and year_headers[column].startswith(str(year))
    ]
    values = []
    for column in candidates:
        try:
            values.append((column, _accounting_value(value_row[column])))
        except ValueError:
            continue
    if len(values) != 1:
        raise ValueError(
            f"expected one HONE value column for {year} {quarter_label}, "
            f"found {values}"
        )
    return values[0][0]


def extract_quarters(path: Path) -> list[dict]:
    """Extract the eight explicitly labelled quarterly bank results."""
    text = " ".join(
        BeautifulSoup(path.read_bytes(), "html.parser")
        .get_text(" ", strip=True)
        .split()
    )
    required = (
        "HarborOne Bancorp, Inc.",
        "SELECTED QUARTERLY FINANCIAL DATA (UNAUDITED)",
        "in thousands, except share data",
    )
    if not all(phrase.casefold() in text.casefold() for phrase in required):
        raise ValueError("HONE S-1 does not prove issuer and quarterly basis")
    table = _quarterly_table(path)
    labels = table.iloc[:, 0].map(_normal)
    net_interest = table.loc[
        labels.eq("net interest and dividend income")
    ].iloc[0]
    noninterest = table.loc[labels.eq("total noninterest income")].iloc[0]
    net_income = table.loc[labels.eq("net income (loss)")].iloc[0]
    recovered = []
    for year in (2017, 2018):
        for quarter_label, quarter, month_day in QUARTERS:
            column = _value_column(
                table,
                quarter_label=quarter_label,
                year=year,
                value_row=net_interest,
            )
            recovered.append({
                "ticker": "HONE",
                "fiscal_end": f"{year}-{month_day}",
                "revenue": (
                    _accounting_value(net_interest[column])
                    + _accounting_value(noninterest[column])
                ) * 1_000.0,
                "net_income": _accounting_value(net_income[column]) * 1_000.0,
                "fiscal_quarter": quarter,
            })
    return recovered


def _annual_identity_checks(quarters: list[dict]) -> list[dict]:
    checks = []
    for year, expected in ANNUAL_EXPECTED.items():
        rows = [
            row for row in quarters
            if pd.Timestamp(row["fiscal_end"]).year == year
        ]
        if len(rows) != 4:
            raise RuntimeError(f"HONE {year} does not contain four quarters")
        sums = {metric: sum(row[metric] for row in rows) for metric in expected}
        if sums != expected:
            raise RuntimeError(
                f"HONE {year} quarterly values do not close to annual values: "
                f"{sums} != {expected}"
            )
        checks.append({
            "year": year,
            "quarter_sum": sums,
            "audited_annual": expected,
            "difference": {metric: 0.0 for metric in expected},
        })
    return checks


def run(
    registry_path: Path = REGISTRY,
    output_dir: Path = OUTPUT_DIR,
) -> dict:
    registry = pd.read_csv(registry_path, dtype={"cik": str, "accession": str})
    if (
        len(registry) != 1
        or set(registry["ticker"]) != {"HONE"}
        or set(registry["cik"]) != {"1769617"}
    ):
        raise ValueError("HONE registry must bind exactly one issuer S-1")
    row = next(registry.itertuples(index=False))
    if row.form != "S-1" or row.accession.replace("-", "") not in row.source_url:
        raise ValueError("HONE registry does not bind the original S-1 accession")
    source_path = Path(row.local_path)
    source_path.parent.mkdir(parents=True, exist_ok=True)
    if not source_path.exists():
        with urlopen(
            Request(row.source_url, headers=HEADERS), timeout=120
        ) as response:
            source_path.write_bytes(response.read())

    quarters = extract_quarters(source_path)
    annual_checks = _annual_identity_checks(quarters)
    available = pd.Timestamp(row.available_date).date().isoformat()
    facts = []
    for quarter in quarters:
        for metric in ("revenue", "net_income"):
            facts.append({
                "ticker": "HONE",
                "fiscal_end": quarter["fiscal_end"],
                "available_date": available,
                "metric": metric,
                "value": quarter[metric],
                "unit": "USD",
                "taxonomy": "HONE_US_GAAP_S1",
                "concept": f"sec_s1_quarterly_bank_{metric}",
                "form": "S-1",
                "accession": row.accession,
                "source_url": row.source_url,
            })
    frame = pd.DataFrame(facts).sort_values(["fiscal_end", "metric"])
    if len(frame) != 16 or frame[["ticker", "fiscal_end", "metric"]].duplicated().any():
        raise RuntimeError("HONE recovery must contain exactly eight paired quarters")

    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    frame.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1,
        "research_only": True,
        "ticker": "HONE",
        "point_in_time_proven": True,
        "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "accepted_accounting_basis": "US_GAAP_BANK_REVENUE",
        "issuer_boundary": "HARBORONE_BANCORP_INC",
        "accepted_quarter_count": 8,
        "fact_count": 16,
        "recovered_quarters": quarters,
        "annual_identity_checks": annual_checks,
        "filing_sources": [{**row._asdict(), "sha256": _sha256(source_path)}],
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path),
                "sha256": _sha256(facts_path),
            }
        },
        "guardrail": (
            "Only the original S-1's explicitly labelled 2017Q1-2018Q4 "
            "unaudited single-quarter results are accepted, all with the "
            "S-1 filing date as availability. Bank revenue is the disclosed "
            "net interest and dividend income plus total noninterest income; "
            "both years close exactly to the annual statements."
        ),
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    report["manifest"] = str(manifest_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry-path", type=Path, default=REGISTRY)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    print(json.dumps(
        run(args.registry_path, args.output_dir), indent=2, sort_keys=True
    ))


if __name__ == "__main__":
    main()
