"""Data version 2.1 of the reversal 2012-2026 data (v2 + the Alpaca SIP fill): the version switch, the entity
confirmation rules A1-A7 of plan section 0 (2026-10-10), Alpaca's canonical record, and the precedence / second-source
rules of the fill (scripts/reversal_data_v2_1_alpaca.py, scripts/reversal_data_v2_1_fill.py)."""
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from scripts import reversal_data_v2_1_alpaca as alp
from scripts import reversal_data_v2_1_fill as v21

ROOT = Path(__file__).resolve().parents[1]


def _sessions(start="2019-01-01", end="2019-03-31"):
    import exchange_calendars as xcals
    cal = xcals.get_calendar("XNAS", start="2018-01-02", end="2020-12-31")
    s = pd.DatetimeIndex(cal.sessions).tz_localize(None)
    return s[(s >= start) & (s <= end)]


def _paths_for(version: str) -> str:
    env = {**os.environ, "REVERSAL_DATA_VERSION": version, "PYTHONPATH": str(ROOT)}
    code = ("from scripts import reversal_data_common as c; "
            "print(c.DATA_VERSION, c.CACHE.name, c.INPUTS.name, c.V2_PLUS, c.V2_1)")
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True).stdout.strip()


# ------------------------------------------------------------------ version switch


def test_v2_1_has_its_own_copies_and_keeps_the_v2_rules():
    assert _paths_for("v2.1") == "v2.1 reversal_2012_2026_v2_1 inputs_v2_1 True True"
    assert _paths_for("v2") == "v2 reversal_2012_2026_v2 inputs_v2 True False"
    assert _paths_for("v1") == "v1 reversal_2012_2026 inputs False False"


# ------------------------------------------------------------------ entity rules (judge)


def _ok_facts(**kw):
    f = {"kept_rows": 500, "clash_share": 0.0, "delist_in_window": True, "delist_end_sessions": 1,
         "post_delist_real": 0, "transfer_or_successor": False, "coverage": 0.99, "max_gap": 2,
         "max_gap_jump": 0.05, "a6_n": 100, "a6_agree": 99, "a6_level_n": 100, "a6_level_median": 0.001,
         "a7_n": 10, "a7_hits": 10}
    f.update(kw)
    return f


def test_judge_accepts_a_clean_series_and_labels_its_second_sources():
    verdict, failed = alp.judge(_ok_facts())
    assert verdict == "accepted" and failed == []
    assert alp.second_source_label(_ok_facts()) == "confirmed_returns+last_sale"
    bare = _ok_facts(a6_n=0, a6_agree=0, a6_level_n=0, a6_level_median=np.nan, a7_n=1, a7_hits=0)
    assert alp.judge(bare)[0] == "accepted"
    assert alp.second_source_label(bare) == "confirmed_dates_only"


def test_judge_no_bars():
    assert alp.judge(_ok_facts(kept_rows=0)) == ("no_bars", [])


def test_judge_a2_ticker_clash_above_ten_percent():
    assert alp.judge(_ok_facts(clash_share=0.10))[0] == "accepted"
    assert alp.judge(_ok_facts(clash_share=0.11))[1] == ["A2_ticker_clash"]


def test_judge_a4_must_end_near_the_delisting():
    assert alp.judge(_ok_facts(delist_end_sessions=10))[0] == "accepted"
    assert alp.judge(_ok_facts(delist_end_sessions=11))[1] == ["A4_ends_early"]
    assert alp.judge(_ok_facts(delist_end_sessions=None))[1] == ["A4_ends_early"]
    # not delisted inside the window: no end rule
    assert alp.judge(_ok_facts(delist_in_window=False, delist_end_sessions=None))[0] == "accepted"


def test_judge_a4_amended_accepts_an_end_at_the_last_listed_snapshot():
    # Form 25 effective weeks after a trading suspension: the series ends with the Nasdaq listing, not at delist_date
    f = _ok_facts(delist_end_sessions=17, listing_end_sessions=0)
    assert alp.judge(f)[0] == "accepted"
    assert alp.judge(f, amended=False)[1] == ["A4_ends_early"]
    assert alp.judge(_ok_facts(delist_end_sessions=30, listing_end_sessions=11))[1] == ["A4_ends_early"]
    assert alp.judge(_ok_facts(delist_end_sessions=30, listing_end_sessions=None))[1] == ["A4_ends_early"]


