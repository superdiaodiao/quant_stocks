"""Daily run of the forward observation with its health checks (docs/forward_observation_checklist.md B0.8).

Every US trading day after the close (.github/workflows/forward_observation_daily.yml):

1. fetch the B1 / B2 series (``runner.fetch``) and, when one is due, the S-MISP signal, the held names' charts and
   the EFFR (``runner.smisp_fetch``; idempotent: a frozen signal file is reused, never recomputed);
2. run the frozen runner as of the last completed NYSE session. It rewrites docs/forward_observation_log.md, where
   a month row turns ``final`` only once the data hold a session of a later month: the current month's row is
   provisional and is updated daily, and the first run of a new month is the monthly finalisation (never earlier);
3. fetch a second vendor for the whole forward window and run ``quant.observation.health`` on the result, the stored
   vendor files and the second vendor;
4. write state/forward_observation/health.json (statuses, dates, symbols, counts; no price levels) and the replay
   state state/forward_observation/health_replay.json (daily returns and price ratios; no price levels);
5. exit 3 when any check fails (0 otherwise), so the workflow fails, GitHub emails the owner and the workflow opens
   or updates the issue "Forward observation health".

With B4 (the default of ``main``; ``--no-b4``, or the workflow's repository variable FORWARD_OBS_NO_B4 = true, runs
B1-B3 only and keeps the log's B4 part unchanged): step 1 also fetches QLD / IBIT (``runner.fetch``), BTC
and the T-bill (``b4.fetch``) and, after the S-MISP fetch, any due mega-cap signal and the held mega-cap charts
(``b4.megacap_fetch``); step 2 appends the B4 part of the log; step 3 adds the ``health_b4`` checks and the B4 numbers
of the replay state. B4 never stops B1-B3: a B4 line, its rows, its log part, its checks or its numbers that fail are a
``b4_pipeline`` failure, and the log, health.json and the replay state are still written. A dry run computes a due
mega-cap signal into a cache copy of the state (``b4.scratch_state``), never into the working tree. One Yahoo pace and
stop flag (``prices.YahooPacer``) serves the run's Yahoo requests (B1 / B2 fallback, QLD / IBIT, BTC-USD, both
second-vendor loops; the S-MISP / mega-cap chart downloads keep their own).

The log, health.json and the replay state are written only on GitHub Actions or with ``--write``: the scheduled job
owns them, and a local run that rewrote them would collide with the job's next commit. A local run is a dry run.

Usage (from the repo root):
  PYTHONPATH=. .venv/bin/python scripts/forward_observation_daily.py                 # local check, nothing written
  PYTHONPATH=. .venv/bin/python scripts/forward_observation_daily.py --fetch --fetch-smisp --write
"""
from __future__ import annotations

import os

os.environ.setdefault("REVERSAL_DATA_VERSION", "v2")   # as the runner: B3 reads the frozen v2 inputs

import argparse  # noqa: E402
import csv  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402
from urllib.error import HTTPError  # noqa: E402

import pandas as pd  # noqa: E402

from quant.observation import b4 as b4m  # noqa: E402
from quant.observation import b4_data as bdata  # noqa: E402
from quant.observation import health as hc  # noqa: E402
from quant.observation import health_b4 as hb  # noqa: E402
from quant.observation import prices as fp  # noqa: E402
from quant.observation import runner  # noqa: E402
from quant.observation import smisp as fs  # noqa: E402

HEALTH = fp.STATE_DIR / "health.json"
REPLAY = fp.STATE_DIR / "health_replay.json"
FAIL_EXIT = 3
YAHOO_STOP = (401, 403, 429)
YAHOO_GAP_S = 2.0
CROSS_DAYS = 25                      # calendar days asked from the second vendor (>= the 10 compared sessions)


def clean(msg) -> str:
    """No URL query string (a key could sit there) in anything printed or stored."""
    return re.sub(r"\?\S*", "?...", str(msg))


def _latest_rows(path: Path) -> dict:
    out = {}
    if Path(path).exists():
        with Path(path).open(newline="") as fh:
            for r in csv.DictReader(fh):
                out[r["symbol"]] = r
    return out


