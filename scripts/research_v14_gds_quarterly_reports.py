#!/usr/bin/env python3
"""Recover GDS's SEC-filed GAAP quarters for v14 research only."""

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


DEFAULT_REGISTRY = Path("stocks_list_dir/nasdaq/gds_quarterly_reports.csv")
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/gds_sec_quarterly_reports_2017_2021"
)
USER_AGENT = "quant_stocks research data@example.com"
REVENUE_LABELS = ("Total net revenue", "Net revenue")
NET_LABELS = (
    "Net income (loss)",
    "Net (loss) income",
    "Net loss",
    "Net income",
)
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


def _header_date_matches(value: object, fiscal_end: pd.Timestamp) -> bool:
    text = _normal(value).replace(" ,", ",")
    match = re.search(
        r"(?:January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+\d{1,2},?\s+\d{4}",
        text,
        flags=re.IGNORECASE,
    )
    if match is None:
        return False
    parsed = pd.to_datetime(match.group(0), errors="coerce")
    return bool(pd.notna(parsed) and pd.Timestamp(parsed).normalize() == fiscal_end)


def _period_columns(
    table: pd.DataFrame,
    *,
    fiscal_end: pd.Timestamp,
    period_phrase: str,
) -> list[object]:
    selected = []
    for column in table.columns:
        header = [_normal(value) for value in table.head(8)[column]]
        if not any(period_phrase.lower() in value.lower() for value in header):
            continue
        if not any(_header_date_matches(value, fiscal_end) for value in header):
            continue
        if "RMB" not in header:
            continue
        selected.append(column)
    if not selected:
        raise ValueError(
            f"GDS filing table has no RMB {period_phrase!r} "
            f"{fiscal_end.date()} column"
        )
    return selected


def _row_values(
    table: pd.DataFrame,
    labels: tuple[str, ...],
    columns: list[object],
) -> list[float]:
    observed_labels = table.iloc[:, 0].fillna("").map(_normal)
    values = []
    for label in labels:
        rows = table.loc[observed_labels.eq(label)]
        for _, row in rows.iterrows():
            for column in columns:
                parsed = _parse_accounting_number(row[column])
                if parsed is not None:
                    values.append(parsed)
    return values


def _parse_period(
    path: Path,
    *,
    fiscal_end: pd.Timestamp,
    period_phrase: str,
) -> dict[str, float]:
    raw = path.read_bytes()
    text = _normal(BeautifulSoup(raw, "html.parser").get_text(" ", strip=True))
    if "GDS" not in text or "unaudited financial results" not in text.lower():
        raise ValueError(f"GDS exhibit is not a quarterly-results filing: {path}")
    if "in thousands" not in text.lower():
        raise ValueError(f"GDS filing does not prove thousands units: {path}")

    revenue_values: list[float] = []
    net_values: list[float] = []
    for table in pd.read_html(path):
        try:
            columns = _period_columns(
                table,
                fiscal_end=fiscal_end,
                period_phrase=period_phrase,
            )
        except ValueError:
            continue
        revenue_values.extend(_row_values(table, REVENUE_LABELS, columns))
        net_values.extend(_row_values(table, NET_LABELS, columns))

    revenues = sorted(set(revenue_values))
    net_incomes = sorted(set(net_values))
    if len(revenues) != 1 or len(net_incomes) != 1:
        raise ValueError(
            f"expected one agreeing GDS {period_phrase} statement pair in "
            f"{path}, found revenue={revenues}, net_income={net_incomes}"
        )
    return {
        "revenue": revenues[0] * 1000.0,
        "net_income": net_incomes[0] * 1000.0,
    }


def parse_gds_quarter(path: Path, fiscal_end: pd.Timestamp) -> dict[str, float]:
    """Parse only the filing's explicit three-month RMB GAAP columns."""
    return _parse_period(
        path,
        fiscal_end=fiscal_end,
        period_phrase="Three months ended",
    )


