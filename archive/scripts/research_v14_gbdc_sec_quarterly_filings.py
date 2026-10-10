#!/usr/bin/env python3
"""Recover GBDC quarterly fundamentals from SHA-bound SEC 10-Q/10-K tables."""

from __future__ import annotations

import argparse
import json
import re
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
    "stocks_list_dir/nasdaq/gbdc_sec_quarterly_filings.csv"
)
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/gbdc_sec_quarterly_filings_2017_2021"
)
METRIC_LABELS = {
    "revenue": ("Total investment income",),
    "net_income": (
        "Net increase in net assets resulting from operations",
        "Net increase (decrease) in net assets resulting from operations",
    ),
}


def _date_columns(table: pd.DataFrame, period_end: pd.Timestamp) -> list[Any]:
    target = _normal(period_end.strftime("%B %d, %Y").replace(" 0", " "))
    selected = []
    for column in table.columns:
        header = {_normal(value) for value in table[column].head(8)}
        if target in header:
            selected.append(column)
    if not selected:
        raise ValueError(f"GBDC table has no column for {period_end.date()}")
    return selected


def _contains_four_quarter_headers(
    table: pd.DataFrame,
) -> bool:
    dates = set()
    for value in table.head(8).to_numpy().ravel():
        text = str(value).strip()
        if not re.fullmatch(r"[A-Za-z]+ \d{1,2}, \d{4}", text):
            continue
        parsed = pd.to_datetime(text, errors="coerce")
        if pd.notna(parsed):
            dates.add(pd.Timestamp(parsed).normalize())
    return len(dates) >= 4


