#!/usr/bin/env python3
"""Recover QFIN's exact pre-signal annual-plus-nine-month TTM growth."""

from __future__ import annotations

import argparse
from decimal import Decimal
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import shutil
import time
from urllib.request import Request, urlopen
import warnings

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
import pandas as pd

from scripts.research_v14_qfin_annual_growth import (
    SOURCE_PATH as ANNUAL_CACHE_PATH,
    SOURCE_SHA256 as ANNUAL_CACHE_SHA256,
    _exact_fact,
    _load_payload,
)
from src.io.fundamentals_update import OUTPUT_COLUMNS


TICKER = "QFIN"
CIK = 1_741_530
CURRENCY = "CNY"
SOURCE_SCALE = 1_000
FISCAL_END = "2020-09-30"
AVAILABLE_DATE = "2020-11-20"
SIGNAL_DATE = "2021-01-29"
FETCHED_AT = "2026-08-29"
OUTPUT_DIR = Path("output/research_only/v14/qfin_m9_ttm_growth")
SEC_HEADERS = {"User-Agent": "quant-stocks-research contact@example.com"}

BASE_CANDIDATE_DIR = Path(
    "output/research_only/v14/"
    "candidate_fundamentals_v14_checkpoint_20260829_pll_exact_ttm_loss"
)
BASELINE_AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260829_pll_exact_ttm_loss_recovered_financial_priorities.csv"
)
EXPECTED_BASELINE_AUDIT_SHA256 = (
    "548070b273b41f91e1a3d94d25e6adf331c079c8d2e589c5d9c74def0d800133"
)
CURRENT_AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260829_qfin_m9_ttm_recovered_financial_priorities.csv"
)
EXPECTED_CURRENT_AUDIT_SHA256 = (
    "c05a233fa07ff5209e8200ab2e199d9b9b3a1db624c9cb3df37efc0e01e3ec9f"
)

SOURCE_DOCUMENTS = {
    "q3_2019_6k": {
        "role": "m9_2018_and_m9_2019_operands",
        "form": "6-K Exhibit 99.1",
        "filed": "2019-11-27",
        "accepted_at": "2019-11-27T21:15:41Z",
        "accession": "0001104659-19-068196",
        "document": "a19-24025_1ex99d1.htm",
        "expected_sha256": (
            "e96b142c051271366cf2cac5433f77ea95d40f9af2eba46128bef82e9c8c300e"
        ),
    },
    "q3_2020_6k": {
        "role": "m9_2019_comparative_and_m9_2020_operands",
        "form": "6-K Exhibit 99.1",
        "filed": AVAILABLE_DATE,
        "accepted_at": "2020-11-20T11:06:12Z",
        "accession": "0001104659-20-127568",
        "document": "a20-36528_1ex99d1.htm",
        "expected_sha256": (
            "14ea354cba17ff320c513a28b5bd5ded5886f52530491d02f527d268c83520dc"
        ),
    },
}

SOURCE_TEXT_CHECKS = {
    "q3_2019_6k": (
        "360 Finance Announces Third Quarter 2019 Unaudited Financial Results",
        "Total net revenue",
        "Net income",
    ),
    "q3_2020_6k": (
        "360 DigiTech Announces Third Quarter 2020 Unaudited Financial Results",
        "Total net revenue",
        "Net income",
    ),
}

SOURCE_ROW_CHECKS = {
    "q3_2019_6k": (
        {"check_id": "q3_m9_revenue", "row_label": "Total net revenue",
         "expected": (1_302_689, 2_583_039, 361_381, 2_880_554, 6_818_966, 954_009)},
        {"check_id": "q3_m9_net_income", "row_label": "Net income",
         "expected": (577_444, 733_488, 102_620, 598_580, 2_071_606, 289_829)},
    ),
    "q3_2020_6k": (
        {"check_id": "q3_m9_revenue", "row_label": "Total net revenue",
         "expected": (2_583_039, 3_703_521, 545_469, 6_818_966, 10_226_468, 1_506_194)},
        {"check_id": "q3_m9_net_income", "row_label": "Net income",
         "expected": (733_488, 1_231_724, 181_412, 2_071_606, 2_291_344, 337_477)},
    ),
}

