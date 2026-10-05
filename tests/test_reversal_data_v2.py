"""Data version 2 of the reversal 2012-2026 data: the version switch, the archive fetcher's pure helpers and the
fill / fix / third-vote rules (scripts/reversal_data_v2_archive.py, scripts/reversal_data_v2_fill.py)."""
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from scripts import reversal_data_common as common
from scripts import reversal_data_v2_archive as arch
from scripts import reversal_data_v2_fill as fill

ROOT = Path(__file__).resolve().parents[1]


def _sessions(start="2013-01-01", end="2013-03-31"):
    import exchange_calendars as xcals
    cal = xcals.get_calendar("XNAS", start="2012-01-03", end="2014-12-31")
    s = pd.DatetimeIndex(cal.sessions).tz_localize(None)
    return s[(s >= start) & (s <= end)]


# ------------------------------------------------------------------ version switch


def _paths_for(version: str) -> str:
    env = {**os.environ, "REVERSAL_DATA_VERSION": version, "PYTHONPATH": str(ROOT)}
    code = "from scripts import reversal_data_common as c; print(c.DATA_VERSION, c.CACHE.name, c.INPUTS.name)"
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True).stdout.strip()


def test_version_switch_points_v2_at_its_own_copies():
    assert _paths_for("v1") == "v1 reversal_2012_2026 inputs"
    assert _paths_for("v2") == "v2 reversal_2012_2026_v2 inputs_v2"


def test_unknown_version_is_refused():
    env = {**os.environ, "REVERSAL_DATA_VERSION": "v3", "PYTHONPATH": str(ROOT)}
    out = subprocess.run([sys.executable, "-c", "import scripts.reversal_data_common"], env=env, capture_output=True,
                         text=True)
    assert out.returncode != 0 and "v1 or v2" in out.stderr


def test_default_is_v1_and_the_fetcher_never_uses_the_v1_ledger(monkeypatch):
    assert common.V1_CACHE.name == "reversal_2012_2026"
    monkeypatch.setattr(common, "QUOTA_LEDGER", common.QUOTA_LEDGER)  # restored after the test
    monkeypatch.setattr(common, "RAW_INDEX", common.RAW_INDEX)
    arch.use_own_ledger()  # called before every request of the fetcher
    assert "reversal_2012_2026_v2_fill" in str(arch.common.QUOTA_LEDGER)
    assert "reversal_2012_2026_v2_fill" in str(arch.common.RAW_INDEX)


# ------------------------------------------------------------------ archive fetcher helpers


def test_csv_url_facts_daily_dividends_and_span():
    f = arch.csv_url_facts("http://ichart.finance.yahoo.com/table.csv?s=BMC&a=0&b=2&c=2012&d=5&e=1&f=2013&g=d")
    assert f == {"ticker": "BMC", "kind": "daily", "start": "2012-01-02", "end": "2013-06-01"}
    assert arch.csv_url_facts("http://ichart.finance.yahoo.com/table.csv?s=BMC&g=v")["kind"] == "dividends"
    assert arch.csv_url_facts("http://ichart.finance.yahoo.com/table.csv?s=BMC&g=w")["kind"] == "other"


def test_hp_url_facts_skips_paged_views():
    assert arch.hp_url_facts("http://finance.yahoo.com/q/hp?s=AMTD+Historical+Prices") == \
        {"ticker": "AMTD", "usable": True, "start": None, "end": None}
    assert not arch.hp_url_facts("http://finance.yahoo.com/q/hp?s=AMTD&z=66&y=66")["usable"]


def test_capture_span_by_kind():
    lo, hi = arch.capture_span("hp", "20130315000000")
    assert hi == pd.Timestamp("2013-03-15") and (hi - lo).days == arch.HP_DAYS
    lo, hi = arch.capture_span("csv", "20140101000000", "2012-01-02", "2013-06-01")
    assert (lo, hi) == (pd.Timestamp("2012-01-02"), pd.Timestamp("2013-06-01"))


def test_choose_captures_is_a_greedy_cover_with_a_minimum_gain():
    need = _sessions("2013-01-01", "2013-03-31")
    caps = [("20130201", "a", "hp", pd.Timestamp("2012-11-01"), pd.Timestamp("2013-02-01")),
            ("20130331", "b", "hp", pd.Timestamp("2012-12-26"), pd.Timestamp("2013-03-31")),
            ("20130105", "c", "hp", pd.Timestamp("2013-01-01"), pd.Timestamp("2013-01-03"))]
    chosen = arch.choose_captures(need, caps)
    assert [c[1] for c in chosen] == ["b"]  # b covers the whole quarter; c would add < 5 sessions


