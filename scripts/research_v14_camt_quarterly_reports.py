#!/usr/bin/env python3
"""Recover CAMT 2018-2021 direct quarters from contemporaneous SEC 6-Ks."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pandas as pd


REGISTRY = Path("stocks_list_dir/nasdaq/camt_quarterly_reports.csv")
OUTPUT_DIR = Path("output/research_only/v14/camt_quarterly_reports_2018q1_2021q4")
METRIC_LABELS = {
    "revenue": ("revenues",),
    "net_income": ("net income", "net income (loss)"),
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normal(value: object) -> str:
    return " ".join(str(value).replace("\xa0", " ").split()).casefold()


def _accounting_value(value: object) -> float:
    text = str(value).strip().replace(",", "").replace("$", "")
    if text in {"—", "–", "-"}:
        return 0.0
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if match is None:
        raise ValueError(f"no accounting value in {value!r}")
    result = float(match.group())
    return -abs(result) if "(" in text or result < 0 else result


def _statement_table(path: Path) -> pd.DataFrame:
    candidates = []
    for table in pd.read_html(path):
        labels = table.iloc[:, 0].map(_normal)
        if labels.eq("revenues").sum() != 1:
            continue
        if sum(labels.eq(label).sum() for label in METRIC_LABELS["net_income"]) != 1:
            continue
        candidates.append(table)
    if len(candidates) != 1:
        raise ValueError(
            f"expected one CAMT GAAP income statement in {path}, found {len(candidates)}"
        )
    return candidates[0]


def _current_quarter_column(table: pd.DataFrame, *, year: int) -> object:
    columns = []
    for column in table.columns:
        header = _normal(" ".join(str(value) for value in table.iloc[:3][column]))
        if "three months" not in header or str(year) not in header:
            continue
        revenue_row = table.iloc[:, 0].map(_normal).eq("revenues")
        if revenue_row.sum() == 1 and pd.notna(table.loc[revenue_row, column].iloc[0]):
            columns.append(column)
    if len(columns) != 1:
        raise ValueError(
            f"expected one current-three-month column for {year}, found {columns}"
        )
    return columns[0]


def _row_value(table: pd.DataFrame, column: object, label: str) -> float:
    labels = table.iloc[:, 0].map(_normal)
    rows = labels.eq(label)
    if rows.sum() != 1:
        raise ValueError(f"expected one {label!r} row, found {rows.sum()}")
    return _accounting_value(table.loc[rows, column].iloc[0]) * 1_000.0


def _metric_value(
    table: pd.DataFrame, column: object, labels: tuple[str, ...]
) -> float:
    row_labels = table.iloc[:, 0].map(_normal)
    rows = pd.Series(False, index=row_labels.index)
    for label in labels:
        rows |= row_labels.eq(label)
    if rows.sum() != 1:
        raise ValueError(f"expected one of {labels!r}, found {rows.sum()}")
    return _accounting_value(table.loc[rows, column].iloc[0]) * 1_000.0


def extract_statement(path: Path, *, year: int) -> dict[str, float]:
    """Extract only the current three-month GAAP column, never YTD or annual."""
    table = _statement_table(path)
    column = _current_quarter_column(table, year=year)
    return {
        metric: _metric_value(table, column, labels)
        for metric, labels in METRIC_LABELS.items()
    }


def extract_2019q2_discontinued_event(path: Path) -> dict[str, float]:
    table = _statement_table(path)
    column = _current_quarter_column(table, year=2019)
    return {
        "continuing_operations_net_income": _row_value(
            table, column, "net income from continuing operations"
        ),
        "discontinued_operations_net_income": _row_value(
            table, column, "net income from discontinued operations"
        ),
        "gaap_total_net_income": _row_value(table, column, "net income"),
    }


def run(registry_path: Path = REGISTRY, output_dir: Path = OUTPUT_DIR) -> dict:
    registry = pd.read_csv(registry_path, dtype={"cik": str, "accession": str})
    expected = {
        f"{year}_q{quarter}"
        for year in range(2018, 2022)
        for quarter in range(1, 5)
    }
    if set(registry["ticker"]) != {"CAMT"} or set(registry["cik"]) != {"1109138"}:
        raise ValueError("CAMT registry contains another issuer")
    if len(registry) != 16 or set(registry["source_id"]) != expected:
        raise ValueError("CAMT registry must contain 2018Q1 through 2021Q4")
    if set(registry["form"]) != {"6-K"}:
        raise ValueError("CAMT registry must contain only SEC 6-K filings")

    paths = {row.source_id: Path(row.local_path) for row in registry.itertuples(index=False)}
    missing = [str(path) for path in paths.values() if not path.exists()]
    if missing:
        raise FileNotFoundError("missing CAMT SEC 6-Ks: " + ", ".join(missing))

    facts = []
    recovered = []
    for row in registry.sort_values("source_id").itertuples(index=False):
        year, quarter = (int(part) for part in row.source_id.replace("q", "").split("_"))
        accession_compact = row.accession.replace("-", "")
        if accession_compact not in row.source_url:
            raise ValueError(f"source URL is not bound to accession {row.accession}")
        available_date = pd.Timestamp(row.available_date).normalize()
        fiscal_end = pd.Period(f"{year}Q{quarter}", freq="Q-DEC").end_time.normalize()
        if available_date <= fiscal_end:
            raise ValueError(f"non-PIT CAMT availability for {row.source_id}")
        values = extract_statement(paths[row.source_id], year=year)
        recovered.append({
            "ticker": "CAMT",
            "fiscal_end": fiscal_end.date().isoformat(),
            "available_date": available_date.date().isoformat(),
            **values,
            "derivation": "direct_reported_current_three_months",
            "source_id": row.source_id,
        })
        for metric, value in values.items():
            facts.append({
                "ticker": "CAMT",
                "metric": metric,
                "fiscal_end": fiscal_end.date().isoformat(),
                "available_date": available_date.date().isoformat(),
                "value": value,
                "unit": "USD",
                "taxonomy": "CAMT_US_GAAP_SEC_6K",
                "concept": f"sec_filed_camt_gaap_{metric}",
                "form": row.form,
                "accession": row.accession,
                "source_url": row.source_url,
            })

    frame = pd.DataFrame(facts).sort_values(["fiscal_end", "metric"]).reset_index(drop=True)
    if len(frame) != 32 or frame[["ticker", "metric", "fiscal_end"]].duplicated().any():
        raise RuntimeError("CAMT recovery must contain exactly 16 paired quarters")
    if frame["fiscal_end"].nunique() != 16:
        raise RuntimeError("CAMT recovery is not a continuous 16-quarter chain")

    event = extract_2019q2_discontinued_event(paths["2019_q2"])
    if event["continuing_operations_net_income"] + event[
        "discontinued_operations_net_income"
    ] != event["gaap_total_net_income"]:
        raise RuntimeError("CAMT 2019Q2 GAAP net income components do not close")

    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    frame.to_csv(facts_path, index=False)
    bindings = [
        {**row._asdict(), "sha256": _sha256(paths[row.source_id])}
        for row in registry.itertuples(index=False)
    ]
    manifest = {
        "schema_version": 1,
        "research_only": True,
        "ticker": "CAMT",
        "point_in_time_proven": True,
        "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "accepted_accounting_basis": "US_GAAP",
        "accepted_quarter_count": 16,
        "fact_count": 32,
        "filing_sources": bindings,
        "recovered_quarters": recovered,
        "material_accounting_events": [{
            "source_id": "2019_q2",
            "description": (
                "The directly reported current-quarter GAAP total includes income from "
                "discontinued operations; it is not relabeled as continuing-operations profit."
            ),
            **event,
        }],
        "outputs": {
            "strict_quarterly_facts": {"path": str(facts_path), "sha256": _sha256(facts_path)}
        },
        "guardrail": (
            "Every fact uses the current three-month column in its contemporaneous 6-K. "
            "Six-month, nine-month, full-year, comparative, and non-GAAP columns are excluded."
        ),
        "release_status": "BLOCKED",
        "promotion_eligible": False,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, sort_keys=True))
