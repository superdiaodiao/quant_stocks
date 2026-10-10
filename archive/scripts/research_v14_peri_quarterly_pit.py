#!/usr/bin/env python3
"""Recover PERI's source-locked USD quarters at its two missing PIT signals."""

from __future__ import annotations

import argparse
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import time
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
import pandas as pd


CIK = 1_338_940
OUTPUT_DIR = Path("output/research_only/v14/peri_quarterly_pit")
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
OUTPUT_COLUMNS = [
    "ticker",
    "fiscal_end",
    "available_date",
    "metric",
    "value",
    "taxonomy",
    "concept",
    "form",
    "accession",
    "fetched_at",
]

QUARTER_SOURCES = {
    "2018-12-31": {
        "filed": "2019-02-13",
        "accession": "0001178913-19-000392",
        "document": "exhibit_99-1.htm",
        "sha256": "42b89533b4eedddc8a3b3b089175f77d28639644ff73938b02e78428324f05ea",
    },
    "2019-03-31": {
        "filed": "2019-05-15",
        "accession": "0001178913-19-001478",
        "document": "exhibit_99-1.htm",
        "sha256": "0776b4f165dfea4f866c701da0f04e5228cf95b43261132da6eab8f7ec148694",
    },
    "2019-06-30": {
        "filed": "2019-08-07",
        "accession": "0001178913-19-002072",
        "document": "exhibit_99-1.htm",
        "sha256": "3bd9be8b7602fe02541186b4940c1884b23d3bef55f71b114cc72b7c71355a03",
    },
    "2019-09-30": {
        "filed": "2019-11-06",
        "accession": "0001178913-19-002597",
        "document": "exhibit_99-1.htm",
        "sha256": "e3b65f3d283a7f894f495cc936c9c841b47688d3acfe44bf5ee9e7c50497bba1",
    },
    "2019-12-31": {
        "filed": "2020-02-12",
        "accession": "0001178913-20-000354",
        "document": "exhibit_99-1.htm",
        "sha256": "7164bc1b22c40ad2288de7cd57cbe5f55b65da825608996769f7bd05a1639362",
    },
    "2020-03-31": {
        "filed": "2020-05-06",
        "accession": "0001178913-20-001323",
        "document": "exhibit_99-1.htm",
        "sha256": "001b62ae0d02e6a76b302cb413c4edd4d2a12fae8c04329b7ddaa575ab9a399f",
    },
    "2020-06-30": {
        "filed": "2020-08-05",
        "accession": "0001178913-20-002240",
        "document": "exhibit_99-1.htm",
        "sha256": "084c9c95b3eabef84a0c60f8d1b663e7456f581a502df1cb0f993dab213af73b",
    },
    "2020-09-30": {
        "filed": "2020-10-28",
        "accession": "0001178913-20-002911",
        "document": "exhibit_99-1.htm",
        "sha256": "9e1f7b2997c1a99c3c1583d463583f414703d39ca30a65ba9f411ef824e651e6",
    },
    "2020-12-31": {
        "filed": "2021-02-09",
        "accession": "0001178913-21-000383",
        "document": "exhibit_99-1.htm",
        "sha256": "e3c0b6de055eed76d1d15c900a1c8a4521db676537d6db82de435c08b80abf1a",
    },
    "2021-03-31": {
        "filed": "2021-05-04",
        "accession": "0001178913-21-001569",
        "document": "exhibit_99-1.htm",
        "sha256": "c53cc77fb331e5602118a8aad4563b132a23abaa36cf5996a65e84a690b2476c",
    },
    "2021-06-30": {
        "filed": "2021-08-03",
        "accession": "0001178913-21-002487",
        "document": "exhibit_99-1.htm",
        "sha256": "963388992bc64a5f981f44b6c1d7cdf09dd0d239cea8e802b31c7e03ae0d21e3",
    },
    "2021-09-30": {
        "filed": "2021-10-26",
        "accession": "0001178913-21-003247",
        "document": "exhibit_99-1.htm",
        "sha256": "b8bef859536213231e798b6235bb0042d9b6665bd2bbbe62fa7c23fe555d3854",
    },
}

ANNUAL_SOURCES = {
    "2018": {
        "filed": "2019-03-19",
        "accession": "0001178913-19-000875",
        "document": "zk1922805.htm",
        "sha256": "f48ffb0c1230a38b49a2bc45649cdc9ad12baf152ac2f8316cc70e51f35ac06c",
    },
    "2019": {
        "filed": "2020-03-16",
        "accession": "0001178913-20-000826",
        "document": "zk2024148.htm",
        "sha256": "8d5a644d7c54c13c5f90a8425a697ccec9878a044abb53a3442acc22dab74cd4",
    },
    "2020": {
        "filed": "2021-03-25",
        "accession": "0001178913-21-001193",
        "document": "peri20f1220.htm",
        "sha256": "63d0c56bf6399c2aeb02d6e55d4af2a3854e526e625a4392f322d68924e02531",
    },
}

