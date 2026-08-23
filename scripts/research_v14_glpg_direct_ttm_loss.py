#!/usr/bin/env python3
"""Recover exact GLPG TTM losses from contemporaneous SEC XBRL filings."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

import pandas as pd

from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.io.fundamentals_update import OUTPUT_COLUMNS


OUTPUT_DIR = Path("output/research_only/v14/glpg_direct_ttm_loss")
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
SOURCES = {
    "2018_fy": {
        "accession": "0001558370-19-002655",
        "filed": "2019-03-29",
        "document": "glpg-20181231.xml",
        "context": "Duration_1_1_2018_To_12_31_2018",
        "expected": -29_259_000.0,
    },
    "2018_h1": {
        "accession": "0001558370-19-006344",
        "filed": "2019-07-25",
        "document": "glpg-20190630.xml",
        "context": "Duration_1_1_2018_To_6_30_2018",
        "expected": -59_056_000.0,
    },
    "2019_h1": {
        "accession": "0001558370-19-006344",
        "filed": "2019-07-25",
        "document": "glpg-20190630.xml",
        "context": "Duration_1_1_2019_To_6_30_2019",
        "expected": -95_905_000.0,
    },
}
EXPECTED_TTM = {
    "2018-12-31": -29_259_000.0,
    "2019-06-30": -66_108_000.0,
}


def _url(spec: dict) -> str:
    accession = spec["accession"].replace("-", "")
    return (
        "https://www.sec.gov/Archives/edgar/data/1421876/"
        f"{accession}/{spec['document']}"
    )


def _fetch(spec: dict) -> bytes:
    with urlopen(Request(_url(spec), headers=SEC_HEADERS), timeout=120) as response:
        return response.read()


def _extract(raw: bytes, context: str) -> float:
    root = ET.fromstring(raw)
    values = {
        float(element.text)
        for element in root.iter()
        if element.tag.endswith("}ProfitLoss")
        and element.attrib.get("contextRef") == context
        and element.text is not None
    }
    if len(values) != 1:
        raise RuntimeError(
            f"expected one GLPG ProfitLoss fact for {context}, found {values}"
        )
    return values.pop()


def recover(output_dir: Path = OUTPUT_DIR) -> dict:
    raw_by_url: dict[str, bytes] = {}
    values = {}
    for name, spec in SOURCES.items():
        url = _url(spec)
        raw = raw_by_url.setdefault(url, _fetch(spec))
        value = _extract(raw, spec["context"])
        if value != spec["expected"]:
            raise RuntimeError(f"GLPG {name} source changed: {value}")
        values[name] = value
    ttm = {
        "2018-12-31": values["2018_fy"],
        "2019-06-30": (
            values["2018_fy"] - values["2018_h1"] + values["2019_h1"]
        ),
    }
    if ttm != EXPECTED_TTM:
        raise RuntimeError(f"GLPG exact TTM values changed: {ttm}")
    metadata = {
        "2018-12-31": ("2019-03-29", SOURCES["2018_fy"]["accession"], "20-F"),
        "2019-06-30": (
            "2019-07-25",
            "+".join([
                SOURCES["2018_fy"]["accession"],
                SOURCES["2019_h1"]["accession"],
            ]),
            "20-F_PLUS_6-K_H1",
        ),
    }
    fetched_at = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    facts = pd.DataFrame([{
        "ticker": "GLPG",
        "fiscal_end": fiscal_end,
        "available_date": metadata[fiscal_end][0],
        "metric": "net_income_ttm",
        "value": value,
        "taxonomy": "ifrs-full",
        "concept": "sec_exact_ttm:ProfitLoss",
        "form": metadata[fiscal_end][2],
        "accession": metadata[fiscal_end][1],
        "fetched_at": fetched_at,
    } for fiscal_end, value in ttm.items()], columns=OUTPUT_COLUMNS)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    sources = []
    for spec in SOURCES.values():
        url = _url(spec)
        if any(item["url"] == url for item in sources):
            continue
        raw = raw_by_url[url]
        sources.append({
            "accession": spec["accession"], "filed": spec["filed"],
            "url": url, "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        })
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "ticker": "GLPG",
        "cik": 1_421_876,
        "accepted_exact_ttm_count": len(facts),
        "sources": sources,
        "outputs": {"strict_quarterly_facts": {
            "path": str(facts_path),
            "sha256": hashlib.sha256(facts_path.read_bytes()).hexdigest(),
        }},
        "guardrail": (
            "Uses exact annual loss and FY-minus-prior-H1-plus-current-H1 "
            "rolling loss from contemporaneous SEC XBRL. It does not split "
            "six-month values into quarters or create growth eligibility."
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
            base_dir=args.base_dir,
            supplement_dir=args.output_dir,
            output_dir=args.candidate_output_dir,
        )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
