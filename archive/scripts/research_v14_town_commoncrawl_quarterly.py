#!/usr/bin/env python3
"""Recover TOWN 2019Q3 from its contemporaneously crawled FDIC Form 10-Q."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import pandas as pd
from pypdf import PdfReader


DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/town_commoncrawl_2019q3"
)
INDEX = "CC-MAIN-2019-51"
CAPTURE = {
    "url": (
        "https://investor.townebank.com/Cache/400908980.PDF"
        "?O=PDF&T=&Y=&D=&FID=400908980&iid=4050678"
    ),
    "timestamp": "20191215232734",
    "digest": "6JO6CPJ5SQA4SGT5RLTEXNHUUY7RBDD3",
    "filename": "crawl-data/CC-MAIN-2019-51/segments/1575541310970.85/warc/CC-MAIN-20191215225643-20191216013643-00314.warc.gz",
    "offset": 409415761,
    "length": 613302,
}


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def fetch_warc_record() -> bytes:
    start = int(CAPTURE["offset"])
    end = start + int(CAPTURE["length"]) - 1
    request = Request(
        "https://data.commoncrawl.org/" + str(CAPTURE["filename"]),
        headers={"Range": f"bytes={start}-{end}"},
    )
    with urlopen(request, timeout=60) as response:
        compressed = response.read()
    return gzip.decompress(compressed)


def repair_linearized_pdf(record: bytes) -> bytes:
    start = record.find(b"%PDF-")
    if start < 0:
        raise ValueError("Common Crawl record contains no PDF")
    pdf = record[start:]
    # The WARC capture lacks the optional trailing EOF marker.  Its linearized
    # first-page section is nevertheless complete; align /L with captured size
    # and neutralize the stale hint-table byte range before parsing.
    pdf = re.sub(
        rb"/L (\d+) /H \[ \d+ \d+ \]",
        lambda _: b"/L " + str(len(pdf)).encode() + b" /H [ 0 0 ]",
        pdf,
        count=1,
    )
    return pdf


def parse_town_2019q3(pdf: bytes) -> dict[str, Any]:
    reader = PdfReader(io.BytesIO(pdf))
    first_page = " ".join((reader.pages[0].extract_text() or "").split())
    if not all(value in first_page for value in (
        "FORM 10-Q",
        "quarterly period ended September 30, 2019",
        "FDIC Insurance Cert. Number: 35095",
        "Trading Symbol(s)",
        "TOWN The Nasdaq Global Select Market",
    )):
        raise ValueError("TOWN identity or reporting period is not proven")
    statement = None
    for page in reader.pages:
        text = " ".join((page.extract_text() or "").split())
        if (
            "CONSOLIDATED STATEMENTS OF INCOME" in text
            and "Three Months Ended" in text
            and "September 30" in text
            and "Net interest income" in text
            and "Total noninterest income" in text
            and "Net income $" in text
        ):
            statement = text
            break
    if statement is None:
        raise ValueError("TOWN income statement not found")
    patterns = {
        "net_interest_income": r"Net interest income ([\d,]+) [\d,]+",
        "noninterest_income": r"Total noninterest income ([\d,]+) [\d,]+",
        "net_income": r"Net income \$ ([\d,]+) \$ [\d,]+",
    }
    values = {}
    for metric, pattern in patterns.items():
        match = re.search(pattern, statement)
        if match is None:
            raise ValueError(f"TOWN statement does not prove {metric}")
        values[metric] = float(match.group(1).replace(",", "")) * 1000.0
    return {
        "revenue": values["net_interest_income"] + values["noninterest_income"],
        "net_income": values["net_income"],
        **values,
    }


def run(output_dir: Path = DEFAULT_OUTPUT_DIR) -> dict[str, Any]:
    record = fetch_warc_record()
    pdf = repair_linearized_pdf(record)
    values = parse_town_2019q3(pdf)
    rows = pd.DataFrame([
        {
            "ticker": "TOWN",
            "fiscal_end": "2019-09-30",
            "available_date": "2019-11-08",
            "metric": metric,
            "value": values[metric],
            "concept": concept,
            "taxonomy": "fdic-10q",
            "form": "10-Q",
            "accession": "FDIC-CERT-35095-2019Q3",
            "unit": "USD",
            "source": "commoncrawl_contemporaneous_fdic_form_10q",
            "source_archive": CAPTURE["filename"],
            "source_archive_sha256": _sha256_bytes(record),
            "derivation_prior_accession": "",
        }
        for metric, concept in (
            ("revenue", "NetInterestIncomePlusTotalNoninterestIncome"),
            ("net_income", "NetIncome"),
        )
    ])
    output_dir.mkdir(parents=True, exist_ok=True)
    quarters_path = output_dir / "strict_quarterly_facts.csv"
    rows.to_csv(quarters_path, index=False)
    manifest = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "promotion_eligible": False,
        "release_status": "BLOCKED",
        "ticker": "TOWN",
        "fiscal_end": "2019-09-30",
        "filed_date": "2019-11-08",
        "capture": CAPTURE,
        "warc_record_sha256": _sha256_bytes(record),
        "repaired_pdf_sha256": _sha256_bytes(pdf),
        "values": values,
        "outputs": {
            "quarters": {
                "path": str(quarters_path),
                "sha256": hashlib.sha256(quarters_path.read_bytes()).hexdigest(),
            }
        },
        "guardrail": (
            "Only the quarter explicitly disclosed in the FDIC Form 10-Q "
            "captured by Common Crawl in December 2019 is used. Current FDIC "
            "API values and later annual reports are excluded because they do "
            "not prove historical revision state."
        ),
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    manifest["manifest"] = str(manifest_path)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = run(args.output_dir)
    print(json.dumps({
        "manifest": result["manifest"],
        "point_in_time_proven": True,
        "values": result["values"],
        "promotion_eligible": False,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
