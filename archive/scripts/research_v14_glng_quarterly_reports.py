#!/usr/bin/env python3
"""Recover GLNG's PIT quarterly revenue and parent-attributable net income."""

from __future__ import annotations

import argparse
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import time
from urllib.request import Request, urlopen

import pandas as pd

from src.io.fundamentals_update import OUTPUT_COLUMNS


CIK = 1_207_179
OUTPUT_DIR = Path("output/research_only/v14/glng_quarterly_reports")
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}

PRESS_SOURCES = {
    "2019-09-30": {
        "filed": "2019-11-26",
        "accession": "0001207179-19-000020",
        "document": "golarlngq32019pressrelease.htm",
        "period": "q3",
        "sha256": "5762211edcee63f05271b41655e3d32a0e7ec4c3b80582bc9aabd28a6feec44d",
    },
    "2019-12-31": {
        "filed": "2020-02-25",
        "accession": "0001207179-20-000002",
        "document": "golarlngq42019pressrel.htm",
        "period": "q4",
        "sha256": "3fd90812eec8bf22f58b76cbceed78bfa7f39e7090ddf0d45fa059d3322ad71b",
    },
    "2020-03-31": {
        "filed": "2020-05-28",
        "accession": "0001207179-20-000011",
        "document": "golarlngq12020pressrel.htm",
        "period": "q1",
        "sha256": "95184fb01f539c8d416da7da3fdfaf2770f3e60d9887c3885543aef5d5f88387",
    },
    "2020-06-30": {
        "filed": "2020-08-13",
        "accession": "0001207179-20-000018",
        "document": "golarlngltdq22020press.htm",
        "period": "q2",
        "sha256": "b6d19f7c255052ec0acc0a4b0b2225b317449fba56d3d4b1a309a95378bcfda8",
    },
    "2020-09-30": {
        "filed": "2020-11-30",
        "accession": "0001207179-20-000025",
        "document": "golarlngltdq32020press.htm",
        "period": "q3",
        "sha256": "d9c0bef7ef143ef2066c201e6633c90b92c5cdf0bf9839da8868a93c6a115557",
    },
    "2021-03-31": {
        "filed": "2021-05-20",
        "accession": "0001207179-21-000009",
        "document": "golarlngltdq12021pressrele.htm",
        "period": "q1",
        "sha256": "89a593a1683de7e55e0325a6c215f66de940bb9ff5ddd435a7d5d34956f2b538",
    },
    "2021-06-30": {
        "filed": "2021-08-09",
        "accession": "0001207179-21-000013",
        "document": "golarlngltdq22021pressrele.htm",
        "period": "q2",
        "sha256": "856232376d9c31b354e4b7c47379fd96ad977d8af8b8b96632cff023e464f8ed",
    },
    "2021-09-30": {
        "filed": "2021-11-09",
        "accession": "0001207179-21-000022",
        "document": "golarlngltdq32021pressrele.htm",
        "period": "q3",
        "sha256": "7cf47df7608de6a7e78d170d9efdf6e18df1904c599eec8efb5ef613f7004820",
    },
}

ANNUAL_SOURCES = {
    "2019": {
        "filed": "2020-04-30",
        "accession": "0001207179-20-000008",
        "document": "glng-20191231.htm",
        "sha256": "d15e04382a1f6786ec256f52283bc2999297f7cfaafe3d059e46be74f411a80a",
    },
    "2020": {
        "filed": "2021-04-22",
        "accession": "0001207179-21-000005",
        "document": "glng-20201231.htm",
        "sha256": "94389e66a08ffd90563b3e7182be648b73285bd327b608d961b6a7fd6ddf9221",
    },
}