def test_split_ratio_reads_every_layout():
    assert arch.split_ratio("2: 1 Stock Split") == 2.0
    assert arch.split_ratio("SPLIT:1:16") == 1 / 16
    assert arch.split_ratio("1:16 Stock Splits") == 1 / 16
    assert arch.split_ratio("Dividend") is None


def _capture(closes, adjs, dates, volume=None):
    return pd.DataFrame({"date": pd.to_datetime(dates), "close": closes, "adj": adjs,
                         "volume": volume if volume is not None else [100.0] * len(closes)})


def test_raw_from_capture_detects_an_adjusted_close():
    # a 2:1 split on day 3; the page shows split-adjusted closes (no jump), Adj Close agrees
    rows = _capture([50.0, 51.0, 51.5, 52.0], [50.0, 51.0, 51.5, 52.0], ["2013-01-02", "2013-01-03", "2013-01-04", "2013-01-07"],
                    volume=[200.0, 200.0, 100.0, 100.0])
    rec = arch.raw_from_capture(rows, [(pd.Timestamp("2013-01-04"), "2: 1 Stock Split")])
    assert rec["mode"].iloc[0] == "adjusted"
    assert list(rec["close_raw"]) == [100.0, 102.0, 51.5, 52.0]
    assert list(rec["volume_raw"]) == [100.0, 100.0, 100.0, 100.0]
    assert list(rec["split"]) == [1.0, 1.0, 2.0, 1.0]


def test_raw_from_capture_keeps_an_as_traded_close():
    rows = _capture([100.0, 102.0, 51.5, 52.0], [50.0, 51.0, 51.5, 52.0], ["2013-01-02", "2013-01-03", "2013-01-04", "2013-01-07"])
    rec = arch.raw_from_capture(rows, [(pd.Timestamp("2013-01-04"), "2: 1 Stock Split"), (pd.Timestamp("2013-01-07"), "0.25 Dividend")])
    assert rec["mode"].iloc[0] == "raw"
    assert list(rec["close_raw"]) == [100.0, 102.0, 51.5, 52.0]
    assert rec["div"].iloc[-1] == 0.25


def test_post_delisting_sessions_are_counted():
    s = _sessions("2013-01-01", "2013-01-31")
    assert arch.post_delist_count(pd.Series(s), pd.Timestamp("2013-01-24"), s) == 5
    assert arch.post_delist_count(pd.Series(s), None, s) == 0


# ------------------------------------------------------------------ fixes


def test_fix_keeps_the_rows_own_prior_close():
    prior = fill.implied_prior_close(close=10.0, split=1.0, div=0.0, tr=-0.65)
    assert abs(prior - 10.0 / 0.35) < 1e-9
    # 2 child shares at 9.5 each per parent share
    tr = fill.fixed_return(10.0, 1.0, 2 * 9.5, prior)
    assert abs(tr - ((10.0 + 19.0) / prior - 1)) < 1e-12
    assert np.isnan(fill.fixed_return(10.0, 1.0, 0.0, np.nan))


def test_children_spec_with_aliases():
    assert fill.parse_children("LSXMA:1;BATRA:0.1") == [(["LSXMA"], 1.0), (["BATRA"], 0.1)]
    assert fill.parse_children("LMCK|FWONK:2") == [(["LMCK", "FWONK"], 2.0)]


def test_committed_fix_table_has_sec_urls_and_no_levels():
    t = pd.read_csv(ROOT / "output/research_only/reversal_2012_2026/inputs_v2/v2_fixes.csv", dtype=str,
                    keep_default_na=False)
    assert list(t.columns) == fill.FIX_COLUMNS
    assert len(t) == 12 and t["fix_id"].is_unique
    assert t["sec_url"].str.startswith("https://www.sec.gov/").all()
    # only share ratios and the documented cash; no price level column
    assert not any(c in t.columns for c in ("close", "close_raw", "price", "prior_close_raw"))
    for spec in t.loc[t["kind"] == "distribution_value", "children"]:
        assert fill.parse_children(spec)


# ------------------------------------------------------------------ fills


def test_complete_weeks_need_every_session():
    s = _sessions("2013-01-07", "2013-01-18")  # two whole weeks
    weeks = fill.complete_weeks(s.delete(7), s)
    assert len(weeks) == 1 and list(weeks)[0].end_time.date().isoformat() == "2013-01-11"


def _archive_rows(dates, closes, capture, kind="hp", divs=None):
    n = len(dates)
    return pd.DataFrame({"date": pd.to_datetime(dates), "kind": kind, "capture": capture, "otc": False,
                         "close": closes, "adj": closes, "volume": [1000.0] * n, "close_raw": closes,
                         "volume_raw": [1000.0] * n, "split": [1.0] * n, "div": divs or [0.0] * n, "mode": "none"})


