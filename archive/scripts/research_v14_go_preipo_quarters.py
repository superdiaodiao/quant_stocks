#!/usr/bin/env python3
"""Recover GO's 2017-2018 pre-IPO direct quarters from its original S-1."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd
from bs4 import BeautifulSoup


REGISTRY = Path("stocks_list_dir/nasdaq/go_preipo_quarterly_reports.csv")
OUTPUT_DIR = Path("output/research_only/v14/go_preipo_quarters_2017q1_2018q4")
HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
QUARTERS = (
    ("First Quarter 2017", "2017-04-01"),
    ("Second Quarter 2017", "2017-07-01"),
    ("Third Quarter 2017", "2017-09-30"),
    ("Fourth Quarter 2017", "2017-12-30"),
    ("First Quarter 2018", "2018-03-31"),
    ("Second Quarter 2018", "2018-06-30"),
    ("Third Quarter 2018", "2018-09-29"),
    ("Fourth Quarter 2018", "2018-12-29"),
)
ANNUAL_EXPECTED = {
    2017: {"revenue": 2_075_465_000.0, "net_income": 20_601_000.0},
    2018: {"revenue": 2_287_660_000.0, "net_income": 15_868_000.0},
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normal(value: object) -> str:
    return " ".join(str(value).replace("\xa0", " ").split()).casefold()


def _accounting_value(value: object, trailing: object = None) -> float:
    text = str(value).strip().replace(",", "").replace("$", "")
    if trailing is not None and str(trailing).strip() == ")":
        text += ")"
    match = re.search(r"\d+(?:\.\d+)?", text)
    if match is None:
        raise ValueError(f"no accounting value in {value!r}")
    result = float(match.group())
    return -result if "(" in text else result


def _document_text(path: Path) -> str:
    return " ".join(
        BeautifulSoup(path.read_bytes(), "html.parser").get_text(" ", strip=True).split()
    )


def _quarterly_table(path: Path) -> pd.DataFrame:
    candidates = []
    expected_headers = {label.casefold() for label, _ in QUARTERS}
    for table in pd.read_html(path):
        labels = table.iloc[:, 0].map(_normal)
        headers = {_normal(value) for value in table.head(4).to_numpy().ravel()}
        if not expected_headers.issubset(headers):
            continue
        if labels.eq("net sales").sum() != 1:
            continue
        if labels.str.startswith("net income (loss)").sum() != 1:
            continue
        candidates.append(table)
    if len(candidates) != 1:
        raise ValueError(f"expected one GO quarterly-results table, found {len(candidates)}")
    return candidates[0]


def extract_quarters(path: Path) -> list[dict]:
    """Extract only the eight explicitly labelled unaudited single quarters."""
    text = _document_text(path)
    required = (
        "Grocery Outlet Holding Corp.",
        "Quarterly Results of Operations",
        "prepared on the same basis as the consolidated financial statements",
    )
    if not all(phrase.casefold() in text.casefold() for phrase in required):
        raise ValueError("GO S-1 does not prove issuer and quarterly accounting basis")
    table = _quarterly_table(path)
    header_row = table.iloc[1].map(_normal)
    sales_row = table.iloc[:, 0].map(_normal).eq("net sales")
    income_row = table.iloc[:, 0].map(_normal).str.startswith("net income (loss)")
    recovered = []
    for label, fiscal_end in QUARTERS:
        columns = [column for column in table.columns if header_row[column] == label.casefold()]
        value_columns = [
            column for column in columns
            if pd.notna(table.loc[sales_row, column].iloc[0])
            and str(table.loc[sales_row, column].iloc[0]).strip() != "$"
        ]
        if len(value_columns) != 1:
            raise ValueError(f"expected one value column for {label}, found {value_columns}")
        column = value_columns[0]
        trailing = table.loc[income_row, column + 1].iloc[0] if column + 1 in table.columns else None
        recovered.append({
            "ticker": "GO",
            "fiscal_end": fiscal_end,
            "revenue": _accounting_value(table.loc[sales_row, column].iloc[0]) * 1_000.0,
            "net_income": _accounting_value(
                table.loc[income_row, column].iloc[0], trailing
            ) * 1_000.0,
        })
    return recovered


def _annual_identity_checks(quarters: list[dict]) -> list[dict]:
    checks = []
    for year, expected in ANNUAL_EXPECTED.items():
        rows = [row for row in quarters if pd.Timestamp(row["fiscal_end"]).year == year]
        if len(rows) != 4:
            raise RuntimeError(f"GO {year} does not contain four quarters")
        sums = {metric: sum(row[metric] for row in rows) for metric in expected}
        differences = {metric: sums[metric] - expected[metric] for metric in expected}
        # The S-1's four displayed 2017 net-income quarters sum to $20.602m,
        # while its audited annual statement displays $20.601m. Preserve the
        # direct quarters and explicitly bind the disclosed $1k rounding difference.
        if differences["revenue"] != 0 or abs(differences["net_income"]) > 1_000:
            raise RuntimeError(f"GO {year} quarterly values do not close to annual values")
        checks.append({
            "year": year,
            "quarter_sum": sums,
            "audited_annual": expected,
            "difference": differences,
            "maximum_accepted_rounding_difference": 1_000.0,
        })
    return checks


def run(
    registry_path: Path = REGISTRY,
    output_dir: Path = OUTPUT_DIR,
) -> dict:
    registry = pd.read_csv(registry_path, dtype={"cik": str, "accession": str})
    if len(registry) != 1 or set(registry["ticker"]) != {"GO"} or set(registry["cik"]) != {"1771515"}:
        raise ValueError("GO registry must bind exactly one issuer S-1")
    row = next(registry.itertuples(index=False))
    if row.form != "S-1" or row.accession.replace("-", "") not in row.source_url:
        raise ValueError("GO registry does not bind the original S-1 accession")
    source_path = Path(row.local_path)
    source_path.parent.mkdir(parents=True, exist_ok=True)
    if not source_path.exists():
        with urlopen(Request(row.source_url, headers=HEADERS), timeout=120) as response:
            source_path.write_bytes(response.read())

    quarters = extract_quarters(source_path)
    annual_checks = _annual_identity_checks(quarters)
    available = pd.Timestamp(row.available_date).date().isoformat()
    recovered = [
        {
            **quarter,
            "available_date": available,
            "derivation": "direct_s1_unaudited_single_quarter",
            "accession": row.accession,
        }
        for quarter in quarters
    ]
    facts = []
    for quarter in recovered:
        for metric in ("revenue", "net_income"):
            facts.append({
                "ticker": "GO",
                "fiscal_end": quarter["fiscal_end"],
                "available_date": available,
                "metric": metric,
                "value": quarter[metric],
                "unit": "USD",
                "taxonomy": "GO_US_GAAP_S1",
                "concept": f"sec_s1_quarterly_results_{metric}",
                "form": "S-1",
                "accession": row.accession,
                "source_url": row.source_url,
            })
    frame = pd.DataFrame(facts).sort_values(["fiscal_end", "metric"])
    if len(frame) != 16 or frame[["ticker", "fiscal_end", "metric"]].duplicated().any():
        raise RuntimeError("GO recovery must contain exactly eight paired quarters")

    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    frame.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1,
        "research_only": True,
        "ticker": "GO",
        "point_in_time_proven": True,
        "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "accepted_accounting_basis": "US_GAAP",
        "issuer_boundary": "GROCERY_OUTLET_HOLDING_CORP_SUCCESSOR",
        "accepted_quarter_count": 8,
        "fact_count": 16,
        "recovered_quarters": recovered,
        "annual_identity_checks": annual_checks,
        "filing_sources": [{
            **row._asdict(),
            "sha256": _sha256(source_path),
        }],
        "outputs": {
            "strict_quarterly_facts": {"path": str(facts_path), "sha256": _sha256(facts_path)}
        },
        "guardrail": (
            "Only the S-1's explicitly labelled 2017Q1-2018Q4 unaudited single-quarter "
            "US-GAAP results are accepted, all with the S-1 filing date as availability. "
            "The disclosed $1k 2017 net-income annual rounding difference is preserved."
        ),
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["manifest"] = str(manifest_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry-path", type=Path, default=REGISTRY)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    print(json.dumps(run(args.registry_path, args.output_dir), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
