"""Offline tests for scripts/stop_rules.py and scripts/research_stops.py (synthetic data only)."""
import math
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from scripts import research_canslim_dev as cs
from scripts import research_indicators as ri
from scripts import research_livermore as lv
from scripts import research_stops as rs
from scripts import stop_rules as sr


# ---------------------------------------------------------------- stop rules

def test_grid_is_the_registered_one():
    g = sr.stop_grid()
    labels = [s.label for s in g]
    assert len(g) == 28 and len(set(labels)) == 28
    assert labels[0] == "none" and labels[1] == "fixed3" and labels[18] == "fixed20"
    assert labels[19:25] == ["trail5", "trail10", "trail15", "trail20", "trail25", "trail30"]
    assert labels[25:] == ["vol2", "vol3", "vol4"]
    assert all(sr.parse(lab) == s for lab, s in zip(labels, g))


def test_triggered_formulas():
    f8, t10, v3 = sr.StopSpec("fixed", 0.08), sr.StopSpec("trail", 0.10), sr.StopSpec("vol", 3.0)
    assert sr.triggered(f8, 0.9199, 1.0, 1.5, np.nan) and not sr.triggered(f8, 0.9201, 1.0, 1.5, np.nan)
    assert sr.triggered(t10, 1.3499, 1.0, 1.5, np.nan) and not sr.triggered(t10, 1.3501, 1.0, 1.5, np.nan)
    assert sr.triggered(v3, 0.9399, 1.0, 1.0, 0.02) and not sr.triggered(v3, 0.9401, 1.0, 1.0, 0.02)
    assert not sr.triggered(v3, 0.5, 1.0, 1.0, np.nan)            # no sigma -> never
    assert not sr.triggered(sr.NONE, 0.01, 1.0, 1.0, 0.02)
    assert not sr.triggered(f8, np.nan, 1.0, 1.0, 0.02)
    with pytest.raises(ValueError):
        sr.schedule([sr.NONE], 2)


def test_sigma20_is_causal():
    idx = pd.DataFrame({"A": np.cumprod(1 + np.r_[0, np.tile([0.01, -0.01], 20)])},
                       index=pd.bdate_range("2020-01-01", periods=41))
    s = sr.sigma20(idx)["A"]
    assert s.iloc[:20].isna().all() and np.isfinite(s.iloc[20])
    idx2 = idx.copy()
    idx2.iloc[30:] *= 3
    assert sr.sigma20(idx2)["A"].iloc[:30].equals(s.iloc[:30])


# ---------------------------------------------------------------- Livermore engine with stops

def _lv_window(path):
    sessions = pd.bdate_range("2016-12-01", periods=len(path) + 22)
    perf = sessions[22:]
    close = pd.DataFrame({"A": np.r_[np.full(22, path[0]), path]}, index=sessions)
    idx = close / close.iloc[0]
    perf_idx = idx.copy()
    perf_idx.loc[perf_idx.index < perf[0]] = np.nan
    q = pd.Series(100.0, index=sessions)
    spec = {"perf_start": str(perf[0].date()), "perf_end": str(sessions[-1].date()),
            "effective_end": str(sessions[-1].date()), "price_start": str(sessions[0].date()),
            "universe_start": str(sessions[0].date()), "judged_from": str(perf[0].date())}
    data = lv.WinData(name="syn", spec=spec, sessions=sessions, universe=pd.DataFrame(), sig_idx=idx,
                      perf_idx=perf_idx, close=close, close_adj=close, vol_adj=close * 0 + 1,
                      last_row=pd.Series(sessions[-1], index=["A"]), qqq_close=q, qqq_perf_idx=q / q.iloc[0],
                      terminal_events=pd.DataFrame(), guard={})
    return data, perf


def _lv_run(data, sig_day, stops, **kw):
    cfg = lv.Config(k=1, tranches=(1.0,), stop=0.08, trail="pct90", market="none", idle="cash", spread_mult=0.0)
    brk = pd.DataFrame(False, index=data.sessions, columns=["A"])
    brk.loc[sig_day, "A"] = True
    fridays = [d for d in data.sessions if d.weekday() == 4]
    leaders = pd.DataFrame([{"week_end": f, "security_id": "A", "rs": 1.0} for f in fridays])
    return lv.simulate(cfg, data, leaders, brk, None, pd.Series(True, index=data.sessions), {}, stops=stops, **kw)


