#!/usr/bin/env python3
"""Build source-locked, research-only exact TTM growth packages for AUDC.

AudioCodes reports under U.S. GAAP in U.S. dollars.  Five complete TTM pairs
are reconstructed from audited 20-F annual values and reported 6-K cumulative
periods.  No standalone fiscal quarter, FX conversion, estimate, or post-signal
value is manufactured.
"""

from __future__ import annotations

import argparse
from bs4 import BeautifulSoup
from datetime import date
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
from typing import Iterable
from urllib.request import Request, urlopen

import pandas as pd

from src.io.fundamentals_update import OUTPUT_COLUMNS


OUTPUT_DIR = Path("output/research_only/v14/audc_exact_ttm_growth")
TICKER = "AUDC"
CIK = 1_086_434
CURRENCY = "USD"
SOURCE_SCALE = 1_000
ACCOUNTING_STANDARD = "US-GAAP"
PIT_CUTOFF = "2021-11-30"
FETCHED_AT = "2026-08-24"
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}


SOURCE_DOCUMENTS = {
    "20f_fy2018": {
        "form": "20-F",
        "filed": "2019-03-19",
        "accepted_at": "2019-03-19T15:30:07Z",
        "accession": "0001144204-19-014779",
        "document": "tv513243_20f.htm",
        "local_path": "sources/tv513243_20f.htm",
        "expected_sha256": "1de0f7208ee98dbde5e5ab8f8f3d4d3452a3d3603dc5b5e4581fdafb6bfb7844",
        "url": "https://www.sec.gov/Archives/edgar/data/1086434/000114420419014779/tv513243_20f.htm",
    },
    "20f_fy2019": {
        "form": "20-F",
        "filed": "2020-02-25",
        "accepted_at": "2020-02-25T12:26:56Z",
        "accession": "0001104659-20-024465",
        "document": "tm206469-1_20f.htm",
        "local_path": "sources/tm206469-1_20f.htm",
        "expected_sha256": "67d2058e5b7c3d6e81432bf0055b27d81bacecbbd1d0d00b2ed35f51d21ac2c9",
        "url": "https://www.sec.gov/Archives/edgar/data/1086434/000110465920024465/tm206469-1_20f.htm",
    },
    "20f_fy2020": {
        "form": "20-F",
        "filed": "2021-04-27",
        "accepted_at": "2021-04-27T10:05:26Z",
        "accession": "0001104659-21-055099",
        "document": "audc-20201231x20f.htm",
        "local_path": "sources/audc-20201231x20f.htm",
        "expected_sha256": "0a3a8c321f22e86aa1f906b61461744aa7ff068729fa73e588521095f8260297",
        "url": "https://www.sec.gov/Archives/edgar/data/1086434/000110465921055099/audc-20201231x20f.htm",
    },
    "6k_q2_2018": {
        "form": "6-K Exhibit 99.1",
        "filed": "2018-07-24",
        "accepted_at": "2018-07-24T11:38:35Z",
        "accession": "0001144204-18-039458",
        "document": "tv499046_ex1.htm",
        "local_path": "sources/tv499046_ex1.htm",
        "expected_sha256": "c4afe2905a530db0e7f6be048550b8425f81c9b70bd213a3e1361bdc37e3e494",
        "url": "https://www.sec.gov/Archives/edgar/data/1086434/000114420418039458/tv499046_ex1.htm",
    },
    "6k_q3_2018": {
        "form": "6-K Exhibit 99.1",
        "filed": "2018-10-23",
        "accepted_at": "2018-10-23T12:40:04Z",
        "accession": "0001144204-18-054844",
        "document": "tv505250_ex1.htm",
        "local_path": "sources/tv505250_ex1.htm",
        "expected_sha256": "2cf3382b32fa80c200980ca6619267baa9947fb50752f7dbf5096e1f49433811",
        "url": "https://www.sec.gov/Archives/edgar/data/1086434/000114420418054844/tv505250_ex1.htm",
    },
    "6k_q1_2019": {
        "form": "6-K Exhibit 99.1",
        "filed": "2019-04-30",
        "accepted_at": "2019-04-30T13:00:17Z",
        "accession": "0001144204-19-022137",
        "document": "tv520057_ex-1.htm",
        "local_path": "sources/tv520057_ex-1.htm",
        "expected_sha256": "a454374da3543a6e11a4036197427ffffa7fd8ab19d1810d0df47487b4446538",
        "url": "https://www.sec.gov/Archives/edgar/data/1086434/000114420419022137/tv520057_ex-1.htm",
    },
    "6k_q2_2019": {
        "form": "6-K Exhibit 99.1",
        "filed": "2019-07-23",
        "accepted_at": "2019-07-23T13:00:35Z",
        "accession": "0001144204-19-035471",
        "document": "tv525654_ex1.htm",
        "local_path": "sources/tv525654_ex1.htm",
        "expected_sha256": "8ada0f06a765a414d7113f9c44cfd817d645812d7a222b02fc894c57e829a325",
        "url": "https://www.sec.gov/Archives/edgar/data/1086434/000114420419035471/tv525654_ex1.htm",
    },
    "6k_q3_2019": {
        "form": "6-K Exhibit 99.1",
        "filed": "2019-10-29",
        "accepted_at": "2019-10-29T14:00:58Z",
        "accession": "0001104659-19-057132",
        "document": "tm1921326d1_ex1.htm",
        "local_path": "sources/tm1921326d1_ex1.htm",
        "expected_sha256": "8e74e56cec96d9e7ec40d3f7bb5fa478db88009a5a1b4ddfefeeb2b57ffc97db",
        "url": "https://www.sec.gov/Archives/edgar/data/1086434/000110465919057132/tm1921326d1_ex1.htm",
    },
    "6k_q1_2020": {
        "form": "6-K Exhibit 99.1",
        "filed": "2020-04-27",
        "accepted_at": "2020-04-27T12:00:58Z",
        "accession": "0001104659-20-051313",
        "document": "tm2017753d1_ex1.htm",
        "local_path": "sources/tm2017753d1_ex1.htm",
        "expected_sha256": "d736d2e122220f275d35de61f0f15079872cfccd9baa5e36167db18f2a926255",
        "url": "https://www.sec.gov/Archives/edgar/data/1086434/000110465920051313/tm2017753d1_ex1.htm",
    },
    "6k_q2_2020": {
        "form": "6-K Exhibit 99.1",
        "filed": "2020-07-28",
        "accepted_at": "2020-07-28T12:00:19Z",
        "accession": "0001104659-20-087029",
        "document": "tm2025835d1_ex1.htm",
        "local_path": "sources/tm2025835d1_ex1.htm",
        "expected_sha256": "e9d33e07f6c04f6d84257b051413ec128fb92a59df36c626efaeec7af3d2bbbe",
        "url": "https://www.sec.gov/Archives/edgar/data/1086434/000110465920087029/tm2025835d1_ex1.htm",
    },
    "6k_q3_2020": {
        "form": "6-K Exhibit 99.1",
        "filed": "2020-10-27",
        "accepted_at": "2020-10-27T13:30:15Z",
        "accession": "0001104659-20-118406",
        "document": "tm2034325d1_ex1.htm",
        "local_path": "sources/tm2034325d1_ex1.htm",
        "expected_sha256": "073cc3baab9f04fc359883906b900c5d8c10bfb9846c90cda45bd6e87819dbcc",
        "url": "https://www.sec.gov/Archives/edgar/data/1086434/000110465920118406/tm2034325d1_ex1.htm",
    },
    "6k_q2_2021": {
        "form": "6-K Exhibit 99.1",
        "filed": "2021-07-27",
        "accepted_at": "2021-07-27T12:01:54Z",
        "accession": "0001104659-21-096094",
        "document": "tm2123274d1_ex1.htm",
        "local_path": "sources/tm2123274d1_ex1.htm",
        "expected_sha256": "c2c689c79b6f5e1998804d474d07a5d81ac63905861eed6f0d402631962ef39b",
        "url": "https://www.sec.gov/Archives/edgar/data/1086434/000110465921096094/tm2123274d1_ex1.htm",
    },
    "6k_q3_2021": {
        "form": "6-K Exhibit 99.1",
        "filed": "2021-10-26",
        "accepted_at": "2021-10-26T12:15:48Z",
        "accession": "0001104659-21-129771",
        "document": "tm2131055d1_ex1.htm",
        "local_path": "sources/tm2131055d1_ex1.htm",
        "expected_sha256": "1ffeb8b94fa26cbbd5e0e88919c80933372bc2611d9ebc96ce9c705c0c303541",
        "url": "https://www.sec.gov/Archives/edgar/data/1086434/000110465921129771/tm2131055d1_ex1.htm",
    },
}


