"""Development-period research (2017-01-01 .. 2022-12-31 only) for a systematic CAN SLIM (William O'Neil) strategy.

Rules studied (each letter is a switchable screen; see ``Config``):

- C: latest quarterly EPS (as known at the signal date) vs the same quarter a year earlier >= ``c_min``, with a
  positive year-ago base. Quarterly EPS comes from SEC XBRL companyfacts (diluted, then basic-and-diluted, then
  basic). A missing fiscal Q4 is derived as fiscal-year EPS minus the nine-month year-to-date EPS (or minus the
  three reported quarters).
- A: annual EPS up in each of the last 3 fiscal years with a positive base (``up3``), or a 3-year CAGR >= 25%
  (``cagr25``), or off.
- N: split-adjusted close within ``n_within`` of its 252-session high (new-high proxy).
- S: optional breakout volume: in the 5 sessions to the signal date, an up day with volume >= ``s_min`` x the
  prior 50-session average volume (split-adjusted).
- L: relative strength (total return over ``rs_lookback`` sessions skipping the last ``rs_skip``) in the top
  ``rs_top`` of the eligible universe; buys are ranked by this percentile.
- I: no institutional-ownership data; not used (top-300 dollar volume is a weak proxy for sponsorship).
- M: QQQ above its 50-day and/or 200-day simple moving average at the signal date; otherwise the sleeve is sold
  and the money sits in cash (``idle=cash``, IBKR pays no interest on the first $10,000) or in QQQ (``idle=qqq``).
  ``core`` > 0 adds a buy-and-hold QQQ core beside the CAN SLIM sleeve.

Universe: each week the top ``n_universe`` (<= 300) Nasdaq common stocks by 50-day median dollar volume with a raw
close >= $10 (``weekly_universe_top300.csv.gz``; ranks already require close >= $10), a close on the week end, not a
foreign filer (they have no 10-Q EPS and fail C anyway).

Portfolio: up to K names, each bought at sleeve NAV / K at the close of the session after the signal week end; no
rebalancing of continuing names. Sells: stop loss (close-to-close total return since entry <= -stop, sold at the next
session's close), optional profit target (same timing), fail-the-screen at a rebalance date, and M turning off.
Costs: IBKR Pro Tiered for the actual dollar size (commission $0.0035/share, min $0.35, max 1%, pass-through, SEC and
FINRA TAF on sells, half-spread by dv rank) from ``scripts/research_reversal_dev.py``.

HARD RULE: no return, price outcome or performance number dated before 2017-01-01 or after 2022-12-31 is computed.
``load_dev_data`` truncates every price/return frame to [PRICE_START, DEV_END] and asserts it; signal-only inputs
(52-week highs, relative strength, moving averages) may use 2015-10..2016 prices; the performance index uses returns
dated on or after PERF_START only (earlier returns are masked and asserted); EPS facts are used only when filed
strictly before the signal date.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import sys
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import research_reversal_dev as rev  # noqa: E402  (cost model, index, deflated Sharpe)

NORM = NormalDist()
DEV_START = "2017-01-01"
DEV_END = "2022-12-31"
PRICE_START = "2015-10-01"     # signal warm-up only (52-week high, 12-month RS, 200-day MA)
UNIVERSE_START = "2016-01-01"
FIRST_SIGNAL = "2016-12-30"    # its trades execute at the close of 2017-01-03

INPUTS = ROOT / "output/research_only/reversal_2012_2026/inputs"
CACHE = Path("/Users/bytedance/code/quant_stocks/research_cache/reversal_2012_2026")
MAIN = Path("/Users/bytedance/code/quant_stocks")
CF_DIRS = [MAIN / "research_cache/canslim_dev/sec_companyfacts",
           MAIN / "cleaned_stocks_data/financial/sec_companyfacts_cache",
           MAIN / "research_cache/holdout_2011_2019/sec_companyfacts",
           MAIN / "research_cache/sue_lt_2020_2026/sec_companyfacts",
           CACHE / "raw/sec/companyfacts"]
CF_FETCH_DIR = CF_DIRS[0]
EPS_CACHE = MAIN / "research_cache/canslim_dev"
OUT = ROOT / "output/research_only/canslim_dev_2017_2022"

EPS_CONCEPTS = ("EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted", "EarningsPerShareBasic")
QQQ_HALF_SPREAD = 0.0001
CASH_RATE = 0.0   # IBKR pays no interest on the first $10,000 of cash


# ======================================================================== the date guard

class DateGuardError(AssertionError):
    pass


def assert_window(dates, start: str | None = None, end: str = DEV_END, what: str = "dates") -> None:
    """Raise when any date is after ``end`` (or before ``start``)."""
    values = pd.to_datetime(pd.Series(list(dates)) if not isinstance(dates, (pd.Series, pd.Index)) else dates)
    values = values.dropna()
    if not len(values):
        return
    if values.max() > pd.Timestamp(end):
        raise DateGuardError(f"{what}: {values.max().date()} is after {end}")
    if start is not None and values.min() < pd.Timestamp(start):
        raise DateGuardError(f"{what}: {values.min().date()} is before {start}")


def truncate(frame: pd.DataFrame, column: str, start: str, end: str = DEV_END) -> pd.DataFrame:
    s = frame[column].astype(str)
    out = frame.loc[(s >= start) & (s <= end)].copy()
    assert_window(out[column], start, end, column)
    return out


# ======================================================================== SEC companyfacts and point-in-time EPS

def _cf_path(cik: int) -> Path | None:
    for d in CF_DIRS:
        p = d / f"CIK{int(cik):010d}.json.gz"
        if p.is_file():
            return p
    return None


def fetch_companyfacts(ciks, max_per_second: float = 3.0) -> dict:
    """Fetch SEC companyfacts for CIKs not cached anywhere (<= 3 requests/s, User-Agent from sec_contact)."""
    import requests
    from src.io.sec_contact import sec_user_agent
    CF_FETCH_DIR.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": sec_user_agent(ROOT), "Accept-Encoding": "gzip, deflate"}
    missing = [c for c in sorted(set(int(c) for c in ciks)) if _cf_path(c) is None]
    log = {"requested": len(missing), "ok": 0, "not_found": [], "error": {}}
    last = 0.0
    for c in missing:
        wait = 1.0 / max_per_second - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        last = time.time()
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{c:010d}.json"
        try:
            r = requests.get(url, headers=headers, timeout=60)
        except Exception as exc:  # noqa: BLE001
            log["error"][c] = str(exc)[:200]
            continue
        if r.status_code == 404:
            log["not_found"].append(c)
            continue
        if r.status_code != 200:
            log["error"][c] = f"http {r.status_code}"
            continue
        env = {"cik": c, "fetched_at": pd.Timestamp.now("UTC").isoformat(), "source_url": url, "payload": r.json()}
        (CF_FETCH_DIR / f"CIK{c:010d}.json.gz").write_bytes(gzip.compress(json.dumps(env).encode(), 6))
        log["ok"] += 1
    return log


def extract_eps_facts(cik: int) -> pd.DataFrame:
    """All EPS facts (USD/shares) of one CIK: start, end, val, filed, form, priority (0 = diluted)."""
    p = _cf_path(cik)
    cols = ["cik", "concept", "priority", "start", "end", "val", "filed", "form", "accn"]
    if p is None:
        return pd.DataFrame(columns=cols)
    env = json.loads(gzip.decompress(p.read_bytes()))
    payload = env.get("payload", env)
    gaap = payload.get("facts", {}).get("us-gaap", {})
    rows = []
    for pri, concept in enumerate(EPS_CONCEPTS):
        for unit, facts in gaap.get(concept, {}).get("units", {}).items():
            if unit != "USD/shares":
                continue
            for f in facts:
                if "start" not in f or f.get("val") is None:
                    continue
                rows.append((int(cik), concept, pri, f["start"], f["end"], float(f["val"]), f["filed"],
                             f.get("form", ""), f.get("accn", "")))
    return pd.DataFrame(rows, columns=cols)


def _days(a: str, b: str) -> int:
    return (pd.Timestamp(b) - pd.Timestamp(a)).days


def eps_states(facts: pd.DataFrame, d0_by_qend: dict | None = None) -> pd.DataFrame:
    """Point-in-time EPS state of one company after each filing date.

    At filing date f only facts with ``filed <= f`` are known; for each period the most recently filed value wins
    (so later restatements, e.g. for stock splits, replace earlier ones from their filing date on). The state is
    usable for signal dates strictly after ``avail`` (= f, or the earnings-release session for a new quarter when
    ``d0_by_qend`` is given and the release came first).
    """
    out_cols = ["cik", "filed", "avail", "q_end", "q_eps", "q_ya_end", "q_ya_eps", "c_growth", "q_derived",
                "fy0_end", "fy0", "fy1", "fy2", "fy3", "a_up3", "a_cagr3"]
    if facts.empty:
        return pd.DataFrame(columns=out_cols)
    f = facts.copy()
    f["dur"] = (pd.to_datetime(f["end"]) - pd.to_datetime(f["start"])).dt.days
    f["cls"] = np.select([f["dur"].between(75, 105), f["dur"].between(250, 290), f["dur"].between(340, 390)],
                         ["Q", "Y9", "FY"], "")
    f = f[f["cls"] != ""].sort_values(["filed", "priority"], ascending=[True, False])
    if f.empty:
        return pd.DataFrame(columns=out_cols)
    cik = int(f["cik"].iloc[0])
    known: dict = {}          # (cls, end) -> (start, val)
    rows = []
    prev_qend, prev_avail = None, None
    for filed, g in f.groupby("filed", sort=True):
        for r in g.itertuples():          # priority descending inside a date, so diluted (0) is written last
            known[(r.cls, r.end)] = (r.start, r.val)
        q = {e: v for (c, e), (s, v) in known.items() if c == "Q"}
        derived = set()
        fys = sorted(((e, s, v) for (c, e), (s, v) in known.items() if c == "FY"), reverse=True)
        for e, s, v in fys:
            if any(abs(_days(e, qe)) <= 7 for qe in q):
                continue
            y9 = [(ye, yv) for (c, ye), (ys, yv) in known.items()
                  if c == "Y9" and abs(_days(ys, s)) <= 10 and 80 <= _days(ye, e) <= 105]
            if y9:
                q[e] = v - y9[0][1]
                derived.add(e)
                continue
            three = [qv for qe, qv in q.items() if qe not in derived and _days(s, qe) >= 75 and _days(qe, e) >= 75]
            if len(three) == 3:
                q[e] = v - sum(three)
                derived.add(e)
        state = {"cik": cik, "filed": filed}
        if q:
            qe = max(q)
            ya = [e for e in q if 350 <= _days(e, qe) <= 380]
            state.update(q_end=qe, q_eps=q[qe], q_derived=qe in derived)
            if ya:
                ye = max(ya)
                state.update(q_ya_end=ye, q_ya_eps=q[ye])
                state["c_growth"] = (q[qe] - q[ye]) / abs(q[ye]) if q[ye] > 0 else np.nan
        chain = []
        for e, s, v in fys:
            if not chain or 350 <= _days(e, chain[-1][0]) <= 380:
                chain.append((e, v))
            if len(chain) == 4:
                break
        if chain:
            state["fy0_end"] = chain[0][0]
            for i, (e, v) in enumerate(chain):
                state[f"fy{i}"] = v
        if len(chain) == 4:
            v = [c[1] for c in chain]
            state["a_up3"] = bool(v[3] > 0 and v[2] > v[3] and v[1] > v[2] and v[0] > v[1])
            state["a_cagr3"] = (v[0] / v[3]) ** (1 / 3) - 1 if v[3] > 0 and v[0] > 0 else np.nan
        avail = filed
        qe = state.get("q_end")
        if d0_by_qend and qe is not None and qe != prev_qend:
            for k_end, d0 in d0_by_qend.items():
                if abs(_days(k_end, qe)) <= 10 and qe < d0 < filed:
                    avail = d0
                    break
        if prev_avail is not None and avail < prev_avail:
            avail = prev_avail
        state["avail"] = avail
        prev_qend, prev_avail = qe, avail
        rows.append(state)
    out = pd.DataFrame(rows)
    for c in out_cols:
        if c not in out:
            out[c] = np.nan
    return out[out_cols]


def eps_asof(states: pd.DataFrame, cik: int, t: str) -> dict | None:
    """The state of one CIK usable at signal date t: latest with avail < t (strictly)."""
    s = states[(states["cik"] == cik) & (states["avail"].astype(str) < str(t))]
    if s.empty:
        return None
    return s.sort_values(["avail", "filed"]).iloc[-1].to_dict()


def build_eps_states(ciks, timing: str = "filed") -> pd.DataFrame:
    cache = EPS_CACHE / f"eps_states_{timing}.csv.gz"
    if cache.is_file():
        return pd.read_csv(cache, dtype={"filed": str, "avail": str, "q_end": str, "q_ya_end": str, "fy0_end": str})
    d0 = {}
    if timing == "d0":
        ev = pd.read_csv(INPUTS / "earnings_events.csv", dtype=str,
                         usecols=["cik", "event_kind", "d0_session", "fiscal_quarter_end"])
        ev = ev[(ev["event_kind"] == "results_release") & ev["fiscal_quarter_end"].notna() & ev["d0_session"].notna()]
        for c, g in ev.groupby("cik"):
            d0[int(c)] = dict(zip(g["fiscal_quarter_end"], g["d0_session"]))
    parts = []
    for c in sorted(set(int(x) for x in ciks)):
        parts.append(eps_states(extract_eps_facts(c), d0.get(c) if timing == "d0" else None))
    out = pd.concat([p for p in parts if len(p)], ignore_index=True)
    EPS_CACHE.mkdir(parents=True, exist_ok=True)
    out.to_csv(cache, index=False)
    return out


# ======================================================================== the single loader

@dataclass
class DevData:
    sessions: pd.DatetimeIndex        # PRICE_START .. DEV_END
    universe: pd.DataFrame
    sig_idx: pd.DataFrame             # total-return index for signals (warm-up included)
    perf_idx: pd.DataFrame            # total-return index from PERF_START only (earlier returns masked)
    close: pd.DataFrame               # raw close, forward filled (order sizing only)
    close_adj: pd.DataFrame           # split-adjusted close (signals)
    vol_adj: pd.DataFrame             # split-adjusted volume (signals)
    last_row: pd.Series
    qqq_close: pd.Series
    qqq_perf_idx: pd.Series
    terminal_events: pd.DataFrame
    guard: dict


def load_dev_data(terminal_awaiting: float = rev.TERMINAL_D5) -> DevData:
    guard = {"dev_start": DEV_START, "dev_end": DEV_END, "price_start_signals_only": PRICE_START, "frames": {}}

    def note(name, frame, column, start):
        out = truncate(frame, column, start)
        guard["frames"][name] = {"rows_kept": int(len(out)),
                                 "rows_dropped_after_end": int((frame[column].astype(str) > DEV_END).sum()),
                                 "rows_dropped_before_start": int((frame[column].astype(str) < start).sum()),
                                 "min_date": str(out[column].min()), "max_date": str(out[column].max())}
        return out

    uni = pd.read_csv(INPUTS / "weekly_universe_top300.csv.gz", dtype=str)
    uni = note("weekly_universe_top300", uni, "week_end", UNIVERSE_START)
    uni["dv50_rank"] = pd.to_numeric(uni["dv50_rank"], errors="coerce")
    uni["week_end"] = pd.to_datetime(uni["week_end"])

    panel = pd.read_csv(CACHE / "prices/daily_panel.csv.gz", dtype={"security_id": str, "date": str},
                        usecols=["security_id", "date", "close_raw", "volume_raw", "split_factor", "tr"])
    qqq = pd.read_csv(CACHE / "factors/qqq_joined.csv", dtype={"date": str})
    term = pd.read_csv(INPUTS / "terminal_returns_2012_2026.csv", dtype=str)
    term = term[term["last_price_date"].notna()]
    term = note("terminal_returns (by last_price_date)", term, "last_price_date", PRICE_START)

    ids = set(uni["security_id"])
    successor = {r.security_id: r.continued_as for r in term.itertuples() if isinstance(r.continued_as, str)}
    ids |= {successor[s] for s in list(ids) if s in successor}
    panel = panel[panel["security_id"].isin(ids)]
    panel = note("daily_panel", panel, "date", PRICE_START)
    qqq = note("qqq_joined", qqq, "date", PRICE_START).sort_values("date")
    sessions = pd.DatetimeIndex(pd.to_datetime(qqq["date"]))
    qqq_close = pd.Series(qqq["close"].to_numpy(float), index=sessions)
    qqq_tr = pd.Series(((qqq["close"] + qqq["dividend"].fillna(0)) / qqq["close"].shift(1) - 1).to_numpy(), index=sessions)

    panel["date"] = pd.to_datetime(panel["date"])
    piv = {c: panel.pivot(index="date", columns="security_id", values=c).reindex(sessions)
           for c in ("tr", "close_raw", "volume_raw", "split_factor")}
    tr, close, vol, split = piv["tr"], piv["close_raw"], piv["volume_raw"], piv["split_factor"].fillna(1.0)
    has_row = close.notna()
    last_row = has_row[::-1].idxmax()
    last_row[~has_row.any()] = pd.NaT
    for pred, succ in successor.items():
        if pred not in tr.columns or succ not in tr.columns or pd.isna(last_row.get(pred)):
            continue
        after = tr.index > last_row[pred]
        for fr in (tr, close, vol, split):
            fr.loc[after, pred] = fr.loc[after, succ]
        last_row[pred] = last_row[succ]

    events = []
    last_session = sessions[-1]
    term_by_id = {r.security_id: r for r in term.itertuples()}
    for sid in tr.columns:
        lr = last_row[sid]
        if pd.isna(lr) or lr >= last_session:
            continue
        nxt = sessions[sessions > lr][0]
        r = term_by_id.get(sid)
        status = r.status if r is not None else "no_terminal_record"
        if status == "computed":
            value = float(r.terminal_return)
        elif status == "awaiting_d5":
            value = terminal_awaiting
        else:
            value = 0.0
        tr.loc[nxt, sid] = value
        events.append({"security_id": sid, "ticker": getattr(r, "ticker", ""), "last_row": lr.date().isoformat(),
                       "booked_on": nxt.date().isoformat(), "status": status, "terminal_return": value})
    events = pd.DataFrame(events)

    cum_split = split.cumprod()
    close_adj = close * cum_split
    vol_adj = vol / cum_split
    sig_idx = rev.make_index(tr, close)
    perf_start = sessions[sessions >= pd.Timestamp(DEV_START)][0]
    tr_perf = tr.copy()
    tr_perf.loc[tr_perf.index <= perf_start] = np.nan       # positions start at the close of the first 2017 session
    started = close.notna().mul(np.asarray(close.index >= perf_start), axis=0).cumsum() > 0
    perf_idx = (1 + tr_perf.fillna(0.0)).cumprod().where(started)
    qtr = qqq_tr.copy()
    qtr[qtr.index <= perf_start] = np.nan
    qqq_perf_idx = (1 + qtr.fillna(0.0)).cumprod()
    assert_window(tr_perf.dropna(how="all").index, perf_start.strftime("%Y-%m-%d"), DEV_END, "performance returns")
    for name, fr in (("tr", tr), ("close", close), ("volume", vol), ("qqq", qqq_close)):
        assert_window(fr.index, PRICE_START, DEV_END, name)
    guard["perf_start_session"] = str(perf_start.date())
    guard["max_session_loaded"] = str(sessions.max().date())
    guard["assertion"] = (f"PASS: every price/return frame lies in [{PRICE_START}, {DEV_END}]; performance returns are "
                          f"masked before {perf_start.date()} (first return used: the session after it)")
    return DevData(sessions=sessions, universe=uni, sig_idx=sig_idx, perf_idx=perf_idx, close=close.ffill(),
                   close_adj=close_adj, vol_adj=vol_adj, last_row=last_row, qqq_close=qqq_close,
                   qqq_perf_idx=qqq_perf_idx, terminal_events=events, guard=guard)


# ======================================================================== signals

def price_features(close_adj: pd.DataFrame, vol_adj: pd.DataFrame, sig_idx: pd.DataFrame) -> dict:
    """Wide signal frames (sessions x securities)."""
    high = close_adj.rolling(252, min_periods=200).max()
    avg50 = vol_adj.rolling(50, min_periods=40).mean().shift(1)
    up = close_adj > close_adj.shift(1)
    ratio = (vol_adj / avg50).where(up)
    out = {"off_high": 1 - close_adj / high, "vol_ratio5": ratio.rolling(5, min_periods=1).max()}
    for lb, sk in ((252, 21), (126, 21), (252, 0)):
        out[f"rs_{lb}_{sk}"] = sig_idx.shift(sk) / sig_idx.shift(lb) - 1
    return out


def market_filter(qqq_close: pd.Series) -> pd.DataFrame:
    return pd.DataFrame({"sma50": qqq_close > qqq_close.rolling(50).mean(),
                         "sma200": qqq_close > qqq_close.rolling(200).mean()})


def attach_eps(feat: pd.DataFrame, states: pd.DataFrame, prefix: str = "") -> pd.DataFrame:
    """As-of merge: for each (week_end, cik) the latest state with avail < week_end."""
    st = states.copy()
    st["avail_ts"] = pd.to_datetime(st["avail"]) + pd.Timedelta(days=1)    # usable strictly after avail
    st = st.sort_values(["avail_ts", "filed"])
    left = feat.copy()
    left["cik_i"] = pd.to_numeric(left["cik"], errors="coerce")
    left = left[left["cik_i"].notna()].copy()
    left["cik_i"] = left["cik_i"].astype(int)
    st["cik_i"] = st["cik"].astype(int)
    left = left.sort_values("week_end")
    cols = ["q_end", "q_eps", "q_ya_eps", "c_growth", "fy0_end", "a_up3", "a_cagr3", "avail", "filed"]
    m = pd.merge_asof(left, st[["avail_ts", "cik_i"] + cols].rename(columns={c: prefix + c for c in cols}),
                      left_on="week_end", right_on="avail_ts", by="cik_i", direction="backward")
    out = feat.merge(m[["week_end", "security_id"] + [prefix + c for c in cols]], on=["week_end", "security_id"],
                     how="left")
    return out


def build_features(data: DevData, states: dict) -> pd.DataFrame:
    pf = price_features(data.close_adj, data.vol_adj, data.sig_idx)
    uni = data.universe
    ok = (uni["price_ge_10"] == "Y") & (uni["close_on_week_end"] == "Y") & (uni["foreign_filer"] != "Y") & \
        uni["dv50_rank"].notna() & uni["security_id"].isin(data.close_adj.columns) & \
        (uni["week_end"] >= pd.Timestamp(FIRST_SIGNAL))
    u = uni.loc[ok, ["week_end", "security_id", "ticker", "cik", "dv50_rank"]].copy()
    sess = data.sessions
    pos = sess.searchsorted(u["week_end"], side="right") - 1
    u["session"] = sess[pos]
    assert (u["session"] <= u["week_end"]).all()
    cols = {sid: i for i, sid in enumerate(data.close_adj.columns)}
    ci = u["security_id"].map(cols).to_numpy()
    for name, fr in pf.items():
        u[name] = fr.to_numpy()[pos, ci]
    feat = attach_eps(u, states["filed"])
    if "d0" in states:
        feat = attach_eps(feat, states["d0"], prefix="d0_")
    return feat


@dataclass(frozen=True)
class Config:
    n_universe: int = 300
    c_min: float | None = 0.25
    a_rule: str = "up3"            # up3 | cagr25 | off
    n_within: float | None = 0.15
    rs_lookback: int = 252
    rs_skip: int = 21
    rs_top: float | None = 0.20
    s_min: float | None = None
    m_rule: str = "sma50"          # none | sma50 | sma200 | sma50_200
    idle: str = "cash"             # cash | qqq
    core: float = 0.0
    k: int = 8
    rebalance: str = "weekly"      # weekly | monthly
    stop: float | None = 0.08
    profit: float | None = None
    exit_on_fail: bool = True
    eps_timing: str = "filed"      # filed | d0
    spread_mult: float = 1.0
    account: float = 10_000.0

    @property
    def name(self) -> str:
        f = lambda x: "off" if x is None else f"{int(round(x * 100))}"  # noqa: E731
        parts = [f"U{self.n_universe}", f"C{f(self.c_min)}", f"A{self.a_rule}", f"N{f(self.n_within)}",
                 f"L{self.rs_lookback}s{self.rs_skip}t{f(self.rs_top)}", f"S{'off' if self.s_min is None else self.s_min}",
                 f"M{self.m_rule}", f"idle{self.idle}", f"core{f(self.core)}", f"K{self.k}", self.rebalance[0].upper(),
                 f"stop{f(self.stop)}", f"pt{f(self.profit)}", "xfail" if self.exit_on_fail else "xhold"]
        if self.eps_timing != "filed":
            parts.append(f"eps{self.eps_timing}")
        return "_".join(parts)


def screen(feat: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Rows of ``feat`` passing the screen, with the RS percentile, sorted by week then RS descending."""
    g = feat[feat["dv50_rank"] <= cfg.n_universe].copy()
    rs = g[f"rs_{cfg.rs_lookback}_{cfg.rs_skip}"]
    g = g[rs.notna()].copy()
    g["rs_pct"] = g.groupby("week_end")[f"rs_{cfg.rs_lookback}_{cfg.rs_skip}"].rank(pct=True)
    p = "d0_" if cfg.eps_timing == "d0" else ""
    keep = pd.Series(True, index=g.index)
    stale_q = (g["week_end"] - pd.to_datetime(g[p + "q_end"])).dt.days
    stale_y = (g["week_end"] - pd.to_datetime(g[p + "fy0_end"])).dt.days
    if cfg.c_min is not None:
        keep &= (g[p + "c_growth"] >= cfg.c_min) & (stale_q <= 200)
    if cfg.a_rule == "up3":
        keep &= (g[p + "a_up3"].astype(str) == "True") & (stale_y <= 550)
    elif cfg.a_rule == "cagr25":
        keep &= (g[p + "a_cagr3"] >= 0.25) & (stale_y <= 550)
    if cfg.n_within is not None:
        keep &= g["off_high"] <= cfg.n_within
    if cfg.rs_top is not None:
        keep &= g["rs_pct"] > 1 - cfg.rs_top + 1e-9   # top share by rank / n
    if cfg.s_min is not None:
        keep &= g["vol_ratio5"] >= cfg.s_min
    out = g[keep]
    return out.sort_values(["week_end", "rs_pct", "security_id"], ascending=[True, False, True])


