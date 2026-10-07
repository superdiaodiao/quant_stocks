"""Synthetic tests for scripts/research_t_grid.py (no vendor data needed)."""
import math

import numpy as np
import pandas as pd
import pytest

from scripts import research_intraday_t as it
from scripts import research_selective_t as st
from scripts import research_t_grid as g
from scripts.research_reversal_dev import order_cost

AX = [n for n, _ in g.AXES]


def code(**kw) -> dict:
    """One configuration (codes per axis); defaults: D20 q1 S SMA H20 f=1/3 res25 ALL."""
    d = {"measure": 1, "level": 0, "direction": 0, "exit": 0, "hold": 2, "frac": 1, "reserve": 2, "regime": 0}
    d.update(kw)
    return d


def codes_of(*cfgs) -> dict:
    return {k: np.array([c[k] for c in cfgs], int) for k in AX}


def make_rows(o, h, lo, c, sig_hi=None, sig_lo=None, sma=None, sigma=0.02, split=None, div=None, up=None,
              opp_s=None, opp_b=None, start=1, hs=2.0, oneq=None, sessions=None):
    """One account; active from ``start`` (base bought at the close of start-1). Signals given for combo index 4
    (D20, level q1) unless full arrays are passed."""
    o, h, lo, c = (np.asarray(x, float) for x in (o, h, lo, c))
    T = len(c)
    sessions = pd.bdate_range("2020-01-01", periods=T) if sessions is None else sessions
    split = np.ones(T) if split is None else np.asarray(split, float)
    div = np.zeros(T) if div is None else np.asarray(div, float)

    def full_sig(x, n):
        a = np.zeros((1, n, T), bool)
        if x is not None:
            x = np.asarray(x, bool)
            if x.ndim == 1:
                a[0, 4 if n == 32 else 1] = x
            else:
                a[0] = x
        return a
    smap = np.full((1, 3, T), np.nan)
    if sma is not None:
        smap[0, :, :] = np.asarray(sma, float)
    active = np.zeros((1, T), bool)
    active[0, start:] = True
    init = np.zeros((1, T), bool)
    init[0, start] = True
    last = np.zeros((1, T), bool)
    last[0, -1] = True
    prevc = np.r_[np.nan, c[:-1]] / split
    prevc[0] = c[0]
    tr = np.r_[0.0, (c[1:] + div[1:]) / (c[:-1] / split[1:]) - 1]
    return g.Rows("X", sessions, ["X"], np.array([hs]), o[None], h[None], lo[None], c[None], prevc[None], split[None],
                  div[None], tr[None], active, init, last, full_sig(sig_hi, 32), full_sig(sig_lo, 32),
                  full_sig(opp_s, 8), full_sig(opp_b, 8), smap,
                  (np.ones((1, T), bool) if up is None else np.asarray(up, bool)[None]),
                  np.full((1, T), sigma), np.zeros(T), np.full(T, np.nan) if oneq is None else np.asarray(oneq, float))


def flat(n, p=100.0):
    return [p] * n, [p] * n, [p] * n, [p] * n


def base_shares(p=100.0, frac=0.75, hs=2.0):
    return math.floor(frac * (g.START_EQUITY - g.BUFFER) / (p * (1 + max(hs / 1e4, 0.005 / p))))


def values(sim, k=0):
    R = sim["rec"]["R"][:, k]
    R = np.nan_to_num(R)
    return g.START_EQUITY * np.cumprod(1 + R)


# ------------------------------------------------------------------ grid, costs, helpers

def test_grid_size_and_labels():
    df = g.grid()
    assert len(df) == g.N_GRID == 8 * 4 * 3 * 5 * 4 * 4 * 4 * 2 == 61_440
    assert not df.duplicated().any()
    assert np.ravel_multi_index(tuple(df.iloc[123].to_numpy()), g.SHAPE) == 123
    lab = g.label(df.iloc[0])
    assert lab.startswith("D10 p80 S SMA H5 1/5 res0 ALL")


