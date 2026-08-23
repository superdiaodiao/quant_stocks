#!/usr/bin/env python3
"""Recover exact HCM TTM losses from annual and six-month SEC filings."""

from __future__ import annotations

import argparse
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
import hashlib
import json
from pathlib import Path
import re
from urllib.request import Request, urlopen
import warnings

import pandas as pd

from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.io.fundamentals_update import OUTPUT_COLUMNS


OUTPUT_DIR = Path("output/research_only/v14/hcm_direct_ttm_loss")
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
SOURCES = {
    "2019_fy": {
        "accession": "0001104659-20-028220",
        "filed": "2020-03-03",
        "document": "hcm-20191231x20fe6f6a8.htm",
        "context": "Duration_1_1_2019_To_12_31_2019",
        "expected": -106_024_000.0,
    },
    "2020_h1": {
        "accession": "0001104659-20-088202",
        "filed": "2020-07-30",
        "document": "hcm-20200630xex991.htm",
        "context": "Duration_1_1_2020_To_6_30_2020",
        "expected": -49_694_000.0,
    },
    "2019_h1_comparative": {
        "accession": "0001104659-20-088202",
        "filed": "2020-07-30",
        "document": "hcm-20200630xex991.htm",
        "context": "Duration_1_1_2019_To_6_30_2019",
        "expected": -45_369_000.0,
    },
    "2020_fy": {
        "accession": "0001104659-21-031897",
        "filed": "2021-03-04",
        "document": "hcm-20201231x20f.htm",
        "context": "Duration_1_1_2020_To_12_31_2020",
        "expected": -125_730_000.0,
    },
    "2021_h1": {
        "accession": "0001104659-21-096648",
        "filed": "2021-07-28",
        "document": "hcm-20210630xex991.htm",
        "context": "Duration_1_1_2021_To_6_30_2021",
        "expected": -102_397_000.0,
    },
}
EXPECTED_TTM = {
    "2019-12-31": -106_024_000.0,
    "2020-06-30": -110_349_000.0,
    "2020-12-31": -125_730_000.0,
    "2021-06-30": -178_433_000.0,
}


def _url(spec: dict) -> str:
    accession = spec["accession"].replace("-", "")
    return (
        "https://www.sec.gov/Archives/edgar/data/1648257/"
        f"{accession}/{spec['document']}"
    )


def _fetch(spec: dict) -> bytes:
    with urlopen(Request(_url(spec), headers=SEC_HEADERS), timeout=120) as response:
        return response.read()


def _number(tag) -> float:
    cleaned = re.sub(r"[^0-9.]", "", tag.get_text(" ", strip=True))
    if not cleaned:
        raise ValueError("inline XBRL fact has no numeric value")
    value = float(cleaned) * (10 ** int(tag.get("scale", "0")))
    return -abs(value) if tag.get("sign") == "-" else value


def _extract(raw: bytes, context_fragment: str) -> float:
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
    soup = BeautifulSoup(raw, "lxml")
    candidates = {
        _number(tag)
        for tag in soup.find_all(
            lambda item: item.name
            and item.name.casefold().endswith("nonfraction")
            and str(item.get("name", "")).casefold()
            == "us-gaap:netincomeloss"
            and str(item.get("contextref", "")).casefold()
            .startswith(context_fragment.casefold())
            and "axis" not in str(item.get("contextref", "")).casefold()
        )
    }
    if len(candidates) != 1:
        raise RuntimeError(
            f"expected one HCM net income fact for {context_fragment}, "
            f"found {sorted(candidates)}"
        )
    return candidates.pop()


def recover(output_dir: Path = OUTPUT_DIR) -> dict:
    raw_by_url = {}
    values = {}
    for name, spec in SOURCES.items():
        url = _url(spec)
        raw = raw_by_url.setdefault(url, _fetch(spec))
        value = _extract(raw, spec["context"])
        if value != spec["expected"]:
            raise RuntimeError(f"HCM {name} source changed: {value}")
        values[name] = value
    ttm = {
        "2019-12-31": values["2019_fy"],
        "2020-06-30": (
            values["2019_fy"] - values["2019_h1_comparative"]
            + values["2020_h1"]
        ),
        "2020-12-31": values["2020_fy"],
        "2021-06-30": (
            values["2020_fy"] - values["2020_h1"] + values["2021_h1"]
        ),
    }
    if ttm != EXPECTED_TTM:
        raise RuntimeError(f"HCM exact TTM values changed: {ttm}")
    available_dates = {
        "2019-12-31": SOURCES["2019_fy"]["filed"],
        "2020-06-30": SOURCES["2020_h1"]["filed"],
        "2020-12-31": SOURCES["2020_fy"]["filed"],
        "2021-06-30": SOURCES["2021_h1"]["filed"],
    }
    accessions = {
        "2019-12-31": SOURCES["2019_fy"]["accession"],
        "2020-06-30": "+".join([
            SOURCES["2019_fy"]["accession"],
            SOURCES["2020_h1"]["accession"],
        ]),
        "2020-12-31": SOURCES["2020_fy"]["accession"],
        "2021-06-30": "+".join([
            SOURCES["2020_fy"]["accession"],
            SOURCES["2021_h1"]["accession"],
        ]),
    }
    fetched_at = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    facts = pd.DataFrame([{
        "ticker": "HCM",
        "fiscal_end": fiscal_end,
        "available_date": available_dates[fiscal_end],
        "metric": "net_income_ttm",
        "value": value,
        "taxonomy": "us-gaap",
        "concept": "sec_exact_ttm:NetIncomeLoss",
        "form": "20-F" if fiscal_end.endswith("12-31") else "20-F_PLUS_6-K_H1",
        "accession": accessions[fiscal_end],
        "fetched_at": fetched_at,
    } for fiscal_end, value in ttm.items()], columns=OUTPUT_COLUMNS)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    unique_sources = []
    for name, spec in SOURCES.items():
        url = _url(spec)
        if any(source["url"] == url for source in unique_sources):
            continue
        raw = raw_by_url[url]
        unique_sources.append({
            "name": name,
            "accession": spec["accession"],
            "filed": spec["filed"],
            "url": url,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
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
        "ticker": "HCM",
        "cik": 1_648_257,
        "accepted_exact_ttm_count": len(facts),
        "sources": unique_sources,
        "outputs": {"strict_quarterly_facts": {
            "path": str(facts_path),
            "sha256": hashlib.sha256(facts_path.read_bytes()).hexdigest(),
        }},
        "guardrail": (
            "Uses exact annual TTM losses and exact FY-minus-H1-plus-next-H1 "
            "rolling losses. It does not split six-month values into quarters "
            "and cannot create a positive-growth eligibility record."
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
    report = recover(args.output_dir)
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