def signal_schedule(feat_weeks, sessions: pd.DatetimeIndex, m_on: pd.Series, rebalance: str) -> list:
    """(week_end t, execution session e, is_rebalance, m_on) for each week; e is the next session after t."""
    weeks = sorted(pd.to_datetime(pd.Series(list(feat_weeks)).unique()))
    out = []
    for i, t in enumerate(weeks):
        later = sessions[sessions > t]
        if not len(later):
            continue
        e = later[0]
        if e < pd.Timestamp(DEV_START):
            continue
        if rebalance == "weekly":
            reb = True
        else:
            reb = i == len(weeks) - 1 or weeks[i + 1].month != t.month
        sig_session = sessions[sessions <= t][-1]
        out.append((t, e, reb, bool(m_on.loc[sig_session])))
    assert_window([e for _, e, _, _ in out], DEV_START, DEV_END, "execution sessions")
    return out


# ======================================================================== the portfolio engine

def order_cost(value: float, price: float, sell: bool, hs: float) -> float:
    if value <= 0:
        return 0.0
    return rev.order_cost(value / price, price, sell=sell, hs=hs)["total"]


def simulate(cfg: Config, sessions: pd.DatetimeIndex, idx: pd.DataFrame, close: pd.DataFrame, last_row: pd.Series,
             qqq_idx: pd.Series, qqq_close: pd.Series, sched: list, picks: dict, rank_of: dict,
             account: float) -> dict:
    """Daily simulation of the CAN SLIM sleeve with ``account`` dollars.

    ``picks``: week_end -> list of passing security ids, best first. ``rank_of``: week_end -> {sid: dv50 rank}.
    Positions are held as units of the total-return index (value = units x idx). Returns daily NAV etc.
    """
    perf = sessions[(sessions >= pd.Timestamp(DEV_START)) & (sessions <= pd.Timestamp(DEV_END))]
    col = {s: i for i, s in enumerate(idx.columns)}
    I = idx.reindex(perf).to_numpy()
    P = close.reindex(perf).to_numpy()
    Q = qqq_idx.reindex(perf).to_numpy()
    QP = qqq_close.reindex(perf).to_numpy()
    lr = last_row.reindex(idx.columns)
    exec_map = {e: (t, reb, m) for t, e, reb, m in sched}
    cash, qunits = account, 0.0
    pos: dict = {}          # sid -> [units, entry_day_i, entry_idx]
    pending: set = set()
    navs, expo, costs = np.zeros(len(perf)), np.zeros(len(perf)), np.zeros(len(perf))
    traded = 0.0
    n_buy = n_sell = n_stop = n_profit = n_fail = n_m = n_qqq = 0
    trades = []
    last_week = None
    first_day = True

    def hs(sid, price):
        r = rank_of.get(last_week, {}).get(sid, np.nan) if last_week is not None else np.nan
        return rev.half_spread(r, price, cfg.spread_mult)

    for i, d in enumerate(perf):
        day_cost = 0.0
        if first_day:
            first_day = False
        # delisted / ended series: value (terminal return booked) moves to cash, no order
        for sid in [s for s in pos if pd.notna(lr[s]) and d > lr[s]]:
            u, ei, eidx = pos.pop(sid)
            val = u * I[i, col[sid]]
            cash += val
            trades.append({"sid": sid, "exit": d, "reason": "delisted", "ret": I[i, col[sid]] / eidx - 1})
            pending.discard(sid)

        def sell(sid, reason):
            nonlocal cash, traded, day_cost, n_sell
            u, ei, eidx = pos.pop(sid)
            c = col[sid]
            val = u * I[i, c]
            k = order_cost(val, P[i, c], True, hs(sid, P[i, c]))
            cash += val - k
            day_cost += k
            traded += val
            n_sell += 1
            trades.append({"sid": sid, "exit": d, "reason": reason, "ret": I[i, c] / eidx - 1, "days": i - ei})

        for sid in sorted(pending):
            if sid in pos:
                reason = "stop" if I[i, col[sid]] / pos[sid][2] - 1 < 0 else "profit"
                sell(sid, "pending_" + reason)
        pending = set()
        stock_val = sum(u * I[i, col[s]] for s, (u, _, _) in pos.items())
        touched = False
        if d in exec_map:
            t, reb, m_ok = exec_map[d]
            last_week = t
            if not m_ok:
                for sid in sorted(pos):
                    sell(sid, "market_off")
                    n_m += 1
                touched = True
            elif reb:
                passing = picks.get(t, [])
                pset = set(passing)
                if cfg.exit_on_fail:
                    for sid in sorted(pos):
                        if sid not in pset:
                            sell(sid, "fail_screen")
                            n_fail += 1
                stock_val = sum(u * I[i, col[s]] for s, (u, _, _) in pos.items())
                nav = cash + qunits * Q[i] + stock_val
                size = nav / cfg.k
                avail = cash + (qunits * Q[i] if cfg.idle == "qqq" else 0.0)
                for sid in passing:
                    if len(pos) >= cfg.k:
                        break
                    c = col.get(sid)
                    if sid in pos or c is None or not np.isfinite(I[i, c]) or not np.isfinite(P[i, c]):
                        continue
                    if pd.notna(lr[sid]) and lr[sid] < d:      # series already ended (no look-ahead on future delistings)
                        continue
                    amt = min(size, avail)
                    if amt < 0.2 * size or amt < 100:
                        break
                    k = order_cost(amt, P[i, c], False, hs(sid, P[i, c]))
                    pos[sid] = [(amt - k) / I[i, c], i, I[i, c]]
                    cash -= amt
                    avail -= amt
                    day_cost += k
                    traded += amt
                    n_buy += 1
                touched = True
        if cfg.idle == "qqq" and (touched or cash > 0.02 * (cash + qunits * Q[i] + 1e-9)):
            stock_val = sum(u * I[i, col[s]] for s, (u, _, _) in pos.items())
            qv = qunits * Q[i]
            nav = cash + qv + stock_val
            target = max(cash + qv, 0.0)
            delta = target - qv
            if abs(delta) > 0.02 * nav or (target == 0 and qv > 0):
                k = order_cost(abs(delta), QP[i], delta < 0, max(QQQ_HALF_SPREAD, 0.005 / QP[i]))
                qunits += delta / Q[i]
                cash -= delta + k
                day_cost += k
                traded += abs(delta)
                n_qqq += 1
        # stop / profit triggers at today's close -> sell at the next session's close
        for sid, (u, ei, eidx) in pos.items():
            if ei == i:
                continue
            r = I[i, col[sid]] / eidx - 1
            if cfg.stop is not None and r <= -cfg.stop:
                pending.add(sid)
                n_stop += 1
            elif cfg.profit is not None and r >= cfg.profit:
                pending.add(sid)
                n_profit += 1
        stock_val = sum(u * I[i, col[s]] for s, (u, _, _) in pos.items())
        cash *= 1 + CASH_RATE / 252
        navs[i] = cash + qunits * Q[i] + stock_val
        expo[i] = stock_val / navs[i] if navs[i] > 0 else 0.0
        costs[i] = day_cost
    return {"dates": perf, "nav": pd.Series(navs, index=perf), "exposure": pd.Series(expo, index=perf),
            "cost": pd.Series(costs, index=perf), "traded": traded, "n_buy": n_buy, "n_sell": n_sell,
            "n_stop": n_stop, "n_profit": n_profit, "n_fail": n_fail, "n_market_off": n_m, "n_qqq_orders": n_qqq,
            "trades": pd.DataFrame(trades)}


