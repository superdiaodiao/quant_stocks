"""Synthetic tests of the B4 health checks (quant/observation/health_b4.py and their wiring in daily.py).

Each check passes on clean data and fails on the fault it targets; the daily run with B4 is idempotent and keeps the
B1-B3 checks as they were. No network, no vendor data."""
from __future__ import annotations

import copy
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quant.observation import b4, b4_data as bdata
from quant.observation import daily
from quant.observation import health as hc
from quant.observation import health_b4 as hb

sys.path.insert(0, str(Path(__file__).parent))
import test_forward_observation_b4 as tb  # noqa: E402
import test_forward_observation_health as th  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CAL = tb.CAL


def utc(s: str) -> datetime:
    return pd.Timestamp(s, tz="UTC").to_pydatetime()


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    return tb.make_b4_env(tmp_path_factory.mktemp("b4h"))


def kw_of(env, tmp, **extra):
    kw = th.daily_kw(env, tmp)
    kw.update({"b4": True, "b4_dir": env["b4"], "b4_state": env["mstate"], **extra})
    return kw


def by_name(rep):
    return {c["name"]: c for c in rep["checks"]}


def summary(rep):
    return "\n".join(hc.summary_lines(rep))


@pytest.fixture(scope="module")
def res(env):
    return tb.compute(env, "2026-12-31")


# ======================================================================== the daily run with B4


def test_daily_run_with_b4_is_clean_idempotent_and_keeps_b1_b3(env, tmp_path):
    kw = kw_of(env, tmp_path)
    daily.run_daily(as_of="2026-12-30", **kw)
    rep = daily.run_daily(as_of="2026-12-31", **kw)
    by = by_name(rep)
    for name in ("b4_pipeline", "b4_data_freshness", "b4_data_integrity", "b4_vendor_agreement", "b4_rules",
                 "b4_signal"):
        assert by[name]["status"] in (hc.OK, hc.WARN), summary(rep)
    assert by["b4_rules"]["status"] == hc.OK and "LEV-B3" in by["b4_rules"]["message"], summary(rep)
    assert by["b4_signal"]["status"] == hc.OK and "2026-10-30" in by["b4_signal"]["message"]
    assert by["log_status"]["status"] == hc.OK and by["replay_drift"]["status"] == hc.OK, summary(rep)
    files = {p: p.read_bytes() for p in (tmp_path / "log.md", tmp_path / "health.json", tmp_path / "replay.json")}
    assert daily.run_daily(as_of="2026-12-31", **kw) == rep
    assert all(p.read_bytes() == b for p, b in files.items())        # byte-identical rerun
    state = json.loads((tmp_path / "replay.json").read_text())
    assert {"LEV-B3.ret", "LEV-B3.match", "V3.hmatch", "BT4b.ret", "M1.ret"} <= set(state["numbers"]["series"])
    assert {"m_2026-10-30.csv", "m_2026-11-30.csv"} <= set(state["signal_hashes"])
    assert set(state["inputs"]) >= {"px", "b4px", "b4m", "b4btc", "b4rf"}
    # the B1-B3 checks give the same results as a run without B4
    kw0 = th.daily_kw(env, tmp_path / "nob4")
    (tmp_path / "nob4").mkdir()
    daily.run_daily(as_of="2026-12-30", **kw0)
    rep0 = daily.run_daily(as_of="2026-12-31", **kw0)
    for c in rep0["checks"]:
        if c["name"] not in ("log_status", "replay_drift"):
            assert by[c["name"]] == c, c["name"]
    # the next session: the B4 numbers replay without drift
    rep3 = daily.run_daily(as_of="2027-01-04", **kw)
    assert by_name(rep3)["replay_drift"]["status"] == hc.OK, summary(rep3)
    assert any(n.startswith("LEV-B3: SMA200 state") for n in rep3["next_session"])


