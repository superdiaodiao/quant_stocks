#!/usr/bin/env python3
"""Recover BILI's SEC-filed direct GAAP quarters for v14 research only."""

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


DEFAULT_REGISTRY = Path("stocks_list_dir/nasdaq/bili_quarterly_reports.csv")
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/bili_sec_quarterly_reports_2018_2020"
)
USER_AGENT = "quant_stocks research data@example.com"
OUTPUT_COLUMNS = [
    "ticker", "fiscal_end", "available_date", "metric", "value",
    "taxonomy", "concept", "form", "accession", "unit", "source",
    "source_archive", "source_archive_sha256", "derivation_prior_accession",
]
EXPECTED_ENDS = list(pd.date_range("2018-03-31", "2020-12-31", freq="QE"))
COMPARATIVE_ENDS = {
    end: end - pd.DateOffset(years=1) for end in EXPECTED_ENDS[:4]
}


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
    fiscal_end: pd.Timestamp,
    period_phrase: str,
) -> list[object]:
    phrase_columns: set[object] = set()
    date_columns: set[object] = set()
    year_columns: set[object] = set()
    currency_columns: set[object] = set()
    combined_date_columns: set[object] = set()
    month_day = fiscal_end.strftime("%B %-d").lower()
    combined_date = fiscal_end.strftime("%B %-d, %Y").lower()
    for _, row in table.head(9).iterrows():
        for column, value in row.items():
            text = _normal(value)
            lower = text.lower().rstrip(",")
            if period_phrase.lower() in lower:
                phrase_columns.add(column)
            if lower == month_day:
                date_columns.add(column)
            if lower == combined_date:
                combined_date_columns.add(column)
            if text == str(fiscal_end.year):
                year_columns.add(column)
            if text.casefold() == "rmb":
                currency_columns.add(column)
    selected = [
        column for column in table.columns
        if column in phrase_columns
        and column in currency_columns
        and (
            (column in date_columns and column in year_columns)
            or column in combined_date_columns
        )
    ]
    if not selected:
        raise ValueError(
            f"BILI table has no RMB {period_phrase!r} {fiscal_end.date()} columns"
        )
    return selected


def _row_label(row: pd.Series) -> str:
    values = [_normal(value) for value in row.iloc[: min(4, len(row))]]
    return next((value for value in values if value), "")


def _row_value(
    table: pd.DataFrame,
    *,
    label: str,
    columns: list[object],
) -> float:
    rows = table.loc[
        table.apply(_row_label, axis=1).str.casefold().eq(label.casefold())
    ]
    if rows.empty:
        raise ValueError(f"expected at least one BILI row labelled {label!r}")
    values = sorted({
        parsed
        for _, row in rows.iterrows()
        for column in columns
        if (parsed := _parse_accounting_number(row[column])) is not None
    })
    if len(values) != 1:
        raise ValueError(f"expected one BILI value for {label!r}; values={values}")
    return values[0]


def _proves_rmb_thousands(raw: bytes) -> bool:
    text = _normal(BeautifulSoup(raw, "html.parser").get_text(" ", strip=True))
    lower = text.lower()
    if "bilibili inc" not in lower:
        return False
    return bool(re.search(
        r"(?:amounts|expressed)\s+in\s+thousands(?:\s+of\s+rmb)?|"
        r"rmb\s+in\s+thousands",
        text,
        flags=re.IGNORECASE,
    ))


def _parse_period(
    path: Path,
    *,
    fiscal_end: pd.Timestamp,
    period_phrase: str,
) -> dict[str, float]:
    raw = path.read_bytes()
    if not _proves_rmb_thousands(raw):
        raise ValueError(f"BILI source does not prove issuer/RMB-thousands: {path}")
    pairs: list[tuple[float, float]] = []
    for table in pd.read_html(path):
        try:
            columns = _period_columns(
                table, fiscal_end=fiscal_end, period_phrase=period_phrase
            )
            revenue = _row_value(
                table, label="Total net revenues", columns=columns
            )
            net_income = _row_value(table, label="Net loss", columns=columns)
        except ValueError:
            continue
        if revenue > 100_000:
            pairs.append((revenue, net_income))
    pairs = sorted(set(pairs))
    if len(pairs) != 1:
        raise ValueError(
            f"expected one direct BILI {period_phrase} pair in {path}; pairs={pairs}"
        )
    return {
        "revenue": pairs[0][0] * 1000.0,
        "net_income": pairs[0][1] * 1000.0,
    }


def parse_bili_quarter(path: Path, fiscal_end: pd.Timestamp) -> dict[str, float]:
    """Parse only the explicit three-month RMB GAAP statement column."""
    return _parse_period(
        path, fiscal_end=fiscal_end, period_phrase="Three Months Ended"
    )


