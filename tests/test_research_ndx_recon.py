"""Synthetic-data tests for scripts/research_ndx_recon.py (no network, no vendor prices)."""
import numpy as np
import pandas as pd

from scripts import research_ndx_recon as m

WIKI = """intro
{| class="wikitable sortable" id="changes"
! rowspan="2" |Date
! colspan="2" |Added
! colspan="2" |Removed
! rowspan="2" |Reason
|-
!Ticker
!Security
!Ticker
!Security
|-
|December 23, 2019
|ANSS
|[[Ansys]]
|HAS
|[[Hasbro]]
|Annual index reconstitution.<ref name="a">{{Cite web |date=December 13, 2019 |title=Annual Changes |url=https://x/y}}</ref>
|-
|November 21, 2019
|EXC
|[[Exelon]]
|CELG
|[[Celgene]]
|[[Celgene]] was acquired by [[Bristol Myers Squibb]].<ref>{{Cite web |date=November 18, 2019 |title=Exelon to join |url=https://x/z}}</ref>
|-
|February 2, 2022
|CEG
|[[Constellation Energy]]
|
|
|Component member Exelon spun off Constellation Energy.
|-
|January 1, 2005
|OLD
|Old
|
|
|Out of window.
|}
"""


def test_parse_and_classify():
    ev = m.build_events_raw(WIKI)
    assert set(ev.ticker) == {"ANSS", "HAS", "EXC", "CELG", "CEG"}
    t = ev.set_index("ticker")
    assert t.loc["ANSS", "kind"] == "annual" and t.loc["ANSS", "type"] == "announced"
    assert t.loc["HAS", "type"] == "index"
    assert t.loc["CELG", "type"] == "acquisition"
    assert t.loc["CEG", "type"] == "spin"
    assert t.loc["ANSS", "ann_wiki"] == "2019-12-13"
    assert t.loc["EXC", "ann_wiki"] == "2019-11-18"
    assert t.loc["ANSS", "event_id"] == "NDX-20191223-ADD-ANSS"


def test_headline_date():
    assert m._inclusion_in_headline("Moderna, Inc. to Join the NASDAQ-100 Index Beginning July 20, 2020") == "2020-07-20"
    assert m._inclusion_in_headline("Walmart Inc. to Join the Nasdaq-100 Index Beginning January 20th, 2026") == \
        "2026-01-20"


def _sessions():
    return pd.bdate_range("2019-12-02", "2020-02-28")


def test_finalize_sessions_after_close():
    ev = m.build_events_raw(WIKI)
    ev = ev[ev.ticker.isin(["ANSS", "HAS"])]
    checks = pd.DataFrame([{"group": "annual-2019", "gnw_published_et": "2019-12-13 20:05",
                            "gnw_url": "https://gnw/x"}])
    fin = m.finalize(ev, checks, _sessions()).set_index("ticker")
    a = fin.loc["ANSS"]
    assert a.announce_date_et == "2019-12-13" and a.announce_time_et == "20:05"
    assert a.ann_session == "2019-12-13"       # Friday, after the close
    assert a.buy_session == "2019-12-16"       # next Monday
    assert a.effective_date == "2019-12-20"    # last session before the Dec 23 inclusion


def test_finalize_weekend_release_and_preopen():
    ev = m.build_events_raw(WIKI)
    ev = ev[ev.ticker == "ANSS"]
    sat = pd.DataFrame([{"group": "annual-2019", "gnw_published_et": "2019-12-14 10:00", "gnw_url": "u"}])
    a = m.finalize(ev, sat, _sessions()).iloc[0]
    assert a.ann_session == "2019-12-13" and a.buy_session == "2019-12-16"
    pre = pd.DataFrame([{"group": "annual-2019", "gnw_published_et": "2019-12-16 08:00", "gnw_url": "u"}])
    a = m.finalize(ev, pre, _sessions()).iloc[0]
    assert a.ann_session == "2019-12-13" and a.buy_session == "2019-12-16"


class FakeAsset:
    def __init__(self, px, ret):
        self.px, self.ret = np.asarray(px, float), np.asarray(ret, float)
        self.dv = np.full(len(px), 1e9)
        self.last_i = len(px) - 1


def test_simulate_flat_oneq_and_event_gain():
    n = 10
    sess = pd.bdate_range("2020-01-02", periods=n)
    oneq_px, oneq_ret = np.full(n, 50.0), np.zeros(n)
    px = np.array([100, 100, 100, 110, 110, 110, 110, 110, 110, 110], float)
    ret = np.r_[0, np.diff(px) / px[:-1]]
    assets = {"E1": FakeAsset(px, ret)}
    plan = [{"key": "E1", "entry_i": 2, "exit_i": 4, "order": (0,), "leg": "R1"}]
    res = m.simulate(plan, assets, sess, oneq_ret, oneq_px)
    tr = res["trades"].iloc[0]
    assert tr.exit_kind == "planned" and tr.days == 2
    assert 0.09 < tr.ret < 0.10 and abs(tr.excess - tr.ret) < 1e-12   # flat ONEQ
    # one tenth of NAV gained ~10%: NAV up ~1%, minus costs
    assert 10_080 < res["nav"].iloc[-1] < 10_100
    assert res["exposure"].iloc[3] > 0.09 and res["exposure"].iloc[-1] == 0


def test_simulate_slots_and_skip():
    n = 6
    sess = pd.bdate_range("2020-01-02", periods=n)
    assets = {f"E{k}": FakeAsset(np.full(n, 20.0), np.zeros(n)) for k in range(12)}
    plan = [{"key": f"E{k}", "entry_i": 1, "exit_i": 3, "order": (k,)} for k in range(12)]
    plan.append({"key": "E0", "entry_i": 4, "exit_i": 4, "order": (0,)})   # exit not after entry: skipped
    res = m.simulate(plan, assets, sess, np.zeros(n), np.full(n, 50.0))
    assert res["skipped_full"] == 2 and res["skipped_price"] == 1
    assert res["npos"].max() == 10


def test_car_and_cluster_t():
    sess = pd.bdate_range("2020-01-02", periods=40)
    bench = {b: pd.DataFrame({"px": 1.0, "ret": np.zeros(40)}, index=sess) for b in ("QQQ", "ONEQ")}
    r = np.zeros(40)
    r[5:8] = 0.01          # three days of +1% between buy and effective
    assets = {"A": FakeAsset(np.full(40, 10.0), r)}
    ev = pd.DataFrame([{"event_id": "A", "group": "g", "kind": "annual", "side": "add", "type": "announced",
                        "ticker": "A", "inclusion_date": str(sess[9].date()), "effective_date": str(sess[8].date()),
                        "ann_session": str(sess[3].date()), "buy_session": str(sess[4].date())}])
    t = m.car_table(ev, sess, bench, assets).iloc[0]
    assert abs(t.r1_QQQ - 0.03) < 1e-12 and abs(t.ann_QQQ - 0.03) < 1e-12 and t.post20_ONEQ == 0
    mean, tt, n = m._t(pd.Series([0.01, 0.02, 0.03]))
    assert n == 3 and abs(mean - 0.02) < 1e-12 and tt > 3
