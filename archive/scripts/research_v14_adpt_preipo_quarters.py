#!/usr/bin/env python3
"""Recover ADPT's 2018-2019 quarters without backdating later facts."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import shutil
from io import BytesIO
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd

from src.io.fundamentals_update import (
    OUTPUT_COLUMNS,
    merge_fundamentals,
    parse_companyfacts_annual,
    parse_companyfacts_quarterly,
)


REGISTRY = Path("stocks_list_dir/nasdaq/adpt_preipo_quarterly_reports.csv")
CACHE = Path("output/research_only/v14/companyfacts_cache/CIK0001478320.json.gz")
OUTPUT_DIR = Path("output/research_only/v14/adpt_preipo_quarters_2018_2019")
HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
EXPECTED_Q1 = {
    2018: {"revenue": 9_715_000.0, "net_income": -12_391_000.0},
    2019: {"revenue": 12_666_000.0, "net_income": -18_386_000.0},
}
EXPECTED_DIRECT = {
    "2018-06-30": {"revenue": 11_568_000.0, "net_income": -12_493_000.0},
    "2018-09-30": {"revenue": 17_188_000.0, "net_income": -8_292_000.0},
    "2019-06-30": {"revenue": 22_138_000.0, "net_income": -15_659_000.0},
    "2019-09-30": {"revenue": 26_058_000.0, "net_income": -13_950_000.0},
}
EXPECTED_ANNUAL = {
    2018: {"revenue": 55_663_000.0, "net_income": -46_447_000.0},
    2019: {"revenue": 85_071_000.0, "net_income": -68_606_000.0},
}
EXPECTED_Q4 = {
    2018: {"revenue": 17_192_000.0, "net_income": -13_271_000.0},
    2019: {"revenue": 24_209_000.0, "net_income": -20_611_000.0},
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normal(value: object) -> str:
    return " ".join(str(value).replace("\xa0", " ").split()).casefold()


def _accounting_value(value: object) -> float:
    text = str(value).replace(",", "").replace("$", "").strip()
    if text in {"—", "-", "–"}:
        return 0.0
    match = re.search(r"\d+(?:\.\d+)?", text)
    if match is None:
        raise ValueError(f"no accounting value in {value!r}")
    result = float(match.group())
    return -result if "(" in text else result


def extract_s1_q1(path: Path) -> dict[int, dict[str, float]]:
    """Extract only the two explicitly labelled S-1 single quarters."""
    candidates = []
    for table in pd.read_html(BytesIO(path.read_bytes())):
        if len(table) < 20 or len(table.columns) < 8:
            continue
        labels = table.iloc[:, 0].map(_normal)
        if labels.eq("total revenue").sum() != 1:
            continue
        if labels.eq("net loss").sum() != 1:
            continue
        headers = " ".join(
            _normal(value) for value in table.head(4).to_numpy().ravel()
        )
        if "three months ended march 31" not in headers:
            continue
        revenue = table.loc[labels.eq("total revenue")].iloc[0]
        income = table.loc[labels.eq("net loss")].iloc[0]
        period_headers = table.iloc[1].map(_normal)
        year_headers = table.iloc[2].map(_normal)
        recovered = {}
        for year in (2018, 2019):
            def one_value(row: pd.Series) -> float:
                values = []
                for column in table.columns:
                    if "three months ended march 31" not in period_headers[column]:
                        continue
                    if not year_headers[column].startswith(str(year)):
                        continue
                    try:
                        values.append(_accounting_value(row[column]))
                    except ValueError:
                        continue
                unique = sorted(set(values))
                if len(unique) != 1:
                    raise ValueError(
                        f"ADPT S-1 expected one Q1 {year} value, found {unique}"
                    )
                return unique[0] * 1_000.0

            recovered[year] = {
                "revenue": one_value(revenue),
                "net_income": one_value(income),
            }
        candidates.append(recovered)
    if not candidates or any(candidate != EXPECTED_Q1 for candidate in candidates):
        raise ValueError(
            f"ADPT S-1 Q1 values differ from strict expectation: {candidates}"
        )
    return EXPECTED_Q1


def _first_rows(frame: pd.DataFrame, fiscal_ends: list[str]) -> pd.DataFrame:
    selected = frame.loc[
        frame["fiscal_end"].isin(pd.to_datetime(fiscal_ends))
        & frame["metric"].isin({"revenue", "net_income"})
    ].sort_values("available_date").drop_duplicates(
        ["fiscal_end", "metric"], keep="first"
    )
    return selected.sort_values(["fiscal_end", "metric"])


def run(
    registry_path: Path = REGISTRY,
    cache_path: Path = CACHE,
    output_dir: Path = OUTPUT_DIR,
) -> dict:
    registry = pd.read_csv(registry_path, dtype={"cik": str, "accession": str})
    if (
        len(registry) != 1
        or registry.iloc[0]["ticker"] != "ADPT"
        or registry.iloc[0]["cik"] != "1478320"
    ):
        raise ValueError("ADPT registry must bind exactly CIK 1478320")
    row = registry.iloc[0]
    if (
        row["form"] != "S-1"
        or row["accession"].replace("-", "") not in row["source_url"]
    ):
        raise ValueError("ADPT registry must bind the original IPO S-1")
    source = Path(row["local_path"])
    source.parent.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        with urlopen(
            Request(row["source_url"], headers=HEADERS), timeout=120
        ) as response:
            source.write_bytes(response.read())
    q1 = extract_s1_q1(source)

    with gzip.open(cache_path, "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    if int(envelope["payload"]["cik"]) != 1478320:
        raise ValueError("ADPT cache has the wrong CIK")
    fetched_at = pd.Timestamp(envelope["fetched_at"]).tz_localize(None).normalize()
    quarterly = parse_companyfacts_quarterly(
        "ADPT", envelope["payload"], envelope["fetched_at"]
    )
    direct = _first_rows(quarterly, list(EXPECTED_DIRECT))
    if len(direct) != 8:
        raise RuntimeError("ADPT cache must contain four paired Q2-Q3 comparisons")
    actual_direct = {
        str(end.date()): group.set_index("metric")["value"].to_dict()
        for end, group in direct.groupby("fiscal_end")
    }
    if actual_direct != EXPECTED_DIRECT:
        raise RuntimeError(f"ADPT direct comparisons changed: {actual_direct}")
    expected_dates = {
        "2018-06-30": "2019-08-13", "2019-06-30": "2019-08-13",
        "2018-09-30": "2019-11-12", "2019-09-30": "2019-11-12",
    }
    actual_dates = {
        str(end.date()): str(group["available_date"].max().date())
        for end, group in direct.groupby("fiscal_end")
    }
    if actual_dates != expected_dates:
        raise RuntimeError(f"ADPT direct comparison dates changed: {actual_dates}")

    annual = _first_rows(
        parse_companyfacts_annual(
            "ADPT", envelope["payload"], envelope["fetched_at"]
        ),
        ["2018-12-31", "2019-12-31"],
    )
    if len(annual) != 4 or not annual["available_date"].eq(
        pd.Timestamp("2020-02-26")
    ).all():
        raise RuntimeError("ADPT first 2018-2019 annual facts must be 2020-02-26")
    actual_annual = {
        end.year: group.set_index("metric")["value"].to_dict()
        for end, group in annual.groupby("fiscal_end")
    }
    if actual_annual != EXPECTED_ANNUAL:
        raise RuntimeError(f"ADPT annual facts changed: {actual_annual}")

    q1_rows = []
    for year, values in q1.items():
        for metric, value in values.items():
            q1_rows.append({
                "ticker": "ADPT", "fiscal_end": f"{year}-03-31",
                "available_date": row["available_date"], "metric": metric,
                "value": value, "taxonomy": "ADPT_US_GAAP_S1",
                "concept": f"sec_s1_three_months_ended_q1_{metric}",
                "form": "S-1", "accession": row["accession"],
                "fetched_at": fetched_at,
            })
    known = pd.concat([
        pd.DataFrame(q1_rows, columns=OUTPUT_COLUMNS),
        direct[OUTPUT_COLUMNS],
    ], ignore_index=True)
    known["fiscal_end"] = pd.to_datetime(known["fiscal_end"])
    known["available_date"] = pd.to_datetime(known["available_date"])
    q4_rows = []
    for year, annual_values in EXPECTED_ANNUAL.items():
        year_known = known.loc[known["fiscal_end"].dt.year.eq(year)]
        sums = year_known.groupby("metric")["value"].sum().to_dict()
        for metric, annual_value in annual_values.items():
            annual_row = annual.loc[
                annual["fiscal_end"].dt.year.eq(year)
                & annual["metric"].eq(metric)
            ].iloc[0]
            q4_rows.append({
                "ticker": "ADPT", "fiscal_end": f"{year}-12-31",
                "available_date": annual_row["available_date"], "metric": metric,
                "value": annual_value - sums[metric],
                "taxonomy": "ADPT_US_GAAP_S1_COMPANYFACTS",
                "concept": f"derived_fy_minus_disclosed_q1_q2_q3:{metric}",
                "form": "10-K_RESIDUAL", "accession": annual_row["accession"],
                "fetched_at": fetched_at,
            })
    actual_q4 = {
        pd.Timestamp(row_["fiscal_end"]).year: {
            item["metric"]: item["value"]
            for item in q4_rows
            if pd.Timestamp(item["fiscal_end"]).year
            == pd.Timestamp(row_["fiscal_end"]).year
        }
        for row_ in q4_rows
    }
    if actual_q4 != EXPECTED_Q4:
        raise RuntimeError(f"ADPT Q4 residual changed: {actual_q4}")
    recovered = pd.concat([
        known, pd.DataFrame(q4_rows, columns=OUTPUT_COLUMNS)
    ], ignore_index=True).sort_values(["fiscal_end", "metric", "available_date"])
    recovered["fiscal_end"] = pd.to_datetime(recovered["fiscal_end"])
    recovered["available_date"] = pd.to_datetime(recovered["available_date"])
    if (
        len(recovered) != 16
        or recovered[["ticker", "fiscal_end", "metric"]].duplicated().any()
    ):
        raise RuntimeError("ADPT recovery must contain exactly eight paired quarters")
    checks = {
        year: {
            metric: float(value)
            for metric, value in recovered.loc[
                recovered["fiscal_end"].dt.year.eq(year)
            ].groupby("metric")["value"].sum().items()
        }
        for year in (2018, 2019)
    }
    if checks != EXPECTED_ANNUAL:
        raise RuntimeError(f"ADPT recovered quarters do not close: {checks}")

    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    recovered.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1, "research_only": True, "ticker": "ADPT",
        "point_in_time_proven": True, "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED", "promotion_eligible": False,
        "accepted_quarter_count": 8, "fact_count": 16,
        "annual_identity_checks": checks, "q4_residual": EXPECTED_Q4,
        "sources": [
            {**row.to_dict(), "sha256": _sha256(source)},
            {"path": str(cache_path), "sha256": _sha256(cache_path)},
        ],
        "outputs": {"strict_quarterly_facts": {
            "path": str(facts_path), "sha256": _sha256(facts_path)
        }},
        "guardrail": (
            "Q1 2018 and Q1 2019 are explicit single quarters in the original "
            "2019-05-30 S-1. Q2-Q3 retain their first 2019 10-Q filing dates. "
            "Q4 is FY minus the three already disclosed quarters and becomes "
            "available only with the 2020-02-26 10-K. Both years close exactly."
        ),
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    report["manifest"] = str(manifest_path)
    return report


def integrate_candidate(
    *,
    base_dir: Path,
    supplement_dir: Path = OUTPUT_DIR,
    output_dir: Path,
) -> dict:
    """Overlay strict quarterly facts without modifying either source dataset."""
    base_dir = Path(base_dir)
    supplement_dir = Path(supplement_dir)
    output_dir = Path(output_dir)
    base_annual = base_dir / "annual.csv"
    base_quarterly = base_dir / "quarterly.csv"
    base_manifest = base_dir / "manifest.json"
    supplement = supplement_dir / "strict_quarterly_facts.csv"
    supplement_manifest = supplement_dir / "manifest.json"
    bound = {
        path: _sha256(path)
        for path in (
            base_annual, base_quarterly, base_manifest,
            supplement, supplement_manifest,
        )
    }
    base = pd.read_csv(base_quarterly)
    incoming = pd.read_csv(supplement)
    key_columns = [
        "ticker", "fiscal_end", "available_date", "metric", "accession"
    ]
    before_keys = set(map(tuple, base[key_columns].astype(str).to_numpy()))
    incoming_keys = set(map(tuple, incoming[key_columns].astype(str).to_numpy()))
    merged = merge_fundamentals(base, incoming)
    output_dir.mkdir(parents=True, exist_ok=True)
    annual_output = output_dir / "annual.csv"
    quarterly_output = output_dir / "quarterly.csv"
    shutil.copyfile(base_annual, annual_output)
    merged.to_csv(quarterly_output, index=False)
    after = {path: _sha256(path) for path in bound}
    if after != bound:
        raise RuntimeError("ADPT integration source changed while being read")
    report = {
        "schema_version": 1, "research_only": True,
        "formal_financials_modified": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED", "promotion_eligible": False,
        "base": {
            "path": str(base_dir),
            "manifest_sha256": bound[base_manifest],
            "annual_sha256": bound[base_annual],
            "quarterly_sha256": bound[base_quarterly],
        },
        "supplement": {
            "path": str(supplement),
            "sha256": bound[supplement],
            "manifest_sha256": bound[supplement_manifest],
        },
        "inserted_identity_rows": len(incoming_keys - before_keys),
        "outputs": {
            "annual": str(annual_output),
            "annual_sha256": _sha256(annual_output),
            "quarterly": str(quarterly_output),
            "quarterly_sha256": _sha256(quarterly_output),
            "quarterly_rows": len(merged),
        },
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    report["manifest"] = str(manifest_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry-path", type=Path, default=REGISTRY)
    parser.add_argument("--cache-path", type=Path, default=CACHE)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--base-dir", type=Path)
    parser.add_argument("--candidate-output-dir", type=Path)
    args = parser.parse_args()
    report = run(args.registry_path, args.cache_path, args.output_dir)
    if (args.base_dir is None) != (args.candidate_output_dir is None):
        parser.error("--base-dir and --candidate-output-dir must be used together")
    if args.base_dir is not None:
        report["candidate"] = integrate_candidate(
            base_dir=args.base_dir,
            supplement_dir=args.output_dir,
            output_dir=args.candidate_output_dir,
        )
    print(json.dumps(report, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
