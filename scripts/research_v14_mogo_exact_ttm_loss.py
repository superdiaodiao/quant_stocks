#!/usr/bin/env python3
"""Build source-locked, research-only exact-TTM loss evidence for MOGO.

Mogo was a Canadian foreign private issuer whose Nasdaq security was common
shares, not an ADS.  This supplement accepts only consolidated IFRS amounts in
Canadian dollars that were filed before the 2020-12-31 signal.  It emits one
exact negative TTM profit fact, sufficient only to classify MOGO as known
non-positive-profit; it cannot manufacture a quarter, revenue, or growth fact.
"""

from __future__ import annotations

import argparse
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
import hashlib
from http.client import IncompleteRead
import json
from pathlib import Path
import re
from typing import Iterable
from urllib.error import URLError
from urllib.request import Request, urlopen
import warnings

import pandas as pd

from src.io.fundamentals_update import OUTPUT_COLUMNS


OUTPUT_DIR = Path("output/research_only/v14/mogo_exact_ttm_loss")
TICKER = "MOGO"
CIK = 1_602_842
CURRENCY = "CAD"
SOURCE_SCALE = 1_000
ACCOUNTING_STANDARD = "IFRS-IASB"
PIT_CUTOFF = "2020-12-31"
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
BASELINE_BINDING = {
    "quarterly": (
        "output/research_only/v14/"
        "candidate_fundamentals_v14_batch_afya_legn_sdgr_companyfacts_allt_"
        "glng_allk_asnd_csiq_cron_iq_jamf_lx_iiiv_peri_uxin_gain_azpn_li/"
        "quarterly.csv"
    ),
    "quarterly_sha256": (
        "a605ab2745a6670b392d9845edbcded37d5292e638d1a5892f311ec5b320e015"
    ),
    "audit": (
        "output/research_only/v14/"
        "batch_afya_legn_sdgr_companyfacts_allt_glng_allk_asnd_csiq_cron_iq_"
        "jamf_lx_iiiv_peri_uxin_gain_azpn_li_audit.json"
    ),
    "audit_sha256": (
        "224d2102d5e01deed49276a2c6389ac264d40dacd100659dbdb025bc32f69b24"
    ),
    "priorities": (
        "output/research_only/v14/"
        "batch_afya_legn_sdgr_companyfacts_allt_glng_allk_asnd_csiq_cron_iq_"
        "jamf_lx_iiiv_peri_uxin_gain_azpn_li_audit_financial_priorities.csv"
    ),
    "priorities_sha256": (
        "82220c640bed35db39e983d2856d87fe5db1a32089edd4680b381fbb2f7c1539"
    ),
    "baseline_reason": "foreign_periodic_no_10q",
}