M9_VALUES_CNY_THOUSANDS = {
    "m9_2018": {"revenue": 2_880_554, "net_income": 598_580},
    "m9_2019": {"revenue": 6_818_966, "net_income": 2_071_606},
    "m9_2020": {"revenue": 10_226_468, "net_income": 2_291_344},
}
EXPECTED_ANNUAL_CNY_THOUSANDS = {
    "fy2018": {"revenue": 4_447_018, "net_income": 1_193_311},
    "fy2019": {"revenue": 9_219_847, "net_income": 2_501_304},
}
EXPECTED_TTM_CNY_THOUSANDS = {
    "prior": {"revenue": 8_385_430, "net_income": 2_666_337},
    "current": {"revenue": 12_627_349, "net_income": 2_721_042},
}
EXPECTED_GROWTH = {
    "revenue": 0.5058677968810186,
    "net_income": 0.020516911403172218,
}
TARGET_METRICS = frozenset(
    {"revenue_ttm", "net_income_ttm", "revenue_growth", "net_income_growth"}
)
AUDIT_OBSERVATIONS = (
    ("liq2000000-age150-growth", 150),
    ("liq10000000-age150-growth", 150),
)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _source_url(source: dict) -> str:
    accession = source["accession"].replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{CIK}/"
        f"{accession}/{source['document']}"
    )


def _download_bytes(url: str) -> bytes:
    error: Exception | None = None
    for attempt in range(5):
        try:
            with urlopen(Request(url, headers=SEC_HEADERS), timeout=120) as response:
                return response.read()
        except OSError as exc:  # pragma: no cover - network retry
            error = exc
            if attempt < 4:
                time.sleep(attempt + 1)
    raise RuntimeError(f"failed to download locked QFIN source: {url}") from error


def _normalize(value: object) -> str:
    return " ".join(
        str(value).replace("\xa0", " ").replace("\u200b", " ").split()
    )


def _html_text(payload: bytes) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(payload, "lxml")
    return _normalize(soup.get_text(" ", strip=True))


def _numeric_cells(values) -> list[int]:
    numbers = []
    for value in values:
        text = _normalize(value)
        if not text or text.casefold() == "nan" or text in {"$", ")", "-", "—"}:
            continue
        negative = text.startswith("(") or text.endswith(")")
        cleaned = re.sub(r"[^0-9.-]", "", text)
        if not re.fullmatch(r"-?\d+(?:\.\d+)?", cleaned):
            continue
        number = float(cleaned)
        if not number.is_integer():
            continue
        integer = int(number)
        numbers.append(-abs(integer) if negative else integer)
    return numbers


def _contains_subsequence(values: list[int], expected: tuple[int, ...]) -> bool:
    return any(
        tuple(values[index:index + len(expected)]) == expected
        for index in range(len(values) - len(expected) + 1)
    )


def verify_source_values(raw_by_source: dict[str, bytes]) -> list[dict]:
    if set(raw_by_source) != set(SOURCE_DOCUMENTS):
        raise ValueError("QFIN raw source set does not match the source lock")
    verified = []
    for source_id, checks in SOURCE_ROW_CHECKS.items():
        tables = pd.read_html(BytesIO(raw_by_source[source_id]))
        for check in checks:
            expected = tuple(check["expected"])
            matches = []
            for table in tables:
                for _, row in table.iterrows():
                    if _normalize(row.iloc[0]).casefold() != _normalize(
                        check["row_label"]
                    ).casefold():
                        continue
                    values = _numeric_cells(row.iloc[1:])
                    if _contains_subsequence(values, expected):
                        matches.append(tuple(values))
            if not matches:
                raise RuntimeError(
                    f"QFIN source row changed for {source_id}/{check['check_id']}"
                )
            verified.append({
                "source_id": source_id,
                "check_id": check["check_id"],
                "row_label": check["row_label"],
                "expected_values_cny_thousands": list(expected),
                "match_count": len(matches),
            })
    return verified


def validate_source_lock(sources: dict[str, dict] | None = None) -> None:
    documents = SOURCE_DOCUMENTS if sources is None else sources
    if set(documents) != set(SOURCE_DOCUMENTS):
        raise ValueError("QFIN source document set changed")
    for source_id, source in documents.items():
        locked = SOURCE_DOCUMENTS[source_id]
        for field in ("form", "accepted_at", "accession", "document"):
            if source[field] != locked[field]:
                raise ValueError(
                    f"QFIN source {source_id} changed locked identity field {field}"
                )
        if source["filed"] > SIGNAL_DATE:
            raise ValueError(f"QFIN source {source_id} violates the PIT cutoff")
        if not re.fullmatch(r"[0-9a-f]{64}", source["expected_sha256"]):
            raise ValueError(f"QFIN source {source_id} has an invalid SHA-256")
    if AVAILABLE_DATE != max(source["filed"] for source in documents.values()):
        raise ValueError("QFIN availability date is not the latest M9 filing")