def stored_sources(path: Path) -> dict:
    """{symbol: vendor} of the latest fetch per symbol (the runner's fetch_sources.csv)."""
    return {s: r["source"] for s, r in _latest_rows(path).items()}


def stored_history_checks(path: Path) -> dict:
    """{symbol: history check of the latest fetch} (fetch_sources.csv: the fetched tail vs the history state)."""
    out = {}
    for s, r in _latest_rows(path).items():
        try:
            out[s] = json.loads(r.get("history_check") or "{}")
        except ValueError:
            out[s] = {"overlap_ok": False, "overlap_max_abs_close_ratio_diff": "unreadable check"}
    return out


def sources_summary(src: dict, syms) -> str:
    """'tiingo' when one vendor holds every series, else 'tiingo; yahoo: AAPL, MSFT' (the minority by name)."""
    by: dict = {}
    for s in syms:
        by.setdefault(src.get(s) or "unknown", []).append(s)
    main = max(by, key=lambda v: (len(by[v]), v))
    return "; ".join([main] + [f"{v}: {', '.join(by[v])}" for v in sorted(by) if v != main])


def fetch_secondaries(items: dict, target: pd.Timestamp, getters: dict | None = None, sleep=time.sleep,
                      since=None, pacer: fp.YahooPacer | None = None) -> tuple:
    """Second-vendor charts from ``since`` (the forward window) to ``target``. ``items`` {name: (exchange ticker,
    yahoo symbol, stored vendor)}. Alpaca (one call per adjustment for all names) and Yahoo (one request per 2 s,
    stop at 401/403/429) for every name not stored from that vendor. Tiingo is not asked here: from 2026-11 it is the
    stored source of B1 / B2, and its free monthly symbol budget is kept for the runner. Returns ({vendor: {name:
    frame, or None when that vendor answered but has no data for the name (Alpaca: no bars; Yahoo: HTTP 404)}},
    notes); a name the vendor was not asked about or failed on (429, error) is absent. ``pacer``: the run's shared
    Yahoo pace and stop flag (a 429 seen earlier in the run stops this loop before it starts)."""
    getters = getters or {}
    pacer = pacer or fp.YahooPacer(YAHOO_GAP_S, sleep)
    start = str(min(target - pd.Timedelta(days=CROSS_DAYS),
                    pd.Timestamp(since or target) - pd.Timedelta(days=7)).date())   # revised days too
    end = str((target + pd.Timedelta(days=1)).date())     # (prices.alpaca_bars caps it at now - 16 min)
    out, notes = {}, []
    alp = [n for n, (_, _, src) in items.items() if src != "alpaca"]
    headers = getters["alpaca_headers"] if "alpaca_headers" in getters else fp.alpaca_headers()
    if alp and headers:
        try:
            tick = {n: items[n][0] for n in alp}
            got = fp.alpaca_charts(sorted(set(tick.values())), start, end, headers, getters.get("alpaca"), sleep)
            out["alpaca"] = {n: hc.chart_frame(got[t], target) if t in got else None for n, t in tick.items()}
            miss = [n for n, f in out["alpaca"].items() if f is None]
            if miss:
                notes.append(f"alpaca: no bars for {', '.join(miss[:8])}")
        except Exception as exc:  # noqa: BLE001 - a second-vendor problem is reported, it does not stop the run
            notes.append(f"alpaca: not compared ({type(exc).__name__}{_code(exc)})")
    elif alp:
        notes.append("alpaca: no keys, not compared")
    yah = [n for n, (_, _, src) in items.items() if src != "yahoo"]
    if yah and getters.get("yahoo_enabled", True) and pacer.stopped:
        notes.append(f"yahoo: not asked ({pacer.stopped} earlier in this run)")
    elif yah and getters.get("yahoo_enabled", True):
        frames = {}
        for n in yah:
            pacer.wait()                             # (the first request of a run does not wait)
            try:
                frames[n] = hc.chart_frame(json.loads(fp.yahoo_get(items[n][1], start, end, getters.get("yahoo"))),
                                           target)
            except HTTPError as exc:
                notes.append(f"yahoo: HTTP {exc.code} at {n}")
                if exc.code == 404:
                    frames[n] = None
                if exc.code in YAHOO_STOP:
                    pacer.stop(f"HTTP {exc.code} at {n}")
                    break
            except Exception as exc:  # noqa: BLE001
                notes.append(f"yahoo: {type(exc).__name__} at {n}")
        out["yahoo"] = frames
    return out, notes


