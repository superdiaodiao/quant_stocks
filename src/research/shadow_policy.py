"""Single source of truth for the precommitted shadow promotion policy."""

FORWARD_EVIDENCE_START = "2026-07-18"
MIN_CONTIGUOUS_SESSIONS = 252
MIN_COMPLETED_MONTHLY_PERIODS = 12
MIN_WINNING_PERIODS = 7
REQUIRE_EXTERNAL_ANCHOR = True
REQUIRE_POSITIVE_EXCESS = True


def promotion_policy() -> dict:
    return {
        "forward_evidence_start": FORWARD_EVIDENCE_START,
        "minimum_contiguous_sessions": MIN_CONTIGUOUS_SESSIONS,
        "minimum_completed_monthly_periods": MIN_COMPLETED_MONTHLY_PERIODS,
        "minimum_winning_periods": MIN_WINNING_PERIODS,
        "require_external_anchor": REQUIRE_EXTERNAL_ANCHOR,
        "require_positive_excess": REQUIRE_POSITIVE_EXCESS,
    }
