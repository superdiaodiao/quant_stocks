"""Synthetic tests of the daily health checks (quant/observation/health.py, daily.py, health_issue.py).

Each check passes on clean data and fails on the fault it targets; calendar edge cases (weekends, holidays, half
days, both DST regimes of the workflow's cron); idempotency of the daily run. No network, no vendor data."""
from __future__ import annotations

import copy
import gzip
import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quant.observation import daily
from quant.observation import health as hc
from quant.observation import health_issue as hi
from quant.observation import runner as fo
from quant.strategies import selective_t as st
from quant.strategies import t_grid as tg

sys.path.insert(0, str(Path(__file__).parent))
import test_forward_observation as tfo  # noqa: E402  (the synthetic B3 environment of the runner tests)

ROOT = Path(__file__).resolve().parents[1]
CAL = hc.nyse_sessions("2024-01-02", "2027-03-31")          # real NYSE sessions for the B1 / B2 data
STOCKS = ("AAPL", "MSFT")
SHOCKS = tfo.SHOCKS


def utc(s: str) -> datetime:
    return pd.Timestamp(s, tz="UTC").to_pydatetime()


# ======================================================================== synthetic charts on NYSE sessions


def payload(sym: str, seed: int, vol: float, sessions=CAL, split_on: str | None = None,
            adjusted_split: bool = True) -> dict:
    """Yahoo v8 body: split-adjusted OHLC (unless ``adjusted_split`` is False), adjclose, quarterly dividend, an
    optional 2:1 split event; the forced shocks of the runner tests so that both rules trade."""
    rng = np.random.default_rng(seed)
    r = rng.standard_t(4, len(sessions)) * vol / math.sqrt(2) + 0.0004
    for day, x in SHOCKS.get("QQQ" if sym == "QQQ" else "stock" if sym != "ONEQ" else "", {}).items():
        k = sessions.searchsorted(pd.Timestamp(day))
        if k < len(sessions) and sessions[k] == pd.Timestamp(day):
            r[k] = x
    c = 100 * np.exp(np.cumsum(r))
    o = c * np.exp(rng.normal(0, vol / 3, len(c)))
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, vol / 2, len(c))))
    lo = np.minimum(o, c) * (1 - np.abs(rng.normal(0, vol / 2, len(c))))
    ts = [int(pd.Timestamp(d.date(), tz="America/New_York").replace(hour=9, minute=30).timestamp()) for d in sessions]
    div_days = list(range(30, len(c), 63))
    adj = c.copy()
    for k in div_days:
        adj[:k] *= 1 - 0.3 / c[k - 1]
    ev = {"dividends": {str(ts[k]): {"date": ts[k], "amount": 0.3} for k in div_days}}
    if split_on:
        k = int(sessions.searchsorted(pd.Timestamp(split_on)))
        ev["splits"] = {str(ts[k]): {"date": ts[k], "numerator": 2, "denominator": 1}}
        if not adjusted_split:          # the vendor forgot to adjust the history: the close halves on the split day
            for arr in (o, h, lo, c):
                arr[k:] /= 2
    q = {"open": list(o), "high": list(h), "low": list(lo), "close": list(c), "volume": [1e6] * len(c)}
    return {"chart": {"result": [{"timestamp": ts, "meta": {"dataGranularity": "1d", "symbol": sym},
                                  "indicators": {"quote": [q], "adjclose": [{"adjclose": list(adj)}]},
                                  "events": ev}]}}


SPEC = {"QQQ": (1, 0.015), "ONEQ": (2, 0.014), "AAPL": (3, 0.035), "MSFT": (4, 0.03)}


def write_raw(d: Path, sessions=CAL, edit=None) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    for sym, (seed, vol) in SPEC.items():
        p = payload(sym, seed, vol, sessions)
        if edit:
            edit(sym, p)
        (d / f"{sym}.json").write_text(json.dumps(p))
    return d


def frames_of(raw: Path, end) -> dict:
    return {s: hc.chart_frame(raw / f"{s}.json", end) for s in SPEC}


def drop_day(p: dict, day: str) -> dict:
    j = p["chart"]["result"][0]
    t = int(pd.Timestamp(day, tz="America/New_York").replace(hour=9, minute=30).timestamp())
    k = j["timestamp"].index(t)
    j["timestamp"].pop(k)
    for v in j["indicators"]["quote"][0].values():
        v.pop(k)
    j["indicators"]["adjclose"][0]["adjclose"].pop(k)
    return p


@pytest.fixture(scope="module")
def raw(tmp_path_factory):
    return write_raw(tmp_path_factory.mktemp("raw"))


# ======================================================================== calendar


@pytest.mark.parametrize("now, want", [
    ("2026-10-19 09:48", "2026-10-16"),     # Monday morning: Friday
    ("2026-10-19 21:30", "2026-10-16"),     # 17:30 EDT: not complete yet
    ("2026-10-19 22:30", "2026-10-19"),     # 18:30 EDT
    ("2026-10-24 15:00", "2026-10-23"),     # Saturday
    ("2026-11-02 22:30", "2026-10-30"),     # 17:30 EST (DST ended 11-01): Friday
    ("2026-11-02 23:30", "2026-11-02"),     # 18:30 EST
    ("2026-11-26 23:30", "2026-11-25"),     # Thanksgiving
    ("2026-11-27 19:00", "2026-11-25"),     # half day, 14:00 EST: its bar counts from 18:00
    ("2026-11-27 23:30", "2026-11-27"),
    ("2026-12-25 23:30", "2026-12-24"),     # Christmas: the half day before
    ("2027-01-01 23:30", "2026-12-31"),     # New Year
    ("2026-07-03 23:30", "2026-07-02"),     # Independence Day observed on Friday
    ("2027-03-26 23:30", "2027-03-25"),     # Good Friday
    ("2027-03-15 22:30", "2027-03-15"),     # 18:30 EDT, DST began 03-14
])
def test_last_completed_session(now, want):
    assert hc.last_completed_session(utc(now)) == pd.Timestamp(want)


def test_month_end_sessions_and_due_signals():
    cal = hc.nyse_sessions("2026-10-01", "2027-02-26")
    assert hc.month_end_sessions(cal, "2026-10-30", "2027-01-28") == [pd.Timestamp(d) for d in
                                                                      ("2026-10-30", "2026-11-30", "2026-12-31")]
    assert hc.month_end_sessions(cal, "2026-10-30", "2027-01-31")[-1] == pd.Timestamp("2027-01-29")
    assert hc.b3_due_signals(cal, "2026-10-30") == []                     # due at the next session's run
    assert hc.b3_due_signals(cal, "2026-11-02") == [pd.Timestamp("2026-10-30")]
    assert hc.next_session("2026-12-31", cal) == pd.Timestamp("2027-01-04")   # 01-01 holiday
    assert list(hc.sessions_between("2026-11-25", "2026-11-30", cal)) == [pd.Timestamp("2026-11-27")]


def _crons(path: Path) -> list:
    return re.findall(r'cron:\s*"([^"]+)"', path.read_text())


def cron_runs_for(day: str, crons: list) -> list:
    """New York times of the cron runs that belong to the evening of ``day`` (a weekday)."""
    out = []
    for c in crons:
        minute, hour, dom, mon, dow = c.split()
        assert (dom, mon) == ("*", "*")
        lo, hi = (int(x) for x in dow.split("-"))
        for shift in (0, 1):                                    # a run after midnight UTC belongs to the evening before
            t = pd.Timestamp(f"{day} {int(hour):02d}:{int(minute):02d}", tz="UTC") + pd.Timedelta(days=shift)
            if lo <= t.isoweekday() <= hi and t.tz_convert("America/New_York").date() == pd.Timestamp(day).date():
                out.append(t)
    return out


@pytest.mark.parametrize("day", ["2026-10-19", "2026-11-16", "2027-03-01", "2027-07-12",   # EDT, EST, EST, EDT
                                 "2026-10-23", "2026-11-20"])                            # Fridays
def test_daily_cron_runs_after_the_close_in_both_dst_regimes(day):
    """Each weekday evening has three runs that check that day's session (one may be dropped by GitHub): two early
    ones and the final one (FINAL_CRON), which runs after the 20:00 grace hour, so a missing bar fails that
    evening."""
    wf = (ROOT / ".github/workflows/forward_observation_daily.yml").read_text()
    crons = _crons(ROOT / ".github/workflows/forward_observation_daily.yml")
    final = re.search(r'FINAL_CRON: "([^"]+)"', wf).group(1)
    assert final in crons and len(crons) == 3
    runs = cron_runs_for(day, crons)
    checked = [t for t in runs if hc.last_completed_session(t.to_pydatetime()) == pd.Timestamp(day)]
    assert len(checked) == 3, runs
    last = cron_runs_for(day, [final])
    assert len(last) == 1 and last[0] == max(runs) and not hc.in_grace(last[0].to_pydatetime(), day)
    assert all(t.tz_convert("America/New_York").date() == pd.Timestamp(day).date() for t in runs)


def test_freshness_grace_before_20h_new_york(raw):
    target = pd.Timestamp("2026-11-13")
    fr = frames_of(raw, target)
    stale = dict(fr, MSFT=fr["MSFT"][fr["MSFT"].index <= "2026-11-12"])
    assert hc.in_grace(utc("2026-11-13 23:30"), target)             # 18:30 EST
    assert not hc.in_grace(utc("2026-11-14 01:30"), target)         # 20:30 EST
    assert not hc.in_grace(utc("2026-11-16 23:30"), target)         # a later day
    c = hc.check_freshness(stale, target, CAL, data_end="2026-11-12", grace=True)
    assert c.status == hc.WARN and "not published yet" in c.items[0]
    assert hc.check_freshness(stale, target, CAL, data_end="2026-11-12").status == hc.FAIL
    assert hc.check_freshness(dict(fr, ONEQ=None), target, CAL, grace=True).status == hc.FAIL   # no file: no grace


# ======================================================================== chart reader


