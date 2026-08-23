#!/usr/bin/env python3
"""Recover KRNT's SEC-filed GAAP quarters for v14 research only."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pandas as pd
from bs4 import BeautifulSoup

from scripts.research_v14_sec_filing_exhibit_financials import (
    _parse_accounting_number,
)


DEFAULT_REGISTRY = Path("stocks_list_dir/nasdaq/krnt_quarterly_reports.csv")
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/krnt_sec_quarterly_reports_2017_2021"
)
USER_AGENT = "quant_stocks research data@example.com"
REVENUE_LABELS = ("Total revenues", "Revenues, net", "Revenues")
NET_LABELS = ("Net income (loss)", "Net (loss) income", "Net income", "Net loss")
OUTPUT_COLUMNS = [
    "ticker",
    "fiscal_end",
    "available_date",
    "metric",
    "value",
    "taxonomy",
    "concept",
    "form",
    "accession",
    "unit",
    "source",
    "source_archive",
    "source_archive_sha256",
    "derivation_prior_accession",
]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normal(value: object) -> str:
    return " ".join(str(value).replace("\xa0", " ").split()).strip()


def _period_columns(
    table: pd.DataFrame,
    *,
    year: int,
    period_phrase: str,
) -> list[object]:
    phrase_columns: set[object] = set()
    year_columns: set[object] = set()
    for _, row in table.head(8).iterrows():
        for column, value in row.items():
            text = _normal(value)
            if period_phrase.lower() in text.lower():
                phrase_columns.add(column)
            if text == str(year):
                year_columns.add(column)
    selected = [
        column
        for column in table.columns
        if column in phrase_columns and column in year_columns
    ]
    if not selected:
        raise ValueError(
            f"KRNT filing table has no {period_phrase!r} {year} columns"
        )
    return selected


def _row_value(
    table: pd.DataFrame,
    labels: tuple[str, ...],
    columns: list[object],
) -> tuple[str, float]:
    observed_labels = table.iloc[:, 0].fillna("").map(_normal)
    for label in labels:
        rows = table.loc[observed_labels.eq(label)]
        if len(rows) != 1:
            continue
        parsed = [
            value
            for column in columns
            if (value := _parse_accounting_number(rows.iloc[0][column])) is not None
        ]
        values = sorted(set(parsed))
        if len(values) == 1:
            return label, values[0]
    raise ValueError(f"expected one KRNT statement value for one of {labels!r}")


def _proves_usd_thousands(raw: bytes) -> bool:
    text = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
    normalized = _normal(text)
    return bool(
        re.search(
            r"(?:U\.S\.\s*(?:dollars?|\$)|US\s*dollars?|\$)\s+in\s+thousands",
            normalized,
            flags=re.IGNORECASE,
        )
    )


def _parse_period(
    path: Path,
    *,
    fiscal_end: pd.Timestamp,
    period_phrase: str,
) -> dict[str, float]:
    raw = path.read_bytes()
    if not _proves_usd_thousands(raw):
        raise ValueError(f"KRNT filing does not prove USD-thousands units: {path}")
    matches: list[tuple[float, float]] = []
    for table in pd.read_html(path):
        try:
            columns = _period_columns(
                table,
                year=fiscal_end.year,
                period_phrase=period_phrase,
            )
            _, revenue = _row_value(table, REVENUE_LABELS, columns)
            _, net_income = _row_value(table, NET_LABELS, columns)
        except ValueError:
            continue
        if revenue > 1_000:
            matches.append((revenue, net_income))
    values = sorted(set(matches))
    if len(values) != 1:
        raise ValueError(
            f"expected one agreeing KRNT {period_phrase} statement pair in "
            f"{path}, found {values}"
        )
    return {
        "revenue": values[0][0] * 1000.0,
        "net_income": values[0][1] * 1000.0,
    }


def parse_krnt_quarter(path: Path, fiscal_end: pd.Timestamp) -> dict[str, float]:
    """Parse only the filing's explicit three-month GAAP statement columns."""
    return _parse_period(
        path,
        fiscal_end=fiscal_end,
        period_phrase="Three Months Ended",
    )


