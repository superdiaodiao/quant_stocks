#!/usr/bin/env python3
"""Recover CGBD PIT BDC quarters across its same-CIK issuer renames."""

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
    "output/research_only/v14/cgbd_sec_quarterly_filings_2015_2021"
)
SEC_BASE = "https://www.sec.gov/Archives/edgar/data/1544206"
HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
CIK = 1544206
ISSUER_NAMES = (
    "carlyle gms finance, inc.",
    "tcg bdc, inc.",
    "tcg bdc inc.",
    "carlyle secured lending, inc.",
)
REVENUE_LABELS = ("Total investment income",)
NET_INCOME_LABELS = (
    "Net increase (decrease) in net assets resulting from operations",
    "Net (decrease) increase in net assets resulting from operations",
    "Net increase in net assets resulting from operations",
    "Net decrease in net assets resulting from operations",
)

FILING_SPECS = (
    ("2015-03-31", "2015-05-08", "10-Q", "0001193125-15-179892", "d910346d10q.htm"),
    ("2015-06-30", "2015-08-12", "10-Q", "0001193125-15-288215", "d22833d10q.htm"),
    ("2015-09-30", "2015-11-06", "10-Q", "0001193125-15-370274", "d58654d10q.htm"),
    ("2015-12-31", "2016-03-11", "10-K", "0001193125-16-501694", "d154180d10k.htm"),
    ("2016-03-31", "2016-05-09", "10-Q", "0001193125-16-584027", "d357694d10q.htm"),
    ("2016-06-30", "2016-08-10", "10-Q", "0001193125-16-678210", "d208364d10q.htm"),
    ("2016-09-30", "2016-11-10", "10-Q", "0001193125-16-766113", "d277543d10q.htm"),
    ("2016-12-31", "2017-03-22", "10-K", "0001193125-17-090984", "d334761d10k.htm"),
    ("2017-03-31", "2017-05-10", "10-Q", "0001193125-17-166087", "d394686d10q.htm"),
    ("2017-06-30", "2017-08-09", "10-Q", "0001544206-17-000017", "tcgbdc-2q2017_10q.htm"),
    ("2017-09-30", "2017-11-07", "10-Q", "0001544206-17-000031", "tcgbdc-3q2017_10q.htm"),
    ("2017-12-31", "2018-02-27", "10-K", "0001544206-18-000010", "cgbd-2017x12x31x10k.htm"),
    ("2018-03-31", "2018-05-03", "10-Q", "0001544206-18-000041", "cgbd-1q2018_10q.htm"),
    ("2018-06-30", "2018-08-07", "10-Q", "0001544206-18-000052", "cgbd-2q2018_10q.htm"),
    ("2018-09-30", "2018-11-06", "10-Q", "0001544206-18-000062", "cgbd-3q2018_10q.htm"),
    ("2018-12-31", "2019-02-26", "10-K", "0001544206-19-000007", "cgbd-2018x12x31x10k.htm"),
    ("2019-03-31", "2019-05-07", "10-Q", "0001544206-19-000018", "cgbd-1q2019_10q.htm"),
    ("2019-06-30", "2019-08-06", "10-Q", "0001544206-19-000048", "cgbd-2q2019_10q.htm"),
    ("2019-09-30", "2019-11-05", "10-Q", "0001544206-19-000059", "cgbd-3q2019_10q.htm"),
    ("2019-12-31", "2020-02-25", "10-K", "0001544206-20-000009", "cgbd_20191231x10-kxdocument.htm"),
    ("2020-03-31", "2020-05-05", "10-Q", "0001544206-20-000035", "cgbd_1q20x10-qxdocument.htm"),
    ("2020-06-30", "2020-08-05", "10-Q", "0001544206-20-000060", "cgbd2q2010-qdocument.htm"),
    ("2020-09-30", "2020-11-04", "10-Q", "0001544206-20-000087", "cgbd3q2010-qdocument.htm"),
    ("2020-12-31", "2021-02-23", "10-K", "0001544206-21-000009", "cgbd_20201231x10-kxdocument.htm"),
    ("2021-03-31", "2021-05-04", "10-Q", "0001544206-21-000029", "cgbd_1q21x10-qxdocument.htm"),
    ("2021-06-30", "2021-08-03", "10-Q", "0001544206-21-000039", "cgbd_2q21x10-qxdocument.htm"),
    ("2021-09-30", "2021-11-02", "10-Q", "0001544206-21-000058", "cgbd_3q21x10-qxdocument.htm"),
    ("2021-12-31", "2022-02-22", "10-K", "0001544206-22-000009", "cgbd_20211231x10-kxdocument.htm"),
)


