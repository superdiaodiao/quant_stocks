#!/usr/bin/env python3
"""Recover SOHU's exact restated GAAP quarters before the 2021-08-31 signal."""

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


TICKER = "SOHU"
CIK = 1_734_107
CURRENCY = "USD"
SOURCE_SCALE = 1_000
SIGNAL_DATE = "2021-08-31"
FETCHED_AT = "2026-08-29"
OUTPUT_DIR = Path("output/research_only/v14/sohu_restated_quarters")
SEC_HEADERS = {"User-Agent": "quant-stocks-research contact@example.com"}

BASE_CANDIDATE_DIR = Path(
    "output/research_only/v14/"
    "candidate_fundamentals_v14_checkpoint_20260829_sbet_accounting_acquirer_loss"
)
BASELINE_AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260829_sbet_accounting_acquirer_loss_recovered_financial_priorities.csv"
)
EXPECTED_BASELINE_AUDIT_SHA256 = (
    "a967120ee6b07c0f51daaf69d1ad68151400f2e2ab62c396577829af42742f73"
)
CURRENT_AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260829_sohu_restated_quarters_recovered_financial_priorities.csv"
)
EXPECTED_CURRENT_AUDIT_SHA256 = (
    "da109905c70d36898fe8f2689275fc272e69d69a0451f9866de59e6eb8beedca"
)

SOURCE_DOCUMENTS = {
    "2020_q3": {
        "role": "restated_2020q3_2020q2_and_2019q3_three_month_actuals",
        "form": "6-K Exhibit 99.1",
        "filed": "2020-11-16",
        "accepted_at": "2020-11-16T11:12:52Z",
        "accession": "0001193125-20-293704",
        "document": "d73259dex991.htm",
        "expected_sha256": (
            "3977602aa2a959b9ef4a26a4744c5bed37dd6b73157770401fc6d7378a0938a9"
        ),
    },
    "2020_q4": {
        "role": "restated_2020q4_2020q3_2019q4_and_audited_annual_identities",
        "form": "6-K Exhibit 99.1",
        "filed": "2021-02-04",
        "accepted_at": "2021-02-04T11:05:28Z",
        "accession": "0001193125-21-027947",
        "document": "d14648dex991.htm",
        "expected_sha256": (
            "8c7aff1250708967527ac534be82962ce7b350742089fcd303f9a957c1c8f753"
        ),
    },
    "2021_q1": {
        "role": "restated_2021q1_2020q4_and_2020q1_three_month_actuals",
        "form": "6-K Exhibit 99.1",
        "filed": "2021-05-14",
        "accepted_at": "2021-05-14T11:24:59Z",
        "accession": "0001193125-21-161263",
        "document": "d496609dex991.htm",
        "expected_sha256": (
            "f975d0da638468a3c5ef49e01ec54697e194beb6456b220d33e81a677155e2e3"
        ),
    },
    "2021_q2": {
        "role": "restated_2021q2_2021q1_and_2020q2_three_month_actuals",
        "form": "6-K Exhibit 99.1",
        "filed": "2021-08-09",
        "accepted_at": "2021-08-09T11:51:04Z",
        "accession": "0001193125-21-239888",
        "document": "d213594dex991.htm",
        "expected_sha256": (
            "11c6e3aa6deaeb45afe7e61cce5b4e825a4487c3cd328ff06fd99bc577b13e8c"
        ),
    },
}

CONTINUITY_TEXT_CHECKS = (
    "results of operations for Sogou have been excluded",
    "presented in separate line items as discontinued operations",
    "Retrospective adjustments to the historical statements have been made",
    "consistent basis of comparison",
)
SOURCE_ROW_CHECKS = {
    "2020_q3": (
        "Total revenues 157,894 159,961 167,499",
        "Net loss attributable to Sohu.com Limited (29,564 ) (79,930 ) (22,919 )",
    ),
    "2020_q4": (
        "Total revenues 253,235 157,894 188,705 749,890 673,803",
        "Net income/(loss) attributable to Sohu.com Limited 43,488 (29,564 ) "
        "(17,095 ) (86,112 ) (149,336 )",
    ),
    "2021_q1": (
        "Total revenues 222,093 253,235 178,800",
        "Net income/(loss) attributable to Sohu.com Limited 49,196 43,488 "
        "(20,106 )",
    ),
    "2021_q2": (
        "Total revenues 204,402 222,093 159,961",
        "Net income/(loss) attributable to Sohu.com Limited 40,739 49,196 "
        "(79,930 )",
        "expect the completion of the transaction will be during the second half of 2021",
    ),
}

