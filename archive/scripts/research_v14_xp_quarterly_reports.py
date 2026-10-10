#!/usr/bin/env python3
"""Recover XP 2018Q4-2021Q3 from contemporaneous SEC IFRS filings."""

from __future__ import annotations

import argparse
import gzip
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import time
from urllib.request import Request, urlopen

import pandas as pd

from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.io.fundamentals_update import OUTPUT_COLUMNS


OUTPUT_DIR = Path("output/research_only/v14/xp_quarterly_reports")
COMPANYFACTS = Path(
    "cleaned_stocks_data/financial/sec_companyfacts_cache/CIK0001787425.json.gz"
)
CIK = 1_787_425
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}

# Filing tables are in thousands of BRL unless scale is explicitly one million.
# The table parser proves the current quarter and the immediately comparable
# quarter independently; the IPO prospectus provides exact nine-month and
# annual statements used only for the two declared Q4 residuals.
SOURCES = {
    "prospectus": {
        "filed": "2019-12-11",
        "accession": "0001193125-19-311633",
        "document": "d829388d424b1.htm",
        "sha256": "0749624a1d25e50f3e7e26fbcb85688bb2ada0301f30cf8945e081d9c1adefa0",
    },
    "2020q1": {
        "filed": "2020-05-12", "accession": "0000950103-20-009419",
        "document": "dp128053_ex9903.htm",
        "sha256": "88114606c363b65d3176cbef001108cf658d68e69f4cd0796897cfea77325643",
    },
    "2020q2": {
        "filed": "2020-08-12", "accession": "0000950103-20-015677",
        "document": "dp134210_ex9903.htm",
        "sha256": "260fc05ae42243f887cddd1ce1fa4ef7b40cc14294f61917600d89d01651c320",
    },
    "2020q3": {
        "filed": "2020-11-09", "accession": "0000950103-20-021814",
        "document": "dp140503_ex9903.htm",
        "sha256": "be765bb1cf22046f37cea1d2681267dc7742ee44c5e8974ea6979ea138222510",
    },
    "2021q1": {
        "filed": "2021-05-05", "accession": "0000950103-21-006725",
        "document": "dp150552_ex9902.htm",
        "sha256": "304564a70ce5f30d0bb821e9d6a26a08f520e5c430f0e8580284c2b711132211",
    },
    "2021q2": {
        "filed": "2021-08-04", "accession": "0000950103-21-011870",
        "document": "dp155760_ex9903.htm",
        "sha256": "e1f4ff2189e6f51525a2397ee929e458712758617ec33a59339dae375d7e9daa",
    },
    "2021q3": {
        "filed": "2021-11-04", "accession": "0000950103-21-017343",
        "document": "dp161285_ex9903.htm",
        "sha256": "af3ad7368b4c2fc5cf9f6c8793e99257eed7563ccf6081c2ef022d85bb596db8",
    },
}

EXPECTED = {
    "2018-12-31": (885_155_000.0, 113_448_000.0, "2019-12-11"),
    "2019-03-31": (933_992_000.0, 210_438_000.0, "2020-05-12"),
    "2019-06-30": (1_146_597_000.0, 228_062_000.0, "2020-08-12"),
    "2019-09-30": (1_355_924_000.0, 260_798_000.0, "2020-11-09"),
    "2019-12-31": (1_691_295_000.0, 390_186_000.0, "2020-05-22"),
    "2020-03-31": (1_734_841_000.0, 397_554_000.0, "2020-05-12"),
    "2020-06-30": (1_920_929_000.0, 540_263_000.0, "2020-08-12"),
    "2020-09-30": (2_100_737_000.0, 541_284_000.0, "2020-11-09"),
    "2020-12-31": (2_395_098_000.0, 602_388_000.0, "2021-04-29"),
    "2021-03-31": (2_628_041_000.0, 734_148_000.0, "2021-05-05"),
    "2021-06-30": (3_018_085_000.0, 931_275_000.0, "2021-08-04"),
    "2021-09-30": (3_171_359_000.0, 936_387_000.0, "2021-11-04"),
}


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
    raise RuntimeError(f"failed to fetch XP source {_url(spec)}") from error


