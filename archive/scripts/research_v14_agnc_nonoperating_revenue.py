#!/usr/bin/env python3
"""Recover AGNC 2021 quarterly revenue across an SEC concept cutover."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.io.fundamentals_update import OUTPUT_COLUMNS


CACHE = Path("output/research_only/v14/companyfacts_cache/CIK0001423689.json.gz")
OUTPUT_DIR = Path("output/research_only/v14/agnc_nonoperating_revenue")
SPECS = {
    "2021-03-31": {
        "start": "2021-01-01",
        "filed": "2021-05-07",
        "accession": "0001423689-21-000037",
        "net_interest": 528_000_000.0,
        "nonoperating": 471_000_000.0,
        "operating_expenses": 24_000_000.0,
        "net_income": 975_000_000.0,
        "revenue": 999_000_000.0,
    },
    "2021-06-30": {
        "start": "2021-04-01",
        "filed": "2021-08-05",
        "accession": "0001423689-21-000063",
        "net_interest": 232_000_000.0,
        "nonoperating": -621_000_000.0,
        "operating_expenses": 22_000_000.0,
        "net_income": -411_000_000.0,
        "revenue": -389_000_000.0,
    },
    "2021-09-30": {
        "start": "2021-07-01",
        "filed": "2021-11-05",
        "accession": "0001423689-21-000070",
        "net_interest": 279_000_000.0,
        "nonoperating": -45_000_000.0,
        "operating_expenses": 22_000_000.0,
        "net_income": 212_000_000.0,
        "revenue": 234_000_000.0,
    },
}
COMPARATIVE_CUTOVER = {
    "start": "2020-01-01",
    "end": "2020-03-31",
    "old_accession": "0001423689-20-000042",
    "old_filed": "2020-05-11",
    "new_accession": "0001423689-21-000037",
    "new_filed": "2021-05-07",
    "value": -2_463_000_000.0,
}
Q4_2020_INPUTS = {
    "annual": {
        "start": "2020-01-01",
        "end": "2020-12-31",
        "filed": "2021-02-26",
        "accession": "0001423689-21-000011",
        "net_interest": 845_000_000.0,
        "nonoperating": -1_018_000_000.0,
        "operating_expenses": 93_000_000.0,
        "net_income": -266_000_000.0,
    },
    "quarters": [
        {
            "start": "2020-01-01", "end": "2020-03-31",
            "filed": "2020-05-11", "accession": "0001423689-20-000042",
            "net_interest": 65_000_000.0, "nonoperating": -2_463_000_000.0,
            "operating_expenses": 23_000_000.0, "net_income": -2_421_000_000.0,
        },
        {
            "start": "2020-04-01", "end": "2020-06-30",
            "filed": "2020-08-07", "accession": "0001423689-20-000047",
            "net_interest": 295_000_000.0, "nonoperating": 447_000_000.0,
            "operating_expenses": 24_000_000.0, "net_income": 718_000_000.0,
        },
        {
            "start": "2020-07-01", "end": "2020-09-30",
            "filed": "2020-11-05", "accession": "0001423689-20-000051",
            "net_interest": 302_000_000.0, "nonoperating": 381_000_000.0,
            "operating_expenses": 21_000_000.0, "net_income": 662_000_000.0,
        },
    ],
}
EXPECTED_Q4_2020 = {
    "start": "2020-10-01",
    "filed": "2021-02-26",
    "accession": (
        "0001423689-21-000011+0001423689-20-000042+"
        "0001423689-20-000047+0001423689-20-000051"
    ),
    "net_interest": 183_000_000.0,
    "nonoperating": 617_000_000.0,
    "operating_expenses": 25_000_000.0,
    "net_income": 775_000_000.0,
    "revenue": 800_000_000.0,
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fact(
    payload: dict,
    concept: str,
    *,
    start: str,
    end: str,
    accession: str,
    filed: str,
    form: str = "10-Q",
) -> float:
    values = payload["facts"]["us-gaap"][concept]["units"]["USD"]
    matches = {
        float(value["val"])
        for value in values
        if value.get("start") == start
        and value.get("end") == end
        and value.get("accn") == accession
        and value.get("filed") == filed
        and value.get("form") == form
    }
    if len(matches) != 1:
        raise RuntimeError(
            f"AGNC exact fact changed for {concept} {start}/{end}: {matches}"
        )
    return matches.pop()


def extract_quarters(payload: dict) -> dict[str, dict[str, float | str]]:
    cutover = COMPARATIVE_CUTOVER
    old_value = _fact(
        payload,
        "NoninterestIncome",
        start=cutover["start"],
        end=cutover["end"],
        accession=cutover["old_accession"],
        filed=cutover["old_filed"],
    )
    new_value = _fact(
        payload,
        "NonoperatingIncomeExpense",
        start=cutover["start"],
        end=cutover["end"],
        accession=cutover["new_accession"],
        filed=cutover["new_filed"],
    )
    if old_value != cutover["value"] or new_value != cutover["value"]:
        raise RuntimeError(
            f"AGNC concept cutover is not value-identical: {old_value}, {new_value}"
        )

    annual_spec = Q4_2020_INPUTS["annual"]
    annual = {
        "net_interest": _fact(
            payload, "InterestIncomeExpenseNet", **{
                key: annual_spec[key]
                for key in ("start", "end", "accession", "filed")
            }, form="10-K"
        ),
        "nonoperating": _fact(
            payload, "NonoperatingIncomeExpense", **{
                key: annual_spec[key]
                for key in ("start", "end", "accession", "filed")
            }, form="10-K"
        ),
        "operating_expenses": _fact(
            payload, "OperatingExpenses", **{
                key: annual_spec[key]
                for key in ("start", "end", "accession", "filed")
            }, form="10-K"
        ),
        "net_income": _fact(
            payload, "NetIncomeLoss", **{
                key: annual_spec[key]
                for key in ("start", "end", "accession", "filed")
            }, form="10-K"
        ),
    }
    if annual != {
        key: annual_spec[key]
        for key in ("net_interest", "nonoperating", "operating_expenses", "net_income")
    }:
        raise RuntimeError(f"AGNC 2020 annual inputs changed: {annual}")
    direct_quarters = []
    for quarter_spec in Q4_2020_INPUTS["quarters"]:
        common = {
            key: quarter_spec[key]
            for key in ("start", "end", "accession", "filed")
        }
        quarter = {
            "net_interest": _fact(payload, "InterestIncomeExpenseNet", **common),
            "nonoperating": _fact(payload, "NoninterestIncome", **common),
            "operating_expenses": _fact(payload, "OperatingExpenses", **common),
            "net_income": _fact(payload, "NetIncomeLoss", **common),
        }
        if quarter != {
            key: quarter_spec[key]
            for key in ("net_interest", "nonoperating", "operating_expenses", "net_income")
        }:
            raise RuntimeError(f"AGNC {quarter_spec['end']} inputs changed: {quarter}")
        direct_quarters.append(quarter)
    q4 = {
        "start": "2020-10-01",
        "filed": annual_spec["filed"],
        "accession": EXPECTED_Q4_2020["accession"],
        **{
            metric: annual[metric] - sum(quarter[metric] for quarter in direct_quarters)
            for metric in ("net_interest", "nonoperating", "operating_expenses", "net_income")
        },
    }
    q4["revenue"] = q4["net_interest"] + q4["nonoperating"]
    if q4 != EXPECTED_Q4_2020 or q4["revenue"] - q4["operating_expenses"] != q4["net_income"]:
        raise RuntimeError(f"AGNC 2020Q4 residual changed: {q4}")

    recovered: dict[str, dict[str, float | str]] = {"2020-12-31": q4}
    for end, expected in SPECS.items():
        common = {
            "start": expected["start"],
            "end": end,
            "accession": expected["accession"],
            "filed": expected["filed"],
        }
        net_interest = _fact(payload, "InterestIncomeExpenseNet", **common)
        nonoperating = _fact(payload, "NonoperatingIncomeExpense", **common)
        operating_expenses = _fact(payload, "OperatingExpenses", **common)
        net_income = _fact(payload, "NetIncomeLoss", **common)
        revenue = net_interest + nonoperating
        actual = {
            "start": expected["start"],
            "filed": expected["filed"],
            "accession": expected["accession"],
            "net_interest": net_interest,
            "nonoperating": nonoperating,
            "operating_expenses": operating_expenses,
            "net_income": net_income,
            "revenue": revenue,
        }
        if actual != expected:
            raise RuntimeError(f"AGNC {end} inputs changed: {actual}")
        if revenue - operating_expenses != net_income:
            raise RuntimeError(f"AGNC {end} income statement does not reconcile")
        recovered[end] = actual
    return recovered


def run(cache_path: Path = CACHE, output_dir: Path = OUTPUT_DIR) -> dict:
    with gzip.open(cache_path, "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    payload = envelope["payload"]
    if int(payload["cik"]) != 1_423_689:
        raise ValueError("AGNC cache has the wrong CIK")
    quarters = extract_quarters(payload)
    fetched_at = pd.Timestamp(envelope["fetched_at"]).tz_localize(None).normalize()
    rows = []
    for end, values in quarters.items():
        metrics = ("revenue", "net_income") if end == "2020-12-31" else ("revenue",)
        for metric in metrics:
            rows.append({
                "ticker": "AGNC",
                "fiscal_end": end,
                "available_date": values["filed"],
                "metric": metric,
                "value": values[metric],
                "taxonomy": "us-gaap",
                "concept": (
                    "derived_q4:NetIncomeLoss" if metric == "net_income"
                    else "derived_bank_revenue:"
                    "InterestIncomeExpenseNet+NonoperatingIncomeExpense"
                ),
                "form": "10-K+10-Q_RESIDUAL" if end == "2020-12-31" else "10-Q",
                "accession": values["accession"],
                "fetched_at": fetched_at,
            })
    facts = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.sort_values("fiscal_end").to_csv(facts_path, index=False)
    report = {
        "schema_version": 1,
        "research_only": True,
        "ticker": "AGNC",
        "point_in_time_proven": True,
        "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "accepted_quarter_count": 4,
        "fact_count": 5,
        "comparative_cutover": {
            **COMPARATIVE_CUTOVER,
            "old_concept": "NoninterestIncome",
            "new_concept": "NonoperatingIncomeExpense",
            "values_identical": True,
        },
        "recovered_quarters": quarters,
        "sources": [{"path": str(cache_path), "sha256": _sha256(cache_path)}],
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path),
                "sha256": _sha256(facts_path),
            }
        },
        "guardrail": (
            "The 2020Q1 comparative proves the old NoninterestIncome and new "
            "NonoperatingIncomeExpense concepts carry the same value at the "
            "cutover. 2020Q4 is the annual statement minus the first three "
            "direct quarters; its strict residual replaces an issuer-mistagged "
            "NetIncomeLoss frame. Every recovered quarter reconciles net interest "
            "plus nonoperating income, less operating expenses, to net income."
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
