#!/usr/bin/env python3
"""Recover AMRK fiscal 2020Q4 from its contemporaneous 10-K."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.io.fundamentals_update import OUTPUT_COLUMNS


CACHE = Path("output/research_only/v14/companyfacts_cache/CIK0001591588.json.gz")
OUTPUT_DIR = Path("output/research_only/v14/amrk_2020_10k_q4")
ANNUAL_ACCESSION = "0001564590-20-043316"
ANNUAL_FILED = "2020-09-14"
FISCAL_END = "2020-06-30"
ANNUAL_NET_INCOME = 30_509_000.0
ANNUAL_REVENUE = 5_461_094_000.0
PRIOR_QUARTERS = {
    "2019-09-30": {
        "accession": "0001591588-19-000043",
        "filed": "2019-11-12",
        "net_income_concept": "IncomeLossFromContinuingOperations",
        "net_income": 128_000.0,
        "revenue": 1_481_014_000.0,
    },
    "2019-12-31": {
        "accession": "0001591588-20-000013",
        "filed": "2020-02-10",
        "net_income": 1_234_000.0,
        "revenue": 1_055_590_000.0,
    },
    "2020-03-31": {
        "accession": "0001564590-20-023508",
        "filed": "2020-05-08",
        "net_income": 11_321_000.0,
        "revenue": 1_258_722_000.0,
    },
}
EXPECTED_Q4 = {"net_income": 17_826_000.0, "revenue": 1_665_768_000.0}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _exact_fact(
    payload: dict,
    concept: str,
    start: str,
    end: str,
    accession: str,
    filed: str,
    form: str,
) -> float:
    values = payload["facts"]["us-gaap"][concept]["units"]["USD"]
    matches = {
        float(item["val"])
        for item in values
        if item.get("start") == start
        and item.get("end") == end
        and item.get("accn") == accession
        and item.get("filed") == filed
        and item.get("form") == form
    }
    if len(matches) != 1:
        raise RuntimeError(
            f"AMRK exact fact changed for {concept} {end} {accession}: {matches}"
        )
    return matches.pop()


def extract(payload: dict) -> tuple[dict[str, float], dict]:
    annual = {
        "net_income": _exact_fact(
            payload,
            "NetIncomeLoss",
            "2019-07-01",
            FISCAL_END,
            ANNUAL_ACCESSION,
            ANNUAL_FILED,
            "10-K",
        ),
        "revenue": _exact_fact(
            payload,
            "Revenues",
            "2019-07-01",
            FISCAL_END,
            ANNUAL_ACCESSION,
            ANNUAL_FILED,
            "10-K",
        ),
    }
    if annual != {"net_income": ANNUAL_NET_INCOME, "revenue": ANNUAL_REVENUE}:
        raise RuntimeError(f"AMRK fiscal 2020 annual facts changed: {annual}")

    quarter_values = {}
    for end, spec in PRIOR_QUARTERS.items():
        start = {
            "2019-09-30": "2019-07-01",
            "2019-12-31": "2019-10-01",
            "2020-03-31": "2020-01-01",
        }[end]
        quarter_values[end] = {
            metric: _exact_fact(
                payload,
                spec.get("net_income_concept", "NetIncomeLoss")
                if metric == "net_income"
                else "Revenues",
                start,
                end,
                spec["accession"],
                spec["filed"],
                "10-Q",
            )
            for metric in ("net_income", "revenue")
        }
        expected = {
            "net_income": spec["net_income"],
            "revenue": spec["revenue"],
        }
        if quarter_values[end] != expected:
            raise RuntimeError(f"AMRK prior quarter changed for {end}: {quarter_values[end]}")

    residual = {
        metric: annual[metric]
        - sum(values[metric] for values in quarter_values.values())
        for metric in annual
    }
    if residual != EXPECTED_Q4:
        raise RuntimeError(f"AMRK fiscal 2020Q4 residual changed: {residual}")
    return residual, {"annual": annual, "prior_quarters": quarter_values}


def run(cache_path: Path = CACHE, output_dir: Path = OUTPUT_DIR) -> dict:
    with gzip.open(cache_path, "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    payload = envelope["payload"]
    if int(payload["cik"]) != 1_591_588:
        raise RuntimeError(f"unexpected AMRK CIK: {payload.get('cik')}")
    q4, reconciliation = extract(payload)

    fetched_at = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    rows = [
        {
            "ticker": "AMRK",
            "fiscal_end": FISCAL_END,
            "available_date": ANNUAL_FILED,
            "metric": metric,
            "value": value,
            "taxonomy": "us-gaap",
            "concept": f"strict_10k_q4:{'NetIncomeLoss' if metric == 'net_income' else 'Revenues'}",
            "form": "10-K:FY_MINUS_Q1_Q2_Q3",
            "accession": ANNUAL_ACCESSION,
            "fetched_at": fetched_at,
        }
        for metric, value in sorted(q4.items())
    ]
    facts = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
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
        "ticker": "AMRK",
        "cik": 1_591_588,
        "accepted_quarter_count": 1,
        "accepted_fact_count": 2,
        "quarter": {
            "fiscal_end": FISCAL_END,
            "available_date": ANNUAL_FILED,
            **q4,
        },
        "source": {
            "path": str(cache_path),
            "sha256": _sha256(cache_path),
            "annual_accession": ANNUAL_ACCESSION,
            "annual_filed": ANNUAL_FILED,
        },
        "validation": {
            "method": "contemporaneous_fiscal_year_minus_three_quarters",
            **reconciliation,
        },
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path),
                "sha256": _sha256(facts_path),
            }
        },
        "guardrail": (
            "Uses only facts available by the 2020-09-14 10-K filing. The later "
            "2021 comparative revision is not back-propagated into this PIT row."
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