def validate_filing_specs() -> None:
    frame = pd.DataFrame(
        FILING_SPECS,
        columns=["fiscal_end", "filed", "form", "accession", "document"],
    )
    frame["fiscal_end"] = pd.to_datetime(frame["fiscal_end"])
    frame["filed"] = pd.to_datetime(frame["filed"])
    if len(frame) != 28 or frame["fiscal_end"].duplicated().any():
        raise ValueError("CGBD filing chain must contain 28 unique quarters")
    if _longest_chain(frame["fiscal_end"].tolist()) != 28:
        raise ValueError("CGBD filing specifications are not quarterly-continuous")
    year_end = frame["fiscal_end"].dt.month.eq(12)
    if not frame.loc[year_end, "form"].eq("10-K").all():
        raise ValueError("CGBD December fiscal year ends must use 10-K filings")
    if not frame.loc[~year_end, "form"].eq("10-Q").all():
        raise ValueError("CGBD interim periods must use 10-Q filings")
    if not (frame["filed"] - frame["fiscal_end"]).dt.days.between(0, 150).all():
        raise ValueError("CGBD filing specifications contain an untimely filing")


def _cgbd_period_columns(
    table: pd.DataFrame,
    *,
    period_end: pd.Timestamp,
    annual: bool,
) -> list:
    phrase_columns = set()
    target_columns = set()
    target_date = _normal(period_end.strftime("%B %d, %Y").replace(" 0", " "))
    for _, row in table.head(12).iterrows():
        for column, value in row.items():
            text = _normal(value)
            if annual:
                if "years ended" in text or "year ended" in text:
                    phrase_columns.add(column)
                if text == str(period_end.year):
                    target_columns.add(column)
            else:
                if "three month period" in text:
                    phrase_columns.add(column)
                if text == target_date:
                    target_columns.add(column)
    selected = [
        column for column in table.columns
        if column in phrase_columns and column in target_columns
    ]
    if not selected:
        kind = "annual" if annual else "direct-quarter"
        raise ValueError(f"CGBD table has no {kind} column for {period_end.date()}")
    return selected


def parse_cgbd_statement(
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
    if not any(name in document for name in ISSUER_NAMES):
        raise ValueError(f"CGBD same-CIK issuer identity is not proven: {path}")
    if "in thousands" not in document:
        raise ValueError(f"CGBD filing does not prove thousands units: {path}")
    candidates = []
    for table in pd.read_html(path):
        # Full statement of operations only; compact MD&A highlights are not
        # an accounting source and may contain rounded values.
        if len(table) < 30:
            continue
        first = table.iloc[:, 0].fillna("").map(_normal)
        if not first.isin({_normal(label) for label in REVENUE_LABELS}).any():
            continue
        if not first.isin({_normal(label) for label in NET_INCOME_LABELS}).any():
            continue
        try:
            columns = _cgbd_period_columns(
                table, period_end=period_end, annual=annual
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
            f"expected one agreeing CGBD statement pair in {path}, "
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
            annual = parse_cgbd_statement(path, period_end=end, annual=True)
            prior = [
                row for row in recovered
                if end - pd.DateOffset(months=9) <= pd.Timestamp(row["fiscal_end"]) < end
            ]
            if len(prior) != 3:
                raise RuntimeError(f"CGBD {fiscal_end} Q4 has {len(prior)} prior quarters")
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
            values = parse_cgbd_statement(path, period_end=end, annual=False)
            derivation = "direct_original_sec_three_month_statement"
        if values["revenue"] <= 0:
            raise ValueError(f"CGBD {fiscal_end} revenue is not positive")
        lag_days = (pd.Timestamp(filed) - end).days
        common = {
            "ticker": "CGBD", "fiscal_end": fiscal_end,
            "available_date": filed, "taxonomy": "us-gaap", "form": form,
            "accession": accession, "unit": "USD",
            "source": "sec_cgbd_same_cik_original_quarterly_statement",
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
            "ticker": "CGBD", "fiscal_end": fiscal_end,
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
    if len(paired) != 28 or not paired.eq(2).all():
        raise RuntimeError("CGBD output is not exactly 28 paired quarters")
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1, "research_only": True,
        "point_in_time_proven": True, "parameters_frozen": False,
        "promotion_eligible": False, "release_status": "BLOCKED",
        "ticker": "CGBD", "cik": CIK, "currency": "USD",
        "quarter_count": 28, "direct_quarter_count": 21,
        "derived_q4_count": 7,
        "same_cik_former_name_chain": list(ISSUER_NAMES),
        "predecessor_entity_join_used": False,
        "longest_continuous_timely_paired_quarters": 28,
        "recovered_quarters": recovered,
        "annual_identity_checks": annual_checks,
        "filings": bindings,
        "outputs": {"quarters": {
            "path": str(facts_path), "sha256": _sha256(facts_path),
        }},
        "guardrail": (
            "All periods use CIK 1544206. Carlyle GMS Finance, TCG BDC, and "
            "Carlyle Secured Lending are SEC-recorded former names of the same "
            "issuer; no predecessor-company join is used. Q1-Q3 are original "
            "three-month values and Q4 is audited annual less those original "
            "PIT quarters. Formal data and trading remain blocked."
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
        "predecessor_entity_join_used": result["predecessor_entity_join_used"],
        "release_status": result["release_status"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
