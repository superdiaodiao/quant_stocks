#!/usr/bin/env python3
"""Recover LI's source-locked CNY quarters for its two missing PIT signals."""

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


CIK = 1_791_706
OUTPUT_DIR = Path("output/research_only/v14/li_quarterly_pit")
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
    "historical_quarters": {
        "filed": "2020-12-04",
        "form": "424B4",
        "accession": "0001047469-20-005648",
        "document": "a2242705z424b4.htm",
        "sha256": "2a9aa844bb86c9a7360902d68b20155d3369fbf82b675abea72bd393fd059a87",
        "expected_length": 7,
        "net_label": "net loss",
        "expected_revenue": (
            0.0,
            0.0,
            0.0,
            284_367_000.0,
            851_675_000.0,
            1_947_238_000.0,
            2_510_799_000.0,
        ),
        "expected_net_income": (
            -358_361_000.0,
            -670_479_000.0,
            -683_616_000.0,
            -726_080_000.0,
            -77_113_000.0,
            -75_162_000.0,
            -106_929_000.0,
        ),
    },
    "2020q4": {
        "filed": "2021-02-25",
        "form": "6-K:EX-99.1",
        "accession": "0001104659-21-027868",
        "document": "a21-7956_1ex99d1.htm",
        "sha256": "4d7f0ae06a28adb9312517e468bdf1d28671d1b47986e7c1d8bf01a4aea94333",
        "expected_length": 6,
        "net_label": "net (loss)/income",
        "expected_revenue": (
            2_510_799_000.0,
            4_146_897_000.0,
            635_539_000.0,
            284_367_000.0,
            9_456_609_000.0,
            1_449_288_000.0,
        ),
        "expected_net_income": (
            -106_929_000.0,
            107_547_000.0,
            16_482_000.0,
            -2_438_536_000.0,
            -151_657_000.0,
            -23_243_000.0,
        ),
    },
    "2021q1": {
        "filed": "2021-05-26",
        "form": "6-K:EX-99.1",
        "accession": "0001104659-21-072101",
        "document": "tm2117612d1_ex99-1.htm",
        "sha256": "7d9e4da1815c640df93c0c4bbb758c82f67f4182687b4ec888de135cd0a2b5c5",
        "expected_length": 4,
        "net_label": "net (loss)/income",
        "expected_revenue": (
            851_675_000.0,
            4_146_897_000.0,
            3_575_201_000.0,
            545_682_000.0,
        ),
        "expected_net_income": (
            -77_113_000.0,
            107_547_000.0,
            -359_967_000.0,
            -54_943_000.0,
        ),
    },
    "2021q2": {
        "filed": "2021-08-30",
        "form": "6-K:EX-99.1",
        "accession": "0001104659-21-110714",
        "document": "tm2126523d1_ex99-1.htm",
        "sha256": "ff3bce0c2124b9a4e34f79e6c65ea3186dabd263a0a0e382e01e5ae949a27072",
        "expected_length": 4,
        "net_label": "net loss",
        "expected_revenue": (
            1_947_238_000.0,
            3_575_201_000.0,
            5_038_952_000.0,
            780_435_000.0,
        ),
        "expected_net_income": (
            -75_162_000.0,
            -359_967_000.0,
            -235_489_000.0,
            -36_472_000.0,
        ),
    },
    "2021q3": {
        "filed": "2021-11-29",
        "form": "6-K:EX-99.1",
        "accession": "0001104659-21-144060",
        "document": "tm2134048d1_ex99-1.htm",
        "sha256": "40805d26011229c64f57594a72eb8ffbc6dad6a16c0eff8f83acc865e0e41caa",
        "expected_length": 4,
        "net_label": "net loss",
        "expected_revenue": (
            2_510_799_000.0,
            5_038_952_000.0,
            7_775_174_000.0,
            1_206_688_000.0,
        ),
        "expected_net_income": (
            -106_929_000.0,
            -235_489_000.0,
            -21_510_000.0,
            -3_337_000.0,
        ),
    },
}

