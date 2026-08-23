#!/usr/bin/env python3
"""Recover a strict 2017-2020 SSRM quarterly chain from SEC-filed reports."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import urllib.request

import pandas as pd


DEFAULT_REGISTRY = Path("stocks_list_dir/nasdaq/ssrm_quarterly_reports.csv")
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/ssrm_sec_quarterly_reports_2017_2020"
)
USER_AGENT = "quant-stocks-research contact@example.com"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normal(value) -> str:
    return " ".join(str(value).replace("\xa0", " ").split()).casefold()


def _number(value) -> float | None:
    if pd.isna(value):
        return None
    text = str(value).strip().replace(",", "").replace("$", "")
    if not text or text.casefold() in {"nan", "—", "-"}:
        return None
    negative = text.startswith("(") or text.endswith(")")
    text = text.strip("() ")
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", text):
        return None
    number = float(text)
    return -abs(number) if negative else number


def _download(url: str, path: Path) -> None:
    if path.exists():
        return
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = response.read()
    if len(payload) < 10_000:
        raise RuntimeError(f"SEC filing payload unexpectedly small: {url}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def _net_label(label: str) -> bool:
    normalized = _normal(label)
    return normalized.startswith("net ") or normalized == "net (loss) income"


NET_LABEL_PRIORITY = (
    "net income",
    "net (loss) income",
    "net income (loss)",
    "net income and net income attributable to shareholders",
)


def _net_rows(table: pd.DataFrame) -> pd.DataFrame:
    first = table.iloc[:, 0].fillna("").astype(str).map(_normal)
    for label in NET_LABEL_PRIORITY:
        rows = table.loc[first.eq(label)]
        if len(rows):
            return rows
    return table.loc[first.map(_net_label)]


def _statement_table(path: Path, phrase: str | None) -> pd.DataFrame:
    candidates = []
    for table in pd.read_html(io.BytesIO(path.read_bytes())):
        first = table.iloc[:, 0].fillna("").astype(str)
        has_revenue = first.map(_normal).eq("revenue").any()
        has_net = first.map(_net_label).any()
        header = " ".join(map(str, table.head(6).to_numpy().ravel()))
        if has_revenue and has_net and (phrase is None or phrase.casefold() in header.casefold()):
            candidates.append(table)
    if not candidates:
        raise ValueError(
            f"expected an SSRM income statement in {path}, found none"
        )
    return max(candidates, key=lambda table: (len(table.columns), len(table)))


def _columns(table: pd.DataFrame, year: int, phrase: str | None) -> list:
    selected = []
    for column in table.columns:
        values = [_normal(value) for value in table.head(6)[column]]
        has_year = str(year) in values or f"{year}.0" in values
        has_phrase = phrase is None or any(phrase.casefold() in value for value in values)
        if has_year and has_phrase:
            selected.append(column)
    if not selected:
        raise ValueError(f"no SSRM {year} {phrase or 'annual'} columns")
    return selected


def _row_value(table: pd.DataFrame, columns: list, metric: str) -> float:
    first = table.iloc[:, 0].fillna("").astype(str)
    rows = (
        table.loc[first.map(_normal).eq("revenue")]
        if metric == "revenue"
        else _net_rows(table)
    )
    if len(rows) != 1:
        raise ValueError(f"expected one SSRM {metric} row, found {len(rows)}")
    values = {
        value for column in columns
        if (value := _number(rows.iloc[0][column])) is not None
    }
    if len(values) != 1:
        raise ValueError(f"ambiguous SSRM {metric} values: {sorted(values)}")
    return round(values.pop() * 1_000.0, 2)


def _extract(path: Path, year: int, phrase: str | None) -> dict[str, float]:
    table = _statement_table(path, phrase)
    columns = _columns(table, year, phrase)
    return {
        metric: _row_value(table, columns, metric)
        for metric in ("revenue", "net_income")
    }


def run(*, registry_path: Path = DEFAULT_REGISTRY, output_dir: Path = DEFAULT_OUTPUT_DIR) -> dict:
    registry = pd.read_csv(registry_path, dtype={"accession": str})
    expected = {(year, quarter) for year in range(2017, 2021) for quarter in range(1, 5)}
    observed = set(zip(registry["year"], registry["quarter"]))
    if observed != expected or len(registry) != 16 or set(registry["ticker"]) != {"SSRM"}:
        raise ValueError("SSRM registry must contain exactly 2017Q1-2020Q4")
    values = {}
    bindings = []
    for row in registry.itertuples(index=False):
        path = Path(row.local_path)
        _download(row.source_url, path)
        phrase = "three months ended" if row.mode == "direct_quarter" else None
        extracted = _extract(path, int(row.year), phrase)
        if row.mode == "annual_minus_q1_q3":
            extracted = {
                metric: round(
                    extracted[metric]
                    - sum(values[(int(row.year), quarter)][metric] for quarter in (1, 2, 3)),
                    2,
                )
                for metric in ("revenue", "net_income")
            }
        values[(int(row.year), int(row.quarter))] = extracted
        bindings.append({
            "year": int(row.year), "quarter": int(row.quarter),
            "available_date": row.available_date, "accession": row.accession,
            "mode": row.mode, "source_url": row.source_url,
            "path": str(path), "sha256": _sha256(path),
        })
    fact_rows = []
    for row in registry.itertuples(index=False):
        key = (int(row.year), int(row.quarter))
        fiscal_end = pd.Timestamp(year=key[0], month=key[1] * 3, day=1) + pd.offsets.MonthEnd(0)
        for metric, concept in (("revenue", "Revenue"), ("net_income", "NetIncomeLoss")):
            fact_rows.append({
                "ticker": "SSRM", "fiscal_end": fiscal_end,
                "available_date": pd.Timestamp(row.available_date), "metric": metric,
                "value": values[key][metric], "taxonomy": "ifrs-full",
                "concept": concept, "form": "6-K", "accession": row.accession,
                "unit": "USD", "source": "sec_filed_ssrm_quarterly_report",
                "source_archive": Path(row.local_path).name,
                "source_archive_sha256": _sha256(Path(row.local_path)),
                "derivation_prior_accession": "" if row.mode == "direct_quarter" else "Q1-Q3",
            })
    quarters = pd.DataFrame(fact_rows).sort_values(["fiscal_end", "metric"])
    if quarters.groupby("fiscal_end")["metric"].nunique().eq(2).sum() != 16:
        raise RuntimeError("SSRM recovered chain is not 16 paired quarters")
    output_dir.mkdir(parents=True, exist_ok=True)
    quarters_path = output_dir / "strict_quarterly_facts.csv"
    quarters.to_csv(quarters_path, index=False)
    report = {
        "schema_version": 1, "research_only": True, "point_in_time_proven": True,
        "promotion_eligible": False, "release_status": "BLOCKED", "ticker": "SSRM",
        "accepted_quarter_count": 16,
        "recovered_quarters": [
            {
                "ticker": "SSRM",
                "fiscal_end": (
                    pd.Timestamp(year=year, month=quarter * 3, day=1)
                    + pd.offsets.MonthEnd(0)
                ).strftime("%Y-%m-%d"),
                "available_date": str(
                    registry.loc[
                        registry["year"].eq(year)
                        & registry["quarter"].eq(quarter),
                        "available_date",
                    ].iloc[0]
                ),
                "revenue": values[(year, quarter)]["revenue"],
                "net_income": values[(year, quarter)]["net_income"],
            }
            for year, quarter in sorted(values)
        ],
        "registry": {"path": str(registry_path), "sha256": _sha256(registry_path)},
        "reports": bindings,
        "outputs": {"quarters": {"path": str(quarters_path), "sha256": _sha256(quarters_path)}},
        "guardrail": (
            "Direct three-month values retain SEC filing dates. Q4 is derived only "
            "from the same year's filed annual total less already filed Q1-Q3; no "
            "fact is backdated and formal fundamentals are not modified."
        ),
    }
    manifest = output_dir / "manifest.json"
    manifest.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["manifest"] = str(manifest)
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
        "release_status": result["release_status"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
