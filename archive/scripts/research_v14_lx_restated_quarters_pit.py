#!/usr/bin/env python3
"""Recover LX's exact restated CNY quarters for the 2019-12-31 PIT gap."""

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


CIK = 1_708_259
SIGNAL_DATE = "2019-12-31"
OUTPUT_DIR = Path("output/research_only/v14/lx_restated_quarters_pit")
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

SOURCES = {
    "2017_q4": {
        "filed": "2018-03-20",
        "accession": "0001104659-18-018907",
        "document": "a18-8711_1ex99d1.htm",
        "role": "original_2017q4_current_quarter",
        "sha256": "b79a035942ea53c44a2100bf31f2653a57283f210628480f2672812cd90c940a",
    },
    "2018_q4_revision": {
        "filed": "2019-03-15",
        "accession": "0001104659-19-015012",
        "document": "a19-6588_1ex99d1.htm",
        "role": "revised_2018q1_q3_and_original_2018q4",
        "sha256": "e23fb3eb0e9d9ae0221b43be3ca8ff54d8c84a9666be6ff4e4e502be60f6651b",
    },
    "2018_20f": {
        "filed": "2019-04-30",
        "accession": "0001104659-19-025320",
        "document": "a19-1147_120f.htm",
        "role": "original_audited_2018_annual_identity",
        "sha256": "09d29fa3d90ecfa1b614701dd9709af3391f5886845c71cd10f93c09f5d885f7",
    },
    "2019_q2_restatement": {
        "filed": "2019-08-30",
        "accession": "0001104659-19-048111",
        "document": "a19-18113_1ex99d1.htm",
        "role": "restated_2019q1_and_original_2019q2",
        "sha256": "0a463b0812aa896bc8f4acf96552abe34f4286e1a26784c80f2c82e077cf8d2d",
    },
    "2019_q3": {
        "filed": "2019-11-18",
        "accession": "0001104659-19-064861",
        "document": "a19-23254_1ex99d1.htm",
        "role": "original_2019q3_and_nine_month_identity",
        "sha256": "cbb8be333d089b769fd9d2bb19e9c779044f2d6ee32a8763bc44c053d12fb5e1",
    },
}

EXPECTED_QUARTERS = {
    "2017-12-31": (1_593_702_000.0, 100_438_000.0),
    "2018-03-31": (1_613_718_000.0, 177_860_000.0),
    "2018-06-30": (2_039_417_000.0, 663_802_000.0),
    "2018-09-30": (1_850_116_000.0, 447_227_000.0),
    "2018-12-31": (2_093_645_000.0, 688_417_000.0),
    "2019-03-31": (1_774_510_000.0, 424_300_000.0),
    "2019-06-30": (2_492_940_000.0, 627_964_000.0),
    "2019-09-30": (3_187_996_000.0, 724_366_000.0),
}
EXPECTED_2018_ANNUAL = (7_596_896_000.0, 1_977_306_000.0)
EXPECTED_2019_H1 = (4_267_450_000.0, 1_052_264_000.0)
EXPECTED_2019_9M = (7_455_446_000.0, 1_776_630_000.0)
AVAILABLE_DATES = {
    "2017-12-31": "2018-03-20",
    "2018-03-31": "2019-03-15",
    "2018-06-30": "2019-03-15",
    "2018-09-30": "2019-03-15",
    "2018-12-31": "2019-03-15",
    "2019-03-31": "2019-08-30",
    "2019-06-30": "2019-08-30",
    "2019-09-30": "2019-11-18",
}
SOURCE_FOR_QUARTER = {
    "2017-12-31": "2017_q4",
    "2018-03-31": "2018_q4_revision",
    "2018-06-30": "2018_q4_revision",
    "2018-09-30": "2018_q4_revision",
    "2018-12-31": "2018_q4_revision",
    "2019-03-31": "2019_q2_restatement",
    "2019-06-30": "2019_q2_restatement",
    "2019-09-30": "2019_q3",
}
SCENARIOS = (
    "liq10000000-age150-growth",
    "liq10000000-age365-growth",
    "liq10000000-age550-growth",
    "liq2000000-age150-growth",
    "liq2000000-age365-growth",
    "liq2000000-age550-growth",
)


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
    raise RuntimeError(f"failed to fetch LX source {_url(spec)}") from error


def _normalize(value: object) -> str:
    return " ".join(str(value).replace("\xa0", " ").split()).strip()


def _label(value: object) -> str:
    return _normalize(value).rstrip("*").strip().casefold()


def _amount(value: object) -> float | None:
    text = _normalize(value)
    if not text or text.casefold() == "nan" or text in {"—", "-"}:
        return None
    cleaned = re.sub(r"[^0-9.]", "", text)
    if not cleaned:
        return None
    amount = float(cleaned) * 1000.0
    return -amount if "(" in text or text.startswith("-") else amount


