#!/usr/bin/env python3
"""Recover RCEL's exact pre-signal audited FY2019 IFRS loss."""

from __future__ import annotations

import argparse
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


TICKER = "RCEL"
CIK = 1_762_303
CURRENCY = "AUD"
FISCAL_END = "2019-06-30"
AVAILABLE_DATE = "2019-10-31"
SIGNAL_DATE = "2019-12-31"
FETCHED_AT = "2026-08-29"
EXPECTED_NET_INCOME_TTM_AUD = -35_160_227
OUTPUT_DIR = Path("output/research_only/v14/rcel_exact_annual_loss")
SEC_HEADERS = {"User-Agent": "quant-stocks-research contact@example.com"}

BASE_CANDIDATE_DIR = Path(
    "output/research_only/v14/"
    "candidate_fundamentals_v14_checkpoint_20260829_qfin_m9_ttm"
)
BASELINE_AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260829_qfin_m9_ttm_recovered_financial_priorities.csv"
)
EXPECTED_BASELINE_AUDIT_SHA256 = (
    "c05a233fa07ff5209e8200ab2e199d9b9b3a1db624c9cb3df37efc0e01e3ec9f"
)
CURRENT_AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260829_rcel_exact_annual_loss_recovered_financial_priorities.csv"
)
EXPECTED_CURRENT_AUDIT_SHA256 = (
    "2c73eb43e9364645e154f2703c2d740d9d420a9eb0453d40d009eef2c12cc75b"
)

SOURCE_DOCUMENTS = {
    "original_20f": {
        "role": "audited_fy2019_ifrs_loss",
        "form": "20-F",
        "filed": AVAILABLE_DATE,
        "accepted_at": "2019-10-31T19:49:06Z",
        "accession": "0001193125-19-280420",
        "document": "d818680d20f.htm",
        "expected_sha256": (
            "99f59357b6fe5a914fc557c57d649630b0f9945ab3acc5ab54692e758f38a5b3"
        ),
    },
    "xbrl_only_20fa": {
        "role": "pre_signal_confirmation_original_20f_unchanged",
        "form": "20-F/A",
        "filed": "2019-11-19",
        "accepted_at": "2019-11-19T22:22:36Z",
        "accession": "0001193125-19-295696",
        "document": "d818680d20fa.htm",
        "expected_sha256": (
            "e87230bf49c6e8ad497f350ecb2eee2adb544e215d579977aea9e7bfa95e72c9"
        ),
    },
}

SOURCE_TEXT_CHECKS = {
    "original_20f": (
        "AVITA MEDICAL LIMITED",
        "CONSOLIDATED STATEMENT OF PROFIT OR LOSS AND OTHER COMPREHENSIVE INCOME",
        "(IN AUSTRALIAN DOLLARS)",
        "Loss for the period",
        "prepared in Australian dollars and in accordance with the International Financial Reporting Standards",
    ),
    "xbrl_only_20fa": (
        "solely to furnish Exhibit 101",
        "does not amend any information set forth in the Original 20-F",
        "has not updated disclosures included therein",
    ),
}

SOURCE_ROW_CHECKS = (
    {
        "check_id": "audited_loss_for_period",
        "row_label": "Loss for the period",
        "expected": (-35_160_227, -16_519_155, -11_511_024),
    },
)

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
    raise RuntimeError(f"failed to download locked RCEL source: {url}") from error


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
        if not text or text.casefold() == "nan" or text in {"$", "A$", ")", "—"}:
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


def verify_source_values(original_20f: bytes) -> list[dict]:
    tables = pd.read_html(BytesIO(original_20f))
    verified = []
    for check in SOURCE_ROW_CHECKS:
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
            raise RuntimeError(f"RCEL source row changed for {check['check_id']}")
        verified.append({
            "source_id": "original_20f",
            "check_id": check["check_id"],
            "row_label": check["row_label"],
            "expected_values_aud": list(expected),
            "match_count": len(matches),
        })
    return verified


def validate_source_lock(sources: dict[str, dict] | None = None) -> None:
    documents = SOURCE_DOCUMENTS if sources is None else sources
    if set(documents) != set(SOURCE_DOCUMENTS):
        raise ValueError("RCEL raw source set changed")
    for source_id, source in documents.items():
        locked = SOURCE_DOCUMENTS[source_id]
        for field in ("form", "accepted_at", "accession", "document"):
            if source[field] != locked[field]:
                raise ValueError(
                    f"RCEL source {source_id} changed locked identity field {field}"
                )
        if source["filed"] > SIGNAL_DATE:
            raise ValueError(f"RCEL source {source_id} violates the PIT cutoff")
        if not re.fullmatch(r"[0-9a-f]{64}", source["expected_sha256"]):
            raise ValueError(f"RCEL source {source_id} has an invalid SHA-256")
    if documents["original_20f"]["filed"] != AVAILABLE_DATE:
        raise ValueError("RCEL fact availability must equal the original 20-F filing")
    if documents["xbrl_only_20fa"]["filed"] <= AVAILABLE_DATE:
        raise ValueError("RCEL XBRL-only amendment chronology changed")


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
                f"RCEL source SHA-256 changed for {source_id}: {actual_sha}"
            )
        text = _html_text(payload).casefold()
        missing = [
            fragment for fragment in SOURCE_TEXT_CHECKS[source_id]
            if _normalize(fragment).casefold() not in text
        ]
        if missing:
            raise RuntimeError(f"RCEL source text changed for {source_id}: {missing}")
        raw_by_source[source_id] = payload
        provenance[source_id] = {
            **source,
            "url": _source_url(source),
            "local_path": str(path),
            "actual_sha256": actual_sha,
            "bytes": len(payload),
            "downloaded": downloaded,
        }
    return provenance, verify_source_values(raw_by_source["original_20f"])


