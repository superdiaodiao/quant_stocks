#!/usr/bin/env python3
"""Recover JD quarters from SEC-filed RMB GAAP consolidated statements."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

import pandas as pd
from bs4 import BeautifulSoup

from scripts.research_v14_sec_filing_exhibit_financials import (
    _parse_accounting_number,
)
from scripts.research_v14_team_sec_quarterly_filings import _longest_chain


DEFAULT_REGISTRY = Path("stocks_list_dir/nasdaq/jd_quarterly_reports.csv")
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/jd_sec_quarterly_reports_2017_2021"
)
NET_LABELS = ("Net income", "Net loss", "Net income/(loss)")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normal(value: Any) -> str:
    return " ".join(str(value).replace("\xa0", " ").split()).casefold()


def _statement_tables(path: Path) -> tuple[pd.DataFrame, pd.DataFrame, str]:
    document = " ".join(
        BeautifulSoup(path.read_bytes(), "html.parser")
        .get_text(" ", strip=True)
        .split()
    )
    if not re.search(r"in thousands", document, flags=re.IGNORECASE):
        raise ValueError(f"JD filing does not prove thousand-unit statements: {path}")
    revenue_candidates = []
    net_candidates: list[tuple[pd.DataFrame, str]] = []
    for table in pd.read_html(path):
        first = table.iloc[:, 0].fillna("").map(_normal)
        header = " ".join(_normal(value) for value in table.head(5).to_numpy().ravel())
        if "months ended" not in header:
            continue
        if (
            first.eq("total net revenues").any()
            and first.str.contains("cost of revenues", regex=False).any()
            and first.str.contains("before tax", regex=False).any()
        ):
            revenue_candidates.append(table)
        labels = [label for label in NET_LABELS if first.eq(_normal(label)).any()]
        if (
            len(labels) == 1
            and first.str.contains("income tax", regex=False).any()
            and first.str.contains("ordinary shareholders", regex=False).any()
        ):
            net_candidates.append((table, labels[0]))
    if len(revenue_candidates) != 1 or len(net_candidates) != 1:
        raise ValueError(
            f"expected one JD GAAP revenue/net statement in {path}, found "
            f"{len(revenue_candidates)}/{len(net_candidates)}"
        )
    return revenue_candidates[0], net_candidates[0][0], net_candidates[0][1]


def _columns(table: pd.DataFrame, *, year: int, period_phrase: str) -> list[Any]:
    selected = []
    phrase = _normal(period_phrase)
    for column in table.columns:
        headers = [_normal(value) for value in table[column].head(5)]
        if (
            any(phrase in value for value in headers)
            and any(re.search(rf"(?:^|\D){year}(?:\D|$)", value) for value in headers)
            and any(value.startswith("rmb") for value in headers)
        ):
            selected.append(column)
    if not selected:
        raise ValueError(f"JD statement has no RMB {period_phrase} {year} columns")
    return selected


def _row_value(table: pd.DataFrame, label: str, columns: list[Any]) -> float:
    first = table.iloc[:, 0].fillna("").map(_normal)
    rows = table.loc[first.eq(_normal(label))]
    if len(rows) != 1:
        raise ValueError(f"expected one JD row for {label!r}")
    values = sorted({
        value for column in columns
        if (value := _parse_accounting_number(rows.iloc[0][column])) is not None
    })
    if len(values) != 1:
        raise ValueError(f"expected one repeated JD RMB value for {label!r}: {values}")
    return round(values[0] * 1_000.0, 2)


def parse_jd_quarter(path: Path, *, fiscal_year: int, fiscal_quarter: int) -> dict[str, Any]:
    revenue_table, net_table, net_label = _statement_tables(path)
    current_revenue_columns = _columns(
        revenue_table, year=fiscal_year, period_phrase="For the three months ended"
    )
    current_net_columns = _columns(
        net_table, year=fiscal_year, period_phrase="For the three months ended"
    )
    prior_revenue_columns = _columns(
        revenue_table, year=fiscal_year - 1, period_phrase="For the three months ended"
    )
    prior_net_columns = _columns(
        net_table, year=fiscal_year - 1, period_phrase="For the three months ended"
    )
    current = {
        "revenue": _row_value(revenue_table, "Total net revenues", current_revenue_columns),
        "net_income": _row_value(net_table, net_label, current_net_columns),
    }
    prior = {
        "revenue": _row_value(revenue_table, "Total net revenues", prior_revenue_columns),
        "net_income": _row_value(net_table, net_label, prior_net_columns),
    }
    annual = None
    if fiscal_quarter == 4:
        annual_revenue_columns = _columns(
            revenue_table, year=fiscal_year, period_phrase="For the year ended"
        )
        annual_net_columns = _columns(
            net_table, year=fiscal_year, period_phrase="For the year ended"
        )
        annual = {
            "revenue": _row_value(revenue_table, "Total net revenues", annual_revenue_columns),
            "net_income": _row_value(net_table, net_label, annual_net_columns),
        }
    return {
        "current": current, "prior_year_comparison": prior,
        "annual": annual, "reported_net_label": net_label,
    }


def _values_agree(left: dict[str, float], right: dict[str, float]) -> bool:
    return all(
        math.isclose(left[metric], right[metric], rel_tol=0, abs_tol=0.01)
        for metric in ("revenue", "net_income")
    )


def run(
    *, registry_path: Path = DEFAULT_REGISTRY, output_dir: Path = DEFAULT_OUTPUT_DIR
) -> dict[str, Any]:
    registry = pd.read_csv(
        registry_path,
        dtype={"ticker": str, "cik": int, "accession": str},
        parse_dates=["fiscal_end", "available_date"],
    )
    if set(registry["ticker"]) != {"JD"} or set(registry["cik"]) != {1549802}:
        raise ValueError("JD registry contains another issuer")
    expected = {(year, quarter) for year in range(2017, 2022) for quarter in range(1, 5)}
    observed = set(zip(registry["fiscal_year"], registry["fiscal_quarter"]))
    if observed != expected or len(registry) != 20:
        raise ValueError("JD registry is not the complete 2017Q1-2021Q4 grid")
    if registry.duplicated(["fiscal_year", "fiscal_quarter"]).any():
        raise ValueError("JD registry contains duplicate fiscal quarters")

    rows = []
    recovered = []
    bindings = []
    parsed_by_slot = {}
    restatement_entry = registry.loc[
        registry["fiscal_year"].eq(2018) & registry["fiscal_quarter"].eq(1)
    ].iloc[0]
    restatement_path = Path(restatement_entry["local_path"])
    restatement_parsed = parse_jd_quarter(
        restatement_path, fiscal_year=2018, fiscal_quarter=1
    )
    restatement_audit = None
    for entry in registry.sort_values(["fiscal_year", "fiscal_quarter"]).itertuples(index=False):
        path = Path(entry.local_path)
        parsed = parse_jd_quarter(
            path, fiscal_year=int(entry.fiscal_year), fiscal_quarter=int(entry.fiscal_quarter)
        )
        values = dict(parsed["current"])
        fact_available_date = entry.available_date
        fact_accession = entry.accession
        fact_form = entry.form
        fact_path = path
        source = "explicit_sec_filed_jd_gaap_rmb_consolidated_statement"
        derivation_prior_accession = ""
        if int(entry.fiscal_year) == 2017 and int(entry.fiscal_quarter) == 1:
            restated = dict(restatement_parsed["prior_year_comparison"])
            if values["net_income"] != restated["net_income"]:
                raise RuntimeError("JD 2017Q1 restatement unexpectedly changes net income")
            restatement_audit = {
                "slot": "2017Q1",
                "original_value": values,
                "restated_value": restated,
                "difference": {
                    metric: restated[metric] - values[metric]
                    for metric in ("revenue", "net_income")
                },
                "original_accession": entry.accession,
                "restatement_accession": restatement_entry["accession"],
                "restatement_available_date": pd.Timestamp(
                    restatement_entry["available_date"]
                ).strftime("%Y-%m-%d"),
                "backdated": False,
            }
            values = restated
            fact_available_date = pd.Timestamp(restatement_entry["available_date"])
            fact_accession = restatement_entry["accession"]
            fact_form = restatement_entry["form"]
            fact_path = restatement_path
            source = "later_sec_filed_jd_comparative_restatement"
            derivation_prior_accession = entry.accession
        if values["revenue"] <= 0:
            raise ValueError(f"JD quarterly revenue is not positive: {entry.accession}")
        lag_days = int((fact_available_date - entry.fiscal_end).days)
        if not 0 <= lag_days <= 90 and derivation_prior_accession == "":
            raise ValueError(f"JD report is not timely: {entry.accession}")
        common = {
            "ticker": "JD", "fiscal_end": entry.fiscal_end,
            "available_date": fact_available_date, "taxonomy": "us-gaap",
            "form": fact_form, "accession": fact_accession, "unit": "CNY",
            "source": source,
            "source_archive": fact_path.name,
            "source_archive_sha256": _sha256(fact_path),
            "derivation_prior_accession": derivation_prior_accession,
        }
        for metric, concept in (("revenue", "Revenue"), ("net_income", "ProfitLoss")):
            rows.append({**common, "metric": metric, "value": values[metric], "concept": concept})
        recovered.append({
            "ticker": "JD", "fiscal_year": int(entry.fiscal_year),
            "fiscal_quarter": int(entry.fiscal_quarter),
            "fiscal_end": entry.fiscal_end.strftime("%Y-%m-%d"),
            "available_date": fact_available_date.strftime("%Y-%m-%d"),
            "availability_lag_days": lag_days, **values,
            "reported_net_label": parsed["reported_net_label"],
            "derivation": (
                "later_comparative_restatement_bound_to_actual_filing_date"
                if derivation_prior_accession
                else "direct_three_month_sec_filed_gaap_rmb_statement"
            ),
            "accession": fact_accession,
        })
        parsed_by_slot[(int(entry.fiscal_year), int(entry.fiscal_quarter))] = parsed
        bindings.append({
            "accession": entry.accession,
            "fiscal_end": entry.fiscal_end.strftime("%Y-%m-%d"),
            "available_date": entry.available_date.strftime("%Y-%m-%d"),
            "path": str(path), "sha256": _sha256(path),
            "source_url": entry.source_url,
            "availability_evidence": "sec_filing_date",
        })

    frame = pd.DataFrame(recovered)
    comparator_checks = []
    for (year, quarter), parsed in sorted(parsed_by_slot.items()):
        if year == 2017:
            continue
        original = frame.loc[
            frame["fiscal_year"].eq(year - 1) & frame["fiscal_quarter"].eq(quarter)
        ].iloc[0]
        original_values = {metric: float(original[metric]) for metric in ("revenue", "net_income")}
        comparison = parsed["prior_year_comparison"]
        if not _values_agree(original_values, comparison):
            raise RuntimeError(f"JD {year}Q{quarter} prior comparator disagrees")
        comparator_checks.append({
            "reporting_slot": f"{year}Q{quarter}",
            "compared_slot": f"{year - 1}Q{quarter}",
            "later_report_comparison": comparison,
            "contemporaneous_original": original_values,
            "exact_match": True, "later_comparison_used_to_replace_original": False,
        })

    annual_checks = []
    for year in range(2017, 2022):
        year_rows = frame.loc[frame["fiscal_year"].eq(year)]
        quarter_sum = {
            metric: float(year_rows[metric].sum()) for metric in ("revenue", "net_income")
        }
        annual = parsed_by_slot[(year, 4)]["annual"]
        if annual is None or not _values_agree(quarter_sum, annual):
            raise RuntimeError(f"JD {year} quarter sum disagrees with annual")
        annual_checks.append({
            "fiscal_year": year, "quarter_sum": quarter_sum,
            "q4_reported_annual": annual, "exact_match": True,
        })

    quarters = pd.DataFrame(rows).sort_values(["fiscal_end", "metric"])
    paired = quarters.groupby("fiscal_end")["metric"].nunique()
    longest = _longest_chain(paired.loc[paired.eq(2)].index.tolist())
    if longest != 20:
        raise RuntimeError(f"JD timely paired quarterly chain is not continuous: {longest}/20")
    output_dir.mkdir(parents=True, exist_ok=True)
    quarters_path = output_dir / "strict_quarterly_facts.csv"
    quarters.to_csv(quarters_path, index=False)
    report = {
        "schema_version": 1, "research_only": True, "point_in_time_proven": True,
        "promotion_eligible": False, "release_status": "BLOCKED",
        "ticker": "JD", "currency": "CNY", "quarter_count": 20,
        "longest_continuous_timely_paired_quarters": longest,
        "recovered_quarters": recovered,
        "restatement_audit": restatement_audit,
        "prior_year_cross_checks": comparator_checks,
        "annual_cross_checks": annual_checks,
        "registry": {"path": str(registry_path), "sha256": _sha256(registry_path)},
        "reports": bindings,
        "outputs": {"quarters": {"path": str(quarters_path), "sha256": _sha256(quarters_path)}},
        "guardrail": (
            "Only direct three-month GAAP Total net revenues and consolidated Net "
            "income/loss in RMB thousands are accepted. USD convenience translations, "
            "non-GAAP profit, and ordinary-shareholder reconciliation rows are excluded."
        ),
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report["manifest"] = str(manifest_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = run(registry_path=args.registry, output_dir=args.output_dir)
    print(json.dumps({
        "manifest": result["manifest"], "quarter_count": result["quarter_count"],
        "longest_continuous_timely_paired_quarters": result["longest_continuous_timely_paired_quarters"],
        "release_status": result["release_status"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
