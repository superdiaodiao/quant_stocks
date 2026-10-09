"""Synthetic tests for scripts/forward_observation.py (no vendor data, no network)."""
import json
import math

import numpy as np
import pandas as pd
import pytest

from scripts import forward_observation as fo
from scripts import research_intraday_t as it
from scripts import research_selective_t as st
from scripts import research_t_grid as tg

STOCKS = ("AAPL", "MSFT")
SESSIONS = pd.bdate_range("2024-01-02", "2027-01-29")


# forced daily log returns in the forward window, so that both rules trade: deep one-day drops for the stocks
# (close <= 0.9 x SMA20), runs of down days for QQQ (RSI2 at its 2nd percentile), each followed by a rebound
SHOCKS = {"stock": {"2026-10-20": -0.17, "2026-10-21": 0.04, "2026-10-22": 0.04, "2026-11-17": -0.16,
                    "2026-11-18": 0.05, "2026-12-15": -0.17, "2026-12-16": 0.05},
          "QQQ": {**{str(d.date()): -0.025 for d in pd.bdate_range("2026-10-19", periods=8)},
                  **{str(d.date()): 0.02 for d in pd.bdate_range("2026-10-29", periods=3)},
                  **{str(d.date()): -0.025 for d in pd.bdate_range("2026-11-30", periods=8)},
                  **{str(d.date()): 0.02 for d in pd.bdate_range("2026-12-10", periods=3)}}}


def payload(sym: str, seed: int, vol: float, sessions=SESSIONS, split_on: str | None = None) -> dict:
    """A Yahoo v8 chart body: split-adjusted OHLC, adjclose, a quarterly dividend, optional 2:1 split."""
    rng = np.random.default_rng(seed)
    r = rng.standard_t(4, len(sessions)) * vol / math.sqrt(2) + 0.0004
    for day, x in SHOCKS.get("QQQ" if sym == "QQQ" else "stock" if sym != "ONEQ" else "", {}).items():
        k = sessions.searchsorted(pd.Timestamp(day))
        if k < len(sessions) and sessions[k] == pd.Timestamp(day):
            r[k] = x
    c = 100 * np.exp(np.cumsum(r))
    o = c * np.exp(rng.normal(0, vol / 3, len(c)))
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, vol / 2, len(c))))
    lo = np.minimum(o, c) * (1 - np.abs(rng.normal(0, vol / 2, len(c))))
    ts = [int(pd.Timestamp(d.date(), tz="America/New_York").replace(hour=9, minute=30).timestamp()) for d in sessions]
    div_days = [k for k in range(30, len(c), 63)]
    divs = {str(ts[k]): {"date": ts[k], "amount": 0.3} for k in div_days}
    adj = c.copy()
    for k in div_days:   # back-adjust closes before the ex-date for the dividend
        adj[:k] *= 1 - 0.3 / c[k - 1]
    ev = {"dividends": divs}
    if split_on:
        k = int(sessions.searchsorted(pd.Timestamp(split_on)))
        ev["splits"] = {str(ts[k]): {"date": ts[k], "numerator": 2, "denominator": 1}}
    return {"chart": {"result": [{"timestamp": ts, "meta": {"dataGranularity": "1d", "symbol": sym},
                                  "indicators": {"quote": [{"open": list(o), "high": list(h), "low": list(lo),
                                                            "close": list(c)}],
                                                 "adjclose": [{"adjclose": list(adj)}]},
                                  "events": ev}]}}


def write_raw(tmp_path, mutate_after: str | None = None):
    raw = tmp_path / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    spec = {"QQQ": (1, 0.015), "ONEQ": (2, 0.014), "AAPL": (3, 0.035), "MSFT": (4, 0.03)}
    for sym, (seed, vol) in spec.items():
        p = payload(sym, seed, vol, split_on="2026-12-07" if sym == "AAPL" else None)
        if mutate_after:   # wild values after a date (must not change anything up to that date)
            res = p["chart"]["result"][0]
            cut = pd.Timestamp(mutate_after, tz="America/New_York").timestamp()
            q = res["indicators"]["quote"][0]
            for k, t in enumerate(res["timestamp"]):
                if t > cut + 86400 - 1:
                    f = 0.5 if k % 2 else 1.7
                    for key in ("open", "high", "low", "close"):
                        q[key][k] *= f
                    res["indicators"]["adjclose"][0]["adjclose"][k] *= f
        (raw / f"{sym}.json").write_text(json.dumps(p))
    return raw


@pytest.fixture()
def raw(tmp_path):
    return write_raw(tmp_path)


def final_rows(res):
    return {k: [r for r in v["rows"] if r["status"] == "final"] for k, v in res["lines"].items() if v.get("started")}


# ------------------------------------------------------------------ start of the accounts


def test_before_start_no_trades_and_log_says_start(raw, tmp_path):
    log = tmp_path / "log.md"
    for as_of in ("2026-10-09", "2026-10-12"):     # the 10-12 close is the account start: still no forward session
        res = fo.run(as_of, raw_dir=raw, log_path=log, stocks=STOCKS)
        assert not any(v.get("started") for v in res["lines"].values())
    text = log.read_text()
    assert "observation starts 2026-10-12" in text
    assert fo.parse_rows(text) == {"B1": {}, "SEL-A": {}, "SEL-P": {}}


