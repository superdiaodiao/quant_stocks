#!/usr/bin/env python3
"""Recover direct Bank OZK quarterly disclosures for v14 research only."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd
from bs4 import BeautifulSoup

from scripts.research_v14_cybr_quarterly_reports import (
    OUTPUT_COLUMNS,
    _period_columns,
    _row_value,
)


DEFAULT_REGISTRY = Path("stocks_list_dir/nasdaq/ozk_quarterly_reports.csv")
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/ozk_ir_quarterly_reports_2018_2021"
)
USER_AGENT = "quant_stocks research data@example.com"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_url(slug: str) -> str:
    return f"https://ir.ozk.com/news-releases/news-release-details/{slug}"


def _archive_url(timestamp: str, slug: str) -> str:
    return f"https://web.archive.org/web/{timestamp}id_/{_source_url(slug)}"


def _parse_quarter(path: Path, fiscal_end: pd.Timestamp) -> dict[str, float]:
    raw = path.read_bytes()
    text = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
    if not re.search(r"Bank (?:of the Ozarks|OZK)", text, re.IGNORECASE):
        raise ValueError(f"not a Bank OZK disclosure: {path}")
    if not re.search(r"Dollars? in [Tt]housands", text):
        raise ValueError(f"OZK disclosure does not prove thousands unit: {path}")
    pairs: list[tuple[float, float, float]] = []
    for table in pd.read_html(path):
        try:
            columns = _period_columns(
                table,
                fiscal_end=fiscal_end,
                period_phrase="Three months ended",
            )
            _, net_interest = _row_value(
                table, labels=("Net interest income",), columns=columns
            )
            _, noninterest = _row_value(
                table,
                labels=(
                    "Total non-interest income",
                    "Total noninterest income",
                    "Non-interest income",
                ),
                columns=columns,
            )
            _, net_income = _row_value(
                table, labels=("Net income",), columns=columns
            )
        except ValueError:
            continue
        if net_interest > 10_000:
            pairs.append((net_interest, noninterest, net_income))
    pairs = sorted(set(pairs))
    if len(pairs) != 1:
        raise ValueError(
            f"expected one direct OZK quarter in {path}; pairs={pairs}"
        )
    net_interest, noninterest, net_income = pairs[0]
    return {
        "revenue": (net_interest + noninterest) * 1000.0,
        "net_income": net_income * 1000.0,
        "net_interest_income": net_interest * 1000.0,
        "noninterest_income": noninterest * 1000.0,
    }


def _download(url: str, path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=90) as response:
        payload = response.read()
    if len(payload) < 250_000 or b"<html" not in payload[:2_000].lower():
        raise ValueError(f"unexpected archived OZK disclosure: {url}")
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def run(
    *,
    registry_path: Path = DEFAULT_REGISTRY,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    download_missing: bool = True,
) -> dict:
    registry = pd.read_csv(
        registry_path,
        dtype={"archive_timestamp": str},
        parse_dates=["fiscal_end", "available_date"],
    )
    if set(registry["ticker"]) != {"OZK"} or len(registry) != 15:
        raise ValueError("OZK registry must contain the declared 15 versions")
    if registry.duplicated(["fiscal_end", "available_date"]).any():
        raise ValueError("OZK registry contains duplicate fact versions")
    raw_dir = output_dir / "raw"
    records: list[dict] = []
    recovered: list[dict] = []
    bindings: list[dict] = []
    for entry in registry.sort_values(["available_date", "fiscal_end"]).itertuples():
        source_url = _source_url(entry.slug)
        archive_url = _archive_url(entry.archive_timestamp, entry.slug)
        path = raw_dir / f"{entry.archive_timestamp}_{entry.slug}.html"
        if download_missing:
            _download(archive_url, path)
        values = _parse_quarter(path, entry.fiscal_end)
        page_text = BeautifulSoup(
            path.read_bytes(), "html.parser"
        ).get_text(" ", strip=True)
        published = entry.available_date.strftime("%-m/%-d/%Y")
        if published not in page_text:
            raise ValueError(
                f"OZK page does not prove publication date {published}: {path}"
            )
        common = {
            "ticker": "OZK",
            "fiscal_end": entry.fiscal_end,
            "available_date": entry.available_date,
            "taxonomy": "issuer_gaap",
            "form": "ISSUER_QUARTERLY_EARNINGS_RELEASE",
            "accession": f"ozk-ir-{entry.available_date:%Y%m%d}-{entry.fiscal_end:%Y%m%d}",
            "unit": "USD",
            "source": "direct_issuer_three_month_gaap_statement",
            "source_archive": path.name,
            "source_archive_sha256": _sha256(path),
            "derivation_prior_accession": "",
        }
        records.extend([
            {
                **common,
                "metric": "revenue",
                "value": values["revenue"],
                "concept": "NetInterestIncome+NoninterestIncome",
            },
            {
                **common,
                "metric": "net_income",
                "value": values["net_income"],
                "concept": "NetIncome",
            },
        ])
        recovered.append({
            "ticker": "OZK",
            "fiscal_end": entry.fiscal_end.strftime("%Y-%m-%d"),
            "available_date": entry.available_date.strftime("%Y-%m-%d"),
            **values,
            "derivation": "direct_three_month_issuer_statement",
        })
        bindings.append({
            "fiscal_end": entry.fiscal_end.strftime("%Y-%m-%d"),
            "available_date": entry.available_date.strftime("%Y-%m-%d"),
            "source_url": source_url,
            "archive_url": archive_url,
            "archive_timestamp": entry.archive_timestamp,
            "path": str(path),
            "sha256": _sha256(path),
        })

    quarters = pd.DataFrame(records, columns=OUTPUT_COLUMNS).sort_values(
        ["fiscal_end", "available_date", "metric"]
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    quarters_path = output_dir / "strict_quarterly_facts.csv"
    quarters.to_csv(quarters_path, index=False)
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "promotion_eligible": False,
        "release_status": "BLOCKED",
        "ticker": "OZK",
        "issuer": "Bank OZK",
        "quarter_fact_version_count": 15,
        "unique_fiscal_quarter_count": int(quarters["fiscal_end"].nunique()),
        "missing_contemporaneous_quarters": ["2020-03-31"],
        "recovered_quarters": recovered,
        "filings": bindings,
        "registry": {"path": str(registry_path), "sha256": _sha256(registry_path)},
        "outputs": {
            "quarters": {"path": str(quarters_path), "sha256": _sha256(quarters_path)}
        },
        "guardrail": (
            "Uses only direct three-month issuer tables and the release date "
            "printed on each primary investor-relations page. Bank revenue is "
            "net interest income plus total non-interest income, excluding the "
            "separately disclosed FTE adjustment. The absent contemporaneous "
            "2020Q1 page is not backdated: its direct comparative column first "
            "becomes available with the 2021Q1 release on 2021-04-22. Wayback "
            "capture timestamps and content hashes are bound. Research-only; "
            "no formal replacement or trading authorization."
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
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    result = run(
        registry_path=args.registry,
        output_dir=args.output_dir,
        download_missing=not args.offline,
    )
    print(json.dumps({
        "manifest": result["manifest"],
        "quarter_fact_version_count": result["quarter_fact_version_count"],
        "unique_fiscal_quarter_count": result["unique_fiscal_quarter_count"],
        "promotion_eligible": False,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
