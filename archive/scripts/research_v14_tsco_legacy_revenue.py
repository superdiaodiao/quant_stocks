#!/usr/bin/env python3
"""Recover TSCO 2017-2018 quarters across an SEC revenue-taxonomy transition."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd


DEFAULT_RAW = Path(
    "cleaned_stocks_data/financial/sec_companyfacts_cache/CIK0000916365.json.gz"
)
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/tsco_revenue_transition_2017_2018"
)
REVENUE_CONCEPT = "SalesRevenueGoodsNet"
DIRECT_QUARTERS = {
    pd.Timestamp("2017-04-01"): (pd.Timestamp("2017-01-01"), "2017-05-10"),
    pd.Timestamp("2017-07-01"): (pd.Timestamp("2017-04-02"), "2017-08-10"),
    pd.Timestamp("2017-09-30"): (pd.Timestamp("2017-07-02"), "2017-11-09"),
    pd.Timestamp("2018-03-31"): (pd.Timestamp("2017-12-31"), "2018-05-11"),
    pd.Timestamp("2018-06-30"): (pd.Timestamp("2018-04-01"), "2018-08-09"),
    pd.Timestamp("2018-09-29"): (pd.Timestamp("2018-07-01"), "2018-11-08"),
}
ANNUAL_PERIODS = {
    pd.Timestamp("2017-12-30"): {
        "start": pd.Timestamp("2017-01-01"),
        "filed": "2018-02-22",
        "quarter_ends": ["2017-04-01", "2017-07-01", "2017-09-30"],
    },
    pd.Timestamp("2018-12-29"): {
        "start": pd.Timestamp("2017-12-31"),
        "filed": "2019-02-21",
        "quarter_ends": ["2018-03-31", "2018-06-30", "2018-09-29"],
    },
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
    if int(wrapper.get("cik", 0)) != 916365 or wrapper.get("symbols") != ["TSCO"]:
        raise ValueError("TSCO Company Facts wrapper identity mismatch")
    payload = wrapper.get("payload", {})
    if int(payload.get("cik", 0)) != 916365:
        raise ValueError("TSCO Company Facts payload CIK mismatch")
    if "TRACTOR SUPPLY" not in str(payload.get("entityName", "")).upper():
        raise ValueError("TSCO Company Facts issuer mismatch")
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
        item for item in facts[concept]["units"]["USD"]
        if pd.Timestamp(item["start"]) == start
        and pd.Timestamp(item["end"]) == end
        and pd.Timestamp(item["filed"]) == pd.Timestamp(filed)
        and item.get("form") in {"10-Q", "10-K", "10-K/A"}
    ]
    if len(matches) != 1:
        raise ValueError(
            f"TSCO {concept} fact is not unique for {start.date()}-{end.date()} "
            f"filed {filed}: {len(matches)}"
        )
    return matches[0]


def _strict_quarter_rows(payload: dict) -> pd.DataFrame:
    facts = payload["facts"]["us-gaap"]
    for concept in (REVENUE_CONCEPT, "NetIncomeLoss"):
        if set(facts[concept]["units"]) != {"USD"}:
            raise ValueError(f"TSCO {concept} must contain only USD facts")

    rows = []
    direct_values: dict[tuple[pd.Timestamp, str], float] = {}
    for end, (start, filed) in DIRECT_QUARTERS.items():
        for metric, concept in {
            "revenue": REVENUE_CONCEPT,
            "net_income": "NetIncomeLoss",
        }.items():
            item = _matching_fact(
                facts, concept, start=start, end=end, filed=filed
            )
            value = float(item["val"])
            direct_values[(end, metric)] = value
            rows.append({
                "ticker": "TSCO",
                "fiscal_end": end,
                "available_date": pd.Timestamp(filed),
                "metric": metric,
                "value": value,
                "taxonomy": "us-gaap",
                "concept": concept,
                "form": item["form"],
                "accession": item["accn"],
                "derivation": "direct_three_month_sec_fact",
            })

    for end, spec in ANNUAL_PERIODS.items():
        for metric, concept in {
            "revenue": REVENUE_CONCEPT,
            "net_income": "NetIncomeLoss",
        }.items():
            item = _matching_fact(
                facts,
                concept,
                start=spec["start"],
                end=end,
                filed=spec["filed"],
            )
            components = [
                direct_values[(pd.Timestamp(quarter_end), metric)]
                for quarter_end in spec["quarter_ends"]
            ]
            value = float(item["val"]) - sum(components)
            if metric == "revenue" and value <= 0:
                raise ValueError("TSCO derived fourth-quarter revenue must be positive")
            rows.append({
                "ticker": "TSCO",
                "fiscal_end": end,
                "available_date": pd.Timestamp(spec["filed"]),
                "metric": metric,
                "value": value,
                "taxonomy": "us-gaap",
                "concept": f"derived_q4:{concept}",
                "form": item["form"],
                "accession": item["accn"],
                "derivation": "annual_less_originally_filed_q1_q2_q3",
            })

    selected = pd.DataFrame(rows).sort_values(
        ["fiscal_end", "metric"]
    ).reset_index(drop=True)
    if (
        len(selected) != 16
        or selected["fiscal_end"].nunique() != 8
        or not selected.groupby("fiscal_end")["metric"].nunique().eq(2).all()
    ):
        raise RuntimeError("TSCO bridge chain is not exactly eight paired quarters")
    return selected


def run(
    *, raw_path: Path = DEFAULT_RAW, output_dir: Path = DEFAULT_OUTPUT_DIR
) -> dict:
    wrapper = _load_wrapper(raw_path)
    facts = _strict_quarter_rows(wrapper["payload"])
    facts["unit"] = "USD"
    facts["source"] = "sec_companyfacts_tsco_revenue_transition"
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
            "ticker": "TSCO",
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
        "ticker": "TSCO",
        "accepted_quarter_count": 8,
        "recovered_quarters": recovered,
        "taxonomy_transition": {
            "legacy_revenue_concept": REVENUE_CONCEPT,
            "successor_revenue_concept": "Revenues",
            "restored_fiscal_period": "fiscal 2017Q1 through fiscal 2018Q4",
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
            "Only TSCO USD facts known by each original filing date are used. "
            "Q1-Q3 are direct three-month facts; Q4 is annual less those same "
            "year's originally filed Q1-Q3. Later comparisons are excluded and "
            "formal fundamentals remain unchanged."
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