def test_accounts_open_at_the_2026_10_12_close(raw):
    d = fo.load_all("2026-10-30", raw, STOCKS)
    w = fo.window_of(d.qqq, d.end)
    assert d.qqq.s.sessions[w.s - 1] == pd.Timestamp("2026-10-12") and w.sessions[0] == pd.Timestamp("2026-10-13")
    res = fo.compute("2026-10-30", raw, STOCKS)
    for k in ("B1", "SEL-A", "SEL-P"):
        v = res["lines"][k]
        assert v["ret"].index[0] == pd.Timestamp("2026-10-13")
        assert v["oneq"].index[0] == pd.Timestamp("2026-10-13")
        assert all(t["entry"] >= pd.Timestamp("2026-10-14") for t in v["trades"])   # signal at a forward close
    # the research value series starts at the 10-12 close with $10,000 (base bought at that close)
    m = d.stocks["AAPL"]
    wa = fo.window_of(m, d.end)
    sim = st.simulate(m, wa.s, wa.e, st.Spec(st.fams_of("S3-Yb", "stock")))
    assert sim["value"].index[0] == pd.Timestamp("2026-10-12") and sim["value"].iloc[0] == st.START_EQUITY


# ------------------------------------------------------------------ no look-ahead


def test_no_lookahead_future_rows_do_not_matter(tmp_path):
    a = fo.compute("2026-11-30", write_raw(tmp_path / "a"), STOCKS)
    b = fo.compute("2026-11-30", write_raw(tmp_path / "b", mutate_after="2026-11-30"), STOCKS)
    for k in ("B1", "SEL-A", "SEL-P"):
        assert [fo.row_line(r) for r in a["lines"][k]["rows"]] == [fo.row_line(r) for r in b["lines"][k]["rows"]]
        pd.testing.assert_series_equal(a["lines"][k]["ret"], b["lines"][k]["ret"])
    # the loaders drop every row after the as-of date
    d = fo.load_all("2026-11-30", tmp_path / "b" / "raw", STOCKS)
    assert d.qqq.s.sessions[-1] == pd.Timestamp("2026-11-30")
    assert all(m.s.sessions[-1] <= pd.Timestamp("2026-11-30") for m in d.stocks.values())


def test_final_rows_do_not_change_when_later_data_arrive(raw):
    early = fo.compute("2026-12-31", raw, STOCKS)
    late = fo.compute("2027-01-29", raw, STOCKS)
    fe, fl = final_rows(early), final_rows(late)
    n = 0
    for k, rows in fe.items():
        later = {r["month"]: r for r in late["lines"][k]["rows"]}
        for r in rows:
            assert fo.row_line(later[r["month"]]) == fo.row_line(r), (k, r["month"])
            n += 1
    assert n >= 4      # Oct and Nov of three lines at least (H_match uses the exposure realised to date)


# ------------------------------------------------------------------ idempotency


def test_rerun_same_month_replaces_the_row(raw, tmp_path):
    log = tmp_path / "log.md"
    fo.run("2026-11-13", raw_dir=raw, log_path=log, stocks=STOCKS)
    t1 = log.read_text()
    fo.run("2026-11-13", raw_dir=raw, log_path=log, stocks=STOCKS)
    assert log.read_text() == t1                                   # byte-identical rerun
    rows = fo.parse_rows(t1)
    assert sorted(rows["B1"]) == ["2026-10", "2026-11"] and rows["SEL-A"]["2026-11"].endswith("| provisional |")
    fo.run("2026-11-30", raw_dir=raw, log_path=log, stocks=STOCKS)
    t2 = log.read_text()
    rows2 = fo.parse_rows(t2)
    for k in ("B1", "SEL-A", "SEL-P"):
        assert sorted(rows2[k]) == ["2026-10", "2026-11"]           # replaced, not duplicated
        assert rows2[k]["2026-10"] == rows[k]["2026-10"]
        assert rows2[k]["2026-10"].endswith("| final |") and rows2[k]["2026-11"].endswith("| provisional |")
    assert t2.count("<!-- table:B1:start -->") == 1 and t2.count("## Status") == 1
    with pytest.raises(SystemExit):                                # an earlier as-of may not overwrite the log
        fo.run("2026-11-13", raw_dir=raw, log_path=log, stocks=STOCKS)
    fo.run("2026-11-13", raw_dir=raw, log_path=log, stocks=STOCKS, dry_run=True)
    assert log.read_text() == t2


def test_no_price_levels_in_the_log(raw, tmp_path):
    log = tmp_path / "log.md"
    fo.run("2026-12-31", raw_dir=raw, log_path=log, stocks=STOCKS)
    body = log.read_text().split("## Status", 1)[1]
    for k, rows in fo.parse_rows(body).items():
        for ln in rows.values():
            cells = ln.strip("|").split("|")
            numbers = [c for c in cells[3:9]]
            assert all(c.strip() == "" or c.strip().endswith("%") or "." in c for c in numbers)
    d = fo.load_all("2026-12-31", raw, STOCKS)
    close = f"{d.qqq.s.close[-1]:.2f}"
    assert close not in body


