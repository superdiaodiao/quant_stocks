#!/usr/bin/env python3
"""Parse TEAM's SEC-filed three-month IFRS statements for v14 research."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd
from bs4 import BeautifulSoup

from scripts.research_v14_sec_filing_exhibit_financials import (
    _parse_accounting_number,
)


DEFAULT_REGISTRY = Path(
    "stocks_list_dir/nasdaq/team_sec_quarterly_filings.csv"
)
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/team_sec_quarterly_filings_2018_2021"
)
NET_LABELS = (
    "Net income (loss)",
    "Net (loss) income",
    "Net loss",
    "Net income",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _period_columns(
    table: pd.DataFrame,
    year: int,
    period_phrase: str = "Three Months Ended",
) -> list[Any]:
    phrase_columns = set()
    year_columns = set()
    for _, row in table.head(8).iterrows():
        for column, value in row.items():
            text = str(value).strip()
            if period_phrase in text:
                phrase_columns.add(column)
            if text == str(year):
                year_columns.add(column)
    selected = [
        column for column in table.columns
        if column in phrase_columns and column in year_columns
    ]
    if not selected:
        raise ValueError(
            f"TEAM filing table has no {period_phrase!r} {year} columns"
        )
    return selected


def _row_value(table: pd.DataFrame, label: str, columns: list[Any]) -> float:
    labels = table.iloc[:, 0].fillna("").astype(str).str.strip()
    rows = table.loc[labels.eq(label)]
    if len(rows) != 1:
        raise ValueError(f"expected one TEAM statement row labelled {label!r}")
    parsed = [
        value
        for column in columns
        if (value := _parse_accounting_number(rows.iloc[0][column])) is not None
    ]
    values = sorted(set(parsed))
    if len(values) != 1:
        raise ValueError(
            f"expected one repeated TEAM value for {label!r}, found {values}"
        )
    return values[0]


def _parse_team_period(
    path: Path,
    fiscal_end: pd.Timestamp,
    period_phrase: str,
) -> dict[str, float]:
    raw = path.read_bytes()
    text = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
    normalized = " ".join(text.split())
    if "U.S. $ in thousands" not in normalized:
        raise ValueError(f"TEAM filing does not prove USD-thousands units: {path}")
    tables = pd.read_html(path)
    matches = []
    for table in tables:
        first = table.iloc[:, 0].fillna("").astype(str).str.strip()
        if (
            first.eq("Total revenues").any()
            and first.isin(NET_LABELS).any()
            and table.astype(str).apply(
                lambda column: column.str.contains(
                    period_phrase, case=False, regex=False
                )
            ).any().any()
        ):
            matches.append(table)
    parsed = []
    for table in matches:
        try:
            columns = _period_columns(table, fiscal_end.year, period_phrase)
        except ValueError:
            continue
        first = table.iloc[:, 0].fillna("").astype(str).str.strip()
        net_labels = [label for label in NET_LABELS if first.eq(label).any()]
        if len(net_labels) != 1:
            continue
        revenue_thousands = _row_value(table, "Total revenues", columns)
        net_income_thousands = _row_value(table, net_labels[0], columns)
        # Percentage-analysis tables repeat the same labels.  TEAM's actual
        # statement revenue is safely above USD 10m in every registered quarter.
        if revenue_thousands > 10_000:
            parsed.append((revenue_thousands, net_income_thousands))
    values = sorted(set(parsed))
    if len(values) != 1:
        raise ValueError(
            f"expected one agreeing TEAM statement value pair in {path}, "
            f"found {values}"
        )
    revenue = values[0][0] * 1000.0
    net_income = values[0][1] * 1000.0
    if revenue <= 0:
        raise ValueError("TEAM quarterly revenue must be positive")
    return {"revenue": revenue, "net_income": net_income}


def parse_team_quarter(path: Path, fiscal_end: pd.Timestamp) -> dict[str, float]:
    return _parse_team_period(path, fiscal_end, "Three Months Ended")


def _longest_chain(ends: list[pd.Timestamp]) -> int:
    ordered = sorted(set(ends))
    longest = current = 1 if ordered else 0
    for left, right in zip(ordered, ordered[1:]):
        if 60 <= (right - left).days <= 135:
            current += 1
            longest = max(longest, current)
        else:
            current = 1
    return longest


def _is_registry_boundary_truncation(
    *,
    fiscal_start: pd.Timestamp,
    fiscal_end: pd.Timestamp,
    registry_start: pd.Timestamp,
) -> bool:
    """Allow only the first partial fiscal year created by the registry start."""
    return fiscal_start < registry_start <= fiscal_end


def run(
    *,
    registry_path: Path = DEFAULT_REGISTRY,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> dict:
    registry = pd.read_csv(
        registry_path,
        dtype={"ticker": str, "cik": int, "accession": str},
        parse_dates=["fiscal_end", "filed_date"],
    )
    if set(registry["ticker"]) != {"TEAM"} or set(registry["cik"]) != {1650372}:
        raise ValueError("TEAM registry contains another issuer")
    if registry.duplicated("fiscal_end").any():
        raise ValueError("TEAM registry contains duplicate quarter ends")
    rows = []
    bindings = []
    recovered = []
    annual_values = {}
    for entry in registry.sort_values("fiscal_end").itertuples(index=False):
        path = Path(entry.local_path)
        values = parse_team_quarter(path, entry.fiscal_end)
        if entry.fiscal_end.month == 6:
            annual_values[entry.fiscal_end] = _parse_team_period(
                path, entry.fiscal_end, "Fiscal Year Ended"
            )
        lag_days = int((entry.filed_date - entry.fiscal_end).days)
        if not 0 <= lag_days <= 150:
            raise ValueError(f"TEAM filing is not timely: {entry.accession}")
        common = {
            "ticker": "TEAM",
            "fiscal_end": entry.fiscal_end,
            "available_date": entry.filed_date,
            "taxonomy": "ifrs-full",
            "form": entry.form,
            "accession": entry.accession,
            "unit": "USD",
            "source": "explicit_sec_filed_three_month_statement",
            "source_archive": path.name,
            "source_archive_sha256": _sha256(path),
            "derivation_prior_accession": "",
        }
        rows.extend([
            {
                **common,
                "metric": "revenue",
                "value": values["revenue"],
                "concept": "RevenueFromContractsWithCustomers",
            },
            {
                **common,
                "metric": "net_income",
                "value": values["net_income"],
                "concept": "ProfitLossAttributableToOwnersOfParent",
            },
        ])
        recovered.append({
            "ticker": "TEAM",
            "fiscal_end": entry.fiscal_end.strftime("%Y-%m-%d"),
            "available_date": entry.filed_date.strftime("%Y-%m-%d"),
            "availability_lag_days": lag_days,
            **values,
            "derivation": "direct_three_month_sec_filing_statement",
        })
        bindings.append({
            "accession": entry.accession,
            "fiscal_end": entry.fiscal_end.strftime("%Y-%m-%d"),
            "filed_date": entry.filed_date.strftime("%Y-%m-%d"),
            "path": str(path),
            "sha256": _sha256(path),
            "source_url": entry.source_url,
        })
    quarters = pd.DataFrame(rows).sort_values(["fiscal_end", "metric"])
    paired_ends = quarters.groupby("fiscal_end")["metric"].nunique()
    paired_ends = paired_ends.loc[paired_ends.eq(2)].index.tolist()
    longest = _longest_chain(paired_ends)
    if longest != len(registry):
        raise RuntimeError(
            f"TEAM SEC filing chain is not fully continuous: {longest}/{len(registry)}"
        )
    annual_cross_checks = []
    annual_boundary_truncations = []
    quarter_frame = pd.DataFrame(recovered)
    quarter_frame["fiscal_end"] = pd.to_datetime(quarter_frame["fiscal_end"])
    registry_start = quarter_frame["fiscal_end"].min()
    for fiscal_end, annual in sorted(annual_values.items()):
        fiscal_start = fiscal_end - pd.DateOffset(years=1) + pd.Timedelta(days=1)
        fiscal_rows = quarter_frame.loc[
            quarter_frame["fiscal_end"].between(fiscal_start, fiscal_end)
        ]
        if len(fiscal_rows) != 4:
            if _is_registry_boundary_truncation(
                fiscal_start=fiscal_start,
                fiscal_end=fiscal_end,
                registry_start=registry_start,
            ):
                annual_boundary_truncations.append({
                    "fiscal_end": fiscal_end.strftime("%Y-%m-%d"),
                    "registered_quarter_count": len(fiscal_rows),
                    "reason": "registry_starts_inside_fiscal_year",
                })
                continue
            raise RuntimeError(
                f"TEAM fiscal year {fiscal_end.date()} has {len(fiscal_rows)} quarters"
            )
        observed = {
            metric: float(fiscal_rows[metric].sum())
            for metric in ("revenue", "net_income")
        }
        if observed != annual:
            raise RuntimeError(
                f"TEAM quarter sum differs from filed annual values for "
                f"{fiscal_end.date()}: {observed} != {annual}"
            )
        annual_cross_checks.append({
            "fiscal_end": fiscal_end.strftime("%Y-%m-%d"),
            "quarter_sum": observed,
            "filed_annual": annual,
            "exact_match": True,
        })

    output_dir.mkdir(parents=True, exist_ok=True)
    quarters_path = output_dir / "strict_quarterly_facts.csv"
    quarters.to_csv(quarters_path, index=False)
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "promotion_eligible": False,
        "release_status": "BLOCKED",
        "ticker": "TEAM",
        "currency": "USD",
        "quarter_count": len(registry),
        "longest_continuous_timely_paired_quarters": longest,
        "recovered_quarters": recovered,
        "annual_cross_checks": annual_cross_checks,
        "annual_boundary_truncations": annual_boundary_truncations,
        "registry": {
            "path": str(registry_path), "sha256": _sha256(registry_path)
        },
        "filings": bindings,
        "outputs": {
            "quarters": {
                "path": str(quarters_path), "sha256": _sha256(quarters_path)
            }
        },
        "guardrail": (
            "Only direct SEC-filed three-month IFRS statements are accepted. "
            "This research artifact does not modify formal fundamentals or authorize trading."
        ),
    }
    manifest = output_dir / "manifest.json"
    manifest.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["manifest"] = str(manifest)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = run(registry_path=args.registry, output_dir=args.output_dir)
    print(json.dumps({
        "manifest": result["manifest"],
        "quarter_count": result["quarter_count"],
        "longest_continuous_timely_paired_quarters": result[
            "longest_continuous_timely_paired_quarters"
        ],
        "promotion_eligible": False,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
