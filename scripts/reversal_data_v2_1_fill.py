"""Data version 2.1 of the reversal 2012-2026 data: the Alpaca SIP rows enter the canonical series inside the reconcile
step (plan step 9) when ``REVERSAL_DATA_VERSION=v2.1`` (owner decision 2026-10-10, plan section 0 (d)-(f)).

Data only. Nothing here computes signals, strategy or portfolio returns, return rankings or spreads; a single-stock
daily total return is used only to build and check each security's own series, as in step 9.

Called from ``reversal_data_v2_fill.apply_v2`` after the v2 fixes and before the v2 archive fills, so Alpaca ranks
above the archived Yahoo captures. Only entity-confirmed series are read (``reversal_data_v2_1_alpaca.py confirm``
writes nothing for an ambiguous one). Per security:

1. **Third votes.** On a v1/v2 ``disagree_unresolved`` day, Alpaca votes (it is independent of WIKI, Tiingo, Yahoo
   and the stored file). When exactly one of the disagreeing sources agrees within 0.5% (plus cent rounding below
   $1), the day takes that source's return and level (``v21_third_vote:alpaca>{source}``).
2. **Days a source already holds.** Alpaca counts as one more source (``n_sources`` + 1, ``max_src_diff``). Where
   the row's primary ranks below Alpaca (Yahoo; WIKI from 2017-11-01; archive), Alpaca's return agrees and S and D
   are the same, Alpaca's raw close and volume become the level (``v21_alpaca_primary:{old}``, return kept). Where
   Alpaca disagrees with a single-source row, the day is ``disagree_unresolved`` (rule R3: two sources, no
   majority); where the row's own sources agreed, Alpaca is the minority (``v21_alpaca_minority``).
3. **Fills.** Listed sessions no source holds get Alpaca's row (``src_primary`` ``alpaca``), whole weeks only (the
   series' last week counts to its last row when it ends within 10 days of the delisting). The first fill row after
   an existing row keeps its return only when Alpaca's previous close is within 0.5% of the existing close
   (``v21_splice_blank``); after a gap the return is blank (``gap_return_blank``). Archived Yahoo captures, QuantQuote
   and companiesmarketcap vote as in v2: a majority against Alpaca that holds a total-return source (archive or
   QuantQuote) gives the day that source's return (``v21_majority:{source}``); a disagreement with no majority is
   ``disagree_unresolved``.

A security that was not a step-9 target gets a series when Alpaca fills whole weeks for it (added to the ids with a
minimal state, as v2 does for archive-only series).
"""
from __future__ import annotations

import pickle
from pathlib import Path

import numpy as np
import pandas as pd

from scripts import reversal_data_common as common
from scripts import reversal_data_v2_fill as v2fill

SERIES = common.V2_1_ALPACA / "series"
ALPACA = "alpaca"
TOL_R = 0.005
SPLICE_LEVEL = 0.005
LAST_WEEK_DAYS = 10
WIKI_FIRST_UNTIL = pd.Timestamp("2017-10-31")   # v1 precedence: WIKI first to 2017-10-31, Tiingo first after
SAME_SPLIT = 1e-6
SAME_DIV_ABS, SAME_DIV_REL = 0.001, 0.001


def log(message: str) -> None:
    print(f"[v2.1] {message}", flush=True)


# ======================================================================== pure helpers (tested)


def ranks_below_alpaca(src: str, day: pd.Timestamp) -> bool:
    """Plan section 0 (e): Tiingo -> Alpaca -> Yahoo -> WIKI from 2017-11-01; WIKI -> Tiingo -> Alpaca -> Yahoo
    before; archive (and anything later) always below Alpaca."""
    if src in ("yahoo", "archive"):
        return True
    if src == "wiki":
        return pd.Timestamp(day) > WIKI_FIRST_UNTIL
    return False


