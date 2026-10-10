#!/usr/bin/env python3
"""Recover AFMD 2018Q1-2021Q2 from contemporaneous SEC filings."""

from __future__ import annotations

import argparse
from io import BytesIO
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.request import Request, urlopen

from lxml import etree
import pandas as pd

from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.io.fundamentals_update import OUTPUT_COLUMNS


OUTPUT_DIR = Path("output/research_only/v14/afmd_quarterly_reports")
CIK = 1_608_390
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
SOURCES = {
    "2019_h1": {
        "accession": "0001558370-19-007372", "filed": "2019-08-07",
        "document": "afmd-20190630.xml", "kind": "xbrl",
        "sha256": "29d3a15b1247437bb0ecb3b583be8c5eea7ea7ffa7810b12d699d44161e58e0d",
    },
    "2019_q3": {
        "accession": "0001193125-19-295163", "filed": "2019-11-19",
        "document": "d766201dex991.htm", "kind": "nine_month_html",
        "sha256": "bc02d097d7badbb44a3e007dbc75e8e039c0c7503b769cabf7de64b5c031bcf5",
    },
    "2019_fy": {
        "accession": "0001558370-20-004500", "filed": "2020-04-28",
        "document": "afmd-20191231.xml", "kind": "xbrl",
        "sha256": "aba9dd18665c3cf236ca9697c73d637c90f376b8f85ebd1e4317d2615d1606b7",
    },
    "2020_h1": {
        "accession": "0001104659-20-092988", "filed": "2020-08-11",
        "document": "afmd-20200630.xml", "kind": "xbrl",
        "sha256": "31623aee1db2090490e25040858ac24b5de4221cf39e865a64c06393ace5ba4d",
    },
    "2020_q3": {
        "accession": "0001193125-20-289576", "filed": "2020-11-10",
        "document": "d700924dex991.htm", "kind": "nine_month_html",
        "sha256": "ee9d2e600f42cfb4328880d019164387223d9d081382ad56a5cfe0f13c5833fc",
    },
    "2020_fy": {
        "accession": "0001047469-21-000956", "filed": "2021-04-15",
        "document": "afmd-20201231.xml", "kind": "xbrl",
        "sha256": "2728489d3c555eec10761a99e984375703ebbe44d46b147b1db7835447d18f5b",
    },
    "2021_h1": {
        "accession": "0001104659-21-113620", "filed": "2021-09-08",
        "document": "afmd-20210630x6k_htm.xml", "kind": "xbrl",
        "sha256": "5a9154908ab3a82e0b77314256c410fe612ff64de10f73225d8338ad5649f7fa",
    },
}
EXPECTED = {
    "2018-03-31": (532_000.0, -8_203_000.0),
    "2018-06-30": (150_000.0, -8_014_000.0),
    "2018-09-30": (306_000.0, -12_020_000.0),
    "2018-12-31": (22_747_000.0, 8_760_000.0),
    "2019-03-31": (11_353_000.0, 1_852_000.0),
    "2019-06-30": (4_008_000.0, -10_340_000.0),
    "2019-09-30": (2_103_000.0, -10_884_000.0),
    "2019-12-31": (3_927_000.0, -12_993_000.0),
    "2020-03-31": (5_135_000.0, -8_289_000.0),
    "2020-06-30": (2_934_000.0, -12_238_000.0),
    "2020-09-30": (10_545_000.0, -5_966_000.0),
    "2020-12-31": (9_746_000.0, -14_873_000.0),
    "2021-03-31": (11_659_000.0, 1_412_000.0),
    "2021-06-30": (9_707_000.0, -18_752_000.0),
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
    raise RuntimeError(f"failed to fetch AFMD source {_url(spec)}") from error


def _plain_contexts(root) -> dict[str, tuple[str, str]]:
    contexts = {}
    for context in root.xpath('//*[local-name()="context"]'):
        starts = context.xpath('.//*[local-name()="startDate"]/text()')
        ends = context.xpath('.//*[local-name()="endDate"]/text()')
        dimensions = context.xpath('.//*[local-name()="explicitMember"]')
        if starts and ends and not dimensions:
            contexts[context.get("id")] = (starts[0], ends[0])
    return contexts


def parse_xbrl(raw: bytes) -> dict[tuple[str, str, str], float]:
    root = etree.parse(BytesIO(raw)).getroot()
    contexts = _plain_contexts(root)
    facts = {}
    concepts = {"Revenue": "revenue", "ProfitLoss": "net_income"}
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        concept = etree.QName(element).localname
        context = contexts.get(element.get("contextRef"))
        if concept not in concepts or context is None or element.text is None:
            continue
        key = (context[0], context[1], concepts[concept])
        value = float(re.sub(r"[^0-9.-]", "", element.text))
        if key in facts and facts[key] != value:
            raise RuntimeError(f"conflicting AFMD XBRL fact {key}")
        facts[key] = value
    return facts


def parse_nine_month_html(raw: bytes) -> dict[str, float]:
    table = pd.read_html(BytesIO(raw))[0]
    output = {}
    for label, metric in (("Revenue", "revenue"),
                          ("Loss for the period", "net_income")):
        matches = table.loc[table.iloc[:, 0].astype(str).str.strip().eq(label)]
        if len(matches) != 1:
            raise RuntimeError(f"AFMD nine-month filing lacks unique {label}")
        row = matches.iloc[0]
        values = []
        for column in (7, 11, 15, 19):
            text = str(row.iloc[column]).strip()
            number = float(re.sub(r"[^0-9.]", "", text)) * 1000.0
            values.append(-number if text.startswith("(") else number)
        output[f"q3_current_{metric}"] = values[0]
        output[f"q3_comparative_{metric}"] = values[1]
        output[f"nine_month_current_{metric}"] = values[2]
        output[f"nine_month_comparative_{metric}"] = values[3]
    return output


def derive_quarters(parsed: dict[str, dict]) -> dict[str, dict[str, float]]:
    quarters = {}

    def h1(source: str, year: int, comparative_year: int | None = None) -> None:
        facts = parsed[source]
        years = [year] + ([comparative_year] if comparative_year else [])
        for observed_year in years:
            for metric in ("revenue", "net_income"):
                first_half = facts[(
                    f"{observed_year}-01-01", f"{observed_year}-06-30", metric
                )]
                second = facts[(
                    f"{observed_year}-04-01", f"{observed_year}-06-30", metric
                )]
                quarters.setdefault(f"{observed_year}-03-31", {})[metric] = (
                    first_half - second
                )
                quarters.setdefault(f"{observed_year}-06-30", {})[metric] = second

    h1("2019_h1", 2019, 2018)
    h1("2020_h1", 2020)
    h1("2021_h1", 2021)
    for source, year, comparative_year in (
        ("2019_q3", 2019, 2018), ("2020_q3", 2020, None)
    ):
        facts = parsed[source]
        for metric in ("revenue", "net_income"):
            quarters.setdefault(f"{year}-09-30", {})[metric] = (
                facts[f"q3_current_{metric}"]
            )
            if comparative_year:
                quarters.setdefault(f"{comparative_year}-09-30", {})[metric] = (
                    facts[f"q3_comparative_{metric}"]
                )
    for source, year in (("2019_fy", 2019), ("2020_fy", 2020)):
        facts = parsed[source]
        for observed_year in (year, year - 1 if year == 2019 else year):
            for metric in ("revenue", "net_income"):
                annual = facts[(
                    f"{observed_year}-01-01", f"{observed_year}-12-31", metric
                )]
                first_three = sum(
                    quarters[f"{observed_year}-{end}"][metric]
                    for end in ("03-31", "06-30", "09-30")
                )
                quarters.setdefault(f"{observed_year}-12-31", {})[metric] = (
                    annual - first_three
                )
    return dict(sorted(quarters.items()))


def _source_for_quarter(fiscal_end: str) -> str:
    year = int(fiscal_end[:4])
    month = fiscal_end[5:7]
    if month in {"03", "06"}:
        return "2019_h1" if year <= 2019 else f"{year}_h1"
    if month == "09":
        return "2019_q3" if year <= 2019 else f"{year}_q3"
    return "2019_fy" if year <= 2019 else f"{year}_fy"


def recover(output_dir: Path = OUTPUT_DIR) -> dict:
    raw_by_source = {}
    parsed = {}
    source_report = []
    for name, spec in SOURCES.items():
        raw = _fetch(spec)
        digest = hashlib.sha256(raw).hexdigest()
        if digest != spec["sha256"]:
            raise RuntimeError(f"AFMD source changed for {name}: {digest}")
        raw_by_source[name] = raw
        parsed[name] = (
            parse_xbrl(raw) if spec["kind"] == "xbrl"
            else parse_nine_month_html(raw)
        )
        source_report.append({
            "name": name, "accession": spec["accession"],
            "filed": spec["filed"], "url": _url(spec),
            "sha256": digest, "bytes": len(raw),
        })
    quarters = derive_quarters(parsed)
    observed = {
        fiscal_end: (values["revenue"], values["net_income"])
        for fiscal_end, values in quarters.items()
    }
    if observed != EXPECTED:
        raise RuntimeError(f"AFMD recovered quarters changed: {observed}")
    fetched_at = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    rows = []
    for fiscal_end, metrics in quarters.items():
        source_name = _source_for_quarter(fiscal_end)
        spec = SOURCES[source_name]
        for metric, value in metrics.items():
            rows.append({
                "ticker": "AFMD", "fiscal_end": fiscal_end,
                "available_date": spec["filed"], "metric": metric,
                "value": value, "taxonomy": "ifrs-full",
                "concept": f"sec_strict_quarter:{metric}",
                "form": "20-F" if source_name.endswith("fy") else "6-K",
                "accession": spec["accession"], "fetched_at": fetched_at,
            })
    facts = pd.DataFrame(rows, columns=OUTPUT_COLUMNS).sort_values(
        ["available_date", "fiscal_end", "metric"]
    ).reset_index(drop=True)
    if len(facts) != 28 or facts["fiscal_end"].nunique() != 14:
        raise RuntimeError("AFMD recovery must contain 14 paired quarters")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1, "research_only": True,
        "point_in_time_proven": True, "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED", "promotion_eligible": False,
        "formal_financials_modified": False, "ticker": "AFMD", "cik": CIK,
        "accepted_quarter_count": 14, "accepted_fact_count": 28,
        "sources": source_report,
        "outputs": {"strict_quarterly_facts": {
            "path": str(facts_path),
            "sha256": hashlib.sha256(facts_path.read_bytes()).hexdigest(),
        }},
        "guardrail": (
            "Q2 and Q3 are explicit single-quarter facts. Q1 is H1 minus Q2; "
            "Q4 is FY minus the first nine months. Comparative quarters retain "
            "the date of the filing that first proves them. No cumulative "
            "period is divided evenly and no formal financial file is changed."
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
