"""Synthetic tests of the B4 forward lines (quant/observation/b4.py, b4_data.py, b4_megacap.py; checklist B4).

No vendor data, no network. Each line equals the research code on the same synthetic data (studies.sector_lev /
voltarget / bitcoin glue, quant.strategies.megacap functions), uses no data after the as-of date, keeps its final
rows when later data arrive, and the B1-B3 part of the log is unchanged when the B4 part is added."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quant.backtest import flagged_weights as fw
from quant.data import sector_etfs as etfs
from quant.observation import b4, b4_data as bdata, b4_megacap as bm
from quant.observation import health as hc
from quant.observation import runner as fo
from quant.strategies import bitcoin as bt
from quant.strategies import megacap as mc
from quant.strategies import registry
from quant.strategies import voltarget as vt

sys.path.insert(0, str(Path(__file__).parent))
import test_forward_observation as tfo  # noqa: E402
import test_forward_observation_health as th  # noqa: E402

CAL = tfo.SESSIONS                            # the synthetic B1-B3 data use business days 2024-01-02 .. 2027-01-29
M_SIGS = ("2026-10-30", "2026-11-30", "2026-12-31")
GROUP = {"S01": "G1", "S02": "G1"}           # a two-class company: one class only per signal


# ======================================================================== the synthetic environment


def btc_bodies(days, seed=9, bump: dict | None = None, zero_volume=()) -> tuple[dict, dict]:
    """(Bitstamp body, Coinbase body) for UTC ``days``; ``bump`` {day: factor} moves Bitstamp's close only."""
    rng = np.random.default_rng(seed)
    c = 60000 * np.exp(np.cumsum(rng.normal(0.0005, 0.03, len(days))))
    ts = [int(pd.Timestamp(x, tz="UTC").timestamp()) for x in days]
    bump = bump or {}
    bs = [{"timestamp": str(t), "open": f"{x:.2f}", "high": f"{x * 1.01:.2f}", "low": f"{x * 0.99:.2f}",
           "close": f"{x * bump.get(str(d.date()), 1.0):.2f}",
           "volume": "0" if str(d.date()) in zero_volume else "12.5"} for t, x, d in zip(ts, c, days)]
    cb = [[t, x * 0.99, x * 1.01, x, x * (1 + 1e-4), 100.0] for t, x in zip(ts, c)][::-1]
    return {"data": {"pair": "BTC/USD", "ohlc": bs}}, cb


def write_btc(b4dir: Path, last="2027-03-02", fetched="2027-03-03T03:30:00+00:00", **kw) -> None:
    d = b4dir / "btc"
    d.mkdir(parents=True, exist_ok=True)
    for p in d.glob("*.json"):
        p.unlink()
    bs, cb = btc_bodies(pd.date_range("2026-09-01", last, freq="D"), **kw)
    (d / "bitstamp_00.json").write_text(json.dumps(bs))
    (d / "coinbase_00.json").write_text(json.dumps(cb))
    (d / "fetched.json").write_text(json.dumps({"bitstamp": {"fetched_utc": fetched, "pages": 1},
                                                "coinbase": {"fetched_utc": fetched, "pages": 1}}))


def write_dtb3(b4dir: Path, last="2027-03-31") -> None:
    days = pd.bdate_range("2026-01-02", last)
    (b4dir / "DTB3.csv").write_text("observation_date,DTB3\n" + "".join(
        f"{d.date()},{3.9 - 0.002 * k:.3f}\n" for k, d in enumerate(days)))


def m_base() -> pd.DataFrame:
    b = tfo.smisp_base()
    b["multi_class_group"] = b["ticker"].map(GROUP)
    return b


def m_facts(cik, seed, future=False):
    return tfo.facts(cik, seed, future)


def m_signal(s, charts: Path, future=False):
    fx = {cik: m_facts(cik, seed, future) for _, cik, seed in tfo.names()}
    return bm.compute_signal(s, m_base(), charts, None, lambda ciks: {c: fx[c] for c in ciks}, CAL,
                             lists=tfo.empty_lists(), alias={})