SOURCE_ROW_CHECKS = {
    "20f_fy2018": (
        {"metric": "revenue", "line_item": "Total revenues", "periods": ("FY2016", "FY2017", "FY2018"), "expected_values": (145_571, 156_739, 176_223)},
        {"metric": "net_income", "line_item": "Net income", "periods": ("FY2016", "FY2017", "FY2018"), "expected_values": (16_238, 4_030, 13_493)},
    ),
    "20f_fy2019": (
        {"metric": "revenue", "line_item": "Total revenues", "periods": ("FY2017", "FY2018", "FY2019"), "expected_values": (156_739, 176_223, 200_287)},
        {"metric": "net_income", "line_item": "Net income", "periods": ("FY2017", "FY2018", "FY2019"), "expected_values": (4_030, 13_493, 3_977)},
    ),
    "20f_fy2020": (
        {"metric": "revenue", "line_item": "Total revenues", "periods": ("FY2020", "FY2019", "FY2018"), "expected_values": (220_774, 200_287, 176_223)},
        {"metric": "net_income", "line_item": "Net income", "periods": ("FY2020", "FY2019", "FY2018"), "expected_values": (27_248, 3_977, 13_493)},
    ),
    "6k_q2_2018": (
        {"metric": "revenue", "line_item": "Total Revenues", "periods": ("H1 2018", "H1 2017", "Q2 2018", "Q2 2017"), "expected_values": (85_927, 76_113, 43_502, 38_736)},
        {"metric": "net_income", "line_item": "Net income", "periods": ("H1 2018", "H1 2017", "Q2 2018", "Q2 2017"), "expected_values": (4_828, 2_309, 2_394, 1_014)},
    ),
    "6k_q3_2018": (
        {"metric": "revenue", "line_item": "Total Revenues", "periods": ("9M 2018", "9M 2017", "Q3 2018", "Q3 2017"), "expected_values": (130_446, 115_321, 44_519, 39_208)},
        {"metric": "net_income", "line_item": "Net income", "periods": ("9M 2018", "9M 2017", "Q3 2018", "Q3 2017"), "expected_values": (8_963, 3_358, 4_135, 1_049)},
    ),
    "6k_q1_2019": (
        {"metric": "revenue", "line_item": "Total Revenues", "periods": ("Q1 2019", "Q1 2018"), "expected_values": (46_579, 42_425)},
        {"metric": "net_income", "line_item": "Net income", "periods": ("Q1 2019", "Q1 2018"), "expected_values": (3_049, 2_434)},
    ),
    "6k_q2_2019": (
        {"metric": "revenue", "line_item": "Total Revenues", "periods": ("H1 2019", "H1 2018", "Q2 2019", "Q2 2018"), "expected_values": (96_078, 85_927, 49_499, 43_502)},
        {"metric": "net_income", "line_item": "Net income", "periods": ("H1 2019", "H1 2018", "Q2 2019", "Q2 2018"), "expected_values": (7_843, 4_828, 4_794, 2_394)},
    ),
    "6k_q3_2019": (
        {"metric": "revenue", "line_item": "Total Revenues", "periods": ("9M 2019", "9M 2018", "Q3 2019", "Q3 2018"), "expected_values": (147_490, 130_446, 51_412, 44_519)},
        {"metric": "net_income", "line_item": "Net income", "periods": ("9M 2019", "9M 2018", "Q3 2019", "Q3 2018"), "expected_values": (12_210, 8_963, 4_367, 4_135)},
    ),
    "6k_q1_2020": (
        {"metric": "revenue", "line_item": "Total Revenues", "periods": ("Q1 2020", "Q1 2019"), "expected_values": (52_022, 46_579)},
        {"metric": "net_income", "line_item": "Net income", "periods": ("Q1 2020", "Q1 2019"), "expected_values": (5_264, 3_049)},
    ),
    "6k_q2_2020": (
        {"metric": "revenue", "line_item": "Total Revenues", "periods": ("H1 2020", "H1 2019", "Q2 2020", "Q2 2019"), "expected_values": (105_544, 96_078, 53_522, 49_499)},
        {"metric": "net_income", "line_item": "Net income", "periods": ("H1 2020", "H1 2019", "Q2 2020", "Q2 2019"), "expected_values": (11_903, 7_843, 6_639, 4_794)},
    ),
    "6k_q3_2020": (
        {"metric": "revenue", "line_item": "Total Revenues", "periods": ("9M 2020", "9M 2019", "Q3 2020", "Q3 2019"), "expected_values": (162_108, 147_490, 56_564, 51_412)},
        {"metric": "net_income", "line_item": "Net income", "periods": ("9M 2020", "9M 2019", "Q3 2020", "Q3 2019"), "expected_values": (18_867, 12_210, 6_964, 4_367)},
    ),
    "6k_q2_2021": (
        {"metric": "revenue", "line_item": "Total Revenues", "periods": ("H1 2021", "H1 2020", "Q2 2021", "Q2 2020"), "expected_values": (119_413, 105_544, 60_575, 53_522)},
        {"metric": "net_income", "line_item": "Net income", "periods": ("H1 2021", "H1 2020", "Q2 2021", "Q2 2020"), "expected_values": (18_233, 11_903, 8_240, 6_639)},
    ),
    "6k_q3_2021": (
        {"metric": "revenue", "line_item": "Total Revenues", "periods": ("9M 2021", "9M 2020", "Q3 2021", "Q3 2020"), "expected_values": (182_821, 162_108, 63_408, 56_564)},
        {"metric": "net_income", "line_item": "Net income", "periods": ("9M 2021", "9M 2020", "Q3 2021", "Q3 2020"), "expected_values": (26_496, 18_867, 8_263, 6_964)},
    ),
}


