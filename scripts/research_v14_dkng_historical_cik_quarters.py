#!/usr/bin/env python3
"""Recover DKNG 2019Q1-2020Q4 from its original historical-CIK filings."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
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
TARGET_FISCAL_ENDS = frozenset(EXPECTED_QUARTERS)
TARGET_METRICS = frozenset(METRIC_CONCEPTS)


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


def _load_bound_payload(raw_path: Path) -> dict:
    _download_companyfacts(raw_path)
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


def recover(
    *,
    raw_path: Path = RAW_PATH,
    output_dir: Path = OUTPUT_DIR,
    fetched_at: str | pd.Timestamp | None = None,
) -> dict:
    raw_path = Path(raw_path)
    output_dir = Path(output_dir)
    payload = _load_bound_payload(raw_path)
    inputs = _extract_required_facts(payload)
    quarters = derive_quarters(inputs)
    fetched = (
        pd.Timestamp.now("UTC").tz_localize(None).normalize()
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
    facts = pd.DataFrame(rows, columns=OUTPUT_COLUMNS).sort_values(
        ["fiscal_end", "metric"]
    ).reset_index(drop=True)
    if (
        len(facts) != 16
        or facts[["ticker", "fiscal_end", "metric"]].duplicated().any()
        or set(facts["accession"]) != {spec["accession"] for spec in SOURCES.values()}
    ):
        raise RuntimeError("DKNG recovery must be exactly eight paired original quarters")

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
    first_missing = sorted(required_first_signal - set(available))
    if not first_missing:
        raise RuntimeError("2020-09-30 must remain unrecoverable")

    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
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
        "schema_version": 1,
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
        "accepted_fact_count": 16,
        "annual_identity_checks": annual_checks,
        "period_identity_checks": period_identity_checks,
        "ttm_checks": {
            "2019": prior_ttm,
            "2020": current_ttm,
            "2020_net_income_is_negative": current_ttm["net_income"] < 0,
        },
        "signal_coverage": {
            "2020-09-30": {
                "recoverable": False,
                "missing_from_bounded_supplement": first_missing,
                "strict_unrecoverable_anchor": "2018-09-30",
                "reason": (
                    "The pre-signal S-4/424B3 chain has no Old DraftKings "
                    "2018 H1 or discrete 2018Q3 actual revenue/net income."
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
        "raw_payload": {
            "path": str(raw_path),
            "sha256": _sha256(raw_path),
            "source_url": (
                "https://data.sec.gov/api/xbrl/companyfacts/"
                f"CIK{HISTORICAL_CIK:010d}.json"
            ),
        },
        "outputs": {"strict_quarterly_facts": {
            "path": str(facts_path), "sha256": _sha256(facts_path),
        }},
        "guardrail": (
            "Only the original 2020Q2 10-Q, 2020Q3 10-Q, and 2020 10-K "
            "accessions under historical CIK 1772757 are eligible. Diamond "
            "Eagle shell facts, S-4/424B3 pro forma concepts, current CIK "
            "1883685, and the post-signal 2020 10-K/A are excluded. Q1 is "
            "H1 minus Q2 and Q4 is FY minus 9M; no fact is backdated."
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
    }
    if incoming_keys != expected_keys or not _target_mask(incoming).all():
        raise RuntimeError("DKNG supplement scope is not exactly eight paired quarters")

    target = _target_mask(base)
    replaced = base.loc[target].copy()
    untouched = base.loc[~target].copy()
    merged = pd.concat([untouched, incoming], ignore_index=True).sort_values(
        ["ticker", "fiscal_end", "metric", "available_date"]
    ).reset_index(drop=True)
    if len(merged) != len(untouched) + 16:
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
    parser.add_argument("--base-dir", type=Path)
    parser.add_argument("--candidate-output-dir", type=Path)
    args = parser.parse_args()
    if (args.base_dir is None) != (args.candidate_output_dir is None):
        parser.error("--base-dir and --candidate-output-dir must be used together")
    report = recover(raw_path=args.raw_path, output_dir=args.output_dir)
    if args.base_dir is not None:
        report["candidate"] = integrate_candidate(
            base_dir=args.base_dir,
            supplement_dir=args.output_dir,
            output_dir=args.candidate_output_dir,
        )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
