#!/usr/bin/env python3
"""Recover two direct TTEK quarterly revenue facts omitted without SEC frames."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd


DEFAULT_RAW = Path(
    "output/research_only/v14/companyfacts_cache/CIK0000831641.json.gz"
)
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/ttek_legacy_revenue_2018"
)
ACCESSION = "0000831641-18-000097"
FILED = "2018-11-16"
DIRECT_REVENUE = {
    pd.Timestamp("2018-04-01"): (pd.Timestamp("2018-01-01"), 700_262_000.0),
    pd.Timestamp("2018-09-30"): (pd.Timestamp("2018-07-02"), 739_343_000.0),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_wrapper(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        wrapper = json.load(handle)
    if int(wrapper.get("cik", 0)) != 831641 or wrapper.get("symbols") != ["TTEK"]:
        raise ValueError("TTEK Company Facts wrapper identity mismatch")
    payload = wrapper.get("payload") or {}
    if int(payload.get("cik", 0)) != 831641:
        raise ValueError("TTEK Company Facts payload CIK mismatch")
    if "TETRA TECH" not in str(payload.get("entityName", "")).upper():
        raise ValueError("TTEK Company Facts issuer mismatch")
    return wrapper


def strict_revenue_rows(payload: dict) -> pd.DataFrame:
    concept = payload["facts"]["us-gaap"]["Revenues"]
    if set(concept.get("units") or {}) != {"USD"}:
        raise ValueError("TTEK Revenues facts must use only USD")
    rows = []
    for fiscal_end, (start, expected_value) in DIRECT_REVENUE.items():
        matches = [
            fact for fact in concept["units"]["USD"]
            if pd.Timestamp(fact.get("start")) == start
            and pd.Timestamp(fact.get("end")) == fiscal_end
            and fact.get("accn") == ACCESSION
            and fact.get("form") == "10-K"
            and pd.Timestamp(fact.get("filed")) == pd.Timestamp(FILED)
        ]
        if len(matches) != 1:
            raise ValueError(
                f"TTEK direct revenue fact is not unique for {fiscal_end.date()}: "
                f"{len(matches)}"
            )
        fact = matches[0]
        if float(fact["val"]) != expected_value:
            raise ValueError("TTEK direct revenue differs from predeclared evidence")
        rows.append({
            "ticker": "TTEK", "fiscal_end": fiscal_end,
            "available_date": pd.Timestamp(FILED), "metric": "revenue",
            "value": expected_value, "taxonomy": "us-gaap",
            "concept": "Revenues", "form": "10-K",
            "accession": ACCESSION, "unit": "USD",
            "derivation": "direct_three_month_sec_fact_without_frame",
        })
    result = pd.DataFrame(rows).sort_values("fiscal_end").reset_index(drop=True)
    if len(result) != 2 or result["fiscal_end"].nunique() != 2:
        raise RuntimeError("TTEK recovery is not exactly two direct revenue facts")
    return result


def run(*, raw_path: Path = DEFAULT_RAW, output_dir: Path = DEFAULT_OUTPUT_DIR):
    wrapper = _load_wrapper(raw_path)
    facts = strict_revenue_rows(wrapper["payload"])
    facts["source"] = "sec_companyfacts_ttek_direct_quarter_without_frame"
    facts["source_archive"] = raw_path.name
    facts["source_archive_sha256"] = _sha256(raw_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    recovered = [{
        "ticker": row.ticker,
        "fiscal_end": pd.Timestamp(row.fiscal_end).strftime("%Y-%m-%d"),
        "available_date": pd.Timestamp(row.available_date).strftime("%Y-%m-%d"),
        "metric": row.metric, "value": float(row.value),
    } for row in facts.itertuples(index=False)]
    report = {
        "schema_version": 1, "research_only": True,
        "point_in_time_proven": True, "parameters_frozen": False,
        "promotion_eligible": False, "release_status": "BLOCKED",
        "ticker": "TTEK", "accepted_fact_count": len(facts),
        "recovered_facts": recovered,
        "raw_payload": {
            "path": str(raw_path), "sha256": _sha256(raw_path),
            "source_url": wrapper.get("source_url"),
            "fetched_at": wrapper.get("fetched_at"),
        },
        "outputs": {"quarters": {
            "path": str(facts_path), "sha256": _sha256(facts_path),
        }},
        "guardrail": (
            "Only the two predeclared USD Revenues facts with direct "
            "three-month durations in Tetra Tech's 2018 10-K are restored. "
            "They were omitted because SEC supplied no frame. No cumulative "
            "difference or net-income substitution is used."
        ),
    }
    manifest = output_dir / "manifest.json"
    manifest.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["manifest"] = str(manifest)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = run(raw_path=args.raw, output_dir=args.output_dir)
    print(json.dumps({
        "accepted_fact_count": result["accepted_fact_count"],
        "manifest": result["manifest"],
        "release_status": result["release_status"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