# ======================================================================== metrics

def weekly(series: pd.Series) -> pd.Series:
    return series.resample("W-FRI").last().dropna()


def perf_metrics(nav: pd.Series, bench: pd.Series, account: float, cost: pd.Series, traded: float,
                 exposure: pd.Series) -> dict:
    assert_window(nav.index, DEV_START, DEV_END, "performance NAV")
    r = nav.pct_change()
    r.iloc[0] = nav.iloc[0] / account - 1
    b = bench.pct_change()
    b.iloc[0] = bench.iloc[0] / account - 1
    years = (nav.index[-1] - pd.Timestamp("2016-12-30")).days / 365.25
    cagr = (nav.iloc[-1] / account) ** (1 / years) - 1
    bcagr = (bench.iloc[-1] / account) ** (1 / years) - 1
    a = r - b
    wn = weekly(pd.concat([pd.Series([account], index=[pd.Timestamp("2016-12-30")]), nav]))
    wb = weekly(pd.concat([pd.Series([account], index=[pd.Timestamp("2016-12-30")]), bench]))
    wa = (wn.pct_change() - wb.pct_change()).dropna()
    sd = a.std(ddof=1)
    out = {
        "cagr": cagr, "qqq_cagr": bcagr, "excess_cagr": cagr - bcagr,
        "vol": r.std(ddof=1) * math.sqrt(252), "max_dd": rev.max_drawdown(r),
        "qqq_max_dd": rev.max_drawdown(b),
        "sharpe": r.mean() / r.std(ddof=1) * math.sqrt(252) if r.std() > 0 else np.nan,
        "qqq_sharpe": b.mean() / b.std(ddof=1) * math.sqrt(252),
        "ir": a.mean() / sd * math.sqrt(252) if sd > 0 else np.nan,
        "t_excess_daily": a.mean() / sd * math.sqrt(len(a)) if sd > 0 else np.nan,
        "t_excess_weekly": wa.mean() / wa.std(ddof=1) * math.sqrt(len(wa)) if wa.std() > 0 else np.nan,
        "ir_weekly_per_period": wa.mean() / wa.std(ddof=1) if wa.std() > 0 else np.nan,
        "weekly_active_skew": float(wa.skew()), "weekly_active_kurt": float(wa.kurt() + 3), "weeks": len(wa),
        "tracking_error": sd * math.sqrt(252),
        "turnover_one_way_per_year": traded / 2 / nav.mean() / years,
        "cost_drag_per_year": float((cost / nav.shift(1).fillna(account)).sum() / years),
        "cost_usd_total": float(cost.sum()),
        "time_in_market": float((exposure > 0.05).mean()), "avg_exposure": float(exposure.mean()),
        "active_weekly_mean_ann": wa.mean() * 52,
    }
    for n in (5, 10):
        out[f"active_ann_drop_best{n}w"] = wa.drop(wa.nlargest(n).index).mean() * 52
    by = {}
    for y in range(2017, 2023):
        m = nav.index.year == y
        if not m.any():
            continue
        prev_n = nav[nav.index.year < y].iloc[-1] if (nav.index.year < y).any() else account
        prev_b = bench[bench.index.year < y].iloc[-1] if (bench.index.year < y).any() else account
        sy = nav[m].iloc[-1] / prev_n - 1
        by_ = bench[m].iloc[-1] / prev_b - 1
        by[y] = {"strategy": round(float(sy), 4), "qqq": round(float(by_), 4), "excess": round(float(sy - by_), 4)}
    out["by_year"] = by
    out["years_beating_qqq"] = sum(v["excess"] > 0 for v in by.values())
    return out


