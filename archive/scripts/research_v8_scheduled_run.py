#!/usr/bin/env python3
"""Refresh isolated market data and run one authorized local v8 shadow check."""

from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
from pathlib import Path

from scripts.research_v6_market_refresh import refresh
from scripts.research_v6_scheduled_run import latest_completed_session
from scripts.research_v8_observe import observe


DEFAULT_MANIFEST = Path("output/research_v8_monthly_risk_budget_blend_shadow_summary.json")
DEFAULT_STATE = Path("output/daily/can-slim-v8-monthly-risk-budget-blend-shadow/runtime_state.json")
DEFAULT_MODEL_DIR = DEFAULT_STATE.parent
DEFAULT_ROOT = Path("output/research_only/v6_market")
DEFAULT_QQQ = Path("output/research_only/qqq_nasdaq_history.csv")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def scheduled_run(
    *,
    local_date: str,
    manifest_path: Path = DEFAULT_MANIFEST,
    state_path: Path = DEFAULT_STATE,
    model_dir: Path = DEFAULT_MODEL_DIR,
    root: Path = DEFAULT_ROOT,
    qqq_path: Path = DEFAULT_QQQ,
    workers: int = 16,
) -> dict:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if not state.get("enabled") or state.get("mode") != "MANUAL_LOCAL_SHADOW":
        raise ValueError("v8 local shadow runtime is not enabled")
    if state.get("manifest_sha256") != _sha256(manifest_path):
        raise ValueError("runtime state does not bind the current v8 manifest")
    expected = latest_completed_session(local_date)
    market = refresh(
        expected_session=expected, summary_path=manifest_path,
        root=root, qqq_path=qqq_path, workers=workers,
    )
    if not market["readiness"]["ready_for_v6_signal"]:
        observation = None
        status = "MARKET_DATA_NOT_READY"
    else:
        observation = observe(
            as_of=expected, manifest_path=manifest_path,
            model_dir=model_dir, qqq_path=qqq_path, root=root,
        )
        status = "COMPLETED_RESEARCH_ONLY_V8_SHADOW_RUN"
    payload = {
        "status": status,
        "expected_session": expected.strftime("%Y-%m-%d"),
        "market": market,
        "observation": observation,
        "release_status": "BLOCKED",
        "broker_action_authorized": False,
    }
    output = model_dir / "latest_scheduled_run.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    payload["output"] = str(output)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-date", default=date.today().isoformat())
    parser.add_argument("--workers", type=int, default=16)
    args = parser.parse_args()
    print(json.dumps(scheduled_run(local_date=args.local_date, workers=args.workers), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
