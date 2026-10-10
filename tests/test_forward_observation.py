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
    assert fo.parse_rows(text) == {"B1": {}, "SEL-A": {}, "SEL-P": {}, "B3": {}}
    assert "observation starts at the 2026-10-30 month-end signal" in text


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
                   sleep=lambda s: calls.append(s), history=None, source="yahoo")
    assert out == {"QQQ": "ok (yahoo)", "AAPL": "ok (yahoo)"} and st.SECONDS_PER_REQUEST in calls
    assert "period1=915148800" in calls[0]          # QQQ from 1999-01-01
    assert json.loads((tmp_path / "QQQ.json").read_text())["chart"]["result"][0]["meta"]["dataGranularity"] == "1d"
    assert len((tmp_path / "log.csv").read_text().splitlines()) == 3


# ================================================================== B3: S-MISP short overlay (scripts/forward_smisp.py)

from scripts import forward_smisp as fs  # noqa: E402
from scripts import research_reversal_dev as rev  # noqa: E402
from scripts import research_short_overlay as ro  # noqa: E402

N_NAMES = 26
SIGS = ("2026-10-30", "2026-11-30", "2026-12-31")


def chart(sym: str, seed: int, sessions=SESSIONS, mutate_after: str | None = None, stop: str | None = None) -> dict:
    """Yahoo v8 body with volume (split-adjusted OHLC, adjclose, a quarterly dividend)."""
    rng = np.random.default_rng(seed)
    if stop:
        sessions = sessions[sessions <= pd.Timestamp(stop)]
    vol = 0.02 + 0.02 * (seed % 5) / 4
    r = rng.standard_t(4, len(sessions)) * vol / math.sqrt(2) + 0.0002 * (seed % 7 - 3)
    c = (40 + 5 * seed) * np.exp(np.cumsum(r))
    o = c * np.exp(rng.normal(0, vol / 3, len(c)))
    h = np.maximum(o, c) * 1.01
    lo = np.minimum(o, c) * 0.99
    v = rng.lognormal(math.log(1e6 * (1 + seed % 9)), 0.3, len(c))
    ts = [int(pd.Timestamp(d.date(), tz="America/New_York").replace(hour=9, minute=30).timestamp()) for d in sessions]
    divs = {str(ts[k]): {"date": ts[k], "amount": 0.2} for k in range(40, len(c), 63)}
    adj = c.copy()
    for k in range(40, len(c), 63):
        adj[:k] *= 1 - 0.2 / c[k - 1]
    if mutate_after:
        cut = pd.Timestamp(mutate_after)
        for k, d in enumerate(sessions):
            if d > cut:
                f = 0.4 if k % 2 else 2.5
                c[k] *= f
                o[k] *= f
                h[k] *= f
                lo[k] *= f
                adj[k] *= f
                v[k] *= 9
    return {"chart": {"result": [{"timestamp": ts, "meta": {"dataGranularity": "1d", "symbol": sym},
                                  "indicators": {"quote": [{"open": list(o), "high": list(h), "low": list(lo),
                                                            "close": list(c), "volume": list(v)}],
                                                 "adjclose": [{"adjclose": list(adj)}]},
                                  "events": {"dividends": divs}}]}}


def facts(cik: int, seed: int, future: bool = False) -> dict:
    """A companyfacts payload: annual 10-K facts FY2022-2025 (filed each February), share counts; ``future`` adds a
    wild FY2026 10-K filed 2026-11-10 and restates FY2025 on that date (after the first signal)."""
    rng = np.random.default_rng(1000 + seed)
    years = [2022, 2023, 2024, 2025]
    g = {"rev": 1e9 * (1 + seed / 10), "ta": 2e9 * (1 + rng.random())}
    gaap, dei = {}, {}

    def add(concept, unit, y, val, dur=True, filed=None):
        item = {"end": f"{y}-12-31", "val": float(val), "filed": filed or f"{y + 1}-02-15", "form": "10-K"}
        if dur:
            item["start"] = f"{y}-01-01"
        gaap.setdefault(concept, {"units": {}})["units"].setdefault(unit, []).append(item)

    def year(y, k, filed=None, wild=1.0):
        rev_ = g["rev"] * (1 + 0.1 * rng.normal()) ** k * wild
        ta = g["ta"] * (1 + 0.05 * k + 0.2 * rng.random()) * wild
        ni = rev_ * (0.15 * rng.normal())
        vals = {"Revenues": rev_, "CostOfRevenue": rev_ * (0.3 + 0.4 * rng.random()),
                "OperatingIncomeLoss": rev_ * 0.1 * rng.normal() + rev_ * 0.05, "NetIncomeLoss": ni,
                "NetCashProvidedByUsedInOperatingActivities": ni + rev_ * 0.1 * rng.normal()}
        for cpt, v in vals.items():
            add(cpt, "USD", y, v, True, filed)
        add("EarningsPerShareDiluted", "USD/shares", y, ni / 1e8, True, filed)
        add("WeightedAverageNumberOfDilutedSharesOutstanding", "shares", y, 1e8 * (1 + 0.05 * k * rng.random()) * wild,
            True, filed)
        inst = {"Assets": ta, "AssetsCurrent": ta * 0.4, "LiabilitiesCurrent": ta * 0.2 * (1 + rng.random()),
                "Liabilities": ta * 0.5, "StockholdersEquity": ta * 0.5, "CashAndCashEquivalentsAtCarryingValue": ta * 0.1,
                "LongTermDebtNoncurrent": ta * 0.2 * rng.random(), "RetainedEarningsAccumulatedDeficit": ta * rng.normal() * 0.2}
        for cpt, v in inst.items():
            add(cpt, "USD", y, v, False, filed)
        dei.setdefault("EntityCommonStockSharesOutstanding", {"units": {"shares": []}})["units"]["shares"].append(
            {"end": filed or f"{y + 1}-02-10", "val": 1e8 * (1 + 0.1 * rng.random()) * wild,
             "filed": filed or f"{y + 1}-02-15", "form": "10-K"})

    for k, y in enumerate(years):
        year(y, k)
    if future:
        year(2026, 4, filed="2026-11-10", wild=7.0)
        year(2025, 3, filed="2026-11-10", wild=0.1)
    return {"cik": cik, "facts": {"us-gaap": gaap, "dei": dei}}