def _download(source_url: str, local_path: Path) -> None:
    if local_path.exists():
        return
    if not source_url.startswith(
        "https://www.sec.gov/Archives/edgar/data/1625791/"
    ):
        raise ValueError(f"KRNT registry contains a non-SEC source: {source_url}")
    local_path.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            request = Request(
                source_url,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
            )
            with urlopen(request, timeout=60) as response:
                payload = response.read()
            if len(payload) < 10_000 or b"<html" not in payload[:2_000].lower():
                raise ValueError(f"unexpected SEC filing payload: {source_url}")
            temporary = local_path.with_suffix(local_path.suffix + ".tmp")
            temporary.write_bytes(payload)
            temporary.replace(local_path)
            time.sleep(1)
            return
        except (HTTPError, URLError, TimeoutError, ValueError) as error:
            last_error = error
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"failed to download {source_url}: {last_error}")


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
    download_missing: bool = True,
) -> dict:
    registry = pd.read_csv(
        registry_path,
        dtype={"ticker": str, "cik": int, "accession": str},
        parse_dates=["fiscal_end", "filed_date"],
    )
    if set(registry["ticker"]) != {"KRNT"} or set(registry["cik"]) != {1625791}:
        raise ValueError("KRNT registry contains another issuer")
    if len(registry) != 20 or registry.duplicated("fiscal_end").any():
        raise ValueError("KRNT registry must contain exactly 20 unique quarters")
    expected_ends = list(pd.date_range("2017-03-31", "2021-12-31", freq="QE"))
    if registry.sort_values("fiscal_end")["fiscal_end"].tolist() != expected_ends:
        raise ValueError("KRNT registry is not the complete 2017Q1-2021Q4 chain")

    rows: list[dict] = []
    recovered: list[dict] = []
    bindings: list[dict] = []
    annual_values: dict[int, dict[str, float]] = {}
    original_accessions: dict[pd.Timestamp, str] = {}
    for entry in registry.sort_values("fiscal_end").itertuples(index=False):
        path = Path(entry.local_path)
        if download_missing:
            _download(entry.source_url, path)
        if not path.exists():
            raise FileNotFoundError(path)
        values = parse_krnt_quarter(path, entry.fiscal_end)
        original_accessions[entry.fiscal_end] = entry.accession
        if entry.fiscal_end.month == 12:
            annual_values[entry.fiscal_end.year] = _parse_period(
                path,
                fiscal_end=entry.fiscal_end,
                period_phrase="Year Ended",
            )
        lag_days = int((entry.filed_date - entry.fiscal_end).days)
        if not 0 <= lag_days <= 150:
            raise ValueError(f"KRNT filing is not timely: {entry.accession}")
        common = {
            "ticker": "KRNT",
            "fiscal_end": entry.fiscal_end,
            "available_date": entry.filed_date,
            "taxonomy": "us-gaap",
            "form": entry.form,
            "accession": entry.accession,
            "unit": "USD",
            "source": "explicit_sec_filed_three_month_gaap_statement",
            "source_archive": path.name,
            "source_archive_sha256": _sha256(path),
            "derivation_prior_accession": "",
        }
        rows.extend(
            [
                {
                    **common,
                    "metric": "revenue",
                    "value": values["revenue"],
                    "concept": "Revenues",
                },
                {
                    **common,
                    "metric": "net_income",
                    "value": values["net_income"],
                    "concept": "NetIncomeLoss",
                },
            ]
        )
        recovered.append(
            {
                "ticker": "KRNT",
                "fiscal_end": entry.fiscal_end.strftime("%Y-%m-%d"),
                "available_date": entry.filed_date.strftime("%Y-%m-%d"),
                "availability_lag_days": lag_days,
                **values,
                "derivation": "direct_three_month_sec_filing_gaap_statement",
            }
        )
        bindings.append(
            {
                "accession": entry.accession,
                "fiscal_end": entry.fiscal_end.strftime("%Y-%m-%d"),
                "filed_date": entry.filed_date.strftime("%Y-%m-%d"),
                "path": str(path),
                "sha256": _sha256(path),
                "source_url": entry.source_url,
            }
        )

    quarter_frame = pd.DataFrame(recovered)
    quarter_frame["fiscal_end"] = pd.to_datetime(quarter_frame["fiscal_end"])
    annual_cross_checks = []
    restatement_versions = []
    for year, annual in sorted(annual_values.items()):
        year_rows = quarter_frame.loc[quarter_frame["fiscal_end"].dt.year.eq(year)]
        original_sum = {
            metric: float(year_rows[metric].sum())
            for metric in ("revenue", "net_income")
        }
        if len(year_rows) != 4:
            raise RuntimeError(f"KRNT {year} has {len(year_rows)} original quarters")
        method = "original_quarter_sum"
        checked_sum = original_sum
        if original_sum != annual:
            comparative_registry = registry.loc[
                registry["fiscal_end"].dt.year.eq(year + 1)
            ].sort_values("fiscal_end")
            if len(comparative_registry) != 4:
                raise RuntimeError(
                    f"KRNT {year} annual mismatch has no complete later comparatives"
                )
            comparative_sum = {"revenue": 0.0, "net_income": 0.0}
            for entry in comparative_registry.itertuples(index=False):
                restated_end = entry.fiscal_end - pd.DateOffset(years=1)
                path = Path(entry.local_path)
                values = _parse_period(
                    path,
                    fiscal_end=restated_end,
                    period_phrase="Three Months Ended",
                )
                for metric in comparative_sum:
                    comparative_sum[metric] += values[metric]
                prior_accession = original_accessions[restated_end]
                common = {
                    "ticker": "KRNT",
                    "fiscal_end": restated_end,
                    "available_date": entry.filed_date,
                    "taxonomy": "us-gaap",
                    "form": entry.form,
                    "accession": entry.accession,
                    "unit": "USD",
                    "source": (
                        "explicit_sec_filed_comparative_three_month_gaap_restatement"
                    ),
                    "source_archive": path.name,
                    "source_archive_sha256": _sha256(path),
                    "derivation_prior_accession": prior_accession,
                }
                rows.extend(
                    [
                        {
                            **common,
                            "metric": "revenue",
                            "value": values["revenue"],
                            "concept": "Revenues",
                        },
                        {
                            **common,
                            "metric": "net_income",
                            "value": values["net_income"],
                            "concept": "NetIncomeLoss",
                        },
                    ]
                )
                recovered.append(
                    {
                        "ticker": "KRNT",
                        "fiscal_end": restated_end.strftime("%Y-%m-%d"),
                        "available_date": entry.filed_date.strftime("%Y-%m-%d"),
                        "availability_lag_days": int(
                            (entry.filed_date - restated_end).days
                        ),
                        **values,
                        "derivation": "later_sec_filed_comparative_restatement",
                        "derivation_prior_accession": prior_accession,
                    }
                )
                restatement_versions.append(
                    {
                        "fiscal_end": restated_end.strftime("%Y-%m-%d"),
                        "available_date": entry.filed_date.strftime("%Y-%m-%d"),
                        "original_accession": prior_accession,
                        "restatement_accession": entry.accession,
                        **values,
                    }
                )
            checked_sum = comparative_sum
            method = "later_comparative_quarter_sum"
            if comparative_sum != annual:
                raise RuntimeError(
                    f"KRNT {year} later comparatives do not reconcile the annual: "
                    f"{comparative_sum} != {annual}"
                )
        annual_cross_checks.append(
            {
                "year": year,
                "original_quarter_sum": original_sum,
                "checked_quarter_sum": checked_sum,
                "filed_annual": annual,
                "method": method,
                "exact_match": True,
            }
        )

    quarters = pd.DataFrame(rows, columns=OUTPUT_COLUMNS).sort_values(
        ["fiscal_end", "available_date", "metric"]
    )
    paired = quarters.groupby("fiscal_end")["metric"].nunique()
    paired_ends = paired.loc[paired.eq(2)].index.tolist()
    longest = _longest_chain(paired_ends)
    if longest != 20:
        raise RuntimeError(f"KRNT quarter chain is not continuous: {longest}/20")

    output_dir.mkdir(parents=True, exist_ok=True)
    quarters_path = output_dir / "strict_quarterly_facts.csv"
    quarters.to_csv(quarters_path, index=False)
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "promotion_eligible": False,
        "release_status": "BLOCKED",
        "ticker": "KRNT",
        "cik": 1625791,
        "currency": "USD",
        "gaap_only": True,
        "quarter_count": 20,
        "quarter_fact_version_count": len(recovered),
        "restatement_version_count": len(restatement_versions),
        "longest_continuous_timely_paired_quarters": longest,
        "recovered_quarters": recovered,
        "restatement_versions": restatement_versions,
        "annual_cross_checks": annual_cross_checks,
        "registry": {"path": str(registry_path), "sha256": _sha256(registry_path)},
        "filings": bindings,
        "outputs": {
            "quarters": {"path": str(quarters_path), "sha256": _sha256(quarters_path)}
        },
        "guardrail": (
            "Only direct SEC-filed three-month GAAP statements in USD are accepted. "
            "Later comparative restatements are bound to their actual later filing "
            "dates and never backfilled into the original decision date. Non-GAAP "
            "adjusted results are excluded. This artifact is research-only, "
            "does not modify formal fundamentals, and does not authorize trading."
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
    print(
        json.dumps(
            {
                "manifest": result["manifest"],
                "quarter_count": result["quarter_count"],
                "annual_cross_check_count": len(result["annual_cross_checks"]),
                "promotion_eligible": False,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
