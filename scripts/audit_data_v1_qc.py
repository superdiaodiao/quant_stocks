"""Data audit: cross-check the frozen data v1 against QuantConnect (docs/audit_data_v1_vs_quantconnect.md).

Design (pre-registration 1.8): QuantConnect draws its own sample from its own data (qc/data_audit/main.py)
and returns QC values only, as runtime statistics. Nothing from the local data is sent to QuantConnect.
This script does the comparison locally.

Subcommands:
  compare   read output/research_only/data_audit_qc/qc_runs/*.json (the runtime statistics of each QC run,
            saved by hand from the backtest result), match QC's tokens to data v1 and write
            output/research_only/data_audit_qc/{pairs.csv, splits.csv, delistings.csv, universe_diag.csv,
            summary.json}
Nothing here edits data v1.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
INPUTS = ROOT / "output/research_only/reversal_2012_2026/inputs"
CACHE = Path("/Users/bytedance/code/quant_stocks/research_cache/reversal_2012_2026")
OUT = ROOT / "output/research_only/data_audit_qc"
RUNS = OUT / "qc_runs"
BASE = date(2011, 12, 26)          # Monday; QC week number = (date - BASE).days // 7
DIAG = ("2014-06-27", "2019-06-28", "2024-06-28")


# ------------------------------------------------------------------ pure helpers (tested)

def week_index(dates: pd.Series, week_ends: list[str]) -> np.ndarray:
    """Index i of the week (week_ends[i-1], week_ends[i]] that holds each date (0 for dates up to week_ends[0])."""
    we = np.array(week_ends, dtype="datetime64[D]")
    d = pd.to_datetime(dates).values.astype("datetime64[D]")
    return np.searchsorted(we, d, side="left")


def weekly_returns(panel: pd.DataFrame, week_ends: list[str]) -> pd.DataFrame:
    """Compound daily total returns ``tr`` into weeks (previous week end, week end].

    One row per (security_id, wi): ret, n_rows, any_nan (a missing ``tr`` inside the window, i.e. the series
    starts there), last_date, has_prior (a row dated on or before the previous week end).
    """
    p = panel[["security_id", "date", "tr"]].copy()
    p["wi"] = week_index(p["date"], week_ends)
    p = p[p["wi"] < len(week_ends)]
    p["g"] = 1.0 + p["tr"].astype(float)
    first = p.groupby("security_id")["date"].min()
    agg = p.groupby(["security_id", "wi"]).agg(ret=("g", "prod"), n_rows=("g", "size"),
                                                n_nan=("g", lambda s: int(s.isna().sum())),
                                                last_date=("date", "max")).reset_index()
    agg["ret"] = agg["ret"] - 1.0
    agg["any_nan"] = agg["n_nan"] > 0
    prev_we = pd.Series([week_ends[i - 1] if i > 0 else "0000-00-00" for i in agg["wi"]], index=agg.index)
    agg["has_prior"] = agg["security_id"].map(first).astype(str) <= prev_we
    return agg.drop(columns="n_nan")


def factor_method_returns(g: pd.DataFrame) -> pd.Series:
    """Daily returns with a cash distribution handled the way a price-factor adjustment does it (QuantConnect,
    and most "adjusted close" series): close_t x split_t / (close_t-1 - div_t) - 1 on an ex-date, instead of
    data v1's CRSP-style (close_t x split_t + div_t) / close_t-1 - 1. Identical when there is no distribution.
    ``g`` is one security's rows in date order with close_raw, split_factor, div_cash and tr."""
    prev = g["close_raw"].shift(1)
    div = g["div_cash"].fillna(0.0)
    alt = g["close_raw"] * g["split_factor"].fillna(1.0) / (prev - div) - 1
    return alt.where(div > 0, g["tr"])


def qc_week(d) -> int:
    d = pd.Timestamp(d).date()
    return (d - BASE).days // 7


def join_chunks(stats: dict, prefix: str) -> str:
    """Join runtime statistics PREFIX00, PREFIX01, ... in order (chunks are cut at 200 characters)."""
    keys = sorted((k for k in stats if k.startswith(prefix) and k[len(prefix):].isdigit()),
                  key=lambda k: int(k[len(prefix):]))
    return "".join(stats[k] for k in keys)