def names():
    return [(f"S{k:02d}", 900000 + k, k + 1) for k in range(N_NAMES)]


def smisp_base() -> pd.DataFrame:
    return pd.DataFrame([{"security_id": str(cik), "ticker": sym, "yahoo": sym, "cik": str(cik), "sic": 3570.0,
                          "multi_class_group": None, "source": "frozen", "eligible": True}
                         for sym, cik, _ in names()])


def write_charts(d, mutate_after=None, stop=None):
    d.mkdir(parents=True, exist_ok=True)
    for sym, _, seed in names():
        st_ = stop if (stop and sym == "S03") else None
        (d / f"{sym}.json.gz").write_bytes(__import__("gzip").compress(json.dumps(
            chart(sym, seed, mutate_after=mutate_after, stop=st_)).encode()))
    return d


def empty_lists():
    return pd.DataFrame({"security_id": pd.Series(dtype=str), "as_of": pd.Series(dtype="datetime64[ns]"),
                         "market_cap": pd.Series(dtype=float)})


def signal_at(s, chart_dir, raw, future=False):
    qqq = fo.load_market("QQQ", "2027-01-29", raw)
    fx = {cik: facts(cik, seed, future) for _, cik, seed in names()}
    return fs.compute_signal(s, smisp_base(), chart_dir, lambda ciks: {c: fx[c] for c in ciks}, qqq, qqq.s.sessions,
                             lists=empty_lists())


@pytest.fixture(scope="module")
def smisp_env(tmp_path_factory):
    return make_smisp_env(tmp_path_factory.mktemp("smisp"))


def make_smisp_env(root):
    """Raw QQQ / ONEQ, the synthetic charts and the three frozen signal files of a B3 forward run."""
    raw = write_raw(root)
    charts = write_charts(root / "charts", stop="2026-12-10")      # S03 stops trading on 2026-12-10
    sm = root / "smisp"
    for s in SIGS:
        sc, meta = signal_at(s, charts, raw)
        out = fs.signal_path(s, sm / "state")
        out.parent.mkdir(parents=True, exist_ok=True)
        sc.to_csv(out, index=False)
    held = sm / "held"
    held.mkdir()
    for p in charts.glob("*.json.gz"):
        (held / p.name).write_bytes(p.read_bytes())
    effr = "Effective Date,Rate Type,Rate (%)\n" + "".join(
        f"{d:%m/%d/%Y},EFFR,{3.5 + 0.001 * k:.3f}\n" for k, d in enumerate(pd.bdate_range("2026-06-01", "2027-02-01")))
    (sm / "EFFR_nyfed.csv").write_text(effr)
    return {"root": root, "raw": raw, "charts": charts, "smisp": sm}


# ------------------------------------------------------------------ signal: point in time


def test_smisp_signal_no_lookahead(smisp_env, tmp_path):
    raw, s = smisp_env["raw"], "2026-10-30"
    a, ma = signal_at(s, smisp_env["charts"], raw)
    wild = write_charts(tmp_path / "wild", mutate_after=s, stop="2026-12-10")
    b, mb = signal_at(s, wild, raw, future=True)        # later prices wild, a later 10-K and a restatement filed later
    pd.testing.assert_frame_equal(a, b)
    assert ma["worst_2n"] == mb["worst_2n"] and len(ma["worst_2n"]) == 20
    assert ma["u300"] == ma["ranked"] == N_NAMES - 2        # two names closed below $10 at the week end: unranked
    assert (pd.to_datetime(a["filed"]) < pd.Timestamp(s)).all()
    assert a["S-MISP"].notna().sum() == ma["u300"]
    # the loaders drop every row after the signal date
    fr = fs.price_frames({"x": wild / "S01.json.gz"}, pd.bdate_range("2026-01-02", "2026-12-31"), s)
    assert fr["close"]["x"].last_valid_index() <= pd.Timestamp(s)


def test_smisp_scores_are_the_research_composite(smisp_env):
    raw, s = smisp_env["raw"], "2026-11-30"
    qqq = fo.load_market("QQQ", "2027-01-29", raw)
    base = smisp_base()
    grid = qqq.s.sessions[(qqq.s.sessions <= s) & (qqq.s.sessions >= pd.Timestamp(s) - pd.Timedelta(days=460))]
    fr = fs.price_frames({r.security_id: smisp_env["charts"] / f"{r.yahoo}.json.gz" for r in base.itertuples()},
                         grid, s)
    cand = fs.u300_candidates(fs.universe_ranks(base, fr, fs.week_end_on_or_before(s, qqq.s.sessions)), fr, s)
    fx = {cik: facts(cik, seed) for _, cik, seed in names()}
    panel = fs.build_panel(cand, fr, fx, qqq.s.tr.reindex(grid), s, empty_lists())
    sc = fs.scores_of(panel)
    np.testing.assert_allclose(sc["S-MISP"].to_numpy(), ro.misp_score(panel).to_numpy(), equal_nan=True)
    np.testing.assert_allclose(sc["borrow"].to_numpy(), ro.borrow_rates(panel).to_numpy())
    # research momentum: index(t-21) / index(t-252) - 1 on the total-return index
    sid = panel["security_id"].iloc[0]
    ix = fr["idx"][sid]
    k = ix.index.get_loc(pd.Timestamp(s))
    assert np.isclose(panel["mom_12_1"].iloc[0], ix.iloc[k - 21] / ix.iloc[k - 252] - 1)
    # the universe rank is by 50-session median dollar volume, ties by security id
    w = fs.week_end_on_or_before(s, qqq.s.sessions)
    assert w == pd.Timestamp("2026-11-27")                       # 11-30 is a Monday: the previous Friday
    dv = (fr["close"] * fr["volume"]).rolling(50, min_periods=25).median().loc[w]
    dv = dv[fr["close"].loc[:w].iloc[-6:].ffill().iloc[-1] >= 10]             # week close >= $10
    assert list(cand.sort_values("dv50_rank")["security_id"]) == list(dv.sort_values(ascending=False).index)


