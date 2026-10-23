"""Tests of the fixes after the 2026-10-24 reviews of the B4 lines (faithfulness replay, fault injection, data and
workflow), before any forward data: the mega-cap signal's series starts, share-count ties, raw volume and real-close
check; the isolation of B4 from B1-B3; the detectors that missed faults or raised false alarms; the shared Yahoo pace,
the Tiingo key header and the shorter QLD / IBIT fetch. Synthetic data, no network."""
from __future__ import annotations

import copy
import json
import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quant.observation import b4, b4_data as bdata, b4_megacap as bm
from quant.observation import daily
from quant.observation import health as hc
from quant.observation import health_b4 as hb
from quant.observation import prices as fp
from quant.observation import runner as fo
from quant.observation import smisp as fs

sys.path.insert(0, str(Path(__file__).parent))
import test_forward_observation as tfo  # noqa: E402
import test_forward_observation_b4 as tb  # noqa: E402
import test_forward_observation_b4_health as tbh  # noqa: E402
import test_forward_observation_health as th  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CAL = tb.CAL
T = pd.Timestamp("2026-12-31")


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    return tb.make_b4_env(tmp_path_factory.mktemp("b4fix"))


@pytest.fixture(scope="module")
def res(env):
    return tb.compute(env, "2026-12-31")


def facts_for():
    fx = {cik: tb.m_facts(cik, seed) for _, cik, seed in tfo.names()}
    return lambda ciks: {c: fx[c] for c in ciks}


def signal(env, s, **kw):
    return bm.compute_signal(s, tb.m_base(), env["mcharts"], None, facts_for(), CAL, lists=tfo.empty_lists(),
                             alias={}, **kw)


def signals(env) -> dict:
    return {pd.Timestamp(s): bm.load_signal(bm.signal_path(s, env["mstate"])) for s in tb.M_SIGS}


# ======================================================================== the mega-cap signal (faithfulness review)


def test_share_fact_ties_keep_the_shortest_period_whatever_the_order():
    rows = [(1, 0, "2026-06-30", "2026-08-01", 801e6, "2026-01-01"),     # 10-Q: year to date
            (1, 0, "2026-06-30", "2026-08-01", 744e6, "2026-04-01"),     # 10-Q: the quarter
            (1, 2, "2026-07-25", "2026-08-01", 750e6, ""),
            (2, 0, "2026-06-30", "2026-08-01", 5e6, "2026-04-01")]
    a = bm.share_frame(rows)
    for k in range(4):                                  # the other companies in the frame do not matter
        pd.testing.assert_frame_equal(a, bm.share_frame(rows[k:] + rows[:k]))
    assert a.loc[(a["cik"] == 1) & (a["pri"] == 0), "val"].tolist() == [744e6] and len(a) == 3
    payload = {"facts": {"us-gaap": {"WeightedAverageNumberOfDilutedSharesOutstanding": {"units": {"shares": [
        {"end": "2026-06-30", "filed": "2026-08-01", "val": 801e6, "start": "2026-01-01"},
        {"end": "2026-06-30", "filed": "2026-08-01", "val": 744e6, "start": "2026-04-01"},
        {"end": "2026-09-30", "filed": "2026-10-30", "val": 1e9, "start": "2026-07-01"}]}}}}}
    f = bm.share_facts({7: payload}, "2026-10-30")       # filed strictly before s: the 10-30 filing is left out
    assert f["val"].tolist() == [744e6]


def test_universe_dollar_volume_uses_the_raw_volume():
    idx = pd.bdate_range("2026-11-02", periods=4)
    close = pd.DataFrame({"A": [100.0, 100.0, 50.0, 50.0]}, index=idx)            # a 2:1 split on day 3
    adj = pd.DataFrame({"A": [50.0, 50.0, 50.0, 50.0]}, index=idx)
    vol = pd.DataFrame({"A": [2e6, 2e6, 2e6, 2e6]}, index=idx)                     # split-adjusted volume
    fr = bm.raw_volume({"close": close, "close_adj": adj, "volume": vol})
    assert fr["volume"]["A"].tolist() == [1e6, 1e6, 2e6, 2e6]
    assert (fr["close"] * fr["volume"])["A"].tolist() == [1e8] * 4             # real close x raw volume


