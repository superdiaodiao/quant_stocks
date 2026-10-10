#!/usr/bin/env python3
"""Build a source-locked, research-only exact-TTM growth package for BZUN.

Baozun is a foreign private issuer whose quarterly results are furnished on
Form 6-K rather than Form 10-Q.  This package uses only as-reported RMB-thousand
U.S.-GAAP total net revenue and net income attributable to Baozun's ordinary
shareholders.  Each TTM is an exact annual-minus-matching-quarters-plus-current-
quarters identity.  Adjusted measures, convenience USD translations, FX joins,
and filings after the applicable signal date are deliberately excluded.
"""

from __future__ import annotations

import argparse
from bs4 import BeautifulSoup
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
from typing import Iterable
from urllib.request import Request, urlopen

import pandas as pd

from src.io.fundamentals_update import OUTPUT_COLUMNS


OUTPUT_DIR = Path("output/research_only/v14/bzun_exact_ttm_growth")
TICKER = "BZUN"
CIK = 1_625_414
CURRENCY = "RMB"
SOURCE_SCALE = 1_000
ACCOUNTING_STANDARD = "US-GAAP"
PIT_CUTOFF = "2021-01-29"
FETCHED_AT = "2026-08-24"
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}

BASELINE_BINDING = {
    "audit_path": (
        "output/research_only/v14/"
        "batch_afya_legn_sdgr_companyfacts_allt_glng_allk_asnd_csiq_cron_iq_"
        "jamf_lx_iiiv_peri_uxin_gain_azpn_li_gilt_eslt_meso_mmyt_price_overlay_"
        "audit.json"
    ),
    "audit_sha256": (
        "a9a3fdc3d78192cef55eb72898b988edd9805a260bf58723d45fc6baaa90d0f5"
    ),
    "financial_priorities_sha256": (
        "ac0c18c7c24419c26e8e63065d3618b1e165f5d23a98bd6b18cfdac72f95b7e7"
    ),
    "quarterly_sha256": (
        "532ea8465abb1c20c75f838609c267dd17cd92003981908a8b5e56b8ff7fd293"
    ),
    "missing_observation_count": 18,
}


