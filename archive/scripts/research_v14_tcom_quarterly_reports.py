#!/usr/bin/env python3
"""Recover TCOM 2018Q1-2021Q4 GAAP quarters from contemporaneous SEC 6-Ks."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pandas as pd


REGISTRY = Path("stocks_list_dir/nasdaq/tcom_quarterly_reports.csv")
OUTPUT_DIR = Path("output/research_only/v14/tcom_quarterly_reports_2018q1_2021q4")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normal(value: object) -> str:
    return " ".join(str(value).replace("\xa0", " ").split()).casefold()


def _number(value: object) -> float | None:
    text = str(value).strip().replace(",", "")
    if not re.search(r"\d", text):
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if match is None:
        return None
    result = float(match.group())
    return -abs(result) if "(" in text or result < 0 else result


def _row_values(table: pd.DataFrame, row_mask: pd.Series, columns: list[int]) -> list[float]:
    values: list[float] = []
    for column in columns:
        value = _number(table.loc[row_mask].iloc[0, column])
        if value is not None and value not in values:
            values.append(value)
    return values


def extract_quarter(path: Path, year: int, quarter: int) -> dict[str, float]:
    fiscal_end = pd.Period(f"{year}Q{quarter}", freq="Q-DEC").end_time
    date_label = _normal(fiscal_end.strftime("%B %d, %Y").replace(" 0", " "))
    candidates: list[dict[str, float]] = []
    for table in pd.read_html(path):
        labels = table.iloc[:, 0].map(_normal)
        revenue_row = labels.eq("net revenue")
        net_income_row = (
            labels.str.startswith("net ")
            & labels.str.contains("attributable to", regex=False)
            & labels.str.contains(r"(?:ctrip|trip\.com)", regex=True)
            & ~labels.str.contains("non-controlling", regex=False)
        )
        if revenue_row.sum() != 1 or net_income_row.sum() != 1:
            continue
        columns: list[int] = []
        for column in range(1, len(table.columns)):
            headers = [_normal(value) for value in table.iloc[:6, column]]
            joined = " ".join(headers)
            is_quarter = any(
                "quarter ended" in header or "three months ended" in header
                for header in headers
            )
            is_long_period = any(
                "six months ended" in header or "year ended" in header
                for header in headers
            )
            if date_label in joined and "rmb" in joined and is_quarter and not is_long_period:
                columns.append(column)
        revenue = _row_values(table, revenue_row, columns)
        net_income = _row_values(table, net_income_row, columns)
        if len(revenue) == 1 and len(net_income) == 1:
            candidates.append({
                "revenue": revenue[0] * 1_000_000.0,
                "net_income": net_income[0] * 1_000_000.0,
            })
    if len(candidates) != 1:
        raise ValueError(
            f"expected one TCOM current-quarter RMB statement in {path}, found {len(candidates)}"
        )
    return candidates[0]


def run(registry_path: Path = REGISTRY, output_dir: Path = OUTPUT_DIR) -> dict:
    registry = pd.read_csv(registry_path, dtype={"cik": str, "accession": str})
    expected = {f"{year}q{quarter}" for year in range(2018, 2022) for quarter in range(1, 5)}
    if set(registry["ticker"]) != {"TCOM"} or set(registry["cik"]) != {"1269238"}:
        raise ValueError("TCOM registry contains another issuer")
    if len(registry) != 16 or set(registry["source_id"]) != expected:
        raise ValueError("TCOM registry must contain exactly 2018Q1-2021Q4")

    facts = []
    recovered = []
    sources = []
    for row in registry.itertuples(index=False):
        path = Path(row.local_path)
        if not path.exists():
            raise FileNotFoundError(path)
        year = int(row.source_id[:4])
        quarter = int(row.source_id[-1])
        fiscal_end = pd.Period(f"{year}Q{quarter}", freq="Q-DEC").end_time.normalize()
        values = extract_quarter(path, year, quarter)
        sources.append({**row._asdict(), "sha256": _sha256(path)})
        recovered.append({
            "ticker": "TCOM",
            "fiscal_end": fiscal_end.date().isoformat(),
            "available_date": row.available_date,
            **values,
            "derivation": "direct_contemporaneous_sec_6k_gaap_quarter_statement",
        })
        for metric, value in values.items():
            facts.append({
                "ticker": "TCOM",
                "metric": metric,
                "fiscal_end": fiscal_end.date().isoformat(),
                "available_date": row.available_date,
                "value": value,
                "unit": "CNY",
                "taxonomy": "TCOM_SEC_6K",
                "concept": f"sec_6k_tcom_gaap_{metric}",
                "form": row.form,
                "accession": row.accession,
                "source_url": row.source_url,
            })

    frame = pd.DataFrame(facts).sort_values(["fiscal_end", "metric"]).reset_index(drop=True)
    if len(frame) != 32 or frame[["ticker", "metric", "fiscal_end"]].duplicated().any():
        raise RuntimeError("TCOM recovery must contain exactly 16 paired quarters")
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    frame.to_csv(facts_path, index=False)
    manifest = {
        "schema_version": 1,
        "research_only": True,
        "ticker": "TCOM",
        "point_in_time_proven": True,
        "parameters_frozen": False,
        "accepted_quarter_count": 16,
        "fact_count": 32,
        "currency": "CNY",
        "filing_sources": sources,
        "recovered_quarters": recovered,
        "outputs": {
            "strict_quarterly_facts": {"path": str(facts_path), "sha256": _sha256(facts_path)}
        },
        "guardrail": (
            "Only the current-quarter RMB GAAP statement columns are accepted. "
            "USD convenience translations, non-GAAP reconciliations, six-month and annual columns are excluded."
        ),
        "release_status": "BLOCKED",
        "promotion_eligible": False,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, sort_keys=True))
