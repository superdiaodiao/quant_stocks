"""Signals from free SEC data (docs/research_ledger_sec_alt.md, section 0): opportunistic insider purchases (I1),
insider cluster buys (I2), I1 under a market-cap limit (I3), 13F cloning (F1 / F2), and their two engines (an
event book that holds each buy for a fixed number of sessions; an equal-weight target portfolio).

Moved unchanged from studies/sec_alt.py (``routine_owners``, ``qualified_purchases``, ``cluster_signals``,
``entry_session``, ``attach_entries``, ``insider_entries``, ``f13_targets``, ``rank_lookup``, ``run_insider``,
``run_targets``, ``benchmark_nav``). The registered rule numbers (minimum purchase, cluster size and window, hold,
book size, market-cap limit, rebalance band), the account, the window and every cache folder are parameters; the
SEC readers are in quant.data.sec_alt.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd

from quant.backtest.costs import ibkr_order_cost, rank_half_spread
from quant.data.sec_alt import SecMap, shares_asof
from quant.data.sources.yahoo import parse_chart


# ======================================================================== insider signals

def routine_owners(trades: pd.DataFrame) -> set:
    """{(owner_cik, year)}: owners with an open-market trade in the same calendar month in each of the three previous
    calendar years, counting only filings made before 1 January of ``year`` (Cohen-Malloy-Pomorski)."""
    t = trades[["owner_cik", "trans_date", "filing_date"]].copy()
    t["ty"], t["tm"] = t["trans_date"].dt.year, t["trans_date"].dt.month
    out = set()
    years = trades["trans_date"].dt.year
    years = years[years.between(2000, 2026)]
    for year in range(int(years.min()) + 3, int(years.max()) + 2):
        known = t[(t["filing_date"] < pd.Timestamp(f"{year}-01-01")) & t["ty"].between(year - 3, year - 1)]
        if known.empty:
            continue
        g = known.drop_duplicates(["owner_cik", "ty", "tm"]).groupby(["owner_cik", "tm"])["ty"].nunique()
        out |= {(int(o), year) for o in g[g == 3].index.get_level_values(0).unique()}
    return out


def qualified_purchases(trades: pd.DataFrame, routine: set, bad_title: re.Pattern) -> pd.DataFrame:
    """One row per (Form 4, officer / director owner): opportunistic open-market purchase totals (securities whose
    title matches ``bad_title`` are not common stock and are dropped)."""
    p = trades[(trades["code"] == "P") & (trades["ad"] == "A") & (trades["price"] > 0) & (trades["shares"] > 0)
               & (trades["trans_date"] <= trades["filing_date"])
               & trades["relationship"].fillna("").str.contains("Director|Officer")
               & ~trades["title"].fillna("").str.contains(bad_title)].copy()
    p["value"] = p["shares"] * p["price"]
    g = p.groupby(["accession", "owner_cik"]).agg(
        filing_date=("filing_date", "first"), issuer_cik=("issuer_cik", "first"), symbol=("symbol", "first"),
        trans_date=("trans_date", "max"), value=("value", "sum")).reset_index()
    key = list(zip(g["owner_cik"].astype(int), g["trans_date"].dt.year))
    g["routine"] = [k in routine for k in key]
    return g


def cluster_signals(q: pd.DataFrame, cluster_n: int, cluster_days: int, min_purchase: float) -> pd.DataFrame:
    """I2: per issuer, a Form 4 that brings the count of distinct qualifying insiders whose purchase dates fit in one
    ``cluster_days`` window to >= ``cluster_n`` (window total >= ``min_purchase``), using only Form 4s filed by that
    day; then ``cluster_days`` days of quiet."""
    out = []
    for cik, g in q.sort_values(["filing_date", "accession"]).groupby("issuer_cik"):
        g = g.reset_index(drop=True)
        quiet_until = pd.Timestamp.min
        for i, r in g.iterrows():
            if r["filing_date"] <= quiet_until:
                continue
            known = g[(g["filing_date"] <= r["filing_date"])
                      & ((g["trans_date"] - r["trans_date"]).abs() <= pd.Timedelta(days=cluster_days))]
            best = None
            for a in sorted(set(known["trans_date"])):
                if not (a <= r["trans_date"] <= a + pd.Timedelta(days=cluster_days)):
                    continue
                w = known[(known["trans_date"] >= a) & (known["trans_date"] <= a + pd.Timedelta(days=cluster_days))]
                n, v = w["owner_cik"].nunique(), w["value"].sum()
                if n >= cluster_n and v >= min_purchase and (best is None or v > best[1]):
                    best = (n, v)
            if best is not None:
                out.append({"filing_date": r["filing_date"], "issuer_cik": cik, "symbol": r["symbol"],
                            "value": best[1], "n_insiders": best[0], "accession": r["accession"]})
                quiet_until = r["filing_date"] + pd.Timedelta(days=cluster_days)
    return pd.DataFrame(out, columns=["filing_date", "issuer_cik", "symbol", "value", "n_insiders", "accession"])


def entry_session(sessions: pd.DatetimeIndex, filing_date: pd.Timestamp) -> pd.Timestamp | None:
    i = sessions.searchsorted(filing_date, side="right")
    return sessions[i] if i < len(sessions) else None


def attach_entries(sig: pd.DataFrame, secmap: SecMap, sessions: pd.DatetimeIndex, close: pd.DataFrame,
                   last_row: pd.Series, start: str, end: str) -> tuple[pd.DataFrame, dict]:
    """Map each signal to a panel security and an entry session in (start, end]; count every reason a signal is
    skipped."""
    counts = {"signals": int(len(sig))}
    rows = []
    for r in sig.itertuples():
        sid, how = secmap.lookup(r.issuer_cik, r.symbol if isinstance(r.symbol, str) else "", r.filing_date)
        if sid is None:
            counts[how] = counts.get(how, 0) + 1
            continue
        e = entry_session(sessions, r.filing_date)
        if e is None or e > pd.Timestamp(end) or e <= pd.Timestamp(start):
            counts["entry_outside_window"] = counts.get("entry_outside_window", 0) + 1
            continue
        if sid not in close.columns or not np.isfinite(close.at[e, sid]) or (pd.notna(last_row.get(sid))
                                                                               and last_row[sid] < e):
            counts["no_panel_price_on_entry"] = counts.get("no_panel_price_on_entry", 0) + 1
            continue
        rows.append({**r._asdict(), "security_id": sid, "entry": e, "map": how})
    df = pd.DataFrame(rows)
    if len(df):
        df = df.drop(columns=["Index"]).sort_values(["entry", "value"], ascending=[True, False])
        before = len(df)
        df = df.drop_duplicates(["entry", "security_id"])
        counts["same_day_duplicates"] = before - len(df)
    counts["priced_signals"] = int(len(df))
    return df, counts


def insider_entries(data, secmap: SecMap, trades: pd.DataFrame, shares: pd.DataFrame, *, bad_title: re.Pattern,
                    min_purchase: float, cluster_n: int, cluster_days: int, mcap_max: float, start: str,
                    end: str) -> tuple[dict, dict, pd.DataFrame]:
    """The I1 / I2 / I3 entries (``attach_entries``) from the open-market ``trades`` and the shares outstanding,
    with the filter and skip counts. Returns (entries, counts, qualified purchases)."""
    routine = routine_owners(trades)
    q = qualified_purchases(trades, routine, bad_title)
    q = q[q["filing_date"] >= pd.Timestamp("2013-12-01")]
    stats = {"form4_open_market_rows": int(len(trades)), "qualified_purchase_filings_2014on": int(len(q)),
             "routine_dropped": int(q["routine"].sum())}
    opp = q[~q["routine"]]
    sig1 = opp[opp["value"] >= min_purchase][["filing_date", "issuer_cik", "symbol", "value", "accession", "owner_cik"]]
    sig2 = cluster_signals(opp, cluster_n, cluster_days, min_purchase)
    out, counts = {}, {}
    out["I1"], counts["I1"] = attach_entries(sig1, secmap, data.sessions, data.close, data.last_row, start, end)
    out["I2"], counts["I2"] = attach_entries(sig2, secmap, data.sessions, data.close, data.last_row, start, end)
    e = out["I1"].copy()
    caps = []
    for r in e.itertuples():
        sh = shares_asof(shares, int(r.issuer_cik), r.filing_date)
        px_day = data.sessions[data.sessions <= r.filing_date][-1]
        caps.append(sh * data.close.at[px_day, r.security_id])
    e["mcap"] = caps
    counts["I3"] = {"from_I1_priced": int(len(e)), "no_shares_data": int(e["mcap"].isna().sum()),
                    "mcap_ge_5b": int((e["mcap"] >= mcap_max).sum())}
    out["I3"] = e[e["mcap"] < mcap_max]
    counts["I3"]["priced_signals"] = int(len(out["I3"]))
    return out, {"filters": stats, **counts}, q


def f13_targets(top: pd.DataFrame, sessions: pd.DatetimeIndex, cmap: dict, start: str,
                end: str) -> tuple[dict, pd.DataFrame]:
    """Trade session -> target list (union of each manager's latest top 10, priced names only)."""
    top = top.copy()
    top["security_id"] = top["cusip"].map(cmap)
    latest, targets, cov = {}, {}, []
    start = pd.Timestamp(start)
    for acc, g in top.sort_values(["acceptance", "rank"]).groupby("accession", sort=False):
        mgr = g["manager"].iloc[0]
        latest[mgr] = g
        acc_date = pd.Timestamp(g["acceptance"].iloc[0][:10])
        trade = entry_session(sessions, acc_date)
        if trade is None or trade > pd.Timestamp(end):
            continue
        trade = max(trade, start)
        names = []
        for m in sorted(latest):
            names += [s for s in latest[m]["security_id"] if isinstance(s, str)]
        targets[trade] = list(dict.fromkeys(names))
        cov.append({"manager": mgr, "accession": acc, "acceptance": g["acceptance"].iloc[0],
                    "report_date": g["report_date"].iloc[0], "trade_session": str(trade.date()),
                    "top10_priced_n": int(g["security_id"].notna().sum()),
                    "top10_value_share_priced": float(g.loc[g["security_id"].notna(), "weight_in_top10"].sum()),
                    "priced": " ".join(g.loc[g["security_id"].notna(), "name"]),
                    "target_n": len(targets[trade])})
    # sessions before START collapse onto START: keep only the last pre-start target
    return targets, pd.DataFrame(cov)


# ======================================================================== portfolio engines

def rank_lookup(universe: pd.DataFrame):
    weeks = np.array(sorted(universe["week_end"].unique()), dtype="datetime64[ns]")
    by = {pd.Timestamp(w): g.set_index("security_id")["dv50_rank"].to_dict() for w, g in universe.groupby("week_end")}

    def f(d: pd.Timestamp, sid: str) -> float:
        i = weeks.searchsorted(np.datetime64(d), side="right") - 1
        return by.get(pd.Timestamp(weeks[i]), {}).get(sid, np.nan) if i >= 0 else np.nan
    return f


def _cost(value: float, price: float, sell: bool, rank: float) -> float:
    if value <= 0 or not np.isfinite(price) or price <= 0:
        return 0.0
    return ibkr_order_cost(value / price, price, sell=sell, hs=rank_half_spread(rank, price))["total"]


def run_insider(entries: pd.DataFrame, data, rank_of, *, start: str, end: str, account: float, k: int,
                hold: int) -> dict:
    """Event portfolio: buy at the entry close (NAV / k, cash permitting), sell at the close ``hold`` sessions later."""
    perf = data.sessions[(data.sessions >= pd.Timestamp(start)) & (data.sessions <= pd.Timestamp(end))]
    col = {s: i for i, s in enumerate(data.perf_idx.columns)}
    I = data.perf_idx.reindex(perf).to_numpy()
    P = data.close.reindex(perf).to_numpy()
    lr = data.last_row
    by_day = {d: g for d, g in entries.groupby("entry")} if len(entries) else {}
    cash = account
    pos = {}                      # sid -> [units, entry_i, entry_idx, entry_value]
    nav, cost, expo, npos, tv = (np.zeros(len(perf)) for _ in range(5))
    traded, trades, skipped_full, skipped_cash, skipped_held = 0.0, [], 0, 0, 0
    for i, d in enumerate(perf):
        c_day = 0.0
        for sid in [s for s in pos if pd.notna(lr.get(s)) and d > lr[s]]:
            u, ei, eidx, ev = pos.pop(sid)
            v = u * I[i, col[sid]]
            cash += v
            trades.append({"sid": sid, "entry": perf[ei], "exit": d, "reason": "delisted", "ret": I[i, col[sid]] / eidx - 1,
                           "pnl": v - ev})
        for sid in [s for s, p in pos.items() if i - p[1] >= hold]:
            u, ei, eidx, ev = pos.pop(sid)
            c = col[sid]
            v = u * I[i, c]
            k_ = _cost(v, P[i, c], True, rank_of(d, sid))
            cash += v - k_
            c_day += k_
            traded += v
            tv[i] += v
            trades.append({"sid": sid, "entry": perf[ei], "exit": d, "reason": "held_63", "ret": I[i, c] / eidx - 1,
                           "pnl": v - k_ - ev})
        if d in by_day:
            stock = sum(p[0] * I[i, col[s]] for s, p in pos.items())
            size = (cash + stock) / k
            for r in by_day[d].itertuples():
                sid = r.security_id
                if sid in pos:
                    skipped_held += 1
                    continue
                if len(pos) >= k:
                    skipped_full += 1
                    continue
                c = col.get(sid)
                if c is None or not np.isfinite(I[i, c]) or not np.isfinite(P[i, c]):
                    continue
                amt = min(size, cash)
                if amt < 0.2 * size or amt < 100:
                    skipped_cash += 1
                    continue
                k_ = _cost(amt, P[i, c], False, rank_of(d, sid))
                pos[sid] = [(amt - k_) / I[i, c], i, I[i, c], amt]
                cash -= amt
                c_day += k_
                traded += amt
                tv[i] += amt
        stock = sum(p[0] * I[i, col[s]] for s, p in pos.items())
        nav[i], cost[i] = cash + stock, c_day
        expo[i], npos[i] = (stock / nav[i] if nav[i] > 0 else 0.0), len(pos)
    open_ = [{"sid": s, "entry": perf[p[1]], "exit": pd.NaT, "reason": "open_at_end",
              "ret": I[-1, col[s]] / p[2] - 1, "pnl": p[0] * I[-1, col[s]] - p[3]} for s, p in pos.items()]
    return {"nav": pd.Series(nav, index=perf), "cost": pd.Series(cost, index=perf), "exposure": pd.Series(expo, index=perf),
            "n_positions": pd.Series(npos, index=perf), "traded": traded, "traded_day": pd.Series(tv, index=perf), "trades": pd.DataFrame(trades + open_),
            "skipped_full": skipped_full, "skipped_cash": skipped_cash, "skipped_already_held": skipped_held}


def run_targets(targets: dict, data, rank_of, *, start: str, end: str, account: float, band: float) -> dict:
    """Equal-weight target portfolio. ``targets``: trade session -> list of security ids (may be empty = cash).
    At each target session: sell names that left, then move every target name to NAV / n (skip moves < band x NAV)."""
    perf = data.sessions[(data.sessions >= pd.Timestamp(start)) & (data.sessions <= pd.Timestamp(end))]
    col = {s: i for i, s in enumerate(data.perf_idx.columns)}
    I = data.perf_idx.reindex(perf).to_numpy()
    P = data.close.reindex(perf).to_numpy()
    lr = data.last_row
    cash = account
    pos = {}                      # sid -> [units, entry_i, cost_basis]
    nav, cost, expo, npos, tv = (np.zeros(len(perf)) for _ in range(5))
    traded, trades, n_rebal = 0.0, [], 0

    def close_spell(sid, i, d, v_out, reason):
        u, ei, basis = pos.pop(sid)
        trades.append({"sid": sid, "entry": perf[ei], "exit": d, "reason": reason, "ret": v_out / basis - 1 if basis else
                       np.nan, "pnl": v_out - basis})

    for i, d in enumerate(perf):
        c_day = 0.0
        for sid in [s for s in pos if pd.notna(lr.get(s)) and d > lr[s]]:
            v = pos[sid][0] * I[i, col[sid]]
            cash += v
            close_spell(sid, i, d, v, "delisted")
        if d in targets:
            n_rebal += 1
            tgt = [s for s in dict.fromkeys(targets[d]) if s in col and np.isfinite(I[i, col[s]])
                   and np.isfinite(P[i, col[s]]) and not (pd.notna(lr.get(s)) and lr[s] < d)]
            for sid in [s for s in pos if s not in tgt]:
                c = col[sid]
                v = pos[sid][0] * I[i, c]
                k_ = _cost(v, P[i, c], True, rank_of(d, sid))
                cash += v - k_
                c_day += k_
                traded += v
                tv[i] += v
                close_spell(sid, i, d, v - k_, "left_target")
            total = cash + sum(p[0] * I[i, col[s]] for s, p in pos.items())
            want = total / len(tgt) if tgt else 0.0
            deltas = {s: want - (pos[s][0] * I[i, col[s]] if s in pos else 0.0) for s in tgt}
            for sid, dv in sorted(deltas.items(), key=lambda x: x[1]):       # sells first
                c = col[sid]
                if abs(dv) < band * total and sid in pos:
                    continue
                if dv < 0:
                    v = -dv
                    k_ = _cost(v, P[i, c], True, rank_of(d, sid))
                    frac = v / (pos[sid][0] * I[i, c])
                    pos[sid][2] *= (1 - frac)
                    pos[sid][0] -= v / I[i, c]
                    cash += v - k_
                else:
                    v = min(dv, cash)
                    if v < 100:
                        continue
                    k_ = _cost(v, P[i, c], False, rank_of(d, sid))
                    if sid in pos:
                        pos[sid][0] += (v - k_) / I[i, c]
                        pos[sid][2] += v
                    else:
                        pos[sid] = [(v - k_) / I[i, c], i, v]
                    cash -= v
                c_day += k_
                traded += v
                tv[i] += v
        stock = sum(p[0] * I[i, col[s]] for s, p in pos.items())
        nav[i], cost[i] = cash + stock, c_day
        expo[i], npos[i] = (stock / nav[i] if nav[i] > 0 else 0.0), len(pos)
    for s in list(pos):
        close_spell(s, len(perf) - 1, pd.NaT, pos[s][0] * I[-1, col[s]], "open_at_end")
    return {"nav": pd.Series(nav, index=perf), "cost": pd.Series(cost, index=perf), "exposure": pd.Series(expo, index=perf),
            "n_positions": pd.Series(npos, index=perf), "traded": traded, "traded_day": pd.Series(tv, index=perf), "trades": pd.DataFrame(trades),
            "n_rebalances": n_rebal}


def benchmark_nav(ticker: str, sessions: pd.DatetimeIndex, bench_dir: Path, end: str, account: float) -> pd.Series:
    """Buy-and-hold value of ``account`` in ``ticker`` (Yahoo chart in ``bench_dir``, total return, IBKR entry cost
    with a 2 bp (ONEQ) / 1 bp half-spread)."""
    df = parse_chart(bench_dir / f"chart_{ticker}.json", end)
    adj = pd.Series(df["adjclose"].to_numpy(float), index=pd.DatetimeIndex(df["date"])).reindex(sessions).ffill()
    px = pd.Series(df["close"].to_numpy(float), index=pd.DatetimeIndex(df["date"])).reindex(sessions).ffill()
    hs = 2e-4 if ticker == "ONEQ" else 1e-4
    k = ibkr_order_cost(account / px.iloc[0], px.iloc[0], sell=False, hs=hs)["total"]
    return (account - k) * adj / adj.iloc[0]
