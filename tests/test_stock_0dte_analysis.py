"""studies/stock_0dte.py (ledger docs/research_ledger_stock_0dte.md 0.4-0.10): unit checks of the book, fees, earnings
timing and the per-year-beta alpha, and an end-to-end run on a synthetic panel with one planted edge."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant.strategies import stock_0dte as Q
from studies import stock_0dte as A

PCS, CCS, IC, LC, ORB, PRF = (A.shape("PCS", 0.10), A.shape("CCS", 0.10), A.shape("IC", 0.16), A.shape("LC", 0.05),
                              A.ORB_SHAPE, A.shape("PRF"))


def test_config_count_and_threshold():
    cf = A.config_list()
    assert len(cf) == A.N_TRIALS == 29_162
    assert abs(A.T_GRID - 4.78) < 0.01 and abs(A.T_CUM - 4.96) < 0.01
    assert cf["name"].is_unique
    assert set(cf.loc[cf["e"] == A.ON, "f"].map(lambda f: A.FILTERS[f])) == set(A.F_ON)
    assert (cf["b"] == A.ORB_SHAPE).sum() == 2 * len(A.F_ORB)


def test_book_take_stops_at_the_first_position_over_300():
    s = np.array([0, 0, 0, 0, 1, 1])
    ml = np.array([100.0, 150.0, 80.0, 10.0, 290.0, 20.0])
    assert A.book_take(s, ml).tolist() == [True, True, False, False, True, False]


def test_alpha_years_matches_direct_ols_hac():
    rng = np.random.default_rng(1)
    n = 300
    yrs = np.repeat([2016, 2017, 2018], 100)
    x = rng.normal(size=n)
    r = 0.02 + np.where(yrs == 2017, 0.5, -0.3) * x + rng.normal(scale=0.1, size=n)
    a, t = A.alpha_years(r, x, yrs)
    Xd = np.column_stack([np.ones(n)] + [np.where(yrs == y, x, 0.0) for y in (2016, 2017, 2018)])
    inv = np.linalg.inv(Xd.T @ Xd)
    beta = inv @ Xd.T @ r
    g = Xd * (r - Xd @ beta)[:, None]
    S = g.T @ g
    for k in range(1, 6):
        G = g[k:].T @ g[:-k]
        S += (1 - k / 6) * (G + G.T)
    se = np.sqrt((inv @ S @ inv)[0, 0])
    assert abs(a[0] - beta[0]) < 1e-12 and abs(t[0] - beta[0] / se) < 1e-9


def one_row(k, e, s, **kw):
    base = dict(k=k, entry=e, shape=s, code=0, credit=0.20, den=0.814, pnl_H=0.186, pnl_M1=0.186, pnl_M2=0.186,
                pnl_M3=0.186, imp_entry=0.01, d_close=0.0, x_M1=0, x_M2=0, x_M3=0, spr_entry=0.04, imp_M1=0.0,
                imp_M2=0.0, imp_M3=0.0, delta=0.10, width_frac=0.01, n_entry=2, n_flat=0, n_M1=0, n_M2=0, n_M3=0,
                fee_entry=0.014, fee_flat=0.0, nsh_hold=0, nsh_flat=0, d15=0.0, s_fill=100.0, xfee_M1=0.0,
                xfee_M2=0.0, xfee_M3=0.0, flat_orders=0, flat_surcharge=0.0, stuck=0, direction=0)
    base.update(kw)
    return base


def name_row(d, nid, name, rank, rank_on, S=100.0, **kw):
    r = {"date": d, "nid": nid, "name": name, "half": 0, "status": 0, "nchain": 50, "nload": 20, "penny": 1,
         "sig_ok": 1, "S_open": S, "S_close": S, "prev_close": S, "sig": 0.02, "kmp": S, "rank": rank, "nreal": 380,
         "vix_prev": 15.0, **{f"S_e{e}": S for e in range(Q.NE)}, **{f"strad_e{e}": 1.0 for e in range(Q.NE)},
         **{f"pin_e{e}": S for e in range(Q.NE)}, "rank_on": rank_on, "S_on": S, "strad_on": 1.2, "vix_on": 15.0,
         "next_open": S, "split_next": 0, "close_official": S}
    r.update(kw)
    return r


def tiny_panel():
    d = pd.Timestamp("2024-03-08")
    names = pd.DataFrame([name_row(d, 1, "AAA", 1, 1), name_row(d, 2, "BBB", 2, 2)])
    rows = pd.DataFrame([one_row(0, 0, PCS), one_row(1, 0, PCS, fee_entry=0.012, n_entry=2),
                         one_row(0, 0, LC, credit=-0.03, den=0.0355, pnl_H=-0.0355, pnl_M1=-0.0355, pnl_M2=-0.0355,
                                 pnl_M3=-0.0355, n_entry=1, fee_entry=0.0055)])
    return Q.build_panel(names, rows)


def test_surcharges_combo_and_per_leg():
    P = tiny_panel()
    B = A.Bases(P)
    b = B.get(PCS)
    # 2 legs at $0.65 + $0.05: tier $1.30 -> no combo surcharge; per leg 2 x ($1 - $0.65) = $0.70 a position
    assert abs(b["sur"][0, 0, 0]) < 1e-12 and abs(b["sur_leg"][0, 0, 0] * 100 - 0.70) < 1e-9
    # tier 2 x $0.55 = $1.10 for name 2 -> per leg 2 x $0.45
    assert abs(b["sur_leg"][0, 0, 1] * 100 - 0.90) < 1e-9
    lc = B.get(LC)
    assert abs(lc["sur"][0, 0, 0] * 100 - 0.50) < 1e-9          # one $0.50 contract pays the $1 minimum
    assert abs(lc["ml"][0, 0] - 3.55) < 1e-9 and lc["k10"][0, 0]
    assert abs(b["ml"][0, 0] - 81.4) < 1e-9


def test_earnings_timing():
    days = pd.DatetimeIndex(["2024-03-06", "2024-03-07", "2024-03-08", "2024-03-11"])
    names = pd.DataFrame([name_row(pd.Timestamp("2024-03-08"), 1, "AAA", 1, 1),
                          name_row(pd.Timestamp("2024-03-08"), 2, "BBB", 2, 2),
                          name_row(pd.Timestamp("2024-03-08"), 3, "CCC", 3, 3),
                          name_row(pd.Timestamp("2024-03-08"), 4, "DDD", 4, 4)])
    P = Q.build_panel(names, pd.DataFrame([one_row(0, 0, PCS)]))
    t = pd.to_datetime(["2024-03-07 16:05", "2024-03-08 11:00", "2023-12-01 07:00", "2024-03-07 15:50"])
    rel = pd.DataFrame({"cik": ["1", "2", "3", "3"], "form": "8-K", "t_et": t})
    s = days
    rel["d0"] = [s[s.searchsorted(x.normalize(), "right")] if x.hour >= 16 else x.normalize() for x in t]
    rel = rel[rel["d0"].isin(days)]
    ef = A.earnings_flags(P, rel, {"AAA": "1", "BBB": "2", "CCC": "3", "DDD": "4"}, days)
    assert ef["earn"][:, 0].tolist() == [True] * 5 + [True]           # after the close the day before: every entry
    # 11:00 release: same-day entries from 12:00; the overnight position (opened the day before) holds through it
    assert ef["earn"][:, 1].tolist() == [False, False, True, True, True, True]
    assert ef["react"].tolist() == [True, True, False, False] and ef["known"].tolist() == [True, True, True, False]
    assert ef["earn"][Q.NE, 2]                                        # 15:50 the day before, after the 15:46 fill


def make_synthetic(cal):
    """Seven names a week-end session (six ranked, one overnight-only), six structures, an edge planted in the
    09:45 put credit spread; earnings releases for AAA every 13th week."""
    sess = A.week_ends(cal)
    sess = sess[(sess >= A.START) & (sess <= A.END)]
    rng = np.random.default_rng(7)
    names, rows = [], []
    k = 0
    for d in sess:
        for j, nm in enumerate(("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG")):
            rank, rank_on = (j + 1, j + 1) if j < 6 else (0, 7)
            names.append(name_row(d, j + 1, nm, rank, rank_on))
            for e in range(Q.NB):
                if rank == 0 and e < Q.NE:
                    continue
                for s in (PCS, CCS, IC, LC, PRF) + ((ORB,) if e == 0 else ()):
                    if s in (PCS, CCS):
                        edge = s == PCS and e == 0
                        win = rng.random() < (0.97 if edge else 0.80)
                        pnl = 0.186 if win else -0.828
                        r = one_row(k, e, s, pnl_H=pnl, pnl_M1=pnl, pnl_M2=pnl, pnl_M3=pnl,
                                    n_flat=0 if win else 2, fee_flat=0.0 if win else 0.014, stuck=int(rng.random() < 0.02))
                    elif s == IC:
                        win = rng.random() < 0.6
                        pnl = 0.372 if win else -0.656
                        r = one_row(k, e, s, credit=0.40, den=0.628, fee_entry=0.028, n_entry=4, pnl_H=pnl,
                                    pnl_M1=pnl, pnl_M2=pnl, pnl_M3=pnl, n_flat=0 if win else 2,
                                    fee_flat=0.0 if win else 0.014)
                    elif s == LC:
                        pnl = 1.0 if rng.random() < 0.02 else -0.0555
                        r = one_row(k, e, s, credit=-0.05, den=0.0555, fee_entry=0.0055, n_entry=1, pnl_H=pnl,
                                    pnl_M1=pnl, pnl_M2=pnl, pnl_M3=pnl)
                    elif s == PRF:
                        pnl = 0.5 if rng.random() < 0.3 else -0.25
                        r = one_row(k, e, s, credit=-0.10, den=1.10, fee_entry=0.042, n_entry=6, pnl_H=pnl,
                                    pnl_M1=pnl, pnl_M2=pnl, pnl_M3=pnl, width_frac=0.01)
                    else:
                        pnl = 1.0 if rng.random() < 0.3 else -0.6
                        r = one_row(k, e, s, credit=-1.0, den=1.007, fee_entry=0.007, n_entry=1, pnl_H=pnl,
                                    pnl_M1=pnl, pnl_M2=pnl, pnl_M3=pnl, x_M1=0, x_M2=8, x_M3=1 + (rng.random() < 0.5))
                    rows.append(r)
            k += 1
    names = pd.DataFrame(names)
    rows = pd.DataFrame(rows)
    days_rec = pd.DataFrame({"date": sess, "n_names": 7})
    cik = {"AAA": "1", "BBB": "2", "CCC": "3"}
    rel = pd.DataFrame({"cik": "1", "form": "8-K", "t_et": [d - pd.Timedelta(hours=8) for d in sess[::13]]})
    rel["d0"] = sess[::13]
    return names, rows, days_rec, rel, cik


def formal_metas(chunks=None) -> list[dict]:
    """Run records as the formal runs carry them (qc/stock_0dte/runner.js): one clean run per registered chunk on the
    frozen code."""
    out = []
    for a, b in chunks or A.CHUNKS:
        ymd = lambda x: f"date({int(x[:4])}, {int(x[5:7])}, {int(x[8:])})"
        book = f"BOOK = ({ymd(a)}, {ymd(b)})"
        out.append({"rs": {"RUN": f"book {a}..{b} mech0 top20 nch100 shapes 81 seq 9 emitted 9 left 0 final 1 tz x",
                           "ERR": "none"}, "book_line": book, "mech_line": "MECH = False",
                    "hashes": {"main": A.main_hash(book, "MECH = False"),
                               **{k: A.FROZEN[k] for k in ("sim", "simx", "optmath")}}})
    return out


@pytest.fixture(scope="module")
def synthetic():
    try:
        cal = A.calendar()
        bench = A.benchmarks(cal)
    except Exception as ex:                                            # the local Yahoo cache is not in the repo
        pytest.skip(f"no benchmark cache: {ex}")
    names, rows, days_rec, rel, cik = make_synthetic(cal)
    return A.analyse_frames(names, rows, days_rec, formal_metas(), cal_days=cal, bench=bench, rel=rel, ciks=cik,
                            divs=None, quick=True, write=False)


def test_end_to_end_runs_and_finds_the_planted_edge(synthetic):
    res, series = synthetic
    assert res["configs"] == A.N_TRIALS and res["sessions"] > 700
    wf = res["walk_forward"]["K10"]
    picked = [p["config"] for p in wf["picks"] if p.get("config")]
    assert len(picked) == len(wf["picks"]) == 11
    assert all("PCS0.10-W1|09:45" in c for c in picked), picked
    assert wf["t"] > 5 and wf["checks"]["a_t"] and wf["checks"]["b_e3_positive"]
    assert set(res["go"]) >= {"1_k10_oos_pass", "2_pbo_below_half", "3_replay_beats_oneq_E3", "4_beats_oneq13",
                              "6_no_free_exit", "GO"}
    # the K10 book of the planted structure holds three $81.40 positions a session (the fourth would pass $300)
    table = series["table"]
    i = int(np.flatnonzero(table["name"] == f"P10|{Q.shape_name(PCS)}|09:45|H|ALL")[0])
    M = series["M"]
    assert M["k10_t"][i].all()
    rp = res["replay_10k"]["K10_cons_old_pdt"]
    assert rp["taken"] > 0 and rp["skipped_pdt"] > 0               # 2 series a spread: the second one is blocked
    assert 0 < res["stuck"]["share"] < 0.05


def test_end_to_end_earn_filter_and_comparators(synthetic):
    res, series = synthetic
    masks, P = series["masks"], series["P"]
    nm = P.names
    earn_days = pd.DatetimeIndex(nm.loc[masks["EARN"][0], "date"])
    assert len(earn_days) and set(nm.loc[masks["EARN"][0], "name"]) == {"AAA"}
    assert not (masks["NOEARN"][0] & masks["EARN"][0]).any()
    assert not masks["NOEARN"][0][nm["name"].isin(["DDD", "EEE"]).to_numpy()].any()   # no CIK: unknown
    nos = [c["no"] for c in res["comparators"]]
    assert nos == list(range(1, 23))


def test_orb_slot_h_takes_the_breakouts_own_closing_order():
    d = pd.Timestamp("2024-03-08")
    names = pd.DataFrame([name_row(d, 1, "AAA", 1, 1), name_row(d, 2, "BBB", 2, 2)])
    common = dict(credit=-1.0, den=1.007, fee_entry=0.007, n_entry=1, x_M2=8, x_M3=1, spr_entry=0.04)
    rows = pd.DataFrame([
        # a take-profit at minute 116: the exit order's fee, contracts and improvement are packed in slot M1
        one_row(0, 0, ORB, pnl_H=0.8, pnl_M1=0.8, pnl_M2=0.8, pnl_M3=0.8, x_M1=116, imp_M1=0.02, imp_M2=0.02,
                imp_M3=0.02, n_M1=1, n_M2=1, n_M3=1, n_flat=1, xfee_M1=0.70, **common),
        # never sold before 15:50: the flatten's fee in slot M1, its one order's surcharge in flat_surcharge
        one_row(1, 0, ORB, pnl_H=-0.2, pnl_M1=-0.2, pnl_M2=-0.2, pnl_M3=-0.2, x_M1=0, n_M1=1, n_M2=1, n_M3=1,
                n_flat=1, xfee_M1=0.70, flat_orders=1, flat_surcharge=0.35, **common)])
    P = Q.build_panel(names, rows)
    for m in range(4):
        assert abs(P.fee_exit[ORB, 0, m, 0] - 0.007) < 1e-12 and P.n_exit[ORB, 0, m, 0] == 1
        assert abs(P.touch[ORB, 0, m, 0] + 0.03) < 1e-12 and abs(P.cons[ORB, 0, m, 0] + 0.036) < 1e-12
    b = A.Bases(P).get(ORB)
    assert abs(b["sur"][0, 0, 0] * 100 - 0.70) < 1e-9 and abs(b["sur_leg"][0, 0, 0] * 100 - 0.70) < 1e-9
    assert abs(b["sur"][0, 0, 1] * 100 - 0.70) < 1e-9                # entry $0.35 + the flatten order's $0.35
    assert abs(b["fees"][0, 0, 0] * 100 - 1.40) < 1e-9


def test_no_free_exit_never_beats_the_conservative_series():
    d = pd.Timestamp("2024-03-08")
    names = pd.DataFrame([name_row(d, j + 1, nm, j + 1, j + 1) for j, nm in enumerate(("AAA", "BBB", "CCC"))])
    rows = pd.DataFrame([
        one_row(0, 0, PCS, pnl_H=-0.828, pnl_M1=-0.828, pnl_M2=-0.828, pnl_M3=-0.828, n_flat=2, fee_flat=0.014),
        one_row(1, 0, PCS, pnl_H=0.10, pnl_M1=0.10, pnl_M2=0.10, pnl_M3=0.10, n_flat=2, fee_flat=0.014, stuck=1),
        one_row(2, 0, LC, credit=-0.05, den=0.0555, fee_entry=0.0055, n_entry=1, pnl_H=-0.0555, pnl_M1=-0.0555,
                pnl_M2=-0.0555, pnl_M3=-0.0555)])
    P = Q.build_panel(names, rows)
    B = A.Bases(P)
    pcs, lc = B.get(PCS), B.get(LC)
    assert abs(pcs["conv"]["free"][0, 0, 0] - pcs["conv"]["cons"][0, 0, 0]) < 1e-12      # max loss, no stuck leg
    assert abs(pcs["conv"]["free"][0, 0, 1] - (pcs["conv"]["cons"][0, 0, 1] - 0.25)) < 1e-12
    assert abs(lc["conv"]["free"][0, 0, 2] - lc["conv"]["cons"][0, 0, 2]) < 1e-12        # a worthless lottery
    assert (pcs["conv"]["free"] <= pcs["conv"]["cons"] + 1e-12).all()


def test_week_ends_skip_a_week_cut_by_the_calendar_and_keep_holiday_thursdays():
    days = pd.DatetimeIndex(["2025-06-30", "2025-07-01", "2025-07-02", "2025-07-03", "2026-07-20", "2026-07-21",
                             "2026-07-22", "2026-07-23", "2026-07-24", "2026-07-27", "2026-07-28", "2026-07-29"])
    assert [str(x.date()) for x in A.week_ends(days)] == ["2025-07-03", "2026-07-24"]


def test_status2_close_falls_back_to_the_settlement_close():
    ymd = 240809
    head = [900_000_000_000_000 + ymd * 1000 + 3, 2 * 10**11, 0, 0, 100_500, 20_000, 0, 10**6, 1850]
    head += [0] * 19 + [0]
    vals = [950_000_000_000_000 + ymd * 100 + 1] + head + [970_000_000_000_000 + ymd * 1000 + 3, 98_500, 0, 99_010]
    names, recs, days = Q.parse_stream(vals, {3: "AMD"})
    assert names.loc[0, "status"] == 2 and names.loc[0, "S_close"] == 99.01
    nm = pd.DataFrame([name_row(pd.Timestamp("2024-08-09"), 3, "AMD", 0, 3, S_close=np.nan, next_open=98.5)])
    P = Q.build_panel(nm, pd.DataFrame([one_row(0, Q.NE, PCS, s_fill=100.0, nsh_flat=1)]))
    assert P.xpos[PCS, Q.NE, 0] == 0.0 and A.Bases(P).gap[0] == 0.0  # no close at all: no move, no unwind


def test_earnings_use_every_cik_of_a_ticker_and_the_breakouts_own_cutoff():
    days = pd.DatetimeIndex(["2015-07-15", "2015-07-16", "2015-07-17", "2015-07-20"])
    d = pd.Timestamp("2015-07-17")
    names = pd.DataFrame([name_row(d, 1, "GOOGL", 1, 1), name_row(d, 2, "BBB", 2, 2)])
    rows = pd.DataFrame([one_row(0, 0, ORB, credit=-1.0, den=1.007, x_M2=8, x_M3=1),
                         one_row(1, 0, ORB, credit=-1.0, den=1.007, x_M2=122, x_M3=1)])
    P = Q.build_panel(names, rows)
    t = pd.to_datetime(["2015-07-16 16:01", "2015-07-17 09:40", "2015-07-17 10:00"])
    rel = pd.DataFrame({"cik": ["1288776", "2", "2"], "form": "8-K", "t_et": t,
                        "d0": pd.to_datetime(["2015-07-17", "2015-07-17", "2015-07-17"])})
    ef = A.earnings_flags(P, rel, {"GOOGL": {"1652044", "1288776"}, "BBB": {"2"}}, days)
    assert ef["known"].tolist() == [True, True] and ef["react"].tolist() == [True, True]
    assert ef["earn"][0].tolist() == [True, True]                       # both released before 09:45
    # the breakout of GOOGL decides at 09:37 (after the release the day before); BBB's at 11:31, after 10:00 too
    assert ef["earn_orb"].tolist() == [True, True]
    rel.loc[1, "t_et"] = pd.Timestamp("2015-07-17 09:40")
    rows.loc[1, "x_M2"] = 8                                             # a 09:37 breakout before BBB's 09:40 release
    ef = A.earnings_flags(Q.build_panel(names, rows), rel.iloc[1:2], {"BBB": {"2"}}, days)
    assert not ef["earn_orb"][1] and ef["earn"][0, 1]


def test_ratio_fly_full_exit_pays_the_per_leg_minimum_by_its_own_leg_sizes():
    d = pd.Timestamp("2024-03-08")
    names = pd.DataFrame([name_row(d, 1, "AAA", 1, 1)])
    rows = pd.DataFrame([one_row(0, 0, PRF, credit=-0.10, den=1.10, fee_entry=0.042, n_entry=6, x_M2=200,
                                 n_M2=6, xfee_M2=4.20, pnl_M2=0.3)])
    b = A.Bases(Q.build_panel(names, rows)).get(PRF)
    # tier $0.65 a contract: legs of 1, 3 and 2 contracts -> only the 1-lot wing is below $1, entry and exit
    assert abs(b["sur_leg"][0, 2, 0] * 100 - 0.70) < 1e-9


def test_chunk_check_accepts_a_registered_chunk_rerun_in_month_halves():
    a, b = A.CHUNKS[0]
    halves = [(a, "2012-07-31"), ("2012-08-01", b)]
    assert A.chunk_problems(list(A.CHUNKS)) == []
    assert A.chunk_problems(halves + list(A.CHUNKS[1:])) == []
    probs = A.chunk_problems([(a, "2012-07-31"), ("2012-08-02", b)] + list(A.CHUNKS[1:]))      # a gap
    assert probs and "not covered" in probs[0]
    assert A.chunk_problems(list(A.CHUNKS) + [("2027-01-01", "2027-02-28")])[0].startswith("runs outside")
    assert A.chunk_problems(list(A.CHUNKS) + [A.CHUNKS[3]])[0].startswith("repeated")
    metas = formal_metas()
    metas[2]["hashes"]["sim"] = "0" * 16
    days = A.week_ends(pd.bdate_range("2012-01-02", "2026-07-31"))
    with pytest.raises(ValueError, match="sim"):
        A.check_inputs(metas, pd.DataFrame({"date": days}), pd.bdate_range("2012-01-02", "2026-07-31"), False)


def test_sharpe_shift_hits_the_target_exactly():
    rng = np.random.default_rng(3)
    traded = rng.random(700) < 0.27
    r = np.where(traded, rng.normal(-0.004, 0.01, 700), 0.0).astype(np.float32)
    for k in (0.4 / np.sqrt(52), 1.0 / np.sqrt(52)):
        x = r.astype(np.float64) + A.sharpe_shift(r, traded, k)
        assert abs(x.mean() / x.std() - k) < 1e-5 and (x[~traded] == 0).all()


def test_no_free_exit_does_not_lift_a_loss_already_below_the_floor():
    d = pd.Timestamp("2024-03-08")
    names = pd.DataFrame([name_row(d, 1, "AAA", 1, 1)])
    # a ratio fly closed leg by leg above the top strike: loss beyond -den - exit fees
    pnl = -(1.10 + 0.042) - 0.02
    rows = pd.DataFrame([one_row(0, 0, PRF, credit=-0.10, den=1.10, fee_entry=0.042, n_entry=6, pnl_H=pnl,
                                 pnl_M1=pnl, pnl_M2=pnl, pnl_M3=pnl, n_flat=6, fee_flat=0.042, width_frac=0.01)])
    b = A.Bases(Q.build_panel(names, rows)).get(PRF)
    assert abs(b["conv"]["free"][0, 0, 0] - b["conv"]["cons"][0, 0, 0]) < 1e-12


def test_yahoo_daily_reads_dividends_and_returns(monkeypatch):
    import quant.data.sources.sec_cache as sc
    ts = [int(pd.Timestamp(d + " 09:30", tz="America/New_York").timestamp()) for d in ("2014-06-05", "2014-06-06",
                                                                                       "2014-06-09", "2014-06-10")]
    chart = {"timestamp": ts, "indicators": {"adjclose": [{"adjclose": [10.0, 10.5, 10.5, 10.29]}]},
             "events": {"dividends": {"a": {"date": ts[1], "amount": 0.47}},
                        "splits": {"b": {"date": ts[2], "numerator": 7, "denominator": 1}}}}
    monkeypatch.setattr(sc, "yahoo_chart", lambda sym, **kw: chart if sym == "AAPL" else None)
    divs, rets = A.yahoo_daily(["AAPL", "GONE"])
    assert divs.attrs["missing"] == ["GONE"]
    assert divs.loc[0, "ex_date"] == pd.Timestamp("2014-06-06") and abs(divs.loc[0, "cash"] - 3.29) < 1e-9
    assert list(rets.index.strftime("%Y-%m-%d")) == ["2014-06-05", "2014-06-06", "2014-06-09", "2014-06-10"]
    assert abs(rets.loc["2014-06-06", "AAPL"] - 0.05) < 1e-12 and abs(rets.loc["2014-06-10", "AAPL"] + 0.02) < 1e-12
