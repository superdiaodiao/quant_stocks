"""Validate heuristic price jumps against sourced corporate-action events."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

from src.conf import CLEANED_PRICE_DATA_DIR


YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
NASDAQ_DIVIDENDS = "https://api.nasdaq.com/api/quote/{ticker}/dividends"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
OUTPUT_COLUMNS = [
    "ticker",
    "split_date",
    "raw_price_ratio",
    "matched_factor",
    "selected_by_fixed_top3",
    "validation_status",
    "confirmed_action_type",
    "confirmed_action_date",
    "confirmed_adjustment_factor",
    "cash_amount",
    "primary_source",
    "secondary_source",
    "fetch_error",
]
REVIEWED_MARKET_MOVES_FILE = Path(
    "stocks_list_dir/nasdaq/reviewed_market_moves.csv"
)


def load_reviewed_market_moves(
    path: str | Path = REVIEWED_MARKET_MOVES_FILE,
) -> pd.DataFrame:
    """Load sourced real-price moves that must not be back-adjusted."""
    frame = pd.read_csv(path)
    required = {
        "ticker", "event_date", "classification", "source_url",
        "verified_at", "notes",
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(
            f"reviewed market moves missing columns: {sorted(missing)}"
        )
    frame = frame.copy()
    frame["ticker"] = frame["ticker"].astype(str).str.upper().str.strip()
    frame["event_date"] = pd.to_datetime(
        frame["event_date"], errors="raise"
    ).dt.normalize()
    if not frame["classification"].eq("market_move_no_adjustment").all():
        raise ValueError("unsupported reviewed market move classification")
    if frame["source_url"].fillna("").str.strip().eq("").any():
        raise ValueError("reviewed market moves require a primary source")
    if frame.duplicated(["ticker", "event_date"]).any():
        raise ValueError("reviewed market moves contain duplicate events")
    return frame


def apply_reviewed_market_moves(
    validation: pd.DataFrame,
    reviewed_market_moves: pd.DataFrame,
) -> pd.DataFrame:
    """Overlay primary-source no-adjustment reviews onto fetched results."""
    result = validation.copy()
    result["split_date"] = pd.to_datetime(
        result["split_date"], errors="raise"
    ).dt.normalize()
    for event in reviewed_market_moves.itertuples(index=False):
        mask = (
            result["ticker"].astype(str).str.upper().eq(event.ticker)
            & result["split_date"].eq(event.event_date)
        )
        if not mask.any():
            continue
        result.loc[mask, "validation_status"] = "CONFIRMED_MARKET_MOVE"
        result.loc[mask, "confirmed_action_type"] = (
            "MARKET_MOVE_NO_ADJUSTMENT"
        )
        result.loc[mask, "confirmed_action_date"] = (
            event.event_date.strftime("%Y-%m-%d")
        )
        result.loc[mask, "confirmed_adjustment_factor"] = pd.NA
        result.loc[mask, "cash_amount"] = pd.NA
        result.loc[mask, "primary_source"] = event.source_url
        result.loc[mask, "fetch_error"] = pd.NA
    return result


def _read_json(url: str, retries: int = 3) -> dict:
    error = None
    for attempt in range(retries):
        try:
            with urlopen(Request(url, headers=HEADERS), timeout=30) as response:
                return json.load(response)
        except Exception as exc:
            error = exc
            if attempt + 1 < retries:
                time.sleep(2**attempt)
    raise RuntimeError(str(error))


def _yahoo_url(
    ticker: str, start: pd.Timestamp, end: pd.Timestamp
) -> str:
    params = urlencode({
        "period1": int(start.replace(tzinfo=timezone.utc).timestamp()),
        "period2": int(
            (end + pd.Timedelta(days=1)).replace(tzinfo=timezone.utc).timestamp()
        ),
        "events": "div,splits",
        "interval": "1d",
    })
    return YAHOO_CHART.format(ticker=ticker) + "?" + params


def fetch_yahoo_actions(
    ticker: str, start: pd.Timestamp, end: pd.Timestamp
) -> tuple[pd.DataFrame, pd.DataFrame, str]:
    """Return Yahoo-reported splits and cash events in one request."""
    url = _yahoo_url(ticker, start, end)
    payload = _read_json(url)
    result = ((payload.get("chart", {}).get("result") or [{}])[0])
    split_rows = []
    for event in (result.get("events", {}).get("splits", {}) or {}).values():
        numerator = float(event["numerator"])
        denominator = float(event["denominator"])
        split_rows.append({
            "ticker": ticker.upper(),
            "effective_date": pd.Timestamp(
                datetime.fromtimestamp(event["date"], tz=timezone.utc)
            ).tz_localize(None).normalize(),
            "adjustment_factor": denominator / numerator,
            "source": url,
            "source_tier": "secondary",
        })
    cash_rows = []
    for event in (
        result.get("events", {}).get("dividends", {}) or {}
    ).values():
        cash_rows.append({
            "ticker": ticker.upper(),
            "effective_date": pd.Timestamp(
                datetime.fromtimestamp(event["date"], tz=timezone.utc)
            ).tz_localize(None).normalize(),
            "cash_amount": float(event["amount"]),
            "source": url,
            "source_tier": "secondary",
        })
    return pd.DataFrame(split_rows), pd.DataFrame(cash_rows), url


def fetch_yahoo_splits(
    ticker: str, start: pd.Timestamp, end: pd.Timestamp
) -> pd.DataFrame:
    """Return Yahoo-reported split events with back-adjustment factors."""
    splits, _, _ = fetch_yahoo_actions(ticker, start, end)
    return splits


def fetch_nasdaq_cash_distributions(ticker: str) -> pd.DataFrame:
    """Return Nasdaq-reported cash distributions for one symbol."""
    url = NASDAQ_DIVIDENDS.format(ticker=ticker) + "?assetclass=stocks"
    payload = _read_json(url)
    dividends = ((payload.get("data") or {}).get("dividends") or {})
    rows = []
    for event in dividends.get("rows") or []:
        if str(event.get("type") or "").lower() != "cash":
            continue
        amount = pd.to_numeric(
            str(event.get("amount") or "").replace("$", "").replace(",", ""),
            errors="coerce",
        )
        effective_date = pd.to_datetime(
            event.get("exOrEffDate"), errors="coerce"
        )
        if pd.isna(amount) or pd.isna(effective_date):
            continue
        rows.append({
            "ticker": ticker.upper(),
            "effective_date": effective_date.normalize(),
            "cash_amount": float(amount),
            "source": url,
            "source_tier": "official",
        })
    return pd.DataFrame(rows)


def validate_candidate_events(
    candidates: pd.DataFrame,
    split_events: pd.DataFrame,
    cash_events: pd.DataFrame,
    close_by_ticker: dict[str, pd.Series],
    checked_sources: dict[str, str] | None = None,
    fetch_errors: dict[str, str] | None = None,
    reviewed_market_moves: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Classify inferred split candidates using sourced action records."""
    checked_sources = checked_sources or {}
    fetch_errors = fetch_errors or {}
    reviewed_market_moves = (
        pd.DataFrame()
        if reviewed_market_moves is None
        else reviewed_market_moves
    )
    rows = []
    for candidate in candidates.itertuples(index=False):
        ticker = str(candidate.ticker).upper()
        event_date = pd.Timestamp(candidate.split_date).normalize()
        split = split_events.loc[
            split_events["ticker"].eq(ticker)
            & split_events["effective_date"].eq(event_date)
        ] if len(split_events) else pd.DataFrame()
        nearby_split = split_events.loc[
            split_events["ticker"].eq(ticker)
            & split_events["effective_date"].sub(event_date).abs().le(
                pd.Timedelta(days=400)
            )
            & split_events["adjustment_factor"].sub(
                float(candidate.matched_factor)
            ).abs().div(float(candidate.matched_factor)).le(0.025)
        ] if len(split_events) else pd.DataFrame()
        cash = cash_events.loc[
            cash_events["ticker"].eq(ticker)
            & cash_events["effective_date"].eq(event_date)
        ] if len(cash_events) else pd.DataFrame()
        reviewed = reviewed_market_moves.loc[
            reviewed_market_moves["ticker"].eq(ticker)
            & reviewed_market_moves["event_date"].eq(event_date)
        ] if len(reviewed_market_moves) else pd.DataFrame()
        status = (
            "SOURCE_FETCH_FAILED"
            if ticker in fetch_errors else "UNRESOLVED_PRICE_JUMP"
        )
        action_type = None
        action_date = None
        factor = None
        cash_amount = None
        primary_source = None
        secondary_source = checked_sources.get(ticker)
        if len(reviewed):
            event = reviewed.iloc[-1]
            status = "CONFIRMED_MARKET_MOVE"
            action_type = "MARKET_MOVE_NO_ADJUSTMENT"
            action_date = event_date
            primary_source = event["source_url"]
        elif len(split):
            event = split.iloc[-1]
            status = "CONFIRMED"
            action_type = "SPLIT"
            action_date = event["effective_date"]
            factor = float(event["adjustment_factor"])
            if event.get("source_tier") == "official":
                primary_source = event["source"]
            else:
                secondary_source = event["source"]
        elif len(nearby_split):
            event = nearby_split.iloc[
                nearby_split["effective_date"].sub(event_date).abs().argmin()
            ]
            status = "CONFIRMED"
            action_type = "PROVIDER_ADJUSTMENT_DISCONTINUITY"
            action_date = event["effective_date"]
            factor = float(event["adjustment_factor"])
            secondary_source = event["source"]
        elif len(cash):
            ordered_cash = (
                cash.sort_values(
                    "source_tier",
                    key=lambda values: values.map(
                        {"secondary": 0, "official": 1}
                    ).fillna(0),
                )
                if "source_tier" in cash
                else cash
            )
            event = ordered_cash.iloc[-1]
            series = close_by_ticker[ticker].dropna().sort_index()
            prior = series.loc[series.index < event_date]
            cash_amount = float(event["cash_amount"])
            if len(prior) and 0 < cash_amount < float(prior.iloc[-1]):
                status = "CONFIRMED"
                action_type = "CASH_DISTRIBUTION"
                action_date = event_date
                factor = (float(prior.iloc[-1]) - cash_amount) / float(
                    prior.iloc[-1]
                )
                if event.get("source_tier") == "official":
                    primary_source = event["source"]
                else:
                    secondary_source = event["source"]
        rows.append({
            "ticker": ticker,
            "split_date": event_date,
            "raw_price_ratio": float(candidate.raw_price_ratio),
            "matched_factor": float(candidate.matched_factor),
            "selected_by_fixed_top3": bool(
                getattr(candidate, "selected_by_fixed_top3", False)
            ),
            "validation_status": status,
            "confirmed_action_type": action_type,
            "confirmed_action_date": action_date,
            "confirmed_adjustment_factor": factor,
            "cash_amount": cash_amount,
            "primary_source": primary_source,
            "secondary_source": secondary_source,
            "fetch_error": fetch_errors.get(ticker),
        })
    return pd.DataFrame(rows, columns=OUTPUT_COLUMNS)


