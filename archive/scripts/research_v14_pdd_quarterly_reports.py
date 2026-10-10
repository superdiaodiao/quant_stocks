#!/usr/bin/env python3
"""Recover strict PDD 2018Q3-2020Q4 quarters from SEC-filed earnings reports."""

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

DEFAULT_REGISTRY = Path("stocks_list_dir/nasdaq/pdd_quarterly_reports.csv")
DEFAULT_OUTPUT_DIR = Path("output/research_only/v14/pdd_sec_quarterly_reports_2018_2020")
USER_AGENT = "quant-stocks-research contact@example.com"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normal(value) -> str:
    return " ".join(str(value).replace("\xa0", " ").replace("\u200b", " ").split()).casefold()


def _number(value) -> float | None:
    if pd.isna(value):
        return None
    text = str(value).strip().replace(",", "").replace("RMB", "")
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
    if len(payload) < 50_000:
        raise RuntimeError(f"PDD SEC filing payload unexpectedly small: {url}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def _metric_rows(table: pd.DataFrame, metric: str) -> pd.DataFrame:
    first = table.iloc[:, 0].fillna("").astype(str).map(_normal)
    if metric == "revenue":
        for label in ("total revenues", "revenues"):
            rows = table.loc[first.eq(label)]
            if len(rows):
                return rows
    else:
        for label in ("net loss", "net income/(loss)", "net income (loss)", "net income"):
            rows = table.loc[first.eq(label)]
            if len(rows):
                return rows
    return table.iloc[0:0]


def _statement(path: Path, phrase: str) -> pd.DataFrame:
    candidates = []
    for table in pd.read_html(io.BytesIO(path.read_bytes())):
        header = " ".join(map(str, table.head(7).to_numpy().ravel()))
        if (
            phrase.casefold() in _normal(header)
            and len(_metric_rows(table, "revenue"))
            and len(_metric_rows(table, "net_income"))
        ):
            candidates.append(table)
    if not candidates:
        raise ValueError(f"no PDD {phrase} statement in {path}")
    return max(candidates, key=lambda table: len(table))


def _extract(path: Path, year: int, phrase: str) -> dict[str, float]:
    table = _statement(path, phrase)
    columns = []
    for column in table.columns:
        header = [_normal(value) for value in table.head(7)[column]]
        if (str(year) in header or f"{year}.0" in header) and any(
            phrase.casefold() in value for value in header
        ) and "us$" not in header:
            columns.append(column)
    if not columns:
        raise ValueError(f"no PDD RMB {year} {phrase} column in {path}")
    result = {}
    for metric in ("revenue", "net_income"):
        rows = _metric_rows(table, metric)
        if len(rows) != 1:
            raise ValueError(f"ambiguous PDD {metric} row in {path}")
        values = {
            value for column in columns
            if (value := _number(rows.iloc[0][column])) is not None
        }
        if len(values) != 1:
            raise ValueError(f"ambiguous PDD {metric} values in {path}: {values}")
        result[metric] = round(values.pop() * 1_000.0, 2)
    return result


def run(*, registry_path: Path = DEFAULT_REGISTRY, output_dir: Path = DEFAULT_OUTPUT_DIR) -> dict:
    registry = pd.read_csv(registry_path, dtype={"accession": str})
    expected = {(2018, 3), (2018, 4)} | {
        (year, quarter) for year in (2019, 2020) for quarter in range(1, 5)
    }
    if set(zip(registry.year, registry.quarter)) != expected or len(registry) != 10:
        raise ValueError("PDD registry must contain exactly 2018Q3-2020Q4")
    values, bindings = {}, []
    for row in registry.itertuples(index=False):
        path = Path(row.local_path)
        _download(row.source_url, path)
        phrase = "nine months ended" if row.mode == "ytd_minus_q1_q2" else "three months ended"
        extracted = _extract(path, int(row.year), phrase)
        if row.mode == "ytd_minus_q1_q2":
            extracted = {
                metric: round(
                    extracted[metric]
                    - values[(int(row.year), 1)][metric]
                    - values[(int(row.year), 2)][metric], 2
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
    fact_rows, recovered = [], []
    for row in registry.itertuples(index=False):
        key = (int(row.year), int(row.quarter))
        fiscal_end = pd.Timestamp(year=key[0], month=key[1] * 3, day=1) + pd.offsets.MonthEnd(0)
        recovered.append({
            "ticker": "PDD", "fiscal_end": fiscal_end.strftime("%Y-%m-%d"),
            "available_date": row.available_date, **values[key],
        })
        for metric, concept in (("revenue", "Revenue"), ("net_income", "ProfitLoss")):
            fact_rows.append({
                "ticker": "PDD", "fiscal_end": fiscal_end,
                "available_date": pd.Timestamp(row.available_date), "metric": metric,
                "value": values[key][metric], "taxonomy": "ifrs-full",
                "concept": concept, "form": "6-K", "accession": row.accession,
                "unit": "CNY", "source": "sec_filed_pdd_quarterly_results",
                "source_archive": Path(row.local_path).name,
                "source_archive_sha256": _sha256(Path(row.local_path)),
                "derivation": row.mode,
            })
    quarters = pd.DataFrame(fact_rows).sort_values(["fiscal_end", "metric"])
    if quarters.groupby("fiscal_end").metric.nunique().eq(2).sum() != 10:
        raise RuntimeError("PDD recovered chain is not 10 paired quarters")
    output_dir.mkdir(parents=True, exist_ok=True)
    quarters_path = output_dir / "strict_quarterly_facts.csv"
    quarters.to_csv(quarters_path, index=False)
    report = {
        "schema_version": 1, "research_only": True, "point_in_time_proven": True,
        "promotion_eligible": False, "release_status": "BLOCKED", "ticker": "PDD",
        "accepted_quarter_count": 10, "recovered_quarters": recovered,
        "registry": {"path": str(registry_path), "sha256": _sha256(registry_path)},
        "reports": bindings,
        "outputs": {"quarters": {"path": str(quarters_path), "sha256": _sha256(quarters_path)}},
        "guardrail": (
            "Only RMB facts explicitly filed in each earnings 6-K are used. "
            "2020Q3 is nine-month YTD less already filed Q1-Q2; comparative "
            "columns are not backdated and formal fundamentals are unchanged."
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
