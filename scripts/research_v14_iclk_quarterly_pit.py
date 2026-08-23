#!/usr/bin/env python3
"""Recover ICLK's source-locked USD quarters for its two missing PIT signals."""

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


CIK = 1_697_818
OUTPUT_DIR = Path("output/research_only/v14/iclk_quarterly_pit")
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
    "2018-12-31": {
        "filed": "2019-03-27",
        "accession": "0001104659-19-017556",
        "document": "a19-7157_1ex99d1.htm",
        "exhibit": "EX-99.1",
        "sha256": "b6a3c2fa4a2b855d9c32e8a92e20b91249a8da2f2500aa96519bdbb6b6c211c5",
        "current_index": 1,
        "revenue_label": "net revenues 4",
        "profit_label": (
            "net loss attributable to iclick interactive asia group limited's "
            "ordinary shareholders"
        ),
        "expected_revenue": (
            39_761_000.0,
            39_504_000.0,
            125_258_000.0,
            160_017_000.0,
        ),
        "expected_net_income": (
            -12_163_000.0,
            -7_937_000.0,
            -29_931_000.0,
            -32_409_000.0,
        ),
    },
    "2019-03-31": {
        "filed": "2019-05-30",
        "accession": "0001104659-19-032554",
        "document": "a19-10689_1ex99d2.htm",
        "exhibit": "EX-99.2",
        "sha256": "adf583d44f80c530719a186ef53a216bda1a79d07396c0a555d63d4510b754cf",
        "current_index": 0,
        "revenue_label": "revenue",
        "profit_label": (
            "net loss attributable to iclick interactive asia group limited's "
            "ordinary shareholders"
        ),
        "expected_revenue": (39_218_000.0, 35_229_000.0),
        "expected_net_income": (-2_135_000.0, -112_000.0),
    },
    "2019-06-30": {
        "filed": "2019-08-28",
        "accession": "0001104659-19-047635",
        "document": "a19-17796_1ex99d1.htm",
        "exhibit": "EX-99.1",
        "sha256": "3c2283acfabaf7292e638acdfa4b03f06f89629134f07ad71e3c275e261be12b",
        "current_index": 0,
        "revenue_label": "revenue",
        "profit_label": (
            "net loss attributable to iclick interactive asia group limited's "
            "ordinary shareholders"
        ),
        "expected_revenue": (
            49_347_000.0,
            42_697_000.0,
            88_565_000.0,
            77_926_000.0,
        ),
        "expected_net_income": (
            -3_141_000.0,
            -2_606_000.0,
            -5_276_000.0,
            -2_718_000.0,
        ),
    },
    "2019-09-30": {
        "filed": "2019-11-27",
        "accession": "0001104659-19-068197",
        "document": "a19-23917_1ex99d1.htm",
        "exhibit": "EX-99.1",
        "sha256": "4d738f0a04d068a7c07aeba20cc77f443dd3ad69520cc929fb335a410451e4f1",
        "current_index": 0,
        "revenue_label": "revenue",
        "profit_label": (
            "net income/ (loss) attributable to iclick interactive asia group "
            "limited's ordinary shareholders"
        ),
        "expected_revenue": (
            54_168_000.0,
            42_587_000.0,
            142_733_000.0,
            120_513_000.0,
        ),
        "expected_net_income": (
            1_413_000.0,
            -21_754_000.0,
            -3_863_000.0,
            -24_472_000.0,
        ),
    },
    "2019-12-31": {
        "filed": "2020-04-01",
        "accession": "0001564590-20-014728",
        "document": "iclk-ex991_102.htm",
        "exhibit": "EX-99.1",
        "sha256": "60463d68aad303b4a552f755d3d2531e9224fada2fa35184bb55ed1839ec90e0",
        "current_index": 0,
        "revenue_label": "revenue",
        "profit_label": (
            "net loss attributable to iclick interactive asia group limited's "
            "ordinary shareholders"
        ),
        "expected_revenue": (
            56_675_000.0,
            39_504_000.0,
            199_408_000.0,
            160_017_000.0,
        ),
        "expected_net_income": (
            -5_740_000.0,
            -7_937_000.0,
            -9_603_000.0,
            -32_409_000.0,
        ),
    },
    "2020-03-31": {
        "filed": "2020-05-22",
        "accession": "0001564590-20-026733",
        "document": "iclk-ex991_6.htm",
        "exhibit": "EX-99.1",
        "sha256": "71e920aed4b20fb34afc92f183445902561997ed1ba25b4590a127ef432087fb",
        "current_index": 0,
        "revenue_label": "revenue",
        "profit_label": (
            "net loss attributable to iclick interactive asia group limited's "
            "ordinary shareholders"
        ),
        "expected_revenue": (49_035_000.0, 39_218_000.0),
        "expected_net_income": (-7_734_000.0, -2_135_000.0),
    },
    "2020-06-30": {
        "filed": "2020-08-24",
        "accession": "0001564590-20-041045",
        "document": "iclk-ex991_6.htm",
        "exhibit": "EX-99.1",
        "sha256": "b524dfbf93ef6aa9aa80c622434b702de8cb30bb51721f414b8e5bb0657a6bd4",
        "current_index": 0,
        "revenue_label": "revenue",
        "profit_label": (
            "net income/(loss) attributable to iclick interactive asia group "
            "limited's ordinary shareholders"
        ),
        "expected_revenue": (
            58_113_000.0,
            49_347_000.0,
            107_148_000.0,
            88_565_000.0,
        ),
        "expected_net_income": (
            382_000.0,
            -3_141_000.0,
            -7_352_000.0,
            -5_276_000.0,
        ),
    },
    "2020-09-30": {
        "filed": "2020-11-24",
        "accession": "0001564590-20-055057",
        "document": "iclk-ex991_6.htm",
        "exhibit": "EX-99.1",
        "sha256": "542661d1d874daef9889527cfc1088ecad43ae1a67e6a434382f47f67a4d5766",
        "current_index": 0,
        "revenue_label": "revenue",
        "profit_label": (
            "net (loss)/income attributable to iclick interactive asia group "
            "limited's ordinary shareholders"
        ),
        "expected_revenue": (
            68_905_000.0,
            54_168_000.0,
            176_053_000.0,
            142_733_000.0,
        ),
        "expected_net_income": (
            -6_552_000.0,
            1_413_000.0,
            -13_904_000.0,
            -3_863_000.0,
        ),
    },
}

