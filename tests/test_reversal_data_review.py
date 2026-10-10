"""Tests for the hand-review merge (scripts/reversal_data_review.py). Synthetic verdict files only: no cache."""
import json

import pandas as pd
import pytest

from pipelines.reversal_data import reconcile as rc
from pipelines.reversal_data import review as rv

STAMP = "2026-10-03T00:00:00Z"
MOVE_COLS = ["item_id", "security_id", "ticker", "event_date", "rule", "classification", "source_url", "evidence_sources",
             "correct_source", "event_type", "split_factor", "cash_per_share", "first_new_session", "effective_date",
             "verified_at", "reviewer", "notes"]
SPLIT_COLS = ["item_id", "security_id", "ticker", "ex_date", "verdict", "split_factor", "ex_date_confirmed", "event_type",
              "source_url", "evidence_sources", "verified_at", "reviewer", "notes"]
DIST_COLS = ["item_id", "security_id", "ticker", "ex_date", "verdict", "distribution_type", "ratio", "cash_per_share",
             "distributed_security", "ex_date_confirmed", "source_url", "verified_at", "reviewer", "notes"]
TERM_COLS = ["item_id", "security_id", "ticker", "verdict", "terminal_type", "event_subtype", "cash", "shares",
             "acquirer_security_id", "acquirer_name", "last_trading_day", "special_dividend", "special_dividend_record",
             "fixed_value", "value_rule", "url", "approved", "hold", "hold_last_session", "note", "verified_at", "reviewer"]
SEC = "https://www.sec.gov/Archives/edgar/data/1/a.htm"


def _queue(root, queue, columns, batch, verdicts, mechanical=None):
    folder = root / queue
    folder.mkdir(parents=True)
    pd.DataFrame(columns=columns).to_csv(folder / "verdict_template.csv", index=False)
    pd.DataFrame(batch, columns=["item_id", "security_id", "ticker", "event_date", "rule", "end_date", "ex_date"]) \
        .to_csv(folder / "batch_01.csv", index=False)
    pd.DataFrame(verdicts, columns=columns).fillna("").to_csv(folder / "verdict_01.csv", index=False)
    if mechanical is not None:
        pd.DataFrame(mechanical, columns=columns).fillna("").to_csv(folder / "mechanical_verdicts.csv", index=False)


def _move(item, day, rule, cls, **extra):
    return {"item_id": item, "security_id": "1", "ticker": "T", "event_date": day, "rule": rule, "classification": cls,
            "verified_at": STAMP, "reviewer": "hand:test", "notes": "checked", **extra}


def _round(tmp_path, moves=None, mechanical=None, splits=None, dists=None, terminal=None, price=None):
    root = tmp_path / "round10"
    move_batch = [{"item_id": m["item_id"], "security_id": "1", "ticker": "T", "event_date": m["event_date"],
                   "rule": m["rule"], "end_date": m.get("_end", m["event_date"])} for m in (moves or [])
                  if not m["item_id"].startswith("x")]
    _queue(root, "moves", MOVE_COLS, move_batch, [{k: v for k, v in m.items() if k != "_end"} for m in (moves or [])],
           mechanical or [])
    _queue(root, "splits", SPLIT_COLS, [{"item_id": s["item_id"], "security_id": s["security_id"], "ticker": s["ticker"],
                                         "ex_date": s["ex_date"]} for s in (splits or [])], splits or [], [])
    _queue(root, "distributions", DIST_COLS, [{"item_id": d["item_id"], "security_id": d["security_id"],
                                               "ticker": d["ticker"], "ex_date": d["ex_date"]} for d in (dists or [])],
           dists or [])
    _queue(root, "terminal", TERM_COLS, [{"item_id": t["item_id"], "security_id": t["security_id"], "ticker": t["ticker"]}
                                         for t in (terminal or [])], terminal or [])
    _queue(root, "terminal_price", TERM_COLS, [{"item_id": t["item_id"], "security_id": t["security_id"],
                                                "ticker": t["ticker"]} for t in (price or [])], price or [])
    return root


def test_the_note_pattern_is_the_reconcile_steps():
    assert rv.LEVEL_IN_NOTE.pattern == rc.LEVEL_IN_NOTE.pattern and rv.LEVEL_IN_NOTE.flags == rc.LEVEL_IN_NOTE.flags