def test_livermore_fixed_stop_triggers_at_close_and_sells_next_close():
    path = np.r_[np.full(5, 10.0), [10.0, 9.8, 9.6, 9.4, 9.2, 9.0, 8.8, 8.6, 8.4, 8.2], np.full(15, 8.0)]
    data, perf = _lv_window(path)
    res = _lv_run(data, perf[3], sr.StopSpec("fixed", 0.05))
    o = res["orders"]
    buy, sell = o[o["side"] == "buy"].iloc[0], o[o["side"] == "sell"].iloc[0]
    assert buy["date"] == perf[4] and sell["reason"] == "stop"
    entry = data.close.loc[perf[4], "A"]
    i = list(perf).index(sell["date"])
    assert data.close.loc[perf[i - 1], "A"] / entry - 1 <= -0.05     # triggered on the previous close
    assert data.close.loc[perf[i - 2], "A"] / entry - 1 > -0.05
    # none: never stops; the original code path (stops=None, cfg.stop=8%) equals passing fixed8
    assert (_lv_run(data, perf[3], sr.NONE)["orders"]["side"] == "sell").sum() == 0
    a, b = _lv_run(data, perf[3], None), _lv_run(data, perf[3], sr.StopSpec("fixed", 0.08))
    assert np.allclose(a["nav"], b["nav"])


def test_livermore_trailing_stop_tracks_the_highest_close_since_entry():
    path = np.r_[np.full(5, 10.0), [10, 11, 12, 13, 12.5, 12.0, 11.6, 11.0, 10.5], np.full(16, 10.5)]
    data, perf = _lv_window(path)
    res = _lv_run(data, perf[4], sr.StopSpec("trail", 0.10))        # filled at perf[5] (10.0)
    sell = res["orders"][res["orders"]["side"] == "sell"].iloc[0]
    # peak 13 (perf[8]); 13 * 0.9 = 11.7 -> first close <= 11.7 is 11.6 at perf[11] -> sold at perf[12]
    assert sell["reason"] == "stop" and sell["date"] == perf[12]
    # a fixed 10% stop from the entry (10.0) would never fire on this path
    assert (_lv_run(data, perf[4], sr.StopSpec("fixed", 0.10))["orders"]["side"] == "sell").sum() == 0


def test_livermore_stop_schedule_switch_applies_from_its_session():
    path = np.r_[np.full(5, 10.0), [10, 9.6, 9.5, 9.5, 9.5, 9.5, 9.5], np.full(18, 9.5)]
    data, perf = _lv_window(path)
    sched = [sr.NONE] * len(perf)
    k = 15
    sched[k:] = [sr.StopSpec("fixed", 0.04)] * (len(perf) - k)       # the tighter stop starts at close k
    res = _lv_run(data, perf[3], sched)
    sell = res["orders"][res["orders"]["side"] == "sell"].iloc[0]
    assert sell["date"] == perf[k + 1]                               # already below -4%: sold at the next close


# ---------------------------------------------------------------- O10 engine (research_indicators.stock_sim)

def _ri_data(I):
    idx = I.index
    return SimpleNamespace(sessions=idx, spec={"perf_start": str(idx[0].date()), "effective_end": str(idx[-1].date())},
                           perf_idx=I, close=I * 20, sig_idx=I,
                           last_row=pd.Series([pd.NaT] * len(I.columns), index=I.columns))


