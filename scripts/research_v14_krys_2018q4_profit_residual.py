#!/usr/bin/env python3
"""Recover KRYS 2018Q4 profit from its first-filed 2018 10-K."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
from io import BytesIO
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd

from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.io.fundamentals_update import OUTPUT_COLUMNS, parse_companyfacts_quarterly


REGISTRY = Path("stocks_list_dir/nasdaq/krys_2018_annual_report.csv")
CACHE = Path("output/research_only/v14/companyfacts_cache/CIK0001711279.json.gz")
OUTPUT_DIR = Path("output/research_only/v14/krys_2018q4_profit_residual")
HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
EXPECTED_QUARTERS = {
    "2018-03-31": -2_150_000.0,
    "2018-06-30": -2_276_000.0,
    "2018-09-30": -2_755_000.0,
}
EXPECTED_DATES = {
    "2018-03-31": "2018-05-07",
    "2018-06-30": "2018-08-06",
    "2018-09-30": "2018-11-05",
}
EXPECTED_ANNUAL = -10_889_000.0
EXPECTED_Q4 = -3_708_000.0


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normal(value: object) -> str:
    return " ".join(str(value).replace("\xa0", " ").split()).casefold()


def _accounting_value(value: object) -> float:
    text = str(value).replace(",", "").replace("$", "").strip()
    match = re.search(r"\d+(?:\.\d+)?", text)
    if match is None:
        raise ValueError(f"no accounting value in {value!r}")
    value_number = float(match.group())
    return -value_number if "(" in text else value_number


def extract_annual_net_loss(path: Path) -> float:
    candidates = []
    for table in pd.read_html(BytesIO(path.read_bytes())):
        if len(table) < 10 or len(table.columns) < 8:
            continue
        labels = table.iloc[:, 0].map(_normal)
        if labels.eq("net loss").sum() != 1:
            continue
        headers = " ".join(
            _normal(value) for value in table.head(4).to_numpy().ravel()
        )
        if "year ended" not in headers or "december 31" not in headers:
            continue
        row = table.loc[labels.eq("net loss")].iloc[0]
        year_headers = " ".join(
            _normal(value) for value in table.head(4).to_numpy().ravel()
        )
        if "2018" not in year_headers:
            continue
        values = []
        for column in table.columns:
            column_headers = {
                _normal(table.iloc[index][column])
                for index in range(min(4, len(table)))
            }
            if not any(value.startswith("2018") for value in column_headers):
                continue
            try:
                values.append(_accounting_value(row[column]) * 1_000.0)
            except ValueError:
                continue
        unique = sorted(set(values))
        if len(unique) == 1:
            candidates.append(unique[0])
    if not candidates or any(value != EXPECTED_ANNUAL for value in candidates):
        raise ValueError(f"KRYS 2018 annual net loss changed: {candidates}")
    return EXPECTED_ANNUAL


def run(
    registry_path: Path = REGISTRY,
    cache_path: Path = CACHE,
    output_dir: Path = OUTPUT_DIR,
) -> dict:
    registry = pd.read_csv(registry_path, dtype={"cik": str, "accession": str})
    if (
        len(registry) != 1
        or registry.iloc[0]["ticker"] != "KRYS"
        or registry.iloc[0]["cik"] != "1711279"
    ):
        raise ValueError("KRYS registry must bind exactly CIK 1711279")
    row = registry.iloc[0]
    if (
        row["form"] != "10-K"
        or row["accession"].replace("-", "") not in row["source_url"]
    ):
        raise ValueError("KRYS registry must bind the first-filed 2018 10-K")
    source = Path(row["local_path"])
    source.parent.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        with urlopen(
            Request(row["source_url"], headers=HEADERS), timeout=120
        ) as response:
            source.write_bytes(response.read())
    annual = extract_annual_net_loss(source)

    with gzip.open(cache_path, "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    if int(envelope["payload"]["cik"]) != 1711279:
        raise ValueError("KRYS cache has the wrong CIK")
    quarterly = parse_companyfacts_quarterly(
        "KRYS", envelope["payload"], envelope["fetched_at"]
    )
    known = quarterly.loc[
        quarterly["metric"].eq("net_income")
        & quarterly["fiscal_end"].isin(pd.to_datetime(list(EXPECTED_QUARTERS)))
        & quarterly["available_date"].le(pd.Timestamp(row["available_date"]))
    ].sort_values("available_date").drop_duplicates(
        ["fiscal_end", "metric"], keep="last"
    ).sort_values("fiscal_end")
    values = {
        str(item.fiscal_end.date()): float(item.value)
        for item in known.itertuples(index=False)
    }
    dates = {
        str(item.fiscal_end.date()): str(item.available_date.date())
        for item in known.itertuples(index=False)
    }
    if values != EXPECTED_QUARTERS or dates != EXPECTED_DATES:
        raise RuntimeError(
            f"KRYS quarter inputs changed: values={values}, dates={dates}"
        )
    q4 = float(annual - known["value"].sum())
    if q4 != EXPECTED_Q4:
        raise RuntimeError(f"KRYS 2018Q4 residual changed: {q4}")
    fetched_at = pd.Timestamp(envelope["fetched_at"]).tz_localize(None).normalize()
    fact = pd.DataFrame([{
        "ticker": "KRYS", "fiscal_end": "2018-12-31",
        "available_date": row["available_date"], "metric": "net_income",
        "value": q4, "taxonomy": "KRYS_US_GAAP_10K_COMPANYFACTS",
        "concept": "derived_first_filed_fy_minus_known_q1_q2_q3:NetIncomeLoss",
        "form": "10-K_RESIDUAL", "accession": row["accession"],
        "fetched_at": fetched_at,
    }], columns=OUTPUT_COLUMNS)
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    fact.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1, "research_only": True, "ticker": "KRYS",
        "point_in_time_proven": True, "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED", "promotion_eligible": False,
        "accepted_quarter_count": 1, "fact_count": 1,
        "quarter_inputs": values, "quarter_input_available_dates": dates,
        "annual_input": annual, "q4_residual": q4,
        "sources": [
            {**row.to_dict(), "sha256": _sha256(source)},
            {"path": str(cache_path), "sha256": _sha256(cache_path)},
        ],
        "outputs": {"strict_quarterly_facts": {
            "path": str(facts_path), "sha256": _sha256(facts_path)
        }},
        "guardrail": (
            "The original 2019-03-12 10-K directly supplies FY2018 net loss. "
            "2018Q1-Q3 were already public; Q4 is the exact residual and is "
            "available only on the 10-K filing date."
        ),
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    report["manifest"] = str(manifest_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry-path", type=Path, default=REGISTRY)
    parser.add_argument("--cache-path", type=Path, default=CACHE)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--base-dir", type=Path)
    parser.add_argument("--candidate-output-dir", type=Path)
    args = parser.parse_args()
    report = run(args.registry_path, args.cache_path, args.output_dir)
    if (args.base_dir is None) != (args.candidate_output_dir is None):
        parser.error("--base-dir and --candidate-output-dir must be used together")
    if args.base_dir is not None:
        report["candidate"] = integrate_candidate(
            base_dir=args.base_dir,
            supplement_dir=args.output_dir,
            output_dir=args.candidate_output_dir,
        )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
