"""Exposure-aware rules for valuing a frozen monthly stock portfolio.

A frozen signal's stocks are bought at its execution close and carried until
the next execution close.  A price event outside those holding windows cannot
change the portfolio's value, so it must not stop a valuation; inside them a
valuation must rest on stored prices or on sourced evidence, never on a guess.
Pure functions only:

* the holding windows of every targeted stock, and the stocks whose close a
  valuation date needs;
* held stocks whose stored prices stop inside a holding window, which need a
  sourced terminal return;
* the append-only supplement of sourced events recorded after the frozen
  corporate-action table (splits, confirmed market moves, terminal returns);
* carrying already-valued input rows forward unchanged, so a vendor revision
  of history can never rewrite a frozen valuation, and a numeric digest of
  those rows;
* raw one-session moves that a split could explain, which must be explained
  by a sourced or measured event before a valuation or a selection uses them.
"""

from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path

import pandas as pd

from src.research.data_quality import detect_common_split_events


CASH = "__CASH__"
SPLIT = "SPLIT"
MARKET_MOVE = "MARKET_MOVE"
TERMINAL_RETURN = "TERMINAL_RETURN"
EVENT_TYPES = (SPLIT, MARKET_MOVE, TERMINAL_RETURN)
SUPPLEMENT_COLUMNS = (
    "event_id",
    "recorded_at",
    "ticker",
    "event_type",
    "event_date",
    "adjustment_factor",
    "terminal_return",
    "source_url",
    "note",
)
# The split-like jump tolerance of data_quality.detect_common_split_events.
JUMP_TOLERANCE = 0.025
REVISION_SAMPLE_ROWS = 20
# A raw one-session ratio at or beyond these is reviewed as a possible split
# whatever its factor: 3:2 and larger forward splits, 1:1.4 and larger reverse
# ones, even on a day the stock also moved.  Common whole factors are
# reviewed within JUMP_TOLERANCE as before.
LARGE_MOVE_LOW = 0.70
LARGE_MOVE_HIGH = 1.40
# A recorded split's factor may differ from the stored jump by the real move
# of its day, up to this fraction.
SPLIT_DAY_MOVE_LIMIT = 0.30

Window = tuple[pd.Timestamp, pd.Timestamp]


def holding_windows(
    target_schedule: pd.DataFrame, end: str | pd.Timestamp
) -> dict[str, list[Window]]:
    """Per stock, the (entry, exit] windows in which it carries a position.

    A positive target effective at ``e_i`` is held over ``(e_i, e_i+1]``, where
    ``e_i+1`` is the next effective date, or over ``(e_i, end]`` after the last
    one.  Windows of a stock that stays targeted are merged.  Stop exits inside
    a window are ignored, so the windows can only over-state exposure.
    """
    end = pd.Timestamp(end).normalize()
    schedule = target_schedule.copy()
    schedule["effective_date"] = pd.to_datetime(
        schedule["effective_date"], errors="raise"
    ).dt.normalize()
    schedule = schedule.loc[schedule["effective_date"].le(end)]
    dates = sorted(pd.Timestamp(item) for item in schedule["effective_date"].unique())
    spans: dict[str, list[list[pd.Timestamp]]] = {}
    for position, effective in enumerate(dates):
        exit_date = dates[position + 1] if position + 1 < len(dates) else end
        group = schedule.loc[schedule["effective_date"].eq(effective)]
        held = group.loc[
            group["ticker"].astype(str).ne(CASH)
            & group["target_weight"].astype(float).gt(0.0),
            "ticker",
        ]
        for ticker in held.astype(str):
            windows = spans.setdefault(ticker, [])
            if windows and windows[-1][1] == effective:
                windows[-1][1] = exit_date
            else:
                windows.append([effective, exit_date])
    return {
        ticker: [(entry, exit_date) for entry, exit_date in windows]
        for ticker, windows in spans.items()
    }


def closes_required(
    target_schedule: pd.DataFrame, as_of: str | pd.Timestamp
) -> list[str]:
    """Stocks whose ``as_of`` close a valuation uses: held into it or bought on it."""
    as_of = pd.Timestamp(as_of).normalize()
    windows = holding_windows(target_schedule, as_of)
    held = {
        ticker
        for ticker, spans in windows.items()
        if any(entry < as_of <= exit_date for entry, exit_date in spans)
    }
    schedule = target_schedule.copy()
    schedule["effective_date"] = pd.to_datetime(
        schedule["effective_date"], errors="raise"
    ).dt.normalize()
    bought = schedule.loc[
        schedule["effective_date"].eq(as_of)
        & schedule["ticker"].astype(str).ne(CASH)
        & schedule["target_weight"].astype(float).gt(0.0),
        "ticker",
    ]
    return sorted(held | set(bought.astype(str)))


