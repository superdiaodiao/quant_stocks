#!/usr/bin/env python3
"""Recover strict SBLK 2019Q1-2021Q4 PIT quarterly facts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd
import pdfplumber
from bs4 import BeautifulSoup


DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/sblk_quarterly_reports_2019q1_2021q4"
)
DEFAULT_Q3_PDF = Path("tmp/pdfs/sblk_q3_2021/sblk_q3_2021.pdf")
DEFAULT_Q4_2019_PDF = Path("tmp/pdfs/sblk_q4_2019/sblk_q4_2019.pdf")
DEFAULT_2019_PDF_DIR = Path("tmp/pdfs/sblk_2019_quarters")
DEFAULT_2020_PDF_DIR = Path("tmp/pdfs/sblk_2020_quarters")
HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
Q3_PDF_SHA256 = "84b39282fe8cf7bd12cff9877937f55690a9846e4c5c2c5d0242ba8c2c215831"
Q4_2019_PDF_SHA256 = "b86c6dd13357ac4dd3a75b3eb574fe33cbabc8a28fc225d9d470bcc1d3cff75a"

SOURCES = {
    "q1_2020_ir_pdf": {
        "kind": "official_ir_pdf", "available_date": "2020-05-26",
        "form": "IR-PRESS-RELEASE", "accession": "",
        "document": "sblk_q1_2020.pdf",
        "url": "https://www.starbulk.com/media/uploads_file/2020/05/27/p1e99dnvlp1ssj10ao1m1u14hc1u3n4.pdf",
        "expected_values": (160_862, 166_490, 2_755, -5_342),
        "expected_sha256": "33757f55516dc92311619393e7b66cc6a81afe16f3cc7d1bff3fd47d765ccf62",
        "expected_pages": 17,
        "validation_patterns": (
            r"May 26, 2020", r"Voyage Revenues \$160,862 \$166,490",
            r"Net income/\(loss\) \$2,755 \(\$5,342\)",
            r"Adjusted Net income\s*/\s*\(loss\).*?\(\$22,174\)",
        ),
    },
    "q2_2020_ir_pdf": {
        "kind": "official_ir_pdf", "available_date": "2020-08-05",
        "form": "IR-PRESS-RELEASE", "accession": "",
        "document": "sblk_q2_2020.pdf",
        "url": "https://www.starbulk.com/media/uploads_file/2020/08/06/p1ef07udhsm9plj31v1o12sqgh24.pdf",
        "expected_values": (146_134, 157_792, 306_996, 324_282,
                            -44_120, -40_173, -41_365, -45_515),
        "expected_sha256": "818f4a958eb3e6960b96aeb8c3d251b5fbe09d87a0cfc87e83d0342ba5c11e63",
        "expected_pages": 20,
        "validation_patterns": (
            r"August 5, 2020",
            r"Voyage Revenues \$146,134 \$157,792 \$306,996 \$324,282",
            r"Net income/\(loss\) \(\$44,120\) \(\$40,173\) \(\$41,365\) \(\$45,515\)",
            r"Adjusted Net income\s*/\s*\(loss\).*?\(\$18,131\)",
        ),
    },
    "q3_2020_ir_pdf": {
        "kind": "official_ir_pdf", "available_date": "2020-11-16",
        "form": "IR-PRESS-RELEASE", "accession": "",
        "document": "sblk_q3_2020.pdf",
        "url": "https://www.starbulk.com/media/uploads_file/2020/11/17/p1en9mdodoh3k7b81h2g1rqu9gk4.pdf",
        "expected_values": (200_222, 248_444, 507_218, 572_726,
                            23_251, 5_815, -18_114, -39_700),
        "expected_sha256": "17920f0157b22f9cfb6f3a9e9056fe72b8570841d73c424205ae40ca08a4b346",
        "expected_pages": 12,
        "validation_patterns": (
            r"November 16, 2020",
            r"Voyage Revenues \$200,222 \$248,444 \$507,218 \$572,726",
            r"Net income/\(loss\) \$23,251 \$5,815 \(\$18,114\) \(\$39,700\)",
            r"Adjusted Net income\s*/\s*\(loss\).*?\$27,339",
        ),
    },
    "q1_2019_ir_pdf": {
        "kind": "official_ir_pdf", "available_date": "2019-05-22",
        "form": "IR-PRESS-RELEASE", "accession": "",
        "document": "sblk_q1_2019.pdf",
        "url": "https://www.starbulk.com/media/uploads_file/2019/05/23/p1dbgmbtlok931hr6ekp1eu0neo4.pdf",
        "expected_values": (166_490, 121_057, -5_342, 9_900),
        "expected_sha256": "a920aa3201b4c94b53eb7074a57dac0eae12d09d187cf192a5a11475d6409232",
        "expected_pages": 15,
        "validation_patterns": (
            r"May 22, 2019", r"Voyage Revenues \$166,490 \$121,057",
            r"Net income/\(loss\) \(\$5,342\) \$9,900",
            r"Adjusted Net income\s*/\s*\(loss\).*?\(\$8,532\)",
        ),
    },
    "q2_2019_ir_pdf": {
        "kind": "official_ir_pdf", "available_date": "2019-08-07",
        "form": "IR-PRESS-RELEASE", "accession": "",
        "document": "sblk_q2_2019.pdf",
        "url": "https://www.starbulk.com/media/uploads_file/2019/08/08/p1dhn02272lbp1f71r9h1ht01suv4.pdf",
        "expected_values": (157_792, 132_604, 324_282, 253_661,
                            -40_173, 10_728, -45_515, 20_628),
        "expected_sha256": "066d7ad07e1034a12742dae5344cbced9c7c8b04d1c1d48a77487be286cf8c09",
        "expected_pages": 18,
        "validation_patterns": (
            r"August 7, 2019",
            r"Voyage Revenues \$157,792 \$132,604 \$324,282 \$253,661",
            r"Net income/\(loss\) \(\$40,173\) \$10,728 \(\$45,515\) \$20,628",
            r"Adjusted Net income\s*/\s*\(loss\).*?\(\$20,520\)",
        ),
    },
    "q3_2019_ir_pdf": {
        "kind": "official_ir_pdf", "available_date": "2019-11-20",
        "form": "IR-PRESS-RELEASE", "accession": "",
        "document": "sblk_q3_2019.pdf",
        "url": "https://www.starbulk.com/media/uploads_file/2019/11/21/p1dq5di9nv40q18do1unl1dvb1t4h4.pdf",
        "expected_values": (248_444, 188_467, 572_726, 442_128,
                            5_815, 26_054, -39_700, 46_682),
        "expected_sha256": "c5412c30da014c2aef5c1e24f9d8bc3d7d4260c0936bc854205acbadcb2f75ca",
        "expected_pages": 21,
        "validation_patterns": (
            r"November 20, 2019",
            r"Voyage Revenues \$248,444 \$188,467 \$572,726 \$442,128",
            r"Net income/\(loss\) \$5,815 \$26,054 \(\$39,700\) \$46,682",
            r"Adjusted Net income\s*/\s*\(loss\).*?\$17,266",
        ),
    },
    "q4_2019_ir_pdf": {
        "kind": "official_ir_pdf",
        "available_date": "2020-02-19",
        "form": "IR-PRESS-RELEASE",
        "accession": "",
        "document": "sblk_q4_2019.pdf",
        "url": (
            "https://www.starbulk.com/media/uploads_file/2020/02/20/"
            "p1e1fq5t3s1pun1jha1a739o11inp4.pdf"
        ),
        "expected_values": (248_639, 209_433, 821_365, 651_561,
                            23_499, 11_715, -16_201, 58_397),
        "expected_sha256": Q4_2019_PDF_SHA256,
        "expected_pages": 19,
    },
    "q1_2021_sec": {
        "kind": "sec_html",
        "available_date": "2021-05-24",
        "form": "6-K",
        "accession": "0000919574-21-003838",
        "document": "d8857058_ex99-1.htm",
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1386716/"
            "000091957421003838/d8857058_ex99-1.htm"
        ),
        "expected_values": (160_862, 200_467, 2_755, 35_763),
    },
    "h1_2021_sec": {
        "kind": "sec_html",
        "available_date": "2021-08-06",
        "form": "6-K",
        "accession": "0000919574-21-004842",
        "document": "sblk-20210630.htm",
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1386716/"
            "000091957421004842/sblk-20210630.htm"
        ),
        "expected_values": (306_996, 511_878, -41_365, 159_972),
    },
    "q4_2020_sec": {
        "kind": "sec_html",
        "available_date": "2021-02-17",
        "form": "6-K",
        "accession": "0000919574-21-001771",
        "document": "d8806013_6-k.htm",
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1386716/"
            "000091957421001771/d8806013_6-k.htm"
        ),
        "expected_values": (186_023, 27_774),
    },
    "q3_2021_ir_pdf": {
        "kind": "official_ir_pdf",
        "available_date": "2021-11-16",
        "form": "IR-PRESS-RELEASE",
        "accession": "",
        "document": "sblk_q3_2021.pdf",
        "url": (
            "https://www.starbulk.com/media/uploads_file/2021/11/17/"
            "p1fkldjbp7scbvp2qjj1v1g1jso4.pdf"
        ),
        "expected_values": (
            415_688, 200_222, 927_566, 507_218,
            220_407, 23_251, 380_379, -18_114,
        ),
        "expected_sha256": Q3_PDF_SHA256,
        "expected_pages": 13,
    },
    "fy_2021_sec": {
        "kind": "sec_html",
        "available_date": "2022-03-15",
        "form": "20-F",
        "accession": "0000919574-22-002290",
        "document": "sblk-20211231.htm",
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1386716/"
            "000091957422002290/sblk-20211231.htm"
        ),
        "expected_values": (1_427_423, 680_530),
    },
}

# Source values are in thousands of USD. A prior tuple is subtracted only
# where both inputs have the same consolidated GAAP scope and cumulative span.
PERIOD_EVIDENCE = {
    "2019-03-31": {
        "available_date": "2019-05-22", "revenue": 166_490, "profit": -5_342,
        "derivation": "direct_official_three_month_gaap_statement",
        "current": ("q1_2019_ir_pdf", 166_490, -5_342), "prior": None,
    },
    "2019-06-30": {
        "available_date": "2019-08-07", "revenue": 157_792, "profit": -40_173,
        "derivation": "direct_official_three_month_gaap_statement",
        "current": ("q2_2019_ir_pdf", 157_792, -40_173), "prior": None,
    },
    "2019-09-30": {
        "available_date": "2019-11-20", "revenue": 248_444, "profit": 5_815,
        "derivation": "direct_official_three_month_gaap_statement",
        "current": ("q3_2019_ir_pdf", 248_444, 5_815), "prior": None,
    },
    "2019-12-31": {
        "available_date": "2020-02-19",
        "revenue": 248_639, "profit": 23_499,
        "derivation": "direct_official_three_month_gaap_statement",
        "current": ("q4_2019_ir_pdf", 248_639, 23_499), "prior": None,
    },
    "2020-03-31": {
        "available_date": "2020-05-26",
        "revenue": 160_862, "profit": 2_755,
        "derivation": "direct_official_three_month_gaap_statement",
        "current": ("q1_2020_ir_pdf", 160_862, 2_755), "prior": None,
    },
    "2020-06-30": {
        "available_date": "2020-08-05",
        "revenue": 146_134, "profit": -44_120,
        "derivation": "direct_official_three_month_gaap_statement",
        "current": ("q2_2020_ir_pdf", 146_134, -44_120), "prior": None,
    },
    "2020-09-30": {
        "available_date": "2020-11-16",
        "revenue": 200_222, "profit": 23_251,
        "derivation": "direct_official_three_month_gaap_statement",
        "current": ("q3_2020_ir_pdf", 200_222, 23_251), "prior": None,
    },
    "2020-12-31": {
        "available_date": "2021-02-17",
        "revenue": 186_023, "profit": 27_774,
        "derivation": "direct_official_three_month_results_statement",
        "current": ("q4_2020_sec", 186_023, 27_774), "prior": None,
    },
    "2021-03-31": {
        "available_date": "2021-05-24",
        "revenue": 200_467, "profit": 35_763,
        "derivation": "direct_sec_three_month_statement",
        "current": ("q1_2021_sec", 200_467, 35_763), "prior": None,
    },
    "2021-06-30": {
        "available_date": "2021-08-06",
        "revenue": 311_411, "profit": 124_209,
        "derivation": "contemporaneous_h1_minus_q1",
        "current": ("h1_2021_sec", 511_878, 159_972),
        "prior": ("q1_2021_sec", 200_467, 35_763),
    },
    "2021-09-30": {
        "available_date": "2021-11-16",
        "revenue": 415_688, "profit": 220_407,
        "derivation": "direct_official_three_month_gaap_statement",
        "current": ("q3_2021_ir_pdf", 415_688, 220_407), "prior": None,
    },
    "2021-12-31": {
        "available_date": "2022-03-15",
        "revenue": 499_857, "profit": 300_151,
        "derivation": "contemporaneous_fy_minus_official_9m",
        "current": ("fy_2021_sec", 1_427_423, 680_530),
        "prior": ("q3_2021_ir_pdf", 927_566, 380_379),
    },
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_html(raw: bytes) -> str:
    return " ".join(BeautifulSoup(raw, "html.parser").get_text(" ").split())


def _value_tokens(value: int) -> set[str]:
    absolute = abs(value)
    unsigned = {str(absolute), f"{absolute:,}"}
    if value >= 0:
        return unsigned
    return unsigned | {
        f"-{absolute}", f"-{absolute:,}",
        f"({absolute})", f"({absolute:,})",
        f"( {absolute} )", f"( {absolute:,} )",
    }


def validate_sec_html(raw: bytes, *, expected_values: tuple[int, ...]) -> None:
    text = _normalized_html(raw)
    if not re.search(r"Star Bulk Carriers Corp", text, re.I):
        raise ValueError("SBLK issuer identity is not proven")
    if not re.search(r"(?:thousands of )?U\.S\. [Dd]ollars|USD", text):
        raise ValueError("SBLK USD reporting currency is not proven")
    if not re.search(r"Voyage [Rr]evenues", text):
        raise ValueError("SBLK voyage revenue row is not proven")
    if not re.search(r"Net income\s*/?\s*\(?loss\)?", text, re.I):
        raise ValueError("SBLK GAAP net income row is not proven")
    for value in expected_values:
        if not any(token in text for token in _value_tokens(value)):
            raise ValueError(f"SBLK filing does not prove expected value {value}")


def validate_q3_pdf_text(text: str) -> None:
    normalized = " ".join(text.split())
    required = (
        r"Star Bulk Carriers Corp", r"November 16, 2021",
        r"Expressed in thousands of U\.S\. dollars",
        r"Voyage Revenues \$415,688 \$200,222 \$927,566 \$507,218",
        r"Net income/\(loss\) \$220,407 \$23,251 \$380,379 \(\$18,114\)",
    )
    for pattern in required:
        if not re.search(pattern, normalized, re.I):
            raise ValueError(f"SBLK Q3 PDF evidence is not proven: {pattern}")
    if not re.search(
        r"Adjusted Net income\s*/\s*\(loss\).*?\$224,671", normalized, re.I
    ):
        raise ValueError("SBLK adjusted-net-income separation is not proven")


def validate_q4_2019_pdf_text(text: str) -> None:
    normalized = " ".join(text.split())
    required = (
        r"Star Bulk Carriers Corp", r"February 19, 2020",
        r"Expressed in thousands of U\.S\. dollars",
        r"Voyage Revenues \$248,639 \$209,433 \$821,365 \$651,561",
        r"Net income/\(loss\) \$23,499 \$11,715 \(\$16,201\) \$58,397",
    )
    for pattern in required:
        if not re.search(pattern, normalized, re.I):
            raise ValueError(f"SBLK 2019Q4 PDF evidence is not proven: {pattern}")
    if not re.search(
        r"Adjusted Net income\s*/\s*\(loss\).*?\$34,500", normalized, re.I
    ):
        raise ValueError("SBLK 2019Q4 adjusted-net-income separation is not proven")


def validate_2019_quarter_pdf_text(text: str, patterns: tuple[str, ...]) -> None:
    normalized = " ".join(text.split())
    common = (r"Star Bulk Carriers Corp", r"Expressed in thousands of U\.S\. dollars")
    for pattern in (*common, *patterns):
        if not re.search(pattern, normalized, re.I):
            raise ValueError(f"SBLK 2019 quarter PDF evidence is not proven: {pattern}")


def extract_official_pdf_text(path: Path, *, expected_pages: int) -> str:
    with pdfplumber.open(path) as document:
        if len(document.pages) != expected_pages:
            raise ValueError("SBLK official PDF page count changed")
        return document.pages[0].extract_text() or ""


def _download(url: str, path: Path) -> None:
    with urlopen(Request(url, headers=HEADERS), timeout=120) as response:
        path.write_bytes(response.read())


def _row(
    *, fiscal_end: str, available_date: str, metric: str, value: int,
    source_id: str, source: dict, archive: str, archive_sha256: str,
    derivation: str, prior_source_id: str, prior_source_sha256: str,
) -> dict:
    return {
        "ticker": "SBLK", "fiscal_end": fiscal_end,
        "available_date": available_date, "metric": metric,
        "value": float(value * 1_000), "taxonomy": "us-gaap",
        "concept": "VoyageRevenues" if metric == "revenue" else "NetIncomeLoss",
        "form": source["form"], "accession": source["accession"], "unit": "USD",
        "source": "sblk_contemporaneous_gaap_reporting_chain",
        "source_id": source_id, "source_archive": archive,
        "source_archive_sha256": archive_sha256,
        "derivation": derivation,
        "derivation_prior_source_id": prior_source_id,
        "derivation_prior_source_sha256": prior_source_sha256,
    }


def run(
    *, output_dir: Path = DEFAULT_OUTPUT_DIR,
    q3_pdf_path: Path = DEFAULT_Q3_PDF,
    q4_2019_pdf_path: Path = DEFAULT_Q4_2019_PDF,
    pdf_2019_dir: Path = DEFAULT_2019_PDF_DIR,
    pdf_2020_dir: Path = DEFAULT_2020_PDF_DIR,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(exist_ok=True)
    sources = {}
    local_pdf_paths = {
        "q3_2021_ir_pdf": q3_pdf_path,
        "q4_2019_ir_pdf": q4_2019_pdf_path,
        "q1_2019_ir_pdf": pdf_2019_dir / "sblk_q1_2019.pdf",
        "q2_2019_ir_pdf": pdf_2019_dir / "sblk_q2_2019.pdf",
        "q3_2019_ir_pdf": pdf_2019_dir / "sblk_q3_2019.pdf",
        "q1_2020_ir_pdf": pdf_2020_dir / "sblk_q1_2020.pdf",
        "q2_2020_ir_pdf": pdf_2020_dir / "sblk_q2_2020.pdf",
        "q3_2020_ir_pdf": pdf_2020_dir / "sblk_q3_2020.pdf",
    }
    for source_id, spec in SOURCES.items():
        suffix = ".pdf" if spec["kind"] == "official_ir_pdf" else ".htm"
        path = raw_dir / f"{source_id}{suffix}"
        if not path.exists():
            local_pdf = local_pdf_paths.get(source_id)
            if local_pdf is not None and local_pdf.exists():
                shutil.copy2(local_pdf, path)
            else:
                _download(spec["url"], path)
        sha = _sha256(path)
        if spec["kind"] == "official_ir_pdf":
            if sha != spec["expected_sha256"]:
                raise ValueError(f"SBLK {source_id} official PDF SHA256 changed")
            pdf_text = extract_official_pdf_text(
                path, expected_pages=spec["expected_pages"]
            )
            if source_id == "q3_2021_ir_pdf":
                validate_q3_pdf_text(pdf_text)
            elif source_id == "q4_2019_ir_pdf":
                validate_q4_2019_pdf_text(pdf_text)
            else:
                validate_2019_quarter_pdf_text(
                    pdf_text, spec["validation_patterns"]
                )
        else:
            validate_sec_html(path.read_bytes(), expected_values=spec["expected_values"])
        sources[source_id] = {
            "source_id": source_id, "kind": spec["kind"],
            "available_date": spec["available_date"], "form": spec["form"],
            "accession": spec["accession"], "document": spec["document"],
            "url": spec["url"], "path": str(path), "sha256": sha,
        }

    rows = []
    recovered = []
    for fiscal_end, item in PERIOD_EVIDENCE.items():
        current = item["current"]
        prior = item["prior"]
        if item["available_date"] != SOURCES[current[0]]["available_date"]:
            raise RuntimeError(f"SBLK current-source PIT date mismatch for {fiscal_end}")
        if prior is not None:
            if current[1] - prior[1] != item["revenue"]:
                raise RuntimeError(f"SBLK revenue derivation mismatch for {fiscal_end}")
            if current[2] - prior[2] != item["profit"]:
                raise RuntimeError(f"SBLK profit derivation mismatch for {fiscal_end}")
        current_source = sources[current[0]]
        prior_source = sources[prior[0]] if prior is not None else None
        common = {
            "fiscal_end": fiscal_end,
            "available_date": item["available_date"],
            "source_id": current[0], "source": SOURCES[current[0]],
            "archive": Path(current_source["path"]).name,
            "archive_sha256": current_source["sha256"],
            "derivation": item["derivation"],
            "prior_source_id": prior[0] if prior is not None else "",
            "prior_source_sha256": prior_source["sha256"] if prior_source else "",
        }
        rows.extend([
            _row(metric="revenue", value=item["revenue"], **common),
            _row(metric="net_income", value=item["profit"], **common),
        ])
        recovered.append({
            "ticker": "SBLK", "fiscal_end": fiscal_end,
            "available_date": item["available_date"],
            "revenue": float(item["revenue"] * 1_000),
            "net_income": float(item["profit"] * 1_000),
            "derivation": item["derivation"],
        })

    facts = pd.DataFrame(rows).sort_values(["fiscal_end", "metric"])
    if len(facts) != 24 or facts["fiscal_end"].nunique() != 12:
        raise RuntimeError("SBLK recovery is not exactly twelve paired quarters")
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1, "research_only": True,
        "point_in_time_proven": True, "parameters_frozen": False,
        "promotion_eligible": False, "release_status": "BLOCKED",
        "ticker": "SBLK", "accepted_quarter_count": 12,
        "recovered_quarters": recovered,
        "filing_sources": list(sources.values()),
        "outputs": {"quarters": {"path": str(facts_path),
                                  "sha256": _sha256(facts_path)}},
        "guardrail": (
            "Only consolidated Voyage revenues and GAAP Net income/(loss) in "
            "USD are accepted. Q2 and Q4 use exact same-scope cumulative "
            "differences. Later comparative quarters retain their first proven "
            "publication dates and are never backdated. Adjusted net income, "
            "TCE revenue, forecasts, and annual allocations are rejected."
        ),
    }
    manifest = output_dir / "manifest.json"
    manifest.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["manifest"] = str(manifest)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--q3-pdf", type=Path, default=DEFAULT_Q3_PDF)
    parser.add_argument("--q4-2019-pdf", type=Path, default=DEFAULT_Q4_2019_PDF)
    parser.add_argument("--pdf-2019-dir", type=Path, default=DEFAULT_2019_PDF_DIR)
    parser.add_argument("--pdf-2020-dir", type=Path, default=DEFAULT_2020_PDF_DIR)
    args = parser.parse_args()
    result = run(
        output_dir=args.output_dir,
        q3_pdf_path=args.q3_pdf,
        q4_2019_pdf_path=args.q4_2019_pdf,
        pdf_2019_dir=args.pdf_2019_dir,
        pdf_2020_dir=args.pdf_2020_dir,
    )
    print(json.dumps({
        "accepted_quarter_count": result["accepted_quarter_count"],
        "manifest": result["manifest"],
        "release_status": result["release_status"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
