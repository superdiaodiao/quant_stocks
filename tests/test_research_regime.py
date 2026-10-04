"""Tests for scripts/research_regime.py: signals use only past data, next-close execution, rebalancing math."""
from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

import scripts.research_regime as rr
from scripts.research_qqq_timing import parse_chart

ASSETS = ("QQQ", "ONEQ", "SPY", "IEF", "GLD", "MTUM", "USMV")


def synthetic_data(n: int = 900, seed: int = 0, late: dict | None = None) -> rr.Data:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2010-01-01", periods=n)
    rets = pd.DataFrame(rng.normal(0.0004, 0.012, size=(n, len(ASSETS))), index=idx, columns=list(ASSETS))
    rets.iloc[0] = np.nan
    for t, first in (late or {}).items():          # an ETF that lists later
        rets.loc[: idx[first], t] = np.nan
    adj = (1 + rets.fillna(0)).cumprod() * 50
    for t, first in (late or {}).items():
        adj.loc[: idx[first - 1], t] = np.nan
    rets = adj.pct_change(fill_method=None)
    rf = pd.Series(0.0001, index=idx)
    return rr.Data(sessions=idx, adj=adj, close=adj.copy(), rets=rets, rf=rf, raw={}, guard={})


def truncated(d: rr.Data, cut: pd.Timestamp) -> rr.Data:
    return rr.Data(sessions=d.sessions[d.sessions <= cut], adj=d.adj.loc[:cut], close=d.close.loc[:cut],
                   rets=d.rets.loc[:cut], rf=d.rf.loc[:cut], raw={}, guard={})


# ---------------------------------------------------------------- signals use only past data

@pytest.mark.parametrize("name", [r.name for r in rr.RULES])
def test_signals_unchanged_by_truncation(name):
    d = synthetic_data()
    full = rr.target_weights(name, d)
    for k in (400, 555, 701, 899):
        cut = d.sessions[k]
        part = rr.target_weights(name, truncated(d, cut))
        # the last session of a truncated sample is never a month end, so compare up to the session before it
        a, b = full.loc[: d.sessions[k - 1]], part.loc[: d.sessions[k - 1]]
        pd.testing.assert_frame_equal(a, b)


@pytest.mark.parametrize("name", [r.name for r in rr.RULES])
def test_signals_unchanged_when_future_prices_change(name):
    d = synthetic_data()
    k = 600
    d2 = synthetic_data()
    shock = d2.adj.iloc[k + 1:] * 1.7          # a large jump in every price after session k
    d2.adj.iloc[k + 1:] = shock
    d2.rets = d2.adj.pct_change(fill_method=None)
    a = rr.target_weights(name, d).iloc[:k]
    b = rr.target_weights(name, d2).iloc[:k]
    pd.testing.assert_frame_equal(a, b)


def test_sma_state_by_hand():
    p = pd.Series([1.0, 2.0, 3.0, 2.0, 1.0])
    s = rr.sma_state(p, 3)
    assert np.isnan(s.iloc[0]) and np.isnan(s.iloc[1])
    assert s.iloc[2] == 1.0          # 3 > mean(1,2,3)=2
    assert s.iloc[3] == 0.0          # 2 < mean(2,3,2)=2.33
    assert s.iloc[4] == 0.0


def test_trailing_rf_compounds_last_n_sessions():
    rf = pd.Series([0.01, 0.02, 0.03, 0.04])
    t = rr.trailing_rf(rf, 2)
    assert np.isnan(t.iloc[0])
    assert t.iloc[3] == pytest.approx(1.03 * 1.04 - 1)


def test_vol_state_uses_own_trailing_median():
    r = pd.Series([0.01, -0.01] * 30 + [0.05, -0.05] * 5)
    s = rr.vol_state(r, window=4, med_len=20)
    assert s.iloc[59] == 0.0            # constant vol: not strictly below its median
    assert s.iloc[-1] == 0.0            # vol jumped above the median
    calm = pd.Series([0.05, -0.05] * 30 + [0.01, -0.01] * 5)
    assert rr.vol_state(calm, window=4, med_len=20).iloc[-1] == 1.0


