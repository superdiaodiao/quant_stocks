#!/usr/bin/env python3
"""Recover QDEL history from predecessor CIK 353569 Company Facts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pandas as pd

from src.io.fundamentals_update import parse_companyfacts_quarterly


DEFAULT_REGISTRY = Path("stocks_list_dir/nasdaq/qdel_historical_cik.csv")
DEFAULT_RAW = Path("output/data_provenance/qdel_companyfacts/CIK0000353569.json")
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/qdel_historical_companyfacts_2017_2021"
)
USER_AGENT = "quant-stocks-research contact@example.com"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(path: Path, cik: int) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
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
        raise RuntimeError(f"QDEL predecessor Company Facts is unexpectedly small: {url}")
    os.replace(temporary, path)


def _earliest_paired_quarters(parsed: pd.DataFrame) -> pd.DataFrame:
    parsed = parsed.loc[parsed["metric"].isin({"revenue", "net_income"})].copy()
    parsed["fiscal_end"] = pd.to_datetime(parsed["fiscal_end"])
    parsed["available_date"] = pd.to_datetime(parsed["available_date"])
    selected = []
    for fiscal_end, group in parsed.groupby("fiscal_end", sort=True):
        for available_date, filed in group.groupby("available_date", sort=True):
            rows = []
            for metric in ("revenue", "net_income"):
                metric_rows = filed.loc[filed["metric"].eq(metric)].copy()
                values = metric_rows["value"].astype(float).unique()
                if len(values) != 1:
                    rows = []
                    break
                rows.append(metric_rows.iloc[[0]])
            if len(rows) == 2:
                selected.extend(rows)
                break
    if not selected:
        return parsed.iloc[0:0]
    return pd.concat(selected, ignore_index=True)


def run(
    *,
    registry_path: Path = DEFAULT_REGISTRY,
    raw_path: Path = DEFAULT_RAW,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    fetched_at: str = "2026-08-12",
) -> dict:
    registry = pd.read_csv(registry_path)
    if len(registry) != 1 or registry.iloc[0]["ticker"] != "QDEL":
        raise ValueError("QDEL historical CIK registry must contain exactly one QDEL row")
    rule = registry.iloc[0]
    predecessor_cik = int(rule["predecessor_cik"])
    current_cik = int(rule["current_cik"])
    if predecessor_cik != 353569 or current_cik != 1906324:
        raise ValueError("unexpected QDEL predecessor/current CIK pair")
    _download(raw_path, predecessor_cik)
    payload = json.loads(raw_path.read_text(encoding="utf-8"))
    if int(payload.get("cik", 0)) != predecessor_cik:
        raise RuntimeError("QDEL predecessor payload CIK mismatch")
    if "QUIDEL" not in str(payload.get("entityName", "")).upper():
        raise RuntimeError("QDEL predecessor payload issuer mismatch")

    parsed = parse_companyfacts_quarterly("QDEL", payload, pd.Timestamp(fetched_at))
    parsed = _earliest_paired_quarters(parsed)
    start = pd.Timestamp(rule["minimum_fiscal_end"])
    end = pd.Timestamp(rule["maximum_fiscal_end"])
    parsed = parsed.loc[pd.to_datetime(parsed["fiscal_end"]).between(start, end)].copy()
    lag = (
        pd.to_datetime(parsed["available_date"])
        - pd.to_datetime(parsed["fiscal_end"])
    ).dt.days
    parsed = parsed.loc[lag.between(0, 550)].copy()
    paired = parsed.pivot_table(
        index=["fiscal_end", "available_date"],
        columns="metric",
        values="value",
        aggfunc="first",
    ).dropna(subset=["revenue", "net_income"]).reset_index()
    expected_ends = pd.date_range("2017-03-31", "2021-12-31", freq="QE-DEC")
    if list(pd.to_datetime(paired["fiscal_end"])) != list(expected_ends):
        raise RuntimeError("QDEL predecessor chain is not exactly 2017Q1-2021Q4")

    facts = parsed.sort_values(["fiscal_end", "metric"])[
        [
            "ticker", "fiscal_end", "available_date", "metric", "value",
            "taxonomy", "concept", "form", "accession",
        ]
    ].copy()
    facts["unit"] = "USD"
    facts["source"] = "sec_companyfacts_qdel_predecessor_cik"
    facts["source_archive"] = raw_path.name
    facts["source_archive_sha256"] = _sha256(raw_path)
    facts["derivation"] = facts["concept"].map(
        lambda value: "annual_minus_q1_q3" if str(value).startswith("derived_q4:") else "direct_quarter"
    )

    recovered = [
        {
            "ticker": "QDEL",
            "fiscal_end": pd.Timestamp(row.fiscal_end).strftime("%Y-%m-%d"),
            "available_date": pd.Timestamp(row.available_date).strftime("%Y-%m-%d"),
            "revenue": float(row.revenue),
            "net_income": float(row.net_income),
        }
        for row in paired.itertuples(index=False)
    ]
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "promotion_eligible": False,
        "release_status": "BLOCKED",
        "ticker": "QDEL",
        "accepted_quarter_count": 20,
        "recovered_quarters": recovered,
        "historical_cik_transition": {
            "predecessor_cik": predecessor_cik,
            "current_cik": current_cik,
            "transition_date": str(rule["transition_date"]),
            "evidence_url": rule["evidence_url"],
            "maximum_predecessor_fiscal_end": str(rule["maximum_fiscal_end"]),
        },
        "registry": {"path": str(registry_path), "sha256": _sha256(registry_path)},
        "raw_payload": {
            "path": str(raw_path),
            "sha256": _sha256(raw_path),
            "source_url": f"https://data.sec.gov/api/xbrl/companyfacts/CIK{predecessor_cik:010d}.json",
        },
        "outputs": {"quarters": {"path": str(facts_path), "sha256": _sha256(facts_path)}},
        "guardrail": (
            "Only predecessor CIK 353569 facts through fiscal 2021 are used. "
            "Each quarter retains its earliest paired SEC availability date; "
            "later successor comparatives are excluded and formal fundamentals remain unchanged."
        ),
    }
    manifest = output_dir / "manifest.json"
    manifest.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["manifest"] = str(manifest)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--fetched-at", default="2026-08-12")
    args = parser.parse_args()
    result = run(
        registry_path=args.registry,
        raw_path=args.raw,
        output_dir=args.output_dir,
        fetched_at=args.fetched_at,
    )
    print(json.dumps({
        "manifest": result["manifest"],
        "accepted_quarter_count": result["accepted_quarter_count"],
        "release_status": result["release_status"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
