#!/usr/bin/env python3
"""Develop capped inverse-volatility weights on frozen monthly Top-5 names.

The PIT universe, profitability filter, liquid pool, momentum ranking, Top-5
names and monthly trading dates remain unchanged.  Candidates only change the
weight exponent applied to preceding 63-day volatility, with a fixed 30%
single-name cap and full stock exposure in risk-on months.  Training years do
not count as final wins.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from scripts import research_v23_stock_only_frequency as v23
from scripts import research_v30_2019_selection_path_adjudication as v30
from scripts import research_v35_weekly_volatility_budget_development as v35
from scripts import research_v36_correlation_diversified_momentum as v36
from scripts import research_v39_risk_adjusted_momentum_development as v39
from src.conf import CLEANED_PRICE_DATA_DIR, NASDAQ_INDEX_FILE
from src.research.can_slim import replay_can_slim_target_schedule


DEVELOPMENT_START = v35.DEVELOPMENT_START
DEVELOPMENT_END = v35.DEVELOPMENT_END
DEVELOPMENT_YEARS = v35.DEVELOPMENT_YEARS
FINAL_COMPARISON_YEARS: tuple[int, ...] = ()
TRUE_PROSPECTIVE_START = "2026-08-31"
COSTS = v23.COSTS
INVERSE_VOLATILITY_POWERS = (0.5, 1.0, 1.5)
VOLATILITY_LOOKBACK_DAYS = 63
MINIMUM_VOLATILITY_OBSERVATIONS = 40
MAXIMUM_SINGLE_STOCK_WEIGHT = 0.30
FORBIDDEN_ETFS = v23.FORBIDDEN_ETFS

OUTPUT_DIR = Path(
    "output/research_only/v40/inverse_volatility_weights_20260830"
)
PROTOCOL_PATH = OUTPUT_DIR / "frozen_protocol.json"
DEVELOPMENT_OUTPUT_DIR = OUTPUT_DIR / "development_results"
V30_MANIFEST = v30.RESULT_OUTPUT_DIR / "manifest.json"
V30_TARGETS = v30.RESULT_OUTPUT_DIR / "selected_targets.csv"
V39_MANIFEST = v39.DEVELOPMENT_OUTPUT_DIR / "manifest.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _file_binding(path: Path) -> dict:
    path = Path(path)
    return {"path": str(path), "sha256": _sha256(path)}


def _directory_binding(path: Path, pattern: str) -> dict:
    path = Path(path)
    digest = hashlib.sha256()
    files = sorted(path.glob(pattern))
    for item in files:
        digest.update(item.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256(item).encode("ascii"))
        digest.update(b"\n")
    return {
        "path": str(path),
        "pattern": pattern,
        "file_count": len(files),
        "content_manifest_sha256": digest.hexdigest(),
    }


def candidate_specs() -> list[dict]:
    return [
        {
            "key": f"inverse_volatility_weight_power_{power:g}",
            "inverse_volatility_power": power,
            "volatility_lookback_trading_days": VOLATILITY_LOOKBACK_DAYS,
            "minimum_volatility_observations": MINIMUM_VOLATILITY_OBSERVATIONS,
            "maximum_single_stock_weight": MAXIMUM_SINGLE_STOCK_WEIGHT,
            "stock_selection": "frozen_monthly_top5",
            "signal_frequency": "monthly",
        }
        for power in INVERSE_VOLATILITY_POWERS
    ]


def _validate_sources() -> dict:
    v30_manifest = json.loads(V30_MANIFEST.read_text(encoding="utf-8"))
    if not v30_manifest["audit"]["strategy_selection_path_complete"]:
        raise RuntimeError("v30 base selection path changed")
    v39_manifest = json.loads(V39_MANIFEST.read_text(encoding="utf-8"))
    if v39_manifest["development_status"] != "BLOCKED":
        raise RuntimeError("v39 development status changed")
    return {
        "base_candidate": v30.SELECTED_CANDIDATE,
        "v39_risk_adjusted_ranking_rejected_before_observation": True,
        "2026_used_to_motivate_architecture": True,
        "2026_used_for_weight_power_selection": False,
    }


def freeze_protocol(path: Path = PROTOCOL_PATH) -> dict:
    path = Path(path)
    if path.exists():
        raise RuntimeError(f"v40 protocol will not be overwritten: {path}")
    runner = Path(__file__).resolve().relative_to(Path.cwd().resolve())
    protocol = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V40_INVERSE_VOLATILITY_WEIGHTS_PRECOMMITMENT",
        "status": "FROZEN_NOT_DEVELOPED",
        "frozen_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_diagnosis": _validate_sources(),
        "evaluation_boundary": {
            "development_start": DEVELOPMENT_START,
            "development_end": DEVELOPMENT_END,
            "training_years": list(DEVELOPMENT_YEARS),
            "training_years_excluded_from_final_comparison": True,
            "final_comparison_years": [],
            "2019_role": "base_selector_development_not_weight_power_training",
            "2026_used_for_parameter_selection": False,
            "architecture_isolated_from_2026": False,
            "parameter_isolated_from_2026": True,
            "true_prospective_start": TRUE_PROSPECTIVE_START,
        },
        "candidate_grid": candidate_specs(),
        "candidate_count": len(candidate_specs()),
        "weight_policy": {
            "names": "frozen v30 monthly Top-5",
            "raw_weight": "1 / trailing_63d_volatility ** power",
            "maximum_single_stock_weight": MAXIMUM_SINGLE_STOCK_WEIGHT,
            "gross_stock_exposure": 1.0,
            "volatility_data_cutoff": "signal close",
            "execution": "next trading close",
            "risk_off_asset": "CASH",
            "forbidden_index_etfs": sorted(FORBIDDEN_ETFS),
        },
        "cost_bps": list(COSTS),
        "benchmark": "NASDAQ_COMPOSITE",
        "training_eligibility_gates": {
            "positive_compounded_excess_at_30_and_50bps": True,
            "minimum_positive_training_year_rate_at_50bps": (
                v35.MINIMUM_TRAINING_WIN_RATE
            ),
            "maximum_drawdown_lag_percentage_points_at_50bps": (
                v35.MAXIMUM_DRAWDOWN_LAG * 100.0
            ),
            "maximum_absolute_drawdown_percentage_points_at_50bps": (
                v35.MAXIMUM_ABSOLUTE_DRAWDOWN * 100.0
            ),
            "label": "TRAINING_DIAGNOSTIC_NOT_FINAL_EVIDENCE",
        },
        "selection_order": [
            "training eligible first",
            "absolute drawdown at 50bps ascending",
            "drawdown lag at 50bps ascending",
            "worst annual training excess at 50bps descending",
            "compounded training excess at 50bps descending",
            "turnover at 50bps ascending",
            "candidate key ascending",
        ],
        "walk_forward_folds": [
            {"selection_years": list(range(2020, year)), "test_year": year}
            for year in range(2022, 2026)
        ],
        "input_bindings": {
            "runner": _file_binding(runner),
            "v30_manifest": _file_binding(V30_MANIFEST),
            "v30_targets": _file_binding(V30_TARGETS),
            "v35_evaluation_helpers": _file_binding(
                Path("scripts/research_v35_weekly_volatility_budget_development.py")
            ),
            "v36_input_helpers": _file_binding(
                Path("scripts/research_v36_correlation_diversified_momentum.py")
            ),
            "v39_manifest": _file_binding(V39_MANIFEST),
            "target_replay": _file_binding(Path("src/research/can_slim.py")),
            "price_directory": _directory_binding(
                Path(CLEANED_PRICE_DATA_DIR), "*.csv"
            ),
            "nasdaq_index": _file_binding(Path(NASDAQ_INDEX_FILE)),
        },
        "parameters_frozen_before_development": True,
        "contains_index_etf_holdings": False,
        "brokerage_or_trading_authorized": False,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(protocol, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {**protocol, "protocol": _file_binding(path)}


def _validated_protocol(path: Path) -> tuple[dict, str]:
    path = Path(path)
    protocol_sha = _sha256(path)
    protocol = json.loads(path.read_text(encoding="utf-8"))
    if protocol["status"] != "FROZEN_NOT_DEVELOPED":
        raise RuntimeError("v40 protocol status changed")
    if protocol["candidate_grid"] != candidate_specs():
        raise RuntimeError("v40 candidate grid changed")
    if protocol["source_diagnosis"] != _validate_sources():
        raise RuntimeError("v40 source diagnosis changed")
    for name, binding in protocol["input_bindings"].items():
        if "content_manifest_sha256" in binding:
            actual = _directory_binding(Path(binding["path"]), binding["pattern"])
            if actual != binding:
                raise RuntimeError(f"v40 directory binding changed for {name}")
        elif _sha256(Path(binding["path"])) != binding["sha256"]:
            raise RuntimeError(f"v40 file binding changed for {name}")
    return protocol, protocol_sha


def capped_inverse_volatility_weights(
    volatility: pd.Series,
    *,
    power: float,
    maximum_weight: float = MAXIMUM_SINGLE_STOCK_WEIGHT,
) -> pd.Series:
    if power <= 0.0:
        raise ValueError("inverse-volatility power must be positive")
    if not 0.0 < maximum_weight <= 1.0:
        raise ValueError("maximum weight must be in (0, 1]")
    vol = volatility.astype(float)
    if vol.empty or vol.isna().any() or vol.le(0.0).any():
        raise ValueError("volatility must be positive and complete")
    if len(vol) * maximum_weight < 1.0 - 1e-12:
        raise ValueError("maximum weight cannot support full investment")
    scores = vol.pow(-power)
    weights = pd.Series(0.0, index=vol.index)
    remaining = list(vol.index)
    remaining_weight = 1.0
    while remaining:
        allocation = scores.loc[remaining] / scores.loc[remaining].sum()
        allocation *= remaining_weight
        over = allocation.loc[allocation.gt(maximum_weight + 1e-12)].index.tolist()
        if not over:
            weights.loc[remaining] = allocation
            break
        for ticker in over:
            weights.loc[ticker] = maximum_weight
            remaining.remove(ticker)
            remaining_weight -= maximum_weight
    if abs(float(weights.sum()) - 1.0) > 1e-9:
        raise RuntimeError("capped inverse-volatility weights do not sum to one")
    return weights


def generate_weighted_targets(
    adjusted_close: pd.DataFrame,
    base_targets: pd.DataFrame,
    *,
    inverse_volatility_power: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    prices = adjusted_close.sort_index()
    returns = prices.pct_change(fill_method=None)
    base = base_targets.copy()
    base["effective_date"] = pd.to_datetime(
        base["effective_date"], errors="raise"
    ).dt.normalize()
    rows = []
    audits = []
    for effective_date, group in base.groupby("effective_date", sort=True):
        active = group.loc[group["ticker"].ne("__CASH__")]
        tickers = active["ticker"].astype(str).tolist()
        previous_position = int(prices.index.searchsorted(effective_date)) - 1
        if previous_position < 0:
            continue
        cutoff = pd.Timestamp(prices.index[previous_position])
        weights = pd.Series(dtype=float)
        volatility = pd.Series(dtype=float)
        if tickers:
            window = returns.loc[:cutoff, tickers].tail(VOLATILITY_LOOKBACK_DAYS)
            observations = window.notna().sum()
            volatility = window.std(ddof=1) * np.sqrt(252.0)
            valid = bool(
                observations.ge(MINIMUM_VOLATILITY_OBSERVATIONS).all()
                and volatility.notna().all()
                and volatility.gt(0.0).all()
            )
            if valid:
                weights = capped_inverse_volatility_weights(
                    volatility,
                    power=inverse_volatility_power,
                )
        audits.append({
            "effective_date": pd.Timestamp(effective_date),
            "volatility_cutoff_date": cutoff,
            "selected": tickers,
            "weighted": weights.index.astype(str).tolist(),
            "maximum_weight": float(weights.max()) if len(weights) else None,
            "minimum_weight": float(weights.min()) if len(weights) else None,
        })
        if weights.empty:
            rows.append({
                "effective_date": effective_date,
                "ticker": "__CASH__",
                "target_weight": 0.0,
                "base_transaction_cost_bps": 10.0,
            })
            continue
        for ticker, weight in weights.items():
            rows.append({
                "effective_date": effective_date,
                "ticker": str(ticker),
                "target_weight": float(weight),
                "base_transaction_cost_bps": 10.0,
            })
    targets = pd.DataFrame(rows)
    tickers = set(targets["ticker"].astype(str)) - {"__CASH__"}
    forbidden = sorted(tickers & FORBIDDEN_ETFS)
    if forbidden:
        raise RuntimeError(f"v40 selected forbidden ETFs: {forbidden}")
    return targets, pd.DataFrame(audits)


def develop(
    protocol_path: Path = PROTOCOL_PATH,
    output_dir: Path = DEVELOPMENT_OUTPUT_DIR,
) -> dict:
    protocol, protocol_sha = _validated_protocol(protocol_path)
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"v40 output will not be overwritten: {output_dir}")
    inputs = v36._load_inputs()
    base_targets = pd.read_csv(V30_TARGETS, parse_dates=["effective_date"])
    base_targets = base_targets.loc[
        base_targets["effective_date"].between(DEVELOPMENT_START, DEVELOPMENT_END)
    ].copy()
    results_by_candidate = {}
    targets_by_candidate = {}
    audits_by_candidate = {}
    summaries = {}
    for spec in protocol["candidate_grid"]:
        targets, audit = generate_weighted_targets(
            inputs["close"],
            base_targets,
            inverse_volatility_power=float(spec["inverse_volatility_power"]),
        )
        results = {}
        for cost in COSTS:
            stressed = targets.copy()
            stressed["base_transaction_cost_bps"] = float(cost)
            daily, _ = replay_can_slim_target_schedule(
                inputs["raw_close"],
                inputs["nasdaq"],
                stressed,
                DEVELOPMENT_START,
                DEVELOPMENT_END,
            )
            results[cost] = v35._canonicalize_result(daily, inputs["nasdaq"])
        results_by_candidate[spec["key"]] = results
        targets_by_candidate[spec["key"]] = targets
        audits_by_candidate[spec["key"]] = audit
        summaries[spec["key"]] = v35._summary(results)

    selected, ranking = v35.select_candidate(results_by_candidate)
    folds = []
    for fold in protocol["walk_forward_folds"]:
        years = tuple(int(year) for year in fold["selection_years"])
        fold_selected, fold_ranking = v35.select_candidate(
            results_by_candidate, years
        )
        ranked_candidate = fold_selected or fold_ranking[0]["candidate"]
        test_year = int(fold["test_year"])
        test = v35._period_metrics(
            results_by_candidate[ranked_candidate][50], (test_year,)
        )
        folds.append({
            **fold,
            "selected_candidate": ranked_candidate,
            "training_gates_passed": fold_selected is not None,
            "test_excess_vs_nasdaq_50bps": test[
                "compounded_excess_vs_nasdaq"
            ],
            "test_status": (
                "PASS"
                if test["compounded_excess_vs_nasdaq"] > 0.0
                else "BLOCKED"
            ),
            "final_evidence": False,
        })

    output_dir.mkdir(parents=True, exist_ok=True)
    ranking_path = output_dir / "candidate_ranking.csv"
    pd.DataFrame(ranking).to_csv(ranking_path, index=False)
    summaries_path = output_dir / "candidate_summaries.json"
    summaries_path.write_text(
        json.dumps(summaries, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    folds_path = output_dir / "walk_forward_training_diagnostics.csv"
    pd.DataFrame(folds).to_csv(folds_path, index=False)
    outputs = {
        "candidate_ranking": _file_binding(ranking_path),
        "candidate_summaries": _file_binding(summaries_path),
        "walk_forward_training_diagnostics": _file_binding(folds_path),
    }
    selected_spec = None
    selected_summary = None
    if selected is not None:
        selected_spec = next(
            spec for spec in protocol["candidate_grid"] if spec["key"] == selected
        )
        selected_summary = summaries[selected]
        targets_path = output_dir / "selected_targets.csv"
        targets_by_candidate[selected].to_csv(targets_path, index=False)
        outputs["selected_targets"] = _file_binding(targets_path)
        audit_path = output_dir / "selected_weight_audit.csv"
        audit_export = audits_by_candidate[selected].copy()
        audit_export["selected"] = audit_export["selected"].map(json.dumps)
        audit_export["weighted"] = audit_export["weighted"].map(json.dumps)
        audit_export.to_csv(audit_path, index=False)
        outputs["selected_weight_audit"] = _file_binding(audit_path)
        for cost in COSTS:
            path = output_dir / f"selected_daily_{cost}bps.csv"
            results_by_candidate[selected][cost].to_csv(path, index_label="date")
            outputs[f"selected_daily_{cost}bps"] = _file_binding(path)
    report = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V40_INVERSE_VOLATILITY_WEIGHTS_DEVELOPMENT_RESULT",
        "protocol": {"path": str(protocol_path), "sha256": protocol_sha},
        "development_status": "PASS" if selected is not None else "BLOCKED",
        "selected_candidate": selected,
        "selected_specification": selected_spec,
        "selected_summary": selected_summary,
        "evaluation_boundary": protocol["evaluation_boundary"],
        "training_ranking": ranking,
        "walk_forward_training_diagnostics": folds,
        "walk_forward_pass_count": sum(
            fold["test_status"] == "PASS" for fold in folds
        ),
        "all_walk_forward_folds_passed": bool(
            folds and all(fold["test_status"] == "PASS" for fold in folds)
        ),
        "training_years_counted_as_final_wins": False,
        "final_comparison_years": [],
        "contains_index_etf_holdings": False,
        "risk_off_asset": "CASH",
        "2026_used_for_parameter_selection": False,
        "outputs": outputs,
        "brokerage_or_trading_authorized": False,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {**report, "manifest": _file_binding(manifest_path)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    freeze_parser = subparsers.add_parser("freeze")
    freeze_parser.add_argument("--protocol", type=Path, default=PROTOCOL_PATH)
    develop_parser = subparsers.add_parser("develop")
    develop_parser.add_argument("--protocol", type=Path, default=PROTOCOL_PATH)
    develop_parser.add_argument(
        "--output-dir", type=Path, default=DEVELOPMENT_OUTPUT_DIR
    )
    args = parser.parse_args()
    report = (
        freeze_protocol(args.protocol)
        if args.command == "freeze"
        else develop(args.protocol, args.output_dir)
    )
    fields = (
        ("status", "candidate_count", "evaluation_boundary", "protocol")
        if args.command == "freeze"
        else (
            "development_status",
            "selected_candidate",
            "walk_forward_pass_count",
            "training_years_counted_as_final_wins",
            "release_status",
            "manifest",
        )
    )
    print(json.dumps({field: report[field] for field in fields}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
