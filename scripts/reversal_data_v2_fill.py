"""Data version 2 of the reversal 2012-2026 data: corporate-action fixes, gap fills and third votes, applied inside
the reconcile step (plan step 9) when ``REVERSAL_DATA_VERSION=v2`` (owner decisions 2026-10-05, plan section 0).

Data only. Nothing here computes signals, strategy or portfolio returns, return rankings or spreads; a single-stock
daily total return ``tr`` is computed only to build each security's own canonical series, as in step 9. The rules
of version 1 are unchanged (plan section 0: version 2 is a robustness version):

1. **Fixes** (``INPUTS/v2_fixes.csv``, committed: SEC facts only, no price level). The 5 own errors and 8
   unrecorded corporate actions of docs/audit_data_v1_vs_quantconnect.md (2.6, 2.7). A distribution of another
   security's shares is valued as version 1 valued ZG 2015 and LBTYK 2015 (``REVIEW_EVENT_EXCEPTIONS``): the
   distributed security at its own close on the ex-date (its first regular-way session), booked as cash D on the
   parent's ex-date row (the CRSP convention of EBAY 2015-07-20's ``spinoff_cash``); the parent's own share ratio
   (UNTD's 1-for-7, LMCA 2016's 0.25 new shares) is S. ``tr`` is recomputed from the row's own prior close
   (C_prev = (C x S_old + D_old) / (1 + tr_old)), so no level is chained. The child close comes from, in order: the
   child's canonical v2 series, its WIKI raw file, the cached Yahoo v8 chart restored to raw, or a Yahoo chart
   fetched once into ``V2_FILL/raw/yahoo_child`` (owner decision 2026-10-02 covers Yahoo); which one, and whether a
   second source agrees within 0.5%, is written to ``reconcile/v2_fixes_applied.csv`` (local: it holds levels).
2. **Fills** (survey section 5). Sessions the version-1 sources leave empty, inside the security's listing, get a
   row from the archived Yahoo captures (``V2_FILL/series/archive``, built by reversal_data_v2_archive.py):
   archived ``table.csv`` first, then history pages, the latest capture holding the session. ``src_primary`` is
   ``archive`` (flag ``archive_csv`` / ``archive_page``); a row needs the raw volume too. Only whole weeks are filled: a calendar week gets fill rows
   only when every listed XNAS session of it has a row afterwards (the last week of a series that ends within 10
   days of its delisting counts to its last row). ``tr`` comes from the capture's own consecutive rows; a first fill
   row after a version-1 row keeps a return only when the capture also holds the previous session and its raw
   close there is within 0.5% of the version-1 close (else ``v2_splice_blank``). QuantQuote (``tr``) and
   companiesmarketcap (price return from market cap) are second votes: ``n_sources`` counts every source with a
   return that day and ``max_src_diff`` the widest gap; two that disagree beyond 0.5% (plus cent rounding below $1)
   with no majority flag ``disagree_unresolved``. Days only QuantQuote or companiesmarketcap cover have no raw level
   (QuantQuote is adjusted, companiesmarketcap is market cap), so they are not panel rows: they go to
   ``prices/v2_return_only.csv.gz`` (local) for the owner to decide on.
3. **Third votes** on version-1 ``disagree_unresolved`` days: archived Yahoo (independent of WIKI, Tiingo and the
   stored file, never of live Yahoo) and QuantQuote. When exactly one of the disagreeing sources agrees within 0.5%
   with an independent vote, the day takes that source's return (and its level when it is a vendor) and is flagged
   ``v2_third_vote:{voter}>{source}`` instead of ``disagree_unresolved``.

Securities that were not step-9 targets (version-1 ``not_candidate`` gaps) get a series when the archive fills
whole weeks for them; they are added to the step's ids with a minimal state.
"""
from __future__ import annotations

import gzip
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

from scripts import reversal_data_common as common

FILL = common.V2_FILL
SERIES = FILL / "series"
FIXES_NAME = "v2_fixes.csv"
TOL_R = 0.005
SPLICE_LEVEL = 0.005
LAST_WEEK_DAYS = 10
YAHOO_CHILD = FILL / "raw" / "yahoo_child"
# distributed securities with no local close: one Yahoo v8 chart each may be fetched (no other ticker is asked, so a
# reused ticker of today's holder is never read for an old security)
YAHOO_FETCH_OK = {"QRTEP", "QVCGP", "GLIBP", "LBRDP", "MAXN", "MAXNQ", "ENVXW"}
ARCHIVE = "archive"
FIX_COLUMNS = ["fix_id", "finding", "v1_item_id", "security_id", "ticker", "ex_date", "child_date", "kind",
               "new_shares_per_old", "children", "cash_per_share", "sec_url", "terms"]
APPLIED_COLUMNS = ["fix_id", "security_id", "ticker", "ex_date", "applied", "reason", "children_sources",
                   "children_second_source", "split_before", "split_after", "div_before", "div_after", "tr_before",
                   "tr_after"]


def log(message: str) -> None:
    print(f"[v2] {message}", flush=True)


# ======================================================================== pure helpers (tested)


