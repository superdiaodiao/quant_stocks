"""Pure security-type filtering shared by research and data refresh jobs."""

from __future__ import annotations

import pandas as pd


NON_COMMON_SECURITY_PATTERN = (
    r"\bPreferred\b|\bPreference Shares?\b|\bWarrants?\b|\b(?:Sub)?Units?\b|Notes? due|"
    r"Debenture|\bRights?\b|Tangible Equity| - Depositary Shares$|"
    r"Depositary Shares, each Representing|Depositary Shares Each Representing|"
    r"Depositary Shares? rep|Trust Preferred|Preferred Units|Senior Notes|Subordinated Notes|"
    r"\bETF\b|\bETNs?\b|\bExchange Traded Notes?\b|\bIndex Fund\b|"
    r"\bTest Stock\b|\bWhen[- ]Issued\b|"
    r"\bLiberty Braves Common Stock\b|"
    r"\bStrategic Total Return\b|\bOpportunities Trust\b|\bClosed[- ]End\b|"
    r"\bL\.?P\.?\b|\bLimited Partnership\b|"
    r"\bAcquisition\b.*\b(?:Corp(?:oration)?|Co(?:mpany)?|Ltd\.?|Inc\.?|Group)\b|"
    r"\bMerger Corporation\b|\bGrowth Opportunity Corp\b|"
    r"\bEnergy Transition Corp\b|\bCapital Investment Corp\b|"
    r"\bCapital Corp(?:oration)?\.?\s*(?:III|II|I)?\s*-?\s*Class A Ordinary|"
    r"\bGrowth Corporation\s*-?\s*Class A Ordinary|"
    r"\bEquity Partners(?:\s+III)?[,]? Inc\.?\s*-?\s*Class A Ordinary"
)


def investable_common_equities(universe: pd.DataFrame) -> pd.DataFrame:
    """Remove preferreds, warrants, units, rights, and debt from research."""
    eligible = universe.loc[
        ~universe["Name"].astype(str).str.contains(
            NON_COMMON_SECURITY_PATTERN, case=False, na=False, regex=True
        )
    ].copy()
    for flag in ("ETF", "Test Issue", "NextShares"):
        if flag in eligible:
            eligible = eligible.loc[
                ~eligible[flag].astype(str).str.upper().eq("Y")
            ]
    return eligible
