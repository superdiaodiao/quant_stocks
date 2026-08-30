#!/usr/bin/env python3
"""Correct v47 data/risk defects before the first prospective signal.

This is a single fixed repair, not a new threshold search.  It keeps v47's
Top-5 selector and 20/25 percent risk thresholds while replacing inferred
corporate actions with sourced events, applying the minimum-price gate to
contemporaneous nominal prices, making stops effective before a coincident
monthly rebalance, rejecting missing entry closes, enforcing same-UTC-date
SIGNAL universe staging, and storing portable repository-relative bindings.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

import pandas as pd

from scripts import research_v26_large_liquid_stock_momentum as v26
from scripts import research_v29_recovered_2019_stock_momentum as v29
from scripts import research_v30_2019_selection_path_adjudication as v30
from scripts import research_v33_portfolio_stop_development as v33
from scripts import research_v42_prospective_v28_observation as v42
from scripts import research_v43_isolated_prospective_v28_observation as v43
from scripts import research_v46_entry_loss_stop_development as v46
from scripts import research_v47_hybrid_entry_portfolio_stop as v47
from scripts import research_v48_isolated_prospective_v47_observation as v48
from src.research.corrected_stock_policy import (
    VALIDATION_PATH,
    corrected_price_views,
    large_liquid_ranking,
    load_corporate_action_validation,
    replay_with_sourced_hybrid_stop,
)
from src.research.universe_history import universe_as_of


MODEL_VERSION = "v50r1-corrected-v47-sourced-actions"
DEVELOPMENT_START = v47.DEVELOPMENT_START
DEVELOPMENT_END = v47.DEVELOPMENT_END
DEVELOPMENT_YEARS = v47.DEVELOPMENT_YEARS
COSTS = v47.COSTS
ENTRY_LOSS_FRACTION = v47.ENTRY_LOSS_FRACTION
PORTFOLIO_TRAILING_STOP_FRACTION = v47.PORTFOLIO_TRAILING_STOP_FRACTION
FIRST_PROSPECTIVE_SIGNAL_DATE = v48.FIRST_PROSPECTIVE_SIGNAL_DATE
REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = Path("output/research_only/v50/corrected_v47_20260831_r1")
DEVELOPMENT_PROTOCOL_PATH = OUTPUT_DIR / "development_protocol.json"
DEVELOPMENT_OUTPUT_DIR = OUTPUT_DIR / "development_results"
PROTOCOL_PATH = OUTPUT_DIR / "frozen_protocol.json"
LEDGER_PATH = OUTPUT_DIR / "prospective_ledger.jsonl"
SIGNALS_DIR = OUTPUT_DIR / "signals"
BUNDLES_DIR = OUTPUT_DIR / "bundles"
WORK_DIR = OUTPUT_DIR / "staging_work"
V48_SUPERSESSION_PATH = v48.OUTPUT_DIR / "superseded_by_v50.json"
V30_TARGETS = v30.RESULT_OUTPUT_DIR / "selected_targets.csv"
V43_VALIDATED_BUNDLE = v43._validated_bundle


def _resolve_path(path: str | Path) -> Path:
    item = Path(path)
    return item if item.is_absolute() else REPO_ROOT / item


def _sha256(path: str | Path) -> str:
    return hashlib.sha256(_resolve_path(path).read_bytes()).hexdigest()


def _portable_path(path: str | Path) -> str:
    resolved = _resolve_path(path).resolve()
    try:
        return resolved.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(resolved)


def _file_binding(path: str | Path) -> dict:
    return {"path": _portable_path(path), "sha256": _sha256(path)}


def _git_head() -> str:
    import subprocess

    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def correction_specification() -> dict:
    return {
        "selector": "v30 fixed Top-5 monthly selector",
        "risk_thresholds": {
            "entry_loss_fraction": ENTRY_LOSS_FRACTION,
            "portfolio_trailing_stop_fraction": (
                PORTFOLIO_TRAILING_STOP_FRACTION
            ),
        },
        "price_return_units": "sourced confirmed corporate actions only",
        "minimum_price_units": "contemporaneous nominal price",
        "unresolved_ranked_pool_action": "fail_closed",
        "unresolved_target_action": "fail_closed",
        "coincident_stop_and_rebalance": "stop_vetoes_same_close_reentry",
        "missing_monthly_entry_close": "fail_closed",
        "signal_universe_staging": "same_utc_date_as_signal_only",
        "input_binding_location": "repository_relative",
        "new_threshold_search": False,
    }


def _zero_signal_v48_events() -> list[dict]:
    events = v43.read_ledger(v48.LEDGER_PATH)
    if [event["event_type"] for event in events] != ["PROTOCOL_FROZEN"]:
        raise RuntimeError("v48 can only be repaired before its first signal")
    return events


def _development_bindings() -> dict:
    return {
        "runner": _file_binding(__file__),
        "corrected_policy": _file_binding(
            "src/research/corrected_stock_policy.py"
        ),
        "v30_protocol": _file_binding(v30.PROTOCOL_PATH),
        "v30_manifest": _file_binding(v30.RESULT_OUTPUT_DIR / "manifest.json"),
        "v30_targets": _file_binding(V30_TARGETS),
        "v47_protocol": _file_binding(v47.PROTOCOL_PATH),
        "v47_manifest": _file_binding(
            v47.DEVELOPMENT_OUTPUT_DIR / "manifest.json"
        ),
        "corporate_action_validation": _file_binding(VALIDATION_PATH),
        "reviewed_market_moves": _file_binding(
            "stocks_list_dir/nasdaq/reviewed_market_moves.csv"
        ),
    }


def freeze_development_protocol(
    path: str | Path = DEVELOPMENT_PROTOCOL_PATH,
) -> dict:
    item = Path(path)
    if item.exists():
        raise RuntimeError(f"v50 development protocol will not be overwritten: {item}")
    _zero_signal_v48_events()
    protocol = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V50_CORRECTED_V47_PRECOMMITMENT",
        "status": "FROZEN_NOT_DEVELOPED",
        "frozen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "objective": (
            "Repair externally identified data and stop-execution defects "
            "without searching a new selector or risk threshold."
        ),
        "correction_specification": correction_specification(),
        "development_window": {
            "start": DEVELOPMENT_START,
            "end": DEVELOPMENT_END,
            "years": list(DEVELOPMENT_YEARS),
        },
        "cost_bps": list(COSTS),
        "benchmark": "NASDAQ_COMPOSITE",
        "candidate_count": 1,
        "input_bindings": _development_bindings(),
        "v48_signal_count_at_freeze": 0,
        "2026_used_for_threshold_selection": False,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
    }
    item.parent.mkdir(parents=True, exist_ok=True)
    with item.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(protocol, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return {**protocol, "protocol": _file_binding(item)}


def _validated_development_protocol(
    path: str | Path = DEVELOPMENT_PROTOCOL_PATH,
) -> tuple[dict, str]:
    item = Path(path)
    protocol = json.loads(item.read_text(encoding="utf-8"))
    if protocol.get("status") != "FROZEN_NOT_DEVELOPED":
        raise RuntimeError("unexpected v50 development protocol status")
    if protocol.get("correction_specification") != correction_specification():
        raise RuntimeError("v50 correction specification changed")
    if protocol.get("input_bindings") != _development_bindings():
        raise RuntimeError("v50 development input binding changed")
    if protocol.get("release_status") != "BLOCKED" or protocol.get(
        "promotion_eligible"
    ):
        raise RuntimeError("v50 development release boundary changed")
    return protocol, _sha256(item)


def _load_corrected_inputs() -> dict:
    inputs = v29._load_inputs()
    validation = load_corporate_action_validation()
    continuous, eligibility = corrected_price_views(
        inputs["raw_close"], validation
    )
    inputs["close"] = continuous
    inputs["eligibility_close"] = eligibility
    inputs["corporate_action_validation"] = validation
    inputs["technical_cache"] = {}
    inputs["quality_cache"] = {}
    inputs["large_liquid_cache"] = {}
    return inputs


@contextmanager
def _corrected_selector_runtime():
    original = v26._large_liquid_ranking
    try:
        v26._large_liquid_ranking = large_liquid_ranking
        yield
    finally:
        v26._large_liquid_ranking = original


def _corrected_targets_and_inputs() -> tuple[dict, pd.DataFrame, dict]:
    inputs = _load_corrected_inputs()
    spec = v30.selected_specification()
    snapshots = v30.normalize_meta_identity(
        v29.load_repaired_universe_snapshots()
    )
    with _corrected_selector_runtime():
        audit, adjudicated = v30.audit_gap_selection_path(
            inputs, snapshots, spec
        )
    if audit.get("status") != "PASS":
        raise RuntimeError("v50 2019 selection-path adjudication failed")
    universe_cache: dict[pd.Timestamp, set[str] | None] = {}

    def universe(signal_date):
        stamp = pd.Timestamp(signal_date).normalize()
        if stamp in adjudicated:
            return adjudicated[stamp] - v29.FORBIDDEN_ETFS
        if stamp not in universe_cache:
            symbols = universe_as_of(
                snapshots,
                stamp,
                maximum_age_days=v29.MAXIMUM_SNAPSHOT_AGE_DAYS,
            )
            universe_cache[stamp] = (
                None
                if symbols is None
                else set(symbols) - v29.FORBIDDEN_ETFS
            )
        return universe_cache[stamp]

    inputs["universe"] = universe
    inputs["technical_cache"] = {}
    inputs["quality_cache"] = {}
    inputs["large_liquid_cache"] = {}
    with _corrected_selector_runtime():
        _base_results, targets = v26._generate_candidate(spec, inputs)
    return inputs, targets, audit


def _target_difference(
    left: pd.DataFrame, right: pd.DataFrame
) -> pd.DataFrame:
    keys = ["effective_date", "ticker", "target_weight"]
    first = left[keys].copy()
    second = right[keys].copy()
    first["effective_date"] = pd.to_datetime(first["effective_date"])
    second["effective_date"] = pd.to_datetime(second["effective_date"])
    return first.merge(second, on=keys, how="outer", indicator=True).loc[
        lambda frame: frame["_merge"].ne("both")
    ]


def develop(
    protocol_path: str | Path = DEVELOPMENT_PROTOCOL_PATH,
    output_dir: str | Path = DEVELOPMENT_OUTPUT_DIR,
) -> dict:
    _protocol, protocol_sha = _validated_development_protocol(protocol_path)
    output = Path(output_dir)
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"v50 development output will not be overwritten: {output}")
    inputs, targets, selection_audit = _corrected_targets_and_inputs()
    legacy_targets = pd.read_csv(V30_TARGETS, parse_dates=["effective_date"])
    differences = _target_difference(legacy_targets, targets)
    target_schedule_unchanged = differences.empty
    if not target_schedule_unchanged:
        raise RuntimeError(
            "Corrected price policy changed the frozen selector target path"
        )
    targets = targets.loc[
        targets["effective_date"].between(DEVELOPMENT_START, DEVELOPMENT_END)
    ].copy()
    validation = inputs["corporate_action_validation"]
    results = {}
    for cost in COSTS:
        daily = replay_with_sourced_hybrid_stop(
            inputs["raw_close"],
            inputs["nasdaq"],
            targets,
            DEVELOPMENT_START,
            DEVELOPMENT_END,
            validation=validation,
            entry_loss_fraction=ENTRY_LOSS_FRACTION,
            portfolio_stop_fraction=PORTFOLIO_TRAILING_STOP_FRACTION,
            transaction_cost_bps=float(cost),
        )
        results[cost] = v33._canonicalize_result(
            daily, inputs["nasdaq"], DEVELOPMENT_START, DEVELOPMENT_END
        )
    summary = v33._summary(results)
    annual_50 = summary["costs"]["50"]["annual_training_diagnostics"]
    positive_years = sum(row["excess_vs_nasdaq"] > 0.0 for row in annual_50)
    metrics_50 = summary["costs"]["50"]
    baseline_drawdown = v46._baseline_drawdown_50bps()
    folds = []
    for year in range(2022, 2026):
        metrics = v33._period_metrics(results[50], (year,))
        folds.append(
            {
                "test_year": year,
                "test_excess_vs_nasdaq_50bps": metrics[
                    "compounded_excess_vs_nasdaq"
                ],
                "test_status": (
                    "PASS"
                    if metrics["compounded_excess_vs_nasdaq"] > 0.0
                    else "BLOCKED"
                ),
                "final_evidence": False,
            }
        )
    gates = {
        "corrected_target_schedule_unchanged": target_schedule_unchanged,
        "selection_path_complete": selection_audit["strategy_selection_path_complete"],
        "positive_each_training_year_at_50bps": positive_years
        == len(DEVELOPMENT_YEARS),
        "positive_compounded_excess_at_30bps": summary["costs"]["30"][
            "compounded_excess_vs_nasdaq"
        ]
        > 0.0,
        "positive_compounded_excess_at_50bps": metrics_50[
            "compounded_excess_vs_nasdaq"
        ]
        > 0.0,
        "absolute_drawdown_at_50bps_no_worse_than_baseline": abs(
            metrics_50["strategy_maximum_drawdown"]
        )
        <= baseline_drawdown + 1e-12,
        "all_expanding_test_years_positive": all(
            fold["test_status"] == "PASS" for fold in folds
        ),
    }
    passed = all(gates.values())
    output.mkdir(parents=True, exist_ok=True)
    summary_path = output / "candidate_summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    targets_path = output / "corrected_targets.csv"
    targets.to_csv(targets_path, index=False)
    difference_path = output / "target_differences.csv"
    differences.to_csv(difference_path, index=False)
    folds_path = output / "walk_forward_training_diagnostics.csv"
    pd.DataFrame(folds).to_csv(folds_path, index=False)
    outputs = {
        "candidate_summary": _file_binding(summary_path),
        "corrected_targets": _file_binding(targets_path),
        "target_differences": _file_binding(difference_path),
        "walk_forward_training_diagnostics": _file_binding(folds_path),
    }
    for cost in COSTS:
        daily_path = output / f"selected_daily_{cost}bps.csv"
        results[cost].to_csv(daily_path, index_label="date")
        outputs[f"selected_daily_{cost}bps"] = _file_binding(daily_path)
    report = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V50_CORRECTED_V47_DEVELOPMENT_RESULT",
        "protocol": {
            "path": _portable_path(protocol_path),
            "sha256": protocol_sha,
        },
        "development_status": "PASS" if passed else "BLOCKED",
        "correction_specification": correction_specification(),
        "candidate_summary": summary,
        "gates": gates,
        "positive_training_years_50bps": positive_years,
        "strategy_drawdown_50bps": abs(
            metrics_50["strategy_maximum_drawdown"]
        ),
        "maximum_allowed_drawdown_50bps": baseline_drawdown,
        "walk_forward_training_diagnostics": folds,
        "target_difference_count": len(differences),
        "research_forward_observation_ready": passed,
        "training_years_counted_as_final_wins": False,
        "2026_used_for_threshold_selection": False,
        "outputs": outputs,
        "brokerage_or_trading_authorized": False,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {**report, "manifest": _file_binding(manifest_path)}


def _development_manifest() -> dict:
    path = DEVELOPMENT_OUTPUT_DIR / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("development_status") != "PASS" or not manifest.get(
        "research_forward_observation_ready"
    ):
        raise RuntimeError("v50 corrected development gates did not pass")
    if not all(manifest.get("gates", {}).values()):
        raise RuntimeError("v50 corrected development manifest has a failed gate")
    return manifest


def _selected_model() -> dict:
    model = dict(v48._selected_model())
    model["correction_specification"] = correction_specification()
    return model


def freeze_protocol(
    path: str | Path = PROTOCOL_PATH,
    ledger_path: str | Path = LEDGER_PATH,
) -> dict:
    item = Path(path)
    ledger = Path(ledger_path)
    if item.exists() or ledger.exists():
        raise RuntimeError("v50 protocol/ledger will not be overwritten")
    _zero_signal_v48_events()
    manifest = _development_manifest()
    protocol = {
        "schema_version": 3,
        "research_only": True,
        "model_version": MODEL_VERSION,
        "status": "FROZEN_WAITING_FOR_FIRST_SIGNAL",
        "frozen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "code_commit": _git_head(),
        "supersedes": {
            "model_version": v48.MODEL_VERSION,
            "protocol": _file_binding(v48.PROTOCOL_PATH),
            "ledger": _file_binding(v48.LEDGER_PATH),
            "v48_signal_count": 0,
            "reason": [
                "legacy integer-ratio split inference could erase real crashes",
                "historical minimum-price eligibility used future-adjusted units",
                "stop checks were skipped before a monthly rebalance",
                "late SIGNAL staging could use a future universe",
                "frozen source bindings were checkout-absolute",
            ],
        },
        "model": _selected_model(),
        "evidence_partition": {
            "2020_2025": {
                "role": "CORRECTED_TRAINING_DIAGNOSTIC_ONLY",
                "years": list(DEVELOPMENT_YEARS),
                "counts_as_official_comparison": False,
                "official_year_wins": 0,
            },
            "2026_01_07": {
                "role": "RESEARCHER_EXPOSED_REUSED_DIAGNOSTIC",
                "counts_as_official_comparison": False,
                "official_year_wins": 0,
            },
            "prospective": {
                "first_signal_date": FIRST_PROSPECTIVE_SIGNAL_DATE.strftime(
                    "%Y-%m-%d"
                ),
                "counts_as_official_comparison": True,
            },
        },
        "signal_policy": {
            "frequency": "completed calendar-month final Nasdaq session",
            "execution": "next common trading-session close",
            "universe_staging": "same UTC date as signal",
            "late_signal_bundle_allowed": False,
        },
        "risk_policy": {
            "entry_loss_fraction": ENTRY_LOSS_FRACTION,
            "entry_reference": "adjusted close at latest monthly rebalance",
            "portfolio_trailing_stop_fraction": (
                PORTFOLIO_TRAILING_STOP_FRACTION
            ),
            "portfolio_peak_reset": "latest monthly rebalance",
            "stop_checked_before_rebalance": True,
            "coincident_stop_precedence": "stop_vetoes_same_close_reentry",
            "missing_entry_close": "fail_closed",
        },
        "price_policy": {
            "automatic_heuristic_adjustment_allowed": False,
            "confirmed_actions_only": True,
            "reviewed_market_moves_preserved": True,
            "unresolved_rank_or_target_event": "fail_closed",
        },
        "evaluation": {
            "primary_benchmark": "NASDAQ_COMPOSITE_PRICE_RETURN",
            "secondary_benchmark": "QQQ_TOTAL_RETURN_REFERENCE_ONLY",
            "transaction_cost_bps": list(COSTS),
            "training_years_are_never_counted_as_wins": True,
            "official_score_requires_complete_prospective_periods": True,
        },
        "immutability": {
            "protocol_overwrite_allowed": False,
            "signal_overwrite_allowed": False,
            "ledger_mode": "append_only_sha256_chain",
            "input_binding_location": "repository_relative",
        },
        "input_bindings": {
            "runner": _file_binding(__file__),
            "corrected_policy": _file_binding(
                "src/research/corrected_stock_policy.py"
            ),
            "development_protocol": _file_binding(DEVELOPMENT_PROTOCOL_PATH),
            "development_manifest": _file_binding(
                DEVELOPMENT_OUTPUT_DIR / "manifest.json"
            ),
            "v43_runtime_core": _file_binding(v43.__file__),
            "v42_calculation_core": _file_binding(v42.__file__),
            "v48_protocol": _file_binding(v48.PROTOCOL_PATH),
            "v48_ledger": _file_binding(v48.LEDGER_PATH),
            "corporate_action_validation": _file_binding(VALIDATION_PATH),
            "reviewed_market_moves": _file_binding(
                "stocks_list_dir/nasdaq/reviewed_market_moves.csv"
            ),
        },
        "corrected_training_positive_years_50bps": manifest[
            "positive_training_years_50bps"
        ],
        "parameters_frozen_before_prospective_data": True,
        "broker_connection_used": False,
        "order_created": False,
        "capital_allocated": False,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
    }
    item.parent.mkdir(parents=True, exist_ok=True)
    with item.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(protocol, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    protocol_sha = _sha256(item)
    v43.append_event(
        path=ledger,
        protocol_sha256=protocol_sha,
        event_type="PROTOCOL_FROZEN",
        payload={
            "protocol_path": _portable_path(item),
            "protocol_sha256": protocol_sha,
            "first_prospective_signal_date": (
                FIRST_PROSPECTIVE_SIGNAL_DATE.strftime("%Y-%m-%d")
            ),
            "official_training_year_wins": 0,
            "superseded_v48_signal_count": 0,
        },
    )
    return {**protocol, "protocol": _file_binding(item), "ledger": str(ledger)}


def write_v48_supersession(
    *,
    path: str | Path = V48_SUPERSESSION_PATH,
    successor_protocol: str | Path = PROTOCOL_PATH,
) -> dict:
    item = Path(path)
    if item.exists():
        raise RuntimeError("v48 supersession record will not be overwritten")
    _zero_signal_v48_events()
    record = {
        "schema_version": 1,
        "status": "SUPERSEDED_BEFORE_FIRST_SIGNAL",
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "v48_protocol": _file_binding(v48.PROTOCOL_PATH),
        "v48_ledger": _file_binding(v48.LEDGER_PATH),
        "v48_signal_count": 0,
        "successor_protocol": _file_binding(successor_protocol),
        "reason": (
            "source-locked price, eligibility, stop-precedence, timeliness, "
            "and portable-binding defects were repaired before any signal"
        ),
    }
    item.parent.mkdir(parents=True, exist_ok=True)
    with item.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return {**record, "record": _file_binding(item)}


def _validated_protocol(
    path: str | Path = PROTOCOL_PATH,
) -> tuple[dict, str]:
    item = Path(path)
    protocol = json.loads(item.read_text(encoding="utf-8"))
    protocol_sha = _sha256(item)
    if protocol.get("model_version") != MODEL_VERSION:
        raise RuntimeError("unexpected v50 model version")
    if protocol.get("model") != _selected_model():
        raise RuntimeError("v50 frozen model binding changed")
    if protocol.get("release_status") != "BLOCKED" or protocol.get(
        "promotion_eligible"
    ):
        raise RuntimeError("v50 release boundary changed")
    for name, binding in protocol["input_bindings"].items():
        if _sha256(binding["path"]) != binding["sha256"]:
            raise RuntimeError(f"v50 frozen input changed: {name}")
    return protocol, protocol_sha


def _validated_bundle(
    bundle: str | Path, expected_purpose: str | None = None
) -> tuple[dict, str]:
    manifest, manifest_sha = V43_VALIDATED_BUNDLE(bundle, expected_purpose)
    if manifest.get("runner_version") != MODEL_VERSION:
        raise RuntimeError("bundle was not frozen by the v50 runner")
    if manifest.get("purpose") == "SIGNAL":
        created_at = pd.Timestamp(manifest["created_at"])
        if created_at.tzinfo is None:
            raise RuntimeError("v50 SIGNAL bundle created_at lacks a timezone")
        if created_at.tz_convert("UTC").date() != pd.Timestamp(
            manifest["as_of"]
        ).date():
            raise RuntimeError(
                "v50 SIGNAL bundle was staged after its declared UTC date"
            )
    return manifest, manifest_sha


def _build_signal_payload(
    *,
    signal_date: pd.Timestamp,
    bundle: Path,
    protocol: dict,
    protocol_sha: str,
    manifest_sha: str,
) -> dict:
    inputs = v42._load_signal_inputs(bundle, signal_date)
    validation = load_corporate_action_validation()
    continuous, eligibility = corrected_price_views(
        inputs["raw_close"], validation
    )
    inputs["close"] = continuous
    inputs["eligibility_close"] = eligibility
    inputs["corporate_action_validation"] = validation
    inputs["technical_cache"] = {}
    inputs["quality_cache"] = {}
    inputs["large_liquid_cache"] = {}
    payload = v42.build_signal_payload(
        signal_date=signal_date,
        inputs=inputs,
        model=protocol["model"],
        protocol_sha256=protocol_sha,
        bundle_manifest_sha256=manifest_sha,
        ranking_function=large_liquid_ranking,
    )
    payload["model_version"] = MODEL_VERSION
    payload["corrected_price_policy_verified"] = True
    payload["signal_staging_timeliness_verified"] = True
    return payload


@contextmanager
def _v43_runtime():
    replacements = {
        "MODEL_VERSION": MODEL_VERSION,
        "_validated_protocol": _validated_protocol,
        "_validated_bundle": _validated_bundle,
        "_build_signal_payload": _build_signal_payload,
    }
    original = {name: getattr(v43, name) for name in replacements}
    try:
        for name, value in replacements.items():
            setattr(v43, name, value)
        yield
    finally:
        for name, value in original.items():
            setattr(v43, name, value)


def _hybrid_replay_adapter(
    raw_close: pd.DataFrame,
    index_close: pd.Series,
    target_schedule: pd.DataFrame,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    trailing_stop_fraction: float,
    transaction_cost_bps: float,
) -> pd.DataFrame:
    if trailing_stop_fraction != PORTFOLIO_TRAILING_STOP_FRACTION:
        raise RuntimeError("v50 portfolio-stop interface binding changed")
    return replay_with_sourced_hybrid_stop(
        raw_close,
        index_close,
        target_schedule,
        start,
        end,
        validation=load_corporate_action_validation(),
        entry_loss_fraction=ENTRY_LOSS_FRACTION,
        portfolio_stop_fraction=PORTFOLIO_TRAILING_STOP_FRACTION,
        transaction_cost_bps=transaction_cost_bps,
    )


def _signal_staging_is_timely(
    stamp: str | pd.Timestamp,
    observed_at: datetime | None = None,
) -> bool:
    observed = observed_at or datetime.now(timezone.utc)
    if observed.tzinfo is None:
        raise ValueError("signal staging timestamp must be timezone-aware")
    return observed.astimezone(timezone.utc).date() == pd.Timestamp(
        stamp
    ).date()


def stage_bundle(*, observed_at: datetime | None = None, **kwargs) -> dict:
    purpose = str(kwargs.get("purpose", "")).upper()
    stamp = pd.Timestamp(kwargs.get("as_of")).normalize()
    kwargs.setdefault("bundles_dir", BUNDLES_DIR)
    kwargs.setdefault("work_dir", WORK_DIR)
    kwargs.setdefault("signals_dir", SIGNALS_DIR)
    kwargs.setdefault("ledger_path", LEDGER_PATH)
    existing = Path(kwargs["bundles_dir"]) / f"{stamp:%Y-%m-%d}_{purpose.lower()}"
    if purpose == "SIGNAL" and not existing.exists():
        if not _signal_staging_is_timely(stamp, observed_at):
            raise RuntimeError(
                "v50 refuses late SIGNAL staging with a current universe"
            )
    with _v43_runtime():
        return v43.stage_bundle(**kwargs)


def freeze_signal(
    *,
    bundle: str | Path,
    protocol_path: str | Path = PROTOCOL_PATH,
    ledger_path: str | Path = LEDGER_PATH,
    signals_dir: str | Path = SIGNALS_DIR,
) -> dict:
    with _v43_runtime():
        return v43.freeze_signal(
            bundle=bundle,
            protocol_path=protocol_path,
            ledger_path=ledger_path,
            signals_dir=signals_dir,
        )


def append_mark(
    *,
    bundle: str | Path,
    protocol_path: str | Path = PROTOCOL_PATH,
    ledger_path: str | Path = LEDGER_PATH,
    signals_dir: str | Path = SIGNALS_DIR,
) -> dict:
    original_replay = v42.v28.replay_with_individual_trailing_stop
    try:
        v42.v28.replay_with_individual_trailing_stop = _hybrid_replay_adapter
        with _v43_runtime():
            result = v43.append_mark(
                bundle=bundle,
                protocol_path=protocol_path,
                ledger_path=ledger_path,
                signals_dir=signals_dir,
            )
    finally:
        v42.v28.replay_with_individual_trailing_stop = original_replay
    result["corrected_hybrid_risk_replay_verified"] = True
    return result


def status(
    *,
    protocol_path: str | Path = PROTOCOL_PATH,
    ledger_path: str | Path = LEDGER_PATH,
) -> dict:
    with _v43_runtime():
        result = v43.status(
            protocol_path=protocol_path,
            ledger_path=ledger_path,
        )
    result["supersedes_v48_before_first_signal"] = True
    result["corrected_price_policy"] = "SOURCED_ACTIONS_ONLY"
    result["late_signal_bundle_allowed"] = False
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("freeze-development")
    subparsers.add_parser("develop")
    subparsers.add_parser("freeze-protocol")
    subparsers.add_parser("write-v48-supersession")
    stage_parser = subparsers.add_parser("stage-bundle")
    stage_parser.add_argument("--as-of", required=True)
    stage_parser.add_argument("--purpose", choices=["SIGNAL", "MARK"], required=True)
    stage_parser.add_argument("--workers", type=int, default=16)
    stage_parser.add_argument("--fundamental-workers", type=int, default=4)
    signal_parser = subparsers.add_parser("freeze-signal")
    signal_parser.add_argument("--bundle", type=Path, required=True)
    mark_parser = subparsers.add_parser("append-mark")
    mark_parser.add_argument("--bundle", type=Path, required=True)
    subparsers.add_parser("status")
    args = parser.parse_args()
    if args.command == "freeze-development":
        result = freeze_development_protocol()
    elif args.command == "develop":
        result = develop()
    elif args.command == "freeze-protocol":
        result = freeze_protocol()
    elif args.command == "write-v48-supersession":
        result = write_v48_supersession()
    elif args.command == "stage-bundle":
        result = stage_bundle(
            as_of=args.as_of,
            purpose=args.purpose,
            workers=args.workers,
            fundamental_workers=args.fundamental_workers,
        )
    elif args.command == "freeze-signal":
        result = freeze_signal(bundle=args.bundle)
    elif args.command == "append-mark":
        result = append_mark(bundle=args.bundle)
    else:
        result = status()
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