def _code(exc) -> str:
    c = getattr(exc, "code", None)
    return f" {c}" if c else ""


def held_charts(line: dict | None, held_map: dict, base: Path, target) -> dict:
    """{ticker: frame} of the shorts held after the latest B3 rebalance."""
    if not line or not line.get("started"):
        return {}
    out = {}
    for sid, yahoo in held_map.items():
        p = base / "held" / f"{yahoo}.json.gz"
        out[line["tick"].get(sid, sid)] = hc.chart_frame(p, target) if p.exists() else None
    return out


def held_second_vendor(others: dict, held_src: dict) -> dict:
    """{ticker: (last bar of a second vendor or None, True when every vendor asked has no data for it)}."""
    out = {}
    for tick, stored in held_src.items():
        frames = [f[tick] for v, f in others.items() if v != stored and tick in f]
        lasts = [df.index.max() for df in frames if df is not None]
        out[tick] = (max(lasts) if lasts else None, bool(frames) and not lasts)
    return out


def b3_fingerprints(line: dict | None, base: Path, sigs: dict, target) -> dict:
    """{ticker: {date: ratios}} of every short over the sessions it was held (each rebalance to the next)."""
    if not line or not line.get("started"):
        return {}
    mk = line["mk"]
    held = sorted(line["res"]["held"], key=lambda x: x[0])
    ymap = {}
    for sc in sigs.values():
        ymap.update(dict(zip(sc["security_id"], sc["yahoo"] if "yahoo" in sc else sc["ticker"])))
    days: dict = {}
    for j, (t, names) in enumerate(held):
        t1 = held[j + 1][0] if j + 1 < len(held) else len(mk.sessions) - 1
        for n in names:
            days.setdefault(n, set()).update(mk.sessions[t: t1 + 1])
    out = {}
    for sid, ds in days.items():
        p = base / "held" / f"{ymap.get(sid, sid)}.json.gz"
        df = hc.chart_frame(p, target) if p.exists() else None
        if df is not None:
            out[line["tick"].get(sid, sid)] = hc.chart_fingerprints(df, df.index[0], target, sorted(ds))
    return out


def load_signals(state_dir: Path, names) -> dict:
    out = {}
    for name in names:
        p = Path(state_dir) / name
        if p.exists():
            out[name] = pd.read_csv(p, dtype={"security_id": str, "ticker": str, "yahoo": str})
    return out