def test_daily_main_runs_b4_by_default(monkeypatch, tmp_path):
    calls = {}

    def fake(**kw):
        calls.update(kw)
        return hc.report("2026-12-31", [hc.Check("x", hc.OK, "m")])
    monkeypatch.setattr(daily, "run_daily", fake)
    assert daily.main(["--dry-run"]) == 0 and calls["b4"] is True
    assert daily.main(["--dry-run", "--no-b4"]) == 0 and calls["b4"] is False


# ======================================================================== independent rules


def test_rules_clean_then_each_fault_fails(env, res):
    qqq = hc.chart_frame(env["raw"] / "QQQ.json", "2026-12-31")
    _, _, frames = hb.check_signals(env["mstate"], "2026-12-31", CAL)
    c, nxt = hb.check_rules(res, qqq, frames)
    assert c.status == hc.OK and "M6" in c.message, c.items
    assert any(n.startswith("V3: min(1.5") for n in nxt) and any(n.startswith("BT4b: BTC weight") for n in nxt)

    def fails_with(edit, text):
        r = copy.copy(res)
        r["lines"] = {k: dict(v) for k, v in res["lines"].items()}
        fr = {k: v.copy() for k, v in frames.items()}
        edit(r["lines"], fr)
        c2, _ = hb.check_rules(r, qqq, fr)
        assert c2.status == hc.FAIL and any(text in x for x in c2.items), c2.items
    # LEV-B3: one decision day with the other state
    v = res["lines"]["LEV-B3"]
    d = v["w"].index[v["s_i"] + 3]

    def flip(lines, fr):
        w = lines["LEV-B3"]["w"].copy()
        w.loc[d, ["QQQ", "QLDX"]] = [0.5, 0.5] if w.loc[d, "QLDX"] == 0 else [1.0, 0.0]
        lines["LEV-B3"]["w"] = w
    fails_with(flip, "the SMA200 rule gives")

    def exposure(lines, fr):
        e = lines["LEV-B3"]["exposure"].copy()
        e.iloc[5] += 0.4
        lines["LEV-B3"]["exposure"] = e
    fails_with(exposure, "held exposure")
    # V3: a month-end target off by 1%

    def v3(lines, fr):
        e = lines["V3"]["e_t"].copy()
        m = [x for x in hb._month_end_days(e.index) if x > pd.Timestamp("2026-10-26")][0]
        e.loc[m] *= 1.01
        lines["V3"]["e_t"] = e
    fails_with(v3, "the rule gives")
    # BT4b: the BTC weight after a rebalance

    def sleeve(lines, fr):
        w = lines["BT4b"]["btc_weight"].copy()
        w.iloc[0] = 0.12
        lines["BT4b"]["btc_weight"] = w
    fails_with(sleeve, "BTC weight after the rebalance")
    # mega-cap: a tampered signal file, and runner targets that do not follow the file

    def tamper(lines, fr):
        sc = fr["m_2026-11-30.csv"]
        a, b_ = sc.index[sc["w_M1"] > 0][0], sc.index[sc["w_M1"] == 0][0]
        sc.loc[a, "w_M1"], sc.loc[b_, "w_M1"] = 0.0, 0.1
    fails_with(tamper, "file targets")

    def targets(lines, fr):
        t = dict(lines["M2"]["targets"])
        s = max(t)
        t[s] = t[s][:-1] + [("OTHER", 0.2, 1.0, np.nan, "sec_shares")]
        lines["M2"]["targets"] = t
    fails_with(targets, "the runner's targets differ")


def test_sma_state_and_v3_exposure_equal_the_definitions():
    rng = np.random.default_rng(3)
    p = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0003, 0.02, 900))), index=pd.bdate_range("2023-01-02",
                                                                                                 periods=900))
    from quant.signals.technical import trend_state
    st, _ = hb.sma_state(p)
    pd.testing.assert_series_equal(st, trend_state(p, 200, 0.02), check_names=False)
    r = p.pct_change()
    from quant.strategies import voltarget as vt
    e, _ = vt.daily_exposure("V3", r, p, b4.V3_PARAMS)
    mine = hb.v3_exposure(p, p.index[300::50])
    assert all(abs(mine[d] - e[d]) < 1e-12 for d in mine)