EXPECTED_QUARTERS = {
    "2018-12-31": (71_962_000.0, 4_887_000.0),
    "2019-03-31": (53_849_000.0, 1_232_000.0),
    "2019-06-30": (63_567_000.0, 2_900_000.0),
    "2019-09-30": (65_777_000.0, 2_874_000.0),
    "2019-12-31": (78_257_000.0, 5_887_000.0),
    "2020-03-31": (66_053_000.0, 1_334_000.0),
    "2020-06-30": (60_341_000.0, -2_239_000.0),
    "2020-09-30": (83_413_000.0, 2_128_000.0),
    "2020-12-31": (118_256_000.0, 9_002_000.0),
    "2021-03-31": (89_817_000.0, 3_306_000.0),
    "2021-06-30": (109_677_000.0, 7_083_000.0),
    "2021-09-30": (121_029_000.0, 10_622_000.0),
}
EXPECTED_ANNUALS = {
    "2018": (252_845_000.0, 8_121_000.0),
    "2019": (261_450_000.0, 12_893_000.0),
    "2020": (328_063_000.0, 10_225_000.0),
}
EXPECTED_CUMULATIVE = {
    "2019_h1": (117_416_000.0, 4_132_000.0),
    "2019_9m": (183_193_000.0, 7_006_000.0),
    "2020_h1": (126_394_000.0, -905_000.0),
    "2020_9m": (209_807_000.0, 1_223_000.0),
    "2021_h1": (199_494_000.0, 10_389_000.0),
    "2021_9m": (320_523_000.0, 21_011_000.0),
}
SIGNAL_SCENARIOS = {
    "2021-01-29": (
        "liq10000000-age150-growth",
        "liq2000000-age150-growth",
    ),
    "2021-10-29": (
        "liq2000000-age150-growth",
        "liq2000000-age365-growth",
        "liq2000000-age550-growth",
    ),
}


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
    raise RuntimeError(f"failed to fetch PERI source {_url(spec)}") from error


def _normalize(value: object) -> str:
    return " ".join(str(value).replace("\xa0", " ").split()).strip()


def _amount(value: object) -> float | None:
    text = _normalize(value)
    if not text or text.casefold() == "nan" or text in {"—", "-"} or "%" in text:
        return None
    cleaned = re.sub(r"[^0-9.]", "", text)
    if not cleaned:
        return None
    amount = float(cleaned) * 1000.0
    # PERI's 2020 H1 GAAP loss is only $905 thousand.  The 100-thousand
    # floor still rejects percentage and per-share rows in the same tables.
    if amount <= 100_000:
        return None
    return -amount if "(" in text or text.startswith("-") else amount


def _header_cells(table: pd.DataFrame, column: int) -> list[str]:
    column_name = table.columns[column]
    names = column_name if isinstance(column_name, tuple) else (column_name,)
    result = [
        _normalize(value)
        for value in names
        if not str(value).casefold().startswith("unnamed")
    ]
    result.extend(
        _normalize(table.iat[row, column]) for row in range(min(4, len(table)))
    )
    return result


def _period_columns(
    table: pd.DataFrame, fiscal_end: str, period_phrase: str
) -> list[int]:
    target = pd.Timestamp(fiscal_end)
    month_day = f"{target.strftime('%B')} {target.day}".casefold()
    year = str(target.year)
    columns = []
    for column in range(len(table.columns)):
        header = " ".join(_header_cells(table, column)).replace(",", "").casefold()
        if period_phrase.casefold() in header and month_day in header and year in header:
            columns.append(column)
    return columns


def _metric(
    table: pd.DataFrame,
    fiscal_end: str,
    period_phrase: str,
    labels: set[str],
) -> float | None:
    if "$" not in set(map(str, table.to_numpy().ravel())):
        return None
    columns = _period_columns(table, fiscal_end, period_phrase)
    values = set()
    for _, row in table.iterrows():
        label = _normalize(row.iloc[0]).casefold()
        if label not in labels:
            continue
        for column in columns:
            value = _amount(row.iloc[column])
            if value is not None:
                values.add(value)
    return values.pop() if len(values) == 1 else None


