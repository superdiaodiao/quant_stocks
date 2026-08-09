import hashlib
import json
from pathlib import Path

from src.conf import PROJECT_PATH


EVIDENCE = Path(PROJECT_PATH) / (
    "output/data_provenance/"
    "companyfacts_q1_fp_guard_candidate_readiness_2026-08-09.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_q1_fp_guard_candidate_closes_observable_selection_competitors() -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    candidate = evidence["candidate_files"]
    readiness = evidence["readiness"]
    coverage = readiness["signal_price_coverage"]

    assert evidence["research_only"] is True
    assert evidence["release_status"] == "BLOCKED"
    assert evidence["formal_validation_rerun"] is False
    assert evidence["formal_outputs_written"] is False
    assert _sha256(Path(PROJECT_PATH) / candidate["annual_path"]) == candidate[
        "annual_sha256"
    ]
    assert _sha256(Path(PROJECT_PATH) / candidate["quarterly_path"]) == candidate[
        "quarterly_sha256"
    ]
    assert evidence["formal_files_unchanged"] == {
        "annual_sha256": (
            "62f6c624b2fac85118ea6d49646870f1a56fa053687cb617dc468d856e19c34d"
        ),
        "quarterly_sha256": (
            "1be16a6342217d6771eca7d2ca49156e726b424ebfbfe7b90dfa6c232ea8bf69"
        ),
    }

    assert readiness["complete"] is False
    assert readiness["checks"]["observed_delisting_returns_complete"] is False
    assert readiness["checks"]["signal_member_financials_complete"] is False
    assert coverage["unresolved_observable_potential_competitor_symbols"] == []
    assert coverage["confirmed_terminal_before_signal_symbols"] == ["APLS", "PPBI"]
    passing = {
        row["signal_date"]: row
        for row in coverage["by_signal"]
        if row["missing_passing_financial_screen_count"]
    }
    assert passing["2023-08-31"]["missing_passing_financial_screen_symbols"] == "SEZL"
    assert passing["2023-08-31"][
        "unresolved_observable_potential_competitor_count"
    ] == 0
    assert passing["2025-09-30"]["missing_passing_financial_screen_symbols"] == "PPBI"
    assert passing["2025-09-30"]["confirmed_terminal_before_signal_count"] == 1
    assert passing["2026-05-29"]["missing_passing_financial_screen_symbols"] == "APLS"
    assert passing["2026-05-29"]["confirmed_terminal_before_signal_count"] == 1
    assert readiness["historical_quarterly_conflict_order_sensitivity"][
        "financial_eligibility_changed_ticker_signal_count"
    ] == 0

