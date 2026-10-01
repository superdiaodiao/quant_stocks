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
    assert event["nasdaq"] == pytest.approx(1.0, abs=0.01)  # the stored file moves with the total return


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
    assert len(breaks) == 1 and breaks[0]["ex_date"] == day and breaks[0]["nasdaq"] == pytest.approx(10.0, rel=0.01)
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


def test_a_stored_row_that_reverts_is_a_glitch_not_a_review_item():
    n = 30
    close = _walk(n)
    stored = close.copy()
    stored[10] *= 1.06  # one bad stored row (LANC 2020 mixes adjusted and raw closes)
    result = rc.reconcile_security("X", {"yahoo": _frame(close), "stored": _frame(stored)}, _ctx())
    for k in (10, 11):
        flags = _flags(result["canonical"], str(SESSIONS[k].date()))
        assert "stored_glitch" in flags and "disagree_unresolved" not in flags
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


def test_stored_raw_jump_on_the_ex_date_confirms_a_single_vendor_split():
    n = 30
    close = _walk(n)
    close[15:] /= 4.0
    split = np.ones(n)
    split[15] = 4.0
    result = rc.reconcile_security("X", {"yahoo": _frame(close, split=split), "stored": _frame(close)}, _ctx())
    event = result["events"][0]
    assert event["nasdaq"] == pytest.approx(4.0, rel=0.01) and event["agree"] == "Y"
    assert event["sources_confirming"] == "yahoo+stored"


def test_hidden_split_is_not_claimed_when_the_adjusted_stored_file_moves_too():
    n = 30
    close = _walk(n)
    close[20:] /= 2.2
    result = rc.reconcile_security("X", {"yahoo": _frame(close), "stored": _frame(close)}, _ctx())
    flags = _flags(result["canonical"], str(SESSIONS[20].date()))
    assert "move_2x" in flags and "hidden_split" not in flags