def implied_prior_close(close: float, split: float, div: float, tr: float) -> float:
    """The prior close a row's ``tr`` was measured from: (C x S + D) / (1 + tr)."""
    if not (np.isfinite(tr) and tr > -1 and close > 0):
        return np.nan
    return (close * split + div) / (1.0 + tr)


def fixed_return(close: float, split: float, div: float, prior: float) -> float:
    if not (np.isfinite(prior) and prior > 0):
        return np.nan
    return (close * split + div) / prior - 1.0


def parse_children(text: str) -> list[tuple[list[str], float]]:
    """'LSXMA:1;LMCK|FWONK:2' -> [(['LSXMA'], 1.0), (['LMCK', 'FWONK'], 2.0)]: distributed shares per parent share;
    ``|`` lists the tickers the same security had (tried in order)."""
    out = []
    for part in str(text or "").split(";"):
        if ":" in part:
            tk, ratio = part.rsplit(":", 1)
            out.append(([t.strip().upper() for t in tk.split("|") if t.strip()], float(ratio)))
    return out


def rounding_allowance(close: float, prev: float) -> float:
    """Cent rounding's room in a daily return: 0.005 / C_t + 0.005 / C_{t-1} (version 1's stored-vote rule)."""
    if not (close > 0 and prev > 0):
        return 0.0
    return 0.005 / close + 0.005 / prev


def agree(a: float, b: float, allowance: float = 0.0) -> bool:
    return bool(np.isfinite(a) and np.isfinite(b) and abs(a - b) <= TOL_R + allowance)


def complete_weeks(dates: pd.DatetimeIndex, required: pd.DatetimeIndex) -> set:
    """The W-FRI calendar weeks all of whose ``required`` sessions are in ``dates``."""
    have = set(pd.DatetimeIndex(dates))
    req = pd.Series(required, index=required)
    out = set()
    for week, days in req.groupby(required.to_period("W-FRI")):
        if all(d in have for d in days):
            out.add(week)
    return out


def capture_record(rows: pd.DataFrame, sessions: pd.DatetimeIndex) -> pd.DataFrame:
    """One fill row per date from a security's archived captures: level, volume, S and D from the preferred capture
    (table.csv before pages, then the latest capture); ``tr`` from the preferred capture that holds both the date
    and the previous session, (C x S + D) / C_prev - 1 on that capture's own raw rows; D from any capture's dividend
    row that day (as paid). Columns: date, close_raw, volume_raw, split, div, tr, kind, capture."""
    if rows.empty:
        return pd.DataFrame(columns=["date", "close_raw", "volume_raw", "split", "div", "tr", "kind", "capture"])
    df = rows.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df[df["close_raw"] > 0]
    df["rank_kind"] = np.where(df["kind"].eq("csv"), 0, 1)
    df["capture"] = df["capture"].astype(str)
    pos = pd.Series(np.arange(len(sessions)), index=sessions)
    df["pos"] = df["date"].map(pos)
    df = df.dropna(subset=["pos"])
    df["pos"] = df["pos"].astype(int)
    # dividends: any capture's row that day (the table.csv files carry none; their dividend files and the pages do)
    divs = df[df["div"] > 0].groupby("date")["div"].max()
    df["div_any"] = df["date"].map(divs).fillna(0.0)
    df = df.sort_values(["capture", "pos"])
    prev_pos = df.groupby("capture")["pos"].shift(1)
    prev_close = df.groupby("capture")["close_raw"].shift(1)
    consecutive = (df["pos"] - prev_pos) == 1
    df["tr_own"] = np.where(consecutive, (df["close_raw"] * df["split"] + df["div_any"]) / prev_close - 1.0, np.nan)
    df = df.sort_values(["date", "rank_kind", "capture"], ascending=[True, True, False])
    level = df.groupby("date").head(1).set_index("date")
    ret = df[np.isfinite(df["tr_own"])].groupby("date").head(1).set_index("date")["tr_own"]
    vol = df[np.isfinite(df["volume_raw"])].groupby("date").head(1).set_index("date")["volume_raw"]
    out = pd.DataFrame({"close_raw": level["close_raw"], "split": level["split"], "div": level["div_any"],
                        "kind": level["kind"], "capture": level["capture"]})
    out["volume_raw"] = vol.reindex(out.index)
    out["tr"] = ret.reindex(out.index)
    return out.reset_index()[["date", "close_raw", "volume_raw", "split", "div", "tr", "kind", "capture"]]


def vote_returns(frame: pd.DataFrame | None, column: str, sessions: pd.DatetimeIndex) -> pd.Series:
    """A vote source's return into each date, only across consecutive sessions."""
    if frame is None or frame.empty:
        return pd.Series(dtype=float)
    s = frame.assign(date=pd.to_datetime(frame["date"])).drop_duplicates("date", keep="last").set_index("date")[column]
    s = s.astype(float).sort_index()
    s = s[(s > 0) & s.index.isin(sessions)]
    pos = pd.Series(sessions.get_indexer(s.index), index=s.index)
    r = s / s.shift(1) - 1.0
    return r[pos.diff() == 1]


# ======================================================================== child closes for the fixes


