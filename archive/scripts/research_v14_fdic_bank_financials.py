#!/usr/bin/env python3
"""Build a provisional FDIC Call Report sensitivity overlay for v14.

The BankFind API exposes current historical values, not immutable snapshots of
what the API returned on each historical date.  Outputs therefore remain
research-only and explicitly fail the point-in-time promotion gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

from src.io.fundamentals_update import OUTPUT_COLUMNS, merge_fundamentals


FDIC_API = "https://api.fdic.gov/banks/financials"
DEFAULT_BASE_DIR = Path(
    "output/research_only/v14/"
    "candidate_fundamentals_price_overlay_706_issuer_fix2_final"
)
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/"
    "candidate_fundamentals_price_overlay_706_fdic_sensitivity"
)
BANKS = {
    "OZK": {"cert": 110, "expected_names": {"BANK OZK", "BANK OF THE OZARKS"}},
    "TOWN": {"cert": 35095, "expected_names": {"TOWNE BANK", "TOWNEBANK"}},
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fetch_fdic_financials(cert: int) -> tuple[dict, str]:
    params = {
        "filters": f"CERT:{int(cert)}",
        "fields": "CERT,REPDTE,NAME,NIM,NONII,NETINC",
        "limit": 1000,
        "format": "json",
        "sort_by": "REPDTE",
        "sort_order": "ASC",
    }
    url = FDIC_API + "?" + urlencode(params)
    request = Request(url, headers={"User-Agent": "quant-stocks-research"})
    with urlopen(request, timeout=45) as response:
        document = json.load(response)
    return document, url


def fdic_ytd_to_quarters(
    ticker: str,
    document: dict,
    *,
    available_lag_days: int = 60,
    start: str = "2017-01-01",
    end: str = "2026-12-31",
) -> pd.DataFrame:
    """Convert cumulative Call Report income fields to single quarters."""
    normalized = str(ticker).strip().upper()
    rule = BANKS.get(normalized)
    if rule is None:
        raise ValueError(f"No FDIC research mapping for {normalized}")
    rows = pd.DataFrame([item["data"] for item in document.get("data", [])])
    required = {"CERT", "REPDTE", "NAME", "NIM", "NONII", "NETINC"}
    missing = required - set(rows.columns)
    if missing:
        raise RuntimeError(f"FDIC response is missing fields: {sorted(missing)}")
    rows["REPDTE"] = pd.to_datetime(rows["REPDTE"], format="%Y%m%d")
    rows = rows.loc[rows["REPDTE"].between(start, end)].copy()
    if rows.empty:
        raise RuntimeError(f"FDIC response has no rows for {normalized}")
    if set(rows["CERT"].astype(int)) != {int(rule["cert"])}:
        raise RuntimeError(f"FDIC certificate mismatch for {normalized}")
    names = set(rows["NAME"].astype(str).str.upper())
    if not names.issubset(rule["expected_names"]):
        raise RuntimeError(
            f"Unexpected FDIC institution names for {normalized}: {sorted(names)}"
        )
    rows = rows.sort_values("REPDTE")
    rows["year"] = rows["REPDTE"].dt.year
    records = []
    for _, year_rows in rows.groupby("year", sort=True):
        previous = None
        for row in year_rows.itertuples(index=False):
            ytd = {
                "revenue": float(row.NIM) + float(row.NONII),
                "net_income": float(row.NETINC),
            }
            values = ytd if previous is None else {
                metric: value - previous[metric]
                for metric, value in ytd.items()
            }
            available = row.REPDTE + pd.Timedelta(days=available_lag_days)
            accession = f"fdic-cert-{int(row.CERT)}-{row.REPDTE:%Y%m%d}"
            for metric, value in values.items():
                records.append({
                    "ticker": normalized,
                    "fiscal_end": row.REPDTE,
                    "available_date": available,
                    "metric": metric,
                    "value": value * 1000.0,
                    "taxonomy": "fdic_bankfind",
                    "concept": (
                        "derived_fdic_bank_revenue:NIM+NONII"
                        if metric == "revenue"
                        else "fdic_call_report:NETINC"
                    ),
                    "form": "FDIC_CALL_REPORT_PROVISIONAL",
                    "accession": accession,
                    "fetched_at": pd.Timestamp.now(tz="UTC").tz_localize(None).normalize(),
                })
            previous = ytd
    result = pd.DataFrame(records, columns=OUTPUT_COLUMNS)
    if result.duplicated(
        ["ticker", "fiscal_end", "available_date", "metric", "accession"]
    ).any():
        raise RuntimeError("FDIC quarterly conversion produced duplicate rows")
    return result.sort_values(
        ["ticker", "available_date", "fiscal_end", "metric"]
    )


def run(
    *,
    base_dir: Path = DEFAULT_BASE_DIR,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    available_lag_days: int = 60,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    sources = []
    for ticker, rule in BANKS.items():
        document, url = fetch_fdic_financials(rule["cert"])
        raw_path = raw_dir / f"fdic_cert_{rule['cert']}.json"
        raw_path.write_text(
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        frame = fdic_ytd_to_quarters(
            ticker,
            document,
            available_lag_days=available_lag_days,
        )
        frames.append(frame)
        sources.append({
            "ticker": ticker,
            "cert": rule["cert"],
            "url": url,
            "raw_path": str(raw_path),
            "raw_sha256": _sha256(raw_path),
            "output_rows": len(frame),
            "first_fiscal_end": str(pd.to_datetime(frame["fiscal_end"]).min().date()),
            "last_fiscal_end": str(pd.to_datetime(frame["fiscal_end"]).max().date()),
        })
    supplement = pd.concat(frames, ignore_index=True)
    base_annual = base_dir / "annual.csv"
    base_quarterly = base_dir / "quarterly.csv"
    before = {"annual": _sha256(base_annual), "quarterly": _sha256(base_quarterly)}
    quarterly = merge_fundamentals(pd.read_csv(base_quarterly), supplement)
    annual_output = output_dir / "annual.csv"
    quarterly_output = output_dir / "quarterly.csv"
    supplement_output = output_dir / "supplemental_fdic_quarterly.csv"
    pd.read_csv(base_annual).to_csv(annual_output, index=False)
    quarterly.to_csv(quarterly_output, index=False)
    supplement.to_csv(supplement_output, index=False)
    after = {"annual": _sha256(base_annual), "quarterly": _sha256(base_quarterly)}
    if after != before:
        raise RuntimeError("v14 base changed during FDIC sensitivity merge")
    report = {
        "schema_version": 1,
        "research_only": True,
        "release_status": "BLOCKED",
        "point_in_time_proven": False,
        "promotion_eligible": False,
        "sensitivity_only": True,
        "available_date_policy": (
            f"REPDTE plus {available_lag_days} days; conservative lag, but "
            "historical API revision state is not proven"
        ),
        "warning": (
            "Current FDIC historical values may contain later revisions. "
            "Do not use this overlay for final training or promotion until "
            "historical publication-state evidence is bound."
        ),
        "base_hashes": after,
        "sources": sources,
        "supplemental_rows": len(supplement),
        "merged_quarterly_rows": len(quarterly),
        "outputs": {
            "annual": {"path": str(annual_output), "sha256": _sha256(annual_output)},
            "quarterly": {"path": str(quarterly_output), "sha256": _sha256(quarterly_output)},
            "supplemental": {"path": str(supplement_output), "sha256": _sha256(supplement_output)},
        },
    }
    manifest = output_dir / "manifest.json"
    manifest.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["manifest"] = str(manifest)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dir", type=Path, default=DEFAULT_BASE_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--available-lag-days", type=int, default=60)
    args = parser.parse_args()
    report = run(
        base_dir=args.base_dir,
        output_dir=args.output_dir,
        available_lag_days=args.available_lag_days,
    )
    print(json.dumps({
        "manifest": report["manifest"],
        "supplemental_rows": report["supplemental_rows"],
        "merged_quarterly_rows": report["merged_quarterly_rows"],
        "point_in_time_proven": False,
        "promotion_eligible": False,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
