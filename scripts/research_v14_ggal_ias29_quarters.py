#!/usr/bin/env python3
"""Recover GGAL's 2021 PIT gaps on one IAS 29 Argentine-peso basis."""

from __future__ import annotations

import argparse
from decimal import Decimal, getcontext
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.request import Request, urlopen

from lxml import etree
import pandas as pd

from src.io.fundamentals_update import OUTPUT_COLUMNS


getcontext().prec = 28

TICKER = "GGAL"
CIK = 1_114_700
RECOVERABLE_SIGNALS = ("2021-08-31", "2021-10-29")
BLOCKED_SIGNALS = {
    "2019-06-28": {
        "recoverable": False,
        "reason": (
            "The 2019Q1 6-K explicitly says IAS 29 restatement was not used, "
            "while the pre-signal 2018 20-F annual figures are stated at "
            "December 2018 purchasing power. No eight-quarter actual chain "
            "exists on one ARS measurement basis."
        ),
    },
    "2019-07-31": {
        "recoverable": False,
        "reason": (
            "The same nominal-versus-IAS-29 basis conflict remains, and the "
            "next quarterly financial-results 6-K was not filed until "
            "2019-08-14."
        ),
    },
}
AVAILABLE_DATE = "2021-08-31"
ACCEPTED_AT_UTC = "2021-08-31T14:39:09.000Z"
TARGET_CPI = Decimal("483.6049")
ANNUAL_CPI = Decimal("385.8826")
SOURCE_DIR = Path("output/data_provenance/ggal_ias29_quarters")
OUTPUT_DIR = Path("output/research_only/v14/ggal_ias29_quarters_2019q3_2021q2")
AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260827_sy_glpg_rlmd_smpl_classified_financial_priorities.csv"
)
EXPECTED_AUDIT_SHA256 = (
    "616ebd6a836bb1f0571ad690fbcd1b0bf56ae06b092041ac406eb976b6243e0e"
)
SEC_HEADERS = {"User-Agent": "quant-stocks-research contact@example.com"}


def _spec(
    accession: str,
    filed: str,
    document: str,
    sha256: str,
    *,
    accepted_at_utc: str | None = None,
    form: str | None = None,
) -> dict:
    return {
        "accession": accession,
        "filed": filed,
        "form": form or ("20-F" if document.endswith(".xml") else "6-K"),
        "document": document,
        "sha256": sha256,
        "accepted_at_utc": accepted_at_utc,
    }


SOURCES = {
    "2019_q1_comparison": _spec(
        "0001193125-20-165387", "2020-06-10", "d940835dex991.htm",
        "f3eb298a7e838d4cbc7c00248b56ce8c2cb399f0a3fb6a072a914738da75bb8b",
    ),
    "2019_q2_comparison": _spec(
        "0001193125-20-232295", "2020-08-27", "d34225dex991.htm",
        "77666149596dab3ca3d50a928324d48d3d946bf1989df3050c94a68acb0acd9f",
    ),
    "2019_q3_comparison": _spec(
        "0001193125-20-305054", "2020-11-30", "d25625dex991.htm",
        "a76913b252ac1b82880f5b8578c11313c8be5ed7962593e7a6fc67267d6096c9",
    ),
    "2020_q4_report": _spec(
        "0001193125-21-075893", "2021-03-10", "d128953dex991.htm",
        "a4017b3eb54d368a916caed5c5ccaa226d0c739de4b84886ff23001262a685d4",
    ),
    "2020_20f": _spec(
        "0001193125-21-129010", "2021-04-23", "ggal-20201231.xml",
        "91e4a20801dc92626723a3db1780d7b1b77a4d79374c086114244e93a756640f",
    ),
    "2021_q1_report": _spec(
        "0001193125-21-178027", "2021-06-01", "d105558dex991.htm",
        "939eca872714116738ecaf2d464e654f9c9012f81edafcaf5d557f13110fff5f",
    ),
    "2021_q2_report": _spec(
        "0001193125-21-261491", AVAILABLE_DATE, "d154926dex991.htm",
        "299990898e286a45e17e1f370998d9dc2433a5184a603ec909cac0f6c2d3cb44",
        accepted_at_utc=ACCEPTED_AT_UTC,
    ),
    "blocked_2019_q1_report": _spec(
        "0001193125-19-149579", "2019-05-16", "d650008dex991.htm",
        "5d065ea10e730deaa4ce44e97c047fe15a8c6ac9d20a354f12af1474b95a50b0",
    ),
    "blocked_2018_20f": _spec(
        "0001193125-19-146966", "2019-05-15", "d728957d20f.htm",
        "9d7bf111f3fea13dc0a6d5635646c34fe697b2faa97aa5e224b3aa01cdb78331",
        form="20-F",
    ),
    "blocked_2019_q2_report": _spec(
        "0001193125-19-220829", "2019-08-14", "d793034dex991.htm",
        "948d3c38072ceef454db01c09e53837bd74be2af51f0cfe4f018ac9d9d046c47",
    ),
}