def prepare_verified_sources(output_dir: Path) -> tuple[dict, list[dict]]:
    validate_source_lock()
    provenance = {}
    raw_by_source = {}
    for source_id, source in SOURCE_DOCUMENTS.items():
        path = output_dir / "sources" / f"{source_id}_{source['document']}"
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
                f"QFIN source SHA-256 changed for {source_id}: {actual_sha}"
            )
        text = _html_text(payload).casefold()
        missing = [
            fragment for fragment in SOURCE_TEXT_CHECKS[source_id]
            if _normalize(fragment).casefold() not in text
        ]
        if missing:
            raise RuntimeError(f"QFIN source text changed for {source_id}: {missing}")
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


def _annual_values_cny_thousands(payload: dict) -> dict:
    filing = {
        "accession": "0001104659-20-054414",
        "filed": "2020-04-30",
    }
    concepts = {
        "revenue": "RevenueFromContractWithCustomerExcludingAssessedTax",
        "net_income": "ProfitLoss",
    }
    result = {}
    for fiscal_year in (2018, 2019):
        result[f"fy{fiscal_year}"] = {}
        for metric, concept in concepts.items():
            value_cny = _exact_fact(
                payload,
                concept=concept,
                start=f"{fiscal_year}-01-01",
                end=f"{fiscal_year}-12-31",
                accession=filing["accession"],
                filed=filing["filed"],
            )
            if value_cny % SOURCE_SCALE != 0:
                raise RuntimeError(
                    f"QFIN annual {metric} is not exact in CNY thousands: "
                    f"{value_cny}"
                )
            result[f"fy{fiscal_year}"][metric] = int(value_cny / SOURCE_SCALE)
    if result != EXPECTED_ANNUAL_CNY_THOUSANDS:
        raise RuntimeError(f"QFIN annual operands changed: {result}")
    return result


def _growth(current: int, prior: int) -> float:
    if prior == 0:
        raise ValueError("QFIN exact TTM growth denominator cannot be zero")
    return float((Decimal(current) - Decimal(prior)) / abs(Decimal(prior)))


def ttm_evidence(payload: dict | None = None) -> dict:
    if payload is None:
        _, payload = _load_payload(ANNUAL_CACHE_PATH)
    annual = _annual_values_cny_thousands(payload)
    prior = {
        metric: (
            annual["fy2018"][metric]
            - M9_VALUES_CNY_THOUSANDS["m9_2018"][metric]
            + M9_VALUES_CNY_THOUSANDS["m9_2019"][metric]
        )
        for metric in ("revenue", "net_income")
    }
    current = {
        metric: (
            annual["fy2019"][metric]
            - M9_VALUES_CNY_THOUSANDS["m9_2019"][metric]
            + M9_VALUES_CNY_THOUSANDS["m9_2020"][metric]
        )
        for metric in ("revenue", "net_income")
    }
    if {"prior": prior, "current": current} != EXPECTED_TTM_CNY_THOUSANDS:
        raise RuntimeError("QFIN exact TTM operands changed")
    growth = {
        metric: _growth(current[metric], prior[metric])
        for metric in ("revenue", "net_income")
    }
    for metric, expected in EXPECTED_GROWTH.items():
        if abs(growth[metric] - expected) > 1e-15:
            raise RuntimeError(f"QFIN {metric} growth changed: {growth[metric]}")
    return {
        "ticker": TICKER,
        "currency": CURRENCY,
        "source_scale": SOURCE_SCALE,
        "fiscal_end": FISCAL_END,
        "available_date": AVAILABLE_DATE,
        "signal_date": SIGNAL_DATE,
        "financial_age_days": int(
            (pd.Timestamp(SIGNAL_DATE) - pd.Timestamp(AVAILABLE_DATE)).days
        ),
        "annual_operands_cny_thousands": annual,
        "m9_operands_cny_thousands": M9_VALUES_CNY_THOUSANDS,
        "derived": {
            "prior_ttm_cny_thousands": prior,
            "current_ttm_cny_thousands": current,
            "growth": growth,
        },
        "formulas": {
            "prior_ttm": "FY2018 - M9_2018 + M9_2019",
            "current_ttm": "FY2019 - M9_2019 + M9_2020",
            "growth": "(current_ttm - prior_ttm) / abs(prior_ttm)",
        },
        "guardrails": {
            "same_cny_basis": True,
            "us_gaap_only": True,
            "gaap_net_income_only": True,
            "non_gaap_metrics_excluded": True,
            "usd_convenience_translations_excluded": True,
            "post_signal_fy2020_20f_excluded": True,
        },
    }