def run_corporate_action_validation(
    selected_only: bool = True,
    workers: int = 4,
) -> tuple[pd.DataFrame, dict]:
    """Fetch and validate selected or all heuristic price-jump candidates."""
    source = Path("output/can_slim_fixed_top3_split_events.csv")
    candidates = pd.read_csv(source, parse_dates=["split_date"])
    if selected_only:
        candidates = candidates.loc[
            candidates["selected_by_fixed_top3"]
        ].copy()
    split_frames = []
    yahoo_cash_frames = []
    checked_sources = {}
    fetch_errors = {}
    close_by_ticker = {}
    requests_by_ticker = {}
    for ticker in sorted(candidates["ticker"].unique()):
        ticker_candidates = candidates.loc[candidates["ticker"].eq(ticker)]
        start = ticker_candidates["split_date"].min() - pd.Timedelta(days=400)
        end = min(
            ticker_candidates["split_date"].max() + pd.Timedelta(days=400),
            pd.Timestamp.now(tz="UTC").tz_localize(None).normalize(),
        )
        requests_by_ticker[ticker] = (start, end)
        price = pd.read_csv(
            Path(CLEANED_PRICE_DATA_DIR) / f"{ticker.lower()}.csv",
            parse_dates=["date"],
        ).set_index("date")["close"]
        close_by_ticker[ticker] = price
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(fetch_yahoo_actions, ticker, start, end): ticker
            for ticker, (start, end) in requests_by_ticker.items()
        }
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                splits, cash, checked_url = future.result()
                checked_sources[ticker] = checked_url
                if len(splits):
                    split_frames.append(splits)
                if len(cash):
                    yahoo_cash_frames.append(cash)
            except Exception as exc:
                fetch_errors[ticker] = str(exc)
    split_events = (
        pd.concat(split_frames, ignore_index=True)
        if split_frames else pd.DataFrame(
            columns=[
                "ticker", "effective_date", "adjustment_factor", "source",
                "source_tier",
            ]
        )
    )
    yahoo_cash = (
        pd.concat(yahoo_cash_frames, ignore_index=True)
        if yahoo_cash_frames else pd.DataFrame(
            columns=[
                "ticker", "effective_date", "cash_amount", "source",
                "source_tier",
            ]
        )
    )
    candidate_keys = set(zip(
        candidates["ticker"].astype(str).str.upper(),
        candidates["split_date"].dt.normalize(),
    ))
    official_cash_tickers = sorted({
        str(row.ticker).upper()
        for row in yahoo_cash.itertuples(index=False)
        if (str(row.ticker).upper(), pd.Timestamp(row.effective_date)) in candidate_keys
    })
    official_cash_frames = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(fetch_nasdaq_cash_distributions, ticker): ticker
            for ticker in official_cash_tickers
        }
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                cash = future.result()
                if len(cash):
                    official_cash_frames.append(cash)
            except Exception as exc:
                prior = fetch_errors.get(ticker)
                message = f"Nasdaq cash lookup: {exc}"
                fetch_errors[ticker] = (
                    f"{prior}; {message}" if prior else message
                )
    cash_events = pd.concat(
        [yahoo_cash, *official_cash_frames], ignore_index=True
    )
    result = validate_candidate_events(
        candidates,
        split_events,
        cash_events,
        close_by_ticker,
        checked_sources,
        fetch_errors,
        load_reviewed_market_moves(),
    )
    return write_validation_outputs(result, selected_only)


