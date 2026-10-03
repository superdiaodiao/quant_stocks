"""Tests for plan step 9, the canonical series and the split / distribution / review tables
(scripts/reversal_data_reconcile.py). Synthetic frames only: no cache, no network."""
from datetime import datetime, timezone
import re

import numpy as np
import pandas as pd
import pytest

from scripts import reversal_data_reconcile as rc

SESSIONS = rc.pf.xnas_sessions("2015-01-02", "2015-03-31")


def _frame(closes, start=0, split=None, div=None, volume=1000.0, rowflag=None, sessions=SESSIONS):
    n = len(closes)
    dates = sessions[start: start + n]
    return pd.DataFrame({"security_id": "X", "date": dates, "close": np.asarray(closes, dtype=float),
                         "volume": np.full(n, volume, dtype=float) if np.isscalar(volume) else np.asarray(volume, float),
                         "split": np.ones(n) if split is None else np.asarray(split, float),
                         "div": np.zeros(n) if div is None else np.asarray(div, float),
                         "rowflag": [""] * n if rowflag is None else rowflag, "file": "f"})


def _ctx(listed=None):
    return {"sessions": SESSIONS, "window": (SESSIONS[0], SESSIONS[-1]),
            "listed": listed or (lambda grid: np.ones(len(grid), dtype=bool)), "ticker_of": lambda day: "TEST"}


def _walk(n, start=100.0, step=0.003, seed=1):
    rng = np.random.default_rng(seed)
    return start * np.cumprod(1 + rng.normal(0, step, n))


def _flags(canonical, day):
    return canonical.loc[canonical["date"] == day, "flags"].iloc[0].split(";")


@pytest.fixture(autouse=True)
def _no_hand_review(tmp_path, monkeypatch):
    """The tests never read the cache's merged hand-review verdicts (a test that needs them writes its own)."""
    monkeypatch.setattr(rc, "REVIEW_DIR", tmp_path / "no_review_verdicts")
    monkeypatch.setattr(rc, "_REVIEW_CACHE", {})


# ------------------------------------------------------------------ formula and ratios

def test_total_return_is_the_crsp_definition():
    r = rc.total_return(np.array([50.0]), np.array([2.0]), np.array([1.0]), np.array([100.0]))
    assert r[0] == pytest.approx((50 * 2 + 1) / 100 - 1)


@pytest.mark.parametrize("value,ordinary", [(2.0, True), (0.04, True), (1.5, True), (25.0, True), (0.1, True),
                                            (1.275, False), (1.061, False), (0.9535, False), (4.423, False),
                                            (1.603, False), (2.376, False)])
def test_ordinary_ratio_keeps_distributions_out(value, ordinary):
    assert rc.ordinary_ratio(value) is ordinary


def test_near_split_factor_needs_a_split_sized_ordinary_jump():
    assert rc.near_split_factor(10.02) == 10.0
    assert rc.near_split_factor(0.0501) == pytest.approx(0.05)
    assert rc.near_split_factor(1.2) is None  # too small to be a split
    assert rc.near_split_factor(7.7) is None  # not near an ordinary ratio


def test_splice_points_ignore_short_runs():
    primary = np.array([0] * 30 + [1] * 2 + [0] * 30 + [1] * 25)
    idx = np.arange(len(primary))
    assert rc.splice_points(primary, idx) == [(62, 0, 1)]


def test_adj_identity_flags_a_row_whose_adjclose_ratio_breaks_the_formula():
    rows = pd.DataFrame({"close": [10.0, 11.0, 5.5, 5.6], "split": [1, 1, 2, 1], "div": [0, 0, 0, 0.0],
                         "adj": [5.0, 5.5, 5.5, 5.7]})
    assert rc.adj_identity_flags(rows).tolist() == [False, False, False, True]


# ------------------------------------------------------------------ source selection

def test_majority_outvotes_the_default_source():
    r = np.array([[0.05], [0.0], [0.0001], [np.nan]])
    valid = np.isfinite(r)
    rank = np.array([[0.0], [1.0], [2.0], [np.inf]])
    choice = rc.select_sources(r, valid, valid, rank)
    assert choice["primary"][0] == rc.T_  # wiki is outvoted by tiingo and yahoo
    assert choice["override"][0] and not choice["unresolved"][0]
    assert choice["minority"][rc.W, 0]


def test_two_sources_that_disagree_are_unresolved():
    r = np.array([[0.05], [0.0], [np.nan], [np.nan]])
    valid = np.isfinite(r)
    rank = np.array([[0.0], [1.0], [2.0], [np.inf]])
    choice = rc.select_sources(r, valid, valid, rank)
    assert choice["unresolved"][0] and choice["primary"][0] == rc.W


# ------------------------------------------------------------------ the canonical series

def test_split_day_return_uses_the_split_factor_and_events_agree():
    n = 40
    close = _walk(n)
    close[20:] = close[20:] / 2.0
    split = np.ones(n)
    split[20] = 2.0
    frames = {"wiki": _frame(close, split=split), "yahoo": _frame(close, split=split),
              "stored": _frame(np.r_[close[:20] / 2.0, close[20:]])}  # adjusted for the split
    result = rc.reconcile_security("X", frames, _ctx())
    canonical = result["canonical"]
    day = str(SESSIONS[20].date())
    row = canonical[canonical["date"] == day].iloc[0]
    assert row["split_factor"] == 2.0 and abs(row["tr"]) < 0.02
    assert "split" in _flags(canonical, day)
    assert row["n_sources"] == 3  # the adjusted stored file is a valid vote on the split day (before 2023)
    event = [e for e in result["events"] if e["event_type"] == "split"][0]
    assert event["agree"] == "Y" and "stored" in event["sources_confirming"]
    # the stored file moves with the total return: adjusted, so it confirms the ratio
    assert event["stored_implied_k"] == pytest.approx(1.0, abs=0.01) and event["stored_state"] == "adjusted"
    assert event["nasdaq"] == ""  # no Nasdaq calendar source yet


def test_odd_ratio_is_a_distribution_not_a_split():
    n = 30
    close = _walk(n)
    close[10:] = close[10:] / 1.275
    split = np.ones(n)
    split[10] = 1.275
    flags = [""] * n
    flags[10] = "yahoo_odd_ratio"
    result = rc.reconcile_security("X", {"yahoo": _frame(close, split=split, rowflag=flags)}, _ctx())
    day = str(SESSIONS[10].date())
    assert "distribution_factor" in _flags(result["canonical"], day)
    assert result["events"][0]["event_type"] == "distribution"
    assert result["specials"][0]["classification"] == "distribution_as_ratio"
    assert result["specials"][0]["pct_of_prior"] == pytest.approx(1 - 1 / 1.275, rel=1e-4)


def test_yahoo_junction_row_is_not_a_split_and_gives_no_return():
    n = 30
    close = _walk(n)
    close[15:] = close[15:] * 3.5
    split = np.ones(n)
    split[15] = 3.502  # S from another history (891103's MTCH junction)
    flags = [""] * n
    flags[15] = "yahoo_junction"
    result = rc.reconcile_security("X", {"yahoo": _frame(close, split=split, rowflag=flags)}, _ctx())
    row = result["canonical"].iloc[15]
    assert row["split_factor"] == 1.0 and np.isnan(row["tr"])
    assert "yahoo_junction" in row["flags"]
    assert result["events"] == []


def test_stored_unit_break_is_recorded_and_excluded_from_the_vote():
    n = 30
    close = _walk(n)
    stored = close.copy()
    stored[12:] = stored[12:] / 10.0  # the stored file changes units; vendors show nothing
    frames = {"wiki": _frame(close), "yahoo": _frame(close), "stored": _frame(stored)}
    result = rc.reconcile_security("X", frames, _ctx())
    day = str(SESSIONS[12].date())
    assert "stored_excluded" in _flags(result["canonical"], day)
    breaks = [e for e in result["events"] if e["event_type"] == "unit_break"]
    assert len(breaks) == 1 and breaks[0]["ex_date"] == day
    assert breaks[0]["stored_implied_k"] == pytest.approx(10.0, rel=0.01) and breaks[0]["stored_state"] == "unit_change"
    assert breaks[0]["agree"] == "Y"  # both vendors show no split


def test_special_cash_and_the_known_spinoff_label():
    n = 30
    close = _walk(n)
    close[8:] = close[8:] * 0.7
    div = np.zeros(n)
    div[8] = close[7] * 0.3
    result = rc.reconcile_security("X", {"wiki": _frame(close, div=div)}, _ctx())
    day = str(SESSIONS[8].date())
    assert "special_div" in _flags(result["canonical"], day)
    special = result["specials"][0]
    assert special["classification"] == "special_cash_or_spinoff" and special["pct_of_prior"] == pytest.approx(0.3, rel=1e-6)


def test_disagreement_majority_and_unresolved_queue():
    n = 30
    close = _walk(n)
    bad = close.copy()
    bad[10] = bad[10] * 1.03  # one source wrong on one day
    three = rc.reconcile_security("X", {"wiki": _frame(bad), "tiingo": _frame(close), "yahoo": _frame(close)}, _ctx())
    day = str(SESSIONS[10].date())
    flags = _flags(three["canonical"], day)
    assert any(f.startswith("majority_override") for f in flags)
    assert three["canonical"].iloc[10]["src_primary"] == "tiingo"
    assert not [m for m in three["moves"] if m["rule"] == "R3"]
    two = rc.reconcile_security("X", {"wiki": _frame(bad), "yahoo": _frame(close)}, _ctx())
    assert "disagree_unresolved" in _flags(two["canonical"], day)
    assert [m for m in two["moves"] if m["rule"] == "R3" and m["event_date"] == day]


def test_filler_after_the_last_trade_is_cut():
    n = 40
    close = _walk(n)
    volume = np.full(n, 1000.0)
    close[30:] = close[29]
    volume[30:] = 0.0
    result = rc.reconcile_security("X", {"tiingo": _frame(close, volume=volume)}, _ctx())
    assert result["summary"]["filler_cut"] == 10
    assert result["canonical"]["date"].iloc[-1] == str(SESSIONS[29].date())


def test_interior_gap_flag_and_queue():
    n = 40
    close = _walk(n)
    frame = _frame(close).drop(index=[15, 16]).reset_index(drop=True)
    result = rc.reconcile_security("X", {"wiki": frame}, _ctx())
    after = str(SESSIONS[17].date())
    assert "gap_before:2" in _flags(result["canonical"], after)
    gaps = [m for m in result["moves"] if m["rule"] == "R6"]
    assert len(gaps) == 1 and gaps[0]["event_date"] == str(SESSIONS[15].date())


def test_flat_run_without_a_second_vendor_is_queued_and_a_confirmed_one_is_not():
    n = 30
    close = _walk(n)
    close[10:14] = close[10]
    single = rc.reconcile_security("X", {"wiki": _frame(close)}, _ctx())
    assert [m for m in single["moves"] if m["rule"] == "R4"]
    both = rc.reconcile_security("X", {"wiki": _frame(close), "yahoo": _frame(close)}, _ctx())
    assert not [m for m in both["moves"] if m["rule"] == "R4"]
    assert "flat_run" in _flags(both["canonical"], str(SESSIONS[11].date()))


def test_move_2x_and_hidden_split_are_queued_with_agreeing_sources():
    n = 30
    close = _walk(n)
    close[20:] = close[20:] / 3.0  # a 3:1 split nobody recorded
    frames = {"wiki": _frame(close), "yahoo": _frame(close)}
    result = rc.reconcile_security("X", frames, _ctx())
    day = str(SESSIONS[20].date())
    flags = _flags(result["canonical"], day)
    assert "move_2x" in flags and "hidden_split" in flags
    entry = [m for m in result["moves"] if m["event_date"] == day][0]
    assert entry["rule"] == "R1/R2" and entry["sources_agreeing"] == "wiki+yahoo"
    assert entry["classification"] == "unreviewed"


def test_level_difference_between_vendors_is_flagged():
    n = 30
    close = _walk(n)
    result = rc.reconcile_security("X", {"wiki": _frame(close), "yahoo": _frame(close * 0.95)}, _ctx())
    assert any(f.startswith("level_diff") for f in _flags(result["canonical"], str(SESSIONS[5].date())))
    assert [m for m in result["moves"] if m["rule"] == "R7"]


def test_rows_outside_the_listing_are_flagged():
    n = 20
    listed = lambda grid: np.arange(len(grid)) >= 5
    result = rc.reconcile_security("X", {"yahoo": _frame(_walk(n))}, _ctx(listed))
    assert "outside_listing" in _flags(result["canonical"], str(SESSIONS[2].date()))
    assert "outside_listing" not in _flags(result["canonical"], str(SESSIONS[8].date()))


def test_precedence_switches_to_tiingo_in_november_2017():
    sessions = rc.pf.xnas_sessions("2017-10-02", "2017-11-30")
    close = _walk(len(sessions))
    frames = {"wiki": _frame(close, sessions=sessions), "tiingo": _frame(close, sessions=sessions)}
    ctx = {"sessions": sessions, "window": (sessions[0], sessions[-1]),
           "listed": lambda grid: np.ones(len(grid), dtype=bool), "ticker_of": lambda day: "T"}
    canonical = rc.reconcile_security("X", frames, ctx)["canonical"]
    assert set(canonical.loc[canonical["date"] < "2017-11-01", "src_primary"]) == {"wiki"}
    assert set(canonical.loc[canonical["date"] >= "2017-11-01", "src_primary"]) == {"tiingo"}


def test_stored_vote_is_dropped_on_ex_dates_from_2023():
    sessions = rc.pf.xnas_sessions("2024-01-02", "2024-02-28")
    n = len(sessions)
    close = _walk(n)
    div = np.zeros(n)
    div[10] = 0.5
    stored = close.copy()  # price only: no dividend in its return
    frames = {"yahoo": _frame(close, div=div, sessions=sessions), "stored": _frame(stored, sessions=sessions)}
    ctx = {"sessions": sessions, "window": (sessions[0], sessions[-1]),
           "listed": lambda grid: np.ones(len(grid), dtype=bool), "ticker_of": lambda day: "T"}
    canonical = rc.reconcile_security("X", frames, ctx)["canonical"]
    assert canonical.iloc[10]["n_sources"] == 1 and "stored_excluded" in canonical.iloc[10]["flags"]
    assert canonical.iloc[11]["n_sources"] == 2


# ------------------------------------------------------------------ Yahoo restore (holdout charts)

def _stamp(day):
    return int(datetime.strptime(day, "%Y-%m-%d").replace(hour=14, minute=30, tzinfo=timezone.utc).timestamp())


def test_yahoo_restore_multiplies_closes_and_dividends_by_later_splits():
    days = ["2015-01-05", "2015-01-06", "2015-01-07"]
    result = {"meta": {"symbol": "T", "dataGranularity": "1d"}, "timestamp": [_stamp(d) for d in days],
              "events": {"splits": {"a": {"date": _stamp("2015-01-07"), "numerator": 2, "denominator": 1}},
                         "dividends": {"b": {"date": _stamp("2015-01-06"), "amount": 0.25}}},
              "indicators": {"quote": [{"close": [50.0, 51.0, 52.0], "volume": [200, 200, 100]}]}}
    rows = rc.yahoo_restore(result)
    assert rows["close"].tolist() == [100.0, 102.0, 52.0]
    assert rows["volume"].tolist() == [100.0, 100.0, 100.0]
    assert rows["div"].tolist() == [0.0, 0.5, 0.0]
    assert rows["split"].tolist() == [1.0, 1.0, 2.0]
    assert [str(d.date()) for d in rows["date"]] == days


def test_a_stored_row_that_reverts_is_a_glitch_queued_as_single_vendor_vs_stored():
    n = 30
    close = _walk(n)
    stored = close.copy()
    stored[10] *= 1.06  # one bad row in one of the two (LANC 2020 mixes adjusted and raw closes)
    result = rc.reconcile_security("X", {"yahoo": _frame(close), "stored": _frame(stored)}, _ctx())
    for k in (10, 11):
        flags = _flags(result["canonical"], str(SESSIONS[k].date()))
        assert "stored_glitch" in flags and "disagree_unresolved" not in flags
    assert result["canonical"].iloc[10]["src_primary"] == "yahoo"  # the vendor stands
    # which row is bad is not known: one R3 entry for the two-day run (plan 4.4 R3)
    r3 = [m for m in result["moves"] if m["rule"] == "R3"]
    assert len(r3) == 1 and r3[0]["event_date"] == str(SESSIONS[10].date())
    assert "single vendor vs stored on 2 session(s)" in r3[0]["notes"]


def test_a_small_stored_glitch_is_flagged_only():
    n = 30
    close = _walk(n)
    stored = close.copy()
    stored[10] *= 1.015  # below the 2% queue line
    result = rc.reconcile_security("X", {"yahoo": _frame(close), "stored": _frame(stored)}, _ctx())
    assert "stored_glitch" in _flags(result["canonical"], str(SESSIONS[10].date()))
    assert not [m for m in result["moves"] if m["rule"] == "R3"]


def test_a_lasting_stored_shift_is_queued():
    n = 30
    close = _walk(n)
    stored = close.copy()
    stored[:12] *= 0.95  # the stored file adjusts for something on day 12 that the vendor lacks
    result = rc.reconcile_security("X", {"yahoo": _frame(close), "stored": _frame(stored)}, _ctx())
    day = str(SESSIONS[12].date())
    assert "stored_shift" in _flags(result["canonical"], day)
    assert [m for m in result["moves"] if m["rule"] == "R3" and m["event_date"] == day]


