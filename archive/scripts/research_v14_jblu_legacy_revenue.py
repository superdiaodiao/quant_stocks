#!/usr/bin/env python3
"""Recover JBLU 2017-2018 quarters across an SEC revenue-taxonomy transition."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd


DEFAULT_RAW = Path(
    "cleaned_stocks_data/financial/sec_companyfacts_cache/CIK0001158463.json.gz"
)
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/jblu_revenue_transition_2017_2018"
)
LEGACY_REVENUE = "SalesRevenueServicesNet"
SUCCESSOR_REVENUE = "RevenueFromContractWithCustomerExcludingAssessedTax"
DIRECT_QUARTERS = {
    pd.Timestamp("2017-03-31"): (
        pd.Timestamp("2017-01-01"), "2017-04-28", LEGACY_REVENUE
    ),
    pd.Timestamp("2017-06-30"): (
        pd.Timestamp("2017-04-01"), "2017-07-28", LEGACY_REVENUE
    ),
    pd.Timestamp("2017-09-30"): (
        pd.Timestamp("2017-07-01"), "2017-10-27", LEGACY_REVENUE
    ),
    pd.Timestamp("2017-12-31"): (
        pd.Timestamp("2017-10-01"), "2018-02-16", LEGACY_REVENUE
    ),
    pd.Timestamp("2018-03-31"): (
        pd.Timestamp("2018-01-01"), "2018-04-27", LEGACY_REVENUE
    ),
    pd.Timestamp("2018-06-30"): (
        pd.Timestamp("2018-04-01"), "2018-07-26", SUCCESSOR_REVENUE
    ),
    pd.Timestamp("2018-09-30"): (
        pd.Timestamp("2018-07-01"), "2018-10-26", SUCCESSOR_REVENUE
    ),
}
Q4_2018_END = pd.Timestamp("2018-12-31")
Q4_2018_START = pd.Timestamp("2018-10-01")
Q4_2018_FILED = "2019-02-21"
ANNUAL_2018_START = pd.Timestamp("2018-01-01")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_wrapper(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        wrapper = json.load(handle)
    if int(wrapper.get("cik", 0)) != 1158463 or wrapper.get("symbols") != ["JBLU"]:
        raise ValueError("JBLU Company Facts wrapper identity mismatch")
    payload = wrapper.get("payload", {})
    if int(payload.get("cik", 0)) != 1158463:
        raise ValueError("JBLU Company Facts payload CIK mismatch")
    if "JETBLUE AIRWAYS" not in str(payload.get("entityName", "")).upper():
        raise ValueError("JBLU Company Facts issuer mismatch")
    return wrapper


def _matching_fact(
    facts: dict,
    concept: str,
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
    filed: str,
) -> dict:
    matches = [
        item
        for item in facts[concept]["units"]["USD"]
        if pd.Timestamp(item["start"]) == start
        and pd.Timestamp(item["end"]) == end
        and pd.Timestamp(item["filed"]) == pd.Timestamp(filed)
        and item.get("form") in {"10-Q", "10-K", "10-K/A"}
    ]
    if len(matches) != 1:
        raise ValueError(
            f"JBLU {concept} fact is not unique for {start.date()}-{end.date()} "
            f"filed {filed}: {len(matches)}"
        )
    return matches[0]


def _row(
    *,
    fiscal_end: pd.Timestamp,
    available_date: str,
    metric: str,
    value: float,
    concept: str,
    item: dict,
    derivation: str,
) -> dict:
    return {
        "ticker": "JBLU",
        "fiscal_end": fiscal_end,
        "available_date": pd.Timestamp(available_date),
        "metric": metric,
        "value": value,
        "taxonomy": "us-gaap",
        "concept": concept,
        "form": item["form"],
        "accession": item["accn"],
        "derivation": derivation,
    }


def _strict_quarter_rows(payload: dict) -> pd.DataFrame:
    facts = payload["facts"]["us-gaap"]
    for concept in (LEGACY_REVENUE, SUCCESSOR_REVENUE, "NetIncomeLoss"):
        if set(facts[concept]["units"]) != {"USD"}:
            raise ValueError(f"JBLU {concept} must contain only USD facts")

    rows = []
    direct_revenue: dict[pd.Timestamp, float] = {}
    for end, (start, filed, revenue_concept) in DIRECT_QUARTERS.items():
        revenue_item = _matching_fact(
            facts, revenue_concept, start=start, end=end, filed=filed
        )
        revenue = float(revenue_item["val"])
        direct_revenue[end] = revenue
        rows.append(_row(
            fiscal_end=end,
            available_date=filed,
            metric="revenue",
            value=revenue,
            concept=revenue_concept,
            item=revenue_item,
            derivation="direct_three_month_sec_fact",
        ))
        income_item = _matching_fact(
            facts, "NetIncomeLoss", start=start, end=end, filed=filed
        )
        rows.append(_row(
            fiscal_end=end,
            available_date=filed,
            metric="net_income",
            value=float(income_item["val"]),
            concept="NetIncomeLoss",
            item=income_item,
            derivation="direct_three_month_sec_fact",
        ))

    annual_revenue_item = _matching_fact(
        facts,
        SUCCESSOR_REVENUE,
        start=ANNUAL_2018_START,
        end=Q4_2018_END,
        filed=Q4_2018_FILED,
    )
    q4_revenue = float(annual_revenue_item["val"]) - sum(
        direct_revenue[end]
        for end in (
            pd.Timestamp("2018-03-31"),
            pd.Timestamp("2018-06-30"),
            pd.Timestamp("2018-09-30"),
        )
    )
    if q4_revenue <= 0:
        raise ValueError("JBLU derived 2018 fourth-quarter revenue must be positive")
    rows.append(_row(
        fiscal_end=Q4_2018_END,
        available_date=Q4_2018_FILED,
        metric="revenue",
        value=q4_revenue,
        concept=f"derived_q4:{SUCCESSOR_REVENUE}",
        item=annual_revenue_item,
        derivation=(
            "annual_less_originally_filed_q1_q2_q3_across_taxonomy_transition"
        ),
    ))

    q4_income_item = _matching_fact(
        facts,
        "NetIncomeLoss",
        start=Q4_2018_START,
        end=Q4_2018_END,
        filed=Q4_2018_FILED,
    )
    rows.append(_row(
        fiscal_end=Q4_2018_END,
        available_date=Q4_2018_FILED,
        metric="net_income",
        value=float(q4_income_item["val"]),
        concept="NetIncomeLoss",
        item=q4_income_item,
        derivation="direct_three_month_sec_fact",
    ))

    selected = pd.DataFrame(rows).sort_values(
        ["fiscal_end", "metric"]
    ).reset_index(drop=True)
    if (
        len(selected) != 16
        or selected["fiscal_end"].nunique() != 8
        or not selected.groupby("fiscal_end")["metric"].nunique().eq(2).all()
    ):
        raise RuntimeError("JBLU bridge chain is not exactly eight paired quarters")
    return selected


def run(
    *, raw_path: Path = DEFAULT_RAW, output_dir: Path = DEFAULT_OUTPUT_DIR
) -> dict:
    wrapper = _load_wrapper(raw_path)
    facts = _strict_quarter_rows(wrapper["payload"])
    facts["unit"] = "USD"
    facts["source"] = "sec_companyfacts_jblu_revenue_transition"
    facts["source_archive"] = raw_path.name
    facts["source_archive_sha256"] = _sha256(raw_path)

    paired = facts.pivot_table(
        index=["fiscal_end", "available_date"],
        columns="metric",
        values="value",
        aggfunc="first",
    ).reset_index()
    recovered = [
        {
            "ticker": "JBLU",
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
        "ticker": "JBLU",
        "accepted_quarter_count": 8,
        "recovered_quarters": recovered,
        "taxonomy_transition": {
            "legacy_revenue_concept": LEGACY_REVENUE,
            "successor_revenue_concept": SUCCESSOR_REVENUE,
            "restored_fiscal_period": "2017Q1 through 2018Q4",
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
            "Only JBLU USD facts known by each original filing date are used. "
            "2017Q1-2018Q3 and all net income values are direct three-month "
            "facts. 2018Q4 revenue is the original 2018 annual revenue less "
            "the originally filed Q1-Q3 values across the taxonomy transition. "
            "Later comparisons are excluded; formal fundamentals remain unchanged."
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