EXPECTED_QUARTERS = {
    "2019-09-30": (98_670_000.0, -82_301_000.0),
    "2019-12-31": (139_048_000.0, 24_768_000.0),
    "2020-03-31": (122_559_000.0, -104_247_000.0),
    "2020-06-30": (102_242_000.0, -155_634_000.0),
    "2020-09-30": (95_152_000.0, -21_802_000.0),
    "2020-12-31": (118_684_000.0, 8_126_000.0),
    "2021-03-31": (125_827_000.0, 25_364_000.0),
    "2021-06-30": (104_287_000.0, 471_433_000.0),
    "2021-09-30": (106_603_000.0, -90_955_000.0),
}
EXPECTED_ANNUALS = {
    "2019": (448_750_000.0, -211_956_000.0),
    "2020": (438_637_000.0, -273_557_000.0),
}
SIGNAL_DATES = ("2021-09-30", "2021-10-29", "2021-12-31")


def _url(spec: dict) -> str:
    accession = spec["accession"].replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{CIK}/"
        f"{accession}/{spec['document']}"
    )


def _fetch(spec: dict) -> bytes:
    request = Request(_url(spec), headers=SEC_HEADERS)
    error = None
    for attempt in range(4):
        try:
            with urlopen(request, timeout=120) as response:
                return response.read()
        except Exception as exc:  # pragma: no cover - network retry
            error = exc
            if attempt < 3:
                time.sleep(attempt + 1)
    raise RuntimeError(f"failed to fetch GLNG source {_url(spec)}") from error


def _normalize(value: object) -> str:
    return " ".join(str(value).replace("\xa0", " ").split()).strip()


def _amount(value: object) -> float | None:
    text = _normalize(value)
    if not text or text.casefold() == "nan" or "%" in text:
        return None
    cleaned = re.sub(r"[^0-9.]", "", text)
    if not cleaned:
        return None
    result = float(cleaned) * 1000.0
    if abs(result) < 1_000_000:
        return None
    return -result if "(" in text or text.startswith("-") else result


def _ordered_amounts(row: pd.Series) -> list[float]:
    result = []
    for value in row.iloc[1:]:
        amount = _amount(value)
        if amount is None or (result and amount == result[-1]):
            continue
        result.append(amount)
    return result


def _table_pairs(raw: bytes) -> list[tuple[list[float], list[float]]]:
    pairs = []
    income_pattern = re.compile(
        r"^net (?:income/\(loss\)|\(loss\)/income|income|loss) "
        r"attributable to (?:stockholders of )?golar lng (?:limited|ltd)$",
        re.IGNORECASE,
    )
    for table in pd.read_html(BytesIO(raw)):
        flattened = _normalize(" ".join(map(str, table.to_numpy().ravel())))
        if "thousands of $" not in flattened.casefold():
            continue
        first = table.iloc[:, 0].map(_normalize)
        revenue_rows = list(
            table.index[first.str.fullmatch("Total operating revenues", case=False)]
        )
        income_rows = list(
            table.index[first.map(lambda value: bool(income_pattern.fullmatch(value)))]
        )
        for revenue_row in revenue_rows:
            for income_row in income_rows:
                revenue = _ordered_amounts(table.loc[revenue_row])
                net_income = _ordered_amounts(table.loc[income_row])
                if revenue and net_income:
                    pairs.append((revenue, net_income))
    if not pairs:
        raise RuntimeError("GLNG filing lacks USD revenue/parent-income table")
    return pairs


def parse_press_release(raw: bytes, period: str) -> dict[str, tuple[float, float]]:
    pairs = _table_pairs(raw)
    current = {(revenue[0], income[0]) for revenue, income in pairs}
    if len(current) != 1:
        raise RuntimeError(f"GLNG press release has ambiguous current quarter: {current}")
    result = {"current": current.pop()}
    if period in {"q2", "q3"}:
        ytd = {
            (revenue[2], income[2])
            for revenue, income in pairs
            if len(revenue) == 4 and len(income) == 4
        }
        if len(ytd) != 1:
            raise RuntimeError(f"GLNG press release has ambiguous YTD period: {ytd}")
        result["ytd"] = ytd.pop()
    return result


def parse_annual(raw: bytes) -> tuple[float, float]:
    annuals = {(revenue[0], income[0]) for revenue, income in _table_pairs(raw)}
    if len(annuals) != 1:
        raise RuntimeError(f"GLNG 20-F has ambiguous annual values: {annuals}")
    return annuals.pop()