def strict_quarterly_facts(payload: dict | None = None) -> pd.DataFrame:
    evidence = ttm_evidence(payload)["derived"]
    values = {
        "revenue_ttm": evidence["current_ttm_cny_thousands"]["revenue"] * SOURCE_SCALE,
        "net_income_ttm": evidence["current_ttm_cny_thousands"]["net_income"] * SOURCE_SCALE,
        "revenue_growth": evidence["growth"]["revenue"],
        "net_income_growth": evidence["growth"]["net_income"],
    }
    accessions = "0001104659-20-054414+" + "+".join(
        source["accession"] for source in SOURCE_DOCUMENTS.values()
    )
    facts = pd.DataFrame([{
        "ticker": TICKER,
        "fiscal_end": FISCAL_END,
        "available_date": AVAILABLE_DATE,
        "metric": metric,
        "value": value,
        "taxonomy": "us-gaap",
        "concept": f"qfin_annual_m9_ttm:{metric}:CNY",
        "form": "20-F_PLUS_6-K_Q3_M9_TTM",
        "accession": accessions,
        "fetched_at": FETCHED_AT,
    } for metric, value in values.items()], columns=OUTPUT_COLUMNS)
    if len(facts) != 4 or set(facts["metric"]) != TARGET_METRICS:
        raise RuntimeError("QFIN recovery must contain the four-field TTM bundle")
    return facts


def _audit_rows(path: Path, expected_sha256: str) -> pd.DataFrame:
    actual_sha = _sha256(path)
    if actual_sha != expected_sha256:
        raise RuntimeError(f"QFIN audit binding changed: {actual_sha}")
    frame = pd.read_csv(path)
    scenarios = {scenario for scenario, _age in AUDIT_OBSERVATIONS}
    return frame.loc[
        frame["ticker"].eq(TICKER) & frame["scenario"].isin(scenarios)
    ].copy()


def validate_audit_binding(
    path: Path, expected_sha256: str, *, expect_recovered: bool
) -> dict:
    rows = _audit_rows(path, expected_sha256)
    if expect_recovered:
        if not rows.empty:
            raise RuntimeError("QFIN remains in the current financial priorities")
        return {
            "path": str(path), "sha256": expected_sha256,
            "remaining_observation_count": 0, "status": "RECOVERED",
        }
    scenarios = {scenario for scenario, _age in AUDIT_OBSERVATIONS}
    if len(rows) != 2 or set(rows["scenario"]) != scenarios:
        raise RuntimeError("QFIN baseline audit scenarios changed")
    expected = {
        "missing_signal_count": 1,
        "no_raw_pit_financial_facts_signal_count": 0,
        "insufficient_growth_history_signal_count": 0,
        "stale_growth_snapshot_signal_count": 1,
    }
    for column, value in expected.items():
        if not rows[column].eq(value).all():
            raise RuntimeError(f"QFIN baseline {column} changed")
    if set(rows["first_missing_signal_date"]) != {SIGNAL_DATE}:
        raise RuntimeError("QFIN baseline signal date changed")
    return {
        "path": str(path), "sha256": expected_sha256,
        "missing_observation_count": 2,
        "classification": "q3_m9_sec_exhibit_parser_omission",
    }