def write_m_charts(d: Path, mutate_after=None, stop: dict | None = None) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    for sym, _, seed in tfo.names():
        body = tfo.chart(sym, seed, mutate_after=mutate_after, stop=(stop or {}).get(sym))
        (d / f"{sym}.json.gz").write_bytes(__import__("gzip").compress(json.dumps(body).encode()))
    return d


def tracked(sym: str, base: np.ndarray, seed: int) -> dict:
    """A Yahoo body whose day returns are ``base`` (QLD: 2 x QQQ, IBIT: BTC) plus small noise, no dividends."""
    rng = np.random.default_rng(seed)
    r = np.nan_to_num(base) + rng.normal(0, 0.001, len(base))
    c = 50 * np.cumprod(1 + r)
    o = c * np.exp(rng.normal(0, 0.005, len(c)))
    h, lo = np.maximum(o, c) * 1.005, np.minimum(o, c) * 0.995
    ts = [int(pd.Timestamp(d.date(), tz="America/New_York").replace(hour=9, minute=30).timestamp()) for d in CAL]
    q = {"open": list(o), "high": list(h), "low": list(lo), "close": list(c), "volume": [1e6] * len(c)}
    return {"chart": {"result": [{"timestamp": ts, "meta": {"dataGranularity": "1d", "symbol": sym},
                                  "indicators": {"quote": [q], "adjclose": [{"adjclose": list(c)}]}, "events": {}}]}}


def write_legs(raw: Path, edit=None) -> Path:
    """QLD = 2 x QQQ day returns, IBIT = the BTC session returns (from 2026-09-02), each plus 0.1% noise: the B4
    tracking checks (health_b4.track_items) hold on the synthetic data."""
    raw.mkdir(parents=True, exist_ok=True)
    qqq = hc.chart_frame(raw / "QQQ.json")["adjclose"].reindex(CAL)
    days = pd.date_range("2026-09-01", "2027-03-02", freq="D")
    bs, _ = btc_bodies(days)
    btc = pd.Series([float(x["close"]) for x in bs["data"]["ohlc"]], index=days).reindex(CAL)
    rng = np.random.default_rng(6)
    ib = btc.pct_change().to_numpy()
    ib = np.where(np.isfinite(ib), ib, rng.normal(0, 0.03, len(CAL)))
    for sym, base, seed in (("QLD", 2 * qqq.pct_change().to_numpy(), 5), ("IBIT", ib, 6)):
        p = tracked(sym, base, seed)
        if edit:
            edit(sym, p)
        (raw / f"{sym}.json").write_text(json.dumps(p))
    return raw


def make_b4_env(root: Path, sigs=M_SIGS) -> dict:
    """The B1-B3 synthetic environment of the health tests plus QLD / IBIT, BTC, the T-bill, the frozen mega-cap
    signals and their held charts."""
    env = tfo.make_smisp_env(root)
    th.add_meta(env["smisp"] / "state")
    env["cal"] = CAL
    write_legs(env["raw"])
    b4dir = root / "b4"
    write_btc(b4dir)
    write_dtb3(b4dir)
    charts = write_m_charts(root / "mcharts")
    state = root / "mstate"
    for s in sigs:
        sig, meta, _ = m_signal(s, charts)
        bm.write_signal(s, sig, meta, state)
    held = b4dir / "megacap" / "held"
    held.mkdir(parents=True)
    for p in charts.glob("*.json.gz"):
        (held / p.name).write_bytes(p.read_bytes())
    env.update({"b4": b4dir, "mstate": state, "mcharts": charts, "held": held})
    return env


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    return make_b4_env(tmp_path_factory.mktemp("b4"))


def compute(env, as_of, raw=None, b4dir=None, state=None):
    return b4.compute(as_of, raw or env["raw"], b4dir or env["b4"], state or env["mstate"], env["smisp"])