# ======================================================================== signal files


def test_signal_files_missing_changed_and_grace(env, tmp_path):
    c, hashes, frames = hb.check_signals(env["mstate"], "2026-12-31", CAL)
    assert c.status == hc.OK and set(hashes) == {"m_2026-10-30.csv", "m_2026-11-30.csv", "m_2026-10-30_meta.json",
                                                 "m_2026-11-30_meta.json"}
    c, _, _ = hb.check_signals(env["mstate"], "2026-10-30", CAL)
    assert c.status == hc.OK and "today's close" in c.message
    state = tmp_path / "s"
    shutil.copytree(env["mstate"], state)
    (state / "m_2026-11-30.csv").unlink()
    c, _, _ = hb.check_signals(state, "2026-12-31", CAL)
    assert c.status == hc.FAIL and "2026-11-30 missing" in c.items[0]
    c, _, _ = hb.check_signals(state, "2026-12-01", CAL, grace=True)       # due at this session's run: warn
    assert c.status == hc.WARN
    c, _, _ = hb.check_signals(env["mstate"], "2026-12-31", CAL, known={"m_2026-10-30.csv": "0" * 64})
    assert c.status == hc.FAIL and "changed after it was first recorded" in c.items[0]
    (state / "m_2026-10-30_meta.json").unlink()
    c, _, _ = hb.check_signals(state, "2026-11-02", CAL)
    assert c.status == hc.FAIL and "meta.json: missing" in " ".join(c.items)


# ======================================================================== data: freshness, integrity, vendors


def test_freshness(env, res):
    t = pd.Timestamp("2026-12-31")
    fr = {s: hc.chart_frame(env["raw"] / f"{s}.json", t) for s in bdata.ETFS}
    btc_last = res["btc"].close.index[-1]
    now = utc("2027-01-01 03:30")
    ok = hb.check_freshness(fr, t, CAL, btc_last, pd.Timestamp("2026-12-30"), now, line_end=b4.line_ends(res),
                            data_end=t)
    assert ok.status == hc.OK, ok.items
    stale = dict(fr, QLD=fr["QLD"][fr["QLD"].index < pd.Timestamp("2026-12-29")])
    assert hb.check_freshness(stale, t, CAL, btc_last, t, now).status == hc.FAIL
    # BTC: the session's candle closes at 00:00 UTC; one day short is a warning on an early run only
    early = utc("2026-12-31 23:30")
    assert hb.check_freshness(fr, t, CAL, t - pd.Timedelta(days=1), t, early).status == hc.OK
    c = hb.check_freshness(fr, t, CAL, t - pd.Timedelta(days=1), t, now, grace=True)
    assert c.status == hc.WARN and "BTC" in c.items[0]
    assert hb.check_freshness(fr, t, CAL, t - pd.Timedelta(days=1), t, now).status == hc.FAIL
    assert hb.check_freshness(fr, t, CAL, None, t, now).status == hc.FAIL
    # the T-bill file
    assert hb.check_freshness(fr, t, CAL, t, pd.Timestamp("2026-12-22"), now).status == hc.WARN
    assert hb.check_freshness(fr, t, CAL, t, pd.Timestamp("2026-12-15"), now).status == hc.FAIL
    # a line that stops before the data end without a reason
    c = hb.check_freshness(fr, t, CAL, t, t, now, line_end={"LEV-B3": pd.Timestamp("2026-12-30")}, data_end=t)
    assert c.status == hc.FAIL and "LEV-B3: line ends" in c.items[-1]
    assert hb.check_freshness(fr, t, CAL, t, t, now, line_end={"M1": pd.Timestamp("2026-11-30")}, data_end=t,
                              waiting=["M1"]).status == hc.OK
    assert hb.check_freshness(fr, t, CAL, t - pd.Timedelta(days=1), t, early,
                              line_end={"BT4b": t - pd.Timedelta(days=1)}, data_end=t).status == hc.OK


