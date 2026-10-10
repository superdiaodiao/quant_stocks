#!/usr/bin/env python3
"""Recover STNE's exact disclosed-precision 2020Q3 IFRS quarter."""

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

from src.io.fundamentals_update import OUTPUT_COLUMNS


TICKER = "STNE"
CIK = 1_745_431
CURRENCY = "BRL"
SOURCE_SCALE = 1_000_000
FISCAL_END = "2020-09-30"
AVAILABLE_DATE = "2020-10-29"
SIGNAL_DATE = "2021-02-26"
FETCHED_AT = "2026-08-29"
OUTPUT_DIR = Path("output/research_only/v14/stne_q3_2020")
SEC_HEADERS = {"User-Agent": "quant-stocks-research contact@example.com"}

BASE_CANDIDATE_DIR = Path(
    "output/research_only/v14/"
    "candidate_fundamentals_v14_checkpoint_20260829_rcel_exact_annual_loss"
)
BASELINE_AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260829_rcel_exact_annual_loss_recovered_financial_priorities.csv"
)
EXPECTED_BASELINE_AUDIT_SHA256 = (
    "2c73eb43e9364645e154f2703c2d740d9d420a9eb0453d40d009eef2c12cc75b"
)
CURRENT_AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260829_stne_q3_recovered_financial_priorities.csv"
)
EXPECTED_CURRENT_AUDIT_SHA256 = (
    "a48029b30e5835eb8ae0b240c7526589696d44cf28a64e286222f26be09d25ed"
)

SOURCE_DOCUMENT = {
    "role": "contemporaneous_2020q3_ifrs_statement",
    "form": "6-K Exhibit 99.1",
    "filed": AVAILABLE_DATE,
    "accepted_at": "2020-10-29T20:49:17Z",
    "accession": "0000950103-20-021026",
    "document": "dp139707_ex9901.htm",
    "expected_sha256": (
        "aaabcf1ac24677bb5e0380613f76fe25e291c3f5b2b1aab3b94f79c2cd2afdd3"
    ),
}
SOURCE_TEXT_CHECKS = (
    "StoneCo Reports Third Quarter 2020 Financial Results",
    "Statement of Profit or Loss (R$mm)",
    "Total Revenue and Income",
    "Net income for the period",
    "Adjusted Net Income",
)
SOURCE_ROW_CHECKS = (
    {
        "check_id": "q3_total_revenue_and_income",
        "row_label": "Total revenue and income",
        "expected": (934.3, 100.0, 671.1, 100.0, 39.2),
    },
    {
        "check_id": "q3_ifrs_net_income",
        "row_label": "Net income for the period",
        "expected": (249.1, 26.7, 191.3, 28.5, 30.2),
    },
)
Q3_VALUES_BRL = {"revenue": 934_300_000, "net_income": 249_100_000}
TTM_OPERANDS_BRL = {
    "prior": {
        "revenue": (529_353_000, 535_773_000, 586_192_000, 671_100_000),
        "net_income": (127_113_000, 177_036_000, 171_853_000, 191_300_000),
    },
    "current": {
        "revenue": (782_903_000, 716_756_000, 667_400_000, 934_300_000),
        "net_income": (264_006_000, 158_619_000, 123_600_000, 249_100_000),
    },
}
EXPECTED_TTM_BRL = {
    "prior": {"revenue": 2_322_418_000, "net_income": 667_302_000},
    "current": {"revenue": 3_101_359_000, "net_income": 795_325_000},
}
EXPECTED_GROWTH = {
    "revenue": 0.335400862377057,
    "net_income": 0.19185166536290915,
}
TARGET_METRICS = frozenset({"revenue", "net_income"})
AUDIT_OBSERVATIONS = (
    ("liq2000000-age150-growth", 150),
    ("liq10000000-age150-growth", 150),
)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _source_url(source: dict = SOURCE_DOCUMENT) -> str:
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
    raise RuntimeError(f"failed to download locked STNE source: {url}") from error


def _normalize(value: object) -> str:
    return " ".join(
        str(value).replace("\xa0", " ").replace("\u200b", " ").split()
    )


def _html_text(payload: bytes) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(payload, "lxml")
    return _normalize(soup.get_text(" ", strip=True))


def _numeric_cells(values) -> list[float]:
    numbers = []
    for value in values:
        text = _normalize(value)
        if not text or text.casefold() == "nan" or text in {"%", ")", "—"}:
            continue
        negative = text.startswith("(") or text.endswith(")")
        cleaned = re.sub(r"[^0-9.-]", "", text)
        if not re.fullmatch(r"-?\d+(?:\.\d+)?", cleaned):
            continue
        number = float(cleaned)
        numbers.append(-abs(number) if negative else number)
    return numbers


