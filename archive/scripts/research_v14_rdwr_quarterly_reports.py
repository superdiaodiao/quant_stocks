#!/usr/bin/env python3
"""Recover strict RDWR 2016Q4-2021Q3 facts from SEC-filed earnings releases."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess

import pandas as pd


DEFAULT_REGISTRY = Path("stocks_list_dir/nasdaq/rdwr_quarterly_reports.csv")
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/rdwr_sec_quarterly_reports_2016_2021"
)
USER_AGENT = "quant-stocks-research contact@example.com"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normal(value) -> str:
    return " ".join(
        str(value).replace("\xa0", " ").replace("\u200b", " ").split()
    ).casefold()


def _number(value) -> float | None:
    if pd.isna(value):
        return None
    text = str(value).strip().replace(",", "").replace("$", "")
    if not text or text.casefold() in {"nan", "—", "-", ")"}:
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
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    subprocess.run(
        [
            "curl", "--fail", "--silent", "--show-error", "--max-time", "60",
            "--retry", "3", "--retry-delay", "3", "-A", USER_AGENT,
            "-o", str(temporary), url,
        ],
        check=True,
    )
    if temporary.stat().st_size < 20_000:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"RDWR SEC filing payload unexpectedly small: {url}")
    os.replace(temporary, path)


def _metric_rows(table: pd.DataFrame, metric: str) -> pd.DataFrame:
    first = table.iloc[:, 0].fillna("").astype(str).map(_normal)
    if metric == "revenue":
        return table.loc[first.isin({"revenue", "revenues"})]
    return table.loc[first.isin({"net income", "net loss", "net income (loss)"})]


def _statement(path: Path) -> pd.DataFrame:
    candidates = []
    for table in pd.read_html(io.BytesIO(path.read_bytes())):
        header = _normal(" ".join(map(str, table.head(8).to_numpy().ravel())))
        if (
            "three months ended" in header
            and len(_metric_rows(table, "revenue"))
            and len(_metric_rows(table, "net_income"))
        ):
            candidates.append(table)
    if not candidates:
        raise ValueError(f"no RDWR three-month income statement in {path}")
    return min(candidates, key=lambda table: (len(table), len(table.columns)))


def _extract(path: Path, year: int) -> dict[str, float]:
    table = _statement(path)
    columns = []
    for column in table.columns:
        header = [_normal(value) for value in table.head(8)[column]]
        if (
            any(str(year) in value for value in header)
            and any("three months ended" in value for value in header)
        ):
            columns.append(column)
    if not columns:
        raise ValueError(f"no RDWR {year} three-month column in {path}")
    result = {}
    for metric in ("revenue", "net_income"):
        rows = _metric_rows(table, metric)
        candidates = {
            value
            for _, metric_row in rows.iterrows()
            for column in columns
            if (value := _number(metric_row[column])) is not None
        }
        if len(candidates) != 1:
            raise ValueError(f"ambiguous RDWR {year} {metric}: {candidates}")
        result[metric] = round(candidates.pop() * 1_000.0, 2)
    return result


def run(
    *, registry_path: Path = DEFAULT_REGISTRY, output_dir: Path = DEFAULT_OUTPUT_DIR
) -> dict:
    registry = pd.read_csv(registry_path, dtype={"accession": str})
    expected = {(2016, 4)} | {
        (year, quarter) for year in range(2017, 2021) for quarter in range(1, 5)
    } | {(2021, quarter) for quarter in range(1, 4)}
    if (
        set(zip(registry["year"], registry["quarter"])) != expected
        or len(registry) != 20
        or set(registry["ticker"]) != {"RDWR"}
    ):
        raise ValueError("RDWR registry must contain exactly 2016Q4-2021Q3")

    values, bindings = {}, []
    for row in registry.itertuples(index=False):
        path = Path(row.local_path)
        _download(row.source_url, path)
        key = (int(row.year), int(row.quarter))
        values[key] = _extract(path, key[0])
        bindings.append(
            {
                "year": key[0],
                "quarter": key[1],
                "available_date": row.available_date,
                "accession": row.accession,
                "source_url": row.source_url,
                "path": str(path),
                "sha256": _sha256(path),
            }
        )

    facts, recovered = [], []
    for row in registry.itertuples(index=False):
        key = (int(row.year), int(row.quarter))
        fiscal_end = (
            pd.Timestamp(year=key[0], month=key[1] * 3, day=1)
            + pd.offsets.MonthEnd(0)
        )
        recovered.append(
            {
                "ticker": "RDWR",
                "fiscal_end": fiscal_end.strftime("%Y-%m-%d"),
                "available_date": row.available_date,
                **values[key],
            }
        )
        for metric, concept in (("revenue", "Revenues"), ("net_income", "NetIncomeLoss")):
            facts.append(
                {
                    "ticker": "RDWR",
                    "fiscal_end": fiscal_end,
                    "available_date": pd.Timestamp(row.available_date),
                    "metric": metric,
                    "value": values[key][metric],
                    "taxonomy": "us-gaap",
                    "concept": concept,
                    "form": "6-K",
                    "accession": row.accession,
                    "unit": "USD",
                    "source": "sec_filed_rdwr_quarterly_earnings",
                    "source_archive": Path(row.local_path).name,
                    "source_archive_sha256": _sha256(Path(row.local_path)),
                    "derivation": "direct_three_month_gaap",
                }
            )
    quarters = pd.DataFrame(facts).sort_values(["fiscal_end", "metric"])
    if quarters.groupby("fiscal_end")["metric"].nunique().eq(2).sum() != 20:
        raise RuntimeError("RDWR recovered chain is not 20 paired quarters")

    output_dir.mkdir(parents=True, exist_ok=True)
    quarters_path = output_dir / "strict_quarterly_facts.csv"
    quarters.to_csv(quarters_path, index=False)
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "promotion_eligible": False,
        "release_status": "BLOCKED",
        "ticker": "RDWR",
        "accepted_quarter_count": 20,
        "recovered_quarters": recovered,
        "registry": {"path": str(registry_path), "sha256": _sha256(registry_path)},
        "reports": bindings,
        "outputs": {
            "quarters": {"path": str(quarters_path), "sha256": _sha256(quarters_path)}
        },
        "guardrail": (
            "Only direct three-month GAAP USD facts in SEC-filed RDWR earnings "
            "releases are used. Filing dates are retained and formal fundamentals "
            "are unchanged."
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
    print(
        json.dumps(
            {
                "manifest": result["manifest"],
                "accepted_quarter_count": result["accepted_quarter_count"],
                "release_status": result["release_status"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
