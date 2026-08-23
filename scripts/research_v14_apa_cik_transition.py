#!/usr/bin/env python3
"""Recover APA across the Apache-to-APA holding-company CIK transition."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from io import BytesIO
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd

from src.io.fundamentals_update import (
    OUTPUT_COLUMNS,
    parse_companyfacts_quarterly,
)


REGISTRY = Path("stocks_list_dir/nasdaq/apa_cik_transition_reports.csv")
CURRENT_CACHE = Path(
    "output/research_only/v14/companyfacts_cache/CIK0001841666.json.gz"
)
OUTPUT_DIR = Path("output/research_only/v14/apa_cik_transition_quarters")
HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _download(url: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return
    with urlopen(Request(url, headers=HEADERS), timeout=120) as response:
        path.write_bytes(response.read())


def _accounting_value(value: object) -> float:
    text = str(value).replace(",", "").replace("$", "").strip()
    match = re.fullmatch(r"\(?\s*(-?\d+(?:\.\d+)?)\s*\)?", text)
    if match is None:
        raise ValueError(f"not one accounting value: {value!r}")
    result = float(match.group(1))
    return -abs(result) if "(" in text else result


def extract_total_revenue(path: Path) -> float:
    """Extract the current single-quarter total revenue in USD."""
    candidates = []
    for table in pd.read_html(BytesIO(path.read_bytes())):
        if len(table) < 8 or len(table.columns) < 9:
            continue
        labels = (
            table.iloc[:, 0].astype(str)
            .str.replace(r"\s+", " ", regex=True).str.strip().str.casefold()
        )
        if labels.eq("total revenues").sum() != 1:
            continue
        if not labels.str.contains(
            "net income.*attributable to common stock", regex=True
        ).any():
            continue
        quarter_header = table.iloc[1].astype(str).str.casefold()
        year_header = table.iloc[2].astype(str).str.strip()
        current_columns = [
            column for column in table.columns
            if "quarter ended" in str(quarter_header[column])
            and str(year_header[column]) == "2021"
        ]
        revenue_row = table.loc[labels.eq("total revenues")].iloc[0]
        values = []
        for column in current_columns:
            try:
                values.append(_accounting_value(revenue_row[column]))
            except ValueError:
                continue
        unique = sorted(set(values))
        if len(unique) == 1:
            candidates.append(unique[0] * 1_000_000.0)
    if len(candidates) != 1:
        raise ValueError(
            f"expected one APA current-quarter revenue, found {candidates}"
        )
    return candidates[0]


def _load_current_payload(path: Path) -> tuple[dict, object]:
    import gzip

    with gzip.open(path, "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    if int(envelope["payload"]["cik"]) != 1841666:
        raise ValueError("current APA cache is not CIK 1841666")
    return envelope["payload"], envelope["fetched_at"]


def run(
    registry_path: Path = REGISTRY,
    current_cache: Path = CURRENT_CACHE,
    output_dir: Path = OUTPUT_DIR,
) -> dict:
    registry = pd.read_csv(
        registry_path, dtype={"cik": str, "accession": str},
        keep_default_na=False,
    )
    if set(registry["cik"]) != {"6769", "1841666"}:
        raise ValueError("APA registry must bind legacy and successor CIKs")
    if registry["accession"].duplicated().any():
        raise ValueError("APA registry accessions must be unique")
    for row in registry.itertuples(index=False):
        _download(row.source_url, Path(row.local_path))

    legacy_row = registry.loc[registry["form"].eq("COMPANY_FACTS")].iloc[0]
    legacy_payload = json.loads(Path(legacy_row["local_path"]).read_text())
    if (
        int(legacy_payload.get("cik", -1)) != 6769
        or "APACHE" not in str(legacy_payload.get("entityName", "")).upper()
    ):
        raise ValueError("legacy Company Facts is not Apache CIK 6769")
    legacy = parse_companyfacts_quarterly(
        "APA", legacy_payload, legacy_row["available_date"]
    )
    legacy = legacy.loc[
        legacy["metric"].isin({"revenue", "net_income"})
        & pd.to_datetime(legacy["fiscal_end"]).le("2020-12-31")
    ].copy()
    legacy["concept"] = (
        "research_predecessor_cik_6769:" + legacy["concept"].astype(str)
    )

    current_payload, fetched_at = _load_current_payload(current_cache)
    current = parse_companyfacts_quarterly("APA", current_payload, fetched_at)
    current_income = current.loc[
        current["metric"].eq("net_income")
        & pd.to_datetime(current["fiscal_end"]).between(
            "2021-03-31", "2021-09-30"
        )
    ].copy()
    first_income = (
        current_income.sort_values("available_date")
        .drop_duplicates("fiscal_end", keep="first")
    )
    filing_rows = registry.loc[registry["form"].eq("10-Q")]
    revenues = []
    for row in filing_rows.itertuples(index=False):
        revenues.append({
            "ticker": "APA",
            "fiscal_end": row.fiscal_end,
            "available_date": row.available_date,
            "metric": "revenue",
            "value": extract_total_revenue(Path(row.local_path)),
            "taxonomy": "APA_2021_INLINE_XBRL_STATEMENT",
            "concept": "sec_10q_total_revenues",
            "form": "10-Q",
            "accession": row.accession,
            "fetched_at": pd.Timestamp(fetched_at).tz_localize(None).normalize(),
        })
    successor_revenue = pd.DataFrame(revenues, columns=OUTPUT_COLUMNS)
    successor_revenue["fiscal_end"] = pd.to_datetime(
        successor_revenue["fiscal_end"]
    )
    successor_revenue["available_date"] = pd.to_datetime(
        successor_revenue["available_date"]
    )
    paired = successor_revenue.merge(
        first_income[["fiscal_end", "available_date", "value"]],
        on=["fiscal_end", "available_date"], validate="one_to_one",
        suffixes=("_revenue", "_net_income"),
    )
    if len(paired) != 3 or (paired["value_revenue"] <= 0).any():
        raise RuntimeError("APA successor quarters are not three positive pairs")

    combined = pd.concat([legacy, successor_revenue, first_income])
    combined = combined[OUTPUT_COLUMNS].sort_values(
        ["ticker", "available_date", "fiscal_end", "metric"]
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    combined.to_csv(facts_path, index=False)
    sources = [
        {**row._asdict(), "sha256": _sha256(Path(row.local_path))}
        for row in registry.itertuples(index=False)
    ]
    report = {
        "schema_version": 1,
        "research_only": True,
        "ticker": "APA",
        "predecessor_cik": 6769,
        "successor_cik": 1841666,
        "point_in_time_proven": True,
        "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "legacy_output_rows": int(len(legacy)),
        "successor_revenue_rows": int(len(successor_revenue)),
        "successor_paired_quarters": int(len(paired)),
        "successor_revenues": successor_revenue[
            ["fiscal_end", "available_date", "value", "accession"]
        ].to_dict("records"),
        "filing_sources": sources,
        "outputs": {"strict_quarterly_facts": {
            "path": str(facts_path), "sha256": _sha256(facts_path)
        }},
        "guardrail": (
            "CIK 6769 is used only through 2020-12-31. CIK 1841666's zero "
            "generic revenue placeholders are rejected; 2021 total revenues "
            "come from the contemporaneous consolidated 10-Q statements, "
            "while net income remains from same-date Company Facts."
        ),
    }
    manifest = output_dir / "manifest.json"
    manifest.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n"
    )
    report["manifest"] = str(manifest)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry-path", type=Path, default=REGISTRY)
    parser.add_argument("--current-cache", type=Path, default=CURRENT_CACHE)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    print(json.dumps(
        run(args.registry_path, args.current_cache, args.output_dir),
        indent=2, sort_keys=True, default=str,
    ))


if __name__ == "__main__":
    main()