SOURCE_TEXT_CHECKS = {
    source_id: (
        ("U.S. GAAP", "U.S. dollars in thousands", "Total revenues")
        if source_id.startswith("20f_")
        else (
            "Consolidated Statements of Operations",
            "U.S. dollars in thousands",
            "Unaudited",
        )
    )
    for source_id in SOURCE_DOCUMENTS
}


PACKAGE_METADATA = {
    "2019-06-30": {"available_date": "2019-07-23", "period": "H1", "source_ids": ("20f_fy2018", "6k_q2_2018", "6k_q2_2019")},
    "2019-09-30": {"available_date": "2019-10-29", "period": "9M", "source_ids": ("20f_fy2018", "6k_q3_2018", "6k_q3_2019")},
    "2020-03-31": {"available_date": "2020-04-27", "period": "Q1", "source_ids": ("20f_fy2019", "6k_q1_2019", "6k_q1_2020")},
    "2021-06-30": {"available_date": "2021-07-27", "period": "H1", "source_ids": ("20f_fy2020", "6k_q2_2020", "6k_q2_2021")},
    "2021-09-30": {"available_date": "2021-10-26", "period": "9M", "source_ids": ("20f_fy2020", "6k_q3_2020", "6k_q3_2021")},
}


OPERANDS_USD_THOUSANDS = {
    "2019-06-30": {
        "revenue": (176_223, 85_927, 96_078, 156_739, 76_113, 85_927),
        "net_income": (13_493, 4_828, 7_843, 4_030, 2_309, 4_828),
    },
    "2019-09-30": {
        "revenue": (176_223, 130_446, 147_490, 156_739, 115_321, 130_446),
        "net_income": (13_493, 8_963, 12_210, 4_030, 3_358, 8_963),
    },
    "2020-03-31": {
        "revenue": (200_287, 46_579, 52_022, 176_223, 42_425, 46_579),
        "net_income": (3_977, 3_049, 5_264, 13_493, 2_434, 3_049),
    },
    "2021-06-30": {
        "revenue": (220_774, 105_544, 119_413, 200_287, 96_078, 105_544),
        "net_income": (27_248, 11_903, 18_233, 3_977, 7_843, 11_903),
    },
    "2021-09-30": {
        "revenue": (220_774, 162_108, 182_821, 200_287, 147_490, 162_108),
        "net_income": (27_248, 18_867, 26_496, 3_977, 12_210, 18_867),
    },
}


