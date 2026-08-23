#!/usr/bin/env python3
"""Recover PFLT 2018Q4-2021Q3 from contemporaneous SEC 8-K exhibits."""

from __future__ import annotations

import argparse
from io import BytesIO
import hashlib
import json
from pathlib import Path
import re
from urllib.request import Request, urlopen

import pandas as pd

from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.io.fundamentals_update import OUTPUT_COLUMNS


CIK = 1_504_619
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
OUTPUT_DIR = Path("output/research_only/v14/pflt_sec_exhibits_2018q4_2021q3")

REVENUE_LABEL = "Total investment income"
PROFIT_LABEL_FRAGMENT = "net assets resulting from operations"

# Fiscal labels follow PFLT's September year-end. The fiscal_end values below
# remain calendar dates, as required by the candidate fundamentals schema.
FILINGS = {
    "2019Q1": {
        "accession": "0001171843-19-000762",
        "filed": "2019-02-06",
        "document": "exh_991.htm",
        "fiscal_end": "2018-12-31",
        "heading": "Three Months Ended December 31",
        "current_year": 2018,
        "prior_year": 2017,
        "current": (23_184_232.0, 4_995_195.0),
        "prior": (14_836_360.0, 1_918_782.0),
    },
    "2019Q2": {
        "accession": "0001171843-19-003117",
        "filed": "2019-05-08",
        "document": "exh_991.htm",
        "fiscal_end": "2019-03-31",
        "heading": "Three Months Ended March 31",
        "current_year": 2019,
        "prior_year": 2018,
        "current": (23_005_339.0, -5_471_725.0),
        "prior": (16_500_820.0, 15_589_760.0),
    },
    "2019Q3": {
        "accession": "0001171843-19-005256",
        "filed": "2019-08-07",
        "document": "exh_991.htm",
        "fiscal_end": "2019-06-30",
        "heading": "Three Months Ended June 30",
        "current_year": 2019,
        "prior_year": 2018,
        "current": (22_876_008.0, 4_518_816.0),
        "prior": (19_529_069.0, 4_968_387.0),
        "ytd_heading": "Nine Months Ended June 30",
        "current_ytd": (69_065_579.0, 4_042_286.0),
        "prior_ytd": (50_866_249.0, 22_476_929.0),
    },
    "2019FY": {
        "accession": "0001171843-19-007689",
        "filed": "2019-11-20",
        "document": "exh_991.htm",
        "fiscal_end": "2019-09-30",
        "heading": "Years Ended September 30",
        "current_year": 2019,
        "prior_year": 2018,
        "current": (92_947_182.0, 11_416_106.0),
        "prior": (72_204_579.0, 33_490_222.0),
    },
    "2020Q1": {
        "accession": "0001171843-20-000723",
        "filed": "2020-02-05",
        "document": "exh_991.htm",
        "fiscal_end": "2019-12-31",
        "heading": "Three Months Ended December 31",
        "current_year": 2019,
        "prior_year": 2018,
        "current": (24_638_674.0, 9_972_894.0),
        "prior": (23_184_232.0, 4_995_195.0),
    },
    "2020Q2": {
        "accession": "0001171843-20-003590",
        "filed": "2020-05-11",
        "document": "exh_991.htm",
        "fiscal_end": "2020-03-31",
        "heading": "Three Months Ended March 31",
        "current_year": 2020,
        "prior_year": 2019,
        "current": (26_326_437.0, -21_101_049.0),
        "prior": (23_005_339.0, -5_471_725.0),
    },
    "2020Q3": {
        "accession": "0001171843-20-005551",
        "filed": "2020-08-05",
        "document": "exh_991.htm",
        "fiscal_end": "2020-06-30",
        "heading": "Three Months Ended June 30",
        "current_year": 2020,
        "prior_year": 2019,
        "current": (22_765_518.0, 12_555_145.0),
        "prior": (22_876_008.0, 4_518_816.0),
        "ytd_heading": "Nine Months Ended June 30",
        "current_ytd": (73_730_632.0, 1_426_990.0),
        "prior_ytd": (69_065_579.0, 4_042_286.0),
    },
    "2020FY": {
        "accession": "0001171843-20-008089",
        "filed": "2020-11-18",
        "document": "exh_991.htm",
        "fiscal_end": "2020-09-30",
        "heading": "Years Ended September 30",
        "current_year": 2020,
        "prior_year": 2019,
        "current": (95_486_370.0, 18_413_044.0),
        "prior": (92_947_182.0, 11_416_106.0),
    },
    "2021Q1": {
        "accession": "0001171843-21-000872",
        "filed": "2021-02-09",
        "document": "exh_991.htm",
        "fiscal_end": "2020-12-31",
        "heading": "Three Months Ended December 31",
        "current_year": 2020,
        "prior_year": 2019,
        "current": (20_733_491.0, 26_130_588.0),
        "prior": (24_638_674.0, 9_972_894.0),
    },
    "2021Q2": {
        "accession": "0001171843-21-003164",
        "filed": "2021-05-05",
        "document": "exh_991.htm",
        "fiscal_end": "2021-03-31",
        "heading": "Three Months Ended March 31",
        "current_year": 2021,
        "prior_year": 2020,
        "current": (19_435_021.0, 11_673_345.0),
        "prior": (26_326_437.0, -21_101_049.0),
    },
    "2021Q3": {
        "accession": "0001171843-21-005493",
        "filed": "2021-08-04",
        "document": "exh_991.htm",
        "fiscal_end": "2021-06-30",
        "heading": "Three Months Ended June 30",
        "current_year": 2021,
        "prior_year": 2020,
        "current": (20_905_906.0, 14_707_479.0),
        "prior": (22_765_518.0, 12_555_145.0),
        "ytd_heading": "Nine Months Ended June 30",
        "current_ytd": (61_074_418.0, 52_511_411.0),
        "prior_ytd": (73_730_632.0, 1_426_990.0),
    },
    "2021FY": {
        "accession": "0001171843-21-008034",
        "filed": "2021-11-17",
        "document": "exh_991.htm",
        "fiscal_end": "2021-09-30",
        "heading": "Years Ended September 30",
        "current_year": 2021,
        "prior_year": 2020,
        "current": (82_693_512.0, 56_516_043.0),
        "prior": (95_486_370.0, 18_413_044.0),
    },
}

