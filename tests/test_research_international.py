"""Tests for the international study (synthetic data only): exchange-time-zone dates, no look-ahead in the index
rules and the rotation, execution lag and costs of the exposure engine, data checks, currency decomposition."""
from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

from quant.data import international as intl
from quant.data.guards import FutureDataError
from quant.data.sources.yahoo import parse_chart, parse_chart_exchange
from quant.strategies import international as st
from studies import international as study

P = st.Params()


def synthetic(n=1500, seed=0, start="1990-01-01") -> pd.Series:
    rng = np.random.default_rng(seed)
    r = rng.normal(0.0004, 0.012, n)
    if n > 650:
        r[500:650] = rng.normal(-0.004, 0.02, 150)      # a bear phase so the rules switch
    idx = pd.bdate_range(start, periods=n)
    return pd.Series(100 * np.cumprod(1 + r), index=idx)


def _chart(tmp_path, tz, stamps, closes):
    j = {"chart": {"result": [{"meta": {"exchangeTimezoneName": tz}, "timestamp": stamps,
                               "indicators": {"quote": [{"close": closes}], "adjclose": [{"adjclose": closes}]}}]}}
    p = tmp_path / "c.json"
    p.write_text(json.dumps(j))
    return p


# ---------------------------------------------------------------- data

def test_exchange_dates_are_local_not_new_york(tmp_path):
    # Tokyo opens 09:00 JST = 00:00 UTC = the previous evening in New York
    stamps = [int(pd.Timestamp(d, tz="Asia/Tokyo").timestamp()) for d in
              ("2020-01-06 09:00", "2020-01-07 09:00", "2020-01-08 09:00")]
    p = _chart(tmp_path, "Asia/Tokyo", stamps, [1.0, None, 3.0])
    df = parse_chart_exchange(p, "2020-01-07")
    assert df["date"].tolist() == ["2020-01-06"]                     # null close dropped, truncated at end
    assert parse_chart(p, "2020-01-07")["date"].tolist()[0] == "2020-01-05"   # the NY parser would shift it


def test_exchange_parser_rejects_duplicates(tmp_path):
    s = int(pd.Timestamp("2020-01-06 09:00", tz="Asia/Tokyo").timestamp())
    with pytest.raises(ValueError):
        parse_chart_exchange(_chart(tmp_path, "Asia/Tokyo", [s, s + 60], [1.0, 2.0]), "2020-02-01")


def test_series_checks_flags_flat_runs_gaps_and_moves():
    idx = pd.bdate_range("2000-01-03", periods=600)
    p = pd.Series(np.linspace(100, 200, 600), index=idx)
    p.iloc[100:108] = p.iloc[100]                                    # 8 identical closes
    p.iloc[300] = p.iloc[299] * 1.30
    p = p.drop(idx[400:405])                                         # a gap of a week
    c = intl.series_checks(p)
    assert c["flat_runs_ge_5"] == 1 and c["longest_flat_run"] == 8
    assert c["days_abs_move_gt_15pct"] == 2 and c["longest_gap_calendar_days"] >= 9
    assert c["duplicates"] == 0 and c["non_positive"] == 0


def test_currency_decomposition_recovers_the_fx_part():
    idx = pd.bdate_range("2005-01-03", periods=1500)
    rng = np.random.default_rng(1)
    loc = pd.Series(100 * np.cumprod(1 + rng.normal(0.0003, 0.01, len(idx))), index=idx)
    fx_local_per_usd = pd.Series(100 * np.cumprod(1 + rng.normal(0.0001, 0.005, len(idx))), index=idx)
    etf = loc / fx_local_per_usd * 1.0001 ** np.arange(len(idx))     # USD price plus a small daily "dividend"
    v = intl.etf_vs_index(loc, etf, fx_local_per_usd, inverse=True)
    assert v["corr_etf_vs_index_usd"] > 0.999
    assert abs((1 + v["cagr_index_local_price"]) * (1 + v["cagr_fx_usd_per_local"]) - 1 - v["cagr_index_usd_price"]) < 1e-9
    assert v["gap_etf_minus_index_usd"] > 0


# ---------------------------------------------------------------- index rules: no look-ahead, timing, formulas

@pytest.mark.parametrize("rule", st.INDEX_RULES)
def test_index_targets_do_not_look_ahead(rule):
    p = synthetic()
    base = st.index_target(rule, p, P)
    cut = 900
    q = p.copy()
    q.iloc[cut + 1:] *= np.linspace(0.3, 3.0, len(q) - cut - 1)        # shock the future
    shocked = st.index_target(rule, q, P)
    pd.testing.assert_series_equal(base.iloc[:cut], shocked.iloc[:cut])
    trunc = st.index_target(rule, p.iloc[:cut + 1], P)
    pd.testing.assert_series_equal(base.iloc[:cut], trunc.iloc[:cut])