AUDIT_SIGNAL_DATES = (
    "2019-09-30", "2019-10-31", "2019-12-31", "2020-04-30",
    "2020-05-29", "2021-08-31", "2021-10-29", "2021-11-30",
)
AUDIT_OBSERVATIONS = tuple(
    (f"liq2000000-age{age}-growth", signal_date, age)
    for age in (150, 365, 550)
    for signal_date in AUDIT_SIGNAL_DATES
) + tuple(
    (f"liq10000000-age{age}-growth", "2020-05-29", age)
    for age in (150, 365, 550)
)


COMPARATIVE_MATCHES = (
    ("FY2018 revenue", "20f_fy2018", "20f_fy2019", 176_223),
    ("FY2018 net income", "20f_fy2018", "20f_fy2019", 13_493),
    ("FY2019 revenue", "20f_fy2019", "20f_fy2020", 200_287),
    ("FY2019 net income", "20f_fy2019", "20f_fy2020", 3_977),
    ("H1 2018 revenue", "6k_q2_2018", "6k_q2_2019", 85_927),
    ("H1 2018 net income", "6k_q2_2018", "6k_q2_2019", 4_828),
    ("9M 2018 revenue", "6k_q3_2018", "6k_q3_2019", 130_446),
    ("9M 2018 net income", "6k_q3_2018", "6k_q3_2019", 8_963),
    ("Q1 2019 revenue", "6k_q1_2019", "6k_q1_2020", 46_579),
    ("Q1 2019 net income", "6k_q1_2019", "6k_q1_2020", 3_049),
    ("H1 2020 revenue", "6k_q2_2020", "6k_q2_2021", 105_544),
    ("H1 2020 net income", "6k_q2_2020", "6k_q2_2021", 11_903),
    ("9M 2020 revenue", "6k_q3_2020", "6k_q3_2021", 162_108),
    ("9M 2020 net income", "6k_q3_2020", "6k_q3_2021", 18_867),
)


