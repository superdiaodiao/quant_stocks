"""Offline tests for scripts/research_grid_check.py (synthetic data only)."""
import numpy as np
import pandas as pd
import pytest

from scripts import research_grid_check as gc
from scripts import research_indicators as ri


def test_ma_grid_matches_owner_optimiser():
    g = gc.ma_grid()
    assert len(g) == 8 * 41 == 328                      # short 3..10, long 20..60, all short < long
    assert len(set(g)) == len(g) and all(s < l_ for s, l_ in g)
    assert min(s for s, _ in g) == 3 and max(s for s, _ in g) == 10
    assert min(l_ for _, l_ in g) == 20 and max(l_ for _, l_ in g) == 60
    assert gc.OWNER_REF["ma"] in g


def test_donchian_grid():
    g = gc.donchian_grid()
    assert len(g) == 51 * 6 == 306
    assert {e for _, e in g} == {5, 10, 15, 20, 25, 30}
    assert {n for n, _ in g} == set(range(10, 61))
    assert gc.OWNER_REF["donchian"] in g and (20, 10) in g and (55, 20) in g


def test_period_constants_match_the_ledger():
    assert gc.INDEX_PERIODS["dev"] == ("1999-03-10", "2014-12-31")
    assert gc.INDEX_PERIODS["test"] == ("2014-12-31", "2026-09-30")
    assert set(gc.STOCK_PERIODS.values()) == {"dev", "test1", "test2"}
    assert ri.lv.WINDOWS["dev"]["perf_end"] == "2022-12-31"
    assert ri.lv.WINDOWS["test2"]["judged_from"] == "2014-01-01"


def test_assert_in_period_guards():
    ix = pd.bdate_range("2014-12-01", "2014-12-31")
    gc.assert_in_period(ix, "2014-12-01", "2014-12-31")
    with pytest.raises(gc.FutureDataError):
        gc.assert_in_period(ix.append(pd.DatetimeIndex(["2015-01-02"])), "2014-12-01", "2014-12-31")
    with pytest.raises(AssertionError):
        gc.assert_in_period(ix, "2014-12-15", "2014-12-31")
    with pytest.raises(AssertionError):
        gc.assert_in_period(pd.DatetimeIndex([]), "2014-12-01", "2014-12-31")


def test_rules_reuse_the_indicator_study():
    rng = np.random.default_rng(3)
    idx = pd.bdate_range("2001-01-01", periods=400)
    c = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.015, 400))), index=idx)
    bars = {"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "vix": None, "has_hl": True}
    for kind, (a, b), ref in (("ma", (5, 20), ri.RULE_BY_CODE["O1"]), ("donchian", (20, 20), ri.RULE_BY_CODE["O10"])):
        r = gc.make_rule(kind, a, b)
        assert r.holding == ref.holding
        for x, y in zip(r.fn(bars), ref.fn(bars)):
            pd.testing.assert_series_equal(x, y)
    # the closure keeps its own parameters
    r1, r2 = gc.make_rule("ma", 3, 20), gc.make_rule("ma", 10, 60)
    assert not r1.fn(bars)[0].equals(r2.fn(bars)[0])