def test_i1_band_hysteresis_and_warmup():
    p = synthetic()
    e = st.index_target("I1", p, P)
    assert e.iloc[:P.sma_len - 1].isna().all() and e.iloc[P.sma_len - 1:].notna().all()
    ma = p.rolling(P.sma_len).mean()
    on = (p > ma * 1.02) & ma.notna()
    off = (p < ma * 0.98) & ma.notna()
    assert (e[on] == 1).all() and (e[off] == 0).all()
    assert set(e.dropna().unique()) <= {0.0, 1.0}


def test_i2_and_i3_change_only_after_month_ends():
    p = synthetic()
    me = pd.Series(p.index.to_period("M"), index=p.index)
    is_me = np.array(me != me.shift(-1))
    is_me[-1] = False
    for rule in ("I2", "I3"):
        e = st.index_target(rule, p, P)
        # a value first appears or changes only on a month-end row
        first = e.notna() & e.shift(1).isna()
        assert is_me[(first | (e.diff().abs() > 0)).values].all(), rule


def test_i3_formula_and_cap():
    p = synthetic()
    e = st.index_target("I3", p, P).dropna()
    assert (e <= 1.0).all() and (e > 0).all()
    r = p.pct_change()
    vol = r.rolling(20).std() * math.sqrt(252)
    d = e.index[e.diff().abs() > 0][0]                              # a month-end where it changed
    assert e.loc[d] == pytest.approx(min(1.0, 0.20 / vol.loc[d]))


def test_i2_hurdle_is_used():
    p = synthetic()
    hi = pd.Series(10.0, index=p.index)                              # nothing beats a 1000% hurdle
    assert (st.index_target("I2", p, P, hi).dropna() == 0).all()


# ---------------------------------------------------------------- exposure engine

def test_engine_lag_cost_and_cash():
    idx = pd.bdate_range("2020-01-01", periods=8)
    r = pd.Series([0.0, 0.01, 0.02, -0.05, 0.03, 0.01, 0.0, 0.02], index=idx)
    e = pd.Series([1, 1, 0, 0, 0, 1, 1, 1], index=idx, dtype=float)   # off decided at day 2, on at day 5
    sim = st.simulate_exposure(e, r, 0.001, 1, cost=0.01)
    v = sim["value"]
    assert v.iloc[0] == pytest.approx(0.99)                           # entry buy pays 1%
    assert v.iloc[1] == pytest.approx(0.99 * 1.02)                    # day 2 return still earned (trade at its close)
    # target 0 decided at day 2 -> sold at the close of day 3 (after earning day 3's -5%), cash from day 4
    v3 = 0.99 * 1.02 * 0.95
    assert v.iloc[2] == pytest.approx(v3 * (1 - 0.01))
    assert v.iloc[3] == pytest.approx(v3 * 0.99 * 1.001)
    assert sim["switches"] == 2
    assert sim["held"].loc[idx[3]] == 1.0 and sim["held"].loc[idx[4]] == 0.0   # weight earning each day's return


def test_buy_hold_equals_index_less_entry_cost():
    p = synthetic(400)
    r = p.pct_change()
    sim = st.simulate_exposure(pd.Series(1.0, index=p.index), r, 0.0, 10, cost=0.001)
    assert sim["value"].iloc[-1] == pytest.approx(0.999 * p.iloc[-1] / p.iloc[10])
    assert sim["switches"] == 0


def test_engine_raises_without_target():
    p = synthetic(300)
    with pytest.raises(ValueError):
        st.simulate_exposure(st.index_target("I1", p, P), p.pct_change(), 0.0, 5, cost=0.0)


def test_first_entry_month_end_and_not_before():
    p = synthetic()
    tg = [st.index_target(k, p, P) for k in st.INDEX_RULES]
    i = st.first_entry(tg)
    assert all(t.iloc[i - 1] == t.iloc[i - 1] for t in tg)
    assert p.index[i - 1].month != p.index[i].month
    j = st.first_entry(tg, not_before="1994-06-01")
    assert p.index[j] >= pd.Timestamp("1994-06-01") and p.index[j - 1].month != p.index[j].month


# ---------------------------------------------------------------- rotation