def _period_columns(
    table: pd.DataFrame, fiscal_end: str, period_phrase: str
) -> list[int]:
    target = pd.Timestamp(fiscal_end)
    month_day = f"{target.strftime('%B')} {target.day}".casefold()
    year = str(target.year)
    columns = []
    for column in range(len(table.columns)):
        header_cells = [_normalize(table.iat[row, column]) for row in range(min(4, len(table)))]
        header = " ".join(header_cells).replace(",", "").casefold()
        if (
            period_phrase.casefold() in header
            and month_day in header
            and year in header
            and any(cell.casefold() == "rmb" for cell in header_cells)
        ):
            columns.append(column)
    return columns


def _metric(
    table: pd.DataFrame,
    fiscal_end: str,
    period_phrase: str,
    labels: set[str],
) -> float | None:
    columns = _period_columns(table, fiscal_end, period_phrase)
    if len(columns) != 1:
        return None
    rows = [
        row
        for _, row in table.iterrows()
        if _label(row.iloc[0]) in labels
    ]
    values = {
        value
        for row in rows
        if (value := _amount(row.iloc[columns[0]])) is not None
    }
    if len(values) != 1:
        return None
    return values.pop()


def parse_periods(
    raw: bytes, fiscal_ends: tuple[str, ...], period_phrase: str
) -> dict[str, tuple[float, float]]:
    candidates = {fiscal_end: set() for fiscal_end in fiscal_ends}
    for table in pd.read_html(BytesIO(raw)):
        for fiscal_end in fiscal_ends:
            revenue = _metric(
                table,
                fiscal_end,
                period_phrase,
                {"total operating revenue"},
            )
            net_income = _metric(
                table,
                fiscal_end,
                period_phrase,
                {"net income", "net (loss)/income"},
            )
            if revenue is not None and net_income is not None:
                candidates[fiscal_end].add((revenue, net_income))
    result = {}
    for fiscal_end, values in candidates.items():
        if len(values) != 1:
            raise RuntimeError(
                f"LX {fiscal_end} {period_phrase} CNY values are ambiguous: {values}"
            )
        result[fiscal_end] = values.pop()
    return result


def build_quarters(raw_sources: dict[str, bytes]) -> dict[str, tuple[float, float]]:
    quarters = {}
    quarters.update(
        parse_periods(raw_sources["2017_q4"], ("2017-12-31",), "three months ended")
    )
    quarters.update(
        parse_periods(
            raw_sources["2018_q4_revision"],
            ("2018-03-31", "2018-06-30", "2018-09-30", "2018-12-31"),
            "three months ended",
        )
    )
    quarters.update(
        parse_periods(
            raw_sources["2019_q2_restatement"],
            ("2019-03-31", "2019-06-30"),
            "three months ended",
        )
    )
    quarters.update(
        parse_periods(raw_sources["2019_q3"], ("2019-09-30",), "three months ended")
    )
    if quarters != EXPECTED_QUARTERS:
        raise RuntimeError(f"LX recovered restated quarters changed: {quarters}")

    release_2018 = parse_periods(
        raw_sources["2018_q4_revision"], ("2018-12-31",), "year ended"
    )["2018-12-31"]
    audited_2018 = parse_periods(
        raw_sources["2018_20f"], ("2018-12-31",), "year ended"
    )["2018-12-31"]
    sum_2018 = tuple(
        sum(quarters[f"2018-{month_day}"][index] for month_day in ("03-31", "06-30", "09-30", "12-31"))
        for index in range(2)
    )
    if release_2018 != EXPECTED_2018_ANNUAL or audited_2018 != EXPECTED_2018_ANNUAL:
        raise RuntimeError("LX original 2018 6-K/20-F annual identity changed")
    if sum_2018 != EXPECTED_2018_ANNUAL:
        raise RuntimeError("LX revised 2018 quarterly sum does not equal audited FY")

    h1 = parse_periods(
        raw_sources["2019_q2_restatement"], ("2019-06-30",), "six months ended"
    )["2019-06-30"]
    nine_months = parse_periods(
        raw_sources["2019_q3"], ("2019-09-30",), "nine months ended"
    )["2019-09-30"]
    calculated_h1 = tuple(
        quarters["2019-03-31"][index] + quarters["2019-06-30"][index]
        for index in range(2)
    )
    calculated_9m = tuple(
        calculated_h1[index] + quarters["2019-09-30"][index]
        for index in range(2)
    )
    if h1 != EXPECTED_2019_H1 or calculated_h1 != h1:
        raise RuntimeError("LX restated 2019 H1 identity failed")
    if nine_months != EXPECTED_2019_9M or calculated_9m != nine_months:
        raise RuntimeError("LX 2019 nine-month identity failed")
    return quarters