def build_quarters(
    press: dict[str, dict[str, tuple[float, float]]],
    annuals: dict[str, tuple[float, float]],
) -> dict[str, tuple[float, float]]:
    if annuals != EXPECTED_ANNUALS:
        raise RuntimeError(f"GLNG audited annual values changed: {annuals}")
    quarters = {fiscal_end: values["current"] for fiscal_end, values in press.items()}

    for year, q2_end, q3_end in (
        ("2020", "2020-06-30", "2020-09-30"),
        ("2021", "2021-06-30", "2021-09-30"),
    ):
        q1_end = f"{year}-03-31"
        expected_h1 = tuple(
            quarters[q1_end][i] + quarters[q2_end][i] for i in range(2)
        )
        if press[q2_end]["ytd"] != expected_h1:
            raise RuntimeError(f"GLNG {year} H1 cumulative identity failed")
        expected_9m = tuple(expected_h1[i] + quarters[q3_end][i] for i in range(2))
        if press[q3_end]["ytd"] != expected_9m:
            raise RuntimeError(f"GLNG {year} 9M cumulative identity failed")

    q3_2019_ytd = press["2019-09-30"]["ytd"]
    q4_2019_residual = tuple(
        annuals["2019"][i] - q3_2019_ytd[i] for i in range(2)
    )
    if quarters["2019-12-31"] != q4_2019_residual:
        raise RuntimeError("GLNG 2019 audited FY/Q4 identity failed")

    q3_2020_ytd = press["2020-09-30"]["ytd"]
    quarters["2020-12-31"] = tuple(
        annuals["2020"][i] - q3_2020_ytd[i] for i in range(2)
    )
    if quarters != EXPECTED_QUARTERS:
        raise RuntimeError(f"GLNG recovered quarterly values changed: {quarters}")
    return quarters


def audit_signals(
    quarters: dict[str, tuple[float, float]],
    available_dates: dict[str, str],
) -> list[dict]:
    expected_windows = {
        "2021-09-30": ("2019-09-30", "2021-06-30"),
        "2021-10-29": ("2019-09-30", "2021-06-30"),
        "2021-12-31": ("2019-12-31", "2021-09-30"),
    }
    audits = []
    for signal_date in SIGNAL_DATES:
        eligible = sorted(
            fiscal_end
            for fiscal_end in quarters
            if available_dates[fiscal_end] <= signal_date
        )[-8:]
        if len(eligible) != 8 or (eligible[0], eligible[-1]) != expected_windows[signal_date]:
            raise RuntimeError(f"GLNG {signal_date} does not have the exact PIT window")
        previous, current = eligible[:4], eligible[4:]
        previous_ttm = {
            "revenue": sum(quarters[end][0] for end in previous),
            "net_income": sum(quarters[end][1] for end in previous),
        }
        current_ttm = {
            "revenue": sum(quarters[end][0] for end in current),
            "net_income": sum(quarters[end][1] for end in current),
        }
        growth = {
            metric: (current_ttm[metric] - previous_ttm[metric])
            / abs(previous_ttm[metric])
            for metric in previous_ttm
        }
        if growth["revenue"] >= 0:
            raise RuntimeError(f"GLNG {signal_date} revenue growth no longer fails")
        audits.append(
            {
                "signal_date": signal_date,
                "quarter_window": eligible,
                "previous_ttm": previous_ttm,
                "current_ttm": current_ttm,
                "growth": growth,
                "deterministic_result": "FAIL_REVENUE_GROWTH",
            }
        )
    return audits


