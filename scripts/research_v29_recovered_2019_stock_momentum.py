#!/usr/bin/env python3
"""Re-run the stock-only v26 grid with recovered 2019 PIT snapshots.

This version changes only the point-in-time universe input.  It adds four
source-locked v14 snapshots that were never wired into v26, keeps the 40-day
freshness rule, and leaves the four still-unresolved 2019 month-end signals in
cash.  Frozen v26-v28 artifacts are not modified.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts import research_v23_stock_only_frequency as v23
from scripts import research_v26_large_liquid_stock_momentum as v26
from src.research.universe_history import (
    load_universe_snapshots,
    snapshot_directory,
    universe_as_of,
)


DEVELOPMENT_START = v26.DEVELOPMENT_START
DEVELOPMENT_END = v26.DEVELOPMENT_END
DEVELOPMENT_YEARS = v26.DEVELOPMENT_YEARS
OBSERVATION_START = v26.OBSERVATION_START
COSTS = v26.COSTS
FORBIDDEN_ETFS = v26.FORBIDDEN_ETFS
MAXIMUM_SNAPSHOT_AGE_DAYS = v26.v24.MAXIMUM_SNAPSHOT_AGE_DAYS

RECOVERED_SNAPSHOT_DIRECTORY = Path(
    "output/research_only/v14/universe_snapshots"
)
RECOVERED_2019_DATES = (
    "2019-02-22",
    "2019-07-15",
    "2019-10-02",
    "2019-12-31",
)
RECOVERY_MANIFESTS = (
    RECOVERED_SNAPSHOT_DIRECTORY / "github_recovery_manifest.json",
    RECOVERED_SNAPSHOT_DIRECTORY / "github_pinned_gap_recovery_manifest.json",
    RECOVERED_SNAPSHOT_DIRECTORY / "nasdaq_trader_file_import_manifest.json",
)
SIGNAL_DATES_2019 = tuple(pd.to_datetime([
    "2019-01-31",
    "2019-02-28",
    "2019-03-29",
    "2019-04-30",
    "2019-05-31",
    "2019-06-28",
    "2019-07-31",
    "2019-08-30",
    "2019-09-30",
    "2019-10-31",
    "2019-11-29",
    "2019-12-31",
]))
EXPECTED_USABLE_2019_SIGNALS = (
    "2019-01-31",
    "2019-02-28",
    "2019-03-29",
    "2019-06-28",
    "2019-07-31",
    "2019-10-31",
    "2019-11-29",
    "2019-12-31",
)
EXPECTED_MISSING_2019_SIGNALS = (
    "2019-04-30",
    "2019-05-31",
    "2019-08-30",
    "2019-09-30",
)

OUTPUT_DIR = Path(
    "output/research_only/v29/recovered_2019_stock_momentum_20260830"
)
PROTOCOL_PATH = OUTPUT_DIR / "frozen_protocol.json"
DEVELOPMENT_OUTPUT_DIR = OUTPUT_DIR / "development_results"


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _file_binding(path: Path) -> dict:
    path = Path(path)
    return {"path": str(path), "sha256": _sha256(path)}


def _directory_binding(path: Path, pattern: str) -> dict:
    return v26._directory_binding(Path(path), pattern)


def candidate_specs() -> list[dict]:
    return v26.candidate_specs()


def recovered_snapshot_paths() -> dict[pd.Timestamp, Path]:
    return {
        pd.Timestamp(date): RECOVERED_SNAPSHOT_DIRECTORY
        / f"nasdaq_listed_{date}.csv"
        for date in RECOVERED_2019_DATES
    }


def load_repaired_universe_snapshots() -> dict[pd.Timestamp, set[str]]:
    """Add only the four predeclared v14 dates to the formal PIT history."""
    formal = load_universe_snapshots()
    # Direct security labels in each recovered file are used independently.
    # This prevents any other v14 snapshot from altering the selected dates.
    recovered = load_universe_snapshots(
        RECOVERED_SNAPSHOT_DIRECTORY,
        carry_forward_confirmed_types=False,
    )
    for stamp, path in recovered_snapshot_paths().items():
        if not path.exists() or stamp not in recovered:
            raise RuntimeError(f"missing recovered snapshot: {path}")
        if stamp in formal and formal[stamp] != recovered[stamp]:
            raise RuntimeError(f"recovered snapshot conflicts with formal date: {stamp}")
        formal[stamp] = recovered[stamp]
    return dict(sorted(formal.items()))


def coverage_adjudication() -> dict:
    formal_dates = {
        pd.Timestamp(path.stem.removeprefix("nasdaq_listed_"))
        for path in snapshot_directory().glob("nasdaq_listed_*.csv")
    }
    dates = formal_dates | set(recovered_snapshot_paths())
    usable = []
    missing = []
    evidence = []
    for signal in SIGNAL_DATES_2019:
        latest = max((stamp for stamp in dates if stamp <= signal), default=None)
        age = None if latest is None else int((signal - latest).days)
        row = {
            "signal_date": signal.strftime("%Y-%m-%d"),
            "snapshot_date": None if latest is None else latest.strftime("%Y-%m-%d"),
            "snapshot_age_days": age,
        }
        evidence.append(row)
        if age is not None and age <= MAXIMUM_SNAPSHOT_AGE_DAYS:
            usable.append(row["signal_date"])
        else:
            missing.append(row["signal_date"])
    if tuple(usable) != EXPECTED_USABLE_2019_SIGNALS:
        raise RuntimeError(f"unexpected usable 2019 signals: {usable}")
    if tuple(missing) != EXPECTED_MISSING_2019_SIGNALS:
        raise RuntimeError(f"unexpected missing 2019 signals: {missing}")
    return {
        "maximum_snapshot_age_days": MAXIMUM_SNAPSHOT_AGE_DAYS,
        "usable_signal_count": len(usable),
        "expected_signal_count": len(SIGNAL_DATES_2019),
        "usable_signal_dates": usable,
        "missing_signal_dates": missing,
        "missing_universe_policy": "CASH_NO_BACKFILL_NO_STALE_EXTENSION",
        "evidence": evidence,
    }


def freeze_protocol(path: Path = PROTOCOL_PATH) -> dict:
    path = Path(path)
    if path.exists():
        raise RuntimeError(f"v29 protocol will not be overwritten: {path}")
    runner = Path(__file__).resolve().relative_to(Path.cwd().resolve())
    recovered_bindings = {
        f"recovered_snapshot_{stamp.strftime('%Y%m%d')}": _file_binding(snapshot)
        for stamp, snapshot in recovered_snapshot_paths().items()
    }
    manifest_bindings = {
        f"recovery_manifest_{index}": _file_binding(manifest)
        for index, manifest in enumerate(RECOVERY_MANIFESTS, start=1)
    }
    protocol = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V29_RECOVERED_2019_STOCK_MOMENTUM_PRECOMMITMENT",
        "status": "FROZEN_NOT_DEVELOPED",
        "frozen_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "objective": (
            "Measure the isolated effect of wiring source-locked 2019 PIT "
            "snapshots into the frozen v26 stock-only candidate grid."
        ),
        "development_window": {
            "start": DEVELOPMENT_START,
            "end": DEVELOPMENT_END,
            "years": list(DEVELOPMENT_YEARS),
        },
        "reserved_observation_start": OBSERVATION_START,
        "candidate_grid": candidate_specs(),
        "candidate_count": len(candidate_specs()),
        "coverage_adjudication": coverage_adjudication(),
        "change_isolation": {
            "base_grid": "v26",
            "only_added_snapshot_dates": list(RECOVERED_2019_DATES),
            "formal_snapshot_files_modified": False,
            "unresolved_signal_policy": "CASH",
        },
        "cost_bps": list(COSTS),
        "primary_benchmark": "QQQ_TOTAL_RETURN",
        "secondary_benchmark": "NASDAQ_COMPOSITE",
        "selection_order": [
            "eligible first",
            "annual wins versus QQQ at 50bps descending",
            "worst annual excess versus QQQ at 50bps descending",
            "compounded excess versus QQQ at 50bps descending",
            "turnover at 50bps ascending",
            "candidate key ascending",
        ],
        "walk_forward_folds": [
            {"selection_years": list(range(2019, year)), "test_year": year}
            for year in range(2022, 2026)
        ],
        "input_bindings": {
            "runner": _file_binding(runner),
            "v23_evaluation_helpers": _file_binding(
                Path("scripts/research_v23_stock_only_frequency.py")
            ),
            "v24_signal_helpers": _file_binding(
                Path("scripts/research_v24_stock_momentum_development.py")
            ),
            "v26_candidate_helpers": _file_binding(
                Path("scripts/research_v26_large_liquid_stock_momentum.py")
            ),
            "universe_history": _file_binding(Path("src/research/universe_history.py")),
            "formal_universe_snapshots": _directory_binding(
                snapshot_directory(), "nasdaq_listed_*.csv"
            ),
            **recovered_bindings,
            **manifest_bindings,
            "price_directory": _directory_binding(
                Path(v26.CLEANED_PRICE_DATA_DIR), "*.csv"
            ),
            "nasdaq_index": _file_binding(Path(v26.NASDAQ_INDEX_FILE)),
            "qqq_history": _file_binding(Path(v26.v24.v15.QQQ_HISTORY["path"])),
            "quarterly_fundamentals": _file_binding(
                Path(v26.POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE)
            ),
        },
        "parameters_frozen_before_development": True,
        "2026_used_for_development_or_selection": False,
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
        raise RuntimeError("v29 protocol status changed")
    if protocol["candidate_grid"] != candidate_specs():
        raise RuntimeError("v29 candidate grid changed")
    if protocol["coverage_adjudication"] != coverage_adjudication():
        raise RuntimeError("v29 coverage adjudication changed")
    if protocol["release_status"] != "BLOCKED" or protocol["promotion_eligible"]:
        raise RuntimeError("v29 release boundary changed")
    for name, binding in protocol["input_bindings"].items():
        if "content_manifest_sha256" in binding:
            actual = _directory_binding(Path(binding["path"]), binding["pattern"])
            if actual != binding:
                raise RuntimeError(f"v29 directory binding changed for {name}")
        elif _sha256(Path(binding["path"])) != binding["sha256"]:
            raise RuntimeError(f"v29 file binding changed for {name}")
    return protocol, protocol_sha


def _load_inputs() -> dict:
    inputs = v26._load_inputs()
    snapshots = load_repaired_universe_snapshots()
    universe_cache: dict[pd.Timestamp, set[str] | None] = {}

    def universe(signal_date):
        stamp = pd.Timestamp(signal_date).normalize()
        if stamp not in universe_cache:
            symbols = universe_as_of(
                snapshots,
                stamp,
                maximum_age_days=MAXIMUM_SNAPSHOT_AGE_DAYS,
            )
            universe_cache[stamp] = (
                None if symbols is None else set(symbols) - FORBIDDEN_ETFS
            )
        return universe_cache[stamp]

    inputs["universe"] = universe
    inputs["technical_cache"] = {}
    inputs["quality_cache"] = {}
    inputs["large_liquid_cache"] = {}
    return inputs


def _generate_candidate(spec: dict, inputs: dict):
    return v26._generate_candidate(spec, inputs)


def develop(
    protocol_path: Path = PROTOCOL_PATH,
    output_dir: Path = DEVELOPMENT_OUTPUT_DIR,
) -> dict:
    protocol, protocol_sha = _validated_protocol(protocol_path)
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"v29 development output will not be overwritten: {output_dir}")
    inputs = _load_inputs()
    results_by_candidate = {}
    targets_by_candidate = {}
    summaries = {}
    for spec in protocol["candidate_grid"]:
        key = spec["key"]
        results, targets = _generate_candidate(spec, inputs)
        results_by_candidate[key] = results
        targets_by_candidate[key] = targets
        summaries[key] = v23._summary(results)

    selected, ranking = v23.select_candidate(summaries)
    folds = []
    for fold in protocol["walk_forward_folds"]:
        years = tuple(int(year) for year in fold["selection_years"])
        fold_selected, fold_ranking = v23.select_candidate(summaries, years)
        test_year = int(fold["test_year"])
        ranked_candidate = fold_selected or fold_ranking[0]["candidate"]
        test = next(
            row for row in summaries[ranked_candidate]["costs"]["50"]["annual"]
            if int(row["year"]) == test_year
        )
        folds.append({
            **fold,
            "selected_candidate": ranked_candidate,
            "training_gates_passed": fold_selected is not None,
            "test_excess_vs_qqq_50bps": float(test["excess_vs_qqq"]),
            "test_status": "PASS" if float(test["excess_vs_qqq"]) > 0.0 else "BLOCKED",
        })

    output_dir.mkdir(parents=True, exist_ok=True)
    ranking_path = output_dir / "candidate_ranking.csv"
    pd.DataFrame(ranking).to_csv(ranking_path, index=False)
    summary_path = output_dir / "candidate_summaries.json"
    summary_path.write_text(
        json.dumps(summaries, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    folds_path = output_dir / "walk_forward_folds.csv"
    pd.DataFrame(folds).to_csv(folds_path, index=False)
    outputs = {
        "candidate_ranking": _file_binding(ranking_path),
        "candidate_summaries": _file_binding(summary_path),
        "walk_forward_folds": _file_binding(folds_path),
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
        for cost in COSTS:
            result_path = output_dir / f"selected_daily_{cost}bps.csv"
            results_by_candidate[selected][cost].to_csv(result_path, index_label="date")
            outputs[f"selected_daily_{cost}bps"] = _file_binding(result_path)

    report = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V29_RECOVERED_2019_STOCK_MOMENTUM_RESULT",
        "protocol": {"path": str(protocol_path), "sha256": protocol_sha},
        "development_status": "PASS" if selected is not None else "BLOCKED",
        "coverage_adjudication": protocol["coverage_adjudication"],
        "selected_candidate": selected,
        "selected_specification": selected_spec,
        "selected_summary": selected_summary,
        "walk_forward_folds": folds,
        "walk_forward_pass_count": sum(
            fold["test_status"] == "PASS" for fold in folds
        ),
        "all_walk_forward_folds_passed": bool(
            folds and all(fold["test_status"] == "PASS" for fold in folds)
        ),
        "contains_index_etf_holdings": False,
        "risk_off_asset": "CASH",
        "2026_used_for_development_or_selection": False,
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
        ("status", "candidate_count", "coverage_adjudication", "protocol")
        if args.command == "freeze"
        else (
            "development_status",
            "selected_candidate",
            "walk_forward_pass_count",
            "release_status",
            "manifest",
        )
    )
    print(json.dumps({field: report[field] for field in fields}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
