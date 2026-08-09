#!/usr/bin/env python3
"""Build full-period drawdown and recovery diagnostics from daily returns."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd


EVIDENCE_START = "2021-01-01"
TOLERANCE = 1e-12


def clean_record(record: dict) -> dict:
    return {
        key: (
            None
            if pd.isna(value)
            else value.item() if hasattr(value, "item") else value
        )
        for key, value in record.items()
    }


def drawdown_episodes(returns: pd.Series) -> tuple[pd.DataFrame, dict]:
    returns = pd.to_numeric(returns, errors="coerce").fillna(0.0)
    returns = returns.sort_index()
    equity = (1 + returns).cumprod()
    if equity.empty:
        return pd.DataFrame(), {
            "sessions": 0,
            "maximum_drawdown": None,
            "time_underwater_fraction": None,
            "currently_underwater": False,
        }
    peak_value = float(equity.iloc[0])
    peak_date = pd.Timestamp(equity.index[0])
    active = None
    episodes = []
    underwater_sessions = 0
    previous_date = peak_date
    for session_number, (date, value) in enumerate(
        equity.items(), start=1
    ):
        date = pd.Timestamp(date)
        value = float(value)
        if value >= peak_value * (1 - TOLERANCE):
            if active is not None:
                active["underwater_end"] = previous_date.strftime("%Y-%m-%d")
                active["recovery_date"] = date.strftime("%Y-%m-%d")
                active["recovered"] = True
                active["peak_to_recovery_sessions"] = (
                    session_number - active["peak_session_number"]
                )
                active["peak_to_recovery_calendar_days"] = int(
                    (date - pd.Timestamp(active["peak_date"])).days
                )
                episodes.append(active)
                active = None
            if value > peak_value:
                peak_value = value
                peak_date = date
            previous_date = date
            continue
        drawdown = value / peak_value - 1
        underwater_sessions += 1
        if active is None:
            active = {
                "peak_date": peak_date.strftime("%Y-%m-%d"),
                "peak_session_number": session_number - 1,
                "underwater_start": date.strftime("%Y-%m-%d"),
                "trough_date": date.strftime("%Y-%m-%d"),
                "maximum_drawdown": drawdown,
                "underwater_sessions": 1,
            }
        else:
            active["underwater_sessions"] += 1
            if drawdown < active["maximum_drawdown"]:
                active["maximum_drawdown"] = drawdown
                active["trough_date"] = date.strftime("%Y-%m-%d")
        previous_date = date
    if active is not None:
        active["underwater_end"] = previous_date.strftime("%Y-%m-%d")
        active["recovery_date"] = None
        active["recovered"] = False
        active["peak_to_recovery_sessions"] = (
            len(equity) - active["peak_session_number"]
        )
        active["peak_to_recovery_calendar_days"] = int(
            (
                previous_date - pd.Timestamp(active["peak_date"])
            ).days
        )
        episodes.append(active)
    frame = pd.DataFrame(episodes)
    if not frame.empty:
        frame = frame.drop(columns=["peak_session_number"])
        maximum = clean_record(
            frame.loc[frame["maximum_drawdown"].idxmin()].to_dict()
        )
        longest = clean_record(
            frame.loc[
                frame["underwater_sessions"].idxmax()
            ].to_dict()
        )
    else:
        maximum = longest = None
    current_drawdown = float(equity.iloc[-1] / equity.cummax().iloc[-1] - 1)
    current = (
        clean_record(frame.iloc[-1].to_dict())
        if not frame.empty and not bool(frame.iloc[-1]["recovered"])
        else None
    )
    summary = {
        "evidence_start": pd.Timestamp(equity.index[0]).strftime("%Y-%m-%d"),
        "evidence_end": pd.Timestamp(equity.index[-1]).strftime("%Y-%m-%d"),
        "sessions": int(len(equity)),
        "ending_growth_of_one": float(equity.iloc[-1]),
        "maximum_drawdown": (
            float(frame["maximum_drawdown"].min())
            if not frame.empty else 0.0
        ),
        "maximum_drawdown_episode": maximum,
        "longest_underwater_episode": longest,
        "time_underwater_fraction": (
            underwater_sessions / len(equity)
        ),
        "currently_underwater": current is not None,
        "current_drawdown": current_drawdown,
        "current_underwater_episode": current,
        "drawdown_episode_count": int(len(frame)),
    }
    return frame, summary


def build_path_risk_report(
    backtest: pd.DataFrame,
    evidence_start: str = EVIDENCE_START,
) -> tuple[pd.DataFrame, dict]:
    frame = backtest.copy()
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame.loc[frame["date"] >= pd.Timestamp(evidence_start)].set_index(
        "date"
    )
    all_episodes = []
    summary = {
        "status": "RESEARCH_ONLY",
        "method": (
            "Daily close-to-close equity drawdowns over the fixed evidence "
            "period. Recovery means regaining the prior equity high."
        ),
    }
    for label, column in (
        ("strategy", "strategy"),
        ("nasdaq", "benchmark"),
    ):
        episodes, diagnostics = drawdown_episodes(frame[column])
        if not episodes.empty:
            episodes.insert(0, "series", label)
            all_episodes.append(episodes)
        summary[label] = diagnostics
    return (
        pd.concat(all_episodes, ignore_index=True)
        if all_episodes else pd.DataFrame(),
        summary,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backtest",
        default="output/can_slim_fixed_top3_backtest.csv",
    )
    parser.add_argument(
        "--output-json",
        default="output/can_slim_fixed_top3_path_risk.json",
    )
    parser.add_argument(
        "--output-csv",
        default="output/can_slim_fixed_top3_drawdown_episodes.csv",
    )
    parser.add_argument("--evidence-start", default=EVIDENCE_START)
    args = parser.parse_args()
    backtest_path = Path(args.backtest)
    episodes, summary = build_path_risk_report(
        pd.read_csv(backtest_path),
        evidence_start=args.evidence_start,
    )
    summary["input_backtest"] = {
        "path": str(backtest_path),
        "sha256": hashlib.sha256(backtest_path.read_bytes()).hexdigest(),
        "bytes": backtest_path.stat().st_size,
    }
    output_json = Path(args.output_json)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    episodes.to_csv(args.output_csv, index=False)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
