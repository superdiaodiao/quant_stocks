"""Offline tests for scripts/research_indicators.py (synthetic data only; no cache files are read)."""
import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from scripts import research_indicators as ri

talib = pytest.importorskip("talib")


def _walk(n=600, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2001-01-01", periods=n)
    c = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.015, n))), index=idx)
    h = c * (1 + rng.uniform(0, 0.02, n))
    lo = c * (1 - rng.uniform(0, 0.02, n))
    return {"open": c, "high": h, "low": lo, "close": c, "vix": pd.Series(rng.uniform(10, 40, n), index=idx),
            "has_hl": True}


# ---------------------------------------------------------------- indicator math

def test_known_values():
    s = pd.Series([1.0, 2, 3, 4, 5, 6])
    assert ri.sma(s, 3).tolist()[2:] == [2.0, 3.0, 4.0, 5.0]
    assert np.isnan(ri.sma(s, 3).iloc[1])
    up = pd.Series(np.arange(1.0, 40.0))
    assert ri.rsi(up, 14).dropna().eq(100).all()                 # only gains -> RSI 100
    alt = pd.Series([10.0, 11.0] * 40)
    assert abs(ri.rsi(alt, 14).iloc[-1] - 50) < 5                 # equal gains and losses -> near 50
    # Donchian: channel from the n sessions before t (today's own high is excluded)
    h = pd.Series([1.0, 2, 3, 4, 5])
    up_, lo_ = ri.donchian(h, h, 3)
    assert up_.tolist()[3:] == [3.0, 4.0] and lo_.tolist()[3:] == [1.0, 2.0]
    # Bollinger: mean +/- k sample std
    c = pd.Series([1.0, 2.0, 3.0])
    ma, u, low = ri.bollinger(c, 3, 2.0)
    assert ma.iloc[-1] == 2.0 and u.iloc[-1] == pytest.approx(4.0) and low.iloc[-1] == pytest.approx(0.0)


def test_kdj_by_hand():
    h = pd.Series([10.0, 12.0, 11.0])
    lo = pd.Series([8.0, 9.0, 9.0])
    c = pd.Series([9.0, 11.0, 10.0])
    k, d, j = ri.kdj(h, lo, c, n=2)
    rsv2 = (11 - 8) / (12 - 8) * 100          # 75 (first valid RSV, starts the EMA)
    rsv3 = (10 - 9) / (12 - 9) * 100          # 33.33
    assert k.iloc[1] == pytest.approx(rsv2)
    assert k.iloc[2] == pytest.approx(rsv2 * 2 / 3 + rsv3 / 3)
    assert j.iloc[2] == pytest.approx(3 * k.iloc[2] - 2 * d.iloc[2])


def test_against_talib_after_warmup():
    b = _walk()
    c, h, lo = b["close"], b["high"], b["low"]
    tail = slice(300, None)
    np.testing.assert_allclose(ri.rsi(c, 14).iloc[tail], talib.RSI(c, 14).iloc[tail], atol=1e-6)
    dif, dea, hist = ri.macd(c)
    m, s, hh = talib.MACD(c, 12, 26, 9)
    np.testing.assert_allclose(dif.iloc[tail], m.iloc[tail], atol=1e-6)
    np.testing.assert_allclose(dea.iloc[tail], s.iloc[tail], atol=1e-6)
    np.testing.assert_allclose(ri.atr(h, lo, c, 14).iloc[tail], talib.ATR(h, lo, c, 14).iloc[tail], atol=1e-6)
    np.testing.assert_allclose(ri.ema(c, 20).iloc[tail], talib.EMA(c, 20).iloc[tail], atol=1e-6)


def test_close_only_true_range():
    c = pd.Series([10.0, 11.0, 9.0])
    assert ri.true_range(c, c, c).tolist()[1:] == [1.0, 2.0]


# ---------------------------------------------------------------- no look-ahead

@pytest.mark.parametrize("rule", ri.RULES, ids=[r.code for r in ri.RULES])
def test_rules_have_no_lookahead(rule):
    b = _walk()
    cut = 450
    b2 = {k: (v.copy() if isinstance(v, pd.Series) else v) for k, v in b.items()}
    for k in ("open", "high", "low", "close"):
        b2[k].iloc[cut:] = b2[k].iloc[cut:] * 3.0            # change the future violently
    b2["vix"].iloc[cut:] = 99.0
    s1 = ri.state_machine(*rule.fn(b), b["close"], rule.take_profit, rule.buy_wins)
    s2 = ri.state_machine(*rule.fn(b2), b2["close"], rule.take_profit, rule.buy_wins)
    assert s1.iloc[:cut].equals(s2.iloc[:cut])


def test_rules_fire_on_synthetic_data():
    b = _walk(1500)
    for r in ri.RULES:
        st = ri.state_machine(*r.fn(b), b["close"], r.take_profit, r.buy_wins)
        assert 0 < st.mean() < 1, r.code


def test_owner_original_donchian_never_fires():
    """The 2727a062d version put today's high in the channel: close > max(high incl. today) is impossible."""
    b = _walk()
    orig_upper = b["high"].rolling(20).max()
    assert not (b["close"] > orig_upper).any()
    buy, _ = ri.r_donchian(b)
    assert buy.any()


# ---------------------------------------------------------------- state machine and execution

