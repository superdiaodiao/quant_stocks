"""Concentrated mega-cap strategies on the frozen data version 1 panel (pre-registered in docs/research_ledger_megacap.md).

Rules (6 configurations, monthly; signal at the last session of a month, trade at the next session's close):
  M1 top 10 Nasdaq names by point-in-time market cap, equal weight
  M2 top 5, equal weight
  M3 among the top 20 by market cap, the 5 with the best 6-month (126-session) total return, equal weight
  M4 among the top 20, the 10 with the best 12-1 momentum (t-252 .. t-21), equal weight
  M5 M3, but 100% QQQ when QQQ closes below its 200-session SMA on the signal day
  M6 top 10 by market cap, cap-weighted (sanity: should track QQQ)

Market cap at signal session s (first available): SEC shares (filed < s, within 400 days; diluted weighted average ->
basic weighted average -> dei cover shares -> balance-sheet shares; split-adjusted from the filing date) x raw close;
else dei public float (filed < s) x split-adjusted price change since its measurement date; else the Nasdaq company
list market cap (as_of_session <= s, within 365 days) x price change; else 50-day median dollar volume x that week's
median (market cap / dollar volume) of the top-50-by-dollar-volume names that have a market cap.

Judged vs ONEQ total return in 2014-01..2019-12 and 2020-01..2026-08 (2012-2013 reported only). Costs: IBKR Tiered
($10k account) from scripts/research_reversal_dev.py. Delisted names keep their terminal return (loader).

Usage:  PYTHONPATH=. .venv/bin/python scripts/research_megacap.py [--check-ranks]
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import research_livermore as lv  # noqa: E402  (window loader: panel, universe, terminal values, QQQ)
from scripts import research_indicators as ind  # noqa: E402  (ONEQ on sessions)
from scripts import research_canslim_dev as cs  # noqa: E402  (companyfacts cache dirs, date guard)
from scripts import research_reversal_dev as rev  # noqa: E402  (IBKR order cost, half spread, rebalance band)

OUT = ROOT / "output/research_only/megacap"
SHARES_CACHE = Path("/Users/bytedance/code/quant_stocks/research_cache/megacap/sec_share_facts.csv.gz")
LISTS = lv.CACHE / "prefilter/lists.csv.gz"
ACCOUNT = 10_000.0
WINDOW = "megacap"
lv.WINDOWS[WINDOW] = {"perf_start": "2012-01-01", "perf_end": "2026-08-31", "price_start": "2011-06-01",
                      "universe_start": "2012-01-01", "judged_from": "2014-01-01"}
PERIODS = {"2012-2013 (report only)": ("2012-01-01", "2013-12-31"),
           "H1 2014-2019": ("2014-01-01", "2019-12-31"),
           "H2 2020-2026-08": ("2020-01-01", "2026-08-31"),
           "Full 2014-2026-08": ("2014-01-01", "2026-08-31")}
JUDGED = ("H1 2014-2019", "H2 2020-2026-08")
FULL = "Full 2014-2026-08"
SHARE_CONCEPTS = (("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"),
                  ("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic"),
                  ("dei", "EntityCommonStockSharesOutstanding"),
                  ("us-gaap", "CommonStockSharesOutstanding"))
FRESH_DAYS = 400
LIST_DAYS = 365
MAX_MCAP = 6e12           # a proxy above $6T is a unit error: dropped
SHARES_VS_FLOAT = 2.5     # shares x price above 2.5x the public-float value -> shares rejected (data rule 0.2a)
DV_RATIO_MAX = 10.0       # market cap / dollar volume above 10x the week's top-50 median -> rejected (rule 0.2a)
ONEQ_HS = 2e-4
QQQ_HS = 1e-4
N_TRIALS = 6


@dataclass(frozen=True)
class Rule:
    name: str
    pool: int                 # top-N by market cap
    hold: int                 # names held
    mom: str | None = None    # None, "m6" (126 sessions) or "m12_1"
    trend: bool = False       # 100% QQQ when QQQ < SMA200 at the signal
    capw: bool = False        # cap-weighted instead of equal weight


RULES = (Rule("M1", 10, 10), Rule("M2", 5, 5), Rule("M3", 20, 5, "m6"), Rule("M4", 20, 10, "m12_1"),
         Rule("M5", 20, 5, "m6", trend=True), Rule("M6", 10, 10, capw=True))
assert len(RULES) == N_TRIALS


# ======================================================================== market-cap inputs

def _facts_from_payload(cik: int, payload: dict) -> list:
    rows = []
    facts = payload.get("facts", {})
    for pri, (tax, concept) in enumerate(SHARE_CONCEPTS):
        for unit, items in facts.get(tax, {}).get(concept, {}).get("units", {}).items():
            if unit != "shares":
                continue
            for f in items:
                if f.get("val") is None or not f.get("filed") or not f.get("end"):
                    continue
                rows.append((cik, pri, f["end"], f["filed"], float(f["val"]), f.get("start", "")))
    for unit, items in facts.get("dei", {}).get("EntityPublicFloat", {}).get("units", {}).items():
        if unit != "USD":
            continue
        for f in items:
            if f.get("val") is None or not f.get("filed") or not f.get("end"):
                continue
            rows.append((cik, 9, f["end"], f["filed"], float(f["val"]), ""))     # priority 9 = public float (USD)
    return rows


def extract_share_facts(ciks, cache: Path = SHARES_CACHE, refresh: bool = False) -> pd.DataFrame:
    """Long table cik, pri (0-3 share concepts, 9 public float), end, filed, val, start from every local
    companyfacts copy of each CIK (copies are merged and de-duplicated). Cached."""
    cols = ["cik", "pri", "end", "filed", "val", "start"]
    want = sorted(set(int(c) for c in ciks))
    done = cache.with_suffix(".ciks.json")
    if cache.exists() and done.exists() and not refresh and set(want) <= set(json.loads(done.read_text())):
        return pd.read_csv(cache, dtype={"start": str})
    rows = []
    for k, cik in enumerate(want):
        for d in cs.CF_DIRS:
            p = d / f"CIK{cik:010d}.json.gz"
            if not p.is_file():
                continue
            env = json.loads(gzip.decompress(p.read_bytes()))
            rows += _facts_from_payload(cik, env.get("payload", env))
        if k % 250 == 0:
            print(f"  companyfacts {k} CIKs read", flush=True)
    out = pd.DataFrame(rows, columns=cols).drop_duplicates(["cik", "pri", "end", "filed", "val"])
    cache.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(cache, index=False, compression="gzip")
    done.write_text(json.dumps(want))
    return out


def latest_fact_asof(queries: pd.DataFrame, facts: pd.DataFrame, fresh_days: int = FRESH_DAYS) -> pd.DataFrame:
    """For each query row (qid, cik, s) and each priority level, the fact with the latest ``end`` among facts filed
    strictly before s (ties: the latest filed), kept only when it was filed within ``fresh_days`` before s.
    Returns qid, pri, end, filed, val."""
    out = []
    q = queries[["qid", "cik", "s"]].astype({"cik": "int64"}).copy()
    q["s"] = pd.to_datetime(q["s"]).astype("datetime64[ns]")
    for pri, f in facts.groupby("pri"):
        f = f.copy()
        f["filed"] = pd.to_datetime(f["filed"]).astype("datetime64[ns]")
        f["end"] = pd.to_datetime(f["end"]).astype("datetime64[ns]")
        f = f.sort_values(["cik", "filed", "end"])
        # running "best by end" as filing dates advance (per CIK)
        best = []
        for cik, g in f.groupby("cik", sort=False):
            e = g["end"].to_numpy()
            run = np.maximum.accumulate(e.astype("int64"))
            keep = g[e.astype("int64") == run]       # rows that set or tie the running max of end
            best.append(keep)
        f = pd.concat(best) if best else f
        f = f.sort_values("filed")
        m = pd.merge_asof(q.sort_values("s"), f[["cik", "filed", "end", "val"]], left_on="s", right_on="filed",
                          by="cik", direction="backward", allow_exact_matches=False)
        m = m[m["filed"].notna() & ((m["s"] - m["filed"]).dt.days <= fresh_days)]
        m["pri"] = pri
        out.append(m[["qid", "pri", "end", "filed", "val"]])
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame(columns=["qid", "pri", "end", "filed", "val"])


def load_company_lists() -> pd.DataFrame:
    if not LISTS.exists():
        return pd.DataFrame(columns=["security_id", "as_of", "market_cap"])
    l = pd.read_csv(LISTS, dtype=str, keep_default_na=False)
    l["market_cap"] = pd.to_numeric(l["market_cap"], errors="coerce")
    l["as_of"] = pd.to_datetime(l["as_of_session"].where(l["as_of_session"] != "", l["snapshot_date"]))
    return l.loc[l["market_cap"] > 0, ["security_id", "as_of", "market_cap"]]


def _value_at(frame: pd.DataFrame, sids, dates) -> np.ndarray:
    """frame.loc[last session <= date, sid] for each pair (NaN when the date is before the frame)."""
    idx = frame.index
    pos = idx.searchsorted(pd.to_datetime(pd.Series(list(dates))).to_numpy(), side="right") - 1
    col = frame.columns.get_indexer(list(sids))
    vals = frame.to_numpy()
    ok = (pos >= 0) & (col >= 0)
    out = np.full(len(pos), np.nan)
    out[ok] = vals[pos[ok], col[ok]]
    return out


def market_caps(cand: pd.DataFrame, facts: pd.DataFrame, lists: pd.DataFrame, close: pd.DataFrame,
                tr_idx: pd.DataFrame, dv50: pd.DataFrame) -> pd.DataFrame:
    """Add ``mcap`` and ``mcap_src`` (sec_shares / public_float / company_list / dollar_volume) to candidate rows
    (columns s, security_id, cik, dv50_rank). Every input is dated strictly before (filings) or at (prices) s.
    Each source is a dollar value at an anchor date (shares x raw close at the filing session; the float at its
    measurement date; the list market cap at its as-of session) carried to s with the total-return index ratio
    idx(s) / idx(anchor), which handles splits and special distributions (e.g. Google's 2014 class C dividend);
    the dividend yield since the anchor (<= 400 days) is ignored."""
    c = cand.reset_index(drop=True).copy()
    c["qid"] = np.arange(len(c))
    c["mcap"], c["mcap_src"] = np.nan, ""
    # a share count filed before a series' first price uses its first price (the index ratio then cancels); a dollar
    # value (float, list market cap) dated before the series' first price cannot be carried forward: dropped
    idx_b = tr_idx.bfill()
    close_b = close.bfill()
    idx_s = _value_at(tr_idx, c["security_id"], c["s"])
    has_cik = c["cik"].notna()
    if len(facts) and has_cik.any():
        got = latest_fact_asof(c.loc[has_cik, ["qid", "cik", "s"]], facts)
        # 1. shares x price (first share concept by priority)
        sh = got[got["pri"] < 9].sort_values(["qid", "pri"]).drop_duplicates("qid")
        v_sh = np.full(len(c), np.nan)
        if len(sh):
            q = sh["qid"].to_numpy()
            sids = c.loc[q, "security_id"]
            v_sh[q] = (sh["val"].to_numpy() * _value_at(close_b, sids, sh["filed"]) * idx_s[q]
                       / _value_at(idx_b, sids, sh["filed"]))
        # 2. public float x price change since its measurement date
        fl = got[got["pri"] == 9]
        v_fl = np.full(len(c), np.nan)
        if len(fl):
            q = fl["qid"].to_numpy()
            v_fl[q] = fl["val"].to_numpy() * idx_s[q] / _value_at(tr_idx, c.loc[q, "security_id"], fl["end"])
        good = lambda v: np.isfinite(v) & (v > 0) & (v < MAX_MCAP)  # noqa: E731
        # cross-check (data rule, section 0.2a): shares x price more than SHARES_VS_FLOAT x the float value is a
        # unit error or an ADR-ratio mismatch (ordinary shares x ADS price) -> the float value is used instead
        bad_sh = good(v_sh) & good(v_fl) & (v_sh > SHARES_VS_FLOAT * v_fl)
        use_sh = good(v_sh) & ~bad_sh
        use_fl = ~use_sh & good(v_fl)
        c.loc[use_sh, "mcap"] = v_sh[use_sh]
        c.loc[use_sh, "mcap_src"] = "sec_shares"
        c.loc[use_fl, "mcap"] = v_fl[use_fl]
        c.loc[use_fl, "mcap_src"] = np.where(bad_sh[use_fl], "public_float_shares_rejected", "public_float")
    # 3. company list market cap x price change since its as-of session
    need = c["mcap"].isna()
    if need.any() and len(lists):
        left = c.loc[need, ["qid", "security_id", "s"]].copy()
        left["s"] = pd.to_datetime(left["s"]).astype("datetime64[ns]")
        right = lists.assign(as_of=lists["as_of"].astype("datetime64[ns]")).sort_values("as_of")
        m = pd.merge_asof(left.sort_values("s"), right, left_on="s", right_on="as_of", by="security_id",
                          direction="backward", tolerance=pd.Timedelta(days=LIST_DAYS))
        m = m[m["market_cap"].notna()]
        if len(m):
            q = m["qid"].to_numpy()
            v = m["market_cap"].to_numpy() * idx_s[q] / _value_at(tr_idx, m["security_id"], m["as_of"])
            ok = np.isfinite(v) & (v > 0) & (v < MAX_MCAP)
            c.loc[q[ok], "mcap"] = v[ok]
            c.loc[q[ok], "mcap_src"] = "company_list"
    # 4. dollar volume on the market-cap scale; (data rule 0.2a) a proxy whose market cap / dollar volume is more
    #    than DV_RATIO_MAX x that week's median of the top-50 names is treated as an error and replaced by it
    c["dv50"] = _value_at(dv50, c["security_id"], c["s"])
    for s, g in c.groupby("s"):
        top = g[(g["dv50_rank"] <= 50) & g["mcap"].notna() & (g["dv50"] > 0)]
        ratio = float((top["mcap"] / top["dv50"]).median()) if len(top) else np.nan
        if not np.isfinite(ratio):
            continue
        odd = g.index[g["mcap"].notna() & (g["dv50"] > 0) & (g["mcap"] / g["dv50"] > DV_RATIO_MAX * ratio)]
        miss = g.index[g["mcap"].isna() & (g["dv50"] > 0)]
        c.loc[odd, "mcap_src"] = "dollar_volume_proxy_rejected"
        c.loc[miss, "mcap_src"] = "dollar_volume"
        c.loc[odd.union(miss), "mcap"] = c.loc[odd.union(miss), "dv50"] * ratio
    return c.drop(columns=["qid"])


def successor_ciks(uni: pd.DataFrame) -> dict:
    """predecessor CIK -> successor CIK for securities that continued as another security (terminal file
    ``continued_as``, e.g. Google Inc. -> Alphabet 2015-10), so a successor without its own filings yet uses the
    predecessor's share counts."""
    term = pd.read_csv(lv.INPUTS / "terminal_returns_2012_2026.csv", dtype=str)
    term = term[term["continued_as"].notna() & term["cik"].notna()]
    cik_of = uni.dropna(subset=["cik"]).drop_duplicates("security_id").set_index("security_id")["cik"]
    out = {}
    for r in term.itertuples():
        succ = cik_of.get(r.continued_as)
        if succ is not None and str(int(float(succ))) != str(int(float(r.cik))):
            out[int(float(r.cik))] = int(float(succ))
    return out


def add_predecessor_facts(facts: pd.DataFrame, alias: dict) -> pd.DataFrame:
    extra = [facts[facts["cik"] == p].assign(cik=s) for p, s in alias.items()]
    return pd.concat([facts] + extra, ignore_index=True) if extra else facts


# ======================================================================== candidates and targets

def signal_sessions(sessions: pd.DatetimeIndex) -> list:
    """Last session of every month except the last loaded month when it is incomplete (no later session)."""
    s = pd.Series(sessions, index=sessions)
    last = s.groupby(sessions.to_period("M")).max()
    return [d for d in last if d < sessions[-1]]


def candidates(uni: pd.DataFrame, signals: list, close: pd.DataFrame, last_row: pd.Series) -> pd.DataFrame:
    """For each signal s: the universe week with week_end <= s (latest), rows with a dv50 rank, a close at s and an
    unfinished series. One row per company (multi-class group, else CIK): the class with the best dv50 rank."""
    u = uni[uni["dv50_rank"].notna() & uni["security_id"].isin(close.columns)].copy()
    weeks = np.array(sorted(u["week_end"].unique()), dtype="datetime64[ns]")
    by_week = {w: g for w, g in u.groupby("week_end")}
    rows = []
    for s in signals:
        k = weeks.searchsorted(np.datetime64(s), side="right") - 1
        if k < 0:
            continue
        g = by_week[pd.Timestamp(weeks[k])].copy()
        g["s"] = s
        g["universe_week"] = pd.Timestamp(weeks[k])
        rows.append(g)
    c = pd.concat(rows, ignore_index=True)
    px = _value_at(close, c["security_id"], c["s"])
    lr = pd.to_datetime(c["security_id"].map(last_row))
    c = c[np.isfinite(px) & (lr.isna() | (lr >= c["s"])).to_numpy()].copy()
    c["cik"] = pd.to_numeric(c["cik"], errors="coerce").astype("Int64")
    c["company"] = c["multi_class_group"].where(c["multi_class_group"].notna() & (c["multi_class_group"] != ""),
                                                c["cik"].astype(str))
    c["company"] = c["company"].where(c["company"] != "<NA>", c["security_id"])
    c = c.sort_values(["s", "company", "dv50_rank", "security_id"]).drop_duplicates(["s", "company"])
    cs.assert_window(c["universe_week"], "2012-01-01", lv.WINDOWS[WINDOW]["perf_end"], "universe weeks used")
    return c.reset_index(drop=True)


def momentum_frames(idx: pd.DataFrame) -> dict:
    """Total-return momentum known at the close of each session (complete history required)."""
    return {"m6": idx / idx.shift(126) - 1, "m12_1": idx.shift(21) / idx.shift(252) - 1}


def build_targets(rule: Rule, ranked: pd.DataFrame, mom: dict, qqq_close: pd.Series) -> dict:
    """signal session -> list of (sid, weight, dv50_rank, mcap, mcap_src); 'QQQ' for the trend filter's QQQ leg.
    ``ranked``: candidates with mcap, one row per company. Signals with too few names are skipped."""
    sma = qqq_close.rolling(200, min_periods=200).mean()
    out = {}
    for s, g in ranked.groupby("s"):
        g = g[g["mcap"].notna()].sort_values(["mcap", "security_id"], ascending=[False, True]).head(rule.pool)
        if len(g) < rule.pool:
            continue
        if rule.mom is not None:
            m = mom[rule.mom]
            g = g.assign(mom=_value_at(m, g["security_id"], [s] * len(g)))
            if g["mom"].isna().all():
                continue
            g = g[g["mom"].notna()].sort_values(["mom", "security_id"], ascending=[False, True]).head(rule.hold)
            if len(g) < rule.hold:
                continue
        if rule.trend:
            if not np.isfinite(sma.loc[s]):
                continue
            if qqq_close.loc[s] < sma.loc[s]:
                out[s] = [("QQQ", 1.0, np.nan, np.nan, "qqq")]
                continue
        w = (g["mcap"] / g["mcap"].sum()).to_numpy() if rule.capw else np.full(len(g), 1.0 / len(g))
        out[s] = [(r.security_id, float(wi), float(r.dv50_rank), float(r.mcap), r.mcap_src)
                  for r, wi in zip(g.itertuples(), w)]
    return out


# ======================================================================== the portfolio engine

def order_cost(value: float, price: float, sell: bool, hs: float) -> float:
    if value <= 0 or not np.isfinite(price) or price <= 0:
        return 0.0
    return rev.order_cost(value / price, price, sell=sell, hs=hs)["total"]


def simulate(targets: dict, sessions: pd.DatetimeIndex, idx: pd.DataFrame, close: pd.DataFrame,
             last_row: pd.Series, qqq_idx: pd.Series, qqq_close: pd.Series, account: float = ACCOUNT,
             band: float = rev.REBALANCE_BAND, end: str | None = None) -> dict:
    """Daily simulation. A target decided at the close of signal session s is traded at the close of the next
    session. Positions are units of the total-return index ('QQQ' uses the QQQ index). Sells first, then buys
    (scaled down when cash is short); a continuing position is traded only when it is more than ``band`` x its
    target value away from the target. Ended series are turned into cash (terminal return already booked)."""
    if not targets:
        raise ValueError("no targets")
    sigs = sorted(targets)
    execs = {}
    for s in sigs:
        later = sessions[sessions > s]
        if len(later):
            execs[later[0]] = s
    start = min(execs)
    perf = sessions[(sessions >= start) & (sessions <= pd.Timestamp(end or sessions[-1]))]
    col = {c: i for i, c in enumerate(idx.columns)}
    I = idx.reindex(perf).to_numpy()
    P = close.reindex(perf).to_numpy()
    Q = qqq_idx.reindex(perf).to_numpy()
    QP = qqq_close.reindex(perf).to_numpy()
    lr = last_row
    cash = account
    pos: dict = {}           # sid -> units
    nav = np.zeros(len(perf))
    cost = np.zeros(len(perf))
    traded = np.zeros(len(perf))
    n_orders = np.zeros(len(perf), dtype=int)
    n_names = np.zeros(len(perf), dtype=int)

    def ix(sid, i):
        return Q[i] if sid == "QQQ" else I[i, col[sid]]

    def px(sid, i):
        return QP[i] if sid == "QQQ" else P[i, col[sid]]

    for i, d in enumerate(perf):
        for sid in [s for s in pos if s != "QQQ" and pd.notna(lr.get(s)) and d > lr[s]]:
            cash += pos.pop(sid) * I[i, col[sid]]
        if d in execs:
            tgt = targets[execs[d]]
            hs_of = {sid: (QQQ_HS if sid == "QQQ" else rev.half_spread(rk, px(sid, i))) for sid, _, rk, _, _ in tgt}
            for sid in pos:
                if sid not in hs_of:
                    hs_of[sid] = QQQ_HS if sid == "QQQ" else rev.half_spread(50, px(sid, i))
            total = cash + sum(u * ix(s, i) for s, u in pos.items())
            want = {sid: w * total for sid, w, _, _, _ in tgt if sid == "QQQ" or np.isfinite(ix(sid, i))}
            sells, buys = {}, {}
            for sid, u in pos.items():
                cur = u * ix(sid, i)
                tv = want.get(sid, 0.0)
                if tv == 0.0:
                    sells[sid] = cur
                elif abs(tv - cur) > band * tv:
                    (sells if tv < cur else buys)[sid] = abs(tv - cur)
            for sid, tv in want.items():
                if sid not in pos:
                    buys[sid] = tv
            for sid, v in sorted(sells.items()):
                k = order_cost(v, px(sid, i), True, hs_of[sid])
                pos[sid] -= v / ix(sid, i)
                if pos[sid] <= 1e-12 or sid not in want:
                    pos.pop(sid)
                cash += v - k
                cost[i] += k
                traded[i] += v
                n_orders[i] += 1
            need = sum(buys.values())
            scale = min(1.0, cash / need) if need > 0 else 1.0
            for sid, v in sorted(buys.items()):
                amt = v * scale
                if amt < 1.0:
                    continue
                k = order_cost(amt, px(sid, i), False, hs_of[sid])
                pos[sid] = pos.get(sid, 0.0) + (amt - k) / ix(sid, i)
                cash -= amt
                cost[i] += k
                traded[i] += amt
                n_orders[i] += 1
            cash = max(cash, 0.0) if abs(cash) < 1e-6 else cash
        nav[i] = cash + sum(u * ix(s, i) for s, u in pos.items())
        n_names[i] = len(pos)
    return {"nav": pd.Series(nav, index=perf), "cost": pd.Series(cost, index=perf),
            "traded": pd.Series(traded, index=perf), "orders": pd.Series(n_orders, index=perf),
            "names": pd.Series(n_names, index=perf), "start": perf[0]}


def buy_hold(level: pd.Series, price: pd.Series, dates: pd.DatetimeIndex, hs: float, account: float = ACCOUNT):
    lvl = level.reindex(dates)
    k = order_cost(account, float(price.reindex(dates).iloc[0]), False, hs)
    return (account - k) * lvl / lvl.iloc[0]


# ======================================================================== metrics

def _monthly(r: pd.Series) -> pd.Series:
    return (1 + r).groupby(r.index.to_period("M")).prod() - 1


def _yearly(r: pd.Series) -> pd.Series:
    return (1 + r).groupby(r.index.year).prod() - 1


def max_drawdown(v: pd.Series) -> float:
    return float((v / v.cummax() - 1).min())


def longest_drawdown_days(v: pd.Series) -> int:
    """Longest calendar-day span from a peak until the value first regains it (or the end, if never)."""
    peak_val, peak_day, longest = -np.inf, None, 0
    for d, x in v.items():
        if x >= peak_val:
            if peak_day is not None:
                longest = max(longest, (d - peak_day).days)
            peak_val, peak_day = x, d
    if peak_day is not None and v.iloc[-1] < peak_val:
        longest = max(longest, (v.index[-1] - peak_day).days)
    return int(longest)


def window_metrics(nav: pd.Series, oneq: pd.Series, qqq: pd.Series, a: str, b: str, cost: pd.Series | None = None,
                   traded: pd.Series | None = None, orders: pd.Series | None = None) -> dict | None:
    """Metrics of ``nav`` over [a, b], rebased at the last session before a (or the first session when the series
    starts inside the window)."""
    a_, b_ = pd.Timestamp(a), pd.Timestamp(b)
    before = nav.index[nav.index < a_]
    t0 = before[-1] if len(before) else nav.index[0]
    keep = (nav.index >= t0) & (nav.index <= b_)
    if keep.sum() < 40:
        return None
    v, vb, vq = nav[keep], oneq.reindex(nav.index)[keep], qqq.reindex(nav.index)[keep]
    r, rb, rq = v.pct_change().iloc[1:], vb.pct_change().iloc[1:], vq.pct_change().iloc[1:]
    years = (v.index[-1] - v.index[0]).days / 365.25
    cagr = (v.iloc[-1] / v.iloc[0]) ** (1 / years) - 1
    bc = (vb.iloc[-1] / vb.iloc[0]) ** (1 / years) - 1
    qc = (vq.iloc[-1] / vq.iloc[0]) ** (1 / years) - 1
    dd, bdd, qdd = max_drawdown(v), max_drawdown(vb), max_drawdown(vq)
    act = r - rb
    mx = _monthly(r) - _monthly(rb)
    mq = _monthly(r) - _monthly(rq)
    actq = r - rq
    beta = float(np.cov(r, rb, ddof=1)[0, 1] / rb.var(ddof=1))
    alpha = float((r.mean() - beta * rb.mean()) * 252)
    down = np.sqrt((np.minimum(r, 0) ** 2).mean()) * math.sqrt(252)
    ys, yb = _yearly(r), _yearly(rb)
    m = _monthly(r)
    out = {"start": str(r.index[0].date()), "end": str(r.index[-1].date()), "years": round(years, 2),
           "cagr": cagr, "oneq_cagr": bc, "qqq_cagr": qc, "excess_vs_oneq": cagr - bc, "excess_vs_qqq": cagr - qc,
           "vol": float(r.std(ddof=1) * math.sqrt(252)), "max_dd": dd, "oneq_max_dd": bdd, "qqq_max_dd": qdd,
           "dd_shallower_than_oneq_pp": (abs(bdd) - abs(dd)) * 100,
           "longest_dd_days": longest_drawdown_days(v), "oneq_longest_dd_days": longest_drawdown_days(vb),
           "sharpe": float(r.mean() / r.std(ddof=1) * math.sqrt(252)),
           "oneq_sharpe": float(rb.mean() / rb.std(ddof=1) * math.sqrt(252)),
           "sortino": float(r.mean() * 252 / down) if down > 0 else np.nan,
           "calmar": cagr / abs(dd) if dd < 0 else np.nan,
           "ir": float(act.mean() / act.std(ddof=1) * math.sqrt(252)) if act.std() > 0 else np.nan,
           "tracking_error": float(act.std(ddof=1) * math.sqrt(252)),
           "beta_vs_oneq": beta, "alpha_vs_oneq_ann": alpha,
           "months": int(len(mx)), "t_monthly_excess_vs_oneq": float(mx.mean() / mx.std(ddof=1) * math.sqrt(len(mx)))
           if mx.std() > 0 else np.nan,
           "t_monthly_excess_vs_qqq": float(mq.mean() / mq.std(ddof=1) * math.sqrt(len(mq))) if mq.std() > 0 else np.nan,
           "ir_vs_qqq": float(actq.mean() / actq.std(ddof=1) * math.sqrt(252)) if actq.std() > 0 else np.nan,
           "years_beating_qqq": int((ys > _yearly(rq)).sum()),
           "years_beating_oneq": int((ys > yb).sum()), "n_years": int(len(ys)),
           "share_years_beating_oneq": float((ys > yb).mean()),
           "worst_year": float(ys.min()), "worst_year_label": int(ys.idxmin()),
           "worst_month": float(m.min()), "worst_month_label": str(m.idxmin())}
    if cost is not None:
        sel = (cost.index > t0) & (cost.index <= b_)
        prev = nav.shift(1).reindex(cost.index)[sel]
        out["cost_drag_per_year"] = float((cost[sel] / prev).sum() / years)
        out["cost_usd"] = float(cost[sel].sum())
        out["turnover_one_way_per_year"] = float(traded[sel].sum() / 2 / v.mean() / years)
        out["orders"] = int(orders[sel].sum())
    return out


def criteria(per: dict) -> dict:
    h = [per[p] for p in JUDGED]
    a = all(x["cagr"] > x["oneq_cagr"] for x in h) and per[FULL]["t_monthly_excess_vs_oneq"] >= 2.0
    b = all(x["dd_shallower_than_oneq_pp"] >= 10.0 and x["cagr"] >= x["oneq_cagr"] - 0.03 for x in h)
    return {"A_cagr_above_oneq_both_halves_and_full_t_ge_2": bool(a),
            "B_dd_10pp_shallower_and_cagr_within_3pp_both_halves": bool(b), "pass": bool(a or b)}


# ======================================================================== run

def prepare():
    data = lv.load_window(WINDOW)
    end = data.spec["effective_end"]
    print("DATE GUARD:", data.guard["assertion"])
    dv50 = (data.close_adj * data.vol_adj).rolling(50, min_periods=20).median()
    sigs = signal_sessions(data.sessions)
    cand = candidates(data.universe, sigs, data.close, data.last_row)
    ciks = cand["cik"].dropna().astype(int).unique()
    alias = successor_ciks(data.universe)
    facts = extract_share_facts(sorted(set(ciks) | set(alias)))
    facts = add_predecessor_facts(facts, alias)
    lists = load_company_lists()
    ranked = market_caps(cand[["s", "security_id", "ticker", "cik", "dv50_rank", "universe_week"]], facts, lists,
                         data.close, data.sig_idx, dv50)
    oneq_lvl, oneq_px = ind.oneq_on_sessions(data.sessions, lv.WINDOWS[WINDOW]["price_start"], end)
    return data, ranked, oneq_lvl, oneq_px, end


def rank_check(ranked: pd.DataFrame) -> pd.DataFrame:
    """Top 20 by market cap at each December signal (for eyeballing against known history) + source shares."""
    rows = []
    for s, g in ranked.groupby("s"):
        g = g[g["mcap"].notna()].sort_values("mcap", ascending=False).head(20)
        for k, r in enumerate(g.itertuples(), 1):
            rows.append({"s": s.date().isoformat(), "rank": k, "ticker": r.ticker, "security_id": r.security_id,
                         "mcap_bn": round(r.mcap / 1e9, 1), "src": r.mcap_src, "dv50_rank": r.dv50_rank})
    return pd.DataFrame(rows)


def run(args) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    data, ranked, oneq_lvl, oneq_px, end = prepare()
    top = rank_check(ranked)
    top.to_csv(OUT / "top20_by_month.csv", index=False)
    src = top.groupby(top["s"].str[:4])["src"].value_counts(normalize=True).unstack(fill_value=0).round(3)
    src.to_csv(OUT / "mcap_source_share_top20.csv")
    print("market-cap source share among each month's top 20:\n", src.to_string())
    dec = top[top["s"].str[5:7] == "12"]
    print("December top 10:")
    for s, g in dec.groupby("s"):
        print(" ", s, ", ".join(f"{t}({m:.0f}{'' if x == 'sec_shares' else '*'})" for t, m, x in
                                zip(g.ticker[:10], g.mcap_bn[:10], g.src[:10])))
    if args.check_ranks:
        return {}
    mom = momentum_frames(data.sig_idx)
    ranked_x = ranked.copy()          # sensitivity (0.2a): names with no market-cap source are not ranked
    ranked_x.loc[ranked_x["mcap_src"] == "dollar_volume", "mcap"] = np.nan
    results, summary, by_year, holds, navs = {}, [], [], [], {}
    for rule in RULES:
        tg = build_targets(rule, ranked, mom, data.qqq_close)
        tgx = build_targets(rule, ranked_x, mom, data.qqq_close)
        simx = simulate(tgx, data.sessions, data.perf_idx, data.close,
                        data.last_row, data.qqq_perf_idx, data.qqq_close)
        sim = simulate(tg, data.sessions, data.perf_idx, data.close,
                       data.last_row, data.qqq_perf_idx, data.qqq_close)
        sim0 = simulate(tg, data.sessions, data.perf_idx, data.close,
                        data.last_row, data.qqq_perf_idx, data.qqq_close, band=0.0)
        dates = sim["nav"].index
        oneq = buy_hold(oneq_lvl, oneq_px, dates, ONEQ_HS)
        qqq = buy_hold(data.qqq_perf_idx, data.qqq_close, dates, QQQ_HS)
        per = {}
        for pname, (a, b) in PERIODS.items():
            m = window_metrics(sim["nav"], oneq, qqq, a, b, sim["cost"], sim["traded"], sim["orders"])
            m0 = window_metrics(sim0["nav"], oneq, qqq, a, b)
            mx = window_metrics(simx["nav"], oneq.reindex(simx["nav"].index), qqq.reindex(simx["nav"].index), a, b)
            if m is None:
                continue
            m["cagr_band0_sensitivity"] = m0["cagr"] if m0 else np.nan
            m["cagr_no_dv_fallback_sensitivity"] = mx["cagr"] if mx else np.nan
            m["t_no_dv_fallback_sensitivity"] = mx["t_monthly_excess_vs_oneq"] if mx else np.nan
            m["max_dd_no_dv_fallback_sensitivity"] = mx["max_dd"] if mx else np.nan
            per[pname] = m
            summary.append({"rule": rule.name, "period": pname, **m})
        crit = criteria(per)
        crit_x = criteria({p: {"cagr": v["cagr_no_dv_fallback_sensitivity"], "oneq_cagr": v["oneq_cagr"],
                               "t_monthly_excess_vs_oneq": v["t_no_dv_fallback_sensitivity"],
                               "dd_shallower_than_oneq_pp": (abs(v["oneq_max_dd"])
                                                             - abs(v["max_dd_no_dv_fallback_sensitivity"])) * 100}
                           for p, v in per.items()})
        n_dv = sum(1 for lst in tg.values() for x in lst if str(x[4]).startswith("dollar_volume"))
        results[rule.name] = {"criteria_no_dv_fallback_sensitivity": crit_x, "picks_using_dollar_volume_mcap": n_dv,
                              "rule": rule.__dict__, "start": str(sim["start"].date()), "periods": per, "criteria": crit,
                              "avg_names_held": float(sim["names"].mean()),
                              "share_months_in_qqq": float(np.mean([t[0][0] == "QQQ" for t in tg.values()]))}
        r = sim["nav"].pct_change().dropna()
        for y, v in _yearly(r).items():
            by_year.append({"rule": rule.name, "year": y, "strategy": v,
                            "oneq": _yearly(oneq.pct_change().dropna()).get(y),
                            "qqq": _yearly(qqq.pct_change().dropna()).get(y)})
        for s, lst in tg.items():
            for k, (sid, w, rk, mc, sr) in enumerate(lst, 1):
                holds.append({"rule": rule.name, "signal": s.date().isoformat(), "slot": k, "security_id": sid,
                              "weight": round(w, 4), "mcap_bn": round(mc / 1e9, 1) if np.isfinite(mc) else None,
                              "mcap_src": sr})
        navs[rule.name] = sim["nav"]
        navs[f"ONEQ_from_{rule.name}"] = oneq
        navs[f"QQQ_from_{rule.name}"] = qqq
        f = per[FULL]
        print(f"{rule.name}: start {sim['start'].date()} | " + " | ".join(
            f"{p.split()[0]} CAGR {per[p]['cagr']:+.1%} ONEQ {per[p]['oneq_cagr']:+.1%} DD {per[p]['max_dd']:.0%}"
            f" (ONEQ {per[p]['oneq_max_dd']:.0%})" for p in JUDGED) +
            f" | full t {f['t_monthly_excess_vs_oneq']:.2f} | pass {crit['pass']}")
    # benchmark rows (ONEQ and QQQ themselves, from M1's start)
    for name in ("ONEQ", "QQQ"):
        s_ = navs[f"{name}_from_M1"]
        for pname, (a, b) in PERIODS.items():
            m = window_metrics(s_, navs["ONEQ_from_M1"], navs["QQQ_from_M1"], a, b)
            if m:
                summary.append({"rule": f"{name} buy-hold", "period": pname, **m})
    qper = {p: window_metrics(navs["QQQ_from_M1"], navs["ONEQ_from_M1"], navs["QQQ_from_M1"], a, b)
            for p, (a, b) in PERIODS.items()}
    qqq_reference = criteria(qper)
    print("reference (not a trial): QQQ buy-hold judged by the same criteria vs ONEQ:", qqq_reference,
          f"full t {qper[FULL]['t_monthly_excess_vs_oneq']:.2f}")
    tick = ranked.drop_duplicates("security_id").set_index("security_id")["ticker"]
    hd = pd.DataFrame(holds)
    hd["ticker"] = hd["security_id"].map(tick).fillna(hd["security_id"])
    hd.to_csv(OUT / "holdings_by_signal.csv", index=False)
    pd.DataFrame(summary).to_csv(OUT / "summary.csv", index=False)
    pd.DataFrame(by_year).to_csv(OUT / "by_year.csv", index=False)
    pd.DataFrame(navs).to_csv(OUT / "nav_daily.csv")
    out = {"window": data.spec, "guard": data.guard["assertion"], "n_trials": N_TRIALS,
           "bonferroni_t_one_sided_5pct": NormalDist().inv_cdf(1 - 0.05 / N_TRIALS),
           "mcap_source_share_top20_by_year": json.loads(src.to_json(orient="index")),
           "qqq_buyhold_reference_criteria": qqq_reference, "results": results, "terminal_events": int(len(data.terminal_events))}
    (OUT / "results.json").write_text(json.dumps(out, indent=1, default=lambda x: x if not isinstance(x, float)
                                                 else round(x, 6)) + "\n")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check-ranks", action="store_true", help="only build and print the market-cap ranking")
    run(ap.parse_args(argv))


if __name__ == "__main__":
    main()