def test_cost_vec_matches_order_cost():
    rng = np.random.default_rng(1)
    for _ in range(200):
        q = int(rng.integers(1, 3000))
        px = float(rng.uniform(5, 900))
        sell = bool(rng.integers(0, 2))
        bps = float(rng.choice([1.0, 2.0]))
        mult = float(rng.choice([1.0, 2.0]))
        want = order_cost(q, px, sell, max(bps / 1e4, 0.005 / px) * mult)["total"]
        got = float(g.cost_vec(q, px, sell, bps, g.CostModel(True, mult)))
        assert got == pytest.approx(want, rel=1e-12)
    assert float(g.cost_vec(0, 100.0, True, 2.0)) == 0.0
    assert float(g.cost_vec(10, 100.0, True, 2.0, g.CostModel(False))) == 0.0


def test_expanding_thresholds_use_only_the_past():
    rng = np.random.default_rng(2)
    x = rng.normal(size=600)
    th = g.expanding_thresholds(x, (0.9, 0.1))
    assert np.isnan(th[:, :200]).all() and np.isfinite(th[:, 200:]).all()
    assert th[0, 400] == pytest.approx(np.quantile(x[:400], 0.9))
    y = x.copy()
    y[400] = 1e6               # today's value does not enter today's threshold
    assert g.expanding_thresholds(y, (0.9,))[0, 400] == th[0, 400]


def test_neighbour_median_ordinal_axes_only():
    s = np.zeros(g.N_GRID)
    i = np.ravel_multi_index((0, 1, 0, 0, 1, 1, 1, 0), g.SHAPE)
    s[i] = 10.0                              # an isolated spike
    nm = g.neighbour_median(s)
    assert nm[i] == 0.0                      # the spike's neighbourhood median is 0
    s2 = np.zeros(g.N_GRID)
    blk = np.zeros(g.SHAPE)
    blk[0, :, 0, 0, :, :, :, 0] = 5.0        # a plateau over all ordinal values
    nm2 = g.neighbour_median(blk.ravel())
    assert nm2[i] == 5.0
    del s2


def test_select_argmax_and_plateau():
    s = np.zeros(g.N_GRID)
    spike = np.ravel_multi_index((0, 1, 0, 0, 1, 1, 1, 0), g.SHAPE)
    s[spike] = 3.0
    blk = s.reshape(g.SHAPE).copy()
    blk[2, :, 1, 3, :, :, :, 1] = 1.0
    sel = g.select(blk.ravel())
    assert sel["A"] == spike
    assert np.unravel_index(sel["P"], g.SHAPE)[0] == 2


# ------------------------------------------------------------------ equivalence with selective_t

def synthetic_market(n=420, seed=3, split_day=None, div_day=None):
    rng = np.random.default_rng(seed)
    adj = 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.015, n)))
    o_adj = np.r_[adj[0], adj[:-1]] * np.exp(rng.normal(0, 0.006, n))
    h_adj = np.maximum(o_adj, adj) * (1 + np.abs(rng.normal(0, 0.006, n)))
    l_adj = np.minimum(o_adj, adj) * (1 - np.abs(rng.normal(0, 0.006, n)))
    factor = np.ones(n)
    split = np.ones(n)
    if split_day is not None:
        factor[:split_day] = 2.0
        split[split_day] = 2.0
    div = np.zeros(n)
    if div_day is not None:
        div[div_day] = 0.5
    o, h, lo, c = o_adj * factor, h_adj * factor, l_adj * factor, adj * factor
    dates = pd.bdate_range("2015-01-01", periods=n)
    s = it.Series_("AAPL", dates, o, h, lo, c, np.r_[np.nan, c[:-1]] / split, split, div, np.full(n, 0.02),
                   pd.Series(np.r_[np.nan, (c[1:] + div[1:]) / (c[:-1] / split[1:]) - 1], index=dates), {})
    sma = pd.Series(adj).rolling(20, min_periods=20).mean().to_numpy()
    mk = st.Market("AAPL", s, factor, sma * factor, np.r_[np.nan, sma[:-1]] * factor, np.full(n, 50.0))
    return mk, adj, sma