def started(res):
    return {k: v for k, v in res["lines"].items() if v.get("started")}


# ======================================================================== the frozen values = the studies'


def test_registry_values_equal_the_studies():
    from studies import bitcoin as sb
    from studies import sector_lev as ssl
    from studies import voltarget as svt
    lev, v3, b = (registry.get(k).frozen for k in ("LEV-B3", "V3", "BT4b"))
    assert ssl.B_EXPOSURE["B3"] == (lev["on"], lev["off"]) and (ssl.SMA_LEN, ssl.BAND) == (lev["sma"], lev["band"])
    assert b4.V3_PARAMS == svt.PARAMS and svt.CAP["V3"] == v3["cap"]
    assert (sb.BTC_HS, sb.QQQ_HS, sb.ONEQ_HS, sb.ETF_BTC_HS) == (b["btc_half_spread"], b["qqq_half_spread"],
                                                                 b["oneq_half_spread"], b["etf_btc_half_spread"])
    assert bt.SPEC["BT4b"].sleeve == b["sleeve"] and bt.SPEC["BT4b"].family == "sleeve"
    assert [r.name for r in bm.RULES] == ["M1", "M2", "M3", "M6"] and bm.RULES == tuple(
        r for r in mc.RULES if r.name in ("M1", "M2", "M3", "M6"))
    # the V3 cap is the research function's: 1.5
    r = pd.Series(np.r_[np.tile([0.03, -0.03], 150), np.tile([0.001, -0.001], 20)],
                  index=pd.bdate_range("2020-01-01", periods=340))
    e, _ = vt.daily_exposure("V3", r, (1 + r).cumprod(), b4.V3_PARAMS)
    assert e.max() == pytest.approx(1.5)
    # the mega-cap registry entry and the health module transcribe the same rules
    from quant.observation import health_b4 as hb
    for k, p in hb.M_RULES.items():
        assert registry.MEGACAP[k]["pool"] == p["pool"] and registry.MEGACAP[k]["hold"] == p["hold"]
    assert (hb.LEV_ON, hb.LEV_OFF, hb.LEV_SMA, hb.LEV_BAND) == (lev["on"], lev["off"], lev["sma"], lev["band"])
    assert (hb.V3_CAP, hb.V3_SHORT, hb.V3_LR_MIN, hb.BT_SLEEVE) == (v3["cap"], v3["vol_short"], v3["vol_lr_min"],
                                                                   b["sleeve"])
    assert (hb.START_CLOSE, hb.FIRST_SIGNAL) == (registry.B4_START_CLOSE, registry.B4_FIRST_SIGNAL)


# ======================================================================== window and status


def test_before_the_start_nothing_runs(env):
    res = compute(env, "2026-10-23")
    assert not started(res)
    assert all(not v.get("error") for v in res["lines"].values()), res["lines"]
    text, warnings = b4.render(res)
    assert "**opens at the 2026-10-26 close**" in text
    assert "observation starts at the 2026-10-30 month-end signal" in text
    assert fo.parse_rows(text) == {k: {} for k in b4.KEYS} and not warnings
    res = compute(env, "2026-10-26")            # the opening close itself: no return session yet
    assert not started(res)


def test_daily_lines_open_at_the_2026_10_26_close(env):
    res = compute(env, "2026-10-30")
    for k in ("LEV-B3", "V3"):
        v = res["lines"][k]
        assert v["started"] and v["ret"].index[0] == pd.Timestamp("2026-10-27"), k
        assert v["sim"]["value"].index[0] == pd.Timestamp("2026-10-26")
        assert v["sim"]["value"].iloc[0] < 10_000 and v["sim"]["value"].iloc[0] > 9_990   # bought at that close
    # the monthly lines: the 10-30 signal is known only once 11-02 exists, traded at that close
    assert not any(res["lines"][k].get("started") for k in (*b4.MEGA, "BT4b"))
    res = compute(env, "2026-11-03")
    for k in (*b4.MEGA, "BT4b"):
        v = res["lines"][k]
        assert v["started"] and v["ret"].index[0] == pd.Timestamp("2026-11-03"), k


