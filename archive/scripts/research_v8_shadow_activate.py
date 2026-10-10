#!/usr/bin/env python3
"""Activate local v8 shadow recording without enabling broker actions."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path


DEFAULT_MANIFEST = Path("output/research_v8_monthly_risk_budget_blend_shadow_summary.json")
DEFAULT_STATE = Path(
    "output/daily/can-slim-v8-monthly-risk-budget-blend-shadow/runtime_state.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def activate(
    manifest_path: Path = DEFAULT_MANIFEST,
    state_path: Path = DEFAULT_STATE,
) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("policy_status") != "FROZEN_FORWARD_ONLY":
        raise ValueError("v8 policy is not frozen")
    if manifest.get("release_status") != "BLOCKED":
        raise ValueError("v8 must remain BLOCKED")
    checks = {}
    for path, expected in manifest["bindings"]["runtime_code"].items():
        local = Path(path)
        checks[path] = local.is_file() and _sha256(local) == expected
    if not checks or not all(checks.values()):
        failed = [path for path, passed in checks.items() if not passed]
        raise ValueError(f"runtime SHA verification failed: {failed}")
    payload = {
        "schema_version": 1,
        "mode": "MANUAL_LOCAL_SHADOW",
        "enabled": True,
        "activated_at_utc": datetime.now(timezone.utc).isoformat(),
        "manifest_path": str(manifest_path),
        "manifest_sha256": _sha256(manifest_path),
        "forward_evidence_start": manifest["forward_evidence_start"],
        "release_status": "BLOCKED",
        "broker_connection_authorized": False,
        "broker_action_authorized": False,
        "github_workflow_enabled": False,
        "launch_agent_installed": False,
        "runtime_code_checks": checks,
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    args = parser.parse_args()
    print(json.dumps(activate(args.manifest, args.state), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
