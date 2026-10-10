"""Counts for docs/reversal_2012_2026_data_report_v2_1.md: what data version 2.1 (v2 + the Alpaca SIP fill) changed
against version 2, and the materiality test of plan section 0 (g) for the study robustness rerun.

Data only: counts of plan rows, name-weeks, slots, rows, flags, statuses and checks. No signal, strategy return,
portfolio return or cross-stock return aggregate is computed, and no vendor price level is written. The materiality
test counts how many universe name-days changed their own daily return (a count, not an average). Version 2 and 2.1
files are only read. Output (counts and IDs, may go to git): ``output/research_only/reversal_2012_2026/v2_1_report/``.

    PYTHONPATH=. .venv/bin/python scripts/reversal_data_v2_1_report.py
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
MAIN = Path("/Users/bytedance/code/quant_stocks")
V = {"v2": (MAIN / "research_cache/reversal_2012_2026_v2", ROOT / "output/research_only/reversal_2012_2026/inputs_v2"),
     "v2.1": (MAIN / "research_cache/reversal_2012_2026_v2_1",
              ROOT / "output/research_only/reversal_2012_2026/inputs_v2_1")}
ALPACA = MAIN / "research_cache/reversal_2012_2026_v2_1_alpaca"
OUT = ROOT / "output/research_only/reversal_2012_2026/v2_1_report"
# plan section 0 (g), set before any v2.1 number was seen
MAT_SHARE_PP = 0.0010         # model share moves by more than 0.10 percentage points in a year
MAT_SLOTS_ALL = 0.005         # top-250 sets differ in more than 0.5% of all slots
MAT_SLOTS_YEAR = 0.01         # ... or in more than 1% of one year's slots
MAT_DAYS = 0.001              # more than 0.1% of v2 universe name-days change their return by more than 1e-6
TR_EPS = 1e-6


def checks(inputs: Path) -> tuple[dict, dict]:
    v = json.loads((inputs / "validation_summary.json").read_text())
    return {c["check"]: c for c in v["checks"]}, v


def table_a(cache: Path) -> dict:
    return json.loads((cache / "universe/universe_summary.json").read_text())["check_6_three_estimates"]["by_year"]


def weekly(cache: Path) -> pd.DataFrame:
    return pd.read_csv(cache / "universe/weekly_listed.csv.gz",
                       usecols=["week_end", "security_id", "missing", "missing_reason", "evidence", "p_top250",
                                "dv50_rank", "dv20_rank"], dtype={"security_id": str})


def top250(inputs: Path) -> pd.DataFrame:
    t = pd.read_csv(inputs / "weekly_universe_top300.csv.gz", usecols=["week_end", "security_id", "dv50_rank", "dv20_rank"],
                    dtype={"security_id": str})
    return t[(t["dv50_rank"] <= 250) | (t["dv20_rank"] <= 250)][["week_end", "security_id"]]


def _pct(x) -> str:
    return "" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{100 * float(x):.3f}%"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    out: dict = {"built_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                 "no_returns_aggregated": True,
                 "note": "counts of plan rows, name-weeks, slots, rows, flags, statuses and checks only; no vendor price level"}
    # ---- 1. the 374 plan rows
    pr = pd.read_csv(ALPACA / "plan_rows.csv", dtype={"security_id": str})
    pr.to_csv(OUT / "plan_rows.csv", index=False)
    ent = pd.read_csv(ALPACA / "entity.csv", dtype={"security_id": str})
    keep = ["security_id", "symbols", "verdict", "failed_rules", "second_source", "kept_rows", "coverage", "max_gap",
            "a6_n", "a6_agree", "a6_level_n", "a7_n", "a7_hits", "delist_date", "delist_end_sessions",
            "post_delist_real", "first_date", "last_date"]
    ent[[c for c in keep if c in ent]].to_csv(OUT / "entity_verdicts.csv", index=False)
    post = pr[pr["need_sessions_from2016"] > 0]
    out["plan_rows"] = {
        "rows": int(len(pr)), "by_status": pr["alpaca_status"].value_counts().to_dict(),
        "by_group_status": pd.crosstab(pr["group"], pr["alpaca_status"]).to_dict("index"),
        "rows_with_pre2016_need": int((pr["need_sessions_pre2016"] > 0).sum()),
        "rows_need_only_from2016": int(((pr["need_sessions_pre2016"] == 0) & (pr["need_sessions_from2016"] > 0)).sum()),
        "need_sessions_from2016": int(post["need_sessions_from2016"].sum()),
        "need_sessions_from2016_held": int(post["alpaca_sessions_held"].sum()),
        "need_sessions_pre2016": int(pr["need_sessions_pre2016"].sum()),
        "ambiguous_rules": ent.loc[ent["verdict"] == "ambiguous", "failed_rules"].str.split().explode().value_counts().to_dict(),
        "second_source_of_filled": ent.loc[ent["verdict"] == "accepted", "second_source"].value_counts().to_dict(),
        "expected_top250_weeks": {s: round(float(g["expected_top250_weeks"].astype(float).sum()), 2)
                                  for s, g in pr.groupby("alpaca_status")},
    }
    # ---- 2. the 2% rule, v2 against v2.1
    tb, ta, cks = {}, {}, {}
    for ver, (cache, inputs) in V.items():
        c, raw = checks(inputs)
        cks[ver] = (c, raw)
        tb[ver] = c["universe_unfillable"]["numbers"]["by_year"]
        ta[ver] = table_a(cache)
    rows = []
    for y in sorted(tb["v2"], key=int):
        r = {"year": int(y)}
        for ver in V:
            b, a = tb[ver].get(y, {}), ta[ver].get(y, {})
            k = ver.replace(".", "_")
            r.update({f"{k}_B_model": b.get("share"), f"{k}_B_calibrated": b.get("share_calibrated"),
                      f"{k}_B_upper": b.get("share_upper"), f"{k}_A_model": a.get("model"),
                      f"{k}_A_calibrated": a.get("calibrated"), f"{k}_A_upper": a.get("upper")})
        r["v2_1_B_over_2pct"] = bool((r["v2_1_B_model"] or 0) > 0.02)
        r["v2_B_over_2pct"] = bool((r["v2_B_model"] or 0) > 0.02)
        rows.append(r)
    share = pd.DataFrame(rows)
    share.to_csv(OUT / "missing_share_by_year.csv", index=False)
    out["missing_share_by_year"] = share.to_dict("records")
    # ---- 3. name-weeks: v2 missing weeks that v2.1 prices; unknown-size weeks resolved
    w2, w21 = weekly(V["v2"][0]), weekly(V["v2.1"][0])
    key = ["week_end", "security_id"]
    j = w2[w2["missing"] == True].merge(  # noqa: E712
        w21[key + ["missing", "evidence", "dv50_rank", "dv20_rank"]].rename(
            columns={"missing": "m21", "evidence": "ev21", "dv50_rank": "dv50_21", "dv20_rank": "dv20_21"}), on=key, how="left")
    j["closed"] = j["m21"].eq(False)
    j["closed_top250"] = j["closed"] & ((j["dv50_21"] <= 250) | (j["dv20_21"] <= 250))
    j["year"] = j["week_end"].str[:4].astype(int)
    j["p"] = j["p_top250"].astype(float).fillna(0.0)
    j["unknown"] = j["evidence"].eq("unknown")
    j["unknown_resolved"] = j["unknown"] & (j["closed"] | ~j["ev21"].eq("unknown"))
    by = j.groupby("year").agg(v2_missing_name_weeks=("closed", "size"), closed_name_weeks=("closed", "sum"),
                               v2_expected_top250_slots=("p", "sum"),
                               expected_slots_closed=("p", lambda s: float(s[j.loc[s.index, "closed"]].sum())),
                               closed_and_ranked_top250=("closed_top250", "sum"),
                               v2_unknown_name_weeks=("unknown", "sum"), unknown_resolved=("unknown_resolved", "sum"))
    by = by.reset_index().round(2)
    by.to_csv(OUT / "gap_weeks_closed_by_year.csv", index=False)
    out["gap_weeks_closed_by_year"] = by.to_dict("records")
    out["gap_weeks_closed_by_reason"] = j.groupby("missing_reason").agg(
        v2_name_weeks=("closed", "size"), closed=("closed", "sum"), unknown=("unknown", "sum"),
        unknown_resolved=("unknown_resolved", "sum")).reset_index().to_dict("records")
    u21 = w21[(w21["missing"] == True) & w21["evidence"].eq("unknown")]  # noqa: E712
    out["unknown_missing_name_weeks"] = {"v2": int(j["unknown"].sum()), "v2.1": int(len(u21)),
                                         "v2_resolved_in_v2.1": int(j["unknown_resolved"].sum())}
    newm = w21[w21["missing"] == True].merge(w2[key + ["missing"]].rename(columns={"missing": "m2"}), on=key, how="left")  # noqa: E712
    out["name_weeks_missing_in_v2_1_not_in_v2"] = int((newm["m2"] != True).sum())  # noqa: E712
    # ---- 4. top-250 slots (materiality)
    t2, t21 = top250(V["v2"][1]), top250(V["v2.1"][1])
    t2["in2"], t21["in21"] = True, True
    tt = t2.merge(t21, on=key, how="outer")
    tt["year"] = tt["week_end"].str[:4].astype(int)
    tt["diff"] = tt["in2"].isna() | tt["in21"].isna()
    slots = tt.groupby("year").agg(v2_slots=("in2", "count"), v21_slots=("in21", "count"), differing=("diff", "sum")).reset_index()
    # a slot that changes hands counts once (one name in, one out)
    slots["slots_changed"] = (slots["differing"] / 2).round(1)
    slots["share_changed"] = slots["slots_changed"] / slots["v2_slots"]
    slots.to_csv(OUT / "top250_slots_by_year.csv", index=False)
    total_changed = float(slots["slots_changed"].sum())
    total_slots = int(slots["v2_slots"].sum())
    out["top250_slots"] = {"by_year": slots.to_dict("records"), "changed": total_changed, "slots": total_slots,
                           "share": total_changed / total_slots if total_slots else 0.0,
                           "names_entering": tt.loc[tt["in2"].isna(), "security_id"].value_counts().head(25).to_dict()}
    # ---- 5. panel: rows by source, Alpaca flags, two-source counts; universe name-days whose return changed
    cols = ["security_id", "date", "tr", "src_primary", "n_sources", "max_src_diff", "flags"]
    p2 = pd.read_csv(V["v2"][0] / "prices/daily_panel.csv.gz", usecols=cols, dtype={"security_id": str, "date": str,
                                                                                    "flags": str, "src_primary": str})
    p21 = pd.read_csv(V["v2.1"][0] / "prices/daily_panel.csv.gz", usecols=cols, dtype={"security_id": str, "date": str,
                                                                                      "flags": str, "src_primary": str})
    for p in (p2, p21):
        p["flags"] = p["flags"].fillna("")
    out["panel_rows"] = {"v2": int(len(p2)), "v2.1": int(len(p21)), "v2_securities": int(p2["security_id"].nunique()),
                         "v2.1_securities": int(p21["security_id"].nunique())}
    out["panel_rows_by_source"] = {"v2": p2["src_primary"].value_counts().to_dict(),
                                   "v2.1": p21["src_primary"].value_counts().to_dict()}
    al = p21[p21["src_primary"] == "alpaca"]
    out["alpaca_rows"] = {"rows": int(len(al)), "securities": int(al["security_id"].nunique()),
                          "by_year": al["date"].str[:4].value_counts().sort_index().to_dict(),
                          "filled_rows": int(al["flags"].str.contains("alpaca", regex=False).sum()
                                             - al["flags"].str.contains("v21_alpaca_primary", regex=False).sum())}
    out["flags"] = {f: {"v2": int(p2["flags"].str.contains(f, regex=False).sum()),
                        "v2.1": int(p21["flags"].str.contains(f, regex=False).sum())}
                    for f in ("disagree_unresolved", "v21_alpaca_primary:", "v21_third_vote:", "v21_splice_blank",
                              "v21_majority:", "v21_alpaca_minority", "v21_spinoff_unvalued", "alpaca_split_from_bars",
                              "alpaca_div_from_bars", "gap_return_blank", "v2_third_vote:")}
    for ver, p in (("v2", p2), ("v2.1", p21)):
        m = p[p["n_sources"] >= 2]
        out.setdefault("two_source", {})[ver] = {
            "name_days": int(len(p)), "name_days_2plus": int(len(m)),
            "agree_0p5pct": int((m["max_src_diff"] <= 0.005).sum()),
            "share_2plus_agree_of_2plus": float((m["max_src_diff"] <= 0.005).mean()) if len(m) else None}
    # universe name-days of v2: (security, date) inside a week in which v2 ranks the name in its top 250
    t2w = t2[key].copy()
    t2w["week_end"] = pd.to_datetime(t2w["week_end"])
    p2u = p2[["security_id", "date", "tr"]].copy()
    p2u["week_end"] = pd.to_datetime(p2u["date"]).dt.to_period("W-FRI").dt.end_time.dt.normalize()
    p2u = p2u.merge(t2w, on=["security_id", "week_end"], how="inner")
    m = p2u.merge(p21[["security_id", "date", "tr"]].rename(columns={"tr": "tr21"}), on=["security_id", "date"], how="left")
    a, b = m["tr"].astype(float), m["tr21"].astype(float)
    changed = ((a - b).abs() > TR_EPS) | (a.isna() != b.isna())
    out["universe_name_days"] = {"v2_universe_name_days": int(len(m)), "changed_return": int(changed.sum()),
                                 "share": float(changed.mean()) if len(m) else 0.0,
                                 "securities_with_changes": int(m.loc[changed, "security_id"].nunique())}
    # ---- 6. validate and terminal
    c2, v2 = cks["v2"]
    c21, v21 = cks["v2.1"]
    vt = pd.DataFrame([{"check": k, "v2": c2[k]["status"], "v2.1": c21.get(k, {}).get("status", "absent")} for k in c2])
    vt.to_csv(OUT / "validate_v2_v2_1.csv", index=False)
    out["validate"] = {"v2": {s: int((vt["v2"] == s).sum()) for s in ("pass", "fail", "no_input")},
                       "v2.1": {s: int((vt["v2.1"] == s).sum()) for s in ("pass", "fail", "no_input")},
                       "changed": vt[vt["v2"] != vt["v2.1"]].to_dict("records"),
                       "v2.1_generated_utc": v21.get("generated_utc")}
    nums = {}
    for k in ("multi_source_agreement", "review_queue", "terminal_open", "terminal_coverage", "universe_vendor_source",
              "universe_listed_gaps", "universe_proxy_margin", "universe_capture_coverage", "universe_nasdaq100",
              "panel_integrity", "candidates_resolved", "universe_fetch_margin", "universe_form25"):
        n2, n21 = c2.get(k, {}).get("numbers", {}), c21.get(k, {}).get("numbers", {})
        flat = {f: (n2.get(f), n21.get(f)) for f in n2 if not isinstance(n2.get(f), (dict, list))}
        nums[k] = {f: {"v2": x, "v2.1": y} for f, (x, y) in flat.items() if x != y}
    out["validate_numbers_changed"] = nums
    tr2 = pd.read_csv(V["v2"][1] / "terminal_returns_2012_2026.csv", dtype=str, keep_default_na=False)
    tr21 = pd.read_csv(V["v2.1"][1] / "terminal_returns_2012_2026.csv", dtype=str, keep_default_na=False)
    out["terminal_status"] = {"v2": tr2["status"].value_counts().to_dict(), "v2.1": tr21["status"].value_counts().to_dict()}
    mp = V["v2.1"][0] / "prefilter/tiingo_month2_plan.csv"
    if mp.exists():
        out["month2_plan_rebuilt_v2_1_rows"] = int(len(pd.read_csv(mp, dtype=str)))
    # ---- 7. materiality (plan section 0 (g))
    moves = [(r["year"], abs((r["v2_1_B_model"] or 0) - (r["v2_B_model"] or 0))) for r in rows]
    crossed = [r["year"] for r in rows if r["v2_B_over_2pct"] != r["v2_1_B_over_2pct"]]
    worst_year_slots = float(slots["share_changed"].max()) if len(slots) else 0.0
    tests = {"model_share_move_pp_max": max(x for _, x in moves) * 100,
             "model_share_move_over_0p10pp": [y for y, x in moves if x > MAT_SHARE_PP],
             "years_crossing_2pct": crossed,
             "top250_slots_share_all": out["top250_slots"]["share"], "top250_slots_share_worst_year": worst_year_slots,
             "universe_name_days_changed_share": out["universe_name_days"]["share"]}
    tests["material"] = bool(tests["model_share_move_over_0p10pp"] or crossed
                             or tests["top250_slots_share_all"] > MAT_SLOTS_ALL or worst_year_slots > MAT_SLOTS_YEAR
                             or tests["universe_name_days_changed_share"] > MAT_DAYS)
    out["materiality"] = tests
    (OUT / "summary.json").write_text(json.dumps(out, indent=1, default=str) + "\n")
    # ---- tables
    lines = ["#### Missing share of top-250 slots by year, v2 against v2.1 (Table B: validate check 6; Table A: step 12, "
             "every non-pending reason)", "",
             "| Year | B model v2 | B model v2.1 | B calibrated v2.1 | B upper v2 | B upper v2.1 | A model v2 | A model v2.1 | "
             "A upper v2 | A upper v2.1 | v2.1 over 2% |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in share.itertuples():
        lines.append(f"| {r.year} | {_pct(r.v2_B_model)} | {_pct(r.v2_1_B_model)} | {_pct(r.v2_1_B_calibrated)} | "
                     f"{_pct(r.v2_B_upper)} | {_pct(r.v2_1_B_upper)} | {_pct(r.v2_A_model)} | {_pct(r.v2_1_A_model)} | "
                     f"{_pct(r.v2_A_upper)} | {_pct(r.v2_1_A_upper)} | {'**yes**' if r.v2_1_B_over_2pct else 'no'} |")
    lines += ["", "#### v2 missing name-weeks that v2.1 prices, and unknown-size weeks resolved", "",
              "| Year | v2 missing | closed | v2 expected top-250 slots | of which closed | closed and in top 250 | "
              "v2 unknown-size | resolved |", "|---|---|---|---|---|---|---|---|"]
    for r in by.itertuples():
        lines.append(f"| {r.year} | {int(r.v2_missing_name_weeks):,} | {int(r.closed_name_weeks):,} | "
                     f"{r.v2_expected_top250_slots:,.2f} | {r.expected_slots_closed:,.2f} | {int(r.closed_and_ranked_top250):,} | "
                     f"{int(r.v2_unknown_name_weeks):,} | {int(r.unknown_resolved):,} |")
    lines += ["", "#### Top-250 slots that changed hands", "", "| Year | slots | changed | share |", "|---|---|---|---|"]
    for r in slots.itertuples():
        lines.append(f"| {r.year} | {int(r.v2_slots):,} | {r.slots_changed:,.1f} | {_pct(r.share_changed)} |")
    v = out["validate"]
    lines += ["", f"#### Validate: v2 {v['v2']} against v2.1 {v['v2.1']}", "", "| Check | v2 | v2.1 |", "|---|---|---|"]
    for r in v["changed"]:
        lines.append(f"| {r['check']} | {r['v2']} | {r['v2.1']} |")
    if not v["changed"]:
        lines.append("| (no check changed status) | | |")
    (OUT / "tables.md").write_text("\n".join(lines) + "\n")
    print(json.dumps({k: out[k] for k in ("plan_rows", "validate", "materiality", "unknown_missing_name_weeks",
                                          "alpaca_rows", "two_source")}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