class ChildCloses:
    """Raw closes of a distributed security on a date, from local sources first."""

    def __init__(self, prices_dir: Path, intervals: pd.DataFrame, wiki_dir: Path, yahoo_raw: Path):
        self.prices_dir, self.wiki_dir, self.yahoo_raw = prices_dir, wiki_dir, yahoo_raw
        self.intervals = intervals
        self._yahoo: dict[str, pd.DataFrame] = {}

    def _canonical(self, ticker: str, day: pd.Timestamp) -> float | None:
        iv = self.intervals[self.intervals["ticker"].str.upper() == ticker]
        for sid in dict.fromkeys(iv["security_id"]):
            path = self.prices_dir / f"{sid}.csv"
            if path.exists():
                p = pd.read_csv(path, usecols=["date", "close_raw"])
                row = p[p["date"] == str(day.date())]
                if len(row):
                    return float(row["close_raw"].iloc[0])
        return None

    def _wiki(self, ticker: str, day: pd.Timestamp) -> float | None:
        path = self.wiki_dir / f"{ticker}.csv.gz"
        if not path.exists():
            return None
        p = pd.read_csv(path, usecols=["date", "close"])
        row = p[p["date"] == str(day.date())]
        return float(row["close"].iloc[0]) if len(row) else None

    def _yahoo_frame(self, ticker: str, fetch: bool) -> pd.DataFrame:
        if ticker in self._yahoo:
            return self._yahoo[ticker]
        from scripts.reversal_data_reconcile import yahoo_restore
        files = sorted(self.yahoo_raw.glob(f"{ticker}__*.json.gz")) + sorted(YAHOO_CHILD.glob(f"{ticker}__*.json.gz"))
        frame = pd.DataFrame(columns=["date", "close"])
        if not files and fetch:
            got = self._fetch_yahoo(ticker)
            files = [got] if got else []
        for f in files[-1:]:
            try:
                result = (json.loads(gzip.decompress(f.read_bytes())).get("chart") or {}).get("result") or []
                if result:
                    frame = yahoo_restore(result[0])[["date", "close"]]
            except Exception:  # noqa: BLE001
                pass
        self._yahoo[ticker] = frame
        return frame

    def _fetch_yahoo(self, ticker: str) -> Path | None:
        path = YAHOO_CHILD / f"{ticker}__v2.json.gz"
        if path.exists():
            return path
        if path.with_name(path.name + ".404").exists():
            return None
        common.RAW_INDEX, common.QUOTA_LEDGER = FILL / "raw_index.csv.gz", FILL / "quota_ledger.csv"
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?period1=1262304000&period2=1798761600"
               "&interval=1d&events=div%2Csplits")
        try:
            common.cached_get(url, path, source="yahoo", headers={"User-Agent": "Mozilla/5.0",
                                                                 "Accept": "application/json"},
                              limiter=common.SlidingWindowLimiter({2: 1}), symbol=ticker)
        except Exception as exc:  # noqa: BLE001
            log(f"yahoo child {ticker}: {type(exc).__name__}")
            return None
        return path

    def close(self, ticker: str, day: pd.Timestamp, fetch: bool = True) -> tuple[float | None, str, str]:
        """(close, source, second-source agreement Y/N/'') of ``ticker`` on ``day``."""
        found = []
        for name, getter in (("canonical", self._canonical), ("wiki", self._wiki)):
            try:
                v = getter(ticker, day)
            except Exception:  # noqa: BLE001
                v = None
            if v:
                found.append((name, v))
        arch = SERIES / "archive" / f"child_{ticker}.csv.gz"
        if arch.exists():
            a = pd.read_csv(arch)
            if len(a):
                a = a[pd.to_datetime(a["date"]) == day].sort_values("capture", ascending=False)
                if len(a):
                    found.append(("archive", float(a["close_raw"].iloc[0])))
        y = self._yahoo_frame(ticker, fetch=fetch and not found and ticker in YAHOO_FETCH_OK)
        row = y[y["date"] == day] if len(y) else y
        if len(row):
            found.append(("yahoo", float(row["close"].iloc[0])))
        if not found:
            return None, "", ""
        second = ""
        if len(found) > 1:
            second = "Y" if abs(found[1][1] / found[0][1] - 1) <= TOL_R else "N"
        return found[0][1], found[0][0], second


# ======================================================================== the hook


def load_fixes(inputs: Path) -> pd.DataFrame:
    path = inputs / FIXES_NAME
    if not path.exists():
        return pd.DataFrame(columns=FIX_COLUMNS)
    return pd.read_csv(path, dtype=str, keep_default_na=False)


NUMERIC = ("close_raw", "volume_raw", "split_factor", "div_cash", "tr", "n_sources", "max_src_diff")


def _read_prices(path: Path) -> pd.DataFrame:
    p = pd.read_csv(path, dtype={"date": str, "flags": str, "src_primary": str}, keep_default_na=False,
                    na_values=[""])
    for c in NUMERIC:
        if c in p:
            p[c] = p[c].astype(float)
    p["flags"] = p["flags"].fillna("")
    return p


def _add_flag(flags: str, flag: str) -> str:
    parts = [p for p in str(flags or "").split(";") if p]
    if flag not in parts:
        parts.append(flag)
    return ";".join(parts)