@pytest.mark.parametrize("claimed", [4.0, 3.0, 2.5, 1.7])
def test_a_raw_stored_jump_does_not_confirm_a_single_vendor_ratio(claimed):
    # The raw price falls 4x and Yahoo claims S. A stored file that shows the same raw jump implies
    # k = (1 + tr) / (1 + r_stored) = S whatever S is: it confirms the date and raw prices only.
    n = 30
    close = _walk(n)
    close[15:] /= 4.0
    split = np.ones(n)
    split[15] = claimed
    result = rc.reconcile_security("X", {"yahoo": _frame(close, split=split), "stored": _frame(close)}, _ctx())
    event = result["events"][0]
    assert event["stored_implied_k"] == pytest.approx(claimed, rel=0.01) and event["stored_state"] == "raw"
    assert event["agree"] == "N" and event["sources_confirming"] == "yahoo"
    assert "stored raw jump: date and raw prices only" in event["notes"]
    tr = result["canonical"].iloc[15]["tr"]
    day = str(SESSIONS[15].date())
    noted = [m for m in result["moves"] if m["event_date"] == day and "rests on a" in m["notes"]]
    assert bool(noted) == (abs(tr) >= rc.RATIO_QUEUE_TR)  # S = 3.0, 2.5, 1.7 give |tr| >= 10%; 4.0 does not
    if noted:  # one entry per day: R1c, or the R1 entry of a 2x move (S = 1.7: tr -57.5%) with the same note
        assert len(noted) == 1 and noted[0]["rule"] == ("R1" if abs(tr) >= 0.5 else "R1c")


def test_single_source_distribution_ratio_with_a_big_tr_is_queued():
    # LGND 2022-11-02: Yahoo and stored both go 84.61 -> 68.00; Yahoo's 1.603 alone gives tr +28.8%
    n = 30
    close = _walk(n, start=84.61)
    close[15:] *= 68.00 / 84.61 * close[14] / close[15]
    split = np.ones(n)
    split[15] = 1.603
    flags = [""] * n
    flags[15] = "yahoo_odd_ratio"
    result = rc.reconcile_security("X", {"yahoo": _frame(close, split=split, rowflag=flags),
                                         "stored": _frame(close)}, _ctx())
    event = result["events"][0]
    assert event["event_type"] == "distribution" and event["agree"] == "N" and event["sources_confirming"] == "yahoo"
    day = str(SESSIONS[15].date())
    assert result["canonical"].iloc[15]["tr"] == pytest.approx(close[15] * 1.603 / close[14] - 1)
    entry = [m for m in result["moves"] if m["rule"] == "R1c"]
    assert len(entry) == 1 and entry[0]["event_date"] == day and "1.603" in entry[0]["notes"]


def test_vendor_error_is_outvoted_with_the_stored_vote_not_booked_as_a_unit_break():
    # PTCT 2013-06-20..25: WIKI's rows are 10x too low; Yahoo and the stored file are right
    n = 30
    close = _walk(n)
    wiki = close.copy()
    wiki[10:14] /= 10.0
    frames = {"wiki": _frame(wiki), "yahoo": _frame(close), "stored": _frame(close)}
    result = rc.reconcile_security("X", frames, _ctx())
    canonical = result["canonical"]
    for k in (10, 14):
        row = canonical.iloc[k]
        flags = row["flags"].split(";")
        assert row["src_primary"] == "yahoo" and row["tr"] == pytest.approx(close[k] / close[k - 1] - 1)
        assert "majority_override:wiki" in flags and "disagree_resolved:wiki" in flags
        assert "disagree_unresolved" not in flags and "stored_excluded" not in flags
        assert row["n_sources"] == 3
    assert not [e for e in result["events"] if e["event_type"] == "unit_break"]
    assert not [m for m in result["moves"] if m["rule"] in ("R1", "R3")]
    assert [m for m in result["moves"] if m["rule"] == "R7"]  # WIKI's level run is still queued


def test_single_vendor_split_jump_is_a_hidden_split_or_vendor_error_not_a_unit_break():
    # CMCT 2025-01-06: Yahoo lacks a 1:10 (0.17 -> 1.68) while the stored file moves normally
    n = 30
    close = _walk(n)
    yahoo = close.copy()
    yahoo[:15] /= 10.0
    result = rc.reconcile_security("X", {"yahoo": _frame(yahoo), "stored": _frame(close)}, _ctx())
    day = str(SESSIONS[15].date())
    flags = _flags(result["canonical"], day)
    assert {"vendor_split_jump", "hidden_split", "move_2x"} <= set(flags) and "stored_excluded" not in flags
    assert not [e for e in result["events"] if e["event_type"] == "unit_break"]
    entries = [m for m in result["moves"] if m["event_date"] == day]
    assert [m["rule"] for m in entries] == ["R1/R2"]
    assert "fits 0.1 split" in entries[0]["notes"] and "vendor error" in entries[0]["notes"]


def test_single_vendor_split_sized_move_below_2x_is_queued_r2_r3():
    n = 30
    close = _walk(n)
    yahoo = close.copy()
    yahoo[:15] /= 1.5  # the only vendor jumps 1.5x; the stored file does not
    result = rc.reconcile_security("X", {"yahoo": _frame(yahoo), "stored": _frame(close)}, _ctx())
    day = str(SESSIONS[15].date())
    flags = _flags(result["canonical"], day)
    assert "vendor_split_jump" in flags and "hidden_split" not in flags  # hidden_split needs a 2x move
    assert not [e for e in result["events"] if e["event_type"] == "unit_break"]
    assert [m["rule"] for m in result["moves"] if m["event_date"] == day] == ["R2/R3"]


def test_a_big_move_the_stored_file_also_shows_is_not_a_vendor_jump():
    # OPTT 2016-06-02: Yahoo -31.0%, stored -32.2%: a market move both show, 1.2% apart
    n = 30
    close = _walk(n)
    yahoo, stored = close.copy(), close.copy()
    yahoo[15:] *= 0.69
    stored[15:] *= 0.678
    result = rc.reconcile_security("X", {"yahoo": _frame(yahoo), "stored": _frame(stored)}, _ctx())
    day = str(SESSIONS[15].date())
    assert "vendor_split_jump" not in _flags(result["canonical"], day)
    assert "R2/R3" not in [m["rule"] for m in result["moves"]]


def test_single_vendor_day_stored_unit_change_is_still_a_unit_break():
    n = 30
    close = _walk(n)
    stored = close.copy()
    stored[12:] /= 10.0  # the stored file changes units; the only vendor moves normally
    result = rc.reconcile_security("X", {"yahoo": _frame(close), "stored": _frame(stored)}, _ctx())
    day = str(SESSIONS[12].date())
    assert "stored_excluded" in _flags(result["canonical"], day)
    breaks = [e for e in result["events"] if e["event_type"] == "unit_break"]
    assert len(breaks) == 1 and breaks[0]["ex_date"] == day and breaks[0]["agree"] == "N"


def test_penny_stock_stored_unit_change_allows_for_cent_rounding():
    # CBIO 2025-06-02: stored 0.2099 -> 20.20 and stays; Yahoo's cent closes 0.21 -> 0.20 put k 1.05%
    # from 1/100, inside 1% plus the rounding allowance
    n = 30
    true = _walk(n, start=0.2099, step=0.01, seed=3)
    yahoo = np.round(true, 2)
    stored = true.copy()
    stored[12:] *= 100.0
    stored[12] = stored[11] * 100.0 * (0.20 / 0.21) * 1.0105  # k = (0.20 / 0.21) / (stored jump) about 1/101
    yahoo[11], yahoo[12] = 0.21, 0.20
    stored[13:] = stored[12] * true[13:] / true[12]
    result = rc.reconcile_security("X", {"yahoo": _frame(yahoo), "stored": _frame(stored)}, _ctx())
    day = str(SESSIONS[12].date())
    assert "stored_excluded" in _flags(result["canonical"], day)
    breaks = [e for e in result["events"] if e["event_type"] == "unit_break"]
    assert [b["ex_date"] for b in breaks] == [day]


def test_an_ordinary_ratio_stored_jump_that_comes_back_is_not_a_unit_change():
    n = 30
    close = _walk(n)
    stored = close.copy()
    stored[12] *= 1.5  # one bad stored row at exactly 3:2, back the next day
    result = rc.reconcile_security("X", {"yahoo": _frame(close), "stored": _frame(stored)}, _ctx())
    flags = _flags(result["canonical"], str(SESSIONS[12].date()))
    assert "stored_excluded" not in flags and "stored_glitch" in flags
    assert not [e for e in result["events"] if e["event_type"] == "unit_break"]
    assert [m for m in result["moves"] if m["rule"] == "R3"]


def test_a_stored_file_off_by_a_near_but_not_ordinary_ratio_stays_a_vote():
    # CGC 2023-06-30: k = 0.657 is 1.4% from 2/3, so not a stored unit change at the 1% line
    n = 30
    close = _walk(n)
    stored = close.copy()
    stored[12] = stored[11] * (close[12] / close[11]) / 0.657  # k = (1 + r_vendor) / (1 + r_stored) = 0.657
    stored[13:] = stored[12] * close[13:] / close[12]
    result = rc.reconcile_security("X", {"yahoo": _frame(close), "stored": _frame(stored)}, _ctx())
    flags = _flags(result["canonical"], str(SESSIONS[12].date()))
    assert "stored_excluded" not in flags
    assert not [e for e in result["events"] if e["event_type"] == "unit_break"]
    assert [m for m in result["moves"] if m["rule"] == "R3" and m["event_date"] == str(SESSIONS[12].date())]


def test_a_split_wiki_serves_as_cash_is_a_split_not_a_special_distribution():
    # PZZA 2013-12-30: WIKI close 46 with ex-dividend 46 (2:1), Yahoo S = 2, stored adjusted
    n = 30
    close = _walk(n, start=90.0)
    close[15:] /= 2.0
    split = np.ones(n)
    split[15] = 2.0
    cash = np.zeros(n)
    cash[15] = close[15]  # (S - 1) x close: the new shares at the day's close
    stored = np.r_[close[:15] / 2.0, close[15:]]
    frames = {"wiki": _frame(close, div=cash), "yahoo": _frame(close, split=split), "stored": _frame(stored)}
    result = rc.reconcile_security("X", frames, _ctx())
    row = result["canonical"].iloc[15]
    flags = row["flags"].split(";")
    assert row["src_primary"] == "wiki" and row["split_factor"] == 2.0 and row["div_cash"] == 0.0
    assert "split" in flags and "cash_as_split:wiki" in flags and "special_div" not in flags
    assert row["tr"] == pytest.approx(close[15] * 2 / close[14] - 1)
    event = result["events"][0]
    assert event["event_type"] == "split" and event["agree"] == "Y"
    assert set(event["sources_confirming"].split("+")) == {"wiki(cash)", "yahoo", "stored"}
    assert result["specials"] == []


def test_a_real_distribution_served_as_cash_and_as_an_odd_ratio_stays_a_distribution():
    # EBAY 2015-07-20 style: Yahoo an odd ratio, WIKI the cash; no ordinary ratio anywhere
    n = 30
    close = _walk(n)
    ratio = 2.376
    close[15:] /= ratio
    split = np.ones(n)
    split[15] = ratio
    cash = np.zeros(n)
    cash[15] = close[14] * (1 - 1 / ratio)
    flags = [""] * n
    flags[15] = "yahoo_odd_ratio"
    frames = {"wiki": _frame(close, div=cash), "yahoo": _frame(close, split=split, rowflag=flags)}
    result = rc.reconcile_security("X", frames, _ctx())
    assert "cash_as_split" not in result["canonical"].iloc[15]["flags"]
    assert result["events"][0]["event_type"] == "distribution"
    assert result["specials"][0]["classification"] == "distribution_ratio_and_cash"


def test_ratio_below_one_has_no_pct_of_prior():
    # HON 2026-06-29 style: an odd ratio below 1 (0.9535) is not a share of value taken away
    n = 30
    close = _walk(n)
    close[15:] /= 0.9535
    split = np.ones(n)
    split[15] = 0.9535
    flags = [""] * n
    flags[15] = "yahoo_odd_ratio"
    result = rc.reconcile_security("X", {"yahoo": _frame(close, split=split, rowflag=flags)}, _ctx())
    special = result["specials"][0]
    assert special["pct_of_prior"] == "" and "ratio below 1: no pct" in special["notes"]


def test_tiny_volume_filler_after_the_last_trade_is_cut():
    # SPLK 2024-03-15: Tiingo repeats close 156.90 after the last trade with volumes 0, 90, 47, 5546
    n = 40
    close = _walk(n)
    volume = np.full(n, 1_000_000.0)
    close[31:] = close[30]
    volume[31:] = [0, 90, 47, 5546, 0, 120, 0, 90, 47]
    result = rc.reconcile_security("X", {"tiingo": _frame(close, volume=volume)}, _ctx())
    assert result["canonical"]["date"].iloc[-1] == str(SESSIONS[30].date())
    assert result["summary"]["filler_cut"] == 9 and result["summary"]["filler_cut_tiny_volume"] == 9


def test_repeated_closes_with_real_volume_at_the_end_are_kept():
    n = 40
    close = _walk(n)
    close[35:] = close[34]  # pinned at a deal price, still trading
    volume = np.full(n, 1_000_000.0)
    volume[35:] = 400_000.0
    result = rc.reconcile_security("X", {"tiingo": _frame(close, volume=volume)}, _ctx())
    assert len(result["canonical"]) == n and result["summary"]["filler_cut"] == 0


def test_vendor_only_agreement_counts_leave_the_stored_vote_out():
    n = 30
    close = _walk(n)
    frames = {"wiki": _frame(close), "yahoo": _frame(close), "stored": _frame(close)}
    summary = rc.reconcile_security("X", frames, _ctx())["summary"]
    assert summary["rows_multi_vendor"] == n - 1 and summary["rows_multi_vendor_agree"] == n - 1
    single = rc.reconcile_security("X", {"yahoo": _frame(close), "stored": _frame(close)}, _ctx())["summary"]
    assert single["rows_multi_source"] == n - 1 and single["rows_multi_vendor"] == 0


def test_vendor_agreement_reports_the_v_sample_separately(tmp_path, monkeypatch):
    candidates = tmp_path / "candidates.csv"
    pd.DataFrame({"security_id": ["V1", "A1"], "reason": [rc.V_SAMPLE_REASON, "Y_active_rank300"]}).to_csv(
        candidates, index=False)
    monkeypatch.setattr(rc, "CANDIDATES", candidates)
    pairs = pd.DataFrame([
        {"security_id": "V1", "a": "tiingo", "b": "yahoo", "days_r": 100, "days_r_1e4": 99, "days_r_0p5pct": 100},
        {"security_id": "A1", "a": "tiingo", "b": "yahoo", "days_r": 50, "days_r_1e4": 40, "days_r_0p5pct": 49},
        {"security_id": "A1", "a": "yahoo", "b": "stored", "days_r": 500, "days_r_1e4": 10, "days_r_0p5pct": 495}])
    states = {"V1": {"summary": {"rows_multi_vendor": 100, "rows_multi_vendor_agree": 100}},
              "A1": {"summary": {"rows_multi_vendor": 50, "rows_multi_vendor_agree": 49}}}
    out = rc.vendor_agreement(states, pairs, pd.DataFrame())
    assert out["vendor_only"] == {"name_days_2plus_vendors": 150, "agree_0p5pct": 149, "share": round(149 / 150, 5)}
    assert out["v_sample_tiingo_yahoo"]["days"] == 100 and out["v_sample_tiingo_yahoo"]["share_1e4"] == 0.99
    assert out["all_tiingo_yahoo"]["days"] == 150 and out["pair_days"]["yahoo-stored"]["days"] == 500


def test_yahoo_volume_tokens_flag_the_rows_before_the_ex_date(tmp_path, monkeypatch):
    days = [str(d.date()) for d in SESSIONS[:6]]
    yahoo_dir = tmp_path / "yahoo"
    yahoo_dir.mkdir()
    pd.DataFrame({"date": days, "close_raw": [10.0, 10.1, 10.2, 5.1, 5.2, 5.3], "volume_raw": 1000.0,
                  "split_factor": [1, 1, 1, 2, 1, 1], "div_cash": 0.0, "junction": ""}).to_csv(
        yahoo_dir / "X.csv.gz", index=False, compression="gzip")
    events = yahoo_dir / "events.csv"
    pd.DataFrame({"security_id": ["X"], "ex_date": [days[3]], "event_type": ["split"],
                  "flags": ["volume_restore_unverified"]}).to_csv(events, index=False)
    monkeypatch.setattr(rc, "YAHOO_DIR", yahoo_dir)
    monkeypatch.setattr(rc, "YAHOO_EVENTS", events)
    rows = rc.load_new_yahoo_rows({"X"})
    assert ["yahoo_volume_unverified" in f for f in rows["rowflag"]] == [True, True, True, False, False, False]
    canonical = rc.reconcile_security("X", {"yahoo": rows}, _ctx())["canonical"]
    assert ["yahoo_volume_unverified" in f for f in canonical["flags"]] == [True, True, True, False, False, False]


def test_hidden_split_is_not_claimed_when_the_adjusted_stored_file_moves_too():
    n = 30
    close = _walk(n)
    close[20:] /= 2.2
    result = rc.reconcile_security("X", {"yahoo": _frame(close), "stored": _frame(close)}, _ctx())
    flags = _flags(result["canonical"], str(SESSIONS[20].date()))
    assert "move_2x" in flags and "hidden_split" not in flags


# ------------------------------------------------------------------ listings after a Form 25 cut, relist junctions

def _smci_identity():
    """SMCI (1375365): Form 25 on 2019-03-22 (removed for late filings), on Nasdaq again from 2020-01."""
    intervals = pd.DataFrame([
        {"security_id": "1375365", "ticker": "SMCI", "start": "2010-12-31", "end": "2018-08-22",
         "end_next_absent": "2018-09-07", "start_prev_absent": "", "name_in_source": "Super Micro Computer",
         "share_class": "COMMON"},
        {"security_id": "1375365", "ticker": "SMCI", "start": "2019-12-28", "end": "2026-08-01",
         "end_next_absent": "", "start_prev_absent": "2019-12-27", "name_in_source": "Super Micro Computer",
         "share_class": "COMMON"}])
    master = pd.DataFrame([{"security_id": "1375365", "delist_date": "2019-03-22"}])
    return rc.identity_from(intervals, master)


