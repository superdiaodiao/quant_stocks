"""Compare two quarterly-fundamentals versions under the frozen policy.

All non-quarterly inputs are held at their current project versions.  This is
a research diagnostic: it never replaces the formal fundamentals file or
changes the frozen model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from src.conf import (
    CLEANED_PRICE_DATA_DIR,
    NASDAQ_INDEX_FILE,
    POINT_IN_TIME_EPS_FILE,
    POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE,
)
from src.financial.eps import load_eps_history
from src.financial.quarterly_fundamentals import (
    load_quarterly_fundamentals,
)
from src.research.can_slim import calculate_can_slim_returns_with_ledger
from src.research.can_slim_validation import fixed_top3_config
from src.research.data_quality import back_adjust_common_splits
from src.research.panel_data import load_panel
from src.research.universe_history import (
    load_universe_snapshots,
    universe_as_of,
)


DEFAULT_SUMMARY_OUTPUT = Path(
    "output/can_slim_quarterly_data_version_impact_summary.json"
)
DEFAULT_ANNUAL_OUTPUT = Path(
    "output/can_slim_quarterly_data_version_impact_annual.csv"
)
DEFAULT_SIGNAL_OUTPUT = Path(
    "output/can_slim_quarterly_data_version_impact_signals.csv"
)

SEMANTIC_FACT_COLUMNS = [
    "ticker",
    "fiscal_end",
    "available_date",
    "metric",
    "value",
    "taxonomy",
    "concept",
    "form",
    "accession",
]
FACT_KEY_COLUMNS = [
    "ticker",
    "fiscal_end",
    "available_date",
    "metric",
]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def quarterly_inventory(frame: pd.DataFrame) -> dict:
    """Return semantic fact inventory independent of fetch timestamps."""
    semantic = frame.reindex(columns=SEMANTIC_FACT_COLUMNS).drop_duplicates()
    keys = frame.reindex(columns=FACT_KEY_COLUMNS).drop_duplicates()
    return {
        "rows": int(len(frame)),
        "tickers": int(frame["ticker"].nunique()),
        "semantic_fact_rows": int(len(semantic)),
        "fact_keys": int(len(keys)),
    }


def quarterly_inventory_diff(
    reference: pd.DataFrame,
    candidate: pd.DataFrame,
) -> dict:
    """Count semantic rows and PIT fact keys added or removed."""
    reference_semantic = reference.reindex(
        columns=SEMANTIC_FACT_COLUMNS
    ).drop_duplicates()
    candidate_semantic = candidate.reindex(
        columns=SEMANTIC_FACT_COLUMNS
    ).drop_duplicates()
    semantic = reference_semantic.merge(
        candidate_semantic,
        on=SEMANTIC_FACT_COLUMNS,
        how="outer",
        indicator=True,
    )
    reference_keys = reference.reindex(
        columns=FACT_KEY_COLUMNS
    ).drop_duplicates()
    candidate_keys = candidate.reindex(
        columns=FACT_KEY_COLUMNS
    ).drop_duplicates()
    keys = reference_keys.merge(
        candidate_keys,
        on=FACT_KEY_COLUMNS,
        how="outer",
        indicator=True,
    )
    return {
        "reference": quarterly_inventory(reference),
        "candidate": quarterly_inventory(candidate),
        "candidate_only_semantic_fact_rows": int(
            semantic["_merge"].eq("right_only").sum()
        ),
        "reference_only_semantic_fact_rows": int(
            semantic["_merge"].eq("left_only").sum()
        ),
        "candidate_only_fact_keys": int(
            keys["_merge"].eq("right_only").sum()
        ),
        "reference_only_fact_keys": int(
            keys["_merge"].eq("left_only").sum()
        ),
        "candidate_only_tickers": sorted(
            set(candidate["ticker"]) - set(reference["ticker"])
        ),
        "reference_only_tickers": sorted(
            set(reference["ticker"]) - set(candidate["ticker"])
        ),
    }


def _annual(result: pd.DataFrame) -> pd.DataFrame:
    annual = (
        (1 + result[["strategy", "benchmark"]])
        .groupby(result.index.year)
        .prod()
        - 1
    )
    annual["excess_vs_nasdaq"] = (
        annual["strategy"] - annual["benchmark"]
    )
    annual.index.name = "year"
    return annual


def changed_target_signals(
    reference_ledger: pd.DataFrame,
    candidate_ledger: pd.DataFrame,
    *,
    start: str = "2021-01-01",
) -> pd.DataFrame:
    """Reconstruct and compare positive targets after each ledger signal."""
    ledgers = {
        "reference": reference_ledger.copy(),
        "candidate": candidate_ledger.copy(),
    }
    groups = {}
    signal_dates = set()
    for name, ledger in ledgers.items():
        ledger["signal_date"] = pd.to_datetime(
            ledger["signal_date"], errors="coerce"
        ).dt.normalize()
        ledger = ledger.dropna(subset=["signal_date"])
        groups[name] = {
            signal: group
            for signal, group in ledger.groupby("signal_date", sort=True)
        }
        signal_dates.update(groups[name])

    states = {"reference": {}, "candidate": {}}
    rows = []
    for signal_date in sorted(signal_dates):
        for name in states:
            group = groups[name].get(signal_date)
            if group is None:
                continue
            for row in group.itertuples(index=False):
                states[name][str(row.ticker)] = float(
                    row.target_weight_after
                )
        if signal_date < pd.Timestamp(start):
            continue
        targets = {
            name: sorted(
                ticker
                for ticker, weight in state.items()
                if weight > 0
            )
            for name, state in states.items()
        }
        if set(targets["reference"]) == set(targets["candidate"]):
            continue
        rows.append({
            "signal_date": signal_date.strftime("%Y-%m-%d"),
            "reference_tickers": "|".join(targets["reference"]),
            "candidate_tickers": "|".join(targets["candidate"]),
            "removed_tickers": "|".join(sorted(
                set(targets["reference"]) - set(targets["candidate"])
            )),
            "added_tickers": "|".join(sorted(
                set(targets["candidate"]) - set(targets["reference"])
            )),
        })
    return pd.DataFrame(rows, columns=[
        "signal_date",
        "reference_tickers",
        "candidate_tickers",
        "removed_tickers",
        "added_tickers",
    ])


def run_quarterly_data_version_impact(
    reference_path: Path,
    candidate_path: Path = Path(
        POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE
    ),
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Replay both versions while fixing every other project input."""
    reference_path = Path(reference_path)
    candidate_path = Path(candidate_path)
    reference = load_quarterly_fundamentals(reference_path)
    candidate = load_quarterly_fundamentals(candidate_path)
    config = fixed_top3_config()
    load_start = (
        pd.Timestamp(config.start) - pd.Timedelta(days=400)
    ).strftime("%Y-%m-%d")
    close, dollar_volume = load_panel(
        CLEANED_PRICE_DATA_DIR, load_start, config.end
    )
    adjusted_close = back_adjust_common_splits(close).sort_index()
    nasdaq = pd.read_csv(
        NASDAQ_INDEX_FILE, index_col="date", parse_dates=True
    )["close"]
    eps = load_eps_history(POINT_IN_TIME_EPS_FILE)
    snapshots = load_universe_snapshots()
    universe = lambda value: universe_as_of(snapshots, value)

    results = {}
    ledgers = {}
    for name, quarterly in (
        ("reference", reference),
        ("candidate", candidate),
    ):
        results[name], ledgers[name] = (
            calculate_can_slim_returns_with_ledger(
                adjusted_close,
                dollar_volume,
                nasdaq,
                eps,
                config,
                universe,
                quarterly,
                adjust_splits=False,
                eligibility_close=close,
            )
        )

    annual = pd.concat(
        {
            name: _annual(result).loc[2021:]
            for name, result in results.items()
        },
        axis=1,
    )
    annual.columns = [
        f"{scenario}_{metric}"
        for scenario, metric in annual.columns
    ]
    annual["strategy_delta"] = (
        annual["candidate_strategy"] - annual["reference_strategy"]
    )
    annual["excess_delta"] = (
        annual["candidate_excess_vs_nasdaq"]
        - annual["reference_excess_vs_nasdaq"]
    )
    annual = annual.reset_index()
    signals = changed_target_signals(
        ledgers["reference"], ledgers["candidate"]
    )
    inventory = quarterly_inventory_diff(reference, candidate)
    summary = {
        "purpose": "quarterly_data_version_impact",
        "comparison_design": (
            "Only the quarterly fundamentals file changes; current prices, "
            "Nasdaq index, universe snapshots, EPS, frozen selector, execution "
            "timing, and 10 bps one-way cost are held fixed."
        ),
        "formal_fundamentals_modified_by_this_run": False,
        "formal_model_changed": False,
        "reference_path": str(reference_path),
        "candidate_path": str(candidate_path),
        "reference_sha256": _file_sha256(reference_path),
        "candidate_sha256": _file_sha256(candidate_path),
        "inventory_basis": (
            "Frames after load_quarterly_fundamentals point-in-time ticker "
            "identity normalization; fetched_at is excluded from semantic "
            "fact comparisons."
        ),
        "inventory": inventory,
        "reference_wins_vs_nasdaq": int(
            annual["reference_excess_vs_nasdaq"].gt(0).sum()
        ),
        "candidate_wins_vs_nasdaq": int(
            annual["candidate_excess_vs_nasdaq"].gt(0).sum()
        ),
        "changed_target_signal_count": int(len(signals)),
        "changed_target_signal_dates": signals[
            "signal_date"
        ].tolist(),
        "reference_final_portfolio_value": float(
            results["reference"]["portfolio_value"].iloc[-1]
        ),
        "candidate_final_portfolio_value": float(
            results["candidate"]["portfolio_value"].iloc[-1]
        ),
    }
    return annual, signals, summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-quarterly", type=Path, required=True)
    parser.add_argument(
        "--candidate-quarterly",
        type=Path,
        default=Path(POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE),
    )
    parser.add_argument(
        "--summary-output", type=Path, default=DEFAULT_SUMMARY_OUTPUT
    )
    parser.add_argument(
        "--annual-output", type=Path, default=DEFAULT_ANNUAL_OUTPUT
    )
    parser.add_argument(
        "--signal-output", type=Path, default=DEFAULT_SIGNAL_OUTPUT
    )
    args = parser.parse_args()
    annual, signals, summary = run_quarterly_data_version_impact(
        args.reference_quarterly,
        args.candidate_quarterly,
    )
    for path in (
        args.summary_output,
        args.annual_output,
        args.signal_output,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
    annual.to_csv(args.annual_output, index=False)
    signals.to_csv(args.signal_output, index=False)
    args.summary_output.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