def _download(source_url: str, local_path: Path) -> None:
    if local_path.exists():
        return
    if not source_url.startswith(
        "https://www.sec.gov/Archives/edgar/data/1723690/"
    ):
        raise ValueError(f"BILI registry contains a non-SEC source: {source_url}")
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
    raise RuntimeError(f"failed download {source_url}: {last_error}")


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
    if set(registry["ticker"]) != {"BILI"} or set(registry["cik"]) != {1723690}:
        raise ValueError("BILI registry contains another issuer")
    if len(registry) != 12 or registry.duplicated("fiscal_end").any():
        raise ValueError("BILI registry must contain 12 unique quarters")
    if registry.sort_values("fiscal_end")["fiscal_end"].tolist() != EXPECTED_ENDS:
        raise ValueError("BILI registry is not the complete 2018Q1-2020Q4 chain")

    rows: list[dict] = []
    recovered: list[dict] = []
    comparative_versions: list[dict] = []
    bindings: list[dict] = []
    annual_values: dict[int, dict[str, float]] = {}
    for entry in registry.sort_values("fiscal_end").itertuples(index=False):
        path = Path(entry.local_path)
        if download_missing:
            _download(entry.source_url, path)
        if not path.exists():
            raise FileNotFoundError(path)
        values = parse_bili_quarter(path, entry.fiscal_end)
        if entry.fiscal_end.month == 12:
            annual_values[entry.fiscal_end.year] = _parse_period(
                path, fiscal_end=entry.fiscal_end, period_phrase="Year Ended"
            )
        lag_days = int((entry.filed_date - entry.fiscal_end).days)
        if not 0 <= lag_days <= 150:
            raise ValueError(f"BILI filing is not timely: {entry.accession}")
        common = {
            "ticker": "BILI", "fiscal_end": entry.fiscal_end,
            "available_date": entry.filed_date, "taxonomy": "us-gaap",
            "form": entry.form, "accession": entry.accession, "unit": "CNY",
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
            "ticker": "BILI", "fiscal_end": entry.fiscal_end.strftime("%Y-%m-%d"),
            "available_date": entry.filed_date.strftime("%Y-%m-%d"),
            "availability_lag_days": lag_days, **values,
            "derivation": "direct_three_month_sec_filing_gaap_statement",
        })
        comparative_end = COMPARATIVE_ENDS.get(entry.fiscal_end)
        if comparative_end is not None:
            comparative_values = parse_bili_quarter(path, comparative_end)
            comparative_common = {
                **common,
                "fiscal_end": comparative_end,
                "available_date": entry.filed_date,
                "source": "explicit_sec_filed_prior_year_comparative_gaap_statement",
                "derivation_prior_accession": entry.accession,
            }
            rows.extend([
                {**comparative_common, "metric": "revenue",
                 "value": comparative_values["revenue"],
                 "concept": "RevenueFromContractWithCustomerExcludingAssessedTax"},
                {**comparative_common, "metric": "net_income",
                 "value": comparative_values["net_income"],
                 "concept": "NetIncomeLoss"},
            ])
            comparative = {
                "ticker": "BILI",
                "fiscal_end": comparative_end.strftime("%Y-%m-%d"),
                "available_date": entry.filed_date.strftime("%Y-%m-%d"),
                "availability_lag_days": int(
                    (entry.filed_date - comparative_end).days
                ),
                **comparative_values,
                "derivation": "direct_prior_year_comparative_sec_filing_statement",
                "source_accession": entry.accession,
            }
            recovered.append(comparative)
            comparative_versions.append(comparative)
        bindings.append({
            "accession": entry.accession,
            "fiscal_end": entry.fiscal_end.strftime("%Y-%m-%d"),
            "filed_date": entry.filed_date.strftime("%Y-%m-%d"),
            "path": str(path), "sha256": _sha256(path),
            "source_url": entry.source_url,
            "comparative_fiscal_end": (
                comparative_end.strftime("%Y-%m-%d")
                if comparative_end is not None else None
            ),
        })

        if entry.fiscal_end == pd.Timestamp("2018-12-31"):
            annual_values[2017] = _parse_period(
                path,
                fiscal_end=pd.Timestamp("2017-12-31"),
                period_phrase="Year Ended",
            )

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
                f"BILI {year} quarters do not reconcile annual: "
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
    if longest != 16:
        raise RuntimeError(f"BILI quarter chain is not continuous: {longest}/16")
    output_dir.mkdir(parents=True, exist_ok=True)
    quarters_path = output_dir / "strict_quarterly_facts.csv"
    quarters.to_csv(quarters_path, index=False)
    report = {
        "schema_version": 1, "research_only": True,
        "point_in_time_proven": True, "promotion_eligible": False,
        "release_status": "BLOCKED", "ticker": "BILI", "cik": 1723690,
        "currency": "CNY", "accounting_standard": "US GAAP", "gaap_only": True,
        "issuer_identity": {
            "sec_name": "Bilibili Inc.", "ticker": "BILI",
            "continuous_2018_2020": True, "former_sec_names": [],
        },
        "quarter_count": 16, "quarter_fact_version_count": len(recovered),
        "restatement_version_count": 0,
        "prior_year_comparative_version_count": len(comparative_versions),
        "longest_continuous_timely_paired_quarters": longest,
        "recovered_quarters": recovered, "restatement_versions": [],
        "prior_year_comparative_versions": comparative_versions,
        "annual_cross_checks": annual_cross_checks,
        "registry": {"path": str(registry_path), "sha256": _sha256(registry_path)},
        "filings": bindings,
        "outputs": {"quarters": {
            "path": str(quarters_path), "sha256": _sha256(quarters_path),
        }},
        "guardrail": (
            "Only direct SEC-filed three-month US-GAAP Total net revenues and "
            "Net loss rows in RMB thousands are accepted. USD translations, "
            "adjusted metrics, and annual-minus-YTD derivations are excluded. "
            "Prior-year comparison columns retain the later SEC filing date. "
            "Research-only; no formal "
            "replacement or trading authorization."
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
