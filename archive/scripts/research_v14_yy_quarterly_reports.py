#!/usr/bin/env python3
"""Recover YY/JOYY 2018Q1-2020Q3 from contemporaneous SEC 6-K exhibits."""

from __future__ import annotations

import argparse
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
from urllib.request import Request, urlopen

import pandas as pd
from bs4 import BeautifulSoup

from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.io.fundamentals_update import OUTPUT_COLUMNS


OUTPUT_DIR = Path("output/research_only/v14/yy_quarterly_reports")
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
CIK = 1_530_238
SOURCES = {
    "2019-03-31": {
        "accession": "0001144204-19-028513",
        "filed": "2019-05-29",
        "document": "tv522609_ex99-1.htm",
        "issuer_marker": "YY Inc.",
        "revenue": 4_780_584_000.0,
        "net_income": 3_149_982_000.0,
        "comparative_fiscal_end": "2018-03-31",
        "comparative_revenue": 3_248_931_000.0,
        "comparative_net_income": 968_913_000.0,
    },
    "2019-06-30": {
        "accession": "0001144204-19-040204",
        "filed": "2019-08-15",
        "document": "tv527724_ex99-1.htm",
        "issuer_marker": "YY Inc.",
        "revenue": 6_295_247_000.0,
        "net_income": 107_419_000.0,
        "comparative_fiscal_end": "2018-06-30",
        "comparative_revenue": 3_773_230_000.0,
        "comparative_net_income": -276_483_000.0,
    },
    "2019-09-30": {
        "accession": "0001104659-19-062908",
        "filed": "2019-11-13",
        "document": "tm1922724d1_ex99-1.htm",
        "issuer_marker": "YY Inc.",
        "revenue": 6_882_214_000.0,
        "net_income": 177_807_000.0,
        "comparative_fiscal_end": "2018-09-30",
        "comparative_revenue": 4_100_472_000.0,
        "comparative_net_income": 680_817_000.0,
    },
    "2019-12-31": {
        "accession": "0001104659-20-034688",
        "filed": "2020-03-17",
        "document": "tm2012957d1_ex99-1.htm",
        "issuer_marker": "JOYY Inc.",
        "revenue": 7_618_159_000.0,
        "net_income": 264_821_000.0,
        "comparative_fiscal_end": "2018-12-31",
        "comparative_revenue": 4_640_924_000.0,
        "comparative_net_income": 742_450_000.0,
    },
    "2020-03-31": {
        "accession": "0001104659-20-064479",
        "filed": "2020-05-21",
        "document": "tm2020586d1_ex99-1.htm",
        "issuer_marker": "JOYY Inc.",
        "revenue": 7_149_445_000.0,
        "net_income": 486_686_000.0,
    },
    "2020-06-30": {
        "accession": "0001104659-20-094330",
        "filed": "2020-08-13",
        "document": "tm2027518d1_ex99-1.htm",
        "issuer_marker": "JOYY Inc.",
        "revenue": 5_840_092_000.0,
        "net_income": 6_954_923_000.0,
    },
    "2020-09-30": {
        "accession": "0001104659-20-126130",
        "filed": "2020-11-17",
        "document": "tm2036086d2_ex99-2.htm",
        "issuer_marker": "JOYY Inc.",
        "revenue": 6_286_375_000.0,
        "net_income": 2_298_045_000.0,
    },
}


def _url(spec: dict) -> str:
    accession = spec["accession"].replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{CIK}/"
        f"{accession}/{spec['document']}"
    )


def _fetch(spec: dict) -> bytes:
    with urlopen(Request(_url(spec), headers=SEC_HEADERS), timeout=120) as response:
        return response.read()


def _normalized(value: object) -> str:
    return " ".join(str(value).replace("\xa0", " ").split()).strip().casefold()


def _number(value: object) -> float | None:
    text = str(value).strip().replace(",", "")
    if text.casefold() in {"", "nan", "—", "-"}:
        return None
    negative = text.startswith("(")
    text = re.sub(r"[^0-9.]", "", text)
    if not text:
        return None
    number = float(text) * 1000.0
    return -number if negative else number


def _date_columns(table: pd.DataFrame, fiscal_end: str) -> list[int]:
    target = pd.Timestamp(fiscal_end).normalize()
    columns = set()
    for row in table.iloc[:6].itertuples(index=False, name=None):
        for index, value in enumerate(row):
            parsed = pd.to_datetime(
                " ".join(str(value).replace("\xa0", " ").split()),
                errors="coerce",
            )
            if not pd.isna(parsed) and pd.Timestamp(parsed).normalize() == target:
                columns.add(index)
    # The exhibits repeat the current-period date for both RMB and translated
    # US-dollar columns.  Only the statement's RMB-thousand source values are
    # admissible; mixing both currencies would create a false conflict.
    header_rows = range(min(6, len(table)))
    has_period_headers = any(
        "months ended" in _normalized(table.iat[row, index])
        for row in header_rows
        for index in range(len(table.columns))
    )
    return sorted(
        index
        for index in columns
        if any(
            _normalized(table.iat[row, index]) == "rmb"
            for row in header_rows
        )
        and (
            not has_period_headers
            or any(
                _normalized(table.iat[row, index]) == "three months ended"
                for row in header_rows
            )
        )
    )


