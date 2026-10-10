#!/usr/bin/env python3
"""Recover BANR 2017-2018 bank revenue from its original SEC filings."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd


DEFAULT_RAW = Path(
    "cleaned_stocks_data/financial/sec_companyfacts_cache/CIK0000946673.json.gz"
)
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/banr_legacy_bank_revenue_2017_2021"
)
NET_INTEREST = "InterestIncomeExpenseNet"
NONINTEREST = "OtherOperatingIncome"
NET_INCOME = "NetIncomeLoss"
DIRECT_QUARTERS = {
    pd.Timestamp("2017-03-31"): (pd.Timestamp("2017-01-01"), "2017-05-05"),
    pd.Timestamp("2017-06-30"): (pd.Timestamp("2017-04-01"), "2017-08-04"),
    pd.Timestamp("2017-09-30"): (pd.Timestamp("2017-07-01"), "2017-11-06"),
    pd.Timestamp("2018-03-31"): (pd.Timestamp("2018-01-01"), "2018-05-04"),
    pd.Timestamp("2018-06-30"): (pd.Timestamp("2018-04-01"), "2018-08-03"),
    pd.Timestamp("2018-09-30"): (pd.Timestamp("2018-07-01"), "2018-11-02"),
    pd.Timestamp("2019-03-31"): (pd.Timestamp("2019-01-01"), "2019-05-03"),
    pd.Timestamp("2019-06-30"): (pd.Timestamp("2019-04-01"), "2019-08-02"),
    pd.Timestamp("2019-09-30"): (pd.Timestamp("2019-07-01"), "2019-11-01"),
    pd.Timestamp("2020-03-31"): (pd.Timestamp("2020-01-01"), "2020-05-07"),
    pd.Timestamp("2020-06-30"): (pd.Timestamp("2020-04-01"), "2020-08-05"),
    pd.Timestamp("2020-09-30"): (pd.Timestamp("2020-07-01"), "2020-11-05"),
    pd.Timestamp("2021-03-31"): (pd.Timestamp("2021-01-01"), "2021-05-05"),
    pd.Timestamp("2021-06-30"): (pd.Timestamp("2021-04-01"), "2021-08-05"),
    pd.Timestamp("2021-09-30"): (pd.Timestamp("2021-07-01"), "2021-11-04"),
}
ANNUAL_PERIODS = {
    pd.Timestamp("2017-12-31"): {
        "start": pd.Timestamp("2017-01-01"),
        "filed": "2018-02-23",
        "quarter_ends": ["2017-03-31", "2017-06-30", "2017-09-30"],
    },
    pd.Timestamp("2018-12-31"): {
        "start": pd.Timestamp("2018-01-01"),
        "filed": "2019-02-26",
        "quarter_ends": ["2018-03-31", "2018-06-30", "2018-09-30"],
    },
    pd.Timestamp("2019-12-31"): {
        "start": pd.Timestamp("2019-01-01"),
        "filed": "2020-02-21",
        "quarter_ends": ["2019-03-31", "2019-06-30", "2019-09-30"],
    },
    pd.Timestamp("2020-12-31"): {
        "start": pd.Timestamp("2020-01-01"),
        "filed": "2021-02-23",
        "quarter_ends": ["2020-03-31", "2020-06-30", "2020-09-30"],
    },
    pd.Timestamp("2021-12-31"): {
        "start": pd.Timestamp("2021-01-01"),
        "filed": "2022-02-24",
        "quarter_ends": ["2021-03-31", "2021-06-30", "2021-09-30"],
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
    if int(wrapper.get("cik", 0)) != 946673 or wrapper.get("symbols") != ["BANR"]:
        raise ValueError("BANR Company Facts wrapper identity mismatch")
    payload = wrapper.get("payload", {})
    if int(payload.get("cik", 0)) != 946673:
        raise ValueError("BANR Company Facts payload CIK mismatch")
    if "BANNER CORPORATION" not in str(payload.get("entityName", "")).upper():
        raise ValueError("BANR Company Facts issuer mismatch")
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
            f"BANR {concept} fact is not unique for {start.date()}-{end.date()} "
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
        "ticker": "BANR",
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
    for concept in (NET_INTEREST, NONINTEREST, NET_INCOME):
        if set(facts[concept]["units"]) != {"USD"}:
            raise ValueError(f"BANR {concept} must contain only USD facts")

    rows = []
    direct_components: dict[pd.Timestamp, dict[str, float]] = {}
    for end, (start, filed) in DIRECT_QUARTERS.items():
        net_interest = _matching_fact(
            facts, NET_INTEREST, start=start, end=end, filed=filed
        )
        noninterest = _matching_fact(
            facts, NONINTEREST, start=start, end=end, filed=filed
        )
        direct_components[end] = {
            NET_INTEREST: float(net_interest["val"]),
            NONINTEREST: float(noninterest["val"]),
        }
        rows.append(_row(
            fiscal_end=end,
            available_date=filed,
            metric="revenue",
            value=float(net_interest["val"]) + float(noninterest["val"]),
            concept=f"derived_bank_revenue:{NET_INTEREST}+{NONINTEREST}",
            item=net_interest,
            derivation="sum_original_three_month_bank_income_components",
        ))
        income = _matching_fact(
            facts, NET_INCOME, start=start, end=end, filed=filed
        )
        rows.append(_row(
            fiscal_end=end,
            available_date=filed,
            metric="net_income",
            value=float(income["val"]),
            concept=NET_INCOME,
            item=income,
            derivation="direct_three_month_sec_fact",
        ))

    for end, spec in ANNUAL_PERIODS.items():
        filed = str(spec["filed"])
        q4_components = {}
        annual_items = {}
        for concept in (NET_INTEREST, NONINTEREST):
            annual = _matching_fact(
                facts,
                concept,
                start=spec["start"],
                end=end,
                filed=filed,
            )
            annual_items[concept] = annual
            q4_components[concept] = float(annual["val"]) - sum(
                direct_components[pd.Timestamp(quarter_end)][concept]
                for quarter_end in spec["quarter_ends"]
            )
        revenue = q4_components[NET_INTEREST] + q4_components[NONINTEREST]
        if revenue <= 0:
            raise ValueError(f"BANR derived revenue must be positive for {end.date()}")
        rows.append(_row(
            fiscal_end=end,
            available_date=filed,
            metric="revenue",
            value=revenue,
            concept=f"derived_bank_revenue:{NET_INTEREST}+{NONINTEREST}",
            item=annual_items[NET_INTEREST],
            derivation="annual_bank_income_components_less_original_q1_q2_q3",
        ))
        annual_income = _matching_fact(
            facts,
            NET_INCOME,
            start=spec["start"],
            end=end,
            filed=filed,
        )
        q4_income = float(annual_income["val"]) - sum(
            float(_matching_fact(
                facts,
                NET_INCOME,
                start=DIRECT_QUARTERS[pd.Timestamp(quarter_end)][0],
                end=pd.Timestamp(quarter_end),
                filed=DIRECT_QUARTERS[pd.Timestamp(quarter_end)][1],
            )["val"])
            for quarter_end in spec["quarter_ends"]
        )
        rows.append(_row(
            fiscal_end=end,
            available_date=filed,
            metric="net_income",
            value=q4_income,
            concept=NET_INCOME,
            item=annual_income,
            derivation="annual_net_income_less_original_q1_q2_q3",
        ))

    selected = pd.DataFrame(rows).sort_values(
        ["fiscal_end", "metric"]
    ).reset_index(drop=True)
    if (
        len(selected) != 40
        or selected["fiscal_end"].nunique() != 20
        or not selected.groupby("fiscal_end")["metric"].nunique().eq(2).all()
    ):
        raise RuntimeError("BANR recovery is not exactly twenty paired quarters")
    return selected


def run(
    *, raw_path: Path = DEFAULT_RAW, output_dir: Path = DEFAULT_OUTPUT_DIR
) -> dict:
    wrapper = _load_wrapper(raw_path)
    facts = _strict_quarter_rows(wrapper["payload"])
    facts["unit"] = "USD"
    facts["source"] = "sec_companyfacts_banr_legacy_bank_revenue"
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
            "ticker": "BANR",
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
        "ticker": "BANR",
        "accepted_quarter_count": 20,
        "recovered_quarters": recovered,
        "bank_revenue_mapping": {
            "net_interest_income_concept": NET_INTEREST,
            "noninterest_income_concept": NONINTEREST,
            "restored_fiscal_period": "2017Q1 through 2021Q4",
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
            "Revenue is net interest income plus the issuer's OtherOperatingIncome "
            "bank noninterest-income line. Q1-Q3 use each original quarterly filing; "
            "Q4 revenue components and net income use the original annual filing less those original Q1-Q3 "
            "values. Later restatements are excluded and formal financials are unchanged."
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
