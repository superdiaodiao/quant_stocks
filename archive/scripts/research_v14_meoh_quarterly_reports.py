#!/usr/bin/env python3
"""Recover MEOH 2018Q4-2021Q3 from contemporaneous SEC 6-K reports."""

from __future__ import annotations

import argparse
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import time
from urllib.request import Request, urlopen

import pandas as pd
from bs4 import BeautifulSoup

from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.io.fundamentals_update import OUTPUT_COLUMNS


OUTPUT_DIR = Path("output/research_only/v14/meoh_quarterly_reports")
CIK = 886_977
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
SOURCES = {
    "2018-12-31": ("2019-01-30", "0001628280-19-000658", "a2018q4-xxdocument.htm", "63523eb0a3bad7bd343782805fa0260ee9a36743ac5f5a939302a8749faa813f"),
    "2019-03-31": ("2019-04-24", "0001628280-19-004643", "a2019q1-xxdocument.htm", "4a041ec786cd789973ed00302bbaea80a54e7a1256d7d22e48880a92d85ec731"),
    "2019-06-30": ("2019-07-31", "0001628280-19-009476", "a2019q2-xxdocument.htm", "b349968e6b6ed36a8388bb58e419e946ce4deee01e1e7c0b03ce5e99aef02479"),
    "2019-09-30": ("2019-10-30", "0001628280-19-012849", "a2019q3-xxdocument.htm", "daf8be594363e2936bae761d6373a01fd7e5ba74b632f4910d07e0b33e243d0b"),
    "2019-12-31": ("2020-01-29", "0001628280-20-000704", "a2019q4-xxdocument.htm", "fae31381ca6bfe48c23ad4b1cdec8e84a5e4858d1d5d84c81195781ada4a88ec"),
    "2020-03-31": ("2020-05-05", "0001628280-20-006477", "a2020q1-xxdocument.htm", "e9891273cd3a66c72fa5e608e108cb701f231bebe4f4da376ba915ccd30bc290"),
    "2020-06-30": ("2020-07-29", "0001628280-20-010835", "a2020q2-xxdocument.htm", "9d212a5e9f495a78233872b9db797ca18dee8a869ba1854b0b415a7268a4ad2f"),
    "2020-09-30": ("2020-10-28", "0001628280-20-015001", "a2020q3-xxdocument.htm", "46c4c1248008e86cff4cf7bdccc6bdf880bc50f48edc15c05d14dbd4ac1f105f"),
    "2020-12-31": ("2021-01-27", "0001628280-21-000864", "a2020q4-xxdocument.htm", "fac822fe89a31558392b3e60f62e18940126b51f958f6ac5a7ae30dde3875923"),
    "2021-03-31": ("2021-04-28", "0001628280-21-007957", "a2021q1-xxdocument.htm", "c8310a7217b9c77af9c6ebb380a7c8639a88b50405012efcad2abbd013425f49"),
    "2021-06-30": ("2021-07-28", "0001628280-21-014755", "a2021q2-xxdocument.htm", "3b76275f548dfa2558123b2d25b520179622cce53ab3ef1ca27da148905bcb4c"),
    "2021-09-30": ("2021-10-27", "0001628280-21-020651", "q32021-xxdocument.htm", "05fbcdfe57a89cd7961a9e5a6c40c5525d9ca786da721bf6cfd8e9d6d6a409ab"),
}
EXPECTED = {
    "2018-12-31": (977_000_000.0, 161_000_000.0),
    "2019-03-31": (733_000_000.0, 38_000_000.0),
    "2019-06-30": (734_000_000.0, 50_000_000.0),
    "2019-09-30": (650_000_000.0, -10_000_000.0),
    "2019-12-31": (659_000_000.0, 9_000_000.0),
    "2020-03-31": (745_000_000.0, 23_000_000.0),
    "2020-06-30": (512_000_000.0, -65_000_000.0),
    "2020-09-30": (581_000_000.0, -88_000_000.0),
    "2020-12-31": (811_000_000.0, -27_000_000.0),
    "2021-03-31": (1_016_000_000.0, 105_000_000.0),
    "2021-06-30": (1_068_000_000.0, 107_000_000.0),
    "2021-09-30": (1_078_000_000.0, 71_000_000.0),
}


def _spec(values: tuple[str, str, str, str]) -> dict:
    filed, accession, document, sha256 = values
    return {"filed": filed, "accession": accession, "document": document,
            "sha256": sha256}


def _url(spec: dict) -> str:
    compact = str(spec["accession"]).replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{CIK}/{compact}/{spec['document']}"


def _fetch(spec: dict) -> bytes:
    request = Request(_url(spec), headers=SEC_HEADERS)
    error = None
    for attempt in range(4):
        try:
            with urlopen(request, timeout=120) as response:
                return response.read()
        except Exception as exc:  # pragma: no cover - transient network path
            error = exc
            if attempt < 3:
                time.sleep(attempt + 1)
    raise RuntimeError(f"failed to fetch MEOH source {_url(spec)}") from error


