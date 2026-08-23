#!/usr/bin/env python3
"""Recover SLRC 2017-2021 PIT quarters from original SEC statements."""

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
from scripts.research_v14_gbdc_sec_quarterly_filings import _date_columns


DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/slrc_sec_quarterly_filings_2017_2021"
)
SEC_BASE = "https://www.sec.gov/Archives/edgar/data/1418076"
HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
METRIC_LABELS = {
    "revenue": ("Total investment income",),
    "net_income": (
        "Net increase in net assets resulting from operations",
        "Net increase (decrease) in net assets resulting from operations",
    ),
}


def _direct_columns(table: pd.DataFrame, period_end: pd.Timestamp) -> list:
    target = _normal(period_end.strftime("%B %d, %Y").replace(" 0", " "))
    phrase_columns = set()
    date_columns = set()
    for _, row in table.head(8).iterrows():
        for column, value in row.items():
            text = _normal(value)
            if "three months ended" in text:
                phrase_columns.add(column)
            if text == target:
                date_columns.add(column)
    selected = [
        column for column in table.columns
        if column in phrase_columns and column in date_columns
    ]
    if selected:
        return selected
    header = " ".join(
        _normal(value) for value in table.head(8).to_numpy().ravel()
    )
    if "three months ended" in header and not any(
        phrase in header
        for phrase in ("six months ended", "nine months ended", "year ended")
    ):
        return _date_columns(table, period_end)
    raise ValueError(f"SLRC table has no direct three-month column for {target}")

# Fiscal end, filed date, form, accession, primary document.  This is the
# complete contemporaneous 2017-2021 10-Q/10-K chain from SEC submissions.
FILING_SPECS = (
    ("2017-03-31", "2017-05-02", "10-Q", "0001193125-17-154010", "d376040d10q.htm"),
    ("2017-06-30", "2017-08-01", "10-Q", "0001193125-17-244350", "d395032d10q.htm"),
    ("2017-09-30", "2017-11-02", "10-Q", "0001193125-17-331039", "d475322d10q.htm"),
    ("2017-12-31", "2018-02-22", "10-K", "0001193125-18-054158", "d523003d10k.htm"),
    ("2018-03-31", "2018-05-07", "10-Q", "0001193125-18-154565", "d551740d10q.htm"),
    ("2018-06-30", "2018-08-06", "10-Q", "0001193125-18-239799", "d602115d10q.htm"),
    ("2018-09-30", "2018-11-05", "10-Q", "0001193125-18-318416", "d649185d10q.htm"),
    ("2018-12-31", "2019-02-21", "10-K", "0001193125-19-047052", "d669184d10k.htm"),
    ("2019-03-31", "2019-05-06", "10-Q", "0001193125-19-138383", "d730624d10q.htm"),
    ("2019-06-30", "2019-08-05", "10-Q", "0001193125-19-212977", "d703824d10q.htm"),
    ("2019-09-30", "2019-11-04", "10-Q", "0001193125-19-283352", "d813311d10q.htm"),
    ("2019-12-31", "2020-02-20", "10-K", "0001193125-20-043666", "d853127d10k.htm"),
    ("2020-03-31", "2020-05-07", "10-Q", "0001193125-20-136333", "d927981d10q.htm"),
    ("2020-06-30", "2020-08-04", "10-Q", "0001193125-20-209363", "d59162d10q.htm"),
    ("2020-09-30", "2020-11-05", "10-Q", "0001193125-20-286862", "d887217d10q.htm"),
    ("2020-12-31", "2021-02-24", "10-K", "0001193125-21-054667", "d118840d10k.htm"),
    ("2021-03-31", "2021-05-05", "10-Q", "0001193125-21-151723", "d26269d10q.htm"),
    ("2021-06-30", "2021-08-03", "10-Q", "0001193125-21-235044", "d186851d10q.htm"),
    ("2021-09-30", "2021-11-03", "10-Q", "0001193125-21-318452", "d242122d10q.htm"),
    ("2021-12-31", "2022-03-01", "10-K", "0001193125-22-061323", "d290947d10k.htm"),
)