def test_integrity(env, res):
    t = pd.Timestamp("2026-12-31")
    fr = {s: hc.chart_frame(env["raw"] / f"{s}.json", t) for s in bdata.ETFS}
    assert hb.check_integrity(fr, t, CAL, res["btc"], {"QLD": "yahoo", "IBIT": "yahoo"}).status == hc.OK
    bad = dict(fr, QLD=fr["QLD"].copy())
    bad["QLD"].iloc[-3, bad["QLD"].columns.get_loc("close")] = -1.0
    assert hb.check_integrity(bad, t, CAL, res["btc"]).status == hc.FAIL
    gap = bdata.Btc(close=res["btc"].close.drop(pd.Timestamp("2026-11-20")), info={}, sources=res["btc"].sources)
    c = hb.check_integrity(fr, t, CAL, gap)
    assert c.status == hc.FAIL and "missing UTC day" in c.items[0]
    jump = res["btc"].close.copy()
    jump[jump.index >= pd.Timestamp("2026-12-01")] *= 2.0
    c = hb.check_integrity(fr, t, CAL, bdata.Btc(close=jump, info={}, sources=res["btc"].sources))
    assert c.status == hc.FAIL and "beyond the frozen bound" in c.items[0]
    filled = bdata.Btc(close=res["btc"].close, info={"filled_days": {"2026-11-20": "coinbase"}},
                       sources=res["btc"].sources)
    assert hb.check_integrity(fr, t, CAL, filled).status == hc.WARN


def test_vendor_agreement(env, res, tmp_path):
    t = pd.Timestamp("2026-12-31")
    fr = {s: hc.chart_frame(env["raw"] / f"{s}.json", t) for s in bdata.ETFS}
    other = {"alpaca": {s: f.copy() for s, f in fr.items()}}
    src = {"QLD": "yahoo", "IBIT": "yahoo"}
    assert hb.check_vendors(fr, other, t, CAL, src, btc=res["btc"]).status == hc.OK
    off = {"alpaca": {s: f.copy() for s, f in fr.items()}}
    off["alpaca"]["QLD"].iloc[-10, off["alpaca"]["QLD"].columns.get_loc("close")] *= 1.01
    assert hb.check_vendors(fr, off, t, CAL, src, btc=res["btc"]).status == hc.FAIL
    assert hb.check_vendors(fr, {}, t, CAL, src, btc=res["btc"]).status == hc.FAIL          # the last run
    assert hb.check_vendors(fr, {}, t, CAL, src, btc=res["btc"], strict=False).status == hc.WARN
    # BTC: Bitstamp vs Coinbase
    for bump, want in ((1.02, hc.FAIL), (1.006, hc.WARN)):
        d = tmp_path / f"b{bump}"
        tb.write_btc(d, bump={"2026-12-10": bump})
        btc = bdata.load_btc(d, "2026-12-31")
        c = hb.check_vendors(fr, other, t, CAL, src, btc=btc)
        assert c.status == want and any("BTC 2026-12-1" in x for x in c.items), (bump, c.items)
    lone = bdata.Btc(close=res["btc"].close, info={}, sources=type(res["btc"].sources)(
        frames={}, close=res["btc"].sources.close[["bitstamp"]], volume=res["btc"].sources.volume[["bitstamp"]]))
    assert hb.check_vendors(fr, other, t, CAL, src, btc=lone).status == hc.FAIL
    assert hb.check_vendors(fr, other, t, CAL, src, btc=lone, strict=False).status == hc.WARN


def test_pipeline(res):
    assert hb.check_pipeline(res).status == hc.OK
    assert hb.check_pipeline(res, ["BTC kraken: error OSError"]).status == hc.WARN
    bad = {**res, "lines": {**res["lines"], "V3": {"started": False, "error": "RuntimeError: no DTB3 file"}}}
    c = hb.check_pipeline(bad)
    assert c.status == hc.FAIL and c.items == ["V3: RuntimeError: no DTB3 file"]
    assert hb.check_pipeline(None).status == hc.FAIL


