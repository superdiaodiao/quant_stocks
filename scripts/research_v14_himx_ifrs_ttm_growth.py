#!/usr/bin/env python3
"""Recover HIMX's exact pre-signal IFRS TTM growth bundle.

Himax's 2020 20-F and its Q3 2020/Q3 2021 earnings exhibits provide the
audited annual and exact nine-month operands needed to bridge two comparable
TTM windows.  The quarterly exhibits explicitly omit a complete set of IFRS
interim disclosures, so this package uses only their issuer-reported IFRS
statement amounts and excludes every non-IFRS reconciliation value.
"""

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

from bs4 import BeautifulSoup
import pandas as pd

from src.io.fundamentals_update import OUTPUT_COLUMNS


TICKER = "HIMX"
CIK = 1_342_338
CURRENCY = "USD"
SOURCE_SCALE = 1_000
FISCAL_END = "2021-09-30"
AVAILABLE_DATE = "2021-11-04"
SIGNAL_DATE = "2021-12-31"
FETCHED_AT = "2026-08-29"
OUTPUT_DIR = Path("output/research_only/v14/himx_ifrs_ttm_growth")
SEC_HEADERS = {"User-Agent": "quant-stocks-research contact@example.com"}

BASE_CANDIDATE_DIR = Path(
    "output/research_only/v14/"
    "candidate_fundamentals_v14_checkpoint_20260829_gcmg_predecessor_ttm"
)
BASELINE_AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260829_gcmg_predecessor_ttm_recovered_financial_priorities.csv"
)
EXPECTED_BASELINE_AUDIT_SHA256 = (
    "75d91fb6e43e5b9cc7cc2128711ceb7dc694245f117ffc939c7e3c3e0d21afc3"
)
CURRENT_AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260829_himx_ifrs_ttm_recovered_financial_priorities.csv"
)
EXPECTED_CURRENT_AUDIT_SHA256 = (
    "efa698d6aa12b84c50f55b04e5fd91bc7f5fb64669b83ce42e0b20f7ff438e06"
)

SOURCE_DOCUMENTS = {
    "fy2020_20f": {
        "role": "audited_ifrs_fy2019_fy2020_operands",
        "form": "20-F",
        "filed": "2021-03-31",
        "accession": "0001104659-21-044259",
        "document": "tm214047d1_20f.htm",
        "expected_sha256": (
            "c6267e569e96c7796b6da857540f224b6baac974922e334dedc45844b7850508"
        ),
    },
    "q3_2020_6k": {
        "role": "ifrs_m9_2019_m9_2020_operands",
        "form": "6-K_EX-99.1",
        "filed": "2020-11-12",
        "accession": "0001171843-20-007884",
        "document": "exh_991.htm",
        "expected_sha256": (
            "b08a3f194629e0ec4ef0eca6ca1691626a7db82e37b62be7d6dcedc319bb58c8"
        ),
    },
    "q3_2021_6k": {
        "role": "ifrs_m9_2020_m9_2021_operands",
        "form": "6-K_EX-99.1",
        "filed": AVAILABLE_DATE,
        "accession": "0001171843-21-007499",
        "document": "exh_991.htm",
        "expected_sha256": (
            "4c597bec10786d4b6a7942a6232488ce76caad433413eb7a578b9a1c421cc238"
        ),
    },
}

SOURCE_TEXT_CHECKS = {
    "fy2020_20f": (
        "International Financial Reporting Standards as issued by the International Accounting Standards Board",
        "Consolidated Statements of Profit or Loss",
        "Profit (loss) for the year",
    ),
    "q3_2020_6k": (
        "Himax Technologies, Inc.",
        "These interim financials do not fully comply with IFRS",
        "Amounts in Thousands of U.S. Dollars",
        "Nine Months Ended",
    ),
    "q3_2021_6k": (
        "Himax Technologies, Inc.",
        "These interim financials do not fully comply with IFRS",
        "Amounts in Thousands of U.S. Dollars",
        "Nine Months Ended",
        "Non-IFRS Unaudited Supplemental Data",
    ),
}