def test_a_late_listing_has_no_momentum_before_its_series_start(env):
    s = "2026-11-30"
    sig0, _, _ = signal(env, s)
    sid = sig0.loc[sig0["w_M3"] > 0, "security_id"].iloc[0]
    tick = sig0.set_index("security_id").loc[sid, "ticker"]
    sig1, meta1, _ = signal(env, s, starts={sid: pd.Timestamp("2026-09-01")})
    a, b_ = sig0.set_index("security_id"), sig1.set_index("security_id")
    assert np.isnan(b_.loc[sid, "mom_m6"]) and b_.loc[sid, "w_M3"] == 0.0
    assert meta1["series_start_cut"] == {tick: "2026-09-01"} and tick not in meta1["targets"]["M3"]
    others = [x for x in a.index if x != sid and x in b_.index]
    pd.testing.assert_series_equal(a.loc[others, "mom_m6"], b_.loc[others, "mom_m6"])
    # a start before the window cuts nothing
    _, meta2, _ = signal(env, s, starts={sid: pd.Timestamp("2020-01-01")})
    assert meta2["series_start_cut"] == {}


def test_a_split_after_the_signal_is_corrected_from_the_raw_close(env):
    s = "2026-11-30"
    sig0, meta0, full0 = signal(env, s)
    f0 = full0["full"].set_index("security_id")
    sid = f0.index[2]
    tick, yahoo = f0.loc[sid, "ticker"], f0.loc[sid, "yahoo"]
    chart = fs.load_chart(env["mcharts"] / f"{yahoo}.json.gz", s)["close"].iloc[-1]
    _, meta1, full1 = signal(env, s, raw_close={sid: 2 * chart})       # the bars hold a 2:1 split after s
    assert meta1["real_close_split_corrected"] == {tick: 2.0} and meta1["real_close_unexplained"] == {}
    assert full1["full"].set_index("security_id").loc[sid, "mcap"] / f0.loc[sid, "mcap"] == pytest.approx(2.0)
    _, meta2, full2 = signal(env, s, raw_close={sid: 1.07 * chart})    # not a split ratio: named, not applied
    assert meta2["real_close_unexplained"] == {tick: 1.07} and meta2["real_close_split_corrected"] == {}
    assert full2["full"].set_index("security_id").loc[sid, "mcap"] == f0.loc[sid, "mcap"]
    _, meta3, full3 = signal(env, s, raw_close={sid: 1.01 * chart})    # within 2%: nothing
    assert meta3["real_close_unexplained"] == {} and meta0["series_start_cut"] == {}


def test_check_real_close_asks_alpaca_raw_bars_once(tmp_path):
    base = pd.DataFrame({"security_id": ["1", "2"], "ticker": ["AAA", "BBB"], "eligible": [True, True]})
    calls = []

    def alp(url, headers):
        calls.append(url)
        return json.dumps({"bars": {"AAA": [{"t": "2026-11-30T05:00:00Z", "o": 1, "h": 1, "l": 1, "c": 123.45,
                                             "v": 10}]}, "next_page_token": None}).encode()
    g = {"alpaca_headers": {"APCA-API-KEY-ID": "k"}, "alpaca": alp}
    assert bm.check_real_close(base, "2026-11-30", tmp_path, g, sleep=lambda s: None) == "checked"
    assert len(calls) == 1 and "adjustment=raw" in calls[0] and bm.load_raw_close(tmp_path) == {"1": 123.45}
    assert bm.check_real_close(base, "2026-11-30", tmp_path, g) == "checked" and len(calls) == 1   # done once
    assert bm.check_real_close(base, "2026-11-30", tmp_path / "x", {"alpaca_headers": None}) == \
        "not checked: no Alpaca keys"

    def down(url, headers):
        raise OSError("down")
    note = bm.check_real_close(base, "2026-11-30", tmp_path / "y", {"alpaca_headers": {"k": "v"}, "alpaca": down})
    assert note.startswith("not checked: Alpaca OSError") and bm.load_raw_close(tmp_path / "y") is None