def test_merge_accepts_good_rows_and_rejects_bad_ones_with_the_reason(tmp_path):
    moves = [
        _move("moves-01-001", "2015-02-02", "R1b", "market_move_no_adjustment", source_url=SEC),
        _move("moves-01-002", "2015-02-03", "R1b", "market_move_no_adjustment"),  # no source_url
        _move("moves-01-003", "2015-02-04", "R3", "vendor_error", evidence_sources="wiki+tiingo", correct_source="tiingo",
              notes="tiingo close 12.34 is right"),  # a price level in the note
        _move("moves-01-004", "2015-02-05", "R4", "halt", source_url=SEC, verified_at="yesterday"),
        _move("moves-01-005", "2015-02-06", "R3", "vendor_error", evidence_sources="tiingo+yahoo", correct_source="tiingo"),
        _move("moves-01-006", "2015-02-06", "R7", "unresolved", _end="2015-02-12"),  # same day, another class
        _move("moves-01-007", "2015-02-09", "R4", "flat_genuine", evidence_sources="yahoo+stored"),
        _move("x-not-in-batch", "2015-02-10", "R4", "unresolved"),
        _move("moves-01-008", "2015-02-11", "R1", "made_up_class"),
        _move("moves-01-009", "2015-02-12", "R1/R2", "unrecorded_event", source_url=SEC, event_type="reverse_split",
              split_factor="0.04"),
    ]
    mechanical = [_move("moves-mech-001", "2015-03-02", "R4", "vendor_error", evidence_sources="stored")]
    root = _round(tmp_path, moves=moves, mechanical=mechanical)
    summary = rv.merge(root, existing_terminal={}, inputs=None)
    rejected = pd.read_csv(root / "merged" / "rejected.csv").set_index("item_id")["reason"]
    assert "needs a source_url" in rejected["moves-01-002"]
    assert "LEVEL_IN_NOTE" in rejected["moves-01-003"]
    assert "not a UTC timestamp" in rejected["moves-01-004"]
    assert "disagrees" in rejected["moves-01-005"] and "disagrees" in rejected["moves-01-006"]
    assert "not in any batch" in rejected["x-not-in-batch"]
    assert "not in the schema" in rejected["moves-01-008"]
    assert "two evidence_sources" in rejected["moves-mech-001"]  # one source does not outvote a vendor
    merged = pd.read_csv(root / "merged" / "moves_verdicts.csv", dtype=str, keep_default_na=False)
    assert sorted(merged["item_id"]) == ["moves-01-001", "moves-01-007", "moves-01-009"]
    changes = pd.read_csv(root / "merged" / "moves_data_changes.csv", dtype=str, keep_default_na=False)
    assert changes["item_id"].tolist() == ["moves-01-009"] and changes["applied"].tolist() == ["N"]
    assert summary["rejected"]["rows"] == 8
    assert json.loads((root / "merged" / "merge_summary.json").read_text())["merged_rows"]["moves_verdicts.csv"] == 3


def test_a_key_that_does_not_match_its_batch_or_the_build_queue_is_rejected(tmp_path):
    moves = [_move("moves-01-001", "2015-02-02", "R1b", "market_move_no_adjustment", source_url=SEC),
             _move("moves-01-002", "2015-02-03", "R1b", "market_move_no_adjustment", source_url=SEC)]
    root = _round(tmp_path, moves=moves)
    batch = pd.read_csv(root / "moves" / "batch_01.csv", dtype=str)
    batch.loc[0, "event_date"] = "2015-01-30"
    batch.to_csv(root / "moves" / "batch_01.csv", index=False)
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    pd.DataFrame([{"ticker": "T", "event_date": "2015-02-04", "classification": "unreviewed", "source_url": "",
                   "verified_at": "", "notes": "[R1b] move", "security_id": "1", "sources_agreeing": ""}]) \
        .to_csv(inputs / "reviewed_moves.csv", index=False)
    rv.merge(root, existing_terminal={}, inputs=inputs)
    rejected = pd.read_csv(root / "merged" / "rejected.csv").set_index("item_id")["reason"]
    assert "does not match the batch" in rejected["moves-01-001"]
    assert "not in the build's queue table" in rejected["moves-01-002"]


def test_split_and_distribution_checks_and_their_data_changes(tmp_path):
    splits = [{"item_id": "s1", "security_id": "1", "ticker": "T", "ex_date": "2015-02-02", "verdict": "confirmed",
               "source_url": SEC, "event_type": "reverse_split", "verified_at": STAMP, "reviewer": "h", "notes": ""},
              {"item_id": "s2", "security_id": "1", "ticker": "T", "ex_date": "2015-03-02", "verdict": "corrected",
               "source_url": SEC, "split_factor": "0.1", "verified_at": STAMP, "reviewer": "h", "notes": "1-for-10"},
              {"item_id": "s3", "security_id": "1", "ticker": "T", "ex_date": "2015-04-01", "verdict": "confirmed",
               "evidence_sources": "yahoo", "verified_at": STAMP, "reviewer": "h", "notes": ""}]
    dists = [{"item_id": "d1", "security_id": "2", "ticker": "U", "ex_date": "2015-02-10", "verdict": "not_a_distribution",
              "source_url": SEC, "verified_at": STAMP, "reviewer": "h", "notes": "a 1-for-200 reverse split"},
             {"item_id": "d2", "security_id": "2", "ticker": "U", "ex_date": "2015-03-10", "verdict": "confirmed",
              "distribution_type": "spinoff", "verified_at": STAMP, "reviewer": "h", "notes": ""}]
    root = _round(tmp_path, splits=splits, dists=dists)
    rv.merge(root, existing_terminal={}, inputs=None)
    rejected = pd.read_csv(root / "merged" / "rejected.csv").set_index("item_id")["reason"]
    assert set(rejected.index) == {"s3", "d2"}
    changes = pd.read_csv(root / "merged" / "split_data_changes.csv", dtype=str, keep_default_na=False)
    assert changes.set_index("item_id")["change"].to_dict() == {"s2": "factor 0.1", "d1": "not a distribution"}
    assert (changes["applied"] == "N").all()