EXPECTED_QUARTERS = {
    "2018-12-31": (39_504_000.0, -7_937_000.0),
    "2019-03-31": (39_218_000.0, -2_135_000.0),
    "2019-06-30": (49_347_000.0, -3_141_000.0),
    "2019-09-30": (54_168_000.0, 1_413_000.0),
    "2019-12-31": (56_675_000.0, -5_740_000.0),
    "2020-03-31": (49_035_000.0, -7_734_000.0),
    "2020-06-30": (58_113_000.0, 382_000.0),
    "2020-09-30": (68_905_000.0, -6_552_000.0),
}
SIGNAL_SCENARIOS = {
    "2020-12-31": ("liq2000000-age150-growth",),
    "2021-01-29": ("liq2000000-age150-growth",),
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
    raise RuntimeError(f"failed to fetch ICLK source {_url(spec)}") from error


def _normalize(value: object) -> str:
    return " ".join(str(value).replace("\xa0", " ").split()).strip()


def _label(value: object) -> str:
    return _normalize(value).casefold().replace("’", "'")


def _row_values(cells: list[str]) -> tuple[float, ...]:
    values = []
    for cell in cells[1:]:
        text = _normalize(cell)
        if not re.fullmatch(r"\(?\d+(?:,\d{3})*\)?", text):
            continue
        amount = float(text.replace("(", "").replace(")", "").replace(",", ""))
        values.append(-amount * 1000.0 if text.startswith("(") else amount * 1000.0)
    return tuple(values)


def parse_statement(
    raw: bytes, expected_length: int, revenue_label: str, profit_label: str
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    soup = BeautifulSoup(raw, "lxml")
    text = _normalize(soup.get_text(" "))
    if "iClick Interactive Asia Group Limited" not in text:
        raise RuntimeError("ICLK historical issuer identity is absent")
    if "US$ in thousands" not in text:
        raise RuntimeError("ICLK USD-thousands currency marker is absent")
    if "GAAP" not in text:
        raise RuntimeError("ICLK GAAP accounting marker is absent")

    candidates = set()
    for table in soup.find_all("table"):
        revenue_rows, profit_rows = [], []
        for row in table.find_all("tr"):
            cells = [
                _normalize(cell.get_text(" "))
                for cell in row.find_all(["td", "th"])
            ]
            if not cells:
                continue
            label = _label(cells[0])
            if label == revenue_label:
                revenue_rows.append(_row_values(cells))
            if label == profit_label:
                profit_rows.append(_row_values(cells))
        for revenue in revenue_rows:
            for net_income in profit_rows:
                if len(revenue) == expected_length and len(net_income) == expected_length:
                    candidates.add((revenue, net_income))
    if len(candidates) != 1:
        raise RuntimeError(f"ICLK GAAP USD statement values are ambiguous: {candidates}")
    return candidates.pop()


def validate_snapshots(
    snapshots: dict[str, tuple[tuple[float, ...], tuple[float, ...]]]
) -> dict[str, tuple[float, float]]:
    for fiscal_end, spec in SOURCES.items():
        expected = (spec["expected_revenue"], spec["expected_net_income"])
        if snapshots.get(fiscal_end) != expected:
            raise RuntimeError(
                f"ICLK {fiscal_end} source values changed: {snapshots.get(fiscal_end)}"
            )
    quarters = {}
    for fiscal_end, (revenue, net_income) in snapshots.items():
        index = SOURCES[fiscal_end]["current_index"]
        quarters[fiscal_end] = (revenue[index], net_income[index])
    if quarters != EXPECTED_QUARTERS:
        raise RuntimeError(f"ICLK recovered quarters changed: {quarters}")

    identities = {
        "2019_h1": (
            ("2019-03-31", "2019-06-30"),
            (88_565_000.0, -5_276_000.0),
        ),
        "2019_9m": (
            ("2019-03-31", "2019-06-30", "2019-09-30"),
            (142_733_000.0, -3_863_000.0),
        ),
        "2019_fy": (
            ("2019-03-31", "2019-06-30", "2019-09-30", "2019-12-31"),
            (199_408_000.0, -9_603_000.0),
        ),
        "2020_h1": (
            ("2020-03-31", "2020-06-30"),
            (107_148_000.0, -7_352_000.0),
        ),
        "2020_9m": (
            ("2020-03-31", "2020-06-30", "2020-09-30"),
            (176_053_000.0, -13_904_000.0),
        ),
    }
    for name, (window, expected) in identities.items():
        actual = (
            sum(quarters[end][0] for end in window),
            sum(quarters[end][1] for end in window),
        )
        if actual != expected:
            raise RuntimeError(f"ICLK {name}/quarter identity failed: {actual}")

    for later_source, earlier_quarter in {
        "2019-12-31": "2018-12-31",
        "2020-03-31": "2019-03-31",
        "2020-06-30": "2019-06-30",
        "2020-09-30": "2019-09-30",
    }.items():
        revenue, net_income = snapshots[later_source]
        if (revenue[1], net_income[1]) != quarters[earlier_quarter]:
            raise RuntimeError(
                f"ICLK pre-signal comparative changed {earlier_quarter}"
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
            raise RuntimeError(f"ICLK {signal_date} exact PIT window failed: {eligible}")
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
                    "fiscal_end": "2020-09-30",
                    "filed": SOURCES["2020-09-30"]["filed"],
                    "accession": SOURCES["2020-09-30"]["accession"],
                    "url": _url(SOURCES["2020-09-30"]),
                },
                "previous_ttm": previous_ttm,
                "current_ttm": current_ttm,
                "growth": growth,
                "deterministic_result": "EXCLUDE_EXACT_NEGATIVE_NET_INCOME_TTM",
            }
        )
    if sum(row["missing_observation_count"] for row in audits) != 2:
        raise RuntimeError("ICLK aggregate missing-observation count changed")
    return audits


