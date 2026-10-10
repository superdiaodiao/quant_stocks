#!/usr/bin/env python3
"""Recover MMYT's source-locked USD quarters for its two missing PIT signals."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
import pandas as pd


CIK = 1_495_153
OUTPUT_DIR = Path("output/research_only/v14/mmyt_quarterly_pit")
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
    "2019-03-31": {
        "filed": "2019-05-16",
        "accession": "0001564590-19-019513",
        "sha256": "b0c3d003deebc3c3d6b460f14a6a340cab6929f49acfc0e9ee15903f0531194e",
        "expected_revenue": (
            157_806_000.0,
            120_177_000.0,
            675_256_000.0,
            486_011_000.0,
        ),
        "expected_net_income": (
            -44_117_000.0,
            -40_393_000.0,
            -220_240_000.0,
            -167_883_000.0,
        ),
    },
    "2019-06-30": {
        "filed": "2019-07-30",
        "accession": "0001564590-19-026721",
        "sha256": "fb48332a859191d2ca7a9f49d27909aadbce2d5f3d18278fdb64869dfea30655",
        "expected_revenue": (137_410_000.0, 141_737_000.0),
        "expected_net_income": (-51_231_000.0, -42_592_000.0),
    },
    "2019-09-30": {
        "filed": "2019-11-04",
        "accession": "0000950123-19-009998",
        "sha256": "63cd71ba870b32b078b4605be56b1f88168c8e282cf6fe47a6b866f88416d440",
        "expected_revenue": (
            103_609_000.0,
            117_957_000.0,
            241_019_000.0,
            259_694_000.0,
        ),
        "expected_net_income": (
            -46_965_000.0,
            -36_803_000.0,
            -98_196_000.0,
            -79_395_000.0,
        ),
    },
    "2019-12-31": {
        "filed": "2020-02-11",
        "accession": "0001564590-20-004110",
        "sha256": "3d014f64eb9aab47100a14a650d2f336033f7b3fb2bc53dc5f0776996fabef7c",
        "expected_revenue": (
            124_815_000.0,
            146_889_000.0,
            365_834_000.0,
            406_583_000.0,
        ),
        "expected_net_income": (
            -29_294_000.0,
            -29_511_000.0,
            -127_490_000.0,
            -108_906_000.0,
        ),
    },
    "2020-03-31": {
        "filed": "2020-06-26",
        "accession": "0001564590-20-030663",
        "sha256": "1e31e99410559252956bf2e0cbcf5273b19f7a5d86470933e4c1e18a4a396a8a",
        "expected_revenue": (
            120_177_000.0,
            104_946_000.0,
            486_011_000.0,
            511_529_000.0,
        ),
        "expected_net_income": (
            -40_393_000.0,
            -338_611_000.0,
            -167_883_000.0,
            -447_517_000.0,
        ),
    },
    "2020-06-30": {
        "filed": "2020-08-21",
        "accession": "0001564590-20-040855",
        "sha256": "0fcef502f557022294d3ccfa86e718241d5eea8cb81bc031ec066bea7140cf67",
        "expected_revenue": (141_737_000.0, 6_361_000.0),
        "expected_net_income": (-42_592_000.0, -34_570_000.0),
    },
    "2020-09-30": {
        "filed": "2020-10-27",
        "accession": "0001564590-20-047764",
        "sha256": "5412f06d7cff7ceb19ed23d5be8d1f39f15749375a14b24d07b2f971920a2dd9",
        "expected_revenue": (
            117_957_000.0,
            21_052_000.0,
            259_694_000.0,
            27_413_000.0,
        ),
        "expected_net_income": (
            -36_803_000.0,
            -21_177_000.0,
            -79_395_000.0,
            -55_747_000.0,
        ),
    },
    "2020-12-31": {
        "filed": "2021-01-28",
        "accession": "0001564590-21-002744",
        "sha256": "1715940d1613a3fb7ed1c0e5b614054f2a57dc0e35c83416033ac397dad75607",
        "expected_revenue": (
            146_889_000.0,
            56_806_000.0,
            406_583_000.0,
            84_219_000.0,
        ),
        "expected_net_income": (
            -29_511_000.0,
            -3_496_000.0,
            -108_906_000.0,
            -59_243_000.0,
        ),
    },
}

EXPECTED_QUARTERS = {
    "2019-03-31": (120_177_000.0, -40_393_000.0),
    "2019-06-30": (141_737_000.0, -42_592_000.0),
    "2019-09-30": (117_957_000.0, -36_803_000.0),
    "2019-12-31": (146_889_000.0, -29_511_000.0),
    "2020-03-31": (104_946_000.0, -338_611_000.0),
    "2020-06-30": (6_361_000.0, -34_570_000.0),
    "2020-09-30": (21_052_000.0, -21_177_000.0),
    "2020-12-31": (56_806_000.0, -3_496_000.0),
}
SIGNAL_SCENARIOS = {
    "2021-01-29": ("liq2000000-age150-growth",),
    "2021-02-26": (
        "liq10000000-age150-growth",
        "liq2000000-age150-growth",
    ),
}


def _url(spec: dict) -> str:
    accession = spec["accession"].replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{CIK}/"
        f"{accession}/mmyt-ex991_6.htm"
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
    raise RuntimeError(f"failed to fetch MMYT source {_url(spec)}") from error


def _normalize(value: object) -> str:
    return " ".join(str(value).replace("\xa0", " ").split()).strip()


def _row_values(cells: list[str]) -> tuple[float, ...]:
    values = []
    for cell in cells[1:]:
        text = _normalize(cell)
        if not re.fullmatch(r"\(?\d{1,3}(?:,\d{3})+\)?", text):
            continue
        amount = float(text.replace("(", "").replace(")", "").replace(",", ""))
        values.append(-amount * 1000.0 if text.startswith("(") else amount * 1000.0)
    return tuple(values)


def parse_statement(
    raw: bytes, expected_length: int
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    soup = BeautifulSoup(raw, "lxml")
    text = _normalize(soup.get_text(" "))
    if "MakeMyTrip Limited" not in text:
        raise RuntimeError("MMYT issuer identity is absent")
    if "IFRS" not in text:
        raise RuntimeError("MMYT IFRS accounting marker is absent")
    if "thousands" not in text.casefold():
        raise RuntimeError("MMYT source scale is absent")
    if "$" not in text and "USD thousands" not in text:
        raise RuntimeError("MMYT USD currency marker is absent")

    candidates = set()
    for table in soup.find_all("table"):
        table_text = _normalize(table.get_text(" ")).casefold()
        if "non-ifrs" in table_text or "adjusted revenue" in table_text:
            continue
        revenue_rows, net_income_rows = [], []
        for row in table.find_all("tr"):
            cells = [
                _normalize(cell.get_text(" "))
                for cell in row.find_all(["td", "th"])
            ]
            if not cells:
                continue
            label = cells[0].casefold()
            if label == "total revenue":
                revenue_rows.append(_row_values(cells))
            if label == "loss for the period":
                net_income_rows.append(_row_values(cells))
        for revenue in revenue_rows:
            for net_income in net_income_rows:
                if len(revenue) == expected_length and len(net_income) == expected_length:
                    candidates.add((revenue, net_income))
    if len(candidates) != 1:
        raise RuntimeError(f"MMYT IFRS USD statement values are ambiguous: {candidates}")
    return candidates.pop()


def validate_snapshots(
    snapshots: dict[str, tuple[tuple[float, ...], tuple[float, ...]]]
) -> dict[str, tuple[float, float]]:
    for fiscal_end, spec in SOURCES.items():
        expected = (spec["expected_revenue"], spec["expected_net_income"])
        if snapshots.get(fiscal_end) != expected:
            raise RuntimeError(
                f"MMYT {fiscal_end} source values changed: {snapshots.get(fiscal_end)}"
            )
    quarters = {
        fiscal_end: (revenue[1], net_income[1])
        for fiscal_end, (revenue, net_income) in snapshots.items()
    }
    if quarters != EXPECTED_QUARTERS:
        raise RuntimeError(f"MMYT recovered quarters changed: {quarters}")

    if (
        sum(quarters[end][0] for end in ("2019-06-30", "2019-09-30")),
        sum(quarters[end][1] for end in ("2019-06-30", "2019-09-30")),
    ) != (259_694_000.0, -79_395_000.0):
        raise RuntimeError("MMYT fiscal 2020 H1/quarter identity failed")
    if (
        sum(
            quarters[end][0]
            for end in ("2019-06-30", "2019-09-30", "2019-12-31")
        ),
        sum(
            quarters[end][1]
            for end in ("2019-06-30", "2019-09-30", "2019-12-31")
        ),
    ) != (406_583_000.0, -108_906_000.0):
        raise RuntimeError("MMYT fiscal 2020 9M/quarter identity failed")
    if (
        sum(
            quarters[end][0]
            for end in ("2019-06-30", "2019-09-30", "2019-12-31", "2020-03-31")
        ),
        sum(
            quarters[end][1]
            for end in ("2019-06-30", "2019-09-30", "2019-12-31", "2020-03-31")
        ),
    ) != (511_529_000.0, -447_517_000.0):
        raise RuntimeError("MMYT fiscal 2020 FY/quarter identity failed")
    if (
        sum(quarters[end][0] for end in ("2020-06-30", "2020-09-30")),
        sum(quarters[end][1] for end in ("2020-06-30", "2020-09-30")),
    ) != (27_413_000.0, -55_747_000.0):
        raise RuntimeError("MMYT fiscal 2021 H1/quarter identity failed")
    if (
        sum(
            quarters[end][0]
            for end in ("2020-06-30", "2020-09-30", "2020-12-31")
        ),
        sum(
            quarters[end][1]
            for end in ("2020-06-30", "2020-09-30", "2020-12-31")
        ),
    ) != (84_219_000.0, -59_243_000.0):
        raise RuntimeError("MMYT fiscal 2021 9M/quarter identity failed")

    cross_checks = {
        "2020-03-31": "2019-03-31",
        "2020-06-30": "2019-06-30",
        "2020-09-30": "2019-09-30",
        "2020-12-31": "2019-12-31",
    }
    for later_source, earlier_quarter in cross_checks.items():
        revenue, net_income = snapshots[later_source]
        if (revenue[0], net_income[0]) != quarters[earlier_quarter]:
            raise RuntimeError(
                f"MMYT pre-signal comparative changed {earlier_quarter}"
            )
    return quarters


def audit_signals(quarters: dict[str, tuple[float, float]]) -> list[dict]:
    expected_window = sorted(quarters)
    audits = []
    for signal_date, scenarios in SIGNAL_SCENARIOS.items():
        eligible = sorted(
            fiscal_end
            for fiscal_end, spec in SOURCES.items()
            if spec["filed"] <= signal_date
        )[-8:]
        if eligible != expected_window:
            raise RuntimeError(f"MMYT {signal_date} exact PIT window failed: {eligible}")
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
        audits.append(
            {
                "signal_date": signal_date,
                "affected_scenarios": list(scenarios),
                "missing_observation_count": len(scenarios),
                "quarter_window": eligible,
                "last_available_financial_filing": {
                    "fiscal_end": "2020-12-31",
                    "filed": SOURCES["2020-12-31"]["filed"],
                    "accession": SOURCES["2020-12-31"]["accession"],
                    "url": _url(SOURCES["2020-12-31"]),
                },
                "previous_ttm": previous_ttm,
                "current_ttm": current_ttm,
                "growth": growth,
                "deterministic_result": "EXCLUDE_EXACT_NEGATIVE_NET_INCOME_TTM",
            }
        )
    if sum(row["missing_observation_count"] for row in audits) != 3:
        raise RuntimeError("MMYT aggregate missing-observation count changed")
    return audits


def recover(output_dir: Path = OUTPUT_DIR) -> dict:
    snapshots, source_manifest = {}, []
    for fiscal_end, spec in SOURCES.items():
        raw = _fetch(spec)
        digest = hashlib.sha256(raw).hexdigest()
        if digest != spec["sha256"]:
            raise RuntimeError(f"MMYT source changed for {fiscal_end}: {digest}")
        snapshots[fiscal_end] = parse_statement(raw, len(spec["expected_revenue"]))
        source_manifest.append(
            {
                "role": "original_quarter",
                "fiscal_end": fiscal_end,
                "form": "6-K:EX-99.1",
                "filed": spec["filed"],
                "accession": spec["accession"],
                "url": _url(spec),
                "sha256": digest,
                "bytes": len(raw),
            }
        )

    quarters = validate_snapshots(snapshots)
    signal_audit = audit_signals(quarters)
    fetched_at = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    rows = []
    for fiscal_end, (revenue, net_income) in quarters.items():
        spec = SOURCES[fiscal_end]
        for metric, value, concept in (
            ("revenue", revenue, "ifrs-full:Revenue"),
            ("net_income", net_income, "ifrs-full:ProfitLoss"),
        ):
            rows.append(
                {
                    "ticker": "MMYT",
                    "fiscal_end": fiscal_end,
                    "available_date": spec["filed"],
                    "metric": metric,
                    "value": value,
                    "taxonomy": "ifrs-full",
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
        "ticker": "MMYT",
        "cik": CIK,
        "currency": "USD",
        "source_scale": "thousands",
        "fiscal_year_end": "March 31",
        "accepted_quarter_count": len(quarters),
        "accepted_fact_count": len(facts),
        "aggregate_missing_observation_count": 3,
        "recovery_classification": (
            "STRICTLY_RECOVERABLE_EXACT_NEGATIVE_NET_INCOME_TTM_EXCLUSION"
        ),
        "sources": source_manifest,
        "signal_audit": signal_audit,
        "accounting_identities": {
            "fiscal_2020_h1": {
                "revenue": 259_694_000.0,
                "net_income": -79_395_000.0,
            },
            "fiscal_2020_9m": {
                "revenue": 406_583_000.0,
                "net_income": -108_906_000.0,
            },
            "fiscal_2020_fy": {
                "revenue": 511_529_000.0,
                "net_income": -447_517_000.0,
            },
            "fiscal_2021_h1": {
                "revenue": 27_413_000.0,
                "net_income": -55_747_000.0,
            },
            "fiscal_2021_9m": {
                "revenue": 84_219_000.0,
                "net_income": -59_243_000.0,
            },
        },
        "accounting_scope": {
            "accepted": "IFRS total revenue and consolidated loss for the period",
            "ifrs_15_window": (
                "all accepted quarters begin after the April 1, 2018 cumulative-effect adoption"
            ),
            "excluded": [
                "adjusted revenue or adjusted margin",
                "constant-currency growth",
                "adjusted operating profit/loss",
                "loss attributable to owners",
                "earnings per share",
            ],
        },
        "revision_isolation": {
            "original_current_quarter_releases_used": True,
            "post_signal_filings_used": False,
            "later_comparatives_used_as_emitted_facts": False,
            "pre_signal_comparatives_used_only_for_identity_checks": True,
            "comparative_changes_detected": False,
        },
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path),
                "sha256": hashlib.sha256(facts_path.read_bytes()).hexdigest(),
            }
        },
        "guardrail": (
            "Only hash-locked original SEC issuer 6-K earnings exhibits under CIK "
            "1495153 are accepted. Emitted values are explicit current-quarter "
            "USD-thousands IFRS facts. Cumulative periods, non-IFRS/adjusted or "
            "constant-currency measures, pre-IFRS-15 comparatives, later "
            "restatements, and post-signal filings are excluded."
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
                "recovery_classification": report["recovery_classification"],
                "release_status": report["release_status"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
