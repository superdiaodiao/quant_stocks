"""Tests for the bitcoin study (synthetic data only): parsers and day convention, no look-ahead in the rules,
execution lag, costs, calendar -> session compounding, sleeve bands, data helpers."""
from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

from quant.backtest.costs import ibkr_crypto_cost
from quant.data import bitcoin as bd
from quant.data.guards import FutureDataError
from quant.data.sources import crypto
from quant.strategies import bitcoin as bt

P = bt.Params()
DAY = 86400


def _ts(day: str) -> int:
    return int(pd.Timestamp(day, tz="UTC").timestamp())


def synthetic_prices(n=1500, seed=0, start="2012-01-01") -> pd.Series:
    rng = np.random.default_rng(seed)
    r = rng.normal(0.002, 0.04, n)
    r[400:520] = rng.normal(-0.01, 0.05, 120)            # a bear phase so the rules switch
    idx = pd.date_range(start, periods=n, freq="D")
    return pd.Series(100 * np.cumprod(1 + r), index=idx)


# ---------------------------------------------------------------- parsers and the UTC day convention

def test_parsers_use_utc_candle_start_and_truncate():
    cb = json.dumps([[_ts("2020-01-03"), 1, 3, 2, 2.5, 10], [_ts("2020-01-02"), 1, 3, 2, 2.4, 10],
                     [_ts("2020-01-01"), 1, 3, 2, 2.2, 10]]).encode()
    df = crypto.parse_coinbase([cb], "2020-01-02")
    assert list(df["date"]) == ["2020-01-01", "2020-01-02"] and list(df["close"]) == [2.2, 2.4]
    bs = json.dumps({"data": {"ohlc": [{"timestamp": str(_ts("2020-01-01")), "open": "1", "high": "2", "low": "1",
                                        "close": "1.5", "volume": "3"}]}}).encode()
    assert crypto.parse_bitstamp([bs], "2020-01-05")["date"].tolist() == ["2020-01-01"]
    kr = json.dumps({"error": [], "result": {"XXBTZUSD": [[_ts("2020-01-01"), "1", "2", "1", "1.5", "1.4", "3", 5],
                                                          [_ts("2020-01-02"), "1", "2", "1", "1.6", "1.4", "3", 5]],
                                             "last": 0}}).encode()
    assert crypto.parse_kraken(kr, "2020-01-05", today_utc="2020-01-02")["date"].tolist() == ["2020-01-01"]
    y = {"chart": {"result": [{"meta": {"exchangeTimezoneName": "UTC"},
                               "timestamp": [_ts("2020-01-01"), _ts("2020-01-02"), _ts("2020-01-03") + 3600],
                               "indicators": {"quote": [{"open": [1, 1, 1], "high": [2, 2, 2], "low": [1, 1, 1],
                                                         "close": [1.1, 1.2, 1.3], "volume": [1, 1, 1]}]}}]}}
    df = crypto.parse_yahoo_crypto(json.dumps(y).encode(), "2020-01-05")
    assert df["date"].tolist() == ["2020-01-01", "2020-01-02"]          # the running day (off-midnight) dropped


def test_conflicting_duplicate_candles_raise():
    a = json.dumps([[_ts("2020-01-01"), 1, 3, 2, 2.2, 10]]).encode()
    b = json.dumps([[_ts("2020-01-01"), 1, 3, 2, 2.3, 10]]).encode()
    with pytest.raises(ValueError):
        crypto.parse_coinbase([a, b], "2020-01-05")
    assert len(crypto.parse_coinbase([a, a], "2020-01-05")) == 1


def test_primary_close_fills_zero_volume_days():
    idx = pd.date_range("2020-01-01", periods=5, freq="D")
    close = pd.DataFrame({"bitstamp": [1, 2, 2, 4, 5.0], "coinbase": [1, 2, 3.1, 4, 5.0]}, index=idx)
    vol = pd.DataFrame({"bitstamp": [1, 1, 0, 1, 1.0], "coinbase": 1.0}, index=idx)
    p, info = bd.primary_close(bd.Sources({}, close, vol), fill=("coinbase",))
    assert p.iloc[2] == 3.1 and info["filled_days"] == {"2020-01-03": "coinbase"}


def test_on_sessions_and_calendar_rf():
    btc = pd.Series(np.arange(10.0) + 1, index=pd.date_range("2020-01-01", periods=10, freq="D"))
    s = pd.DatetimeIndex(["2020-01-02", "2020-01-03", "2020-01-06"])
    assert bd.on_sessions(btc, s).tolist() == [2.0, 3.0, 6.0]
    with pytest.raises(ValueError):
        bd.on_sessions(btc.drop(pd.Timestamp("2020-01-06")), s)
    rf = pd.Series(0.001, index=s)
    crf = bd.calendar_rf(rf, btc.index)
    assert crf.sum() == pytest.approx(0.003) and crf.loc["2020-01-04"] == 0