def _panel(n=800):
    idx = pd.bdate_range("2004-01-01", periods=n)
    t = np.arange(n)
    adj = pd.DataFrame({"A": 100 * 1.0010 ** t, "B": 100 * 1.0005 ** t, "C": 100 * 0.9995 ** t}, index=idx)
    rf = pd.Series(0.0001, index=idx)
    return adj, rf


def test_rotation_picks_top_and_filters_to_cash():
    adj, rf = _panel()
    w = st.rotation_weights(adj, rf, ("A", "B", "C"), 1, 252).dropna()
    assert (w["A"] == 1).all()
    w2 = st.rotation_weights(adj, rf, ("C", "B"), 2, 252).dropna()
    # B (+13%/yr) beats the 2.5% T-bill, C (-12%) does not -> half B, half cash
    assert (w2["B"] == 0.5).all() and (w2["CASH"] == 0.5).all() and (w2["C"] == 0).all()
    w3 = st.rotation_weights(adj, rf * 20, ("B", "C"), 1, 252).dropna()   # 50%/yr hurdle
    assert (w3["CASH"] == 1).all()
    assert np.allclose(w.sum(axis=1), 1) and np.allclose(w2.sum(axis=1), 1)


def test_rotation_no_look_ahead_and_month_end_only():
    adj, rf = _panel()
    rng = np.random.default_rng(3)
    adj = adj * np.cumprod(1 + rng.normal(0, 0.02, adj.shape), axis=0)
    w = st.rotation_weights(adj, rf, ("A", "B", "C"), 1, 252)
    cut = 600
    sh = adj.copy()
    sh.iloc[cut + 1:, 2] *= 5
    w2 = st.rotation_weights(sh, rf, ("A", "B", "C"), 1, 252)
    pd.testing.assert_frame_equal(w.iloc[:cut], w2.iloc[:cut])
    per = pd.Series(adj.index.to_period("M"), index=adj.index)
    is_me = np.array(per != per.shift(-1))
    changed = np.array(w.fillna(-1).diff().abs().sum(axis=1) > 0)
    changed[0] = False
    assert is_me[changed].all()


# ---------------------------------------------------------------- study helpers

def test_decade_table_and_calendar_cagr():
    idx = pd.bdate_range("1998-07-01", "2014-12-31")
    r = pd.Series(0.0003, index=idx)
    b = pd.Series(0.0002, index=idx)
    dec = st.decade_table(r, b)
    assert [x["decade"] for x in dec] == ["2000s", "2010s"]          # 1990s has fewer than 480 sessions
    assert all(x["cagr_minus_bench"] > 0 and x["max_dd"] == 0 for x in dec)
    yrs = (idx[-1] - idx[0]).days / 365.25
    assert study.cal_cagr(r) == pytest.approx((1.0003 ** len(r)) ** (1 / yrs) - 1, rel=2e-3)


def test_judge_index_ab():
    idx = pd.bdate_range("2000-01-03", periods=2000)
    rng = np.random.default_rng(5)
    b = pd.Series(rng.normal(0.0003, 0.01, len(idx)), index=idx)
    held = pd.Series(1.0, index=idx)
    same = study.judge_index(b.copy(), b, held)["verdict"]
    assert not same["A"] and not same["B"] and not same["pass"]
    better = study.judge_index(b + 0.0004, b, held)["verdict"]
    assert better["A"] and better["A_bonferroni"]


def test_cash_tbill_accrues_by_calendar_days():
    idx = pd.DatetimeIndex(["2020-01-02", "2020-01-03", "2020-01-06"])
    dtb3 = pd.Series([0.0365, 0.0365], index=pd.DatetimeIndex(["2020-01-01", "2020-01-06"]))
    c = study.cash_tbill(pd.Series(1.0, index=idx), dtb3)
    assert c.iloc[2] == pytest.approx(1.0365 ** (3 / 365) - 1)        # Friday -> Monday earns 3 days


def test_trial_count_and_prereg_block():
    assert study.N_TRIALS == 37
    text = study.LEDGER.read_text()
    assert "<!-- PREREG-BEGIN -->" in text and "<!-- PREREG-END -->" in text


def test_parsers_refuse_future_rows(tmp_path):
    s = [int(pd.Timestamp(d, tz="Europe/Berlin").timestamp()) for d in ("2026-09-30 09:00", "2026-10-01 09:00")]
    df = parse_chart_exchange(_chart(tmp_path, "Europe/Berlin", s, [1.0, 2.0]), "2026-09-30")
    assert df["date"].tolist() == ["2026-09-30"]
    from quant.data.guards import assert_dev_dates
    with pytest.raises(FutureDataError):
        assert_dev_dates(["2026-10-01"], "2026-09-30")
