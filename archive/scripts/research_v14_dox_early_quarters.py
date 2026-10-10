#!/usr/bin/env python3
"""Recover the two DOX quarters needed for 2019 eight-quarter history."""

from __future__ import annotations

import argparse
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import time
from urllib.request import Request, urlopen

import pandas as pd

from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.io.fundamentals_update import OUTPUT_COLUMNS


OUTPUT_DIR = Path("output/research_only/v14/dox_early_quarters")
CIK = 1_062_579
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
SOURCES = {
    "2017-06-30": {
        "accession": "0001193125-17-246236", "filed": "2017-08-03",
        "document": "d436150dex991.htm",
        "sha256": "0bfd64adf099d20cb21d3aedcac220caced471cf23ee3372908db97e13b59eb8",
    },
    "2017-09-30": {
        "accession": "0001193125-17-337491", "filed": "2017-11-09",
        "document": "d493135dex991.htm",
        "sha256": "bcbde454fbd1f93393771f02961c589a2a5fca4435a6020f1a022873737054b7",
    },
}
EXPECTED = {
    "2017-06-30": {"revenue": 966_695_000.0, "net_income": 119_264_000.0},
    "2017-09-30": {"revenue": 979_724_000.0, "net_income": 107_209_000.0},
}


def _url(spec: dict) -> str:
    return (
        f"https://www.sec.gov/Archives/edgar/data/{CIK}/"
        f"{spec['accession'].replace('-', '')}/{spec['document']}"
    )


def _fetch(spec: dict) -> bytes:
    request = Request(_url(spec), headers=SEC_HEADERS)
    error = None
    for attempt in range(4):
        try:
            with urlopen(request, timeout=120) as response:
                return response.read()
        except Exception as exc:  # pragma: no cover - network retry
            error = exc
            if attempt < 3:
                time.sleep(attempt + 1)
    raise RuntimeError(f"failed to fetch DOX source {_url(spec)}") from error


def _number(value: object) -> float:
    text = str(value).strip()
    negative = text.startswith("(") or text.startswith("-")
    cleaned = re.sub(r"[^0-9.]", "", text)
    if not cleaned:
        raise RuntimeError(f"DOX filing value is not numeric: {value!r}")
    number = float(cleaned) * 1000.0
    return -number if negative else number


def parse_quarter(raw: bytes) -> dict[str, float]:
    """Read the first (current-quarter) USD value from the GAAP summary."""
    for table in pd.read_html(BytesIO(raw)):
        first = table.iloc[:, 0].astype(str).str.strip().str.lower()
        revenue = first.eq("revenue")
        net_income = first.eq("net income")
        if revenue.sum() != 1 or net_income.sum() != 1:
            continue
        output = {}
        for metric, mask in (("revenue", revenue), ("net_income", net_income)):
            row = table.loc[mask].iloc[0]
            currency_cells = [
                column for column in range(len(row) - 1)
                if str(row.iloc[column]).strip() == "$"
            ]
            if len(currency_cells) != 4:
                continue
            output[metric] = _number(row.iloc[currency_cells[0] + 1])
        if set(output) == {"revenue", "net_income"}:
            return output
    raise RuntimeError("DOX filing lacks the expected GAAP quarterly summary")


def recover(output_dir: Path = OUTPUT_DIR) -> dict:
    fetched_at = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    rows = []
    source_report = []
    observed = {}
    for fiscal_end, spec in SOURCES.items():
        raw = _fetch(spec)
        digest = hashlib.sha256(raw).hexdigest()
        if digest != spec["sha256"]:
            raise RuntimeError(f"DOX source changed for {fiscal_end}: {digest}")
        facts = parse_quarter(raw)
        observed[fiscal_end] = facts
        source_report.append({
            "fiscal_end": fiscal_end, "accession": spec["accession"],
            "filed": spec["filed"], "url": _url(spec),
            "sha256": digest, "bytes": len(raw),
        })
        for metric, value in facts.items():
            rows.append({
                "ticker": "DOX", "fiscal_end": fiscal_end,
                "available_date": spec["filed"], "metric": metric,
                "value": value, "taxonomy": "us-gaap",
                "concept": f"sec_strict_quarter:{metric}", "form": "6-K",
                "accession": spec["accession"], "fetched_at": fetched_at,
            })
    if observed != EXPECTED:
        raise RuntimeError(f"DOX recovered quarters changed: {observed}")
    facts = pd.DataFrame(rows, columns=OUTPUT_COLUMNS).sort_values(
        ["available_date", "fiscal_end", "metric"]
    ).reset_index(drop=True)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1, "research_only": True,
        "point_in_time_proven": True, "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED", "promotion_eligible": False,
        "formal_financials_modified": False, "ticker": "DOX", "cik": CIK,
        "accepted_quarter_count": 2, "accepted_fact_count": 4,
        "sources": source_report,
        "outputs": {"strict_quarterly_facts": {
            "path": str(facts_path),
            "sha256": hashlib.sha256(facts_path.read_bytes()).hexdigest(),
        }},
        "guardrail": (
            "Both quarters use explicit current-quarter USD GAAP facts in "
            "contemporaneous SEC 6-K exhibits. No cumulative period is split, "
            "no later comparative is backdated, and no formal file is changed."
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
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--base-dir", type=Path)
    parser.add_argument("--candidate-output-dir", type=Path)
    args = parser.parse_args()
    if (args.base_dir is None) != (args.candidate_output_dir is None):
        parser.error("--base-dir and --candidate-output-dir must be used together")
    report = recover(args.output_dir)
    if args.base_dir is not None:
        report["candidate"] = integrate_candidate(
            base_dir=args.base_dir, supplement_dir=args.output_dir,
            output_dir=args.candidate_output_dir,
        )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
