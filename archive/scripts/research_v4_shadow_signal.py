"""Record a post-freeze v4 shadow signal without touching formal v1 output."""

from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
from pathlib import Path

import pandas as pd

from src.research.can_slim_daily_recommendations import (
    generate_can_slim_shadow_recommendations,
    save_can_slim_shadow_recommendations,
)
from src.research.shadow_ledger import write_shadow_ledger_manifest


MODEL_VERSION = "can-slim-v4-cost-robust-top10-shadow"
DEFAULT_SUMMARY = Path("output/research_v4_cost_robust_top10_shadow_summary.json")
DEFAULT_OUTPUT_DIR = Path("output/daily")


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def record_signal(
    *,
    decision_date: date | pd.Timestamp | None = None,
    summary_path: str | Path = DEFAULT_SUMMARY,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
) -> dict:
    summary_path = Path(summary_path)
    summary = json.loads(summary_path.read_text())
    if summary.get("model_version") != MODEL_VERSION:
        raise ValueError("unexpected v4 shadow model version")
    if summary.get("policy_status") != "FROZEN_FORWARD_ONLY":
        raise ValueError("v4 shadow policy is not frozen")
    if summary.get("release_status") != "BLOCKED":
        raise ValueError("v4 shadow runner must not be production eligible")

    frozen_bindings = {
        "summary_path": str(summary_path.resolve()),
        "summary_sha256": _sha256(summary_path),
        "data_manifest_sha256": summary["source_evidence"][
            "data_manifest_sha256"
        ],
        "strategy_code_sha256": summary["source_evidence"][
            "strategy_code_sha256"
        ],
        "quarterly_input_sha256": summary["quarterly_input"]["sha256"],
    }

    model_dir = Path(output_dir) / MODEL_VERSION
    history_path = model_dir / "recommendation_history.csv"
    recommendations, metadata = generate_can_slim_shadow_recommendations(
        decision_date=decision_date,
        summary_file=summary_path,
        history_file=history_path,
        refresh_parameters=False,
    )
    reasons = set(recommendations["action_reason"].astype(str))
    if metadata.get("model_snapshot_effective_start") is None:
        if reasons != {"MODEL_NOT_YET_EFFECTIVE_AT_EXECUTION"}:
            raise ValueError("missing active v4 snapshot without the expected reason")
        return {
            "status": "WAITING_FOR_FIRST_POST_FREEZE_SIGNAL",
            "written": False,
            "as_of": metadata["as_of"],
            "signal_date": metadata["signal_date"],
            "forward_evidence_start": summary["forward_evidence_start"],
            "model_version": MODEL_VERSION,
            "release_status": "BLOCKED",
            "promotion_eligible": False,
            "frozen_bindings": frozen_bindings,
        }

    signal_date = pd.Timestamp(metadata["signal_date"])
    if signal_date < pd.Timestamp(summary["forward_evidence_start"]):
        raise ValueError("refusing to backfill a pre-freeze v4 signal")
    recommendation_path = save_can_slim_shadow_recommendations(
        recommendations, metadata, output_dir
    )
    manifest_path = write_shadow_ledger_manifest(history_path)
    return {
        "status": "RECORDED_LOCAL_SHADOW_SIGNAL",
        "written": True,
        "as_of": metadata["as_of"],
        "signal_date": metadata["signal_date"],
        "model_version": MODEL_VERSION,
        "recommendation_path": str(recommendation_path),
        "history_path": str(history_path),
        "manifest_path": str(manifest_path),
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "external_anchor": False,
        "frozen_bindings": frozen_bindings,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Record the next eligible local research-v4 shadow signal."
    )
    parser.add_argument("--decision-date", type=pd.Timestamp)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    print(
        json.dumps(
            record_signal(
                decision_date=args.decision_date,
                summary_path=args.summary,
                output_dir=args.output_dir,
            ),
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