def test_first_listed_records_a_later_listing_once(tmp_path):
    state = tmp_path / "st"
    state.mkdir()
    pd.DataFrame({"security_id": ["A"], "ticker": ["AAA"], "first_listed": ["2025-06-06"],
                  "source": ["v2_weekly_listed"]}).to_csv(state / bm.FIRST_LISTED_NAME, index=False)
    base = pd.DataFrame({"security_id": ["A", "B", "C", "D"], "ticker": ["AAA", "BBB", "CCC", "DDD"],
                         "eligible": [True, True, True, False], "source": ["frozen", "new", "frozen", "new"]})
    listed, rec = bm.update_first_listed(base, "2026-11-30", state)
    assert rec == ["BBB"] and listed["B"] == pd.Timestamp("2026-11-30") and not {"C", "D"} & set(listed)
    listed, rec = bm.update_first_listed(base, "2026-12-31", state)
    assert rec == [] and listed["B"] == pd.Timestamp("2026-11-30")
    lead = pd.Timedelta(days=bm.MOVER_LEAD_DAYS)
    assert bm.series_starts(base, listed) == {"A": pd.Timestamp("2025-06-06") - lead,
                                              "B": pd.Timestamp("2026-11-30") - lead}


def test_the_committed_first_listings():
    fl = pd.read_csv(ROOT / "state/forward_observation/megacap" / bm.FIRST_LISTED_NAME, dtype=str)
    assert list(fl.columns) == ["security_id", "ticker", "first_listed", "source"]
    assert fl["security_id"].is_unique and (fl["first_listed"] >= bm.FIRST_LISTED_FROM).all()
    assert set(fl["source"]) <= {"v2_weekly_listed", "forward_snapshot"}
    first = dict(zip(fl["ticker"], fl["first_listed"]))
    assert first["PLTR"] == "2024-12-06" and first["WMT"] == "2026-01-02"
    # the research series starts: PLTR 2024-08-19 (109 days), WMT 2025-09-18 (106 days)
    assert bm.MOVER_LEAD_DAYS == 108
    assert str((pd.Timestamp(first["PLTR"]) - pd.Timedelta(days=bm.MOVER_LEAD_DAYS)).date()) == "2024-08-20"


def test_a_signal_without_targets_keeps_the_holdings(env, res):
    sigs = signals(env)
    sigs[pd.Timestamp("2026-11-30")] = sigs[pd.Timestamp("2026-11-30")].assign(w_M2=0.0)
    tg = bm.targets_of(sigs, "M2")
    assert pd.Timestamp("2026-11-30") not in tg and len(tg) == 2
    out = bm.run_rule("M2", sigs, res["legs"], env["held"], "2026-12-15")
    assert out["sim"]["names"][pd.Timestamp("2026-12-02")] == 5          # no sale to cash at the 12-01 close
    none = {k: v.assign(w_M2=0.0) for k, v in signals(env).items()}
    assert bm.targets_of(none, "M2") == {}


def test_only_names_whose_month_is_open_are_downloaded_again(env, tmp_path):
    sigs = signals(env)
    s1, s2, s3 = (pd.Timestamp(s) for s in tb.M_SIGS)
    w = [f"w_{r.name}" for r in bm.RULES]
    old = sigs[s1][(sigs[s1][w] > 0).any(axis=1)]
    gone = old[~old["security_id"].isin(set(sigs[s2].loc[(sigs[s2][w] > 0).any(axis=1), "security_id"]) |
                                         set(sigs[s3].loc[(sigs[s3][w] > 0).any(axis=1), "security_id"]))]
    if gone.empty:                                   # make one October name leave at the 11-30 signal
        y = old["security_id"].iloc[-1]
        for s in (s2, s3):
            sigs[s].loc[sigs[s]["security_id"] == y, w] = 0.0
        gone = old[old["security_id"] == y]
    y = gone["yahoo"].iloc[0]
    held = tmp_path / "held"
    held.mkdir()
    for p in env["held"].glob("*.json.gz"):
        shutil.copy2(p, held / p.name)
    assert y in bm.names_to_refresh(sigs, held, CAL[CAL <= "2026-12-15"])     # sold 12-01, December still open
    assert y not in bm.names_to_refresh(sigs, held, CAL[CAL <= "2027-01-05"])  # December final: file kept
    assert y in bm.names_to_refresh(sigs, held)                                 # no calendar: every name
    (held / f"{y}.json.gz").unlink()
    assert y in bm.names_to_refresh(sigs, held, CAL[CAL <= "2027-01-05"])      # no stored chart: downloaded


