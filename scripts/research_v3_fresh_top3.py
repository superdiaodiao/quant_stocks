"""Build the forward-only fresh-financial Top 3 shadow challenger.

The configuration is economically predeclared rather than selected for its
historical score: concentrated leadership, the established $10M liquidity
floor, and a 150-day maximum PIT financial age.  Historical replay is a
diagnostic only; promotion requires a new 12-month shadow record.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.conf import (
    CLEANED_PRICE_DATA_DIR,
    NASDAQ_INDEX_FILE,
    POINT_IN_TIME_EPS_FILE,
    POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE,
    PROJECT_PATH,
)
from src.financial.eps import load_eps_history
from src.financial.quarterly_fundamentals import load_quarterly_fundamentals
from src.io.security_identity import issuer_rename_transitions
from src.research.can_slim import calculate_can_slim_returns_with_ledger
from src.research.can_slim_data_audit import audit_selected_histories
from src.research.can_slim_validation import fixed_top3_config
from src.research.data_fingerprint import (
    build_data_manifest,
    can_slim_input_fingerprints,
)
from src.research.data_quality import back_adjust_common_splits
from src.research.panel_data import load_panel
from src.research.universe_history import load_universe_snapshots, universe_as_of


MODEL_VERSION = "can-slim-v3-fresh-top3-shadow"
FORWARD_START = "2026-08-10"
MINIMUM_FORWARD_MONTHS = 12
DEFAULT_PREFIX = Path(PROJECT_PATH) / "output/research_v3_fresh_top3"
DEFAULT_SUMMARY = Path(PROJECT_PATH) / (
    "output/data_provenance/research_v3_fresh_top3_2026-08-10.json"
)


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def challenger_config():
    return replace(
        fixed_top3_config(),
        maximum_financial_age_days=150,
    )


def _annual(result: pd.DataFrame) -> pd.DataFrame:
    annual = (1 + result[["strategy", "benchmark"]]).groupby(
        result.index.year
    ).prod() - 1
    annual["excess_vs_nasdaq"] = annual["strategy"] - annual["benchmark"]
    annual.index.name = "year"
    return annual


def _replay(config, close, dollar_volume, nasdaq, eps, quarterly, snapshots):
    adjusted = back_adjust_common_splits(close).sort_index()
    return calculate_can_slim_returns_with_ledger(
        adjusted,
        dollar_volume,
        nasdaq,
        eps,
        config,
        lambda value: universe_as_of(snapshots, value),
        quarterly,
        adjust_splits=False,
        eligibility_close=close,
        identity_transitions=issuer_rename_transitions(),
    )


def run(
    prefix: Path,
    summary_path: Path,
    quarterly_path: Path = Path(
        POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE
    ),
) -> dict:
    config = challenger_config()
    load_start = (pd.Timestamp(config.start) - pd.Timedelta(days=400)).strftime(
        "%Y-%m-%d"
    )
    close, dollar_volume = load_panel(CLEANED_PRICE_DATA_DIR, load_start, config.end)
    nasdaq = pd.read_csv(
        NASDAQ_INDEX_FILE, index_col="date", parse_dates=True
    )["close"]
    eps = load_eps_history(POINT_IN_TIME_EPS_FILE)
    quarterly_path = Path(quarterly_path)
    quarterly = load_quarterly_fundamentals(quarterly_path)
    snapshots = load_universe_snapshots()

    result, ledger = _replay(
        config, close, dollar_volume, nasdaq, eps, quarterly, snapshots
    )
    annual = _annual(result).loc[2021:]
    cost_rows = []
    for cost_bps in (10.0, 30.0, 50.0):
        stressed_config = replace(config, transaction_cost_bps=cost_bps)
        stressed, _ = (
            (result, ledger)
            if cost_bps == config.transaction_cost_bps
            else _replay(
                stressed_config,
                close,
                dollar_volume,
                nasdaq,
                eps,
                quarterly,
                snapshots,
            )
        )
        for year, row in _annual(stressed).loc[2021:].iterrows():
            cost_rows.append(
                {
                    "cost_bps": cost_bps,
                    "year": int(year),
                    "strategy": float(row.strategy),
                    "nasdaq": float(row.benchmark),
                    "excess_vs_nasdaq": float(row.excess_vs_nasdaq),
                }
            )
    cost = pd.DataFrame(cost_rows)
    selected_ledger, selected_audit = audit_selected_histories(
        fixed_config=config,
        quarterly_path=quarterly_path,
    )

    paths = {
        "daily": prefix.with_name(prefix.name + "_daily.csv"),
        "trade_ledger": prefix.with_name(prefix.name + "_trade_ledger.csv"),
        "annual": prefix.with_name(prefix.name + "_annual.csv"),
        "cost_stress": prefix.with_name(prefix.name + "_cost_stress.csv"),
        "selected_audit": prefix.with_name(prefix.name + "_selected_audit.json"),
        "selected_ledger": prefix.with_name(prefix.name + "_selected_ledger.csv"),
    }
    prefix.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(paths["daily"], index_label="date")
    ledger.to_csv(paths["trade_ledger"], index=False)
    annual.reset_index().to_csv(paths["annual"], index=False)
    cost.to_csv(paths["cost_stress"], index=False)
    _atomic_json(paths["selected_audit"], selected_audit)
    selected_ledger.to_csv(paths["selected_ledger"], index=False)

    wins = int(annual["excess_vs_nasdaq"].gt(0).sum())
    cost_wins = {
        str(int(cost_bps)): int(group["excess_vs_nasdaq"].gt(0).sum())
        for cost_bps, group in cost.groupby("cost_bps")
    }
    fingerprints = can_slim_input_fingerprints()
    fingerprints["quarterly_fundamentals"] = {
        **fingerprints["quarterly_fundamentals"],
        "path": str(quarterly_path.resolve()),
        "sha256": _sha256(quarterly_path),
        "bytes": quarterly_path.stat().st_size,
    }
    is_formal_input = quarterly_path.resolve() == Path(
        POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE
    ).resolve()
    payload = {
        "format_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "model_version": (
            MODEL_VERSION
            if is_formal_input
            else f"{MODEL_VERSION}-data-sensitivity"
        ),
        "purpose": (
            "forward_only_shadow_challenger"
            if is_formal_input
            else "historical_data_sensitivity"
        ),
        "research_only": True,
        "release_status": "BLOCKED",
        "formal_validation_rerun": False,
        "formal_v1_modified": False,
        "quarterly_input": {
            "path": str(quarterly_path.resolve()),
            "sha256": _sha256(quarterly_path),
            "is_formal_input": is_formal_input,
        },
        "configuration": asdict(config),
        "rationale": {
            "concentrated_leadership": "retain the CAN SLIM Top 3 hypothesis",
            "liquidity_floor": 10_000_000.0,
            "maximum_financial_age_days": 150,
            "selection_mode": "growth",
        },
        "historical_diagnostic": {
            "in_sample_contaminated": True,
            "eligible_for_promotion": False,
            "years": int(len(annual)),
            "wins_vs_nasdaq": wins,
            "median_excess_vs_nasdaq": float(
                annual["excess_vs_nasdaq"].median()
            ),
            "minimum_excess_vs_nasdaq": float(
                annual["excess_vs_nasdaq"].min()
            ),
            "cost_stress_wins": cost_wins,
        },
        "selected_data_audit": selected_audit,
        "shadow_policy": {
            "forward_start": FORWARD_START,
            "minimum_forward_months": MINIMUM_FORWARD_MONTHS,
            "minimum_monthly_signal_observations": 12,
            "parameters_must_remain_unchanged": True,
            "data_manifest_must_remain_verifiable": True,
            "net_excess_at_30_bps_must_be_positive": True,
            "maximum_drawdown_must_not_exceed_40pct": True,
            "selected_price_and_terminal_data_must_remain_complete": True,
        },
        "promotion_eligible": False,
        "promotion_blockers": [
            "historical_result_is_post_selection_diagnostic",
            "minimum_12_month_forward_shadow_not_observed",
        ],
        "data_manifest": build_data_manifest(fingerprints),
        "strategy_code_fingerprint": fingerprints["strategy_code"],
        "artifact_bindings": {
            name: {"path": str(path), "sha256": _sha256(path)}
            for name, path in paths.items()
        },
    }
    _atomic_json(summary_path, payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, default=DEFAULT_PREFIX)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument(
        "--quarterly-input",
        type=Path,
        default=Path(POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE),
        help=(
            "Research-only quarterly PIT input override. Its SHA replaces "
            "the formal quarterly component in the generated data manifest."
        ),
    )
    args = parser.parse_args()
    payload = run(args.prefix, args.summary, args.quarterly_input)
    print(json.dumps(payload["historical_diagnostic"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