def parse_token(tok: str) -> dict:
    """TICKER.week36.tr[.p<pr>][.s<ratio>][.d<dist bp>][.L<weekday>] -> dict."""
    raw = tok.split(".")
    parts = raw[:3]
    for x in raw[3:]:           # the split ratio is written with a decimal point ("s1.05"): re-join it
        if x.isdigit() and len(parts) > 3:
            parts[-1] += "." + x
        else:
            parts.append(x)
    out = {"ticker": parts[0], "week": int(parts[1], 36), "qc_tr_bp": None if parts[2] == "na" else float(parts[2]),
           "qc_pr_bp": None, "qc_split": None, "qc_dist_bp": None, "L": None, "wide": False}
    for x in parts[3:]:
        tag, val = x[0], x[1:]
        if tag == "p":
            out["qc_pr_bp"] = float(val)
        elif tag == "s":
            out["qc_split"] = float(val)
        elif tag == "d":
            out["qc_dist_bp"] = float(val)
        elif tag == "L":
            out["L"] = int(val)
        elif tag == "W" and val == "":
            out["wide"] = True
    if out["qc_pr_bp"] is None and out["qc_tr_bp"] is not None:
        out["qc_pr_bp"] = out["qc_tr_bp"]
    if out["L"] is not None and out["L"] <= 4:
        out["qc_last_date"] = (BASE + timedelta(days=7 * out["week"] + out["L"])).isoformat()
    return out


def stratum(t: dict) -> str:
    if t["L"] is not None:
        return "L"
    if t["qc_split"] is not None:
        return "S"
    if t["qc_dist_bp"] is not None:
        return "D"
    return "R"


# ------------------------------------------------------------------ compare

def load_runs() -> tuple[list[dict], dict, list[dict]]:
    tokens, diag, sums = [], {}, []
    for f in sorted(RUNS.glob("*.json")):
        st = json.loads(f.read_text())
        sums.append({"file": f.name, "SUM": st.get("SUM"), "PLEN": st.get("PLEN"), "PEND": st.get("PEND")})
        txt = join_chunks(st, "P")
        for tok in txt.split():
            t = parse_token(tok)
            t["run"] = f.stem
            tokens.append(t)
        for yy in ("14", "19", "24"):
            u = join_chunks(st, "U" + yy)
            if u:
                diag[yy] = u.split()
    return tokens, diag, sums


