#!/usr/bin/env python3
"""Recover PNTG's eight quarters from its first contemporaneous 10-K."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import urllib.request
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.io.fundamentals_update import OUTPUT_COLUMNS


SOURCE_URL = (
    "https://www.sec.gov/Archives/edgar/data/1766400/"
    "000176640020000034/pntg-20191231.htm"
)
SOURCE_SHA256 = "97e3cd100ade38b9644a55417a0b8380176f0fcb3e4fdf406a910d86592895f8"
ACCESSION = "0001766400-20-000034"
AVAILABLE_DATE = "2020-03-04"
CIK = 1_766_400
OUTPUT_DIR = Path("output/research_only/v14/pntg_selected_quarters")
SOURCE_PATH = OUTPUT_DIR / "pntg-20191231.htm"
EXPECTED = {
    "2018-03-31": (67_979_000.0, 3_381_000.0),
    "2018-06-30": (69_789_000.0, 4_161_000.0),
    "2018-09-30": (72_953_000.0, 4_372_000.0),
    "2018-12-31": (75_337_000.0, 3_770_000.0),
    "2019-03-31": (77_907_000.0, 1_334_000.0),
    "2019-06-30": (82_734_000.0, 3_487_000.0),
    "2019-09-30": (88_398_000.0, 1_524_000.0),
    "2019-12-31": (89_492_000.0, -3_799_000.0),
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _download(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        request = urllib.request.Request(
            SOURCE_URL, headers={"User-Agent": "quant-research contact@example.com"}
        )
        path.write_bytes(urllib.request.urlopen(request, timeout=120).read())
    if _sha256(path) != SOURCE_SHA256:
        raise RuntimeError("PNTG 2019 10-K source SHA256 changed")


def strict_selected_quarters(html: bytes) -> dict[str, tuple[float, float]]:
    text = " ".join(BeautifulSoup(html, "html.parser").stripped_strings)
    header = (
        "Dec. 31, 2019 Sept. 30, 2019 June 30, 2019 Mar. 31, 2019 "
        "Dec. 31, 2018 Sept. 30, 2018 June 30, 2018 Mar. 31, 2018"
    )
    if text.count(header) != 1:
        raise RuntimeError("PNTG selected-quarter header is not unique")
    section = text.split(header, 1)[1]
    section = section.split("The summation of quarterly per share information", 1)[0]
    normalized = re.sub(r"[\s,$]", "", section)
    required_rows = (
        "(Inthousandsexceptpersharedata)",
        "Revenues8949288398827347790775337729536978967979",
        "CostofServices6888868286630385872956313541675186050081",
        "TotalExpenses9088786472794227608070621671506425663400",
        "Netincome(loss)(3799)1803368714843952441544423470",
        "Incomeattributabletononcontrollinginterests—2792001501824328189",
        "Netincome(loss)attributabletoThePennantGroupInc.(3799)1524348713343770437241613381",
    )
    if any(row not in normalized for row in required_rows):
        raise RuntimeError("PNTG selected-quarter values or ordering changed")
    return dict(EXPECTED)


def recover(source_path: Path = SOURCE_PATH, output_dir: Path = OUTPUT_DIR) -> dict:
    source_path, output_dir = Path(source_path), Path(output_dir)
    _download(source_path)
    selected = strict_selected_quarters(source_path.read_bytes())
    fetched_at = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    records = []
    for fiscal_end, (revenue, net_income) in selected.items():
        for metric, value, concept in (
            ("revenue", revenue, "SelectedQuarterlyRevenues"),
            (
                "net_income", net_income,
                "SelectedQuarterlyNetIncomeLossAttributableToPennant",
            ),
        ):
            records.append({
                "ticker": "PNTG", "fiscal_end": fiscal_end,
                "available_date": AVAILABLE_DATE, "metric": metric,
                "value": value, "taxonomy": "us-gaap", "concept": concept,
                "form": "10-K", "accession": ACCESSION,
                "fetched_at": fetched_at,
            })
    facts = pd.DataFrame(records, columns=OUTPUT_COLUMNS)
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1, "research_only": True,
        "point_in_time_proven": True, "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED", "promotion_eligible": False,
        "formal_financials_modified": False, "ticker": "PNTG", "cik": CIK,
        "accepted_quarter_count": len(selected), "accepted_fact_count": len(facts),
        "source": {"path": str(source_path), "url": SOURCE_URL,
                   "sha256": _sha256(source_path), "filed": AVAILABLE_DATE,
                   "accession": ACCESSION},
        "outputs": {"strict_quarterly_facts": {
            "path": str(facts_path), "sha256": _sha256(facts_path),
        }},
        "guardrail": (
            "Only the eight quarters explicitly presented together in the "
            "10-K filed on 2020-03-04 are accepted. Net income attributable "
            "to Pennant is used to match the existing Company Facts series. "
            "No pre-filing availability date or later revision is used."
        ),
    }
    manifest = output_dir / "manifest.json"
    manifest.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["manifest"] = str(manifest)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE_PATH)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--base-dir", type=Path)
    parser.add_argument("--candidate-output-dir", type=Path)
    args = parser.parse_args()
    if (args.base_dir is None) != (args.candidate_output_dir is None):
        parser.error("--base-dir and --candidate-output-dir must be used together")
    report = recover(args.source, args.output_dir)
    if args.base_dir is not None:
        report["candidate"] = integrate_candidate(
            base_dir=args.base_dir, supplement_dir=args.output_dir,
            output_dir=args.candidate_output_dir,
        )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
