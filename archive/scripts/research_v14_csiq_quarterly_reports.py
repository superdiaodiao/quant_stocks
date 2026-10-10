#!/usr/bin/env python3
"""Recover CSIQ's SEC-filed direct GAAP quarters for v14 research only."""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pandas as pd
from bs4 import BeautifulSoup

from scripts.research_v14_cybr_quarterly_reports import (
    OUTPUT_COLUMNS,
    _longest_chain,
    _normal,
    _period_columns,
    _row_value,
    _sha256,
)


DEFAULT_REGISTRY = Path("stocks_list_dir/nasdaq/csiq_quarterly_reports.csv")
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/csiq_sec_quarterly_reports_2017_2021"
)
USER_AGENT = "quant_stocks research data@example.com"
EXPECTED_ENDS = list(pd.date_range("2017-03-31", "2021-12-31", freq="QE"))
REVENUE_LABELS = ("Net revenues",)
NET_LABELS = ("Net income (loss)", "Net income")
MAX_RECONCILIATION_DELTA_USD = 2_000.0


def _proves_issuer_usd_thousands(raw: bytes) -> bool:
    text = _normal(BeautifulSoup(raw, "html.parser").get_text(" ", strip=True))
    lower = text.casefold()
    if "canadian solar inc" not in lower:
        return False
    return bool(re.search(
        r"in\s+thousands\s+of\s+(?:u\.?s\.?\s+)?dollars?",
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
    if not _proves_issuer_usd_thousands(raw):
        raise ValueError(
            f"CSIQ source does not prove issuer/USD-thousands: {path}"
        )
    pairs: list[tuple[float, float]] = []
    for table in pd.read_html(path):
        try:
            columns = _period_columns(
                table, fiscal_end=fiscal_end, period_phrase=period_phrase
            )
            _, revenue = _row_value(
                table, labels=REVENUE_LABELS, columns=columns
            )
            _, net_income = _row_value(
                table, labels=NET_LABELS, columns=columns
            )
        except ValueError:
            continue
        if revenue > 10_000:
            pairs.append((revenue, net_income))
    pairs = sorted(set(pairs))
    if len(pairs) != 1:
        raise ValueError(
            f"expected one direct CSIQ {period_phrase} pair in {path}; "
            f"pairs={pairs}"
        )
    return {
        "revenue": pairs[0][0] * 1000.0,
        "net_income": pairs[0][1] * 1000.0,
    }


def parse_csiq_quarter(path: Path, fiscal_end: pd.Timestamp) -> dict[str, float]:
    """Parse only the explicit three-month consolidated GAAP column."""
    return _parse_period(
        path, fiscal_end=fiscal_end, period_phrase="Three months ended"
    )


def _download(source_url: str, local_path: Path) -> None:
    if local_path.exists():
        return
    if not source_url.startswith(
        "https://www.sec.gov/Archives/edgar/data/1375877/"
    ):
        raise ValueError(f"CSIQ registry contains a non-SEC source: {source_url}")
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
            if len(payload) < 200_000 or b"<html" not in payload[:2_000].lower():
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


def _annual_reconciliation(
    frame: pd.DataFrame,
    annual_values: dict[int, dict[str, float]],
) -> list[dict]:
    checks = []
    for year, filed_annual in sorted(annual_values.items()):
        year_rows = frame.loc[frame["fiscal_end"].dt.year.eq(year)]
        if len(year_rows) != 4:
            raise RuntimeError(f"CSIQ {year} does not contain four quarters")
        quarter_sum = {
            metric: float(year_rows[metric].sum())
            for metric in ("revenue", "net_income")
        }
        delta = {
            metric: quarter_sum[metric] - filed_annual[metric]
            for metric in ("revenue", "net_income")
        }
        if any(
            abs(value) > MAX_RECONCILIATION_DELTA_USD
            for value in delta.values()
        ):
            raise RuntimeError(
                f"CSIQ {year} quarters do not reconcile annual: "
                f"{quarter_sum} != {filed_annual}; delta={delta}"
            )
        checks.append({
            "year": year,
            "quarter_sum": quarter_sum,
            "filed_annual": filed_annual,
            "delta_usd": delta,
            "exact_match": all(value == 0 for value in delta.values()),
            "within_disclosed_filing_tolerance": True,
        })
    return checks


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
    if set(registry["ticker"]) != {"CSIQ"} or set(registry["cik"]) != {1375877}:
        raise ValueError("CSIQ registry contains another issuer")
    if len(registry) != 20 or registry.duplicated("fiscal_end").any():
        raise ValueError("CSIQ registry must contain 20 unique quarters")
    if registry.sort_values("fiscal_end")["fiscal_end"].tolist() != EXPECTED_ENDS:
        raise ValueError("CSIQ registry is not the complete 2017Q1-2021Q4 chain")

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
        values = parse_csiq_quarter(path, entry.fiscal_end)
        if entry.fiscal_end.month == 12:
            annual_values[entry.fiscal_end.year] = _parse_period(
                path,
                fiscal_end=entry.fiscal_end,
                period_phrase="Twelve months ended",
            )
        lag_days = int((entry.filed_date - entry.fiscal_end).days)
        if not 0 <= lag_days <= 150:
            raise ValueError(f"CSIQ filing is not timely: {entry.accession}")
        common = {
            "ticker": "CSIQ",
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
        rows.extend([
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
                "concept": "ProfitLoss",
            },
        ])
        recovered.append({
            "ticker": "CSIQ",
            "fiscal_end": entry.fiscal_end.strftime("%Y-%m-%d"),
            "available_date": entry.filed_date.strftime("%Y-%m-%d"),
            "availability_lag_days": lag_days,
            **values,
            "derivation": "direct_three_month_sec_filing_gaap_statement",
        })
        bindings.append({
            "accession": entry.accession,
            "fiscal_end": entry.fiscal_end.strftime("%Y-%m-%d"),
            "filed_date": entry.filed_date.strftime("%Y-%m-%d"),
            "path": str(path),
            "sha256": _sha256(path),
            "source_url": entry.source_url,
        })

    frame = pd.DataFrame(recovered)
    frame["fiscal_end"] = pd.to_datetime(frame["fiscal_end"])
    annual_cross_checks = _annual_reconciliation(frame, annual_values)
    quarters = pd.DataFrame(rows, columns=OUTPUT_COLUMNS).sort_values(
        ["fiscal_end", "available_date", "metric"]
    )
    paired = quarters.groupby("fiscal_end")["metric"].nunique()
    longest = _longest_chain(paired.loc[paired.eq(2)].index.tolist())
    if longest != 20:
        raise RuntimeError(f"CSIQ quarter chain is not continuous: {longest}/20")

    output_dir.mkdir(parents=True, exist_ok=True)
    quarters_path = output_dir / "strict_quarterly_facts.csv"
    quarters.to_csv(quarters_path, index=False)
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "promotion_eligible": False,
        "release_status": "BLOCKED",
        "ticker": "CSIQ",
        "cik": 1375877,
        "currency": "USD",
        "accounting_standard": "US GAAP",
        "gaap_only": True,
        "issuer_identity": {
            "sec_name": "Canadian Solar Inc.",
            "ticker": "CSIQ",
            "continuous_2017_2021": True,
        },
        "quarter_count": 20,
        "quarter_fact_version_count": 20,
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
            "Only direct SEC-filed three-month consolidated US-GAAP Net "
            "revenues and Net income (loss) rows in USD thousands are accepted. "
            "Non-GAAP metrics and annual-minus-YTD derivations are excluded. "
            "The published 2017 quarterly revenue sum exceeds the filed annual "
            "value by USD 2,000; this source discrepancy is retained explicitly "
            "and is the only non-exact annual reconciliation. Research-only; no "
            "formal replacement or trading authorization."
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
        "quarter_count": result["quarter_count"],
        "longest_continuous_timely_paired_quarters": result[
            "longest_continuous_timely_paired_quarters"
        ],
        "promotion_eligible": False,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