# ------------------------------------------------------------------ simulation: the research functions


def research_reference(env, as_of: str):
    """research_short_overlay.build_market / schedule / simulate on the same synthetic data."""
    raw, sm = env["raw"], env["smisp"]
    qqq = fo.load_market("QQQ", as_of, raw)
    sigs = {pd.Timestamp(s): fs.load_signal(fs.signal_path(s, sm / "state")) for s in SIGS
            if pd.Timestamp(s) < pd.Timestamp(as_of)}
    sc = pd.concat(sigs.values(), ignore_index=True)
    sc["s"] = pd.to_datetime(sc["s"]).astype("datetime64[ns]")
    ses = qqq.s.sessions
    grid = ses[(ses >= pd.Timestamp(SIGS[0])) & (ses <= pd.Timestamp(as_of))]
    files = fs.held_files(sigs, sm / "held")
    fr = fs.price_frames(files, grid, as_of)
    close = fr["close"]
    tr = fr["tr"].copy()
    has = close.notna()
    events = []
    for sid in close.columns:
        lr = has[sid][::-1].idxmax()
        if lr < grid[-1]:
            nxt = grid[grid > lr][0]
            tr.loc[nxt, sid] = 0.0
            events.append({"security_id": sid, "booked_on": str(nxt.date()), "status": "no_terminal_record",
                           "terminal_return": 0.0})

    class D:
        pass
    data = D()
    data.sessions, data.sig_idx, data.close = grid, rev.make_index(tr, close), close.ffill()
    data.terminal_events = pd.DataFrame(events, columns=["security_id", "booked_on", "status", "terminal_return"])
    data.qqq_close = pd.Series(qqq.s.close, index=ses).reindex(grid)
    effr = ro.load_effr(sm / "EFFR_nyfed.csv")
    mk = ro.build_market(data, list(close.columns), effr, qqq.s.tr.reindex(grid))
    sched = ro.schedule(mk, sc, "S-MISP", 10)
    res = ro.simulate(mk, sched, ro.Cfg(signal="S-MISP", n=10, k=0.2, mode="MN"), sched[0][0], len(grid) - 1)
    return mk, sched, res


def test_b3_matches_research_short_overlay(smisp_env):
    as_of = "2027-01-29"
    res = fo.compute(as_of, smisp_env["raw"], STOCKS, smisp_dir=smisp_env["smisp"],
                     borrow_dir=smisp_env["root"] / "no_borrow")
    v = res["lines"]["B3"]
    assert v["started"] and v["first_trade"] == pd.Timestamp("2026-11-02")
    mk, sched, ref = research_reference(smisp_env, as_of)
    pd.testing.assert_series_equal(v["nav"], ref["nav"]["nav"])
    assert v["res"]["acc"] == ref["acc"] and ref["acc"]["delisted_covered"] >= 0
    assert [e[0] for e in v["sched"]] == [e[0] for e in sched]
    rows = v["rows"]
    mr = ro.month_returns(ref["nav"]["nav"])
    assert [r["month"] for r in rows] == ["2026-11", "2026-12", "2027-01"]
    assert np.allclose([r["ret"] for r in rows], mr.to_numpy())
    # the short leg is the research ideal basket over each holding period
    legs = ro.basket_diagnostics(mk, sched, 10, sched[0][0], len(mk.sessions) - 1)
    assert np.isclose(rows[0]["leg"], legs["basket"].iloc[0]) and np.isclose(rows[0]["leg_qqq"], legs["qqq"].iloc[0])
    assert rows[0]["leg_period"] == "11-02→12-01"
    assert [r["status"] for r in rows] == ["final", "final", "provisional"]
    # the shorts are the names held after the month's rebalance, at most N, from the worst 2N of the signal
    held0 = ref["held"][0][1]
    assert 0 < len(held0) <= 10 and set(held0) <= set(sched[0][2])
    # no IBKR snapshot: the IBKR-fee sensitivity equals the strategy
    assert rows[0]["fee_days"] == 0 and np.isclose(rows[0]["ret_ibkr"], rows[0]["ret"])
    # MN: about (1 + k) QQQ at the first close
    nav0 = v["res"]["nav"].iloc[0]
    assert abs(nav0["qqq"] / nav0["nav"] - 1.2) < 0.03 and abs(nav0["short"] / nav0["nav"] - 0.2) < 0.06