def test_chart_frame_reads_events_and_real_factor(tmp_path):
    p = payload("AAPL", 3, 0.03, split_on="2026-11-02")
    f = hc.chart_frame(p, "2026-12-31")
    assert f.loc["2026-11-02", "split"] == 2.0
    assert f.loc["2026-10-30", "factor"] == 2.0 and f.loc["2026-11-02", "factor"] == 1.0
    assert (f["div"] > 0).sum() > 5
    assert hc.chart_frame(p, "2026-10-30")["factor"].iloc[-1] == 1.0      # a split after the end is not known
    assert hc.chart_frame({"chart": {"result": [{}]}}) is None


# ======================================================================== 1. freshness


def test_freshness_ok_stale_and_held_names(raw):
    target = pd.Timestamp("2026-11-13")
    fr = frames_of(raw, target)
    c = hc.check_freshness(fr, target, CAL, data_end=target)
    assert c.status == hc.OK, c
    stale = dict(fr, MSFT=fr["MSFT"][fr["MSFT"].index <= "2026-11-12"])
    c = hc.check_freshness(stale, target, CAL, data_end=target)
    assert c.status == hc.FAIL and "MSFT: last bar 2026-11-12" in c.items[0]
    assert hc.check_freshness(dict(fr, ONEQ=None), target, CAL).status == hc.FAIL
    assert hc.check_freshness(fr, target, CAL, data_end="2026-11-12").status == hc.FAIL
    # a held short one session behind: fail (vendor delay?); five sessions: warn (delisted, the rule covers it)
    one = fr["AAPL"][fr["AAPL"].index <= "2026-11-12"]
    five = fr["AAPL"][fr["AAPL"].index <= "2026-11-06"]
    assert hc.check_freshness(fr, target, CAL, held={"XYZ": one}).status == hc.FAIL
    assert hc.check_freshness(fr, target, CAL, held={"XYZ": five}).status == hc.WARN
    assert hc.check_freshness(fr, target, CAL, held={"XYZ": fr["AAPL"]}).status == hc.OK


def test_freshness_over_a_holiday_weekend(raw):
    """Run on Monday 2027-01-18 (MLK day) evening: the last completed session is Friday 01-15."""
    target = hc.last_completed_session(utc("2027-01-18 23:30"))
    assert target == pd.Timestamp("2027-01-15")
    assert hc.check_freshness(frames_of(raw, target), target, CAL, data_end=target).status == hc.OK


# ======================================================================== 2. integrity


def test_integrity_clean_and_each_fault(tmp_path):
    target = pd.Timestamp("2026-12-31")
    clean = frames_of(write_raw(tmp_path / "clean"), target)
    c = hc.check_integrity(clean, target, CAL)
    assert c.status == hc.OK, c.items

    def one(edit, sym="AAPL"):
        p = payload(sym, *SPEC[sym])
        edit(p)
        return hc.check_integrity(dict(clean, **{sym: hc.chart_frame(p, target)}), target, CAL)
    q = lambda p: p["chart"]["result"][0]["indicators"]["quote"][0]  # noqa: E731
    k = int(CAL.searchsorted(pd.Timestamp("2026-11-18")))
    # a missing session inside the window
    c = one(lambda p: drop_day(p, "2026-11-18"))
    assert c.status == hc.FAIL and "missing session" in c.items[0]

    # a bar on a holiday (Thanksgiving)
    def holiday(p):
        j = p["chart"]["result"][0]
        t = int(pd.Timestamp("2026-11-26", tz="America/New_York").replace(hour=9, minute=30).timestamp())
        pos = j["timestamp"].index(int(pd.Timestamp("2026-11-25", tz="America/New_York").replace(
            hour=9, minute=30).timestamp())) + 1
        j["timestamp"].insert(pos, t)
        for v in q(p).values():
            v.insert(pos, v[pos - 1])
        j["indicators"]["adjclose"][0]["adjclose"].insert(pos, j["indicators"]["adjclose"][0]["adjclose"][pos - 1])
    c = one(holiday)
    assert c.status == hc.FAIL and "non-session" in " ".join(c.items)

    # a non-positive price
    c = one(lambda p: q(p)["low"].__setitem__(k, 0.0))
    assert c.status == hc.FAIL and "non-positive" in " ".join(c.items)

    # an unexplained jump (a bad tick: close x 3 for one day)
    def tick(p):
        for key in ("open", "high", "low", "close"):
            q(p)[key][k] *= 3
        p["chart"]["result"][0]["indicators"]["adjclose"][0]["adjclose"][k] *= 3
    c = one(tick)
    assert c.status == hc.FAIL and "no split or dividend explains it" in " ".join(c.items)
    # a missing open is a warning (the runner fills it from the close)
    c = one(lambda p: q(p)["open"].__setitem__(k, None))
    assert c.status == hc.WARN


def test_integrity_split_explained_and_unadjusted_split_caught():
    target = pd.Timestamp("2026-12-31")
    base = {s: hc.chart_frame(payload(s, *SPEC[s]), target) for s in SPEC}
    ok = dict(base, AAPL=hc.chart_frame(payload("AAPL", 3, 0.035, split_on="2026-11-02"), target))
    c = hc.check_integrity(ok, target, CAL)
    assert c.status == hc.OK, c.items                    # the raw close halves, the split explains it
    f = ok["AAPL"]
    assert hc.real_close(f).loc["2026-11-02"] / hc.real_close(f).loc["2026-10-30"] < 0.6
    bad = dict(base, AAPL=hc.chart_frame(payload("AAPL", 3, 0.035, split_on="2026-11-02", adjusted_split=False),
                                         target))
    c = hc.check_integrity(bad, target, CAL)
    assert c.status == hc.FAIL and "split 2" in " ".join(c.items)


# ======================================================================== 3. vendor agreement


def test_vendor_agreement(raw):
    target = pd.Timestamp("2026-11-13")
    fr = frames_of(raw, target)
    c = hc.check_vendors(fr, {"alpaca": dict(fr)}, target, CAL)
    assert c.status == hc.OK and "closes within 0.2%" in c.message
    off = fr["MSFT"].copy()
    off.loc["2026-11-10", "close"] *= 1.01
    c = hc.check_vendors(fr, {"alpaca": dict(fr, MSFT=off)}, target, CAL)
    assert c.status == hc.FAIL and c.items == ["MSFT 2026-11-10: stored close differs from alpaca by 0.99% "
                                               "(tolerance 0.2%)"]
    assert c.data["keys"] == ["MSFT 2026-11-10"]
    # 0.3% apart: within 0.5% but beyond the 0.2% of the B1 / B2 series (B3 shorts keep 0.5%)
    off3 = fr["MSFT"].copy()
    off3.loc["2026-11-10", "close"] *= 1.003
    assert hc.check_vendors(fr, {"alpaca": dict(fr, MSFT=off3)}, target, CAL).status == hc.FAIL
    assert hc.check_vendors(fr, {"alpaca": dict(fr, MSFT=off3)}, target, CAL,
                            tols={"MSFT": hc.VENDOR_TOL_B3}).status == hc.OK
    # accepted by the owner (--accept-data): reported, not failed
    c = hc.check_vendors(fr, {"alpaca": dict(fr, MSFT=off)}, target, CAL, accepted=["MSFT 2026-11-10"])
    assert c.status == hc.OK and "(accepted earlier)" in c.items[0]
    # no second vendor: a warning on an early run, a failure on the evening's last run
    none = {"alpaca": {s: None for s in fr}}
    assert hc.check_vendors(fr, none, target, CAL, notes=["alpaca: no keys"]).status == hc.WARN
    assert hc.check_vendors(fr, none, target, CAL, must=list(fr), strict=False).status == hc.WARN
    c = hc.check_vendors(fr, none, target, CAL, must=list(fr))
    assert c.status == hc.FAIL and "last run" in c.message
    # one series without a second vendor: listed, a warning
    c = hc.check_vendors(fr, {"alpaca": dict(fr, MSFT=None)}, target, CAL, must=list(fr))
    assert c.status == hc.WARN and "no second vendor for MSFT" in c.items
    # the stored vendor is not compared with itself
    c = hc.check_vendors(fr, {"alpaca": dict(fr, MSFT=off)}, target, CAL, sources={"MSFT": "alpaca"})
    assert c.status == hc.OK
    # a split inside the compared days, adjusted by one vendor and raw (split event only) by the other: same real close
    sp = hc.chart_frame(payload("AAPL", 3, 0.035, split_on="2026-11-09"), target)
    other = sp.copy()
    other.loc[other.index < "2026-11-09", ["open", "high", "low", "close"]] *= 2   # raw prices before the split
    other["factor"] = 1.0
    assert hc.check_vendors({"AAPL": sp}, {"yahoo": {"AAPL": other}}, target, CAL).status == hc.OK


def test_fetch_secondaries_skips_the_stored_vendor_and_stops_yahoo(raw):
    target = pd.Timestamp("2026-11-13")
    body = json.dumps(payload("QQQ", 1, 0.015)).encode()
    urls = []

    def yahoo(url):
        urls.append(url)
        if "/MSFT?" in url:
            from urllib.error import HTTPError
            raise HTTPError(url, 429, "slow down", None, None)
        return body
    items = {"QQQ": ("QQQ", "QQQ", "tiingo"), "MSFT": ("MSFT", "MSFT", "tiingo"), "ONEQ": ("ONEQ", "ONEQ", "tiingo"),
             "AAPL": ("AAPL", "AAPL", "yahoo")}
    out, notes = daily.fetch_secondaries(items, target, {"alpaca_headers": None, "yahoo": yahoo}, sleep=lambda s: None)
    assert [u.split("/chart/")[1].split("?")[0] for u in urls] == ["QQQ", "MSFT"]   # AAPL is stored from Yahoo
    assert "yahoo: HTTP 429 at MSFT" in notes and "alpaca: no keys, not compared" in notes
    assert out["yahoo"]["QQQ"].index[-1] == target


# ======================================================================== 4. signal consistency (B1 / B2)


@pytest.fixture(scope="module")
def computed(raw, tmp_path_factory):
    nos = tmp_path_factory.mktemp("nosmisp")
    return {a: fo.compute(a, raw, STOCKS, smisp_dir=nos) for a in ("2026-11-13", "2027-01-29")}


