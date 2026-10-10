#!/usr/bin/env python3
"""Add SHA-bound issuer filing exhibits to strict SEC quarter reconstruction."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pandas as pd

from scripts.research_v14_sec_filing_dataset_foreign_quarters import (
    _standard_metric_map,
    reconstruct_quarters,
)


DEFAULT_BASE_DIR = Path(
    "output/research_only/v14/sec_filing_dataset_foreign_quarters_2019_2021"
)
DEFAULT_ZLAB_EXHIBIT = Path(
    "output/data_provenance/sec_submissions_cache/"
    "ZLAB_0001564590-19-033837_ex99-1.htm"
)
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/"
    "sec_filing_dataset_foreign_quarters_2019_2021_zlab_exhibit"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_accounting_number(value: Any) -> float | None:
    if pd.isna(value):
        return None
    text = str(value).strip().replace(",", "").replace("$", "")
    if not text or text in {"—", "-", "nan"}:
        return None
    negative = text.startswith("(") or text.endswith(")")
    text = text.strip("() ")
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", text):
        return None
    number = float(text)
    return -abs(number) if negative else number


def _table_value_for_year(table: pd.DataFrame, label: str, year: int) -> float:
    year_columns = []
    for _, row in table.head(6).iterrows():
        for column, value in row.items():
            if str(value).strip() == str(year):
                year_columns.append(column)
    year_columns = list(dict.fromkeys(year_columns))
    if not year_columns:
        raise ValueError(f"filing table has no {year} header")
    label_rows = table.loc[
        table.iloc[:, 0].fillna("").astype(str).str.strip().eq(label)
    ]
    if len(label_rows) != 1:
        raise ValueError(f"expected exactly one filing row labelled {label!r}")
    values = [
        value
        for column in year_columns
        if (value := _parse_accounting_number(label_rows.iloc[0][column])) is not None
    ]
    if len(values) != 1:
        raise ValueError(
            f"expected one numeric {year} value for {label!r}, found {values}"
        )
    return values[0]


def parse_zlab_h1_2019(exhibit: Path) -> pd.DataFrame:
    """Parse the exact SEC-filed six-month ZLAB income-statement rows."""
    tables = pd.read_html(exhibit)
    matches = [
        table for table in tables
        if table.iloc[:, 0].fillna("").astype(str).str.strip().eq("Revenue").any()
        and table.iloc[:, 0].fillna("").astype(str).str.strip().eq("Net loss").any()
        and table.astype(str).apply(
            lambda column: column.str.contains(
                "For the six months ended June 30", case=False, regex=False
            )
        ).any().any()
    ]
    if len(matches) != 1:
        raise ValueError(
            f"expected one ZLAB six-month income statement, found {len(matches)}"
        )
    table = matches[0]
    revenue = _table_value_for_year(table, "Revenue", 2019)
    net_income = _table_value_for_year(table, "Net loss", 2019)
    if revenue <= 0 or net_income >= 0:
        raise ValueError("ZLAB filing exhibit has unexpected revenue/net-loss signs")
    priorities = _standard_metric_map()
    archive_sha = _sha256(exhibit)
    common = {
        "ticker": "ZLAB",
        "cik": 1704292,
        "taxonomy": "us-gaap",
        "unit": "USD",
        "end": pd.Timestamp("2019-06-30"),
        "filed_date": pd.Timestamp("2019-09-03"),
        "qtrs": 2,
        "form": "6-K",
        "adsh": "0001564590-19-033837",
        "source_archive": exhibit.name,
        "source_archive_sha256": archive_sha,
    }
    rows = []
    for metric, tag, label, value in (
        (
            "revenue",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenue",
            revenue,
        ),
        ("net_income", "NetIncomeLoss", "Net loss", net_income),
    ):
        rows.append({
            **common,
            "metric": metric,
            "tag": tag,
            "plabel": label,
            "concept_priority": priorities[tag][1],
            "value": value,
        })
    return pd.DataFrame(rows)


def run(
    *,
    base_dir: Path = DEFAULT_BASE_DIR,
    zlab_exhibit: Path = DEFAULT_ZLAB_EXHIBIT,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> dict[str, Any]:
    base_manifest_path = base_dir / "manifest.json"
    base_manifest = json.loads(base_manifest_path.read_text(encoding="utf-8"))
    raw_path = base_dir / "raw_income_statement_facts.csv"
    raw = pd.read_csv(raw_path, parse_dates=["end", "filed_date"])
    exhibit_rows = parse_zlab_h1_2019(zlab_exhibit)
    combined_raw = pd.concat([raw, exhibit_rows], ignore_index=True)
    quarters, conflicts = reconstruct_quarters(combined_raw)

    zlab_q3 = quarters.loc[
        quarters["ticker"].eq("ZLAB")
        & quarters["fiscal_end"].eq(pd.Timestamp("2019-09-30"))
    ].set_index("metric")
    expected = {"revenue": 4_919_549.0, "net_income": -65_366_947.0}
    observed = zlab_q3["value"].to_dict()
    if observed != expected:
        raise RuntimeError(f"unexpected ZLAB Q3 reconstruction: {observed}")
    lag_days = int(
        (zlab_q3["available_date"].max() - pd.Timestamp("2019-09-30")).days
    )
    if lag_days != 113:
        raise RuntimeError(f"unexpected ZLAB Q3 availability lag: {lag_days}")

    output_dir.mkdir(parents=True, exist_ok=True)
    exhibit_rows_path = output_dir / "issuer_exhibit_ytd_facts.csv"
    combined_raw_path = output_dir / "combined_raw_income_statement_facts.csv"
    quarters_path = output_dir / "strict_quarterly_facts.csv"
    conflicts_path = output_dir / "conflicts.csv"
    exhibit_rows.to_csv(exhibit_rows_path, index=False)
    combined_raw.to_csv(combined_raw_path, index=False)
    quarters.to_csv(quarters_path, index=False)
    conflicts.to_csv(conflicts_path, index=False)
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "promotion_eligible": False,
        "release_status": "BLOCKED",
        "base": {
            "manifest": str(base_manifest_path),
            "manifest_sha256": _sha256(base_manifest_path),
            "raw_sha256": _sha256(raw_path),
            "strict_quarter_row_count": base_manifest["strict_quarter_row_count"],
        },
        "issuer_exhibits": [{
            "ticker": "ZLAB",
            "accession": "0001564590-19-033837",
            "filed": "2019-09-03",
            "form": "6-K",
            "path": str(zlab_exhibit),
            "sha256": _sha256(zlab_exhibit),
            "period_end": "2019-06-30",
            "qtrs": 2,
            "parsed_values": {
                "revenue": 3_420_183.0,
                "net_income": -83_273_723.0,
            },
        }],
        "strict_quarter_row_count": len(quarters),
        "conflict_count": len(conflicts),
        "recovered_quarters": [{
            "ticker": "ZLAB",
            "fiscal_end": "2019-09-30",
            "available_date": "2020-01-21",
            "availability_lag_days": lag_days,
            "revenue": expected["revenue"],
            "net_income": expected["net_income"],
            "derivation": "nine_month_ytd_minus_six_month_ytd",
        }],
        "outputs": {
            "issuer_exhibit_ytd": {
                "path": str(exhibit_rows_path), "sha256": _sha256(exhibit_rows_path)
            },
            "combined_raw": {
                "path": str(combined_raw_path), "sha256": _sha256(combined_raw_path)
            },
            "quarters": {
                "path": str(quarters_path), "sha256": _sha256(quarters_path)
            },
            "conflicts": {
                "path": str(conflicts_path), "sha256": _sha256(conflicts_path)
            },
        },
        "guardrail": (
            "The recovered quarter is research input only; it does not by itself "
            "make ZLAB continuous, freeze parameters, or authorize trading."
        ),
    }
    manifest = output_dir / "manifest.json"
    manifest.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["manifest"] = str(manifest)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dir", type=Path, default=DEFAULT_BASE_DIR)
    parser.add_argument("--zlab-exhibit", type=Path, default=DEFAULT_ZLAB_EXHIBIT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = run(
        base_dir=args.base_dir,
        zlab_exhibit=args.zlab_exhibit,
        output_dir=args.output_dir,
    )
    print(json.dumps({
        "manifest": result["manifest"],
        "strict_quarter_row_count": result["strict_quarter_row_count"],
        "recovered_quarters": result["recovered_quarters"],
        "promotion_eligible": False,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
