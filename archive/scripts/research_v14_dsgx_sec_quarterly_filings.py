#!/usr/bin/env python3
"""Recover DSGX quarterly fundamentals from SHA-bound SEC 6-K exhibits."""

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
    "stocks_list_dir/nasdaq/dsgx_sec_quarterly_filings.csv"
)
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/dsgx_sec_quarterly_filings_2017_2021"
)
METRIC_LABELS = {
    "revenue": ("REVENUES", "Revenues"),
    "net_income": ("NET INCOME", "Net income"),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normal(value: Any) -> str:
    return " ".join(str(value).strip().split()).casefold()


def _period_columns(
    table: pd.DataFrame,
    *,
    year: int,
    period_phrase: str,
) -> list[Any]:
    phrase_columns: set[Any] = set()
    year_columns: set[Any] = set()
    phrase = period_phrase.casefold()
    for _, row in table.head(8).iterrows():
        for column, value in row.items():
            text = _normal(value)
            if phrase in text:
                phrase_columns.add(column)
            if text == str(year):
                year_columns.add(column)
    selected = [
        column for column in table.columns
        if column in phrase_columns and column in year_columns
    ]
    if not selected:
        header_text = " ".join(
            _normal(value) for value in table.head(8).to_numpy().ravel()
        )
        competing_phrases = {
            candidate.casefold()
            for candidate in (
                "Three Months Ended",
                "Six Months Ended",
                "Nine Months Ended",
                "Year Ended",
            )
            if candidate.casefold() != phrase
        }
        if (
            phrase in header_text
            and not any(candidate in header_text for candidate in competing_phrases)
        ):
            selected = [
                column for column in table.columns if column in year_columns
            ]
    if not selected:
        raise ValueError(
            f"DSGX table has no {period_phrase!r} {year} value column"
        )
    return selected


def _row_value(
    table: pd.DataFrame,
    *,
    labels: tuple[str, ...],
    columns: list[Any],
) -> float:
    normalized_labels = {_normal(label) for label in labels}
    first = table.iloc[:, 0].fillna("").map(_normal)
    rows = table.loc[first.isin(normalized_labels)]
    if len(rows) != 1:
        raise ValueError(
            f"expected one DSGX statement row in {labels!r}, found {len(rows)}"
        )
    parsed = [
        value
        for column in columns
        if (value := _parse_accounting_number(rows.iloc[0][column])) is not None
    ]
    values = sorted(set(parsed))
    if len(values) != 1:
        raise ValueError(
            f"expected one repeated DSGX value in {labels!r}, found {values}"
        )
    return values[0]


def parse_statement_period(
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
        for marker in ("us dollars in thousands", "u.s. dollars in thousands")
    ):
        raise ValueError(f"DSGX filing does not prove USD-thousands units: {path}")
    candidates: list[tuple[bool, dict[str, float]]] = []
    for table in pd.read_html(path):
        first_raw = table.iloc[:, 0].fillna("").astype(str).str.strip()
        first = first_raw.map(_normal)
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
            canonical_statement = (
                first_raw.eq("REVENUES").any()
                and first_raw.eq("NET INCOME").any()
            )
            candidates.append((canonical_statement, values))
    if any(canonical for canonical, _values in candidates):
        candidates = [item for item in candidates if item[0]]
    unique = {
        (values["revenue"], values["net_income"])
        for _canonical, values in candidates
    }
    if len(unique) != 1:
        raise ValueError(
            f"expected one agreeing DSGX {period_phrase} value pair in {path}, "
            f"found {sorted(unique)}"
        )
    revenue, net_income = next(iter(unique))
    return {"revenue": revenue, "net_income": net_income}


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


def run(
    *,
    registry_path: Path = DEFAULT_REGISTRY,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> dict[str, Any]:
    registry = pd.read_csv(
        registry_path,
        dtype={"ticker": str, "cik": int, "accession": str},
        parse_dates=["fiscal_end", "filed_date", "prior_fiscal_end"],
    )
    if set(registry["ticker"]) != {"DSGX"} or set(registry["cik"]) != {1050140}:
        raise ValueError("DSGX registry contains another issuer")
    if registry.duplicated("fiscal_end").any():
        raise ValueError("DSGX registry contains duplicate quarter ends")

    rows: list[dict[str, Any]] = []
    recovered: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    annual_cross_checks: list[dict[str, Any]] = []
    for entry in registry.sort_values("fiscal_end").itertuples(index=False):
        path = Path(entry.local_path)
        derivation = str(entry.derivation)
        prior_path: Path | None = None
        if derivation == "direct_three_month_statement":
            values_thousands = parse_statement_period(
                path,
                period_end=entry.fiscal_end,
                period_phrase="Three Months Ended",
            )
            prior_accession = ""
        elif derivation == "annual_minus_nine_months":
            annual = parse_statement_period(
                path,
                period_end=entry.fiscal_end,
                period_phrase="Year Ended",
            )
            prior_end = pd.Timestamp(entry.prior_fiscal_end)
            prior_path = Path(entry.prior_local_path)
            nine_months = parse_statement_period(
                prior_path,
                period_end=prior_end,
                period_phrase="Nine Months Ended",
            )
            values_thousands = {
                metric: annual[metric] - nine_months[metric]
                for metric in METRIC_LABELS
            }
            if values_thousands["revenue"] <= 0:
                raise ValueError(f"DSGX derived Q4 revenue is not positive: {entry.accession}")
            prior_accession = str(entry.prior_accession)
            annual_cross_checks.append({
                "fiscal_end": entry.fiscal_end.strftime("%Y-%m-%d"),
                "annual_thousands": annual,
                "nine_months_thousands": nine_months,
                "derived_q4_thousands": values_thousands,
                "exact_arithmetic_identity": True,
            })
        else:
            raise ValueError(f"unsupported DSGX derivation: {derivation}")

        values = {
            metric: value * 1000.0
            for metric, value in values_thousands.items()
        }
        lag_days = int((entry.filed_date - entry.fiscal_end).days)
        if not 0 <= lag_days <= 150:
            raise ValueError(f"DSGX filing is not timely: {entry.accession}")
        common = {
            "ticker": "DSGX",
            "fiscal_end": entry.fiscal_end,
            "available_date": entry.filed_date,
            "taxonomy": "us-gaap",
            "form": entry.form,
            "accession": entry.accession,
            "unit": "USD",
            "source": "sec_filed_dsgx_quarterly_statement",
            "source_archive": path.name,
            "source_archive_sha256": _sha256(path),
            "derivation_prior_accession": prior_accession,
        }
        for metric, concept in (
            ("revenue", "RevenueFromContractWithCustomerIncludingAssessedTax"),
            ("net_income", "NetIncomeLoss"),
        ):
            rows.append({
                **common,
                "metric": metric,
                "value": values[metric],
                "concept": concept,
            })
        recovered.append({
            "ticker": "DSGX",
            "fiscal_end": entry.fiscal_end.strftime("%Y-%m-%d"),
            "available_date": entry.filed_date.strftime("%Y-%m-%d"),
            "availability_lag_days": lag_days,
            **values,
            "derivation": derivation,
        })
        binding = {
            "accession": entry.accession,
            "fiscal_end": entry.fiscal_end.strftime("%Y-%m-%d"),
            "filed_date": entry.filed_date.strftime("%Y-%m-%d"),
            "path": str(path),
            "sha256": _sha256(path),
            "source_url": entry.source_url,
            "derivation": derivation,
        }
        if prior_path is not None:
            binding["prior"] = {
                "accession": prior_accession,
                "period_end": pd.Timestamp(entry.prior_fiscal_end).strftime("%Y-%m-%d"),
                "path": str(prior_path),
                "sha256": _sha256(prior_path),
                "source_url": entry.prior_source_url,
            }
        bindings.append(binding)

    quarters = pd.DataFrame(rows).sort_values(["fiscal_end", "metric"])
    paired = quarters.groupby("fiscal_end")["metric"].nunique()
    paired_ends = paired.loc[paired.eq(2)].index.tolist()
    longest = _longest_chain(paired_ends)
    if longest != len(registry):
        raise RuntimeError(
            f"DSGX SEC filing chain is not fully continuous: {longest}/{len(registry)}"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    quarters_path = output_dir / "strict_quarterly_facts.csv"
    quarters.to_csv(quarters_path, index=False)
    report: dict[str, Any] = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "promotion_eligible": False,
        "release_status": "BLOCKED",
        "ticker": "DSGX",
        "currency": "USD",
        "quarter_count": len(registry),
        "direct_quarter_count": int(registry["derivation"].eq("direct_three_month_statement").sum()),
        "derived_q4_count": int(registry["derivation"].eq("annual_minus_nine_months").sum()),
        "longest_continuous_timely_paired_quarters": longest,
        "recovered_quarters": recovered,
        "annual_cross_checks": annual_cross_checks,
        "registry": {"path": str(registry_path), "sha256": _sha256(registry_path)},
        "filings": bindings,
        "outputs": {
            "quarters": {"path": str(quarters_path), "sha256": _sha256(quarters_path)}
        },
        "guardrail": (
            "Only direct SEC-filed three-month statements or exact annual-minus-"
            "nine-month identities are accepted. This research artifact does not "
            "modify formal fundamentals or authorize trading."
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
        "derived_q4_count": result["derived_q4_count"],
        "longest_continuous_timely_paired_quarters": result[
            "longest_continuous_timely_paired_quarters"
        ],
        "promotion_eligible": False,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