def qqq_benchmark(qqq_idx: pd.Series, qqq_close: pd.Series, dates: pd.DatetimeIndex, account: float) -> pd.Series:
    q = qqq_idx.reindex(dates)
    k = order_cost(account, float(qqq_close.reindex(dates).iloc[0]), False, QQQ_HALF_SPREAD)
    return (account - k) * q / q.iloc[0]


# ======================================================================== grid

def build_grid() -> list[tuple[str, Config]]:
    base = Config()
    grid: list[tuple[str, Config]] = []
    seen = set()

    def add(tag, cfg):
        if cfg.name not in seen:
            seen.add(cfg.name)
            grid.append((tag, cfg))

    add("baseline", base)
    # stage A: portfolio x market factorial on the baseline screen
    for k in (5, 8, 10):
        for reb in ("weekly", "monthly"):
            for m in ("none", "sma50", "sma200"):
                for idle in ("cash", "qqq"):
                    for stop in (None, 0.08):
                        for xf in (True, False):
                            add("A_portfolio", replace(base, k=k, rebalance=reb, m_rule=m, idle=idle, stop=stop,
                                                       exit_on_fail=xf))
    # stage B: one-at-a-time screen and rule changes, on four portfolio settings (weekly / monthly x idle cash / QQQ)
    for reb, idle in (("weekly", "cash"), ("monthly", "cash"), ("weekly", "qqq"), ("monthly", "qqq")):
        b = replace(base, rebalance=reb, idle=idle)
        for ch in (dict(c_min=0.20), dict(c_min=0.30), dict(c_min=None),
                   dict(a_rule="cagr25"), dict(a_rule="off"),
                   dict(n_within=0.10), dict(n_within=0.25), dict(n_within=None),
                   dict(rs_lookback=126), dict(rs_skip=0), dict(rs_top=0.10), dict(rs_top=0.30), dict(rs_top=None),
                   dict(s_min=1.4), dict(s_min=1.5),
                   dict(stop=0.07), dict(profit=0.20), dict(profit=0.25),
                   dict(m_rule="sma50_200"), dict(core=0.5), dict(core=0.5, idle="qqq"),
                   dict(eps_timing="d0"), dict(n_universe=150)):
            add("B_one_at_a_time", replace(b, **ch))
    return grid


