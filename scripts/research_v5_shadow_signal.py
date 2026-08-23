#!/usr/bin/env python3
"""Record a post-freeze v5 allocation decision without backfilling warmup."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import pandas as pd

from src.research.shadow_evaluation import nasdaq_calendar_for_year


MODEL_VERSION = "can-slim-v5-qqq-relative-trend-core-shadow"
V4_MODEL_VERSION = "can-slim-v4-cost-robust-top10-shadow"
DEFAULT_SUMMARY = Path("output/research_v5_qqq_relative_trend_core_shadow_summary.json")
DEFAULT_V4_HISTORY = Path("output/daily") / V4_MODEL_VERSION / "recommendation_history.csv"
DEFAULT_STATE_HISTORY = Path("output/daily") / MODEL_VERSION / "relative_strength_history.csv"
DEFAULT_QQQ = Path("output/research_only/qqq_nasdaq_history.csv")
DEFAULT_OUTPUT_DIR = Path("output/daily")


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _last_nasdaq_session_of_month(stamp: pd.Timestamp) -> pd.Timestamp:
    calendar = nasdaq_calendar_for_year(stamp.year)
    start = stamp.to_period("M").start_time
    end = stamp.to_period("M").end_time.normalize()
    sessions = calendar.sessions_in_range(start, end)
    if not len(sessions):
        raise ValueError(f"no Nasdaq sessions in {stamp:%Y-%m}")
    return pd.Timestamp(sessions[-1]).tz_localize(None).normalize()


def _nasdaq_sessions_between(
    start: pd.Timestamp, end: pd.Timestamp
) -> pd.DatetimeIndex:
    """Return normalized Nasdaq sessions across every year in the interval."""
    sessions = []
    for year in range(start.year, end.year + 1):
        calendar = nasdaq_calendar_for_year(year)
        year_start = max(start, pd.Timestamp(year=year, month=1, day=1))
        year_end = min(end, pd.Timestamp(year=year, month=12, day=31))
        sessions.extend(calendar.sessions_in_range(year_start, year_end))
    return pd.DatetimeIndex(sessions).tz_localize(None).normalize()


def _atomic_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(temporary, index=False)
    os.replace(temporary, path)


def record_signal(
    *,
    decision_date: str | pd.Timestamp,
    summary_path: str | Path = DEFAULT_SUMMARY,
    v4_history_path: str | Path = DEFAULT_V4_HISTORY,
    state_history_path: str | Path = DEFAULT_STATE_HISTORY,
    qqq_path: str | Path = DEFAULT_QQQ,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
) -> dict:
    decision = pd.Timestamp(decision_date).normalize()
    summary_path = Path(summary_path)
    v4_history_path = Path(v4_history_path)
    state_history_path = Path(state_history_path)
    qqq_path = Path(qqq_path)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary.get("model_version") != MODEL_VERSION:
        raise ValueError("unexpected v5 shadow model version")
    if summary.get("policy_status") != "FROZEN_FORWARD_ONLY":
        raise ValueError("v5 shadow policy is not frozen")
    if summary.get("release_status") != "BLOCKED":
        raise ValueError("v5 shadow must remain BLOCKED")
    if decision < pd.Timestamp(summary["forward_evidence_start"]):
        raise ValueError("refusing to record a pre-freeze v5 signal")
    expected_signal = _last_nasdaq_session_of_month(decision)
    if decision != expected_signal:
        return {
            "status": "WAITING_FOR_MONTH_END_SIGNAL",
            "written": False,
            "decision_date": decision.strftime("%Y-%m-%d"),
            "expected_signal_date": expected_signal.strftime("%Y-%m-%d"),
            "model_version": MODEL_VERSION,
            "release_status": "BLOCKED",
        }
    if not state_history_path.is_file():
        return {
            "status": "WAITING_FOR_RELATIVE_STRENGTH_WARMUP",
            "written": False,
            "decision_date": decision.strftime("%Y-%m-%d"),
            "observed_return_intervals": 0,
            "required_return_intervals": 63,
            "model_version": MODEL_VERSION,
            "release_status": "BLOCKED",
        }
    state = pd.read_csv(state_history_path, parse_dates=["date"])
    required = {"date", "v4_nav", "qqq_total_return_nav"}
    missing = required - set(state.columns)
    if missing:
        raise ValueError(f"v5 state history missing columns: {sorted(missing)}")
    state = state.loc[
        state["date"].between(
            pd.Timestamp(summary["forward_evidence_start"]), decision
        )
    ].sort_values("date").drop_duplicates("date", keep="first")
    if state.empty or state["date"].iloc[-1] != decision or len(state) < 64:
        return {
            "status": "WAITING_FOR_RELATIVE_STRENGTH_WARMUP",
            "written": False,
            "decision_date": decision.strftime("%Y-%m-%d"),
            "observed_return_intervals": max(len(state) - 1, 0),
            "required_return_intervals": 63,
            "model_version": MODEL_VERSION,
            "release_status": "BLOCKED",
        }
    trailing = state.tail(64)
    expected_dates = _nasdaq_sessions_between(
        trailing["date"].iloc[0], decision
    )
    if len(expected_dates) != 64 or not trailing["date"].reset_index(drop=True).equals(
        pd.Series(expected_dates)
    ):
        return {
            "status": "WAITING_FOR_CONTIGUOUS_RELATIVE_STRENGTH_WARMUP",
            "written": False,
            "decision_date": decision.strftime("%Y-%m-%d"),
            "observed_return_intervals": max(len(state) - 1, 0),
            "required_contiguous_return_intervals": 63,
            "model_version": MODEL_VERSION,
            "release_status": "BLOCKED",
        }
    v4_return = float(trailing["v4_nav"].iloc[-1] / trailing["v4_nav"].iloc[0] - 1.0)
    qqq_return = float(
        trailing["qqq_total_return_nav"].iloc[-1]
        / trailing["qqq_total_return_nav"].iloc[0]
        - 1.0
    )
    qqq = pd.read_csv(qqq_path, parse_dates=["date"]).sort_values("date")
    qqq = qqq.loc[qqq["date"].le(decision)].tail(200)
    if len(qqq) < 200 or qqq["date"].iloc[-1] != decision:
        raise ValueError("QQQ history lacks the 200-session trend window through decision date")
    qqq_trend_on = bool(qqq["close"].iloc[-1] > qqq["close"].mean())
    v4_leads = v4_return >= qqq_return
    if v4_leads:
        satellite_weight, core_weight = 1.0, 0.0
    elif qqq_trend_on:
        satellite_weight, core_weight = 0.5, 0.5
    else:
        satellite_weight, core_weight = 0.5, 0.0
    if not v4_history_path.is_file():
        raise ValueError("v4 shadow recommendation history is missing")
    v4 = pd.read_csv(v4_history_path)
    v4 = v4.loc[
        v4["model_version"].astype(str).eq(V4_MODEL_VERSION)
        & pd.to_datetime(v4["signal_date"]).eq(decision)
    ].copy()
    if v4.empty:
        raise ValueError("same-day frozen v4 recommendation is missing")
    v4["target_weight"] = pd.to_numeric(v4["target_weight"], errors="raise")
    v4 = v4.loc[v4["target_weight"].gt(0), ["ticker", "target_weight"]]
    rows = []
    for position in v4.itertuples(index=False):
        rows.append({
            "signal_date": decision.strftime("%Y-%m-%d"),
            "ticker": str(position.ticker),
            "target_weight": float(position.target_weight) * satellite_weight,
            "sleeve": "v4",
        })
    if core_weight > 0:
        rows.append({
            "signal_date": decision.strftime("%Y-%m-%d"),
            "ticker": "QQQ",
            "target_weight": core_weight,
            "sleeve": "core",
        })
    target_exposure = sum(row["target_weight"] for row in rows)
    if target_exposure < 1.0 - 1e-12:
        rows.append({
            "signal_date": decision.strftime("%Y-%m-%d"),
            "ticker": "__CASH__",
            "target_weight": 1.0 - target_exposure,
            "sleeve": "cash",
        })
    recommendations = pd.DataFrame(rows)
    recommendations["model_version"] = MODEL_VERSION
    recommendations["release_status"] = "BLOCKED"
    recommendations["promotion_eligible"] = False
    recommendations["v4_trailing_63_session_return"] = v4_return
    recommendations["qqq_trailing_63_session_total_return"] = qqq_return
    recommendations["qqq_200_session_trend_on"] = qqq_trend_on
    recommendations["frozen_summary_sha256"] = _sha256(summary_path)
    model_dir = Path(output_dir) / MODEL_VERSION
    output = model_dir / f"recommendations_{decision:%Y-%m-%d}.csv"
    history_path = model_dir / "recommendation_history.csv"
    if output.is_file():
        existing = pd.read_csv(output)
        pd.testing.assert_frame_equal(existing, recommendations, check_dtype=False)
        return {
            "status": "ALREADY_RECORDED",
            "written": False,
            "output": str(output),
            "model_version": MODEL_VERSION,
            "release_status": "BLOCKED",
        }
    history = pd.read_csv(history_path) if history_path.is_file() else pd.DataFrame()
    combined = pd.concat([history, recommendations], ignore_index=True)
    _atomic_csv(output, recommendations)
    _atomic_csv(history_path, combined)
    return {
        "status": "RECORDED_LOCAL_V5_SHADOW_SIGNAL",
        "written": True,
        "signal_date": decision.strftime("%Y-%m-%d"),
        "satellite_weight": satellite_weight,
        "core_weight": core_weight,
        "cash_weight": 1.0 - satellite_weight - core_weight,
        "output": str(output),
        "history": str(history_path),
        "model_version": MODEL_VERSION,
        "release_status": "BLOCKED",
        "external_anchor": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decision-date", required=True)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--v4-history", type=Path, default=DEFAULT_V4_HISTORY)
    parser.add_argument("--state-history", type=Path, default=DEFAULT_STATE_HISTORY)
    parser.add_argument("--qqq", type=Path, default=DEFAULT_QQQ)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    print(json.dumps(record_signal(
        decision_date=args.decision_date,
        summary_path=args.summary,
        v4_history_path=args.v4_history,
        state_history_path=args.state_history,
        qqq_path=args.qqq,
        output_dir=args.output_dir,
    ), indent=2))


if __name__ == "__main__":
    main()
