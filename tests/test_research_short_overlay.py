"""Tests for scripts/research_short_overlay.py on synthetic data: signal construction (mispricing composite, borrow
tiers, buffered loser selection), IBKR interest rules, short P&L with dividends and delisting, whole-share rounding,
Reg T scaling, forced liquidation, terminal-value variants and the pass criteria."""
import numpy as np
import pandas as pd
import pytest

from scripts import research_short_overlay as so


# ------------------------------------------------------------------ helpers

def _market(n_days=60, prices=None, qqq_ret=0.0, effr=0.0, booked=None):
    sessions = pd.bdate_range("2020-01-01", periods=n_days)
    prices = prices if prices is not None else {"A": 100.0, "B": 50.0, "C": 20.0}
    sids = sorted(prices)
    close = np.array([[prices[s]] * n_days for s in sids], float).T
    idx = close / close[0]
    return so.Market(sessions=sessions, sids=sids, col={s: i for i, s in enumerate(sids)}, idx=idx.copy(),
                     close=close.copy(), booked=booked or {}, qqq_tr=np.full(n_days, qqq_ret),
                     qqq_px=np.full(n_days, 300.0), effr=np.full(n_days, effr),
                     idx_variants={"base": idx.copy()})


def _sched(mk, order, info=None, at=(0,)):
    info = info or {s: (10.0, 0.0) for s in order}
    return [(t, mk.sessions[t], list(order), info) for t in at]


# ------------------------------------------------------------------ signals

def test_misp_score_direction_and_minimum_parts():
    s = pd.Timestamp("2020-01-31")
    p = pd.DataFrame({"s": [s] * 3, "accruals": [0.1, 0.0, -0.1], "share_iss": [0.1, 0.0, -0.1],
                      "asset_g": [0.5, 0.1, 0.0], "noa": [1.0, 0.5, 0.2], "gpa": [0.0, 0.2, 0.4],
                      "roa": [-0.1, 0.05, 0.1], "mom_12_1": [-0.3, 0.0, 0.3], "altman_z": [1.0, 3.0, 5.0]})
    m = so.misp_score(p)
    assert m.iloc[0] < m.iloc[1] < m.iloc[2]          # first row is "bad" on every anomaly -> most overpriced
    p2 = p.copy()
    for c in ["accruals", "share_iss", "asset_g", "noa", "gpa"]:
        p2.loc[0, c] = np.nan
    m2 = so.misp_score(p2)
    assert np.isnan(m2.iloc[0]) and np.isfinite(m2.iloc[1])   # only 3 of 8 available -> no score


def test_borrow_tiers():
    s = pd.Timestamp("2020-01-31")
    n = 200
    p = pd.DataFrame({"s": [s] * n, "mcap": np.arange(n, 0, -1) * 1e9, "vol_3m": np.linspace(0.01, 0.05, n)})
    p.loc[0, "vol_3m"] = 0.10                            # biggest company is the most volatile: still 0.5%
    r = so.borrow_rates(p)
    assert r.iloc[0] == so.BORROW_BIG
    assert (r.iloc[:100] == so.BORROW_BIG).all()
    assert r.iloc[-1] == so.BORROW_HTB                   # small and most volatile
    assert r.iloc[120] == so.BORROW_REST


def test_loser_orders_and_buffer():
    s1, s2 = pd.Timestamp("2020-01-31"), pd.Timestamp("2020-02-28")
    sc = pd.DataFrame({"s": [s1] * 6 + [s2] * 6, "security_id": list("ABCDEF") * 2,
                       "x": [1, 2, 3, 4, 5, 6, 3, 4, 1, 2, 5, 6]})
    o = so.loser_orders(sc, "x", 2)
    assert o[s1] == ["A", "B", "C", "D"]                  # worst first, 2N long
    assert o[s2] == ["C", "D", "A", "B"]
    # A and B were held: both still in the worst 4 -> kept although C, D are now worse
    assert so.select_shorts(o[s2], {"A", "B"}, 2) == ["A", "B"]
    # held name E dropped out of the worst 2N -> replaced from the worst down
    assert so.select_shorts(o[s2], {"A", "E"}, 2) == ["A", "C"]
    # names failing ok() are skipped for the next one
    assert so.select_shorts(o[s1], set(), 2, ok=lambda sid: sid != "A") == ["B", "C"]


# ------------------------------------------------------------------ interest rules

