"""Tests for scripts/research_fundamentals.py: point-in-time use of filings, factor formulas on synthetic statements,
composites, top-10 targets, walk-forward selection using only past data, and the multiple-testing helpers."""
import math

import numpy as np
import pandas as pd
import pytest

from scripts import research_fundamentals as rf


# ------------------------------------------------------------------ synthetic statements

def _facts(cik=1, years=None, filed=None, extra=()):
    """Annual facts for fiscal years ending Dec 31; ``years`` = {year: {group: value}}; each year's facts are filed
    on ``filed[year]`` (default: Feb 20 of the next year) together with the prior year's comparatives."""
    rows = []
    for y, vals in years.items():
        f = (filed or {}).get(y, f"{y + 1}-02-20")
        end = f"{y}-12-31"
        for g, v in vals.items():
            kind = "S" if g in rf.STOCK else "F"
            rows.append((cik, g, 0, kind, end, f, float(v)))
    rows += list(extra)
    return pd.DataFrame(rows, columns=rf.ANNUAL_COLS)


BASE = {"rev": 1000, "cogs": 600, "oi": 150, "ni": 100, "da": 40, "rd": 50, "int": 10, "cfo": 160, "capex": 60,
        "div": 20, "buy": 30, "eps": 2.0, "sh": 50, "ta": 2000, "ca": 800, "cl": 400, "tl": 1000, "be": 1000,
        "cash": 200, "debt_cur": 50, "ltd_nc": 300, "re": 500, "ar": 100, "inv": 150, "ap": 80, "tp": 10}


def _year(scale=1.0, **over):
    d = {k: v * scale if k not in ("eps",) else v for k, v in BASE.items()}
    d.update(over)
    return d


def test_formulas_on_synthetic_statements():
    years = {2016: _year(0.7, eps=1.0, sh=52), 2017: _year(0.8, eps=1.2, sh=51), 2018: _year(0.9, eps=1.5, sh=50.5),
             2019: _year(1.0, eps=2.0, sh=50)}
    # 2019 values are BASE; 2018 = 0.9 x BASE (eps 1.5, sh 50.5)
    st = rf.company_states(_facts(years=years))
    last = st.iloc[-1]
    assert last["fy0_end"] == "2019-12-31"
    assert last["roe"] == pytest.approx(100 / 1000)
    assert last["roa"] == pytest.approx(100 / 2000)
    assert last["gpa"] == pytest.approx(400 / 2000)
    assert last["gross_margin"] == pytest.approx(0.4)
    assert last["op_margin"] == pytest.approx(0.15)
    assert last["opa"] == pytest.approx(150 / 2000)
    debt = 300 + 50
    assert last["roic"] == pytest.approx(150 / (debt + 1000 - 200))
    # cbop = (OI + DA + RD - dAR - dINV + dAP) / TA with 2018 at 0.9x
    cb = (150 + 40 + 50 - (100 - 90) - (150 - 135) + (80 - 72)) / 2000
    assert last["cbop"] == pytest.approx(cb)
    assert last["sales_g1"] == pytest.approx(1000 / 900 - 1)
    assert last["sales_g3"] == pytest.approx((1000 / 700) ** (1 / 3) - 1)
    assert last["eps_g1"] == pytest.approx((2.0 - 1.5) / 1.5)
    assert last["eps_g3"] == pytest.approx((2.0 - 1.0) / 1.0)
    assert last["opinc_g1"] == pytest.approx((150 - 135) / 135)
    avg_ta = (2000 + 1800) / 2
    assert last["accruals"] == pytest.approx((100 - 160) / avg_ta)
    sloan = ((800 - 720) - (200 - 180) - ((400 - 360) - (50 - 45) - (10 - 9)) - 40) / avg_ta
    assert last["sloan"] == pytest.approx(sloan)
    assert last["cash_conv"] == pytest.approx(1.6)
    assert last["noa"] == pytest.approx(((2000 - 200) - (2000 - debt - 1000)) / 1800)
    assert last["asset_g"] == pytest.approx(2000 / 1800 - 1)
    assert last["capex_assets"] == pytest.approx(60 / 2000)
    assert last["share_iss"] == pytest.approx(math.log(50 / 50.5))
    assert last["de"] == pytest.approx(debt / 1000)
    assert last["nd_ebitda"] == pytest.approx((debt - 200) / (150 + 40))
    assert last["current_ratio"] == pytest.approx(2.0)
    assert last["int_cov"] == pytest.approx(15.0)
    assert last["asset_turn"] == pytest.approx(0.5)
    assert last["d_asset_turn"] == pytest.approx(0.0)
    assert last["rd_sales"] == pytest.approx(0.05)
    assert last["altman_part"] == pytest.approx(1.2 * 400 / 2000 + 1.4 * 500 / 2000 + 3.3 * 150 / 2000 + 1000 / 2000)
    # ROA identical every year (scaled statements) -> zero earnings variability over 4 years
    assert last["earn_var"] == pytest.approx(0.0, abs=1e-12)
    # numerators kept for the market factors
    assert last["fcf0"] == pytest.approx(100) and last["payout0"] == pytest.approx(50)