def _download(source_url: str, local_path: Path) -> None:
    if local_path.exists():
        return
    if not source_url.startswith(
        "https://www.sec.gov/Archives/edgar/data/1526125/"
    ):
        raise ValueError(f"GDS registry contains a non-SEC source: {source_url}")
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
            if len(payload) < 50_000 or b"<html" not in payload[:2_000].lower():
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
    if set(registry["ticker"]) != {"GDS"} or set(registry["cik"]) != {1526125}:
        raise ValueError("GDS registry contains another issuer")
    if len(registry) != 20 or registry.duplicated("fiscal_end").any():
        raise ValueError("GDS registry must contain exactly 20 unique quarters")
    expected_ends = list(pd.date_range("2017-03-31", "2021-12-31", freq="QE"))
    if registry.sort_values("fiscal_end")["fiscal_end"].tolist() != expected_ends:
        raise ValueError("GDS registry is not the complete 2017Q1-2021Q4 chain")

    rows: list[dict] = []
    recovered: list[dict] = []
    bindings: list[dict] = []
    annual_values: dict[int, dict[str, float]] = {}
    for entry in registry.sort_values("fiscal_end").itertuples(index=False):
        path = Path(entry.local_path)
        if download_missing:
            _download(entry.source_url, path)
        if not path.exists():
            raise FileNotFoundError(path)
        values = parse_gds_quarter(path, entry.fiscal_end)
        if entry.fiscal_end.month == 12:
            annual_values[entry.fiscal_end.year] = _parse_period(
                path,
                fiscal_end=entry.fiscal_end,
                period_phrase="Year ended",
            )
        lag_days = int((entry.filed_date - entry.fiscal_end).days)
        if not 0 <= lag_days <= 150:
            raise ValueError(f"GDS filing is not timely: {entry.accession}")
        common = {
            "ticker": "GDS",
            "fiscal_end": entry.fiscal_end,
            "available_date": entry.filed_date,
            "taxonomy": "us-gaap",
            "form": entry.form,
            "accession": entry.accession,
            "unit": "CNY",
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
                "ticker": "GDS",
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
    for year, annual in sorted(annual_values.items()):
        year_rows = quarter_frame.loc[quarter_frame["fiscal_end"].dt.year.eq(year)]
        if len(year_rows) != 4:
            raise RuntimeError(f"GDS {year} has {len(year_rows)} quarters")
        quarter_sum = {
            metric: float(year_rows[metric].sum())
            for metric in ("revenue", "net_income")
        }
        if quarter_sum != annual:
            raise RuntimeError(
                f"GDS {year} original quarters do not reconcile the annual: "
                f"{quarter_sum} != {annual}"
            )
        annual_cross_checks.append(
            {
                "year": year,
                "quarter_sum": quarter_sum,
                "filed_annual": annual,
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
        raise RuntimeError(f"GDS quarter chain is not continuous: {longest}/20")

    output_dir.mkdir(parents=True, exist_ok=True)
    quarters_path = output_dir / "strict_quarterly_facts.csv"
    quarters.to_csv(quarters_path, index=False)
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "promotion_eligible": False,
        "release_status": "BLOCKED",
        "ticker": "GDS",
        "cik": 1526125,
        "currency": "CNY",
        "gaap_only": True,
        "issuer_identity": {
            "sec_name": "GDS Holdings Ltd",
            "nasdaq_ticker": "GDS",
            "continuous_2017_2021": True,
            "former_sec_names": [],
        },
        "quarter_count": 20,
        "quarter_fact_version_count": len(recovered),
        "restatement_version_count": 0,
        "longest_continuous_timely_paired_quarters": longest,
        "recovered_quarters": recovered,
        "restatement_versions": [],
        "annual_cross_checks": annual_cross_checks,
        "registry": {"path": str(registry_path), "sha256": _sha256(registry_path)},
        "filings": bindings,
        "outputs": {
            "quarters": {"path": str(quarters_path), "sha256": _sha256(quarters_path)}
        },
        "guardrail": (
            "Only direct SEC-filed three-month GAAP statements in RMB are "
            "accepted. USD convenience translations and non-GAAP adjusted "
            "results are excluded. Availability is the actual SEC filing date. "
            "This artifact is research-only, does not modify formal "
            "fundamentals, and does not authorize trading."
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
                "quarter_fact_version_count": result["quarter_fact_version_count"],
                "longest_continuous_timely_paired_quarters": result[
                    "longest_continuous_timely_paired_quarters"
                ],
                "promotion_eligible": False,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
