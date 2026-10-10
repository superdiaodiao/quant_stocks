#!/usr/bin/env python3
"""Recover NXPI's SEC-filed direct GAAP quarters for v14 research only."""

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


DEFAULT_REGISTRY = Path("stocks_list_dir/nasdaq/nxpi_quarterly_reports.csv")
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/nxpi_sec_quarterly_reports_2017_2019"
)
USER_AGENT = "quant_stocks research data@example.com"
OUTPUT_COLUMNS = [
    "ticker", "fiscal_end", "available_date", "metric", "value",
    "taxonomy", "concept", "form", "accession", "unit", "source",
    "source_archive", "source_archive_sha256", "derivation_prior_accession",
]
EXPECTED_ENDS = [
    pd.Timestamp(value) for value in (
        "2017-04-02", "2017-07-02", "2017-10-01", "2017-12-31",
        "2018-04-01", "2018-07-01", "2018-09-30", "2018-12-31",
        "2019-03-31", "2019-06-30", "2019-09-29", "2019-12-31",
    )
]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normal(value: object) -> str:
    if pd.isna(value):
        return ""
    return " ".join(str(value).replace("\xa0", " ").split()).strip()


def _period_columns(
    table: pd.DataFrame,
    *,
    period_label: str,
    period_phrases: tuple[str, ...],
) -> list[object]:
    phrase_columns: set[object] = set()
    label_columns: set[object] = set()
    normalized_label = _normal(period_label).lower()
    for _, row in table.head(8).iterrows():
        for column, value in row.items():
            text = _normal(value)
            lower = text.lower()
            if any(phrase.lower() in lower for phrase in period_phrases):
                phrase_columns.add(column)
            if lower == normalized_label:
                label_columns.add(column)
    selected = [
        column for column in table.columns
        if column in phrase_columns and column in label_columns
    ]
    if not selected:
        raise ValueError(
            f"NXPI table has no {period_phrases!r} {period_label!r} columns"
        )
    return selected


def _row_value(
    table: pd.DataFrame,
    *,
    label: str,
    columns: list[object],
) -> float:
    observed = table.iloc[:, 0].fillna("").map(_normal)
    rows = table.loc[observed.str.casefold().eq(label.casefold())]
    if len(rows) != 1:
        raise ValueError(f"expected one NXPI row labelled {label!r}")
    values = sorted({
        parsed
        for column in columns
        if (parsed := _parse_accounting_number(rows.iloc[0][column])) is not None
    })
    if len(values) != 1:
        raise ValueError(
            f"expected one NXPI value for {label!r}, found {values}"
        )
    return values[0]


def _proves_usd_millions(raw: bytes) -> bool:
    text = _normal(BeautifulSoup(raw, "html.parser").get_text(" ", strip=True))
    if "nxp semiconductors" not in text.lower():
        return False
    return bool(re.search(
        r"(?:\$\s*in\s+millions|in\s+millions\s+of\s+(?:u\.s\.\s+)?dollars?"
        r"|amounts\s+in\s+millions\s+of\s+usd)",
        text,
        flags=re.IGNORECASE,
    ))


def _parse_period(
    path: Path,
    *,
    period_label: str,
    period_phrases: tuple[str, ...],
) -> dict[str, float]:
    raw = path.read_bytes()
    if not _proves_usd_millions(raw):
        raise ValueError(f"NXPI source does not prove issuer/USD-millions: {path}")
    matches: list[tuple[float, float]] = []
    for table in pd.read_html(path):
        try:
            columns = _period_columns(
                table,
                period_label=period_label,
                period_phrases=period_phrases,
            )
            revenue = _row_value(table, label="Revenue", columns=columns)
            net_income = _row_value(
                table, label="Net income (loss)", columns=columns
            )
        except ValueError:
            continue
        if revenue > 100:
            matches.append((revenue, net_income))
    pairs = sorted(set(matches))
    if len(pairs) != 1:
        raise ValueError(
            f"expected one direct NXPI statement pair in {path}; pairs={pairs}"
        )
    return {
        "revenue": pairs[0][0] * 1_000_000.0,
        "net_income": pairs[0][1] * 1_000_000.0,
    }


def parse_nxpi_quarter(
    path: Path,
    *,
    period_label: str,
) -> dict[str, float]:
    """Parse only an explicit three-month GAAP statement column."""
    return _parse_period(
        path,
        period_label=period_label,
        period_phrases=("Three months ended",),
    )


