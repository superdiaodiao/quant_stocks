#!/usr/bin/env python3
"""Recover REG 2018 quarter comparatives from its original 2018 SEC 10-K."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd


DEFAULT_RAW = Path(
    "cleaned_stocks_data/financial/sec_companyfacts_cache/CIK0000910606.json.gz"
)
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/reg_2018_annual_comparatives"
)
FILED = "2019-02-21"
ACCESSION = "0000910606-19-000006"
REVENUE = "Revenues"
NET_INCOME = "NetIncomeLossAvailableToCommonStockholdersBasic"
QUARTERS = {
    pd.Timestamp("2018-03-31"): pd.Timestamp("2018-01-01"),
    pd.Timestamp("2018-06-30"): pd.Timestamp("2018-04-01"),
    pd.Timestamp("2018-09-30"): pd.Timestamp("2018-07-01"),
    pd.Timestamp("2018-12-31"): pd.Timestamp("2018-10-01"),
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
    if int(wrapper.get("cik", 0)) != 910606 or wrapper.get("symbols") != ["REG"]:
        raise ValueError("REG Company Facts wrapper identity mismatch")
    payload = wrapper.get("payload", {})
    if int(payload.get("cik", 0)) != 910606:
        raise ValueError("REG Company Facts payload CIK mismatch")
    if "REGENCY CENTERS" not in str(payload.get("entityName", "")).upper():
        raise ValueError("REG Company Facts issuer mismatch")
    return wrapper


def _matching_fact(
    facts: dict,
    concept: str,
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> dict:
    matches = [
        item
        for item in facts[concept]["units"]["USD"]
        if pd.Timestamp(item["start"]) == start
        and pd.Timestamp(item["end"]) == end
        and pd.Timestamp(item["filed"]) == pd.Timestamp(FILED)
        and item.get("form") == "10-K"
        and item.get("accn") == ACCESSION
    ]
    if len(matches) != 1:
        raise ValueError(
            f"REG {concept} fact is not unique for {start.date()}-{end.date()} "
            f"in {ACCESSION}: {len(matches)}"
        )
    return matches[0]


def _strict_quarter_rows(payload: dict) -> pd.DataFrame:
    facts = payload["facts"]["us-gaap"]
    for concept in (REVENUE, NET_INCOME):
        if set(facts[concept]["units"]) != {"USD"}:
            raise ValueError(f"REG {concept} must contain only USD facts")

    rows = []
    for end, start in QUARTERS.items():
        for metric, concept in (("revenue", REVENUE), ("net_income", NET_INCOME)):
            item = _matching_fact(facts, concept, start=start, end=end)
            rows.append({
                "ticker": "REG",
                "fiscal_end": end,
                "available_date": pd.Timestamp(FILED),
                "metric": metric,
                "value": float(item["val"]),
                "taxonomy": "us-gaap",
                "concept": concept,
                "form": item["form"],
                "accession": item["accn"],
                "derivation": "direct_three_month_annual_comparative_fact",
            })
    selected = pd.DataFrame(rows).sort_values(
        ["fiscal_end", "metric"]
    ).reset_index(drop=True)
    if (
        len(selected) != 8
        or selected["fiscal_end"].nunique() != 4
        or not selected.groupby("fiscal_end")["metric"].nunique().eq(2).all()
    ):
        raise RuntimeError("REG recovery is not exactly four paired quarters")
    return selected


def run(
    *, raw_path: Path = DEFAULT_RAW, output_dir: Path = DEFAULT_OUTPUT_DIR
) -> dict:
    wrapper = _load_wrapper(raw_path)
    facts = _strict_quarter_rows(wrapper["payload"])
    facts["unit"] = "USD"
    facts["source"] = "sec_companyfacts_reg_2018_annual_comparatives"
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
            "ticker": "REG",
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
        "ticker": "REG",
        "accepted_quarter_count": 4,
        "recovered_quarters": recovered,
        "filing_binding": {
            "filed": FILED,
            "accession": ACCESSION,
            "form": "10-K",
            "fiscal_year": 2018,
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
            "Only direct three-month REG facts in the original 2018 10-K filed "
            "2019-02-21 are used. Availability remains the filing date; quarter-end "
            "dates are not treated as knowledge dates and formal financials are unchanged."
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