def recover(output_dir: Path = OUTPUT_DIR) -> dict:
    press, annuals, sources = {}, {}, []
    for fiscal_end, spec in PRESS_SOURCES.items():
        raw = _fetch(spec)
        digest = hashlib.sha256(raw).hexdigest()
        if digest != spec["sha256"]:
            raise RuntimeError(f"GLNG source changed for {fiscal_end}: {digest}")
        press[fiscal_end] = parse_press_release(raw, spec["period"])
        sources.append(
            {
                "role": "quarter",
                "fiscal_end": fiscal_end,
                "filed": spec["filed"],
                "accession": spec["accession"],
                "url": _url(spec),
                "sha256": digest,
                "bytes": len(raw),
            }
        )
    for year, spec in ANNUAL_SOURCES.items():
        raw = _fetch(spec)
        digest = hashlib.sha256(raw).hexdigest()
        if digest != spec["sha256"]:
            raise RuntimeError(f"GLNG annual source changed for {year}: {digest}")
        annuals[year] = parse_annual(raw)
        sources.append(
            {
                "role": "annual_identity",
                "fiscal_year": year,
                "filed": spec["filed"],
                "accession": spec["accession"],
                "url": _url(spec),
                "sha256": digest,
                "bytes": len(raw),
            }
        )

    quarters = build_quarters(press, annuals)
    available_dates = {
        fiscal_end: spec["filed"] for fiscal_end, spec in PRESS_SOURCES.items()
    }
    available_dates["2020-12-31"] = ANNUAL_SOURCES["2020"]["filed"]
    signal_audit = audit_signals(quarters, available_dates)

    fetched_at = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    rows = []
    for fiscal_end, (revenue, net_income) in quarters.items():
        if fiscal_end == "2020-12-31":
            accession = (
                ANNUAL_SOURCES["2020"]["accession"]
                + "+"
                + PRESS_SOURCES["2020-09-30"]["accession"]
            )
            form = "20-F_MINUS_6-K_9M"
        else:
            accession = PRESS_SOURCES[fiscal_end]["accession"]
            form = "6-K:PRESS_RELEASE:CURRENT_QUARTER"
        for metric, value in (("revenue", revenue), ("net_income", net_income)):
            rows.append(
                {
                    "ticker": "GLNG",
                    "fiscal_end": fiscal_end,
                    "available_date": available_dates[fiscal_end],
                    "metric": metric,
                    "value": value,
                    "taxonomy": "us-gaap",
                    "concept": (
                        "sec_issuer:TotalOperatingRevenues"
                        if metric == "revenue"
                        else "sec_issuer:NetIncomeLossAttributableToGolarLNG"
                    ),
                    "form": form,
                    "accession": accession,
                    "fetched_at": fetched_at,
                }
            )
    facts = pd.DataFrame(rows, columns=OUTPUT_COLUMNS).sort_values(
        ["available_date", "fiscal_end", "metric"]
    ).reset_index(drop=True)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "shared_candidate_integrated": False,
        "ticker": "GLNG",
        "cik": CIK,
        "currency": "USD",
        "source_scale": "thousands",
        "accepted_quarter_count": len(quarters),
        "accepted_fact_count": len(facts),
        "sources": sources,
        "signal_audit": signal_audit,
        "excluded_preliminary_2020_q4": {
            "filed": "2021-02-25",
            "accession": "0001171843-21-001307",
            "reported_parent_net_income": 9_456_000.0,
            "reason": (
                "The original audited 2020 20-F filed 2021-04-22 implies "
                "8,126,000 via FY minus 9M. Availability is therefore not "
                "backdated to the preliminary release."
            ),
        },
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path),
                "sha256": hashlib.sha256(facts_path.read_bytes()).hexdigest(),
            }
        },
        "guardrail": (
            "Only source-locked SEC issuer releases and original 20-Fs are used. "
            "USD thousands are scaled to dollars; quarter and YTD columns are "
            "validated separately. Net income is attributable to Golar LNG "
            "Limited, matching NetIncomeLoss, and excludes the consolidated "
            "ProfitLoss amount including non-controlling interests. The 2021Q3 "
            "release is unavailable to the September and October signals."
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
    args = parser.parse_args()
    report = recover(args.output_dir)
    print(
        json.dumps(
            {
                "accepted_quarter_count": report["accepted_quarter_count"],
                "manifest": report["manifest"],
                "release_status": report["release_status"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
