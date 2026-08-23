#!/usr/bin/env python3
"""Recover RPAY predecessor quarters from its contemporaneous 424B3 table."""

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
    "https://www.sec.gov/Archives/edgar/data/1720592/"
    "000121390019011288/f424b3_thunderbridgeacq.htm"
)
SOURCE_SHA256 = "afa2bd3ed65fff20b325526f5131ff9bdaa06c002dbdc878dd2993fc0d4ab353"
ACCESSION = "0001213900-19-011288"
AVAILABLE_DATE = "2019-06-24"
TRANSITION_URL = (
    "https://www.sec.gov/Archives/edgar/data/1720592/"
    "000156459019043526/rpay-10q_20190930.htm"
)
TRANSITION_SHA256 = "a620ee613a7819717c78e15f2fc8280d05d0168b28231c18e78354320f6a1876"
TRANSITION_ACCESSION = "0001564590-19-043526"
TRANSITION_AVAILABLE_DATE = "2019-11-14"
CIK = 1_720_592
OUTPUT_DIR = Path("output/research_only/v14/rpay_selected_quarters")
SOURCE_PATH = OUTPUT_DIR / "f424b3_thunderbridgeacq.htm"
TRANSITION_PATH = OUTPUT_DIR / "rpay-10q_20190930.htm"
EXPECTED = {
    "2017-06-30": (21_747_000.0, 1_632_000.0),
    "2017-09-30": (22_804_000.0, 368_000.0),
    "2017-12-31": (25_559_000.0, 4_431_000.0),
    "2018-03-31": (32_797_000.0, 181_000.0),
    "2018-06-30": (31_066_000.0, 4_484_000.0),
    "2018-09-30": (32_292_000.0, 3_727_000.0),
    "2018-12-31": (33_858_000.0, 2_145_000.0),
    "2019-03-31": (39_249_000.0, 4_864_000.0),
}
TRANSITION_EXPECTED = {
    "2019-06-30": (36_234_000.0, 4_156_000.0),
    "2019-09-30": (41_063_000.0, -41_244_000.0),
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _download(path: Path, url: str, expected_sha256: str, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "quant-research contact@example.com"},
        )
        path.write_bytes(urllib.request.urlopen(request, timeout=120).read())
    if _sha256(path) != expected_sha256:
        raise RuntimeError(f"RPAY {label} source SHA256 changed")


def strict_selected_quarters(html: bytes) -> dict[str, tuple[float, float]]:
    text = re.sub(
        r"\s+", " ", " ".join(BeautifulSoup(html, "html.parser").stripped_strings)
    )
    intro = "Selected Quarterly Results of Operations"
    if text.count(intro) != 1:
        raise RuntimeError("RPAY selected-quarter section is not unique")
    section = text.split(intro, 1)[1].split("Seasonality", 1)[0]
    header = (
        "Three Months Ended March 31, 2019 December 31, 2018 "
        "September 30, 2018 June 30, 2018 March 31, 2018 "
        "December 31, 2017 September 30, 2017 June 30, 2017"
    )
    if section.count(header) != 1:
        raise RuntimeError("RPAY selected-quarter header is not unique")
    normalized = re.sub(r"[\s,$]", "", section)
    required = (
        "lasteightquarters",
        "Totalrevenue3924933858322923106632797255592280421747",
        "Netincome486421453727448418144313681632",
    )
    if any(value not in normalized for value in required):
        raise RuntimeError("RPAY selected-quarter values or ordering changed")
    return dict(EXPECTED)


def strict_transition_quarters(
    html: bytes, selected: dict[str, tuple[float, float]]
) -> dict[str, tuple[float, float]]:
    text = re.sub(
        r"\s+", " ", " ".join(BeautifulSoup(html, "html.parser").stripped_strings)
    )
    header = (
        "Successor Predecessor (in $ thousands) July 11, 2019 through "
        "September 30, 2019 July 1, 2019 through July 10, 2019 "
        "January 1, 2019 through July 10, 2019 Three Months Ended "
        "September 30, 2018 Nine Months Ended September 30, 2018"
    )
    if text.count(header) != 1:
        raise RuntimeError("RPAY transition-results header is not unique")
    section = text.split(header, 1)[1].split(
        "Three Months Ended September 30, 2019 Compared", 1
    )[0]
    normalized = re.sub(r"[\s,$]", "", section)
    required = (
        "TotalRevenue371563907793903229296155",
        "Netincome(loss)attributabletotheCompany(8481)(32763)(23743)37278392",
    )
    if any(value not in normalized for value in required):
        raise RuntimeError("RPAY transition-quarter values or ordering changed")

    q1_revenue, q1_net_income = selected["2019-03-31"]
    q2_revenue = (79_390_000.0 - q1_revenue) - 3_907_000.0
    q2_net_income = (-23_743_000.0 - q1_net_income) - (-32_763_000.0)
    q3_revenue = 37_156_000.0 + 3_907_000.0
    q3_net_income = -8_481_000.0 + -32_763_000.0
    observed = {
        "2019-06-30": (q2_revenue, q2_net_income),
        "2019-09-30": (q3_revenue, q3_net_income),
    }
    if observed != TRANSITION_EXPECTED:
        raise RuntimeError("RPAY transition-quarter derivation changed")
    return observed


