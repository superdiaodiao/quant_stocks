#!/usr/bin/env python3
"""Recover strict WIX 2016Q4-2021Q3 facts from SEC-filed earnings releases."""

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


DEFAULT_REGISTRY = Path("stocks_list_dir/nasdaq/wix_quarterly_reports.csv")
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/wix_sec_quarterly_reports_2016_2021"
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
        raise RuntimeError(f"WIX SEC filing payload unexpectedly small: {url}")
    os.replace(temporary, path)


def _first_column(table: pd.DataFrame) -> pd.Series:
    return table.iloc[:, 0].fillna("").astype(str).map(_normal)


def _statement(path: Path) -> pd.DataFrame:
    candidates = []
    for table in pd.read_html(io.BytesIO(path.read_bytes())):
        first = _first_column(table)
        header_rows = table.apply(
            lambda row: any(
                _normal(value) == "three months ended" for value in row
            ),
            axis=1,
        )
        if not header_rows.any():
            continue
        header_start = int(header_rows[header_rows].index[0])
        table = table.loc[header_start:].reset_index(drop=True)
        first = _first_column(table)
        has_net = first.isin({"net loss", "net income (loss)"}).any()
        has_revenue = first.eq("revenue").any() or (
            first.eq("creative subscriptions").any()
            and first.eq("business solutions").any()
        )
        if has_net and has_revenue:
            candidates.append(table)
    if not candidates:
        raise ValueError(f"no WIX three-month GAAP income statement in {path}")
    return min(candidates, key=lambda table: (len(table), len(table.columns)))


def _current_value(row: pd.Series, table: pd.DataFrame, year: int) -> float:
    if len(table.columns) < 9:
        raise ValueError("WIX statement lacks the expected current-quarter columns")
    header = [_normal(value) for value in table.iloc[:4, 6:9].to_numpy().ravel()]
    if str(year) not in header or "three months ended" not in header:
        raise ValueError(f"WIX statement current-quarter header does not bind {year}")
    candidates = {
        value for cell in row.iloc[6:9] if (value := _number(cell)) is not None
    }
    if len(candidates) != 1:
        raise ValueError(f"ambiguous WIX {year} current-quarter value: {candidates}")
    return candidates.pop()


def _extract(path: Path, year: int) -> dict[str, float]:
    table = _statement(path)
    first = _first_column(table)
    net_rows = table.loc[first.isin({"net loss", "net income (loss)"})]
    if len(net_rows) != 1:
        raise ValueError(f"ambiguous WIX {year} GAAP net income row")
    net_income = _current_value(net_rows.iloc[0], table, year)

    revenue_rows = table.loc[first.eq("revenue")]
    direct = []
    for _, row in revenue_rows.iterrows():
        try:
            direct.append(_current_value(row, table, year))
        except ValueError:
            pass
    if direct:
        if len(set(direct)) != 1:
            raise ValueError(f"ambiguous WIX {year} direct revenue: {direct}")
        revenue = direct[0]
        derivation = "direct_three_month_gaap"
    else:
        revenue_header = first[first.eq("revenue")].index
        cost_header = first[first.eq("cost of revenue")].index
        if len(revenue_header) != 1 or len(cost_header) != 1:
            raise ValueError(f"WIX {year} revenue section is not uniquely bounded")
        section = table.loc[revenue_header[0] + 1 : cost_header[0] - 1]
        section_first = _first_column(section)
        components = []
        for label in ("creative subscriptions", "business solutions"):
            rows = section.loc[section_first.eq(label)]
            if len(rows) != 1:
                raise ValueError(f"WIX {year} missing unique {label} revenue row")
            components.append(_current_value(rows.iloc[0], table, year))
        revenue = sum(components)
        derivation = "sum_direct_three_month_gaap_revenue_components"

    return {
        "revenue": round(revenue * 1_000.0, 2),
        "net_income": round(net_income * 1_000.0, 2),
        "revenue_derivation": derivation,
    }


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
        or set(registry["ticker"]) != {"WIX"}
    ):
        raise ValueError("WIX registry must contain exactly 2016Q4-2021Q3")

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
                "ticker": "WIX",
                "fiscal_end": fiscal_end.strftime("%Y-%m-%d"),
                "available_date": row.available_date,
                "revenue": values[key]["revenue"],
                "net_income": values[key]["net_income"],
            }
        )
        for metric, concept in (
            ("revenue", "Revenues"), ("net_income", "NetIncomeLoss")
        ):
            facts.append(
                {
                    "ticker": "WIX",
                    "fiscal_end": fiscal_end,
                    "available_date": pd.Timestamp(row.available_date),
                    "metric": metric,
                    "value": values[key][metric],
                    "taxonomy": "us-gaap",
                    "concept": concept,
                    "form": "6-K",
                    "accession": row.accession,
                    "unit": "USD",
                    "source": "sec_filed_wix_quarterly_earnings",
                    "source_archive": Path(row.local_path).name,
                    "source_archive_sha256": _sha256(Path(row.local_path)),
                    "derivation": (
                        values[key]["revenue_derivation"]
                        if metric == "revenue"
                        else "direct_three_month_gaap"
                    ),
                }
            )
    quarters = pd.DataFrame(facts).sort_values(["fiscal_end", "metric"])
    if quarters.groupby("fiscal_end")["metric"].nunique().eq(2).sum() != 20:
        raise RuntimeError("WIX recovered chain is not 20 paired quarters")

    output_dir.mkdir(parents=True, exist_ok=True)
    quarters_path = output_dir / "strict_quarterly_facts.csv"
    quarters.to_csv(quarters_path, index=False)
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "promotion_eligible": False,
        "release_status": "BLOCKED",
        "ticker": "WIX",
        "accepted_quarter_count": 20,
        "recovered_quarters": recovered,
        "registry": {"path": str(registry_path), "sha256": _sha256(registry_path)},
        "reports": bindings,
        "outputs": {
            "quarters": {"path": str(quarters_path), "sha256": _sha256(quarters_path)}
        },
        "guardrail": (
            "Only current-period three-month GAAP USD facts in SEC-filed WIX "
            "earnings releases are used. From 2020 onward revenue is the strict "
            "sum of the two directly reported GAAP revenue components. Non-GAAP "
            "rows and cumulative comparison columns are excluded; formal "
            "fundamentals are unchanged."
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
