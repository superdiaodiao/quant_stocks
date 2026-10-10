#!/usr/bin/env python3
"""Build source-locked, research-only exact-TTM loss evidence for CRON.

The Nasdaq security was Cronos Group common stock, while its contemporaneous
issuer financial statements were IFRS reports in Canadian dollars.  This
supplement preserves only the exact negative attributable TTM state that was
public before 2019-02-28.  It cannot create a quarter or growth observation.
"""

from __future__ import annotations

import argparse
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
import hashlib
import json
from pathlib import Path
import re
from typing import Iterable
from urllib.request import Request, urlopen
import warnings

import pandas as pd

from src.io.fundamentals_update import OUTPUT_COLUMNS


OUTPUT_DIR = Path("output/research_only/v14/cron_exact_ttm_loss")
TICKER = "CRON"
CIK = 1_656_472
CURRENCY = "CAD"
SOURCE_SCALE = 1_000
ACCOUNTING_STANDARD = "IFRS-IASB"
PIT_CUTOFF = "2019-02-28"
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
BASELINE_BINDING = {
    "quarterly": (
        "output/research_only/v14/"
        "candidate_fundamentals_v14_batch_afya_legn_sdgr/quarterly.csv"
    ),
    "quarterly_sha256": (
        "3332d40899309a99b8740415eaf275e35262111104ce634fc8bf2612b9fb172a"
    ),
    "audit": "output/research_only/v14/batch_afya_legn_sdgr_audit.json",
    "audit_sha256": (
        "ea94b239992e914e5efaa77e0d585949a1e3e8aa3d669ad62bd19af5a8474a5a"
    ),
    "baseline_reason": "no_raw_pit_financial_facts",
}

