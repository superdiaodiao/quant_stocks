#!/usr/bin/env python3
"""Recover NEWT's pre-bank 2017-2021 PIT BDC quarters from SEC filings."""

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
    _period_columns,
    _row_value,
    _sha256,
)


DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/newt_prebank_quarterly_filings_2017_2021"
)
SEC_BASE = "https://www.sec.gov/Archives/edgar/data/1587987"
HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
PREBANK_CUTOFF = pd.Timestamp("2021-12-31")
REVENUE_LABELS = ("Total investment income",)
NET_INCOME_LABELS = (
    "Net increase in net assets resulting from operations",
    "Net increase (decrease) in net assets resulting from operations",
    "Net (decrease) increase in net assets resulting from operations",
    "Net decrease in net assets resulting from operations",
    # NEWT's 2017 statement of operations abbreviated the final operating
    # result row; this label is accepted only in a table that also contains
    # the unique Total investment income row.
    "Net increase in net assets",
    "Net decrease in net assets",
)

FILING_SPECS = (
    ("2017-03-31", "2017-05-08", "10-Q", "0001587987-17-000034", "newt-33117x10q.htm"),
    ("2017-06-30", "2017-08-07", "10-Q", "0001587987-17-000050", "newt-63017x10q.htm"),
    ("2017-09-30", "2017-11-07", "10-Q", "0001587987-17-000064", "newt-93017x10q.htm"),
    ("2017-12-31", "2018-03-16", "10-K", "0001587987-18-000013", "newt-123117x10k.htm"),
    ("2018-03-31", "2018-05-09", "10-Q", "0001587987-18-000025", "newt-33118x10q.htm"),
    ("2018-06-30", "2018-08-08", "10-Q", "0001587987-18-000052", "newt-63018x10q.htm"),
    ("2018-09-30", "2018-11-09", "10-Q", "0001587987-18-000071", "newt-93018x10q.htm"),
    ("2018-12-31", "2019-03-18", "10-K", "0001587987-19-000009", "newt-123118x10k.htm"),
    ("2019-03-31", "2019-05-03", "10-Q", "0001587987-19-000029", "newt-33119x10q.htm"),
    ("2019-06-30", "2019-08-09", "10-Q", "0001587987-19-000051", "newt-63019x10q.htm"),
    ("2019-09-30", "2019-11-12", "10-Q", "0001587987-19-000065", "newt-93019x10q.htm"),
    ("2019-12-31", "2020-03-16", "10-K", "0001587987-20-000013", "newt-123119x10k.htm"),
    ("2020-03-31", "2020-05-11", "10-Q", "0001587987-20-000023", "newt3312010q.htm"),
    ("2020-06-30", "2020-08-10", "10-Q", "0001587987-20-000051", "newt6302010qnextgen.htm"),
    ("2020-09-30", "2020-11-12", "10-Q", "0001587987-20-000068", "newt9302010q.htm"),
    ("2020-12-31", "2021-03-29", "10-K", "0001587987-21-000047", "newt12312010k.htm"),
    ("2021-03-31", "2021-05-13", "10-Q", "0001587987-21-000063", "newt3312110q.htm"),
    ("2021-06-30", "2021-08-13", "10-Q", "0001587987-21-000125", "newt6302110q.htm"),
    ("2021-09-30", "2021-11-12", "10-Q", "0001587987-21-000144", "newt9302110q.htm"),
    ("2021-12-31", "2022-03-01", "10-K", "0001587987-22-000010", "newt12312110k.htm"),
)


def validate_filing_specs() -> None:
    frame = pd.DataFrame(
        FILING_SPECS,
        columns=["fiscal_end", "filed", "form", "accession", "document"],
    )
    frame["fiscal_end"] = pd.to_datetime(frame["fiscal_end"])
    frame["filed"] = pd.to_datetime(frame["filed"])
    if len(frame) != 20 or frame["fiscal_end"].duplicated().any():
        raise ValueError("NEWT pre-bank chain must contain 20 unique quarters")
    if frame["fiscal_end"].max() != PREBANK_CUTOFF:
        raise ValueError("NEWT pre-bank chain must stop at 2021-12-31")
    if (frame["fiscal_end"] > PREBANK_CUTOFF).any():
        raise ValueError("NEWT bank-era period is forbidden in BDC recovery")
    if _longest_chain(frame["fiscal_end"].tolist()) != 20:
        raise ValueError("NEWT pre-bank chain is not quarterly-continuous")
    year_end = frame["fiscal_end"].dt.month.eq(12)
    if not frame.loc[year_end, "form"].eq("10-K").all():
        raise ValueError("NEWT year-end periods must use 10-K filings")
    if not frame.loc[~year_end, "form"].eq("10-Q").all():
        raise ValueError("NEWT interim periods must use 10-Q filings")
    if not (frame["filed"] - frame["fiscal_end"]).dt.days.between(0, 150).all():
        raise ValueError("NEWT filing specifications contain an untimely filing")