EXPECTED_QUARTERS = {
    "2019-12-31": (284_367_000.0, -726_080_000.0),
    "2020-03-31": (851_675_000.0, -77_113_000.0),
    "2020-06-30": (1_947_238_000.0, -75_162_000.0),
    "2020-09-30": (2_510_799_000.0, -106_929_000.0),
    "2020-12-31": (4_146_897_000.0, 107_547_000.0),
    "2021-03-31": (3_575_201_000.0, -359_967_000.0),
    "2021-06-30": (5_038_952_000.0, -235_489_000.0),
    "2021-09-30": (7_775_174_000.0, -21_510_000.0),
}
QUARTER_SOURCE = {
    "2019-12-31": ("historical_quarters", 3),
    "2020-03-31": ("historical_quarters", 4),
    "2020-06-30": ("historical_quarters", 5),
    "2020-09-30": ("historical_quarters", 6),
    "2020-12-31": ("2020q4", 1),
    "2021-03-31": ("2021q1", 2),
    "2021-06-30": ("2021q2", 2),
    "2021-09-30": ("2021q3", 2),
}
SIGNAL_SCENARIOS = {
    "2021-11-30": (
        "liq10000000-age150-growth",
        "liq2000000-age150-growth",
    ),
    "2021-12-31": (
        "liq10000000-age150-growth",
        "liq2000000-age150-growth",
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
    raise RuntimeError(f"failed to fetch LI source {_url(spec)}") from error


def _normalize(value: object) -> str:
    return " ".join(str(value).replace("\xa0", " ").split()).strip()


def _row_values(cells: list[str]) -> tuple[float, ...]:
    values = []
    for cell in cells[1:]:
        text = _normalize(cell)
        if text in {"—", "–", "-"}:
            values.append(0.0)
            continue
        if not re.fullmatch(r"\(?\d{1,3}(?:,\d{3})+\)?", text):
            continue
        amount = float(text.replace("(", "").replace(")", "").replace(",", ""))
        values.append(-amount * 1000.0 if text.startswith("(") else amount * 1000.0)
    return tuple(values)


def parse_statement(
    raw: bytes, expected_length: int, net_label: str
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    soup = BeautifulSoup(raw, "lxml")
    text = _normalize(soup.get_text(" "))
    if "Li Auto Inc." not in text:
        raise RuntimeError("LI issuer identity is absent")
    if "in thousands" not in text.casefold():
        raise RuntimeError("LI source scale is absent")

    candidates = set()
    for table in soup.find_all("table"):
        table_text = _normalize(table.get_text(" "))
        if "pro forma" in table_text.casefold():
            continue
        revenue_rows, net_rows = [], []
        for row in table.find_all("tr"):
            cells = [
                _normalize(cell.get_text(" "))
                for cell in row.find_all(["td", "th"])
            ]
            if not cells:
                continue
            label = cells[0].casefold()
            if label == "total revenues":
                revenue_rows.append(_row_values(cells))
            if label == net_label.casefold():
                net_rows.append(_row_values(cells))
        for revenue in revenue_rows:
            for net_income in net_rows:
                if len(revenue) == expected_length and len(net_income) == expected_length:
                    candidates.add((revenue, net_income))
    if len(candidates) != 1:
        raise RuntimeError(f"LI CNY statement values are ambiguous: {candidates}")
    return candidates.pop()


def validate_snapshots(
    snapshots: dict[str, tuple[tuple[float, ...], tuple[float, ...]]]
) -> dict[str, tuple[float, float]]:
    for key, spec in SOURCES.items():
        expected = (spec["expected_revenue"], spec["expected_net_income"])
        if snapshots.get(key) != expected:
            raise RuntimeError(f"LI {key} source values changed: {snapshots.get(key)}")

    quarters = {}
    for fiscal_end, (source_key, index) in QUARTER_SOURCE.items():
        revenue, net_income = snapshots[source_key]
        quarters[fiscal_end] = (revenue[index], net_income[index])
    if quarters != EXPECTED_QUARTERS:
        raise RuntimeError(f"LI recovered quarters changed: {quarters}")

    historical_revenue, historical_net = snapshots["historical_quarters"]
    if (sum(historical_revenue[:4]), sum(historical_net[:4])) != (
        284_367_000.0,
        -2_438_536_000.0,
    ):
        raise RuntimeError("LI 2019 FY/quarter identity failed")
    if (sum(historical_revenue[4:]), sum(historical_net[4:])) != (
        5_309_712_000.0,
        -259_204_000.0,
    ):
        raise RuntimeError("LI 2020 9M/quarter identity failed")
    if (
        sum(quarters[end][0] for end in quarters if end.startswith("2020-")),
        sum(quarters[end][1] for end in quarters if end.startswith("2020-")),
    ) != (9_456_609_000.0, -151_657_000.0):
        raise RuntimeError("LI 2020 FY/quarter identity failed")

    cross_checks = {
        "2020q4": {0: "2020-09-30"},
        "2021q1": {0: "2020-03-31", 1: "2020-12-31"},
        "2021q2": {0: "2020-06-30", 1: "2021-03-31"},
        "2021q3": {0: "2020-09-30", 1: "2021-06-30"},
    }
    for source_key, positions in cross_checks.items():
        revenue, net_income = snapshots[source_key]
        for index, fiscal_end in positions.items():
            if (revenue[index], net_income[index]) != quarters[fiscal_end]:
                raise RuntimeError(
                    f"LI pre-signal comparative changed {fiscal_end} in {source_key}"
                )
    return quarters


def audit_signals(quarters: dict[str, tuple[float, float]]) -> list[dict]:
    audits = []
    expected_window = sorted(quarters)
    for signal_date, scenarios in SIGNAL_SCENARIOS.items():
        eligible = sorted(
            fiscal_end
            for fiscal_end, (source_key, _) in QUARTER_SOURCE.items()
            if SOURCES[source_key]["filed"] <= signal_date
        )[-8:]
        if eligible != expected_window:
            raise RuntimeError(f"LI {signal_date} exact PIT window failed: {eligible}")
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
                    "fiscal_end": "2021-09-30",
                    "filed": SOURCES["2021q3"]["filed"],
                    "accession": SOURCES["2021q3"]["accession"],
                    "url": _url(SOURCES["2021q3"]),
                },
                "previous_ttm": previous_ttm,
                "current_ttm": current_ttm,
                "growth": growth,
                "deterministic_result": "EXCLUDE_EXACT_NEGATIVE_NET_INCOME_TTM",
            }
        )
    if sum(row["missing_observation_count"] for row in audits) != 4:
        raise RuntimeError("LI aggregate missing-observation count changed")
    return audits


