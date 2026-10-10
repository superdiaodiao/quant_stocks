#!/usr/bin/env python3
"""Recover AZN 2018-2021 direct GAAP quarters from original SEC 6-K results."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd
from bs4 import BeautifulSoup


REGISTRY = Path("stocks_list_dir/nasdaq/azn_quarterly_results.csv")
OUTPUT_DIR = Path("output/research_only/v14/azn_quarterly_results_2018_2021")
HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
METRICS = {"revenue": "total revenue", "net_income": "profit for the period"}
METRIC_LABELS = {
    "revenue": ("total revenue",),
    "net_income": (
        "profit for the period",
        "(loss)/profit for the period",
        "profit/(loss) for the period",
    ),
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
        if all(labels.isin(METRIC_LABELS[metric]).sum() == 1 for metric in METRICS):
            candidates.append(table)
    return candidates


def extract_period(path: Path, *, year: int, phrase: str) -> dict[str, float]:
    """Extract one reported USD-million period, excluding YTD and CER columns."""
    unique = set()
    for table in _statement_tables(path):
        labels = table.iloc[:, 0].fillna("").map(_normal)
        accepted_labels = {
            label for aliases in METRIC_LABELS.values() for label in aliases
        }
        first_metric_row = min(labels.index[labels.isin(accepted_labels)])
        table_header = _normal(
            " ".join(
                str(value)
                for value in table.loc[table.index < first_metric_row].to_numpy().ravel()
            )
        )
        if phrase.casefold() not in table_header:
            continue
        for column in table.columns:
            # SEC presentation tables can put the year in a spacer column and the
            # USD-million unit/value in the adjacent column.  Bind a value column
            # only to its local header group so the prior-year group cannot leak in.
            group_start = max(int(table.columns[0]), int(column) - 2)
            column_header = _normal(
                " ".join(
                    str(table.loc[row, column])
                    for row in table.index[table.index < first_metric_row]
                )
            )
            local_header = _normal(
                " ".join(
                    str(table.loc[row, header_column])
                    for row in table.index[table.index < first_metric_row]
                    for header_column in range(group_start, int(column) + 1)
                )
            )
            explicit_years = set(
                re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", column_header)
            )
            if explicit_years:
                year_matches = str(year) in explicit_years
            else:
                year_matches = str(year) in local_header
            if not year_matches or "$m" not in local_header:
                continue
            values = []
            valid = True
            for metric in METRICS:
                row = labels.isin(METRIC_LABELS[metric])
                trailing = table.loc[row, column + 1].iloc[0] if column + 1 in table.columns else None
                try:
                    values.append(_accounting(table.loc[row, column].iloc[0], trailing) * 1_000_000.0)
                except ValueError:
                    valid = False
                    break
            if valid and values[0] > 0:
                unique.add(tuple(values))
    if len(unique) != 1:
        raise ValueError(
            f"expected one AZN {year} {phrase!r} pair in {path}, found {sorted(unique)}"
        )
    return dict(zip(METRICS, next(iter(unique))))


def run(registry_path: Path = REGISTRY, output_dir: Path = OUTPUT_DIR) -> dict:
    registry = pd.read_csv(registry_path, dtype={"cik": str, "accession": str})
    expected = {f"{year}_q{quarter}" for year in range(2018, 2022) for quarter in range(1, 5)}
    if len(registry) != 16 or set(registry["ticker"]) != {"AZN"} or set(registry["cik"]) != {"901832"}:
        raise ValueError("AZN registry must bind one issuer and sixteen filings")
    if set(registry["source_id"]) != expected or set(registry["form"]) != {"6-K"}:
        raise ValueError("AZN registry must cover 2018Q1 through 2021Q4 with 6-Ks")

    paths = {}
    for row in registry.itertuples(index=False):
        if row.accession.replace("-", "") not in row.source_url:
            raise ValueError(f"AZN URL is not accession-bound: {row.source_id}")
        path = Path(row.local_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            with urlopen(Request(row.source_url, headers=HEADERS), timeout=120) as response:
                path.write_bytes(response.read())
        text = " ".join(BeautifulSoup(path.read_bytes(), "html.parser").get_text(" ", strip=True).split())
        if "AstraZeneca".casefold() not in text.casefold():
            raise ValueError(f"AZN issuer identity missing in {row.source_id}")
        paths[row.source_id] = path

    recovered = []
    annual_checks = []
    for year in range(2018, 2022):
        current_year = []
        for quarter in range(1, 5):
            source_id = f"{year}_q{quarter}"
            row = registry.loc[registry["source_id"].eq(source_id)].iloc[0]
            values = extract_period(paths[source_id], year=year, phrase="quarter ended")
            item = {
                "ticker": "AZN", "fiscal_end": row.fiscal_end,
                "available_date": row.available_date, **values,
                "derivation": "direct_reported_current_quarter_usd_millions",
                "source_id": source_id, "accession": row.accession,
            }
            recovered.append(item)
            current_year.append(item)
        annual = extract_period(paths[f"{year}_q4"], year=year, phrase="year ended")
        sums = {metric: sum(row[metric] for row in current_year) for metric in METRICS}
        differences = {metric: sums[metric] - annual[metric] for metric in METRICS}
        if any(abs(value) > 1_000_000.0 for value in differences.values()):
            raise RuntimeError(f"AZN {year} direct quarters do not close to annual: {differences}")
        annual_checks.append({
            "year": year, "quarter_sum": sums, "reported_annual": annual,
            "difference": differences, "maximum_rounding_difference": 1_000_000.0,
        })

    facts = []
    for quarter in recovered:
        source = registry.loc[registry["source_id"].eq(quarter["source_id"])].iloc[0]
        for metric in METRICS:
            facts.append({
                "ticker": "AZN", "fiscal_end": quarter["fiscal_end"],
                "available_date": quarter["available_date"], "metric": metric,
                "value": quarter[metric], "unit": "USD", "taxonomy": "AZN_IFRS_SEC_6K",
                "concept": f"sec_filed_azn_{metric}", "form": source.form,
                "accession": source.accession, "source_url": source.source_url,
            })
    frame = pd.DataFrame(facts).sort_values(["fiscal_end", "metric"])
    if len(frame) != 32 or frame[["ticker", "fiscal_end", "metric"]].duplicated().any():
        raise RuntimeError("AZN recovery must contain sixteen paired quarters")

    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    frame.to_csv(facts_path, index=False)
    bindings = [{**row._asdict(), "sha256": _sha256(paths[row.source_id])}
                for row in registry.itertuples(index=False)]
    report = {
        "schema_version": 1, "research_only": True, "ticker": "AZN",
        "point_in_time_proven": True, "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED", "promotion_eligible": False,
        "accepted_accounting_basis": "IFRS_AS_FILED_WITH_SEC",
        "currency": "USD", "accepted_quarter_count": 16, "fact_count": 32,
        "recovered_quarters": recovered, "annual_identity_checks": annual_checks,
        "filing_sources": bindings,
        "outputs": {"strict_quarterly_facts": {
            "path": str(facts_path), "sha256": _sha256(facts_path),
        }},
        "guardrail": (
            "Every fact uses the original SEC-filed GAAP/IFRS current-quarter USD-million "
            "Total Revenue and Profit for the period columns. H1, nine-month, full-year, "
            "constant-exchange-rate and core/non-GAAP columns are excluded."
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