def compare() -> dict:
    tokens, diag, sums = load_runs()
    ws = pd.read_csv(INPUTS / "weekly_universe_summary.csv", usecols=["week_end"], dtype=str)
    week_ends = ["2011-12-30"] + sorted(ws["week_end"].unique())
    wk_to_wi = {qc_week(w): i for i, w in enumerate(week_ends)}
    uni = pd.read_csv(INPUTS / "weekly_universe_top300.csv.gz", dtype=str,
                      usecols=["week_end", "security_id", "ticker", "dv50_rank", "dv20_rank"])
    uni["wi"] = uni["week_end"].map({w: i for i, w in enumerate(week_ends)})
    panel = pd.read_csv(CACHE / "prices/daily_panel.csv.gz", dtype={"security_id": str, "date": str},
                        usecols=["security_id", "date", "tr", "split_factor", "div_cash", "close_raw", "n_sources", "src_primary"])
    wr = weekly_returns(panel, week_ends).set_index(["security_id", "wi"])
    uni_key = {(r.ticker, r.wi): r for r in uni.itertuples()}
    splits = pd.read_csv(INPUTS / "split_events.csv", dtype=str)
    splits["wi"] = week_index(splits["ex_date"], week_ends)
    dists = pd.read_csv(INPUTS / "special_distributions.csv", dtype=str, usecols=["security_id", "ex_date", "classification"])
    dists["wi"] = week_index(dists["ex_date"], week_ends)
    term = pd.read_csv(INPUTS / "terminal_returns_2012_2026.csv", dtype=str)
    master = pd.read_csv(INPUTS / "security_master.csv", dtype=str,
                         usecols=["security_id", "tickers_observed", "last_listed", "delist_date", "foreign_filer"])

    # ---------------- check A: weekly returns (R, S, D tokens)
    rows = []
    for t in tokens:
        if t["L"] is not None:
            continue
        wi = wk_to_wi.get(t["week"])
        r = dict(t, stratum=stratum(t), wi=wi, week_end=week_ends[wi] if wi is not None else None)
        u = uni_key.get((t["ticker"], wi)) if wi is not None else None
        if wi is None:
            r["status"] = "outside_our_weeks"
        elif u is None:
            r["status"] = "not_in_our_top300"
        else:
            r.update(security_id=u.security_id, dv50_rank=u.dv50_rank, dv20_rank=u.dv20_rank)
            key = (u.security_id, wi)
            if key not in wr.index or bool(wr.loc[key, "any_nan"]) or not bool(wr.loc[key, "has_prior"]):
                r["status"] = "ours_not_computable"
            else:
                r["our_bp"] = float(wr.loc[key, "ret"]) * 1e4
                r["diff_bp"] = r["qc_tr_bp"] - r["our_bp"]
                r["status"] = "compared"
        rows.append(r)
    pairs = pd.DataFrame(rows)
    # diagnostic: our weekly return re-computed with the price-factor convention for cash distributions
    need = set(pairs.loc[pairs["status"] == "compared", "security_id"])
    alt = {}
    for sid, g in panel[panel["security_id"].isin(need)].sort_values("date").groupby("security_id"):
        g = g.assign(alt=factor_method_returns(g), wi=week_index(g["date"], week_ends))
        alt[sid] = g.groupby("wi")["alt"].apply(lambda x: (1 + x.astype(float)).prod() - 1)
    pairs["our_bp_factor_method"] = [alt[r.security_id].get(r.wi, np.nan) * 1e4 if r.status == "compared" else np.nan
                                     for r in pairs.itertuples()]
    pairs["diff_factor_method_bp"] = pairs["qc_tr_bp"] - pairs["our_bp_factor_method"]
    comp = pairs[pairs["status"] == "compared"].copy()
    comp["abs_diff"] = comp["diff_bp"].abs()
    comp["year"] = comp["week_end"].str[:4]

    def rates(f):
        n = len(f)
        return {"n": int(n), "le50": round(float((f["abs_diff"] <= 50).mean()), 4) if n else None,
                "le200": round(float((f["abs_diff"] <= 200).mean()), 4) if n else None}

    a = {"tokens": int(len(pairs)), "by_status": pairs["status"].value_counts().to_dict(),
         "by_stratum_status": pairs.groupby(["stratum", "status"]).size().unstack(fill_value=0).to_dict("index"),
         "all": rates(comp), "by_stratum": {s: rates(g) for s, g in comp.groupby("stratum")},
         "by_year": {y: rates(g) for y, g in comp.groupby("year")},
         "abs_diff_quantiles_bp": {str(q): round(float(comp["abs_diff"].quantile(q)), 2)
                                   for q in (0.5, 0.75, 0.9, 0.95, 0.99, 1.0)} if len(comp) else {},
         "diff_mean_bp": round(float(comp["diff_bp"].mean()), 3) if len(comp) else None}

    # ---------------- check C: splits
    srow = []
    ours_split = splits[splits["event_type"].isin(["split", "reverse_split"])]
    for t in tokens:
        if t["qc_split"] is None:
            continue
        wi = wk_to_wi.get(t["week"])
        u = uni_key.get((t["ticker"], wi)) if wi is not None else None
        r = {"ticker": t["ticker"], "week_end": week_ends[wi] if wi is not None else None, "qc_ratio": t["qc_split"]}
        if u is None:
            r["status"] = "not_in_our_top300"
        else:
            ev = splits[(splits["security_id"] == u.security_id) & (splits["wi"] == wi)]
            r["security_id"] = u.security_id
            r["our_events"] = "; ".join(f"{e.ex_date}:{e.split_factor}:{e.event_type}" for e in ev.itertuples())
            hit = [e for e in ev.itertuples() if e.split_factor not in (None, "", "nan") and isinstance(e.split_factor, str)
                   and abs(float(e.split_factor) / t["qc_split"] - 1) <= 0.01]
            if hit:
                r["status"] = "both" if hit[0].event_type in ("split", "reverse_split") else "qc_split_matches_our_" + hit[0].event_type
            else:
                r["status"] = "qc_only"
        srow.append(r)
    # ours-only: our split in a week where QC returned a token for that ticker without a split
    for t in tokens:
        if t["qc_split"] is not None or t["L"] is not None:
            continue
        wi = wk_to_wi.get(t["week"])
        u = uni_key.get((t["ticker"], wi)) if wi is not None else None
        if u is None:
            continue
        ev = ours_split[(ours_split["security_id"] == u.security_id) & (ours_split["wi"] == wi)]
        for e in ev.itertuples():
            srow.append({"ticker": t["ticker"], "week_end": week_ends[wi], "security_id": u.security_id,
                         "qc_ratio": None, "our_events": f"{e.ex_date}:{e.split_factor}:{e.event_type}", "status": "ours_only"})
    split_df = pd.DataFrame(srow)
    # our split events (split / reverse_split) in our top-300 weeks, for the coverage denominator
    in_uni = set(zip(uni["security_id"], uni["wi"]))
    our_uni_splits = ours_split[[k in in_uni for k in zip(ours_split["security_id"], ours_split["wi"])]]
    c = {"qc_split_tokens": int(len(split_df[split_df["qc_ratio"].notna()])) if len(split_df) else 0,
         "by_status": split_df["status"].value_counts().to_dict() if len(split_df) else {},
         "our_split_events_in_top300_weeks": int(len(our_uni_splits))}

    # ---------------- check B: delistings
    drow = []
    term_t = term[term["last_price_date"].notna()].copy()
    last_row = panel.groupby("security_id")["date"].max()
    panel_by_sid = {k: g for k, g in panel.groupby("security_id")}
    # the mode-L reruns (no fill-forward) supersede the delisting tokens of the mode A and W runs when present
    use_l = any(t["run"].startswith("L_") for t in tokens)
    for t in tokens:
        if t["L"] is None or (use_l and not t["run"].startswith("L_")):
            continue
        wide = t["wide"] or t["run"].startswith("W_")
        r = {"ticker": t["ticker"], "qc_last_date": t.get("qc_last_date"), "qc_ret_bp": t["qc_tr_bp"], "L": t["L"],
             "scan": "wide_lt5" if wide else "top400", "run": t["run"]}
        m = term_t[term_t["ticker"] == t["ticker"]]
        if r["qc_last_date"] and len(m):
            dd = (pd.to_datetime(m["last_price_date"]) - pd.Timestamp(r["qc_last_date"])).dt.days
            m = m.assign(dd=dd)[dd.abs() <= 31]
        if not len(m) or not r["qc_last_date"]:
            qd = pd.Timestamp(r["qc_last_date"]) if r["qc_last_date"] else None
            # 1) a terminal row without a last price (no vendor price in data v1), matched on the delisting date
            m2 = term[(term["ticker"] == t["ticker"]) & term["last_price_date"].isna() & term["delist_date"].notna()]
            if qd is not None and len(m2):
                m2 = m2[(pd.to_datetime(m2["delist_date"]) - qd).dt.days.abs() <= 60]
            if qd is not None and len(m2):
                x = m2.iloc[0]
                r.update(security_id=x["security_id"], status_term=x["status"], terminal_type=x["terminal_type"],
                         our_delist_date=x["delist_date"], best_rank=x["best_rank"],
                         status="terminal_row_without_price")
                drow.append(r)
                continue
            # 2) the security master: is the name known to data v1 at all?
            hit = master[master["tickers_observed"].fillna("").str.split().apply(lambda v: t["ticker"] in v)]
            if qd is not None and len(hit):
                end = pd.to_datetime(hit["delist_date"].fillna(hit["last_listed"]), errors="coerce")
                hit = hit[(end - qd).dt.days.abs() <= 120]
            if len(hit):
                x = hit.iloc[0]
                sid = x["security_id"]
                r.update(security_id=sid, our_last_row=last_row.get(sid), our_delist_date=x["delist_date"],
                         foreign_filer=x["foreign_filer"], status="in_master_no_terminal_row")
            else:
                r["status"] = "not_in_our_master"
            drow.append(r)
            continue
        m = m.iloc[(m["dd"].abs()).argsort()].iloc[0]
        sid = m["security_id"]
        r.update(security_id=sid, our_last_price_date=m["last_price_date"], status_term=m["status"],
                 terminal_type=m["terminal_type"], terminal_return=m["terminal_return"],
                 our_last_row=last_row.get(sid), date_diff_days=int(m["dd"]))
        # our return from the last week end before QC's last date to our last price date, then with the
        # terminal return booked (QC's last bar is often the delisting-day value, e.g. the cash deal price)
        qd = r["qc_last_date"]
        wprev = max(w for w in week_ends if w < qd)
        g = panel_by_sid.get(sid)
        g = g[(g["date"] > wprev) & (g["date"] <= m["last_price_date"])] if g is not None else g
        px = float((1 + g["tr"].astype(float)).prod() - 1) if g is not None and len(g) else 0.0
        term_r = float(m["terminal_return"]) if m["status"] == "computed" and isinstance(m["terminal_return"], str) else 0.0
        r["our_ret_bp"] = px * 1e4
        r["our_ret_with_terminal_bp"] = ((1 + px) * (1 + term_r) - 1) * 1e4
        r["status"] = "matched"
        drow.append(r)
    dl = pd.DataFrame(drow)
    if len(dl) and "date_diff_days" in dl:
        dl["ret_diff_bp"] = dl["qc_ret_bp"] - dl.get("our_ret_with_terminal_bp")
    b = {"qc_delistings": int(len(dl)), "by_scan_status": dl.groupby(["scan", "status"]).size().unstack(fill_value=0).to_dict("index") if len(dl) else {}}
    if len(dl) and "date_diff_days" in dl:
        mt = dl[(dl["status"] == "matched") & (dl["scan"] == "top400")]
        b["date_diff_days"] = {"same": int((mt["date_diff_days"] == 0).sum()),
                               "ours_later": int((mt["date_diff_days"] > 0).sum()),
                               "ours_earlier": int((mt["date_diff_days"] < 0).sum()),
                               "abs_gt_5": int((mt["date_diff_days"].abs() > 5).sum())}
        b["ret_abs_diff_le50"] = int((mt["ret_diff_bp"].abs() <= 50).sum())
        b["ret_abs_diff_le200"] = int((mt["ret_diff_bp"].abs() <= 200).sum())
        b["by_terminal_status"] = mt["status_term"].value_counts().to_dict()
    d5 = term[term["status"] == "awaiting_d5"][["ticker", "security_id", "last_price_date", "best_rank", "terminal_type"]].copy()
    if len(dl) and "security_id" in dl:
        found = dl.dropna(subset=["security_id"]).set_index("security_id")
        d5["qc_last_date"] = d5["security_id"].map(found["qc_last_date"]) if "qc_last_date" in found else None
        d5["qc_ret_bp"] = d5["security_id"].map(found["qc_ret_bp"])
    b["d5_names"] = int(len(d5))
    b["d5_found_in_qc_delistings"] = int(d5["qc_last_date"].notna().sum()) if "qc_last_date" in d5 else 0

    # ---------------- check D: universe diagnostic
    ud = []
    for yy, ticks in diag.items():
        w = [x for x in DIAG if x[2:4] == yy][0]
        ours = uni[(uni["week_end"] == w)].copy()
        ours["dv50_rank"] = pd.to_numeric(ours["dv50_rank"])
        ours300 = set(ours[ours["dv50_rank"] <= 300]["ticker"])
        q = set(ticks[:300])
        ud.append({"week_end": w, "qc_n": len(q), "ours_n": len(ours300), "overlap": len(q & ours300),
                   "qc_only": " ".join(sorted(q - ours300))[:2000], "ours_only": " ".join(sorted(ours300 - q))[:2000]})
    udf = pd.DataFrame(ud)

    OUT.mkdir(parents=True, exist_ok=True)
    pairs.to_csv(OUT / "pairs.csv", index=False)
    split_df.to_csv(OUT / "splits.csv", index=False)
    dl.to_csv(OUT / "delistings.csv", index=False)
    d5.to_csv(OUT / "d5_names.csv", index=False)
    udf.to_csv(OUT / "universe_diag.csv", index=False)
    summary = {"runs": sums, "A_weekly_returns": a, "B_delistings": b, "C_splits": c,
               "D_universe": udf.drop(columns=["qc_only", "ours_only"]).to_dict("records") if len(udf) else []}
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("compare")
    a = ap.parse_args(argv)
    if a.cmd == "compare":
        print(json.dumps(compare(), indent=2, default=str)[:6000])


if __name__ == "__main__":
    sys.exit(main())
