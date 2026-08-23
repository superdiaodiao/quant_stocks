#!/usr/bin/env python3
"""Recover SSRM 2021Q1-Q3 from contemporaneous SEC 6-K IFRS exhibits."""

from __future__ import annotations

import argparse
from io import BytesIO
import hashlib
import json
from pathlib import Path
import re
from urllib.request import Request, urlopen

import pandas as pd

from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.io.fundamentals_update import OUTPUT_COLUMNS

OUTPUT_DIR = Path("output/research_only/v14/ssrm_2021_6k_quarters")
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
FILINGS = {
    "2021-03-31": {
        "accession": "0001279569-21-000607",
        "filed": "2021-05-06",
        "ending": "March 31",
        "revenue": 366_484_000.0,
        "net_income": 59_762_000.0,
    },
    "2021-06-30": {
        "accession": "0001279569-21-001073",
        "filed": "2021-08-04",
        "ending": "June 30",
        "revenue": 376_950_000.0,
        "net_income": 51_604_000.0,
    },
    "2021-09-30": {
        "accession": "0001279569-21-001514",
        "filed": "2021-11-03",
        "ending": "September 30",
        "revenue": 322_846_000.0,
        "net_income": 62_454_000.0,
    },
}


def _normalize(value) -> str:
    return " ".join(str(value).replace("\xa0", " ").split()).casefold()


def _number(value) -> float | None:
    text = str(value).strip()
    if not text or text.lower() == "nan" or text in {"$", "—", "-"}:
        return None
    negative = "(" in text
    cleaned = re.sub(r"[^0-9.]", "", text)
    if not cleaned:
        return None
    result = float(cleaned) * 1_000.0
    return -result if negative else result


def _url(spec: dict) -> str:
    accession = spec["accession"].replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/921638/{accession}/ex992.htm"


def _fetch(spec: dict) -> bytes:
    with urlopen(Request(_url(spec), headers=SEC_HEADERS), timeout=120) as response:
        return response.read()


def _extract(raw: bytes, ending: str) -> dict[str, float]:
    candidates = []
    for table in pd.read_html(BytesIO(raw)):
        first = table.iloc[:, 0].map(_normalize)
        revenue_rows = list(table.index[first.eq("revenue")])
        income_rows = list(table.index[first.eq("net income")])
        if len(revenue_rows) != 1 or len(income_rows) != 1:
            continue
        if not any(
            "three months ended" in _normalize(value)
            and ending.casefold() in _normalize(value)
            for value in table.iloc[:8].to_numpy().ravel()
        ):
            continue
        columns = []
        for column in table.columns:
            header = [_normalize(value) for value in table[column].iloc[:8]]
            if (
                any(
                    "three months ended" in value
                    and ending.casefold() in value
                    for value in header
                )
                and "2021" in header
            ):
                columns.append(column)
        values = {}
        for metric, row in (
            ("revenue", revenue_rows[0]),
            ("net_income", income_rows[0]),
        ):
            found = {
                value
                for column in columns
                if (value := _number(table.loc[row, column])) is not None
            }
            if len(found) != 1:
                break
            values[metric] = found.pop()
        if len(values) == 2:
            candidates.append(values)
    unique = {tuple(sorted(candidate.items())) for candidate in candidates}
    if len(unique) != 1:
        raise RuntimeError(
            f"expected one unique SSRM statement result for {ending}, found {unique}"
        )
    return dict(unique.pop())


def recover(output_dir: Path = OUTPUT_DIR) -> dict:
    rows = []
    sources = []
    fetched_at = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    for fiscal_end, spec in FILINGS.items():
        raw = _fetch(spec)
        values = _extract(raw, spec["ending"])
        expected = {
            "revenue": spec["revenue"],
            "net_income": spec["net_income"],
        }
        if values != expected:
            raise RuntimeError(f"SSRM {fiscal_end} exhibit changed: {values}")
        sources.append({
            "fiscal_end": fiscal_end,
            "accession": spec["accession"],
            "filed": spec["filed"],
            "url": _url(spec),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
        })
        for metric, value in values.items():
            rows.append({
                "ticker": "SSRM",
                "fiscal_end": fiscal_end,
                "available_date": spec["filed"],
                "metric": metric,
                "value": value,
                "taxonomy": "ifrs-full",
                "concept": f"sec_6k_plain_html:{'Revenue' if metric == 'revenue' else 'NetIncomeLoss'}",
                "form": "6-K:EX-99.2:THREE_MONTHS",
                "accession": spec["accession"],
                "fetched_at": fetched_at,
            })
    facts = pd.DataFrame(rows, columns=OUTPUT_COLUMNS).sort_values(
        ["fiscal_end", "metric"]
    )
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "ticker": "SSRM",
        "cik": 921_638,
        "accepted_quarter_count": 3,
        "accepted_fact_count": 6,
        "sources": sources,
        "outputs": {"strict_quarterly_facts": {"path": str(facts_path), "sha256": hashlib.sha256(facts_path.read_bytes()).hexdigest()}},
        "guardrail": (
            "Uses explicit three-month IFRS statements in contemporaneous 6-K "
            "Exhibit 99.2 files. No half-year allocation and no later US-GAAP "
            "restatement is used."
        ),
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report["manifest"] = str(manifest_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--base-dir", type=Path)
    parser.add_argument("--candidate-output-dir", type=Path)
    args = parser.parse_args()
    report = recover(args.output_dir)
    if (args.base_dir is None) != (args.candidate_output_dir is None):
        parser.error("--base-dir and --candidate-output-dir must be used together")
    if args.base_dir is not None:
        report["candidate"] = integrate_candidate(base_dir=args.base_dir, supplement_dir=args.output_dir, output_dir=args.candidate_output_dir)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