def test_smci_listing_after_its_form25_cut_survives_into_the_canonical_series():
    identity = _smci_identity()
    mapping = identity["mapping"]
    later = mapping[mapping["list_start"] >= "2019-12-28"].iloc[0]
    assert bool(later["after_cut"]) and later["list_end"] == rc.WINDOW_END  # not cut to the 2019 Form 25
    assert identity["relisted"]["1375365"] == [("2019-03-22", "2019-12-28", rc.WINDOW_END)]
    low, high = rc.security_windows(mapping, {"1375365"})["1375365"]
    assert str(low.date()) == rc.WINDOW_START and str(high.date()) == rc.WINDOW_END
    sessions = rc.pf.xnas_sessions("2018-06-01", "2024-12-31")
    frame = _frame(_walk(len(sessions), step=0.01), sessions=sessions)  # one vendor file through the OTC months
    ctx = {"sessions": sessions, "window": (sessions[0], sessions[-1]),
           "listed": lambda grid: rc.listed_mask(mapping, "1375365", grid), "ticker_of": lambda day: "SMCI",
           "relists": identity["relisted"]["1375365"]}
    canonical = rc.reconcile_security("1375365", {"tiingo": frame}, ctx)["canonical"]
    assert canonical["date"].iloc[-1] == str(sessions[-1].date())  # reaches 2024 (and 2026 in the real window)
    outside = canonical["flags"].str.contains("outside_listing")
    assert outside[(canonical["date"] > "2018-09-06") & (canonical["date"] < "2019-12-28")].all()  # the OTC months
    assert not outside[canonical["date"] >= "2019-12-28"].any()
    assert canonical["tr"].iloc[1:].notna().all()  # the same shares: one series, no junction


def _relist_frames():
    """Old shares to day 19 (0.12 at the end), new shares from day 20 (31.00): Yahoo carries both,
    Tiingo only the new shares; the stored file is in its own units."""
    n = 40
    old = 0.12 * np.cumprod(1 + np.random.default_rng(3).normal(0, 0.01, 20))
    new = _walk(20, start=31.0, seed=4)
    close = np.r_[old, new]
    frames = {"yahoo": _frame(close), "tiingo": _frame(new, start=20), "stored": _frame(close * 3.0)}
    return n, close, frames


def test_relist_junction_gives_no_return_across_the_old_and_new_shares():
    n, close, frames = _relist_frames()
    day = str(SESSIONS[20].date())
    ctx = {**_ctx(), "junctions": [day]}
    result = rc.reconcile_security("X", frames, ctx)
    canonical = result["canonical"]
    row = canonical[canonical["date"] == day].iloc[0]
    assert np.isnan(row["tr"]) and "relist_junction" in row["flags"].split(";")
    assert not {"move_2x", "move_40", "hidden_split"} & set(row["flags"].split(";"))
    assert not [m for m in result["moves"] if m["event_date"] == day]  # nothing queued on a return across it
    assert canonical["tr"].drop(index=[0, 20]).notna().all()  # both sides keep their own returns
    segments = result["summary"]["segments"]
    assert [(s["first"], s["last"]) for s in segments] == [(str(SESSIONS[0].date()), str(SESSIONS[19].date())),
                                                          (day, str(SESSIONS[n - 1].date()))]
    # without the junction the same data is a 258x move queued as R1
    plain = rc.reconcile_security("X", frames, _ctx())
    assert "move_2x" in _flags(plain["canonical"], day)
    assert [m for m in plain["moves"] if m["event_date"] == day and m["rule"].startswith("R1")]


def test_relist_junction_drops_an_exchange_a_vendor_serves_as_a_split():
    n, close, frames = _relist_frames()
    split = np.ones(n)
    split[20] = 1 / 89.0  # a vendor chaining the old shares into the new ones
    frames["yahoo"] = _frame(close, split=split)
    day = str(SESSIONS[20].date())
    result = rc.reconcile_security("X", frames, {**_ctx(), "junctions": [day]})
    assert not [e for e in result["events"] if e["ex_date"] == day]
    assert result["summary"]["junction_dropped_events"] and result["summary"]["junction_dropped_events"][0].startswith("yahoo")
    assert result["canonical"].loc[result["canonical"]["date"] == day, "split_factor"].iloc[0] == 1.0


def test_filler_before_a_relist_junction_is_cut_from_the_old_shares():
    n, close, frames = _relist_frames()
    volume = np.full(n, 1000.0)
    close = close.copy()
    close[16:20] = close[15]
    volume[16:20] = 0.0  # the old shares stop trading four sessions before the new ones start
    frames["yahoo"] = _frame(close, volume=volume)
    result = rc.reconcile_security("X", frames, {**_ctx(), "junctions": [str(SESSIONS[20].date())]})
    assert result["summary"]["segments"][0]["last"] == str(SESSIONS[15].date())
    assert result["summary"]["filler_cut"] == 4


def test_unreviewed_relisting_with_a_10x_level_change_is_queued_r9():
    n, close, frames = _relist_frames()
    day = str(SESSIONS[20].date())
    relists = [(str(SESSIONS[10].date()), str(SESSIONS[18].date()), str(SESSIONS[-1].date()))]
    result = rc.reconcile_security("X", frames, {**_ctx(), "relists": relists})
    assert "relist_jump" in _flags(result["canonical"], day)
    r9 = [m for m in result["moves"] if m["rule"] == "R9"]
    assert len(r9) == 1 and r9[0]["event_date"] == day and r9[0]["listed"]
    assert not [m for m in rc.reconcile_security("X", frames, _ctx())["moves"] if m["rule"] == "R9"]


def test_series_ends_lists_the_old_shares_at_a_relist_junction(tmp_path, monkeypatch):
    candidates = tmp_path / "candidates.csv"
    pd.DataFrame(columns=["security_id", "planned_source", "ticker_for_source"]).to_csv(candidates, index=False)
    monkeypatch.setattr(rc, "CANDIDATES", candidates)
    monkeypatch.setattr(rc, "TIINGO_STATUS", tmp_path / "missing.csv")
    monkeypatch.setattr(rc, "TERMINAL_FILES", [])
    terminal = tmp_path / "terminal.csv"
    pd.DataFrame([{"security_id": "J", "terminal_type": "bankruptcy_otc", "status": "awaiting_d5", "last_price_date": "",
                   "end_date": "2020-11-06"}]).to_csv(terminal, index=False)
    monkeypatch.setattr(rc, "TERMINAL_2012_2026", terminal)
    monkeypatch.setitem(rc.RELIST_JUNCTIONS, "J", {"first_new_session": "2020-11-20", "kind": "bankruptcy_new_equity",
                                                   "url": "u", "read": True, "note": ""})
    sessions = rc.pf.xnas_sessions("2020-01-02", rc.WINDOW_END)
    master = pd.DataFrame([{"security_id": sid, "name": sid, "delist_date": "2020-11-06", "last_listed": "2026-08-01",
                            "successor_security_id": "", "transfer_date": ""} for sid in ("J", "S")])
    identity = {"master": master, "ticker_map": rc.pf.TickerMap(pd.DataFrame(
        [{"security_id": "J", "ticker": "J", "list_start": "2020-01-02", "list_end": rc.WINDOW_END}])),
        "relisted": {"J": [("2020-11-06", "2020-11-20", rc.WINDOW_END)], "S": [("2020-11-06", "2021-01-04", rc.WINDOW_END)]}}
    end = str(sessions[-1].date())
    states = {"J": {"summary": {"rows": 100, "last_date": end, "ticker_last": "J", "junctions": ["2020-11-20"],
                                "segments": [{"first": "2020-01-02", "last": "2020-11-19", "last_src": "yahoo", "ticker": "OAS"},
                                             {"first": "2020-11-20", "last": end, "last_src": "tiingo"}]}},
              "S": {"summary": {"rows": 100, "last_date": end, "ticker_last": "S"}}}
    targets = pd.DataFrame({"security_id": ["J", "S"], "weeks_rank300": [10, 5], "in_candidates": [True, True]})
    ends = rc.series_ends(states, targets, identity, sessions)
    assert ends["security_id"].tolist() == ["J"]  # S, the same shares listed again, reaches the window end
    row = ends.iloc[0]
    assert row["category"] == "old_shares_at_relist_junction" and row["last_date"] == "2020-11-19"
    assert row["junction_date"] == "2020-11-20" and row["ticker_last"] == "OAS"
    assert row["terminal_2012_2026"].startswith("bankruptcy_otc/awaiting_d5")


# ------------------------------------------------------------------ the known stored-file break days

BREAK_SESSIONS = rc.pf.xnas_sessions("2025-06-02", "2025-07-15")


def _break_ctx():
    return {"sessions": BREAK_SESSIONS, "window": (BREAK_SESSIONS[0], BREAK_SESSIONS[-1]),
            "listed": lambda grid: np.ones(len(grid), dtype=bool), "ticker_of": lambda day: "T"}


def test_break_day_unit_change_by_a_non_ordinary_factor_is_a_unit_break():
    """HON 2025-06-24: the stored file goes 224.74 -> 425.80 (x1.895) while Yahoo shows -0.06%."""
    n = len(BREAK_SESSIONS)
    k = int(BREAK_SESSIONS.get_loc(pd.Timestamp(rc.BREAK_DAYS[0])))
    close = _walk(n, start=224.0)
    stored = close.copy()
    stored[k:] *= 1.895
    result = rc.reconcile_security("X", {"yahoo": _frame(close, sessions=BREAK_SESSIONS),
                                         "stored": _frame(stored, sessions=BREAK_SESSIONS)}, _break_ctx())
    breaks = [e for e in result["events"] if e["event_type"] == "unit_break"]
    assert len(breaks) == 1 and breaks[0]["ex_date"] == rc.BREAK_DAYS[0]
    assert breaks[0]["stored_implied_k"] == pytest.approx(1 / 1.895, rel=0.01)
    assert "no ordinary split ratio" in breaks[0]["notes"]
    assert result["summary"]["break_days"][0]["state"] == "unit_break"
    # an ordinary-ratio change keeps its note short (NFLX 2025-06-24, 10:1)
    stored10 = close.copy()
    stored10[k:] /= 10.0
    ten = rc.reconcile_security("X", {"yahoo": _frame(close, sessions=BREAK_SESSIONS),
                                      "stored": _frame(stored10, sessions=BREAK_SESSIONS)}, _break_ctx())
    note = [e for e in ten["events"] if e["event_type"] == "unit_break"][0]["notes"]
    assert "known 2025-06-24 break" in note and "no ordinary" not in note


def test_break_day_market_move_both_show_is_no_unit_break():
    """NKTR 2025-06-24: Yahoo and the stored file both go 9.54 -> 24.45 (+156%); step 6's stored-only
    split-ratio test lists the file, but nothing broke."""
    n = len(BREAK_SESSIONS)
    k = int(BREAK_SESSIONS.get_loc(pd.Timestamp(rc.BREAK_DAYS[0])))
    close = _walk(n, start=9.5)
    close[k:] *= 2.563
    result = rc.reconcile_security("X", {"yahoo": _frame(close, sessions=BREAK_SESSIONS),
                                         "stored": _frame(close, sessions=BREAK_SESSIONS)}, _break_ctx())
    assert not [e for e in result["events"] if e["event_type"] == "unit_break"]
    assert "stored_excluded" in _flags(result["canonical"], rc.BREAK_DAYS[0])  # still left out of the vote
    assert result["summary"]["break_days"][0]["state"] == "stored_moves_with_vendors"
    entry = [m for m in result["moves"] if m["event_date"] == rc.BREAK_DAYS[0]][0]
    assert entry["rule"] == "R1" and "moves the same" in entry["notes"]


# ------------------------------------------------------------------ the offline rebuild: out-dir and comparison

def test_configure_paths_sends_every_output_under_the_out_dir(tmp_path, monkeypatch):
    for name in ("PRICES_DIR", "OUT", "SOURCE_CACHE", "STATE_DIR", "LOG_DIR", "SPLIT_EVENTS", "SPECIAL", "REVIEWED_MOVES"):
        monkeypatch.setattr(rc, name, getattr(rc, name))  # restored after the test
    default_cache = rc.DEFAULT_SOURCE_CACHE
    rc.configure_paths(tmp_path)
    root = tmp_path.resolve()
    assert rc.PRICES_DIR == root / "prices" and rc.OUT == root / "reconcile"
    assert rc.STATE_DIR == root / "reconcile" / "per_security" and rc.SOURCE_CACHE == root / "reconcile" / "sources"
    assert {rc.SPLIT_EVENTS, rc.SPECIAL, rc.REVIEWED_MOVES} == {root / "inputs" / f for f in
                                                               ("split_events.csv", "special_distributions.csv",
                                                                "reviewed_moves.csv")}
    assert rc.DEFAULT_SOURCE_CACHE == default_cache  # still read (never written) when its signature matches
    assert rc.peak_rss_mb() > 0


def test_compare_series_counts_added_rows_and_changed_values():
    old = pd.DataFrame({"date": ["2020-01-02", "2020-01-03"], "close_raw": [10.0, 11.0], "volume_raw": [5.0, 5.0],
                        "split_factor": [1.0, 1.0], "div_cash": [0.0, 0.0], "tr": [np.nan, 0.1],
                        "src_primary": ["tiingo", "tiingo"], "flags": ["", ""]})
    same = rc.compare_series(old, old.copy())
    assert same["kind"] == "unchanged" and same["rows_added"] == 0
    new = pd.concat([old, old.iloc[[1]].assign(date="2020-01-06")], ignore_index=True)
    assert rc.compare_series(old, new)["kind"] == "dates" and rc.compare_series(old, new)["rows_added"] == 1
    changed = old.copy()
    changed.loc[1, "tr"] = np.nan  # a return blanked (a relist junction)
    out = rc.compare_series(old, changed)
    assert out["kind"] == "values" and out["tr_changed"] == 1 and out["close_raw_changed"] == 0
    flagged = old.copy()
    flagged.loc[0, "flags"] = "relist_junction"
    assert rc.compare_series(old, flagged)["kind"] == "flags_only"


def test_scan_series_restarts_the_dollar_volume_windows_at_a_relist_junction(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "PRICES_DIR", tmp_path)
    sessions = rc.pf.xnas_sessions("2020-01-02", "2020-08-31")
    n, k = len(sessions), 100
    close = np.r_[np.full(k, 100.0), np.full(n - k, 1.0)]  # old shares: $100M a day; new shares: $1M
    flags = [""] * n
    flags[k] = "relist_junction"
    pd.DataFrame({"date": sessions.strftime("%Y-%m-%d"), "close_raw": close, "volume_raw": 1_000_000.0,
                  "flags": flags, "n_sources": 1, "max_src_diff": 0.0}).to_csv(tmp_path / "J.csv", index=False)
    weeks = rc.pf.week_ends(sessions, "2020-01-03", "2020-08-28")
    cutoffs = pd.DataFrame({"cut50": 50e6, "cut20": 50e6}, index=weeks)
    dv_weeks = rc.scan_series(["J"], cutoffs)["dv_weeks"]["J"]
    assert len(dv_weeks) and dv_weeks.max() < sessions[k]  # no new-share week borrows the old shares' volume


def test_ensure_disk_refuses_to_write_below_the_free_space_limit(tmp_path):
    rc.ensure_disk(tmp_path / "prices" / "X.csv", min_free_mb=0)  # a path that does not exist yet is fine
    with pytest.raises(RuntimeError, match="free"):
        rc.ensure_disk(tmp_path / "X.csv", min_free_mb=1e12)


# ------------------------------------------------------------------ round 7: junction entries, zero-volume placeholders,
# the old shares' own end, the compare headline, the break-day guide, R9 always queued

def test_relist_junction_entries_were_read_and_carry_their_dates():
    assert set(rc.RELIST_JUNCTIONS) == {"1486159", "1839341", "105319", "1456772", "1556739", "20520", "1580864",
                                        "1009759"}
    for sid, entry in rc.RELIST_JUNCTIONS.items():
        assert entry["read"] is True and entry["read_on"], sid  # every document named was read by hand
        assert entry["url"].startswith(rc._SEC_ARCHIVE) and entry["nasdaq_end_url"].startswith(rc._SEC_ARCHIVE), sid
        assert entry["old_nasdaq_last_session"] < entry["effective_date"] < entry["first_new_session"], sid
        assert pd.Timestamp(entry["first_new_session"]).dayofweek < 5, sid
    assert rc.RELIST_JUNCTIONS["1456772"]["first_new_session"] == "2026-06-22"  # OPI: its first traded row
    assert rc.RELIST_JUNCTIONS["1456772"]["effective_date"] == "2026-06-17"
    assert rc.RELIST_JUNCTIONS["1839341"]["effective_date"] == "2024-01-23"
    assert rc.RELIST_JUNCTIONS["1556739"]["first_new_session"] == "2018-04-18"  # THRY: Dex Media's new shares


def test_new_share_rows_with_zero_volume_before_the_first_trade_are_cut_at_a_junction():
    """OPI: Yahoo's first new-share row (2026-06-18) has volume 0 at a placeholder price; the first trade is
    two sessions later at half that level. The junction sits on the first trade, and no -50% return is left."""
    n = 40
    old = _walk(20, start=13.0, seed=5)
    new = _walk(20, start=17.0, seed=6)
    close = np.r_[old, new]
    close[20:22] = 33.85  # the placeholder (and its repeat)
    volume = np.full(n, 1000.0)
    volume[20:22] = 0.0
    frames = {"yahoo": _frame(close, volume=volume)}
    placeholder, first_trade = str(SESSIONS[20].date()), str(SESSIONS[22].date())
    for junction in (placeholder, first_trade):  # an entry dated on the placeholder gives the same series
        result = rc.reconcile_security("X", frames, {**_ctx(), "junctions": [junction]})
        canonical = result["canonical"]
        assert placeholder not in set(canonical["date"])
        row = canonical[canonical["date"] == first_trade].iloc[0]
        assert np.isnan(row["tr"]) and "relist_junction" in row["flags"].split(";")
        assert not (canonical["tr"].abs() > 0.3).any()
        assert result["summary"]["segments"][1]["first"] == first_trade
    dated_on_placeholder = rc.reconcile_security("X", frames, {**_ctx(), "junctions": [placeholder]})
    assert dated_on_placeholder["summary"]["junction_leading_zero_volume_cut"] == [placeholder,
                                                                                   str(SESSIONS[21].date())]


