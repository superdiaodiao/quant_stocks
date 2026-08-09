"""Research-only selection impact for cached StockAnalysis bridge evidence.

This script deliberately does not import source prices into the formal price
store.  It uses only cache pages already recorded by stockanalysis_price_triage
and evaluates the frozen selector only at signal dates for which a source is
continuous from the last formal price through that signal date.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pandas as pd

from scripts.stockanalysis_price_triage import (
    DEFAULT_CACHE_DIR,
    DEFAULT_OUTPUT as DEFAULT_TRIAGE_OUTPUT,
    _coverage_summary,
    _local_prices,
    _overlap_summary,
    _read_cached_page,
    parse_stockanalysis_history,
)
from src.conf import (
    CLEANED_PRICE_DATA_DIR,
    NASDAQ_INDEX_FILE,
    POINT_IN_TIME_EPS_FILE,
    POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE,
)
from src.financial.eps import load_eps_history
from src.financial.quarterly_fundamentals import load_quarterly_fundamentals
from src.research.can_slim import score_can_slim_cross_section
from src.research.can_slim_validation import fixed_top3_config
from src.research.corporate_action_selection_impact import (
    compare_scored_cross_sections,
)
from src.research.data_fingerprint import can_slim_input_fingerprints
from src.research.data_quality import back_adjust_common_splits
from src.research.panel_data import load_panel
from src.research.universe_history import load_universe_snapshots, universe_as_of
from src.strategy.common import market_regime_is_on, scheduled_signal_dates


RESEARCH_STATUS = "RESEARCH_ONLY"
CONTINUOUS_BRIDGE_ASSESSMENT = (
    "REVIEW_CONTINUOUS_BRIDGE_REQUIRES_TERMINAL_EVIDENCE"
)
DEFAULT_OUTPUT_DIR = Path("output/data_provenance")
DEFAULT_SUMMARY_PATH = DEFAULT_OUTPUT_DIR / "stockanalysis_price_gap_selection_impact.json"
DEFAULT_SIGNALS_PATH = DEFAULT_OUTPUT_DIR / "stockanalysis_price_gap_selection_signals.csv"
DEFAULT_DETAILS_PATH = DEFAULT_OUTPUT_DIR / "stockanalysis_price_gap_selection_details.csv"
DEFAULT_OBSERVATIONS_PATH = (
    DEFAULT_OUTPUT_DIR / "stockanalysis_price_gap_selection_bridge_observations.csv"
)


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_triage_records(path: str | Path) -> dict[str, dict[str, Any]]:
    report = json.loads(Path(path).read_text(encoding="utf-8"))
    return {
        str(record.get("ticker", "")).upper(): record
        for record in report.get("records", [])
        if record.get("ticker")
    }


def _bridge_eligibility(
    *,
    triage_record: dict[str, Any],
    envelope: dict[str, Any],
    coverage: dict[str, Any],
    overlap: dict[str, Any],
    minimum_overlap_sessions: int,
) -> str | None:
    """Return an exclusion reason, keeping all imported-price assumptions explicit."""
    if triage_record.get("status") != "RESEARCH_LEAD_ONLY":
        return "TRIAGE_RECORD_NOT_RESEARCH_LEAD"
    if triage_record.get("assessment") != CONTINUOUS_BRIDGE_ASSESSMENT:
        return "TRIAGE_RECORD_NOT_CONTINUOUS_BRIDGE"
    if triage_record.get("cache_payload_sha256") != envelope.get("payload_sha256"):
        return "TRIAGE_CACHE_SHA_MISMATCH"
    if not coverage.get("source_bridges_from_local_to_source_end"):
        return "SOURCE_NOT_CONTINUOUS_FROM_FORMAL_PRICE"
    if overlap.get("overlap_sessions", 0) < minimum_overlap_sessions:
        return "INSUFFICIENT_FORMAL_SOURCE_OVERLAP"
    if overlap.get("price_ratio_within_tolerance_fraction", 0.0) < 0.95:
        return "SOURCE_PRICE_OVERLAP_MISMATCH"
    if overlap.get("volume_ratio_within_tolerance_fraction", 0.0) < 0.95:
        return "SOURCE_VOLUME_OVERLAP_MISMATCH"
    return None


def load_continuous_bridge_evidence(
    *,
    triage_path: str | Path = DEFAULT_TRIAGE_OUTPUT,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    price_dir: str | Path = CLEANED_PRICE_DATA_DIR,
    benchmark_dates: pd.Series | pd.DatetimeIndex,
    analysis_end: str | pd.Timestamp,
    minimum_overlap_sessions: int = 20,
    relative_tolerance: float = 0.01,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Load only cache-verified, continuous research bridges; never fetch."""
    triage_records = _load_triage_records(triage_path)
    parsed_end = pd.Timestamp(analysis_end).normalize()
    bridges: dict[str, dict[str, Any]] = {}
    diagnostics: list[dict[str, Any]] = []

    for ticker, triage_record in sorted(triage_records.items()):
        diagnostic: dict[str, Any] = {
            "ticker": ticker,
            "triage_status": triage_record.get("status"),
            "triage_assessment": triage_record.get("assessment"),
            "eligible": False,
        }
        if triage_record.get("assessment") != CONTINUOUS_BRIDGE_ASSESSMENT:
            diagnostic["exclusion_reason"] = "TRIAGE_RECORD_NOT_CONTINUOUS_BRIDGE"
            diagnostics.append(diagnostic)
            continue

        try:
            envelope, payload = _read_cached_page(cache_dir, ticker)
            source, provider = parse_stockanalysis_history(payload)
            local = _local_prices(ticker, price_dir)
            coverage = _coverage_summary(
                local,
                source,
                benchmark_dates,
                parsed_end,
            )
            overlap = _overlap_summary(local, source, relative_tolerance)
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
            diagnostic["exclusion_reason"] = "SOURCE_CACHE_OR_PARSE_ERROR"
            diagnostic["error"] = str(exc)
            diagnostics.append(diagnostic)
            continue

        exclusion = _bridge_eligibility(
            triage_record=triage_record,
            envelope=envelope,
            coverage=coverage,
            overlap=overlap,
            minimum_overlap_sessions=minimum_overlap_sessions,
        )
        diagnostic.update(
            {
                "source_provider": provider,
                "cache_payload_sha256": envelope.get("payload_sha256"),
                "cache_fetched_at": envelope.get("fetched_at"),
                "coverage": coverage,
                "overlap": overlap,
            }
        )
        if exclusion is not None:
            diagnostic["exclusion_reason"] = exclusion
            diagnostics.append(diagnostic)
            continue

        price_scale = float(overlap["price_ratio_median"])
        volume_scale = float(overlap["volume_ratio_median"])
        bridge = {
            "ticker": ticker,
            "source": source,
            "source_provider": provider,
            "source_url": envelope.get("source_url"),
            "cache_payload_sha256": envelope.get("payload_sha256"),
            "cache_fetched_at": envelope.get("fetched_at"),
            "last_local_price_date": pd.Timestamp(
                coverage["last_local_price_date"]
            ).normalize(),
            "source_end_date": pd.Timestamp(
                coverage["source_bridge_end_date"]
            ).normalize(),
            "price_scale": price_scale,
            "volume_scale": volume_scale,
            "coverage": coverage,
            "overlap": overlap,
            "triage_assessment": triage_record.get("assessment"),
        }
        bridges[ticker] = bridge
        diagnostic.update(
            {
                "eligible": True,
                "source_end_date": bridge["source_end_date"].strftime("%Y-%m-%d"),
            }
        )
        diagnostics.append(diagnostic)

    return bridges, diagnostics