def test_b3_no_lookahead_and_final_rows(smisp_env, tmp_path):
    env = dict(smisp_env)
    a = fo.compute("2026-12-31", env["raw"], STOCKS, smisp_dir=env["smisp"], borrow_dir=tmp_path)
    # wild prices after the as-of date in every held chart and in QQQ / ONEQ change nothing up to it
    sm2 = tmp_path / "smisp"
    for s in SIGS:
        fs.signal_path(s, sm2 / "state").parent.mkdir(parents=True, exist_ok=True)
        fs.signal_path(s, sm2 / "state").write_bytes(fs.signal_path(s, env["smisp"] / "state").read_bytes())
    (sm2 / "EFFR_nyfed.csv").write_bytes((env["smisp"] / "EFFR_nyfed.csv").read_bytes())
    write_charts(sm2 / "held", mutate_after="2026-12-31", stop="2026-12-10")
    raw2 = write_raw(tmp_path / "r2", mutate_after="2026-12-31")
    b = fo.compute("2026-12-31", raw2, STOCKS, smisp_dir=sm2, borrow_dir=tmp_path)
    assert [fo.b3_row_line(r) for r in a["lines"]["B3"]["rows"]] == [fo.b3_row_line(r) for r in b["lines"]["B3"]["rows"]]
    # a final row does not change when later data arrive
    late = fo.compute("2027-01-29", env["raw"], STOCKS, smisp_dir=env["smisp"], borrow_dir=tmp_path)
    fin = [r for r in a["lines"]["B3"]["rows"] if r["status"] == "final"]
    assert [r["month"] for r in fin] == ["2026-11"]
    later = {r["month"]: r for r in late["lines"]["B3"]["rows"]}
    assert all(fo.b3_row_line(later[r["month"]]) == fo.b3_row_line(r) for r in fin)


def test_b3_log_idempotent_and_pending(smisp_env, tmp_path):
    log = tmp_path / "log.md"
    kw = {"smisp_dir": smisp_env["smisp"], "borrow_dir": tmp_path, "stocks": STOCKS, "raw_dir": smisp_env["raw"]}
    fo.run("2026-12-31", log_path=log, **kw)
    t1 = log.read_text()
    fo.run("2026-12-31", log_path=log, **kw)
    assert log.read_text() == t1
    rows = fo.parse_rows(t1)["B3"]
    assert sorted(rows) == ["2026-11", "2026-12"] and rows["2026-11"].endswith("| final |")
    assert rows["2026-12"].endswith("| provisional |")
    # before the first signal is executable: the start message, no rows, no data needed
    res = fo.compute("2026-10-30", smisp_env["raw"], STOCKS, smisp_dir=tmp_path / "nothing")
    assert not res["lines"]["B3"]["started"]
    assert "observation starts at the 2026-10-30" in fo.render_status("2026-10-30", "2026-10-30", res["lines"])
    # a due signal without its file: pending, nothing simulated
    res = fo.compute("2026-11-05", smisp_env["raw"], STOCKS, smisp_dir=tmp_path / "nothing")
    assert not res["lines"]["B3"]["started"] and "2026-10-30 not computed" in res["lines"]["B3"]["reason"]


# ------------------------------------------------------------------ IBKR borrow-fee snapshots


BORROW_TXT = """#BOF|{stamp}
#SYM|CUR|NAME|CON|ISIN|REBATERATE|FEERATE|AVAILABLE|FIGI|
AAPL|USD|APPLE INC|265598|XXXXXXX31005|3.4719|0.4081|>10000000|BBG000B9XRY4|
BRK B|USD|BERKSHIRE HATHAWAY INC-CL B|72063691|XXXXXXX07026|3.6018|0.2782|>10000000|BBG000DWG505|
HARD|USD|HARD TO BORROW INC|1|X|-40.5|{fee}|0|BBG1|
GME|USD|GAMESTOP CORP-CLASS A|36285627|XXXXXXXW1099|3.5890|0.2910|150000|BBG000BB5BF6|
#EOF|4
"""


def test_borrow_fee_parser_and_snapshot_choice(tmp_path):
    stamp, df = fs.parse_borrow_text(BORROW_TXT.format(stamp="2026.10.09|22:47:23", fee="55.25"))
    assert stamp == pd.Timestamp("2026-10-09 22:47:23")
    assert df.loc["AAPL", "FEERATE"] == pytest.approx(0.4081) and df.loc["AAPL", "AVAILABLE"] > 1e7
    assert df.loc["HARD", "AVAILABLE"] == 0 and df.loc["HARD", "FEERATE"] == pytest.approx(55.25)
    assert df.loc["GME", "AVAILABLE"] == 150000 and "BRK B" in df.index
    assert list(fs.parse_borrow_text(BORROW_TXT.format(stamp="2026.10.09|22:47:23", fee="1"), ["GME"])[1].index) == ["GME"]
    with pytest.raises(ValueError):
        fs.parse_borrow_text("SYM|FEE\n")
    assert fs.ibkr_symbol("BRK.B") == "BRK B" and fs.ibkr_symbol("brk-b") == "BRK B"
    # two snapshots: the latest one whose own file time is on or before the day
    raw = tmp_path / "raw"
    raw.mkdir()
    import gzip
    for name, st_, fee in (("usa_20261010T030139Z.txt.gz", "2026.10.09|22:47:23", "55.25"),
                           ("usa_20261012T223000Z.txt.gz", "2026.10.12|18:30:00", "80.00")):
        with gzip.open(raw / name, "wt") as fh:
            fh.write(BORROW_TXT.format(stamp=st_, fee=fee))
    snaps = fs.BorrowSnapshots(tmp_path)
    assert snaps.on("2026-10-08", ["HARD"]) == (None, {"HARD": None})
    t, f = snaps.on("2026-10-11", ["HARD", "NONE"])
    assert t == pd.Timestamp("2026-10-09 22:47:23") and f["HARD"] == (55.25, 0.0) and f["NONE"] is None
    assert snaps.on("2026-10-12", ["HARD"])[1]["HARD"] == (80.0, 0.0)
    assert snaps.on("2026-11-30", ["AAPL"])[1]["AAPL"][0] == pytest.approx(0.4081)