# ======================================================================== isolation (data and workflow review)


def test_a_row_error_stops_only_its_line(env, monkeypatch):
    real = b4.month_rows

    def rows(k, v):
        if k == "V3":
            raise ValueError("boom")
        return real(k, v)
    monkeypatch.setattr(b4, "month_rows", rows)
    r = tb.compute(env, "2026-12-31")
    assert r["lines"]["V3"]["error"] == "monthly rows: ValueError: boom"
    assert r["lines"]["LEV-B3"]["rows"] and r["lines"]["M1"]["rows"]


def test_the_runner_keeps_the_b4_part_on_a_render_error_and_without_b4(env, tmp_path, monkeypatch):
    kw = {"raw_dir": env["raw"], "stocks": tfo.STOCKS, "smisp_dir": env["smisp"]}
    cfg = {"b4_dir": env["b4"], "state_dir": env["mstate"]}
    log = tmp_path / "log.md"
    fo.run("2026-12-31", log_path=log, b4=cfg, **kw)
    t1 = log.read_text()
    head1, part = b4.split_log(t1)
    assert part.startswith("## B4: seven candidates added 2026-10-24") and head1 + part == t1
    fo.run("2027-01-04", log_path=log, **kw)                         # --no-b4: B1-B3 updated, B4 kept unchanged
    t2 = log.read_text()
    assert b4.split_log(t2)[1] == part and b4.split_log(t2)[0] != head1
    assert t2.startswith(fo.render_log(*(lambda r: (r["as_of"], r["data_end"], r["lines"]))(
        fo.compute("2027-01-04", env["raw"], tfo.STOCKS, env["smisp"])), t1)[0])

    def broken(*a, **k):
        raise RuntimeError("render broke")
    monkeypatch.setattr(b4, "render", broken)
    r = fo.run("2027-01-29", log_path=log, b4=cfg, **kw)
    t3 = log.read_text()
    assert b4.split_log(t3)[1] == part and "data through: 2027-01-29" in t3
    assert "render broke" in r["b4"]["render_error"] and any("B4 part not rendered" in w for w in r["warnings"])
    c = hb.check_pipeline(r["b4"])
    assert c.status == hc.FAIL and c.items[-1].startswith("log: B4 part not rendered")


def test_a_b4_check_error_does_not_stop_the_b1_b3_record(env, tmp_path, monkeypatch):
    def broken(*a, **k):
        raise KeyError("x")
    monkeypatch.setattr(daily, "b4_health", broken)
    rep = daily.run_daily(as_of="2026-12-31", **tbh.kw_of(env, tmp_path))
    by = tbh.by_name(rep)
    assert by["b4_pipeline"]["status"] == hc.FAIL and by["b4_pipeline"]["items"][0].startswith("health_b4: KeyError")
    assert by["replay_drift"]["status"] == hc.OK and by["log_status"]["status"] == hc.OK
    assert (tmp_path / "health.json").exists() and (tmp_path / "replay.json").exists()


def test_without_b4_the_kept_b4_part_is_not_judged(env, tmp_path):
    kw = tbh.kw_of(env, tmp_path)
    daily.run_daily(as_of="2026-12-30", **kw)                        # the log now has a B4 part
    kw["b4"] = False
    rep = daily.run_daily(as_of="2027-01-04", **kw)                  # a new month without B4
    by = tbh.by_name(rep)
    assert by["log_status"]["status"] == hc.OK, by["log_status"]
    assert "b4_pipeline" not in by and "## B4: seven candidates" in (tmp_path / "log.md").read_text()