def recover(
    source_path: Path = SOURCE_PATH,
    transition_path: Path = TRANSITION_PATH,
    output_dir: Path = OUTPUT_DIR,
) -> dict:
    source_path, transition_path, output_dir = (
        Path(source_path),
        Path(transition_path),
        Path(output_dir),
    )
    _download(source_path, SOURCE_URL, SOURCE_SHA256, "424B3")
    _download(
        transition_path,
        TRANSITION_URL,
        TRANSITION_SHA256,
        "2019 Q3 10-Q",
    )
    selected = strict_selected_quarters(source_path.read_bytes())
    transition = strict_transition_quarters(
        transition_path.read_bytes(), selected
    )
    quarters = {**selected, **transition}
    fetched_at = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    records = []
    for fiscal_end, (revenue, net_income) in quarters.items():
        is_transition = fiscal_end in transition
        for metric, value, concept in (
            ("revenue", revenue, "SelectedQuarterlyTotalRevenue"),
            ("net_income", net_income, "SelectedQuarterlyNetIncome"),
        ):
            records.append(
                {
                    "ticker": "RPAY",
                    "fiscal_end": fiscal_end,
                    "available_date": (
                        TRANSITION_AVAILABLE_DATE if is_transition
                        else AVAILABLE_DATE
                    ),
                    "metric": metric,
                    "value": value,
                    "taxonomy": "us-gaap",
                    "concept": (
                        f"derived_predecessor_successor:{concept}"
                        if is_transition else concept
                    ),
                    "form": "10-Q" if is_transition else "424B3",
                    "accession": (
                        TRANSITION_ACCESSION if is_transition else ACCESSION
                    ),
                    "fetched_at": fetched_at,
                }
            )
    facts = pd.DataFrame(records, columns=OUTPUT_COLUMNS).sort_values(
        ["fiscal_end", "metric"]
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "ticker": "RPAY",
        "cik": CIK,
        "accepted_quarter_count": len(quarters),
        "accepted_fact_count": len(facts),
        "sources": [
            {
                "path": str(source_path),
                "url": SOURCE_URL,
                "sha256": _sha256(source_path),
                "filed": AVAILABLE_DATE,
                "form": "424B3",
                "accession": ACCESSION,
            },
            {
                "path": str(transition_path),
                "url": TRANSITION_URL,
                "sha256": _sha256(transition_path),
                "filed": TRANSITION_AVAILABLE_DATE,
                "form": "10-Q",
                "accession": TRANSITION_ACCESSION,
            },
        ],
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path),
                "sha256": _sha256(facts_path),
            }
        },
        "guardrail": (
            "Eight Repay predecessor quarters come from the 2019-06-24 "
            "424B3. 2019 Q2 subtracts exact presented Q1 and July 1-10 values "
            "from the predecessor year-to-July-10 column; 2019 Q3 adds the "
            "predecessor July 1-10 and successor July 11-September 30 columns "
            "from the 2019-11-14 10-Q. Pro forma data and Thunder Bridge "
            "shell-company facts are excluded."
        ),
    }
    manifest = output_dir / "manifest.json"
    manifest.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["manifest"] = str(manifest)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE_PATH)
    parser.add_argument("--transition-source", type=Path, default=TRANSITION_PATH)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--base-dir", type=Path)
    parser.add_argument("--candidate-output-dir", type=Path)
    args = parser.parse_args()
    if (args.base_dir is None) != (args.candidate_output_dir is None):
        parser.error("--base-dir and --candidate-output-dir must be used together")
    report = recover(args.source, args.transition_source, args.output_dir)
    if args.base_dir is not None:
        report["candidate"] = integrate_candidate(
            base_dir=args.base_dir,
            supplement_dir=args.output_dir,
            output_dir=args.candidate_output_dir,
        )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