def test_judge_a4_real_bars_after_the_delisting_unless_a_transfer_or_successor():
    assert alp.judge(_ok_facts(post_delist_real=3))[1] == ["A4_bars_after_delisting"]
    assert alp.judge(_ok_facts(post_delist_real=3, transfer_or_successor=True))[0] == "accepted"


def test_judge_a5_continuity():
    assert alp.judge(_ok_facts(coverage=0.89))[1] == ["A5_coverage"]
    assert alp.judge(_ok_facts(max_gap=11))[1] == ["A5_gap"]
    assert alp.judge(_ok_facts(max_gap_jump=0.51))[1] == ["A5_gap_jump"]


def test_judge_a6_returns_and_level():
    assert alp.judge(_ok_facts(a6_n=100, a6_agree=95))[0] == "accepted"
    assert alp.judge(_ok_facts(a6_n=100, a6_agree=94))[1] == ["A6_returns"]
    # fewer than 5 return days: the return share is not judged, the level still is
    assert alp.judge(_ok_facts(a6_n=4, a6_agree=0))[0] == "accepted"
    assert alp.judge(_ok_facts(a6_n=4, a6_agree=0, a6_level_n=3, a6_level_median=0.03))[1] == ["A6_level"]


def test_judge_a7_last_sale():
    assert alp.judge(_ok_facts(a7_n=10, a7_hits=8))[0] == "accepted"
    assert alp.judge(_ok_facts(a7_n=10, a7_hits=7))[1] == ["A7_last_sale"]
    assert alp.judge(_ok_facts(a7_n=2, a7_hits=0))[0] == "accepted"  # fewer than 3 snapshots: not judged


def test_judge_lists_every_failed_rule():
    _, failed = alp.judge(_ok_facts(clash_share=0.5, max_gap=40, a6_agree=10))
    assert failed == ["A2_ticker_clash", "A5_gap", "A6_returns"]


# ------------------------------------------------------------------ entity helpers


def test_interval_mask_uses_start_to_end_next_absent_and_skips_blank_starts():
    s = _sessions("2019-01-01", "2019-02-28")
    iv = pd.DataFrame([{"start": "2019-01-10", "end": "2019-01-20", "end_next_absent": "2019-01-31"},
                       {"start": "", "end": "2019-02-28", "end_next_absent": ""}])
    m = alp.interval_mask(s, iv)
    assert s[m].min() == pd.Timestamp("2019-01-10") and s[m].max() == pd.Timestamp("2019-01-31")
    iv2 = pd.DataFrame([{"start": "2019-02-01", "end": "2019-02-15", "end_next_absent": ""}])
    assert alp.interval_mask(s, iv2).sum() == len(s[(s >= "2019-02-01") & (s <= "2019-02-15")])


def test_filler_rows_after_the_last_trade_are_flagged():
    close = np.array([1.0, 1.1, 0.9, 0.1345, 0.1345, 0.1345])
    vol = np.array([100.0, 120.0, 90.0, 5000.0, 0.0, 0.0])
    assert alp.filler_flags(close, vol).tolist() == [False, False, False, False, True, True]


def test_overlap_returns_count_agreement_with_cent_rounding_and_levels():
    d = pd.to_datetime(["2019-01-02", "2019-01-03", "2019-01-04"])
    a = pd.DataFrame({"date": d, "close_raw": [10.0, 10.1, 10.2], "tr": [np.nan, 0.01, 0.0099]})
    o = pd.DataFrame({"date": d, "close_raw": [10.0, 10.1, 10.25], "tr": [np.nan, 0.0101, 0.02]})
    r = alp.overlap_returns(a, o)
    assert r["n"] == 2 and r["agree"] == 1 and r["level_n"] == 3
    # below $1 the cent-rounding room widens the tolerance
    a2 = pd.DataFrame({"date": d[:2], "close_raw": [0.20, 0.21], "tr": [np.nan, 0.05]})
    o2 = pd.DataFrame({"date": d[:2], "close_raw": [0.20, 0.21], "tr": [np.nan, 0.09]})
    assert alp.overlap_returns(a2, o2)["agree"] == 1


