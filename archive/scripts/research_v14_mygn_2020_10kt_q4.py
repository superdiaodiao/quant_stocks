#!/usr/bin/env python3
"""Recover MYGN 2020Q4 from its contemporaneous transition 10-KT."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.io.fundamentals_update import OUTPUT_COLUMNS


CACHE = Path("output/research_only/v14/companyfacts_cache/CIK0000899923.json.gz")
OUTPUT_DIR = Path("output/research_only/v14/mygn_2020_10kt_q4")
ACCESSION = "0000899923-21-000021"
FILED = "2021-03-16"
START = "2020-10-01"
END = "2020-12-31"
EXPECTED = {
    "revenue": {
        "concept": "RevenueFromContractWithCustomerExcludingAssessedTax",
        "value": 154_600_000.0,
    },
    "net_income": {"concept": "NetIncomeLoss", "value": -37_900_000.0},
}
HALF_YEAR_EXPECTED = {
    "revenue": 299_800_000.0,
    "net_income": -53_100_000.0,
}
Q3_EXPECTED = {
    "revenue": 145_200_000.0,
    "net_income": -15_200_000.0,
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fact(payload: dict, concept: str, start: str, end: str) -> float:
    values = payload["facts"]["us-gaap"][concept]["units"]["USD"]
    matches = {
        float(value["val"])
        for value in values
        if value.get("start") == start
        and value.get("end") == end
        and value.get("accn") == ACCESSION
        and value.get("filed") == FILED
        and value.get("form") == "10-KT"
    }
    if len(matches) != 1:
        raise RuntimeError(
            f"MYGN exact 10-KT fact changed for {concept} {start}/{end}: {matches}"
        )
    return matches.pop()


def extract(payload: dict) -> dict[str, float]:
    result = {
        metric: _fact(payload, spec["concept"], START, END)
        for metric, spec in EXPECTED.items()
    }
    expected_values = {metric: spec["value"] for metric, spec in EXPECTED.items()}
    if result != expected_values:
        raise RuntimeError(f"MYGN direct 2020Q4 facts changed: {result}")

    half_year = {
        metric: _fact(payload, spec["concept"], "2020-07-01", END)
        for metric, spec in EXPECTED.items()
    }
    if half_year != HALF_YEAR_EXPECTED:
        raise RuntimeError(f"MYGN transition-period facts changed: {half_year}")
    residual = {
        metric: half_year[metric] - Q3_EXPECTED[metric]
        for metric in EXPECTED
    }
    if residual != result:
        raise RuntimeError(
            f"MYGN direct Q4 does not equal six months minus Q3: {residual}"
        )
    return result


def run(cache_path: Path = CACHE, output_dir: Path = OUTPUT_DIR) -> dict:
    with gzip.open(cache_path, "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    payload = envelope["payload"]
    if int(payload["cik"]) != 899_923:
        raise RuntimeError(f"unexpected MYGN CIK: {payload.get('cik')}")
    values = extract(payload)

    fetched_at = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    rows = []
    for metric, spec in EXPECTED.items():
        rows.append({
            "ticker": "MYGN",
            "fiscal_end": END,
            "available_date": FILED,
            "metric": metric,
            "value": values[metric],
            "taxonomy": "us-gaap",
            "concept": f"10-KT-direct:{spec['concept']}",
            "form": "10-KT",
            "accession": ACCESSION,
            "fetched_at": fetched_at,
        })
    facts = pd.DataFrame(rows, columns=OUTPUT_COLUMNS).sort_values("metric")
    output_dir = Path(output_dir)
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
        "ticker": "MYGN",
        "cik": 899_923,
        "accepted_quarter_count": 1,
        "accepted_fact_count": 2,
        "quarter": {
            "fiscal_end": END,
            "available_date": FILED,
            **values,
        },
        "source": {
            "path": str(cache_path),
            "sha256": _sha256(cache_path),
            "accession": ACCESSION,
            "form": "10-KT",
        },
        "validation": {
            "direct_quarter_facts": True,
            "six_months_minus_q3_matches_direct_q4": True,
            "half_year": HALF_YEAR_EXPECTED,
            "q3": Q3_EXPECTED,
        },
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path),
                "sha256": _sha256(facts_path),
            }
        },
        "guardrail": (
            "The direct quarter and transition-period facts come from the same "
            "contemporaneous 10-KT and reconcile to the prior 10-Q Q3 values. "
            "The supplement changes availability timing only in research data."
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
    parser.add_argument("--cache", type=Path, default=CACHE)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--base-dir", type=Path)
    parser.add_argument("--candidate-output-dir", type=Path)
    args = parser.parse_args()
    report = run(args.cache, args.output_dir)
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