def test_piotroski_counts_signals():
    y1 = rf.year_inputs({(g, "2018-12-31"): {0: v} for g, v in _year(0.9).items()}, "2018-12-31",
                        {g: {"2018-12-31"} for g in rf.STOCK})
    good = _year(1.0, ni=150, cfo=200, sh=44, ltd_nc=200, ca=900, cogs=550, ta=1900)   # all 9 favourable
    y0 = rf.year_inputs({(g, "2019-12-31"): {0: v} for g, v in good.items()}, "2019-12-31",
                        {g: {"2019-12-31"} for g in rf.STOCK})
    assert rf.piotroski(y0, y1) == pytest.approx(9.0)
    bad = _year(1.0, ni=-10, cfo=-20, sh=60, ltd_nc=900, ca=300, cogs=700, rev=800)
    y0b = rf.year_inputs({(g, "2019-12-31"): {0: v} for g, v in bad.items()}, "2019-12-31",
                         {g: {"2019-12-31"} for g in rf.STOCK})
    assert rf.piotroski(y0b, y1) == pytest.approx(0.0)
    assert np.isnan(rf.piotroski(y0, None))       # only 4 of 9 signals computable without a prior year


def test_concept_priority_gross_profit_fallback_and_debt_rules():
    known = {("rev", "2019-12-31"): {0: 1000.0, 3: 990.0}, ("cogs", "2019-12-31"): {2: 700.0},
             ("ltd_all", "2019-12-31"): {0: 500.0}, ("stb", "2019-12-31"): {0: 20.0},
             ("ltd_cur", "2019-12-31"): {0: 100.0}}
    x = rf.year_inputs(known, "2019-12-31", {"ltd_all": {"2019-12-31"}, "stb": {"2019-12-31"},
                                             "ltd_cur": {"2019-12-31"}})
    assert x["rev"] == 1000.0                       # first concept by priority
    assert x["gp"] == pytest.approx(300.0)          # revenue - cost of revenue when GrossProfit is missing
    assert x["debt"] == pytest.approx(520.0)        # LongTermDebt (incl. current part) + short-term borrowings
    known[("gross", "2019-12-31")] = {0: 1200.0}    # gross profit above revenue = tagging error -> empty
    assert rf.year_inputs(known, "2019-12-31", {})["gp"] is None


def test_interest_coverage_rules():
    def cov(**kw):
        y = rf.year_inputs({(g, "2019-12-31"): {0: v} for g, v in _year(1.0, **kw).items()
                            if v is not None}, "2019-12-31", {g: {"2019-12-31"} for g in rf.STOCK})
        if kw.get("int", 1) is None:
            y["int"] = None
        return rf.accounting_factors([y])["int_cov"]
    assert cov(int=0.0) == rf.INT_COV_CAP
    assert np.isnan(cov(int=None))
    assert cov(int=0.01) == rf.INT_COV_CAP           # capped