def test_signals_match_the_runner(raw, computed):
    for as_of, res in computed.items():
        c, nxt = hc.check_signals(res["lines"], frames_of(raw, as_of), as_of, STOCKS, universe_check=False)
        assert c.status == hc.OK, c.items
    c, _ = hc.check_signals(computed["2027-01-29"]["lines"], frames_of(raw, "2027-01-29"), "2027-01-29", STOCKS,
                            universe_check=False)
    n = {k: int(v) for k, v in re.findall(r"(B1|SEL-A|SEL-P) (\d+) trade", c.message)}
    assert n["B1"] >= 3 and n["SEL-A"] >= 2 and n["SEL-P"] >= 2, c.message
    # the registered universe is checked in production
    c, _ = hc.check_signals(computed["2027-01-29"]["lines"], frames_of(raw, "2027-01-29"), "2027-01-29", STOCKS)
    assert c.status == hc.FAIL and "registered U18" in c.items[0]


def test_signals_catch_a_missing_or_extra_trade(raw, computed):
    res = copy.deepcopy(computed["2027-01-29"])
    fr = frames_of(raw, "2027-01-29")
    res["lines"]["B1"]["trades"].pop(0)
    c, _ = hc.check_signals(res["lines"], fr, "2027-01-29", STOCKS, universe_check=False)
    assert c.status == hc.FAIL and "independent rule has" in c.items[0]
    res = copy.deepcopy(computed["2027-01-29"])
    t = dict(res["lines"]["SEL-A"]["trades"][0])
    res["lines"]["SEL-A"]["trades"][0] = {**t, "exit": t["exit"] + pd.Timedelta(days=1)}
    c, _ = hc.check_signals(res["lines"], fr, "2027-01-29", STOCKS, universe_check=False)
    assert c.status == hc.FAIL and any(x.startswith("SEL-A: runner has") for x in c.items)


def test_signals_catch_a_changed_frozen_parameter(raw, monkeypatch, tmp_path):
    """A change to a frozen value in the strategy modules changes the runner's trades, not the independent rule."""
    fr = frames_of(raw, "2027-01-29")
    monkeypatch.setitem(st.PARAMS["stock"], "Yb", 0.08)           # B1 trigger 8% below SMA20 instead of 10%
    res = fo.compute("2027-01-29", raw, STOCKS, smisp_dir=tmp_path)
    c, _ = hc.check_signals(res["lines"], fr, "2027-01-29", STOCKS, universe_check=False)
    assert c.status == hc.FAIL and any(x.startswith("B1 ") for x in c.items)
    monkeypatch.undo()
    real = tg.measures

    def rsi3(adj):                                                  # "RSI2" silently computed as RSI(3)
        x = real(adj)
        x[3] = st.rsi_wilder(adj, 3)
        return x
    monkeypatch.setattr(tg, "measures", rsi3)
    res = fo.compute("2027-01-29", raw, STOCKS, smisp_dir=tmp_path)
    c, _ = hc.check_signals(res["lines"], fr, "2027-01-29", STOCKS, universe_check=False)
    assert c.status == hc.FAIL and any(x.startswith("SEL-A") for x in c.items)
    assert any(x.startswith("SEL-P") for x in c.items)


def test_next_session_notes_on_a_trigger_close(raw, tmp_path):
    """The stock shock of 2026-10-20 (close far below SMA20) is announced for the next open the same evening (MSFT;
    AAPL already holds a trade from 10-14, so it gets no second one)."""
    res = fo.compute("2026-10-20", raw, STOCKS, smisp_dir=tmp_path)
    c, nxt = hc.check_signals(res["lines"], frames_of(raw, "2026-10-20"), "2026-10-20", STOCKS, universe_check=False)
    assert c.status == hc.OK
    assert "B1 MSFT: close <= 0.9 x SMA20 at the 10-20 close: buy at the next open" in nxt
    assert "B1 AAPL: open since 10-14 (holding day 5 of 20; sell limit resting)" in nxt
    assert not any(n.startswith("B1 AAPL: close") for n in nxt)
    res2 = fo.compute("2026-10-21", raw, STOCKS, smisp_dir=tmp_path)
    assert any(t["symbol"] == "MSFT" and t["entry"] == pd.Timestamp("2026-10-21")
               for t in res2["lines"]["B1"]["trades"])


def long_window_trades(raw_dir, stocks, end, b1_starts, b2_starts) -> dict:
    """The independent replays vs the frozen simulators (selective_t.simulate, t_grid.exact_run) on windows much
    longer than the forward window so far: every trade must be the same (entry, exit, reason)."""
    d = fo.load_all(end, raw_dir, stocks)
    n = {"B1": 0, "B2": 0}
    spec = st.variant_spec(st.fams_of("S3-Yb", "stock"), "net", st.S2_SLIP["stock"])
    for start in b1_starts:
        for sym, m in d.stocks.items():
            w = fo.window_of(m, end, start)
            sim = st.simulate(m, w.s, w.e, spec)
            theirs = {(str(r.entry.date()), str(r.exit.date()), r.reason) for r in sim["trips"].itertuples()}
            rp = hc.b1_replay(hc.chart_frame(fo.raw_path(sym, raw_dir), end), end, start_close=start)
            mine = {(str(t["entry"].date()), str(t["exit"].date()), t["reason"]) for t in rp["trades"]}
            assert mine == theirs, (sym, start, sorted(mine ^ theirs)[:4])
            n["B1"] += len(mine)
    data, q = fo.b2_data(d), hc.chart_frame(fo.raw_path("QQQ", raw_dir), end)
    for start in b2_starts:
        ses = d.qqq.s.sessions
        first = ses[ses.searchsorted(pd.Timestamp(start)) + 1]
        ex = tg.exact_run(data, "QQQ", list(fo.B2_IDS.values()), str(first.date()), end, tg.CostModel(),
                          record_trades=True)
        lv = hc.b2_levels(q, end, start)
        for j, (name, rule) in enumerate(hc.B2_RULES.items()):
            theirs = {(str(ex["sessions"][t["t0"]].date()), str(ex["sessions"][t["t1"]].date()), t["reason"])
                      for t in ex["trades"] if t["cfg"] == j}
            mine = {(str(t["entry"].date()), str(t["exit"].date()), t["reason"])
                    for t in hc.b2_replay(lv, rule["max_hold"])["trades"]}
            assert mine == theirs, (name, start, sorted(mine ^ theirs)[:4])
            n["B2"] += len(mine)
    return n


def test_independent_rules_equal_the_frozen_simulators_over_long_windows(raw):
    n = long_window_trades(raw, STOCKS, "2027-01-29", ("2024-03-01", "2025-06-02"), ("2025-01-02", "2026-03-02"))
    assert n["B1"] >= 5 and n["B2"] >= 5, n


def test_independent_rules_on_the_local_vendor_files():
    """Same on the real B1 / B2 files of the local cache (skipped where they are not, e.g. on GitHub Actions)."""
    syms = ("QQQ", "ONEQ", *fo.STOCKS)
    if not all(fo.raw_path(s, fo.RAW).exists() for s in syms):
        pytest.skip("no local forward-observation raw files")
    end = str(hc.chart_frame(fo.raw_path("QQQ", fo.RAW)).index[-1].date())
    n = long_window_trades(fo.RAW, fo.STOCKS, end, ("2024-03-01",), ("2005-01-03",))
    assert n["B1"] > 50 and n["B2"] > 50, n


def test_rsi_wilder_matches_the_definition():
    c = np.array([10.0, 11.0, 10.5, 10.0, 10.8, 11.2, 11.0])
    r = hc.rsi_wilder(c, 2)
    au, ad = (1.0 + 0.0) / 2, (0.0 + 0.5) / 2                    # first two changes: +1, -0.5
    assert math.isclose(r[2], 100 - 100 / (1 + au / ad))
    au, ad = (au + 0.0) / 2, (ad + 0.5) / 2
    assert math.isclose(r[3], 100 - 100 / (1 + au / ad)) and np.isnan(r[:2]).all()


# ======================================================================== 5. log status


def test_log_status(raw, tmp_path):
    log = tmp_path / "log.md"
    res = fo.run("2026-11-13", raw_dir=raw, log_path=log, stocks=STOCKS, smisp_dir=tmp_path / "s")
    c = hc.check_log_status(log.read_text(), res["data_end"])
    assert c.status == hc.OK and "only 2026-11 is provisional" in c.message
    bad = log.read_text().replace("| 2026-11 (", "| 2026-11 (")
    bad = re.sub(r"(\| 2026-11 \|.*)\| provisional \|", r"\1| final |", bad, count=1)
    c = hc.check_log_status(bad, res["data_end"])
    assert c.status == hc.FAIL and "expected 'provisional'" in c.items[0]


# ======================================================================== 6. B3 signal files


def add_meta(state: Path):
    for p in sorted(state.glob("s_*.csv")):
        sc = pd.read_csv(p, dtype={"security_id": str, "ticker": str, "yahoo": str})
        w = list(sc.set_index("security_id").loc[hc.worst_2n(sc), "ticker"])
        meta = {"u300": len(sc), "scored": int(sc["S-MISP"].notna().sum()), "worst_2n": w}
        p.with_name(p.stem + "_meta.json").write_text(json.dumps(meta))


@pytest.fixture(scope="module")
def b3env(tmp_path_factory):
    env = tfo.make_smisp_env(tmp_path_factory.mktemp("b3"))
    add_meta(env["smisp"] / "state")
    env["cal"] = tfo.SESSIONS                      # the synthetic B3 data use business days
    return env


def b3_line(env, as_of, borrow=None):
    res = fo.compute(as_of, env["raw"], tfo.STOCKS, smisp_dir=env["smisp"], borrow_dir=borrow or env["root"] / "nb")
    return res["lines"]["B3"]


def test_b3_before_and_at_the_first_signal(b3env):
    st_ = b3env["smisp"] / "state"
    c, _, _ = hc.check_b3(st_, "2026-10-29", b3env["cal"], None)
    assert c.status == hc.OK and "2026-10-30 close" in c.message
    c, _, _ = hc.check_b3(st_, "2026-10-30", b3env["cal"], None)
    assert c.status == hc.OK and "today's close" in c.message