def test_a_dry_run_computes_signals_in_a_scratch_copy(tmp_path):
    state, cache = tmp_path / "state", tmp_path / "b4"
    state.mkdir()
    (state / "m_2026-10-30.csv").write_text("x\n")
    (state / bm.FIRST_LISTED_NAME).write_text("security_id,ticker,first_listed,source\n")
    d = b4.scratch_state(state, cache)
    assert d.parent == cache and sorted(p.name for p in d.iterdir()) == sorted(p.name for p in state.iterdir())
    (d / "m_2026-11-30.csv").write_text("y\n")
    assert not (state / "m_2026-11-30.csv").exists()
    assert not (b4.scratch_state(state, cache) / "m_2026-11-30.csv").exists()     # a fresh copy each dry run


# ======================================================================== detectors (fault-injection review)


def test_early_run_grace_for_a_line_one_session_short(env, res):
    fr = {s: hc.chart_frame(env["raw"] / f"{s}.json", T) for s in bdata.ETFS}
    now = tbh.utc("2027-01-01 03:30")
    ends = {"LEV-B3": pd.Timestamp("2026-12-30"), "V3": pd.Timestamp("2026-12-30")}
    c = hb.check_freshness(fr, T, CAL, T, T, now, grace=True, line_end=ends, data_end=T)
    assert c.status == hc.WARN and all("waits for a late bar" in x for x in c.items)
    assert hb.check_freshness(fr, T, CAL, T, T, now, line_end=ends, data_end=T).status == hc.FAIL
    two = {"LEV-B3": pd.Timestamp("2026-12-29")}                     # two sessions short: a failure on any run
    assert hb.check_freshness(fr, T, CAL, T, T, now, grace=True, line_end=two, data_end=T).status == hc.FAIL
    # BT4b one BTC day short on an early run
    btc = {"BT4b": pd.Timestamp("2026-12-30")}                      # the line ends at the last complete BTC day
    c = hb.check_freshness(fr, T, CAL, T - pd.Timedelta(days=1), T, now, grace=True, line_end=btc, data_end=T)
    assert c.status == hc.WARN, c.items
    assert hb.check_freshness(fr, T, CAL, T - pd.Timedelta(days=1), T, now, line_end=btc, data_end=T).status == \
        hc.FAIL
    assert all(bm.split_ratio(x) is None for x in (1.07, 1.37, 0.93)) and bm.split_ratio(0.1002) == 0.1
    assert (bm.split_ratio(2.004), bm.split_ratio(1.5), bm.split_ratio(1 / 3)) == (2.0, 1.5, 1 / 3)


def test_held_names_are_judged_with_their_second_vendor(env, res):
    fr = {s: hc.chart_frame(env["raw"] / f"{s}.json", T) for s in bdata.ETFS}
    now = tbh.utc("2027-01-01 03:30")
    full = hc.chart_frame(env["held"] / "S01.json.gz", T)

    def held(n):
        return {"S01": full.iloc[: len(full) - n]}
    ok = hb.check_freshness(fr, T, CAL, T, T, now, held={"S01": full}, held_other={"S01": (T, False)})
    assert ok.status == hc.OK
    later = hb.check_freshness(fr, T, CAL, T, T, now, held=held(1), held_other={"S01": (T, False)})
    assert later.status == hc.FAIL and "second vendor has bars through" in later.items[0]
    gone = hb.check_freshness(fr, T, CAL, T, T, now, held=held(1), held_other={"S01": (None, True)})
    assert gone.status == hc.WARN and "delisted or halted" in gone.items[0]       # both vendors stop: held as cash
    lone = hb.check_freshness(fr, T, CAL, T, T, now, held=held(1), held_other={})
    assert lone.status == hc.FAIL and "no second vendor" in lone.items[0]
    assert hb.check_freshness(fr, T, CAL, T, T, now, held=held(5), held_other={}).status == hc.WARN
    assert hb.check_freshness(fr, T, CAL, T, T, now, grace=True, held=held(1), held_other={}).status == hc.WARN