def parse_period(
    raw: bytes, fiscal_end: str, period_phrase: str
) -> tuple[float, float]:
    text = " ".join(BeautifulSoup(raw, "html.parser").get_text(" ").split())
    if "PERION NETWORK LTD. AND ITS SUBSIDIARIES" not in text.upper():
        raise RuntimeError("PERI issuer/consolidation identity is absent")
    if "In thousands" not in text:
        raise RuntimeError("PERI source scale is absent")
    candidates = set()
    for table in pd.read_html(BytesIO(raw)):
        revenue = _metric(
            table, fiscal_end, period_phrase, {"total revenues", "revenues"}
        )
        net_income = _metric(
            table,
            fiscal_end,
            period_phrase,
            {"net income", "net income (loss)", "net loss"},
        )
        if revenue is not None and net_income is not None:
            candidates.add((revenue, net_income))
    if len(candidates) != 1:
        raise RuntimeError(
            f"PERI {fiscal_end} {period_phrase} USD values are ambiguous: {candidates}"
        )
    return candidates.pop()


def validate_quarters(
    quarters: dict[str, tuple[float, float]],
    annuals: dict[str, tuple[float, float]],
    cumulative: dict[str, tuple[float, float]],
) -> None:
    if quarters != EXPECTED_QUARTERS:
        raise RuntimeError(f"PERI recovered quarters changed: {quarters}")
    if annuals != EXPECTED_ANNUALS:
        raise RuntimeError(f"PERI audited annuals changed: {annuals}")
    if cumulative != EXPECTED_CUMULATIVE:
        raise RuntimeError(f"PERI cumulative periods changed: {cumulative}")

    for year in (2019, 2020):
        annual_sum = tuple(
            sum(
                quarters[f"{year}-{month_day}"][index]
                for month_day in ("03-31", "06-30", "09-30", "12-31")
            )
            for index in range(2)
        )
        if annual_sum != annuals[str(year)]:
            raise RuntimeError(f"PERI {year} FY/quarter identity failed")
    for year in (2019, 2020, 2021):
        h1 = tuple(
            quarters[f"{year}-03-31"][index] + quarters[f"{year}-06-30"][index]
            for index in range(2)
        )
        nine_months = tuple(
            h1[index] + quarters[f"{year}-09-30"][index]
            for index in range(2)
        )
        if h1 != cumulative[f"{year}_h1"]:
            raise RuntimeError(f"PERI {year} H1 identity failed")
        if nine_months != cumulative[f"{year}_9m"]:
            raise RuntimeError(f"PERI {year} 9M identity failed")


def build_quarters(
    raw_quarters: dict[str, bytes], raw_annuals: dict[str, bytes]
) -> dict[str, tuple[float, float]]:
    quarters = {
        fiscal_end: parse_period(raw, fiscal_end, "three months ended")
        for fiscal_end, raw in raw_quarters.items()
    }
    annuals = {
        year: parse_period(raw, f"{year}-12-31", "year ended")
        for year, raw in raw_annuals.items()
    }
    for year in (2018, 2019, 2020):
        release_annual = parse_period(
            raw_quarters[f"{year}-12-31"], f"{year}-12-31", "year ended"
        )
        if release_annual != annuals[str(year)]:
            raise RuntimeError(f"PERI {year} original 6-K/20-F annual mismatch")
    cumulative = {}
    for year in (2019, 2020, 2021):
        cumulative[f"{year}_h1"] = parse_period(
            raw_quarters[f"{year}-06-30"], f"{year}-06-30", "six months ended"
        )
        cumulative[f"{year}_9m"] = parse_period(
            raw_quarters[f"{year}-09-30"], f"{year}-09-30", "nine months ended"
        )
    validate_quarters(quarters, annuals, cumulative)
    return quarters


