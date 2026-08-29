#!/usr/bin/env python3
"""Expose PLL's exact pre-signal H1-derived TTM loss as exclusion evidence."""

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


TICKER = "PLL"
CIK = 1_728_205
CURRENCY = "USD"
FISCAL_END = "2020-12-31"
AVAILABLE_DATE = "2021-03-18"
SIGNAL_DATE = "2021-03-31"
FETCHED_AT = "2026-08-29"
OUTPUT_DIR = Path("output/research_only/v14/pll_exact_ttm_loss")
SEC_HEADERS = {"User-Agent": "quant-stocks-research contact@example.com"}

BASE_CANDIDATE_DIR = Path(
    "output/research_only/v14/"
    "candidate_fundamentals_v14_checkpoint_20260829_ncty_h1_ttm"
)
BASELINE_AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260829_ncty_h1_ttm_recovered_financial_priorities.csv"
)
EXPECTED_BASELINE_AUDIT_SHA256 = (
    "21307afa31a7c768b42f60dccc4c618bb4974b565754091b3cd40b33113d7f8d"
)
CURRENT_AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260829_pll_exact_ttm_loss_recovered_financial_priorities.csv"
)
EXPECTED_CURRENT_AUDIT_SHA256 = (
    "548070b273b41f91e1a3d94d25e6adf331c079c8d2e589c5d9c74def0d800133"
)

SOURCE_DOCUMENTS = {
    "fy2020_20f": {
        "role": "audited_fy2020_and_fy2019_operands",
        "form": "20-F",
        "filed": "2020-10-13",
        "accepted_at": "2020-10-13T06:09:19Z",
        "accession": "0001140361-20-022848",
        "document": "nc10015300x1_20f.htm",
        "expected_sha256": (
            "c97d70bcec0a42d7dc9368d08e5dc09bb77a6c3b49a126c602506d665d2554c1"
        ),
    },
    "h1_2021_6k": {
        "role": "unaudited_h1_2021_and_h1_2020_operands",
        "form": "6-K Exhibit 99.1",
        "filed": AVAILABLE_DATE,
        "accepted_at": "2021-03-18T09:49:53Z",
        "accession": "0001140361-21-009007",
        "document": "brhc10021723_ex99-1.htm",
        "expected_sha256": (
            "b6e617aa9c1b474bd28f0e15feb14a2740650d9337a1a609467ca13bcdb2584b"
        ),
    },
}

SOURCE_TEXT_CHECKS = {
    "fy2020_20f": (
        "ANNUAL REPORT PURSUANT TO SECTION 13 OR 15(d)",
        "Piedmont Lithium Limited",
        "International Financial Reporting Standards as issued by the International Accounting Standards Board",
    ),
    "h1_2021_6k": (
        "UNAUDITED CONDENSED CONSOLIDATED FINANCIAL STATEMENTS",
        "for the six months ended December 31, 2020 and 2019",
        "International Accounting Standard",
        "34 Interim Financial Reporting",
        "presented in United States dollars (US$)",
    ),
}

SOURCE_ROW_CHECKS = {
    "fy2020_20f": ({
        "check_id": "annual_profit_loss",
        "row_label": "Loss for the year",
        "expected": (-5_671_099, -9_822_626, -9_957_817),
    },),
    "h1_2021_6k": ({
        "check_id": "half_year_profit_loss",
        "row_label": "Loss for the period",
        "expected": (-5_465_327, -3_475_979),
    },),
}

OPERANDS_USD = {
    "fy2020": -5_671_099,
    "h1_2020": -3_475_979,
    "h1_2021": -5_465_327,
}
EXPECTED_TTM_USD = -7_660_447
TARGET_METRICS = frozenset({"net_income_ttm"})
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
    raise RuntimeError(f"failed to download locked PLL source: {url}") from error


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
        raise ValueError("PLL raw source set does not match the source lock")
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
                    f"PLL source row changed for {check['check_id']}"
                )
            verified.append({
                "source_id": source_id,
                "check_id": check["check_id"],
                "row_label": check["row_label"],
                "expected_values_usd": list(expected),
                "match_count": len(matches),
            })
    return verified


def validate_source_lock(sources: dict[str, dict] | None = None) -> None:
    documents = SOURCE_DOCUMENTS if sources is None else sources
    if set(documents) != set(SOURCE_DOCUMENTS):
        raise ValueError("PLL source document set changed")
    for source_id, source in documents.items():
        locked = SOURCE_DOCUMENTS[source_id]
        for field in ("form", "accepted_at", "accession", "document"):
            if source[field] != locked[field]:
                raise ValueError(
                    f"PLL source {source_id} changed locked identity field {field}"
                )
        if source["filed"] > SIGNAL_DATE:
            raise ValueError(f"PLL source {source_id} violates the PIT cutoff")
        if not re.fullmatch(r"[0-9a-f]{64}", source["expected_sha256"]):
            raise ValueError(f"PLL source {source_id} has an invalid SHA-256")
        url = _source_url(source)
        if f"/data/{CIK}/{source['accession'].replace('-', '')}/" not in url:
            raise ValueError(f"PLL source {source_id} does not lock CIK/accession")
        if not url.endswith("/" + source["document"]):
            raise ValueError(f"PLL source {source_id} does not lock document")
    if AVAILABLE_DATE != max(source["filed"] for source in documents.values()):
        raise ValueError("PLL availability date is not the latest operand filing")


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
                f"PLL source SHA-256 changed for {source_id}: {actual_sha}"
            )
        text = _html_text(payload).casefold()
        missing = [
            fragment for fragment in SOURCE_TEXT_CHECKS[source_id]
            if _normalize(fragment).casefold() not in text
        ]
        if missing:
            raise RuntimeError(f"PLL source text changed for {source_id}: {missing}")
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


