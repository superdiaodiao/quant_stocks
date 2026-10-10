"""Research-only annual survivorship and retained-price coverage audit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd

from src.conf import CLEANED_PRICE_DATA_DIR, NASDAQ_INDEX_FILE, PROJECT_PATH
from src.io.security_identity import issuer_rename_transitions
from src.io.terminal_returns import load_observed_terminal_returns
from src.research.historical_data_audit import load_price_date_metadata
from src.research.universe_history import load_universe_snapshots


EVIDENCE_CLASSES = (
    "official_sec_terminal_return",
    "sourced_issuer_rename_transition",
    "unresolved_later_absence_proxy",
)


def _is_official_sec_source(source_url: object) -> bool:
    """Return whether a terminal-return source is an SEC-hosted record."""
    host = urlparse(str(source_url or "")).hostname
    if not host:
        return False
    host = host.lower()
    return host == "sec.gov" or host.endswith(".sec.gov")


def _official_terminal_dates_by_ticker(
    terminal_returns: pd.DataFrame | None,
) -> dict[str, list[pd.Timestamp]]:
    """Index SEC-sourced terminal-return evidence without trusting other URLs."""
    if terminal_returns is None or terminal_returns.empty:
        return {}
    required = {"ticker", "last_price_date", "source_url"}
    missing = required - set(terminal_returns.columns)
    if missing:
        raise ValueError(
            "terminal return evidence is missing columns: "
            f"{sorted(missing)}"
        )
    dates_by_ticker: dict[str, list[pd.Timestamp]] = {}
    for row in terminal_returns.itertuples(index=False):
        if not _is_official_sec_source(row.source_url):
            continue
        ticker = str(row.ticker).strip().upper()
        terminal_date = pd.Timestamp(row.last_price_date).normalize()
        if ticker:
            dates_by_ticker.setdefault(ticker, []).append(terminal_date)
    return dates_by_ticker


def _rename_successors_by_ticker(
    issuer_renames: pd.DataFrame | None,
) -> dict[str, list[tuple[str, pd.Timestamp]]]:
    """Index sourced same-issuer ticker transitions by the historical ticker."""
    if issuer_renames is None or issuer_renames.empty:
        return {}
    required = {
        "provider_ticker",
        "historical_ticker",
        "current_ticker_first_date",
    }
    missing = required - set(issuer_renames.columns)
    if missing:
        raise ValueError(
            "issuer rename evidence is missing columns: "
            f"{sorted(missing)}"
        )
    frame = issuer_renames
    if "identity_type" in frame:
        frame = frame.loc[frame["identity_type"].eq("issuer_rename")]
    successors: dict[str, list[tuple[str, pd.Timestamp]]] = {}
    for row in frame.itertuples(index=False):
        historical = str(row.historical_ticker).strip().upper()
        successor = str(row.provider_ticker).strip().upper()
        effective_date = pd.Timestamp(row.current_ticker_first_date).normalize()
        if historical and successor:
            successors.setdefault(historical, []).append(
                (successor, effective_date)
            )
    return successors


def _price_coverage_summary(
    tickers: list[str],
    *,
    snapshot_date: pd.Timestamp,
    benchmark: pd.DatetimeIndex,
    last_membership: dict[str, pd.Timestamp],
    price_date_metadata: dict[str, pd.Series],
) -> dict:
    """Summarize retained-price coverage through each ticker's last membership."""
    expected_sessions = 0
    observed_sessions = 0
    price_file_count = 0
    complete_count = 0
    for ticker in tickers:
        terminal_membership = last_membership[ticker]
        expected = benchmark[
            (benchmark >= snapshot_date)
            & (benchmark <= terminal_membership)
        ]
        expected_set = set(expected)
        observed = price_date_metadata.get(ticker)
        if observed is None:
            continue
        price_file_count += 1
        observed_dates = pd.DatetimeIndex(
            pd.to_datetime(observed).dropna()
        ).normalize()
        covered = len(expected_set & set(observed_dates))
        expected_sessions += len(expected_set)
        observed_sessions += covered
        if covered == len(expected_set):
            complete_count += 1
    count = len(tickers)
    return {
        "count": count,
        "price_file_count": price_file_count,
        "price_file_rate": price_file_count / count if count else 1.0,
        "price_complete_count": complete_count,
        "price_complete_rate": complete_count / count if count else 1.0,
        "expected_price_sessions": expected_sessions,
        "observed_price_sessions": observed_sessions,
        "price_session_coverage": (
            observed_sessions / expected_sessions
            if expected_sessions else 1.0
        ),
        "sample": sorted(tickers)[:20],
    }


