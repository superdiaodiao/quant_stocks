"""Hand-review verdicts for the reversal 2012-2026 data build (docs/reversal_2012_2026_data_plan.md 4.3-4.5, 6).

Data only: this step reads the reviewers' verdict files and writes the merged inputs the build reads; it
computes no return and makes no request.

The hand review of round 10 wrote one verdict file per batch under ``CACHE/review/round10/<queue>/``
(``verdict_NN.csv``, plus ``mechanical_verdicts.csv`` for the rows settled by the plan's own rules), keyed by
``item_id`` with the batch's key columns repeated. ``merge`` checks every row and writes
``CACHE/review/round10/merged/``:

- ``moves_verdicts.csv``: the moves queue (``INPUTS/reviewed_moves.csv``), keyed (security_id, event_date, rule).
  ``reversal_data_reconcile.build_move_queue`` copies classification, source_url (or ``evidence: <sources>``),
  verified_at and a short note into the queue; an ``unresolved`` verdict leaves the row ``unreviewed`` (open)
  with the reason in its notes. ``vendor_error`` rows with a ``correct_source`` are read by the reconcile step as
  per-day source choices (``source_overrides``): on that day (an R7 run: every listed session from event_date to
  end_date) the canonical row comes from that vendor, and a ``disagree_unresolved`` day the verdict covers is
  ``disagree_reviewed`` instead.
- ``split_verdicts.csv`` and ``distribution_verdicts.csv``: keyed (security_id, ex_date); the reconcile step's
  ``build_split_table`` fills sec_url (``source_url``, or ``evidence: <sources>`` for a two-source majority) and
  verified_at of ``split_events.csv``, ``build_special_table`` the sec_url of ``special_distributions.csv``.
  ``unresolved`` verdicts fill nothing.
- ``terminal_verdicts.csv``: keyed security_id (the terminal and terminal_price queues); the terminal step merges
  each row into its ``REVIEWED`` entry (``terminal_entries``: blank fields keep the code's value; ``price_gap``
  rows add their terms and limit but no ``approved``, so the value guard still applies once a price arrives).
- ``moves_data_changes.csv`` and ``split_data_changes.csv``: every verdict that changes the canonical series
  (a source choice, an unrecorded event, a corrected or rejected split or distribution), with ``applied`` =
  whether the build acts on it (source choices: Y; S/D changes: N, for the owner).
- ``rejected.csv``: rows that failed a check (unknown item or key, a value outside the schema, missing
  evidence, a price level in a note, two different verdicts for one build key), with the reason; a rejected row
  is not merged, so its queue item stays open.
- ``terminal_reviewed_entries.py``: the REVIEWED entries the terminal step builds from the verdicts, beside the
  code's existing entry (for the owner's diff).
- ``merge_summary.json``: counts per queue and verdict, rejections, warnings (a source_url that is not an SEC,
  exchange or FINRA page; a '$' in a note).

Usage::

    PYTHONPATH=. python scripts/reversal_data_review.py            # merge round 10 into CACHE/review/round10/merged
    PYTHONPATH=. python scripts/reversal_data_review.py --out-dir /tmp/merged
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys

import pandas as pd

from scripts import reversal_data_common as common

ROUND_DIR = common.CACHE / "review" / "round10"
MERGED_DIR = ROUND_DIR / "merged"
# the reconcile step's note pattern (reversal_data_reconcile.LEVEL_IN_NOTE; a test keeps the two equal)
LEVEL_IN_NOTE = re.compile(r"\bclose[sd]?\s+\$?\d+(?:\.\d+)?(?![\d.]*x)", re.I)
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?Z$")
SOURCES = ("wiki", "tiingo", "yahoo", "stored")
VENDORS = ("wiki", "tiingo", "yahoo")
PRIMARY_HOSTS = ("sec.gov", "nasdaqtrader.com", "nasdaq.com", "finra.org")

MOVE_CLASSES = ("market_move_no_adjustment", "market_move_second_source", "vendor_error", "stored_error",
                "unrecorded_event", "event_confirmed", "halt", "flat_genuine", "vendor_gap_unfilled",
                "relist_junction", "unresolved")
MOVE_EVENT_TYPES = ("", "split", "reverse_split", "spinoff", "stock_dividend", "special_cash")
SPLIT_VERDICTS = ("confirmed", "corrected", "not_a_split", "reclassify_distribution", "unresolved")
SPLIT_EVENT_TYPES = ("", "split", "reverse_split", "distribution")
DIST_VERDICTS = ("confirmed", "corrected", "not_a_distribution", "unresolved")
DIST_TYPES = ("", "spinoff", "stock_dividend", "special_cash", "rights", "return_of_capital", "liquidating",
              "merger_consideration", "other")
TERMINAL_VERDICTS = ("approve", "correct", "decide_type", "hold", "price_gap", "unresolved")
TERMINAL_TYPES = ("", "cash_merger", "stock_merger", "mixed", "liquidation", "exchange_move", "bankruptcy_otc", "unknown")
VALUE_RULES = ("", "election", "plan_new_shares")

QUEUES = {
    "moves": {"key_check": ["security_id", "ticker", "event_date", "rule"], "verdict": "classification",
              "note": "notes", "mechanical": True},
    "splits": {"key_check": ["security_id", "ticker", "ex_date"], "verdict": "verdict", "note": "notes",
               "mechanical": True},
    "distributions": {"key_check": ["security_id", "ticker", "ex_date"], "verdict": "verdict", "note": "notes",
                      "mechanical": False},
    "terminal": {"key_check": ["security_id", "ticker"], "verdict": "verdict", "note": "note", "mechanical": False},
    "terminal_price": {"key_check": ["security_id", "ticker"], "verdict": "verdict", "note": "note",
                       "mechanical": False},
}
# the text fields that end up in a committed table or in the code (checked against LEVEL_IN_NOTE)
TEXT_FIELDS = {"moves": ["notes"], "splits": ["notes"], "distributions": ["notes", "distributed_security"],
               "terminal": ["note", "approved", "hold", "acquirer_name"],
               "terminal_price": ["note", "approved", "hold", "acquirer_name"]}

# terminal verdict columns -> REVIEWED keys (reversal_data_terminal.REVIEW_KEYS)
TERMINAL_MAP = {"terminal_type": "type", "event_subtype": "sub", "cash": "cash", "shares": "shares",
                "acquirer_security_id": "acq", "acquirer_name": "acq_name", "last_trading_day": "limit",
                "special_dividend": "special_dividend", "special_dividend_record": "special_dividend_record",
                "fixed_value": "value", "value_rule": "rule", "url": "url", "approved": "approved", "hold": "hold",
                "hold_last_session": "hold_last_session", "note": "note"}
TERMINAL_FLOATS = ("cash", "shares", "value", "special_dividend")
# the consideration keys a verdict with a terminal_type restates as a whole (a blank one is then absent, not kept):
# a reviewer's terms replace the code's (MTCH: stock_merger 1.0337, the code's 3.00 cash dropped; PRMW: the mixed
# consideration, the code's rule 'election' dropped)
TERMINAL_TERMS = ("type", "sub", "cash", "shares", "value", "rule", "stock_value")
# keys of an existing REVIEWED entry to remove whatever the verdict says (none needed for round 10)
TERMINAL_DROP_KEYS: dict[str, tuple[str, ...]] = {}


def _host(url: str) -> str:
    m = re.match(r"^https?://([^/]+)", url.strip())
    return m.group(1).lower() if m else ""


def is_url(value: str) -> bool:
    return bool(_host(value))


def primary_host(url: str) -> bool:
    host = _host(url)
    return any(host == h or host.endswith("." + h) for h in PRIMARY_HOSTS)


def sources_of(value: str) -> list[str]:
    return [s for s in str(value).split("+") if s]


def _number(value: str) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def verdict_files(root: Path, queue: str) -> list[Path]:
    files = sorted((root / queue).glob("verdict_[0-9]*.csv"))
    if QUEUES[queue]["mechanical"] and (root / queue / "mechanical_verdicts.csv").exists():
        files.append(root / queue / "mechanical_verdicts.csv")
    return files


def batch_rows(root: Path, queue: str) -> pd.DataFrame:
    files = sorted((root / queue).glob("batch_*.csv"))
    frames = [read_csv(f).assign(_batch=f.name) for f in files if f.stat().st_size]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


# --------------------------------------------------------------------------------------------- row checks

def check_move(row: dict) -> list[str]:
    c = row["classification"]
    bad = []
    url, ev = row["source_url"].strip(), sources_of(row["evidence_sources"])
    if c not in MOVE_CLASSES:
        return [f"classification {c!r} not in the schema"]
    if [s for s in ev if s not in SOURCES]:
        bad.append(f"evidence_sources {row['evidence_sources']!r} outside {'+'.join(SOURCES)}")
    if row["correct_source"] not in ("",) + VENDORS:
        bad.append(f"correct_source {row['correct_source']!r} not in the schema")
    if row["event_type"] not in MOVE_EVENT_TYPES:
        bad.append(f"event_type {row['event_type']!r} not in the schema")
    if url and not is_url(url):
        bad.append("source_url is not a URL")
    for col in ("split_factor", "cash_per_share"):
        if row[col] and not _number(row[col]):
            bad.append(f"{col} {row[col]!r} is not a number")
    for col in ("first_new_session", "effective_date"):
        if row[col] and not DATE.match(row[col]):
            bad.append(f"{col} {row[col]!r} is not a date")
    two = len(set(ev)) >= 2
    if c == "market_move_no_adjustment" and not url:
        bad.append("market_move_no_adjustment needs a source_url")
    if c == "market_move_second_source" and not two:
        bad.append("market_move_second_source needs two evidence_sources")
    if c == "vendor_error" and not (url or two):
        bad.append("vendor_error needs a source_url or two evidence_sources")
    if c == "stored_error" and not (url or [s for s in ev if s != "stored"]):
        bad.append("stored_error needs a source_url or a vendor in evidence_sources")
    if c in ("unrecorded_event", "event_confirmed"):
        if not url or not row["event_type"] or not (row["split_factor"] or row["cash_per_share"]):
            bad.append(f"{c} needs source_url, event_type and split_factor or cash_per_share")
    if c == "halt" and not url:
        bad.append("halt needs a source_url")
    if c in ("flat_genuine", "vendor_gap_unfilled") and not (url or ev):
        bad.append(f"{c} needs evidence_sources or a source_url")
    if c == "relist_junction" and not (url and row["first_new_session"] and row["effective_date"]):
        bad.append("relist_junction needs source_url, first_new_session and effective_date")
    if c == "unresolved" and not row["notes"].strip():
        bad.append("unresolved needs notes saying what was checked")
    return bad


def check_split(row: dict) -> list[str]:
    v, bad = row["verdict"], []
    url, ev = row["source_url"].strip(), sources_of(row["evidence_sources"])
    if v not in SPLIT_VERDICTS:
        return [f"verdict {v!r} not in the schema"]
    if row["event_type"] not in SPLIT_EVENT_TYPES:
        bad.append(f"event_type {row['event_type']!r} not in the schema")
    if [s for s in ev if s not in SOURCES]:
        bad.append(f"evidence_sources {row['evidence_sources']!r} outside the schema")
    if url and not is_url(url):
        bad.append("source_url is not a URL")
    if row["split_factor"] and not _number(row["split_factor"]):
        bad.append("split_factor is not a number")
    if row["ex_date_confirmed"] and not DATE.match(row["ex_date_confirmed"]):
        bad.append("ex_date_confirmed is not a date")
    two = len(set(ev)) >= 2
    if v in ("confirmed", "not_a_split") and not (url or two):
        bad.append(f"{v} needs a source_url or two evidence_sources")
    if v == "corrected" and not (url and (row["split_factor"] or row["ex_date_confirmed"])):
        bad.append("corrected needs source_url and split_factor or ex_date_confirmed")
    if v == "reclassify_distribution" and not url:
        bad.append("reclassify_distribution needs a source_url")
    if v == "unresolved" and not row["notes"].strip():
        bad.append("unresolved needs notes")
    return bad


def check_distribution(row: dict) -> list[str]:
    v, bad = row["verdict"], []
    url = row["source_url"].strip()
    if v not in DIST_VERDICTS:
        return [f"verdict {v!r} not in the schema"]
    if row["distribution_type"] not in DIST_TYPES:
        bad.append(f"distribution_type {row['distribution_type']!r} not in the schema")
    if url and not is_url(url):
        bad.append("source_url is not a URL")
    for col in ("ratio", "cash_per_share"):
        if row[col] and not _number(row[col]):
            bad.append(f"{col} is not a number")
    if row["ex_date_confirmed"] and not DATE.match(row["ex_date_confirmed"]):
        bad.append("ex_date_confirmed is not a date")
    if v in ("confirmed", "corrected", "not_a_distribution") and not url:
        bad.append(f"{v} needs a source_url")
    if v == "unresolved" and not row["notes"].strip():
        bad.append("unresolved needs notes")
    return bad


def check_terminal(row: dict) -> list[str]:
    v, bad = row["verdict"], []
    url = row["url"].strip()
    if v not in TERMINAL_VERDICTS:
        return [f"verdict {v!r} not in the schema"]
    if row["terminal_type"] not in TERMINAL_TYPES:
        bad.append(f"terminal_type {row['terminal_type']!r} not in the schema")
    if row["value_rule"] not in VALUE_RULES:
        bad.append(f"value_rule {row['value_rule']!r} not in the schema")
    if row["hold_last_session"] not in ("", "Y"):
        bad.append(f"hold_last_session {row['hold_last_session']!r} not in the schema")
    if url and not is_url(url):
        bad.append("url is not a URL")
    for col in ("cash", "shares", "fixed_value", "special_dividend"):
        if row[col] and not _number(row[col]):
            bad.append(f"{col} {row[col]!r} is not a number")
    for col in ("last_trading_day", "special_dividend_record"):
        if row[col] and not DATE.match(row[col]):
            bad.append(f"{col} {row[col]!r} is not a date")
    if v in ("approve", "correct") and not (url and row["approved"].strip()):
        bad.append(f"{v} needs url and approved")
    if v == "decide_type" and not (url and row["terminal_type"] and row["terminal_type"] != "unknown"):
        bad.append("decide_type needs url and a terminal_type")
    if v == "hold" and not row["hold"].strip():
        bad.append("hold needs a hold reason")
    if v == "price_gap" and not (url and row["note"].strip()):
        bad.append("price_gap needs url and a note naming the session and symbol")
    if v == "unresolved" and not row["note"].strip():
        bad.append("unresolved needs a note")
    return bad


CHECKS = {"moves": check_move, "splits": check_split, "distributions": check_distribution,
          "terminal": check_terminal, "terminal_price": check_terminal}


def load_queue(root: Path, queue: str) -> tuple[pd.DataFrame, list[dict], dict]:
    """Every verdict row of one queue with the checks applied: (accepted rows, rejections, warnings)."""
    spec = QUEUES[queue]
    template = list(read_csv(root / queue / "verdict_template.csv").columns)
    batches = batch_rows(root, queue)
    by_item = {r["item_id"]: r for r in batches.to_dict("records")} if len(batches) else {}
    rows, rejected = [], []
    warnings = {"non_primary_url": [], "dollar_in_note": []}
    for path in verdict_files(root, queue):
        frame = read_csv(path)
        if list(frame.columns) != template:
            missing = [c for c in template if c not in frame.columns]
            if missing:
                rejected += [{"queue": queue, "file": path.name, "item_id": r.get("item_id", ""),
                              "reason": f"columns missing: {missing}"} for r in frame.to_dict("records")]
                continue
            frame = frame[template]
        for row in frame.to_dict("records"):
            row = {k: str(v).strip() if k not in TEXT_FIELDS[queue] else str(v) for k, v in row.items()}
            reasons = []
            mechanical = path.name == "mechanical_verdicts.csv"
            batch = by_item.get(row["item_id"])
            if batch is None and not mechanical:
                reasons.append("item_id not in any batch")
            elif batch is not None:
                for col in spec["key_check"]:
                    if col in batch and str(batch[col]) != row[col]:
                        reasons.append(f"{col} {row[col]!r} does not match the batch ({batch[col]!r})")
            if mechanical and not row["item_id"]:
                reasons.append("item_id blank")
            if not STAMP.match(row["verified_at"]):
                reasons.append(f"verified_at {row['verified_at']!r} is not a UTC timestamp")
            if not row["reviewer"]:
                reasons.append("reviewer blank")
            for col in TEXT_FIELDS[queue]:
                if LEVEL_IN_NOTE.search(row.get(col, "")):
                    reasons.append(f"{col} matches LEVEL_IN_NOTE")
            reasons += CHECKS[queue](row)
            url = row.get("source_url", row.get("url", ""))
            if url and not primary_host(url):
                warnings["non_primary_url"].append({"item_id": row["item_id"], "host": _host(url)})
            if "$" in row.get(spec["note"], ""):
                warnings["dollar_in_note"].append(row["item_id"])
            row["_file"] = path.name
            row["_end_date"] = (batch or {}).get("end_date", "") or row.get("event_date", "")
            if reasons:
                rejected.append({"queue": queue, "file": path.name, "item_id": row["item_id"],
                                 "reason": "; ".join(reasons)})
            else:
                rows.append(row)
    frame = pd.DataFrame(rows)
    if len(frame):
        dup = frame[frame.duplicated("item_id", keep=False)]
        for item, part in dup.groupby("item_id"):
            rejected += [{"queue": queue, "file": f, "item_id": item, "reason": "item_id given more than once"}
                         for f in part["_file"]]
        frame = frame[~frame["item_id"].isin(dup["item_id"])]
    missing = sorted(set(by_item) - set(frame["item_id"] if len(frame) else []) -
                     {r["item_id"] for r in rejected})
    warnings["batch_items_without_verdict"] = missing
    return frame.reset_index(drop=True), rejected, warnings


def _conflicts(frame: pd.DataFrame, keys: list[str], verdict: str, queue: str) -> tuple[pd.DataFrame, list[dict]]:
    """Rows sharing a build key must carry one verdict; every row of a key with two is rejected."""
    if not len(frame):
        return frame, []
    n = frame.groupby(keys)[verdict].transform("nunique")
    bad = frame[n > 1]
    rejected = [{"queue": queue, "file": r["_file"], "item_id": r["item_id"],
                 "reason": f"another verdict for the same {'+'.join(keys)} disagrees"} for r in bad.to_dict("records")]
    return frame[n <= 1].reset_index(drop=True), rejected


def merge_moves(frame: pd.DataFrame) -> tuple[pd.DataFrame, list[dict], pd.DataFrame]:
    frame, rejected = _conflicts(frame, ["security_id", "event_date", "rule"], "classification", "moves")
    # the build's older key: one (ticker, event_date) carries one classification
    frame, more = _conflicts(frame, ["ticker", "event_date"], "classification", "moves")
    rejected += more
    columns = ["security_id", "ticker", "event_date", "end_date", "rule", "classification", "source_url",
               "evidence_sources", "correct_source", "event_type", "split_factor", "cash_per_share",
               "first_new_session", "effective_date", "verified_at", "reviewer", "item_id", "notes"]
    if not len(frame):
        return pd.DataFrame(columns=columns), rejected, pd.DataFrame(columns=CHANGE_COLUMNS)
    frame = frame.rename(columns={"_end_date": "end_date"})
    merged = frame[columns].sort_values(["security_id", "event_date", "rule", "item_id"], kind="stable")
    changes = []
    for r in merged.to_dict("records"):
        if r["classification"] == "vendor_error" and r["correct_source"]:
            changes.append({**_change_base(r, "moves"), "change": f"use {r['correct_source']} for the day's row"
                            + (f" through {r['end_date']}" if r["end_date"] and r["end_date"] != r["event_date"] else ""),
                            "applied": "Y", "how": "reconcile source_overrides"})
        elif r["classification"] in ("unrecorded_event", "relist_junction"):
            terms = " ".join(x for x in (r["event_type"], f"factor {r['split_factor']}" if r["split_factor"] else "",
                                         f"cash {r['cash_per_share']}" if r["cash_per_share"] else "",
                                         f"first new session {r['first_new_session']}" if r["first_new_session"] else "")
                             if x)
            changes.append({**_change_base(r, "moves"), "change": f"{r['classification']}: {terms}", "applied": "N",
                            "how": "owner: no event-record input in the build (RELIST_JUNCTIONS is a code dict)"})
        elif r["classification"] == "vendor_error":
            changes.append({**_change_base(r, "moves"), "change": "flag the row (no other vendor has the day)",
                            "applied": "Y", "how": "reconcile flag review_vendor_error"})
    return merged.reset_index(drop=True), rejected, pd.DataFrame(changes, columns=CHANGE_COLUMNS)


CHANGE_COLUMNS = ["queue", "item_id", "security_id", "ticker", "date", "verdict", "change", "applied", "how",
                  "source_url", "evidence_sources"]


def _change_base(r: dict, queue: str) -> dict:
    return {"queue": queue, "item_id": r["item_id"], "security_id": r["security_id"], "ticker": r["ticker"],
            "date": r.get("event_date") or r.get("ex_date", ""), "verdict": r.get("classification") or r.get("verdict"),
            "source_url": r.get("source_url", ""), "evidence_sources": r.get("evidence_sources", "")}


def merge_events(splits: pd.DataFrame, dists: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, list[dict], pd.DataFrame]:
    splits, rejected = _conflicts(splits, ["security_id", "ex_date"], "verdict", "splits")
    dists, more = _conflicts(dists, ["security_id", "ex_date"], "verdict", "distributions")
    rejected += more
    split_cols = ["security_id", "ticker", "ex_date", "verdict", "split_factor", "ex_date_confirmed", "event_type",
                  "source_url", "evidence_sources", "verified_at", "reviewer", "item_id", "notes"]
    dist_cols = ["security_id", "ticker", "ex_date", "verdict", "distribution_type", "ratio", "cash_per_share",
                 "distributed_security", "ex_date_confirmed", "source_url", "verified_at", "reviewer", "item_id", "notes"]
    splits = splits[split_cols].sort_values(["security_id", "ex_date"], kind="stable") if len(splits) else \
        pd.DataFrame(columns=split_cols)
    dists = dists[dist_cols].sort_values(["security_id", "ex_date"], kind="stable") if len(dists) else \
        pd.DataFrame(columns=dist_cols)
    changes = []
    for r in splits.to_dict("records"):
        if r["verdict"] in ("corrected", "not_a_split", "reclassify_distribution"):
            what = {"corrected": " ".join(x for x in (f"factor {r['split_factor']}" if r["split_factor"] else "",
                                                       f"ex-date {r['ex_date_confirmed']}" if r["ex_date_confirmed"] and
                                                       r["ex_date_confirmed"] != r["ex_date"] else "") if x),
                    "not_a_split": "no split that day (S = 1)",
                    "reclassify_distribution": "a distribution, not a split"}[r["verdict"]]
            changes.append({**_change_base(r, "splits"), "change": what, "applied": "N",
                            "how": "owner: no per-event S override in the build"})
    for r in dists.to_dict("records"):
        if r["verdict"] in ("corrected", "not_a_distribution"):
            what = " ".join(x for x in (r["distribution_type"], f"ratio {r['ratio']}" if r["ratio"] else "",
                                         f"cash {r['cash_per_share']}" if r["cash_per_share"] else "",
                                         f"ex-date {r['ex_date_confirmed']}" if r["ex_date_confirmed"] and
                                         r["ex_date_confirmed"] != r["ex_date"] else "") if x)
            changes.append({**_change_base(r, "distributions"),
                            "change": what or ("not a distribution" if r["verdict"] == "not_a_distribution" else ""),
                            "applied": "N", "how": "owner: no per-event S/D override in the build"})
    return splits.reset_index(drop=True), dists.reset_index(drop=True), rejected, \
        pd.DataFrame(changes, columns=CHANGE_COLUMNS)


def merge_terminal(terminal: pd.DataFrame, price: pd.DataFrame) -> tuple[pd.DataFrame, list[dict]]:
    cols = ["security_id", "ticker", "queue", "verdict", "terminal_type", "event_subtype", "cash", "shares",
            "acquirer_security_id", "acquirer_name", "last_trading_day", "special_dividend", "special_dividend_record",
            "fixed_value", "value_rule", "url", "approved", "hold", "hold_last_session", "note", "verified_at",
            "reviewer", "item_id"]
    both = pd.concat([f.assign(queue=q) for f, q in ((terminal, "terminal"), (price, "terminal_price")) if len(f)],
                     ignore_index=True) if len(terminal) or len(price) else pd.DataFrame()
    if not len(both):
        return pd.DataFrame(columns=cols), []
    dup = both[both.duplicated("security_id", keep=False)]
    rejected = [{"queue": r["queue"], "file": r["_file"], "item_id": r["item_id"],
                 "reason": "another verdict for the same security_id"} for r in dup.to_dict("records")]
    both = both[~both["security_id"].isin(dup["security_id"])]
    return both[cols].sort_values("security_id", kind="stable").reset_index(drop=True), rejected


# ------------------------------------------------------------------------------------------------ loaders

def load_merged(name: str, merged_dir: Path | None = None) -> pd.DataFrame | None:
    path = (merged_dir or MERGED_DIR) / name
    return read_csv(path) if path.exists() else None


def move_note(r: dict) -> str:
    """The short note the moves queue carries for a verdict (the full note stays in moves_verdicts.csv)."""
    basis = r["source_url"] or (f"evidence: {r['evidence_sources']}" if r["evidence_sources"] else "")
    extra = f"; use {r['correct_source']}" if r.get("correct_source") else ""
    head = f"hand review {r['item_id']} ({r['reviewer']}): {r['classification']}{extra}"
    return f"{head}; {basis}" if basis else head


def source_overrides(frame: pd.DataFrame | None) -> dict[str, list[dict]]:
    """Per security, the vendor_error verdicts with a correct_source: [{start, end, source, item_id}]."""
    out: dict[str, list[dict]] = {}
    if frame is None or not len(frame):
        return out
    hits = frame[frame["classification"].eq("vendor_error") & frame["correct_source"].isin(VENDORS)]
    for r in hits.to_dict("records"):
        end = r.get("end_date") or r["event_date"]
        out.setdefault(r["security_id"], []).append({"start": r["event_date"], "end": max(end, r["event_date"]),
                                                    "source": r["correct_source"], "item_id": r["item_id"]})
    for sid in out:
        out[sid].sort(key=lambda e: (e["start"], e["item_id"]))
    return out


def reviewed_days(frame: pd.DataFrame | None) -> dict[str, list[dict]]:
    """Per security, the vendor_error verdicts (any correct_source): their spans, for the flag review_vendor_error."""
    out: dict[str, list[dict]] = {}
    if frame is None or not len(frame):
        return out
    hits = frame[frame["classification"].eq("vendor_error")]
    for r in hits.to_dict("records"):
        end = r.get("end_date") or r["event_date"]
        out.setdefault(r["security_id"], []).append({"start": r["event_date"], "end": max(end, r["event_date"]),
                                                    "source": r["correct_source"], "item_id": r["item_id"]})
    return out


def _float(value: str):
    return float(value) if value not in ("", None) else None


def terminal_entries(frame: pd.DataFrame | None, existing: dict[str, dict] | None = None) -> dict[str, dict]:
    """REVIEWED entries from the terminal verdicts, merged onto the code's own entries (``existing``).

    - A verdict with a terminal_type restates the consideration (TERMINAL_TERMS): the code's terms are replaced, a
      blank field is then absent. ``acq`` and ``acq_name`` change only when the verdict names an
      acquirer_security_id (a descriptive name alone would be matched by name, which the verdict did not check).
    - limit (last_trading_day), url, special dividend fields: a non-blank verdict field replaces the code's.
    - approve / correct / decide_type: ``approved`` is the verdict's text (blank: the code's approval, if any,
      stays); a code ``hold`` the verdict does not repeat is dropped (the reviewer settled it).
    - hold: its reason replaces the code's hold.
    - price_gap: no ``approved`` (its text stays in terminal_verdicts.csv), so the value guard still applies once
      the price arrives; a code hold the verdict does not repeat is dropped.
    - unresolved: no change.
    The code's ``note`` is kept; ``review`` names the verdict (item_id and verdict) for the output note."""
    existing = existing or {}
    out: dict[str, dict] = {}
    if frame is None or not len(frame):
        return out
    for r in frame.to_dict("records"):
        sid, verdict = r["security_id"], r["verdict"]
        if verdict == "unresolved":
            continue
        entry = dict(existing.get(sid) or {})
        for key in TERMINAL_DROP_KEYS.get(sid, ()):
            entry.pop(key, None)
        fields = {}
        for column, key in TERMINAL_MAP.items():
            value = r.get(column, "")
            if value in ("", None):
                continue
            if key in TERMINAL_FLOATS:
                value = float(value)
            elif key == "hold_last_session":
                value = value == "Y"
            fields[key] = value
        if r.get("terminal_type"):
            for key in TERMINAL_TERMS:
                entry.pop(key, None)
        if not r.get("acquirer_security_id"):
            fields.pop("acq_name", None)
        elif "acq_name" not in fields:
            entry.pop("acq_name", None)
        approved = fields.pop("approved", "")
        if verdict in ("approve", "correct", "decide_type") and approved:
            fields["approved"] = f"{r['item_id']}: {approved}"
        fields.pop("note", None)  # the verdict's note and a price_gap's 'approved' stay in terminal_verdicts.csv
        # a short label under its own key: the terminal step appends it to the output note after the value guard has
        # read the code's note (a verdict's 'no election' must not raise the election guard)
        fields["review"] = f"hand review {r['item_id']} ({verdict}{', terms checked, price pending' if verdict == 'price_gap' else ''})"
        if verdict != "hold" and "hold" not in fields:
            entry.pop("hold", None)
        entry.update(fields)
        out[sid] = entry
    return out