@pytest.mark.parametrize("split_day,div_day", [(None, None), (250, 180)])
def test_s_leg_equals_selective_t_s1(split_day, div_day):
    mk, adj, sma = synthetic_market(split_day=split_day, div_day=div_day)
    s = mk.s
    n = len(adj)
    with np.errstate(invalid="ignore"):
        trig = adj >= 1.03 * sma
    smap = np.vstack([mk.sma_prev] * 3)
    rows = make_rows(s.open, s.high, s.low, s.close, sig_hi=trig, sma=smap, split=s.split, div=s.div, start=20,
                     sessions=s.sessions)
    fam = st.Fam("S1", "X", 0.03, max_hold=20)
    ref = st.simulate(mk, 20, n - 1, st.Spec((fam,)), None)
    ref_h = st.simulate(mk, 20, n - 1, st.Spec(()), None)
    ref_100 = st.simulate(mk, 20, n - 1, st.Spec((), mode="margin", base_frac=1.0, rebalance=False), None)
    cfgs = codes_of(code(), code(direction=g.DIR_NONE, reserve=2), code(direction=g.DIR_NONE, reserve=0))
    sim = g.simulate_grid(rows, cfgs, record=True)
    assert len(ref["trips"]) > 5
    for k, rr in enumerate((ref, ref_h, ref_100)):
        got = values(sim, k)[20:]
        np.testing.assert_allclose(got, rr["value"].to_numpy()[1:], rtol=1e-9)
    assert sim["acc"]["nT"][:, 0].sum() == len(ref["trips"])
    assert sim["acc"]["nTarget"][:, 0].sum() == (ref["trips"]["reason"] == "target").sum()


# ------------------------------------------------------------------ rule mechanics

def test_b_leg_target_needs_trade_through():
    # dip signal at the close of day 2 -> buy at the open of day 3 (100); target = 100 * (1 + 2 * 0.02) = 104
    sig = np.zeros(12, bool)
    sig[2] = True
    o, h, lo, c = flat(12)
    h[4] = 104.04          # touches, does not trade through max(1c, 0.05%) = 5.2c
    h[9] = 104.06          # trades through, but after the 5-day time stop (close of day 7)
    rows = make_rows(o, h, lo, c, sig_lo=sig, sigma=0.02)
    sim = g.simulate_grid(rows, codes_of(code(direction=1, exit=1, hold=0)), g.CostModel(False), record=True)
    assert sim["acc"]["nTimeout"].sum() == 1 and sim["acc"]["nTarget"].sum() == 0
    assert sim["acc"]["sDays"].sum() == 5
    o, h, lo, c = flat(12)
    h[4], h[6] = 104.04, 104.06
    rows = make_rows(o, h, lo, c, sig_lo=sig, sigma=0.02)
    sim = g.simulate_grid(rows, codes_of(code(direction=1, exit=1, hold=0)), g.CostModel(False), record=True)
    assert sim["acc"]["nTimeout"].sum() == 0 and sim["acc"]["nTarget"].sum() == 1
    b = base_shares()
    q = min(math.floor(b / 3), math.floor((g.START_EQUITY - b * 100 - g.BUFFER) / (1.1 * 100)))   # cash cap
    assert sim["acc"]["sG"].sum() == pytest.approx(4.0 / 100 * 1e4)     # +4% gross per trip
    assert values(sim)[-1] - g.START_EQUITY == pytest.approx(q * 4.0, abs=1e-6)


def test_trailing_exit_next_open():
    # buy-dip at day 3's open (100); closes 103, 100.5 -> 100.5 <= 103 * (1 - 0.02) -> sell at day 5's open (99)
    o, h, lo, c = flat(12)
    sig = np.zeros(12, bool)
    sig[2] = True
    c[3], h[3] = 103, 103
    o[4], h[4], lo[4], c[4] = 103, 103, 100.5, 100.5
    o[5], c[5], lo[5] = 99, 99, 99
    rows = make_rows(o, h, lo, c, sig_lo=sig, sigma=0.01)
    sim = g.simulate_grid(rows, codes_of(code(direction=1, exit=2, hold=0)), g.CostModel(False), record=True)
    assert sim["acc"]["nT"].sum() == 1 and sim["acc"]["nTimeout"].sum() == 0
    assert sim["acc"]["sG"].sum() == pytest.approx(-1.0 / 100 * 1e4)
    assert sim["acc"]["sDays"].sum() == 2       # held through the closes of days 3 and 4