# ------------------------------------------------------------------ point in time

def test_states_use_only_facts_filed_before_and_restatements_apply_from_their_filing():
    years = {2018: _year(0.9), 2019: _year(1.0)}
    restated = [(1, "ni", 0, "F", "2019-12-31", "2020-06-15", 50.0)]   # 10-K/A filed later halves 2019 NI
    st = rf.company_states(_facts(years=years, extra=restated))
    eps = pd.DataFrame(columns=["cik", "avail", "q_end", "c_growth"])
    rows = pd.DataFrame({"s": pd.to_datetime(["2019-06-28", "2020-02-20", "2020-02-21", "2020-06-15", "2020-06-16"]),
                         "cik": 1, "security_id": "X"})
    m = rf.asof_states(rows, st, eps).set_index("s")
    assert m.loc["2019-06-28", "fy0_end"] == "2018-12-31"
    assert m.loc["2020-02-20", "fy0_end"] == "2018-12-31"       # filed that day: not usable yet (strictly before)
    assert m.loc["2020-02-21", "fy0_end"] == "2019-12-31"
    assert m.loc["2020-02-21", "roe"] == pytest.approx(0.1)
    assert m.loc["2020-06-15", "roe"] == pytest.approx(0.1)     # restatement not known on its filing day
    assert m.loc["2020-06-16", "roe"] == pytest.approx(0.05)
    assert (pd.to_datetime(m["filed"]) < m.index).all()


def test_stale_states_and_quarterly_eps_age():
    st = rf.company_states(_facts(years={2017: _year(0.9), 2018: _year(1.0)}))
    eps = pd.DataFrame({"cik": [1], "avail": ["2019-05-01"], "q_end": ["2019-03-31"], "c_growth": [0.25]})
    rows = pd.DataFrame({"s": pd.to_datetime(["2019-05-01", "2019-05-31", "2020-06-30", "2020-07-31"]),
                         "cik": 1, "security_id": "X"})
    m = rf.asof_states(rows, st, eps).set_index("s")
    assert np.isnan(m.loc["2019-05-01", "q_eps_g"])          # avail day itself: not yet
    assert m.loc["2019-05-31", "q_eps_g"] == pytest.approx(0.25)
    assert np.isnan(m.loc["2020-06-30", "q_eps_g"])          # quarter ended > 200 days earlier
    assert m.loc["2020-06-30", "roe"] == pytest.approx(0.1)  # FY2018 end + 547 days: still fresh
    assert np.isnan(m.loc["2020-07-31", "roe"])              # > 550 days after FY0 end: dropped


def test_market_factors():
    m = pd.DataFrame({"mcap": [2000.0, 1000.0, np.nan], "ni0": [100.0, 100.0, 100.0], "be0": [500.0, -5.0, 1.0],
                      "rev0": [1000.0] * 3, "cfo0": [150.0] * 3, "ebit0": [200.0] * 3, "debt0": [100.0, 0.0, 0.0],
                      "cash0": [300.0, 2000.0, 0.0], "fcf0": [90.0] * 3, "payout0": [40.0] * 3,
                      "altman_part": [2.0] * 3, "tl0": [1000.0] * 3, "rd0": [50.0, np.nan, 1.0]})
    for f in rf.FACTORS:
        if f not in m:
            m[f] = np.nan
    out = rf.market_factors(m)
    assert out.loc[0, "ep"] == pytest.approx(0.05) and out.loc[0, "bm"] == pytest.approx(0.25)
    assert np.isnan(out.loc[1, "bm"])                                  # negative book equity
    assert out.loc[0, "ebit_ev"] == pytest.approx(200 / (2000 + 100 - 300))
    assert np.isnan(out.loc[1, "ebit_ev"])                             # EV <= 0
    assert out.loc[0, "altman_z"] == pytest.approx(2.0 + 0.6 * 2.0)
    assert out.loc[0, "size"] == pytest.approx(math.log(2000))
    assert out.loc[2, ["ep", "size", "rd_mcap"]].isna().all()          # no market cap -> no market factors