def recover(output_dir: Path = OUTPUT_DIR) -> dict:
    snapshots, source_manifest = {}, []
    for key, spec in SOURCES.items():
        raw = _fetch(spec)
        digest = hashlib.sha256(raw).hexdigest()
        if digest != spec["sha256"]:
            raise RuntimeError(f"LI source changed for {key}: {digest}")
        snapshots[key] = parse_statement(
            raw, spec["expected_length"], spec["net_label"]
        )
        source_manifest.append(
            {
                "role": key,
                "form": spec["form"],
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
        source_key, _ = QUARTER_SOURCE[fiscal_end]
        spec = SOURCES[source_key]
        output_form = (
            "424B4:HISTORICAL_QUARTER_TABLE"
            if source_key == "historical_quarters"
            else f"{spec['form']}:CURRENT_QUARTER"
        )
        for metric, value, concept in (
            ("revenue", revenue, "sec_issuer:TotalRevenues"),
            ("net_income", net_income, "NetIncomeLoss"),
        ):
            rows.append(
                {
                    "ticker": "LI",
                    "fiscal_end": fiscal_end,
                    "available_date": spec["filed"],
                    "metric": metric,
                    "value": value,
                    "taxonomy": "us-gaap",
                    "concept": concept,
                    "form": output_form,
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
        "ticker": "LI",
        "cik": CIK,
        "currency": "CNY",
        "source_scale": "thousands",
        "accepted_quarter_count": len(quarters),
        "accepted_fact_count": len(facts),
        "aggregate_missing_observation_count": 4,
        "recovery_classification": (
            "STRICTLY_RECOVERABLE_EXACT_NEGATIVE_NET_INCOME_TTM_EXCLUSION"
        ),
        "sources": source_manifest,
        "signal_audit": signal_audit,
        "accounting_identities": {
            "2019_fy": {"revenue": 284_367_000.0, "net_income": -2_438_536_000.0},
            "2020_9m": {"revenue": 5_309_712_000.0, "net_income": -259_204_000.0},
            "2020_fy": {"revenue": 9_456_609_000.0, "net_income": -151_657_000.0},
        },
        "profit_ownership": {
            "accepted": (
                "consolidated US-GAAP Net (loss)/income, including discontinued "
                "operations where the issuer reported them"
            ),
            "concept": "NetIncomeLoss",
            "excluded": [
                "net loss attributable to ordinary shareholders",
                "non-GAAP net income/loss",
                "earnings per ADS/share",
            ],
        },
        "revision_isolation": {
            "historical_source": (
                "2020-12-04 follow-on 424B4 explicit actual quarterly table"
            ),
            "pro_forma_used": False,
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
            "Only hash-locked SEC issuer filings under historical CIK 1791706 are "
            "accepted. Emitted values are explicit current-quarter CNY-thousands "
            "consolidated US-GAAP facts. USD convenience translations, cumulative "
            "periods, pro-forma data, attributable-shareholder loss, non-GAAP "
            "metrics, post-signal filings, and later restatements are excluded."
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