def run_daily(as_of=None, now: datetime | None = None, fetch: bool = False, fetch_smisp: bool = False,
              dry_run: bool = False, cross_check: bool = True, accept_replay: bool = False,
              accept_data: bool = False, early: bool = False, test_alert: bool = False, host: str | None = None,
              raw_dir=None, log_path=None, stocks=None, smisp_dir=None, smisp_state=None, borrow_dir=None,
              health_path: Path = HEALTH, replay_path: Path = REPLAY, calendar=None, getters: dict | None = None,
              sources: dict | None = None, universe_check: bool = True, save_details: bool = True,
              sleep=time.sleep, b4: bool = False, b4_dir=None, b4_state=None) -> dict:
    """One daily run; returns the health report (also written to ``health_path`` unless ``dry_run``).

    ``early``: a scheduled run that is not the evening's last (a bar missing only for the session just closed is a
    warning, whatever the clock; no second vendor is a warning). ``accept_data``: record the current data differences
    (second-vendor disagreements, day moves and total-return gaps, by symbol and date) as reviewed: reported, no
    longer failed. ``test_alert``: add a failing check to test the notification path. ``host``: "actions" on GitHub
    Actions (pipeline_liveness counts only the job's own runs). ``b4``: also fetch, run and check the B4 lines
    (checklist section B4; ``b4_dir`` the raw BTC / T-bill / mega-cap chart cache, ``b4_state`` the frozen mega-cap
    signals; default locations when None)."""
    raw_dir = Path(raw_dir or runner.RAW)
    log_path = Path(log_path or runner.LOG)
    stocks = tuple(stocks or runner.STOCKS)
    base = Path(smisp_dir or runner.SMISP_DIR)
    state_dir = Path(smisp_state) if smisp_state else (base / "state" if smisp_dir else runner.SMISP_STATE)
    borrow_dir = Path(borrow_dir or fs.BORROW_DIR)
    now = now or datetime.now(timezone.utc)
    cal = calendar if calendar is not None else hc.default_calendar(now)
    latest = hc.last_completed_session(now, cal)
    target = latest if as_of is None else min(hc.session_on_or_before(as_of, cal), latest)
    t = str(target.date())
    prev_health, prev_replay = hc.load_json(health_path), hc.load_json(replay_path)
    notes, b4_notes = [], []
    pacer = fp.YahooPacer(YAHOO_GAP_S, sleep)       # one Yahoo pace and stop flag for the whole run
    if b4 and dry_run and fetch_smisp:               # a due mega-cap signal of a dry run goes to a scratch copy
        b4_state = b4m.scratch_state(b4_state, b4_dir)
    b4cfg = {"b4_dir": b4_dir, "state_dir": b4_state} if b4 else None
    b4p = b4m.paths(b4_dir, b4_state, smisp_dir) if b4 else None

    # 1. fetch (a vendor problem is reported, the run goes on with the stored files)
    if fetch:
        try:
            want = runner.b_source()
            got = runner.fetch(("QQQ", "ONEQ", *stocks), raw_dir=raw_dir, pacer=pacer)
            notes += [f"fetch {s}: {clean(v)}" for s, v in got.items() if not str(v).startswith("ok")]
            fell = [s for s, v in got.items() if want == "tiingo" and str(v) == "ok (yahoo)"]
            if fell:
                notes.append(f"fetch: Tiingo refused or failed for {', '.join(fell)}; stored from Yahoo instead "
                             "(Tiingo quota used up?)")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"fetch stopped: {type(exc).__name__}")
        if b4:
            try:
                want = runner.b_source()
                got = runner.fetch(bdata.ETFS, raw_dir=raw_dir, pacer=pacer)
                b4_notes += [f"fetch {s}: {clean(v)}" for s, v in got.items() if not str(v).startswith("ok")]
                fell = [s for s, v in got.items() if want == "tiingo" and str(v) == "ok (yahoo)"]
                if fell:
                    b4_notes.append(f"fetch: Tiingo refused or failed for {', '.join(fell)}; stored from Yahoo instead")
            except Exception as exc:  # noqa: BLE001
                b4_notes.append(f"B4 ETF fetch stopped: {type(exc).__name__}")
            try:
                got = b4m.fetch(b4p["b4_dir"], sleep=sleep, pacer=pacer)
                b4_notes += [f"BTC {k}: {clean(v)}" for k, v in got["btc"].items() if not str(v).startswith("ok")]
                if got["dtb3"] != "ok":
                    b4_notes.append(f"T-bill (DTB3): {clean(got['dtb3'])}")
            except Exception as exc:  # noqa: BLE001
                b4_notes.append(f"BTC / T-bill fetch stopped: {type(exc).__name__}")
    if fetch_smisp:
        try:
            runner.smisp_fetch(t, raw_dir, smisp_dir)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"S-MISP fetch stopped: {type(exc).__name__}: {clean(exc)}")
        if b4:
            try:
                ses = runner.load_market("QQQ", t, raw_dir).s.sessions
                got = b4m.megacap_fetch(ses, b4p["b4_dir"], b4p["state_dir"], b4p["smisp_dir"], sleep=sleep)
                b4_notes += [clean(x) for x in got.get("notes", [])]
            except Exception as exc:  # noqa: BLE001
                b4_notes.append(f"mega-cap fetch stopped: {type(exc).__name__}: {clean(exc)}")

    # 2. the frozen runner as of the last completed session
    res, run_error = None, None
    try:
        kw = {"stocks": stocks, "smisp_dir": smisp_dir, "borrow_dir": borrow_dir, "smisp_state": smisp_state}
        if dry_run:
            res = runner.compute_all(t, raw_dir, **kw, b4=b4cfg)
            old = log_path.read_text() if log_path.exists() else ""
            res["text"], res["warnings"] = runner.render_all(res, old)
        else:
            res = runner.run(t, raw_dir=raw_dir, log_path=log_path, **kw, b4=b4cfg)
            if save_details and any(v.get("started") for v in res["lines"].values()):
                runner.save_run_details(res)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - reported as a failed check, health.json still written
        run_error = f"{type(exc).__name__}: {clean(exc)}"

    # 3. data and the second vendor
    syms = ("QQQ", "ONEQ", *stocks)
    frames = {s: hc.chart_frame(runner.raw_path(s, raw_dir), t) if runner.raw_path(s, raw_dir).exists() else None
              for s in syms}
    lines = res["lines"] if res else {}
    b3_line = lines.get("B3")
    data_end = res["data_end"] if res else None
    known = (prev_replay or {}).get("signal_hashes") or {}
    accepted = sorted((prev_replay or {}).get("data_accepted") or {})
    grace = early or hc.in_grace(now, target)
    _, _, held_map = hc.check_b3(state_dir, target, cal, b3_line, known, grace, data_end)
    held = held_charts(b3_line, held_map, base, t)
    first_trade = b3_line.get("first_trade") if b3_line and b3_line.get("started") else None
    fetch_log = runner.FETCH_LOG.with_name("fetch_sources.csv")
    src = dict(sources) if sources is not None else stored_sources(fetch_log)
    fp_from = pd.Timestamp(hc.START_CLOSE) - pd.Timedelta(days=hc.FINGERPRINT_FROM_DAYS)
    others, vnotes, held_src = {}, [], {}
    if cross_check:
        items = {s: (s, s, src.get(s)) for s in syms}
        hsrc = stored_sources(base / "held" / "incoming" / "chart_sources.csv")
        hitems = {}
        for sid, y in held_map.items():
            tk = b3_line["tick"].get(sid, sid)
            hitems[tk] = (tk, y, hsrc.get(y, "alpaca"))
            held_src[tk] = hitems[tk][2]
        others, vnotes = fetch_secondaries({**items, **hitems}, target, getters, sleep, fp_from, pacer)
    # a session no stored series and no second vendor has (while later ones exist): an unscheduled closure that the
    # pinned calendar does not know; left out of the calendar of every check (named in data_integrity)
    istart = pd.Timestamp(hc.START_CLOSE) - pd.Timedelta(days=hc.INTEGRITY_FROM_DAYS)
    closures = hc.unscheduled_closures(frames, others, cal, istart, target)
    cal = cal.difference(pd.DatetimeIndex(closures)) if closures else cal
    b3_check, hashes, held_map = hc.check_b3(state_dir, target, cal, b3_line, known, grace, data_end)
    effr_p = base / "EFFR_nyfed.csv"
    effr_last = None
    if first_trade is not None and effr_p.exists():
        e = pd.to_datetime(pd.read_csv(effr_p).iloc[:, 0], format="%m/%d/%Y", errors="coerce").dropna()
        effr_last = e.max() if len(e) else None

    # 4. checks
    checks = []
    pipe_items = notes + ([f"runner: {run_error}"] if run_error else [])
    checks.append(hc.Check("pipeline", hc.FAIL if run_error else hc.WARN if notes else hc.OK,
                           "runner did not complete" if run_error else
                           ("dry run: nothing written" if dry_run else "fetch, runner and log write completed")
                           + (f" ({len(notes)} fetch note(s))" if notes and not run_error else ""), pipe_items))
    checks.append(hc.check_freshness(frames, target, cal, data_end, held, effr_last, grace,
                                     held_second_vendor(others, held_src)))
    checks.append(hc.check_integrity(frames, target, cal, held=held, held_start=first_trade,
                                     history=calendar is None,   # (synthetic test calendars: window only)
                                     overlap=stored_history_checks(fetch_log) if sources is None else None,
                                     closures=closures, accepted=accepted))
    # the second vendor over the whole forward window on every run: a restated old day fails until the vendors
    # agree again (or the owner accepts it with --accept-data)
    window = {s: list(cal[(cal >= fp_from) & (cal <= target)]) for s in syms}
    vend = None
    if cross_check:
        vend = hc.check_vendors({**frames, **held}, others, target, cal, {**src, **held_src}, vnotes,
                                extra_days=window, confirm=hc.large_moves(frames, target, cal, held),
                                tols={tk: hc.VENDOR_TOL_B3 for tk in held}, strict=not early, accepted=accepted,
                                must=syms)
        checks.append(vend)
    else:
        checks.append(hc.Check("vendor_agreement", hc.OK, "skipped (--no-cross-check)"))
    nxt = []
    if res:
        sig_check, nxt = hc.check_signals(lines, frames, t, stocks, universe_check)
        checks.append(sig_check)
        ends = {"B3": b3_line["last_session"]} if b3_line and b3_line.get("started") else None
        text = res["text"]
        if b4 and res.get("b4") is not None and not res["b4"].get("render_error"):
            ends = {**(ends or {}), **b4m.line_ends(res["b4"])}
        else:                                        # a B4 part kept from an earlier run is not this run's
            text = b4m.split_log(text)[0]
        checks.append(hc.check_log_status(text, data_end, ends))
    else:
        checks.append(hc.Check("signal_consistency", hc.FAIL, "not run: the runner did not complete"))
        checks.append(hc.Check("log_status", hc.FAIL, "not run: the runner did not complete"))
    checks.append(b3_check)
    checks.append(hc.check_b3_borrow(borrow_dir, target, b3_line))
    b4res, b4_inputs, b4_hashes = (res or {}).get("b4"), {}, {}
    b4_next = []
    if b4:
        try:
            b4_checks, b4_inputs, b4_hashes, b4_next = b4_health(
                b4res, b4p, raw_dir, frames, t, cal, now, grace, early, cross_check, src, known, accepted, getters,
                sleep, fp_from, istart, data_end, b4_notes, pacer)
        except Exception as exc:  # noqa: BLE001 - the B1-B3 checks, health.json and the replay state still follow
            b4_checks = [hc.Check("b4_pipeline", hc.FAIL, "the B4 checks did not complete",
                                  [f"health_b4: {type(exc).__name__}: {clean(exc)}"] + list(b4_notes))]
        checks += b4_checks
    new_state = None
    if res:
        inputs = {"px": {s: hc.chart_fingerprints(df, fp_from, t) for s, df in frames.items() if df is not None},
                  "b3": b3_fingerprints(b3_line, base, load_signals(state_dir, hashes), t),
                  "effr": hc.effr_fingerprints(effr_p, pd.Timestamp(hc.B3_FIRST_SIGNAL) - pd.Timedelta(days=40), t)
                  if first_trade is not None else {}, **b4_inputs}
        numbers = hc.replay_numbers(lines)
        if b4res is not None:
            try:
                numbers["series"].update(b4m.series_of(b4res))
            except Exception as exc:  # noqa: BLE001 - reported; the B1-B3 numbers are recorded
                checks.append(hc.Check("b4_pipeline", hc.FAIL, "the B4 numbers were not recorded",
                                       [f"series_of: {type(exc).__name__}: {clean(exc)}"]))
        rep_check, new_state = hc.check_replay(prev_replay, numbers, inputs, data_end, res.get("warnings", ()),
                                               accept_replay)
        checks.append(rep_check)
        if new_state is not None:
            new_state["signal_hashes"] = dict(sorted({**known, **hashes, **b4_hashes}.items()))
            acc = set(accepted)
            if accept_data:
                acc |= {k for c in checks for k in c.data.get("keys", ())}
            if acc:
                new_state["data_accepted"] = {k: "accepted with --accept-data" for k in sorted(acc)}
    else:
        checks.append(hc.Check("replay_drift", hc.FAIL, "not run: the runner did not complete"))
    checks.append(hc.check_liveness(prev_health, target, cal, host))
    if test_alert:
        checks.append(hc.Check("test_alert", hc.FAIL, "forced failure to test the notification path (workflow input "
                                                      "test_alert); the next normal run closes the issue"))

    # 5. report and state
    last_run = target if res else (pd.Timestamp(prev_health["last_run_session"])
                                   if prev_health and prev_health.get("last_run_session") else None)
    if b4:
        nxt = nxt + list(b4_next)
    rep = hc.report(target, checks, data_end, last_run, nxt,
                    {"stored": sources_summary(src, syms), "cross_check": "on" if cross_check else "off"}, host)
    if not dry_run:
        Path(health_path).parent.mkdir(parents=True, exist_ok=True)
        Path(health_path).write_text(hc.dumps(rep))
        if new_state is not None:
            Path(replay_path).write_text(hc.dumps(new_state))
    return rep