def test_b3_clean_line(b3env):
    line = b3_line(b3env, "2027-01-29")
    c, hashes, held = hc.check_b3(b3env["smisp"] / "state", "2027-01-29", b3env["cal"], line)
    assert c.status in (hc.OK, hc.WARN), c.items        # (whole-share rounding may leave fewer than N names)
    assert not [x for x in c.items if "outside" in x or "missing" in x]
    assert len(hashes) == 3 and 0 < len(held) <= hc.B3_N


def test_b3_missing_signal_fails(b3env, tmp_path):
    empty = tmp_path / "state"
    empty.mkdir()
    c, _, _ = hc.check_b3(empty, "2026-11-02", b3env["cal"], None)
    assert c.status == hc.FAIL and "signal 2026-10-30 missing: due at the 2026-11-02 run" in c.items[0]
    c, _, _ = hc.check_b3(empty, "2026-11-02", b3env["cal"], None, grace=True)    # before 20:00 on 11-02
    assert c.status == hc.WARN
    c, _, _ = hc.check_b3(empty, "2026-11-03", b3env["cal"], None, grace=True)    # a day late: no grace
    assert c.status == hc.FAIL


def _copy_state(b3env, tmp_path) -> Path:
    d = tmp_path / "state"
    d.mkdir(parents=True)
    for p in (b3env["smisp"] / "state").iterdir():
        (d / p.name).write_bytes(p.read_bytes())
    return d


def test_b3_bad_signal_files_fail(b3env, tmp_path):
    d = _copy_state(b3env, tmp_path)
    p = d / "s_2026-10-30.csv"
    sc = pd.read_csv(p, dtype={"security_id": str})
    sc.loc[sc.index[5:], "S-MISP"] = np.nan                       # only 5 scored names: fewer than N
    sc.to_csv(p, index=False)
    c, _, _ = hc.check_b3(d, "2026-11-02", b3env["cal"], None)
    assert c.status == hc.FAIL and any("fewer than N = 10" in x for x in c.items)
    d2 = _copy_state(b3env, tmp_path / "b")
    m = d2 / "s_2026-10-30_meta.json"
    meta = json.loads(m.read_text())
    meta["worst_2n"] = meta["worst_2n"][:19]                      # 19 names instead of 2N = 20
    m.write_text(json.dumps(meta))
    c, _, _ = hc.check_b3(d2, "2026-11-02", b3env["cal"], None)
    assert c.status == hc.FAIL and any("worst-2N list (19 names)" in x for x in c.items)
    d3 = _copy_state(b3env, tmp_path / "c")
    sc = pd.read_csv(d3 / "s_2026-10-30.csv", dtype={"security_id": str})
    sc.loc[0, "S-MISP"] = 1.7
    sc.to_csv(d3 / "s_2026-10-30.csv", index=False)
    c, _, _ = hc.check_b3(d3, "2026-11-02", b3env["cal"], None)
    assert c.status == hc.FAIL and any("outside 0..1" in x for x in c.items)


def test_b3_changed_frozen_file_and_wrong_shorts_fail(b3env):
    st_ = b3env["smisp"] / "state"
    line = b3_line(b3env, "2027-01-29")
    _, hashes, _ = hc.check_b3(st_, "2027-01-29", b3env["cal"], line)
    known = dict(hashes, **{"s_2026-11-30.csv": "0" * 64})
    c, _, _ = hc.check_b3(st_, "2027-01-29", b3env["cal"], line, known)
    assert c.status == hc.FAIL and any("changed after it was first recorded" in x for x in c.items)
    bad = copy.copy(line)
    bad["res"] = dict(line["res"])
    t, names = line["res"]["held"][-1]
    bad["res"]["held"] = line["res"]["held"][:-1] + [(t, list(names[:-1]) + ["not-a-loser"])]
    c, _, _ = hc.check_b3(st_, "2027-01-29", b3env["cal"], bad)
    assert c.status == hc.FAIL and any("outside the worst 2N" in x for x in c.items)
    # a runner that did not start although a signal is executable
    c, _, _ = hc.check_b3(st_, "2027-01-29", b3env["cal"], {"started": False, "reason": "no EFFR file"})
    assert c.status == hc.FAIL and "no EFFR file" in c.items[0]


def test_b3_borrow_snapshots_report_only(b3env, tmp_path):
    line = b3_line(b3env, "2026-12-31")
    assert hc.check_b3_borrow(tmp_path, "2026-10-29", None).status == hc.OK
    c = hc.check_b3_borrow(tmp_path, "2026-12-31", line)            # report only: never a warning every day
    assert c.status == hc.OK and c.message.startswith("report only")
    (tmp_path / "raw").mkdir()
    with gzip.open(tmp_path / "raw" / "usa_20261230T223000Z.txt.gz", "wt") as fh:
        fh.write("#BOF|2026.12.30|18:30:00\n#SYM|CUR|NAME|CON|ISIN|REBATERATE|FEERATE|AVAILABLE|FIGI|\n#EOF\n")
    assert hc.check_b3_borrow(tmp_path, "2026-12-31", line).status == hc.OK


# ======================================================================== 7. replay drift


def _inputs(raw, end):
    fpf = pd.Timestamp(hc.START_CLOSE) - pd.Timedelta(days=hc.FINGERPRINT_FROM_DAYS)
    return {"px": {s: hc.chart_fingerprints(f, fpf, end) for s, f in frames_of(raw, end).items()}, "b3": {},
            "effr": {}}


def test_replay_numbers_are_stable_from_day_to_day(raw, tmp_path):
    """The frozen simulators are causal except at the window's last session: every day before the previous run's
    last session (H_match: before its month) and every closed trade is unchanged by later data."""
    days = [d for d in CAL if pd.Timestamp("2026-10-27") <= d <= pd.Timestamp("2026-11-04")] + [
        pd.Timestamp("2026-12-01"), pd.Timestamp("2027-01-29")]
    prev, compared = None, 0
    for d in days:
        res = fo.compute(str(d.date()), raw, STOCKS, smisp_dir=tmp_path)
        c, state = hc.check_replay(prev, hc.replay_numbers(res["lines"]), _inputs(raw, d), res["data_end"])
        assert c.status == hc.OK, (d, c.items)
        if prev:
            p_end = pd.Timestamp(prev["data_end"])
            compared += sum(1 for v in prev["numbers"]["series"].values() for day in v if pd.Timestamp(day) < p_end)
        prev = state
    assert compared > 200 and prev["numbers"]["trades"]


def test_replay_numbers_b3_stable(b3env):
    a = fo.compute("2026-12-15", b3env["raw"], tfo.STOCKS, smisp_dir=b3env["smisp"], borrow_dir=b3env["root"] / "x")
    b = fo.compute("2027-01-29", b3env["raw"], tfo.STOCKS, smisp_dir=b3env["smisp"], borrow_dir=b3env["root"] / "x")
    _, s = hc.check_replay(None, hc.replay_numbers(a["lines"]), {}, a["data_end"])
    assert s["numbers"]["series"]["B3.nav"] and s["numbers"]["b3_held"]
    c, _ = hc.check_replay(s, hc.replay_numbers(b["lines"]), {}, b["data_end"])
    assert c.status == hc.OK, c.items


def test_replay_drift_fails_persists_and_can_be_accepted(raw, tmp_path):
    a = fo.compute("2026-11-13", raw, STOCKS, smisp_dir=tmp_path)
    b = fo.compute("2026-11-20", raw, STOCKS, smisp_dir=tmp_path)
    _, s = hc.check_replay(None, hc.replay_numbers(a["lines"]), _inputs(raw, "2026-11-13"), a["data_end"])
    s["numbers"]["series"]["SEL-A.ret"]["2026-11-05"] += 1e-6        # as if the code had changed a number
    cur = hc.replay_numbers(b["lines"])
    c, s2 = hc.check_replay(s, cur, _inputs(raw, "2026-11-20"), b["data_end"])
    assert c.status == hc.FAIL and c.items[0].startswith("SEL-A.ret 2026-11-05")
    assert s2["numbers"]["series"]["SEL-A.ret"]["2026-11-05"] == s["numbers"]["series"]["SEL-A.ret"]["2026-11-05"]
    c, _ = hc.check_replay(s2, cur, _inputs(raw, "2026-11-20"), b["data_end"])
    assert c.status == hc.FAIL                                      # still failing on the next run
    c, s3 = hc.check_replay(s2, cur, _inputs(raw, "2026-11-20"), b["data_end"], accept=True)
    assert c.status == hc.WARN and "accepted" in c.message
    c, _ = hc.check_replay(s3, cur, _inputs(raw, "2026-11-20"), b["data_end"])
    assert c.status == hc.OK
    # an earlier as-of: not compared, state kept
    c, s4 = hc.check_replay(s3, cur, {}, "2026-11-10")
    assert c.status == hc.WARN and s4 is None


def test_replay_drift_after_a_vendor_revision_is_a_named_warning(tmp_path):
    raw = write_raw(tmp_path / "raw")
    a = fo.compute("2026-11-13", raw, STOCKS, smisp_dir=tmp_path)
    _, s = hc.check_replay(None, hc.replay_numbers(a["lines"]), _inputs(raw, "2026-11-13"), a["data_end"])

    def revise(sym, p):                                             # the vendor restates AAPL on 11-05
        if sym == "AAPL":
            k = int(CAL.searchsorted(pd.Timestamp("2026-11-05")))
            j = p["chart"]["result"][0]
            for key in ("open", "high", "low", "close"):
                j["indicators"]["quote"][0][key][k] *= 1.004
            j["indicators"]["adjclose"][0]["adjclose"][k] *= 1.004
    write_raw(raw, edit=revise)
    b = fo.compute("2026-11-20", raw, STOCKS, smisp_dir=tmp_path)
    c, s2 = hc.check_replay(s, hc.replay_numbers(b["lines"]), _inputs(raw, "2026-11-20"), b["data_end"])
    assert c.status == hc.WARN, c.items
    assert any("vendor revised: AAPL 2026-11-05" in x for x in c.items)
    assert any(x.startswith("B1.ret 2026-11-05") for x in c.items)
    assert not any(x.startswith("SEL-A") for x in c.items)           # QQQ / ONEQ did not change
    c, _ = hc.check_replay(s2, hc.replay_numbers(b["lines"]), _inputs(raw, "2026-11-20"), b["data_end"])
    assert c.status == hc.OK                                         # accepted once explained


