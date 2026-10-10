#!/usr/bin/env python3
"""Build source-locked, research-only exact-TTM loss evidence for UXIN.

UXIN was a Nasdaq-listed ADS for Uxin Limited, a foreign private issuer whose
contemporaneous reports used U.S. GAAP and issuer-level CNY amounts.  This
supplement preserves only exact consolidated negative TTM profit states that
were public before the two 2021 signals.  It cannot create quarterly facts,
revenue growth, or a positive-profit candidate.
"""

from __future__ import annotations

import argparse
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
import hashlib
import http.client
import json
from pathlib import Path
import re
import time
from typing import Iterable
from urllib.request import Request, urlopen
import warnings

import pandas as pd

from src.io.fundamentals_update import OUTPUT_COLUMNS


OUTPUT_DIR = Path("output/research_only/v14/uxin_exact_ttm_loss")
TICKER = "UXIN"
CIK = 1_729_173
CURRENCY = "CNY"
SOURCE_SCALE = 1_000
ACCOUNTING_STANDARD = "US-GAAP"
PIT_CUTOFF = "2021-05-28"
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
BASELINE_BINDING = {
    "quarterly": (
        "output/research_only/v14/"
        "candidate_fundamentals_v14_batch_afya_legn_sdgr_companyfacts_allt_"
        "glng_allk_asnd_csiq_cron_iq_jamf_lx/quarterly.csv"
    ),
    "quarterly_sha256": (
        "a7663535f1dc11b42e8fb7802948f5ae8a355626f6d4b4d4d45be9996324b87b"
    ),
    "audit": (
        "output/research_only/v14/"
        "batch_afya_legn_sdgr_companyfacts_allt_glng_allk_asnd_csiq_cron_"
        "iq_jamf_lx_audit.json"
    ),
    "audit_sha256": (
        "c5133e9899bdf73f1192b28f19ef7c3f920f170ec56f2cc771419151a64783f3"
    ),
    "financial_priorities": (
        "output/research_only/v14/"
        "batch_afya_legn_sdgr_companyfacts_allt_glng_allk_asnd_csiq_cron_"
        "iq_jamf_lx_audit_financial_priorities.csv"
    ),
    "financial_priorities_sha256": (
        "445012666a3e95017fcf097ce77bea532dda194b849fa76d6d3de16e194d3115"
    ),
    "baseline_reason": "FOREIGN_PERIODIC_NO_10Q",
}