SOURCE_ROW_CHECKS = {
    "fy2020_20f": (
        {
            "check_id": "audited_annual_revenue",
            "row_label": "Revenues",
            "expected": (723_605, 671_835, 887_282),
        },
        {
            "check_id": "audited_annual_consolidated_profit",
            "row_label": "Profit (loss) for the year",
            "expected": (6_026, -16_184, 45_160),
        },
    ),
    "q3_2020_6k": (
        {
            "check_id": "m9_2020_revenue",
            "row_label": "Revenues",
            "expected": (611_512, 496_906),
        },
        {
            "check_id": "m9_2020_consolidated_profit",
            "row_label": "Profit (loss) for the period",
            "expected": (11_814, -16_749),
        },
    ),
    "q3_2021_6k": (
        {
            "check_id": "m9_2021_third_party_revenue",
            "row_label": "Revenues from third parties, net",
            "expected": (1_095_152, 611_512),
        },
        {
            "check_id": "m9_2021_related_party_revenue",
            "row_label": "Revenues from related parties, net",
            "expected": (50,),
        },
        {
            "check_id": "m9_2021_consolidated_profit",
            "row_label": "Profit for the period",
            "expected": (292_463, 11_814),
        },
    ),
}

OPERANDS_USD_THOUSANDS = {
    "fy2019": {"revenue": 671_835, "net_income": -16_184},
    "fy2020": {"revenue": 887_282, "net_income": 45_160},
    "m9_2019": {"revenue": 496_906, "net_income": -16_749},
    "m9_2020": {"revenue": 611_512, "net_income": 11_814},
    "m9_2021": {"revenue": 1_095_202, "net_income": 292_463},
}
EXPECTED_TTM_USD_THOUSANDS = {
    "previous": {"revenue": 786_441, "net_income": 12_379},
    "current": {"revenue": 1_370_972, "net_income": 325_809},
}
EXPECTED_GROWTH = {
    "revenue": 0.7432610965094648,
    "net_income": 25.319492689231763,
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
            with urlopen(
                Request(url, headers=SEC_HEADERS), timeout=120
            ) as response:
                return response.read()
        except OSError as exc:  # pragma: no cover - network retry
            error = exc
            if attempt < 4:
                time.sleep(attempt + 1)
    raise RuntimeError(f"failed to download locked HIMX source: {url}") from error


def _normalize(value: object) -> str:
    return " ".join(
        str(value).replace("\xa0", " ").replace("\u200b", " ").split()
    )


def _numeric_cells(values) -> list[int]:
    numbers = []
    for value in values:
        text = _normalize(value)
        if not text or text.casefold() == "nan" or text in {"$", ")", "-"}:
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
    width = len(expected)
    return any(
        tuple(values[index:index + width]) == expected
        for index in range(len(values) - width + 1)
    )


def verify_source_values(raw_by_source: dict[str, bytes]) -> list[dict]:
    if set(raw_by_source) != set(SOURCE_DOCUMENTS):
        raise ValueError("HIMX raw source set does not match the source lock")
    verified = []
    for source_id, checks in SOURCE_ROW_CHECKS.items():
        tables = pd.read_html(BytesIO(raw_by_source[source_id]))
        for check in checks:
            matches = []
            expected = tuple(check["expected"])
            label = _normalize(check["row_label"]).casefold()
            for table in tables:
                for _, row in table.iterrows():
                    first = _normalize(row.iloc[0]).casefold()
                    values = _numeric_cells(row.iloc[1:])
                    if first == label and _contains_subsequence(values, expected):
                        matches.append(tuple(values))
            if not matches:
                raise RuntimeError(
                    f"HIMX source row changed for {check['check_id']}"
                )
            verified.append({
                "source_id": source_id,
                "check_id": check["check_id"],
                "row_label": check["row_label"],
                "expected_values_usd_thousands": list(expected),
                "match_count": len(matches),
            })
    return verified


def validate_source_lock(sources: dict[str, dict] | None = None) -> None:
    documents = SOURCE_DOCUMENTS if sources is None else sources
    if set(documents) != set(SOURCE_DOCUMENTS):
        raise ValueError("HIMX source document set changed")
    for source_id, source in documents.items():
        if source["filed"] > SIGNAL_DATE:
            raise ValueError(f"HIMX source {source_id} violates the PIT cutoff")
        if not re.fullmatch(r"[0-9a-f]{64}", source["expected_sha256"]):
            raise ValueError(f"HIMX source {source_id} has an invalid SHA-256")
        url = _source_url(source)
        if f"/data/{CIK}/{source['accession'].replace('-', '')}/" not in url:
            raise ValueError(f"HIMX source {source_id} does not lock CIK/accession")
        if not url.endswith("/" + source["document"]):
            raise ValueError(f"HIMX source {source_id} does not lock document")
    if AVAILABLE_DATE != max(source["filed"] for source in documents.values()):
        raise ValueError("HIMX availability date is not the latest operand filing")


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
                f"HIMX source SHA-256 changed for {source_id}: {actual_sha}"
            )
        text = _normalize(
            BeautifulSoup(payload, "lxml").get_text(" ", strip=True)
        ).casefold()
        missing = [
            fragment for fragment in SOURCE_TEXT_CHECKS[source_id]
            if _normalize(fragment).casefold() not in text
        ]
        if missing:
            raise RuntimeError(f"HIMX source text changed for {source_id}: {missing}")
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
    operands = OPERANDS_USD_THOUSANDS
    previous = {
        metric: operands["fy2019"][metric] - operands["m9_2019"][metric]
        + operands["m9_2020"][metric]
        for metric in ("revenue", "net_income")
    }
    current = {
        metric: operands["fy2020"][metric] - operands["m9_2020"][metric]
        + operands["m9_2021"][metric]
        for metric in ("revenue", "net_income")
    }
    growth = {
        metric: current[metric] / previous[metric] - 1.0
        for metric in ("revenue", "net_income")
    }
    if previous != EXPECTED_TTM_USD_THOUSANDS["previous"]:
        raise RuntimeError(f"HIMX previous TTM changed: {previous}")
    if current != EXPECTED_TTM_USD_THOUSANDS["current"]:
        raise RuntimeError(f"HIMX current TTM changed: {current}")
    for metric, expected in EXPECTED_GROWTH.items():
        if abs(growth[metric] - expected) > 1e-12:
            raise RuntimeError(f"HIMX {metric} growth changed: {growth[metric]}")
    if previous["revenue"] <= 0 or previous["net_income"] <= 0:
        raise RuntimeError("HIMX growth denominator must remain explicitly positive")
    return {
        "ticker": TICKER,
        "currency": CURRENCY,
        "source_scale": SOURCE_SCALE,
        "fiscal_end": FISCAL_END,
        "available_date": AVAILABLE_DATE,
        "signal_date": SIGNAL_DATE,
        "financial_age_days": (
            pd.Timestamp(SIGNAL_DATE) - pd.Timestamp(AVAILABLE_DATE)
        ).days,
        "operands_usd_thousands": operands,
        "derived": {
            "previous_ttm_usd_thousands": previous,
            "current_ttm_usd_thousands": current,
            "growth": growth,
        },
        "metric_mapping": {
            "revenue": "total consolidated Revenues",
            "net_income": "consolidated Profit (loss) for the year/period",
            "candidate_alignment": (
                "The annual candidate maps ifrs-full ProfitLoss, so the bridge "
                "uses consolidated profit rather than stockholder-attributable profit."
            ),
        },
        "accounting_boundary": {
            "annual_basis": "IFRS as issued by the IASB",
            "interim_basis": (
                "issuer-reported IFRS statement amounts with explicitly incomplete "
                "interim disclosures"
            ),
            "currency_and_scale_consistent": True,
            "non_ifrs_reconciliation_excluded": True,
            "adjusted_metrics_excluded": True,
            "later_fy2021_results_excluded": True,
        },
    }