def ttm_evidence() -> dict:
    return {
        "ticker": TICKER,
        "currency": CURRENCY,
        "fiscal_end": FISCAL_END,
        "available_date": AVAILABLE_DATE,
        "signal_date": SIGNAL_DATE,
        "financial_age_days": int(
            (pd.Timestamp(SIGNAL_DATE) - pd.Timestamp(AVAILABLE_DATE)).days
        ),
        "net_income_ttm_aud": EXPECTED_NET_INCOME_TTM_AUD,
        "statement_row": "Loss for the period",
        "accounting_boundary": {
            "presentation_currency": "AUD",
            "ifrs_as_issued_by_iasb": True,
            "audited_consolidated_statement": True,
            "loss_for_period_not_comprehensive_loss": True,
            "xbrl_only_amendment_changed_financials": False,
            "post_signal_filings_excluded": True,
        },
    }


def strict_quarterly_facts() -> pd.DataFrame:
    fact = pd.DataFrame([{
        "ticker": TICKER,
        "fiscal_end": FISCAL_END,
        "available_date": AVAILABLE_DATE,
        "metric": "net_income_ttm",
        "value": EXPECTED_NET_INCOME_TTM_AUD,
        "taxonomy": "ifrs-full",
        "concept": "rcel_exact_annual_loss:LossForThePeriod:AUD",
        "form": "20-F",
        "accession": SOURCE_DOCUMENTS["original_20f"]["accession"],
        "fetched_at": FETCHED_AT,
    }], columns=OUTPUT_COLUMNS)
    if len(fact) != 1 or set(fact["metric"]) != TARGET_METRICS:
        raise RuntimeError("RCEL recovery must contain only exact TTM net income")
    if float(fact.iloc[0]["value"]) >= 0:
        raise RuntimeError("RCEL recovery no longer proves nonpositive profit")
    return fact


def _audit_rows(path: Path, expected_sha256: str) -> pd.DataFrame:
    actual_sha = _sha256(path)
    if actual_sha != expected_sha256:
        raise RuntimeError(f"RCEL audit binding changed: {actual_sha}")
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
            raise RuntimeError("RCEL remains in the current financial priorities")
        return {
            "path": str(path),
            "sha256": expected_sha256,
            "remaining_observation_count": 0,
            "status": "RECOVERED",
        }
    scenarios = {scenario for scenario, _age in AUDIT_OBSERVATIONS}
    if len(rows) != 3 or set(rows["scenario"]) != scenarios:
        raise RuntimeError("RCEL baseline audit scenarios changed")
    expected = {
        "missing_signal_count": 1,
        "no_raw_pit_financial_facts_signal_count": 1,
        "insufficient_growth_history_signal_count": 0,
        "stale_growth_snapshot_signal_count": 0,
    }
    for column, value in expected.items():
        if not rows[column].eq(value).all():
            raise RuntimeError(f"RCEL baseline {column} changed")
    if set(rows["first_missing_signal_date"]) != {SIGNAL_DATE}:
        raise RuntimeError("RCEL baseline signal date changed")
    return {
        "path": str(path),
        "sha256": expected_sha256,
        "missing_observation_count": 3,
        "classification": "historical_foreign_issuer_cik_parser_omission",
    }


def recovered_observations() -> pd.DataFrame:
    evidence = ttm_evidence()
    return pd.DataFrame([{
        "scenario": scenario,
        "ticker": TICKER,
        "signal_date": SIGNAL_DATE,
        "maximum_age_days": age,
        "resolved": True,
        "decision": "known_nonpositive_profit",
        "net_income_ttm_aud": EXPECTED_NET_INCOME_TTM_AUD,
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
            "path": str(current_audit_path),
            "sha256": expected_current_audit_sha256,
            "remaining_observation_count": 3,
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
        "source_documents": sources,
        "source_value_verification": source_verification,
        "accounting_boundary": evidence["accounting_boundary"],
        "audit_binding": {
            "baseline": baseline,
            "current": current,
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
                "sha256": _sha256(observations_path),
                "row_count": len(observations),
            },
        },
        "guardrail": (
            "Uses only the audited consolidated IFRS Loss for the period in "
            "the original pre-signal FY2019 20-F. The later pre-signal 20-F/A "
            "is source-locked because it expressly added only XBRL and did not "
            "amend the original financial information. Total comprehensive "
            "loss, revenue growth, non-GAAP values, and post-signal filings are "
            "excluded. The exact negative TTM profit state resolves the frozen "
            "profit gate without inventing growth. Formal files are unchanged."
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
        raise RuntimeError("RCEL integration requires the quarterly schema")
    if len(incoming) != 1 or not _target_mask(incoming).all():
        raise RuntimeError("RCEL supplement scope is not the exact annual loss")
    target = _target_mask(base)
    replaced = base.loc[target].copy()
    untouched = base.loc[~target].copy()
    merged = pd.concat([untouched, incoming], ignore_index=True).sort_values(
        ["ticker", "fiscal_end", "metric", "available_date"]
    ).reset_index(drop=True)
    if len(merged) != len(base) - len(replaced) + len(incoming):
        raise RuntimeError("RCEL integration row count changed unexpectedly")
    output_dir.mkdir(parents=True, exist_ok=True)
    annual_path = output_dir / "annual.csv"
    quarterly_path = output_dir / "quarterly.csv"
    shutil.copyfile(inputs[0], annual_path)
    merged.to_csv(quarterly_path, index=False)
    if {path: _sha256(path) for path in inputs} != bound:
        raise RuntimeError("RCEL integration source changed while being read")
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