def test_month_end_mask_and_monthly_hold():
    idx = pd.DatetimeIndex(["2020-01-30", "2020-01-31", "2020-02-03", "2020-02-28", "2020-03-02"])
    assert rr.month_end_mask(idx).tolist() == [False, True, False, True, False]
    w = pd.DataFrame({"A": [0.1, 0.2, 0.3, 0.4, 0.5]}, index=idx)
    out = rr.monthly_only(w)
    assert np.isnan(out["A"].iloc[0])
    assert out["A"].tolist()[1:] == [0.2, 0.2, 0.4, 0.4]


def test_r2b_picks_stronger_and_falls_back_to_bonds():
    d = synthetic_data(n=600)
    idx = d.sessions
    # QQQ rises strongly, SPY rises less: hold QQQ; then both fall: hold IEF
    up_q = np.r_[np.linspace(50, 100, 400), np.linspace(100, 40, 200)]
    up_s = np.r_[np.linspace(50, 80, 400), np.linspace(80, 40, 200)]
    d.adj["QQQ"], d.adj["SPY"] = up_q, up_s
    w = rr.target_weights("R2b", d)
    me = rr.month_end_mask(idx)
    i_up = [i for i in range(300, 400) if me[i]][-1]
    i_dn = [i for i in range(560, 599) if me[i]][-1]
    assert w.iloc[i_up].to_dict() == {"QQQ": 1.0, "SPY": 0.0, "IEF": 0.0}
    assert w.iloc[i_dn].to_dict() == {"QQQ": 0.0, "SPY": 0.0, "IEF": 1.0}


def test_inverse_vol_weights():
    rng = np.random.default_rng(1)
    r = pd.DataFrame({"A": rng.normal(0, 0.01, 200), "B": rng.normal(0, 0.03, 200)})
    w = rr.inverse_vol_weights(r, 63)
    assert w.iloc[:62].isna().all().all()
    last = w.iloc[-1]
    sa, sb = r["A"].iloc[-63:].std(), r["B"].iloc[-63:].std()
    assert last.sum() == pytest.approx(1.0)
    assert last["A"] / last["B"] == pytest.approx(sb / sa)


# ---------------------------------------------------------------- execution timing and rebalancing math

def two_asset_frame(n=6):
    idx = pd.bdate_range("2021-01-04", periods=n)
    rets = pd.DataFrame({"QQQ": [np.nan, 0.10, 0.20, 0.30, 0.40, 0.50][:n],
                         "IEF": [np.nan, -0.01, -0.02, -0.03, -0.04, -0.05][:n]}, index=idx)
    close = pd.DataFrame(50.0, index=idx, columns=["QQQ", "IEF"])
    return idx, rets, close


def test_next_close_execution():
    idx, rets, close = two_asset_frame()
    # decision at close of session 2: switch from QQQ to IEF
    w = pd.DataFrame({"QQQ": [1, 1, 0, 0, 0, 0], "IEF": [0, 0, 1, 1, 1, 1]}, index=idx, dtype=float)
    sim = rr.simulate(w, rets, close, start_i=1, costs=False)
    r = sim["ret"]
    # enter at close 1 with the target decided at 0 (QQQ); session 2 and 3 earn QQQ (trade happens at close 3);
    # IEF from session 4 on
    assert r.loc[idx[2]] == pytest.approx(0.20)
    assert r.loc[idx[3]] == pytest.approx(0.30)
    assert r.loc[idx[4]] == pytest.approx(-0.04)
    assert sim["trades"] == 1
    same = rr.simulate(w, rets, close, start_i=1, lag=0, costs=False)["ret"]
    assert same.loc[idx[3]] == pytest.approx(-0.03)     # lag 0: traded at close 2


def test_drift_without_rebalance_and_monthly_rebalance():
    idx, rets, close = two_asset_frame()
    w = pd.DataFrame({"QQQ": 0.5, "IEF": 0.5}, index=idx)
    sim = rr.simulate(w, rets, close, start_i=1, costs=False)
    q, b = 5000.0, 5000.0
    for i in range(2, 6):
        q *= 1 + rets["QQQ"].iloc[i]
        b *= 1 + rets["IEF"].iloc[i]
        assert sim["value"].iloc[i - 1] == pytest.approx(q + b)
    assert sim["trades"] == 0 and sim["orders"] == 2
    # a new target at decision 3 rebalances at close 4 back to the new weights
    w2 = w.copy()
    w2.iloc[3:] = [0.2, 0.8]
    s2 = rr.simulate(w2, rets, close, start_i=1, costs=False)
    h = s2["held"]
    assert h.loc[idx[5], "QQQ"] == pytest.approx(0.2)
    assert s2["trades"] == 1