SOURCE_DOCUMENTS = {
    "40f_2018_04_30_fy2017_ex992": {
        "form": "40-F/EX-99.2",
        "filed": "2018-04-30",
        "accession": "0001193125-18-140678",
        "document": "d552281dex992.htm",
        "local_path": "sources/cron_2017_40f_exhibit_99-2.htm",
        "expected_sha256": (
            "c82093a2e2b6bbff52a9d479edf702250b18a5ab62ce9f2af8c3463495fd0cb5"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1656472/"
            "000119312518140678/d552281dex992.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
        "audit_status": "AUDITED",
    },
    "6k_2018_11_13_9m_ex991": {
        "form": "6-K/EX-99.1",
        "filed": "2018-11-13",
        "accession": "0001564590-18-029098",
        "document": "cron-ex991_9.htm",
        "local_path": "sources/cron_2018_9m_exhibit_99-1.htm",
        "expected_sha256": (
            "bccbd793104bbacdfbdd1a37e74de0b8b24397d57c8ef40462e4d30840a78627"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1656472/"
            "000156459018029098/cron-ex991_9.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
        "audit_status": "UNAUDITED",
    },
}
ALLOWED_SOURCE_ACCESSIONS = {
    source["accession"] for source in SOURCE_DOCUMENTS.values()
}

ACCOUNTING_POLICY_AUDIT = {
    "source_id": "6k_2018_11_13_9m_ex991",
    "change": (
        "voluntary capitalization of direct and indirect biological-asset "
        "transformation costs"
    ),
    "application": "RETROSPECTIVE",
    "profit_effect": "NO_IMPACT_CURRENT_OR_PRIOR_PERIOD_NET_INCOME_LOSS",
    "other_effect": (
        "presentation changed between production costs, cost of sales, and "
        "fair-value adjustments; gross profit and cash flow were unchanged"
    ),
}

POST_SIGNAL_EXCLUSIONS = (
    {
        "form": "40-F",
        "filed": "2019-03-26",
        "accession": "0001193125-19-085847",
        "document": "d711365d40f.htm",
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1656472/"
            "000119312519085847/d711365d40f.htm"
        ),
        "reason": "FY2018 annual filing was 26 days after the signal",
    },
    {
        "form": "10-K/A",
        "filed": "2020-03-30",
        "accession": "0001656472-20-000033",
        "reason": (
            "later U.S.-GAAP presentation/restatement cannot replace the "
            "contemporaneous IFRS-CAD state"
        ),
    },
)

OPERANDS_CAD_THOUSANDS = {
    "fy2017_profit_attributable": {
        "source_id": "40f_2018_04_30_fy2017_ex992",
        "period": "FY2017",
        "table_column": "FY2017",
        "metric": "profit_attributable",
        "line_item": "Net Income (loss) attributable to common shareholders",
        "value": 2_491,
    },
    "m9_2017_profit_attributable": {
        "source_id": "6k_2018_11_13_9m_ex991",
        "period": "9M 2017",
        "table_column": "M9_2017",
        "metric": "profit_attributable",
        "line_item": "Cronos Group",
        "value": 428,
    },
    "m9_2018_profit_attributable": {
        "source_id": "6k_2018_11_13_9m_ex991",
        "period": "9M 2018",
        "table_column": "M9_2018",
        "metric": "profit_attributable",
        "line_item": "Cronos Group",
        "value": -7_537,
    },
}

SOURCE_PARSE_SPECS = {
    "40f_2018_04_30_fy2017_ex992": {
        "identity_phrases": (
            "CRONOS GROUP INC. CONSOLIDATED FINANCIAL STATEMENTS",
            "For the Years Ended December 31, 2017 and December 31, 2016",
            "in thousands of Canadian dollars",
            "Independent Auditors’ Report",
        ),
        "row_specs": {
            "net_income": {
                "context_phrases": (
                    "2017", "2016", "Product sales", "Other comprehensive income",
                    "Weighted average number of outstanding shares",
                ),
                "columns": {"FY2017": "FY2017", "FY2016": "FY2016"},
                "row_label": "Net income (loss)",
                "occurrence": 0,
            },
            "profit_attributable": {
                "context_phrases": (
                    "Numerator", "2017", "2016", "Denominator",
                    "Net Income (loss) attributable to common shareholders",
                ),
                "columns": {"FY2017": "FY2017", "FY2016": "FY2016"},
                "row_label": (
                    "Net Income (loss) attributable to common shareholders"
                ),
                "occurrence": 0,
            },
        },
    },
    "6k_2018_11_13_9m_ex991": {
        "identity_phrases": (
            "CRONOS GROUP INC.",
            "Unaudited Condensed Interim Consolidated Financial Statements",
            "For the Three and Nine Months Ended September 30, 2018 and September 30, 2017",
            "in thousands of Canadian dollars",
            "International Financial Reporting Standard",
        ),
        "row_specs": {
            "net_income": {
                "context_phrases": (
                    "Three Months Ended September 30",
                    "Nine Months Ended September 30",
                    "2018", "2017", "Revenue", "Comprehensive income (loss)",
                ),
                "columns": {
                    "Q3_2018": "Q3 2018",
                    "Q3_2017": "Q3 2017",
                    "M9_2018": "9M 2018",
                    "M9_2017": "9M 2017",
                },
                "row_label": "Net income (loss)",
                "occurrence": 0,
            },
            "profit_attributable": {
                "context_phrases": (
                    "Three Months Ended September 30",
                    "Nine Months Ended September 30",
                    "Net income (loss) attributable to",
                    "Comprehensive income (loss) attributable to",
                ),
                "columns": {
                    "Q3_2018": "Q3 2018",
                    "Q3_2017": "Q3 2017",
                    "M9_2018": "9M 2018",
                    "M9_2017": "9M 2017",
                },
                "row_label": "Cronos Group",
                "occurrence": 0,
            },
        },
    },
}

SOURCE_VALUE_EXPECTATIONS = {
    "fy2017_net_income": {
        "source_id": "40f_2018_04_30_fy2017_ex992",
        "metric": "net_income",
        "table_column": "FY2017",
        "value": 2_491,
    },
    "fy2017_profit_attributable": {
        "source_id": "40f_2018_04_30_fy2017_ex992",
        "metric": "profit_attributable",
        "table_column": "FY2017",
        "value": 2_491,
    },
    "m9_2017_net_income": {
        "source_id": "6k_2018_11_13_9m_ex991",
        "metric": "net_income",
        "table_column": "M9_2017",
        "value": 428,
    },
    "m9_2017_profit_attributable": {
        "source_id": "6k_2018_11_13_9m_ex991",
        "metric": "profit_attributable",
        "table_column": "M9_2017",
        "value": 428,
    },
    "m9_2018_net_income": {
        "source_id": "6k_2018_11_13_9m_ex991",
        "metric": "net_income",
        "table_column": "M9_2018",
        "value": -7_598,
    },
    "m9_2018_profit_attributable": {
        "source_id": "6k_2018_11_13_9m_ex991",
        "metric": "profit_attributable",
        "table_column": "M9_2018",
        "value": -7_537,
    },
}

TTM_SPEC = {
    "fiscal_end": "2018-09-30",
    "available_date": "2018-11-13",
    "formula": "FY2017 - 9M_2017 + 9M_2018",
    "terms": (
        (1, "fy2017_profit_attributable"),
        (-1, "m9_2017_profit_attributable"),
        (1, "m9_2018_profit_attributable"),
    ),
    "expected_cad_thousands": -5_474,
    "form": "40-F_PLUS_6-K_9M_CUMULATIVE_TTM",
}

AUDIT_OBSERVATIONS = (
    ("liq10000000-age150-growth", "2019-02-28", 150),
    ("liq10000000-age365-growth", "2019-02-28", 365),
    ("liq10000000-age550-growth", "2019-02-28", 550),
    ("liq2000000-age150-growth", "2019-02-28", 150),
    ("liq2000000-age365-growth", "2019-02-28", 365),
    ("liq2000000-age550-growth", "2019-02-28", 550),
)


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _download_source(url: str) -> bytes:
    with urlopen(Request(url, headers=SEC_HEADERS), timeout=120) as response:
        return response.read()


def _normalize_text(value: str) -> str:
    return " ".join(
        value.replace("\xa0", " ").replace("−", "-").split()
    ).casefold()


def _row_numbers(row) -> list[int]:
    text = " ".join(row.stripped_strings).replace("\xa0", " ")
    text = re.sub(r"\s*,\s*", ",", text)
    tokens = re.findall(
        r"\(\s*\d[\d,]*\s*\)|(?<![\w])\d[\d,]*(?![\w])",
        text,
    )
    values = []
    for token in tokens:
        digits = re.sub(r"\D", "", token)
        if digits:
            value = int(digits)
            values.append(-value if "(" in token else value)
    return values


def _parse_source_tables(source_id: str, raw: bytes) -> dict[str, dict]:
    spec = SOURCE_PARSE_SPECS[source_id]
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
    soup = BeautifulSoup(raw, "lxml")
    document_text = _normalize_text(" ".join(soup.stripped_strings))
    if any(
        _normalize_text(phrase) not in document_text
        for phrase in spec["identity_phrases"]
    ):
        raise RuntimeError(f"CRON source identity changed for {source_id}")
    parsed = {}
    for metric, row_spec in spec["row_specs"].items():
        context = tuple(
            _normalize_text(item) for item in row_spec["context_phrases"]
        )
        normalized_label = _normalize_text(row_spec["row_label"])
        expected_count = len(row_spec["columns"])
        occurrence = int(row_spec.get("occurrence", 0))
        candidates = []
        for table in soup.find_all("table"):
            table_text = _normalize_text(" ".join(table.stripped_strings))
            if not all(item in table_text for item in context):
                continue
            matching_rows = []
            for row in table.find_all("tr"):
                labels = [
                    _normalize_text(" ".join(cell.stripped_strings))
                    for cell in row.find_all(("td", "th"))
                ]
                first_label = next((item for item in labels if item), "")
                if first_label == normalized_label:
                    matching_rows.append(row)
            if len(matching_rows) <= occurrence:
                continue
            values = _row_numbers(matching_rows[occurrence])
            if len(values) >= expected_count:
                candidates.append(dict(zip(
                    row_spec["columns"], values[-expected_count:], strict=True
                )))
        if not candidates:
            raise RuntimeError(
                f"no unambiguous CRON {metric} table for {source_id}"
            )
        canonical = json.dumps(candidates[0], sort_keys=True)
        if any(json.dumps(item, sort_keys=True) != canonical for item in candidates):
            raise RuntimeError(f"conflicting CRON {metric} tables for {source_id}")
        parsed[metric] = candidates[0]
    return parsed


def validate_source_lock(sources: dict[str, dict] | None = None) -> None:
    documents = SOURCE_DOCUMENTS if sources is None else sources
    for source_id, source in documents.items():
        accession = source["accession"]
        if accession not in ALLOWED_SOURCE_ACCESSIONS:
            raise ValueError(f"unapproved source accession: {accession}")
        if source["filed"] > PIT_CUTOFF:
            raise ValueError(f"source {source_id} was filed after PIT cutoff")
        if source["currency"] != CURRENCY or source["scale"] != SOURCE_SCALE:
            raise ValueError(f"source {source_id} has mixed currency or scale")
        if source["accounting_standard"] != ACCOUNTING_STANDARD:
            raise ValueError(f"source {source_id} is not IFRS-IASB")
        accession_path = accession.replace("-", "")
        if f"/data/{CIK}/{accession_path}/" not in source["url"]:
            raise ValueError(f"source {source_id} URL does not lock CIK/accession")
        if not source["url"].endswith("/" + source["document"]):
            raise ValueError(f"source {source_id} URL does not lock document")
        relative_path = Path(source["local_path"])
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError(f"source {source_id} has unsafe local_path")
        if not re.fullmatch(r"[0-9a-f]{64}", source["expected_sha256"]):
            raise ValueError(f"source {source_id} has invalid expected SHA-256")
    for item_id, item in OPERANDS_CAD_THOUSANDS.items():
        if item["source_id"] not in documents:
            raise ValueError(f"source value {item_id} has no locked source")
    if any(item["filed"] <= PIT_CUTOFF for item in POST_SIGNAL_EXCLUSIONS):
        raise ValueError("post-signal exclusion was available by the signal")


def verify_source_values(raw_by_source: dict[str, bytes]) -> list[dict]:
    if set(raw_by_source) != set(SOURCE_DOCUMENTS):
        raise ValueError("raw source set does not match the source lock")
    parsed = {
        source_id: _parse_source_tables(source_id, raw)
        for source_id, raw in raw_by_source.items()
    }
    verified = []
    for item_id, item in SOURCE_VALUE_EXPECTATIONS.items():
        parsed_value = parsed[item["source_id"]][item["metric"]][
            item["table_column"]
        ]
        expected_value = int(item["value"])
        if parsed_value != expected_value:
            raise RuntimeError(
                f"source value {item_id} changed: parsed {parsed_value}, "
                f"expected {expected_value}"
            )
        verified.append({
            "item_id": item_id,
            "source_id": item["source_id"],
            "metric": item["metric"],
            "table_column": item["table_column"],
            "currency": CURRENCY,
            "scale": SOURCE_SCALE,
            "expected_value": expected_value,
            "parsed_value": parsed_value,
        })
    if (
        parsed["40f_2018_04_30_fy2017_ex992"]["net_income"]["FY2017"]
        != parsed["40f_2018_04_30_fy2017_ex992"]["profit_attributable"][
            "FY2017"
        ]
    ):
        raise RuntimeError("CRON FY2017 attribution scope mismatch")
    for column, expected_nci in (("M9_2017", 0), ("M9_2018", -61)):
        total = parsed["6k_2018_11_13_9m_ex991"]["net_income"][column]
        attributable = parsed["6k_2018_11_13_9m_ex991"][
            "profit_attributable"
        ][column]
        if total - attributable != expected_nci:
            raise RuntimeError(f"CRON {column} non-controlling interest changed")
    return verified


def prepare_verified_sources(
    output_dir: Path,
) -> tuple[dict[str, bytes], dict[str, dict], list[dict]]:
    validate_source_lock()
    output_dir = Path(output_dir)
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
        actual_sha256 = _sha256_bytes(raw)
        if actual_sha256 != source["expected_sha256"]:
            raise RuntimeError(
                f"CRON source SHA-256 mismatch for {source_id}: {actual_sha256}"
            )
        raw_by_source[source_id] = raw
        provenance[source_id] = {
            **source,
            "local_path": str(local_path),
            "actual_sha256": actual_sha256,
            "bytes": len(raw),
            "downloaded": downloaded,
        }
    return raw_by_source, provenance, verify_source_values(raw_by_source)


def exact_ttm_evidence() -> list[dict]:
    validate_source_lock()
    value_thousands = sum(
        coefficient * int(OPERANDS_CAD_THOUSANDS[operand_id]["value"])
        for coefficient, operand_id in TTM_SPEC["terms"]
    )
    if value_thousands != TTM_SPEC["expected_cad_thousands"]:
        raise RuntimeError("CRON exact TTM changed")
    if value_thousands >= 0:
        raise RuntimeError("CRON direct exact-TTM layer is exclusion-only")
    source_ids = list(dict.fromkeys(
        OPERANDS_CAD_THOUSANDS[operand_id]["source_id"]
        for _, operand_id in TTM_SPEC["terms"]
    ))
    sources = [SOURCE_DOCUMENTS[source_id] for source_id in source_ids]
    if TTM_SPEC["available_date"] != max(source["filed"] for source in sources):
        raise RuntimeError("CRON availability date changed")
    return [{
        "ticker": TICKER,
        "evidence_kind": "exact_cumulative_ttm_loss_as_reported",
        "fiscal_end": TTM_SPEC["fiscal_end"],
        "available_date": TTM_SPEC["available_date"],
        "currency": CURRENCY,
        "source_scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
        "net_income_ttm": value_thousands * SOURCE_SCALE,
        "formula": TTM_SPEC["formula"],
        "operand_ids": [operand_id for _, operand_id in TTM_SPEC["terms"]],
        "source_ids": source_ids,
        "source_accessions": [source["accession"] for source in sources],
        "source_urls": [source["url"] for source in sources],
        "form": TTM_SPEC["form"],
        "profit_scope": (
            "consolidated IFRS profit/loss attributable to Cronos Group/common "
            "shareholders; issuer CAD amount, not EPS"
        ),
    }]


def direct_ttm_facts(fetched_at: str | None = None) -> pd.DataFrame:
    if fetched_at is None:
        fetched_at = str(
            pd.Timestamp.now("UTC").tz_localize(None).normalize().date()
        )
    rows = [{
        "ticker": TICKER,
        "fiscal_end": evidence["fiscal_end"],
        "available_date": evidence["available_date"],
        "metric": "net_income_ttm",
        "value": evidence["net_income_ttm"],
        "taxonomy": "ifrs-full",
        "concept": "cron_exact_ttm:ProfitLossAttributableToOwnersOfParent:CAD",
        "form": evidence["form"],
        "accession": "+".join(evidence["source_accessions"]),
        "fetched_at": fetched_at,
    } for evidence in exact_ttm_evidence()]
    return pd.DataFrame(rows, columns=OUTPUT_COLUMNS)


def resolve_observation(signal_date: str, maximum_age_days: int) -> dict:
    signal = pd.Timestamp(signal_date)
    evidence = pd.DataFrame(exact_ttm_evidence())
    evidence["fiscal_end"] = pd.to_datetime(evidence["fiscal_end"])
    evidence["available_date"] = pd.to_datetime(evidence["available_date"])
    eligible = evidence.loc[
        evidence["available_date"].le(signal)
        & (signal - evidence["available_date"]).dt.days.le(maximum_age_days)
    ].sort_values(["fiscal_end", "available_date"])
    if eligible.empty:
        return {
            "resolved": False,
            "decision": "missing_financial",
            "reason": "no exact TTM loss available within the age limit",
        }
    row = eligible.iloc[-1]
    return {
        "resolved": True,
        "decision": "known_nonpositive_profit",
        "fiscal_end": row["fiscal_end"].strftime("%Y-%m-%d"),
        "available_date": row["available_date"].strftime("%Y-%m-%d"),
        "financial_age_days": int((signal - row["available_date"]).days),
        "net_income_ttm": int(row["net_income_ttm"]),
        "currency": row["currency"],
        "source_accessions": row["source_accessions"],
    }


def resolve_audit_observations(
    observations: Iterable[tuple[str, str, int]] = AUDIT_OBSERVATIONS,
) -> pd.DataFrame:
    return pd.DataFrame([{
        "scenario": scenario,
        "signal_date": signal_date,
        "maximum_age_days": maximum_age_days,
        **resolve_observation(signal_date, maximum_age_days),
    } for scenario, signal_date, maximum_age_days in observations])


def build(output_dir: Path = OUTPUT_DIR) -> dict:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _, provenance, source_value_verification = prepare_verified_sources(output_dir)
    evidence = exact_ttm_evidence()
    facts = direct_ttm_facts()
    resolutions = resolve_audit_observations()
    if not resolutions["resolved"].all():
        raise RuntimeError("not every declared CRON audit observation resolved")

    facts_path = output_dir / "strict_quarterly_facts.csv"
    evidence_path = output_dir / "exact_ttm_evidence.json"
    resolution_path = output_dir / "audit_observation_resolution.json"
    facts.to_csv(facts_path, index=False)
    evidence_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    resolution_path.write_text(
        resolutions.to_json(orient="records", indent=2) + "\n",
        encoding="utf-8",
    )
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "shared_candidate_integrated": False,
        "ticker": TICKER,
        "cik": CIK,
        "accounting_standard": ACCOUNTING_STANDARD,
        "reporting_currency": CURRENCY,
        "security": (
            "Cronos Group common shares listed on TSX and Nasdaq; consolidated "
            "issuer CAD amounts, not ADS or EPS"
        ),
        "reporting_profile": "CANADIAN_FOREIGN_PRIVATE_ISSUER_40-F_6-K",
        "baseline_binding": BASELINE_BINDING,
        "accepted_exact_ttm_loss_count": len(facts),
        "resolved_unique_signal_date_count": resolutions["signal_date"].nunique(),
        "resolved_audit_observation_count": len(resolutions),
        "source_documents": provenance,
        "source_value_verification": source_value_verification,
        "accounting_policy_audit": ACCOUNTING_POLICY_AUDIT,
        "post_signal_exclusions": POST_SIGNAL_EXCLUSIONS,
        "revenue_assessment": {
            "direct_growth_emitted": False,
            "reason": (
                "Exact negative attributable TTM profit resolves eligibility. "
                "No revenue, quarterly split, EPS, or growth fact is emitted."
            ),
        },
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path), "sha256": _sha256(facts_path)
            },
            "exact_ttm_evidence": {
                "path": str(evidence_path), "sha256": _sha256(evidence_path)
            },
            "audit_observation_resolution": {
                "path": str(resolution_path), "sha256": _sha256(resolution_path)
            },
        },
        "guardrail": (
            "Every operand is an as-reported consolidated IFRS attributable "
            "amount in CAD thousands. Q3 total loss is separately reconciled "
            "to Cronos Group attribution. The latest operand was filed "
            "2018-11-13, 107 days before the signal. The 2019-03-26 annual "
            "filing and later U.S.-GAAP presentation are excluded. The layer "
            "cannot create quarterly growth."
        ),
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
    args = parser.parse_args()
    report = build(args.output_dir)
    print(json.dumps({
        "manifest": report["manifest"],
        "accepted_exact_ttm_loss_count": report["accepted_exact_ttm_loss_count"],
        "resolved_unique_signal_date_count": report["resolved_unique_signal_date_count"],
        "resolved_audit_observation_count": report["resolved_audit_observation_count"],
        "promotion_eligible": False,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
