"""Isolate same-issuer ticker normalization under the frozen Top 3 policy.

This diagnostic compares the current research inputs with a counterfactual in
which selected, SEC-sourced ``issuer_rename`` rows are removed.  It does not
change formal inputs, prices, parameters, or validation artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import src.financial.eps as eps_module
import src.financial.quarterly_fundamentals as quarterly_module
from src.conf import (
    CLEANED_PRICE_DATA_DIR,
    NASDAQ_INDEX_FILE,
    POINT_IN_TIME_EPS_FILE,
    POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE,
    PROJECT_PATH,
)
from src.io.security_identity import (
    SECURITY_IDENTITY_FILE,
    issuer_rename_transitions,
    load_security_identity,
    normalize_point_in_time_tickers,
)
from src.research.can_slim import calculate_can_slim_returns_with_ledger
from src.research.can_slim_validation import fixed_top3_config
from src.research.data_fingerprint import (
    build_data_manifest,
    can_slim_input_fingerprints,
)
from src.research.data_quality import back_adjust_common_splits
from src.research.panel_data import load_panel
from src.research.quarterly_data_version_impact import changed_target_signals
from src.research.universe_history import load_universe_snapshots, universe_as_of


DEFAULT_OUTPUT = Path(PROJECT_PATH) / (
    "output/data_provenance/issuer_rename_sensitivity_2026-08-10.json"
)
DEFAULT_ANNUAL_OUTPUT = Path(PROJECT_PATH) / (
    "output/data_provenance/issuer_rename_sensitivity_annual_2026-08-10.csv"
)
DEFAULT_SIGNAL_OUTPUT = Path(PROJECT_PATH) / (
    "output/data_provenance/issuer_rename_sensitivity_signals_2026-08-10.csv"
)
FORMAL_IMPACT_SUMMARY = Path(PROJECT_PATH) / (
    "output/can_slim_quarterly_data_version_impact_summary.json"
)
FORMAL_IMPACT_ANNUAL = Path(PROJECT_PATH) / (
    "output/can_slim_quarterly_data_version_impact_annual.csv"
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


def _annual(result: pd.DataFrame, label: str) -> pd.DataFrame:
    annual = (1 + result[["strategy", "benchmark"]]).groupby(
        result.index.year
    ).prod() - 1
    annual.index.name = "year"
    return annual.rename(
        columns={"strategy": f"{label}_strategy", "benchmark": f"{label}_benchmark"}
    )


def _target_tickers(ledger: pd.DataFrame, signal_date: str) -> list[str]:
    cutoff = pd.Timestamp(signal_date).normalize()
    frame = ledger.copy()
    frame["signal_date"] = pd.to_datetime(
        frame["signal_date"], errors="coerce"
    ).dt.normalize()
    state: dict[str, float] = {}
    for row in frame.loc[frame["signal_date"].le(cutoff)].itertuples(index=False):
        state[str(row.ticker)] = float(row.target_weight_after)
    return sorted(ticker for ticker, weight in state.items() if weight > 0)


def _load_counterfactual_fundamentals(
    disabled_historical_tickers: set[str],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    identities = load_security_identity()
    disabled = identities["identity_type"].eq("issuer_rename") & identities[
        "historical_ticker"
    ].isin(disabled_historical_tickers)
    filtered = identities.loc[~disabled].copy()
    removed = identities.loc[disabled].copy()
    if removed.empty:
        raise ValueError(
            "no issuer_rename rows matched disabled historical tickers: "
            f"{sorted(disabled_historical_tickers)}"
        )
    with tempfile.TemporaryDirectory(prefix="issuer-rename-sensitivity-") as tmp:
        filtered_path = Path(tmp) / "security_identity.csv"
        filtered.to_csv(filtered_path, index=False, date_format="%Y-%m-%d")

        def normalize(frame: pd.DataFrame, path: str | Path = filtered_path) -> pd.DataFrame:
            return normalize_point_in_time_tickers(frame, filtered_path)

        with patch.object(
            eps_module, "normalize_point_in_time_tickers", normalize
        ), patch.object(
            quarterly_module, "normalize_point_in_time_tickers", normalize
        ):
            eps = eps_module.load_eps_history(POINT_IN_TIME_EPS_FILE)
            quarterly = quarterly_module.load_quarterly_fundamentals(
                POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE
            )
    return eps, quarterly, removed


def _replay(
    close: pd.DataFrame,
    dollar_volume: pd.DataFrame,
    nasdaq: pd.Series,
    eps: pd.DataFrame,
    quarterly: pd.DataFrame,
    snapshots: dict,
    identity_transitions: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    config = fixed_top3_config()
    adjusted_close = back_adjust_common_splits(close).sort_index()
    return calculate_can_slim_returns_with_ledger(
        adjusted_close,
        dollar_volume,
        nasdaq,
        eps,
        config,
        lambda value: universe_as_of(snapshots, value),
        quarterly,
        adjust_splits=False,
        eligibility_close=close,
        identity_transitions=identity_transitions,
    )


def run(
    *,
    disabled_historical_tickers: set[str],
    output: Path,
    annual_output: Path,
    signal_output: Path,
) -> dict:
    config = fixed_top3_config()
    load_start = (pd.Timestamp(config.start) - pd.Timedelta(days=400)).strftime(
        "%Y-%m-%d"
    )
    close, dollar_volume = load_panel(CLEANED_PRICE_DATA_DIR, load_start, config.end)
    nasdaq = pd.read_csv(
        NASDAQ_INDEX_FILE, index_col="date", parse_dates=True
    )["close"]
    snapshots = load_universe_snapshots()
    current_eps = eps_module.load_eps_history(POINT_IN_TIME_EPS_FILE)
    current_quarterly = quarterly_module.load_quarterly_fundamentals(
        POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE
    )
    current_transitions = issuer_rename_transitions()
    counterfactual_eps, counterfactual_quarterly, removed = (
        _load_counterfactual_fundamentals(disabled_historical_tickers)
    )
    counterfactual_transitions = current_transitions.loc[
        ~current_transitions["historical_ticker"].isin(disabled_historical_tickers)
    ].copy()

    current_result, current_ledger = _replay(
        close,
        dollar_volume,
        nasdaq,
        current_eps,
        current_quarterly,
        snapshots,
        current_transitions,
    )
    counterfactual_result, counterfactual_ledger = _replay(
        close,
        dollar_volume,
        nasdaq,
        counterfactual_eps,
        counterfactual_quarterly,
        snapshots,
        counterfactual_transitions,
    )

    annual = pd.concat(
        [
            _annual(counterfactual_result, "rename_disabled"),
            _annual(current_result, "current"),
        ],
        axis=1,
    ).loc[2021:]
    annual["strategy_delta"] = (
        annual["current_strategy"] - annual["rename_disabled_strategy"]
    )
    annual["benchmark_consistent"] = annual["current_benchmark"].eq(
        annual["rename_disabled_benchmark"]
    )
    annual.reset_index().to_csv(annual_output, index=False)

    signals = changed_target_signals(counterfactual_ledger, current_ledger)
    signals = signals.rename(
        columns={
            "reference_tickers": "rename_disabled_tickers",
            "candidate_tickers": "current_tickers",
            "removed_tickers": "current_removed_tickers",
            "added_tickers": "current_added_tickers",
        }
    )
    signals.to_csv(signal_output, index=False)

    fingerprints = can_slim_input_fingerprints()
    data_manifest = build_data_manifest(fingerprints)
    formal_summary = json.loads(FORMAL_IMPACT_SUMMARY.read_text(encoding="utf-8"))
    formal_annual = pd.read_csv(FORMAL_IMPACT_ANNUAL)
    formal_2025 = formal_annual.loc[formal_annual["year"].eq(2025)].iloc[0]
    current_wins = int(
        annual["current_strategy"].gt(annual["current_benchmark"]).sum()
    )
    counterfactual_wins = int(
        annual["rename_disabled_strategy"]
        .gt(annual["rename_disabled_benchmark"])
        .sum()
    )
    signal_date = "2025-10-31"
    payload = {
        "format_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "purpose": "issuer_rename_fixed_top3_sensitivity",
        "research_only": True,
        "release_status": "BLOCKED",
        "formal_validation_rerun": False,
        "formal_outputs_written": False,
        "disabled_historical_tickers": sorted(disabled_historical_tickers),
        "removed_identity_rows": removed.assign(
            last_historical_date=lambda x: x["last_historical_date"].dt.strftime(
                "%Y-%m-%d"
            ),
            current_ticker_first_date=lambda x: x[
                "current_ticker_first_date"
            ].dt.strftime("%Y-%m-%d"),
        ).to_dict(orient="records"),
        "input_evidence": {
            "security_identity_path": str(SECURITY_IDENTITY_FILE),
            "security_identity_sha256": _sha256(SECURITY_IDENTITY_FILE),
            "eps_path": str(POINT_IN_TIME_EPS_FILE),
            "eps_sha256": _sha256(POINT_IN_TIME_EPS_FILE),
            "quarterly_path": str(POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE),
            "quarterly_sha256": _sha256(POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE),
            "nasdaq_path": str(NASDAQ_INDEX_FILE),
            "nasdaq_sha256": _sha256(NASDAQ_INDEX_FILE),
            "current_data_manifest": data_manifest,
        },
        "outputs": {
            "annual_path": str(annual_output),
            "annual_sha256": _sha256(annual_output),
            "signals_path": str(signal_output),
            "signals_sha256": _sha256(signal_output),
        },
        "results": {
            "current_wins_vs_nasdaq": current_wins,
            "rename_disabled_wins_vs_nasdaq": counterfactual_wins,
            "changed_signal_count": int(len(signals)),
            "current_2025_strategy": float(annual.loc[2025, "current_strategy"]),
            "rename_disabled_2025_strategy": float(
                annual.loc[2025, "rename_disabled_strategy"]
            ),
            "nasdaq_2025": float(annual.loc[2025, "current_benchmark"]),
            "signal_date": signal_date,
            "current_tickers": _target_tickers(current_ledger, signal_date),
            "rename_disabled_tickers": _target_tickers(
                counterfactual_ledger, signal_date
            ),
        },
        "formal_4_of_6_reference": {
            "summary_path": str(FORMAL_IMPACT_SUMMARY),
            "summary_sha256": _sha256(FORMAL_IMPACT_SUMMARY),
            "candidate_wins_vs_nasdaq": int(formal_summary["candidate_wins_vs_nasdaq"]),
            "annual_path": str(FORMAL_IMPACT_ANNUAL),
            "annual_sha256": _sha256(FORMAL_IMPACT_ANNUAL),
            "strategy_2025": float(formal_2025["candidate_strategy"]),
            "nasdaq_2025": float(formal_2025["candidate_benchmark"]),
        },
    }
    payload["conclusions"] = {
        "rename_disabled_matches_formal_2025_return": bool(
            abs(
                payload["results"]["rename_disabled_2025_strategy"]
                - payload["formal_4_of_6_reference"]["strategy_2025"]
            )
            < 1e-12
        ),
        "rename_normalization_changes_2025_selection": bool(
            payload["results"]["current_tickers"]
            != payload["results"]["rename_disabled_tickers"]
        ),
        "formal_result_revalidated": False,
    }
    _atomic_json(output, payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disable", default="COMM")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--annual-output", type=Path, default=DEFAULT_ANNUAL_OUTPUT)
    parser.add_argument("--signal-output", type=Path, default=DEFAULT_SIGNAL_OUTPUT)
    args = parser.parse_args()
    payload = run(
        disabled_historical_tickers={
            ticker.strip().upper() for ticker in args.disable.split(",") if ticker.strip()
        },
        output=args.output,
        annual_output=args.annual_output,
        signal_output=args.signal_output,
    )
    print(json.dumps(payload["results"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