def validation_summary(result: pd.DataFrame, selected_only: bool) -> dict:
    """Summarize a fetched or deterministically refreshed validation table."""
    prefix = (
        "can_slim_selected_corporate_action_validation"
        if selected_only else "can_slim_all_corporate_action_validation"
    )
    unresolved = int(
        result["validation_status"].eq("UNRESOLVED_PRICE_JUMP").sum()
    )
    failed = int(
        result["validation_status"].eq("SOURCE_FETCH_FAILED").sum()
    )
    summary = {
        "status": "PASS" if unresolved == 0 and failed == 0 else "BLOCKED",
        "scope": (
            "selected_symbols_from_can_slim_fixed_top3"
            if selected_only else "all_integer_ratio_price_jump_candidates"
        ),
        "candidate_events": int(len(result)),
        "candidate_tickers": int(result["ticker"].nunique()),
        "confirmed_events": int(
            result["validation_status"].eq("CONFIRMED").sum()
        ),
        "confirmed_market_moves": int(
            result["validation_status"].eq(
                "CONFIRMED_MARKET_MOVE"
            ).sum()
        ),
        "confirmed_splits": int(
            result["confirmed_action_type"].eq("SPLIT").sum()
        ),
        "confirmed_cash_distributions": int(
            result["confirmed_action_type"].eq("CASH_DISTRIBUTION").sum()
        ),
        "confirmed_provider_adjustment_discontinuities": int(
            result["confirmed_action_type"].eq(
                "PROVIDER_ADJUSTMENT_DISCONTINUITY"
            ).sum()
        ),
        "confirmed_with_official_primary_source": int(
            result["primary_source"].notna().sum()
        ),
        "confirmed_with_secondary_source_only": int(
            (
                result["validation_status"].eq("CONFIRMED")
                & result["primary_source"].isna()
                & result["secondary_source"].notna()
            ).sum()
        ),
        "unresolved_events": unresolved,
        "source_fetch_failed_events": failed,
        "source_fetch_failed_tickers": int(
            result.loc[
                result["validation_status"].eq("SOURCE_FETCH_FAILED"),
                "ticker",
            ].nunique()
        ),
        "automatic_heuristic_adjustment_allowed": (
            unresolved == 0 and failed == 0
        ),
        "warning": (
            "A price ratio near an integer split factor is not evidence of a "
            "split. CONFIRMED_MARKET_MOVE events are sourced real price moves "
            "and must not be adjusted. Unresolved events require sourced "
            "review before automatic back-adjustment."
        ),
    }
    return summary