# ------------------------------------------------------------------ composites and targets

def test_zscores_respect_sign_and_composite_minimum():
    s = pd.Timestamp("2020-01-31")
    p = pd.DataFrame({"s": [s] * 4, "asset_g": [0.1, 0.2, 0.3, 0.4], "roe": [0.4, 0.3, 0.2, 0.1]})
    z = rf.zscores(p, ["asset_g", "roe"])
    assert z["asset_g"].idxmax() == 0 and z["roe"].idxmax() == 0     # low asset growth / high ROE are "good"
    assert z["asset_g"].mean() == pytest.approx(0.0) and z["asset_g"].std() == pytest.approx(1.0)
    full = pd.DataFrame({"s": [s] * 4})
    for f in rf.FACTORS:
        full[f] = np.nan
    full["asset_g"] = [0.1, 0.2, 0.3, 0.4]                 # investment family: 1 of 3 factors < ceil(3/2) = 2
    c = rf.composites(full)
    assert c["C_investment"].isna().all()
    full["capex_assets"] = [0.1, 0.2, 0.3, 0.4]
    c = rf.composites(full)
    assert c["C_investment"].notna().all() and c["C_investment"].idxmax() == 0
    assert c["C_ALL"].isna().all()                         # only 1 of 7 families available


def test_top_targets_sign_ties_and_minimum():
    s1, s2 = pd.Timestamp("2020-01-31"), pd.Timestamp("2020-02-28")
    p = pd.DataFrame({"s": [s1] * 12 + [s2] * 9, "security_id": [f"S{i:02d}" for i in range(12)] + list("ABCDEFGHI"),
                      "dv50_rank": 1.0, "mcap": 1e9,
                      "size": list(range(12)) + list(range(9))})
    tg = rf.top_targets(p, "size")                       # size: smaller is better
    assert [x[0] for x in tg[s1]] == [f"S{i:02d}" for i in range(10)]
    assert all(abs(x[1] - 0.1) < 1e-12 for x in tg[s1])
    assert s2 not in tg                                  # only 9 rankable names: no rebalance
    p.loc[:11, "size"] = 1.0                             # all tied -> security id order
    assert [x[0] for x in rf.top_targets(p, "size")[s1]] == [f"S{i:02d}" for i in range(10)]


# ------------------------------------------------------------------ walk-forward uses only past data

def _nav(daily_ret, start="2014-01-02", end="2020-12-31"):
    d = pd.bdate_range(start, end)
    return pd.Series(rf.ACCOUNT * np.cumprod(1 + np.full(len(d), daily_ret)), index=d)


def test_trailing_sharpe_ignores_data_after_asof_and_needs_full_window():
    rng = np.random.default_rng(0)
    d = pd.bdate_range("2014-01-02", "2020-12-31")
    nav = pd.Series(rf.ACCOUNT * np.cumprod(1 + rng.normal(0.0005, 0.01, len(d))), index=d)
    asof = pd.Timestamp("2016-12-30")
    a = rf.trailing_sharpe(nav, asof)
    nav2 = nav.copy()
    nav2[nav2.index > asof] *= 5.0                       # change the future
    assert rf.trailing_sharpe(nav2, asof) == pytest.approx(a)
    assert np.isfinite(a)
    assert np.isnan(rf.trailing_sharpe(nav, pd.Timestamp("2016-11-30")))  # Jan 2014..Nov 2016 = 35 months