def same_actions(s1: float, d1: float, s2: float, d2: float) -> bool:
    s1, s2 = float(s1 or 1.0), float(s2 or 1.0)
    d1, d2 = float(d1 or 0.0), float(d2 or 0.0)
    return abs(s1 / s2 - 1) <= SAME_SPLIT and abs(d1 - d2) <= max(SAME_DIV_ABS, SAME_DIV_REL * abs(d2))


def alpaca_frame(sid: str) -> pd.DataFrame:
    path = SERIES / f"{sid}.csv.gz"
    if not path.exists():
        return pd.DataFrame(columns=["date", "close_raw", "volume_raw", "split", "div", "tr", "flags"])
    a = pd.read_csv(path, keep_default_na=False, na_values=[""])
    a["date"] = pd.to_datetime(a["date"])
    a["flags"] = a["flags"].fillna("").astype(str)
    for c in ("close_raw", "volume_raw", "split", "div", "tr"):
        a[c] = a[c].astype(float)
    return a


def third_votes(canon: pd.DataFrame, frames: dict, alp: pd.DataFrame, sessions: pd.DatetimeIndex) -> tuple[pd.DataFrame, list[dict]]:
    """Step 1 of the module notes."""
    out = canon.copy()
    if out.empty or alp.empty:
        return out, []
    mask = out["flags"].fillna("").str.contains("disagree_unresolved", regex=False)
    if not mask.any():
        return out, []
    a = alp.set_index("date")
    rows = []
    for k in np.flatnonzero(mask.values):
        day = pd.Timestamp(out.at[k, "date"])
        if day not in a.index or not np.isfinite(a.at[day, "tr"]):
            continue
        vote = float(a.at[day, "tr"])
        rets = {}
        for name in ("wiki", "tiingo", "yahoo"):
            v = v2fill.source_return(frames.get(name), day, sessions)
            if np.isfinite(v):
                rets[name] = v
        v = v2fill.source_return(frames.get("stored"), day, sessions, stored=True)
        if np.isfinite(v):
            rets["stored"] = v
        allow = v2fill.rounding_allowance(float(out.at[k, "close_raw"]),
                                          float(out.at[k - 1, "close_raw"]) if k > 0 else np.nan)
        hits = [s for s, sv in rets.items() if abs(sv - vote) <= TOL_R + allow]
        misses = [s for s in rets if s not in hits]
        entry = {"date": str(day.date()), "sources": {s: round(x, 6) for s, x in rets.items()},
                 "alpaca": round(vote, 6), "resolved": ""}
        if len(hits) == 1 and misses:
            src = hits[0]
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
            out.at[k, "flags"] = v2fill._add_flag(v2fill._drop_flag(out.at[k, "flags"], "disagree_unresolved"),
                                                  f"v21_third_vote:alpaca>{src}")
            entry["resolved"] = f"alpaca>{src}"
        rows.append(entry)
    return out, rows


