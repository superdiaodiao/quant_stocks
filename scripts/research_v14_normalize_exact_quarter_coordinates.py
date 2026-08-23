#!/usr/bin/env python3
"""Normalize exact duplicate near-date quarter coordinates, research-only."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil

import pandas as pd


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def exact_coordinate_mappings(
    quarterly: pd.DataFrame, tolerance_days: int = 7
) -> list[dict]:
    """Return mappings proven equal on both strategy metrics.

    The first coordinate that had both revenue and net income publicly
    available is canonical. Later comparative filings sometimes relabel the
    same 52/53-week quarter to a nearby calendar date; retaining both dates
    creates a false extra quarter in rolling windows.
    """
    rows = quarterly.copy()
    rows["fiscal_end"] = pd.to_datetime(rows["fiscal_end"])
    rows["available_date"] = pd.to_datetime(rows["available_date"])
    latest = rows.sort_values("available_date").drop_duplicates(
        ["ticker", "fiscal_end", "metric"], keep="last"
    )
    values = latest.pivot_table(
        index=["ticker", "fiscal_end"],
        columns="metric", values="value", aggfunc="last",
    ).dropna(subset=["revenue", "net_income"]).reset_index()
    first_metric = rows.groupby(
        ["ticker", "fiscal_end", "metric"], sort=False
    )["available_date"].min().unstack("metric")
    first_pair = first_metric[["revenue", "net_income"]].max(axis=1)
    values["first_pair_available"] = pd.MultiIndex.from_frame(
        values[["ticker", "fiscal_end"]]
    ).map(first_pair)

    mappings = []
    for ticker, group in values.groupby("ticker", sort=True):
        ordered = group.sort_values("fiscal_end").reset_index(drop=True)
        used_sources: set[pd.Timestamp] = set()
        for index in range(1, len(ordered)):
            left = ordered.iloc[index - 1]
            right = ordered.iloc[index]
            gap = int((right.fiscal_end - left.fiscal_end).days)
            if not 1 <= gap <= tolerance_days:
                continue
            if not (
                float(left.revenue) == float(right.revenue)
                and float(left.net_income) == float(right.net_income)
            ):
                continue
            candidates = sorted(
                (left, right),
                key=lambda row: (row.first_pair_available, row.fiscal_end),
            )
            canonical, duplicate = candidates
            if duplicate.fiscal_end in used_sources:
                raise RuntimeError(
                    f"overlapping duplicate-coordinate mapping for {ticker}"
                )
            used_sources.add(duplicate.fiscal_end)
            mappings.append({
                "ticker": str(ticker),
                "duplicate_fiscal_end": duplicate.fiscal_end.strftime("%Y-%m-%d"),
                "canonical_fiscal_end": canonical.fiscal_end.strftime("%Y-%m-%d"),
                "day_gap": gap,
                "revenue": float(canonical.revenue),
                "net_income": float(canonical.net_income),
                "canonical_first_pair_available": (
                    canonical.first_pair_available.strftime("%Y-%m-%d")
                ),
                "duplicate_first_pair_available": (
                    duplicate.first_pair_available.strftime("%Y-%m-%d")
                ),
            })
    return mappings


def run(base_dir: Path, output_dir: Path, tolerance_days: int = 7) -> dict:
    base_dir = Path(base_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    annual_source = base_dir / "annual.csv"
    quarterly_source = base_dir / "quarterly.csv"
    base_manifest = base_dir / "manifest.json"
    before = {
        "annual": _sha256(annual_source),
        "quarterly": _sha256(quarterly_source),
        "manifest": _sha256(base_manifest),
    }
    quarterly = pd.read_csv(quarterly_source)
    mappings = exact_coordinate_mappings(quarterly, tolerance_days)
    fiscal_end = pd.to_datetime(quarterly["fiscal_end"])
    changed_rows = 0
    for item in mappings:
        mask = (
            quarterly["ticker"].astype(str).str.upper().eq(item["ticker"])
            & fiscal_end.eq(pd.Timestamp(item["duplicate_fiscal_end"]))
        )
        changed_rows += int(mask.sum())
        quarterly.loc[mask, "fiscal_end"] = item["canonical_fiscal_end"]
    quarterly = quarterly.drop_duplicates().sort_values(
        ["ticker", "fiscal_end", "available_date", "metric", "accession"]
    )
    annual_output = output_dir / "annual.csv"
    quarterly_output = output_dir / "quarterly.csv"
    shutil.copy2(annual_source, annual_output)
    quarterly.to_csv(quarterly_output, index=False)
    report = {
        "schema_version": 1,
        "research_only": True,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "tolerance_days": int(tolerance_days),
        "mapping_count": len(mappings),
        "affected_ticker_count": len({item["ticker"] for item in mappings}),
        "rewritten_row_count": changed_rows,
        "mappings": mappings,
        "base_hashes": before,
        "outputs": {
            "annual": str(annual_output),
            "annual_sha256": _sha256(annual_output),
            "quarterly": str(quarterly_output),
            "quarterly_sha256": _sha256(quarterly_output),
        },
    }
    if report["outputs"]["annual_sha256"] != before["annual"]:
        raise RuntimeError("annual research input changed")
    manifest = output_dir / "manifest.json"
    manifest.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return {"manifest": str(manifest), **report}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--tolerance-days", type=int, default=7)
    args = parser.parse_args()
    print(json.dumps(run(
        args.base_dir, args.output_dir, args.tolerance_days
    ), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