def test_interest_rules_small_and_large_accounts():
    # overlay at 10k: own cash = 0 -> nothing charged, nothing earned (short proceeds earn 0 below 100k NAV)
    d, c = so.daily_interest(cash=2000.0, short_mv=2000.0, nav=10_000.0, effr=0.05, days=360)
    assert d == 0 and c == 0
    # market neutral: own cash -2000 -> loan at EFFR + 1.5%
    d, c = so.daily_interest(cash=0.0, short_mv=2000.0, nav=10_000.0, effr=0.05, days=360)
    assert d == pytest.approx(2000 * 0.065) and c == 0
    # large account: short proceeds earn EFFR - 0.5%; free cash above 10k too
    d, c = so.daily_interest(cash=70_000.0, short_mv=50_000.0, nav=200_000.0, effr=0.05, days=360)
    assert d == 0 and c == pytest.approx(10_000 * 0.045 + 50_000 * 0.045)
    # 50k NAV: free cash credit scaled by half, short proceeds nothing
    d, c = so.daily_interest(cash=30_000.0, short_mv=0.0, nav=50_000.0, effr=0.05, days=360)
    assert c == pytest.approx(20_000 * 0.045 * 0.5)


def test_short_maintenance_tiers():
    assert so.short_maint(100.0, 10, 1000.0) == pytest.approx(300.0)
    assert so.short_maint(10.0, 10, 100.0) == pytest.approx(50.0)
    assert so.short_maint(3.0, 10, 30.0) == pytest.approx(30.0)
    assert so.short_maint(1.0, 10, 10.0) == pytest.approx(25.0)


# ------------------------------------------------------------------ simulation

def test_flat_prices_ideal_overlay_keeps_nav():
    mk = _market()
    cfg = so.Cfg(n=2, k=0.2, mode="OV", ideal=True)
    res = so.simulate(mk, _sched(mk, ["A", "B", "C"]), cfg, 0, 59)
    nav = res["nav"]
    assert nav["nav"].iloc[-1] == pytest.approx(10_000.0)
    assert nav["short"].iloc[0] == pytest.approx(2000.0)
    assert nav["qqq"].iloc[0] == pytest.approx(10_000.0)


def test_short_gains_when_loser_falls_and_pays_dividends():
    mk = _market()
    a = mk.col["A"]
    mk.close[30:, a] = 50.0                           # A halves
    mk.idx_variants["base"][30:, a] = 0.5
    mk.idx = mk.idx_variants["base"]
    cfg = so.Cfg(n=1, k=0.2, mode="OV", ideal=True)
    res = so.simulate(mk, _sched(mk, ["A"]), cfg, 0, 59)
    assert res["nav"]["nav"].iloc[-1] == pytest.approx(11_000.0)     # +50% of 2,000 short
    # a dividend (total-return index up 2%, price flat) costs the short
    mk2 = _market()
    mk2.idx_variants["base"][30:, a] = 1.02
    mk2.idx = mk2.idx_variants["base"]
    res2 = so.simulate(mk2, _sched(mk2, ["A"]), cfg, 0, 59)
    assert res2["nav"]["nav"].iloc[-1] == pytest.approx(10_000.0 - 40.0)


def test_delisted_short_covers_at_terminal_value_and_variants():
    mk = _market(booked={0: 30})
    a = mk.col["A"]
    base = mk.idx_variants["base"]
    base[30:, a] = 0.45                               # booked -55% (D5)
    mk.idx = base
    cfg = so.Cfg(n=1, k=0.2, mode="OV", ideal=True)
    res = so.simulate(mk, _sched(mk, ["A"]), cfg, 0, 59)
    assert res["acc"]["delisted_covered"] == 1
    assert res["nav"]["n_short"].iloc[-1] == 0
    assert res["nav"]["nav"].iloc[-1] == pytest.approx(10_000 + 2000 * 0.55)
    # terminal variants on a DataFrame index
    sessions = mk.sessions
    idx = pd.DataFrame(base.copy(), index=sessions, columns=mk.sids)
    ev = pd.DataFrame({"security_id": ["A"], "booked_on": [sessions[30]], "status": ["awaiting_d5"],
                       "terminal_return": [-0.55]})
    m100 = so.terminal_variant(idx, ev, "d5_minus100")
    assert m100["A"].iloc[30] == pytest.approx(0.0) and m100["A"].iloc[29] == pytest.approx(1.0)
    z = so.terminal_variant(idx, ev, "delist_0pct")
    assert z["A"].iloc[-1] == pytest.approx(1.0)
    ev2 = ev.assign(status="computed")
    assert so.terminal_variant(idx, ev2, "delist_0pct")["A"].iloc[-1] == pytest.approx(0.45)


