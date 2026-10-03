"""Offline tests for scripts/research_livermore.py (synthetic data only; no cache files are read)."""
import numpy as np
import pandas as pd
import pytest

from scripts import research_livermore as lv


# ---------------------------------------------------------------- date guard

def test_windows_and_guard():
    assert lv.WINDOWS["dev"]["perf_start"] == "2017-01-01" and lv.WINDOWS["dev"]["perf_end"] == "2022-12-31"
    assert lv.WINDOWS["test2"]["judged_from"] == "2014-01-01"
    with pytest.raises(lv.DateGuardError):
        lv.cs.assert_window(["2022-12-30", "2023-01-03"], "2017-01-01", "2022-12-31")
    frame = pd.DataFrame({"date": ["2015-09-30", "2015-10-01", "2022-12-30", "2023-01-03"]})
    out = lv.cs.truncate(frame, "date", "2015-10-01", "2022-12-31")
    assert out["date"].tolist() == ["2015-10-01", "2022-12-30"]


def test_oneshot_refuses_without_frozen_rule(tmp_path, monkeypatch):
    monkeypatch.setattr(lv, "FROZEN", tmp_path / "missing.json")
    with pytest.raises(SystemExit):
        lv.run_oneshot("test1")


# ---------------------------------------------------------------- pivots

def _frame(values, name="A"):
    idx = pd.bdate_range("2020-01-01", periods=len(values))
    return pd.DataFrame({name: np.asarray(values, float)}, index=idx)


def test_new_high_breakout_uses_prior_closes_only():
    vals = list(np.linspace(10, 12, 260)) + [11.5, 12.5, 12.4]
    c = _frame(vals)
    v = _frame(np.ones(len(vals)))
    sig = lv.breakout_frame(c, v, "high252", None)["A"]
    assert bool(sig.iloc[259])                # each rising close is a new high vs the prior 252
    assert not bool(sig.iloc[260])            # 11.5 is below the prior high 12.0
    assert bool(sig.iloc[261])                # 12.5 clears 12.0
    assert not bool(sig.iloc[262])            # 12.4 < 12.5: today's own close is not in the pivot
    # no look-ahead: changing the future does not change past signals
    vals2 = vals[:261] + [100.0, 1.0]
    sig2 = lv.breakout_frame(_frame(vals2), _frame(np.ones(len(vals2))), "high252", None)["A"]
    assert sig2.iloc[:261].equals(sig.iloc[:261])


def test_base_breakout_requires_tight_range_and_volume():
    base = [10.0, 10.5, 10.2, 10.8, 10.4] * 6      # 30 sessions inside 10.0..10.8 (8% range)
    c = _frame(base + [11.0])
    vol = _frame([100.0] * 30 + [300.0])
    assert bool(lv.breakout_frame(c, vol, "base25_t10", None)["A"].iloc[-1])
    assert not bool(lv.breakout_frame(c, vol, "base25_t05", None)["A"].iloc[-1])     # range too wide for 5%
    assert not bool(lv.breakout_frame(_frame(base + [10.7]), vol, "base25_t10", None)["A"].iloc[-1])
    # volume: needs 50 prior sessions; build a longer base
    c2 = _frame(base * 2 + [11.0])
    v_hi = _frame([100.0] * 60 + [160.0])
    v_lo = _frame([100.0] * 60 + [120.0])
    assert bool(lv.breakout_frame(c2, v_hi, "base25_t10", 1.5)["A"].iloc[-1])
    assert not bool(lv.breakout_frame(c2, v_lo, "base25_t10", 1.5)["A"].iloc[-1])


def test_trail_frames():
    c = _frame([10.0] * 20 + [12.0, 9.0])
    sma = lv.trail_frame(c, "sma20")["A"]
    assert not bool(sma.iloc[20]) and bool(sma.iloc[21])
    low = lv.trail_frame(_frame([10, 11, 12, 11.5, 10.5]), "low3")["A"]
    assert not bool(low.iloc[3]) and bool(low.iloc[4])        # 10.5 < min(11, 12, 11.5)


# ---------------------------------------------------------------- engine on a synthetic window

def _window(paths: dict, qqq=None, start="2017-01-02", n=40):
    """A WinData with daily sessions, one leader week per Friday, stocks priced by ``paths``."""
    sessions = pd.bdate_range("2016-12-01", periods=n + 22)
    perf = sessions[sessions >= pd.Timestamp(start)]
    cols = list(paths)
    close = pd.DataFrame({s: np.r_[np.full(len(sessions) - len(perf), p[0]), p[:len(perf)]] for s, p in
                          paths.items()}, index=sessions)
    idx = close / close.iloc[0]
    perf_idx = idx.copy()
    perf_idx.loc[perf_idx.index < perf[0]] = np.nan
    q = pd.Series(100.0, index=sessions) if qqq is None else qqq
    spec = {"perf_start": str(perf[0].date()), "perf_end": str(sessions[-1].date()),
            "effective_end": str(sessions[-1].date()), "price_start": str(sessions[0].date()),
            "universe_start": str(sessions[0].date()), "judged_from": str(perf[0].date())}
    data = lv.WinData(name="syn", spec=spec, sessions=sessions, universe=pd.DataFrame(), sig_idx=idx,
                      perf_idx=perf_idx, close=close, close_adj=close, vol_adj=close * 0 + 1,
                      last_row=pd.Series(sessions[-1], index=cols), qqq_close=q, qqq_perf_idx=q / q.iloc[0],
                      terminal_events=pd.DataFrame(), guard={})
    fridays = [d for d in sessions if d.weekday() == 4]
    leaders = pd.DataFrame([{"week_end": f, "security_id": s, "rs": 1.0 - j * 0.1} for f in fridays
                            for j, s in enumerate(cols)])
    return data, perf