def test_b3_ibkr_fee_columns(smisp_env, tmp_path):
    """IBKR fees from a snapshot for every held name: summary columns and the report-only sensitivity."""
    import gzip
    sigs = {pd.Timestamp(s): fs.load_signal(fs.signal_path(s, smisp_env["smisp"] / "state")) for s in SIGS}
    tick = sorted({t for sc in sigs.values() for t in sc["ticker"]})
    body = "#BOF|2026.10.30|18:30:00\n#SYM|CUR|NAME|CON|ISIN|REBATERATE|FEERATE|AVAILABLE|FIGI|\n" + "".join(
        f"{t}|USD|{t}|1|X|3.0|{(9.0 if t == 'S00' else 0.25):.2f}|{0 if t == 'S00' else 5000}|B|\n"
        for t in tick if t != "S01") + "#EOF\n"
    (tmp_path / "raw").mkdir()
    with gzip.open(tmp_path / "raw" / "usa_20261030T223000Z.txt.gz", "wt") as fh:
        fh.write(body)
    res = fo.compute("2027-01-29", smisp_env["raw"], STOCKS, smisp_dir=smisp_env["smisp"], borrow_dir=tmp_path)
    v = res["lines"]["B3"]
    rows = v["rows"]
    f = v["fees"]
    assert len(f) and set(f["snapshot"].dropna()) == {pd.Timestamp("2026-10-30 18:30:00")}
    r0 = rows[0]
    held0 = {v["tick"][s] for s in v["res"]["held"][0][1]}
    assert r0["fee_days"] == (pd.Timestamp("2026-12-01") - pd.Timestamp("2026-11-02")).days
    in_file = held0 - {"S01"}
    exp_mean = np.mean([0.09 if t == "S00" else 0.0025 for t in in_file])
    assert np.isclose(r0["fee_mean"], exp_mean)
    assert r0["unborrowable"] == (["S00"] if "S00" in held0 else [])
    assert r0["not_in_file"] == (["S01"] if "S01" in held0 else [])
    line = fo.b3_row_line(r0)
    assert ("S00 (AVAILABLE 0)" in line) == ("S00" in held0)
    # the sensitivity differs from the strategy by the fee difference only (strategy unchanged)
    assert np.isclose(r0["ret"], ro.month_returns(v["nav"]).iloc[0])
    assert r0["ret_ibkr"] != r0["ret"]


def test_universe_base_identity_and_classification():
    frozen = pd.DataFrame([
        {"security_id": "1", "ticker": "OLD", "eligible": True, "non_common": False, "spac_shell": False,
         "foreign": False, "investment_company": False, "cik": "11", "sic": "3570", "multi_class_group": None},
        {"security_id": "2", "ticker": "KEEP", "eligible": True, "non_common": False, "spac_shell": False,
         "foreign": False, "investment_company": False, "cik": "22", "sic": "6022", "multi_class_group": None},
        {"security_id": "3", "ticker": "FRGN", "eligible": False, "non_common": False, "spac_shell": False,
         "foreign": True, "investment_company": False, "cik": "33", "sic": "3570", "multi_class_group": None}])
    listing = pd.DataFrame({"Symbol": ["NEWT", "KEEP", "FRGN", "IPO", "ADR", "WRNT", "ETFX", "NOID"],
                            "Name": ["Old Co - Common Stock", "Keep Bank - Common Stock", "Foreign - Common Stock",
                                     "New Co - Common Stock", "Overseas - Class A Ordinary Shares",
                                     "New Co - Warrants", "Fund ETF", "Unknown - Common Stock"],
                            "ETF": ["N", "N", "N", "N", "N", "N", "Y", "N"], "Test Issue": ["N"] * 8})
    tick = {"NEWT": 11, "KEEP": 22, "FRGN": 33, "IPO": 44, "ADR": 55, "WRNT": 44}
    base = fs.base_rows(listing, frozen, tick)
    by = base.set_index("ticker")
    assert set(by.index) == {"NEWT", "KEEP", "FRGN", "IPO", "ADR", "NOID"}      # warrants and ETFs are not common
    assert by.loc["NEWT", "source"] == "frozen_cik" and by.loc["NEWT", "security_id"] == "1"
    assert by.loc["KEEP", "source"] == "frozen" and by.loc["IPO", "source"] == "new" and by.loc["NOID", "source"] == "no_cik"
    subs = {44: {"sic": "2834", "filings": {"recent": {"form": ["424B4", "S-1/A", "S-1"]}}},
            55: {"sic": "3674", "filings": {"recent": {"form": ["6-K", "F-1"]}}}}
    assert fs.needs_submissions(base) == [44, 55]
    b = fs.finish_base(base, subs).set_index("ticker")
    assert b.loc["IPO", "eligible"] and b.loc["IPO", "sic"] == 2834
    assert not b.loc["ADR", "eligible"] and not b.loc["FRGN", "eligible"]
    assert b.loc["KEEP", "eligible"] and b.loc["NEWT", "eligible"]               # financials leave at U300, not here
    assert "NOID" not in b.index
    assert fs.classify_submissions({"sic": "6770", "filings": {"recent": {"form": ["10-Q"]}}})["spac"]
    assert fs.classify_submissions({"sic": "3570", "filings": {"recent": {"form": ["20-F", "10-K"]}}})["foreign"]
    assert not fs.classify_submissions({"sic": "3570", "filings": {"recent": {"form": ["10-Q", "20-F"]}}})["foreign"]


