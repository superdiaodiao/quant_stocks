#!/usr/bin/env python3
"""Run unfrozen v14 adaptive pretraining with strict research-only status."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from src.conf import CLEANED_PRICE_DATA_DIR
from src.research.can_slim_walk_forward import run_walk_forward


DEFAULT_QUARTERLY = Path(
    "output/research_only/v14/candidate_fundamentals/quarterly.csv"
)
DEFAULT_SNAPSHOT_DIR = Path("output/research_only/v14/universe_snapshots")
DEFAULT_DATA_AUDIT = Path(
    "output/research_only/v14/candidate_path_audit_after_companyfacts.json"
)
DEFAULT_OUTPUT_DIR = Path("output/research_only/v14/adaptive_pretrain")
DEFAULT_PRICE_DIR = Path(CLEANED_PRICE_DATA_DIR)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _price_directory_binding(path: Path) -> dict:
    digest = hashlib.sha256()
    files = sorted(path.glob("*.csv"))
    for price_file in files:
        digest.update(price_file.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256(price_file).encode("ascii"))
        digest.update(b"\n")
    return {
        "path": str(path),
        "file_count": len(files),
        "content_manifest_sha256": digest.hexdigest(),
    }


def research_summary(walk_forward: dict, data_audit: dict) -> dict:
    historical_status = walk_forward.get("release_status")
    return {
        **walk_forward,
        "schema_version": 1,
        "research_only": True,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "historical_diagnostic_status": historical_status,
        "data_gates": data_audit["gates"],
        "parameters_frozen": False,
        "forward_observation_months": 0,
        "interpretation_guardrail": (
            "This run may validate adaptive mechanics and expose sensitivity. "
            "Its selected parameters are not a frozen strategy and must be "
            "refit with the identical predeclared procedure after data gates pass."
        ),
    }


def run(
    *,
    quarterly_path: Path = DEFAULT_QUARTERLY,
    snapshot_dir: Path = DEFAULT_SNAPSHOT_DIR,
    data_audit_path: Path = DEFAULT_DATA_AUDIT,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    price_dir: Path = DEFAULT_PRICE_DIR,
    artifact_tag: str = "research_v14_pretrain",
) -> dict:
    data_audit = json.loads(data_audit_path.read_text(encoding="utf-8"))
    if not data_audit["gates"].get("research_pretraining_allowed"):
        raise ValueError("candidate-path audit does not allow research pretraining")
    candidates, walk, raw_summary = run_walk_forward(
        signal_frequency="monthly",
        artifact_suffix=f"_{artifact_tag}",
        use_quarterly_fundamentals=True,
        adaptive_channel=False,
        maximum_financial_age_days=(150, 365, 550),
        quarterly_path=quarterly_path,
        universe_snapshot_dir=snapshot_dir,
        allow_no_evidence_fallback=False,
        price_dir=price_dir,
    )
    summary = research_summary(raw_summary, data_audit)
    summary["input_bindings"] = {
        "artifact_tag": artifact_tag,
        "quarterly": {
            "path": str(quarterly_path), "sha256": _sha256(quarterly_path)
        },
        "candidate_path_audit": {
            "path": str(data_audit_path), "sha256": _sha256(data_audit_path)
        },
        "snapshot_dir": str(snapshot_dir),
        "snapshot_file_count": len(list(snapshot_dir.glob("nasdaq_listed_*.csv"))),
        "price_directory": _price_directory_binding(price_dir),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    candidates_path = output_dir / "candidate_annual_results.csv"
    walk_path = output_dir / "walk_forward_annual.csv"
    summary_path = output_dir / "summary.json"
    candidates.to_csv(candidates_path, index=False)
    walk.to_csv(walk_path, index=False)
    summary["outputs"] = {
        "candidate_annual_results": str(candidates_path),
        "walk_forward_annual": str(walk_path),
        "summary": str(summary_path),
    }
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quarterly", type=Path, default=DEFAULT_QUARTERLY)
    parser.add_argument("--snapshot-dir", type=Path, default=DEFAULT_SNAPSHOT_DIR)
    parser.add_argument("--data-audit", type=Path, default=DEFAULT_DATA_AUDIT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--price-dir", type=Path, default=DEFAULT_PRICE_DIR)
    parser.add_argument(
        "--artifact-tag", default="research_v14_pretrain",
        help="Unique provenance tag for intermediate walk-forward outputs.",
    )
    args = parser.parse_args()
    result = run(
        quarterly_path=args.quarterly,
        snapshot_dir=args.snapshot_dir,
        data_audit_path=args.data_audit,
        output_dir=args.output_dir,
        price_dir=args.price_dir,
        artifact_tag=args.artifact_tag,
    )
    print(json.dumps({
        "summary": result["outputs"]["summary"],
        "candidate_count": result["candidate_count"],
        "out_of_sample_years": result["out_of_sample_years"],
        "wins_vs_nasdaq": result["wins_vs_nasdaq"],
        "historical_diagnostic_status": result["historical_diagnostic_status"],
        "release_status": result["release_status"],
        "promotion_eligible": result["promotion_eligible"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
