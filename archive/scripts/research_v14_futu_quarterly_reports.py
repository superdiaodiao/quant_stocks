#!/usr/bin/env python3
"""Recover FUTU 2019Q1-2020Q3 from contemporaneous SEC 6-K exhibits."""

from __future__ import annotations

import argparse
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
from urllib.request import Request, urlopen

import pandas as pd

from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.io.fundamentals_update import OUTPUT_COLUMNS


OUTPUT_DIR = Path("output/research_only/v14/futu_quarterly_reports")
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
SOURCES = {
    "2019-03-31": {
        "accession": "0001193125-19-155937", "filed": "2019-05-24",
        "document": "d742029dex991.htm", "revenue": 236_449_000.0,
        "net_income": 45_541_000.0,
        "comparative_fiscal_end": "2018-03-31",
        "comparative_revenue": 172_409_000.0,
        "comparative_net_income": 45_781_000.0,
    },
    "2019-06-30": {
        "accession": "0001193125-19-228482", "filed": "2019-08-26",
        "document": "d767225dex991.htm", "revenue": 259_854_000.0,
        "net_income": 55_330_000.0,
        "comparative_fiscal_end": "2018-06-30",
        "comparative_revenue": 186_232_000.0,
        "comparative_net_income": 24_237_000.0,
    },
    "2019-09-30": {
        "accession": "0001564590-19-044147", "filed": "2019-11-22",
        "document": "futu-ex991_6.htm", "revenue": 254_342_000.0,
        "net_income": 20_851_000.0,
        "comparative_fiscal_end": "2018-09-30",
        "comparative_revenue": 225_526_000.0,
        "comparative_net_income": 30_320_000.0,
    },
    "2019-12-31": {
        "accession": "0001104659-20-034955", "filed": "2020-03-18",
        "document": "a20-12946_1ex99d1.htm", "revenue": 310_910_000.0,
        "net_income": 43_942_000.0,
        "comparative_fiscal_end": "2018-12-31",
        "comparative_revenue": 227_176_000.0,
        "comparative_net_income": 38_174_000.0,
    },
    "2020-03-31": {
        "accession": "0001104659-20-061090", "filed": "2020-05-14",
        "document": "a20-19544_1ex99d1.htm", "revenue": 490_642_000.0,
        "net_income": 154_854_000.0,
    },
    "2020-06-30": {
        "accession": "0001104659-20-094244", "filed": "2020-08-13",
        "document": "a20-27099_1ex99d1.htm", "revenue": 687_564_000.0,
        "net_income": 236_488_000.0,
    },
    "2020-09-30": {
        "accession": "0001104659-20-127100", "filed": "2020-11-19",
        "document": "a20-36228_1ex99d1.htm", "revenue": 946_172_000.0,
        "net_income": 401_721_000.0,
    },
}


def _url(spec: dict) -> str:
    accession = spec["accession"].replace("-", "")
    return (
        "https://www.sec.gov/Archives/edgar/data/1754581/"
        f"{accession}/{spec['document']}"
    )


def _fetch(spec: dict) -> bytes:
    with urlopen(Request(_url(spec), headers=SEC_HEADERS), timeout=120) as response:
        return response.read()


def _number(value: object) -> float | None:
    text = str(value).strip().replace(",", "")
    if text.casefold() in {"", "nan", "—", "-"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = re.sub(r"[^0-9.]", "", text)
    if not text:
        return None
    number = float(text)
    return -number if negative else number


def _hkd_thousands_pair(raw: bytes, label: str) -> tuple[float, float]:
    candidates = set()
    for table in pd.read_html(BytesIO(raw)):
        string = table.astype(str)
        matching = table.loc[
            string.apply(
                lambda column: column.str.fullmatch(
                    rf"\s*{re.escape(label)}\s*", case=False
                )
            ).any(axis=1)
        ]
        for row in matching.itertuples(index=False, name=None):
            numbers = [number for value in row if (number := _number(value)) is not None]
            # Every source statement is ordered prior-year quarter, current
            # quarter, current-quarter USD, followed by optional YTD columns.
            if len(numbers) >= 3:
                candidates.add((numbers[0], numbers[1]))
    if len(candidates) != 1:
        raise RuntimeError(
            f"expected one current-quarter FUTU {label}, found {sorted(candidates)}"
        )
    comparative, current = candidates.pop()
    return comparative * 1000.0, current * 1000.0


def parse_quarter(raw: bytes) -> dict[str, float]:
    return {
        "revenue": _hkd_thousands_pair(raw, "Total revenues")[1],
        "net_income": _hkd_thousands_pair(raw, "Net income")[1],
    }


def recover(output_dir: Path = OUTPUT_DIR) -> dict:
    rows = []
    sources = []
    fetched_at = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    for fiscal_end, spec in SOURCES.items():
        raw = _fetch(spec)
        values = parse_quarter(raw)
        expected = {key: spec[key] for key in ("revenue", "net_income")}
        if values != expected:
            raise RuntimeError(
                f"FUTU {fiscal_end} source changed: {values} != {expected}"
            )
        if "comparative_fiscal_end" in spec:
            comparative = {
                "revenue": _hkd_thousands_pair(raw, "Total revenues")[0],
                "net_income": _hkd_thousands_pair(raw, "Net income")[0],
            }
            expected_comparative = {
                metric: spec[f"comparative_{metric}"]
                for metric in ("revenue", "net_income")
            }
            if comparative != expected_comparative:
                raise RuntimeError(
                    f"FUTU {fiscal_end} comparative changed: "
                    f"{comparative} != {expected_comparative}"
                )
            for metric, value in comparative.items():
                rows.append({
                    "ticker": "FUTU",
                    "fiscal_end": spec["comparative_fiscal_end"],
                    "available_date": spec["filed"], "metric": metric,
                    "value": value, "taxonomy": "us-gaap",
                    "concept": f"sec_6k_exhibit_comparative:{'TotalRevenues' if metric == 'revenue' else 'NetIncome'}",
                    "form": "6-K", "accession": spec["accession"],
                    "fetched_at": fetched_at,
                })
        for metric, value in values.items():
            rows.append({
                "ticker": "FUTU", "fiscal_end": fiscal_end,
                "available_date": spec["filed"], "metric": metric,
                "value": value, "taxonomy": "us-gaap",
                "concept": f"sec_6k_exhibit:{'TotalRevenues' if metric == 'revenue' else 'NetIncome'}",
                "form": "6-K", "accession": spec["accession"],
                "fetched_at": fetched_at,
            })
        sources.append({
            "fiscal_end": fiscal_end, "accession": spec["accession"],
            "filed": spec["filed"], "url": _url(spec), "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        })
    facts = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1, "research_only": True,
        "point_in_time_proven": True, "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED", "promotion_eligible": False,
        "formal_financials_modified": False, "ticker": "FUTU",
        "cik": 1_754_581,
        "accepted_quarter_count": int(facts["fiscal_end"].nunique()),
        "sources": sources,
        "outputs": {"strict_quarterly_facts": {
            "path": str(facts_path),
            "sha256": hashlib.sha256(facts_path.read_bytes()).hexdigest(),
        }},
        "guardrail": (
            "Uses exact HKD-thousand GAAP statement rows from each quarter's "
            "contemporaneous SEC 6-K exhibit. Later comparative filings do not "
            "replace earlier availability dates."
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