# ------------------------------------------------------------------------------------------------- merge

def queue_keys(inputs: Path) -> dict[str, set]:
    """The build's own queue keys in INPUTS (the tables the verdicts answer): a verdict for a key the build no
    longer has is rejected."""
    keys = {}
    path = inputs / "reviewed_moves.csv"
    if path.exists():
        r = read_csv(path)
        rule = r["notes"].str.extract(r"^\[([^\]]+)\]", expand=False).fillna("")
        keys["moves"] = set(zip(r["security_id"], r["event_date"], rule))
    path = inputs / "split_events.csv"
    if path.exists():
        r = read_csv(path)
        keys["splits"] = set(zip(r["security_id"], r["ex_date"]))
    path = inputs / "special_distributions.csv"
    if path.exists():
        r = read_csv(path)
        keys["distributions"] = set(zip(r["security_id"], r["ex_date"]))
    path = inputs / "terminal_returns_2012_2026.csv"
    if path.exists():
        r = read_csv(path)
        keys["terminal"] = keys["terminal_price"] = {(s,) for s in r["security_id"]}
    return keys


KEY_COLUMNS = {"moves": ["security_id", "event_date", "rule"], "splits": ["security_id", "ex_date"],
               "distributions": ["security_id", "ex_date"], "terminal": ["security_id"],
               "terminal_price": ["security_id"]}