def run(args) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    data = load_dev_data()
    print(data.guard["assertion"])
    for name, g in data.guard["frames"].items():
        print(f"  guard {name}: kept {g['rows_kept']} rows [{g['min_date']} .. {g['max_date']}], dropped "
              f"{g['rows_dropped_after_end']} after {DEV_END} and {g['rows_dropped_before_start']} before start")
    ciks = data.universe["cik"].dropna().unique()
    if args.fetch:
        log = fetch_companyfacts(ciks)
        print("companyfacts fetch:", {k: (len(v) if isinstance(v, (list, dict)) else v) for k, v in log.items()})
    states = {"filed": build_eps_states(ciks, "filed"), "d0": build_eps_states(ciks, "d0")}
    feat = build_features(data, states)
    assert_window(feat["week_end"], FIRST_SIGNAL, DEV_END, "signal weeks")
    # point-in-time check on the built table: no EPS state used on or before its availability date
    used = feat.dropna(subset=["avail"])
    assert (pd.to_datetime(used["avail"]) < used["week_end"]).all(), "EPS look-ahead"
    used = feat.dropna(subset=["d0_avail"])
    assert (pd.to_datetime(used["d0_avail"]) < used["week_end"]).all(), "EPS look-ahead (d0)"
    print(f"point-in-time EPS check: {len(used)} (week, name) rows, every state available before its week: PASS")
    mkt = market_filter(data.qqq_close)
    m_series = {"none": pd.Series(True, index=data.sessions), "sma50": mkt["sma50"], "sma200": mkt["sma200"],
                "sma50_200": mkt["sma50"] & mkt["sma200"]}
    rank_of = {t: dict(zip(g["security_id"], g["dv50_rank"])) for t, g in data.universe.groupby("week_end")}
    weeks = sorted(feat["week_end"].unique())

    def run_cfg(cfg: Config, d: DevData = data):
        sel = screen(feat, cfg)
        picks = {t: list(g["security_id"]) for t, g in sel.groupby("week_end")}
        sched = signal_schedule(weeks, d.sessions, m_series[cfg.m_rule], cfg.rebalance)
        sleeve_acct = cfg.account * (1 - cfg.core)
        res = simulate(cfg, d.sessions, d.perf_idx, d.close, d.last_row, d.qqq_perf_idx, d.qqq_close, sched,
                       picks, rank_of, sleeve_acct)
        nav = res["nav"]
        if cfg.core > 0:
            nav = nav + qqq_benchmark(d.qqq_perf_idx, d.qqq_close, res["dates"], cfg.account * cfg.core)
        bench = qqq_benchmark(d.qqq_perf_idx, d.qqq_close, res["dates"], cfg.account)
        expo = res["exposure"] * res["nav"] / nav
        m = perf_metrics(nav, bench, cfg.account, res["cost"], res["traded"], expo)
        n_pass = sel.groupby("week_end").size().reindex(weeks).fillna(0)
        m.update({k: res[k] for k in ("n_buy", "n_sell", "n_stop", "n_profit", "n_fail", "n_market_off",
                                      "n_qqq_orders")})
        m["avg_names_passing"] = float(n_pass.mean())
        m["weeks_with_zero_passing"] = int((n_pass == 0).sum())
        tr_ = res["trades"]
        if len(tr_):
            m["win_rate_closed"] = float((tr_["ret"] > 0).mean())
            m["avg_trade_ret"] = float(tr_["ret"].mean())
        return m, res, nav, bench

    stress = load_dev_data(terminal_awaiting=rev.TERMINAL_D5_STRESS)
    grid = build_grid()
    rows, keep = [], {}
    for i, (tag, cfg) in enumerate(grid, 1):
        m, res, nav, bench = run_cfg(cfg)
        m2, *_ = run_cfg(replace(cfg, spread_mult=2.0))
        row = {"variant": i, "config": cfg.name, "tag": tag, **asdict(cfg),
               **{k: v for k, v in m.items() if k != "by_year"},
               "excess_cagr_spread2x": m2["excess_cagr"], "ir_spread2x": m2["ir"],
               "by_year_excess": json.dumps({y: v["excess"] for y, v in m["by_year"].items()}),
               "by_year_strategy": json.dumps({y: v["strategy"] for y, v in m["by_year"].items()})}
        rows.append(row)
        keep[cfg.name] = (cfg, m, nav, bench, res)
        print(f"[{i}/{len(grid)}] {cfg.name}: CAGR {m['cagr']:+.1%} ex {m['excess_cagr']:+.1%} IR {m['ir']:.2f} "
              f"t {m['t_excess_weekly']:.2f} DD {m['max_dd']:.0%} buys {m['n_buy']} cost {m['cost_drag_per_year']:.2%}",
              flush=True)
    df = pd.DataFrame(rows)
    df.to_csv(OUT / "grid.csv", index=False)

    n = len(df)
    trials = df["ir_weekly_per_period"].to_numpy()
    fields = [f for f in Config.__dataclass_fields__ if f not in ("spread_mult", "account")]

    def robustness(row, label):
        cfg, m, nav, bench, res = keep[row["config"]]
        m_stress, *_ = run_cfg(cfg, stress)
        dsr = rev.deflated_sharpe(float(row["ir_weekly_per_period"]), int(row["weeks"]),
                                  float(row["weekly_active_skew"]), float(row["weekly_active_kurt"]), trials)
        neigh = []
        for r in rows:       # parameter neighbours: variants differing in exactly one rule field
            diff = [f for f in fields if r[f] != getattr(cfg, f) and not (pd.isna(r[f]) and getattr(cfg, f) is None)]
            if len(diff) == 1:
                neigh.append({"changed": diff[0], "value": r[diff[0]], "excess_cagr": r["excess_cagr"],
                              "ir": r["ir"], "variant": r["variant"], "config": r["config"]})
        nav.to_frame("nav").assign(qqq=bench, exposure=res["exposure"]).to_csv(OUT / f"{label}_daily_nav.csv")
        res["trades"].to_csv(OUT / f"{label}_trades.csv", index=False)
        pd.DataFrame(neigh).to_csv(OUT / f"{label}_neighbours.csv", index=False)
        return {"row": {k: fmt(v) for k, v in row.items()}, "by_year": m["by_year"],
                "terminal_minus100_excess_cagr": m_stress["excess_cagr"], "neighbours": neigh,
                "neighbours_beating_qqq": sum(x["excess_cagr"] > 0 for x in neigh),
                "deflated_sharpe_active_weekly": dsr}

    fmt = lambda v: float(v) if isinstance(v, (np.floating, np.integer, float, int)) and not isinstance(v, bool) else v  # noqa: E731
    best = df.sort_values("ir", ascending=False).iloc[0]
    # post-hoc label (written after seeing the grid): a variant that buys at least 4 names a year on average is
    # "actively trading"; the pre-registered pick can be a one-time purchase held for six years
    active = df[df["n_buy"] >= 24].sort_values("ir", ascending=False).iloc[0]
    rob_best = robustness(best, "best")
    rob_active = robustness(active, "best_active")
    base_row = df[df["tag"] == "baseline"].iloc[0]
    summary = {
        "date_guard": data.guard,
        "periods": {"development": [DEV_START, DEV_END], "one_shot_check_1": ["2023-01-01", "2026-07-31"],
                    "one_shot_check_2": ["2012-01-01", "2016-12-31"],
                    "signal_warmup_prices_from": PRICE_START, "first_signal_week": FIRST_SIGNAL},
        "variants_tried": n,
        "variants_by_tag": df["tag"].value_counts().to_dict(),
        "variants_beating_qqq": int((df["excess_cagr"] > 0).sum()),
        "variants_beating_qqq_spread2x": int((df["excess_cagr_spread2x"] > 0).sum()),
        "bonferroni_t_one_sided_5pct": float(NORM.inv_cdf(1 - 0.05 / n)),
        "selection_rule": "highest net information ratio vs QQQ total return over 2017-2022 (set before running)",
        "best_by_ir_preregistered": rob_best,
        "best_actively_trading_post_hoc": rob_active,
        "baseline": {k: fmt(v) for k, v in base_row.items()},
        "qqq": {"cagr": float(best["qqq_cagr"]), "max_dd": float(best["qqq_max_dd"]), "sharpe": float(best["qqq_sharpe"])},
        "terminal_events_in_window": data.terminal_events["status"].value_counts().to_dict()
        if len(data.terminal_events) else {},
        "eps_states": {k: int(len(v)) for k, v in states.items()},
        "eps_coverage_weeks_with_c_value": float(feat["c_growth"].notna().mean()),
        "cost_model": "IBKR Pro Tiered via scripts/research_reversal_dev.py order_cost/half_spread; cash earns 0",
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    feat.to_csv(EPS_CACHE / "features.csv.gz", index=False)
    return summary


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--fetch", action="store_true", help="fetch missing SEC companyfacts first (<= 3 req/s)")
    p.add_argument("--fetch-only", action="store_true")
    args = p.parse_args(argv)
    if args.fetch_only:
        uni = pd.read_csv(INPUTS / "weekly_universe_top300.csv.gz", dtype=str, usecols=["week_end", "cik"])
        uni = truncate(uni, "week_end", UNIVERSE_START)
        log = fetch_companyfacts(uni["cik"].dropna().unique())
        print(json.dumps({k: (v if not isinstance(v, (list, dict)) else len(v)) for k, v in log.items()}))
        print("errors:", log["error"])
        return
    s = run(args)
    print("variants:", s["variants_tried"], "Bonferroni t:", round(s["bonferroni_t_one_sided_5pct"], 2))


if __name__ == "__main__":
    main()