def b4_health(b4res, b4p, raw_dir, frames, t, cal, now, grace, early, cross_check, src, known, accepted, getters,
              sleep, fp_from, istart, data_end, b4_notes, pacer=None) -> tuple[list, dict, dict, list]:
    """The B4 checks (quant.observation.health_b4), the B4 replay inputs, the mega-cap signal hashes and the
    next-session notes."""
    target = pd.Timestamp(t)
    sig_check, hashes, mframes = hb.check_signals(b4p["state_dir"], target, cal, known, grace)
    etf = {s: hc.chart_frame(runner.raw_path(s, raw_dir), t) if runner.raw_path(s, raw_dir).exists() else None
           for s in bdata.ETFS}
    names = hb.held_names(b4res)
    held = {tk: hc.chart_frame(b4p["held_dir"] / f"{y}.json.gz", t) if (b4p["held_dir"] / f"{y}.json.gz").exists()
            else None for tk, y in names.items()}
    hsrc = stored_sources(b4p["held_dir"] / "incoming" / "chart_sources.csv")
    sources = {**{s: src.get(s) for s in bdata.ETFS}, **{tk: hsrc.get(y, "alpaca") for tk, y in names.items()}}
    btc = (b4res or {}).get("btc")
    rf_p = b4p["b4_dir"] / "DTB3.csv"
    rf_last = None
    if rf_p.exists():
        d = pd.read_csv(rf_p)
        d = d[pd.to_numeric(d.iloc[:, 1], errors="coerce").notna()]
        rf_last = pd.to_datetime(d.iloc[:, 0]).max() if len(d) else None
    lines = (b4res or {}).get("lines", {})
    waiting = [k for k, v in lines.items() if v.get("pending") is not None or v.get("waiting_for")]
    others, vnotes = {}, []
    if cross_check:                                  # (first: the held names' freshness uses the second vendor)
        items = {s: (s, s, sources.get(s)) for s in bdata.ETFS}
        items.update({tk: (tk, y, sources[tk]) for tk, y in names.items()})
        others, vnotes = fetch_secondaries(items, target, getters, sleep, fp_from, pacer)
    out = [hb.check_pipeline(b4res, b4_notes),
           hb.check_freshness(etf, target, cal, btc.close.index[-1] if btc is not None else None,
                              rf_last, now, grace, b4m.line_ends(b4res) if b4res else None, data_end, waiting,
                              held, held_second_vendor(others, {tk: sources[tk] for tk in names}),
                              btc_note=(b4res or {}).get("btc_error")),
           hb.check_integrity(etf, target, cal, btc, sources, istart, accepted, held, frames.get("QQQ"))]
    if cross_check:
        out.append(hb.check_vendors({**etf, **held}, others, target, cal, sources, vnotes, btc, strict=not early,
                                    accepted=accepted, start=fp_from))
    else:
        out.append(hc.Check("b4_vendor_agreement", hc.OK, "skipped (--no-cross-check)"))
    rules, nxt = hb.check_rules(b4res, frames.get("QQQ"), mframes)
    out += [rules, sig_check]
    legs = (b4res or {}).get("legs")
    inputs = {"b4px": {s: hc.chart_fingerprints(df, fp_from, t) for s, df in etf.items() if df is not None},
              "b4m": hb.megacap_fingerprints(b4res, b4p["held_dir"], fp_from, t),
              "b4btc": hb.btc_fingerprints(btc, hb.BTC_FP_FROM, t),
              "b4rf": hb.rf_fingerprints(legs.rf if legs is not None else None, fp_from, t)}
    return out, inputs, hashes, nxt