def test_opp_exit_and_regime_filter():
    o, h, lo, c = flat(14)
    sig = np.zeros(14, bool)
    sig[2] = True
    opp = np.zeros(14, bool)
    opp[5] = True
    rows = make_rows(o, h, lo, c, sig_hi=sig, opp_s=opp)
    sim = g.simulate_grid(rows, codes_of(code(exit=3, hold=3), code(exit=3, hold=3, regime=1)), g.CostModel(False))
    assert sim["acc"]["nT"][:, 0].sum() == 1
    assert sim["acc"]["sDays"][:, 0].sum() == 3   # sold day 3, exit at the open of day 6
    rows_dn = make_rows(o, h, lo, c, sig_hi=sig, opp_s=opp, up=np.zeros(14, bool))
    sim2 = g.simulate_grid(rows_dn, codes_of(code(exit=3, hold=3, regime=1), code(exit=3, hold=3, regime=0)))
    assert sim2["acc"]["nT"][:, 0].sum() == 0 and sim2["acc"]["nT"][:, 1].sum() == 1


def test_cash_reserve_caps_the_add_and_unsettled_buyback():
    o, h, lo, c = flat(12)
    sig = np.zeros(12, bool)
    sig[2] = True
    # buy-dip with f = 1 and a 10% reserve: q limited by settled cash (~ $1,000 / 110)
    rows = make_rows(o, h, lo, c, sig_lo=sig)
    sim = g.simulate_grid(rows, codes_of(code(direction=1, frac=3, reserve=1, exit=4, hold=0)), g.CostModel(False),
                          record=True)
    held = sim["rec"]["x"][4, 0, 0] * values(sim)[4] / 100.0
    b = base_shares(frac=0.9)
    cash0 = g.START_EQUITY - b * 100
    assert held == pytest.approx(b + math.floor((cash0 - g.BUFFER) / (1.1 * 100)), abs=1e-6)
    # sell-high f = 1, 10% reserve: buy-back limit at the SMA on the entry day needs settled cash -> not placed;
    # proceeds settle the next session, then the limit fills
    sma = np.full((3, 12), 99.0)
    o2, h2, lo2, c2 = flat(12)
    lo2[3] = 98.0
    lo2[4] = 98.0
    rows2 = make_rows(o2, h2, lo2, c2, sig_hi=sig, sma=sma)
    sim2 = g.simulate_grid(rows2, codes_of(code(frac=3, reserve=1), code(frac=3, reserve=0)), g.CostModel(False))
    assert sim2["acc"]["sDays"][:, 0].sum() == 2     # cash account: day 4 (2nd holding day)
    assert sim2["acc"]["sDays"][:, 1].sum() == 1     # margin account: same day


def test_margin_interest_on_negative_cash():
    o, h, lo, c = flat(30)
    sig = np.zeros(30, bool)
    sig[2] = True
    rows = make_rows(o, h, lo, c, sig_lo=sig)
    rows.rf[:] = 0.0001
    sim = g.simulate_grid(rows, codes_of(code(direction=1, frac=3, reserve=0, exit=4, hold=3)), g.CostModel(False),
                          record=True)
    v = values(sim)
    b = base_shares(frac=1.0)
    debt = b * 100 - (g.START_EQUITY - b * 100)      # cash after adding b shares at 100
    want = debt * (0.0001 + g.MARGIN_SPREAD / 252)
    assert v[3] - v[4] == pytest.approx(want, rel=1e-6)


