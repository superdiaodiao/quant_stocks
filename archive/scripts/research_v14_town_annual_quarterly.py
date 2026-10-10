#!/usr/bin/env python3
"""Recover TOWN 2018Q1-2021Q4 from dated FDIC Form 10-K reports."""

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
from pypdf import PdfReader

from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from scripts.research_v14_town_commoncrawl_quarterly import (
    CAPTURE,
    fetch_warc_record,
    parse_town_2019q3,
    repair_linearized_pdf,
)
from src.io.fundamentals_update import OUTPUT_COLUMNS


OUTPUT_DIR = Path("output/research_only/v14/town_annual_quarterly")
ANNUAL_REPORT_URL = (
    "https://www.annualreports.com/HostedData/AnnualReportArchive/t/"
    "NASDAQ_TOWN_{year}.pdf"
)
SOURCES = {
    2019: {
        "available_date": "2020-02-28",
        "sha256": "366c4a12ad6415112052f06016a87705c45c2dbbc3b80716350d72a76a54fc9a",
        "note": "NOTE 26: QUARTERLY FINANCIAL DATA (UNAUDITED)",
        "identity": (
            "2019 Annual Report",
            "annual report on Form 10-K of TowneBank",
            "February 28, 2020",
        ),
        "years": (2019, 2018),
    },
    2020: {
        "available_date": "2021-02-26",
        "sha256": "506288795f24de874462ec47785c4dd93799747754db7535a846c35a95a40711",
        "note": "NOTE 27: QUARTERLY FINANCIAL DATA (UNAUDITED)",
        "identity": (
            "FORM 10-K",
            "FDIC Insurance Certificate Number: 35095",
            "TOWN The Nasdaq Global Select Market",
            "February 26, 2021",
        ),
        "years": (2020,),
    },
    2021: {
        "available_date": "2022-02-25",
        "sha256": "da6c0e65d6838bddcb942e8c0d403f5512df12b0e2d420b4a6cf6ac4f9901bb2",
        "note": "NOTE 27. QUARTERLY FINANCIAL DATA (UNAUDITED)",
        "identity": (
            "2021 Annual Report",
            "TowneBank",
            "February 25, 2022",
        ),
        "years": (2021,),
    },
}
EXPECTED = {
    "2018-03-31": (126_277_000.0, 25_944_000.0),
    "2018-06-30": (137_058_000.0, 36_138_000.0),
    "2018-09-30": (137_914_000.0, 39_252_000.0),
    "2018-12-31": (131_417_000.0, 36_440_000.0),
    "2019-03-31": (133_856_000.0, 32_084_000.0),
    "2019-06-30": (144_537_000.0, 36_242_000.0),
    "2019-09-30": (145_879_000.0, 39_400_000.0),
    "2019-12-31": (139_671_000.0, 35_948_000.0),
    "2020-03-31": (137_696_000.0, 27_605_000.0),
    "2020-06-30": (162_656_000.0, 37_222_000.0),
    "2020-09-30": (192_135_000.0, 50_715_000.0),
    "2020-12-31": (171_848_000.0, 53_891_000.0),
    "2021-03-31": (182_509_000.0, 72_631_000.0),
    "2021-06-30": (167_321_000.0, 58_002_000.0),
    "2021-09-30": (170_076_000.0, 52_743_000.0),
    "2021-12-31": (160_424_000.0, 41_657_000.0),
}


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _fetch_annual_report(year: int) -> bytes:
    request = Request(
        ANNUAL_REPORT_URL.format(year=year),
        headers={"User-Agent": "quant_stocks research contact@example.com"},
    )
    error = None
    for attempt in range(3):
        try:
            with urlopen(request, timeout=120) as response:
                return response.read()
        except Exception as exc:  # pragma: no cover - network retry
            error = exc
            if attempt < 2:
                time.sleep(attempt + 1)
    raise RuntimeError(f"failed to fetch TOWN {year} annual report") from error


def _numbers(text: str) -> list[float]:
    tokens = re.findall(r"\(\s*[\d,]+\s*\)|[\d,]+|—", text)
    values = []
    for token in tokens:
        if token == "—":
            values.append(0.0)
            continue
        negative = token.startswith("(")
        number = float(re.sub(r"\D", "", token)) * 1000.0
        values.append(-number if negative else number)
    return values


def _metric_row(block: str, label: str, next_label: str) -> list[float]:
    match = re.search(
        re.escape(label) + r"\s+(.*?)\s+" + re.escape(next_label), block
    )
    if match is None:
        raise ValueError(f"TOWN annual report lacks {label}")
    values = _numbers(match.group(1))
    if len(values) != 4:
        raise ValueError(f"TOWN {label} does not contain four quarters: {values}")
    return values


def parse_quarter_block(block: str) -> dict[str, list[float]]:
    interest_income = _metric_row(block, "Interest income", "Interest expense")
    interest_expense = _metric_row(
        block, "Interest expense", "Provision for"
    )
    noninterest_income = _metric_row(
        block, "Noninterest income", "Net gain on investment securities"
    )
    securities_gain = _metric_row(
        block, "Net gain on investment securities", "Noninterest expense"
    )
    net_income = _metric_row(block, "Net income", "Noncontrolling interest")
    revenue = [
        interest_income[index]
        - interest_expense[index]
        + noninterest_income[index]
        + securities_gain[index]
        for index in range(4)
    ]
    return {"revenue": revenue, "net_income": net_income}


