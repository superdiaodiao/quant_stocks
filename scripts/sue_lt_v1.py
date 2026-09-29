#!/usr/bin/env python3
"""Runner for the sue-lt-v1 forward observation (docs/sue_lt_v1_protocol.md).

    PYTHONPATH=. python scripts/sue_lt_v1.py status
    PYTHONPATH=. python scripts/sue_lt_v1.py check          # what is due now
    PYTHONPATH=. python scripts/sue_lt_v1.py run [--push]   # do what is due
    PYTHONPATH=. python scripts/sue_lt_v1.py rehearse --as-of 2026-09-30
    PYTHONPATH=. python scripts/sue_lt_v1.py freeze-protocol   # once, on the live branch

Writes happen only on the ``live/sue-lt-v1`` branch, whose code is pinned
by the frozen protocol's import closure. ``rehearse`` runs a SIGNAL for any
past month end into a scratch directory and records nothing.

Exit codes: 0 done or nothing due; 1 error; 2 a SIGNAL window was missed
(no backfill: the book stays as it is until the next month end); 3 the
protocol is not frozen.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pandas as pd

from src.financial.quarterly_fundamentals import load_quarterly_fundamentals
from src.io import fundamentals_update
from scripts.research_v6_market_refresh import seed_cache
from src.io.nasdaq_update import fetch_history, refresh_universe, update_all
from src.io.security_universe import investable_common_equities
from src.research import prospective_schedule as schedule
from src.research import sue_live as live
from src.research.code_closure import closure_digest, project_import_closure
from src.research.panel_data import load_panel
from scripts.research_v5_trend_core_satellite import refresh_core_price

BRANCH = "live/sue-lt-v1"
ROOT = Path("output/research_only/sue_lt_v1")
LEDGER = ROOT / "ledger.jsonl"
PROTOCOL = ROOT / "frozen_protocol.json"
SIGNALS = ROOT / "signals"
WORK = Path("research_cache/sue_lt_v1")
PROTOCOL_DOC = Path("docs/sue_lt_v1_protocol.md")
CLOSURE_ROOTS = ("scripts/sue_lt_v1.py",)
FORBIDDEN_ETFS = {"QQQ", "TQQQ", "SQQQ", "QQQM", "ONEQ"}
MINIMUM_SUE_COVERAGE = 0.6
MINIMUM_PRICE_COVERAGE = 0.98


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
    return protocol, _sha256(PROTOCOL)


def freeze_protocol() -> dict:
    _require_live_branch()
    if PROTOCOL.exists() or LEDGER.exists():
        raise RuntimeError("sue-lt-v1 is already frozen")
    now = _now()
    first_opens, _ = schedule.signal_window(live.FIRST_SIGNAL_DATE)
    if now >= first_opens:
        raise RuntimeError("the first SIGNAL window has opened; freeze a later version instead")
    protocol = {
        "model_version": live.MODEL_VERSION,
        "frozen_at": now.isoformat(timespec="seconds"),
        "protocol_document": str(PROTOCOL_DOC),
        "protocol_document_sha256": _sha256(PROTOCOL_DOC),
        "first_signal_date": live.FIRST_SIGNAL_DATE.strftime("%Y-%m-%d"),
        "parameters": {
            "pool": live.POOL, "holdings": live.HOLDINGS, "minimum_price": live.MINIMUM_PRICE,
            "minimum_history_sessions": live.MINIMUM_HISTORY, "liquidity_sessions": live.LIQUIDITY_SESSIONS,
            "stale_sessions": live.STALE_SESSIONS, "start_cash": live.START_CASH, "cost_model": live.COST_MODEL,
        },
        "evaluation": {"months": 24, "benchmark": "QQQ total return, buy and hold",
                       "early_stop_points_behind_qqq": 25},
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


def decide(now: datetime, events: list[dict]) -> dict:
    frozen = {e["payload"]["signal_date"] for e in _signal_events(events)}
    completed = schedule.latest_completed_session(now)
    missed = []
    for month_end in schedule.month_end_sessions(live.FIRST_SIGNAL_DATE, pd.Timestamp(now.date()) + pd.Timedelta(days=7)):
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
            return {"action": "RUN_MARK", "as_of": completed.strftime("%Y-%m-%d"), "missed": missed}
        if last is not None and pd.Timestamp(last["as_of"]) < completed:
            return {"action": "RUN_MARK", "as_of": completed.strftime("%Y-%m-%d"), "missed": missed}
    return {"action": "NOTHING_DUE", "as_of": None, "missed": missed}


# ------------------------------------------------------------------ SIGNAL


def stage_signal(as_of: pd.Timestamp, work: Path, workers: int, holdings: list[str]) -> dict:
    """Refresh universe, prices and SEC quarters for one month end; select targets."""
    work.mkdir(parents=True, exist_ok=True)
    universe_path = work / "universe.csv"
    refresh_universe(as_of.date(), min_market_cap=0, target_path=universe_path, common_equities_only=True)
    current = investable_common_equities(pd.read_csv(universe_path, keep_default_na=False))
    symbols = sorted(set(current["Symbol"].dropna().astype(str).str.upper()) - FORBIDDEN_ETFS)
    price_dir, index_path = work / "prices", work / "nasdaq_index.csv"
    seed_cache(symbols, price_dir=price_dir, index_path=index_path)
    update_all(end=as_of.date(), workers=workers, tickers=symbols, price_dir=price_dir, index_path=index_path)
    start = (as_of - pd.Timedelta(days=420)).strftime("%Y-%m-%d")
    close, dollar_volume = load_panel(price_dir, start, as_of.strftime("%Y-%m-%d"))
    # The panel loader drops known non-common securities, as in the backtest;
    # coverage is measured over the names that can enter the pool.
    close = close.reindex(columns=[c for c in close.columns if c in set(symbols)])
    priced = close.loc[as_of].notna().sum() if as_of in close.index else 0
    price_coverage = priced / max(close.shape[1], 1)
    if price_coverage < MINIMUM_PRICE_COVERAGE:
        raise RuntimeError(f"only {price_coverage:.1%} of the universe has a {as_of:%Y-%m-%d} close; retry later")
    pool = live.liquidity_pool(close, dollar_volume, set(symbols), as_of)
    ticker_map = {str(t).upper(): int(c) for t, c in fundamentals_update.fetch_sec_ticker_map().items()}
    requested = sorted(set(pool) | set(holdings))
    mapped = [t for t in requested if t in ticker_map]
    fundamentals_update.update_fundamentals(
        as_of=as_of.date(), workers=4, refresh_after_days=0,
        output=work / "fundamentals.csv", quarterly_output=work / "quarterly.csv",
        force=True, tickers=mapped, cache_dir=work / "companyfacts_cache",
    )
    quarterly = load_quarterly_fundamentals(work / "quarterly.csv")
    if pd.to_datetime(quarterly["available_date"]).gt(as_of).any():
        quarterly = quarterly.loc[pd.to_datetime(quarterly["available_date"]).le(as_of)]
    result = live.select_targets(close, dollar_volume, set(symbols), quarterly, as_of, issuer=ticker_map)
    coverage = len(result["sue"]) / max(len(result["pool"]), 1)
    result.update({
        "universe_size": len(symbols),
        "price_coverage": price_coverage,
        "sec_unmapped": sorted(set(requested) - set(mapped)),
        "sue_coverage_of_pool": coverage,
        "inputs": {"universe.csv": _sha256(universe_path), "quarterly.csv": _sha256(work / "quarterly.csv")},
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
    result = stage_signal(as_of, WORK / as_of.strftime("%Y-%m-%d"), workers, holdings)
    if _now() >= closes:
        raise RuntimeError("the SIGNAL window closed during staging; the month is missed")
    SIGNALS.mkdir(parents=True, exist_ok=True)
    path = SIGNALS / f"signal_{as_of:%Y-%m-%d}.json"
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    live.append_event(LEDGER, protocol_sha, "SIGNAL_FROZEN", {
        "signal_date": result["signal_date"], "targets": result["targets"],
        "signal_file": path.name, "signal_sha256": _sha256(path),
    })
    return {"action": "SIGNAL_FROZEN", "signal_date": result["signal_date"], "targets": result["targets"]}


# ------------------------------------------------------------------ MARK


def _closes(tickers: list[str], start: pd.Timestamp, end: pd.Timestamp) -> dict[str, pd.Series]:
    out = {}
    for ticker in tickers:
        frame = fetch_history(ticker, start.date(), end.date())
        out[ticker] = frame.set_index("date")["close"].astype(float) if len(frame) else pd.Series(dtype=float)
    return out


def _qqq_total_returns(end: pd.Timestamp) -> pd.Series:
    path = refresh_core_price(WORK / "qqq.csv")
    frame = pd.read_csv(path, parse_dates=["date"]).set_index("date").sort_index().loc[:end]
    return (frame["close"] + frame["cash_dividend"]) / frame["close"].shift() - 1.0


def run_mark(as_of: pd.Timestamp, protocol_sha: str, events: list[dict]) -> dict:
    last = _last_valuation(events)
    book = last["book"] if last else live.new_book()
    qqq_nav = last["qqq_nav"] if last else None
    pending = [e["payload"] for e in _signal_events(events) if e["payload"]["signal_date"] not in _executed(events)]
    signal = pending[0] if pending else None
    first = pd.Timestamp(last["as_of"]) if last else pd.Timestamp(signal["signal_date"])
    sessions = [s for s in schedule.sessions_between(first, as_of) if s > first]
    if not sessions:
        return {"action": "NOTHING_DUE"}
    tickers = sorted(set(book["positions"]) | set(signal["targets"] if signal else []))
    closes = _closes(tickers, first - pd.Timedelta(days=10), as_of)
    qqq = _qqq_total_returns(as_of)
    execution = schedule.next_session(pd.Timestamp(signal["signal_date"])) if signal else None
    appended = []
    previous = first
    for session in sessions:
        if book["positions"]:
            returns = {}
            for ticker in book["positions"]:
                series = closes.get(ticker, pd.Series(dtype=float))
                if session in series.index and previous in series.index:
                    returns[ticker] = float(series[session] / series[previous] - 1.0)
                elif session in series.index:
                    earlier = series.loc[:previous]
                    returns[ticker] = float(series[session] / earlier.iloc[-1] - 1.0) if len(earlier) else None
                else:
                    returns[ticker] = None
            book = live.roll_forward(book, returns, session)
        else:
            book = {**book, "as_of": session.strftime("%Y-%m-%d")}
        if qqq_nav is not None:
            qqq_nav *= 1.0 + float(qqq.get(session, 0.0))
        if execution is not None and session == execution:
            prices = {t: float(s[session]) for t, s in closes.items() if session in s.index}
            book, orders = live.execute(book, signal["targets"], prices)
            live.append_event(LEDGER, protocol_sha, "TRADES_EXECUTED", {
                "signal_date": signal["signal_date"], "execution_date": session.strftime("%Y-%m-%d"),
                "orders": orders, "book": book,
            })
            if qqq_nav is None:
                qqq_nav = live.START_CASH
        book, retired = live.retire_stale(book)
        for item in retired:
            live.append_event(LEDGER, protocol_sha, "POSITION_RETIRED",
                              {"as_of": session.strftime("%Y-%m-%d"), **item})
        if qqq_nav is not None:
            live.append_event(LEDGER, protocol_sha, "VALUATION_APPENDED", {
                "as_of": session.strftime("%Y-%m-%d"), "nav": live.nav(book), "qqq_nav": qqq_nav, "book": book,
            })
            appended.append(session.strftime("%Y-%m-%d"))
        previous = session
    return {"action": "MARKED", "sessions": appended, "nav": live.nav(book), "qqq_nav": qqq_nav}


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
    result = stage_signal(stamp, Path("research_cache/sue_lt_v1_rehearsal") / as_of, workers, [])
    elapsed = (_now() - started).total_seconds()
    return {"rehearsal": True, "elapsed_seconds": round(elapsed), **{k: result[k] for k in (
        "signal_date", "targets", "universe_size", "price_coverage", "sue_coverage_of_pool", "sec_unmapped")}}


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