# ======================================================================== replay drift and the log


def test_replay_drift_of_b4_numbers(res):
    nums = {"series": b4.series_of(res), "trades": {}, "b3_held": {}}
    inputs = {"px": {}, "b4px": {"QLD": {"2026-12-01": "1 1 1 1 1 0 1"}}}
    _, state = hc.check_replay(None, nums, inputs, "2026-12-31")
    later = copy.deepcopy(nums)
    day = sorted(later["series"]["LEV-B3.ret"])[-5]
    later["series"]["LEV-B3.ret"][day] += 1e-4
    c, _ = hc.check_replay(state, later, inputs, "2027-01-04")
    assert c.status == hc.FAIL and "LEV-B3.ret" in c.items[0]
    revised = {"px": {}, "b4px": {"QLD": {"2026-12-01": "1.01 1 1 1 1 0 1"}}}
    c, _ = hc.check_replay(state, later, revised, "2027-01-04")
    assert c.status == hc.WARN and "LEV-B3.ret" in " ".join(c.items)
    noise = copy.deepcopy(nums)
    day = sorted(noise["series"]["M1.ret"])[-5]
    noise["series"]["M1.ret"][day] += 5e-5                      # Alpaca rounding noise: within 1e-4
    assert hc.check_replay(state, noise, inputs, "2027-01-04")[0].status == hc.OK
    w = ["BT4b 2026-11: a final row changed (vendor data revision?)\n  old x\n  new y"]
    c, _ = hc.check_replay(state, nums, inputs, "2027-01-04", w)
    assert c.status == hc.FAIL and "log BT4b 2026-11" in c.items[0]


def test_log_status_judges_each_b4_line_against_its_own_end(env, tmp_path):
    from quant.observation import runner as fo
    res = tb.compute(env, "2026-12-15")
    text, _ = b4.render(res)
    full = "- As-of date of the last run: 2026-12-15; data through: 2026-12-15.\n" + text
    ends = b4.line_ends(res)
    assert hc.check_log_status(full, "2026-12-15", ends).status == hc.OK
    row = fo.parse_rows(text)["LEV-B3"]["2026-11"]
    assert row.endswith("| final |")
    bad = full.replace(row, row[: -len("| final |")] + "| provisional |")
    c = hc.check_log_status(bad, "2026-12-15", ends)
    assert c.status == hc.FAIL and "LEV-B3 2026-11" in c.items[0]
    # a mega-cap line that waits for its 11-30 signal keeps November provisional, and that is right
    state = tmp_path / "s"
    state.mkdir()
    for q in env["mstate"].glob("m_2026-10-30*"):
        (state / q.name).write_bytes(q.read_bytes())
    r2 = tb.compute(env, "2026-12-15", state=state)
    t2, _ = b4.render(r2)
    assert fo.parse_rows(t2)["M1"]["2026-11"].endswith("| provisional |")
    assert hc.check_log_status("data through: 2026-12-15\n" + t2, "2026-12-15", b4.line_ends(r2)).status == hc.OK


# ======================================================================== the workflow


def test_workflow_runs_b4_and_prunes_only_after_both_signals():
    wf = (ROOT / ".github/workflows/forward_observation_daily.yml").read_text()
    assert "--fetch --fetch-smisp" in wf and 'args="--fetch --fetch-smisp --report-out' in wf
    # B4 is on unless the repository variable switches it off; the month-end downloads get 180 minutes
    assert "NO_B4: ${{ vars.FORWARD_OBS_NO_B4 }}" in wf
    assert 'if [ "$NO_B4" = "true" ]; then args="$args --no-b4"; fi' in wf and wf.count('args="$args --no-b4"') == 1
    assert "timeout-minutes: 180" in wf
    assert "git add docs/forward_observation_log.md state/forward_observation" in wf
    assert "state/forward_observation/megacap/m_" in wf          # the S-MISP folder is kept until both are frozen