def _number(value: object, scale: float = 1000.0) -> float | None:
    if pd.isna(value):
        return None
    text = str(value).strip()
    negative = "(" in text or text.startswith("-")
    cleaned = re.sub(r"[^0-9.]", "", text)
    if not cleaned:
        return None
    return (-1.0 if negative else 1.0) * float(cleaned) * scale


def _matching_rows(table: pd.DataFrame) -> tuple[int, int] | None:
    labels = table.iloc[:, 0].astype(str).str.replace(r"\s+", " ", regex=True).str.strip()
    revenue = labels.str.fullmatch("Total revenue and income", case=False)
    income = labels.str.fullmatch("Net income for the period", case=False)
    if not revenue.any() or not income.any():
        return None
    return int(revenue[revenue].index[0]), int(income[income].index[0])


def _header_rows(table: pd.DataFrame) -> int:
    labels = table.iloc[:, 0]
    for position, value in enumerate(labels):
        if pd.notna(value) and str(value).strip().lower() not in {"", "nan"}:
            return position
    return min(len(table), 4)


def parse_interim(
    raw: bytes,
    periods: list[tuple[str, int]],
    column_hints: dict[str, int] | None = None,
) -> dict[str, tuple[float, float]]:
    """Extract declared three-month IFRS columns from one SEC exhibit."""
    candidates = []
    for table in pd.read_html(BytesIO(raw)):
        rows = _matching_rows(table)
        if rows is None:
            continue
        revenue_row, income_row = rows
        header_rows = _header_rows(table)
        headers = [
            " ".join(
                [str(table.columns[column])]
                + [str(table.iloc[row, column]) for row in range(header_rows)]
            )
            for column in range(table.shape[1])
        ]
        candidates.append((table, revenue_row, income_row, headers))
    if not candidates:
        raise RuntimeError("XP filing lacks the IFRS quarterly income statement")

    result: dict[str, tuple[float, float]] = {}
    for fiscal_end, year in periods:
        matches = []
        for table, revenue_row, income_row, headers in candidates:
            for column, header in enumerate(headers):
                normalized = " ".join(header.split())
                if str(year) not in normalized or "three months" not in normalized.lower():
                    continue
                revenue = _number(table.iloc[revenue_row, column])
                income = _number(table.iloc[income_row, column])
                if revenue is not None and income is not None:
                    matches.append((revenue, income))
        unique = sorted(set(matches))
        if not unique and column_hints and fiscal_end in column_hints:
            column = column_hints[fiscal_end]
            hinted = []
            for table, revenue_row, income_row, _ in candidates:
                if column >= table.shape[1]:
                    continue
                revenue = _number(table.iloc[revenue_row, column])
                income = _number(table.iloc[income_row, column])
                if revenue is not None and income is not None:
                    hinted.append((revenue, income))
            unique = sorted(set(hinted))
        if len(unique) != 1:
            raise RuntimeError(
                f"XP {fiscal_end} quarterly values are not uniquely proven: {unique}"
            )
        result[fiscal_end] = unique[0]
    return result


