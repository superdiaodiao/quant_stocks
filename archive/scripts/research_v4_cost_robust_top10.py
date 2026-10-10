"""Build a forward-only Top 10 challenger from the fixed cost screen.

The candidate was selected after inspecting historical diagnostics.  Those
diagnostics therefore cannot promote it; the artifact is only a configuration
freeze for a new, future shadow record.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
from pathlib import Path

import pandas as pd

from scripts.research_v3_fresh_top3 import _annual, _atomic_json, _replay, _sha256
from src.conf import (
    CLEANED_PRICE_DATA_DIR,
    NASDAQ_INDEX_FILE,
    POINT_IN_TIME_EPS_FILE,
    POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE,
    PROJECT_PATH,
)
from src.financial.eps import load_eps_history
from src.financial.quarterly_fundamentals import load_quarterly_fundamentals
from src.research.can_slim_data_audit import audit_selected_histories
from src.research.can_slim_validation import fixed_top3_config
from src.research.data_fingerprint import (
    build_data_manifest,
    can_slim_input_fingerprints,
)
from src.research.panel_data import load_panel
from src.research.universe_history import load_universe_snapshots


MODEL_VERSION = "can-slim-v4-cost-robust-top10-shadow"
FORWARD_START = "2026-08-10"
DEFAULT_PREFIX = Path(PROJECT_PATH) / "output/research_v4_cost_robust_top10"
DEFAULT_SUMMARY = (
    Path(PROJECT_PATH)
    / "output/data_provenance/research_v4_cost_robust_top10_2026-08-10.json"
)


def challenger_config():
    return replace(
        fixed_top3_config(),
        top_n=10,
        maximum_position_weight=0.1,
        maximum_financial_age_days=150,
        minimum_median_dollar_volume=10_000_000.0,
    )


def validate_selection_evidence(
    path: Path, quarterly_path: Path, config_id: int = 15
) -> dict:
    payload = json.loads(path.read_text())
    if payload.get("diagnostic_only") is not True:
        raise ValueError("candidate selection evidence must be diagnostic-only")
    if payload.get("promotion_eligible") is not False:
        raise ValueError("candidate selection evidence cannot be promotion eligible")
    if config_id not in payload.get("robust_candidate_ids", []):
        raise ValueError(f"candidate {config_id} did not pass every cost stress")
    if payload["quarterly_input"]["sha256"] != _sha256(quarterly_path):
        raise ValueError("candidate selection quarterly input does not match")
    expected = asdict(challenger_config())
    selected = payload["candidate_configs"][config_id]
    if selected != expected:
        raise ValueError("candidate selection configuration does not match v4 freeze")
    artifact = Path(payload["artifact"]["path"])
    if not artifact.is_file() or _sha256(artifact) != payload["artifact"]["sha256"]:
        raise ValueError("candidate selection artifact hash does not match")
    return payload


def run(
    prefix: Path,
    summary_path: Path,
    quarterly_path: Path,
    selection_path: Path,
) -> dict:
    config = challenger_config()
    selection = validate_selection_evidence(selection_path, quarterly_path)
    load_start = (pd.Timestamp(config.start) - pd.Timedelta(days=400)).strftime(
        "%Y-%m-%d"
    )
    close, dollar_volume = load_panel(
        CLEANED_PRICE_DATA_DIR, load_start, config.end
    )
    nasdaq = pd.read_csv(
        NASDAQ_INDEX_FILE, index_col="date", parse_dates=True
    )["close"]
    eps = load_eps_history(POINT_IN_TIME_EPS_FILE)
    quarterly = load_quarterly_fundamentals(quarterly_path)
    snapshots = load_universe_snapshots()

    result, ledger = _replay(
        config, close, dollar_volume, nasdaq, eps, quarterly, snapshots
    )
    annual = _annual(result).loc[2021:]
    cost_rows: list[dict] = []
    for cost_bps in (10.0, 30.0, 50.0):
        stressed, _ = _replay(
            replace(config, transaction_cost_bps=cost_bps),
            close,
            dollar_volume,
            nasdaq,
            eps,
            quarterly,
            snapshots,
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
    result.to_csv(paths["daily"])
    ledger.to_csv(paths["trade_ledger"], index=False)
    annual.reset_index(names="year").to_csv(paths["annual"], index=False)
    cost.to_csv(paths["cost_stress"], index=False)
    selected_ledger.to_csv(paths["selected_ledger"], index=False)
    _atomic_json(paths["selected_audit"], selected_audit)

    fingerprints = can_slim_input_fingerprints()
    fingerprints["quarterly_fundamentals"] = {
        **fingerprints["quarterly_fundamentals"],
        "path": str(quarterly_path.resolve()),
        "sha256": _sha256(quarterly_path),
        "bytes": quarterly_path.stat().st_size,
    }
    oos_diagnostic = annual.loc[2022:2026]
    cost_oos = cost[cost["year"].between(2022, 2026)]
    payload = {
        "format_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "model_version": MODEL_VERSION,
        "purpose": "forward_only_shadow_challenger",
        "research_only": True,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "historical_selection_contaminated": True,
        "historical_diagnostic": {
            "years": len(oos_diagnostic),
            "wins_vs_nasdaq": int(
                oos_diagnostic["excess_vs_nasdaq"].gt(0).sum()
            ),
            "minimum_excess_vs_nasdaq": float(
                oos_diagnostic["excess_vs_nasdaq"].min()
            ),
            "median_excess_vs_nasdaq": float(
                oos_diagnostic["excess_vs_nasdaq"].median()
            ),
            "cost_stress_wins": {
                str(int(cost_bps)): int(group["excess_vs_nasdaq"].gt(0).sum())
                for cost_bps, group in cost_oos.groupby("cost_bps")
            },
        },
        "configuration": asdict(config),
        "selection_evidence": {
            "path": str(selection_path.resolve()),
            "sha256": _sha256(selection_path),
            "candidate_id": 15,
            "screen_artifact_sha256": selection["artifact"]["sha256"],
        },
        "quarterly_input": {
            "path": str(quarterly_path.resolve()),
            "sha256": _sha256(quarterly_path),
            "is_formal_input": quarterly_path.resolve()
            == Path(POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE).resolve(),
        },
        "selected_data_audit": selected_audit,
        "data_manifest": build_data_manifest(fingerprints),
        "strategy_code_fingerprint": fingerprints["strategy_code"],
        "artifact_bindings": {
            name: {"path": str(path), "sha256": _sha256(path)}
            for name, path in paths.items()
        },
        "shadow_policy": {
            "forward_start": FORWARD_START,
            "minimum_forward_months": 12,
            "minimum_monthly_signal_observations": 12,
            "parameters_must_remain_unchanged": True,
            "data_manifest_must_remain_verifiable": True,
            "selected_price_and_terminal_data_must_remain_complete": True,
            "net_excess_at_30_bps_must_be_positive": True,
            "maximum_drawdown_must_not_exceed_40pct": True,
        },
        "promotion_blockers": [
            "historical_configuration_is_post_selection",
            "minimum_12_month_forward_shadow_not_observed",
        ],
    }
    _atomic_json(summary_path, payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build the cost-robust Top 10 forward-only challenger."
    )
    parser.add_argument("--prefix", type=Path, default=DEFAULT_PREFIX)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument(
        "--quarterly-input",
        type=Path,
        default=Path(POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE),
    )
    parser.add_argument("--selection-evidence", type=Path, required=True)
    args = parser.parse_args()
    payload = run(
        args.prefix, args.summary, args.quarterly_input, args.selection_evidence
    )
    print(
        json.dumps(
            {
                "model_version": payload["model_version"],
                "wins_vs_nasdaq": payload["historical_diagnostic"][
                    "wins_vs_nasdaq"
                ],
                "cost_stress_wins": payload["historical_diagnostic"][
                    "cost_stress_wins"
                ],
                "release_status": payload["release_status"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