def test_month_rows_status_and_counts(env):
    res = compute(env, "2027-01-29")
    for k, v in started(res).items():
        rows = v["rows"]
        assert [r["status"] for r in rows][-1] == "provisional" and all(r["status"] == "final" for r in rows[:-1])
        first = "2026-10" if k in b4.DAILY else "2026-11"
        assert rows[0]["month"] == first and rows[-1]["month"] == "2027-01", k
        assert [r["n"] for r in rows] == list(range(1, len(rows) + 1))
        assert np.isfinite(rows[-1]["t_oneq"]) and rows[1]["t_oneq"] != rows[1]["t_oneq"]   # t from n = 3


# ======================================================================== the research code on the same data


def research_data(legs) -> etfs.Data:
    """The study's legs (quant.data.sector_etfs.build_legs) from the same forward files: IEF / ^IXIC / ^NDX stand-ins
    only feed legs the B4 lines never hold."""
    adj = legs.adj[["QQQ", "ONEQ", "QLD"]].assign(IEF=legs.adj["ONEQ"])
    close = legs.close[["QQQ", "ONEQ", "QLD"]].assign(IEF=legs.close["ONEQ"])
    rets, px, _ = etfs.build_legs(adj, close, legs.adj["ONEQ"], legs.rf)
    return etfs.Data(sessions=legs.sessions, adj=adj, rets=rets, close=px,
                     p_qqq=(1 + legs.adj["QQQ"].pct_change(fill_method=None).fillna(0)).cumprod(),  # chain_qqq
                     rf=legs.rf, raw={}, guard={})                                     # (after QQQ lists)


def test_lev_b3_and_v3_equal_the_research_simulation(env):
    from studies import sector_lev as ssl
    from studies import voltarget as svt
    res = compute(env, "2027-01-29")
    d = research_data(res["legs"])
    v = res["lines"]["LEV-B3"]
    w, flag = ssl.targets("B3", d)
    sim = fw.simulate(w, flag, d.rets, d.close, v["s_i"])
    pd.testing.assert_series_equal(v["ret"], sim["ret"], check_names=False, rtol=1e-12, atol=1e-14)
    cw, cf = vt.constant_targets(1.37, d.sessions)
    pd.testing.assert_series_equal(v["match"], fw.simulate(cw, cf, d.rets, d.close, v["s_i"])["ret"],
                                   check_names=False, rtol=1e-12, atol=1e-14)
    pd.testing.assert_series_equal(v["oneq"], fw.buy_hold("BENCH", d, v["s_i"])["ret"], check_names=False)
    assert set(v["state"].iloc[v["s_i"]:].dropna()) <= {0.0, 1.0}
    # V3: the study's targets and engine; the last month's comparison = the study's constant at the window's e-bar
    v = res["lines"]["V3"]
    w, flag, _ = vt.targets("V3", d, svt.PARAMS)
    sim = fw.simulate(w, flag, d.rets, d.close, v["s_i"])
    pd.testing.assert_series_equal(v["ret"], sim["ret"], check_names=False, rtol=1e-12, atol=1e-14)
    e_bar = float(vt.held_exposure(sim["held"]).mean())
    cw, cf = vt.constant_targets(e_bar, d.sessions)
    c = fw.simulate(cw, cf, d.rets, d.close, v["s_i"])["ret"]
    last = v["ret"].index[-1].to_period("M")
    pd.testing.assert_series_equal(v["match"][v["match"].index.to_period("M") == last],
                                   c[c.index.to_period("M") == last], check_names=False, rtol=1e-12)
    assert v["ebar"][str(last)] == pytest.approx(e_bar)
    # the exposures are those of the rule: LEV-B3 1.0 / 1.5 after trades, V3 within [0, 1.5] targets
    e = res["lines"]["V3"]["e_t"].dropna()
    assert (e <= 1.5 + 1e-12).all() and (e > 0).all()