def parse_ipo_2018(raw: bytes) -> dict[str, tuple[float, float]]:
    """Extract exact 2018 nine-month and annual BRL IFRS totals."""
    if "brazilian reais" not in raw.decode("latin1", errors="ignore").lower():
        raise RuntimeError("XP IPO filing does not prove BRL reporting currency")
    found: dict[str, set[tuple[float, float]]] = {"nine_month": set(), "annual": set()}
    for table in pd.read_html(BytesIO(raw)):
        rows = _matching_rows(table)
        if rows is None:
            labels = table.iloc[:, 0].astype(str).str.replace(r"\s+", " ", regex=True).str.strip()
            revenue = labels.str.fullmatch("Total revenue and income", case=False)
            income = labels.str.fullmatch("Net income for the year", case=False)
            if not revenue.any() or not income.any():
                continue
            rows = int(revenue[revenue].index[0]), int(income[income].index[0])
        revenue_row, income_row = rows
        header_rows = _header_rows(table)
        for column in range(table.shape[1]):
            header = " ".join(
                str(table.iloc[row, column]) for row in range(header_rows)
            )
            normalized = " ".join(header.split())
            if "2018" not in normalized:
                continue
            key = None
            if "nine months" in normalized.lower():
                key = "nine_month"
            elif (
                "year ended" in normalized.lower()
                or re.fullmatch(r"(?:nan\s+)*2018(?:\.0)?", normalized.strip())
            ):
                key = "annual"
            if key is None:
                continue
            scale = 1_000_000.0 if "million" in normalized.lower() else 1000.0
            revenue = _number(table.iloc[revenue_row, column], scale)
            income = _number(table.iloc[income_row, column], scale)
            if revenue is not None and income is not None:
                found[key].add((revenue, income))
    exact = {
        "nine_month": (2_073_298_000.0, 351_882_000.0),
        "annual": (2_958_453_000.0, 465_330_000.0),
    }
    for key, expected in exact.items():
        if expected not in found[key]:
            raise RuntimeError(f"XP IPO filing does not prove exact {key} values")
    return exact


def _load_annual() -> tuple[dict, str]:
    digest = hashlib.sha256(COMPANYFACTS.read_bytes()).hexdigest()
    with gzip.open(COMPANYFACTS, "rt", encoding="utf-8") as handle:
        wrapper = json.load(handle)
    payload = wrapper.get("payload", {})
    if int(wrapper.get("cik", 0)) != CIK or int(payload.get("cik", 0)) != CIK:
        raise RuntimeError("XP Company Facts CIK mismatch")
    if payload.get("entityName") != "XP Inc.":
        raise RuntimeError("XP Company Facts issuer mismatch")
    return wrapper, digest


def _annual_fact(payload: dict, concept: str, fiscal_end: str,
                 filed: str, accession: str) -> float:
    matches = [
        fact for fact in payload["facts"]["ifrs-full"][concept]["units"]["BRL"]
        if fact.get("start") == f"{fiscal_end[:4]}-01-01"
        and fact.get("end") == fiscal_end
        and fact.get("filed") == filed
        and fact.get("accn") == accession
    ]
    if len(matches) != 1:
        raise RuntimeError(f"XP annual {concept} is not unique for {fiscal_end}")
    return float(matches[0]["val"])