def strict_quarterly_facts() -> pd.DataFrame:
    evidence = ttm_evidence()["derived"]
    values = {
        "revenue_ttm": evidence["current_ttm_usd_thousands"]["revenue"]
        * SOURCE_SCALE,
        "net_income_ttm": evidence["current_ttm_usd_thousands"]["net_income"]
        * SOURCE_SCALE,
        "revenue_growth": evidence["growth"]["revenue"],
        "net_income_growth": evidence["growth"]["net_income"],
    }
    rows = [{
        "ticker": TICKER,
        "fiscal_end": FISCAL_END,
        "available_date": AVAILABLE_DATE,
        "metric": metric,
        "value": value,
        "taxonomy": "ifrs-full",
        "concept": f"himx_ifrs_annual_m9_ttm:{metric}",
        "form": "20-F+6-K_DERIVED_TTM",
        "accession": SOURCE_DOCUMENTS["q3_2021_6k"]["accession"],
        "fetched_at": FETCHED_AT,
    } for metric, value in values.items()]
    facts = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    if len(facts) != 4 or set(facts["metric"]) != TARGET_METRICS:
        raise RuntimeError("HIMX recovery must contain the four-field TTM bundle")
    return facts


def _audit_rows(path: Path, expected_sha256: str) -> pd.DataFrame:
    actual_sha = _sha256(path)
    if actual_sha != expected_sha256:
        raise RuntimeError(f"HIMX audit binding changed: {actual_sha}")
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
            raise RuntimeError("HIMX remains in the current financial priorities")
        return {
            "path": str(path), "sha256": expected_sha256,
            "remaining_observation_count": 0, "status": "RECOVERED",
        }
    scenarios = {scenario for scenario, _age in AUDIT_OBSERVATIONS}
    if len(rows) != 2 or set(rows["scenario"]) != scenarios:
        raise RuntimeError("HIMX baseline audit scenarios changed")
    expected = {
        "missing_signal_count": 1,
        "no_raw_pit_financial_facts_signal_count": 1,
        "insufficient_growth_history_signal_count": 0,
        "stale_growth_snapshot_signal_count": 0,
    }
    for column, value in expected.items():
        if not rows[column].eq(value).all():
            raise RuntimeError(f"HIMX baseline {column} changed")
    if set(rows["first_missing_signal_date"]) != {SIGNAL_DATE}:
        raise RuntimeError("HIMX baseline signal date changed")
    return {
        "path": str(path), "sha256": expected_sha256,
        "missing_observation_count": 2,
        "classification": "no_raw_pit_financial_facts",
    }