def test_bt4b_equals_the_research_sleeve(env):
    from studies import bitcoin as sb
    res = compute(env, "2027-01-29")
    v = res["lines"]["BT4b"]
    d = research_data(res["legs"])
    ses = v["sessions"]
    rets, prices = sb.legs_sessions(res["btc"].close, d, ses)
    w, flag = bt.sleeve_targets(bt.SPEC["BT4b"], ses)
    sim = bt.simulate(w, flag, rets, prices, v["s_i"], sb.cost_fns(), monthly_fee=0.15)
    pd.testing.assert_series_equal(v["ret"], sim["ret"], check_names=False, rtol=1e-12, atol=1e-14)
    pd.testing.assert_series_equal(v["oneq"], bt.buy_hold("ONEQ", rets, prices, v["s_i"], sb.cost_fns())["ret"],
                                   check_names=False)
    assert sim["fee_dollars"] == pytest.approx(0.15 * 2)          # December and January
    # report only: IBIT as the BTC leg (the study's run_sleeve(..., etf=IBIT) path)
    etf = {"ret": res["legs"].rets["IBIT"].reindex(ses), "close": res["legs"].close["IBIT"].reindex(ses)}
    r2, p2 = sb.legs_sessions(res["btc"].close, d, ses, etf_btc=etf["ret"])
    s2 = bt.simulate(w, flag, r2, p2, v["s_i"], sb.cost_fns(etf_close=etf["close"]), monthly_fee=0.15)
    pd.testing.assert_series_equal(v["ibit"], s2["ret"], check_names=False, rtol=1e-12, atol=1e-14)
    # the BTC close of session S is the UTC-day-S candle (weekends fall into Monday's return)
    p = res["btc"].close
    mon = pd.Timestamp("2026-11-09")
    assert v["btc_ret"][mon] == pytest.approx(p[mon] / p[pd.Timestamp("2026-11-06")] - 1)


def test_megacap_lines_equal_the_research_engine(env):
    res = compute(env, "2027-01-29")
    legs = res["legs"]
    sigs, pending = b4.frozen_signals(legs.sessions, env["mstate"])
    assert pending is None and [str(s.date()) for s in sigs] == list(M_SIGS)
    full = {}
    for s in M_SIGS:
        _, _, f = m_signal(s, env["mcharts"])
        full[pd.Timestamp(s)] = f["targets"]
    for k in b4.MEGA:
        v = res["lines"][k]
        tg = {s: full[s][k] for s in full}
        grid = legs.sessions[(legs.sessions >= min(tg)) & (legs.sessions <= v["last_session"])]
        fr = bm.frames_of(sigs, env["held"], grid, str(v["last_session"].date()))
        sim = mc.simulate(tg, grid, fr["idx"], fr["close"], fr["last_row"], legs.adj["QQQ"], legs.close["QQQ"])
        pd.testing.assert_series_equal(v["nav"], sim["nav"], check_names=False, rtol=1e-10)
        assert {s: [x[0] for x in t] for s, t in v["targets"].items()} == {s: [x[0] for x in t] for s, t in tg.items()}
        oneq = mc.buy_hold(legs.adj["ONEQ"], legs.close["ONEQ"], sim["nav"].index, mc.ONEQ_HS)
        pd.testing.assert_series_equal(v["oneq"], (oneq / oneq.shift(1) - 1).iloc[1:], check_names=False)


