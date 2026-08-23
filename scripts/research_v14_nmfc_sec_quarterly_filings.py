#!/usr/bin/env python3
"""Recover NMFC 2017-2021 PIT quarters from original SEC statements."""

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
from scripts.research_v14_slrc_sec_quarterly_filings import _direct_columns


DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/nmfc_sec_quarterly_filings_2017_2021"
)
SEC_BASE = "https://www.sec.gov/Archives/edgar/data/1496099"
HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
ISSUER_FRAGMENTS = (
    "new mountain finance corporation",
    "new mountain finance corp",
)
REVENUE_LABELS = ("Total investment income",)
PARENT_NET_INCOME_LABELS = (
    "Net (decrease) increase in net assets resulting from operations related to New Mountain Finance Corporation",
    "Net increase (decrease) in net assets resulting from operations related to New Mountain Finance Corporation",
    "Net increase in net assets resulting from operations related to New Mountain Finance Corporation",
)
GENERIC_NET_INCOME_LABELS = (
    "Net increase in net assets resulting from operations",
    "Net increase (decrease) in net assets resulting from operations",
    "Net (decrease) increase in net assets resulting from operations",
    "Net decrease in net assets resulting from operations",
)

FILING_SPECS = (
    ("2017-03-31", "2017-05-08", "10-Q", "0001496099-17-000007", "nmfc-033117x10q.htm"),
    ("2017-06-30", "2017-08-08", "10-Q", "0001496099-17-000009", "nmfc-063017x10q.htm"),
    ("2017-09-30", "2017-11-07", "10-Q", "0001496099-17-000011", "nmfc-093017x10q.htm"),
    ("2017-12-31", "2018-02-28", "10-K", "0001496099-18-000002", "nmfc-12312017x10k.htm"),
    ("2018-03-31", "2018-05-07", "10-Q", "0001496099-18-000006", "nmfc-033118x10q.htm"),
    ("2018-06-30", "2018-08-07", "10-Q", "0001496099-18-000008", "nmfc-063018x10q.htm"),
    ("2018-09-30", "2018-11-07", "10-Q", "0001496099-18-000010", "nmfc-093018x10q.htm"),
    ("2018-12-31", "2019-02-27", "10-K", "0001496099-19-000003", "nmfc-12312018x10k.htm"),
    ("2019-03-31", "2019-05-06", "10-Q", "0001496099-19-000010", "nmfc-033119x10q.htm"),
    ("2019-06-30", "2019-08-07", "10-Q", "0001496099-19-000012", "nmfc-063019x10q.htm"),
    ("2019-09-30", "2019-11-06", "10-Q", "0001496099-19-000014", "nmfc-093019x10q.htm"),
    ("2019-12-31", "2020-02-26", "10-K", "0001496099-20-000002", "nmfc-12312019x10k.htm"),
    ("2020-03-31", "2020-05-06", "10-Q", "0001496099-20-000004", "nmfc-033120x10q.htm"),
    ("2020-06-30", "2020-08-05", "10-Q", "0001496099-20-000007", "nmfc-063020x10q.htm"),
    ("2020-09-30", "2020-11-04", "10-Q", "0001496099-20-000010", "nmfc-093020x10q.htm"),
    ("2020-12-31", "2021-02-24", "10-K", "0001496099-21-000003", "nmfc-12312020x10k.htm"),
    ("2021-03-31", "2021-05-05", "10-Q", "0001496099-21-000006", "nmfc-033121x10q.htm"),
    ("2021-06-30", "2021-08-04", "10-Q", "0001496099-21-000011", "nmfc-063021x10q.htm"),
    ("2021-09-30", "2021-11-03", "10-Q", "0001496099-21-000015", "nmfc-093021x10q.htm"),
    ("2021-12-31", "2022-02-28", "10-K", "0001496099-22-000002", "nmfc-12312021x10k.htm"),
)


