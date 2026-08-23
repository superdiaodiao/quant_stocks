#!/usr/bin/env python3
"""Recover DDOG 2018Q4 and 2019Q4 with strict PIT residual dates."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
from io import BytesIO
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd

from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.io.fundamentals_update import (
    OUTPUT_COLUMNS,
    parse_companyfacts_annual,
    parse_companyfacts_quarterly,
)


REGISTRY = Path("stocks_list_dir/nasdaq/ddog_2019_ipo_s1.csv")
CACHE = Path("output/research_only/v14/companyfacts_cache/CIK0001561550.json.gz")
OUTPUT_DIR = Path("output/research_only/v14/ddog_preipo_q4")
HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
S1_EXPECTED = {
    "fy2018": {"revenue": 198_077_000.0, "net_income": -10_762_000.0},
    "h1_2018": {"revenue": 85_393_000.0, "net_income": 498_000.0},
    "h1_2019": {"revenue": 153_272_000.0, "net_income": -13_440_000.0},
}
EXPECTED_DIRECT = {
    "2018-09-30": {"revenue": 51_074_000.0, "net_income": -4_673_000.0},
    "2019-03-31": {"revenue": 70_050_000.0, "net_income": -9_491_000.0},
    "2019-06-30": {"revenue": 83_222_000.0, "net_income": -3_949_000.0},
    "2019-09-30": {"revenue": 95_864_000.0, "net_income": -4_161_000.0},
}
EXPECTED_DIRECT_DATES = {
    "2018-09-30": "2019-11-13",
    "2019-03-31": "2020-05-12",
    "2019-06-30": "2020-08-07",
    "2019-09-30": "2019-11-13",
}
EXPECTED_FY2019 = {"revenue": 362_780_000.0, "net_income": -16_710_000.0}
EXPECTED_Q4 = {
    "2018-12-31": {"revenue": 61_610_000.0, "net_income": -6_587_000.0},
    "2019-12-31": {"revenue": 113_644_000.0, "net_income": 891_000.0},
}
EXPECTED_Q4_DATES = {
    "2018-12-31": "2019-11-13",
    "2019-12-31": "2020-08-07",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normal(value: object) -> str:
    return " ".join(str(value).replace("\xa0", " ").split()).casefold()


def _accounting_value(value: object) -> float:
    text = str(value).replace(",", "").replace("$", "").strip()
    match = re.search(r"\d+(?:\.\d+)?", text)
    if match is None:
        raise ValueError(f"no accounting value in {value!r}")
    number = float(match.group())
    return -number if "(" in text else number


def extract_s1_values(path: Path) -> dict[str, dict[str, float]]:
    candidates = []
    for table in pd.read_html(BytesIO(path.read_bytes())):
        if len(table) < 20 or len(table.columns) < 14:
            continue
        labels = table.iloc[:, 0].map(_normal)
        if labels.eq("revenue").sum() != 1 or labels.eq("net (loss) income").sum() != 1:
            continue
        headers = " ".join(
            _normal(value) for value in table.head(4).to_numpy().ravel()
        )
        required = (
            "year ended december 31,", "six months ended june 30,",
            "2018", "2019",
        )
        if not all(phrase in headers for phrase in required):
            continue
        revenue = table.loc[labels.eq("revenue")].iloc[0]
        income = table.loc[labels.eq("net (loss) income")].iloc[0]

        def value_for(section: str, year: str, row: pd.Series) -> float:
            matches = []
            for column in table.columns:
                column_headers = [
                    _normal(table.iloc[index][column])
                    for index in range(min(4, len(table)))
                ]
                if section not in column_headers or year not in column_headers:
                    continue
                try:
                    matches.append(_accounting_value(row[column]) * 1_000.0)
                except ValueError:
                    continue
            unique = sorted(set(matches))
            if len(unique) != 1:
                raise ValueError(
                    f"DDOG S-1 {section} {year} values are ambiguous: {unique}"
                )
            return unique[0]

        candidate = {
            "fy2018": {
                "revenue": value_for("year ended december 31,", "2018", revenue),
                "net_income": value_for("year ended december 31,", "2018", income),
            },
            "h1_2018": {
                "revenue": value_for("six months ended june 30,", "2018", revenue),
                "net_income": value_for("six months ended june 30,", "2018", income),
            },
            "h1_2019": {
                "revenue": value_for("six months ended june 30,", "2019", revenue),
                "net_income": value_for("six months ended june 30,", "2019", income),
            },
        }
        # Exclude percentage/illustrative tables that repeat the same labels
        # but are not the GAAP statement amounts in thousands.
        if candidate["fy2018"]["revenue"] > 100_000_000:
            candidates.append(candidate)
    if not candidates or any(candidate != S1_EXPECTED for candidate in candidates):
        raise ValueError(f"DDOG S-1 values changed: {candidates}")
    return S1_EXPECTED


def _first_rows(frame: pd.DataFrame, fiscal_ends: list[str]) -> pd.DataFrame:
    return frame.loc[
        frame["fiscal_end"].isin(pd.to_datetime(fiscal_ends))
        & frame["metric"].isin({"revenue", "net_income"})
    ].sort_values("available_date").drop_duplicates(
        ["fiscal_end", "metric"], keep="first"
    ).sort_values(["fiscal_end", "metric"])


def run(
    registry_path: Path = REGISTRY,
    cache_path: Path = CACHE,
    output_dir: Path = OUTPUT_DIR,
) -> dict:
    registry = pd.read_csv(registry_path, dtype={"cik": str, "accession": str})
    if (
        len(registry) != 1
        or registry.iloc[0]["ticker"] != "DDOG"
        or registry.iloc[0]["cik"] != "1561550"
    ):
        raise ValueError("DDOG registry must bind exactly CIK 1561550")
    row = registry.iloc[0]
    if row["form"] != "S-1" or row["accession"].replace("-", "") not in row["source_url"]:
        raise ValueError("DDOG registry must bind the original IPO S-1")
    source = Path(row["local_path"])
    source.parent.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        with urlopen(Request(row["source_url"], headers=HEADERS), timeout=120) as response:
            source.write_bytes(response.read())
    s1 = extract_s1_values(source)

    with gzip.open(cache_path, "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    if int(envelope["payload"]["cik"]) != 1561550:
        raise ValueError("DDOG cache has the wrong CIK")
    quarterly = parse_companyfacts_quarterly(
        "DDOG", envelope["payload"], envelope["fetched_at"]
    )
    direct = _first_rows(quarterly, list(EXPECTED_DIRECT))
    actual_direct = {
        str(end.date()): group.set_index("metric")["value"].to_dict()
        for end, group in direct.groupby("fiscal_end")
    }
    actual_dates = {
        str(end.date()): str(group["available_date"].max().date())
        for end, group in direct.groupby("fiscal_end")
    }
    if actual_direct != EXPECTED_DIRECT or actual_dates != EXPECTED_DIRECT_DATES:
        raise RuntimeError(
            f"DDOG direct inputs changed: values={actual_direct}, dates={actual_dates}"
        )
    annual = _first_rows(
        parse_companyfacts_annual(
            "DDOG", envelope["payload"], envelope["fetched_at"]
        ),
        ["2019-12-31"],
    )
    if len(annual) != 2 or not annual["available_date"].eq(
        pd.Timestamp("2020-02-25")
    ).all():
        raise RuntimeError("DDOG FY2019 must first be available on 2020-02-25")
    actual_fy2019 = annual.set_index("metric")["value"].to_dict()
    if actual_fy2019 != EXPECTED_FY2019:
        raise RuntimeError(f"DDOG FY2019 values changed: {actual_fy2019}")

    q4_rows = []
    specs = (
        (
            "2018-12-31", s1["fy2018"], s1["h1_2018"],
            direct.loc[direct["fiscal_end"].eq(pd.Timestamp("2018-09-30"))],
            pd.Timestamp(EXPECTED_Q4_DATES["2018-12-31"]),
            row["accession"] + "+0001564590-19-043256",
        ),
        (
            "2019-12-31", EXPECTED_FY2019, None,
            direct.loc[direct["fiscal_end"].isin(pd.to_datetime([
                "2019-03-31", "2019-06-30", "2019-09-30"
            ]))],
            pd.Timestamp(EXPECTED_Q4_DATES["2019-12-31"]),
            "0001564590-20-006422+0001564590-20-038405",
        ),
    )
    fetched_at = pd.Timestamp(envelope["fetched_at"]).tz_localize(None).normalize()
    for fiscal_end, annual_values, h1_values, inputs, available_date, accession in specs:
        input_sums = inputs.groupby("metric")["value"].sum().to_dict()
        if h1_values is not None:
            input_sums = {
                metric: h1_values[metric] + input_sums[metric]
                for metric in annual_values
            }
        if inputs["available_date"].max() > available_date:
            raise RuntimeError(f"DDOG {fiscal_end} residual is backdated")
        for metric, annual_value in annual_values.items():
            q4_rows.append({
                "ticker": "DDOG", "fiscal_end": fiscal_end,
                "available_date": available_date, "metric": metric,
                "value": annual_value - input_sums[metric],
                "taxonomy": "DDOG_GAAP_S1_COMPANYFACTS",
                "concept": f"derived_fy_minus_known_periods:{metric}",
                "form": "S-1+10-Q_RESIDUAL" if fiscal_end == "2018-12-31" else "10-K+10-Q_RESIDUAL",
                "accession": accession, "fetched_at": fetched_at,
            })
    recovered = pd.DataFrame(q4_rows, columns=OUTPUT_COLUMNS)
    recovered["fiscal_end"] = pd.to_datetime(recovered["fiscal_end"])
    recovered["available_date"] = pd.to_datetime(recovered["available_date"])
    actual_q4 = {
        str(end.date()): group.set_index("metric")["value"].to_dict()
        for end, group in recovered.groupby("fiscal_end")
    }
    if actual_q4 != EXPECTED_Q4:
        raise RuntimeError(f"DDOG Q4 residuals changed: {actual_q4}")
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    recovered.sort_values(["fiscal_end", "metric"]).to_csv(facts_path, index=False)
    report = {
        "schema_version": 1, "research_only": True, "ticker": "DDOG",
        "point_in_time_proven": True, "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED", "promotion_eligible": False,
        "accepted_quarter_count": 2, "fact_count": 4,
        "s1_values": s1, "direct_inputs": actual_direct,
        "direct_input_available_dates": actual_dates,
        "fy2019": actual_fy2019, "q4_residuals": actual_q4,
        "q4_available_dates": EXPECTED_Q4_DATES,
        "sources": [
            {**row.to_dict(), "sha256": _sha256(source)},
            {"path": str(cache_path), "sha256": _sha256(cache_path)},
        ],
        "outputs": {"strict_quarterly_facts": {
            "path": str(facts_path), "sha256": _sha256(facts_path)
        }},
        "guardrail": (
            "2018Q4 is FY2018 minus S-1 H1 and the first-filed Q3 comparison; "
            "2019Q4 is FY2019 minus the first available Q1-Q3 comparisons. "
            "Each residual is available only on its last required input date."
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
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