SOURCE_DOCUMENTS = {
    "20f_2020_05_28_fy2019": {
        "form": "20-F",
        "filed": "2020-05-28",
        "accession": "0001477932-20-003100",
        "document": "mogo_20f.htm",
        "description": "FORM 20-F; audited FY2019 consolidated statements",
        "local_path": "sources/mogo_2019_20f.htm",
        "expected_sha256": (
            "488c8651968e5053ccd6022fc34774e1a669ff49f01cba38ca6dc33ba0c0c583"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1602842/"
            "000147793220003100/mogo_20f.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
        "audit_status": "AUDITED",
    },
    "6k_2020_11_10_q3_fs_ex991": {
        "form": "6-K/EX-99.1",
        "filed": "2020-11-10",
        "accession": "0001477932-20-006358",
        "document": "mogo_ex991.htm",
        "description": "FINANCIAL STATEMENTS; Q3/9M interim statements",
        "local_path": "sources/mogo_2020_q3_6k_exhibit_99-1.htm",
        "expected_sha256": (
            "2fb690f145b2879262b00a231015327812346919a32d2813ece48cdf465066c8"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1602842/"
            "000147793220006358/mogo_ex991.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
        "audit_status": "UNAUDITED",
    },
    "6k_2020_11_10_q3_mda_ex992": {
        "form": "6-K/EX-99.2",
        "filed": "2020-11-10",
        "accession": "0001477932-20-006358",
        "document": "mogo_ex992.htm",
        "description": "MANAGEMENT'S DISCUSSION AND ANALYSIS; Q3 2020",
        "local_path": "sources/mogo_2020_q3_6k_exhibit_99-2.htm",
        "expected_sha256": (
            "f717852aeffb2f924e41726f4545df0e8b0caefe690e23e49a1d02f4b263ae74"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1602842/"
            "000147793220006358/mogo_ex992.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
        "audit_status": "ISSUER_MDA_DERIVED_FROM_INTERIM_IFRS_STATEMENTS",
    },
}
ALLOWED_SOURCE_ACCESSIONS = {
    source["accession"] for source in SOURCE_DOCUMENTS.values()
}

ACCOUNTING_POLICY_AUDIT = {
    "consolidation_scope": (
        "The interim statements consolidate Mogo Inc. and all direct and "
        "indirect wholly-owned subsidiaries and its structured entity; no "
        "non-controlling-interest allocation is presented."
    ),
    "q3_2019_purchase_price_adjustment": {
        "application": "RETROSPECTIVE_IN_PRE_SIGNAL_2020_Q3_COMPARATIVE",
        "gain_previously_reported_cad_thousands": -14_349,
        "gain_revised_cad_thousands": -13_249,
        "reason": "finalization of business-combination purchase price allocation",
        "accepted_nine_month_loss_cad_thousands": -4_637,
    },
    "loan_protection_recast": {
        "application": "RETROSPECTIVE_PRESENTATION_RECAST",
        "nine_month_2019_revenue_reduction_cad_thousands": 4_527,
        "profit_effect": "NO_IMPACT_ON_GROSS_PROFIT_OR_NET_LOSS",
    },
    "ifrs16_comparability": (
        "Every accepted direct quarter is Q4 2019 or later and therefore is "
        "within Mogo's post-adoption IFRS 16 period."
    ),
    "later_restatement_backfill": False,
}

POST_SIGNAL_EXCLUSIONS = (
    {
        "form": "40-F",
        "filed": "2021-03-26",
        "accession": "0001564590-21-015848",
        "document": "mogo-40f_20201231.htm",
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1602842/"
            "000156459021015848/mogo-40f_20201231.htm"
        ),
        "reason": "FY2020 annual report was filed after the signal and is not used",
    },
)

SOURCE_PARSE_SPECS = {
    "20f_2020_05_28_fy2019": {
        "identity_phrases": (
            "FORM 20-F",
            "For the fiscal year ended December 31, 2019",
            "Expressed in thousands of Canadian Dollars",
            "International Financial Reporting Standards as issued by the International Accounting Standards Board",
            "Net loss and comprehensive loss",
        ),
        "row_specs": {
            "profit_loss": {
                "context_phrases": (
                    "For the years ended December 31",
                    "Revenue",
                    "Cost of revenue",
                    "Gain on acquisition, net",
                    "Other expenses (income)",
                ),
                "columns": {
                    "FY2019": "FY2019",
                    "FY2018": "FY2018",
                    "FY2017": "FY2017",
                },
                "row_label": "Net loss and comprehensive loss",
            },
        },
    },
    "6k_2020_11_10_q3_fs_ex991": {
        "identity_phrases": (
            "Interim Condensed Consolidated Financial Statements (Unaudited)",
            "For the three and nine months ended September 30, 2020 and 2019",
            "Expressed in thousands of Canadian Dollars",
            "International Accounting Standards",
            "direct and indirect wholly-owned subsidiaries",
        ),
        "row_specs": {
            "profit_loss": {
                "context_phrases": (
                    "Three months ended",
                    "Nine months ended",
                    "Revenue",
                    "Gain on acquisition, net of transaction costs",
                    "Weighted average number of diluted shares",
                ),
                "columns": {
                    "Q3_2020": "Q3 2020",
                    "Q3_2019": "Q3 2019",
                    "M9_2020": "9M 2020",
                    "M9_2019": "9M 2019",
                },
                "row_label": (
                    "Net income (loss) and comprehensive income (loss)"
                ),
            },
        },
    },
    "6k_2020_11_10_q3_mda_ex992": {
        "identity_phrases": (
            "MANAGEMENT'S DISCUSSION AND ANALYSIS",
            "FOR THE QUARTER ENDED SEPTEMBER 30, 2020",
            "Selected Quarterly Information",
            "Income Statement Highlights",
            "prepared in accordance with International Financial Reporting Standards",
        ),
        "row_specs": {
            "profit_loss": {
                "context_phrases": (
                    "Third Quarter",
                    "Second Quarter",
                    "First Quarter",
                    "Fourth Quarter",
                    "Income Statement Highlights",
                    "Adjusted EBITDA",
                ),
                "columns": {
                    "Q3_2020": "Q3 2020",
                    "Q2_2020": "Q2 2020",
                    "Q1_2020": "Q1 2020",
                    "Q4_2019": "Q4 2019",
                    "Q3_2019": "Q3 2019",
                    "Q2_2019": "Q2 2019",
                    "Q1_2019": "Q1 2019",
                    "Q4_2018": "Q4 2018",
                },
                "row_label": (
                    "Net income (loss) and comprehensive income (loss)"
                ),
            },
        },
    },
}

SOURCE_VALUE_EXPECTATIONS = {
    "fy2019_profit_loss": {
        "source_id": "20f_2020_05_28_fy2019",
        "metric": "profit_loss",
        "table_column": "FY2019",
        "value": -10_825,
    },
    "m9_2019_profit_loss_revised": {
        "source_id": "6k_2020_11_10_q3_fs_ex991",
        "metric": "profit_loss",
        "table_column": "M9_2019",
        "value": -4_637,
    },
    "m9_2020_profit_loss": {
        "source_id": "6k_2020_11_10_q3_fs_ex991",
        "metric": "profit_loss",
        "table_column": "M9_2020",
        "value": -10_596,
    },
    "q4_2019_profit_loss": {
        "source_id": "6k_2020_11_10_q3_mda_ex992",
        "metric": "profit_loss",
        "table_column": "Q4_2019",
        "value": -6_188,
    },
    "q1_2020_profit_loss": {
        "source_id": "6k_2020_11_10_q3_mda_ex992",
        "metric": "profit_loss",
        "table_column": "Q1_2020",
        "value": -10_065,
    },
    "q2_2020_profit_loss": {
        "source_id": "6k_2020_11_10_q3_mda_ex992",
        "metric": "profit_loss",
        "table_column": "Q2_2020",
        "value": -1_550,
    },
    "q3_2020_profit_loss": {
        "source_id": "6k_2020_11_10_q3_mda_ex992",
        "metric": "profit_loss",
        "table_column": "Q3_2020",
        "value": 1_019,
    },
}

OPERANDS_CAD_THOUSANDS = {
    item_id: {
        **item,
        "period": item_id.split("_profit_loss")[0].upper(),
        "line_item": "Net income (loss) and comprehensive income (loss)",
    }
    for item_id, item in SOURCE_VALUE_EXPECTATIONS.items()
}

TTM_SPEC = {
    "fiscal_end": "2020-09-30",
    "available_date": "2020-11-10",
    "formula": "Q4_2019 + Q1_2020 + Q2_2020 + Q3_2020",
    "equivalent_formula": "FY2019 - 9M_2019_revised + 9M_2020",
    "terms": (
        (1, "q4_2019_profit_loss"),
        (1, "q1_2020_profit_loss"),
        (1, "q2_2020_profit_loss"),
        (1, "q3_2020_profit_loss"),
    ),
    "expected_cad_thousands": -16_784,
    "form": "6-K_EX-99.2_QUARTERLY_TTM_WITH_20-F_6-K_CROSSCHECK",
}

AUDIT_OBSERVATIONS = (
    ("liq2000000-age150-growth", "2020-12-31", 150),
    ("liq2000000-age365-growth", "2020-12-31", 365),
    ("liq2000000-age550-growth", "2020-12-31", 550),
)


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _download_source(url: str) -> bytes:
    last_error = None
    for _attempt in range(3):
        try:
            with urlopen(Request(url, headers=SEC_HEADERS), timeout=120) as response:
                return response.read()
        except (IncompleteRead, TimeoutError, URLError) as exc:
            last_error = exc
    raise RuntimeError(f"MOGO SEC source download failed after 3 attempts: {url}") from last_error


def _normalize_text(value: str) -> str:
    return " ".join(
        value.replace("\xa0", " ")
        .replace("−", "-")
        .replace("’", "'")
        .replace("‑", "-")
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


def _parse_source_tables(source_id: str, raw: bytes) -> dict[str, dict]:
    spec = SOURCE_PARSE_SPECS[source_id]
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
    soup = BeautifulSoup(raw, "lxml")
    document_text = _normalize_text(" ".join(soup.stripped_strings))
    if any(
        _normalize_text(phrase) not in document_text
        for phrase in spec["identity_phrases"]
    ):
        raise RuntimeError(f"MOGO source identity changed for {source_id}")
    parsed = {}
    for metric, row_spec in spec["row_specs"].items():
        context = tuple(
            _normalize_text(item) for item in row_spec["context_phrases"]
        )
        normalized_label = _normalize_text(row_spec["row_label"])
        expected_count = len(row_spec["columns"])
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
                        row_spec["columns"],
                        values[-expected_count:],
                        strict=True,
                    )))
        if not candidates:
            raise RuntimeError(
                f"no unambiguous MOGO {metric} table for {source_id}"
            )
        canonical = json.dumps(candidates[0], sort_keys=True)
        if any(json.dumps(item, sort_keys=True) != canonical for item in candidates):
            raise RuntimeError(f"conflicting MOGO {metric} tables for {source_id}")
        parsed[metric] = candidates[0]
    return parsed