def test_megacap_signal_rules_and_one_class_per_company(env):
    sig, meta, full = m_signal("2026-11-30", env["mcharts"])
    f = full["full"]
    assert not ({"S01", "S02"} <= set(f["ticker"]))               # one class of the two-class company
    assert list(sig["mcap_rank"]) == list(range(1, len(sig) + 1)) and sig["mcap_rel"].iloc[0] == 1.0
    top = f.sort_values(["mcap", "security_id"], ascending=[False, True])
    assert set(sig.loc[sig["w_M1"] > 0, "security_id"]) == set(top["security_id"].head(10))
    assert np.allclose(sig.loc[sig["w_M1"] > 0, "w_M1"], 0.1) and np.allclose(sig.loc[sig["w_M2"] > 0, "w_M2"], 0.2)
    m6 = sig[sig["w_M6"] > 0].set_index("security_id")["w_M6"]
    mc6 = top.head(10).set_index("security_id")["mcap"]
    assert np.allclose(m6.sort_index(), (mc6 / mc6.sum()).sort_index(), rtol=1e-10)
    pool = top.head(20).dropna(subset=["mom_m6"]).sort_values(["mom_m6", "security_id"], ascending=[False, True])
    assert set(sig.loc[sig["w_M3"] > 0, "security_id"]) == set(pool["security_id"].head(5))
    assert meta["targets"]["M1"] == list(top["ticker"].head(10))
    assert "mcap" not in sig.columns                              # no dollar levels in the committed file
    assert (f["mcap_src"] == "sec_shares").mean() > 0.9


# ======================================================================== no look-ahead, idempotency, final rows


def test_megacap_signal_no_lookahead(env, tmp_path):
    s = "2026-10-30"                             # (the synthetic future 10-K and restatement are filed 2026-11-10)
    a, ma, _ = m_signal(s, env["mcharts"])
    wild = write_m_charts(tmp_path / "wild", mutate_after=s)
    b, mb, _ = m_signal(s, wild, future=True)    # later prices wild, a 10-K filed after s, a restatement after s
    pd.testing.assert_frame_equal(a, b)
    assert ma["targets"] == mb["targets"]


