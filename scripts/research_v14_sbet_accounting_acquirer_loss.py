#!/usr/bin/env python3
"""Recover SBET's audited accounting-acquirer annual loss at merger close."""

from __future__ import annotations

import argparse
import hashlib
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


TICKER = "SBET"
HISTORICAL_CIK = 1_025_561
CURRENT_CIK_EXCLUDED = 1_981_535
CURRENCY = "USD"
FISCAL_END = "2020-12-31"
AVAILABLE_DATE = "2021-07-27"
SIGNAL_DATE = "2021-08-31"
FETCHED_AT = "2026-08-29"
OUTPUT_DIR = Path("output/research_only/v14/sbet_accounting_acquirer_loss")
SEC_HEADERS = {"User-Agent": "quant-stocks-research contact@example.com"}

BASE_CANDIDATE_DIR = Path(
    "output/research_only/v14/"
    "candidate_fundamentals_v14_checkpoint_20260829_rcon_h1_ttm_loss"
)
BASELINE_AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260829_rcon_h1_ttm_loss_recovered_financial_priorities.csv"
)
EXPECTED_BASELINE_AUDIT_SHA256 = (
    "e7172ee182c3e88df71978dd1b4450f383d3d2ac2ecd8c436b6e1ebb7e4b17ef"
)
CURRENT_AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260829_sbet_accounting_acquirer_loss_recovered_financial_priorities.csv"
)
EXPECTED_CURRENT_AUDIT_SHA256 = (
    "a967120ee6b07c0f51daaf69d1ad68151400f2e2ab62c396577829af42742f73"
)

SOURCE_DOCUMENTS = {
    "merger_proxy": {
        "role": "audited_old_sharplink_actual_and_accounting_acquirer_evidence",
        "form": "6-K Exhibit 99.2",
        "filed": "2021-06-16",
        "accepted_at": "2021-06-16T09:45:17Z",
        "accession": "0001178913-21-002084",
        "document": "exhibit_99-2.htm",
        "expected_sha256": (
            "1d2d7bb1549f2edeebcea50f6fe685f453111efc17597cb66afa2d85d8b73418"
        ),
    },
    "merger_close": {
        "role": "completed_merger_control_and_sbet_identity_evidence",
        "form": "6-K Exhibit 99.1",
        "filed": AVAILABLE_DATE,
        "accepted_at": "2021-07-27T17:02:57Z",
        "accession": "0001178913-21-002392",
        "document": "exhibit_99-1.htm",
        "expected_sha256": (
            "d391bb21e90372a04d8d34dc5e1ca89a27665c3a08cf3fff9b3724d33a72b915"
        ),
    },
}
SOURCE_TEXT_CHECKS = {
    "merger_proxy": (
        "Consolidated Financial Statements December 31, 2020 and 2019 SharpLink, Inc. and Subsidiary",
        "in conformity with accounting principles generally accepted in the United States of America",
        "Eliminates equity of the legal acquiree (accounting acquirer)",
        "SharpLink shareholders will own approximately 86%",
        "presented for illustrative purposes only and are not necessarily indicative",
    ),
    "merger_close": (
        "Mer Telemanagement Solutions Completes Merger with SharpLink, Inc.",
        "Corporate Name Changed to SharpLink Gaming Ltd.",
        "today announced the closing of its previously announced plan to merge",
        "former SharpLink shareholders collectively own approximately 86%",
        "ticker SBET",
        "Company's officers were replaced by SharpLink's officers",
    ),
}
ANNUAL_STATEMENT_HEADER = (
    "SharpLink, Inc. and Subsidiary Consolidated Statements of Operations "
    "Years Ended December 31, 2020 and 2019 2020 2019"
)
ANNUAL_ROW_PATTERN = r"Net Loss \$ \(1,139,072 \) \$ \(306,153 \)"
EXPECTED_NET_LOSS_USD = -1_139_072
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
        f"https://www.sec.gov/Archives/edgar/data/{HISTORICAL_CIK}/"
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
    raise RuntimeError(f"failed to download locked SBET source: {url}") from error


def _normalize(value: object) -> str:
    return " ".join(
        str(value)
        .replace("\xa0", " ")
        .replace("\u200b", " ")
        .replace("’", "'")
        .replace("“", "")
        .replace("”", "")
        .split()
    )


def _html_text(payload: bytes) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(payload, "lxml")
    return _normalize(soup.get_text(" ", strip=True))