def test_walk_forward_picks_from_past_only():
    sigs = [d for d in pd.bdate_range("2013-12-31", "2019-12-31") if d == d + pd.offsets.BMonthEnd(0)]
    rng = np.random.default_rng(1)
    d = pd.bdate_range("2014-01-02", "2019-12-31")
    navs = {k: pd.Series(rf.ACCOUNT * np.cumprod(1 + rng.normal(mu, 0.01, len(d))), index=d)
            for k, mu in (("a", 0.0002), ("b", 0.0008), ("c", -0.0003))}
    targets = {k: {s: [(k.upper(), 1.0, 1.0, 1e9, "")] for s in sigs} for k in navs}
    tg, picks = rf.walk_forward(navs, targets, sigs, first_year=2017)
    first = picks[0]
    assert first["year"] == 2017 and first["selected_at"] == "2016-12-30"
    # every pick equals the best trailing Sharpe computed on data truncated at the selection date
    for pk in picks:
        asof = pd.Timestamp(pk["selected_at"])
        trunc = {k: v[v.index <= asof] for k, v in navs.items()}
        best = max(trunc, key=lambda k: (rf.trailing_sharpe(trunc[k], asof), k))
        assert pk["pick"] == best
        # changing the future does not change the pick
        fut = {k: v.where(v.index <= asof, v * (10 if k != best else 0.1)) for k, v in navs.items()}
        _, p2 = rf.walk_forward(fut, targets, [s for s in sigs if s <= asof] + [s for s in sigs if s > asof],
                                first_year=asof.year + 1)
        assert p2[0]["pick"] == pk["pick"]
    # the selection at Dec Y-1 governs the 12 signals Dec Y-1 .. Nov Y
    s_dec = pd.Timestamp("2016-12-30")
    year = [s for s in sigs if s >= s_dec][:12]
    assert all(tg[s][0][0] == first["pick"].upper() for s in year)
    assert min(tg) == s_dec


# ------------------------------------------------------------------ statistics and registration

def test_t_sf_and_bh():
    assert rf.t_sf(2.0, 10) == pytest.approx(0.036694, abs=1e-5)
    assert rf.t_sf(0.0, 50) == pytest.approx(0.5)
    assert rf.t_sf(-2.0, 10) == pytest.approx(1 - 0.036694, abs=1e-5)
    assert rf.t_sf(3.0, 1000) == pytest.approx(0.001384, abs=2e-5)
    p = np.array([0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205, 0.212, 0.216])
    assert rf.bh_reject(p, 0.05).sum() == 2


def test_rule_counts_and_signs():
    assert len(rf.FACTORS) == 40 and len(set(rf.FACTORS)) == 40
    assert rf.N_TESTS == 96
    assert rf.SIGN["size"] == -1 and rf.SIGN["accruals"] == -1 and rf.SIGN["gpa"] == 1 and rf.SIGN["bm"] == 1


def test_prereg_hash_covers_only_the_block():
    text = "intro\n<!-- PREREG-BEGIN -->\nrules\n<!-- PREREG-END -->\nresults"
    assert rf.prereg_hash(text) == rf.prereg_hash(text.replace("results", "other results"))
    assert rf.prereg_hash(text) != rf.prereg_hash(text.replace("rules", "changed"))


def test_reject_small_mcaps_flags_share_unit_errors():
    s = pd.Timestamp("2020-01-31")
    n = 60
    c = pd.DataFrame({"s": [s] * n, "in300": True, "dv50_rank": np.arange(1, n + 1, dtype=float),
                      "dv50": 1e7, "mcap": 1.4e9, "mcap_src": "sec_shares"})
    c.loc[55, "mcap"] = 1.4e6            # shares filed in thousands: 1000x too small
    c.loc[56, "mcap"] = 0.2e9            # 7x below the median ratio: plausible, kept
    out = rf.reject_small_mcaps(c)
    assert np.isnan(out.loc[55, "mcap"]) and out.loc[55, "mcap_src"] == "rejected_too_small"
    assert out.loc[56, "mcap"] == pytest.approx(0.2e9)
