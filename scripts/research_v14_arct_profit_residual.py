#!/usr/bin/env python3
"""Recover ARCT 2019Q4 profit once all residual inputs were public."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.io.fundamentals_update import (
    OUTPUT_COLUMNS,
    parse_companyfacts_annual,
    parse_companyfacts_quarterly,
)


CACHE = Path("output/research_only/v14/companyfacts_cache/CIK0001768224.json.gz")
OUTPUT_DIR = Path("output/research_only/v14/arct_2019q4_profit_residual")
CUTOFF = pd.Timestamp("2020-05-08")
EXPECTED_QUARTERS = {
    "2019-03-31": -6_884_000.0,
    "2019-06-30": -685_000.0,
    "2019-09-30": -7_433_000.0,
}
EXPECTED_DATES = {
    "2019-03-31": "2020-05-08",
    "2019-06-30": "2019-08-14",
    "2019-09-30": "2019-11-08",
}
EXPECTED_ANNUAL = -25_991_000.0
EXPECTED_Q4 = -10_989_000.0


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(cache_path: Path = CACHE, output_dir: Path = OUTPUT_DIR) -> dict:
    with gzip.open(cache_path, "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    if int(envelope["payload"]["cik"]) != 1768224:
        raise ValueError("ARCT cache has the wrong CIK")
    quarterly = parse_companyfacts_quarterly(
        "ARCT", envelope["payload"], envelope["fetched_at"]
    )
    known = quarterly.loc[
        quarterly["metric"].eq("net_income")
        & quarterly["fiscal_end"].isin(pd.to_datetime(list(EXPECTED_QUARTERS)))
        & quarterly["available_date"].le(CUTOFF)
    ].sort_values("available_date").drop_duplicates(
        ["fiscal_end", "metric"], keep="last"
    ).sort_values("fiscal_end")
    values = {
        str(row.fiscal_end.date()): float(row.value)
        for row in known.itertuples(index=False)
    }
    dates = {
        str(row.fiscal_end.date()): str(row.available_date.date())
        for row in known.itertuples(index=False)
    }
    if values != EXPECTED_QUARTERS or dates != EXPECTED_DATES:
        raise RuntimeError(
            f"ARCT comparison inputs changed: values={values}, dates={dates}"
        )
    annual = parse_companyfacts_annual(
        "ARCT", envelope["payload"], envelope["fetched_at"]
    )
    annual = annual.loc[
        annual["metric"].eq("net_income")
        & annual["fiscal_end"].eq(pd.Timestamp("2019-12-31"))
        & annual["available_date"].le(CUTOFF)
    ].sort_values("available_date").tail(1)
    if len(annual) != 1:
        raise RuntimeError("ARCT first 2019 annual profit fact is missing")
    annual_row = annual.iloc[0]
    if (
        float(annual_row["value"]) != EXPECTED_ANNUAL
        or annual_row["available_date"] != pd.Timestamp("2020-03-16")
        or annual_row["accession"] != "0001564590-20-011037"
    ):
        raise RuntimeError("ARCT first 2019 annual profit fact changed")
    available = max(annual_row["available_date"], known["available_date"].max())
    q4 = float(annual_row["value"] - known["value"].sum())
    if q4 != EXPECTED_Q4 or available != CUTOFF:
        raise RuntimeError(
            f"ARCT 2019Q4 residual changed: value={q4}, available={available}"
        )
    fetched_at = pd.Timestamp(envelope["fetched_at"]).tz_localize(None).normalize()
    fact = pd.DataFrame([{
        "ticker": "ARCT", "fiscal_end": "2019-12-31",
        "available_date": available, "metric": "net_income", "value": q4,
        "taxonomy": "ARCT_US_GAAP_COMPANYFACTS",
        "concept": "derived_fy_minus_latest_known_q1_q2_q3:NetIncomeLoss",
        "form": "10-K+10-Q_RESIDUAL",
        "accession": "0001564590-20-011037+0001564590-20-023633",
        "fetched_at": fetched_at,
    }], columns=OUTPUT_COLUMNS)
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    fact.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1, "research_only": True, "ticker": "ARCT",
        "point_in_time_proven": True, "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED", "promotion_eligible": False,
        "accepted_quarter_count": 1, "fact_count": 1,
        "cutoff": str(CUTOFF.date()), "quarter_inputs": values,
        "quarter_input_available_dates": dates,
        "annual_input": {
            "value": EXPECTED_ANNUAL,
            "available_date": str(annual_row["available_date"].date()),
            "accession": annual_row["accession"],
        },
        "q4_residual": EXPECTED_Q4,
        "sources": [{"path": str(cache_path), "sha256": _sha256(cache_path)}],
        "outputs": {"strict_quarterly_facts": {
            "path": str(facts_path), "sha256": _sha256(facts_path)
        }},
        "guardrail": (
            "2019Q4 NetIncomeLoss is annual 2019 less the latest Q1-Q3 "
            "comparatives available by 2020-05-08. It is not available for "
            "the 2020-04-30 signal and is never backdated."
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
