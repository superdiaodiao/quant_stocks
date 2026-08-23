#!/usr/bin/env python3
"""Recover strict GRFS quarters from contemporaneous SEC IFRS reports."""

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
    "output/research_only/v14/grfs_sec_quarterly_filings_2017q1_2020q4"
)
SEC_BASE = "https://www.sec.gov/Archives/edgar/data/1438569"
HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}

# Values are thousands of EUR. Q1/Q2 are the explicit six- and three-month
# columns in the same interim filing. Q3/Q4 are exact differences between
# adjacent, contemporaneous cumulative filings in the original reporting chain.
PERIOD_EVIDENCE = {
    "2017-03-31": {
        "available_date": "2017-07-28", "revenue": 1_061_680,
        "profit": 133_735, "derivation": "same_filing_h1_minus_direct_q2",
        "current": ("0001104659-17-047589", "a17-18519_16k.htm",
                    2_192_447, 277_370),
        "prior": ("0001104659-17-047589", "a17-18519_16k.htm",
                  1_130_767, 143_635),
    },
    "2017-06-30": {
        "available_date": "2017-07-28", "revenue": 1_130_767,
        "profit": 143_635, "derivation": "direct_three_month_statement",
        "current": ("0001104659-17-047589", "a17-18519_16k.htm",
                    1_130_767, 143_635), "prior": None,
    },
    "2017-09-30": {
        "available_date": "2017-11-02", "revenue": 1_057_736,
        "profit": 153_360, "derivation": "contemporaneous_9m_minus_h1",
        "current": ("0001104659-17-065382", "a17-24928_16k.htm",
                    3_250_183, 430_730),
        "prior": ("0001104659-17-047589", "a17-18519_16k.htm",
                  2_192_447, 277_370),
    },
    "2017-12-31": {
        "available_date": "2018-02-28", "revenue": 1_067_890,
        "profit": 230_584, "derivation": "contemporaneous_fy_minus_9m",
        "current": ("0001104659-18-013386", "a18-7196_16k.htm",
                    4_318_073, 661_314),
        "prior": ("0001104659-17-065382", "a17-24928_16k.htm",
                  3_250_183, 430_730),
    },
    "2018-03-31": {
        "available_date": "2018-07-27", "revenue": 1_023_012,
        "profit": 142_871, "derivation": "same_filing_h1_minus_direct_q2",
        "current": ("0001104659-18-047551", "a18-17766_16k.htm",
                    2_120_118, 317_883),
        "prior": ("0001104659-18-047551", "a18-17766_16k.htm",
                  1_097_106, 175_012),
    },
    "2018-06-30": {
        "available_date": "2018-07-27", "revenue": 1_097_106,
        "profit": 175_012, "derivation": "direct_three_month_statement",
        "current": ("0001104659-18-047551", "a18-17766_16k.htm",
                    1_097_106, 175_012), "prior": None,
    },
    "2018-09-30": {
        "available_date": "2018-11-05", "revenue": 1_143_800,
        "profit": 148_606, "derivation": "contemporaneous_9m_minus_h1",
        "current": ("0001104659-18-065887", "a18-39503_16k.htm",
                    3_263_918, 466_489),
        "prior": ("0001104659-18-047551", "a18-17766_16k.htm",
                  2_120_118, 317_883),
    },
    "2018-12-31": {
        "available_date": "2019-02-28", "revenue": 1_222_806,
        "profit": 127_917, "derivation": "contemporaneous_fy_minus_9m",
        "current": ("0001104659-19-011270", "a19-5541_16k.htm",
                    4_486_724, 594_406),
        "prior": ("0001104659-18-065887", "a18-39503_16k.htm",
                  3_263_918, 466_489),
    },
    "2019-03-31": {
        "available_date": "2019-07-31", "revenue": 1_156_777,
        "profit": 114_282, "derivation": "same_filing_h1_minus_direct_q2",
        "current": ("0001104659-19-042789", "a19-16300_16k.htm",
                    2_423_360, 294_639),
        "prior": ("0001104659-19-042789", "a19-16300_16k.htm",
                  1_266_583, 180_357),
    },
    "2019-06-30": {
        "available_date": "2019-07-31", "revenue": 1_266_583,
        "profit": 180_357, "derivation": "direct_three_month_statement",
        "current": ("0001104659-19-042789", "a19-16300_16k.htm",
                    1_266_583, 180_357), "prior": None,
    },
    "2019-09-30": {
        "available_date": "2019-10-29", "revenue": 1_314_421,
        "profit": 151_763, "derivation": "contemporaneous_9m_minus_h1",
        "current": ("0001104659-19-057088", "a19-21288_16k.htm",
                    3_737_781, 446_402),
        "prior": ("0001104659-19-042789", "a19-16300_16k.htm",
                  2_423_360, 294_639),
    },
    "2019-12-31": {
        "available_date": "2020-02-27", "revenue": 1_360_910,
        "profit": 202_242, "derivation": "contemporaneous_fy_minus_9m",
        "current": ("0001104659-20-026130", "a20-11032_16k.htm",
                    5_098_691, 648_644),
        "prior": ("0001104659-19-057088", "a19-21288_16k.htm",
                  3_737_781, 446_402),
    },
    "2020-03-31": {
        "available_date": "2020-07-30", "revenue": 1_293_319,
        "profit": 203_540, "derivation": "same_filing_h1_minus_direct_q2",
        "current": ("0001104659-20-088132", "tm2026023-1_6k.htm",
                    2_677_341, 261_676),
        "prior": ("0001104659-20-088132", "tm2026023-1_6k.htm",
                  1_384_022, 58_136),
    },
    "2020-06-30": {
        "available_date": "2020-07-30", "revenue": 1_384_022,
        "profit": 58_136, "derivation": "direct_three_month_statement",
        "current": ("0001104659-20-088132", "tm2026023-1_6k.htm",
                    1_384_022, 58_136), "prior": None,
    },
    "2020-09-30": {
        "available_date": "2020-11-05", "revenue": 1_353_372,
        "profit": 285_884, "derivation": "contemporaneous_9m_minus_h1",
        "current": ("0001104659-20-121561", "tm2033871-2_6k.htm",
                    4_030_713, 547_560),
        "prior": ("0001104659-20-088132", "tm2026023-1_6k.htm",
                  2_677_341, 261_676),
    },
    "2020-12-31": {
        "available_date": "2021-02-26", "revenue": 1_309_325,
        "profit": 161_430, "derivation": "contemporaneous_fy_minus_9m",
        "current": ("0001104659-21-028616", "tm217998d1_6k.htm",
                    5_340_038, 708_990),
        "prior": ("0001104659-20-121561", "tm2033871-2_6k.htm",
                  4_030_713, 547_560),
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


def validate_filing(raw: bytes, *, expected_values: tuple[int, int]) -> None:
    text = _normalized_text(raw)
    if not re.search(r"Grifols,?\s+S\.A\.", text, re.I):
        raise ValueError("GRFS issuer identity is not proven")
    if not re.search(r"(?:thousands|millions) of (?:E|e)uros|EUR", text):
        raise ValueError("GRFS EUR reporting currency is not proven")
    for value in expected_values:
        tokens = {str(value), f"{value:,}", f"{value:,}".replace(",", ".")}
        if not any(token in text for token in tokens):
            raise ValueError(f"GRFS filing does not prove expected value {value}")


def _row(*, fiscal_end: str, available_date: str, metric: str, value: int,
         accession: str, archive: str, archive_sha256: str,
         derivation: str, prior_accession: str) -> dict:
    return {
        "ticker": "GRFS", "fiscal_end": fiscal_end,
        "available_date": available_date, "metric": metric,
        "value": float(value * 1_000), "taxonomy": "ifrs-full",
        "concept": "Revenue" if metric == "revenue" else "ProfitLoss",
        "form": "6-K", "accession": accession, "unit": "EUR",
        "source": "sec_grfs_contemporaneous_ifrs_reporting_chain",
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
                raise RuntimeError(f"GRFS revenue derivation mismatch for {fiscal_end}")
            if current[3] - prior[3] != item["profit"]:
                raise RuntimeError(f"GRFS profit derivation mismatch for {fiscal_end}")
        source = sources[(current[0], current[1])]
        prior_accession = prior[0] if prior is not None else ""
        common = {
            "fiscal_end": fiscal_end,
            "available_date": item["available_date"],
            "accession": current[0], "archive": Path(source["path"]).name,
            "archive_sha256": source["sha256"],
            "derivation": item["derivation"],
            "prior_accession": prior_accession,
        }
        rows.extend([
            _row(metric="revenue", value=item["revenue"], **common),
            _row(metric="net_income", value=item["profit"], **common),
        ])
        recovered.append({
            "ticker": "GRFS", "fiscal_end": fiscal_end,
            "available_date": item["available_date"],
            "revenue": float(item["revenue"] * 1_000),
            "net_income": float(item["profit"] * 1_000),
            "derivation": item["derivation"],
        })

    facts = pd.DataFrame(rows).sort_values(["fiscal_end", "metric"])
    expected_quarters = len(PERIOD_EVIDENCE)
    if (len(facts) != expected_quarters * 2
            or facts["fiscal_end"].nunique() != expected_quarters):
        raise RuntimeError(
            "GRFS recovery does not contain exactly one paired row set per "
            "evidence quarter"
        )
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1, "research_only": True,
        "point_in_time_proven": True, "parameters_frozen": False,
        "promotion_eligible": False, "release_status": "BLOCKED",
        "ticker": "GRFS", "accepted_quarter_count": expected_quarters,
        "recovered_quarters": recovered,
        "filing_sources": list(sources.values()),
        "outputs": {"quarters": {"path": str(facts_path),
                                  "sha256": _sha256(facts_path)}},
        "guardrail": (
            "Only reported consolidated IFRS revenue and consolidated profit "
            "are used. Direct three-month values or exact adjacent cumulative "
            "differences from contemporaneous filings are accepted. Later "
            "comparatives, adjusted profit, segment values, and ADR currency "
            "conversion are rejected. Image-only 2021 H1/9M disclosures remain "
            "unrecovered rather than being inferred."
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