def _tuple_matches(values: list[float], expected: tuple[float, ...]) -> bool:
    return len(values) == len(expected) and all(
        abs(actual - wanted) < 1e-12
        for actual, wanted in zip(values, expected)
    )


def verify_source_values(payload: bytes) -> list[dict]:
    verified = []
    tables = pd.read_html(BytesIO(payload))
    for check in SOURCE_ROW_CHECKS:
        expected = tuple(check["expected"])
        matches = []
        for table in tables:
            header = _normalize(table.iloc[0, 0]) if not table.empty else ""
            if header.casefold() != "statement of profit or loss (r$mm)".casefold():
                continue
            for _, row in table.iterrows():
                if _normalize(row.iloc[0]).casefold() != _normalize(
                    check["row_label"]
                ).casefold():
                    continue
                values = _numeric_cells(row.iloc[1:])
                if _tuple_matches(values, expected):
                    matches.append(tuple(values))
        if len(matches) != 1:
            raise RuntimeError(
                f"STNE source row changed for {check['check_id']}: {matches}"
            )
        verified.append({
            "check_id": check["check_id"],
            "row_label": check["row_label"],
            "expected_values_r_millions": list(expected),
            "match_count": len(matches),
        })
    return verified


def validate_source_lock(source: dict | None = None) -> None:
    document = SOURCE_DOCUMENT if source is None else source
    for field in ("form", "accepted_at", "accession", "document"):
        if document[field] != SOURCE_DOCUMENT[field]:
            raise ValueError(f"STNE source changed locked identity field {field}")
    if document["filed"] > SIGNAL_DATE:
        raise ValueError("STNE source violates the PIT cutoff")
    if document["filed"] != AVAILABLE_DATE:
        raise ValueError("STNE fact availability must equal the 6-K filing date")
    if not re.fullmatch(r"[0-9a-f]{64}", document["expected_sha256"]):
        raise ValueError("STNE source has an invalid SHA-256")


def prepare_verified_source(output_dir: Path) -> tuple[dict, list[dict]]:
    validate_source_lock()
    path = output_dir / "sources" / SOURCE_DOCUMENT["document"]
    downloaded = False
    if path.exists():
        payload = path.read_bytes()
    else:
        payload = _download_bytes(_source_url())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        downloaded = True
    actual_sha = _sha256_bytes(payload)
    if actual_sha != SOURCE_DOCUMENT["expected_sha256"]:
        raise RuntimeError(f"STNE source SHA-256 changed: {actual_sha}")
    text = _html_text(payload).casefold()
    missing = [
        fragment for fragment in SOURCE_TEXT_CHECKS
        if _normalize(fragment).casefold() not in text
    ]
    if missing:
        raise RuntimeError(f"STNE source text changed: {missing}")
    provenance = {
        **SOURCE_DOCUMENT,
        "url": _source_url(),
        "local_path": str(path),
        "actual_sha256": actual_sha,
        "bytes": len(payload),
        "downloaded": downloaded,
    }
    return provenance, verify_source_values(payload)


def _growth(current: int, prior: int) -> float:
    if prior == 0:
        raise ValueError("STNE TTM growth denominator cannot be zero")
    return float((Decimal(current) - Decimal(prior)) / abs(Decimal(prior)))


def ttm_evidence() -> dict:
    totals = {
        basis: {
            metric: sum(TTM_OPERANDS_BRL[basis][metric])
            for metric in ("revenue", "net_income")
        }
        for basis in ("prior", "current")
    }
    if totals != EXPECTED_TTM_BRL:
        raise RuntimeError("STNE existing disclosed-precision TTM operands changed")
    growth = {
        metric: _growth(totals["current"][metric], totals["prior"][metric])
        for metric in ("revenue", "net_income")
    }
    for metric, expected in EXPECTED_GROWTH.items():
        if abs(growth[metric] - expected) > 1e-15:
            raise RuntimeError(f"STNE {metric} growth changed: {growth[metric]}")
    return {
        "ticker": TICKER,
        "currency": CURRENCY,
        "fiscal_end": FISCAL_END,
        "available_date": AVAILABLE_DATE,
        "signal_date": SIGNAL_DATE,
        "financial_age_days": int(
            (pd.Timestamp(SIGNAL_DATE) - pd.Timestamp(AVAILABLE_DATE)).days
        ),
        "q3_2020_values_brl": Q3_VALUES_BRL,
        "ttm_operands_brl": TTM_OPERANDS_BRL,
        "derived": {"ttm_brl": totals, "growth": growth},
        "accounting_boundary": {
            "presentation_currency": "BRL",
            "ifrs_total_revenue_and_income": True,
            "ifrs_net_income_for_period": True,
            "reported_precision_preserved": "R$0.1m",
            "adjusted_net_income_excluded": True,
            "post_signal_filings_excluded": True,
        },
    }


