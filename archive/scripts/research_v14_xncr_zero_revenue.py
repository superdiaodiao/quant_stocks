#!/usr/bin/env python3
"""Restore XNCR's explicitly reported zero-revenue 2018 quarters."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import urllib.request
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.io.fundamentals_update import OUTPUT_COLUMNS


CIK = 1_326_732
RAW_PATH = Path(
    "cleaned_stocks_data/financial/sec_companyfacts_cache/CIK0001326732.json.gz"
)
RAW_SHA256 = "6a589142bdc4cb9867e2bca1af599b895a57d3d3ef976a666da7d461b1d93427"
OUTPUT_DIR = Path("output/research_only/v14/xncr_zero_revenue")
SOURCES = {
    "2018-03-31": {
        "url": "https://www.sec.gov/Archives/edgar/data/1326732/000155837018004166/xncr-20180331x10q.htm",
        "sha256": "3b7bff8e3347af51ee6706223d9166ba3498dbf08b1ed1d9b57fe79a098996a2",
        "filed": "2018-05-07", "accession": "0001558370-18-004166",
        "filename": "xncr-20180331x10q.htm", "comparative": "3.5",
    },
    "2018-06-30": {
        "url": "https://www.sec.gov/Archives/edgar/data/1326732/000155837018006482/xncr-20180630x10q.htm",
        "sha256": "9de24cff2a3256125b3e013f638960d5d85b71a9af113a1688f8f5cc3e2d5711",
        "filed": "2018-08-07", "accession": "0001558370-18-006482",
        "filename": "xncr-20180630x10q.htm", "comparative": "12.5",
    },
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _download(url: str, path: Path, expected_sha256: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        request = urllib.request.Request(
            url, headers={"User-Agent": "quant-research contact@example.com"}
        )
        path.write_bytes(urllib.request.urlopen(request, timeout=120).read())
    if _sha256(path) != expected_sha256:
        raise RuntimeError(f"source SHA256 changed: {path}")


def require_explicit_zero_revenue(html: bytes, comparative: str) -> float:
    text = " ".join(BeautifulSoup(html, "html.parser").stripped_strings)
    normalized = re.sub(r"[\s,$]", "", text)
    table_row = f"Totalrevenues—{comparative}({comparative})"
    explanation = f"Revenueswerelowerby{comparative}million"
    if normalized.count(table_row) != 1 or normalized.count(explanation) != 1:
        raise RuntimeError("XNCR zero-revenue disclosure changed or is not unique")
    return 0.0


def q4_revenue_fact(payload: dict) -> dict:
    facts = payload.get("payload", payload)["facts"]["us-gaap"]
    rows = facts["RevenueFromContractWithCustomerExcludingAssessedTax"]["units"]["USD"]
    matches = [row for row in rows if (
        row.get("start") == "2018-10-01"
        and row.get("end") == "2018-12-31"
        and row.get("val") == 11_564_000
        and row.get("filed") == "2019-02-26"
        and row.get("accn") == "0001558370-19-001049"
        and row.get("form") == "10-K"
    )]
    if len(matches) != 1:
        raise RuntimeError("XNCR 2018Q4 Company Facts evidence changed")
    return matches[0]


def recover(
    raw_path: Path = RAW_PATH, output_dir: Path = OUTPUT_DIR,
) -> dict:
    raw_path, output_dir = Path(raw_path), Path(output_dir)
    if _sha256(raw_path) != RAW_SHA256:
        raise RuntimeError("XNCR raw Company Facts SHA256 changed")
    output_dir.mkdir(parents=True, exist_ok=True)
    records, bindings = [], []
    fetched_at = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    for fiscal_end, source in SOURCES.items():
        path = output_dir / source["filename"]
        _download(source["url"], path, source["sha256"])
        value = require_explicit_zero_revenue(
            path.read_bytes(), source["comparative"]
        )
        records.append({
            "ticker": "XNCR", "fiscal_end": fiscal_end,
            "available_date": source["filed"], "metric": "revenue",
            "value": value, "taxonomy": "us-gaap",
            "concept": "ExplicitZeroTotalRevenues", "form": "10-Q",
            "accession": source["accession"], "fetched_at": fetched_at,
        })
        bindings.append({
            "path": str(path), "url": source["url"],
            "sha256": _sha256(path), "filed": source["filed"],
            "accession": source["accession"],
        })
    with gzip.open(raw_path, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    q4 = q4_revenue_fact(payload)
    records.append({
        "ticker": "XNCR", "fiscal_end": q4["end"],
        "available_date": q4["filed"], "metric": "revenue",
        "value": float(q4["val"]), "taxonomy": "us-gaap",
        "concept": "RevenueFromContractWithCustomerExcludingAssessedTax",
        "form": q4["form"], "accession": q4["accn"],
        "fetched_at": fetched_at,
    })
    bindings.append({
        "path": str(raw_path), "sha256": _sha256(raw_path),
        "source_url": payload.get("source_url"), "filed": q4["filed"],
        "accession": q4["accn"],
    })
    facts = pd.DataFrame(records, columns=OUTPUT_COLUMNS)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1, "research_only": True,
        "point_in_time_proven": True, "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED", "promotion_eligible": False,
        "formal_financials_modified": False, "ticker": "XNCR", "cik": CIK,
        "accepted_quarter_count": 3, "accepted_fact_count": len(facts),
        "sources": bindings,
        "outputs": {"strict_quarterly_facts": {
            "path": str(facts_path), "sha256": _sha256(facts_path),
        }},
        "guardrail": (
            "2018Q1 and Q2 are accepted only because each contemporaneous "
            "10-Q explicitly reports total revenue as zero; no cumulative "
            "zero is allocated between quarters. Q4 is the exact duration "
            "fact filed in the contemporaneous 2018 10-K."
        ),
    }
    manifest = output_dir / "manifest.json"
    manifest.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["manifest"] = str(manifest)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-path", type=Path, default=RAW_PATH)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--base-dir", type=Path)
    parser.add_argument("--candidate-output-dir", type=Path)
    args = parser.parse_args()
    if (args.base_dir is None) != (args.candidate_output_dir is None):
        parser.error("--base-dir and --candidate-output-dir must be used together")
    report = recover(args.raw_path, args.output_dir)
    if args.base_dir is not None:
        report["candidate"] = integrate_candidate(
            base_dir=args.base_dir, supplement_dir=args.output_dir,
            output_dir=args.candidate_output_dir,
        )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
