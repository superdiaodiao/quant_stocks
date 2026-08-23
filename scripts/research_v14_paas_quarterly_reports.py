#!/usr/bin/env python3
"""Recover PAAS's SEC-filed IFRS quarters for v14 research only."""

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


DEFAULT_REGISTRY = Path("stocks_list_dir/nasdaq/paas_quarterly_reports.csv")
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/paas_sec_quarterly_reports_2017_2021"
)
USER_AGENT = "quant_stocks research data@example.com"
OUTPUT_COLUMNS = [
    "ticker", "fiscal_end", "available_date", "metric", "value",
    "taxonomy", "concept", "form", "accession", "unit", "source",
    "source_archive", "source_archive_sha256", "derivation_prior_accession",
]
COMPARATIVE_RESTATEMENTS = {
    pd.Timestamp("2020-03-31"): pd.Timestamp("2019-03-31"),
    pd.Timestamp("2020-06-30"): pd.Timestamp("2019-06-30"),
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
    month_day = fiscal_end.strftime("%B %-d")
    selected = []
    for column in table.columns:
        header = " ".join(
            _normal(value) for value in table.head(8)[column] if _normal(value)
        )
        normalized = header.replace(",", "")
        if period_phrase.lower() not in normalized.lower():
            continue
        if month_day.lower() not in normalized.lower():
            continue
        if str(fiscal_end.year) not in normalized:
            continue
        selected.append(column)
    if not selected:
        raise ValueError(
            f"PAAS table has no {period_phrase!r} {fiscal_end.date()} column"
        )
    return selected


def _row_label(row: pd.Series) -> str:
    values = [_normal(value) for value in row.iloc[: min(4, len(row))]]
    nonempty = [value for value in values if value]
    return nonempty[0] if nonempty else ""


def _metric_values(
    table: pd.DataFrame,
    columns: list[object],
    *,
    metric: str,
) -> list[float]:
    values = []
    for _, row in table.iterrows():
        label = _row_label(row).lower()
        if metric == "revenue":
            matches = label == "revenue" or label.startswith("revenue (note")
        else:
            matches = (
                label in {"net earnings", "net loss"}
                or (
                    label.startswith("net ")
                    and "for the period" in label
                    and ("earnings" in label or "loss" in label)
                )
            )
            matches = matches and "adjusted" not in label
        if not matches:
            continue
        for column in columns:
            parsed = _parse_accounting_number(row[column])
            if parsed is not None:
                values.append(parsed)
    return values


def _metric_pair_from_tables(
    tables: list[pd.DataFrame],
    *,
    fiscal_end: pd.Timestamp,
    period_phrase: str,
) -> tuple[float, float]:
    pairs: list[tuple[float, float]] = []
    all_revenues: set[float] = set()
    all_net_incomes: set[float] = set()
    for table in tables:
        try:
            columns = _period_columns(
                table, fiscal_end=fiscal_end, period_phrase=period_phrase
            )
        except ValueError:
            continue
        revenues = sorted(set(_metric_values(table, columns, metric="revenue")))
        net_incomes = sorted(
            set(_metric_values(table, columns, metric="net_income"))
        )
        all_revenues.update(revenues)
        all_net_incomes.update(net_incomes)
        # Segment-note tables can contain many rows labelled Revenue.  Accept
        # only a single table that proves both consolidated metrics together.
        if len(revenues) == 1 and len(net_incomes) == 1:
            pairs.append((revenues[0], net_incomes[0]))
    agreeing_pairs = sorted(set(pairs))
    if len(agreeing_pairs) == 1:
        return agreeing_pairs[0]
    # Some SEC inline-XBRL filings split the consolidated income statement
    # across adjacent HTML tables.  Permit that layout only when each metric
    # remains globally unique across every matching-period table; any segment
    # or note-table ambiguity still fails closed.
    if len(all_revenues) == 1 and len(all_net_incomes) == 1:
        return (next(iter(all_revenues)), next(iter(all_net_incomes)))
    raise ValueError(
        f"expected one agreeing PAAS {period_phrase} pair; pairs={agreeing_pairs}; "
        f"revenues={sorted(all_revenues)}; net_incomes={sorted(all_net_incomes)}"
    )


def _parse_period(
    path: Path,
    *,
    fiscal_end: pd.Timestamp,
    period_phrase: str,
) -> dict[str, float]:
    raw = path.read_bytes()
    text = _normal(BeautifulSoup(raw, "html.parser").get_text(" ", strip=True))
    lower = text.lower()
    if "pan american silver" not in lower:
        raise ValueError(f"PAAS source does not identify the issuer: {path}")
    if "thousands" not in lower or not (
        "u.s. dollar" in lower or "united states dollar" in lower
    ):
        raise ValueError(f"PAAS source does not prove USD-thousands units: {path}")

    try:
        revenue, net_income = _metric_pair_from_tables(
            pd.read_html(path),
            fiscal_end=fiscal_end,
            period_phrase=period_phrase,
        )
    except ValueError as error:
        raise ValueError(f"{error} in {path}") from error
    return {
        "revenue": revenue * 1000.0,
        "net_income": net_income * 1000.0,
    }


def parse_paas_quarter(path: Path, fiscal_end: pd.Timestamp) -> dict[str, float]:
    """Parse explicit three-month IFRS revenue and period net earnings."""
    return _parse_period(
        path, fiscal_end=fiscal_end, period_phrase="Three months ended"
    )


def _download(source_url: str, local_path: Path) -> None:
    if local_path.exists():
        return
    if not source_url.startswith(
        "https://www.sec.gov/Archives/edgar/data/771992/"
    ):
        raise ValueError(f"PAAS registry contains a non-SEC source: {source_url}")
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
    if set(registry["ticker"]) != {"PAAS"} or set(registry["cik"]) != {771992}:
        raise ValueError("PAAS registry contains another issuer")
    if len(registry) != 20 or registry.duplicated("fiscal_end").any():
        raise ValueError("PAAS registry must contain exactly 20 unique quarters")
    expected = list(pd.date_range("2017-03-31", "2021-12-31", freq="QE"))
    if registry.sort_values("fiscal_end")["fiscal_end"].tolist() != expected:
        raise ValueError("PAAS registry is not the complete 2017Q1-2021Q4 chain")

    rows: list[dict] = []
    recovered: list[dict] = []
    restatement_versions: list[dict] = []
    bindings: list[dict] = []
    annual_values: dict[int, dict[str, float]] = {}
    for entry in registry.sort_values("fiscal_end").itertuples(index=False):
        path = Path(entry.local_path)
        if download_missing:
            _download(entry.source_url, path)
        if not path.exists():
            raise FileNotFoundError(path)
        values = parse_paas_quarter(path, entry.fiscal_end)
        if entry.fiscal_end.month == 12:
            annual_values[entry.fiscal_end.year] = _parse_period(
                path, fiscal_end=entry.fiscal_end, period_phrase="Year ended"
            )
        lag_days = int((entry.filed_date - entry.fiscal_end).days)
        if not 0 <= lag_days <= 150:
            raise ValueError(f"PAAS filing is not timely: {entry.accession}")
        source = (
            "explicit_sec_filed_interim_three_month_ifrs_statement"
            if entry.source_kind == "interim_financial_statements"
            else "explicit_sec_filed_q4_three_month_ifrs_results_table"
        )
        common = {
            "ticker": "PAAS", "fiscal_end": entry.fiscal_end,
            "available_date": entry.filed_date, "taxonomy": "ifrs-full",
            "form": entry.form, "accession": entry.accession, "unit": "USD",
            "source": source, "source_archive": path.name,
            "source_archive_sha256": _sha256(path),
            "derivation_prior_accession": "",
        }
        rows.extend([
            {**common, "metric": "revenue", "value": values["revenue"],
             "concept": "Revenue"},
            {**common, "metric": "net_income", "value": values["net_income"],
             "concept": "ProfitLoss"},
        ])
        recovered.append({
            "ticker": "PAAS", "fiscal_end": entry.fiscal_end.strftime("%Y-%m-%d"),
            "available_date": entry.filed_date.strftime("%Y-%m-%d"),
            "availability_lag_days": lag_days, **values,
            "derivation": "direct_three_month_sec_filing_ifrs_statement",
        })
        comparative_end = COMPARATIVE_RESTATEMENTS.get(entry.fiscal_end)
        if comparative_end is not None:
            comparative_values = parse_paas_quarter(path, comparative_end)
            comparative_common = {
                **common,
                "fiscal_end": comparative_end,
                "available_date": entry.filed_date,
                "source": (
                    "explicit_sec_filed_comparative_three_month_ifrs_statement"
                ),
            }
            rows.extend([
                {
                    **comparative_common, "metric": "revenue",
                    "value": comparative_values["revenue"], "concept": "Revenue",
                },
                {
                    **comparative_common, "metric": "net_income",
                    "value": comparative_values["net_income"], "concept": "ProfitLoss",
                },
            ])
            restatement = {
                "ticker": "PAAS",
                "fiscal_end": comparative_end.strftime("%Y-%m-%d"),
                "available_date": entry.filed_date.strftime("%Y-%m-%d"),
                "availability_lag_days": int(
                    (entry.filed_date - comparative_end).days
                ),
                **comparative_values,
                "derivation": (
                    "direct_comparative_three_month_sec_filing_ifrs_statement"
                ),
                "restates_accession": entry.accession,
            }
            recovered.append(restatement)
            restatement_versions.append(restatement)
        bindings.append({
            "accession": entry.accession,
            "fiscal_end": entry.fiscal_end.strftime("%Y-%m-%d"),
            "filed_date": entry.filed_date.strftime("%Y-%m-%d"),
            "source_kind": entry.source_kind, "path": str(path),
            "sha256": _sha256(path), "source_url": entry.source_url,
            "comparative_fiscal_end": (
                comparative_end.strftime("%Y-%m-%d")
                if comparative_end is not None else None
            ),
        })

    frame = pd.DataFrame(recovered)
    frame["fiscal_end"] = pd.to_datetime(frame["fiscal_end"])
    frame["available_date"] = pd.to_datetime(frame["available_date"])
    annual_cross_checks = []
    for year, annual in sorted(annual_values.items()):
        year_rows = (
            frame.loc[frame["fiscal_end"].dt.year.eq(year)]
            .sort_values("available_date")
            .drop_duplicates("fiscal_end", keep="last")
        )
        quarter_sum = {
            metric: float(year_rows[metric].sum())
            for metric in ("revenue", "net_income")
        }
        if len(year_rows) != 4 or quarter_sum != annual:
            raise RuntimeError(
                f"PAAS {year} quarters do not reconcile the annual: "
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
    if longest != 20:
        raise RuntimeError(f"PAAS quarter chain is not continuous: {longest}/20")

    output_dir.mkdir(parents=True, exist_ok=True)
    quarters_path = output_dir / "strict_quarterly_facts.csv"
    quarters.to_csv(quarters_path, index=False)
    report = {
        "schema_version": 1, "research_only": True,
        "point_in_time_proven": True, "promotion_eligible": False,
        "release_status": "BLOCKED", "ticker": "PAAS", "cik": 771992,
        "currency": "USD", "accounting_standard": "IFRS", "gaap_only": True,
        "issuer_identity": {
            "sec_name": "PAN AMERICAN SILVER CORP", "ticker": "PAAS",
            "continuous_2017_2021": True, "former_sec_names": [],
        },
        "quarter_count": 20, "quarter_fact_version_count": len(recovered),
        "restatement_version_count": len(restatement_versions),
        "longest_continuous_timely_paired_quarters": longest,
        "recovered_quarters": recovered,
        "restatement_versions": restatement_versions,
        "annual_cross_checks": annual_cross_checks,
        "registry": {"path": str(registry_path), "sha256": _sha256(registry_path)},
        "filings": bindings,
        "outputs": {"quarters": {
            "path": str(quarters_path), "sha256": _sha256(quarters_path)
        }},
        "guardrail": (
            "Only explicit SEC-filed three-month IFRS revenue and net earnings "
            "tables in thousands of USD are accepted. Annual-minus-nine-month "
            "derivations and adjusted earnings are excluded. Later comparative "
            "quarter restatements retain their later filing availability. "
            "Research-only; no formal replacement or "
            "trading authorization."
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
        registry_path=args.registry, output_dir=args.output_dir,
        download_missing=not args.offline,
    )
    print(json.dumps({
        "manifest": result["manifest"], "quarter_count": result["quarter_count"],
        "quarter_fact_version_count": result["quarter_fact_version_count"],
        "longest_continuous_timely_paired_quarters": result[
            "longest_continuous_timely_paired_quarters"
        ], "promotion_eligible": False,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