def _series_ends_setup(tmp_path, monkeypatch, junction, segments, unfillable=None):
    candidates = tmp_path / "candidates.csv"
    pd.DataFrame(columns=["security_id", "planned_source", "ticker_for_source"]).to_csv(candidates, index=False)
    monkeypatch.setattr(rc, "CANDIDATES", candidates)
    monkeypatch.setattr(rc, "TIINGO_STATUS", tmp_path / "missing.csv")
    monkeypatch.setattr(rc, "TERMINAL_FILES", [])
    monkeypatch.setattr(rc, "TERMINAL_2012_2026", tmp_path / "missing_terminal.csv")
    path = tmp_path / "unfillable.csv"
    pd.DataFrame(unfillable or [], columns=["security_id", "status"]).to_csv(path, index=False)
    monkeypatch.setattr(rc, "UNFILLABLE", path)
    monkeypatch.setitem(rc.RELIST_JUNCTIONS, "J", junction)
    sessions = rc.pf.xnas_sessions("2016-01-04", rc.WINDOW_END)
    master = pd.DataFrame([{"security_id": "J", "name": "J", "delist_date": "2025-11-24", "last_listed": "2026-08-01",
                            "successor_security_id": "", "transfer_date": ""}])
    identity = {"master": master, "ticker_map": rc.pf.TickerMap(pd.DataFrame(
        [{"security_id": "J", "ticker": "J", "list_start": "2016-01-04", "list_end": rc.WINDOW_END}])),
        "relisted": {"J": [("2025-11-24", "2026-06-02", rc.WINDOW_END)]}}
    end = str(sessions[-1].date())
    states = {"J": {"summary": {"rows": 100, "last_date": end, "ticker_last": "J",
                                "junctions": [junction["first_new_session"]], "segments": segments}}}
    targets = pd.DataFrame({"security_id": ["J"], "weeks_rank300": [8], "in_candidates": [True]})
    return rc.series_ends(states, targets, identity, sessions).iloc[0], sessions


def test_old_shares_that_stop_at_the_wiki_end_have_a_data_gap_not_a_junction_end(tmp_path, monkeypatch):
    """OPI: the old shares' series stops at the WIKI end (2018-03-27); they traded on Nasdaq to 2025-10-06."""
    junction = {"first_new_session": "2026-06-22", "kind": "bankruptcy_new_equity", "url": "u", "read": True,
                "effective_date": "2026-06-17", "old_nasdaq_last_session": "2025-10-06", "note": ""}
    segments = [{"first": "2016-06-23", "last": rc.WIKI_END, "last_src": "wiki", "ticker": "GOV"},
                {"first": "2026-06-22", "last": rc.WINDOW_END, "last_src": "yahoo"}]
    row, sessions = _series_ends_setup(tmp_path, monkeypatch, junction, segments,
                                       unfillable=[{"security_id": "J", "status": "wrong_entity"}])
    assert row["category"] == "old_shares_at_relist_junction" and row["last_date"] == rc.WIKI_END
    assert row["likely_cause"] == "wiki_end_no_later_source"
    assert (row["old_nasdaq_last_session"], row["old_shares_cancelled"]) == ("2025-10-06", "2026-06-17")
    gap = int(((sessions > pd.Timestamp(rc.WIKI_END)) & (sessions <= pd.Timestamp("2025-10-06"))).sum())
    assert row["sessions_after_last_row"] == gap and row["unfillable_status"] == "wrong_entity"


def test_old_shares_that_run_on_into_the_otc_months_reach_their_nasdaq_end(tmp_path, monkeypatch):
    """CHRD: the old OAS series runs to 2020-11-19 (OTC), past its last Nasdaq session 2020-10-09."""
    junction = {"first_new_session": "2020-11-20", "kind": "bankruptcy_new_equity", "url": "u", "read": True,
                "effective_date": "2020-11-19", "old_nasdaq_last_session": "2020-10-09", "note": ""}
    segments = [{"first": "2019-12-27", "last": "2020-11-19", "last_src": "yahoo", "ticker": "OAS"},
                {"first": "2020-11-20", "last": rc.WINDOW_END, "last_src": "tiingo"}]
    row, _ = _series_ends_setup(tmp_path, monkeypatch, junction, segments)
    assert row["sessions_after_last_row"] == 0 and row["likely_cause"] == "relist_junction:bankruptcy_new_equity"
    assert row["unfillable_status"] == ""


def test_compare_headline_counts_value_changes_inside_the_common_dates_of_extended_series():
    old = pd.DataFrame({"date": ["2018-03-26", "2018-03-27"], "close_raw": [10.0, 11.0], "volume_raw": [5.0, 5.0],
                        "split_factor": [1.0, 1.0], "div_cash": [0.0, 0.0], "tr": [np.nan, 0.1],
                        "src_primary": ["wiki", "wiki"], "flags": ["", ""]})
    new = pd.concat([old, old.iloc[[1]].assign(date="2018-03-28")], ignore_index=True)
    new.loc[1, ["src_primary", "tr"]] = ["tiingo", 0.1002]  # Tiingo replaced WIKI on a common date
    extended = rc.compare_series(old, new)
    assert extended["kind"] == "dates" and extended["dates_values"] and extended["common_rows_values_changed"] == 1
    plain = rc.compare_series(old, pd.concat([old, old.iloc[[1]].assign(date="2018-03-28")], ignore_index=True))
    assert plain["kind"] == "dates" and not plain["dates_values"] and plain["common_rows_values_changed"] == 0
    frame = pd.DataFrame([{"security_id": "A", **extended}, {"security_id": "B", **plain},
                          {"security_id": "C", "kind": "added", "old_rows": 0, "new_rows": 3}])
    headline = rc.dates_value_changes(frame)
    assert (headline["securities"], headline["rows"], headline["dates_series"]) == (1, 1, 2)
    assert headline["by_column"]["tr_changed"] == 1 and headline["by_column"]["src_primary_changed"] == 1


def test_break_day_table_tells_a_stored_only_test_how_to_read_each_state():
    states = {"1": {"summary": {"ticker_last": "NKTR", "break_days": [
                  {"date": "2025-06-24", "state": "stored_moves_with_vendors", "stored_r": 1.56, "tr": 1.56,
                   "stored_implied_k": 1.0}]}},
              "2": {"summary": {"ticker_last": "HON", "break_days": [
                  {"date": "2025-06-24", "state": "unit_break", "stored_r": 0.895, "tr": -0.0006,
                   "stored_implied_k": 0.527}]}},
              "3": {"summary": {"ticker_last": "ZZZ"}}}
    table = rc.break_day_table(states)
    assert set(table["how_to_read"]["states"]) == {"unit_break", "stored_moves_with_vendors",
                                                   "unit_change_on_vendor_event", "stored_differs"}
    assert "priced_without_unit_break_row" in table["how_to_read"]["for_a_stored_only_test"]
    day = table["days"]["2025-06-24"]
    assert day["securities_by_state"] == {"stored_moves_with_vendors": ["1 NKTR"], "unit_break": ["2 HON"]}
    assert day["securities"]["1"]["ticker"] == "NKTR" and day["by_state"] == {"stored_moves_with_vendors": 1,
                                                                              "unit_break": 1}


def test_every_r9_hit_is_queued_even_outside_the_relevant_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "OUT", tmp_path)
    monkeypatch.setattr(rc, "REVIEWED_FORMAT", tmp_path / "missing.csv")
    monkeypatch.setattr(rc, "relevant_spans", lambda dv_weeks: {})  # nothing ranked: no entry is relevant
    moves = [{"ticker": "THRY", "event_date": "2018-04-18", "classification": "unreviewed", "source_url": "",
              "verified_at": "", "notes": "[R9] raw close ...", "security_id": "1556739", "sources_agreeing": "",
              "rule": "R9", "listed": True},
             {"ticker": "THRY", "event_date": "2018-04-19", "classification": "unreviewed", "source_url": "",
              "verified_at": "", "notes": "[R4] zero volume", "security_id": "1556739", "sources_agreeing": "",
              "rule": "R4", "listed": True}]
    queue, facts = rc.build_move_queue({"1556739": {"moves": moves}}, {})
    assert queue["event_date"].tolist() == ["2018-04-18"] and facts["r9_queued_outside_scope"] == 1
    assert "outside the relevant scope" in queue["notes"].iloc[0]
    assert (tmp_path / "moves_all.csv").exists()


# ------------------------------------------------------------------ round 8: no price level in a committed note, the
# placeholder before a junction counted as one, a listing start measured against a quote

LEVEL_FREE = re.compile(r"\d{4}-\d{2}-\d{2}|\d+(?:\.\d+)?x|[+-]?\d+(?:\.\d+)?%")


def test_r9_note_carries_dates_and_the_ratio_but_no_price_level():
    n, close, frames = _relist_frames()
    relists = [(str(SESSIONS[10].date()), str(SESSIONS[18].date()), str(SESSIONS[-1].date()))]
    result = rc.reconcile_security("X", frames, {**_ctx(), "relists": relists})
    note = [m for m in result["moves"] if m["rule"] == "R9"][0]["notes"]
    assert str(SESSIONS[19].date()) in note and str(SESSIONS[20].date()) in note and "x from" in note
    for level in (close[19], close[20]):  # neither close, at any rounding the note could use
        assert f"{level:.4g}" not in note and f"{level:.2f}" not in note
    assert not re.search(r"\d+\.\d+", LEVEL_FREE.sub("", note))  # only dates, ratios and percentages
    assert not rc.LEVEL_IN_NOTE.search(note)


def test_the_queue_refuses_a_note_with_a_price_level(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "OUT", tmp_path)
    monkeypatch.setattr(rc, "REVIEWED_FORMAT", tmp_path / "missing.csv")
    monkeypatch.setattr(rc, "relevant_spans", lambda dv_weeks: {"1": [("2015-01-01", "2015-12-31")]})
    base = {"ticker": "T", "event_date": "2015-02-02", "classification": "unreviewed", "source_url": "",
            "verified_at": "", "security_id": "1", "sources_agreeing": "", "listed": True}
    ratios = [{**base, "rule": "R7", "notes": "[R7] tiingo raw close 1.0214x of yahoo on 3 session(s) to 2015-02-04"},
              {**base, "rule": "R4", "notes": "[R4] 5 identical raw closes to 2015-02-09; zero volume"}]
    queue, _ = rc.build_move_queue({"1": {"moves": ratios}}, {})
    assert len(queue) == 2  # ratios and counts pass
    leaking = ratios + [{**base, "rule": "R9", "notes": "[R9] raw close 1.125 on 2015-01-30 -> 0.075 (0.0667x)"}]
    with pytest.raises(ValueError, match="price level"):
        rc.build_move_queue({"1": {"moves": leaking}}, {})


def test_a_placeholder_before_the_first_traded_session_is_a_junction_cut_not_old_share_filler():
    """OPI: the entry's first_new_session is the first trade (2026-06-22); Yahoo's 2026-06-18 row, after the plan's
    effective date (2026-06-17), has volume 0. It is reported as the new shares' placeholder; a zero-volume row
    of the old shares before the effective date stays R5 filler."""
    n = 40
    close = np.r_[_walk(20, start=13.0, seed=5), _walk(20, start=17.0, seed=6)]
    volume = np.full(n, 1000.0)
    close[18:22] = [close[17], 33.85, 33.85, 33.85]
    volume[18:22] = 0.0  # 18: the old shares' filler; 20, 21: the new shares' placeholder; 19: none
    frames = {"yahoo": _frame(close, volume=volume)}
    frames["yahoo"] = frames["yahoo"].drop(index=19)  # the effective date itself has no vendor row
    effective, first_trade = str(SESSIONS[19].date()), str(SESSIONS[22].date())
    ctx = {**_ctx(), "junctions": [first_trade], "junction_effective": {first_trade: effective}}
    result = rc.reconcile_security("X", frames, ctx)
    summary = result["summary"]
    assert summary["junction_leading_zero_volume_cut"] == [str(SESSIONS[20].date()), str(SESSIONS[21].date())]
    assert summary["filler_cut"] == 1  # the old shares' own zero-volume row before the effective date
    canonical = result["canonical"]
    assert not set(canonical["date"]) & {str(SESSIONS[k].date()) for k in (18, 20, 21)}
    row = canonical[canonical["date"] == first_trade].iloc[0]
    assert np.isnan(row["tr"]) and "relist_junction" in row["flags"].split(";")
    assert summary["segments"][0]["last"] == str(SESSIONS[17].date())
    # without the effective date the same rows are all the old shares' filler (the series is the same)
    plain = rc.reconcile_security("X", frames, {**_ctx(), "junctions": [first_trade]})
    assert plain["summary"]["junction_leading_zero_volume_cut"] == [] and plain["summary"]["filler_cut"] == 3
    assert plain["canonical"]["date"].tolist() == canonical["date"].tolist()


def test_relist_junction_entries_pass_their_effective_date_to_the_placeholder_cut():
    assert rc.junction_effective_of("1456772") == {"2026-06-22": "2026-06-17"}
    assert rc.junction_effective_of("0") == {}


def test_a_listing_start_measured_against_a_zero_volume_quote_has_no_return():
    """THRY 2020-10-01: the direct listing's first row after a long run of identical zero-volume Yahoo closes."""
    n = 40
    close = _walk(n)
    close[:20] = 5.865  # the quote
    volume = np.full(n, 1000.0)
    volume[:20] = 0.0
    listed = lambda grid: np.arange(len(grid)) >= 20
    result = rc.reconcile_security("X", {"yahoo": _frame(close, volume=volume)}, _ctx(listed))
    day = str(SESSIONS[20].date())
    row = result["canonical"][result["canonical"]["date"] == day].iloc[0]
    assert np.isnan(row["tr"]) and row["n_sources"] == 0
    assert "listing_start_after_quote" in row["flags"].split(";") and "move_40" not in row["flags"].split(";")
    assert result["summary"]["listing_starts_after_quote"] == [day]
    assert not [m for m in result["moves"] if m["event_date"] == day and m["rule"].startswith("R1")]
    assert result["canonical"]["tr"].iloc[21:].notna().all()
    # a trade outside the listing within the staleness span is a price: the return stands
    traded = volume.copy()
    traded[17] = 500.0
    kept = rc.reconcile_security("X", {"yahoo": _frame(close, volume=traded)}, _ctx(listed))
    row = kept["canonical"][kept["canonical"]["date"] == day].iloc[0]
    assert np.isfinite(row["tr"]) and "listing_start_after_quote" not in row["flags"].split(";")
    assert "listing_starts_after_quote" not in kept["summary"]


# ------------------------------------------------------------------ round 9: gaps, successor links, closing special
# dividends, the listing-start quote rule, mechanical R1 classification, relistings under a new ticker, superseded files

def test_no_return_across_a_gap_of_more_than_ten_sessions():
    """Plan R9 (Frontier 2018-03-01: 213 sessions with a 1:15 reverse split in them): a gap of more than
    GAP_RETURN_MAX sessions leaves tr blank; a gap of exactly ten keeps its return."""
    close = _walk(60)
    for missing, blank in ((11, True), (10, False)):
        keep = [k for k in range(60) if not 20 <= k < 20 + missing]
        frame = _frame(close)
        frame = frame.iloc[keep].reset_index(drop=True)
        result = rc.reconcile_security("X", {"yahoo": frame}, _ctx())
        day = str(SESSIONS[20 + missing].date())
        row = result["canonical"][result["canonical"]["date"] == day].iloc[0]
        assert np.isnan(row["tr"]) == blank, missing
        assert ("gap_return_blank" in row["flags"].split(";")) == blank, missing
        assert f"gap_before:{missing}" in row["flags"].split(";")
        if blank:
            assert result["summary"]["gap_returns_blank"] == [f"{day} (gap {missing})"]
            assert row["n_sources"] == 0
            assert not [m for m in result["moves"] if m["event_date"] == day and m["rule"].startswith("R1")]


def _link_bundles(pred_close, succ_yahoo):
    """A predecessor P with WIKI rows 0..29 (rows 20..29 are the successor's, which the ticker map gave P), a Tiingo
    file fetched for P with a filler row after its last session (21CF), and a successor S whose Yahoo file carries
    the ticker's whole history, 0..39."""
    wiki = _frame(pred_close[:30]).assign(security_id="P", file="wiki/T.csv.gz")
    tiingo = _frame(pred_close[:21]).assign(security_id="P", file="tiingo_run/P.csv")
    tiingo.loc[20, ["close", "volume"]] = [pred_close[19], 65.0]
    yahoo = _frame(succ_yahoo).assign(security_id="S", file="yahoo/S.csv.gz")
    return {"wiki": {"P": wiki}, "tiingo_new": {"P": tiingo}, "tiingo_old": {}, "yahoo_new": {"S": yahoo},
            "yahoo_old": {}, "stored": {}}


@pytest.mark.parametrize("continues", [True, False])
def test_a_successor_link_cuts_the_predecessor_and_a_continuing_one_measures_the_first_return_from_its_close(
        monkeypatch, continues):
    close = _walk(40)
    cut = str(SESSIONS[19].date())
    monkeypatch.setitem(rc.SUCCESSOR_LINKS, "P", {"successor": "S", "ticker": "T", "last_session": cut,
                                                  "continues": continues, "basis": "test", "url": "u"})
    bundles = _link_bundles(close, close)
    frames, facts = rc.link_frames("S", rc.frames_for("S", bundles), bundles, "P")
    assert facts["own_rows_dropped"] == 20  # the successor's Yahoo rows up to the cut are the predecessor's
    # the ticker-mapped WIKI rows after the cut always go over; the predecessor's own Tiingo file only when it continues
    assert facts["predecessor_rows_added"] == (11 if continues else 10)
    assert ("tiingo" in frames) == continues
    assert facts["anchor_sources"] == (["wiki", "tiingo", "yahoo"] if continues else [])
    ctx = {**_ctx(), "link_cut": cut, "link_continues": continues, "link_predecessor": "P"}
    result = rc.reconcile_security("S", frames, ctx)
    canonical = result["canonical"]
    first = canonical.iloc[0]
    assert first["date"] == str(SESSIONS[20].date()) and cut not in set(canonical["date"])  # no day in both series
    if continues:
        assert first["tr"] == pytest.approx(close[20] / close[19] - 1)
        assert "successor_link:P" in first["flags"].split(";") and first["n_sources"] >= 2
        assert result["summary"]["successor_link"]["first_row_return"] == "measured"
    else:
        assert np.isnan(first["tr"]) and "successor_of:P" in first["flags"].split(";")
        assert result["summary"]["successor_link"]["first_row_return"] == "blank"
    assert canonical["tr"].iloc[1:].notna().all()


