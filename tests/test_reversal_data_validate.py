"""Tests for plan step 14, the validation checks and the manifest (scripts/reversal_data_validate.py).

Synthetic inputs in a temporary directory; no request is sent. The import scan enforces that the
validator imports no signal, backtest, strategy or research module. The last group reads the built
summary and manifest when they exist, and is skipped otherwise.
"""
import ast
import gzip
import json
from pathlib import Path
import sys
import urllib.request

import numpy as np
import pandas as pd
import pytest

from pipelines.reversal_data import common
from pipelines.reversal_data import validate as va

SCRIPT = Path("pipelines/reversal_data/validate.py")   # moved from scripts/reversal_data_validate.py (phase 3)


# ------------------------------------------------------------------ what the validator may import and call

def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            names |= {f"{base}.{alias.name}" if base == "pipelines.reversal_data" else base for alias in node.names}
    return names


def test_the_validator_imports_no_signal_backtest_or_research_module():
    allowed_third_party = {"numpy", "pandas", "exchange_calendars", "pipelines.reversal_data.common"}
    for name in _imports(SCRIPT):
        top = name.split(".")[0]
        assert name in allowed_third_party or top in sys.stdlib_module_names or top == "__future__", name
        assert not any(word in name.lower() for word in ("signal", "backtest", "strategy", "portfolio", "research",
                                                         "src.")), name


def test_the_validator_has_no_dynamic_import_and_no_network_call():
    text = SCRIPT.read_text(encoding="utf-8")
    for forbidden in ("importlib", "__import__(", "urlopen", "cached_get", "requests.", "http.client", "socket"):
        assert forbidden not in text, forbidden
    tree = ast.parse(text)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "update_manifest"]
    assert not calls  # the manifest is built in memory and written once, by write_outputs
    writers = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            n = sum(isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "atomic_write"
                    for c in ast.walk(node))
            if n:
                writers[node.name] = n
    # the review batch writer writes one new review file on request (--write-multi-source-batch) and nothing else
    assert writers == {"write_outputs": 2, "write_validation_summary": 1, "write_multi_source_batch": 1}


def test_every_check_is_one_function_named_like_its_result():
    names = [va.check_name(f) for f in va.CHECKS]
    assert len(names) == len(set(names)) == 35
    text = SCRIPT.read_text(encoding="utf-8")
    for name in names:
        assert f'name, dataset, plan = "{name}"' in text


# ------------------------------------------------------------------ fixtures

@pytest.fixture
def ctx(tmp_path, monkeypatch):
    for sub in ("inputs", "cache/factors", "cache/prices", "cache/raw/kf", "cache/tiingo", "main", "repo"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)

    def no_network(*args, **kwargs):
        raise AssertionError("a request was made")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    return va.Context(inputs=tmp_path / "inputs", cache=tmp_path / "cache", main=tmp_path / "main", repo=tmp_path / "repo")


def _write(path: Path, frame: pd.DataFrame) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, compression="gzip" if path.suffix == ".gz" else None)
    return path


def _factor_rows(dates, series):
    return pd.DataFrame([{"date": d, "series": s, "value_pct": "0.10", "value_dec": "0.0010", "missing": ""}
                         for s in series for d in dates])


