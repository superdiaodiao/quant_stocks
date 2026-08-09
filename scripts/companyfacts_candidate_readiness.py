"""Audit historical readiness against a recipe-bound Company Facts candidate.

The candidate quarterly file is used in memory.  Formal fundamentals and
formal validation artifacts are never written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from scripts.companyfacts_candidate_sensitivity import (
    DEFAULT_CANDIDATE_DIR,
    DEFAULT_SCOPE,
)
from src.conf import (
    POINT_IN_TIME_FUNDAMENTALS_FILE,
    POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE,
    PROJECT_PATH,
)
from src.financial.quarterly_fundamentals import load_quarterly_fundamentals
from src.research.historical_data_audit import backtest_data_readiness


DEFAULT_OUTPUT = Path(PROJECT_PATH) / (
    "output/data_provenance/"
    "companyfacts_q1_fp_guard_candidate_readiness_2026-08-09.json"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(path: Path) -> str:
    return str(path.resolve().relative_to(Path(PROJECT_PATH).resolve()))


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def run(
    *,
    scope_path: Path,
    candidate_dir: Path,
    output: Path,
    start: str,
    end: str,
) -> dict:
    scope = json.loads(scope_path.read_text(encoding="utf-8"))
    rebuild_path = candidate_dir / "rebuild_report.json"
    rebuild = json.loads(rebuild_path.read_text(encoding="utf-8"))
    annual_path = candidate_dir / "annual.csv"
    quarterly_path = candidate_dir / "quarterly.csv"
    expected_manifest = scope["snapshot"]["cache_manifest_sha256"]
    expected_recipe = scope["rebuild_recipe_sha256"]
    if rebuild["inputs"]["cache_manifest_sha256"] != expected_manifest:
        raise ValueError("candidate rebuild cache manifest does not match scope")
    if rebuild["inputs"]["rebuild_recipe_sha256"] != expected_recipe:
        raise ValueError("candidate rebuild recipe does not match scope")

    quarterly = load_quarterly_fundamentals(quarterly_path)
    readiness = backtest_data_readiness(
        start,
        end,
        quarterly_fundamentals=quarterly,
    )
    payload = {
        "format_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "purpose": "companyfacts_q1_fp_guard_candidate_readiness",
        "research_only": True,
        "release_status": "BLOCKED",
        "formal_validation_rerun": False,
        "formal_outputs_written": False,
        "scope": {
            "path": _relative(scope_path),
            "sha256": _sha256(scope_path),
            "snapshot_id": scope["snapshot"]["snapshot_id"],
            "cache_manifest_sha256": expected_manifest,
            "rebuild_recipe_sha256": expected_recipe,
            "parser_sha256": scope["rebuild_recipe"]["parser_sha256"],
        },
        "candidate_files": {
            "annual_path": _relative(annual_path),
            "annual_sha256": _sha256(annual_path),
            "quarterly_path": _relative(quarterly_path),
            "quarterly_sha256": _sha256(quarterly_path),
            "rebuild_report_path": _relative(rebuild_path),
            "rebuild_report_sha256": _sha256(rebuild_path),
        },
        "formal_files_unchanged": {
            "annual_sha256": _sha256(Path(POINT_IN_TIME_FUNDAMENTALS_FILE)),
            "quarterly_sha256": _sha256(
                Path(POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE)
            ),
        },
        "readiness": readiness,
    }
    _atomic_json(output, payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", type=Path, default=DEFAULT_SCOPE)
    parser.add_argument("--candidate-dir", type=Path, default=DEFAULT_CANDIDATE_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--start", default="2021-01-01")
    parser.add_argument("--end", default="2026-07-17")
    args = parser.parse_args()
    payload = run(
        scope_path=args.scope,
        candidate_dir=args.candidate_dir,
        output=args.output,
        start=args.start,
        end=args.end,
    )
    coverage = payload["readiness"]["signal_price_coverage"]
    print(json.dumps({
        "complete": payload["readiness"]["complete"],
        "checks": payload["readiness"]["checks"],
        "unresolved_observable_potential_competitor_symbols": coverage[
            "unresolved_observable_potential_competitor_symbols"
        ],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