def validate_filing_specs() -> None:
    frame = pd.DataFrame(
        FILING_SPECS,
        columns=["fiscal_end", "filed", "form", "accession", "document"],
    )
    frame["fiscal_end"] = pd.to_datetime(frame["fiscal_end"])
    frame["filed"] = pd.to_datetime(frame["filed"])
    if len(frame) != 20 or frame["fiscal_end"].duplicated().any():
        raise ValueError("NMFC filing chain must contain 20 unique quarters")
    if _longest_chain(frame["fiscal_end"].tolist()) != 20:
        raise ValueError("NMFC filing specifications are not quarterly-continuous")
    year_end = frame["fiscal_end"].dt.month.eq(12)
    if not frame.loc[year_end, "form"].eq("10-K").all():
        raise ValueError("NMFC year-end periods must use 10-K filings")
    if not frame.loc[~year_end, "form"].eq("10-Q").all():
        raise ValueError("NMFC interim periods must use 10-Q filings")
    if not (frame["filed"] - frame["fiscal_end"]).dt.days.between(0, 150).all():
        raise ValueError("NMFC filing specifications contain an untimely filing")


def parse_nmfc_statement(
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
    if not any(fragment in document for fragment in ISSUER_FRAGMENTS):
        raise ValueError(f"NMFC filing issuer identity is not proven: {path}")
    if "in thousands" not in document:
        raise ValueError(f"NMFC filing does not prove thousands units: {path}")
    candidates = []
    for table in pd.read_html(path):
        first = table.iloc[:, 0].fillna("").map(_normal)
        if not first.isin({_normal(label) for label in REVENUE_LABELS}).any():
            continue
        parent_present = first.isin(
            {_normal(label) for label in PARENT_NET_INCOME_LABELS}
        ).any()
        net_labels = (
            PARENT_NET_INCOME_LABELS
            if parent_present else GENERIC_NET_INCOME_LABELS
        )
        if not first.isin({_normal(label) for label in net_labels}).any():
            continue
        try:
            columns = (
                _period_columns(
                    table, year=period_end.year, period_phrase="Year ended"
                )
                if annual else _direct_columns(table, period_end)
            )
            values = {
                "revenue": _row_value(
                    table, labels=REVENUE_LABELS, columns=columns
                ),
                "net_income": _row_value(
                    table, labels=net_labels, columns=columns
                ),
            }
        except ValueError:
            continue
        if values["revenue"] > 10_000:
            candidates.append(values)
    unique = {
        (values["revenue"], values["net_income"])
        for values in candidates
    }
    if len(unique) != 1:
        raise ValueError(
            f"expected one agreeing NMFC statement pair in {path}, "
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
            annual = parse_nmfc_statement(path, period_end=end, annual=True)
            prior = [
                row for row in recovered
                if end - pd.DateOffset(months=9) <= pd.Timestamp(row["fiscal_end"]) < end
            ]
            if len(prior) != 3:
                raise RuntimeError(f"NMFC {fiscal_end} Q4 has {len(prior)} prior quarters")
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
            values = parse_nmfc_statement(path, period_end=end, annual=False)
            derivation = "direct_original_sec_three_month_statement"
        if values["revenue"] <= 0:
            raise ValueError(f"NMFC {fiscal_end} revenue is not positive")
        lag_days = (pd.Timestamp(filed) - end).days
        common = {
            "ticker": "NMFC", "fiscal_end": fiscal_end,
            "available_date": filed, "taxonomy": "us-gaap", "form": form,
            "accession": accession, "unit": "USD",
            "source": "sec_nmfc_original_quarterly_statement",
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
            "ticker": "NMFC", "fiscal_end": fiscal_end,
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
        raise RuntimeError("NMFC output is not exactly 20 paired quarters")
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1, "research_only": True,
        "point_in_time_proven": True, "parameters_frozen": False,
        "promotion_eligible": False, "release_status": "BLOCKED",
        "ticker": "NMFC", "currency": "USD", "quarter_count": 20,
        "direct_quarter_count": 15, "derived_q4_count": 5,
        "longest_continuous_timely_paired_quarters": 20,
        "recovered_quarters": recovered,
        "annual_identity_checks": annual_checks,
        "filings": bindings,
        "outputs": {"quarters": {
            "path": str(facts_path), "sha256": _sha256(facts_path),
        }},
        "guardrail": (
            "Only original NMFC SEC-filed USD-thousands statements are used. "
            "Q1-Q3 are direct three-month values; Q4 is audited annual less "
            "the three earlier original PIT quarters. Later comparatives are "
            "not backdated. Formal fundamentals and trading state are untouched."
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
        "release_status": result["release_status"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
