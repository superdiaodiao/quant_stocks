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

from scripts import reversal_data_common as common
from scripts import reversal_data_validate as va

SCRIPT = Path("scripts/reversal_data_validate.py")


# ------------------------------------------------------------------ what the validator may import and call

def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            names |= {f"{base}.{alias.name}" if base == "scripts" else base for alias in node.names}
    return names


def test_the_validator_imports_no_signal_backtest_or_research_module():
    allowed_third_party = {"numpy", "pandas", "exchange_calendars", "scripts.reversal_data_common"}
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
    assert len(calls) == 1  # only the final step builds the manifest


def test_every_check_is_one_function_named_like_its_result():
    names = [va.check_name(f) for f in va.CHECKS]
    assert len(names) == len(set(names)) == 33
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


def test_unfillable_impact_is_judged_per_year_against_250_slots(ctx, monkeypatch):
    weeks = ctx.week_ends[ctx.week_ends.year == 2013]
    frame = pd.DataFrame({"security_id": "u", "week_end": weeks, "vendor": False, "above": True, "outside": False})
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


# ------------------------------------------------------------------ the run, the summary and the manifest

def test_a_check_that_raises_is_reported_as_an_error_and_the_run_goes_on(ctx, monkeypatch):
    def broken(c):
        raise ValueError("boom")

    broken.__name__ = "check_broken"
    monkeypatch.setattr(va, "CHECKS", [broken, va.check_review_queue])
    results = va.run_checks(ctx)
    assert [r["status"] for r in results] == ["error", "no_input"]
    assert "boom" in results[0]["details"]["error"] and not results[0]["passed"]


def test_summary_and_manifest_hash_every_file_and_hold_no_key(ctx, monkeypatch, tmp_path):
    _write(ctx.inputs / "split_events.csv", _splits(1, 1))
    for name in ("daily_panel.csv.gz",):
        _write(ctx.cache / "prices" / name, pd.DataFrame({"security_id": ["1"], "date": ["2015-01-02"]}))
    _write(ctx.cache / "factors" / "ff5_2x3_daily.csv", _factor_rows(["2015-01-02"], ["RF"]))
    (ctx.cache / "raw" / "kf" / "a.zip").write_bytes(b"zip")
    index_line = "2026-10-01T00:00:00+00:00,yahoo,https://query1.finance.yahoo.com/v8/finance/chart/AAA,200,10,ab,x\n"
    (ctx.cache / "raw_index.csv.gz").write_bytes(gzip.compress(
        ("fetched_utc,source,url_redacted,http_status,bytes,sha256,cache_path\n" + index_line).encode()))
    monkeypatch.setattr(common, "INPUTS", ctx.inputs)
    monkeypatch.setattr(va, "secret_values", lambda: ["sk_fake_secret_value_123"])
    summary = va.write_validation_summary(ctx, [va.check_review_queue(ctx)])
    assert (ctx.inputs / "validation_summary.json").exists() and summary["counts"] == {"no_input": 1}
    manifest = va.build_manifest(ctx, summary)
    files = manifest["files"]
    assert files["split_events.csv"]["sha256"] == common.sha256_file(ctx.inputs / "split_events.csv")
    assert files["validation_summary.json"]["sha256"] == common.sha256_file(ctx.inputs / "validation_summary.json")
    assert str(ctx.cache / "prices" / "daily_panel.csv.gz") in files and str(ctx.cache / "raw" / "kf" / "a.zip") in files
    assert "manifest.json" not in files
    on_disk = json.loads((ctx.inputs / "manifest.json").read_text())
    for key in ("generated_utc", "scripts_git_commit", "scripts", "sources", "raw_index_sha256", "files", "validation"):
        assert key in on_disk
    assert on_disk["sources"]["yahoo"]["requests"] == 1 and on_disk["sources"]["yahoo"]["endpoint_template"]
    assert any("weekly_listed.csv.gz" in m for m in on_disk["files_missing"])   # the universe files are not in the fixture
    monkeypatch.setattr(va, "secret_values", lambda: ["yahoo.com/v8"])           # a "key" that does appear
    with pytest.raises(RuntimeError):
        va.build_manifest(ctx, summary)
    assert json.loads((ctx.inputs / "manifest.json").read_text())["files"]    # the earlier manifest is left as it was


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


@pytest.mark.skipif(not BUILT_MANIFEST.exists(), reason="manifest.json not built yet")
def test_built_manifest_lists_every_input_file_and_the_test_files_from_the_cache():
    manifest = json.loads(BUILT_MANIFEST.read_text())
    inputs = {p.name for p in common.INPUTS.glob("*") if p.is_file() and p.name != "manifest.json"}
    assert inputs <= set(manifest["files"])
    assert str(common.CACHE / "prices" / "daily_panel.csv.gz") in manifest["files"]
    for name in va.FACTOR_FILES:
        assert str(common.CACHE / "factors" / name) in manifest["files"]
    assert manifest["scripts_git_commit"] and manifest["raw_index_sha256"]
    text = BUILT_MANIFEST.read_text()
    assert "api_key=" not in text.replace("api_key=REDACTED", "")