REJECTED_LATER_FILINGS = {
    "0001104659-22-010030": {"form": "6-K", "filed": "2022-02-01", "reason": "after every audited signal date"},
    "0001410578-22-001057": {"form": "20-F", "filed": "2022-04-28", "reason": "later FY2021 annual report; never used to revise PIT packages"},
}


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_path(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _download_source(url: str) -> bytes:
    with urlopen(Request(url, headers=SEC_HEADERS), timeout=120) as response:
        return response.read()


def _normalize_text(value: object) -> str:
    return " ".join(str(value).replace("\xa0", " ").replace("\u200b", " ").split())


def _numeric_cells(row: Iterable[object]) -> list[int]:
    values: list[int] = []
    for cell in row:
        text = _normalize_text(cell)
        if not text or text.lower() == "nan" or text in {"$", ")"}:
            continue
        negative = text.startswith("(") or text.endswith(")")
        cleaned = re.sub(r"[^0-9.-]", "", text)
        if not cleaned or cleaned in {"-", "."}:
            continue
        try:
            number = float(cleaned)
        except ValueError:
            continue
        if not number.is_integer():
            continue
        value = int(number)
        values.append(-abs(value) if negative else value)
    return values


def _contains_subsequence(values: list[int], expected: list[int]) -> bool:
    width = len(expected)
    return any(values[i:i + width] == expected for i in range(len(values) - width + 1))


def _parse_checked_rows(raw: bytes, source_id: str) -> list[dict[str, object]]:
    tables = pd.read_html(BytesIO(raw))
    verified = []
    for check in SOURCE_ROW_CHECKS[source_id]:
        expected = list(check["expected_values"])
        matched = False
        for table in tables:
            for _, row in table.iterrows():
                if _normalize_text(row.iloc[0]).lower() != str(check["line_item"]).lower():
                    continue
                if _contains_subsequence(_numeric_cells(row.iloc[1:]), expected):
                    matched = True
                    break
            if matched:
                break
        if not matched:
            raise RuntimeError(f"source row changed: {source_id} {check['line_item']} expected {check['expected_values']}")
        verified.append({
            "source_id": source_id,
            "metric": check["metric"],
            "line_item": check["line_item"],
            "periods": list(check["periods"]),
            "values": expected,
            "currency": CURRENCY,
            "scale": SOURCE_SCALE,
        })
    return verified


def validate_source_lock(sources: dict[str, dict[str, object]] | None = None) -> None:
    sources = SOURCE_DOCUMENTS if sources is None else sources
    for source_id, source in sources.items():
        if source["filed"] > PIT_CUTOFF:
            raise ValueError(f"{source_id} violates PIT cutoff {PIT_CUTOFF}")
        if not str(source["url"]).startswith("https://www.sec.gov/Archives/edgar/data/1086434/"):
            raise ValueError(f"{source_id} is not an official SEC archive URL")
        if not re.fullmatch(r"[0-9a-f]{64}", str(source["expected_sha256"])):
            raise ValueError(f"{source_id} lacks a valid SHA-256 lock")
    for fiscal_end, package in PACKAGE_METADATA.items():
        source_dates = [str(sources[source_id]["filed"]) for source_id in package["source_ids"]]
        if max(source_dates) != package["available_date"]:
            raise ValueError(f"{fiscal_end} available_date is not its latest source filing")


def verify_sources(output_dir: Path) -> tuple[dict, list[dict], list[dict]]:
    validate_source_lock()
    manifest_sources, verified_rows, verified_fragments = {}, [], []
    for source_id, source in SOURCE_DOCUMENTS.items():
        local_path = output_dir / str(source["local_path"])
        downloaded = False
        if not local_path.exists():
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(_download_source(str(source["url"])))
            downloaded = True
        raw = local_path.read_bytes()
        actual_sha = _sha256_bytes(raw)
        if actual_sha != source["expected_sha256"]:
            raise RuntimeError(f"SHA-256 mismatch for {source_id}: {actual_sha} != {source['expected_sha256']}")
        verified_rows.extend(_parse_checked_rows(raw, source_id))
        normalized = _normalize_text(BeautifulSoup(raw, "html.parser").get_text(" ", strip=True))
        for fragment in SOURCE_TEXT_CHECKS[source_id]:
            if _normalize_text(fragment).lower() not in normalized.lower():
                raise RuntimeError(f"source text changed: {source_id} {fragment}")
            verified_fragments.append({"source_id": source_id, "fragment": fragment})
        manifest_sources[source_id] = {
            **source,
            "local_path": str(local_path),
            "actual_sha256": actual_sha,
            "bytes": len(raw),
            "downloaded": downloaded,
            "currency": CURRENCY,
            "scale": SOURCE_SCALE,
            "accounting_standard": ACCOUNTING_STANDARD,
        }
    return manifest_sources, verified_rows, verified_fragments


def _growth(current: int, prior: int) -> float:
    if prior == 0:
        raise ValueError("cannot calculate growth from a zero prior TTM")
    return (current - prior) / abs(prior)


def exact_ttm_evidence() -> dict[str, object]:
    packages = {}
    for fiscal_end, metrics in OPERANDS_USD_THOUSANDS.items():
        derived = {}
        for metric, operands in metrics.items():
            current_annual, current_subtract, current_add, prior_annual, prior_subtract, prior_add = operands
            current_ttm = current_annual - current_subtract + current_add
            prior_ttm = prior_annual - prior_subtract + prior_add
            derived[metric] = {
                "prior_ttm_usd_thousands": prior_ttm,
                "current_ttm_usd_thousands": current_ttm,
                "growth": _growth(current_ttm, prior_ttm),
                "current_formula": "current annual - same-period prior YTD + current YTD",
                "prior_formula": "prior annual - same-period older YTD + prior YTD",
            }
        metadata = PACKAGE_METADATA[fiscal_end]
        packages[fiscal_end] = {
            "available_date": metadata["available_date"],
            "cumulative_period": metadata["period"],
            "currency": CURRENCY,
            "scale": SOURCE_SCALE,
            "accounting_standard": ACCOUNTING_STANDARD,
            "source_ids": list(metadata["source_ids"]),
            "source_accessions": [SOURCE_DOCUMENTS[s]["accession"] for s in metadata["source_ids"]],
            "operands_usd_thousands": metrics,
            "derived": derived,
        }
    return {
        "ticker": TICKER,
        "currency": CURRENCY,
        "accounting_standard": ACCOUNTING_STANDARD,
        "packages": packages,
        "comparative_match_count": len(COMPARATIVE_MATCHES),
        "comparative_matches": [
            {"label": label, "original_source": original, "later_comparative_source": later, "value_usd_thousands": value, "matched": True}
            for label, original, later, value in COMPARATIVE_MATCHES
        ],
        "restatement_isolation": (
            "All reused annual and cumulative comparatives match their original filings. "
            "Each TTM package becomes available only on its latest operand filing date; "
            "2022 filings are explicitly rejected and no later figure is backfilled."
        ),
    }


def strict_quarterly_facts() -> pd.DataFrame:
    records = []
    evidence = exact_ttm_evidence()["packages"]
    concepts = {"revenue": "Revenues", "net_income": "NetIncomeLoss"}
    for fiscal_end, package in evidence.items():
        accession = "+".join(package["source_accessions"])
        for metric, values in package["derived"].items():
            for output_metric, value in (
                (f"{metric}_ttm", values["current_ttm_usd_thousands"] * SOURCE_SCALE),
                (f"{metric}_growth", values["growth"]),
            ):
                records.append({
                    "ticker": TICKER,
                    "fiscal_end": fiscal_end,
                    "available_date": package["available_date"],
                    "metric": output_metric,
                    "value": value,
                    "taxonomy": "us-gaap",
                    "concept": f"audc_exact_cumulative_ttm:{concepts[metric]}:{CURRENCY}",
                    "form": "20-F_PLUS_6-K_CUMULATIVE_TTM",
                    "accession": accession,
                    "fetched_at": FETCHED_AT,
                })
    return pd.DataFrame(records, columns=OUTPUT_COLUMNS).sort_values(["fiscal_end", "metric"]).reset_index(drop=True)


def _package_for_signal(signal_date: str) -> tuple[str, dict[str, object]]:
    eligible = [(fiscal_end, package) for fiscal_end, package in exact_ttm_evidence()["packages"].items() if package["available_date"] <= signal_date]
    if not eligible:
        raise ValueError(f"no AUDC package available for {signal_date}")
    return max(eligible, key=lambda item: (item[1]["available_date"], item[0]))


def resolve_audit_observations() -> pd.DataFrame:
    rows = []
    for scenario, signal_date, maximum_age_days in AUDIT_OBSERVATIONS:
        fiscal_end, package = _package_for_signal(signal_date)
        age_days = (date.fromisoformat(signal_date) - date.fromisoformat(package["available_date"])).days
        derived = package["derived"]
        rows.append({
            "scenario": scenario,
            "signal_date": signal_date,
            "maximum_age_days": maximum_age_days,
            "resolved": age_days <= maximum_age_days,
            "decision": "complete_exact_cumulative_ttm_growth_bundle",
            "fiscal_end": fiscal_end,
            "available_date": package["available_date"],
            "financial_age_days": age_days,
            "revenue_ttm": derived["revenue"]["current_ttm_usd_thousands"] * SOURCE_SCALE,
            "revenue_growth": derived["revenue"]["growth"],
            "net_income_ttm": derived["net_income"]["current_ttm_usd_thousands"] * SOURCE_SCALE,
            "net_income_growth": derived["net_income"]["growth"],
            "currency": CURRENCY,
        })
    return pd.DataFrame(rows)


def validate_exact_packages() -> None:
    expected = {
        "2019-06-30": (166_553, 186_374, 6_549, 16_508),
        "2019-09-30": (171_864, 193_267, 9_635, 16_740),
        "2020-03-31": (180_377, 205_730, 14_108, 6_192),
        "2021-06-30": (209_753, 234_643, 8_037, 33_578),
        "2021-09-30": (214_905, 241_487, 10_634, 34_877),
    }
    packages = exact_ttm_evidence()["packages"]
    for fiscal_end, values in expected.items():
        revenue, profit = packages[fiscal_end]["derived"]["revenue"], packages[fiscal_end]["derived"]["net_income"]
        actual = (revenue["prior_ttm_usd_thousands"], revenue["current_ttm_usd_thousands"], profit["prior_ttm_usd_thousands"], profit["current_ttm_usd_thousands"])
        if actual != values:
            raise RuntimeError(f"AUDC exact TTM package changed for {fiscal_end}: {actual}")
    facts = strict_quarterly_facts()
    if len(facts) != 20 or set(facts.groupby(["fiscal_end", "available_date"])["metric"].nunique()) != {4}:
        raise RuntimeError("AUDC direct TTM growth package is incomplete")


def build(output_dir: Path = OUTPUT_DIR) -> dict[str, object]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    sources, verified_rows, verified_fragments = verify_sources(output_dir)
    validate_exact_packages()
    facts = strict_quarterly_facts()
    evidence = exact_ttm_evidence()
    resolution = resolve_audit_observations()
    unrecoverable: list[dict[str, object]] = []

    paths = {
        "strict_quarterly_facts": output_dir / "strict_quarterly_facts.csv",
        "exact_ttm_evidence": output_dir / "exact_ttm_evidence.json",
        "audit_observation_resolution": output_dir / "audit_observation_resolution.json",
        "unrecoverable_observations": output_dir / "unrecoverable_observations.json",
    }
    facts.to_csv(paths["strict_quarterly_facts"], index=False)
    paths["exact_ttm_evidence"].write_text(json.dumps(evidence, indent=2) + "\n")
    paths["audit_observation_resolution"].write_text(json.dumps(resolution.to_dict(orient="records"), indent=2) + "\n")
    paths["unrecoverable_observations"].write_text(json.dumps(unrecoverable, indent=2) + "\n")
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "parameters_frozen": False,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "ticker": TICKER,
        "cik": CIK,
        "pit_cutoff": PIT_CUTOFF,
        "currency": CURRENCY,
        "accounting_standard": ACCOUNTING_STANDARD,
        "accepted_direct_growth_package_count": len(PACKAGE_METADATA),
        "accepted_fact_count": len(facts),
        "resolved_audit_observation_count": int(resolution["resolved"].sum()),
        "resolved_unique_signal_date_count": int(resolution["signal_date"].nunique()),
        "unrecoverable_observation_count": len(unrecoverable),
        "source_operand_verification_count": sum(len(row["values"]) for row in verified_rows),
        "source_text_verification_count": len(verified_fragments),
        "comparative_match_count": len(COMPARATIVE_MATCHES),
        "sources": sources,
        "source_value_verification": {"rows": verified_rows, "fragments": verified_fragments},
        "rejected_later_filings": REJECTED_LATER_FILINGS,
        "outputs": {name: {"path": str(path), "sha256": _sha256_path(path)} for name, path in paths.items()},
        "guardrail": (
            "Every operand is a reported U.S.-dollar-thousand US-GAAP annual or "
            "cumulative amount available before its served signal. Repeated "
            "comparatives are identical to their original disclosures. No quarter, "
            "estimate, FX conversion, later filing, or formal financial fact is made up."
        ),
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(report, indent=2) + "\n")
    report["manifest"] = str(manifest_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    print(json.dumps(build(args.output_dir), indent=2))


if __name__ == "__main__":
    main()
