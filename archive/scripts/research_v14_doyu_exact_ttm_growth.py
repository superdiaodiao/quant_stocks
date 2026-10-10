#!/usr/bin/env python3
"""Build DOYU's source-locked exact-TTM growth research supplement."""

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


TICKER = "DOYU"
CIK = 1_762_417
OUTPUT_DIR = Path("output/research_only/v14/doyu_exact_ttm_growth")
AUDIT_PATH = Path(
    "output/research_only/v14/"
    "batch_afya_legn_sdgr_companyfacts_allt_glng_allk_asnd_csiq_cron_iq_"
    "jamf_lx_iiiv_peri_uxin_gain_azpn_li_gilt_eslt_meso_mmyt_opra_mogo_"
    "price_overlay_audit.json"
)
AUDIT_SHA256 = "cb85678c6bf6b648b5fec47d33a65546b2f30168825a4c27fb322d6c6a8bab13"
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

SCENARIOS = tuple(
    f"liq{liquidity}-age{age}-growth"
    for liquidity in (2_000_000, 10_000_000)
    for age in (150, 365, 550)
)
SIGNALS = ("2020-07-31", "2020-08-31", "2020-10-30")


def _scaled(values: tuple[int, ...], scale: int = 1_000) -> tuple[float, ...]:
    return tuple(float(value * scale) for value in values)


SOURCES = {
    "2019q2": {
        "role": "quarterly_release",
        "form": "6-K:EX-99.1",
        "filed": "2019-08-13",
        "accession": "0001104659-19-045427",
        "document": "a19-17009_1ex99d1.htm",
        "sha256": "d9dc411702c8baca3345373892ded90fa91ba31f9b61a651cf19c30deed8384c",
        "scale": 1_000,
        "expected_revenue": _scaled(
            (802_909, 1_489_124, 1_872_729, 272_794, 1_469_391, 3_361_853, 489_709)
        ),
        "expected_net_income": _scaled(
            (-228_702, 18_151, 23_156, 3_373, -384_367, 41_307, 6_017)
        ),
    },
    "2019q3": {
        "role": "quarterly_release_full_submission",
        "form": "6-K:EX-99.1:FULL-SUBMISSION",
        "filed": "2019-11-27",
        "accession": "0001193125-19-301612",
        "document": "0001193125-19-301612.txt",
        "sha256": "be7e2319ca2bb57531f504e5a8740bd7967abec414655e286703cd9cdbb3e87b",
        "scale": 1_000,
        "expected_revenue": _scaled(
            (1_024_820, 1_872_729, 1_858_476, 260_956, 2_494_211, 5_220_328, 733_007)
        ),
        "expected_net_income": _scaled(
            (-220_482, 23_156, -165_400, -23_223, -604_849, -124_093, -17_423)
        ),
    },
    "2019q4": {
        "role": "quarterly_and_annual_release",
        "form": "6-K:EX-99.1",
        "filed": "2020-03-19",
        "accession": "0001193125-20-078073",
        "document": "d885678dex991.htm",
        "sha256": "fa12bfbe3c78a3c68d4f9850abd7b437bb38f044ff00b405d806c52e1e38e200",
        "scale": 1_000,
        "expected_revenue": _scaled(
            (1_160_172, 1_858_476, 2_062_902, 294_894, 3_654_383, 7_283_230, 1_041_146)
        ),
        "expected_net_income": _scaled(
            (-271_431, -165_400, 157_441, 22_507, -876_280, 33_348, 4_767)
        ),
    },
    "2019_20f": {
        "role": "audited_annual_ownership_and_identity_check",
        "form": "20-F",
        "filed": "2020-04-28",
        "accession": "0001193125-20-122129",
        "document": "d120656d20f.htm",
        "sha256": "82127804f4172b900953f4905ffa2737709f28def1318dfc182ad137933116de",
        "scale": 1,
        "expected_revenue": (
            1_885_717_001.0,
            3_654_383_126.0,
            7_283_230_253.0,
            1_041_145_646.0,
        ),
        "expected_net_income": (
            -612_897_944.0,
            -876_279_828.0,
            33_348_128.0,
            4_767_151.0,
        ),
        "expected_attributable": (
            -612_897_944.0,
            -882_941_495.0,
            39_753_232.0,
            5_682_768.0,
        ),
    },
    "2020q1": {
        "role": "quarterly_release",
        "form": "6-K:EX-99.1",
        "filed": "2020-05-26",
        "accession": "0001193125-20-150742",
        "document": "d931819dex991.htm",
        "sha256": "84a1189d1816a727ee17e1297b101e1f307cef7ee68aff89721e06b920c93d3d",
        "scale": 1_000,
        "expected_revenue": _scaled((1_489_124, 2_062_902, 2_278_035, 321_112)),
        "expected_net_income": _scaled((18_151, 157_441, 254_526, 35_878)),
    },
    "2020q2": {
        "role": "quarterly_release",
        "form": "6-K:EX-99.1",
        "filed": "2020-08-10",
        "accession": "0001193125-20-214048",
        "document": "d91986dex991.htm",
        "sha256": "2f8daba52ac077c71d72e7c4d3dba3547535c8d0a82fc09305adc6ed63adfca5",
        "scale": 1_000,
        "expected_revenue": _scaled(
            (1_872_729, 2_278_035, 2_508_152, 354_419, 3_361_853, 4_786_187, 676_321)
        ),
        "expected_net_income": _scaled(
            (23_156, 254_526, 319_270, 45_114, 41_308, 573_796, 81_081)
        ),
    },
}