def parse_annual_report(raw: bytes, source_year: int) -> dict[str, dict[str, float]]:
    spec = SOURCES[source_year]
    reader = PdfReader(BytesIO(raw))
    pages = [" ".join((page.extract_text() or "").split()) for page in reader.pages]
    full_text = " ".join(pages)
    if not all(marker in full_text for marker in spec["identity"]):
        raise ValueError(f"TOWN {source_year} report identity/date is not proven")
    note_pages = [page for page in pages if spec["note"] in page]
    if len(note_pages) != 1:
        raise ValueError(f"TOWN {source_year} quarterly note is not unique")
    note = note_pages[0]
    results = {}
    for index, year in enumerate(spec["years"]):
        start = note.find(f"{year} Fourth Third Second First")
        if start < 0:
            raise ValueError(f"TOWN annual report lacks {year} quarterly header")
        later_starts = [
            note.find(f"{other} Fourth Third Second First", start + 1)
            for other in spec["years"][index + 1 :]
        ]
        later_starts = [value for value in later_starts if value >= 0]
        end = min(later_starts) if later_starts else len(note)
        parsed = parse_quarter_block(note[start:end])
        fiscal_ends = (
            f"{year}-12-31", f"{year}-09-30",
            f"{year}-06-30", f"{year}-03-31",
        )
        for quarter_index, fiscal_end in enumerate(fiscal_ends):
            results[fiscal_end] = {
                metric: values[quarter_index]
                for metric, values in parsed.items()
            }
    return results


def _fact_rows(
    facts: dict[str, dict[str, float]], *, available_date: str,
    accession: str, concept_prefix: str, fetched_at: pd.Timestamp,
) -> list[dict]:
    return [
        {
            "ticker": "TOWN",
            "fiscal_end": fiscal_end,
            "available_date": available_date,
            "metric": metric,
            "value": value,
            "taxonomy": "fdic-10k-quarterly-note",
            "concept": f"{concept_prefix}:{metric}",
            "form": "10-K",
            "accession": accession,
            "fetched_at": fetched_at,
        }
        for fiscal_end, metrics in sorted(facts.items())
        for metric, value in metrics.items()
    ]


def recover(output_dir: Path = OUTPUT_DIR) -> dict:
    fetched_at = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    rows = []
    sources = []
    recovered = {}
    for source_year, spec in SOURCES.items():
        raw = _fetch_annual_report(source_year)
        if _sha256(raw) != spec["sha256"]:
            raise RuntimeError(f"TOWN {source_year} annual report SHA changed")
        parsed = parse_annual_report(raw, source_year)
        recovered.update(parsed)
        rows.extend(_fact_rows(
            parsed,
            available_date=spec["available_date"],
            accession=f"FDIC-CERT-35095-{source_year}-10K",
            concept_prefix="fdic_10k_quarterly_note",
            fetched_at=fetched_at,
        ))
        sources.append({
            "source_year": source_year,
            "available_date": spec["available_date"],
            "url": ANNUAL_REPORT_URL.format(year=source_year),
            "sha256": _sha256(raw),
            "bytes": len(raw),
        })
    observed = {
        fiscal_end: (values["revenue"], values["net_income"])
        for fiscal_end, values in recovered.items()
    }
    if observed != EXPECTED:
        raise RuntimeError(f"TOWN annual quarterly values changed: {observed}")

    record = fetch_warc_record()
    pdf = repair_linearized_pdf(record)
    contemporaneous = parse_town_2019q3(pdf)
    if (
        contemporaneous["revenue"], contemporaneous["net_income"]
    ) != EXPECTED["2019-09-30"]:
        raise RuntimeError("TOWN contemporaneous 2019Q3 conflicts with 10-K")
    rows.extend(_fact_rows(
        {"2019-09-30": {
            "revenue": contemporaneous["revenue"],
            "net_income": contemporaneous["net_income"],
        }},
        available_date="2019-11-08",
        accession="FDIC-CERT-35095-2019Q3",
        concept_prefix="fdic_10q",
        fetched_at=fetched_at,
    ))
    sources.append({
        "source_year": "2019Q3",
        "available_date": "2019-11-08",
        "url": CAPTURE["url"],
        "commoncrawl_capture": CAPTURE,
        "warc_record_sha256": _sha256(record),
        "repaired_pdf_sha256": _sha256(pdf),
    })

    facts = pd.DataFrame(rows, columns=OUTPUT_COLUMNS).sort_values(
        ["available_date", "fiscal_end", "metric"]
    ).reset_index(drop=True)
    if len(facts) != 34 or facts["fiscal_end"].nunique() != 16:
        raise RuntimeError("TOWN recovery must contain 16 annual-note quarters plus Q3 PIT")
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
        "ticker": "TOWN",
        "fdic_certificate": 35095,
        "accepted_fiscal_quarter_count": 16,
        "accepted_fact_observation_count": 34,
        "sources": sources,
        "outputs": {"strict_quarterly_facts": {
            "path": str(facts_path),
            "sha256": hashlib.sha256(facts_path.read_bytes()).hexdigest(),
        }},
        "guardrail": (
            "The 2019Q3 values retain their contemporaneous FDIC 10-Q date. "
            "All other quarterly-note values become available only on the "
            "dated annual report that disclosed them; they are never "
            "backdated to the original quarter. Current FDIC API revisions "
            "and formal financial files remain untouched."
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
