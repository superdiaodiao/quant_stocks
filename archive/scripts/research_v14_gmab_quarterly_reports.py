#!/usr/bin/env python3
"""Recover strict GMAB 2018Q1-2021Q3 facts from SEC-filed reports."""

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


DEFAULT_REGISTRY = Path("stocks_list_dir/nasdaq/gmab_quarterly_reports.csv")
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/gmab_sec_quarterly_reports_2018_2021"
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
    text = str(value).strip().replace(",", "")
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
    if temporary.stat().st_size < 100_000:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"GMAB SEC filing payload unexpectedly small: {url}")
    os.replace(temporary, path)


def _metric_rows(table: pd.DataFrame, metric: str) -> pd.DataFrame:
    first = table.iloc[:, 0].fillna("").astype(str).map(_normal)
    label = "revenue" if metric == "revenue" else "net result"
    return table.loc[first.eq(label)]


def _statement(path: Path, period: str) -> pd.DataFrame:
    candidates = []
    for table in pd.read_html(io.BytesIO(path.read_bytes())):
        flat = _normal(" ".join(map(str, table.to_numpy().ravel())))
        if (
            period == "annual"
            and "primary income statement" in flat
            and "revenue" in flat
            and "net result" in flat
        ):
            candidates.append(table)
            continue
        if not len(_metric_rows(table, "revenue")) or not len(
            _metric_rows(table, "net_income")
        ):
            continue
        header = _normal(" ".join(map(str, table.head(6).to_numpy().ravel())))
        if period == "annual":
            if "dk k million" in header or "dkk million" in header:
                candidates.append(table)
        elif period in header:
            candidates.append(table)
    if not candidates:
        raise ValueError(f"no GMAB {period} income statement in {path}")
    if period != "annual":
        # Prefer the dedicated primary statement over the compact five-period
        # summary, which contains several columns for the same year.
        return min(candidates, key=lambda table: (len(table.columns), -len(table)))
    return max(candidates, key=lambda table: (len(table), len(table.columns)))


def _period_phrase(period: str) -> str:
    return {
        "q1": "1st quarter of",
        "q2": "2nd quarter of",
        "q3": "3rd quarter of",
        "h1": "6 months ended",
        "annual": "annual",
    }[period]


def _columns(table: pd.DataFrame, year: int, period: str) -> list:
    selected = []
    for column in table.columns:
        header = [_normal(value) for value in table.head(6)[column]]
        has_year = any(str(year) in value for value in header)
        # The statement has already been selected by period. In many SEC HTML
        # tables the period label is in the cell immediately left of the year,
        # so requiring both on the numeric column would reject a valid table.
        if has_year:
            selected.append(column)
    if not selected:
        raise ValueError(f"no GMAB {year} {period} column")
    return selected


def _annual_from_flat_table(table: pd.DataFrame, year: int) -> dict[str, float]:
    text = " ".join(table.fillna("").astype(str).to_numpy().ravel())
    year_match = re.search(r"Note\s+(\d{4})\s+(\d{4})", text, re.I)
    if not year_match:
        raise ValueError("GMAB annual statement has no year header")
    years = [int(value) for value in year_match.groups()]
    if year not in years:
        raise ValueError(f"GMAB annual statement has no {year} column")
    position = years.index(year) + 1
    result = {}
    for metric, pattern in (
        ("revenue", r"Revenue\s+([\d,]+)\s+([\d,]+)"),
        ("net_income", r"Net result(?! before tax)\s+([\d,]+)\s+([\d,]+)"),
    ):
        match = re.search(pattern, text, re.I)
        if not match:
            raise ValueError(f"GMAB annual statement lacks {metric}")
        result[metric] = float(match.group(position).replace(",", ""))
    return result


def _extract(path: Path, year: int, period: str, scale: float) -> dict[str, float]:
    table = _statement(path, _period_phrase(period))
    if period == "annual" and len(table.columns) <= 2:
        values = _annual_from_flat_table(table, year)
    else:
        columns = _columns(table, year, period)
        values = {}
        for metric in ("revenue", "net_income"):
            rows = _metric_rows(table, metric)
            candidates = {
                value
                for _, metric_row in rows.iterrows()
                for column in columns
                if (value := _number(metric_row[column])) is not None
            }
            if len(candidates) != 1:
                raise ValueError(
                    f"ambiguous GMAB {year} {period} {metric}: {candidates}"
                )
            values[metric] = candidates.pop()
    return {metric: round(value * scale, 2) for metric, value in values.items()}