def test_to_sessions_compounds_weekends_into_next_session():
    days = pd.date_range("2020-01-01", "2020-01-14", freq="D")             # Wed .. Tue
    r = pd.Series(0.01, index=days)
    sessions = pd.bdate_range("2019-12-31", "2020-01-14")
    out = bt.to_sessions(r, sessions)
    assert out.loc["2020-01-06"] == pytest.approx(1.01 ** 3 - 1)            # Sat + Sun + Mon
    assert out.loc["2020-01-07"] == pytest.approx(0.01)
    assert np.prod(1 + out) == pytest.approx(np.prod(1 + r[r.index <= out.index[-1]]))


# ---------------------------------------------------------------- no look-ahead

@pytest.mark.parametrize("name", [s.name for s in bt.SPECS if s.family == "standalone"])
def test_standalone_targets_unchanged_by_truncation(name):
    p = synthetic_prices()
    full = bt.btc_state(name, p, P)
    for cut in (p.index[700], p.index[1000], p.index[1300]):
        part = bt.btc_state(name, p.loc[:cut], P)
        pd.testing.assert_series_equal(full.loc[:cut].iloc[:-1], part.iloc[:-1], check_freq=False)


@pytest.mark.parametrize("name", [s.name for s in bt.SPECS if s.family == "standalone"])
def test_future_shock_does_not_change_past_targets(name):
    p0 = synthetic_prices()
    p1 = p0.copy()
    p1.iloc[900:] *= np.linspace(0.3, 3.0, len(p1) - 900)
    a, b = bt.btc_state(name, p0, P), bt.btc_state(name, p1, P)
    pd.testing.assert_series_equal(a.iloc[:899], b.iloc[:899])


def test_rule_formulas():
    p = synthetic_prices()
    s = bt.btc_state("BT1a", p, P)
    i = 800
    assert s.iloc[i] == float(p.iloc[i] > p.iloc[i - 199:i + 1].mean())
    assert s.iloc[:199].isna().all() and s.iloc[199:].notna().all()
    m = bt.btc_state("BT2a", p, P)
    me = [d for d in p.index[:-1] if d.month != (d + pd.Timedelta(days=1)).month]
    d0, d3 = me[20], me[17]
    assert m.loc[d0] == float(p[d0] / p[d3] - 1 > 0)
    # BT2 only changes on the day after a month end
    ch = m.ne(m.shift()) & m.notna() & m.shift().notna()
    assert all((d - pd.Timedelta(days=1)).month != d.month or d.day == 1 for d in m.index[ch]) or \
        all(d in set(me) for d in m.index[ch])
    v = bt.btc_state("BT3", p, P)
    assert v.max() <= 1.0 and v.min() > 0
    raw = np.minimum(1, 0.40 / (p.pct_change().rolling(30).std() * math.sqrt(365)))
    steps = v.diff().abs().dropna()
    assert (steps[steps > 0] > P.vol_band).all()
    assert (v - raw).abs().max() <= P.vol_band + 1e-12


def test_banded_keeps_last_accepted():
    t = pd.Series([0.5, 0.55, 0.62, 0.58, 0.2, np.nan, 0.25])
    assert bt.banded(t, 0.10).tolist()[:5] == [0.5, 0.5, 0.62, 0.62, 0.2]


# ---------------------------------------------------------------- engine: lag, costs, bands

def _grid(n=40):
    idx = pd.date_range("2021-01-01", periods=n, freq="D")
    r = pd.DataFrame({"BTC": 0.02, "CASH": 0.0}, index=idx)
    px = pd.DataFrame({"BTC": 100.0, "CASH": np.nan}, index=idx)
    return idx, r, px


def test_target_decided_at_t_earns_from_t_plus_2():
    idx, r, px = _grid()
    w = pd.DataFrame({"BTC": 0.0, "CASH": 1.0}, index=idx)
    w.iloc[10:] = [1.0, 0.0]                                   # decided at row 10
    sim = bt.simulate(w, pd.Series(False, index=idx), r, px, 1, {}, costs=False)
    ret = sim["ret"]
    assert (ret.loc[:idx[11]] == 0).all() and ret.loc[idx[12]] == pytest.approx(0.02)
    assert sim["trades"] == 1


