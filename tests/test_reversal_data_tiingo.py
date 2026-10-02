import gzip
import io
import json
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError

import numpy as np
import pandas as pd
import pytest

from scripts import reversal_data_common as common
from scripts import reversal_data_tiingo as tiingo

SECRET = "TESTKEY0123456789"
CANDIDATE_COLUMNS = ["security_id", "ticker_for_source", "needed_start", "needed_end", "reason", "planned_source",
                     "status", "fetch_month", "best_rank", "tiingo_row_end", "tiingo_row_start", "tiingo_flags",
                     "tiingo_range_match", "fetch_order"]


def candidate_rows(rows):
    return pd.DataFrame([dict(zip(CANDIDATE_COLUMNS, r)) for r in rows], columns=CANDIDATE_COLUMNS)


def write_candidates(path, rows):
    candidate_rows(rows).to_csv(path, index=False)
    return path


def tiingo_rows(dates, close=10.0, volume=1000, split=None, div=None):
    """Tiingo-shaped rows whose adjClose follows the identity."""
    split = split or {}
    div = div or {}
    rows, adj = [], 1.0
    previous = None
    for k, day in enumerate(dates):
        c = close if np.isscalar(close) else close[k]
        s, d = split.get(day, 1.0), div.get(day, 0.0)
        if previous is not None:
            adj *= (c * s + d) / previous
        previous = c
        rows.append({"date": f"{day}T00:00:00.000Z", "close": c, "high": c, "low": c, "open": c, "volume": volume,
                     "adjClose": adj, "adjHigh": adj, "adjLow": adj, "adjOpen": adj, "adjVolume": volume,
                     "divCash": d, "splitFactor": s})
    return rows


SESSIONS = pd.bdate_range("2018-01-01", "2018-12-31")


# ------------------------------------------------------------------ URL, key, order, status

def test_url_never_carries_the_key_and_uses_tiingo_ticker_form(monkeypatch):
    monkeypatch.setattr(common, "read_env_key", lambda path, name: SECRET)
    headers = tiingo.auth_headers()
    url = tiingo.price_url("BRK.B")
    assert headers["Authorization"] == f"Token {SECRET}"
    assert SECRET not in url and "token" not in url.lower()
    assert url == "https://api.tiingo.com/tiingo/daily/brk-b/prices?startDate=2011-06-01&endDate=2026-08-31"


def test_tier_order_then_liquidity(tmp_path):
    path = write_candidates(tmp_path / "c.csv", [
        ("1", "VVV", "2014-01-01", "2026-08-31", "V_verify_sample", "tiingo", "pending", "2026-10", "5"),
        ("2", "SMP", "2019-01-01", "2020-01-01", "B_C_sample_300M_500M", "tiingo", "pending", "2026-10", "1"),
        ("3", "BB1", "2019-01-01", "2020-01-01", "B_B_float_500M_1B", "tiingo", "pending", "2026-10", "9"),
        ("4", "AA2", "2012-01-01", "2014-01-01", "A2_mcap_rank400", "tiingo", "pending", "2026-10", "3"),
        ("5", "AA1", "2012-01-01", "2014-01-01", "A1_wiki_dv_rank300", "tiingo", "pending", "2026-10", "300"),
        ("6", "BA2", "2019-01-01", "2020-01-01", "B_A_float_ge_1B", "tiingo", "pending", "2026-10", "80"),
        ("7", "BA1", "2019-01-01", "2020-01-01", "B_A_float_ge_1B", "tiingo", "pending", "2026-10", "40"),
        ("8", "CCC", "2019-01-01", "2020-01-01", "C_late_start", "tiingo", "pending", "2026-10", ""),
        ("9", "YYY", "2019-01-01", "2020-01-01", "Y_active_rank300", "yahoo", "pending", "2026-10", "1"),
    ])
    candidates = tiingo.load_candidates(path)
    assert "YYY" not in set(candidates["ticker_for_source"])
    rows = tiingo.select_rows(candidates, tiingo.load_status(tmp_path / "none.csv"))
    # Without the prefilter's fetch_order: its reason priority (the tier-C sample before V), then rank.
    assert tiingo.ticker_order(rows) == ["BA1", "BA2", "AA1", "AA2", "BB1", "CCC", "SMP", "VVV"]


def test_the_prefilters_fetch_order_wins(tmp_path):
    path = write_candidates(tmp_path / "c.csv", [
        ("1", "VVV", "2014-01-01", "2026-08-31", "V_verify_sample", "tiingo", "pending", "2026-10", "5", "", "", "",
         "Y", "1"),
        ("2", "BA1", "2019-01-01", "2020-01-01", "B_A_float_ge_1B", "tiingo", "pending", "2026-10", "40", "", "", "",
         "Y", "2"),
        ("3", "SSS", "2024-07-01", "2025-08-01", "S_stored_only_delisted_rank300", "tiingo", "pending", "2026-10", "8",
         "", "", "", "Y", "3")])
    rows = tiingo.select_rows(tiingo.load_candidates(path), tiingo.load_status(tmp_path / "none.csv"))
    assert tiingo.ticker_order(rows) == ["VVV", "BA1", "SSS"]


