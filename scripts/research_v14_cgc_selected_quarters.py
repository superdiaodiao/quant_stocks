#!/usr/bin/env python3
"""Recover CGC's eight contemporaneously published US-GAAP quarters."""

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
    "https://www.sec.gov/Archives/edgar/data/1737927/"
    "000156459020027897/cgc-10k_20200331.htm"
)
SOURCE_SHA256 = "50d73a96b61700d89c87fdadf1d009567e09c06264dc5cff0bb57fb1b8137ddf"
ACCESSION = "0001564590-20-027897"
AVAILABLE_DATE = "2020-06-01"
CIK = 1_737_927
OUTPUT_DIR = Path("output/research_only/v14/cgc_selected_quarters")
SOURCE_PATH = OUTPUT_DIR / "cgc-10k_20200331.htm"
EXPECTED = {
    "2018-06-30": (25_916_000.0, -93_299_000.0),
    "2018-09-30": (23_327_000.0, -310_428_000.0),
    "2018-12-31": (83_048_000.0, 39_194_000.0),
    "2019-03-31": (94_050_000.0, -347_492_000.0),
    "2019-06-30": (90_482_000.0, -194_051_000.0),
    "2019-09-30": (76_613_000.0, 242_650_000.0),
    "2019-12-31": (123_764_000.0, -109_634_000.0),
    "2020-03-31": (107_913_000.0, -1_326_405_000.0),
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _download(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        request = urllib.request.Request(
            SOURCE_URL, headers={"User-Agent": "quant-research contact@example.com"}
        )
        path.write_bytes(urllib.request.urlopen(request).read())
    if _sha256(path) != SOURCE_SHA256:
        raise RuntimeError("CGC 2020 10-K source SHA256 changed")


def strict_selected_quarters(html: bytes) -> dict[str, tuple[float, float]]:
    text = " ".join(BeautifulSoup(html, "html.parser").stripped_strings)
    marker = "The following tables present our unaudited quarterly results of operations for the eight consecutive quarters ended March 31, 2020:"
    if text.count(marker) != 1:
        raise RuntimeError("CGC selected-quarter disclosure is not unique")
    section = text.split(marker, 1)[1]
    section = section.split("Critical Accounting Policies and Estimates", 1)[0]
    normalized = re.sub(r"[\s,$]", "", section)
    required_rows = (
        "QUARTERENDEDJune30September30December31March312019201920192020Fullyear",
        "Netrevenue9048276613123764107913398772",
        "Grossmargin18290364338208(91825)(31684)",
        "Net(loss)income(194051)242650(109634)(1326405)(1387440)",
        "Net(loss)incomeattributabletoCanopyGrowthCorporation(185869)258918(91354)(1303021)(1321326)",
        "QUARTERENDEDJune30September30December31March312018201820182019Fullyear",
        "Netrevenue25916233278304894050226341",
        "Grossmargin7464(19336)190722104528245",
        "Net(loss)income(93299)(310428)39194(347492)(712025)",
        "Net(loss)incomeattributabletoCanopyGrowthCorporation(89671)(317830)50736(379516)(736281)",
    )
    if any(row not in normalized for row in required_rows):
        raise RuntimeError("CGC selected-quarter values or ordering changed")
    return dict(EXPECTED)


def recover(source_path: Path = SOURCE_PATH, output_dir: Path = OUTPUT_DIR) -> dict:
    source_path = Path(source_path)
    output_dir = Path(output_dir)
    _download(source_path)
    selected = strict_selected_quarters(source_path.read_bytes())
    fetched_at = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    records = []
    for fiscal_end, (revenue, net_income) in selected.items():
        for metric, value, concept in (
            ("revenue", revenue, "SelectedQuarterlyNetRevenue"),
            ("net_income", net_income, "SelectedQuarterlyNetIncomeLoss"),
        ):
            records.append({
                "ticker": "CGC", "fiscal_end": fiscal_end,
                "available_date": AVAILABLE_DATE, "metric": metric,
                "value": value, "taxonomy": "us-gaap", "concept": concept,
                "form": "10-K", "accession": ACCESSION,
                "fetched_at": fetched_at,
            })
    rows = pd.DataFrame(records, columns=OUTPUT_COLUMNS)
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    rows.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1, "research_only": True,
        "point_in_time_proven": True, "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED", "promotion_eligible": False,
        "formal_financials_modified": False, "ticker": "CGC", "cik": CIK,
        "accepted_quarter_count": len(selected), "accepted_fact_count": len(rows),
        "source": {"path": str(source_path), "url": SOURCE_URL,
                   "sha256": _sha256(source_path), "filed": AVAILABLE_DATE,
                   "accession": ACCESSION},
        "outputs": {"strict_quarterly_facts": {
            "path": str(facts_path), "sha256": _sha256(facts_path),
        }},
        "guardrail": (
            "Only the eight same-basis US-GAAP quarters in the selected-quarter "
            "table filed on 2020-06-01 are accepted. Earlier IFRS 6-K values, "
            "pro-forma measures, cumulative arithmetic and post-filing revisions "
            "are excluded. Values are Canadian dollars as reported."
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