def test_qld_and_ibit_must_track_qqq_and_btc(env, res):
    fr = {s: hc.chart_frame(env["raw"] / f"{s}.json", T) for s in bdata.ETFS}
    qqq = hc.chart_frame(env["raw"] / "QQQ.json", T)
    src = {"QLD": "yahoo", "IBIT": "yahoo"}
    assert hb.check_integrity(fr, T, CAL, res["btc"], src, qqq=qqq).status == hc.OK
    day = pd.Timestamp("2026-12-10")
    for sym, f in (("QLD", 1.4), ("IBIT", 1.4)):
        bad = dict(fr, **{sym: fr[sym].copy()})
        later = bad[sym].index >= day
        bad[sym].loc[later, ["open", "high", "low", "close", "adjclose"]] *= f   # a level shift both vendors carry
        c = hb.check_integrity(bad, T, CAL, res["btc"], src, qqq=qqq)
        assert c.status == hc.FAIL and any(x.startswith(f"{sym} 2026-12-10: day return") for x in c.items), c.items
        assert c.data["keys"] == [f"{sym} 2026-12-10"]
        acc = hb.check_integrity(bad, T, CAL, res["btc"], src, accepted=c.data["keys"], qqq=qqq)
        assert acc.status != hc.FAIL and any("(accepted earlier)" in x for x in acc.items)


def test_a_wrong_split_right_after_a_trade_fails(env, res):
    qqq = hc.chart_frame(env["raw"] / "QQQ.json", T)
    _, _, frames = hb.check_signals(env["mstate"], T, CAL)
    for k, day, x in (("LEV-B3", "2026-10-27", 0.1), ("V3", "2026-11-03", 0.05)):
        r = copy.copy(res)
        r["lines"] = {kk: dict(v) for kk, v in res["lines"].items()}
        e = r["lines"][k]["exposure"].copy()
        e[pd.Timestamp(day)] += x                     # within the 0.12 drift tolerance, but the day after a trade
        r["lines"][k]["exposure"] = e
        c, _ = hb.check_rules(r, qqq, frames)
        assert c.status == hc.FAIL and any(f"{k} {day}" in i and "after a trade" in i for i in c.items), c.items


def test_signal_files_meta_hashed_first_hash_kept_and_no_shared_chart(env, tmp_path):
    state = tmp_path / "s"
    shutil.copytree(env["mstate"], state)
    _, known, _ = hb.check_signals(state, T, CAL)
    meta = state / "m_2026-11-30_meta.json"
    meta.write_text(meta.read_text().replace('"s": "2026-11-30"', '"s": "2026-11-30", "x": 1'))
    c, hashes, _ = hb.check_signals(state, T, CAL, known)
    assert c.status == hc.FAIL and "m_2026-11-30_meta.json: changed" in c.items[0]
    assert hashes == known                            # the next run still compares with the first hash
    sc = pd.read_csv(state / "m_2026-11-30.csv", dtype={"security_id": str, "ticker": str, "yahoo": str})
    sc.loc[1, "yahoo"] = sc.loc[0, "yahoo"]
    assert any("chart symbol appears twice" in x for x in hb.signal_items("m_2026-11-30.csv", sc))


def test_b3_signal_hash_keeps_the_first_record(tmp_path):
    env = tfo.make_smisp_env(tmp_path)
    th.add_meta(env["smisp"] / "state")
    st_ = env["smisp"] / "state"
    _, hashes, _ = hc.check_b3(st_, "2026-11-02", CAL, None)
    known = {"s_2026-10-30.csv": "0" * 64}
    c, h2, _ = hc.check_b3(st_, "2026-11-02", CAL, None, known)
    assert c.status == hc.FAIL and h2["s_2026-10-30.csv"] == "0" * 64 != hashes["s_2026-10-30.csv"]


def test_a_stray_qld_bar_is_reported_not_raised(env, tmp_path):
    raw = tmp_path / "raw"
    shutil.copytree(env["raw"], raw)
    body = json.loads((raw / "QLD.json").read_text())
    r = body["chart"]["result"][0]
    sat = int(pd.Timestamp("2026-12-12 09:30", tz="America/New_York").timestamp())     # a Saturday
    k = next(i for i, t in enumerate(r["timestamp"]) if t > sat)
    r["timestamp"].insert(k, sat)
    for q in r["indicators"]["quote"][0].values():
        q.insert(k, q[k - 1])
    r["indicators"]["adjclose"][0]["adjclose"].insert(k, r["indicators"]["adjclose"][0]["adjclose"][k - 1])
    (raw / "QLD.json").write_text(json.dumps(body))
    out = tb.compute(env, "2026-12-31", raw=raw)
    assert out["lines"]["BT4b"]["started"] and out["lines"]["M1"]["started"]          # the other lines run
    c = hb.check_integrity({"QLD": hc.chart_frame(raw / "QLD.json", T)}, T, CAL)
    assert c.status == hc.FAIL