# ======================================================================== 8. liveness


def test_liveness():
    assert hc.check_liveness(None, "2026-11-13", CAL).status == hc.OK
    assert hc.check_liveness({"last_run_session": "2026-11-12"}, "2026-11-13", CAL).status == hc.OK
    assert hc.check_liveness({"last_run_session": "2026-11-13"}, "2026-11-13", CAL).status == hc.OK
    c = hc.check_liveness({"last_run_session": "2026-11-10"}, "2026-11-13", CAL)
    assert c.status == hc.FAIL and c.items == ["2026-11-11", "2026-11-12"]
    assert hc.check_liveness({"last_run_session": "2026-11-25"}, "2026-11-27", CAL).status == hc.OK   # holiday
    assert hc.check_liveness({"last_run_session": "2026-11-13"}, "2026-11-16", CAL).status == hc.OK   # weekend


# ======================================================================== the daily run end to end


def daily_kw(env, tmp):
    return {"raw_dir": env["raw"], "log_path": tmp / "log.md", "stocks": tfo.STOCKS, "smisp_dir": env["smisp"],
            "borrow_dir": tmp / "borrow", "health_path": tmp / "health.json", "replay_path": tmp / "replay.json",
            "calendar": env["cal"], "cross_check": False, "universe_check": False, "save_details": False,
            "now": utc("2027-03-01 12:00")}


def test_daily_run_is_idempotent_and_writes_no_price_levels(b3env, tmp_path):
    kw = daily_kw(b3env, tmp_path)
    daily.run_daily(as_of="2026-12-30", **kw)                       # the previous session (baseline)
    rep = daily.run_daily(as_of="2026-12-31", **kw)
    assert rep["status"] in (hc.OK, hc.WARN), daily_summary(rep)
    files = {p: p.read_bytes() for p in (tmp_path / "log.md", tmp_path / "health.json", tmp_path / "replay.json")}
    rep2 = daily.run_daily(as_of="2026-12-31", **kw)                # the second cron run of the same session
    assert rep2 == rep, daily_summary(rep2)
    assert all(p.read_bytes() == b for p, b in files.items())      # byte-identical rerun: nothing to commit
    # the next session: liveness and replay pass, rows update
    nxt = str(hc.next_session("2026-12-31", b3env["cal"]).date())   # (the synthetic data trade on 01-01)
    rep3 = daily.run_daily(as_of=nxt, **kw)
    by = {c["name"]: c for c in rep3["checks"]}
    assert by["pipeline_liveness"]["status"] == hc.OK and by["replay_drift"]["status"] == hc.OK, daily_summary(rep3)
    assert json.loads((tmp_path / "health.json").read_text())["last_run_session"] == nxt
    assert (tmp_path / "log.md").read_text() != files[tmp_path / "log.md"]   # the provisional rows moved on
    # no vendor price level anywhere in the committed files
    d = fo.load_all(nxt, b3env["raw"], tfo.STOCKS)
    closes = {f"{x:.2f}" for m in (d.qqq, d.oneq, *d.stocks.values()) for x in m.s.close[-60:]}
    for p in (tmp_path / "health.json", tmp_path / "replay.json"):
        text = p.read_text()
        assert not [c for c in closes if re.search(rf"(?<![\d.]){re.escape(c)}(?!\d)", text)], p.name


def daily_summary(rep):
    return "\n".join(hc.summary_lines(rep))


def test_daily_run_flags_a_skipped_session_and_a_missing_file(b3env, tmp_path):
    kw = daily_kw(b3env, tmp_path)
    daily.run_daily(as_of="2026-12-28", **kw)
    rep = daily.run_daily(as_of="2026-12-31", **kw)                 # 12-29 and 12-30 were never run
    by = {c["name"]: c for c in rep["checks"]}
    assert rep["status"] == hc.FAIL and by["pipeline_liveness"]["items"] == ["2026-12-29", "2026-12-30"]
    # the runner cannot run (a data file is gone): health.json still written, the last run session kept
    kw2 = dict(kw, raw_dir=tmp_path / "empty")
    (tmp_path / "empty").mkdir()
    rep = daily.run_daily(as_of="2027-01-04", **kw2)
    by = {c["name"]: c for c in rep["checks"]}
    assert by["pipeline"]["status"] == hc.FAIL and "missing raw data" in by["pipeline"]["items"][0]
    assert by["data_freshness"]["status"] == hc.FAIL
    assert json.loads((tmp_path / "health.json").read_text())["last_run_session"] == "2026-12-31"


def test_daily_main_exit_codes(b3env, tmp_path, monkeypatch):
    calls = {}

    def fake(**kw):
        calls.update(kw)
        return hc.report("2026-12-31", [hc.Check("x", status, "m")])
    for status, code in ((hc.OK, 0), (hc.WARN, 0), (hc.FAIL, 3)):
        monkeypatch.setattr(daily, "run_daily", fake)
        out = tmp_path / "r.json"
        assert daily.main(["--dry-run", "--report-out", str(out)]) == code
        assert json.loads(out.read_text())["status"] == status
    assert calls["dry_run"] and calls["cross_check"]


# ======================================================================== the GitHub issue


BOT = {"login": "app/github-actions", "is_bot": True}


class FakeGh:
    """The few gh calls of health_issue, on an in-memory list of issues."""

    def __init__(self, issues=()):
        self.issues = [dict(it) for it in issues]
        self.calls = []
        self.comments = []

    def _get(self, num):
        return next(it for it in self.issues if it["number"] == int(num))

    def __call__(self, args):
        self.calls.append(args[:2])
        if args[:2] == ["issue", "list"]:
            state = args[args.index("--state") + 1]
            return json.dumps([{k: it[k] for k in ("number", "title", "body", "author")}
                               for it in self.issues if it["state"] == state])
        if args[:2] == ["issue", "create"]:
            body = Path(args[args.index("--body-file") + 1]).read_text()
            num = max([it["number"] for it in self.issues] + [6]) + 1
            self.issues.append({"number": num, "title": args[args.index("--title") + 1], "body": body,
                                "state": "open", "author": BOT})
        if args[:2] == ["issue", "edit"]:
            self._get(args[2])["body"] = Path(args[args.index("--body-file") + 1]).read_text()
        if args[:2] == ["issue", "comment"]:
            self.comments.append((int(args[2]), Path(args[args.index("--body-file") + 1]).read_text()))
        if args[:2] == ["issue", "close"]:
            self._get(args[2])["state"] = "closed"
        if args[:2] == ["issue", "reopen"]:
            self._get(args[2])["state"] = "open"
        return ""

    def open_(self, title=hi.TITLE):
        return [it for it in self.issues if it["state"] == "open" and it["title"] == title]


def rep_of(session, checks, early=False, week_last=False):
    return dict(hc.report(session, checks), run={"early": early, "week_last": week_last})


def test_issue_opened_updated_and_closed():
    bad = rep_of("2026-11-13", [hc.Check("data_freshness", hc.FAIL, "1 problem",
                                         ["QQQ: last bar 2026-11-12, expected 2026-11-13"]),
                                hc.Check("b3_signal", hc.OK, "fine")])
    gh = FakeGh()
    assert hi.sync(bad, "3", "u1", gh, mention="@owner") == "opened"
    body = gh.issues[0]["body"]
    assert body.startswith("@owner") and "FAIL" in body and "| data_freshness | fail |" in body
    assert "QQQ: last bar 2026-11-12" in body
    assert hi.sync(bad, "3", "u1", gh, mention="@owner") == "#7 unchanged"   # a rerun of the same run: nothing
    # the same failure a day later: the body is refreshed, no comment (no email every day)
    later = rep_of("2026-11-16", [hc.Check("data_freshness", hc.FAIL, "1 problem",
                                           ["QQQ: last bar 2026-11-13, expected 2026-11-16"])])
    assert hi.sync(later, "3", "u2", gh, mention="@owner") == "#7 body refreshed" and not gh.comments
    assert "2026-11-16" in gh.issues[0]["body"]
    # a new failure: body and a comment (the email)
    worse = rep_of("2026-11-16", [hc.Check("signal_consistency", hc.FAIL, "1 mismatch", ["B1 AAPL: runner has"])])
    assert hi.sync(worse, "3", "u3", gh, mention="@owner") == "updated #7"
    assert gh.comments and gh.comments[-1][1].startswith("@owner")
    # warnings on an early run do not close it; on the evening's last run they do
    warn = [hc.Check("data_freshness", hc.OK, "fine"), hc.Check("x", hc.WARN, "w")]
    assert hi.sync(rep_of("2026-11-17", warn, early=True), "0", "u4", gh) == "#7 left open (warnings on an early run)"
    assert hi.sync(rep_of("2026-11-17", warn), "0", "u4", gh) == "closed #7" and not gh.open_()
    assert hi.sync(rep_of("2026-11-17", warn), "0", "u5", gh) == "nothing to do"
    # the next failure reopens the bot's issue instead of opening a new one
    assert hi.sync(bad, "3", "u6", gh, mention="@owner") == "reopened #7" and len(gh.issues) == 1
    # the run stopped before the checks: no report
    gh = FakeGh()
    assert hi.sync(None, "1", "u7", gh) == "opened"
    assert "stopped before the health checks finished" in gh.issues[0]["body"]
    # all checks passed but the results were not pushed
    gh = FakeGh()
    ok = rep_of("2026-11-17", [hc.Check("x", hc.OK, "fine")])
    assert hi.sync(ok, "0", "u8", gh, commit_outcome="failure") == "opened"
    assert "were not pushed" in gh.issues[0]["body"]
    assert hi.sync(ok, "0", "u9", gh, commit_outcome="success") == "closed #7"