def _drop_flag(flags: str, flag: str) -> str:
    return ";".join(p for p in str(flags or "").split(";") if p and p != flag)


def apply_fixes(prices_dir: Path, inputs: Path, cache: Path, intervals: pd.DataFrame) -> pd.DataFrame:
    fixes = load_fixes(inputs)
    children = ChildCloses(prices_dir, intervals, cache / "wiki" / "by_ticker", cache / "raw" / "yahoo")
    rows = []
    for f in fixes.to_dict("records"):
        base = {"fix_id": f["fix_id"], "security_id": f["security_id"], "ticker": f["ticker"], "ex_date": f["ex_date"]}
        if f["kind"] == "series_extension":
            rows.append({**base, "applied": "by_fill", "reason": "rows come from the archive fill, if any"})
            continue
        path = prices_dir / f"{f['security_id']}.csv"
        if not path.exists():
            rows.append({**base, "applied": "N", "reason": "no canonical series"})
            continue
        p = _read_prices(path)
        k = p.index[p["date"] == f["ex_date"]]
        if not len(k):
            rows.append({**base, "applied": "N", "reason": "no canonical row on the ex-date"})
            continue
        k = int(k[0])
        day = pd.Timestamp(f.get("child_date") or f["ex_date"])
        value, sources, seconds, missing = float(f["cash_per_share"] or 0.0), [], [], []
        for aliases, ratio in parse_children(f["children"]):
            c = None
            for tk in aliases:
                c, src, second = children.close(tk, day, fetch=False)  # step 9 is offline: prefetch_children first
                if c is not None:
                    break
            if c is None:
                missing.append("|".join(aliases))
                continue
            value += ratio * c
            sources.append(f"{tk}:{src}")
            seconds.append(f"{tk}:{second or '-'}")
        if missing:
            rows.append({**base, "applied": "N", "reason": "no close for " + " ".join(missing) + " on the ex-date",
                         "children_sources": " ".join(sources)})
            continue
        C, S0, D0, tr0 = (float(p.at[k, "close_raw"]), float(p.at[k, "split_factor"]), float(p.at[k, "div_cash"]),
                          float(p.at[k, "tr"]) if pd.notna(p.at[k, "tr"]) else np.nan)
        prior = implied_prior_close(C, S0, D0, tr0)
        S1 = float(f["new_shares_per_old"]) if f["new_shares_per_old"] else 1.0
        D1 = value
        tr1 = fixed_return(C, S1, D1, prior)
        p.at[k, "split_factor"], p.at[k, "div_cash"], p.at[k, "tr"] = S1, D1, tr1
        p.at[k, "flags"] = _add_flag(p.at[k, "flags"], f"v2_fix:{f['fix_id']}")
        common.atomic_write(path, p.to_csv(index=False, float_format="%.10g").encode())
        rows.append({**base, "applied": "Y", "reason": f["kind"], "children_sources": " ".join(sources),
                     "children_second_source": " ".join(seconds), "split_before": S0, "split_after": S1,
                     "div_before": D0, "div_after": D1, "tr_before": tr0, "tr_after": tr1})
    out = pd.DataFrame(rows, columns=APPLIED_COLUMNS)
    log(f"fixes: {len(out)} rows, applied {int((out['applied'] == 'Y').sum())}")
    return out


def _listing_required(intervals: pd.DataFrame, sid: str, sessions: pd.DatetimeIndex, delist) -> pd.DatetimeIndex:
    iv = intervals[intervals["security_id"] == sid]
    mask = np.zeros(len(sessions), dtype=bool)
    for r in iv.itertuples():
        end = r.end_next_absent if isinstance(r.end_next_absent, str) and r.end_next_absent else r.end
        mask |= (sessions >= pd.Timestamp(r.start)) & (sessions <= pd.Timestamp(end))
    if delist is not None:
        mask &= sessions < delist
    return sessions[mask]