def _term(item, sid, verdict, **extra):
    return {"item_id": item, "security_id": sid, "ticker": "T", "verdict": verdict, "verified_at": STAMP,
            "reviewer": "hand", **extra}


def test_terminal_entries_restate_the_terms_and_keep_the_guard_for_price_gaps():
    code = {"10": {"type": "mixed", "sub": "election", "cash": 3.0, "shares": 1.0, "acq": "891103",
                   "note": "code note", "hold": "old hold"},
            "11": {"type": "mixed", "cash": 14.0, "shares": 0.6549, "acq": "", "acq_name": "Cott (NYSE: COT)",
                   "rule": "election"},
            "12": {"type": "cash_merger", "cash": 2.0}}
    frame = pd.DataFrame([
        _term("t-1", "10", "correct", terminal_type="stock_merger", event_subtype="election", shares="1.0337",
              acquirer_security_id="891103", acquirer_name="Match Group", last_trading_day="2020-06-30", url=SEC,
              approved="non-electing shares got 1.0337 New Match"),
        _term("t-2", "11", "price_gap", terminal_type="mixed", cash="5.04", shares="0.6549",
              acquirer_name="Cott Corporation (NYSE: COT; renamed Primo)", url=SEC, approved="terms checked",
              note="needs COT 2020-03-02"),
        _term("t-3", "12", "hold", terminal_type="cash_merger", cash="2.00", hold="the halt day traded",
              hold_last_session="Y", url=SEC),
        _term("t-4", "13", "unresolved", note="nothing found"),
        _term("t-5", "14", "approve", terminal_type="bankruptcy_otc", event_subtype="bankruptcy",
              last_trading_day="2023-08-16", url=SEC, approved="8-K Item 3.01")]).reindex(columns=TERM_COLS).fillna("")
    entries = rv.terminal_entries(frame, code)
    mtch = entries["10"]
    assert mtch["type"] == "stock_merger" and mtch["shares"] == pytest.approx(1.0337) and "cash" not in mtch
    assert mtch["limit"] == "2020-06-30" and mtch["approved"].startswith("t-1: ") and mtch["url"] == SEC
    assert "hold" not in mtch and mtch["note"] == "code note" and mtch["review"] == "hand review t-1 (correct)"
    prmw = entries["11"]
    assert prmw["cash"] == pytest.approx(5.04) and "rule" not in prmw  # the code's 'election' rule is restated away
    assert prmw["acq_name"] == "Cott (NYSE: COT)"  # no acquirer id in the verdict: the code's name stays
    assert "approved" not in prmw and "price pending" in prmw["review"]  # the guard still applies once priced
    hold = entries["12"]
    assert hold["hold"] == "the halt day traded" and hold["hold_last_session"] is True and "approved" not in hold
    assert "13" not in entries
    assert entries["14"]["type"] == "bankruptcy_otc" and entries["14"]["approved"] == "t-5: 8-K Item 3.01"
    assert code["10"]["cash"] == 3.0  # the code's entries are not changed in place


def test_source_overrides_cover_an_r7_run_to_its_end_date():
    frame = pd.DataFrame([{"security_id": "1", "event_date": "2015-08-26", "end_date": "2015-09-14", "rule": "R7",
                           "classification": "vendor_error", "correct_source": "tiingo", "item_id": "m1"},
                          {"security_id": "1", "event_date": "2015-08-18", "end_date": "", "rule": "R3",
                           "classification": "vendor_error", "correct_source": "", "item_id": "m2"},
                          {"security_id": "2", "event_date": "2015-08-18", "end_date": "2015-08-18", "rule": "R3",
                           "classification": "stored_error", "correct_source": "tiingo", "item_id": "m3"}])
    assert rv.source_overrides(frame) == {"1": [{"start": "2015-08-26", "end": "2015-09-14", "source": "tiingo",
                                                 "item_id": "m1"}]}
    assert [e["item_id"] for e in rv.reviewed_days(frame)["1"]] == ["m1", "m2"]


def test_move_note_names_the_verdict_and_its_basis_without_the_full_text():
    note = rv.move_note({"item_id": "moves-09-001", "reviewer": "hand", "classification": "vendor_error",
                         "correct_source": "wiki", "source_url": "", "evidence_sources": "wiki+stored"})
    assert note == "hand review moves-09-001 (hand): vendor_error; use wiki; evidence: wiki+stored"