def test_issue_ignores_an_issue_not_opened_by_the_bot():
    gh = FakeGh([{"number": 3, "title": hi.TITLE, "body": "someone else", "state": "open",
                  "author": {"login": "mallory", "is_bot": False}}])
    ok = rep_of("2026-11-17", [hc.Check("x", hc.OK, "fine")])
    assert hi.sync(ok, "0", "u", gh) == "nothing to do" and gh.issues[0]["state"] == "open"
    bad = rep_of("2026-11-17", [hc.Check("x", hc.FAIL, "bad")])
    assert hi.sync(bad, "3", "u", gh) == "opened" and gh.issues[0]["body"] == "someone else"


def test_signature_ignores_dates_and_numbers_only():
    a = hi.failure_core(rep_of("2026-11-13", [hc.Check("data_freshness", hc.FAIL, "2 problem(s)",
                                                       ["QQQ: last bar 2026-11-12, expected 2026-11-13"])]))
    b = hi.failure_core(rep_of("2026-11-16", [hc.Check("data_freshness", hc.FAIL, "3 problem(s)",
                                                       ["QQQ: last bar 2026-11-13, expected 2026-11-16"])]))
    c = hi.failure_core(rep_of("2026-11-16", [hc.Check("data_freshness", hc.FAIL, "3 problem(s)",
                                                       ["ONEQ: last bar 2026-11-13, expected 2026-11-16"])]))
    assert hi.signature(a) == hi.signature(b) != hi.signature(c)


def test_weekly_warning_digest():
    gh = FakeGh()
    warn = [hc.Check("vendor_agreement", hc.WARN, "1 warning", ["CMCSA 2026-11-13: day total return differs"])]
    assert "not the week's last" in hi.sync_weekly(rep_of("2026-11-12", warn), "u", gh)
    assert "not the week's last" in hi.sync_weekly(rep_of("2026-11-13", warn, early=True, week_last=True), "u", gh)
    assert hi.sync_weekly(rep_of("2026-11-13", warn, week_last=True), "u", gh, mention="@owner") == "opened"
    it = gh.open_(hi.TITLE_WARN)[0]
    assert it["body"].startswith("@owner") and "CMCSA 2026-11-13" in it["body"]
    assert "already posted" in hi.sync_weekly(rep_of("2026-11-13", warn, week_last=True), "u", gh)
    assert hi.sync_weekly(rep_of("2026-11-20", warn, week_last=True), "u", gh) == f"updated #{it['number']}"
    ok = [hc.Check("vendor_agreement", hc.OK, "fine")]
    assert hi.sync_weekly(rep_of("2026-11-27", ok, week_last=True), "u", gh) == f"closed #{it['number']}"
    assert daily.week_last("2026-11-27") and daily.week_last("2026-11-25") is False   # (Thanksgiving week)
    assert daily.week_last("2026-12-31") and not daily.week_last("2026-12-30")   # (01-01 is a holiday)


def test_workflows():
    daily_wf = (ROOT / ".github/workflows/forward_observation_daily.yml").read_text()
    assert "PYTHONPATH=. python scripts/forward_observation_daily.py $args" in daily_wf
    assert "--fetch --fetch-smisp" in daily_wf and "issues: write" in daily_wf and "contents: write" in daily_wf
    assert "python -m quant.observation.health_issue" in daily_wf
    assert "group: forward-observation" in daily_wf
    # early runs, the final run, the dispatch inputs
    assert '[ "$SCHEDULE" != "$FINAL_CRON" ]; then args="$args --early"' in daily_wf
    for x in ("test_alert", "accept_data", "accept_replay", "--test-alert", "--accept-data"):
        assert x in daily_wf
    # the branch tip at job start, no token in .git/config during the run, a push retry, the commit outcome and the
    # owner's mention passed to the issue step, a cache key per attempt
    assert "ref: ${{ github.ref }}" in daily_wf and "persist-credentials: false" in daily_wf
    assert "for i in 1 2 3 4 5" in daily_wf and "id: commit" in daily_wf
    assert '--commit-outcome "$COMMIT" --mention "$MENTION"' in daily_wf and "github.repository_owner" in daily_wf
    assert "key: forward-obs-${{ github.run_id }}-${{ github.run_attempt }}" in daily_wf
    monthly = (ROOT / ".github/workflows/forward_observation.yml").read_text()
    assert "group: forward-observation" in monthly and "schedule:" not in monthly   # one scheduled writer
    assert "--dry-run" in monthly and "git push" not in monthly and "contents: read" in monthly   # one writer at all


# ======================================================================== fixes after the fault-injection, false-alarm
# and workflow reviews (2026-10-19)


def test_freshness_grace_only_for_the_session_just_closed(raw):
    target = pd.Timestamp("2026-11-13")
    fr = frames_of(raw, target)
    one = dict(fr, MSFT=fr["MSFT"][fr["MSFT"].index <= "2026-11-12"])
    two = dict(fr, MSFT=fr["MSFT"][fr["MSFT"].index <= "2026-11-11"])
    assert hc.check_freshness(one, target, CAL, grace=True).status == hc.WARN
    c = hc.check_freshness(two, target, CAL, grace=True)           # a day older: fails on every run
    assert c.status == hc.FAIL and "2 sessions behind" in c.items[0]
    assert hc.check_freshness(fr, target, CAL, data_end="2026-11-11", grace=True).status == hc.FAIL
    # every series stops at the previous session on the last run: the closure hint
    allst = {s: f[f.index <= "2026-11-12"] for s, f in fr.items()}
    c = hc.check_freshness(allst, target, CAL)
    assert c.status == hc.FAIL and any("EXTRA_CLOSURES" in x for x in c.items)


def test_freshness_of_held_shorts_uses_the_second_vendor(raw):
    target = pd.Timestamp("2026-11-13")
    fr = frames_of(raw, target)
    a = fr["AAPL"]
    one, six = a[a.index <= "2026-11-12"], a[a.index <= "2026-11-05"]
    d = pd.Timestamp
    # the second vendor has a later bar: the stored vendor is behind (fails even when long behind)
    assert hc.check_freshness(fr, target, CAL, held={"X": one}, held_other={"X": (d("2026-11-13"), False)}).status \
        == hc.FAIL
    assert hc.check_freshness(fr, target, CAL, held={"X": six}, held_other={"X": (d("2026-11-13"), False)}).status \
        == hc.FAIL
    # both vendors stop at the same day, or the second has no data (HTTP 404): delisted or halted, a warning
    c = hc.check_freshness(fr, target, CAL, held={"X": one}, held_other={"X": (d("2026-11-12"), False)})
    assert c.status == hc.WARN and "delisted or halted" in c.items[0]
    assert hc.check_freshness(fr, target, CAL, held={"X": one}, held_other={"X": (None, True)}).status == hc.WARN
    # no second vendor: as before (1-2 sessions behind fails)
    assert hc.check_freshness(fr, target, CAL, held={"X": one}).status == hc.FAIL
    # the EFFR: warn when 4-6 sessions behind, fail beyond
    assert hc.check_freshness(fr, target, CAL, effr_last="2026-11-09").status == hc.WARN
    assert hc.check_freshness(fr, target, CAL, effr_last="2026-11-04").status == hc.FAIL


def test_integrity_total_return_consistency_history_and_overlap(tmp_path):
    target = pd.Timestamp("2026-12-31")
    clean = {s: hc.chart_frame(payload(s, *SPEC[s]), target) for s in SPEC}
    assert hc.check_integrity(clean, target, CAL, history=True).status == hc.OK

    def phantom_div(p):                               # a 3% dividend no adjusted close reflects
        j = p["chart"]["result"][0]
        t = int(pd.Timestamp("2026-11-18", tz="America/New_York").replace(hour=9, minute=30).timestamp())
        k = j["timestamp"].index(t)
        j["events"]["dividends"][str(t)] = {"date": t, "amount": 0.03 * j["indicators"]["quote"][0]["close"][k]}
        return p
    bad = dict(clean, AAPL=hc.chart_frame(phantom_div(payload("AAPL", *SPEC["AAPL"])), target))
    c = hc.check_integrity(bad, target, CAL)
    assert c.status == hc.FAIL and any("adjusted-close return differ" in x for x in c.items)
    # a session missing from the history before the window (QQQ feeds the RSI2 percentile from 1999)
    old = dict(clean, QQQ=hc.chart_frame(drop_day(payload("QQQ", *SPEC["QQQ"]), "2025-03-10"), target))
    assert hc.check_integrity(old, target, CAL).status == hc.OK                     # the window alone
    c = hc.check_integrity(old, target, CAL, history=True)
    assert c.status == hc.FAIL and "history before" in c.items[0] and "2025-03-10" in c.items[0]
    # the fetched tail disagreed with the committed history state
    c = hc.check_integrity(clean, target, CAL, overlap={"QQQ": {"overlap_ok": False,
                                                                "overlap_max_abs_close_ratio_diff": 0.01}})
    assert c.status == hc.FAIL and "history state" in c.items[0]
    c = hc.check_integrity(clean, target, CAL, closures=[pd.Timestamp("2026-11-18")])
    assert c.status == hc.WARN and "unscheduled closure" in c.items[0]


def test_unscheduled_closure(monkeypatch):
    target = pd.Timestamp("2026-12-31")
    gone = {s: hc.chart_frame(drop_day(payload(s, *SPEC[s]), "2026-11-18"), target) for s in SPEC}
    assert hc.unscheduled_closures(gone, {}, CAL, "2026-08-28", target) == [pd.Timestamp("2026-11-18")]
    # a second vendor has the bar: a data fault, not a closure
    other = {"alpaca": {"QQQ": hc.chart_frame(payload("QQQ", *SPEC["QQQ"]), target)}}
    assert hc.unscheduled_closures(gone, other, CAL, "2026-08-28", target) == []
    one = dict(gone, QQQ=hc.chart_frame(payload("QQQ", *SPEC["QQQ"]), target))   # one series has it
    assert hc.unscheduled_closures(one, {}, CAL, "2026-08-28", target) == []
    cal2 = CAL.difference([pd.Timestamp("2026-11-18")])
    assert hc.check_integrity(gone, target, cal2).status == hc.OK
    monkeypatch.setattr(hc, "EXTRA_CLOSURES", ("2026-11-18",))
    assert pd.Timestamp("2026-11-18") not in hc.nyse_sessions("2026-11-01", "2026-11-30")