def recover(output_dir: Path = OUTPUT_DIR) -> dict:
    wrapper, companyfacts_sha = _load_annual()
    payload = wrapper["payload"]
    rows, sources = [], []
    values: dict[str, tuple[float, float, str, str, str]] = {}

    raw_by_key: dict[str, bytes] = {}
    for key, spec in SOURCES.items():
        raw = _fetch(spec)
        digest = hashlib.sha256(raw).hexdigest()
        if digest != spec["sha256"]:
            raise RuntimeError(f"XP source changed for {key}: {digest}")
        raw_by_key[key] = raw
        sources.append({
            "key": key, "filed": spec["filed"], "accession": spec["accession"],
            "url": _url(spec), "sha256": digest, "bytes": len(raw),
        })

    ipo = parse_ipo_2018(raw_by_key["prospectus"])
    q4_2018 = tuple(ipo["annual"][i] - ipo["nine_month"][i] for i in range(2))
    values["2018-12-31"] = (
        *q4_2018, SOURCES["prospectus"]["filed"],
        SOURCES["prospectus"]["accession"], "derived_q4",
    )

    interim_specs = {
        "2020q1": [("2019-03-31", 2019), ("2020-03-31", 2020)],
        "2020q2": [("2019-06-30", 2019), ("2020-06-30", 2020)],
        "2020q3": [("2019-09-30", 2019), ("2020-09-30", 2020)],
        "2021q1": [("2021-03-31", 2021)],
        "2021q2": [("2021-06-30", 2021)],
        "2021q3": [("2021-09-30", 2021)],
    }
    for key, periods in interim_specs.items():
        hints = {"2021-09-30": 13} if key == "2021q3" else None
        parsed = parse_interim(raw_by_key[key], periods, column_hints=hints)
        spec = SOURCES[key]
        for fiscal_end, pair in parsed.items():
            values[fiscal_end] = (*pair, spec["filed"], spec["accession"], "direct_quarter")

    annual_specs = {
        "2019-12-31": ("2020-05-22", "0000950103-20-010060"),
        "2020-12-31": ("2021-04-29", "0001628280-21-008199"),
    }
    for fiscal_end, (filed, accession) in annual_specs.items():
        year = int(fiscal_end[:4])
        annual = (
            _annual_fact(payload, "RevenueAndOperatingIncome", fiscal_end, filed, accession),
            _annual_fact(payload, "ProfitLoss", fiscal_end, filed, accession),
        )
        prior = [values[f"{year}-{month_day}"] for month_day in ("03-31", "06-30", "09-30")]
        q4 = tuple(annual[i] - sum(pair[i] for pair in prior) for i in range(2))
        values[fiscal_end] = (*q4, filed, accession, "derived_q4")

    observed = {
        fiscal_end: (value[0], value[1], value[2])
        for fiscal_end, value in sorted(values.items())
    }
    if observed != EXPECTED:
        raise RuntimeError(f"XP recovered quarters changed: {observed}")

    fetched_at = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    for fiscal_end, (revenue, income, filed, accession, derivation) in sorted(values.items()):
        for metric, value, concept in (
            ("revenue", revenue, "RevenueAndOperatingIncome"),
            ("net_income", income, "ProfitLoss"),
        ):
            rows.append({
                "ticker": "XP", "fiscal_end": fiscal_end,
                "available_date": filed, "metric": metric, "value": value,
                "taxonomy": "ifrs-full", "concept": f"{derivation}:{concept}",
                "form": (
                    "424B1" if fiscal_end == "2018-12-31"
                    else "20-F" if derivation == "derived_q4" else "6-K"
                ),
                "accession": accession, "fetched_at": fetched_at,
            })
    facts = pd.DataFrame(rows, columns=OUTPUT_COLUMNS).sort_values(
        ["available_date", "fiscal_end", "metric"]
    ).reset_index(drop=True)
    if len(facts) != 24 or facts["fiscal_end"].nunique() != 12:
        raise RuntimeError("XP recovery is not exactly twelve paired quarters")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1, "research_only": True,
        "point_in_time_proven": True, "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED", "promotion_eligible": False,
        "formal_financials_modified": False, "ticker": "XP", "cik": CIK,
        "accepted_quarter_count": 12, "accepted_fact_count": 24,
        "sources": sources,
        "companyfacts": {
            "path": str(COMPANYFACTS), "sha256": companyfacts_sha,
            "source_url": wrapper.get("source_url"),
            "fetched_at": wrapper.get("fetched_at"),
        },
        "outputs": {"strict_quarterly_facts": {
            "path": str(facts_path),
            "sha256": hashlib.sha256(facts_path.read_bytes()).hexdigest(),
        }},
        "guardrail": (
            "Every direct observation is an explicit three-month BRL IFRS "
            "Total revenue and income and Net income for the period value in "
            "an SEC filing. Q4 values are annual less three "
            "independently proven quarters using the first eligible annual "
            "filing date. Components, adjusted measures, cumulative periods "
            "and later comparative availability are excluded."
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