def active_bridges_for_signal(
    bridges: dict[str, dict[str, Any]],
    signal_date: str | pd.Timestamp,
) -> dict[str, dict[str, Any]]:
    """Use a bridge only after formal data stops and before its own source end."""
    signal = pd.Timestamp(signal_date).normalize()
    return {
        ticker: bridge
        for ticker, bridge in bridges.items()
        if bridge["last_local_price_date"] < signal <= bridge["source_end_date"]
    }


def apply_bridges_for_signal(
    close: pd.DataFrame,
    dollar_volume: pd.DataFrame,
    bridges: dict[str, dict[str, Any]],
    signal_date: str | pd.Timestamp,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, int]]:
    """Overlay cached evidence in memory through one signal date only."""
    signal = pd.Timestamp(signal_date).normalize()
    overlay_close = close.copy(deep=True)
    overlay_dollar_volume = dollar_volume.copy(deep=True)
    applied_rows: dict[str, int] = {}

    for ticker, bridge in active_bridges_for_signal(bridges, signal).items():
        if ticker not in overlay_close.columns or ticker not in overlay_dollar_volume:
            continue
        source = bridge["source"].copy()
        source["date"] = pd.to_datetime(source["date"]).dt.normalize()
        rows = source.loc[
            source["date"].gt(bridge["last_local_price_date"])
            & source["date"].le(signal)
        ].set_index("date")
        rows = rows.loc[rows.index.intersection(overlay_close.index)]
        if rows.empty:
            continue
        aligned_close = rows["close"].astype(float) * float(bridge["price_scale"])
        aligned_volume = rows["volume"].astype(float) * float(bridge["volume_scale"])
        overlay_close.loc[rows.index, ticker] = aligned_close
        overlay_dollar_volume.loc[rows.index, ticker] = aligned_close * aligned_volume
        applied_rows[ticker] = int(len(rows))

    return overlay_close, overlay_dollar_volume, applied_rows