def test_snapshot_check_accepts_an_adjacent_session():
    s = _sessions("2019-01-01", "2019-01-31")
    a = pd.DataFrame({"date": s, "close_raw": np.linspace(10, 12, len(s))})
    snaps = pd.DataFrame({"as_of_session": [str(s[5].date()), str(s[10].date()), str(s[15].date())],
                          "last_sale": [float(a.close_raw[6]), float(a.close_raw[10]) * 1.5, float(a.close_raw[15])]})
    r = alp.snapshot_check(a, snaps, s)
    assert r == {"n": 3, "hits": 2}


def test_continuity_finds_gaps_and_jumps_across_them():
    s = _sessions("2019-01-01", "2019-02-28")
    keep = s.delete(range(10, 22))  # 12 listed sessions missing
    closes = np.where(keep < s[22], 10.0, 20.0)
    c = alp.continuity(keep, closes, np.ones(len(keep)), s)
    assert c["max_gap"] == 12 and abs(c["max_gap_jump"] - 1.0) < 1e-9 and c["coverage"] < 0.9
    # a split inside the gap is not a jump
    c2 = alp.continuity(keep, closes, np.where(keep == s[22], 0.5, 1.0), s)
    assert c2["max_gap_jump"] < 1e-9


def test_window_starts_no_earlier_than_2016_and_reaches_past_the_delisting():
    w = alp.window_of({"needed_start": "2016-02-01", "needed_end": "2019-05-01"}, "2019-06-14")
    assert w[0] == pd.Timestamp("2016-01-04") and w[1] == pd.Timestamp("2019-07-14")
    assert alp.window_of({"needed_start": "2012-01-01", "needed_end": "2015-12-31"}, "") is None


# ------------------------------------------------------------------ canonical record


def _bars(dates, closes, vols=None):
    return pd.DataFrame({"date": pd.DatetimeIndex(dates), "close": closes,
                         "volume": vols if vols is not None else [1000.0] * len(closes)})


def test_canonical_record_split_dividend_and_spinoff():
    s = _sessions("2019-01-02", "2019-01-10")  # 2,3,4,7,8,9,10
    raw = _bars(s, [100.0, 101.0, 50.0, 51.0, 50.0, 49.0, 48.0])
    split_adj = _bars(s, [50.0, 50.5, 50.0, 51.0, 50.0, 49.0, 48.0])        # 2-for-1 on 01-04
    all_adj = split_adj.assign(close=[49.0, 49.49, 49.0, 49.98, 50.0, 49.0, 48.0])  # dividend factor 0.98 to 01-07
    acts = {"forward_splits": [{"ex_date": "2019-01-04", "new_rate": 2, "old_rate": 1}],
            "cash_dividends": [{"ex_date": "2019-01-08", "rate": 1.0}],
            "spin_offs": [{"ex_date": "2019-01-10"}]}
    rec = alp.canonical_record(raw, split_adj, all_adj, acts, s).set_index("date")
    assert rec.at[s[2], "split"] == 2.0 and abs(rec.at[s[2], "tr"] - (50 * 2 / 101 - 1)) < 1e-12
    assert rec.at[s[4], "div"] == 1.0 and abs(rec.at[s[4], "tr"] - (51.0 / 51.0 - 1)) < 1e-12
    assert np.isnan(rec.at[s[0], "tr"]) and np.isnan(rec.at[s[6], "tr"])
    assert "v21_spinoff_unvalued" in rec.at[s[6], "flags"]


def test_canonical_record_split_from_bars_and_mismatch():
    s = _sessions("2019-01-02", "2019-01-07")
    raw = _bars(s, [30.0, 31.0, 10.0, 10.5])
    split_adj = _bars(s, [10.0, 10.3333, 10.0, 10.5])  # an unrecorded 3-for-1 on 01-04
    rec = alp.canonical_record(raw, split_adj, split_adj, {}, s).set_index("date")
    assert rec.at[s[2], "split"] == 3.0 and "alpaca_split_from_bars" in rec.at[s[2], "flags"]
    # a record the bars contradict blanks the return
    acts = {"forward_splits": [{"ex_date": str(s[2].date()), "new_rate": 2, "old_rate": 1}]}
    rec2 = alp.canonical_record(raw, split_adj, split_adj, acts, s).set_index("date")
    assert "alpaca_split_mismatch" in rec2.at[s[2], "flags"] and np.isnan(rec2.at[s[2], "tr"])


