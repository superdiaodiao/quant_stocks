"""Measure the in-memory impact of safe foreign-quarter candidates.

No production input is written.  The report compares the frozen strategy and
candidate-financial coverage before and after temporarily appending only
payloads that pass the strict foreign-quarter diagnostic.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from src.conf import (
    CLEANED_PRICE_DATA_DIR,
    NASDAQ_INDEX_FILE,
    POINT_IN_TIME_EPS_FILE,
    POINT_IN_TIME_FUNDAMENTALS_FILE,
    POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE,
)
from src.financial.eps import load_eps_history
from src.financial.quarterly_fundamentals import load_quarterly_fundamentals
from src.io.fundamentals_update import (
    SEC_COMPANYFACTS_CACHE_DIR,
    _companyfacts_cache_files,
    _read_companyfacts_cache_envelope,
    cached_companyfacts_symbol_payload_profiles,
)
from src.research.can_slim import calculate_can_slim_returns_with_ledger
from src.research.can_slim_validation import (
    fixed_top3_config,
    technical_candidate_financial_coverage,
)
from src.research.foreign_quarterly_diagnostics import (
    DEFAULT_PRIORITY_FILE,
    diagnose_foreign_payload,
    foreign_quarters_to_point_in_time,
    run_foreign_quarterly_diagnostics,
)
from src.research.panel_data import load_panel
from src.research.universe_history import load_universe_snapshots, universe_as_of


DEFAULT_SUMMARY_OUTPUT = Path(
    "output/can_slim_foreign_quarterly_impact_summary.json"
)
DEFAULT_ANNUAL_OUTPUT = Path(
    "output/can_slim_foreign_quarterly_impact_annual.csv"
)


def _annual(result: pd.DataFrame) -> pd.DataFrame:
    annual = (
        (1 + result[["strategy", "benchmark"]])
        .groupby(result.index.year)
        .prod()
        - 1
    )
    annual["excess_vs_nasdaq"] = annual["strategy"] - annual["benchmark"]
    annual.index.name = "year"
    return annual


def _payloads_for_symbols(
    symbols: set[str], cache_dir: Path
) -> dict[str, tuple[dict, object]]:
    payloads = {}
    for path in _companyfacts_cache_files(cache_dir):
        envelope = _read_companyfacts_cache_envelope(path)
        for raw_symbol in envelope.get("symbols", []):
            symbol = str(raw_symbol).strip().upper()
            if symbol in symbols:
                payloads[symbol] = (
                    envelope.get("payload") or {},
                    envelope["fetched_at"],
                )
    missing = symbols - set(payloads)
    if missing:
        raise RuntimeError(
            "Diagnostic-passing symbols disappeared from cache: "
            + ", ".join(sorted(missing))
        )
    return payloads


def run_foreign_quarterly_impact(
    priority_file: Path = DEFAULT_PRIORITY_FILE,
    cache_dir: Path = SEC_COMPANYFACTS_CACHE_DIR,
) -> tuple[pd.DataFrame, dict]:
    detail, diagnostic_summary = run_foreign_quarterly_diagnostics(
        priority_file, cache_dir
    )
    quarterly = load_quarterly_fundamentals(
        POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE
    )
    integrated_mask = quarterly["concept"].astype(str).str.startswith(
        "foreign_"
    )
    integrated_symbols = set(
        quarterly.loc[integrated_mask, "ticker"].astype(str)
    )
    if integrated_symbols:
        mode = "formal_rows_vs_removal_counterfactual"
        eligible = integrated_symbols
        payloads = _payloads_for_symbols(eligible, Path(cache_dir))
        integrated_validation = {
            symbol: diagnose_foreign_payload(symbol, 0, payload)[
                "diagnostic_status"
            ]
            for symbol, (payload, _fetched_at) in sorted(payloads.items())
        }
        failed = {
            symbol: status
            for symbol, status in integrated_validation.items()
            if status != "PASS_DIAGNOSTIC_ONLY"
        }
        if failed:
            raise RuntimeError(
                "Integrated foreign quarters no longer pass validation: "
                + json.dumps(failed, sort_keys=True)
            )
        research_rows = quarterly.loc[integrated_mask].copy()
        baseline_quarterly = quarterly.loc[~integrated_mask].copy()
        augmented = quarterly
    else:
        mode = "research_rows_vs_current_formal"
        integrated_validation = {}
        eligible = set(
            detail.loc[
                detail["eligible_for_parser_research"], "ticker"
            ].astype(str)
        )
        selected_currencies = detail.set_index("ticker")[
            "selected_currency"
        ].to_dict()
        payloads = _payloads_for_symbols(eligible, Path(cache_dir))
        research_rows = pd.concat(
            [
                foreign_quarters_to_point_in_time(
                    symbol,
                    payload,
                    fetched_at,
                    selected_currencies[symbol],
                )
                for symbol, (payload, fetched_at) in sorted(payloads.items())
            ],
            ignore_index=True,
        ) if payloads else pd.DataFrame()
        baseline_quarterly = quarterly
        augmented = pd.concat(
            [quarterly, research_rows], ignore_index=True
        )

    config = fixed_top3_config()
    load_start = (
        pd.Timestamp(config.start) - pd.Timedelta(days=400)
    ).strftime("%Y-%m-%d")
    close, dollar_volume = load_panel(
        CLEANED_PRICE_DATA_DIR, load_start, config.end
    )
    nasdaq = pd.read_csv(
        NASDAQ_INDEX_FILE, index_col="date", parse_dates=True
    )["close"]
    eps = load_eps_history(POINT_IN_TIME_EPS_FILE)
    annual_fundamentals = pd.read_csv(
        POINT_IN_TIME_FUNDAMENTALS_FILE, usecols=["ticker", "form"]
    )
    snapshots = load_universe_snapshots()
    universe = lambda value: universe_as_of(snapshots, value)
    raw_profiles = cached_companyfacts_symbol_payload_profiles(cache_dir)

    coverage = {}
    for name, frame in (
        ("baseline", baseline_quarterly),
        ("augmented", augmented),
    ):
        coverage[name] = technical_candidate_financial_coverage(
            close,
            dollar_volume,
            nasdaq,
            frame,
            snapshots,
            config,
            annual_fundamentals=annual_fundamentals,
            raw_cache_profiles=raw_profiles,
        )

    baseline, baseline_ledger = calculate_can_slim_returns_with_ledger(
        close,
        dollar_volume,
        nasdaq,
        eps,
        config,
        universe,
        baseline_quarterly,
    )
    augmented_result, augmented_ledger = (
        calculate_can_slim_returns_with_ledger(
            close, dollar_volume, nasdaq, eps, config, universe, augmented
        )
    )
    annual = pd.concat(
        {
            "baseline": _annual(baseline).loc[2021:],
            "augmented": _annual(augmented_result).loc[2021:],
        },
        axis=1,
    )
    annual.columns = [
        f"{scenario}_{metric}" for scenario, metric in annual.columns
    ]
    annual = annual.reset_index()

    ledger_columns = [
        "signal_date",
        "execution_date",
        "ticker",
        "side",
        "target_weight_after",
    ]
    ledger_comparison = baseline_ledger[ledger_columns].merge(
        augmented_ledger[ledger_columns],
        on=ledger_columns,
        how="outer",
        indicator=True,
    )
    ledger_differences = int(ledger_comparison["_merge"].ne("both").sum())
    baseline_missing = coverage["baseline"][
        "missing_financial_observations"
    ]
    augmented_missing = coverage["augmented"][
        "missing_financial_observations"
    ]
    summary = {
        "purpose": "in_memory_foreign_quarterly_impact",
        "comparison_mode": mode,
        "formal_fundamentals_modified_by_this_run": False,
        "formal_foreign_rows_present": bool(integrated_symbols),
        "formal_model_changed": False,
        "eligible_symbols": sorted(eligible),
        "integrated_validation": integrated_validation,
        "research_row_count": int(len(research_rows)),
        "baseline_missing_financial_observations": int(baseline_missing),
        "augmented_missing_financial_observations": int(augmented_missing),
        "missing_financial_observations_recovered": int(
            baseline_missing - augmented_missing
        ),
        "baseline_financial_coverage": float(
            coverage["baseline"]["financial_coverage"]
        ),
        "augmented_financial_coverage": float(
            coverage["augmented"]["financial_coverage"]
        ),
        "baseline_ledger_rows": int(len(baseline_ledger)),
        "augmented_ledger_rows": int(len(augmented_ledger)),
        "ledger_difference_rows": ledger_differences,
        "baseline_final_portfolio_value": float(
            baseline["portfolio_value"].iloc[-1]
        ),
        "augmented_final_portfolio_value": float(
            augmented_result["portfolio_value"].iloc[-1]
        ),
        "historical_strategy_path_changed": bool(
            not baseline["strategy"].equals(augmented_result["strategy"])
        ),
        "diagnostic_inputs": diagnostic_summary,
    }
    return annual, summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--priority-file", type=Path, default=DEFAULT_PRIORITY_FILE)
    parser.add_argument("--cache-dir", type=Path, default=SEC_COMPANYFACTS_CACHE_DIR)
    parser.add_argument(
        "--summary-output", type=Path, default=DEFAULT_SUMMARY_OUTPUT
    )
    parser.add_argument("--annual-output", type=Path, default=DEFAULT_ANNUAL_OUTPUT)
    args = parser.parse_args()
    annual, summary = run_foreign_quarterly_impact(
        args.priority_file, args.cache_dir
    )
    args.annual_output.parent.mkdir(parents=True, exist_ok=True)
    annual.to_csv(args.annual_output, index=False)
    args.summary_output.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