# Exact three-month GAAP values from the issuer statements, scaled to USD.
# Net income is the parent-attributable line, which exactly sums to the same
# FY2020/FY2019 annual series already present in the candidate.
EXPECTED_QUARTERS_USD_THOUSANDS = {
    "2019-09-30": (167_499, -22_919),
    "2019-12-31": (188_705, -17_095),
    "2020-03-31": (178_800, -20_106),
    "2020-06-30": (159_961, -79_930),
    "2020-09-30": (157_894, -29_564),
    "2020-12-31": (253_235, 43_488),
    "2021-03-31": (222_093, 49_196),
    "2021-06-30": (204_402, 40_739),
}
SOURCE_FOR_QUARTER = {
    "2019-09-30": "2020_q3",
    "2019-12-31": "2020_q4",
    "2020-03-31": "2021_q1",
    "2020-06-30": "2020_q3",
    "2020-09-30": "2020_q3",
    "2020-12-31": "2020_q4",
    "2021-03-31": "2021_q1",
    "2021-06-30": "2021_q2",
}
AVAILABLE_DATES = {
    fiscal_end: SOURCE_DOCUMENTS[source_id]["filed"]
    for fiscal_end, source_id in SOURCE_FOR_QUARTER.items()
}
EXPECTED_TTM_USD = {
    "prior": {"revenue": 694_965_000, "net_income": -140_050_000},
    "current": {"revenue": 837_624_000, "net_income": 103_859_000},
}
EXPECTED_GROWTH = {
    "revenue": 0.20527508579568755,
    "net_income": 1.741585148161371,
}
AUDITED_ANNUAL_IDENTITY_USD = {
    "2019": {"revenue": 673_803_000, "net_income": -149_336_000},
    "2020": {"revenue": 749_890_000, "net_income": -86_112_000},
}
TARGET_FISCAL_ENDS = frozenset(EXPECTED_QUARTERS_USD_THOUSANDS)
TARGET_METRICS = frozenset({"revenue", "net_income"})
AUDIT_OBSERVATIONS = (("liq2000000-age150-growth", 150),)


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
    raise RuntimeError(f"failed to download locked SOHU source: {url}") from error


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
        raise ValueError("SOHU source document set changed")
    for source_id, source in documents.items():
        locked = SOURCE_DOCUMENTS[source_id]
        for field in ("form", "filed", "accepted_at", "accession", "document"):
            if source[field] != locked[field]:
                raise ValueError(
                    f"SOHU source {source_id} changed locked identity field {field}"
                )
        if source["filed"] > SIGNAL_DATE:
            raise ValueError(f"SOHU source {source_id} violates the PIT cutoff")
        if not re.fullmatch(r"[0-9a-f]{64}", source["expected_sha256"]):
            raise ValueError(f"SOHU source {source_id} has an invalid SHA-256")
        if not _source_url(source).endswith("/" + source["document"]):
            raise ValueError(f"SOHU source {source_id} does not lock document")
    if max(source["filed"] for source in documents.values()) != "2021-08-09":
        raise ValueError("SOHU latest source availability changed")


def verify_source_values(raw_by_source: dict[str, bytes]) -> list[dict]:
    if set(raw_by_source) != set(SOURCE_DOCUMENTS):
        raise ValueError("SOHU raw source set does not match the source lock")
    checks = []
    for source_id, payload in raw_by_source.items():
        text = _html_text(payload)
        expected = (*CONTINUITY_TEXT_CHECKS, *SOURCE_ROW_CHECKS[source_id])
        missing = [
            fragment
            for fragment in expected
            if _normalize(fragment).casefold() not in text.casefold()
        ]
        if missing:
            raise RuntimeError(f"SOHU statement rows changed for {source_id}: {missing}")
        checks.append({
            "source_id": source_id,
            "continuing_discontinued_restatement_verified": True,
            "exact_revenue_row_verified": True,
            "exact_parent_attributable_net_income_row_verified": True,
        })
    return checks


def prepare_verified_sources(output_dir: Path) -> tuple[dict, list[dict]]:
    validate_source_lock()
    provenance, raw_by_source = {}, {}
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
                f"SOHU source SHA-256 changed for {source_id}: {actual_sha}"
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