ROUND10_CONTINUED = {"885721", "1058057", "1005201", "912752", "1602065", "1100441", "1334814", "1316631.B",
                     "1560385.T-LMCA", "1560385.T-LMCK", "1355096.T-LINTA", "1355096.T-LINTB", "1355096.T-QVCA",
                     "1355096.T-QVCB", "1355096.T-QRTEA", "1355096.T-QRTEB", "1038205"}


def test_successor_links_cover_the_26_round_8_pairs_and_continue_only_one_for_one_reorganisations():
    from scripts import reversal_data_terminal as terminal

    assert len(rc.SUCCESSOR_LINKS) == 26 + 17 + 3  # round 8's pairs, round 10's 1:1 pairs and its three cuts
    cut_only = {p for p, link in rc.SUCCESSOR_LINKS.items() if not link["continues"]}
    assert cut_only == {"1308161.A", "1308161.B", "356213", "1491778", "929940",  # election, cash or other shares
                        "1326807", "1620280", "1734342.B"}  # round 10: ISBC 2.55, UNIT 0.6029, AMTBB folded into A
    assert ROUND10_CONTINUED <= {p for p, link in rc.SUCCESSOR_LINKS.items() if link["continues"]}
    for pred, link in rc.SUCCESSOR_LINKS.items():
        assert link["successor"] != pred and pd.Timestamp(link["last_session"]).dayofweek < 5, pred
        assert link["url"].startswith("https://www.sec.gov/Archives/edgar/data/"), pred
        review = terminal.REVIEWED.get(pred) or {}
        if link["continues"] and review:
            assert review.get("shares", 1.0) == 1.0 and review.get("cash") is None, pred
            assert review.get("sub", "reorganization") in ("reorganization", "rename"), pred
            assert review.get("acq") == link["successor"], pred
            if review.get("limit"):  # the terminal step reads the same last session
                assert review["limit"] == link["last_session"], pred
    assert rc.SUCCESSOR_LINKS["1288776.A"]["last_session"] == "2015-10-02"  # Google's last old row
    assert rc.SUCCESSOR_LINKS["1058057"]["last_session"] == "2021-04-20"  # Marvell: 4:01 p.m. ET Bermuda merger
    assert rc.SUCCESSOR_LINKS["885721"]["last_session"] == "2012-03-30"  # ESRX: halted before the open on 04-02
    assert rc.SUCCESSOR_LINKS["912752"]["last_session"] == "2023-05-31"  # Sinclair: 12:00 am on 2023-06-01
    # a class folded into another existing class hands nothing over: Class A is no successor
    assert rc.SUCCESSOR_LINKS["1734342.B"]["handover"] is False and "1734342.A" not in rc.successor_of()
    assert len(set(rc.successor_of())) == len(rc.SUCCESSOR_LINKS) - 1  # one predecessor per successor


def test_load_targets_prices_the_successor_of_a_continuing_target(tmp_path, monkeypatch):
    candidates = tmp_path / "candidates.csv"
    pd.DataFrame([{"security_id": "P", "reason": "A1_x", "planned_source": "wiki", "status": "done"}]).to_csv(
        candidates, index=False)
    weekly = tmp_path / "weekly.pkl"
    pd.DataFrame({"security_id": ["P"], "week_end": [pd.Timestamp("2015-01-02")], "dv50_rank": [5], "dv20_rank": [5],
                  "dv50": [1.0], "dv20": [1.0]}).to_pickle(weekly)
    monkeypatch.setattr(rc, "CANDIDATES", candidates)
    monkeypatch.setattr(rc, "WEEKLY_METRICS", weekly)
    monkeypatch.setattr(rc, "SUCCESSOR_LINKS", {"P": {"successor": "S", "continues": True, "last_session": "2015-01-02"},
                                                "Q": {"successor": "R", "continues": True, "last_session": "2015-01-02"}})
    targets = rc.load_targets().set_index("security_id")
    assert list(targets.index) == ["P", "S"]  # Q is no target, so neither is R
    assert targets.loc["S", "successor_of_target"] and not targets.loc["P", "successor_of_target"]


def test_a_closing_special_dividend_the_terminal_value_owns_is_not_booked_in_the_series():
    """CHNG: Tiingo books the $2.00 special dividend on 2022-09-28, five days before its record date at the
    closing; the last close still carries it, so the terminal value owns it and the series must not book it."""
    close = _walk(40)
    div = np.zeros(40)
    div[25] = 2.0
    div[10] = 0.5  # an ordinary dividend stays
    frames = {"tiingo": _frame(close, div=div)}
    record = str(SESSIONS[28].date())
    ctx = {**_ctx(), "terminal_dividends": [{"amount": 2.0, "record": record, "url": "u"}]}
    result = rc.reconcile_security("X", frames, ctx)
    canonical = result["canonical"]
    row = canonical[canonical["date"] == str(SESSIONS[25].date())].iloc[0]
    assert row["div_cash"] == 0.0 and row["tr"] == pytest.approx(close[25] / close[24] - 1)
    assert "div_in_terminal_value" in row["flags"].split(";")
    assert result["summary"]["terminal_dividends_dropped"][0]["date"] == str(SESSIONS[25].date())
    assert [d["ex_date"] for d in result["dividends"]] == [str(SESSIONS[10].date())]
    plain = rc.reconcile_security("X", frames, _ctx())
    assert [d["ex_date"] for d in plain["dividends"]] == [str(SESSIONS[10].date()), str(SESSIONS[25].date())]


def test_terminal_owned_dividends_come_from_the_terminal_steps_reviewed_entries():
    owned = rc.terminal_owned_dividends()
    assert owned["1756497"] == [{"amount": 2.0, "record": "2022-10-03",
                                 "url": "https://www.sec.gov/Archives/edgar/data/1756497/000119312522256246/d403452d8k.htm"}]
    assert owned["1581164"][0]["amount"] == 1.75 and owned["1581164"][0]["record"] == "2021-06-15"
    assert "1470215" not in owned  # paid before the last trade: the series keeps it


def test_a_zero_volume_listing_start_keeps_its_return_and_the_first_trade_after_a_quote_is_blank():
    """Round 8 review: the rule acts on the listing's first traded row. Yahoo files with volume 0 every day while the
    close moves keep their returns; an IPO whose first row has volume 0 at a reference price has its first trade
    blank, not the zero-volume row."""
    close = _walk(40)
    listed = lambda grid: np.arange(len(grid)) >= 20
    moving = rc.reconcile_security("X", {"yahoo": _frame(close, volume=0.0)}, _ctx(listed))
    row = moving["canonical"][moving["canonical"]["date"] == str(SESSIONS[20].date())].iloc[0]
    assert np.isfinite(row["tr"]) and "listing_start_after_quote" not in row["flags"].split(";")
    assert "listing_starts_after_quote" not in moving["summary"]
    # the IPO: a zero-volume reference row on the first listed day, then the first trade
    ipo = close.copy()
    volume = np.full(40, 1000.0)
    volume[20] = 0.0
    frame = _frame(ipo, volume=volume).iloc[20:].reset_index(drop=True)
    result = rc.reconcile_security("X", {"yahoo": frame}, _ctx(listed))
    first_trade = str(SESSIONS[21].date())
    row = result["canonical"][result["canonical"]["date"] == first_trade].iloc[0]
    assert np.isnan(row["tr"]) and "listing_start_after_quote" in row["flags"].split(";")
    assert result["summary"]["listing_starts_after_quote"] == [first_trade]
    # a listing older than the grid (every session listed) shows no listing start: thin zero-volume days stay
    thin = np.full(40, 1000.0)
    thin[:5] = 0.0
    flat = close.copy()
    flat[:6] = flat[0]
    old = rc.reconcile_security("X", {"yahoo": _frame(flat, volume=thin)}, _ctx())
    assert "listing_starts_after_quote" not in old["summary"]


def test_r1_entries_two_sources_confirm_are_classified_mechanically_and_the_rest_stay_unreviewed(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "OUT", tmp_path)
    monkeypatch.setattr(rc, "REVIEWED_FORMAT", tmp_path / "missing.csv")
    monkeypatch.setattr(rc, "relevant_spans", lambda dv_weeks: {"1": [("2015-01-01", "2015-12-31")]})
    base = {"ticker": "T", "classification": "unreviewed", "source_url": "", "verified_at": "", "security_id": "1",
            "listed": True}
    moves = [{**base, "event_date": "2015-02-02", "rule": "R1", "sources_agreeing": "yahoo+stored",
              "notes": "[R1] move of 2.40x; 2 source(s) agree within 0.5%"},
             {**base, "event_date": "2015-02-03", "rule": "R1", "sources_agreeing": "wiki+tiingo",
              "notes": "[R1] move of 0.40x; 2 source(s) agree within 0.5%"},
             {**base, "event_date": "2015-02-04", "rule": "R1", "sources_agreeing": "wiki",
              "notes": "[R1] move of 2.10x; 1 source(s) agree within 0.5%"},
             {**base, "event_date": "2015-02-05", "rule": "R1b", "sources_agreeing": "yahoo+stored",
              "notes": "[R1b] move of +45% confirmed by no second source"},
             {**base, "event_date": "2015-02-06", "rule": "R1/R2", "sources_agreeing": "yahoo+stored",
              "notes": "[R1/R2] price ratio 2.00x with no split in any source"}]
    queue, facts = rc.build_move_queue({"1": {"moves": moves}}, {})
    by_day = queue.set_index("event_date")
    assert by_day.loc["2015-02-02", "classification"] == by_day.loc["2015-02-03", "classification"] == \
        rc.R1_MECHANICAL_CLASS
    assert "stored file counts as a second source" in by_day.loc["2015-02-02", "notes"]
    assert "stored file" not in by_day.loc["2015-02-03", "notes"] and by_day.loc["2015-02-03", "verified_at"]
    assert (by_day.loc[["2015-02-04", "2015-02-05", "2015-02-06"], "classification"] == "unreviewed").all()
    mech = facts["classified_mechanically"]
    assert (mech["rows"], mech["with_two_vendors"], mech["vendor_and_stored"]) == (2, 1, 1)
    assert facts["unreviewed"] == 3


def test_a_form25_followed_by_a_listing_under_a_new_ticker_is_a_relisting():
    """Frontier: FTR delisted by a 2020 Form 25, FYBR listed from 2021-05-04; the master's delist date is the 2026
    Form 25 (Verizon), so the after_cut rule cannot see the 2020 one."""
    mapping = pd.DataFrame([{"security_id": "20520", "ticker": "FTR", "list_start": "2012-01-25", "list_end": "2020-05-09"},
                            {"security_id": "20520", "ticker": "FYBR", "list_start": "2021-05-04", "list_end": "2026-01-30"},
                            {"security_id": "7", "ticker": "SEV", "list_start": "2012-01-03", "list_end": "2019-06-01"},
                            {"security_id": "7", "ticker": "SEVN", "list_start": "2019-07-01", "list_end": "2026-08-31"}])
    master = pd.DataFrame({"security_id": ["20520", "7"], "cik": ["20520", "7"], "delist_date": ["2026-01-30", ""]})
    form25 = pd.DataFrame([
        {"subject_cik": "0000020520", "effective_date": "2020-05-09", "filing_date": "2020-04-29",
         "classification": "common_delisting"},
        {"subject_cik": "0000020520", "effective_date": "2026-01-30", "filing_date": "2026-01-20",
         "classification": "common_delisting"},
        {"subject_cik": "7", "effective_date": "2019-06-15", "filing_date": "2019-06-05", "classification": "transfer"}])
    found = rc.form25_relistings(mapping, master, form25)
    assert found == {"20520": [("2020-05-09", "2021-05-04", "2026-01-30")]}  # a transfer Form 25 is no relisting
    assert rc.form25_relistings(mapping, master, None) == {}


def test_series_ends_measures_a_relisting_that_was_delisted_again_against_its_later_delisting(tmp_path, monkeypatch):
    candidates = tmp_path / "candidates.csv"
    pd.DataFrame(columns=["security_id", "planned_source", "ticker_for_source"]).to_csv(candidates, index=False)
    monkeypatch.setattr(rc, "CANDIDATES", candidates)
    monkeypatch.setattr(rc, "TIINGO_STATUS", tmp_path / "missing.csv")
    monkeypatch.setattr(rc, "TERMINAL_FILES", [])
    monkeypatch.setattr(rc, "TERMINAL_2012_2026", tmp_path / "missing_terminal.csv")
    monkeypatch.setattr(rc, "UNFILLABLE", tmp_path / "missing_unfillable.csv")
    sessions = rc.pf.xnas_sessions("2020-01-02", rc.WINDOW_END)
    master = pd.DataFrame([{"security_id": "F", "name": "F", "delist_date": "2026-01-30", "last_listed": "2026-01-01",
                            "successor_security_id": "", "transfer_date": ""}])
    identity = {"master": master, "ticker_map": rc.pf.TickerMap(pd.DataFrame(
        [{"security_id": "F", "ticker": "F", "list_start": "2020-01-02", "list_end": "2026-01-30"}])),
        "relisted": {"F": [("2020-05-09", "2021-05-04", "2026-01-30")]}}
    states = {"F": {"summary": {"rows": 100, "last_date": "2026-01-16", "ticker_last": "FYBR"}}}
    targets = pd.DataFrame({"security_id": ["F"], "weeks_rank300": [99], "in_candidates": [True]})
    row = rc.series_ends(states, targets, identity, sessions).iloc[0]
    assert (row["category"], row["delist_date"]) == ("ends_before_delist", "2026-01-30")


def test_rebuild_moves_price_files_of_non_targets_aside_and_never_deletes_them(tmp_path, monkeypatch):
    prices, out = tmp_path / "prices", tmp_path / "reconcile"
    prices.mkdir()
    for name in ("1.csv", "817473.csv", "daily_panel.csv.gz"):
        (prices / name).write_text("x")
    monkeypatch.setattr(rc, "PRICES_DIR", prices)
    monkeypatch.setattr(rc, "OUT", out)
    moved = rc.supersede_non_targets(["1"])
    assert [m["security_id"] for m in moved] == ["817473"]
    assert sorted(p.name for p in prices.iterdir()) == ["1.csv", "daily_panel.csv.gz"]
    target = rc.superseded_dir() / "817473.csv"
    assert target.exists() and target.parent.parent == out
    (prices / "817473.csv").write_text("y")  # a second run the same day keeps both copies
    rc.supersede_non_targets(["1"])
    assert sorted(p.name for p in rc.superseded_dir().iterdir()) == ["817473.1.csv", "817473.csv"]
    # an emptied series is moved aside too, not deleted
    monkeypatch.setattr(rc, "STATE_DIR", tmp_path / "state")
    rc.save_result("1", {"canonical": pd.DataFrame(columns=rc.PRICE_COLUMNS), "summary": {"rows": 0}}, "sig")
    assert not (prices / "1.csv").exists() and (rc.superseded_dir() / "1.csv").exists()


def test_dividend_and_successor_link_tables(monkeypatch):
    states = {"1": {"dividends": [{"ex_date": "2015-02-02", "cash_as_paid": 0.5, "sources": "tiingo+yahoo",
                                   "ticker": "A", "src_primary": "tiingo", "other_amounts": "", "special": ""}],
                    "summary": {"rows": 10}},
              "P": {"summary": {"rows": 10, "last_date": "2015-01-30", "successor_link_rows":
                                {"rows_after_last_session_cut": 3}}},
              "S": {"summary": {"rows": 10, "first_date": "2015-02-02",
                                "successor_link": {"first_row": "2015-02-02", "first_row_return": "measured",
                                                   "sessions_between": 0, "anchor_sources": ["wiki"]},
                                "successor_link_rows": {"own_rows_dropped": 5, "predecessor_rows_added": 3}}}}
    frame, facts = rc.dividend_table(states)
    assert list(frame.columns[:4]) == ["security_id", "ex_date", "cash_as_paid", "sources"]
    assert (facts["rows"], facts["rows_one_source"]) == (1, 0)
    monkeypatch.setattr(rc, "SUCCESSOR_LINKS", {"P": {"successor": "S", "ticker": "T", "last_session": "2015-01-30",
                                                      "continues": True, "basis": "b", "url": "u", "note": ""}})
    links, link_facts = rc.successor_link_table(states)
    row = links.iloc[0]
    assert (row["status"], row["predecessor_rows_cut"], row["anchor_sources"]) == ("continued", 3, "wiki")
    assert link_facts["by_status"] == {"continued": 1}


# ------------------------------------------------------------------ round 10: successor links

def test_a_thin_predecessor_is_anchored_on_its_last_row_before_the_cut(monkeypatch):
    # LBTYB: no trade on the last session itself (2013-06-07); the anchor is the last row before it
    close = _walk(40)
    cut = str(SESSIONS[19].date())
    monkeypatch.setitem(rc.SUCCESSOR_LINKS, "P", {"successor": "S", "ticker": "T", "last_session": cut,
                                                  "continues": True, "basis": "test", "url": "u"})
    wiki = pd.concat([_frame(close[:18]), _frame(close[20:30], start=20)]).assign(security_id="P", file="wiki/T.csv.gz")
    bundles = {"wiki": {"P": wiki}, "tiingo_new": {}, "tiingo_old": {}, "yahoo_new": {}, "yahoo_old": {}, "stored": {}}
    frames, facts = rc.link_frames("S", rc.frames_for("S", bundles), bundles, "P", SESSIONS)
    assert facts["anchor_sources"] == ["wiki"] and frames["wiki"]["date"].min() == SESSIONS[17]
    ctx = {**_ctx(), "link_cut": cut, "link_continues": True, "link_predecessor": "P"}
    result = rc.reconcile_security("S", frames, ctx)
    first = result["canonical"].iloc[0]
    assert first["date"] == str(SESSIONS[20].date())
    assert first["tr"] == pytest.approx(close[20] / close[17] - 1)  # over the two sessions without a row
    assert {"successor_link:P", "gap_before:2"} <= set(first["flags"].split(";"))
    assert result["summary"]["successor_link"]["sessions_between"] == 2