def test_capture_record_prefers_csv_then_the_latest_capture_and_keeps_returns_inside_a_capture():
    s = _sessions("2013-01-02", "2013-01-10")
    old = _archive_rows(s[:4], [10.0, 10.1, 10.2, 10.3], "20130105")
    new = _archive_rows(s[2:6], [20.4, 20.6, 20.8, 21.0], "20130110")  # another level: a later capture
    rec = fill.capture_record(pd.concat([old, new]), s).set_index("date")
    assert rec.at[s[2], "capture"] == "20130110" and rec.at[s[2], "close_raw"] == 20.4
    # the later capture's first row has no previous row of its own: the return comes from the older capture
    assert abs(rec.at[s[2], "tr"] - (10.2 / 10.1 - 1)) < 1e-12
    assert abs(rec.at[s[3], "tr"] - (20.6 / 20.4 - 1)) < 1e-12
    csv = _archive_rows(s[1:3], [10.1, 10.2], "20120101", kind="csv")
    rec2 = fill.capture_record(pd.concat([new, csv]), s).set_index("date")
    assert rec2.at[s[2], "kind"] == "csv"


def test_fill_adds_only_whole_weeks_and_counts_a_second_source():
    s = _sessions("2013-01-07", "2013-01-25")  # three whole weeks
    closes = list(np.linspace(10, 11, len(s)))
    archive = _archive_rows(s, closes, "20130201")
    archive = archive[archive["date"] != s[12]]  # a hole in the third week
    qq = pd.Series({d: c / p - 1 for d, c, p in zip(s[1:], closes[1:], closes[:-1])})
    out, facts, ro = fill.fill_security("X", None, archive, {"quantquote": qq}, s, s, None)
    assert facts["rows_added"] == 10  # weeks 1-2 only
    assert set(out["src_primary"]) == {"archive"}
    assert (out["n_sources"].iloc[1:] == 2).all() and pd.isna(out["tr"].iloc[0])
    # the third week's sessions only QuantQuote covers are return-only days, never panel rows
    assert facts["return_only_days"] == 4 and set(ro["source"]) == {"quantquote"}


def test_fill_flags_an_unresolved_two_source_disagreement():
    s = _sessions("2013-01-07", "2013-01-11")
    closes = [10.0, 10.1, 10.2, 10.3, 10.4]
    archive = _archive_rows(s, closes, "20130201")
    qq = pd.Series({s[2]: 0.05})  # QuantQuote says +5% on day 3
    out, _, _ = fill.fill_security("X", None, archive, {"quantquote": qq}, s, s, None)
    row = out.set_index("date").loc[str(s[2].date())]
    assert "disagree_unresolved" in row["flags"] and row["n_sources"] == 2


def test_third_vote_settles_wiki_against_tiingo_but_never_counts_archive_against_yahoo():
    s = _sessions("2013-01-07", "2013-01-11")
    day = s[2]
    canon = pd.DataFrame({"date": [str(d.date()) for d in s], "close_raw": 10.0, "volume_raw": 1.0, "split_factor": 1.0,
                          "div_cash": 0.0, "tr": 0.0, "src_primary": "wiki", "n_sources": 2, "max_src_diff": 0.0,
                          "flags": ["", "", "disagree_unresolved", "", ""]})

    def frame(c2):
        return pd.DataFrame({"date": s, "close": [10.0, 10.0, c2, 10.0, 10.0], "volume": 1.0, "split": 1.0, "div": 0.0})

    archive = _archive_rows(s, [10.0, 10.0, 10.5, 10.0, 10.0], "20130201")
    out, rows = fill.third_votes(canon, {"wiki": frame(10.0), "tiingo": frame(10.5)}, archive, None, s)
    r = out.set_index("date").loc[str(day.date())]
    assert r["src_primary"] == "tiingo" and abs(r["tr"] - 0.05) < 1e-12
    assert "v2_third_vote:archive>tiingo" in r["flags"] and "disagree_unresolved" not in r["flags"]
    # wiki against live Yahoo: archived Yahoo agreeing with Yahoo is the same source, not a vote
    out2, rows2 = fill.third_votes(canon, {"wiki": frame(10.0), "yahoo": frame(10.5)}, archive, None, s)
    assert "disagree_unresolved" in out2.set_index("date").loc[str(day.date()), "flags"]
    assert rows2[0]["resolved"] == ""


def test_rounding_allowance_only_matters_below_a_dollar():
    assert fill.rounding_allowance(50.0, 50.0) < 0.0003
    assert fill.rounding_allowance(0.5, 0.5) == 0.02
    assert fill.agree(0.010, 0.020, fill.rounding_allowance(0.5, 0.5))
    assert not fill.agree(0.010, 0.020)


