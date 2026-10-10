"""Freeze the research-v4 configuration for future-only shadow evidence."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile


MODEL_VERSION = "can-slim-v4-cost-robust-top10-shadow"


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", dir=path.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


def create_manifest(evidence_path: Path, path_risk_path: Path) -> dict:
    evidence = json.loads(evidence_path.read_text())
    path_risk = json.loads(path_risk_path.read_text())
    if evidence.get("model_version") != MODEL_VERSION:
        raise ValueError("unexpected v4 model version")
    if evidence.get("release_status") != "BLOCKED":
        raise ValueError("v4 research evidence must remain BLOCKED")
    if evidence.get("promotion_eligible") is not False:
        raise ValueError("v4 research evidence cannot already be promotion eligible")
    if evidence.get("historical_selection_contaminated") is not True:
        raise ValueError("v4 historical selection must be disclosed as contaminated")
    audit = evidence["selected_data_audit"]
    if audit["positions_with_missing_holding_prices"] != 0:
        raise ValueError("v4 selected positions have missing holding prices")
    if audit["positions_with_unresolved_terminal_return"] != 0:
        raise ValueError("v4 selected positions have unresolved terminal returns")
    if set(evidence["historical_diagnostic"]["cost_stress_wins"].values()) != {4}:
        raise ValueError("v4 did not retain four wins at every declared cost")
    daily = evidence["artifact_bindings"]["daily"]
    if path_risk["input_backtest"]["sha256"] != daily["sha256"]:
        raise ValueError("path-risk report does not bind the v4 daily artifact")
    policy = evidence["shadow_policy"]
    return {
        "format_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "model_version": MODEL_VERSION,
        "policy_status": "FROZEN_FORWARD_ONLY",
        "release_status": "BLOCKED",
        "release_reason": (
            "The cost-robust Top 10 configuration was selected after historical "
            "inspection.  Historical results are diagnostic only; promotion "
            "requires a new forward shadow record."
        ),
        "promotion_eligible": False,
        "forward_evidence_start": policy["forward_start"],
        "observed_forward_months": 0,
        "current_shadow_config_ids": [15],
        "current_shadow_configs": [evidence["configuration"]],
        "model_snapshots": [
            {
                "effective_start": policy["forward_start"],
                "effective_end": None,
                "config_ids": [15],
                "configs": [evidence["configuration"]],
            }
        ],
        "promotion_policy": policy,
        "quarterly_input": evidence["quarterly_input"],
        "historical_diagnostic": evidence["historical_diagnostic"],
        "selected_data_audit": audit,
        "risk_diagnostic": {
            "maximum_drawdown": path_risk["strategy"]["maximum_drawdown"],
            "current_drawdown": path_risk["strategy"]["current_drawdown"],
            "time_underwater_fraction": path_risk["strategy"][
                "time_underwater_fraction"
            ],
        },
        "source_evidence": {
            "path": str(evidence_path.resolve()),
            "sha256": _sha256(evidence_path),
            "data_manifest_sha256": evidence["data_manifest"]["sha256"],
            "strategy_code_sha256": evidence["strategy_code_fingerprint"][
                "sha256"
            ],
        },
        "selection_evidence": evidence["selection_evidence"],
        "path_risk_evidence": {
            "path": str(path_risk_path.resolve()),
            "sha256": _sha256(path_risk_path),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Freeze the research-v4 future-only shadow manifest."
    )
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--path-risk", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = create_manifest(args.evidence, args.path_risk)
    _atomic_json(args.output, payload)
    print(
        json.dumps(
            {
                "model_version": payload["model_version"],
                "policy_status": payload["policy_status"],
                "release_status": payload["release_status"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
