"""Core of the sue-lt-v1 forward observation (docs/sue_lt_v1_protocol.md).

Pure computation, no network: the append-only hash-chained ledger, target
selection, the simulated book and its trades. Selection and order costs
are imported from the backtest that earned this observation (ledger item
6), so the live rule cannot drift from what was tested.

Positions are held as dollar values rolled forward by each session's
return from a freshly downloaded series, never as share counts, so a
provider rewriting history for a split cannot create a false gain or loss.
Each holding keeps the date of the close it was last valued at, so a
session whose close arrives late is not lost: the next valuation spans it.
"""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path

import pandas as pd

from scripts.research_earnings_surprise import (
    ANNOUNCEMENT_WINDOW,
    first_reported_net_income,
    sue_at,
)
from scripts.research_sue_low_turnover import _buy_notional, order_cost

MODEL_VERSION = "sue-lt-v1"
FIRST_SIGNAL_DATE = pd.Timestamp("2026-10-30")
# The last Nasdaq session of 2027-10 (12 months after the first execution):
# the final valuation. No signal on or after it.
END_DATE = pd.Timestamp("2027-10-29")
START_CASH = 10_000.0
POOL = 100
HOLDINGS = 10
MINIMUM_PRICE = 5.0
MINIMUM_HISTORY = 252
LIQUIDITY_SESSIONS = 50
STALE_SESSIONS = 10
COST_MODEL = "ibkr_tiered"
# A new name whose equal share of the cash is below this is not bought.
MINIMUM_BUY = 1.0
NULL_HASH = "0" * 64
EVENT_TYPES = (
    "PROTOCOL_FROZEN",
    "SIGNAL_FROZEN",
    "TRADES_EXECUTED",
    "VALUATION_APPENDED",
    "POSITION_RETIRED",
    "TERMINAL_RECORDED",
)


# ------------------------------------------------------------------ ledger


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _event_hash(unsigned: dict) -> str:
    return hashlib.sha256(_canonical(unsigned)).hexdigest()


def validate_chain(events: list[dict]) -> None:
    previous, protocol = NULL_HASH, None
    signals: list[str] = []
    executed: set[str] = set()
    last_valuation = None
    terminated = False
    for index, event in enumerate(events):
        if event.get("event_index") != index:
            raise RuntimeError("ledger event indexes are not contiguous")
        if event.get("event_type") not in EVENT_TYPES:
            raise RuntimeError("ledger contains an unknown event type")
        if event.get("prev_hash") != previous:
            raise RuntimeError("ledger previous-hash link is broken")
        unsigned = {k: v for k, v in event.items() if k != "event_hash"}
        if event.get("event_hash") != _event_hash(unsigned):
            raise RuntimeError("ledger event hash is invalid")
        kind, payload = event["event_type"], event.get("payload") or {}
        if terminated:
            raise RuntimeError("ledger continues after its terminal record")
        if index == 0:
            if kind != "PROTOCOL_FROZEN":
                raise RuntimeError("ledger must start with the protocol freeze")
            protocol = event.get("protocol_sha256")
        elif kind == "PROTOCOL_FROZEN" or event.get("protocol_sha256") != protocol:
            raise RuntimeError("ledger mixes protocol bindings")
        if kind == "SIGNAL_FROZEN":
            day = str(payload["signal_date"])
            if day in signals or pd.Timestamp(day) < FIRST_SIGNAL_DATE:
                raise RuntimeError("signal is duplicated or precedes the first signal date")
            if pd.Timestamp(day) >= END_DATE:
                raise RuntimeError("no signal is taken on or after the end date")
            if signals and pd.Timestamp(day) <= pd.Timestamp(signals[-1]):
                raise RuntimeError("signals are not in date order")
            signals.append(day)
        elif kind == "TRADES_EXECUTED":
            day = str(payload["signal_date"])
            if day not in signals or day in executed:
                raise RuntimeError("execution does not bind exactly one frozen signal")
            if pd.Timestamp(payload["execution_date"]) <= pd.Timestamp(day):
                raise RuntimeError("execution must follow its signal")
            executed.add(day)
        elif kind == "VALUATION_APPENDED":
            day = pd.Timestamp(payload["as_of"])
            if last_valuation is not None and day <= last_valuation:
                raise RuntimeError("valuation dates are not append-only")
            if day > END_DATE:
                raise RuntimeError("valuation after the end date")
            # A frozen signal executes at the first session after it, before
            # that session is valued; a valuation past it proves it was skipped.
            if any(pd.Timestamp(s) < day for s in signals if s not in executed):
                raise RuntimeError("valuation passes a signal that was never executed")
            last_valuation = day
        elif kind == "TERMINAL_RECORDED":
            if last_valuation is None or pd.Timestamp(payload["as_of"]) != last_valuation:
                raise RuntimeError("the terminal record must follow the valuation it judges")
            terminated = True
        previous = event["event_hash"]