def run(
    *, registry_path: Path = DEFAULT_REGISTRY, output_dir: Path = DEFAULT_OUTPUT_DIR
) -> dict:
    registry = pd.read_csv(registry_path, dtype={"accession": str})
    expected = {(2018, quarter) for quarter in range(1, 5)} | {
        (year, quarter)
        for year in (2019, 2020)
        for quarter in range(1, 5)
    } | {(2021, quarter) for quarter in range(1, 4)}
    observed = set(zip(registry["year"], registry["quarter"]))
    if observed != expected or len(registry) != 15 or set(registry["ticker"]) != {"GMAB"}:
        raise ValueError("GMAB registry must contain exactly 2018Q1-2021Q3")

    values: dict[tuple[int, int], dict[str, float]] = {}
    bindings = []
    for row in registry.itertuples(index=False):
        path = Path(row.local_path)
        _download(row.source_url, path)
        source_year = int(row.year)
        extracted = _extract(path, source_year, row.period, float(row.scale))
        mode = str(row.mode)
        if "ytd_minus_q2" in mode:
            q2_period = "q2"
            q2 = _extract(path, source_year, q2_period, float(row.scale))
            extracted = {
                metric: round(extracted[metric] - q2[metric], 2)
                for metric in ("revenue", "net_income")
            }
        elif "annual_minus_q1_q3" in mode:
            extracted = {
                metric: round(
                    extracted[metric]
                    - sum(
                        values[(source_year, quarter)][metric]
                        for quarter in (1, 2, 3)
                    ),
                    2,
                )
                for metric in ("revenue", "net_income")
            }
        values[(int(row.year), int(row.quarter))] = extracted
        bindings.append(
            {
                "year": int(row.year),
                "quarter": int(row.quarter),
                "available_date": row.available_date,
                "accession": row.accession,
                "mode": row.mode,
                "source_url": row.source_url,
                "path": str(path),
                "sha256": _sha256(path),
            }
        )

    fact_rows, recovered = [], []
    for row in registry.itertuples(index=False):
        key = (int(row.year), int(row.quarter))
        fiscal_end = (
            pd.Timestamp(year=key[0], month=key[1] * 3, day=1)
            + pd.offsets.MonthEnd(0)
        )
        recovered.append(
            {
                "ticker": "GMAB",
                "fiscal_end": fiscal_end.strftime("%Y-%m-%d"),
                "available_date": row.available_date,
                **values[key],
            }
        )
        for metric, concept in (("revenue", "Revenue"), ("net_income", "ProfitLoss")):
            fact_rows.append(
                {
                    "ticker": "GMAB",
                    "fiscal_end": fiscal_end,
                    "available_date": pd.Timestamp(row.available_date),
                    "metric": metric,
                    "value": values[key][metric],
                    "taxonomy": "ifrs-full",
                    "concept": concept,
                    "form": "6-K",
                    "accession": row.accession,
                    "unit": "DKK",
                    "source": "sec_filed_gmab_quarterly_report",
                    "source_archive": Path(row.local_path).name,
                    "source_archive_sha256": _sha256(Path(row.local_path)),
                    "derivation": row.mode,
                }
            )
    quarters = pd.DataFrame(fact_rows).sort_values(["fiscal_end", "metric"])
    if quarters.groupby("fiscal_end")["metric"].nunique().eq(2).sum() != 15:
        raise RuntimeError("GMAB recovered chain is not 15 paired quarters")

    output_dir.mkdir(parents=True, exist_ok=True)
    quarters_path = output_dir / "strict_quarterly_facts.csv"
    quarters.to_csv(quarters_path, index=False)
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "promotion_eligible": False,
        "release_status": "BLOCKED",
        "ticker": "GMAB",
        "accepted_quarter_count": 15,
        "recovered_quarters": recovered,
        "registry": {"path": str(registry_path), "sha256": _sha256(registry_path)},
        "reports": bindings,
        "outputs": {
            "quarters": {"path": str(quarters_path), "sha256": _sha256(quarters_path)}
        },
        "guardrail": (
            "Only DKK facts in SEC-filed GMAB reports are used. Comparative "
            "quarters retain their later filing dates; Q4 is annual less the "
            "same year's already filed Q1-Q3. Formal fundamentals are unchanged."
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