def _download(source_url: str, local_path: Path) -> None:
    if local_path.exists():
        return
    if not source_url.startswith(
        "https://www.sec.gov/Archives/edgar/data/1413447/"
    ):
        raise ValueError(f"NXPI registry contains a non-SEC source: {source_url}")
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
            if len(payload) < 100_000 or b"<html" not in payload[:2_000].lower():
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
    if set(registry["ticker"]) != {"NXPI"} or set(registry["cik"]) != {1413447}:
        raise ValueError("NXPI registry contains another issuer")
    if len(registry) != 12 or registry.duplicated("fiscal_end").any():
        raise ValueError("NXPI registry must contain 12 unique quarters")
    if registry.sort_values("fiscal_end")["fiscal_end"].tolist() != EXPECTED_ENDS:
        raise ValueError("NXPI registry is not the complete 2017Q1-2019Q4 chain")

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
        values = parse_nxpi_quarter(path, period_label=entry.period_label)
        if entry.fiscal_end.month == 12:
            annual_values[entry.fiscal_end.year] = _parse_period(
                path,
                period_label=str(entry.fiscal_end.year),
                period_phrases=("Full Year", "Full-year", "Year Ended"),
            )
        lag_days = int((entry.filed_date - entry.fiscal_end).days)
        if not 0 <= lag_days <= 150:
            raise ValueError(f"NXPI filing is not timely: {entry.accession}")
        common = {
            "ticker": "NXPI", "fiscal_end": entry.fiscal_end,
            "available_date": entry.filed_date, "taxonomy": "us-gaap",
            "form": entry.form, "accession": entry.accession, "unit": "USD",
            "source": "explicit_sec_filed_three_month_gaap_statement",
            "source_archive": path.name, "source_archive_sha256": _sha256(path),
            "derivation_prior_accession": "",
        }
        rows.extend([
            {**common, "metric": "revenue", "value": values["revenue"],
             "concept": "RevenueFromContractWithCustomerExcludingAssessedTax"},
            {**common, "metric": "net_income", "value": values["net_income"],
             "concept": "NetIncomeLoss"},
        ])
        recovered.append({
            "ticker": "NXPI", "fiscal_end": entry.fiscal_end.strftime("%Y-%m-%d"),
            "available_date": entry.filed_date.strftime("%Y-%m-%d"),
            "availability_lag_days": lag_days, **values,
            "derivation": "direct_three_month_sec_filing_gaap_statement",
        })
        bindings.append({
            "accession": entry.accession,
            "fiscal_end": entry.fiscal_end.strftime("%Y-%m-%d"),
            "filed_date": entry.filed_date.strftime("%Y-%m-%d"),
            "path": str(path), "sha256": _sha256(path),
            "source_url": entry.source_url,
        })

    frame = pd.DataFrame(recovered)
    frame["fiscal_end"] = pd.to_datetime(frame["fiscal_end"])
    annual_cross_checks = []
    for year, annual in sorted(annual_values.items()):
        year_rows = frame.loc[frame["fiscal_end"].dt.year.eq(year)]
        quarter_sum = {
            metric: float(year_rows[metric].sum())
            for metric in ("revenue", "net_income")
        }
        if len(year_rows) != 4 or quarter_sum != annual:
            raise RuntimeError(
                f"NXPI {year} quarters do not reconcile annual: "
                f"{quarter_sum} != {annual}"
            )
        annual_cross_checks.append({
            "year": year, "quarter_sum": quarter_sum,
            "filed_annual": annual, "exact_match": True,
        })

    quarters = pd.DataFrame(rows, columns=OUTPUT_COLUMNS).sort_values(
        ["fiscal_end", "available_date", "metric"]
    )
    paired = quarters.groupby("fiscal_end")["metric"].nunique()
    longest = _longest_chain(paired.loc[paired.eq(2)].index.tolist())
    if longest != 12:
        raise RuntimeError(f"NXPI quarter chain is not continuous: {longest}/12")

    output_dir.mkdir(parents=True, exist_ok=True)
    quarters_path = output_dir / "strict_quarterly_facts.csv"
    quarters.to_csv(quarters_path, index=False)
    report = {
        "schema_version": 1, "research_only": True,
        "point_in_time_proven": True, "promotion_eligible": False,
        "release_status": "BLOCKED", "ticker": "NXPI", "cik": 1413447,
        "currency": "USD", "accounting_standard": "US GAAP",
        "gaap_only": True,
        "issuer_identity": {
            "sec_name": "NXP Semiconductors N.V.", "ticker": "NXPI",
            "continuous_2017_2019": True,
            "reporting_transition": "20-F/6-K to 10-K/10-Q in 2019",
        },
        "quarter_count": 12, "quarter_fact_version_count": 12,
        "restatement_version_count": 0,
        "longest_continuous_timely_paired_quarters": longest,
        "recovered_quarters": recovered, "restatement_versions": [],
        "annual_cross_checks": annual_cross_checks,
        "registry": {"path": str(registry_path), "sha256": _sha256(registry_path)},
        "filings": bindings,
        "outputs": {"quarters": {
            "path": str(quarters_path), "sha256": _sha256(quarters_path),
        }},
        "guardrail": (
            "Only direct SEC-filed three-month GAAP Revenue and Net income "
            "(loss) statement rows in USD millions are accepted. Adjusted "
            "metrics and annual-minus-YTD derivations are excluded. Later "
            "domestic-filer facts do not backdate availability. Research-only; "
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
        "manifest": result["manifest"], "quarter_count": result["quarter_count"],
        "longest_continuous_timely_paired_quarters": result[
            "longest_continuous_timely_paired_quarters"
        ], "promotion_eligible": False,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
