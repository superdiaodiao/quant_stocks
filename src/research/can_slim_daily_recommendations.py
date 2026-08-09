"""Shadow daily recommendation output using the same CAN SLIM selector as replay."""

from __future__ import annotations

import json
import argparse
import os
from datetime import date
from pathlib import Path

import pandas as pd

from src.conf import (
    CLEANED_PRICE_DATA_DIR, NASDAQ_INDEX_FILE, POINT_IN_TIME_EPS_FILE,
    POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE,
)
from src.financial.eps import load_eps_history
from src.financial.quarterly_fundamentals import load_quarterly_fundamentals
from src.research.can_slim import (
    CanSlimConfig,
    calculate_keltner_upper_panel,
    select_can_slim_ensemble_portfolio,
)
from src.research.can_slim_walk_forward import fit_annual_parameter_snapshot
from src.research.data_quality import back_adjust_common_splits
from src.research.data_fingerprint import (
    build_data_manifest,
    can_slim_input_fingerprints,
    fingerprint_file,
)
from src.research.panel_data import load_ohlc_panel, load_panel
from src.research.shadow_ledger import portfolio_source_columns
from src.research.universe_history import load_universe_snapshots, universe_as_of
from src.strategy.common import (
    CASH_SENTINEL,
    market_regime_is_on,
    online_rebalance_context,
)


def quarterly_input_from_summary(summary: dict) -> tuple[Path, dict]:
    """Resolve and verify the quarterly PIT input frozen by a model summary."""
    declared = summary.get("quarterly_input") or {}
    path = Path(
        declared.get("path", POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE)
    ).resolve()
    if not path.is_file():
        raise ValueError(f"Frozen quarterly input does not exist: {path}")
    fingerprint = fingerprint_file(path)
    expected_sha = declared.get("sha256")
    if expected_sha and fingerprint["sha256"] != expected_sha:
        raise ValueError("Frozen quarterly input SHA-256 does not match")
    return path, fingerprint


def add_current_liquidity_guidance(
    recommendations: pd.DataFrame,
    dollar_volume: pd.DataFrame,
    as_of: pd.Timestamp,
    account_sizes: tuple[float, ...] = (100_000.0, 1_000_000.0),
    liquidity_window: int = 50,
) -> pd.DataFrame:
    """Add read-only full-target capacity hints without sizing an order."""
    result = recommendations.copy()
    medians = {}
    for ticker in result["ticker"].astype(str):
        if ticker == CASH_SENTINEL or ticker not in dollar_volume:
            medians[ticker] = float("nan")
            continue
        history = dollar_volume.loc[
            dollar_volume.index <= pd.Timestamp(as_of), ticker
        ].dropna().tail(liquidity_window)
        medians[ticker] = (
            float(history.median()) if len(history) else float("nan")
        )
    result["current_median_dollar_volume_50d"] = (
        result["ticker"].astype(str).map(medians)
    )
    target_weight = pd.to_numeric(
        result.get("target_weight", 0.0), errors="coerce"
    ).fillna(0.0)
    for account_size in account_sizes:
        label = f"{int(account_size):d}"
        result[f"full_target_participation_at_{label}_account"] = (
            target_weight
            * account_size
            / result["current_median_dollar_volume_50d"]
        )
    for participation in (0.01, 0.05):
        label = f"{int(participation * 100)}pct"
        result[f"full_target_account_capacity_at_{label}"] = (
            participation
            * result["current_median_dollar_volume_50d"]
            / target_weight.where(target_weight.gt(0))
        )
    return result