BLOCKED_TEXT_CHECKS = {
    "blocked_2018_20f": (
        "financial statements whose functional currency is the Argentine peso, "
        "have been prepared in accordance with IAS 29",
        "results of operations for the year ended December 31, 2018 and 2017 "
        "are reflected in terms of current purchasing power using the Consumer "
        "Price Index",
    ),
    "blocked_2019_q1_report": (
        "criteria for restating the financial information established in IAS 29 "
        "have not been used",
        "Its application would have widespread effects on the financial statements",
    ),
    "blocked_2019_q2_report": (
        "announced its financial results for the second quarter that ended on "
        "June 30, 2019",
        "criteria for restating the financial information established in IAS 29 "
        "has not been used",
        "Its application would have widespread effects on the financial statements",
    ),
}

BLOCKED_AUDIT_OBSERVATIONS = tuple(
    (f"liq{liquidity}-age{age}-growth", signal, age)
    for liquidity in (2_000_000, 10_000_000)
    for age in (150, 365, 550)
    for signal in BLOCKED_SIGNALS
)

# Triplets are current quarter, immediately preceding quarter, prior-year
# quarter, in millions of ARS.  "revenue" is the issuer's Net operating income,
# the quarterly presentation of IFRS RevenueAndOperatingIncome after provisions.
EXPECTED_REPORTS = {
    "2019_q1_comparison": {
        "cpi": Decimal("305.5515"),
        "revenue": (Decimal("38426"), Decimal("35500"), Decimal("46087")),
        "net_income": (Decimal("8536"), Decimal("1308"), Decimal("10668")),
    },
    "2019_q2_comparison": {
        "cpi": Decimal("321.9738"),
        "revenue": (Decimal("42974"), Decimal("40491"), Decimal("43147")),
        "net_income": (Decimal("5637"), Decimal("8995"), Decimal("9933")),
    },
    "2019_q3_comparison": {
        "cpi": Decimal("346.6207"),
        "revenue": (Decimal("41535"), Decimal("46263"), Decimal("40433")),
        "net_income": (Decimal("5511"), Decimal("6069"), Decimal("2702")),
    },
    "2020_q4_report": {
        "cpi": ANNUAL_CPI,
        "revenue": (Decimal("38307"), Decimal("46239"), Decimal("46119")),
        "net_income": (Decimal("3106"), Decimal("6136"), Decimal("1533")),
    },
    "2021_q1_report": {
        "cpi": Decimal("435.8657"),
        "revenue": (Decimal("49648"), Decimal("44040"), Decimal("54814")),
        "net_income": (Decimal("2146"), Decimal("3625"), Decimal("11590")),
    },
    "2021_q2_report": {
        "cpi": TARGET_CPI,
        "revenue": (Decimal("61537"), Decimal("55086"), Decimal("65470")),
        "net_income": (Decimal("8884"), Decimal("2381"), Decimal("8764")),
    },
}
EXPECTED_ANNUAL = {
    2019: {
        "revenue": Decimal("200474991000"),
        "net_income": Decimal("32427485000"),
    },
    2020: {
        "revenue": Decimal("182710688000"),
        "net_income": Decimal("25532780000"),
    },
}
TARGET_FISCAL_ENDS = (
    "2019-09-30", "2019-12-31", "2020-03-31", "2020-06-30",
    "2020-09-30", "2020-12-31", "2021-03-31", "2021-06-30",
)


