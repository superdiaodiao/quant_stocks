#!/usr/bin/env python3
"""Recover EPIX FY2020 quarters across its foreign-to-domestic transition."""

from __future__ import annotations

import argparse
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from io import BytesIO
import hashlib
import json
from pathlib import Path
import re
from urllib.request import Request, urlopen
import warnings

import pandas as pd

from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.io.fundamentals_update import OUTPUT_COLUMNS


OUTPUT_DIR = Path("output/research_only/v14/epix_transition_quarters")
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
SOURCES = {
    "2020_q2": {
        "accession": "0001279569-20-000703",
        "filed": "2020-05-07",
        "document": "ex992.htm",
    },
    "2020_q3": {
        "accession": "0001279569-20-001172",
        "filed": "2020-08-07",
        "document": "ex992.htm",
    },
    "2020_fy": {
        "accession": "0001558370-20-014403",
        "filed": "2020-12-15",
        "document": "tmb-20200930x10k.htm",
    },
    "2021_q1": {
        "accession": "0001558370-21-000895",
        "filed": "2021-02-11",
        "document": "tmb-20201231x10q.htm",
    },
}
EXPECTED = {
    "2020-03-31": -9_356_174.0,
    "2020-06-30": -4_932_696.0,
    "2020-09-30": -4_534_289.0,
    "2020-12-31": -6_528_704.0,
}


def _url(spec: dict) -> str:
    accession = spec["accession"].replace("-", "")
    return (
        "https://www.sec.gov/Archives/edgar/data/1633932/"
        f"{accession}/{spec['document']}"
    )


def _fetch(spec: dict) -> bytes:
    with urlopen(Request(_url(spec), headers=SEC_HEADERS), timeout=120) as response:
        return response.read()


def _normalize(value) -> str:
    return " ".join(str(value).replace("\xa0", " ").split()).casefold()


def _number(value) -> float | None:
    text = str(value).strip()
    if not text or text.lower() == "nan" or text in {"$", "—", "-", ")"}:
        return None
    cleaned = re.sub(r"[^0-9.]", "", text)
    if not cleaned:
        return None
    result = float(cleaned)
    return -result if "(" in text else result


def _table_value(
    raw: bytes,
    *,
    row_label: str,
    period_phrase: str,
    year: str,
) -> float:
    candidates = set()
    for table in pd.read_html(BytesIO(raw)):
        first = table.iloc[:, 0].map(_normalize)
        rows = list(table.index[first.eq(row_label.casefold())])
        if len(rows) != 1:
            continue
        for column in table.columns:
            header = " ".join(
                [_normalize(column), *map(_normalize, table[column].iloc[:4])]
            )
            if period_phrase.casefold() not in header or year not in header:
                continue
            value = _number(table.loc[rows[0], column])
            if value is not None:
                candidates.add(value)
    if len(candidates) != 1:
        raise RuntimeError(
            f"expected one {row_label} value for {period_phrase} {year}, "
            f"found {sorted(candidates)}"
        )
    return candidates.pop()


def _inline_xbrl_net_loss(raw: bytes, context_fragment: str) -> float:
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
    soup = BeautifulSoup(raw, "lxml")
    candidates = set()
    for tag in soup.find_all(
        lambda item: item.name
        and item.name.casefold().endswith("nonfraction")
        and str(item.get("name", "")).casefold() == "us-gaap:netincomeloss"
        and context_fragment.casefold()
        in str(item.get("contextref", "")).casefold()
    ):
        value = _number(tag.get_text(" ", strip=True))
        if value is None:
            continue
        scale = int(tag.get("scale", "0"))
        value *= 10**scale
        if tag.get("sign") == "-":
            value = -abs(value)
        candidates.add(value)
    if len(candidates) != 1:
        raise RuntimeError(
            f"expected one inline XBRL net loss for {context_fragment}, "
            f"found {sorted(candidates)}"
        )
    return candidates.pop()