def merge(root: Path = ROUND_DIR, out_dir: Path | None = None, existing_terminal: dict[str, dict] | None = None,
          inputs: Path | None = common.INPUTS) -> dict:
    out_dir = Path(out_dir or root / "merged")
    out_dir.mkdir(parents=True, exist_ok=True)
    known = queue_keys(Path(inputs)) if inputs is not None else {}
    accepted, rejected, warnings = {}, [], {}
    for queue in QUEUES:
        frame, bad, warn = load_queue(root, queue)
        if queue in known and len(frame):
            key = list(zip(*[frame[c] for c in KEY_COLUMNS[queue]]))
            unknown = [k not in known[queue] for k in key]
            bad += [{"queue": queue, "file": r["_file"], "item_id": r["item_id"],
                     "reason": "key not in the build's queue table (" + "+".join(KEY_COLUMNS[queue]) + ")"}
                    for r, u in zip(frame.to_dict("records"), unknown) if u]
            frame = frame[[not u for u in unknown]].reset_index(drop=True)
        accepted[queue], warnings[queue] = frame, warn
        rejected += bad
    moves, bad, move_changes = merge_moves(accepted["moves"])
    rejected += bad
    splits, dists, bad, split_changes = merge_events(accepted["splits"], accepted["distributions"])
    rejected += bad
    terminal, bad = merge_terminal(accepted["terminal"], accepted["terminal_price"])
    rejected += bad
    outputs = {"moves_verdicts.csv": moves, "split_verdicts.csv": splits, "distribution_verdicts.csv": dists,
               "terminal_verdicts.csv": terminal, "moves_data_changes.csv": move_changes,
               "split_data_changes.csv": split_changes,
               "rejected.csv": pd.DataFrame(rejected, columns=["queue", "file", "item_id", "reason"])}
    for name, frame in outputs.items():
        frame.to_csv(out_dir / name, index=False, lineterminator="\n")
    if existing_terminal is None:
        try:
            from scripts import reversal_data_terminal as terminal_step
            existing_terminal = terminal_step.CODE_REVIEWED
        except Exception:  # noqa: BLE001 - the listing is a convenience; the merge stands without it
            existing_terminal = {}
    entries = terminal_entries(terminal, existing_terminal)
    lines = ["# REVIEWED entries built from terminal_verdicts.csv (reversal_data_review.terminal_entries).",
             "# The terminal step applies them itself; this file shows them beside the code's own entry.", ""]
    for sid, entry in sorted(entries.items()):
        lines.append(f"# {sid}: code entry {existing_terminal.get(sid)!r}")
        lines.append(f"{sid!r}: {entry!r},")
    (out_dir / "terminal_reviewed_entries.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    summary = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "root": str(root),
        "accepted": {q: {"rows": int(len(f)), "by_verdict": f[QUEUES[q]["verdict"]].value_counts().to_dict()
                         if len(f) else {}} for q, f in accepted.items()},
        "merged_rows": {k: int(len(v)) for k, v in outputs.items()},
        "rejected": {"rows": len(rejected), "by_queue": pd.Series([r["queue"] for r in rejected]).value_counts().to_dict()
                     if rejected else {}},
        "warnings": {q: {"non_primary_url": len(w["non_primary_url"]), "non_primary_hosts":
                         pd.Series([x["host"] for x in w["non_primary_url"]]).value_counts().to_dict()
                         if w["non_primary_url"] else {},
                         "dollar_in_note": len(w["dollar_in_note"]),
                         "batch_items_without_verdict": w["batch_items_without_verdict"]}
                     for q, w in warnings.items()},
        "data_changes": {"moves": move_changes["applied"].value_counts().to_dict() if len(move_changes) else {},
                         "splits_distributions": split_changes["applied"].value_counts().to_dict()
                         if len(split_changes) else {}},
        "terminal_entries": len(entries),
    }
    (out_dir / "merge_summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", default=str(ROUND_DIR), help="the round's review folder")
    parser.add_argument("--out-dir", default="", help="where the merged files go (default <root>/merged)")
    parser.add_argument("--inputs", default=str(common.INPUTS),
                        help="the INPUTS folder whose queue tables the verdicts must match ('' to skip the check)")
    args = parser.parse_args(argv)
    summary = merge(Path(args.root), Path(args.out_dir) if args.out_dir else None,
                    inputs=Path(args.inputs) if args.inputs else None)
    print(json.dumps({k: summary[k] for k in ("accepted", "merged_rows", "rejected", "data_changes")}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