def test_whole_share_rounding_and_skip():
    # 10k, N=2, k=0.2 -> $1,000 per name; A at $3,000 rounds to 0 shares and is skipped for C
    mk = _market(prices={"A": 3000.0, "B": 50.0, "C": 20.0})
    cfg = so.Cfg(n=2, k=0.2, mode="OV")
    res = so.simulate(mk, _sched(mk, ["A", "B", "C"]), cfg, 0, 5)
    assert res["acc"]["zero_share_skips"] >= 1
    assert res["nav"]["n_short"].iloc[0] == 2
    assert res["nav"]["short"].iloc[0] == pytest.approx(20 * 50 + 50 * 20)
    assert res["acc"]["orders"] == 3                  # two short sales + QQQ buy
    assert res["nav"]["nav"].iloc[0] < 10_000.0       # costs paid


def test_market_neutral_regt_scaling_and_margin_interest():
    mk = _market(effr=0.03)
    cfg = so.Cfg(n=2, k=0.5, mode="MN", ideal=False, account=1_000_000.0)
    res = so.simulate(mk, _sched(mk, ["B", "C"], info={"B": (10.0, 0.0), "C": (10.0, 0.0)}), cfg, 0, 59)
    r0 = res["nav"].iloc[0]
    # requirement 0.5 * (QQQ + short) capped at 0.95 NAV -> k scaled to 0.45
    assert r0["short"] / r0["nav"] == pytest.approx(0.45, abs=0.002)
    assert r0["qqq"] / r0["nav"] == pytest.approx(1.45, abs=0.002)
    assert res["acc"]["scaled_rebalances"] == 1
    assert res["acc"]["margin_int"] > 0
    # OV at k = 0.5 is not scaled
    cfg2 = so.Cfg(n=2, k=0.5, mode="OV", account=1_000_000.0)
    res2 = so.simulate(mk, _sched(mk, ["B", "C"]), cfg2, 0, 5)
    assert res2["acc"]["scaled_rebalances"] == 0
    assert res2["acc"]["margin_int"] < 1.0           # only the QQQ order cost is ever borrowed


def test_squeeze_triggers_forced_liquidation():
    mk = _market(prices={"A": 100.0, "B": 50.0, "C": 20.0})
    a = mk.col["A"]
    mk.close[10:, a] = 2000.0                         # 20x squeeze
    mk.idx_variants["base"][10:, a] = 20.0
    mk.idx = mk.idx_variants["base"]
    cfg = so.Cfg(n=1, k=0.5, mode="OV", account=100_000.0)
    res = so.simulate(mk, _sched(mk, ["A"], info={"A": (10.0, 0.0)}), cfg, 0, 30)
    assert res["acc"]["liquidations"] == 1
    assert res["nav"]["n_short"].iloc[11] == 0
    assert res["nav"]["nav"].iloc[-1] < 0.1 * 100_000  # the squeeze wiped out most of the account


def test_rebalance_band_keeps_continuing_short():
    mk = _market()
    sched = _sched(mk, ["A"], at=(0, 20, 40))
    cfg = so.Cfg(n=1, k=0.2, mode="OV")
    res = so.simulate(mk, sched, cfg, 0, 59)
    assert res["acc"]["orders"] == 2                  # one short sale, one QQQ buy; later months inside the bands


# ------------------------------------------------------------------ evaluation

def test_month_returns_and_criteria():
    d = pd.bdate_range("2020-01-01", "2020-04-30")
    lvl = pd.Series(np.linspace(100, 130, len(d)), index=d)
    m = so.month_returns(lvl)
    assert len(m) == 4 and m.iloc[0] == pytest.approx(lvl[lvl.index.month == 1].iloc[-1] / 100 - 1)
    base = {"cagr": 0.20, "oneq_cagr": 0.15, "t_ex_oneq": 2.1, "dd_shallower_pp": 0.0}
    assert so.fold_criteria(base)["A"]
    assert not so.fold_criteria({**base, "t_ex_oneq": 1.9})["pass"]
    assert so.fold_criteria({"cagr": 0.13, "oneq_cagr": 0.15, "t_ex_oneq": -1.0, "dd_shallower_pp": 12.0})["B"]
    assert not so.fold_criteria({"cagr": 0.11, "oneq_cagr": 0.15, "t_ex_oneq": -1.0, "dd_shallower_pp": 12.0})["pass"]


def test_bonferroni_hurdle():
    t = so.bonferroni_t(170, n=32)
    assert 2.9 < t < 3.1
    assert so.bonferroni_t(170, n=1) == pytest.approx(1.654, abs=0.01)


def test_ols_alpha_recovers_known_values():
    rng = np.random.default_rng(1)
    x = pd.Series(rng.normal(0.01, 0.05, 400))
    y = -0.005 + 1.3 * x + pd.Series(rng.normal(0, 0.001, 400))
    a = so.ols_alpha(y, x)
    assert a["beta"] == pytest.approx(1.3, abs=0.01)
    assert a["alpha_ann"] == pytest.approx(-0.06, abs=0.003)
    assert a["alpha_t"] < -20
