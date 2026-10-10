#!/usr/bin/env python3
"""Recover REG 2019Q1 from its original SEC rendered XBRL report."""

from __future__ import annotations

import argparse
import hashlib
from io import StringIO
import json
from pathlib import Path
import urllib.request

import pandas as pd


SOURCE_URL = (
    "https://www.sec.gov/Archives/edgar/data/910606/"
    "000091060619000014/R4.htm"
)
ACCESSION = "0000910606-19-000014"
FILED = "2019-05-10"
DEFAULT_OUTPUT_DIR = Path("output/research_only/v14/reg_2019q1_sec_report")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _number(value) -> float:
    text = str(value).strip().replace("$", "").replace(",", "")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    return float(text)


def parse_statement(html: str) -> tuple[float, float]:
    tables = pd.read_html(StringIO(html))
    candidates = [table for table in tables if table.shape[1] == 3]
    if len(candidates) != 1:
        raise ValueError(f"REG statement table is not unique: {len(candidates)}")
    table = candidates[0]
    headers = [str(column[-1] if isinstance(column, tuple) else column) for column in table]
    if headers[1:] != ["Mar. 31, 2019", "Mar. 31, 2018"]:
        raise ValueError(f"unexpected REG statement headers: {headers}")
    labels = table.iloc[:, 0].astype(str).str.strip()
    revenue_values = {
        _number(value)
        for value in table.loc[labels.eq("Total revenues")].iloc[:, 1].dropna()
    }
    income_values = {
        _number(value)
        for value in table.loc[
            labels.eq("Net income attributable to the Company")
        ].iloc[:, 1].dropna()
    }
    if revenue_values != {286257.0} or income_values != {90446.0}:
        raise ValueError(
            f"REG statement values changed: revenue={revenue_values}, "
            f"net_income={income_values}"
        )
    return 286257000.0, 90446000.0


def _fetch(path: Path, *, url: str = SOURCE_URL) -> None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "quant-stocks research contact@example.com"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read()
    if b"Consolidated Statements of Operations" not in payload:
        raise ValueError("downloaded REG SEC report is not the expected statement")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def run(*, output_dir: Path = DEFAULT_OUTPUT_DIR, refresh: bool = False) -> dict:
    raw_path = output_dir / "raw" / "R4.htm"
    if refresh or not raw_path.exists():
        _fetch(raw_path)
    revenue, net_income = parse_statement(raw_path.read_text(encoding="utf-8"))
    facts = pd.DataFrame([
        {
            "ticker": "REG",
            "fiscal_end": pd.Timestamp("2019-03-31"),
            "available_date": pd.Timestamp(FILED),
            "metric": "revenue",
            "value": revenue,
            "taxonomy": "us-gaap",
            "concept": "Revenues",
            "form": "10-Q",
            "accession": ACCESSION,
            "derivation": "direct_sec_rendered_xbrl_statement_value",
        },
        {
            "ticker": "REG",
            "fiscal_end": pd.Timestamp("2019-03-31"),
            "available_date": pd.Timestamp(FILED),
            "metric": "net_income",
            "value": net_income,
            "taxonomy": "us-gaap",
            "concept": "NetIncomeLoss",
            "form": "10-Q",
            "accession": ACCESSION,
            "derivation": "direct_sec_rendered_xbrl_statement_value",
        },
    ]).sort_values("metric").reset_index(drop=True)
    facts["unit"] = "USD"
    facts["source"] = "sec_rendered_xbrl_reg_2019q1"
    facts["source_archive"] = raw_path.name
    facts["source_archive_sha256"] = _sha256(raw_path)

    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "promotion_eligible": False,
        "release_status": "BLOCKED",
        "ticker": "REG",
        "accepted_quarter_count": 1,
        "recovered_quarters": [{
            "ticker": "REG",
            "fiscal_end": "2019-03-31",
            "available_date": FILED,
            "revenue": revenue,
            "net_income": net_income,
        }],
        "filing_binding": {
            "source_url": SOURCE_URL,
            "filed": FILED,
            "accession": ACCESSION,
            "form": "10-Q",
            "raw_path": str(raw_path),
            "raw_sha256": _sha256(raw_path),
        },
        "outputs": {
            "quarters": {"path": str(facts_path), "sha256": _sha256(facts_path)}
        },
        "guardrail": (
            "Values are read from the SEC-hosted rendered XBRL Consolidated "
            "Statements of Operations for the original 2019Q1 filing. The SEC "
            "response is cached and SHA-bound; availability is the filing date."
        ),
    }
    manifest = output_dir / "manifest.json"
    manifest.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["manifest"] = str(manifest)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    result = run(output_dir=args.output_dir, refresh=args.refresh)
    print(json.dumps({
        "manifest": result["manifest"],
        "accepted_quarter_count": result["accepted_quarter_count"],
        "release_status": result["release_status"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