def test_a_ticker_shared_by_rows_is_asked_once_at_its_best_place(tmp_path):
    path = write_candidates(tmp_path / "c.csv", [
        ("1", "XYZ", "2014-01-01", "2026-08-31", "V_verify_sample", "tiingo", "pending", "2026-10", "5"),
        ("2", "ABC", "2019-01-01", "2020-01-01", "B_A_float_ge_1B", "tiingo", "pending", "2026-10", "50"),
        ("3", "XYZ", "2018-01-01", "2020-01-01", "B_A_float_ge_1B", "tiingo", "pending", "2026-10", "60"),
    ])
    rows = tiingo.select_rows(tiingo.load_candidates(path), tiingo.load_status(tmp_path / "none.csv"))
    assert tiingo.ticker_order(rows) == ["ABC", "XYZ"]


def test_selection_skips_final_rows_month2_rows_and_retries_deferred(tmp_path):
    path = write_candidates(tmp_path / "c.csv", [
        ("1", "AAA", "2019-01-01", "2020-01-01", "B_A_float_ge_1B", "tiingo", "pending", "2026-10", "1"),
        ("2", "BBB", "2019-01-01", "2020-01-01", "B_A_float_ge_1B", "tiingo", "pending", "2026-10", "2"),
        ("3", "CCC", "2019-01-01", "2020-01-01", "B_A_float_ge_1B", "tiingo", "pending", "2026-10", "3"),
        ("4", "SSS", "2024-07-01", "2025-01-01", "S_stored_only_delisted_rank300", "tiingo", "pending_month2",
         "2026-11", "4"),
        ("5", "TTT", "2019-01-01", "2020-01-01", "B_C_rest_300M_500M", "tiingo", "conditional_tier_c", "2026-11", "5"),
    ])
    candidates = tiingo.load_candidates(path)
    status = tiingo.merge_status(tiingo.load_status(tmp_path / "none.csv"), [
        {"security_id": "1", "ticker_for_source": "AAA", "needed_start": "2019-01-01", "needed_end": "2020-01-01",
         "status": "done", "order": 1},
        {"security_id": "2", "ticker_for_source": "BBB", "needed_start": "2019-01-01", "needed_end": "2020-01-01",
         "status": "deferred_quota", "order": 2},
    ])
    assert tiingo.ticker_order(tiingo.select_rows(candidates, status)) == ["BBB", "CCC"]
    month2 = tiingo.select_rows(candidates, status, {"pending_month2", "conditional_tier_c"})
    assert tiingo.ticker_order(month2) == ["BBB", "SSS", "TTT"]
    only = tiingo.select_rows(candidates, status, {"pending"}, {"B_A_float_ge_1B"})
    assert set(only["ticker_for_source"]) == {"BBB", "CCC"}


def test_status_merge_replaces_rows_of_the_same_key(tmp_path):
    key = {"security_id": "1", "ticker_for_source": "AAA", "needed_start": "2019-01-01", "needed_end": "2020-01-01"}
    status = tiingo.merge_status(tiingo.load_status(tmp_path / "none.csv"), [{**key, "status": "error", "order": 1}])
    status = tiingo.merge_status(status, [{**key, "status": "done", "order": 1},
                                          {**key, "security_id": "2", "status": "no_data", "order": 2}])
    assert list(status["status"]) == ["done", "no_data"]
    tiingo.write_status(status, tmp_path / "s.csv")
    assert list(tiingo.load_status(tmp_path / "s.csv")["status"]) == ["done", "no_data"]


# ------------------------------------------------------------------ response and fields

def test_error_bodies_are_recognised_and_quota_text_detected():
    rows, error = tiingo.parse_body(json.dumps(tiingo_rows(["2018-01-02"])).encode())
    assert len(rows) == 1 and error == ""
    rows, error = tiingo.parse_body(b'{"detail": "Error: You have run over your 500 symbol look up for this month."}')
    assert rows is None and tiingo.is_quota_text(error)
    rows, error = tiingo.parse_body(b'{"detail": "Error: Ticker \'ZZZZ\' not found"}')
    assert rows is None and not tiingo.is_quota_text(error)
    assert tiingo.parse_body(b"<html>")[0] is None


def test_field_summary_reports_events_and_the_adjclose_identity():
    days = [d.strftime("%Y-%m-%d") for d in SESSIONS[:30]]
    prices = tiingo_rows(days, close=[10.0] * 10 + [5.0] * 20, split={days[10]: 2.0}, div={days[20]: 0.1})
    summary = tiingo.field_summary(prices, tiingo.to_frame(prices))
    assert summary["fields_missing"] == "" and summary["rows_total"] == 30
    assert summary["split_events"] == f"{days[10]}:2" and summary["div_events"] == 1
    assert float(summary["adj_identity_max_err"]) < 1e-12 and summary["adj_identity_bad_rows"] == 0
    prices[25]["adjClose"] *= 1.01
    assert tiingo.field_summary(prices, tiingo.to_frame(prices))["adj_identity_bad_rows"] == 2
    del prices[0]["divCash"]
    assert "divCash" in tiingo.field_summary(prices, tiingo.to_frame(prices))["fields_missing"]