def recovered_observations() -> pd.DataFrame:
    evidence = ttm_evidence()
    derived = evidence["derived"]
    return pd.DataFrame([{
        "scenario": scenario,
        "ticker": TICKER,
        "signal_date": SIGNAL_DATE,
        "maximum_age_days": age,
        "resolved": True,
        "decision": "recovered_exact_ifrs_annual_m9_ttm_growth_bundle",
        "revenue_ttm_usd": (
            derived["current_ttm_usd_thousands"]["revenue"] * SOURCE_SCALE
        ),
        "net_income_ttm_usd": (
            derived["current_ttm_usd_thousands"]["net_income"] * SOURCE_SCALE
        ),
        "revenue_growth": derived["growth"]["revenue"],
        "net_income_growth": derived["growth"]["net_income"],
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
            "The four-field bundle uses audited annual and exact nine-month "
            "consolidated amounts public by 2021-11-04. The interim exhibits "
            "explicitly omit a complete set of IFRS interim disclosures; no "
            "missing disclosure is inferred. Stockholder-attributable profit, "
            "non-IFRS reconciliations, adjusted metrics, and later FY2021 "
            "results are excluded. Formal financial files remain unchanged."
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
    """Copy-on-write overlay of only HIMX's 2021Q3 direct TTM bundle."""
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
        raise RuntimeError("HIMX integration requires the quarterly schema")
    if len(incoming) != 4 or not _target_mask(incoming).all():
        raise RuntimeError("HIMX supplement scope is not the four-field TTM bundle")
    target = _target_mask(base)
    replaced = base.loc[target].copy()
    untouched = base.loc[~target].copy()
    merged = pd.concat([untouched, incoming], ignore_index=True).sort_values(
        ["ticker", "fiscal_end", "metric", "available_date"]
    ).reset_index(drop=True)
    if len(merged) != len(untouched) + 4:
        raise RuntimeError("HIMX overlay changed rows outside the bounded key space")

    output_dir.mkdir(parents=True, exist_ok=True)
    annual_path = output_dir / "annual.csv"
    quarterly_path = output_dir / "quarterly.csv"
    shutil.copyfile(inputs[0], annual_path)
    merged.to_csv(quarterly_path, index=False)
    if {path: _sha256(path) for path in inputs} != bound:
        raise RuntimeError("HIMX integration source changed while being read")
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