def test_costs_charged_on_trades_only():
    idx, rets, close = two_asset_frame()
    w = pd.DataFrame({"QQQ": [1, 1, 0, 0, 0, 0], "IEF": [0, 0, 1, 1, 1, 1]}, index=idx, dtype=float)
    a = rr.simulate(w, rets, close, start_i=1, costs=True)
    b = rr.simulate(w, rets, close, start_i=1, costs=False)
    buy = rr.order_cost(10000, 50.0, False, 1e-4)
    assert a["value"].iloc[0] == pytest.approx(10000 - buy)
    ratio = (a["value"] / b["value"])
    assert ratio.iloc[1] == pytest.approx(ratio.iloc[0])          # no cost between trades
    assert ratio.iloc[3] < ratio.iloc[2]                          # cost at the switch (close 3)


def test_order_cost_numbers():
    # 200 shares at $50: commission 0.70, fees 0.10, spread 1 bp of 10,000 = 1.0, sell reg 0.3
    assert rr.order_cost(10000, 50.0, True, 1e-4) == pytest.approx(0.70 + 0.10 + 1.0 + 0.30)
    assert rr.order_cost(100, 50.0, False, 1e-4) == pytest.approx(0.35 + 0.001 + 0.01)   # minimum commission


def test_entry_index_requires_listing_and_month_end():
    d = synthetic_data(n=700, late={"MTUM": 450})
    w = rr.target_weights("R4", d)
    i = rr.entry_index(w, d, ("MTUM", "USMV"), False)
    assert i == 450 and not np.isnan(d.adj["MTUM"].iloc[i]) and np.isnan(d.adj["MTUM"].iloc[i - 1])
    w5 = rr.target_weights("R5", d)
    j = rr.entry_index(w5, d, ("QQQ", "IEF", "GLD"), True)
    assert rr.month_end_mask(d.sessions)[j - 1]
    assert w5.iloc[: j - 1].isna().any(axis=1).sum() >= 63
    sim = rr.simulate(w, d.rets, d.close, i)          # never holds MTUM before it lists
    assert len(sim["ret"]) == len(d.sessions) - 1 - i


# ---------------------------------------------------------------- criteria and date guard

def p(cagr, oneq, dd, oneq_dd, t=0.0):
    return {"cagr": cagr, "oneq_cagr": oneq, "dd_shallower_than_oneq_pp": (abs(oneq_dd) - abs(dd)) * 100,
            "t_monthly_excess_vs_oneq": t}


def test_criteria():
    ok_a = rr.evaluate(p(0.15, 0.12, -0.5, -0.5, 2.1), p(0.1, 0.09, -0.5, -0.5), p(0.2, 0.19, -0.3, -0.3))
    assert ok_a["A_higher_cagr_all_three_and_t_ge_2"] and ok_a["pass"]
    no_t = rr.evaluate(p(0.15, 0.12, -0.5, -0.5, 1.9), p(0.1, 0.09, -0.5, -0.5), p(0.2, 0.19, -0.3, -0.3))
    assert not no_t["pass"]
    ok_b = rr.evaluate(p(0.11, 0.13, -0.3, -0.5), p(0.08, 0.10, -0.2, -0.5), p(0.15, 0.17, -0.2, -0.35))
    assert ok_b["B_dd_10pp_shallower_and_cagr_within_3pp_all_three"] and ok_b["pass"]
    one_half_bad = rr.evaluate(p(0.11, 0.13, -0.3, -0.5), p(0.08, 0.10, -0.2, -0.5), p(0.13, 0.17, -0.2, -0.35))
    assert not one_half_bad["pass"]


def test_parse_chart_truncates_after_end(tmp_path):
    days = pd.to_datetime(["2026-09-29 16:00", "2026-09-30 16:00", "2026-10-01 16:00"]).tz_localize("America/New_York")
    ts = [int(x.timestamp()) for x in days]
    j = {"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [{"close": [1.0, 2.0, 3.0]}],
                                                              "adjclose": [{"adjclose": [1.0, 2.0, 3.0]}]}}]}}
    f = tmp_path / "chart_X.json"
    f.write_text(json.dumps(j))
    df = parse_chart(f, rr.END)
    assert df["date"].tolist() == ["2026-09-29", "2026-09-30"]
