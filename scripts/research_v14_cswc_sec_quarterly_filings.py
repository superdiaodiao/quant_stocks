#!/usr/bin/env python3
"""Recover CSWC PIT BDC quarters across its March fiscal year boundary."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd
from bs4 import BeautifulSoup

from scripts.research_v14_dsgx_sec_quarterly_filings import (
    _longest_chain,
    _normal,
    _row_value,
    _sha256,
)


DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/cswc_sec_quarterly_filings_2016_2021"
)
SEC_BASE = "https://www.sec.gov/Archives/edgar/data/17313"
HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
REVENUE_LABELS = ("Total investment income",)
NET_INCOME_LABELS = (
    "Net increase in net assets from operations",
    "Net decrease in net assets from operations",
    "Net increase (decrease) in net assets from operations",
    "Net (decrease) increase in net assets from operations",
    "Net increase in net assets resulting from operations",
    "Net decrease in net assets resulting from operations",
    "Net increase (decrease) in net assets resulting from operations",
    "Net (decrease) increase in net assets resulting from operations",
)

# CSWC's fiscal year ends March 31.  The sequence starts with fiscal Q1 ending
# June 30, 2016 and contains five complete fiscal years through March 31, 2021.
FILING_SPECS = (
    ("2016-06-30", "2016-08-09", "10-Q", "0000017313-16-000009", "cswc-20160630x10q.htm"),
    ("2016-09-30", "2016-11-08", "10-Q", "0000017313-16-000023", "cswc-20160930x10q.htm"),
    ("2016-12-31", "2017-02-07", "10-Q", "0001558370-17-000369", "cswc-20161231x10q.htm"),
    ("2017-03-31", "2017-06-01", "10-K", "0000017313-17-000012", "cswc-20170331x10k.htm"),
    ("2017-06-30", "2017-08-08", "10-Q", "0000017313-17-000021", "cswc-20170630x10q.htm"),
    ("2017-09-30", "2017-11-07", "10-Q", "0000017313-17-000039", "cswc-20170930x10q.htm"),
    ("2017-12-31", "2018-02-06", "10-Q", "0000017313-18-000007", "cswc-20171231x10q.htm"),
    ("2018-03-31", "2018-06-05", "10-K", "0000017313-18-000023", "cswc-20180331x10k.htm"),
    ("2018-06-30", "2018-08-07", "10-Q", "0000017313-18-000035", "cswc6301810q.htm"),
    ("2018-09-30", "2018-11-07", "10-Q", "0000017313-18-000057", "cswc9301810q.htm"),
    ("2018-12-31", "2019-02-05", "10-Q", "0000017313-19-000008", "cswc12311810q.htm"),
    ("2019-03-31", "2019-06-04", "10-K", "0000017313-19-000024", "cswc3311910-k.htm"),
    ("2019-06-30", "2019-08-06", "10-Q", "0000017313-19-000053", "cswc6301910q.htm"),
    ("2019-09-30", "2019-11-05", "10-Q", "0000017313-19-000098", "cswc9301910q.htm"),
    ("2019-12-31", "2020-02-04", "10-Q", "0000017313-20-000004", "cswc12311910q.htm"),
    ("2020-03-31", "2020-06-02", "10-K", "0000017313-20-000051", "cswc3312010-k.htm"),
    ("2020-06-30", "2020-08-04", "10-Q", "0000017313-20-000079", "cswc6302010q.htm"),
    ("2020-09-30", "2020-11-02", "10-Q", "0000017313-20-000105", "cswc9302010q.htm"),
    ("2020-12-31", "2021-02-02", "10-Q", "0000017313-21-000006", "cswc12312010q.htm"),
    ("2021-03-31", "2021-05-26", "10-K", "0000017313-21-000075", "cswc3312110-k.htm"),
    ("2021-06-30", "2021-08-03", "10-Q", "0000017313-21-000107", "cswc6302110q.htm"),
    ("2021-09-30", "2021-11-02", "10-Q", "0000017313-21-000169", "cswc9302110q.htm"),
    ("2021-12-31", "2022-02-01", "10-Q", "0000017313-22-000010", "cswc12312110q.htm"),
)


def validate_filing_specs() -> None:
    frame = pd.DataFrame(
        FILING_SPECS,
        columns=["fiscal_end", "filed", "form", "accession", "document"],
    )
    frame["fiscal_end"] = pd.to_datetime(frame["fiscal_end"])
    frame["filed"] = pd.to_datetime(frame["filed"])
    if len(frame) != 23 or frame["fiscal_end"].duplicated().any():
        raise ValueError("CSWC filing chain must contain 23 unique quarters")
    if _longest_chain(frame["fiscal_end"].tolist()) != 23:
        raise ValueError("CSWC filing specifications are not quarterly-continuous")
    year_end = frame["fiscal_end"].dt.month.eq(3)
    if not frame.loc[year_end, "form"].eq("10-K").all():
        raise ValueError("CSWC March fiscal year ends must use 10-K filings")
    if not frame.loc[~year_end, "form"].eq("10-Q").all():
        raise ValueError("CSWC non-March periods must use 10-Q filings")
    if not (frame["filed"] - frame["fiscal_end"]).dt.days.between(0, 150).all():
        raise ValueError("CSWC filing specifications contain an untimely filing")


def _cswc_period_columns(
    table: pd.DataFrame,
    *,
    year: int,
    period_phrase: str,
) -> list:
    # CSWC tables include issuer/title/unit rows before their multi-row headers,
    # placing the year below the eight-row window used by the shared parser.
    phrase_columns = set()
    year_columns = set()
    for _, row in table.head(15).iterrows():
        for column, value in row.items():
            text = _normal(value)
            if period_phrase.casefold() in text:
                phrase_columns.add(column)
            if text == str(year):
                year_columns.add(column)
    selected = [
        column for column in table.columns
        if column in phrase_columns and column in year_columns
    ]
    if not selected:
        raise ValueError(
            f"CSWC table has no {period_phrase!r} {year} value column"
        )
    return selected


def parse_cswc_statement(
    path: Path,
    *,
    period_end: pd.Timestamp,
    annual: bool,
) -> dict[str, float]:
    document = " ".join(
        BeautifulSoup(path.read_bytes(), "html.parser")
        .get_text(" ", strip=True)
        .split()
    ).casefold()
    if "capital southwest corporation" not in document:
        raise ValueError(f"CSWC filing issuer identity is not proven: {path}")
    if "in thousands" not in document:
        raise ValueError(f"CSWC filing does not prove thousands units: {path}")
    candidates = []
    phrase = "Year ended" if annual else "Three Months Ended"
    for table in pd.read_html(path):
        # CSWC filings repeat rounded statement highlights in compact MD&A
        # tables.  The complete statement of operations has the detailed
        # income/expense and gain/loss rows; compact summaries can differ by
        # one thousand dollars and are not the accounting source of record.
        if len(table) < 30:
            continue
        first = table.iloc[:, 0].fillna("").map(_normal)
        if not first.isin({_normal(label) for label in REVENUE_LABELS}).any():
            continue
        if not first.isin({_normal(label) for label in NET_INCOME_LABELS}).any():
            continue
        try:
            columns = _cswc_period_columns(
                table, year=period_end.year, period_phrase=phrase
            )
            values = {
                "revenue": _row_value(
                    table, labels=REVENUE_LABELS, columns=columns
                ),
                "net_income": _row_value(
                    table, labels=NET_INCOME_LABELS, columns=columns
                ),
            }
        except ValueError:
            continue
        if values["revenue"] > 1_000:
            candidates.append(values)
    unique = {
        (values["revenue"], values["net_income"])
        for values in candidates
    }
    if len(unique) != 1:
        raise ValueError(
            f"expected one agreeing CSWC statement pair in {path}, "
            f"found {sorted(unique)}"
        )
    revenue, net_income = next(iter(unique))
    return {"revenue": revenue * 1000.0, "net_income": net_income * 1000.0}


def run(*, output_dir: Path = DEFAULT_OUTPUT_DIR) -> dict:
    validate_filing_specs()
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    recovered = []
    rows = []
    bindings = []
    annual_checks = []
    for fiscal_end, filed, form, accession, document in FILING_SPECS:
        end = pd.Timestamp(fiscal_end)
        path = raw_dir / f"{accession}_{document}"
        url = f"{SEC_BASE}/{accession.replace('-', '')}/{document}"
        if not path.exists():
            with urlopen(Request(url, headers=HEADERS), timeout=120) as response:
                path.write_bytes(response.read())
        prior_accessions = ""
        if form == "10-K":
            annual = parse_cswc_statement(path, period_end=end, annual=True)
            prior = [
                row for row in recovered
                if end - pd.DateOffset(months=9) <= pd.Timestamp(row["fiscal_end"]) < end
            ]
            if len(prior) != 3:
                raise RuntimeError(f"CSWC {fiscal_end} Q4 has {len(prior)} prior quarters")
            values = {
                metric: annual[metric] - sum(row[metric] for row in prior)
                for metric in ("revenue", "net_income")
            }
            prior_accessions = ";".join(row["accession"] for row in prior)
            annual_checks.append({
                "fiscal_end": fiscal_end, "annual": annual,
                "prior_three_quarter_sum": {
                    metric: sum(row[metric] for row in prior)
                    for metric in ("revenue", "net_income")
                },
                "derived_q4": values, "exact_arithmetic_identity": True,
            })
            derivation = "march_fiscal_annual_minus_original_pit_q1_q2_q3"
        else:
            values = parse_cswc_statement(path, period_end=end, annual=False)
            derivation = "direct_original_sec_three_month_statement"
        if values["revenue"] <= 0:
            raise ValueError(f"CSWC {fiscal_end} revenue is not positive")
        lag_days = (pd.Timestamp(filed) - end).days
        common = {
            "ticker": "CSWC", "fiscal_end": fiscal_end,
            "available_date": filed, "taxonomy": "us-gaap", "form": form,
            "accession": accession, "unit": "USD",
            "source": "sec_cswc_original_quarterly_statement",
            "source_archive": path.name, "source_archive_sha256": _sha256(path),
            "derivation": derivation,
            "derivation_prior_accession": prior_accessions,
        }
        for metric, concept in (
            ("revenue", "GrossInvestmentIncomeOperating"),
            ("net_income", "NetIncreaseDecreaseInNetAssetsResultingFromOperations"),
        ):
            rows.append({**common, "metric": metric, "value": values[metric],
                         "concept": concept})
        recovered.append({
            "ticker": "CSWC", "fiscal_end": fiscal_end,
            "available_date": filed, "availability_lag_days": lag_days,
            **values, "derivation": derivation, "accession": accession,
        })
        bindings.append({
            "fiscal_end": fiscal_end, "filed_date": filed,
            "accession": accession, "path": str(path), "sha256": _sha256(path),
            "source_url": url,
        })
    facts = pd.DataFrame(rows).sort_values(["fiscal_end", "metric"])
    paired = facts.groupby("fiscal_end")["metric"].nunique()
    if len(paired) != 23 or not paired.eq(2).all():
        raise RuntimeError("CSWC output is not exactly 23 paired quarters")
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1, "research_only": True,
        "point_in_time_proven": True, "parameters_frozen": False,
        "promotion_eligible": False, "release_status": "BLOCKED",
        "ticker": "CSWC", "currency": "USD", "quarter_count": 23,
        "fiscal_year_end_month": 3,
        "direct_quarter_count": 18, "derived_q4_count": 5,
        "longest_continuous_timely_paired_quarters": 23,
        "recovered_quarters": recovered,
        "annual_identity_checks": annual_checks,
        "filings": bindings,
        "outputs": {"quarters": {
            "path": str(facts_path), "sha256": _sha256(facts_path),
        }},
        "guardrail": (
            "CSWC fiscal years end March 31. Only original USD-thousands SEC "
            "statements are accepted: June, September, and December are direct "
            "three-month fiscal Q1-Q3 values; March Q4 is the audited fiscal "
            "annual statement less those original PIT quarters. Calendar-year "
            "Q4 assignment is forbidden. Formal data and trading remain blocked."
        ),
    }
    manifest = output_dir / "manifest.json"
    manifest.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["manifest"] = str(manifest)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = run(output_dir=args.output_dir)
    print(json.dumps({
        "manifest": result["manifest"], "quarter_count": result["quarter_count"],
        "fiscal_year_end_month": result["fiscal_year_end_month"],
        "release_status": result["release_status"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