def test_canonical_record_dividend_from_bars_and_no_return_across_a_gap():
    s = _sessions("2019-01-02", "2019-01-08")  # 2,3,4,7,8
    raw = _bars(s[[0, 1, 3, 4]], [20.0, 20.0, 20.0, 19.8])
    split_adj = raw.copy()
    all_adj = raw.assign(close=[19.8, 19.8, 19.8, 19.8])  # factor 0.99 before 01-08: D = 0.01 x 20
    rec = alp.canonical_record(raw, split_adj, all_adj, {}, s).set_index("date")
    assert abs(rec.at[s[4], "div"] - 0.2) < 1e-9 and "alpaca_div_from_bars" in rec.at[s[4], "flags"]
    assert abs(rec.at[s[4], "tr"]) < 1e-12
    assert np.isnan(rec.at[s[3], "tr"])  # 01-04 is missing: no return into 01-07


def test_canonical_record_ignores_rounding_noise_in_the_adjusted_closes():
    s = _sessions("2019-01-02", "2019-01-08")
    raw = _bars(s, [20.0, 20.1, 20.2, 20.1, 20.0])
    all_adj = raw.assign(close=[19.997, 20.1, 20.2, 20.1, 20.0])  # a 0.015% wobble on one day, then back
    rec = alp.canonical_record(raw, raw, all_adj, {}, s)
    assert (rec["div"] == 0).all() and not rec["flags"].str.contains("div_from_bars").any()


def test_bars_frame_reads_one_symbol():
    body = json.dumps({"bars": {"ACET": [{"t": "2019-01-02T05:00:00Z", "c": 0.88, "v": 226086},
                                         {"t": "2019-01-03T05:00:00Z", "c": 0.9811, "v": 233362}]},
                       "next_page_token": None}).encode()
    f = alp.bars_frame(body, "ACET")
    assert f["date"].dt.strftime("%Y-%m-%d").tolist() == ["2019-01-02", "2019-01-03"] and f["close"].iloc[1] == 0.9811
    assert alp.bars_frame(body, "XXXX").empty


# ------------------------------------------------------------------ fill: precedence and second source


def test_precedence_alpaca_next_to_tiingo():
    assert v21.ranks_below_alpaca("yahoo", pd.Timestamp("2016-05-02"))
    assert v21.ranks_below_alpaca("archive", pd.Timestamp("2016-05-02"))
    assert not v21.ranks_below_alpaca("tiingo", pd.Timestamp("2020-05-01"))
    assert not v21.ranks_below_alpaca("wiki", pd.Timestamp("2017-10-31"))
    assert v21.ranks_below_alpaca("wiki", pd.Timestamp("2017-11-01"))


def _canon(dates, closes, trs, src, n=1, diff=0.0, flags=""):
    return pd.DataFrame({"date": [str(d.date()) for d in dates], "close_raw": closes, "volume_raw": 1000.0,
                         "split_factor": 1.0, "div_cash": 0.0, "tr": trs, "src_primary": src, "n_sources": n,
                         "max_src_diff": diff, "flags": flags})


def _alp(dates, closes, trs, vol=2000.0):
    return pd.DataFrame({"date": pd.DatetimeIndex(dates), "close_raw": closes, "volume_raw": vol, "split": 1.0,
                         "div": 0.0, "tr": trs, "flags": ""})


def test_merge_existing_swaps_level_below_alpaca_and_counts_a_second_source():
    s = _sessions("2019-01-02", "2019-01-04")
    c = _canon(s, [10.0, 10.1, 10.2], [np.nan, 0.01, 0.0099], ["yahoo", "yahoo", "tiingo"])
    a = _alp(s, [10.01, 10.11, 10.21], [np.nan, 0.01, 0.0099])
    out, f = v21.merge_existing(c, a)
    assert f["votes"] == 2 and f["agree"] == 2 and f["swapped"] == 1
    assert out.at[1, "src_primary"] == "alpaca" and out.at[1, "close_raw"] == 10.11 and out.at[1, "volume_raw"] == 2000.0
    assert "v21_alpaca_primary:yahoo" in out.at[1, "flags"]
    assert out.at[2, "src_primary"] == "tiingo" and out.at[2, "close_raw"] == 10.2   # Tiingo stays first
    assert (out["n_sources"].iloc[1:] == 2).all()


