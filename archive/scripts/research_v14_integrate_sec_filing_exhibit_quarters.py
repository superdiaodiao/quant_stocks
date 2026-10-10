#!/usr/bin/env python3
"""Integrate manifest-declared SEC exhibit quarters into a v14 candidate only."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

import pandas as pd

from src.io.fundamentals_update import merge_fundamentals


DEFAULT_BASE_DIR = Path(
    "output/research_only/v14/"
    "candidate_fundamentals_price_overlay_706_existing_reparse5_foreign_ay"
)
DEFAULT_EXHIBIT_DIR = Path(
    "output/research_only/v14/"
    "sec_filing_dataset_foreign_quarters_2019_2021_zlab_exhibit"
)
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/"
    "candidate_fundamentals_price_overlay_706_existing_reparse5_foreign_ay_zlab"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def select_manifest_recovered_rows(
    quarters: pd.DataFrame,
    manifest: dict,
    *,
    fetched_at: str,
) -> pd.DataFrame:
    selected = []
    for recovered in manifest.get("recovered_facts", []):
        ticker = str(recovered["ticker"]).upper()
        fiscal_end = pd.Timestamp(recovered["fiscal_end"])
        expected_available = pd.Timestamp(recovered["available_date"])
        metric = str(recovered["metric"])
        rows = quarters.loc[
            quarters["ticker"].astype(str).str.upper().eq(ticker)
            & pd.to_datetime(quarters["fiscal_end"]).eq(fiscal_end)
            & pd.to_datetime(quarters["available_date"]).eq(expected_available)
            & quarters["metric"].eq(metric)
        ]
        if len(rows) != 1:
            raise ValueError(
                f"expected one recovered {ticker} {fiscal_end.date()} "
                f"{metric} row"
            )
        row = rows.iloc[0]
        if float(row["value"]) != float(recovered["value"]):
            raise ValueError("recovered fact value differs from manifest")
        selected.append({
            "ticker": ticker,
            "fiscal_end": fiscal_end,
            "available_date": expected_available,
            "metric": metric,
            "value": float(row["value"]),
            "taxonomy": row["taxonomy"],
            "concept": row["concept"],
            "form": row["form"],
            "accession": row["accession"],
            "fetched_at": fetched_at,
        })
    for recovered in manifest.get("recovered_quarters", []):
        ticker = str(recovered["ticker"]).upper()
        fiscal_end = pd.Timestamp(recovered["fiscal_end"])
        expected_available = pd.Timestamp(recovered["available_date"])
        for metric in ("revenue", "net_income"):
            rows = quarters.loc[
                quarters["ticker"].astype(str).str.upper().eq(ticker)
                & pd.to_datetime(quarters["fiscal_end"]).eq(fiscal_end)
                & pd.to_datetime(quarters["available_date"]).eq(expected_available)
                & quarters["metric"].eq(metric)
            ]
            if len(rows) != 1:
                raise ValueError(
                    f"expected one recovered {ticker} {fiscal_end.date()} {metric} row"
                )
            row = rows.iloc[0]
            if pd.Timestamp(row["available_date"]) != expected_available:
                raise ValueError("recovered row availability differs from manifest")
            if float(row["value"]) != float(recovered[metric]):
                raise ValueError("recovered row value differs from manifest")
            selected.append({
                "ticker": ticker,
                "fiscal_end": fiscal_end,
                "available_date": expected_available,
                "metric": metric,
                "value": float(row["value"]),
                "taxonomy": row["taxonomy"],
                "concept": row["concept"],
                "form": row["form"],
                "accession": row["accession"],
                "fetched_at": fetched_at,
            })
    return pd.DataFrame(selected)


def run(
    *,
    base_dir: Path = DEFAULT_BASE_DIR,
    exhibit_dir: Path = DEFAULT_EXHIBIT_DIR,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    fetched_at: str = "2026-08-12",
) -> dict:
    exhibit_manifest_path = exhibit_dir / "manifest.json"
    exhibit_manifest = json.loads(exhibit_manifest_path.read_text(encoding="utf-8"))
    if not exhibit_manifest.get("point_in_time_proven"):
        raise ValueError("SEC exhibit artifact is not point-in-time proven")
    quarters_path = exhibit_dir / "strict_quarterly_facts.csv"
    quarters = pd.read_csv(
        quarters_path, parse_dates=["fiscal_end", "available_date"]
    )
    supplement = select_manifest_recovered_rows(
        quarters, exhibit_manifest, fetched_at=fetched_at
    )

    base_annual = base_dir / "annual.csv"
    base_quarterly = base_dir / "quarterly.csv"
    frozen_before = {
        "annual": _sha256(base_annual), "quarterly": _sha256(base_quarterly)
    }
    quarterly = merge_fundamentals(pd.read_csv(base_quarterly), supplement)
    output_dir.mkdir(parents=True, exist_ok=True)
    annual_output = output_dir / "annual.csv"
    quarterly_output = output_dir / "quarterly.csv"
    supplement_output = output_dir / "supplemental_sec_filing_exhibit_quarterly.csv"
    shutil.copy2(base_annual, annual_output)
    quarterly.to_csv(quarterly_output, index=False)
    supplement.to_csv(supplement_output, index=False)
    frozen_after = {
        "annual": _sha256(base_annual), "quarterly": _sha256(base_quarterly)
    }
    if frozen_after != frozen_before:
        raise RuntimeError("v14 base changed during SEC exhibit integration")
    report = {
        "schema_version": 1,
        "research_only": True,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "supplemental_row_count": len(supplement),
        "merged_quarterly_row_count": len(quarterly),
        "base_hashes": frozen_after,
        "exhibit_manifest": {
            "path": str(exhibit_manifest_path),
            "sha256": _sha256(exhibit_manifest_path),
        },
        "outputs": {
            "annual": {"path": str(annual_output), "sha256": _sha256(annual_output)},
            "quarterly": {
                "path": str(quarterly_output), "sha256": _sha256(quarterly_output)
            },
            "supplemental": {
                "path": str(supplement_output), "sha256": _sha256(supplement_output)
            },
        },
    }
    manifest = output_dir / "manifest.json"
    manifest.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["manifest"] = str(manifest)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dir", type=Path, default=DEFAULT_BASE_DIR)
    parser.add_argument("--exhibit-dir", type=Path, default=DEFAULT_EXHIBIT_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--fetched-at", default="2026-08-12")
    args = parser.parse_args()
    result = run(
        base_dir=args.base_dir,
        exhibit_dir=args.exhibit_dir,
        output_dir=args.output_dir,
        fetched_at=args.fetched_at,
    )
    print(json.dumps({
        "manifest": result["manifest"],
        "supplemental_row_count": result["supplemental_row_count"],
        "merged_quarterly_row_count": result["merged_quarterly_row_count"],
        "promotion_eligible": False,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