def _newt_period_columns(
    table: pd.DataFrame,
    *,
    year: int,
    period_phrase: str,
) -> list:
    try:
        return _period_columns(
            table, year=year, period_phrase=period_phrase
        )
    except ValueError:
        phrase = period_phrase.casefold()
        combined = set()
        for _, row in table.head(8).iterrows():
            for column, value in row.items():
                text = _normal(value)
                if phrase in text and str(year) in text:
                    combined.add(column)
        selected = [column for column in table.columns if column in combined]
        if not selected:
            raise ValueError(
                f"NEWT table has no {period_phrase!r} {year} value column"
            )
        return selected


def parse_newt_bdc_statement(
    path: Path,
    *,
    period_end: pd.Timestamp,
    annual: bool,
) -> dict[str, float]:
    if pd.Timestamp(period_end) > PREBANK_CUTOFF:
        raise ValueError("NEWT bank-era period is forbidden in BDC parser")
    document = " ".join(
        BeautifulSoup(path.read_bytes(), "html.parser")
        .get_text(" ", strip=True)
        .split()
    ).casefold()
    if "newtek business services corp" not in document:
        raise ValueError(f"NEWT pre-bank issuer identity is not proven: {path}")
    if "in thousands" not in document:
        raise ValueError(f"NEWT filing does not prove thousands units: {path}")
    candidates = []
    phrase = "Year ended" if annual else "Three Months Ended"
    for table in pd.read_html(path):
        first = table.iloc[:, 0].fillna("").map(_normal)
        if not first.isin({_normal(label) for label in REVENUE_LABELS}).any():
            continue
        if not first.isin({_normal(label) for label in NET_INCOME_LABELS}).any():
            continue
        try:
            columns = _newt_period_columns(
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
            f"expected one agreeing NEWT BDC statement pair in {path}, "
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
            annual = parse_newt_bdc_statement(path, period_end=end, annual=True)
            prior = [
                row for row in recovered
                if end - pd.DateOffset(months=9) <= pd.Timestamp(row["fiscal_end"]) < end
            ]
            if len(prior) != 3:
                raise RuntimeError(f"NEWT {fiscal_end} Q4 has {len(prior)} prior quarters")
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
            derivation = "annual_minus_original_pit_q1_q2_q3"
        else:
            values = parse_newt_bdc_statement(path, period_end=end, annual=False)
            derivation = "direct_original_sec_three_month_statement"
        if values["revenue"] <= 0:
            raise ValueError(f"NEWT {fiscal_end} revenue is not positive")
        lag_days = (pd.Timestamp(filed) - end).days
        common = {
            "ticker": "NEWT", "fiscal_end": fiscal_end,
            "available_date": filed, "taxonomy": "us-gaap", "form": form,
            "accession": accession, "unit": "USD",
            "source": "sec_newt_prebank_original_quarterly_statement",
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
            "ticker": "NEWT", "fiscal_end": fiscal_end,
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
    if len(paired) != 20 or not paired.eq(2).all():
        raise RuntimeError("NEWT output is not exactly 20 paired quarters")
    if pd.to_datetime(facts["fiscal_end"]).max() > PREBANK_CUTOFF:
        raise RuntimeError("NEWT BDC output contains a bank-era period")
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1, "research_only": True,
        "point_in_time_proven": True, "parameters_frozen": False,
        "promotion_eligible": False, "release_status": "BLOCKED",
        "ticker": "NEWT", "currency": "USD", "quarter_count": 20,
        "direct_quarter_count": 15, "derived_q4_count": 5,
        "business_model": "BDC_PRE_BANK_CONVERSION",
        "maximum_accepted_fiscal_end": str(PREBANK_CUTOFF.date()),
        "post_conversion_periods_accepted": 0,
        "longest_continuous_timely_paired_quarters": 20,
        "recovered_quarters": recovered,
        "annual_identity_checks": annual_checks,
        "filings": bindings,
        "outputs": {"quarters": {
            "path": str(facts_path), "sha256": _sha256(facts_path),
        }},
        "guardrail": (
            "Only NEWT's original pre-conversion BDC USD-thousands statements "
            "through 2021-12-31 are accepted. Q1-Q3 are direct three-month "
            "values and Q4 is audited annual less the earlier original PIT "
            "quarters. No post-conversion bank revenue concept may extend or "
            "restate this BDC sequence. Formal data and trading remain blocked."
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
        "post_conversion_periods_accepted": result["post_conversion_periods_accepted"],
        "release_status": result["release_status"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