def recover(output_dir: Path = OUTPUT_DIR) -> dict:
    raw = {name: _fetch(spec) for name, spec in SOURCES.items()}
    q2 = _table_value(
        raw["2020_q2"], row_label="Net loss and comprehensive loss for the period",
        period_phrase="Three months ended March 31", year="2020",
    )
    q3 = _table_value(
        raw["2020_q3"], row_label="Net loss and comprehensive loss for the period",
        period_phrase="Three months ended June 30", year="2020",
    )
    nine_months = _table_value(
        raw["2020_q3"], row_label="Net loss and comprehensive loss for the period",
        period_phrase="Nine months ended June 30", year="2020",
    )
    fiscal_year = _table_value(
        raw["2020_fy"], row_label="Net loss, net of income tax",
        period_phrase="Year Ended", year="2020",
    )
    q4 = fiscal_year - nine_months
    q1 = _inline_xbrl_net_loss(
        raw["2021_q1"], "Duration_10_1_2020_To_12_31_2020"
    )
    values = {
        "2020-03-31": q2,
        "2020-06-30": q3,
        "2020-09-30": q4,
        "2020-12-31": q1,
    }
    if values != EXPECTED:
        raise RuntimeError(f"EPIX source values changed: {values}")

    available_dates = {
        "2020-03-31": SOURCES["2020_q2"]["filed"],
        "2020-06-30": SOURCES["2020_q3"]["filed"],
        "2020-09-30": SOURCES["2020_fy"]["filed"],
        "2020-12-31": SOURCES["2021_q1"]["filed"],
    }
    accessions = {
        "2020-03-31": SOURCES["2020_q2"]["accession"],
        "2020-06-30": SOURCES["2020_q3"]["accession"],
        "2020-09-30": (
            SOURCES["2020_fy"]["accession"] + "+" +
            SOURCES["2020_q3"]["accession"]
        ),
        "2020-12-31": SOURCES["2021_q1"]["accession"],
    }
    fetched_at = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    rows = [{
        "ticker": "EPIX",
        "fiscal_end": fiscal_end,
        "available_date": available_dates[fiscal_end],
        "metric": "net_income",
        "value": value,
        "taxonomy": "ifrs-full" if fiscal_end < "2020-09-30" else "us-gaap",
        "concept": (
            "sec_6k_plain_html:ProfitLoss"
            if fiscal_end < "2020-09-30"
            else "sec_transition:NetIncomeLoss"
        ),
        "form": (
            "6-K:EX-99.2:THREE_MONTHS"
            if fiscal_end < "2020-09-30"
            else "10-K_MINUS_6-K_9M" if fiscal_end == "2020-09-30"
            else "10-Q:INLINE_XBRL"
        ),
        "accession": accessions[fiscal_end],
        "fetched_at": fetched_at,
    } for fiscal_end, value in values.items()]
    facts = pd.DataFrame(rows, columns=OUTPUT_COLUMNS).sort_values("fiscal_end")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    sources = [{
        "name": name,
        "accession": spec["accession"],
        "filed": spec["filed"],
        "url": _url(spec),
        "sha256": hashlib.sha256(raw[name]).hexdigest(),
        "bytes": len(raw[name]),
    } for name, spec in SOURCES.items()]
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "ticker": "EPIX",
        "cik": 1_633_932,
        "accepted_quarter_count": 4,
        "accepted_fact_count": 4,
        "sources": sources,
        "outputs": {"strict_quarterly_facts": {
            "path": str(facts_path),
            "sha256": hashlib.sha256(facts_path.read_bytes()).hexdigest(),
        }},
        "guardrail": (
            "Uses contemporaneous three-month net losses for March and June, "
            "derives September only as FY minus the contemporaneous nine-month "
            "statement, and reads December from the filed inline XBRL. No "
            "revenue is invented for this pre-revenue issuer."
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