def _scale(df, day, k, cols=("open", "high", "low", "close", "adjclose")):
    out = df.copy()
    out.loc[day, list(cols)] *= k
    return out


def test_vendor_total_returns_large_moves_and_the_whole_window(raw):
    target = pd.Timestamp("2026-11-13")
    fr = frames_of(raw, target)
    clean = {"alpaca": dict(fr)}
    # a phantom 3% dividend on the newest day (close and adjusted close agree with the second vendor)
    q = fr["QQQ"].copy()
    q.loc[target, "div"] = 0.03 * q.loc[target, "close"]
    c = hc.check_vendors(dict(fr, QQQ=q), clean, target, CAL)
    assert c.status == hc.FAIL and "QQQ 2026-11-13: stored day total return differs" in c.items[0]
    # ONEQ adjusted close 2% off on one day (the close agrees): fails; 0.7%: a warning
    assert hc.check_vendors(dict(fr, ONEQ=_scale(fr["ONEQ"], "2026-11-10", 0.98, ("adjclose",))), clean, target,
                            CAL).status == hc.FAIL
    assert hc.check_vendors(dict(fr, ONEQ=_scale(fr["ONEQ"], "2026-11-10", 0.993, ("adjclose",))), clean, target,
                            CAL).status == hc.WARN
    # an Alpaca-stored chart has no dividend events: only its adjusted close is compared
    nodiv = fr["MSFT"].copy()
    nodiv["div"] = 0.0
    assert hc.check_vendors(dict(fr, MSFT=nodiv), {"yahoo": dict(fr)}, target, CAL,
                            sources={"MSFT": "alpaca"}).status == hc.OK
    # a 40% one-day spike (within the 0.50 integrity bound) must be confirmed by a second vendor
    spiked = dict(fr, AAPL=_scale(fr["AAPL"], "2026-11-10", 1.4))
    big = hc.large_moves(spiked, target, CAL)
    assert big == {"AAPL": [pd.Timestamp("2026-11-10"), pd.Timestamp("2026-11-11")]}
    c = hc.check_vendors(spiked, {}, target, CAL, confirm=big, must=list(fr))
    assert c.status == hc.FAIL and "no second vendor confirms it" in c.items[0]
    assert hc.check_vendors(spiked, {}, target, CAL, confirm=big, must=list(fr), strict=False).status == hc.WARN
    c = hc.check_vendors(spiked, {"alpaca": dict(spiked)}, target, CAL, confirm=big)   # a real move, confirmed
    assert c.status == hc.OK and "2 large move(s) confirmed" in c.message
    assert hc.check_vendors(spiked, clean, target, CAL, confirm=big).status == hc.FAIL
    # a restated old day (outside the last 10 sessions) fails once the whole window is compared
    old = dict(fr, MSFT=_scale(fr["MSFT"], "2026-10-14", 1.01))
    assert hc.check_vendors(old, clean, target, CAL).status == hc.OK
    window = {s: list(CAL[(CAL >= "2026-10-05") & (CAL <= target)]) for s in fr}
    c = hc.check_vendors(old, clean, target, CAL, extra_days=window)
    assert c.status == hc.FAIL and c.items[0].startswith("MSFT 2026-10-14: stored close differs")


def test_signals_catch_duplicates_fill_prices_accounts_and_hmatch(raw, computed):
    fr = frames_of(raw, "2027-01-29")

    def check(res):
        return hc.check_signals(res["lines"], fr, "2027-01-29", STOCKS, universe_check=False)[0]
    res = copy.deepcopy(computed["2027-01-29"])
    res["lines"]["B1"]["trades"].append(dict(res["lines"]["B1"]["trades"][0]))     # a trade recorded twice
    c = check(res)
    assert c.status == hc.FAIL and any("twice or more" in x for x in c.items)
    res = copy.deepcopy(computed["2027-01-29"])
    res["lines"]["SEL-A"]["trades"].append(dict(res["lines"]["SEL-A"]["trades"][0]))
    assert check(res).status == hc.FAIL
    res = copy.deepcopy(computed["2027-01-29"])                                    # same dates, net +3 points
    closed = [t for t in res["lines"]["B1"]["trades"] if not t["open"]]
    closed[0]["net_pct"] += 3.0
    c = check(res)
    assert c.status == hc.FAIL and any("from the rule's fill prices" in x for x in c.items)
    res = copy.deepcopy(computed["2027-01-29"])
    res["lines"]["SEL-P"]["trades"][0]["net_pct"] -= 2.0                          # a fill 2% too low
    assert check(res).status == hc.FAIL
    res = copy.deepcopy(computed["2027-01-29"])
    res["lines"]["B1"]["n_accounts"] = 1
    c = check(res)
    assert c.status == hc.FAIL and "accounts" in c.items[0]
    res = copy.deepcopy(computed["2027-01-29"])
    hm = res["lines"]["SEL-A"]["hmatch"]
    res["lines"]["SEL-A"]["hmatch"] = hm.where(hm.index != hm.index[5], hm + 1e-6)
    c = check(res)
    assert c.status == hc.FAIL and "H_match" in c.items[0]


def test_a_tie_excuses_only_a_nearby_divergence():
    mine = [{"entry": pd.Timestamp("2026-11-02"), "exit": pd.Timestamp("2026-11-05"), "reason": "target"}]
    f, w = hc._compare("B1 X", mine, [], ["trigger at 2026-10-14"])               # an old tie: still a failure
    assert f and not w
    f, w = hc._compare("B1 X", mine, [], ["fill at 2026-11-04"])
    assert w and not f


def test_log_status_judges_b3_against_its_own_line():
    text = ("data through: 2026-12-01\n<!-- table:B3:start -->\n| month | n | status |\n|---|---|---|\n"
            "| 2026-11 | 1 | provisional |\n<!-- table:B3:end -->\n")
    assert hc.check_log_status(text, "2026-12-01").status == hc.FAIL
    assert hc.check_log_status(text, "2026-12-01", {"B3": "2026-11-30"}).status == hc.OK
    final = text.replace("| provisional |", "| final |")
    assert hc.check_log_status(final, "2026-12-01", {"B3": "2026-11-30"}).status == hc.FAIL


def _state(px, numbers, data_end="2026-12-01"):
    return {"schema": hc.SCHEMA, "data_end": data_end, "inputs": {"px": px, "b3": {}, "effr": {}},
            "numbers": {"series": numbers, "trades": {}, "b3_held": {}}}


def test_replay_ignores_fetch_noise_and_links_revisions_to_their_own_instruments(raw):
    f = frames_of(raw, "2026-11-20")
    fpf = pd.Timestamp(hc.START_CLOSE) - pd.Timedelta(days=hc.FINGERPRINT_FROM_DAYS)
    base = {s: hc.chart_fingerprints(x, fpf, "2026-11-20") for s, x in f.items()}
    # Yahoo's adjusted closes move by ~5e-7 from one fetch to the next: not a revision
    noisy = dict(f, QQQ=_scale(f["QQQ"], "2026-11-05", 1 + 5e-7, ("adjclose",)))
    now = {s: hc.chart_fingerprints(x, fpf, "2026-11-20") for s, x in noisy.items()}
    assert hc.input_changes({"px": base}, {"px": now}) == []
    revised = dict(f, QQQ=_scale(f["QQQ"], "2026-11-05", 1.004))
    now = {s: hc.chart_fingerprints(x, fpf, "2026-11-20") for s, x in revised.items()}
    ch = hc.input_changes({"px": base}, {"px": now})
    assert ("px", "QQQ", "2026-11-05") in ch
    assert hc.input_changes({"px": base}, {"px": now}, before="2026-11-05") == []   # on / after the previous end
    # a fingerprint missing now (a dropped bar) is not a revision
    gone = {s: dict(v) for s, v in base.items()}
    del gone["AAPL"]["2026-11-05"]
    assert hc.input_changes({"px": base}, {"px": gone}) == []
    # a QQQ revision explains SEL numbers, not B1 numbers
    prev = _state(base, {"B1.ret": {"2026-11-10": 0.01}, "SEL-A.ret": {"2026-11-10": 0.01}}, "2026-11-20")
    cur = {"series": {"B1.ret": {"2026-11-10": 0.0101}, "SEL-A.ret": {"2026-11-10": 0.0101}}, "trades": {},
           "b3_held": {}}
    c, _ = hc.check_replay(prev, cur, {"px": now, "b3": {}, "effr": {}}, "2026-11-23")
    assert c.status == hc.FAIL and c.items[0].startswith("B1.ret 2026-11-10")
    assert any(x.startswith("SEL-A.ret 2026-11-10") for x in c.items[1:])
    # series built on adjusted closes tolerate 2e-6
    prev = _state(base, {"SEL-A.hmatch": {"2026-10-20": 0.01}, "SEL-A.oneq": {"2026-11-10": 0.01}}, "2026-12-01")
    cur = {"series": {"SEL-A.hmatch": {"2026-10-20": 0.01 + 1e-6}, "SEL-A.oneq": {"2026-11-10": 0.01 + 1e-6}},
           "trades": {}, "b3_held": {}}
    assert hc.check_replay(prev, cur, {"px": base, "b3": {}, "effr": {}}, "2026-12-01")[0].status == hc.OK


def test_hmatch_drift_is_explained_by_a_revision_later_in_its_month():
    one = "1 1 1 1 1 0 1"
    prev = _state({"QQQ": {"2026-11-20": one}, "AAPL": {"2026-11-20": one}},
                  {"SEL-A.hmatch": {"2026-11-05": 0.01}})
    cur = {"series": {"SEL-A.hmatch": {"2026-11-05": 0.011}}, "trades": {}, "b3_held": {}}
    rev = "1.01 1 1 1 1.01 0 1"
    c, _ = hc.check_replay(prev, cur, {"px": {"QQQ": {"2026-11-20": rev}, "AAPL": {"2026-11-20": one}}},
                           "2026-12-02")
    assert c.status == hc.WARN, c.items
    c, _ = hc.check_replay(prev, cur, {"px": {"QQQ": {"2026-11-20": one}, "AAPL": {"2026-11-20": rev}}},
                           "2026-12-02")
    assert c.status == hc.FAIL                                          # AAPL does not feed SEL-A


