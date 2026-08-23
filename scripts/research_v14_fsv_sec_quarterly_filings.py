#!/usr/bin/env python3
"""Recover FSV quarterly fundamentals from SHA-bound SEC 6-K exhibits."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd
from bs4 import BeautifulSoup

from scripts.research_v14_dsgx_sec_quarterly_filings import (
    _longest_chain,
    _normal,
    _period_columns,
    _row_value,
    _sha256,
)


DEFAULT_REGISTRY = Path(
    "stocks_list_dir/nasdaq/fsv_sec_quarterly_filings.csv"
)
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/fsv_sec_quarterly_filings_2017_2021"
)
METRIC_LABELS = {
    "revenue": ("Revenues",),
    "net_income": ("Net earnings (loss)", "Net earnings"),
}


def parse_fsv_period(
    path: Path,
    *,
    period_end: pd.Timestamp,
    period_phrase: str,
) -> dict[str, float]:
    normalized_document = " ".join(
        BeautifulSoup(path.read_bytes(), "html.parser")
        .get_text(" ", strip=True)
        .split()
    ).casefold()
    if not any(
        marker in normalized_document
        for marker in (
            "thousands of us dollars",
            "thousands of us$",
            "thousands of u.s. dollars",
        )
    ):
        raise ValueError(f"FSV filing does not prove USD-thousands units: {path}")

    candidates: list[dict[str, float]] = []
    for table in pd.read_html(path):
        first = table.iloc[:, 0].fillna("").map(_normal)
        if not all(
            first.isin({_normal(label) for label in labels}).any()
            for labels in METRIC_LABELS.values()
        ):
            continue
        try:
            columns = _period_columns(
                table,
                year=period_end.year,
                period_phrase=period_phrase,
            )
            values = {
                metric: _row_value(table, labels=labels, columns=columns)
                for metric, labels in METRIC_LABELS.items()
            }
        except ValueError:
            continue
        if values["revenue"] > 10_000:
            candidates.append(values)
    unique = {
        (values["revenue"], values["net_income"])
        for values in candidates
    }
    if len(unique) != 1:
        raise ValueError(
            f"expected one agreeing FSV {period_phrase} statement pair in {path}, "
            f"found {sorted(unique)}"
        )
    revenue, net_income = next(iter(unique))
    return {"revenue": revenue * 1000.0, "net_income": net_income * 1000.0}


def parse_fsv_quarter(path: Path, fiscal_end: pd.Timestamp) -> dict[str, float]:
    return parse_fsv_period(
        path,
        period_end=fiscal_end,
        period_phrase="Three months",
    )


def run(
    *,
    registry_path: Path = DEFAULT_REGISTRY,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> dict[str, Any]:
    registry = pd.read_csv(
        registry_path,
        dtype={"ticker": str, "cik": int, "accession": str},
        parse_dates=["fiscal_end", "filed_date"],
    )
    if set(registry["ticker"]) != {"FSV"} or set(registry["cik"]) != {1637810}:
        raise ValueError("FSV registry contains another issuer")
    if registry.duplicated("fiscal_end").any():
        raise ValueError("FSV registry contains duplicate quarter ends")

    rows: list[dict[str, Any]] = []
    recovered: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    annual_paths: dict[pd.Timestamp, Path] = {}
    for entry in registry.sort_values("fiscal_end").itertuples(index=False):
        path = Path(entry.local_path)
        values = parse_fsv_quarter(path, entry.fiscal_end)
        if entry.fiscal_end.month == 12:
            annual_paths[entry.fiscal_end] = path
        lag_days = int((entry.filed_date - entry.fiscal_end).days)
        if not 0 <= lag_days <= 150:
            raise ValueError(f"FSV filing is not timely: {entry.accession}")
        common = {
            "ticker": "FSV",
            "fiscal_end": entry.fiscal_end,
            "available_date": entry.filed_date,
            "taxonomy": "us-gaap",
            "form": entry.form,
            "accession": entry.accession,
            "unit": "USD",
            "source": "explicit_sec_filed_three_month_statement",
            "source_archive": path.name,
            "source_archive_sha256": _sha256(path),
            "derivation_prior_accession": "",
        }
        for metric, concept in (
            ("revenue", "RevenueFromContractWithCustomerIncludingAssessedTax"),
            ("net_income", "ProfitLoss"),
        ):
            rows.append({
                **common,
                "metric": metric,
                "value": values[metric],
                "concept": concept,
            })
        recovered.append({
            "ticker": "FSV",
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
    paired = quarters.groupby("fiscal_end")["metric"].nunique()
    paired_ends = paired.loc[paired.eq(2)].index.tolist()
    longest = _longest_chain(paired_ends)
    if longest != len(registry):
        raise RuntimeError(
            f"FSV SEC filing chain is not fully continuous: {longest}/{len(registry)}"
        )

    quarter_frame = pd.DataFrame(recovered)
    quarter_frame["fiscal_end"] = pd.to_datetime(quarter_frame["fiscal_end"])
    annual_cross_checks: list[dict[str, Any]] = []
    for fiscal_end, path in sorted(annual_paths.items()):
        annual = parse_fsv_period(
            path,
            period_end=fiscal_end,
            period_phrase="Twelve months",
        )
        fiscal_rows = quarter_frame.loc[
            quarter_frame["fiscal_end"].dt.year.eq(fiscal_end.year)
        ]
        if len(fiscal_rows) != 4:
            raise RuntimeError(
                f"FSV fiscal year {fiscal_end.year} has {len(fiscal_rows)} quarters"
            )
        observed = {
            metric: float(fiscal_rows[metric].sum())
            for metric in ("revenue", "net_income")
        }
        if observed != annual:
            raise RuntimeError(
                f"FSV quarter sum differs from filed annual values for "
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
    report: dict[str, Any] = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "promotion_eligible": False,
        "release_status": "BLOCKED",
        "ticker": "FSV",
        "currency": "USD",
        "quarter_count": len(registry),
        "direct_quarter_count": len(registry),
        "longest_continuous_timely_paired_quarters": longest,
        "recovered_quarters": recovered,
        "annual_cross_checks": annual_cross_checks,
        "registry": {"path": str(registry_path), "sha256": _sha256(registry_path)},
        "filings": bindings,
        "outputs": {
            "quarters": {"path": str(quarters_path), "sha256": _sha256(quarters_path)}
        },
        "guardrail": (
            "Only direct SEC-filed three-month FSV statements in USD thousands "
            "are accepted. This research artifact does not modify formal "
            "fundamentals or authorize trading."
        ),
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["manifest"] = str(manifest_path)
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
        "direct_quarter_count": result["direct_quarter_count"],
        "longest_continuous_timely_paired_quarters": result[
            "longest_continuous_timely_paired_quarters"
        ],
        "promotion_eligible": False,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