def _number(value: object) -> float | None:
    text = str(value).strip()
    if not re.fullmatch(r"\$?\s*\(?-?[0-9][0-9,]*(?:\.[0-9]+)?\)?", text):
        return None
    cleaned = re.sub(r"[^0-9.]", "", text)
    negative = "(" in text or text.startswith("-")
    return (-1.0 if negative else 1.0) * float(cleaned) * 1_000_000.0


def parse_quarter(raw: bytes, fiscal_end: str) -> dict[str, float]:
    text = " ".join(BeautifulSoup(raw, "html.parser").get_text(" ").split())
    end = pd.Timestamp(fiscal_end)
    month = end.strftime("%b").lower()
    date_pattern = rf"{month}\w*\s+{end.day},?\s*{end.year}"
    if not re.search(r"\$\s*millions|\(\$\s*millions", text, re.I):
        raise RuntimeError("MEOH filing does not prove USD millions scale")

    matches = []
    statement_found = False
    requested_period_found = False
    for table in pd.read_html(BytesIO(raw)):
        labels = table.iloc[:, 0].astype(str).str.replace(
            r"\s+", " ", regex=True
        ).str.strip()
        revenue = labels.str.fullmatch(r"Revenue(?:\s*\d+)?", case=False)
        income = labels.str.fullmatch(
            r"Net income(?: \(loss\))? \(attributable to Methanex shareholders\)",
            case=False,
        )
        if not revenue.any() or not income.any():
            continue
        statement_found = True
        revenue_position = table.index.get_loc(revenue[revenue].index[0])
        header = " ".join(
            str(value)
            for value in table.iloc[:revenue_position].to_numpy().ravel()
        )
        dates = re.findall(
            r"(?:Jan|Mar|Jun|Sep|Dec)\w*\s+\d{1,2},?\s*\d{4}",
            header,
            re.I,
        )
        if not dates or not re.fullmatch(date_pattern, dates[0], re.I):
            continue
        requested_period_found = True
        revenue_values = [
            parsed for value in table.loc[revenue[revenue].index[0]].tolist()
            if (parsed := _number(value)) is not None
        ]
        income_values = [
            parsed for value in table.loc[income[income].index[0]].tolist()
            if (parsed := _number(value)) is not None
        ]
        if revenue_values and income_values:
            matches.append((revenue_values[0], income_values[0]))
    if statement_found and not requested_period_found:
        raise RuntimeError("MEOH filing does not prove requested quarter end")
    unique = sorted(set(matches))
    if len(unique) != 1:
        raise RuntimeError(f"MEOH current-quarter facts are not unique: {unique}")
    return {"revenue": unique[0][0], "net_income": unique[0][1]}


def recover(output_dir: Path = OUTPUT_DIR) -> dict:
    fetched_at = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    rows, sources, observed = [], [], {}
    for fiscal_end, raw_spec in SOURCES.items():
        spec = _spec(raw_spec)
        raw = _fetch(spec)
        digest = hashlib.sha256(raw).hexdigest()
        if digest != spec["sha256"]:
            raise RuntimeError(f"MEOH source changed for {fiscal_end}: {digest}")
        facts = parse_quarter(raw, fiscal_end)
        observed[fiscal_end] = (facts["revenue"], facts["net_income"])
        sources.append({
            "fiscal_end": fiscal_end, "filed": spec["filed"],
            "accession": spec["accession"], "url": _url(spec),
            "sha256": digest, "bytes": len(raw),
        })
        for metric, value in facts.items():
            rows.append({
                "ticker": "MEOH", "fiscal_end": fiscal_end,
                "available_date": spec["filed"], "metric": metric,
                "value": value, "taxonomy": "ifrs-full",
                "concept": (
                    "Revenue" if metric == "revenue"
                    else "ProfitLossAttributableToOwnersOfParent"
                ),
                "form": "6-K", "accession": spec["accession"],
                "fetched_at": fetched_at,
            })
    if observed != EXPECTED:
        raise RuntimeError(f"MEOH recovered quarters changed: {observed}")
    facts = pd.DataFrame(rows, columns=OUTPUT_COLUMNS).sort_values(
        ["available_date", "fiscal_end", "metric"]
    ).reset_index(drop=True)
    if len(facts) != 24 or facts["fiscal_end"].nunique() != 12:
        raise RuntimeError("MEOH recovery is not exactly twelve paired quarters")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1, "research_only": True,
        "point_in_time_proven": True, "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED", "promotion_eligible": False,
        "formal_financials_modified": False, "ticker": "MEOH", "cik": CIK,
        "accepted_quarter_count": 12, "accepted_fact_count": 24,
        "sources": sources,
        "outputs": {"strict_quarterly_facts": {
            "path": str(facts_path),
            "sha256": hashlib.sha256(facts_path.read_bytes()).hexdigest(),
        }},
        "guardrail": (
            "Every observation is the explicit current three-month USD-million "
            "Revenue and IFRS Net income or loss attributable to Methanex "
            "shareholders in a contemporaneous SEC 6-K. Adjusted revenue, "
            "adjusted income, per-share figures and cumulative columns are "
            "excluded. Each filing and content SHA is bound in this manifest."
        ),
    }
    manifest = output_dir / "manifest.json"
    manifest.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["manifest"] = str(manifest)
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
            base_dir=args.base_dir, supplement_dir=args.output_dir,
            output_dir=args.candidate_output_dir,
        )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