def test_accumulators_match_recorded_series():
    mk, adj, sma = synthetic_market(n=500, seed=7)
    s = mk.s
    rng = np.random.default_rng(0)
    sig = np.zeros((32, 500), bool)
    sig[:, 30:] = rng.random((32, 470)) < 0.05
    rows = make_rows(s.open, s.high, s.low, s.close, sig_hi=sig, sig_lo=sig, sma=np.vstack([mk.sma_prev] * 3), start=25)
    df = g.grid().sample(64, random_state=1)
    sim = g.simulate_grid(rows, g.cfg_arrays(df), record=True)
    allyears = np.ones(len(sim["years"]), bool)
    ws = g.wstats(sim, allyears)
    R = pd.DataFrame(sim["rec"]["R"][25:])
    M = pd.Series(sim["rec"]["M"][25:])
    X = sim["rec"]["x"][25:, 0, :]
    for k in (0, 5, 40):
        w = X[:, k].mean()
        e = R[k] - w * M + max(w - 1, 0) * sim["f"][25:]
        assert ws["w"][k] == pytest.approx(w)
        assert ws["mean_e"][k] == pytest.approx(e.mean(), rel=1e-9, abs=1e-12)
        sd = (R[k] - w * M).std(ddof=0)
        assert ws["ir"][k] == pytest.approx(e.mean() / sd * math.sqrt(252), rel=1e-6, abs=1e-9)
        assert ws["cagr"][k] == pytest.approx(np.prod(1 + R[k]) ** (252 / len(R)) - 1, rel=1e-9)
        hm = np.prod(1 + w * M - max(w - 1, 0) * sim["f"][25:]) ** (252 / len(M)) - 1
        assert g.geo_hmatch(sim, allyears, ws["w"])[k] == pytest.approx(hm, rel=1e-9)


def test_walk_forward_uses_only_prior_years():
    mk, adj, sma = synthetic_market(n=1300, seed=9)
    s = mk.s
    rng = np.random.default_rng(1)
    sig = rng.random((32, 1300)) < 0.04
    rows = make_rows(s.open, s.high, s.low, s.close, sig_hi=sig, sig_lo=sig, sma=np.vstack([mk.sma_prev] * 3))
    sim = g.simulate_grid(rows, g.cfg_arrays(g.grid()), g.CostModel())
    picks = g.walk_forward_picks(sim)
    assert picks and picks[0][0] == 2023                # 2020-2022 = 784 sessions >= 750
    sim2 = {**sim, "acc": {k: v.copy() for k, v in sim["acc"].items()}}
    sim2["acc"]["sR"][-1] += 1.0                         # change the last year only
    picks2 = g.walk_forward_picks(sim2)
    assert picks2 == picks                               # last year's data never selects its own config


def test_hmatch_per_account_exposure():
    o, h, lo, c = flat(10)
    rows = make_rows(o, h, lo, c)
    rows.tr[0, :] = 0.01
    ex = {"sim": {"rec": {"x": np.full((10, 1, 1), 0.5)}}, "act": rows.active.T, "acct": np.cumsum(rows.init.T, 0),
          "tr": rows.tr.T, "f": np.zeros(10), "sessions": rows.sessions}
    hm = g.hmatch_series(ex, 0)
    assert hm.iloc[1:].tolist() == pytest.approx([0.005] * 9)


def test_t10_spells_keep_slots_and_open_new_accounts():
    master = pd.bdate_range("2012-06-01", "2012-10-31")
    n = len(master)

    class I:  # minimal instrument stub
        def __init__(self, has):
            self.has = has
    full = np.ones(n, bool)
    late = np.zeros(n, bool)
    late[master >= pd.Timestamp("2012-08-15")] = True
    insts = {f"s{i}": I(full) for i in range(11)}
    insts["s10"] = I(late)
    rows = []
    for s_, mem in (("2012-06-29", range(10)), ("2012-07-31", [0, 1, 2, 3, 4, 5, 6, 7, 8, 10]),
                    ("2012-08-31", [0, 1, 2, 3, 4, 5, 6, 7, 8, 9])):
        for k, i in enumerate(mem):
            rows.append({"s": s_, "rank": k + 1, "security_id": f"s{i}"})
    top = pd.DataFrame(rows)
    top["hold_month"] = pd.PeriodIndex(pd.to_datetime(top["s"]), freq="M") + 1
    sp = g.t10_spells(top, master, insts)
    by = {}
    for slot, sid, a, b in sp:
        by.setdefault(sid, []).append((slot, str(master[a].date()), str(master[b].date())))
    assert by["s0"] == [(0, "2012-07-02", "2012-09-28")]
    assert by["s9"] == [(9, "2012-07-02", "2012-07-31"), (9, "2012-09-03", "2012-09-28")]
    # s10 has data from 2012-08-15: its account opens the day after its first real row
    assert by["s10"] == [(9, "2012-08-16", "2012-08-31")]