def write_validation_outputs(
    result: pd.DataFrame,
    selected_only: bool,
) -> tuple[pd.DataFrame, dict]:
    prefix = (
        "can_slim_selected_corporate_action_validation"
        if selected_only else "can_slim_all_corporate_action_validation"
    )
    target = Path("output") / f"{prefix}.csv"
    result.to_csv(target, index=False)
    summary = validation_summary(result, selected_only)
    (Path("output") / f"{prefix}_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    return result, summary


def refresh_existing_validation_with_reviewed_moves(
    selected_only: bool,
) -> tuple[pd.DataFrame, dict]:
    """Reuse prior network evidence and apply only new primary-source reviews."""
    prefix = (
        "can_slim_selected_corporate_action_validation"
        if selected_only else "can_slim_all_corporate_action_validation"
    )
    path = Path("output") / f"{prefix}.csv"
    result = pd.read_csv(path)
    result = apply_reviewed_market_moves(
        result, load_reviewed_market_moves()
    )
    return write_validation_outputs(result, selected_only)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all-candidates", action="store_true")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--reuse-existing-sources", action="store_true")
    args = parser.parse_args()
    selected_only = not args.all_candidates
    if args.reuse_existing_sources:
        result, summary = refresh_existing_validation_with_reviewed_moves(
            selected_only
        )
    else:
        result, summary = run_corporate_action_validation(
            selected_only=selected_only,
            workers=args.workers,
        )
    if not args.all_candidates:
        print(result.to_string(index=False))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