def test_a_changed_final_log_row_fails_unless_its_inputs_were_revised():
    one, rev = "1 1 1 1 1 0 1", "1.01 1 1 1 1.01 0 1"
    prev = _state({"AAPL": {"2026-10-20": one}, "QQQ": {"2026-10-20": one}}, {}, "2026-11-20")
    cur = {"series": {}, "trades": {}, "b3_held": {}}
    w = ["B1 2026-10: a final row changed (vendor data revision?)\n  old | ... |\n  new | ... |"]
    c, _ = hc.check_replay(prev, cur, {"px": {"AAPL": {"2026-10-20": one}, "QQQ": {"2026-10-20": one}}},
                           "2026-11-23", w)
    assert c.status == hc.FAIL and "log B1 2026-10: a final row changed" in c.items[0]
    c, _ = hc.check_replay(prev, cur, {"px": {"AAPL": {"2026-10-20": rev}, "QQQ": {"2026-10-20": one}}},
                           "2026-11-23", w)
    assert c.status == hc.WARN
    c, _ = hc.check_replay(prev, cur, {"px": {"AAPL": {"2026-10-20": one}, "QQQ": {"2026-10-20": rev}}},
                           "2026-11-23", w)
    assert c.status == hc.FAIL                                          # QQQ does not feed B1's row
    old = dict(prev, schema=1)                                          # a state of the older format: re-baselined
    assert "older format" in hc.check_replay(old, cur, {}, "2026-11-23")[0].message


def test_liveness_starts_with_the_first_actions_run():
    local = {"last_run_session": "2026-11-10", "host": "local"}
    c = hc.check_liveness(local, "2026-11-13", CAL, host="actions")
    assert c.status == hc.OK and "first daily run on GitHub Actions" in c.message
    assert hc.check_liveness({"last_run_session": "2026-11-10"}, "2026-11-13", CAL, host="actions").status == hc.OK
    assert hc.check_liveness(dict(local, host="actions"), "2026-11-13", CAL, host="actions").status == hc.FAIL


def test_alpaca_requests_never_ask_for_the_last_15_minutes():
    """The free plan refuses SIP data of the last 15 minutes (HTTP 403 for any end date of today or later)."""
    from urllib.parse import parse_qs, urlparse
    from quant.observation import prices as fp
    now = utc("2026-11-03 01:30")                                      # 20:30 EST on 11-02
    urls = []

    def get(url, headers):
        urls.append(url)
        return json.dumps({"bars": {}, "next_page_token": None}).encode()
    for end in ("2026-10-30", "2026-11-02", "2026-11-03", "2026-11-10"):
        fp.alpaca_bars(["QQQ"], "2026-10-01", end, "raw", {"x": "y"}, get, sleep=lambda s: None, now=now)
    ends = [parse_qs(urlparse(u).query)["end"][0] for u in urls]
    assert ends[0] == "2026-10-30"                                     # long over: the date itself
    for e in ends[1:]:
        t = pd.Timestamp(e)
        assert t.tzinfo is not None and t <= pd.Timestamp(now) - pd.Timedelta(minutes=15), e
        assert t >= pd.Timestamp("2026-11-02 04:00", tz="UTC")         # still holds the 11-02 daily bar
    assert fp.alpaca_end("2026-10-16", utc("2026-10-19 10:37")) == "2026-10-16"
    assert fp.alpaca_end("2026-10-19", utc("2026-10-19 10:37")) == "2026-10-19T10:21:00Z"


def test_smisp_charts_fall_back_to_yahoo_when_alpaca_refuses(tmp_path, monkeypatch):
    from urllib.error import HTTPError
    from quant.observation import smisp as fs
    monkeypatch.setattr(fs.fp, "alpaca_headers", lambda: {"APCA-API-KEY-ID": "k", "APCA-API-SECRET-KEY": "s"})

    def refuse(url, headers):
        raise HTTPError(url, 403, "subscription does not permit querying recent SIP data", None, None)
    body = json.dumps(payload("AAA", 9, 0.02, sessions=CAL[-300:])).encode()
    src = fs.fetch_charts({"AAA": "AAA", "BBB": "BBB"}, tmp_path, "2026-09-01", "2026-11-03",
                          {"alpaca": refuse, "yahoo": lambda u: body}, sleep=lambda s: None)
    assert src == {"AAA": "yahoo", "BBB": "yahoo"}


def test_daily_main_writes_only_on_actions_or_with_write(monkeypatch, tmp_path):
    calls = []

    def fake(**kw):
        calls.append(kw)
        return hc.report("2026-12-31", [hc.Check("x", hc.OK, "m")])
    monkeypatch.setattr(daily, "run_daily", fake)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    daily.main([])
    assert calls[-1]["dry_run"] and calls[-1]["host"] == "local"
    daily.main(["--write"])
    assert not calls[-1]["dry_run"]
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    out = tmp_path / "r.json"
    daily.main(["--early", "--report-out", str(out)])
    assert not calls[-1]["dry_run"] and calls[-1]["host"] == "actions" and calls[-1]["early"]
    assert json.loads(out.read_text())["run"] == {"early": True, "week_last": True, "dry_run": False}


def test_daily_run_reports_a_tiingo_fallback_and_a_test_alert(b3env, tmp_path, monkeypatch):
    kw = daily_kw(b3env, tmp_path)
    monkeypatch.setattr(fo, "b_source", lambda now=None: "tiingo")
    monkeypatch.setattr(fo, "fetch", lambda syms, raw_dir=None, pacer=None: {s: "ok (yahoo)" for s in syms})
    rep = daily.run_daily(as_of="2026-12-30", fetch=True, test_alert=True, **kw)
    by = {c["name"]: c for c in rep["checks"]}
    assert by["pipeline"]["status"] == hc.WARN and "Tiingo refused or failed" in by["pipeline"]["items"][0]
    assert by["test_alert"]["status"] == hc.FAIL and rep["status"] == hc.FAIL


def test_held_second_vendor():
    f = pd.DataFrame({"close": [1.0, 1.0]}, index=pd.to_datetime(["2026-11-12", "2026-11-13"]))
    others = {"yahoo": {"A": f, "B": None}, "alpaca": {"A": f.iloc[:1]}}
    got = daily.held_second_vendor(others, {"A": "alpaca", "B": "alpaca", "C": "alpaca"})
    assert got == {"A": (pd.Timestamp("2026-11-13"), False), "B": (None, True), "C": (None, False)}


def test_vendor_early_run_leniency(raw):
    """On an early run the session just closed may still be corrected, and a large move may have no second-vendor
    bar yet: warnings there, failures on the evening's last run."""
    target = pd.Timestamp("2026-11-13")
    fr = frames_of(raw, target)
    off = dict(fr, MSFT=_scale(fr["MSFT"], target, 1.01))
    assert hc.check_vendors(off, {"alpaca": dict(fr)}, target, CAL).status == hc.FAIL
    c = hc.check_vendors(off, {"alpaca": dict(fr)}, target, CAL, strict=False)
    assert c.status == hc.WARN and "may still correct it" in c.items[0]
    old = dict(fr, MSFT=_scale(fr["MSFT"], "2026-11-10", 1.01))      # an older day: fails on every run
    assert hc.check_vendors(old, {"alpaca": dict(fr)}, target, CAL, strict=False).status == hc.FAIL
    spiked = dict(fr, AAPL=_scale(fr["AAPL"], target, 1.4, ("open", "high", "low", "close")))
    big = hc.large_moves(spiked, target, CAL)
    late = {"alpaca": {s: f[f.index < target] for s, f in spiked.items()}}   # the second vendor lacks the bar yet
    assert hc.check_vendors(spiked, late, target, CAL, confirm=big, strict=False).status == hc.WARN
    assert hc.check_vendors(spiked, late, target, CAL, confirm=big).status == hc.FAIL


def test_integrity_accepts_a_reviewed_day(tmp_path):
    target = pd.Timestamp("2026-12-31")
    clean = {s: hc.chart_frame(payload(s, *SPEC[s]), target) for s in SPEC}
    bad = dict(clean, AAPL=_scale(clean["AAPL"], "2026-11-18", 3.0))
    c = hc.check_integrity(bad, target, CAL)
    assert c.status == hc.FAIL and "AAPL 2026-11-18" in c.data["keys"]
    c = hc.check_integrity(bad, target, CAL, accepted=c.data["keys"])
    assert c.status == hc.OK and all("(accepted earlier)" in x for x in c.items)


def test_daily_run_accept_data_records_the_keys(b3env, tmp_path):
    kw = daily_kw(b3env, tmp_path)
    daily.run_daily(as_of="2026-12-30", **kw)
    raw2 = tmp_path / "raw2"
    raw2.mkdir()
    for p in Path(b3env["raw"]).iterdir():
        (raw2 / p.name).write_bytes(p.read_bytes())
    p = raw2 / f"{tfo.STOCKS[0]}.json"
    j = json.loads(p.read_text())
    r = j["chart"]["result"][0]
    k = len(r["timestamp"]) - 40
    for key in ("open", "high", "low", "close"):
        r["indicators"]["quote"][0][key][k] *= 3.0
    r["indicators"]["adjclose"][0]["adjclose"][k] *= 3.0
    p.write_text(json.dumps(j))
    kw2 = dict(kw, raw_dir=raw2)
    by = {c["name"]: c for c in daily.run_daily(as_of="2026-12-31", **kw2)["checks"]}
    assert by["data_integrity"]["status"] == hc.FAIL
    daily.run_daily(as_of="2026-12-31", accept_data=True, accept_replay=True, **kw2)
    assert json.loads((tmp_path / "replay.json").read_text())["data_accepted"]
    by = {c["name"]: c for c in daily.run_daily(as_of="2026-12-31", **kw2)["checks"]}
    assert by["data_integrity"]["status"] == hc.OK, by["data_integrity"]