def audit_signals(quarters: dict[str, tuple[float, float]]) -> list[dict]:
    expected_windows = {
        "2021-01-29": ("2018-12-31", "2020-09-30"),
        "2021-10-29": ("2019-12-31", "2021-09-30"),
    }
    audits = []
    for signal_date, scenarios in SIGNAL_SCENARIOS.items():
        eligible = sorted(
            fiscal_end
            for fiscal_end in quarters
            if QUARTER_SOURCES[fiscal_end]["filed"] <= signal_date
        )[-8:]
        if len(eligible) != 8 or (eligible[0], eligible[-1]) != expected_windows[signal_date]:
            raise RuntimeError(f"PERI {signal_date} exact PIT window failed: {eligible}")
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
        if current_ttm["net_income"] < 0:
            result = "EXCLUDE_EXACT_NEGATIVE_NET_INCOME_TTM"
        elif growth["revenue"] <= 0:
            result = "FAIL_REVENUE_GROWTH"
        elif growth["net_income"] <= 0:
            result = "FAIL_NET_INCOME_GROWTH"
        else:
            result = "PASS_POSITIVE_REVENUE_AND_NET_INCOME_GROWTH"
        last_end = eligible[-1]
        audits.append(
            {
                "signal_date": signal_date,
                "affected_scenarios": list(scenarios),
                "missing_observation_count": len(scenarios),
                "quarter_window": eligible,
                "last_available_financial_filing": {
                    "fiscal_end": last_end,
                    "filed": QUARTER_SOURCES[last_end]["filed"],
                    "accession": QUARTER_SOURCES[last_end]["accession"],
                    "url": _url(QUARTER_SOURCES[last_end]),
                },
                "previous_ttm": previous_ttm,
                "current_ttm": current_ttm,
                "growth": growth,
                "deterministic_result": result,
            }
        )
    if sum(row["missing_observation_count"] for row in audits) != 5:
        raise RuntimeError("PERI aggregate missing-observation count changed")
    return audits


def recover(output_dir: Path = OUTPUT_DIR) -> dict:
    raw_quarters, raw_annuals, sources = {}, {}, []
    for role, registry, destination in (
        ("quarter", QUARTER_SOURCES, raw_quarters),
        ("annual_identity", ANNUAL_SOURCES, raw_annuals),
    ):
        for key, spec in registry.items():
            raw = _fetch(spec)
            digest = hashlib.sha256(raw).hexdigest()
            if digest != spec["sha256"]:
                raise RuntimeError(f"PERI source changed for {key}: {digest}")
            destination[key] = raw
            sources.append(
                {
                    "role": role,
                    "period": key,
                    "form": "6-K:EX-99.1" if role == "quarter" else "20-F",
                    "filed": spec["filed"],
                    "accession": spec["accession"],
                    "url": _url(spec),
                    "sha256": digest,
                    "bytes": len(raw),
                }
            )

    quarters = build_quarters(raw_quarters, raw_annuals)
    signal_audit = audit_signals(quarters)
    fetched_at = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    rows = []
    for fiscal_end, (revenue, net_income) in quarters.items():
        spec = QUARTER_SOURCES[fiscal_end]
        for metric, value, concept in (
            ("revenue", revenue, "sec_issuer:TotalRevenues"),
            ("net_income", net_income, "NetIncomeLoss"),
        ):
            rows.append(
                {
                    "ticker": "PERI",
                    "fiscal_end": fiscal_end,
                    "available_date": spec["filed"],
                    "metric": metric,
                    "value": value,
                    "taxonomy": "us-gaap",
                    "concept": concept,
                    "form": "6-K:EX-99.1:CURRENT_QUARTER",
                    "accession": spec["accession"],
                    "fetched_at": fetched_at,
                }
            )
    facts = pd.DataFrame(rows, columns=OUTPUT_COLUMNS).sort_values(
        ["available_date", "fiscal_end", "metric"]
    )
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
        "ticker": "PERI",
        "cik": CIK,
        "currency": "USD",
        "source_scale": "thousands",
        "accepted_quarter_count": len(quarters),
        "accepted_fact_count": len(facts),
        "aggregate_missing_observation_count": 5,
        "sources": sources,
        "signal_audit": signal_audit,
        "profit_ownership": {
            "accepted": "consolidated GAAP net income of Perion Network Ltd. and its subsidiaries",
            "concept": "NetIncomeLoss",
            "excluded": ["adjusted net income", "adjusted EBITDA", "earnings per share"],
        },
        "revision_isolation": {
            "accepted_original_annuals": ["2018", "2019", "2020"],
            "amended_financials_used": False,
            "later_comparatives_used": False,
            "post_signal_2020q4_excluded_from_2021_01_29": {
                "filed": "2021-02-09",
                "accession": "0001178913-21-000383",
            },
        },
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path),
                "sha256": hashlib.sha256(facts_path.read_bytes()).hexdigest(),
            }
        },
        "guardrail": (
            "Only hash-locked original SEC issuer 6-K earnings exhibits and original "
            "20-Fs are accepted. Every emitted amount is an explicit current-quarter "
            "USD-thousands consolidated GAAP value. Cumulative periods, non-GAAP "
            "metrics, adjacent non-earnings 6-Ks, amended filings, and post-signal "
            "quarters are excluded separately for each signal."
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
                "aggregate_missing_observation_count": report[
                    "aggregate_missing_observation_count"
                ],
                "manifest": report["manifest"],
                "release_status": report["release_status"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
