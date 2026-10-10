"""Offline tests for scripts/research_megacap_oos3.py (synthetic data only)."""
import numpy as np
import pandas as pd

from scripts import research_megacap_oos3 as o3


def _days(a, n):
    return pd.bdate_range(a, periods=n)


def test_trend_series_splices_ndx_scaled_to_qqq_before_first_close():
    d = _days("1998-01-01", 300)
    first = d[250]
    ndx = pd.Series(np.linspace(1000, 2000, 300), index=d)
    qqq = pd.Series(ndx[d >= first].to_numpy() / 40.0, index=d[d >= first])
    tr = o3.trend_series(qqq, ndx, first)
    assert np.allclose(tr["x"].to_numpy(), ndx.to_numpy() / 40.0)       # scaled at the junction
    assert (tr.loc[tr.index < first, "src"] == "ndx").all() and tr.loc[first, "src"] == "qqq"
    assert np.isnan(tr["sma"].iloc[198]) and np.isclose(tr["sma"].iloc[199], tr["x"].iloc[:200].mean())


def test_apply_trend_cash_before_qqq_trades_then_qqq():
    d = _days("1999-01-01", 80)
    tr = pd.DataFrame({"x": 1.0, "sma": 2.0, "src": "ndx"}, index=d)       # always below the SMA
    m3 = {d[10]: [("A", 0.2, 1.0, 1.0, "x")], d[50]: [("A", 0.2, 1.0, 1.0, "x")]}
    out, log = o3.apply_trend(m3, tr, first_qqq_exec=d[30], sessions=d)
    assert out[d[10]] == [] and out[d[50]][0][0] == "QQQ"
    assert list(log["action"]) == ["cash (QQQ not yet trading)", "QQQ"]
    tr["x"] = 3.0
    out, _ = o3.apply_trend(m3, tr, first_qqq_exec=d[30], sessions=d)
    assert out[d[10]] == m3[d[10]]


def test_criteria_bonferroni_tiers():
    base = {"cagr": 0.08, "bench_cagr": 0.05, "max_dd": -0.80, "bench_max_dd": -0.83}
    assert o3.criteria({**base, "t_monthly_excess": 2.40})["pass"]
    c = o3.criteria({**base, "t_monthly_excess": 2.20})
    assert not c["pass"] and c["A_tier"].startswith("batch-only")
    assert o3.criteria({**base, "t_monthly_excess": 2.05})["A_tier"].startswith("nominal")
    b = o3.criteria({"cagr": 0.03, "bench_cagr": 0.05, "max_dd": -0.70, "bench_max_dd": -0.83, "t_monthly_excess": 0.0})
    assert b["B"] and b["pass"]


def test_chain_pre_scales_at_anchor_and_leaves_own_unchanged():
    cal = _days("1998-01-01", 120)
    cal_pre, own_days = cal[:100], cal[100:]
    own = pd.Series(np.linspace(5.0, 6.0, 20), index=own_days)
    pre = pd.Series(np.linspace(1.0, 2.0, 115), index=cal[:115])
    pre.iloc[100:] = own.iloc[:15].to_numpy() * 0.3            # same returns as own in the overlap
    out, chk = o3.chain_pre(pre, own.copy(), cal_pre)
    assert chk["ok"] and chk["anchor"] == str(own_days[0].date())
    k = own.iloc[0] / pre.loc[own_days[0]]
    assert np.allclose(out.to_numpy(), pre.iloc[:100].to_numpy() * k)
    assert chk["overlap_max_abs_ret_diff"] < 1e-9
    ext = o3.extended_index(pd.DataFrame({"A": own, "B": own}), {"A": out}, cal_pre)
    assert ext["A"].iloc[:100].notna().all() and ext["B"].iloc[:100].isna().all()
    assert np.allclose(ext["A"].iloc[100:].to_numpy(), own.to_numpy())


def test_vector_gross_holds_to_next_trade():
    d = _days("2000-01-03", 6)
    idx = pd.DataFrame({"A": [1, 1, 2, 2, 2, 2.0], "B": [1, 1, 1, 1, 1, 1.0]}, index=d)
    qqq = pd.Series([1, 1, 1, 1, 1.5, 1.5], index=d)
    tg = {d[0]: [("A", 0.5, 1, 1, "x"), ("B", 0.5, 1, 1, "x")], d[2]: [("QQQ", 1.0, np.nan, np.nan, "qqq")]}
    # trade d1 -> d3: 0.5*2 + 0.5*1 = 1.5; d3 -> end: QQQ 1.5
    assert np.isclose(o3.vector_gross(tg, d, idx, qqq), 1.5 * 1.5)