def parse_gbdc_quarter(
    path: Path,
    *,
    period_end: pd.Timestamp,
    source_mode: str,
) -> dict[str, float]:
    normalized_document = " ".join(
        BeautifulSoup(path.read_bytes(), "html.parser")
        .get_text(" ", strip=True)
        .split()
    ).casefold()
    if "in thousands" not in normalized_document:
        raise ValueError(f"GBDC filing does not prove thousands units: {path}")
    candidates = []
    for table in pd.read_html(path):
        first = table.iloc[:, 0].fillna("").map(_normal)
        if not all(
            first.isin({_normal(label) for label in labels}).any()
            for labels in METRIC_LABELS.values()
        ):
            continue
        try:
            if source_mode == "direct_three_month_statement":
                columns = _period_columns(
                    table,
                    year=period_end.year,
                    period_phrase="Three months",
                )
            elif source_mode == "annual_quarter_table":
                if not _contains_four_quarter_headers(table):
                    continue
                columns = _date_columns(table, period_end)
            elif source_mode == "annual_statement":
                columns = _date_columns(table, period_end)
            else:
                raise ValueError(f"unsupported GBDC source mode {source_mode}")
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
            f"expected one agreeing GBDC statement pair in {path}, "
            f"found {sorted(unique)}"
        )
    revenue, net_income = next(iter(unique))
    return {"revenue": revenue * 1000.0, "net_income": net_income * 1000.0}


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
    if set(registry["ticker"]) != {"GBDC"} or set(registry["cik"]) != {1476765}:
        raise ValueError("GBDC registry contains another issuer")
    if registry.duplicated("fiscal_end").any():
        raise ValueError("GBDC registry contains duplicate quarter ends")

    rows = []
    recovered = []
    bindings = []
    annual_paths = {}
    annual_identity_checks = []
    for entry in registry.sort_values("fiscal_end").itertuples(index=False):
        path = Path(entry.local_path)
        derivation_prior_accession = ""
        if entry.source_mode == "annual_minus_prior_quarters":
            annual = parse_gbdc_quarter(
                path,
                period_end=entry.fiscal_end,
                source_mode="annual_statement",
            )
            prior = [
                row for row in recovered
                if (
                    entry.fiscal_end - pd.DateOffset(months=9)
                    <= pd.Timestamp(row["fiscal_end"])
                    < entry.fiscal_end
                )
            ]
            if len(prior) != 3:
                raise RuntimeError(
                    f"GBDC {entry.fiscal_end.date()} Q4 derivation has "
                    f"{len(prior)} prior quarters"
                )
            values = {
                metric: annual[metric] - sum(row[metric] for row in prior)
                for metric in ("revenue", "net_income")
            }
            derivation_prior_accession = ";".join(
                row["accession"] for row in prior
            )
            annual_identity_checks.append({
                "fiscal_end": entry.fiscal_end.strftime("%Y-%m-%d"),
                "filed_annual": annual,
                "prior_three_quarter_sum": {
                    metric: sum(row[metric] for row in prior)
                    for metric in ("revenue", "net_income")
                },
                "derived_fourth_quarter": values,
                "identity_exact": True,
            })
        else:
            values = parse_gbdc_quarter(
                path,
                period_end=entry.fiscal_end,
                source_mode=entry.source_mode,
            )
        lag_days = int((entry.filed_date - entry.fiscal_end).days)
        if not 0 <= lag_days <= 150:
            raise ValueError(f"GBDC filing is not timely: {entry.accession}")
        if entry.source_mode == "annual_quarter_table":
            annual_paths[entry.fiscal_end] = path
        common = {
            "ticker": "GBDC",
            "fiscal_end": entry.fiscal_end,
            "available_date": entry.filed_date,
            "taxonomy": "us-gaap",
            "form": entry.form,
            "accession": entry.accession,
            "unit": "USD",
            "source": "explicit_sec_filed_quarterly_statement",
            "source_archive": path.name,
            "source_archive_sha256": _sha256(path),
            "derivation_prior_accession": derivation_prior_accession,
        }
        for metric, concept in (
            ("revenue", "GrossInvestmentIncomeOperating"),
            (
                "net_income",
                "NetIncreaseDecreaseInNetAssetsResultingFromOperations",
            ),
        ):
            rows.append({
                **common,
                "metric": metric,
                "value": values[metric],
                "concept": concept,
            })
        recovered.append({
            "ticker": "GBDC",
            "fiscal_end": entry.fiscal_end.strftime("%Y-%m-%d"),
            "available_date": entry.filed_date.strftime("%Y-%m-%d"),
            "availability_lag_days": lag_days,
            **values,
            "derivation": entry.source_mode,
            "accession": entry.accession,
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
            f"GBDC SEC filing chain is not fully continuous: {longest}/{len(registry)}"
        )

    recovered_frame = pd.DataFrame(recovered)
    recovered_frame["fiscal_end"] = pd.to_datetime(
        recovered_frame["fiscal_end"]
    )
    annual_quarter_cross_checks = []
    for fiscal_end, path in sorted(annual_paths.items()):
        fiscal_year = recovered_frame.loc[
            recovered_frame["fiscal_end"].between(
                fiscal_end - pd.DateOffset(months=9), fiscal_end
            )
        ]
        if len(fiscal_year) != 4:
            raise RuntimeError(
                f"GBDC fiscal year {fiscal_end.date()} has {len(fiscal_year)} quarters"
            )
        checked = []
        differences = []
        for row in fiscal_year.itertuples(index=False):
            annual_values = parse_gbdc_quarter(
                path,
                period_end=row.fiscal_end,
                source_mode="annual_quarter_table",
            )
            observed = {
                "revenue": float(row.revenue),
                "net_income": float(row.net_income),
            }
            absolute_difference = {
                metric: abs(annual_values[metric] - observed[metric])
                for metric in ("revenue", "net_income")
            }
            # Both statements are presented in whole USD thousands.  Preserve
            # the original 10-Q value for point-in-time use, while accepting at
            # most one displayed unit of later 10-K rounding/reclassification.
            if any(value > 1_000.0 for value in absolute_difference.values()):
                raise RuntimeError(
                    f"GBDC original quarter differs from filed annual table "
                    f"for {row.fiscal_end.date()}: {observed} != {annual_values}; "
                    f"difference={absolute_difference}"
                )
            checked.append(row.fiscal_end.strftime("%Y-%m-%d"))
            differences.append({
                "fiscal_end": row.fiscal_end.strftime("%Y-%m-%d"),
                "absolute_difference": absolute_difference,
            })
        annual_quarter_cross_checks.append({
            "fiscal_end": fiscal_end.strftime("%Y-%m-%d"),
            "checked_quarters": checked,
            "differences": differences,
            "maximum_allowed_difference": 1_000.0,
            "within_one_displayed_thousand": True,
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
        "ticker": "GBDC",
        "currency": "USD",
        "quarter_count": len(registry),
        "longest_continuous_timely_paired_quarters": longest,
        "recovered_quarters": recovered,
        "annual_quarter_cross_checks": annual_quarter_cross_checks,
        "annual_identity_checks": annual_identity_checks,
        "registry": {"path": str(registry_path), "sha256": _sha256(registry_path)},
        "filings": bindings,
        "outputs": {
            "quarters": {"path": str(quarters_path), "sha256": _sha256(quarters_path)}
        },
        "guardrail": (
            "Only explicit quarterly values in SEC-filed GBDC 10-Q statements "
            "and 10-K quarterly tables are accepted; when no quarterly table "
            "exists, Q4 is the filed annual value less the three previously "
            "filed quarters. Original 10-Q values are "
            "preserved point-in-time; annual-table cross-check differences may "
            "not exceed one displayed USD-thousand unit. This research artifact "
            "does not modify formal fundamentals or authorize trading."
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
        "longest_continuous_timely_paired_quarters": result[
            "longest_continuous_timely_paired_quarters"
        ],
        "promotion_eligible": False,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
