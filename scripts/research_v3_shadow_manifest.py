"""Create the immutable daily-recommendation manifest for research-v3."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from src.conf import PROJECT_PATH


DEFAULT_EVIDENCE = Path(PROJECT_PATH) / (
    "output/data_provenance/research_v3_fresh_top3_2026-08-10.json"
)
DEFAULT_OUTPUT = Path(PROJECT_PATH) / (
    "output/research_v3_fresh_top3_shadow_summary.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_manifest(evidence: dict, evidence_path: Path) -> dict:
    if evidence.get("promotion_eligible") is not False:
        raise ValueError("challenger evidence must remain non-promoted")
    config = dict(evidence["configuration"])
    start = evidence["shadow_policy"]["forward_start"]
    return {
        "format_version": 1,
        "model_version": evidence["model_version"],
        "policy_status": "FROZEN_SHADOW_CHALLENGER",
        "release_status": "BLOCKED",
        "release_reason": "Historical diagnostic is selection-contaminated; forward record is empty.",
        "parameter_update_frequency": "frozen",
        "signal_frequency": config["signal_frequency"],
        "uses_quarterly_fundamentals": True,
        "uses_adaptive_channel": False,
        "current_shadow_config_ids": [0],
        "current_shadow_configs": [config],
        "model_snapshots": [
            {
                "effective_start": start,
                "effective_end": "2099-12-31",
                "training_end": "2026-08-09",
                "config_ids": [0],
                "configs": [config],
            }
        ],
        "forward_evidence_start": start,
        "promotion_policy": evidence["shadow_policy"],
        "promotion_eligible": False,
        "observed_forward_months": 0,
        "source_evidence": {
            "path": str(evidence_path),
            "sha256": _sha256(evidence_path),
            "data_manifest_sha256": evidence["data_manifest"]["sha256"],
            "strategy_code_sha256": evidence["strategy_code_fingerprint"][
                "sha256"
            ],
        },
    }


def invalidate_for_data_sensitivity(
    manifest: dict,
    sensitivity: dict,
    sensitivity_path: Path,
) -> dict:
    if sensitivity.get("purpose") != "historical_data_sensitivity":
        raise ValueError("invalidation evidence must be a data sensitivity")
    quarterly_input = sensitivity.get("quarterly_input") or {}
    if quarterly_input.get("is_formal_input") is not False:
        raise ValueError("invalidation evidence must use a non-formal input")
    diagnostic = sensitivity.get("historical_diagnostic") or {}
    if diagnostic.get("eligible_for_promotion") is not False:
        raise ValueError("data sensitivity must remain non-promoted")
    invalidated = dict(manifest)
    invalidated.update(
        {
            "policy_status": "INVALIDATED_DATA_RELIABILITY",
            "release_status": "BLOCKED",
            "release_reason": (
                "Proven-only SEC financial replay reduced the historical "
                "diagnostic to "
                f"{diagnostic.get('wins_vs_nasdaq')}/"
                f"{diagnostic.get('years')}; challenger withdrawn before "
                "any forward observation."
            ),
            "promotion_eligible": False,
            "current_shadow_config_ids": [],
            "current_shadow_configs": [],
            "model_snapshots": [],
            "invalidation_evidence": {
                "path": str(sensitivity_path),
                "sha256": _sha256(sensitivity_path),
                "quarterly_input_sha256": quarterly_input.get("sha256"),
                "wins_vs_nasdaq": diagnostic.get("wins_vs_nasdaq"),
                "cost_stress_wins": diagnostic.get("cost_stress_wins"),
            },
        }
    )
    return invalidated


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--data-sensitivity-evidence", type=Path)
    args = parser.parse_args()
    evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
    manifest = build_manifest(evidence, args.evidence)
    if args.data_sensitivity_evidence is not None:
        sensitivity = json.loads(
            args.data_sensitivity_evidence.read_text(encoding="utf-8")
        )
        manifest = invalidate_for_data_sensitivity(
            manifest,
            sensitivity,
            args.data_sensitivity_evidence,
        )
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(args.output)
    print(json.dumps({
        "model_version": manifest["model_version"],
        "forward_evidence_start": manifest["forward_evidence_start"],
        "release_status": manifest["release_status"],
    }, indent=2))


if __name__ == "__main__":
    main()