def ttm_evidence() -> dict:
    ttm = (
        OPERANDS_USD["fy2020"]
        - OPERANDS_USD["h1_2020"]
        + OPERANDS_USD["h1_2021"]
    )
    if ttm != EXPECTED_TTM_USD or ttm >= 0:
        raise RuntimeError(f"PLL exact TTM loss changed: {ttm}")
    return {
        "ticker": TICKER,
        "currency": CURRENCY,
        "fiscal_end": FISCAL_END,
        "available_date": AVAILABLE_DATE,
        "signal_date": SIGNAL_DATE,
        "financial_age_days": int(
            (pd.Timestamp(SIGNAL_DATE) - pd.Timestamp(AVAILABLE_DATE)).days
        ),
        "operands_usd": OPERANDS_USD,
        "net_income_ttm_usd": ttm,
        "formula": "FY2020 - H1_FY2020 + H1_FY2021",
        "metric_mapping": {
            "net_income": "IFRS consolidated Loss for the year/period",
        },
        "accounting_boundary": {
            "standard": "IFRS as issued by IASB; interim report follows IAS 34",
            "presentation_currency": "USD",
            "same_currency_all_operands": True,
            "consolidated_basis_consistent": True,
            "interim_accounting_policies_consistent_with_annual": True,
            "adjusted_metrics_excluded": True,
            "later_us_gaap_redomiciliation_filings_excluded": True,
        },
    }


def strict_quarterly_facts() -> pd.DataFrame:
    evidence = ttm_evidence()
    accessions = "+".join(
        source["accession"] for source in SOURCE_DOCUMENTS.values()
    )
    facts = pd.DataFrame([{
        "ticker": TICKER,
        "fiscal_end": FISCAL_END,
        "available_date": AVAILABLE_DATE,
        "metric": "net_income_ttm",
        "value": evidence["net_income_ttm_usd"],
        "taxonomy": "ifrs-full",
        "concept": "pll_annual_h1_ttm:ProfitLoss:USD",
        "form": "20-F_PLUS_6-K_H1_CUMULATIVE_TTM",
        "accession": accessions,
        "fetched_at": FETCHED_AT,
    }], columns=OUTPUT_COLUMNS)
    if len(facts) != 1 or set(facts["metric"]) != TARGET_METRICS:
        raise RuntimeError("PLL recovery must contain only exact TTM loss")
    return facts


def _audit_rows(path: Path, expected_sha256: str) -> pd.DataFrame:
    actual_sha = _sha256(path)
    if actual_sha != expected_sha256:
        raise RuntimeError(f"PLL audit binding changed: {actual_sha}")
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
            raise RuntimeError("PLL remains in the current financial priorities")
        return {
            "path": str(path), "sha256": expected_sha256,
            "remaining_observation_count": 0, "status": "RECOVERED",
        }
    scenarios = {scenario for scenario, _age in AUDIT_OBSERVATIONS}
    if len(rows) != 2 or set(rows["scenario"]) != scenarios:
        raise RuntimeError("PLL baseline audit scenarios changed")
    expected = {
        "missing_signal_count": 1,
        "no_raw_pit_financial_facts_signal_count": 1,
        "insufficient_growth_history_signal_count": 0,
        "stale_growth_snapshot_signal_count": 0,
    }
    for column, value in expected.items():
        if not rows[column].eq(value).all():
            raise RuntimeError(f"PLL baseline {column} changed")
    if set(rows["first_missing_signal_date"]) != {SIGNAL_DATE}:
        raise RuntimeError("PLL baseline signal date changed")
    return {
        "path": str(path),
        "sha256": expected_sha256,
        "missing_observation_count": 2,
        "classification": "foreign_h1_sec_exhibit_parser_omission",
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
        "net_income_ttm_usd": evidence["net_income_ttm_usd"],
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
        "source_documents": sources,
        "source_value_verification": source_verification,
        "metric_mapping": evidence["metric_mapping"],
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
            "The exclusion fact uses only the pre-signal audited FY2020 20-F "
            "and exact H1 FY2021/H1 FY2020 comparative 6-K amounts in USD. It "
            "emits only a negative TTM profit state; revenue and growth are not "
            "invented. Later US-GAAP redomiciliation filings and adjusted "
            "measures are excluded, and formal financial files remain unchanged."
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
    """Copy-on-write overlay of only PLL's exact negative TTM fact."""
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
        raise RuntimeError("PLL integration requires the quarterly schema")
    if len(incoming) != 1 or not _target_mask(incoming).all():
        raise RuntimeError("PLL supplement scope is not the exact TTM loss")
    target = _target_mask(base)
    replaced = base.loc[target].copy()
    untouched = base.loc[~target].copy()
    merged = pd.concat([untouched, incoming], ignore_index=True).sort_values(
        ["ticker", "fiscal_end", "metric", "available_date"]
    ).reset_index(drop=True)
    if len(merged) != len(untouched) + 1:
        raise RuntimeError("PLL overlay changed rows outside the bounded key space")

    output_dir.mkdir(parents=True, exist_ok=True)
    annual_path = output_dir / "annual.csv"
    quarterly_path = output_dir / "quarterly.csv"
    shutil.copyfile(inputs[0], annual_path)
    merged.to_csv(quarterly_path, index=False)
    if {path: _sha256(path) for path in inputs} != bound:
        raise RuntimeError("PLL integration source changed while being read")
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
