#!/usr/bin/env python3
"""Recover ARGX quarters without crossing its 2021 EUR-to-USD boundary."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd

from scripts.research_v14_sec_filing_exhibit_financials import (
    _parse_accounting_number,
)
from scripts.research_v14_team_sec_quarterly_filings import _longest_chain


DEFAULT_REGISTRY = Path("stocks_list_dir/nasdaq/argx_quarterly_reports.csv")
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/argx_sec_quarterly_reports_2019_2021"
)
METRICS = ("revenue", "net_income")


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


def run(
    *,
    registry_path: Path = DEFAULT_REGISTRY,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
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
    sources = _source_rows(registry)
    paths = {source_id: Path(row.local_path) for source_id, row in sources.items()}
    missing = [str(path) for path in paths.values() if not path.exists()]
    if missing:
        raise FileNotFoundError("missing ARGX SEC archives: " + ", ".join(missing))

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
    audit_path = output_dir / "audited_quarter_matrix.csv"
    quarters.to_csv(quarters_path, index=False)
    pd.DataFrame(audit_rows).to_csv(audit_path, index=False)
    bindings = [{
        "source_id": row.source_id, "accession": row.accession,
        "available_date": pd.Timestamp(row.available_date).strftime("%Y-%m-%d"),
        "path": str(paths[row.source_id]), "sha256": _sha256(paths[row.source_id]),
        "source_url": row.source_url, "availability_evidence": "sec_filing_date",
    } for row in registry.itertuples(index=False)]
    report = {
        "schema_version": 1, "research_only": True, "point_in_time_proven": True,
        "promotion_eligible": False, "release_status": "BLOCKED", "ticker": "ARGX",
        "accepted_currency": "EUR", "accepted_quarter_count": 16,
        "longest_continuous_paired_quarters": longest,
        "recovered_quarters": recovered,
        "excluded_audited_quarters": [row for row in audit_rows if not row["accepted_for_research_quarterly"]],
        "registry": {"path": str(registry_path), "sha256": _sha256(registry_path)},
        "reports": bindings,
        "outputs": {
            "quarters": {"path": str(quarters_path), "sha256": _sha256(quarters_path)},
            "audit_matrix": {"path": str(audit_path), "sha256": _sha256(audit_path)},
        },
        "guardrail": (
            "Only the unit-consistent 2017-2020 EUR chain is integrated. The 2017-2018 "
            "single quarters derived from later direct comparative columns retain the "
            "later filing dates and are never backdated. Later SEC-filed "
            "comparators keep their actual filing dates. Audited 2021 USD quarters are excluded "
            "because ARGX changed reporting currency and complete restated 2020 USD quarters are "
            "not proven; mixing EUR and USD would create false TTM growth."
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
    args = parser.parse_args()
    result = run(registry_path=args.registry, output_dir=args.output_dir)
    print(json.dumps({
        "manifest": result["manifest"],
        "accepted_quarter_count": result["accepted_quarter_count"],
        "excluded_audited_quarter_count": len(result["excluded_audited_quarters"]),
        "release_status": result["release_status"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
