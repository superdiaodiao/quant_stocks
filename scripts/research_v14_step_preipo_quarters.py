#!/usr/bin/env python3
"""Recover STEP's eight PIT quarters on a consolidated predecessor basis."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import re
import shutil
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd
from bs4 import BeautifulSoup

from src.financial.quarterly_fundamentals import quarterly_growth_snapshot
from src.io.fundamentals_update import OUTPUT_COLUMNS, merge_fundamentals


CIK = 1_796_022
TICKER = "STEP"
COMPANYFACTS_URL = (
    "https://data.sec.gov/api/xbrl/companyfacts/CIK0001796022.json"
)
COMPANYFACTS_CACHE = Path(
    "output/research_only/v14/companyfacts_cache/CIK0001796022.json.gz"
)
COMPANYFACTS_SHA256 = (
    "92d749be26e737e6a7a030481174f37ed1bb8a5a4541e2ab5a7c68fa7114600e"
)
S1_URL = (
    "https://www.sec.gov/Archives/edgar/data/1796022/"
    "000119312520228520/d828990ds1.htm"
)
S1_ACCESSION = "0001193125-20-228520"
S1_FILED = "2020-08-24"
S1_SHA256 = (
    "04df40a58689255c76663a19adaf73ebe982d524e7773867a8d4fb0830df8069"
)
S1_PATH = Path("output/data_provenance/step_2020_ipo/step_2020_s1.htm")
OUTPUT_DIR = Path(
    "output/research_only/v14/step_preipo_quarters_2019q3_2021q1"
)
HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}

SIGNAL_DATES = ("2021-09-30", "2021-10-29")
MAXIMUM_AGES = (150, 365, 550)
TARGET_FISCAL_ENDS = (
    "2019-09-30",
    "2019-12-31",
    "2020-03-31",
    "2020-06-30",
    "2020-09-30",
    "2020-12-31",
    "2021-03-31",
    "2021-06-30",
)
METRIC_CONCEPTS = {"revenue": "Revenues", "net_income": "ProfitLoss"}
REJECTED_NET_INCOME_CONCEPT = "NetIncomeLoss"

EXPECTED = {
    "2019-09-30": {"revenue": 131_872_000.0, "net_income": 46_360_000.0},
    "2019-12-31": {"revenue": 71_215_000.0, "net_income": 17_234_000.0},
    "2020-03-31": {"revenue": 143_945_000.0, "net_income": 50_182_000.0},
    "2020-06-30": {"revenue": -61_413_000.0, "net_income": -52_360_000.0},
    "2020-09-30": {"revenue": 242_913_000.0, "net_income": 108_369_000.0},
    "2020-12-31": {"revenue": 247_150_000.0, "net_income": 107_389_000.0},
    "2021-03-31": {"revenue": 359_066_000.0, "net_income": 151_195_000.0},
    "2021-06-30": {"revenue": 308_605_000.0, "net_income": 126_519_000.0},
}
EXPECTED_S1_OPERANDS = {
    "2019-06-30": {"revenue": 99_579_000.0, "net_income": 31_009_000.0},
    "2020-06-30": {"revenue": -61_413_000.0, "net_income": -52_360_000.0},
    "2020-03-31_FY": {
        "revenue": 446_611_000.0,
        "net_income": 144_785_000.0,
    },
}
EXPECTED_ANNUAL = {
    "2020-03-31": {"revenue": 446_611_000.0, "net_income": 144_785_000.0},
    "2021-03-31": {"revenue": 787_716_000.0, "net_income": 314_593_000.0},
}
EXPECTED_TTM = {
    "prior_revenue_ttm": 285_619_000.0,
    "revenue_ttm": 1_157_734_000.0,
    "revenue_growth": 3.0534208158420832,
    "prior_net_income_ttm": 61_416_000.0,
    "net_income_ttm": 493_472_000.0,
    "net_income_growth": 7.034909469844991,
}

DIRECT_COORDINATES = {
    "2019-09-30": {
        "start": "2019-07-01",
        "accession": "0001796022-20-000010",
        "filed": "2020-11-12",
        "form": "10-Q",
    },
    "2019-12-31": {
        "start": "2019-10-01",
        "accession": "0001796022-21-000007",
        "filed": "2021-02-11",
        "form": "10-Q",
    },
    "2020-06-30": {
        "start": "2020-04-01",
        "accession": "0001796022-21-000050",
        "filed": "2021-08-12",
        "form": "10-Q",
    },
    "2020-09-30": {
        "start": "2020-07-01",
        "accession": "0001796022-20-000010",
        "filed": "2020-11-12",
        "form": "10-Q",
    },
    "2020-12-31": {
        "start": "2020-10-01",
        "accession": "0001796022-21-000007",
        "filed": "2021-02-11",
        "form": "10-Q",
    },
    "2021-06-30": {
        "start": "2021-04-01",
        "accession": "0001796022-21-000050",
        "filed": "2021-08-12",
        "form": "10-Q",
    },
}
Q4_COORDINATES = {
    "2020-03-31": {
        "start": "2019-04-01",
        "accession": "0001796022-21-000044",
        "filed": "2021-06-23",
        "form": "10-K",
        "quarter_ends": ("2019-06-30", "2019-09-30", "2019-12-31"),
    },
    "2021-03-31": {
        "start": "2020-04-01",
        "accession": "0001796022-21-000044",
        "filed": "2021-06-23",
        "form": "10-K",
        "quarter_ends": ("2020-06-30", "2020-09-30", "2020-12-31"),
    },
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normal(value: object) -> str:
    return " ".join(
        str(value).replace("\xa0", " ").replace("\u200b", " ").split()
    ).casefold()


def _accounting_value(value: object) -> float:
    text = str(value).replace(",", "").replace("$", "").strip()
    if text in {"—", "-", "–"}:
        return 0.0
    match = re.search(r"\d+(?:\.\d+)?", text)
    if match is None:
        raise ValueError(f"no accounting value in {value!r}")
    result = float(match.group())
    return -result if "(" in text else result


def ensure_s1(path: Path = S1_PATH) -> Path:
    """Download the locked original S-1 only when the local copy is absent."""
    path = Path(path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with urlopen(Request(S1_URL, headers=HEADERS), timeout=120) as response:
            path.write_bytes(response.read())
    actual = _sha256(path)
    if actual != S1_SHA256:
        raise RuntimeError(
            f"STEP S-1 SHA changed: expected {S1_SHA256}, found {actual}"
        )
    return path


def _table_value(
    table: pd.DataFrame,
    row: pd.Series,
    *,
    period_phrase: str,
    year: int,
) -> float:
    period_headers = table.iloc[1].map(_normal)
    year_headers = table.iloc[2].map(_normal)
    values = []
    for column in table.columns:
        if period_headers[column] != period_phrase:
            continue
        if not year_headers[column].startswith(str(year)):
            continue
        try:
            values.append(_accounting_value(row[column]) * 1_000.0)
        except ValueError:
            continue
    unique = sorted(set(values))
    if len(unique) != 1:
        raise ValueError(
            f"STEP S-1 expected one {period_phrase} {year} value, found {unique}"
        )
    return unique[0]


def extract_s1_operands(path: Path) -> dict[str, dict[str, float]]:
    """Extract the predecessor Q1 operands and FY2020 annual identity."""
    raw = path.read_bytes()
    text = " ".join(
        BeautifulSoup(raw, "html.parser").get_text(" ", strip=True).split()
    ).casefold()
    required = (
        "stepstone group lp, our predecessor for accounting purposes",
        "for periods prior to giving effect to the reorganization transactions",
        "the partnership and its consolidated subsidiaries",
    )
    if not all(phrase in text for phrase in required):
        raise ValueError("STEP S-1 does not prove the locked predecessor boundary")

    candidates = []
    for table in pd.read_html(BytesIO(raw)):
        if len(table) < 30 or len(table.columns) < 20:
            continue
        labels = table.iloc[:, 0].map(_normal)
        if labels.eq("total revenues").sum() != 1:
            continue
        if labels.eq("net income (loss)").sum() != 1:
            continue
        headers = " ".join(
            _normal(value) for value in table.head(4).to_numpy().ravel()
        )
        if (
            "three months ended june 30," not in headers
            or "year ended march 31," not in headers
        ):
            continue
        revenue = table.loc[labels.eq("total revenues")].iloc[0]
        income = table.loc[labels.eq("net income (loss)")].iloc[0]
        candidates.append({
            "2019-06-30": {
                "revenue": _table_value(
                    table,
                    revenue,
                    period_phrase="three months ended june 30,",
                    year=2019,
                ),
                "net_income": _table_value(
                    table,
                    income,
                    period_phrase="three months ended june 30,",
                    year=2019,
                ),
            },
            "2020-06-30": {
                "revenue": _table_value(
                    table,
                    revenue,
                    period_phrase="three months ended june 30,",
                    year=2020,
                ),
                "net_income": _table_value(
                    table,
                    income,
                    period_phrase="three months ended june 30,",
                    year=2020,
                ),
            },
            "2020-03-31_FY": {
                "revenue": _table_value(
                    table,
                    revenue,
                    period_phrase="year ended march 31,",
                    year=2020,
                ),
                "net_income": _table_value(
                    table,
                    income,
                    period_phrase="year ended march 31,",
                    year=2020,
                ),
            },
        })
    if not candidates or any(
        candidate != EXPECTED_S1_OPERANDS for candidate in candidates
    ):
        raise ValueError(f"STEP S-1 operands changed: {candidates}")
    return deepcopy(EXPECTED_S1_OPERANDS)


def load_companyfacts(cache_path: Path = COMPANYFACTS_CACHE) -> dict:
    cache_path = Path(cache_path)
    actual = _sha256(cache_path)
    if actual != COMPANYFACTS_SHA256:
        raise RuntimeError(
            "STEP Company Facts SHA changed: "
            f"expected {COMPANYFACTS_SHA256}, found {actual}"
        )
    with gzip.open(cache_path, "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    if (
        envelope.get("cik") != CIK
        or envelope.get("symbols") != [TICKER]
        or envelope.get("source_url") != COMPANYFACTS_URL
        or envelope.get("payload", {}).get("cik") != CIK
    ):
        raise RuntimeError("STEP Company Facts envelope identity changed")
    return envelope


def _duration_rows(payload: dict, concept: str) -> list[dict]:
    if concept not in set(METRIC_CONCEPTS.values()):
        raise ValueError(
            f"STEP issuer override rejects unsupported concept {concept!r}"
        )
    return payload.get("facts", {}).get("us-gaap", {}).get(
        concept, {}
    ).get("units", {}).get("USD", [])


def _select_companyfact(
    payload: dict,
    concept: str,
    *,
    fiscal_end: str,
    start: str,
    accession: str,
    filed: str,
    form: str,
    annual: bool = False,
) -> float:
    values = []
    for row in _duration_rows(payload, concept):
        if (
            row.get("end") != fiscal_end
            or row.get("start") != start
            or row.get("accn") != accession
            or row.get("filed") != filed
            or row.get("form") != form
        ):
            continue
        start_date = pd.to_datetime(row.get("start"), errors="coerce")
        end_date = pd.to_datetime(row.get("end"), errors="coerce")
        value = pd.to_numeric(row.get("val"), errors="coerce")
        if pd.isna(start_date) or pd.isna(end_date) or pd.isna(value):
            continue
        days = (end_date - start_date).days
        if annual and not 330 <= days <= 400:
            continue
        if not annual and not 60 <= days <= 135:
            continue
        values.append(float(value))
    unique = sorted(set(values))
    if len(unique) != 1:
        raise RuntimeError(
            "STEP expected one Company Fact for "
            f"{concept} {fiscal_end} {accession}, found {unique}"
        )
    return unique[0]


def recover_quarters(
    envelope: dict,
    s1_operands: dict[str, dict[str, float]],
) -> tuple[pd.DataFrame, dict]:
    """Build one paired consolidated fact per target quarter."""
    payload = envelope["payload"]
    us_gaap = payload.get("facts", {}).get("us-gaap", {})
    if REJECTED_NET_INCOME_CONCEPT not in us_gaap:
        raise RuntimeError("STEP expected the rejected parent concept to be present")
    if "ProfitLoss" not in us_gaap:
        raise RuntimeError(
            "STEP consolidated ProfitLoss is absent; NetIncomeLoss fallback is forbidden"
        )
    fetched_at = pd.Timestamp(envelope["fetched_at"]).tz_localize(None).normalize()
    recovered: dict[str, dict[str, float]] = {}
    records = []

    for fiscal_end, coordinate in DIRECT_COORDINATES.items():
        values = {}
        for metric, concept in METRIC_CONCEPTS.items():
            value = _select_companyfact(
                payload,
                concept,
                fiscal_end=fiscal_end,
                start=coordinate["start"],
                accession=coordinate["accession"],
                filed=coordinate["filed"],
                form=coordinate["form"],
            )
            values[metric] = value
            records.append({
                "ticker": TICKER,
                "fiscal_end": fiscal_end,
                "available_date": coordinate["filed"],
                "metric": metric,
                "value": value,
                "taxonomy": "us-gaap",
                "concept": concept,
                "form": coordinate["form"],
                "accession": coordinate["accession"],
                "fetched_at": fetched_at,
            })
        recovered[fiscal_end] = values

    annual_values = {}
    for fiscal_end, coordinate in Q4_COORDINATES.items():
        values = {}
        annual_values[fiscal_end] = {}
        for metric, concept in METRIC_CONCEPTS.items():
            annual_value = _select_companyfact(
                payload,
                concept,
                fiscal_end=fiscal_end,
                start=coordinate["start"],
                accession=coordinate["accession"],
                filed=coordinate["filed"],
                form=coordinate["form"],
                annual=True,
            )
            annual_values[fiscal_end][metric] = annual_value
            operands = []
            for quarter_end in coordinate["quarter_ends"]:
                if quarter_end in recovered:
                    operands.append(recovered[quarter_end][metric])
                else:
                    operands.append(s1_operands[quarter_end][metric])
            value = annual_value - sum(operands)
            values[metric] = value
            records.append({
                "ticker": TICKER,
                "fiscal_end": fiscal_end,
                "available_date": coordinate["filed"],
                "metric": metric,
                "value": value,
                "taxonomy": "STEP_US_GAAP_CONSOLIDATED_PREDECESSOR",
                "concept": f"derived_q4:{concept}",
                "form": coordinate["form"],
                "accession": coordinate["accession"],
                "fetched_at": fetched_at,
            })
        recovered[fiscal_end] = values

    recovered = dict(sorted(recovered.items()))
    if recovered != EXPECTED:
        raise RuntimeError(f"STEP recovered quarters changed: {recovered}")
    if annual_values != EXPECTED_ANNUAL:
        raise RuntimeError(f"STEP annual Company Facts changed: {annual_values}")
    if s1_operands["2020-06-30"] != EXPECTED["2020-06-30"]:
        raise RuntimeError("STEP S-1 and 2021 comparative Q1 values disagree")

    frame = pd.DataFrame(records, columns=OUTPUT_COLUMNS).sort_values(
        ["fiscal_end", "metric"]
    ).reset_index(drop=True)
    if (
        len(frame) != 16
        or frame[["ticker", "fiscal_end", "metric"]].duplicated().any()
        or frame.groupby("fiscal_end")["metric"].nunique().ne(2).any()
        or frame["concept"].str.contains(REJECTED_NET_INCOME_CONCEPT).any()
    ):
        raise RuntimeError("STEP recovery is not eight paired consolidated quarters")
    return frame, annual_values


def annual_identity_checks(
    facts: pd.DataFrame,
    s1_operands: dict[str, dict[str, float]],
    annual_values: dict[str, dict[str, float]],
) -> list[dict]:
    values = {
        str(pd.Timestamp(end).date()): group.set_index("metric")["value"].to_dict()
        for end, group in facts.groupby(pd.to_datetime(facts["fiscal_end"]))
    }
    definitions = {
        "2020-03-31": ("2019-06-30", "2019-09-30", "2019-12-31", "2020-03-31"),
        "2021-03-31": ("2020-06-30", "2020-09-30", "2020-12-31", "2021-03-31"),
    }
    checks = []
    for annual_end, quarter_ends in definitions.items():
        sums = {}
        for metric in METRIC_CONCEPTS:
            operands = []
            for quarter_end in quarter_ends:
                source = s1_operands if quarter_end == "2019-06-30" else values
                operands.append(source[quarter_end][metric])
            sums[metric] = sum(operands)
        expected = annual_values[annual_end]
        if sums != expected:
            raise RuntimeError(
                f"STEP {annual_end} quarters do not close: {sums} != {expected}"
            )
        checks.append({
            "fiscal_end": annual_end,
            "quarter_ends": list(quarter_ends),
            "quarter_sum": sums,
            "companyfacts_annual": expected,
            "difference": {metric: 0.0 for metric in METRIC_CONCEPTS},
        })
    return checks


def snapshot_checks(facts: pd.DataFrame) -> list[dict]:
    working = facts.copy()
    working["fiscal_end"] = pd.to_datetime(working["fiscal_end"])
    working["available_date"] = pd.to_datetime(working["available_date"])
    earliest_signal = pd.Timestamp(min(SIGNAL_DATES))
    if working["available_date"].gt(earliest_signal).any():
        bad = working.loc[working["available_date"].gt(earliest_signal)]
        raise RuntimeError(
            "STEP recovery contains post-signal facts: "
            f"{bad[['fiscal_end', 'available_date', 'metric']].to_dict('records')}"
        )

    checks = []
    for signal_date in SIGNAL_DATES:
        for maximum_age_days in MAXIMUM_AGES:
            snapshot = quarterly_growth_snapshot(
                working,
                pd.Timestamp(signal_date),
                maximum_age_days=maximum_age_days,
            )
            if TICKER not in snapshot.index:
                raise RuntimeError(
                    f"STEP missing from {signal_date} age {maximum_age_days} snapshot"
                )
            row = snapshot.loc[TICKER]
            actual = {
                "revenue_ttm": float(row["revenue_ttm"]),
                "revenue_growth": float(row["revenue_growth"]),
                "net_income_ttm": float(row["net_income_ttm"]),
                "net_income_growth": float(row["net_income_growth"]),
            }
            for field, value in actual.items():
                if not math.isclose(
                    value,
                    EXPECTED_TTM[field],
                    rel_tol=1e-12,
                    abs_tol=1e-6,
                ):
                    raise RuntimeError(
                        f"STEP {signal_date} age {maximum_age_days} {field} "
                        f"changed: {value}"
                    )
            checks.append({
                "signal_date": signal_date,
                "maximum_age_days": maximum_age_days,
                "fiscal_end": str(pd.Timestamp(row["fiscal_end"]).date()),
                "growth_available_date": str(
                    pd.Timestamp(row["growth_available_date"]).date()
                ),
                "financial_age_days": int(row["financial_age_days"]),
                **actual,
            })
    if len(checks) != 6:
        raise RuntimeError("STEP expected six signal-date/age snapshot checks")
    return checks


def run(
    *,
    cache_path: Path = COMPANYFACTS_CACHE,
    s1_path: Path = S1_PATH,
    output_dir: Path = OUTPUT_DIR,
) -> dict:
    s1_path = ensure_s1(s1_path)
    s1_operands = extract_s1_operands(s1_path)
    envelope = load_companyfacts(cache_path)
    facts, annual_values = recover_quarters(envelope, s1_operands)
    annual_checks = annual_identity_checks(facts, s1_operands, annual_values)
    snapshots = snapshot_checks(facts)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1,
        "research_only": True,
        "ticker": TICKER,
        "cik": CIK,
        "point_in_time_proven": True,
        "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "accepted_accounting_basis": "CONSOLIDATED_ACCOUNTING_PREDECESSOR",
        "accepted_revenue_concept": "Revenues",
        "accepted_net_income_concept": "ProfitLoss",
        "rejected_net_income_concept": REJECTED_NET_INCOME_CONCEPT,
        "accepted_quarter_count": 8,
        "fact_count": 16,
        "target_fiscal_ends": list(TARGET_FISCAL_ENDS),
        "s1_operands": s1_operands,
        "annual_identity_checks": annual_checks,
        "snapshot_checks": snapshots,
        "expected_ttm": EXPECTED_TTM,
        "sources": [
            {
                "source": "SEC_COMPANY_FACTS_RAW_CACHE",
                "url": COMPANYFACTS_URL,
                "path": str(cache_path),
                "sha256": _sha256(Path(cache_path)),
                "fetched_at": envelope["fetched_at"],
            },
            {
                "source": "SEC_S1_ACCOUNTING_PREDECESSOR_AND_Q1_OPERANDS",
                "url": S1_URL,
                "path": str(s1_path),
                "sha256": _sha256(s1_path),
                "form": "S-1",
                "accession": S1_ACCESSION,
                "filed": S1_FILED,
            },
        ],
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path),
                "sha256": _sha256(facts_path),
            }
        },
        "guardrail": (
            "STEP is an Up-C issuer. Revenue and profit are recovered on the "
            "same consolidated accounting-predecessor boundary using Revenues "
            "and ProfitLoss. NetIncomeLoss is explicitly rejected because it "
            "is zero for the pre-IPO shell and parent-attributable after the "
            "IPO. Every accepted filing date precedes both missing signals."
        ),
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    report["manifest"] = str(manifest_path)
    return report


def integrate_candidate(
    *,
    base_dir: Path,
    supplement_dir: Path = OUTPUT_DIR,
    output_dir: Path,
) -> dict:
    """Replace only STEP's eight conflicting quarter coordinates."""
    base_dir = Path(base_dir)
    supplement_dir = Path(supplement_dir)
    output_dir = Path(output_dir)
    base_annual = base_dir / "annual.csv"
    base_quarterly = base_dir / "quarterly.csv"
    base_manifest = base_dir / "manifest.json"
    supplement = supplement_dir / "strict_quarterly_facts.csv"
    supplement_manifest = supplement_dir / "manifest.json"
    source_paths = (
        base_annual,
        base_quarterly,
        base_manifest,
        supplement,
        supplement_manifest,
    )
    before_sha = {path: _sha256(path) for path in source_paths}

    base = pd.read_csv(base_quarterly)
    incoming = pd.read_csv(supplement)
    base_ends = pd.to_datetime(base["fiscal_end"], errors="coerce")
    conflict = (
        base["ticker"].astype(str).str.upper().eq(TICKER)
        & base_ends.dt.strftime("%Y-%m-%d").isin(TARGET_FISCAL_ENDS)
        & base["metric"].isin(METRIC_CONCEPTS)
    )
    removed = base.loc[conflict].copy()
    retained = base.loc[~conflict].copy()
    merged = merge_fundamentals(retained, incoming)
    target = merged.loc[
        merged["ticker"].eq(TICKER)
        & merged["fiscal_end"].dt.strftime("%Y-%m-%d").isin(TARGET_FISCAL_ENDS)
        & merged["metric"].isin(METRIC_CONCEPTS)
    ]
    if (
        len(target) != 16
        or target[["ticker", "fiscal_end", "metric"]].duplicated().any()
        or target["concept"].str.contains(REJECTED_NET_INCOME_CONCEPT).any()
    ):
        raise RuntimeError("STEP candidate overlay retained conflicting quarter rows")
    snapshot_checks(target)

    output_dir.mkdir(parents=True, exist_ok=True)
    annual_output = output_dir / "annual.csv"
    quarterly_output = output_dir / "quarterly.csv"
    shutil.copyfile(base_annual, annual_output)
    merged.to_csv(quarterly_output, index=False)
    after_sha = {path: _sha256(path) for path in source_paths}
    if after_sha != before_sha:
        raise RuntimeError("STEP integration source changed while being read")

    report = {
        "schema_version": 1,
        "research_only": True,
        "formal_financials_modified": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "ticker": TICKER,
        "removed_conflict_rows": int(len(removed)),
        "inserted_strict_rows": int(len(incoming)),
        "conflict_scope": {
            "ticker": TICKER,
            "fiscal_ends": list(TARGET_FISCAL_ENDS),
            "metrics": sorted(METRIC_CONCEPTS),
        },
        "base": {
            "path": str(base_dir),
            "manifest_sha256": before_sha[base_manifest],
            "annual_sha256": before_sha[base_annual],
            "quarterly_sha256": before_sha[base_quarterly],
        },
        "supplement": {
            "path": str(supplement),
            "sha256": before_sha[supplement],
            "manifest_sha256": before_sha[supplement_manifest],
        },
        "outputs": {
            "annual": str(annual_output),
            "annual_sha256": _sha256(annual_output),
            "quarterly": str(quarterly_output),
            "quarterly_sha256": _sha256(quarterly_output),
            "quarterly_rows": int(len(merged)),
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
    parser.add_argument("--cache-path", type=Path, default=COMPANYFACTS_CACHE)
    parser.add_argument("--s1-path", type=Path, default=S1_PATH)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--base-dir", type=Path)
    parser.add_argument("--candidate-output-dir", type=Path)
    args = parser.parse_args()
    report = run(
        cache_path=args.cache_path,
        s1_path=args.s1_path,
        output_dir=args.output_dir,
    )
    if (args.base_dir is None) != (args.candidate_output_dir is None):
        parser.error("--base-dir and --candidate-output-dir must be used together")
    if args.base_dir is not None:
        report["candidate"] = integrate_candidate(
            base_dir=args.base_dir,
            supplement_dir=args.output_dir,
            output_dir=args.candidate_output_dir,
        )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
