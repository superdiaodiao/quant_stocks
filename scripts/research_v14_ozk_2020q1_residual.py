#!/usr/bin/env python3
"""Recover Bank OZK 2020Q1 from its contemporaneous Q2/YTD release."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from scripts.research_v14_cybr_quarterly_reports import _period_columns, _row_value
from scripts.research_v14_ozk_quarterly_reports import _parse_quarter
from src.io.fundamentals_update import OUTPUT_COLUMNS


SOURCE = Path(
    "output/research_only/v14/ozk_ir_quarterly_reports_2018_2021/raw/"
    "20211023004645_bank-ozk-announces-second-quarter-2020-earnings.html"
)
SOURCE_MANIFEST = Path(
    "output/research_only/v14/ozk_ir_quarterly_reports_2018_2021/manifest.json"
)
OUTPUT_DIR = Path("output/research_only/v14/ozk_2020q1_residual")
AVAILABLE_DATE = pd.Timestamp("2020-07-23")
EXPECTED_Q2 = {"revenue": 238_184_000.0, "net_income": 50_257_000.0}
EXPECTED_YTD = {"revenue": 475_638_000.0, "net_income": 62_115_000.0}
EXPECTED_Q1 = {"revenue": 237_454_000.0, "net_income": 11_858_000.0}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse_ytd(path: Path) -> dict[str, float]:
    candidates = []
    for table in pd.read_html(path):
        try:
            columns = _period_columns(
                table,
                fiscal_end=pd.Timestamp("2020-06-30"),
                period_phrase="Six months ended",
            )
            _, net_interest = _row_value(
                table, labels=("Net interest income",), columns=columns
            )
            _, noninterest = _row_value(
                table,
                labels=(
                    "Total non-interest income",
                    "Total noninterest income",
                    "Non-interest income",
                ),
                columns=columns,
            )
            _, net_income = _row_value(
                table, labels=("Net income",), columns=columns
            )
        except ValueError:
            continue
        if net_interest > 100_000:
            candidates.append({
                "revenue": (net_interest + noninterest) * 1_000.0,
                "net_income": net_income * 1_000.0,
            })
    unique = {tuple(sorted(item.items())) for item in candidates}
    if unique != {tuple(sorted(EXPECTED_YTD.items()))}:
        raise ValueError(f"OZK 2020H1 values changed: {candidates}")
    return EXPECTED_YTD


def run(
    source_path: Path = SOURCE,
    source_manifest_path: Path = SOURCE_MANIFEST,
    output_dir: Path = OUTPUT_DIR,
) -> dict:
    source_path = Path(source_path)
    source_manifest_path = Path(source_manifest_path)
    manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    matching = [
        row for row in manifest["filings"]
        if row["fiscal_end"] == "2020-06-30"
        and row["available_date"] == "2020-07-23"
    ]
    if len(matching) != 1 or matching[0]["sha256"] != _sha256(source_path):
        raise RuntimeError("OZK Q2 source is not bound to its original manifest")
    direct = _parse_quarter(source_path, pd.Timestamp("2020-06-30"))
    q2 = {metric: direct[metric] for metric in ("revenue", "net_income")}
    if q2 != EXPECTED_Q2:
        raise RuntimeError(f"OZK 2020Q2 direct facts changed: {q2}")
    ytd = _parse_ytd(source_path)
    q1 = {metric: ytd[metric] - q2[metric] for metric in ytd}
    if q1 != EXPECTED_Q1:
        raise RuntimeError(f"OZK 2020Q1 residual changed: {q1}")
    records = []
    for metric, value in q1.items():
        records.append({
            "ticker": "OZK", "fiscal_end": "2020-03-31",
            "available_date": AVAILABLE_DATE, "metric": metric, "value": value,
            "taxonomy": "issuer_gaap",
            "concept": f"derived_2020H1_minus_direct_2020Q2:{metric}",
            "form": "ISSUER_QUARTERLY_EARNINGS_RELEASE_RESIDUAL",
            "accession": "ozk-ir-20200723-20200331-residual",
            "fetched_at": pd.Timestamp("2026-08-12"),
        })
    facts = pd.DataFrame(records, columns=OUTPUT_COLUMNS)
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1, "research_only": True, "ticker": "OZK",
        "point_in_time_proven": True, "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED", "promotion_eligible": False,
        "accepted_quarter_count": 1, "fact_count": 2,
        "available_date": str(AVAILABLE_DATE.date()),
        "q2_direct": q2, "h1_ytd": ytd, "q1_residual": q1,
        "sources": [{
            "path": str(source_path), "sha256": _sha256(source_path),
            "source_manifest": str(source_manifest_path),
            "source_manifest_sha256": _sha256(source_manifest_path),
            "archive_timestamp": matching[0]["archive_timestamp"],
            "source_url": matching[0]["source_url"],
        }],
        "outputs": {"strict_quarterly_facts": {
            "path": str(facts_path), "sha256": _sha256(facts_path)
        }},
        "guardrail": (
            "2020Q1 is 2020H1 YTD minus the direct 2020Q2 values from the same "
            "2020-07-23 archived issuer release. It is unavailable before that "
            "date. The contemporaneous $237.454M revenue is retained instead "
            "of the later $237.455M comparative revision."
        ),
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    report["manifest"] = str(manifest_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-path", type=Path, default=SOURCE)
    parser.add_argument("--source-manifest-path", type=Path, default=SOURCE_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--base-dir", type=Path)
    parser.add_argument("--candidate-output-dir", type=Path)
    args = parser.parse_args()
    report = run(args.source_path, args.source_manifest_path, args.output_dir)
    if (args.base_dir is None) != (args.candidate_output_dir is None):
        parser.error("--base-dir and --candidate-output-dir must be used together")
    if args.base_dir is not None:
        report["candidate"] = integrate_candidate(
            base_dir=args.base_dir,
            supplement_dir=args.output_dir,
            output_dir=args.candidate_output_dir,
        )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
