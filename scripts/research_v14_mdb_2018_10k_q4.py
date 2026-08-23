#!/usr/bin/env python3
"""Recover MDB fiscal 2017Q4 and 2018Q4 from strict SEC residuals."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.io.fundamentals_update import OUTPUT_COLUMNS


CACHE = Path("output/research_only/v14/companyfacts_cache/CIK0001441816.json.gz")
OUTPUT_DIR = Path("output/research_only/v14/mdb_2018_10k_q4")
ANNUAL_ACCESSION = "0001441816-18-000028"
YTD_ACCESSION = "0001441816-17-000010"
AVAILABLE_DATE = "2018-03-30"
CONCEPTS = {
    "revenue": "SalesRevenueNet",
    "net_income": "NetIncomeLoss",
}
EXPECTED_INPUTS = {
    "2017-01-31": {
        "annual": {"revenue": 101_358_000.0, "net_income": -86_681_000.0},
        "ytd": {"revenue": 71_424_000.0, "net_income": -64_861_000.0},
    },
    "2018-01-31": {
        "annual": {"revenue": 154_519_000.0, "net_income": -96_359_000.0},
        "ytd": {"revenue": 109_478_000.0, "net_income": -69_985_000.0},
    },
}
EXPECTED_Q4 = {
    "2017-01-31": {"revenue": 29_934_000.0, "net_income": -21_820_000.0},
    "2018-01-31": {"revenue": 45_041_000.0, "net_income": -26_374_000.0},
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _exact_fact(
    payload: dict,
    *,
    concept: str,
    accession: str,
    start: str,
    end: str,
) -> float:
    facts = payload["facts"]["us-gaap"][concept]["units"]["USD"]
    matches = {
        float(fact["val"])
        for fact in facts
        if fact.get("accn") == accession
        and fact.get("start") == start
        and fact.get("end") == end
    }
    if len(matches) != 1:
        raise RuntimeError(
            f"MDB exact SEC fact changed for {concept} {start}/{end}: {matches}"
        )
    return matches.pop()


def extract_inputs(payload: dict) -> dict[str, dict[str, dict[str, float]]]:
    specs = {
        "2017-01-31": {
            "annual": (ANNUAL_ACCESSION, "2016-02-01", "2017-01-31"),
            "ytd": (YTD_ACCESSION, "2016-02-01", "2016-10-31"),
        },
        "2018-01-31": {
            "annual": (ANNUAL_ACCESSION, "2017-02-01", "2018-01-31"),
            "ytd": (YTD_ACCESSION, "2017-02-01", "2017-10-31"),
        },
    }
    inputs: dict[str, dict[str, dict[str, float]]] = {}
    for fiscal_end, periods in specs.items():
        inputs[fiscal_end] = {}
        for period_name, (accession, start, end) in periods.items():
            inputs[fiscal_end][period_name] = {
                metric: _exact_fact(
                    payload,
                    concept=concept,
                    accession=accession,
                    start=start,
                    end=end,
                )
                for metric, concept in CONCEPTS.items()
            }
    if inputs != EXPECTED_INPUTS:
        raise RuntimeError(f"MDB residual inputs changed: {inputs}")
    return inputs


def run(cache_path: Path = CACHE, output_dir: Path = OUTPUT_DIR) -> dict:
    with gzip.open(cache_path, "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    payload = envelope["payload"]
    if int(payload["cik"]) != 1_441_816:
        raise ValueError("MDB cache has the wrong CIK")
    inputs = extract_inputs(payload)
    fetched_at = pd.Timestamp(envelope["fetched_at"]).tz_localize(None).normalize()
    records = []
    for fiscal_end, periods in inputs.items():
        for metric in CONCEPTS:
            records.append(
                {
                    "ticker": "MDB",
                    "fiscal_end": fiscal_end,
                    "available_date": AVAILABLE_DATE,
                    "metric": metric,
                    "value": periods["annual"][metric] - periods["ytd"][metric],
                    "taxonomy": "us-gaap",
                    "concept": f"derived_q4:{CONCEPTS[metric]}",
                    "form": "10-K+10-Q_RESIDUAL",
                    "accession": f"{ANNUAL_ACCESSION}+{YTD_ACCESSION}",
                    "fetched_at": fetched_at,
                }
            )
    facts = pd.DataFrame(records, columns=OUTPUT_COLUMNS)
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
    actual = {
        str(fiscal_end.date()): group.set_index("metric")["value"].to_dict()
        for fiscal_end, group in facts.groupby("fiscal_end")
    }
    if actual != EXPECTED_Q4:
        raise RuntimeError(f"MDB Q4 residuals changed: {actual}")

    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.sort_values(["fiscal_end", "metric"]).to_csv(facts_path, index=False)
    report = {
        "schema_version": 1,
        "research_only": True,
        "ticker": "MDB",
        "point_in_time_proven": True,
        "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "accepted_quarter_count": 2,
        "fact_count": 4,
        "available_date": AVAILABLE_DATE,
        "inputs": inputs,
        "q4_residuals": actual,
        "sources": [{"path": str(cache_path), "sha256": _sha256(cache_path)}],
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path),
                "sha256": _sha256(facts_path),
            }
        },
        "guardrail": (
            "Each fiscal Q4 equals the comparative annual 10-K value minus the "
            "matching nine-month 10-Q value. Both residuals become available only "
            "on 2018-03-30, the later required filing date."
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
    parser.add_argument("--cache-path", type=Path, default=CACHE)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--base-dir", type=Path)
    parser.add_argument("--candidate-output-dir", type=Path)
    args = parser.parse_args()
    report = run(args.cache_path, args.output_dir)
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