EXPECTED_Q4 = {
    2019: (23_881_603.0, 7_373_820.0),
    2020: (21_755_738.0, 16_986_054.0),
    2021: (21_619_094.0, 4_004_632.0),
}
EXPECTED_QUARTERLY_CLOSURE_DIFFERENCE = {
    2019: (0.0, 0.0),
    2020: (-3.0, 0.0),
    2021: (0.0, 1.0),
}


def _normalize(value) -> str:
    return " ".join(str(value).replace("\xa0", " ").split()).casefold()


def _accounting_number(value) -> float | None:
    text = str(value).strip()
    if not text or text.lower() == "nan" or text in {"$", "—", "-"}:
        return None
    negative = text.startswith("(") or text.endswith(")")
    cleaned = re.sub(r"[^0-9.]", "", text)
    if not cleaned:
        return None
    result = float(cleaned)
    return -result if negative else result


def _source_url(item: dict) -> str:
    accession = item["accession"].replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{CIK}/"
        f"{accession}/{item['document']}"
    )


def _fetch(item: dict) -> bytes:
    request = Request(_source_url(item), headers=SEC_HEADERS)
    with urlopen(request, timeout=120) as response:
        return response.read()


def _statement_table(raw: bytes) -> pd.DataFrame:
    matches = []
    for table in pd.read_html(BytesIO(raw)):
        text = table.astype(str)
        flattened = {_normalize(value) for value in text.to_numpy().ravel()}
        if _normalize(REVENUE_LABEL) not in flattened:
            continue
        if not any(
            value.startswith("net ")
            and PROFIT_LABEL_FRAGMENT in value
            and "per common share" not in value
            for value in flattened
        ):
            continue
        matches.append(table)
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one PFLT statement-of-operations table, found {len(matches)}"
        )
    return matches[0]