def recover(output_dir: Path = OUTPUT_DIR) -> dict:
    snapshots, source_manifest = {}, []
    for fiscal_end, spec in SOURCES.items():
        raw = _fetch(spec)
        digest = hashlib.sha256(raw).hexdigest()
        if digest != spec["sha256"]:
            raise RuntimeError(f"ICLK source changed for {fiscal_end}: {digest}")
        snapshots[fiscal_end] = parse_statement(
            raw,
            len(spec["expected_revenue"]),
            spec["revenue_label"],
            spec["profit_label"],
        )
        source_manifest.append(
            {
                "role": "original_quarter",
                "fiscal_end": fiscal_end,
                "form": f"6-K:{spec['exhibit']}",
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
            ("revenue", revenue, "RevenueFromContractWithCustomer"),
            (
                "net_income",
                net_income,
                "sec_issuer:NetIncomeLossAttributableToOrdinaryShareholders",
            ),
        ):
            rows.append(
                {
                    "ticker": "ICLK",
                    "fiscal_end": fiscal_end,
                    "available_date": spec["filed"],
                    "metric": metric,
                    "value": value,
                    "taxonomy": "us-gaap",
                    "concept": concept,
                    "form": f"6-K:{spec['exhibit']}:CURRENT_QUARTER",
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
        "ticker": "ICLK",
        "cik": CIK,
        "historical_issuer_name": "iClick Interactive Asia Group Limited",
        "currency": "USD",
        "source_scale": "thousands",
        "accepted_quarter_count": len(quarters),
        "accepted_fact_count": len(facts),
        "aggregate_missing_observation_count": 2,
        "recovery_classification": (
            "STRICTLY_RECOVERABLE_EXACT_NEGATIVE_NET_INCOME_TTM_EXCLUSION"
        ),
        "sources": source_manifest,
        "signal_audit": signal_audit,
        "accounting_identities": {
            "2019_h1": {"revenue": 88_565_000.0, "net_income": -5_276_000.0},
            "2019_9m": {"revenue": 142_733_000.0, "net_income": -3_863_000.0},
            "2019_fy": {"revenue": 199_408_000.0, "net_income": -9_603_000.0},
            "2020_h1": {"revenue": 107_148_000.0, "net_income": -7_352_000.0},
            "2020_9m": {"revenue": 176_053_000.0, "net_income": -13_904_000.0},
        },
        "profit_ownership": {
            "accepted": (
                "US-GAAP net income/loss attributable to iClick Interactive "
                "Asia Group Limited's ordinary shareholders"
            ),
            "matches_candidate_annuals": {
                "2018": -32_409_000.0,
                "2019": -9_603_000.0,
            },
            "excluded": [
                "consolidated net income/loss before noncontrolling interests",
                "net income/loss attributable to noncontrolling interests",
                "comprehensive income/loss",
                "adjusted net income/loss",
                "earnings per ADS/share",
            ],
        },
        "revision_isolation": {
            "original_current_quarter_releases_used": True,
            "post_signal_filings_used": False,
            "later_comparatives_used_as_emitted_facts": False,
            "pre_signal_comparatives_used_only_for_identity_checks": True,
            "comparative_changes_detected": False,
            "post_signal_amber_international_name_change_used": False,
        },
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path),
                "sha256": hashlib.sha256(facts_path.read_bytes()).hexdigest(),
            }
        },
        "guardrail": (
            "Only hash-locked original SEC issuer 6-K earnings exhibits filed "
            "under historical CIK 1697818 are accepted. Emitted values are explicit "
            "current-quarter US$-thousands GAAP facts. Consolidated pre-NCI loss, "
            "NCI, cumulative periods, non-GAAP metrics, later restatements, the "
            "post-signal Amber identity, and post-signal filings are excluded."
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
