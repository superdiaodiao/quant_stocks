#!/usr/bin/env python3
"""Recover only CSIQ 2016Q4 for the 2019-02-28 research-only PIT gap."""

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

CIK = 1_375_877
SIGNAL_DATE = "2019-02-28"
OUTPUT_DIR = Path("output/research_only/v14/csiq_2016q4_pit")
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
    "q4_release": {
        "role": "direct_quarter_and_preliminary_fy_identity",
        "filed": "2017-03-21",
        "accession": "0001104659-17-018201",
        "document": "a17-8942_1ex99d1.htm",
        "form": "6-K:EX-99.1",
        "sha256": "c3ae8f1f6fea13c6851f5a8b214fe35943e00f5fe2fe2ba44c0afe3e7371f061",
    },
    "q3_release": {
        "role": "original_nine_month_identity",
        "filed": "2016-11-21",
        "accession": "0001104659-16-158231",
        "document": "a16-21952_1ex99d1.htm",
        "form": "6-K:EX-99.1",
        "sha256": "2477b8b2dfd5a716fe1e69067244dcfd9844d496bfd2cf49a3afe29ba6763d56",
    },
    "annual_20f": {
        "role": "original_audited_fy_identity",
        "filed": "2017-04-27",
        "accession": "0001047469-17-002970",
        "document": "a2230964z20-f.htm",
        "form": "20-F",
        "sha256": "3f8b5c8f57a6c6e9c04a2615a9615e8072db16ba6bdb73e38f8359ade495c8e3",
    },
}

EXPECTED_RELEASE = {
    "q4": (668_428_000.0, -13_776_000.0),
    "fy": (2_853_078_000.0, 65_275_000.0),
    "q4_noncontrolling": -448_000.0,
    "q4_parent": -13_328_000.0,
    "fy_noncontrolling": 26_000.0,
    "fy_parent": 65_249_000.0,
}
EXPECTED_NINE_MONTHS = (2_184_650_000.0, 79_051_000.0)
EXPECTED_AUDITED = {
    "fy": (2_853_078_000.0, 65_275_000.0),
    "fy_parent": 65_249_000.0,
}