def reuse_recorded_signal_portfolio(
    recommendations: pd.DataFrame,
    history_file: str | Path,
    close_at_as_of: pd.Series,
    *,
    as_of: pd.Timestamp,
    execution_date: pd.Timestamp | None,
    generated_at: str,
    action: str,
    mode: str,
    action_reason: str | None = None,
) -> pd.DataFrame:
    """Keep the first recorded portfolio frozen for its whole signal period."""
    path = Path(history_file)
    if recommendations.empty or not path.exists():
        return recommendations
    history = pd.read_csv(path)
    required = {"signal_date", "model_version", "generated_at", "ticker"}
    if not required.issubset(history.columns):
        missing = sorted(required - set(history.columns))
        raise ValueError(f"Recommendation history lacks freeze columns: {missing}")
    legacy_cash = (
        history["ticker"].astype(str).str.upper().eq("CASH")
        & pd.to_numeric(
            history.get("target_weight", 0), errors="coerce"
        ).fillna(0).eq(0)
    )
    history.loc[legacy_cash, "ticker"] = CASH_SENTINEL
    signal_date = str(recommendations["signal_date"].iloc[0])
    model_version = str(recommendations["model_version"].iloc[0])
    matching = history.loc[
        history["signal_date"].astype(str).eq(signal_date)
        & history["model_version"].astype(str).eq(model_version)
    ].copy()
    if matching.empty:
        return recommendations
    generation = pd.to_datetime(
        matching["generated_at"], utc=True, errors="coerce"
    )
    if generation.isna().any():
        raise ValueError("Recommendation history has invalid generated_at values")
    frozen = matching.loc[generation == generation.min()].copy()
    frozen["ticker"] = frozen["ticker"].astype(str).str.upper()
    frozen = frozen.sort_values(
        "rank" if "rank" in frozen.columns else "ticker"
    )
    prices = close_at_as_of.copy()
    prices.index = prices.index.astype(str).str.upper()
    missing = sorted(
        set(
            frozen.loc[
                frozen["ticker"].ne(CASH_SENTINEL), "ticker"
            ]
        )
        - set(prices.index)
    )
    if missing:
        raise ValueError(f"Frozen portfolio lacks current prices: {missing}")
    frozen["as_of"] = as_of.strftime("%Y-%m-%d")
    frozen["execution_date"] = (
        execution_date.strftime("%Y-%m-%d")
        if execution_date is not None else ""
    )
    frozen["current_price"] = frozen["ticker"].map(prices).fillna(1.0)
    if "portfolio_generated_at" not in frozen.columns:
        frozen["portfolio_generated_at"] = generation.min().isoformat()
    frozen["generated_at"] = generated_at
    frozen["action"] = action
    frozen["mode"] = mode
    if action_reason is not None:
        frozen["action_reason"] = action_reason
    return frozen.reset_index(drop=True)


def configs_for_decision_date(
    summary: dict, decision_date: date | pd.Timestamp | None
) -> tuple[list[CanSlimConfig], dict | None]:
    requested_date = pd.Timestamp(decision_date or date.today()).normalize()
    available_snapshots = [
        snapshot for snapshot in summary.get("model_snapshots", [])
        if pd.Timestamp(snapshot["effective_start"]) <= requested_date
    ]
    active_snapshot = max(
        available_snapshots, key=lambda snapshot: snapshot["effective_start"],
        default=None,
    )
    if summary.get("model_snapshots") and active_snapshot is None:
        return [], None
    config_values = (
        active_snapshot["configs"] if active_snapshot
        else summary["current_shadow_configs"]
    )
    return [CanSlimConfig(**values) for values in config_values], active_snapshot


def refresh_parameter_snapshot_if_due(
    summary_file: str | Path,
    decision_date: date | pd.Timestamp | None = None,
) -> tuple[dict, bool]:
    """Create this year's frozen parameters once, using only prior-year data."""
    summary_path = Path(summary_file)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary.get("parameter_update_frequency") == "frozen":
        return summary, False
    requested_date = pd.Timestamp(decision_date or date.today()).normalize()
    snapshots = summary.get("model_snapshots", [])
    if any(
        pd.Timestamp(snapshot["effective_start"]).year == requested_date.year
        for snapshot in snapshots
    ):
        return summary, False
    snapshot, ranking = fit_annual_parameter_snapshot(
        requested_date.year,
        signal_frequency=summary.get("signal_frequency", "monthly"),
        use_quarterly_fundamentals=summary.get(
            "uses_quarterly_fundamentals", True
        ),
        adaptive_channel=summary.get("uses_adaptive_channel", True),
    )
    snapshots.append(snapshot)
    snapshots.sort(key=lambda item: item["effective_start"])
    summary["model_snapshots"] = snapshots
    summary["current_shadow_config_ids"] = snapshot["config_ids"]
    summary["current_shadow_configs"] = snapshot["configs"]
    summary["last_parameter_refresh"] = requested_date.strftime("%Y-%m-%d")
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    ranking_path = summary_path.with_name(
        f"{summary_path.stem}_ranking_{requested_date.year}.csv"
    )
    ranking.to_csv(ranking_path, index=False)
    return summary, True