def test_terminal_attaches_d5_evidence_without_changing_status(tmp_path, monkeypatch):
    from scripts import reversal_data_terminal as term
    ev = tmp_path / "v2_d5_sec_evidence.csv"
    ev.write_text("ticker,security_id,cik,delist_date,decision,accession,doc_url,doc_date,statement,otc_ticker,notes\n"
                  "GTAT,1394954,1394954,2014-12-21,zero_recovery,0001628280-16-012680,https://www.sec.gov/x,2016-03-14,"
                  "s,GTATQ,\n")
    monkeypatch.setattr(term, "D5_EVIDENCE", ev)
    frame = pd.DataFrame({"security_id": ["1394954", "1"], "status": ["awaiting_d5", "computed"]})
    out = term.attach_d5_evidence(frame)
    assert list(out["status"]) == ["awaiting_d5", "computed"]
    assert list(out["sec_equity_decision"]) == ["zero_recovery", ""]


def test_archive_rows_give_otc_rows_only_after_the_last_nasdaq_row(tmp_path, monkeypatch):
    from scripts import reversal_data_terminal as term
    d = tmp_path / "series" / "archive"
    d.mkdir(parents=True)
    pd.DataFrame({"date": ["2014-10-14", "2014-10-15", "2014-10-14", "2014-10-16"], "capture": ["1", "1", "2", "2"],
                  "otc": [False, False, True, True], "close_raw": [1.0, 1.1, 0.9, 0.5],
                  "volume_raw": [10.0, 10.0, 5.0, 5.0]}).to_csv(d / "X.csv.gz", index=False)
    monkeypatch.setattr(term.common, "V2_FILL", tmp_path)
    rows = term.archive_rows("X", [])
    src = pd.concat(rows)
    assert set(src.loc[src["src"] == "archive_otc", "date"].dt.strftime("%Y-%m-%d")) == {"2014-10-16"}


def test_post_tables_closes_the_fixed_queue_rows_and_marks_the_split_row(tmp_path):
    inputs, out, prices = tmp_path / "inputs", tmp_path / "reconcile", tmp_path / "prices"
    for d in (inputs, out, prices):
        d.mkdir()
    pd.DataFrame([{c: "" for c in fill.FIX_COLUMNS} | {
        "fix_id": "F06", "v1_item_id": "moves-02-035", "security_id": "1142701", "ticker": "UNTD", "ex_date": "2013-11-01",
        "kind": "distribution_value", "new_shares_per_old": "0.142857142857", "children": "FTD:0.2",
        "sec_url": "https://www.sec.gov/x", "terms": "1 FTD per 5"}]).to_csv(inputs / "v2_fixes.csv", index=False)
    pd.DataFrame([{"fix_id": "F06", "security_id": "1142701", "ticker": "UNTD", "ex_date": "2013-11-01", "applied": "Y",
                   "split_after": 1 / 7, "div_after": 6.0}]).to_csv(out / "v2_fixes_applied.csv", index=False)
    pd.DataFrame({"date": ["2013-11-01"], "close_raw": [10.0]}).to_csv(prices / "1142701.csv", index=False)
    se = pd.DataFrame([{"security_id": "1142701", "ticker": "UNTD", "ex_date": "2013-11-01", "split_factor": "0.1428571429",
                        "event_type": "reverse_split", "sec_url": "", "verified_at": "", "notes": "hand review"}])
    rm = pd.DataFrame([{"ticker": "UNTD", "event_date": "2013-11-01", "classification": "unreviewed", "source_url": "",
                        "verified_at": "", "notes": "[R1] move | hand review moves-02-035: open", "security_id": "1142701",
                        "sources_agreeing": ""}])
    se.to_csv(inputs / "split_events.csv", index=False)
    rm.to_csv(inputs / "reviewed_moves.csv", index=False)
    facts = fill.post_tables(inputs / "split_events.csv", inputs / "reviewed_moves.csv", out, inputs, prices)
    assert facts["queue_rows_closed_by_fix"] == 1 and facts["split_rows_updated"] == 1
    rm2 = pd.read_csv(inputs / "reviewed_moves.csv", dtype=str)
    assert rm2.loc[0, "classification"] == "event_confirmed" and rm2.loc[0, "source_url"] == "https://www.sec.gov/x"
    se2 = pd.read_csv(inputs / "split_events.csv", dtype=str)
    assert se2.loc[0, "event_type"] == "spinoff" and abs(float(se2.loc[0, "split_factor"]) - (10 / 7 + 6) / 10) < 1e-5
