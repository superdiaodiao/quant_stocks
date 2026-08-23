#!/usr/bin/env python3
"""Restore ICLR's contemporaneous 2017-2018 quarterly revenue PIT chain."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd
from bs4 import BeautifulSoup


REGISTRY = Path("stocks_list_dir/nasdaq/iclr_2017_quarterly_reports.csv")
OUTPUT_DIR = Path("output/research_only/v14/iclr_2017_quarterly_reports")
HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
METRICS = {
    # ICLR adopted ASC 606 in 2018. Its 2017 statements label the strategy
    # input "Net revenue" while the 2018 as-reported statement labels it
    # "Revenue"; both are the issuer's audited/GAAP top-line measure.
    "revenue": ("net revenue", "revenue", "revenue/gross revenue"),
    "net_income": ("net income",),
}
ANNUAL = {
    2017: {"revenue": 1_758_439_000.0, "net_income": 281_488_000.0},
    2018: {"revenue": 2_595_777_000.0, "net_income": 322_656_000.0},
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normal(value: object) -> str:
    return " ".join(str(value).replace("\xa0", " ").split()).casefold()


def _accounting(value: object, trailing: object = None) -> float:
    text = str(value).strip().replace(",", "").replace("$", "")
    if trailing is not None and str(trailing).strip() == ")":
        text += ")"
    match = re.search(r"\d+(?:\.\d+)?", text)
    if match is None:
        raise ValueError(f"no accounting value in {value!r}")
    result = float(match.group())
    return -result if "(" in text else result


def _statement_tables(path: Path) -> list[pd.DataFrame]:
    candidates = []
    for table in pd.read_html(path):
        labels = table.iloc[:, 0].fillna("").map(_normal)
        if all(labels.isin(metric_labels).sum() == 1 for metric_labels in METRICS.values()):
            candidates.append(table)
    return candidates


def _metric_value(table: pd.DataFrame, metric: str, column: object) -> float:
    labels = table.iloc[:, 0].fillna("").map(_normal)
    row = labels.isin(METRICS[metric])
    trailing = table.loc[row, column + 1].iloc[0] if column + 1 in table.columns else None
    return _accounting(table.loc[row, column].iloc[0], trailing) * 1_000.0


def extract_direct_quarter(path: Path, *, year: int = 2017) -> dict[str, float]:
    """Use the current three-month column, never the six/nine-month column."""
    unique = set()
    for table in _statement_tables(path):
        table_header = _normal(" ".join(str(value) for value in table.head(4).to_numpy().ravel()))
        if str(year - 1) not in table_header:
            # Excludes 2018 ASC-606 adjustment-only tables, which contain
            # as-reported, adjustment and pro-forma columns for the same date.
            continue
        for column in table.columns:
            header = _normal(" ".join(str(table.iloc[row, column]) for row in range(min(4, len(table)))))
            if "three months ended" not in header or str(year) not in header:
                continue
            try:
                values = tuple(_metric_value(table, metric, column) for metric in METRICS)
            except ValueError:
                continue
            if values[0] > 0:
                unique.add(values)
    if len(unique) != 1:
        raise ValueError(f"expected one ICLR direct-quarter pair in {path}, found {sorted(unique)}")
    values = next(iter(unique))
    return dict(zip(METRICS, values))


def extract_annual(path: Path, *, year: int = 2017) -> dict[str, float]:
    expected = ANNUAL[year]
    unique = set()
    for table in _statement_tables(path):
        for column in table.columns:
            header = _normal(" ".join(str(table.iloc[row, column]) for row in range(min(5, len(table)))))
            if "year ended" not in header or str(year) not in header:
                continue
            try:
                values = tuple(_metric_value(table, metric, column) for metric in METRICS)
            except ValueError:
                continue
            if values == tuple(expected.values()):
                unique.add(values)
    if unique != {tuple(expected.values())}:
        raise ValueError(f"ICLR audited {year} annual pair not uniquely proven in {path}")
    return expected.copy()


def run(registry_path: Path = REGISTRY, output_dir: Path = OUTPUT_DIR) -> dict:
    registry = pd.read_csv(registry_path, dtype={"cik": str, "accession": str})
    expected_sources = {
        f"{year}_{period}"
        for year in (2017, 2018)
        for period in ("q1", "q2", "q3", "fy")
    }
    if len(registry) != 8 or set(registry["ticker"]) != {"ICLR"} or set(registry["cik"]) != {"1060955"}:
        raise ValueError("ICLR registry must bind one issuer and eight 2017-2018 filings")
    if set(registry["source_id"]) != expected_sources:
        raise ValueError("ICLR registry does not cover 2017-2018 Q1-Q3 and FY")

    paths = {}
    for row in registry.itertuples(index=False):
        if row.accession.replace("-", "") not in row.source_url:
            raise ValueError(f"ICLR URL is not accession-bound: {row.source_id}")
        path = Path(row.local_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            with urlopen(Request(row.source_url, headers=HEADERS), timeout=120) as response:
                path.write_bytes(response.read())
        document = " ".join(BeautifulSoup(path.read_bytes(), "html.parser").get_text(" ", strip=True).split())
        if "ICON plc".casefold() not in document.casefold():
            raise ValueError(f"ICLR issuer identity missing in {row.source_id}")
        paths[row.source_id] = path

    recovered = []
    annual_checks = []
    expected_q4 = {
        2017: {"revenue": 455_139_000.0, "net_income": 71_108_000.0},
        2018: {"revenue": 679_025_000.0, "net_income": 88_163_000.0},
    }
    for year in (2017, 2018):
        current_year = []
        for period in ("q1", "q2", "q3"):
            source_id = f"{year}_{period}"
            row = registry.loc[registry["source_id"].eq(source_id)].iloc[0]
            quarter = {
                "ticker": "ICLR", "fiscal_end": row.fiscal_end,
                "available_date": row.available_date,
                **extract_direct_quarter(paths[source_id], year=year),
                "derivation": "direct_original_sec_three_month_statement",
                "source_id": source_id, "accession": row.accession,
            }
            recovered.append(quarter)
            current_year.append(quarter)
        annual_id = f"{year}_fy"
        annual_row = registry.loc[registry["source_id"].eq(annual_id)].iloc[0]
        annual = extract_annual(paths[annual_id], year=year)
        q4 = {
            metric: annual[metric] - sum(quarter[metric] for quarter in current_year)
            for metric in METRICS
        }
        if q4 != expected_q4[year]:
            raise RuntimeError(f"unexpected ICLR {year} Q4 arithmetic: {q4}")
        recovered.append({
            "ticker": "ICLR", "fiscal_end": annual_row.fiscal_end,
            "available_date": annual_row.available_date, **q4,
            "derivation": "audited_annual_minus_original_pit_q1_q2_q3",
            "source_id": annual_id, "accession": annual_row.accession,
        })
        annual_checks.append({
            "year": year, "audited_annual": annual,
            "quarter_sum": {
                metric: sum(row[metric] for row in recovered if str(row["fiscal_end"]).startswith(str(year)))
                for metric in METRICS
            },
            "exact_arithmetic_identity": True,
        })

    facts = []
    for quarter in recovered:
        source = registry.loc[registry["source_id"].eq(quarter["source_id"])].iloc[0]
        for metric in METRICS:
            facts.append({
                "ticker": "ICLR", "fiscal_end": quarter["fiscal_end"],
                "available_date": quarter["available_date"], "metric": metric,
                "value": quarter[metric], "unit": "USD", "taxonomy": "us-gaap",
                "concept": f"sec_filed_iclr_net_{metric}", "form": source.form,
                "accession": source.accession, "source_url": source.source_url,
            })
    frame = pd.DataFrame(facts).sort_values(["fiscal_end", "metric"])
    if len(frame) != 16 or frame[["ticker", "fiscal_end", "metric"]].duplicated().any():
        raise RuntimeError("ICLR recovery must contain eight paired quarters")

    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    frame.to_csv(facts_path, index=False)
    bindings = [{**row._asdict(), "sha256": _sha256(paths[row.source_id])}
                for row in registry.itertuples(index=False)]
    report = {
        "schema_version": 1, "research_only": True, "ticker": "ICLR",
        "point_in_time_proven": True, "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED", "promotion_eligible": False,
        "accepted_accounting_basis": "US_GAAP", "accepted_quarter_count": 8,
        "direct_quarter_count": 6, "derived_q4_count": 2, "fact_count": 16,
        "recovered_quarters": recovered,
        "annual_identity_checks": annual_checks,
        "filing_sources": bindings,
        "outputs": {"strict_quarterly_facts": {
            "path": str(facts_path), "sha256": _sha256(facts_path),
        }},
        "guardrail": (
            "ICLR 2017-2018 Q1-Q3 use only contemporaneous SEC 6-K current-three-month "
            "columns. Each Q4 is the audited 20-F annual value less those original PIT quarters. "
            "Six-month, nine-month and later comparative disclosures are excluded."
        ),
    }
    manifest = output_dir / "manifest.json"
    manifest.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["manifest"] = str(manifest)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry-path", type=Path, default=REGISTRY)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    print(json.dumps(run(args.registry_path, args.output_dir), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