SOURCE_DOCUMENTS = {
    "20f_2018_04_11_fy2017": {
        "role": "original_annual_operand_and_republication_anchor",
        "form": "20-F",
        "filed": "2018-04-11",
        "accepted": "2018-04-11T20:37:07.000Z",
        "accession": "0001144204-18-020145",
        "document": "tv488603_20f.htm",
        "local_path": "sources/tv488603_20f.htm",
        "expected_sha256": (
            "a83306d54269d08055fb7002b9494b9965c1c5bf3e8525d943c645e9754d8f61"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1625414/"
            "000114420418020145/tv488603_20f.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
    },
    "6k_2018_05_17_q1": {
        "role": "original_quarter_operand",
        "form": "6-K/EX-99.1",
        "filed": "2018-05-17",
        "accepted": "2018-05-17T11:37:34.000Z",
        "accession": "0001144204-18-029393",
        "document": "tv494445_ex99-1.htm",
        "local_path": "sources/tv494445_ex99-1.htm",
        "expected_sha256": (
            "94730e783f8b20ebf238066213620a7d074e49fafad8746de429a91fcee77895"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1625414/"
            "000114420418029393/tv494445_ex99-1.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
    },
    "20f_2019_04_03_fy2018": {
        "role": "original_annual_operand_and_prior_year_corroboration",
        "form": "20-F",
        "filed": "2019-04-03",
        "accepted": "2019-04-03T18:05:49.000Z",
        "accession": "0001144204-19-017964",
        "document": "tv516935_20f.htm",
        "local_path": "sources/tv516935_20f.htm",
        "expected_sha256": (
            "0e36099ba5fcda04577622586702b7d95d87f6ccf05c958b50a9a18cc2481c15"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1625414/"
            "000114420419017964/tv516935_20f.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
    },
    "6k_2019_05_29_q1": {
        "role": "original_quarter_operand",
        "form": "6-K/EX-99.1",
        "filed": "2019-05-29",
        "accepted": "2019-05-29T10:13:28.000Z",
        "accession": "0001144204-19-028516",
        "document": "tv522610_ex99-1.htm",
        "local_path": "sources/tv522610_ex99-1.htm",
        "expected_sha256": (
            "35dbe3f246a25c2536a6ec552f3fd394f8903ca6e42626a06e11faaa76fcb710"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1625414/"
            "000114420419028516/tv522610_ex99-1.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
    },
    "6k_2019_08_21_q2": {
        "role": "original_quarter_operand",
        "form": "6-K/EX-99.1",
        "filed": "2019-08-21",
        "accepted": "2019-08-21T10:06:50.000Z",
        "accession": "0001144204-19-040869",
        "document": "tv528079_ex99-1.htm",
        "local_path": "sources/tv528079_ex99-1.htm",
        "expected_sha256": (
            "5bc87acb86eebf93247e334eb16917af164d8788cae296ada6c6e51378d6b7e7"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1625414/"
            "000114420419040869/tv528079_ex99-1.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
    },
    "6k_2019_11_21_q3": {
        "role": "original_quarter_operand",
        "form": "6-K/EX-99.1",
        "filed": "2019-11-21",
        "accepted": "2019-11-21T11:02:15.000Z",
        "accession": "0001104659-19-066004",
        "document": "tm1923590d1_ex99-1.htm",
        "local_path": "sources/tm1923590d1_ex99-1.htm",
        "expected_sha256": (
            "44e4e2dbc43f66c525bf5463aecddd7416f98f89b7a4f1c8a12d3c8636e475e9"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1625414/"
            "000110465919066004/tm1923590d1_ex99-1.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
    },
    "20f_2020_04_28_fy2019": {
        "role": "original_annual_operand_and_prior_year_corroboration",
        "form": "20-F",
        "filed": "2020-04-28",
        "accepted": "2020-04-28T13:30:32.000Z",
        "accession": "0001104659-20-052015",
        "document": "bzun-20191231x20f.htm",
        "local_path": "sources/bzun-20191231x20f.htm",
        "expected_sha256": (
            "15e8caf50e56cea652a92a644217ee75e0d65727cb5e5e8b5ed2165968232b12"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1625414/"
            "000110465920052015/bzun-20191231x20f.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
    },
    "6k_2020_06_02_q1": {
        "role": "original_quarter_operand",
        "form": "6-K/EX-99.1",
        "filed": "2020-06-02",
        "accepted": "2020-06-02T10:40:42.000Z",
        "accession": "0001104659-20-068642",
        "document": "tm2021580d1_ex99-1.htm",
        "local_path": "sources/tm2021580d1_ex99-1.htm",
        "expected_sha256": (
            "4ebf1f43bc31615260c784516d99bc0d9f1c2954e195fb4329f9bd1d3cebada7"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1625414/"
            "000110465920068642/tm2021580d1_ex99-1.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
    },
    "6k_2020_08_21_q2": {
        "role": "original_quarter_operand",
        "form": "6-K/EX-99.1",
        "filed": "2020-08-21",
        "accepted": "2020-08-21T12:14:09.000Z",
        "accession": "0001104659-20-097425",
        "document": "tm2029168d1_ex99-1.htm",
        "local_path": "sources/tm2029168d1_ex99-1.htm",
        "expected_sha256": (
            "719db84f50d643a6d232986834167536b13f69c6ad4def25112cce25791944ea"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1625414/"
            "000110465920097425/tm2029168d1_ex99-1.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
    },
    "6k_2020_11_23_q3": {
        "role": "latest_original_quarter_operand",
        "form": "6-K/EX-99.1",
        "filed": "2020-11-23",
        "accepted": "2020-11-23T11:59:58.000Z",
        "accession": "0001104659-20-128179",
        "document": "a20-36906_2ex99d1.htm",
        "local_path": "sources/a20-36906_2ex99d1.htm",
        "expected_sha256": (
            "4d28963feeb919c8e529e6c9ad9b8552514f932c63f7ce8f702a4b5114633b0c"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1625414/"
            "000110465920128179/a20-36906_2ex99d1.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
    },
}


SOURCE_ROW_CHECKS = {
    "20f_2018_04_11_fy2017": (
        {
            "metric": "revenue",
            "line_item": "Total net revenues",
            "periods": ("FY2015", "FY2016", "FY2017"),
            "expected_values": (2_598_443, 3_390_275, 4_148_808),
        },
        {
            "metric": "net_income",
            "line_item": (
                "Net income (loss) attributable to ordinary shareholders of "
                "Baozun Inc."
            ),
            "periods": ("FY2015", "FY2016", "FY2017"),
            "expected_values": (-2_711, 86_633, 208_866),
        },
    ),
    "6k_2018_05_17_q1": (
        {
            "metric": "revenue",
            "line_item": "Total net revenues",
            "periods": ("Q1 2017", "Q1 2018"),
            "expected_values": (804_871, 921_199),
        },
        {
            "metric": "net_income",
            "line_item": (
                "Net income attributable to ordinary shareholders of Baozun Inc."
            ),
            "periods": ("Q1 2017", "Q1 2018"),
            "expected_values": (10_605, 14_931),
        },
    ),
    "20f_2019_04_03_fy2018": (
        {
            "metric": "revenue",
            "line_item": "Total net revenues",
            "periods": ("FY2016", "FY2017", "FY2018"),
            "expected_values": (3_390_275, 4_148_808, 5_393_037),
        },
        {
            "metric": "net_income",
            "line_item": (
                "Net income attributable to ordinary shareholders of Baozun Inc."
            ),
            "periods": ("FY2016", "FY2017", "FY2018"),
            "expected_values": (86_633, 208_866, 269_712),
        },
    ),
    "6k_2019_05_29_q1": (
        {
            "metric": "revenue",
            "line_item": "Total net revenues",
            "periods": ("Q1 2018", "Q1 2019"),
            "expected_values": (921_199, 1_286_761),
        },
        {
            "metric": "net_income",
            "line_item": (
                "Net income attributable to ordinary shareholders of Baozun Inc."
            ),
            "periods": ("Q1 2018", "Q1 2019"),
            "expected_values": (14_931, 34_009),
        },
    ),
    "6k_2019_08_21_q2": (
        {
            "metric": "revenue",
            "line_item": "Total net revenues",
            "periods": ("Q2 2018", "Q2 2019"),
            "expected_values": (1_159_130, 1_704_210),
        },
        {
            "metric": "net_income",
            "line_item": (
                "Net income attributable to ordinary shareholders of Baozun Inc."
            ),
            "periods": ("Q2 2018", "Q2 2019"),
            "expected_values": (36_751, 67_062),
        },
    ),
    "6k_2019_11_21_q3": (
        {
            "metric": "revenue",
            "line_item": "Total net revenues",
            "periods": ("Q3 2018", "Q3 2019"),
            "expected_values": (1_110_761, 1_503_094),
        },
        {
            "metric": "net_income",
            "line_item": (
                "Net income attributable to ordinary shareholders of Baozun Inc."
            ),
            "periods": ("Q3 2018", "Q3 2019"),
            "expected_values": (29_787, 39_352),
        },
    ),
    "20f_2020_04_28_fy2019": (
        {
            "metric": "revenue",
            "line_item": "Total net revenues",
            "periods": ("FY2017", "FY2018", "FY2019"),
            "expected_values": (4_148_808, 5_393_037, 7_278_192),
        },
        {
            "metric": "net_income",
            "line_item": (
                "Net income attributable to ordinary shareholders of Baozun Inc."
            ),
            "periods": ("FY2017", "FY2018", "FY2019"),
            "expected_values": (208_866, 269_712, 281_297),
        },
    ),
    "6k_2020_06_02_q1": (
        {
            "metric": "revenue",
            "line_item": "Total net revenues",
            "periods": ("Q1 2019", "Q1 2020"),
            "expected_values": (1_286_761, 1_523_640),
        },
        {
            "metric": "net_income",
            "line_item": (
                "Net income attributable to ordinary shareholders of Baozun Inc."
            ),
            "periods": ("Q1 2019", "Q1 2020"),
            "expected_values": (34_009, 2_239),
        },
    ),
    "6k_2020_08_21_q2": (
        {
            "metric": "revenue",
            "line_item": "Total net revenues",
            "periods": ("Q2 2019", "Q2 2020"),
            "expected_values": (1_704_210, 2_152_066),
        },
        {
            "metric": "net_income",
            "line_item": (
                "Net income attributable to ordinary shareholders of Baozun Inc."
            ),
            "periods": ("Q2 2019", "Q2 2020"),
            "expected_values": (67_062, 119_771),
        },
    ),
    "6k_2020_11_23_q3": (
        {
            "metric": "revenue",
            "line_item": "Total net revenues",
            "periods": ("Q3 2019", "Q3 2020"),
            "expected_values": (1_503_094, 1_829_159),
        },
        {
            "metric": "net_income",
            "line_item": (
                "Net income attributable to ordinary shareholders of Baozun Inc."
            ),
            "periods": ("Q3 2019", "Q3 2020"),
            "expected_values": (39_352, 64_635),
        },
    ),
}


SOURCE_TEXT_CHECKS = {
    "20f_2018_04_11_fy2017": (
        "Net Income Attributable to Ordinary Shareholders of Baozun Inc.",
        "RMB208.9 million",
        "convenience of the reader",
        "U.S. GAAP",
    ),
    "6k_2018_05_17_q1": (
        "Baozun Announces First Quarter 2018 Unaudited Financial Results",
        "For the three months ended March 31, 2017 2018 RMB RMB US$",
        "convenience of the reader",
        "U.S. GAAP",
    ),
    "20f_2019_04_03_fy2018": (
        "adopted this standard on January 1, 2018 using a full retrospective approach",
        "cumulative effect to the beginning balance of shareholders' equity",
        "convenience of the reader",
        "U.S. GAAP",
    ),
    "6k_2019_05_29_q1": (
        "Baozun Announces First Quarter 2019 Unaudited Financial Results",
        "For the three months ended March 31, 2018 2019 RMB RMB US$",
        "convenience of the reader",
        "U.S. GAAP",
    ),
    "6k_2019_08_21_q2": (
        "Baozun Announces Second Quarter 2019 Unaudited Financial Results",
        "For the three months ended June 30, 2018 2019 RMB RMB US$",
        "convenience of the reader",
        "U.S. GAAP",
    ),
    "6k_2019_11_21_q3": (
        "Baozun Announces Third Quarter 2019 Unaudited Financial Results",
        "For the three months ended September 30, 2018 2019 RMB RMB US$",
        "convenience of the reader",
        "U.S. GAAP",
    ),
    "20f_2020_04_28_fy2019": (
        "adopted this standard on January 1, 2018 using a full retrospective approach",
        "convenience of the reader",
        "U.S. GAAP",
    ),
    "6k_2020_06_02_q1": (
        "Baozun Announces First Quarter 2020 Unaudited Financial Results",
        "For the three months ended March 31, 2019 2020 RMB RMB US$",
        "convenience of the reader",
        "U.S. GAAP",
    ),
    "6k_2020_08_21_q2": (
        "Baozun Announces Second Quarter 2020 Unaudited Financial Results",
        "For the three months ended June 30, 2019 2020 RMB RMB US$",
        "convenience of the reader",
        "U.S. GAAP",
    ),
    "6k_2020_11_23_q3": (
        "Baozun Announces Third Quarter 2020 Unaudited Financial Results",
        "For the three months ended September 30, 2019 2020 RMB RMB US$",
        "convenience of the reader",
        "U.S. GAAP",
    ),
}


OPERANDS_RMB_THOUSANDS = {
    "revenue": {
        "fy2017": 4_148_808,
        "fy2018": 5_393_037,
        "fy2019": 7_278_192,
        "q1_2017": 804_871,
        "q1_2018": 921_199,
        "q2_2018": 1_159_130,
        "q3_2018": 1_110_761,
        "q1_2019": 1_286_761,
        "q2_2019": 1_704_210,
        "q3_2019": 1_503_094,
        "q1_2020": 1_523_640,
        "q2_2020": 2_152_066,
        "q3_2020": 1_829_159,
    },
    "net_income": {
        "fy2017": 208_866,
        "fy2018": 269_712,
        "fy2019": 281_297,
        "q1_2017": 10_605,
        "q1_2018": 14_931,
        "q2_2018": 36_751,
        "q3_2018": 29_787,
        "q1_2019": 34_009,
        "q2_2019": 67_062,
        "q3_2019": 39_352,
        "q1_2020": 2_239,
        "q2_2020": 119_771,
        "q3_2020": 64_635,
    },
}


SNAPSHOT_DEFINITIONS = (
    {
        "snapshot_id": "ttm_2019_q1",
        "fiscal_end": "2019-03-31",
        "available_date": "2019-05-29",
        "prior_formula": "FY2017 - Q1_2017 + Q1_2018",
        "current_formula": "FY2018 - Q1_2018 + Q1_2019",
        "prior_terms": ((1, "fy2017"), (-1, "q1_2017"), (1, "q1_2018")),
        "current_terms": ((1, "fy2018"), (-1, "q1_2018"), (1, "q1_2019")),
        "source_ids": (
            "20f_2018_04_11_fy2017",
            "6k_2018_05_17_q1",
            "20f_2019_04_03_fy2018",
            "6k_2019_05_29_q1",
        ),
    },
    {
        "snapshot_id": "ttm_2020_q2",
        "fiscal_end": "2020-06-30",
        "available_date": "2020-08-21",
        "prior_formula": "FY2018 - H1_2018 + H1_2019",
        "current_formula": "FY2019 - H1_2019 + H1_2020",
        "prior_terms": (
            (1, "fy2018"), (-1, "q1_2018"), (-1, "q2_2018"),
            (1, "q1_2019"), (1, "q2_2019"),
        ),
        "current_terms": (
            (1, "fy2019"), (-1, "q1_2019"), (-1, "q2_2019"),
            (1, "q1_2020"), (1, "q2_2020"),
        ),
        "source_ids": (
            "20f_2019_04_03_fy2018",
            "6k_2019_05_29_q1",
            "6k_2019_08_21_q2",
            "20f_2020_04_28_fy2019",
            "6k_2020_06_02_q1",
            "6k_2020_08_21_q2",
        ),
    },
    {
        "snapshot_id": "ttm_2020_q3",
        "fiscal_end": "2020-09-30",
        "available_date": "2020-11-23",
        "prior_formula": "FY2018 - M9_2018 + M9_2019",
        "current_formula": "FY2019 - M9_2019 + M9_2020",
        "prior_terms": (
            (1, "fy2018"), (-1, "q1_2018"), (-1, "q2_2018"),
            (-1, "q3_2018"), (1, "q1_2019"), (1, "q2_2019"),
            (1, "q3_2019"),
        ),
        "current_terms": (
            (1, "fy2019"), (-1, "q1_2019"), (-1, "q2_2019"),
            (-1, "q3_2019"), (1, "q1_2020"), (1, "q2_2020"),
            (1, "q3_2020"),
        ),
        "source_ids": (
            "20f_2019_04_03_fy2018",
            "6k_2019_05_29_q1",
            "6k_2019_08_21_q2",
            "6k_2019_11_21_q3",
            "20f_2020_04_28_fy2019",
            "6k_2020_06_02_q1",
            "6k_2020_08_21_q2",
            "6k_2020_11_23_q3",
        ),
    },
)


AUDIT_OBSERVATIONS = tuple(
    (f"liq{liquidity}-age{age}-growth", signal_date, age)
    for liquidity in (2_000_000, 10_000_000)
    for age in (150, 365, 550)
    for signal_date in ("2019-07-31", "2020-08-31", "2021-01-29")
)


REJECTED_LATER_FILINGS = {
    "0001104659-21-031782": {
        "form": "6-K",
        "filed": "2021-03-04",
        "reason": "postdates the latest 2021-01-29 signal and is not backfilled",
    },
    "0001104659-21-049360": {
        "form": "20-F",
        "filed": "2021-04-12",
        "reason": "postdates the latest signal and cannot revise 2020 operands",
    },
}


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_path(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _download_source(url: str) -> bytes:
    with urlopen(Request(url, headers=SEC_HEADERS), timeout=120) as response:
        return response.read()


def _normalize_text(value: object) -> str:
    return " ".join(
        str(value)
        .replace("\xa0", " ")
        .replace("\u2019", "'")
        .replace("\u2018", "'")
        .split()
    )


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


def _parse_checked_rows(raw: bytes, source_id: str) -> list[dict[str, object]]:
    tables = pd.read_html(BytesIO(raw))
    verified: list[dict[str, object]] = []
    for check in SOURCE_ROW_CHECKS[source_id]:
        expected = list(check["expected_values"])
        matches = []
        for table in tables:
            for _, row in table.iterrows():
                if _normalize_text(row.iloc[0]).lower() != str(
                    check["line_item"]
                ).lower():
                    continue
                numeric = _numeric_cells(row.iloc[1:])
                if len(numeric) >= len(expected) and numeric[: len(expected)] == expected:
                    matches.append(expected)
        if not matches:
            raise RuntimeError(
                f"source row changed: {source_id} {check['line_item']} "
                f"expected {check['expected_values']}"
            )
        verified.append(
            {
                "source_id": source_id,
                "metric": check["metric"],
                "line_item": check["line_item"],
                "periods": list(check["periods"]),
                "selected_rmb_values": matches[0],
                "currency": CURRENCY,
                "scale": SOURCE_SCALE,
                "trailing_usd_translation_rejected": True,
            }
        )
    return verified


def validate_source_lock(
    sources: dict[str, dict[str, object]] | None = None,
) -> None:
    sources = SOURCE_DOCUMENTS if sources is None else sources
    for source_id, source in sources.items():
        if source["filed"] > PIT_CUTOFF:
            raise ValueError(f"{source_id} violates PIT cutoff {PIT_CUTOFF}")
        if not str(source["accepted"]).startswith(f"{source['filed']}T"):
            raise ValueError(f"{source_id} filed/accepted dates disagree")
        if source["currency"] != CURRENCY or source["scale"] != SOURCE_SCALE:
            raise ValueError(f"{source_id} has mixed currency or scale")
        if source["accounting_standard"] != ACCOUNTING_STANDARD:
            raise ValueError(f"{source_id} has a non-US-GAAP accounting basis")
        prefix = "https://www.sec.gov/Archives/edgar/data/1625414/"
        if not str(source["url"]).startswith(prefix):
            raise ValueError(f"{source_id} is not an official SEC archive URL")
        if str(source["document"]) != Path(str(source["url"])).name:
            raise ValueError(f"{source_id} URL/document mismatch")
        compact_accession = str(source["accession"]).replace("-", "")
        if compact_accession not in str(source["url"]):
            raise ValueError(f"{source_id} URL/accession mismatch")
        if not re.fullmatch(r"[0-9a-f]{64}", str(source["expected_sha256"])):
            raise ValueError(f"{source_id} lacks a valid full-file SHA-256 lock")

    for definition in SNAPSHOT_DEFINITIONS:
        available_date = str(definition["available_date"])
        for source_id in definition["source_ids"]:
            if sources[source_id]["filed"] > available_date:
                raise ValueError(
                    f"{source_id} postdates snapshot {definition['snapshot_id']}"
                )


def verify_sources(
    output_dir: Path,
) -> tuple[dict[str, dict[str, object]], list[dict[str, object]], list[dict[str, str]]]:
    validate_source_lock()
    manifest_sources: dict[str, dict[str, object]] = {}
    verified_rows: list[dict[str, object]] = []
    verified_fragments: list[dict[str, str]] = []
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
            raise RuntimeError(
                f"SHA-256 mismatch for {source_id}: {actual_sha} != "
                f"{source['expected_sha256']}"
            )
        verified_rows.extend(_parse_checked_rows(raw, source_id))
        normalized = _normalize_text(
            BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
        )
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
            "selected_basis": (
                "as-reported RMB-thousand U.S.-GAAP rows; non-GAAP/adjusted "
                "rows and trailing US$ convenience translations rejected"
            ),
        }
    return manifest_sources, verified_rows, verified_fragments


def _sum_terms(values: dict[str, int], terms: Iterable[tuple[int, str]]) -> int:
    return sum(sign * values[name] for sign, name in terms)


def _growth(current: int, prior: int) -> float:
    if prior == 0:
        raise ValueError("cannot calculate growth from a zero prior TTM")
    return (current - prior) / abs(prior)


def exact_ttm_evidence() -> dict[str, object]:
    snapshots: list[dict[str, object]] = []
    for definition in SNAPSHOT_DEFINITIONS:
        derived: dict[str, dict[str, object]] = {}
        for metric, operands in OPERANDS_RMB_THOUSANDS.items():
            prior_ttm = _sum_terms(operands, definition["prior_terms"])
            current_ttm = _sum_terms(operands, definition["current_terms"])
            derived[metric] = {
                "prior_ttm_rmb_thousands": prior_ttm,
                "current_ttm_rmb_thousands": current_ttm,
                "growth": _growth(current_ttm, prior_ttm),
                "prior_formula": definition["prior_formula"],
                "current_formula": definition["current_formula"],
                "prior_terms": [list(term) for term in definition["prior_terms"]],
                "current_terms": [list(term) for term in definition["current_terms"]],
            }
        snapshots.append(
            {
                "snapshot_id": definition["snapshot_id"],
                "fiscal_end": definition["fiscal_end"],
                "available_date": definition["available_date"],
                "source_ids": list(definition["source_ids"]),
                "operand_accessions": [
                    SOURCE_DOCUMENTS[source_id]["accession"]
                    for source_id in definition["source_ids"]
                ],
                "derived": derived,
            }
        )
    return {
        "ticker": TICKER,
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
        "metric_mapping": {
            "revenue": "Total net revenues (not services-only revenue)",
            "net_income": (
                "Net income attributable to ordinary shareholders of Baozun Inc.; "
                "not consolidated net income and not non-GAAP net income"
            ),
        },
        "operands_rmb_thousands": OPERANDS_RMB_THOUSANDS,
        "snapshots": snapshots,
        "accounting_policy_comparability": {
            "asc_606": (
                "The 2019 20-F states ASC 606 was adopted using the full "
                "retrospective approach. Original FY2017 and FY2018 revenue and "
                "issuer-attributable net income equal the next 20-F comparatives."
            ),
            "quarter_republication_checks": (
                "Q1 2018 and Q1/Q2/Q3 2019 comparison columns equal the original "
                "quarter releases; no later value is substituted."
            ),
            "status": "EXACT_AS_REPORTED_US_GAAP_RMB_COMPARABLE",
        },
        "rejected_measurements": (
            "Non-GAAP net income",
            "adjusted or non-GAAP operating measures",
            "US$ convenience translations",
            "ADS/per-share amounts",
            "post-signal filings or later restatements",
        ),
    }


def strict_quarterly_facts() -> pd.DataFrame:
    evidence = exact_ttm_evidence()
    records = []
    concepts = {
        "revenue": "TotalNetRevenues",
        "net_income": "NetIncomeAttributableToOrdinaryShareholders",
    }
    for snapshot in evidence["snapshots"]:
        accession = "+".join(snapshot["operand_accessions"])
        for metric, values in snapshot["derived"].items():
            for output_metric, value in (
                (f"{metric}_ttm", values["current_ttm_rmb_thousands"] * SOURCE_SCALE),
                (f"{metric}_growth", values["growth"]),
            ):
                records.append(
                    {
                        "ticker": TICKER,
                        "fiscal_end": snapshot["fiscal_end"],
                        "available_date": snapshot["available_date"],
                        "metric": output_metric,
                        "value": value,
                        "taxonomy": "us-gaap",
                        "concept": (
                            f"bzun_exact_ttm:{concepts[metric]}:{CURRENCY}"
                        ),
                        "form": "20-F_PLUS_6-K_EXACT_QUARTER_TTM",
                        "accession": accession,
                        "fetched_at": FETCHED_AT,
                    }
                )
    return (
        pd.DataFrame(records, columns=OUTPUT_COLUMNS)
        .sort_values(["fiscal_end", "metric"])
        .reset_index(drop=True)
    )


def _selected_snapshot(signal_date: str) -> dict[str, object]:
    eligible = [
        snapshot
        for snapshot in exact_ttm_evidence()["snapshots"]
        if snapshot["available_date"] <= signal_date
    ]
    if not eligible:
        raise RuntimeError(f"no PIT snapshot for {signal_date}")
    return max(eligible, key=lambda snapshot: str(snapshot["available_date"]))


def resolve_audit_observations() -> pd.DataFrame:
    rows = []
    for scenario, signal_date, maximum_age_days in AUDIT_OBSERVATIONS:
        snapshot = _selected_snapshot(signal_date)
        age = (pd.Timestamp(signal_date) - pd.Timestamp(snapshot["available_date"])).days
        source_filed_dates = [
            SOURCE_DOCUMENTS[source_id]["filed"] for source_id in snapshot["source_ids"]
        ]
        resolved = (
            snapshot["available_date"] <= signal_date
            and age <= maximum_age_days
            and all(filed <= signal_date for filed in source_filed_dates)
        )
        derived = snapshot["derived"]
        rows.append(
            {
                "scenario": scenario,
                "signal_date": signal_date,
                "maximum_age_days": maximum_age_days,
                "resolved": resolved,
                "decision": "complete_exact_as_reported_ttm_growth_bundle",
                "fiscal_end": snapshot["fiscal_end"],
                "available_date": snapshot["available_date"],
                "financial_age_days": age,
                "revenue_ttm": (
                    derived["revenue"]["current_ttm_rmb_thousands"] * SOURCE_SCALE
                ),
                "revenue_growth": derived["revenue"]["growth"],
                "net_income_ttm": (
                    derived["net_income"]["current_ttm_rmb_thousands"] * SOURCE_SCALE
                ),
                "net_income_growth": derived["net_income"]["growth"],
                "currency": CURRENCY,
            }
        )
    return pd.DataFrame(rows)


def validate_exact_package() -> None:
    evidence = exact_ttm_evidence()
    expected = {
        "2019-03-31": {
            "revenue": (4_265_136, 5_758_599),
            "net_income": (213_192, 288_790),
        },
        "2020-06-30": {
            "revenue": (6_303_679, 7_962_927),
            "net_income": (319_101, 302_236),
        },
        "2020-09-30": {
            "revenue": (6_696_012, 8_288_992),
            "net_income": (328_666, 327_519),
        },
    }
    for snapshot in evidence["snapshots"]:
        for metric in ("revenue", "net_income"):
            values = snapshot["derived"][metric]
            actual = (
                values["prior_ttm_rmb_thousands"],
                values["current_ttm_rmb_thousands"],
            )
            if actual != expected[snapshot["fiscal_end"]][metric]:
                raise RuntimeError(
                    f"BZUN exact TTM changed: {snapshot['fiscal_end']} {metric}"
                )

    facts = strict_quarterly_facts()
    if len(facts) != 12 or set(facts["metric"]) != {
        "revenue_ttm",
        "revenue_growth",
        "net_income_ttm",
        "net_income_growth",
    }:
        raise RuntimeError("BZUN exact growth packages are incomplete")
    resolution = resolve_audit_observations()
    if len(resolution) != 18 or not resolution["resolved"].all():
        raise RuntimeError("BZUN did not resolve all 18 strict-PIT observations")


def build(output_dir: Path = OUTPUT_DIR) -> dict[str, object]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    sources, verified_rows, verified_fragments = verify_sources(output_dir)
    validate_exact_package()
    facts = strict_quarterly_facts()
    evidence = exact_ttm_evidence()
    resolution = resolve_audit_observations()

    facts_path = output_dir / "strict_quarterly_facts.csv"
    evidence_path = output_dir / "exact_ttm_evidence.json"
    resolution_path = output_dir / "audit_observation_resolution.json"
    manifest_path = output_dir / "manifest.json"
    facts.to_csv(facts_path, index=False)
    evidence_path.write_text(json.dumps(evidence, indent=2) + "\n")
    resolution_path.write_text(
        json.dumps(resolution.to_dict(orient="records"), indent=2) + "\n"
    )
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "parameters_frozen": False,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "shared_candidate_integrated": False,
        "formal_financials_modified": False,
        "ticker": TICKER,
        "cik": CIK,
        "pit_cutoff": PIT_CUTOFF,
        "currency": CURRENCY,
        "accounting_standard": ACCOUNTING_STANDARD,
        "baseline_binding": BASELINE_BINDING,
        "accepted_direct_growth_package_count": len(evidence["snapshots"]),
        "accepted_fact_count": len(facts),
        "resolved_audit_observation_count": int(resolution["resolved"].sum()),
        "expected_audit_observation_count": len(AUDIT_OBSERVATIONS),
        "resolved_unique_signal_date_count": int(resolution["signal_date"].nunique()),
        "source_count": len(sources),
        "source_operand_verification_count": sum(
            len(row["selected_rmb_values"]) for row in verified_rows
        ),
        "source_text_verification_count": len(verified_fragments),
        "sources": sources,
        "source_value_verification": {
            "rows": verified_rows,
            "fragments": verified_fragments,
        },
        "rejected_later_filings": REJECTED_LATER_FILINGS,
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path),
                "sha256": _sha256_path(facts_path),
            },
            "exact_ttm_evidence": {
                "path": str(evidence_path),
                "sha256": _sha256_path(evidence_path),
            },
            "audit_observation_resolution": {
                "path": str(resolution_path),
                "sha256": _sha256_path(resolution_path),
            },
        },
        "guardrail": (
            "Only exact as-reported RMB-thousand U.S.-GAAP total net revenues "
            "and net income attributable to Baozun ordinary shareholders are "
            "used. Non-GAAP/adjusted values, US$ convenience translations, FX "
            "joins, ADS amounts, post-signal filings, and later restatements are "
            "rejected. This standalone research artifact is not integrated."
        ),
    }
    manifest_path.write_text(json.dumps(report, indent=2) + "\n")
    report["manifest"] = str(manifest_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    report = build(args.output_dir)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
