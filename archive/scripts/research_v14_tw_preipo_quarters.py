#!/usr/bin/env python3
"""Recover Tradeweb's explicit 2017Q1-2018Q4 S-1 quarter table."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from io import BytesIO
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd

from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.io.fundamentals_update import OUTPUT_COLUMNS


REGISTRY = Path("stocks_list_dir/nasdaq/tw_2019_ipo_s1.csv")
OUTPUT_DIR = Path("output/research_only/v14/tw_preipo_quarters")
HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
EXPECTED = {
    "2017-03-31": {"revenue": 138_335_000.0, "net_income": 33_380_000.0},
    "2017-06-30": {"revenue": 139_676_000.0, "net_income": 12_110_000.0},
    "2017-09-30": {"revenue": 141_558_000.0, "net_income": 31_051_000.0},
    "2017-12-31": {"revenue": 143_399_000.0, "net_income": 7_106_000.0},
    "2018-03-31": {"revenue": 169_503_000.0, "net_income": 45_308_000.0},
    "2018-06-30": {"revenue": 171_015_000.0, "net_income": 38_897_000.0},
    "2018-09-30": {"revenue": 165_253_000.0, "net_income": 45_954_000.0},
    "2018-12-31": {"revenue": 178_637_000.0, "net_income": 29_307_000.0},
}
DATE_LABELS = {
    "2017-03-31": "mar. 31, 2017",
    "2017-06-30": "june 30, 2017",
    "2017-09-30": "sept. 30, 2017",
    "2017-12-31": "dec. 31, 2017",
    "2018-03-31": "mar. 31, 2018",
    "2018-06-30": "june 30, 2018",
    "2018-09-30": "sept. 30, 2018",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normal(value: object) -> str:
    return " ".join(str(value).replace("\xa0", " ").replace("\u200b", " ").split()).casefold()


def _accounting_value(value: object) -> float:
    text = str(value).replace(",", "").replace("$", "").strip()
    match = re.search(r"\d+(?:\.\d+)?", text)
    if match is None:
        raise ValueError(f"no accounting value in {value!r}")
    number = float(match.group())
    return -number if "(" in text else number


def extract_quarters(path: Path) -> dict[str, dict[str, float]]:
    candidates = []
    for table in pd.read_html(BytesIO(path.read_bytes())):
        if len(table) < 20 or len(table.columns) < 45:
            continue
        labels = table.iloc[:, 0].map(_normal)
        if labels.eq("gross revenues").sum() != 1 or labels.eq("net income").sum() != 1:
            continue
        headers = " ".join(
            _normal(value) for value in table.head(4).to_numpy().ravel()
        )
        if "successor" not in headers or "predecessor" not in headers:
            continue
        if "october 1, 2018 to december 31, 2018" not in headers:
            continue
        revenue = table.loc[labels.eq("gross revenues")].iloc[0]
        income = table.loc[labels.eq("net income")].iloc[0]

        def value_for(label: str, row: pd.Series) -> float:
            matches = []
            for column in table.columns:
                column_headers = {
                    _normal(table.iloc[index][column])
                    for index in range(min(4, len(table)))
                }
                if label not in column_headers:
                    continue
                try:
                    matches.append(_accounting_value(row[column]) * 1_000.0)
                except ValueError:
                    continue
            unique = sorted(set(matches))
            if len(unique) != 1:
                raise ValueError(f"TW S-1 {label} values are ambiguous: {unique}")
            return unique[0]

        recovered = {}
        for fiscal_end, label in DATE_LABELS.items():
            recovered[fiscal_end] = {
                "revenue": value_for(label, revenue),
                "net_income": value_for(label, income),
            }
        successor_label = "october 1, 2018 to december 31, 2018"
        recovered["2018-12-31"] = {
            "revenue": value_for(successor_label, revenue),
            "net_income": value_for(successor_label, income),
        }
        candidates.append(recovered)
    if not candidates or any(candidate != EXPECTED for candidate in candidates):
        raise ValueError(f"TW S-1 quarter table changed: {candidates}")
    return EXPECTED


def run(registry_path: Path = REGISTRY, output_dir: Path = OUTPUT_DIR) -> dict:
    registry = pd.read_csv(registry_path, dtype={"cik": str, "accession": str})
    if (
        len(registry) != 1
        or registry.iloc[0]["ticker"] != "TW"
        or registry.iloc[0]["cik"] != "1758730"
    ):
        raise ValueError("TW registry must bind exactly CIK 1758730")
    row = registry.iloc[0]
    if row["form"] != "S-1" or row["accession"].replace("-", "") not in row["source_url"]:
        raise ValueError("TW registry must bind the original IPO S-1")
    source = Path(row["local_path"])
    source.parent.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        with urlopen(Request(row["source_url"], headers=HEADERS), timeout=120) as response:
            source.write_bytes(response.read())
    quarters = extract_quarters(source)
    records = []
    for fiscal_end, values in quarters.items():
        issuer_basis = "SUCCESSOR" if fiscal_end == "2018-12-31" else "PREDECESSOR"
        for metric, value in values.items():
            records.append({
                "ticker": "TW", "fiscal_end": fiscal_end,
                "available_date": row["available_date"], "metric": metric,
                "value": value, "taxonomy": f"TW_GAAP_S1_{issuer_basis}",
                "concept": (
                    "s1_direct_gross_revenues" if metric == "revenue"
                    else "s1_direct_net_income"
                ),
                "form": "S-1", "accession": row["accession"],
                "fetched_at": pd.Timestamp("2026-08-13"),
            })
    facts = pd.DataFrame(records, columns=OUTPUT_COLUMNS).sort_values(
        ["fiscal_end", "metric"]
    )
    if len(facts) != 16 or facts.groupby("fiscal_end")["metric"].nunique().ne(2).any():
        raise RuntimeError("TW recovery must contain eight paired quarters")
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1, "research_only": True, "ticker": "TW",
        "point_in_time_proven": True, "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED", "promotion_eligible": False,
        "accepted_quarter_count": 8, "fact_count": 16,
        "predecessor_quarter_count": 7, "successor_quarter_count": 1,
        "recovered_quarters": quarters,
        "sources": [{**row.to_dict(), "sha256": _sha256(source)}],
        "outputs": {"strict_quarterly_facts": {
            "path": str(facts_path), "sha256": _sha256(facts_path)
        }},
        "guardrail": (
            "Only the S-1's explicit quarterly table is accepted. Revenue uses "
            "Gross revenues, matching the later SEC quarterly revenue facts; "
            "Net Revenue is not substituted. 2017Q1-2018Q3 remain explicitly "
            "labelled predecessor and 2018Q4 successor in taxonomy provenance."
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
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--base-dir", type=Path)
    parser.add_argument("--candidate-output-dir", type=Path)
    args = parser.parse_args()
    report = run(args.registry_path, args.output_dir)
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
