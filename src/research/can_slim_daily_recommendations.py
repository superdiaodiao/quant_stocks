"""Shadow daily recommendation output using the same CAN SLIM selector as replay."""

from __future__ import annotations

import json
import argparse
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
from src.research.panel_data import load_ohlc_panel, load_panel
from src.research.universe_history import load_universe_snapshots, universe_as_of
from src.strategy.common import market_regime_is_on, online_rebalance_context


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
        set(frozen.loc[frozen["ticker"].ne("CASH"), "ticker"]) - set(prices.index)
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
            load_quarterly_fundamentals(
                POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE
            )
            if any(
                config.use_quarterly_fundamentals for config in configs
            )
            else None,
            keltner_upper,
        )
    else:
        scores = pd.DataFrame()
    mode = "PRODUCTION_ELIGIBLE" if summary["release_status"] == "PASS" else "SHADOW"
    risk_on = bool(configs) and market_regime_is_on(
        timing["signal_date"], index_close, configs[0].market_ma_days
    )
    action = "BUY_NEXT_CLOSE" if risk_on and timing["order_pending"] else ("HOLD_POSITION" if risk_on else "HOLD_CASH")
    if configs and (scores.empty or not risk_on):
        scores = pd.DataFrame(index=pd.Index(["CASH"], name="ticker"))
    generated_at = pd.Timestamp.now(tz="UTC").isoformat()
    scores.insert(0, "as_of", timing["as_of"].strftime("%Y-%m-%d"))
    scores.insert(1, "ticker", scores.index)
    scores.insert(2, "rank", range(1, len(scores) + 1))
    scores.insert(3, "action", action)
    scores.insert(4, "mode", mode)
    if "target_weight" not in scores.columns or not risk_on:
        scores["target_weight"] = 0.0
    scores.insert(5, "model_version", model_version)
    scores.insert(6, "signal_date", timing["signal_date"].strftime("%Y-%m-%d"))
    scores.insert(
        7, "current_price",
        [
            1.0 if ticker == "CASH"
            else float(trade_close.loc[timing["as_of"], ticker])
            for ticker in scores.index
        ],
    )
    scores.insert(8, "generated_at", generated_at)
    scores = reuse_recorded_signal_portfolio(
        scores.reset_index(drop=True), history_file,
        trade_close.loc[timing["as_of"]],
        as_of=timing["as_of"], execution_date=timing["execution_date"], generated_at=generated_at,
        action=action, mode=mode,
    )
    return scores, {
        "as_of": timing["as_of"].strftime("%Y-%m-%d"),
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
    history_path = output / "recommendation_history.csv"
    history = (
        pd.read_csv(history_path)
        if history_path.exists() else pd.DataFrame()
    )
    if not history.empty and not recommendations.empty:
        replacement_keys = set(zip(
            recommendations["as_of"].astype(str),
            recommendations["model_version"].astype(str),
        ))
        keep = [
            (str(row.as_of), str(row.model_version)) not in replacement_keys
            for row in history[["as_of", "model_version"]].itertuples(index=False)
        ]
        history = history.loc[keep]
    combined = pd.concat([history, recommendations], ignore_index=True)
    combined = combined.drop_duplicates(
        ["as_of", "ticker", "model_version"], keep="last"
    )
    combined.to_csv(history_path, index=False)
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