def strict_quarterly_facts() -> pd.DataFrame:
    concepts = {
        "revenue": "RevenueAndOperatingIncome",
        "net_income": "ProfitLoss",
    }
    facts = pd.DataFrame([{
        "ticker": TICKER,
        "fiscal_end": FISCAL_END,
        "available_date": AVAILABLE_DATE,
        "metric": metric,
        "value": value,
        "taxonomy": "ifrs-full",
        "concept": f"stne_q3_disclosed_precision:{concepts[metric]}:BRL",
        "form": "6-K",
        "accession": SOURCE_DOCUMENT["accession"],
        "fetched_at": FETCHED_AT,
    } for metric, value in Q3_VALUES_BRL.items()], columns=OUTPUT_COLUMNS)
    if len(facts) != 2 or set(facts["metric"]) != TARGET_METRICS:
        raise RuntimeError("STNE recovery must contain the paired Q3 IFRS facts")
    return facts


def _audit_rows(path: Path, expected_sha256: str) -> pd.DataFrame:
    actual_sha = _sha256(path)
    if actual_sha != expected_sha256:
        raise RuntimeError(f"STNE audit binding changed: {actual_sha}")
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
            raise RuntimeError("STNE remains in the current financial priorities")
        return {
            "path": str(path), "sha256": expected_sha256,
            "remaining_observation_count": 0, "status": "RECOVERED",
        }
    scenarios = {scenario for scenario, _age in AUDIT_OBSERVATIONS}
    if len(rows) != 2 or set(rows["scenario"]) != scenarios:
        raise RuntimeError("STNE baseline audit scenarios changed")
    expected = {
        "missing_signal_count": 1,
        "no_raw_pit_financial_facts_signal_count": 0,
        "insufficient_growth_history_signal_count": 0,
        "stale_growth_snapshot_signal_count": 1,
    }
    for column, value in expected.items():
        if not rows[column].eq(value).all():
            raise RuntimeError(f"STNE baseline {column} changed")
    if set(rows["first_missing_signal_date"]) != {SIGNAL_DATE}:
        raise RuntimeError("STNE baseline signal date changed")
    return {
        "path": str(path), "sha256": expected_sha256,
        "missing_observation_count": 2,
        "classification": "q3_2020_sec_exhibit_parser_omission",
    }


def recovered_observations() -> pd.DataFrame:
    evidence = ttm_evidence()
    growth = evidence["derived"]["growth"]
    return pd.DataFrame([{
        "scenario": scenario,
        "ticker": TICKER,
        "signal_date": SIGNAL_DATE,
        "maximum_age_days": age,
        "resolved": True,
        "decision": "recovered_exact_disclosed_precision_ifrs_q3",
        "revenue_growth": growth["revenue"],
        "net_income_growth": growth["net_income"],
        "passes_growth_gates": (
            growth["revenue"] >= 0.15 and growth["net_income"] >= 0.15
        ),
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
    source, source_verification = prepare_verified_source(output_dir)
    evidence = ttm_evidence()
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
        "source_document": source,
        "source_value_verification": source_verification,
        "accounting_boundary": evidence["accounting_boundary"],
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
                "path": str(observations_path),
                "sha256": _sha256(observations_path), "row_count": len(observations),
            },
        },
        "guardrail": (
            "Adds only the original pre-signal 2020Q3 IFRS Total revenue and "
            "income and Net income for the period at the issuer's R$0.1m "
            "disclosed precision. The separate adjusted net-income row, "
            "estimates, added precision, and post-signal filings are excluded. "
            "Existing earlier STNE quarter provenance remains unchanged, and "
            "formal financial files are not modified."
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
        raise RuntimeError("STNE integration requires the quarterly schema")
    if len(incoming) != 2 or not _target_mask(incoming).all():
        raise RuntimeError("STNE supplement scope is not the paired Q3 facts")
    target = _target_mask(base)
    replaced = base.loc[target].copy()
    untouched = base.loc[~target].copy()
    merged = pd.concat([untouched, incoming], ignore_index=True).sort_values(
        ["ticker", "fiscal_end", "metric", "available_date"]
    ).reset_index(drop=True)
    if len(merged) != len(base) - len(replaced) + len(incoming):
        raise RuntimeError("STNE integration row count changed unexpectedly")
    output_dir.mkdir(parents=True, exist_ok=True)
    annual_path = output_dir / "annual.csv"
    quarterly_path = output_dir / "quarterly.csv"
    shutil.copyfile(inputs[0], annual_path)
    merged.to_csv(quarterly_path, index=False)
    if {path: _sha256(path) for path in inputs} != bound:
        raise RuntimeError("STNE integration source changed while being read")
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