def unpriced_holdings(
    raw_close: pd.DataFrame,
    windows: dict[str, list[Window]],
    as_of: str | pd.Timestamp,
) -> list[dict]:
    """Held stocks whose stored prices stop inside a holding window.

    The replay carries such a position at a terminal return from the session
    after its last close, so that return must be sourced.  A stock without any
    stored price is left to the replay, which refuses an unpriced entry.
    """
    as_of = pd.Timestamp(as_of).normalize()
    rows = []
    for ticker, spans in sorted(windows.items()):
        if ticker not in raw_close.columns:
            continue
        prices = raw_close[ticker].loc[:as_of].dropna()
        if prices.empty:
            continue
        last = pd.Timestamp(prices.index.max()).normalize()
        for entry, exit_date in spans:
            exit_date = min(exit_date, as_of)
            if entry <= last < exit_date:
                rows.append({
                    "ticker": ticker,
                    "last_price_date": f"{last:%Y-%m-%d}",
                    "holding_window": [f"{entry:%Y-%m-%d}", f"{exit_date:%Y-%m-%d}"],
                })
                break
    return rows


def inside_holdings(
    events: pd.DataFrame, windows: dict[str, list[Window]]
) -> pd.Series:
    """Mask of dated price events that fall inside their stock's holding window."""
    def inside(row) -> bool:
        date = pd.Timestamp(row["split_date"]).normalize()
        return any(
            entry < date <= exit_date
            for entry, exit_date in windows.get(str(row["ticker"]).upper(), [])
        )

    if events.empty:
        return pd.Series(dtype=bool)
    return events.apply(inside, axis=1).astype(bool)


def empty_supplement() -> pd.DataFrame:
    return pd.DataFrame(columns=list(SUPPLEMENT_COLUMNS), dtype=str)


def _number(value: str) -> float | None:
    text = str(value).strip()
    if not text:
        return None
    number = float(text)
    if not math.isfinite(number):
        raise ValueError(f"non-finite number: {text}")
    return number


def validate_supplement(frame: pd.DataFrame) -> None:
    """Refuse a supplement that is not a clean, sourced, ordered event list."""
    if list(frame.columns) != list(SUPPLEMENT_COLUMNS):
        raise ValueError(
            f"sourced event supplement columns must be {list(SUPPLEMENT_COLUMNS)}"
        )
    previous = None
    seen: set[tuple[str, str, str]] = set()
    for position, row in enumerate(frame.itertuples(index=False), start=1):
        label = f"sourced event {position}"
        if str(row.event_id) != str(position):
            raise ValueError(f"{label}: event_id must be {position}")
        recorded = pd.Timestamp(row.recorded_at)
        if recorded.tzinfo is None:
            raise ValueError(f"{label}: recorded_at must be timezone-aware")
        if previous is not None and recorded < previous:
            raise ValueError(f"{label}: recorded_at goes back in time")
        previous = recorded
        ticker = str(row.ticker)
        if not ticker or ticker != ticker.strip().upper():
            raise ValueError(f"{label}: ticker must be upper-case and trimmed")
        if row.event_type not in EVENT_TYPES:
            raise ValueError(f"{label}: unsupported event_type {row.event_type}")
        if str(row.event_date) != f"{pd.Timestamp(row.event_date):%Y-%m-%d}":
            raise ValueError(f"{label}: event_date must be YYYY-MM-DD")
        if not str(row.source_url).startswith(("https://", "http://")):
            raise ValueError(f"{label}: an http(s) source_url is required")
        factor = _number(row.adjustment_factor)
        terminal = _number(row.terminal_return)
        if row.event_type == SPLIT:
            if factor is None or factor <= 0 or factor == 1 or terminal is not None:
                raise ValueError(
                    f"{label}: a SPLIT needs a positive adjustment_factor other "
                    "than 1 and no terminal_return"
                )
        elif row.event_type == MARKET_MOVE:
            if factor is not None or terminal is not None:
                raise ValueError(f"{label}: a MARKET_MOVE carries no numbers")
        elif terminal is None or terminal < -1.0 or factor is not None:
            raise ValueError(
                f"{label}: a TERMINAL_RETURN needs terminal_return >= -1 and no "
                "adjustment_factor"
            )
        kind = "terminal" if row.event_type == TERMINAL_RETURN else "jump"
        key = (ticker, str(row.event_date), kind)
        if key in seen:
            raise ValueError(f"{label}: duplicates an earlier {kind} event")
        seen.add(key)