def validate_source_lock(sources: dict[str, dict] | None = None) -> None:
    documents = SOURCE_DOCUMENTS if sources is None else sources
    if set(documents) != set(SOURCE_DOCUMENTS):
        raise ValueError("SBET source document set changed")
    for source_id, source in documents.items():
        locked = SOURCE_DOCUMENTS[source_id]
        for field in ("form", "accepted_at", "accession", "document"):
            if source[field] != locked[field]:
                raise ValueError(
                    f"SBET source {source_id} changed locked identity field {field}"
                )
        if source["filed"] > SIGNAL_DATE:
            raise ValueError(f"SBET source {source_id} violates the PIT cutoff")
        if not re.fullmatch(r"[0-9a-f]{64}", source["expected_sha256"]):
            raise ValueError(f"SBET source {source_id} has an invalid SHA-256")
        url = _source_url(source)
        if f"/data/{HISTORICAL_CIK}/{source['accession'].replace('-', '')}/" not in url:
            raise ValueError(f"SBET source {source_id} does not lock historical CIK")
        if not url.endswith("/" + source["document"]):
            raise ValueError(f"SBET source {source_id} does not lock document")
    if AVAILABLE_DATE != max(source["filed"] for source in documents.values()):
        raise ValueError("SBET availability date is not the completed-merger filing")


def verify_source_values(raw_by_source: dict[str, bytes]) -> list[dict]:
    if set(raw_by_source) != set(SOURCE_DOCUMENTS):
        raise ValueError("SBET raw source set does not match the source lock")
    texts = {key: _html_text(payload) for key, payload in raw_by_source.items()}
    for source_id, text in texts.items():
        missing = [
            fragment
            for fragment in SOURCE_TEXT_CHECKS[source_id]
            if _normalize(fragment).casefold() not in text.casefold()
        ]
        if missing:
            raise RuntimeError(f"SBET source text changed for {source_id}: {missing}")

    proxy = texts["merger_proxy"]
    start = proxy.find(ANNUAL_STATEMENT_HEADER)
    if start < 0:
        raise RuntimeError("SBET audited annual statement header changed")
    end = proxy.find("Consolidated Statements of Stockholders' Equity", start)
    if end < 0:
        raise RuntimeError("SBET audited annual statement boundary changed")
    statement = proxy[start:end]
    matches = re.findall(ANNUAL_ROW_PATTERN, statement)
    if len(matches) != 1:
        raise RuntimeError("SBET audited Old SharpLink net-loss row changed")
    return [
        {
            "source_id": "merger_proxy",
            "row_label": "Net Loss",
            "old_sharplink_fy2020_net_loss_usd": EXPECTED_NET_LOSS_USD,
            "match_count": len(matches),
            "pro_forma_tables_excluded": True,
        },
        {
            "source_id": "merger_close",
            "completed_transaction": True,
            "post_close_ticker": TICKER,
            "old_sharplink_fully_diluted_ownership_approx_pct": 86,
        },
    ]


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
                f"SBET source SHA-256 changed for {source_id}: {actual_sha}"
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


def predecessor_evidence() -> dict:
    if EXPECTED_NET_LOSS_USD >= 0:
        raise RuntimeError("SBET predecessor loss must remain negative")
    return {
        "ticker": TICKER,
        "historical_cik": HISTORICAL_CIK,
        "current_cik_excluded": CURRENT_CIK_EXCLUDED,
        "currency": CURRENCY,
        "fiscal_end": FISCAL_END,
        "available_date": AVAILABLE_DATE,
        "signal_date": SIGNAL_DATE,
        "financial_age_days": int(
            (pd.Timestamp(SIGNAL_DATE) - pd.Timestamp(AVAILABLE_DATE)).days
        ),
        "net_income_ttm_usd": EXPECTED_NET_LOSS_USD,
        "metric_mapping": {
            "selected_metric": "Old SharpLink audited consolidated Net Loss",
            "reason": (
                "The proxy identifies Old SharpLink as the legal acquiree and "
                "accounting acquirer; at close its former shareholders held "
                "approximately 86% and its officers and directors took control."
            ),
        },
        "transaction_accounting": {
            "legal_issuer_cik": HISTORICAL_CIK,
            "accounting_acquirer": "SharpLink, Inc. and Subsidiary",
            "merger_completed": True,
            "post_close_ticker": TICKER,
            "old_sharplink_ownership_approx_pct": 86,
            "historical_actual_only": True,
            "pro_forma_excluded": True,
            "later_domesticated_cik_excluded": CURRENT_CIK_EXCLUDED,
        },
    }


