#!/usr/bin/env python3
"""Runner for the sue-lt-v1 forward observation (docs/sue_lt_v1_protocol.md).

    PYTHONPATH=. python scripts/sue_lt_v1.py status
    PYTHONPATH=. python scripts/sue_lt_v1.py check          # what is due now
    PYTHONPATH=. python scripts/sue_lt_v1.py run [--push]   # do what is due
    PYTHONPATH=. python scripts/sue_lt_v1.py rehearse --as-of 2026-09-30   # a recent month end
    PYTHONPATH=. python scripts/sue_lt_v1.py freeze-protocol   # once, on the live branch

Writes happen only on the ``live/sue-lt-v1`` branch, whose code is pinned
by the frozen protocol's import closure. ``rehearse`` runs a SIGNAL for any
past month end into a scratch directory and records nothing.

Nasdaq publishes a session's rows hours after the close. A SIGNAL or MARK
started before that raises an error containing "retry later" and records
nothing; the scheduler runs it again.

Exit codes: 0 done or nothing due; 1 error; 2 a SIGNAL window was missed
(no backfill: the book stays as it is until the next month end); 3 the
protocol is not frozen.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
from urllib.request import Request, urlopen

import exchange_calendars
import numpy as np
import pandas as pd

from src.conf import CLEANED_PRICE_DATA_DIR
from src.financial.quarterly_fundamentals import load_quarterly_fundamentals
from src.io import fundamentals_update, nasdaq_update
from src.io.financial_update import SEC_HEADERS
from src.io.nasdaq_update import fetch_history, refresh_universe
from src.io.security_universe import investable_common_equities
from src.research import prospective_schedule as schedule
from src.research import sue_live as live
from src.research.code_closure import closure_digest, project_import_closure
from src.research.panel_data import load_panel
from src.research.universe_history import known_non_common_symbols
from scripts.research_v5_trend_core_satellite import refresh_core_price

BRANCH = "live/sue-lt-v1"
ROOT = Path("output/research_only/sue_lt_v1")
LEDGER = ROOT / "ledger.jsonl"
PROTOCOL = ROOT / "frozen_protocol.json"
SIGNALS = ROOT / "signals"
WORK = Path("research_cache/sue_lt_v1")
PROTOCOL_DOC = Path("docs/sue_lt_v1_protocol.md")
DATA_RELEASE = Path("data_release/latest.json")
CLOSURE_ROOTS = ("scripts/sue_lt_v1.py",)
FORBIDDEN_ETFS = {"QQQ", "TQQQ", "SQQQ", "QQQM", "ONEQ"}
MINIMUM_SUE_COVERAGE = 0.6
MINIMUM_PRICE_COVERAGE = 0.98
# A name this close to the pool's liquidity floor must have a signal-day
# close (v50r3's fix for a silently dropped name); a failed download is
# judged by its stored history, which may be older, so more widely.
POOL_VICINITY = 0.8
FAILED_DOWNLOAD_VICINITY = 0.5
# Downloads still failing after the usual retries get these longer pauses.
DOWNLOAD_RETRY_PAUSES_SECONDS = (300, 600)
# The SIGNAL panel starts 420 days before the signal, and load_panel reads
# 400 days before its start: a download from 830 days back covers every row
# the pool rules can see, in a smaller response than the full history.
PANEL_DAYS = 420
DOWNLOAD_DAYS = PANEL_DAYS + 400 + 10
SEC_SUBMISSIONS_API = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
# A MARK asks again for a name whose price history came back empty.
EMPTY_RETRY_PAUSES_SECONDS = (5, 20)
# A one-session move this close to a split ratio waits one day: the provider
# may not have back-adjusted the history yet.
SPLIT_FACTORS = tuple(sorted({f for n in (2, 3, 4, 5, 10, 20) for f in (n, 1 / n)} | {1.5, 2 / 3}))
SPLIT_TOLERANCE = 0.01
# Amendments do not add a quarter, so they are not compared.
PERIODIC_FORMS = {"10-Q", "10-K"}
SEC_RETRY_PAUSES_SECONDS = (90, 180)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _branch() -> str:
    return subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True,
                          text=True, check=True).stdout.strip()


def _require_live_branch() -> None:
    if _branch() != BRANCH:
        raise RuntimeError(f"sue-lt-v1 writes only on {BRANCH}")


def runtime_environment() -> dict:
    return {
        "python": platform.python_version(),
        "pandas": pd.__version__,
        "numpy": np.__version__,
        "exchange_calendars": exchange_calendars.__version__,
    }


def data_release() -> dict:
    """The data release a SIGNAL restores: its manifest and the manifest's hash."""
    manifest = json.loads(DATA_RELEASE.read_text(encoding="utf-8"))
    return {"tag": manifest.get("tag"), "archive_sha256": manifest.get("sha256"),
            "manifest_sha256": _sha256(DATA_RELEASE)}


# ------------------------------------------------------------------ protocol


def closure() -> dict:
    files = project_import_closure(list(CLOSURE_ROOTS), Path.cwd())
    return {"files": files, "file_count": len(files), "sha256": closure_digest(files)}