def _metric_row(table: pd.DataFrame, metric: str) -> int:
    first = table.iloc[:, 0].map(_normalize)
    if metric == "revenue":
        mask = first.eq(_normalize(REVENUE_LABEL))
    elif metric == "net_income":
        mask = (
            first.str.startswith("net ")
            & first.str.contains(PROFIT_LABEL_FRAGMENT, regex=False)
            & ~first.str.contains("per common share", regex=False)
        )
    else:
        raise ValueError(f"unsupported PFLT metric {metric!r}")
    rows = list(table.index[mask])
    if len(rows) != 1:
        raise RuntimeError(f"expected one PFLT {metric} row, found {rows}")
    return rows[0]


def _period_value(
    table: pd.DataFrame,
    metric: str,
    heading: str,
    year: int,
) -> float:
    row = _metric_row(table, metric)
    header_rows = table.iloc[:8]
    columns = []
    for column in table.columns:
        header = [_normalize(value) for value in header_rows[column]]
        if (
            any(_normalize(heading) in value for value in header)
            and _normalize(year) in header
        ):
            columns.append(column)
    values = {
        number
        for column in columns
        if (number := _accounting_number(table.loc[row, column])) is not None
    }
    if len(values) != 1:
        raise RuntimeError(
            f"expected one PFLT {metric} value for {heading} {year}, "
            f"found {sorted(values)} in columns {columns}"
        )
    return values.pop()


def _pair(table: pd.DataFrame, heading: str, year: int) -> tuple[float, float]:
    return (
        _period_value(table, "revenue", heading, year),
        _period_value(table, "net_income", heading, year),
    )


def _reconstruct_quarters(parsed: dict[str, dict]) -> tuple[dict, dict]:
    for current_key, comparison_key in (
        ("2019Q1", "2020Q1"),
        ("2019Q2", "2020Q2"),
        ("2019Q3", "2020Q3"),
        ("2019FY", "2020FY"),
        ("2020Q1", "2021Q1"),
        ("2020Q2", "2021Q2"),
        ("2020Q3", "2021Q3"),
        ("2020FY", "2021FY"),
    ):
        if parsed[current_key]["current"] != parsed[comparison_key]["prior"]:
            raise RuntimeError(
                f"PFLT comparison mismatch: {current_key} current does not "
                f"equal {comparison_key} prior"
            )

    quarters = {
        item["fiscal_end"]: parsed[key]["current"]
        for key, item in FILINGS.items()
        if key.endswith(("Q1", "Q2", "Q3"))
    }
    closure = {}
    for year in (2019, 2020, 2021):
        annual = parsed[f"{year}FY"]["current"]
        nine_month = parsed[f"{year}Q3"]["current_ytd"]
        q4 = tuple(annual[index] - nine_month[index] for index in range(2))
        if q4 != EXPECTED_Q4[year]:
            raise RuntimeError(f"PFLT {year}Q4 residual changed: {q4}")
        quarters[FILINGS[f"{year}FY"]["fiscal_end"]] = q4

        direct = [
            parsed[f"{year}Q{quarter}"]["current"]
            for quarter in (1, 2, 3)
        ]
        difference = tuple(
            sum(values[index] for values in direct) + q4[index] - annual[index]
            for index in range(2)
        )
        if difference != EXPECTED_QUARTERLY_CLOSURE_DIFFERENCE[year]:
            raise RuntimeError(
                f"PFLT {year} source closure difference changed: {difference}"
            )
        closure[year] = difference
    return quarters, closure