def load_supplement(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    if not path.is_file():
        return empty_supplement()
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    validate_supplement(frame)
    return frame


def supplement_digest(frame: pd.DataFrame) -> str:
    """Order-sensitive digest of supplement rows exactly as stored."""
    text = frame[list(SUPPLEMENT_COLUMNS)].to_csv(index=False, lineterminator="\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def append_supplement_event(path: str | Path, event: dict) -> dict:
    """Append one validated event; earlier rows are rewritten byte-for-byte."""
    path = Path(path)
    frame = load_supplement(path)
    row = {column: "" for column in SUPPLEMENT_COLUMNS}
    row.update({key: "" if value is None else str(value) for key, value in event.items()})
    row["event_id"] = str(len(frame) + 1)
    candidate = pd.concat(
        [frame, pd.DataFrame([row], columns=list(SUPPLEMENT_COLUMNS), dtype=str)],
        ignore_index=True,
    )
    validate_supplement(candidate)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        candidate.to_csv(handle, index=False, lineterminator="\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return row


def supplement_validation_rows(frame: pd.DataFrame) -> pd.DataFrame:
    """Split and market-move events in the corporate-action validation schema."""
    jumps = frame.loc[frame["event_type"].isin([SPLIT, MARKET_MOVE])]
    dates = pd.to_datetime(jumps["event_date"], errors="raise").dt.normalize()
    split = jumps["event_type"].eq(SPLIT)
    return pd.DataFrame({
        "ticker": jumps["ticker"].astype(str),
        "split_date": dates,
        "validation_status": split.map(
            {True: "CONFIRMED", False: "CONFIRMED_MARKET_MOVE"}
        ),
        "confirmed_action_type": split.map(
            {True: "SPLIT", False: "MARKET_MOVE_NO_ADJUSTMENT"}
        ),
        "confirmed_action_date": dates,
        "confirmed_adjustment_factor": pd.to_numeric(
            jumps["adjustment_factor"].replace("", None), errors="coerce"
        ),
        "primary_source": jumps["source_url"].astype(str),
    }).reset_index(drop=True)


def supplement_terminal_returns(
    frame: pd.DataFrame,
) -> dict[tuple[str, pd.Timestamp], float]:
    rows = frame.loc[frame["event_type"].eq(TERMINAL_RETURN)]
    return {
        (str(row.ticker), pd.Timestamp(row.event_date).normalize()): float(
            row.terminal_return
        )
        for row in rows.itertuples(index=False)
    }


def carry_frozen_rows(
    fresh: pd.DataFrame,
    frozen: pd.DataFrame,
    through: str | pd.Timestamp,
    compared: list[str],
) -> tuple[pd.DataFrame, dict]:
    """Keep ``frozen`` rows dated on or before ``through``; append later ``fresh`` rows.

    Returns the combined rows in ``frozen``'s columns and an audit of how the
    fresh download differs from the frozen rows it was not allowed to replace.
    """
    through = pd.Timestamp(through).normalize()
    fresh = fresh.copy()
    frozen = frozen.copy()
    fresh["date"] = pd.to_datetime(fresh["date"], errors="raise").dt.normalize()
    frozen["date"] = pd.to_datetime(frozen["date"], errors="raise").dt.normalize()
    fresh = fresh.drop_duplicates("date", keep="last").sort_values("date")
    kept = frozen.loc[frozen["date"].le(through)].sort_values("date")
    appended = fresh.loc[fresh["date"].gt(through)].reindex(columns=frozen.columns)
    combined = pd.concat([kept, appended], ignore_index=True)

    columns = [column for column in compared if column in frozen.columns]
    old = kept.set_index("date")[columns].apply(pd.to_numeric, errors="coerce")
    new = (
        fresh.loc[fresh["date"].le(through)]
        .set_index("date")
        .reindex(columns=columns)
        .apply(pd.to_numeric, errors="coerce")
    )
    joined = old.join(new, how="outer", lsuffix="_frozen", rsuffix="_fresh")
    revised = pd.Series(False, index=joined.index)
    for column in columns:
        left = joined[f"{column}_frozen"]
        right = joined[f"{column}_fresh"]
        revised |= left.isna().ne(right.isna()) | (
            left.notna() & right.notna() & left.ne(right)
        )
    sample = joined.loc[revised].head(REVISION_SAMPLE_ROWS)
    audit = {
        "frozen_through": f"{through:%Y-%m-%d}",
        "frozen_rows_kept": int(len(kept)),
        "fresh_rows_appended": int(len(appended)),
        "revised_frozen_rows": int(revised.sum()),
        "revised_sample": [
            {
                "date": f"{date:%Y-%m-%d}",
                **{
                    name: None if pd.isna(value) else float(value)
                    for name, value in values.items()
                },
            }
            for date, values in sample.iterrows()
        ],
    }
    return combined, audit


def rows_digest_text(
    frame: pd.DataFrame, columns: list[str], through: str | pd.Timestamp
) -> str:
    """Canonical numeric text of the rows dated on or before ``through``.

    Values are compared as floats, so a column that pandas later reads as
    float instead of int (for example after a missing value arrives) cannot
    change the digest of rows that did not change.
    """
    through = pd.Timestamp(through).normalize()
    dates = pd.to_datetime(frame["date"], errors="raise").dt.normalize()
    keep = dates.le(through)
    values = frame.loc[keep, columns].apply(pd.to_numeric, errors="coerce")
    rows = sorted(
        zip(dates.loc[keep], values.itertuples(index=False, name=None), strict=True),
        key=lambda item: item[0],
    )
    return "\n".join(
        f"{date:%Y-%m-%d}," + ",".join(repr(float(value)) for value in row)
        for date, row in rows
    )


def split_like_moves(
    raw_close: pd.DataFrame,
    tickers=None,
    start: str | pd.Timestamp | None = None,
    end: str | pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Raw one-session moves a split could explain, dated by their session.

    ``reason`` is ``COMMON_SPLIT_RATIO`` for a ratio within JUMP_TOLERANCE of a
    whole split factor and ``LARGE_MOVE`` for any ratio at or beyond
    LARGE_MOVE_LOW / LARGE_MOVE_HIGH.  Sessions without a close are skipped,
    so a move across a gap is dated at the session trading resumed.
    """
    columns = list(raw_close.columns)
    if tickers is not None:
        names = {str(ticker).upper() for ticker in tickers}
        columns = [column for column in columns if str(column).upper() in names]
    frame = raw_close[columns]
    if end is not None:
        frame = frame.loc[: pd.Timestamp(end)]
    found: dict[tuple[str, pd.Timestamp], dict] = {}
    for event in detect_common_split_events(frame).itertuples(index=False):
        key = (str(event.ticker).upper(), pd.Timestamp(event.split_date).normalize())
        found[key] = {
            "ticker": key[0],
            "split_date": key[1],
            "raw_price_ratio": float(event.raw_price_ratio),
            "reason": "COMMON_SPLIT_RATIO",
        }
    for column in frame.columns:
        ratios = frame[column].dropna().pct_change(fill_method=None).add(1).dropna()
        large = ratios.loc[ratios.le(LARGE_MOVE_LOW) | ratios.ge(LARGE_MOVE_HIGH)]
        for date, ratio in large.items():
            key = (str(column).upper(), pd.Timestamp(date).normalize())
            found.setdefault(key, {
                "ticker": key[0],
                "split_date": key[1],
                "raw_price_ratio": float(ratio),
                "reason": "LARGE_MOVE",
            })
    result = pd.DataFrame(
        list(found.values()),
        columns=["ticker", "split_date", "raw_price_ratio", "reason"],
    )
    if start is not None:
        result = result.loc[result["split_date"].ge(pd.Timestamp(start).normalize())]
    if end is not None:
        result = result.loc[result["split_date"].le(pd.Timestamp(end).normalize())]
    return result.sort_values(["split_date", "ticker"], ignore_index=True)


def unexplained_moves(
    raw_close: pd.DataFrame,
    validation: pd.DataFrame,
    tickers=None,
    start: str | pd.Timestamp | None = None,
    end: str | pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Split-like moves without a resolved event on the same stock and session."""
    moves = split_like_moves(raw_close, tickers, start, end)
    if moves.empty:
        return moves
    resolved = validation.loc[
        validation["validation_status"].isin(["CONFIRMED", "CONFIRMED_MARKET_MOVE"])
    ]
    known = set(zip(
        resolved["ticker"].astype(str).str.upper(),
        pd.to_datetime(resolved["split_date"]).dt.normalize(),
        strict=True,
    ))
    explained = [
        (row.ticker, row.split_date) in known for row in moves.itertuples(index=False)
    ]
    return moves.loc[[not flag for flag in explained]].reset_index(drop=True)
