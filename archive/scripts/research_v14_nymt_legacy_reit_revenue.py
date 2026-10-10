#!/usr/bin/env python3
"""Recover NYMT 2017-2021 mortgage-REIT revenue from original SEC filings."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd


DEFAULT_RAW = Path(
    "cleaned_stocks_data/financial/sec_companyfacts_cache/CIK0001273685.json.gz"
)
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/nymt_legacy_reit_revenue_2017_2021"
)
NET_INTEREST = "InterestIncomeExpenseNet"
OTHER_INCOME_CONCEPTS = (
    "OtherOperatingIncomeExpenseNet",
    "NoninterestIncome",
)
OPERATING_INCOME = "OperatingIncomeLoss"
OPERATING_EXPENSES = "OperatingExpenses"
NET_INCOME = "NetIncomeLoss"
DIRECT_QUARTERS = {
    pd.Timestamp("2017-03-31"): (pd.Timestamp("2017-01-01"), "2017-05-09"),
    pd.Timestamp("2017-06-30"): (pd.Timestamp("2017-04-01"), "2017-08-07"),
    pd.Timestamp("2017-09-30"): (pd.Timestamp("2017-07-01"), "2017-11-07"),
    pd.Timestamp("2018-03-31"): (pd.Timestamp("2018-01-01"), "2018-05-08"),
    pd.Timestamp("2018-06-30"): (pd.Timestamp("2018-04-01"), "2018-08-06"),
    pd.Timestamp("2018-09-30"): (pd.Timestamp("2018-07-01"), "2018-11-06"),
    pd.Timestamp("2019-03-31"): (pd.Timestamp("2019-01-01"), "2019-05-07"),
    pd.Timestamp("2019-06-30"): (pd.Timestamp("2019-04-01"), "2019-08-06"),
    pd.Timestamp("2019-09-30"): (pd.Timestamp("2019-07-01"), "2019-11-07"),
    pd.Timestamp("2020-03-31"): (pd.Timestamp("2020-01-01"), "2020-05-26"),
    pd.Timestamp("2020-06-30"): (pd.Timestamp("2020-04-01"), "2020-08-07"),
    pd.Timestamp("2020-09-30"): (pd.Timestamp("2020-07-01"), "2020-11-06"),
    pd.Timestamp("2021-03-31"): (pd.Timestamp("2021-01-01"), "2021-05-07"),
    pd.Timestamp("2021-06-30"): (pd.Timestamp("2021-04-01"), "2021-08-06"),
    pd.Timestamp("2021-09-30"): (pd.Timestamp("2021-07-01"), "2021-11-04"),
}
ANNUAL_PERIODS = {
    pd.Timestamp("2017-12-31"): {
        "start": pd.Timestamp("2017-01-01"),
        "filed": "2018-02-27",
        "quarter_ends": ["2017-03-31", "2017-06-30", "2017-09-30"],
    },
    pd.Timestamp("2018-12-31"): {
        "start": pd.Timestamp("2018-01-01"),
        "filed": "2019-02-25",
        "quarter_ends": ["2018-03-31", "2018-06-30", "2018-09-30"],
    },
    pd.Timestamp("2019-12-31"): {
        "start": pd.Timestamp("2019-01-01"),
        "filed": "2020-02-28",
        "quarter_ends": ["2019-03-31", "2019-06-30", "2019-09-30"],
    },
    pd.Timestamp("2020-12-31"): {
        "start": pd.Timestamp("2020-01-01"),
        "filed": "2021-02-26",
        "quarter_ends": ["2020-03-31", "2020-06-30", "2020-09-30"],
    },
    pd.Timestamp("2021-12-31"): {
        "start": pd.Timestamp("2021-01-01"),
        "filed": "2022-02-25",
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
    if int(wrapper.get("cik", 0)) != 1273685 or "NYMT" not in wrapper.get(
        "symbols", []
    ):
        raise ValueError("NYMT Company Facts wrapper identity mismatch")
    payload = wrapper.get("payload", {})
    if int(payload.get("cik", 0)) != 1273685:
        raise ValueError("NYMT Company Facts payload CIK mismatch")
    if str(payload.get("entityName", "")).upper() not in {
        "ADAMAS TRUST, INC.",
        "NEW YORK MORTGAGE TRUST, INC.",
    }:
        raise ValueError("NYMT Company Facts issuer mismatch")
    return wrapper


def _matches(
    facts: dict,
    concept: str,
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
    filed: str,
) -> list[dict]:
    concept_facts = facts.get(concept, {}).get("units", {}).get("USD", [])
    return [
        item
        for item in concept_facts
        if pd.Timestamp(item["start"]) == start
        and pd.Timestamp(item["end"]) == end
        and pd.Timestamp(item["filed"]) == pd.Timestamp(filed)
        and item.get("form") in {"10-Q", "10-K", "10-K/A"}
    ]


def _matching_fact(
    facts: dict,
    concept: str,
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
    filed: str,
) -> dict:
    matches = _matches(
        facts, concept, start=start, end=end, filed=filed
    )
    if len(matches) != 1:
        raise ValueError(
            f"NYMT {concept} fact is not unique for {start.date()}-{end.date()} "
            f"filed {filed}: {len(matches)}"
        )
    return matches[0]


def _other_income_fact(
    facts: dict,
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
    filed: str,
) -> tuple[str, dict]:
    matches = [
        (concept, item)
        for concept in OTHER_INCOME_CONCEPTS
        for item in _matches(
            facts, concept, start=start, end=end, filed=filed
        )
    ]
    if len(matches) != 1:
        raise ValueError(
            "NYMT other operating income fact is not unique for "
            f"{start.date()}-{end.date()} filed {filed}: {len(matches)}"
        )
    return matches[0]


def _validate_operating_identity(
    facts: dict,
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
    filed: str,
    revenue: float,
) -> bool:
    income = _matches(
        facts, OPERATING_INCOME, start=start, end=end, filed=filed
    )
    expenses = _matches(
        facts, OPERATING_EXPENSES, start=start, end=end, filed=filed
    )
    if not income and not expenses:
        return False
    if len(income) != 1 or len(expenses) != 1:
        return False
    identity_value = float(income[0]["val"]) + float(expenses[0]["val"])
    if identity_value != revenue:
        raise ValueError(
            "NYMT bank revenue components fail operating-statement identity "
            f"for {start.date()}-{end.date()} filed {filed}: "
            f"{revenue} != {identity_value}"
        )
    return True


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
        "ticker": "NYMT",
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


def _strict_quarter_rows(payload: dict) -> tuple[pd.DataFrame, int]:
    facts = payload["facts"]["us-gaap"]
    for concept in (NET_INTEREST, NET_INCOME):
        if set(facts[concept]["units"]) != {"USD"}:
            raise ValueError(f"NYMT {concept} must contain only USD facts")

    rows = []
    identity_checks = 0
    direct_components: dict[pd.Timestamp, dict[str, float]] = {}
    direct_income: dict[pd.Timestamp, float] = {}
    for end, (start, filed) in DIRECT_QUARTERS.items():
        net_interest = _matching_fact(
            facts, NET_INTEREST, start=start, end=end, filed=filed
        )
        other_concept, other_income = _other_income_fact(
            facts, start=start, end=end, filed=filed
        )
        revenue = float(net_interest["val"]) + float(other_income["val"])
        if _validate_operating_identity(
            facts, start=start, end=end, filed=filed, revenue=revenue
        ):
            identity_checks += 1
        direct_components[end] = {
            NET_INTEREST: float(net_interest["val"]),
            "other_income": float(other_income["val"]),
        }
        rows.append(_row(
            fiscal_end=end,
            available_date=filed,
            metric="revenue",
            value=revenue,
            concept=f"derived_reit_revenue:{NET_INTEREST}+{other_concept}",
            item=net_interest,
            derivation="sum_original_three_month_reit_income_components",
        ))
        income = _matching_fact(
            facts, NET_INCOME, start=start, end=end, filed=filed
        )
        direct_income[end] = float(income["val"])
        rows.append(_row(
            fiscal_end=end,
            available_date=filed,
            metric="net_income",
            value=direct_income[end],
            concept=NET_INCOME,
            item=income,
            derivation="direct_three_month_sec_fact",
        ))

    for end, spec in ANNUAL_PERIODS.items():
        filed = str(spec["filed"])
        net_interest = _matching_fact(
            facts, NET_INTEREST, start=spec["start"], end=end, filed=filed
        )
        other_concept, other_income = _other_income_fact(
            facts, start=spec["start"], end=end, filed=filed
        )
        annual_revenue = float(net_interest["val"]) + float(other_income["val"])
        if _validate_operating_identity(
            facts,
            start=spec["start"],
            end=end,
            filed=filed,
            revenue=annual_revenue,
        ):
            identity_checks += 1
        quarter_ends = [pd.Timestamp(value) for value in spec["quarter_ends"]]
        q4_revenue = annual_revenue - sum(
            direct_components[quarter_end][NET_INTEREST]
            + direct_components[quarter_end]["other_income"]
            for quarter_end in quarter_ends
        )
        rows.append(_row(
            fiscal_end=end,
            available_date=filed,
            metric="revenue",
            value=q4_revenue,
            concept=f"derived_reit_revenue:{NET_INTEREST}+{other_concept}",
            item=net_interest,
            derivation="annual_reit_income_components_less_original_q1_q2_q3",
        ))
        annual_income = _matching_fact(
            facts, NET_INCOME, start=spec["start"], end=end, filed=filed
        )
        rows.append(_row(
            fiscal_end=end,
            available_date=filed,
            metric="net_income",
            value=float(annual_income["val"])
            - sum(direct_income[quarter_end] for quarter_end in quarter_ends),
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
        raise RuntimeError("NYMT recovery is not exactly twenty paired quarters")
    return selected, identity_checks


def run(
    *, raw_path: Path = DEFAULT_RAW, output_dir: Path = DEFAULT_OUTPUT_DIR
) -> dict:
    wrapper = _load_wrapper(raw_path)
    facts, identity_checks = _strict_quarter_rows(wrapper["payload"])
    facts["unit"] = "USD"
    facts["source"] = "sec_companyfacts_nymt_legacy_reit_revenue"
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
            "ticker": "NYMT",
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
        "ticker": "NYMT",
        "accepted_quarter_count": 20,
        "operating_statement_identity_checks": identity_checks,
        "recovered_quarters": recovered,
        "revenue_mapping": {
            "net_interest_income_concept": NET_INTEREST,
            "other_income_concepts_by_original_filing": list(
                OTHER_INCOME_CONCEPTS
            ),
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
            "Revenue is net interest income plus the single other-operating-income "
            "concept present in each original filing. Every available operating-income "
            "plus operating-expense identity must match exactly. Q1-Q3 use original "
            "10-Q facts; Q4 uses the original 10-K less those original Q1-Q3 values. "
            "Later comparative filings and formal financial files are unchanged."
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
        "operating_statement_identity_checks": result[
            "operating_statement_identity_checks"
        ],
        "release_status": result["release_status"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