def _url(spec: dict) -> str:
    return (
        f"https://www.sec.gov/Archives/edgar/data/{CIK}/"
        f"{spec['accession'].replace('-', '')}/{spec['document']}"
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _download(path: Path, spec: dict) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with urlopen(Request(_url(spec), headers=SEC_HEADERS), timeout=120) as response:
        temporary.write_bytes(response.read())
    os.replace(temporary, path)


def _normal_text(raw: bytes) -> str:
    root = etree.HTML(raw)
    if root is None:
        raise RuntimeError("GGAL SEC report is not parseable HTML")
    return " ".join(" ".join(root.itertext()).replace("\xa0", " ").split())


_NUMBER = r"(?:\(\s*)?[0-9][0-9,.]*(?:\s*\))?"


def _number(value: str) -> Decimal:
    text = value.replace(",", "").replace(" ", "")
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    result = Decimal(text)
    return -result if negative else result


def _triplets(text: str, label: str) -> set[tuple[Decimal, Decimal, Decimal]]:
    pattern = re.compile(
        rf"{re.escape(label)}\s+({_NUMBER})\s+({_NUMBER})\s+({_NUMBER})",
        flags=re.IGNORECASE,
    )
    return {
        tuple(_number(value) for value in match.groups())
        for match in pattern.finditer(text)
    }


def parse_report(raw: bytes, expected: dict) -> dict:
    """Verify one issuer report's consolidated IAS 29 quarter triplets."""
    text = _normal_text(raw)
    if "ias 29" not in text.casefold():
        raise RuntimeError("GGAL recovery report lacks its IAS 29 disclosure")
    result = {"cpi": expected["cpi"]}
    for metric, label in (
        ("revenue", "Net operating income"), ("net_income", "Net income")
    ):
        observed = _triplets(text, label)
        if expected[metric] not in observed:
            raise RuntimeError(
                f"GGAL {metric} triplet changed: expected {expected[metric]}, "
                f"observed {sorted(observed)}"
            )
        result[metric] = expected[metric]
    return result


def parse_annual_xbrl(raw: bytes) -> dict[int, dict[str, Decimal]]:
    """Read only dimensionless ARS annual facts from the original 2020 20-F."""
    root = etree.fromstring(raw)
    contexts = {}
    for context in root.xpath('//*[local-name()="context"]'):
        starts = context.xpath('.//*[local-name()="startDate"]/text()')
        ends = context.xpath('.//*[local-name()="endDate"]/text()')
        dimensions = context.xpath(
            './/*[local-name()="explicitMember" or local-name()="typedMember"]'
        )
        if starts and ends and not dimensions:
            contexts[context.get("id")] = (starts[0], ends[0])
    result = {}
    for year in (2019, 2020):
        period = (f"{year}-01-01", f"{year}-12-31")
        result[year] = {}
        for metric, concept in (
            ("revenue", "RevenueAndOperatingIncome"),
            ("net_income", "ProfitLoss"),
        ):
            values = {
                Decimal(element.text)
                for element in root.xpath(f'//*[local-name()="{concept}"]')
                if contexts.get(element.get("contextRef")) == period
                and "ARS" in str(element.get("unitRef", "")).upper()
            }
            if values != {EXPECTED_ANNUAL[year][metric]}:
                raise RuntimeError(
                    f"GGAL original 20-F {year} {metric} changed: {values}"
                )
            result[year][metric] = values.pop()
    return result


def validate_blocked_source_text(raw_by_source: dict[str, bytes]) -> list[dict]:
    checked = []
    for source_name, fragments in BLOCKED_TEXT_CHECKS.items():
        text = _normal_text(raw_by_source[source_name]).casefold()
        for fragment in fragments:
            if fragment.casefold() not in text:
                raise RuntimeError(
                    f"GGAL blocked-source disclosure changed for "
                    f"{source_name}: {fragment}"
                )
            checked.append({"source": source_name, "fragment": fragment})
    return checked


def resolve_blocked_observations() -> pd.DataFrame:
    rows = []
    for scenario, signal, maximum_age_days in BLOCKED_AUDIT_OBSERVATIONS:
        rows.append({
            "scenario": scenario,
            "ticker": TICKER,
            "signal_date": signal,
            "maximum_age_days": maximum_age_days,
            "resolved": False,
            "decision": "unrecoverable_ias29_measurement_basis_conflict",
            "reason": BLOCKED_SIGNALS[signal]["reason"],
        })
    return pd.DataFrame(rows)


def blocked_rejected_derivations() -> list[dict]:
    return [
        {
            "candidate": "combine 2018 IAS-29 annuals with nominal 2019Q1",
            "rejected": True,
            "reason": (
                "annual results are in December-2018 purchasing power while "
                "the issuer says Q1 did not use IAS 29"
            ),
        },
        {
            "candidate": "back-normalize nominal Q1 with an estimated CPI ratio",
            "rejected": True,
            "reason": (
                "IAS 29 has widespread statement effects and no issuer-restated "
                "eight-quarter chain existed by either signal"
            ),
        },
        {
            "candidate": "use 2019Q2 financial results",
            "rejected": True,
            "filed": SOURCES["blocked_2019_q2_report"]["filed"],
            "accession": SOURCES["blocked_2019_q2_report"]["accession"],
            "reason": "filed after both signals and still explicitly nominal",
        },
    ]


def validate_audit_binding(path: Path, expected_sha256: str) -> dict:
    path = Path(path)
    actual_sha = _sha256(path)
    if actual_sha != expected_sha256:
        raise RuntimeError(f"GGAL audit binding changed: {actual_sha}")
    priorities = pd.read_csv(path)
    expected_scenarios = {
        scenario for scenario, _, _ in BLOCKED_AUDIT_OBSERVATIONS
    }
    rows = priorities.loc[
        priorities["ticker"].eq(TICKER)
        & priorities["scenario"].isin(expected_scenarios)
    ]
    if set(rows["scenario"]) != expected_scenarios or len(rows) != len(
        expected_scenarios
    ):
        raise RuntimeError("GGAL priority scenarios changed")
    if not rows["missing_signal_count"].eq(len(BLOCKED_SIGNALS)).all():
        raise RuntimeError("GGAL priority missing-signal counts changed")
    if not rows["no_raw_pit_financial_facts_signal_count"].eq(
        len(BLOCKED_SIGNALS)
    ).all():
        raise RuntimeError("GGAL priority raw-PIT classification changed")
    if set(rows["first_missing_signal_date"]) != {min(BLOCKED_SIGNALS)}:
        raise RuntimeError("GGAL first missing signal changed")
    if set(rows["last_missing_signal_date"]) != {max(BLOCKED_SIGNALS)}:
        raise RuntimeError("GGAL last missing signal changed")
    return {
        "path": str(path),
        "sha256": actual_sha,
        "scenario_count": len(rows),
        "missing_observation_count": len(BLOCKED_AUDIT_OBSERVATIONS),
        "signals": sorted(BLOCKED_SIGNALS),
    }


def _to_target(value_millions: Decimal, source_cpi: Decimal) -> Decimal:
    return value_millions * Decimal(1_000_000) * TARGET_CPI / source_cpi


def derive_quarters(
    reports: dict[str, dict] = EXPECTED_REPORTS,
    annual: dict[int, dict[str, Decimal]] = EXPECTED_ANNUAL,
) -> tuple[dict[str, dict[str, Decimal]], dict, dict]:
    """Normalize pre-signal issuer comparisons and close Q4 to audited FY."""
    all_quarters: dict[str, dict[str, Decimal]] = {}
    inputs = {
        "2019-03-31": ("2019_q1_comparison", 2),
        "2019-06-30": ("2019_q2_comparison", 2),
        "2019-09-30": ("2019_q3_comparison", 2),
        "2020-03-31": ("2021_q1_report", 2),
        "2020-06-30": ("2021_q2_report", 2),
        "2020-09-30": ("2020_q4_report", 1),
        "2021-03-31": ("2021_q2_report", 1),
        "2021-06-30": ("2021_q2_report", 0),
    }
    for fiscal_end, (source_name, position) in inputs.items():
        report = reports[source_name]
        all_quarters[fiscal_end] = {
            metric: _to_target(report[metric][position], report["cpi"])
            for metric in ("revenue", "net_income")
        }
    annual_target = {
        year: {
            metric: value * TARGET_CPI / ANNUAL_CPI
            for metric, value in values.items()
        }
        for year, values in annual.items()
    }
    for year in (2019, 2020):
        all_quarters[f"{year}-12-31"] = {
            metric: annual_target[year][metric] - sum(
                all_quarters[f"{year}-{end}"][metric]
                for end in ("03-31", "06-30", "09-30")
            )
            for metric in ("revenue", "net_income")
        }
    identity_checks = {
        year: {
            metric: sum(
                all_quarters[f"{year}-{end}"][metric]
                for end in ("03-31", "06-30", "09-30", "12-31")
            )
            for metric in ("revenue", "net_income")
        }
        for year in (2019, 2020)
    }
    if identity_checks != annual_target:
        raise RuntimeError(f"GGAL IAS 29 annual identities do not close: {identity_checks}")

    quarters = {end: all_quarters[end] for end in TARGET_FISCAL_ENDS}
    prior_ends = TARGET_FISCAL_ENDS[:4]
    current_ends = TARGET_FISCAL_ENDS[4:]
    prior = {
        metric: sum(quarters[end][metric] for end in prior_ends)
        for metric in ("revenue", "net_income")
    }
    current = {
        metric: sum(quarters[end][metric] for end in current_ends)
        for metric in ("revenue", "net_income")
    }
    ttm = {
        "prior": prior,
        "current": current,
        "growth": {
            metric: (current[metric] - prior[metric]) / abs(prior[metric])
            for metric in ("revenue", "net_income")
        },
    }
    return quarters, identity_checks, ttm


def _source_for_quarter(fiscal_end: str) -> tuple[str, str]:
    mapping = {
        "2019-09-30": ("2019_q3_comparison", "issuer_comparative_cpi_normalized"),
        "2019-12-31": ("2020_20f", "audited_fy_minus_normalized_q1_q3"),
        "2020-03-31": ("2021_q1_report", "issuer_comparative_cpi_normalized"),
        "2020-06-30": ("2021_q2_report", "issuer_comparative_target_cpi"),
        "2020-09-30": ("2020_q4_report", "issuer_comparative_cpi_normalized"),
        "2020-12-31": ("2020_20f", "audited_fy_minus_normalized_q1_q3"),
        "2021-03-31": ("2021_q2_report", "issuer_comparative_target_cpi"),
        "2021-06-30": ("2021_q2_report", "issuer_direct_target_cpi"),
    }
    return mapping[fiscal_end]


def build_facts(
    quarters: dict[str, dict[str, Decimal]], fetched_at: str | pd.Timestamp,
) -> pd.DataFrame:
    rows = []
    for fiscal_end, values in quarters.items():
        source_name, derivation = _source_for_quarter(fiscal_end)
        spec = SOURCES[source_name]
        for metric, value in values.items():
            rows.append({
                "ticker": TICKER,
                "fiscal_end": fiscal_end,
                # The unified June-2021 purchasing-power version is not public
                # until the Q2 report; no underlying comparative is backdated.
                "available_date": AVAILABLE_DATE,
                "metric": metric,
                "value": float(value),
                "taxonomy": "ifrs-full-ias29-ars",
                "concept": (
                    "RevenueAndOperatingIncome" if metric == "revenue"
                    else "ProfitLoss"
                ) + f":{derivation}",
                "form": (
                    "20-F_RESIDUAL_IAS29" if source_name == "2020_20f"
                    else "6-K_IAS29"
                ),
                "accession": spec["accession"],
                "fetched_at": pd.Timestamp(fetched_at).tz_localize(None),
            })
    facts = pd.DataFrame(rows, columns=OUTPUT_COLUMNS).sort_values(
        ["fiscal_end", "metric"]
    ).reset_index(drop=True)
    if (
        len(facts) != 16
        or facts[["ticker", "fiscal_end", "metric"]].duplicated().any()
        or not facts["available_date"].eq(AVAILABLE_DATE).all()
    ):
        raise RuntimeError("GGAL recovery must be exactly eight paired PIT quarters")
    return facts


def recover(
    *,
    source_dir: Path = SOURCE_DIR,
    output_dir: Path = OUTPUT_DIR,
    fetched_at: str | pd.Timestamp = "2026-08-23",
    audit_path: Path = AUDIT_PATH,
    expected_audit_sha256: str = EXPECTED_AUDIT_SHA256,
) -> dict:
    source_dir = Path(source_dir)
    output_dir = Path(output_dir)
    paths = {}
    source_report = []
    for name, spec in SOURCES.items():
        path = source_dir / spec["document"]
        _download(path, spec)
        digest = _sha256(path)
        if digest != spec["sha256"]:
            raise RuntimeError(f"GGAL SEC source changed for {name}: {digest}")
        paths[name] = path
        source_report.append({
            "name": name, "accession": spec["accession"],
            "filed": spec["filed"], "accepted_at_utc": spec["accepted_at_utc"],
            "form": spec["form"], "url": _url(spec), "sha256": digest,
        })

    reports = {
        name: parse_report(paths[name].read_bytes(), expected)
        for name, expected in EXPECTED_REPORTS.items()
    }
    annual = parse_annual_xbrl(paths["2020_20f"].read_bytes())
    blocked_text_checks = validate_blocked_source_text({
        name: paths[name].read_bytes() for name in BLOCKED_TEXT_CHECKS
    })
    audit_binding = validate_audit_binding(
        audit_path, expected_audit_sha256
    )

    quarters, identities, ttm = derive_quarters(reports, annual)
    facts = build_facts(quarters, fetched_at)
    if pd.Timestamp(ACCEPTED_AT_UTC) > pd.Timestamp("2021-08-31T20:00:00Z"):
        raise RuntimeError("GGAL Q2 report was not accepted before the signal close")

    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    observations = resolve_blocked_observations()
    observations_path = output_dir / "unrecoverable_observations.csv"
    observations.to_csv(observations_path, index=False)
    rejected = blocked_rejected_derivations()
    rejected_path = output_dir / "rejected_derivations.json"
    rejected_path.write_text(
        json.dumps(rejected, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    decimal_json = lambda values: {
        key: float(value) if isinstance(value, Decimal) else value
        for key, value in values.items()
    }
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "negative_evidence_source_locked": True,
        "parameters_frozen": False,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "shared_candidate_integrated": False,
        "ticker": TICKER,
        "cik": CIK,
        "currency": "ARS",
        "measurement_basis": "June 30 2021 purchasing power under IAS 29",
        "target_cpi": float(TARGET_CPI),
        "accepted_quarter_count": 8,
        "accepted_fact_count": 16,
        "recoverable_signals": list(RECOVERABLE_SIGNALS),
        "blocked_signals": BLOCKED_SIGNALS,
        "blocked_observation_count": len(observations),
        "blocked_recovery_classification": (
            "UNRECOVERABLE_IAS29_MEASUREMENT_BASIS_CONFLICT"
        ),
        "blocked_source_text_checks": blocked_text_checks,
        "blocked_rejected_derivations": rejected,
        "audit_binding": audit_binding,
        "annual_identity_checks": {
            str(year): decimal_json(values) for year, values in identities.items()
        },
        "ttm_checks": {
            name: decimal_json(values) for name, values in ttm.items()
        },
        "sources": source_report,
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path), "sha256": _sha256(facts_path),
                "row_count": len(facts),
            },
            "unrecoverable_observations": {
                "path": str(observations_path),
                "sha256": _sha256(observations_path),
                "row_count": len(observations),
            },
            "rejected_derivations": {
                "path": str(rejected_path), "sha256": _sha256(rejected_path),
                "row_count": len(rejected),
            },
        },
        "guardrail": (
            "The supplement becomes available only with the 2021-08-31 Q2 "
            "6-K accepted at 14:39:09 UTC. All eight quarters use issuer IAS "
            "29 comparisons available by that time and are normalized with "
            "issuer-disclosed CPI to June 2021 ARS. 2019 and 2020 Q4 are "
            "audited 20-F FY residuals. No post-signal filing is used. The two "
            "2019 signals remain blocked because their Q1 report explicitly "
            "did not apply IAS 29 and no homogeneous eight-quarter chain existed."
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
    parser.add_argument("--source-dir", type=Path, default=SOURCE_DIR)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--fetched-at", default="2026-08-23")
    parser.add_argument("--audit-path", type=Path, default=AUDIT_PATH)
    parser.add_argument(
        "--expected-audit-sha256", default=EXPECTED_AUDIT_SHA256
    )
    args = parser.parse_args()
    report = recover(
        source_dir=args.source_dir,
        output_dir=args.output_dir,
        fetched_at=args.fetched_at,
        audit_path=args.audit_path,
        expected_audit_sha256=args.expected_audit_sha256,
    )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