def _factor_files(ctx, dates):
    _write(ctx.cache / "factors" / "ff5_2x3_daily.csv", _factor_rows(dates, ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"]))
    _write(ctx.cache / "factors" / "mom_daily.csv", _factor_rows(dates, ["Mom"]))
    _write(ctx.cache / "factors" / "st_rev_daily.csv", _factor_rows(dates, ["ST_Rev"]))
    ind = pd.DataFrame({"date": np.tile(dates, 98), "weighting": np.repeat(["vw", "ew"], 49 * len(dates)),
                        "industry_id": np.tile(np.repeat(np.arange(1, 50), len(dates)), 2),
                        "series": "X", "value_pct": "0.1", "value_dec": "0.001", "missing": ""})
    _write(ctx.cache / "factors" / "ind49_daily.csv.gz", ind)
    _write(ctx.cache / "factors" / "vix_daily.csv",
           pd.DataFrame({"date": dates, "open": 15.0, "high": 15.0, "low": 15.0, "close": 15.0, "xnas_session": "Y"}))


# ------------------------------------------------------------------ factors

def test_factor_rows_pass_on_exactly_the_sessions_and_fail_on_one_missing_day(ctx):
    dates = list(ctx.sessions_between(va.FACTOR_FROM, va.FACTOR_TO).strftime("%Y-%m-%d"))
    assert len(dates) == va.FACTOR_ROWS == 3655
    _factor_files(ctx, dates)
    out = va.check_factor_rows(ctx)
    assert out["passed"] and out["numbers"]["series"] == 6 + 1 + 1 + 98 + 1
    _write(ctx.cache / "factors" / "mom_daily.csv", _factor_rows(dates[:100] + dates[101:], ["Mom"]))
    fresh = va.Context(inputs=ctx.inputs, cache=ctx.cache, main=ctx.main)
    out = va.check_factor_rows(fresh)
    assert not out["passed"] and out["details"]["failing"]["mom_daily:Mom"]["sessions_missing"] == 1


def test_factor_rows_count_a_missing_code_as_a_failure(ctx):
    dates = list(ctx.sessions_between(va.FACTOR_FROM, va.FACTOR_TO).strftime("%Y-%m-%d"))
    _factor_files(ctx, dates)
    rows = _factor_rows(dates, ["ST_Rev"])
    rows.loc[5, "value_pct"] = "-99.99"
    _write(ctx.cache / "factors" / "st_rev_daily.csv", rows)
    out = va.check_factor_rows(ctx)
    assert not out["passed"] and out["details"]["failing"]["st_rev_daily:ST_Rev"]["missing_codes"] == 1


def test_a_missing_factor_file_is_no_input(ctx):
    out = va.check_factor_rows(ctx)
    assert out["status"] == "no_input" and not out["passed"] and out["details"]["missing_inputs"]


def test_own_returns_follow_the_crsp_definition():
    close = pd.Series([100.0, 50.0, 51.0])
    split = pd.Series([1.0, 2.0, 1.0])
    div = pd.Series([0.0, 1.0, 0.0])
    r = va._own_returns(close, split, div)
    assert np.isnan(r[0]) and r[1] == pytest.approx(0.01) and r[2] == pytest.approx(0.02)


# ------------------------------------------------------------------ listings

def test_listing_snapshots_find_hash_mismatches_small_files_and_list_old_snapshots(ctx):
    snap = ctx.cache / "listings" / "symdir" / "a.csv"
    _write(snap, pd.DataFrame({"Symbol": ["AAA"], "Name": ["A Inc"]}))
    good = common.sha256_file(snap)
    index = pd.DataFrame([
        {"snapshot_date": "2011-12-30", "source": "wayback_symdir", "rows": 2500, "sha256": good,
         "snapshot_file": "research_cache/reversal_2012_2026/listings/symdir/a.csv"},
        {"snapshot_date": "2013-01-02", "source": "repo_symdir", "rows": 2500, "sha256": good,
         "snapshot_file": "research_cache/reversal_2012_2026/listings/symdir/a.csv"},
    ])
    _write(ctx.inputs / "listing_snapshots_index.csv", index)
    out = va.check_listing_snapshots(ctx)
    # 2012: one snapshot a year old, so many weeks are over 160 days; listed, and the plan accepts that
    assert out["numbers"]["weeks_age_over_160"] > 0 and out["details"]["stale_stretches"][0]["snapshot"] == "2011-12-30"
    assert out["numbers"]["weeks_with_snapshot"] == 759 and out["passed"]
    index.loc[1, "rows"] = 900
    index.loc[0, "sha256"] = "0" * 64
    _write(ctx.inputs / "listing_snapshots_index.csv", index)
    out = va.check_listing_snapshots(va.Context(inputs=ctx.inputs, cache=ctx.cache, main=ctx.main))
    assert not out["passed"] and out["numbers"]["symbol_files_under_1000_rows"] == 1
    assert out["numbers"]["files_hash_mismatch"] == 1


# ------------------------------------------------------------------ prices

def _panel(ctx, rows):
    frame = pd.DataFrame(rows)
    for column, value in (("close_raw", 20.0), ("volume_raw", 1000.0), ("split_factor", 1.0), ("div_cash", 0.0),
                          ("tr", 0.0), ("src_primary", "yahoo"), ("n_sources", 1), ("max_src_diff", 0.0), ("flags", "")):
        frame[column] = frame[column].fillna(value) if column in frame else value
    _write(ctx.cache / "prices" / "daily_panel.csv.gz", frame)
    for sid, group in frame.groupby("security_id"):
        _write(ctx.cache / "prices" / f"{sid}.csv", group.drop(columns=["security_id"]))
    return frame


def test_multi_source_agreement_needs_the_share_and_no_unresolved_day(ctx):
    days = list(ctx.sessions_between("2015-01-02", "2015-12-31").strftime("%Y-%m-%d"))[:200]
    rows = [{"security_id": "1", "date": d, "n_sources": 2, "max_src_diff": 0.0} for d in days]
    _panel(ctx, rows)
    out = va.check_multi_source_agreement(ctx)
    assert out["numbers"]["share"] == 1.0 and out["numbers"]["disagree_unresolved"] == 0
    rows[3].update(max_src_diff=0.02, flags="disagree_unresolved")
    _panel(ctx, rows)
    out = va.check_multi_source_agreement(va.Context(inputs=ctx.inputs, cache=ctx.cache, main=ctx.main))
    assert not out["passed"] and out["numbers"]["share"] == 0.995 and out["numbers"]["disagree_unresolved"] == 1


def test_multi_source_batch_lists_universe_unresolved_days_under_their_moves_items(ctx, tmp_path):
    _universe(ctx, [{"week_end": "2015-01-09", "security_id": "1", "dv50_rank": 10},
                    {"week_end": "2015-01-09", "security_id": "2", "dv50_rank": 20}])
    _master(ctx, [{"security_id": "1"}, {"security_id": "2"}])
    _write(ctx.inputs / "terminal_returns_2012_2026.csv", pd.DataFrame(
        columns=["security_id", "ticker", "terminal_type", "status", "last_price_date"]))
    days = list(ctx.sessions_between("2015-01-02", "2015-03-31").strftime("%Y-%m-%d"))
    rows = [{"security_id": sid, "date": d} for sid in ("1", "2") for d in days]
    for r in rows:
        if (r["security_id"], r["date"]) in {("1", "2015-01-06"), ("2", "2015-01-07"), ("1", "2015-03-30")}:
            r.update(n_sources=2, max_src_diff=0.01, flags="disagree_unresolved", src_primary="tiingo")
    _panel(ctx, rows)
    moves = ctx.cache / "review" / "round10" / "moves"
    _write(moves / "batch_09.csv", pd.DataFrame([
        {"item_id": "moves-09-002", "security_id": "1", "ticker": "A", "rule": "R3", "event_date": "2015-01-05",
         "end_date": "2015-01-06", "verdict_path": str(moves / "verdict_09.csv")},
        {"item_id": "moves-09-003", "security_id": "1", "ticker": "A", "rule": "R1", "event_date": "2015-01-06",
         "end_date": "", "verdict_path": str(moves / "verdict_09.csv")}]))
    frame = va.write_multi_source_batch(ctx, tmp_path / "batch_01.csv")
    # 2015-03-30 lies after the hold weeks of the one universe week: not a universe day
    assert sorted(zip(frame["security_id"], frame["universe_day"], frame["item_id"])) == [
        ("1", "2015-01-06", "moves-09-002"), ("1", "2015-01-06", "moves-09-003"), ("2", "2015-01-07", "multi-01-001")]
    assert frame.set_index("item_id").loc[["moves-09-002", "moves-09-003"], "match_kind"].tolist() == [
        "inside_item_range", "event_date"]
    new = frame[frame["item_id"] == "multi-01-001"].iloc[0]
    assert new["in_moves_batch"] == "N" and new["verdict_path"].endswith("multi_source/verdict_01.csv")
    assert set(frame.loc[frame["in_moves_batch"] == "Y", "verdict_path"]) == {str(moves / "verdict_09.csv")}
    assert pd.read_csv(tmp_path / "batch_01.csv", dtype=str).shape[0] == 3


def test_panel_integrity_flags_non_sessions_duplicates_and_stored_rows(ctx):
    rows = [{"security_id": "1", "date": "2015-01-02"}, {"security_id": "1", "date": "2015-01-05"}]
    _panel(ctx, rows)
    assert va.check_panel_integrity(ctx)["passed"]
    rows += [{"security_id": "1", "date": "2015-01-03"}, {"security_id": "1", "date": "2015-01-05"},
             {"security_id": "2", "date": "2015-01-05", "src_primary": "stored"}]
    _panel(ctx, rows)
    out = va.check_panel_integrity(va.Context(inputs=ctx.inputs, cache=ctx.cache, main=ctx.main))
    assert not out["passed"]
    assert out["numbers"]["non_session_dates"] == 1 and out["numbers"]["duplicates"] == 1
    assert out["numbers"]["src_not_vendor"] == 1


def test_adj_identity_error_is_zero_for_consistent_rows_and_catches_a_bad_one():
    frame = pd.DataFrame({"close": [100.0, 50.0, 49.0], "splitFactor": [1.0, 2.0, 1.0], "divCash": [0.0, 1.0, 0.0]})
    frame["adjClose"] = [1.0, 1.01, 1.01 * 49.0 / 50.0]
    assert va.adj_identity_error(frame).max() < 1e-12
    frame.loc[2, "adjClose"] *= 1.001
    assert va.adj_identity_error(frame).max() == pytest.approx(0.001, rel=1e-6)


def test_tiingo_row_check_reads_the_month1_raw_files(ctx):
    rows = [{"date": "2015-01-02T00:00:00.000Z", "close": 10.0, "adjClose": 5.0, "divCash": 0.0, "splitFactor": 1.0},
            {"date": "2015-01-05T00:00:00.000Z", "close": 11.0, "adjClose": 5.5, "divCash": 0.0, "splitFactor": 1.0}]
    raw = ctx.cache / "raw" / "tiingo" / "AAA__2011-06-01_2026-08-31.json.gz"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(gzip.compress(json.dumps(rows).encode()))
    _write(ctx.cache / "tiingo" / "fetch_status.csv",
           pd.DataFrame([{"security_id": "1", "ticker_for_source": "AAA", "status": "done", "raw_path": str(raw),
                          "prices_path": ""}]))
    out = va.check_tiingo_row_check(ctx)
    assert out["passed"] and out["numbers"]["rows_checked"] == 1
    rows[1]["adjClose"] = 5.6
    raw.write_bytes(gzip.compress(json.dumps(rows).encode()))
    out = va.check_tiingo_row_check(va.Context(inputs=ctx.inputs, cache=ctx.cache, main=ctx.main))
    assert not out["passed"] and out["numbers"]["files_over"] == 1


def test_adj_identity_kinds_name_cash_per_new_share_and_a_half_cent_close():
    # row 1: S = 1.1 with cash of the same value, adjClose taking D x S (AFSI 2012-08-30's shape)
    # row 3: adjClose built from a close half a cent above the close field (RAVN), row 4 reverses it
    close = [28.8, 26.05, 26.0, 30.76, 30.47, 31.0]
    split = [1.0, 1.1, 1.0, 1.0, 1.0, 1.0]
    div = [0.0, 2.605, 0.0, 0.0, 0.0, 0.0]
    adj = [1.0]
    for i in range(1, len(close)):
        adj.append(adj[-1] * (close[i] * split[i] + div[i] * split[i]) / close[i - 1])
    frame = pd.DataFrame({"date": [f"2012-0{m}-01" for m in range(1, 7)], "close": close, "splitFactor": split,
                          "divCash": div, "adjClose": adj})
    frame.loc[3, "adjClose"] = frame.loc[2, "adjClose"] * 30.765 / 26.0
    frame.loc[4:, "adjClose"] = frame.loc[3, "adjClose"] * np.cumprod(np.array(close[4:]) / np.array([30.765] + close[4:-1]))
    kinds = va.adj_identity_kinds(frame)
    assert kinds["kind"].tolist() == ["dividend_times_split", "sub_cent", "sub_cent"]
    assert kinds["date"].tolist() == ["2012-02-01", "2012-04-01", "2012-05-01"]
    assert kinds["close_gap"].iloc[1] == pytest.approx(0.005, abs=1e-9)
    frame.loc[3, "adjClose"] *= 1.01   # a whole-percent miss is neither
    assert "other" in set(va.adj_identity_kinds(frame)["kind"])


def test_price_quantum_finds_the_coarsest_grid_within_float32_restoration_error():
    q = va.price_quantum([19.14000034, 19.045, 1.2345, 0.47625 * 40, 12.3456789])
    assert q[0] == 0.01 and q[1] == 0.001 and q[2] == 0.0001 and q[3] == 0.01 and np.isnan(q[4])


def test_rounding_explained_accepts_a_cent_rounded_close_and_not_a_cent_gap_or_an_event_gap():
    dates = ["2011-06-01", "2011-06-02", "2011-06-03", "2011-06-06", "2011-06-07", "2011-06-08"]
    t = pd.DataFrame({"date": dates, "close": [19.14, 19.045, 19.10, 19.20, 19.30, 19.40],
                      "splitFactor": 1.0, "divCash": [0, 0, 0, 0, 0, 0.1]})
    y = pd.DataFrame({"date": dates, "close_raw": [19.14000034, 19.04999971, 19.10, 19.22, 19.30, 19.40],
                      "split_factor": 1.0, "div_cash": 0.0})
    out = va.rounding_explained(t, y)
    # 06-02: half-cent close in Tiingo, cent close in Yahoo; 06-03: both days within the grid
    assert out.loc["2011-06-02", "rounding"] and out.loc["2011-06-03", "rounding"]
    # 06-06: a two-cent gap is not rounding, nor is the day after it; 06-08: the dividend differs
    assert not out.loc["2011-06-06", "rounding"] and not out.loc["2011-06-07", "rounding"]
    assert not out.loc["2011-06-08", "event_ok"] and not out.loc["2011-06-08", "rounding"]


def _v_fixture(ctx, t_close, y_close):
    days = list(ctx.sessions_between("2015-01-02", "2015-03-31").strftime("%Y-%m-%d"))[:len(t_close)]
    prices = _write(ctx.cache / "tiingo" / "prices" / "AAA.csv.gz",
                    pd.DataFrame({"date": days, "close": t_close, "splitFactor": 1.0, "divCash": 0.0}))
    _write(ctx.cache / "tiingo" / "fetch_status.csv",
           pd.DataFrame([{"security_id": "1", "ticker_for_source": "AAA", "status": "done", "raw_path": "",
                          "prices_path": str(prices)}]))
    _write(ctx.cache / "yahoo" / "1.csv.gz", pd.DataFrame({"date": days, "close_raw": y_close, "split_factor": 1.0,
                                                           "div_cash": 0.0, "junction": ""}))
    _write(ctx.inputs / "candidate_fetch_list.csv",
           pd.DataFrame([{"security_id": "1", "ticker_for_source": "AAA", "reason": "V_verify_sample", "status": "done"},
                         {"security_id": "2", "ticker_for_source": "BBB", "reason": "V_verify_sample",
                          "status": "deferred_quota"}]))
    _panel(ctx, [{"security_id": "1", "date": d} for d in days])


def test_v_sample_reports_rounding_days_apart_and_keeps_the_plan_share(ctx):
    t_close = [10.0, 10.005, 10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7]
    y_close = [10.0, 10.01, 10.0, 10.1, 10.2, 10.25, 10.4, 10.5, 10.6, 10.7]   # 1 rounding day pair, 1 five-cent gap
    _v_fixture(ctx, t_close, y_close)
    out = va.check_v_sample(ctx)
    n = out["numbers"]
    assert not out["passed"] and n["compared"] == 1 and n["days"] == 9
    assert n["over_1e-4"] == 4 and n["rounding_explained"] == 2 and n["not_rounding"] == 2
    assert n["not_rounding_by_kind"] == {"level_gap": 2} and n["share"] == pytest.approx(5 / 9, abs=1e-6)
    assert n["share_within_or_rounding"] == pytest.approx(7 / 9, abs=1e-6)
    assert n["not_compared_by_candidate_status"] == {"deferred_quota": 1}
    assert [d["date"] for d in out["details"]["not_rounding_days"]] == ["2015-01-09", "2015-01-12"]


def test_known_cases_read_a_merger_last_price_date_as_the_repo_anchor_not_the_last_session(ctx):
    days = list(ctx.sessions_between("2015-02-02", "2015-03-31").strftime("%Y-%m-%d"))
    _write(ctx.inputs / "ticker_intervals.csv", pd.DataFrame(
        [{"security_id": s, "ticker": t, "start": "2010-01-04", "end": "2026-08-31", "exchange": "NASDAQ", "source": "x",
          "source_url": ""} for s, t in (("1", "AAA"), ("2", "BBB"), ("3", "CCC"), ("4", "DDD"))]))
    ends = {"1": "2015-03-09", "3": "2015-03-05", "4": "2015-03-12", "2": "2015-03-31"}
    _panel(ctx, [{"security_id": s, "date": d} for s, last in ends.items() for d in days if d <= last])
    _universe(ctx, [{"week_end": "2015-02-06", "security_id": s, "dv50_rank": 10} for s in ends])
    actions = pd.DataFrame([{"predecessor": p, "last_price_date": "2015-03-02", "successor": "BBB",
                             "effective_date": "2015-03-10", "share_ratio": 0.5, "cash_per_share": 10.0,
                             "source_url": "https://www.sec.gov/x", "verified_at": "2026-01-01"} for p in ("AAA", "CCC", "DDD")])
    _write(ctx.main / "stocks_list_dir" / "nasdaq" / "corporate_actions.csv", actions)
    states = {r["predecessor"]: (r["state"], r["ends"]) for r in va.check_known_cases(ctx)["details"]["repo_files"]["corporate_actions"]}
    # AAA trades on to the session before the effective date (ANSS 2025-07-16 against its 2025-07-17 closing date)
    assert states["AAA"] == ("consistent", "last_session_before_effective_date")
    assert states["CCC"][0] == "series_ends_between_last_price_date_and_effective_date"
    assert states["DDD"][0] == "series_runs_past_effective_date"


def test_nasdaq_dividends_reads_cash_and_cash_stock_rows_and_the_answer_state():
    payload = {"data": {"dividends": {"rows": [
        {"exOrEffDate": "12/01/2022", "type": "Cash/Stock", "amount": "$0.265"},
        {"exOrEffDate": "09/06/2022", "type": "Cash", "amount": "$0.265"},
        {"exOrEffDate": "N/A", "type": "Cash", "amount": "$1.00"}]}}}
    cash, stock, state = va.nasdaq_dividends(payload)
    assert state == "rows" and cash.to_dict() == {"2022-09-06": 0.265, "2022-12-01": 0.265} and stock == {"2022-12-01"}
    empty = {"data": {"dividends": {"rows": None}}, "message": "Dividend History for Non-Nasdaq symbols is not available"}
    assert va.nasdaq_dividends(empty)[2] == "not_nasdaq"
    empty["message"] = "Dividend History information is presently unavailable for this company."
    assert va.nasdaq_dividends(empty)[2] == "no_history"


def test_compare_cash_leaves_out_a_distribution_only_one_side_has():
    a = pd.Series({"2015-03-04": 0.21, "2015-06-03": 0.21, "2015-12-01": 0.21})
    b = pd.Series({"2015-03-04": 0.21, "2015-06-03": 0.215, "2015-07-01": 5.0, "2016-03-01": 0.21})
    got = va.compare_cash(a, b, "2015-01-01", "2015-12-31", skip={"2015-07-01"})
    assert got["matched"] == 2 and got["either"] == 3 and got["skipped"] == 1
    assert got["only_a"] == ["2015-12-01"] and got["only_b"] == [] and got["gaps"] == [("2015-06-03", pytest.approx(0.005))]


def test_dividends_compare_the_nasdaq_sample_with_the_canonical_dividends(ctx, monkeypatch):
    monkeypatch.setattr(va, "NASDAQ_DIV_SAMPLE", 1)
    days = list(ctx.sessions_between("2015-01-02", "2015-12-31").strftime("%Y-%m-%d"))
    _panel(ctx, [{"security_id": "1", "date": d, "split_factor": 1.05 if d == "2015-11-24" else 1.0} for d in days])
    _write(ctx.cache / "dividends.csv", pd.DataFrame(
        [{"security_id": "1", "ex_date": d, "cash_as_paid": c, "sources": "yahoo", "special": s}
         for d, c, s in (("2015-03-04", 0.21, ""), ("2015-06-03", 0.21, ""), ("2015-07-01", 5.0, "Y"),
                         ("2015-09-09", 0.21, ""), ("2015-11-24", 0.21, ""))]))
    folder = ctx.cache / "raw" / "nasdaq" / "dividends"
    _write(folder / "sample.csv", pd.DataFrame([{"security_id": "1", "ticker": "AAA", "stratum": "R_seeded_draw"}]))
    rows = [{"exOrEffDate": d, "type": t, "amount": a} for d, t, a in (
        ("03/04/2015", "Cash", "$0.21"), ("06/03/2015", "Cash", "$0.21"), ("09/09/2015", "Cash", "$0.215"),
        ("11/24/2015", "Cash/Stock", "$0.21"), ("12/01/2015", "Cash/Stock", "$0.21"))]
    (folder / "AAA.json.gz").write_bytes(gzip.compress(json.dumps({"data": {"dividends": {"rows": rows}}}).encode()))
    out = va.check_dividends(ctx)
    pair = out["numbers"]["nasdaq_pairs"]["canonical_nasdaq"]
    assert not out["passed"] and out["numbers"]["nasdaq_answers"] == {"rows": 1}
    assert (pair["ex_dates_matched"], pair["ex_dates_either"], pair["distribution_dates_left_out"]) == (4, 5, 1)
    assert pair["amounts_over_0.001"] == 1 and pair["only_nasdaq"] == 1 and not pair["meets_threshold"]
    lists = out["details"]["nasdaq_pairs"]["canonical_nasdaq"]
    assert lists["amount_gaps"][0]["date"] == "2015-09-09" and lists["only_nasdaq"][0]["date"] == "2015-12-01"
    # the Cash/Stock day with a canonical split factor passes; the one without is listed
    assert [r["date"] for r in out["details"]["nasdaq_stock_dividend_days_without_split"]] == ["2015-12-01"]
    assert pair["usable_names"] == 1 and out["numbers"]["nasdaq_usable_names_by_pair"]["canonical_nasdaq"] == 1
    # A second name whose only canonical "dividend" is a distribution and Nasdaq knows none compares nothing:
    # the sample has 2 names but 1 usable comparison, so a 2-name sample is not complete.
    monkeypatch.setattr(va, "NASDAQ_DIV_SAMPLE", 2)
    _panel(ctx, [{"security_id": sid, "date": d, "split_factor": 1.05 if (sid, d) == ("1", "2015-11-24") else 1.0}
                 for sid in ("1", "2") for d in days])
    canonical = pd.read_csv(ctx.cache / "dividends.csv", dtype=str, keep_default_na=False)
    _write(ctx.cache / "dividends.csv", pd.concat([canonical, pd.DataFrame(
        [{"security_id": "2", "ex_date": "2015-06-10", "cash_as_paid": "3.0", "sources": "yahoo", "special": "Y"}])]))
    _write(folder / "sample.csv", pd.DataFrame([{"security_id": "1", "ticker": "AAA", "stratum": "R_seeded_draw"},
                                                {"security_id": "2", "ticker": "BBB", "stratum": "R_seeded_draw"}]))
    (folder / "BBB.json.gz").write_bytes(gzip.compress(json.dumps({"data": {"dividends": {"rows": []}}}).encode()))
    out = va.check_dividends(va.Context(inputs=ctx.inputs, cache=ctx.cache, main=ctx.main))
    pair = out["numbers"]["nasdaq_pairs"]["canonical_nasdaq"]
    assert out["numbers"]["nasdaq_sample_names"] == 2 and pair["securities"] == 2
    assert pair["usable_names"] == 1 and pair["nothing_to_compare_tickers"] == ["BBB"]
    assert not out["numbers"]["nasdaq_sample_complete"]
    # Both judged pairs need the sample's usable names (the plan's row names Tiingo against Nasdaq): with no
    # month-1 Tiingo file Tiingo-Nasdaq compares nothing and is the larger shortfall.
    by_pair = out["numbers"]["nasdaq_usable_names_short_of_sample_by_pair"]
    assert by_pair["canonical_nasdaq"] == 1 and by_pair["tiingo_nasdaq"] == 2 - pair_usable(out, "tiingo_nasdaq")
    assert out["numbers"]["nasdaq_usable_names_short_of_sample"] == max(by_pair.values())


def pair_usable(out, key):
    return out["numbers"]["nasdaq_usable_names_by_pair"][key]


def test_review_queue_fails_on_any_unreviewed_item(ctx):
    queue = pd.DataFrame([
        {"ticker": "A", "event_date": "2015-01-02", "classification": "market_move", "source_url": "u", "verified_at": "x",
         "notes": "[R1] move", "security_id": "1", "sources_agreeing": ""},
        {"ticker": "B", "event_date": "2016-01-04", "classification": "unreviewed", "source_url": "", "verified_at": "",
         "notes": "[R4] flat", "security_id": "2", "sources_agreeing": ""}])
    _write(ctx.inputs / "reviewed_moves.csv", queue)
    out = va.check_review_queue(ctx)
    assert not out["passed"] and out["numbers"]["open"] == 1 and out["numbers"]["open_by_rule"] == {"R4": 1}


# ------------------------------------------------------------------ splits

def _splits(n_agree, n_total, reviewed=True, url=True):
    rows = []
    for i in range(n_total):
        agree = i < n_agree
        rows.append({"security_id": str(i), "ticker": f"T{i}", "ex_date": "2015-01-02", "split_factor": "2",
                     "event_type": "split", "tiingo": "", "yahoo": "2.0", "wiki": "2.0", "nasdaq": "",
                     "agree": "Y" if agree else "N", "sec_url": "", "verified_at": "" if agree or not reviewed else "x",
                     "notes": "", "sec_candidates": ""})
    rows.append({"security_id": "d", "ticker": "D", "ex_date": "2015-01-02", "split_factor": "1.2", "event_type": "distribution",
                 "tiingo": "", "yahoo": "1.2", "wiki": "", "nasdaq": "", "agree": "Y", "sec_url": "u" if url else "",
                 "verified_at": "x", "notes": "", "sec_candidates": ""})
    return pd.DataFrame(rows)


def test_split_agreement_threshold_is_98_percent_with_reviews_and_sec_urls(ctx):
    _write(ctx.inputs / "special_distributions.csv",
           pd.DataFrame([{"security_id": "d", "ex_date": "2015-01-02", "pct_of_prior": "0.15", "sec_url": "u"}]))
    _write(ctx.inputs / "split_events.csv", _splits(98, 99))
    assert va.check_split_agreement(ctx)["passed"]
    for table in (_splits(96, 99), _splits(98, 99, reviewed=False), _splits(98, 99, url=False)):
        _write(ctx.inputs / "split_events.csv", table)
        assert not va.check_split_agreement(va.Context(inputs=ctx.inputs, cache=ctx.cache, main=ctx.main))["passed"]


# ------------------------------------------------------------------ terminal values and the universe

def _universe(ctx, rows):
    frame = pd.DataFrame(rows)
    for column, value in (("ticker", "T"), ("dv20_rank", 999), ("price_ge_10", "Y")):
        if column not in frame:
            frame[column] = value
    _write(ctx.inputs / "weekly_universe_top300.csv.gz", frame)


def _master(ctx, rows):
    base = {"security_id": "", "cik": "", "first_ticker": "", "name": "X", "share_class": "COMMON", "first_listed": "2010-12-31",
            "last_listed": "", "delist_date": "", "foreign_filer": "N", "multi_class_group": ""}
    _write(ctx.inputs / "security_master.csv", pd.DataFrame([{**base, **r} for r in rows]))


def test_terminal_coverage_needs_a_row_for_every_universe_series_that_ends_early(ctx):
    _universe(ctx, [{"week_end": "2015-01-09", "security_id": "1", "dv50_rank": 10},
                    {"week_end": "2015-01-09", "security_id": "2", "dv50_rank": 20}])
    _panel(ctx, [{"security_id": "1", "date": "2015-01-09"}, {"security_id": "2", "date": "2026-08-31"}])
    _master(ctx, [{"security_id": "1"}, {"security_id": "2"}])
    terminal = pd.DataFrame([{"security_id": "9", "ticker": "Z", "terminal_type": "cash_merger", "status": "computed",
                              "status_note": "", "last_price_date": "2014-01-02"}])
    _write(ctx.inputs / "terminal_returns_2012_2026.csv", terminal)
    out = va.check_terminal_coverage(ctx)
    assert not out["passed"] and out["numbers"]["without_row"] == 1 and out["details"]["without_row"][0]["security_id"] == "1"
    terminal = pd.concat([terminal, pd.DataFrame([{"security_id": "1", "ticker": "A", "terminal_type": "cash_merger",
                                                   "status": "computed", "status_note": "",
                                                   "last_price_date": "2015-01-09"}])], ignore_index=True)
    _write(ctx.inputs / "terminal_returns_2012_2026.csv", terminal)
    out = va.check_terminal_coverage(va.Context(inputs=ctx.inputs, cache=ctx.cache, main=ctx.main))
    assert out["passed"] and out["numbers"]["universe_series_ending_early"] == 1


def test_universe_name_days_cover_the_week_and_the_next_and_stop_at_the_terminal_price(ctx):
    _universe(ctx, [{"week_end": "2015-01-09", "security_id": "1", "dv50_rank": 10},
                    {"week_end": "2015-01-09", "security_id": "2", "dv50_rank": 400}])
    _master(ctx, [{"security_id": "1"}, {"security_id": "2"}])
    _write(ctx.inputs / "terminal_returns_2012_2026.csv",
           pd.DataFrame([{"security_id": "1", "ticker": "A", "terminal_type": "cash_merger", "status": "computed",
                          "status_note": "", "last_price_date": "2015-01-13"}]))
    days = va._universe_name_days(ctx)
    dates = list(ctx.sessions[days["pos"].values].strftime("%Y-%m-%d"))
    # the week ending 2015-01-09 (Monday to Friday) and the next week up to the last price on 2015-01-13
    assert dates == ["2015-01-05", "2015-01-06", "2015-01-07", "2015-01-08", "2015-01-09", "2015-01-12", "2015-01-13"]
    assert set(days["security_id"]) == {"1"}  # rank 400 is not a universe name-week
    _panel(ctx, [{"security_id": "1", "date": d} for d in dates if d != "2015-01-07"])
    out = va.check_universe_vendor_source(ctx)
    assert not out["passed"] and out["numbers"]["missing"] == 1 and out["numbers"]["name_days"] == 7


def test_universe_build_is_no_input_without_the_step12_files(ctx):
    out = va.check_universe_build(ctx)
    assert out["status"] == "no_input" and not out["passed"] and len(out["details"]["missing_inputs"]) == 4


def test_universe_build_sees_sec_submissions_cached_or_fetched_again_after_the_build(ctx):
    import gzip as gz
    from pipelines.reversal_data import universe as un
    directory = ctx.cache / "raw" / "sec" / "submissions"
    directory.mkdir(parents=True, exist_ok=True)
    def write(name, payload):
        (directory / name).write_bytes(gz.compress(json.dumps(payload).encode()))
    write("CIK0000000010.json.gz", {"cik": "10", "filings": {
        "recent": {"form": ["N-2", "40-17G"], "filingDate": ["2012-06-01", "2013-05-01"], "accessionNumber": ["c", "b"]},
        "files": [{"name": "CIK0000000010-submissions-001.json", "filingFrom": "2004-01-01", "filingTo": "2011-12-31"}]}})
    master = pd.DataFrame({"security_id": ["10"], "cik": ["10"], "first_ticker": ["BDC"]})
    _, _, facts = un.investment_companies(master, {"10"}, directory=directory)
    build = ctx.cache / "universe"
    build.mkdir(parents=True, exist_ok=True)
    un.write_csv(build / un.SUBMISSIONS_TABLE, pd.DataFrame(sorted(facts["files"].items()), columns=["name", "sha256"]),
                 compress=True)
    key = f"{un.SUBMISSIONS_DIGEST_KEY} ({directory})"
    (build / "universe_summary.json").write_text(json.dumps({"inputs_sha256": {key: facts["digest"]}}))
    assert va.SUBMISSIONS_DIGEST_KEY == un.SUBMISSIONS_DIGEST_KEY and va.SUBMISSIONS_TABLE == un.SUBMISSIONS_TABLE
    assert va._universe_stale_inputs(ctx) == []
    # The older page that was missing at build time is cached now: the spans may change.
    write("CIK0000000010-submissions-001.json.gz", {"form": ["N-54A"], "filingDate": ["2004-04-21"], "accessionNumber": ["e"]})
    stale = va._universe_stale_inputs(va.Context(inputs=ctx.inputs, cache=ctx.cache, main=ctx.main))
    assert len(stale) == 1 and stale[0]["file"] == key
    assert stale[0]["files"] == [{"name": "CIK0000000010-submissions-001.json.gz", "recorded": "missing", "now": "cached"}]
    # A table edited after the build no longer gives the recorded digest.
    un.write_csv(build / un.SUBMISSIONS_TABLE, pd.DataFrame([["CIK0000000010.json.gz", "0" * 64]], columns=["name", "sha256"]),
                 compress=True)
    stale = va._universe_stale_inputs(va.Context(inputs=ctx.inputs, cache=ctx.cache, main=ctx.main))
    assert stale == [{"file": "research_cache/reversal_2012_2026/universe/" + un.SUBMISSIONS_TABLE,
                      "state": "does not give the digest in inputs_sha256"}]


def test_universe_build_fails_when_the_universe_was_built_from_other_inputs(ctx):
    _write(ctx.inputs / "unfillable.csv", pd.DataFrame([{"security_id": "u"}]))
    upstream = _write(ctx.cache / "reconcile" / "no_series.csv", pd.DataFrame([{"security_id": "1"}]))
    recorded = {"inputs_sha256": {str(common.INPUTS / "unfillable.csv"): common.sha256_file(ctx.inputs / "unfillable.csv"),
                                  str(common.CACHE / "reconcile" / "no_series.csv"): common.sha256_file(upstream),
                                  "tiingo_status (x)": "0" * 64}}
    (ctx.cache / "universe").mkdir(parents=True, exist_ok=True)
    (ctx.cache / "universe" / "universe_summary.json").write_text(json.dumps(recorded))
    assert va._universe_stale_inputs(ctx) == []
    _write(upstream, pd.DataFrame([{"security_id": "2"}]))          # the upstream step ran again after the build
    stale = va._universe_stale_inputs(va.Context(inputs=ctx.inputs, cache=ctx.cache, main=ctx.main))
    assert stale == [{"file": "research_cache/reversal_2012_2026/reconcile/no_series.csv",
                      "state": "changed since the universe build"}]
    _universe(ctx, [{"week_end": "2015-01-09", "security_id": "1", "dv50_rank": 10}])
    out = va.check_universe_build(va.Context(inputs=ctx.inputs, cache=ctx.cache, main=ctx.main))
    assert not out["passed"] and out["numbers"]["built_from_inputs_on_disk"] is False


def test_unfillable_impact_is_judged_per_year_against_250_slots(ctx, monkeypatch):
    weeks = ctx.week_ends[ctx.week_ends.year == 2013]
    frame = pd.DataFrame({"security_id": "u", "week_end": weeks, "vendor": False, "above": True, "outside": False,
                          "proxy_known": True, "dv_above": False, "unknown_size": False})
    monkeypatch.setattr(va, "_proxy_table", lambda c: (frame, "test"))
    _write(ctx.inputs / "unfillable.csv", pd.DataFrame([{"security_id": "u", "needed_start": "2012-01-01",
                                                          "needed_end": "2014-12-31", "est_weeks_in_top250": "52"}]))
    out = va.check_universe_unfillable(ctx)
    assert out["passed"] and out["numbers"]["by_year"][2013]["name_weeks"] == 52   # 52 / 13,000 = 0.4%
    many = pd.concat([frame.assign(security_id=f"u{i}") for i in range(6)])     # 312 / 13,000 = 2.4%
    monkeypatch.setattr(va, "_proxy_table", lambda c: (many, "test"))
    _write(ctx.inputs / "unfillable.csv", pd.DataFrame([{"security_id": f"u{i}", "needed_start": "2012-01-01",
                                                          "needed_end": "2014-12-31", "est_weeks_in_top250": "52"}
                                                         for i in range(6)]))
    out = va.check_universe_unfillable(va.Context(inputs=ctx.inputs, cache=ctx.cache, main=ctx.main))
    assert not out["passed"] and out["numbers"]["years_over_2pct"] == [2013]


def test_universe_name_days_run_four_weeks_and_ignore_an_end_before_a_later_listing(ctx):
    _universe(ctx, [{"week_end": "2015-01-09", "security_id": "1", "dv50_rank": 10},
                    {"week_end": "2015-01-09", "security_id": "2", "dv50_rank": 20}])
    _master(ctx, [{"security_id": "1", "delist_date": "2015-01-13"}, {"security_id": "2", "delist_date": "2015-01-13"}])
    _write(ctx.inputs / "ticker_intervals.csv", pd.DataFrame([
        {"security_id": "1", "ticker": "A", "start": "2010-01-04", "end": "2015-01-13", "exchange": "NASDAQ"},
        {"security_id": "2", "ticker": "B", "start": "2010-01-04", "end": "2015-01-13", "exchange": "NASDAQ"},
        {"security_id": "2", "ticker": "B", "start": "2016-03-01", "end": "2020-01-02", "exchange": "NASDAQ"}]))
    days = va._universe_name_days(ctx)
    dates = {sid: list(ctx.sessions[g["pos"].values].strftime("%Y-%m-%d")) for sid, g in days.groupby("security_id")}
    assert dates["1"][-1] == "2015-01-13"                          # cut at the delisting
    assert dates["2"][0] == "2015-01-05" and dates["2"][-1] == "2015-02-06"   # listed again later: 4 weeks after the week
    assert len(dates["2"]) == 5 * 5 - 1                            # 2015-01-19 is a holiday
    one = va._universe_name_days(ctx, hold_weeks=1)
    assert one.groupby("security_id").size().to_dict() == {"1": 7, "2": 10}


def _proxy_fixture(ctx, extra_rows, week="2015-01-09"):
    """Week ``week``: 51 priced names ranked 200-250 (market cap 1e9) and the unpriced ``extra_rows``."""
    band = [{"week_end": week, "security_id": f"b{i}", "dv50_rank": 200 + i, "mcap": 1e9, "float_usd": np.nan,
             "dv50": 1e7} for i in range(51)]
    rows = band + [{"week_end": week, "dv50_rank": np.nan, "dv50": np.nan, **r} for r in extra_rows]
    listed = pd.DataFrame(rows)
    listed["eligible"] = True
    listed["outside_trading"] = False
    listed["missing_reason"] = np.where(listed["security_id"].str.startswith("b"), "", "not_candidate")
    if "evidence" in listed:
        listed["evidence"] = listed["evidence"].fillna("")
    listed["pf_ge_cut250"] = listed.pop("pf_ge_cut250") if "pf_ge_cut250" in listed else False
    _write(ctx.cache / "universe" / "weekly_listed.csv.gz",
           listed.drop(columns=["mcap", "float_usd", "dv50"]).assign(pf_ge_cut250=listed["pf_ge_cut250"].fillna(False)))
    for column in ("mcap", "float_usd", "dv50"):
        listed[column] = pd.to_numeric(listed[column])
    weekly = listed[["week_end", "security_id", "mcap", "float_usd", "dv50"]].assign(
        week_end=pd.to_datetime(listed["week_end"]))
    (ctx.cache / "prefilter").mkdir(parents=True, exist_ok=True)
    weekly.to_pickle(ctx.cache / "prefilter" / "weekly_metrics.pkl")
    _universe(ctx, [{"week_end": week, "security_id": f"b{i}", "dv50_rank": 200 + i} for i in range(51)])
    _panel(ctx, [{"security_id": f"b{i}", "date": week} for i in range(51)])


def test_proxy_margin_fails_on_an_unpriced_name_of_unknown_size(ctx):
    small = {"security_id": "s", "mcap": 1e6, "float_usd": np.nan}
    by_dv = {"security_id": "d", "mcap": np.nan, "float_usd": np.nan, "dv50": 1e3}
    unknown = {"security_id": "u", "mcap": np.nan, "float_usd": np.nan}
    _proxy_fixture(ctx, [small, by_dv, unknown])
    out = va.check_universe_proxy_margin(ctx)
    n = out["numbers"]
    assert n["plan_rule_met"] and n["max_in_a_week"] == 0       # the plan's count alone would pass
    assert not out["passed"] and n["unpriced_name_weeks_unknown_size"] == 1 and n["unpriced_name_weeks_no_proxy"] == 2
    assert out["details"]["unknown_size"]["names"][0]["security_id"] == "u"
    _write(ctx.inputs / "listing_snapshots_index.csv", pd.DataFrame(columns=["snapshot_date", "source", "as_of_session",
                                                                            "snapshot_file"]))
    _master(ctx, [{"security_id": "s"}])
    _write(ctx.inputs / "ticker_intervals.csv", pd.DataFrame([{"security_id": "s", "ticker": "S", "start": "2010-01-04",
                                                                "end": "2020-01-02", "exchange": "NASDAQ"}]))
    weekly = va.check_universe_capture_coverage(ctx)["numbers"]["weekly_mcap_weighted"]
    assert weekly["unpriced_name_weeks_unknown_size"] == 1 and weekly["unpriced_name_weeks_without_weight"] == 2
    _proxy_fixture(ctx, [small, by_dv])
    out = va.check_universe_proxy_margin(va.Context(inputs=ctx.inputs, cache=ctx.cache, main=ctx.main))
    assert out["passed"] and out["numbers"]["unpriced_name_weeks_unknown_size"] == 0
    _proxy_fixture(ctx, [small, {**by_dv, "dv50": 5e7, "pf_ge_cut250": True}])   # no proxy, step-6 dv at the cut
    out = va.check_universe_proxy_margin(va.Context(inputs=ctx.inputs, cache=ctx.cache, main=ctx.main))
    assert not out["passed"] and out["numbers"]["unpriced_name_weeks_no_proxy_dv_at_or_above_cut250"] == 1


def test_proxy_margin_takes_step12_evidence_classes(ctx):
    rows = [{"security_id": "st", "mcap": np.nan, "float_usd": np.nan, "evidence": "dv"},        # stored-file dv
            {"security_id": "lo", "mcap": np.nan, "float_usd": np.nan, "evidence": "price_lt_10"},
            {"security_id": "uk", "mcap": 1e6, "float_usd": np.nan, "evidence": "unknown"}]      # step 12 says unknown
    _proxy_fixture(ctx, rows)
    out = va.check_universe_proxy_margin(ctx)
    assert not out["passed"] and out["numbers"]["unpriced_name_weeks_unknown_size"] == 1
    assert out["details"]["unknown_size"]["names"][0]["security_id"] == "uk"


def test_proxy_margin_reads_the_step12_unknown_counts(ctx):
    _proxy_fixture(ctx, [{"security_id": "s", "mcap": 1e6, "float_usd": np.nan}])
    summary = pd.DataFrame({"week_end": ["2015-01-09"], "n_missing_unknown_size": [2], "n_unknown_foreign_flag": [5]})
    _write(ctx.inputs / "weekly_universe_summary.csv", summary)
    out = va.check_universe_proxy_margin(ctx)
    assert not out["passed"] and out["numbers"]["step12_unknown_counts"] == {
        "n_missing_unknown_size": {"name_weeks": 2, "weeks_above_zero": 1}}


def test_unfillable_impact_fails_on_weeks_of_unknown_size(ctx):
    _proxy_fixture(ctx, [{"security_id": "u", "mcap": np.nan, "float_usd": np.nan}])
    _write(ctx.inputs / "unfillable.csv", pd.DataFrame([{"security_id": "u", "needed_start": "2015-01-01",
                                                          "needed_end": "2015-12-31", "est_weeks_in_top250": "1",
                                                          "proxy": "none"}]))
    out = va.check_universe_unfillable(ctx)
    n = out["numbers"]
    # the 2% rule reads the model (plan section 0): one unknown week does not fail it, it is reported beside it
    assert out["passed"] and n["unknown_size_name_weeks"] == 1
    assert n["years_over_2pct"] == [] and n["rows_with_proxy_none"] == 1
    year = n["by_year"][2015]
    assert year["unknown_rate"] == va.UNKNOWN_SAMPLE_POOLED_RATE and year["unknown_rate_basis"] == "pooled (year not sampled)"
    assert year["calibrated_name_weeks"] == round(va.UNKNOWN_SAMPLE_POOLED_RATE, 2) and year["upper_name_weeks"] == 1
    assert n["three_estimates"]["name_weeks"]["model"] == 0 and n["three_estimates"]["name_weeks"]["upper"] == 1
    assert n["three_estimates"]["years_over_2pct"] == {"model": [], "calibrated": [], "upper": []}
    assert n["three_estimates"]["calibration_sample"]["matches"] is False        # no sample file in the fixture


def test_unfillable_impact_reports_calibrated_and_upper_beside_the_model(ctx, monkeypatch):
    weeks = ctx.week_ends[ctx.week_ends.year == 2022]
    known = pd.DataFrame({"security_id": "k", "week_end": weeks, "vendor": False, "above": True, "outside": False,
                          "proxy_known": True, "dv_above": False, "unknown_size": False})
    unknown = [known.assign(security_id=f"u{i}", above=False, proxy_known=False, unknown_size=True) for i in range(5)]
    monkeypatch.setattr(va, "_proxy_table", lambda c: (pd.concat([known, *unknown]), "test"))
    _write(ctx.inputs / "unfillable.csv", pd.DataFrame([{"security_id": s, "needed_start": "2022-01-01",
                                                          "needed_end": "2022-12-31", "est_weeks_in_top250": "0"}
                                                         for s in ["k"] + [f"u{i}" for i in range(5)]]))
    out = va.check_universe_unfillable(ctx)
    year = out["numbers"]["by_year"][2022]
    slots = 250 * len(weeks)
    assert year["unknown_rate"] == 0.001898 and year["unknown_size_name_weeks"] == 5 * len(weeks)
    assert year["share"] == round(len(weeks) / slots, 5)                                  # model 0.4%
    assert year["share_upper"] == round(6 * len(weeks) / slots, 5)                         # upper 2.4%
    assert year["share_calibrated"] < 0.005
    assert out["passed"] and out["numbers"]["three_estimates"]["years_over_2pct"] == {
        "model": [], "calibrated": [], "upper": [2022]}


def test_the_validator_unknown_size_rates_equal_step12s():
    from pipelines.reversal_data import universe as un
    assert va.UNKNOWN_SAMPLE_SHA256 == un.UNKNOWN_SAMPLE_SHA256 and va.UNKNOWN_SAMPLE_PATH == un.UNKNOWN_SAMPLE_PATH
    assert va.UNKNOWN_SAMPLE_POOLED_RATE == un.UNKNOWN_SAMPLE_POOLED_RATE
    assert va.UNKNOWN_SAMPLE_RATES == {v["years"]: v["rate"] for v in un.UNKNOWN_SAMPLE_STRATA.values()}
    for year in range(2012, 2027):
        assert va._unknown_rate(year)[0] == un.unknown_rate(year)[0]


def test_listed_gaps_fail_on_series_gaps_and_unknown_reasons_only(ctx):
    weeks = ["2020-04-03", "2020-04-09", "2020-04-17"]
    rows = [{"week_end": w, "security_id": "smci", "ticker": "SMCI", "missing_reason": "series_gap"} for w in weeks]
    rows += [{"week_end": w, "security_id": "tiny", "ticker": "TINY", "missing_reason": "not_candidate"} for w in weeks]
    rows += [{"week_end": "2020-04-03", "security_id": "x", "ticker": "X", "missing_reason": ""}]
    listed = pd.DataFrame(rows).assign(eligible=True, pf_ge_cut250=False)
    listed.loc[0, "pf_ge_cut250"] = True
    _write(ctx.cache / "universe" / "weekly_listed.csv.gz", listed)
    out = va.check_universe_listed_gaps(ctx)
    assert not out["passed"] and out["numbers"]["blocking_name_weeks"] == 3
    span = out["details"]["blocking_spans_by_reason"]["series_gap"][0]
    assert (span["security_id"], span["first"], span["last"], span["weeks"]) == ("smci", "2020-04-03", "2020-04-17", 3)
    assert span["weeks_step6_dv_at_or_above_cut250"] == 1
    listed["missing_reason"] = listed["missing_reason"].replace({"series_gap": "not_candidate"})
    _write(ctx.cache / "universe" / "weekly_listed.csv.gz", listed)
    assert va.check_universe_listed_gaps(va.Context(inputs=ctx.inputs, cache=ctx.cache, main=ctx.main))["passed"]
    listed.loc[1, "missing_reason"] = "something_new"
    _write(ctx.cache / "universe" / "weekly_listed.csv.gz", listed)
    out = va.check_universe_listed_gaps(va.Context(inputs=ctx.inputs, cache=ctx.cache, main=ctx.main))
    assert not out["passed"] and out["numbers"]["unknown_reasons"] == ["something_new"]


def test_short_window_weeks_block_only_when_their_evidence_could_reach_the_top_250(ctx):
    base = {"week_end": "2020-09-04", "ticker": "", "missing_reason": "short_window", "proxy_above": False}
    rows = [{**base, "security_id": "kdp", "evidence": "dv", "pf_ge_cut250": True},      # step-6 dv at the cut
            {**base, "security_id": "prx", "evidence": "proxy", "proxy_above": True},   # proxy alone at the median
            {**base, "security_id": "unk", "evidence": "unknown"},
            {**base, "security_id": "sml", "evidence": "dv"},                            # dv under the cut
            {**base, "security_id": "low", "evidence": "price_lt_10"},
            {**base, "security_id": "pbl", "evidence": "proxy"}]                         # proxy under the median
    listed = pd.DataFrame(rows).assign(eligible=True)
    listed["pf_ge_cut250"] = listed["pf_ge_cut250"].fillna(False).astype(bool)
    _write(ctx.cache / "universe" / "weekly_listed.csv.gz", listed)
    out = va.check_universe_listed_gaps(ctx)
    n = out["numbers"]
    assert not out["passed"] and n["blocking_by_reason"] == {"short_window_top250_evidence": 3}
    assert n["short_window"]["blocking_by_kind"] == {"step6_dv_ge_cut250": 1, "proxy_only_at_band_median": 1, "unknown": 1,
                                                     "own_dv20_rank_le300": 0, "own_dv20_rank_le300_only": 0}
    assert n["short_window"]["judged_small_not_blocking"] == 3 and n["short_window"]["name_weeks"] == 6
    assert {r["security_id"] for r in out["details"]["short_window_judged_small"]} == {"sml", "low", "pbl"}
    _write(ctx.cache / "universe" / "weekly_listed.csv.gz", listed[listed["security_id"].isin(["sml", "low", "pbl"])])
    assert va.check_universe_listed_gaps(va.Context(inputs=ctx.inputs, cache=ctx.cache, main=ctx.main))["passed"]


def test_a_short_window_week_whose_own_dv20_rank_is_near_the_top_250_blocks(ctx):
    # Round-10 merge review: APA 2020-07-02 ranked 101st on its own canonical dv20 with step-6 dv under the cut
    # and a proxy under the median; it was judged small. Its own window now blocks it (rank 300 with margin).
    base = {"week_end": "2020-07-02", "ticker": "", "missing_reason": "short_window", "proxy_above": False,
            "pf_ge_cut250": False, "evidence": "proxy"}
    rows = [{**base, "security_id": "apa", "dv20_rank": 100, "dv20_rank_any_price": 101},
            {**base, "security_id": "chk", "dv20_rank": np.nan, "dv20_rank_any_price": 300},   # at the margin
            {**base, "security_id": "sml", "dv20_rank": 301, "dv20_rank_any_price": 320},
            {**base, "security_id": "nor", "dv20_rank": np.nan, "dv20_rank_any_price": np.nan}]
    _write(ctx.cache / "universe" / "weekly_listed.csv.gz", pd.DataFrame(rows).assign(eligible=True))
    out = va.check_universe_listed_gaps(ctx)
    n = out["numbers"]["short_window"]
    assert not out["passed"] and out["numbers"]["blocking_name_weeks"] == 2
    assert n["blocking_by_kind"]["own_dv20_rank_le300"] == 2 and n["blocking_by_kind"]["own_dv20_rank_le300_only"] == 2
    assert n["judged_small_not_blocking"] == 2 and n["own_dv20_rank_limit"] == 300
    assert {r["security_id"] for r in out["details"]["short_window_judged_small"]} == {"sml", "nor"}
    # step 12 reads the same rows the same way
    from pipelines.reversal_data import universe as un
    frame = pd.DataFrame(rows).assign(pf_dv_ok=False, pf_price_low=False)
    assert un.blocks_week(frame).tolist() == va.short_window_blocks(frame).tolist() == [True, True, False, False]
    # the own rank counts only for short_window weeks in step 12
    assert not un.blocks_week(frame.assign(missing_reason="series_gap", evidence="dv")).any()


def test_the_validator_knows_every_missing_reason_step12_emits():
    from pipelines.reversal_data import universe as un
    assert set(un.MISSING_REASONS) <= va.KNOWN_MISSING
    assert {"answer_not_in_panel", "series_gap", "fetched_pending_reconcile"} <= va.BLOCKING_MISSING
    assert va.SHORT_WINDOW in un.MISSING_REASONS and va.SHORT_WINDOW not in va.BLOCKING_MISSING   # judged by evidence
    assert set(va.PENDING_SOURCES) | {"fetched_pending_reconcile"} == un.PENDING_REASONS


def _gap_fixture(ctx, rows, terminal=(), candidates=(), master=()):
    listed = pd.DataFrame(rows).assign(eligible=True, pf_ge_cut250=False)
    _write(ctx.cache / "universe" / "weekly_listed.csv.gz", listed)
    _master(ctx, list(master) or [{"security_id": s} for s in sorted(set(listed["security_id"]))])
    _write(ctx.inputs / "terminal_returns_2012_2026.csv", pd.DataFrame(
        list(terminal), columns=["security_id", "ticker", "terminal_type", "status", "last_price_date"]))
    _write(ctx.inputs / "candidate_fetch_list.csv", pd.DataFrame(
        list(candidates), columns=["security_id", "ticker_for_source", "reason", "planned_source", "status",
                                   "needed_start", "needed_end"]))
    _panel(ctx, [{"security_id": "zz", "date": "2015-01-02"}])


def test_listed_gaps_stop_at_a_merger_last_price_but_keep_a_blank_one_blocking(ctx):
    rows = [{"week_end": "2024-07-26", "security_id": "hibb", "ticker": "HIBB", "missing_reason": "series_gap"},
            {"week_end": "2024-08-02", "security_id": "hibb", "ticker": "HIBB", "missing_reason": "series_gap"},
            {"week_end": "2024-08-02", "security_id": "amed", "ticker": "AMED", "missing_reason": "series_gap"}]
    terminal = [{"security_id": "hibb", "ticker": "HIBB", "terminal_type": "cash_merger", "status": "computed",
                 "last_price_date": "2024-07-24"},
                {"security_id": "amed", "ticker": "AMED", "terminal_type": "cash_merger", "status": "computed",
                 "last_price_date": ""}]
    _gap_fixture(ctx, rows, terminal)
    out = va.check_universe_listed_gaps(ctx)
    n = out["numbers"]
    # HIBB's week of its last price still blocks; the week after it does not; AMED (no last price) blocks.
    assert n["blocking_name_weeks"] == 2 and n["after_series_end_not_blocking"] == 1
    assert out["details"]["after_series_end"][0]["week_end"] == "2024-08-02"
    assert {s["security_id"] for s in out["details"]["blocking_spans_by_reason"]["series_gap"]} == {"hibb", "amed"}


def test_listed_gaps_block_pending_weeks_without_an_open_candidate_row_of_that_source(ctx):
    weeks = ["2020-04-03", "2024-06-07"]
    rows = [{"week_end": w, "security_id": "smci", "ticker": "SMCI", "missing_reason": "yahoo_pending"} for w in weeks]
    rows += [{"week_end": "2024-06-07", "security_id": "corz", "ticker": "CORZ", "missing_reason": "yahoo_pending"}]
    candidates = [{"security_id": "smci", "ticker_for_source": "SMCI", "reason": "B_A_float_ge_1B", "planned_source": "tiingo",
                   "status": "done", "needed_start": "2018-01-21", "needed_end": "2020-02-25"},
                  {"security_id": "corz", "ticker_for_source": "CORZ", "reason": "Y_active_rank300", "planned_source": "yahoo",
                   "status": "pending", "needed_start": "2023-12-15", "needed_end": "2026-08-31"}]
    _gap_fixture(ctx, rows, candidates=candidates)
    out = va.check_universe_listed_gaps(ctx)
    n = out["numbers"]
    # SMCI has no Yahoo row at all (only a Tiingo row of 2018-2020): both weeks block; CORZ's Yahoo row is open.
    assert not out["passed"] and n["pending_without_open_candidate"] == 2 and n["blocking_name_weeks"] == 2
    assert set(out["details"]["blocking_spans_by_reason"]) == {"yahoo_pending_without_open_candidate"}
    candidates.append({"security_id": "smci", "ticker_for_source": "SMCI", "reason": "Y_active_rank300",
                       "planned_source": "yahoo", "status": "pending", "needed_start": "2019-11-01", "needed_end": "2026-08-31"})
    _gap_fixture(ctx, rows, candidates=candidates)
    assert va.check_universe_listed_gaps(va.Context(inputs=ctx.inputs, cache=ctx.cache, main=ctx.main))["passed"]


def test_a_need_only_partly_in_unfillable_csv_stays_open_and_backs_its_pending_weeks(ctx):
    rows = [{"week_end": "2022-04-01", "security_id": "hofv", "ticker": "HOFV", "missing_reason": "tiingo_pending"}]
    candidates = [{"security_id": "hofv", "ticker_for_source": "HOFV", "reason": "S_stored_only_delisted_rank300",
                   "planned_source": "tiingo", "status": "pending", "needed_start": "2020-04-18",
                   "needed_end": "2025-06-30"}]
    _gap_fixture(ctx, rows, candidates=candidates)
    _write(ctx.inputs / "unfillable.csv", pd.DataFrame([{"security_id": "hofv", "needed_start": "2020-04-18",
                                                          "needed_end": "2020-06-30"}]))
    out = va.check_universe_listed_gaps(ctx)
    assert out["passed"] and out["numbers"]["pending_without_open_candidate"] == 0
    resolved = va.check_candidates_resolved(ctx)["numbers"]
    assert resolved["open"] == 1 and resolved["documented_unfillable"] == 0
    _write(ctx.inputs / "unfillable.csv", pd.DataFrame([{"security_id": "hofv", "needed_start": "2020-04-18",
                                                          "needed_end": "2025-06-30"}]))
    fresh = va.Context(inputs=ctx.inputs, cache=ctx.cache, main=ctx.main)
    assert va.check_candidates_resolved(fresh)["numbers"]["documented_unfillable"] == 1
    assert not va.check_universe_listed_gaps(fresh)["passed"]     # a documented need backs no pending week


def test_answer_not_in_panel_blocks_as_a_known_reason(ctx):
    _gap_fixture(ctx, [{"week_end": "2021-06-04", "security_id": "chrd", "ticker": "CHRD",
                        "missing_reason": "answer_not_in_panel"}])
    out = va.check_universe_listed_gaps(ctx)
    assert not out["passed"] and out["numbers"]["unknown_reasons"] == []
    assert out["numbers"]["blocking_by_reason"] == {"answer_not_in_panel": 1}


def test_a_stale_market_cap_does_not_outweigh_a_float_or_dollar_volume_at_the_cut(ctx):
    stale = {"security_id": "ino", "mcap": 2.4e8, "float_usd": 4.2e9}            # band median float 4e9 (below)
    lazr = {"security_id": "lazr", "mcap": 5e8, "float_usd": np.nan, "dv50": 5e7, "pf_ge_cut250": True}
    rows = [stale, lazr]
    week = "2015-01-09"
    band = [{"week_end": week, "security_id": f"b{i}", "dv50_rank": 200 + i, "mcap": 1e9, "float_usd": 4e9,
             "dv50": 1e7} for i in range(51)]
    _proxy_fixture(ctx, [])
    listed = pd.DataFrame(band + [{"week_end": week, "dv50_rank": np.nan, "dv50": np.nan, **r} for r in rows])
    listed["eligible"], listed["outside_trading"] = True, False
    listed["missing_reason"] = np.where(listed["security_id"].str.startswith("b"), "", "unfillable")
    listed["pf_ge_cut250"] = listed["pf_ge_cut250"].fillna(False)
    _write(ctx.cache / "universe" / "weekly_listed.csv.gz", listed.drop(columns=["dv50"]))
    out = va.check_universe_proxy_margin(ctx)
    n = out["numbers"]
    assert n["plan_definition"]["name_weeks"] == 0               # the plan's market-cap-first rule sees neither
    assert n["name_weeks"] == 1 and n["name_weeks_float_at_median_mcap_below"] == 1      # INO by its float
    assert n["unpriced_name_weeks_dv_at_or_above_cut250_proxy_below"] == 1 and not out["passed"]   # LAZR by dv
    _write(ctx.inputs / "unfillable.csv", pd.DataFrame([{"security_id": s, "needed_start": "2015-01-01",
                                                          "needed_end": "2015-12-31", "est_weeks_in_top250": "0",
                                                          "proxy": "mcap"} for s in ("ino", "lazr")]))
    u = va.check_universe_unfillable(va.Context(inputs=ctx.inputs, cache=ctx.cache, main=ctx.main))["numbers"]
    assert u["estimated_name_weeks"] == 2 and u["estimated_name_weeks_plan_proxy_rule"] == 0


def test_unfillable_check_reports_the_step12_class_cap_beside_the_plan_count(ctx):
    # QRTEB-shaped: a class carrying the company's float above the band median; step 12 caps it by its own
    # dollar volume. The plan's count still counts it (pass / fail unchanged); the capped view leaves it out.
    week = "2015-01-09"
    _proxy_fixture(ctx, [])
    band = [{"week_end": week, "security_id": f"b{i}", "dv50_rank": 200 + i, "float_usd": 4e9} for i in range(51)]
    rows = [{"week_end": week, "security_id": "qrteb", "float_usd": 9e9, "proxy_class_capped": True},
            {"week_end": week, "security_id": "solo", "float_usd": 9e9, "proxy_class_capped": False}]
    listed = pd.DataFrame(band + rows)
    listed["eligible"], listed["outside_trading"], listed["pf_ge_cut250"] = True, False, False
    listed["proxy_class_capped"] = listed["proxy_class_capped"].fillna(False).astype(bool)
    listed["missing_reason"] = np.where(listed["security_id"].str.startswith("b"), "", "unfillable")
    _write(ctx.cache / "universe" / "weekly_listed.csv.gz", listed)
    _write(ctx.inputs / "unfillable.csv", pd.DataFrame([{"security_id": s, "needed_start": "2015-01-01",
                                                          "needed_end": "2015-12-31", "est_weeks_in_top250": "1",
                                                          "proxy": "float"} for s in ("qrteb", "solo")]))
    u = va.check_universe_unfillable(va.Context(inputs=ctx.inputs, cache=ctx.cache, main=ctx.main))["numbers"]
    assert u["estimated_name_weeks"] == 2
    cap = u["step12_class_capped"]
    assert cap["recorded"] and cap["estimated_name_weeks_class_capped"] == 1 and cap["by_security"] == {"qrteb": 1}
    assert cap["share_by_year_without_capped"][2015] == va._share(1, u["by_year"][2015]["slots"])
    assert u["by_year"][2015]["share"] == va._share(2, u["by_year"][2015]["slots"])
    # An older build without the column: nothing capped.
    _write(ctx.cache / "universe" / "weekly_listed.csv.gz", listed.drop(columns="proxy_class_capped"))
    u = va.check_universe_unfillable(va.Context(inputs=ctx.inputs, cache=ctx.cache, main=ctx.main))["numbers"]
    assert not u["step12_class_capped"]["recorded"] and u["step12_class_capped"]["estimated_name_weeks_class_capped"] == 0


def test_a_universe_build_in_another_directory_is_read_and_the_summary_goes_to_out_dir(ctx, tmp_path):
    build, out = tmp_path / "build", tmp_path / "out"
    build.mkdir()
    _write(build / "weekly_listed.csv.gz", pd.DataFrame([{"week_end": "2015-01-09", "security_id": "x", "eligible": True,
                                                         "missing_reason": "series_gap", "ticker": "X"}]))
    _write(build / "weekly_universe_top300.csv.gz", pd.DataFrame([{"week_end": "2015-01-09", "security_id": "x",
                                                                  "ticker": "X", "dv50_rank": 1, "dv20_rank": 1,
                                                                  "price_ge_10": "Y"}]))
    _write(build / "investment_company_spans.csv", pd.DataFrame([{"security_id": "bdc", "cik": "1", "ticker": "B",
                                                                 "start": "2004-01-01", "end": "2099-12-31"}]))
    other = va.Context(inputs=ctx.inputs, cache=ctx.cache, main=ctx.main, universe_dir=build, out_dir=out)
    assert other.input_path("weekly_universe_top300.csv.gz") == build / "weekly_universe_top300.csv.gz"
    assert other.input_path("security_master.csv") == ctx.inputs / "security_master.csv"
    assert other.cache_path("universe/weekly_listed.csv.gz") == build / "weekly_listed.csv.gz"
    assert "official" in other.universe[1] and other.listed[0]["security_id"].tolist() == ["x"]
    assert other.investment_company_on("bdc", "2015-01-09") and not other.investment_company_on("x", "2015-01-09")
    assert other.key(build / "weekly_listed.csv.gz") == va.CACHE_KEY_PREFIX + "universe/weekly_listed.csv.gz"
    assert other.key(build / "weekly_universe_top300.csv.gz") == "weekly_universe_top300.csv.gz"
    out.mkdir()
    va.write_validation_summary(other, [va.result("x", "d", "p", "t", True)])
    assert (out / "validation_summary.json").exists() and not (ctx.inputs / "validation_summary.json").exists()


def test_a_candidate_whose_need_lies_in_an_investment_company_span_is_not_open(ctx):
    _write(ctx.inputs / "candidate_fetch_list.csv", pd.DataFrame([
        {"security_id": "acas", "ticker_for_source": "ACAS", "reason": "A1", "planned_source": "tiingo", "status": "pending",
         "needed_start": "2012-01-01", "needed_end": "2016-12-31"},
        {"security_id": "op", "ticker_for_source": "OP", "reason": "A1", "planned_source": "tiingo", "status": "pending",
         "needed_start": "2012-01-01", "needed_end": "2016-12-31"}]))
    _panel(ctx, [{"security_id": "zz", "date": "2015-01-02"}])
    _write(ctx.cache / "universe" / "investment_company_spans.csv", pd.DataFrame([
        {"security_id": "acas", "cik": "1", "ticker": "ACAS", "start": "1997-08-27", "end": "2017-01-02"}]))
    out = va.check_candidates_resolved(ctx)
    assert out["numbers"]["open"] == 1 and out["numbers"]["investment_company_need"] == 1
    assert out["details"]["open"][0]["security_id"] == "op"


def _fetch_fixture(ctx, fetched: dict, ranks: dict):
    """Candidates b (B-B), c (B-C sample), r (B-C rest); ``fetched`` maps id -> fetch status, ``ranks`` id -> dv50 rank."""
    _write(ctx.inputs / "candidate_fetch_list.csv", pd.DataFrame([
        {"security_id": "b", "ticker_for_source": "BB", "reason": va.TIER_BB, "status": "pending", "planned_source": "tiingo"},
        {"security_id": "c", "ticker_for_source": "CC", "reason": va.TIER_BC_SAMPLE, "status": "pending",
         "planned_source": "tiingo"},
        {"security_id": "r", "ticker_for_source": "RR", "reason": va.TIER_BC_REST, "status": "conditional_tier_c",
         "planned_source": "tiingo"}]))
    _write(ctx.cache / "tiingo" / "fetch_status.csv", pd.DataFrame(
        [{"security_id": k, "reason": "x", "status": v, "prices_path": "", "raw_path": ""} for k, v in fetched.items()]
        or [{"security_id": "", "reason": "", "status": "", "prices_path": "", "raw_path": ""}]))
    _panel(ctx, [{"security_id": k, "date": "2019-01-04", "src_primary": "tiingo"} for k, v in fetched.items()
                 if v == "done"] + [{"security_id": "z", "date": "2019-01-04"}])
    _universe(ctx, [{"week_end": "2019-01-04", "security_id": k, "dv50_rank": v} for k, v in ranks.items()]
              or [{"week_end": "2019-01-04", "security_id": "z", "dv50_rank": 1}])


def test_fetch_margin_waits_until_the_b_b_and_b_c_sample_names_are_fetched_and_in_the_panel(ctx):
    _fetch_fixture(ctx, {}, {})
    out = va.check_universe_fetch_margin(ctx)
    assert out["status"] == "no_input" and not out["passed"] and out["numbers"]["required_pending"] == 2
    _fetch_fixture(ctx, {"b": "done", "c": "done"}, {})
    (ctx.cache / "prices" / "daily_panel.csv.gz").unlink()
    _panel(ctx, [{"security_id": "b", "date": "2019-01-04", "src_primary": "tiingo"}])   # c fetched, step 9 not rerun
    out = va.check_universe_fetch_margin(va.Context(inputs=ctx.inputs, cache=ctx.cache, main=ctx.main))
    assert out["status"] == "no_input" and out["details"]["pending"] == [
        {"security_id": "c", "ticker_for_source": "CC", "reason": va.TIER_BC_SAMPLE, "state": "fetched_not_in_panel"}]


def test_fetch_margin_flags_a_tight_screen_and_the_tier_c_rule(ctx):
    _fetch_fixture(ctx, {"b": "done", "c": "done"}, {"b": 240, "c": 280})
    out = va.check_universe_fetch_margin(ctx)
    n = out["numbers"]
    assert out["status"] == "fail" and n["tier_b_b_or_b_c_names_in_top250"] == 1
    assert n["tier_c_rule_triggered"] and n["tier_c_rest_open"] == 1
    assert "lower the screen" in out["details"]["action"] and "tier C" in out["details"]["action"]
    _fetch_fixture(ctx, {"b": "done", "c": "no_data"}, {"b": 270})
    out = va.check_universe_fetch_margin(va.Context(inputs=ctx.inputs, cache=ctx.cache, main=ctx.main))
    assert out["passed"] and not out["numbers"]["tier_c_rule_triggered"]


def _stored_fixture(ctx, flags_b):
    rows = [{"security_id": "1", "date": "2025-06-24", "flags": "stored_excluded"},
            {"security_id": "2", "date": "2025-06-24", "flags": flags_b},
            {"security_id": "1", "date": "2024-03-01", "div_cash": 0.5, "flags": "stored_excluded"},
            {"security_id": "1", "date": "2016-03-01", "div_cash": 0.5}]
    _panel(ctx, rows)
    _write(ctx.inputs / "ticker_intervals.csv", pd.DataFrame([
        {"security_id": "1", "ticker": "AAA", "start": "2010-01-04", "end": "2026-08-31", "exchange": "NASDAQ"},
        {"security_id": "2", "ticker": "BBB", "start": "2010-01-04", "end": "2026-08-31", "exchange": "NASDAQ"},
        {"security_id": "3", "ticker": "CCC", "start": "2010-01-04", "end": "2026-08-31", "exchange": "NASDAQ"}]))
    split = _splits(1, 1).iloc[:1].assign(security_id="1", ticker="AAA", ex_date="2025-06-24", event_type="unit_break")
    _write(ctx.inputs / "split_events.csv", split)
    (ctx.cache / "prefilter").mkdir(parents=True, exist_ok=True)
    (ctx.cache / "prefilter" / "prefilter_summary.json").write_text(
        json.dumps({"stored_break_files_2025_06_24": ["AAA", "bbb", "CCC", "ZZZ"]}))


def test_stored_comparison_needs_every_priced_break_file_excluded_or_a_unit_break(ctx):
    _stored_fixture(ctx, "")
    out = va.check_stored_comparison(ctx)
    n = out["numbers"]
    assert not out["passed"] and n["break_list_priced"] == 2 and n["break_list_priced_exceptions"] == 1
    assert out["details"]["exceptions"][0]["security_id"] == "2"
    assert n["break_list_not_priced_that_day"] == 1 and n["break_list_unmapped"] == 1   # CCC has no row; ZZZ no holder
    _stored_fixture(ctx, "stored_excluded")
    out = va.check_stored_comparison(va.Context(inputs=ctx.inputs, cache=ctx.cache, main=ctx.main))
    assert out["passed"] and out["details"]["priced_without_unit_break_row"] == [
        {"security_id": "2", "files": ["bbb"], "stored_excluded": True, "reconcile_state": "not in break_days"}]
    assert not out["numbers"]["reconcile_break_days_read"] and out["numbers"]["without_unit_break_row_unexplained"] == 1


def _break_day_states():
    return {"1": {"summary": {"ticker_last": "AAA", "break_days": [
                {"date": "2025-06-24", "state": "unit_break", "stored_r": -0.5, "tr": 0.01, "stored_implied_k": 2.02}]}},
            "2": {"summary": {"ticker_last": "BBB", "break_days": [
                {"date": "2025-06-24", "state": "stored_moves_with_vendors", "stored_r": 1.5, "tr": 1.5,
                 "stored_implied_k": 1.0}]}}}


def _check_with_break_days(ctx, break_days):
    _stored_fixture(ctx, "stored_excluded")
    (ctx.cache / "reconcile").mkdir(parents=True, exist_ok=True)
    (ctx.cache / "reconcile" / "summary.json").write_text(json.dumps({"break_days": break_days}))
    return va.check_stored_comparison(va.Context(inputs=ctx.inputs, cache=ctx.cache, main=ctx.main))


def test_stored_comparison_reads_step9_break_days_to_explain_a_real_move_without_a_unit_break(ctx):
    # The table is written by step 9's own code, so the two sides change together ({how_to_read, days: {...}}).
    from pipelines.reversal_data import reconcile as rc
    table = rc.break_day_table(_break_day_states())
    assert "days" in table and "2025-06-24" not in table
    out = _check_with_break_days(ctx, table)
    n = out["numbers"]
    assert out["passed"] and n["reconcile_break_days_read"] and n["without_unit_break_row_explained_by_reconcile"] == 1
    assert n["without_unit_break_row_unexplained"] == 0 and n["reconcile_break_day_listed"] == 2
    assert n["reconcile_break_day_states"] == {"unit_break": 1, "stored_moves_with_vendors": 1}
    assert out["details"]["priced_without_unit_break_row"][0]["reconcile_state"] == "stored_moves_with_vendors"
    # Only the state is carried over: no return of the day reaches the summary.
    assert "stored_r" not in json.dumps(out) and '"tr"' not in json.dumps(out) and "implied_k" not in json.dumps(out)


def test_stored_comparison_still_reads_the_older_break_days_layout_without_a_days_layer(ctx):
    from pipelines.reversal_data import reconcile as rc
    legacy = rc.break_day_table(_break_day_states())["days"]       # the 10:35 dry run: days at the top level
    out = _check_with_break_days(ctx, legacy)
    n = out["numbers"]
    assert out["passed"] and n["without_unit_break_row_explained_by_reconcile"] == 1
    assert n["reconcile_break_day_states"] == {"unit_break": 1, "stored_moves_with_vendors": 1}
    # A table without the day (or an odd shape) explains nothing and lists the name as unexplained.
    for odd in ({"how_to_read": {}, "days": {}}, {"days": {"2026-06-29": {}}}, [], "x"):
        out = _check_with_break_days(ctx, odd)
        assert out["numbers"]["without_unit_break_row_unexplained"] == 1
        assert out["numbers"]["reconcile_break_day_states"] == {}


def _plan_value(name, column):
    values = va.PLAN_VALUES.get((name, column))
    if values:
        return sorted(values[0])[0]
    prefixes = va.PLAN_VALUE_PREFIXES.get((name, column))
    return prefixes[0][0] + "x" if prefixes else "v"


def _plan_files(ctx):
    for name, columns in va.PLAN_INPUTS.items():
        _write(ctx.inputs / name, pd.DataFrame([{c: _plan_value(name, c) for c in columns}]))
    for relative, columns in {**va.PLAN_CACHE, **va.PLAN_CACHE_INFRA}.items():
        frame = pd.DataFrame([{c: "1" for c in columns if (relative, c) not in va.PLAN_COLUMN_MAPPINGS}])
        _write(ctx.cache / relative, frame)
    _write(ctx.cache / "prices" / "1.csv", pd.DataFrame([{c: "1" for c in va.PRICE_COLUMNS}]))


def test_plan_files_need_every_planned_file_column_and_value(ctx):
    _plan_files(ctx)
    out = va.check_plan_files(ctx)
    assert out["passed"], out["details"]
    assert {(m["file"], m["column"]) for m in out["details"]["documented_mappings"]} >= {
        ("research_cache/reversal_2012_2026/raw_index.csv.gz", "path")}
    (ctx.inputs / "exchange_moves.csv").unlink()
    events = pd.read_csv(ctx.inputs / "earnings_events.csv").drop(columns=["first_in_fiscal_quarter"])
    _write(ctx.inputs / "earnings_events.csv", events)
    _write(ctx.inputs / "unfillable.csv", pd.read_csv(ctx.inputs / "unfillable.csv").assign(proxy="none"))
    (ctx.cache / "dividends.csv").unlink()
    _write(ctx.cache / "prices" / "2.csv", pd.DataFrame([{"date": "2015-01-02", "close_raw": 1}]))
    out = va.check_plan_files(va.Context(inputs=ctx.inputs, cache=ctx.cache, main=ctx.main))
    d = out["details"]
    assert not out["passed"]
    assert d["files_missing"] == ["exchange_moves.csv", "research_cache/reversal_2012_2026/dividends.csv"]
    assert d["columns_missing"] == [{"file": "earnings_events.csv", "column": "first_in_fiscal_quarter"}]
    assert d["values_outside_plan"] == [{"file": "unfillable.csv", "column": "proxy", "value": "none", "rows": 1}]
    assert d["price_files_missing_columns"] == ["2.csv"]
    coverage = va.check_manifest_coverage(va.Context(inputs=ctx.inputs, cache=ctx.cache, main=ctx.main))
    assert not coverage["passed"] and coverage["status"] == "fail"
    assert {"exchange_moves.csv", "research_cache/reversal_2012_2026/dividends.csv"} <= set(coverage["details"]["missing"])


# ------------------------------------------------------------------ the run, the summary and the manifest

def test_a_check_that_raises_is_reported_as_an_error_and_the_run_goes_on(ctx, monkeypatch):
    def broken(c):
        raise ValueError("boom")

    broken.__name__ = "check_broken"
    monkeypatch.setattr(va, "CHECKS", [broken, va.check_review_queue])
    results = va.run_checks(ctx)
    assert [r["status"] for r in results] == ["error", "no_input"]
    assert "boom" in results[0]["details"]["error"] and not results[0]["passed"]


def _manifest_fixture(ctx):
    _write(ctx.inputs / "split_events.csv", _splits(1, 1))
    _write(ctx.cache / "prices" / "daily_panel.csv.gz", pd.DataFrame({"security_id": ["1"], "date": ["2015-01-02"]}))
    _write(ctx.cache / "factors" / "ff5_2x3_daily.csv", _factor_rows(["2015-01-02"], ["RF"]))
    (ctx.cache / "raw" / "kf" / "a.zip").write_bytes(b"zip")
    index_line = "2026-10-01T00:00:00+00:00,yahoo,https://query1.finance.yahoo.com/v8/finance/chart/AAA,200,10,ab,x\n"
    (ctx.cache / "raw_index.csv.gz").write_bytes(gzip.compress(
        ("fetched_utc,source,url_redacted,http_status,bytes,sha256,cache_path\n" + index_line).encode()))


def test_summary_and_manifest_hash_every_file_with_relative_keys_and_hold_no_key(ctx, monkeypatch):
    _manifest_fixture(ctx)
    monkeypatch.setattr(va, "secret_values", lambda: ["sk_fake_secret_value_123"])
    results = [va.check_review_queue(ctx), va.check_split_agreement(ctx)]
    summary, manifest = va.write_outputs(ctx, results)
    assert summary["counts"] == {"no_input": 2}
    on_disk = json.loads((ctx.inputs / "manifest.json").read_text())
    assert on_disk == json.loads(json.dumps(manifest))
    files = on_disk["files"]
    assert files["split_events.csv"]["sha256"] == common.sha256_file(ctx.inputs / "split_events.csv")
    assert files["validation_summary.json"]["sha256"] == common.sha256_file(ctx.inputs / "validation_summary.json")
    panel_key = "research_cache/reversal_2012_2026/prices/daily_panel.csv.gz"
    assert panel_key in files and "research_cache/reversal_2012_2026/raw/kf/a.zip" in files
    assert not any(k.startswith("/") for k in files)                           # relative keys only (plan 1.1)
    assert on_disk["sha256"] == {k: v["sha256"] for k, v in files.items()}     # the plan's sha256{relative_path} map
    assert "manifest.json" not in files
    for key in va.MANIFEST_PLAN_KEYS + ("scripts", "files", "validation", "validation_inputs"):
        assert key in on_disk
    assert on_disk["sources"]["yahoo"]["requests"] == 1 and on_disk["sources"]["yahoo"]["endpoint_template"]
    assert on_disk["raw_index_sha256"] == common.sha256_file(ctx.cache / "raw_index.csv.gz")
    assert any("weekly_listed.csv.gz" in m for m in on_disk["files_missing"])   # the universe files are not in the fixture
    assert "split_events.csv" in summary["inputs_read"]                         # what the checks read, with its hash
    # a key value that would appear: nothing is written, the earlier files stay as they were
    before = {n: (ctx.inputs / n).read_bytes() for n in ("manifest.json", "validation_summary.json")}
    monkeypatch.setattr(va, "secret_values", lambda: ["yahoo.com/v8"])
    with pytest.raises(RuntimeError):
        va.write_outputs(va.Context(inputs=ctx.inputs, cache=ctx.cache, main=ctx.main), results)
    assert before == {n: (ctx.inputs / n).read_bytes() for n in before}


def test_an_input_that_changes_during_the_run_blocks_both_files_and_a_live_file_does_not(ctx, monkeypatch):
    _manifest_fixture(ctx)
    monkeypatch.setattr(va, "secret_values", lambda: [])
    for path in va.files_for_test(ctx):          # as main does: hash the test's files before the checks
        if path.exists():
            ctx.sha(path)
    results = [va.check_split_agreement(ctx)]
    va.source_facts(ctx)                         # reads the live raw_index once
    with gzip.open(ctx.cache / "raw_index.csv.gz", "at") as handle:   # the Tiingo run logs another request
        handle.write("2026-10-02T00:00:00+00:00,tiingo,https://api.tiingo.com/x,200,5,cd,y\n")
    summary, _ = va.write_outputs(ctx, results)
    assert [c["file"] for c in summary["live_inputs_moved_on"]] == ["research_cache/reversal_2012_2026/raw_index.csv.gz"]
    written = {n: (ctx.inputs / n).read_bytes() for n in ("manifest.json", "validation_summary.json")}
    fresh = va.Context(inputs=ctx.inputs, cache=ctx.cache, main=ctx.main)
    for path in va.files_for_test(fresh):
        if path.exists() and path.name != "validation_summary.json":
            fresh.sha(path)
    results = [va.check_split_agreement(fresh)]
    _write(ctx.inputs / "split_events.csv", _splits(1, 2))        # an upstream step rewrites a file the check read
    with pytest.raises(va.InputsChanged):
        va.write_outputs(fresh, results)
    assert written == {n: (ctx.inputs / n).read_bytes() for n in written}
    again = va.Context(inputs=ctx.inputs, cache=ctx.cache, main=ctx.main)
    again.sha(ctx.cache / "factors" / "ff5_2x3_daily.csv")              # read by no check, hashed at the start
    _write(ctx.cache / "factors" / "ff5_2x3_daily.csv", _factor_rows(["2015-01-05"], ["RF"]))
    assert again.input_changes()["blocking"]


# ------------------------------------------------------------------ the built outputs (skipped until built)

BUILT_SUMMARY = common.INPUTS / "validation_summary.json"
BUILT_MANIFEST = common.INPUTS / "manifest.json"


@pytest.mark.skipif(not BUILT_SUMMARY.exists(), reason="validation_summary.json not built yet")
def test_built_summary_reports_every_check_with_its_threshold_and_numbers():
    summary = json.loads(BUILT_SUMMARY.read_text())
    names = [c["check"] for c in summary["checks"]]
    assert names == [va.check_name(f) for f in va.CHECKS]
    for check in summary["checks"]:
        assert check["status"] in ("pass", "fail", "no_input", "error") and check["threshold"]
        assert check["passed"] == (check["status"] == "pass")
    assert set(summary["passed"]) | set(summary["failed"]) == set(names)
    assert summary["inputs_read"] and all(len(v) == 64 for v in summary["inputs_read"].values())


@pytest.mark.skipif(not BUILT_MANIFEST.exists(), reason="manifest.json not built yet")
def test_built_manifest_lists_every_input_file_and_the_test_files_from_the_cache():
    manifest = json.loads(BUILT_MANIFEST.read_text())
    inputs = {p.name for p in common.INPUTS.glob("*") if p.is_file() and p.name not in ("manifest.json",)
              and not p.name.endswith(".tmp")}
    assert inputs <= set(manifest["files"])
    assert va.CACHE_KEY_PREFIX + "prices/daily_panel.csv.gz" in manifest["files"]
    for name in va.FACTOR_FILES:
        assert va.CACHE_KEY_PREFIX + "factors/" + name in manifest["files"]
    assert not any(k.startswith("/") for k in manifest["files"])
    assert manifest["sha256"] == {k: v["sha256"] for k, v in manifest["files"].items()}
    assert manifest["scripts_git_commit"] and manifest["raw_index_sha256"]
    summary = json.loads(BUILT_SUMMARY.read_text())
    assert manifest["files"]["validation_summary.json"]["sha256"] == common.sha256_file(BUILT_SUMMARY)
    for key, digest in summary["inputs_read"].items():   # the same hash wherever a file appears
        assert manifest["sha256"].get(key, manifest["validation_inputs"].get(key)) == digest
    text = BUILT_MANIFEST.read_text()
    assert "api_key=" not in text.replace("api_key=REDACTED", "")