# ------------------------------------------------------------------ entity check

def frame_for(days, close=10.0, volume=1000):
    return tiingo.to_frame(tiingo_rows([d.strftime("%Y-%m-%d") for d in days], close=close, volume=volume))


def reference(days, dv):
    return pd.DataFrame({"date": pd.DatetimeIndex(days), "dv": dv, "src": "stored"})


def quotes(days, last_sale, check="confirmed"):
    return pd.DataFrame({"as_of_session": pd.DatetimeIndex(days), "last_sale": last_sale, "as_of_check": check})


def test_entity_check_ok_when_dollar_volume_and_lastsale_agree():
    days = SESSIONS[(SESSIONS >= "2018-02-01") & (SESSIONS <= "2018-06-29")]
    check = tiingo.entity_check(frame_for(days), "2018-02-01", "2018-07-10", SESSIONS,
                                reference(days, 10_000.0 * 0.9), quotes(days[::20], 10.1))
    assert check["entity_check"] == "ok", check["entity_notes"]
    assert check["covers_end"] == "Y" and check["need_coverage"] == 1.0  # the end slack is not missing
    assert tiingo.row_status(check) == "done"


def test_entity_check_fails_another_company_by_dollar_volume_or_level():
    days = SESSIONS[(SESSIONS >= "2018-02-01") & (SESSIONS <= "2018-06-29")]
    by_dv = tiingo.entity_check(frame_for(days), "2018-02-01", "2018-06-29", SESSIONS, reference(days, 1e6))
    assert by_dv["entity_check"] == "fail" and tiingo.row_status(by_dv) == "wrong_entity"
    by_level = tiingo.entity_check(frame_for(days), "2018-02-01", "2018-06-29", SESSIONS, None,
                                   quotes(days[::20], 31.0))
    assert by_level["entity_check"] == "fail" and "LastSale" in by_level["entity_notes"]
    loose = tiingo.entity_check(frame_for(days), "2018-02-01", "2018-06-29", SESSIONS, None,
                                quotes(days[::20], 10.4, check="unverified"))
    assert loose["entity_check"] == "ok"  # 3.8% off is inside the 5% band of an unverified as-of session
    loose_ok = tiingo.entity_check(frame_for(days), "2018-02-01", "2018-06-29", SESSIONS, None,
                                   quotes(days[::20], 10.4, check="confirmed"))
    assert loose_ok["entity_check"] == "fail"


def test_entity_check_flags_gaps_jumps_partial_cover_and_no_rows():
    days = SESSIONS[(SESSIONS >= "2018-02-01") & (SESSIONS <= "2018-06-29")]
    holed = days[:40].append(days[60:])
    close = [10.0] * 40 + [30.0] * (len(days) - 60)
    check = tiingo.entity_check(frame_for(holed, close=close), "2018-02-01", "2018-06-29", SESSIONS,
                                reference(holed, 10_000.0 * np.array([1.0] * 40 + [3.0] * (len(days) - 60))))
    assert check["max_gap_sessions"] == 20 and check["gap_jumps"] == 1
    assert tiingo.row_status(check) == "partial"  # 20 of the needed sessions are missing
    late = tiingo.entity_check(frame_for(days[50:]), "2018-02-01", "2018-06-29", SESSIONS, reference(days, 10_000.0))
    assert late["covers_start"] == "N" and tiingo.row_status(late) == "partial"
    none = tiingo.entity_check(frame_for(days), "2019-01-01", "2019-06-01", SESSIONS)
    assert none["entity_check"] == "fail" and none["rows_in_need"] == 0
    bare = tiingo.entity_check(frame_for(days), "2018-02-01", "2018-06-29", SESSIONS)
    assert bare["entity_check"] == "review" and tiingo.row_status(bare) == "done_review"


# ------------------------------------------------------------------ ledger and limiter

def write_ledger(path, rows):
    lines = ["fetched_utc,source,month,symbol,status"] + [",".join(r) for r in rows]
    path.write_text("\n".join(lines) + "\n")