def test_step7_trim_mark_on_the_successors_first_yahoo_row_is_cleared_after_handed_over_rows(monkeypatch):
    # MRVL: the predecessor's rows run to 2021-04-26 under the ticker, the successor's own Yahoo file starts
    # 2021-04-27 with step 7's trim mark; without clearing it the 04-26 -> 04-27 return would be lost
    close = _walk(40)
    cut = str(SESSIONS[19].date())
    monkeypatch.setitem(rc.SUCCESSOR_LINKS, "P", {"successor": "S", "ticker": "T", "last_session": cut,
                                                  "continues": True, "basis": "test", "url": "u"})
    old_yahoo = _frame(close[:25]).assign(security_id="P", file="holdout/T.json")
    own = _frame(close[25:], start=25, rowflag=["yahoo_junction"] + [""] * 14).assign(security_id="S",
                                                                                        file="yahoo/S.csv.gz")
    bundles = {"wiki": {}, "tiingo_new": {}, "tiingo_old": {}, "yahoo_new": {"S": own}, "yahoo_old": {"P": old_yahoo},
               "stored": {}}
    frames, facts = rc.link_frames("S", rc.frames_for("S", bundles), bundles, "P", SESSIONS)
    assert facts["yahoo_junction_cleared"] == str(SESSIONS[25].date())
    ctx = {**_ctx(), "link_cut": cut, "link_continues": True, "link_predecessor": "P"}
    canonical = rc.reconcile_security("S", frames, ctx)["canonical"]
    assert canonical["date"].iloc[0] == str(SESSIONS[20].date())
    assert canonical["tr"].notna().all()  # every row from the first one on, 25 included
    day25 = canonical.loc[canonical["date"] == str(SESSIONS[25].date())].iloc[0]
    assert day25["tr"] == pytest.approx(close[25] / close[24] - 1) and "yahoo_junction" not in day25["flags"]


def test_a_shared_first_session_and_the_raw_chart_fill_start_the_successor_on_its_real_first_day(monkeypatch):
    # 21CF -> Fox: Fox trades from 2019-03-19, the day 21CF traded as TFCFA; step 7 cut Fox's file at 2019-05-06
    close = _walk(40)
    cut = str(SESSIONS[19].date())
    link = {"successor": "S", "ticker": "T", "last_session": cut, "continues": False, "basis": "test", "url": "u",
            "successor_first_session": cut, "raw_yahoo_fill": "T"}
    monkeypatch.setitem(rc.SUCCESSOR_LINKS, "P", link)
    own_tiingo = _frame(close[:20] * 1.3).assign(security_id="P", file="tiingo_run/P.csv")  # the predecessor's own
    stored = _frame(close[12:30], start=12).assign(security_id="P", file="t.csv")  # ticker-mapped: the successor's
    own_yahoo = _frame(close[25:], start=25, rowflag=["yahoo_junction"] + [""] * 14).assign(security_id="S",
                                                                                              file="yahoo/S.csv.gz")
    raw = _frame(close[10:30], start=10).assign(security_id="", file="raw_yahoo/T__x.json.gz")
    monkeypatch.setattr(rc, "raw_yahoo_rows", lambda ticker: raw if ticker == "T" else rc._empty_rows())
    bundles = {"wiki": {}, "tiingo_new": {"P": own_tiingo}, "tiingo_old": {}, "yahoo_new": {"S": own_yahoo},
               "yahoo_old": {}, "stored": {"P": stored}}
    # the predecessor keeps its own file's row on the shared day, not the ticker's (the successor's)
    pred, pred_facts = rc.predecessor_frames(rc.frames_for("P", bundles), link)
    assert pred["tiingo"]["date"].max() == SESSIONS[19] and pred["stored"]["date"].max() == SESSIONS[18]
    assert pred_facts["ticker_rows_on_shared_day_given_to_successor"] == 1
    frames, facts = rc.link_frames("S", rc.frames_for("S", bundles), bundles, "P", SESSIONS)
    assert facts["raw_yahoo_fill_rows"] == 6 and facts["first_session"] == cut  # rows 19..24 from the raw chart
    assert "tiingo" not in frames and frames["stored"]["date"].min() == SESSIONS[19]
    assert facts["yahoo_junction_cleared"] == str(SESSIONS[25].date())
    ctx = {**_ctx(), "link_cut": rc.previous_session_of(cut, SESSIONS), "link_continues": False,
           "link_predecessor": "P"}
    canonical = rc.reconcile_security("S", frames, ctx)["canonical"]
    first = canonical.iloc[0]
    assert first["date"] == cut and np.isnan(first["tr"])  # a new series: no return on its first row
    assert {"raw_yahoo_fill", "successor_of:P"} <= set(first["flags"].split(";"))
    assert canonical["tr"].iloc[1:].notna().all()  # 2019-05-06 included: the trim mark is cleared


def test_a_class_folded_into_another_class_hands_nothing_over(monkeypatch):
    monkeypatch.setattr(rc, "SUCCESSOR_LINKS", {"B": {"successor": "A", "ticker": "TB", "last_session": "2015-01-30",
                                                      "continues": False, "handover": False, "basis": "b", "url": "u"},
                                                "P": {"successor": "S", "ticker": "T", "last_session": "2015-01-30",
                                                      "continues": True, "basis": "b", "url": "u"}})
    assert rc.successor_of() == {"S": "P"}
    days = rc.pf.xnas_sessions("2015-01-02", "2015-02-27")
    as_int = lambda d: d.values.astype("datetime64[D]").astype(np.int32)
    states = {"B": {"summary": {"rows": 19, "last_date": "2015-01-30"}, "dates": as_int(days[:19])},
              "A": {"summary": {"rows": 38, "first_date": "2015-01-02", "last_date": "2015-02-27"},
                    "dates": as_int(days)},
              "P": {"summary": {"rows": 19, "last_date": "2015-01-30"}, "dates": as_int(days[:19])},
              "S": {"summary": {"rows": 19, "first_date": "2015-02-02", "last_date": "2015-02-27",
                                "successor_link": {"first_row": "2015-02-02", "first_row_return": "measured"},
                                "successor_link_rows": {"first_session": "2015-02-02"}}, "dates": as_int(days[19:])}}
    master = pd.DataFrame({"security_id": ["A", "S"], "last_listed": ["2026-08-01", "2026-08-01"]})
    links, facts = rc.successor_link_table(states, master)
    links = links.set_index("predecessor_id")
    assert links.loc["B", "status"] == "cut_class_kept" and links.loc["B", "overlap_days"] == ""
    assert links.loc["P", "status"] == "continued" and links.loc["P", "overlap_days"] == 0
    # S ends in 2015 while listed to 2026: a short series after the continuation, listed in the summary
    assert bool(links.loc["P", "short_after_continuation"]) and not bool(links.loc["B", "short_after_continuation"])
    assert [p["successor"] for p in facts["short_after_continuation"]["pairs"]] == ["S"]
    assert facts["overlap_days_total"] == 0


def test_load_targets_follows_a_chain_of_continuing_links(tmp_path, monkeypatch):
    candidates = tmp_path / "candidates.csv"
    pd.DataFrame([{"security_id": "P", "reason": "A1_x", "planned_source": "wiki", "status": "done"}]).to_csv(
        candidates, index=False)
    weekly = tmp_path / "weekly.pkl"
    pd.DataFrame({"security_id": ["P"], "week_end": [pd.Timestamp("2015-01-02")], "dv50_rank": [5], "dv20_rank": [5],
                  "dv50": [1.0], "dv20": [1.0]}).to_pickle(weekly)
    monkeypatch.setattr(rc, "CANDIDATES", candidates)
    monkeypatch.setattr(rc, "WEEKLY_METRICS", weekly)
    monkeypatch.setattr(rc, "SUCCESSOR_LINKS", {"P": {"successor": "S", "continues": True, "last_session": "2015-01-02"},
                                                "S": {"successor": "T", "continues": True, "last_session": "2016-01-04"},
                                                "T": {"successor": "U", "continues": False, "last_session": "2017-01-03"}})
    targets = rc.load_targets().set_index("security_id")
    assert list(targets.index) == ["P", "S", "T"]  # LINTA -> QVCA -> QRTEA: the whole chain; U is a cut, not added


def test_a_target_without_a_listing_interval_gets_no_series(tmp_path, monkeypatch):
    # Ford 37996: step 4 removed its only Nasdaq interval; its ticker's vendor rows are not a series
    monkeypatch.setattr(rc, "PRICES_DIR", tmp_path / "prices")
    monkeypatch.setattr(rc, "STATE_DIR", tmp_path / "state")
    (tmp_path / "prices").mkdir()
    (tmp_path / "state").mkdir()
    monkeypatch.setattr(rc, "MIN_FREE_MB", 0)
    monkeypatch.setattr(rc, "SUCCESSOR_LINKS", {})
    bundles = {"wiki": {"F": _frame(_walk(30)).assign(security_id="F", file="wiki/F.csv.gz")}, "tiingo_new": {},
               "tiingo_old": {}, "yahoo_new": {}, "yahoo_old": {}, "stored": {}}
    identity = {"mapping": pd.DataFrame(columns=["security_id", "ticker", "list_start", "list_end"]),
                "master": pd.DataFrame({"security_id": ["F"], "first_ticker": ["F"]}), "relisted": {}}
    states = rc.run_securities(["F"], bundles, identity, {}, SESSIONS)
    assert states["F"]["summary"]["rows"] == 0 and states["F"]["summary"]["no_listing_interval"]
    assert not (tmp_path / "prices" / "F.csv").exists()
    targets = pd.DataFrame([{"security_id": "F", "candidate_reasons": "Y_active_all", "planned_sources": "",
                             "candidate_status": "done", "weeks_rank300": 0, "best_dv50": np.nan}])
    monkeypatch.setattr(rc, "tiingo_waiting", lambda: pd.DataFrame(columns=["security_id"]))
    monkeypatch.setattr(rc, "UNFILLABLE", tmp_path / "none.csv")
    assert rc.no_series_table(states, targets)["reason"].tolist() == ["no_listing_interval"]


def test_a_continuing_predecessor_gets_back_the_successors_rows_of_its_tail_gap(monkeypatch):
    # Zillow: its own rows stop at 2014-11-20, Zillow Group's Yahoo file has the days to the cut (2015-02-17)
    close = _walk(40)
    cut = str(SESSIONS[19].date())
    link = {"successor": "S", "ticker": "T", "last_session": cut, "continues": True, "basis": "test", "url": "u"}
    monkeypatch.setitem(rc.SUCCESSOR_LINKS, "P", link)
    wiki = _frame(close[:15]).assign(security_id="P", file="wiki/T.csv.gz")
    yahoo = _frame(close).assign(security_id="S", file="yahoo/S.csv.gz")
    bundles = {"wiki": {"P": wiki}, "tiingo_new": {}, "tiingo_old": {}, "yahoo_new": {"S": yahoo}, "yahoo_old": {},
               "stored": {}}
    pred, facts = rc.predecessor_frames(rc.frames_for("P", bundles), link, rc.frames_for("S", bundles), "P")
    assert facts["successor_rows_given_back"] == 5  # rows 15..19 only: the earlier history keeps its own source
    assert pred["yahoo"]["date"].tolist() == list(SESSIONS[15:20]) and (pred["yahoo"]["security_id"] == "P").all()
    frames, link_facts = rc.link_frames("S", rc.frames_for("S", bundles), bundles, "P", SESSIONS)
    # the anchor: the successor's own row on the last session (WIKI's last row, 14, is older and not used)
    assert "yahoo" in link_facts["anchor_sources"] and frames["yahoo"]["date"].min() == SESSIONS[19]
    ctx = {**_ctx(), "link_cut": cut, "link_continues": True, "link_predecessor": "P"}
    first = rc.reconcile_security("S", frames, ctx)["canonical"].iloc[0]
    assert first["date"] == str(SESSIONS[20].date()) and first["tr"] == pytest.approx(close[20] / close[19] - 1)


def test_a_cut_successors_first_row_books_no_conversion_event(monkeypatch):
    # UNIT -> New Uniti: Yahoo books the 0.6029 merger ratio as a split on New Uniti's first row (2025-08-04);
    # the predecessor's terminal value holds it, the new series starts without it
    close = _walk(40)
    cut = str(SESSIONS[19].date())
    monkeypatch.setitem(rc.SUCCESSOR_LINKS, "P", {"successor": "S", "ticker": "T", "last_session": cut,
                                                  "continues": False, "basis": "test", "url": "u",
                                                  "handover_files": True})
    split = np.ones(40)
    split[20] = 0.602
    yahoo = _frame(close, split=split).assign(security_id="P", file="yahoo/P.csv.gz")
    bundles = {"wiki": {}, "tiingo_new": {}, "tiingo_old": {}, "yahoo_new": {"P": yahoo}, "yahoo_old": {}, "stored": {}}
    frames, facts = rc.link_frames("S", rc.frames_for("S", bundles), bundles, "P", SESSIONS)
    assert facts["predecessor_rows_added"] == 20  # handover_files: the predecessor's own file runs on in S
    ctx = {**_ctx(), "link_cut": cut, "link_continues": False, "link_predecessor": "P"}
    result = rc.reconcile_security("S", frames, ctx)
    first = result["canonical"].iloc[0]
    assert first["date"] == str(SESSIONS[20].date()) and np.isnan(first["tr"]) and first["split_factor"] == 1.0
    assert "distribution_factor" not in first["flags"] and not [e for e in result["events"] if e["ex_date"] == first["date"]]
    assert result["summary"]["link_first_row_event_dropped"]["split"] == pytest.approx(0.602)


# ------------------------------------------------------------------ the hand review's merged verdicts

def _write_review(tmp_path, monkeypatch, **tables):
    folder = tmp_path / "merged"
    folder.mkdir(exist_ok=True)
    for name, rows in tables.items():
        pd.DataFrame(rows).to_csv(folder / f"{name}.csv", index=False)
    monkeypatch.setattr(rc, "REVIEW_DIR", folder)
    monkeypatch.setattr(rc, "_REVIEW_CACHE", {})
    return folder


_MOVE_VERDICT = {"security_id": "1", "ticker": "T", "end_date": "", "source_url": "", "evidence_sources": "",
                 "correct_source": "", "event_type": "", "split_factor": "", "cash_per_share": "", "first_new_session": "",
                 "effective_date": "", "verified_at": "2026-10-03T00:00:00Z", "reviewer": "hand:test", "notes": "checked"}


def test_move_verdicts_fill_the_queue_and_an_unresolved_one_stays_open(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "OUT", tmp_path)
    monkeypatch.setattr(rc, "REVIEWED_FORMAT", tmp_path / "missing.csv")
    monkeypatch.setattr(rc, "relevant_spans", lambda dv_weeks: {"1": [("2015-01-01", "2015-12-31")]})
    _write_review(tmp_path, monkeypatch, moves_verdicts=[
        {**_MOVE_VERDICT, "event_date": "2015-02-02", "rule": "R1b", "classification": "market_move_no_adjustment",
         "source_url": "https://www.sec.gov/Archives/edgar/data/1/x.htm", "item_id": "moves-01-001"},
        {**_MOVE_VERDICT, "event_date": "2015-02-03", "rule": "R3", "classification": "vendor_error",
         "evidence_sources": "tiingo+yahoo", "correct_source": "tiingo", "item_id": "moves-01-002"},
        {**_MOVE_VERDICT, "event_date": "2015-02-04", "rule": "R4", "classification": "unresolved",
         "item_id": "moves-01-003"},
        {**_MOVE_VERDICT, "event_date": "2015-02-09", "rule": "R4", "classification": "halt",
         "source_url": "https://www.nasdaqtrader.com/x", "item_id": "moves-01-004"}])
    base = {"ticker": "T", "classification": "unreviewed", "source_url": "", "verified_at": "", "security_id": "1",
            "listed": True, "sources_agreeing": ""}
    moves = [{**base, "event_date": "2015-02-02", "rule": "R1b", "notes": "[R1b] move of +45% confirmed by no second source"},
             {**base, "event_date": "2015-02-03", "rule": "R3", "notes": "[R3] sources disagree with no majority"},
             {**base, "event_date": "2015-02-04", "rule": "R4", "notes": "[R4] zero volume on 1 session(s)"},
             {**base, "event_date": "2015-02-05", "rule": "R4", "notes": "[R4] zero volume on 1 session(s)"}]
    queue, facts = rc.build_move_queue({"1": {"moves": moves}}, {})
    by_day = queue.set_index("event_date")
    assert by_day.loc["2015-02-02", "classification"] == "market_move_no_adjustment"
    assert by_day.loc["2015-02-02", "source_url"].startswith("https://www.sec.gov/")
    assert by_day.loc["2015-02-03", "classification"] == "vendor_error"
    assert "evidence: tiingo+yahoo" in by_day.loc["2015-02-03", "notes"] and "use tiingo" in by_day.loc["2015-02-03", "notes"]
    assert by_day.loc["2015-02-04", "classification"] == "unreviewed"  # unresolved: still open
    assert "moves-01-003: unresolved, open" in by_day.loc["2015-02-04", "notes"]
    assert by_day.loc["2015-02-05", "classification"] == "unreviewed"  # no verdict
    review = facts["hand_review"]
    assert (review["applied"], review["unresolved"], review["verdicts_unmatched"]) == (2, 1, 1)
    assert review["unmatched_items"] == ["moves-01-004"]  # its queue row is gone: reported, not applied
    assert facts["unreviewed"] == 2