def strict_quarterly_facts() -> pd.DataFrame:
    evidence = predecessor_evidence()
    facts = pd.DataFrame([{
        "ticker": TICKER,
        "fiscal_end": FISCAL_END,
        "available_date": AVAILABLE_DATE,
        "metric": "net_income_ttm",
        "value": evidence["net_income_ttm_usd"],
        "taxonomy": "us-gaap",
        "concept": "old_sharplink_accounting_acquirer:NetLoss:audited_actual",
        "form": "6-K_PROXY_AUDITED_HISTORICAL_ACTUAL_AT_MERGER_CLOSE",
        "accession": "+".join(
            source["accession"] for source in SOURCE_DOCUMENTS.values()
        ),
        "fetched_at": FETCHED_AT,
    }], columns=OUTPUT_COLUMNS)
    if len(facts) != 1 or set(facts["metric"]) != TARGET_METRICS:
        raise RuntimeError("SBET recovery must contain only exact audited loss")
    return facts


def _audit_rows(path: Path, expected_sha256: str) -> pd.DataFrame:
    actual_sha = _sha256(path)
    if actual_sha != expected_sha256:
        raise RuntimeError(f"SBET audit binding changed: {actual_sha}")
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
            raise RuntimeError("SBET remains in the current financial priorities")
        return {
            "path": str(path),
            "sha256": expected_sha256,
            "remaining_observation_count": 0,
            "status": "RECOVERED",
        }
    scenarios = {scenario for scenario, _age in AUDIT_OBSERVATIONS}
    if len(rows) != 3 or set(rows["scenario"]) != scenarios:
        raise RuntimeError("SBET baseline audit scenarios changed")
    expected = {
        "missing_signal_count": 1,
        "no_raw_pit_financial_facts_signal_count": 1,
        "insufficient_growth_history_signal_count": 0,
        "stale_growth_snapshot_signal_count": 0,
    }
    for column, value in expected.items():
        if not rows[column].eq(value).all():
            raise RuntimeError(f"SBET baseline {column} changed")
    if set(rows["first_missing_signal_date"]) != {SIGNAL_DATE}:
        raise RuntimeError("SBET baseline signal date changed")
    return {
        "path": str(path),
        "sha256": expected_sha256,
        "missing_observation_count": 3,
        "classification": "historical_cik_accounting_acquirer_actual_omission",
    }


def recovered_observations() -> pd.DataFrame:
    evidence = predecessor_evidence()
    return pd.DataFrame([{
        "scenario": scenario,
        "ticker": TICKER,
        "signal_date": SIGNAL_DATE,
        "maximum_age_days": age,
        "resolved": True,
        "decision": "known_nonpositive_accounting_acquirer_profit",
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
    evidence = predecessor_evidence()
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
    evidence_path = output_dir / "accounting_acquirer_evidence.json"
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
        "historical_cik": HISTORICAL_CIK,
        "current_cik_excluded": CURRENT_CIK_EXCLUDED,
        "currency": CURRENCY,
        "accepted_fact_count": len(facts),
        "resolved_audit_observation_count": len(observations),
        "source_documents": sources,
        "source_value_verification": source_verification,
        "metric_mapping": evidence["metric_mapping"],
        "transaction_accounting": evidence["transaction_accounting"],
        "audit_binding": {
            "baseline": baseline,
            "current": current,
            "recovered_observation_count": len(observations),
        },
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path), "sha256": _sha256(facts_path),
            },
            "accounting_acquirer_evidence": {
                "path": str(evidence_path), "sha256": _sha256(evidence_path),
            },
            "recovered_observations": {
                "path": str(observations_path),
                "sha256": _sha256(observations_path),
                "row_count": len(observations),
            },
        },
        "guardrail": (
            "Uses only Old SharpLink's audited FY2020 historical actual and the "
            "pre-signal proxy/closing evidence that it was the accounting acquirer, "
            "its owners held about 86%, its leadership took control, and SBET began "
            "trading after close. Separate pro-forma tables, legacy MTS earnings, "
            "later CIK 1981535 facts, adjusted measures, and growth are excluded."
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
    """Copy-on-write overlay of only SBET's audited predecessor loss."""
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
        raise RuntimeError("SBET integration requires the quarterly schema")
    if len(incoming) != 1 or not _target_mask(incoming).all():
        raise RuntimeError("SBET supplement scope is not the exact audited loss")
    target = _target_mask(base)
    replaced = base.loc[target].copy()
    untouched = base.loc[~target].copy()
    merged = pd.concat([untouched, incoming], ignore_index=True).sort_values(
        ["ticker", "fiscal_end", "metric", "available_date"]
    ).reset_index(drop=True)
    if len(merged) != len(untouched) + 1:
        raise RuntimeError("SBET overlay changed rows outside the bounded key space")

    output_dir.mkdir(parents=True, exist_ok=True)
    annual_path = output_dir / "annual.csv"
    quarterly_path = output_dir / "quarterly.csv"
    shutil.copyfile(inputs[0], annual_path)
    merged.to_csv(quarterly_path, index=False)
    if {path: _sha256(path) for path in inputs} != bound:
        raise RuntimeError("SBET integration source changed while being read")
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