def test_o10_trailing_and_vol_stops_next_close():
    idx = pd.bdate_range("2020-01-01", periods=40)
    rets = np.r_[np.tile([0.02, -0.02], 11), [0.05, 0.05, 0.05], [-0.03, -0.03, -0.03, -0.03], np.zeros(11)]
    I = pd.DataFrame({"A": np.cumprod(1 + rets)}, index=idx)
    data = _ri_data(I)
    buy = pd.DataFrame(False, index=idx, columns=["A"])
    sell = pd.DataFrame(False, index=idx, columns=["A"])
    buy.loc[idx[21], "A"] = True                                      # filled at idx[22]
    rule = ri.RULE_BY_CODE["O10"]
    res = ri.stock_sim(rule, data, buy, sell, {idx[0]: ["A"]}, {}, stops=sr.StopSpec("trail", 0.05))
    tr = res["trades"].iloc[0]
    # peak at idx[24]; closes -3% per day from idx[25]: -3% then -5.9% at idx[26] -> sold at idx[27]
    assert tr["entry"] == idx[22] and tr["reason"] == "stop" and tr["exit"] == idx[27]
    assert res["counts"]["stop"] == 1
    # vol stop: sigma20 at the entry close ~ 2.3%; k=1 -> distance ~2.3% below the entry close (k=2 never fires here)
    assert ri.stock_sim(rule, data, buy, sell, {idx[0]: ["A"]}, {}, stops=sr.StopSpec("vol", 2.0))["trades"].empty
    res = ri.stock_sim(rule, data, buy, sell, {idx[0]: ["A"]}, {}, stops=sr.StopSpec("vol", 1.0))
    tr = res["trades"].iloc[0]
    sig = sr.sigma20(I)["A"].loc[idx[22]]
    entry = I["A"].loc[idx[22]]
    hit = [d for d in idx[23:] if I["A"].loc[d] / entry - 1 <= -1 * sig][0]
    assert tr["exit"] == idx[list(idx).index(hit) + 1] and tr["reason"] == "stop"
    # default path unchanged
    a = ri.stock_sim(rule, data, buy, sell, {idx[0]: ["A"]}, {})
    b = ri.stock_sim(rule, data, buy, sell, {idx[0]: ["A"]}, {}, stops=sr.NONE)
    assert np.allclose(a["nav"], b["nav"]) and "stop" not in a["counts"]


# ---------------------------------------------------------------- CAN SLIM engine

def test_canslim_trailing_stop_sells_next_close():
    path = [1.0, 1.1, 1.3, 1.25, 1.15, 1.10, 1.10]
    sessions = pd.bdate_range("2017-01-02", periods=len(path))
    idx = pd.DataFrame({"X": path}, index=sessions)
    sched = [(pd.Timestamp("2016-12-30"), sessions[0], True, True)]
    args = (cs.Config(k=1, stop=0.08, m_rule="none"), sessions, idx, idx * 50, pd.Series({"X": sessions[-1]}),
            pd.Series(1.0, index=sessions), pd.Series(300.0, index=sessions), sched,
            {pd.Timestamp("2016-12-30"): ["X"]}, {}, 10_000.0)
    res = cs.simulate(*args, stops=sr.StopSpec("trail", 0.10))
    tr = res["trades"]
    # peak 1.3; 1.15 <= 1.17 at sessions[4] -> sold at sessions[5]
    assert tr["reason"].tolist() == ["pending_stop"] and tr["exit"].iloc[0] == sessions[5]
    assert res["n_stop"] == 1
    assert cs.simulate(*args)["n_sell"] == 0                          # original 8% from entry never fires
    with pytest.raises(ValueError):
        cs.simulate(*args, stops=sr.StopSpec("vol", 2.0))             # vol needs sigma


# ---------------------------------------------------------------- walk-forward picking

def _rets(seed, mu, n=1500, start="2014-06-02"):
    rng = np.random.default_rng(seed)
    return pd.Series(rng.normal(mu, 0.01, n), index=pd.bdate_range(start, periods=n))