def _cfg(**kw):
    base = dict(k=2, tranches=(0.5, 0.5), add_step=0.05, stop=0.08, trail="pct50", market="none", idle="cash",
                spread_mult=0.0)
    base.update(kw)
    return lv.Config(**base)


def _run(cfg, data, brk_days, trail_days=None, m_on=None):
    cols = list(data.close.columns)
    brk = pd.DataFrame(False, index=data.sessions, columns=cols)
    for sid, days in brk_days.items():
        brk.loc[days, sid] = True
    tr = None
    if trail_days is not None:
        tr = pd.DataFrame(False, index=data.sessions, columns=cols)
        for sid, days in trail_days.items():
            tr.loc[days, sid] = True
    fridays = [d for d in data.sessions if d.weekday() == 4]
    leaders = pd.DataFrame([{"week_end": f, "security_id": s, "rs": 1.0 - j * 0.1} for f in fridays
                            for j, s in enumerate(cols)])
    m = pd.Series(True, index=data.sessions) if m_on is None else m_on
    return lv.simulate(cfg, data, leaders, brk, tr, m, {})


def test_entry_fills_at_next_close_and_pyramids_only_after_profit():
    up = np.r_[np.full(5, 10.0), np.linspace(10, 12, 35)]
    data, perf = _window({"A": up})
    d_sig = perf[6]
    res = _run(_cfg(), data, {"A": [d_sig]})
    o = res["orders"]
    buy = o[o["side"] == "buy"].iloc[0]
    assert buy["date"] == perf[7]                     # signal at d's close -> filled at d+1's close
    assert abs(buy["value"] - 10_000 / 2 * 0.5) < 1e-6
    adds = o[o["side"] == "add"]
    assert len(adds) == 1
    entry_px = data.close.loc[perf[7], "A"]
    add_day = adds.iloc[0]["date"]
    i_add = list(perf).index(add_day)
    signal_close = data.close.loc[perf[i_add - 1], "A"]
    assert signal_close / entry_px - 1 >= 0.05        # decided on a close >= +5% over entry, filled next close
    assert data.close.loc[perf[i_add - 2], "A"] / entry_px - 1 < 0.05   # not one day earlier


def test_never_adds_to_a_loser_and_stop_sells_next_close():
    down = np.r_[np.full(8, 10.0), np.linspace(10, 8.5, 32)]
    data, perf = _window({"A": down})
    res = _run(_cfg(), data, {"A": [perf[6]]})
    o = res["orders"]
    assert (o["side"] == "add").sum() == 0
    sell = o[o["side"] == "sell"].iloc[0]
    assert sell["reason"] == "stop"
    entry_px = data.close.loc[perf[7], "A"]
    i_sell = list(perf).index(sell["date"])
    assert data.close.loc[perf[i_sell - 1], "A"] / entry_px - 1 <= -0.08     # trigger on the prior close
    assert data.close.loc[perf[i_sell - 2], "A"] / entry_px - 1 > -0.08


def test_trailing_exit_executes_at_next_close():
    flat = np.r_[np.full(5, 10.0), np.linspace(10, 10.2, 35)]
    data, perf = _window({"A": flat})
    res = _run(_cfg(trail="sma50"), data, {"A": [perf[3]]}, trail_days={"A": [perf[15]]})
    sell = res["orders"][res["orders"]["side"] == "sell"].iloc[0]
    assert sell["reason"] == "trail" and sell["date"] == perf[16]


def test_market_off_blocks_entries_and_exits():
    flat = np.full(40, 10.0)
    data, perf = _window({"A": flat, "B": flat})
    m = pd.Series(True, index=data.sessions)
    m.loc[perf[10]:] = False
    res = _run(_cfg(market="sma200", m_exit=True), data, {"A": [perf[3]], "B": [perf[12]]}, m_on=m)
    o = res["orders"]
    assert set(o.loc[o["side"] == "buy", "sid"]) == {"A"}            # B's breakout came while the market was off
    sell = o[o["side"] == "sell"].iloc[0]
    assert sell["reason"] == "market_off" and sell["date"] == perf[11]


def test_max_names_and_no_look_ahead_in_engine():
    flat = np.full(40, 10.0)
    data, perf = _window({"A": flat, "B": flat, "C": flat})
    res = _run(_cfg(k=2), data, {"A": [perf[3]], "B": [perf[3]], "C": [perf[3]]})
    assert set(res["orders"]["sid"]) == {"A", "B"}                  # ranked by RS, capped at K
    # changing prices after day 20 does not change NAV up to day 20
    d2, _ = _window({"A": np.r_[flat[:21], np.full(19, 30.0)], "B": flat, "C": flat})
    res2 = _run(_cfg(k=2), d2, {"A": [perf[3]], "B": [perf[3]], "C": [perf[3]]})
    assert np.allclose(res["nav"].iloc[:21], res2["nav"].iloc[:21])


def test_config_names_unique_in_grid():
    g = lv.build_grid()
    assert len({c.name for _, c in g}) == len(g)
    assert g[0][0] == "baseline"
