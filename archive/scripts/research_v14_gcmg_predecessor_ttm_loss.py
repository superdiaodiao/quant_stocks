#!/usr/bin/env python3
"""Recover GCMG's pre-signal predecessor-attributable TTM loss.

GCMG began trading after its 2020-11-17 recapitalization.  The 2020-12-04
S-1 states that the combined financial statements continue the historical
financial statements of the operating GCM Companies at historical cost and
separately presents selected historical actuals.  Those actuals prove a
negative attributable TTM before the 2020-12-31 signal, so no growth rate or
pro-forma value is needed for the frozen positive-profit gate.
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


TICKER = "GCMG"
CIK = 1_819_796
CURRENCY = "USD"
SOURCE_SCALE = 1_000
FISCAL_END = "2020-09-30"
AVAILABLE_DATE = "2020-12-04"
SIGNAL_DATE = "2020-12-31"
FETCHED_AT = "2026-08-29"
OUTPUT_DIR = Path("output/research_only/v14/gcmg_predecessor_ttm_loss")
SEC_HEADERS = {"User-Agent": "quant-stocks-research contact@example.com"}

BASE_CANDIDATE_DIR = Path(
    "output/research_only/v14/"
    "candidate_fundamentals_v14_checkpoint_20260829_forr_asc605_ttm"
)
BASELINE_AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260829_forr_asc605_ttm_recovered_financial_priorities.csv"
)
EXPECTED_BASELINE_AUDIT_SHA256 = (
    "61187e04add06ca401a181636b575d77148c4377fbdfe362afb98c992dfc8c1f"
)
CURRENT_AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260829_gcmg_predecessor_ttm_recovered_financial_priorities.csv"
)
EXPECTED_CURRENT_AUDIT_SHA256 = (
    "75d91fb6e43e5b9cc7cc2128711ceb7dc694245f117ffc939c7e3c3e0d21afc3"
)

SOURCE_DOCUMENTS = {
    "earnings_8k_exhibit": {
        "role": "timely_m9_2020_actual_cross_check",
        "form": "8-K_EX-99.1",
        "filed": "2020-11-19",
        "accession": "0001213900-20-038131",
        "document": "ea130179ex99-1_gcmgrosvenor.htm",
        "expected_sha256": (
            "a8a5eb511f9eb7dd1e16796ebcb383ec5e84a3382dbb76ccca62652add1c451a"
        ),
    },
    "historical_actuals_s1": {
        "role": "audited_annual_and_unaudited_m9_historical_actuals",
        "form": "S-1",
        "filed": AVAILABLE_DATE,
        "accession": "0001213900-20-040855",
        "document": "fs12020_gcmgrosvenor.htm",
        "expected_sha256": (
            "fff876b67947ead324660cfaade6e7a4b6d06f5eb7af338a13a33d3a8fdb5445"
        ),
    },
}

SOURCE_TEXT_CHECKS = {
    "earnings_8k_exhibit": (
        "GCM Grosvenor Reports Strong Third Quarter",
        "reported its results for the third fiscal quarter ended September 30, 2020",
        "GAAP Financial Measures",
        "Three Months Ended",
        "Nine Months Ended",
    ),
    "historical_actuals_s1": (
        "SELECTED HISTORICAL FINANCIAL INFORMATION",
        "selected financial information on a historical basis for GCM Grosvenor",
        "derived from the audited financial statements of GCM Grosvenor",
        "derived from the unaudited condensed financial statements of GCM Grosvenor",
        "business combination will be accounted for as a recapitalization",
        "financial statements of the combined entity will represent a continuation",
        "net assets of GCM PubCo will be stated at historical cost",
        "no goodwill or other intangible assets recorded",
        "presented for illustrative purposes only and do not purport to represent",
    ),
}

SOURCE_ROW_CHECKS = {
    "earnings_8k_exhibit": (
        {
            "check_id": "m9_2020_revenue_cross_check",
            "context": (
                "Three Months Ended", "Nine Months Ended",
                "June 30, 2020", "Sept 30, 2020",
                "Employee Compensation and Benefits",
            ),
            "row_label": "Total Operating Revenues",
            "expected": (90_130, 101_746, 274_493),
        },
        {
            "check_id": "m9_2020_net_income_cross_check",
            "context": (
                "Three Months Ended", "Nine Months Ended",
                "June 30, 2020", "Sept 30, 2020",
                "Employee Compensation and Benefits",
            ),
            "row_label": "Net Income (Loss)",
            "expected": (929, 11_168, 1_771),
        },
        {
            "check_id": "m9_2020_attributable_cross_check",
            "context": (
                "Three Months Ended", "Nine Months Ended",
                "Adjusted pre-tax income & Adjusted net income",
                "Net fees attributable to GCM Grosvenor",
            ),
            "row_label": "Net income (loss) attributable to GCM Grosvenor",
            "expected": (5_927, 1_326, -7_702),
        },
    ),
    "historical_actuals_s1": (
        {
            "check_id": "selected_actual_revenue",
            "context": (
                "Nine Months Ended September 30",
                "Year Ended December 31",
                "Statement of Income Data (in thousands)",
                "Management fees", "Incentive fees",
            ),
            "row_label": "Total operating revenues",
            "expected": (274_493, 315_098, 416_394, 378_496, 377_006),
        },
        {
            "check_id": "selected_actual_net_income",
            "context": (
                "Nine Months Ended September 30",
                "Year Ended December 31",
                "Statement of Income Data (in thousands)",
                "Management fees", "Incentive fees",
            ),
            "row_label": "Net income",
            "expected": (1_771, 53_722, 59_998, 63_685, 73_020),
        },
        {
            "check_id": "selected_actual_attributable",
            "context": (
                "Nine Months Ended September 30",
                "Year Ended December 31",
                "Statement of Income Data (in thousands)",
                "Management fees", "Incentive fees",
            ),
            "row_label": "Net income (loss) attributable to GCM Grosvenor",
            "expected": (-7_702, 41_430, 46_777, 39_199, 53_039),
        },
    ),
}

OPERANDS_USD_THOUSANDS = {
    "fy2019_attributable": 46_777,
    "m9_2019_attributable": 41_430,
    "m9_2020_attributable": -7_702,
    "fy2019_revenue": 416_394,
    "m9_2019_revenue": 315_098,
    "m9_2020_revenue": 274_493,
    "fy2019_consolidated_net_income": 59_998,
    "m9_2019_consolidated_net_income": 53_722,
    "m9_2020_consolidated_net_income": 1_771,
}
EXPECTED_ATTRIBUTABLE_TTM = -2_355
EXPECTED_REVENUE_TTM = 375_789
EXPECTED_CONSOLIDATED_NET_INCOME_TTM = 8_047
TARGET_METRICS = frozenset({"net_income_ttm"})
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
    raise RuntimeError(f"failed to download locked GCMG source: {url}") from last_error


def _normalize_text(value: str) -> str:
    return " ".join(
        value.replace("\xa0", " ")
        .replace("\u200b", " ")
        .replace("’", "'")
        .replace("–", "-")
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
            f"GCMG source row changed for {check['check_id']}: {candidates}"
        )
    return candidates[0]


def verify_source_values(raw_by_source: dict[str, bytes]) -> list[dict]:
    if set(raw_by_source) != set(SOURCE_DOCUMENTS):
        raise ValueError("GCMG raw source set does not match source lock")
    parsed = {}
    verified = []
    for source_id, payload in raw_by_source.items():
        soup = BeautifulSoup(payload, "lxml")
        text = _normalize_text(" ".join(soup.stripped_strings))
        missing = [
            phrase for phrase in SOURCE_TEXT_CHECKS[source_id]
            if _normalize_text(phrase) not in text
        ]
        if missing:
            raise RuntimeError(f"GCMG source text changed for {source_id}: {missing}")
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

    earnings = parsed["earnings_8k_exhibit"]
    actuals = parsed["historical_actuals_s1"]
    if earnings["m9_2020_revenue_cross_check"][-1] != actuals[
        "selected_actual_revenue"
    ][0]:
        raise RuntimeError("GCMG M9 2020 revenue cross-check changed")
    if earnings["m9_2020_net_income_cross_check"][-1] != actuals[
        "selected_actual_net_income"
    ][0]:
        raise RuntimeError("GCMG M9 2020 consolidated income cross-check changed")
    if earnings["m9_2020_attributable_cross_check"][-1] != actuals[
        "selected_actual_attributable"
    ][0]:
        raise RuntimeError("GCMG M9 2020 attributable income cross-check changed")
    return verified


def validate_source_lock(sources: dict[str, dict] | None = None) -> None:
    documents = SOURCE_DOCUMENTS if sources is None else sources
    if set(documents) != set(SOURCE_DOCUMENTS):
        raise ValueError("GCMG source document set changed")
    for source_id, source in documents.items():
        if source["filed"] > SIGNAL_DATE:
            raise ValueError(f"GCMG source {source_id} violates the PIT cutoff")
        if not re.fullmatch(r"[0-9a-f]{64}", source["expected_sha256"]):
            raise ValueError(f"GCMG source {source_id} has an invalid SHA-256")
        url = _source_url(source)
        if f"/data/{CIK}/{source['accession'].replace('-', '')}/" not in url:
            raise ValueError(f"GCMG source {source_id} does not lock CIK/accession")
        if not url.endswith("/" + source["document"]):
            raise ValueError(f"GCMG source {source_id} does not lock document")
    if AVAILABLE_DATE != max(source["filed"] for source in documents.values()):
        raise ValueError("GCMG availability date is not the latest operand filing")


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
                f"GCMG source SHA-256 changed for {source_id}: {actual_sha}"
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


def predecessor_ttm_evidence() -> dict:
    validate_source_lock()
    attributable = (
        OPERANDS_USD_THOUSANDS["fy2019_attributable"]
        - OPERANDS_USD_THOUSANDS["m9_2019_attributable"]
        + OPERANDS_USD_THOUSANDS["m9_2020_attributable"]
    )
    revenue = (
        OPERANDS_USD_THOUSANDS["fy2019_revenue"]
        - OPERANDS_USD_THOUSANDS["m9_2019_revenue"]
        + OPERANDS_USD_THOUSANDS["m9_2020_revenue"]
    )
    consolidated = (
        OPERANDS_USD_THOUSANDS["fy2019_consolidated_net_income"]
        - OPERANDS_USD_THOUSANDS["m9_2019_consolidated_net_income"]
        + OPERANDS_USD_THOUSANDS["m9_2020_consolidated_net_income"]
    )
    if attributable != EXPECTED_ATTRIBUTABLE_TTM:
        raise RuntimeError("GCMG predecessor-attributable TTM changed")
    if revenue != EXPECTED_REVENUE_TTM:
        raise RuntimeError("GCMG predecessor revenue TTM changed")
    if consolidated != EXPECTED_CONSOLIDATED_NET_INCOME_TTM:
        raise RuntimeError("GCMG consolidated net-income TTM changed")
    if attributable >= 0:
        raise RuntimeError("GCMG attributable TTM must remain negative")
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
        "operands_usd_thousands": OPERANDS_USD_THOUSANDS,
        "derived": {
            "net_income_ttm_attributable_usd_thousands": attributable,
            "revenue_ttm_usd_thousands": revenue,
            "consolidated_net_income_ttm_usd_thousands": consolidated,
            "formula": "FY2019 actual - M9 2019 actual + M9 2020 actual",
        },
        "metric_mapping": {
            "selected_metric": (
                "Net income (loss) attributable to GCM Grosvenor, after both "
                "redeemable and other noncontrolling interests"
            ),
            "reason": (
                "This is the actual predecessor-parent earnings line continued "
                "by the recapitalized public company; consolidated net income "
                "includes earnings allocated away to noncontrolling interests."
            ),
        },
        "transaction_accounting": {
            "method": "GAAP_RECAPITALIZATION",
            "continuity": (
                "The S-1 states the combined financial statements continue the "
                "GCM Companies' financial statements at historical cost, with "
                "no goodwill or other intangible assets recorded."
            ),
            "pro_forma_excluded": True,
            "boundary": (
                "Only the Selected Historical Financial Information actuals are "
                "used. The separate unaudited pro-forma tables, adjusted FRE, "
                "adjusted net income, transaction estimates, and later 10-K are excluded."
            ),
        },
    }


def strict_quarterly_facts() -> pd.DataFrame:
    evidence = predecessor_ttm_evidence()
    row = {
        "ticker": TICKER,
        "fiscal_end": FISCAL_END,
        "available_date": AVAILABLE_DATE,
        "metric": "net_income_ttm",
        "value": (
            evidence["derived"]["net_income_ttm_attributable_usd_thousands"]
            * SOURCE_SCALE
        ),
        "taxonomy": "us-gaap",
        "concept": "gcmg_predecessor_attributable_ttm:actual_historical",
        "form": "S-1_HISTORICAL_ACTUAL_DERIVED",
        "accession": SOURCE_DOCUMENTS["historical_actuals_s1"]["accession"],
        "fetched_at": FETCHED_AT,
    }
    facts = pd.DataFrame([row], columns=OUTPUT_COLUMNS)
    if len(facts) != 1 or set(facts["metric"]) != TARGET_METRICS:
        raise RuntimeError("GCMG recovery must contain one direct TTM loss")
    return facts


def _audit_rows(path: Path, expected_sha256: str) -> pd.DataFrame:
    actual_sha = _sha256(path)
    if actual_sha != expected_sha256:
        raise RuntimeError(f"GCMG audit binding changed: {actual_sha}")
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
            raise RuntimeError("GCMG remains in the current financial priorities")
        return {
            "path": str(path), "sha256": expected_sha256,
            "remaining_observation_count": 0, "status": "RECOVERED",
        }
    scenarios = {scenario for scenario, _ in AUDIT_OBSERVATIONS}
    if len(rows) != 3 or set(rows["scenario"]) != scenarios:
        raise RuntimeError("GCMG baseline audit scenarios changed")
    if not rows["no_raw_pit_financial_facts_signal_count"].eq(1).all():
        raise RuntimeError("GCMG baseline no-raw-facts classification changed")
    for column in (
        "insufficient_growth_history_signal_count",
        "stale_growth_snapshot_signal_count",
    ):
        if not rows[column].eq(0).all():
            raise RuntimeError("GCMG baseline history/stale classification changed")
    if set(rows["first_missing_signal_date"]) != {SIGNAL_DATE}:
        raise RuntimeError("GCMG baseline signal date changed")
    return {
        "path": str(path), "sha256": expected_sha256,
        "missing_observation_count": 3,
        "classification": "no_raw_pit_financial_facts",
    }


def recovered_observations() -> pd.DataFrame:
    evidence = predecessor_ttm_evidence()
    return pd.DataFrame([{
        "scenario": scenario,
        "ticker": TICKER,
        "signal_date": SIGNAL_DATE,
        "maximum_age_days": age,
        "resolved": True,
        "decision": "recovered_known_nonpositive_predecessor_attributable_ttm",
        "net_income_ttm_usd": evidence["derived"][
            "net_income_ttm_attributable_usd_thousands"
        ] * SOURCE_SCALE,
        "financial_age_days": evidence["financial_age_days"],
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
    evidence = predecessor_ttm_evidence()
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
    evidence_path = output_dir / "predecessor_ttm_evidence.json"
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
        "accepted_direct_ttm_loss_count": 1,
        "accepted_fact_count": len(facts),
        "resolved_audit_observation_count": len(observations),
        "source_documents": sources,
        "source_value_verification": source_verification,
        "transaction_accounting": evidence["transaction_accounting"],
        "metric_mapping": evidence["metric_mapping"],
        "audit_binding": {
            "baseline": baseline,
            "current": current,
            "recovered_observation_count": len(observations),
        },
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path), "sha256": _sha256(facts_path),
            },
            "predecessor_ttm_evidence": {
                "path": str(evidence_path), "sha256": _sha256(evidence_path),
            },
            "recovered_observations": {
                "path": str(observations_path),
                "sha256": _sha256(observations_path),
                "row_count": len(observations),
            },
        },
        "guardrail": (
            "The loss is exact predecessor-parent attributable income derived "
            "from audited FY2019 and unaudited M9 actuals filed by 2020-12-04. "
            "The S-1 establishes recapitalization continuity at historical cost. "
            "Separate pro-forma tables, adjusted earnings, consolidated income "
            "allocated to noncontrolling interests, later 10-K values, and any "
            "growth rate are excluded. Formal financial files remain unchanged."
        ),
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    report["manifest"] = str(manifest_path)
    return report


def _target_mask(frame: pd.DataFrame) -> pd.Series:
    fiscal_end = pd.to_datetime(frame["fiscal_end"], errors="raise").dt.strftime(
        "%Y-%m-%d"
    )
    return (
        frame["ticker"].eq(TICKER)
        & fiscal_end.eq(FISCAL_END)
        & frame["metric"].isin(TARGET_METRICS)
    )


def integrate_candidate(
    *, base_dir: Path, supplement_dir: Path = OUTPUT_DIR, output_dir: Path
) -> dict:
    """Copy-on-write overlay of only GCMG's 2020Q3 direct TTM loss."""
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
        raise RuntimeError("GCMG candidate integration requires the quarterly schema")
    if len(incoming) != 1 or not _target_mask(incoming).all():
        raise RuntimeError("GCMG supplement scope is not the direct TTM loss")
    target = _target_mask(base)
    replaced = base.loc[target].copy()
    untouched = base.loc[~target].copy()
    merged = pd.concat([untouched, incoming], ignore_index=True).sort_values(
        ["ticker", "fiscal_end", "metric", "available_date"]
    ).reset_index(drop=True)
    if len(merged) != len(untouched) + 1:
        raise RuntimeError("GCMG overlay changed rows outside the bounded key space")

    output_dir.mkdir(parents=True, exist_ok=True)
    annual_path = output_dir / "annual.csv"
    quarterly_path = output_dir / "quarterly.csv"
    shutil.copyfile(inputs[0], annual_path)
    merged.to_csv(quarterly_path, index=False)
    if {path: _sha256(path) for path in inputs} != bound:
        raise RuntimeError("GCMG integration source changed while being read")
    report = {
        "schema_version": 1,
        "research_only": True,
        "formal_financials_modified": False,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "overlay_ticker": TICKER,
        "overlay_fiscal_end": FISCAL_END,
        "overlay_metrics": sorted(TARGET_METRICS),
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