# These seven rows already exist in the bound candidate. They are pinned here only
# to prove that the two-row supplement closes the exact eight-quarter PIT window.
EXISTING_QUARTERS = {
    "2017-03-31": (677_042_000.0, -13_743_000.0, "2017-06-06"),
    "2017-06-30": (692_366_000.0, 40_354_000.0, "2017-08-14"),
    "2017-09-30": (912_223_000.0, 13_592_000.0, "2017-11-09"),
    "2017-12-31": (1_108_764_000.0, 62_780_000.0, "2018-03-19"),
    "2018-03-31": (1_424_911_000.0, 43_886_000.0, "2018-05-16"),
    "2018-06-30": (650_590_000.0, 15_973_000.0, "2018-08-14"),
    "2018-09-30": (767_970_000.0, 68_472_000.0, "2018-11-15"),
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
    raise RuntimeError(f"failed to fetch CSIQ source {_url(spec)}") from error


def _normalize(value: object) -> str:
    return " ".join(str(value).replace("\xa0", " ").split()).strip()


def _amount(value: object) -> float | None:
    text = _normalize(value)
    if not text or text.casefold() == "nan" or "%" in text:
        return None
    cleaned = re.sub(r"[^0-9.]", "", text)
    if not cleaned:
        return None
    amount = float(cleaned) * 1000.0
    return -amount if "(" in text or text.startswith("-") else amount


def _ordered_amounts(row: pd.Series) -> list[float]:
    amounts = []
    for value in row.iloc[1:]:
        amount = _amount(value)
        if amount is None or (amounts and amount == amounts[-1]):
            continue
        amounts.append(amount)
    return amounts


def _statement_rows(table: pd.DataFrame) -> dict[str, list[float]]:
    wanted = {
        "Net revenues",
        "Net income",
        "Net income (loss)",
        "Less: Net income (loss) attributable to non-controlling interests",
        "Net income attributable to Canadian Solar Inc.",
        "Net income (loss) attributable to Canadian Solar Inc.",
    }
    result = {}
    for _, row in table.iterrows():
        label = _normalize(row.iloc[0])
        if label in wanted:
            result[label] = _ordered_amounts(row)
    return result


def parse_q4_release(raw: bytes) -> dict[str, object]:
    candidates = []
    for table in pd.read_html(BytesIO(raw)):
        flattened = _normalize(" ".join(map(str, table.to_numpy().ravel())))
        if "Three Months Ended" not in flattened or "Twelve Months Ended" not in flattened:
            continue
        rows = _statement_rows(table)
        total = rows.get("Net income (loss)")
        noncontrolling = rows.get(
            "Less: Net income (loss) attributable to non-controlling interests"
        )
        parent = rows.get("Net income (loss) attributable to Canadian Solar Inc.")
        revenue = rows.get("Net revenues")
        if not all((revenue, total, noncontrolling, parent)):
            continue
        if min(map(len, (revenue, total, noncontrolling, parent))) < 5:
            continue
        candidates.append(
            {
                "q4": (revenue[0], total[0]),
                "fy": (revenue[3], total[3]),
                "q4_noncontrolling": noncontrolling[0],
                "q4_parent": parent[0],
                "fy_noncontrolling": noncontrolling[3],
                "fy_parent": parent[3],
            }
        )
    if not candidates or any(row != candidates[0] for row in candidates[1:]):
        raise RuntimeError(
            f"CSIQ Q4 release has ambiguous statement tables: {candidates}"
        )
    return candidates[0]


def parse_nine_months(raw: bytes) -> tuple[float, float]:
    candidates = set()
    for table in pd.read_html(BytesIO(raw)):
        flattened = _normalize(" ".join(map(str, table.to_numpy().ravel())))
        if "Three Months Ended" not in flattened or "Nine Months Ended" not in flattened:
            continue
        rows = _statement_rows(table)
        revenue = rows.get("Net revenues")
        total = rows.get("Net income") or rows.get("Net income (loss)")
        if revenue and total and min(len(revenue), len(total)) >= 5:
            candidates.add((revenue[3], total[3]))
    if len(candidates) != 1:
        raise RuntimeError(f"CSIQ Q3 release has ambiguous nine-month totals: {candidates}")
    return candidates.pop()


def parse_annual(raw: bytes) -> dict[str, object]:
    candidates = set()
    for table in pd.read_html(BytesIO(raw)):
        rows = _statement_rows(table)
        revenue = rows.get("Net revenues")
        total = rows.get("Net income (loss)") or rows.get("Net income")
        parent = rows.get("Net income (loss) attributable to Canadian Solar Inc.")
        if revenue and total and parent:
            candidates.add((revenue[-1], total[-1], parent[-1]))
    expected = (
        EXPECTED_AUDITED["fy"][0],
        EXPECTED_AUDITED["fy"][1],
        EXPECTED_AUDITED["fy_parent"],
    )
    if expected not in candidates:
        raise RuntimeError(f"CSIQ original 20-F lacks expected audited FY row: {candidates}")
    return {"fy": expected[:2], "fy_parent": expected[2]}


def validate_quarter(
    release: dict[str, object],
    nine_months: tuple[float, float],
    audited: dict[str, object],
) -> tuple[float, float]:
    if release != EXPECTED_RELEASE:
        raise RuntimeError(f"CSIQ original Q4 release values changed: {release}")
    if nine_months != EXPECTED_NINE_MONTHS:
        raise RuntimeError(f"CSIQ original 9M values changed: {nine_months}")
    if audited != EXPECTED_AUDITED:
        raise RuntimeError(f"CSIQ original audited FY values changed: {audited}")

    q4 = release["q4"]
    fy = release["fy"]
    if tuple(nine_months[i] + q4[i] for i in range(2)) != fy:
        raise RuntimeError("CSIQ original 6-K FY/9M/Q4 identity failed")
    if audited["fy"] != fy:
        raise RuntimeError("CSIQ original 20-F disagrees with the original Q4 6-K")
    if q4[1] - release["q4_noncontrolling"] != release["q4_parent"]:
        raise RuntimeError("CSIQ Q4 total/NCI/parent ownership identity failed")
    if fy[1] - release["fy_noncontrolling"] != release["fy_parent"]:
        raise RuntimeError("CSIQ FY total/NCI/parent ownership identity failed")
    if audited["fy_parent"] != release["fy_parent"]:
        raise RuntimeError("CSIQ original 20-F parent-income identity failed")
    return q4


def audit_signal(q4: tuple[float, float]) -> dict:
    quarters = {"2016-12-31": (*q4, SOURCES["q4_release"]["filed"])}
    quarters.update(EXISTING_QUARTERS)
    eligible = sorted(
        fiscal_end
        for fiscal_end, (_, _, available_date) in quarters.items()
        if available_date <= SIGNAL_DATE
    )
    expected_window = [
        "2016-12-31",
        "2017-03-31",
        "2017-06-30",
        "2017-09-30",
        "2017-12-31",
        "2018-03-31",
        "2018-06-30",
        "2018-09-30",
    ]
    if eligible != expected_window:
        raise RuntimeError(f"CSIQ signal does not have the exact PIT window: {eligible}")
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
        raise RuntimeError(f"CSIQ recovered growth is not positive: {growth}")
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
            raise RuntimeError(f"CSIQ source changed for {name}: {digest}")
        raw_sources[name] = raw
        source_manifest.append(
            {
                "name": name,
                "role": spec["role"],
                "form": spec["form"],
                "filed": spec["filed"],
                "accession": spec["accession"],
                "url": _url(spec),
                "sha256": digest,
                "bytes": len(raw),
            }
        )

    release = parse_q4_release(raw_sources["q4_release"])
    nine_months = parse_nine_months(raw_sources["q3_release"])
    audited = parse_annual(raw_sources["annual_20f"])
    q4 = validate_quarter(release, nine_months, audited)
    signal_audit = audit_signal(q4)

    fetched_at = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    rows = []
    for metric, value, concept in (
        ("revenue", q4[0], "Revenues"),
        ("net_income", q4[1], "ProfitLoss"),
    ):
        rows.append(
            {
                "ticker": "CSIQ",
                "fiscal_end": "2016-12-31",
                "available_date": SOURCES["q4_release"]["filed"],
                "metric": metric,
                "value": value,
                "taxonomy": "us-gaap",
                "concept": concept,
                "form": "6-K:EX-99.1:CURRENT_QUARTER",
                "accession": SOURCES["q4_release"]["accession"],
                "fetched_at": fetched_at,
            }
        )
    facts = pd.DataFrame(rows, columns=OUTPUT_COLUMNS).sort_values("metric")

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
        "ticker": "CSIQ",
        "cik": CIK,
        "signal_date": SIGNAL_DATE,
        "currency": "USD",
        "source_scale": "thousands",
        "accepted_quarter_count": 1,
        "accepted_fact_count": len(facts),
        "sources": source_manifest,
        "signal_audit": signal_audit,
        "ownership": {
            "accepted_concept": "ProfitLoss",
            "accepted_total_net_income": q4[1],
            "noncontrolling_interest": release["q4_noncontrolling"],
            "excluded_parent_attributable_net_income": release["q4_parent"],
            "identity": "total net income minus NCI equals parent-attributable net income",
        },
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path),
                "sha256": hashlib.sha256(facts_path.read_bytes()).hexdigest(),
            }
        },
        "guardrail": (
            "Only the original pre-signal 2016Q3 and 2016Q4 issuer releases and "
            "the original 2016 20-F are accepted, all hash-locked. Direct USD-"
            "thousands three-month values are used. The supplement emits only "
            "2016Q4 and preserves the candidate's consolidated ProfitLoss metric; "
            "the parent-attributable amount is explicitly excluded. No later "
            "20-F/A, post-signal filing, cumulative period, or pro forma value is used."
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
                "accepted_fact_count": report["accepted_fact_count"],
                "manifest": report["manifest"],
                "release_status": report["release_status"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