def test_limiter_is_seeded_from_the_ledger_so_a_restart_keeps_the_hourly_window(tmp_path):
    now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    stamp = lambda minutes: (now - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    rows = [(stamp(5 + k), "tiingo", "2026-10", f"S{k}", "200") for k in range(45)]
    rows += [(stamp(60 * 30), "tiingo", "2026-09", "OLD", "200"), (stamp(1), "sec", "2026-10", "X", "200")]
    write_ledger(tmp_path / "ledger.csv", rows)
    seed = tiingo.ledger_rows(path=tmp_path / "ledger.csv")
    assert len(seed) == 46  # the SEC row is not Tiingo's
    limiter = tiingo.make_limiter(45, 900, seed, now=now)
    assert len(limiter.stamps) == 45  # the 30-hour-old request is outside the daily window
    assert tiingo.limiter_delay(limiter) > 600  # the oldest of the 45 is 50 minutes old
    assert tiingo.limiter_delay(tiingo.make_limiter(45, 900, seed.iloc[:10], now=now)) == 0
    assert tiingo.symbols_this_month("2026-10", path=tmp_path / "ledger.csv") == {f"S{k}" for k in range(45)}
    assert tiingo.rolling_symbols(30, path=tmp_path / "ledger.csv", now=now) == 46


def test_limits_above_the_free_tier_are_refused():
    with pytest.raises(SystemExit):
        tiingo.main(["--hourly", "50"])
    # round 7: the month's last symbols may be spent on purpose, up to the free tier's 500, never past it
    with pytest.raises(SystemExit):
        tiingo.main(["--month-stop", "501"])
    assert tiingo.MONTH_LIMIT == 500 and tiingo.MONTH_STOP == 480


def test_month2_selects_the_yahoo_fallbacks_budget_deferrals_and_the_tier_c_rest(tmp_path):
    # Round 7: the prefilter's Yahoo fallbacks were 'deferred_quota' rows this fetcher never deferred itself, so
    # neither the default run nor 'pending_month2,conditional_tier_c' selected them (DISCB, WOLF, LMCB, VRM ...).
    path = write_candidates(tmp_path / "c.csv", [
        ("1", "AAA", "2019-01-01", "2020-01-01", "A2_mcap_rank400", "tiingo", "pending_month2", "2026-11", "1"),
        ("2", "BBB", "2019-01-01", "2020-01-01", "B_A_float_ge_1B", "tiingo", "deferred_quota", "2026-11", "2"),
        ("3", "CCC", "2019-01-01", "2020-01-01", "B_C_rest_300M_500M", "tiingo", "conditional_tier_c", "2026-11", "3"),
        ("4", "DDD", "2019-01-01", "2020-01-01", "B_C_rest_300M_500M", "tiingo", "tier_c_not_triggered", "", "4"),
        ("5", "EEE", "2019-01-01", "2020-01-01", "B_A_float_ge_1B", "tiingo", "pending", "2026-10", "5"),
        ("6", "FFF", "2019-01-01", "2020-01-01", "B_A_float_ge_1B", "tiingo", "pending", "2026-10", "6"),
    ])
    candidates = tiingo.load_candidates(path)
    status = tiingo.merge_status(tiingo.load_status(tmp_path / "none.csv"), [
        {"security_id": "6", "ticker_for_source": "FFF", "needed_start": "2019-01-01", "needed_end": "2020-01-01",
         "status": "done", "order": 6}])
    assert set(tiingo.MONTH_2_STATUSES) == {"pending", "deferred_quota", "pending_month2", "conditional_tier_c"}
    month2 = tiingo.select_rows(candidates, status, set(tiingo.MONTH_2_STATUSES))
    assert set(tiingo.ticker_order(month2)) == {"AAA", "BBB", "CCC", "EEE"}
    # the old documented statuses miss the budget's own deferrals
    old = tiingo.select_rows(candidates, status, {"pending_month2", "conditional_tier_c"})
    assert "BBB" not in set(old["ticker_for_source"])


def test_month2_flag_sets_the_month2_statuses(sandbox, capsys):
    assert run(sandbox, "--dry-run", "--month2", "--month-stop", "500") == 0
    out = capsys.readouterr().out
    # CCC's pending_month2 row is selected with its month-1 row (one ticker); the yahoo row never
    assert "CCC" in out and "YYY" not in out and "stop at 500" in out


@pytest.mark.skipif(not (tiingo.CANDIDATES.exists() and (tiingo.PREFILTER / "tiingo_month2_plan.csv").exists()),
                    reason="step 6 outputs not built")
def test_built_month2_plan_rows_marked_selectable_are_what_month2_selects():
    plan = pd.read_csv(tiingo.PREFILTER / "tiingo_month2_plan.csv", dtype=str, keep_default_na=False)
    if "fetch_selectable" not in plan:
        pytest.skip("month-2 plan built before round 7")
    candidates = tiingo.load_candidates(tiingo.CANDIDATES)
    status = tiingo.load_status()
    selected = tiingo.select_rows(candidates, status, set(tiingo.MONTH_2_STATUSES))
    keys = {tiingo.row_key(r) for _, r in selected.iterrows()}
    # (rows answered since the plan was built are final now, so no run selects them again)
    final = {tiingo.row_key(r) for _, r in status.iterrows() if r["status"] in tiingo.FINAL}
    wanted = plan[plan["fetch_selectable"].isin(["Y", "fetch_shadowed_only"])]
    assert {tiingo.row_key(r) for _, r in wanted.iterrows()} - final <= keys
    # the Yahoo fallbacks are all selectable (or asked only on purpose: WOLF)
    fallback = plan[plan["group"] == "yahoo_fallback"]
    assert set(fallback["fetch_selectable"]) <= {"Y", "fetch_shadowed_only"}


# ------------------------------------------------------------------ the run, with a fake network

class FakeResponse:
    def __init__(self, body):
        self.body, self.status = body, 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Every path in tmp_path, the key faked, and a network that answers from ``answers``."""
    monkeypatch.setattr(common, "QUOTA_LEDGER", tmp_path / "quota_ledger.csv")
    monkeypatch.setattr(common, "RAW_INDEX", tmp_path / "raw_index.csv.gz")
    for name in ("RAW_DIR", "OUT_DIR", "PRICES_DIR"):
        monkeypatch.setattr(tiingo, name, tmp_path / name.lower())
    for name, file in (("STATUS", "fetch_status.csv"), ("SUMMARY", "fetch_summary.json"), ("LOCK", "fetch.lock")):
        monkeypatch.setattr(tiingo, name, tmp_path / "out_dir" / file)
    monkeypatch.setattr(tiingo, "PREFILTER", tmp_path / "prefilter")
    monkeypatch.setattr(tiingo, "load_references", lambda ids, directory=None: ({}, {}))
    monkeypatch.setattr(tiingo, "xnas_sessions", lambda start=None, end=None: pd.bdate_range("2011-06-01", "2026-08-31"))
    monkeypatch.setattr(common, "read_env_key", lambda path, name: SECRET)
    monkeypatch.setattr(common.time, "sleep", lambda seconds: None)
    state = {"answers": {}, "urls": [], "auth": []}

    def fake_urlopen(request, timeout=None):
        url = request.full_url
        state["urls"].append(url)
        state["auth"].append(request.get_header("Authorization"))
        ticker = url.split("/daily/")[1].split("/")[0].upper()
        answer = state["answers"].get(ticker, 404)
        if isinstance(answer, int):
            raise HTTPError(url, answer, "error", {}, io.BytesIO(b'{"detail": "Error: refused"}'))
        return FakeResponse(answer)

    monkeypatch.setattr(common, "urlopen", fake_urlopen)
    days = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2018-01-02", "2019-12-31")]
    state["good"] = json.dumps(tiingo_rows(days)).encode()
    state["candidates"] = write_candidates(tmp_path / "candidates.csv", [
        ("1", "AAA", "2018-01-21", "2019-12-31", "B_A_float_ge_1B", "tiingo", "pending", "2026-10", "10"),
        ("2", "BBB", "2018-01-21", "2019-12-31", "B_A_float_ge_1B", "tiingo", "pending", "2026-10", "20"),
        ("3", "CCC", "2018-01-21", "2019-12-31", "A1_wiki_dv_rank300", "tiingo", "pending", "2026-10", "5"),
        ("4", "CCC", "2018-06-01", "2019-06-01", "S_stored_only_delisted_rank300", "tiingo", "pending_month2",
         "2026-11", "50"),
        ("5", "YYY", "2018-01-21", "2019-12-31", "Y_active_rank300", "yahoo", "pending", "2026-10", "1"),
    ])
    state["tmp"] = tmp_path
    return state


def run(sandbox, *extra):
    already = [] if "--already-used" in extra or "--dry-run" in extra or "--offline" in extra else ["--already-used", "0"]
    return tiingo.main(["--candidates", str(sandbox["candidates"]), "--spacing", "0", *already, *extra])


def test_trial_limit_fetches_the_top_tickers_once_and_writes_only_the_status_file(sandbox, capsys):
    sandbox["answers"] = {"AAA": sandbox["good"], "BBB": sandbox["good"], "CCC": sandbox["good"]}
    before = sandbox["candidates"].read_bytes()
    assert run(sandbox, "--limit", "2") == 0
    assert [u.split("/daily/")[1].split("/")[0] for u in sandbox["urls"]] == ["aaa", "bbb"]
    assert set(sandbox["auth"]) == {f"Token {SECRET}"}
    status = tiingo.load_status()
    assert list(status["ticker_for_source"]) == ["AAA", "BBB"] and set(status["status"]) == {"done_review"}
    assert sandbox["candidates"].read_bytes() == before  # the candidate list is never rewritten
    assert (tiingo.PRICES_DIR / "AAA.csv.gz").exists() and not tiingo.LOCK.exists()
    assert common.quota_used("tiingo", unique_symbols=True) == 2
    # the rest: only CCC is asked; its month-2 row is served by the same answer
    assert run(sandbox) == 0
    assert len(sandbox["urls"]) == 3
    status = tiingo.load_status()
    assert sorted(status.loc[status["ticker_for_source"] == "CCC", "security_id"]) == ["3", "4"]
    # nothing is asked again (a recheck re-reads the cache), and the key is in no output, log or status file
    assert run(sandbox) == 0 and len(sandbox["urls"]) == 3
    assert run(sandbox, "--offline", "--recheck") == 0 and len(sandbox["urls"]) == 3
    assert len(tiingo.load_status()) == 4 and json.loads(tiingo.SUMMARY.read_text())["tickers_processed"] == ["AAA", "BBB", "CCC"]
    text = capsys.readouterr().out + common.QUOTA_LEDGER.read_text() + gzip.open(common.RAW_INDEX, "rt").read()
    text += tiingo.STATUS.read_text() + tiingo.SUMMARY.read_text()
    assert SECRET not in text


def test_monthly_symbol_stop_defers_the_rest(sandbox):
    sandbox["answers"] = {"AAA": sandbox["good"], "BBB": sandbox["good"], "CCC": sandbox["good"]}
    assert run(sandbox, "--already-used", "479") == 0
    assert len(sandbox["urls"]) == 1  # 479 + AAA = 480: stop before BBB
    status = tiingo.load_status().set_index("ticker_for_source")["status"]
    assert status["AAA"] == "done_review" and status["BBB"] == "deferred_quota" and status["CCC"] == "deferred_quota"
    assert "monthly symbol stop" in json.loads(tiingo.SUMMARY.read_text())["stop"]
    assert run(sandbox) == 0 and len(sandbox["urls"]) == 3  # next month (or a higher budget) picks them up


def test_the_last_symbols_of_a_month_stop_at_500_and_each_run_keeps_its_summary(sandbox):
    # Round 7: the month's last 20 symbols (480 used) are spent with --month-stop 500, never past it.
    sandbox["answers"] = {"AAA": sandbox["good"], "BBB": sandbox["good"], "CCC": sandbox["good"]}
    assert run(sandbox, "--already-used", "498", "--month-stop", "500") == 0
    assert len(sandbox["urls"]) == 2  # 498 + AAA + BBB = 500: stop before CCC
    status = tiingo.load_status().set_index("ticker_for_source")["status"]
    assert status["CCC"] == "deferred_quota"
    runs = sorted((tiingo.SUMMARY.parent / "runs").glob("fetch_summary_*Z.json"))
    assert len(runs) == 1 and json.loads(runs[0].read_text())["stop"].startswith("monthly symbol stop: 500")


def test_429_and_limit_bodies_stop_the_run_and_404_is_no_data(sandbox):
    sandbox["answers"] = {"AAA": 404, "BBB": 429, "CCC": sandbox["good"]}
    assert run(sandbox) == 0
    status = tiingo.load_status().set_index("ticker_for_source")["status"]
    assert status["AAA"] == "no_data" and status["BBB"] == "deferred_quota" and status["CCC"] == "deferred_quota"
    assert len(sandbox["urls"]) == 2
    sandbox["answers"]["BBB"] = b'{"detail": "Error: You have run over your hourly request allocation."}'
    assert run(sandbox) == 0
    assert tiingo.load_status().set_index("ticker_for_source")["status"]["BBB"] == "deferred_quota"
    assert not tiingo.raw_path("BBB").exists()  # the error body is set aside, not kept as the answer
    assert list(tiingo.RAW_DIR.glob("BBB__*.error-*"))
    sandbox["answers"]["BBB"] = sandbox["good"]
    assert run(sandbox) == 0
    status = tiingo.load_status().set_index("ticker_for_source")["status"]
    assert status["BBB"] == "done_review" and set(status["CCC"]) == {"done_review"}


def test_auth_error_stops_without_deferring(sandbox):
    sandbox["answers"] = {"AAA": 401, "BBB": sandbox["good"]}
    assert run(sandbox) == 1
    status = tiingo.load_status()
    assert list(status["ticker_for_source"]) == ["AAA"] and list(status["status"]) == ["error"]
    assert len(sandbox["urls"]) == 1


def test_dry_run_and_offline_make_no_request(sandbox):
    assert run(sandbox, "--dry-run") == 0
    assert run(sandbox, "--offline") == 0
    assert sandbox["urls"] == [] and len(tiingo.load_status()) == 0


def test_cached_tickers_after_a_stop_are_still_checked_without_a_request(sandbox):
    sandbox["answers"] = {"AAA": sandbox["good"], "BBB": sandbox["good"]}
    common.atomic_write(tiingo.raw_path("CCC"), gzip.compress(sandbox["good"], mtime=0))
    assert run(sandbox, "--already-used", "479") == 0
    status = tiingo.load_status()
    by = status.groupby("ticker_for_source")["status"].agg(set)
    assert by["AAA"] == {"done_review"} and by["BBB"] == {"deferred_quota"} and by["CCC"] == {"done_review"}
    assert len(sandbox["urls"]) == 1
    summary = json.loads(tiingo.SUMMARY.read_text())
    assert summary["tickers_deferred"] == ["BBB"] and summary["tickers_processed"] == ["AAA", "CCC"]


def test_entity_check_falls_back_to_the_whole_overlap_when_the_window_has_no_reference():
    early = SESSIONS[(SESSIONS >= "2018-01-02") & (SESSIONS <= "2018-03-30")]
    later = SESSIONS[(SESSIONS >= "2018-04-02") & (SESSIONS <= "2018-09-28")]
    frame = frame_for(early.append(later))
    same = tiingo.entity_check(frame, "2018-04-02", "2018-09-28", SESSIONS, reference(early, 10_000.0))
    assert same["entity_check"] == "review" and same["dv_ratio_median"] == "" and same["dv_ratio_median_all"] == 1.0
    assert "outside" not in same["entity_notes"]
    other = tiingo.entity_check(frame, "2018-04-02", "2018-09-28", SESSIONS, reference(early, 1e7))
    assert other["entity_check"] == "fail" and "(outside the window)" in other["entity_notes"]


def test_byte_count_skips_corrupt_members_of_the_shared_raw_index(tmp_path):
    good = lambda line: gzip.compress((line + "\n").encode(), mtime=0)
    header = "fetched_utc,source,url_redacted,http_status,bytes,sha256,cache_path"
    broken = bytearray(good("2026-10-01T00:00:02+00:00,tiingo,u,200,999,x,p"))
    broken[12:16] = b"\xff\xff\xff\xff"  # a member that does not decompress
    data = (good(header) + good("2026-10-01T00:00:01+00:00,tiingo,u,200,1000,x,p") + bytes(broken)
            + good("2026-10-01T00:00:03+00:00,sec,u,200,5000,x,p")
            + good("2026-09-30T23:59:59+00:00,tiingo,u,200,7000,x,p")
            + good("2026-10-01T00:00:04+00:00,tiingo,u,200,250,x,p"))
    (tmp_path / "raw_index.csv.gz").write_bytes(data)
    text, bad = tiingo.read_gzip_members(tmp_path / "raw_index.csv.gz")
    assert bad >= 1 and text.count("\n") == 5
    assert tiingo.index_bytes("2026-10", path=tmp_path / "raw_index.csv.gz") == 1250
    raw = tmp_path / "raw"
    common.atomic_write(raw / "AAA__2011-06-01_2026-08-31.json.gz", gzip.compress(b"x" * 4321, mtime=0))
    assert tiingo.own_bytes(datetime.now(timezone.utc).strftime("%Y-%m"), raw) == 4321


def write_supported(directory, rows):
    import zipfile

    directory.mkdir(parents=True, exist_ok=True)
    text = "ticker,exchange,assetType,priceCurrency,startDate,endDate\n" + "".join(",".join(r) + "\n" for r in rows)
    with zipfile.ZipFile(directory / "supported_tickers_2026-10-01.zip", "w") as archive:
        archive.writestr("supported_tickers.csv", text)


def test_a_row_hidden_by_a_later_tiingo_row_is_not_asked(sandbox):
    sandbox["candidates"] = write_candidates(sandbox["tmp"] / "hidden.csv", [
        ("1", "OLD", "2018-01-21", "2019-12-31", "B_A_float_ge_1B", "tiingo", "pending", "2026-10", "10", "2019-12-20"),
        ("2", "SHR", "2018-01-21", "2019-12-31", "B_A_float_ge_1B", "tiingo", "pending", "2026-10", "20", "2019-12-20"),
        ("3", "SHR", "2014-01-01", "2026-08-31", "V_verify_sample", "tiingo", "pending", "2026-10", "30", "2026-09-30"),
        ("4", "NEW", "2018-01-21", "2019-12-31", "B_A_float_ge_1B", "tiingo", "pending", "2026-10", "40", "2019-12-31"),
    ])
    write_supported(sandbox["tmp"] / "prefilter", [
        ("OLD", "NASDAQ", "Stock", "USD", "1999-01-04", "2019-12-20"), ("OLD", "NASDAQ", "ETF", "USD", "2023-12-14", "2026-09-30"),
        ("SHR", "NASDAQ", "Stock", "USD", "2012-01-03", "2019-12-20"), ("SHR", "NASDAQ", "Stock", "USD", "2014-01-02", "2026-09-30"),
        ("NEW", "NASDAQ", "Stock", "USD", "2010-01-04", "2019-12-31")])
    sandbox["answers"] = {t: sandbox["good"] for t in ("OLD", "SHR", "NEW")}
    assert run(sandbox) == 0
    asked = [u.split("/daily/")[1].split("/")[0] for u in sandbox["urls"]]
    assert asked == ["shr", "new"]  # SHR is still asked for its V row, which is the latest row
    status = tiingo.load_status().set_index("security_id")
    assert status.loc["1", "status"] == "wrong_entity" and status.loc["1", "entity_check"] == "precheck"
    assert "ETF" in status.loc["1", "entity_notes"]
    assert status.loc["2", "entity_check"] != "precheck" and status.loc["4", "status"] == "done_review"
    assert json.loads(tiingo.SUMMARY.read_text())["tickers_not_asked_hidden_by_later_row"] == ["OLD"]
    # After the full run, without clearing the status file: the precheck row can be asked on purpose.
    assert run(sandbox, "--tickers", "old") == 0 and len(sandbox["urls"]) == 2  # final: not asked
    etf = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2023-12-14", "2026-08-31")]
    sandbox["answers"]["OLD"] = json.dumps(tiingo_rows(etf)).encode()
    assert run(sandbox, "--fetch-shadowed", "--tickers", "old,new") == 0
    assert [u.split("/daily/")[1].split("/")[0] for u in sandbox["urls"]][2:] == ["old"]  # NEW is final
    status = tiingo.load_status().set_index("security_id")
    # the answer is the ETF row, not the matched one: wrong_entity on the served range, no price file
    assert status.loc["1", "status"] == "wrong_entity" and status.loc["1", "served_row"] == "2023-12-14..2026-09-30"
    assert "another company" in status.loc["1", "entity_notes"] and not (tiingo.PRICES_DIR / "OLD.csv.gz").exists()
    assert status.loc["4", "prices_path"].endswith("NEW.csv.gz")


def test_live_runs_need_the_owners_already_used_count(sandbox):
    with pytest.raises(SystemExit):
        tiingo.main(["--candidates", str(sandbox["candidates"]), "--spacing", "0"])
    with pytest.raises(SystemExit):
        run(sandbox, "--fetch-shadowed")  # --fetch-shadowed names its tickers
    assert sandbox["urls"] == []


def test_a_403_refuses_one_ticker_and_two_in_a_row_stop_the_run(sandbox):
    sandbox["answers"] = {"AAA": 403, "BBB": sandbox["good"], "CCC": sandbox["good"]}
    assert run(sandbox) == 0
    status = tiingo.load_status().set_index("ticker_for_source")["status"]
    assert status["AAA"] == "refused" and status["BBB"] == "done_review"
    assert run(sandbox) == 0 and len(sandbox["urls"]) == 3  # refused is final: not asked again
    sandbox["candidates"] = write_candidates(sandbox["tmp"] / "two.csv", [
        ("1", "XXX", "2018-01-21", "2019-12-31", "B_A_float_ge_1B", "tiingo", "pending", "2026-10", "10"),
        ("2", "YYY", "2018-01-21", "2019-12-31", "B_A_float_ge_1B", "tiingo", "pending", "2026-10", "20"),
        ("3", "ZZZ", "2018-01-21", "2019-12-31", "B_A_float_ge_1B", "tiingo", "pending", "2026-10", "30")])
    sandbox["answers"] = {"XXX": 403, "YYY": 403, "ZZZ": sandbox["good"]}
    assert run(sandbox) == 1
    assert "refusals" in json.loads(tiingo.SUMMARY.read_text())["stop"] and len(sandbox["urls"]) == 5


def test_an_interrupted_run_writes_its_summary_and_releases_the_lock(sandbox, monkeypatch):
    sandbox["answers"] = {"AAA": sandbox["good"], "BBB": sandbox["good"]}
    real = tiingo.fetch_one

    def stop_on_bbb(ticker, *args, **kwargs):
        if ticker == "BBB":
            raise KeyboardInterrupt
        return real(ticker, *args, **kwargs)

    monkeypatch.setattr(tiingo, "fetch_one", stop_on_bbb)
    with pytest.raises(KeyboardInterrupt):
        run(sandbox)
    summary = json.loads(tiingo.SUMMARY.read_text())
    assert summary["stop"].startswith("interrupted") and summary["tickers_processed"] == ["AAA"]
    assert not tiingo.LOCK.exists()


def test_no_rows_in_the_window_is_wrong_entity_only_when_another_row_answered():
    days = SESSIONS[(SESSIONS >= "2018-02-01") & (SESSIONS <= "2018-06-29")]
    check = tiingo.entity_check(frame_for(days), "2019-01-01", "2019-06-01", SESSIONS)
    assert tiingo.row_status(check) == "no_data_in_window"
    rows = [{"start": "2010-01-04", "end": "2019-12-31", "served": False, "asset_type": "Stock", "exchange": "NASDAQ"},
            {"start": "2018-02-01", "end": "2018-06-29", "served": True, "asset_type": "Stock", "exchange": "NYSE"}]
    row = pd.Series({"tiingo_row_start": "2010-01-04", "tiingo_row_end": "2019-12-31", "tiingo_flags": ""})
    verified = tiingo.verify_served(check, row, frame_for(days), rows)
    assert tiingo.row_status(verified) == "wrong_entity" and verified["served_row"] == "2018-02-01..2018-06-29"
    ok = tiingo.entity_check(frame_for(days), "2018-02-01", "2018-06-29", SESSIONS)
    flagged = pd.Series({"tiingo_row_start": "2018-02-01", "tiingo_row_end": "2018-06-29", "tiingo_flags": "shared:x"})
    out = tiingo.verify_served(ok, flagged, frame_for(days), rows)
    assert out["entity_check"] == "review" and "no reference confirms" in out["entity_notes"]


def test_field_summary_counts_duplicates_off_session_rows_and_both_tolerances():
    days = [d.strftime("%Y-%m-%d") for d in SESSIONS[:10]]
    prices = tiingo_rows(days)
    prices.append(dict(prices[-1]))  # a duplicate date
    prices.append({**prices[0], "date": "2018-01-06T00:00:00.000Z"})  # a Saturday
    prices[5]["adjClose"] *= 1 + 5e-8
    summary = tiingo.field_summary(prices, tiingo.to_frame(prices), SESSIONS)
    assert summary["dup_dates"] == 1 and summary["non_session_rows"] == 1
    assert summary["adj_identity_bad_rows"] >= 1 and summary["adj_identity_bad_rows_1e6"] == 0


def test_spacing_spreads_requests_and_counts_seeded_stamps():
    now = datetime.now(timezone.utc)
    seed = pd.DataFrame({"fetched_utc": [(now - timedelta(seconds=10)).isoformat(timespec="seconds")]})
    limiter = tiingo.make_limiter(45, 900, seed, now=now, spacing=60)
    assert 45 < tiingo.limiter_delay(limiter) <= 51  # the last request was 10 s ago
    assert tiingo.limiter_delay(tiingo.make_limiter(45, 900, seed, now=now)) == 0
