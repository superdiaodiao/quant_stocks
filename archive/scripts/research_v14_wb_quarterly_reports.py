#!/usr/bin/env python3
"""Recover WB 2017Q1-2021Q1 from contemporaneous SEC 6-K exhibits."""

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


OUTPUT_DIR = Path("output/research_only/v14/wb_quarterly_reports")
CIK = 1_595_761
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
SOURCES = {
    "2017-03-31": ("2017-05-17", "0001104659-17-033466", "a17-13392_1ex99d1.htm", "ca8d63ca0aaa49e9fdaf2564336c138543fddca1d0c260ddfd07de99b10927cd"),
    "2017-06-30": ("2017-08-10", "0001104659-17-050860", "a17-20024_1ex99d1.htm", "69ee94848f23821db591cb5f23809fca86b74ba846957e42e958cd4662b226eb"),
    "2017-09-30": ("2017-11-08", "0001104659-17-066832", "a17-26150_1ex99d1.htm", "b5b936bd19d626204a1b4a41727b4d714307df0290775249bb05cbed6a48fd8e"),
    "2017-12-31": ("2018-02-14", "0001104659-18-009224", "a18-6002_1ex99d1.htm", "ffd8e706adf0533306e41477bf9c320e7fccf5c0604a4e013ec8673684e62878"),
    "2018-03-31": ("2018-05-10", "0001104659-18-031881", "a18-13196_1ex99d1.htm", "892c67a5d1292c007fe45fda1d2568c7de9738cb275ab5b6cf4b1c57494b2985"),
    "2018-06-30": ("2018-08-09", "0001104659-18-050683", "a18-18478_1ex99d1.htm", "3bb21a60f2fa4b5de85d7f691ce710bf7fe552f38c40653cd36edeb0436faba8"),
    "2018-09-30": ("2018-11-29", "0001104659-18-070442", "a18-40805_1ex99d1.htm", "4ceb8ef2818965ad82cf404dde7aec28e0fb0c8fb11a7fe86e69fd8c273b46e3"),
    "2018-12-31": ("2019-03-06", "0001104659-19-013018", "a19-5878_1ex99d1.htm", "9584204d6a201b86476865917a8cb2086276bfc329e8eebc2709c6f61f6f667a"),
    "2019-03-31": ("2019-05-24", "0001104659-19-031623", "a19-10570_1ex99.htm", "fb675cd09c420eb99042317c2ab9050f860f133e55f7a6c31b7ac7328265060f"),
    "2019-06-30": ("2019-08-20", "0001104659-19-046592", "a19-17337_1ex99d1.htm", "e88b7d652ca6631ba18778fcd5f6b6817797e54f17dd78f9cc8cc2e56691ae66"),
    "2019-09-30": ("2019-11-15", "0001104659-19-064507", "a19-22941_1ex99d1.htm", "44f0dcace0a9dece35f39ba77b8c5610b0d999d59fc309020ab27b7a42f589cb"),
    "2019-12-31": ("2020-02-27", "0001104659-20-025622", "a20-10996_1ex99d1.htm", "668eb543af32a18b8125bddf3ff9f12a394f5556bd496bda7dbf39d4ce8adfdf"),
    "2020-03-31": ("2020-05-20", "0001104659-20-064264", "a20-20192_1ex99d1.htm", "08c4fa06c05817dd4e21262bff0ed81744b2c5935e696c4a7e8829184a94d38e"),
    "2020-06-30": ("2020-09-29", "0001104659-20-109461", "a20-31897_1ex99d1.htm", "1734448981c4c59301b3b1d14d4eb5a0305ab440263e61c06ca45870ca1a2efb"),
    "2020-09-30": ("2020-12-29", "0001104659-20-139970", "tm2039396d1_ex99-1.htm", "01fba014bb24d5b138917905a92a012616c6b44936dda3b2becaad442525ecde"),
    "2020-12-31": ("2021-03-19", "0001104659-21-038640", "a21-10182_1ex99d1.htm", "48c013d0a40ce1859b773607b60ca5f1d1d0d677d12ef89e4d6f0f8c3886a910"),
    "2021-03-31": ("2021-05-11", "0001104659-21-064199", "tm2115724d1_ex99-1.htm", "0b3ab941912c692fb5c240937c8b8e0233a7c9a3364fc1b86205856902c56102"),
}
EXPECTED = {
    "2017-03-31": (199_201_000.0, 46_931_000.0),
    "2017-06-30": (253_373_000.0, 73_548_000.0),
    "2017-09-30": (320_035_000.0, 101_129_000.0),
    "2017-12-31": (377_445_000.0, 130_982_000.0),
    "2018-03-31": (349_883_000.0, 99_085_000.0),
    "2018-06-30": (426_589_000.0, 140_914_000.0),
    "2018-09-30": (460_171_000.0, 165_317_000.0),
    "2018-12-31": (481_875_000.0, 166_507_000.0),
    "2019-03-31": (399_177_000.0, 150_442_000.0),
    "2019-06-30": (431_836_000.0, 102_996_000.0),
    "2019-09-30": (467_753_000.0, 146_169_000.0),
    "2019-12-31": (468_148_000.0, 95_068_000.0),
    "2020-03-31": (323_389_000.0, 52_108_000.0),
    "2020-06-30": (387_393_000.0, 198_416_000.0),
    "2020-09-30": (465_739_000.0, 33_798_000.0),
    "2020-12-31": (513_410_000.0, 29_042_000.0),
    "2021-03-31": (458_896_000.0, 49_820_000.0),
}