def test_merge_existing_disagreement_single_source_is_unresolved_majority_holds_otherwise():
    s = _sessions("2019-01-02", "2019-01-04")
    c = _canon(s, [10.0, 10.1, 10.2], [np.nan, 0.01, 0.0099], "yahoo", n=[1, 1, 2], diff=[0, 0, 0.001])
    a = _alp(s, [10.0, 10.5, 10.2], [np.nan, 0.05, -0.03])
    out, f = v21.merge_existing(c, a)
    assert f["disagree"] == 2 and f["new_unresolved"] == 1 and f["minority"] == 1 and f["swapped"] == 0
    assert "disagree_unresolved" in out.at[1, "flags"] and "v21_alpaca_minority" in out.at[2, "flags"]
    assert out.at[1, "src_primary"] == "yahoo"


def test_fill_rows_whole_weeks_only_and_splice_rule():
    s = _sessions("2019-01-07", "2019-01-25")  # three weeks: 5 + 5 + 4 sessions (2019-01-21 is a holiday)
    c = _canon(s[:5], [10.0] * 5, [np.nan, 0, 0, 0, 0], "yahoo")
    a = _alp(s[3:13], [10.0, 10.0, 10.3] + [10.3] * 7, [np.nan, 0.0, 0.03] + [0.0] * 7)  # misses s[13]
    out, f = v21.fill_rows(c, a, {}, s, s, None)
    added = out[out["src_primary"] == "alpaca"]
    # week 2 is whole (s[5..9]); week 3 lacks s[14] so none of it enters
    assert added["date"].tolist() == [str(d.date()) for d in s[5:10]]
    assert added.iloc[0]["tr"] == 0.03  # Alpaca's close on s[4] equals the existing close: return kept
    a2 = a.assign(close_raw=np.where(a["date"] == s[4], 11.0, a["close_raw"]))
    out2, _ = v21.fill_rows(c, a2, {}, s, s, None)
    first = out2[out2["src_primary"] == "alpaca"].iloc[0]
    assert np.isnan(first["tr"]) and "v21_splice_blank" in first["flags"]


def test_fill_rows_votes_majority_and_unresolved():
    s = _sessions("2019-01-07", "2019-01-11")
    a = _alp(s, [10.0, 10.1, 10.2, 10.3, 10.4], [np.nan, 0.01, 0.0099, 0.0098, 0.0097])
    arch = pd.Series({s[1]: 0.05, s[2]: 0.0099})
    qq = pd.Series({s[1]: 0.05, s[3]: 0.08})
    out, f = v21.fill_rows(pd.DataFrame(columns=v21.EMPTY_CANON), a, {"archive": arch, "quantquote": qq}, s, s, None)
    o = out.set_index("date")
    d = lambda k: str(s[k].date())  # noqa: E731
    assert o.at[d(1), "tr"] == 0.05 and "v21_majority:archive" in o.at[d(1), "flags"] and o.at[d(1), "n_sources"] == 3
    assert o.at[d(2), "n_sources"] == 2 and "disagree" not in o.at[d(2), "flags"]
    assert "disagree_unresolved" in o.at[d(3), "flags"]
    assert f["fill_two_source"] == 3 and f["fill_unresolved"] == 1 and f["fill_majority"] == 1


def test_third_vote_settles_a_two_source_disagreement():
    s = _sessions("2019-01-02", "2019-01-04")
    c = _canon(s, [10.0, 10.1, 10.2], [np.nan, 0.01, 0.0099], "tiingo", n=2, diff=0.04,
               flags=["", "disagree_unresolved", ""])
    frames = {"tiingo": pd.DataFrame({"date": s, "close": [10.0, 10.1, 10.2], "split": 1.0, "div": 0.0, "volume": 5.0}),
              "yahoo": pd.DataFrame({"date": s, "close": [10.0, 10.5, 10.2], "split": 1.0, "div": 0.0, "volume": 6.0})}
    a = _alp(s, [10.0, 10.1, 10.2], [np.nan, 0.01, 0.0099])
    out, rows = v21.third_votes(c, frames, a, s)
    assert rows[0]["resolved"] == "alpaca>tiingo"
    assert "v21_third_vote:alpaca>tiingo" in out.at[1, "flags"] and "disagree_unresolved" not in out.at[1, "flags"]
    assert out.at[1, "n_sources"] == 3
    # the settled day is not voted on twice
    out2, f = v21.merge_existing(out, a)
    assert f["votes"] == 1