def ttm_evidence() -> dict:
    quarters = {
        fiscal_end: {
            "revenue": values[0] * SOURCE_SCALE,
            "net_income": values[1] * SOURCE_SCALE,
        }
        for fiscal_end, values in EXPECTED_QUARTERS_USD_THOUSANDS.items()
    }
    ends = list(quarters)
    prior_ends, current_ends = ends[:4], ends[4:]
    derived = {}
    for label, window in (("prior", prior_ends), ("current", current_ends)):
        derived[label] = {
            metric: sum(quarters[end][metric] for end in window)
            for metric in ("revenue", "net_income")
        }
        if derived[label] != EXPECTED_TTM_USD[label]:
            raise RuntimeError(f"SOHU {label} TTM identity changed")
    growth = {
        metric: (derived["current"][metric] - derived["prior"][metric])
        / abs(derived["prior"][metric])
        for metric in ("revenue", "net_income")
    }
    for metric, expected in EXPECTED_GROWTH.items():
        if abs(growth[metric] - expected) > 1e-12:
            raise RuntimeError(f"SOHU {metric} growth changed")

    calculated_2020 = {
        metric: sum(
            quarters[end][metric]
            for end in ends
            if end.startswith("2020-")
        )
        for metric in ("revenue", "net_income")
    }
    if calculated_2020 != AUDITED_ANNUAL_IDENTITY_USD["2020"]:
        raise RuntimeError("SOHU 2020 quarterly sum does not match audited annual identity")
    return {
        "ticker": TICKER,
        "currency": CURRENCY,
        "signal_date": SIGNAL_DATE,
        "quarter_window": ends,
        "quarterly_usd": quarters,
        "prior_ttm_usd": derived["prior"],
        "current_ttm_usd": derived["current"],
        "growth": growth,
        # Both annual values are explicit in the locked 2020Q4 exhibit. The
        # eight-quarter window contains all four 2020 quarters, so FY2020 also
        # has an independent sum identity; only Q3/Q4 are needed from 2019.
        "audited_annual_identity_usd": AUDITED_ANNUAL_IDENTITY_USD,
        "calculated_2020_annual_identity_usd": calculated_2020,
        "growth_available_date": "2021-08-09",
        "financial_age_days": int(
            (pd.Timestamp(SIGNAL_DATE) - pd.Timestamp("2021-08-09")).days
        ),
        "basis": {
            "gaap": True,
            "parent_attributable_net_income": True,
            "sogou_presented_as_discontinued_operations": True,
            "retrospective_comparators": True,
            "non_gaap_excluded": True,
            "post_signal_sogou_sale_completion_and_gain_excluded": True,
        },
    }


def strict_quarterly_facts() -> pd.DataFrame:
    rows = []
    for fiscal_end, (revenue, net_income) in EXPECTED_QUARTERS_USD_THOUSANDS.items():
        source_id = SOURCE_FOR_QUARTER[fiscal_end]
        source = SOURCE_DOCUMENTS[source_id]
        for metric, value, concept in (
            ("revenue", revenue * SOURCE_SCALE, "issuer_statement:TotalRevenues"),
            (
                "net_income",
                net_income * SOURCE_SCALE,
                "issuer_statement:NetIncomeLossAttributableToSohuComLimited",
            ),
        ):
            rows.append({
                "ticker": TICKER,
                "fiscal_end": fiscal_end,
                "available_date": AVAILABLE_DATES[fiscal_end],
                "metric": metric,
                "value": value,
                "taxonomy": "us-gaap",
                "concept": concept,
                "form": "6-K:EX-99.1:RESTATED_THREE_MONTH_GAAP",
                "accession": source["accession"],
                "fetched_at": FETCHED_AT,
            })
    facts = pd.DataFrame(rows, columns=OUTPUT_COLUMNS).sort_values(
        ["fiscal_end", "metric"]
    ).reset_index(drop=True)
    if len(facts) != 16 or set(facts["metric"]) != TARGET_METRICS:
        raise RuntimeError("SOHU recovery must contain the exact 16 quarterly facts")
    return facts