def recovered_observations(payload: dict | None = None) -> pd.DataFrame:
    evidence = ttm_evidence(payload)
    derived = evidence["derived"]
    return pd.DataFrame([{
        "scenario": scenario,
        "ticker": TICKER,
        "signal_date": SIGNAL_DATE,
        "maximum_age_days": age,
        "resolved": True,
        "decision": "recovered_exact_us_gaap_annual_m9_ttm_growth_bundle",
        "revenue_ttm_cny": derived["current_ttm_cny_thousands"]["revenue"] * SOURCE_SCALE,
        "net_income_ttm_cny": derived["current_ttm_cny_thousands"]["net_income"] * SOURCE_SCALE,
        "revenue_growth": derived["growth"]["revenue"],
        "net_income_growth": derived["growth"]["net_income"],
        "passes_net_income_growth_gate": derived["growth"]["net_income"] >= 0.15,
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
    envelope, payload = _load_payload(ANNUAL_CACHE_PATH)
    evidence = ttm_evidence(payload)
    facts = strict_quarterly_facts(payload)
    observations = recovered_observations(payload)
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
            "path": str(current_audit_path), "sha256": expected_current_audit_sha256,
            "remaining_observation_count": 2,
            "status": "PENDING_CANDIDATE_INTEGRATION",
        }
    facts_path = output_dir / "strict_quarterly_facts.csv"
    evidence_path = output_dir / "ttm_evidence.json"
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
        "accepted_fact_count": len(facts),
        "resolved_audit_observation_count": len(observations),
        "annual_source": {
            "path": str(ANNUAL_CACHE_PATH),
            "sha256": ANNUAL_CACHE_SHA256,
            "url": envelope["source_url"],
        },
        "source_documents": sources,
        "source_value_verification": source_verification,
        "guardrails": evidence["guardrails"],
        "audit_binding": {
            "baseline": baseline, "current": current,
            "recovered_observation_count": len(observations),
        },
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path), "sha256": _sha256(facts_path),
            },
            "ttm_evidence": {
                "path": str(evidence_path), "sha256": _sha256(evidence_path),
            },
            "recovered_observations": {
                "path": str(observations_path), "sha256": _sha256(observations_path),
                "row_count": len(observations),
            },
        },
        "guardrail": (
            "Uses only same-accession CNY annual comparatives and exact GAAP "
            "nine-month statement rows from the two pre-signal Q3 6-K exhibits. "
            "Non-GAAP values, USD convenience translations, estimates, and the "
            "post-signal FY2020 20-F are excluded. The recovered net-income "
            "growth remains below the frozen gate, so missingness is removed "
            "without making QFIN eligible. Formal financial files are unchanged."
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
    base_dir = Path(base_dir)
    supplement_dir = Path(supplement_dir)
    output_dir = Path(output_dir)
    inputs = (
        base_dir / "annual.csv", base_dir / "quarterly.csv",
        base_dir / "manifest.json", supplement_dir / "strict_quarterly_facts.csv",
        supplement_dir / "manifest.json",
    )
    bound = {path: _sha256(path) for path in inputs}
    base = pd.read_csv(inputs[1])
    incoming = pd.read_csv(inputs[3])
    if list(base.columns) != OUTPUT_COLUMNS or list(incoming.columns) != OUTPUT_COLUMNS:
        raise RuntimeError("QFIN integration requires the quarterly schema")
    if len(incoming) != 4 or not _target_mask(incoming).all():
        raise RuntimeError("QFIN supplement scope is not the four-field TTM bundle")
    target = _target_mask(base)
    replaced = base.loc[target].copy()
    untouched = base.loc[~target].copy()
    merged = pd.concat([untouched, incoming], ignore_index=True).sort_values(
        ["ticker", "fiscal_end", "metric", "available_date"]
    ).reset_index(drop=True)
    if len(merged) != len(base) - len(replaced) + len(incoming):
        raise RuntimeError("QFIN integration row count changed unexpectedly")
    output_dir.mkdir(parents=True, exist_ok=True)
    annual_path = output_dir / "annual.csv"
    quarterly_path = output_dir / "quarterly.csv"
    shutil.copyfile(inputs[0], annual_path)
    merged.to_csv(quarterly_path, index=False)
    if {path: _sha256(path) for path in inputs} != bound:
        raise RuntimeError("QFIN integration source changed while being read")
    report = {
        "schema_version": 1, "research_only": True,
        "formal_financials_modified": False, "release_status": "BLOCKED",
        "promotion_eligible": False, "overlay_ticker": TICKER,
        "overlay_fiscal_end": FISCAL_END,
        "overlay_metrics": sorted(TARGET_METRICS),
        "removed_conflicting_rows": len(replaced),
        "inserted_strict_rows": len(incoming),
        "base": {"path": str(base_dir), "sha256": {
            str(path): digest for path, digest in bound.items()
        }},
        "outputs": {
            "annual": str(annual_path), "annual_sha256": _sha256(annual_path),
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
            base_dir=args.base_dir, supplement_dir=args.output_dir,
            output_dir=args.candidate_output_dir,
        )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