def fill_security(sid: str, canonical: pd.DataFrame | None, archive: pd.DataFrame, votes: dict[str, pd.Series],
                  required: pd.DatetimeIndex, sessions: pd.DatetimeIndex, delist) -> tuple[pd.DataFrame, dict, pd.DataFrame]:
    """The canonical file with fill rows added (see the module notes), the facts, and the return-only days."""
    canon = canonical if canonical is not None else pd.DataFrame(
        columns=["date", "close_raw", "volume_raw", "split_factor", "div_cash", "tr", "src_primary", "n_sources",
                 "max_src_diff", "flags"])
    have = pd.DatetimeIndex(pd.to_datetime(canon["date"])) if len(canon) else pd.DatetimeIndex([])
    rec = capture_record(archive, sessions)
    rec = rec[rec["date"].isin(required) & ~rec["date"].isin(have)]
    facts = {"archive_days_offered": int(len(rec))}
    # a panel row needs the raw volume too (dollar volume ranks; validate's panel integrity): rows without it do not
    # enter, so their week is not whole and stays missing
    facts["archive_days_without_volume"] = int((~np.isfinite(rec["volume_raw"].astype(float))).sum())
    rec = rec[np.isfinite(rec["volume_raw"].astype(float))]
    # whole weeks only; the series' last week counts to its last row when it ends near the delisting
    union = have.union(pd.DatetimeIndex(rec["date"]))
    req = required
    if len(union) and delist is not None and (delist - union.max()).days <= LAST_WEEK_DAYS:
        req = required[required <= union.max()]
    weeks_ok = complete_weeks(union, req)
    rec = rec[pd.DatetimeIndex(rec["date"]).to_period("W-FRI").isin(list(weeks_ok))] if len(rec) else rec
    facts["archive_days_filled"] = int(len(rec))
    new_rows = []
    by_date = canon.assign(_d=pd.to_datetime(canon["date"])).set_index("_d") if len(canon) else None
    arch_all = capture_record(archive, sessions).set_index("date") if len(archive) else None
    filled_dates = set(rec["date"])
    pos = pd.Series(np.arange(len(sessions)), index=sessions)
    for r in rec.itertuples():
        flags = ["archive_csv" if r.kind == "csv" else "archive_page"]
        tr = r.tr
        p = pos[r.date]
        prev_day = sessions[p - 1] if p > 0 else None
        if prev_day is not None and prev_day not in filled_dates:
            # a junction with a version-1 row (or a gap): keep the return only on a level match there
            if by_date is not None and prev_day in by_date.index and arch_all is not None and prev_day in arch_all.index:
                c1, c0 = float(by_date.at[prev_day, "close_raw"]), float(arch_all.at[prev_day, "close_raw"])
                if not (c1 > 0 and abs(c0 / c1 - 1) <= SPLICE_LEVEL):
                    tr, flags = np.nan, flags + ["v2_splice_blank"]
            elif not np.isfinite(tr):
                flags.append("gap_return_blank")
        n, diff = (1 if np.isfinite(tr) else 0), 0.0
        rets = {ARCHIVE: tr} if np.isfinite(tr) else {}
        price_ret = (r.close_raw * r.split / arch_all.at[prev_day, "close_raw"] - 1.0) \
            if (prev_day is not None and arch_all is not None and prev_day in arch_all.index) else np.nan
        allowance = rounding_allowance(r.close_raw, arch_all.at[prev_day, "close_raw"]) \
            if (prev_day is not None and arch_all is not None and prev_day in arch_all.index) else 0.0
        for name, series in votes.items():
            v = series.get(r.date, np.nan)
            if np.isfinite(v):
                rets[name] = v
        if len(rets) >= 2:
            # pairwise agreement; the companiesmarketcap vote is a price return, so it is set against the archive's
            # price return (C x S / C_prev - 1), the other pairs against total returns
            names = list(rets)
            agree_with = {k: 0 for k in names}
            widest = 0.0
            for i, a in enumerate(names):
                for b in names[i + 1:]:
                    va = price_ret if (a == ARCHIVE and b == "cmc") else rets[a]
                    vb = price_ret if (b == ARCHIVE and a == "cmc") else rets[b]
                    if not (np.isfinite(va) and np.isfinite(vb)):
                        continue
                    widest = max(widest, abs(va - vb))
                    if agree(va, vb, allowance):
                        agree_with[a] += 1
                        agree_with[b] += 1
            n, diff = len(names), widest
            if ARCHIVE in rets and agree_with[ARCHIVE] == 0:
                majority = [k for k in names if k != ARCHIVE and agree_with[k] >= 1]
                if "quantquote" in majority:
                    tr, flags = rets["quantquote"], flags + ["v2_majority:quantquote"]
                elif widest > TOL_R + allowance:
                    flags.append("disagree_unresolved")
        new_rows.append({"date": str(r.date.date()), "close_raw": r.close_raw, "volume_raw": r.volume_raw,
                         "split_factor": r.split, "div_cash": r.div, "tr": tr, "src_primary": ARCHIVE,
                         "n_sources": n, "max_src_diff": diff, "flags": ";".join(flags)})
    added = pd.DataFrame(new_rows, columns=canon.columns)
    out = pd.concat([canon, added], ignore_index=True) if len(added) else canon.copy()
    out = out.sort_values("date", kind="stable").reset_index(drop=True)
    # return-only days: QuantQuote / companiesmarketcap returns on listed sessions no level source holds
    have_after = set(pd.to_datetime(out["date"])) if len(out) else set()
    ro = []
    for name, series in votes.items():
        for d, v in series.items():
            if d in required and d not in have_after and np.isfinite(v):
                ro.append({"security_id": sid, "date": str(d.date()), "source": name, "return": v,
                           "kind": "total_return" if name == "quantquote" else "price_return_from_market_cap"})
    facts["return_only_days"] = len({x["date"] for x in ro})
    facts["rows_added"] = int(len(added))
    return out, facts, pd.DataFrame(ro)