def audit_survivorship_by_year(
    snapshots: dict[pd.Timestamp, set[str]],
    benchmark_dates: pd.Series,
    price_date_metadata: dict[str, pd.Series],
    *,
    start: str = "2021-01-01",
    end: str | None = None,
    terminal_returns: pd.DataFrame | None = None,
    issuer_renames: pd.DataFrame | None = None,
) -> dict:
    """Measure later-absent annual members and their price coverage.

    ``later_absent`` is an observable membership proxy, not a legal delisting
    classification: issuer renames, mergers, and snapshot gaps can also make a
    symbol disappear. SEC-hosted terminal-return records and sourced issuer
    renames are separately identified, leaving the remainder explicitly as a
    non-delisting-proof proxy. Coverage is measured only through the member's
    last observed PIT membership date, so expected post-termination prices are
    not counted as missing.
    """
    normalized = {
        pd.Timestamp(date).normalize(): {
            str(symbol).strip().upper()
            for symbol in members
            if str(symbol).strip()
        }
        for date, members in snapshots.items()
    }
    dates = sorted(normalized)
    if not dates:
        return {
            "status": "INCOMPLETE",
            "method": "observable_later_membership_absence_proxy",
            "years": [],
        }
    benchmark = pd.Series(pd.to_datetime(benchmark_dates).dropna())
    benchmark = pd.DatetimeIndex(benchmark).normalize().unique().sort_values()
    analysis_end = min(
        pd.Timestamp(end).normalize() if end else benchmark.max(),
        benchmark.max(),
        dates[-1],
    )
    eligible_dates = [date for date in dates if date <= analysis_end]
    if not eligible_dates:
        return {
            "status": "INCOMPLETE",
            "method": "observable_later_membership_absence_proxy",
            "years": [],
        }
    last_snapshot = eligible_dates[-1]
    last_membership: dict[str, pd.Timestamp] = {}
    membership_dates: dict[str, list[pd.Timestamp]] = {}
    for date in eligible_dates:
        for ticker in normalized[date]:
            last_membership[ticker] = date
            membership_dates.setdefault(ticker, []).append(date)

    official_terminal_dates = _official_terminal_dates_by_ticker(
        terminal_returns
    )
    rename_successors = _rename_successors_by_ticker(issuer_renames)

    first_year = pd.Timestamp(start).year
    rows = []
    for year in range(first_year, analysis_end.year + 1):
        annual_dates = [date for date in eligible_dates if date.year == year]
        if not annual_dates:
            continue
        snapshot_date = annual_dates[-1]
        members = normalized[snapshot_date]
        later_absent = sorted(
            ticker
            for ticker in members
            if last_membership.get(ticker) is not None
            and last_membership[ticker] < last_snapshot
        )
        evidence_groups = {evidence: [] for evidence in EVIDENCE_CLASSES}
        for ticker in later_absent:
            terminal_membership = last_membership[ticker]
            successors = rename_successors.get(ticker, [])
            rename_observed = any(
                effective_date > snapshot_date
                and any(
                    observed_at >= effective_date
                    and observed_at > terminal_membership
                    for observed_at in membership_dates.get(successor, [])
                )
                for successor, effective_date in successors
            )
            terminal_observed = any(
                snapshot_date <= terminal_date <= analysis_end
                for terminal_date in official_terminal_dates.get(ticker, [])
            )
            if rename_observed:
                evidence_groups["sourced_issuer_rename_transition"].append(ticker)
            elif terminal_observed:
                evidence_groups["official_sec_terminal_return"].append(ticker)
            else:
                evidence_groups["unresolved_later_absence_proxy"].append(ticker)
        coverage = _price_coverage_summary(
            later_absent,
            snapshot_date=snapshot_date,
            benchmark=benchmark,
            last_membership=last_membership,
            price_date_metadata=price_date_metadata,
        )
        evidence_breakdown = {
            evidence: _price_coverage_summary(
                tickers,
                snapshot_date=snapshot_date,
                benchmark=benchmark,
                last_membership=last_membership,
                price_date_metadata=price_date_metadata,
            )
            for evidence, tickers in evidence_groups.items()
        }
        rows.append({
            "year": year,
            "snapshot_date": snapshot_date.strftime("%Y-%m-%d"),
            "member_count": len(members),
            "later_absent_proxy_count": len(later_absent),
            "later_absent_proxy_rate": (
                len(later_absent) / len(members) if members else 0.0
            ),
            "price_file_count": coverage["price_file_count"],
            "price_file_rate": coverage["price_file_rate"],
            "price_complete_count": coverage["price_complete_count"],
            "price_complete_rate": coverage["price_complete_rate"],
            "expected_price_sessions": coverage["expected_price_sessions"],
            "observed_price_sessions": coverage["observed_price_sessions"],
            "price_session_coverage": coverage["price_session_coverage"],
            "official_sec_terminal_return_count": evidence_breakdown[
                "official_sec_terminal_return"
            ]["count"],
            "sourced_issuer_rename_transition_count": evidence_breakdown[
                "sourced_issuer_rename_transition"
            ]["count"],
            "unresolved_later_absence_proxy_count": evidence_breakdown[
                "unresolved_later_absence_proxy"
            ]["count"],
            "evidence_breakdown": evidence_breakdown,
            "later_absent_proxy_sample": later_absent[:20],
        })
    return {
        "status": "RESEARCH_ONLY",
        "method": "observable_later_membership_absence_proxy",
        "warning": (
            "Later absence is not legal delisting evidence. Only the "
            "official_sec_terminal_return class has an SEC-hosted terminal "
            "record; sourced_issuer_rename_transition explains continuity, "
            "and unresolved_later_absence_proxy remains a membership proxy."
        ),
        "evidence_class_definitions": {
            "official_sec_terminal_return": (
                "Later-absent member with an SEC-hosted sourced terminal "
                "return dated inside the analysis window; this is terminal "
                "evidence, not a universal legal-delisting label."
            ),
            "sourced_issuer_rename_transition": (
                "Later-absent historical ticker whose sourced same-issuer "
                "successor appears in a later PIT snapshot; not a terminal "
                "event."
            ),
            "unresolved_later_absence_proxy": (
                "Later membership absence without either evidence type; this "
                "is explicitly not proof of delisting."
            ),
        },
        "analysis_start": pd.Timestamp(start).strftime("%Y-%m-%d"),
        "analysis_end": analysis_end.strftime("%Y-%m-%d"),
        "latest_snapshot": last_snapshot.strftime("%Y-%m-%d"),
        "years": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2021-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument(
        "--output",
        default=str(Path(PROJECT_PATH) / "output/can_slim_survivorship_by_year.json"),
    )
    args = parser.parse_args()
    benchmark_dates = pd.read_csv(
        NASDAQ_INDEX_FILE, usecols=["date"], parse_dates=["date"]
    )["date"]
    report = audit_survivorship_by_year(
        load_universe_snapshots(),
        benchmark_dates,
        load_price_date_metadata()[1],
        start=args.start,
        end=args.end,
        terminal_returns=load_observed_terminal_returns(),
        issuer_renames=issuer_rename_transitions(),
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