EXPECTED_QUARTERS = {
    "2018-06-30": (802_909_000.0, -228_702_000.0),
    "2018-09-30": (1_024_820_000.0, -220_482_000.0),
    "2018-12-31": (1_160_172_000.0, -271_431_000.0),
    "2019-03-31": (1_489_124_000.0, 18_151_000.0),
    "2019-06-30": (1_872_729_000.0, 23_156_000.0),
    "2019-09-30": (1_858_476_000.0, -165_400_000.0),
    "2019-12-31": (2_062_902_000.0, 157_441_000.0),
    "2020-03-31": (2_278_035_000.0, 254_526_000.0),
    "2020-06-30": (2_508_152_000.0, 319_270_000.0),
}
QUARTER_SOURCE = {
    "2018-06-30": ("2019q2", 0, "COMPARATIVE_QUARTER"),
    "2018-09-30": ("2019q3", 0, "COMPARATIVE_QUARTER"),
    "2018-12-31": ("2019q4", 0, "COMPARATIVE_QUARTER"),
    "2019-03-31": ("2019q2", 1, "PRECEDING_QUARTER"),
    "2019-06-30": ("2019q2", 2, "CURRENT_QUARTER"),
    "2019-09-30": ("2019q3", 2, "CURRENT_QUARTER"),
    "2019-12-31": ("2019q4", 2, "CURRENT_QUARTER"),
    "2020-03-31": ("2020q1", 2, "CURRENT_QUARTER"),
    "2020-06-30": ("2020q2", 2, "CURRENT_QUARTER"),
}
EXPECTED_SIGNAL_WINDOWS = {
    "2020-07-31": tuple(EXPECTED_QUARTERS)[:8],
    "2020-08-31": tuple(EXPECTED_QUARTERS)[1:],
    "2020-10-30": tuple(EXPECTED_QUARTERS)[1:],
}
EXPECTED_TTM = {
    "2020-07-31": {
        "previous": {"revenue": 4_477_025_000.0, "net_income": -702_464_000.0},
        "current": {"revenue": 8_072_142_000.0, "net_income": 269_723_000.0},
        "growth": {
            "revenue": 0.8030147251802257,
            "net_income": 1.3839670075619535,
        },
    },
    "2020-08-31": {
        "previous": {"revenue": 5_546_845_000.0, "net_income": -450_606_000.0},
        "current": {"revenue": 8_707_565_000.0, "net_income": 565_837_000.0},
        "growth": {
            "revenue": 0.5698230255217155,
            "net_income": 2.255724513211098,
        },
    },
    "2020-10-30": {
        "previous": {"revenue": 5_546_845_000.0, "net_income": -450_606_000.0},
        "current": {"revenue": 8_707_565_000.0, "net_income": 565_837_000.0},
        "growth": {
            "revenue": 0.5698230255217155,
            "net_income": 2.255724513211098,
        },
    },
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
    raise RuntimeError(f"failed to fetch DOYU source {_url(spec)}") from error


def _normalize(value: object) -> str:
    return " ".join(str(value).replace("\xa0", " ").split()).strip()


def _label(value: object) -> str:
    return _normalize(value).casefold().replace("’", "'")


def _row_values(cells: list[str], scale: int) -> tuple[float, ...]:
    values = []
    for cell in cells[1:]:
        raw = _normalize(cell)
        negative = raw.lstrip().startswith("(") or raw.startswith("-")
        cleaned = (
            raw.replace("(", "")
            .replace(")", "")
            .replace(",", "")
            .replace("$", "")
            .strip()
        )
        if not re.fullmatch(r"\d+(?:\.\d+)?", cleaned):
            continue
        amount = float(cleaned) * scale
        values.append(-amount if negative else amount)
    return tuple(values)


def _statement_rows(raw: bytes, expected_length: int, scale: int) -> tuple:
    soup = BeautifulSoup(raw, "lxml")
    text = _normalize(soup.get_text(" "))
    if "DouYu International Holdings Limited" not in text:
        raise RuntimeError("DOYU issuer identity marker is absent")
    if "RMB" not in text or "US$" not in text:
        raise RuntimeError("DOYU RMB/US$ column markers are absent")
    if "GAAP" not in text:
        raise RuntimeError("DOYU US-GAAP marker is absent")

    candidates = set()
    for table in soup.find_all("table"):
        revenues, profits = [], []
        for row in table.find_all("tr"):
            cells = [
                _normalize(cell.get_text(" "))
                for cell in row.find_all(["td", "th"])
            ]
            if not cells:
                continue
            label = _label(cells[0])
            if label.startswith("net revenues"):
                revenues.append(_row_values(cells, scale))
            if label in {"net income (loss)", "net income", "net loss"}:
                profits.append(_row_values(cells, scale))
        for revenue in revenues:
            for net_income in profits:
                if len(revenue) == expected_length and len(net_income) == expected_length:
                    candidates.add((revenue, net_income))
    if len(candidates) != 1:
        raise RuntimeError(f"DOYU consolidated statement is ambiguous: {candidates}")
    return candidates.pop()


def _ownership_row(raw: bytes, expected: tuple[float, ...]) -> tuple[float, ...]:
    soup = BeautifulSoup(raw, "lxml")
    candidates = set()
    for row in soup.find_all("tr"):
        cells = [
            _normalize(cell.get_text(" "))
            for cell in row.find_all(["td", "th"])
        ]
        if not cells:
            continue
        if _label(cells[0]).startswith(
            "net income (loss) attributable to ordinary shareholders"
        ):
            values = _row_values(cells, 1)
            if len(values) == len(expected):
                candidates.add(values)
    if expected not in candidates:
        raise RuntimeError(f"DOYU exact ownership row is absent: {candidates}")
    return expected


def validate_audit_binding(path: Path = AUDIT_PATH) -> dict:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != AUDIT_SHA256:
        raise RuntimeError(f"DOYU audit binding changed: {digest}")
    audit = json.loads(path.read_text(encoding="utf-8"))
    for scenario in SCENARIOS:
        coverage = audit["scenarios"][scenario]["coverage"]
        matches = [
            row
            for row in coverage["missing_financial_priorities"]
            if row["ticker"] == TICKER
        ]
        if len(matches) != 1:
            raise RuntimeError(f"DOYU audit row missing for {scenario}")
        row = matches[0]
        expected = {
            "missing_signal_count": 3,
            "first_missing_signal_date": SIGNALS[0],
            "last_missing_signal_date": SIGNALS[-1],
            "no_raw_pit_financial_facts_signal_count": 3,
        }
        if any(row[key] != value for key, value in expected.items()):
            raise RuntimeError(f"DOYU audit row changed for {scenario}: {row}")
    return {
        "path": str(path),
        "sha256": digest,
        "scenario_count": len(SCENARIOS),
        "exact_signals": list(SIGNALS),
        "aggregate_missing_observation_count": len(SCENARIOS) * len(SIGNALS),
        "technical_replay_control": {
            "method": (
                "Targeted replay of the bound price panel, universe snapshots, "
                "market regime, and both liquidity configurations; financial-age "
                "variants share the same non-financial candidate mask."
            ),
            "price_directory": audit["input_bindings"]["price_directory"],
            "snapshot_dir": audit["input_bindings"]["snapshot_dir"],
            "candidate_by_signal": {
                "2020-07-31": True,
                "2020-08-31": True,
                "2020-09-30": False,
                "2020-10-30": True,
            },
        },
    }


def _within_rounding(actual: float, expected: float) -> bool:
    return abs(actual - expected) <= 1_000.0


def validate_snapshots(
    snapshots: dict[str, tuple[tuple[float, ...], tuple[float, ...]]],
    annual: tuple[tuple[float, ...], tuple[float, ...]],
    attributable: tuple[float, ...],
) -> dict[str, tuple[float, float]]:
    for source_id, spec in SOURCES.items():
        if source_id == "2019_20f":
            continue
        expected = (spec["expected_revenue"], spec["expected_net_income"])
        if snapshots.get(source_id) != expected:
            raise RuntimeError(f"DOYU source values changed for {source_id}")
    annual_spec = SOURCES["2019_20f"]
    if annual != (
        annual_spec["expected_revenue"],
        annual_spec["expected_net_income"],
    ):
        raise RuntimeError("DOYU audited annual values changed")
    if attributable != annual_spec["expected_attributable"]:
        raise RuntimeError("DOYU audited ownership row changed")
    if annual[1][-1] != 4_767_151.0 or attributable[-1] != 5_682_768.0:
        raise RuntimeError("DOYU candidate annual ownership semantics changed")

    quarters = {}
    for fiscal_end, (source_id, index, _role) in QUARTER_SOURCE.items():
        revenue, net_income = snapshots[source_id]
        quarters[fiscal_end] = (revenue[index], net_income[index])
    if quarters != EXPECTED_QUARTERS:
        raise RuntimeError(f"DOYU recovered quarters changed: {quarters}")

    comparisons = {
        ("2019q3", 1): "2019-06-30",
        ("2019q4", 1): "2019-09-30",
        ("2020q1", 0): "2019-03-31",
        ("2020q1", 1): "2019-12-31",
        ("2020q2", 0): "2019-06-30",
        ("2020q2", 1): "2020-03-31",
    }
    for (source_id, index), fiscal_end in comparisons.items():
        revenue, net_income = snapshots[source_id]
        if (revenue[index], net_income[index]) != quarters[fiscal_end]:
            raise RuntimeError(f"DOYU comparative changed for {fiscal_end}")

    identities = {
        "2019_h1": (
            ("2019-03-31", "2019-06-30"),
            (snapshots["2019q2"][0][5], snapshots["2019q2"][1][5]),
        ),
        "2019_9m": (
            ("2019-03-31", "2019-06-30", "2019-09-30"),
            (snapshots["2019q3"][0][5], snapshots["2019q3"][1][5]),
        ),
        "2019_fy": (
            ("2019-03-31", "2019-06-30", "2019-09-30", "2019-12-31"),
            (snapshots["2019q4"][0][5], snapshots["2019q4"][1][5]),
        ),
        "2020_h1": (
            ("2020-03-31", "2020-06-30"),
            (snapshots["2020q2"][0][5], snapshots["2020q2"][1][5]),
        ),
    }
    for name, (window, expected) in identities.items():
        actual = (
            sum(quarters[end][0] for end in window),
            sum(quarters[end][1] for end in window),
        )
        if not all(_within_rounding(left, right) for left, right in zip(actual, expected)):
            raise RuntimeError(f"DOYU {name} identity failed: {actual} vs {expected}")

    reported_fy = snapshots["2019q4"][0][5], snapshots["2019q4"][1][5]
    audited_fy = annual[0][2], annual[1][2]
    if not all(_within_rounding(left, right) for left, right in zip(reported_fy, audited_fy)):
        raise RuntimeError("DOYU 2019 6-K/20-F annual identity failed")
    return quarters


def audit_signals(quarters: dict[str, tuple[float, float]]) -> list[dict]:
    audits = []
    for signal_date in SIGNALS:
        eligible = sorted(
            fiscal_end
            for fiscal_end, (source_id, _index, _role) in QUARTER_SOURCE.items()
            if SOURCES[source_id]["filed"] <= signal_date
        )[-8:]
        if tuple(eligible) != EXPECTED_SIGNAL_WINDOWS[signal_date]:
            raise RuntimeError(f"DOYU {signal_date} PIT window changed: {eligible}")
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
        expected = EXPECTED_TTM[signal_date]
        if previous_ttm != expected["previous"] or current_ttm != expected["current"]:
            raise RuntimeError(f"DOYU {signal_date} TTM totals changed")
        if any(abs(growth[key] - expected["growth"][key]) > 1e-15 for key in growth):
            raise RuntimeError(f"DOYU {signal_date} growth changed")
        if not (
            current_ttm["net_income"] > 0
            and growth["net_income"] >= 0.25
            and growth["revenue"] >= 0.10
        ):
            raise RuntimeError(f"DOYU {signal_date} exact growth gate failed")
        latest = eligible[-1]
        source_id = QUARTER_SOURCE[latest][0]
        financial_age_days = int(
            (
                pd.Timestamp(signal_date)
                - pd.Timestamp(SOURCES[source_id]["filed"])
            ).days
        )
        if financial_age_days > 150:
            raise RuntimeError(f"DOYU {signal_date} age-150 gate failed")
        audits.append(
            {
                "signal_date": signal_date,
                "affected_scenarios": list(SCENARIOS),
                "missing_observation_count": len(SCENARIOS),
                "quarter_window": eligible,
                "financial_age_days": financial_age_days,
                "last_available_financial_filing": {
                    "fiscal_end": latest,
                    "filed": SOURCES[source_id]["filed"],
                    "accession": SOURCES[source_id]["accession"],
                    "url": _url(SOURCES[source_id]),
                },
                "previous_ttm": previous_ttm,
                "current_ttm": current_ttm,
                "growth": growth,
                "deterministic_result": (
                    "PASS_EXACT_TTM_GROWTH_AND_POSITIVE_NET_INCOME"
                ),
            }
        )
    if sum(row["missing_observation_count"] for row in audits) != 18:
        raise RuntimeError("DOYU aggregate missing-observation count changed")
    return audits


def resolved_observations(signal_audit: list[dict]) -> pd.DataFrame:
    rows = []
    for audit in signal_audit:
        for scenario in audit["affected_scenarios"]:
            rows.append(
                {
                    "scenario": scenario,
                    "ticker": TICKER,
                    "signal_date": audit["signal_date"],
                    "last_available_fiscal_end": audit[
                        "last_available_financial_filing"
                    ]["fiscal_end"],
                    "available_date": audit["last_available_financial_filing"][
                        "filed"
                    ],
                    "revenue_previous_ttm": audit["previous_ttm"]["revenue"],
                    "revenue_current_ttm": audit["current_ttm"]["revenue"],
                    "revenue_growth": audit["growth"]["revenue"],
                    "net_income_previous_ttm": audit["previous_ttm"]["net_income"],
                    "net_income_current_ttm": audit["current_ttm"]["net_income"],
                    "net_income_growth": audit["growth"]["net_income"],
                    "resolution": audit["deterministic_result"],
                }
            )
    frame = pd.DataFrame(rows).sort_values(["signal_date", "scenario"])
    if len(frame) != 18:
        raise RuntimeError(f"DOYU resolved observation count changed: {len(frame)}")
    return frame


def recover(output_dir: Path = OUTPUT_DIR, audit_path: Path = AUDIT_PATH) -> dict:
    audit_binding = validate_audit_binding(audit_path)
    snapshots, source_manifest = {}, []
    annual = attributable = None
    for source_id, spec in SOURCES.items():
        raw = _fetch(spec)
        digest = hashlib.sha256(raw).hexdigest()
        if digest != spec["sha256"]:
            raise RuntimeError(f"DOYU source changed for {source_id}: {digest}")
        statement = _statement_rows(
            raw, len(spec["expected_revenue"]), spec["scale"]
        )
        if source_id == "2019_20f":
            annual = statement
            attributable = _ownership_row(raw, spec["expected_attributable"])
        else:
            snapshots[source_id] = statement
        source_manifest.append(
            {
                "source_id": source_id,
                "role": spec["role"],
                "form": spec["form"],
                "filed": spec["filed"],
                "accession": spec["accession"],
                "url": _url(spec),
                "sha256": digest,
                "bytes": len(raw),
            }
        )
    if annual is None or attributable is None:
        raise RuntimeError("DOYU annual ownership audit was not loaded")

    quarters = validate_snapshots(snapshots, annual, attributable)
    signal_audit = audit_signals(quarters)
    observations = resolved_observations(signal_audit)
    fetched_at = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    rows = []
    for fiscal_end, (revenue, net_income) in quarters.items():
        source_id, _index, role = QUARTER_SOURCE[fiscal_end]
        spec = SOURCES[source_id]
        for metric, value, concept in (
            ("revenue", revenue, "RevenueFromContractWithCustomerExcludingAssessedTax"),
            ("net_income", net_income, "ProfitLoss"),
        ):
            rows.append(
                {
                    "ticker": TICKER,
                    "fiscal_end": fiscal_end,
                    "available_date": spec["filed"],
                    "metric": metric,
                    "value": value,
                    "taxonomy": "us-gaap",
                    "concept": concept,
                    "form": f"{spec['form']}:{role}",
                    "accession": spec["accession"],
                    "fetched_at": fetched_at,
                }
            )
    facts = pd.DataFrame(rows, columns=OUTPUT_COLUMNS).sort_values(
        ["fiscal_end", "metric"]
    )
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    observations_path = output_dir / "resolved_observations.csv"
    facts.to_csv(facts_path, index=False)
    observations.to_csv(observations_path, index=False)

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
        "ticker": TICKER,
        "cik": CIK,
        "historical_issuer_name": "DouYu International Holdings Limited",
        "currency": "CNY",
        "source_scale": "RMB thousands for 6-K facts; exact units for 20-F check",
        "convenience_usd_translation_emitted": False,
        "accepted_quarter_count": len(quarters),
        "accepted_fact_count": len(facts),
        "aggregate_missing_observation_count": len(observations),
        "recovery_classification": "STRICTLY_RECOVERABLE_EXACT_TTM_GROWTH",
        "audit_binding": audit_binding,
        "sources": source_manifest,
        "signal_audit": signal_audit,
        "profit_semantics": {
            "accepted": "US-GAAP consolidated net income (loss), before NCI attribution",
            "candidate_2019_annual_usd_value": 4_767_151.0,
            "audited_2019_consolidated_cny_value": 33_348_128.0,
            "excluded_attributable_2019_cny_value": 39_753_232.0,
            "excluded": [
                "net income/loss attributable to DouYu or ordinary shareholders",
                "net income/loss attributable to non-controlling interests",
                "adjusted net income/loss",
                "earnings per share or ADS",
            ],
        },
        "revision_and_rounding_isolation": {
            "original_quarterly_releases_used": True,
            "post_signal_filings_used": False,
            "later_comparatives_used_as_emitted_facts": False,
            "quarterly_comparatives_changed": False,
            "rounding_tolerance_cny": 1_000.0,
            "known_rounding_only_differences": [
                "2019 quarter-sum revenue exceeds reported FY revenue by CNY1,000",
                "2020Q2 release reports comparative H1 2019 net income CNY1,000 above the original 2019Q2 H1 release; no individual quarter changed",
                "6-K FY2019 rounded totals differ from exact 20-F totals by CNY253 revenue and CNY128 net income",
            ],
        },
        "excluded_post_signal_source": {
            "reason": "filed after the latest audited signal; never used",
            "form": "6-K:EX-99.1",
            "filed": "2020-11-12",
            "accession": "0001193125-20-290971",
            "url": (
                "https://www.sec.gov/Archives/edgar/data/1762417/"
                "000119312520290971/d934230dex991.htm"
            ),
        },
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path),
                "sha256": hashlib.sha256(facts_path.read_bytes()).hexdigest(),
            },
            "resolved_observations": {
                "path": str(observations_path),
                "sha256": hashlib.sha256(observations_path.read_bytes()).hexdigest(),
            },
        },
        "guardrail": (
            "Only hash-locked official SEC filings available by each signal are "
            "accepted. Emitted facts are RMB US-GAAP consolidated current or "
            "explicit comparative quarters. USD convenience translations, FX "
            "conversion, adjusted results, attribution rows, estimates, cumulative "
            "periods as substitutes, and post-signal filings are excluded."
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
    parser.add_argument("--audit-path", type=Path, default=AUDIT_PATH)
    args = parser.parse_args()
    report = recover(args.output_dir, args.audit_path)
    print(
        json.dumps(
            {
                "accepted_quarter_count": report["accepted_quarter_count"],
                "resolved_observation_count": report[
                    "aggregate_missing_observation_count"
                ],
                "recovery_classification": report["recovery_classification"],
                "release_status": report["release_status"],
                "manifest": report["manifest"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