def source_return(frame: pd.DataFrame | None, day: pd.Timestamp, sessions: pd.DatetimeIndex, stored: bool = False) -> float:
    """A version-1 source's own return into ``day`` (consecutive sessions only)."""
    if frame is None or frame.empty:
        return np.nan
    f = frame[(frame["date"] <= day)].tail(2)
    if len(f) < 2 or f["date"].iloc[-1] != day:
        return np.nan
    p = sessions.get_indexer([f["date"].iloc[0], day])
    if p[1] - p[0] != 1 or not f["close"].iloc[0] > 0:
        return np.nan
    c1, c0 = float(f["close"].iloc[-1]), float(f["close"].iloc[0])
    if stored:
        return c1 / c0 - 1.0
    return (c1 * float(f["split"].iloc[-1]) + float(f["div"].iloc[-1])) / c0 - 1.0


def third_votes(canon: pd.DataFrame, frames: dict, archive: pd.DataFrame, qq: pd.Series,
                sessions: pd.DatetimeIndex) -> tuple[pd.DataFrame, list[dict]]:
    """Resolve version-1 ``disagree_unresolved`` days with an independent vote (see the module notes)."""
    if canon.empty:
        return canon, []
    out = canon.copy()
    mask = out["flags"].fillna("").str.contains("disagree_unresolved", regex=False)
    if not mask.any():
        return out, []
    arch = capture_record(archive, sessions).set_index("date") if archive is not None and len(archive) else None
    log_rows = []
    for k in np.flatnonzero(mask.values):
        day = pd.Timestamp(out.at[k, "date"])
        rets = {}
        for name in ("wiki", "tiingo", "yahoo"):
            v = source_return(frames.get(name), day, sessions)
            if np.isfinite(v):
                rets[name] = v
        v = source_return(frames.get("stored"), day, sessions, stored=True)
        if np.isfinite(v):
            rets["stored"] = v
        voters = {}
        if arch is not None and day in arch.index and np.isfinite(arch.at[day, "tr"]):
            voters[ARCHIVE] = float(arch.at[day, "tr"])
        q = qq.get(day, np.nan) if qq is not None and len(qq) else np.nan
        if np.isfinite(q):
            voters["quantquote"] = float(q)
        entry = {"security_id": "", "date": str(day.date()), "sources": json.dumps({a: round(b, 6) for a, b in rets.items()}),
                 "voters": json.dumps({a: round(b, 6) for a, b in voters.items()}), "resolved": ""}
        chosen = None
        for voter, vv in voters.items():
            hits = [s for s, sv in rets.items() if agree(sv, vv) and not (voter == ARCHIVE and s == "yahoo")]
            misses = [s for s in rets if s not in hits]
            if voter == ARCHIVE and "yahoo" in rets and not agree(rets["yahoo"], vv):
                entry["note"] = "archived Yahoo differs from live Yahoo: one source, not a vote"
                continue
            if len(hits) == 1 and misses:
                chosen = (voter, hits[0])
                break
        if chosen is None:
            log_rows.append(entry)
            continue
        voter, src = chosen
        out.at[k, "tr"] = rets[src]
        if src in ("wiki", "tiingo", "yahoo"):
            f = frames[src]
            row = f[f["date"] == day].iloc[-1]
            out.at[k, "close_raw"], out.at[k, "split_factor"], out.at[k, "div_cash"] = (
                float(row["close"]), float(row["split"]), float(row["div"]))
            if np.isfinite(row["volume"]):
                out.at[k, "volume_raw"] = float(row["volume"])
            out.at[k, "src_primary"] = src
        out.at[k, "n_sources"] = int(out.at[k, "n_sources"] or 0) + 1
        out.at[k, "flags"] = _add_flag(_drop_flag(out.at[k, "flags"], "disagree_unresolved"),
                                       f"v2_third_vote:{voter}>{src}")
        entry["resolved"] = f"{voter}>{src}"
        log_rows.append(entry)
    return out, log_rows


def archive_frame(sid: str) -> pd.DataFrame:
    path = SERIES / "archive" / f"{sid}.csv.gz"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    df = df[~df["otc"].astype(str).str.lower().eq("true")]
    return df


def vote_frames(sid: str, sessions: pd.DatetimeIndex) -> dict[str, pd.Series]:
    out = {}
    qq = SERIES / "quantquote" / f"{sid}.csv.gz"
    if qq.exists():
        out["quantquote"] = vote_returns(pd.read_csv(qq), "close", sessions)
    cmc = SERIES / "cmc" / f"{sid}.csv.gz"
    if cmc.exists():
        out["cmc"] = vote_returns(pd.read_csv(cmc), "mcap", sessions)
    return out