def week_last(session) -> bool:
    """``session`` is the last NYSE session of its calendar week (the weekly warning digest is posted then)."""
    s = pd.Timestamp(session)
    nxt = hc.next_session(s, hc.nyse_sessions(s, s + pd.Timedelta(days=14)))
    return nxt is None or nxt.isocalendar()[:2] != s.isocalendar()[:2]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--as-of", help="session to check (YYYY-MM-DD); default: the last completed NYSE session")
    ap.add_argument("--fetch", action="store_true", help="refresh the B1 / B2 series first (runner --fetch)")
    ap.add_argument("--fetch-smisp", action="store_true",
                    help="compute a due S-MISP signal and refresh the held charts and the EFFR (runner --fetch-smisp)")
    ap.add_argument("--write", action="store_true",
                    help="write the log, health.json and the replay state (default only on GitHub Actions: the "
                         "scheduled job owns these files; a local run is a dry run)")
    ap.add_argument("--dry-run", action="store_true", help="write nothing (log, health.json, replay state)")
    ap.add_argument("--early", action="store_true",
                    help="a scheduled run that is not the evening's last: a bar missing only for the session just "
                         "closed, and a missing second vendor, are warnings (the last run fails on them)")
    ap.add_argument("--no-cross-check", action="store_true", help="skip the second-vendor comparison")
    ap.add_argument("--no-b4", action="store_true",
                    help="do not run the B4 lines (M1 M2 M3 M6, LEV-B3, V3, BT4b) or their checks; the log's B4 part "
                         "is kept unchanged")
    ap.add_argument("--accept-replay", action="store_true",
                    help="accept changed recorded numbers (after a reviewed code or data change): rewrite the replay "
                         "state from this run")
    ap.add_argument("--accept-data", action="store_true",
                    help="accept the current data differences after review (second-vendor "
                         "disagreements, day moves, total-return gaps, e.g. a corporate action): recorded in the "
                         "replay state and reported, no longer failed")
    ap.add_argument("--test-alert", action="store_true",
                    help="add a failing check (nothing else changes) to test the email and the issue")
    ap.add_argument("--report-out", help="also write the report JSON here (for the workflow's issue step)")
    a = ap.parse_args(argv)
    on_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    dry = a.dry_run or not (a.write or on_actions)
    if dry and not a.dry_run:
        print("local run: nothing is written (the scheduled job owns the log and the state; --write to override)")
    rep = run_daily(as_of=a.as_of, fetch=a.fetch, fetch_smisp=a.fetch_smisp, dry_run=dry,
                    cross_check=not a.no_cross_check, accept_replay=a.accept_replay, accept_data=a.accept_data,
                    early=a.early, test_alert=a.test_alert, host="actions" if on_actions else "local",
                    b4=not a.no_b4)
    print("\n".join(hc.summary_lines(rep)))
    if a.report_out:
        out = dict(rep, run={"early": a.early, "week_last": week_last(rep["session"]), "dry_run": dry})
        Path(a.report_out).write_text(hc.dumps(out))
    return FAIL_EXIT if rep["status"] == hc.FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
