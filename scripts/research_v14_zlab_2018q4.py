#!/usr/bin/env python3
"""Recover ZLAB 2018Q4 from an annual statement and later nine-month comparator."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

import pandas as pd


ANNUAL_SOURCE = Path("output/data_provenance/zlab_quarterly/zlab_2018_fy.htm")
NINE_MONTH_SOURCE = Path(
    "output/data_provenance/sec_submissions_cache/"
    "ZLAB_0001564590-20-001532_R4.htm"
)
OUTPUT_DIR = Path("output/research_only/v14/zlab_2018q4_pit")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normal(value: object) -> str:
    return " ".join(str(value).replace("\xa0", " ").split()).casefold()


def _accounting(value: object, trailing: object = None) -> float:
    text = str(value).replace(",", "").replace("$", "").strip()
    if trailing is not None and str(trailing).strip() == ")":
        text += ")"
    match = re.search(r"\d+(?:\.\d+)?", text)
    if match is None:
        raise ValueError(f"no accounting value in {value!r}")
    result = float(match.group())
    return -result if "(" in text else result


def _annual_value(path: Path, label: str, year: int) -> float:
    values = set()
    for table in pd.read_html(path):
        labels = table.iloc[:, 0].fillna("").map(_normal)
        rows = labels.eq(label.casefold())
        if rows.sum() != 1:
            continue
        for column in table.columns:
            header = _normal(
                " ".join(str(table.iloc[row, column]) for row in range(min(3, len(table))))
            )
            if str(year) not in header:
                continue
            trailing = table.loc[rows, column + 1].iloc[0] if column + 1 in table.columns else None
            try:
                values.add(_accounting(table.loc[rows, column].iloc[0], trailing))
            except ValueError:
                pass
    if len(values) != 1:
        raise ValueError(f"expected one ZLAB {year} annual {label!r}, found {values}")
    return next(iter(values))


def _nine_month_2018(path: Path) -> tuple[float, float]:
    matches = []
    for table in pd.read_html(path):
        header = " ".join(
            [*(str(value) for value in table.columns),
             *(str(value) for value in table.head(2).to_numpy().ravel())]
        )
        labels = table.iloc[:, 0].fillna("").map(_normal)
        if "9 Months Ended" in header and labels.eq("net loss").sum() == 1:
            matches.append((table, labels))
    if len(matches) != 1:
        raise ValueError(f"expected one ZLAB nine-month statement, found {len(matches)}")
    table, labels = matches[0]
    columns = [column for column in table.columns if "Sep. 30, 2018" in str(column)]
    if len(columns) != 1:
        raise ValueError("expected one 2018 nine-month comparison column")
    column = columns[0]
    revenue = table.loc[labels.eq("revenue"), column].iloc[0]
    cost_of_sales = table.loc[labels.eq("cost of sales"), column].iloc[0]
    if not pd.isna(revenue) or not pd.isna(cost_of_sales):
        raise ValueError("2018 nine-month comparison must explicitly contain no sales")
    net_loss = _accounting(table.loc[labels.eq("net loss"), column].iloc[0])
    return 0.0, net_loss


def recover(annual_path: Path = ANNUAL_SOURCE,
            nine_month_path: Path = NINE_MONTH_SOURCE) -> dict[str, float]:
    annual = {
        "revenue": _annual_value(annual_path, "revenue", 2018),
        "net_income": _annual_value(annual_path, "net loss", 2018),
    }
    nine_month_revenue, nine_month_net = _nine_month_2018(nine_month_path)
    quarter = {
        "revenue": annual["revenue"] - nine_month_revenue,
        "net_income": annual["net_income"] - nine_month_net,
    }
    if quarter != {"revenue": 129_452.0, "net_income": -63_357_297.0}:
        raise ValueError(f"unexpected ZLAB 2018Q4 derivation: {quarter}")
    return quarter


def run(*, annual_path: Path = ANNUAL_SOURCE,
        nine_month_path: Path = NINE_MONTH_SOURCE,
        output_dir: Path = OUTPUT_DIR) -> dict:
    quarter = recover(annual_path, nine_month_path)
    available_date = "2020-01-21"
    accession = "0001564590-20-001532"
    rows = []
    for metric, value in quarter.items():
        rows.append({
            "ticker": "ZLAB", "fiscal_end": "2018-12-31",
            "available_date": available_date, "metric": metric, "value": value,
            "unit": "USD", "taxonomy": "us-gaap",
            "concept": f"derived_q4:{'RevenueFromContractWithCustomerExcludingAssessedTax' if metric == 'revenue' else 'NetIncomeLoss'}",
            "form": "6-K", "accession": accession,
        })
    facts = pd.DataFrame(rows)
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    recovered_quarter = {
        "ticker": "ZLAB", "fiscal_end": "2018-12-31",
        "available_date": available_date, **quarter,
        "derivation": "2018_annual_minus_2018_nine_month_comparator",
    }
    report = {
        "schema_version": 1, "research_only": True,
        "point_in_time_proven": True, "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED", "promotion_eligible": False,
        "ticker": "ZLAB", "accepted_quarter_count": 1, "fact_count": 2,
        "recovered_quarters": [recovered_quarter],
        "source_bindings": [
            {"path": str(annual_path), "sha256": _sha256(annual_path),
             "accession": "0001564590-19-006625", "filed_date": "2019-03-07",
             "role": "exact_2018_annual_statement"},
            {"path": str(nine_month_path), "sha256": _sha256(nine_month_path),
             "accession": accession, "filed_date": available_date,
             "role": "exact_2018_nine_month_comparator"},
        ],
        "guardrail": (
            "The quarter is available only on 2020-01-21, when the later SEC-filed "
            "nine-month comparator made the annual-minus-YTD derivation possible. "
            "No six-month amount is relabelled as a quarter."
        ),
        "outputs": {"strict_quarterly_facts": {
            "path": str(facts_path), "sha256": _sha256(facts_path),
        }},
    }
    manifest = output_dir / "manifest.json"
    manifest.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["manifest"] = str(manifest)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--annual-path", type=Path, default=ANNUAL_SOURCE)
    parser.add_argument("--nine-month-path", type=Path, default=NINE_MONTH_SOURCE)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    print(json.dumps(run(annual_path=args.annual_path,
                         nine_month_path=args.nine_month_path,
                         output_dir=args.output_dir), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