def test_log_status_names_a_row_after_the_line_end():
    text = ("data through: 2026-12-31\n<!-- table:M1:start -->\n| month | n | status |\n|---|---|---|\n"
            "| 2026-11 | 1 | provisional |\n| 2026-12 | 2 | provisional |\n<!-- table:M1:end -->\n")
    c = hc.check_log_status(text, "2026-12-31", {"M1": pd.Timestamp("2026-11-30")})
    assert c.status == hc.FAIL and c.items == ["M1 2026-12: a row after the line's last session 2026-11-30"]


def test_megacap_fingerprints_cover_the_holding_periods_only(env, res):
    fp_ = hb.megacap_fingerprints(res, env["held"], "2026-10-05", T)
    allday = hb.megacap_fingerprints(res, env["held"], "2026-10-05", T, margin=10_000)
    assert set(fp_) == set(allday) and fp_
    first_exec = pd.Timestamp("2026-11-02")
    for tick, days in fp_.items():
        d = pd.DatetimeIndex(list(days))
        assert d.min() >= first_exec - pd.Timedelta(days=7) and set(days) <= set(allday[tick])


# ======================================================================== Yahoo pace, Tiingo key, QLD / IBIT start


def test_one_yahoo_pace_and_stop_for_the_whole_run(tmp_path):
    clock, slept = [0.0], []
    p = fp.YahooPacer(2.0, sleep=lambda s: (slept.append(s), clock.__setitem__(0, clock[0] + s)),
                      clock=lambda: clock[0])
    p.wait()
    clock[0] += 0.5
    p.wait()
    assert slept == [1.5]
    p.stop("HTTP 429 at MSFT")
    p.stop("HTTP 403 at X")
    assert p.stopped == "HTTP 429 at MSFT"
    asked = []
    out, notes = daily.fetch_secondaries({"QLD": ("QLD", "QLD", "tiingo")}, T, {"alpaca_headers": None,
                                         "yahoo": lambda u: asked.append(u)}, sleep=lambda s: None, pacer=p)
    assert asked == [] and "yahoo: not asked (HTTP 429 at MSFT earlier in this run)" in notes
    calls = []
    got = fo.fetch(("QLD",), raw_dir=tmp_path / "raw", log=tmp_path / "f.csv", getter=lambda u: calls.append(u),
                   source="yahoo", history=None, pacer=p)
    assert got == {"QLD": "skipped (Yahoo stopped)"} and calls == []


def test_the_tiingo_key_goes_in_the_header():
    seen = {}

    def get(url, headers):
        seen.update(url=url, headers=headers)
        return json.dumps([{"date": "2026-11-02T00:00:00Z", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1,
                            "adjClose": 1, "divCash": 0, "splitFactor": 1}]).encode()
    fp.tiingo_chart("QLD", "2026-08-01", "2026-11-03", key="secret-key", getter=get)
    assert "secret-key" not in seen["url"] and "token" not in seen["url"]
    assert seen["headers"]["Authorization"] == "Token secret-key"


def test_qld_and_ibit_are_fetched_from_august_2026():
    assert fo.FETCH_START["QLD"] == fo.FETCH_START["IBIT"] == "2026-08-01"
    assert pd.Timestamp(fo.FETCH_START["QLD"]) < pd.Timestamp(hc.START_CLOSE) - pd.Timedelta(
        days=hc.INTEGRITY_FROM_DAYS)                  # before the integrity window (2026-08-28)
    assert fo.FETCH_START["QQQ"] == "1999-01-01" and fo.FETCH_START["ONEQ"] == "2003-01-01"      # B1 / B2 unchanged