def test_event_verdicts_fill_sec_url_and_verified_at_and_an_unresolved_one_fills_nothing(tmp_path, monkeypatch):
    _write_review(tmp_path, monkeypatch, split_verdicts=[
        {"security_id": "1", "ticker": "T", "ex_date": "2015-02-02", "verdict": "confirmed", "split_factor": "",
         "ex_date_confirmed": "2015-02-02", "event_type": "reverse_split",
         "source_url": "https://www.sec.gov/Archives/a.htm", "evidence_sources": "", "verified_at": "2026-10-02T10:00:00Z",
         "reviewer": "hand", "item_id": "splits-01-001", "notes": "8-K"},
        {"security_id": "1", "ticker": "T", "ex_date": "2015-03-02", "verdict": "confirmed", "split_factor": "",
         "ex_date_confirmed": "", "event_type": "split", "source_url": "", "evidence_sources": "yahoo+stored",
         "verified_at": "2026-10-03T00:00:00Z", "reviewer": "mechanical", "item_id": "splits-mech-001", "notes": ""}],
        distribution_verdicts=[
        {"security_id": "2", "ticker": "U", "ex_date": "2015-02-10", "verdict": "corrected", "distribution_type": "spinoff",
         "ratio": "0.25", "cash_per_share": "", "distributed_security": "NEWCO", "ex_date_confirmed": "",
         "source_url": "https://www.sec.gov/Archives/b.htm", "verified_at": "2026-10-02T11:00:00Z", "reviewer": "hand",
         "item_id": "distributions-01-001", "notes": "Form 10"},
        {"security_id": "2", "ticker": "U", "ex_date": "2015-03-10", "verdict": "unresolved", "distribution_type": "",
         "ratio": "", "cash_per_share": "", "distributed_security": "", "ex_date_confirmed": "", "source_url": "",
         "verified_at": "2026-10-02T11:00:00Z", "reviewer": "hand", "item_id": "distributions-01-002",
         "notes": "no record date"}])
    split = pd.DataFrame({"security_id": ["1", "1", "2", "2"], "ex_date": ["2015-02-02", "2015-03-02", "2015-02-10", "2015-03-10"],
                          "sec_url": "", "verified_at": "", "notes": "n"})
    facts = rc.apply_event_verdicts(split, "split_events")
    assert split["sec_url"].tolist() == ["https://www.sec.gov/Archives/a.htm", "evidence: yahoo+stored",
                                         "https://www.sec.gov/Archives/b.htm", ""]
    assert split["verified_at"].tolist()[:3] == ["2026-10-02T10:00:00Z", "2026-10-03T00:00:00Z", "2026-10-02T11:00:00Z"]
    assert split["verified_at"].iloc[3] == ""
    assert "hand review distributions-01-001: corrected (spinoff, ratio 0.25)" in split["notes"].iloc[2]
    assert "distributions-01-002: unresolved" in split["notes"].iloc[3]
    assert (facts["filled"], facts["unresolved"]) == (3, 1)
    special = pd.DataFrame({"security_id": ["2"], "ex_date": ["2015-02-10"], "sec_url": [""], "notes": [""]})
    rc.apply_event_verdicts(special, "special_distributions")
    assert special["sec_url"].iloc[0] == "https://www.sec.gov/Archives/b.htm"


def test_a_confirmed_price_adjustment_keeps_its_url_over_a_verdict(tmp_path, monkeypatch):
    _write_review(tmp_path, monkeypatch, split_verdicts=[
        {"security_id": "1", "ticker": "T", "ex_date": "2015-02-02", "verdict": "confirmed", "split_factor": "",
         "ex_date_confirmed": "", "event_type": "split", "source_url": "https://www.sec.gov/Archives/new.htm",
         "evidence_sources": "", "verified_at": "2026-10-02T10:00:00Z", "reviewer": "hand", "item_id": "s-1", "notes": ""}])
    split = pd.DataFrame({"security_id": ["1"], "ex_date": ["2015-02-02"], "sec_url": ["https://old"],
                          "verified_at": ["2026-01-01"], "notes": [""]})
    rc.apply_event_verdicts(split, "split_events")
    assert (split["sec_url"].iloc[0], split["verified_at"].iloc[0]) == ("https://old", "2026-01-01")


def test_a_reviewed_source_choice_resolves_a_two_vendor_disagreement_and_the_queue_keeps_its_row():
    n = 30
    close = _walk(n)
    bad = close.copy()
    bad[10] = bad[10] * 1.03  # wiki wrong on one day; only two vendors, so the vote leaves it unresolved
    day = str(SESSIONS[10].date())
    ctx = {**_ctx(), "review": {"source": [{"start": day, "end": day, "source": "yahoo", "item_id": "moves-x"}],
                                "flag": [{"start": day, "end": day, "source": "yahoo", "item_id": "moves-x"}]}}
    result = rc.reconcile_security("X", {"wiki": _frame(bad), "yahoo": _frame(close)}, ctx)
    canonical = result["canonical"].set_index("date")
    assert canonical.loc[day, "src_primary"] == "yahoo"
    flags = canonical.loc[day, "flags"].split(";")
    assert "disagree_unresolved" not in flags
    assert {"review_source:yahoo", "disagree_reviewed", "disagree_resolved:wiki"} <= set(flags)
    assert canonical.loc[day, "tr"] == pytest.approx(close[10] / close[9] - 1)
    # the next day is wiki's own return again (each source's own rows: nothing chained across the choice)
    nxt = str(SESSIONS[11].date())
    assert canonical.loc[nxt, "src_primary"] == "wiki" and "disagree_unresolved" in canonical.loc[nxt, "flags"]
    assert [m for m in result["moves"] if m["rule"] == "R3" and m["event_date"] == day]  # the verdict's row stays
    assert result["summary"]["review_sources"]["unresolved_resolved"] == 1


def test_a_reviewed_vendor_error_without_another_vendor_only_flags_the_day():
    n = 30
    close = _walk(n)
    day = str(SESSIONS[12].date())
    ctx = {**_ctx(), "review": {"source": [], "flag": [{"start": day, "end": day, "source": "", "item_id": "m"}]}}
    result = rc.reconcile_security("X", {"yahoo": _frame(close)}, ctx)
    assert "review_vendor_error" in _flags(result["canonical"], day)


def test_a_reviewed_source_choice_on_a_level_run_keeps_the_vote_r7_entry():
    n = 30
    close = _walk(n)
    other = close * 1.05  # a lasting 5% level offset in tiingo against wiki (a wrong class in one vendor)
    days = [str(d.date()) for d in SESSIONS[5:15]]
    ctx = {**_ctx(), "review": {"source": [{"start": days[0], "end": days[-1], "source": "tiingo", "item_id": "m"}],
                                "flag": []}}
    result = rc.reconcile_security("X", {"wiki": _frame(close), "tiingo": _frame(other)}, ctx)
    canonical = result["canonical"].set_index("date")
    assert (canonical.loc[days, "src_primary"] == "tiingo").all()
    r7 = [m for m in result["moves"] if m["rule"] == "R7"]
    assert r7 and "tiingo raw close 1.0500x of wiki" in r7[0]["notes"]  # measured against the vote's primary (2015: wiki)


def test_a_documented_halt_is_kept_not_cut_as_filler_and_its_r4_entry_is_settled(monkeypatch):
    n = 40
    close = _walk(n)
    volume = np.full(n, 1000.0)
    close[30:] = close[29]
    volume[30:] = 0.0
    start = str(SESSIONS[30].date())
    monkeypatch.setitem(rc.HALTED_SPANS, "H", {"start": start, "documented_to": str(SESSIONS[n - 1].date()),
                                              "classification": "flat_genuine",
                                              "evidence": "yahoo+stored", "url": "https://www.sec.gov/Archives/h.htm",
                                              "note": "halted"})
    result = rc.reconcile_security("H", {"yahoo": _frame(close, volume=volume)}, _ctx())
    assert result["summary"]["filler_cut"] == 0
    assert result["canonical"]["date"].iloc[-1] == str(SESSIONS[n - 1].date())
    assert "halt" in _flags(result["canonical"], start)
    r4 = [m for m in result["moves"] if m["rule"] == "R4"]
    assert r4 and all(m["classification"] == "flat_genuine" and m["source_url"] for m in r4)
    plain = rc.reconcile_security("X", {"yahoo": _frame(close, volume=volume)}, _ctx())
    assert plain["summary"]["filler_cut"] == 10


def test_halt_rows_after_the_last_documented_day_stay_flagged_and_their_r4_entry_open(monkeypatch):
    n = 40
    close = _walk(n)
    volume = np.full(n, 1000.0)
    close[30:] = close[29]
    volume[30:] = 0.0
    start, documented = str(SESSIONS[30].date()), str(SESSIONS[33].date())
    monkeypatch.setitem(rc.HALTED_SPANS, "H", {"start": start, "documented_to": documented,
                                              "classification": "flat_genuine", "evidence": "yahoo+stored",
                                              "url": "https://www.sec.gov/Archives/h.htm", "note": "suspended"})
    result = rc.reconcile_security("H", {"yahoo": _frame(close, volume=volume)}, _ctx())
    assert result["summary"]["filler_cut"] == 0  # every halted row is kept
    assert "halt_end_undocumented" not in _flags(result["canonical"], documented)
    assert "halt_end_undocumented" in _flags(result["canonical"], str(SESSIONS[34].date()))
    r4 = sorted((m["event_date"], m["classification"]) for m in result["moves"] if m["rule"] == "R4")
    # the flat run (from the last traded close, session 29) is split at the documented day: settled, then open
    assert r4 == [(str(SESSIONS[29].date()), "flat_genuine"), (str(SESSIONS[34].date()), "unreviewed")]
    assert any("no document shows the halt after" in m["notes"] for m in result["moves"] if m["rule"] == "R4")


def test_the_halted_spans_name_the_last_documented_day():
    for entry in rc.HALTED_SPANS.values():
        assert entry["start"] <= entry["documented_to"]


def test_review_overrides_read_the_merged_moves_verdicts(tmp_path, monkeypatch):
    _write_review(tmp_path, monkeypatch, moves_verdicts=[
        {**_MOVE_VERDICT, "security_id": "9", "event_date": "2015-02-02", "end_date": "2015-02-06", "rule": "R7",
         "classification": "vendor_error", "evidence_sources": "tiingo+yahoo", "correct_source": "tiingo", "item_id": "a"},
        {**_MOVE_VERDICT, "security_id": "9", "event_date": "2015-03-02", "rule": "R4", "classification": "vendor_error",
         "evidence_sources": "yahoo+stored", "correct_source": "", "item_id": "b"},
        {**_MOVE_VERDICT, "security_id": "8", "event_date": "2015-03-02", "rule": "R1", "classification": "unresolved",
         "item_id": "c"}])
    over = rc.review_overrides()
    assert set(over) == {"9"}
    assert over["9"]["source"] == [{"start": "2015-02-02", "end": "2015-02-06", "source": "tiingo", "item_id": "a"}]
    assert [f["item_id"] for f in over["9"]["flag"]] == ["a", "b"]
    monkeypatch.setattr(rc, "APPLY_REVIEW_SOURCE_OVERRIDES", False)
    assert rc.review_overrides()["9"]["source"] == []


def test_a_continuing_predecessor_keeps_its_tiingo_rows_to_the_successor_window_end(tmp_path, monkeypatch):
    path = tmp_path / "ASRT.csv"
    days = pd.bdate_range("2020-05-01", "2020-08-31")
    pd.DataFrame({"date": days.strftime("%Y-%m-%d"), "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                  "volume": 100, "adjClose": 1.0, "adjOpen": 1.0, "adjHigh": 1.0, "adjLow": 1.0, "adjVolume": 100,
                  "divCash": 0.0, "splitFactor": 1.0}).to_csv(path, index=False)
    status = tmp_path / "fetch_status.csv"
    pd.DataFrame([{"security_id": "P", "status": "done", "prices_path": str(path)}]).to_csv(status, index=False)
    monkeypatch.setattr(rc, "TIINGO_STATUS", status)
    link = {"successor": "S", "ticker": "ASRT", "last_session": "2020-05-19", "continues": True}
    monkeypatch.setitem(rc.SUCCESSOR_LINKS, "P", {**link, "tiingo_to_successor_end": "gap review"})
    windows = {"P": (pd.Timestamp("2020-01-01"), pd.Timestamp("2020-06-18")),
               "S": (pd.Timestamp("2020-03-01"), pd.Timestamp("2020-08-14"))}
    identity = {"mapping": pd.DataFrame(columns=["security_id", "ticker", "list_start", "list_end"])}
    rows, facts = rc.load_new_tiingo_rows(identity, {"P"}, windows)
    assert rows["date"].max() == pd.Timestamp("2020-08-14")
    assert "P" in facts["predecessor_windows_extended"]
    # a continuing link without the mark keeps the predecessor's own window (merge review: LBTYB, SBGI, VNOM, QVCB)
    monkeypatch.setitem(rc.SUCCESSOR_LINKS, "P", link)
    rows, facts = rc.load_new_tiingo_rows(identity, {"P"}, windows)
    assert rows["date"].max() == pd.Timestamp("2020-06-18") and facts["predecessor_windows_extended"] == []


def test_only_the_gap_reviews_two_links_hand_the_predecessors_tiingo_answer_on():
    marked = sorted(p for p, link in rc.SUCCESSOR_LINKS.items() if link.get("tiingo_to_successor_end"))
    assert marked == ["1005201", "1355096.T-QRTEA"]
    assert all(rc.SUCCESSOR_LINKS[p]["continues"] for p in marked)


# ------------------------------------------------------------------ verdicts that change S or D (round-10 merge review)

_SEC = "https://www.sec.gov/Archives/edgar/data/1/x.htm"
_SPLIT_VERDICT = {"security_id": "1", "ticker": "T", "split_factor": "", "ex_date_confirmed": "", "event_type": "",
                  "source_url": _SEC, "evidence_sources": "", "verified_at": "2026-10-03T00:00:00Z", "reviewer": "hand",
                  "notes": "8-K"}
_DIST_VERDICT = {"security_id": "1", "ticker": "T", "distribution_type": "", "ratio": "", "cash_per_share": "",
                 "distributed_security": "", "ex_date_confirmed": "", "source_url": _SEC,
                 "verified_at": "2026-10-03T00:00:00Z", "reviewer": "hand", "notes": "8-K"}


def _plan(tmp_path, monkeypatch, **tables):
    _write_review(tmp_path, monkeypatch, **tables)
    monkeypatch.setattr(rc, "_EVENT_PLAN", {})
    monkeypatch.setattr(rc, "REVIEW_EVENT_EXCEPTIONS", {})
    return rc.review_event_plan().set_index("item_id")


def _day(k):
    return str(SESSIONS[k].date())


def _events_ctx(plan):
    items = [{"date": r["date"], "split": r["split"], "cash": r["cash"], "item_id": i, "source": r["source"],
              "mode": r["mode"], "scale": r["scale"]}
             for i, r in plan[plan["action"] == "apply"].iterrows()]
    return {**_ctx(), "review": {"source": [], "flag": [], "events": items}}


def test_a_corrected_split_sets_s_on_the_confirmed_day_and_clears_the_vendors_day(tmp_path, monkeypatch):
    close = _walk(30)
    close[12:] = close[12:] * 10.0  # a real 1-for-10 on session 12; the vendor books 1-for-30 on session 14
    split = np.ones(30)
    split[14] = 1 / 30
    plan = _plan(tmp_path, monkeypatch, split_verdicts=[
        {**_SPLIT_VERDICT, "ex_date": _day(14), "verdict": "corrected", "split_factor": "0.1",
         "ex_date_confirmed": _day(12), "item_id": "splits-01-001"}])
    assert plan["action"].tolist() == ["apply", "apply"]
    result = rc.reconcile_security("1", {"yahoo": _frame(close, split=split)}, _events_ctx(plan))
    c = result["canonical"].set_index("date")
    assert c.loc[_day(12), "split_factor"] == pytest.approx(0.1)
    assert c.loc[_day(12), "tr"] == pytest.approx(close[12] * 0.1 / close[11] - 1)
    assert c.loc[_day(14), "tr"] == pytest.approx(close[14] / close[13] - 1)
    assert c.loc[_day(14), "split_factor"] == 1.0
    assert "review_event:splits-01-001" in c.loc[_day(14), "flags"]
    assert [e["item_id"] for e in result["summary"]["review_events"]["applied"]] == ["splits-01-001", "splits-01-001"]


def test_a_not_a_split_verdict_removes_the_vendors_factor(tmp_path, monkeypatch):
    close = _walk(30)
    split = np.ones(30)
    split[10] = 1 / 6  # the abandoned 1-for-6 (WHLR): the closes show no jump
    plan = _plan(tmp_path, monkeypatch, split_verdicts=[
        {**_SPLIT_VERDICT, "ex_date": _day(10), "verdict": "not_a_split", "split_factor": "1", "item_id": "splits-01-002"}])
    result = rc.reconcile_security("1", {"yahoo": _frame(close, split=split)}, _events_ctx(plan))
    row = result["canonical"].set_index("date").loc[_day(10)]
    assert row["split_factor"] == 1.0 and row["tr"] == pytest.approx(close[10] / close[9] - 1)
    assert "reverse_split" not in row["flags"]


def test_corrected_cash_and_a_removed_duplicate_booking(tmp_path, monkeypatch):
    close = _walk(30)
    close[20:] = close[20:] * 0.7
    div = np.zeros(30)
    div[15] = 0.3 * close[14]  # a misdated copy (no drop that day: LENZ 2024-03-15)
    div[20] = 2.0 * 0.3 * close[19]  # the real one, booked in the wrong units (LENZ 7.21 for 1.03)
    plan = _plan(tmp_path, monkeypatch, distribution_verdicts=[
        {**_DIST_VERDICT, "ex_date": _day(15), "verdict": "not_a_distribution", "item_id": "distributions-01-001"},
        {**_DIST_VERDICT, "ex_date": _day(20), "verdict": "corrected", "distribution_type": "special_cash",
         "cash_per_share": f"{0.3 * close[19]:.6f}", "item_id": "distributions-01-002"}])
    result = rc.reconcile_security("1", {"yahoo": _frame(close, div=div)}, _events_ctx(plan))
    c = result["canonical"].set_index("date")
    assert c.loc[_day(15), "div_cash"] == 0.0 and c.loc[_day(15), "tr"] == pytest.approx(close[15] / close[14] - 1)
    assert abs(c.loc[_day(20), "tr"] - (close[20] / close[19] - 0.7)) < 1e-6
    assert [d["ex_date"] for d in result["dividends"]] == [_day(20)]


