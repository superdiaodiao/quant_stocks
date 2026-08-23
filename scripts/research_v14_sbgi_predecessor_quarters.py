#!/usr/bin/env python3
"""Recover SBGI 2016-2021 quarters from its SEC predecessor CIK."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd
from bs4 import BeautifulSoup

from src.io.fundamentals_update import parse_companyfacts_quarterly


DEFAULT_RAW = Path(
    "output/research_only/v14/companyfacts_cache/CIK0000912752.json.gz"
)
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/sbgi_predecessor_quarters_2016_2021"
)
TRANSITION_URL = (
    "https://www.sec.gov/Archives/edgar/data/1971213/"
    "000119312523158935/d530850d8k.htm"
)
ORIGINAL_ACCESSIONS = {
    "2016-03-31": "0000912752-16-000032",
    "2016-06-30": "0000912752-16-000045",
    "2016-09-30": "0000912752-16-000059",
    "2016-12-31": "0000912752-17-000006",
    "2017-03-31": "0000912752-17-000015",
    "2017-06-30": "0000912752-17-000026",
    "2017-09-30": "0000912752-17-000036",
    "2017-12-31": "0000912752-18-000006",
    "2018-03-31": "0000912752-18-000017",
    "2018-06-30": "0000912752-18-000029",
    "2018-09-30": "0000912752-18-000041",
    "2018-12-31": "0000912752-19-000012",
    "2019-03-31": "0000912752-19-000025",
    "2019-06-30": "0000912752-19-000053",
    "2019-09-30": "0000912752-19-000069",
    "2019-12-31": "0000912752-20-000013",
    "2020-03-31": "0000912752-20-000035",
    "2020-06-30": "0000912752-20-000068",
    "2020-09-30": "0000912752-20-000080",
    "2020-12-31": "0000912752-21-000012",
    "2021-03-31": "0000912752-21-000039",
    "2021-06-30": "0000912752-21-000064",
    "2021-09-30": "0000912752-21-000082",
    "2021-12-31": "0000912752-22-000024",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_wrapper(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        wrapper = json.load(handle)
    payload = wrapper.get("payload", {})
    if int(wrapper.get("cik", 0)) != 912752 or int(
        payload.get("cik", 0)
    ) != 912752:
        raise ValueError("SBGI predecessor Company Facts CIK mismatch")
    if str(payload.get("entityName", "")).upper() not in {
        "SINCLAIR BROADCAST GROUP, LLC",
        "SINCLAIR BROADCAST GROUP INC",
    }:
        raise ValueError("SBGI predecessor Company Facts issuer mismatch")
    return wrapper


def validate_transition_filing(raw: bytes) -> str:
    text = " ".join(BeautifulSoup(raw, "html.parser").get_text(" ").split())
    required = (
        "company formerly known as Sinclair Broadcast Group, Inc.",
        "holding company reorganization",
        "New Sinclair would become the publicly-traded parent company of SBG",
        "Effective at 12:00 am Eastern U.S. time on June 1, 2023",
        "exchanged on a one-for-one basis",
    )
    missing = [phrase for phrase in required if phrase.lower() not in text.lower()]
    if missing:
        raise ValueError(
            "SBGI transition filing does not prove successor continuity: "
            + repr(missing)
        )
    return text


def fetch_transition_filing() -> bytes:
    request = Request(
        TRANSITION_URL,
        headers={"User-Agent": "quant_stocks research contact@example.com"},
    )
    with urlopen(request, timeout=60) as response:
        return response.read()


def select_original_rows(parsed: pd.DataFrame) -> pd.DataFrame:
    selected = []
    for fiscal_end, accession in ORIGINAL_ACCESSIONS.items():
        rows = parsed.loc[
            pd.to_datetime(parsed["fiscal_end"]).eq(pd.Timestamp(fiscal_end))
            & parsed["accession"].astype(str).eq(accession)
            & parsed["metric"].isin({"revenue", "net_income"})
        ].copy()
        counts = rows.groupby("metric").size().to_dict()
        if counts != {"net_income": 1, "revenue": 1}:
            raise ValueError(
                f"SBGI original filing {accession} for {fiscal_end} is not "
                f"an exact revenue/net-income pair: {counts}"
            )
        available_dates = pd.to_datetime(rows["available_date"]).unique()
        if len(available_dates) != 1:
            raise ValueError(
                f"SBGI original filing {accession} has multiple availability dates"
            )
        lag = pd.Timestamp(available_dates[0]) - pd.Timestamp(fiscal_end)
        if not 0 <= lag.days <= 150:
            raise ValueError(
                f"SBGI original filing {accession} has invalid reporting lag"
            )
        selected.append(rows)
    result = pd.concat(selected, ignore_index=True).sort_values(
        ["fiscal_end", "metric"]
    ).reset_index(drop=True)
    if len(result) != 48 or result["fiscal_end"].nunique() != 24:
        raise RuntimeError("SBGI recovery is not exactly twenty-four paired quarters")
    return result


def run(
    *, raw_path: Path = DEFAULT_RAW, output_dir: Path = DEFAULT_OUTPUT_DIR
) -> dict:
    wrapper = _load_wrapper(raw_path)
    transition = fetch_transition_filing()
    validate_transition_filing(transition)
    parsed = parse_companyfacts_quarterly(
        "SBGI", wrapper["payload"], wrapper.get("fetched_at")
    )
    rows = select_original_rows(parsed)
    rows["unit"] = "USD"
    rows["source"] = "sec_companyfacts_sbgi_predecessor_cik"
    rows["source_archive"] = raw_path.name
    rows["source_archive_sha256"] = _sha256(raw_path)

    paired = rows.pivot_table(
        index=["fiscal_end", "available_date"],
        columns="metric",
        values="value",
        aggfunc="first",
    ).reset_index()
    recovered = [
        {
            "ticker": "SBGI",
            "fiscal_end": pd.Timestamp(row.fiscal_end).strftime("%Y-%m-%d"),
            "available_date": pd.Timestamp(row.available_date).strftime("%Y-%m-%d"),
            "revenue": float(row.revenue),
            "net_income": float(row.net_income),
        }
        for row in paired.itertuples(index=False)
    ]

    output_dir.mkdir(parents=True, exist_ok=True)
    transition_path = output_dir / "sec_8k_2023_holding_company_transition.html"
    transition_path.write_bytes(transition)
    quarters_path = output_dir / "strict_quarterly_facts.csv"
    rows.to_csv(quarters_path, index=False)
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "promotion_eligible": False,
        "release_status": "BLOCKED",
        "ticker": "SBGI",
        "predecessor_cik": 912752,
        "successor_cik": 1971213,
        "accepted_quarter_count": 24,
        "recovered_quarters": recovered,
        "identity_transition": {
            "source_url": TRANSITION_URL,
            "path": str(transition_path),
            "sha256": _sha256(transition_path),
            "effective_date": "2023-06-01",
            "share_exchange": "one-for-one",
            "evidence": (
                "SEC Form 8-K states that Sinclair, Inc. became the publicly "
                "traded parent of the former Sinclair Broadcast Group through "
                "a one-for-one holding-company share exchange."
            ),
        },
        "raw_payload": {
            "path": str(raw_path),
            "sha256": _sha256(raw_path),
            "source_url": wrapper.get("source_url"),
            "fetched_at": wrapper.get("fetched_at"),
        },
        "outputs": {
            "quarters": {"path": str(quarters_path), "sha256": _sha256(quarters_path)}
        },
        "guardrail": (
            "Only rows from each quarter's original contemporaneous 10-Q or "
            "10-K accession are selected. Later comparative filings are "
            "excluded. Q4 values use the repository's existing SEC parser "
            "derivation and concept priority. Formal financial files are unchanged."
        ),
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["manifest"] = str(manifest_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = run(raw_path=args.raw, output_dir=args.output_dir)
    print(json.dumps({
        "accepted_quarter_count": result["accepted_quarter_count"],
        "manifest": result["manifest"],
        "release_status": result["release_status"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