def test_pick_uses_only_data_before_january_first():
    rets = {"none": _rets(1, 0.0005), "fixed8": _rets(2, 0.0002)}
    p = rs.pick(rets, 2018, "sharpe")
    assert pd.Timestamp(p["window_last"]) < pd.Timestamp("2018-01-01")
    changed = {k: v.copy() for k, v in rets.items()}
    for v in changed.values():
        v[v.index >= "2018-01-01"] = np.where(np.arange((v.index >= "2018-01-01").sum()) % 2, 0.5, -0.4)
    changed["fixed8"][changed["fixed8"].index >= "2018-01-01"] = 0.05
    for m in rs.PICK_METRICS:
        assert rs.pick(changed, 2018, m)["key"] == rs.pick(rets, 2018, m)["key"]
    # data older than the 3-year lookback is ignored too
    old = {k: v.copy() for k, v in rets.items()}
    old["fixed8"][old["fixed8"].index < "2015-01-01"] = 0.05
    assert rs.pick(old, 2018, "sharpe")["key"] == p["key"]


def test_pick_best_metric_and_ties_by_grid_order():
    idx = pd.bdate_range("2015-01-01", "2017-12-29")
    a = pd.Series(np.tile([0.01, -0.005], len(idx))[:len(idx)], index=idx)
    b = a * 2                       # same Sharpe, higher CAGR, deeper drawdown
    assert rs.pick({"none": a, "fixed5": b}, 2018, "sharpe")["key"] == "none"
    assert rs.pick({"fixed5": b, "none": a}, 2018, "sharpe")["key"] == "fixed5"
    assert rs.pick({"none": a, "fixed5": b}, 2018, "cagr")["key"] == "fixed5"
    c = a.copy()
    c.iloc[100] = -0.30             # one crash: worse Calmar
    assert rs.pick({"fixed5": c, "none": a}, 2018, "calmar")["key"] == "none"


def test_trailing_window_and_schedule_timing():
    idx = pd.bdate_range("2017-12-20", "2018-01-10")
    dy = rs.decision_year(idx)
    assert dy[list(idx).index(pd.Timestamp("2017-12-29"))] == 2018      # last close of 2017 decides with 2018's pick
    assert dy[list(idx).index(pd.Timestamp("2017-12-28"))] == 2017
    sched = rs.stop_schedule(idx, {2017: {"key": "none"}, 2018: {"key": "trail15"}})
    assert sched[list(idx).index(pd.Timestamp("2017-12-28"))] == sr.NONE
    assert sched[list(idx).index(pd.Timestamp("2017-12-29"))] == sr.StopSpec("trail", 0.15)
    r = _rets(3, 0.0)
    w = rs.trailing_window(r, 2018)
    assert w.index.min() >= pd.Timestamp("2015-01-01") and w.index.max() < pd.Timestamp("2018-01-01")


# ---------------------------------------------------------------- metric formulas

def test_drawdown_duration_and_ulcer():
    d = pd.date_range("2020-01-01", periods=6, freq="D")
    nav = pd.Series([100, 110, 99, 105, 111, 100.0], index=d)
    days, unrec = rs.longest_drawdown_days(nav)
    assert days == 3 and not unrec                         # peak 110 on day 1, recovered (111) on day 4
    nav2 = pd.Series([100, 120, 90, 95, 100, 101.0], index=d)
    assert rs.longest_drawdown_days(nav2) == (4, True)     # never back to 120: counts to the last date
    dd = np.array([0, 0, -0.1, -5 / 110, 0, -11 / 111]) * 100
    assert rs.ulcer_index(nav) == pytest.approx(math.sqrt(np.mean(dd ** 2)))


def test_sharpe_sortino_calmar_cagr():
    r = pd.Series([0.01, -0.02, 0.03, -0.01, 0.02])
    assert rs.sharpe(r) == pytest.approx(r.mean() / r.std(ddof=1) * math.sqrt(252))
    assert rs.sortino(r) == pytest.approx(r.mean() / math.sqrt((0.02 ** 2 + 0.01 ** 2) / 5) * math.sqrt(252))
    v = np.cumprod(1 + r.to_numpy())
    assert rs.cagr_of_returns(r) == pytest.approx(v[-1] ** (252 / 5) - 1)
    assert rs.mdd_of_returns(r) == pytest.approx(min(0, (v[1] / 1.01) - 1, v[3] / v[2] - 1, (v / np.maximum.accumulate(np.r_[1, v])[1:] - 1).min()))
    assert rs.calmar_of_returns(r) == pytest.approx(rs.cagr_of_returns(r) / abs(rs.mdd_of_returns(r)))


