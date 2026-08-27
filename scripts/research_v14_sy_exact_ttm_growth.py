#!/usr/bin/env python3
"""Build source-locked SY exact-TTM growth facts for two PIT signals."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from urllib.request import Request, urlopen
import warnings

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
import pandas as pd

from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.io.fundamentals_update import OUTPUT_COLUMNS


OUTPUT_DIR = Path("output/research_only/v14/sy_exact_ttm_growth")
AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260824_hone_sibn_knsa_hcat_wprt_vff_final.json"
)
TICKER = "SY"
CIK = 1_758_530
CURRENCY = "RMB"
SOURCE_SCALE = 1_000
ACCOUNTING_STANDARD = "US-GAAP"
FETCHED_AT = "2026-08-27"
SEC_HEADERS = {
    "User-Agent": "quant_stocks-research/1.0 contact@example.com"
}

SOURCE_DOCUMENTS = {
    "fy2019_20f": {
        "role": "audited_annual_operands",
        "form": "20-F",
        "filed": "2020-04-27",
        "accession": "0001104659-20-051203",
        "document": "a20-1178_120f.htm",
        "local_path": "sources/a20-1178_120f.htm",
        "expected_sha256": (
            "5b765322954ca2033035be4cfe04d23e063819702db194ae41585c5defef8108"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1758530/"
            "000110465920051203/a20-1178_120f.htm"
        ),
    },
    "q1_2019_6k": {
        "role": "q1_comparative_operands",
        "form": "6-K/EX-99.1",
        "filed": "2019-05-30",
        "accession": "0001104659-19-032746",
        "document": "a19-10672_1ex99d1.htm",
        "local_path": "sources/a19-10672_1ex99d1.htm",
        "expected_sha256": (
            "11cf87f74994ff4b08c6e70e9a320f4d8463fe4463146a6496be344f3b78023b"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1758530/"
            "000110465919032746/a19-10672_1ex99d1.htm"
        ),
    },
    "q1_2020_6k": {
        "role": "q1_current_operands",
        "form": "6-K/EX-99.1",
        "filed": "2020-05-18",
        "accession": "0001104659-20-062991",
        "document": "a20-20059_1ex99d1.htm",
        "local_path": "sources/a20-20059_1ex99d1.htm",
        "expected_sha256": (
            "6c537806c5814f4d5303680117c814dc0742a06772e664f0af3f48ad4460e75a"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1758530/"
            "000110465920062991/a20-20059_1ex99d1.htm"
        ),
    },
    "q3_2019_6k": {
        "role": "nine_month_comparative_operands",
        "form": "6-K/EX-99.1",
        "filed": "2019-12-05",
        "accession": "0001104659-19-070083",
        "document": "a19-24570_1ex99d1.htm",
        "local_path": "sources/a19-24570_1ex99d1.htm",
        "expected_sha256": (
            "266a04498440eed85ed06a407eadb4a9018513580acd1176cb8ad1ed3f4408d5"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1758530/"
            "000110465919070083/a19-24570_1ex99d1.htm"
        ),
    },
    "q3_2020_6k": {
        "role": "nine_month_current_operands",
        "form": "6-K/EX-99.1",
        "filed": "2020-11-25",
        "accession": "0001104659-20-129215",
        "document": "a20-37117_1ex99d1.htm",
        "local_path": "sources/a20-37117_1ex99d1.htm",
        "expected_sha256": (
            "d60bfbc6fe94704ad86799e0d888b7e07357c99f2e9a0fef2c7dc8f00d6e3563"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1758530/"
            "000110465920129215/a20-37117_1ex99d1.htm"
        ),
    },
}

SOURCE_ROW_CHECKS = {
    "fy2019_20f": (
        {
            "metric": "revenue",
            "line_item": "Total revenues",
            "periods": (
                "fy2016", "fy2017", "fy2018", "fy2019",
                "fy2019_usd_convenience",
            ),
            "expected_values": (49_090, 259_305, 617_226, 1_151_637, 165_422),
        },
        {
            "metric": "net_income",
            "line_item": "Net (loss)/income",
            "periods": (
                "fy2016", "fy2017", "fy2018", "fy2019",
                "fy2019_usd_convenience",
            ),
            "expected_values": (-81_036, 17_202, 55_083, 176_724, 25_383),
        },
    ),
    "q1_2019_6k": (
        {
            "metric": "revenue",
            "line_item": "Total revenues",
            "periods": (
                "q1_2017", "q1_2018", "q1_2019",
                "q1_2019_usd_convenience",
            ),
            "expected_values": (113_700, 183_013, 206_053, 30_703),
        },
        {
            "metric": "net_income",
            "line_item": "Net income",
            "periods": (
                "q1_2017", "q1_2018", "q1_2019",
                "q1_2019_usd_convenience",
            ),
            "expected_values": (30_616, 40_813, 45_905, 6_840),
        },
    ),
    "q1_2020_6k": (
        {
            "metric": "revenue",
            "line_item": "Total revenues",
            "periods": (
                "q1_2019", "q4_2019", "q1_2020",
                "q1_2020_usd_convenience",
            ),
            "expected_values": (206_053, 358_174, 182_554, 25_782),
        },
        {
            "metric": "net_income",
            "line_item": "Net income/(loss)",
            "periods": (
                "q1_2019", "q4_2019", "q1_2020",
                "q1_2020_usd_convenience",
            ),
            "expected_values": (45_905, 69_945, -35_883, -5_066),
        },
    ),
    "q3_2019_6k": (
        {
            "metric": "revenue",
            "line_item": "Total revenues",
            "periods": (
                "q3_2018", "q3_2019", "q3_2019_usd_convenience",
                "m9_2018", "m9_2019", "m9_2019_usd_convenience",
            ),
            "expected_values": (
                168_357, 302_425, 42_311, 434_213, 793_463, 111_010,
            ),
        },
        {
            "metric": "net_income",
            "line_item": "Net (loss)/income",
            "periods": (
                "q3_2018", "q3_2019", "q3_2019_usd_convenience",
                "m9_2018", "m9_2019", "m9_2019_usd_convenience",
            ),
            "expected_values": (
                -25_216, 31_600, 4_419, 14_270, 106_779, 14_940,
            ),
        },
    ),
    "q3_2020_6k": (
        {
            "metric": "revenue",
            "line_item": "Total revenues",
            "periods": (
                "q3_2019", "q3_2020", "q3_2020_usd_convenience",
                "m9_2019", "m9_2020", "m9_2020_usd_convenience",
            ),
            "expected_values": (
                302_425, 359_579, 52_960, 793_463, 870_353, 128_189,
            ),
        },
        {
            "metric": "net_income",
            "line_item": "Net income/(loss)",
            "periods": (
                "q3_2019", "q3_2020", "q3_2020_usd_convenience",
                "m9_2019", "m9_2020", "m9_2020_usd_convenience",
            ),
            "expected_values": (
                31_600, 903, 132, 106_779, -32_840, -4_837,
            ),
        },
    ),
}

SOURCE_TEXT_CHECKS = {
    "fy2019_20f": (
        "SO-YOUNG INTERNATIONAL INC.",
        "For the Year Ended December 31",
        "RMB",
    ),
    "q1_2019_6k": (
        "So-Young Reports First Quarter 2019 Unaudited Financial Results",
        "For the Three Months Ended",
    ),
    "q1_2020_6k": (
        "So-Young Reports First Quarter 2020 Unaudited Financial Results",
        "For the Three Months Ended",
    ),
    "q3_2019_6k": (
        "So-Young Reports Third Quarter 2019 Unaudited Financial Results",
        "For the Nine Months Ended",
    ),
    "q3_2020_6k": (
        "So-Young Reports Third Quarter 2020 Unaudited Financial Results",
        "For the Nine Months Ended",
    ),
}

BUNDLES = {
    "q1_2020": {
        "fiscal_end": "2020-03-31",
        "available_date": "2020-05-18",
        "period_prefix": "q1",
        "prior_partial": "q1_2018",
        "middle_partial": "q1_2019",
        "current_partial": "q1_2020",
        "sources": ("fy2019_20f", "q1_2019_6k", "q1_2020_6k"),
    },
    "m9_2020": {
        "fiscal_end": "2020-09-30",
        "available_date": "2020-11-25",
        "period_prefix": "m9",
        "prior_partial": "m9_2018",
        "middle_partial": "m9_2019",
        "current_partial": "m9_2020",
        "sources": ("fy2019_20f", "q3_2019_6k", "q3_2020_6k"),
    },
}

AUDIT_OBSERVATIONS = tuple(
    (f"liq2000000-age{age}-growth", signal, age)
    for age in (150, 365, 550)
    for signal in ("2020-07-31", "2021-02-26")
)


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_path(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _download_source(url: str) -> bytes:
    with urlopen(Request(url, headers=SEC_HEADERS), timeout=120) as response:
        return response.read()


def _normalize_text(value: str) -> str:
    return " ".join(
        value.replace("\xa0", " ")
        .replace("–", "-")
        .replace("—", "-")
        .replace("−", "-")
        .split()
    ).casefold()


def _integer_cell(cell_text: str) -> int | None:
    text = "".join(cell_text.replace("\xa0", " ").split())
    if re.fullmatch(r"\(?-?\d[\d,]*\)?", text) is None:
        return None
    value = int(re.sub(r"[^0-9]", "", text))
    return -value if text.startswith(("(", "-")) else value


def _matching_rows(raw: bytes, line_item: str) -> list[tuple[int, ...]]:
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
    soup = BeautifulSoup(raw, "lxml")
    normalized_label = _normalize_text(line_item)
    matches = []
    for row in soup.find_all("tr"):
        cells = row.find_all(("td", "th"), recursive=False)
        labels = [_normalize_text(cell.get_text(" ", strip=True)) for cell in cells]
        first_label = next((label for label in labels if label), "")
        if first_label != normalized_label:
            continue
        values = tuple(
            value
            for cell in cells
            if (value := _integer_cell(cell.get_text(" ", strip=True)))
            is not None
        )
        if values:
            matches.append(values)
    return matches


def validate_source_lock(sources: dict[str, dict] | None = None) -> None:
    documents = SOURCE_DOCUMENTS if sources is None else sources
    if set(documents) != set(SOURCE_DOCUMENTS):
        raise ValueError("SY source set changed")
    accessions = set()
    for source_id, source in documents.items():
        if source["accession"] in accessions:
            raise ValueError("duplicate SY source accession")
        accessions.add(source["accession"])
        if source["accession"].replace("-", "") not in source["url"]:
            raise ValueError(f"SY source URL does not lock accession: {source_id}")
        if not source["url"].endswith("/" + source["document"]):
            raise ValueError(f"SY source URL does not lock document: {source_id}")
        local_path = Path(source["local_path"])
        if local_path.is_absolute() or ".." in local_path.parts:
            raise ValueError(f"unsafe SY source local_path: {source_id}")
        if re.fullmatch(r"[0-9a-f]{64}", source["expected_sha256"]) is None:
            raise ValueError(f"invalid SY expected SHA-256: {source_id}")
    for bundle_name, bundle in BUNDLES.items():
        latest_source_date = max(
            documents[source_id]["filed"] for source_id in bundle["sources"]
        )
        if latest_source_date != bundle["available_date"]:
            raise ValueError(f"SY {bundle_name} PIT availability changed")


def verify_source_evidence(raw_by_source: dict[str, bytes]) -> dict:
    if set(raw_by_source) != set(SOURCE_DOCUMENTS):
        raise ValueError("raw SY source set does not match source lock")
    operands: dict[str, dict[str, int]] = {
        "revenue": {},
        "net_income": {},
    }
    source_rows = []
    for source_id, checks in SOURCE_ROW_CHECKS.items():
        raw = raw_by_source[source_id]
        for check in checks:
            expected_values = tuple(check["expected_values"])
            matches = _matching_rows(raw, check["line_item"])
            if expected_values not in matches:
                raise RuntimeError(
                    f"SY source row changed for {source_id} "
                    f"{check['line_item']}: expected {expected_values}, "
                    f"got {matches[:5]}"
                )
            if len(check["periods"]) != len(expected_values):
                raise RuntimeError("SY period/value mapping length changed")
            metric_operands = operands[check["metric"]]
            for period, value in zip(
                check["periods"], expected_values, strict=True
            ):
                previous = metric_operands.get(period)
                if previous is not None and previous != value:
                    raise RuntimeError(
                        f"SY comparative changed for {check['metric']} "
                        f"{period}: {previous} != {value}"
                    )
                metric_operands[period] = value
            source_rows.append({
                "source_id": source_id,
                "metric": check["metric"],
                "line_item": check["line_item"],
                "periods": list(check["periods"]),
                "parsed_values": list(expected_values),
            })
    text_fragments = []
    for source_id, fragments in SOURCE_TEXT_CHECKS.items():
        soup = BeautifulSoup(raw_by_source[source_id], "lxml")
        document_text = _normalize_text(soup.get_text(" ", strip=True))
        for fragment in fragments:
            if _normalize_text(fragment) not in document_text:
                raise RuntimeError(
                    f"SY source disclosure changed for {source_id}: {fragment}"
                )
            text_fragments.append({
                "source_id": source_id,
                "fragment": fragment,
            })
    return {
        "operands_rmb_thousands": operands,
        "source_rows": source_rows,
        "text_fragments": text_fragments,
    }


def _derive_bundle(
    operands: dict[str, dict[str, int]], bundle: dict
) -> dict[str, dict[str, float]]:
    derived = {}
    for metric in ("revenue", "net_income"):
        values = operands[metric]
        prior_ttm = (
            values["fy2018"]
            - values[bundle["prior_partial"]]
            + values[bundle["middle_partial"]]
        )
        current_ttm = (
            values["fy2019"]
            - values[bundle["middle_partial"]]
            + values[bundle["current_partial"]]
        )
        growth = (current_ttm - prior_ttm) / abs(prior_ttm)
        derived[metric] = {
            "prior_ttm_rmb_thousands": prior_ttm,
            "current_ttm_rmb_thousands": current_ttm,
            "growth": growth,
        }
    return derived


def strict_quarterly_facts(
    raw_by_source: dict[str, bytes],
) -> tuple[pd.DataFrame, dict]:
    source_evidence = verify_source_evidence(raw_by_source)
    operands = source_evidence["operands_rmb_thousands"]
    records = []
    derived_bundles = {}
    for bundle_name, bundle in BUNDLES.items():
        derived = _derive_bundle(operands, bundle)
        derived_bundles[bundle_name] = derived
        composite_accession = "+".join(
            SOURCE_DOCUMENTS[source_id]["accession"]
            for source_id in bundle["sources"]
        )
        common = {
            "ticker": TICKER,
            "fiscal_end": bundle["fiscal_end"],
            "available_date": bundle["available_date"],
            "taxonomy": "us-gaap",
            "form": "20-F_PLUS_6-K_CUMULATIVE_TTM",
            "accession": composite_accession,
            "fetched_at": FETCHED_AT,
        }
        for metric in ("revenue", "net_income"):
            metric_data = derived[metric]
            concept = f"sy_exact_ttm:{metric}:RMB"
            records.extend((
                {
                    **common,
                    "metric": f"{metric}_ttm",
                    "value": float(
                        metric_data["current_ttm_rmb_thousands"]
                        * SOURCE_SCALE
                    ),
                    "concept": concept,
                },
                {
                    **common,
                    "metric": f"{metric}_growth",
                    "value": metric_data["growth"],
                    "concept": concept,
                },
            ))
    facts = pd.DataFrame(records, columns=OUTPUT_COLUMNS)
    facts = facts.sort_values(["available_date", "metric"]).reset_index(drop=True)
    return facts, {
        **source_evidence,
        "derived_bundles": derived_bundles,
        "guardrail": (
            "Only exact reported RMB GAAP rows are used. USD convenience "
            "translations and non-GAAP rows are verified as excluded. Each "
            "TTM is FY minus the prior cumulative period plus the current "
            "cumulative period."
        ),
    }


def prepare_verified_sources(
    output_dir: Path,
) -> tuple[dict[str, dict], dict[str, bytes]]:
    validate_source_lock()
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
        actual_sha = _sha256_bytes(raw)
        if actual_sha != source["expected_sha256"]:
            raise RuntimeError(
                f"SY source SHA-256 mismatch for {source_id}: {actual_sha}"
            )
        raw_by_source[source_id] = raw
        provenance[source_id] = {
            **source,
            "local_path": str(local_path),
            "actual_sha256": actual_sha,
            "bytes": len(raw),
            "downloaded": downloaded,
        }
    return provenance, raw_by_source


def resolved_audit_observations(evidence: dict) -> list[dict]:
    results = []
    by_signal = {
        "2020-07-31": "q1_2020",
        "2021-02-26": "m9_2020",
    }
    for scenario, signal_date, maximum_age_days in AUDIT_OBSERVATIONS:
        bundle_name = by_signal[signal_date]
        bundle = BUNDLES[bundle_name]
        derived = evidence["derived_bundles"][bundle_name]
        age = int(
            (
                pd.Timestamp(signal_date)
                - pd.Timestamp(bundle["available_date"])
            ).days
        )
        net_income = derived["net_income"]
        results.append({
            "scenario": scenario,
            "signal_date": signal_date,
            "maximum_age_days": maximum_age_days,
            "financial_age_days": age,
            "resolved": True,
            "decision": (
                "pass_growth_filters"
                if derived["revenue"]["growth"] >= 0.10
                and net_income["growth"] >= 0.25
                else "fail_growth_filters"
            ),
            "revenue_growth": derived["revenue"]["growth"],
            "net_income_ttm": (
                net_income["current_ttm_rmb_thousands"] * SOURCE_SCALE
            ),
            "net_income_growth": net_income["growth"],
        })
    return results


def build(
    output_dir: Path = OUTPUT_DIR,
    audit_path: Path = AUDIT_PATH,
) -> dict:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    provenance, raw_by_source = prepare_verified_sources(output_dir)
    facts, evidence = strict_quarterly_facts(raw_by_source)
    resolutions = resolved_audit_observations(evidence)

    facts_path = output_dir / "strict_quarterly_facts.csv"
    evidence_path = output_dir / "exact_ttm_evidence.json"
    resolution_path = output_dir / "resolved_observations.json"
    facts.to_csv(facts_path, index=False)
    evidence_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    resolution_path.write_text(
        json.dumps(resolutions, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    audit_path = Path(audit_path)
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "ticker": TICKER,
        "cik": CIK,
        "currency": CURRENCY,
        "source_scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
        "accepted_direct_growth_package_count": len(BUNDLES),
        "accepted_fact_count": len(facts),
        "resolved_audit_observation_count": len(resolutions),
        "sources": provenance,
        "audit_binding": {
            "path": str(audit_path),
            "sha256": _sha256_path(audit_path),
            "scenarios": sorted({item[0] for item in AUDIT_OBSERVATIONS}),
            "signals": sorted({item[1] for item in AUDIT_OBSERVATIONS}),
            "missing_observation_count": len(AUDIT_OBSERVATIONS),
        },
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path),
                "sha256": _sha256_path(facts_path),
            },
            "exact_ttm_evidence": {
                "path": str(evidence_path),
                "sha256": _sha256_path(evidence_path),
            },
            "resolved_observations": {
                "path": str(resolution_path),
                "sha256": _sha256_path(resolution_path),
            },
        },
        "guardrail": evidence["guardrail"],
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    report["manifest"] = str(manifest_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--audit-path", type=Path, default=AUDIT_PATH)
    parser.add_argument("--base-dir", type=Path)
    parser.add_argument("--candidate-output-dir", type=Path)
    args = parser.parse_args()
    if (args.base_dir is None) != (args.candidate_output_dir is None):
        parser.error("--base-dir and --candidate-output-dir must be used together")
    report = build(args.output_dir, args.audit_path)
    if args.base_dir is not None:
        report["candidate"] = integrate_candidate(
            base_dir=args.base_dir,
            supplement_dir=args.output_dir,
            output_dir=args.candidate_output_dir,
        )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
