"""Tests for plan step 9, the canonical series and the split / distribution / review tables
(scripts/reversal_data_reconcile.py). Synthetic frames only: no cache, no network."""
from datetime import datetime, timezone

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
