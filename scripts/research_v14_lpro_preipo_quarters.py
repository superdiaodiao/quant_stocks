#!/usr/bin/env python3
"""Recover LPRO 2019 quarters from its S-1 and later timely comparatives."""

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

from src.io.fundamentals_update import OUTPUT_COLUMNS, parse_companyfacts_quarterly


REGISTRY = Path("stocks_list_dir/nasdaq/lpro_preipo_quarterly_reports.csv")
CACHE = Path("output/research_only/v14/companyfacts_cache/CIK0001806201.json.gz")
OUTPUT_DIR = Path("output/research_only/v14/lpro_preipo_quarters_2019")
HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
EXPECTED = {
    "annual": {"revenue": 92_847_000.0, "net_income": 62_544_000.0},
    "q1": {"revenue": 19_484_000.0, "net_income": 12_904_000.0},
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _accounting_value(value: object) -> float:
    text = str(value).replace(",", "").replace("$", "").strip()
    match = re.search(r"\d+(?:\.\d+)?", text)
    if match is None:
        raise ValueError(f"no accounting value in {value!r}")
    result = float(match.group())
    return -result if "(" in text else result


def extract_s1_values(path: Path) -> dict:
    candidates = []
    for table in pd.read_html(BytesIO(path.read_bytes())):
        if len(table) < 15:
            continue
        labels = (
            table.iloc[:, 0].astype(str)
            .str.replace(r"\s+", " ", regex=True).str.strip().str.casefold()
        )
        if labels.eq("total revenue (1)").sum() != 1:
            continue
        if labels.eq("net income").sum() != 1:
            continue
        headers = " ".join(
            str(value) for value in table.head(3).to_numpy().ravel()
        ).casefold()
        if "three months ended march" not in headers or "2019" not in headers:
            continue
        revenue = table.loc[labels.eq("total revenue (1)")].iloc[0]
        income = table.loc[labels.eq("net income")].iloc[0]
        quarter_headers = (
            table.iloc[1].astype(str)
            .str.replace(r"\s+", " ", regex=True).str.casefold()
        )
        year_headers = table.iloc[2].astype(str).str.strip()

        def one_value(row: pd.Series, phrase: str, year: str) -> float:
            values = []
            for column in table.columns:
                if phrase not in str(quarter_headers[column]):
                    continue
                if str(year_headers[column]) != year:
                    continue
                try:
                    values.append(_accounting_value(row[column]))
                except ValueError:
                    continue
            unique = sorted(set(values))
            if len(unique) != 1:
                raise ValueError(f"LPRO S-1 expected one {phrase} {year}: {unique}")
            return unique[0] * 1_000.0

        candidates.append({
            "annual": {
                "revenue": one_value(revenue, "years ended december", "2019"),
                "net_income": one_value(income, "years ended december", "2019"),
            },
            "q1": {
                "revenue": one_value(revenue, "three months ended march", "2019"),
                "net_income": one_value(income, "three months ended march", "2019"),
            },
        })
    if candidates != [EXPECTED]:
        raise ValueError(f"LPRO S-1 values differ from strict expectation: {candidates}")
    return candidates[0]


def run(
    registry_path: Path = REGISTRY,
    cache_path: Path = CACHE,
    output_dir: Path = OUTPUT_DIR,
) -> dict:
    registry = pd.read_csv(registry_path, dtype={"cik": str, "accession": str})
    if len(registry) != 1 or registry.iloc[0]["cik"] != "1806201":
        raise ValueError("LPRO registry must bind exactly CIK 1806201")
    row = registry.iloc[0]
    source = Path(row["local_path"])
    source.parent.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        with urlopen(
            Request(row["source_url"], headers=HEADERS), timeout=120
        ) as response:
            source.write_bytes(response.read())
    s1 = extract_s1_values(source)

    with gzip.open(cache_path, "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    if int(envelope["payload"]["cik"]) != 1806201:
        raise ValueError("LPRO cache has the wrong CIK")
    parsed = parse_companyfacts_quarterly(
        "LPRO", envelope["payload"], envelope["fetched_at"]
    )
    comparative = parsed.loc[
        parsed["fiscal_end"].isin(
            [pd.Timestamp("2019-06-30"), pd.Timestamp("2019-09-30")]
        )
        & parsed["metric"].isin({"revenue", "net_income"})
    ].sort_values("available_date").drop_duplicates(
        ["fiscal_end", "metric"], keep="first"
    )
    if len(comparative) != 4:
        raise RuntimeError("LPRO cache does not contain paired 2019 Q2-Q3 comparatives")
    first_available = comparative.pivot(
        index="fiscal_end", columns="metric", values="available_date"
    )
    if first_available.loc[pd.Timestamp("2019-06-30")].max() != pd.Timestamp("2020-08-14"):
        raise RuntimeError("LPRO Q2 comparison was not first available on 2020-08-14")
    if first_available.loc[pd.Timestamp("2019-09-30")].max() != pd.Timestamp("2020-11-13"):
        raise RuntimeError("LPRO Q3 comparison was not first available on 2020-11-13")

    q1 = []
    for metric, value in s1["q1"].items():
        q1.append({
            "ticker": "LPRO", "fiscal_end": "2019-03-31",
            "available_date": row["available_date"], "metric": metric,
            "value": value, "taxonomy": "LPRO_US_GAAP_S1",
            "concept": f"sec_s1_summary_of_operations_{metric}",
            "form": "S-1", "accession": row["accession"],
            "fetched_at": pd.Timestamp(envelope["fetched_at"]).tz_localize(None).normalize(),
        })
    q1_frame = pd.DataFrame(q1, columns=OUTPUT_COLUMNS)
    known = pd.concat([q1_frame, comparative[OUTPUT_COLUMNS]])
    sums = known.groupby("metric")["value"].sum().to_dict()
    q4 = []
    for metric, annual in s1["annual"].items():
        q4.append({
            "ticker": "LPRO", "fiscal_end": "2019-12-31",
            "available_date": "2020-11-13", "metric": metric,
            "value": annual - sums[metric], "taxonomy": "LPRO_US_GAAP_S1",
            "concept": f"derived_s1_fy_minus_q1_q2_q3:{metric}",
            "form": "S-1+10-Q", "accession": (
                row["accession"] + "+0001806201-20-000007"
            ),
            "fetched_at": pd.Timestamp(envelope["fetched_at"]).tz_localize(None).normalize(),
        })
    recovered = pd.concat([known, pd.DataFrame(q4, columns=OUTPUT_COLUMNS)])
    checks = recovered.groupby("metric")["value"].sum().to_dict()
    if checks != s1["annual"]:
        raise RuntimeError(f"LPRO recovered quarters do not close: {checks}")
    expected_q4 = {"revenue": 26_076_000.0, "net_income": 17_440_000.0}
    actual_q4 = pd.DataFrame(q4).set_index("metric")["value"].to_dict()
    if actual_q4 != expected_q4:
        raise RuntimeError(f"LPRO Q4 residual is unexpected: {actual_q4}")

    output_dir.mkdir(parents=True, exist_ok=True)
    facts = output_dir / "strict_quarterly_facts.csv"
    recovered.sort_values(["fiscal_end", "metric"]).to_csv(facts, index=False)
    report = {
        "schema_version": 1, "research_only": True, "ticker": "LPRO",
        "point_in_time_proven": True, "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED", "promotion_eligible": False,
        "accepted_quarter_count": 4, "fact_count": 8,
        "annual_identity": {"expected": s1["annual"], "quarter_sum": checks},
        "q4_residual": actual_q4,
        "sources": [
            {**row.to_dict(), "sha256": _sha256(source)},
            {"path": str(cache_path), "sha256": _sha256(cache_path)},
        ],
        "outputs": {"strict_quarterly_facts": {
            "path": str(facts), "sha256": _sha256(facts)
        }},
        "guardrail": (
            "2019Q1 and FY use the 2020-07-01 S-1; Q2 and Q3 use first-filed "
            "2020 comparative 10-Q facts; Q4 becomes available only when Q3 "
            "was filed and is FY minus the three already disclosed quarters."
        ),
    }
    manifest = output_dir / "manifest.json"
    manifest.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n")
    report["manifest"] = str(manifest)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry-path", type=Path, default=REGISTRY)
    parser.add_argument("--cache-path", type=Path, default=CACHE)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    print(json.dumps(run(args.registry_path, args.cache_path, args.output_dir), indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
