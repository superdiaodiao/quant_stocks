#!/usr/bin/env python3
"""Audit v14 candidate-path data before adaptive walk-forward fitting."""

from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path

import pandas as pd

from src.conf import CLEANED_PRICE_DATA_DIR, NASDAQ_INDEX_FILE
from src.financial.quarterly_fundamentals import load_quarterly_fundamentals
from src.io.fundamentals_update import (
    SEC_COMPANYFACTS_CACHE_DIR,
    cached_companyfacts_symbol_payload_profiles,
)
from src.research.can_slim_validation import technical_candidate_financial_coverage
from src.research.can_slim_walk_forward import candidate_configs
from src.research.historical_data_audit import (
    audit_signal_price_coverage,
    load_price_date_metadata,
)
from src.research.panel_data import load_panel
from src.research.universe_history import load_universe_snapshots


DEFAULT_QUARTERLY = Path(
    "output/data_provenance/companyfacts_proven_only_manifest-"
    "6c8a87fcc71cfcd5-recipe-6f0998be-q1-fp-guard-bank-duration-v3/quarterly.csv"
)
DEFAULT_SNAPSHOT_DIR = Path("output/research_only/v14/universe_snapshots")
DEFAULT_SUPPLEMENTAL_CACHE = Path("output/research_only/v14/companyfacts_cache")
DEFAULT_PREFIX = Path("output/research_only/v14/candidate_path_audit")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _price_directory_binding(path: Path) -> dict:
    digest = hashlib.sha256()
    files = sorted(path.glob("*.csv"))
    for price_file in files:
        digest.update(price_file.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256(price_file).encode("ascii"))
        digest.update(b"\n")
    return {
        "path": str(path),
        "file_count": len(files),
        "content_manifest_sha256": digest.hexdigest(),
    }


def representative_configs(start: str, end: str):
    """Keep every data-relevant setting while removing top-N duplicates."""
    configs = candidate_configs(
        signal_frequency="monthly",
        use_quarterly_fundamentals=True,
        adaptive_channel=False,
        end=end,
        maximum_financial_age_days=(150, 365, 550),
    )
    unique = {}
    for config in configs:
        key = (
            float(config.minimum_median_dollar_volume),
            int(config.maximum_financial_age_days),
            str(config.selection_mode),
            float(config.minimum_relative_volume),
            float(config.minimum_52_week_high_ratio),
            float(config.minimum_eps_growth),
            float(config.minimum_revenue_growth),
        )
        unique.setdefault(key, replace(config, start=start, end=end))
    return [unique[key] for key in sorted(unique)]


def scenario_id(config) -> str:
    return (
        f"liq{int(config.minimum_median_dollar_volume)}-"
        f"age{int(config.maximum_financial_age_days)}-"
        f"{config.selection_mode}"
    )


