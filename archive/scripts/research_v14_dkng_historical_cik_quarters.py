#!/usr/bin/env python3
"""Recover DKNG 2019Q1-2020Q4 from its original historical-CIK filings."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd

from src.io.fundamentals_update import OUTPUT_COLUMNS


TICKER = "DKNG"
HISTORICAL_CIK = 1_772_757
CURRENT_CIK = 1_883_685
RECOVERABLE_SIGNAL = pd.Timestamp("2021-02-26")
UNRECOVERABLE_SIGNAL = pd.Timestamp("2020-09-30")
RAW_PATH = Path("output/data_provenance/dkng_companyfacts/CIK0001772757.json")
OUTPUT_DIR = Path("output/research_only/v14/dkng_historical_cik_quarters")
SEC_HEADERS = {"User-Agent": "quant-stocks-research contact@example.com"}
FETCHED_AT = "2026-08-28"
EXPECTED_RAW_SHA256 = (
    "e11b3c034a24ac1aa6d29ad07a0b9bf036a6f80b135b51a1162938512643265b"
)
BASELINE_AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260828_bctx_apr2021_classified_financial_priorities.csv"
)
EXPECTED_BASELINE_AUDIT_SHA256 = (
    "616ebd6a836bb1f0571ad690fbcd1b0bf56ae06b092041ac406eb976b6243e0e"
)
AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260828_dkng_2020q2_recovered_financial_priorities.csv"
)
EXPECTED_AUDIT_SHA256 = (
    "31f84e8feb0e9af45dbd8c680b565f3231c2aa35003b41e05bd38f82f9ee18d9"
)

S4A_SOURCE = {
    "accession": "0001104659-20-032585",
    "filed": "2020-03-12",
    "form": "S-4/A",
    "document": "tv538206-s4a.htm",
    "expected_sha256": (
        "dc3db681970b2cd4b3f86e08929e2a350404a78411559e747e8f87aa7dffd351"
    ),
}

SOURCES = {
    "2020_q2": {
        "accession": "0001104659-20-095002",
        "filed": "2020-08-14",
        "form": "10-Q",
        "document": "tm2020441-1_10q.htm",
    },
    "2020_q3": {
        "accession": "0001104659-20-124673",
        "filed": "2020-11-13",
        "form": "10-Q",
        "document": "tm2029548d1_10q.htm",
    },
    "2020_fy": {
        "accession": "0001104659-21-028617",
        "filed": "2021-02-26",
        "form": "10-K",
        "document": "tm217048d1_10k.htm",
    },
}
SHELL_ACCESSIONS = {
    "0001104659-20-032113",  # Diamond Eagle Acquisition Corp. 2019 10-K
    "0001104659-20-062038",  # Diamond Eagle Acquisition Corp. 2020Q1 10-Q
}
RESTATEMENT_ACCESSIONS = {
    "0001104659-21-059563",  # post-signal 2020 10-K/A filed 2021-05-03
}
METRIC_CONCEPTS = {
    "revenue": "RevenueFromContractWithCustomerIncludingAssessedTax",
    "net_income": "NetIncomeLoss",
}

# Each input is an actual Old DraftKings period in one of the three original
# operating-company filings.  No S-4/424B3 pro forma concept is accepted.
REQUIRED_FACTS = {
    "2019_h1": {
        "source": "2020_q2", "start": "2019-01-01", "end": "2019-06-30",
        "revenue": 125_482_000.0, "net_income": -57_667_000.0,
    },
    "2019_q2": {
        "source": "2020_q2", "start": "2019-04-01", "end": "2019-06-30",
        "revenue": 57_390_000.0, "net_income": -28_113_000.0,
    },
    "2019_9m": {
        "source": "2020_q3", "start": "2019-01-01", "end": "2019-09-30",
        "revenue": 192_496_000.0, "net_income": -113_586_000.0,
    },
    "2019_q3": {
        "source": "2020_q3", "start": "2019-07-01", "end": "2019-09-30",
        "revenue": 67_014_000.0, "net_income": -55_919_000.0,
    },
    "2019_fy": {
        "source": "2020_fy", "start": "2019-01-01", "end": "2019-12-31",
        "revenue": 323_410_000.0, "net_income": -142_734_000.0,
    },
    "2020_h1": {
        "source": "2020_q2", "start": "2020-01-01", "end": "2020-06-30",
        "revenue": 159_473_000.0, "net_income": -230_117_000.0,
    },
    "2020_q2": {
        "source": "2020_q2", "start": "2020-04-01", "end": "2020-06-30",
        "revenue": 70_931_000.0, "net_income": -161_437_000.0,
    },
    "2020_9m": {
        "source": "2020_q3", "start": "2020-01-01", "end": "2020-09-30",
        "revenue": 292_309_000.0, "net_income": -577_870_000.0,
    },
    "2020_q3": {
        "source": "2020_q3", "start": "2020-07-01", "end": "2020-09-30",
        "revenue": 132_836_000.0, "net_income": -347_753_000.0,
    },
    "2020_fy": {
        "source": "2020_fy", "start": "2020-01-01", "end": "2020-12-31",
        "revenue": 614_532_000.0, "net_income": -844_270_000.0,
    },
}
EXPECTED_QUARTERS = {
    "2019-03-31": {"revenue": 68_092_000.0, "net_income": -29_554_000.0},
    "2019-06-30": {"revenue": 57_390_000.0, "net_income": -28_113_000.0},
    "2019-09-30": {"revenue": 67_014_000.0, "net_income": -55_919_000.0},
    "2019-12-31": {"revenue": 130_914_000.0, "net_income": -29_148_000.0},
    "2020-03-31": {"revenue": 88_542_000.0, "net_income": -68_680_000.0},
    "2020-06-30": {"revenue": 70_931_000.0, "net_income": -161_437_000.0},
    "2020-09-30": {"revenue": 132_836_000.0, "net_income": -347_753_000.0},
    "2020-12-31": {"revenue": 322_223_000.0, "net_income": -266_400_000.0},
}
EXPECTED_OLD_DRAFTKINGS_ANNUAL = {
    2019: {"revenue": 323_410_000.0, "net_income": -142_734_000.0},
    2018: {"revenue": 226_277_000.0, "net_income": -76_220_000.0},
    2017: {"revenue": 191_844_000.0, "net_income": -75_556_000.0},
}
EXPECTED_SIGNAL_TTM = {
    "revenue": 357_401_000.0,
    "net_income": -315_184_000.0,
}
TARGET_FISCAL_ENDS = frozenset(EXPECTED_QUARTERS)
TARGET_METRICS = frozenset(METRIC_CONCEPTS)
DIRECT_TTM_METRICS = frozenset({"revenue_ttm", "net_income_ttm"})
AUDIT_OBSERVATIONS = (
    ("liq2000000-age150-growth", 150),
    ("liq2000000-age365-growth", 365),
    ("liq2000000-age550-growth", 550),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_url(spec: dict) -> str:
    accession = spec["accession"].replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{HISTORICAL_CIK}/"
        f"{accession}/{spec['document']}"
    )


def _download_bytes(url: str) -> bytes:
    with urlopen(Request(url, headers=SEC_HEADERS), timeout=120) as response:
        return response.read()


def _plain_text(payload: bytes) -> str:
    decoded = payload.decode("utf-8", errors="replace")
    decoded = re.sub(r"<script\b.*?</script>", " ", decoded, flags=re.I | re.S)
    decoded = re.sub(r"<style\b.*?</style>", " ", decoded, flags=re.I | re.S)
    decoded = re.sub(r"<[^>]+>", " ", decoded)
    decoded = html.unescape(decoded).replace("\u200b", " ")
    return re.sub(r"\s+", " ", decoded).strip()


def verify_s4a_actual_annuals(
    payload: bytes,
    expected_sha256: str = S4A_SOURCE["expected_sha256"],
) -> dict:
    actual_sha = hashlib.sha256(payload).hexdigest()
    if actual_sha != expected_sha256:
        raise RuntimeError(f"DKNG S-4/A source SHA changed: {actual_sha}")
    text = _plain_text(payload)
    required = (
        "SELECTED HISTORICAL CONSOLIDATED FINANCIAL INFORMATION OF DRAFTKINGS",
        "derived from the audited historical consolidated financial statements "
        "of DraftKings",
        "prior to and without giving pro forma effect to the impact of the "
        "Business Combination",
        "For the year ended December 31",
        "Statement of Operations Data",
    )
    missing = [fragment for fragment in required if fragment not in text]
    if missing:
        raise RuntimeError(f"DKNG S-4/A actual-basis guards missing: {missing}")
    revenue = (
        r"Revenue\s+323,410\s+\$?\s*226,277\s+\$?\s*191,844"
    )
    loss = (
        r"Net Loss\s+\$?\s*\(\s*142,734\s*\)\s+\$?\s*"
        r"\(\s*76,220\s*\)\s+\$?\s*\(\s*75,556\s*\)"
    )
    if re.search(revenue, text, flags=re.I) is None or re.search(
        loss, text, flags=re.I
    ) is None:
        raise RuntimeError("DKNG S-4/A actual annual rows changed")
    return {
        **S4A_SOURCE,
        "url": _source_url(S4A_SOURCE),
        "sha256": actual_sha,
        "bytes": len(payload),
        "basis": "Old DraftKings audited historical actuals, not pro forma",
        "annual_actuals": EXPECTED_OLD_DRAFTKINGS_ANNUAL,
    }


def _download_companyfacts(path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    url = (
        "https://data.sec.gov/api/xbrl/companyfacts/"
        f"CIK{HISTORICAL_CIK:010d}.json"
    )
    with urlopen(Request(url, headers=SEC_HEADERS), timeout=120) as response:
        temporary.write_bytes(response.read())
    if temporary.stat().st_size < 100_000:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("DKNG historical Company Facts payload is unexpectedly small")
    os.replace(temporary, path)


def _load_bound_payload(
    raw_path: Path,
    expected_sha256: str = EXPECTED_RAW_SHA256,
) -> dict:
    _download_companyfacts(raw_path)
    actual_sha = _sha256(raw_path)
    if actual_sha != expected_sha256:
        raise RuntimeError(f"DKNG Company Facts source SHA changed: {actual_sha}")
    payload = json.loads(raw_path.read_text(encoding="utf-8"))
    if int(payload.get("cik", 0)) != HISTORICAL_CIK:
        raise RuntimeError(
            f"DKNG recovery requires historical CIK {HISTORICAL_CIK}, "
            f"not current CIK {CURRENT_CIK} or another issuer"
        )
    if "DRAFTKINGS" not in str(payload.get("entityName", "")).upper():
        raise RuntimeError("historical-CIK payload issuer is not DraftKings")
    return payload


def _extract_required_facts(payload: dict) -> dict[str, dict[str, float]]:
    extracted: dict[str, dict[str, float]] = {}
    for name, requirement in REQUIRED_FACTS.items():
        spec = SOURCES[requirement["source"]]
        if pd.Timestamp(spec["filed"]) > RECOVERABLE_SIGNAL:
            raise RuntimeError(f"post-signal source is forbidden: {spec['accession']}")
        values = {}
        for metric, concept in METRIC_CONCEPTS.items():
            try:
                units = payload["facts"]["us-gaap"][concept]["units"]["USD"]
            except KeyError as exc:
                raise RuntimeError(f"DKNG Company Facts lacks {concept} USD facts") from exc
            matches = [
                fact for fact in units
                if fact.get("accn") == spec["accession"]
                and fact.get("filed") == spec["filed"]
                and fact.get("form") == spec["form"]
                and fact.get("start") == requirement["start"]
                and fact.get("end") == requirement["end"]
            ]
            observed = {float(fact["val"]) for fact in matches}
            expected = float(requirement[metric])
            if observed != {expected}:
                raise RuntimeError(
                    f"DKNG original fact changed for {name} {metric}: {observed}"
                )
            values[metric] = expected
        extracted[name] = values
    return extracted


def derive_quarters(inputs: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    """Derive only H1-minus-Q2 and FY-minus-9M residual quarters."""
    quarters: dict[str, dict[str, float]] = {}
    for year in (2019, 2020):
        quarters[f"{year}-03-31"] = {
            metric: inputs[f"{year}_h1"][metric] - inputs[f"{year}_q2"][metric]
            for metric in TARGET_METRICS
        }
        quarters[f"{year}-06-30"] = dict(inputs[f"{year}_q2"])
        quarters[f"{year}-09-30"] = dict(inputs[f"{year}_q3"])
        quarters[f"{year}-12-31"] = {
            metric: inputs[f"{year}_fy"][metric] - inputs[f"{year}_9m"][metric]
            for metric in TARGET_METRICS
        }
    observed = dict(sorted(quarters.items()))
    if observed != EXPECTED_QUARTERS:
        raise RuntimeError(f"DKNG recovered quarters changed: {observed}")
    return observed


def _source_for_quarter(fiscal_end: str) -> tuple[str, str]:
    month = fiscal_end[5:7]
    if month in {"03", "06"}:
        return "2020_q2", "h1_minus_q2" if month == "03" else "direct_quarter"
    if month == "09":
        return "2020_q3", "direct_quarter"
    return "2020_fy", "fy_minus_9m"


def derive_signal_ttm(
    inputs: dict[str, dict[str, float]],
    annual_actuals: dict[int, dict[str, float]] = EXPECTED_OLD_DRAFTKINGS_ANNUAL,
) -> dict[str, float]:
    """Bridge audited FY2019 actuals through the timely 2020Q2 comparison."""
    result = {
        metric: (
            annual_actuals[2019][metric]
            - inputs["2019_h1"][metric]
            + inputs["2020_h1"][metric]
        )
        for metric in TARGET_METRICS
    }
    if result != EXPECTED_SIGNAL_TTM:
        raise RuntimeError(f"DKNG 2020Q2 direct TTM changed: {result}")
    return result


def _audit_rows(path: Path, expected_sha256: str) -> pd.DataFrame:
    actual_sha = _sha256(path)
    if actual_sha != expected_sha256:
        raise RuntimeError(f"DKNG audit binding changed: {actual_sha}")
    priorities = pd.read_csv(path)
    scenarios = {scenario for scenario, _ in AUDIT_OBSERVATIONS}
    return priorities.loc[
        priorities["ticker"].eq(TICKER)
        & priorities["scenario"].isin(scenarios)
    ].copy()


def _validate_baseline_audit(path: Path, expected_sha256: str) -> dict:
    rows = _audit_rows(path, expected_sha256)
    scenarios = {scenario for scenario, _ in AUDIT_OBSERVATIONS}
    if set(rows["scenario"]) != scenarios or len(rows) != len(scenarios):
        raise RuntimeError("DKNG baseline audit scenarios changed")
    expected_one = (
        "missing_signal_count",
        "insufficient_growth_history_signal_count",
    )
    if any(not rows[column].eq(1).all() for column in expected_one):
        raise RuntimeError("DKNG baseline missing classification changed")
    expected_zero = (
        "no_raw_pit_financial_facts_signal_count",
        "stale_growth_snapshot_signal_count",
    )
    if any(not rows[column].eq(0).all() for column in expected_zero):
        raise RuntimeError("DKNG baseline raw-fact classification changed")
    if set(rows["first_missing_signal_date"]) != {
        UNRECOVERABLE_SIGNAL.strftime("%Y-%m-%d")
    } or set(rows["last_missing_signal_date"]) != {
        UNRECOVERABLE_SIGNAL.strftime("%Y-%m-%d")
    }:
        raise RuntimeError("DKNG baseline signal date changed")
    return {
        "path": str(path),
        "sha256": expected_sha256,
        "missing_observation_count": len(rows),
        "classification": "insufficient_growth_history",
    }


def _validate_current_audit(path: Path, expected_sha256: str) -> dict:
    rows = _audit_rows(path, expected_sha256)
    if rows.empty:
        return {
            "path": str(path),
            "sha256": expected_sha256,
            "remaining_observation_count": 0,
            "status": "RECOVERED_KNOWN_NONPOSITIVE_DIRECT_TTM",
        }
    baseline = _validate_baseline_audit(path, expected_sha256)
    return {
        "path": str(path),
        "sha256": expected_sha256,
        "remaining_observation_count": baseline["missing_observation_count"],
        "status": "PENDING_CANDIDATE_INTEGRATION",
    }


def _recovered_observations() -> pd.DataFrame:
    return pd.DataFrame([
        {
            "scenario": scenario,
            "ticker": TICKER,
            "signal_date": UNRECOVERABLE_SIGNAL.strftime("%Y-%m-%d"),
            "maximum_age_days": maximum_age_days,
            "resolved": True,
            "decision": "recovered_known_nonpositive_direct_ttm",
            "net_income_ttm_usd": EXPECTED_SIGNAL_TTM["net_income"],
            "available_date": SOURCES["2020_q2"]["filed"],
        }
        for scenario, maximum_age_days in AUDIT_OBSERVATIONS
    ])


def recover(
    *,
    raw_path: Path = RAW_PATH,
    output_dir: Path = OUTPUT_DIR,
    fetched_at: str | pd.Timestamp | None = None,
    s4a_path: Path | None = None,
    expected_raw_sha256: str = EXPECTED_RAW_SHA256,
    expected_s4a_sha256: str = S4A_SOURCE["expected_sha256"],
    baseline_audit_path: Path = BASELINE_AUDIT_PATH,
    expected_baseline_audit_sha256: str = EXPECTED_BASELINE_AUDIT_SHA256,
    audit_path: Path = AUDIT_PATH,
    expected_audit_sha256: str = EXPECTED_AUDIT_SHA256,
) -> dict:
    raw_path = Path(raw_path)
    output_dir = Path(output_dir)
    payload = _load_bound_payload(raw_path, expected_raw_sha256)
    s4a_path = Path(s4a_path) if s4a_path is not None else (
        output_dir / "sources" / "source_0001104659-20-032585_s4a.html"
    )
    if s4a_path.exists():
        s4a_payload = s4a_path.read_bytes()
    else:
        s4a_payload = _download_bytes(_source_url(S4A_SOURCE))
    s4a_evidence = verify_s4a_actual_annuals(
        s4a_payload, expected_s4a_sha256
    )
    if not s4a_path.exists():
        s4a_path.parent.mkdir(parents=True, exist_ok=True)
        s4a_path.write_bytes(s4a_payload)
    inputs = _extract_required_facts(payload)
    quarters = derive_quarters(inputs)
    signal_ttm = derive_signal_ttm(inputs)
    fetched = (
        pd.Timestamp(FETCHED_AT)
        if fetched_at is None else pd.Timestamp(fetched_at).tz_localize(None)
    )
    rows = []
    for fiscal_end, metrics in quarters.items():
        source_name, derivation = _source_for_quarter(fiscal_end)
        source = SOURCES[source_name]
        available_date = pd.Timestamp(source["filed"])
        if available_date > RECOVERABLE_SIGNAL:
            raise RuntimeError("DKNG quarter was not public by the recoverable signal")
        for metric, value in metrics.items():
            rows.append({
                "ticker": TICKER,
                "fiscal_end": fiscal_end,
                "available_date": source["filed"],
                "metric": metric,
                "value": value,
                "taxonomy": "us-gaap",
                "concept": f"sec_original_{derivation}:{metric}",
                "form": source["form"] if derivation == "direct_quarter" else f"{source['form']}_RESIDUAL",
                "accession": source["accession"],
                "fetched_at": fetched,
            })
    direct_accession = (
        f"{S4A_SOURCE['accession']}+{SOURCES['2020_q2']['accession']}"
    )
    for metric, value in signal_ttm.items():
        rows.append({
            "ticker": TICKER,
            "fiscal_end": "2020-06-30",
            "available_date": SOURCES["2020_q2"]["filed"],
            "metric": f"{metric}_ttm",
            "value": value,
            "taxonomy": "us-gaap",
            "concept": "derived_fy2019_minus_h1_2019_plus_h1_2020",
            "form": "S-4/A+10-Q_DERIVED",
            "accession": direct_accession,
            "fetched_at": fetched,
        })
    facts = pd.DataFrame(rows, columns=OUTPUT_COLUMNS).sort_values(
        ["fiscal_end", "metric"]
    ).reset_index(drop=True)
    quarter_facts = facts.loc[facts["metric"].isin(TARGET_METRICS)]
    if (
        len(facts) != 18
        or facts[["ticker", "fiscal_end", "metric"]].duplicated().any()
        or set(quarter_facts["accession"])
        != {spec["accession"] for spec in SOURCES.values()}
        or set(facts.loc[facts["metric"].isin(DIRECT_TTM_METRICS), "accession"])
        != {direct_accession}
    ):
        raise RuntimeError(
            "DKNG recovery must contain eight paired quarters and two direct TTMs"
        )

    period_identity_checks = {}
    annual_checks = {}
    for year in (2019, 2020):
        calculated = {
            "h1": {
                metric: float(sum(
                    quarters[f"{year}-{end}"][metric]
                    for end in ("03-31", "06-30")
                ))
                for metric in TARGET_METRICS
            },
            "9m": {
                metric: float(sum(
                    quarters[f"{year}-{end}"][metric]
                    for end in ("03-31", "06-30", "09-30")
                ))
                for metric in TARGET_METRICS
            },
            "fy": {
                metric: float(sum(
                    quarters[f"{year}-{end}"][metric]
                    for end in ("03-31", "06-30", "09-30", "12-31")
                ))
                for metric in TARGET_METRICS
            },
        }
        expected = {
            period: dict(inputs[f"{year}_{period}"])
            for period in ("h1", "9m", "fy")
        }
        if calculated != expected:
            raise RuntimeError(
                f"DKNG cumulative-period identities do not close for {year}: "
                f"{calculated}"
            )
        period_identity_checks[year] = calculated
        annual_checks[year] = calculated["fy"]
    expected_annual = {
        year: dict(inputs[f"{year}_fy"]) for year in (2019, 2020)
    }
    if annual_checks != expected_annual:
        raise RuntimeError(f"DKNG annual identities do not close: {annual_checks}")
    current_ttm = annual_checks[2020]
    prior_ttm = annual_checks[2019]
    if current_ttm["net_income"] >= 0:
        raise RuntimeError("DKNG 2020 TTM net income must remain negative")

    available = facts.loc[
        pd.to_datetime(facts["available_date"]) <= UNRECOVERABLE_SIGNAL,
        "fiscal_end",
    ].unique().tolist()
    required_first_signal = {
        "2018-09-30", "2018-12-31", "2019-03-31", "2019-06-30",
        "2019-09-30", "2019-12-31", "2020-03-31", "2020-06-30",
    }
    missing_growth_history = sorted(required_first_signal - set(available))
    if not missing_growth_history or signal_ttm["net_income"] >= 0:
        raise RuntimeError("DKNG direct-TTM recovery invariants changed")

    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    recovered_observations = _recovered_observations()
    recovered_path = output_dir / "recovered_observations.csv"
    recovered_observations.to_csv(recovered_path, index=False)
    baseline_audit = _validate_baseline_audit(
        Path(baseline_audit_path), expected_baseline_audit_sha256
    )
    current_audit = _validate_current_audit(
        Path(audit_path), expected_audit_sha256
    )
    source_report = [
        {
            "name": name,
            "accession": spec["accession"],
            "filed": spec["filed"],
            "filed_on_or_before_signal": (
                pd.Timestamp(spec["filed"]) <= RECOVERABLE_SIGNAL
            ),
            "form": spec["form"],
            "url": _source_url(spec),
        }
        for name, spec in SOURCES.items()
    ]
    report = {
        "schema_version": 2,
        "research_only": True,
        "point_in_time_proven": True,
        "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "ticker": TICKER,
        "historical_cik": HISTORICAL_CIK,
        "current_cik_excluded": CURRENT_CIK,
        "accepted_quarter_count": 8,
        "accepted_fact_count": 18,
        "accepted_direct_ttm_fact_count": 2,
        "annual_identity_checks": annual_checks,
        "period_identity_checks": period_identity_checks,
        "ttm_checks": {
            "2019": prior_ttm,
            "2020": current_ttm,
            "2020_net_income_is_negative": current_ttm["net_income"] < 0,
            "signal_2020q2": signal_ttm,
            "signal_2020q2_net_income_is_negative": (
                signal_ttm["net_income"] < 0
            ),
            "signal_2020q2_formula": "FY2019 - H1_2019 + H1_2020",
        },
        "signal_coverage": {
            "2020-09-30": {
                "recoverable": True,
                "classification": "KNOWN_NONPOSITIVE_DIRECT_TTM_PROFIT",
                "net_income_ttm": signal_ttm["net_income"],
                "revenue_ttm": signal_ttm["revenue"],
                "available_date": SOURCES["2020_q2"]["filed"],
                "missing_growth_history": missing_growth_history,
                "reason": (
                    "The S-4/A supplies audited FY2019 Old DraftKings actuals "
                    "and the Q2 10-Q supplies H1 2019/2020 actuals. Their exact "
                    "TTM net loss fails the positive-profit gate, so absent "
                    "2018 growth history is not needed to classify the signal."
                ),
            },
            "2021-02-26": {
                "recoverable": True,
                "recovered_fiscal_ends": sorted(TARGET_FISCAL_ENDS),
            },
        },
        "exclusions": {
            "shell_accessions": sorted(SHELL_ACCESSIONS),
            "restatement_accessions": sorted(RESTATEMENT_ACCESSIONS),
            "pro_forma_concepts_used": False,
            "current_cik_used": False,
        },
        "sources": source_report,
        "s4a_actual_source": {
            **s4a_evidence,
            "local_path": str(s4a_path),
        },
        "raw_payload": {
            "path": str(raw_path),
            "sha256": _sha256(raw_path),
            "source_url": (
                "https://data.sec.gov/api/xbrl/companyfacts/"
                f"CIK{HISTORICAL_CIK:010d}.json"
            ),
        },
        "audit_binding": {
            "baseline": baseline_audit,
            "current": current_audit,
            "recovered_observation_count": len(recovered_observations),
        },
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path), "sha256": _sha256(facts_path),
            },
            "recovered_observations": {
                "path": str(recovered_path),
                "sha256": _sha256(recovered_path),
                "row_count": len(recovered_observations),
            },
        },
        "guardrail": (
            "The 2020Q2 10-Q, 2020Q3 10-Q, and 2020 10-K operating facts use "
            "historical CIK 1772757. The pre-signal direct TTM uses only the "
            "audited historical Old DraftKings actuals in the 2020-03-12 "
            "S-4/A plus H1 comparisons in the 2020-08-14 10-Q. Diamond Eagle "
            "shell facts, S-4/A pro forma tables, current CIK 1883685, and the "
            "post-signal 10-K/A are excluded; no fact is backdated."
        ),
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    report["manifest"] = str(manifest_path)
    return report


def _target_mask(frame: pd.DataFrame) -> pd.Series:
    ends = pd.to_datetime(frame["fiscal_end"], errors="raise").dt.strftime("%Y-%m-%d")
    return (
        frame["ticker"].eq(TICKER)
        & ends.isin(TARGET_FISCAL_ENDS)
        & frame["metric"].isin(TARGET_METRICS)
    )


def _direct_ttm_mask(frame: pd.DataFrame) -> pd.Series:
    ends = pd.to_datetime(
        frame["fiscal_end"], errors="raise"
    ).dt.strftime("%Y-%m-%d")
    return (
        frame["ticker"].eq(TICKER)
        & ends.eq("2020-06-30")
        & frame["metric"].isin(DIRECT_TTM_METRICS)
    )


def integrate_candidate(
    *,
    base_dir: Path,
    supplement_dir: Path = OUTPUT_DIR,
    output_dir: Path,
) -> dict:
    """Copy-on-write overlay of only DKNG's exact eight-quarter key space."""
    base_dir = Path(base_dir)
    supplement_dir = Path(supplement_dir)
    output_dir = Path(output_dir)
    inputs = (
        base_dir / "annual.csv",
        base_dir / "quarterly.csv",
        base_dir / "manifest.json",
        supplement_dir / "strict_quarterly_facts.csv",
        supplement_dir / "manifest.json",
    )
    bound = {path: _sha256(path) for path in inputs}
    base = pd.read_csv(inputs[1])
    incoming = pd.read_csv(inputs[3])
    if list(base.columns) != OUTPUT_COLUMNS or list(incoming.columns) != OUTPUT_COLUMNS:
        raise RuntimeError("DKNG candidate integration requires the quarterly schema")
    incoming_keys = set(zip(
        incoming["ticker"],
        pd.to_datetime(incoming["fiscal_end"]).dt.strftime("%Y-%m-%d"),
        incoming["metric"],
    ))
    expected_keys = {
        (TICKER, fiscal_end, metric)
        for fiscal_end in TARGET_FISCAL_ENDS for metric in TARGET_METRICS
    } | {
        (TICKER, "2020-06-30", metric) for metric in DIRECT_TTM_METRICS
    }
    if incoming_keys != expected_keys or not (
        _target_mask(incoming) | _direct_ttm_mask(incoming)
    ).all():
        raise RuntimeError(
            "DKNG supplement scope is not eight paired quarters plus direct TTMs"
        )

    target = _target_mask(base) | _direct_ttm_mask(base)
    replaced = base.loc[target].copy()
    untouched = base.loc[~target].copy()
    merged = pd.concat([untouched, incoming], ignore_index=True).sort_values(
        ["ticker", "fiscal_end", "metric", "available_date"]
    ).reset_index(drop=True)
    if len(merged) != len(untouched) + 18:
        raise RuntimeError("DKNG overlay changed rows outside the bounded key space")

    output_dir.mkdir(parents=True, exist_ok=True)
    annual_output = output_dir / "annual.csv"
    quarterly_output = output_dir / "quarterly.csv"
    shutil.copyfile(inputs[0], annual_output)
    merged.to_csv(quarterly_output, index=False)
    if {path: _sha256(path) for path in inputs} != bound:
        raise RuntimeError("DKNG integration source changed while being read")
    report = {
        "schema_version": 1,
        "research_only": True,
        "formal_financials_modified": False,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "overlay_ticker": TICKER,
        "overlay_fiscal_ends": sorted(TARGET_FISCAL_ENDS),
        "overlay_metrics": sorted(TARGET_METRICS),
        "removed_conflicting_rows": len(replaced),
        "inserted_strict_rows": len(incoming),
        "base": {"path": str(base_dir), "sha256": {str(k): v for k, v in bound.items()}},
        "outputs": {
            "annual": str(annual_output),
            "annual_sha256": _sha256(annual_output),
            "quarterly": str(quarterly_output),
            "quarterly_sha256": _sha256(quarterly_output),
            "quarterly_rows": len(merged),
        },
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    report["manifest"] = str(manifest_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-path", type=Path, default=RAW_PATH)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--s4a-path", type=Path)
    parser.add_argument("--expected-raw-sha256", default=EXPECTED_RAW_SHA256)
    parser.add_argument(
        "--expected-s4a-sha256", default=S4A_SOURCE["expected_sha256"]
    )
    parser.add_argument(
        "--baseline-audit-path", type=Path, default=BASELINE_AUDIT_PATH
    )
    parser.add_argument(
        "--expected-baseline-audit-sha256",
        default=EXPECTED_BASELINE_AUDIT_SHA256,
    )
    parser.add_argument("--audit-path", type=Path, default=AUDIT_PATH)
    parser.add_argument(
        "--expected-audit-sha256", default=EXPECTED_AUDIT_SHA256
    )
    parser.add_argument("--base-dir", type=Path)
    parser.add_argument("--candidate-output-dir", type=Path)
    args = parser.parse_args()
    if (args.base_dir is None) != (args.candidate_output_dir is None):
        parser.error("--base-dir and --candidate-output-dir must be used together")
    report = recover(
        raw_path=args.raw_path,
        output_dir=args.output_dir,
        s4a_path=args.s4a_path,
        expected_raw_sha256=args.expected_raw_sha256,
        expected_s4a_sha256=args.expected_s4a_sha256,
        baseline_audit_path=args.baseline_audit_path,
        expected_baseline_audit_sha256=(
            args.expected_baseline_audit_sha256
        ),
        audit_path=args.audit_path,
        expected_audit_sha256=args.expected_audit_sha256,
    )
    if args.base_dir is not None:
        report["candidate"] = integrate_candidate(
            base_dir=args.base_dir,
            supplement_dir=args.output_dir,
            output_dir=args.candidate_output_dir,
        )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