def test_crypto_cost_min_and_cap():
    assert ibkr_crypto_cost(10_000, 0) == pytest.approx(18.0)
    assert ibkr_crypto_cost(500, 0) == pytest.approx(1.75)
    assert ibkr_crypto_cost(100, 0) == pytest.approx(1.0)          # 1% cap
    assert ibkr_crypto_cost(1000, 5e-4) == pytest.approx(1.8 + 0.5)


def test_buy_hold_cost_and_fee():
    idx, r, px = _grid()
    fns = {"BTC": lambda v, p, s, ts: ibkr_crypto_cost(v, 0.0)}
    sim = bt.buy_hold("BTC", r, px, 0, fns, monthly_fee=0.15)
    expect = 10_000 * (1 - 0.0018) * 1.02 ** (len(idx) - 1)
    months = len(set(idx.to_period("M"))) - 1
    assert sim["value"].iloc[-1] < expect and sim["fee_dollars"] == pytest.approx(0.15 * months)


def test_sleeve_band_trades_only_out_of_band():
    sessions = pd.bdate_range("2021-01-01", periods=120)
    btc = np.zeros(120)
    btc[30] = 0.40                                               # BTC jumps once: weight 10% -> ~13.5%
    rets = pd.DataFrame({"QQQ": 0.0, "BTC": btc, "CASH": 0.0}, index=sessions)
    px = pd.DataFrame({"QQQ": 100.0, "BTC": np.nan, "CASH": np.nan}, index=sessions)
    spec = bt.SPEC["BT4d"]
    w, flag = bt.sleeve_targets(spec, sessions)
    assert not flag.any()
    sim = bt.simulate(w, flag, rets, px, 1, {}, band={"BTC": bt.band_width(P, spec.sleeve)}, costs=False)
    assert bt.band_width(P, 0.10) == pytest.approx(0.025)
    assert sim["trades"] == 1
    assert sim["held"]["BTC"].iloc[-1] == pytest.approx(0.10)
    w4, f4 = bt.sleeve_targets(bt.SPEC["BT4b"], sessions)
    assert f4.sum() == len(set(sessions.to_period("M"))) - 1


def test_bt5_trend_moves_btc_part_to_cash():
    sessions = pd.bdate_range("2021-01-01", periods=10)
    on = pd.Series([1, 1, 0, 0, 1, 1, 1, 0, 0, 0.0], index=sessions)
    w, _ = bt.sleeve_targets(bt.SPEC["BT5c"], sessions, on)
    assert (w.sum(axis=1) == 1).all() and w.loc[sessions[2], "CASH"] == pytest.approx(0.2)
    assert w.loc[sessions[0], "BTC"] == pytest.approx(0.2) and (w["QQQ"] == 0.8).all()


# ---------------------------------------------------------------- diagnostics

def test_weekend_test_and_risk_contribution():
    idx = pd.date_range("2020-01-01", periods=700, freq="D")
    rng = np.random.default_rng(1)
    r = pd.Series(rng.normal(0, 0.01, len(idx)), index=idx) + np.where(idx.dayofweek >= 5, 0.01, 0.0)
    out = bt.weekend_test(r)
    assert out["welch_t_weekend_minus_weekday"] > 5 and out["weekend_share_of_days"] == pytest.approx(2 / 7, abs=0.01)
    a = pd.Series(rng.normal(0, 0.02, 500))
    b = pd.Series(rng.normal(0, 0.01, 500))
    port = 0.3 * a + 0.7 * b
    ra = bt.risk_contribution(a, port, pd.Series(0.3, index=a.index))
    rb = bt.risk_contribution(b, port, pd.Series(0.7, index=a.index))
    assert ra + rb == pytest.approx(1.0)


def test_halving_table_and_drawdowns():
    p = pd.Series(np.r_[np.linspace(1, 2, 400), np.linspace(2, 0.5, 300), np.linspace(0.5, 3, 500)],
                  index=pd.date_range("2015-01-01", periods=1200, freq="D"))
    t = bt.halving_table(p, {"2016-01-01": 1, "2010-01-01": 0})
    assert t[0]["ret_p180d"] == pytest.approx(p[pd.Timestamp("2016-06-29")] / p[pd.Timestamp("2016-01-01")] - 1)
    assert t[1]["note"] == "before the data"
    ep = bd.drawdown_episodes(p, -0.5)
    assert len(ep) == 1 and ep[0]["depth"] == pytest.approx(-0.75, abs=0.01)


def test_date_guard_in_parsers():
    cb = json.dumps([[_ts("2020-01-03"), 1, 3, 2, 2.5, 10]]).encode()
    assert crypto.parse_coinbase([cb], "2020-01-02").empty
    with pytest.raises(FutureDataError):
        from quant.data.guards import assert_dev_dates
        assert_dev_dates(["2020-01-03"], "2020-01-02")