def test_signal_dates_and_week_end():
    ses = pd.bdate_range("2026-10-01", "2026-12-04")
    assert fs.signal_dates(ses) == [pd.Timestamp("2026-10-30"), pd.Timestamp("2026-11-30")]
    assert fs.signal_dates(ses[ses <= "2026-10-30"]) == []       # 10-30 is known as month end once 11-02 exists
    assert fs.week_end_on_or_before("2026-10-30", ses) == pd.Timestamp("2026-10-30")
    assert fs.week_end_on_or_before("2026-11-30", ses) == pd.Timestamp("2026-11-27")
    assert fs.week_end_on_or_before("2026-10-30", ses[ses <= "2026-10-30"]) == pd.Timestamp("2026-10-30")


# ================================================================== price sources (scripts/forward_prices.py)

from scripts import forward_prices as fp  # noqa: E402


def test_history_state_roundtrip_and_tail_merge(tmp_path):
    """The committed state holds ratios only; history + tail rebuild the original chart exactly."""
    full = payload("QQQ", 1, 0.015, split_on="2025-06-02")
    end = "2026-10-09"
    fp.write_history_state({"QQQ": full}, end, tmp_path / "h.csv.gz")
    state = fp.load_history_state(tmp_path / "h.csv.gz")
    assert set(state.columns) == set(fp.HISTORY_COLS)
    assert state["c_r"].between(0.3, 3).all()                  # ratios, not levels
    df, divs, spl = fp.payload_frame(full)
    tail_df = df[df.index >= pd.Timestamp(end) - pd.Timedelta(days=30)].copy()
    tail = fp.chart_payload("QQQ", tail_df, {d: v for d, v in divs.items() if d >= tail_df.index[0]}, {})
    merged, check = fp.merge_history("QQQ", state, tail)
    assert check["overlap_ok"] and check["overlap_days"] > 10 and check["new_days"] > 50
    a = it.build("QQQ", *_ohlc_and_splits(full))
    b = it.build("QQQ", *_ohlc_and_splits(merged))
    np.testing.assert_allclose(a.close, b.close, rtol=1e-9)
    np.testing.assert_allclose(a.tr.to_numpy()[1:], b.tr.to_numpy()[1:], rtol=1e-7, atol=1e-12)
    np.testing.assert_allclose(a.div, b.div, rtol=1e-7, atol=1e-12)
    assert list(a.sessions) == list(b.sessions)
    # a tail that disagrees on the overlapping days is flagged
    bad = tail_df.copy()
    bad.loc[bad.index[3], ["open", "high", "low", "close"]] *= 1.01
    _, chk = fp.merge_history("QQQ", state, fp.chart_payload("QQQ", bad, {}, {}))
    assert not chk["overlap_ok"]


def _ohlc_and_splits(p):
    path = Path(__import__("tempfile").mkdtemp()) / "x.json"
    path.write_text(json.dumps(p))
    return it.parse_ohlc(path, "2027-12-31"), it.split_events(path, "2027-12-31")


from pathlib import Path  # noqa: E402


def test_tiingo_rows_become_the_yahoo_format():
    days = pd.bdate_range("2026-11-02", periods=6)
    raw = [100, 101, 102, 51.5, 52, 53]                       # 2:1 split on day 4
    rows = [{"date": f"{d.date()}T00:00:00.000Z", "open": c, "high": c * 1.01, "low": c * 0.99, "close": c,
             "volume": 1e6, "adjClose": c / (2 if k < 3 else 1) * (0.99 if k < 5 else 1.0),
             "divCash": 0.5 if k == 5 else 0.0, "splitFactor": 2.0 if k == 3 else 1.0}
            for k, (d, c) in enumerate(zip(days, raw))]
    p = fp.tiingo_rows_to_payload("XYZ", rows)
    df, splits = st.ohlc_frame(p, "2027-01-01")
    s = it.build("XYZ", df, splits)
    np.testing.assert_allclose(s.close, raw)                  # real close back from split-adjusted x later splits
    assert splits == {str(days[3].date()): 2.0}
    assert np.isclose(s.div[5], 0.5) and s.split[3] == 2.0
    np.testing.assert_allclose(df["close"].to_numpy()[:3], np.array(raw[:3]) / 2)


def test_alpaca_charts_from_raw_split_all(tmp_path):
    days = pd.bdate_range("2026-11-02", periods=5)
    raw_c = [200.0, 202.0, 50.0, 51.0, 52.0]                    # 4:1 split on day 3
    def bars(adj):
        out = []
        for k, (d, c) in enumerate(zip(days, raw_c)):
            f = 4.0 if (adj in ("split", "all") and k < 2) else 1.0
            cc = round(c / f, 4) * (0.995 if adj == "all" and k < 4 else 1.0)
            out.append({"t": f"{d.date()}T04:00:00Z", "o": cc, "h": cc, "l": cc, "c": cc,
                        "v": 1000.0 * (f if adj != "raw" else 1.0), "n": 1, "vw": cc})
        return out
    calls = []

    def getter(url, headers):
        calls.append(url)
        adj = re.search(r"adjustment=(\w+)", url).group(1)
        return json.dumps({"bars": {"ABC": bars(adj)}, "next_page_token": None}).encode()
    got = fp.alpaca_charts(["ABC", "NOPE"], "2026-11-01", "2026-11-10", headers={"x": "y"}, getter=getter,
                           sleep=lambda s: None)
    assert set(got) == {"ABC"} and len(calls) == 3
    df, splits = st.ohlc_frame(got["ABC"], "2027-01-01")
    s = it.build("ABC", df, splits)
    assert splits == {str(days[2].date()): 4.0}
    np.testing.assert_allclose(s.close, raw_c)
    assert np.isclose(s.tr.iloc[4], 52.0 / (51.0 * 0.995) - 1)    # total return from the adjustment=all close
    assert "feed=sip" in calls[0] and "timeframe=1Day" in calls[0]


