#!/usr/bin/env python3
"""Build a source-locked CCEP H1 2021 exact-TTM growth package."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.io.fundamentals_update import OUTPUT_COLUMNS


SOURCE_PATH = Path(
    "cleaned_stocks_data/financial/sec_companyfacts_cache/CIK0001650107.json.gz"
)
SOURCE_SHA256 = "9aa57daca4967d0691b78ac98debf3177e69bdf7432bec42f85c0c285e1f6282"
OUTPUT_DIR = Path("output/research_only/v14/ccep_h1_exact_growth")
CIK = 1650107
TICKER = "CCEP"
CURRENCY = "EUR"
AVAILABLE_DATE = "2021-09-02"
FISCAL_END = "2021-07-02"

CONCEPTS = {
    "revenue": "Revenue",
    "net_income": "ProfitLossAttributableToOwnersOfParent",
}

PERIODS = {
    "fy2019": {
        "start": "2019-01-01", "end": "2019-12-31",
        "accession": "0001650107-21-000022", "filed": "2021-03-12",
        "form": "20-F",
    },
    "fy2020": {
        "start": "2020-01-01", "end": "2020-12-31",
        "accession": "0001650107-21-000022", "filed": "2021-03-12",
        "form": "20-F",
    },
    "h1_2019": {
        "start": "2019-01-01", "end": "2019-06-28",
        "accession": "0001650107-20-000076", "filed": "2020-08-06",
        "form": "6-K",
    },
    "h1_2020": {
        "start": "2020-01-01", "end": "2020-06-26",
        "accession": "0001650107-21-000070", "filed": AVAILABLE_DATE,
        "form": "6-K",
    },
    "h1_2021": {
        "start": "2021-01-01", "end": FISCAL_END,
        "accession": "0001650107-21-000070", "filed": AVAILABLE_DATE,
        "form": "6-K",
    },
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_payload(source_path: Path) -> tuple[dict, dict]:
    actual = _sha256(source_path)
    if actual != SOURCE_SHA256:
        raise ValueError(f"CCEP Company Facts SHA mismatch: {actual}")
    with gzip.open(source_path, "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    payload = envelope["payload"]
    if int(payload["cik"]) != CIK:
        raise ValueError(f"unexpected CCEP CIK: {payload['cik']}")
    return envelope, payload


def _exact_fact(payload: dict, concept: str, period: dict) -> float:
    records = payload["facts"]["ifrs-full"][concept]["units"][CURRENCY]
    matches = [
        record for record in records
        if record.get("start") == period["start"]
        and record.get("end") == period["end"]
        and record.get("accn") == period["accession"]
        and record.get("filed") == period["filed"]
        and record.get("form") == period["form"]
    ]
    values = {float(record["val"]) for record in matches}
    if len(values) != 1:
        raise ValueError(
            f"expected one CCEP {concept} value for {period}, got {values}"
        )
    return values.pop()


def ccep_direct_growth_facts(payload: dict, fetched_at: str) -> tuple[pd.DataFrame, dict]:
    operands = {
        metric: {
            period_name: _exact_fact(payload, concept, period)
            for period_name, period in PERIODS.items()
        }
        for metric, concept in CONCEPTS.items()
    }
    records: list[dict] = []
    derived: dict[str, dict[str, float]] = {}
    composite_accession = "+".join(sorted({
        period["accession"] for period in PERIODS.values()
    }))
    for metric, values in operands.items():
        prior_ttm = values["fy2019"] - values["h1_2019"] + values["h1_2020"]
        current_ttm = values["fy2020"] - values["h1_2020"] + values["h1_2021"]
        growth = (current_ttm - prior_ttm) / abs(prior_ttm)
        derived[metric] = {
            "prior_ttm": prior_ttm, "current_ttm": current_ttm, "growth": growth
        }
        concept = CONCEPTS[metric]
        for output_metric, value in (
            (f"{metric}_ttm", current_ttm),
            (f"{metric}_growth", growth),
        ):
            records.append({
                "ticker": TICKER,
                "fiscal_end": FISCAL_END,
                "available_date": AVAILABLE_DATE,
                "metric": output_metric,
                "value": value,
                "taxonomy": "ifrs-full",
                "concept": f"ccep_exact_h1_ttm:{concept}:{CURRENCY}",
                "form": "20-F_PLUS_6-K_H1_CUMULATIVE_TTM",
                "accession": composite_accession,
                "fetched_at": fetched_at,
            })
    facts = pd.DataFrame(records, columns=OUTPUT_COLUMNS).sort_values("metric")
    return facts.reset_index(drop=True), {"operands": operands, "derived": derived}


def build(source_path: Path = SOURCE_PATH, output_dir: Path = OUTPUT_DIR) -> dict:
    source_path = Path(source_path)
    envelope, payload = _load_payload(source_path)
    facts, evidence = ccep_direct_growth_facts(
        payload, str(envelope["fetched_at"])[:10]
    )
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    evidence_path = output_dir / "exact_ttm_evidence.json"
    evidence_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "ticker": TICKER,
        "cik": CIK,
        "currency": CURRENCY,
        "source": {
            "path": str(source_path), "sha256": SOURCE_SHA256,
            "source_url": envelope["source_url"],
        },
        "accepted_direct_growth_package_count": 1,
        "accepted_fact_count": len(facts),
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path), "sha256": _sha256(facts_path)
            },
            "exact_ttm_evidence": {
                "path": str(evidence_path), "sha256": _sha256(evidence_path)
            },
        },
        "guardrail": (
            "Current and prior H1 TTM values use reported EUR IFRS facts: "
            "FY minus prior H1 plus current H1. No quarter is manufactured, "
            "the later duplicate 6-K accession is excluded, and the package "
            "is unavailable before 2021-09-02."
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
    parser.add_argument("--source", type=Path, default=SOURCE_PATH)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--base-dir", type=Path)
    parser.add_argument("--candidate-output-dir", type=Path)
    args = parser.parse_args()
    if (args.base_dir is None) != (args.candidate_output_dir is None):
        parser.error("--base-dir and --candidate-output-dir must be used together")
    report = build(args.source, args.output_dir)
    if args.base_dir is not None:
        report["candidate"] = integrate_candidate(
            base_dir=args.base_dir,
            supplement_dir=args.output_dir,
            output_dir=args.candidate_output_dir,
        )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