def validated_protocol() -> tuple[dict, str]:
    if not PROTOCOL.is_file():
        raise FileNotFoundError("sue-lt-v1 protocol is not frozen")
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    current = closure()
    if current["sha256"] != protocol["code_closure"]["sha256"]:
        changed = sorted(
            path for path in set(current["files"]) | set(protocol["code_closure"]["files"])
            if current["files"].get(path) != protocol["code_closure"]["files"].get(path)
        )
        raise RuntimeError("sue-lt-v1 code changed since the freeze: " + ", ".join(changed))
    if _sha256(PROTOCOL_DOC) != protocol["protocol_document_sha256"]:
        raise RuntimeError("the sue-lt-v1 protocol document changed since the freeze")
    if data_release() != protocol["data_release"]:
        raise RuntimeError("the data release changed since the freeze")
    return protocol, _sha256(PROTOCOL)


def freeze_protocol() -> dict:
    _require_live_branch()
    if PROTOCOL.exists() or LEDGER.exists():
        raise RuntimeError("sue-lt-v1 is already frozen")
    now = _now()
    first_opens, _ = schedule.signal_window(live.FIRST_SIGNAL_DATE)
    if now >= first_opens:
        raise RuntimeError("the first SIGNAL window has opened; freeze a later version instead")
    if "状态：**生效**" not in PROTOCOL_DOC.read_text(encoding="utf-8"):
        raise RuntimeError("the protocol document is still a draft; mark it 生效 on master first")
    protocol = {
        "model_version": live.MODEL_VERSION,
        "frozen_at": now.isoformat(timespec="seconds"),
        "protocol_document": str(PROTOCOL_DOC),
        "protocol_document_sha256": _sha256(PROTOCOL_DOC),
        "first_signal_date": live.FIRST_SIGNAL_DATE.strftime("%Y-%m-%d"),
        "end_date": live.END_DATE.strftime("%Y-%m-%d"),
        "parameters": {
            "pool": live.POOL, "holdings": live.HOLDINGS, "minimum_price": live.MINIMUM_PRICE,
            "minimum_history_sessions": live.MINIMUM_HISTORY, "liquidity_sessions": live.LIQUIDITY_SESSIONS,
            "stale_sessions": live.STALE_SESSIONS, "start_cash": live.START_CASH, "cost_model": live.COST_MODEL,
            "minimum_buy": live.MINIMUM_BUY,
        },
        "evaluation": {
            "months": 24, "benchmark": "QQQ total return, buy and hold",
            "pass": "nav above qqq_nav at the final valuation, after the protocol's manual adjustments",
            "points_behind_qqq": "100 * (qqq_nav - nav) / start_cash, at every valuation, reported only",
            "early_stop": None,
            "report_only_benchmark": "QQEW total return over the same sessions",
        },
        "data_release": data_release(),
        "runtime_environment_at_freeze": runtime_environment(),
        "code_closure": closure(),
        "broker_connection_used": False,
        "order_created": False,
    }
    ROOT.mkdir(parents=True, exist_ok=True)
    PROTOCOL.write_text(json.dumps(protocol, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    live.append_event(LEDGER, _sha256(PROTOCOL), "PROTOCOL_FROZEN",
                      {"first_signal_date": protocol["first_signal_date"]})
    return {"status": "FROZEN", "code_closure_files": protocol["code_closure"]["file_count"]}


# ------------------------------------------------------------------ decisions


def _signal_events(events: list[dict]) -> list[dict]:
    return [e for e in events if e["event_type"] == "SIGNAL_FROZEN"]


def _executed(events: list[dict]) -> set[str]:
    return {e["payload"]["signal_date"] for e in events if e["event_type"] == "TRADES_EXECUTED"}


def _last_valuation(events: list[dict]) -> dict | None:
    marks = [e for e in events if e["event_type"] == "VALUATION_APPENDED"]
    return marks[-1]["payload"] if marks else None


def _terminal(events: list[dict]) -> dict | None:
    ends = [e for e in events if e["event_type"] == "TERMINAL_RECORDED"]
    return ends[-1]["payload"] if ends else None


def decide(now: datetime, events: list[dict]) -> dict:
    terminal = _terminal(events)
    if terminal is not None:
        return {"action": "NOTHING_DUE", "as_of": None, "missed": [], "terminal": terminal}
    frozen = {e["payload"]["signal_date"] for e in _signal_events(events)}
    completed = schedule.latest_completed_session(now)
    settled = completed is not None and completed > live.END_DATE
    # After END_DATE the final MARK is still as of END_DATE; whether Nasdaq
    # has published is judged on the latest completed session instead.
    extra = {"gate_session": completed.strftime("%Y-%m-%d")} if settled else {}
    if settled:
        completed = live.END_DATE
    missed = []
    horizon = min(pd.Timestamp(now.date()) + pd.Timedelta(days=7), live.END_DATE - pd.Timedelta(days=1))
    for month_end in schedule.month_end_sessions(live.FIRST_SIGNAL_DATE, horizon):
        day = month_end.strftime("%Y-%m-%d")
        opens, closes = schedule.signal_window(month_end)
        if day in frozen:
            continue
        if opens <= now < closes:
            return {"action": "RUN_SIGNAL", "as_of": day, "missed": missed}
        if now >= closes:
            missed.append(day)
    pending = sorted(frozen - _executed(events))
    last = _last_valuation(events)
    if completed is not None and frozen:
        if pending and schedule.next_session(pd.Timestamp(pending[0])) <= completed:
            return {"action": "RUN_MARK", "as_of": completed.strftime("%Y-%m-%d"), "missed": missed, **extra}
        if last is not None and pd.Timestamp(last["as_of"]) < completed:
            return {"action": "RUN_MARK", "as_of": completed.strftime("%Y-%m-%d"), "missed": missed, **extra}
        if settled and (last is not None or pending):
            # END_DATE has passed without a terminal record: the final MARK writes it.
            return {"action": "RUN_MARK", "as_of": completed.strftime("%Y-%m-%d"), "missed": missed, **extra}
    return {"action": "NOTHING_DUE", "as_of": None, "missed": missed}


# ------------------------------------------------------------------ SIGNAL


def _require_published(session: pd.Timestamp) -> None:
    """Fail in seconds, not after hours of downloads, when ``session`` is not published.

    The request spans DOWNLOAD_DAYS: for a session months back (a rehearsal)
    Nasdaq drops rows from short ranges, but serves long ones whole.
    """
    start = (session - pd.Timedelta(days=DOWNLOAD_DAYS)).date()
    frame = fetch_history("QQQ", start, session.date(), asset_class="etf", retries=2)
    if not pd.to_datetime(frame["date"]).dt.normalize().eq(session).any():
        raise RuntimeError(f"Nasdaq has not published the {session:%Y-%m-%d} QQQ close; retry later")


def _fetch_fresh(ticker: str, as_of: pd.Timestamp, price_dir: Path) -> dict:
    try:
        start = (as_of - pd.Timedelta(days=DOWNLOAD_DAYS)).date()
        frame = nasdaq_update.fetch_history(ticker, start, as_of.date())
        rows = nasdaq_update._atomic_merge(price_dir / f"{ticker.lower()}.csv", frame, ticker)
        return {"ticker": ticker, "status": "updated" if rows else "no_data", "rows": rows}
    except Exception as exc:  # reported per name, retried below
        return {"ticker": ticker, "status": "failed", "error": f"{type(exc).__name__}: {exc}"}


def sec_ticker_map() -> dict[str, int]:
    """SEC's ticker-to-CIK map, fetched before the hours of price downloads."""
    error = None
    for pause in (0, *SEC_RETRY_PAUSES_SECONDS):
        time.sleep(pause)
        try:
            return {str(t).upper(): int(c) for t, c in fundamentals_update.fetch_sec_ticker_map().items()}
        except Exception as exc:  # refused or timed out: ask again
            error = exc
    raise RuntimeError(f"the SEC ticker map is unavailable ({type(error).__name__}: {error}); retry later")


def download_prices(symbols: list[str], as_of: pd.Timestamp, price_dir: Path, workers: int) -> dict:
    """Each name's history as the provider serves it on the signal evening.

    Nothing is seeded from stored files, so a reused ticker cannot splice a
    former security's rows onto the new one and no stored tail can fail to
    reconcile. Names still failing after the pauses are returned.
    """
    if price_dir.exists():
        shutil.rmtree(price_dir)
    price_dir.mkdir(parents=True)
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for item in pool.map(lambda t: _fetch_fresh(t, as_of, price_dir), symbols):
            results[item["ticker"]] = item
    short = len(nasdaq_update.FINAL_RETRY_PAUSES_SECONDS)
    for round_, pause in enumerate((*nasdaq_update.FINAL_RETRY_PAUSES_SECONDS, *DOWNLOAD_RETRY_PAUSES_SECONDS)):
        # An empty history is usually a name without one (a listing made that
        # day); it gets the short pauses only, a failed request all of them.
        again = [t for t, item in results.items()
                 if item["status"] == "failed" or (item["status"] == "no_data" and round_ < short)]
        if not again:
            break
        time.sleep(pause)
        for ticker in again:
            results[ticker] = _fetch_fresh(ticker, as_of, price_dir)
    counts = pd.Series([item["status"] for item in results.values()]).value_counts().to_dict()
    return {
        "counts": {str(k): int(v) for k, v in counts.items()},
        "failed": {t: item.get("error") for t, item in sorted(results.items()) if item["status"] == "failed"},
        "no_data": sorted(t for t, item in results.items() if item["status"] == "no_data"),
    }


def _stored_liquidity(ticker: str) -> float | None:
    """50-row median dollar volume in the restored data release, for a name that failed to download."""
    path = Path(CLEANED_PRICE_DATA_DIR) / f"{ticker.lower()}.csv"
    try:
        frame = pd.read_csv(path, usecols=["date", "close", "volume"]).tail(live.LIQUIDITY_SESSIONS)
    except (OSError, ValueError):  # no stored file, or not a price file
        return None
    value = (frame["close"] * frame["volume"]).median()
    return None if pd.isna(value) else float(value)


def price_gate(close: pd.DataFrame, dollar_volume: pd.DataFrame, symbols: set[str],
               as_of: pd.Timestamp, unfetched: dict[str, float | None],
               must_have: set[str] = frozenset()) -> dict:
    """Names that could be in the pool but have no signal-day close.

    ``unpriced`` traded the session before and may simply be late: the SIGNAL
    waits for them. ``halted`` had no close that session either and is left
    out, as the rule leaves out any name without a signal-day close.
    ``unfetched`` maps each name whose download failed or came back empty to
    its liquidity in the restored data release (None without a stored file).
    The SIGNAL also waits for one of them that is in ``must_have`` (the
    holdings and the previous signal's pool) or was near the pool floor then.
    """
    names = sorted(set(symbols) & set(close.columns))
    frame = close.loc[:as_of, names]
    pool = live.pool_liquidity(close, dollar_volume, symbols, as_of)
    floor = float(pool.min()) if len(pool) >= live.POOL else 0.0
    liquidity = dollar_volume.reindex_like(frame).tail(live.LIQUIDITY_SESSIONS).median()
    near = (liquidity.ge(POOL_VICINITY * floor)
            & frame.notna().sum().ge(live.MINIMUM_HISTORY - 1)
            & frame.ffill().iloc[-1].ge(POOL_VICINITY * live.MINIMUM_PRICE)
            & frame.iloc[-1].isna())
    candidates = sorted(near[near].index)
    previous = frame.index[-2] if len(frame.index) > 1 else None
    halted = [t for t in candidates if previous is None or pd.isna(frame.at[previous, t])]
    return {
        "pool_floor": floor,
        "unpriced": [t for t in candidates if t not in halted],
        "halted": halted,
        "failed_near_pool": sorted(t for t, value in unfetched.items() if t in must_have or (
            value is not None and value >= FAILED_DOWNLOAD_VICINITY * floor)),
    }


def panel_excluded(symbols: list[str], close: pd.DataFrame, unfetched: dict) -> dict[str, str]:
    """Why each universe name is not in the SIGNAL panel."""
    known = known_non_common_symbols()
    reasons = {}
    for ticker in sorted(set(symbols) - set(close.columns)):
        if ticker in unfetched:
            reasons[ticker] = "download failed or empty"
        elif ticker in known:
            reasons[ticker] = "non-common in the data release's listing records"
        else:
            reasons[ticker] = "fewer than 150 sessions in the panel window"
    return reasons


def refresh_sec(as_of: pd.Timestamp, work: Path, universe_path: Path, tickers: list[str],
                ticker_map: dict[str, int]) -> dict:
    """First-reported SEC quarters for ``tickers``, touching nothing outside ``work``.

    The refresher's state and audit files go to ``work``; it uses the ticker
    map already fetched; and, as on a GitHub runner, where the reviewed
    foreign-quarter registry is absent, no registry is read.
    """
    replacements = {
        "NASDAQ_300M_STOCK_LIST_FILE": str(universe_path),
        "FUNDAMENTALS_REFRESH_STATE_FILE": str(work / "refresh_state.json"),
        "FUNDAMENTALS_COVERAGE_FILE": str(work / "coverage.json"),
        "QUARTERLY_FUNDAMENTALS_COVERAGE_FILE": str(work / "quarterly_coverage.json"),
        "VALIDATED_FOREIGN_QUARTERLY_FILE": work / "no_validated_foreign_quarterly.csv",
        "fetch_sec_ticker_map": lambda: dict(ticker_map),
    }
    original = {name: getattr(fundamentals_update, name) for name in replacements}
    try:
        for name, value in replacements.items():
            setattr(fundamentals_update, name, value)
        audit = fundamentals_update.update_fundamentals(
            as_of=as_of.date(), workers=4, refresh_after_days=0,
            output=work / "fundamentals.csv", quarterly_output=work / "quarterly.csv",
            force=True, tickers=tickers, cache_dir=work / "companyfacts_cache",
        )
    finally:
        for name, value in original.items():
            setattr(fundamentals_update, name, value)
    return {str(item["ticker"]).upper(): str(item.get("reason")) for item in audit.get("failures", [])}


def _permanent_sec_failure(reason: str) -> bool:
    """No SEC financial data for the company (a foreign filer), as opposed to a refused request."""
    return reason == "no_sec_fundamentals" or "HTTP Error 404" in reason


def refresh_sec_with_retries(as_of: pd.Timestamp, work: Path, universe_path: Path,
                             tickers: list[str], ticker_map: dict[str, int]) -> dict:
    """refresh_sec, asking again for names whose request was refused or timed out.

    A company without SEC financial data (a foreign filer) is not asked again.
    The refresher merges each pass into the same output files.
    """
    failures = refresh_sec(as_of, work, universe_path, tickers, ticker_map)
    for pause in SEC_RETRY_PAUSES_SECONDS:
        transient = sorted(t for t, reason in failures.items() if not _permanent_sec_failure(reason))
        if not transient:
            break
        time.sleep(pause)
        again = refresh_sec(as_of, work, universe_path, transient, ticker_map)
        for ticker in transient:
            if ticker in again:
                failures[ticker] = again[ticker]
            else:
                failures.pop(ticker)
    return failures


def sue_gaps(pool: list[str], sue: dict, quarterly: pd.DataFrame, as_of: pd.Timestamp,
             window_start: pd.Timestamp, ticker_map: dict[str, int], sec_failures: dict) -> dict:
    """Why each pool name has no SUE."""
    known = quarterly.loc[pd.to_datetime(quarterly["available_date"]).le(as_of)]
    income = live.first_reported_net_income(known)
    gaps = {}
    for ticker in pool:
        if ticker in sue:
            continue
        if ticker not in ticker_map:
            gaps[ticker] = {"reason": "no_sec_cik"}
            continue
        if ticker in sec_failures:
            reason = "no_sec_financial_data" if _permanent_sec_failure(sec_failures[ticker]) else "sec_refresh_failed"
            gaps[ticker] = {"reason": reason, "detail": sec_failures[ticker]}
            continue
        rows = income.loc[income["ticker"].eq(ticker)].sort_values("fiscal_end")
        if rows.empty:
            gaps[ticker] = {"reason": "no_quarterly_net_income"}
            continue
        latest = rows.iloc[-1]
        entry = {"latest_fiscal_end": f"{pd.Timestamp(latest['fiscal_end']):%Y-%m-%d}",
                 "latest_available": f"{pd.Timestamp(latest['available_date']):%Y-%m-%d}"}
        if pd.Timestamp(latest["available_date"]) < window_start:
            gaps[ticker] = {"reason": "latest_quarter_before_window", **entry}
        else:
            gaps[ticker] = {"reason": "too_few_year_over_year_changes", **entry}
    return gaps


def sec_freshness(pool: list[str], quarterly: pd.DataFrame, as_of: pd.Timestamp,
                  ticker_map: dict[str, int]) -> dict[str, dict]:
    """For each pool name with a CIK: is EDGAR ahead of companyfacts? (a record only)

    Behind means EDGAR has a 10-Q or 10-K filed later than the latest quarter
    companyfacts reports: that name has no SUE this month, or is ranked on
    the quarter before.
    """
    known = quarterly.loc[pd.to_datetime(quarterly["available_date"]).le(as_of)]
    income = live.first_reported_net_income(known)
    latest = income.groupby("ticker")["available_date"].max() if len(income) else pd.Series(dtype=object)
    out = {}
    for ticker in pool:
        if ticker not in ticker_map:
            continue
        filing = latest_periodic_filing(ticker_map[ticker], as_of)
        time.sleep(0.2)  # SEC asks for at most ten requests a second
        edgar = filing.get("edgar_latest_periodic") or {}
        reported = pd.Timestamp(latest[ticker]) if ticker in latest.index else None
        out[ticker] = {
            **filing,
            "companyfacts_latest_available": None if reported is None else f"{reported:%Y-%m-%d}",
            "companyfacts_behind_edgar": bool(edgar) and (reported is None or pd.Timestamp(edgar["filed"]) > reported),
        }
    return out


def latest_periodic_filing(cik: int, as_of: pd.Timestamp) -> dict:
    """The latest 10-Q or 10-K on EDGAR filed by ``as_of`` (a record only; selection ignores it)."""
    try:
        request = Request(SEC_SUBMISSIONS_API.format(cik=int(cik)), headers=SEC_HEADERS)
        with urlopen(request, timeout=30) as response:
            recent = json.load(response)["filings"]["recent"]
        filings = [
            (filed, form, period)
            for form, filed, period in zip(recent["form"], recent["filingDate"], recent["reportDate"])
            if form in PERIODIC_FORMS and pd.Timestamp(filed) <= as_of
        ]
    except Exception as exc:
        return {"edgar_error": f"{type(exc).__name__}: {exc}"}
    if not filings:
        return {"edgar_latest_periodic": None}
    filed, form, period = max(filings)
    return {"edgar_latest_periodic": {"form": form, "filed": filed, "period": period}}


def stage_signal(as_of: pd.Timestamp, work: Path, workers: int, holdings: list[str],
                 previous_pool: list[str] = (), rehearsal: bool = False) -> dict:
    """Refresh universe, prices and SEC quarters for one month end; select targets.

    ``rehearsal`` is for a past month end: today's universe then holds names
    listed after it, whose empty histories must not hold the gates.
    """
    _require_published(as_of)
    ticker_map = sec_ticker_map()
    work.mkdir(parents=True, exist_ok=True)
    universe_path = work / "universe.csv"
    refresh_universe(as_of.date(), min_market_cap=0, target_path=universe_path, common_equities_only=True)
    current = investable_common_equities(pd.read_csv(universe_path, keep_default_na=False))
    symbols = sorted(set(current["Symbol"].dropna().astype(str).str.upper()) - FORBIDDEN_ETFS)
    price_dir = work / "prices"
    download = download_prices(symbols, as_of, price_dir, workers)
    start = (as_of - pd.Timedelta(days=PANEL_DAYS)).strftime("%Y-%m-%d")

    def panel() -> tuple[pd.DataFrame, pd.DataFrame]:
        close, dollar_volume = load_panel(price_dir, start, as_of.strftime("%Y-%m-%d"))
        # The panel loader drops known non-common securities, as in the backtest;
        # coverage is measured over the names that can enter the pool.
        return close.reindex(columns=[c for c in close.columns if c in set(symbols)]), dollar_volume

    close, dollar_volume = panel()
    unfetched = {t: _stored_liquidity(t)
                 for t in [*download["failed"], *([] if rehearsal else download["no_data"])]}
    priced = close.loc[as_of].notna().sum() if as_of in close.index else 0
    price_coverage = priced / max(close.shape[1] + len(unfetched), 1)
    if price_coverage < MINIMUM_PRICE_COVERAGE:
        raise RuntimeError(f"only {price_coverage:.1%} of the universe has a {as_of:%Y-%m-%d} close; retry later")
    must_have = set(holdings) | set(previous_pool)
    gate = price_gate(close, dollar_volume, set(symbols), as_of, unfetched, must_have)
    if gate["unpriced"]:
        for ticker in gate["unpriced"]:  # published since the first pass?
            _fetch_fresh(ticker, as_of, price_dir)
        close, dollar_volume = panel()
        gate = price_gate(close, dollar_volume, set(symbols), as_of, unfetched, must_have)
    if gate["unpriced"] or gate["failed_near_pool"]:
        names = ", ".join(gate["unpriced"] + gate["failed_near_pool"])
        raise RuntimeError(f"names near the pool have no {as_of:%Y-%m-%d} close ({names}); retry later")
    pool = live.liquidity_pool(close, dollar_volume, set(symbols), as_of)
    requested = sorted(set(pool) | set(holdings))
    mapped = [t for t in requested if t in ticker_map]
    sec_failures = refresh_sec_with_retries(as_of, work, universe_path, mapped, ticker_map)
    quarterly = load_quarterly_fundamentals(work / "quarterly.csv")
    if pd.to_datetime(quarterly["available_date"]).gt(as_of).any():
        quarterly = quarterly.loc[pd.to_datetime(quarterly["available_date"]).le(as_of)]
    result = live.select_targets(close, dollar_volume, set(symbols), quarterly, as_of, issuer=ticker_map)
    coverage = len(result["sue"]) / max(len(result["pool"]), 1)
    gaps = sue_gaps(result["pool"], result["sue"], quarterly, as_of,
                    pd.Timestamp(result["window_start"]), ticker_map, sec_failures)
    freshness = sec_freshness(result["pool"], quarterly, as_of, ticker_map)
    for ticker, gap in gaps.items():
        gap.update(freshness.get(ticker, {}))
    result.update({
        "universe_size": len(symbols),
        "price_coverage": price_coverage,
        "price_download": download,
        "price_gate": {"pool_floor": gate["pool_floor"], "halted_near_pool": gate["halted"],
                       "unfetched_stored_liquidity": unfetched},
        "panel_excluded": panel_excluded(symbols, close, unfetched),
        "sec_unmapped": sorted(set(requested) - set(mapped)),
        "sec_refresh_failures": sec_failures,
        "sue_coverage_of_pool": coverage,
        "sue_missing": gaps,
        "sue_from_older_quarter": sorted(t for t in result["sue"]
                                         if freshness.get(t, {}).get("companyfacts_behind_edgar")),
        "sec_freshness": freshness,
        "inputs": {"universe.csv": _sha256(universe_path), "quarterly.csv": _sha256(work / "quarterly.csv")},
        "data_release": data_release(),
        "runtime_environment": runtime_environment(),
    })
    if coverage < MINIMUM_SUE_COVERAGE:
        raise RuntimeError(f"SUE covers only {coverage:.0%} of the pool; the SEC refresh looks incomplete")
    return result


def run_signal(as_of: pd.Timestamp, protocol_sha: str, events: list[dict], workers: int) -> dict:
    opens, closes = schedule.signal_window(as_of)
    if not opens <= _now() < closes:
        raise RuntimeError(f"the {as_of:%Y-%m-%d} SIGNAL window is not open")
    last = _last_valuation(events)
    holdings = sorted((last or {}).get("book", {}).get("positions", {}))
    previous_pool: list[str] = []
    frozen = _signal_events(events)
    if frozen and (SIGNALS / frozen[-1]["payload"].get("signal_file", "")).is_file():
        previous_pool = json.loads((SIGNALS / frozen[-1]["payload"]["signal_file"]).read_text(encoding="utf-8"))["pool"]
    result = stage_signal(as_of, WORK / as_of.strftime("%Y-%m-%d"), workers, holdings, previous_pool)
    if _now() >= closes:
        raise RuntimeError("the SIGNAL window closed during staging; the month is missed")
    SIGNALS.mkdir(parents=True, exist_ok=True)
    path = SIGNALS / f"signal_{as_of:%Y-%m-%d}.json"
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    live.append_event(LEDGER, protocol_sha, "SIGNAL_FROZEN", {
        "signal_date": result["signal_date"], "targets": result["targets"],
        "signal_file": path.name, "signal_sha256": _sha256(path),
    })
    return {"action": "SIGNAL_FROZEN", "signal_date": result["signal_date"], "targets": result["targets"],
            "sue_coverage_of_pool": result["sue_coverage_of_pool"],
            "companyfacts_behind_edgar": sorted(t for t, g in result["sue_missing"].items()
                                                if g.get("companyfacts_behind_edgar")),
            "sue_from_older_quarter": result["sue_from_older_quarter"],
            "sec_refresh_failures": {t: r for t, r in result["sec_refresh_failures"].items()
                                     if not _permanent_sec_failure(r)},
            "no_sec_financial_data": sorted(t for t, r in result["sec_refresh_failures"].items()
                                            if _permanent_sec_failure(r)),
            "price_download_failed": sorted([*result["price_download"]["failed"],
                                             *result["price_download"]["no_data"]])}


# ------------------------------------------------------------------ MARK


def _closes(tickers: list[str], start: pd.Timestamp, end: pd.Timestamp) -> dict[str, pd.Series]:
    """Each name's closes; an empty response is asked again before it is believed."""
    out = {}
    for ticker in tickers:
        frame = fetch_history(ticker, start.date(), end.date())
        for pause in EMPTY_RETRY_PAUSES_SECONDS:
            if len(frame):
                break
            time.sleep(pause)
            frame = fetch_history(ticker, start.date(), end.date())
        out[ticker] = frame.set_index("date")["close"].astype(float) if len(frame) else pd.Series(dtype=float)
    return out


def _split_like(ratio: float) -> bool:
    """A one-session price ratio within 1% of a common split ratio."""
    return any(abs(ratio / factor - 1.0) <= SPLIT_TOLERANCE for factor in SPLIT_FACTORS)


def _qqq_total_returns(end: pd.Timestamp) -> pd.Series:
    """QQQ's return on each session it traded: close plus the ex-date cash dividend."""
    path = refresh_core_price(WORK / "qqq.csv")
    frame = pd.read_csv(path, parse_dates=["date"]).set_index("date").sort_index().loc[:end]
    return ((frame["close"] + frame["cash_dividend"]) / frame["close"].shift() - 1.0).dropna()


def _return_since(series: pd.Series, session: pd.Timestamp, since: str | pd.Timestamp) -> float | None:
    """A name's return from its close on ``since`` (the latest on or before it) to ``session``."""
    if session not in series.index:
        return None
    base = series.loc[:pd.Timestamp(since)]
    return float(series[session] / base.iloc[-1] - 1.0) if len(base) else None


def run_mark(as_of: pd.Timestamp, protocol_sha: str, events: list[dict],
             now: datetime | None = None) -> dict:
    """Value every session since the last valuation, executing each pending
    signal at the first session after it that the market traded.

    A session counts as traded when QQQ has a close for it: a calendar session
    without one (an unscheduled closure) is skipped. The latest session waits
    ("retry later") until QQQ and every name that should have a close have
    one, and a one-session move of a split ratio waits a day for the provider
    to back-adjust; so a late publication is never booked as a halt. Once a
    session after END_DATE has completed nothing waits any more, and the last
    valuation closes the observation.
    """
    as_of = min(pd.Timestamp(as_of), live.END_DATE)
    latest = schedule.latest_completed_session(now or _now())
    settled = as_of >= live.END_DATE and latest is not None and latest > live.END_DATE
    last = _last_valuation(events)
    executed = _executed(events)
    pending = [e["payload"] for e in _signal_events(events) if e["payload"]["signal_date"] not in executed]
    if last is None and not pending:
        return {"action": "NOTHING_DUE"}
    book = json.loads(json.dumps(last["book"])) if last else live.new_book()
    last_priced = book.setdefault("last_priced", {})
    for ticker in book["positions"]:
        last_priced.setdefault(ticker, book["as_of"])
    qqq_nav = last["qqq_nav"] if last else None
    first = pd.Timestamp(last["as_of"]) if last else pd.Timestamp(pending[0]["signal_date"])
    calendar = [s for s in schedule.sessions_between(first, as_of) if s > first]
    appended, retired_all, trades, terminal = [], [], [], None
    qqq = pd.Series(dtype=float)
    if calendar:
        qqq = _qqq_total_returns(latest if settled else as_of)
        # Settled, a missing END_DATE row counts as a closure only once QQQ has
        # a later row: the market reopened and Nasdaq published it.
        if as_of not in qqq.index and not (settled and qqq.index.max() > as_of):
            raise RuntimeError(f"Nasdaq has not published the {as_of:%Y-%m-%d} QQQ close; retry later")
        qqq = qqq.loc[:as_of]
    elif not settled:
        return {"action": "NOTHING_DUE"}
    traded = [s for s in calendar if s in qqq.index]
    executions = {}
    for signal in pending:
        day = next((s for s in traded if s > pd.Timestamp(signal["signal_date"])), None)
        if day is not None:
            executions[day] = signal
    tickers = sorted(set(book["positions"]) | {t for s in pending for t in s["targets"]})
    since = min([pd.Timestamp(d) for d in last_priced.values()] + [first])
    closes = _closes(tickers, since - pd.Timedelta(days=10), as_of) if traded else {}
    if traded and not settled:
        previous = traded[-2] if len(traded) > 1 else first
        executing = executions.get(as_of)
        # Names that should have an as_of close: they traded the session before,
        # or the ledger priced them then, or they are bought or sold today.
        base = {t: pd.Timestamp(d) for t, d in last_priced.items() if t in book["positions"]}
        for session, signal in executions.items():
            for ticker in signal["targets"]:
                if session in closes.get(ticker, pd.Series(dtype=float)).index:
                    base.setdefault(ticker, session)  # bought at that close
        late = [t for t, s in closes.items() if as_of not in s.index and (
            previous in s.index or base.get(t) == previous or (executing and t in executing["targets"]))]
        if late:
            raise RuntimeError(f"no {as_of:%Y-%m-%d} close yet for {', '.join(late)}; retry later")
        split = [t for t, b in base.items() if b < as_of and previous in closes[t].index
                 and _split_like(float(closes[t][as_of] / closes[t][previous]))]
        if split:
            raise RuntimeError(f"split-like move on {as_of:%Y-%m-%d} for {', '.join(split)}; "
                               "the provider may not have back-adjusted yet; retry later")
    for session in traded:
        day = session.strftime("%Y-%m-%d")
        if book["positions"]:
            returns = {t: _return_since(closes.get(t, pd.Series(dtype=float)), session, book["last_priced"][t])
                       for t in book["positions"]}
            book = live.roll_forward(book, returns, session)
        else:
            book = {**book, "as_of": day}
        if qqq_nav is not None:
            qqq_nav *= 1.0 + float(qqq[session])
        signal = executions.get(session)
        if signal is not None:
            prices = {t: float(s[session]) for t, s in closes.items() if session in s.index}
            book, orders = live.execute(book, signal["targets"], prices, day=session)
            live.append_event(LEDGER, protocol_sha, "TRADES_EXECUTED", {
                "signal_date": signal["signal_date"], "execution_date": day, "orders": orders, "book": book,
            })
            trades.append({"signal_date": signal["signal_date"], "execution_date": day,
                           "bought": [o["ticker"] for o in orders if o["side"] == "BUY"],
                           "sold": [o["ticker"] for o in orders if o["side"] == "SELL"]})
            if qqq_nav is None:
                qqq_nav = live.START_CASH
        book, retired = live.retire_stale(book)
        for item in retired:
            live.append_event(LEDGER, protocol_sha, "POSITION_RETIRED", {"as_of": day, **item})
            retired_all.append({"as_of": day, **item})
        if qqq_nav is not None:
            value = live.nav(book)
            live.append_event(LEDGER, protocol_sha, "VALUATION_APPENDED", {
                "as_of": day, "nav": value, "qqq_nav": qqq_nav,
                "points_behind_qqq": live.points_behind(value, qqq_nav), "book": book,
            })
            appended.append(day)
            terminal = live.terminal_record(session, value, qqq_nav)
            if terminal is not None:
                live.append_event(LEDGER, protocol_sha, "TERMINAL_RECORDED", terminal)
                break
    value = live.nav(book)
    final_day = appended[-1] if appended else (last["as_of"] if last else None)
    if terminal is None and settled and final_day is not None and qqq_nav is not None:
        # END_DATE passed: the last valuation on or before it is the final one.
        terminal = live.terminal_record(pd.Timestamp(final_day), value, qqq_nav, final=True)
        live.append_event(LEDGER, protocol_sha, "TERMINAL_RECORDED", terminal)
    if not appended and terminal is None:
        return {"action": "NOTHING_DUE"}
    return {
        "action": "MARKED", "sessions": appended, "nav": value, "qqq_nav": qqq_nav,
        "points_behind_qqq": None if qqq_nav is None else live.points_behind(value, qqq_nav),
        "trades": trades, "retired": retired_all, "terminal": terminal,
        "skipped_calendar_sessions": [s.strftime("%Y-%m-%d") for s in calendar if s not in qqq.index],
        "weights": {t: v / value for t, v in sorted(book["positions"].items())} if value else {},
    }


# ------------------------------------------------------------------ commands


def _push(message: str) -> None:
    subprocess.run(["git", "add", str(ROOT)], check=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        return
    subprocess.run(["git", "commit", "-q", "-m", message], check=True)
    for _ in range(3):
        if subprocess.run(["git", "push", "-q", "origin", f"HEAD:{BRANCH}"]).returncode == 0:
            return
        subprocess.run(["git", "pull", "-q", "--rebase", "origin", BRANCH], check=True)
    raise RuntimeError("could not push the sue-lt-v1 ledger")


def status() -> dict:
    events = live.read_ledger(LEDGER)
    last = _last_valuation(events)
    return {
        "model_version": live.MODEL_VERSION,
        "frozen": PROTOCOL.is_file(),
        "events": len(events),
        "signals": [e["payload"]["signal_date"] for e in _signal_events(events)],
        "latest_valuation": None if last is None else {k: last[k] for k in ("as_of", "nav", "qqq_nav")},
        "terminal": _terminal(events),
    }


def check() -> tuple[dict, int]:
    if not PROTOCOL.is_file():
        return {"action": "NOT_FROZEN"}, 3
    validated_protocol()
    decision = decide(_now(), live.read_ledger(LEDGER))
    return decision, (2 if decision["missed"] else 0)


def run(push: bool, workers: int) -> tuple[dict, int]:
    _require_live_branch()
    protocol, protocol_sha = validated_protocol()
    events = live.read_ledger(LEDGER)
    decision = decide(_now(), events)
    if decision["action"] == "RUN_SIGNAL":
        result = run_signal(pd.Timestamp(decision["as_of"]), protocol_sha, events, workers)
    elif decision["action"] == "RUN_MARK":
        result = run_mark(pd.Timestamp(decision["as_of"]), protocol_sha, events)
    else:
        result = {"action": "NOTHING_DUE"}
    if push and result.get("action") != "NOTHING_DUE":
        _push(f"sue-lt-v1: {result['action'].lower()} {decision['as_of']}")
    return {**decision, "result": result}, (2 if decision["missed"] else 0)


def rehearse(as_of: str, workers: int) -> dict:
    """A full SIGNAL for a past month end into scratch space; nothing is recorded."""
    stamp = pd.Timestamp(as_of)
    if not schedule.is_month_end_session(stamp):
        raise ValueError("rehearse needs a month-end session")
    started = _now()
    result = stage_signal(stamp, Path("research_cache/sue_lt_v1_rehearsal") / as_of, workers, [],
                          rehearsal=True)
    elapsed = (_now() - started).total_seconds()
    return {"rehearsal": True, "elapsed_seconds": round(elapsed), **{k: result[k] for k in (
        "signal_date", "targets", "sue", "universe_size", "price_coverage", "price_download", "price_gate",
        "panel_excluded", "sue_coverage_of_pool", "sec_unmapped", "sec_refresh_failures", "sue_missing",
        "sue_from_older_quarter", "data_release", "runtime_environment")}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("check")
    sub.add_parser("freeze-protocol")
    runner = sub.add_parser("run")
    runner.add_argument("--push", action="store_true")
    runner.add_argument("--workers", type=int, default=1)
    rehearsal = sub.add_parser("rehearse")
    rehearsal.add_argument("--as-of", required=True)
    rehearsal.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    code = 0
    if args.command == "status":
        result = status()
    elif args.command == "check":
        result, code = check()
    elif args.command == "freeze-protocol":
        result = freeze_protocol()
    elif args.command == "run":
        result, code = run(args.push, args.workers)
    else:
        result = rehearse(args.as_of, args.workers)
    print(json.dumps(result, indent=2, default=str))
    sys.exit(code)


if __name__ == "__main__":
    main()