def recover(output_dir: Path = OUTPUT_DIR) -> dict:
    raw = {key: _fetch(item) for key, item in FILINGS.items()}
    parsed = {}
    sources = []
    for key, item in FILINGS.items():
        table = _statement_table(raw[key])
        current = _pair(table, item["heading"], item["current_year"])
        prior = _pair(table, item["heading"], item["prior_year"])
        if current != item["current"] or prior != item["prior"]:
            raise RuntimeError(
                f"PFLT {key} statement changed: current={current}, prior={prior}"
            )
        parsed[key] = {"current": current, "prior": prior}
        if "ytd_heading" in item:
            current_ytd = _pair(
                table, item["ytd_heading"], item["current_year"]
            )
            prior_ytd = _pair(table, item["ytd_heading"], item["prior_year"])
            if (
                current_ytd != item["current_ytd"]
                or prior_ytd != item["prior_ytd"]
            ):
                raise RuntimeError(
                    f"PFLT {key} YTD statement changed: "
                    f"current={current_ytd}, prior={prior_ytd}"
                )
            parsed[key].update(
                {"current_ytd": current_ytd, "prior_ytd": prior_ytd}
            )
        sources.append({
            "period": key,
            "accession": item["accession"],
            "filed": item["filed"],
            "document": item["document"],
            "url": _source_url(item),
            "sha256": hashlib.sha256(raw[key]).hexdigest(),
            "bytes": len(raw[key]),
        })

    quarters, closure = _reconstruct_quarters(parsed)
    fetched_at = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    rows = []
    for fiscal_end, values in sorted(quarters.items()):
        year = pd.Timestamp(fiscal_end).year
        quarter = pd.Timestamp(fiscal_end).quarter
        if quarter == 3:
            source_key = f"{year}FY"
            concept_prefix = "plain_html_derived_q4"
            form = "8-K:EX-99.1:FY_MINUS_9M"
        else:
            fiscal_year = year + 1 if quarter == 4 else year
            fiscal_quarter = 1 if quarter == 4 else quarter + 1
            source_key = f"{fiscal_year}Q{fiscal_quarter}"
            concept_prefix = "plain_html_statement"
            form = "8-K:EX-99.1"
        source = FILINGS[source_key]
        for metric, value, concept in (
            ("revenue", values[0], "TotalInvestmentIncome"),
            (
                "net_income",
                values[1],
                "NetAssetsFromOperationsIncreaseDecrease",
            ),
        ):
            rows.append({
                "ticker": "PFLT",
                "fiscal_end": fiscal_end,
                "available_date": source["filed"],
                "metric": metric,
                "value": value,
                "taxonomy": "sec-plain-html",
                "concept": f"{concept_prefix}:{concept}",
                "form": form,
                "accession": source["accession"],
                "fetched_at": fetched_at,
            })
    facts = pd.DataFrame(rows, columns=OUTPUT_COLUMNS).sort_values(
        ["fiscal_end", "metric"]
    )
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)

    recovered_quarters = []
    for fiscal_end, group in facts.groupby("fiscal_end", sort=True):
        values = group.set_index("metric")["value"].to_dict()
        recovered_quarters.append({
            "fiscal_end": str(fiscal_end),
            "available_date": str(group["available_date"].iloc[0]),
            "revenue": float(values["revenue"]),
            "net_income": float(values["net_income"]),
        })
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "ticker": "PFLT",
        "cik": CIK,
        "accepted_quarter_count": len(recovered_quarters),
        "accepted_fact_count": len(facts),
        "recovered_quarters": recovered_quarters,
        "sources": sources,
        "validation": {
            "current_to_later_comparative_matches": 8,
            "q4_derivation": "fiscal_year_minus_nine_months",
            "quarterly_closure_difference": {
                str(year): {
                    "revenue": closure[year][0],
                    "net_income": closure[year][1],
                }
                for year in closure
            },
            "maximum_absolute_source_discrepancy_usd": max(
                abs(value)
                for difference in closure.values()
                for value in difference
            ),
            "unit": "USD",
        },
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path),
                "sha256": hashlib.sha256(facts_path.read_bytes()).hexdigest(),
            }
        },
        "guardrail": (
            "Twelve contemporaneous SEC 8-K earnings exhibits bind every direct "
            "quarter and later comparative. Q4 uses FY minus nine months. The "
            "issuer-reported $3 and $1 source closure discrepancies are retained "
            "and audited, not silently adjusted. These facts remain research-only."
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
    report = recover(args.output_dir)
    if (args.base_dir is None) != (args.candidate_output_dir is None):
        parser.error("--base-dir and --candidate-output-dir must be used together")
    if args.base_dir is not None:
        report["candidate"] = integrate_candidate(
            base_dir=args.base_dir,
            supplement_dir=args.output_dir,
            output_dir=args.candidate_output_dir,
        )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
