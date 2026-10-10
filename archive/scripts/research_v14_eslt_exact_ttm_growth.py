#!/usr/bin/env python3
"""Build source-locked, research-only exact-TTM growth evidence for ESLT.

Elbit Systems is a foreign private issuer that reports consolidated U.S.-GAAP
results in USD.  Contemporaneous 6-K nine-month statements and the 2018 20-F
allow current and prior TTM revenue and issuer-attributable profit to be
derived without manufacturing a missing 2017Q4 or summing independently
reported quarters that differ slightly from later cumulative statements.
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


OUTPUT_DIR = Path("output/research_only/v14/eslt_exact_ttm_growth")
TICKER = "ESLT"
CIK = 1_027_664
CURRENCY = "USD"
SOURCE_SCALE = 1_000
ACCOUNTING_STANDARD = "US-GAAP_AS_REPORTED"
FISCAL_END = "2019-09-30"
AVAILABLE_DATE = "2019-11-26"
PIT_CUTOFF = "2020-02-28"
FETCHED_AT = "2026-08-23"
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}

BASELINE_BINDING = {
    "quarterly": (
        "output/research_only/v14/"
        "candidate_fundamentals_v14_batch_afya_legn_sdgr_companyfacts_allt_"
        "glng_allk_asnd_csiq_cron_iq_jamf_lx_iiiv_peri/quarterly.csv"
    ),
    "quarterly_sha256": (
        "0f6a6be2a22ea64c31203805061bb0408ef636b2eebc888178754a9e963d3c3d"
    ),
    "audit": (
        "output/research_only/v14/"
        "batch_afya_legn_sdgr_companyfacts_allt_glng_allk_asnd_csiq_cron_"
        "iq_jamf_lx_iiiv_peri_audit.json"
    ),
    "audit_sha256": (
        "5a73b0278d0a3b081169ca9e93546af77d1c8a783218ecef2a6cd6cdb891e317"
    ),
    "financial_priorities": (
        "output/research_only/v14/"
        "batch_afya_legn_sdgr_companyfacts_allt_glng_allk_asnd_csiq_cron_"
        "iq_jamf_lx_iiiv_peri_audit_financial_priorities.csv"
    ),
    "financial_priorities_sha256": (
        "5e032914a243bc4628af966b85400b2bb8d450925824bce9aaf1d9589362a815"
    ),
    "baseline_reason": "FOREIGN_PERIODIC_NO_10Q",
}

SOURCE_DOCUMENTS = {
    "6k_2018_11_20_q3": {
        "role": "prior_nine_month_operands_and_comparatives",
        "form": "6-K",
        "filed": "2018-11-20",
        "accession": "0001628280-18-014522",
        "document": "esltq320186k.htm",
        "local_path": "sources/eslt_2018q3_6k.htm",
        "expected_sha256": (
            "2d9e0fae08efdb5b2cd57ad25628ab50612877e0f972cc36e710884a6103acee"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1027664/"
            "000162828018014522/esltq320186k.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
        "audit_status": "UNAUDITED_9M_WITH_AUDITED_FY2017_COMPARATIVE",
    },
    "20f_2019_03_19_fy2018": {
        "role": "audited_annual_operands",
        "form": "20-F",
        "filed": "2019-03-19",
        "accession": "0001628280-19-003104",
        "document": "eslt1231201820-fdoc.htm",
        "local_path": "sources/eslt_2018_20f.htm",
        "expected_sha256": (
            "14759570f2ebb7211525c32cf11c85ba0d94dd7608b814d519543dcec138da6f"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1027664/"
            "000162828019003104/eslt1231201820-fdoc.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
        "audit_status": "AUDITED",
    },
    "6k_2019_11_26_q3": {
        "role": "latest_nine_month_operands_and_comparatives",
        "form": "6-K",
        "filed": AVAILABLE_DATE,
        "accession": "0001628280-19-014525",
        "document": "esltq320196k.htm",
        "local_path": "sources/eslt_2019q3_6k.htm",
        "expected_sha256": (
            "1cf8d078d238642e911b075b1ee6a2049f8d9661431be2561cb8a65193872b9c"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1027664/"
            "000162828019014525/esltq320196k.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
        "audit_status": "UNAUDITED_9M_WITH_AUDITED_FY2018_COMPARATIVE",
    },
}
ALLOWED_SOURCE_ACCESSIONS = {
    source["accession"] for source in SOURCE_DOCUMENTS.values()
}

SOURCE_TEXT_CHECKS = {
    "6k_2018_11_20_q3": (
        "ELBIT SYSTEMS REPORTS THIRD QUARTER 2018 RESULTS",
        "Unless otherwise stated, all financial data presented is GAAP financial data",
        "CONSOLIDATED STATEMENTS OF INCOME",
        "In thousands of US Dollars",
        "adopted the new revenue recognition accounting standard ASC 606",
        "using the modified retrospective approach",
        "prior to 2018 are presented",
        "prior revenue recognition standard, ASC 605",
    ),
    "20f_2019_03_19_fy2018": (
        "prepared in accordance with United States generally accepted accounting principles",
        "all financial information contained in this annual report is presented in U.S. dollars",
        "CONSOLIDATED STATEMENTS OF INCOME",
        "adopted ASC 606 using the modified retrospective method effective as of January 1, 2018",
    ),
    "6k_2019_11_26_q3": (
        "ELBIT SYSTEMS REPORTS THIRD QUARTER 2019 RESULTS",
        "Unless otherwise stated, all financial data presented is GAAP financial data",
        "CONSOLIDATED STATEMENTS OF INCOME",
        "In thousands of US Dollars",
        "adopted Accounting Standards Update (ASU) 2016-02, Leases (ASC 842)",
        "periods prior to January 1, 2019 are not restated",
    ),
}

NET_INCOME_LABEL = "Net income attributable to Elbit Systems Ltd.'s shareholders"
SOURCE_ROW_CHECKS = {
    "6k_2018_11_20_q3": (
        {
            "metric": "revenue",
            "line_item": "Revenues",
            "context_phrases": (
                "Nine Months Ended September 30",
                "Three Months Ended September 30",
                "Year Ended December 31",
                "2018", "2017", "Unaudited", "Audited",
            ),
            "periods": ("M9_2018", "M9_2017", "Q3_2018", "Q3_2017", "FY2017"),
            "expected_values": (2_605_844, 2_368_221, 895_150, 800_734, 3_377_825),
        },
        {
            "metric": "net_income",
            "line_item": NET_INCOME_LABEL,
            "context_phrases": (
                "Nine Months Ended September 30",
                "Three Months Ended September 30",
                "Year Ended December 31",
                "2018", "2017", "Unaudited", "Audited",
            ),
            "periods": ("M9_2018", "M9_2017", "Q3_2018", "Q3_2017", "FY2017"),
            "expected_values": (205_613, 169_696, 64_055, 61_477, 239_109),
        },
    ),
    "20f_2019_03_19_fy2018": (
        {
            "metric": "revenue",
            "line_item": "Revenues",
            "context_phrases": (
                "Year ended December 31", "2018", "2017", "2016",
                "Gross profit", "Earnings per share attributable",
            ),
            "periods": ("FY2018", "FY2017", "FY2016"),
            "expected_values": (3_683_684, 3_377_825, 3_260_219),
        },
        {
            "metric": "net_income",
            "line_item": NET_INCOME_LABEL,
            "context_phrases": (
                "Year ended December 31", "2018", "2017", "2016",
                "Gross profit", "Earnings per share attributable",
            ),
            "periods": ("FY2018", "FY2017", "FY2016"),
            "expected_values": (206_738, 239_109, 236_909),
        },
    ),
    "6k_2019_11_26_q3": (
        {
            "metric": "revenue",
            "line_item": "Revenues",
            "context_phrases": (
                "Nine Months Ended September 30",
                "Three Months Ended September 30",
                "Year Ended December 31",
                "2019", "2018", "Unaudited", "Audited",
            ),
            "periods": ("M9_2019", "M9_2018", "Q3_2019", "Q3_2018", "FY2018"),
            "expected_values": (3_186_894, 2_605_844, 1_101_190, 895_150, 3_683_684),
        },
        {
            "metric": "net_income",
            "line_item": NET_INCOME_LABEL,
            "context_phrases": (
                "Nine Months Ended September 30",
                "Three Months Ended September 30",
                "Year Ended December 31",
                "2019", "2018", "Unaudited", "Audited",
            ),
            "periods": ("M9_2019", "M9_2018", "Q3_2019", "Q3_2018", "FY2018"),
            "expected_values": (176_343, 205_613, 72_065, 64_055, 206_738),
        },
    ),
}

OPERANDS_USD_THOUSANDS = {
    "revenue": {
        "fy2017": 3_377_825,
        "m9_2017": 2_368_221,
        "m9_2018": 2_605_844,
        "fy2018": 3_683_684,
        "m9_2019": 3_186_894,
    },
    "net_income": {
        "fy2017": 239_109,
        "m9_2017": 169_696,
        "m9_2018": 205_613,
        "fy2018": 206_738,
        "m9_2019": 176_343,
    },
}

AUDIT_OBSERVATIONS = (
    ("liq2000000-age150-growth", "2020-02-28", 150),
    ("liq2000000-age365-growth", "2020-02-28", 365),
    ("liq2000000-age550-growth", "2020-02-28", 550),
)

POST_SIGNAL_EXCLUSIONS = (
    {
        "form": "6-K",
        "filed": "2020-03-25",
        "accession": "0001628280-20-004028",
        "document": "esltq420196k.htm",
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1027664/"
            "000162828020004028/esltq420196k.htm"
        ),
        "reason": "FY2019 results were filed 26 days after the signal",
    },
    {
        "form": "20-F",
        "filed": "2020-03-25",
        "accession": "0001628280-20-004030",
        "document": "eslt1231201920-fdoc.htm",
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1027664/"
            "000162828020004030/eslt1231201920-fdoc.htm"
        ),
        "reason": "FY2019 annual filing was 26 days after the signal",
    },
)


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_path(path: Path) -> str:
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
    raise RuntimeError(f"failed to download locked ESLT source: {url}") from last_error


def _normalize_text(value: str) -> str:
    return " ".join(
        value.replace("\xa0", " ")
        .replace("\u200b", " ")
        .replace("’", "'")
        .replace("−", "-")
        .split()
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


def _parse_source_rows(source_id: str, raw: bytes) -> dict[str, dict[str, int]]:
    soup = _source_soup(raw)
    document_text = _normalize_text(" ".join(soup.stripped_strings))
    for phrase in SOURCE_TEXT_CHECKS[source_id]:
        if _normalize_text(phrase) not in document_text:
            raise RuntimeError(f"ESLT source text changed for {source_id}: {phrase}")

    result = {}
    for check in SOURCE_ROW_CHECKS[source_id]:
        normalized_label = _normalize_text(check["line_item"])
        context = tuple(_normalize_text(item) for item in check["context_phrases"])
        expected_count = len(check["periods"])
        candidates = []
        for table in soup.find_all("table"):
            table_text = _normalize_text(" ".join(table.stripped_strings))
            if not all(item in table_text for item in context):
                continue
            for row in table.find_all("tr"):
                labels = [
                    _normalize_text(" ".join(cell.stripped_strings))
                    for cell in row.find_all(("td", "th"))
                ]
                first_label = next((item for item in labels if item), "")
                if first_label != normalized_label:
                    continue
                values = _row_numbers(row)
                if len(values) >= expected_count:
                    candidates.append(dict(zip(
                        check["periods"], values[-expected_count:], strict=True
                    )))
        if not candidates:
            raise RuntimeError(
                f"no unambiguous ESLT {check['metric']} row for {source_id}"
            )
        canonical = json.dumps(candidates[0], sort_keys=True)
        if any(json.dumps(item, sort_keys=True) != canonical for item in candidates):
            raise RuntimeError(f"conflicting ESLT {check['metric']} rows for {source_id}")
        result[check["metric"]] = candidates[0]
    return result


def validate_source_lock(sources: dict[str, dict] | None = None) -> None:
    documents = SOURCE_DOCUMENTS if sources is None else sources
    for source_id, source in documents.items():
        accession = source["accession"]
        if accession not in ALLOWED_SOURCE_ACCESSIONS:
            raise ValueError(f"unapproved ESLT source accession: {accession}")
        if source["filed"] > PIT_CUTOFF:
            raise ValueError(f"ESLT source {source_id} violates PIT cutoff")
        if source["currency"] != CURRENCY or source["scale"] != SOURCE_SCALE:
            raise ValueError(f"ESLT source {source_id} has mixed currency or scale")
        if source["accounting_standard"] != ACCOUNTING_STANDARD:
            raise ValueError(f"ESLT source {source_id} is not as-reported U.S. GAAP")
        accession_path = accession.replace("-", "")
        if f"/data/{CIK}/{accession_path}/" not in source["url"]:
            raise ValueError(f"ESLT source {source_id} URL does not lock CIK/accession")
        if not source["url"].endswith("/" + source["document"]):
            raise ValueError(f"ESLT source {source_id} URL does not lock document")
        relative_path = Path(source["local_path"])
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError(f"ESLT source {source_id} has unsafe local_path")
        if not re.fullmatch(r"[0-9a-f]{64}", source["expected_sha256"]):
            raise ValueError(f"ESLT source {source_id} has invalid SHA-256")
    if any(item["filed"] <= PIT_CUTOFF for item in POST_SIGNAL_EXCLUSIONS):
        raise ValueError("ESLT post-signal exclusion was available by the signal")


def verify_source_values(raw_by_source: dict[str, bytes]) -> list[dict]:
    if set(raw_by_source) != set(SOURCE_DOCUMENTS):
        raise ValueError("ESLT raw source set does not match source lock")
    parsed = {
        source_id: _parse_source_rows(source_id, raw)
        for source_id, raw in raw_by_source.items()
    }
    verified = []
    for source_id, checks in SOURCE_ROW_CHECKS.items():
        for check in checks:
            actual = parsed[source_id][check["metric"]]
            expected = dict(zip(
                check["periods"], check["expected_values"], strict=True
            ))
            if actual != expected:
                raise RuntimeError(
                    f"ESLT source row changed for {source_id}/{check['metric']}: "
                    f"parsed {actual}, expected {expected}"
                )
            verified.append({
                "source_id": source_id,
                "metric": check["metric"],
                "line_item": check["line_item"],
                "currency": CURRENCY,
                "scale": SOURCE_SCALE,
                "period_values": actual,
            })
    _validate_republication_identities(parsed)
    return verified


def _validate_republication_identities(parsed: dict[str, dict]) -> None:
    q3_2018 = parsed["6k_2018_11_20_q3"]
    annual = parsed["20f_2019_03_19_fy2018"]
    q3_2019 = parsed["6k_2019_11_26_q3"]
    for metric in ("revenue", "net_income"):
        if q3_2018[metric]["FY2017"] != annual[metric]["FY2017"]:
            raise RuntimeError(f"ESLT {metric} FY2017 comparative changed")
        if annual[metric]["FY2018"] != q3_2019[metric]["FY2018"]:
            raise RuntimeError(f"ESLT {metric} FY2018 comparative changed")
        if q3_2018[metric]["M9_2018"] != q3_2019[metric]["M9_2018"]:
            raise RuntimeError(f"ESLT {metric} 9M 2018 comparative changed")


def prepare_verified_sources(
    output_dir: Path,
) -> tuple[dict[str, dict], list[dict]]:
    validate_source_lock()
    provenance = {}
    raw_by_source = {}
    for source_id, source in SOURCE_DOCUMENTS.items():
        local_path = Path(output_dir) / source["local_path"]
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
                f"ESLT source SHA-256 mismatch for {source_id}: {actual_sha256}"
            )
        raw_by_source[source_id] = raw
        provenance[source_id] = {
            **source,
            "local_path": str(local_path),
            "actual_sha256": actual_sha256,
            "bytes": len(raw),
            "downloaded": downloaded,
        }
    return provenance, verify_source_values(raw_by_source)


def _growth(current: int, prior: int) -> float:
    if prior <= 0:
        raise RuntimeError("ESLT growth denominator must be positive")
    return current / prior - 1.0


def exact_ttm_evidence() -> dict:
    validate_source_lock()
    derived = {}
    for metric, values in OPERANDS_USD_THOUSANDS.items():
        prior_ttm = values["fy2017"] - values["m9_2017"] + values["m9_2018"]
        current_ttm = values["fy2018"] - values["m9_2018"] + values["m9_2019"]
        derived[metric] = {
            "prior_ttm_usd_thousands": prior_ttm,
            "current_ttm_usd_thousands": current_ttm,
            "growth": _growth(current_ttm, prior_ttm),
            "prior_formula": "FY2017 - M9_2017 + M9_2018",
            "current_formula": "FY2018 - M9_2018 + M9_2019",
        }
    if derived["revenue"]["prior_ttm_usd_thousands"] != 3_615_448:
        raise RuntimeError("ESLT prior revenue TTM changed")
    if derived["revenue"]["current_ttm_usd_thousands"] != 4_264_734:
        raise RuntimeError("ESLT current revenue TTM changed")
    if derived["net_income"]["prior_ttm_usd_thousands"] != 275_026:
        raise RuntimeError("ESLT prior net-income TTM changed")
    if derived["net_income"]["current_ttm_usd_thousands"] != 177_468:
        raise RuntimeError("ESLT current net-income TTM changed")
    if AVAILABLE_DATE != max(source["filed"] for source in SOURCE_DOCUMENTS.values()):
        raise RuntimeError("ESLT availability date changed")
    return {
        "ticker": TICKER,
        "fiscal_end": FISCAL_END,
        "available_date": AVAILABLE_DATE,
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
        "fiscal_calendar": "calendar year; nine-month periods end September 30",
        "metric_mapping": {
            "revenue": "consolidated Revenues",
            "net_income": (
                "Net income attributable to Elbit Systems Ltd.'s shareholders; "
                "matches the existing ESLT_SEC_6K quarterly layer and annual "
                "us-gaap:NetIncomeLoss facts"
            ),
        },
        "operands_usd_thousands": OPERANDS_USD_THOUSANDS,
        "derived": derived,
        "operand_accessions": [
            SOURCE_DOCUMENTS[source_id]["accession"]
            for source_id in SOURCE_DOCUMENTS
        ],
        "operand_urls": [
            SOURCE_DOCUMENTS[source_id]["url"] for source_id in SOURCE_DOCUMENTS
        ],
        "restatement_isolation": (
            "FY2017 values in the 2018Q3 6-K match the 2018 20-F; FY2018 and "
            "9M 2018 values in the 2019Q3 6-K match the original 20-F/6-K. "
            "No amendment or post-signal annual report is used."
        ),
        "accounting_policy_comparability": {
            "status": "EXACT_AS_REPORTED_US_GAAP_NOT_CONSTANT_POLICY_BASIS",
            "asc_606": (
                "ASC 606 was adopted 2018-01-01 using modified retrospective; "
                "pre-2018 revenue remained ASC 605, and the issuer states the "
                "change influenced part of 2018 revenue growth."
            ),
            "asc_842": (
                "ASC 842 was adopted 2019-01-01 using modified retrospective; "
                "comparatives were not restated and the issuer disclosed a "
                "$21.1m increase in 9M 2019 financial expenses from FX on lease "
                "liabilities."
            ),
            "use_boundary": (
                "The package is exact as reported and suitable only for the "
                "unfrozen research audit. It is not a policy-normalized economic "
                "growth series and is not promotion eligible."
            ),
        },
    }


def strict_quarterly_facts() -> pd.DataFrame:
    evidence = exact_ttm_evidence()
    accession = "+".join(evidence["operand_accessions"])
    rows = []
    concepts = {
        "revenue": "RevenueFromContractWithCustomerExcludingAssessedTax",
        "net_income": "NetIncomeLoss",
    }
    for metric, values in evidence["derived"].items():
        for output_metric, value in (
            (f"{metric}_ttm", values["current_ttm_usd_thousands"] * SOURCE_SCALE),
            (f"{metric}_growth", values["growth"]),
        ):
            rows.append({
                "ticker": TICKER,
                "fiscal_end": FISCAL_END,
                "available_date": AVAILABLE_DATE,
                "metric": output_metric,
                "value": value,
                "taxonomy": "us-gaap",
                "concept": f"eslt_exact_m9_ttm:{concepts[metric]}:{CURRENCY}",
                "form": "20-F_PLUS_6-K_M9_CUMULATIVE_TTM",
                "accession": accession,
                "fetched_at": FETCHED_AT,
            })
    return (
        pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
        .sort_values("metric")
        .reset_index(drop=True)
    )


def resolve_observation(signal_date: str, maximum_age_days: int) -> dict:
    signal = pd.Timestamp(signal_date)
    available = pd.Timestamp(AVAILABLE_DATE)
    financial_age_days = int((signal - available).days)
    if signal < available or financial_age_days > maximum_age_days:
        return {
            "resolved": False,
            "decision": "missing_financial",
            "reason": "exact TTM growth package is outside PIT/age limit",
        }
    evidence = exact_ttm_evidence()["derived"]
    return {
        "resolved": True,
        "decision": "complete_exact_as_reported_ttm_growth_bundle",
        "fiscal_end": FISCAL_END,
        "available_date": AVAILABLE_DATE,
        "financial_age_days": financial_age_days,
        "revenue_ttm": evidence["revenue"]["current_ttm_usd_thousands"] * SOURCE_SCALE,
        "revenue_growth": evidence["revenue"]["growth"],
        "net_income_ttm": (
            evidence["net_income"]["current_ttm_usd_thousands"] * SOURCE_SCALE
        ),
        "net_income_growth": evidence["net_income"]["growth"],
        "currency": CURRENCY,
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


def validate_exact_package() -> None:
    evidence = exact_ttm_evidence()["derived"]
    facts = strict_quarterly_facts()
    if set(facts["metric"]) != {
        "revenue_ttm", "revenue_growth", "net_income_ttm", "net_income_growth"
    }:
        raise RuntimeError("ESLT direct growth package is incomplete")
    if evidence["revenue"]["current_ttm_usd_thousands"] <= 0:
        raise RuntimeError("ESLT revenue TTM must be positive")
    if evidence["net_income"]["current_ttm_usd_thousands"] <= 0:
        raise RuntimeError("ESLT net-income TTM must be positive")


def build(output_dir: Path = OUTPUT_DIR) -> dict:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    sources, source_value_verification = prepare_verified_sources(output_dir)
    validate_exact_package()
    facts = strict_quarterly_facts()
    evidence = exact_ttm_evidence()
    resolutions = resolve_audit_observations()
    if not resolutions["resolved"].all():
        raise RuntimeError("not every declared ESLT observation resolved")

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
        "accounting_policy_normalized": False,
        "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "shared_candidate_integrated": False,
        "ticker": TICKER,
        "cik": CIK,
        "currency": CURRENCY,
        "accounting_standard": ACCOUNTING_STANDARD,
        "security": (
            "Elbit Systems Ltd. ordinary shares listed on Nasdaq and TASE; "
            "consolidated issuer USD amounts, not EPS or per-share values"
        ),
        "reporting_profile": "ISRAELI_FOREIGN_PRIVATE_ISSUER_20-F_6-K",
        "baseline_binding": BASELINE_BINDING,
        "accepted_direct_growth_package_count": 1,
        "accepted_fact_count": len(facts),
        "resolved_audit_observation_count": len(resolutions),
        "resolved_unique_signal_date_count": resolutions["signal_date"].nunique(),
        "source_documents": sources,
        "source_value_verification": source_value_verification,
        "accounting_policy_comparability": evidence[
            "accounting_policy_comparability"
        ],
        "post_signal_exclusions": POST_SIGNAL_EXCLUSIONS,
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path), "sha256": _sha256_path(facts_path)
            },
            "exact_ttm_evidence": {
                "path": str(evidence_path), "sha256": _sha256_path(evidence_path)
            },
            "audit_observation_resolution": {
                "path": str(resolution_path), "sha256": _sha256_path(resolution_path)
            },
        },
        "guardrail": (
            "All operands are exact consolidated USD-thousand as-reported U.S.-GAAP "
            "annual or nine-month cumulative values, latest filed 2019-11-26 "
            "(94 days before the signal). Profit is consistently attributable to "
            "Elbit Systems Ltd. shareholders. Comparative republications are exact. "
            "ASC 606/842 modified-retrospective discontinuities are disclosed and "
            "make this research-only package unsuitable for automatic promotion. "
            "No quarter, currency conversion, later filing, or formal fact is made up."
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
        "accepted_direct_growth_package_count": (
            report["accepted_direct_growth_package_count"]
        ),
        "resolved_audit_observation_count": (
            report["resolved_audit_observation_count"]
        ),
        "promotion_eligible": False,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