def merge_existing(canon: pd.DataFrame, alp: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Step 2 of the module notes, on the days both hold (rows already settled by a third vote are left alone)."""
    out = canon.copy()
    facts = {"overlap_days": 0, "votes": 0, "agree": 0, "disagree": 0, "swapped": 0, "new_unresolved": 0,
             "minority": 0}
    if out.empty or alp.empty:
        return out, facts
    a = alp.set_index("date")
    dates = pd.to_datetime(out["date"])
    for k in np.flatnonzero(dates.isin(a.index).to_numpy()):
        day = dates.iloc[k]
        facts["overlap_days"] += 1
        flags = str(out.at[k, "flags"] or "")
        if "v21_third_vote" in flags:
            continue
        atr, ctr = float(a.at[day, "tr"]), float(out.at[k, "tr"]) if pd.notna(out.at[k, "tr"]) else np.nan
        if not (np.isfinite(atr) and np.isfinite(ctr)):
            continue
        prev = float(out.at[k - 1, "close_raw"]) if k > 0 else np.nan
        allow = v2fill.rounding_allowance(float(out.at[k, "close_raw"]), prev)
        diff = abs(atr - ctr)
        n_old = int(out.at[k, "n_sources"] or 0) if pd.notna(out.at[k, "n_sources"]) else 0
        old_diff = float(out.at[k, "max_src_diff"]) if pd.notna(out.at[k, "max_src_diff"]) else 0.0
        out.at[k, "n_sources"] = max(n_old, 1) + 1
        out.at[k, "max_src_diff"] = max(old_diff, diff)
        facts["votes"] += 1
        if diff <= TOL_R + allow:
            facts["agree"] += 1
            src = str(out.at[k, "src_primary"])
            vol = float(a.at[day, "volume_raw"])
            if ranks_below_alpaca(src, day) and np.isfinite(vol) and vol > 0 and same_actions(
                    a.at[day, "split"], a.at[day, "div"], out.at[k, "split_factor"], out.at[k, "div_cash"]):
                out.at[k, "close_raw"], out.at[k, "volume_raw"] = float(a.at[day, "close_raw"]), vol
                out.at[k, "src_primary"] = ALPACA
                out.at[k, "flags"] = v2fill._add_flag(flags, f"v21_alpaca_primary:{src}")
                facts["swapped"] += 1
        else:
            facts["disagree"] += 1
            if n_old <= 1:
                if "disagree_unresolved" not in flags:
                    facts["new_unresolved"] += 1
                out.at[k, "flags"] = v2fill._add_flag(flags, "disagree_unresolved")
            elif old_diff <= TOL_R + allow:
                out.at[k, "flags"] = v2fill._add_flag(flags, "v21_alpaca_minority")
                facts["minority"] += 1
    return out, facts


def fill_rows(canon: pd.DataFrame, alp: pd.DataFrame, votes: dict[str, pd.Series], required: pd.DatetimeIndex,
              sessions: pd.DatetimeIndex, delist) -> tuple[pd.DataFrame, dict]:
    """Step 3 of the module notes: the canonical file with Alpaca fill rows added, and the facts."""
    have = pd.DatetimeIndex(pd.to_datetime(canon["date"])) if len(canon) else pd.DatetimeIndex([])
    rec = alp[alp["date"].isin(required) & ~alp["date"].isin(have)]
    facts = {"alpaca_days_offered": int(len(rec))}
    rec = rec[np.isfinite(rec["volume_raw"].astype(float))]
    union = have.union(pd.DatetimeIndex(rec["date"]))
    req = required
    if len(union) and delist is not None and (delist - union.max()).days <= LAST_WEEK_DAYS:
        req = required[required <= union.max()]
    weeks_ok = v2fill.complete_weeks(union, req)
    if len(rec):
        rec = rec[pd.DatetimeIndex(rec["date"]).to_period("W-FRI").isin(list(weeks_ok))]
    facts["alpaca_days_filled"] = int(len(rec))
    a_all = alp.set_index("date")
    by_date = canon.assign(_d=pd.to_datetime(canon["date"])).set_index("_d") if len(canon) else None
    filled = set(rec["date"])
    pos = pd.Series(np.arange(len(sessions)), index=sessions)
    new_rows, unresolved, majority = [], 0, 0
    for r in rec.itertuples():
        flags = ["alpaca"] + [f for f in str(r.flags or "").split(";") if f]
        tr = r.tr
        p = pos[r.date]
        prev_day = sessions[p - 1] if p > 0 else None
        if prev_day is not None and prev_day not in filled:
            if by_date is not None and prev_day in by_date.index and prev_day in a_all.index:
                c1, c0 = float(by_date.at[prev_day, "close_raw"]), float(a_all.at[prev_day, "close_raw"])
                if not (c1 > 0 and abs(c0 / c1 - 1) <= SPLICE_LEVEL):
                    tr, flags = np.nan, flags + ["v21_splice_blank"]
            elif not np.isfinite(tr):
                flags.append("gap_return_blank")
        rets = {ALPACA: tr} if np.isfinite(tr) else {}
        prev_close = float(a_all.at[prev_day, "close_raw"]) if (prev_day is not None and prev_day in a_all.index) else np.nan
        price_ret = r.close_raw * r.split / prev_close - 1.0 if np.isfinite(prev_close) else np.nan
        allowance = v2fill.rounding_allowance(r.close_raw, prev_close) if np.isfinite(prev_close) else 0.0
        for name, series in votes.items():
            v = series.get(r.date, np.nan)
            if np.isfinite(v):
                rets[name] = v
        n, diff = (1 if np.isfinite(tr) else 0), 0.0
        if len(rets) >= 2:
            names = list(rets)
            agree_with = {k: 0 for k in names}
            widest = 0.0
            for i, x in enumerate(names):
                for y in names[i + 1:]:
                    vx = price_ret if (x == ALPACA and y == "cmc") else rets[x]
                    vy = price_ret if (y == ALPACA and x == "cmc") else rets[y]
                    if not (np.isfinite(vx) and np.isfinite(vy)):
                        continue
                    widest = max(widest, abs(vx - vy))
                    if abs(vx - vy) <= TOL_R + allowance:
                        agree_with[x] += 1
                        agree_with[y] += 1
            n, diff = len(names), widest
            if ALPACA in rets and agree_with[ALPACA] == 0 and widest > TOL_R + allowance:
                maj = [k for k in names if k != ALPACA and agree_with[k] >= 1]
                pick = next((k for k in ("archive", "quantquote") if k in maj), None)
                if pick is not None:
                    tr, flags = rets[pick], flags + [f"v21_majority:{pick}"]
                    majority += 1
                else:
                    flags.append("disagree_unresolved")
                    unresolved += 1
        new_rows.append({"date": str(r.date.date()), "close_raw": r.close_raw, "volume_raw": r.volume_raw,
                         "split_factor": r.split, "div_cash": r.div, "tr": tr, "src_primary": ALPACA,
                         "n_sources": n, "max_src_diff": diff, "flags": ";".join(flags)})
    cols = list(canon.columns) if len(canon.columns) else EMPTY_CANON
    added = pd.DataFrame(new_rows, columns=cols)
    out = pd.concat([canon, added], ignore_index=True) if len(added) else canon.copy()
    out = out.sort_values("date", kind="stable").reset_index(drop=True)
    facts.update({"rows_added": int(len(added)), "fill_unresolved": unresolved, "fill_majority": majority,
                  "fill_two_source": int((added["n_sources"] >= 2).sum()) if len(added) else 0})
    return out, facts


EMPTY_CANON = ["date", "close_raw", "volume_raw", "split_factor", "div_cash", "tr", "src_primary", "n_sources",
               "max_src_diff", "flags"]


def archive_votes(sid: str, sessions: pd.DatetimeIndex) -> dict[str, pd.Series]:
    """The v2 votes on an Alpaca row: archived Yahoo captures (their own total return), QuantQuote, companiesmarketcap."""
    out = dict(v2fill.vote_frames(sid, sessions))
    arch = v2fill.archive_frame(sid)
    if len(arch):
        rec = v2fill.capture_record(arch, sessions)
        s = rec.set_index("date")["tr"].astype(float)
        out["archive"] = s[np.isfinite(s)]
    return out


# ======================================================================== the hook


def apply_alpaca(ids: list[str], states: dict, prep: dict, prices_dir: Path, out_dir: Path, inputs: Path,
                 bundles: dict, frames_for) -> tuple[list[str], dict, dict]:
    sessions = prep["sessions"]
    intervals = pd.read_csv(inputs / "ticker_intervals.csv", dtype=str, keep_default_na=False)
    master = pd.read_csv(inputs / "security_master.csv", dtype=str, keep_default_na=False).set_index("security_id")
    sids = sorted(p.name[:-len(".csv.gz")] for p in SERIES.glob("*.csv.gz")) if SERIES.exists() else []
    per, added_ids, votes_log = [], [], []
    for sid in sids:
        alp = alpaca_frame(sid)
        if alp.empty:
            continue
        path = prices_dir / f"{sid}.csv"
        canon = v2fill._read_prices(path) if (path.exists() and sid in states) else pd.DataFrame(columns=EMPTY_CANON)
        f = {"security_id": sid, "in_states": sid in states, "rows_before": int(len(canon))}
        if len(canon):
            canon, tv = third_votes(canon, frames_for(sid, bundles), alp, sessions)
            for t in tv:
                votes_log.append({"security_id": sid, **{k: str(v) for k, v in t.items()}})
            f["third_votes_seen"] = len(tv)
            f["third_votes_resolved"] = sum(bool(t["resolved"]) for t in tv)
            canon, mf = merge_existing(canon, alp)
            f.update(mf)
        delist = master.at[sid, "delist_date"] if sid in master.index else ""
        delist = pd.Timestamp(delist) if delist else None
        required = v2fill._listing_required(intervals, sid, sessions, delist)
        canon, ff = fill_rows(canon, alp, archive_votes(sid, sessions), required, sessions, delist)
        f.update(ff)
        per.append(f)
        changed = ff["rows_added"] or f.get("swapped") or f.get("votes") or f.get("third_votes_resolved")
        if not changed or canon.empty:
            continue
        common.atomic_write(path, canon.to_csv(index=False, float_format="%.10g").encode())
        days = pd.to_datetime(canon["date"]).values.astype("datetime64[D]").astype(np.int32)
        if sid in states:
            st = states[sid]
            st["dates"] = days
            st["signature"] = str(st.get("signature", "")) + "|v2.1"
        else:
            st = {"security_id": sid, "events": [], "specials": [], "moves": [], "pairs": [], "dividends": [],
                  "signature": "v21_alpaca_fill_only", "dates": days,
                  "summary": {"security_id": sid, "v21_alpaca_fill_only": True}}
            states[sid] = st
            added_ids.append(sid)
        s = st["summary"]
        s.update({"rows": int(len(canon)), "first_date": str(canon["date"].iloc[0]),
                  "last_date": str(canon["date"].iloc[-1]),
                  "rows_by_primary": {k: int(v) for k, v in canon["src_primary"].value_counts().items()},
                  "last_src": str(canon["src_primary"].iloc[-1]),
                  "last_volume_positive": bool((canon["volume_raw"].iloc[-1] or 0) > 0),
                  "ticker_last": s.get("ticker_last") or (master.at[sid, "first_ticker"] if sid in master.index else ""),
                  "v21": {k: (int(v) if isinstance(v, (int, np.integer, bool)) else v) for k, v in f.items()}})
        common.atomic_write(Path(str(out_dir / "per_security" / f"{sid}.pkl")),
                            pickle.dumps(st, protocol=pickle.HIGHEST_PROTOCOL))
    pf = pd.DataFrame(per)
    common.atomic_write(out_dir / "v21_alpaca_applied.csv", pf.to_csv(index=False).encode())
    common.atomic_write(out_dir / "v21_third_votes.csv", pd.DataFrame(votes_log).to_csv(index=False).encode())

    def total(col):
        return int(pf[col].fillna(0).sum()) if col in pf else 0

    facts = {"series_read": len(sids), "securities_changed": int(len(pf)), "securities_new": len(added_ids),
             "new_ids": added_ids, "rows_added": total("rows_added"), "rows_swapped": total("swapped"),
             "overlap_votes": total("votes"), "overlap_agree": total("agree"), "overlap_disagree": total("disagree"),
             "new_unresolved_overlap": total("new_unresolved"), "fill_unresolved": total("fill_unresolved"),
             "fill_majority": total("fill_majority"), "fill_two_source": total("fill_two_source"),
             "third_votes_seen": total("third_votes_seen"), "third_votes_resolved": total("third_votes_resolved")}
    log(f"alpaca: {facts}")
    return sorted(set(ids) | set(added_ids)), states, facts
