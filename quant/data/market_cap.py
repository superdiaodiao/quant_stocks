"""Point-in-time market capitalisation for the Nasdaq panel (data rules in docs/research_ledger_megacap.md 0.2a).

Market cap at signal session s (first available source):

1. SEC shares (filed strictly before s, within 400 days; diluted weighted average -> basic weighted average -> dei
   cover shares -> balance-sheet shares) x raw close at the filing session, carried to s with the total-return
   index ratio; rejected when above ``shares_vs_float`` x the public-float value;
2. dei public float (filed before s) x index change since its measurement date;
3. the Nasdaq company-list market cap (as-of <= s, within 365 days) x index change;
4. 50-day median dollar volume x that week's median (market cap / dollar volume) of the top-50-by-dollar-volume
   names that have a market cap (also replaces a value above ``dv_ratio_max`` x that median).

Extracted unchanged from scripts/research_megacap.py. The thresholds that study scripts temporarily override
(``SHARES_VS_FLOAT``) are parameters here, not module state.
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path

import numpy as np
import pandas as pd

from quant.data import version
from quant.paths import CACHE_ROOT, MAIN_CHECKOUT

SHARE_CONCEPTS = (("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"),
                  ("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic"),
                  ("dei", "EntityCommonStockSharesOutstanding"),
                  ("us-gaap", "CommonStockSharesOutstanding"))
FRESH_DAYS = 400
LIST_DAYS = 365
MAX_MCAP = 6e12           # a proxy above $6T is a unit error: dropped
SHARES_VS_FLOAT = 2.5     # shares x price above 2.5x the public-float value -> shares rejected (data rule 0.2a)
DV_RATIO_MAX = 10.0       # market cap / dollar volume above 10x the week's top-50 median -> rejected (rule 0.2a)

# local SEC companyfacts copies, searched in this order (scripts/research_canslim_dev.py CF_DIRS)
COMPANYFACTS_DIRS = [CACHE_ROOT / "canslim_dev/sec_companyfacts",
                     MAIN_CHECKOUT / "cleaned_stocks_data/financial/sec_companyfacts_cache",
                     CACHE_ROOT / "holdout_2011_2019/sec_companyfacts",
                     CACHE_ROOT / "sue_lt_2020_2026/sec_companyfacts",
                     version.CACHE / "raw/sec/companyfacts"]
SHARES_CACHE = version.versioned(CACHE_ROOT / "megacap/sec_share_facts.csv.gz")
COMPANY_LISTS = version.CACHE / "prefilter/lists.csv.gz"


def facts_from_payload(cik: int, payload: dict) -> list:
    """Rows (cik, pri, end, filed, val, start): pri 0-3 = the share concepts, 9 = dei public float (USD)."""
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


def extract_share_facts(ciks, cache: Path = SHARES_CACHE, refresh: bool = False,
                        companyfacts_dirs=None) -> pd.DataFrame:
    """Long table cik, pri, end, filed, val, start from every local companyfacts copy of each CIK (copies are
    merged and de-duplicated). Cached in ``cache`` (with the CIK list beside it)."""
    dirs = COMPANYFACTS_DIRS if companyfacts_dirs is None else companyfacts_dirs
    cols = ["cik", "pri", "end", "filed", "val", "start"]
    want = sorted(set(int(c) for c in ciks))
    done = cache.with_suffix(".ciks.json")
    if cache.exists() and done.exists() and not refresh and set(want) <= set(json.loads(done.read_text())):
        return pd.read_csv(cache, dtype={"start": str})
    rows = []
    for k, cik in enumerate(want):
        for d in dirs:
            p = d / f"CIK{cik:010d}.json.gz"
            if not p.is_file():
                continue
            env = json.loads(gzip.decompress(p.read_bytes()))
            rows += facts_from_payload(cik, env.get("payload", env))
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


def load_company_lists(path: Path = COMPANY_LISTS) -> pd.DataFrame:
    """Nasdaq company-list snapshots: security_id, as_of, market_cap (> 0)."""
    if not path.exists():
        return pd.DataFrame(columns=["security_id", "as_of", "market_cap"])
    lists = pd.read_csv(path, dtype=str, keep_default_na=False)
    lists["market_cap"] = pd.to_numeric(lists["market_cap"], errors="coerce")
    lists["as_of"] = pd.to_datetime(lists["as_of_session"].where(lists["as_of_session"] != "", lists["snapshot_date"]))
    return lists.loc[lists["market_cap"] > 0, ["security_id", "as_of", "market_cap"]]


def value_at(frame: pd.DataFrame, sids, dates) -> np.ndarray:
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
                tr_idx: pd.DataFrame, dv50: pd.DataFrame, shares_vs_float: float = SHARES_VS_FLOAT,
                max_mcap: float = MAX_MCAP, dv_ratio_max: float = DV_RATIO_MAX) -> pd.DataFrame:
    """Add ``mcap`` and ``mcap_src`` (sec_shares / public_float / company_list / dollar_volume) to candidate rows
    (columns s, security_id, cik, dv50_rank). Every input is dated strictly before (filings) or at (prices) s.
    Each source is a dollar value at an anchor date carried to s with the total-return index ratio
    idx(s) / idx(anchor), which handles splits and special distributions; the dividend yield since the anchor
    (<= 400 days) is ignored."""
    c = cand.reset_index(drop=True).copy()
    c["qid"] = np.arange(len(c))
    c["mcap"], c["mcap_src"] = np.nan, ""
    # a share count filed before a series' first price uses its first price (the index ratio then cancels); a dollar
    # value (float, list market cap) dated before the series' first price cannot be carried forward: dropped
    idx_b = tr_idx.bfill()
    close_b = close.bfill()
    idx_s = value_at(tr_idx, c["security_id"], c["s"])
    has_cik = c["cik"].notna()
    if len(facts) and has_cik.any():
        got = latest_fact_asof(c.loc[has_cik, ["qid", "cik", "s"]], facts)
        # 1. shares x price (first share concept by priority)
        sh = got[got["pri"] < 9].sort_values(["qid", "pri"]).drop_duplicates("qid")
        v_sh = np.full(len(c), np.nan)
        if len(sh):
            q = sh["qid"].to_numpy()
            sids = c.loc[q, "security_id"]
            v_sh[q] = (sh["val"].to_numpy() * value_at(close_b, sids, sh["filed"]) * idx_s[q]
                       / value_at(idx_b, sids, sh["filed"]))
        # 2. public float x price change since its measurement date
        fl = got[got["pri"] == 9]
        v_fl = np.full(len(c), np.nan)
        if len(fl):
            q = fl["qid"].to_numpy()
            v_fl[q] = fl["val"].to_numpy() * idx_s[q] / value_at(tr_idx, c.loc[q, "security_id"], fl["end"])
        good = lambda v: np.isfinite(v) & (v > 0) & (v < max_mcap)  # noqa: E731
        # cross-check (data rule 0.2a): shares x price more than shares_vs_float x the float value is a unit error
        # or an ADR-ratio mismatch (ordinary shares x ADS price) -> the float value is used instead
        bad_sh = good(v_sh) & good(v_fl) & (v_sh > shares_vs_float * v_fl)
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
            v = m["market_cap"].to_numpy() * idx_s[q] / value_at(tr_idx, m["security_id"], m["as_of"])
            ok = np.isfinite(v) & (v > 0) & (v < max_mcap)
            c.loc[q[ok], "mcap"] = v[ok]
            c.loc[q[ok], "mcap_src"] = "company_list"
    # 4. dollar volume on the market-cap scale; a proxy whose market cap / dollar volume is more than dv_ratio_max x
    #    that week's median of the top-50 names is treated as an error and replaced by it
    c["dv50"] = value_at(dv50, c["security_id"], c["s"])
    for s, g in c.groupby("s"):
        top = g[(g["dv50_rank"] <= 50) & g["mcap"].notna() & (g["dv50"] > 0)]
        ratio = float((top["mcap"] / top["dv50"]).median()) if len(top) else np.nan
        if not np.isfinite(ratio):
            continue
        odd = g.index[g["mcap"].notna() & (g["dv50"] > 0) & (g["mcap"] / g["dv50"] > dv_ratio_max * ratio)]
        miss = g.index[g["mcap"].isna() & (g["dv50"] > 0)]
        c.loc[odd, "mcap_src"] = "dollar_volume_proxy_rejected"
        c.loc[miss, "mcap_src"] = "dollar_volume"
        c.loc[odd.union(miss), "mcap"] = c.loc[odd.union(miss), "dv50"] * ratio
    return c.drop(columns=["qid"])


def successor_ciks(uni: pd.DataFrame, terminal_returns: Path | None = None) -> dict:
    """predecessor CIK -> successor CIK for securities that continued as another security (terminal file
    ``continued_as``, e.g. Google Inc. -> Alphabet 2015-10), so a successor without its own filings yet uses the
    predecessor's share counts."""
    path = version.INPUTS / "terminal_returns_2012_2026.csv" if terminal_returns is None else terminal_returns
    term = pd.read_csv(path, dtype=str)
    term = term[term["continued_as"].notna() & term["cik"].notna()]
    cik_of = uni.dropna(subset=["cik"]).drop_duplicates("security_id").set_index("security_id")["cik"]
    out = {}
    for r in term.itertuples():
        succ = cik_of.get(r.continued_as)
        if succ is not None and str(int(float(succ))) != str(int(float(r.cik))):
            out[int(float(r.cik))] = int(float(succ))
    return out


def add_predecessor_facts(facts: pd.DataFrame, alias: dict) -> pd.DataFrame:
    """Copy each predecessor's facts under its successor's CIK."""
    extra = [facts[facts["cik"] == p].assign(cik=s) for p, s in alias.items()]
    return pd.concat([facts] + extra, ignore_index=True) if extra else facts
