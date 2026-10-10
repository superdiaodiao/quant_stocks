#!/usr/bin/env python3
"""Recover ALGM fiscal Q4 2020 and 2021 without backdating residuals."""

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


REGISTRY = Path("stocks_list_dir/nasdaq/algm_2020_ipo_s1.csv")
CACHE = Path("output/research_only/v14/companyfacts_cache/CIK0000866291.json.gz")
OUTPUT_DIR = Path("output/research_only/v14/algm_preipo_fiscal_q4")
HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
S1_EXPECTED = {
    "fy2020": {"revenue": 650_089_000.0, "net_income": 36_971_000.0},
    "q1_2019": {"revenue": 152_443_000.0, "net_income": 3_184_000.0},
    "q1_2020": {"revenue": 115_001_000.0, "net_income": 4_820_000.0},
}
EXPECTED_DIRECT = {
    "2019-09-27": {"revenue": 163_240_000.0, "net_income": 11_565_000.0},
    "2019-12-27": {"revenue": 159_802_000.0, "net_income": 8_926_000.0},
    "2020-09-25": {"revenue": 136_649_000.0, "net_income": 9_584_000.0},
    "2020-12-25": {"revenue": 164_449_000.0, "net_income": -5_095_000.0},
}
EXPECTED_DIRECT_DATES = {
    "2019-09-27": "2020-11-20",
    "2019-12-27": "2021-02-02",
    "2020-09-25": "2020-11-20",
    "2020-12-25": "2021-02-02",
}
EXPECTED_FY2021 = {"revenue": 591_207_000.0, "net_income": 17_953_000.0}
EXPECTED_Q4 = {
    "2020-03-27": {"revenue": 174_604_000.0, "net_income": 13_296_000.0},
    "2021-03-26": {"revenue": 175_108_000.0, "net_income": 8_644_000.0},
}
EXPECTED_Q4_DATES = {
    "2020-03-27": "2021-02-02",
    "2021-03-26": "2021-05-19",
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
    """Extract the S-1's explicit FY and single-quarter GAAP comparisons."""
    candidates = []
    for table in pd.read_html(BytesIO(path.read_bytes())):
        if len(table) < 12 or len(table.columns) < 14:
            continue
        labels = table.iloc[:, 0].map(_normal)
        if labels.eq("total net sales(3)").sum() != 1:
            continue
        if labels.eq("net income attributable to allegro microsystems, inc.").sum() != 1:
            continue
        headers = " ".join(
            _normal(value) for value in table.head(4).to_numpy().ravel()
        )
        required = (
            "fiscal year ended(1)", "march 27, 2020",
            "three-month period ended(2)", "june 28, 2019", "june 26, 2020",
        )
        if not all(phrase in headers for phrase in required):
            continue
        revenue = table.loc[labels.eq("total net sales(3)")].iloc[0]
        income = table.loc[
            labels.eq("net income attributable to allegro microsystems, inc.")
        ].iloc[0]

        def value_for(period: str, row: pd.Series) -> float:
            matches = []
            for column in table.columns:
                column_headers = {
                    _normal(table.iloc[index][column])
                    for index in range(min(4, len(table)))
                }
                if period not in column_headers:
                    continue
                try:
                    matches.append(_accounting_value(row[column]) * 1_000.0)
                except ValueError:
                    continue
            unique = sorted(set(matches))
            if len(unique) != 1:
                raise ValueError(f"ALGM S-1 {period} values are ambiguous: {unique}")
            return unique[0]

        candidates.append({
            "fy2020": {
                "revenue": value_for("march 27, 2020", revenue),
                "net_income": value_for("march 27, 2020", income),
            },
            "q1_2019": {
                "revenue": value_for("june 28, 2019", revenue),
                "net_income": value_for("june 28, 2019", income),
            },
            "q1_2020": {
                "revenue": value_for("june 26, 2020", revenue),
                "net_income": value_for("june 26, 2020", income),
            },
        })
    if not candidates or any(candidate != S1_EXPECTED for candidate in candidates):
        raise ValueError(f"ALGM S-1 values changed: {candidates}")
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
        or registry.iloc[0]["ticker"] != "ALGM"
        or registry.iloc[0]["cik"] != "866291"
    ):
        raise ValueError("ALGM registry must bind exactly CIK 866291")
    row = registry.iloc[0]
    if (
        row["form"] != "S-1"
        or row["accession"].replace("-", "") not in row["source_url"]
    ):
        raise ValueError("ALGM registry must bind the original 2020 IPO S-1")
    source = Path(row["local_path"])
    source.parent.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        with urlopen(Request(row["source_url"], headers=HEADERS), timeout=120) as response:
            source.write_bytes(response.read())
    s1 = extract_s1_values(source)

    with gzip.open(cache_path, "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    if int(envelope["payload"]["cik"]) != 866291:
        raise ValueError("ALGM cache has the wrong CIK")
    quarterly = parse_companyfacts_quarterly(
        "ALGM", envelope["payload"], envelope["fetched_at"]
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
            f"ALGM direct inputs changed: values={actual_direct}, dates={actual_dates}"
        )
    annual = _first_rows(
        parse_companyfacts_annual(
            "ALGM", envelope["payload"], envelope["fetched_at"]
        ),
        ["2021-03-26"],
    )
    if len(annual) != 2 or not annual["available_date"].eq(
        pd.Timestamp("2021-05-19")
    ).all():
        raise RuntimeError("ALGM FY2021 must first be available on 2021-05-19")
    actual_fy2021 = annual.set_index("metric")["value"].to_dict()
    if actual_fy2021 != EXPECTED_FY2021:
        raise RuntimeError(f"ALGM FY2021 values changed: {actual_fy2021}")

    q1_rows = []
    for fiscal_end, values in (
        ("2019-06-28", s1["q1_2019"]),
        ("2020-06-26", s1["q1_2020"]),
    ):
        for metric, value in values.items():
            q1_rows.append({
                "ticker": "ALGM", "fiscal_end": fiscal_end,
                "available_date": row["available_date"], "metric": metric,
                "value": value, "taxonomy": "ALGM_GAAP_S1",
                "concept": f"s1_direct_single_quarter:{metric}", "form": "S-1",
                "accession": row["accession"],
                "fetched_at": pd.Timestamp(envelope["fetched_at"]).tz_localize(None).normalize(),
            })
    q1_frame = pd.DataFrame(q1_rows, columns=OUTPUT_COLUMNS)
    q1_frame["fiscal_end"] = pd.to_datetime(q1_frame["fiscal_end"])
    q1_frame["available_date"] = pd.to_datetime(q1_frame["available_date"])

    q4_rows = []
    fiscal_specs = (
        (
            "2020-03-27", s1["fy2020"],
            ["2019-06-28", "2019-09-27", "2019-12-27"],
            pd.Timestamp(EXPECTED_Q4_DATES["2020-03-27"]),
            row["accession"] + "+0000866291-21-000007",
        ),
        (
            "2021-03-26", EXPECTED_FY2021,
            ["2020-06-26", "2020-09-25", "2020-12-25"],
            pd.Timestamp(EXPECTED_Q4_DATES["2021-03-26"]),
            "0000866291-21-000020",
        ),
    )
    known = pd.concat([q1_frame, direct[OUTPUT_COLUMNS]], ignore_index=True)
    known["fiscal_end"] = pd.to_datetime(known["fiscal_end"])
    known["available_date"] = pd.to_datetime(known["available_date"])
    for fiscal_end, annual_values, quarter_ends, available_date, accession in fiscal_specs:
        inputs = known.loc[known["fiscal_end"].isin(pd.to_datetime(quarter_ends))]
        if inputs["fiscal_end"].nunique() != 3 or inputs["metric"].nunique() != 2:
            raise RuntimeError(f"ALGM {fiscal_end} does not have three paired inputs")
        if inputs["available_date"].max() > available_date:
            raise RuntimeError(f"ALGM {fiscal_end} residual is backdated")
        sums = inputs.groupby("metric")["value"].sum().to_dict()
        for metric, annual_value in annual_values.items():
            q4_rows.append({
                "ticker": "ALGM", "fiscal_end": fiscal_end,
                "available_date": available_date, "metric": metric,
                "value": annual_value - sums[metric],
                "taxonomy": "ALGM_GAAP_S1_COMPANYFACTS",
                "concept": f"derived_fy_minus_known_q1_q2_q3:{metric}",
                "form": "S-1+10-Q_RESIDUAL" if fiscal_end == "2020-03-27" else "10-K_RESIDUAL",
                "accession": accession,
                "fetched_at": pd.Timestamp(envelope["fetched_at"]).tz_localize(None).normalize(),
            })
    q4_frame = pd.DataFrame(q4_rows, columns=OUTPUT_COLUMNS)
    q4_frame["fiscal_end"] = pd.to_datetime(q4_frame["fiscal_end"])
    q4_frame["available_date"] = pd.to_datetime(q4_frame["available_date"])
    actual_q4 = {
        str(end.date()): group.set_index("metric")["value"].to_dict()
        for end, group in q4_frame.groupby("fiscal_end")
    }
    if actual_q4 != EXPECTED_Q4:
        raise RuntimeError(f"ALGM Q4 residuals changed: {actual_q4}")
    recovered = pd.concat([q1_frame, q4_frame], ignore_index=True).sort_values(
        ["fiscal_end", "metric", "available_date"]
    )
    if len(recovered) != 8:
        raise RuntimeError("ALGM recovery must contain four paired quarters")

    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    recovered.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1, "research_only": True, "ticker": "ALGM",
        "point_in_time_proven": True, "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED", "promotion_eligible": False,
        "accepted_quarter_count": 4, "fact_count": 8,
        "s1_values": s1, "direct_inputs": actual_direct,
        "direct_input_available_dates": actual_dates,
        "fy2021": actual_fy2021, "q4_residuals": actual_q4,
        "q4_available_dates": EXPECTED_Q4_DATES,
        "sources": [
            {**row.to_dict(), "sha256": _sha256(source)},
            {"path": str(cache_path), "sha256": _sha256(cache_path)},
        ],
        "outputs": {"strict_quarterly_facts": {
            "path": str(facts_path), "sha256": _sha256(facts_path)
        }},
        "guardrail": (
            "The 2020 S-1 directly supplies fiscal June 2019/2020 quarters and "
            "FY2020. Fiscal Q4 2020 becomes available only after the last required "
            "comparison on 2021-02-02. Fiscal Q4 2021 becomes available with the "
            "first 2021 10-K on 2021-05-19. No residual is backdated."
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