def test_state_machine_and_take_profit():
    buy = pd.Series([0, 1, 0, 0, 0, 0, 0], dtype=bool)
    sell = pd.Series([0, 0, 0, 0, 1, 0, 0], dtype=bool)
    assert ri.state_machine(buy, sell).tolist() == [0, 1, 1, 1, 0, 0, 0]
    # take profit: buy decided at t=1, filled at close t=2 (100); t=4 close 116 >= 115 -> sell decision
    close = pd.Series([100.0, 100, 100, 110, 116, 120, 120])
    always = pd.Series([1] * 7, dtype=bool)
    never = pd.Series([0] * 7, dtype=bool)
    st = ri.state_machine(always, never, close, take_profit=0.15)
    assert st.tolist() == [1, 1, 1, 1, 0, 1, 1]               # sold at t=4, buys again at t=5 (owner's position.py)
    # buy_wins: a long position is kept when buy and sell fire together
    b2 = pd.Series([1, 1, 0], dtype=bool)
    s2 = pd.Series([0, 1, 1], dtype=bool)
    assert ri.state_machine(b2, s2, buy_wins=True).tolist() == [1, 1, 0]
    assert ri.state_machine(b2, s2).tolist() == [1, 0, 0]


def test_index_sim_executes_next_close():
    idx = pd.bdate_range("2010-01-01", periods=8)
    r1 = pd.Series([0, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07], index=idx)
    data = SimpleNamespace(r1={"COMP": r1}, rf=pd.Series(0.0, index=idx),
                           price={"COMP": pd.Series(50.0, index=idx)})
    target = pd.Series([0, 0, 1, 1, 1, 1, 1, 1.0], index=idx)        # decided at the close of day 2
    sim = ri.index_sim(target, data, "COMP", str(idx[0].date()), str(idx[-1].date()))
    e = sim["exposure_held"]
    assert e.iloc[:3].eq(0).all()          # returns of days 1..3 are earned in cash (traded at the close of day 3)
    assert e.iloc[3:].eq(1).all()          # day 4 onward in the index
    short = ri.index_sim(target, data, "COMP", str(idx[0].date()), "2010-01-05")
    assert short["ret"].index.max() <= pd.Timestamp("2010-01-05")      # never simulates past ``end``


def test_stock_sim_next_close_fill():
    idx = pd.bdate_range("2020-01-01", periods=10)
    cols = ["A", "B"]
    I = pd.DataFrame({"A": np.linspace(1, 1.9, 10), "B": 1.0}, index=idx)
    data = SimpleNamespace(sessions=idx, spec={"perf_start": str(idx[0].date()), "effective_end": str(idx[-1].date())},
                           perf_idx=I, close=I * 20, last_row=pd.Series([pd.NaT, pd.NaT], index=cols))
    buy = pd.DataFrame(False, index=idx, columns=cols)
    sell = pd.DataFrame(False, index=idx, columns=cols)
    buy.loc[idx[2], "A"] = True
    sell.loc[idx[5], "A"] = True
    elig = {idx[0]: ["A", "B"]}
    res = ri.stock_sim(ri.RULE_BY_CODE["T2"], data, buy, sell, elig, {}, account=10_000.0, k=5)
    tr = res["trades"].iloc[0]
    assert tr["entry"] == idx[3] and tr["exit"] == idx[6]       # decided at t, filled at t+1
    assert res["exposure"].iloc[:3].eq(0).all() and res["exposure"].iloc[3] > 0.15


def test_holding_rules_enter_only_on_the_turn():
    idx = pd.bdate_range("2020-01-01", periods=60)
    c = pd.DataFrame({"A": np.r_[np.linspace(10, 5, 30), np.linspace(5, 15, 30)]}, index=idx)
    bars = {"open": c, "high": c, "low": c, "close": c, "vix": None, "has_hl": False}
    buy, _ = ri.stock_signals(ri.RULE_BY_CODE["O1"], bars)
    assert buy["A"].sum() == 1


# ---------------------------------------------------------------- date guard and one-shot gating

def test_parse_ohlc_truncates_and_guards(tmp_path):
    ts = [int(pd.Timestamp(d + " 14:30", tz="UTC").timestamp()) for d in ("2014-12-30", "2014-12-31", "2015-01-02")]
    j = {"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [
        {"open": [1, 2, 3], "high": [1, 2, 3], "low": [1, 2, 3], "close": [1, 2, 3]}]}}]}}
    p = tmp_path / "x.json"
    p.write_text(json.dumps(j))
    df = ri.parse_ohlc(p, "2014-12-31")
    assert str(df.index.max().date()) == "2014-12-31" and len(df) == 2
    with pytest.raises(ri.FutureDataError):
        ri.qt.assert_dev_dates(["2015-01-02"], "2014-12-31")


def test_oneshot_refuses_without_frozen_file(tmp_path, monkeypatch):
    monkeypatch.setattr(ri, "FROZEN", tmp_path / "missing.json")
    with pytest.raises(SystemExit):
        ri.run_oneshot_index()
    with pytest.raises(SystemExit):
        ri.run_oneshot_stock("test1")


def test_criteria():
    assert ri.criteria(0.12, -0.30, 0.10, -0.50, 2.1)["A"]
    assert not ri.criteria(0.12, -0.30, 0.10, -0.50, 1.9)["A"]
    assert ri.criteria(0.08, -0.35, 0.10, -0.50, 0.0)["B"]
    assert not ri.criteria(0.06, -0.35, 0.10, -0.50, 0.0)["B"]
    assert not ri.criteria(0.09, -0.45, 0.10, -0.50, 0.0)["B"]
