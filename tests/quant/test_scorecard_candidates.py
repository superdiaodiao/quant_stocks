"""studies/scorecard_candidates.py: the pure helpers (turnover from held weights, calendar CAGR, window coverage,
descriptive ranks) and the consistency of its declarations. The run itself reads the research caches; its
reproduction checks against each study's saved output are in output/research_only/scorecard/checks.json."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from quant.evaluation import scorecard as sc
from studies import scorecard_candidates as scc


def test_one_way_turnover_counts_the_trade_back_to_target():
    idx = pd.bdate_range("2020-01-01", periods=3)
    held = pd.DataFrame({"A": 0.5, "B": 0.5}, index=idx)                 # 50 / 50, rebalanced every close
    rets = pd.DataFrame({"A": [0.0, 0.10, 0.0], "B": 0.0}, index=idx)   # A drifts to 0.55 / 1.05 on day 2
    traded = 0.55 / 1.05 - 0.5                                           # one-way: sell this much A, buy as much B
    assert scc.one_way_turnover(held, rets) == pytest.approx(traded / (3 / 252), rel=1e-12)
    assert scc.one_way_turnover(held, rets * 0) == 0.0


def test_calendar_cagr_and_returns_from_nav():
    nav = pd.Series([10_000.0, 11_000.0], index=pd.to_datetime(["2020-01-01", "2021-01-01"]))
    r = scc.returns_from_nav(nav)
    assert r.tolist() == [pytest.approx(0.1)] and r.index[0] == pd.Timestamp("2021-01-01")
    assert scc.calendar_cagr(r, nav.index[0]) == pytest.approx(1.1 ** (365.25 / 366) - 1)


def test_cut_needs_the_whole_window():
    r = pd.Series(0.001, index=pd.bdate_range("2020-01-02", "2020-12-31"))
    assert len(scc.cut(r, "2020-01-01", "2020-12-31")) == len(r)
    assert scc.cut(r, "2019-12-01", "2020-12-31") is None                # starts 32 days late
    assert scc.cut(r, "2020-01-01", "2021-01-31") is None                # ends 31 days early
    assert scc.cut(r, "2020-03-01", "2020-06-30").index[0] == pd.Timestamp("2020-03-02")


def test_angle_ranks_skip_self_comparisons_and_benchmarks_in_confidence():
    keys = sorted({k for mets in scc.ANGLES.values() for k, _ in mets})
    cmp = pd.DataFrame({"family": "x", "unit": "x", "ONEQ": 0.0, "A": 1.0, "B": 2.0}, index=keys)
    cmp.loc["years_beating_oneq_share", ["ONEQ", "A", "B"]] = [0.0, 0.4, 0.6]
    detail, avg = scc.angle_ranks(cmp, ["ONEQ", "A", "B"])
    y = detail[detail["metric"] == "years_beating_oneq_share"].set_index("candidate")["rank"]
    assert math.isnan(y["ONEQ"]) and (y["A"], y["B"]) == (2.0, 1.0)
    assert math.isnan(avg.loc["ONEQ", "统计可信度"])
    lower = detail[(detail["metric"] == "operational_complexity")].set_index("candidate")["rank"]
    assert lower.idxmin() == "ONEQ"                                     # lower is better: 0 ranks first


def test_declarations_name_real_scorecard_metrics():
    for mets in scc.ANGLES.values():
        for key, sign in mets:
            assert sign in (1, -1)
            assert key == "capture_spread" or sc.spec(key)
    for keys in scc.HEADLINE.values():
        for key in keys:
            assert sc.spec(key)
    assert set(scc.META) == {"ONEQ", "QQQ", "T01", "LEV-B3", "V3", "BT4b", "M1", "M2", "M3", "M6", "S3-Yb", "SEL-A",
                             "SEL-P", "S-MISP"}
    for m in scc.META.values():
        assert m["oos_status"] in sc.OOS_STATUS and set(m.get("permissions", [])) <= set(sc.PERMISSIONS)
    assert scc.SEL == {"SEL-A": 29876, "SEL-P": 29916} and scc.MEGA == ("M1", "M2", "M3", "M6")
    assert (scc.LEV["on"], scc.LEV["off"], scc.LEV["matched_exposure"]) == (1.5, 1.0, 1.37)
    assert np.isclose(scc.T01_SPREAD, 0.0069599408878067825)