def build_audit(
    *,
    start: str = "2019-01-01",
    end: str = "2021-12-31",
    snapshot_dir: Path = DEFAULT_SNAPSHOT_DIR,
    quarterly_path: Path = DEFAULT_QUARTERLY,
    supplemental_cache_dir: Path | None = None,
    price_dir: Path = Path(CLEANED_PRICE_DATA_DIR),
) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    load_start = (
        pd.Timestamp(start) - pd.Timedelta(days=400)
    ).strftime("%Y-%m-%d")
    close, dollar_volume = load_panel(price_dir, load_start, end)
    nasdaq = pd.read_csv(
        NASDAQ_INDEX_FILE, index_col="date", parse_dates=True
    )["close"]
    quarterly = load_quarterly_fundamentals(quarterly_path)
    annual_path = quarterly_path.with_name("annual.csv")
    annual = pd.read_csv(annual_path) if annual_path.exists() else None
    raw_profiles = cached_companyfacts_symbol_payload_profiles(
        Path(SEC_COMPANYFACTS_CACHE_DIR)
    )
    if supplemental_cache_dir is not None and supplemental_cache_dir.exists():
        raw_profiles.update(
            cached_companyfacts_symbol_payload_profiles(supplemental_cache_dir)
        )
    snapshots = load_universe_snapshots(snapshot_dir)
    configs = representative_configs(start, end)

    financial_scenarios = {}
    priority_rows = []
    for config in configs:
        identifier = scenario_id(config)
        coverage = technical_candidate_financial_coverage(
            close, dollar_volume, nasdaq, quarterly, snapshots, config,
            start=start,
            annual_fundamentals=annual,
            raw_cache_profiles=raw_profiles,
        )
        financial_scenarios[identifier] = coverage
        for row in coverage["missing_financial_priorities"]:
            priority_rows.append({"scenario": identifier, **row})

    price_audit = audit_signal_price_coverage(
        snapshots,
        start,
        end,
        maximum_price_lag_days=7,
        minimum_lookback_rows=253,
        quarterly_fundamentals=quarterly,
        maximum_financial_age_days=max(
            config.maximum_financial_age_days for config in configs
        ),
        minimum_profit_growth=min(
            config.minimum_eps_growth for config in configs
        ),
        minimum_revenue_growth=min(
            config.minimum_revenue_growth for config in configs
        ),
        maximum_signal_snapshot_age_days=30,
        price_date_metadata=load_price_date_metadata(price_dir)[1],
    )
    price_priorities = pd.DataFrame(price_audit["pit_gap_priorities"])
    financial_priorities = pd.DataFrame(priority_rows)
    missing_financial_union = sorted({
        ticker
        for coverage in financial_scenarios.values()
        for ticker in coverage["missing_financial_symbols"]
    })
    snapshot_ready = bool(price_audit["signal_membership_snapshots_complete"])
    price_ready = bool(price_audit["complete"])
    financial_ready = all(
        coverage["complete"] for coverage in financial_scenarios.values()
    )
    summary = {
        "schema_version": 1,
        "research_only": True,
        "release_status": "BLOCKED",
        "requested_period": {"start": start, "end": end},
        "scenario_count": len(configs),
        "scenarios": {
            scenario_id(config): {
                "config": asdict(config),
                "coverage": financial_scenarios[scenario_id(config)],
            }
            for config in configs
        },
        "price_audit": price_audit,
        "missing_financial_symbol_union": missing_financial_union,
        "gates": {
            "signal_membership_snapshots_complete": snapshot_ready,
            "historical_member_prices_complete": price_ready,
            "all_candidate_financial_scenarios_complete": financial_ready,
            "adaptive_training_eligible": (
                snapshot_ready and price_ready and financial_ready
            ),
            "research_pretraining_allowed": True,
        },
        "input_bindings": {
            "quarterly": {
                "path": str(quarterly_path), "sha256": _sha256(quarterly_path)
            },
            "annual": (
                {"path": str(annual_path), "sha256": _sha256(annual_path)}
                if annual_path.exists() else None
            ),
            "companyfacts_cache_profile_count": len(raw_profiles),
            "supplemental_companyfacts_cache": (
                str(supplemental_cache_dir)
                if supplemental_cache_dir is not None else None
            ),
            "nasdaq_index": {
                "path": str(NASDAQ_INDEX_FILE),
                "sha256": _sha256(Path(NASDAQ_INDEX_FILE)),
            },
            "snapshot_dir": str(snapshot_dir),
            "snapshot_file_count": len(list(snapshot_dir.glob("nasdaq_listed_*.csv"))),
            "price_directory": _price_directory_binding(price_dir),
        },
        "interpretation_guardrail": (
            "Research pretraining may exercise the adaptive pipeline, but no "
            "parameter schedule may be frozen or promoted until the same "
            "predeclared fit is rerun with every required data gate passing."
        ),
    }
    return summary, financial_priorities, price_priorities


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2019-01-01")
    parser.add_argument("--end", default="2021-12-31")
    parser.add_argument("--snapshot-dir", type=Path, default=DEFAULT_SNAPSHOT_DIR)
    parser.add_argument("--quarterly", type=Path, default=DEFAULT_QUARTERLY)
    parser.add_argument(
        "--supplemental-cache-dir", type=Path,
        default=DEFAULT_SUPPLEMENTAL_CACHE,
    )
    parser.add_argument(
        "--price-dir", type=Path, default=Path(CLEANED_PRICE_DATA_DIR)
    )
    parser.add_argument("--prefix", type=Path, default=DEFAULT_PREFIX)
    args = parser.parse_args()
    summary, financial, prices = build_audit(
        start=args.start,
        end=args.end,
        snapshot_dir=args.snapshot_dir,
        quarterly_path=args.quarterly,
        supplemental_cache_dir=args.supplemental_cache_dir,
        price_dir=args.price_dir,
    )
    args.prefix.parent.mkdir(parents=True, exist_ok=True)
    financial_path = args.prefix.with_name(
        args.prefix.name + "_financial_priorities.csv"
    )
    price_path = args.prefix.with_name(
        args.prefix.name + "_price_priorities.csv"
    )
    summary_path = args.prefix.with_suffix(".json")
    financial.to_csv(financial_path, index=False)
    prices.to_csv(price_path, index=False)
    summary["outputs"] = {
        "financial_priorities": str(financial_path),
        "price_priorities": str(price_path),
        "summary": str(summary_path),
    }
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "summary": str(summary_path),
        "scenario_count": summary["scenario_count"],
        "missing_financial_symbol_count": len(
            summary["missing_financial_symbol_union"]
        ),
        "unresolved_price_competitor_count": len(
            summary["price_audit"][
                "unresolved_observable_potential_competitor_symbols"
            ]
        ),
        "gates": summary["gates"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