def validate_filing_specs() -> None:
    frame = pd.DataFrame(
        FILING_SPECS,
        columns=["fiscal_end", "filed", "form", "accession", "document"],
    )
    frame["fiscal_end"] = pd.to_datetime(frame["fiscal_end"])
    frame["filed"] = pd.to_datetime(frame["filed"])
    if len(frame) != 20 or frame["fiscal_end"].duplicated().any():
        raise ValueError("SLRC filing chain must contain 20 unique quarters")
    if _longest_chain(frame["fiscal_end"].tolist()) != 20:
        raise ValueError("SLRC filing specifications are not quarterly-continuous")
    if not frame.loc[frame["fiscal_end"].dt.month.eq(12), "form"].eq("10-K").all():
        raise ValueError("SLRC year-end periods must use 10-K filings")
    if not frame.loc[~frame["fiscal_end"].dt.month.eq(12), "form"].eq("10-Q").all():
        raise ValueError("SLRC interim periods must use 10-Q filings")
    lag = (frame["filed"] - frame["fiscal_end"]).dt.days
    if not lag.between(0, 150).all():
        raise ValueError("SLRC filing specifications contain an untimely filing")


def parse_slrc_statement(
    path: Path,
    *,
    period_end: pd.Timestamp,
    annual: bool,
    issuer_fragments: tuple[str, ...] = (
        "solar capital ltd.",
        "slr investment corp.",
    ),
) -> dict[str, float]:
    document = " ".join(
        BeautifulSoup(path.read_bytes(), "html.parser")
        .get_text(" ", strip=True)
        .split()
    ).casefold()
    if not any(fragment.casefold() in document for fragment in issuer_fragments):
        raise ValueError(f"BDC filing issuer identity is not proven: {path}")
    if "in thousands" not in document:
        raise ValueError(f"SLRC filing does not prove thousands units: {path}")
    candidates = []
    for table in pd.read_html(path):
        first = table.iloc[:, 0].fillna("").map(_normal)
        if not all(
            first.isin({_normal(label) for label in labels}).any()
            for labels in METRIC_LABELS.values()
        ):
            continue
        try:
            columns = (
                _period_columns(
                    table, year=period_end.year, period_phrase="Year ended"
                )
                if annual
                else _direct_columns(table, period_end)
            )
            values = {
                metric: _row_value(table, labels=labels, columns=columns)
                for metric, labels in METRIC_LABELS.items()
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
            f"expected one agreeing SLRC statement pair in {path}, "
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
            annual = parse_slrc_statement(path, period_end=end, annual=True)
            prior = [
                row for row in recovered
                if end - pd.DateOffset(months=9) <= pd.Timestamp(row["fiscal_end"]) < end
            ]
            if len(prior) != 3:
                raise RuntimeError(f"SLRC {fiscal_end} Q4 has {len(prior)} prior quarters")
            values = {
                metric: annual[metric] - sum(row[metric] for row in prior)
                for metric in METRIC_LABELS
            }
            prior_accessions = ";".join(row["accession"] for row in prior)
            annual_checks.append({
                "fiscal_end": fiscal_end, "annual": annual,
                "prior_three_quarter_sum": {
                    metric: sum(row[metric] for row in prior)
                    for metric in METRIC_LABELS
                },
                "derived_q4": values, "exact_arithmetic_identity": True,
            })
            derivation = "annual_minus_original_pit_q1_q2_q3"
        else:
            values = parse_slrc_statement(path, period_end=end, annual=False)
            derivation = "direct_original_sec_three_month_statement"
        if values["revenue"] <= 0:
            raise ValueError(f"SLRC {fiscal_end} revenue is not positive")
        lag_days = (pd.Timestamp(filed) - end).days
        if not 0 <= lag_days <= 150:
            raise ValueError(f"SLRC filing is not timely: {accession}")
        common = {
            "ticker": "SLRC", "fiscal_end": fiscal_end,
            "available_date": filed, "taxonomy": "us-gaap", "form": form,
            "accession": accession, "unit": "USD",
            "source": "sec_slrc_original_quarterly_statement",
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
            "ticker": "SLRC", "fiscal_end": fiscal_end,
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
        raise RuntimeError("SLRC output is not exactly 20 paired quarters")
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1, "research_only": True,
        "point_in_time_proven": True, "parameters_frozen": False,
        "promotion_eligible": False, "release_status": "BLOCKED",
        "ticker": "SLRC", "currency": "USD", "quarter_count": 20,
        "direct_quarter_count": 15, "derived_q4_count": 5,
        "longest_continuous_timely_paired_quarters": 20,
        "recovered_quarters": recovered,
        "annual_identity_checks": annual_checks,
        "filings": bindings,
        "outputs": {"quarters": {
            "path": str(facts_path), "sha256": _sha256(facts_path),
        }},
        "guardrail": (
            "Only original SLRC SEC-filed USD-thousands statements are used. "
            "Q1-Q3 are direct three-month values; each Q4 is the audited annual "
            "statement less the three earlier original PIT quarters. Later "
            "comparatives are not backdated. Formal fundamentals are untouched "
            "and trading remains blocked."
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
