#!/usr/bin/env python3
"""Recover INMD 2019-2021 quarters from contemporaneous SEC filings."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd
from bs4 import BeautifulSoup


DEFAULT_COMPANYFACTS = Path(
    "cleaned_stocks_data/financial/sec_companyfacts_cache/CIK0001742692.json.gz"
)
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/inmd_quarterly_reports_2019_2021"
)
SEC_BASE = "https://www.sec.gov/Archives/edgar/data/1742692"
HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
DIRECT_FILINGS = {
    "2019-03-31": {
        "filed": "2019-07-11",
        "accession": "0001144204-19-034453",
        "document": "tv524581-f1.htm",
        "form": "F-1",
    },
    "2019-09-30": {
        "filed": "2019-11-05",
        "accession": "0001104659-19-059824",
        "document": "tm1921789d1_ex99-1.htm",
        "form": "6-K",
    },
    "2020-03-31": {
        "filed": "2020-05-06",
        "accession": "0001178913-20-001326",
        "document": "exhibit_99-1.htm",
        "form": "6-K",
    },
    "2020-06-30": {
        "filed": "2020-08-05",
        "accession": "0001178913-20-002247",
        "document": "exhibit_99-1.htm",
        "form": "6-K",
    },
    "2020-09-30": {
        "filed": "2020-11-12",
        "accession": "0001178913-20-003105",
        "document": "exhibit_99-1.htm",
        "form": "6-K",
    },
    "2021-03-31": {
        "filed": "2021-05-05",
        "accession": "0001178913-21-001598",
        "document": "exhibit_99-1.htm",
        "form": "6-K",
    },
    "2021-06-30": {
        "filed": "2021-07-28",
        "accession": "0001178913-21-002426",
        "document": "exhibit_99-1.htm",
        "form": "6-K",
    },
    "2021-09-30": {
        "filed": "2021-10-26",
        "accession": "0001178913-21-003249",
        "document": "exhibit_99-1.htm",
        "form": "6-K",
    },
}
ANNUAL_FILINGS = {
    "2018-12-31": ("2020-02-18", "0001178913-20-000541"),
    "2019-12-31": ("2020-02-18", "0001178913-20-000541"),
    "2020-12-31": ("2021-02-10", "0001178913-21-000406"),
    "2021-12-31": ("2022-02-10", "0001178913-22-000512"),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _number(value: str) -> float:
    compact = value.replace("$", "").replace(",", "").strip()
    negative = compact.startswith("(") and compact.endswith(")")
    compact = compact.strip("() ")
    result = float(compact) * 1000.0
    return -result if negative else result


def _row_values(statement: str, label: str) -> list[float]:
    match = re.search(
        rf"\b{re.escape(label)}\s+"
        r"((?:\$?\s*\(?[\d,]+\)?\s+){2,4})",
        statement,
        re.IGNORECASE,
    )
    if match is None:
        raise ValueError(f"INMD GAAP statement does not prove {label}")
    values = re.findall(r"\$?\s*\(?[\d,]+\)?", match.group(1))
    return [_number(value) for value in values]


def _parse_statement_columns(raw: bytes, fiscal_end: str) -> dict:
    text = " ".join(BeautifulSoup(raw, "html.parser").get_text(" ").split())
    text = text.replace("\u200b", " ")
    text = " ".join(text.split())
    identity = (
        "INMODE LTD.",
        "CONSOLIDATED STATEMENTS OF INCOME",
        "U.S. dollars in thousands",
        "Unaudited",
    )
    if not all(value.lower() in text.lower() for value in identity):
        raise ValueError("INMD identity, GAAP statement, or units are not proven")
    end = pd.Timestamp(fiscal_end)
    period_label = end.strftime("%B %-d")
    if (
        "three months ended" not in text.lower()
        or period_label.lower() not in text.lower()
    ):
        raise ValueError("INMD filing does not prove the requested quarter")
    titles = list(re.finditer(
        r"INMODE LTD\.\s+(?:CONDENSED\s+)?"
        r"CONSOLIDATED STATEMENTS OF INCOME",
        text,
        re.IGNORECASE,
    ))
    statements = [
        text[title.start() : title.start() + 6000]
        for title in titles
        if "three months ended"
        in text[title.start() : title.start() + 1000].lower()
        and period_label.lower()
        in text[title.start() : title.start() + 1000].lower()
        and "unaudited" in text[title.start() : title.start() + 1000].lower()
    ]
    if not statements:
        raise ValueError("INMD primary GAAP income statement is absent")
    statement = statements[0]
    if statement.upper().startswith("INMODE LTD. RECONCILIATION"):
        raise ValueError("INMD parser selected a non-GAAP reconciliation")
    revenue = _row_values(statement, "REVENUES")
    net_income = _row_values(statement, "NET INCOME")
    expected_columns = 2 if end.quarter == 1 else 4
    if len(revenue) != expected_columns or len(net_income) != expected_columns:
        raise ValueError("INMD statement has an unexpected period-column layout")
    parsed = {
        "current": {"revenue": revenue[0], "net_income": net_income[0]},
        "comparative": {
            "revenue": revenue[1], "net_income": net_income[1]
        },
        "cumulative": None,
        "cumulative_comparative": None,
    }
    if expected_columns == 4:
        parsed["cumulative"] = {
            "revenue": revenue[2],
            "net_income": net_income[2],
        }
        parsed["cumulative_comparative"] = {
            "revenue": revenue[3],
            "net_income": net_income[3],
        }
    return parsed


def parse_statement(raw: bytes, fiscal_end: str) -> dict:
    """Parse the current three-month GAAP column from an INMD SEC filing."""
    return _parse_statement_columns(raw, fiscal_end)["current"]


def _load_companyfacts(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        wrapper = json.load(handle)
    payload = wrapper.get("payload", {})
    if int(wrapper.get("cik", 0)) != 1742692 or int(
        payload.get("cik", 0)
    ) != 1742692:
        raise ValueError("INMD Company Facts CIK mismatch")
    if str(payload.get("entityName", "")).upper() != "INMODE LTD.":
        raise ValueError("INMD Company Facts issuer mismatch")
    return wrapper


def _annual_fact(
    payload: dict,
    *,
    concept: str,
    fiscal_end: str,
    filed: str,
    accession: str,
) -> dict:
    end = pd.Timestamp(fiscal_end)
    start = pd.Timestamp(year=end.year, month=1, day=1)
    matches = [
        item
        for item in payload["facts"]["us-gaap"][concept]["units"]["USD"]
        if pd.Timestamp(item.get("start")) == start
        and pd.Timestamp(item.get("end")) == end
        and item.get("filed") == filed
        and item.get("accn") == accession
        and item.get("form") in {"20-F", "20-F/A"}
    ]
    if len(matches) != 1:
        raise ValueError(
            f"INMD annual {concept} fact is not unique for {fiscal_end}: "
            f"{len(matches)}"
        )
    return matches[0]


def _fact_row(
    *,
    fiscal_end: str,
    available_date: str,
    metric: str,
    value: float,
    concept: str,
    form: str,
    accession: str,
    source_archive: str,
    source_sha256: str,
    derivation: str,
) -> dict:
    return {
        "ticker": "INMD",
        "fiscal_end": fiscal_end,
        "available_date": available_date,
        "metric": metric,
        "value": value,
        "taxonomy": "us-gaap",
        "concept": concept,
        "form": form,
        "accession": accession,
        "unit": "USD",
        "source": "sec_inmd_contemporaneous_financial_statement",
        "source_archive": source_archive,
        "source_archive_sha256": source_sha256,
        "derivation": derivation,
    }


def run(
    *,
    companyfacts_path: Path = DEFAULT_COMPANYFACTS,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> dict:
    wrapper = _load_companyfacts(companyfacts_path)
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    direct: dict[int, list[dict]] = {}
    rows = []
    sources = []
    parsed_by_end = {}
    for fiscal_end, filing in DIRECT_FILINGS.items():
        accession = str(filing["accession"])
        compact = accession.replace("-", "")
        url = f"{SEC_BASE}/{compact}/{filing['document']}"
        with urlopen(Request(url, headers=HEADERS), timeout=90) as response:
            raw = response.read()
        raw_path = raw_dir / f"{accession}_{filing['document']}"
        raw_path.write_bytes(raw)
        parsed = _parse_statement_columns(raw, fiscal_end)
        parsed_by_end[fiscal_end] = parsed
        values = parsed["current"]
        year = pd.Timestamp(fiscal_end).year
        direct.setdefault(year, []).append(values)
        derivation = "direct_three_month_sec_filing_gaap_statement"
        for metric, value in values.items():
            rows.append(_fact_row(
                fiscal_end=fiscal_end,
                available_date=str(filing["filed"]),
                metric=metric,
                value=value,
                concept="Revenues" if metric == "revenue" else "ProfitLoss",
                form=str(filing["form"]),
                accession=accession,
                source_archive=raw_path.name,
                source_sha256=_sha256(raw_path),
                derivation=derivation,
            ))
        sources.append({
            "fiscal_end": fiscal_end,
            "filed": filing["filed"],
            "form": filing["form"],
            "accession": accession,
            "url": url,
            "path": str(raw_path),
            "sha256": _sha256(raw_path),
        })

    q3_2019 = parsed_by_end["2019-09-30"]
    if q3_2019["cumulative"] is None:
        raise RuntimeError("INMD 2019 Q3 filing lacks original nine-month totals")
    q1_2019 = parsed_by_end["2019-03-31"]["current"]
    q2_2019 = {
        metric: q3_2019["cumulative"][metric]
        - q1_2019[metric]
        - q3_2019["current"][metric]
        for metric in ("revenue", "net_income")
    }
    q3_filing = DIRECT_FILINGS["2019-09-30"]
    q3_source = next(
        source
        for source in sources
        if source["fiscal_end"] == "2019-09-30"
    )
    direct[2019].append(q2_2019)
    for metric, value in q2_2019.items():
        rows.append(_fact_row(
            fiscal_end="2019-06-30",
            available_date=str(q3_filing["filed"]),
            metric=metric,
            value=value,
            concept=(
                "derived_9m:Revenues"
                if metric == "revenue"
                else "derived_9m:ProfitLoss"
            ),
            form="6-K",
            accession=str(q3_filing["accession"]),
            source_archive=Path(str(q3_source["path"])).name,
            source_sha256=str(q3_source["sha256"]),
            derivation="original_nine_month_6k_less_original_q1_and_q3",
        ))

    q1_2018 = parsed_by_end["2019-03-31"]["comparative"]
    q3_2018 = parsed_by_end["2019-09-30"]["comparative"]
    cumulative_2018 = parsed_by_end["2019-09-30"][
        "cumulative_comparative"
    ]
    if cumulative_2018 is None:
        raise RuntimeError("INMD 2019 Q3 filing lacks 2018 nine-month comparatives")
    q2_2018 = {
        metric: cumulative_2018[metric] - q1_2018[metric] - q3_2018[metric]
        for metric in ("revenue", "net_income")
    }
    direct[2018] = [q1_2018, q2_2018, q3_2018]
    comparative_quarters = (
        (
            "2018-03-31",
            q1_2018,
            DIRECT_FILINGS["2019-03-31"],
            next(
                source
                for source in sources
                if source["fiscal_end"] == "2019-03-31"
            ),
            "original_f1_comparative_three_month_statement",
        ),
        (
            "2018-06-30",
            q2_2018,
            q3_filing,
            q3_source,
            "original_comparative_nine_month_6k_less_q1_and_q3",
        ),
        (
            "2018-09-30",
            q3_2018,
            q3_filing,
            q3_source,
            "original_6k_comparative_three_month_statement",
        ),
    )
    for fiscal_end, values, filing, source, derivation in comparative_quarters:
        for metric, value in values.items():
            rows.append(_fact_row(
                fiscal_end=fiscal_end,
                available_date=str(filing["filed"]),
                metric=metric,
                value=value,
                concept=("Revenues" if metric == "revenue" else "ProfitLoss"),
                form=str(filing["form"]),
                accession=str(filing["accession"]),
                source_archive=Path(str(source["path"])).name,
                source_sha256=str(source["sha256"]),
                derivation=derivation,
            ))

    payload = wrapper["payload"]
    for fiscal_end, (filed, accession) in ANNUAL_FILINGS.items():
        year = pd.Timestamp(fiscal_end).year
        if len(direct.get(year, [])) != 3:
            raise RuntimeError(f"INMD {year} lacks three proven interim quarters")
        annual_revenue = _annual_fact(
            payload,
            concept="Revenues",
            fiscal_end=fiscal_end,
            filed=filed,
            accession=accession,
        )
        annual_income = _annual_fact(
            payload,
            concept="ProfitLoss",
            fiscal_end=fiscal_end,
            filed=filed,
            accession=accession,
        )
        for metric, concept, annual in (
            ("revenue", "Revenues", annual_revenue),
            ("net_income", "ProfitLoss", annual_income),
        ):
            value = float(annual["val"]) - sum(
                quarter[metric] for quarter in direct[year]
            )
            rows.append(_fact_row(
                fiscal_end=fiscal_end,
                available_date=filed,
                metric=metric,
                value=value,
                concept=f"derived_q4:{concept}",
                form="20-F",
                accession=accession,
                source_archive=companyfacts_path.name,
                source_sha256=_sha256(companyfacts_path),
                derivation="original_annual_20f_less_proven_q1_q2_q3",
            ))

    facts = pd.DataFrame(rows).sort_values(
        ["fiscal_end", "metric"]
    ).reset_index(drop=True)
    if (
        len(facts) != 32
        or facts["fiscal_end"].nunique() != 16
        or not facts.groupby("fiscal_end")["metric"].nunique().eq(2).all()
    ):
        raise RuntimeError("INMD recovery is not exactly sixteen paired quarters")
    paired = facts.pivot_table(
        index=["fiscal_end", "available_date"],
        columns="metric",
        values="value",
        aggfunc="first",
    ).reset_index()
    recovered = [
        {
            "ticker": "INMD",
            "fiscal_end": pd.Timestamp(row.fiscal_end).strftime("%Y-%m-%d"),
            "available_date": pd.Timestamp(row.available_date).strftime("%Y-%m-%d"),
            "revenue": float(row.revenue),
            "net_income": float(row.net_income),
        }
        for row in paired.itertuples(index=False)
    ]
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "parameters_frozen": False,
        "promotion_eligible": False,
        "release_status": "BLOCKED",
        "ticker": "INMD",
        "accepted_quarter_count": 16,
        "recovered_quarters": recovered,
        "filing_sources": sources,
        "companyfacts": {
            "path": str(companyfacts_path),
            "sha256": _sha256(companyfacts_path),
            "source_url": wrapper.get("source_url"),
            "fetched_at": wrapper.get("fetched_at"),
        },
        "outputs": {
            "quarters": {"path": str(facts_path), "sha256": _sha256(facts_path)}
        },
        "guardrail": (
            "2018 Q1/Q3 use the first SEC-hosted comparative GAAP columns, "
            "and 2018 Q2 uses the same filing's comparative nine-month total "
            "less Q1 and Q3. 2019 Q1 and all 2020-2021 Q1-Q3 values use the "
            "first available SEC-hosted GAAP three-month statement; 2019 Q2 "
            "is the original nine-month statement less Q1 and Q3. Each Q4 "
            "is the first matching 20-F annual fact less the three proven "
            "interim quarters. Non-GAAP rows, later annual revisions, and "
            "formal financial files are excluded."
        ),
    }
    manifest = output_dir / "manifest.json"
    manifest.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["manifest"] = str(manifest)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--companyfacts", type=Path, default=DEFAULT_COMPANYFACTS)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = run(companyfacts_path=args.companyfacts, output_dir=args.output_dir)
    print(json.dumps({
        "accepted_quarter_count": result["accepted_quarter_count"],
        "manifest": result["manifest"],
        "release_status": result["release_status"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