def audit_signal(quarters: dict[str, tuple[float, float]]) -> dict:
    eligible = sorted(
        fiscal_end
        for fiscal_end in quarters
        if AVAILABLE_DATES[fiscal_end] <= SIGNAL_DATE
    )
    if eligible != sorted(EXPECTED_QUARTERS):
        raise RuntimeError(f"LX does not have the exact eight-quarter PIT window: {eligible}")
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
    if growth["revenue"] <= 0 or growth["net_income"] <= 0:
        raise RuntimeError(f"LX recovered growth is not positive: {growth}")
    return {
        "signal_date": SIGNAL_DATE,
        "affected_scenarios": list(SCENARIOS),
        "missing_observation_count": len(SCENARIOS),
        "quarter_window": eligible,
        "previous_ttm": previous_ttm,
        "current_ttm": current_ttm,
        "growth": growth,
        "deterministic_result": "PASS_POSITIVE_REVENUE_AND_NET_INCOME_GROWTH",
    }


def recover(output_dir: Path = OUTPUT_DIR) -> dict:
    raw_sources, source_manifest = {}, []
    for name, spec in SOURCES.items():
        raw = _fetch(spec)
        digest = hashlib.sha256(raw).hexdigest()
        if digest != spec["sha256"]:
            raise RuntimeError(f"LX source changed for {name}: {digest}")
        raw_sources[name] = raw
        source_manifest.append(
            {
                "name": name,
                "role": spec["role"],
                "form": "20-F" if name == "2018_20f" else "6-K:EX-99.1",
                "filed": spec["filed"],
                "accession": spec["accession"],
                "url": _url(spec),
                "sha256": digest,
                "bytes": len(raw),
            }
        )

    quarters = build_quarters(raw_sources)
    signal_audit = audit_signal(quarters)
    fetched_at = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    rows = []
    for fiscal_end, (revenue, net_income) in quarters.items():
        source_name = SOURCE_FOR_QUARTER[fiscal_end]
        spec = SOURCES[source_name]
        revised = fiscal_end in {"2018-03-31", "2018-06-30", "2018-09-30", "2019-03-31"}
        for metric, value, concept in (
            ("revenue", revenue, "sec_issuer:TotalOperatingRevenue"),
            ("net_income", net_income, "sec_issuer:NetIncomeLoss"),
        ):
            rows.append(
                {
                    "ticker": "LX",
                    "fiscal_end": fiscal_end,
                    "available_date": AVAILABLE_DATES[fiscal_end],
                    "metric": metric,
                    "value": value,
                    "taxonomy": "us-gaap",
                    "concept": concept,
                    "form": (
                        "6-K:EX-99.1:RESTATED_CURRENT_QUARTER"
                        if revised
                        else "6-K:EX-99.1:CURRENT_QUARTER"
                    ),
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
        "ticker": "LX",
        "cik": CIK,
        "signal_date": SIGNAL_DATE,
        "currency": "CNY",
        "source_scale": "thousands",
        "accepted_quarter_count": len(quarters),
        "accepted_fact_count": len(facts),
        "sources": source_manifest,
        "signal_audit": signal_audit,
        "revision_isolation": {
            "2018_q1_q3": {
                "available_date": "2019-03-15",
                "reason": "ASC 606 and other public-company standards were applied to all three prior quarters",
                "excluded_original_accessions": [
                    "0001104659-18-035295",
                    "0001104659-18-053650",
                    "0001104659-18-068625",
                ],
            },
            "2019_q1": {
                "available_date": "2019-08-30",
                "reason": "issuer corrected overstated financial-services revenue and an out-of-period adjustment",
                "excluded_original_accession": "0001104659-19-030127",
            },
            "excluded_post_signal_2019_20f": {
                "filed": "2020-04-30",
                "accession": "0001104659-20-053901",
            },
        },
        "profit_ownership": {
            "accepted": "consolidated GAAP net income (NetIncomeLoss)",
            "excluded": [
                "adjusted net income",
                "net income attributable to ordinary shareholders after pre-IPO preferred allocations",
            ],
        },
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path),
                "sha256": hashlib.sha256(facts_path.read_bytes()).hexdigest(),
            }
        },
        "guardrail": (
            "Only hash-locked SEC issuer 6-K exhibits and the original pre-signal "
            "2018 20-F are accepted. Every emitted amount is an explicit CNY-"
            "thousands three-month GAAP value. USD convenience translations, "
            "adjusted income, ordinary-shareholder allocations, cumulative columns, "
            "superseded original quarters, and the post-signal 2019 20-F are excluded."
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