def apply_v2(ids: list[str], states: dict, prep: dict, prices_dir: Path, out_dir: Path, inputs: Path, cache: Path,
             bundles: dict, frames_for) -> tuple[list[str], dict, dict]:
    """The version-2 hook of step 9 (run after ``run_securities``, before the tables). Returns the ids (with any
    securities the fill gives a first series), the states, and the facts for summary.json."""
    import pickle

    sessions = prep["sessions"]
    intervals = pd.read_csv(inputs / "ticker_intervals.csv", dtype=str, keep_default_na=False)
    master = pd.read_csv(inputs / "security_master.csv", dtype=str, keep_default_na=False).set_index("security_id")
    facts: dict = {"data_version": "v2"}
    # 1. fixes
    applied = apply_fixes(prices_dir, inputs, cache, intervals)
    common.atomic_write(out_dir / "v2_fixes_applied.csv", applied.to_csv(index=False).encode())
    facts["fixes"] = applied[["fix_id", "security_id", "ex_date", "applied", "reason"]].to_dict("records")
    # 2. fills and 3. third votes
    archive_ids = sorted(p.name[:-len(".csv.gz")] for p in (SERIES / "archive").glob("*.csv.gz")) \
        if (SERIES / "archive").exists() else []
    vote_ids = set()
    for name in ("quantquote", "cmc"):
        if (SERIES / name).exists():
            vote_ids |= {p.name[:-len(".csv.gz")] for p in (SERIES / name).glob("*.csv.gz")}
    ids_set = set(ids)
    fill_facts, third_log, return_only = {}, [], []
    added_ids = []
    for sid in sorted(set(archive_ids) | vote_ids):
        archive = archive_frame(sid) if sid in archive_ids else pd.DataFrame()
        votes = vote_frames(sid, sessions) if sid in vote_ids else {}
        path = prices_dir / f"{sid}.csv"
        # a step-9 target's file was just written by this run; any other file is a fill-only series of an earlier
        # run, rebuilt here from scratch (so a rerun gives the same series and the id stays in the step's ids)
        canon = _read_prices(path) if (path.exists() and sid in states) else None
        changed = False
        if canon is not None and len(canon) and canon["flags"].fillna("").str.contains("disagree_unresolved").any() and \
                (len(archive) or "quantquote" in votes):
            canon2, rows = third_votes(canon, frames_for(sid, bundles), archive, votes.get("quantquote"), sessions)
            for r in rows:
                r["security_id"] = sid
            third_log += rows
            if any(r["resolved"] for r in rows):
                canon, changed = canon2, True
        if len(archive) or votes:
            delist = master.at[sid, "delist_date"] if sid in master.index else ""
            delist = pd.Timestamp(delist) if delist else None
            required = _listing_required(intervals, sid, sessions, delist)
            out, f, ro = fill_security(sid, canon, archive, votes, required, sessions, delist)
            if f["rows_added"]:
                canon, changed = out, True
                fill_facts[sid] = f
            if len(ro):
                return_only.append(ro)
        if not changed:
            continue
        common.atomic_write(path, canon.to_csv(index=False, float_format="%.10g").encode())
        days = pd.to_datetime(canon["date"]).values.astype("datetime64[D]").astype(np.int32)
        if sid in states:
            st = states[sid]
            st["dates"] = days
            st["signature"] = str(st.get("signature", "")) + "|v2"
        else:
            st = {"security_id": sid, "events": [], "specials": [], "moves": [], "pairs": [], "dividends": [],
                  "signature": "v2_fill_only", "dates": days,
                  "summary": {"security_id": sid, "v2_fill_only": True}}
            states[sid] = st
            added_ids.append(sid)
        s = st["summary"]
        s.update({"rows": int(len(canon)), "first_date": str(canon["date"].iloc[0]), "last_date": str(canon["date"].iloc[-1]),
                  "rows_by_primary": {k: int(v) for k, v in canon["src_primary"].value_counts().items()},
                  "last_src": str(canon["src_primary"].iloc[-1]),
                  "last_volume_positive": bool((canon["volume_raw"].iloc[-1] or 0) > 0),
                  "ticker_last": s.get("ticker_last") or (master.at[sid, "first_ticker"] if sid in master.index else ""),
                  "v2": fill_facts.get(sid, {})})
        common.atomic_write(Path(str(out_dir / "per_security" / f"{sid}.pkl")),
                            pickle.dumps(st, protocol=pickle.HIGHEST_PROTOCOL))
    if return_only:
        ro = pd.concat(return_only, ignore_index=True)
        common.atomic_write(prices_dir / "v2_return_only.csv.gz", gzip.compress(ro.to_csv(index=False).encode(), mtime=0))
    tl = pd.DataFrame(third_log)
    common.atomic_write(out_dir / "v2_third_votes.csv", tl.to_csv(index=False).encode())
    facts["fill"] = {"securities_filled": len(fill_facts), "rows_added": int(sum(f["rows_added"] for f in fill_facts.values())),
                     "securities_new": len(added_ids), "new_ids": added_ids,
                     "return_only_days": int(sum(len(x) for x in return_only))}
    facts["third_votes"] = {"days_seen": int(len(tl)), "resolved": int(tl["resolved"].astype(bool).sum()) if len(tl) else 0}
    log(f"fill: {facts['fill']['securities_filled']} securities, {facts['fill']['rows_added']} rows "
        f"({len(added_ids)} new series); third votes {facts['third_votes']}")
    return sorted(set(ids) | set(added_ids)), states, facts