def _rename_comparison_columns(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.rename(
        columns={
            "legacy_candidate": "baseline_candidate",
            "confirmed_candidate": "bridge_candidate",
            "legacy_selected": "baseline_selected",
            "confirmed_selected": "bridge_selected",
            "legacy_score": "baseline_score",
            "confirmed_score": "bridge_score",
            "legacy_rank": "baseline_rank",
            "confirmed_rank": "bridge_rank",
        }
    )


def bridge_observations(
    signal_date: pd.Timestamp,
    baseline: pd.DataFrame,
    bridge: pd.DataFrame,
    active_bridges: dict[str, dict[str, Any]],
    applied_rows: dict[str, int],
    *,
    top_n: int,
    risk_on: bool,
) -> pd.DataFrame:
    """Report each bridged ticker even when it never becomes a candidate."""
    baseline_top = set(baseline.head(top_n).index.astype(str))
    bridge_top = set(bridge.head(top_n).index.astype(str))
    baseline_rank = baseline["score"].rank(ascending=False, method="min")
    bridge_rank = bridge["score"].rank(ascending=False, method="min")
    rows: list[dict[str, Any]] = []
    for ticker in sorted(active_bridges):
        in_baseline = ticker in baseline.index
        in_bridge = ticker in bridge.index
        rows.append(
            {
                "signal_date": pd.Timestamp(signal_date),
                "ticker": ticker,
                "risk_on": bool(risk_on),
                "applied_source_rows": int(applied_rows.get(ticker, 0)),
                "baseline_candidate": in_baseline,
                "bridge_candidate": in_bridge,
                "baseline_selected": ticker in baseline_top,
                "bridge_selected": ticker in bridge_top,
                "baseline_score": (
                    float(baseline.at[ticker, "score"])
                    if in_baseline
                    else float("nan")
                ),
                "bridge_score": (
                    float(bridge.at[ticker, "score"])
                    if in_bridge
                    else float("nan")
                ),
                "baseline_rank": (
                    float(baseline_rank.at[ticker])
                    if ticker in baseline_rank.index
                    else float("nan")
                ),
                "bridge_rank": (
                    float(bridge_rank.at[ticker])
                    if ticker in bridge_rank.index
                    else float("nan")
                ),
            }
        )
    return pd.DataFrame(rows)


def run_selection_impact_diagnostic(
    *,
    triage_path: str | Path = DEFAULT_TRIAGE_OUTPUT,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    price_dir: str | Path = CLEANED_PRICE_DATA_DIR,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Compare frozen selections only where cached bridge evidence is valid."""
    config = fixed_top3_config()
    load_start = (
        pd.Timestamp(config.start) - pd.Timedelta(days=400)
    ).strftime("%Y-%m-%d")
    close, dollar_volume = load_panel(price_dir, load_start, config.end)
    nasdaq = pd.read_csv(
        NASDAQ_INDEX_FILE,
        index_col="date",
        parse_dates=True,
    )["close"]
    eps = load_eps_history(POINT_IN_TIME_EPS_FILE)
    quarterly = load_quarterly_fundamentals(POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE)
    snapshots = load_universe_snapshots()
    universe = lambda date: universe_as_of(snapshots, date)
    bridges, bridge_diagnostics = load_continuous_bridge_evidence(
        triage_path=triage_path,
        cache_dir=cache_dir,
        price_dir=price_dir,
        benchmark_dates=close.index,
        analysis_end=config.end,
    )
    baseline_close = back_adjust_common_splits(close)
    statuses = {
        ticker: "RESEARCH_ONLY_CONTINUOUS_SOURCE_BRIDGE"
        for ticker in bridges
    }
    signal_rows: list[dict[str, Any]] = []
    detail_frames: list[pd.DataFrame] = []
    observation_frames: list[pd.DataFrame] = []

    for signal_date in scheduled_signal_dates(
        close.index,
        config.start,
        config.end,
        config.signal_frequency,
    ):
        active = active_bridges_for_signal(bridges, signal_date)
        if not active:
            continue
        overlay_raw_close, overlay_dollar_volume, applied_rows = apply_bridges_for_signal(
            close,
            dollar_volume,
            active,
            signal_date,
        )
        if not applied_rows:
            continue
        bridge_close = back_adjust_common_splits(overlay_raw_close)
        symbols = universe(signal_date)
        baseline_scores = score_can_slim_cross_section(
            signal_date,
            baseline_close,
            dollar_volume,
            nasdaq,
            eps,
            config,
            symbols,
            quarterly,
        )
        bridge_scores = score_can_slim_cross_section(
            signal_date,
            bridge_close,
            overlay_dollar_volume,
            nasdaq,
            eps,
            config,
            symbols,
            quarterly,
        )
        risk_on = market_regime_is_on(
            signal_date,
            nasdaq,
            config.market_ma_days,
        )
        comparison, details = compare_scored_cross_sections(
            signal_date,
            baseline_scores,
            bridge_scores,
            config.top_n,
            risk_on,
            statuses,
        )
        comparison.update(
            {
                "baseline_top3": comparison.pop("legacy_top3"),
                "bridge_top3": comparison.pop("confirmed_top3"),
                "baseline_candidate_count": comparison.pop("legacy_candidate_count"),
                "bridge_candidate_count": comparison.pop("confirmed_candidate_count"),
                "active_source_bridge_tickers": "|".join(sorted(active)),
                "applied_source_rows": "|".join(
                    f"{ticker}:{count}" for ticker, count in sorted(applied_rows.items())
                ),
            }
        )
        signal_rows.append(comparison)
        if not details.empty:
            details["active_source_bridge_tickers"] = "|".join(sorted(active))
            detail_frames.append(_rename_comparison_columns(details))
        observation_frames.append(
            bridge_observations(
                signal_date,
                baseline_scores,
                bridge_scores,
                active,
                applied_rows,
                top_n=config.top_n,
                risk_on=risk_on,
            )
        )

    signals = pd.DataFrame(signal_rows)
    details = (
        pd.concat(detail_frames, ignore_index=True)
        if detail_frames
        else pd.DataFrame()
    )
    observations = (
        pd.concat(observation_frames, ignore_index=True)
        if observation_frames
        else pd.DataFrame()
    )
    summary = {
        "status": RESEARCH_STATUS,
        "formal_model_changed": False,
        "research_only": True,
        "analysis_type": "cached-source bridge selection comparison only",
        "signal_count_evaluated": int(len(signals)),
        "raw_top3_changed_signals": int(
            signals["raw_top3_changed"].sum() if not signals.empty else 0
        ),
        "executed_top3_changed_signals": int(
            signals["executed_top3_changed"].sum() if not signals.empty else 0
        ),
        "source_bridge_observation_count": int(len(observations)),
        "source_bridge_candidate_change_count": int(
            (
                observations["baseline_candidate"]
                != observations["bridge_candidate"]
            ).sum()
            if not observations.empty
            else 0
        ),
        "evaluated_signal_dates": (
            [
                value.strftime("%Y-%m-%d")
                for value in pd.to_datetime(signals["signal_date"])
            ]
            if not signals.empty
            else []
        ),
        "eligible_continuous_bridges": [
            {
                key: value
                for key, value in bridge.items()
                if key not in {"source"}
            }
            for _, bridge in sorted(bridges.items())
        ],
        "bridge_diagnostics": bridge_diagnostics,
        "input_fingerprints": can_slim_input_fingerprints(),
        "triage_report_sha256": _sha256(triage_path),
        "warning": (
            "This analysis uses cache-verified public-page data only in memory. "
            "It does not write formal prices, terminal returns, coverage files, "
            "or validation artifacts. It cannot establish prices or terminal "
            "returns after each source bridge ends."
        ),
    }
    return signals, details, observations, summary


def write_selection_impact_outputs(
    signals: pd.DataFrame,
    details: pd.DataFrame,
    observations: pd.DataFrame,
    summary: dict[str, Any],
    *,
    summary_path: str | Path = DEFAULT_SUMMARY_PATH,
    signals_path: str | Path = DEFAULT_SIGNALS_PATH,
    details_path: str | Path = DEFAULT_DETAILS_PATH,
    observations_path: str | Path = DEFAULT_OBSERVATIONS_PATH,
) -> None:
    """Write research-only artifacts outside the formal validation set."""
    for path in (summary_path, signals_path, details_path, observations_path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    signals.to_csv(signals_path, index=False)
    details.to_csv(details_path, index=False)
    observations.to_csv(observations_path, index=False)
    target = Path(summary_path)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    os.replace(temporary, target)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--triage-path", default=str(DEFAULT_TRIAGE_OUTPUT))
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    parser.add_argument("--price-dir", default=CLEANED_PRICE_DATA_DIR)
    parser.add_argument("--summary-path", default=str(DEFAULT_SUMMARY_PATH))
    parser.add_argument("--signals-path", default=str(DEFAULT_SIGNALS_PATH))
    parser.add_argument("--details-path", default=str(DEFAULT_DETAILS_PATH))
    parser.add_argument("--observations-path", default=str(DEFAULT_OBSERVATIONS_PATH))
    args = parser.parse_args()
    signals, details, observations, summary = run_selection_impact_diagnostic(
        triage_path=args.triage_path,
        cache_dir=args.cache_dir,
        price_dir=args.price_dir,
    )
    write_selection_impact_outputs(
        signals,
        details,
        observations,
        summary,
        summary_path=args.summary_path,
        signals_path=args.signals_path,
        details_path=args.details_path,
        observations_path=args.observations_path,
    )
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