# ------------------------------------------------------------------ reuse = research answers


def test_b1_matches_research_selective_t(raw):
    res = fo.compute("2027-01-29", raw, ("AAPL",))
    v = res["lines"]["B1"]
    m = fo.load_all("2027-01-29", raw, ("AAPL",)).stocks["AAPL"]
    w = fo.window_of(m, "2027-01-29")
    spec = st.variant_spec(st.fams_of("S3-Yb", "stock"), "net", st.S2_SLIP["stock"])
    sim = st.simulate(m, w.s, w.e, spec)
    hb = st.simulate(m, w.s, w.e, st.Spec((), costs=True))
    pd.testing.assert_series_equal(v["ret"], sim["ret"], check_names=False)
    pd.testing.assert_series_equal(v["hbase"], hb["ret"], check_names=False)
    assert spec.fams == (st.Fam("S3", "Y", 0.10, 0.06, 20),)
    # the last month's H_match uses the whole-window exposure: the research H_match on that month
    last = v["ret"].index[-1].to_period("M")
    ref = st.match_returns(m, w.s, w.e, sim["exposure"])
    pd.testing.assert_series_equal(v["hmatch"][v["hmatch"].index.to_period("M") == last],
                                   ref[ref.index.to_period("M") == last], check_names=False)
    assert len(v["trades"]) == len(sim["trips"]) > 0
    net = (sim["trips"]["gross"] - sim["trips"]["cost"]) / sim["trips"]["notional"] * 100
    assert np.allclose(sorted(t["net_pct"] for t in v["trades"]), sorted(net))


def test_b2_matches_research_t_grid(raw):
    as_of = "2027-01-29"
    res = fo.compute(as_of, raw, STOCKS)
    d = fo.load_all(as_of, raw, STOCKS)
    # the research path: tg.load_all's QQQ instrument, exact_run on the window, eval_ids metrics
    master = d.qqq.s.sessions
    s = d.qqq.s
    ins = tg.make_inst("QQQ", st.HALF_SPREAD_BPS["QQQ"], s.sessions, master, s.open, s.high, s.low, s.close, s.split,
                       s.div, s.tr.to_numpy(float), d.qqq.factor)
    oneq_tr = d.oneq.s.tr.reindex(master)
    data = {"master": master, "insts": {"QQQ": ins}, "rf": pd.Series(0.0, index=master), "oneq_tr": oneq_tr,
            "oneq": d.oneq}
    w = fo.window_of(d.qqq, as_of)
    start, end = str(w.sessions[0].date()), str(w.sessions[-1].date())
    ev = tg.eval_ids(data, "QQQ", [29876, 29916], start, end, tg.CostModel(), data["rf"])
    ex = tg.exact_run(data, "QQQ", [29876, 29916], start, end, tg.CostModel())
    n_trades = 0
    for j, (name, i) in enumerate(fo.B2_IDS.items()):
        v = res["lines"][name]
        pd.testing.assert_series_equal(v["ret"], ev[i]["r"], check_names=False)
        pd.testing.assert_series_equal(v["hbase"], ex["H_base_res25"], check_names=False)
        pd.testing.assert_series_equal(v["hmatch_full_window"], ev[i]["hm"], check_names=False)
        last = v["ret"].index[-1].to_period("M")
        hm = ev[i]["hm"]
        pd.testing.assert_series_equal(v["hmatch"][v["hmatch"].index.to_period("M") == last],
                                       hm[hm.index.to_period("M") == last], check_names=False)
        mm = ev[i]["m"]
        assert len(v["trades"]) == mm["trips"]
        if mm["trips"]:
            assert np.isclose(np.mean([t["net_pct"] for t in v["trades"]]) * 100, mm["net_per_trip_bp"])
            closed = [t for t in v["trades"] if not t["open"]]
            if closed:
                assert np.isclose(np.mean([t["reason"] == "target" for t in v["trades"]]), mm["share_target"])
        n_trades += mm["trips"]
        assert tg.label(tg.grid().loc[i]) == fo.B2_LABELS[name]
    assert n_trades > 0


def test_fetch_writes_plain_json_and_logs(tmp_path):
    body = json.dumps(payload("QQQ", 1, 0.01, SESSIONS[:5])).encode()
    calls = []
    out = fo.fetch(("QQQ", "AAPL"), raw_dir=tmp_path, log=tmp_path / "log.csv", getter=lambda u: calls.append(u) or body,
                   sleep=lambda s: calls.append(s))
    assert out == {"QQQ": "ok", "AAPL": "ok"} and st.SECONDS_PER_REQUEST in calls
    assert "period1=915148800" in calls[0]          # QQQ from 1999-01-01
    assert json.loads((tmp_path / "QQQ.json").read_text())["chart"]["result"][0]["meta"]["dataGranularity"] == "1d"
    assert len((tmp_path / "log.csv").read_text().splitlines()) == 3