def test_a_stock_dividend_with_cash_sets_both(tmp_path, monkeypatch):
    close = _walk(30)
    close[10:] = close[10:] / 1.05
    div = np.zeros(30)
    div[10] = 0.2
    plan = _plan(tmp_path, monkeypatch, distribution_verdicts=[
        {**_DIST_VERDICT, "ex_date": _day(10), "verdict": "corrected", "distribution_type": "stock_dividend",
         "ratio": "1.05", "cash_per_share": "0.5", "item_id": "distributions-01-003"}])
    assert (plan.loc["distributions-01-003", "split"], plan.loc["distributions-01-003", "cash"]) == (1.05, 0.5)
    result = rc.reconcile_security("1", {"yahoo": _frame(close, div=div)}, _events_ctx(plan))
    row = result["canonical"].set_index("date").loc[_day(10)]
    assert (row["split_factor"], row["div_cash"]) == (1.05, 0.5)
    assert row["tr"] == pytest.approx((close[10] * 1.05 + 0.5) / close[9] - 1)


def test_a_reverse_split_booked_as_a_distribution_gets_the_documents_exact_ratio(tmp_path, monkeypatch):
    close = _walk(30)
    close[10:] = close[10:] * 7.7
    split = np.ones(30)
    split[10] = 0.129
    plan = _plan(tmp_path, monkeypatch, distribution_verdicts=[
        {**_DIST_VERDICT, "ex_date": _day(10), "verdict": "not_a_distribution", "ratio": "0.1298701299",
         "item_id": "distributions-01-004"}])
    result = rc.reconcile_security("1", {"yahoo": _frame(close, split=split)}, _events_ctx(plan))
    row = result["canonical"].set_index("date").loc[_day(10)]
    assert row["split_factor"] == pytest.approx(1 / 7.7)
    assert row["tr"] == pytest.approx(close[10] / 7.7 / close[9] - 1, abs=1e-8)


def test_an_unrecorded_reverse_split_is_applied_and_a_spinoff_share_count_is_not(tmp_path, monkeypatch):
    plan = _plan(tmp_path, monkeypatch, moves_verdicts=[
        {**_MOVE_VERDICT, "event_date": _day(10), "rule": "R1/R2", "classification": "unrecorded_event",
         "event_type": "reverse_split", "split_factor": "0.04", "source_url": _SEC, "item_id": "moves-01-001"},
        {**_MOVE_VERDICT, "event_date": _day(12), "rule": "R1c", "classification": "unrecorded_event",
         "event_type": "spinoff", "split_factor": "0.125", "source_url": _SEC, "item_id": "moves-01-002"},
        {**_MOVE_VERDICT, "event_date": _day(14), "rule": "R1", "classification": "unrecorded_event",
         "event_type": "reverse_split", "split_factor": "0.5", "source_url": "https://ir.example.com/x",
         "item_id": "moves-01-003"}])
    assert plan.loc["moves-01-001", "action"] == "apply" and plan.loc["moves-01-001", "split"] == 0.04
    assert plan.loc["moves-01-002", "action"] == "not_applied" and "need a value" in plan.loc["moves-01-002", "reason"]
    assert plan.loc["moves-01-003", "action"] == "not_applied"  # no primary document
    close = _walk(30)
    close[10:] = close[10:] * 25
    result = rc.reconcile_security("1", {"yahoo": _frame(close)}, _events_ctx(plan))
    row = result["canonical"].set_index("date").loc[_day(10)]
    assert row["split_factor"] == 0.04 and abs(row["tr"]) < 0.05
    status = rc.review_event_status({"1": result})
    assert status["moves-01-001"]["status"] == "applied" and status["moves-01-002"]["status"] == "not_applied"
    base = {"ticker": "T", "classification": "unreviewed", "source_url": "", "verified_at": "", "security_id": "1",
            "listed": True, "sources_agreeing": "", "notes": "[R1] x"}
    frame = pd.DataFrame([{**base, "event_date": _day(10), "rule": "R1/R2"},
                          {**base, "event_date": _day(12), "rule": "R1c"}])
    facts = rc.apply_move_verdicts(frame, status)
    assert frame["classification"].tolist() == ["unrecorded_event", "unreviewed"]  # not applied: the row stays open
    assert "not applied to the series" in frame["notes"].iloc[1] and "applied to the series" in frame["notes"].iloc[0]
    assert (facts["applied"], facts["open_not_applied"], facts["series_changed"]) == (1, 1, 1)


def test_a_verdict_not_applied_keeps_its_split_and_distribution_rows_open(tmp_path, monkeypatch):
    plan = _plan(tmp_path, monkeypatch, split_verdicts=[
        {**_SPLIT_VERDICT, "ex_date": _day(10), "verdict": "confirmed", "item_id": "splits-01-001"},
        {**_SPLIT_VERDICT, "ex_date": _day(20), "verdict": "not_a_split", "item_id": "splits-01-002"},
        {**_SPLIT_VERDICT, "ex_date": _day(25), "verdict": "reclassify_distribution", "item_id": "splits-01-003"}],
        distribution_verdicts=[
        {**_DIST_VERDICT, "ex_date": _day(10), "verdict": "corrected", "distribution_type": "spinoff",
         "item_id": "distributions-01-001"}])
    assert plan.loc["distributions-01-001", "action"] == "not_applied"  # a spin-off without a factor: no value
    status = {"distributions-01-001": {"status": "not_applied", "reason": "no value", "change": ""},
              "splits-01-002": {"status": "unused", "reason": "planned, but the day has no kept row", "change": ""},
              "splits-01-003": {"status": "type_only", "reason": "", "change": ""}}
    split = pd.DataFrame({"security_id": "1", "ex_date": [_day(10), _day(20), _day(25)], "sec_url": "",
                          "verified_at": "", "notes": "", "event_type": "split"})
    facts = rc.apply_event_verdicts(split, "split_events", status)
    # the confirmed split verdict does not close a row another verdict leaves open; an unused plan stays open too
    assert split["sec_url"].tolist() == ["", "", _SEC] and split["verified_at"].tolist()[:2] == ["", ""]
    assert split["event_type"].tolist() == ["split", "split", "distribution"]
    assert "not applied to the series: no value; open" in split["notes"].iloc[0]
    assert (facts["open_not_applied"], facts["rows_open_by_verdict"], facts["type_only"]) == (2, 2, 1)
    special = pd.DataFrame({"security_id": ["1"], "ex_date": [_day(10)], "sec_url": [""], "notes": [""]})
    rc.apply_event_verdicts(special, "special_distributions", status)
    assert special["sec_url"].iloc[0] == ""


def test_an_exception_restates_the_action_and_a_source_guard_leaves_another_sources_row_unused(tmp_path, monkeypatch):
    _write_review(tmp_path, monkeypatch, distribution_verdicts=[
        {**_DIST_VERDICT, "ex_date": _day(10), "verdict": "corrected", "distribution_type": "stock_dividend",
         "ratio": "1.05", "cash_per_share": "1.73", "item_id": "distributions-06-019"}],
        moves_verdicts=[
        {**_MOVE_VERDICT, "event_date": _day(10), "rule": "R3", "classification": "unrecorded_event",
         "event_type": "stock_dividend", "split_factor": "3", "source_url": _SEC, "item_id": "moves-11-010"}])
    monkeypatch.setattr(rc, "_EVENT_PLAN", {})
    monkeypatch.setattr(rc, "REVIEW_EVENT_EXCEPTIONS", {
        "distributions-06-019": {"split": 1.05, "cash": 1.73 / 1.05, "source": "yahoo", "why": "units"},
        "moves-11-010": {"apply": False, "same_as": "distributions-06-019", "why": "booked by the other"}})
    plan = rc.review_event_plan().set_index("item_id")
    assert plan.loc["distributions-06-019", "cash"] == pytest.approx(1.73 / 1.05)
    close = _walk(30)
    result = rc.reconcile_security("1", {"wiki": _frame(close)}, _events_ctx(plan))  # a WIKI row, not Yahoo's
    assert result["summary"]["review_events"]["unused"] == ["distributions-06-019"]
    status = rc.review_event_status({"1": result})
    assert status["distributions-06-019"]["status"] == "unused" and status["moves-11-010"]["status"] == "not_applied"
    result = rc.reconcile_security("1", {"yahoo": _frame(close)}, _events_ctx(plan))
    status = rc.review_event_status({"1": result})
    assert status["distributions-06-019"]["status"] == "applied"
    assert status["moves-11-010"]["status"] == "applied" and "with distributions-06-019" in status["moves-11-010"]["change"]


def test_the_round10_exceptions_name_merged_items_and_carry_a_reason():
    for item, entry in rc.REVIEW_EVENT_EXCEPTIONS.items():
        assert re.match(r"^(moves|splits|distributions)-\d\d-\d{3}$", item)
        assert entry["why"] and (entry.get("apply") is False or "split" in entry or "cash" in entry)


@pytest.mark.parametrize("given, exact", [(0.0074074, 1 / 135), (0.003333, 1 / 300), (0.001333, 1 / 750),
                                          (0.181818, 1 / 5.5), (0.056022, 1 / 17.85), (0.0066667, 1 / 150),
                                          (0.1298701299, 1 / 7.7), (1.9365, 1.9365), (2.4484, 2.4484), (0.63, 0.63)])
def test_a_rounded_reverse_ratio_is_read_as_the_exact_one(given, exact):
    assert rc.snap_reverse_ratio(given) == pytest.approx(exact, rel=1e-12)


def test_a_confirmed_ratio_only_verdict_drops_the_double_counted_cash_and_keeps_a_single_booking(tmp_path, monkeypatch):
    # LBRDA 2025-07-15: Tiingo books the spin-off factor and its cash value together (+2.7%); the verdict confirms
    # the ratio, no cash, "apply one, not both". LBTYA: the canonical row carries only the cash, and is kept
    note = "Tiingo carries both factor 1.067 and cash: together +2.7%, either alone about -3.5%; apply one, not both."
    plan = _plan(tmp_path, monkeypatch, distribution_verdicts=[
        {**_DIST_VERDICT, "ex_date": _day(10), "verdict": "confirmed", "distribution_type": "spinoff",
         "ratio": "1.067", "notes": note, "item_id": "distributions-04-036"},
        {**_DIST_VERDICT, "security_id": "2", "ex_date": _day(10), "verdict": "confirmed",
         "distribution_type": "spinoff", "ratio": "1.058", "notes": "Tiingo carries both (+4.0%), a double count.",
         "item_id": "distributions-04-018"},
        {**_DIST_VERDICT, "security_id": "3", "ex_date": _day(10), "verdict": "confirmed",
         "distribution_type": "stock_dividend", "ratio": "1.05", "notes": "5% stock dividend",
         "item_id": "distributions-06-021"}])
    assert plan.loc["distributions-04-036", "action"] == "apply"
    assert plan.loc["distributions-04-036", "mode"] == rc.DROP_DOUBLE_CASH
    assert "distributions-06-021" not in plan.index  # a confirmed verdict without the double-count note changes nothing
    close = _walk(30)
    close[10:] = close[10:] / 1.067 * 0.97  # the spin-off's value and a 3% fall
    split, div = np.ones(30), np.zeros(30)
    split[10], div[10] = 1.067, 0.063 * close[9]
    result = rc.reconcile_security("1", {"tiingo": _frame(close, split=split, div=div)}, _events_ctx(plan.iloc[:1]))
    row = result["canonical"].set_index("date").loc[_day(10)]
    assert (row["split_factor"], row["div_cash"]) == (1.067, 0.0)
    assert row["tr"] == pytest.approx(close[10] * 1.067 / close[9] - 1)
    applied = result["summary"]["review_events"]["applied"]
    assert applied[0]["cash"].endswith(">0") and "review_event:distributions-04-036" in row["flags"]
    # one booking (cash only, LBTYA) is left as it is, and the item counts as applied (the series carries one)
    cash_only = np.zeros(30)
    cash_only[10] = 0.055 * close[9]
    plan2 = plan.loc[["distributions-04-018"]]
    other = rc.reconcile_security("2", {"wiki": _frame(close, div=cash_only)}, _events_ctx(plan2))
    row2 = other["canonical"].set_index("date").loc[_day(10)]
    assert (row2["split_factor"], row2["div_cash"]) == (1.0, pytest.approx(0.055 * close[9]))
    status = rc.review_event_status({"1": result, "2": other})
    assert status["distributions-04-036"]["status"] == "applied"
    assert status["distributions-04-018"]["status"] == "applied"
    table = pd.DataFrame({"security_id": ["1"], "ex_date": [_day(10)], "sec_url": [""], "notes": [""]})
    rc.apply_event_verdicts(table, "special_distributions", status)
    assert table["sec_url"].iloc[0] == _SEC and "applied to the series" in table["notes"].iloc[0]
    # the day has no kept row: the item is unused and the row stays open
    status = rc.review_event_status({"2": other})
    assert status["distributions-04-036"]["status"] == "unused"
    table = pd.DataFrame({"security_id": ["1"], "ex_date": [_day(10)], "sec_url": [""], "notes": [""]})
    rc.apply_event_verdicts(table, "special_distributions", status)
    assert table["sec_url"].iloc[0] == ""


def test_a_yahoo_row_in_adjusted_units_is_restated_as_traded(tmp_path, monkeypatch):
    # CBSH 2012-11-28: the Yahoo row's close and cash are the as-traded values / 1.05; after tr they are restated
    _write_review(tmp_path, monkeypatch, distribution_verdicts=[
        {**_DIST_VERDICT, "ex_date": _day(10), "verdict": "corrected", "distribution_type": "stock_dividend",
         "ratio": "1.05", "cash_per_share": "1.73", "item_id": "distributions-06-019"}])
    monkeypatch.setattr(rc, "_EVENT_PLAN", {})
    monkeypatch.setattr(rc, "REVIEW_EVENT_EXCEPTIONS", {"distributions-06-019": {
        "split": 1.05, "cash": 1.73 / 1.05, "source": "yahoo", "scale": 1.05, "why": "units"}})
    plan = rc.review_event_plan().set_index("item_id")
    close = _walk(30) / 1.05  # Yahoo's levels: as traded / 1.05
    close[10:] = close[10:] / 1.05 * 0.99
    result = rc.reconcile_security("1", {"yahoo": _frame(close)}, _events_ctx(plan))
    row = result["canonical"].set_index("date").loc[_day(10)]
    assert row["close_raw"] == pytest.approx(close[10] * 1.05)
    assert (row["split_factor"], row["div_cash"]) == (1.05, pytest.approx(1.73))
    assert row["tr"] == pytest.approx((close[10] * 1.05 + 1.73 / 1.05) / close[9] - 1)


def test_a_day_the_review_calls_doubtful_is_flagged_and_its_queue_rows_stay_open(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "DOUBTFUL_PRICE_DAYS", {("1", _day(10)): "only one source", ("1", _day(11)): "after it"})
    plan = _plan(tmp_path, monkeypatch, moves_verdicts=[
        {**_MOVE_VERDICT, "event_date": _day(10), "rule": "R1/R2", "classification": "unrecorded_event",
         "event_type": "reverse_split", "split_factor": "0.04", "source_url": _SEC, "item_id": "moves-01-004"},
        {**_MOVE_VERDICT, "event_date": _day(11), "rule": "R1", "classification": "market_move_second_source",
         "evidence_sources": "yahoo+stored", "item_id": "moves-05-006"}])
    close = _walk(30)
    close[10:] = close[10:] * 70
    result = rc.reconcile_security("1", {"yahoo": _frame(close)}, _events_ctx(plan))
    c = result["canonical"].set_index("date")
    assert c.loc[_day(10), "split_factor"] == 0.04  # the documented factor is applied
    assert "doubtful_price" in c.loc[_day(10), "flags"] and "doubtful_price" in c.loc[_day(11), "flags"]
    status = rc.review_event_status({"1": result})
    base = {"ticker": "T", "classification": "unreviewed", "source_url": "", "verified_at": "", "security_id": "1",
            "listed": True, "sources_agreeing": "", "notes": "[R1] x"}
    frame = pd.DataFrame([{**base, "event_date": _day(10), "rule": "R1/R2"},
                          {**base, "event_date": _day(11), "rule": "R1"}])
    facts = rc.apply_move_verdicts(frame, status)
    assert frame["classification"].tolist() == ["unreviewed", "unreviewed"]
    assert frame["verified_at"].tolist() == ["", ""]
    assert "doubtful_price" in frame["notes"].iloc[0] and "applied to the series" in frame["notes"].iloc[0]
    assert facts["open_doubtful_price"] == 2 and facts["applied"] == 0


def test_the_data_change_lists_take_applied_from_the_build(tmp_path, monkeypatch):
    change = {"security_id": "1", "ticker": "T", "date": "", "change": "", "applied": "N",
              "how": "owner: no per-event S/D override in the build", "source_url": _SEC, "evidence_sources": ""}
    _write_review(tmp_path, monkeypatch, split_data_changes=[
        {**change, "queue": "splits", "item_id": "splits-01-001", "verdict": "not_a_split"},
        {**change, "queue": "distributions", "item_id": "distributions-01-002", "verdict": "corrected"}],
        moves_data_changes=[
        {**change, "queue": "moves", "item_id": "moves-01-009", "verdict": "vendor_error", "applied": "Y",
         "how": "reconcile source_overrides"}])
    events = pd.DataFrame([
        {"item_id": "splits-01-001", "queue": "splits", "status": "applied", "change": "S 0.2>1", "reason": ""},
        {"item_id": "distributions-01-002", "queue": "distributions", "status": "not_applied", "change": "",
         "reason": "no value"},
        {"item_id": "distributions-04-036", "queue": "distributions", "status": "applied", "change": "D removed",
         "reason": "", "security_id": "1", "ticker": "T", "date": "2025-07-15", "verdict": "confirmed",
         "kind": "spinoff", "mode": rc.DROP_DOUBLE_CASH, "source_url": _SEC}])
    frame, facts = rc.review_data_changes(events)
    got = frame.set_index("item_id")
    assert got.loc["splits-01-001", "applied"] == "Y" and "applied" in got.loc["splits-01-001", "how"]
    assert got.loc["distributions-01-002", "applied"] == "N" and "no value" in got.loc["distributions-01-002", "how"]
    assert got.loc["moves-01-009", "how"] == "reconcile source_overrides"
    assert got.loc["distributions-04-036", "applied"] == "Y" and facts["added_by_build"] == 1
