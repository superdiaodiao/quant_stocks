#!/usr/bin/env python3
"""Recover FORR's 2018Q3 TTM growth on a comparable ASC 605 basis.

Forrester adopted ASC 606 on 2018-01-01 using the modified retrospective
method, so its 2018 as-reported revenue and profit cannot safely be compared
with the unadjusted 2017 periods.  The timely 2018Q3 10-Q supplies exact
issuer-calculated nine-month amounts as if the previous ASC 605 guidance had
remained in effect.  Those amounts, the 2017 10-K, and the original 2017Q3
10-Q form one source-locked direct-TTM growth bundle without cross-basis
arithmetic, estimates, non-GAAP adjustments, or later filings.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
from pathlib import Path
import re
import shutil
import time
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
import pandas as pd

from src.io.fundamentals_update import OUTPUT_COLUMNS


TICKER = "FORR"
CIK = 1_023_313
CURRENCY = "USD"
SOURCE_SCALE = 1_000
ACCOUNTING_STANDARD = "US-GAAP_ASC605_COMPARABLE_BASIS"
FISCAL_END = "2018-09-30"
AVAILABLE_DATE = "2018-11-06"
SIGNAL_DATE = "2019-02-28"
FETCHED_AT = "2026-08-29"
OUTPUT_DIR = Path("output/research_only/v14/forr_asc605_ttm_growth")
SEC_HEADERS = {"User-Agent": "quant-stocks-research contact@example.com"}

BASE_CANDIDATE_DIR = Path(
    "output/research_only/v14/"
    "candidate_fundamentals_v14_checkpoint_20260828_dkng_2020q2_ttm"
)
BASELINE_AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260828_dkng_2020q2_recovered_financial_priorities.csv"
)
EXPECTED_BASELINE_AUDIT_SHA256 = (
    "31f84e8feb0e9af45dbd8c680b565f3231c2aa35003b41e05bd38f82f9ee18d9"
)
CURRENT_AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260829_forr_asc605_ttm_recovered_financial_priorities.csv"
)
EXPECTED_CURRENT_AUDIT_SHA256 = (
    "61187e04add06ca401a181636b575d77148c4377fbdfe362afb98c992dfc8c1f"
)

SOURCE_DOCUMENTS = {
    "2017_q3_10q": {
        "role": "original_nine_month_2017_cross_check",
        "form": "10-Q",
        "filed": "2017-11-07",
        "accession": "0001564590-17-022209",
        "document": "forr-10q_20170930.htm",
        "expected_sha256": (
            "4665d1779f7cc8cc9b253a53a5907b3a9c481d1fcfbedf236dbcbbd83eb43491"
        ),
    },
    "2017_10k": {
        "role": "audited_annual_and_selected_quarterly_operands",
        "form": "10-K",
        "filed": "2018-03-09",
        "accession": "0001564590-18-005110",
        "document": "forr-10k_20171231.htm",
        "expected_sha256": (
            "4dd197f34a68ee42b7032b83dece392f31b0baa3b8745c00ec7af77b6a30400a"
        ),
    },
    "2018_q3_10q": {
        "role": "asc606_transition_and_previous_guidance_m9_operands",
        "form": "10-Q",
        "filed": AVAILABLE_DATE,
        "accession": "0001564590-18-027383",
        "document": "forr-10q_20180930.htm",
        "expected_sha256": (
            "a8ca7a7b16cec42c5a7951e4c5564d6e8d44212bd7e024bf551d943b8f43ee92"
        ),
    },
}

SOURCE_TEXT_CHECKS = {
    "2017_q3_10q": (
        "FOR THE QUARTERLY PERIOD ENDED September 30, 2017",
        "CONSOLIDATED STATEMENTS OF INCOME",
        "Nine Months Ended",
        "accounting principles generally accepted in the United States of America",
    ),
    "2017_10k": (
        "For the fiscal year ended December 31, 2017",
        "CONSOLIDATED STATEMENTS OF INCOME",
        "selected unaudited consolidated quarterly financial data",
    ),
    "2018_q3_10q": (
        "FOR THE QUARTERLY PERIOD ENDED September 30, 2018",
        "adopted ASC 606 using the modified retrospective method",
        "reported results for 2018 reflect the application of ASC 606",
        "reported results for 2017 were prepared under the guidance of ASC 605",
        "Amounts as if Previous Guidance",
    ),
}

SOURCE_ROW_CHECKS = {
    "2017_q3_10q": (
        {
            "check_id": "m9_2017_revenue",
            "context": (
                "Three Months Ended", "Nine Months Ended", "September 30",
                "Basic income per common share",
            ),
            "row_label": "Total revenues",
            "expected": (80_369, 77_427, 247_296, 242_649),
        },
        {
            "check_id": "m9_2017_net_income",
            "context": (
                "Three Months Ended", "Nine Months Ended", "September 30",
                "Basic income per common share",
            ),
            "row_label": "Net income",
            "expected": (3_953, 3_112, 13_047, 11_861),
        },
    ),
    "2017_10k": (
        {
            "check_id": "annual_revenue",
            "context": (
                "Years Ended December 31", "2017", "2016", "2015",
                "Basic income per common share", "Cost of services and fulfillment",
            ),
            "row_label": "Total revenues",
            "expected": (337_673, 326_095, 313_726),
        },
        {
            "check_id": "annual_net_income",
            "context": (
                "Years Ended December 31", "2017", "2016", "2015",
                "Basic income per common share", "Cost of services and fulfillment",
            ),
            "row_label": "Net income",
            "expected": (15_140, 17_651, 11_996),
        },
        {
            "check_id": "2017_quarterly_revenue",
            "context": (
                "Three Months Ended", "March 31", "June 30", "September 30",
                "December 31", "2017", "Basic income per common share",
            ),
            "row_label": "Total revenues",
            "expected": (77_194, 89_733, 80_369, 90_377),
        },
        {
            "check_id": "2017_quarterly_net_income",
            "context": (
                "Three Months Ended", "March 31", "June 30", "September 30",
                "December 31", "2017", "Basic income per common share",
            ),
            "row_label": "Net income",
            "expected": (3_030, 6_064, 3_953, 2_093),
        },
        {
            "check_id": "2016_quarterly_revenue",
            "context": (
                "Three Months Ended", "March 31", "June 30", "September 30",
                "December 31", "2016", "Basic income per common share",
            ),
            "row_label": "Total revenues",
            "expected": (77_401, 87_821, 77_427, 83_446),
        },
        {
            "check_id": "2016_quarterly_net_income",
            "context": (
                "Three Months Ended", "March 31", "June 30", "September 30",
                "December 31", "2016", "Basic income per common share",
            ),
            "row_label": "Net income",
            "expected": (1_289, 7_460, 3_112, 5_790),
        },
    ),
    "2018_q3_10q": (
        {
            "check_id": "as_reported_revenue",
            "context": (
                "Three Months Ended", "Nine Months Ended", "September 30",
                "Basic income per common share",
            ),
            "row_label": "Total revenues",
            "expected": (84_890, 80_369, 258_992, 247_296),
        },
        {
            "check_id": "as_reported_net_income",
            "context": (
                "Three Months Ended", "Nine Months Ended", "September 30",
                "Basic income per common share",
            ),
            "row_label": "Net income",
            "expected": (3_950, 3_953, 10_005, 13_047),
        },
        {
            "check_id": "previous_guidance_m9_revenue",
            "context": (
                "Consolidated Statement of Income",
                "Nine Months Ended September 30, 2018",
                "Amounts as if Previous Guidance", "As Reported", "Effect",
            ),
            "row_label": "Total revenues",
            "expected": (258_992, 260_480),
        },
        {
            "check_id": "previous_guidance_m9_net_income",
            "context": (
                "Consolidated Statement of Income",
                "Nine Months Ended September 30, 2018",
                "Amounts as if Previous Guidance", "As Reported", "Effect",
            ),
            "row_label": "Net income",
            "expected": (10_005, 10_980),
        },
    ),
}

OPERANDS_USD_THOUSANDS = {
    "revenue": {
        "q4_2016": 83_446,
        "m9_2017": 247_296,
        "q4_2017": 90_377,
        "m9_2018_previous_guidance": 260_480,
    },
    "net_income": {
        "q4_2016": 5_790,
        "m9_2017": 13_047,
        "q4_2017": 2_093,
        "m9_2018_previous_guidance": 10_980,
    },
}

EXPECTED_TTM = {
    "revenue": {"prior": 330_742, "current": 350_857},
    "net_income": {"prior": 18_837, "current": 13_073},
}
DIRECT_METRICS = frozenset({
    "revenue_ttm", "revenue_growth", "net_income_ttm", "net_income_growth"
})
AUDIT_OBSERVATIONS = (
    ("liq2000000-age150-growth", 150),
    ("liq2000000-age365-growth", 365),
    ("liq2000000-age550-growth", 550),
)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _source_url(source: dict) -> str:
    return (
        f"https://www.sec.gov/Archives/edgar/data/{CIK}/"
        f"{source['accession'].replace('-', '')}/{source['document']}"
    )


def _download_bytes(url: str) -> bytes:
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            with urlopen(Request(url, headers=SEC_HEADERS), timeout=120) as response:
                return response.read()
        except (OSError, http.client.IncompleteRead) as error:
            last_error = error
            if attempt < 4:
                time.sleep(attempt + 1)
    raise RuntimeError(f"failed to download locked FORR source: {url}") from last_error


def _normalize_text(value: str) -> str:
    return " ".join(
        value.replace("\xa0", " ")
        .replace("\u200b", " ")
        .replace("’", "'")
        .split()
    ).casefold()


def _row_numbers(row) -> tuple[int, ...]:
    text = " ".join(row.stripped_strings).replace("\xa0", " ")
    text = re.sub(r"\s*,\s*", ",", text)
    values = []
    for token in re.findall(
        r"\(\s*\d[\d,]*\s*\)|(?<![\w.])\d[\d,]*(?![\w.])", text
    ):
        digits = re.sub(r"\D", "", token)
        if digits:
            value = int(digits)
            values.append(-value if "(" in token else value)
    return tuple(values)


def _parse_check(soup: BeautifulSoup, check: dict) -> tuple[int, ...]:
    context = tuple(_normalize_text(value) for value in check["context"])
    label = _normalize_text(check["row_label"])
    expected = tuple(check["expected"])
    candidates = []
    for table in soup.find_all("table"):
        table_text = _normalize_text(" ".join(table.stripped_strings))
        if not all(fragment in table_text for fragment in context):
            continue
        for row in table.find_all("tr"):
            cells = [
                _normalize_text(" ".join(cell.stripped_strings))
                for cell in row.find_all(("td", "th"))
            ]
            first = next((cell for cell in cells if cell), "")
            if first != label:
                continue
            values = _row_numbers(row)
            if len(values) >= len(expected):
                candidates.append(values[-len(expected):])
    if not candidates or set(candidates) != {expected}:
        raise RuntimeError(
            f"FORR source row changed for {check['check_id']}: {candidates}"
        )
    return candidates[0]


def verify_source_values(raw_by_source: dict[str, bytes]) -> list[dict]:
    if set(raw_by_source) != set(SOURCE_DOCUMENTS):
        raise ValueError("FORR raw source set does not match source lock")
    verified = []
    parsed = {}
    for source_id, payload in raw_by_source.items():
        soup = BeautifulSoup(payload, "lxml")
        text = _normalize_text(" ".join(soup.stripped_strings))
        missing = [
            phrase for phrase in SOURCE_TEXT_CHECKS[source_id]
            if _normalize_text(phrase) not in text
        ]
        if missing:
            raise RuntimeError(f"FORR source text changed for {source_id}: {missing}")
        parsed[source_id] = {}
        for check in SOURCE_ROW_CHECKS[source_id]:
            values = _parse_check(soup, check)
            parsed[source_id][check["check_id"]] = values
            verified.append({
                "source_id": source_id,
                "check_id": check["check_id"],
                "row_label": check["row_label"],
                "values_usd_thousands": values,
            })

    q3 = parsed["2017_q3_10q"]
    annual = parsed["2017_10k"]
    transition = parsed["2018_q3_10q"]
    if q3["m9_2017_revenue"][2:] != transition["as_reported_revenue"][3:] + (242_649,):
        raise RuntimeError("FORR 2017 revenue comparative identity changed")
    if q3["m9_2017_net_income"][2:] != transition["as_reported_net_income"][3:] + (11_861,):
        raise RuntimeError("FORR 2017 net-income comparative identity changed")
    if sum(annual["2017_quarterly_revenue"]) != annual["annual_revenue"][0]:
        raise RuntimeError("FORR 2017 quarterly revenue no longer closes to annual")
    if sum(annual["2017_quarterly_net_income"]) != annual["annual_net_income"][0]:
        raise RuntimeError("FORR 2017 quarterly net income no longer closes to annual")
    if sum(annual["2016_quarterly_revenue"]) != annual["annual_revenue"][1]:
        raise RuntimeError("FORR 2016 quarterly revenue no longer closes to annual")
    if sum(annual["2016_quarterly_net_income"]) != annual["annual_net_income"][1]:
        raise RuntimeError("FORR 2016 quarterly net income no longer closes to annual")
    return verified


def validate_source_lock(sources: dict[str, dict] | None = None) -> None:
    documents = SOURCE_DOCUMENTS if sources is None else sources
    if set(documents) != set(SOURCE_DOCUMENTS):
        raise ValueError("FORR source document set changed")
    for source_id, source in documents.items():
        if source["filed"] > SIGNAL_DATE:
            raise ValueError(f"FORR source {source_id} violates the PIT cutoff")
        if not re.fullmatch(r"[0-9a-f]{64}", source["expected_sha256"]):
            raise ValueError(f"FORR source {source_id} has an invalid SHA-256")
        url = _source_url(source)
        if f"/data/{CIK}/{source['accession'].replace('-', '')}/" not in url:
            raise ValueError(f"FORR source {source_id} does not lock CIK/accession")
        if not url.endswith("/" + source["document"]):
            raise ValueError(f"FORR source {source_id} does not lock document")
    if AVAILABLE_DATE != max(source["filed"] for source in documents.values()):
        raise ValueError("FORR availability date is not the latest operand filing")


def prepare_verified_sources(output_dir: Path) -> tuple[dict, list[dict]]:
    validate_source_lock()
    provenance = {}
    raw_by_source = {}
    for source_id, source in SOURCE_DOCUMENTS.items():
        path = output_dir / "sources" / source["document"]
        downloaded = False
        if path.exists():
            payload = path.read_bytes()
        else:
            payload = _download_bytes(_source_url(source))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            downloaded = True
        actual_sha = _sha256_bytes(payload)
        if actual_sha != source["expected_sha256"]:
            raise RuntimeError(
                f"FORR source SHA-256 changed for {source_id}: {actual_sha}"
            )
        raw_by_source[source_id] = payload
        provenance[source_id] = {
            **source,
            "url": _source_url(source),
            "local_path": str(path),
            "actual_sha256": actual_sha,
            "bytes": len(payload),
            "downloaded": downloaded,
        }
    return provenance, verify_source_values(raw_by_source)


def comparable_ttm_evidence() -> dict:
    validate_source_lock()
    derived = {}
    for metric, operands in OPERANDS_USD_THOUSANDS.items():
        prior = operands["q4_2016"] + operands["m9_2017"]
        current = operands["q4_2017"] + operands["m9_2018_previous_guidance"]
        if {"prior": prior, "current": current} != EXPECTED_TTM[metric]:
            raise RuntimeError(f"FORR {metric} comparable TTM changed")
        if prior <= 0:
            raise RuntimeError(f"FORR {metric} growth denominator is not positive")
        derived[metric] = {
            "prior_ttm_usd_thousands": prior,
            "current_ttm_usd_thousands": current,
            "growth": (current - prior) / abs(prior),
            "prior_formula": "2016Q4 + M9 2017 (ASC 605)",
            "current_formula": "2017Q4 + M9 2018 as if previous ASC 605 guidance",
        }
    return {
        "ticker": TICKER,
        "fiscal_end": FISCAL_END,
        "available_date": AVAILABLE_DATE,
        "signal_date": SIGNAL_DATE,
        "financial_age_days": (
            pd.Timestamp(SIGNAL_DATE) - pd.Timestamp(AVAILABLE_DATE)
        ).days,
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
        "operands_usd_thousands": OPERANDS_USD_THOUSANDS,
        "derived": derived,
        "operand_accessions": [
            SOURCE_DOCUMENTS["2017_10k"]["accession"],
            SOURCE_DOCUMENTS["2018_q3_10q"]["accession"],
        ],
        "cross_check_accession": SOURCE_DOCUMENTS["2017_q3_10q"]["accession"],
        "accounting_policy_comparability": {
            "status": "EXACT_ISSUER_DISCLOSED_CONSTANT_ASC605_BASIS",
            "transition": (
                "Forrester adopted ASC 606 on 2018-01-01 using modified "
                "retrospective treatment; 2017 remained ASC 605."
            ),
            "normalization": (
                "The 2018Q3 10-Q supplies exact nine-month 2018 amounts as if "
                "the previous guidance remained in effect. Those issuer-disclosed "
                "amounts are paired only with ASC 605 periods."
            ),
            "excluded": (
                "The as-reported 2018 revenue 258.992m and net income 10.005m "
                "are not used in the growth calculation; no cross-basis splice, "
                "non-GAAP adjustment, pro-forma estimate, or post-signal filing is used."
            ),
        },
    }


def strict_quarterly_facts() -> pd.DataFrame:
    evidence = comparable_ttm_evidence()
    accession = "+".join(evidence["operand_accessions"])
    rows = []
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
                "concept": f"forr_asc605_comparable_ttm:{metric}",
                "form": "10-K+10-Q_ASC605_BASIS_DERIVED",
                "accession": accession,
                "fetched_at": FETCHED_AT,
            })
    facts = pd.DataFrame(rows, columns=OUTPUT_COLUMNS).sort_values("metric")
    if len(facts) != 4 or set(facts["metric"]) != DIRECT_METRICS:
        raise RuntimeError("FORR direct growth package is incomplete")
    return facts.reset_index(drop=True)


def _audit_rows(path: Path, expected_sha256: str) -> pd.DataFrame:
    actual_sha = _sha256(path)
    if actual_sha != expected_sha256:
        raise RuntimeError(f"FORR audit binding changed: {actual_sha}")
    frame = pd.read_csv(path)
    scenarios = {scenario for scenario, _ in AUDIT_OBSERVATIONS}
    return frame.loc[
        frame["ticker"].eq(TICKER) & frame["scenario"].isin(scenarios)
    ].copy()


def validate_audit_binding(
    path: Path, expected_sha256: str, *, expect_recovered: bool
) -> dict:
    rows = _audit_rows(path, expected_sha256)
    if expect_recovered:
        if not rows.empty:
            raise RuntimeError("FORR remains in the current financial priorities")
        return {
            "path": str(path), "sha256": expected_sha256,
            "remaining_observation_count": 0, "status": "RECOVERED",
        }
    scenarios = {scenario for scenario, _ in AUDIT_OBSERVATIONS}
    if len(rows) != 3 or set(rows["scenario"]) != scenarios:
        raise RuntimeError("FORR baseline audit scenarios changed")
    if not rows["stale_growth_snapshot_signal_count"].eq(1).all():
        raise RuntimeError("FORR baseline stale-growth classification changed")
    for column in (
        "no_raw_pit_financial_facts_signal_count",
        "insufficient_growth_history_signal_count",
    ):
        if not rows[column].eq(0).all():
            raise RuntimeError("FORR baseline raw/history classification changed")
    if set(rows["first_missing_signal_date"]) != {SIGNAL_DATE}:
        raise RuntimeError("FORR baseline signal date changed")
    return {
        "path": str(path), "sha256": expected_sha256,
        "missing_observation_count": 3, "classification": "stale_growth_snapshot",
    }


def recovered_observations() -> pd.DataFrame:
    evidence = comparable_ttm_evidence()
    return pd.DataFrame([{
        "scenario": scenario,
        "ticker": TICKER,
        "signal_date": SIGNAL_DATE,
        "maximum_age_days": age,
        "resolved": True,
        "decision": "exact_issuer_disclosed_comparable_asc605_ttm_growth",
        "financial_age_days": evidence["financial_age_days"],
        "revenue_ttm_usd": evidence["derived"]["revenue"][
            "current_ttm_usd_thousands"
        ] * SOURCE_SCALE,
        "revenue_growth": evidence["derived"]["revenue"]["growth"],
        "net_income_ttm_usd": evidence["derived"]["net_income"][
            "current_ttm_usd_thousands"
        ] * SOURCE_SCALE,
        "net_income_growth": evidence["derived"]["net_income"]["growth"],
        "available_date": AVAILABLE_DATE,
    } for scenario, age in AUDIT_OBSERVATIONS])


def build(
    output_dir: Path = OUTPUT_DIR,
    *,
    baseline_audit_path: Path = BASELINE_AUDIT_PATH,
    expected_baseline_audit_sha256: str = EXPECTED_BASELINE_AUDIT_SHA256,
    current_audit_path: Path = CURRENT_AUDIT_PATH,
    expected_current_audit_sha256: str = EXPECTED_CURRENT_AUDIT_SHA256,
) -> dict:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    sources, source_verification = prepare_verified_sources(output_dir)
    evidence = comparable_ttm_evidence()
    facts = strict_quarterly_facts()
    observations = recovered_observations()
    baseline = validate_audit_binding(
        Path(baseline_audit_path), expected_baseline_audit_sha256,
        expect_recovered=False,
    )
    current_is_baseline = (
        Path(current_audit_path) == Path(baseline_audit_path)
        and expected_current_audit_sha256 == expected_baseline_audit_sha256
    )
    current = validate_audit_binding(
        Path(current_audit_path), expected_current_audit_sha256,
        expect_recovered=not current_is_baseline,
    )
    if current_is_baseline:
        current = {
            "path": str(current_audit_path),
            "sha256": expected_current_audit_sha256,
            "remaining_observation_count": 3,
            "status": "PENDING_CANDIDATE_INTEGRATION",
        }

    facts_path = output_dir / "strict_quarterly_facts.csv"
    evidence_path = output_dir / "comparable_ttm_evidence.json"
    observations_path = output_dir / "recovered_observations.csv"
    facts.to_csv(facts_path, index=False)
    evidence_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    observations.to_csv(observations_path, index=False)
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "shared_candidate_integrated": not current_is_baseline,
        "ticker": TICKER,
        "cik": CIK,
        "currency": CURRENCY,
        "accounting_standard": ACCOUNTING_STANDARD,
        "accepted_direct_growth_package_count": 1,
        "accepted_fact_count": len(facts),
        "resolved_audit_observation_count": len(observations),
        "source_documents": sources,
        "source_value_verification": source_verification,
        "accounting_policy_comparability": evidence[
            "accounting_policy_comparability"
        ],
        "audit_binding": {
            "baseline": baseline,
            "current": current,
            "recovered_observation_count": len(observations),
        },
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path), "sha256": _sha256(facts_path),
            },
            "comparable_ttm_evidence": {
                "path": str(evidence_path), "sha256": _sha256(evidence_path),
            },
            "recovered_observations": {
                "path": str(observations_path),
                "sha256": _sha256(observations_path),
                "row_count": len(observations),
            },
        },
        "guardrail": (
            "The direct bundle uses exact USD-thousand values public by "
            "2018-11-06. The issuer's 2018Q3 transition table supplies the M9 "
            "2018 amounts under previous ASC 605 guidance, and only ASC 605 "
            "2016/2017 operands are compared with them. As-reported ASC 606 "
            "amounts are verified but excluded. No formal financial file, "
            "estimate, non-GAAP value, pro-forma value, or later filing is used."
        ),
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    report["manifest"] = str(manifest_path)
    return report


def _direct_target_mask(frame: pd.DataFrame) -> pd.Series:
    fiscal_end = pd.to_datetime(frame["fiscal_end"], errors="raise").dt.strftime(
        "%Y-%m-%d"
    )
    return (
        frame["ticker"].eq(TICKER)
        & fiscal_end.eq(FISCAL_END)
        & frame["metric"].isin(DIRECT_METRICS)
    )


def integrate_candidate(
    *, base_dir: Path, supplement_dir: Path = OUTPUT_DIR, output_dir: Path
) -> dict:
    """Copy-on-write overlay of only FORR's four direct 2018Q3 metrics."""
    base_dir = Path(base_dir)
    supplement_dir = Path(supplement_dir)
    output_dir = Path(output_dir)
    inputs = (
        base_dir / "annual.csv",
        base_dir / "quarterly.csv",
        base_dir / "manifest.json",
        supplement_dir / "strict_quarterly_facts.csv",
        supplement_dir / "manifest.json",
    )
    bound = {path: _sha256(path) for path in inputs}
    base = pd.read_csv(inputs[1])
    incoming = pd.read_csv(inputs[3])
    if list(base.columns) != OUTPUT_COLUMNS or list(incoming.columns) != OUTPUT_COLUMNS:
        raise RuntimeError("FORR candidate integration requires the quarterly schema")
    if len(incoming) != 4 or not _direct_target_mask(incoming).all():
        raise RuntimeError("FORR supplement scope is not the four direct metrics")
    target = _direct_target_mask(base)
    replaced = base.loc[target].copy()
    untouched = base.loc[~target].copy()
    merged = pd.concat([untouched, incoming], ignore_index=True).sort_values(
        ["ticker", "fiscal_end", "metric", "available_date"]
    ).reset_index(drop=True)
    if len(merged) != len(untouched) + 4:
        raise RuntimeError("FORR overlay changed rows outside the bounded key space")

    output_dir.mkdir(parents=True, exist_ok=True)
    annual_path = output_dir / "annual.csv"
    quarterly_path = output_dir / "quarterly.csv"
    shutil.copyfile(inputs[0], annual_path)
    merged.to_csv(quarterly_path, index=False)
    if {path: _sha256(path) for path in inputs} != bound:
        raise RuntimeError("FORR integration source changed while being read")
    report = {
        "schema_version": 1,
        "research_only": True,
        "formal_financials_modified": False,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "overlay_ticker": TICKER,
        "overlay_fiscal_end": FISCAL_END,
        "overlay_metrics": sorted(DIRECT_METRICS),
        "removed_conflicting_rows": len(replaced),
        "inserted_strict_rows": len(incoming),
        "base": {
            "path": str(base_dir),
            "sha256": {str(path): digest for path, digest in bound.items()},
        },
        "outputs": {
            "annual": str(annual_path),
            "annual_sha256": _sha256(annual_path),
            "quarterly": str(quarterly_path),
            "quarterly_sha256": _sha256(quarterly_path),
            "quarterly_rows": len(merged),
        },
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
    report = build(args.output_dir)
    if args.base_dir is not None:
        report["candidate"] = integrate_candidate(
            base_dir=args.base_dir,
            supplement_dir=args.output_dir,
            output_dir=args.candidate_output_dir,
        )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