import re  # noqa: E402


def test_fetch_uses_tiingo_then_yahoo_and_the_history_state(tmp_path):
    full = payload("QQQ", 1, 0.015)
    fp.write_history_state({"QQQ": full}, "2026-10-09", tmp_path / "h.csv.gz")
    df, divs, _ = fp.payload_frame(full)
    tail = fp.chart_payload("QQQ", df[df.index >= "2026-09-01"], {d: v for d, v in divs.items() if d >= pd.Timestamp("2026-09-01")})

    def tiingo_fail(url, headers):
        raise HTTPError(url, 429, "quota", None, None)
    urls = []
    out = fo.fetch(("QQQ",), raw_dir=tmp_path / "raw", log=tmp_path / "log.csv", history=tmp_path / "h.csv.gz",
                   source="tiingo", tiingo_getter=tiingo_fail,
                   getter=lambda u: urls.append(u) or json.dumps(tail).encode(), sleep=lambda s: None)
    assert out == {"QQQ": "ok (yahoo)"}
    assert f"period1={int(pd.Timestamp('2026-09-09', tz='UTC').timestamp())}" in urls[0]   # tail: 10-09 - 30 days
    a, b = it.parse_ohlc(tmp_path / "raw" / "QQQ.json", "2027-12-31"), None
    ref = tmp_path / "ref.json"
    ref.write_text(json.dumps(full))
    b = it.parse_ohlc(ref, "2027-12-31")
    np.testing.assert_allclose(a["close"].to_numpy(), b["close"].to_numpy(), rtol=1e-9)
    srcs = pd.read_csv(tmp_path / "fetch_sources.csv")
    assert srcs["source"].tolist() == ["yahoo"] and json.loads(srcs["history_check"][0])["overlap_ok"]
    assert fo.b_source(datetime(2026, 10, 20, tzinfo=timezone.utc)) == "yahoo"


from datetime import datetime, timezone  # noqa: E402
from urllib.error import HTTPError  # noqa: E402


def test_smisp_charts_alpaca_first_then_yahoo(tmp_path, monkeypatch):
    monkeypatch.setattr(fs.fp, "alpaca_headers", lambda: {"APCA-API-KEY-ID": "k", "APCA-API-SECRET-KEY": "s"})
    days = pd.bdate_range("2026-10-01", periods=5)

    def alpaca(url, headers):
        bars = [{"t": f"{d.date()}T04:00:00Z", "o": 20.0, "h": 21.0, "l": 19.0, "c": 20.0 + k, "v": 1e5, "n": 1,
                 "vw": 20.0} for k, d in enumerate(days)]
        return json.dumps({"bars": {"AAA": bars}, "next_page_token": None}).encode()
    yurls = []
    body = json.dumps(chart("BBB", 3, sessions=days)).encode()
    src = fs.fetch_charts({"AAA": "AAA", "BBB": "BBB"}, tmp_path, "2026-09-01", "2026-10-08",
                          {"alpaca": alpaca, "yahoo": lambda u: yurls.append(u) or body}, sleep=lambda s: None)
    assert src == {"AAA": "alpaca", "BBB": "yahoo"} and len(yurls) == 1 and "/BBB?" in yurls[0]
    f = fs.load_chart(tmp_path / "AAA.json.gz", "2026-10-07")
    assert list(f["close"]) == [20.0, 21.0, 22.0, 23.0, 24.0] and f.index[-1] == pd.Timestamp("2026-10-07")
    # a second run fetches nothing
    src2 = fs.fetch_charts({"AAA": "AAA", "BBB": "BBB"}, tmp_path, "2026-09-01", "2026-10-08",
                           {"alpaca": lambda u, h: pytest.fail("refetch"), "yahoo": lambda u: pytest.fail("refetch")})
    assert src2 == src


# ================================================================== regression lock (refactor of 2026-10-10)
# The runner was simplified without changing any rule or number. tests/golden/forward_observation_synthetic.json
# holds the log text and the per-day run details that the pre-refactor runner produced on the synthetic data above
# (B1, SEL-A, SEL-P and B3 with trades, chained logs, an IBKR borrow snapshot). Regenerate only for an intended rule
# change: UPDATE_FORWARD_GOLDEN=1 pytest tests/test_forward_observation.py -k golden

import os  # noqa: E402

GOLDEN = Path(__file__).with_name("golden") / "forward_observation_synthetic.json"
GOLDEN_AS_OF = ("2026-10-13", "2026-11-13", "2026-12-31", "2027-01-29")


def _round(o):
    if isinstance(o, float):
        return float(f"{o:.12g}")
    if isinstance(o, dict):
        return {k: _round(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_round(v) for v in o]
    return o


def golden_snapshot(root) -> dict:
    env = make_smisp_env(root)
    borrow = root / "borrow"
    (borrow / "raw").mkdir(parents=True)
    body = "#BOF|2026.10.30|18:30:00\n#SYM|CUR|NAME|CON|ISIN|REBATERATE|FEERATE|AVAILABLE|FIGI|\n" + "".join(
        f"S{k:02d}|USD|X|1|X|3.0|{(9.0 if k == 0 else 0.25 + k / 100):.2f}|{0 if k == 0 else 5000}|B|\n"
        for k in range(N_NAMES) if k != 1) + "#EOF\n"
    with __import__("gzip").open(borrow / "raw" / "usa_20261030T223000Z.txt.gz", "wt") as fh:
        fh.write(body)
    out, old = {}, ""
    for as_of in GOLDEN_AS_OF:
        res = fo.compute(as_of, env["raw"], STOCKS, smisp_dir=env["smisp"], borrow_dir=borrow)
        text, warnings = fo.render_log(res["as_of"], res["data_end"], res["lines"], old)
        old = text
        details = json.loads(fo.save_run_details(res, root / "runs").read_text())
        out[as_of] = {"log": text, "warnings": warnings, "details": _round(details)}
    return out


def _assert_close(a, b, path=""):
    if isinstance(a, float) or isinstance(b, float):
        assert a is not None and b is not None and math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12), (path, a, b)
    elif isinstance(a, dict):
        assert set(a) == set(b), (path, set(a) ^ set(b))
        for k in a:
            _assert_close(a[k], b[k], f"{path}/{k}")
    elif isinstance(a, list):
        assert len(a) == len(b), path
        for k, (x, y) in enumerate(zip(a, b)):
            _assert_close(x, y, f"{path}[{k}]")
    else:
        assert a == b, (path, a, b)