def validate_source_lock(sources: dict[str, dict] | None = None) -> None:
    documents = SOURCE_DOCUMENTS if sources is None else sources
    for source_id, source in documents.items():
        accession = source["accession"]
        if accession not in ALLOWED_SOURCE_ACCESSIONS:
            raise ValueError(f"unapproved source accession: {accession}")
        if source["filed"] > PIT_CUTOFF:
            raise ValueError(f"source {source_id} was filed after PIT cutoff")
        if source["currency"] != CURRENCY or source["scale"] != SOURCE_SCALE:
            raise ValueError(f"source {source_id} has mixed currency or scale")
        if source["accounting_standard"] != ACCOUNTING_STANDARD:
            raise ValueError(f"source {source_id} is not IFRS-IASB")
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
    for item_id, item in OPERANDS_CAD_THOUSANDS.items():
        if item["source_id"] not in documents:
            raise ValueError(f"source value {item_id} has no locked source")
    if any(item["filed"] <= PIT_CUTOFF for item in POST_SIGNAL_EXCLUSIONS):
        raise ValueError("post-signal exclusion was available by the signal")


def verify_source_values(raw_by_source: dict[str, bytes]) -> list[dict]:
    if set(raw_by_source) != set(SOURCE_DOCUMENTS):
        raise ValueError("raw source set does not match the source lock")
    parsed = {
        source_id: _parse_source_tables(source_id, raw)
        for source_id, raw in raw_by_source.items()
    }
    verified = []
    for item_id, item in SOURCE_VALUE_EXPECTATIONS.items():
        parsed_value = parsed[item["source_id"]][item["metric"]][
            item["table_column"]
        ]
        expected_value = int(item["value"])
        if parsed_value != expected_value:
            raise RuntimeError(
                f"source value {item_id} changed: parsed {parsed_value}, "
                f"expected {expected_value}"
            )
        verified.append({
            "item_id": item_id,
            "source_id": item["source_id"],
            "metric": item["metric"],
            "table_column": item["table_column"],
            "currency": CURRENCY,
            "scale": SOURCE_SCALE,
            "expected_value": expected_value,
            "parsed_value": parsed_value,
        })

    fy2019 = parsed["20f_2020_05_28_fy2019"]["profit_loss"]["FY2019"]
    m9_2019 = parsed["6k_2020_11_10_q3_fs_ex991"]["profit_loss"]["M9_2019"]
    m9_2020 = parsed["6k_2020_11_10_q3_fs_ex991"]["profit_loss"]["M9_2020"]
    quarters = parsed["6k_2020_11_10_q3_mda_ex992"]["profit_loss"]
    if fy2019 - m9_2019 != quarters["Q4_2019"]:
        raise RuntimeError("MOGO FY2019 minus revised 9M2019 does not equal Q4 2019")
    if sum(quarters[key] for key in ("Q1_2020", "Q2_2020", "Q3_2020")) != m9_2020:
        raise RuntimeError("MOGO 2020 direct quarters do not equal 9M2020")
    return verified


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
                f"MOGO source SHA-256 mismatch for {source_id}: {actual_sha256}"
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
    value_thousands = sum(
        coefficient * int(OPERANDS_CAD_THOUSANDS[operand_id]["value"])
        for coefficient, operand_id in TTM_SPEC["terms"]
    )
    if value_thousands != TTM_SPEC["expected_cad_thousands"]:
        raise RuntimeError("MOGO exact TTM changed")
    cumulative_value = (
        OPERANDS_CAD_THOUSANDS["fy2019_profit_loss"]["value"]
        - OPERANDS_CAD_THOUSANDS["m9_2019_profit_loss_revised"]["value"]
        + OPERANDS_CAD_THOUSANDS["m9_2020_profit_loss"]["value"]
    )
    if cumulative_value != value_thousands:
        raise RuntimeError("MOGO quarterly and cumulative TTM identities differ")
    if value_thousands >= 0:
        raise RuntimeError("MOGO direct exact-TTM layer is exclusion-only")
    source_ids = list(SOURCE_DOCUMENTS)
    sources = [SOURCE_DOCUMENTS[source_id] for source_id in source_ids]
    if TTM_SPEC["available_date"] != max(source["filed"] for source in sources):
        raise RuntimeError("MOGO availability date changed")
    accessions = list(dict.fromkeys(source["accession"] for source in sources))
    return [{
        "ticker": TICKER,
        "evidence_kind": "exact_direct_quarter_ttm_loss_as_reported",
        "fiscal_end": TTM_SPEC["fiscal_end"],
        "available_date": TTM_SPEC["available_date"],
        "currency": CURRENCY,
        "source_scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
        "net_income_ttm": value_thousands * SOURCE_SCALE,
        "formula": TTM_SPEC["formula"],
        "equivalent_formula": TTM_SPEC["equivalent_formula"],
        "operand_ids": [operand_id for _, operand_id in TTM_SPEC["terms"]],
        "source_ids": source_ids,
        "source_accessions": accessions,
        "source_urls": [source["url"] for source in sources],
        "form": TTM_SPEC["form"],
        "profit_scope": (
            "consolidated issuer IFRS net income/loss and comprehensive "
            "income/loss for Mogo Inc. and wholly-owned subsidiaries; CAD "
            "amount, not EPS, adjusted EBITDA, or adjusted cash net income"
        ),
    }]