def _spec(values: tuple[str, str, str, str]) -> dict:
    filed, accession, document, sha256 = values
    return {"filed": filed, "accession": accession, "document": document, "sha256": sha256}


def _url(spec: dict) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{CIK}/{spec['accession'].replace('-', '')}/{spec['document']}"


def _fetch(spec: dict) -> bytes:
    request = Request(_url(spec), headers=SEC_HEADERS)
    error = None
    for attempt in range(4):
        try:
            with urlopen(request, timeout=120) as response:
                return response.read()
        except Exception as exc:  # pragma: no cover
            error = exc
            if attempt < 3:
                time.sleep(attempt + 1)
    raise RuntimeError(f"failed to fetch WB source {_url(spec)}") from error


def _number(value: object) -> float:
    text = str(value).strip()
    negative = text.startswith("(") or text.startswith("-")
    cleaned = re.sub(r"[^0-9.]", "", text)
    if not cleaned:
        raise RuntimeError(f"WB filing value is not numeric: {value!r}")
    return (-1.0 if negative else 1.0) * float(cleaned) * 1000.0


def parse_quarter(raw: bytes) -> dict[str, float]:
    for table in pd.read_html(BytesIO(raw)):
        first = table.iloc[:, 0].astype(str).str.replace(r"\s+", " ", regex=True).str.strip()
        revenue = first.str.fullmatch(r"(?i)(?:Total )?Net revenues")
        income = first.str.fullmatch(r"(?i)Net income attributable to Weibo(?:'s shareholders)?")
        if not revenue.any() or not income.any():
            continue
        revenue_row = table.loc[revenue].iloc[0]
        revenue_values = [value for value in revenue_row.iloc[1:] if pd.notna(value)]
        income_row = table.loc[income].iloc[0]
        currency = [i for i in range(len(income_row) - 1) if str(income_row.iloc[i]).strip() == "$"]
        if not revenue_values or not currency:
            continue
        return {"revenue": _number(revenue_values[0]), "net_income": _number(income_row.iloc[currency[0] + 1])}
    raise RuntimeError("WB filing lacks the GAAP issuer quarterly summary")


def recover(output_dir: Path = OUTPUT_DIR) -> dict:
    fetched_at = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    rows, sources, observed = [], [], {}
    for fiscal_end, raw_spec in SOURCES.items():
        spec = _spec(raw_spec)
        raw = _fetch(spec)
        digest = hashlib.sha256(raw).hexdigest()
        if digest != spec["sha256"]:
            raise RuntimeError(f"WB source changed for {fiscal_end}: {digest}")
        facts = parse_quarter(raw)
        observed[fiscal_end] = (facts["revenue"], facts["net_income"])
        sources.append({"fiscal_end": fiscal_end, "filed": spec["filed"], "accession": spec["accession"], "url": _url(spec), "sha256": digest, "bytes": len(raw)})
        for metric, value in facts.items():
            rows.append({"ticker": "WB", "fiscal_end": fiscal_end, "available_date": spec["filed"], "metric": metric, "value": value, "taxonomy": "us-gaap", "concept": f"sec_strict_quarter:{metric}", "form": "6-K", "accession": spec["accession"], "fetched_at": fetched_at})
    if observed != EXPECTED:
        raise RuntimeError(f"WB recovered quarters changed: {observed}")
    facts = pd.DataFrame(rows, columns=OUTPUT_COLUMNS).sort_values(["available_date", "fiscal_end", "metric"]).reset_index(drop=True)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    report = {"schema_version": 1, "research_only": True, "point_in_time_proven": True, "parameters_frozen": False, "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN", "release_status": "BLOCKED", "promotion_eligible": False, "formal_financials_modified": False, "ticker": "WB", "cik": CIK, "accepted_quarter_count": 17, "accepted_fact_count": 34, "sources": sources, "outputs": {"strict_quarterly_facts": {"path": str(facts_path), "sha256": hashlib.sha256(facts_path.read_bytes()).hexdigest()}}, "guardrail": "Every observation is an explicit current-quarter USD GAAP issuer fact in a contemporaneous SEC 6-K exhibit. Non-GAAP rows, adjustment columns, cumulative periods and later comparatives are excluded."}
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
    if (args.base_dir is None) != (args.candidate_output_dir is None):
        parser.error("--base-dir and --candidate-output-dir must be used together")
    report = recover(args.output_dir)
    if args.base_dir is not None:
        report["candidate"] = integrate_candidate(base_dir=args.base_dir, supplement_dir=args.output_dir, output_dir=args.candidate_output_dir)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
