#!/usr/bin/env python3
"""Recover DLTR bridge quarters across its SEC revenue-taxonomy transition."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd


DEFAULT_RAW = Path(
    "cleaned_stocks_data/financial/sec_companyfacts_cache/CIK0000935703.json.gz"
)
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/dltr_revenue_transition_2017_2018_bridge"
)
REVENUE_CONCEPT = "SalesRevenueGoodsNet"
EXPECTED_QUARTERS = {
    pd.Timestamp("2017-01-28"): (
        pd.Timestamp("2016-10-30"), "2017-03-28", REVENUE_CONCEPT
    ),
    pd.Timestamp("2017-04-29"): (
        pd.Timestamp("2017-01-29"), "2017-05-25", REVENUE_CONCEPT
    ),
    pd.Timestamp("2017-07-29"): (
        pd.Timestamp("2017-04-30"), "2017-08-24", REVENUE_CONCEPT
    ),
    pd.Timestamp("2017-10-28"): (
        pd.Timestamp("2017-07-30"), "2017-11-21", REVENUE_CONCEPT
    ),
    pd.Timestamp("2018-02-03"): (
        pd.Timestamp("2017-10-29"), "2018-03-16", REVENUE_CONCEPT
    ),
    pd.Timestamp("2018-05-05"): (
        pd.Timestamp("2018-02-04"), "2018-05-31", REVENUE_CONCEPT
    ),
    pd.Timestamp("2018-08-04"): (
        pd.Timestamp("2018-05-06"), "2018-08-30", REVENUE_CONCEPT
    ),
    pd.Timestamp("2019-02-02"): (
        pd.Timestamp("2018-11-04"), "2019-03-27",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
    ),
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
    if int(wrapper.get("cik", 0)) != 935703 or wrapper.get("symbols") != ["DLTR"]:
        raise ValueError("DLTR Company Facts wrapper identity mismatch")
    payload = wrapper.get("payload", {})
    if int(payload.get("cik", 0)) != 935703:
        raise ValueError("DLTR Company Facts payload CIK mismatch")
    if "DOLLAR TREE" not in str(payload.get("entityName", "")).upper():
        raise ValueError("DLTR Company Facts issuer mismatch")
    return wrapper


def _strict_quarter_rows(payload: dict) -> pd.DataFrame:
    rows = []
    facts = payload["facts"]["us-gaap"]
    metric_concepts = {
        (end, "revenue"): spec[2]
        for end, spec in EXPECTED_QUARTERS.items()
    }
    metric_concepts.update({
        (end, "net_income"): "NetIncomeLoss" for end in EXPECTED_QUARTERS
    })
    for (expected_end, metric), concept in metric_concepts.items():
        units = facts[concept]["units"]
        if set(units) != {"USD"}:
            raise ValueError(f"DLTR {concept} must contain only USD facts")
        for item in units["USD"]:
            end = pd.Timestamp(item["end"])
            if end != expected_end:
                continue
            expected_start, expected_filed, _ = EXPECTED_QUARTERS[end]
            if (
                pd.Timestamp(item["start"]) != expected_start
                or item.get("form") not in {"10-Q", "10-K", "10-K/A"}
                or pd.Timestamp(item["filed"]) != pd.Timestamp(expected_filed)
            ):
                continue
            rows.append({
                "ticker": "DLTR",
                "fiscal_end": end,
                "available_date": pd.Timestamp(item["filed"]),
                "metric": metric,
                "value": float(item["val"]),
                "taxonomy": "us-gaap",
                "concept": concept,
                "form": item["form"],
                "accession": item["accn"],
            })
    selected = pd.DataFrame(rows).drop_duplicates()
    keys = ["fiscal_end", "available_date", "metric"]
    if selected.duplicated(keys, keep=False).any():
        for _, group in selected.groupby(keys):
            if len(group) > 1 and group["value"].nunique() != 1:
                raise ValueError("DLTR bridge facts contain conflicting values")
        selected = selected.drop_duplicates(keys, keep="first")
    expected = {
        (end, pd.Timestamp(filed), metric)
        for end, (_, filed, _) in EXPECTED_QUARTERS.items()
        for metric in ("revenue", "net_income")
    }
    actual = set(selected[keys].itertuples(index=False, name=None))
    if actual != expected:
        raise RuntimeError("DLTR bridge chain is not exactly eight paired quarters")
    return selected.sort_values(["fiscal_end", "metric"]).reset_index(drop=True)


def run(
    *, raw_path: Path = DEFAULT_RAW, output_dir: Path = DEFAULT_OUTPUT_DIR
) -> dict:
    wrapper = _load_wrapper(raw_path)
    facts = _strict_quarter_rows(wrapper["payload"])
    facts["unit"] = "USD"
    facts["source"] = "sec_companyfacts_dltr_legacy_revenue_transition"
    facts["source_archive"] = raw_path.name
    facts["source_archive_sha256"] = _sha256(raw_path)
    facts["derivation"] = "direct_three_month_sec_fact"

    paired = facts.pivot_table(
        index=["fiscal_end", "available_date"],
        columns="metric",
        values="value",
        aggfunc="first",
    ).reset_index()
    recovered = [
        {
            "ticker": "DLTR",
            "fiscal_end": pd.Timestamp(row.fiscal_end).strftime("%Y-%m-%d"),
            "available_date": pd.Timestamp(row.available_date).strftime("%Y-%m-%d"),
            "revenue": float(row.revenue),
            "net_income": float(row.net_income),
        }
        for row in paired.itertuples(index=False)
    ]

    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "promotion_eligible": False,
        "release_status": "BLOCKED",
        "ticker": "DLTR",
        "accepted_quarter_count": 8,
        "recovered_quarters": recovered,
        "taxonomy_transition": {
            "legacy_revenue_concept": REVENUE_CONCEPT,
            "successor_revenue_concept": (
                "RevenueFromContractWithCustomerExcludingAssessedTax"
            ),
            "restored_fiscal_period": (
                "fiscal 2016Q4 through fiscal 2018Q2, plus fiscal 2018Q4"
            ),
        },
        "raw_payload": {
            "path": str(raw_path),
            "sha256": _sha256(raw_path),
            "source_url": wrapper.get("source_url"),
            "fetched_at": wrapper.get("fetched_at"),
        },
        "outputs": {
            "quarters": {"path": str(facts_path), "sha256": _sha256(facts_path)}
        },
        "guardrail": (
            "Only exact three-month DLTR USD facts filed in the original "
            "10-Q/10-K are used. Later comparative disclosures and cumulative "
            "durations are excluded; formal fundamentals remain unchanged."
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
        "manifest": result["manifest"],
        "accepted_quarter_count": result["accepted_quarter_count"],
        "release_status": result["release_status"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