def _metric_value(
    table: pd.DataFrame,
    fiscal_end: str,
    labels: set[str],
) -> float | None:
    columns = _date_columns(table, fiscal_end)
    if not columns:
        return None
    rows = table.loc[
        table.apply(
            lambda row: any(_normalized(value) in labels for value in row), axis=1
        )
    ]
    values = {
        number
        for row in rows.itertuples(index=False, name=None)
        for index in columns
        if (number := _number(row[index])) is not None
    }
    if len(values) > 1:
        # Segment tables repeat the same date across YY, Huya, Bigo and total
        # columns.  They are useful diagnostics but are not the consolidated
        # statement selected here.
        return None
    return next(iter(values)) if values else None


def parse_quarter(raw: bytes, fiscal_end: str, issuer_marker: str) -> dict[str, float]:
    text = " ".join(BeautifulSoup(raw, "html.parser").get_text(" ").split())
    if issuer_marker.casefold() not in text.casefold():
        raise ValueError(f"YY/JOYY issuer identity is not proven: {issuer_marker}")
    candidates = set()
    for table in pd.read_html(BytesIO(raw)):
        revenue = _metric_value(table, fiscal_end, {"total net revenues"})
        net_income = _metric_value(
            table,
            fiscal_end,
            {"net income", "net income (loss)", "net (loss) income"},
        )
        if revenue is not None and net_income is not None:
            candidates.add((revenue, net_income))
    if len(candidates) != 1:
        raise RuntimeError(
            f"expected one consolidated YY/JOYY quarter for {fiscal_end}, "
            f"found {sorted(candidates)}"
        )
    revenue, net_income = candidates.pop()
    return {"revenue": revenue, "net_income": net_income}


def _row(
    *, fiscal_end: str, available_date: str, metric: str, value: float,
    accession: str, comparative: bool, fetched_at: pd.Timestamp,
) -> dict:
    concept = "TotalNetRevenues" if metric == "revenue" else "NetIncome"
    prefix = "sec_6k_exhibit_comparative" if comparative else "sec_6k_exhibit"
    return {
        "ticker": "YY",
        "fiscal_end": fiscal_end,
        "available_date": available_date,
        "metric": metric,
        "value": value,
        "taxonomy": "us-gaap",
        "concept": f"{prefix}:{concept}",
        "form": "6-K",
        "accession": accession,
        "fetched_at": fetched_at,
    }


def recover(output_dir: Path = OUTPUT_DIR) -> dict:
    rows = []
    sources = []
    fetched_at = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    for fiscal_end, spec in SOURCES.items():
        raw = _fetch(spec)
        values = parse_quarter(raw, fiscal_end, spec["issuer_marker"])
        expected = {key: spec[key] for key in ("revenue", "net_income")}
        if values != expected:
            raise RuntimeError(
                f"YY/JOYY {fiscal_end} source changed: {values} != {expected}"
            )
        for metric, value in values.items():
            rows.append(_row(
                fiscal_end=fiscal_end, available_date=spec["filed"],
                metric=metric, value=value, accession=spec["accession"],
                comparative=False, fetched_at=fetched_at,
            ))
        if "comparative_fiscal_end" in spec:
            comparison_end = spec["comparative_fiscal_end"]
            comparison = parse_quarter(raw, comparison_end, spec["issuer_marker"])
            expected_comparison = {
                metric: spec[f"comparative_{metric}"]
                for metric in ("revenue", "net_income")
            }
            if comparison != expected_comparison:
                raise RuntimeError(
                    f"YY/JOYY {comparison_end} comparison changed: "
                    f"{comparison} != {expected_comparison}"
                )
            for metric, value in comparison.items():
                rows.append(_row(
                    fiscal_end=comparison_end, available_date=spec["filed"],
                    metric=metric, value=value, accession=spec["accession"],
                    comparative=True, fetched_at=fetched_at,
                ))
        sources.append({
            "fiscal_end": fiscal_end,
            "accession": spec["accession"],
            "filed": spec["filed"],
            "url": _url(spec),
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        })
    facts = pd.DataFrame(rows, columns=OUTPUT_COLUMNS).sort_values(
        ["fiscal_end", "metric"]
    ).reset_index(drop=True)
    if (
        len(facts) != 22
        or facts["fiscal_end"].nunique() != 11
        or not facts.groupby("fiscal_end")["metric"].nunique().eq(2).all()
    ):
        raise RuntimeError("YY/JOYY recovery is not exactly eleven paired quarters")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "ticker": "YY",
        "current_sec_ticker": "JOYY",
        "cik": CIK,
        "accepted_quarter_count": 11,
        "sources": sources,
        "outputs": {"strict_quarterly_facts": {
            "path": str(facts_path),
            "sha256": hashlib.sha256(facts_path.read_bytes()).hexdigest(),
        }},
        "guardrail": (
            "Uses the historical market ticker YY for the same CIK through the "
            "issuer rename to JOYY. Each 2018 comparison keeps the filing date "
            "of the 2019 report that first disclosed it; 2020 values use only "
            "their contemporaneous 6-K exhibits. Later discontinued-operation "
            "restatements and formal financial files are excluded."
        ),
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    report["manifest"] = str(manifest_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--base-dir", type=Path)
    parser.add_argument("--candidate-output-dir", type=Path)
    args = parser.parse_args()
    if (args.base_dir is None) != (args.candidate_output_dir is None):
        parser.error("--base-dir and --candidate-output-dir must be used together")
    report = recover(args.output_dir)
    if args.base_dir is not None:
        report["candidate"] = integrate_candidate(
            base_dir=args.base_dir,
            supplement_dir=args.output_dir,
            output_dir=args.candidate_output_dir,
        )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
