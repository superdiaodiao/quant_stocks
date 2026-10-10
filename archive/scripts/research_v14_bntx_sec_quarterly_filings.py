#!/usr/bin/env python3
"""Recover strict BNTX 2019Q1-2021Q4 SEC IFRS quarters."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd
from bs4 import BeautifulSoup


DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/bntx_sec_quarterly_filings_2019q1_2021q4"
)
SEC_BASE = "https://www.sec.gov/Archives/edgar/data/1776985"
HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}

# Exact EUR values. Interim reports explicitly present three-month columns.
# Q4 is the exact annual value less the contemporaneously filed nine-month YTD.
PERIOD_EVIDENCE = {
    # These comparative three-month columns first became SEC-visible in the
    # corresponding 2020 interim filing. Their PIT dates are not backdated.
    "2019-03-31": {
        "available_date": "2020-05-12", "revenue": 26_154_000,
        "profit": -40_762_000, "derivation": "direct_comparative_three_month_statement",
        "current": ("0001564590-20-024741", "bntx-ex991_6.htm",
                    26_154_000, -40_762_000), "prior": None,
    },
    "2019-06-30": {
        "available_date": "2020-08-11", "revenue": 25_785_000,
        "profit": -50_084_000, "derivation": "direct_comparative_three_month_statement",
        "current": ("0001564590-20-039114", "bntx-ex991_6.htm",
                    25_785_000, -50_084_000), "prior": None,
    },
    "2019-09-30": {
        "available_date": "2020-11-10", "revenue": 28_662_000,
        "profit": -30_103_000, "derivation": "direct_comparative_three_month_statement",
        "current": ("0001564590-20-052734", "bntx-ex991_6.htm",
                    28_662_000, -30_103_000), "prior": None,
    },
    "2019-12-31": {
        "available_date": "2020-11-10", "revenue": 27_988_000,
        "profit": -58_223_000, "derivation": "fy_20f_minus_later_9m_comparative",
        "current": ("0001564590-20-014536", "bntx-20f_20191231.htm",
                    108_589_000, -179_172_000),
        "prior": ("0001564590-20-052734", "bntx-ex991_6.htm",
                  80_601_000, -120_949_000),
    },
    "2020-03-31": {
        "available_date": "2020-05-12", "revenue": 27_663_000,
        "profit": -53_386_000, "derivation": "direct_three_month_ifrs_statement",
        "current": ("0001564590-20-024741", "bntx-ex991_6.htm",
                    27_663_000, -53_386_000), "prior": None,
    },
    "2020-06-30": {
        "available_date": "2020-08-11", "revenue": 41_762_000,
        "profit": -88_296_000, "derivation": "direct_three_month_ifrs_statement",
        "current": ("0001564590-20-039114", "bntx-ex991_6.htm",
                    41_762_000, -88_296_000), "prior": None,
    },
    "2020-09-30": {
        "available_date": "2020-11-10", "revenue": 67_458_000,
        "profit": -210_032_000, "derivation": "direct_three_month_ifrs_statement",
        "current": ("0001564590-20-052734", "bntx-ex991_6.htm",
                    67_458_000, -210_032_000), "prior": None,
    },
    "2020-12-31": {
        "available_date": "2021-03-30", "revenue": 345_442_000,
        "profit": 366_912_000, "derivation": "contemporaneous_fy_minus_9m",
        "current": ("0001564590-21-016303", "bntx-ex991_7.htm",
                    482_325_000, 15_198_000),
        "prior": ("0001564590-20-052734", "bntx-ex991_6.htm",
                  136_883_000, -351_714_000),
    },
    "2021-03-31": {
        "available_date": "2021-05-10", "revenue": 2_048_400_000,
        "profit": 1_128_100_000, "derivation": "direct_three_month_ifrs_statement",
        "current": ("0001564590-21-025757", "bntx-ex991_8.htm",
                    2_048_400_000, 1_128_100_000), "prior": None,
    },
    "2021-06-30": {
        "available_date": "2021-08-09", "revenue": 5_308_500_000,
        "profit": 2_787_200_000, "derivation": "direct_three_month_ifrs_statement",
        "current": ("0001564590-21-042360", "bntx-ex991_8.htm",
                    5_308_500_000, 2_787_200_000), "prior": None,
    },
    "2021-09-30": {
        "available_date": "2021-11-09", "revenue": 6_087_300_000,
        "profit": 3_211_000_000, "derivation": "direct_three_month_ifrs_statement",
        "current": ("0001776985-21-000009", "bnt_2021q3exhibit99x1.htm",
                    6_087_300_000, 3_211_000_000), "prior": None,
    },
    "2021-12-31": {
        "available_date": "2022-03-30", "revenue": 5_532_500_000,
        "profit": 3_166_200_000, "derivation": "contemporaneous_fy_minus_9m",
        "current": ("0001776985-22-000019", "bntx-20211231.htm",
                    18_976_700_000, 10_292_500_000),
        "prior": ("0001776985-21-000009", "bnt_2021q3exhibit99x1.htm",
                  13_444_200_000, 7_126_300_000),
    },
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_text(raw: bytes) -> str:
    return " ".join(BeautifulSoup(raw, "html.parser").get_text(" ").split())


def _value_tokens(value: int) -> set[str]:
    absolute = abs(value)
    tokens = {str(absolute), f"{absolute:,}"}
    if absolute % 1_000 == 0:
        thousands = absolute // 1_000
        tokens.update({str(thousands), f"{thousands:,}"})
    millions = absolute / 1_000_000
    tokens.update({
        f"{millions:.1f}", f"{millions:g}",
        f"{millions:,.1f}", f"{millions:,g}",
    })
    return tokens


def validate_filing(raw: bytes, *, expected_values: tuple[int, int]) -> None:
    text = _normalized_text(raw)
    if not re.search(r"BioNTech\s+SE|BioNTech", text, re.I):
        raise ValueError("BNTX issuer identity is not proven")
    if not re.search(r"(?:thousands|millions).*euros|EUR|€", text, re.I):
        raise ValueError("BNTX EUR reporting currency is not proven")
    for value in expected_values:
        if not any(token in text for token in _value_tokens(value)):
            raise ValueError(f"BNTX filing does not prove expected value {value}")


def _row(*, fiscal_end: str, available_date: str, metric: str, value: int,
         accession: str, archive: str, archive_sha256: str,
         derivation: str, prior_accession: str) -> dict:
    return {
        "ticker": "BNTX", "fiscal_end": fiscal_end,
        "available_date": available_date, "metric": metric,
        "value": float(value), "taxonomy": "ifrs-full",
        "concept": (
            "RevenueFromContractsWithCustomers"
            if metric == "revenue" else "ProfitLoss"
        ),
        "form": "20-F" if "20f" in archive.lower() or "20-f" in archive.lower() else "6-K",
        "accession": accession, "unit": "EUR",
        "source": "sec_bntx_contemporaneous_ifrs_reporting_chain",
        "source_archive": archive, "source_archive_sha256": archive_sha256,
        "derivation": derivation,
        "derivation_prior_accession": prior_accession,
    }


def run(*, output_dir: Path = DEFAULT_OUTPUT_DIR) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(exist_ok=True)
    sources: dict[tuple[str, str], dict] = {}
    for item in PERIOD_EVIDENCE.values():
        for evidence in (item["current"], item["prior"]):
            if evidence is None:
                continue
            accession, document, revenue, profit = evidence
            key = (accession, document)
            if key in sources:
                continue
            url = f"{SEC_BASE}/{accession.replace('-', '')}/{document}"
            path = raw_dir / f"{accession}_{document}"
            if not path.exists():
                with urlopen(Request(url, headers=HEADERS), timeout=120) as response:
                    path.write_bytes(response.read())
            validate_filing(path.read_bytes(), expected_values=(revenue, profit))
            sources[key] = {
                "accession": accession, "document": document, "url": url,
                "path": str(path), "sha256": _sha256(path),
            }

    rows = []
    recovered = []
    for fiscal_end, item in PERIOD_EVIDENCE.items():
        current = item["current"]
        prior = item["prior"]
        if prior is not None:
            if current[2] - prior[2] != item["revenue"]:
                raise RuntimeError(f"BNTX revenue derivation mismatch for {fiscal_end}")
            if current[3] - prior[3] != item["profit"]:
                raise RuntimeError(f"BNTX profit derivation mismatch for {fiscal_end}")
        source = sources[(current[0], current[1])]
        common = {
            "fiscal_end": fiscal_end, "available_date": item["available_date"],
            "accession": current[0], "archive": Path(source["path"]).name,
            "archive_sha256": source["sha256"],
            "derivation": item["derivation"],
            "prior_accession": prior[0] if prior is not None else "",
        }
        rows.extend([
            _row(metric="revenue", value=item["revenue"], **common),
            _row(metric="net_income", value=item["profit"], **common),
        ])
        recovered.append({
            "ticker": "BNTX", "fiscal_end": fiscal_end,
            "available_date": item["available_date"],
            "revenue": float(item["revenue"]),
            "net_income": float(item["profit"]),
            "derivation": item["derivation"],
        })

    facts = pd.DataFrame(rows).sort_values(["fiscal_end", "metric"])
    if len(facts) != 24 or facts["fiscal_end"].nunique() != 12:
        raise RuntimeError("BNTX recovery is not exactly twelve paired quarters")
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1, "research_only": True,
        "point_in_time_proven": True, "parameters_frozen": False,
        "promotion_eligible": False, "release_status": "BLOCKED",
        "ticker": "BNTX", "accepted_quarter_count": 12,
        "recovered_quarters": recovered,
        "filing_sources": list(sources.values()),
        "outputs": {"quarters": {"path": str(facts_path),
                                  "sha256": _sha256(facts_path)}},
        "guardrail": (
            "Only reported IFRS total revenue and profit or loss for the period "
            "are accepted. Direct three-month values or exact annual-minus-nine-"
            "month differences from the original filing chain are used. Explicit "
            "comparative quarter columns retain their later SEC filing dates; they "
            "are never backdated. Forecasts and adjusted measures are rejected."
        ),
    }
    manifest = output_dir / "manifest.json"
    manifest.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["manifest"] = str(manifest)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = run(output_dir=args.output_dir)
    print(json.dumps({
        "accepted_quarter_count": result["accepted_quarter_count"],
        "manifest": result["manifest"],
        "release_status": result["release_status"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