def generate_can_slim_shadow_recommendations(
    decision_date: date | pd.Timestamp | None = None,
    summary_file: str | Path = (
        "output/can_slim_fixed_top3_summary.json"
    ),
    history_file: str | Path | None = None,
    refresh_parameters: bool = True,
) -> tuple[pd.DataFrame, dict]:
    if refresh_parameters:
        summary, parameters_refreshed = refresh_parameter_snapshot_if_due(
            summary_file, decision_date
        )
    else:
        summary = json.loads(Path(summary_file).read_text(encoding="utf-8"))
        parameters_refreshed = False
    quarterly_input_declared = bool(summary.get("quarterly_input"))
    quarterly_path, quarterly_fingerprint = quarterly_input_from_summary(summary)
    model_version = summary.get("model_version", "can-slim-v2")
    history_file = history_file or Path(
        f"output/daily/{model_version}/recommendation_history.csv"
    )
    seed_configs = [
        CanSlimConfig(**values)
        for values in summary["current_shadow_configs"]
    ]
    needs_keltner = any(
        config.price_channel == "keltner" for config in seed_configs
    )
    if needs_keltner:
        trade_close, dollar_volume, high, low = load_ohlc_panel(
            CLEANED_PRICE_DATA_DIR,
            min(config.start for config in seed_configs),
            None,
        )
    else:
        trade_close, dollar_volume = load_panel(
            CLEANED_PRICE_DATA_DIR,
            min(config.start for config in seed_configs),
            None,
        )
        high = low = None
    close = back_adjust_common_splits(trade_close).sort_index()
    keltner_upper = None
    if needs_keltner:
        adjustment = close / trade_close.reindex_like(close)
        channel_config = next(
            config
            for config in seed_configs
            if config.price_channel == "keltner"
        )
        keltner_upper = calculate_keltner_upper_panel(
            close, high.reindex_like(close) * adjustment,
            low.reindex_like(close) * adjustment,
            channel_config.keltner_window, channel_config.keltner_atr_window,
            channel_config.keltner_multiplier,
        )
    index_close = pd.read_csv(NASDAQ_INDEX_FILE, index_col="date", parse_dates=True)["close"].sort_index()
    signal_frequency = summary.get(
        "signal_frequency", seed_configs[0].signal_frequency
    )
    timing = online_rebalance_context(close.index, decision_date, signal_frequency)
    parameter_date = timing["execution_date"]
    if parameter_date is None:
        parameter_date = timing["as_of"] + pd.Timedelta(days=1)
    configs, active_snapshot = configs_for_decision_date(
        summary, parameter_date
    )
    symbols = universe_as_of(load_universe_snapshots(), timing["signal_date"])
    if symbols is None:
        raise ValueError(f"No point-in-time universe for {timing['signal_date'].date()}")
    if configs:
        scores = select_can_slim_ensemble_portfolio(
            timing["signal_date"],
            close.loc[:, close.columns.intersection(symbols)],
            dollar_volume.loc[:, dollar_volume.columns.intersection(symbols)],
            index_close,
            load_eps_history(POINT_IN_TIME_EPS_FILE),
            configs,
            symbols,
            load_quarterly_fundamentals(quarterly_path)
            if any(
                config.use_quarterly_fundamentals for config in configs
            )
            else None,
            keltner_upper,
            eligibility_close=trade_close.loc[
                :, trade_close.columns.intersection(symbols)
            ],
        )
    else:
        scores = pd.DataFrame()
    mode = "PRODUCTION_ELIGIBLE" if summary["release_status"] == "PASS" else "SHADOW"
    risk_on = bool(configs) and market_regime_is_on(
        timing["signal_date"], index_close, configs[0].market_ma_days
    )
    action = "BUY_NEXT_CLOSE" if risk_on and timing["order_pending"] else ("HOLD_POSITION" if risk_on else "HOLD_CASH")
    if not configs:
        action_reason = "MODEL_NOT_YET_EFFECTIVE_AT_EXECUTION"
    elif not risk_on:
        action_reason = "MARKET_REGIME_OFF"
    elif scores.empty:
        action_reason = "NO_QUALIFYING_STOCKS"
    else:
        action_reason = "ACTIVE_SELECTION"
    if scores.empty or not risk_on:
        scores = pd.DataFrame(
            index=pd.Index([CASH_SENTINEL], name="ticker")
        )
    generated_at = pd.Timestamp.now(tz="UTC").isoformat()
    scores.insert(0, "as_of", timing["as_of"].strftime("%Y-%m-%d"))
    scores.insert(1, "ticker", scores.index)
    scores.insert(2, "rank", range(1, len(scores) + 1))
    scores.insert(3, "action", action)
    scores.insert(4, "action_reason", action_reason)
    scores.insert(5, "mode", mode)
    if "target_weight" not in scores.columns or not risk_on:
        scores["target_weight"] = 0.0
    scores.insert(6, "model_version", model_version)
    scores.insert(7, "signal_date", timing["signal_date"].strftime("%Y-%m-%d"))
    scores.insert(
        8,
        "execution_date",
        (
            timing["execution_date"].strftime("%Y-%m-%d")
            if timing["execution_date"] is not None
            else ""
        ),
    )
    scores.insert(
        9, "current_price",
        [
            1.0 if ticker == CASH_SENTINEL
            else float(trade_close.loc[timing["as_of"], ticker])
            for ticker in scores.index
        ],
    )
    scores.insert(10, "generated_at", generated_at)
    scores["portfolio_generated_at"] = generated_at
    input_fingerprints = can_slim_input_fingerprints()
    scores["portfolio_strategy_sha256"] = input_fingerprints[
        "strategy_code"
    ]["sha256"]
    if quarterly_input_declared:
        input_fingerprints["quarterly_fundamentals"] = quarterly_fingerprint
        data_manifest = build_data_manifest(input_fingerprints)
        input_fingerprints["data_manifest"] = data_manifest
    else:
        data_manifest = input_fingerprints["data_manifest"]
    scores["portfolio_data_manifest_sha256"] = data_manifest["sha256"]
    scores["portfolio_data_components_json"] = json.dumps(
        data_manifest["components"],
        sort_keys=True,
        separators=(",", ":"),
    )
    for column, value in portfolio_source_columns().items():
        scores[column] = value
    scores = reuse_recorded_signal_portfolio(
        scores.reset_index(drop=True), history_file,
        trade_close.loc[timing["as_of"]],
        as_of=timing["as_of"], execution_date=timing["execution_date"], generated_at=generated_at,
        action=action, mode=mode, action_reason=action_reason,
    )
    scores = add_current_liquidity_guidance(
        scores, dollar_volume, timing["as_of"]
    )
    return scores, {
        "as_of": timing["as_of"].strftime("%Y-%m-%d"),
        # Keep the model signal date in the metadata as well as in each
        # recommendation row.  The daily pipeline uses this field for its
        # selected-price and quarterly-conflict audits, including the
        # no-signal/HOLD_CASH path where the row is only the cash sentinel.
        "signal_date": timing["signal_date"].strftime("%Y-%m-%d"),
        "model_version": model_version,
        "mode": mode,
        "release_status": summary["release_status"],
        "signal_frequency": signal_frequency,
        "recommendations": len(scores),
        "model_snapshot_effective_start": (
            active_snapshot["effective_start"] if active_snapshot else None
        ),
        "model_snapshot_training_end": (
            active_snapshot["training_end"] if active_snapshot else None
        ),
        "parameters_refreshed": parameters_refreshed,
        "action_reason": action_reason,
        "liquidity_guidance": (
            "Read-only full-target participation versus the current "
            "50-session median dollar volume; not an order quantity or "
            "closing-auction fill estimate."
        ),
        "input_fingerprints": input_fingerprints,
    }


