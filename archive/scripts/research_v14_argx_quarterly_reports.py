#!/usr/bin/env python3
"""Recover ARGX quarters without crossing its 2021 EUR-to-USD boundary."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd
from lxml import html as lxml_html

from scripts.research_v14_sec_filing_exhibit_financials import (
    _parse_accounting_number,
)
from scripts.research_v14_team_sec_quarterly_filings import _longest_chain


DEFAULT_REGISTRY = Path("stocks_list_dir/nasdaq/argx_quarterly_reports.csv")
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/argx_sec_quarterly_reports_2019_2021"
)
METRICS = ("revenue", "net_income")
TICKER = "ARGX"
CIK = 1_697_862
BLOCKED_SIGNAL = "2021-08-31"
AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260827_sy_glpg_rlmd_smpl_classified_financial_priorities.csv"
)
EXPECTED_AUDIT_SHA256 = (
    "616ebd6a836bb1f0571ad690fbcd1b0bf56ae06b092041ac406eb976b6243e0e"
)
EXPECTED_SOURCE_SHA256 = {
    "2017_fy": "38665ee7347cc99cf750433ebe5bfc8793c83e733440f3f712aac3e07ea201eb",
    "2018_q1": "3751780847356578b0e437eee9354f4d68d8177dc9b8f6e55a25c40c3b174d85",
    "2018_h1": "5728e62b1655e67dd4de83f17add0447716ec912c13e6b0a12308f8390fc5392",
    "2018_q3": "0029bbb7baa4a8c5d0b42d54ef31603818821ae792515046da8a631dba2b04c2",
    "2018_fy": "2b53ea99f6194231585b005159ddb28f64871451264ca42d2e4bf246b736af53",
    "2019_q1": "0e3f426eb83901558723303582b8f403999ec7e127826dc34bd76c4a89228f1d",
    "2019_h1": "4e064bb710d3d25e8312651b7656e63480e811b4fc708eaa98db6e0c93f925ce",
    "2019_q3": "880072dbf1120b0a4edb2598d5f808ccf41adf031f35e2038a667f83a02bfcbd",
    "2019_fy": "4728f5a4a9a048180f8d6df9b37cd675543b7e91ccc0695a75140f065fd71ab1",
    "2020_q1": "a9ad0ad08759784787760ffedd4877f7bbd718c847491dbf906d48547b2ca541",
    "2020_h1": "3b8a348e285c7409c18c2103387a5c9550bce879b2dfe45a984a4fd826b4f02a",
    "2020_q3": "d59594d663ec4c685e8693ad1a94c824a906a01fc871d6c85f5b07eb0e9ceb66",
    "2020_fy": "11c58258c31ad6371c9b087de790b9fc3ed0142623ad087020bb27a14c63db50",
    "2021_q1": "054d04257d7cd5f3296ed129727f252c74a0f962fc996c5d90420c4d4c0e1595",
    "2021_h1": "4fa41108a592f76eb08eec2ef607195157b56bb4e3cee6f06f58661dffc4e9e5",
    "2021_fy": "a6ca4b81516c110f327dca4de157f31e4561869aa55ef7588f55ba8e2ce47631",
    "2022_q3": "85c34eb6df701a97f6e171033646b4f60f4601abfeac5f1445932f10eb855678",
}
AUDIT_OBSERVATIONS = (
    ("liq2000000-age150-growth", BLOCKED_SIGNAL, 150),
    ("liq10000000-age150-growth", BLOCKED_SIGNAL, 150),
)
NEGATIVE_TEXT_CHECKS = {
    "2021_q1": (
        "As of January 1, 2021, the Company changed its functional and "
        "presentation currency from euro to U.S. dollars",
        "Historical financials have been converted at the average exchange "
        "rate of the related period.",
    ),
    "2021_h1": (
        "The change in presentation currency, effective January 1, 2021, from "
        "EUR to USD is retroactively applied on comparative figures according "
        "to IAS 8 and IAS 21, as if USD had always been the presentation "
        "currency of the consolidated financial statements.",
        "The Company has adopted a change in its presentation currency from "
        "EUR to USD at January 1, 2021",
    ),
}
BLOCKED_REASON = (
    "The pre-signal 2021H1 report provides 2021H1 and comparative 2020H1 in "
    "USD, but no pre-signal filing provides the exact restated 2020 full-year "
    "USD income statement needed to derive 2020H2. The prior-year TTM also "
    "lacks a homogeneous 2019 USD annual/H1 chain. Mixing the filed EUR "
    "annuals with USD half-years or estimating conversions would create a "
    "cross-currency growth series."
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normal(value: Any) -> str:
    return " ".join(str(value).replace("\xa0", " ").split()).casefold()


def _statement_table(path: Path, revenue_label: str, net_label: str) -> pd.DataFrame:
    candidates = []
    for table in pd.read_html(path):
        first = table.iloc[:, 0].fillna("").map(_normal)
        if first.eq(_normal(revenue_label)).any() and first.eq(_normal(net_label)).any():
            candidates.append(table)
    if len(candidates) != 1:
        raise ValueError(
            f"expected one ARGX income statement in {path}, found {len(candidates)}"
        )
    return candidates[0]


def _period_columns(table: pd.DataFrame, phrase: str, year: int) -> list[Any]:
    period_columns: set[Any] = set()
    year_columns: set[Any] = set()
    for _, row in table.head(6).iterrows():
        for column, value in row.items():
            text = _normal(value)
            if _normal(phrase) in text:
                period_columns.add(column)
            if text in {str(year), f"{year}.0"}:
                year_columns.add(column)
    selected = [
        column
        for column in table.columns
        if column in period_columns and column in year_columns
    ]
    if not selected:
        raise ValueError(f"ARGX statement has no {phrase} {year} column")
    return selected


def _row_value(table: pd.DataFrame, label: str, columns: list[Any]) -> float:
    first = table.iloc[:, 0].fillna("").map(_normal)
    rows = table.loc[first.eq(_normal(label))]
    if len(rows) != 1:
        raise ValueError(f"expected one ARGX row for {label!r}")
    values = set()
    for column in columns:
        raw = rows.iloc[0][column]
        if isinstance(raw, str):
            raw = raw.replace("€", "").replace("$", "").strip()
        parsed = _parse_accounting_number(raw)
        if parsed is not None:
            values.add(parsed)
    values = sorted(values)
    if len(values) != 1:
        raise ValueError(f"expected one ARGX value for {label!r}: {values}")
    return round(values[0] * 1_000.0, 2)


def _extract(
    path: Path,
    *,
    phrase: str,
    year: int,
    revenue_label: str = "Revenue",
    net_label: str,
    fixed_column: int | None = None,
) -> dict[str, float]:
    table = _statement_table(path, revenue_label, net_label)
    columns = (
        [table.columns[fixed_column]]
        if fixed_column is not None
        else _period_columns(table, phrase, year)
    )
    return {
        "revenue": _row_value(table, revenue_label, columns),
        "net_income": _row_value(table, net_label, columns),
    }


def _subtract(left: dict[str, float], *rights: dict[str, float]) -> dict[str, float]:
    return {
        metric: round(left[metric] - sum(value[metric] for value in rights), 2)
        for metric in METRICS
    }


def _sum(values: list[dict[str, float]]) -> dict[str, float]:
    return {metric: round(sum(value[metric] for value in values), 2) for metric in METRICS}


def _agree(left: dict[str, float], right: dict[str, float]) -> bool:
    return all(abs(left[metric] - right[metric]) <= 0.01 for metric in METRICS)


def _source_rows(registry: pd.DataFrame) -> dict[str, Any]:
    return {row.source_id: row for row in registry.itertuples(index=False)}


def _document_text(path: Path) -> str:
    root = lxml_html.fromstring(Path(path).read_bytes())
    return " ".join(root.text_content().replace("\xa0", " ").split())


def validate_negative_source_text(paths: dict[str, Path]) -> list[dict[str, str]]:
    checked = []
    for source_id, fragments in NEGATIVE_TEXT_CHECKS.items():
        text = _document_text(paths[source_id]).casefold()
        for fragment in fragments:
            if fragment.casefold() not in text:
                raise RuntimeError(
                    f"ARGX currency disclosure changed for {source_id}: {fragment}"
                )
            checked.append({"source_id": source_id, "fragment": fragment})
    return checked


def validate_audit_binding(path: Path, expected_sha256: str) -> dict[str, Any]:
    path = Path(path)
    actual_sha = _sha256(path)
    if actual_sha != expected_sha256:
        raise RuntimeError(f"ARGX audit binding changed: {actual_sha}")
    priorities = pd.read_csv(path)
    expected_scenarios = {scenario for scenario, _, _ in AUDIT_OBSERVATIONS}
    rows = priorities.loc[
        priorities["ticker"].eq(TICKER)
        & priorities["scenario"].isin(expected_scenarios)
    ]
    if set(rows["scenario"]) != expected_scenarios or len(rows) != len(
        expected_scenarios
    ):
        raise RuntimeError("ARGX priority scenarios changed")
    expected_one = (
        "missing_signal_count",
        "stale_growth_snapshot_signal_count",
    )
    if any(not rows[column].eq(1).all() for column in expected_one):
        raise RuntimeError("ARGX priority missing-signal classification changed")
    expected_zero = (
        "no_raw_pit_financial_facts_signal_count",
        "insufficient_growth_history_signal_count",
    )
    if any(not rows[column].eq(0).all() for column in expected_zero):
        raise RuntimeError("ARGX priority raw-fact classification changed")
    if set(rows["first_missing_signal_date"]) != {BLOCKED_SIGNAL} or set(
        rows["last_missing_signal_date"]
    ) != {BLOCKED_SIGNAL}:
        raise RuntimeError("ARGX blocked signal date changed")
    return {
        "path": str(path),
        "sha256": actual_sha,
        "scenario_count": len(rows),
        "missing_observation_count": len(AUDIT_OBSERVATIONS),
        "signals": [BLOCKED_SIGNAL],
    }


def resolve_unrecoverable_observations() -> pd.DataFrame:
    return pd.DataFrame([
        {
            "scenario": scenario,
            "ticker": TICKER,
            "signal_date": signal,
            "maximum_age_days": maximum_age_days,
            "resolved": False,
            "decision": "unrecoverable_pre_signal_usd_ttm_chain_absent",
            "reason": BLOCKED_REASON,
        }
        for scenario, signal, maximum_age_days in AUDIT_OBSERVATIONS
    ])


def build_negative_evidence(
    observed: dict[str, dict[str, float]],
    sources: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    late_2020_h2_usd = _subtract(
        observed["2020_fy_usd_late"], observed["2020_h1_usd_comparison"]
    )
    late_current_ttm_usd = _sum([
        late_2020_h2_usd,
        observed["2021_h1"],
    ])
    invalid_mixed_current_ttm = _sum([
        _subtract(
            observed["2020_fy"], observed["2020_h1_usd_comparison"]
        ),
        observed["2021_h1"],
    ])
    operands = {
        "signal_date": BLOCKED_SIGNAL,
        "known_pre_signal": {
            "2020_fy_eur": observed["2020_fy"],
            "2020_q1_usd_comparison": observed["2020_q1_usd_comparison"],
            "2020_h1_usd_comparison": observed["2020_h1_usd_comparison"],
            "2021_h1_usd": observed["2021_h1"],
        },
        "missing_pre_signal_operands": [
            "2020_fy_usd_for_current_ttm",
            "2019_fy_and_h1_usd_for_prior_ttm",
        ],
        "late_comparator": {
            "source_id": "2021_fy",
            "filed": pd.Timestamp(sources["2021_fy"].available_date).strftime(
                "%Y-%m-%d"
            ),
            "accession": sources["2021_fy"].accession,
            "2020_fy_usd": observed["2020_fy_usd_late"],
            "derived_2020_h2_usd": late_2020_h2_usd,
            "derived_current_ttm_usd": late_current_ttm_usd,
        },
    }
    rejected = [
        {
            "candidate": "mix 2020 EUR annual with 2020H1/2021H1 USD",
            "rejected": True,
            "invalid_mixed_current_ttm": invalid_mixed_current_ttm,
            "reason": "the annual and half-year operands use different currencies",
        },
        {
            "candidate": "convert EUR annuals with an average FX rate",
            "rejected": True,
            "reason": (
                "the issuer applies IAS 8/IAS 21 retrospective presentation; "
                "an external or rounded FX estimate is not an exact filed PIT operand"
            ),
        },
        {
            "candidate": "backdate the exact 2020 USD annual comparator",
            "rejected": True,
            "filed": operands["late_comparator"]["filed"],
            "accession": operands["late_comparator"]["accession"],
            "reason": "the source was filed after the 2021-08-31 signal",
        },
    ]
    return operands, rejected


def run(
    *,
    registry_path: Path = DEFAULT_REGISTRY,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    audit_path: Path = AUDIT_PATH,
    expected_audit_sha256: str = EXPECTED_AUDIT_SHA256,
) -> dict[str, Any]:
    registry = pd.read_csv(
        registry_path,
        dtype={"ticker": str, "cik": int, "accession": str},
        parse_dates=["available_date"],
    )
    if set(registry["ticker"]) != {"ARGX"} or set(registry["cik"]) != {1697862}:
        raise ValueError("ARGX registry contains another issuer")
    expected_sources = {
        "2017_fy", "2018_q1", "2018_q3",
        "2018_h1", "2018_fy", "2019_q1", "2019_q3",
        "2019_h1", "2019_fy", "2020_q1", "2020_h1", "2020_q3", "2020_fy",
        "2021_q1", "2021_h1", "2021_fy", "2022_q3",
    }
    if set(registry["source_id"]) != expected_sources or len(registry) != len(expected_sources):
        raise ValueError("ARGX registry source set is incomplete")
    if set(EXPECTED_SOURCE_SHA256) != expected_sources:
        raise RuntimeError("ARGX source-lock set is incomplete")
    sources = _source_rows(registry)
    paths = {source_id: Path(row.local_path) for source_id, row in sources.items()}
    missing = [str(path) for path in paths.values() if not path.exists()]
    if missing:
        raise FileNotFoundError("missing ARGX SEC archives: " + ", ".join(missing))
    for source_id, path in paths.items():
        actual_sha = _sha256(path)
        if actual_sha != EXPECTED_SOURCE_SHA256[source_id]:
            raise RuntimeError(
                f"ARGX SEC source changed for {source_id}: {actual_sha}"
            )

    observed = {
        "2017_fy": _extract(
            paths["2017_fy"], phrase="Year Ended", year=2017, fixed_column=1,
            net_label="Loss for the year and total comprehensive loss",
        ),
        "2017_q1_later": _extract(
            paths["2018_q1"], phrase="unused", year=2017, fixed_column=2,
            net_label="Loss for the year and total comprehensive loss",
        ),
        "2017_h1_later": _extract(
            paths["2018_h1"], phrase="Six Months Ended", year=2017,
            net_label="Loss for the period and total comprehensive loss",
        ),
        "2017_m9_later": _extract(
            paths["2018_q3"], phrase="Nine Months Ended", year=2017,
            net_label="Loss for the period and total comprehensive loss",
        ),
        "2018_h1": _extract(
            paths["2018_h1"], phrase="Six Months Ended", year=2018,
            net_label="Loss for the period and total comprehensive loss",
        ),
        "2018_fy": _extract(
            paths["2018_fy"], phrase="Year Ended", year=2018, fixed_column=1,
            net_label="Loss for the year and total comprehensive loss",
        ),
        "2018_q1_later": _extract(
            paths["2019_q1"], phrase="Three Months Ended", year=2018,
            net_label="Profit/(Loss) for the period and total comprehensive loss",
        ),
        "2018_m9_later": _extract(
            paths["2019_q3"], phrase="Nine Months Ended", year=2018,
            net_label="Profit/(Loss) for the period and total comprehensive loss",
        ),
        "2019_q1": _extract(
            paths["2019_q1"], phrase="Three Months Ended", year=2019,
            net_label="Profit/(Loss) for the period and total comprehensive loss",
        ),
        "2019_h1": _extract(
            paths["2019_h1"], phrase="Six Months Ended", year=2019,
            net_label="Loss for the year and total comprehensive loss",
        ),
        "2019_m9": _extract(
            paths["2019_q3"], phrase="Nine Months Ended", year=2019,
            net_label="Profit/(Loss) for the period and total comprehensive loss",
        ),
        "2019_fy": _extract(
            paths["2019_fy"], phrase="Year Ended", year=2019, fixed_column=1,
            net_label="Loss for the year and total comprehensive loss",
        ),
        "2019_q1_later": _extract(
            paths["2020_q1"], phrase="Three Months Ended", year=2019,
            net_label="Profit/(Loss) for the period and total comprehensive loss",
        ),
        "2019_h1_later": _extract(
            paths["2020_h1"], phrase="Six Months Ended", year=2019,
            net_label="Loss for the year and total comprehensive loss",
        ),
        "2019_m9_later": _extract(
            paths["2020_q3"], phrase="Nine Months Ended", year=2019,
            net_label="Loss for the period and total comprehensive loss",
        ),
        "2019_fy_later": _extract(
            paths["2020_fy"], phrase="Year Ended", year=2019, fixed_column=2,
            net_label="Loss for the year and total comprehensive loss",
        ),
        "2020_q1": _extract(
            paths["2020_q1"], phrase="Three Months Ended", year=2020,
            net_label="Profit/(Loss) for the period and total comprehensive loss",
        ),
        "2020_h1": _extract(
            paths["2020_h1"], phrase="Six Months Ended", year=2020,
            net_label="Loss for the year and total comprehensive loss",
        ),
        "2020_m9": _extract(
            paths["2020_q3"], phrase="Nine Months Ended", year=2020,
            net_label="Loss for the period and total comprehensive loss",
        ),
        "2020_fy": _extract(
            paths["2020_fy"], phrase="Year Ended", year=2020, fixed_column=1,
            net_label="Loss for the year and total comprehensive loss",
        ),
        "2021_q1": _extract(
            paths["2021_q1"], phrase="Three Months Ended", year=2021,
            net_label="Loss for the period and total comprehensive loss",
        ),
        "2021_h1": _extract(
            paths["2021_h1"], phrase="Six Months Ended", year=2021,
            net_label="Profit / (Loss) for the period",
        ),
        "2020_q1_usd_comparison": _extract(
            paths["2021_q1"], phrase="Three Months Ended", year=2020,
            net_label="Loss for the period and total comprehensive loss",
        ),
        "2020_h1_usd_comparison": _extract(
            paths["2021_h1"], phrase="unused", year=2020, fixed_column=8,
            net_label="Profit / (Loss) for the period",
        ),
        "2020_fy_usd_late": _extract(
            paths["2021_fy"], phrase="Year Ended", year=2020,
            net_label="Loss for the year",
        ),
        "2021_q3_later": _extract(
            paths["2022_q3"], phrase="Three Months Ended", year=2021,
            revenue_label="Collaboration revenue", net_label="Owners of the parent",
        ),
        "2021_m9_later": _extract(
            paths["2022_q3"], phrase="Nine Months Ended", year=2021,
            revenue_label="Collaboration revenue", net_label="Owners of the parent",
        ),
        "2021_fy": _extract(
            paths["2021_fy"], phrase="Year Ended", year=2021,
            net_label="Loss for the year",
        ),
    }
    if not _agree(observed["2019_h1"], observed["2019_h1_later"]):
        raise RuntimeError("ARGX 2019 H1 later comparator disagrees with original")
    if not _agree(observed["2019_q1"], observed["2019_q1_later"]):
        raise RuntimeError("ARGX 2019 Q1 later comparator disagrees with original")
    if not _agree(observed["2019_m9"], observed["2019_m9_later"]):
        raise RuntimeError("ARGX 2019 nine-month later comparator disagrees with original")
    if not _agree(observed["2019_fy"], observed["2019_fy_later"]):
        raise RuntimeError("ARGX 2019 annual later comparator disagrees with original")
    negative_source_text_checks = validate_negative_source_text(paths)
    audit_binding = validate_audit_binding(audit_path, expected_audit_sha256)
    negative_operands, rejected_derivations = build_negative_evidence(
        observed, sources
    )

    values: dict[tuple[int, int], dict[str, float]] = {}
    values[(2017, 1)] = observed["2017_q1_later"]
    values[(2017, 2)] = _subtract(observed["2017_h1_later"], values[(2017, 1)])
    values[(2017, 3)] = _subtract(observed["2017_m9_later"], observed["2017_h1_later"])
    values[(2017, 4)] = _subtract(observed["2017_fy"], observed["2017_m9_later"])
    values[(2018, 1)] = observed["2018_q1_later"]
    values[(2018, 2)] = _subtract(observed["2018_h1"], values[(2018, 1)])
    values[(2018, 3)] = _subtract(observed["2018_m9_later"], observed["2018_h1"])
    values[(2018, 4)] = _subtract(observed["2018_fy"], observed["2018_m9_later"])
    values[(2019, 1)] = observed["2019_q1"]
    values[(2019, 2)] = _subtract(observed["2019_h1"], values[(2019, 1)])
    values[(2019, 3)] = _subtract(observed["2019_m9"], observed["2019_h1"])
    values[(2019, 4)] = _subtract(observed["2019_fy"], observed["2019_m9"])
    values[(2020, 1)] = observed["2020_q1"]
    values[(2020, 2)] = _subtract(observed["2020_h1"], values[(2020, 1)])
    values[(2020, 3)] = _subtract(observed["2020_m9"], observed["2020_h1"])
    values[(2020, 4)] = _subtract(observed["2020_fy"], observed["2020_m9"])
    values[(2021, 1)] = observed["2021_q1"]
    values[(2021, 2)] = _subtract(observed["2021_h1"], values[(2021, 1)])
    values[(2021, 3)] = observed["2021_q3_later"]
    values[(2021, 4)] = _subtract(observed["2021_fy"], observed["2021_m9_later"])

    if not _agree(_sum([values[(2017, q)] for q in range(1, 5)]), observed["2017_fy"]):
        raise RuntimeError("ARGX 2017 derived quarters do not close to annual")
    if not _agree(_sum([values[(2018, q)] for q in range(1, 5)]), observed["2018_fy"]):
        raise RuntimeError("ARGX 2018 derived quarters do not close to annual")
    if not _agree(_sum([values[(2019, q)] for q in range(1, 5)]), observed["2019_fy"]):
        raise RuntimeError("ARGX 2019 derived quarters do not close to annual")
    if not _agree(_sum([values[(2020, q)] for q in range(1, 5)]), observed["2020_fy"]):
        raise RuntimeError("ARGX 2020 derived quarters do not close to annual")
    if not _agree(_sum([values[(2021, q)] for q in range(1, 5)]), observed["2021_fy"]):
        raise RuntimeError("ARGX 2021 audited quarters do not close to annual")
    if not _agree(_sum([values[(2021, q)] for q in range(1, 4)]), observed["2021_m9_later"]):
        raise RuntimeError("ARGX 2021 Q1-Q3 do not close to later nine-month comparator")

    evidence = {
        (2017, 1): ["2018_q1"],
        (2017, 2): ["2018_q1", "2018_h1"],
        (2017, 3): ["2018_h1", "2018_q3"],
        (2017, 4): ["2017_fy", "2018_q3"],
        (2018, 1): ["2019_q1"],
        (2018, 2): ["2018_h1", "2019_q1"],
        (2018, 3): ["2018_h1", "2019_q3"],
        (2018, 4): ["2018_fy", "2019_q3"],
        (2019, 1): ["2019_q1"], (2019, 2): ["2019_q1", "2019_h1"],
        (2019, 3): ["2019_h1", "2019_q3"],
        (2019, 4): ["2019_q3", "2019_fy"],
        (2020, 1): ["2020_q1"], (2020, 2): ["2020_q1", "2020_h1"],
        (2020, 3): ["2020_h1", "2020_q3"],
        (2020, 4): ["2020_q3", "2020_fy"],
        (2021, 1): ["2021_q1"], (2021, 2): ["2021_q1", "2021_h1"],
        (2021, 3): ["2022_q3"], (2021, 4): ["2021_fy", "2022_q3"],
    }
    fiscal_ends = {
        (year, quarter): pd.Timestamp(year=year, month=quarter * 3, day=1) + pd.offsets.MonthEnd(0)
        for year in range(2017, 2022) for quarter in range(1, 5)
    }
    audit_rows = []
    fact_rows = []
    recovered = []
    for key in sorted(values):
        year, quarter = key
        source_ids = evidence[key]
        available = max(pd.Timestamp(sources[source_id].available_date) for source_id in source_ids)
        fiscal_end = fiscal_ends[key]
        accepted = year <= 2020
        lag_days = int((available - fiscal_end).days)
        accessions = ";".join(sources[source_id].accession for source_id in source_ids)
        derivation = "direct_reported_quarter" if len(source_ids) == 1 else "cumulative_difference"
        audit_rows.append({
            "ticker": "ARGX", "fiscal_year": year, "fiscal_quarter": quarter,
            "fiscal_end": fiscal_end.strftime("%Y-%m-%d"),
            "source_available_date": available.strftime("%Y-%m-%d"),
            "availability_lag_days": lag_days, "currency": "EUR" if year <= 2020 else "USD",
            **values[key], "derivation": derivation,
            "source_ids": ";".join(source_ids), "accepted_for_research_quarterly": accepted,
            "exclusion_reason": "" if accepted else (
                "EUR_TO_USD_BOUNDARY_WITHOUT_COMPLETE_RESTATED_2020_QUARTERS;"
                "2021_Q3_Q4_NOT_KNOWN_UNTIL_2022_Q3"
            ),
        })
        if not accepted:
            continue
        recovered.append({
            "ticker": "ARGX", "fiscal_year": year, "fiscal_quarter": quarter,
            "fiscal_end": fiscal_end.strftime("%Y-%m-%d"),
            "available_date": available.strftime("%Y-%m-%d"),
            "availability_lag_days": lag_days, **values[key],
            "currency": "EUR", "derivation": derivation,
            "source_ids": source_ids, "accession": accessions,
        })
        for metric, concept in (("revenue", "Revenue"), ("net_income", "ProfitLoss")):
            fact_rows.append({
                "ticker": "ARGX", "fiscal_end": fiscal_end,
                "available_date": available, "metric": metric,
                "value": values[key][metric], "taxonomy": "ifrs-full",
                "concept": concept, "form": "6-K/20-F", "accession": accessions,
                "unit": "EUR", "source": "sec_filed_argx_ifrs_quarter_recovery",
                "source_archive": ";".join(paths[source_id].name for source_id in source_ids),
                "source_archive_sha256": ";".join(_sha256(paths[source_id]) for source_id in source_ids),
                "derivation_prior_accession": "",
            })

    quarters = pd.DataFrame(fact_rows).sort_values(["fiscal_end", "metric"])
    paired = quarters.groupby("fiscal_end")["metric"].nunique()
    longest = _longest_chain(paired.loc[paired.eq(2)].index.tolist())
    if longest != 16:
        raise RuntimeError(f"ARGX accepted quarterly chain is not continuous: {longest}/16")
    output_dir.mkdir(parents=True, exist_ok=True)
    quarters_path = output_dir / "strict_quarterly_facts.csv"
    quarter_audit_path = output_dir / "audited_quarter_matrix.csv"
    observations_path = output_dir / "unrecoverable_observations.csv"
    rejected_path = output_dir / "rejected_derivations.json"
    quarters.to_csv(quarters_path, index=False)
    pd.DataFrame(audit_rows).to_csv(quarter_audit_path, index=False)
    observations = resolve_unrecoverable_observations()
    observations.to_csv(observations_path, index=False)
    rejected_path.write_text(
        json.dumps(rejected_derivations, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    bindings = [{
        "source_id": row.source_id, "accession": row.accession,
        "available_date": pd.Timestamp(row.available_date).strftime("%Y-%m-%d"),
        "path": str(paths[row.source_id]), "sha256": _sha256(paths[row.source_id]),
        "source_url": row.source_url, "availability_evidence": "sec_filing_date",
    } for row in registry.itertuples(index=False)]
    report = {
        "schema_version": 2,
        "research_only": True,
        "point_in_time_proven": True,
        "negative_evidence_source_locked": True,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "promotion_eligible": False,
        "release_status": "BLOCKED",
        "formal_financials_modified": False,
        "shared_candidate_integrated": False,
        "ticker": TICKER,
        "cik": CIK,
        "accepted_currency": "EUR",
        "accepted_quarter_count": 16,
        "accepted_fact_count": len(quarters),
        "longest_continuous_paired_quarters": longest,
        "recovered_quarters": recovered,
        "excluded_audited_quarters": [
            row for row in audit_rows
            if not row["accepted_for_research_quarterly"]
        ],
        "blocked_signal": BLOCKED_SIGNAL,
        "blocked_observation_count": len(observations),
        "blocked_recovery_classification": (
            "UNRECOVERABLE_PRE_SIGNAL_USD_TTM_CHAIN_ABSENT"
        ),
        "blocked_reason": BLOCKED_REASON,
        "blocked_source_text_checks": negative_source_text_checks,
        "blocked_operands": negative_operands,
        "blocked_rejected_derivations": rejected_derivations,
        "audit_binding": audit_binding,
        "registry": {"path": str(registry_path), "sha256": _sha256(registry_path)},
        "reports": bindings,
        "outputs": {
            "quarters": {
                "path": str(quarters_path),
                "sha256": _sha256(quarters_path),
                "row_count": len(quarters),
            },
            "audit_matrix": {
                "path": str(quarter_audit_path),
                "sha256": _sha256(quarter_audit_path),
                "row_count": len(audit_rows),
            },
            "unrecoverable_observations": {
                "path": str(observations_path),
                "sha256": _sha256(observations_path),
                "row_count": len(observations),
            },
            "rejected_derivations": {
                "path": str(rejected_path),
                "sha256": _sha256(rejected_path),
                "row_count": len(rejected_derivations),
            },
        },
        "guardrail": (
            "Only the unit-consistent 2017-2020 EUR chain is integrated. The 2017-2018 "
            "single quarters derived from later direct comparative columns retain the "
            "later filing dates and are never backdated. Later SEC-filed "
            "comparators keep their actual filing dates. The pre-signal 2021H1 "
            "and comparative 2020H1 USD values are proven, but no exact pre-signal "
            "2020 full-year USD income statement exists to derive 2020H2, and "
            "the prior TTM lacks a homogeneous 2019 USD chain. Do not mix EUR and "
            "USD, estimate FX conversion, or backdate the 2022 annual comparison."
        ),
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report["manifest"] = str(manifest_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--audit-path", type=Path, default=AUDIT_PATH)
    parser.add_argument(
        "--expected-audit-sha256", default=EXPECTED_AUDIT_SHA256
    )
    args = parser.parse_args()
    result = run(
        registry_path=args.registry,
        output_dir=args.output_dir,
        audit_path=args.audit_path,
        expected_audit_sha256=args.expected_audit_sha256,
    )
    print(json.dumps({
        "manifest": result["manifest"],
        "accepted_quarter_count": result["accepted_quarter_count"],
        "excluded_audited_quarter_count": len(result["excluded_audited_quarters"]),
        "blocked_observation_count": result["blocked_observation_count"],
        "release_status": result["release_status"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
