#!/usr/bin/env python3
"""Recover AUPH 2017-2019 IFRS quarters from contemporaneous SEC filings."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pandas as pd


REGISTRY = Path("stocks_list_dir/nasdaq/auph_quarterly_reports.csv")
OUTPUT_DIR = Path("output/research_only/v14/auph_quarterly_reports_2017q1_2019q4")
REVENUE_PREFIXES = (
    "licensing revenue",
    "research and development revenue",
    "contract revenue",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normal(value: object) -> str:
    return " ".join(str(value).replace("\xa0", " ").split()).casefold()


def _first_accounting_value(row: pd.Series) -> float:
    for value in row.iloc[1:]:
        text = str(value).strip().replace(",", "").replace("$", "")
        if text in {"—", "–", "-"}:
            return 0.0
        match = re.search(r"-?\d+(?:\.\d+)?", text)
        if match is None:
            continue
        result = float(match.group())
        return -abs(result) if "(" in text or result < 0 else result
    raise ValueError(f"no accounting value in row {row.iloc[0]!r}")


def extract_statement(path: Path, *, annual: bool = False) -> dict[str, float]:
    candidates = []
    for table in pd.read_html(path):
        labels = table.iloc[:, 0].map(_normal)
        revenue_rows = pd.Series(False, index=labels.index)
        for prefix in REVENUE_PREFIXES:
            revenue_rows |= labels.str.startswith(prefix)
        preferred_net_label = "net loss for the year" if annual else "net loss for the period"
        fallback_net_label = (
            "net loss and comprehensive loss for the year"
            if annual
            else "net loss and comprehensive loss for the period"
        )
        net_rows = labels.eq(preferred_net_label)
        if not net_rows.any():
            net_rows = labels.eq(fallback_net_label)
        if not revenue_rows.any() or net_rows.sum() != 1:
            continue
        revenue = sum(
            _first_accounting_value(table.loc[index])
            for index in table.index[revenue_rows]
        )
        net_income = _first_accounting_value(table.loc[net_rows].iloc[0])
        candidates.append({
            "revenue": revenue * 1_000.0,
            "net_income": net_income * 1_000.0,
        })
    if len(candidates) != 1:
        raise ValueError(f"expected one AUPH income statement in {path}, found {len(candidates)}")
    return candidates[0]


def _subtract(left: dict[str, float], *rights: dict[str, float]) -> dict[str, float]:
    return {
        metric: left[metric] - sum(right[metric] for right in rights)
        for metric in ("revenue", "net_income")
    }


def run(registry_path: Path = REGISTRY, output_dir: Path = OUTPUT_DIR) -> dict:
    registry = pd.read_csv(registry_path, dtype={"cik": str, "accession": str})
    expected = {
        f"{year}_{period}"
        for year in range(2017, 2020)
        for period in ("q1", "q2", "q3", "fy")
    }
    if set(registry["ticker"]) != {"AUPH"} or set(registry["cik"]) != {"1600620"}:
        raise ValueError("AUPH registry contains another issuer")
    if len(registry) != 12 or set(registry["source_id"]) != expected:
        raise ValueError("AUPH registry must contain 2017-2019 Q1-Q3 and annual filings")

    sources = {row.source_id: row for row in registry.itertuples(index=False)}
    paths = {source_id: Path(row.local_path) for source_id, row in sources.items()}
    missing = [str(path) for path in paths.values() if not path.exists()]
    if missing:
        raise FileNotFoundError("missing AUPH SEC exhibits: " + ", ".join(missing))

    values: dict[tuple[int, int], dict[str, float]] = {}
    evidence: dict[tuple[int, int], list[str]] = {}
    annual_values = {}
    for year in range(2017, 2020):
        for quarter in range(1, 4):
            source_id = f"{year}_q{quarter}"
            values[(year, quarter)] = extract_statement(paths[source_id])
            evidence[(year, quarter)] = [source_id]
        annual_source = f"{year}_fy"
        annual_values[year] = extract_statement(paths[annual_source], annual=True)
        values[(year, 4)] = _subtract(
            annual_values[year], *(values[(year, quarter)] for quarter in range(1, 4))
        )
        evidence[(year, 4)] = [f"{year}_q3", annual_source]
        closed = {
            metric: sum(values[(year, quarter)][metric] for quarter in range(1, 5))
            for metric in ("revenue", "net_income")
        }
        if closed != annual_values[year]:
            raise RuntimeError(f"AUPH {year} quarters do not close to annual statement")

    facts = []
    recovered = []
    for (year, quarter), quarter_values in sorted(values.items()):
        source_ids = evidence[(year, quarter)]
        available_date = max(
            pd.Timestamp(sources[source_id].available_date) for source_id in source_ids
        )
        fiscal_end = pd.Period(f"{year}Q{quarter}", freq="Q-DEC").end_time.normalize()
        accessions = ";".join(sources[source_id].accession for source_id in source_ids)
        derivation = "direct_reported_quarter" if quarter < 4 else "annual_less_first_nine_months"
        recovered.append({
            "ticker": "AUPH",
            "fiscal_end": fiscal_end.date().isoformat(),
            "available_date": available_date.date().isoformat(),
            **quarter_values,
            "derivation": derivation,
            "source_ids": source_ids,
        })
        for metric, value in quarter_values.items():
            facts.append({
                "ticker": "AUPH",
                "metric": metric,
                "fiscal_end": fiscal_end.date().isoformat(),
                "available_date": available_date.date().isoformat(),
                "value": value,
                "unit": "USD",
                "taxonomy": "AUPH_IFRS_SEC_6K_40F",
                "concept": f"sec_filed_auph_ifrs_{metric}",
                "form": "6-K" if quarter < 4 else "6-K/40-F",
                "accession": accessions,
                "source_url": ";".join(sources[source_id].source_url for source_id in source_ids),
            })

    frame = pd.DataFrame(facts).sort_values(["fiscal_end", "metric"]).reset_index(drop=True)
    if len(frame) != 24 or frame[["ticker", "metric", "fiscal_end"]].duplicated().any():
        raise RuntimeError("AUPH recovery must contain exactly 12 paired quarters")
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    frame.to_csv(facts_path, index=False)
    bindings = [
        {**row._asdict(), "sha256": _sha256(paths[row.source_id])}
        for row in registry.itertuples(index=False)
    ]
    manifest = {
        "schema_version": 1,
        "research_only": True,
        "ticker": "AUPH",
        "point_in_time_proven": True,
        "parameters_frozen": False,
        "accepted_accounting_basis": "IFRS",
        "accepted_quarter_count": 12,
        "fact_count": 24,
        "filing_sources": bindings,
        "recovered_quarters": recovered,
        "annual_closure_values": annual_values,
        "outputs": {
            "strict_quarterly_facts": {"path": str(facts_path), "sha256": _sha256(facts_path)}
        },
        "guardrail": (
            "Only the continuous 2017-2019 IFRS chain is accepted. Contemporaneous 2020 "
            "IFRS quarters are excluded because AUPH became a US domestic issuer and its "
            "2021 US-GAAP filings retrospectively present materially different 2020 values; "
            "mixing those accounting bases would create false growth."
        ),
        "release_status": "BLOCKED",
        "promotion_eligible": False,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, sort_keys=True))