def post_tables(split_events: Path, reviewed_moves: Path, out_dir: Path, inputs: Path, prices_dir: Path | None = None) -> dict:
    """Bring the step's tables in line with the version-2 changes: the split/distribution rows and the review queue
    rows of each applied fix (SEC URL, the effective factor (C x S + D) / C, a note), and the R3 queue rows of the
    days a third vote settled. Returns counts."""
    prices_dir = prices_dir or split_events.parent.parent.parent / "research_cache"
    fixes = load_fixes(inputs).set_index("fix_id")
    applied_path = out_dir / "v2_fixes_applied.csv"
    applied = pd.read_csv(applied_path, dtype=str, keep_default_na=False) if applied_path.exists() else pd.DataFrame()
    facts = {"split_rows_updated": 0, "split_rows_added": 0, "queue_rows_closed_by_fix": 0, "queue_rows_closed_by_vote": 0}
    se = pd.read_csv(split_events, dtype=str, keep_default_na=False)
    rm = pd.read_csv(reviewed_moves, dtype=str, keep_default_na=False)
    for a in applied.to_dict("records") if len(applied) else []:
        if a.get("applied") != "Y":
            continue
        f = fixes.loc[a["fix_id"]]
        note = f"v2 fix {a['fix_id']} (2026-10-05): {f['terms']}"
        factor = ""
        try:
            C = None
            path = Path(str(prices_dir)) / f"{a['security_id']}.csv"
            if path.exists():
                pr = pd.read_csv(path, usecols=["date", "close_raw"])
                row = pr[pr["date"] == a["ex_date"]]
                C = float(row["close_raw"].iloc[0]) if len(row) else None
            if C:
                factor = f"{(C * float(a['split_after']) + float(a['div_after'])) / C:.6g}"
        except Exception:  # noqa: BLE001
            factor = ""
        hit = (se["security_id"] == a["security_id"]) & (se["ex_date"] == a["ex_date"])
        if hit.any():
            se.loc[hit, "event_type"] = "spinoff"
            se.loc[hit, "sec_url"] = f["sec_url"]
            se.loc[hit, "verified_at"] = "2026-10-05"
            if factor:
                se.loc[hit, "split_factor"] = factor
            se.loc[hit, "notes"] = se.loc[hit, "notes"].map(lambda n: (n + "; " if n else "") + note)
            facts["split_rows_updated"] += int(hit.sum())
        else:
            new = {c: "" for c in se.columns}
            new.update({"security_id": a["security_id"], "ticker": a["ticker"], "ex_date": a["ex_date"],
                        "split_factor": factor, "event_type": "spinoff", "agree": "N", "sec_url": f["sec_url"],
                        "verified_at": "2026-10-05", "notes": note})
            se = pd.concat([se, pd.DataFrame([new])], ignore_index=True)
            facts["split_rows_added"] += 1
        q = (rm["security_id"] == a["security_id"]) & (rm["event_date"] == a["ex_date"]) & \
            rm["classification"].isin(["", "unreviewed"])
        if f["v1_item_id"]:
            q |= rm["notes"].str.contains(re.escape(f["v1_item_id"]) + r"\b", regex=True) & \
                rm["classification"].isin(["", "unreviewed"])
        if q.any():
            rm.loc[q, "classification"] = "event_confirmed"
            rm.loc[q, "source_url"] = f["sec_url"]
            rm.loc[q, "verified_at"] = "2026-10-05"
            rm.loc[q, "notes"] = rm.loc[q, "notes"].map(lambda n: n + " | " + note)
            facts["queue_rows_closed_by_fix"] += int(q.sum())
    votes_path = out_dir / "v2_third_votes.csv"
    if votes_path.exists() and votes_path.stat().st_size > 1:
        tv = pd.read_csv(votes_path, dtype=str, keep_default_na=False)
        for v in tv[tv["resolved"].astype(bool)].to_dict("records") if "resolved" in tv else []:
            voter, src = v["resolved"].split(">")
            q = (rm["security_id"] == v["security_id"]) & (rm["event_date"] == v["date"]) & \
                rm["classification"].isin(["", "unreviewed"]) & rm["notes"].str.startswith("[R3]")
            if q.any():
                losers = [k for k in json.loads(v["sources"] or "{}") if k != src]
                rm.loc[q, "classification"] = "stored_error" if losers == ["stored"] else "vendor_error"
                rm.loc[q, "verified_at"] = "2026-10-05"
                rm.loc[q, "notes"] = rm.loc[q, "notes"].map(
                    lambda n: n + f" | v2 third vote (2026-10-05): {voter} agrees with {src} within 0.5%")
                facts["queue_rows_closed_by_vote"] += int(q.sum())
    common.atomic_write(split_events, se.to_csv(index=False).encode())
    common.atomic_write(reviewed_moves, rm.to_csv(index=False).encode())
    log(f"tables: {facts}")
    return facts


def prefetch_children() -> None:
    """Fetch, once, the Yahoo v8 charts of the distributed securities no local file holds (``YAHOO_FETCH_OK``),
    before the offline step 9 reads them. Owner decision 2026-10-02 covers Yahoo; one request every 2 s."""
    YAHOO_CHILD.mkdir(parents=True, exist_ok=True)
    c = ChildCloses(Path("."), pd.DataFrame(columns=["ticker", "security_id"]), Path("."), Path("."))
    for tk in sorted(YAHOO_FETCH_OK):
        path = c._fetch_yahoo(tk)
        log(f"yahoo child {tk}: {'cached' if path else 'none'}")


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["prefetch-children"]:
        prefetch_children()
    else:
        raise SystemExit("usage: reversal_data_v2_fill.py prefetch-children")