def test_full_metrics_on_synthetic_series():
    d = pd.bdate_range("2019-01-01", "2021-12-31")
    rng = np.random.default_rng(7)
    rb = pd.Series(rng.normal(0.0005, 0.01, len(d)), index=d)
    oneq = 10_000 * (1 + rb).cumprod()
    qqq = oneq * 1.0
    # strategy = 1.5 x ONEQ daily returns -> monthly beta ~1.5 (compounding makes it inexact)
    nav = 10_000 * (1 + 1.5 * rb).cumprod()
    m = rs.metrics(nav, oneq, qqq, pd.Series(0.0, index=d), 0.0, pd.Series(1.0, index=d), None, 0, 0)
    assert m["beta_oneq"] == pytest.approx(1.5, abs=0.05)
    mr = rs.anchored(nav, 10_000).resample("ME").last().pct_change().dropna()
    mb = rs.anchored(oneq, 10_000).resample("ME").last().pct_change().dropna()
    ma = mr - mb
    assert m["months"] == 36
    assert m["t_monthly_excess"] == pytest.approx(ma.mean() / ma.std(ddof=1) * 6)
    assert m["ir_oneq"] == pytest.approx(ma.mean() / ma.std(ddof=1) * math.sqrt(12))
    beta, a = np.polyfit(mb, mr, 1)
    assert m["alpha_oneq"] == pytest.approx(a * 12)
    assert m["worst_month"] == pytest.approx(mr.min())
    years = (d[-1] - d[0]).days / 365.25
    assert m["cagr"] == pytest.approx((nav.iloc[-1] / 10_000) ** (1 / years) - 1)
    yr = {y: nav[nav.index.year == y].iloc[-1] / (nav[nav.index.year < y].iloc[-1] if y > 2019 else 10_000) - 1
          for y in (2019, 2020, 2021)}
    assert m["worst_year"] == pytest.approx(min(yr.values()))
    assert m["excess_oneq"] == pytest.approx(m["cagr"] - m["oneq_cagr"]) and m["time_in_market"] == 1.0
    stub = pd.concat([pd.Series([10_000.0], index=[pd.Timestamp("2018-12-31")]), nav])
    ob = pd.concat([pd.Series([10_000.0], index=[pd.Timestamp("2018-12-31")]), oneq])
    ms = rs.metrics(stub, ob, ob, pd.Series(0.0, index=stub.index), 0.0, pd.Series(1.0, index=stub.index), None, 0, 0)
    assert 2018 not in ms["by_year"]                     # a year holding only the starting close is not a year
    same = rs.metrics(oneq, oneq, qqq, pd.Series(0.0, index=d), 0.0, pd.Series(1.0, index=d), None, 0, 0)
    assert same["beta_oneq"] == pytest.approx(1.0) and same["share_years_beating_oneq"] == 0.0


def test_trade_metrics_and_judge():
    t = pd.DataFrame({"ret": [0.2, -0.05, 0.1, -0.1, -0.05], "days": [10, 3, 5, 4, np.nan]})
    m = rs.trade_metrics(t)
    assert m["closed_trades"] == 5 and m["win_rate"] == pytest.approx(0.4)
    assert m["win_loss_ratio"] == pytest.approx(0.15 / (0.2 / 3))
    assert m["profit_factor"] == pytest.approx(0.3 / 0.2)
    assert m["avg_hold_days"] == pytest.approx(5.5)
    base = {"cagr": 0.10, "oneq_cagr": 0.12, "t_monthly_excess": 3.0, "max_dd": -0.20, "oneq_max_dd": -0.35}
    assert rs.judge(base) == {"A": False, "B": True, "pass": True}
    assert rs.judge({**base, "max_dd": -0.26})["pass"] is False
    assert rs.judge({**base, "cagr": 0.13, "max_dd": -0.4}) == {"A": True, "B": False, "pass": True}
    assert rs.judge({**base, "cagr": 0.13, "t_monthly_excess": 1.9, "max_dd": -0.4})["pass"] is False