def direct_ttm_facts(fetched_at: str | None = None) -> pd.DataFrame:
    if fetched_at is None:
        fetched_at = str(
            pd.Timestamp.now("UTC").tz_localize(None).normalize().date()
        )
    rows = [{
        "ticker": TICKER,
        "fiscal_end": evidence["fiscal_end"],
        "available_date": evidence["available_date"],
        "metric": "net_income_ttm",
        "value": evidence["net_income_ttm"],
        "taxonomy": "ifrs-full",
        "concept": "mogo_exact_ttm:ProfitLoss:CAD",
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
        raise RuntimeError("not every declared MOGO audit observation resolved")

    facts_path = output_dir / "strict_quarterly_facts.csv"
    evidence_path = output_dir / "exact_ttm_evidence.json"
    resolution_path = output_dir / "audit_observation_resolution.json"
    facts.to_csv(facts_path, index=False)
    evidence_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    resolution_path.write_text(
        resolutions.to_json(orient="records", indent=2) + "\n",
        encoding="utf-8",
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
            "Mogo Inc. common shares listed on Nasdaq and TSX; consolidated "
            "issuer CAD amounts, not ADS or EPS"
        ),
        "reporting_profile": "CANADIAN_FOREIGN_PRIVATE_ISSUER_20-F_6-K_AT_SIGNAL",
        "baseline_binding": BASELINE_BINDING,
        "accepted_exact_ttm_loss_count": len(facts),
        "resolved_unique_signal_date_count": resolutions["signal_date"].nunique(),
        "resolved_audit_observation_count": len(resolutions),
        "source_documents": provenance,
        "source_value_verification": source_value_verification,
        "accounting_policy_audit": ACCOUNTING_POLICY_AUDIT,
        "post_signal_exclusions": POST_SIGNAL_EXCLUSIONS,
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
            "The four direct quarters are consolidated IFRS issuer amounts in "
            "CAD thousands from the 2020-11-10 Q3 MD&A. FY2019 minus the "
            "pre-signal revised 9M2019 comparison equals Q4 2019, and the "
            "three 2020 quarters equal filed 9M2020. The latest source was "
            "filed 51 days before the signal. The 2021-03-26 annual report is "
            "excluded. This layer cannot create quarterly growth."
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