def test_golden_logs_and_daily_numbers_unchanged(tmp_path):
    got = golden_snapshot(tmp_path)
    if os.environ.get("UPDATE_FORWARD_GOLDEN"):
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(got, separators=(",", ":"), ensure_ascii=False) + "\n")
        pytest.skip("golden file rewritten")
    want = json.loads(GOLDEN.read_text())
    assert list(got) == list(want)
    for as_of in want:
        assert got[as_of]["log"] == want[as_of]["log"], as_of            # the log text, byte for byte
        assert got[as_of]["warnings"] == want[as_of]["warnings"]
        _assert_close(got[as_of]["details"], want[as_of]["details"], as_of)
    lines = want["2027-01-29"]["details"]["lines"]
    assert all(len(lines[k]["trades"]) > 0 for k in ("B1", "SEL-A", "SEL-P")) and len(lines["B3"]["held"]) > 0


def _b2_trades_by_counters(data, ids, sessions, j):
    """The pre-refactor extraction, kept as the reference: one truncated research run per forward session, each
    trade inferred from the accumulators (closed counts, net bp, days)."""
    def acc(ex):
        return {key: float(v[:, j].sum()) for key, v in ex["sim"]["acc"].items()}
    start = str(sessions[0].date())
    trades, cur, closed_net, prev = [], None, 0.0, {"nT": 0, "nTarget": 0, "nTimeout": 0, "sDays": 0.0}
    for k in range(len(sessions)):
        a = acc(tg.exact_run(data, "QQQ", ids, start, str(sessions[k].date()), tg.CostModel()))
        done = a["nTarget"] + a["nTimeout"]
        if done > prev["nTarget"] + prev["nTimeout"]:
            cur.update({"exit": sessions[k], "open": False,
                        "reason": "target" if a["nTarget"] > prev["nTarget"] else "timeout",
                        "days": int(round(a["sDays"] - prev["sDays"])), "net_pct": (a["sNet"] - closed_net) / 100})
            trades.append(cur)
            closed_net, cur = a["sNet"], None
            prev = {kk: a[kk] for kk in prev}
        if a["nT"] - done == 1:
            cur = cur or {"symbol": "QQQ", "entry": sessions[k]}
            cur.update({"exit": sessions[k], "open": True, "reason": "end", "days": None,
                        "net_pct": (a["sNet"] - closed_net) / 100})
    return trades + ([cur] if cur else [])


@pytest.mark.parametrize("as_of", ["2026-11-13", "2027-01-29"])
def test_b2_recorded_trades_equal_the_counter_inference(raw, as_of):
    res = fo.compute(as_of, raw, STOCKS)
    d = fo.load_all(as_of, raw, STOCKS)
    data, w = fo.b2_data(d), fo.window_of(d.qqq, as_of)
    ids = list(fo.B2_IDS.values())
    n = 0
    for j, name in enumerate(fo.B2_IDS):
        ref = _b2_trades_by_counters(data, ids, w.sessions, j)
        got = res["lines"][name]["trades"]
        assert [{k: v for k, v in t.items() if k != "net_pct"} for t in got] == \
               [{k: v for k, v in t.items() if k != "net_pct"} for t in ref]
        assert np.allclose([t["net_pct"] for t in got], [t["net_pct"] for t in ref], rtol=1e-12, atol=1e-12)
        n += len(got)
    assert n > 0


def test_exact_run_record_trades_is_optional_and_changes_nothing(raw):
    d = fo.load_all("2027-01-29", raw, STOCKS)
    data, w = fo.b2_data(d), fo.window_of(d.qqq, "2027-01-29")
    ids, start, end = list(fo.B2_IDS.values()), str(w.sessions[0].date()), str(w.sessions[-1].date())
    a = tg.exact_run(data, "QQQ", ids, start, end, tg.CostModel())
    b = tg.exact_run(data, "QQQ", ids, start, end, tg.CostModel(), record_trades=True)
    assert "trades" not in a and "trades" not in a["sim"] and len(b["trades"]) > 0
    for i in ids:
        pd.testing.assert_series_equal(a["R"][i], b["R"][i])
    for key in a["sim"]["acc"]:
        np.testing.assert_array_equal(a["sim"]["acc"][key], b["sim"]["acc"][key])
    np.testing.assert_array_equal(a["sim"]["rec"]["x"], b["sim"]["rec"]["x"])
    # the recorded trades add up to the accumulators
    for k in range(len(ids) + 4):
        tk = [t for t in b["trades"] if t["cfg"] == k]
        A = {key: v[:, k].sum() for key, v in a["sim"]["acc"].items()}
        assert len(tk) == A["nT"] and sum(t["reason"] == "target" for t in tk) == A["nTarget"]
        assert np.isclose(sum(t["net_bp"] for t in tk), A["sNet"]) and sum(t["days"] for t in tk) == A["sDays"]