def read_ledger(path: str | Path) -> list[dict]:
    item = Path(path)
    if not item.is_file():
        return []
    with item.open("r", encoding="utf-8") as handle:
        events = [json.loads(line) for line in handle if line.strip()]
    validate_chain(events)
    return events


def append_event(path: str | Path, protocol_sha256: str, event_type: str, payload: dict,
                 recorded_at: str | None = None) -> dict:
    item = Path(path)
    item.parent.mkdir(parents=True, exist_ok=True)
    with item.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        events = [json.loads(line) for line in handle if line.strip()]
        unsigned = {
            "event_index": len(events),
            "event_type": event_type,
            "recorded_at": recorded_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "model_version": MODEL_VERSION,
            "protocol_sha256": protocol_sha256,
            "prev_hash": events[-1]["event_hash"] if events else NULL_HASH,
            "payload": payload,
        }
        event = {**unsigned, "event_hash": _event_hash(unsigned)}
        validate_chain([*events, event])
        handle.seek(0, os.SEEK_END)
        handle.write(json.dumps(event, sort_keys=True, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return event


# ------------------------------------------------------------------ selection


def pool_liquidity(close: pd.DataFrame, dollar_volume: pd.DataFrame, symbols: set[str],
                   signal_date: pd.Timestamp) -> pd.Series:
    """50-session median dollar volume of the eligible names, most liquid first, top POOL.

    ``close`` is each name's history as downloaded on the signal evening,
    whose last row is the signal day's nominal close; ``dollar_volume`` is
    close times volume, which a split does not change.
    """
    signal_date = pd.Timestamp(signal_date)
    names = sorted(set(symbols) & set(close.columns))
    frame = close.loc[:signal_date, names]
    if frame.empty or frame.index[-1] != signal_date:
        raise RuntimeError("prices do not reach the signal date")
    price = frame.iloc[-1]
    history = frame.notna().sum()
    liquidity = dollar_volume.reindex_like(frame).tail(LIQUIDITY_SESSIONS).median()
    eligible = price.ge(MINIMUM_PRICE) & history.ge(MINIMUM_HISTORY)
    return liquidity.loc[eligible[eligible].index].dropna().nlargest(POOL)


def liquidity_pool(close: pd.DataFrame, dollar_volume: pd.DataFrame, symbols: set[str],
                   signal_date: pd.Timestamp) -> list[str]:
    """The 100 most liquid eligible names on the signal date."""
    return list(pool_liquidity(close, dollar_volume, symbols, signal_date).index)


def one_class_per_issuer(ranked: list[str], liquidity: pd.Series, issuer: dict[str, int],
                         limit: int = HOLDINGS) -> list[str]:
    """Walk the ranking; an issuer already represented is skipped, and the first
    time an issuer appears it is represented by its most liquid class in the ranking."""
    chosen, seen = [], set()
    for ticker in ranked:
        key = issuer.get(ticker, ticker)
        if key in seen:
            continue
        seen.add(key)
        classes = [t for t in ranked if issuer.get(t, t) == key]
        chosen.append(max(classes, key=lambda t: float(liquidity.get(t, 0.0))))
        if len(chosen) == limit:
            break
    return chosen


def select_targets(close: pd.DataFrame, dollar_volume: pd.DataFrame, symbols: set[str],
                   quarterly: pd.DataFrame, signal_date: pd.Timestamp,
                   issuer: dict[str, int] | None = None) -> dict:
    signal_date = pd.Timestamp(signal_date)
    liquidity = pool_liquidity(close, dollar_volume, symbols, signal_date)
    pool = list(liquidity.index)
    sessions = close.loc[:signal_date].index
    window_start = sessions[max(0, len(sessions) - 1 - ANNOUNCEMENT_WINDOW)]
    known = quarterly.loc[pd.to_datetime(quarterly["available_date"]).le(signal_date)]
    income = first_reported_net_income(known)
    scores = sue_at(income.loc[income["ticker"].isin(pool)], signal_date, window_start)
    scores = scores.reindex(pool).dropna().sort_values(ascending=False)
    return {
        "signal_date": signal_date.strftime("%Y-%m-%d"),
        "pool": pool,
        "sue": {ticker: float(value) for ticker, value in scores.items()},
        "targets": one_class_per_issuer(list(scores.index), liquidity, issuer or {}),
        "window_start": window_start.strftime("%Y-%m-%d"),
    }


# ------------------------------------------------------------------ book


def new_book() -> dict:
    return {"cash": START_CASH, "positions": {}, "missing_sessions": {}, "last_priced": {},
            "as_of": None}


def roll_forward(book: dict, returns: dict[str, float | None], day: pd.Timestamp) -> dict:
    """Apply one session the market traded: each held name's return since the
    close it was last valued at, or None when it has no close that session."""
    book = json.loads(json.dumps(book))
    book.setdefault("last_priced", {})
    stamp = pd.Timestamp(day).strftime("%Y-%m-%d")
    for ticker in list(book["positions"]):
        value = returns.get(ticker)
        if value is None:
            book["missing_sessions"][ticker] = book["missing_sessions"].get(ticker, 0) + 1
        else:
            book["positions"][ticker] *= 1.0 + float(value)
            book["missing_sessions"].pop(ticker, None)
            book["last_priced"][ticker] = stamp
    book["as_of"] = stamp
    return book


def retire_stale(book: dict) -> tuple[dict, list[dict]]:
    """Positions without a close for STALE_SESSIONS sessions become cash at their last value."""
    book = json.loads(json.dumps(book))
    retired = []
    for ticker, count in list(book["missing_sessions"].items()):
        if count >= STALE_SESSIONS and ticker in book["positions"]:
            value = book["positions"].pop(ticker)
            book["cash"] += value
            book["missing_sessions"].pop(ticker)
            book.get("last_priced", {}).pop(ticker, None)
            retired.append({"ticker": ticker, "value": value, "missing_sessions": count})
    return book, retired


def execute(book: dict, targets: list[str], nominal_price: dict[str, float],
            day: pd.Timestamp | None = None) -> tuple[dict, list[dict]]:
    """Sell names that left the list; split all cash across listed names not held.

    A name without a close today is neither sold nor bought: a seller is sold
    at the next execution it trades, and a buyer's share goes to the listed
    names that have a close. Nothing is bought when each share would be below
    MINIMUM_BUY; the cash waits for the next execution.
    """
    book = json.loads(json.dumps(book))
    last_priced = book.setdefault("last_priced", {})
    stamp = None if day is None else pd.Timestamp(day).strftime("%Y-%m-%d")
    orders = []
    for ticker in [t for t in book["positions"] if t not in targets]:
        price = nominal_price.get(ticker)
        if price is None:
            continue  # did not trade today: sold at the next execution it trades
        value = book["positions"].pop(ticker)
        fee = order_cost(value, price, True, COST_MODEL)
        book["cash"] += value - fee
        book["missing_sessions"].pop(ticker, None)
        last_priced.pop(ticker, None)
        orders.append({"ticker": ticker, "side": "SELL", "notional": value, "cost": fee, "price": price})
    new = [t for t in targets if t not in book["positions"] and nominal_price.get(t)]
    if new and book["cash"] / len(new) >= MINIMUM_BUY:
        budget = book["cash"] / len(new)
        for ticker in new:
            notional = _buy_notional(budget, nominal_price[ticker], COST_MODEL)
            book["cash"] -= budget
            book["positions"][ticker] = notional
            if stamp is not None:
                last_priced[ticker] = stamp
            orders.append({"ticker": ticker, "side": "BUY", "notional": notional,
                           "cost": budget - notional, "price": nominal_price[ticker]})
    return book, orders


def nav(book: dict) -> float:
    return float(book["cash"] + sum(book["positions"].values()))


# ------------------------------------------------------------------ evaluation


def points_behind(account_nav: float, qqq_nav: float) -> float:
    """How far the account trails QQQ, in percentage points of START_CASH (reported, never a stop)."""
    return 100.0 * (float(qqq_nav) - float(account_nav)) / START_CASH


def terminal_record(day: pd.Timestamp, account_nav: float, qqq_nav: float,
                    final: bool = False) -> dict | None:
    """The end of the observation after the ``day`` valuation, or None.

    There is no early stop: only the END_DATE valuation ends it. ``final``
    marks the last valuation on or before END_DATE once END_DATE has passed
    (END_DATE itself may turn out not to have traded).
    """
    if not (final or pd.Timestamp(day) >= END_DATE):
        return None
    behind = points_behind(account_nav, qqq_nav)
    return {"as_of": pd.Timestamp(day).strftime("%Y-%m-%d"), "nav": float(account_nav),
            "qqq_nav": float(qqq_nav), "points_behind_qqq": behind, "reason": "END_DATE",
            "outcome": "BEAT_QQQ" if account_nav > qqq_nav else "DID_NOT_BEAT_QQQ"}
