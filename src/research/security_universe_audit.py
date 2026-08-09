"""Research-only security-type audit for the current and traded universes."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from src.conf import NASDAQ_300M_STOCK_LIST_FILE
from src.io.financial_update import investable_common_equities
from src.research.universe_history import load_security_master


POOLED_PATTERN = (
    r"\bETF\b|\bETNs?\b|\bExchange Traded Notes?\b|\bClosed-End\b|"
    r"\bStrategic Total Return\b|\bOpportunities Trust\b|"
    r"\bIncome Fund\b|\bBond Fund\b"
)
PARTNERSHIP_PATTERN = r"\bL\.?P\.?\b|\bLimited Partnership\b|\bPartnership\b"
SPAC_PATTERN = r"\bAcquisition\b|\bBlank Check\b"
FOREIGN_PATTERN = (
    r"\bADS\b|\bADR\b|\bDepositary\b|\bOrdinary Shares?\b|"
    r"\bplc\b|\bS\.A\.\b|\bN\.V\.\b"
)


def classify_security_names(frame: pd.DataFrame) -> pd.Series:
    """Return transparent name-based research categories, not legal types."""
    names = frame["Name"].fillna("").astype(str)
    result = pd.Series(
        "OPERATING_COMMON_EQUITY", index=frame.index, dtype=object
    )
    result.loc[
        names.str.contains(FOREIGN_PATTERN, case=False, regex=True)
    ] = "FOREIGN_OR_DEPOSITARY"
    result.loc[
        names.str.contains(SPAC_PATTERN, case=False, regex=True)
    ] = "SPAC_OR_SHELL"
    result.loc[
        names.str.contains(PARTNERSHIP_PATTERN, case=False, regex=True)
    ] = "PARTNERSHIP"
    result.loc[
        names.str.contains(POOLED_PATTERN, case=False, regex=True)
    ] = "POOLED_INVESTMENT"
    return result


def run_security_universe_audit(
    robustness_summary: str | Path = (
        "output/can_slim_fixed_top3_robustness_summary.json"
    ),
    data_audit: str | Path = "output/data_audit.json",
) -> tuple[pd.DataFrame, dict]:
    """Classify the current universe and flag categories that reached trading."""
    current = investable_common_equities(
        pd.read_csv(NASDAQ_300M_STOCK_LIST_FILE)
    ).copy()
    current["Symbol"] = current["Symbol"].astype(str).str.upper()
    current["security_category"] = classify_security_names(current)
    robustness = json.loads(Path(robustness_summary).read_text(encoding="utf-8"))
    traded = set(map(str.upper, robustness.get("traded_symbols", [])))
    current["traded_by_fixed_top3"] = current["Symbol"].isin(traded)
    audit = json.loads(Path(data_audit).read_text(encoding="utf-8"))
    missing = set(map(str.upper, audit.get("missing_financials", [])))
    stale = set(map(str.upper, audit.get("stale_financials", [])))
    current["financial_state"] = "FRESH"
    current.loc[current["Symbol"].isin(stale), "financial_state"] = "STALE"
    current.loc[current["Symbol"].isin(missing), "financial_state"] = "MISSING"

    master = load_security_master()
    historical_only = master.loc[
        master["Symbol"].isin(traded - set(current["Symbol"]))
    ].copy()
    if len(historical_only):
        historical_only["security_category"] = classify_security_names(
            historical_only
        )
        historical_only["traded_by_fixed_top3"] = True
        historical_only["financial_state"] = "NOT_CURRENT"
        current = pd.concat(
            [current, historical_only.reindex(columns=current.columns)],
            ignore_index=True,
        )

    traded_frame = current.loc[current["traded_by_fixed_top3"]]
    review_categories = {"POOLED_INVESTMENT", "PARTNERSHIP", "SPAC_OR_SHELL"}
    summary = {
        "status": (
            "PASS"
            if not traded_frame["security_category"].isin(
                review_categories
            ).any()
            else "REVIEW_REQUIRED"
        ),
        "scope": "name_based_research_classification",
        "current_universe": int(
            current.loc[current["financial_state"].ne("NOT_CURRENT"), "Symbol"].nunique()
        ),
        "traded_symbols": int(traded_frame["Symbol"].nunique()),
        "current_category_counts": {
            str(key): int(value)
            for key, value in current.loc[
                current["financial_state"].ne("NOT_CURRENT"),
                "security_category",
            ].value_counts().items()
        },
        "traded_category_counts": {
            str(key): int(value)
            for key, value in traded_frame["security_category"].value_counts().items()
        },
        "financial_state_counts": {
            str(key): int(value)
            for key, value in current.loc[
                current["financial_state"].ne("NOT_CURRENT"),
                "financial_state",
            ].value_counts().items()
        },
        "warning": (
            "Categories are transparent name-based diagnostics, not definitive "
            "legal-security classifications and not a new selection rule."
        ),
    }
    return current, summary


def main() -> None:
    frame, summary = run_security_universe_audit()
    frame.to_csv(
        "output/can_slim_security_universe_audit.csv", index=False
    )
    Path("output/can_slim_security_universe_audit_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