def _audit_rows(path: Path, expected_sha256: str) -> pd.DataFrame:
    actual_sha = _sha256(path)
    if actual_sha != expected_sha256:
        raise RuntimeError(f"SOHU audit binding changed: {actual_sha}")
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
            raise RuntimeError("SOHU remains in the current financial priorities")
        return {
            "path": str(path),
            "sha256": expected_sha256,
            "remaining_observation_count": 0,
            "status": "RECOVERED",
        }
    if len(rows) != 1 or set(rows["scenario"]) != {
        scenario for scenario, _age in AUDIT_OBSERVATIONS
    }:
        raise RuntimeError("SOHU baseline audit scenario changed")
    expected = {
        "missing_signal_count": 1,
        "no_raw_pit_financial_facts_signal_count": 1,
        "insufficient_growth_history_signal_count": 0,
        "stale_growth_snapshot_signal_count": 0,
    }
    for column, value in expected.items():
        if not rows[column].eq(value).all():
            raise RuntimeError(f"SOHU baseline {column} changed")
    if set(rows["first_missing_signal_date"]) != {SIGNAL_DATE}:
        raise RuntimeError("SOHU baseline signal date changed")
    return {
        "path": str(path),
        "sha256": expected_sha256,
        "missing_observation_count": 1,
        "classification": "foreign_quarterly_source_omission",
    }


def recovered_observations() -> pd.DataFrame:
    evidence = ttm_evidence()
    return pd.DataFrame([{
        "scenario": scenario,
        "ticker": TICKER,
        "signal_date": SIGNAL_DATE,
        "maximum_age_days": age,
        "resolved": True,
        "decision": "complete_positive_gaap_growth_bundle",
        "revenue_ttm_usd": evidence["current_ttm_usd"]["revenue"],
        "net_income_ttm_usd": evidence["current_ttm_usd"]["net_income"],
        "revenue_growth": evidence["growth"]["revenue"],
        "net_income_growth": evidence["growth"]["net_income"],
        "financial_age_days": evidence["financial_age_days"],
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
            "remaining_observation_count": 1,
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
        "accepted_quarter_count": len(EXPECTED_QUARTERS_USD_THOUSANDS),
        "accepted_fact_count": len(facts),
        "resolved_audit_observation_count": len(observations),
        "source_documents": sources,
        "source_value_verification": source_verification,
        "ttm_evidence": evidence,
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
            "Uses only hash-locked pre-signal issuer 6-K exhibits and exact "
            "three-month GAAP values after Sogou was retrospectively presented "
            "as discontinued operations. Parent-attributable net income is used "
            "because it sums exactly to the candidate's audited FY2020/FY2019 "
            "series. Consolidated total income, continuing-only highlights, "
            "non-GAAP measures, the later Sogou sale completion/gain, estimates, "
            "and post-signal filings are excluded."
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
        & fiscal_end.isin(TARGET_FISCAL_ENDS)
        & frame["metric"].isin(TARGET_METRICS)
    )


def integrate_candidate(
    *, base_dir: Path, supplement_dir: Path = OUTPUT_DIR, output_dir: Path
) -> dict:
    """Copy-on-write overlay of only SOHU's eight exact GAAP quarters."""
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
        raise RuntimeError("SOHU integration requires the quarterly schema")
    if len(incoming) != 16 or not _target_mask(incoming).all():
        raise RuntimeError("SOHU supplement scope is not the exact eight-quarter bundle")
    if set(incoming["fiscal_end"]) != TARGET_FISCAL_ENDS:
        raise RuntimeError("SOHU supplement fiscal window changed")
    target = _target_mask(base)
    replaced = base.loc[target].copy()
    untouched = base.loc[~target].copy()
    merged = pd.concat([untouched, incoming], ignore_index=True).sort_values(
        ["ticker", "fiscal_end", "metric", "available_date"]
    ).reset_index(drop=True)
    if len(merged) != len(untouched) + 16:
        raise RuntimeError("SOHU overlay changed rows outside the bounded key space")

    output_dir.mkdir(parents=True, exist_ok=True)
    annual_path = output_dir / "annual.csv"
    quarterly_path = output_dir / "quarterly.csv"
    shutil.copyfile(inputs[0], annual_path)
    merged.to_csv(quarterly_path, index=False)
    if {path: _sha256(path) for path in inputs} != bound:
        raise RuntimeError("SOHU integration source changed while being read")
    report = {
        "schema_version": 1,
        "research_only": True,
        "formal_financials_modified": False,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "overlay_ticker": TICKER,
        "overlay_fiscal_ends": sorted(TARGET_FISCAL_ENDS),
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