def mutate_after(day):
    cut = pd.Timestamp(day, tz="America/New_York").timestamp() + 86400 - 1

    def edit(sym, p):
        res = p["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        for k, t in enumerate(res["timestamp"]):
            if t > cut:
                f = 0.5 if k % 2 else 1.7
                for key in ("open", "high", "low", "close"):
                    q[key][k] *= f
                res["indicators"]["adjclose"][0]["adjclose"][k] *= f
    return edit


def test_no_lookahead_future_rows_do_not_matter(env, tmp_path):
    as_of = "2026-12-15"
    a = compute(env, as_of)
    raw = tmp_path / "raw"
    raw.mkdir()
    for p in env["raw"].glob("*.json"):                          # every chart wild after the as-of date
        body = json.loads(p.read_text())
        mutate_after(as_of)(p.stem, body)
        (raw / p.name).write_text(json.dumps(body))
    b4dir = tmp_path / "b4"
    write_btc(b4dir, bump={str(d.date()): 3.0 for d in pd.date_range("2026-12-16", "2027-03-02")})
    write_dtb3(b4dir)
    held = write_m_charts(b4dir / "megacap" / "held", mutate_after=as_of)
    assert held.exists()
    b = compute(env, as_of, raw=raw, b4dir=b4dir)
    assert set(started(a)) == set(started(b)) == set(b4.KEYS)
    for k in b4.KEYS:
        assert [b4.row_line(k, r) for r in a["lines"][k]["rows"]] == [b4.row_line(k, r) for r in b["lines"][k]["rows"]]
        pd.testing.assert_series_equal(a["lines"][k]["ret"], b["lines"][k]["ret"])
    assert b["legs"].sessions[-1] == pd.Timestamp(as_of) and b["btc"].close.index[-1] == pd.Timestamp(as_of)


def test_final_rows_do_not_change_when_later_data_arrive(env):
    early, late = compute(env, "2026-12-31"), compute(env, "2027-01-29")
    n = 0
    for k, v in started(early).items():
        later = {r["month"]: r for r in late["lines"][k]["rows"]}
        for r in v["rows"]:
            if r["status"] == "final":
                assert b4.row_line(k, later[r["month"]]) == b4.row_line(k, r), (k, r["month"])
                n += 1
    assert n == 2 * 2 + 5      # Oct + Nov of the two daily lines, Nov of the five monthly lines


def test_a_missing_signal_file_stops_the_megacap_lines(env, tmp_path):
    state = tmp_path / "mstate"
    state.mkdir()
    for p in env["mstate"].glob("m_2026-10-30*"):
        (state / p.name).write_bytes(p.read_bytes())
    res = compute(env, "2026-12-15", state=state)
    for k in b4.MEGA:
        v = res["lines"][k]
        assert v["started"] and v["last_session"] == pd.Timestamp("2026-11-30") and v["pending"] == pd.Timestamp(
            "2026-11-30")
        assert v["rows"][-1]["month"] == "2026-11" and v["rows"][-1]["status"] == "provisional"
    text, _ = b4.render(res)
    assert "Pending: signal 2026-11-30 not computed" in text
    assert b4.line_ends(res)["M1"] == pd.Timestamp("2026-11-30")


def test_bt4b_waits_for_the_btc_close_of_the_session(env, tmp_path):
    b4dir = tmp_path / "b4"
    write_btc(b4dir, last="2026-12-15", fetched="2026-12-15T23:30:00+00:00")   # the 12-15 candle still running
    write_dtb3(b4dir)
    (b4dir / "megacap").mkdir()
    (b4dir / "megacap" / "held").symlink_to(env["held"], target_is_directory=True)
    res = compute(env, "2026-12-15", b4dir=b4dir)
    v = res["lines"]["BT4b"]
    assert res["btc"].close.index[-1] == pd.Timestamp("2026-12-14") and v["last_session"] == pd.Timestamp("2026-12-14")
    assert res["lines"]["LEV-B3"]["last_session"] == pd.Timestamp("2026-12-15")


def test_btc_zero_volume_day_is_filled_by_the_registered_rule(env, tmp_path):
    b4dir = tmp_path / "b4"
    write_btc(b4dir, zero_volume=("2026-11-20",), bump={"2026-11-20": 1.5})
    btc = bdata.load_btc(b4dir, "2026-12-31")
    assert btc.info["filled_days"] == {"2026-11-20": "coinbase"}
    cb = btc.sources.close["coinbase"]
    assert btc.close[pd.Timestamp("2026-11-20")] == cb[pd.Timestamp("2026-11-20")]


def test_a_broken_line_does_not_stop_the_others(env, tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    for p in env["raw"].glob("*.json"):
        if p.name != "QLD.json":
            (raw / p.name).write_bytes(p.read_bytes())
    res = compute(env, "2026-12-31", raw=raw)
    assert res["lines"]["LEV-B3"].get("error") and res["lines"]["V3"].get("error")
    assert res["lines"]["BT4b"]["started"] and res["lines"]["M1"]["started"]
    text, _ = b4.render(res, old_text=b4.render(compute(env, "2026-12-31"))[0])
    assert "**not computed this run**" in text and fo.parse_rows(text)["LEV-B3"]      # earlier rows kept


# ======================================================================== the runner and the log file


def test_runner_writes_b1_b3_unchanged_then_b4_and_is_idempotent(env, tmp_path):
    kw = {"raw_dir": env["raw"], "stocks": tfo.STOCKS, "smisp_dir": env["smisp"]}
    log0, log1 = tmp_path / "a.md", tmp_path / "b.md"
    fo.run("2026-12-31", log_path=log0, **kw)
    cfg = {"b4_dir": env["b4"], "state_dir": env["mstate"]}
    fo.run("2026-12-31", log_path=log1, b4=cfg, **kw)
    t0, t1 = log0.read_text(), log1.read_text()
    assert t1.startswith(t0) and "## B4: seven candidates added 2026-10-24" in t1[len(t0):]
    rows = fo.parse_rows(t1)
    assert set(rows) == {"B1", "SEL-A", "SEL-P", "B3", *b4.KEYS}
    assert sorted(rows["M1"]) == ["2026-11", "2026-12"] and sorted(rows["LEV-B3"]) == ["2026-10", "2026-11", "2026-12"]
    fo.run("2026-12-31", log_path=log1, b4=cfg, **kw)
    assert log1.read_text() == t1                                   # byte-identical rerun
    fo.run("2027-01-29", log_path=log1, b4=cfg, **kw)
    t2 = log1.read_text()
    r2 = fo.parse_rows(t2)
    for k in b4.KEYS:
        assert r2[k]["2026-11"] == rows[k]["2026-11"]               # a final row is never rewritten
        assert r2[k]["2026-12"].endswith("| final |") and r2[k]["2027-01"].endswith("| provisional |")
    assert t2.count("<!-- table:M1:start -->") == 1
    # the B4 status, the log-status check and the run details
    assert hc.check_log_status(t2, "2027-01-29", b4.line_ends(fo.compute_all(
        "2027-01-29", env["raw"], tfo.STOCKS, env["smisp"], b4=cfg)["b4"])).status == hc.OK
    res = fo.compute_all("2027-01-29", env["raw"], tfo.STOCKS, env["smisp"], b4=cfg)
    det = fo.run_details(res)
    assert set(det["b4"]["lines"]) == set(b4.KEYS) and det["b4"]["series"]["LEV-B3.match"]


def test_no_price_levels_in_the_b4_log_or_state(env, tmp_path):
    res = compute(env, "2027-01-29")
    text, _ = b4.render(res)
    legs = res["legs"]
    levels = {f"{x:.2f}" for c in ("QQQ", "ONEQ", "QLD", "IBIT") for x in legs.close[c].dropna().iloc[-40:]}
    levels |= {f"{x:.2f}" for x in res["btc"].close.iloc[-40:]}
    for lv in levels:
        assert lv not in text
    for p in env["mstate"].glob("m_*.csv"):
        sig = pd.read_csv(p)
        assert not {"mcap", "close", "price"} & set(sig.columns)
        assert sig["mcap_rel"].max() <= 1.0 and (sig["mcap_rel"] > 0).all()


def test_btc_fetch_pages_and_running_candle(tmp_path):
    """Bitstamp 1000 / Coinbase 300 candles a request; a failed source keeps its previous copy; the fetch day's
    candle is dropped as running."""
    urls = b4.bdata.btc_urls("2026-09-01", "2029-10-31")
    assert len(urls["bitstamp"]) == 2 and len(urls["coinbase"]) == 4 and len(urls["kraken"]) == 1
    days = pd.date_range("2026-09-01", "2026-10-24", freq="D")
    bs, cb = btc_bodies(days)

    def getter(url, headers):
        if "bitstamp" in url:
            return json.dumps(bs).encode()
        if "coinbase" in url:
            return json.dumps(cb).encode()
        if "kraken" in url:
            raise OSError("down")
        return json.dumps({"chart": {"result": [{"timestamp": []}]}}).encode()
    now = pd.Timestamp("2026-10-24 01:30", tz="UTC").to_pydatetime()
    out = bdata.fetch_btc(tmp_path, getter=getter, sleep=lambda s: None, now=now)
    assert out["bitstamp"] == "ok (1)" and out["coinbase"] == "ok (1)" and out["kraken"].startswith("error")
    assert out["yahoo"].startswith("error")
    btc = bdata.load_btc(tmp_path, "2026-10-24")
    assert btc.close.index[-1] == pd.Timestamp("2026-10-23")      # the 10-24 candle was still running
    assert math.isclose(btc.close.iloc[-1], float(bs["data"]["ohlc"][-2]["close"]))
