#!/usr/bin/env python3
"""Recover ESLT 2018Q1-2021Q4 GAAP quarters from contemporaneous SEC 6-Ks."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pandas as pd

REGISTRY = Path("stocks_list_dir/nasdaq/eslt_quarterly_reports.csv")
OUTPUT_DIR = Path("output/research_only/v14/eslt_quarterly_reports_2018q1_2021q4")
NET_LABEL = "Net income attributable to Elbit Systems Ltd.'s shareholders"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _number(value: object) -> float | None:
    text = str(value).strip().replace(",", "")
    if not text or text in {"nan", "$", "%", "—", "-"}:
        return None
    negative = text.startswith("(") or text.endswith(")")
    text = text.strip("()")
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        return None
    result = float(text)
    return -abs(result) if negative else result


def _dedupe_adjacent(values: list[float]) -> list[float]:
    result: list[float] = []
    for value in values:
        if not result or value != result[-1]:
            result.append(value)
    return result


def _metric_values(path: Path, label: str) -> list[float]:
    candidates = []
    for table in pd.read_html(path):
        first = table.iloc[:, 0].astype(str)
        rows = table.loc[first.eq(label)]
        if rows.empty:
            continue
        values = _dedupe_adjacent(
            [number for value in rows.iloc[0, 1:] if (number := _number(value)) is not None]
        )
        if len(values) >= 3:
            candidates.append(values)
    if not candidates:
        raise ValueError(f"No {label!r} statement row in {path}")
    # Acquisition pro-forma tables can repeat the label in USD millions.  The
    # GAAP condensed statement is explicitly in USD thousands and therefore
    # has the largest absolute values in these filings.
    return max(candidates, key=lambda values: max(abs(value) for value in values))


def extract_quarter(path: Path, quarter: int) -> dict[str, float]:
    revenue = _metric_values(path, "Revenues")
    net_income = _metric_values(path, NET_LABEL)
    index = 0 if quarter == 1 else 2
    return {"revenue": revenue[index] * 1_000.0, "net_income": net_income[index] * 1_000.0}


def run(registry_path: Path = REGISTRY, output_dir: Path = OUTPUT_DIR) -> dict:
    registry = pd.read_csv(registry_path, dtype={"cik": str})
    facts = []
    sources = []
    for row in registry.itertuples(index=False):
        path = Path(row.local_path)
        year = int(row.source_id[:4])
        quarter = int(row.source_id[-1])
        values = extract_quarter(path, quarter)
        fiscal_end = pd.Period(f"{year}Q{quarter}", freq="Q-DEC").end_time.normalize()
        sources.append({**row._asdict(), "sha256": _sha256(path)})
        for metric, value in values.items():
            facts.append({
                "ticker": "ESLT", "metric": metric, "fiscal_end": fiscal_end.date().isoformat(),
                "available_date": row.available_date, "value": value, "unit": "USD",
                "taxonomy": "ESLT_SEC_6K", "concept": f"sec_6k_eslt_gaap_{metric}",
                "form": row.form, "accession": row.accession, "source_url": row.source_url,
            })
    frame = pd.DataFrame(facts).sort_values(["fiscal_end", "metric"]).reset_index(drop=True)
    if len(frame) != 32 or frame[["ticker", "metric", "fiscal_end"]].duplicated().any():
        raise RuntimeError("ESLT recovery must contain exactly 16 paired quarters")
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    frame.to_csv(facts_path, index=False)
    recovered_quarters = []
    for (fiscal_end, available_date), rows in frame.groupby(["fiscal_end", "available_date"]):
        values = rows.set_index("metric")["value"].to_dict()
        recovered_quarters.append({
            "ticker": "ESLT", "fiscal_end": fiscal_end, "available_date": available_date,
            "revenue": values["revenue"], "net_income": values["net_income"],
            "derivation": "direct_contemporaneous_sec_6k_gaap_quarter_statement",
        })
    manifest = {
        "schema_version": 1, "research_only": True, "ticker": "ESLT",
        "point_in_time_proven": True, "parameters_frozen": False,
        "accepted_quarter_count": 16, "fact_count": 32,
        "filing_sources": sources, "recovered_quarters": recovered_quarters,
        "outputs": {"strict_quarterly_facts": {"path": str(facts_path), "sha256": _sha256(facts_path)}},
        "release_status": "BLOCKED", "promotion_eligible": False,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, sort_keys=True))