def save_can_slim_shadow_recommendations(
    recommendations: pd.DataFrame,
    metadata: dict,
    output_dir: str | Path = "output/daily",
) -> Path:
    output = Path(output_dir) / metadata["model_version"]
    output.mkdir(parents=True, exist_ok=True)
    path = output / f"recommendations_{metadata['as_of']}.csv"
    recommendations.to_csv(path, index=False)
    path.with_suffix(".json").write_text(
        json.dumps(metadata, indent=2), encoding="utf-8"
    )
    # Compare the exact CSV representation that will be persisted.  The
    # generator may hold dates as ``Timestamp`` objects while an existing
    # ledger is read back from CSV as strings; comparing those in-memory
    # representations would reject a harmless rerun of the same frozen
    # signal.  Reloading makes the idempotency check match the bytes that
    # downstream ledger verification sees.
    history_recommendations = pd.read_csv(path)
    history_path = output / "recommendation_history.csv"
    history = (
        pd.read_csv(history_path)
        if history_path.exists() else pd.DataFrame()
    )
    version_columns = {"signal_date", "generated_at"}
    versioned_history = (
        not history.empty
        and not history_recommendations.empty
        and version_columns.issubset(history.columns)
        and version_columns.issubset(history_recommendations.columns)
    )
    if versioned_history:
        key_columns = {"as_of", "model_version"}
        if key_columns.issubset(history.columns) and key_columns.issubset(
            history_recommendations.columns
        ):
            history_keys = set(zip(
                history["as_of"].astype(str),
                history["model_version"].astype(str),
            ))
            recommendation_keys = set(zip(
                history_recommendations["as_of"].astype(str),
                history_recommendations["model_version"].astype(str),
            ))
            overlapping_keys = history_keys & recommendation_keys

            # A daily rerun must be idempotent.  ``generated_at`` is allowed
            # to change, but the frozen portfolio and all other evidence must
            # be byte-equivalent after normalizing column order and NaNs.  A
            # changed portfolio for an already recorded signal is evidence
            # corruption, not a new version that may be appended.
            ephemeral_columns = {"generated_at"}

            def canonical(frame: pd.DataFrame) -> pd.DataFrame:
                columns = sorted(set(frame.columns) - ephemeral_columns)
                normalized = frame.copy()
                for column in columns:
                    if column not in normalized.columns:
                        normalized[column] = pd.NA
                normalized = normalized[columns].fillna("<NA>").astype(str)
                return normalized.sort_values(columns).reset_index(drop=True)

            for key in sorted(overlapping_keys):
                old_rows = history.loc[
                    (history["as_of"].astype(str) == key[0])
                    & (history["model_version"].astype(str) == key[1])
                ]
                new_rows = history_recommendations.loc[
                    (history_recommendations["as_of"].astype(str) == key[0])
                    & (
                        history_recommendations["model_version"].astype(str)
                        == key[1]
                    )
                ]
                if not canonical(old_rows).equals(canonical(new_rows)):
                    raise RuntimeError(
                        "Shadow recommendation history already contains a "
                        "different frozen portfolio for "
                        f"as_of={key[0]}, model_version={key[1]}"
                    )

            if overlapping_keys:
                history_recommendations = history_recommendations.loc[
                    [
                        (str(row.as_of), str(row.model_version))
                        not in overlapping_keys
                        for row in history_recommendations[
                            ["as_of", "model_version"]
                        ].itertuples(index=False)
                    ]
                ]
                if history_recommendations.empty:
                    return path

    if not versioned_history and not history.empty and not history_recommendations.empty:
        replacement_keys = set(zip(
            history_recommendations["as_of"].astype(str),
            history_recommendations["model_version"].astype(str),
        ))
        keep = [
            (str(row.as_of), str(row.model_version)) not in replacement_keys
            for row in history[
                ["as_of", "model_version"]
            ].itertuples(index=False)
        ]
        history = history.loc[keep]
    combined = pd.concat(
        [history, history_recommendations], ignore_index=True
    )
    if versioned_history:
        try:
            pd.testing.assert_frame_equal(
                combined.iloc[:len(history)][history.columns]
                .reset_index(drop=True),
                history.reset_index(drop=True),
                check_dtype=False,
            )
        except AssertionError as error:
            raise RuntimeError(
                "Shadow recommendation history must be append-only"
            ) from error
    temporary = history_path.with_suffix(history_path.suffix + ".tmp")
    combined.to_csv(temporary, index=False)
    os.replace(temporary, history_path)
    return path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--summary-file", default="output/can_slim_fixed_top3_summary.json"
    )
    args = parser.parse_args()
    recommendations, metadata = generate_can_slim_shadow_recommendations(
        summary_file=args.summary_file
    )
    save_can_slim_shadow_recommendations(recommendations, metadata)


if __name__ == "__main__":
    main()