SOURCE_DOCUMENTS = {
    "20f_2020_07_24_transition": {
        "form": "20-F transition report",
        "filed": "2020-07-24",
        "accession": "0001104659-20-086426",
        "document": "uxin-20200331x20f.htm",
        "local_path": "sources/uxin_2020_transition_20f.htm",
        "expected_sha256": (
            "281df4b6d187c8b7e6f55591177b6e9bb5edb0cf2aa11c4ade69643bec393529"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1729173/"
            "000110465920086426/uxin-20200331x20f.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
        "audit_status": "AUDITED_FY2019_UNAUDITED_TRANSITION_Q1",
        "role": "financial_operand",
    },
    "6k_2020_12_17_h1_ex991": {
        "form": "6-K/EX-99.1",
        "filed": "2020-12-17",
        "accession": "0001104659-20-136853",
        "document": "a20-38783_1ex99d1.htm",
        "local_path": "sources/uxin_2020_h1_exhibit_99-1.htm",
        "expected_sha256": (
            "c42dc89a5dd72571aeb1f7bb16ffe2bc6e0d33720180e3592e522c8f5ae85937"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1729173/"
            "000110465920136853/a20-38783_1ex99d1.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
        "audit_status": "UNAUDITED",
        "role": "financial_operand",
    },
    "6k_2021_01_25_cfo_ex991": {
        "form": "6-K/EX-99.1",
        "filed": "2021-01-25",
        "accession": "0001104659-21-006789",
        "document": "a21-3921_1ex99d1.htm",
        "local_path": "sources/uxin_2021_cfo_exhibit_99-1.htm",
        "expected_sha256": (
            "e04e98641009bde74909389b9e4e8ff2eecfe1be96441ed5c5e1b682a5c7dfff"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1729173/"
            "000110465921006789/a21-3921_1ex99d1.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
        "audit_status": "NOT_APPLICABLE",
        "role": "intervening_nonfinancial_check",
    },
    "6k_2021_04_01_term_sheet_ex991": {
        "form": "6-K/EX-99.1",
        "filed": "2021-04-01",
        "accession": "0001104659-21-045102",
        "document": "a21-11853_1ex99d1.htm",
        "local_path": "sources/uxin_2021_term_sheet_exhibit_99-1.htm",
        "expected_sha256": (
            "0698c2b5eaef092dd3a8ea63b953e9d03e5190c02fee44700502569a398fc660"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1729173/"
            "000110465921045102/a21-11853_1ex99d1.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
        "audit_status": "NOT_APPLICABLE",
        "role": "intervening_nonfinancial_check",
    },
    "6k_2021_04_29_q3_ex991": {
        "form": "6-K/EX-99.1",
        "filed": "2021-04-29",
        "accession": "0001104659-21-056965",
        "document": "a21-14610_1ex99d1.htm",
        "local_path": "sources/uxin_2021_q3_exhibit_99-1.htm",
        "expected_sha256": (
            "3ef5855bf6b3e992a812cae417bb7bd7c7b15917da5c3e9cd7ac89f46f662089"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1729173/"
            "000110465921056965/a21-14610_1ex99d1.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
        "audit_status": "UNAUDITED",
        "role": "financial_operand",
    },
}
ALLOWED_SOURCE_ACCESSIONS = {
    source["accession"] for source in SOURCE_DOCUMENTS.values()
}

SOURCE_IDENTITIES = {
    "20f_2020_07_24_transition": (
        "UXIN LIMITED",
        "CONSOLIDATED STATEMENTS OF COMPREHENSIVE LOSS",
        "FOR THE YEAR ENDED DECEMBER 31, 2017, 2018 AND 2019",
        "AND THE THREE MONTHS ENDED MARCH 31, 2020",
        "U.S. GAAP",
        "change of fiscal year end from December 31 to March 31",
    ),
    "6k_2020_12_17_h1_ex991": (
        "Uxin Reports Unaudited Second Quarter of Fiscal Year 2021 Financial Results",
        "Unaudited Consolidated Statements of Comprehensive Loss",
        "For the six months ended September 30",
    ),
    "6k_2021_01_25_cfo_ex991": (
        "Uxin Announces Change of Chief Financial Officer",
        "Uxin Limited",
    ),
    "6k_2021_04_01_term_sheet_ex991": (
        "Uxin Announces Entry into a Binding Term Sheet with Potential Investors",
        "Uxin Limited",
    ),
    "6k_2021_04_29_q3_ex991": (
        "Uxin Reports Unaudited Third Quarter of Fiscal Year 2021 Financial Results",
        "Unaudited Consolidated Statements of Comprehensive Loss",
        "For the nine months ended December 31",
    ),
}

INTERVENING_FILINGS = {
    "6k_2021_01_25_cfo_ex991": {
        "announcement": "chief financial officer change",
        "financial_payload": False,
    },
    "6k_2021_04_01_term_sheet_ex991": {
        "announcement": "binding investment term sheet",
        "financial_payload": False,
    },
}
FINANCIAL_TABLE_PHRASES = (
    "Unaudited Consolidated Statements of Comprehensive Loss",
    "For the three months ended",
    "For the six months ended",
    "For the nine months ended",
)

SOURCE_PARSE_SPECS = {
    "20f_2020_07_24_transition": {
        "row_specs": {
            "annual_transition_net_loss": {
                "context_phrases": (
                    "For the year ended December 31", "March 31", "2017",
                    "2018", "2019", "2020", "Discontinued operations",
                    "Accretion on redeemable preferred shares",
                ),
                "columns": {
                    "FY2017": "FY2017", "FY2018": "FY2018",
                    "FY2019": "FY2019", "Q1_2020": "Q1 2020",
                    "Q1_2020_USD": "Q1 2020 USD",
                },
                "row_label": "Net loss",
                "occurrence": 0,
            },
            "annual_transition_continuing": {
                "context_phrases": (
                    "For the year ended December 31", "March 31", "2017",
                    "2018", "2019", "2020", "Discontinued operations",
                ),
                "columns": {
                    "FY2017": "FY2017", "FY2018": "FY2018",
                    "FY2019": "FY2019", "Q1_2020": "Q1 2020",
                    "Q1_2020_USD": "Q1 2020 USD",
                },
                "row_label": "Net loss from continuing operations, net of tax",
            },
            "annual_transition_attributable": {
                "context_phrases": (
                    "For the year ended December 31", "March 31", "2017",
                    "2018", "2019", "2020", "Discontinued operations",
                ),
                "columns": {
                    "FY2017": "FY2017", "FY2018": "FY2018",
                    "FY2019": "FY2019", "Q1_2020": "Q1 2020",
                    "Q1_2020_USD": "Q1 2020 USD",
                },
                "row_label": "Net loss attributable to UXIN LIMITED",
            },
            "q1_2019_net_loss": {
                "context_phrases": (
                    "Three months ended March 31", "2019",
                    "Results of Operations", "Cash Flows",
                    "Net income from discontinued operations",
                ),
                "columns": {"Q1_2019": "Q1 2019"},
                "row_label": "Net loss",
            },
            "q1_2019_continuing": {
                "context_phrases": (
                    "Three months ended March 31", "2019",
                    "Results of Operations", "Cash Flows",
                    "Net income from discontinued operations",
                ),
                "columns": {"Q1_2019": "Q1 2019"},
                "row_label": "Net loss from continuing operations, net of tax",
            },
            "q1_2019_attributable": {
                "context_phrases": (
                    "Three months ended March 31", "2019",
                    "Results of Operations", "Cash Flows",
                    "Net income from discontinued operations",
                ),
                "columns": {"Q1_2019": "Q1 2019"},
                "row_label": "Net loss attributable to ordinary shareholders",
            },
        },
    },
    "6k_2020_12_17_h1_ex991": {
        "row_specs": {
            "net_loss": {
                "context_phrases": (
                    "For the three months ended September 30",
                    "For the six months ended September 30",
                    "2019", "2020", "Total revenues",
                    "Discontinued operations",
                ),
                "columns": {
                    "Q3_2019": "Q3 2019", "Q3_2020": "Q3 2020",
                    "Q3_2020_USD": "Q3 2020 USD", "H1_2019": "H1 2019",
                    "H1_2020": "H1 2020", "H1_2020_USD": "H1 2020 USD",
                },
                "row_label": "Net loss",
                "occurrence": 0,
            },
            "continuing": {
                "context_phrases": (
                    "For the three months ended September 30",
                    "For the six months ended September 30",
                    "2019", "2020", "Discontinued operations",
                ),
                "columns": {
                    "Q3_2019": "Q3 2019", "Q3_2020": "Q3 2020",
                    "Q3_2020_USD": "Q3 2020 USD", "H1_2019": "H1 2019",
                    "H1_2020": "H1 2020", "H1_2020_USD": "H1 2020 USD",
                },
                "row_label": "Net loss from continuing operations, net of tax",
            },
            "attributable": {
                "context_phrases": (
                    "For the three months ended September 30",
                    "For the six months ended September 30",
                    "2019", "2020", "Discontinued operations",
                ),
                "columns": {
                    "Q3_2019": "Q3 2019", "Q3_2020": "Q3 2020",
                    "Q3_2020_USD": "Q3 2020 USD", "H1_2019": "H1 2019",
                    "H1_2020": "H1 2020", "H1_2020_USD": "H1 2020 USD",
                },
                "row_label": "Net loss attributable to UXIN LIMITED",
            },
        },
    },
    "6k_2021_04_29_q3_ex991": {
        "row_specs": {
            "net_loss": {
                "context_phrases": (
                    "For the three months ended December 31",
                    "For the nine months ended December 31",
                    "2019", "2020", "Total revenues",
                    "Discontinued operations",
                ),
                "columns": {
                    "Q4_2019": "Q4 2019", "Q4_2020": "Q4 2020",
                    "Q4_2020_USD": "Q4 2020 USD", "M9_2019": "9M 2019",
                    "M9_2020": "9M 2020", "M9_2020_USD": "9M 2020 USD",
                },
                "row_label": "Net loss",
                "occurrence": 0,
            },
            "continuing": {
                "context_phrases": (
                    "For the three months ended December 31",
                    "For the nine months ended December 31",
                    "2019", "2020", "Discontinued operations",
                ),
                "columns": {
                    "Q4_2019": "Q4 2019", "Q4_2020": "Q4 2020",
                    "Q4_2020_USD": "Q4 2020 USD", "M9_2019": "9M 2019",
                    "M9_2020": "9M 2020", "M9_2020_USD": "9M 2020 USD",
                },
                "row_label": "Net loss from continuing operations, net of tax",
            },
            "attributable": {
                "context_phrases": (
                    "For the three months ended December 31",
                    "For the nine months ended December 31",
                    "2019", "2020", "Discontinued operations",
                ),
                "columns": {
                    "Q4_2019": "Q4 2019", "Q4_2020": "Q4 2020",
                    "Q4_2020_USD": "Q4 2020 USD", "M9_2019": "9M 2019",
                    "M9_2020": "9M 2020", "M9_2020_USD": "9M 2020 USD",
                },
                "row_label": "Net loss attributable to UXIN LIMITED",
            },
        },
    },
}

SOURCE_VALUE_EXPECTATIONS = {
    "fy2019_total": ("20f_2020_07_24_transition", "annual_transition_net_loss", "FY2019", -1_990_128),
    "fy2019_continuing": ("20f_2020_07_24_transition", "annual_transition_continuing", "FY2019", -1_327_678),
    "fy2019_attributable": ("20f_2020_07_24_transition", "annual_transition_attributable", "FY2019", -1_988_676),
    "q1_2019_total": ("20f_2020_07_24_transition", "q1_2019_net_loss", "Q1_2019", -284_984),
    "q1_2019_continuing": ("20f_2020_07_24_transition", "q1_2019_continuing", "Q1_2019", -295_539),
    "q1_2019_attributable": ("20f_2020_07_24_transition", "q1_2019_attributable", "Q1_2019", -284_539),
    "q1_2020_total": ("20f_2020_07_24_transition", "annual_transition_net_loss", "Q1_2020", -2_489_562),
    "q1_2020_continuing": ("20f_2020_07_24_transition", "annual_transition_continuing", "Q1_2020", -2_034_385),
    "q1_2020_attributable": ("20f_2020_07_24_transition", "annual_transition_attributable", "Q1_2020", -2_484_179),
    "h1_2019_total": ("6k_2020_12_17_h1_ex991", "net_loss", "H1_2019", -738_416),
    "h1_2019_continuing": ("6k_2020_12_17_h1_ex991", "continuing", "H1_2019", -443_103),
    "h1_2019_attributable": ("6k_2020_12_17_h1_ex991", "attributable", "H1_2019", -737_738),
    "h1_2020_total": ("6k_2020_12_17_h1_ex991", "net_loss", "H1_2020", -115_560),
    "h1_2020_continuing": ("6k_2020_12_17_h1_ex991", "continuing", "H1_2020", -411_304),
    "h1_2020_attributable": ("6k_2020_12_17_h1_ex991", "attributable", "H1_2020", -115_553),
    "q4_2019_total": ("6k_2021_04_29_q3_ex991", "net_loss", "Q4_2019", -966_728),
    "q4_2019_continuing": ("6k_2021_04_29_q3_ex991", "continuing", "Q4_2019", -589_036),
    "q4_2019_attributable": ("6k_2021_04_29_q3_ex991", "attributable", "Q4_2019", -966_399),
    "q4_2020_total": ("6k_2021_04_29_q3_ex991", "net_loss", "Q4_2020", -172_857),
    "q4_2020_continuing": ("6k_2021_04_29_q3_ex991", "continuing", "Q4_2020", -172_857),
    "q4_2020_attributable": ("6k_2021_04_29_q3_ex991", "attributable", "Q4_2020", -172_857),
    "m9_2019_total": ("6k_2021_04_29_q3_ex991", "net_loss", "M9_2019", -1_705_144),
    "m9_2019_attributable": ("6k_2021_04_29_q3_ex991", "attributable", "M9_2019", -1_704_137),
    "m9_2020_total": ("6k_2021_04_29_q3_ex991", "net_loss", "M9_2020", -288_417),
    "m9_2020_attributable": ("6k_2021_04_29_q3_ex991", "attributable", "M9_2020", -288_410),
}

OPERANDS_CNY_THOUSANDS = {
    item_id: {
        "source_id": item[0], "metric": item[1], "table_column": item[2],
        "value": item[3],
    }
    for item_id, item in SOURCE_VALUE_EXPECTATIONS.items()
    if item_id in {
        "fy2019_total", "q1_2019_total", "q1_2020_total",
        "h1_2019_total", "h1_2020_total", "q4_2019_total",
        "q4_2020_total",
    }
}

TTM_SPECS = (
    {
        "fiscal_end": "2020-09-30",
        "available_date": "2020-12-17",
        "formula": "FY2019 - (Q1_2019 + H1_2019) + (Q1_2020 + H1_2020)",
        "terms": (
            (1, "fy2019_total"), (-1, "q1_2019_total"),
            (-1, "h1_2019_total"), (1, "q1_2020_total"),
            (1, "h1_2020_total"),
        ),
        "expected_cny_thousands": -3_571_850,
        "form": "20-F_TRANSITION_PLUS_6-K_H1_CUMULATIVE_TTM",
    },
    {
        "fiscal_end": "2020-12-31",
        "available_date": "2021-04-29",
        "formula": "Q1_2020 + H1_2020 + Q4_2020",
        "roll_forward_formula": "TTM_2020_09 - Q4_2019 + Q4_2020",
        "terms": (
            (1, "q1_2020_total"), (1, "h1_2020_total"),
            (1, "q4_2020_total"),
        ),
        "expected_cny_thousands": -2_777_979,
        "form": "20-F_TRANSITION_PLUS_6-K_H1_PLUS_6-K_Q3_TTM",
    },
)

AUDIT_OBSERVATIONS = (
    ("liq2000000-age150-growth", "2021-04-30", 150),
    ("liq2000000-age150-growth", "2021-05-28", 150),
    ("liq2000000-age365-growth", "2021-05-28", 365),
    ("liq10000000-age150-growth", "2021-05-28", 150),
    ("liq10000000-age365-growth", "2021-05-28", 365),
)

POST_SIGNAL_EXCLUSIONS = (
    {
        "form": "20-F",
        "filed": "2021-07-30",
        "accession": "0001104659-21-098224",
        "document": "uxin-20210331x20f.htm",
        "expected_sha256": (
            "8e7ac674debb5b56c0cf461330fa919e4ef58b7ed767902928b16e3997b1220a"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1729173/"
            "000110465921098224/uxin-20210331x20f.htm"
        ),
        "reason": (
            "annual report was filed after both signals; its repeated prior-period "
            "values are corroboration only and are never backfilled"
        ),
    },
)


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _download_source(url: str) -> bytes:
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            with urlopen(Request(url, headers=SEC_HEADERS), timeout=120) as response:
                return response.read()
        except (OSError, http.client.IncompleteRead) as error:
            last_error = error
            if attempt < 4:
                time.sleep(1 + attempt)
    raise RuntimeError(f"failed to download locked UXIN source: {url}") from last_error


def _normalize_text(value: str) -> str:
    return " ".join(
        value.replace("\xa0", " ").replace("\u200b", " ").replace("−", "-").split()
    ).casefold()


def _row_numbers(row) -> list[int]:
    text = " ".join(row.stripped_strings).replace("\xa0", " ")
    text = re.sub(r"\s*,\s*", ",", text)
    tokens = re.findall(
        r"\(\s*\d[\d,]*\s*\)|(?<![\w])\d[\d,]*(?![\w])",
        text,
    )
    values = []
    for token in tokens:
        digits = re.sub(r"\D", "", token)
        if digits:
            value = int(digits)
            values.append(-value if "(" in token else value)
    return values


def _source_soup(raw: bytes):
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
    return BeautifulSoup(raw, "lxml")


def _verify_source_identity(source_id: str, raw: bytes) -> None:
    document_text = _normalize_text(" ".join(_source_soup(raw).stripped_strings))
    if any(
        _normalize_text(phrase) not in document_text
        for phrase in SOURCE_IDENTITIES[source_id]
    ):
        raise RuntimeError(f"UXIN source identity changed for {source_id}")
    if source_id in INTERVENING_FILINGS and any(
        _normalize_text(phrase) in document_text
        for phrase in FINANCIAL_TABLE_PHRASES
    ):
        raise RuntimeError(
            f"UXIN intervening filing unexpectedly contains financial tables: {source_id}"
        )


def _parse_source_tables(source_id: str, raw: bytes) -> dict[str, dict]:
    _verify_source_identity(source_id, raw)
    soup = _source_soup(raw)
    parsed = {}
    for metric, row_spec in SOURCE_PARSE_SPECS[source_id]["row_specs"].items():
        context = tuple(
            _normalize_text(item) for item in row_spec["context_phrases"]
        )
        normalized_label = _normalize_text(row_spec["row_label"])
        expected_count = len(row_spec["columns"])
        occurrence = int(row_spec.get("occurrence", 0))
        candidates = []
        for table in soup.find_all("table"):
            table_text = _normalize_text(" ".join(table.stripped_strings))
            if not all(item in table_text for item in context):
                continue
            matching_rows = []
            for row in table.find_all("tr"):
                labels = [
                    _normalize_text(" ".join(cell.stripped_strings))
                    for cell in row.find_all(("td", "th"))
                ]
                first_label = next((item for item in labels if item), "")
                if first_label == normalized_label:
                    matching_rows.append(row)
            if len(matching_rows) <= occurrence:
                continue
            values = _row_numbers(matching_rows[occurrence])
            if len(values) >= expected_count:
                candidates.append(dict(zip(
                    row_spec["columns"], values[-expected_count:], strict=True
                )))
        if not candidates:
            raise RuntimeError(f"no unambiguous UXIN {metric} table for {source_id}")
        canonical = json.dumps(candidates[0], sort_keys=True)
        if any(json.dumps(item, sort_keys=True) != canonical for item in candidates):
            raise RuntimeError(f"conflicting UXIN {metric} tables for {source_id}")
        parsed[metric] = candidates[0]
    return parsed


def validate_source_lock(sources: dict[str, dict] | None = None) -> None:
    documents = SOURCE_DOCUMENTS if sources is None else sources
    if set(documents) != set(SOURCE_IDENTITIES):
        raise ValueError("source lock and identity set differ")
    for source_id, source in documents.items():
        accession = source["accession"]
        if accession not in ALLOWED_SOURCE_ACCESSIONS:
            raise ValueError(f"unapproved source accession: {accession}")
        if source["filed"] > PIT_CUTOFF:
            raise ValueError(f"source {source_id} was filed after PIT cutoff")
        if source["currency"] != CURRENCY or source["scale"] != SOURCE_SCALE:
            raise ValueError(f"source {source_id} has mixed currency or scale")
        if source["accounting_standard"] != ACCOUNTING_STANDARD:
            raise ValueError(f"source {source_id} is not U.S. GAAP")
        accession_path = accession.replace("-", "")
        if f"/data/{CIK}/{accession_path}/" not in source["url"]:
            raise ValueError(f"source {source_id} URL does not lock CIK/accession")
        if not source["url"].endswith("/" + source["document"]):
            raise ValueError(f"source {source_id} URL does not lock document")
        relative_path = Path(source["local_path"])
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError(f"source {source_id} has unsafe local_path")
        if not re.fullmatch(r"[0-9a-f]{64}", source["expected_sha256"]):
            raise ValueError(f"source {source_id} has invalid expected SHA-256")
    if any(item["filed"] <= PIT_CUTOFF for item in POST_SIGNAL_EXCLUSIONS):
        raise ValueError("post-signal exclusion was available by the signal")


def verify_source_values(raw_by_source: dict[str, bytes]) -> list[dict]:
    if set(raw_by_source) != set(SOURCE_DOCUMENTS):
        raise ValueError("raw source set does not match the source lock")
    for source_id, raw in raw_by_source.items():
        _verify_source_identity(source_id, raw)
    parsed = {
        source_id: _parse_source_tables(source_id, raw_by_source[source_id])
        for source_id in SOURCE_PARSE_SPECS
    }
    verified = []
    for item_id, (source_id, metric, table_column, expected_value) in (
        SOURCE_VALUE_EXPECTATIONS.items()
    ):
        parsed_value = parsed[source_id][metric][table_column]
        if parsed_value != expected_value:
            raise RuntimeError(
                f"source value {item_id} changed: parsed {parsed_value}, "
                f"expected {expected_value}"
            )
        verified.append({
            "item_id": item_id,
            "source_id": source_id,
            "metric": metric,
            "table_column": table_column,
            "currency": CURRENCY,
            "scale": SOURCE_SCALE,
            "expected_value": expected_value,
            "parsed_value": parsed_value,
        })
    _validate_scope_and_period_identities()
    return verified


def _value(item_id: str) -> int:
    return int(SOURCE_VALUE_EXPECTATIONS[item_id][3])


def _validate_scope_and_period_identities() -> None:
    scope_rows = (
        ("fy2019", "fy2019_total", "fy2019_continuing", -662_450,
         "fy2019_attributable", -1_452),
        ("q1_2019", "q1_2019_total", "q1_2019_continuing", 10_555,
         "q1_2019_attributable", -445),
        ("q1_2020", "q1_2020_total", "q1_2020_continuing", -455_177,
         "q1_2020_attributable", -5_383),
        ("h1_2019", "h1_2019_total", "h1_2019_continuing", -295_313,
         "h1_2019_attributable", -678),
        ("h1_2020", "h1_2020_total", "h1_2020_continuing", 295_744,
         "h1_2020_attributable", -7),
        ("q4_2019", "q4_2019_total", "q4_2019_continuing", -377_692,
         "q4_2019_attributable", -329),
        ("q4_2020", "q4_2020_total", "q4_2020_continuing", 0,
         "q4_2020_attributable", 0),
    )
    for period, total_id, continuing_id, discontinued, attributable_id, nci in scope_rows:
        if _value(total_id) != _value(continuing_id) + discontinued:
            raise RuntimeError(f"UXIN {period} continuing/discontinued scope changed")
        if _value(total_id) - _value(attributable_id) != nci:
            raise RuntimeError(f"UXIN {period} non-controlling interest changed")
    if _value("fy2019_total") != sum(
        _value(item) for item in ("q1_2019_total", "h1_2019_total", "q4_2019_total")
    ):
        raise RuntimeError("UXIN FY2019 period partition changed")
    if _value("m9_2019_total") != _value("h1_2019_total") + _value("q4_2019_total"):
        raise RuntimeError("UXIN 2019 9M bridge changed")
    if _value("m9_2020_total") != _value("h1_2020_total") + _value("q4_2020_total"):
        raise RuntimeError("UXIN 2020 9M bridge changed")


def prepare_verified_sources(
    output_dir: Path,
) -> tuple[dict[str, bytes], dict[str, dict], list[dict]]:
    validate_source_lock()
    output_dir = Path(output_dir)
    raw_by_source = {}
    provenance = {}
    for source_id, source in SOURCE_DOCUMENTS.items():
        local_path = output_dir / source["local_path"]
        if local_path.exists():
            raw = local_path.read_bytes()
            downloaded = False
        else:
            raw = _download_source(source["url"])
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(raw)
            downloaded = True
        actual_sha256 = _sha256_bytes(raw)
        if actual_sha256 != source["expected_sha256"]:
            raise RuntimeError(
                f"UXIN source SHA-256 mismatch for {source_id}: {actual_sha256}"
            )
        raw_by_source[source_id] = raw
        provenance[source_id] = {
            **source,
            "local_path": str(local_path),
            "actual_sha256": actual_sha256,
            "bytes": len(raw),
            "downloaded": downloaded,
        }
    return raw_by_source, provenance, verify_source_values(raw_by_source)


def exact_ttm_evidence() -> list[dict]:
    validate_source_lock()
    _validate_scope_and_period_identities()
    results = []
    for spec in TTM_SPECS:
        value_thousands = sum(
            coefficient * OPERANDS_CNY_THOUSANDS[operand_id]["value"]
            for coefficient, operand_id in spec["terms"]
        )
        if value_thousands != spec["expected_cny_thousands"]:
            raise RuntimeError(f"UXIN exact TTM changed for {spec['fiscal_end']}")
        if value_thousands >= 0:
            raise RuntimeError("UXIN direct exact-TTM layer is exclusion-only")
        source_ids = list(dict.fromkeys(
            OPERANDS_CNY_THOUSANDS[operand_id]["source_id"]
            for _, operand_id in spec["terms"]
        ))
        sources = [SOURCE_DOCUMENTS[source_id] for source_id in source_ids]
        if spec["available_date"] != max(source["filed"] for source in sources):
            raise RuntimeError(f"UXIN availability changed for {spec['fiscal_end']}")
        result = {
            "ticker": TICKER,
            "evidence_kind": "exact_cumulative_ttm_loss_as_reported",
            "fiscal_end": spec["fiscal_end"],
            "available_date": spec["available_date"],
            "currency": CURRENCY,
            "source_scale": SOURCE_SCALE,
            "accounting_standard": ACCOUNTING_STANDARD,
            "net_income_ttm": value_thousands * SOURCE_SCALE,
            "formula": spec["formula"],
            "operand_ids": [operand_id for _, operand_id in spec["terms"]],
            "source_ids": source_ids,
            "source_accessions": [source["accession"] for source in sources],
            "source_urls": [source["url"] for source in sources],
            "form": spec["form"],
            "profit_scope": (
                "consolidated issuer total Net loss under U.S. GAAP, including "
                "continuing and discontinued operations and before attribution "
                "to non-controlling interests; CNY amount, not ADS/EPS"
            ),
        }
        if "roll_forward_formula" in spec:
            result["roll_forward_formula"] = spec["roll_forward_formula"]
        results.append(result)
    rolled = (
        results[0]["net_income_ttm"] // SOURCE_SCALE
        - _value("q4_2019_total") + _value("q4_2020_total")
    )
    if rolled != TTM_SPECS[1]["expected_cny_thousands"]:
        raise RuntimeError("UXIN December TTM roll-forward changed")
    return results


def direct_ttm_facts(fetched_at: str | None = None) -> pd.DataFrame:
    if fetched_at is None:
        fetched_at = str(pd.Timestamp.now("UTC").tz_localize(None).normalize().date())
    rows = [{
        "ticker": TICKER,
        "fiscal_end": evidence["fiscal_end"],
        "available_date": evidence["available_date"],
        "metric": "net_income_ttm",
        "value": evidence["net_income_ttm"],
        "taxonomy": "us-gaap",
        "concept": "uxin_exact_ttm:ProfitLoss:CNY",
        "form": evidence["form"],
        "accession": "+".join(evidence["source_accessions"]),
        "fetched_at": fetched_at,
    } for evidence in exact_ttm_evidence()]
    return pd.DataFrame(rows, columns=OUTPUT_COLUMNS)


def resolve_observation(signal_date: str, maximum_age_days: int) -> dict:
    signal = pd.Timestamp(signal_date)
    evidence = pd.DataFrame(exact_ttm_evidence())
    evidence["fiscal_end"] = pd.to_datetime(evidence["fiscal_end"])
    evidence["available_date"] = pd.to_datetime(evidence["available_date"])
    eligible = evidence.loc[
        evidence["available_date"].le(signal)
        & (signal - evidence["available_date"]).dt.days.le(maximum_age_days)
    ].sort_values(["fiscal_end", "available_date"])
    if eligible.empty:
        return {
            "resolved": False,
            "decision": "missing_financial",
            "reason": "no exact TTM loss available within the age limit",
        }
    row = eligible.iloc[-1]
    return {
        "resolved": True,
        "decision": "known_nonpositive_profit",
        "fiscal_end": row["fiscal_end"].strftime("%Y-%m-%d"),
        "available_date": row["available_date"].strftime("%Y-%m-%d"),
        "financial_age_days": int((signal - row["available_date"]).days),
        "net_income_ttm": int(row["net_income_ttm"]),
        "currency": row["currency"],
        "source_accessions": row["source_accessions"],
    }


def resolve_audit_observations(
    observations: Iterable[tuple[str, str, int]] = AUDIT_OBSERVATIONS,
) -> pd.DataFrame:
    return pd.DataFrame([{
        "scenario": scenario,
        "signal_date": signal_date,
        "maximum_age_days": maximum_age_days,
        **resolve_observation(signal_date, maximum_age_days),
    } for scenario, signal_date, maximum_age_days in observations])


def build(output_dir: Path = OUTPUT_DIR) -> dict:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _, provenance, source_value_verification = prepare_verified_sources(output_dir)
    evidence = exact_ttm_evidence()
    facts = direct_ttm_facts()
    resolutions = resolve_audit_observations()
    if not resolutions["resolved"].all():
        raise RuntimeError("not every declared UXIN audit observation resolved")

    facts_path = output_dir / "strict_quarterly_facts.csv"
    evidence_path = output_dir / "exact_ttm_evidence.json"
    resolution_path = output_dir / "audit_observation_resolution.json"
    facts.to_csv(facts_path, index=False)
    evidence_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    resolution_path.write_text(
        resolutions.to_json(orient="records", indent=2) + "\n", encoding="utf-8"
    )
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "shared_candidate_integrated": False,
        "ticker": TICKER,
        "cik": CIK,
        "accounting_standard": ACCOUNTING_STANDARD,
        "reporting_currency": CURRENCY,
        "security": (
            "Nasdaq-listed ADS, one ADS representing three Class A ordinary "
            "shares; evidence uses consolidated issuer CNY totals, not a "
            "per-ADS amount, EPS, or USD convenience translation"
        ),
        "reporting_profile": "FOREIGN_PRIVATE_ISSUER_20-F_6-K_US-GAAP",
        "baseline_binding": BASELINE_BINDING,
        "accepted_exact_ttm_loss_count": len(facts),
        "resolved_unique_signal_date_count": resolutions["signal_date"].nunique(),
        "resolved_audit_observation_count": len(resolutions),
        "source_documents": provenance,
        "source_value_verification": source_value_verification,
        "intervening_filing_audit": INTERVENING_FILINGS,
        "post_signal_exclusions": POST_SIGNAL_EXCLUSIONS,
        "period_and_scope_audit": {
            "fy2019_identity": (
                "FY2019 = Q1_2019 + H1_2019 + Q4_2019 = -1,990,128 CNY "
                "thousands"
            ),
            "m9_republication_check": (
                "April 29 9M totals equal December 17 H1 plus Q4 for both "
                "2019 and 2020; no pre-signal H1 revision"
            ),
            "discontinued_operations": (
                "Every operand is total Net loss. Continuing plus discontinued "
                "operations is reconciled for all seven operands, so a line-item "
                "reclassification cannot silently alter the selected total."
            ),
            "attribution": (
                "Net loss attributable to UXIN LIMITED is separately parsed and "
                "reconciled to non-controlling interests, but never mixed into "
                "the ProfitLoss total operands."
            ),
        },
        "revenue_assessment": {
            "direct_growth_emitted": False,
            "reason": (
                "Exact negative consolidated TTM profit resolves eligibility. "
                "No revenue, quarterly split, EPS, or growth fact is emitted."
            ),
        },
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path), "sha256": _sha256(facts_path)
            },
            "exact_ttm_evidence": {
                "path": str(evidence_path), "sha256": _sha256(evidence_path)
            },
            "audit_observation_resolution": {
                "path": str(resolution_path), "sha256": _sha256(resolution_path)
            },
        },
        "guardrail": (
            "The latest source for the 2020-12-31 TTM was filed 2021-04-29, "
            "one day before the first signal and 29 days before the second. "
            "The 2021-01-25 and 2021-04-01 exhibits contain no quarterly "
            "financial statements. The 2021-07-30 annual report is explicitly "
            "excluded. This standalone layer cannot manufacture quarters or growth."
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
    args = parser.parse_args()
    report = build(args.output_dir)
    print(json.dumps({
        "manifest": report["manifest"],
        "accepted_exact_ttm_loss_count": report["accepted_exact_ttm_loss_count"],
        "resolved_unique_signal_date_count": report["resolved_unique_signal_date_count"],
        "resolved_audit_observation_count": report["resolved_audit_observation_count"],
        "promotion_eligible": False,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
